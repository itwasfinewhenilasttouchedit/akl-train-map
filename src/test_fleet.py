#!/usr/bin/env python3
"""Gate for /api/fleet: the archive/live merge, and what it must NOT claim.

Run: ARCHIVE_DB=off python src/test_fleet.py

WHY THIS FILE EXISTS
--------------------
The Playwright gate (src/shot_fleet.py) can prove the screen renders and
filters, but it cannot prove the merge is right: the dev server has no AT key
so it has no live trains, and the CI container starts with an EMPTY archive.
Both halves of the endpoint are therefore untestable in the browser at the same
time, and each environment would silently exercise only one of them.

So the merge is tested here instead, against a REAL archive built from the
tracked capture in tests/fixtures/parity_caps.json and a REAL live payload run
through service.annotate()/publish() -- not hand-written dicts shaped to
whatever the code happens to do.

The claims under test, each of which this project could plausibly get wrong:
  1. Every unit label in the archive reaches the response, including the
     bare-number partner twins that own no obs rows -- folded onto the same
     set as their real label rather than appearing as 32 phantom units.
  2. A unit that is running NOW carries live status; one that is not carries
     `live: None`. Those are different claims and must not be merged.
  3. `source` distinguishes archive-only, live-only and both.
  4. The live fields are the ones service.py published -- not a second
     reading of the couple map. Asserted by diffing against `d["services"]`.
  5. No invented metadata: the response contains no build date, depot,
     withdrawal status or livery, because the data has none.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = Path(__file__).resolve().parent.parent
CAPS = ROOT / "tests" / "fixtures" / "parity_caps.json"

fails = []
checks = 0


def ck(ok, name, detail=""):
    global checks
    checks += 1
    if not ok:
        fails.append(name)
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" -- {detail}" if detail else ""))


def main() -> int:
    caps = json.loads(CAPS.read_text())
    ck(len(caps) >= 2, f"fixture has captures ({len(caps)})")

    tmp = tempfile.mkdtemp(prefix="fleetgate")
    db = Path(tmp) / "archive.db"
    # The archive module reads ARCHIVE_DB at init(); set it BEFORE importing
    # server, which calls neither at import time but whose `archive.get()` must
    # see this instance.
    os.environ["ARCHIVE_DB"] = str(db)
    os.environ.setdefault("FEEDBACK_DB", str(Path(tmp) / "feedback.db"))

    import archive
    import feedback
    import service
    import server

    a = archive.init(db)
    ck(a is not None, "fixture archive opened")
    written = sum(a.record(c) for c in caps)
    ck(written > 0, f"fixture rows recorded ({written})")

    # Read the `unit` table DIRECTLY rather than trusting a.fleet(): the whole
    # point of check 1 is that no label is lost between the table and the
    # response, and comparing a.fleet()'s output against itself would pass even
    # if fleet() silently dropped a whole class of rows (it previously would
    # have, on an inner join).
    import sqlite3
    raw = {r[0] for r in sqlite3.connect(str(db)).execute("SELECT unit FROM unit")}
    arch_labels = {r["label"] for r in a.fleet()}
    ck(arch_labels == raw,
       f"fleet() reports every row of the unit table ({len(raw)})",
       f"missing={sorted(raw - arch_labels)[:6]}")
    ck(len(raw) > 50, f"archive knows many labels ({len(raw)})")
    bare = {l for l in raw if not any(c.isalpha() for c in l)}
    ck(bare, f"fixture contains bare-number partner twins ({len(bare)})")

    # A live payload, annotated by the real pipeline so `services[]` is the
    # genuine published shape.
    #
    # DELIBERATELY NOT the whole capture: some units are dropped from the live
    # payload so the archive holds units that are NOT running. Without that,
    # every archived unit would also be live and the three assertions below
    # about archive-only entries would be vacuously true -- exactly the kind of
    # assertion that cannot fail. The drop is asserted to have had an effect.
    live = json.loads(json.dumps(caps[-1]))
    all_trains = live["trains"]
    # Drop whole COUPLED GROUPS, not arbitrary vehicles: removing one half of a
    # 6-car leaves a trailing half whose partner is gone, which service.py
    # would rightly treat as a different (and unrepresentative) situation.
    dropped_sets = set()
    keep = []
    for t in all_trains:
        sn = feedback.set_number(t.get("unit"))
        psn = feedback.set_number(t.get("partner_set"))
        if len(dropped_sets) < 10 and sn and not psn:
            dropped_sets.add(sn)
            continue
        keep.append(t)
    live["trains"] = keep
    ck(len(dropped_sets) >= 5,
       f"some archived units withheld from the live payload ({len(dropped_sets)})")
    service.annotate(live, {}, feedback.set_number)
    svs = live.get("services") or []
    ck(len(svs) > 0, f"live payload publishes services ({len(svs)})")
    server.STATE["data"] = live

    import asyncio
    resp = asyncio.get_event_loop().run_until_complete(server.api_fleet())
    d = json.loads(resp.body)

    units = d["units"]
    ck(d["archive"] is True, "response reports the archive enabled")
    ck(d["count"] == len(units), f"count matches the list ({d['count']})")

    # 1. every archive label reaches the response
    seen = set()
    for u in units:
        seen.update(u["labels"])
    missing = raw - seen
    ck(not missing, f"every archive label is reported ({len(arch_labels)})",
       str(sorted(missing)[:6]))

    # ... and the bare twins did NOT become their own phantom units. Each one
    # must share an entry with the alpha label carrying the same number.
    phantom = [u for u in units
               if u["labels"] and all(not any(c.isalpha() for c in l)
                                      for l in u["labels"])
               and any(feedback.set_number(l) == u["set_no"]
                       for l in raw if any(c.isalpha() for c in l))]
    ck(not phantom, "bare twins fold onto their real label, not phantom units",
       str([u["set_no"] for u in phantom][:6]))

    # 2 + 3. live vs not-live are distinct claims
    live_sets = {int(s) for sv in svs for s in (sv.get("set_nos") or [])}
    ck(live_sets, f"fixture has running sets ({len(live_sets)})")
    with_live = {u["set_no"] for u in units if u["live"]}
    ck(with_live == live_sets,
       f"exactly the running sets carry live status ({len(with_live)})",
       f"only-in-response={sorted(with_live - live_sets)[:5]} "
       f"only-in-services={sorted(live_sets - with_live)[:5]}")
    ck(all(u["source"] in ("archive", "live", "both") for u in units),
       "every entry declares its source")
    ck(all(u["source"] != "archive" for u in units if u["live"]),
       "a unit with live status is never labelled archive-only")
    ck(all(u["live"] is None for u in units if u["source"] == "archive"),
       "an archive-only unit carries no live status")
    # And that class is non-empty, so the three assertions above are live
    # checks rather than statements about an empty set.
    arch_only = [u for u in units if u["source"] == "archive"]
    ck(len(arch_only) >= 5,
       f"the archive-only class is non-empty ({len(arch_only)})",
       str(sorted(u["set_no"] or 0 for u in arch_only)[:8]))
    ck(dropped_sets <= {u["set_no"] for u in arch_only},
       "every withheld set reads as archive-only, not as missing",
       str(sorted(dropped_sets - {u['set_no'] for u in arch_only})))
    ck(d["archive_count"] == sum(1 for u in units if u["source"] != "live"),
       f"archive_count counts archive-sourced entries ({d['archive_count']})")
    ck(d["live_count"] == len(live_sets),
       f"live_count is the running set count ({d['live_count']})")

    # 4. the live fields ARE service.py's published ones
    by_set = {u["set_no"]: u for u in units if u["set_no"]}
    ck(len(by_set) == len([u for u in units if u["set_no"]]),
       f"every keyed entry has a distinct set number ({len(by_set)})")
    drift = []
    for sv in svs:
        for sn in (sv.get("set_nos") or []):
            u = by_set.get(int(sn))
            if u is None or not u["live"]:
                drift.append(f"{sn}: no live block")
                continue
            for k in ("cars", "lead_id"):
                if u["live"][k] != sv.get(k):
                    drift.append(f"{sn}.{k}: {u['live'][k]!r} != {sv.get(k)!r}")
            if u["live"]["in_service"] != bool(sv.get("in_service")):
                drift.append(f"{sn}.in_service")
            if u["live"]["ordered"] != bool(sv.get("ordered")):
                drift.append(f"{sn}.ordered")
    ck(not drift, "live status is exactly what service.py published",
       str(drift[:5]))

    # A 6-car service must report 6 cars on BOTH its halves -- the field is the
    # SERVICE's, so halving it per unit would be a quiet lie.
    six = [sv for sv in svs if sv.get("cars") == 6]
    if six:
        halves = [by_set.get(int(s)) for s in (six[0].get("set_nos") or [])]
        ck(all(h and h["live"]["cars"] == 6 for h in halves),
           f"both halves of a 6-car report 6 cars ({len(six)} such services)")

    # 5. nothing invented
    blob = json.dumps(d).lower()
    invented = [w for w in ("built", "depot", "withdrawn", "livery",
                            "manufactur", "refurb", "scrapped")
                if w in blob]
    ck(not invented, "no fleet metadata the data does not have", str(invented))

    # Archive off must still answer, honestly: live-only, and saying so.
    archive._ARCHIVE = None
    resp2 = asyncio.get_event_loop().run_until_complete(server.api_fleet())
    d2 = json.loads(resp2.body)
    ck(d2["archive"] is False, "archive-off response says archive: false")
    ck(all(u["source"] == "live" for u in d2["units"]),
       "archive-off reports every unit as live-sourced")
    ck(d2["count"] == len(live_sets),
       f"archive-off count is the live set count ({d2['count']})")

    # And with no poll data at all (cold start) it must not raise.
    server.STATE["data"] = None
    d3 = json.loads(asyncio.get_event_loop()
                    .run_until_complete(server.api_fleet()).body)
    ck(d3["units"] == [] and d3["warming"] is True,
       "cold start reports warming with an empty list", json.dumps(d3)[:120])

    print(f"\n==== {checks - len(fails)}/{checks} passed, {len(fails)} failed ====")
    if fails:
        print("FAILURES:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
