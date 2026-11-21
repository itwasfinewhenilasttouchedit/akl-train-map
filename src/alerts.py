"""Rail service alerts from AT's GTFS-realtime `servicealerts` feed.

This feed was not consumed at all before. A field census of every reachable AT
endpoint found 50 populated fields we ignored, and this was the largest gap by
user value: measured on one live poll there were **161 alerts, 12 of them
rail-relevant, 9 marked SEVERE** — real, specific and actionable, e.g.

    "The South City line is closed between Pukekohe and Puhinui after 9:30pm
     on Monday 23 November because of overnight maintenance works."

Without it the map cheerfully shows trains running to a station that is shut.

DESIGN NOTES

`active_period` is the whole point. Most of these alerts are scheduled night
works published days ahead, so an alert that is merely *present* is not an
alert that is *in force*. Everything is bucketed as:

    now      -- active_period covers this instant
    upcoming -- starts within UPCOMING_WINDOW_S (default 24h)
    (older/further-out alerts are dropped entirely)

Showing a Wednesday closure as a live warning on Saturday would be the same
class of dishonesty as drawing a guessed train position.

`informed_entity` is a list of what the alert applies to, any mix of
`route_id`, `stop_id` and `trip.trip_id`. Rail relevance is decided ONLY by
matching against our own baked rail route ids and rail stop ids -- never by
string-matching the text, because "Newmarket" appears in plenty of bus alerts.

Cache TTL is deliberately long (5 min). These are human-authored notices that
change on the order of hours; polling them at the map's 20s cadence would burn
the free-tier quota for nothing. The AT quota incident is in MEMORY.md.

Failure is non-fatal by design: on any error the last good list is served and
`error` is set. An alerts outage must never blank the map.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

URL = "https://api.at.govt.nz/realtime/legacy/servicealerts"

# Human-authored notices; they do not change on a 20-second cadence.
CACHE_TTL_S = 300
# How far ahead to call an alert "upcoming" rather than dropping it.
UPCOMING_WINDOW_S = 24 * 3600

# [fetched_at, payload, error]
_cache: list[Any] = [0.0, None, None]

# Severity ordering for display. AT uses INFO/WARNING/SEVERE; anything
# unrecognised sorts last rather than being guessed at.
_SEV_RANK = {"SEVERE": 0, "WARNING": 1, "INFO": 2}


def _txt(node: Any) -> str:
    """Pull the English string out of a GTFS-RT TranslatedString.

    Shape is {"translation": [{"text": ..., "language": "en"}]}. Prefer an
    explicitly English entry, else take the first; AT has only ever shipped
    `en` but the field is a list and must be treated as one.
    """
    if not isinstance(node, dict):
        return ""
    tr = node.get("translation")
    if not isinstance(tr, list):
        return ""
    for t in tr:
        if isinstance(t, dict) and str(t.get("language", "")).lower().startswith("en"):
            return str(t.get("text") or "").strip()
    for t in tr:
        if isinstance(t, dict) and t.get("text"):
            return str(t["text"]).strip()
    return ""


def _num(v: Any) -> float | None:
    """AT ships numerics as strings on some entities. Never trust the type."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify(alert: dict, now: float) -> str | None:
    """'now' | 'upcoming' | None, from active_period.

    An alert with no active_period at all is treated as in force: AT uses that
    for open-ended disruptions, and suppressing those would hide exactly the
    alerts that matter most.
    """
    periods = alert.get("active_period")
    if not isinstance(periods, list) or not periods:
        return "now"
    best = None
    for p in periods:
        if not isinstance(p, dict):
            continue
        start, end = _num(p.get("start")), _num(p.get("end"))
        if start is not None and start <= now and (end is None or end >= now):
            return "now"
        if start is not None and now < start <= now + UPCOMING_WINDOW_S:
            best = "upcoming"
    return best


def _rail_entities(alert: dict, rail_routes: set[str],
                   rail_stop_ids: set[str]) -> tuple[bool, list[str], list[str]]:
    """Is this alert about rail, and which routes/stops does it name?

    Decided on ids only. The free-text deliberately plays no part: station
    names show up in bus-replacement alerts constantly.
    """
    routes: list[str] = []
    stops: list[str] = []
    ies = alert.get("informed_entity")
    if not isinstance(ies, list):
        return False, routes, stops
    for ie in ies:
        if not isinstance(ie, dict):
            continue
        rid = str(ie.get("route_id") or "")
        sid = str(ie.get("stop_id") or "")
        if rid and rid in rail_routes:
            routes.append(rid)
        if sid and sid in rail_stop_ids:
            stops.append(sid)
    return bool(routes or stops), routes, stops


async def fetch(key: str, rail_routes: set[str], rail_stop_ids: set[str],
                stop_code: dict[str, str] | None = None,
                station_name=None) -> dict:
    """Rail alerts, bucketed into now/upcoming. Cached; never raises.

    `stop_code`/`station_name` translate AT stop_ids into the station names the
    rest of the app uses, so the client never has to know AT's ids.
    """
    now = time.time()
    if _cache[1] is not None and now - _cache[0] < CACHE_TTL_S:
        return _cache[1]

    try:
        headers = {"Ocp-Apim-Subscription-Key": key, "Accept-Encoding": "gzip"}
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get(URL, headers=headers)
            r.raise_for_status()
            body = r.content
            # httpx normally decompresses, but AT has returned a gzip body with
            # no/err Content-Encoding before; sniff the magic bytes to be sure.
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            raw = json.loads(body.decode())
    except Exception as e:                                    # noqa: BLE001
        log.warning("servicealerts fetch failed: %s", e)
        if _cache[1] is not None:
            stale = dict(_cache[1])
            stale["error"] = str(e)[:200]
            stale["stale"] = True
            return stale
        return {"now": [], "upcoming": [], "error": str(e)[:200],
                "total_feed": 0, "generated": int(now)}

    entities = ((raw.get("response") or {}).get("entity")) or []
    buckets: dict[str, list[dict]] = {"now": [], "upcoming": []}

    for e in entities:
        if not isinstance(e, dict):
            continue
        a = e.get("alert")
        if not isinstance(a, dict):
            continue
        is_rail, routes, stops = _rail_entities(a, rail_routes, rail_stop_ids)
        if not is_rail:
            continue
        when = classify(a, now)
        if when not in buckets:
            continue

        codes = []
        for sid in stops:
            code = (stop_code or {}).get(sid)
            nm = station_name(code) if (station_name and code) else None
            if nm and nm not in codes:
                codes.append(nm)

        header = _txt(a.get("header_text"))
        desc = _txt(a.get("description_text"))
        if not header and not desc:
            continue                      # nothing to show a human

        periods = [p for p in (a.get("active_period") or []) if isinstance(p, dict)]
        buckets[when].append({
            "id": str(e.get("id") or ""),
            "header": header,
            "description": desc,
            "effect": a.get("effect"),
            "effect_detail": _txt(a.get("effect_detail")) or None,
            "cause": a.get("cause"),
            "severity": a.get("severity_level"),
            "routes": sorted(set(routes)),
            "stations": codes,
            "starts": int(_num(periods[0].get("start")) or 0) if periods else None,
            "ends": int(_num(periods[0].get("end")) or 0) or None if periods else None,
        })

    for b in buckets.values():
        b.sort(key=lambda x: (_SEV_RANK.get(str(x.get("severity")), 9),
                              x.get("starts") or 0))

    out = {
        "now": buckets["now"],
        "upcoming": buckets["upcoming"],
        "total_feed": len(entities),
        "generated": int(now),
        "error": None,
    }
    _cache[0], _cache[1], _cache[2] = now, out, None
    return out
