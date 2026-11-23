"""Auckland live train map — web server.

No API signup needed: see src/live.py for how positions are derived from
Auckland Transport's open departures endpoint plus the open GTFS feed.

Run:  .venv/bin/python src/server.py      ->  http://localhost:8731
"""
from __future__ import annotations

import os
import sys
import json
import math
import time
import hashlib
import secrets
import asyncio
import logging
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive   # noqa: E402
import live      # noqa: E402
import alerts    # noqa: E402
import board
import consist   # noqa: E402
import consist_at   # noqa: E402
import order_latch  # noqa: E402
import journey   # noqa: E402
import feedback  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
LOG = logging.getLogger("akl.server")

WEB = Path(__file__).resolve().parent.parent / "web"

# The app shell (index.html, admin.html) must NEVER be heuristically cached.
#
# FileResponse sends `etag` and `last-modified` but no `Cache-Control`. With no
# explicit directive a browser is free to invent a freshness lifetime from
# last-modified (RFC 9111 "heuristic expiration"), and mobile browsers are
# aggressive about it — iOS Safari in particular will keep serving a stale
# shell for hours and never send the conditional request that the etag exists
# to answer. This is how a shipped, verified fix stays invisible on a phone
# while being provably correct on the server.
#
# `no-cache` (not `no-store`) is the right directive: the response may still be
# stored, but it must be revalidated every time. The etag then makes that a
# cheap 304 rather than a re-download of ~115 KB.
#
# Only the shell is covered. /static/* keeps normal caching because those are
# genuinely static, and the API responses set their own policy.
_SHELL_CACHE = {
    "Cache-Control": "no-cache, must-revalidate",
    "Pragma": "no-cache",
}

POLL_SECONDS = 20

STATE: dict = {"data": None, "error": None, "fetched": 0}

# Last good payload, staged on the volume next to the feedback DB (not in the
# image: it is state, and the image is read-only in spirit). One server-side
# poller already fans out to every browser, so this is purely about surviving a
# RESTART: without it a redeploy serves an empty map until the first poll lands,
# which is how "0 in service" reached a visitor. Positions a few minutes old,
# honestly labelled with their age, beat no map at all.
CACHE = Path(os.environ.get("TRAINS_CACHE")
             or (Path(os.environ["FEEDBACK_DB"]).parent / "last_trains.json"
                 if os.environ.get("FEEDBACK_DB")
                 else Path(__file__).resolve().parent.parent / "output" / "last_trains.json"))

# Don't serve a cache older than this on boot. Train positions go from "useful"
# to "actively misleading" fairly fast, and a machine that has been down
# overnight should show the warming state rather than yesterday's rush hour.
CACHE_MAX_AGE_S = 900

# Latched 6-car front/rear orders. Persisted beside the cache (and so on the
# volume in production): the order is derived only while a train MOVES, so
# losing the store on a redeploy would blank the order for every stationary
# 6-car until it next pulls away. State, not image content.
LATCH_PATH = Path(os.environ.get("ORDER_LATCH")
                  or (Path(os.environ["FEEDBACK_DB"]).parent / "order_latch.json"
                      if os.environ.get("FEEDBACK_DB")
                      else Path(__file__).resolve().parent.parent / "output" / "order_latch.json"))

_LATCH: order_latch.OrderLatch | None = None


def _latch() -> "order_latch.OrderLatch":
    global _LATCH
    if _LATCH is None:
        _LATCH = order_latch.OrderLatch(LATCH_PATH)
    return _LATCH


def _save_cache(d: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(CACHE)          # atomic: never a half-written cache
    except Exception as e:
        LOG.warning("could not stage cache: %s", e)


def _load_cache() -> None:
    """Seed STATE from the staged payload so a restart has data immediately."""
    try:
        if not CACHE.exists():
            return
        d = json.loads(CACHE.read_text())
        age = int(time.time()) - int(d.get("ts", 0))
        if age > CACHE_MAX_AGE_S:
            LOG.info("staged cache is %ds old, ignoring", age)
            return
        STATE["data"] = d
        STATE["fetched"] = int(d.get("ts", 0))
        LOG.info("warm start: %d trains from staged cache, %ds old",
                 d.get("count", 0), age)
    except Exception as e:
        LOG.warning("could not load staged cache: %s", e)


async def poller() -> None:
    while True:
        try:
            STATE["data"] = await live.collect()
            STATE["fetched"] = int(time.time())
            STATE["error"] = None
            await asyncio.get_running_loop().run_in_executor(None, _annotate_consists)
            _save_cache(STATE["data"])
            # Archive AFTER annotation so consist/order/source land in history,
            # and in the executor because SQLite writes are blocking. Failures
            # are swallowed inside record(): the live map is the product.
            a = archive.get()
            if a is not None:
                await asyncio.get_running_loop().run_in_executor(
                    None, a.record, STATE["data"])
            LOG.info("poll ok: %d trains in %dms",
                     STATE["data"]["count"], STATE["data"]["poll_ms"])
        except Exception as e:
            STATE["error"] = f"{type(e).__name__}: {e}"
            LOG.warning("poll failed: %s", STATE["error"])
            # Record the gap explicitly: a hole in `obs` must not be
            # indistinguishable from "no trains were running".
            a = archive.get()
            if a is not None:
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, a.record_failure, STATE["error"])
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.sleep(POLL_SECONDS)


def _annotate_consists() -> None:
    """Attach consist info to the train payload, and rescue trailing halves.

    THE BUG THIS FIXES
    ------------------
    AT attaches the trip_id to exactly one half of a coupled 6-car service.
    The other half reports position only, so live.py saw no trip, no route and
    no destination and classified it `in_service: False` — drawing the front
    half of a moving 6-car service as grey stabled stock, at line speed, with
    no destination. Measured: 19 of 19 six-car services were affected.

    The out-of-service rule is right for genuine depot moves; it just never
    asked whether the unit was coupled to a train that IS in service. Now it
    does, using AT's published unit list.

    A rescued half keeps `in_service: False` renamed honestly — it is given
    the partner's route, line, colour and destination. Exactly one half of the
    pair is then marked `trailing_half: True` so the client draws ONE train for
    the service instead of two icons telling different stories.

    WHICH half is marked is decided by GEOMETRY, not by trip ownership.
    Ground truth 2026-11-23: AT put the trip on the REAR set on both observed
    6-car services (1033 of 1033+917; 1005 of 1005+1196). The old rule marked
    the trip-less half as trailing, so it labelled the physical FRONT of the
    train as trailing and drew the icon up to ~72 m behind the real leading
    cab. One record was observed carrying `trailing_half: True` and
    `consist_position: "front"` at the same time.

    So: if either half has a `consist_position` from the geometric projection,
    the half at "rear" is the one marked trailing and the "front" half is
    drawn. Only when geometry is unavailable (both halves stationary, which is
    exactly when it returns None) do we fall back to the old trip-ownership
    guess — and `trailing_source` records which rule was used.
    """
    d = STATE.get("data")
    if not d:
        return
    trains = d.get("trains") or []
    if not trains:
        return

    couples = _consist_map()
    if not couples:
        return

    by_set = {}
    for t in trains:
        sn = feedback.set_number(t.get("unit"))
        if sn:
            by_set[str(sn)] = t

    rescued = 0
    pairs_rescued: list[tuple[dict, dict]] = []
    for t in trains:
        sn = feedback.set_number(t.get("unit"))
        pair = couples.get(str(sn)) if sn else None
        if not pair:
            continue
        t["consist"] = pair.get("consist")
        t["consist_source"] = pair.get("source")
        t["partner_set"] = pair.get("partner")
        t["consist_position"] = pair.get("position")
        # "inferred" = geometry resolved it from THIS poll's positions;
        # "latched" = remembered from an earlier moving sample on this same
        # trip. Exposed so the ordering's provenance stays auditable.
        if pair.get("order_source"):
            t["order_source"] = pair["order_source"]

        partner = by_set.get(str(pair.get("partner") or ""))
        if not partner or t.get("in_service") or not partner.get("in_service"):
            continue
        # This half has no trip but its partner is working a service: it is the
        # other end of that train, not stabled stock.
        t["trailing_half"] = True
        t["trailing_source"] = "no_trip"
        t["service_set"] = feedback.set_number(partner.get("unit"))
        for k in ("route", "line", "colour", "dest", "to", "from",
                  "platform", "eta_s", "delay_s", "trip_id",
                  "origin", "terminus"):
            if k in partner:
                t[k] = partner[k]
        rescued += 1
        pairs_rescued.append((t, partner))

    # Second pass: let geometry, not trip ownership, decide which half is the
    # tail. The trip is known to sit on the rear set at least some of the time,
    # so "has no trip" is not "is behind" — see this function's docstring.
    regeom = 0
    for half, partner in pairs_rescued:
        hp, pp = half.get("consist_position"), partner.get("consist_position")
        front = None
        if hp == "front" or pp == "rear":
            front = half
        elif hp == "rear" or pp == "front":
            front = partner
        if front is None:
            continue  # both stationary: geometry has nothing, keep the guess
        rear = partner if front is half else half
        front["trailing_half"] = False
        front["trailing_source"] = "geometry"
        rear["trailing_half"] = True
        rear["trailing_source"] = "geometry"
        if rear is partner:
            # The drawn icon moves to the trip-less half, so it must carry the
            # identity of the service it is physically leading — including
            # `in_service`, or the client's OOS filter would hide the only
            # icon for a running train.
            front["in_service"] = True
            front.setdefault("service_set", feedback.set_number(partner.get("unit")))
            regeom += 1

    if rescued:
        # Recount: these are no longer "out of service" in any sense a rider
        # would recognise, even though they carry no trip of their own.
        d["trailing_halves"] = rescued
        d["out_of_service"] = max(0, int(d.get("out_of_service") or 0) - rescued)
        d["in_service"] = int(d.get("in_service") or 0) + rescued
    LOG.info("consist: %s; rescued %d trailing half/halves (%d re-ordered by geometry)",
             consist_at.summarise(couples), rescued, regeom)


app = FastAPI(title="Auckland live train map")


@app.on_event("startup")
async def _startup() -> None:
    feedback.init_db()
    archive.init()
    _load_cache()
    asyncio.create_task(poller())


@app.get("/api/trains")
async def api_trains() -> JSONResponse:
    d = STATE["data"]
    if d is None:
        # First poll has not finished yet (takes ~3-8s from a cold start).
        # `warming` is an explicit flag rather than something the client has to
        # infer from a zero count: a genuine "no trains anywhere" reading and
        # "I have not looked yet" are completely different claims, and the
        # client must not present the second as the first. age_s is deliberately
        # absent — there is no data, so it has no age.
        return JSONResponse({"count": 0, "in_service": 0, "out_of_service": 0,
                             "trains": [], "warming": True,
                             "error": STATE["error"]})
    out = dict(d)
    out["age_s"] = int(time.time()) - d["ts"]
    out["warming"] = False
    out["error"] = STATE["error"]
    return JSONResponse(out)


@app.get("/api/health")
async def api_health() -> dict:
    d = STATE["data"]
    return {"ok": d is not None and STATE["error"] is None,
            "trains": d["count"] if d else 0,
            "source": d.get("source") if d else None,
            # only the keyless fallback polls stations; the realtime feed is one request
            "stops_polled": d.get("stops_polled") if d else 0,
            "vehicles_in_feed": d.get("vehicles_in_feed") if d else None,
            "with_prediction": d.get("with_prediction") if d else None,
            "poll_ms": d["poll_ms"] if d else None,
            "feedback": feedback.stats()["reports"],
            "last_fetch": STATE["fetched"], "error": STATE["error"]}


@app.get("/api/archive")
async def api_archive(unit: str | None = None, limit: int = 200) -> JSONResponse:
    """Archive stats, or one unit's recorded history.

    Read-only and deliberately dull: the archive is for later analysis, not a
    live feature. `?unit=AMP 281` returns that unit's observations newest
    first — the query the (unit_pk, ts) primary key is shaped for.
    """
    a = archive.get()
    if a is None:
        return JSONResponse({"enabled": False})
    if unit:
        return JSONResponse({"enabled": True, "unit": unit,
                             "rows": a.history(unit, min(int(limit), 2000))})
    return JSONResponse(a.stats())


# ---------------------------------------------------------------- feedback

# Crude per-IP rate limit. This is not security — anyone determined can get
# round it — it exists so one stuck client or bored passer-by can't fill the
# table in an afternoon. Behind Authelia the real gate is the login.
_RATE: dict[str, list] = {}
RATE_N, RATE_WINDOW_S = 10, 600

# The bucket key is a SALTED HASH of the IP, never the IP. The salt is random
# per process and never persisted, so the keys are useless to anyone who gets
# a memory dump, and they cannot be reversed or correlated across restarts.
# A rate limit only needs to know "same sender as before", not who.
_RATE_SALT = secrets.token_bytes(16)


def _bucket(ip: str) -> str:
    return hashlib.blake2b(ip.encode("utf-8", "replace"),
                           key=_RATE_SALT, digest_size=16).hexdigest()


def _rate_ok(ip: str) -> bool:
    """Rate-limit on a hashed bucket key and forget the IP entirely."""
    key = _bucket(ip)
    now = time.time()
    hits = [t for t in _RATE.get(key, []) if now - t < RATE_WINDOW_S]
    # Drop buckets that have fully aged out rather than growing a dict of
    # every visitor forever — expiry is also data minimisation.
    for k in [k for k, v in _RATE.items() if not v or now - v[-1] > RATE_WINDOW_S]:
        if k != key:
            _RATE.pop(k, None)
    _RATE[key] = hits
    if len(hits) >= RATE_N:
        return False
    hits.append(now)
    return True


@app.get("/api/feedback/meta")
async def api_feedback_meta() -> dict:
    """Categories and severities, so the form is built from one source."""
    return {"categories": feedback.CATEGORIES,
            "severities": feedback.SEVERITIES,
            "carriages": feedback.CARRIAGES,
            "consists": {str(k): v for k, v in feedback.CONSISTS.items()},
            "max_car": feedback.MAX_CAR,
            "people_categories": sorted(feedback.PEOPLE_CATEGORIES),
            "behaviours": feedback.BEHAVIOURS,
            "clear_min": feedback.CLEAR_MIN,
            "clear_margin": feedback.CLEAR_MARGIN,
            "max_note": feedback.MAX_NOTE}


@app.get("/api/feedback")
async def api_feedback(unit: str | None = None, limit: int = 50) -> dict:
    sn = feedback.set_number(unit) if unit else None
    return {"reports": feedback.list_reports(sn, limit),
            "stats": feedback.stats()}


@app.post("/api/feedback/{report_id}/vote")
async def api_vote(report_id: int, request: Request) -> JSONResponse:
    """Thumbs up/down on an existing report: does it still smell?

    Rate-limited on the same hashed-IP bucket as posting, because a vote is a
    write and the resolve rule is only as good as its resistance to one person
    clearing every report on the network.
    """
    if not _rate_ok(client_ip(request)):
        return JSONResponse({"error": "too many votes, try later"},
                            status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)
    try:
        vote = int(body.get("vote"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "vote must be 1 or -1"}, status_code=400)
    try:
        # The token is a random value the browser made up and stores locally.
        # It is never linked to anything identifying; it exists only so the
        # same person cannot vote twice on one report.
        tally = feedback.cast_vote(report_id, str(body.get("token") or ""), vote)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(tally, status_code=200)


@app.get("/api/feedback/summary")
async def api_feedback_summary() -> dict:
    """Per-set totals — what the map uses to badge a train."""
    return {"units": feedback.summary(_live_trips())}


def _trip_for_set(sn: int | None) -> str | None:
    """The trip_id this set is currently running, from our own poll data."""
    if sn is None:
        return None
    for t in ((STATE.get("data") or {}).get("trains") or []):
        if feedback.set_number(t.get("unit")) == sn and t.get("trip_id"):
            return str(t["trip_id"])
    return None


def _live_trips() -> set | None:
    """trip_ids currently in the realtime feed.

    People reports expire when their journey ends, and a trip leaving this set
    is exactly that event. Returns None while the poller has no data, so a
    cold start does not read as "every service has terminated" and silently
    expire every live report.
    """
    data = STATE.get("data")
    if not data:
        return None
    trains = data.get("trains") or []
    if not trains:
        return None
    return {t.get("trip_id") for t in trains if t.get("trip_id")}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Fine at Auckland's scale."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def client_ip(request: Request) -> str:
    """The visitor's IP, not the proxy's.

    Behind Fly (or any reverse proxy) request.client.host is the edge, so every
    visitor shares one bucket and the first spammer rate-limits the whole
    country. Fly sets Fly-Client-IP; X-Forwarded-For is the generic fallback
    and we take its FIRST entry. Both are client-settable in principle, but the
    rate limit is a courtesy speed bump, not a security control.

    The return value is used ONLY to derive a salted hash bucket inside
    _rate_ok() and is never logged, stored or returned in a response.
    """
    fly = (request.headers.get("fly-client-ip") or "").strip()
    if fly:
        return fly
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    return (request.client.host if request.client else "?") or "?"


def admin_ok(request: Request) -> bool:
    """True if this request carries the admin token.

    If ADMIN_TOKEN is unset the moderation routes are closed entirely rather
    than open — failing open on a public site would let anyone delete reports.
    """
    want = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if not want:
        return False
    got = (request.headers.get("x-admin-token")
           or request.query_params.get("token") or "").strip()
    return secrets.compare_digest(got, want)


@app.post("/api/feedback")
async def api_feedback_post(request: Request) -> JSONResponse:
    ip = client_ip(request)
    if not _rate_ok(ip):
        return JSONResponse({"error": "too many reports from here, try later"},
                            status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "expected a JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)
    # ---- 6-car attribution -------------------------------------------------
    # A 6-car is two coupled sets. If the rider says "car 5" and we have
    # inferred which set is the rear half, the complaint belongs to THAT set,
    # not to whichever half AT happened to attach the trip to. Getting this
    # wrong brands an innocent train, so when the pairing is unknown we file
    # against the known set and mark the report unattributable instead.
    car_pos = body.get("car_pos")
    con = body.get("consist")
    partner = None
    try:
        cp = int(car_pos) if car_pos not in (None, "") else None
    except (TypeError, ValueError):
        cp = None
    if cp and str(con) == "6":
        sn = feedback.set_number(body.get("unit"))
        pairing = _consist_map().get(str(sn)) if sn else None
        if pairing:
            owner = consist.set_for_car(cp, pairing)
            if owner:
                partner = int(owner)
            else:
                # The car is in THIS set's own half. We still resolved which
                # half the rider was in, so the report is attributable — pass
                # the known set as the owner so it is recorded as confirmed
                # rather than "could be either coupled set".
                partner = int(sn)

    try:
        rec = feedback.add_report(
            unit=body.get("unit"),
            category=body.get("category"),
            severity=body.get("severity"),
            note=body.get("note"),
            case_ref=body.get("case_ref"),
            occurred_at=body.get("occurred_at"),
            carriage=body.get("carriage"),
            car_pos=car_pos,
            consist=con,
            behaviour=body.get("behaviour"),
            # The trip this set is running right now. Taken from the server's
            # own live feed, never from the client: a browser could send any
            # trip_id and keep a people report alive indefinitely. If the train
            # has no trip (stabled, or not yet assigned) this stays None and
            # the report expires on the clock instead.
            trip_id=_trip_for_set(feedback.set_number(body.get("unit"))),
            # When we inferred the couple, a car in the rear half is filed
            # against the PARTNER set, which is the physically correct train.
            # See the attribution block above.
            partner_set=partner,
            # No reporter identity is accepted or stored. The `remote-user`
            # header was trustworthy behind Authelia but is attacker-settable
            # on a public deployment, and a name on a public complaint is
            # personal data this app has no need for. Reports are anonymous.
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:                       # don't leak internals to a form
        LOG.exception("feedback insert failed")
        return JSONResponse({"error": "could not save that report"}, status_code=500)
    return JSONResponse({"ok": True, **rec}, status_code=201)


# ---- moderation ----
# Gated on ADMIN_TOKEN. Hiding is the normal action and is reversible; delete
# is for content that must genuinely be gone.

@app.get("/api/consist")
async def api_consist() -> dict:
    """Coupled 6-car services.

    Membership (3 cars vs 6, and which two sets) comes from AT's own
    `serviceinfo/v1/vehicles/byTripId` — the same source behind "6 CAR" on the
    platform displays. Front/rear order is NOT published by anyone, so it is
    inferred from the two halves' positions; see src/consist.py.

    Each entry carries `source` ("at" or "inferred") and `consist_inferred`,
    so no client can mistake a geometric guess for published data.
    """
    couples = _consist_map()
    at_n = sum(1 for v in couples.values() if v.get("source") == "at")
    return {"couples": couples,
            "inferred": at_n == 0,
            "source": "at" if at_n else "inferred",
            "error": STATE.get("consist_error")}


def _consist_map() -> dict:
    """Current pairing table: AT's published units, ordered by geometry.

    AT is asked which units work each live trip (one cached call per trip).
    That decides whether a service is 3 or 6 cars and names both sets. The
    positional inference still runs, because it supplies the front/rear order
    AT does not publish, and because it is the fallback if the unsubscribed
    Journey Planner key is ever revoked.
    """
    trains = (STATE.get("data") or {}).get("trains") or []
    rows = []
    trips = []
    for t in trains:
        sn = feedback.set_number(t.get("unit"))
        if sn is None:
            continue
        # The payload carries speed in km/h; consist.detect() works in m/s
        # (MIN_SPEED = 2.0 m/s). Passing km/h straight through would compare
        # 7.2 against a 2.0 threshold and call stabled stock "moving", or
        # worse, reading a non-existent key would make every speed None and
        # silently detect nothing at all.
        kmh = t.get("speed_kmh")
        rows.append({"set_no": sn, "lat": t.get("lat"), "lon": t.get("lon"),
                     "speed": (kmh / 3.6) if kmh is not None else 0.0,
                     "bearing": t.get("bearing"),
                     "fix_age_s": t.get("fix_age_s"),
                     "trip_id": t.get("trip_id")})
        if t.get("trip_id"):
            trips.append(t["trip_id"])

    inferred = consist.detect(rows)

    by_trip, err = consist_at.units_for_trips(trips)
    STATE["consist_error"] = err
    at_couples = consist_at.couples_from_units(by_trip)
    merged = consist_at.merge(at_couples, inferred)
    # The latch key needs the service's trip id, and only ONE half of a 6-car
    # carries it. Build the lookup from the rows we already assembled, then
    # fill each set's partner's trip in, so either half resolves the same key.
    trip_by_set = {str(r["set_no"]): r.get("trip_id") for r in rows}
    for sn, row in merged.items():
        if not trip_by_set.get(str(sn)):
            trip_by_set[str(sn)] = trip_by_set.get(str(row.get("partner") or ""))
    _apply_order_latch(merged, trip_by_set)
    return merged


def _apply_order_latch(merged: dict, trip_by_set: dict) -> None:
    """Latch a 6-car's order from the best moving sample; hold it until split.

    Front/rear can only be DERIVED while moving — the projection needs a
    direction of travel, and a stationary unit's bearing is noise (a coupled
    pair was observed reporting bearings 21.4 degrees apart while stopped).
    So roughly half of all couples report `position: None` at any instant, and
    a train's order blinks out every time it stops at a platform.

    But the order did not become unknown: the train was moving a moment ago,
    and two sets joined by a Scharfenberg coupler cannot swap ends without
    uncoupling first. So remember it.

    The latch is keyed on (trip_id, both set numbers), NOT on the pair alone.
    At a terminus the same physical pair reverses and front/rear invert with
    no uncoupling, so a pair-keyed memory would serve a confidently wrong
    order for the whole return working. A new trip inherits nothing and must
    earn its own order. Set numbers are in the key too, so a coupling change
    invalidates the entry even mid-trip.

    `order_source` distinguishes a live determination (`"inferred"`) from a
    remembered one (`"latched"`), so the uncertainty stays visible rather than
    being laundered into the same field.
    """
    latch = _latch()
    seen_pairs: set[tuple[str, str]] = set()
    stored = restored = 0

    # Pass 1: bank every live geometric determination.
    for sn, row in merged.items():
        partner = row.get("partner")
        if not partner or row.get("consist") != 6:
            continue
        seen_pairs.add(tuple(sorted((str(sn), str(partner)))))
        if row.get("position") != "front":
            continue        # record each pair once, from the front half
        if latch.observe(trip_by_set.get(str(sn)), str(sn), str(partner),
                         speed_mps=row.get("speed_mps"),
                         brg_diff=row.get("brg_diff"),
                         gap_m=row.get("gap_m"),
                         fix_skew_s=row.get("fix_skew_s")):
            stored += 1

    # Pass 2: fill in pairs that have no order right now but did earlier.
    for sn, row in merged.items():
        partner = row.get("partner")
        if row.get("position") or not partner or row.get("consist") != 6:
            continue
        pos = latch.position_for(str(sn), trip_by_set.get(str(sn)), str(partner))
        if not pos:
            continue
        row["position"] = pos
        row["cars"] = [1, 2, 3] if pos == "front" else [4, 5, 6]
        row["order_source"] = "latched"
        restored += 1

    # A pair that has stopped appearing together has split (or one half has
    # gone off-feed). Drop its remembered order: they are not one train now.
    #
    # Guarded against a feed outage. The consist table is built from per-trip
    # AT calls; if those error, `merged` arrives EMPTY and every pair would
    # look split, wiping the whole store in one poll for no reason. An empty
    # table is never evidence about couplings, so it is ignored. A table with
    # real content is believed, including when it shows only one pair gone —
    # that is exactly what a genuine split looks like, and the cost of being
    # wrong is small (the order is relearned the next time the pair moves).
    known = latch.known_pairs()
    missing = known - seen_pairs
    forgotten = 0
    if not merged and missing:
        LOG.info("order latch: empty consist table, keeping %d remembered "
                 "order(s) rather than reading it as %d splits",
                 len(known), len(missing))
    else:
        for a, b in missing:
            forgotten += latch.forget_pair(a, b)

    latch.save()
    if stored or restored or forgotten:
        LOG.info("order latch: %d stored, %d restored from memory, "
                 "%d forgotten, %s", stored, restored, forgotten,
                 latch.stats())


@app.get("/api/alerts")
async def api_alerts() -> dict:
    """Rail service alerts, split into in-force now vs upcoming.

    Previously unconsumed: AT publishes 161 alerts network-wide, ~12 touching
    rail, and the map silently showed trains running to stations that were
    closed. Rail relevance is matched on route/stop IDS, never on the alert
    text, because station names appear constantly in bus-replacement notices.

    Never 500s: `alerts.fetch()` returns the last good payload with `error`
    set rather than raising, because an alerts outage must not break the map.
    """
    key = live.api_key()
    if not key:
        return {"now": [], "upcoming": [], "error": "no api key",
                "total_feed": 0, "generated": 0}
    return await alerts.fetch(
        key,
        rail_routes=live.RAIL_ROUTE_IDS,
        rail_stop_ids=set(live.STOP_IDS),
        stop_code=live.STOP_IDS,
        station_name=live.station,
    )


@app.get("/api/stations")
async def api_stations() -> dict:
    """Station list for the planner's pickers, deduped across platforms."""
    return {"stations": [{"name": s["name"], "lat": s["lat"], "lon": s["lon"]}
                         for s in journey.stations()]}


def _station_codes(station: str) -> list[str]:
    """The stop_code(s) to ask AT's departures board about.

    Prefers the station's PARENT code, which returns every platform in a single
    call (each row tagged with its own platform). Falls back to the individual
    platform codes if the baked geometry predates `parent_code`.
    """
    par = (live.GEO.get("parent_code") or {}).get(station)
    if par:
        return [str(par)]
    return [str(code) for code, s in (live.GEO.get("stops") or {}).items()
            if journey._base(s.get("name") or "") == station]


def _mark_cancelled(deps: list[dict]) -> int:
    """Flag departures AT has CANCELED or part-cancelled. Returns the count.

    Only matters on the TIMETABLE fallback path: AT's own live board already
    omits cancelled services, but our timetable reconstruction is built from
    the baked GTFS and knows nothing about today, so without this it will
    cheerfully promise a train that AT has already cancelled. Measured on a
    live feed: 14 cancelled trips, one of them rail.

    Matched on `trip_id`, which `board.to_departures()` and
    `journey.departures_at()` both attach.
    """
    cancels = (STATE.get("data") or {}).get("cancellations") or {}
    if not cancels:
        return 0
    n = 0
    for d in deps:
        c = cancels.get(str(d.get("trip_id") or ""))
        if not c:
            continue
        if c.get("cancelled"):
            d["cancelled"] = True
            n += 1
        if c.get("skipped"):
            d["skipped_stops"] = [live.station(s) for s in c["skipped"]]
    return n


@app.get("/api/departures")
async def api_departures(station: str, limit: int = 8) -> JSONResponse:
    """Live departure board for one station — what a tap on the map shows.

    Prefers AT's OWN departure board (serviceinfo/v1/departures), which is what
    the platform screens and the AT app show: real expected times, real
    platforms, cancellations already removed. Falls back to our timetable
    reconstruction only when that API is unreachable, and says which one the
    rider is looking at via `source`, because the two deserve different trust.
    """
    try:
        codes = _station_codes(station)
        mv, err = board.movements(codes) if codes else ([], "unknown station")
    except Exception as e:  # noqa: BLE001 - never 500 a board
        mv, err = [], type(e).__name__

    data = STATE.get("data") or {}
    geo = live.GEO
    trips = list(geo.get("trips") or {})

    if mv:
        deps = board.to_departures(mv, trips, geo, limit)
        journey.attach_risk(deps, data.get("trains") or [],
                            feedback.summary(_live_trips()),
                            data.get("assignments") or {})
        # Belt and braces: AT's live board is supposed to drop cancellations
        # already, so this should be 0. Applied anyway because "supposed to"
        # is not a guarantee, and a cancelled row is worse than a missing one.
        _mark_cancelled(deps)
        return JSONResponse({"station": station, "departures": deps,
                             "source": "at-live",
                             "warming": not STATE.get("fetched")})

    # No live board. That is either "nothing is running" (correct at 23:40) or
    # "we could not reach AT" — never conflate the two.
    try:
        deps = journey.departures_at(station, datetime.now(feedback.NZ), limit)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    journey.attach_risk(deps, data.get("trains") or [],
                        feedback.summary(_live_trips()),
                        data.get("assignments") or {})
    ncanc = _mark_cancelled(deps)
    return JSONResponse({"station": station, "departures": deps,
                         "source": "timetable", "live_error": err,
                         "cancelled_shown": ncanc,
                         "warming": not STATE.get("fetched")})


@app.get("/api/nearest")
async def api_nearest(lat: float, lon: float) -> dict:
    """Nearest station to a GPS fix.

    The coordinates are used for this one calculation and are NOT logged or
    stored — same rule as visitor IPs. The browser asks the user's permission;
    we just answer the question and forget it.
    """
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return {"error": "coordinates out of range"}
    best, best_m = None, None
    for st in journey.stations():
        if st["lat"] is None or st["lon"] is None:
            continue
        d = _haversine_m(lat, lon, st["lat"], st["lon"])
        if best_m is None or d < best_m:
            best, best_m = st, d
    if best is None:
        return {"error": "no stations with coordinates"}
    return {"station": best["name"], "metres": int(best_m),
            # Auckland's rail network does not reach most of the region. A fix
            # 40 km from any station means the user is nowhere near a train,
            # and silently picking the "nearest" one would be misleading.
            "near": best_m < 2000}


@app.get("/api/journey")
async def api_journey(origin: str, dest: str, limit: int = 4) -> JSONResponse:
    """Direct departures origin -> dest, annotated with pee risk."""
    try:
        res = journey.plan(origin, dest, datetime.now(feedback.NZ), limit=limit)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    journey.attach_risk(res["departures"],
                        (STATE.get("data") or {}).get("trains") or [],
                        feedback.summary(_live_trips()),
                        (STATE.get("data") or {}).get("assignments") or {})
    res["warming"] = not STATE.get("fetched")
    return JSONResponse(res, status_code=200)


@app.get("/api/admin/reports")
async def api_admin_reports(request: Request, limit: int = 200) -> JSONResponse:
    if not admin_ok(request):
        return JSONResponse({"error": "not authorised"}, status_code=401)
    return JSONResponse({"reports": feedback.list_all(limit),
                         "stats": feedback.stats()})


@app.post("/api/admin/reports/{report_id}/hide")
async def api_admin_hide(report_id: int, request: Request) -> JSONResponse:
    if not admin_ok(request):
        return JSONResponse({"error": "not authorised"}, status_code=401)
    hidden = True
    try:
        body = await request.json()
        if isinstance(body, dict) and "hidden" in body:
            hidden = bool(body["hidden"])
    except Exception:
        pass
    if not feedback.set_hidden(report_id, hidden):
        return JSONResponse({"error": "no such report"}, status_code=404)
    LOG.info("report %s hidden=%s by admin", report_id, hidden)
    return JSONResponse({"ok": True, "id": report_id, "hidden": hidden})


@app.delete("/api/admin/reports/{report_id}")
async def api_admin_delete(report_id: int, request: Request) -> JSONResponse:
    if not admin_ok(request):
        return JSONResponse({"error": "not authorised"}, status_code=401)
    if not feedback.delete_report(report_id):
        return JSONResponse({"error": "no such report"}, status_code=404)
    LOG.info("report %s deleted by admin", report_id)
    return JSONResponse({"ok": True, "id": report_id, "deleted": True})


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(WEB / "admin.html", headers=_SHELL_CACHE)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html", headers=_SHELL_CACHE)


app.mount("/static", StaticFiles(directory=WEB), name="static")


# --------------------------------------------------------------- canonical host
# Set CANONICAL_HOST to send every visitor to another origin with a 301.
# Used on Fly (akl-trains.fly.dev) to redirect to the self-hosted site; unset
# on the host that actually serves, so one image does both jobs.
#
# /api/health is DELIBERATELY EXEMPT. Fly's own health check
# (fly.toml [[http_service.checks]] path = "/api/health") follows no
# redirects: 301 it and the platform marks the machine unhealthy, replaces it,
# and the redirect takes itself down. Any monitor pointed at a /api/ path
# likewise wants a real answer, not a hop.
CANONICAL_HOST = os.environ.get("CANONICAL_HOST", "").strip()
_NO_REDIRECT_PREFIXES = ("/api/",)


@app.middleware("http")
async def _canonical_redirect(request: Request, call_next):
    if CANONICAL_HOST:
        path = request.url.path
        if not path.startswith(_NO_REDIRECT_PREFIXES):
            # Preserve path and query: a shared deep link must survive the hop.
            target = f"https://{CANONICAL_HOST}{path}"
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(target, status_code=301)
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn
    # access_log off: it recorded a line per request. Those lines carried the
    # Fly proxy address rather than the visitor's, so this is not a leak being
    # closed — it is simply not generating per-request records we never read.
    # Poll results and errors still log, which is all the operational signal
    # this app actually uses.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8731)),
                log_level="info", access_log=False)
