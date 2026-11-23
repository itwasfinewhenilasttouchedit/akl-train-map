"""Render the bottom control row and the credit chip, and MEASURE them.

The point of this harness is the assertions, not the picture: a screenshot
proves it looks right, the geometry checks prove it cannot silently regress.
Checks: the skin button is a square inside the 2.5.5 minimum, the credit chip
is collapsed on load, it expands on click, and nothing in the bottom row
overlaps anything else.
"""
import sys, json
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/out"

DEVICES = [("phone", 390, 844, 3), ("desktop", 1280, 900, 1)]


def box(page, sel):
    return page.evaluate(
        """(s) => { const e = document.querySelector(s); if (!e) return null;
             const r = e.getBoundingClientRect();
             return {x:r.x, y:r.y, w:r.width, h:r.height,
                     right:r.right, bottom:r.bottom}; }""", sel)


def overlaps(a, b):
    if not a or not b:
        return False
    return not (a["right"] <= b["x"] or b["right"] <= a["x"] or
                a["bottom"] <= b["y"] or b["bottom"] <= a["y"])


fails = []
with sync_playwright() as p:
    br = p.chromium.launch()
    for name, w, h, dpr in DEVICES:
        for skin in ("pixel", "default"):
            # has_touch/is_mobile matter: the credit chip's hover expansion is
            # gated behind @media (hover:hover) and (pointer:fine). Without
            # touch emulation a 390px-wide desktop browser still reports a
            # fine pointer, so the phone case would be tested under rules a
            # real phone never applies.
            ctx = br.new_context(viewport={"width": w, "height": h},
                                 device_scale_factor=dpr,
                                 has_touch=(name == "phone"),
                                 is_mobile=(name == "phone"))
            pg = ctx.new_page()
            pg.goto(BASE, wait_until="load", timeout=60000)
            pg.evaluate("(on) => { try { localStorage.setItem('pixelSkin', on ? '1':'0'); } catch(e){} }",
                        skin == "pixel")
            pg.reload(wait_until="load", timeout=60000)
            pg.wait_for_timeout(2500)

            tag = f"{name}/{skin}"
            btn = box(pg, "#skinbtn")
            attr = box(pg, ".leaflet-control-attribution")
            bar = box(pg, "#favbar")

            # The skin toggle must be a square within the WCAG 2.5.5 minimum.
            if not btn:
                fails.append(f"{tag}: #skinbtn missing")
            else:
                if abs(btn["w"] - btn["h"]) > 1:
                    fails.append(f"{tag}: skinbtn not square {btn['w']}x{btn['h']}")
                if btn["w"] < 44 or btn["h"] < 44:
                    fails.append(f"{tag}: skinbtn below 44px {btn['w']}x{btn['h']}")
                txt = pg.inner_text("#skinbtn").strip()
                if txt != "P":
                    fails.append(f"{tag}: skinbtn label {txt!r} != 'P'")

            # Credit collapsed on load, and small.
            if not attr:
                fails.append(f"{tag}: attribution control missing (licence!)")
            else:
                if attr["w"] > 28:
                    fails.append(f"{tag}: credit not collapsed, w={attr['w']}")
                opened = pg.evaluate(
                    "() => document.querySelector('.leaflet-control-attribution')"
                    ".classList.contains('open')")
                if opened:
                    fails.append(f"{tag}: credit starts expanded")

            # The bottom row must not collide with the toggle or the credit.
            if overlaps(bar, btn):
                fails.append(f"{tag}: favbar overlaps skinbtn")
            if overlaps(bar, attr):
                fails.append(f"{tag}: favbar overlaps credit")
            if overlaps(btn, attr):
                fails.append(f"{tag}: skinbtn overlaps credit")

            # The credit text must still be present in the DOM (licence), even
            # while visually collapsed.
            atxt = pg.evaluate(
                "() => { const e=document.querySelector('.leaflet-control-attribution');"
                " return e ? e.textContent : ''; }")
            for must in ("Esri", "Auckland Transport"):
                if must not in atxt:
                    fails.append(f"{tag}: credit text lost {must!r}")

            # Expanding works.
            pg.click(".leaflet-control-attribution")
            pg.wait_for_timeout(400)
            o2 = box(pg, ".leaflet-control-attribution")
            if o2 and o2["w"] <= 28:
                fails.append(f"{tag}: credit did not expand on click (w={o2['w']})")

            print(f"[{tag}] skinbtn={btn['w']:.0f}x{btn['h']:.0f} "
                  f"credit collapsed={attr['w']:.0f}px expanded={o2['w']:.0f}px "
                  f"favbar.right={bar['right']:.0f} btn.x={btn['x']:.0f}")

            # Park the pointer in the far corner before the screenshot. After a
            # click Playwright leaves the mouse ON the element, so :hover stays
            # active and the credit photographs as expanded -- which looks
            # exactly like the collapse having failed.
            pg.evaluate("() => document.querySelector('.leaflet-control-attribution')"
                        ".classList.remove('open')")
            pg.mouse.move(2, 2)
            pg.wait_for_timeout(400)
            parked = box(pg, ".leaflet-control-attribution")
            if parked and parked["w"] > 28:
                fails.append(f"{tag}: credit still expanded with pointer away "
                             f"(w={parked['w']})")
            pg.screenshot(path=f"{OUT}/bottom_{name}_{skin}.png",
                          clip={"x": 0, "y": max(0, h - 190), "width": w, "height": 190})
            ctx.close()
    br.close()

print()
if fails:
    print("FAIL:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("all bottom-row checks passed")
