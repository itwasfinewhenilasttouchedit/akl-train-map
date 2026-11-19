"""Tests for consist_at: AT's published units merged with geometric order.

Run: .venv/bin/python src/test_consist_at.py
Network-free — units_for_trips is not exercised here, only the pure logic.
"""
import sys

sys.path.insert(0, "src")
import consist_at  # noqa: E402

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s\n       got  %r\n       want %r" % (label, got, want))


print("set_number")
check("AMP label", consist_at.set_number("AMP        484"), "484")
check("4-digit", consist_at.set_number("AMP        1046"), "1046")
check("no truncation of 5 digits",
      consist_at.set_number("AMP 12345"), None)
check("empty", consist_at.set_number(""), None)

print("\ncouples_from_units")
c = consist_at.couples_from_units(
    {"T1": ["AMP        281", "AMP        484"]})
check("6-car both sets present", sorted(c), ["281", "484"])
check("281 partner", c["281"]["partner"], "484")
check("484 partner", c["484"]["partner"], "281")
check("carriage count", c["281"]["consist"], 6)
check("source is at", c["281"]["source"], "at")
check("not flagged inferred", c["281"]["consist_inferred"], False)
check("order not claimed", c["281"]["position"], None)

c3 = consist_at.couples_from_units({"T2": ["AMP        497"]})
check("3-car recorded", c3["497"]["consist"], 3)
check("3-car has no partner", c3["497"]["partner"], None)

check("empty list ignored", consist_at.couples_from_units({"T3": []}), {})
check("unparseable label ignored",
      consist_at.couples_from_units({"T4": ["BUS"]}), {})

dup = consist_at.couples_from_units({"T5": ["AMP 281", "AMP 281"]})
check("duplicate set collapses to 3-car", dup["281"]["consist"], 3)

print("\nmerge: AT wins membership, geometry supplies order")
at = consist_at.couples_from_units({"T1": ["AMP 281", "AMP 484"]})
inf = {
    "281": {"partner": "484", "consist": 6, "position": "rear",
            "cars": [4, 5, 6], "gap_m": 74, "consist_inferred": True},
    "484": {"partner": "281", "consist": 6, "position": "front",
            "cars": [1, 2, 3], "gap_m": 74, "consist_inferred": True},
}
m = consist_at.merge(at, inf)
check("order taken from geometry", m["484"]["position"], "front")
check("cars taken from geometry", m["484"]["cars"], [1, 2, 3])
check("gap carried through", m["484"]["gap_m"], 74)
check("source stays at", m["484"]["source"], "at")
check("order_source labelled", m["484"]["order_source"], "inferred")
check("not flagged inferred", m["484"]["consist_inferred"], False)

print("\nmerge: AT says 6 where geometry was blind (the 13 misses)")
m = consist_at.merge(at, {})
check("AT entry survives alone", m["281"]["consist"], 6)
check("partner from AT", m["281"]["partner"], "484")
check("no order claimed", m["281"]["position"], None)
check("still published, not inferred", m["281"]["consist_inferred"], False)

print("\nmerge: geometry disagrees with AT -> AT wins, order dropped")
bad = {
    "281": {"partner": "999", "consist": 6, "position": "rear",
            "cars": [4, 5, 6], "consist_inferred": True},
}
m = consist_at.merge(at, bad)
check("partner is AT's", m["281"]["partner"], "484")
check("bogus order discarded", m["281"].get("position"), None)
check("bogus cars discarded", m["281"].get("cars"), None)

print("\nmerge: key revoked -> inference alone still answers")
m = consist_at.merge({}, inf)
check("inferred entry kept", m["484"]["consist"], 6)
check("flagged inferred", m["484"]["consist_inferred"], True)
check("source inferred", m["484"]["source"], "inferred")

print("\nmerge: set only geometry knows keeps its entry")
m = consist_at.merge(at, dict(inf, **{
    "700": {"partner": "701", "consist": 6, "position": "front",
            "cars": [1, 2, 3], "consist_inferred": True}}))
check("extra inferred set retained", m["700"]["source"], "inferred")
check("AT sets still present", m["281"]["source"], "at")

print("\nsummarise")
s = consist_at.summarise(m)
check("mentions six-car count", "six-car" in s, True)
print("  text: %s" % s)

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
