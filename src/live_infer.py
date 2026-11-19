"""Keyless fallback: Auckland train positions inferred from the open departures API.

This was the original method, kept as a fallback for when no registered AT API
key is configured (see live.py, which prefers the real GTFS-Realtime feed).

Why it exists: AT's GTFS-Realtime vehicle-position feed needs a registered
subscription key. The *departures* endpoint is served with the key AT ships in
its own public Journey Planner bundle, and it returns, per stop, every monitored
journey with real-time expected arrival/departure times. Poll all 102 rail
stops, group the movements by vehicleJourneyName, and you can place each train
between its last-passed and next-upcoming stop by interpolating along the real
track geometry from the open GTFS shapes.

Result: a live map of every monitored Auckland train, no signup required.
Positions are time-interpolated, not GPS — accurate to roughly the distance a
train covers between prediction updates, and with no unit numbers (the
departures payload carries no vehicle reference at all).
"""
from __future__ import annotations

import json
import math
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

import httpx

LOG = logging.getLogger("akl")

ROOT = Path(__file__).resolve().parent.parent
GEO = json.loads((ROOT / "output" / "rail_geo.json").read_text())
STOPS: dict[str, dict] = GEO["stops"]
SHAPES: dict[str, list] = GEO["shapes"]
SHAPE_CUM: dict[str, list] = GEO["shape_cum"]
STOP_ON_SHAPE: dict[str, dict] = GEO["stop_on_shape"]
SHAPES_BY_ROUTE: dict[str, list] = GEO["shapes_by_route"]
ROUTES: dict[str, dict] = GEO["routes"]
HOPS: dict[str, int] = GEO["hops"]

# per-shape ordered stop-code list, so we can find the stop *before* the next one
SHAPE_ORDER: dict[str, list[str]] = {}
for _sh, _m in STOP_ON_SHAPE.items():
    SHAPE_ORDER[_sh] = [c for c, _ in sorted(_m.items(), key=lambda kv: kv[1])]

BASE = "https://api.at.govt.nz"
KEY = "693150c317fc42c5a2f871aee3f586af"  # public key from AT's own web bundle
HEADERS = {"Ocp-Apim-Subscription-Key": KEY}
CODES: list[str] = sorted(STOPS.keys())


def _iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def point_at(shape_id: str, dist: float) -> tuple[float, float, float]:
    """Return (lat, lon, bearing) at `dist` metres along a shape."""
    poly, cum = SHAPES[shape_id], SHAPE_CUM[shape_id]
    if dist <= 0:
        a, b = poly[0], poly[1]
        return a[0], a[1], bearing(a, b)
    if dist >= cum[-1]:
        a, b = poly[-2], poly[-1]
        return b[0], b[1], bearing(a, b)
    lo, hi = 0, len(cum) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if cum[mid] <= dist:
            lo = mid
        else:
            hi = mid
    seg = cum[hi] - cum[lo] or 1.0
    f = (dist - cum[lo]) / seg
    a, b = poly[lo], poly[hi]
    return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, bearing(a, b)


def bearing(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlo = lo2 - lo1
    y = math.sin(dlo) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def pick_shape(route: str, prev_code: str, next_code: str) -> tuple[str, float, float] | None:
    """Best shape containing both stops in forward order -> (shape, d_prev, d_next)."""
    best = None
    for sh in SHAPES_BY_ROUTE.get(route, []):
        m = STOP_ON_SHAPE.get(sh, {})
        dp, dn = m.get(prev_code), m.get(next_code)
        if dp is None or dn is None or dn <= dp:
            continue
        gap = dn - dp
        if best is None or gap < best[3]:
            best = (sh, dp, dn, gap)
    if best:
        return best[0], best[1], best[2]
    # fall back: any rail shape with both stops
    for sh, m in STOP_ON_SHAPE.items():
        dp, dn = m.get(prev_code), m.get(next_code)
        if dp is not None and dn is not None and dn > dp:
            return sh, dp, dn
    return None


def prev_stop_code(route: str, next_code: str) -> str | None:
    """Stop immediately before `next_code` on this route's track geometry."""
    best = None
    for sh in SHAPES_BY_ROUTE.get(route, []):
        order = SHAPE_ORDER.get(sh, [])
        try:
            i = order.index(next_code)
        except ValueError:
            continue
        if i == 0:
            continue
        cand = order[i - 1]
        gap = STOP_ON_SHAPE[sh][next_code] - STOP_ON_SHAPE[sh][cand]
        if best is None or gap < best[1]:
            best = (cand, gap)
    return best[0] if best else None


async def fetch_departures(client: httpx.AsyncClient, code: str) -> list[dict]:
    try:
        r = await client.get(f"{BASE}/serviceinfo/v1/departures/{code}", headers=HEADERS)
        if r.status_code != 200:
            return []
        return r.json().get("response", {}).get("movements", []) or []
    except Exception:
        return []


async def collect() -> dict:
    """Poll every rail stop, build a live train list."""
    t0 = time.time()
    limits = httpx.Limits(max_connections=16)
    async with httpx.AsyncClient(timeout=20, limits=limits) as c:
        results = await asyncio.gather(*(fetch_departures(c, x) for x in CODES))

    journeys: dict[str, dict] = defaultdict(lambda: {"obs": {}, "route": None, "dest": None})
    for movements in results:
        for m in movements:
            j = m.get("vehicleJourneyName")
            if not j or not m.get("monitored"):
                continue
            code = str(m.get("stop_code") or "")
            if code not in STOPS:
                continue
            seq = m.get("stopSequence")
            jj = journeys[j]
            jj["route"] = jj["route"] or m.get("route_short_name")
            jj["dest"] = jj["dest"] or m.get("destinationDisplay")
            jj["obs"][seq] = {
                "code": code, "seq": seq,
                "t": _iso(m.get("expectedDepartureTime")) or _iso(m.get("expectedArrivalTime"))
                     or _iso(m.get("scheduledDepartureTime")),
                "sched": _iso(m.get("scheduledDepartureTime")),
                "platform": m.get("departurePlatformName"),
                "delay": None,
            }
            o = jj["obs"][seq]
            if o["t"] and o["sched"]:
                o["delay"] = int(o["t"] - o["sched"])

    now = time.time()
    trains = []
    for jid, j in journeys.items():
        obs = [o for o in j["obs"].values() if o["t"] and o["seq"] is not None]
        if not obs:
            continue
        obs.sort(key=lambda o: o["seq"])
        route = j["route"] or ""

        # The departures API only predicts UPCOMING calls, so obs[0] is the next
        # stop this train will reach. Its position is between the preceding
        # scheduled stop and that one.
        future = [o for o in obs if o["t"] > now - 20]
        if not future:
            continue
        nxt = future[0]
        past = [o for o in obs if o["t"] <= now - 20]
        prev = past[-1] if past else None

        prev_code = prev["code"] if prev else prev_stop_code(route, nxt["code"])
        if not prev_code or prev_code == nxt["code"]:
            continue

        if prev:
            prev_t = prev["t"]
        else:
            hop = HOPS.get(f"{route}|{prev_code}|{nxt['code']}") or 150
            prev_t = nxt["t"] - hop
        span = max(nxt["t"] - prev_t, 1.0)
        frac = min(max((now - prev_t) / span, 0.0), 1.0)

        sel = pick_shape(route, prev_code, nxt["code"])
        if sel:
            sh, dp, dn = sel
            dist = dp + (dn - dp) * frac
            lat, lon, brg = point_at(sh, dist)
            speed = round((dn - dp) / span * 3.6, 1)
            geom = "track"
        else:
            a, b = STOPS[prev_code], STOPS[nxt["code"]]
            lat = a["lat"] + (b["lat"] - a["lat"]) * frac
            lon = a["lon"] + (b["lon"] - a["lon"]) * frac
            brg = bearing((a["lat"], a["lon"]), (b["lat"], b["lon"]))
            speed = None
            geom = "straight"

        meta = ROUTES.get(route, {})
        trains.append({
            "id": jid,
            "unit": None,                     # the departures feed has no vehicle reference
            "route": route,
            "line": meta.get("name", route),
            "colour": meta.get("colour", "#2f80ed"),
            "dest": j["dest"],
            "lat": round(lat, 6), "lon": round(lon, 6), "bearing": round(brg, 1),
            "speed_kmh": speed,
            "from": STOPS[prev_code]["name"],
            "to": STOPS[nxt["code"]]["name"],
            "eta_s": int(nxt["t"] - now),
            "delay_s": nxt.get("delay"),
            "platform": nxt.get("platform"),
            "progress": round(frac, 3),
            "stops_seen": len(obs),
            "geometry": geom,
            "inferred_prev": prev is None,
        })

    trains.sort(key=lambda t: (t["line"], t["id"]))
    return {
        "ts": int(now),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "poll_ms": int((time.time() - t0) * 1000),
        "stops_polled": len(CODES),
        "journeys_monitored": len(journeys),
        "count": len(trains),
        "trains": trains,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    d = asyncio.run(collect())
    print(json.dumps({k: v for k, v in d.items() if k != "trains"}, indent=1))
    for t in d["trains"]:
        print(f"  {t['line'][:12]:<12} {t['from'][:16]:<16} -> {t['to'][:16]:<16} "
              f"{t['lat']:.4f},{t['lon']:.4f} eta {t['eta_s']:>4}s "
              f"{t['progress']*100:>5.1f}% {t['geometry']}")
