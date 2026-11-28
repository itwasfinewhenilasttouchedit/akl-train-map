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

    # ---- the four-column data: line + speed in the LIVE half only -------
    # The Fleet list shows unit / line / speed / status. Line and speed are
    # true for THIS INSTANT, so they must come from the live annotation and
    # must be absent (so the client dashes them) when a unit is not running.
    # Both halves are asserted, and each class is proved non-empty first, or
    # these assertions would be statements about an empty set.
    running = [u for u in units if u["live"]]
    not_running = [u for u in units if not u["live"]]
    ck(len(running) >= 5, f"some units are running ({len(running)})")
    ck(len(not_running) >= 5, f"some units are NOT running ({len(not_running)})")
    ck(all("line" in u["live"] for u in running),
       "every running unit carries a `line` key")
    ck(all("speed_kmh" in u["live"] for u in running),
       "every running unit carries a `speed_kmh` key")
    # The values must be the LEAD vehicle's, diffed against the payload rather
    # than against my expectation of it.
    lead_by_id = {t.get("id"): t for t in live["trains"]}
    lmis = []
    for sv in svs:
        lead = lead_by_id.get(sv.get("lead_id")) or {}
        for sn in (sv.get("set_nos") or []):
            u = by_set.get(int(sn))
            if not u or not u["live"]:
                continue
            if u["live"]["line"] != (lead.get("line") or None):
                lmis.append(f"{sn}.line {u['live']['line']!r} != {lead.get('line')!r}")
            if u["live"]["speed_kmh"] != lead.get("speed_kmh"):
                lmis.append(f"{sn}.speed {u['live']['speed_kmh']!r} != {lead.get('speed_kmh')!r}")
    ck(not lmis, "line and speed are the LEAD vehicle's own values", str(lmis[:4]))
    # At least one running unit must actually have a non-empty line, or the
    # assertion above passes on a payload where every line is None.
    ck(sum(1 for u in running if u["live"]["line"]) >= 5,
       f"the fixture has real line names "
       f"({sum(1 for u in running if u['live']['line'])})",
       str(sorted({u["live"]["line"] for u in running if u["live"]["line"]})[:4]))
    # NOT in the archive half. A cached line/speed would be a number that was
    # once right, which the client cannot tell from one that still is.
    arch_rows = server._fleet_cache[1]
    ck(all("line" not in r and "speed_kmh" not in r for r in arch_rows),
       "the CACHED archive rows carry no line or speed")
    ck(all(u["live"] is None for u in not_running),
       "a unit that is not running has no live block to read a line from")

    # ---- the status glyph's data: n_neg, not n --------------------------
    # Praise is a report and must NOT raise a warning triangle. Proved with
    # real rows through the real feedback module, not by trusting the field
    # name: one unit gets a fault, another gets praise only.
    keyed = [u["set_no"] for u in units if u["set_no"]]
    bad_set, praise_set = keyed[0], keyed[1]
    # The app does this at startup; the gate calls api_fleet() directly, so it
    # must create the schema itself or the inserts below hit "no such table".
    feedback.init_db()
    feedback.add_report(unit=f"AM {bad_set}", category="smell",
                        severity=3, car_pos=2, consist=3)
    feedback.add_report(unit=f"AM {praise_set}", category="praise",
                        severity=1)
    r5 = json.loads(asyncio.get_event_loop()
                    .run_until_complete(server.api_fleet()).body)
    rby = {u["set_no"]: u for u in r5["units"] if u["set_no"]}
    ck(all(u["reports"] is not None for u in r5["units"] if u["set_no"]),
       "every keyed entry carries a reports block")
    ck(rby[bad_set]["reports"]["n_neg"] >= 1,
       f"a fault raises n_neg ({rby[bad_set]['reports']['n_neg']})")
    ck(rby[bad_set]["reports"]["worst_neg"] is not None,
       "a fault carries worst_neg")
    ck(rby[praise_set]["reports"]["n_praise"] >= 1,
       f"praise is counted as praise ({rby[praise_set]['reports']['n_praise']})")
    # THE ASSERTION THIS WHOLE BLOCK EXISTS FOR.
    ck(rby[praise_set]["reports"]["n_neg"] == 0,
       f"praise does NOT raise n_neg ({rby[praise_set]['reports']['n_neg']})",
       json.dumps(rby[praise_set]["reports"]))
    ck(rby[praise_set]["reports"]["n"] >= 1,
       "praise still counts in the honest total `n`")
    clean = [u for u in r5["units"]
             if u["set_no"] and u["reports"] and u["reports"]["n"] == 0]
    ck(len(clean) >= 5, f"most units have no reports at all ({len(clean)})")
    ck(all(u["reports"]["n_neg"] == 0 for u in clean),
       "an unreported unit reads n_neg 0, which is the tick state")

    # ---- /api/fleet/{set_no} -------------------------------------------
    u1 = json.loads(asyncio.get_event_loop()
                    .run_until_complete(server.api_fleet_unit(bad_set)).body)
    ck(u1["set_no"] == bad_set and u1["unit"] == f"AM {bad_set}",
       f"the unit endpoint identifies its set ({u1['unit']})")
    ck(u1["known"] is True, "a set the archive knows reads known")
    ck(u1["labels"], f"the unit reports its feed labels ({u1['labels']})")
    # Labels must be the folded set, including bare twins -- not a guessed
    # "AMP {n}", which would under-count every stat built on them.
    expect = sorted(l for l in raw if feedback.set_number(l) == bad_set)
    ck(u1["labels"] == expect,
       "labels are every archive label folding onto this set",
       f"{u1['labels']} vs {expect}")
    a1 = u1["archive"]
    ck(a1["enabled"] is True, "the unit endpoint reports the archive enabled")
    ck(a1["obs"] > 0, f"the unit has observations ({a1['obs']})")
    ck(a1["days"] and a1["days_active"] == len(a1["days"]),
       f"days_active is the number of days with rows ({a1['days_active']})")
    ck(all(d_["fixes"] > 0 for d_ in a1["days"]),
       "no day is reported with zero fixes -- absent means absent")
    ck(sum(d_["fixes"] for d_ in a1["days"]) == a1["obs"],
       "the per-day fixes sum to the total observations",
       f"{sum(d_['fixes'] for d_ in a1['days'])} vs {a1['obs']}")
    cov = a1["coverage"]
    # BAIL OUT NAMED if the coverage block is absent or empty. Everything
    # below subscripts it, so a missing key raises KeyError and the suite dies
    # with a traceback and NO tally -- measured, when a sabotage blanked
    # `coverage` to `{}`. A stack trace is a failure but it is not a
    # diagnosis, which is the same rule the browser gate's column checks
    # already follow.
    if not cov or cov.get("first_ts") is None or cov.get("last_ts") is None:
        ck(False, "the coverage window is reported, so no figure can imply "
                  "more history", f"coverage={cov!r} -- fix that first")
    else:
        ck(cov["first_ts"] and cov["last_ts"],
           "the coverage window is reported, so no figure can imply more history")
        ck(cov["first_ts"] <= a1["first_ts"] and cov["last_ts"] >= a1["last_ts"],
           "the unit's own span sits inside the archive's coverage window")
    ck(u1["reports"] and any(r["set_no"] == bad_set for r in u1["reports"]),
       f"the reported unit's own reports come back ({len(u1['reports'])})")

    # The top speed, and its coupled-partner cross-check.
    tops = [(u["set_no"],
             json.loads(asyncio.get_event_loop()
                        .run_until_complete(
                            server.api_fleet_unit(u["set_no"])).body))
            for u in units[:14] if u["set_no"]]
    withtop = [(sn, b) for sn, b in tops if b["archive"].get("top_speed")]
    ck(withtop, f"some units report a top speed ({len(withtop)} of {len(tops)})")
    bad_speed = [(sn, b["archive"]["top_speed"]["kmh"]) for sn, b in withtop
                 if not (0 <= b["archive"]["top_speed"]["kmh"] <= 160)]
    ck(not bad_speed, "every top speed is a plausible km/h (dkmh/10 applied)",
       str(bad_speed[:4]))
    ck(all("corroborated" in b["archive"]["top_speed"] for _, b in withtop),
       "every top speed states whether the coupled partner corroborates it")
    corr = [(sn, b["archive"]["top_speed"]) for sn, b in withtop
            if b["archive"]["top_speed"]["partner"]]
    ck(corr, f"at least one record has a coupled partner to check against "
             f"({len(corr)})")
    ck(all(t["corroborated"] == (t["partner_kmh"] is not None
                                 and abs(t["partner_kmh"] - t["kmh"]) <= 5.0)
           for _, t in corr),
       "`corroborated` is the partner agreeing, not merely existing")

    # THE STALE-FIX FILTER ON THE TOP SPEED. A fix that is 190 s old reports a
    # speed the train had three minutes and several kilometres ago, so
    # attributing it to that row's time and place is wrong twice. The filter
    # is `fix_age_s <= 60`.
    #
    # MEASURED: removing the filter altogether left this suite at 85/85,
    # because the fixture's fastest row happens to be fresh. So the filter is
    # exercised directly -- a deliberately stale, deliberately absurd row is
    # written and must NOT become the reported record.
    import sqlite3 as _sq
    con = _sq.connect(str(db))
    victim = sorted(by_set)[0]
    upk = con.execute(
        "SELECT unit_pk FROM unit WHERE unit=?",
        (sorted(l for l in raw if feedback.set_number(l) == victim)[0],)
    ).fetchone()[0]
    before = json.loads(asyncio.get_event_loop().run_until_complete(
        server.api_fleet_unit(victim)).body)["archive"]["top_speed"]
    stale_ts = int(before["ts"]) + 7      # a second nobody else occupies
    con.execute(
        "INSERT OR REPLACE INTO obs(ts, unit_pk, speed_dkmh, fix_age_s, flags)"
        " VALUES(?,?,?,?,0)", (stale_ts, upk, 1555, 190))   # 155.5 km/h, 190s old
    con.commit()
    fresh_ok = con.execute(
        "SELECT count(*) FROM obs WHERE unit_pk=? AND fix_age_s>60", (upk,)
    ).fetchone()[0]
    ck(fresh_ok >= 1, f"a stale 155.5 km/h row was planted ({fresh_ok})")
    after = json.loads(asyncio.get_event_loop().run_until_complete(
        server.api_fleet_unit(victim)).body)["archive"]["top_speed"]
    ck(after and after["kmh"] < 155.0,
       f"a 190s-old fix is NOT reported as the top speed "
       f"({after['kmh'] if after else None} km/h)",
       json.dumps(after))
    ck(after and after["ts"] != stale_ts,
       "and the reported record is not the stale row's timestamp")
    # The mirror: the SAME absurd speed on a FRESH fix must be accepted, or
    # the assertion above would pass on a filter that rejected everything.
    con.execute(
        "INSERT OR REPLACE INTO obs(ts, unit_pk, speed_dkmh, fix_age_s, flags)"
        " VALUES(?,?,?,?,0)", (stale_ts + 1, upk, 1554, 4))  # 155.4 km/h, 4s old
    con.commit()
    after2 = json.loads(asyncio.get_event_loop().run_until_complete(
        server.api_fleet_unit(victim)).body)["archive"]["top_speed"]
    ck(after2 and abs(after2["kmh"] - 155.4) < 0.01,
       f"a FRESH fix at the same speed IS reported "
       f"({after2['kmh'] if after2 else None} km/h) -- the filter is on age, "
       f"not on magnitude", json.dumps(after2))
    # Clean up, so the assertions that follow see the real fixture.
    con.execute("DELETE FROM obs WHERE unit_pk=? AND ts IN (?,?)",
                (upk, stale_ts, stale_ts + 1))
    con.commit()
    con.close()
    restored = json.loads(asyncio.get_event_loop().run_until_complete(
        server.api_fleet_unit(victim)).body)["archive"]["top_speed"]
    ck(restored and abs(restored["kmh"] - before["kmh"]) < 0.01,
       "the planted rows were removed and the real record is back",
       f"{restored['kmh'] if restored else None} vs {before['kmh']}")

    # An unknown set is a real answer, not a 500 and not an invented unit.
    uk = json.loads(asyncio.get_event_loop()
                    .run_until_complete(server.api_fleet_unit(9998)).body)
    ck(uk["known"] is False, "an unrecorded set reads known: false")
    ck(uk["archive"]["obs"] == 0 and uk["archive"]["days"] == [],
       "an unrecorded set reports no observations rather than guessing")
    ck(uk["live"] is None and uk["reports"] == [],
       "an unrecorded set carries no live block and no reports")
    ck(uk["archive"]["coverage"]["first_ts"],
       "an unrecorded set STILL states the coverage window")
    bad = asyncio.get_event_loop().run_until_complete(server.api_fleet_unit(0))
    ck(bad.status_code == 400, f"set 0 is rejected ({bad.status_code})")

    # Nothing invented on the unit page either.
    blob1 = json.dumps(u1).lower()
    inv1 = [w for w in ("built", "depot", "withdrawn", "livery", "mileage",
                        "kilometres run", "manufactur", "refurb", "scrapped")
            if w in blob1]
    ck(not inv1, "the unit endpoint invents no fleet metadata", str(inv1))

    # ---- /api/archive/days ---------------------------------------------
    # The endpoint that exists because /api/archive/day takes `day`, singular,
    # and silently ignores `days` -- which is why ?days=14 returned one day.
    ds = json.loads(asyncio.get_event_loop()
                    .run_until_complete(server.api_archive_days(14)).body)
    ck(ds["enabled"] is True, "the day series reports the archive enabled")
    ck(ds["days"], f"the day series returns days ({ds['returned']})")
    ck([x["day"] for x in ds["days"]] == sorted(x["day"] for x in ds["days"]),
       "the day series is oldest-first, so a strip reads left to right")
    ck(all(x["fixes"] > 0 for x in ds["days"]),
       "no day in the series is reported with zero fixes")
    ck(sum(x["fixes"] for x in ds["days"]) == a.count(),
       f"the series accounts for every obs row ({a.count()})")
    # The limit is a real limit, exercised rather than assumed.
    ds1 = json.loads(asyncio.get_event_loop()
                     .run_until_complete(server.api_archive_days(1)).body)
    ck(ds1["returned"] == 1, f"n=1 returns one day ({ds1['returned']})")
    # And the OLD endpoint's behaviour is pinned, so the finding cannot be
    # quietly undone: `day` is singular and defaults to today.
    one = json.loads(asyncio.get_event_loop()
                     .run_until_complete(server.api_archive_day(None, None)).body)
    ck(len(one["days"]) == 1,
       "/api/archive/day still answers for exactly ONE day")

    # THE SERIES MUST SPAN SEVERAL DAYS OR THE ORDER CHECK IS VACUOUS.
    # Measured: the parity fixture is six captures from a single afternoon, so
    # day_series returned exactly ONE day -- and a one-element list is sorted
    # under every ordering, so the oldest-first assertion above passed while
    # `out.reverse()` was deleted. A sabotage run proved it: removing the
    # reverse left test_fleet at 90/90.
    #
    # So plant rows on distinct days directly, assert the order, then remove
    # them. The planted days straddle the real one in BOTH directions, so a
    # newest-first bug cannot be masked by the real day sorting to an end.
    import sqlite3 as _sq
    con = _sq.connect(str(db))
    victim_pk = con.execute("SELECT unit_pk FROM obs LIMIT 1").fetchone()[0]
    base_ts = con.execute("SELECT max(ts) FROM obs").fetchone()[0]
    DAY = 86400
    planted = [base_ts - 3 * DAY, base_ts - 2 * DAY, base_ts + 2 * DAY]
    for i, t in enumerate(planted):
        con.execute("INSERT INTO obs(ts,unit_pk,speed_dkmh,fix_age_s,flags)"
                    " VALUES(?,?,?,?,0)", (t, victim_pk, 100 + i, 5))
    con.commit()
    con.close()
    ds_multi = json.loads(asyncio.get_event_loop()
                          .run_until_complete(server.api_archive_days(14)).body)
    dlist = [x["day"] for x in ds_multi["days"]]
    ck(len(dlist) >= 4,
       f"the series now spans several days, so order can fail ({len(dlist)})",
       str(dlist))
    ck(dlist == sorted(dlist),
       "the MULTI-DAY series is oldest-first, so a strip reads left to right",
       str(dlist))
    ck(dlist[0] < dlist[-1],
       "the first day in the series is strictly older than the last",
       f"{dlist[0]} .. {dlist[-1]}")
    # Days with no rows stay ABSENT rather than being filled in as zero: the
    # gap between the planted clusters is 1 day wide and must not appear.
    ck(len(dlist) == len(set(dlist)) and all(x["fixes"] > 0 for x in ds_multi["days"]),
       "no empty day is invented to pad the series",
       f"{len(dlist)} days, min fixes={min(x['fixes'] for x in ds_multi['days'])}")
    con = _sq.connect(str(db))
    con.execute("DELETE FROM obs WHERE ts IN (?,?,?)", tuple(planted))
    con.commit()
    con.close()
    ck(len(a.day_series(14)) == 1,
       "the planted days were removed and the fixture is one day again")

    # ---- the coupled-partner cross-check on the TOP SPEED ---------------
    # `obs.partner_pk` points at the interned BARE-NUMBER twin ("1236"), and
    # that twin never owns an obs row. Resolving it back to the real labels is
    # the whole validity test on a speed record, and a sabotage that disabled
    # the resolution (`for cpk in cands:` -> `for cpk in []:`) left the suite
    # at 90/90 -- nothing asserted the partner was ever NAMED, let alone that
    # its speed agreed. Measured on this fixture: 30 of 32 multi-label sets
    # have a top speed whose partner resolves, e.g. set 157 at 93.2 km/h
    # corroborated by AMP 904 at 94.2.
    bare_lbl = {l for l in raw if not any(c.isalpha() for c in l)}
    ck(len(bare_lbl) >= 10,
       f"the fixture holds bare-number twins to resolve ({len(bare_lbl)})")
    by_set_lbl = {}
    for l in raw:
        _sn = feedback.set_number(l)
        if _sn:
            by_set_lbl.setdefault(_sn, []).append(l)
    multi_sets = {k: v for k, v in by_set_lbl.items() if len(v) > 1}
    ck(len(multi_sets) >= 10,
       f"sets carry both a lettered label and a twin ({len(multi_sets)})")
    tops = {sn: a.unit_stats(ls).get("top_speed")
            for sn, ls in sorted(multi_sets.items())}
    named = {sn: t for sn, t in tops.items() if t and t.get("partner")}
    ck(len(named) >= 10,
       f"the top speed NAMES its coupled partner ({len(named)}/{len(tops)} sets)",
       str([f"{sn}:{t['partner']}" for sn, t in list(named.items())[:3]]))
    # Naming is not enough -- the partner's own speed must be read back, which
    # is what proves the obs lookup found a real row and not just a label.
    withspd = {sn: t for sn, t in named.items() if t.get("partner_kmh") is not None}
    ck(len(withspd) >= 10,
       f"and reads the partner's speed at the same second ({len(withspd)} sets)")
    # Corroboration must be a COMPARISON, not a constant: true only when the
    # two readings are within tolerance.
    ck(all(t["corroborated"] == (abs(t["partner_kmh"] - t["kmh"]) <= 5.0)
           for t in withspd.values()),
       "corroborated is the ±5 km/h comparison, not a hardcoded flag")
    ck(any(t["corroborated"] for t in withspd.values()),
       "at least one record is actually corroborated")
    # The partner must never be the set's OWN label -- that would be a vehicle
    # corroborating itself.
    selfcorr = [sn for sn, t in named.items()
                if t["partner"] in multi_sets[sn]]
    ck(not selfcorr, "no set corroborates its own top speed", str(selfcorr[:4]))

    # ---- per-day fixes must account for the unit's own total ------------
    # The unit page draws a per-day strip; if the grouped query drops days the
    # strip silently shortens and no total disagrees with it. A `LIMIT 1`
    # sabotage on that query left the suite green.
    #
    # AND THE SET'S OWN ROWS MUST SPAN SEVERAL DAYS, or `LIMIT 1` drops
    # nothing and the check is vacuous a second time: measured, set 157 has
    # 6 observations all inside one afternoon, sum==obs==6 with one day. So
    # plant rows for THIS set on two further days first.
    sn_any = sorted(withspd)[0] if withspd else sorted(multi_sets)[0]
    pks_any = [a._units[l] for l in multi_sets[sn_any] if l in a._units]
    ck(len(pks_any) >= 1, f"set {sn_any} has archive rows to extend")
    con = _sq.connect(str(db))
    base2 = con.execute("SELECT max(ts) FROM obs WHERE unit_pk IN"
                        f" ({','.join('?' * len(pks_any))})", pks_any).fetchone()[0]
    spread = [base2 - 5 * DAY, base2 - 5 * DAY + 60, base2 - 4 * DAY]
    for i, t in enumerate(spread):
        con.execute("INSERT INTO obs(ts,unit_pk,speed_dkmh,fix_age_s,flags)"
                    " VALUES(?,?,?,?,0)", (t, pks_any[0], 200 + i, 5))
    con.commit()
    con.close()
    us_any = a.unit_stats(multi_sets[sn_any])
    ck(len(us_any["days"]) >= 3,
       f"set {sn_any} now spans several days, so a dropped day can fail "
       f"({len(us_any['days'])})",
       str([d["day"] for d in us_any["days"]]))
    ck(us_any["obs"] > 0, f"set {sn_any} has observations ({us_any['obs']})")
    ck(sum(d["fixes"] for d in us_any["days"]) == us_any["obs"],
       f"set {sn_any}: the per-day strip accounts for every observation",
       f"sum={sum(d['fixes'] for d in us_any['days'])} obs={us_any['obs']}")
    ck(us_any["days_active"] == len(us_any["days"]),
       "days_active is the length of the day list, not a separate guess")
    # The strip must also be oldest-first for the same reason the series is.
    _sd = [d["day"] for d in us_any["days"]]
    ck(_sd == sorted(_sd), "the per-day strip is oldest-first", str(_sd))
    con = _sq.connect(str(db))
    con.execute(f"DELETE FROM obs WHERE ts IN ({','.join('?' * len(spread))})",
                tuple(spread))
    con.commit()
    con.close()
    # Re-read AFTER the planted rows are gone, so the endpoint comparison
    # below is against the fixture's real counts and not the inflated ones.
    us_any = a.unit_stats(multi_sets[sn_any])
    ck(sum(d["fixes"] for d in us_any["days"]) == us_any["obs"],
       f"set {sn_any}: the strip still accounts for every observation once "
       f"the planted days are removed",
       f"sum={sum(d['fixes'] for d in us_any['days'])} obs={us_any['obs']}")

    # ---- unit labels are RESOLVED, never guessed as a string ------------
    # `/api/fleet/{n}` must union every label that folds to the set number,
    # including the bare twin. Guessing `AMP {n}` drops the twin -- which owns
    # half the obs rows -- and the unit page then reports half the history.
    sn_probe = sorted(multi_sets)[0]
    up = json.loads(asyncio.get_event_loop()
                    .run_until_complete(server.api_fleet_unit(sn_probe)).body)
    ck(set(up["labels"]) == set(multi_sets[sn_probe]),
       f"set {sn_probe} resolves to every one of its labels",
       f"{sorted(up['labels'])} vs {sorted(multi_sets[sn_probe])}")
    ck(any(not any(c.isalpha() for c in l) for l in up["labels"]),
       f"set {sn_probe} includes its bare-number twin",
       str(sorted(up["labels"])))
    ck(up["archive"]["obs"] == us_any["obs"] if sn_probe == sn_any else True,
       "the endpoint reports the same obs count as the archive directly")

    # ---- the TTL cache for the archive half ----------------------------
    # Added when the production query was measured at ~455 ms over 1.25 M obs
    # rows. Two properties have to hold together, and they pull against each
    # other: the expensive half must NOT re-run, while the live half MUST be
    # recomputed. A cache that satisfies only the first is a bug that serves
    # yesterday's running trains.
    calls = {"n": 0}
    real_fleet = a.fleet
    # Derived from the server's own TTL, not hardcoded: if someone retunes
    # _FLEET_TTL_S, this check follows instead of silently going vacuous.
    _TTL_JUMP = server._FLEET_TTL_S + 1

    def counting_fleet():
        calls["n"] += 1
        return real_fleet()

    a.fleet = counting_fleet
    server._fleet_cache = (0.0, [])          # cold

    r1 = json.loads(asyncio.get_event_loop()
                    .run_until_complete(server.api_fleet()).body)
    after_first = calls["n"]
    r2 = json.loads(asyncio.get_event_loop()
                    .run_until_complete(server.api_fleet()).body)
    ck(after_first == 1, f"a cold call queries the archive once ({after_first})")
    ck(calls["n"] == 1,
       f"a second call inside the TTL does NOT re-query ({calls['n']})")
    ck(r1["count"] == r2["count"] and r1["count"] > 0,
       f"the cached call returns the same fleet ({r2['count']})")

    # The live half must still move. Withdraw every running service and the
    # SAME cached archive rows must now report nobody live -- proving the live
    # annotation is recomputed per request, not frozen into the cache.
    stashed = server.STATE["data"]
    server.STATE["data"] = {"ts": stashed.get("ts"), "trains": [], "services": []}
    r3 = json.loads(asyncio.get_event_loop()
                    .run_until_complete(server.api_fleet()).body)
    ck(calls["n"] == 1,
       f"still no re-query -- the archive half stayed cached ({calls['n']})")
    ck(r3["live_count"] == 0,
       f"live status is recomputed, not cached ({r3['live_count']})")
    ck(all(u["live"] is None for u in r3["units"]),
       "no unit keeps a stale live block when nothing is running")
    ck(r3["count"] == r1["count"],
       f"the archive half is unchanged by the live swing ({r3['count']})")

    # Restore the running payload: the previous request must not have mutated
    # the cached rows. This is the leak the cache is shaped to prevent -- it
    # caches RAW rows, not the folded dicts the live merge writes into.
    server.STATE["data"] = stashed
    r4 = json.loads(asyncio.get_event_loop()
                    .run_until_complete(server.api_fleet()).body)
    ck(r4["live_count"] == r1["live_count"] and r4["live_count"] > 0,
       f"live status returns intact after the swing ({r4['live_count']})")
    ck(r4["count"] == r1["count"],
       "the cached rows were not mutated by a previous request's live merge")

    # An expired TTL must actually re-query, or the "cache" is a one-shot
    # memo that never notices a new unit.
    #
    # The TTL is exercised through the REAL clock arithmetic, by moving the
    # module's own `time.time` forward past the window. An earlier version of
    # this check just overwrote `_fleet_cache` with a 0.0 expiry, which
    # bypassed the arithmetic and still passed when the TTL was sabotaged to
    # `float('inf')` -- an assertion that could not fail.
    real_time = server.time.time
    server.time.time = lambda: real_time() + _TTL_JUMP
    try:
        json.loads(asyncio.get_event_loop()
                   .run_until_complete(server.api_fleet()).body)
    finally:
        server.time.time = real_time
    ck(calls["n"] == 2,
       f"a TTL expiry {_TTL_JUMP}s later re-queries ({calls['n']})")
    a.fleet = real_fleet

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
