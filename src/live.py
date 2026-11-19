"""Live Auckland train positions from AT's GTFS-Realtime vehicle feed.

This is the real thing: actual GPS positions reported by the trains, with
heading, speed, occupancy and the **unit number** (e.g. "AMP 973"). It needs a
registered AT API key — free from https://dev-portal.at.govt.nz — supplied as
AT_API_KEY, or pulled from the control-room key manager (see secret_key()).

One request to /realtime/legacy/vehiclelocations returns every vehicle in
Auckland (~1500); we keep the rail ones. Each in-service entity carries a
trip_id, so the GTFS schedule built by build_static.py tells us which station
is next and when it is due — no departures polling at all.

We show the whole rail fleet, not just trains in service. A vehicle with a rail
`trip` is a passenger service and gets a line colour, destination and ETA. A
vehicle whose label marks it as a train unit (AMP…) but which carries no trip
is stabled stock, an empty-stock move or a depot shunt: real and worth plotting,
but with no destination to claim, so it is flagged `in_service: False` and put
on a separate pseudo-line that the UI can filter off.

If no key is configured, fall back to live_infer.collect(), the keyless
departures-interpolation method. The map works either way; with a key it's GPS.
"""
from __future__ import annotations

import os
import json
import math
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

import httpx

LOG = logging.getLogger("akl.live")

ROOT = Path(__file__).resolve().parent.parent
GEO = json.loads((ROOT / "output" / "rail_geo.json").read_text())
STOPS: dict[str, dict] = GEO["stops"]
SHAPES: dict[str, list] = GEO["shapes"]
SHAPE_CUM: dict[str, list] = GEO["shape_cum"]
ROUTES: dict[str, dict] = GEO["routes"]
TRIPS: dict[str, dict] = GEO.get("trips", {})

BASE = "https://api.at.govt.nz"
VEHICLES = f"{BASE}/realtime/legacy/vehiclelocations"
TRIPUPDATES = f"{BASE}/realtime/legacy/tripupdates"
RAIL_ROUTE_IDS = {"S-C-201", "E-W-201", "O-W-201", "HUIA-404"}
STOP_IDS: dict[str, str] = GEO.get("stop_ids", {})
NZ = timezone(timedelta(hours=13))        # NZDT; GTFS times are local service time

# Rail units identify themselves by label prefix. Verified against a full feed
# snapshot: of 1400+ vehicles, exactly 85 carried an AMP label and every one of
# the 20 vehicles holding a rail trip was AMP-labelled — no bus or ferry uses
# the prefix, so this is a safe way to find train units that have no trip
# assigned. AMA is included defensively (AT has used it for rail stock).
RAIL_UNIT_PREFIXES = ("AMP", "AMA")

# A vehicle whose GPS fix is older than this is stale — it has probably stopped
# reporting (end of run, depot, dead radio). Dropping them keeps ghosts off the map.
MAX_FIX_AGE_S = 180
# Out-of-service units sit still for long stretches and report less often, so
# they get a looser limit; without it, stabled trains would flicker on and off.
OOS_MAX_FIX_AGE_S = 3600

# Pseudo-route for units with no trip, so they get a legend row and a filter
# toggle of their own alongside the four real lines.
OOS_ROUTE = "OOS"
OOS_NAME = "Not in service"
OOS_COLOUR = "#6b7684"


_KEY_CACHE: dict = {"key": None, "at": 0.0}
KEY_TTL_S = 900          # re-read periodically so a rotated key is picked up


def api_key() -> str | None:
    """AT_API_KEY, or a key fetched from the control-room key manager.

    CR_SECRET_URL + CR_SECRET_TOKEN let the container hold only a scoped,
    revocable token instead of the API key itself. Cached for KEY_TTL_S so we
    don't hit the key manager on every poll — but not forever, so rotating the
    secret takes effect without a redeploy.
    """
    k = (os.environ.get("AT_API_KEY") or "").strip()
    if k:
        return k
    now = time.time()
    if _KEY_CACHE["key"] and now - _KEY_CACHE["at"] < KEY_TTL_S:
        return _KEY_CACHE["key"]
    url, tok = os.environ.get("CR_SECRET_URL"), os.environ.get("CR_SECRET_TOKEN")
    if url and tok:
        try:
            r = httpx.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=15)
            if r.status_code == 200:
                v = (r.json().get("value") or "").strip()
                if v:
                    if v != _KEY_CACHE["key"]:
                        LOG.info("AT key loaded from the control-room key manager")
                    _KEY_CACHE.update(key=v, at=now)
                    return v
            LOG.warning("key manager returned %s: %s", r.status_code, r.text[:120])
        except Exception as e:
            LOG.warning("key manager unreachable: %s", e)
        # A cached key beats no key at all if the key manager is briefly down.
        if _KEY_CACHE["key"]:
            LOG.info("using the cached AT key")
            return _KEY_CACHE["key"]
    return None


def bearing(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlo = lo2 - lo1
    y = math.sin(dlo) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def haversine(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    dla, dlo = (la2 - la1) * p, (lo2 - lo1) * p
    h = math.sin(dla / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) * math.sin(dlo / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def snap_to_shape(shape_id: str, lat: float, lon: float) -> float | None:
    """Distance along a shape of the nearest vertex to a point (metres)."""
    poly, cum = SHAPES.get(shape_id), SHAPE_CUM.get(shape_id)
    if not poly:
        return None
    tgt = (lat, lon)
    best_i = min(range(len(poly)), key=lambda i: haversine(poly[i], tgt))
    return cum[best_i]


def unit_label(raw: str) -> str | None:
    """"AMP        973" -> "AMP 973". AT pads the label with spaces."""
    s = " ".join((raw or "").split())
    return s or None


def is_rail_unit(unit: str | None) -> bool:
    """Is this vehicle label a train unit? Used to catch units with no trip."""
    return bool(unit) and unit.upper().startswith(RAIL_UNIT_PREFIXES)


def nearest_station(lat: float, lon: float) -> tuple[str | None, float | None]:
    """Closest rail station to a point, and the distance in metres.

    Out-of-service units have no trip and therefore no origin/destination, so
    the only useful thing to say about where they are is which station or depot
    they are sitting near.
    """
    best, best_d = None, None
    for code, s in STOPS.items():
        d = haversine((lat, lon), (s["lat"], s["lon"]))
        if best_d is None or d < best_d:
            best, best_d = code, d
    return best, best_d


def station(code: str | None) -> str | None:
    """Station name without the trailing platform number.

    GTFS stop names for rail are per-platform ("Newmarket 4"), which reads oddly
    as a destination. Platform is surfaced separately.
    """
    if not code:
        return None
    n = STOPS.get(code, {}).get("name")
    if not n:
        return None
    parts = n.rsplit(" ", 1)
    return parts[0] if len(parts) == 2 and parts[1].isdigit() else n


def platform_of(code: str | None) -> str | None:
    n = STOPS.get(code or "", {}).get("name") or ""
    parts = n.rsplit(" ", 1)
    return parts[1] if len(parts) == 2 and parts[1].isdigit() else None


def num(v) -> float | None:
    """AT's JSON is inconsistent: bearing/speed arrive as a number OR a string
    (and occasionally ""). Coerce to float, or None if it isn't a number."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def service_seconds(now: float) -> float:
    """Seconds since local midnight, which is how GTFS stop_times are expressed."""
    d = datetime.fromtimestamp(now, NZ)
    return d.hour * 3600 + d.minute * 60 + d.second


def preceding(trip: dict, nxt_code: str, lat: float, lon: float):
    """Given the authoritative next stop, find the one before it on this trip
    and how far along that hop the train is. Returns (prev_code, progress)."""
    calls = trip.get("calls") or []
    idx = next((i for i, c in enumerate(calls) if c[0] == nxt_code), None)
    if idx is None or idx == 0:
        return None, None
    prev_code = calls[idx - 1][0]
    a, b = STOPS.get(prev_code), STOPS.get(nxt_code)
    if not a or not b:
        return prev_code, None
    total = haversine((a["lat"], a["lon"]), (b["lat"], b["lon"]))
    if total <= 0:
        return prev_code, None
    done = haversine((a["lat"], a["lon"]), (lat, lon))
    return prev_code, min(max(done / total, 0.0), 1.0)


def next_after(trip: dict, last_code: str, lat: float, lon: float, now: float, delay: float):
    """Next stop after the one just departed, with a delay-adjusted ETA.

    (prev_code, next_code, due_ts, progress). `delay` comes from the live trip
    update and is added to the timetable, which is what makes the ETA honest.
    """
    calls = trip.get("calls") or []
    i = next((k for k, c in enumerate(calls) if c[0] == last_code), None)
    if i is None or i + 1 >= len(calls):
        return last_code, None, None, None
    prev_code, nxt_code = calls[i][0], calls[i + 1][0]
    midnight = datetime.fromtimestamp(now, NZ).replace(hour=0, minute=0, second=0, microsecond=0)
    due = midnight.timestamp() + calls[i + 1][1] + (delay or 0)
    a, b = STOPS.get(prev_code), STOPS.get(nxt_code)
    prog = None
    if a and b:
        total = haversine((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        if total > 0:
            prog = min(max(haversine((a["lat"], a["lon"]), (lat, lon)) / total, 0.0), 1.0)
    return prev_code, nxt_code, due, prog


def next_call(trip: dict, lat: float, lon: float, now: float):
    """Which station this train is heading for, and when it is due.

    Uses the schedule for its trip: find where the train is along the trip's
    stop list (by proximity), then the first stop at or after that point whose
    scheduled time hasn't passed. Returns (prev, next, due_ts, progress).
    """
    calls = trip.get("calls") or []
    if not calls:
        return None, None, None, None

    # Index of the nearest scheduled stop to the current position.
    def dist_to(i: int) -> float:
        c = STOPS.get(calls[i][0])
        return haversine((lat, lon), (c["lat"], c["lon"])) if c else 1e12

    near = min(range(len(calls)), key=dist_to)
    secs = service_seconds(now)

    # The next call is the first one still ahead in time; if the schedule has
    # run out (late-running trip), fall back to the stop after the nearest.
    nxt_i = None
    for i in range(len(calls)):
        if calls[i][1] >= secs - 60:
            nxt_i = i
            break
    if nxt_i is None or nxt_i < near:
        nxt_i = min(near + 1, len(calls) - 1)
    # If the train is sitting past its nearest stop, the next one is ahead of it.
    if nxt_i == near and near + 1 < len(calls):
        nxt_i = near + 1

    prev_i = max(nxt_i - 1, 0)
    prev_code, nxt_code = calls[prev_i][0], calls[nxt_i][0]
    # Scheduled time -> today's timestamp (handles trips that run past midnight).
    midnight = datetime.fromtimestamp(now, NZ).replace(hour=0, minute=0, second=0, microsecond=0)
    due = midnight.timestamp() + calls[nxt_i][1]

    a, b = STOPS.get(prev_code), STOPS.get(nxt_code)
    prog = None
    if a and b and prev_code != nxt_code:
        total = haversine((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        done = haversine((a["lat"], a["lon"]), (lat, lon))
        if total > 0:
            prog = min(max(done / total, 0.0), 1.0)
    return prev_code, nxt_code, due, prog


async def fetch_vehicles(key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get(VEHICLES, headers={"Ocp-Apim-Subscription-Key": key})
        r.raise_for_status()
        body = r.json()
    resp = body.get("response") or {}
    return resp.get("entity") or []


async def fetch_both(key: str) -> tuple[list[dict], list[dict]]:
    """Vehicle positions and trip updates, concurrently.

    Trip updates carry the *predicted* arrival/departure time and delay per
    stop, which is what makes an ETA honest — the GTFS schedule alone only says
    when a train was supposed to be there.
    """
    h = {"Ocp-Apim-Subscription-Key": key}
    async with httpx.AsyncClient(timeout=25) as c:
        async def get(url: str) -> list[dict]:
            try:
                r = await c.get(url, headers=h)
                r.raise_for_status()
                return (r.json().get("response") or {}).get("entity") or []
            except Exception as e:
                LOG.warning("feed %s failed: %s", url.rsplit("/", 1)[-1], e)
                return []
        return await asyncio.gather(get(VEHICLES), get(TRIPUPDATES))


def index_updates(entities: list[dict]) -> dict[str, dict]:
    """trip_id -> {"code", "t", "delay", "seq"} for the train's LAST stop event.

    Important and counter-intuitive: despite the GTFS-RT name, AT's
    `stop_time_update` is **not a forecast**. Measured against the feed header,
    its event time is always in the past (-30s to -300s) and its stop is the one
    the train has just left — the stop index equals or is one behind the stop
    nearest the vehicle's own GPS position. Treating it as "next stop" yields
    uniformly negative ETAs, which is how this was caught.

    What it IS good for is `delay`, which is live and accurate. The caller
    applies that delay to the scheduled time of the genuinely-next stop.

    Other AT shape quirks: `stop_time_update` is a single object rather than the
    spec's list, and `arrival` is often absent while `departure` is present.
    """
    out: dict[str, dict] = {}
    for e in entities:
        tu = e.get("trip_update") or {}
        tid = (tu.get("trip") or {}).get("trip_id")
        if not tid:
            continue
        stu = tu.get("stop_time_update")
        items = stu if isinstance(stu, list) else [stu] if stu else []
        best = None
        for s in items:
            if not isinstance(s, dict):
                continue
            code = STOP_IDS.get(s.get("stop_id") or "")
            ev = s.get("departure") or s.get("arrival") or {}
            t = num(ev.get("time"))
            if not code or t is None:
                continue
            cand = {"code": code, "t": t, "delay": num(ev.get("delay")),
                    "seq": s.get("stop_sequence")}
            if best is None or cand["t"] > best["t"]:      # latest event seen
                best = cand
        # The vehicle label, when tripupdates carries one. This matters well
        # beyond delays: tripupdates knows a vehicle for roughly 2.5x as many
        # rail trips as vehiclelocations does (measured 28 vs 11), because a
        # train is assigned to its next run before it starts physically
        # reporting against it. Where both feeds had a label they agreed on it
        # in every case, so this is extra coverage, not a conflicting source.
        lab = unit_label(((tu.get("vehicle") or {}).get("label") or ""))
        if lab:
            (best or out.setdefault(tid, {}))["unit"] = lab
            if best is None:
                continue
        if best:
            # trip-level delay is a useful fallback when the stop event omits it
            if best["delay"] is None:
                best["delay"] = num(tu.get("delay"))
            out[tid] = best
    return out


async def collect() -> dict:
    """One poll of the realtime feed -> live train list."""
    t0 = time.time()
    key = api_key()
    if not key:
        import live_infer                                  # keyless fallback
        d = await live_infer.collect()
        d["source"] = "departures-inference"
        return d

    entities, updates = await fetch_both(key)
    pred = index_updates(updates)
    now = time.time()
    trains, stale, no_trip, with_pred, oos = [], 0, 0, 0, 0

    for e in entities:
        v = e.get("vehicle") or {}
        trip = v.get("trip") or {}
        unit = unit_label((v.get("vehicle") or {}).get("label", ""))
        in_service = trip.get("route_id") in RAIL_ROUTE_IDS

        # Keep in-service rail trips AND any train unit without one: AT clears
        # `trip` for stabled stock, empty-stock moves and depot shunts, which
        # are real trains worth showing even though they carry no destination.
        if not in_service and not is_rail_unit(unit):
            continue

        pos = v.get("position") or {}
        lat, lon = num(pos.get("latitude")), num(pos.get("longitude"))
        if lat is None or lon is None:
            continue

        fix = num(v.get("timestamp")) or 0
        age = int(now - fix) if fix else None
        limit = MAX_FIX_AGE_S if in_service else OOS_MAX_FIX_AGE_S
        if age is not None and age > limit:
            stale += 1
            continue

        tid = trip.get("trip_id") or ""
        sched = TRIPS.get(tid) if in_service else None
        if in_service and not sched:
            no_trip += 1

        brg = num(pos.get("bearing"))
        spd = num(pos.get("speed"))
        occ = v.get("occupancy_status")

        if not in_service:
            # No trip means no route, no destination and no ETA. Say where it is
            # instead, and give it its own pseudo-line so it can be filtered off.
            oos += 1
            near, dist = nearest_station(lat, lon)
            trains.append({
                "id": (v.get("vehicle") or {}).get("id") or unit or f"oos-{oos}",
                "unit": unit,
                "route": OOS_ROUTE,
                "line": OOS_NAME,
                "colour": OOS_COLOUR,
                "dest": None,
                "lat": round(lat, 6), "lon": round(lon, 6),
                "bearing": round(brg, 1) if brg is not None else None,
                "speed_kmh": round(spd * 3.6, 1) if spd is not None else None,
                "occupancy": int(occ) if isinstance(occ, (int, float)) else None,
                "from": None, "to": None, "platform": None,
                "eta_s": None, "delay_s": None,
                "predicted": False, "progress": None,
                "fix_age_s": age,
                "trip_id": None,
                "geometry": "gps",
                "in_service": False,
                "near": station(near),
                "near_m": int(dist) if dist is not None else None,
                "moving": bool(spd and spd > 1.0),
            })
            continue

        route = (sched or {}).get("route") or trip["route_id"].rsplit("-", 1)[0]
        meta = ROUTES.get(route, {})

        # AT's trip update reports the stop just DEPARTED plus a live delay.
        # Use the delay against the timetable for the genuinely-next stop; fall
        # back to pure position+timetable inference when a trip has no update.
        delay = None
        p = pred.get(tid)
        prev_code = nxt_code = due = prog = None
        if p and sched:
            delay = int(p["delay"]) if p["delay"] is not None else None
            prev_code, nxt_code, due, prog = next_after(
                sched, p["code"], lat, lon, now, p["delay"] or 0)
            if nxt_code:
                with_pred += 1
            else:                                   # terminus: no stop after this
                prev_code = None
                due = None
        elif sched:
            prev_code, nxt_code, due, prog = next_call(sched, lat, lon, now)

        trains.append({
            "id": tid or (v.get("vehicle") or {}).get("id") or unit or "?",
            "unit": unit,
            "route": route,
            "line": meta.get("name", route),
            "colour": meta.get("colour", "#2f80ed"),
            "dest": (sched or {}).get("head") or None,
            "lat": round(lat, 6), "lon": round(lon, 6),
            "bearing": round(brg, 1) if brg is not None else None,
            "speed_kmh": round(spd * 3.6, 1) if spd is not None else None,
            "occupancy": int(occ) if isinstance(occ, (int, float)) else None,
            # prev/next are the stop just DEPARTED and the stop coming up.
            # They are NOT the origin and terminus, and must never be labelled
            # as such: a train at Penrose heading south showed "Penrose ->
            # Te Papapa", which reads as a two-stop service rather than
            # "Waitemata -> Pukekohe, currently between Penrose and Te Papapa".
            "from": station(prev_code),
            "to": station(nxt_code),
            # The genuine endpoints, straight off the trip's own schedule.
            # calls[0] is where this run started, calls[-1] where it finishes;
            # the last call is the one authoritative destination (trip_headsign
            # is operational junk — see the AT data notes).
            "origin": station((sched.get("calls") or [[None]])[0][0]) if sched else None,
            "terminus": station((sched.get("calls") or [[None]])[-1][0]) if sched else None,
            "platform": platform_of(nxt_code),
            "eta_s": int(due - now) if due else None,
            "delay_s": delay,
            "predicted": bool(p),
            "progress": round(prog, 3) if prog is not None else None,
            "fix_age_s": age,
            "trip_id": tid or None,
            "geometry": "gps",
            "in_service": True,
        })

    trains.sort(key=lambda t: (not t["in_service"], t["line"], t["unit"] or ""))
    return {
        "ts": int(now),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "poll_ms": int((time.time() - t0) * 1000),
        "source": "gtfs-realtime",
        "vehicles_in_feed": len(entities),
        "trip_updates": len(pred),
        # trip_id -> unit for EVERY trip the feeds name a vehicle for, including
        # trips whose train is not yet reporting a position. The journey planner
        # needs this: a service is "assigned" the moment AT names its unit, and
        # saying "not assigned yet" when we actually know the set is a lie we
        # can now avoid on ~2.5x as many trips.
        # Rail only. The raw map is ~920 entries because tripupdates covers the
        # whole network; 892 of those are buses and ferries this app never
        # shows, and shipping 37 KB of them to every client every 10 seconds is
        # pure waste. TRIPS is the baked rail timetable, so membership in it is
        # the exact rail test.
        "assignments": {t: p["unit"] for t, p in pred.items()
                        if p.get("unit") and t in TRIPS},
        "with_prediction": with_pred,
        "stale_dropped": stale,
        "unknown_trip": no_trip,
        "count": len(trains),
        "in_service": len(trains) - oos,
        "out_of_service": oos,
        "trains": trains,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    d = asyncio.run(collect())
    print(json.dumps({k: v for k, v in d.items() if k != "trains"}, indent=1))
    for t in d["trains"]:
        if not t.get("in_service", True):
            print(f"  {(t.get('unit') or '-'):<10} {'not in service':<12} "
                  f"near {str(t.get('near'))[:18]:<18} {t.get('near_m')}m "
                  f"{str(t.get('speed_kmh')):>5} km/h "
                  f"{'moving' if t.get('moving') else 'stopped':<7} "
                  f"fix {t.get('fix_age_s')}s")
            continue
        dl = t.get("delay_s")
        dls = f"{dl:+d}s" if isinstance(dl, int) else "  -"
        print(f"  {(t.get('unit') or '-'):<10} {t['line'][:12]:<12} "
              f"{str(t.get('from'))[:14]:<14} -> {str(t.get('to'))[:14]:<14} "
              f"p{str(t.get('platform') or '-'):<2} "
              f"{str(t.get('speed_kmh')):>5} km/h "
              f"eta {str(t.get('eta_s')):>5}s delay {dls:>6} "
              f"{'pred' if t.get('predicted') else 'schd'} fix {t.get('fix_age_s')}s")
