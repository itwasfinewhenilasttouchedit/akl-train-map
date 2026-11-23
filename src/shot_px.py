#!/usr/bin/env python3
"""Render tests for: pixel skin as DEFAULT + our own CSS departures board.

WHY THIS FILE EXISTS
Every claim here is one a human cannot verify by reading the diff, and that
CSS will not report. A rule targeting the wrong selector is valid CSS that
styles nothing and logs nothing; a font that fails to load silently falls back
to monospace and still "works"; and a theme default is a three-state
localStorage problem that looks like a boolean. This project has shipped each
of those classes of bug before.

Run:
  docker exec shotpx python /s.py http://127.0.0.1:8080 /out
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

PHONE = dict(viewport={"width": 390, "height": 844},
             device_scale_factor=2, is_mobile=True, has_touch=True)
DESKTOP = dict(viewport={"width": 1280, "height": 900})

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" -- {detail}" if detail else ""))


def srgb(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum_rgb(r, g, b):
    return 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b)


def parse_rgb(s):
    m = re.findall(r"[\d.]+", s or "")
    if len(m) < 3:
        return None
    return tuple(float(x) for x in m[:3])


def contrast(fg, bg):
    """Contrast ratio from two CSS rgb() strings. Computed, never eyeballed."""
    a, b = parse_rgb(fg), parse_rgb(bg)
    if not a or not b:
        return None
    la, lb = sorted((lum_rgb(*a), lum_rgb(*b)))
    return round((lb + 0.05) / (la + 0.05), 2)


def open_board(pg):
    """Open a station board the way a user does, and wait for real rows."""
    pg.evaluate("""() => {
        const t = document.querySelector('#stwrap');
        if (window.openBoard) openBoard('Waitemata');
    }""")
    try:
        pg.wait_for_selector("#stres .brow", timeout=25000)
        return True
    except Exception:
        return False


def main():
    base, out = sys.argv[1], sys.argv[2]
    with sync_playwright() as p:
        b = p.chromium.launch()

        # ================================================================
        print("== 1. pixel skin is the DEFAULT for a first-time visitor ==")
        ctx = b.new_context(**PHONE)
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.goto(base, wait_until="networkidle", timeout=90000)
        pg.wait_for_timeout(5000)

        check("body has .pixel with no stored preference",
              pg.evaluate("document.body.classList.contains('pixel')"))
        stored = pg.evaluate("localStorage.getItem('pixelSkin')")
        check("stored preference is absent, not written on load",
              stored is None, f"got {stored!r}")
        check("toggle button reports pressed",
              pg.get_attribute("#skinbtn", "aria-pressed") == "true")
        # The visible label is a bare "P" in BOTH states -- the button is a
        # 44px square icon now, so the state is carried by aria-pressed and by
        # the pressed styling, not by the words "PIXEL ON"/"PIXEL OFF". The
        # accessible name (checked next) is what must still name the action.
        check("toggle label is the single-letter icon",
              (pg.inner_text("#skinbtn") or "").strip() == "P",
              repr(pg.inner_text("#skinbtn")))
        check("aria-label offers the action (turn OFF)",
              "off" in (pg.get_attribute("#skinbtn", "aria-label") or "").lower())

        # Text must fit the box it is painted in. scrollWidth > clientWidth
        # catches a child overflowing ITSELF, but not a child overflowing its
        # PARENT -- which is how the HUD title got clipped at 390px while both
        # overflow assertions passed. Compare painted extent against the
        # container, which is what a reader actually sees.
        clipped = pg.evaluate("""() => {
            const out = [];
            const pairs = [['#hudbar h1', '#hudbar'],
                           ['#stat', '#hud'],
                           ['#sttitle', '#st']];
            pairs.forEach(([sel, par]) => {
                const e = document.querySelector(sel), p = document.querySelector(par);
                if (!e || !p) return;
                const a = e.getBoundingClientRect(), b = p.getBoundingClientRect();
                if (a.right > b.right + 1 || a.x < b.x - 1)
                    out.push(sel + ' ' + Math.round(a.right) + '>' + Math.round(b.right));
            });
            return out;
        }""")
        check("no HUD text is clipped by its container", not clipped, str(clipped))

        # The skin must be applied before any visible element paints. If the
        # class only arrived with the deferred IIFE we would see the dark
        # ground here.
        page_bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        check("page ground is the light skin, not the dark default",
              parse_rgb(page_bg) and parse_rgb(page_bg)[0] > 200, page_bg)

        # Basemap must START light rather than loading dark then swapping.
        tiles = pg.evaluate("""() => [...document.querySelectorAll('.leaflet-tile')]
                                     .map(t => t.src).slice(0, 4)""")
        dark_tiles = [t for t in tiles if "World_Dark_Gray" in t]
        check("no dark basemap tiles were ever fetched",
              not dark_tiles, f"{len(dark_tiles)} dark of {len(tiles)}")

        # ================================================================
        print("== 2. self-hosted fonts actually loaded ==")
        fonts = pg.evaluate("""async () => {
            await document.fonts.ready;
            return [...document.fonts].map(f => f.family + '|' + f.status);
        }""")
        check("Pixelify Sans loaded",
              any("Pixelify Sans" in f and "loaded" in f for f in fonts), str(fonts))
        check("Press Start 2P loaded",
              any("Press Start 2P" in f and "loaded" in f for f in fonts))
        # A fallback to monospace is invisible unless measured: compare the
        # painted family against the first family in the stack.
        fam = pg.evaluate("""() => getComputedStyle(document.body).fontFamily""")
        check("body paints Pixelify Sans", "Pixelify Sans" in fam, fam)
        reqs = pg.evaluate("""() => performance.getEntriesByType('resource')
                                     .map(r => r.name).filter(n => /font|woff/.test(n))""")
        check("fonts served from our own origin",
              all("fonts.gstatic" not in r and "fonts.googleapis" not in r
                  for r in reqs), str(reqs))
        check("no request to any Google font host",
              not pg.evaluate("""() => performance.getEntriesByType('resource')
                   .some(r => /fonts\\.(googleapis|gstatic)\\.com/.test(r.name))"""))

        # ================================================================
        print("== 3. our own departures board renders and is legible ==")
        got = open_board(pg)
        check("board opened with real rows", got)
        if got:
            ground = pg.evaluate(
                "getComputedStyle(document.querySelector('#stres')).backgroundColor")
            check("board has its own dark emissive ground",
                  parse_rgb(ground) and parse_rgb(ground)[0] < 60, ground)

            # Column headings exist (recognition over recall) and are not
            # overflowing their container at 390px.
            # Headings are a real .bhead grid row now, not a ::before string.
            # The string version could not align to grid tracks at any size.
            head = pg.evaluate("""() => {
                const e = document.querySelector('#stres .bhead');
                if (!e) return { content: 'none', size: '0px', colour: '' };
                const cs = getComputedStyle(e);
                return { content: e.textContent.trim(),
                         size: cs.fontSize, colour: cs.color };
            }""")
            check("board prints column headings",
                  "DUE" in (head.get("content") or ""), json.dumps(head))

            # Every text colour on the board, measured against the ground.
            pairs = pg.evaluate("""() => {
                const g = getComputedStyle(document.querySelector('#stres')).backgroundColor;
                const pick = s => {
                    const e = document.querySelector('#stres ' + s);
                    return e ? getComputedStyle(e).color : null;
                };
                return { ground: g, btime: pick('.btime'), bdest: pick('.bdest'),
                         bsched: pick('.bsched'), bmin: pick('.bmin') };
            }""")
            for k in ("btime", "bdest", "bsched", "bmin"):
                if not pairs.get(k):
                    continue
                r = contrast(pairs[k], pairs["ground"])
                # 4.5:1 is 1.4.3 AA for body text; this palette was chosen to
                # clear 7:1 AAA, so assert the stronger promise.
                check(f"{k} clears AAA on the board ({r}:1)", r and r >= 7,
                      f"{pairs[k]} on {pairs['ground']}")

            # The board sits inside an ivory panel: its edge is a boundary and
            # 1.4.11 wants 3:1 for that.
            edge = pg.evaluate("""() => {
                const e = document.querySelector('#stres');
                const p = e.closest('#st') || e.parentElement;
                return { board: getComputedStyle(e).backgroundColor,
                         panel: getComputedStyle(p).backgroundColor };
            }""")
            r = contrast(edge["board"], edge["panel"])
            check(f"board edge is a visible boundary ({r}:1, needs 3)",
                  r and r >= 3, json.dumps(edge))

            # Times must stay on one line -- the whole point of the earlier
            # row redesign, which this restyle must not regress.
            rows = pg.evaluate("""() => [...document.querySelectorAll('#stres .brow')]
                .map(r => {
                    const t = r.querySelector('.btime'), s = r.querySelector('.bsched');
                    return { h: Math.round(r.getBoundingClientRect().height),
                             th: t ? Math.round(t.getBoundingClientRect().height) : 0,
                             sh: s ? Math.round(s.getBoundingClientRect().height) : 0,
                             skip: !!r.querySelector('.muted') };
                })""")
            plain = [r for r in rows if not r["skip"]]
            check("board rendered plain rows", len(plain) > 0, f"{len(rows)} rows")
            if plain:
                tall = [r for r in plain if r["h"] > 52]
                check("no plain row is taller than 52px",
                      not tall, f"heights {[r['h'] for r in plain][:8]}")
                # A wrapped time is ~2x line height. Pixel fonts are chunky so
                # the threshold is higher than the default theme's.
                wrapped = [r for r in plain if r["th"] > 26 or r["sh"] > 26]
                check("no time cell wrapped to a second line",
                      not wrapped, f"th {[r['th'] for r in plain][:6]}")

            # Tabular numerals are what make the columns line up.
            check("times use tabular numerals",
                  "tabular-nums" in (pg.evaluate(
                      """() => getComputedStyle(
                           document.querySelector('#stres .btime')).fontVariantNumeric""") or ""))
            check("board time uses the pixel display face",
                  "Press Start 2P" in (pg.evaluate(
                      """() => getComputedStyle(
                           document.querySelector('#stres .btime')).fontFamily""") or ""))

            # Nothing may escape the panel at 390px.
            over = pg.evaluate("""() => {
                const p = document.querySelector('#stres');
                const pr = p.getBoundingClientRect();
                return [...p.querySelectorAll('*')].filter(e => {
                    const r = e.getBoundingClientRect();
                    return r.width && r.right > pr.right + 1;
                }).map(e => e.className + ':' + Math.round(e.getBoundingClientRect().right)
                             + '>' + Math.round(pr.right));
            }""")
            check("nothing overflows the board panel", not over, str(over[:4]))

            # ...and nothing may overflow its own CELL. This is a SEPARATE
            # failure mode and the one that actually shipped: a grid cell sized
            # for the default UI font is too narrow for the wider pixel display
            # face, so the time overran into the next column and the two
            # numbers overlapped. The panel-edge check above passed throughout,
            # because the overflow was interior. Only a screenshot caught it.
            cells = pg.evaluate("""() => {
                const out = [];
                document.querySelectorAll('#stres .brow').forEach(r => {
                    ['.btime', '.bsched', '.bdest', '.bmin', '.setlink'].forEach(s => {
                        const e = r.querySelector(s);
                        if (!e) return;
                        if (e.scrollWidth > e.clientWidth + 1)
                            out.push(s + ' ' + e.scrollWidth + '>' + e.clientWidth
                                     + ' "' + e.textContent.trim() + '"');
                    });
                });
                return out;
            }""")
            check("no board cell overflows its own column",
                  not cells, str(cells[:4]))

            # Adjacent cells must not visually collide, which is what an
            # interior overflow looks like to a reader.
            collide = pg.evaluate("""() => {
                const out = [];
                document.querySelectorAll('#stres .brow').forEach(r => {
                    const t = r.querySelector('.btime'), s = r.querySelector('.bsched');
                    if (!t || !s) return;
                    const a = t.getBoundingClientRect(), b = s.getBoundingClientRect();
                    // Compare painted text extent, not the box: the box can be
                    // narrow while the glyphs spill out of it.
                    if (a.x + t.scrollWidth > b.x + 1)
                        out.push(Math.round(a.x + t.scrollWidth) + '>' + Math.round(b.x));
                });
                return out;
            }""")
            check("due time never runs into the scheduled time",
                  not collide, str(collide[:4]))

            # The set chip belongs to the board, not to the ivory panel. A
            # white chip on a near-black ground is the generic button rule
            # leaking through.
            chip = pg.evaluate("""() => {
                const e = document.querySelector('#stres .setlink');
                if (!e) return null;
                const c = getComputedStyle(e);
                return { bg: c.backgroundColor, fg: c.color,
                         ground: getComputedStyle(
                             document.querySelector('#stres')).backgroundColor };
            }""")
            # Force the three severity variants to render. They only appear
            # naturally when a REPORTED set is due, so the local rig usually
            # shows none and the pale-tint-on-dark-board bug reached prod
            # unseen. Painting them deliberately makes this deterministic
            # rather than dependent on what happens to be running.
            pg.evaluate("""() => {
                const rows = [...document.querySelectorAll('#stres .brow')];
                ['p1','p2','p3'].forEach((p, i) => {
                    const r = rows[i]; if (!r) return;
                    const c = r.querySelector('.setlink'); if (!c) return;
                    c.classList.remove('unit');
                    c.classList.add('pee', p);
                });
            }""")
            pg.wait_for_timeout(300)
            sev = pg.evaluate("""() => ['p1','p2','p3'].map(p => {
                const e = document.querySelector('#stres .pee.' + p);
                if (!e) return null;
                const cs = getComputedStyle(e);
                return { p: p, fg: cs.color, bg: cs.backgroundColor,
                         ground: getComputedStyle(
                             document.querySelector('#stres')).backgroundColor };
            }).filter(Boolean)""")
            for v in (sev or []):
                cr = contrast(v["fg"], v["ground"])
                check(f"severity chip {v['p']} is legible on the board ({cr}:1)",
                      cr and cr >= 4.5, json.dumps(v))
                rgbv = parse_rgb(v["bg"])
                check(f"severity chip {v['p']} is not a pale slab",
                      (not rgbv) or rgbv[0] < 80, json.dumps(v))

            if chip:
                bgp = parse_rgb(chip["bg"])
                # Either transparent, or dark enough to sit on the board.
                light_slab = bgp and len(bgp) >= 3 and bgp[0] > 200 and \
                    "rgba(0, 0, 0, 0)" not in chip["bg"]
                check("set chip is not a white slab on the dark board",
                      not light_slab, json.dumps(chip))
                r = contrast(chip["fg"], chip["ground"])
                check(f"set chip label is legible on the board ({r}:1)",
                      r and r >= 4.5, json.dumps(chip))
            # The no-set cell is a dash, not a sentence -- and the row carrying
            # it must be the same height as every other row. This was a real
            # ragged-rhythm bug: the sentence version collapsed the row to 32px
            # against its neighbours' 39px.
            noset = pg.evaluate("""() => {
                const e = document.querySelector('#stres .noset');
                if (!e) return 'absent';
                const row = e.closest('.brow');
                return { text: e.textContent.trim(),
                         title: e.getAttribute('title') || '',
                         aria: e.getAttribute('aria-label') || '',
                         h: Math.round(row.getBoundingClientRect().height) };
            }""")
            if noset != "absent" and noset:
                check("no-set cell is a dash, not a sentence",
                      noset["text"] in ("\u2014", "-", "\u2013"), json.dumps(noset))
                # The explanation must survive for assistive tech even though
                # the visible text is now a glyph.
                check("no-set dash still explains itself to a screen reader",
                      "assigned" in (noset["aria"] + noset["title"]).lower(),
                      json.dumps(noset))

            # ALIGNMENT, measured structurally rather than by eye.
            # The headings used to be a space-padded string in a ::before,
            # which cannot line up with grid tracks at any size. Comparing the
            # resolved grid-template-columns of the heading row against the
            # data rows is the assertion that actually catches a regression;
            # comparing pixel offsets of the painted text would pass on a
            # padded string that merely happened to look close at one width.
            align = pg.evaluate("""() => {
                const h = document.querySelector('#stres .bhead');
                const r = document.querySelector('#stres .brow');
                if (!h || !r) return 'absent';
                const gh = getComputedStyle(h), gr = getComputedStyle(r);
                const cells = [...h.children].map(c => Math.round(
                    c.getBoundingClientRect().left));
                const rc = [...r.children].map(c => Math.round(
                    c.getBoundingClientRect().left));
                return { htpl: gh.gridTemplateColumns, rtpl: gr.gridTemplateColumns,
                         hgap: gh.columnGap, rgap: gr.columnGap,
                         hleft: cells, rleft: rc };
            }""")
            if align != "absent" and align:
                check("heading row uses the same grid tracks as the data rows",
                      align["htpl"] == align["rtpl"], json.dumps(align))
                check("heading row uses the same column gap as the data rows",
                      align["hgap"] == align["rgap"], json.dumps(align))
                # Belt and braces: the first three columns must actually paint
                # at the same x. Column 4 is an auto track whose content
                # differs, so it is excluded deliberately.
                pairs = list(zip(align["hleft"][:3], align["rleft"][:3]))
                check("heading cells paint at the same x as row cells",
                      all(abs(a - b) <= 1 for a, b in pairs), json.dumps(pairs))

            # The board must belong to the skin, not look like a hole cut in
            # the panel. Warm ground, and the boundary against the ivory panel
            # must still be unambiguous.
            ground = pg.evaluate("""() => {
                const e = document.querySelector('#stres');
                const h = document.querySelector('#stres .bhead');
                if (!e) return 'absent';
                return { bg: getComputedStyle(e).backgroundColor,
                         head: h ? getComputedStyle(h).color : null };
            }""")
            if ground != "absent" and ground:
                m = re.findall(r"\d+", ground["bg"])
                if len(m) >= 3:
                    r_, g_, b_ = int(m[0]), int(m[1]), int(m[2])
                    # Warm == red channel leads blue. A neutral near-black has
                    # r == b, which is exactly the look being fixed.
                    check("board ground is warm, not neutral near-black",
                          r_ > b_ + 8, f"rgb({r_},{g_},{b_})")
                if ground["head"]:
                    # contrast() consumes CSS rgb() strings directly.
                    cr = contrast(ground["head"], ground["bg"])
                    check(f"column headings are legible ({cr}:1)",
                          cr and cr >= 4.5,
                          f"{ground['head']} on {ground['bg']}")

            pg.screenshot(path=f"{out}/px_board.png")

        pg.screenshot(path=f"{out}/px_map.png")
        check("no page/console errors on the default view",
              not errs, str(errs[:2]))

        # ================================================================
        print("== 4. the three localStorage states ==")
        # '0' = chose OFF previously. Must be respected, or we have overridden
        # a user's explicit choice -- the actual risk of changing a default.
        c2 = b.new_context(**PHONE)
        c2.add_init_script("localStorage.setItem('pixelSkin','0')")
        p2 = c2.new_page()
        p2.goto(base, wait_until="networkidle", timeout=90000)
        p2.wait_for_timeout(3500)
        check("stored '0' keeps the DEFAULT (dark) theme",
              not p2.evaluate("document.body.classList.contains('pixel')"))
        bg2 = p2.evaluate("getComputedStyle(document.body).backgroundColor")
        check("opted-out visitor gets the dark ground",
              parse_rgb(bg2) and parse_rgb(bg2)[0] < 60, bg2)
        check("opted-out visitor fetches no light tiles",
              not p2.evaluate("""() => [...document.querySelectorAll('.leaflet-tile')]
                   .some(t => /World_Light_Gray/.test(t.src))"""))
        p2.screenshot(path=f"{out}/px_optout.png")
        c2.close()

        # '1' = chose ON. Belt and braces.
        c3 = b.new_context(**PHONE)
        c3.add_init_script("localStorage.setItem('pixelSkin','1')")
        p3 = c3.new_page()
        p3.goto(base, wait_until="networkidle", timeout=90000)
        p3.wait_for_timeout(3000)
        check("stored '1' gives the pixel skin",
              p3.evaluate("document.body.classList.contains('pixel')"))
        c3.close()

        # ================================================================
        print("== 5. the toggle still round-trips ==")
        pg.click("#skinbtn")
        pg.wait_for_timeout(900)
        check("one click turns the skin OFF",
              not pg.evaluate("document.body.classList.contains('pixel')"))
        check("turning off persists '0'",
              pg.evaluate("localStorage.getItem('pixelSkin')") == "0")
        check("button now offers to turn it on",
              "on" in (pg.get_attribute("#skinbtn", "aria-label") or "").lower())
        check("basemap swapped to dark",
              pg.evaluate("""() => [...document.querySelectorAll('.leaflet-tile')]
                   .some(t => /World_Dark_Gray/.test(t.src))"""))
        pg.click("#skinbtn")
        pg.wait_for_timeout(900)
        check("second click turns it back ON",
              pg.evaluate("document.body.classList.contains('pixel')"))
        check("turning on persists '1'",
              pg.evaluate("localStorage.getItem('pixelSkin')") == "1")

        # Board must survive a skin change while open.
        if open_board(pg):
            g = pg.evaluate(
                "getComputedStyle(document.querySelector('#stres')).backgroundColor")
            check("board keeps its dark ground after a skin round-trip",
                  parse_rgb(g) and parse_rgb(g)[0] < 60, g)

        # ================================================================
        print("== 6. desktop ==")
        dctx = b.new_context(**DESKTOP)
        dp = dctx.new_page()
        derrs = []
        dp.on("pageerror", lambda e: derrs.append(str(e)))
        dp.goto(base, wait_until="networkidle", timeout=90000)
        dp.wait_for_timeout(5000)
        check("desktop defaults to pixel",
              dp.evaluate("document.body.classList.contains('pixel')"))
        if open_board(dp):
            head = dp.evaluate("""() => {
                const e = document.querySelector('#stres .bhead');
                return e ? e.textContent.trim() : 'none';
            }""")
            check("desktop board shows the full heading row",
                  "MIN" in (head or ""), repr(head))
            rows = dp.evaluate("""() => [...document.querySelectorAll('#stres .brow')]
                .filter(r => !r.querySelector('.muted'))
                .map(r => Math.round(r.getBoundingClientRect().height))""")
            check("desktop plain rows stay compact",
                  rows and max(rows) <= 52, str(rows[:8]))
            dp.screenshot(path=f"{out}/px_desktop_board.png")
        dp.screenshot(path=f"{out}/px_desktop.png")
        check("no desktop page errors", not derrs, str(derrs[:2]))

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
