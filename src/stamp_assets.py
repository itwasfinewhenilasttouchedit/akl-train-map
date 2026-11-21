"""Stamp cached asset URLs in web/index.html with a content hash.

Why this exists: a shipped, server-verified fix stayed invisible on mobile
because `FileResponse` sends `etag`/`last-modified` but no `Cache-Control`.
Without an explicit directive a browser may invent a freshness lifetime from
last-modified (RFC 9111 heuristic expiration) and never send the conditional
request the etag exists to answer. iOS Safari is notably aggressive here.

The server now sends `no-cache, must-revalidate` for the HTML shell, which
fixes the shell. But the shell links `/static/pixel.css`, and that URL is
stable, so a cached copy of the OLD stylesheet can still be paired with new
HTML. Headers help only if the browser revalidates; a changed URL cannot be
served from cache at all. So the URL carries a content hash.

Run before deploy. Idempotent: re-running with unchanged assets is a no-op, so
it is safe in a build step or by hand.

    .venv/bin/python src/stamp_assets.py            # stamp
    .venv/bin/python src/stamp_assets.py --check    # verify, exit 1 if stale

`--check` is the useful one in CI: it fails if someone edits pixel.css and
forgets to re-stamp, which is exactly the mistake that produces "I still see
the old version" bug reports.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = WEB / "index.html"

# Assets linked from the shell with a stable URL. rail_geo.json is fetched by
# JS rather than linked, and is deliberately left alone: it is large, changes
# only when the timetable is rebaked, and is already fetched fresh per session.
ASSETS = ["pixel.css"]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def stamp(check_only: bool = False) -> int:
    html = HTML.read_text(encoding="utf-8")
    original = html
    stale: list[str] = []

    for name in ASSETS:
        f = WEB / name
        if not f.exists():
            print("missing asset: %s" % name, file=sys.stderr)
            return 2
        want = digest(f)
        # Match the asset with or without an existing ?v= stamp.
        pat = re.compile(r'(/static/' + re.escape(name) + r')(\?v=([0-9a-f]+))?')
        found = pat.search(html)
        if not found:
            print("asset not referenced in index.html: %s" % name, file=sys.stderr)
            return 2
        have = found.group(3)
        if have != want:
            stale.append("%s: %s -> %s" % (name, have or "(unstamped)", want))
        html = pat.sub(lambda m: "%s?v=%s" % (m.group(1), want), html)

    if check_only:
        if stale:
            print("STALE asset stamps:")
            for s in stale:
                print("  " + s)
            print("run: .venv/bin/python src/stamp_assets.py")
            return 1
        print("asset stamps current: " + ", ".join(
            "%s=%s" % (n, digest(WEB / n)) for n in ASSETS))
        return 0

    if html != original:
        HTML.write_text(html, encoding="utf-8")
        print("stamped:")
        for s in stale:
            print("  " + s)
    else:
        print("no change; stamps already current")
    return 0


if __name__ == "__main__":
    sys.exit(stamp(check_only="--check" in sys.argv))
