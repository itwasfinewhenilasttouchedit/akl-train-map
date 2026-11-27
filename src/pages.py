"""Server-rendered, crawlable pages for stations and lines.

WHY THIS EXISTS
---------------
The whole site is one URL (`/`). Google can index exactly one page, and
nobody can send a friend a link to "Britomart" or "the Southern Line" — the
map is a single JS application and a crawler sees an empty shell.

WHAT GETS A PAGE, AND WHAT DELIBERATELY DOES NOT
------------------------------------------------
Stations (`/station/britomart`) and lines (`/line/southern`). NOT units
(`/train/am-701`), which the backlog note originally proposed.

A unit page is a page about an AM-class set — exactly the metadata a rider
does not think in. ("People just want to know what carriage on the service to
avoid … to them its just a train.") Riders search for the place they are
standing and the line they are on. If an enthusiast-facing unit tier is ever
wanted it can be added later as its own thing; it should not be the first
crawlable surface this site grows.

HONESTY RULES BAKED IN
----------------------
1. **Rendered HTML must say what the map says.** A page that reads "clean"
   while the live map warns is worse than no page. Both read `feedback.summary()`
   and the same consist pairing, and both speak in CARRIAGES, not set numbers
   (see makeupHtml() in web/index.html).
2. **Open reports only. No free text, ever.** Notes are rider-written and may
   name people; the ticker already hides them and a crawlable page is the
   worst possible place to start publishing them. Counts, category and
   carriage only.
3. **No live departure times in the HTML.** The body is cached and crawled;
   baking "08:06, 2 min" into it would publish a time that is wrong seconds
   later and days wrong by the time Google renders it. Timetable facts
   (which lines call here, first/last) are stable and safe; anything live is
   fetched by the page's own JS from the existing API.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey   # noqa: E402
import live      # noqa: E402

SITE = "https://hoki.co.nz"

# ONE PAGE PER AT ROUTE ID, not per colloquial line name.
#
# The first draft had "eastern" and "western" as separate entries both mapping
# to route `E-W`. They would have rendered two pages with identical station
# lists and near-identical text — textbook duplicate content, and Google picks
# one and drops the other. AT through-routes those services and names the
# route "Eastern / Western"; splitting them in the URL space would be claiming
# a structure the timetable does not have.
#
# So: one canonical page per route id, named as AT names it, and the
# colloquial names 301 in (see LINE_ALIASES) so "western line auckland" still
# lands somewhere real.
LINES: dict[str, dict] = {
    "southern-onehunga": {"routes": ["S-C"],  "name": "Southern Line"},
    "eastern-western":   {"routes": ["E-W"],  "name": "Eastern & Western Line"},
    "onehunga-western":  {"routes": ["O-W"],  "name": "Onehunga Line"},
    "te-huia":           {"routes": ["HUIA"], "name": "Te Huia"},
}

LINE_ALIASES: dict[str, str] = {
    "southern": "southern-onehunga",
    "eastern": "eastern-western",
    "western": "eastern-western",
    "east": "eastern-western",
    "west": "eastern-western",
    "onehunga": "onehunga-western",
    "huia": "te-huia",
}


# Renamed and colloquial station names, redirected to the current slug with a
# 301. These are not decoration: Britomart was renamed Waitematā, Mt Eden to
# Maungawhau and Aotea to Te Waihorotiu, and the old names are what people
# still type, link and search for. A 404 on /station/britomart would lose
# exactly the traffic these pages exist to catch. 301 (not an alias page) so
# there is one canonical URL per station and no duplicate content.
ALIASES: dict[str, str] = {
    "britomart": "waitemata",
    "waitemata-britomart": "waitemata",
    "waitematā": "waitemata",
    "mt-eden": "maungawhau",
    "mount-eden": "maungawhau",
    "maungawhau-mt-eden": "maungawhau",
    "aotea": "te-waihorotiu",
    "karangahape": "karanga-a-hape",
    "k-road": "karanga-a-hape",
    "takanini": "takaanini",      # the spelling without the long vowel
    "papakura-station": "papakura",
}


def station_slug(name: str) -> str:
    """'Baldwin Ave' -> 'baldwin-ave'. Lossy by design, so resolve by lookup."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower())
    return s.strip("-")


def stations() -> list[dict]:
    return journey.stations()


def station_by_slug(slug: str) -> dict | None:
    want = (slug or "").strip().lower()
    for s in stations():
        if station_slug(s["name"]) == want:
            return s
    return None


def _routes_at(station: str) -> list[str]:
    """Which AT route ids call at this station, from the baked timetable.

    A timetable fact, so it is safe to bake into cached HTML — unlike a
    departure time. Derived from trips rather than hard-coded, so it cannot
    drift when AT renumbers a line.
    """
    geo = live.GEO
    ids = {sid for s in stations() if s["name"] == station for sid in s["ids"]}
    if not ids:
        return []
    out: set[str] = set()
    for tr in (geo.get("trips") or {}).values():
        if any(sid in ids for sid, _ in (tr.get("calls") or [])):
            r = tr.get("route")
            if r:
                out.add(r)
    return sorted(out)


def _line_stations(routes: list[str]) -> list[tuple[str, bool]]:
    """Stations served by these routes -> [(name, is_core)].

    MEASURED, not assumed. A handful of trips deviate from a line's normal
    path — 9 of 479 `S-C` trips call at Glen Innes — so a naive "any trip
    stops here" set put Glen Innes and Panmure on the Southern Line page.
    That is wrong in the way that matters: someone reads it, waits at Glen
    Innes for a Southern train, and one comes past roughly twice a day.

    The call frequencies are cleanly bimodal (S-C: 83-99% for its real
    stations, then a gap to 5.2%; the branch split sits at ~70% vs ~93%), so
    20% separates "on this line" from "the occasional diversion" with a wide
    margin. Below the threshold the station is dropped entirely rather than
    footnoted: a crawlable page is read without context.
    """
    geo = live.GEO
    stop_name = {}
    for s in stations():
        for sid in s["ids"]:
            stop_name[sid] = s["name"]
    trips = 0
    hits: dict[str, int] = {}
    for tr in (geo.get("trips") or {}).values():
        if tr.get("route") not in routes:
            continue
        trips += 1
        for n in {stop_name[sid] for sid, _ in (tr.get("calls") or [])
                  if sid in stop_name}:
            hits[n] = hits.get(n, 0) + 1
    if not trips:
        return []
    out = []
    for n, c in hits.items():
        share = c / trips
        if share < 0.20:
            continue                      # occasional diversion, not this line
        out.append((n, share >= 0.80))    # core vs branch/peak-only
    return sorted(out)


# --------------------------------------------------------------- reports

def _network_report(summary: dict) -> dict:
    """One honest aggregate of open vehicle faults across the fleet.

    These pages are cached and crawled, and a report belongs to a TRAIN, not
    to a station — so a station page cannot honestly say "avoid carriage 2
    here". What it can say is how much is open right now and what kind, and
    send the reader to the live map, which knows which train is in front of
    them. Counts only: no notes (rider-written, may name people), no set
    numbers (depot metadata).
    """
    trains = 0
    total = 0
    cats: dict[str, int] = {}
    for _sn, f in summary.items():
        if not isinstance(f, dict):
            continue
        n = f.get("n_neg")
        if n is None:
            n = f.get("n") or 0
        if not n:
            continue
        trains += 1
        total += n
        lbl = (f.get("top_fault_label") or f.get("top_category_label")
               or "report").lower()
        cats[lbl] = cats.get(lbl, 0) + n
    top = sorted(cats.items(), key=lambda kv: -kv[1])
    return {"trains": trains, "total": total, "top": top}


def _report_html(summary: dict) -> list[str]:
    r = _network_report(summary)
    if not r["trains"]:
        return ['  <p class="ok">No open carriage reports anywhere on the '
                'network right now.</p>']
    t, n = r["trains"], r["total"]
    kinds = ", ".join(f"{_esc(k)} ({v})" for k, v in r["top"][:4])
    return [
        f'  <div class="warn">{n} open report{"s" if n != 1 else ""} across '
        f'{t} train{"s" if t != 1 else ""} right now'
        + (f" &mdash; {kinds}" if kinds else "") + '.</div>',
        '  <p class="sub">Riders report against the carriage they were in. '
        'A report follows the <em>train</em>, not the station, so open the '
        '<a href="/">live map</a> or the departure board above to see whether '
        'the one you are waiting for is affected &mdash; it will tell you '
        'which carriages to avoid.</p>',
    ]


# --------------------------------------------------------------- rendering

def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _shell(*, title: str, desc: str, canonical: str, h1: str, body: str,
           jsonld: str = "") -> str:
    """One template for every page.

    Plain server-rendered HTML with its own title/description/canonical — the
    point of the exercise. It links back to the map and carries the same
    header mark, but deliberately does NOT load Leaflet or the app: a crawler
    (and a rider on a slow connection) should get the answer as text.
    """
    return f"""<!doctype html>
<html lang="en-nz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(desc)}">
<link rel="canonical" href="{_esc(canonical)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Hoki Trains">
<meta property="og:title" content="{_esc(title)}">
<meta property="og:description" content="{_esc(desc)}">
<meta property="og:url" content="{_esc(canonical)}">
<meta name="twitter:card" content="summary">
{jsonld}
<style>
  :root {{ --bg:#0d1117; --panel:#161c24; --fg:#e8edf2; --muted:#8a97a6;
          --line:#2b3440; --edge:#626D7E; --accent:#1F5BD7; --focus:#7FB3FF; }}
  html,body {{ margin:0; background:var(--bg); color:var(--fg);
               font:15px/1.5 "Segoe UI",system-ui,sans-serif; }}
  main {{ max-width:720px; margin:0 auto; padding:18px 16px 60px; }}
  a {{ color:var(--focus); }}
  a:focus-visible {{ outline:2px solid var(--focus); outline-offset:2px; }}
  h1 {{ font-size:22px; margin:14px 0 4px; }}
  h2 {{ font-size:15px; margin:22px 0 6px; color:var(--muted);
        text-transform:uppercase; letter-spacing:.06em; }}
  .sub {{ color:var(--muted); font-size:13px; margin:0 0 6px; }}
  .card {{ background:var(--panel); border:1px solid var(--line);
           border-radius:12px; padding:12px 14px; margin:10px 0; }}
  .warn {{ background:#45321d; color:#f0a35e; border-radius:6px;
           padding:7px 9px; margin:6px 0; font-size:13.5px; }}
  .ok {{ color:#8fd98f; font-size:13.5px; }}
  ul.cols {{ list-style:none; padding:0; margin:0; display:flex;
             flex-wrap:wrap; gap:6px 10px; }}
  ul.cols li {{ margin:0; }}
  .chip {{ display:inline-block; background:#1b2430; border:1px solid var(--edge);
           border-radius:999px; padding:3px 10px; font-size:13px;
           text-decoration:none; }}
  .mapcta {{ display:inline-block; background:var(--accent); color:#fff;
             border-radius:8px; padding:8px 14px; text-decoration:none;
             font-size:14px; margin-top:6px; }}
  footer {{ margin-top:30px; color:var(--muted); font-size:12px;
            border-top:1px solid var(--line); padding-top:12px; }}
</style>
</head>
<body>
<main>
  <p class="sub"><a href="/">&larr; Hoki &middot; Auckland live train map</a></p>
  <h1>{_esc(h1)}</h1>
{body}
  <footer>
    Live positions and reports come from riders and from Auckland Transport&rsquo;s
    open data. Report counts on this page are open reports at the time it was
    served &mdash; the <a href="/">live map</a> is always current.
  </footer>
</main>
</body>
</html>
"""


def station_page(station: dict, summary: dict, couples: dict) -> str:
    # `couples` is accepted but unused: the page reports an AGGREGATE, and
    # front/back carriage placement needs the live train in front of you.
    # Kept in the signature so the caller stays symmetric with line_page().
    name = station["name"]
    slug = station_slug(name)
    routes = _routes_at(name)
    geo_routes = live.GEO.get("routes") or {}
    rnames = [(geo_routes.get(r) or {}).get("name") or r for r in routes]
    # Which of our line pages serve this station — a real internal link, not a
    # keyword list: the page is only worth indexing if it goes somewhere.
    line_links = sorted({
        (slug_, LINES[slug_]["name"]) for slug_ in LINES
        if any(r in routes for r in LINES[slug_]["routes"])
    })

    body = ['  <div class="card">']
    body.append('    <p class="sub">Auckland train station'
                + (f' &middot; {_esc(", ".join(rnames))}' if rnames else '')
                + '</p>')
    body.append('    <p>Live departures, which carriages riders are avoiding, '
                'and where every train on the network is right now.</p>')
    body.append('    <a class="mapcta" href="/?station=' + _esc(slug)
                + '">Open live departures</a>')
    body.append('  </div>')

    if line_links:
        body.append('  <h2>Lines calling here</h2>')
        body.append('  <ul class="cols">')
        for s_, n_ in line_links:
            body.append(f'    <li><a class="chip" href="/line/{_esc(s_)}">{_esc(n_)}</a></li>')
        body.append('  </ul>')

    body.append('  <h2>Open carriage reports</h2>')
    body.extend(_report_html(summary))

    others = [s["name"] for s in stations() if s["name"] != name]
    body.append('  <h2>Other stations</h2>')
    body.append('  <ul class="cols">')
    for n_ in others:
        body.append(f'    <li><a class="chip" href="/station/{_esc(station_slug(n_))}">'
                    f'{_esc(n_)}</a></li>')
    body.append('  </ul>')

    jsonld = (
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"TrainStation",'
        f'"name":{_json(name + " Train Station")},'
        f'"url":{_json(f"{SITE}/station/{slug}")},'
        '"address":{"@type":"PostalAddress","addressLocality":"Auckland",'
        '"addressCountry":"NZ"},'
        f'"geo":{{"@type":"GeoCoordinates","latitude":{float(station["lat"])},'
        f'"longitude":{float(station["lon"])}}}}}'
        '</script>'
    )
    return _shell(
        title=f"{name} Train Station — live departures and carriage reports",
        desc=(f"Live train departures from {name}, Auckland. See which carriages "
              "riders are avoiding and where every train is right now."),
        canonical=f"{SITE}/station/{slug}",
        h1=f"{name} Train Station",
        body="\n".join(body),
        jsonld=jsonld,
    )


def line_page(slug: str, summary: dict, couples: dict) -> str:
    meta = LINES[slug]
    name = meta["name"]
    sts = _line_stations(meta["routes"])
    core = [n for n, is_core in sts if is_core]
    branch = [n for n, is_core in sts if not is_core]

    body = ['  <div class="card">']
    body.append(f'    <p class="sub">Auckland rail &middot; {len(sts)} stations</p>')
    body.append('    <p>Where every train on this line is right now, live '
                'departures from each station, and which carriages riders are '
                'avoiding.</p>')
    body.append('    <a class="mapcta" href="/">Open the live map</a>')
    body.append('  </div>')

    def chips(names: list[str]) -> list[str]:
        rows = ['  <ul class="cols">']
        for n_ in names:
            rows.append(f'    <li><a class="chip" href="/station/{_esc(station_slug(n_))}">'
                        f'{_esc(n_)}</a></li>')
        rows.append('  </ul>')
        return rows

    body.append('  <h2>Stations on this line</h2>')
    body.extend(chips(core))
    if branch:
        # Named separately, not merged: these are real stations on the route
        # but not every service reaches them (branches, short-turns and peak
        # extensions). Listing them beside the core would promise a train
        # that mostly is not coming.
        body.append('  <h2>Served by some services</h2>')
        body.extend(chips(branch))

    body.append('  <h2>Open carriage reports</h2>')
    body.extend(_report_html(summary))

    body.append('  <h2>Other lines</h2>')
    body.append('  <ul class="cols">')
    for s_ in LINES:
        if s_ == slug:
            continue
        body.append(f'    <li><a class="chip" href="/line/{_esc(s_)}">'
                    f'{_esc(LINES[s_]["name"])}</a></li>')
    body.append('  </ul>')

    return _shell(
        title=f"{name} — live trains, stations and carriage reports",
        desc=(f"Live map of trains on Auckland's {name}. Stations, departures, "
              "and which carriages riders are avoiding."),
        canonical=f"{SITE}/line/{slug}",
        h1=name,
        body="\n".join(body),
    )


def _json(s: str) -> str:
    """JSON string literal, safe to embed inside a <script> block."""
    import json as _j
    return _j.dumps(s).replace("</", "<\\/")


def sitemap() -> str:
    """Every crawlable URL. `/` stays the canonical home of the map itself."""
    urls = [f"{SITE}/"]
    urls += [f"{SITE}/line/{s}" for s in LINES]
    urls += [f"{SITE}/station/{station_slug(s['name'])}" for s in stations()]
    rows = "\n".join(
        f"<url><loc>{u}</loc><changefreq>"
        f"{'daily' if u.endswith('/') else 'weekly'}</changefreq></url>"
        for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{rows}\n</urlset>\n")
