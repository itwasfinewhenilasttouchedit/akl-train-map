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

import calendar
import hashlib
import json
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

-- Station names, normalised. `from`/`to`/`near` repeat the same ~48 strings on
-- every one of ~300k daily rows; a dimension makes storing them nearly free.
CREATE TABLE IF NOT EXISTS place(
  place_pk INTEGER PRIMARY KEY,
  name     TEXT UNIQUE NOT NULL);

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
  -- Added later; every one of these was polled and discarded. See MIGRATIONS.
  occ        INTEGER,   -- GTFS occupancy_status enum 0-6
  from_pk    INTEGER,   -- station just departed  -> place
  to_pk      INTEGER,   -- station next due       -> place
  plat       TEXT,      -- platform at the next call
  eta_s      INTEGER,   -- seconds to the next call, as predicted at `ts`
  near_pk    INTEGER,   -- nearest station for out-of-service stock -> place
  near_m     INTEGER,
  skipped    TEXT,      -- JSON list of skipped stations, NULL when none
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
  error   TEXT,
  -- Feed-health counters the poller already computes. `trains` alone cannot
  -- tell a quiet network from a degrading feed: on 2026-11-25 polls kept
  -- returning ok=1 while vehicles_in_feed collapsed and poll_ms hit 25 s.
  veh_feed     INTEGER,  -- vehicles in the raw realtime feed (whole network)
  trip_upd     INTEGER,  -- tripupdates entities
  with_pred    INTEGER,  -- trains carrying a live prediction
  stale_drop   INTEGER,  -- fixes dropped as too old
  unknown_trip INTEGER,  -- vehicles whose trip_id is not in the baked timetable
  in_svc       INTEGER,
  oos          INTEGER,
  source       TEXT);

-- ---- SERVICE ALERTS ------------------------------------------------------
-- AT's `servicealerts` feed was fetched for the live map and then thrown away,
-- so the archive could show a 15x delay spike and not say why. It is kept as
-- (alert_id, rev) rows: `rev` hashes the mutable content, so AT editing an
-- alert's text appends a revision instead of overwriting the original wording.
-- `first_seen`/`last_seen` are what date an incident — the alert text itself
-- only ever says "an earlier incident".
CREATE TABLE IF NOT EXISTS alert(
  alert_pk   INTEGER PRIMARY KEY,
  alert_id   TEXT NOT NULL,
  rev        TEXT NOT NULL,
  bucket     TEXT,
  header     TEXT, description TEXT,
  effect     TEXT, effect_detail TEXT, cause TEXT, severity TEXT,
  routes     TEXT, stations TEXT,
  starts     INTEGER, ends INTEGER,
  first_seen INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL,
  seen       INTEGER NOT NULL DEFAULT 1,
  UNIQUE(alert_id, rev));

CREATE INDEX IF NOT EXISTS alert_seen ON alert(last_seen);

-- Same reasoning as `poll`: an alerts outage must be distinguishable from a
-- genuinely quiet network, or a disruption with no alert row looks like a
-- disruption AT never announced.
CREATE TABLE IF NOT EXISTS alert_poll(
  ts         INTEGER PRIMARY KEY,
  ok         INTEGER NOT NULL,
  total_feed INTEGER,
  rail_now   INTEGER,
  rail_soon  INTEGER,
  error      TEXT);

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

-- ---- DERIVED VIEWS (no stored data changes; safe to add/drop) -------------
-- Vocabulary: a UNIT is the 3-car set, "AM 701"; its cars are AMP/AMT/AMA 701.
-- A TRAIN (service) is one unit, or two coupled units when consist = 6.
-- The feed reports ONE label per set ("AMP 701") and `partner_pk` points at a
-- bare-number twin ("402") that never has its own rows, so identity is the
-- number alone. These views normalise to it.
CREATE VIEW IF NOT EXISTS v_obs_unit AS
SELECT o.ts, CAST(trim(u.unit,'ABCDEFGHIJKLMNOPQRSTUVWXYZ ') AS INTEGER) AS unit_no,
       CAST(trim(p.unit,'ABCDEFGHIJKLMNOPQRSTUVWXYZ ') AS INTEGER) AS partner_no,
       o.trip_pk, o.lat, o.lon, o.bearing, o.speed_dkmh, o.delay_s,
       o.fix_age_s, o.consist, o.pos, o.c_src, o.o_src,
       o.occ, pf.name AS from_stn, pt.name AS to_stn, o.plat, o.eta_s,
       pn.name AS near_stn, o.near_m, o.skipped
FROM obs o JOIN unit u USING(unit_pk) LEFT JOIN unit p ON p.unit_pk=o.partner_pk
LEFT JOIN place pf ON pf.place_pk=o.from_pk
LEFT JOIN place pt ON pt.place_pk=o.to_pk
LEFT JOIN place pn ON pn.place_pk=o.near_pk;

-- One row per TRAIN per poll. A 6-car is reported by both halves (each has its
-- own GPS); keep the lower-numbered unit's row so the pair counts once, and
-- take the trip/delay from whichever half has one. Joins go through the
-- (unit_pk, ts) primary key: joining on the computed unit_no is a full scan
-- per row and does not finish on 300k rows.
CREATE VIEW IF NOT EXISTS v_train AS
SELECT a.ts,
       CASE WHEN a.partner_no IS NULL THEN 'AM '||a.unit_no
            ELSE 'AM '||a.unit_no||' + AM '||a.partner_no END AS service_units,
       a.unit_no, a.partner_no,
       COALESCE(a.consist, 3) AS n_cars,
       COALESCE(a.trip_pk, b.trip_pk) AS trip_pk,
       a.lat, a.lon, a.bearing, a.speed_dkmh,
       COALESCE(a.delay_s, b.delay_s) AS delay_s, a.fix_age_s,
       COALESCE(a.occ, b.occ) AS occ,
       pf.name AS from_stn, pt.name AS to_stn,
       COALESCE(a.plat, b.plat) AS plat,
       COALESCE(a.eta_s, b.eta_s) AS eta_s,
       COALESCE(a.skipped, b.skipped) AS skipped
FROM (SELECT o.*, CAST(substr(u.unit,5) AS INTEGER) AS unit_no,
             CAST(trim(p.unit) AS INTEGER) AS partner_no
      FROM obs o JOIN unit u USING(unit_pk)
      LEFT JOIN unit p ON p.unit_pk=o.partner_pk) a
LEFT JOIN unit bu ON bu.unit = 'AMP '||a.partner_no
LEFT JOIN obs b ON b.unit_pk=bu.unit_pk AND b.ts=a.ts
LEFT JOIN place pf ON pf.place_pk=COALESCE(a.from_pk, b.from_pk)
LEFT JOIN place pt ON pt.place_pk=COALESCE(a.to_pk, b.to_pk)
WHERE a.partner_no IS NULL OR a.unit_no < a.partner_no;

-- Hour-by-hour delay with the alerts that were in force, so "why was 16:00
-- bad" is one query instead of an afternoon of correlation by hand. Local time
-- is NZDT-fixed (+13): good enough for bucketing, and the raw ts is still there.
CREATE VIEW IF NOT EXISTS v_hour AS
SELECT date(ts,'unixepoch','+13 hours')                            AS day,
       CAST(strftime('%H', ts, 'unixepoch', '+13 hours') AS INTEGER) AS hour,
       count(*)                                                    AS fixes,
       count(DISTINCT unit_pk)                                     AS units,
       count(DISTINCT trip_pk)                                     AS trips,
       round(avg(delay_s), 1)                                      AS avg_delay_s,
       max(delay_s)                                                AS max_delay_s,
       round(100.0 * sum(CASE WHEN delay_s > 300 THEN 1 ELSE 0 END)
             / nullif(count(delay_s), 0), 2)                       AS pct_over_5min,
       round(avg(fix_age_s), 1)                                    AS avg_fix_age_s
FROM obs GROUP BY day, hour;
"""


class Archive:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else _path()
        self.rows = 0
        self.polls = 0
        self.alerts_seen = 0
        self.last_error: str | None = None
        self._units: dict[str, int] = {}
        self._trips: dict[str, int] = {}
        self._places: dict[str, int] = {}
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
        self._migrate()
        self._load_dims()
        LOG.info("archive: %s (%d units, %d trips, %d places known, %d obs)",
                 self.path, len(self._units), len(self._trips),
                 len(self._places), self.count())

    def _migrate(self) -> None:
        """Add columns to tables that already exist.

        `CREATE TABLE IF NOT EXISTS` is a no-op on the live 31 MB database, so
        new columns in SCHEMA would never appear there — the writer would then
        fail on every insert. ALTER TABLE ADD COLUMN is cheap in SQLite (a
        metadata-only change, no table rewrite) and new columns read NULL for
        every existing row, which is the honest value: we genuinely did not
        record them at the time. Views are dropped and recreated instead, since
        a view's text is fixed at creation and IF NOT EXISTS would keep the old
        definition for ever.
        """
        assert self._db is not None
        want = {
            "obs": [("occ", "INTEGER"), ("from_pk", "INTEGER"),
                    ("to_pk", "INTEGER"), ("plat", "TEXT"),
                    ("eta_s", "INTEGER"), ("near_pk", "INTEGER"),
                    ("near_m", "INTEGER"), ("skipped", "TEXT")],
            "poll": [("veh_feed", "INTEGER"), ("trip_upd", "INTEGER"),
                     ("with_pred", "INTEGER"), ("stale_drop", "INTEGER"),
                     ("unknown_trip", "INTEGER"), ("in_svc", "INTEGER"),
                     ("oos", "INTEGER"), ("source", "TEXT")],
        }
        added = 0
        for table, cols in want.items():
            have = {r[1] for r in self._db.execute(
                f"PRAGMA table_info({table})")}
            if not have:
                continue                      # table did not exist; SCHEMA made it
            for name, typ in cols:
                if name not in have:
                    self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                    added += 1
        if added:
            # Views were created against the old column list.
            for v in ("v_obs_unit", "v_train", "v_hour"):
                self._db.execute(f"DROP VIEW IF EXISTS {v}")
            self._db.commit()
            self._db.executescript(SCHEMA)
            LOG.info("archive: migrated, %d columns added, views rebuilt", added)
        self._db.commit()

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
        for pk, n in self._db.execute("SELECT place_pk, name FROM place"):
            self._places[n] = pk

    # -- dimension upserts -------------------------------------------------
    def _place_pk(self, name: str | None) -> int | None:
        if not name:
            return None
        pk = self._places.get(name)
        if pk is None:
            assert self._db is not None
            cur = self._db.execute(
                "INSERT OR IGNORE INTO place(name) VALUES(?)", (name,))
            pk = cur.lastrowid
            if not pk:
                row = self._db.execute(
                    "SELECT place_pk FROM place WHERE name=?", (name,)).fetchone()
                pk = row[0] if row else None
            if pk:
                self._places[name] = pk
        return pk

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
                skipped = t.get("skipped_stops") or None
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
                    self._i(t.get("occupancy")),
                    self._place_pk(t.get("from")),
                    self._place_pk(t.get("to")),
                    t.get("platform"),
                    self._i(t.get("eta_s")),
                    self._place_pk(t.get("near")),
                    self._i(t.get("near_m")),
                    # JSON, not a joined dimension: skipped stops are rare, a
                    # variable-length list, and only ever read as a whole.
                    json.dumps(skipped, separators=(",", ":")) if skipped else None,
                ))
            if rows:
                # One batched write per poll, not one per train: flash writes.
                self._db.executemany(
                    "INSERT OR REPLACE INTO obs VALUES(" + ",".join("?" * 24) + ")",
                    rows)
            self._db.execute(
                "INSERT OR REPLACE INTO poll(ts, ok, trains, poll_ms, clock_ok,"
                " error, veh_feed, trip_upd, with_pred, stale_drop,"
                " unknown_trip, in_svc, oos, source)"
                " VALUES(" + ",".join("?" * 14) + ")",
                (ts, 1, len(rows), self._i(payload.get("poll_ms")),
                 1 if clock_synced() else 0, None,
                 self._i(payload.get("vehicles_in_feed")),
                 self._i(payload.get("trip_updates")),
                 self._i(payload.get("with_prediction")),
                 self._i(payload.get("stale_dropped")),
                 self._i(payload.get("unknown_trip")),
                 self._i(payload.get("in_service")),
                 self._i(payload.get("out_of_service")),
                 payload.get("source")))
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

    # -- service alerts ----------------------------------------------------
    @staticmethod
    def _rev(a: dict) -> str:
        """Content hash of the mutable parts of an alert.

        AT edits alerts in place: the same `id` acquires new wording as an
        incident develops ("delays" becoming "suspended until end of service").
        Keying only on id would overwrite the earlier wording and destroy the
        timeline; keying on (id, rev) appends a revision so the progression is
        recoverable. `bucket` is deliberately NOT hashed — a night-works alert
        rolling from upcoming to now is the same revision, and including it
        would mint a duplicate row for every alert, every day.
        """
        blob = json.dumps([a.get("header"), a.get("description"),
                           a.get("effect"), a.get("effect_detail"),
                           a.get("cause"), a.get("severity"),
                           a.get("routes"), a.get("stations"),
                           a.get("starts"), a.get("ends")],
                          sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]

    def record_alerts(self, payload: dict, ts: int | None = None) -> int:
        """Archive one alerts poll. Returns revisions newly inserted.

        Called on the same cadence the live map already fetches alerts, so it
        costs no extra AT quota. Like record(), it never raises into the caller:
        the alerts endpoint must keep serving the map if the archive is sick.
        """
        if self._db is None:
            return 0
        try:
            now = int(ts or payload.get("generated") or time.time())
            err = payload.get("error")
            # A served-from-cache payload is not a fresh observation. Recording
            # it would fabricate `last_seen` evidence that AT still published
            # the alert at this instant, when in fact we never asked.
            if payload.get("stale"):
                return 0
            buckets = {"now": payload.get("now") or [],
                       "upcoming": payload.get("upcoming") or []}
            new = 0
            for bucket, items in buckets.items():
                for a in items:
                    aid = str(a.get("id") or "")
                    if not aid:
                        continue
                    rev = self._rev(a)
                    cur = self._db.execute(
                        "UPDATE alert SET last_seen=?, seen=seen+1, bucket=?"
                        " WHERE alert_id=? AND rev=?", (now, bucket, aid, rev))
                    if cur.rowcount:
                        continue
                    self._db.execute(
                        "INSERT OR IGNORE INTO alert(alert_id, rev, bucket,"
                        " header, description, effect, effect_detail, cause,"
                        " severity, routes, stations, starts, ends,"
                        " first_seen, last_seen, seen)"
                        " VALUES(" + ",".join("?" * 16) + ")",
                        (aid, rev, bucket, a.get("header"), a.get("description"),
                         a.get("effect"), a.get("effect_detail"), a.get("cause"),
                         a.get("severity"),
                         ",".join(a.get("routes") or []) or None,
                         ",".join(a.get("stations") or []) or None,
                         a.get("starts"), a.get("ends"), now, now, 1))
                    new += 1
            self._db.execute(
                "INSERT OR REPLACE INTO alert_poll(ts, ok, total_feed,"
                " rail_now, rail_soon, error) VALUES(?,?,?,?,?,?)",
                (now, 0 if err else 1, self._i(payload.get("total_feed")),
                 len(buckets["now"]), len(buckets["upcoming"]),
                 str(err)[:500] if err else None))
            self._db.commit()
            self.alerts_seen += new
            return new
        except Exception as exc:  # noqa: BLE001
            LOG.warning("archive: alert write failed: %s", exc)
            try:
                self._db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return 0

    def alerts_at(self, ts: int, window_s: int = 3600) -> list[dict]:
        """Alerts whose observed lifetime overlaps [ts, ts+window_s).

        The join that answers "why was 16:00 bad". Uses first_seen/last_seen —
        when WE saw it — not the alert's own `starts`, because open-ended
        incident alerts frequently carry no active_period at all.
        """
        if self._db is None:
            return []
        try:
            cur = self._db.execute(
                "SELECT alert_id, bucket, header, description, effect, cause,"
                " severity, routes, stations, first_seen, last_seen, seen"
                " FROM alert WHERE first_seen < ? AND last_seen >= ?"
                " ORDER BY first_seen", (ts + window_s, ts))
            cols = ["id", "bucket", "header", "description", "effect", "cause",
                    "severity", "routes", "stations", "first_seen", "last_seen",
                    "seen"]
            return [dict(zip(cols, r)) for r in cur]
        except Exception:  # noqa: BLE001
            return []

    # -- reads -------------------------------------------------------------
    TZ = "+13 hours"   # NZDT. Bucketing only; `ts` is always the truth.

    def _day_rows(self, day: str, max_hour: int) -> dict:
        """Punctuality aggregates for one local day, clipped to < max_hour."""
        assert self._db is not None
        d = f"date(ts,'unixepoch','{self.TZ}')"
        h = f"CAST(strftime('%H',ts,'unixepoch','{self.TZ}') AS INTEGER)"
        where = f"WHERE {d}=? AND {h}<?"
        args = (day, max_hour)
        n, trips, units, fixage, top = self._db.execute(
            f"SELECT count(*), count(DISTINCT trip_pk), count(DISTINCT unit_pk),"
            f" round(avg(fix_age_s),1), max(speed_dkmh) FROM obs {where}",
            args).fetchone()
        delays = [r[0] for r in self._db.execute(
            f"SELECT delay_s FROM obs {where} AND delay_s IS NOT NULL", args)]
        delays.sort()
        def pct(p: float):
            if not delays:
                return None
            return delays[min(len(delays) - 1, int(p * len(delays)))]
        hours = [{"hour": r[0], "fixes": r[1],
                  "avg_delay_s": round(r[2], 1) if r[2] is not None else None}
                 for r in self._db.execute(
                     f"SELECT {h}, count(*), avg(delay_s) FROM obs {where}"
                     f" GROUP BY 1 ORDER BY 1", args)]
        routes = [{"route": r[0], "fixes": r[1], "avg_delay_s": r[2],
                   "pct_over_5min": r[3]}
                  for r in self._db.execute(
                      f"SELECT t.route, count(*), round(avg(o.delay_s),1),"
                      f" round(100.0*sum(CASE WHEN o.delay_s>300 THEN 1 ELSE 0 END)"
                      f" /nullif(count(o.delay_s),0),2)"
                      f" FROM obs o JOIN trip t USING(trip_pk)"
                      f" WHERE date(o.ts,'unixepoch','{self.TZ}')=?"
                      f" AND CAST(strftime('%H',o.ts,'unixepoch','{self.TZ}')"
                      f" AS INTEGER)<? GROUP BY 1 ORDER BY 3 DESC", args)]
        polls, ok, pavg, pmax = self._db.execute(
            f"SELECT count(*), sum(ok), round(avg(poll_ms)), max(poll_ms)"
            f" FROM poll {where}", args).fetchone()
        return {
            "day": day, "fixes": n, "trips": trips, "units": units,
            "avg_fix_age_s": fixage,
            "top_speed_kmh": (top / 10.0) if top else None,
            "delay": {
                "n": len(delays),
                "avg_s": round(sum(delays) / len(delays), 1) if delays else None,
                "median_s": pct(0.5), "p90_s": pct(0.9), "p99_s": pct(0.99),
                "pct_over_5min": (round(100.0 * sum(1 for x in delays if x > 300)
                                        / len(delays), 2) if delays else None),
            },
            "hours": hours, "routes": routes,
            "polls": {"n": polls or 0, "ok": ok or 0,
                      "failed": (polls or 0) - (ok or 0),
                      "avg_ms": pavg, "max_ms": pmax},
        }

    def day_summary(self, day: str | None = None,
                    vs: str | None = None) -> dict:
        """One day's punctuality, optionally compared with another.

        Flagged hours carry the alerts that were live in them. A flag is
        `avg_delay >= 3x the day's median hourly average AND >= 180 s`: a
        purely relative test fires on a calm day where 40 s became 130 s, and
        a purely absolute one misses a genuinely unusual morning.
        """
        if self._db is None:
            return {"enabled": False}
        try:
            today = time.strftime(
                "%Y-%m-%d", time.gmtime(time.time() + 13 * 3600))
            day = day or today
            days = [day] + ([vs] if vs else [])
            # Clip both days to the hours BOTH have, or an in-progress day is
            # unfairly compared with a complete one. The partial final hour is
            # excluded too — it is a few minutes of data masquerading as an
            # hour. With no comparison day there is nothing to be unfair to,
            # so a single-day report is never clipped.
            max_hour = 24
            if vs:
                for d in days:
                    r = self._db.execute(
                        f"SELECT max(CAST(strftime('%H',ts,'unixepoch','{self.TZ}')"
                        f" AS INTEGER)) FROM obs"
                        f" WHERE date(ts,'unixepoch','{self.TZ}')=?",
                        (d,)).fetchone()
                    if r and r[0] is not None:
                        max_hour = min(max_hour, int(r[0]))
            out = {"enabled": True, "clipped_to_hour": max_hour,
                   "timezone": "NZDT (+13)",
                   "days": [self._day_rows(d, max_hour) for d in days]}
            # Attach causes to the primary day's abnormal hours.
            prim = out["days"][0]
            avgs = sorted(x["avg_delay_s"] for x in prim["hours"]
                          if x["avg_delay_s"] is not None)
            flagged = []
            if avgs:
                med = avgs[len(avgs) // 2]
                for hr in prim["hours"]:
                    av = hr["avg_delay_s"]
                    if av is None or av < 180 or av < 3 * med:
                        continue
                    start = _epoch_for(prim["day"], hr["hour"])
                    hr = dict(hr)
                    hr["alerts"] = [
                        {k: a[k] for k in ("header", "effect", "cause",
                                           "routes", "first_seen", "last_seen")}
                        for a in self.alerts_at(start, 3600)]
                    flagged.append(hr)
                prim["median_hourly_avg_s"] = med
            prim["flagged_hours"] = flagged
            if not flagged:
                prim["flagged_hours_note"] = "no hour met the 3x-and-180s test"
            elif not any(h["alerts"] for h in flagged):
                prim["flagged_hours_note"] = (
                    "no archived alert overlaps these hours; alert recording "
                    "began 2026-11-25, so earlier days have no causes stored")
            return out
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}

    def day_series(self, n: int = 14) -> list[dict]:
        """Per-local-day activity for the last `n` days that HAVE data.

        WHY THIS IS NOT `day_summary` CALLED IN A LOOP
        ---------------------------------------------
        `day_summary` is a punctuality report: several full passes over one
        day's rows plus a delay-percentile sort. Fourteen of those is fourteen
        times the work for a sparkline. This is ONE grouped pass, and it asks
        only what a history strip can honestly show.

        Days with no rows are absent rather than reported as zero: "we were not
        recording" and "nothing ran" are different claims, and the archive
        cannot tell them apart (see `poll` for the one that can).
        """
        if self._db is None:
            return []
        try:
            d = f"date(ts,'unixepoch','{self.TZ}')"
            rows = self._db.execute(
                f"SELECT {d} AS day, count(*), count(DISTINCT unit_pk),"
                f" count(DISTINCT trip_pk), max(speed_dkmh), min(ts), max(ts)"
                f" FROM obs GROUP BY 1 ORDER BY 1 DESC LIMIT ?",
                (max(1, int(n)),)).fetchall()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("archive: day_series failed: %s", exc)
            return []
        out = [{"day": r[0], "fixes": r[1], "units": r[2], "trips": r[3],
                "top_speed_kmh": (r[4] / 10.0) if r[4] else None,
                "first_ts": r[5], "last_ts": r[6]} for r in rows]
        out.reverse()          # oldest first: a history strip reads left to right
        return out

    def unit_stats(self, labels: list[str]) -> dict:
        """Everything the archive can honestly say about ONE set.

        `labels` is the caller's list of feed labels for the set -- the fold
        from label to set number lives in `feedback.set_number` and nowhere
        else, so this method is told which labels to union rather than
        re-deriving the rule (the duplicated-rule mistake this codebase has
        already paid for once).

        EVERY FIGURE HERE IS A COUNT OR AN EXTREME OF STORED ROWS. Nothing is
        modelled, estimated or annualised. In particular:
          * speed comes from `speed_dkmh/10` with `fix_age_s <= 60`, because a
            stale fix attributes a speed to the wrong place and time;
          * the top speed is returned with the coupled partner's speed at the
            SAME second when there is one, because a single GPS outlier is
            indistinguishable from a record until the other half agrees;
          * `coverage` is the archive's own span, so a 4-day window can never
            be read as a fleet lifetime.
        """
        if self._db is None:
            return {"enabled": False}
        pks = [self._units[l] for l in labels if l in self._units]
        if not pks:
            return {"enabled": True, "known": False, "labels": list(labels),
                    "obs": 0, "days": [], "days_active": 0,
                    "top_speed": None, "coverage": self._coverage()}
        q = ",".join("?" * len(pks))
        try:
            n, t0, t1, nt = self._db.execute(
                f"SELECT count(*), min(ts), max(ts), count(DISTINCT trip_pk)"
                f" FROM obs WHERE unit_pk IN ({q})", pks).fetchone()
            d = f"date(ts,'unixepoch','{self.TZ}')"
            days = [{"day": r[0], "fixes": r[1],
                     "top_speed_kmh": (r[2] / 10.0) if r[2] else None,
                     "in_service_fixes": r[3],
                     "first_ts": r[4], "last_ts": r[5]}
                    for r in self._db.execute(
                        f"SELECT {d}, count(*), max(speed_dkmh),"
                        f" sum(CASE WHEN flags & {F_IN_SERVICE} THEN 1 ELSE 0 END),"
                        f" min(ts), max(ts)"
                        f" FROM obs WHERE unit_pk IN ({q})"
                        f" GROUP BY 1 ORDER BY 1", pks)]
            top = self._db.execute(
                f"SELECT o.ts, o.speed_dkmh, o.partner_pk, pn.name, pf.name,"
                f" pt.name, o.lat, o.lon"
                f" FROM obs o"
                f" LEFT JOIN place pn ON pn.place_pk=o.near_pk"
                f" LEFT JOIN place pf ON pf.place_pk=o.from_pk"
                f" LEFT JOIN place pt ON pt.place_pk=o.to_pk"
                f" WHERE o.unit_pk IN ({q}) AND o.fix_age_s IS NOT NULL"
                f" AND o.fix_age_s <= 60 AND o.speed_dkmh IS NOT NULL"
                f" ORDER BY o.speed_dkmh DESC LIMIT 1", pks).fetchone()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("archive: unit_stats failed: %s", exc)
            return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}

        top_out = None
        if top:
            # The coupled-pair cross-check. A partner reading the same speed in
            # the same second corroborates the record; a missing partner is
            # reported as such rather than as agreement.
            #
            # `obs.partner_pk` points at the BARE-NUMBER TWIN ("1236") that
            # `record()` interns, and that twin NEVER OWNS A ROW -- measured:
            # 164 rows carry a partner_pk and zero of those partners have an
            # obs row at the same ts. Looking the partner up by partner_pk
            # alone therefore finds nothing and the cross-check silently
            # becomes "no partner", which reads as "running alone" and quietly
            # drops the one validity test a speed record has.
            #
            # So the twin's number is resolved back to the REAL labels that
            # carry rows, using the same `trim(unit, 'A..Z ')` normalisation
            # `v_obs_unit` and `v_train` already use -- over the 186-row `unit`
            # table, so the obs lookup that follows is still a primary-key hit
            # on (unit_pk, ts).
            pspeed = plabel = None
            if top[2] is not None:
                _TRIM = "trim(unit,'ABCDEFGHIJKLMNOPQRSTUVWXYZ ')"
                cands = [r[0] for r in self._db.execute(
                    f"SELECT unit_pk FROM unit WHERE {_TRIM} ="
                    f" (SELECT {_TRIM} FROM unit WHERE unit_pk=?)"
                    f" AND unit_pk<>?", (top[2], top[2]))]
                for cpk in cands:
                    r = self._db.execute(
                        "SELECT u.unit, o.speed_dkmh FROM obs o JOIN unit u"
                        " USING(unit_pk) WHERE o.unit_pk=? AND o.ts=?",
                        (cpk, top[0])).fetchone()
                    if r and r[1] is not None:
                        plabel, pspeed = r[0], r[1] / 10.0
                        break
                if plabel is None:
                    # Name the partner even when it has no fix at that second:
                    # "coupled to X, but X has no fix then" is a different and
                    # more honest statement than "it was running alone".
                    r = self._db.execute(
                        "SELECT unit FROM unit WHERE unit_pk=?",
                        (top[2],)).fetchone()
                    if r:
                        plabel = r[0]
            top_out = {
                "kmh": top[1] / 10.0, "ts": top[0],
                "near": top[3], "from": top[4], "to": top[5],
                "lat": top[6], "lon": top[7],
                "partner": plabel, "partner_kmh": pspeed,
                "corroborated": (pspeed is not None
                                 and abs(pspeed - top[1] / 10.0) <= 5.0),
            }
        return {"enabled": True, "known": True, "labels": list(labels),
                "obs": n or 0, "first_ts": t0, "last_ts": t1,
                "trips": nt or 0, "days": days, "days_active": len(days),
                "top_speed": top_out, "coverage": self._coverage()}

    def _coverage(self) -> dict:
        """The archive's own window, so no page can imply more history than
        there is. Nothing prunes `obs`, so this only ever grows."""
        if self._db is None:
            return {"first_ts": None, "last_ts": None, "span_hours": None}
        try:
            t0, t1 = self._db.execute(
                "SELECT min(ts), max(ts) FROM obs").fetchone()
        except Exception:  # noqa: BLE001
            return {"first_ts": None, "last_ts": None, "span_hours": None}
        span = ((t1 - t0) / 3600.0) if (t0 and t1) else None
        return {"first_ts": t0, "last_ts": t1,
                "span_hours": round(span, 2) if span else None}

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
            arev, aids, a0, a1 = self._db.execute(
                "SELECT count(*), count(DISTINCT alert_id), min(first_seen),"
                " max(last_seen) FROM alert").fetchone()
            apolls, abad = self._db.execute(
                "SELECT count(*), sum(ok=0) FROM alert_poll").fetchone()
            return {
                "enabled": True, "path": str(self.path), "obs": n,
                "units": len(self._units), "trips": len(self._trips),
                "places": len(self._places),
                "polls": polls or 0, "failed_polls": bad or 0,
                "first_ts": t0, "last_ts": t1,
                "span_hours": round(span_h, 2),
                "bytes": size,
                "wal_bytes": wal_bytes,
                "bytes_per_obs": round(size / n, 1) if n else None,
                "mb_per_day": (round(size / 1e6 / (span_h / 24), 1)
                               if span_h > 0.25 else None),
                "session_rows": self.rows,
                "alerts": {"revisions": arev or 0, "distinct": aids or 0,
                           "first_seen": a0, "last_seen": a1,
                           "polls": apolls or 0, "failed_polls": abad or 0,
                           "session_new": self.alerts_seen},
                "last_error": self.last_error,
            }
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}

    def fleet(self) -> list[dict]:
        """Every unit label the archive has ever named, with its row span.

        Deliberately NOT folded to set numbers here. The `unit` table holds
        both real labels ("AMP 958") and the bare-number partner twins ("402")
        that `record()` interns for `obs.partner_pk` and that never get rows of
        their own -- measured on a 6-poll capture: 96 labels, 64 with
        observations, 32 bare twins whose numbers all duplicate a real label.
        Folding is the caller's job precisely because the set-number rule lives
        in exactly one place (`feedback.set_number`), and a second copy of it
        in here is the duplicated-rule mistake this project has already paid
        for once.

        LEFT JOIN, not an inner join: a label with zero observations is a real
        fact about the archive ("interned as a partner, never seen alone") and
        must not be silently dropped into looking like a unit that never
        existed.
        """
        if self._db is None:
            return []
        try:
            cur = self._db.execute(
                "SELECT u.unit, count(o.unit_pk), min(o.ts), max(o.ts)"
                " FROM unit u LEFT JOIN obs o USING(unit_pk)"
                " GROUP BY u.unit_pk ORDER BY u.unit")
            return [{"label": r[0], "obs": r[1] or 0,
                     "first_ts": r[2], "last_ts": r[3]} for r in cur]
        except Exception as exc:  # noqa: BLE001
            LOG.warning("archive: fleet query failed: %s", exc)
            return []

    def history(self, unit: str, limit: int = 200) -> list[dict]:
        """One unit over time — the query the primary key is shaped for."""
        if self._db is None:
            return []
        pk = self._units.get(unit)
        if pk is None:
            return []
        cur = self._db.execute(
            "SELECT o.ts, o.lat, o.lon, o.bearing, o.speed_dkmh, o.delay_s,"
            " o.fix_age_s, o.flags, o.consist, o.pos, o.occ, pf.name, pt.name,"
            " o.plat, o.eta_s, pn.name, o.near_m, o.skipped"
            " FROM obs o"
            " LEFT JOIN place pf ON pf.place_pk=o.from_pk"
            " LEFT JOIN place pt ON pt.place_pk=o.to_pk"
            " LEFT JOIN place pn ON pn.place_pk=o.near_pk"
            " WHERE o.unit_pk=? ORDER BY o.ts DESC LIMIT ?", (pk, int(limit)))
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
                "occupancy": r[10],
                "from": r[11], "to": r[12], "platform": r[13],
                "eta_s": r[14], "near": r[15], "near_m": r[16],
                "skipped_stops": json.loads(r[17]) if r[17] else None,
            })
        return out


def _epoch_for(day: str, hour: int) -> int:
    """Local NZDT (day, hour) -> UTC epoch seconds.

    Deliberately fixed-offset arithmetic rather than time.mktime(), which
    interprets the struct in the HOST's timezone. The container runs UTC, so
    mktime would place a "16:00" bucket 13 hours away from the rows it is
    meant to describe and silently attach the wrong alerts to it.
    """
    y, m, d = (int(x) for x in day.split("-"))
    return calendar.timegm((y, m, d, hour, 0, 0, 0, 0, 0)) - 13 * 3600


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
