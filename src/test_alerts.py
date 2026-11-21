"""Tests for cancellation parsing and alert classification.

Both features read enums and timestamps straight off AT's feed, where the
known failure mode is silence: a wrong enum comparison yields "nothing is
cancelled", which looks exactly like a healthy network and never throws. So
the cases below are built around the traps that were actually measured --
string-vs-int enums, bare-string stop_time_update, and scheduled night works
that must not be shown as in force today.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import alerts                                            # noqa: E402
import live                                              # noqa: E402

PASS = FAIL = 0


def ok(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL %-56s got=%r want=%r" % (name, got, want))


def tu(trip_id, rel=None, stops=None, route="S-C-201"):
    t = {"trip_id": trip_id, "route_id": route}
    if rel is not None:
        t["schedule_relationship"] = rel
    return {"trip_update": {"trip": t, "stop_time_update": stops or []}}


# ---------- index_cancellations ----------
# A real rail stop_id -> code mapping, so the SKIPPED path can resolve.
real_sid = next(iter(live.STOP_IDS)) if live.STOP_IDS else None

ok("canceled as INT 3", "T1" in live.index_cancellations([tu("T1", rel=3)]), True)
ok("canceled as STR '3'", "T2" in live.index_cancellations([tu("T2", rel="3")]), True)
ok("scheduled int 0 not flagged", live.index_cancellations([tu("T3", rel=0)]), {})
ok("scheduled str '0' not flagged", live.index_cancellations([tu("T4", rel="0")]), {})
ok("missing rel not flagged", live.index_cancellations([tu("T5")]), {})
ok("ADDED(1) is not cancelled", live.index_cancellations([tu("T6", rel=1)]), {})
ok("no trip_id is skipped",
   live.index_cancellations([{"trip_update": {"trip": {}}}]), {})

c = live.index_cancellations([tu("T7", rel=3)])
ok("cancelled flag set", c["T7"]["cancelled"], True)
ok("route carried through", c["T7"]["route_id"], "S-C-201")
ok("no skipped stops on plain cancel", c["T7"]["skipped"], [])

# The crash that a first version hit: AT ships a bare string here sometimes.
ok("bare-string stop_time_update does not crash",
   "T8" in live.index_cancellations([tu("T8", rel=3, stops=["garbage"])]), True)
ok("None stop_time_update does not crash",
   "T9" in live.index_cancellations(
       [{"trip_update": {"trip": {"trip_id": "T9", "schedule_relationship": 3},
                         "stop_time_update": None}}]), True)

if real_sid:
    code = live.STOP_IDS[real_sid]
    sk = live.index_cancellations(
        [tu("TA", stops=[{"stop_id": real_sid, "schedule_relationship": 1}])])
    ok("SKIPPED stop flags a running trip", "TA" in sk, True)
    ok("SKIPPED trip not marked fully cancelled", sk["TA"]["cancelled"], False)
    ok("skipped stop resolved to code", sk["TA"]["skipped"], [code])
    sk2 = live.index_cancellations(
        [tu("TB", stops=[{"stop_id": real_sid, "schedule_relationship": "1"}])])
    ok("SKIPPED as string enum", sk2["TB"]["skipped"], [code])
    ok("SCHEDULED stop(0) not skipped",
       live.index_cancellations(
           [tu("TC", stops=[{"stop_id": real_sid, "schedule_relationship": 0}])]), {})
    # Unknown stop ids must be dropped, not passed through as raw AT ids.
    ok("unknown stop_id dropped",
       live.index_cancellations(
           [tu("TD", stops=[{"stop_id": "nope-123", "schedule_relationship": 1}])]), {})

# ---------- alerts.classify ----------
now = time.time()
ok("no active_period = in force", alerts.classify({}, now), "now")
ok("empty active_period = in force",
   alerts.classify({"active_period": []}, now), "now")
ok("covering window = now",
   alerts.classify({"active_period": [{"start": now - 60, "end": now + 60}]}, now), "now")
ok("open-ended started = now",
   alerts.classify({"active_period": [{"start": now - 60}]}, now), "now")
ok("expired = dropped",
   alerts.classify({"active_period": [{"start": now - 7200, "end": now - 3600}]}, now), None)
ok("starts in 1h = upcoming",
   alerts.classify({"active_period": [{"start": now + 3600}]}, now), "upcoming")
ok("starts in 48h = dropped",
   alerts.classify({"active_period": [{"start": now + 48 * 3600}]}, now), None)
ok("string timestamps coerced",
   alerts.classify({"active_period": [{"start": str(int(now - 60)),
                                       "end": str(int(now + 60))}]}, now), "now")
# Multi-period: a live window anywhere wins over an upcoming one.
ok("live window beats upcoming",
   alerts.classify({"active_period": [{"start": now + 3600},
                                      {"start": now - 10, "end": now + 10}]}, now), "now")
ok("junk period entry ignored",
   alerts.classify({"active_period": ["junk", {"start": now - 10}]}, now), "now")

# ---------- alerts._txt ----------
ok("prefers english",
   alerts._txt({"translation": [{"text": "mi", "language": "mi"},
                                {"text": "hello", "language": "en"}]}), "hello")
ok("falls back to first",
   alerts._txt({"translation": [{"text": "only", "language": "xx"}]}), "only")
ok("empty translated string", alerts._txt({}), "")
ok("non-dict is safe", alerts._txt("nope"), "")
ok("non-list translation is safe", alerts._txt({"translation": "x"}), "")

# ---------- alerts._rail_entities ----------
rail_routes = {"S-C-201", "E-W-201"}
rail_stops = {"9212-eb593664"}
isr, r, s = alerts._rail_entities(
    {"informed_entity": [{"route_id": "S-C-201"}]}, rail_routes, rail_stops)
ok("rail route matches", (isr, r), (True, ["S-C-201"]))
isr, r, s = alerts._rail_entities(
    {"informed_entity": [{"route_id": "313-203"}]}, rail_routes, rail_stops)
ok("bus route rejected", isr, False)
isr, r, s = alerts._rail_entities(
    {"informed_entity": [{"stop_id": "9212-eb593664"}]}, rail_routes, rail_stops)
ok("rail stop matches", (isr, s), (True, ["9212-eb593664"]))
isr, _, _ = alerts._rail_entities(
    {"informed_entity": [{"stop_id": "6503-8f7b1c19"}]}, rail_routes, rail_stops)
ok("bus stop rejected", isr, False)
isr, _, _ = alerts._rail_entities({"informed_entity": "junk"}, rail_routes, rail_stops)
ok("junk informed_entity safe", isr, False)
isr, _, _ = alerts._rail_entities({}, rail_routes, rail_stops)
ok("missing informed_entity safe", isr, False)
# Text must never decide relevance.
isr, _, _ = alerts._rail_entities(
    {"informed_entity": [{"route_id": "313-203"}],
     "header_text": {"translation": [{"text": "Newmarket Station", "language": "en"}]}},
    rail_routes, rail_stops)
ok("station name in text does NOT make it rail", isr, False)

print("%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
