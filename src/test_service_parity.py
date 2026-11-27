"""Parity gate: service.annotate() must equal the LEGACY _annotate_consists().

WHY THIS EXISTS
---------------
Phase 2 deletes a 125-line function that six live units depend on. A gate that
asserted the NEW behaviour would only prove the new code agrees with my
expectations -- which is how the trailing-half story went wrong in the first
place. So this gate does not encode any expectation at all. It:

  1. extracts the legacy implementation from git (commit `842a8c4^`, i.e. the
     code that was running in production before phase 2) and `exec`s it;
  2. replays captured REAL production payloads through both implementations;
  3. diffs every key of every train record, plus every top-level counter.

Any difference is a failure. The gate therefore cannot be satisfied by making
the new code "better" -- only by making it identical. Behaviour changes get
their own phase and their own argument.

Fixtures: output/parity_caps.json -- payloads captured from hoki.co.nz,
including the awkward cases (a 6-car with the trip on the REAR set, and a
coupled pair with NO trip on either half, which must stay two icons).

Run: .venv/bin/python src/test_service_parity.py
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import feedback  # noqa: E402
import service as S  # noqa: E402

LEGACY_REV = "842a8c4^:src/server.py"
PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'PASS' if cond else 'FAIL'}: {name}"
          + (f" -- {detail}" if detail and not cond else ""))


# --------------------------------------------------------------------------
# 1. Extract the legacy implementation from git history.
# --------------------------------------------------------------------------
def load_legacy():
    """Pull _annotate_consists + _recount_services out of the pre-phase-2
    server.py and exec them in a namespace with only what they need.

    Deliberately read from git, not from a copy pasted into this file: a
    hand-copied reference drifts, and a drifted reference proves nothing.
    """
    src = subprocess.run(["git", "show", LEGACY_REV], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    lines = src.splitlines()

    def grab(name):
        start = next(i for i, l in enumerate(lines)
                     if l.startswith(f"def {name}("))
        end = start + 1
        while end < len(lines) and not (lines[end].startswith("def ")
                                        or lines[end].startswith("app = ")
                                        or lines[end].startswith("@app")):
            end += 1
        return "\n".join(lines[start:end])

    ns: dict = {
        "STATE": {},          # legacy reads its payload from STATE["data"]
        "feedback": feedback,
        "consist_at": type("X", (), {"summarise": staticmethod(lambda c: "")}),
        "LOG": type("L", (), {"info": staticmethod(lambda *a, **k: None)})(),
        "_consist_map": lambda: LEGACY_COUPLES,
    }
    code = grab("_annotate_consists") + "\n\n" + grab("_recount_services")
    exec(compile(code, "<legacy>", "exec"), ns)
    return ns


LEGACY_COUPLES: dict = {}
legacy = load_legacy()
ok("legacy implementation extracted from git",
   callable(legacy.get("_annotate_consists"))
   and callable(legacy.get("_recount_services")))
ok("legacy source is the pre-phase-2 revision", LEGACY_REV.startswith("842a8c4^"))


# --------------------------------------------------------------------------
# 2. Fixtures.
# --------------------------------------------------------------------------
# Tracked, NOT output/ -- that directory is gitignored, so fixtures left there
# would make this gate silently untestable on a fresh clone. The payloads are
# public AT vehicle data (positions, trips, unit numbers); they carry no rider
# notes and no credentials, so they are safe to commit.
FIX = ROOT / "tests" / "fixtures" / "parity_caps.json"
if not FIX.exists():
    FIX = ROOT / "output" / "parity_caps.json"
caps = json.loads(FIX.read_text()) if FIX.exists() else []
ok("captured production payloads present", len(caps) >= 3, f"{len(caps)} caps")


# Fixture helpers live in test_fixtures.py so phase 3's gate can reuse them
# without importing (and thereby executing) this script. See that module.
from test_fixtures import couples_from, strip  # noqa: E402,E501  (re-exported)

ALL_KEYS = set()
for c in caps:
    for t in c.get("trains") or []:
        ALL_KEYS |= set(t)

print("\n-- per-payload parity --")
diff_count = 0
awkward = {"rear_trip": 0, "stabled_pair": 0, "no_couples": 0}

for i, cap in enumerate(caps):
    base = strip(cap)
    LEGACY_COUPLES = couples_from(cap)
    cpl = couples_from(cap)

    a = copy.deepcopy(base)
    g = legacy["_annotate_consists"].__globals__
    g["_consist_map"] = lambda: cpl
    g["STATE"]["data"] = a           # legacy takes no args; it reads STATE
    legacy["_annotate_consists"]()

    b = copy.deepcopy(base)
    S.annotate(b, cpl, feedback.set_number)

    # top-level counters
    for k in ("in_service", "vehicles_in_service", "out_of_service",
              "coupled_halves", "trailing_halves"):
        if a.get(k) != b.get(k):
            diff_count += 1
            print(f"    cap{i} TOPLEVEL {k}: legacy={a.get(k)} new={b.get(k)}")

    # per-train, every key either implementation touched
    la = {feedback.set_number(t.get("unit")): t for t in a["trains"]}
    lb = {feedback.set_number(t.get("unit")): t for t in b["trains"]}
    if set(la) != set(lb):
        diff_count += 1
        print(f"    cap{i} train sets differ")
    for sn in la:
        ta, tb = la[sn], lb.get(sn) or {}
        for k in set(ta) | set(tb):
            if ta.get(k) != tb.get(k):
                diff_count += 1
                print(f"    cap{i} set {sn} {k}: legacy={ta.get(k)!r} "
                      f"new={tb.get(k)!r}")

    # record which awkward cases this fixture actually exercised
    for t in a["trains"]:
        if t.get("trailing_source") == "geometry" and t.get("service_set") \
                and not t.get("trailing_half"):
            awkward["rear_trip"] += 1
    seen = set()
    for t in a["trains"]:
        sn = feedback.set_number(t.get("unit"))
        p = t.get("partner_set")
        if not p or sn in seen:
            continue
        seen |= {sn, int(p)}
        other = la.get(int(p))
        if other is not None and t.get("in_service") is False \
                and other.get("in_service") is False:
            awkward["stabled_pair"] += 1

ok(f"all {len(caps)} production payloads annotate identically",
   diff_count == 0, f"{diff_count} differing values")

print("\n-- the fixtures actually cover the hard cases --")
ok("a 6-car with the trip on the REAR set is present",
   awkward["rear_trip"] > 0, str(awkward["rear_trip"]))
ok("a coupled pair with NO trip on either half is present",
   awkward["stabled_pair"] > 0, str(awkward["stabled_pair"]))

print("\n-- degenerate inputs agree too --")
for name, payload in [
    ("empty payload", {"trains": []}),
    ("no trains key", {}),
    ("one 3-car", {"trains": [{"unit": "AMP 701", "in_service": True}]}),
    # Real set numbers: feedback.set_number() needs 3-4 digits, so "AMP 1"
    # parses to None and every annotation is skipped -- a fixture that tests
    # nothing while passing.
    ("coupled, no trips", {"trains": [{"unit": "AMP 701", "in_service": False},
                                      {"unit": "AMP 702", "in_service": False}]}),
]:
    cpl = ({"701": {"partner": "702", "position": "front", "consist": 6},
            "702": {"partner": "701", "position": "rear", "consist": 6}}
           if "coupled" in name else {})
    a, b = copy.deepcopy(payload), copy.deepcopy(payload)
    g = legacy["_annotate_consists"].__globals__
    g["_consist_map"] = lambda: cpl
    g["STATE"]["data"] = a
    legacy["_annotate_consists"]()
    S.annotate(b, cpl, feedback.set_number)
    # Phase 3 ADDS `services[]`, which legacy cannot produce. That is the one
    # intended difference, so it is removed by name rather than by relaxing the
    # comparison: every other key must still match exactly, and a typo in the
    # key name would resurrect the full diff rather than hide it.
    svcs = b.pop("services", None)
    ok(f"{name}: identical apart from services[]", a == b,
       f"legacy={a} new={b}")
    ok(f"{name}: services[] was in fact present", svcs is not None)

print("\n-- the stabled pair stays TWO icons (explicitly NOT collapsed) --")
cpl = {"701": {"partner": "702", "position": "front", "consist": 6},
       "702": {"partner": "701", "position": "rear", "consist": 6}}
d = {"trains": [{"unit": "AMP 701", "in_service": False, "moving": True},
                {"unit": "AMP 702", "in_service": False, "moving": True}]}
S.annotate(d, cpl, feedback.set_number)
ok("neither half is marked trailing",
   all(not t.get("trailing_half") for t in d["trains"]))
ok("both still count as out of service", d["out_of_service"] == 2,
   str(d["out_of_service"]))
ok("zero services", d["in_service"] == 0, str(d["in_service"]))
ok("but the consist annotation still lands (it is a real 6-car)",
   all(t.get("consist") == 6 for t in d["trains"]))

print("\n-- the rescue still works when ONE half has the trip --")
d = {"trains": [{"unit": "AMP 701", "in_service": False},
                {"unit": "AMP 702", "in_service": True, "trip_id": "T",
                 "dest": "Manukau"}]}
c = S.annotate(d, cpl, feedback.set_number)
ok("one half rescued", c["rescued"] == 1, str(c))
ok("one service counted", d["in_service"] == 1, str(d["in_service"]))
ok("exactly one half is drawn",
   sum(1 for t in d["trains"]
       if t.get("in_service") is not False and not t.get("trailing_half")) == 1)
front = next(t for t in d["trains"] if not t.get("trailing_half"))
ok("geometry chose the FRONT half as the icon", front["unit"] == "AMP 701")
ok("the front half was promoted to in_service", front["in_service"] is True)
ok("and inherited the destination", front.get("dest") == "Manukau")
ok("re-ordered by geometry, not trip ownership", c["regeom"] == 1)

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
for n, det in FAIL:
    print(f"  FAILED: {n}" + (f" -- {det}" if det else ""))
sys.exit(1 if FAIL else 0)