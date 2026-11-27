#!/usr/bin/env python3
"""Gate /api/health's `revision` field.

WHY THIS FILE EXISTS
--------------------
Phase 2 (`20abf65`) was a behaviour-preserving refactor, and production looked
perfectly healthy afterwards -- but the invariants that looked healthy
(`in_service == drawn icons`, no trailing/front contradictions) held BEFORE
the change as well. So "healthy" proved only "not broken", never "deployed".
There was no way to tell which revision was running:

  * `server.py` is not served, so no content hash to compare;
  * the deploy host's SSH host key is not covered by our certificate
    authority, so it cannot simply be asked;
  * the deploy timer redeploys unattended every five minutes.

An unverifiable deploy on an unattended timer is the actual hazard, not the
refactor. `/api/health` now reports the commit, so every future deploy can be
confirmed from outside with one curl.

This gate pins that it is a real resolved sha and not, say, the literal string
"ref: refs/heads/main" -- which is what a naive `.git/HEAD` read returns, and
which would look populated while telling you nothing.

Run: ARCHIVE_DB=off .venv/bin/python src/test_revision.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="test_rev_")
os.environ["FEEDBACK_DB"] = os.path.join(_TMP, "feedback.db")
os.environ["ARCHIVE_DB"] = "off"
os.environ["ORDER_LATCH"] = os.path.join(_TMP, "latch.json")
os.environ["TRAINS_CACHE"] = os.path.join(_TMP, "cache.json")
os.environ.setdefault("AT_API_KEY", "")
os.environ.pop("APP_REVISION", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import feedback  # noqa: E402

feedback.init_db()   # api_health() reads report stats; the schema must exist
import server  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'PASS' if cond else 'FAIL'}: {name}"
          + (f" -- {detail}" if detail and not cond else ""))


rev = server._revision()
head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                      capture_output=True, text=True).stdout.strip()

ok("a revision is resolved at all", bool(rev), repr(rev))
ok("it is a 40-char hex sha, not a ref name",
   bool(rev and re.fullmatch(r"[0-9a-f]{40}", rev)), repr(rev))
ok("it is NOT the literal contents of .git/HEAD",
   bool(rev) and not str(rev).startswith("ref:"), repr(rev))
ok("it matches `git rev-parse HEAD`", rev == head, f"{rev} vs {head}")
ok("the module-level cache agrees with the function",
   server.REVISION == rev, f"{server.REVISION} vs {rev}")

print("\n-- APP_REVISION overrides, for deploys that ship no .git --")
os.environ["APP_REVISION"] = "deadbeef" * 5
ok("env wins over .git", server._revision() == "deadbeef" * 5)
os.environ["APP_REVISION"] = "  spaced  "
ok("env value is stripped", server._revision() == "spaced")
os.environ["APP_REVISION"] = "x" * 80
ok("env value is truncated to 40", len(server._revision() or "") == 40)
os.environ["APP_REVISION"] = ""
ok("an empty env var falls back to .git rather than reporting ''",
   server._revision() == head, repr(server._revision()))
os.environ.pop("APP_REVISION", None)

print("\n-- it must never break the app --")
import unittest.mock as mock  # noqa: E402
with mock.patch.object(server.Path, "read_text",
                       side_effect=OSError("no .git here")):
    try:
        got = server._revision()
        ok("a missing/unreadable .git returns None, does not raise", got is None,
           repr(got))
    except Exception as e:
        ok("a missing/unreadable .git returns None, does not raise", False,
           f"raised {type(e).__name__}: {e}")

print("\n-- the field is actually in the health payload --")
import asyncio  # noqa: E402
server.STATE["data"] = {"count": 3, "source": "test", "poll_ms": 1,
                        "trains": []}
server.STATE["error"] = None
h = asyncio.run(server.api_health())
ok("/api/health carries `revision`", "revision" in h, str(sorted(h)))
ok("and it is the real sha", h.get("revision") == head, repr(h.get("revision")))
ok("health still reports ok", h.get("ok") is True)
ok("the pre-existing keys survive",
   {"trains", "source", "poll_ms", "feedback", "last_fetch",
    "error"} <= set(h), str(sorted(h)))

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
for n, d in FAIL:
    print(f"  FAILED: {n}" + (f" -- {d}" if d else ""))
sys.exit(1 if FAIL else 0)
