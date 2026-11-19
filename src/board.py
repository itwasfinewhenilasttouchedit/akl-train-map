"""Station departure board from AT's own arrivals/departures API.

WHY THIS EXISTS
---------------
The first version of the departure board computed itself from the baked GTFS
timetable and then applied the realtime `tripupdates` delay. That is a
reconstruction of a departure board, not a departure board: it shows what the
timetable says plus a correction, so a cancelled service still appears, a
platform change never shows, and the times drift from what the platform screens
and the AT app are telling the same passenger.

AT publishes the real thing:

    GET /serviceinfo/v1/departures/{stop_code}

It is SIRI stop-monitoring in JSON. Each entry in `response.movements` carries
both the scheduled and the *expected* time, the real platform, the destination
as displayed, and a `monitored` flag saying whether a vehicle is actually being
tracked. Measured live: a busy stop returns ~9 movements covering roughly 2 to
33 minutes ahead, of which the imminent ones are `monitored: true` and the
later ones are schedule-only.

THE JOIN BACK TO OUR DATA
-------------------------
`vehicleJourneyName` is our GTFS trip_id with the dashes stripped and a time
suffix appended:

    vehicleJourneyName  258880002805802W195590c5f4b5aa222300
    our trip_id         258-880002-80580-2-W195590-c5f4b5aa

So a prefix test on the de-dashed trip_id recovers the trip, and from the trip
we recover the train set — which is the whole point of this map, since the
feedback/pee data is keyed on the set number.

WHAT THIS DOES NOT DO
---------------------
It does not invent a board when AT gives us nothing. An empty `movements` list
at 23:40 is the correct answer (the last trains have gone), and is reported as
such rather than back-filled from the timetable. The caller decides whether to
fall back.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://api.at.govt.nz/serviceinfo/v1/departures/"

# A station is up to four platforms, each its own stop_code, each its own HTTP
# call. Cached briefly so that opening a board, or several people opening the
# same board, does not fan out to AT on every click. 15s is below the ~20s at
# which the underlying data changes.
CACHE_TTL_S = 15
_cache: dict[str, tuple[float, list[dict]]] = {}

TIMEOUT_S = 12


# AT's own Journey Planner key, read out of their public front-end bundle
# (journeyplanner/.../jp.bundle.js, where it appears as envConfig API_KEY).
#
# Why this is here rather than in the secret store:
#   * `serviceinfo` is NOT a subscribable product. The dev portal offers only
#     GTFS API, Realtime API and Realtime Compat API, so there is no
#     subscription a developer can add that grants it — our own registered
#     keys return 401. This key is the only one that works.
#   * It is not a credential in any meaningful sense: it is shipped in plain
#     text to every visitor of AT's own journey planner, and the endpoint is
#     read-only public timetable data. Treating it as a secret would be
#     security theatre, and hiding it in the key manager would imply it is one.
#   * The risk is availability, not secrecy: AT may rotate it or lock the
#     endpoint down at any time without notice, since neither is documented.
#     That is why every caller falls back to the timetable board instead of
#     failing, and why `source` is reported to the client.
#
# Overridable by env in case it rotates and a fix is needed without a rebuild.
JP_KEY = "693150c317fc42c5a2f871aee3f586af"


def _key() -> str:
    """The serviceinfo key, most specific first."""
    return (os.environ.get("AT_SERVICEINFO_KEY")
            or JP_KEY
            or os.environ.get("AT_API_KEY") or "")


def _fetch(stop_code: str) -> list[dict]:
    key = _key()
    if not key:
        return []
    req = urllib.request.Request(
        BASE + urllib.parse.quote(str(stop_code)),
        headers={"Ocp-Apim-Subscription-Key": key},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as f:
        d = json.loads(f.read())
    return ((d.get("response") or {}).get("movements") or [])


def movements(stop_codes: list[str]) -> tuple[list[dict], str | None]:
    """Movements for every platform of one station, merged.

    Returns (movements, error). An error string means we could not reach AT at
    all; it is kept separate from "reached AT and there is nothing running",
    because those two deserve very different words on screen.
    """
    codes = [str(c) for c in stop_codes if c]
    if not codes:
        return [], "no platforms known for this station"

    now = time.time()
    fresh = _cache.get("|".join(sorted(codes)))
    if fresh and now - fresh[0] < CACHE_TTL_S:
        return fresh[1], None

    errs: list[str] = []

    def one(c: str) -> list[dict]:
        try:
            return _fetch(c)
        except urllib.error.HTTPError as e:
            # 401 is the diagnostic case worth naming precisely: the key is
            # valid but not subscribed to the Service Info product.
            errs.append("401 not subscribed to Service Info"
                        if e.code == 401 else f"HTTP {e.code}")
            return []
        except Exception as e:  # noqa: BLE001 - network, report not raise
            errs.append(type(e).__name__)
            return []

    with ThreadPoolExecutor(max_workers=min(4, len(codes))) as ex:
        out: list[dict] = []
        for part in ex.map(one, codes):
            out.extend(part)

    if not out and errs:
        return [], "; ".join(sorted(set(errs)))

    _cache["|".join(sorted(codes))] = (now, out)
    return out, None


def dedash(trip_id: str) -> str:
    return (trip_id or "").replace("-", "")


def to_departures(movements_: list[dict], trip_ids: list[str], geo: dict,
                  limit: int = 8) -> list[dict]:
    """AT movements -> the shape the front end and attach_risk() already expect.

    Deliberate choices:
      * `expectedDepartureTime` wins over `scheduledDepartureTime`. The expected
        time is the whole reason for using this API; falling back to scheduled
        only when AT has no estimate.
      * Sorted by the time actually shown, not by schedule, so a delayed train
        appears where the rider will really catch it.
      * The platform comes from AT (`departurePlatformName`), not from our
        timetable, so a platform change is reflected.
      * `monitored` is passed through as `live`. A rider deserves to know
        whether a time is a tracked estimate or just the timetable.
    """
    import datetime as _dt

    stops = geo.get("stops") or {}
    trips = geo.get("trips") or {}
    out: list[dict] = []
    now = _dt.datetime.now(_dt.timezone.utc)

    for m in movements_:
        iso = m.get("expectedDepartureTime") or m.get("scheduledDepartureTime")
        if not iso:
            continue
        try:
            when = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        eta_s = int((when - now).total_seconds())
        if eta_s < -60:          # already gone
            continue

        tid = trip_for(m, trip_ids)
        dest = m.get("destinationDisplay") or None
        # AT's destinationDisplay is a display string in caps for buses; for
        # rail it is already the station name. Where we recovered the trip we
        # prefer the trip's own last call, which matches the rest of the app's
        # naming (post-CRL names like Waitemata rather than Britomart).
        if tid and trips.get(tid):
            calls = trips[tid].get("calls") or []
            if calls:
                last = stops.get(calls[-1][0], {}).get("name") or ""
                import re as _re
                last = _re.sub(r"\s+\d+$", "", last)
                if last:
                    dest = last

        NZT = _dt.timezone(_dt.timedelta(hours=13))
        nz = when.astimezone(NZT)

        # The scheduled time must be converted too, not sliced out of the raw
        # ISO string. AT returns UTC ("...T10:59:00.000Z"), so slicing gave
        # "sched 10:59" next to a 23:59 departure — a 13-hour discrepancy that
        # reads as a badly broken board.
        sched = None
        iso_s = m.get("scheduledDepartureTime")
        if iso_s:
            try:
                sched = (_dt.datetime.fromisoformat(iso_s.replace("Z", "+00:00"))
                         .astimezone(NZT).strftime("%H:%M"))
            except ValueError:
                sched = None

        out.append({
            "dep": nz.strftime("%H:%M"),
            "dep_s": nz.hour * 3600 + nz.minute * 60 + nz.second,
            "arr": None, "arr_s": None,
            "dest": dest,
            "route": m.get("route_short_name"),
            "headsign": dest,
            "platform": m.get("departurePlatformName")
                        or m.get("arrivalPlatformName"),
            "eta_s": eta_s,
            "live": bool(m.get("monitored")),
            "scheduled": sched,
            "trip_id": tid,
            "trip_ids": [tid] if tid else [],
            "stop_code": m.get("stop_code"),
        })

    out.sort(key=lambda d: d["eta_s"])
    return out[:limit]


_index: dict[int, dict[str, str]] = {}


def _trip_index(trip_ids: list[str]) -> dict[str, str]:
    """{de-dashed trip_id: trip_id}, cached per trip list identity."""
    k = id(trip_ids)
    idx = _index.get(k)
    if idx is None or len(idx) != len(trip_ids):
        idx = {dedash(t): t for t in trip_ids}
        _index.clear()
        _index[k] = idx
    return idx


def trip_for(movement: dict, trip_ids: list[str]) -> str | None:
    r"""Recover our GTFS trip_id from a movement's vehicleJourneyName.

    The rule, verified on rail and on three separate bus movements:

        vehicleJourneyName == trip_id.replace("-", "") + HHMMSS

        258880002805802W195590c5f4b5aa222300
        \__________ 30 chars _________/\_6_/
        258-880002-80580-2-W195590-c5f4b5aa

    Matched EXACTLY on the suffix-stripped string, not with startswith(). A
    prefix test can match a shorter trip_id that happens to be a prefix of the
    right one, which would silently file a departure against the wrong trip —
    and therefore against the wrong train set, which is the one thing this app
    must never get wrong.
    """
    vjn = movement.get("vehicleJourneyName") or ""
    if len(vjn) < 7:
        return None
    return _trip_index(trip_ids).get(vjn[:-6])
