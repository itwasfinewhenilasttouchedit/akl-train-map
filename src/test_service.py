"""Gate for the Service model (src/service.py). No browser, no server.

WHAT THIS GUARDS
----------------
The old model let a train contradict itself: a record was observed carrying
`trailing_half: True` and `consist_position: "front"` at once, and counting
trains needed a compound predicate duplicated in Python and JS. The point of
`Service` is that those states cannot be written down. A gate that only
checked happy paths would not prove that, so most of these assertions are
NEGATIVE -- they require the model to REFUSE bad input.

Run: .venv/bin/python src/test_service.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import service as S  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'PASS' if cond else 'FAIL'}: {name}" + (f" -- {detail}" if detail and not cond else ""))


def refuses(name, fn, *, because=""):
    """Assert the model REJECTS something rather than silently repairing it."""
    try:
        fn()
    except S.ModelError as e:
        ok(name, True, str(e))
        return
    except Exception as e:  # wrong exception type is still a failure
        ok(name, False, f"raised {type(e).__name__}, not ModelError: {e}")
        return
    ok(name, False, f"accepted it {because}")


print("\n-- a 3-car service --")
s3 = S.build([701], in_service=True, units={701: "AMP 701"})
ok("one member", len(s3.members) == 1)
ok("3 cars", s3.cars == 3, str(s3.cars))
ok("not six", s3.six is False)
ok("its only member leads", s3.lead.set_no == 701)
ok("a lone unit is ordered by definition", s3.ordered is False or s3.lead.pos in (None, 1))

print("\n-- a 6-car, order known --")
s6 = S.build([851, 593], in_service=True, positions={593: 1, 851: 2},
             units={593: "AMP 593", 851: "AMP 851"})
ok("two members", len(s6.members) == 2)
ok("6 cars", s6.cars == 6, str(s6.cars))
ok("six", s6.six is True)
ok("ordered", s6.ordered is True)
ok("the FRONT half leads, not the trip-bearing half",
   s6.lead.set_no == 593, f"lead={s6.lead.set_no}")
ok("halves_in_order is front-first",
   [m.set_no for m in s6.halves_in_order()] == [593, 851])

print("\n-- the carriage base is DERIVED (kills the threaded `base` arg) --")
front, rear = s6.halves_in_order()
ok("front half starts at carriage 1", front.carriage_base == 1, str(front.carriage_base))
ok("rear half starts at carriage 4", rear.carriage_base == 4, str(rear.carriage_base))
ok("front covers 1-3", front.carriage_range() == (1, 3))
ok("rear covers 4-6", rear.carriage_range() == (4, 6))
ok("rear-half car 2 is absolute carriage 5 (the real bug)",
   s6.absolute_carriage(851, 2) == 5, str(s6.absolute_carriage(851, 2)))
ok("front-half car 2 is absolute carriage 2",
   s6.absolute_carriage(593, 2) == 2)
ok("rear-half car 1 is absolute carriage 4",
   s6.absolute_carriage(851, 1) == 4)
ok("rear-half car 3 is absolute carriage 6",
   s6.absolute_carriage(851, 3) == 6)
refuses("a car position outside 1-3 is refused",
        lambda: s6.absolute_carriage(851, 4), because="(would point past the train)")

print("\n-- NO ORDER, NO NUMBER --")
unordered = S.build([851, 593], in_service=True,
                    units={593: "AMP 593", 851: "AMP 851"})
ok("unordered when geometry gave nothing", unordered.ordered is False)
ok("every pos is None", all(m.pos is None for m in unordered.members))
ok("carriage_base is None, not a guess",
   all(m.carriage_base is None for m in unordered.members))
ok("absolute_carriage refuses to invent a number",
   unordered.absolute_carriage(851, 2) is None)
ok("still iterable so callers can say 'the other half'",
   len(unordered.halves_in_order()) == 2)
ok("exactly one half still leads (the icon must go somewhere)",
   sum(1 for m in unordered.members if m.is_lead) == 1)

print("\n-- a PARTIAL order is refused outright --")
refuses("one half positioned, one not",
        lambda: S.Service(members=(S.Member(593, pos=1, is_lead=True),
                                   S.Member(851, pos=None))),
        because="(a caller would number carriages from a half-known front)")

print("\n-- the old model's contradictions are now unrepresentable --")
refuses("the REAR half cannot be the drawn icon",
        lambda: S.Service(members=(S.Member(593, pos=1),
                                   S.Member(851, pos=2, is_lead=True))),
        because="(this is trailing_half:True + consist_position:'front')")
refuses("two leads",
        lambda: S.Service(members=(S.Member(593, pos=1, is_lead=True),
                                   S.Member(851, pos=2, is_lead=True))),
        because="(two icons for one train)")
refuses("no lead",
        lambda: S.Service(members=(S.Member(593, pos=1),
                                   S.Member(851, pos=2))),
        because="(a train with no icon)")
refuses("three units",
        lambda: S.Service(members=(S.Member(1, pos=1, is_lead=True),
                                   S.Member(2, pos=2), S.Member(3))),
        because="(AM stock does not run as a 9-car)")
refuses("zero units",
        lambda: S.Service(members=()),
        because="(a service with no train)")
# `build()` dedupes, as consist_at.py:202 already does ("a trip should not
# list a set twice"), so a repeated set collapses to a 3-car rather than
# raising. It must NOT become a phantom 6-car coupled to itself.
_dup = S.build([593, 593], positions={593: 1})
ok("a repeated set dedupes to a 3-car, not a phantom pair",
   _dup.cars == 3 and len(_dup.members) == 1, str(_dup.cars))
# Direct construction is still guarded, since that path has no dedupe.
refuses("a set coupled to itself (direct construction)",
        lambda: S.Service(members=(S.Member(593, pos=1, is_lead=True),
                                   S.Member(593, pos=2))),
        because="(dedupe must not be the only defence)")
refuses("two halves sharing a position",
        lambda: S.Service(members=(S.Member(593, pos=1, is_lead=True),
                                   S.Member(851, pos=1))),
        because="(both at the front)")
refuses("a position beyond the consist length",
        lambda: S.Service(members=(S.Member(593, pos=2, is_lead=True),)),
        because="(pos 2 of a 1-unit service)")
refuses("an empty set list",
        lambda: S.build([None, None]),
        because="")

print("\n-- in_service is a property of the TRAIN, not of a vehicle --")
# AT attaches the trip to ONE half. Asking each vehicle in isolation is the
# bug that made a working 6-car read as stabled stock.
trains = [
    {"set_no": 593, "unit": "AMP 593", "id": "59593", "in_service": True,
     "trip_id": "T1", "dest": "Manukau", "line": "Eastern / Western"},
    {"set_no": 851, "unit": "AMP 851", "id": "59851", "in_service": False},
]
couples = {"593": {"partner": "851", "position": "front", "source": "at",
                   "order_source": "inferred"},
           "851": {"partner": "593", "position": "rear", "source": "at",
                   "order_source": "inferred"}}
svcs = S.build_services(trains, couples)
ok("two vehicles collapse into ONE service", len(svcs) == 1, str(len(svcs)))
svc = svcs[0]
ok("the service is in service", svc.in_service is True)
ok("even though one half said False",
   trains[1]["in_service"] is False and svc.in_service is True)
ok("it is a 6-car", svc.cars == 6)
ok("the trip is owned ONCE by the service", svc.get("trip_id") == "T1")
ok("the destination came from the trip-bearing half",
   svc.get("dest") == "Manukau")
ok("the FRONT half leads, though the trip sits on it here",
   svc.lead.set_no == 593)
ok("provenance preserved: consist_src", svc.consist_src == "at")
ok("provenance preserved: order_src", svc.order_src == "inferred")

print("\n-- the trip on the REAR half (AT's actual behaviour, 2026-11-23) --")
# Ground truth: AT put the trip on the REAR set on both observed 6-cars.
# The old rule marked the trip-less half as trailing and so drew the icon
# ~72 m behind the real leading cab.
trains_r = [
    {"set_no": 1033, "unit": "AMP 1033", "in_service": True, "trip_id": "T2",
     "dest": "Swanson"},
    {"set_no": 917, "unit": "AMP 917", "in_service": False},
]
couples_r = {"1033": {"partner": "917", "position": "rear", "source": "at"},
             "917": {"partner": "1033", "position": "front", "source": "at"}}
sr = S.build_services(trains_r, couples_r)[0]
ok("one service", len(S.build_services(trains_r, couples_r)) == 1)
ok("the icon goes to the geometric FRONT (917), not the trip holder (1033)",
   sr.lead.set_no == 917, f"lead={sr.lead.set_no}")
ok("but the service still carries the trip", sr.get("trip_id") == "T2")
ok("and the destination", sr.get("dest") == "Swanson")
ok("the trip-holding half is the rear", sr.member(1033).pos == 2)
ok("a report on 1033 car 1 is absolute carriage 4",
   sr.absolute_carriage(1033, 1) == 4)

print("\n-- COUNTING: services, not vehicles --")
# Production once showed 71 'in service' against 39 markers, overstating by 32.
many = []
cpl = {}
for i in range(5):  # five 6-cars = 10 vehicles
    a, b = 100 + i * 2, 101 + i * 2
    many += [{"set_no": a, "unit": f"AMP {a}", "in_service": True, "trip_id": f"T{i}"},
             {"set_no": b, "unit": f"AMP {b}", "in_service": False}]
    cpl[str(a)] = {"partner": str(b), "position": "front"}
    cpl[str(b)] = {"partner": str(a), "position": "rear"}
for i in range(3):  # three 3-cars
    sn = 200 + i
    many.append({"set_no": sn, "unit": f"AMP {sn}", "in_service": True, "trip_id": f"U{i}"})
for i in range(2):  # two genuinely stabled units
    sn = 300 + i
    many.append({"set_no": sn, "unit": f"AMP {sn}", "in_service": False})

all_svcs = S.build_services(many, cpl)
running = [x for x in all_svcs if x.in_service]
ok("15 vehicles -> 10 services", len(all_svcs) == 10, str(len(all_svcs)))
ok("8 services running (5 six-cars + 3 three-cars)",
   len(running) == 8, str(len(running)))
ok("2 services stabled", len(all_svcs) - len(running) == 2)
ok("one icon per service -- no double counting",
   sum(1 for x in all_svcs for m in x.members if m.is_lead) == len(all_svcs))
ok("carriage total is 5*6 + 3*3 + 2*3 = 45",
   sum(x.cars for x in all_svcs) == 45, str(sum(x.cars for x in all_svcs)))
ok("every member belongs to exactly one service",
   sorted(m.set_no for x in all_svcs for m in x.members)
   == sorted(t["set_no"] for t in many))

print("\n-- degraded inputs must not crash or invent --")
ok("no couples at all: every vehicle is its own 3-car",
   len(S.build_services(many, {})) == 15)
ok("empty payload", S.build_services([], {}) == [])
ok("None payload", S.build_services(None, None) == [])
ok("a vehicle with no set_no is skipped, not guessed",
   S.build_services([{"unit": "???", "in_service": True}], {}) == [])
ok("a partner named but ABSENT from the feed stays a 3-car",
   len(S.build_services([{"set_no": 593, "in_service": True}],
                        {"593": {"partner": "851", "position": "front"}})) == 1)
onesided = S.build_services([{"set_no": 593, "in_service": True}],
                            {"593": {"partner": "851", "position": "front"}})[0]
ok("...and it is 3 cars, not 6 -- never claim a car count for a unit we "
   "cannot see", onesided.cars == 3, str(onesided.cars))
ok("a self-partner is ignored",
   len(S.build_services([{"set_no": 593, "in_service": True}],
                        {"593": {"partner": "593"}})) == 1)

print("\n-- frozen: a service cannot be mutated into a bad state --")
try:
    s6.members[0].__setattr__("is_lead", False)
    ok("members are immutable", False, "mutation succeeded")
except Exception:
    ok("members are immutable", True)
try:
    object.__setattr__  # noqa
    s6.__setattr__("in_service", False)
    ok("service is immutable", False, "mutation succeeded")
except Exception:
    ok("service is immutable", True)

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    for n, d in FAIL:
        print(f"  FAILED: {n}" + (f" -- {d}" if d else ""))
sys.exit(1 if FAIL else 0)
