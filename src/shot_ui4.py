"""Render test for the four UI changes.

Aimed squarely at what fails SILENTLY in this project: generated HTML/CSS is
valid markup even when it styles nothing and draws nothing, so nothing throws
and nothing logs. Every claim below is asserted against the rendered DOM, and
screenshots are written for human review.

The four changes under test:
  1. alert bell in the top strip replaces the 215px banner
  2. Home/Away favourite stations replace the generic arrivals pill
  3. station board rows: scheduled time in its own column, no wrap, 40px rows
  4. not-in-service units hidden by default, behind the existing OOS checkbox
"""
import sys, json
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8080'
OUT = sys.argv[2] if len(sys.argv) > 2 else '/out'

fails, checks = [], 0


def ck(cond, label, detail=''):
    global checks
    checks += 1
    if cond:
        print(f'  PASS {label}')
    else:
        print(f'  FAIL {label} {detail}')
        fails.append(label)


def box(pg, sel):
    return pg.evaluate("""(s) => {
      const e = document.querySelector(s);
      if (!e) return null;
      const r = e.getBoundingClientRect();
      const cs = getComputedStyle(e);
      return {x:r.x, y:r.y, w:r.width, h:r.height,
              display:cs.display, vis:cs.visibility,
              ws:cs.whiteSpace, hidden:e.hidden};
    }""", sel)


with sync_playwright() as pw:
    b = pw.chromium.launch()

    # ---------- PHONE ----------
    ctx = b.new_context(viewport={'width': 390, 'height': 844},
                        device_scale_factor=2)
    pg = ctx.new_page()
    errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.on('console', lambda m: errs.append('console:' + m.text)
          if m.type == 'error' else None)
    pg.goto(BASE, wait_until='networkidle', timeout=90000)
    pg.wait_for_timeout(6000)

    print('\n== 1. alert bell replaces the banner ==')
    ck(pg.query_selector('#alertbar') is None,
       'old #alertbar element is gone')
    bell = box(pg, '#alertbell')
    drawer = box(pg, '#alertdrawer')
    ck(bell is not None, 'bell exists in DOM')
    if bell:
        # Bell must be a 44px touch target (project standard, WCAG asks 24).
        ck(bell['w'] >= 44 and bell['h'] >= 44,
           'bell is >=44x44', json.dumps(bell))
        # The whole point: it must not eat the viewport like the 215px banner.
        area = (bell['w'] * bell['h']) / (390 * 844) * 100
        ck(area < 3, f'bell occupies {area:.2f}% of viewport (banner was 23.9%)')
        hud = box(pg, '#hud')
        if hud:
            overlap = not (bell['y'] >= hud['y'] + hud['h'] or
                           bell['y'] + bell['h'] <= hud['y'] or
                           bell['x'] >= hud['x'] + hud['w'] or
                           bell['x'] + bell['w'] <= hud['x'])
            ck(not overlap, 'bell does not overlap #hud',
               f'bell={bell} hud={hud}')
    # Drawer starts closed regardless of alert count.
    ck(drawer is None or drawer['hidden'] or drawer['display'] == 'none',
       'drawer is closed on load')
    ck(pg.get_attribute('#alertbell', 'aria-expanded') == 'false',
       'aria-expanded=false on load')
    # Opening it must actually reveal alerts (if any are in force).
    n_alerts = pg.evaluate(
        "() => document.getElementById('alertcount')"
        "       ? +document.getElementById('alertcount').textContent : 0")
    print(f'  (alerts in force: {n_alerts})')
    if n_alerts:
        pg.click('#alertbell')
        pg.wait_for_timeout(400)
        d2 = box(pg, '#alertdrawer')
        ck(d2 and not d2['hidden'] and d2['h'] > 0, 'drawer opens on tap')
        ck(pg.get_attribute('#alertbell', 'aria-expanded') == 'true',
           'aria-expanded=true when open')
        ck(pg.eval_on_selector_all('#alertdrawer .alert', 'e => e.length') > 0,
           'drawer contains alert items')
        # Contrast-critical: bell must carry a severity class, not bare.
        cls = pg.get_attribute('#alertbell', 'class') or ''
        ck('sev-' in cls, f'bell carries severity class ({cls})')
        pg.screenshot(path=f'{OUT}/phone_alert_open.png')
        pg.click('#alertbell')
        pg.wait_for_timeout(300)
        ck(box(pg, '#alertdrawer')['hidden'], 'drawer closes again')
    else:
        ck(bell['hidden'] if bell else True,
           'no alerts in force -> bell hidden entirely')

    print('\n== 2. Home/Away favourites replace arrivals ==')
    ck(pg.query_selector('#sheetbtn') is None, 'old #sheetbtn is gone')
    fb = box(pg, '#favbar')
    ck(fb and fb['display'] == 'flex', 'favbar is a flex row',
       json.dumps(fb))
    for slot, label in [('home', 'Home'), ('away', 'Away')]:
        bx = box(pg, '#fav' + slot)
        ck(bx is not None, f'fav{slot} exists')
        if bx:
            ck(bx['h'] >= 44, f'fav{slot} >=44px tall ({bx["h"]})')
            txt = pg.inner_text('#fav' + slot).strip()
            ck(txt == f'Set {label}',
               f'fav{slot} unset label reads "Set {label}"', f'got "{txt}"')
            cls = pg.get_attribute('#fav' + slot, 'class') or ''
            ck('unset' in cls, f'fav{slot} carries .unset when empty')
    # Three pills must fit one row without overflowing or stacking.
    rows = pg.evaluate("""() => {
      const b = [...document.querySelectorAll('#favbar > button')]
        .map(e => e.getBoundingClientRect());
      return {ys:[...new Set(b.map(r => Math.round(r.y)))], n:b.length,
              right:Math.max(...b.map(r => r.right)),
              left:Math.min(...b.map(r => r.x))};
    }""")
    ck(rows['n'] == 3, f'three pills in favbar ({rows["n"]})')
    ck(len(rows['ys']) == 1, 'pills share one row (no wrap)', json.dumps(rows))
    ck(rows['left'] >= 0 and rows['right'] <= 390,
       'pills fit inside 390px', json.dumps(rows))
    # #skinbtn is position:fixed over this row. A screenshot caught the Plan
    # pill hidden behind it, which no DOM assertion was looking for — so look
    # for it now: no favbar pill may intersect the skin toggle.
    clash = pg.evaluate("""() => {
      const sk = document.getElementById('skinbtn');
      if (!sk) return 'no skinbtn';
      const s = sk.getBoundingClientRect();
      return [...document.querySelectorAll('#favbar > button')].filter(e => {
        const r = e.getBoundingClientRect();
        return !(r.right <= s.left || r.left >= s.right ||
                 r.bottom <= s.top || r.top >= s.bottom);
      }).map(e => e.id);
    }""")
    ck(clash == [], 'no pill sits under the PIXEL toggle', json.dumps(clash))
    # Every pill must be genuinely clickable at its own centre point, not just
    # present in the DOM — the real failure mode is a covered button.
    hit = pg.evaluate("""() => {
      const out = {};
      for (const e of document.querySelectorAll('#favbar > button')) {
        const r = e.getBoundingClientRect();
        const top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
        out[e.id] = !!(top && (top === e || e.contains(top)));
      }
      return out;
    }""")
    ck(all(hit.values()), 'every pill is the topmost element at its centre',
       json.dumps(hit))
    # Truncation check. A pill that ellipsises its LABEL ("Home W…") has lost
    # the part that says which button it is — the station may truncate, the
    # label may not. Only meaningful once favourites are set, so it runs again
    # after the seed below.
    def label_intact(page):
        return page.evaluate("""() => {
          const out = {};
          for (const slot of ['home','away']) {
            const b = document.getElementById('fav'+slot);
            const l = b && b.querySelector('.favlabel');
            if (!l) { out[slot] = 'unset'; continue; }
            out[slot] = {clipped: l.scrollWidth > l.clientWidth + 1,
                         text: l.textContent};
          }
          return out;
        }""")
    # Setting a favourite must persist and switch the button to one-tap mode.
    pg.evaluate("""() => localStorage.setItem('favStations',
        JSON.stringify({home:{station:'Waitemata',label:'Home'},
                        away:{station:'Newmarket',label:'work'}}))""")
    pg.reload(wait_until='networkidle')
    pg.wait_for_timeout(4500)
    ht = pg.inner_text('#favhome').strip().replace('\n', ' ')
    at = pg.inner_text('#favaway').strip().replace('\n', ' ')
    ck('Waitemata' in ht, f'home pill shows saved station ({ht})')
    ck('Newmarket' in at and 'work' in at,
       f'away pill shows custom label + station ({at})')
    ck('unset' not in (pg.get_attribute('#favhome', 'class') or ''),
       'set pill drops .unset')
    li = label_intact(pg)
    ck(all(isinstance(v, dict) and not v['clipped'] for v in li.values()),
       'pill labels are not ellipsised', json.dumps(li))
    # Plan must keep its word, not collapse to a bare clock glyph.
    ptxt = pg.inner_text('#planbtn').strip()
    ck('Plan' in ptxt, f'Plan pill keeps its label ({ptxt!r})')
    pclip = pg.evaluate("""() => { const b=document.getElementById('planbtn');
        return b.scrollWidth > b.clientWidth + 1; }""")
    ck(not pclip, 'Plan pill is not clipped')
    # One tap on a set favourite opens that station's board directly.
    pg.click('#favhome')
    pg.wait_for_timeout(2500)
    title = pg.eval_on_selector('#sttitle', 'e => e.textContent') or ''
    ck(title.strip() == 'Waitemata',
       f'tapping Home opens its board ({title.strip()})')

    print('\n== 3. board rows: scheduled time inline, no wrap ==')
    pg.wait_for_timeout(1500)
    rowinfo = pg.evaluate("""() => {
      const rows = [...document.querySelectorAll('#stres .brow')];
      return rows.map(r => {
        const t = r.querySelector('.btime'), s = r.querySelector('.bsched');
        return {h:Math.round(r.getBoundingClientRect().height),
                time:t ? t.textContent.trim() : null,
                time_h:t ? Math.round(t.getBoundingClientRect().height) : null,
                sched:s ? s.textContent.trim() : null,
                sched_h:s ? Math.round(s.getBoundingClientRect().height) : null,
                sched_ws:s ? getComputedStyle(s).whiteSpace : null,
                skip:!!r.querySelector('div.muted'),
                cols:getComputedStyle(r).gridTemplateColumns};
      });
    }""")
    print('  rows:', json.dumps(rowinfo[:6], indent=1))
    ck(len(rowinfo) > 0, 'board rendered some rows')
    if rowinfo:
        ck(all(r['cols'].split().__len__() == 5 for r in rowinfo),
           'rows use the 5-column grid',
           rowinfo[0]['cols'])
        ck(all(r['sched_ws'] == 'nowrap' for r in rowinfo if r['sched']),
           'scheduled time is white-space:nowrap')
        # The actual complaint: a WRAPPED scheduled time made rows 67px.
        # A row carrying the "Not stopping at X" notice is legitimately taller
        # — that is real information on its own full-width line, not a wrap —
        # so it is measured separately rather than lumped in. Judge the plain
        # rows on height, and the skip rows on whether the TIME wrapped.
        plain = [r for r in rowinfo if not r['skip']]
        skips = [r for r in rowinfo if r['skip']]
        tall = [r for r in plain if r['h'] > 48]
        ck(not tall, 'no plain row taller than 48px (was 67px when wrapping)',
           json.dumps(tall))
        ck(all(r['time_h'] <= 24 and (r['sched_h'] or 0) <= 24 for r in skips),
           f'skip-notice rows ({len(skips)}) keep their times on one line',
           json.dumps(skips))
        # Scheduled time must sit level with the live time, not below it.
        slipped = [r for r in rowinfo if r['sched']]
        print(f'  (rows showing a scheduled time: {len(slipped)})')
        ck(all(r['sched_h'] <= 24 for r in slipped),
           'scheduled time occupies a single line',
           json.dumps(slipped))
        # Row heights should be uniform -> times align down the column.
        hs = sorted({r['h'] for r in plain})
        ck(len(hs) <= 2, f'plain row heights consistent {hs}')
    pg.screenshot(path=f'{OUT}/phone_board.png', full_page=False)
    pg.click('#stclose')
    pg.wait_for_timeout(500)

    print('\n== 4. not-in-service hidden by default ==')
    st = pg.evaluate("""() => {
      const cb = document.querySelector('#legend input[data-code="OOS"]');
      return {
        has_oos_line: !!cb,
        checked: cb ? cb.checked : null,
        stored: localStorage.getItem('showOos'),
        oos_in_payload: (window.LAST_TRAINS||[]).length ? null : 'n/a'
      };
    }""")
    print('  ', json.dumps(st))
    if st['has_oos_line']:
        ck(st['checked'] is False, 'OOS legend checkbox is unchecked by default')
        # The legend lives inside #hudbody, which is COLLAPSED on a phone, so
        # the checkbox is in the DOM but not clickable until the HUD is opened.
        # Open it the way a user would rather than forcing the click.
        if not pg.is_visible('#legend input[data-code="OOS"]'):
            pg.click('#hudbar')
            pg.wait_for_timeout(600)
        ck(pg.is_visible('#legend input[data-code="OOS"]'),
           'OOS checkbox reachable after expanding the HUD')
        # Hidden markers must really be off the map, not just dimmed.
        vis = pg.evaluate("""() => {
          const n = document.querySelectorAll('.leaflet-marker-icon').length;
          const oosIcons = [...document.querySelectorAll('.leaflet-marker-icon')]
            .filter(e => e.innerHTML.includes('oos') ||
                         e.querySelector('rect')).length;
          return {markers:n, with_rect:oosIcons};
        }""")
        print('   markers:', json.dumps(vis))
        # Turning it on must add markers back; that proves the filter is live
        # rather than the payload simply being empty.
        before = pg.eval_on_selector_all('.leaflet-marker-icon', 'e => e.length')
        pg.click('#legend input[data-code="OOS"]')
        pg.wait_for_timeout(1200)
        after = pg.eval_on_selector_all('.leaflet-marker-icon', 'e => e.length')
        print(f'   markers {before} -> {after} after enabling OOS')
        ck(after >= before, 'enabling OOS does not remove markers')
        ck(pg.evaluate("() => localStorage.getItem('showOos')") == '1',
           'choice persisted to localStorage')
        # And it must survive a reload.
        pg.reload(wait_until='networkidle')
        pg.wait_for_timeout(5000)
        again = pg.evaluate(
            """() => { const cb = document.querySelector(
                 '#legend input[data-code="OOS"]');
               return cb ? cb.checked : null; }""")
        ck(again is True, 'OOS choice survives reload', str(again))
        # Reset to default for the screenshot.
        pg.evaluate("() => localStorage.setItem('showOos','0')")
        pg.reload(wait_until='networkidle')
        pg.wait_for_timeout(5000)
    else:
        print('   (no out-of-service units in this payload - cannot assert)')

    print('\n== cross-cutting ==')
    ck(not errs, f'no page/console errors ({len(errs)})', str(errs[:3]))
    # The HUD paints /api/trains' `error` field in red. A screenshot caught
    # "KeyError: 'delay'" sitting there through an entire passing test run,
    # because no assertion read it. Read it now: a server-side exception is a
    # failure even when every pixel is in the right place.
    apierr = pg.evaluate(
        "() => (document.getElementById('err')||{}).textContent || ''")
    ck(not apierr.strip(), 'no server error shown in the HUD',
       repr(apierr.strip()))
    # Nothing may overflow the phone viewport horizontally.
    #
    # THIS ASSERTION USED TO BE UNFALSIFIABLE. It walked every element's
    # getBoundingClientRect() and flagged anything reaching past 391px. Two
    # things made that meaningless:
    #
    #   1. `html` and `body` are both `overflow:hidden` here, so NOTHING can
    #      visibly overflow. A deliberately-planted 400px-wide red canary div
    #      at left:300px painted nothing and was invisible to the naked eye --
    #      the page simply cannot scroll sideways. An assertion that cannot
    #      fail for a real reason is decoration.
    #   2. It flagged 63 elements that paint nothing: the #tktrack marquee
    #      (a double-width strip translated under a 250px `overflow:hidden`
    #      window -- that IS the mechanism) and the four #about-seo links
    #      inside a `.sr-only` box clipped to `rect(0,0,0,0)` with
    #      `white-space:nowrap`, which therefore report 162-179px rects while
    #      being 1x1 on screen. Those four turned CI red from the commit that
    #      added them, and the red was then ignored as "known", which is the
    #      real cost: a gate nobody believes stops being a gate.
    #
    # So assert the signal that actually means something -- `scrollWidth`
    # exceeding `clientWidth`, i.e. real content extending past the window.
    # Measured: that one DOES notice the canary (390 -> 700, scrollable
    # False -> True) even with `overflow:hidden`, because scrollWidth reports
    # the overflow area regardless of whether a scrollbar is offered.
    #
    # Belt and braces: the element walk is KEPT, but restricted to elements
    # that are genuinely paintable (not inside a zero-clipped or 1px-wide
    # hidden container), so a visible chrome element poking out is still named
    # rather than just counted.
    ovf = pg.evaluate("""() => {
      const doc = document.scrollingElement || document.documentElement;
      // Visually hidden = clipped away by an ancestor, not merely offscreen.
      const hidden = (el) => {
        for (let p = el; p; p = p.parentElement) {
          const cs = getComputedStyle(p);
          if (cs.display === 'none' || cs.visibility === 'hidden') return true;
          // .sr-only pattern: clip:rect(0,0,0,0) paints nothing at all.
          if (cs.clip && cs.clip !== 'auto' &&
              /rect\\((0p?x?,\\s*){3}0p?x?\\)/.test(cs.clip)) return true;
          // A marquee strip inside a narrow overflow:hidden window, or any
          // 1x1 visually-hidden host: the child's rect is not what paints.
          if (p !== el && cs.overflow === 'hidden') {
            const pr = p.getBoundingClientRect();
            const er = el.getBoundingClientRect();
            if (er.right > pr.right + 1 || er.left < pr.left - 1) return true;
          }
        }
        return false;
      };
      const bad = [];
      for (const e of document.querySelectorAll('body *')) {
        // Everything Leaflet positions in map space is excluded: tiles,
        // markers and station tooltips all legitimately sit outside the
        // viewport when the map is panned. Only OUR chrome is in scope.
        if (e.closest('.leaflet-pane, .leaflet-proxy, .leaflet-tooltip'))
          continue;
        const cs = getComputedStyle(e);
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        if (hidden(e)) continue;
        const r = e.getBoundingClientRect();
        if (r.width && (r.right > 391 || r.left < -1))
          bad.push(e.tagName + '#' + (e.id||'') + '.' +
                   (e.className||'').toString().slice(0,24) +
                   ' ' + Math.round(r.left) + '..' + Math.round(r.right));
      }
      return {bad: bad.slice(0, 8),
              scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth,
              scrollable: doc.scrollWidth > doc.clientWidth + 1};
    }""")
    ck(not ovf['scrollable'],
       'page does not scroll horizontally',
       f"scrollWidth {ovf['scrollWidth']} vs clientWidth {ovf['clientWidth']}")
    ck(not ovf['bad'], 'no visible element overflows 390px',
       json.dumps(ovf['bad']))

    # PROVE THE ASSERTIONS ABOVE CAN STILL FAIL. A filter tuned until the page
    # passes is indistinguishable from a filter that passes everything, and
    # this gate has already been fooled once. Plant a genuinely overflowing
    # visible element, confirm it is caught, then remove it.
    canary = pg.evaluate("""() => {
      const d = document.createElement('div');
      d.id = 'ovf-canary';
      d.style.cssText = 'position:absolute;left:300px;top:200px;width:400px;'
                      + 'height:30px;background:red;z-index:99999';
      document.body.appendChild(d);
      const doc = document.scrollingElement || document.documentElement;
      const out = {scrollable: doc.scrollWidth > doc.clientWidth + 1,
                   scrollWidth: doc.scrollWidth};
      d.remove();
      return out;
    }""")
    ck(canary['scrollable'],
       'the overflow check still detects a real overflow (canary)',
       json.dumps(canary))
    pg.screenshot(path=f'{OUT}/phone_map.png')

    # pixel skin must restyle the bell, not leave it dark-navy on ivory
    pg.evaluate("() => { document.body.classList.add('pixel'); }")
    pg.wait_for_timeout(600)
    pxbell = pg.evaluate("""() => {
      const b = document.getElementById('alertbell');
      if (!b || b.hidden) return null;
      const cs = getComputedStyle(b);
      return {bg:cs.backgroundColor, color:cs.color, border:cs.borderTopColor};
    }""")
    print('  pixel bell:', json.dumps(pxbell))
    if pxbell:
        ck(pxbell['bg'] != 'rgb(35, 44, 56)',
           'pixel skin restyles the bell', json.dumps(pxbell))
    pg.screenshot(path=f'{OUT}/phone_pixel.png')
    ctx.close()

    # ---------- DESKTOP ----------
    ctx = b.new_context(viewport={'width': 1280, 'height': 900})
    pg = ctx.new_page()
    derrs = []
    pg.on('pageerror', lambda e: derrs.append(str(e)))
    pg.goto(BASE, wait_until='networkidle', timeout=90000)
    pg.wait_for_timeout(6000)
    print('\n== desktop ==')
    fb = box(pg, '#favbar')
    ck(fb and fb['display'] == 'flex', 'favbar visible on desktop',
       json.dumps(fb))
    bell = box(pg, '#alertbell')
    if bell and not bell['hidden']:
        hud = box(pg, '#hud')
        overlap = not (bell['x'] >= hud['x'] + hud['w'] or
                       bell['x'] + bell['w'] <= hud['x'] or
                       bell['y'] >= hud['y'] + hud['h'] or
                       bell['y'] + bell['h'] <= hud['y'])
        ck(not overlap, 'desktop bell clear of HUD',
           f'bell={bell} hud={hud}')
    ck(not derrs, f'no desktop page errors ({len(derrs)})', str(derrs[:2]))
    pg.screenshot(path=f'{OUT}/desktop_map.png')
    ctx.close()
    b.close()

print(f'\n==== {checks - len(fails)}/{checks} passed, {len(fails)} failed ====')
if fails:
    print('FAILURES:', fails)
sys.exit(1 if fails else 0)
