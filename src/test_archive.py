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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
    print(f"\nall {len(fns)} archive tests passed")
