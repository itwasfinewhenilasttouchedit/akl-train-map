"""Persistent observation archive: every fix we already pay AT for, kept.

WHY THIS EXISTS
---------------
The live map keeps nothing. `last_trains.json` is one snapshot overwritten
every poll; `order_latch.json` expires after 3 h and caps at 400 entries;
`feedback.db` holds user reports, not movement. That is RIGHT for a live map —
a position from an hour ago is worthless to it, and a latch that remembered
longer would serve confidently wrong order after a terminus reversal.

This module is the opposite concern: longitudinal network-performance analysis
needs exactly the history the live path is designed to throw away.

It costs ZERO extra AT quota. It subscribes to the poll the server already
makes. Adding a logger is free; adding an *instance* is not (see the quota note
in the at-open-transit-data skill).

THE COLUMN THAT JUSTIFIES THE WHOLE THING
-----------------------------------------
`fix_age_s`. AT's vehicle fixes are NOT simultaneous. At 76 km/h a 10 s skew
produces 211 m of phantom offset against a ~72 m real signal — this caused a
real ordering bug. Any later analysis comparing two vehicles' positions must
weight by fix age, and cannot if we did not record it. Archiving positions
without fix age would bake that error in permanently.

MEASURED, NOT ESTIMATED
-----------------------
262,800 rows (73 trains, 20 s, 20 h) loaded into real SQLite with realistic
per-poll variation, then VACUUMed: 59 B/row => 15 MB/day, 5.6 GB/year.
A wide denormalised schema measured 131 B/row (12.5 GB/year); normalising the
repeated trip/unit labels is a 2.2x win for no loss of fidelity.

MEASURED VS INFERRED
--------------------
`consist_source` and `order_source` are stored as small ints, not dropped. An
archive that cannot distinguish an AT-reported consist from a geometric
inference will be mined years later as if it were all measured.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

LOG = logging.getLogger("archive")

# Flags packed into one integer rather than four columns of 0/1.
F_IN_SERVICE = 1
F_MOVING = 2
F_CANCELLED = 4
F_PREDICTED = 8

# Small-int encodings. 0 is reserved for "absent" so a NULL and a real value
# are never confused after a schema migration.
POS = {"front": 1, "rear": 2}
SRC = {"at": 1, "inferred": 2, "latched": 3, "gps": 4, "schedule": 5}


def _path() -> Path:
    """Beside the other state, on the volume — never in the image."""
    env = os.environ.get("ARCHIVE_DB")
    if env:
        return Path(env)
    fb = os.environ.get("FEEDBACK_DB")
    if fb:
        return Path(fb).parent / "archive.db"
    return Path(__file__).resolve().parent.parent / "output" / "archive.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS unit(
  unit_pk INTEGER PRIMARY KEY,
  unit    TEXT UNIQUE NOT NULL);

CREATE TABLE IF NOT EXISTS trip(
  trip_pk  INTEGER PRIMARY KEY,
  trip_id  TEXT UNIQUE NOT NULL,
  route    TEXT, origin TEXT, terminus TEXT, dest TEXT);

CREATE TABLE IF NOT EXISTS obs(
  ts         INTEGER NOT NULL,
  unit_pk    INTEGER NOT NULL,
  trip_pk    INTEGER,
  lat REAL, lon REAL,
  bearing    INTEGER,
  speed_dkmh INTEGER,
  delay_s    INTEGER,
  progress   REAL,
  fix_age_s  INTEGER,
  flags      INTEGER NOT NULL DEFAULT 0,
  consist    INTEGER,
  partner_pk INTEGER,
  pos        INTEGER,
  c_src      INTEGER,
  o_src      INTEGER,
  PRIMARY KEY (unit_pk, ts)
) WITHOUT ROWID;

-- A failed poll is RECORDED, not left as a hole. Otherwise "no trains running"
-- and "the phone fell off the Wi-Fi" are indistinguishable in analysis, and on
-- this host the second will happen.
CREATE TABLE IF NOT EXISTS poll(
  ts      INTEGER PRIMARY KEY,
  ok      INTEGER NOT NULL,
  trains  INTEGER,
  poll_ms INTEGER,
  clock_ok INTEGER,
  error   TEXT);

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Archive:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else _path()
        self.rows = 0
        self.polls = 0
        self.last_error: str | None = None
        self._units: dict[str, int] = {}
        self._trips: dict[str, int] = {}
        self._db: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=10,
                             check_same_thread=False)
        # WAL + NORMAL: UFS flash has finite writes and a phone can lose power.
        # NORMAL risks the last transaction on a hard crash, never corruption —
        # and one lost 20 s poll in an archive is not worth fsync per write.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(SCHEMA)
        db.commit()
        self._db = db
        self._load_dims()
        LOG.info("archive: %s (%d units, %d trips known, %d obs)",
                 self.path, len(self._units), len(self._trips), self.count())

    def close(self) -> None:
        if self._db is not None:
            self._db.commit()
            self._db.close()
            self._db = None

    def _load_dims(self) -> None:
        assert self._db is not None
        for pk, u in self._db.execute("SELECT unit_pk, unit FROM unit"):
            self._units[u] = pk
        for pk, t in self._db.execute("SELECT trip_pk, trip_id FROM trip"):
            self._trips[t] = pk

    # -- dimension upserts -------------------------------------------------
    def _unit_pk(self, unit: str | None) -> int | None:
        if not unit:
            return None
        pk = self._units.get(unit)
        if pk is None:
            assert self._db is not None
            cur = self._db.execute(
                "INSERT OR IGNORE INTO unit(unit) VALUES(?)", (unit,))
            pk = cur.lastrowid
            if not pk:  # raced or already present
                row = self._db.execute(
                    "SELECT unit_pk FROM unit WHERE unit=?", (unit,)).fetchone()
                pk = row[0] if row else None
            if pk:
                self._units[unit] = pk
        return pk

    def _trip_pk(self, t: dict) -> int | None:
        tid = t.get("trip_id")
        if not tid:
            return None
        pk = self._trips.get(tid)
        if pk is None:
            assert self._db is not None
            cur = self._db.execute(
                "INSERT OR IGNORE INTO trip(trip_id, route, origin, terminus, dest)"
                " VALUES(?,?,?,?,?)",
                (tid, t.get("route"), t.get("origin"), t.get("terminus"),
                 t.get("dest")))
            pk = cur.lastrowid
            if not pk:
                row = self._db.execute(
                    "SELECT trip_pk FROM trip WHERE trip_id=?", (tid,)).fetchone()
                pk = row[0] if row else None
            if pk:
                self._trips[tid] = pk
        return pk

    # -- the write path ----------------------------------------------------
    @staticmethod
    def _flags(t: dict) -> int:
        f = 0
        if t.get("in_service"):
            f |= F_IN_SERVICE
        if t.get("moving"):
            f |= F_MOVING
        if t.get("cancelled"):
            f |= F_CANCELLED
        if t.get("predicted"):
            f |= F_PREDICTED
        return f

    @staticmethod
    def _i(v, scale: float = 1.0):
        if v is None:
            return None
        try:
            return int(round(float(v) * scale))
        except (TypeError, ValueError):
            return None

    def record(self, payload: dict) -> int:
        """Archive one poll's trains. Returns rows written.

        Never raises into the poller: an archive failure must not take the live
        map down with it. The live map is the product; this is a side effect.
        """
        if self._db is None:
            return 0
        try:
            ts = int(payload.get("ts") or time.time())
            trains = payload.get("trains") or []
            rows = []
            for t in trains:
                upk = self._unit_pk(t.get("unit"))
                if upk is None:
                    continue  # no unit label = nothing to key history on
                rows.append((
                    ts, upk, self._trip_pk(t),
                    t.get("lat"), t.get("lon"),
                    self._i(t.get("bearing")),
                    self._i(t.get("speed_kmh"), 10),
                    self._i(t.get("delay_s")),
                    t.get("progress"),
                    self._i(t.get("fix_age_s")),
                    self._flags(t),
                    self._i(t.get("consist")),
                    self._unit_pk(t.get("partner_set")),
                    POS.get(t.get("consist_position")),
                    SRC.get(t.get("consist_source")),
                    SRC.get(t.get("order_source")),
                ))
            if rows:
                # One batched write per poll, not one per train: flash writes.
                self._db.executemany(
                    "INSERT OR REPLACE INTO obs VALUES(" + ",".join("?" * 16) + ")",
                    rows)
            self._db.execute(
                "INSERT OR REPLACE INTO poll(ts, ok, trains, poll_ms, clock_ok, error)"
                " VALUES(?,?,?,?,?,?)",
                (ts, 1, len(rows), self._i(payload.get("poll_ms")),
                 1 if clock_synced() else 0, None))
            self._db.commit()
            self.rows += len(rows)
            self.polls += 1
            self.last_error = None
            return len(rows)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            LOG.warning("archive: write failed: %s", self.last_error)
            try:
                if self._db is not None:
                    self._db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return 0

    def record_failure(self, error: str, ts: int | None = None) -> None:
        """Record that a poll failed, so gaps are explicit in the data."""
        if self._db is None:
            return
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO poll(ts, ok, trains, poll_ms, clock_ok, error)"
                " VALUES(?,?,?,?,?,?)",
                (int(ts or time.time()), 0, None, None,
                 1 if clock_synced() else 0, str(error)[:500]))
            self._db.commit()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("archive: failure-row write failed: %s", exc)

    # -- reads -------------------------------------------------------------
    def count(self) -> int:
        if self._db is None:
            return 0
        try:
            return self._db.execute("SELECT count(*) FROM obs").fetchone()[0]
        except Exception:  # noqa: BLE001
            return 0

    def stats(self) -> dict:
        if self._db is None:
            return {"enabled": False}
        try:
            n, t0, t1 = self._db.execute(
                "SELECT count(*), min(ts), max(ts) FROM obs").fetchone()
            polls, bad = self._db.execute(
                "SELECT count(*), sum(ok=0) FROM poll").fetchone()
            # In WAL mode the main .db file lags badly behind reality — rows
            # live in the -wal until a checkpoint. Reading only .st_size made
            # this report 4096 bytes after 1000 rows, and a bytes_per_obs that
            # *fell* as data grew. page_count*page_size sees the real table.
            pages, psize = (self._db.execute("PRAGMA page_count").fetchone()[0],
                            self._db.execute("PRAGMA page_size").fetchone()[0])
            size = pages * psize
            wal = self.path.with_suffix(self.path.suffix + "-wal")
            wal_bytes = wal.stat().st_size if wal.exists() else 0
            span_h = ((t1 - t0) / 3600.0) if (t0 and t1) else 0.0
            return {
                "enabled": True, "path": str(self.path), "obs": n,
                "units": len(self._units), "trips": len(self._trips),
                "polls": polls or 0, "failed_polls": bad or 0,
                "first_ts": t0, "last_ts": t1,
                "span_hours": round(span_h, 2),
                "bytes": size,
                "wal_bytes": wal_bytes,
                "bytes_per_obs": round(size / n, 1) if n else None,
                "mb_per_day": (round(size / 1e6 / (span_h / 24), 1)
                               if span_h > 0.25 else None),
                "session_rows": self.rows,
                "last_error": self.last_error,
            }
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}

    def history(self, unit: str, limit: int = 200) -> list[dict]:
        """One unit over time — the query the primary key is shaped for."""
        if self._db is None:
            return []
        pk = self._units.get(unit)
        if pk is None:
            return []
        cur = self._db.execute(
            "SELECT ts, lat, lon, bearing, speed_dkmh, delay_s, fix_age_s,"
            " flags, consist, pos FROM obs WHERE unit_pk=?"
            " ORDER BY ts DESC LIMIT ?", (pk, int(limit)))
        out = []
        for r in cur:
            out.append({
                "ts": r[0], "lat": r[1], "lon": r[2], "bearing": r[3],
                "speed_kmh": (r[4] / 10.0) if r[4] is not None else None,
                "delay_s": r[5], "fix_age_s": r[6],
                "in_service": bool(r[7] & F_IN_SERVICE),
                "moving": bool(r[7] & F_MOVING),
                "cancelled": bool(r[7] & F_CANCELLED),
                "consist": r[8],
                "position": {1: "front", 2: "rear"}.get(r[9]),
            })
        return out


def clock_synced() -> bool:
    """Is the host clock trustworthy?

    THIS MATTERS MORE THAN IT LOOKS. The S21 has no RTC and was once found 13 h
    out. Every row's `ts` comes from the host clock, so one timesync failure
    silently poisons the time axis of a dataset whose entire value IS the time
    axis. Recorded per poll so bad spans can be excluded later rather than
    discovered as inexplicable analysis results.
    """
    try:
        p = Path("/run/systemd/timesync/synchronized")
        if p.exists():
            return True
    except Exception:  # noqa: BLE001
        pass
    # No timesyncd marker (e.g. Fly): fall back to a sanity window rather than
    # claiming sync we cannot prove. 2026-01-01 .. 2038.
    return 1767182400 < time.time() < 2145916800


_ARCHIVE: Archive | None = None


def get() -> Archive | None:
    return _ARCHIVE


def init(path: Path | str | None = None) -> Archive | None:
    """Open the archive. Disabled by ARCHIVE_DB=off."""
    global _ARCHIVE
    if os.environ.get("ARCHIVE_DB", "").lower() in {"off", "0", "no", "false"}:
        LOG.info("archive: disabled by ARCHIVE_DB")
        return None
    a = Archive(path)
    try:
        a.open()
        _ARCHIVE = a
        return a
    except Exception as exc:  # noqa: BLE001
        LOG.warning("archive: could not open %s: %s", a.path, exc)
        _ARCHIVE = None
        return None
