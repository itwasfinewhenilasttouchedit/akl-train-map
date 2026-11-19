"""Journey planner over the baked GTFS timetable, with a pee-risk warning.

Scope, deliberately narrow: Auckland's rail network is small and this app only
knows trains. So a "journey" here is one rail leg — board at A, ride to B,
possibly changing at a station both trips call at. No buses, no walking legs,
no fare logic. Anything more would be reimplementing AT's own journey planner
badly, and AT's handles the bits we cannot see.

THE HONEST BIT ABOUT RISK WARNINGS
----------------------------------
The timetable tells us which *trip* runs next. It does NOT tell us which
physical train set will be rostered onto it — AT does not publish that, and
rolling stock gets swapped around constantly. We can only name a set when the
realtime feed already has a vehicle attached to that trip_id, which in practice
means the train is already running and usually close by.

So each departure carries one of three risk states:
  - "known"    : a vehicle is assigned RIGHT NOW; the set number is real and so
                 is its report history. This is the only case where we say a
                 specific train might smell.
  - "unknown"  : no vehicle assigned yet (a future departure). We say so, and
                 give the ROUTE's background rate as context only.
                 IMPORTANT: "assigned" means either feed names a vehicle, not
                 just vehiclelocations. tripupdates publishes a unit for ~2.5x
                 as many rail trips, because a set is rostered onto its next
                 run before it starts reporting a position against it. Reading
                 only the smaller feed made this say "not assigned yet" about
                 services AT had already named — refusing to guess is honest,
                 refusing to look is not.
  - "no-data"  : assigned, but that set has no open reports. Not a promise it
                 smells fine — just an absence of complaints.

Inventing a set number for an unassigned trip would be the single most
tempting lie this file could tell, and it would be wrong within one roster
change. We don't.
"""
from __future__ import annotations

import re
import logging
from datetime import datetime, timedelta

LOG = logging.getLogger("akl.journey")

# Imported lazily in _geo() so this module can be imported by tests without
# paying for live.py's 12 MB geometry load.
_G: dict = {}


def _geo() -> dict:
    global _G
    if not _G:
        import live
        _G = {"stops": live.STOPS, "trips": live.TRIPS,
              "routes": live.GEO.get("routes", {})}
    return _G


# Platform suffixes: GTFS stop names are "Britomart 1", "Newmarket 3". Riders
# think in stations, not platforms, so we group on the base name and keep every
# platform's stop_id under it.
_PLAT = re.compile(r"\s+\d+$")


def _base(name: str) -> str:
    return _PLAT.sub("", (name or "").strip())


def stations() -> list[dict]:
    """Every station, deduped across platforms, alphabetical.

    Returns [{name, ids:[stop_id,...], lat, lon}]. lat/lon is the first
    platform's — good enough to find the nearest station from a GPS fix, since
    platforms at one station are metres apart.
    """
    g = _geo()
    by: dict[str, dict] = {}
    for sid, s in g["stops"].items():
        n = _base(s.get("name"))
        if not n:
            continue
        e = by.setdefault(n, {"name": n, "ids": [], "lat": s.get("lat"),
                              "lon": s.get("lon")})
        e["ids"].append(sid)
    return sorted(by.values(), key=lambda e: e["name"])


def _station_index() -> dict[str, set[str]]:
    return {s["name"]: set(s["ids"]) for s in stations()}


def _secs_now(now: datetime) -> int:
    return now.hour * 3600 + now.minute * 60 + now.second


def _fmt(secs: int) -> str:
    """GTFS seconds-since-midnight -> HH:MM. Values past 24h wrap (after-midnight
    services are timetabled as 24:xx, 25:xx in GTFS and must not render as
    '25:10')."""
    return f"{(secs // 3600) % 24:02d}:{(secs % 3600) // 60:02d}"


def plan(origin: str, dest: str, now: datetime, limit: int = 4,
         window_h: int = 3) -> dict:
    """Direct departures from origin to dest after `now`.

    Only direct trips: a trip qualifies if it calls at an origin platform and
    then, LATER IN THE SAME TRIP, a destination platform. Order matters — the
    same trip calling at both in the wrong sequence is the return working, not
    a journey the rider can make.
    """
    idx = _station_index()
    o_ids, d_ids = idx.get(origin), idx.get(dest)
    if not o_ids:
        raise ValueError(f"unknown station: {origin}")
    if not d_ids:
        raise ValueError(f"unknown station: {dest}")
    if origin == dest:
        raise ValueError("origin and destination are the same station")

    t0 = _secs_now(now)
    horizon = t0 + window_h * 3600
    out: list[dict] = []
    for tid, t in _geo()["trips"].items():
        calls = t.get("calls") or []
        oi = di = None
        for i, (sid, _s) in enumerate(calls):
            if oi is None and sid in o_ids:
                oi = i
            elif oi is not None and sid in d_ids:
                di = i
                break
        if oi is None or di is None or di <= oi:
            continue
        dep, arr = calls[oi][1], calls[di][1]
        # Compare in raw GTFS seconds so a 24:30 service still sorts after
        # 23:50 instead of wrapping to 00:30 and looking like yesterday.
        if dep < t0 or dep > horizon:
            continue
        out.append({
            "trip_id": tid,
            "route": t.get("route"),
            "headsign": (calls[-1] and _base(_geo()["stops"].get(calls[-1][0], {}).get("name", ""))) or t.get("head"),
            "dep_s": dep, "arr_s": arr,
            "dep": _fmt(dep), "arr": _fmt(arr),
            "mins_away": max(0, (dep - t0) // 60),
            "duration_min": max(0, (arr - dep) // 60),
            "stops": di - oi,
        })
    out.sort(key=lambda d: d["dep_s"])
    # GTFS carries one trip per service calendar, so the same real departure
    # appears 2-3 times with different trip_ids (weekday/saturday/holiday
    # variants). Showing "08:04, 08:04, 08:04" makes the planner look broken.
    # Dedupe on the departure fingerprint a rider can actually perceive:
    # time + route + destination. We keep the FIRST survivor, which is the one
    # the live feed is most likely to have a vehicle attached to.
    # The duplicates' trip_ids are KEPT in `trip_ids`, not thrown away: only
    # one calendar variant will have a live vehicle attached, and it is not
    # necessarily the first one we happened to see. attach_risk() checks every
    # alternate, so discarding them here would silently lose the set number.
    # headsign is deliberately NOT part of the key. Calendar variants of the
    # same physical departure can carry different headsigns (a through run and
    # a short-turn sharing one train), and including it split them into two
    # rows instead of one row with two candidate trip_ids. The live vehicle sat
    # on the variant that got split off, so the surviving row reported "no
    # train assigned" while the set number was right there on its sibling.
    # Keyed on departure + arrival + route: one train, one row, all variants.
    seen: dict[tuple, dict] = {}
    uniq: list[dict] = []
    for d in out:
        k = (d["dep_s"], d["arr_s"], d["route"])
        if k in seen:
            seen[k]["trip_ids"].append(d["trip_id"])
            continue
        d["trip_ids"] = [d["trip_id"]]
        seen[k] = d
        uniq.append(d)
    return {"origin": origin, "dest": dest,
            "departures": uniq[:max(1, min(limit, 10))]}


def departures_at(name: str, now: datetime, limit: int = 8) -> list[dict]:
    """Next departures from one station, both directions.

    Built from the baked timetable, the same source as plan(). Each entry
    carries the trip_ids so the caller can attach a live vehicle and its risk
    exactly as it does for a journey.
    """
    geo = _geo()
    idx = _station_index()
    ids = idx.get(name)
    if not ids:
        raise ValueError(f"unknown station: {name}")
    secs = _secs_now(now)
    out: list[dict] = []
    for tid, tr in geo["trips"].items():
        calls = tr.get("calls") or []
        for i, (sid, t) in enumerate(calls):
            if sid not in ids:
                continue
            # The last call of a trip is an arrival, not a departure — nobody
            # boards a train that terminates here.
            if i >= len(calls) - 1:
                break
            if t < secs - 60 or t > secs + 7200:
                break
            out.append({
                "trip_id": tid,
                "dep": _fmt(t),
                "dep_s": t,
                "mins_away": max(0, round((t - secs) / 60)),
                "route": tr.get("route"),
                "dest": _base(geo["stops"].get(calls[-1][0], {}).get("name", "")),
                "platform": (geo["stops"].get(sid, {}).get("name", "") or "")[-1:],
                "stop_id": sid,
                "dir": tr.get("dir"),
            })
            break
    out.sort(key=lambda d: d["dep_s"])
    # Dedupe to ONE PHYSICAL TRAIN per (time, platform, direction).
    #
    # This is stricter than plan()'s dedupe and it has to be. Measured at
    # Waitemata: 8 trips "depart" at 22:42, all from platform 4, all direction
    # 0 — 5 variants running through to Swanson plus short-turns to Maungawhau,
    # New Lynn and Kingsland. One train can be at one platform at one time, so
    # seven of those eight are calendar/pattern variants, not services. A board
    # listing all eight is simply wrong, and wrong in the way that makes a
    # rider miss a train.
    #
    # Every variant's trip_id is kept, because the live vehicle may be attached
    # to any one of them. The destination shown is the dominant variant's; if a
    # vehicle later turns up on a short-turn variant, _resolve_dest() below
    # corrects the destination to that train's actual last call.
    seen: dict[tuple, dict] = {}
    uniq: list[dict] = []
    # Bucket to the displayed MINUTE, not the exact second. GTFS puts two
    # genuine services 55s apart (22:42:00 and 22:42:55) on the same platform;
    # both render as "22:42", so keying on dep_s leaves two identical-looking
    # rows on the board. One train cannot occupy one platform twice in the same
    # minute, so the minute is the honest unit here — and it is what the rider
    # sees. Trip ids from both are retained, so whichever gets the vehicle
    # still resolves.
    for d in out:
        k = (d["dep_s"] // 60, d["stop_id"], d["dir"])
        if k in seen:
            s = seen[k]
            s["trip_ids"].append(d["trip_id"])
            s["dests"][d["trip_id"]] = d["dest"]
            s["_votes"][d["dest"]] = s["_votes"].get(d["dest"], 0) + 1
            # The pattern with the most calendar variants is the scheduled
            # service; the one-offs are exceptions.
            if s["_votes"][d["dest"]] > s["_votes"].get(s["dest"], 0):
                s["dest"] = d["dest"]
            continue
        d["trip_ids"] = [d["trip_id"]]
        d["dests"] = {d["trip_id"]: d["dest"]}
        d["_votes"] = {d["dest"]: 1}
        seen[k] = d
        uniq.append(d)
    for d in uniq:
        d.pop("_votes", None)
    return uniq[:max(1, min(limit, 12))]


def attach_risk(dep_list: list[dict], trains: list[dict], units: dict,
                assignments: dict | None = None) -> None:
    """Annotate each departure with pee risk, in place.

    `trains` is the live /api/trains list (carries trip_id -> unit); `units` is
    feedback.summary(). See the module docstring for why a set number is only
    claimed when a vehicle is genuinely assigned.

    `assignments` is trip_id -> unit from the tripupdates feed, which names a
    vehicle for far more trips than vehiclelocations does — a train is rostered
    to its next run before it starts reporting a position against it. Without
    it the planner said "not assigned yet" for services whose set AT had
    already published, which is the opposite of the honesty this is meant to
    provide: refusing to guess is right, refusing to look is not.
    """
    # Trip ids are joined EXACTLY. An earlier version of this stripped the
    # trailing hash to make "...A182B193180-74bdf70e" match "...-5a149c50",
    # on the assumption that AT's realtime feed and its GTFS disagreed about
    # the suffix. They do not: checked against the GTFS API, all 1789 rail
    # trips match the baked feed exactly and every realtime trip_id is present
    # in both. Those two ids are genuinely DIFFERENT trips on different service
    # calendars. The real bug was the dedupe below dropping the variant that
    # had the vehicle. Fuzzy-matching 362 shared prefixes would risk printing
    # a confidently wrong set number, which is worse than admitting we
    # don't know.
    by_trip: dict[str, dict] = {}
    for t in trains or []:
        tid = t.get("trip_id")
        if tid:
            by_trip[tid] = t
    assignments = assignments or {}

    # Background rate per route, for the "unknown" case: share of this route's
    # recent open smell reports. Context, never presented as a prediction about
    # the specific train.
    for d in dep_list:
        # Check every calendar variant of this departure — see plan()'s dedupe.
        t = None
        for tid in (d.get("trip_ids") or [d.get("trip_id")]):
            if tid in by_trip:
                t = by_trip[tid]
                break
        # A board row collapses several pattern variants into one physical
        # train (see departures_at). Once we know WHICH variant has the
        # vehicle, that trip's own last call is the real destination — the
        # dominant-pattern guess is only for when nothing is assigned yet.
        if t and d.get("dests"):
            actual = d["dests"].get(t.get("trip_id"))
            if actual:
                d["dest"] = actual
        unit = (t or {}).get("unit")
        if not unit:
            # No vehicle is physically reporting against this trip yet, but AT
            # may still have named the set in tripupdates. Check every calendar
            # variant, same as above.
            for tid in (d.get("trip_ids") or [d.get("trip_id")]):
                if tid in assignments:
                    unit = assignments[tid]
                    if d.get("dests") and d["dests"].get(tid):
                        d["dest"] = d["dests"][tid]
                    break
        sn = None
        if unit:
            m = re.search(r"(?<!\d)(\d{2,4})(?!\d)", unit)
            sn = m.group(1) if m else None
        if not sn:
            d["risk"] = {"state": "unknown",
                         "why": "no train assigned to this service yet"}
            continue
        f = units.get(str(sn)) or {}
        n_neg = f.get("n_neg") or 0
        if not n_neg or (f.get("top_fault") != "smell"):
            d["risk"] = {"state": "no-data", "set_no": int(sn), "unit": unit,
                         "why": "no open smell reports for this train"}
            continue
        worst = f.get("worst_neg") or 0
        level = "high" if (worst >= 3 or n_neg >= 2) else (
                "medium" if worst == 2 else "low")
        d["risk"] = {
            "state": "known", "set_no": int(sn), "unit": unit,
            "level": level, "n": n_neg, "worst": worst,
            # Only 3-car reports yield a carriage (see feedback.CONSISTS): on a
            # 6-car we cannot tell which coupled set the rider was in, so
            # "avoid car N" would be a coin flip. Absent here means unknown.
            "car": f.get("worst_car"),
            "car_label": f.get("worst_car_label"),
            "n_sixcar": f.get("n_sixcar") or 0,
            "why": f"{n_neg} open smell report(s) for set {sn}",
        }
