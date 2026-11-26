#!/usr/bin/env python3
"""Gate: every GET route must also answer HEAD.

Why this exists. FastAPI's @app.get registers methods={"GET"} only -- unlike
Starlette's own Route, it does not add HEAD. The consequence was measured in
the production access log on 2026-11-26: a crawler sent `HEAD /sitemap.xml`
and received 405 while `GET /sitemap.xml` returned 200. Link checkers, uptime
monitors and some crawlers probe with HEAD first and read 405 as "no such
route", which is a bad answer for the two paths (/ and /sitemap.xml) that
search engines care about most.

server.py fixes this by patching the route table after all routes are
declared. That is deliberately blanket so a @app.get added later cannot
forget -- and this gate fails if the patch is ever removed or if it stops
covering a route.

Run: ARCHIVE_DB=off python src/test_head.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Isolate from real data before importing server:
#   - ARCHIVE_DB=off   keeps the archive writer out of the way
#   - FEEDBACK_DB      a scratch file, so /api/health's feedback count works.
#     Without an initialised DB, feedback.stats() raises "no such table:
#     reports" and /api/health 500s -- which would look like a HEAD failure
#     rather than the test's own missing fixture.
#   - CANONICAL_HOST   MUST be empty. If set, the redirect middleware answers
#     every non-/api/ path with a 301 and this suite would assert against
#     redirects instead of real responses.
os.environ["ARCHIVE_DB"] = "off"
os.environ["CANONICAL_HOST"] = ""
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["FEEDBACK_DB"] = _tmp.name

import server  # noqa: E402
import feedback  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

feedback.init_db()

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


print("test_head: HEAD must work wherever GET does")

# ---------------------------------------------------------------- route table
get_routes = [r for r in server.app.routes
              if isinstance(r, APIRoute) and "GET" in r.methods]
missing = [r.path for r in get_routes if "HEAD" not in r.methods]
check("every GET route declares HEAD", not missing,
      "missing on: %s" % missing)
check("there are GET routes to check at all", len(get_routes) > 5,
      "found %d" % len(get_routes))

# Write-only routes must NOT have gained HEAD -- 405 is the honest answer
# there, and a blanket "add HEAD everywhere" would be wrong.
wrong = [r.path for r in server.app.routes
         if isinstance(r, APIRoute) and "GET" not in r.methods
         and "HEAD" in r.methods]
check("write-only routes did not gain HEAD", not wrong, "wrongly added: %s" % wrong)

# ---------------------------------------------------------------- live probes
client = TestClient(server.app)

# The SEO-critical pair plus the shell and a static asset. /api/health is
# included because monitors HEAD it.
for path in ("/", "/sitemap.xml", "/robots.txt", "/api/health",
             "/static/pixel.css"):
    g = client.get(path)
    h = client.head(path)
    check("GET %s is 200" % path, g.status_code == 200, "got %s" % g.status_code)
    check("HEAD %s is 200" % path, h.status_code == 200,
          "got %s (GET was %s)" % (h.status_code, g.status_code))
    # A HEAD response must carry the same headers but no body. Serving a body
    # on HEAD is its own bug: some proxies treat the bytes as the next
    # response and desynchronise the connection.
    check("HEAD %s has empty body" % path, h.content == b"",
          "got %d bytes" % len(h.content))
    check("HEAD %s keeps content-type" % path,
          h.headers.get("content-type") == g.headers.get("content-type"),
          "%r vs %r" % (h.headers.get("content-type"),
                        g.headers.get("content-type")))

# A POST-only route must still refuse HEAD. This proves the patch is scoped
# and did not simply make every route accept everything.
r = client.head("/api/feedback/1/vote")
check("HEAD on a POST-only route is still 405", r.status_code == 405,
      "got %s" % r.status_code)

# /api/ paths are exempt from the canonical redirect; confirm HEAD on an API
# route returns real headers rather than a hop, since monitors do not follow.
h = client.head("/api/health")
check("HEAD /api/health is not a redirect", h.status_code == 200,
      "got %s" % h.status_code)

os.unlink(_tmp.name)

print()
if fails:
    print("FAILED %d check(s): %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("test_head: all checks passed")
