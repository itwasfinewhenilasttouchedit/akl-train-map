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
    return pg.evaluate(
        "() => [...document.querySelectorAll('#fllist .flrow')]"
        ".map(r => ({set: r.dataset.set,"
        "            unit: (r.querySelector('.flu')||{}).textContent,"
        "            live: r.classList.contains('live')}))")


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
