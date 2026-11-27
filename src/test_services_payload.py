#!/usr/bin/env python3
"""Phase 3 parity gate: `services[]` must draw EXACTLY the old icon set.

The phase 2 lesson, written into MEMORY.md, was that a parity gate must not
encode the author's expectations -- it must extract the OLD behaviour and diff
against it. So the reference here is not a hand-written list of what I think
should be drawn; it is the literal pre-phase-3 client predicate:

    if (t.trailing_half) continue;        // web/index.html:2889, at 20abf65

transliterated once, below, and applied to the same annotated payloads the new
code sees. If `services[]` and that predicate disagree about a single vehicle,
this fails and names it.

Corpus: tests/fixtures/parity_caps.json -- 6 captured production payloads
(public AT vehicle data only), the same corpus test_service_parity.py uses,
including the two awkward cases: the trip on the REAR set, and a coupled pair
with no trip on either half.

Run: ARCHIVE_DB=off .venv/bin/python src/test_services_payload.py
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ARCHIVE_DB", "off")

import feedback  # noqa: E402
import service  # noqa: E402
# Reuse the phase-2 gate's fixture helpers rather than reimplementing them.
# The captured payloads carry NO `couples` key -- the coupling has to be
# rebuilt from the annotations already in the payload, and the annotations then
# stripped so this runs from the same raw input the server sees. A first draft
# of this gate skipped both steps, so every payload hit annotate()'s
# `if not couples` early return and the gate asserted nothing about the main
# path while reporting 109 green. `strip()` also documents why `service_set`,
# not `trailing_half`, is the honest marker to strip by.
from test_fixtures import couples_from, strip  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAPS = ROOT / "tests" / "fixtures" / "parity_caps.json"

PASS = FAIL = 0


def ck(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {label}")
    else:
        FAIL += 1
        print(f"FAIL {label} -- {detail}")


def old_client_drawn(trains):
    """The pre-phase-3 client's draw set, transliterated from JavaScript.

    `for (const t of d.trains) { if (t.trailing_half) continue; ... }`

    Note what is NOT here: `in_service`. The client drew stabled stock too
    (grey, hidden behind the OOS legend toggle). An earlier draft of phase 3
    dropped those and would have silently deleted every grey marker from the
    map -- this function is what caught it.
    """
    return [t.get("id") for t in trains if not t.get("trailing_half")]


def main():
    caps = json.loads(CAPS.read_text())
    payloads = caps if isinstance(caps, list) else caps.get("payloads", [])
    ck(len(payloads) >= 6, f"corpus has >=6 payloads ({len(payloads)})")

    CARS3 = service.CARS_PER_UNIT
    total_svc = total_six = total_stabled = 0
    for i, cap in enumerate(payloads):
        raw = cap.get("data") or cap
        couples = couples_from(raw)
        d = strip(raw)
        trains = d.get("trains") or []
        if not trains:
            continue
        ck(bool(couples), f"[{i}] fixture actually has couplings to test",
           f"{len(couples)} couples")

        service.annotate(d, couples, feedback.set_number)
        svcs = d.get("services")
        ck(isinstance(svcs, list), f"[{i}] payload carries services[]", str(type(svcs)))
        if not isinstance(svcs, list):
            continue

        # THE parity assertion.
        want = sorted(x for x in old_client_drawn(trains) if x is not None)
        got = sorted(s["lead_id"] for s in svcs if s["lead_id"] is not None)
        ck(want == got, f"[{i}] services[] draws exactly the old icon set",
           f"only_old={sorted(set(want)-set(got))} only_new={sorted(set(got)-set(want))}")

        # The HUD number must equal the number of IN-SERVICE services, which is
        # the whole point of killing the duplicated predicate.
        ck(d["in_service"] == sum(1 for s in svcs if s["in_service"]),
           f"[{i}] HUD in_service == count of in-service services",
           f"hud={d['in_service']} svcs={sum(1 for s in svcs if s['in_service'])}")

        # Invariants from the design doc, section "unrepresentable".
        ids = [s["lead_id"] for s in svcs]
        ck(len(ids) == len(set(ids)), f"[{i}] no vehicle leads two services",
           str([x for x in ids if ids.count(x) > 1][:4]))
        all_sets = [sn for s in svcs for sn in s["set_nos"]]
        ck(len(all_sets) == len(set(all_sets)),
           f"[{i}] no set belongs to two services",
           str([x for x in all_sets if all_sets.count(x) > 1][:4]))
        ck(all(1 <= len(s["set_nos"]) <= 2 or s["set_nos"] == [] for s in svcs),
           f"[{i}] every service has 1 or 2 members")
        ck(all(s["cars"] == 3 * max(1, len(s["set_nos"])) for s in svcs),
           f"[{i}] cars == 3 * members",
           str([(s['set_nos'], s['cars']) for s in svcs if
                s['cars'] != 3 * max(1, len(s['set_nos']))][:3]))

        # A trailing half must never appear as a lead, and its partner must.
        trailing = {t.get("id") for t in trains if t.get("trailing_half")}
        ck(not (trailing & set(ids)),
           f"[{i}] no trailing half leads a service", str(sorted(trailing & set(ids))))

        # THE LEAD MUST BE THE GEOMETRY FRONT, not whichever half happened to
        # come first in the feed. Ground truth 2026-11-23: AT puts the trip on
        # the REAR set at least some of the time, so declaration order and trip
        # ownership are both wrong answers. Measured on this corpus: 52 of 52
        # ordered 6-cars lead from the front.
        by_id = {t.get("id"): t for t in trains}
        for s in svcs:
            if s["cars"] == 6 and s["ordered"]:
                lead = by_id.get(s["lead_id"]) or {}
                ck(lead.get("consist_position") == "front",
                   f"[{i}] ordered 6-car leads from the geometry front",
                   f"set_nos={s['set_nos']} lead_pos={lead.get('consist_position')}")
                ck(s["set_nos"][0] == int(feedback.set_number(lead.get("unit"))),
                   f"[{i}] set_nos is front-first",
                   f"{s['set_nos']} lead={lead.get('unit')}")

        # The preserved behaviour from phase 2: a coupled pair with NO trip on
        # either half stays TWO grey icons rather than collapsing to one 6-car.
        # Published as two 3-car services carrying `coupled_to`, because
        # services[] must describe what is actually on the map. Measured: 36
        # such entries across the corpus.
        stabled = [s for s in svcs if s.get("coupled_to")]
        for s in stabled:
            ck(s["cars"] == CARS3,
               f"[{i}] a stabled coupled half stays a 3-car service", str(s))
            ck(s["in_service"] is False,
               f"[{i}] a stabled coupled half is not in service", str(s))
            ck(len(s["set_nos"]) == 1,
               f"[{i}] a stabled coupled half names only itself", str(s))
            # and its partner must ALSO have been published, or one grey icon
            # would have silently disappeared from the map.
            ck(any(o.get("coupled_to") and s["set_nos"][0] in o["coupled_to"]
                   for o in stabled),
               f"[{i}] its partner is published too", str(s))
        total_stabled += len(stabled)

        six = [s for s in svcs if s["cars"] == 6]
        total_svc += len(svcs)
        total_six += len(six)
        # Ordering honesty: a 6-car claiming `ordered` must have both positions.
        for s in six:
            if s["ordered"]:
                ck(len(s["set_nos"]) == 2,
                   f"[{i}] ordered 6-car names both halves", str(s))

    ck(total_svc > 0, f"services were actually built ({total_svc})")
    ck(total_six > 0, f"corpus exercises 6-car services ({total_six})")
    ck(total_stabled > 0,
       f"corpus exercises the stabled coupled pair ({total_stabled})")

    # Empty feed: must publish [], not leave the previous poll's list.
    d = {"trains": [], "services": [{"lead_id": "stale"}]}
    service.annotate(d, {}, feedback.set_number)
    ck(d.get("services") == [], "empty feed publishes an empty services[]",
       str(d.get("services")))

    # No couples this poll: every vehicle is its own service.
    d = {"trains": [{"id": "a", "unit": "AMP 101", "in_service": True},
                    {"id": "b", "unit": "AMP 102", "in_service": False}]}
    service.annotate(d, {}, feedback.set_number)
    ck(len(d["services"]) == 2, "no coupling info -> one service per vehicle",
       str(d["services"]))
    ck(d["in_service"] == 1, "stabled stock is drawn but not counted",
       f"in_service={d['in_service']} services={len(d['services'])}")

    # is_drawn and is_in_service must be different questions.
    ck(service.is_drawn({"in_service": False}) is True,
       "stabled stock IS drawn (grey marker)")
    ck(service.is_in_service({"in_service": False}) is False,
       "stabled stock is NOT counted as a service")
    ck(service.is_drawn({"trailing_half": True}) is False,
       "a trailing half is NOT drawn")

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
