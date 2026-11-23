#!/usr/bin/env bash
# Pull-based deployer for the self-hosted instance.
#
# WHY PULL AND NOT PUSH: this host sits on an isolated DMZ VLAN whose gateway
# denies all inbound from every internal VLAN and from the WAN. A CI runner
# cannot reach it, and giving it a path in would undo the isolation that makes
# hosting on a phone acceptable at all. Egress is allowed, so the host asks
# GitHub what the current commit is. No inbound hole, no deploy credentials on
# the handset -- the repo is public, so the fetch needs no auth whatsoever.
#
# WHAT IT GUARANTEES
#   * Only rebuilds when origin/main actually moved (SHA comparison). A no-op
#     run costs one HTTPS fetch, not a 2-minute arm64 image build.
#   * Never leaves the site down on a bad commit: the new image is built and
#     health-checked BEFORE the running container is replaced, and a failure
#     rolls the working tree back to the last good SHA and exits non-zero
#     without touching the live container.
#   * Single-flight. A flock stops a slow build and the next timer tick from
#     fighting over the working tree.
#   * .env and data/ are never touched. `git reset --hard` only governs tracked
#     files; the AT key and the archive DB are not tracked and must not be.
#
# Usage:  deploy_pull.sh [--force] [--dry-run]
#   --force    rebuild even when the SHA has not changed
#   --dry-run  report what would happen, change nothing
#
# Exit codes: 0 up to date or deployed; 1 deploy failed (rolled back);
#             2 preconditions wrong (no repo, no .env, docker missing).

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/srv/akl-train-map}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.selfhosted.yaml}"
BRANCH="${BRANCH:-main}"
# Port the container publishes on the host; health is probed here, not through
# the tunnel, so a Cloudflare problem is never mistaken for a bad deploy.
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8731/api/health}"
HEALTH_TRIES="${HEALTH_TRIES:-30}"
HEALTH_SLEEP="${HEALTH_SLEEP:-5}"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/akl-deploy}"
LOCK_FILE="$STATE_DIR/deploy.lock"

FORCE=0
DRY=0
for a in "$@"; do
  case "$a" in
    --force)   FORCE=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

mkdir -p "$STATE_DIR"

log() { printf '%s %s\n' "$(date -Is)" "$*"; }
die() { log "FATAL $*"; exit "${2:-2}"; }

# ---------------------------------------------------------------- single flight
# Taken before any state is read: two concurrent runs could otherwise both see
# "behind", both build, and race the container swap.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another deploy holds the lock; exiting without action"
  exit 0
fi

# ---------------------------------------------------------------- precondition
[ -d "$REPO_DIR/.git" ] || die "no git repo at $REPO_DIR"
cd "$REPO_DIR"
command -v docker >/dev/null || die "docker not on PATH"
[ -f "$COMPOSE_FILE" ] || die "missing $COMPOSE_FILE in $REPO_DIR"
# .env carries AT_API_KEY. Without it the app starts and then serves an empty
# map, which looks like an AT outage rather than a deploy fault -- so refuse.
[ -f .env ] || die "missing .env (holds AT_API_KEY); provision it before deploying"

# A shallow clone cannot be reset to an arbitrary fetched commit cleanly, and
# the original clone here was --depth 1. Deepen once, idempotently.
if [ -f .git/shallow ]; then
  log "shallow clone detected; deepening to full history (one time)"
  [ "$DRY" = 1 ] || git fetch --unshallow --quiet origin || die "unshallow failed"
fi

# ---------------------------------------------------------------- what's there
LOCAL_SHA="$(git rev-parse HEAD)"
log "fetching origin/$BRANCH"
git fetch --quiet origin "$BRANCH" || die "git fetch failed (DMZ egress blocked?)" 1
REMOTE_SHA="$(git rev-parse "origin/$BRANCH")"

log "local  $LOCAL_SHA"
log "remote $REMOTE_SHA"

# Local modifications to TRACKED files mean someone hand-edited prod. Report
# them: the reset below is about to discard them, and silently eating a manual
# hotfix is how you lose the only copy of a fix made at 2am.
DIRTY="$(git status --porcelain --untracked-files=no)"
if [ -n "$DIRTY" ]; then
  log "WARNING tracked files modified locally; reset will discard:"
  printf '%s\n' "$DIRTY" | sed 's/^/    /'
fi

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ] && [ -z "$DIRTY" ] && [ "$FORCE" = 0 ]; then
  log "already up to date; nothing to do"
  exit 0
fi

if [ "$DRY" = 1 ]; then
  log "DRY RUN: would reset to $REMOTE_SHA, build, health-check, then swap"
  [ "$LOCAL_SHA" = "$REMOTE_SHA" ] || git log --oneline "$LOCAL_SHA..$REMOTE_SHA" | sed 's/^/    /'
  exit 0
fi

# ---------------------------------------------------------------- roll forward
# Record the good SHA for rollback before mutating anything.
echo "$LOCAL_SHA" > "$STATE_DIR/last_good_sha"

rollback() {
  log "ROLLBACK to $LOCAL_SHA"
  git reset --hard --quiet "$LOCAL_SHA" || log "rollback reset FAILED -- manual repair needed"
  # The live container was never stopped unless the swap stage reached it, so
  # in the common failure (build or health failure) the site stayed up on the
  # old image and there is nothing further to restore.
}

log "resetting working tree to origin/$BRANCH"
git reset --hard --quiet "origin/$BRANCH" || die "reset failed" 1

log "validating compose file"
if ! docker compose -f "$COMPOSE_FILE" config --quiet; then
  log "compose file invalid at $REMOTE_SHA"
  rollback
  exit 1
fi

# Build into a throwaway tag first. The live tag (akl-train-map:local) is only
# re-pointed after the new image proves it answers /api/health, so a commit
# that fails to build or boot cannot take the site down.
CAND_TAG="akl-train-map:candidate"
log "building candidate image (arm64, expect ~40s warm / ~2m cold)"
if ! docker build --quiet -t "$CAND_TAG" -f Dockerfile . ; then
  log "build FAILED at $REMOTE_SHA; live container untouched"
  rollback
  exit 1
fi

# -------------------------------------------------------- smoke-test candidate
# Boot the candidate on a spare port with its own throwaway state dir, so the
# test cannot corrupt the live archive DB or order latch.
SMOKE_NAME="akl-smoke-$$"
SMOKE_PORT="${SMOKE_PORT:-8739}"
SMOKE_DATA="$(mktemp -d "$STATE_DIR/smoke.XXXXXX")"
cleanup_smoke() {
  docker rm -f "$SMOKE_NAME" >/dev/null 2>&1 || true
  rm -rf "$SMOKE_DATA" 2>/dev/null || true
}
trap cleanup_smoke EXIT

log "smoke-testing candidate on :$SMOKE_PORT"
# Deliberately NOT passing AT keys: a boot test must not consume AT quota or
# depend on AT being up. /api/health answers from local state, which is exactly
# the "does this image start and serve" question being asked.
docker run -d --name "$SMOKE_NAME" \
  -p "127.0.0.1:$SMOKE_PORT:8080" \
  -e PORT=8080 \
  -e FEEDBACK_DB=/data/feedback.db \
  -e ARCHIVE_DB=off \
  -e ORDER_LATCH=/data/order_latch.json \
  -e TRAINS_CACHE=/data/last_trains.json \
  -v "$SMOKE_DATA:/data" \
  "$CAND_TAG" >/dev/null || { log "candidate failed to start"; rollback; exit 1; }

SMOKE_OK=0
for i in $(seq 1 24); do
  if curl -fsS -m 5 "http://127.0.0.1:$SMOKE_PORT/api/health" >/dev/null 2>&1; then
    SMOKE_OK=1; log "candidate healthy after ${i}x2s"; break
  fi
  sleep 2
done
if [ "$SMOKE_OK" = 0 ]; then
  log "candidate never became healthy; last 40 log lines:"
  docker logs --tail 40 "$SMOKE_NAME" 2>&1 | sed 's/^/    /'
  rollback
  exit 1
fi
cleanup_smoke
trap - EXIT

# ------------------------------------------------------------------- swap live
# Only now is the live deployment touched. Promote the proven image.
log "promoting candidate to akl-train-map:local"
docker tag "$CAND_TAG" akl-train-map:local

log "recreating live container"
if ! docker compose -f "$COMPOSE_FILE" up -d; then
  log "compose up FAILED; attempting restart on previous code"
  rollback
  docker compose -f "$COMPOSE_FILE" up -d || log "restart FAILED -- SITE IS DOWN, manual action required"
  exit 1
fi

# --------------------------------------------------------------- verify live
log "verifying live health at $HEALTH_URL"
LIVE_OK=0
for i in $(seq 1 "$HEALTH_TRIES"); do
  if curl -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1; then
    LIVE_OK=1; log "live healthy after $((i * HEALTH_SLEEP))s"; break
  fi
  sleep "$HEALTH_SLEEP"
done

if [ "$LIVE_OK" = 0 ]; then
  log "live health check FAILED after deploy; rolling back code and rebuilding"
  docker logs --tail 40 akl-train-map 2>&1 | sed 's/^/    /'
  rollback
  if docker build --quiet -t akl-train-map:local -f Dockerfile . \
     && docker compose -f "$COMPOSE_FILE" up -d; then
    log "rolled back to $LOCAL_SHA and restarted"
  else
    log "ROLLBACK REBUILD FAILED -- SITE MAY BE DOWN, manual action required"
  fi
  exit 1
fi

echo "$REMOTE_SHA" > "$STATE_DIR/deployed_sha"
log "DEPLOYED $REMOTE_SHA"
curl -fsS -m 5 "$HEALTH_URL" | head -c 400; echo
exit 0
