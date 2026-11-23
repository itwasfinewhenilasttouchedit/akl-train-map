#!/usr/bin/env python3
"""Tests for the CANONICAL_HOST redirect middleware.

Why this suite exists at all: the redirect is the one feature in this app that
can take PRODUCTION DOWN BY WORKING. Fly's platform health check
(fly.toml [[http_service.checks]] path = "/api/health") does not follow
redirects. If the middleware 301s that path, Fly concludes the machine is sick,
destroys it, and the replacement does the same -- a redirect that deletes its
own host. test_health_never_redirects is the guard on exactly that, and it must
never be relaxed.

The env var is read at import time, so each case re-imports server with a
patched environment rather than mutating a live module attribute.

Run:  cd src && ../.venv/bin/python test_canonical.py
"""
import importlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

# Every case hits /api/health, which calls feedback.stats() -> real SQL against
# the feedback DB. Point FEEDBACK_DB at a throwaway file and initialise the
# schema, or the tests inherit whatever DB happens to exist on the machine.
#
# This bit me: the suite passed locally (output/feedback.db was already there
# from development) and failed in CI with "no such table: reports" on a clean
# checkout. A test that depends on ambient local state is not a gate. The temp
# dir must be created BEFORE the first server import, since FEEDBACK_DB is read
# at import time.
_TMP = tempfile.mkdtemp(prefix="test_canonical_")
os.environ["FEEDBACK_DB"] = os.path.join(_TMP, "feedback.db")
os.environ["ARCHIVE_DB"] = "off"
os.environ["ORDER_LATCH"] = os.path.join(_TMP, "order_latch.json")
os.environ["TRAINS_CACHE"] = os.path.join(_TMP, "last_trains.json")

import feedback  # noqa: E402

feedback.init_db()


def client_with(canonical: str | None):
    """Import a fresh server module with CANONICAL_HOST set (or absent)."""
    saved = os.environ.get("CANONICAL_HOST")
    if canonical is None:
        os.environ.pop("CANONICAL_HOST", None)
    else:
        os.environ["CANONICAL_HOST"] = canonical
    for mod in ("server",):
        if mod in sys.modules:
            del sys.modules[mod]
    server = importlib.import_module("server")
    if saved is None:
        os.environ.pop("CANONICAL_HOST", None)
    else:
        os.environ["CANONICAL_HOST"] = saved
    # follow_redirects=False so we assert on the 301 itself, not its target.
    return TestClient(server.app, follow_redirects=False)


class TestCanonicalRedirect(unittest.TestCase):

    def test_health_never_redirects(self):
        """THE critical case: Fly's health check must get a real response.

        A 301 here makes Fly destroy the machine. If this test ever fails,
        do not deploy -- the redirect would take prod offline.
        """
        c = client_with("hoki.co.nz")
        r = c.get("/api/health")
        self.assertNotIn(r.status_code, (301, 302, 307, 308),
                         "health check was redirected; Fly would kill the VM")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("ok") is not None)

    def test_api_paths_exempt(self):
        """Monitors and the archive reader want data, not a hop."""
        c = client_with("hoki.co.nz")
        for path in ("/api/health", "/api/trains", "/api/archive"):
            r = c.get(path)
            self.assertNotIn(r.status_code, (301, 302, 307, 308),
                             f"{path} should not redirect")

    def test_root_redirects(self):
        c = client_with("hoki.co.nz")
        r = c.get("/")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r.headers["location"], "https://hoki.co.nz/")

    def test_query_and_path_preserved(self):
        """A shared deep link must survive the hop intact."""
        c = client_with("hoki.co.nz")
        r = c.get("/admin?token=abc&x=1")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r.headers["location"],
                         "https://hoki.co.nz/admin?token=abc&x=1")

    def test_static_redirects(self):
        c = client_with("hoki.co.nz")
        r = c.get("/static/pixel.css")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r.headers["location"],
                         "https://hoki.co.nz/static/pixel.css")

    def test_disabled_by_default(self):
        """Unset env => the app serves normally. This is the phone's config,
        so a bug here would blank the real site, not just the mirror."""
        c = client_with(None)
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        r = c.get("/api/health")
        self.assertEqual(r.status_code, 200)

    def test_empty_string_disabled(self):
        """CANONICAL_HOST="" must behave as unset, not redirect to https:// ."""
        c = client_with("")
        r = c.get("/")
        self.assertEqual(r.status_code, 200)

    def test_whitespace_only_disabled(self):
        """A stray space in a Fly secret must not produce https:// /."""
        c = client_with("   ")
        r = c.get("/")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
