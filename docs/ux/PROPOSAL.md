# PROPOSAL — refreshed design

Scope: a visual and interaction refresh of `web/index.html`, implementable in plain
HTML/CSS/vanilla JS in the existing single file, with Leaflet from CDN and no build step.
Every token, size and string below is a concrete value an implementer can paste.
Companion documents: `RESEARCH.md` (who/why), `AUDIT.md` (findings, `A-nn` ids referenced
throughout), `PRIORITISED_PLAN.md` (sequencing), `mockup.html` (visual reference).

Three design moves carry the refresh:

1. **Move the controls into the thumb.** A persistent bottom action bar replaces the top-left
   HUD as the place where things happen. (A-10, A-11, A-12)
2. **Make ambiguity explicit.** When a tap could mean two things, ask — a chooser, not a guess.
   The honesty principle applied to the interaction layer, not just the copy. (A-01)
3. **One certainty vocabulary.** Five certainty levels, one visual treatment each, used
   identically in the map, the list, the board, the planner and the popup. (A-06, A-14, A-17, A-20)

---

## 1. Information architecture

The app answers four questions. The IA makes each one reachable in one tap from rest.

```
MAP (always present, full-bleed, the product)
│
├─ STATUS STRIP ............ top, non-interactive, 40px
│    live dot · "18 in service" · "updated 7s ago" · source
│
├─ BOTTOM ACTION BAR ....... persistent, 64px + safe-area, thumb zone
│    [ Near me ]  [ Arrivals ]  [ Plan ]  [ ⋯ More ]
│
├─ SHEETS (one at a time, slide up from the bar, swipe- or button-dismissed)
│    ├─ Near me  → station picked by GPS → DEPARTURE BOARD
│    ├─ Arrivals → "In service (18)" / "Not in service (51)" sections
│    ├─ Plan     → origin/destination → departures with risk
│    └─ More     → legend + line toggles, most-reported units, about/data notes
│
├─ DETAIL SHEET (replaces the Leaflet popup on phones)
│    train identity · next stop · ETA/platform/delay · certainty chips
│    · 6-car inference · reports log · [Report this train]
│
├─ DEPARTURE BOARD SHEET (from a station tap, or from Near me)
│    station name · next 8 departures · per-row certainty chip
│
├─ REPORT DIALOG (modal; the only true modal in the app)
│
└─ CHOOSER (transient, anchored at the tap point, when a tap is ambiguous)
```

**What moved and why**

| Was | Becomes | Why |
|---|---|---|
| `#hud` top-left, collapsible, holds legend + status + worst-units | Status strip (top, read-only) + "More" sheet (bottom) | A-10: nothing persistent should require a hand-shuffle. A-12: a 33%-of-viewport header at 320px fails Reflow. |
| `#sheetbtn` / `#planbtn` — two 34px pills floating over the map | Three/four 48px targets in a solid bar | A-03, A-11: the pills were undersized and the arrivals sheet rendered underneath them. |
| GPS buried inside `#jpwrap` as `#jpgps` | First-class "Near me" in the bar | J1 in RESEARCH.md §6: the most common job was two taps deep behind a planning intent the user did not have. |
| Leaflet popup (`popupFor()`) on phones | Detail sheet, same content, bottom-anchored | Popup measured 211×154px anchored to a marker that moves every 10s; a bottom sheet is thumb-reachable, can hold the reports log, and does not reposition under the user's finger. **Desktop keeps the popup** — it is the right pattern with a mouse and a stable pointer. |
| Reports log nested inside the popup (`.log`) | A section of the detail sheet | Same `logHtml()` string-builder (its race-proof design is preserved — AUDIT "do not fix"). |

Not moved, deliberately: the journey planner stays a separate surface from the map
(`DECISIONS.md`: planning is a different intent from browsing live positions).

---

## 2. Layout — phone, 390 × 844

```
┌───────────────────────────────────────────┐ y=0
│ ●  18 in service  · +51 not in service    │  STATUS STRIP   h=40  (+ safe-area top)
│    updated 7s ago · live GPS positions    │  non-interactive, 2 lines, 11.5/12px
├───────────────────────────────────────────┤ y=40
│                                           │
│                                           │
│                MAP                        │  h = 844 − 40 − 64 − safe-area
│         (full width, full bleed)          │  ≈ 740 usable
│                                           │
│     ▲ trains   ○ stations   ── lines      │
│                                           │
│                                           │
├───────────────────────────────────────────┤ y=780
│  ◎        ≡         ◷         ⋯           │  BOTTOM ACTION BAR  h=64
│ Near me  Arrivals  Plan      More         │  4 × 48px targets, 97px pitch
└───────────────────────────────────────────┘ y=844 (+ env(safe-area-inset-bottom))
```

Bar arithmetic at 390px: `padding 8px` each side → 374px for 4 cells → **93.5px per cell**,
each containing a 48×48 touch target centred, so adjacent target edges are **45.5px apart** —
comfortably past 2.5.8's 24px spacing circle requirement.

**Sheet open** (Arrivals / Near me / Plan / More / Detail / Board):

```
├───────────────────────────────────────────┤ y=40
│                MAP (still visible)        │  ≥ 180px of map always remains
├═══════════════════════════════════════════┤ y=224   ← sheet top, max-height 70vh
│  ────                                     │  grab handle, 36×4px, decorative
│  Arrivals                            ✕    │  sheet title h2 + 48px close
│ ─────────────────────────────────────────  │
│  IN SERVICE (18)                   sticky │
│  │ Manukau        AMP 1114   ⚠2     7m    │  48px rows
│  │ Swanson        AMP 0671   ☠      12m   │
│  …                                        │
│  NOT IN SERVICE (51)         ▸ collapsed  │  A-24
├───────────────────────────────────────────┤ y=780
│  ◎  Near me   ≡ Arrivals*  ◷ Plan  ⋯ More │  bar stays, current item marked
└───────────────────────────────────────────┘
```

Rules:
- A sheet never covers the bar; the bar's active item is marked with `aria-current="true"`
  and a 3px top border in `--focus`.
- A sheet never covers more than **70vh**, so at least ~180px of map stays visible — the user
  keeps spatial context (Nielsen #6).
- Only one sheet at a time. Opening another replaces it. (Prevents the overlay-collision class
  of bug recorded in `MEMORY.md`.)
- Dismiss: the ✕ (48×48), tapping the map above the sheet, Escape, or a downward swipe.
  **The swipe is an addition, never the only route** — WCAG 2.5.7 Dragging Movements (AA).

**Report dialog** is the exception: a true centred modal with a backdrop, because it is a
commit-flow that should not be dismissed by a stray map tap mid-typing.

## 3. Layout — desktop, 1280 × 900

Desktop keeps a left rail rather than adopting the phone bar; a 64px bottom bar on a 1280px
screen wastes the pointer's precision.

```
┌──────────┬────────────────────────────────────────────────┬──────┐
│ RAIL 320 │                                                │ zoom │ 12
│          │                                                │  +   │
│ ● 18 in  │                 MAP                            │  −   │
│   service│                                                │      │
│ updated  │        (Leaflet popup retained here)           │      │
│   7s ago │                                                │      │
│ ───────  │                                                │      │
│ Lines    │                                                │      │
│ ☑ E-W 12 │                                                │      │
│ ☑ S-C  4 │                                                │      │
│ ☑ O-W  1 │                                                │      │
│ ☑ HUIA 0 │                                                │      │
│ ☑ OOS 51 │                                                │      │
│ ───────  │                                                │      │
│ Arrivals │                                                │      │
│ (scroll) │                                                │      │
│ ───────  │                                                │      │
│ Most     │                                                │      │
│ reported │                                                │      │
│ ───────  │                                                │      │
│[Near me] │                                                │      │
│[Plan a   │                                                │      │
│  journey]│                                                │      │
└──────────┴────────────────────────────────────────────────┴──────┘
   12px gutter, rail is a single scrolling column, 876px tall
```

- Rail width **320px** (was 290px for `#hud` plus a separate 268px `#list` — two panels stacked
  vertically with a gap in between, which wasted the column).
- Leaflet zoom controls stay visible on desktop at top-right (they are correctly hidden on
  phones at index.html:301 — pinch is the natural gesture and they collided with `#chev`).
- Planner and Report open as centred dialogs, as today, at 420px wide.
- The breakpoint stays **`max-width:640px`**, matching the existing media query at index.html:296
  and the `isPhone()` test at :459 — changing it would desynchronise the CSS from the JS.

---

## 4. Interaction flows

### 4.1 J1 — "When is my train, from where I am?" (1 tap)

```
tap [Near me]
  → bar item shows a spinner, status strip announces "finding you…" (aria-live)
  → navigator.geolocation.getCurrentPosition(…, {timeout:10000, maximumAge:60000})
  → GET /api/nearest?lat&lon
  ├─ near:true  → Departure Board sheet opens, titled:
  │                "Newmarket"  ·  subtitle "220 m away · from your location"
  ├─ near:false → Board opens anyway, titled "Pukekohe", with a banner:
  │                "Closest station is Pukekohe, 14.2 km away — you may not be
  │                 near the train network."      ← existing honest copy, kept
  ├─ denied     → sheet opens on the station list with:
  │                "Location permission declined — pick a station."   ← no nagging
  └─ no support → "This browser can't do location — pick a station."
```

The existing `#jpgps` handler (index.html:1576-1607) already implements every one of these
branches correctly, including the "you may not be near the train network" honesty case. This
flow **reuses that logic verbatim** and only changes where it is triggered from.

### 4.2 J2 — the ambiguity chooser (the A-01 fix)

Today: `stationNear(x, y, 20)` returns the single nearest station and acts on it silently.
Measured ambiguity: 26 of 48 stations have a neighbour within 24px; 58% of trains sit within
20px of a station dot.

Proposed algorithm — a minimal change to the existing capture-phase handler at index.html:532:

```js
// Collect ALL candidates within the touch radius, not just the nearest.
function candidatesNear(clientX, clientY, maxPx) {
  const mr = document.getElementById('map').getBoundingClientRect();
  const p  = L.point(clientX - mr.x, clientY - mr.y);
  const out = [];
  for (const s of STATION_PTS) {
    const d = p.distanceTo(map.latLngToContainerPoint(s.ll));
    if (d < maxPx) out.push({kind:'station', name:s.name, d});
  }
  for (const [id, m] of markers) {
    const d = p.distanceTo(map.latLngToContainerPoint(m.getLatLng()));
    if (d < maxPx) out.push({kind:'train', id, t:m._t, d});
  }
  return out.sort((a,b) => a.d - b.d);
}
```

Then:

| Candidates | Behaviour |
|---|---|
| 0 | Nothing. (Unchanged.) |
| 1 | Act immediately — board or detail sheet. **No regression on the common case.** |
| 2+ | Render the chooser. |

Chooser spec:

- Anchored at the tap point, flipped to stay on screen, `position:absolute; z-index:1100`.
- `role="menu"`, `aria-label="Several things here — which did you mean?"`.
- Header row, 12px, `--fg-2`: **"Which one?"**
- One row per candidate, **48px tall**, max 5 rows then scroll, ordered nearest-first:
  - station → `○  The Strand` + right-aligned `departures`
  - train   → `▲  AMP 932 · Manukau` + right-aligned `7m`, with the line colour as a 4px left bar
    and the certainty chip if the set has one
- First row receives focus on open; ↑/↓ move, Enter selects, Escape dismisses and **does nothing**
  (no fallback guess — that is the whole point).
- Tapping the backdrop dismisses without acting.
- Each row has an accessible name including its kind: `"Station, The Strand, departures"` /
  `"Train, AMP 932 to Manukau, 7 minutes"`.

Radius stays **20px** (index.html:540). Widening it is tempting and wrong: a wider radius makes
*more* taps ambiguous, and the chooser is what makes ambiguity safe, not the radius.

Desktop: the chooser only appears when 2+ candidates fall within **10px** — a mouse is precise,
and a chooser on every click would be a tax on P4.

### 4.3 Map decluttering (A-01's cosmetic half, and the "reads as cluttered" complaint)

Up to ~80 markers + 48 labels + 4 polylines + badges. Three rules, all pure presentation:

1. **Label collision suppression.** At each `zoomend`, compute each station's nearest neighbour
   in container px. If `< 34px` (the measured width of a label plus padding is ~50px, and 34px
   is where two labels begin to overlap at 11.5px type), hide the *lower-priority* label and keep
   the dot. Priority order: interchange stations (Newmarket, Waitemata, Otahuhu, Papakura,
   Puhinui, Maungawhau) > everything else > alphabetical as the tiebreak so the choice is stable
   frame to frame. At zoom ≥ 13 all labels return. Replaces the current all-or-nothing
   `.lowzoom .stname { display:none }` (index.html:225) — which stays as the floor below zoom 11.
2. **Out-of-service de-emphasis by default.** The hollow squares are 51 of 52 markers off-peak.
   Default the `OOS` legend toggle to **off** at zoom < 12, with the status strip saying
   `+51 not in service` as a tappable affordance that switches them on. P4 loses nothing (one
   tap, and the state persists in localStorage next to `hudCollapsed`); P1 gets a map with 18
   things on it instead of 69.
3. **Badge budget.** A marker shows at most **two** badges (fault triangle + skull), which is
   already enforced by the `shifted` logic at index.html:652-653. Keep. Do not add a third.

None of these change what data exists — only what is drawn at a given zoom. The counts in the
legend and the status strip continue to report the true totals, so nothing is hidden silently.

### 4.4 J3/J4 — see a report, file a report

```
tap train (or choose it from the chooser)
  → DETAIL SHEET
      ┌ Manukau                                    [PEE TRAIN] ┐
      │ AMP 1114 · Eastern / Western                           │
      │ Swanson → Manukau                                      │
      │ ─────────────────────────────────────────────────────  │
      │ next stop   Otahuhu        (left Sylvia Park)          │
      │ arrives 5m · platform 1 · 3 min late                   │
      │ 9.5 km/h · Few seats                    fix 3s ago     │
      │ ─────────────────────────────────────────────────────  │
      │ [i] 6-car service · front half (cars 1–3) · with set   │
      │     674        — inferred, tap to see how we know      │
      │ ⚠  Most reports: Car 2 of 3  (3)                       │
      │ ─────────────────────────────────────────────────────  │
      │ Reports (4)                                   ▾        │
      │ [ Report this train ]                                  │
      └────────────────────────────────────────────────────────┘
  → tap [Report this train] → REPORT DIALOG (unchanged flow, resized controls)
```

Report dialog field order is preserved exactly as today, because the order encodes real logic:
category first (it decides whether the note box or the behaviour list is shown —
`catChanged()` at index.html:1230), then severity + date, then note *or* behaviour, then the
consist inference line, then length, then carriage, then case ref.

Changes are sizing and semantics only:
- All `select`/`input` → `min-height:48px`, `font-size:16px` (16px prevents iOS auto-zoom on focus).
- `#lenpick .len` already 107×48 — **the size reference for the whole app**.
- `#carpick .car` 45×41 → **48×56** for 3-car; for 6-car the row holds 7 cells at 390px
  (7 × 48 = 336px + 6 × 6px gaps = 372px, fits inside 374px) — so the 6-car picker can meet the
  48px floor without wrapping. Today it is 44.9px wide and 41.4px tall.
- Actions: **Send report** full-width 52px primary on its own row; **Cancel** a 44px text button
  below it. Never side by side within a thumb-width (A-03).
- `role="dialog" aria-modal="true" aria-labelledby="fbtitle"`, focus to the title on open,
  Tab trapped, focus returned to the originating control on close (A-04).

### 4.5 J5 — plan a journey

Unchanged in substance. The honesty copy at index.html:1655-1656 is the best string in the app
and is reproduced verbatim. Additions only:

- Each result row gets the **certainty chip** from §6.4 so a board and a planner row look
  identical for the same train (A-17, WCAG 3.2.4).
- When *every* row in a result set is `unknown`, add one explanatory line above the list rather
  than repeating the sentence on each row:
  > **Nothing is rostered yet.** AT assigns trains to late-evening services closer to departure.
  > We'll show set numbers here as soon as they appear.
- Origin/destination `select`s → 48px, 16px type, with a swap button (⇅, 48×48) between them.
- Last trip is already remembered in localStorage (index.html:1545-1550) — keep.

### 4.6 J6 — voting on a report

Same two-vote/margin-of-two rule, same resolved-stays-visible behaviour. Presentation only:

```
 Still a problem?
 [ ⚠ Still a problem (3) ]      ← 44px tall, 10px gap
 [ ✓ Fine now (0) ]
```
Visible text now matches `aria-label` (A-19), the thumb glyphs are gone, and the buttons stack
at 390px rather than being crushed to 68×21.

---

## 5. Design system — named tokens, actual values

All contrast figures below were computed, not estimated. Surfaces are
`--bg #0d1117`, `--panel #161c24`, `--panel-2 #1C232E`, `--field #0F141B`.

### 5.1 Colour

```css
:root{
  /* surfaces — unchanged bases, one new elevation */
  --bg:        #0d1117;   /* map backdrop, page */
  --panel:     #161c24;   /* sheets, rail, dialogs */
  --panel-2:   #1C232E;   /* raised rows, sticky section headers, chooser */
  --field:     #0F141B;   /* inputs, selects, textareas */

  /* boundaries */
  --edge:      #626D7E;   /* component borders. 3.27 panel / 3.61 bg / 3.53 field — AA 1.4.11 */
  --rule:      #2B3440;   /* decorative hairline dividers INSIDE a panel only */

  /* text */
  --fg:        #E6EDF5;   /* 14.52 on panel, 16.04 on bg */
  --fg-2:      #9FB0C3;   /* secondary. 7.73 on panel, 8.54 on bg */
  --fg-3:      #7E8DA0;   /* tertiary, timestamps. 5.06 on panel — still AA */

  /* interactive */
  --accent:    #1F5BD7;   /* primary fill; #FFF on it = 5.94 — AA */
  --accent-hi: #3B7BF0;   /* hover only */
  --focus:     #7FB3FF;   /* focus ring + selected state. 7.99 on panel */

  /* status */
  --live:      #3FB950;   /* fresh feed      6.74 on panel */
  --stale:     #D29922;   /* 45–150s old     6.79 */
  --dead:      #F85149;   /* >150s / error   5.11 */
  --ok:        #4FD272;   /* praise/positive 8.81 */

  /* AT line colours — FIXED BY AUCKLAND TRANSPORT, DO NOT REDESIGN */
  --l-sc:   #D52923;  /* Southern / Onehunga loop   3.76 on bg  */
  --l-ew:   #97C93D;  /* Eastern / Western          9.68 on bg  */
  --l-ow:   #00AEEF;  /* Onehunga                   7.48 on bg  */
  --l-huia: #c678dd;  /* Pukekohe shuttle           6.43 on bg  */
  /* All four clear 1.4.11's 3:1 as graphical objects. S-C at 3.76 is the
     weakest: use it for swatches, polylines and left-bars only, NEVER as
     text colour (it would fail 1.4.3). */
}
```

Severity / certainty chip pairs (foreground on background, all measured):

| Chip | fg | bg | ratio |
|---|---|---|---|
| severity 1 | `#F2DFA0` | `#3A3520` | 9.26 |
| severity 2 | `#FFC08A` | `#45321D` | 7.62 |
| severity 3 | `#FFC2C2` | `#5A1F22` | 8.31 |
| praised | `#A8E6B4` | `#1D3A24` | 8.70 |
| unknown | `#C3CEDE` | `#262E3B` | 8.59 |
| inferred | `#A9CFF0` | `#16202C` | 10.08 |
| behaviour (people) | `#BDCCF0` | `#2D3550` | 7.52 |

These are deliberate small lifts on the current values (`#d9c57a`, `#f0a35e`, `#ff8a8a`,
`#8fd79b`, `#9ec5e8`, `#a9bbe8`) — same hues, same semantics, more headroom for sunlight.

### 5.2 Type

System stack, unchanged from index.html:15 but with the NZ-common faces first:

```css
--font: -apple-system, "Segoe UI", system-ui, Roboto, "Helvetica Neue", sans-serif;
```

| Token | px / line-height | Weight | Used for |
|---|---|---|---|
| `--t-display` | 28 / 1.1 | 700, tabular | the big ETA on a detail sheet |
| `--t-h1` | 20 / 1.25 | 700 | sheet titles, dialog titles |
| `--t-h2` | 17 / 1.3 | 600 | section headers inside a sheet |
| `--t-body` | 15 / 1.45 | 400 | everything a rider reads |
| `--t-body-strong` | 15 / 1.45 | 600 | destination, station name, set number |
| `--t-small` | 13 / 1.4 | 400 | supporting detail, "left Sylvia Park" |
| `--t-chip` | 12 / 1.2 | 700 | certainty and severity chips |
| `--t-micro` | 11.5 / 1.3 | 500 | timestamps, fix age, attribution |
| `--t-label` | 11.5 / 1.2 | 700, 0.04em caps | section labels, "IN SERVICE (18)" |

**Floor is 11.5px; nothing smaller ships.** Today 254 of ~268 sized elements are at 10 or
10.5px (A-15). Station labels `.stname` go 10 → **11.5px**, keeping the existing five-layer
`text-shadow` halo verbatim — it is doing real work against the OSM basemap's own place names.
All numeric runs (ETA, counts, times, set numbers) use `font-variant-numeric: tabular-nums`,
as `#list .eta` already does at index.html:59.

Form inputs are **16px** specifically, so iOS Safari does not zoom the viewport on focus.

### 5.3 Spacing — 4px base

```css
--s-1:4px; --s-2:8px; --s-3:12px; --s-4:16px; --s-5:20px; --s-6:24px; --s-8:32px;
```

| Token | px | Use |
|---|---|---|
| `--r-sm` | 8 | chips, swatches |
| `--r-md` | 12 | buttons, inputs, rows |
| `--r-lg` | 16 | sheets, dialogs, panels |
| `--r-pill` | 999 | bar items, toasts |
| `--bar-h` | 64 | bottom action bar |
| `--strip-h` | 40 | top status strip |
| `--tap` | 48 | **minimum interactive height, everywhere** |
| `--tap-lg` | 56 | primary commit actions |
| `--z-map` | 0 · `--z-panel` 600 · `--z-bar` 700 · `--z-sheet` 800 · `--z-chooser` 1100 · `--z-dialog` 1200 · `--z-toast` 1300 | strict ladder; no two surfaces share a layer (fixes the A-11 class of bug) |

Safe areas: `padding-bottom: env(safe-area-inset-bottom)` on the bar,
`padding-top: env(safe-area-inset-top)` on the status strip.

### 5.4 Elevation

Two levels only.
- Sheets / dialogs: `box-shadow: 0 -8px 32px rgba(0,0,0,.55)` (up for sheets, `0 8px 32px` for
  centred dialogs), `border:1px solid var(--edge)`.
- Chooser / toast: `box-shadow: 0 6px 20px rgba(0,0,0,.5)`.

---

## 6. Component inventory

Each component lists its size, its states, and its accessible name pattern. "Target" is the
measured minimum touch box.

### 6.1 Bottom bar item
`<button class="baritem" aria-current="true|false">`
Target **48×48** inside a 93.5px cell · icon 24px + 11.5px caps label · states: rest / active
(`aria-current`, 3px top border `--focus`, label `--fg`) / pressed (`--panel-2`) / busy (icon
replaced by a 16px spinner, label unchanged, `aria-busy="true"`).
Name = visible label. "Near me", "Arrivals", "Plan", "More" (A-19/2.5.3 compliance by
construction).

### 6.2 Train row (arrivals list, chooser, detail lists)
`<button class="row">` — a real button, not a div (A-13).
Height **48px** · grid `4px linecolour | 1fr destination | auto chips | 56px ETA`.
`font-variant-numeric: tabular-nums` on the ETA so times align down the column.
Accessible name: `"Manukau, AMP 1114, 7 minutes, pee train, 2 reports"` — built from the same
fields `renderList()` already has at index.html:1366-1377.
States: rest / hover `--panel-2` / focus-visible ring / late (`--warn` ETA) / out-of-service
(`opacity:.72` — raised from today's `.62`, which pushed `--fg-2` below 4.5:1 on panel).

### 6.3 Certainty + severity chips
`<span class="chip chip--sev3">` · height **24px** · 8px radius · `--t-chip` 12px 700 ·
padding `3px 8px`.
Severity carries **two channels** (A-14): colour **and** pips.

| Variant | Visible text | Pips | Where it appears |
|---|---|---|---|
| `chip--sev1` | `Faint pee smell` | `●○○` | popup, list (expanded), log |
| `chip--sev2` | `Suspected pee train` | `●●○` | ” |
| `chip--sev3` | `PEE TRAIN` | `●●●` | ” |
| `chip--reports` | `2 reports` | — | list, popup |
| `chip--praise` | `Praised` | — | never with a warning glyph |
| `chip--people` | `☠ Behaviour reported` | — | clears at the last stop |
| `chip--unknown` | `Set unknown` | — | board + planner, identical wording |
| `chip--inferred` | `Inferred` + `?` | — | 6-car couple |
| `chip--resolved` | `Resolved` | — | log rows |
| `chip--oos` | `Not in service` | — | OOS units |

In the arrivals list, where width is tight, the chip collapses to **glyph + number** at a
minimum **24×24** box (today: a 9.9×13px glyph, A-06) and always keeps a non-`title`
accessible name.

### 6.4 The certainty vocabulary (the honesty system, formalised)

Five levels. One treatment each. Used identically in map, list, board, planner and popup —
that consistency is what makes 3.2.4 pass and what makes the honesty principle *legible*
rather than merely true.

| Level | Meaning | Treatment | Canonical copy |
|---|---|---|---|
| **Measured** | Came straight from AT's feed | plain text, no chip | `platform 1`, `3 min late` |
| **Derived** | Computed from AT data by a documented rule | plain text + source on tap | `arrives 5m` |
| **Inferred** | Our inference, could be wrong | `chip--inferred` + `?` button | `6-car service · front half (cars 1–3) · with set 674` / on tap: *"We work this out from two sets reporting GPS one set-length apart. AT doesn't publish consists, so we could be wrong."* |
| **Unknown** | AT has not told us yet | `chip--unknown`, neutral grey, never red | `Set unknown` / on tap: *"AT hasn't rostered a train to this service yet. We'll show the set number once it's running."* |
| **Absent** | Cannot exist | stated plainly, no placeholder | `Not in service — no destination or ETA` |

Hard rules for implementers:
- **Unknown is never styled as an error.** `--fg-2` on `--panel-2`, never `--dead`.
- **Never render a placeholder number.** `age_s` absent means the words "loading…", never a
  digit (`DECISIONS.md` 2026-11-19 — the `?? 999` lesson).
- **Inference always carries its `?`,** and the `?` is a 24px button with real text behind it,
  never a `title` attribute (A-20).
- **Praise is never a fault** — `chip--praise`, no triangle, excluded from "most reported"
  (preserves the `n_neg` / `hasFault()` architecture at index.html:804-807).

### 6.5 Sheet
`<div class="sheet" role="dialog" aria-modal="false" aria-labelledby="…">`
`aria-modal="false"` deliberately: a sheet leaves the map live behind it, and claiming modality
to AT when the map is still interactive would be a lie in the accessibility tree.
Grab handle 36×4px `--edge` (decorative, `aria-hidden`), title `--t-h1`, close button 48×48 in
the top-right, body scrolls internally with `overscroll-behavior: contain`,
`max-height:70vh`, `padding-bottom: calc(var(--bar-h) + env(safe-area-inset-bottom))`.

### 6.6 Dialog (report form only)
`role="dialog" aria-modal="true"`, backdrop `rgba(5,8,12,.72)` (today `.62` — raised for
sunlight), width `min(420px, 100vw - 32px)`, `max-height:88vh`, internal scroll, focus trapped.

### 6.7 Carriage picker (the one widget to preserve almost as-is)
3-car: three **48×56** cells + "Not sure" cell. 6-car: seven cells at 48px wide, 6px gaps =
372px, fits 374px of available width. Glyphs `▲ / ■ / ▼` for front/middle/rear retained —
they communicate orientation, which is the whole point. "Not sure" stays first-class with equal
visual weight.
Selected state: `border-color: var(--focus)` + `inset 0 0 0 1px var(--focus)` +
`background:#1d2836` — two channels, and it fixes the undefined `var(--red)` bug (A-18).
`role="radiogroup"` and `aria-checked` already correct at index.html:426/438 — keep.

### 6.8 Chooser
See §4.2. `role="menu"`, 48px rows, max 5 then scroll, `--panel-2` surface, `--z-chooser`.

### 6.9 Status strip
Non-interactive except the `+51 not in service` affordance.
`aria-live="polite" aria-atomic="true"`, announcing only on **state-class change**
(fresh → stale → dead), not every 10s tick (A-21).
Dot 10px (`--live`/`--stale`/`--dead`) **plus a word** — "live" / "delayed update" /
"not updating" — because a coloured dot alone fails 1.4.1.

### 6.10 Toast
Already correct (`role="status" aria-live="polite"`, index.html:337). Reposition to
`bottom: calc(var(--bar-h) + 12px)` so it never sits under the bar.

---

## 7. Motion and feedback

| Event | Motion | Duration / easing |
|---|---|---|
| Sheet open | translateY(100%) → 0 | 220ms `cubic-bezier(.2,.8,.2,1)` |
| Sheet close | reverse | 160ms `cubic-bezier(.4,0,1,1)` |
| Chooser appear | opacity 0→1, scale .96→1 | 120ms ease-out |
| Chip/state change | colour only | 120ms linear |
| Train marker move | `transform` | **1.6s linear — unchanged** (index.html:64); it matches the 10s poll and reads as travel |
| Toast | opacity | 180ms (unchanged) |
| Focus ring | none | instant, always |
| Live dot | 2s pulse | **only** when the feed is fresh |

Feedback rules:
- Every action acknowledges within **100ms**, even if the result takes longer (press state, then
  spinner, then result).
- A failed fetch keeps the last known data on screen and says so — never blank the map.
  (The existing `reloadLogs()` catch at index.html:984 already does this; generalise it.)
- Nothing moves on screen more than once per poll.

**Reduced motion is mandatory, not optional** (A-23):

```css
@media (prefers-reduced-motion: reduce){
  *, *::before, *::after{
    animation-duration:.01ms !important; animation-iteration-count:1 !important;
    transition-duration:.01ms !important; scroll-behavior:auto !important;
  }
  .train{ transition:none; }     /* markers jump — honest about a 10s poll anyway */
  .livedot{ animation:none; }    /* the colour still carries the meaning */
}
```

---

## 8. Empty, loading, error and unknown states

Every one of these is a designed state with concrete copy. None is a blank.

| Surface | State | What is shown |
|---|---|---|
| Status strip | warming | `◐ Loading…` / `fetching live positions from Auckland Transport` — **no age number** |
| Status strip | fresh | `● 18 in service · +51 not in service` / `updated 7s ago · live GPS positions` |
| Status strip | stale 45–150s | `● 18 in service` / `last update 1 min ago — AT may be slow` |
| Status strip | dead >150s | `● Not updating` / `last update 4 min ago. Positions shown are old.` |
| Status strip | fetch failed | `● Can't reach the live feed` / `Showing the last positions we got, from 2 min ago.` |
| Map | no trains at all | centred card: `No trains are reporting right now.` + `This is normal between about 01:00 and 05:00.` |
| Arrivals | in-service empty | `Nothing in service right now.` + `51 units are stabled — show them` |
| Arrivals | loading | 5 skeleton rows at 48px, `aria-busy="true"` |
| Board | loading | `Loading departures…` (existing) |
| Board | empty | `No departures from Newmarket in the next 2 hours.` |
| Board | error | `Couldn't load departures.` + `[ Try again ]` (a button, not a dead string) |
| Board | row unknown set | `chip--unknown` "Set unknown", expandable to the canonical sentence |
| Planner | same station | `Those are the same station.` (existing) |
| Planner | no direct trains | `No direct trains in the next 3 hours. You may need to change — try planning each leg.` (existing, kept) |
| Planner | all rows unknown | the one-line explanation from §4.5 above the list |
| Planner | API error | `The planner isn't answering. The live map above still works.` |
| Detail | no reports | `No reports yet.` + `[ Report this train ]` |
| Detail | OOS unit | `Not in service` chip · `at Wiri · 120 m` · `stationary — stabled` · **no ETA, no destination, no platform** |
| Report form | sending | button → `Sending…`, disabled, `aria-busy` |
| Report form | sent | `Thanks — report saved.` then auto-close after 900ms (existing) |
| Report form | rejected | the server's message verbatim, in `--dead`, `role="alert"` |
| GPS | denied | `Location permission declined — pick a station.` (existing; no nagging) |
| GPS | far away | `Closest station is Pukekohe, but it's 14.2 km away — you may not be near the train network.` (existing) |

---

## 9. Accessibility behaviour

### 9.1 Focus order
Phone, at rest:
```
1 skip link ("Skip to arrivals list")   ← new, visible on focus
2 status strip (tabindex=-1, programmatic target only)
3 map container (single stop; arrow keys pan, +/− zoom)
4 bar: Near me → Arrivals → Plan → More
```
Train markers leave the sequential tab order entirely (A-09); the arrivals list is their
keyboard equivalent, which is also what WCAG 2.5.8's *Equivalent* exception contemplates.

Sheet open: focus moves to the sheet title (`tabindex="-1"`), then title → close → body
controls → bar. The bar stays reachable (the sheet is non-modal by design).
Dialog open: focus to the dialog title; Tab cycles **only** within the dialog; Escape closes;
focus returns to the control that opened it.
Chooser open: focus to the first row; ↑/↓ cycle; Escape dismisses without acting.

### 9.2 Keyboard operation
| Key | Context | Action |
|---|---|---|
| `Tab` / `Shift+Tab` | global | move focus; trapped inside the report dialog only |
| `Enter` / `Space` | any button or row | activate |
| `Escape` | sheet / dialog / chooser | close the topmost one, in that order |
| `↑ ↓` | chooser, list | move between rows |
| `← ↑ → ↓` | map focused | pan (Leaflet default) |
| `+` `−` | map focused | zoom (Leaflet default) |
| `1`–`4` | map focused | toggle line visibility — **optional**, and only when no field has focus (WCAG 2.1.4 Character Key Shortcuts) |

Everything reachable by pointer is reachable by keyboard. No dragging is required for any
function: the swipe-to-dismiss on sheets is an addition to the ✕ button, and the map pans by
arrow keys as well as by drag (WCAG 2.5.7).

### 9.3 Screen-reader labelling
- `<html lang="en-nz">` — already correct (index.html:2).
- One `<h1>` outside any button (A-22); sheets/dialogs use `<h2>`; sections `<h3>`.
- Landmarks: `<main>` wrapping the map, `<nav aria-label="Main actions">` for the bar.
- Markers: `<svg role="img" aria-label="…">` with a name built from the live data, e.g.
  *"Eastern/Western service to Manukau, unit AMP 1114, arriving in 5 minutes, 2 reports"*, or
  *"Unit AMP 0671, not in service, stabled at Wiri"*.
- Rows: name assembled from the row's own content, never "row 4".
- Icon-only controls carry a visually-hidden label; the visible label is always a prefix of the
  accessible name (2.5.3).
- `aria-live="polite"` on the status strip, `role="alert"` on `#err`, existing `role="status"` on
  `#toast` kept.
- Decorative glyphs (chevrons, grab handles, line swatches) get `aria-hidden="true"`.

```css
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
         clip:rect(0 0 0 0);white-space:nowrap;border:0}
```

### 9.4 Touch targets
| Class | Size | Examples |
|---|---|---|
| Primary | **48×48** min | bar items, sheet rows, chooser rows, close buttons, carriage cells |
| Commit | **52–56** tall | Send report, Find trains |
| Secondary | **44×44** min | vote buttons, Cancel, `?` inference buttons |
| Inline chips | **24×24** min, ≥8px apart | severity chips in dense rows |
| Map markers | 32×32 (unchanged) | resolved by the chooser, not by growing them |
| Station dots | 7px visible (unchanged) | ” |

The 24px floor from WCAG 2.5.8 is never breached. Map markers and station dots stay small
deliberately — 48 fat dots would bury the map, which is exactly why the chooser exists.
Minimum gap between any two adjacent targets: **8px**.

### 9.5 Reduced motion, contrast, zoom
- `prefers-reduced-motion` block as §7.
- All text ≥ 4.5:1; primary text ≥ 7:1 for sunlight; all component boundaries ≥ 3:1.
- Works at 200% browser zoom and at 320×256 without two-dimensional scrolling of the chrome
  (A-12): below 400px width the status strip collapses to one line and the bar drops its labels,
  keeping 48px icon targets.
- `prefers-contrast: more` → `--edge:#8A99AD`, chip borders at 1px, badge glyph strokes +0.4px.

---

## 10. What is deliberately NOT changing

Repeated here from AUDIT.md because it is the part an implementer is most likely to "improve".

1. **`.stname { pointer-events:none }`** — labels are wider than the station gaps; clickable
   labels demonstrably swallowed neighbours' taps.
2. **The capture-phase nearest-station handler** — extended with a chooser, never replaced by
   per-marker hit circles, which fire on whatever is drawn last rather than what is nearest.
3. **No "skip if a train is near" guard** — 58% of trains sit within 20px of a station.
4. **Label hide threshold `zoom < 11`** — the default view *is* zoom 11.
5. **`logHtml()` as a pure string builder** — Leaflet's `setContent()` is not synchronous.
6. **Praise/fault separation (`n_neg`, `hasFault`)** — six surfaces depend on it.
7. **Closed `BEHAVIOURS` list with no free text for `ruffians`**, stripped server-side.
8. **Out-of-service units carry no ETA/destination/platform.**
9. **The four AT line colours** — measured, compliant, and not ours.
10. **The planner's "not assigned yet" sentence** — copied outward, never rewritten.
11. **Anonymity** — no accounts, no identity, no new persistent identifiers.
12. **Single-file, no-build architecture** — every token here is a CSS custom property and every
    behaviour is vanilla JS on top of the functions already in the file.
