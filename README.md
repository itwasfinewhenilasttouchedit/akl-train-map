# Auckland Live Train Map

A self-hosted website showing every Auckland train moving on a map in real
time — real GPS positions, headings, occupancy, live delays and **unit numbers**
(AMP 973) — with an automatic keyless fallback if no API key is configured.

![screenshot](output/map.png)

## Run it

```bash
./run.sh                 # http://localhost:8731
PORT=9000 ./run.sh       # different port
```

First run downloads AT's GTFS feed (~32 MB) and builds the rail geometry.
Thereafter it starts in a second; geometry auto-rebuilds weekly.

## What it shows

- Every live train at its **actual GPS position**, rotated to its real heading.
- The **unit number** of each train (AMP 973), in the list and in the popup.
- Live delay against the timetable, and carriage occupancy.
- **The whole fleet, not just trains in service.** Filled arrows are passenger
  services. Hollow squares are units with no trip — stabled stock, empty-stock
  moves and depot shunts — shown with their location and whether they're moving,
  but no destination or ETA, because AT gives them none. Toggle them off in the
  legend ("Not in service").
- **Rider feedback per unit.** Click any train → "Report this train": category,
  severity, notes, optional AT case reference. Reported units get a coloured
  badge on the map and in the list, and the panel shows the most-reported units.
  Reports are keyed on the set number, so AMT701 and AMP 701 are one train.
- Line colours and filters: Eastern/Western, Southern/Onehunga loop, Onehunga, Huia.
- All 102 rail stations with name tooltips.
- A next-arrivals list, sorted by ETA; click a row to fly to that train.
- Per-train popup: destination, from → to, ETA, platform, delay, speed,
  percentage of the way between stops.

## Two data paths

**With an API key** (free from https://dev-portal.at.govt.nz) it reads AT's
GTFS-Realtime feeds: one request returns every vehicle in Auckland with GPS,
bearing, speed, occupancy and unit number. A second request gives live delays.
The GTFS schedule, prepared at build time, turns a position into "next stop and
when" without any further polling.

**Without a key** it falls back to `src/live_infer.py`, which needs no signup at
all: AT's own Journey Planner ships a public key that opens the per-station
departures endpoint. Poll all 102 rail stations concurrently (~0.6 s), group by
journey, and interpolate each train along the real GTFS track polyline between
its last and next station. No unit numbers, and positions are time-interpolated
rather than GPS, but the map works.

The page says which one is live.

### Supplying the key

Either set `AT_API_KEY`, or — better — keep the key out of the deployment
entirely by pointing the app at a secret store:

```
CR_SECRET_URL=https://your-secret-store.example/secrets/AT_API_KEY
CR_SECRET_TOKEN=<scoped, revocable token>
```

The store just has to answer `GET $CR_SECRET_URL` with the bare key as the body
and honour `Authorization: Bearer $CR_SECRET_TOKEN`. Any secret manager will do.

The key is fetched at startup and cached for 15 minutes, so rotating it needs no
redeploy.

## Layout

```
run.sh                  start everything
src/build_static.py     GTFS zip -> rail geometry, stop projections, hop times
src/live.py             live poller + position inference (runnable standalone)
src/server.py           FastAPI: /, /api/trains, /api/health
src/shot.py             Playwright screenshot, used for verification
web/index.html          Leaflet front end
web/rail_geo.json       slim geometry for the browser (178 KB)
output/rail_geo.json    full geometry for the server (11 MB)
```

## API

- `GET /api/trains` — live train list + poll stats
- `GET /api/health` — `{ok, trains, stops_polled, poll_ms, last_fetch}`

## Caveats

- The departures key is AT's own public web key. If AT rotates it, pull the new
  one from their Journey Planner bundle, or register a free key at
  https://dev-portal.at.govt.nz and use the proper realtime feed instead.
- Only *monitored* journeys appear — a train with no live predictions is omitted.
- "Huia" is the Pukekohe shuttle; often 0 running outside peak.