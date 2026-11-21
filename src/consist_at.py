"""Consist (3-car vs 6-car) from AT's own data, with geometric fallback.

WHERE THIS COMES FROM
---------------------
AT publishes the units working a trip:

    GET /serviceinfo/v1/vehicles/byTripId/{trip_id}
    -> ["AMP        281", "AMP        484"]

This is the source behind "6 CAR" on the platform displays and the carriage
count in AT's journey planner. Their own front-end bundle does:

    CARRIAGES_PER_UNIT = 3
    mapVehiclesToCarriageCount = r => r?.response.length * CARRIAGES_PER_UNIT
    fetchCarriageCount = tripId => service.getVehiclesByTripId({tripId})

So the array length times three is the carriage count: two entries is a 6-car
service and names BOTH sets. Verified 2026-11-20 against 34 live rail trips,
all 200 (29 six-car, 5 three-car), including the 484+281 pair.

WHY IT WAS MISSED FOR SO LONG
-----------------------------
An earlier probe recorded this endpoint as "200 but always returns []". It had
been passing a `vehicleJourneyName` (the de-dashed trip id plus a time
suffix). The endpoint wants the **dashed GTFS trip_id**. One wrong identifier
format turned a working endpoint into a documented dead end.

WHY THE INFERENCE STAYS
-----------------------
This endpoint needs the Journey Planner key, which is not a subscribable
product and can be revoked without notice. Graded against it, the geometric
inference in consist.py scored 21 agree / 13 missed / **0 false positives** —
it understates (the MIN_SPEED guard goes blind on stationary couples) but
never lies. So: AT first, inference as the safety net, and the client is told
which it got via `source`.

ORDERING, AND WHAT AT DOES NOT TELL US
--------------------------------------
The array names both sets but NOT which is at the front. Car 5 of a 6-car is
in the rear set, so a complaint filed by carriage still needs front/rear. We
get that from geometry (consist.detect) and merge: AT decides membership,
geometry decides order. When geometry has no opinion the couple is reported
without `position`/`cars`, and a carriage-specific report degrades to
`shared` rather than guessing a half.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

LOG = logging.getLogger("akl.consist_at")

BASE = "https://api.at.govt.nz/serviceinfo/v1/vehicles/byTripId/"

CARRIAGES_PER_UNIT = 3      # AT's own constant
TIMEOUT_S = 8
MAX_WORKERS = 8

# One HTTP call per live trip, keyed by trip_id.
#
# This was 18 s — DELIBERATELY just under the 20 s poll interval, which meant
# every entry was always expired by the time the next poll looked at it. The
# cache never hit once: ~34 live trips x 3 polls/min = ~147,000 calls/day.
#
# The TTL was wrong because the question is wrong. A trip's unit list does not
# change DURING the trip: sets couple and uncouple at depots and termini, never
# mid-run. We were re-asking "is this a 6-car?" every 20 s about a fact that
# holds for the whole ~50-minute journey.
#
# 600 s is still far shorter than a trip, so a coupling change at a terminus is
# picked up well before that trip_id is in service again, and a genuinely new
# trip_id is a cache miss that gets fetched immediately. Cost: ~4,900 calls/day,
# a 30x reduction, with no loss of accuracy.
CACHE_TTL_S = 600
_cache: dict[str, tuple[float, list[str]]] = {}

# Fall back to the last good answer for this long if AT starts failing, so a
# blip does not make every 6-car on the map silently become a 3-car.
STALE_OK_S = 600
_last_good: dict[str, tuple[float, list[str]]] = {}


def _key() -> str:
    # Same key as the departure board; board.py documents the provenance.
    try:
        import board
        return board._key()
    except Exception:  # noqa: BLE001
        return (os.environ.get("AT_SERVICEINFO_KEY")
                or os.environ.get("AT_API_KEY") or "")


def set_number(label: str) -> str | None:
    """Bare set number from an AM-class label.

    The lookarounds are deliberate: a bare \\d{2,4} truncates "12345" to 1234.
    """
    m = re.search(r"(?<!\d)(\d{2,4})(?!\d)", str(label or ""))
    return m.group(1) if m else None


def _fetch(trip_id: str) -> list[str]:
    key = _key()
    if not key:
        return []
    req = urllib.request.Request(
        BASE + urllib.parse.quote(str(trip_id), safe=""),
        headers={"Ocp-Apim-Subscription-Key": key},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as f:
        d = json.loads(f.read())
    body = d.get("response") if isinstance(d, dict) else d
    if not isinstance(body, list):
        return []
    out = []
    for v in body:
        # Observed as plain strings; accept dicts too in case AT enriches it.
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            lbl = v.get("label") or v.get("vehicleRef") or v.get("id")
            if lbl:
                out.append(str(lbl))
    return out


def units_for_trips(trip_ids: list[str]) -> tuple[dict[str, list[str]], str | None]:
    """{trip_id: [unit label, ...]} for the given trips.

    Returns (mapping, error). An error means AT could not be reached at all;
    trips that legitimately have no units come back as an empty list.
    """
    want = [str(t) for t in dict.fromkeys(trip_ids) if t]
    if not want:
        return {}, None

    now = time.time()
    out: dict[str, list[str]] = {}
    todo: list[str] = []
    for t in want:
        hit = _cache.get(t)
        if hit and now - hit[0] < CACHE_TTL_S:
            out[t] = hit[1]
        else:
            todo.append(t)

    errs: list[str] = []

    def one(t: str) -> tuple[str, list[str] | None]:
        try:
            return t, _fetch(t)
        except urllib.error.HTTPError as e:
            errs.append("401 not subscribed to Service Info"
                        if e.code == 401 else "HTTP %d" % e.code)
            return t, None
        except Exception as e:  # noqa: BLE001 - network: report, never raise
            errs.append(type(e).__name__)
            return t, None

    if todo:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(todo))) as ex:
            for t, units in ex.map(one, todo):
                if units is None:
                    # Reuse the last good answer rather than downgrading the
                    # service to "3 cars" on a transient failure.
                    stale = _last_good.get(t)
                    if stale and now - stale[0] < STALE_OK_S:
                        out[t] = stale[1]
                    continue
                _cache[t] = (now, units)
                if units:
                    _last_good[t] = (now, units)
                out[t] = units

    # Keep the caches from growing without bound across a long uptime.
    if len(_cache) > 4000:
        _cache.clear()
    if len(_last_good) > 4000:
        _last_good.clear()

    err = "; ".join(sorted(set(errs))) if errs and not out else None
    return out, err


def couples_from_units(by_trip: dict[str, list[str]]) -> dict[str, dict]:
    """{set_no: {partner, consist, cars_total, source}} from AT's unit lists.

    Only pairs are emitted. A single-unit trip means a 3-car service, which is
    recorded so callers can distinguish "AT says 3" from "we don't know".
    """
    out: dict[str, dict] = {}
    for trip, units in (by_trip or {}).items():
        sets = [s for s in (set_number(u) for u in units) if s]
        # Dedupe while keeping order; a trip should not list a set twice.
        sets = list(dict.fromkeys(sets))
        if not sets:
            continue
        cars = len(sets) * CARRIAGES_PER_UNIT
        if len(sets) == 1:
            out[sets[0]] = {
                "partner": None, "consist": cars, "position": None,
                "cars": None, "trip_id": trip, "source": "at",
                "consist_inferred": False,
            }
            continue
        for i, sn in enumerate(sets):
            partner = sets[1 - i] if len(sets) == 2 else None
            out[sn] = {
                "partner": partner, "consist": cars, "position": None,
                "cars": None, "trip_id": trip, "source": "at",
                "consist_inferred": False,
            }
    return out


def merge(at_couples: dict[str, dict],
          inferred: dict[str, dict]) -> dict[str, dict]:
    """AT decides membership; geometry supplies front/rear order.

    Rules, in order:
      1. If AT named the units, that wins on `consist` and `partner` — it is
         published data and our inference is only 62% recall.
      2. Front/rear order is never published, so take it from the inference
         whenever the inference agrees with AT about who the partner is.
         Disagreement means the geometry paired the wrong units; drop the
         order rather than apply it to the wrong half.
      3. Sets AT said nothing about keep their inferred entry, still flagged
         `consist_inferred`, so a revoked key degrades instead of going dark.
    """
    out: dict[str, dict] = {}
    for sn, inf in (inferred or {}).items():
        out[sn] = dict(inf)
        out[sn].setdefault("source", "inferred")

    for sn, at in (at_couples or {}).items():
        row = dict(at)
        inf = (inferred or {}).get(sn)
        if inf and at.get("partner") and inf.get("partner") == at.get("partner"):
            row["position"] = inf.get("position")
            row["cars"] = inf.get("cars")
            row["gap_m"] = inf.get("gap_m")
            row["order_source"] = "inferred"
        elif inf and at.get("partner") and inf.get("partner") != at.get("partner"):
            LOG.info("consist: AT pairs %s with %s, geometry said %s "
                     "- keeping AT, dropping order",
                     sn, at.get("partner"), inf.get("partner"))
        out[sn] = row

    return out


def summarise(merged: dict[str, dict]) -> str:
    at6 = sum(1 for v in merged.values()
              if v.get("source") == "at" and v.get("consist") == 6)
    at3 = sum(1 for v in merged.values()
              if v.get("source") == "at" and v.get("consist") == 3)
    inf = sum(1 for v in merged.values() if v.get("source") != "at")
    ordered = sum(1 for v in merged.values() if v.get("position"))
    return ("at: %d six-car service(s), %d three-car; inferred-only sets: %d; "
            "front/rear known: %d" % (at6 // 2, at3, inf, ordered))
