"""Detect coupled 6-car consists by inference from vehicle positions.

WHY THIS EXISTS
---------------
AT publishes no consist information. Both realtime endpoints
(`vehiclelocations`, `tripupdates`) carry exactly ONE vehicle label per trip,
and the only vehicle fields are `id`, `label` and an always-empty
`license_plate`. The non-legacy/v3 endpoints 404. So "is this a 6-car, and
which two sets is it?" cannot be read from the feed.

It CAN be inferred. A 6-car service is two 3-car sets coupled, and both sets
keep reporting their own GPS. Verified against live data (2026-11-19, two
samples 45 s apart):

  - 11 candidate pairs per sample out of ~81 rail vehicles.
  - Separation 53-90 m. An AM-class 3-car set is ~72 m long, so the two
    reporting cars sit about one set-length apart — exactly as expected.
  - Speeds matched within 0.2 m/s and bearings within 1 degrees.
  - 7 pairs persisted across both samples while travelling 500-1000 m, with
    the gap holding to within GPS jitter (one pair held 72 m exactly).
  - In EVERY pair, exactly one set carried the trip_id and the other carried
    none — which also explains the fleet view's "out of service" units that
    are plainly moving at line speed: they are the trailing halves of 6-cars.

THE LIMITS, STATED PLAINLY
--------------------------
1. This is an INFERENCE, not published data. It is labelled as such in the API
   (`consist_inferred: true`) and in the UI.
2. It only works while MOVING. In a stabled yard every unit reads speed 0 and
   bearing 0, and sets park metres apart — a depot of 20 units would produce
   dozens of bogus "couples". Below MIN_SPEED we report nothing rather than
   guess, because a false 6-car claim would mis-attribute a complaint.
3. Two trains genuinely running close together on the same track at the same
   speed would pair falsely. The persistence requirement makes this rare, and
   the cost is low: a wrong 6-car label, not a wrong smell verdict.
"""
from __future__ import annotations

import math
import logging

LOG = logging.getLogger("akl.consist")

# Tuned from the live measurements above, with margin.
MAX_GAP_M = 160.0     # observed 53-90 m; a 3-car set is ~72 m
MIN_SPEED = 2.0       # below this, stationary units are indistinguishable
MAX_SPEED_DIFF = 1.5  # observed <= 0.2 m/s
MAX_BEARING_DIFF = 12.0   # observed <= 1 degrees; allow for curves
MIN_FRONT_SEP_M = 25.0    # below this, front/rear is GPS noise, not geometry


def _m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _bearing_diff(b1: float, b2: float) -> float:
    d = abs(b1 - b2) % 360.0
    return min(d, 360.0 - d)


def _ahead(a: dict, b: dict, heading: float) -> bool:
    """True if a is ahead of b along `heading`.

    Projects the b->a offset onto the direction of travel. Bearing is degrees
    clockwise from north, so the heading unit vector is
    (north, east) = (cos, sin). Separation is 50-90 m against a GPS error of
    5-10 m, so the sign of this dot product is reliable.
    """
    # Local flat approximation: fine over 100 m.
    dn = (a["lat"] - b["lat"]) * 111320.0
    de = (a["lon"] - b["lon"]) * 111320.0 * math.cos(math.radians(b["lat"]))
    th = math.radians(heading)
    return (dn * math.cos(th) + de * math.sin(th)) > 0


def detect(trains: list[dict]) -> dict[str, dict]:
    """Find coupled pairs among live rail vehicles.

    `trains` entries need: unit/set_no, lat, lon, speed (m/s), bearing.
    Returns {set_no: {"partner", "consist", "position", "cars",
                      "consist_inferred"}} for members of a detected couple.
    Sets that are not part of a detected couple are simply absent — callers
    must treat absence as "unknown", never as "3-car".
    """
    cand = []
    for t in trains:
        sn, lat, lon = t.get("set_no"), t.get("lat"), t.get("lon")
        if not sn or lat is None or lon is None:
            continue
        spd = t.get("speed") or 0.0
        if spd < MIN_SPEED:
            continue
        cand.append({"set_no": str(sn), "lat": lat, "lon": lon, "spd": spd,
                     "brg": t.get("bearing") or 0.0,
                     "age": t.get("fix_age_s"),
                     "trip": t.get("trip_id")})

    # Greedy nearest-first pairing. A set belongs to at most one couple, and
    # taking the closest match first stops a middle unit in a line of three
    # from stealing the wrong partner.
    pairs: list[tuple[float, dict, dict]] = []
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            a, b = cand[i], cand[j]
            gap = _m(a["lat"], a["lon"], b["lat"], b["lon"])
            if gap > MAX_GAP_M:
                continue
            if abs(a["spd"] - b["spd"]) > MAX_SPEED_DIFF:
                continue
            if _bearing_diff(a["brg"], b["brg"]) > MAX_BEARING_DIFF:
                continue
            pairs.append((gap, a, b))
    pairs.sort(key=lambda p: p[0])

    out: dict[str, dict] = {}
    taken: set[str] = set()
    for gap, a, b in pairs:
        if a["set_no"] in taken or b["set_no"] in taken:
            continue
        taken.add(a["set_no"])
        taken.add(b["set_no"])
        # Prefer the bearing of whichever unit carries the trip; it is the one
        # AT is actively tracking. Falls back to the average.
        heading = (a["brg"] if a["trip"] else
                   b["brg"] if b["trip"] else (a["brg"] + b["brg"]) / 2.0)
        if gap >= MIN_FRONT_SEP_M:
            front, rear = (a, b) if _ahead(a, b, heading) else (b, a)
            known_order = True
        else:
            # Too close to call: report the couple but not the order, rather
            # than inventing a front/rear that a rider would act on.
            front, rear, known_order = a, b, False
        for unit, pos, cars in ((front, "front", [1, 2, 3]),
                                (rear, "rear", [4, 5, 6])):
            out[unit["set_no"]] = {
                "partner": (rear if unit is front else front)["set_no"],
                "consist": 6,
                "position": pos if known_order else None,
                "cars": cars if known_order else None,
                "gap_m": round(gap),
                "consist_inferred": True,
                # Scoring inputs for order_latch: how much to trust this
                # particular front/rear call. Speed is what makes the
                # direction of travel real; bearing disagreement between two
                # rigidly coupled halves measures the heading noise present.
                "speed_mps": round(min(a["spd"], b["spd"]), 2),
                "brg_diff": round(_bearing_diff(a["brg"], b["brg"]), 1),
                # Fix-age skew between the two halves. The projection compares
                # two POSITIONS for a ~72 m offset, so if one fix is stale the
                # train has moved in between: 10 s of skew at 76 km/h is 211 m
                # of phantom offset, enough to flip the sign. Measured: the
                # one pair out of 28 that reported contradictory orders across
                # polls had exactly that skew. Scored, not rejected.
                "fix_skew_s": (abs(a["age"] - b["age"])
                               if a.get("age") is not None
                               and b.get("age") is not None else None),
            }
    if out:
        LOG.info("consist: %d coupled 6-car service(s) inferred", len(out) // 2)
    return out


def set_for_car(car_pos: int, pairing: dict | None) -> str | None:
    """Which set number a car position belongs to on a detected 6-car.

    Returns None when the order is unknown — the caller must then record the
    report against the set it already knows, flagged as unattributable, rather
    than guessing a half.
    """
    if not pairing or not pairing.get("cars") or not car_pos:
        return None
    if car_pos in pairing["cars"]:
        return None        # this set's own half; caller already has the number
    return pairing.get("partner")
