"""Tests for the feedback store. Run: .venv/bin/python src/test_feedback.py

Uses a throwaway DB file so it never touches real reports.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feedback as f  # noqa: E402

PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def raises(name: str, fn, frag: str) -> None:
    global PASS, FAIL
    try:
        fn()
    except ValueError as e:
        if frag.lower() in str(e).lower():
            PASS += 1
            return
        FAIL += 1
        print(f"  FAIL {name}: wrong message {e!r} (wanted {frag!r})")
        return
    except Exception as e:
        FAIL += 1
        print(f"  FAIL {name}: raised {type(e).__name__} not ValueError")
        return
    FAIL += 1
    print(f"  FAIL {name}: no error raised")


def main() -> None:
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    f.DB_PATH = tmp
    f.init_db()

    # --- set_number: every car prefix of a set must collapse to one number ---
    for s in ("AMT701", "AMP701", "AMA701", "amp 701", "701",
              "AMP        701", " amt-701 "):
        check(f"set_number({s!r})", f.set_number(s), 701)
    check("set_number(None)", f.set_number(None), None)
    check("set_number('')", f.set_number(""), None)
    check("set_number('rubbish')", f.set_number("rubbish"), None)
    check("set_number('AM')", f.set_number("AM"), None)
    # The regression that mattered: don't truncate a long number onto a real unit.
    check("set_number('12345') must not become 1234", f.set_number("12345"), None)
    check("set_number('9')", f.set_number("9"), None)

    # --- validation ---
    raises("bad unit", lambda: f.add_report("zzz", "smell", 2), "set number")
    raises("bad category", lambda: f.add_report("701", "nope", 2), "category must be")
    raises("bad severity", lambda: f.add_report("701", "smell", 9), "severity must be")
    raises("severity text", lambda: f.add_report("701", "smell", "x"), "severity must be")
    raises("bad case ref", lambda: f.add_report("701", "smell", 2, case_ref="no spaces here"),
           "case reference")
    raises("bad date", lambda: f.add_report("701", "smell", 2, occurred_at="last tuesday"),
           "iso format")

    # --- happy path ---
    r = f.add_report("AMT701", "smell", 3, note="strong",
                     case_ref="CAS-PB1H5-S9R4F8", occurred_at="2026-11-17",
                     )
    check("insert returns set_no", r["set_no"], 701)
    check("one report stored", f.stats()["reports"], 1)

    # A different car prefix, same set -> must land on the same unit.
    f.add_report("AMP 701", "smell", 1, note="weaker", occurred_at="2026-11-18")
    check("AMT and AMP collapse to one set", f.stats()["units"], 1)
    check("two reports for 701", len(f.list_reports(701)), 2)

    # --- date-only must not sort to midnight below today's timestamped rows ---
    import datetime as _dt
    today = _dt.datetime.now(f.NZ).date().isoformat()
    f.add_report("888", "noise", 1, occurred_at=today)
    got = f.list_reports(888)[0]["occurred_at"]
    check("date-only today gets a real time", got[11:19] != "00:00:00", True)
    f.add_report("889", "noise", 1, occurred_at="2020-01-02")
    check("past date-only gets midday", f.list_reports(889)[0]["occurred_at"][11:16], "12:00")
    check("stored with timezone", "+" in f.list_reports(889)[0]["occurred_at"], True)
    # A full timestamp must be preserved as given.
    f.add_report("890", "noise", 1, occurred_at="2026-11-17T08:30")
    check("full timestamp kept", f.list_reports(890)[0]["occurred_at"][:16], "2026-11-17T08:30")

    # --- ordering: newest occurrence first ---
    rows = f.list_reports(701)
    check("newest first", rows[0]["note"], "weaker")

    # --- note truncation, not rejection ---
    long_note = "x" * (f.MAX_NOTE + 200)
    f.add_report("253", "cleanliness", 2, note=long_note)
    got = f.list_reports(253)[0]["note"]
    check("note truncated to MAX_NOTE", len(got), f.MAX_NOTE)

    # --- summary shape, which the map depends on ---
    s = f.summary()
    check("summary keyed by string set no", "701" in s, True)
    check("summary counts", s["701"]["n"], 2)
    check("summary worst severity", s["701"]["worst"], 3)
    check("summary top category", s["701"]["top_category"], "smell")

    # --- repeat offenders ---
    st = f.stats()
    check("repeat offender found", st["repeat_offenders"][0]["set_no"], 701)
    check("repeat offender count", st["repeat_offenders"][0]["n"], 2)
    check("category tally", st["by_category"]["smell"], 2)

    # --- limit is clamped, not trusted ---
    check("limit clamped high", len(f.list_reports(limit=99999)) <= 200, True)
    check("limit clamped low", len(f.list_reports(limit=0)) >= 1, True)

    # --- unit / car / service (AM 701 = 3 cars: AMP 701, AMT 701, AMA 701) ---
    r = f.add_report("AM 701", "smell", 2, car_pos=2, consist=3,
                     service_units=[701], route="E-W", trip_id="T1")
    check("middle car derives AMT", r["car_type"], "AMT")
    check("unit is AM n", r["unit"], "AM 701")
    check("car is AMT 701", r["car"], "AMT 701")
    check("end car stays unknown", f.add_report("701", "smell", 1, car_pos=1, consist=3)["car_type"], None)
    check("explicit AMA on end car", f.add_report("701", "smell", 1, car_pos=3, consist=3, car_type="ama")["car_type"], "AMA")
    raises("AMP in middle", lambda: f.add_report("701", "smell", 1, car_pos=2, car_type="AMP"), "middle car")
    raises("AMT on end", lambda: f.add_report("701", "smell", 1, car_pos=1, car_type="AMT"), "end car")
    raises("bad car type", lambda: f.add_report("701", "smell", 1, car_type="XYZ"), "car type")
    check("clean_service_units sorted+deduped", f.clean_service_units([701, "402", "AMP 701"]), "402,701")
    check("clean_service_units caps at 2", len(f.clean_service_units([11, 22, 33]).split(",")), 2)
    rows = [x for x in f.list_reports(701, 50) if x["service_units"] == "701"]
    check("row carries service + route", (rows[0]["service"], rows[0]["route"], rows[0]["unit"]), ("AM 701", "E-W", "AM 701"))
    r6 = f.add_report("402", "smell", 2, car_pos=5, consist=6, service_units=[701, 402], trip_id="T2")
    check("car 5 of 6 is a middle car", r6["car_type"], "AMT")
    check("trip_id kept for non-people reports", f._conn().execute("SELECT trip_id FROM reports WHERE id=?", (r6["id"],)).fetchone()[0], "T2")

    # --- the History count must be reconcilable (the "number of reports on
    #     the units doesn't match the reports" bug) ---
    #
    # `n` counts every kept row; `n_neg` counts OPEN VEHICLE FAULTS. They
    # legitimately differ, but the UI showed `n` under the word "Reports"
    # beside a badge driven by `n_neg`, so a unit whose only history was an
    # expired people report advertised reports it then refused to display.
    # A people report on a trip that is NOT in the live set has expired.
    f.add_report("238", "ruffians", 2, behaviour="loud", trip_id="GONE")
    s2 = f.summary(live_trips=set())          # nothing running: trip expired
    e = s2["238"]
    check("expired people report still counted in n", e["n"], 1)
    check("expired people report is not a fault", e["n_neg"], 0)
    check("expired people report is not live", e.get("n_people", 0), 0)
    check("expired people report is nameable", e.get("n_expired"), 1)
    # Same row, same DB, but the trip is running: live, not expired.
    s3 = f.summary(live_trips={"GONE"})
    check("live people report counted as live", s3["238"].get("n_people"), 1)
    check("live people report not double-counted as expired",
          s3["238"].get("n_expired", 0), 0)

    # --- repeat offenders must mean OPEN FAULTS, not raw rows ---
    # AM 238's only history is one expired ruffians report: zero faults, so it
    # must not be named a repeat offender however many rows it accumulates.
    f.add_report("238", "ruffians", 2, behaviour="loud", trip_id="GONE2")
    st2 = f.stats(live_trips=set())
    check("two expired people rows counted in n",
          f.summary(live_trips=set())["238"]["n"], 2)
    check("but no open faults", f.summary(live_trips=set())["238"]["n_neg"], 0)
    check("so not a repeat offender",
          [x for x in st2["repeat_offenders"] if x["set_no"] == 238], [])
    # Praise is not a fault either.
    f.add_report("1087", "praise", 1)
    f.add_report("1087", "praise", 1)
    st3 = f.stats(live_trips=set())
    check("praise-only set is not a repeat offender",
          [x for x in st3["repeat_offenders"] if x["set_no"] == 1087], [])
    # The real offender is still reported, and on its fault count.
    off = [x for x in st3["repeat_offenders"] if x["set_no"] == 701]
    check("genuine repeat offender still listed", len(off), 1)
    check("repeat offender counts faults not rows", off[0]["n"],
          f.summary(live_trips=set())["701"]["n_neg"])

    # --- /api/health must not pay for the fault maths ---
    sh = f.stats(repeat=False)
    check("health stats skip repeat offenders", "repeat_offenders" in sh, False)
    check("health stats still carry the row count",
          sh["reports"], f.stats(live_trips=set())["reports"])

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
