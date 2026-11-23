# Observation archive — design

Status: **BUILT AND LIVE** on the S21 since 2026-11-23, approved by Matt.
Measured 2026-11-23 on real feed snapshots (73 trains).

## As built

- `src/archive.py` — the module. `src/test_archive.py` — 10 tests, pass on
  x86_64 and on arm64 inside the deployed image.
- Wired into `poller()` in `server.py`, **after** `_annotate_consists()` so
  consist/order/source land in history, and inside `run_in_executor` because
  SQLite writes block the event loop.
- `archive.init()` on startup; `GET /api/archive` for stats,
  `GET /api/archive?unit=AMP+281` for one unit's history.
- Enabled on the phone via `ARCHIVE_DB: /data/archive.db` in
  `compose.s21.yaml` (bind-mount, survives container rebuilds).
  `ARCHIVE_DB=off` disables it. **Not enabled on Fly** — the phone is the
  archive host; adding it on Fly would duplicate rows, not add coverage.
- `record()` never raises into the poller: a failed archive write returns 0 and
  logs. The live map is the product; this is a side effect.

Verified live through the tunnel: 598 rows / 10 polls / 0 failed polls, and a
real unit's history showing genuine movement between fixes with per-row
`fix_age_s` ranging 4–17 s — which is exactly the skew this archive exists to
let us correct for later.

## What this is, and what it is not

The live map keeps **nothing**. `last_trains.json` is one snapshot overwritten
every 20 s; `order_latch.json` expires entries after 3 h (`TTL_S`) and caps at
`MAX_ENTRIES = 400`; `feedback.db` stores user reports, not vehicle movement.
That is correct for a live map — a position from an hour ago is worthless to it,
and a latch that remembered longer would serve confidently wrong order after a
terminus reversal.

This archive is the opposite concern: keep every observation we already pay AT
for, so network performance can be analysed longitudinally later.

**It costs zero extra AT quota.** It subscribes to the poll the server already
makes. This is the whole reason it is cheap — see the quota note in the
`at-open-transit-data` skill: each deployed instance is a separate consumer, so
adding a *logger* is free but adding an *instance* is not.

## Measured size (not estimated)

262,800 rows = one 20 h service day at 20 s with 73 trains, loaded into real
SQLite with realistic per-poll variation, then VACUUMed:

| schema | B/row | day | month | year |
|---|---|---|---|---|
| wide / denormalised | 131 | 34 MB | 1.03 GB | 12.5 GB |
| **normalised + int-packed** | **59** | **15 MB** | **0.46 GB** | **5.6 GB** |

An earlier dedupe test measured 337 B/row and was **discarded as invalid**: it
replayed a single snapshot, so change-detection skipped 315,287 of 315,360 rows
and fixed page overhead dominated. Do not quote that number.

S21 has 209 GB free of 222 GB, 4.2 GB RAM available, 32 °C at spec time.

## Schema

Normalised, because `route`/`origin`/`terminus` repeat identically thousands of
times per day and `unit`/`partner_set` are short labels used as foreign keys.

```sql
CREATE TABLE unit(unit_pk INTEGER PRIMARY KEY, unit TEXT UNIQUE);
CREATE TABLE trip(trip_pk INTEGER PRIMARY KEY, trip_id TEXT UNIQUE,
                  route TEXT, origin TEXT, terminus TEXT, dest TEXT);
CREATE TABLE obs(
  ts         INTEGER NOT NULL,   -- unix seconds, poll time
  unit_pk    INTEGER NOT NULL,
  trip_pk    INTEGER,            -- NULL = not in service / no trip attached
  lat REAL, lon REAL,
  bearing    INTEGER,            -- whole degrees; sub-degree is noise
  speed_dkmh INTEGER,            -- decikm/h, avoids a float
  delay_s    INTEGER,
  progress   REAL,
  fix_age_s  INTEGER,            -- REQUIRED: see fix-skew note below
  flags      INTEGER,            -- bit0 in_service, bit1 moving, bit2 cancelled
  consist    INTEGER,            -- 3 or 6, never 9
  partner_pk INTEGER,
  pos        INTEGER,            -- 1 front, 2 rear, NULL unknown
  PRIMARY KEY (unit_pk, ts)
) WITHOUT ROWID;
```

`PRIMARY KEY (unit_pk, ts)` with `WITHOUT ROWID` is deliberate: the dominant
query is "one unit over time", which this makes a contiguous range scan, and it
drops the redundant rowid. `(ts, unit)` would be better for "whole network at
one instant" — chosen against, because per-unit history is the stated purpose.

Add `CREATE INDEX obs_ts ON obs(ts)` only if instant-wide queries become common;
it costs write throughput, so not by default.

## Rules that must not be lost

1. **Store `fix_age_s` on every row.** AT's vehicle fixes are not simultaneous.
   At 76 km/h a 10 s skew produces 211 m of phantom offset against a ~72 m
   real signal — this caused a real ordering bug. Any later analysis that
   compares two vehicles' positions **must** weight by fix age, and cannot if
   we did not record it. This single column is why the archive is worth having
   rather than just replaying the API.
2. **Record observations, never inferences, as fact.** `pos`/`consist` are
   stored because they are cheap and useful, but `consist_source` /
   `order_source` tell you whether a value came from AT or from geometry. Keep
   those; an archive that cannot distinguish measured from inferred will be
   mined years later as if it were all measured.
3. **Never back this file up inside the repo tree.** The last pre-deletion
   backup landed in `archive/` untracked-but-not-ignored and was one `git add -A`
   from publishing personal data. `.gitignore` now carries `*.db`, `*.sqlite*`,
   `archive/`. Verify with `git check-ignore -v` before any backup.
4. **No personal data.** This is vehicle telemetry only. Do not join it to
   `feedback.db`, which has a reporter-attributable history.

## Operational posture

- **Host: the S21.** The logger tolerates gaps, which is exactly the phone's
  weakness (thermal clamp to 1.04 GHz at 85 °C, no RTC, one historic Wi-Fi
  drop). Gaps are recoverable signal here, not corruption.
- **WAL mode**, `synchronous=NORMAL`. UFS flash has finite writes; one batched
  `executemany` per poll, not a write per train.
- **Gap rows matter.** When a poll fails, record the failure rather than
  silently leaving a hole — otherwise "no trains" and "no data" are
  indistinguishable in analysis. A `poll(ts, ok, trains, poll_ms, error)` table.
- **Clock caveat.** The phone has no RTC and was once 13 h out. `ts` comes from
  the host clock, so a timesync failure silently poisons the time axis. Log
  `CLOCK_MONOTONIC` alongside, or at minimum assert sync at startup.

## Open decision

**Retention.** At 5.6 GB/year on 209 GB free, my recommendation is **keep
everything, indefinitely** — the whole point is longitudinal analysis, and
downsampling now destroys exactly the resolution that makes coupling/uncoupling
transitions visible. Revisit at ~50 GB. Rolling up rows older than N years into
per-minute aggregates is always available later; un-deleting is not.
