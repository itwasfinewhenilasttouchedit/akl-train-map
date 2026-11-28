#!/usr/bin/env python3
"""Render gate for the MENU and the FLEET screen.

Run:
  PLAYWRIGHT_BROWSERS_PATH=/opt/data/.pw .venv/bin/python src/shot_fleet.py \
      http://127.0.0.1:8766 output/gate_run

WHAT THIS GATE IS FOR
---------------------
Three claims that the diff cannot show and that nothing logs when they break:

  1. The bottom-right corner is now a MENU button, at the position and size the
     pixel toggle used to hold, and it actually opens something.
  2. The pixel toggle MOVED INSIDE that menu and still works. "Still works"
     means reachable (visible, and the topmost element at its own centre),
     clickable, and round-tripping `body.pixel` + aria-pressed + localStorage.
     Every one of those is checkable only in a browser.
  3. The Fleet screen lists units and the search box genuinely FILTERS them.
     The filter is tested by taking a real unit number out of the rendered
     list, typing it, and requiring the row count to FALL and that row to
     survive -- a filter that returned everything, or nothing, or ignored the
     query, each fails a different assertion here.

DESIGN NOTES THAT ARE NOT OPTIONAL
----------------------------------
* html/body are overflow:hidden, so a naive document-level overflow check
  paints nothing and passes silently. The layout assertions here therefore
  compare ELEMENT RECTS against the VIEWPORT, and each one is paired with a
  canary that deliberately measures something known to be off-screen, proving
  the check is live rather than structurally incapable of firing.
* The gate must pass in BOTH environments and says which it is in. Locally the
  dev server has no AT key (so no live units, and the fleet comes from the
  archive); in CI there is a key but the container's archive starts empty (so
  the fleet comes from live). Asserting "there are live rows" would be a false
  failure locally; asserting "there are archive rows" would be one in CI. The
  gate asserts what is true in both -- that the list is populated and the
  filter works -- and PRINTS the mix so a human can see which half was
  exercised.
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/out"

PHONE = dict(viewport={"width": 390, "height": 844},
             device_scale_factor=2, is_mobile=True, has_touch=True)
DESKTOP = dict(viewport={"width": 1280, "height": 900})

results = []


def ck(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" -- {detail}" if detail else ""))


def box(pg, sel):
    return pg.evaluate(
        """(s) => { const e = document.querySelector(s); if (!e) return null;
             const r = e.getBoundingClientRect();
             return {x:r.x, y:r.y, w:r.width, h:r.height,
                     right:r.right, bottom:r.bottom,
                     vis: getComputedStyle(e).visibility,
                     disp: getComputedStyle(e).display}; }""", sel)


def topmost_at_centre(pg, sel):
    """Is this element the thing a finger would actually hit at its centre?

    Presence in the DOM is not reachability -- a control covered by an overlay
    is present, correctly styled, and untappable. This is the only check that
    catches that, and it is why it is applied to the menu button and to the
    relocated skin toggle rather than to one of them.
    """
    return pg.evaluate("""(s) => {
        const e = document.querySelector(s);
        if (!e) return 'missing';
        const r = e.getBoundingClientRect();
        if (!r.width || !r.height) return 'zero-size';
        const t = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        if (!t) return 'nothing at centre';
        return (t === e || e.contains(t) || t.contains(e)) ? true : ('covered by ' +
               (t.id ? '#' + t.id : t.className || t.tagName));
    }""", sel)


def rows(pg):
    """Every rendered row, with all four columns read out of the DOM.

    The columns are read SEPARATELY rather than as one text blob: "AM 157
    Eastern / Western 67 ✓" would pass an assertion about any of them being
    in any order, and column ORDER is part of what this gate exists to pin.
    """
    return pg.evaluate(
        "() => [...document.querySelectorAll('#fllist .flrow')]"
        ".map(r => ({set: r.dataset.set,"
        "            unit: (r.querySelector('.flu')||{}).textContent,"
        "            line: (r.querySelector('.flline')||{}).textContent,"
        "            speed: (r.querySelector('.flspd')||{}).textContent,"
        "            glyph: ((r.querySelector('.flst')||{}).textContent||'').trim(),"
        "            glyphLabel: (r.querySelector('.flst')"
        "                         ? r.querySelector('.flst').getAttribute('aria-label')"
        "                         : null),"
        "            glyphTitle: (r.querySelector('.flst')"
        "                         ? r.querySelector('.flst').getAttribute('title')"
        "                         : null),"
        "            live: r.classList.contains('live')}))")


DASH = "\u2014"
TRI = "\u26a0"
TICK = "\u2713"


def column_order(pg):
    """Left-to-right x-order of the four cells in the first row.

    Measured from laid-out rects, NOT from DOM order: a grid can reorder
    visually (`order`, `direction`, explicit column placement) while the
    markup stays as written, and a rider reads the pixels.
    """
    return pg.evaluate("""() => {
      const r = document.querySelector('#fllist .flrow');
      if (!r) return null;
      const cells = ['.flu', '.flline', '.flspd', '.flst']
        .map(s => { const e = r.querySelector(s); if (!e) return null;
                    const b = e.getBoundingClientRect();
                    return {sel: s, x: b.x, right: b.right, w: b.width}; });
      if (cells.some(c => !c)) return {missing: true, cells};
      return {missing: false,
              order: cells.slice().sort((a, b) => a.x - b.x).map(c => c.sel),
              cells};
    }""")


def check_columns(pg, tag, all_rows):
    """FEATURE 1: four columns, in order, with dashes when not running."""
    print(f"== [{tag}] 4. the four fleet columns ==")

    # Every row must HAVE all four cells. A missing cell is the failure mode a
    # text-blob assertion cannot see.
    missing = [r["set"] for r in all_rows
               if r["unit"] is None or r["line"] is None
               or r["speed"] is None or r["glyph"] in (None, "")]
    ck(f"{tag}: every row renders all four columns", not missing,
       f"{len(missing)} incomplete: {missing[:5]}")

    # Text accessor that tolerates a MISSING cell. Without it, deleting a
    # column makes every later assertion raise AttributeError and the gate
    # prints a traceback instead of naming the defect -- which is a failure,
    # but an unreadable one. Measured: sabotaging the line cell away produced
    # exactly that before this existed.
    def txt(r, key):
        return (r[key] or "").strip()
    # BAIL OUT HERE, named, if a cell is absent. Everything below does string
    # work on those cells, so a missing one raises AttributeError on None and
    # the gate emits a traceback with NO tally and no named assertion --
    # measured, when a sabotage deleted the .flline cell. A stack trace is a
    # failure but it is not a diagnosis, which is the rule this file's own
    # docstring sets for the click handlers.
    if missing:
        ck(f"{tag}: SKIPPED the remaining column checks", False,
           "a column cell is missing -- fix that first")
        return

    co = column_order(pg)
    ck(f"{tag}: the columns are laid out unit, line, speed, status (L to R)",
       co and not co["missing"]
       and co["order"] == [".flu", ".flline", ".flspd", ".flst"],
       json.dumps(co["order"] if co and not co.get("missing") else co))

    # The glyph column must not be pushed off the panel by a long line name.
    # Measured against the panel's own right edge, which is the thing that
    # would actually clip it.
    clipped = pg.evaluate("""() => {
      const p = document.querySelector('#fl').getBoundingClientRect();
      const bad = [];
      for (const r of document.querySelectorAll('#fllist .flrow')) {
        const g = r.querySelector('.flst');
        if (!g) continue;
        const b = g.getBoundingClientRect();
        if (b.right > p.right + 1 || b.width < 1) bad.push(r.dataset.set);
      }
      return bad;
    }""")
    ck(f"{tag}: no status glyph is clipped by the panel edge", not clipped,
       str(clipped[:5]))

    # --- running vs not running ---------------------------------------
    run = [r for r in all_rows if r["live"]]
    idle = [r for r in all_rows if not r["live"]]
    print(f"  [info] {tag}: {len(run)} running, {len(idle)} not running")
    # BOTH classes must be non-empty, or the two assertions below are
    # statements about an empty set and cannot fail. The dev server is staged
    # with roughly half the fleet withheld (src/stage_fleet_dev.py) precisely
    # so this holds.
    ck(f"{tag}: the list contains running units ({len(run)})", len(run) >= 3)
    ck(f"{tag}: the list contains NOT-running units ({len(idle)})",
       len(idle) >= 3)

    if idle:
        badline = [r["set"] for r in idle if r["line"].strip() != DASH]
        ck(f"{tag}: a unit that is not running shows an em dash for the line",
           not badline, f"{len(badline)} wrong: "
           f"{[(r['set'], r['line']) for r in idle if r['set'] in badline][:4]}")
        badspd = [r["set"] for r in idle if r["speed"].strip() != DASH]
        ck(f"{tag}: a unit that is not running shows an em dash for the speed",
           not badspd, f"{len(badspd)} wrong: "
           f"{[(r['set'], r['speed']) for r in idle if r['set'] in badspd][:4]}")
    if run:
        # A running unit must NOT be dashed -- the mirror assertion. Without
        # it, a renderer that dashed everything would pass the two above.
        dashed = [r["set"] for r in run if r["line"].strip() == DASH]
        ck(f"{tag}: a RUNNING unit never shows a dash for the line",
           not dashed, f"{len(dashed)} dashed: {dashed[:5]}")
        dasheds = [r["set"] for r in run if r["speed"].strip() == DASH]
        ck(f"{tag}: a RUNNING unit never shows a dash for the speed",
           not dasheds, f"{len(dasheds)} dashed: {dasheds[:5]}")
        # And the speed must be a NUMBER, not a label. Checked on the running
        # set only, because the dash is deliberately not numeric.
        notnum = [(r["set"], r["speed"]) for r in run
                  if not r["speed"].strip().lstrip("-").isdigit()]
        ck(f"{tag}: a running unit's speed column is a number",
           not notnum, str(notnum[:4]))
        # At least one running row must name a real LINE rather than the
        # out-of-service placeholder, or "the line column shows the line" is
        # vacuous on a payload of stabled stock.
        named = [r for r in run
                 if r["line"].strip() not in (DASH, "no line named")]
        ck(f"{tag}: at least one running unit names its line ({len(named)})",
           len(named) >= 1,
           str(sorted({r["line"].strip() for r in named})[:4]))

    # --- the status glyph, BOTH ways -----------------------------------
    tri = [r for r in all_rows if r["glyph"] == TRI]
    tick = [r for r in all_rows if r["glyph"] == TICK]
    print(f"  [info] {tag}: {len(tri)} triangles, {len(tick)} ticks")
    # Both states must be PRESENT. A gate that only ever sees ticks proves
    # nothing about the triangle, and vice versa -- the dev server is seeded
    # with one fault and one praise-only report for exactly this reason.
    ck(f"{tag}: at least one unit shows a warning triangle ({len(tri)})",
       len(tri) >= 1, str([r["set"] for r in tri][:5]))
    ck(f"{tag}: at least one unit shows a tick ({len(tick)})",
       len(tick) >= 1, str([r["set"] for r in tick][:5]))
    ck(f"{tag}: every row's glyph is a triangle or a tick",
       len(tri) + len(tick) == len(all_rows),
       str(sorted({r["glyph"] for r in all_rows})))

    # ACCESSIBILITY: the glyph must not be glyph-only OR colour-only. Every
    # one carries BOTH an aria-label and a title naming the state in words.
    nolabel = [r["set"] for r in all_rows
               if not (r["glyphLabel"] or "").strip()]
    ck(f"{tag}: every status glyph has an aria-label", not nolabel,
       str(nolabel[:5]))
    notitle = [r["set"] for r in all_rows
               if not (r["glyphTitle"] or "").strip()]
    ck(f"{tag}: every status glyph has a title", not notitle, str(notitle[:5]))
    # And the words must say the RIGHT thing -- a label reading "ok" on a
    # triangle is worse than no label.
    wrongtri = [(r["set"], r["glyphLabel"]) for r in tri
                if "open report" not in (r["glyphLabel"] or "").lower()]
    ck(f"{tag}: a triangle's label says it has open reports", not wrongtri,
       str(wrongtri[:3]))
    wrongtick = [(r["set"], r["glyphLabel"]) for r in tick
                 if "no open report" not in (r["glyphLabel"] or "").lower()]
    ck(f"{tag}: a tick's label says it has NO open reports", not wrongtick,
       str(wrongtick[:3]))
    # The two labels must DIFFER, or one string is doing both jobs and the
    # screen-reader user cannot tell the states apart.
    ck(f"{tag}: the two states have different accessible names",
       tri and tick and tri[0]["glyphLabel"] != tick[0]["glyphLabel"],
       f"{tri[0]['glyphLabel'] if tri else None!r} vs "
       f"{tick[0]['glyphLabel'] if tick else None!r}")

    # COLOUR IS NOT THE ONLY CARRIER -- proved by measurement, not by reading
    # the CSS: the triangle and the tick must be different GLYPHS as well as
    # different colours, so the column survives greyscale.
    ck(f"{tag}: the two states use different glyphs, not just colour",
       tri and tick and tri[0]["glyph"] != tick[0]["glyph"],
       f"{tri[0]['glyph'] if tri else None!r} vs "
       f"{tick[0]['glyph'] if tick else None!r}")
    # ... and they ARE differently coloured too, so a sighted scan is quick.
    cols = pg.evaluate("""() => {
      const g = s => { const e = document.querySelector(
          '#fllist .flrow .flst.' + s);
        return e ? getComputedStyle(e).color : null; };
      return {bad: g('bad'), ok: g('ok')};
    }""")
    ck(f"{tag}: the two states are also differently coloured",
       cols["bad"] and cols["ok"] and cols["bad"] != cols["ok"],
       json.dumps(cols))

    # PRAISE MUST NOT RAISE A TRIANGLE. The whole column's hardest case, and
    # it is exercised through the real renderer with real payload shapes
    # rather than trusted from the server test: a client reading `n` instead
    # of `n_neg` would pass every assertion above and still be wrong.
    verdicts = pg.evaluate("""() => {
      const mk = reports => {
        const h = flRow({set_no: 999, unit: 'AM 999', labels: [], obs: 1,
                         live: null, reports});
        const d = document.createElement('div');
        d.innerHTML = h;
        const g = d.querySelector('.flst');
        return {glyph: (g.textContent || '').trim(),
                label: g.getAttribute('aria-label')};
      };
      return {
        praise_only: mk({n: 3, n_neg: 0, worst_neg: null, n_praise: 3}),
        one_fault:   mk({n: 1, n_neg: 1, worst_neg: 2, n_praise: 0}),
        both:        mk({n: 4, n_neg: 1, worst_neg: 3, n_praise: 3}),
        clean:       mk({n: 0, n_neg: 0, worst_neg: null, n_praise: 0}),
        unknown:     mk(null),
      };
    }""")
    ck(f"{tag}: praise ONLY renders a tick, never a triangle",
       verdicts["praise_only"]["glyph"] == TICK,
       json.dumps(verdicts["praise_only"]))
    ck(f"{tag}: praise is still mentioned in the accessible name",
       "praise" in (verdicts["praise_only"]["label"] or "").lower(),
       repr(verdicts["praise_only"]["label"]))
    ck(f"{tag}: one negative report renders a triangle",
       verdicts["one_fault"]["glyph"] == TRI,
       json.dumps(verdicts["one_fault"]))
    ck(f"{tag}: praise does not cancel a fault",
       verdicts["both"]["glyph"] == TRI, json.dumps(verdicts["both"]))
    ck(f"{tag}: no reports at all renders a tick",
       verdicts["clean"]["glyph"] == TICK, json.dumps(verdicts["clean"]))
    # Unknown is a THIRD state, not silently a tick: "we could not read the
    # reports" must never be painted as a clean bill of health.
    ck(f"{tag}: an unreadable report state is neither a tick nor a triangle",
       verdicts["unknown"]["glyph"] not in (TICK, TRI),
       json.dumps(verdicts["unknown"]))
    ck(f"{tag}: and its label says the state is not known",
       "not known" in (verdicts["unknown"]["label"] or "").lower(),
       repr(verdicts["unknown"]["label"]))

    # A STATIONARY RUNNING TRAIN SHOWS 0, NOT A DASH. 0.0 km/h is a
    # measurement (standing at a platform); a falsy test would print a dash
    # and claim we did not know.
    zero = pg.evaluate("""() => {
      const h = flRow({set_no: 998, unit: 'AM 998', labels: [], obs: 1,
                       reports: {n: 0, n_neg: 0, n_praise: 0},
                       live: {line: 'Southern', speed_kmh: 0.0, cars: 3,
                              in_service: true, set_nos: [998]}});
      const d = document.createElement('div');
      d.innerHTML = h;
      return {speed: d.querySelector('.flspd').textContent.trim(),
              line: d.querySelector('.flline').textContent.trim()};
    }""")
    ck(f"{tag}: a stationary running train shows 0, not a dash",
       zero["speed"] == "0", json.dumps(zero))


def check_unit_page(pg, tag, all_rows):
    """FEATURE 2: a row click opens the unit detail page, with real stats."""
    print(f"== [{tag}] 5. the unit detail page ==")
    # Pick a row that is RUNNING IN SERVICE and has archive observations, so
    # the page has a real line, a non-zero speed and real history to render.
    # Falls back through "running at all" to "any row" rather than skipping --
    # on a quiet feed there may be nothing in service, and a skipped check is
    # a check that proves nothing.
    named = [r for r in all_rows
             if r["live"] and (r["set"] or "").isdigit()
             and r["line"].strip() not in (DASH, "Not in service",
                                           "no line named")]
    cands = named or [r for r in all_rows
                      if r["live"] and (r["set"] or "").isdigit()]
    target = (cands or [r for r in all_rows if (r["set"] or "").isdigit()])[0]
    sn = target["set"]
    print(f"  [info] {tag}: opening AM {sn} "
          f"(live={target['live']}, line={target['line'].strip()!r})")

    ck(f"{tag}: the unit panel starts hidden", not pg.is_visible("#fuwrap"))
    # The row must be a real control, not a div with a click handler: tabbing
    # to it and pressing Enter has to work.
    tagname = pg.evaluate(
        "() => document.querySelector('#fllist .flrow').tagName")
    ck(f"{tag}: a fleet row is a real <button> ({tagname})",
       tagname == "BUTTON")
    rowlabel = pg.evaluate(
        "() => document.querySelector('#fllist .flrow').getAttribute('aria-label')")
    ck(f"{tag}: a fleet row has an accessible name naming its own state",
       bool((rowlabel or "").strip()), repr(rowlabel))

    try:
        pg.click(f"#fllist .flrow[data-set='{sn}']", timeout=5000)
    except Exception as exc:
        ck(f"{tag}: clicking a fleet row", False,
           f"{type(exc).__name__}: {str(exc)[:90]}")
        return
    try:
        pg.wait_for_selector("#fuwrap.on", timeout=8000)
    except Exception as exc:
        ck(f"{tag}: clicking a row opens the unit page", False, str(exc)[:90])
        return
    ck(f"{tag}: clicking a row opens the unit page", True)
    # Wait for the fetch to land -- the panel opens immediately with a
    # Loading state by design, so asserting content straight away would race.
    try:
        pg.wait_for_function(
            "() => { const e = document.getElementById('fubody');"
            " return e && e.textContent.trim().length > 40; }", timeout=12000)
    except Exception as exc:
        ck(f"{tag}: the unit page loads its stats", False, str(exc)[:120])
        return
    ck(f"{tag}: the unit page loads its stats", True)

    ck(f"{tag}: the unit page names the unit it was opened for",
       sn in (pg.inner_text("#futitle") or ""),
       repr(pg.inner_text("#futitle")))
    ok, det = within_viewport(pg, "#fu")
    ck(f"{tag}: the unit panel is fully on screen", ok, det)
    ck(f"{tag}: the Fleet list stays mounted behind it, so Back returns there",
       pg.evaluate("() => document.getElementById('flwrap')"
                   ".classList.contains('on')"))

    body = pg.inner_text("#fubody") or ""
    cover = pg.inner_text("#fucover") or ""

    # THE COVERAGE WINDOW. This is the honesty assertion: the archive is days
    # old, so the page must say what window its numbers describe. Asserted on
    # the rendered text, and on the NUMBER being a real span rather than a
    # reassuring phrase.
    ck(f"{tag}: the page states its coverage window",
       "recording window" in cover.lower(), repr(cover[:140]))
    ck(f"{tag}: the coverage window names a span in days",
       re.search(r"\d+(\.\d+)?\s+days", cover) is not None, repr(cover[:140]))
    ck(f"{tag}: the page refuses to call the window a service history",
       "not a service history" in cover.lower(), repr(cover[:160]))

    # KEY STATS. Each is asserted by LABEL AND by a value matching its shape,
    # so a label rendered above an empty cell fails.
    stats = pg.evaluate("""() => {
      const out = {};
      for (const s of document.querySelectorAll('#fubody .fustat')) {
        const k = s.querySelector('.k'), v = s.querySelector('.v');
        if (k && v) out[k.textContent.trim()] = v.textContent.trim();
      }
      return out;
    }""")
    print(f"  [info] {tag}: unit page stats = {json.dumps(stats)[:300]}")
    for key, pat, what in (
            ("Observations", r"^[\d,]+$", "a count"),
            ("First seen", r"\d", "a date"),
            ("Last seen", r"\d", "a date"),
            ("Days active", r"^\d+", "a day count")):
        ck(f"{tag}: the page shows {key} as {what}",
           key in stats and re.search(pat, stats[key]) is not None,
           f"{key}={stats.get(key)!r}")
    ck(f"{tag}: Observations is a non-zero count for a unit with history",
       int((stats.get("Observations") or "0").replace(",", "")) > 0,
       repr(stats.get("Observations")))
    ck(f"{tag}: Days active is reported out of the days recorded",
       "recorded" in (stats.get("Days active") or ""),
       repr(stats.get("Days active")))

    # THE TOP SPEED AND ITS COUPLED-PARTNER CROSS-CHECK. A record claim
    # without the cross-check is exactly the claim this project refuses to
    # make, so the cross-check row is asserted to EXIST whenever a top speed
    # is shown.
    if "Top speed" in stats:
        ck(f"{tag}: the top speed is a km/h figure",
           re.match(r"^\d+(\.\d+)?\s*km/h$", stats["Top speed"]) is not None,
           repr(stats["Top speed"]))
        kmh = float(re.match(r"^(\d+(?:\.\d+)?)", stats["Top speed"]).group(1))
        # AM-class EMUs are geared for 110 km/h. Anything far above that is a
        # GPS artefact, and the gate says so rather than accepting it.
        # ZERO IS ALLOWED: a set that only ever appeared stationary has a real
        # top speed of 0.0, and an earlier version of this assertion
        # (`0 < kmh`) failed on exactly that -- it was testing which unit the
        # fixture happened to pick, not the page.
        ck(f"{tag}: the top speed is physically plausible for an AM-class set",
           0 <= kmh <= 130, f"{kmh} km/h")
        ck(f"{tag}: the page states WHEN the top speed happened",
           "When" in stats and re.search(r"\d", stats["When"]) is not None,
           repr(stats.get("When")))
        ck(f"{tag}: the page states WHERE the top speed happened",
           bool((stats.get("Where") or "").strip()), repr(stats.get("Where")))
        ck(f"{tag}: the page shows the coupled-partner cross-check",
           "Cross-check" in stats and bool(stats["Cross-check"].strip()),
           repr(stats.get("Cross-check")))
        ck(f"{tag}: the cross-check states confirmed, disagreeing, or absent",
           any(w in stats.get("Cross-check", "").lower()
               for w in ("confirmed", "disagrees", "none")),
           repr(stats.get("Cross-check")))
    else:
        print(f"  [info] {tag}: no top speed for AM {sn} "
              f"(no fix under the 60s age limit) -- cross-check not applicable")

    # PER-DAY HISTORY. Real bars, one per day the archive holds -- this is the
    # thing the /api/archive/day investigation existed to make possible.
    bars = pg.evaluate("""() => {
      const b = [...document.querySelectorAll('#fudays .fuday')];
      return b.map(e => ({h: e.getBoundingClientRect().height,
                          label: e.getAttribute('aria-label')}));
    }""")
    ck(f"{tag}: the page draws a per-day activity strip ({len(bars)} bars)",
       len(bars) >= 1, f"{len(bars)} bars")
    if bars:
        ck(f"{tag}: every day bar has a measurable height",
           all(b["h"] >= 1 for b in bars),
           str([round(b["h"], 1) for b in bars][:8]))
        ck(f"{tag}: every day bar names its day and fix count",
           all(re.search(r"\d{4}-\d\d-\d\d.*fix", b["label"] or "")
               for b in bars), repr((bars[0]["label"] or "")[:80]))
        # More than one day proves the strip is not rendering only its first
        # element. Printed rather than asserted when the archive genuinely
        # holds one day, which is a real state and not a defect.
        if len(bars) > 1:
            ck(f"{tag}: the strip spans multiple days ({len(bars)})", True)
        else:
            print(f"  [info] {tag}: archive holds ONE day, so the strip has "
                  f"one bar -- correct, but it cannot prove multi-day render")

    ck(f"{tag}: the page has a Reports section",
       "report" in body.lower(), repr(body[-120:]))
    # NO "Invalid Date" ANYWHERE. `reports.occurred_at` is an ISO-8601 string
    # while every archive timestamp is epoch seconds; passing the string
    # through the epoch formatter rendered "Invalid Date" on every report row.
    # This gate caught that, and the assertion stays because the two field
    # shapes are both called "when this happened" and will be confused again.
    ck(f"{tag}: no date on the page renders as Invalid Date",
       "invalid date" not in (body + cover).lower(),
       repr([ln for ln in body.splitlines()
             if "invalid" in ln.lower()][:3]))
    # Nothing invented. The page must not claim fleet facts this data has no
    # source for -- the same list the server gate enforces, checked again on
    # the RENDERED text because a client could add them without touching the
    # endpoint.
    invented = [w for w in ("built", "depot", "withdrawn", "livery",
                            "mileage", "refurb", "scrapped")
                if w in (body + cover).lower()]
    ck(f"{tag}: the page invents no fleet metadata", not invented,
       str(invented))

    # Screenshot BEFORE the synthetic-payload checks below, which replace
    # FUNIT and re-render: a photo of AM 996 is not a photo of the page.
    pg.screenshot(path=f"{OUT}/fleet_unit_{tag}.png")

    # THE CROSS-CHECK IN ALL THREE OF ITS STATES, driven through the real
    # renderer. The live-payload assertions above only exercise whichever
    # state the opened unit happens to be in -- measured: deleting the
    # no-partner branch entirely left the gate at 214/214, because AM 157 has
    # a partner and took the other branch. A speed record must never be
    # printed WITHOUT its cross-check, in any of the three cases.
    xcheck = pg.evaluate("""() => {
      const mk = top => {
        FUNIT = {set_no: 996, unit: 'AM 996', labels: ['AMP 996'],
                 archive: {enabled: true, known: true, obs: 9,
                           first_ts: 1795776776, last_ts: 1795777021,
                           days: [], days_active: 1, top_speed: top,
                           coverage: {first_ts: 1795344776,
                                      last_ts: 1795777021}},
                 live: null, reports: []};
        fuRender();
        const out = {};
        for (const s of document.querySelectorAll('#fubody .fustat')) {
          const k = s.querySelector('.k'), v = s.querySelector('.v');
          if (k && v) out[k.textContent.trim()] = v.textContent.trim();
        }
        return out;
      };
      const base = {kmh: 104.2, ts: 1795776776, near: 'Puhinui',
                    from: null, to: null, lat: 0, lon: 0};
      return {
        agree:    mk({...base, partner: 'AMP 904', partner_kmh: 103.9,
                      corroborated: true}),
        disagree: mk({...base, partner: 'AMP 904', partner_kmh: 12.0,
                      corroborated: false}),
        alone:    mk({...base, partner: null, partner_kmh: null,
                      corroborated: false}),
      };
    }""")
    for case, word in (("agree", "confirmed"), ("disagree", "disagrees"),
                       ("alone", "none")):
        got = xcheck[case].get("Cross-check")
        ck(f"{tag}: a top speed with a {case} partner prints a cross-check",
           bool(got), f"{case}: Cross-check={got!r}")
        ck(f"{tag}: and the {case} cross-check says '{word}'",
           word in (got or "").lower(), repr(got))
    # The corroborating partner must be NAMED, not merely counted: "confirmed"
    # without saying by what is an appeal to an invisible authority.
    ck(f"{tag}: a confirmed cross-check names the partner set",
       "904" in (xcheck["agree"].get("Cross-check") or ""),
       repr(xcheck["agree"].get("Cross-check")))
    # A disagreement must read as a disagreement, not be softened into one.
    ck(f"{tag}: a disagreeing cross-check does NOT claim confirmation",
       "confirmed" not in (xcheck["disagree"].get("Cross-check") or "").lower(),
       repr(xcheck["disagree"].get("Cross-check")))

    # THE LINE AND SPEED COLUMNS COME FROM THE SERVER'S live BLOCK. If the
    # server stops merging `line`, the client falls back to `route` ("E-W")
    # and every row still shows SOMETHING -- measured: that sabotage left the
    # gate at 214/214. So the endpoint's own contract is asserted here, from
    # the browser, rather than trusted: a running unit must carry a non-null
    # `line`, and the rendered line must be that value and not the fallback.
    api = pg.evaluate("""async () => {
      const d = await (await fetch('/api/fleet', {cache: 'no-store'})).json();
      const live = (d.units || []).filter(u => u.live);
      const insvc = live.filter(u => u.live.in_service);
      return {
        n_live: live.length,
        n_in_service: insvc.length,
        without_line: live.filter(u => u.live.line == null)
                          .map(u => u.set_no).slice(0, 6),
        without_speed: live.filter(u => !('speed_kmh' in u.live))
                           .map(u => u.set_no).slice(0, 6),
        idle_with_line: (d.units || []).filter(u => !u.live && u.line != null)
                           .map(u => u.set_no).slice(0, 6),
        sample: insvc.length ? {set_no: insvc[0].set_no,
                                line: insvc[0].live.line,
                                route: insvc[0].live.route,
                                speed: insvc[0].live.speed_kmh} : null,
      };
    }""")
    print(f"  [info] {tag}: /api/fleet live contract = {json.dumps(api)[:260]}")
    ck(f"{tag}: /api/fleet gives every running unit a non-null line",
       api["n_live"] > 0 and not api["without_line"],
       f"{len(api['without_line'])} missing: {api['without_line']}")
    ck(f"{tag}: /api/fleet gives every running unit a speed_kmh key",
       not api["without_speed"], str(api["without_speed"]))
    ck(f"{tag}: /api/fleet puts no line on a NOT-running unit",
       not api["idle_with_line"], str(api["idle_with_line"]))
    if api["sample"]:
        # The rendered line must be the LINE NAME, not the route code. These
        # differ ("Eastern / Western" vs "E-W"), which is exactly what makes
        # the silent fallback detectable.
        s = api["sample"]
        rendered = pg.evaluate(
            "(sn) => { const r = document.querySelector("
            "'#fllist .flrow[data-set=\"' + sn + '\"]');"
            " return r ? r.querySelector('.flline').textContent.trim() : null; }",
            str(s["set_no"]))
        ck(f"{tag}: the line column renders the line NAME, not the route code",
           rendered == s["line"] and rendered != s["route"],
           f"rendered={rendered!r} line={s['line']!r} route={s['route']!r}")

    # And the report row's date is exercised explicitly through the real
    # renderer, so the Invalid-Date assertion above is not relying on the
    # opened unit happening to have a report. This REPLACES the panel's
    # contents, so it runs last.
    repdate = pg.evaluate("""() => {
      FUNIT = {set_no: 997, unit: 'AM 997', labels: [],
               archive: {enabled: true, known: true, obs: 1, first_ts: 1795776776,
                         last_ts: 1795777021, days: [], days_active: 0,
                         top_speed: null,
                         coverage: {first_ts: 1795344776, last_ts: 1795777021}},
               live: null,
               reports: [{id: 1, set_no: 997, category: 'smell',
                          category_label: 'Smell', severity: 3, car_pos: 2,
                          occurred_at: '2026-11-28T14:46:03+13:00',
                          resolved: false}]};
      fuRender();
      const rows = [...document.querySelectorAll('#fubody .fustat')]
        .map(s => (s.textContent || '').trim());
      return rows.filter(t => t.toLowerCase().includes('smell'));
    }""")
    ck(f"{tag}: a report row renders its ISO date, not Invalid Date",
       bool(repdate) and "invalid date" not in repdate[0].lower()
       and re.search(r"\d", repdate[0]) is not None, str(repdate[:2]))

    # Back must return to the Fleet list, not to the map.
    try:
        pg.click("#fuclose", timeout=5000)
    except Exception as exc:
        ck(f"{tag}: the unit page Back button can be clicked", False,
           str(exc)[:90])
        return
    pg.wait_for_timeout(300)
    ck(f"{tag}: Back closes the unit page", not pg.is_visible("#fuwrap"))
    ck(f"{tag}: Back leaves the Fleet list open", pg.is_visible("#fl"))
    ck(f"{tag}: and the list still has its rows", len(rows(pg)) >= 10,
       f"{len(rows(pg))} rows")


def within_viewport(pg, sel):
    """Element rect fully inside the viewport. Returns (ok, detail)."""
    r = pg.evaluate("""(s) => { const e = document.querySelector(s);
        if (!e) return null; const r = e.getBoundingClientRect();
        return {x:r.x, y:r.y, right:r.right, bottom:r.bottom,
                vw: innerWidth, vh: innerHeight}; }""", sel)
    if not r:
        return False, f"{sel} missing"
    ok = (r["x"] >= -1 and r["y"] >= -1
          and r["right"] <= r["vw"] + 1 and r["bottom"] <= r["vh"] + 1)
    return ok, json.dumps({k: round(v) for k, v in r.items()})


def run(ctx, tag):
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto(BASE, wait_until="networkidle", timeout=90000)
    pg.wait_for_timeout(3500)

    # Every click in this gate goes through tap(), with a 5s timeout instead of
    # Playwright's 30s default: if a control has been hidden, deleted or pushed
    # back under the HUD, the gate must SAY "could not click X" within seconds
    # rather than hanging for half a minute and emitting a stack trace in place
    # of a named defect. A stack trace is a failure; it is not a diagnosis.
    def tap(sel, what):
        try:
            pg.click(sel, timeout=5000)
            return True
        except Exception as exc:
            ck(f"{tag}: {what}", False, f"{type(exc).__name__}: {str(exc)[:90]}")
            return False

    print(f"\n== [{tag}] 1. the corner is a menu button ==")
    mb = box(pg, "#menubtn")
    ck(f"{tag}: #menubtn exists", mb is not None)
    if not mb:
        return errs, pg
    # Visible, checked BEFORE anything clicks it. A hidden-but-present button
    # otherwise turns into a 30s Playwright timeout later on, which is a
    # failure but an unreadable one -- the gate should name the defect.
    vis = pg.is_visible("#menubtn")
    ck(f"{tag}: menubtn is visible", vis,
       f"display={mb['disp']} visibility={mb['vis']} {mb['w']:.0f}x{mb['h']:.0f}")
    if not vis:
        return errs, pg
    ck(f"{tag}: menubtn is a 44px-minimum square",
       abs(mb["w"] - mb["h"]) <= 1 and mb["w"] >= 44 and mb["h"] >= 44,
       f"{mb['w']:.0f}x{mb['h']:.0f}")
    # It must be in the BOTTOM-RIGHT corner -- the position the pixel toggle
    # vacated. Measured against the viewport, not against a remembered number.
    vp = pg.evaluate("() => ({w: innerWidth, h: innerHeight})")
    ck(f"{tag}: menubtn sits in the bottom-right corner",
       vp["w"] - mb["right"] <= 12 and vp["h"] - mb["bottom"] <= 12,
       f"gap right={vp['w'] - mb['right']:.0f} bottom={vp['h'] - mb['bottom']:.0f}")
    ok, det = within_viewport(pg, "#menubtn")
    ck(f"{tag}: menubtn is fully on screen", ok, det)
    # CANARY for the viewport check. The check must be able to FAIL, which an
    # overflow:hidden document makes easy to get wrong -- a naive check paints
    # nothing and silently passes. A node is parked a clear 500px off the left
    # edge and measured with the same function; if that reports "on screen",
    # every layout assertion above is decoration.
    # Deliberately NOT .sr-only: that block's rect is x:-1..0 (1px box, -1px
    # margin), which sits inside the +-1px tolerance and so passed as
    # on-screen -- the canary itself was unfalsifiable.
    pg.evaluate("""() => { const d = document.createElement('div');
        d.id = 'offscreen-canary';
        d.style.cssText = 'position:fixed;left:-500px;top:-500px;'
                        + 'width:40px;height:40px';
        document.body.appendChild(d); }""")
    cok, cdet = within_viewport(pg, "#offscreen-canary")
    ck(f"{tag}: CANARY the viewport check can fail", not cok, cdet)
    # ... and the same function must say YES to something plainly on screen, or
    # it would be failing everything equally and proving nothing either way.
    pok, _ = within_viewport(pg, "#menubtn")
    ck(f"{tag}: CANARY the viewport check can pass", pok)
    pg.evaluate("() => document.getElementById('offscreen-canary').remove()")
    ck(f"{tag}: menubtn is tappable at its own centre",
       topmost_at_centre(pg, "#menubtn") is True,
       str(topmost_at_centre(pg, "#menubtn")))
    ck(f"{tag}: menu starts closed",
       pg.get_attribute("#menubtn", "aria-expanded") == "false"
       and not pg.is_visible("#menu"))

    print(f"== [{tag}] 2. the pixel toggle moved INSIDE the menu ==")
    # Present at all. Checked first and bailed on, so a DELETED toggle reads as
    # "the toggle is gone" rather than as an opaque timeout 20 lines later.
    present = pg.evaluate("() => !!document.getElementById('skinbtn')")
    ck(f"{tag}: #skinbtn still exists", present)
    if not present:
        return errs, pg
    # Before opening: the toggle must NOT be floating loose over the map. This
    # is the assertion that "moved inside" rather than "duplicated".
    ck(f"{tag}: skinbtn is a descendant of #menu",
       pg.evaluate("() => { const m=document.getElementById('menu'),"
                   " s=document.getElementById('skinbtn');"
                   " return !!(m && s && m.contains(s)); }"))
    ck(f"{tag}: skinbtn is hidden while the menu is closed",
       not pg.is_visible("#skinbtn"))
    # The markup must still describe the page without JS (WCAG 4.1.2): the
    # static attribute says the skin is ON because the skin is the default.
    ck(f"{tag}: skinbtn is a real <button>",
       pg.evaluate("() => document.getElementById('skinbtn').tagName") == "BUTTON")

    if not tap("#menubtn", "the menu button can be clicked"):
        return errs, pg
    try:
        pg.wait_for_selector("#skinbtn", state="visible", timeout=5000)
    except Exception as exc:
        ck(f"{tag}: the menu opens and reveals the toggle", False, str(exc)[:90])
        return errs, pg
    ck(f"{tag}: aria-expanded flips to true on open",
       pg.get_attribute("#menubtn", "aria-expanded") == "true")
    sk = box(pg, "#skinbtn")
    ck(f"{tag}: the toggle is a 44px-minimum target inside the menu",
       sk and sk["w"] >= 44 and sk["h"] >= 44,
       f"{sk['w']:.0f}x{sk['h']:.0f}" if sk else "missing")
    ck(f"{tag}: the toggle still shows its single-letter face",
       (pg.inner_text("#skinbtn") or "").strip() == "P",
       repr(pg.inner_text("#skinbtn")))
    ck(f"{tag}: the toggle is tappable at its own centre",
       topmost_at_centre(pg, "#skinbtn") is True,
       str(topmost_at_centre(pg, "#skinbtn")))
    ok, det = within_viewport(pg, "#menu")
    ck(f"{tag}: the open menu is fully on screen", ok, det)

    # The round trip. This is the assertion that the relocation did not break
    # the control: class, aria-pressed and the stored preference must all move
    # together, in both directions.
    was = pg.evaluate("document.body.classList.contains('pixel')")
    ck(f"{tag}: skin starts on (it is the default)", was,
       f"pixel={was}")
    if not tap("#skinbtn", "the toggle can be clicked"):
        return errs, pg
    pg.wait_for_timeout(700)
    ck(f"{tag}: one click turns the skin off",
       not pg.evaluate("document.body.classList.contains('pixel')"))
    ck(f"{tag}: aria-pressed follows the skin to false",
       pg.get_attribute("#skinbtn", "aria-pressed") == "false")
    ck(f"{tag}: the choice persists as '0'",
       pg.evaluate("localStorage.getItem('pixelSkin')") == "0")
    ck(f"{tag}: the menu stays open after toggling",
       pg.is_visible("#skinbtn"))
    if not tap("#skinbtn", "the toggle can be clicked a second time"):
        return errs, pg
    pg.wait_for_timeout(700)
    ck(f"{tag}: a second click turns it back on",
       pg.evaluate("document.body.classList.contains('pixel')"))
    ck(f"{tag}: aria-pressed follows the skin back to true",
       pg.get_attribute("#skinbtn", "aria-pressed") == "true")
    ck(f"{tag}: the choice persists as '1'",
       pg.evaluate("localStorage.getItem('pixelSkin')") == "1")

    print(f"== [{tag}] 3. the Fleet screen ==")
    if not tap("#fleetbtn", "the Fleet menu item can be clicked"):
        return errs, pg
    pg.wait_for_selector("#fllist .flrow", timeout=30000)
    ck(f"{tag}: opening Fleet closes the menu", not pg.is_visible("#menu"))
    all_rows = rows(pg)
    ck(f"{tag}: the fleet lists units ({len(all_rows)})", len(all_rows) >= 10,
       f"{len(all_rows)} rows")
    nlive = sum(1 for r in all_rows if r["live"])
    # Printed, not asserted: which half is populated depends on the
    # environment (see the module docstring). A human reading the log can see
    # at a glance whether this run exercised live annotation or not.
    print(f"  [info] {tag}: {len(all_rows)} rows, {nlive} marked live")
    ck(f"{tag}: every row names a unit",
       all((r["unit"] or "").strip() for r in all_rows),
       str([r for r in all_rows if not (r["unit"] or "").strip()][:3]))
    cnt = pg.inner_text("#flcount")
    ck(f"{tag}: the count states the total", str(len(all_rows)) in cnt, repr(cnt))
    ok, det = within_viewport(pg, "#fl")
    ck(f"{tag}: the fleet panel is fully on screen", ok, det)

    # THE SEARCH. The needle is taken from the rendered list, so the gate
    # cannot pass by coincidence on a hard-coded unit that happens to exist.
    numeric = [r for r in all_rows if (r["set"] or "").isdigit()]
    ck(f"{tag}: rows carry set numbers to search on", len(numeric) >= 10,
       f"{len(numeric)} numeric of {len(all_rows)}")
    needle = numeric[len(numeric) // 2]["set"]
    pg.fill("#flq", needle)
    pg.wait_for_timeout(350)
    hit = rows(pg)
    ck(f"{tag}: searching {needle!r} narrows the list",
       0 < len(hit) < len(all_rows), f"{len(hit)} of {len(all_rows)}")
    ck(f"{tag}: every surviving row matches {needle!r}",
       all(needle in (r["set"] or "") for r in hit),
       str([r["set"] for r in hit if needle not in (r["set"] or "")][:4]))
    ck(f"{tag}: the searched unit itself survives",
       any(r["set"] == needle for r in hit), str([r["set"] for r in hit][:6]))
    cnt = pg.inner_text("#flcount")
    ck(f"{tag}: the count reports the filtered fraction",
       f"{len(hit)} of {len(all_rows)}" in cnt, repr(cnt))

    # A partial query must match by substring, not only by prefix.
    tail = needle[-2:]
    pg.fill("#flq", tail)
    pg.wait_for_timeout(350)
    part = rows(pg)
    ck(f"{tag}: a partial query {tail!r} matches by substring",
       any(r["set"] == needle for r in part) and len(part) >= len(hit),
       f"{len(part)} rows")

    # A query that matches nothing must say so, and show no rows -- a filter
    # that silently falls back to everything is the failure mode here.
    pg.fill("#flq", "zzzzzz")
    pg.wait_for_timeout(350)
    ck(f"{tag}: an impossible query clears the list", rows(pg) == [],
       str(rows(pg)[:3]))
    ck(f"{tag}: and says no unit matched",
       "no unit matches" in (pg.inner_text("#fllist") or "").lower(),
       repr(pg.inner_text("#fllist")[:80]))

    # Clearing it restores the full list.
    pg.fill("#flq", "")
    pg.wait_for_timeout(350)
    ck(f"{tag}: clearing the box restores every unit",
       len(rows(pg)) == len(all_rows),
       f"{len(rows(pg))} vs {len(all_rows)}")

    pg.screenshot(path=f"{OUT}/fleet_{tag}.png")

    # FEATURE 1 and FEATURE 2, both against the restored full list.
    all_rows = rows(pg)
    check_columns(pg, tag, all_rows)
    check_unit_page(pg, tag, all_rows)

    if not tap("#flclose", "the fleet Close button can be clicked"):
        return errs, pg
    pg.wait_for_timeout(300)
    ck(f"{tag}: Close dismisses the fleet screen",
       not pg.is_visible("#fl"))
    ck(f"{tag}: no page errors ({len(errs)})", not errs, str(errs[:2]))
    return errs, pg


def check_decoration_under_dialogs(ctx, tag):
    """Decorative petals stay below the dialog layer.

    Scope honestly: this is a tidiness check, NOT a legibility one. The
    petals animate, so one crossing the Fleet panel is gone in well under a
    second and a reader still gets the number. A screenshot made it look
    like obscured data because a still frame shows where a moving thing can
    be, not what a user experiences.

    It is here to stop the z-index drifting back above the dialogs by
    accident, and it is cheap. Compared by computed z-index rather than
    hit-testing: #sakura is pointer-events:none, so elementFromPoint looks
    straight through it and would pass regardless.
    """
    pg = ctx.new_page()
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(1200)
    pg.click("#menubtn")
    pg.wait_for_timeout(250)
    pg.click("#fleetbtn")
    pg.wait_for_timeout(700)

    z = pg.evaluate("""() => {
      const zi = el => {
        if (!el) return null;
        const v = getComputedStyle(el).zIndex;
        return v === 'auto' ? null : parseInt(v, 10);
      };
      const sak = document.querySelector('#sakura');
      const vis = sak && getComputedStyle(sak).display !== 'none';
      return {
        sakura: zi(sak),
        sakuraVisible: !!vis,
        fleet: zi(document.querySelector('#flwrap')),
        dialogs: ['#flwrap','#jpwrap','#stwrap','#svwrap','#fbwrap']
          .map(s => [s, zi(document.querySelector(s))])
          .filter(([, v]) => v !== null),
      };
    }""")

    # The petals are skin-gated and reduced-motion-gated. If they are not
    # painting at all in this run, say so rather than passing silently --
    # a check that quietly tests nothing is the failure mode this project
    # has already paid for once.
    if not z["sakuraVisible"] or z["sakura"] is None:
        ck(f"{tag}: decoration layer present to test", False,
           f"#sakura not rendering (visible={z['sakuraVisible']}, z={z['sakura']})")
    else:
        worst = min(v for _, v in z["dialogs"]) if z["dialogs"] else None
        ck(f"{tag}: decoration sits under every dialog "
           f"(sakura {z['sakura']} < lowest dialog {worst})",
           worst is not None and z["sakura"] < worst,
           str(z["dialogs"]))

    # Positive control: the same comparison must be capable of saying NO.
    # Without this, a bug that made every z-index null would read as a pass.
    ctrl = pg.evaluate("""() => {
      const d = document.createElement('div');
      d.style.cssText = 'position:fixed;inset:0;z-index:99999;pointer-events:none';
      d.id = '__ctrl_overlay';
      document.body.appendChild(d);
      const z = parseInt(getComputedStyle(d).zIndex, 10);
      const fl = parseInt(getComputedStyle(
        document.querySelector('#flwrap')).zIndex, 10);
      d.remove();
      return z > fl;
    }""")
    ck(f"{tag}: the z-order check can detect an overlay ON TOP (control)", ctrl)
    pg.close()


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        for tag, kw in (("phone", PHONE), ("desktop", DESKTOP)):
            ctx = b.new_context(**kw)
            run(ctx, tag)
            check_decoration_under_dialogs(ctx, tag)
            ctx.close()
        b.close()

    npass = sum(1 for _, ok, _ in results if ok)
    nfail = len(results) - npass
    print(f"\n==== {npass}/{len(results)} passed, {nfail} failed ====")
    if nfail:
        for n, ok, d in results:
            if not ok:
                print(f"  FAILED: {n} -- {d}")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
