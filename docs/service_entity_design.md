# Materialise `Service` — design

Status: **design, phase 0 of 5**. Agreed 2026-11-27 (Matt chose option A).
Scope: `src/live.py`, `src/server.py`, `src/consist_at.py`, `src/archive.py`,
`src/board.py`, `src/journey.py`, `src/pages.py`, `src/feedback.py`,
`web/index.html`.

## The defect, stated precisely

`in_service` carries three distinct meanings that were identical for 3-cars and
came apart for 6-cars:

1. **has a trip** — AT attached a `trip_id` to this vehicle
2. **is carrying passengers** — true for BOTH halves of a 6-car
3. **should be drawn** — true for exactly ONE half

There is no entity whose identity is "the train running trip X". `unit`,
`trip`, `place` and `obs` exist (`archive.py:76+`); the coupling is a nullable
`obs.partner_pk` self-reference. A service exists only as:

- a transient `trainMakeup()` return value computed in JS, per render; and
- a 125-line reconstruction in `server.py:_annotate_consists()` that copies 12
  fields from one vehicle dict onto another at request time.

### Evidence (measured, not asserted)

- `server.py:377` counts trains with `in_service is not False and not
  trailing_half`, and its own comment says this must stay identical to the
  client's draw predicate (`web/index.html:2889`) "so the number in the HUD and
  the number of icons cannot disagree". Two tiers hand-synchronising a
  definition **is** the missing entity.
- `_annotate_consists()` copies `route, line, colour, dest, to, from, platform,
  eta_s, delay_s, trip_id, origin, terminus` — 12 Service attributes stored on
  vehicle rows — then a **second pass partially reverses the first** because
  the first used trip-ownership instead of geometry.
- That docstring records a record observed carrying `trailing_half: True` and
  `consist_position: "front"` simultaneously: the model contradicting itself.
  It was fixed by adding a pass, not by fixing the grain.
- Nothing is persisted at service grain: ~300k rows/day at vehicle grain, so
  historical analysis inherits the flaw permanently.
- Carriage numbering threads an `absolute base` through `halfVerdict(sn, base)`
  by hand because nothing owns the frame of reference.

### NOT the justification

An earlier claim that a trailing half reading `in_service:false` is a live bug
was **wrong** — see `MEMORY.md`. The rescue path works; zero units are
OOS-and-not-trailing while carrying a trip. This is a
**correctness-preserving refactor**. The bar is "do not regress behaviour six
live units depend on tonight", not "ship fast".

## Target model

```
service(service_pk, trip_pk, cars, lead_unit_pk, first_seen, last_seen,
        order_src, consist_src)
service_member(service_pk, unit_pk, pos, is_lead)   -- pos 1 = front half
```

- `obs` keeps only what a GPS fix knows: ts, unit, lat/lon, bearing, speed,
  fix_age. Plus a nullable `service_pk`.
- Service owns: trip, route, line, colour, dest, from/to, platform, eta_s,
  delay_s, origin, terminus, cars, in_service.
- `reports` gains nullable `service_pk` alongside `set_no` (set_no STAYS — a
  report files against the thing that gets cleaned; see DECISIONS 2026-11-27).

### Invariants the model must make unrepresentable

1. A service has 1 or 2 members, never more, never zero.
2. Exactly one member is `is_lead` (the drawn icon).
3. `pos` is 1 or 2 and unique within a service.
4. `cars == 3 * len(members)`.
5. A member's absolute carriage base is `(pos - 1) * 3 + 1` — **derived, never
   passed**. This kills the `base` threading.
6. "Should be drawn" = `is_lead`. "Carrying passengers" = service is in
   service. They are different columns now, so they cannot be conflated.

## Phasing — each phase independently shippable and reversible

- **Phase 1** — `src/service.py`: pure `build_services(trains) -> [Service]`
  with the invariants asserted. No wiring. Gate `shot_service_model.py`
  including property tests over the 19-of-19 six-car fixtures.
- **Phase 2** — wire `server.py` to build services and DERIVE the existing
  payload fields from them. Byte-identical `/api/trains` output, verified by
  diffing live payloads before/after. `_annotate_consists()` deleted.
- **Phase 3** — expose `services[]` in the payload alongside `trains[]`; move
  the client to iterate services. Kills the duplicated draw predicate.
- **Phase 4** — archive: add `service`/`service_member` tables + `obs.service_pk`
  via `_migrate()` (ALTER ADD COLUMN only; `obs` is `WITHOUT ROWID` with a
  composite PK, so no table rewrite is attempted). Old rows keep NULL — the
  honest value.
- **Phase 5** — `reports.service_pk`, carriage base derived from `pos`.

## Migration constraint

Live archive is ~31 MB (local dev copy is 160 KB — **do not size-test against
it**). `archive._migrate()` already does idempotent `ALTER TABLE ADD COLUMN`
and explains why `CREATE TABLE IF NOT EXISTS` is a no-op on an existing DB.
New TABLES are safe (they don't exist yet); new COLUMNS must go through
`_migrate()`'s `want` dict. **No backfill of historical services** — the
coupling inference needs live GPS that the archive stores per-observation;
reconstructing it retroactively would be inventing data.

## Rollback

Phases 1–3 are code-only: revert the commit. Phase 4 adds tables and a nullable
column — reverting the code leaves them unread and harmless (never drop them;
a dropped column leaves recoverable bytes, see `feedback._drop_reporter`).
