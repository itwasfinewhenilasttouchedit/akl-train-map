"""Build static rail geometry from AT's open GTFS zip (no key needed).

Outputs output/rail_geo.json:
  stops:  {stop_code: {name, lat, lon}}
  shapes: {shape_id: [[lat,lon], ...]}        (rail shapes only)
  routes: {route_short_name: {shapes: [...], colour}}
  trip_shape: {route_short_name+dir: shape_id}  (longest representative)
  stop_on_shape: {shape_id: {stop_code: cumulative_metres}}
"""
from __future__ import annotations

import csv
import io
import json
import re
import math
import zipfile
import urllib.request
from pathlib import Path
from collections import defaultdict

OUT = Path(__file__).resolve().parent.parent / "output"
OUT.mkdir(exist_ok=True)
ZIP = OUT / "gtfs.zip"
GTFS_URL = "https://gtfs.at.govt.nz/gtfs.zip"

# AT publishes the SAME schedule two ways: this keyless zip, and a keyed
# GTFS API at /gtfs/v3/* (separate from the realtime API). Verified they
# agree exactly - 1789 rail trips, identical trip_ids, and every realtime
# trip_id present in both. So the zip is correct, not second-class.
#
# What the API adds is /gtfs/v3/versions, which names the schedule currently
# in force. The zip carries the same string in feed_info.txt, so the two can
# be compared: if they diverge, this baked geometry predates a timetable
# change and trip_ids from the realtime feed will start missing. That is
# silent and nasty - the map keeps working while the planner quietly stops
# attaching set numbers.
VERSIONS_URL = "https://api.at.govt.nz/gtfs/v3/versions"

LINE_COLOUR = {"S-C": "#e4002b", "E-W": "#f5a623", "O-W": "#00a650",
               "HUIA": "#7a2f8f", "STH": "#e4002b", "WEST": "#00a650"}
# AT publishes some rail route_colors as 000000, which is invisible on a dark map
COLOUR_OVERRIDE = {"HUIA": "#c678dd"}
LINE_NAME = {"S-C": "Southern / Onehunga loop", "E-W": "Eastern / Western",
             "O-W": "Onehunga", "HUIA": "Huia"}


def download() -> None:
    if ZIP.exists() and ZIP.stat().st_size > 1_000_000:
        return
    print("downloading", GTFS_URL)
    with urllib.request.urlopen(GTFS_URL, timeout=600) as r:
        ZIP.write_bytes(r.read())


def haversine(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    dla, dlo = (la2 - la1) * p, (lo2 - lo1) * p
    h = math.sin(dla / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) * math.sin(dlo / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def feed_version(z: zipfile.ZipFile) -> str:
    """The schedule version baked into this zip, or '' if absent."""
    try:
        with z.open("feed_info.txt") as f:
            for r in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
                return (r.get("feed_version") or "").strip()
    except KeyError:
        pass
    return ""


def check_version(z: zipfile.ZipFile) -> None:
    """Warn when the baked schedule is not the one AT is currently running.

    Advisory only: needs an API key, and a build must never fail because a
    key is absent or AT's API is briefly down. A stale schedule degrades the
    planner quietly, so it is worth a loud line in the build log.
    """
    import os
    key = os.environ.get("AT_API_KEY") or ""
    local = feed_version(z)
    if not key:
        print(f"feed_version (baked): {local or 'unknown'} - live check skipped, no AT_API_KEY")
        return
    try:
        req = urllib.request.Request(VERSIONS_URL,
                                     headers={"Ocp-Apim-Subscription-Key": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read()).get("data") or []
        live = [(d.get("attributes") or {}).get("feed_version", "") for d in data]
    except Exception as e:
        print(f"feed_version check skipped ({type(e).__name__}) - baked: {local or 'unknown'}")
        return
    if local and live and local not in live:
        print("WARNING: baked GTFS is NOT the schedule AT is currently running")
        print(f"  baked: {local}")
        print(f"  live : {', '.join(live)}")
        print("  realtime trip_ids may not match; rebuild to refresh.")
    else:
        print(f"feed_version OK: {local or live}")


def main() -> None:
    download()
    z = zipfile.ZipFile(ZIP)
    check_version(z)

    def rows(name):
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))

    routes = {r["route_id"]: r for r in rows("routes.txt")}
    rail_route_ids = {rid for rid, r in routes.items() if r["route_type"] == "2"}

    trips = [t for t in rows("trips.txt") if t["route_id"] in rail_route_ids]
    rail_trip_ids = {t["trip_id"] for t in trips}
    rail_shape_ids = {t["shape_id"] for t in trips if t.get("shape_id")}
    print(f"rail: {len(rail_route_ids)} routes, {len(trips)} trips, {len(rail_shape_ids)} shapes")

    # stop_times for rail trips -> stop sequence per shape, and the rail stop set
    trip_shape = {t["trip_id"]: t["shape_id"] for t in trips}
    shape_stops: dict[str, dict[int, str]] = defaultdict(dict)
    rail_stop_ids: set[str] = set()
    # scheduled runtime between consecutive stops, per trip
    trip_times: dict[str, list[tuple[int, str, int]]] = defaultdict(list)

    def hms(s: str) -> int | None:
        try:
            h, m, sec = s.split(":")
            return int(h) * 3600 + int(m) * 60 + int(sec)
        except Exception:
            return None

    for st in rows("stop_times.txt"):
        tid = st["trip_id"]
        if tid not in rail_trip_ids:
            continue
        rail_stop_ids.add(st["stop_id"])
        sh = trip_shape.get(tid)
        if sh:
            shape_stops[sh][int(st["stop_sequence"])] = st["stop_id"]
        t = hms(st.get("departure_time") or st.get("arrival_time") or "")
        if t is not None:
            trip_times[tid].append((int(st["stop_sequence"]), st["stop_id"], t))

    stops_all = {s["stop_id"]: s for s in rows("stops.txt")}
    stops: dict[str, dict] = {}
    id_to_code: dict[str, str] = {}
    for sid in rail_stop_ids:
        s = stops_all.get(sid)
        if not s or not s.get("stop_code"):
            continue
        code = s["stop_code"]
        id_to_code[sid] = code
        stops[code] = {"name": s["stop_name"].replace(" Train Station", ""),
                       "lat": float(s["stop_lat"]), "lon": float(s["stop_lon"])}
    print("rail stops with codes:", len(stops))

    # Station -> PARENT stop_code, for AT's departures board.
    #
    # serviceinfo/v1/departures/{stop_code} is keyed on stop_code (NOT the GTFS
    # stop_id - passing an id returns an empty board with HTTP 200, which looks
    # exactly like "nothing is running"). Querying the PARENT station code
    # returns every platform's movements in one call, each row tagged with its
    # own platform stop_code. That turns a 4-call fan-out per station into one.
    #
    # Measured: all 48 rail stations resolve to exactly one parent code, no
    # ambiguity. Baked here so the server needs no extra API call at runtime.
    parent_code: dict[str, str] = {}
    by_id = {s["stop_id"]: s for s in stops_all.values()}
    for sid in rail_stop_ids:
        s = stops_all.get(sid)
        if not s:
            continue
        pid = (s.get("parent_station") or "").strip()
        par = by_id.get(pid)
        if not par or not par.get("stop_code"):
            continue
        base = par["stop_name"].replace(" Train Station", "")
        base = re.sub(r"\s+\d+$", "", base)
        parent_code[base] = par["stop_code"]
    print("stations with a parent stop_code:", len(parent_code))

    # shapes
    pts: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for sh in rows("shapes.txt"):
        sid = sh["shape_id"]
        if sid in rail_shape_ids:
            pts[sid].append((int(sh["shape_pt_sequence"]),
                             float(sh["shape_pt_lat"]), float(sh["shape_pt_lon"])))
    shapes = {sid: [[la, lo] for _, la, lo in sorted(v)] for sid, v in pts.items()}
    print("shape polylines:", len(shapes))

    # cumulative distance + project each stop of that shape onto it
    stop_on_shape: dict[str, dict[str, float]] = {}
    shape_cum: dict[str, list[float]] = {}
    for sid, poly in shapes.items():
        cum = [0.0]
        for i in range(1, len(poly)):
            cum.append(cum[-1] + haversine(poly[i - 1], poly[i]))
        shape_cum[sid] = cum
        m: dict[str, float] = {}
        for seq in sorted(shape_stops.get(sid, {})):
            code = id_to_code.get(shape_stops[sid][seq])
            if not code or code in m:
                continue
            tgt = (stops[code]["lat"], stops[code]["lon"])
            best_i = min(range(len(poly)), key=lambda i: haversine(poly[i], tgt))
            m[code] = cum[best_i]
        stop_on_shape[sid] = m

    # representative shape per (route_short_name, direction)
    rep: dict[str, str] = {}
    for t in trips:
        short = routes[t["route_id"]]["route_short_name"] or t["route_id"].split("-")[0]
        key = f"{short}|{t.get('direction_id','0')}"
        sh = t.get("shape_id")
        if not sh or sh not in shapes:
            continue
        if key not in rep or len(shapes[sh]) > len(shapes[rep[key]]):
            rep[key] = sh

    route_meta = {}
    for rid in rail_route_ids:
        short = routes[rid]["route_short_name"] or rid.split("-")[0]
        raw = (routes[rid].get("route_color") or "").strip()
        colour = COLOUR_OVERRIDE.get(short) or (f"#{raw}" if raw and raw != "000000"
                                                else LINE_COLOUR.get(short, "#2f80ed"))
        route_meta[short] = {
            "name": LINE_NAME.get(short, routes[rid].get("route_long_name") or short),
            "colour": colour,
        }

    # median scheduled hop duration between stop code pairs (route-scoped)
    hop_samples: dict[str, list[int]] = defaultdict(list)
    trip_route = {t["trip_id"]: (routes[t["route_id"]]["route_short_name"]
                                 or t["route_id"].split("-")[0]) for t in trips}
    for tid, seq in trip_times.items():
        seq.sort()
        r = trip_route.get(tid, "")
        for i in range(1, len(seq)):
            a, b = seq[i - 1], seq[i]
            ca, cb = id_to_code.get(a[1]), id_to_code.get(b[1])
            dt = b[2] - a[2]
            if ca and cb and 0 < dt < 3600:
                hop_samples[f"{r}|{ca}|{cb}"].append(dt)

    def median(v: list[int]) -> int:
        v = sorted(v)
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) // 2

    hops = {k: median(v) for k, v in hop_samples.items()}
    print("hop durations:", len(hops))

    # candidate shapes per route short name (for live journey matching)
    shapes_by_route: dict[str, list[str]] = defaultdict(list)
    for t in trips:
        short = routes[t["route_id"]]["route_short_name"] or t["route_id"].split("-")[0]
        sh = t.get("shape_id")
        if sh in shapes and sh not in shapes_by_route[short]:
            shapes_by_route[short].append(sh)

    # Per-trip schedule, keyed by GTFS trip_id. The realtime vehicle feed reports
    # trip_id, so this is what turns a GPS point into "next stop + ETA" without
    # polling anything: ordered [stop_code, seconds-after-midnight] pairs.
    #
    # Destination comes from the trip's LAST CALL, not trip_headsign. AT's rail
    # headsigns are operational strings carrying platform numbers, origins and
    # routing notes ("Pukekohe 3 Clockwise to Newmarket 2 Via NKT1, PPK2"), which
    # no passenger wants to read. The final stop is unambiguous.
    trips_rt: dict[str, dict] = {}
    for t in trips:
        tid = t["trip_id"]
        seq = sorted(trip_times.get(tid, []))
        calls = [[id_to_code[sid], secs] for _, sid, secs in seq if sid in id_to_code]
        if not calls:
            continue
        short = routes[t["route_id"]]["route_short_name"] or t["route_id"].split("-")[0]
        last = stops[calls[-1][0]]["name"]
        parts = last.rsplit(" ", 1)
        trips_rt[tid] = {
            "route": short,
            "head": parts[0] if len(parts) == 2 and parts[1].isdigit() else last,
            "dir": t.get("direction_id", "0"),
            "shape": t.get("shape_id") or "",
            "calls": calls,
        }
    print("trips with schedules:", len(trips_rt))

    data = {"parent_code": parent_code,
            "stops": stops, "shapes": shapes, "shape_cum": shape_cum,
            "stop_on_shape": stop_on_shape, "rep_shape": rep, "routes": route_meta,
            "shapes_by_route": dict(shapes_by_route), "hops": hops, "trips": trips_rt,
            # GTFS stop_id -> stop_code. The realtime trip-updates feed reports
            # stop_id, while everything else here is keyed by code.
            "stop_ids": id_to_code}
    p = OUT / "rail_geo.json"
    p.write_text(json.dumps(data))
    print("wrote", p, p.stat().st_size, "bytes")

    # slim copy for the browser: representative polylines only, simplified
    def simplify(poly: list, tol_m: float = 25.0) -> list:
        out = [poly[0]]
        for pt in poly[1:-1]:
            if haversine(out[-1], pt) >= tol_m:
                out.append(pt)
        out.append(poly[-1])
        return [[round(a, 5), round(b, 5)] for a, b in out]

    web_shapes = {}
    for key, sh in rep.items():
        web_shapes[key] = {"route": key.split("|")[0], "path": simplify(shapes[sh])}
    web = {"stops": stops, "lines": web_shapes, "routes": route_meta}
    wp = Path(__file__).resolve().parent.parent / "web" / "rail_geo.json"
    wp.parent.mkdir(exist_ok=True)
    wp.write_text(json.dumps(web))
    print("wrote", wp, wp.stat().st_size, "bytes",
          {k: len(v["path"]) for k, v in web_shapes.items()})
    print("routes:", route_meta)


if __name__ == "__main__":
    main()
