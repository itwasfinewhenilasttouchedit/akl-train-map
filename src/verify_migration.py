"""End-to-end migration check against a COPY of the live archive database.

Not part of the unit suite: it needs a real archive.db, which only exists on
the deployment host. Run it there before deploying a schema change.

    docker cp akl-train-map:/data/archive.db /tmp/arch_copy.db   # via .backup()
    docker exec akl-train-map python /tmp/verify_migration.py /tmp/arch_copy.db

Why a copy and not the live file: opening the real database with a writer that
is about to ALTER TABLE, while the poller holds it open, is exactly the kind of
thing that should be proven on a throwaway first.
"""
from __future__ import annotations

import sqlite3
import sys

import archive

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/arch_copy.db"

before = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
n_obs = before.execute("SELECT count(*) FROM obs").fetchone()[0]
n_poll = before.execute("SELECT count(*) FROM poll").fetchone()[0]
obs_cols_before = [r[1] for r in before.execute("PRAGMA table_info(obs)")]
checksum = before.execute(
    "SELECT sum(delay_s), sum(ts), count(DISTINCT unit_pk) FROM obs").fetchone()
before.close()
print(f"before: {n_obs} obs, {n_poll} polls, {len(obs_cols_before)} obs columns")

a = archive.Archive(path)
a.open()

n_obs_after = a.count()
cols_after = [r[1] for r in a._db.execute("PRAGMA table_info(obs)")]
checksum_after = a._db.execute(
    "SELECT sum(delay_s), sum(ts), count(DISTINCT unit_pk) FROM obs").fetchone()

assert n_obs_after == n_obs, f"ROWS LOST: {n_obs} -> {n_obs_after}"
assert checksum_after == checksum, f"DATA CHANGED: {checksum} -> {checksum_after}"
added = set(cols_after) - set(obs_cols_before)
print(f"after:  {n_obs_after} obs, {len(cols_after)} obs columns, "
      f"added {sorted(added)}")

# The views must be queryable against the migrated table.
for v in ("v_obs_unit", "v_train", "v_hour"):
    k = a._db.execute(f"SELECT count(*) FROM {v}").fetchone()[0]
    print(f"view {v}: {k} rows")

# And the new-field write path must work on the real file.
import time
ts = int(time.time())
wrote = a.record({"ts": ts, "poll_ms": 500, "vehicles_in_feed": 1400,
                  "trip_updates": 900, "with_prediction": 60,
                  "stale_dropped": 1, "unknown_trip": 0, "in_service": 40,
                  "out_of_service": 50, "source": "verify",
                  "trains": [{"unit": "AMP 281", "lat": -36.8, "lon": 174.7,
                              "delay_s": 61, "fix_age_s": 7, "occupancy": 2,
                              "from": "Newmarket", "to": "Remuera",
                              "platform": "2", "eta_s": 120,
                              "in_service": True, "moving": True}]})
assert wrote == 1, wrote
row = a.history("AMP 281", limit=1)[0]
assert row["from"] == "Newmarket" and row["occupancy"] == 2, row
print(f"write path ok: {row['from']} -> {row['to']}, occ {row['occupancy']}")

# Clean up the probe row so the copy stays representative if reused.
a._db.execute("DELETE FROM obs WHERE ts=?", (ts,))
a._db.execute("DELETE FROM poll WHERE ts=?", (ts,))
a._db.commit()

d = a.day_summary()
print(f"day_summary: {d.get('error') or 'ok'}")
a.close()
print("\nMIGRATION VERIFIED on real data: no rows lost, no values changed.")
