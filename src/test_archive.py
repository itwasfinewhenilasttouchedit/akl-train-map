"""Tests for the observation archive.

These target the failure modes that would make the archive silently worthless
years from now, not just "does it insert a row".
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

import archive


def _train(unit="AMP 281", **kw):
    t = {
        "unit": unit, "trip_id": "258-880002-75180-2-W189600-24ec92dc",
        "route": "E-W", "origin": "Swanson", "terminus": "Manukau",
        "dest": "Manukau", "lat": -36.884736, "lon": 174.714033,
        "bearing": 41.1, "speed_kmh": 30.3, "delay_s": -3, "progress": 0.006,
        "fix_age_s": 8, "in_service": True, "moving": True, "cancelled": False,
        "predicted": True, "consist": 6, "partner_set": "593",
        "consist_position": "rear", "consist_source": "at",
        "order_source": "inferred",
    }
    t.update(kw)
    return t


def _payload(trains, ts=1795000000, poll_ms=540):
    return {"ts": ts, "poll_ms": poll_ms, "trains": trains}


def _fresh():
    p = Path(tempfile.mkdtemp()) / "archive.db"
    a = archive.Archive(p)
    a.open()
    return a


def test_roundtrip_values():
    """Every field survives the int-packing round trip."""
    a = _fresh()
    assert a.record(_payload([_train()])) == 1
    h = a.history("AMP 281")
    assert len(h) == 1
    r = h[0]
    assert r["ts"] == 1795000000
    assert abs(r["lat"] - -36.884736) < 1e-9
    assert r["bearing"] == 41           # whole degrees by design
    assert abs(r["speed_kmh"] - 30.3) < 0.05   # decikm/h round trip
    assert r["delay_s"] == -3           # negative delay must survive
    assert r["fix_age_s"] == 8
    assert r["in_service"] is True and r["moving"] is True
    assert r["cancelled"] is False
    assert r["consist"] == 6 and r["position"] == "rear"
    a.close()
    print("roundtrip ok")


def test_fix_age_always_stored():
    """fix_age_s is the column that justifies the archive. Never drop it."""
    a = _fresh()
    a.record(_payload([_train(fix_age_s=0), _train(unit="AMP 593", fix_age_s=27)]))
    db = sqlite3.connect(str(a.path))
    ages = [r[0] for r in db.execute("SELECT fix_age_s FROM obs ORDER BY fix_age_s")]
    assert ages == [0, 27], ages
    # and a missing fix age must be NULL, not silently 0 — "unknown" and
    # "simultaneous" are completely different claims for skew correction
    a.record(_payload([_train(unit="AMP 999", fix_age_s=None)], ts=1795000020))
    row = db.execute("SELECT fix_age_s FROM obs WHERE unit_pk="
                     "(SELECT unit_pk FROM unit WHERE unit='AMP 999')").fetchone()
    assert row[0] is None, row
    a.close()
    print("fix_age ok (0 preserved, None stays NULL)")


def test_measured_vs_inferred_preserved():
    """An archive that cannot tell AT-reported from inferred is a trap."""
    a = _fresh()
    a.record(_payload([
        _train(unit="A", consist_source="at", order_source="latched"),
        _train(unit="B", consist_source="inferred", order_source=None),
    ]))
    db = sqlite3.connect(str(a.path))
    got = dict(db.execute(
        "SELECT u.unit, o.c_src FROM obs o JOIN unit u USING(unit_pk)"))
    assert got["A"] == archive.SRC["at"]
    assert got["B"] == archive.SRC["inferred"]
    osrc = dict(db.execute(
        "SELECT u.unit, o.o_src FROM obs o JOIN unit u USING(unit_pk)"))
    assert osrc["A"] == archive.SRC["latched"]
    assert osrc["B"] is None    # unknown order must not masquerade as a value
    a.close()
    print("measured-vs-inferred ok")


def test_dimensions_are_deduped():
    """The 2.2x size win depends on NOT repeating trip/unit text per row."""
    a = _fresh()
    for i in range(50):
        a.record(_payload([_train(), _train(unit="AMP 593")],
                          ts=1795000000 + i * 20))
    db = sqlite3.connect(str(a.path))
    assert db.execute("SELECT count(*) FROM obs").fetchone()[0] == 100
    assert db.execute("SELECT count(*) FROM unit").fetchone()[0] == 3  # +partner
    assert db.execute("SELECT count(*) FROM trip").fetchone()[0] == 1
    a.close()
    print("dimension dedupe ok (100 obs, 3 units, 1 trip)")


def test_failed_poll_is_recorded_not_a_hole():
    """'No trains running' and 'the phone fell off Wi-Fi' must differ."""
    a = _fresh()
    a.record(_payload([_train()], ts=1795000000))
    a.record_failure("HTTPError: 503", ts=1795000020)
    a.record(_payload([], ts=1795000040))        # genuinely zero trains
    db = sqlite3.connect(str(a.path))
    rows = list(db.execute("SELECT ts, ok, trains, error FROM poll ORDER BY ts"))
    assert rows[0][1] == 1 and rows[0][2] == 1
    assert rows[1][1] == 0 and rows[1][3] == "HTTPError: 503"
    assert rows[2][1] == 1 and rows[2][2] == 0   # ok, but zero trains
    a.close()
    print("gap rows ok (failure != empty)")


def test_clock_state_recorded():
    """No RTC on the phone: a bad clock must be visible in the data."""
    a = _fresh()
    a.record(_payload([_train()]))
    db = sqlite3.connect(str(a.path))
    assert db.execute("SELECT clock_ok FROM poll").fetchone()[0] in (0, 1)
    assert isinstance(archive.clock_synced(), bool)
    a.close()
    print("clock flag ok")


def test_write_never_raises_into_poller():
    """The live map is the product; the archive must not take it down."""
    a = _fresh()
    a.close()                      # connection gone
    a._db = sqlite3.connect("/nonexistent-dir/x.db") if False else None
    assert a.record(_payload([_train()])) == 0      # returns 0, does not raise
    # and a corrupt payload must not raise either
    a2 = _fresh()
    assert a2.record({"ts": "not-a-number", "trains": [{"unit": "X"}]}) == 0
    assert a2.record({"trains": None}) == 0
    a2.close()
    print("resilience ok")


def test_restart_resumes_same_db():
    """Dimension caches must reload, not duplicate."""
    p = Path(tempfile.mkdtemp()) / "a.db"
    a = archive.Archive(p); a.open()
    a.record(_payload([_train()])); a.close()
    b = archive.Archive(p); b.open()
    b.record(_payload([_train()], ts=1795000020))
    db = sqlite3.connect(str(p))
    assert db.execute("SELECT count(*) FROM unit").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM obs").fetchone()[0] == 2
    assert b.count() == 2
    b.close()
    print("restart ok")


def test_idempotent_same_ts():
    """A replayed poll must not double-count."""
    a = _fresh()
    a.record(_payload([_train()]))
    a.record(_payload([_train()]))      # same ts, same unit
    assert a.count() == 1
    a.close()
    print("idempotent ok")


def test_disabled_by_env():
    old = os.environ.get("ARCHIVE_DB")
    os.environ["ARCHIVE_DB"] = "off"
    try:
        assert archive.init() is None
    finally:
        if old is None:
            os.environ.pop("ARCHIVE_DB", None)
        else:
            os.environ["ARCHIVE_DB"] = old
    print("env disable ok")


# ---------------------------------------------------------------- new fields

def test_previously_discarded_fields_roundtrip():
    """Everything we poll is now stored and comes back intact.

    These eight fields were fetched from AT on every poll for weeks and thrown
    away. The point of the test is that each one survives, because a column
    that silently stays NULL looks exactly like a field AT never sent.
    """
    a = _fresh()
    assert a.record(_payload([_train(
        occupancy=2, **{"from": "Newmarket", "to": "Remuera"},
        platform="2", eta_s=140,
        skipped_stops=["Greenlane", "Ellerslie"])])) == 1
    r = a.history("AMP 281")[0]
    assert r["occupancy"] == 2, r
    assert r["from"] == "Newmarket" and r["to"] == "Remuera", r
    assert r["platform"] == "2" and r["eta_s"] == 140, r
    assert r["skipped_stops"] == ["Greenlane", "Ellerslie"], r
    a.close()
    print("new per-train fields ok")


def test_out_of_service_near_station():
    """Stabled stock has no trip but does have a nearest station."""
    a = _fresh()
    a.record(_payload([_train(trip_id=None, in_service=False,
                              near="Wiri", near_m=240)]))
    r = a.history("AMP 281")[0]
    assert r["near"] == "Wiri" and r["near_m"] == 240, r
    assert r["in_service"] is False
    a.close()
    print("oos near-station ok")


def test_place_dimension_dedupes():
    """Station names are normalised, not repeated per row."""
    a = _fresh()
    for i in range(50):
        a.record(_payload([_train(**{"from": "Newmarket", "to": "Remuera"})],
                          ts=1795000000 + i * 20))
    n = a._db.execute("SELECT count(*) FROM place").fetchone()[0]
    assert n == 2, f"expected 2 places, got {n}"
    assert a.count() == 50
    a.close()
    print("place dedupe ok (50 obs, 2 places)")


def test_poll_health_counters():
    """Feed-health counters are stored, not just the train count.

    2026-11-25 had polls returning ok=1 while the feed degraded; `trains`
    alone could not show it.
    """
    a = _fresh()
    p = _payload([_train()])
    p.update({"vehicles_in_feed": 1431, "trip_updates": 920,
              "with_prediction": 61, "stale_dropped": 4,
              "unknown_trip": 2, "in_service": 39, "out_of_service": 55,
              "source": "gtfs-realtime"})
    a.record(p)
    row = a._db.execute(
        "SELECT veh_feed, trip_upd, with_pred, stale_drop, unknown_trip,"
        " in_svc, oos, source FROM poll").fetchone()
    assert row == (1431, 920, 61, 4, 2, 39, 55, "gtfs-realtime"), row
    a.close()
    print("poll health counters ok")


def test_migration_from_old_schema():
    """An existing database gains the new columns without losing rows.

    This is the failure that would have mattered in production: the live DB is
    31 MB and CREATE TABLE IF NOT EXISTS is a no-op on it, so without ALTER
    TABLE the writer would raise on every poll from the moment of deploy.
    """
    p = Path(tempfile.mkdtemp()) / "old.db"
    db = sqlite3.connect(str(p))
    db.executescript("""
      CREATE TABLE unit(unit_pk INTEGER PRIMARY KEY, unit TEXT UNIQUE NOT NULL);
      CREATE TABLE trip(trip_pk INTEGER PRIMARY KEY, trip_id TEXT UNIQUE NOT NULL,
        route TEXT, origin TEXT, terminus TEXT, dest TEXT);
      CREATE TABLE obs(ts INTEGER NOT NULL, unit_pk INTEGER NOT NULL,
        trip_pk INTEGER, lat REAL, lon REAL, bearing INTEGER, speed_dkmh INTEGER,
        delay_s INTEGER, progress REAL, fix_age_s INTEGER,
        flags INTEGER NOT NULL DEFAULT 0, consist INTEGER, partner_pk INTEGER,
        pos INTEGER, c_src INTEGER, o_src INTEGER,
        PRIMARY KEY (unit_pk, ts)) WITHOUT ROWID;
      CREATE TABLE poll(ts INTEGER PRIMARY KEY, ok INTEGER NOT NULL,
        trains INTEGER, poll_ms INTEGER, clock_ok INTEGER, error TEXT);
      CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
      INSERT INTO unit(unit_pk, unit) VALUES(1,'AMP 281');
      INSERT INTO obs(ts, unit_pk, delay_s, flags) VALUES(1794000000, 1, 42, 1);
    """)
    db.commit()
    db.close()

    a = archive.Archive(p)
    a.open()                                   # must migrate, not explode
    assert a.count() == 1, "pre-existing row lost"
    old = a.history("AMP 281")[0]
    assert old["delay_s"] == 42
    # Honest NULLs: we genuinely did not record these at the time.
    assert old["occupancy"] is None and old["from"] is None
    # And the writer works against the migrated table.
    assert a.record(_payload([_train(**{"from": "Newmarket"})],
                             ts=1795000000)) == 1
    assert a.count() == 2
    assert a.history("AMP 281")[0]["from"] == "Newmarket"
    a.close()
    print("migration ok (row kept, new cols NULL, writer works)")


def test_migration_is_idempotent():
    """Reopening an already-migrated database must not re-add columns."""
    p = Path(tempfile.mkdtemp()) / "a.db"
    for _ in range(3):
        a = archive.Archive(p)
        a.open()
        a.record(_payload([_train()]))
        a.close()
    db = sqlite3.connect(str(p))
    cols = [r[1] for r in db.execute("PRAGMA table_info(obs)")]
    assert len(cols) == len(set(cols)), cols
    assert "occ" in cols
    db.close()
    print("migration idempotent ok")


# ------------------------------------------------------------------- alerts

def _alert(aid="a1", header="South City line delays", **kw):
    a = {"id": aid, "header": header,
         "description": "Delays because of an earlier incident at Newmarket.",
         "effect": "REDUCED_SERVICE", "effect_detail": "Delays",
         "cause": "ACCIDENT", "severity": "SEVERE",
         "routes": ["S-C-201"], "stations": ["Newmarket"],
         "starts": 1795000000, "ends": None}
    a.update(kw)
    return a


def test_alert_recorded_and_upserted():
    """An unchanged alert seen repeatedly is ONE row with a moving last_seen."""
    a = _fresh()
    assert a.record_alerts({"now": [_alert()], "upcoming": [],
                            "total_feed": 161, "generated": 1795000000}) == 1
    assert a.record_alerts({"now": [_alert()], "upcoming": [],
                            "total_feed": 161, "generated": 1795000300}) == 0
    rows = a._db.execute(
        "SELECT alert_id, first_seen, last_seen, seen FROM alert").fetchall()
    assert len(rows) == 1, rows
    assert rows[0][1] == 1795000000 and rows[0][2] == 1795000300, rows
    assert rows[0][3] == 2, rows
    a.close()
    print("alert upsert ok (1 row, last_seen moved, seen=2)")


def test_alert_edit_appends_revision():
    """AT editing an alert must not destroy the earlier wording.

    This is the whole timeline: "delays" becoming "suspended until end of
    service" is the incident escalating, and overwriting it loses that.
    """
    a = _fresh()
    a.record_alerts({"now": [_alert()], "generated": 1795000000})
    a.record_alerts({"now": [_alert(header="Onehunga West line suspended")],
                     "generated": 1795003600})
    rows = a._db.execute(
        "SELECT header, first_seen FROM alert WHERE alert_id='a1'"
        " ORDER BY first_seen").fetchall()
    assert len(rows) == 2, rows
    assert rows[0][0] == "South City line delays"
    assert rows[1][0] == "Onehunga West line suspended"
    a.close()
    print("alert revisioning ok (both wordings kept)")


def test_alert_bucket_change_is_not_a_revision():
    """upcoming -> now is the same alert, not a new revision.

    Night-works notices cross that boundary every single day. Hashing the
    bucket would mint a duplicate row per alert per day for ever.
    """
    a = _fresh()
    a.record_alerts({"upcoming": [_alert()], "generated": 1795000000})
    a.record_alerts({"now": [_alert()], "generated": 1795010000})
    rows = a._db.execute("SELECT bucket, seen FROM alert").fetchall()
    assert len(rows) == 1, rows
    assert rows[0] == ("now", 2), rows
    a.close()
    print("alert bucket roll ok (1 row, bucket updated)")


def test_alert_stale_payload_not_recorded():
    """A cached/stale payload is not evidence AT still published the alert."""
    a = _fresh()
    a.record_alerts({"now": [_alert()], "generated": 1795000000})
    before = a._db.execute("SELECT last_seen FROM alert").fetchone()[0]
    assert a.record_alerts({"now": [_alert()], "stale": True,
                            "error": "timeout",
                            "generated": 1795009999}) == 0
    after = a._db.execute("SELECT last_seen FROM alert").fetchone()[0]
    assert after == before, "stale payload moved last_seen"
    a.close()
    print("stale alert payload ignored ok")


def test_alert_poll_failure_recorded():
    """An alerts outage is distinguishable from a quiet network."""
    a = _fresh()
    a.record_alerts({"now": [], "upcoming": [], "error": "503",
                     "total_feed": 0, "generated": 1795000000})
    row = a._db.execute(
        "SELECT ok, rail_now, error FROM alert_poll").fetchone()
    assert row == (0, 0, "503"), row
    a.close()
    print("alert poll failure row ok")


def test_alerts_at_window():
    """alerts_at finds alerts live in an hour, and excludes ones that are not."""
    a = _fresh()
    a.record_alerts({"now": [_alert("live")], "generated": 1795000000})
    a.record_alerts({"now": [_alert("live")], "generated": 1795003600})
    a.record_alerts({"now": [_alert("old")], "generated": 1794000000})
    hit = a.alerts_at(1795000000, 3600)
    ids = {x["id"] for x in hit}
    assert "live" in ids and "old" not in ids, ids
    a.close()
    print("alerts_at window ok")


def test_epoch_for_is_timezone_fixed():
    """Hour buckets must not depend on the host timezone.

    The container runs UTC; time.mktime() would place a 16:00 NZDT bucket
    13 hours away from the rows it describes and attach the wrong alerts.
    """
    # 2026-11-25 16:00 NZDT == 03:00 UTC the same day.
    got = archive._epoch_for("2026-11-25", 16)
    assert time.strftime("%Y-%m-%d %H:%M", time.gmtime(got)) == \
        "2026-11-25 03:00", time.gmtime(got)
    print("epoch_for tz-fixed ok")


# --------------------------------------------------------------- day summary

def test_day_summary_flags_bad_hour_with_cause():
    """A delay spike is reported WITH the alert that explains it."""
    a = _fresh()
    day = "2026-11-25"
    # Calm baseline: 60 s delay across hours 5..14.
    for h in range(5, 15):
        ts = archive._epoch_for(day, h)
        a.record(_payload([_train(delay_s=60)], ts=ts))
    # One bad hour at 16:00.
    bad = archive._epoch_for(day, 16)
    a.record(_payload([_train(delay_s=900)], ts=bad))
    a.record_alerts({"now": [_alert(header="South City line delays")],
                     "generated": bad + 60})

    s = a.day_summary(day)
    assert s["enabled"] and not s.get("error"), s
    flagged = s["days"][0]["flagged_hours"]
    assert [f["hour"] for f in flagged] == [16], flagged
    assert flagged[0]["alerts"], "bad hour has no cause attached"
    assert "South City" in flagged[0]["alerts"][0]["header"]
    a.close()
    print("day summary flags bad hour with cause ok")


def test_day_summary_clips_both_days_to_same_hours():
    """An in-progress day must not be compared against a full one."""
    a = _fresh()
    for h in range(5, 20):           # yesterday: runs to 19:00
        a.record(_payload([_train(delay_s=60)],
                          ts=archive._epoch_for("2026-11-24", h)))
    for h in range(5, 10):           # today: only to 09:00
        a.record(_payload([_train(delay_s=60)],
                          ts=archive._epoch_for("2026-11-25", h)))
    s = a.day_summary("2026-11-25", "2026-11-24")
    assert s["clipped_to_hour"] == 9, s["clipped_to_hour"]
    # Both days must then cover the same hour count.
    assert len(s["days"][0]["hours"]) == len(s["days"][1]["hours"]), \
        [len(d["hours"]) for d in s["days"]]
    a.close()
    print("day summary clipping ok (both clipped to <9)")


def test_day_summary_quiet_day_flags_nothing():
    """The flag needs BOTH 3x and 180s, so normal variation stays quiet."""
    a = _fresh()
    day = "2026-11-24"
    for h in range(5, 20):
        # 40s..130s: >3x spread, but nothing near 180s.
        a.record(_payload([_train(delay_s=40 + h * 6)],
                          ts=archive._epoch_for(day, h)))
    s = a.day_summary(day)
    assert s["days"][0]["flagged_hours"] == [], s["days"][0]["flagged_hours"]
    assert "note" in str(s["days"][0].get("flagged_hours_note", "")).lower() \
        or s["days"][0].get("flagged_hours_note")
    a.close()
    print("quiet day flags nothing ok")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
    print(f"\nall {len(fns)} archive tests passed")
