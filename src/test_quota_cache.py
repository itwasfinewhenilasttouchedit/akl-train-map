"""Unit-test live.fetch_both()'s update caching WITHOUT a registered key.

The quota'd path cannot be exercised from this shell (no AT_API_KEY), but the
caching logic is what changed, so test that against a stub transport. Counts
calls per endpoint and checks the failure behaviour, which is the part that
could silently break every ETA on the map.

Cases:
 1. first call fetches both
 2. second call within TTL fetches positions only, and still returns the
    SAME updates (frugal must not mean empty)
 3. after the TTL expires, updates are refetched
 4. a FAILING tripupdates fetch falls back to last good, not to []
 5. a failure with no last good returns [] rather than raising
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

import live  # noqa: E402

CALLS = {"v": 0, "t": 0}
FAIL_UPDATES = False


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        if self._p is None:
            raise httpx.HTTPError("boom")

    def json(self):
        return {"response": {"entity": self._p}}


async def fake_get(self, url, *a, **kw):
    u = str(url)
    if "vehiclelocations" in u:
        CALLS["v"] += 1
        return FakeResp([{"veh": CALLS["v"]}])
    CALLS["t"] += 1
    if FAIL_UPDATES:
        return FakeResp(None)
    return FakeResp([{"upd": CALLS["t"]}, {"upd": "b"}])


httpx.AsyncClient.get = fake_get

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s %s" % (label, detail))
    else:
        failed += 1
        print("  FAIL %s %s" % (label, detail))


async def main():
    global FAIL_UPDATES
    print("UPDATES_TTL_S = %s" % live.UPDATES_TTL_S)
    print("=" * 70)

    # reset cache
    live._updates_cache[0], live._updates_cache[1] = 0.0, None
    CALLS["v"] = CALLS["t"] = 0

    print("\n1. first call fetches both")
    v, u = await live.fetch_both("stub")
    check("vehicles fetched", CALLS["v"] == 1, "(v=%d)" % CALLS["v"])
    check("updates fetched", CALLS["t"] == 1, "(t=%d)" % CALLS["t"])
    check("updates non-empty", len(u) == 2, "(%d entities)" % len(u))
    first_u = u

    print("\n2. second call within TTL: positions only, updates still served")
    v2, u2 = await live.fetch_both("stub")
    check("vehicles refetched", CALLS["v"] == 2, "(v=%d)" % CALLS["v"])
    check("updates NOT refetched", CALLS["t"] == 1, "(t=%d)" % CALLS["t"])
    check("updates still returned", len(u2) == 2, "(%d entities)" % len(u2))
    check("same updates as before", u2 == first_u)
    check("positions are fresh, not cached", v2 != v, "(%s vs %s)" % (v2, v))

    print("\n3. after TTL expiry, updates refetched")
    live._updates_cache[0] = live._updates_cache[0] - (live.UPDATES_TTL_S + 1)
    v3, u3 = await live.fetch_both("stub")
    check("updates refetched", CALLS["t"] == 2, "(t=%d)" % CALLS["t"])
    check("fresh updates returned", len(u3) == 2)

    print("\n4. failing tripupdates falls back to last good, NOT []")
    FAIL_UPDATES = True
    live._updates_cache[0] = live._updates_cache[0] - (live.UPDATES_TTL_S + 1)
    before_t = CALLS["t"]
    v4, u4 = await live.fetch_both("stub")
    check("it tried", CALLS["t"] == before_t + 1)
    check("did NOT return empty", len(u4) == 2, "(%d entities)" % len(u4))
    check("positions still returned", bool(v4))

    print("\n5. failure with no last good returns [] and does not raise")
    live._updates_cache[0], live._updates_cache[1] = 0.0, None
    v5, u5 = await live.fetch_both("stub")
    check("empty, no exception", u5 == [], "(%r)" % (u5,))
    check("positions still returned", bool(v5))

    print("\n6. a transient failure must not poison the cache")
    FAIL_UPDATES = False
    live._updates_cache[0], live._updates_cache[1] = 0.0, None
    await live.fetch_both("stub")          # good -> cached
    FAIL_UPDATES = True
    live._updates_cache[0] = live._updates_cache[0] - (live.UPDATES_TTL_S + 1)
    _, u6 = await live.fetch_both("stub")  # fails -> last good
    FAIL_UPDATES = False
    check("last good survived the failure", len(u6) == 2, "(%d)" % len(u6))

    print()
    print("=" * 70)
    print("call budget over the run: vehicles=%d tripupdates=%d"
          % (CALLS["v"], CALLS["t"]))
    print("%d passed, %d failed" % (passed, failed))


asyncio.run(main())
sys.exit(1 if failed else 0)
