#!/usr/bin/env python3
"""Gate the service count: count TRAINS, not GPS vehicles.

WHY THIS FILE EXISTS
A 6-car service is two coupled 3-car sets and AT publishes a separate GPS
vehicle for each half. live.collect() counts vehicles. The map draws one
marker per service (it skips `trailing_half`). So the HUD and the map
disagreed, and nothing in the test suite noticed: measured on production the
HUD claimed 71 in service while 39 markers existed -- an overstatement of 32,
i.e. nearly half the number shown was double-counted hardware.

39 was also exactly the count of distinct trip_ids, which is the independent
confirmation that the service figure, not the vehicle figure, is the honest
answer to "how many trains are running".

This test pins the arithmetic in server._annotate_consists() so the count
cannot silently drift back to counting vehicles.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="test_count_")
# Env is read at import time, so it must be set before importing server.
os.environ["FEEDBACK_DB"] = os.path.join(_TMP, "feedback.db")
os.environ["ARCHIVE_DB"] = "off"
os.environ["ORDER_LATCH"] = os.path.join(_TMP, "latch.json")
os.environ["TRAINS_CACHE"] = os.path.join(_TMP, "cache.json")
os.environ.setdefault("AT_API_KEY", "")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feedback  # noqa: E402
feedback.init_db()
import server  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def mk(unit, trip=None, in_service=True, trailing=False, partner=None):
    return {"unit": unit, "trip_id": trip, "in_service": in_service,
            "trailing_half": trailing, "partner_set": partner,
            "lat": -36.9, "lon": 174.6, "speed_kmh": 0.0}


print("== service count: coupled halves must not be counted twice ==")

# One 6-car service: two vehicles, one trip, the rear flagged trailing.
d = {"trains": [
        mk("AMP 1005", trip="T1", partner="1196"),
        mk("AMP 1196", trip="T1", partner="1005", trailing=True),
     ],
     "in_service": 2, "out_of_service": 0}
server._recount_services(d)
check("6-car pair counts as 1 service", d["in_service"] == 1, f"got {d['in_service']}")
check("vehicle figure retained as 2",
      d.get("vehicles_in_service") == 2, f"got {d.get('vehicles_in_service')}")
check("coupled_halves reports 1", d.get("coupled_halves") == 1,
      f"got {d.get('coupled_halves')}")

# Two independent 3-car services: nothing trailing, so 2 services.
d = {"trains": [mk("AMP 281", trip="T1"), mk("AMP 332", trip="T2")],
     "in_service": 2, "out_of_service": 0}
server._recount_services(d)
check("two single sets count as 2 services", d["in_service"] == 2,
      f"got {d['in_service']}")
check("no coupled halves reported", d.get("coupled_halves") == 0,
      f"got {d.get('coupled_halves')}")

# Out-of-service stock must never land in the service count.
d = {"trains": [
        mk("AMP 281", trip="T1"),
        mk("AMP 999", in_service=False),
        mk("AMP 998", in_service=False),
     ],
     "in_service": 1, "out_of_service": 2}
server._recount_services(d)
check("stabled stock excluded from services", d["in_service"] == 1,
      f"got {d['in_service']}")
check("out_of_service counted separately", d["out_of_service"] == 2,
      f"got {d['out_of_service']}")

# Mixed: one 6-car + one 3-car + one stabled unit = 2 services, 3 vehicles.
d = {"trains": [
        mk("AMP 1005", trip="T1", partner="1196"),
        mk("AMP 1196", trip="T1", partner="1005", trailing=True),
        mk("AMP 281", trip="T2"),
        mk("AMP 999", in_service=False),
     ],
     "in_service": 3, "out_of_service": 1}
server._recount_services(d)
check("mixed fleet: 2 services", d["in_service"] == 2, f"got {d['in_service']}")
check("mixed fleet: 3 in-service vehicles",
      d.get("vehicles_in_service") == 3, f"got {d.get('vehicles_in_service')}")
check("mixed fleet: 1 out of service", d["out_of_service"] == 1,
      f"got {d['out_of_service']}")

# The count must never exceed the number of markers the map would draw: the
# map's filter is `!trailing_half && in_service !== false`, so recompute it
# the same way the client does and require agreement.
drawn = sum(1 for t in d["trains"]
            if t.get("in_service") is not False and not t.get("trailing_half"))
check("count equals markers the map draws", d["in_service"] == drawn,
      f"count={d['in_service']} drawn={drawn}")

# Empty feed must be 0, not a crash and not a stale figure.
d = {"trains": [], "in_service": 7, "out_of_service": 3}
server._recount_services(d)
check("empty feed counts 0 services", d["in_service"] == 0, f"got {d['in_service']}")
check("empty feed counts 0 out of service", d["out_of_service"] == 0,
      f"got {d['out_of_service']}")

print()
if fails:
    print(f"FAILED {len(fails)}: {', '.join(fails)}")
    raise SystemExit(1)
print("all service-count checks passed")
