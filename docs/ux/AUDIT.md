# AUDIT — current interface, itemised

**Subject** `/opt/data/projects/akl-train-map/web/index.html`
**Revision audited** 1677 lines, 82,320 bytes, `sha256 5bb23d5f98107983…`, mtime 2026-11-19 23:53 NZDT.
Line numbers below are verified against that revision. The file is under active edit; if a
citation drifts, grep the quoted selector rather than trusting the number.

**How these findings were produced** — the live production app was rendered in headless
Chromium at 390×844 (`device_scale_factor=2`, mobile, touch) and 1280×900, and measured:
bounding boxes of every control, real tab order, pixel distances between all 48 station dots at
zoom 11, distance from every live train marker to its nearest station, all stylesheet contrast
pairs computed arithmetically, every painted font-size, and `document.styleSheets` scanned for
`:focus` and `prefers-reduced-motion` rules. Every **Measured** line is an observed value.

**Severity** = user harm × frequency, not implementation effort.
Critical = blocks a core task or asserts something false · High = core task slow/error-prone, or
an AA failure on a main path · Medium = friction, or AA failure on a secondary path · Low = polish.

**Summary**

| | Count | IDs |
|---|---|---|
| Critical | 3 | A-01, A-04, A-05 |
| High | 9 | A-02, A-03, A-06, A-07, A-08, A-09, A-10, A-11, A-12 |
| Medium | 8 | A-13 … A-20 |
| Low | 4 | A-21 … A-24 |

Two things the audit explicitly clears: the four AT line colours all pass 1.4.11 against the
dark background (3.76 / 9.68 / 7.48 / 6.43 : 1), and the honesty copy in the planner and the
out-of-service popups is correct and should be copied, not changed.

---

## Critical

### A-01 Map taps are non-deterministic at exactly the moment they matter
**Severity** Critical
**Where** `stationNear()` index.html:498-507; map click handler :509-515; capture-phase handler
:532-544; `L.circleMarker(... interactive:false)` :582; `preferCanvas:true` :455.
**Measured** At the default zoom 11 on a 390×844 phone: the 48 stations produce **59 pairs under
40 px apart** and **26 of the 48 stations have at least one neighbour closer than 24 px**.
Tightest pairs: Waitemata↔Te Waihorotiu **12.0 px**, Parnell↔The Strand **12.4 px**,
Kingsland↔Morningside **13.9 px**, Newmarket↔Grafton **15.3 px**, Baldwin Ave↔Mt Albert 15.8 px,
Maungawhau↔Karanga-a-Hape 17.0 px. Separately, **30 of 52 live train markers (58%) were within
20 px of a station dot**, 9 of them within 1 px (AMP 484 at Waitemata 0 px; AMP 932/945 at The
Strand 0 px). The code uses a 20 px nearest-wins catch radius (:540) and a 22 px radius on the
map-level handler (:513).
**User harm** A rider aiming at The Strand opens Parnell's departure board, or aims at a train
and opens a station. Nothing on screen says a choice was made, so the error is silent — the user
reads a board for the wrong station and may miss a train. This is the user's own top complaint.
**Principle** WCAG 2.5.8 Target Size (Minimum), AA — the *Spacing* exception requires that a
24 px-diameter circle centred on each undersized target not intersect another target's circle;
with 12 px separations it is mathematically impossible to satisfy, so the exception cannot be
claimed and the targets must be made separable another way. Also Nielsen #1 (visibility of
system status) and Norman's gulf of evaluation: the system resolves an ambiguity and never
reports that it did.
**Fix** Keep the nearest-wins proximity test — it is the right algorithm and the comments at
:517-531 record why a per-marker hit circle is not. Add a **disambiguation chooser**: in
`stationNear()`, collect *all* candidates within the radius instead of only the best, and include
train markers within the same radius. If exactly one candidate → act immediately (today's
behaviour, no regression). If two or more → render a small anchored list at the tap point,
`role="menu"`, one 48 px-tall row per candidate, each labelled with its type and name
("Station — The Strand", "Train — AMP 932 to Manukau"), ordered nearest-first, dismissed by
Escape/backdrop tap. See PROPOSAL.md §4.2 for the full spec. This converts a silent wrong answer
into an explicit choice, which is the honesty principle applied to interaction.

### A-02 is High (below). A-03 is High (below).

### A-04 The three overlays are not dialogs, and focus never enters them
**Severity** Critical (accessibility); High for everyone else
**Where** `#fbwrap`/`#fb` :106-109 & :384-448; `#stwrap`/`#st` :137-140 & :371-380;
`#jpwrap`/`#jp` :143-146 & :343-366. Open/close logic: `openForm()` :1093, `closeForm()` :1120,
`openBoard()` :1419, `closeBoard()` :1467, `openPlanner()` :1555, `closePlanner()` :1560.
**Measured** With the journey planner open on the phone, `document.activeElement` was
**`#sheetbtn`** — a control *behind* the overlay. Pressing Tab 14 times produced this sequence:
`planbtn (OUTSIDE), list (OUTSIDE), jpfrom, jpto, jpgps, jpgo, jpclose, BODY (OUTSIDE), map
(OUTSIDE), DIV, DIV, DIV, DIV, DIV` — focus starts outside the modal, leaves it after 5 stops,
and lands on map markers underneath. `#jp` has `role=null`, `aria-modal=null`; `#jpwrap` has
`role=null`. Same for `#fb` and `#st`.
**User harm** A screen-reader user activating "Plan" is given no announcement that anything
opened and is left on a button behind an invisible-to-them overlay. A keyboard user tabs
straight out of the dialog into the map. `#fb` is a form with a Send button — a user can be
focused on controls they cannot see.
**Principle** WCAG 4.1.2 Name, Role, Value (A) — no dialog role; 2.4.3 Focus Order (A) — order
does not preserve meaning; 2.4.11 Focus Not Obscured (Minimum, AA) — focus lands on components
entirely covered by the overlay backdrop.
**Fix** For each of the three: add `role="dialog" aria-modal="true"` to the inner panel and
`aria-labelledby` pointing at its existing `<h2>` (`#jp`→add `id="jptitle"` to the h2 at :345;
`#st`→`#sttitle` already exists at :373; `#fb`→add `id="fbtitle"` to the h2 at :386). On open:
store `document.activeElement` in a module-level `LAST_FOCUS`, then focus the dialog's first
heading (`tabindex="-1"`) — not the first input, so the title is announced first. Trap Tab/Shift+Tab
within the dialog's focusable set. On close: `LAST_FOCUS.focus()`. Note `openForm()` currently
focuses `#fbnote` (:1117) which is the right instinct but focuses an input before announcing the
dialog, and that input is `hidden` for people-category reports.

### A-05 There is not one `:focus` rule in the stylesheet
**Severity** Critical (accessibility)
**Where** The whole `<style>` block :12-316.
**Measured** `document.styleSheets` scanned at runtime: **0 rules matching `:focus`** (the
Leaflet CDN sheet contributes a couple for its own controls, which are hidden on phones at :301).
Custom controls that therefore have no visible focus state: `#hudbar` (a `div[role=button]`,
:321), every `.btn`, every `.vote`, `#lenpick .len`, `#carpick .car`, `.loglink`, `.fbopen`,
`#sheetbtn`, `#planbtn`, `#worst .w` (a `div` with an `onclick` at :1086 and no `tabindex` at all).
**User harm** A keyboard user cannot tell where they are. On `#0d1117` the browser default ring
is nearly invisible, and several elements are divs that never receive a default ring.
**Principle** WCAG 2.4.7 Focus Visible (AA). Spec'd against 2.4.13 Focus Appearance (AAA) for
the ring geometry because the default is unusable on this background.
**Fix** One global rule plus a reset:
```css
:focus-visible { outline: 3px solid #7FB3FF; outline-offset: 2px; border-radius: inherit; }
.leaflet-container :focus-visible { outline-offset: 0; }
```
`#7FB3FF` against `--panel #161c24` is 7.99:1 and against `--bg` 8.83:1, clearing 2.4.13's 3:1
between focused and unfocused states with margin. Add `tabindex="0"` + `role="button"` to
`#worst .w` rows, or convert them to `<button>` (preferred — see A-13).

---

## High

### A-02 Vote buttons fail target size on both size and spacing
**Severity** High
**Where** `.vote` :269-271; `.lv` :268 (`gap:4px`); rendered by `logHtml()` :977-1013.
**Measured** Rendered through the app's own `logHtml()` with realistic report data:
**68.7 × 21.2 px** and **63.6 × 21.2 px**, with a **4.0 px gap** between the two.
**User harm** The confirm/clear vote is the mechanism that stops the map becoming a permanent
accusation (`DECISIONS.md` 2026-11-19). It sits at 21 px tall inside a popup that re-renders
every 10 s, so a mis-tap on a moving train is likely and lands on the *opposite* vote 4 px away.
**Principle** WCAG 2.5.8 Target Size (Minimum), AA. Fails the 24×24 size rule (21.2 px high) and
cannot claim the Spacing exception (4 px gap; needs the 24 px circles not to intersect).
**Fix** `.vote { min-height:44px; padding:10px 14px; font-size:13px; }` and `.lv { gap:10px;
flex-wrap:wrap; }`. At 390 px the two buttons plus the "Still smell?" label will wrap to two
lines — acceptable, and better than a mis-vote. Keep `aria-label="Still a problem"` /
`"Fine now"` (:1006, :1008) but add the visible text to match them (A-19).

### A-03 Primary pills and dialog buttons are under the AA floor
**Severity** High
**Where** `#planbtn` :156-161, `#sheetbtn` :289-293 (`padding:7px 15px; font-size:12.5px`);
`.btn` :100-102 (`padding:4px 9px; font-size:11.5px`); `.popacts` :259; `#fb .acts` :119.
**Measured** Phone 390×844: `#sheetbtn` **79.1 × 34.1**, `#planbtn` **78.6 × 34.1**. Inside the
planner, the three `.btn` elements measured **130 × 24**, **80 × 24**, **51 × 24**. In the report
form, Cancel **59 × 24** and Send report **88 × 24**. Popup action buttons **101 × 22** and
**78 × 22**. Leaflet's own popup close button is 24 × 24 — the only one that passes, and only just.
**User harm** The two most-used controls on the phone (`#sheetbtn`, `#planbtn`) are 34 px tall in
a one-handed, windy, rushed context. **Send report at 24 px is the terminal action of a 90-second
task** — a mis-tap hits Cancel 8 px away and discards everything typed.
**Principle** WCAG 2.5.8 (AA) — 22 px and 24 px heights are at or below the floor with no
applicable exception. Against the §5 context analysis in RESEARCH.md, 2.5.5 (AAA, 44×44) is the
appropriate bar for these.
**Fix** `.btn { min-height:44px; padding:11px 16px; font-size:14px; }`; pills to
`min-height:52px; padding:0 20px; font-size:15px` (see PROPOSAL.md §5 for the full bottom-bar
redesign that replaces them). Increase `#fb .acts { gap:12px }` and put Send on its own row at
full width on phones, Cancel beneath it as a text button — destructive actions should not sit
within a thumb-width of the commit action.

### A-06 The warning glyph is 10 px wide and its meaning exists only in `title=`
**Severity** High
**Where** `.wmini` :93-96; `warnMini()` :846-858; used in `renderList()` :1370 and :1375.
**Measured** Rendered `.wmini` box: **9.9 × 13.0 px**. `tabIndex` = -1 (not focusable). The
severity and the words ("PEE TRAIN", "2 reports") live in `title=` and `aria-label=` only
(:857). The skull glyph is the same (:851-852).
**User harm** P2's entire reason for using this app is to spot a flagged train in the arrivals
list. `title=` does not render on touch — there is no hover on a phone — so on the primary
device the glyph is an unlabelled 10 px mark. Severity is encoded **only** in colour
(`.wmini.p1/.p2/.p3`, :94-96).
**Principle** WCAG 1.4.1 Use of Color (A) — colour is the sole means of conveying severity;
1.4.11 Non-text Contrast (AA) for a 10 px graphical object carrying meaning; Nielsen #6
(recognition rather than recall).
**Fix** Replace the bare glyph with a compact labelled chip: glyph **plus** a short word, e.g.
`⚠ 2` / `☠ live`, minimum 16 px glyph, `min-height:24px`, and a visible severity word in the
expanded row. See PROPOSAL.md §6.3 for the badge system — the key move is that severity gets a
*shape and a word*, not only a hue.

### A-07 The `--line` border colour is invisible: 1.36:1
**Severity** High
**Where** `:root --line:#2b3440` :13. Used as the border of: `.panel` :19-21, every `#fb`
input/select/textarea :113-116, `#jp select` :150-152, `.btn` :101, `.vote` :269,
`#lenpick .len` :176, `#carpick .car` :188, and as `border-top` on `.legend` :45, `.note` :53,
`#worst` :122, `.lr` :264, `.jd` :162, `.pop .reports` :97.
**Measured** `#2b3440` on `--panel #161c24` = **1.36:1**. On `--bg #0d1117` = **1.50:1**.
Required: 3:1.
**User harm** In sunlight the form fields have no visible boundary — a rider cannot see where the
Notes box is or that `#fbref` is an input at all. Section dividers vanish, so the panel reads as
one undifferentiated block.
**Principle** WCAG 1.4.11 Non-text Contrast (AA): "visual information required to identify user
interface components and states" needs 3:1.
**Fix** Split the token. Keep `--line:#2b3440`, renamed `--rule`, for purely decorative hairlines,
and introduce **`--edge:#626D7E`** for every *component boundary*. Measured: **3.27:1** on
`--panel`, **3.61:1** on `--bg`, **3.53:1** on the field colour `#0f141b` — clears 3:1 against all
three surfaces it is drawn on. Apply `--edge` to all inputs, selects, textareas, buttons,
`.vote`, `#lenpick .len`, `#carpick .car` and the panel border; keep `--rule` only for
`border-top` dividers inside a panel, which are decorative grouping and exempt from 1.4.11.

### A-08 `.btn.primary` fails text contrast
**Severity** High
**Where** `.btn.primary { background:#2f6feb }` :104, text inherits `--fg:#e8edf2` from `.btn` :100.
**Measured** `#e8edf2` on `#2f6feb` = **3.88:1**. Required 4.5:1 (the text is 11.5 px, nowhere
near the large-text threshold).
**User harm** "Send report" and "Find trains" — the two commit actions in the app — are the least
legible text on the page, outdoors, at 11.5 px.
**Principle** WCAG 1.4.3 Contrast (Minimum), AA.
**Fix** Darken the fill rather than lightening text that is already near-white:
`--accent:#1F5BD7` with `#FFFFFF` text = **5.94:1**. Keep `#2F6FEB` as `--accent-hi` for hover
only (white on it is 4.57:1, still AA, and hover is a desktop-only transient state); the resting
state must be `#1F5BD7`. Note `--accent` itself is 2.88:1 against `--panel`, so a primary button
also needs a 1px `--edge` border to satisfy 1.4.11 for its own boundary.

### A-09 52 train markers are individually in the tab order, unlabelled
**Severity** High (accessibility)
**Where** `L.divIcon` markers built in `icon()` :666-690, added :1340; Leaflet gives
`.leaflet-marker-icon` `tabindex="0"` by default.
**Measured** 81 focusable elements on the phone at rest; **52 of them are train markers**, each
32 × 32 px with an **empty accessible name** (`label: ""`). 53 `<svg>` elements have neither
`aria-label` nor a `<title>` child.
**User harm** A keyboard or screen-reader user must Tab through 52 nameless "buttons" to reach
anything else, and each announces as nothing. The map is effectively a tab trap by volume.
**Principle** WCAG 4.1.2 Name, Role, Value (A) — no accessible name; 2.4.3 Focus Order (A) —
52 unlabelled stops before any real control; 2.1.1 Keyboard (A) — the markers are reachable but
not meaningfully operable.
**Fix** Two parts. (1) Give every marker a real name:
`L.divIcon({..., html: '<svg role="img" aria-label="Manukau service, AMP 1114, 5 minutes, 2 reports">…'})`
built from the same fields `popupFor()` (:745) already has. (2) Remove the markers from the
sequential tab order (`keyboard:false` on the marker options, or `tabindex="-1"` on the icon) and
provide the **equivalent** keyboard path that WCAG 2.5.8's *Equivalent* exception also leans on:
the arrivals list, which already lists every train and already flies to it on click (:1388-1392).
Make the arrivals list rows real `<button>`s (A-13) and the map becomes operable without
52 phantom stops. This is also the honest architecture — the list is the accessible twin of the map.

### A-10 All persistent controls sit at the top of the screen; nothing is thumb-reachable except two 34 px pills
**Severity** High
**Where** `#hud` :22, phone override :297-298 (`top:8px; left:8px; right:8px`); `#sheetbtn`
/`#planbtn` :310-312.
**Measured** Phone: `#hud` collapsed occupies **374 × 89.1 px at y=8 — 10.1% of the screen**;
expanded **466.2 px tall = 53.0%**. The only bottom-anchored controls are the two pills at
**y = 797.9**, together **157.7 px of the 390 px width** — 40% of the bottom bar is used, 60% is
empty map. The collapse chevron `#chev` is **12 × 6 px** at x=361 — the top-right corner, the
hardest point on the screen for a right thumb (and the hit area is the whole `#hudbar` row at
362 × 29.8, which is the right call but still 29.8 px tall and at the top).
**User harm** One-handed operation requires a hand-shuffle to reach the status panel, legend and
"most reported" list. The expanded HUD then covers over half the map.
**Principle** Not a WCAG failure — a context failure against RESEARCH.md §5 (one-handed thumb
arc ≈ bottom 55%) and Nielsen #7 (flexibility and efficiency of use).
**Fix** Move the persistent controls to a **bottom action bar** (PROPOSAL.md §5): a 64 px-tall
bar with three 48 px targets — *Near me*, *Arrivals*, *Plan* — plus a status strip. The HUD's
content (legend, counts, most-reported) becomes a bottom sheet pulled from that bar. Keep the
title/status line at the top where it is glanceable without being interactive.

### A-11 The arrivals sheet overlaps the pills it was opened from
**Severity** High
**Where** `#list` phone rules :304-306 (`bottom:8px; max-height:38vh`), `#sheetbtn`/`#planbtn`
:310-312 (`bottom:12px`), both `z-index:650` vs `#list` inheriting `.panel`'s `z-index:600` :19.
**Measured** With the sheet open on the phone: `#list` spans y **497.3 → 836.0**; `#sheetbtn` and
`#planbtn` sit at y **797.9 → 832.0**. The sheet's last **34 px of rows are underneath both
pills**, and because the pills have the higher z-index they win. Confirmed visually: the rows
"AMP 131 / stabled · Puhinui" render behind the "Hide arrivals" and "Plan" pills.
**User harm** The final rows of a 52-row list are unreachable and partially unreadable. A tap
there hits a pill instead — another silent wrong action.
**Principle** WCAG 2.4.11 Focus Not Obscured (Minimum, AA) when those rows are focusable
(they will be, per A-13); Nielsen #1.
**Fix** In the proposed bottom-bar layout the sheet is anchored *above* the bar by construction:
`#list { bottom: calc(var(--bar-h) + 8px) }` with `--bar-h:64px`, and the sheet gets
`padding-bottom: env(safe-area-inset-bottom)`. Interim one-line fix if the bar is deferred:
`#list { bottom: 56px; max-height: calc(100vh - 56px - 110px); }`.

### A-12 Reflow at 320 px: the HUD takes a third of the viewport and the body cannot scroll
**Severity** High
**Where** `html,body { overflow:hidden }` :15; `#hud` :297-298; `#hudbody { max-height:52vh }` :302.
**Measured** At 320 × 256 (the 400 %-zoom equivalent 1.4.10 tests at): `#hud` renders
**304 × 89 px = 33 % of the viewport** while collapsed; `document.body` overflow is `hidden` and
`canScrollBody` is **false**. Expanding the HUD at that size covers the map entirely with no way
to scroll to what is underneath.
**User harm** A low-vision user at 400 % zoom gets a third of their screen consumed by a header
they cannot scroll away, and the map — the content — is what gets squeezed.
**Principle** WCAG 1.4.10 Reflow (AA). The map itself is covered by the spec's two-dimensional-
layout exception; **the chrome is not**.
**Fix** Below 400 px viewport width, collapse the HUD to a single 40 px status strip (status dot,
count, age) with the title truncated, and move everything else behind the bottom bar's sheet,
which scrolls internally. Add `@media (max-height:420px)` handling so the sheet's `max-height`
is `60vh` rather than a fixed px.

---

## Medium

### A-13 Clickable rows are `div`s with `onclick` — invisible to keyboard and AT
**Severity** Medium (High for P5)
**Where** `#list .row` built in `renderList()` :1366-1377, handler attached :1388; `#worst .w`
built :1079-1085, handler :1086; `.brow` departure rows built in `loadBoard()` :1455-1461.
**Measured** `.brow` rows measured 30.4 px tall and `rowClickable` was **false** — the board rows
are not interactive at all. `#list .row` rows are 21.9–23.4 px tall on desktop. None of the three
has `tabindex`, `role`, or a keyboard handler.
**User harm** Keyboard users cannot reach the arrivals list, the "most reported units" list, or
anything on a departure board. The 21.9 px row height also fails 2.5.8 on touch.
**Principle** WCAG 2.1.1 Keyboard (A), 4.1.2 Name, Role, Value (A), 2.5.8 Target Size (AA).
**Fix** Render each as `<button type="button" class="row">` with `min-height:48px` and a real
accessible name assembled from the row's own data ("Manukau, AMP 1114, 7 minutes, 2 reports").
The existing delegated-click pattern (:907) keeps working unchanged. Departure-board rows should
become buttons too, opening the train's detail — today a board is a dead end (A-16).

### A-14 Severity is carried by hue alone across five surfaces
**Severity** Medium
**Where** `.flag.s1/.s2/.s3` :74-76; `.pee.p1/.p2/.p3` :87-89; `.wmini.*` :94-96;
`.jrisk.p1/.p2/.p3` :166-168; `WARN_FILL` :620.
**Measured** The three severity levels differ only in background/foreground hue. All the
text/background pairs pass 1.4.3 individually (7.14 / 5.85 / 5.99 : 1 for `.flag`), so this is
not a contrast failure — it is a *channel* failure.
**User harm** A red-green or low-saturation-perception user, or anyone in direct sun where hue
differentiation collapses, cannot tell "faint pee smell" from "PEE TRAIN" where the label is
abbreviated or absent (notably `.wmini`, A-06).
**Principle** WCAG 1.4.1 Use of Color (A).
**Fix** Add a second channel per level that is already in the domain: **1/2/3 severity numerals
or filled pips** rendered inside every severity chip (`●○○`, `●●○`, `●●●`), plus the existing
colour. The `.pee` verdict chips already carry distinct words, which is correct; the fix is for
`.flag`, `.wmini` and `WARN_FILL`.

### A-15 Ten-pixel type, on 48 station labels and 206 other elements
**Severity** Medium
**Where** `.stname { font-size:10px }` :217; `.lh .when { font-size:10px }` :266;
`.inf { font-size:9px }` :255; `#carpick .cs { font-size:9.5px }` :200; `.note { font-size:10.5px }`
:52; `.flag/.good/.pee/.tag/.stale/.oostag` all `10.5px` :72, :79, :84, :69, :274, :277.
**Measured** Painted font sizes on the phone, by element count: **10 px → 48 elements**,
**10.5 px → 206 elements**, 11.5 px → 2, 12 px → 2, 12.5 px → 4, 14 px → 4, 15 px → 1, 17 px → 1.
So the two smallest sizes account for 254 of ~268 sized elements. On desktop: 10 px → 48,
10.5 px → 283, 11 px → 55.
**User harm** Sunlight, motion and presbyopia all attack small type first. The station labels —
the thing that makes the map navigable — are the smallest text on screen.
**Principle** Not a direct WCAG failure (1.4.4 Resize Text is satisfiable via zoom), but a direct
failure against the sunlight/motion context in RESEARCH.md §5, and it interacts with 2.5.8 because
small type produces small targets.
**Fix** Type scale floor of **11 px**, and **12 px** for anything a rider reads to make a
decision. Specifically: `.stname` 10 → **11.5 px** with `font-weight:700` retained (the existing
5-layer text-shadow halo at :218-219 is doing real work and should stay); `.flag/.pee/.good/.tag`
10.5 → **12 px**; `.inf` 9 → **11 px** inside a 20 px circle; `.lh .when` 10 → **11.5 px**.
Raising `.stname` increases label collisions — pair this with the label-decluttering rule in
PROPOSAL.md §4.3 (hide labels for stations whose neighbour is closer than 34 px at the current
zoom, keeping the dot).

### A-16 A departure board is a dead end
**Severity** Medium
**Where** `loadBoard()` :1429-1465; `.brow` :228-230.
**Measured** Board for Newmarket rendered 8 rows, 30.4 px each, none interactive. Content
observed: "23:54 Henderson set not assigned 6m / 00:06 Newmarket set not assigned 18m / …" —
8 of 8 rows read "set not assigned".
**User harm** The rider's next question after "when is it" is "which train is that, and is it a
bad one" — the board shows the answer only when a set is known, and offers no way to act on it.
**Principle** Nielsen #7 (flexibility/efficiency); not a WCAG failure.
**Fix** Make each `.brow` a 48 px `<button>`: when `risk.state === 'known'` it opens that set's
detail/report sheet; when `unknown` it is still a button that expands one line of explanation
(see A-17). Add the line colour as a 4 px left bar on each row — the board currently shows no
line identity at all, which is a real gap when four lines call at Newmarket.

### A-17 "set not assigned" is terse where the planner is generous
**Severity** Medium
**Where** Board: `'<span class="muted">set not assigned</span>'` :1453. Planner: the full honest
sentence at :1655-1656 — "Train not assigned yet — we can't tell you which set this will be until
it's running."
**Measured** Observed board output: every one of 8 Newmarket rows read "set not assigned", with
no explanation. Observed planner output for Papakura→Waitemata at 23:50: both rows carried the
full sentence.
**User harm** The same underlying truth is communicated well in one place and cryptically in
another. "set not assigned" reads like an error or a data outage; it is neither — it is AT not
having rostered a train yet, which is normal, especially late at night.
**Principle** WCAG 3.2.4 Consistent Identification (AA); Nielsen #2 (match with the real world);
and the project's own honesty principle, which is about being *understood*, not merely accurate.
**Fix** One shared certainty vocabulary used by board, planner, popup and list (PROPOSAL.md §6.4).
Board rows get the chip **"set unknown"** with an `aria-describedby` and an on-tap expansion
reading: *"AT hasn't rostered a train to this service yet. We'll show the set number once it's
running."* Identical wording everywhere.

### A-18 `--panel` differs from the documented palette, and `--red` is referenced but never defined
**Severity** Medium
**Where** `:root` :13 declares `--panel:#161c24`. `var(--red)` is used at `#lenpick .len.on`
(:179), `#carpick .car.on` (:191) and `.vote.on` (:272) — and `--red` appears nowhere in `:root`.
**Measured** `grep` of the file: `--red` has 3 consumers and 0 definitions. An undefined custom
property makes `border-color: var(--red)` invalid-at-computed-value-time, so the border falls back
to `currentColor` — meaning the **selected state of the carriage picker and the vote buttons is
drawn in the text colour, not a deliberate selection colour**.
**User harm** The "which carriage was I in" selection — the single most valuable field in the
report form — has a selected state that was designed to be red and is silently not. Selection is
still discernible via `background:#1d2836`, so this is degradation rather than breakage.
**Principle** WCAG 1.4.11 Non-text Contrast (AA) for component *state*; Nielsen #1.
**Fix** Define the token explicitly. Do **not** reuse the AT line red `#D52923` for UI state — it
means "Southern line" in this product and overloading it would be a genuine semantic error.
Use `--sel:#7FB3FF` (the same hue as the focus ring, **7.99:1** on panel, **3:1**+ as a border on
every surface) for selected states, and set `border-color:var(--sel); box-shadow: inset 0 0 0 1px
var(--sel)` so selection reads as a 2 px edge. Keep the `background:#1d2836` fill as the second
channel (1.4.1).

### A-19 `aria-label` does not contain the visible label on the vote buttons
**Severity** Medium
**Where** `logHtml()` :1006-1009: visible text is `👎 yes (3)` with `aria-label="Still a problem"`;
visible `👍 no (0)` with `aria-label="Fine now"`.
**Measured** Rendered text confirmed: *"Still smell? 👎 yes (3) 👍 no (0)"*.
**User harm** Speech-input users saying "click yes" will not match the accessible name "Still a
problem", so the control cannot be activated by voice. Separately the glyphs are inverted against
intuition — 👎 is paired with "yes" (yes, still a problem) and 👍 with "no", which is defensible
in context but reads as a bug at a glance.
**Principle** WCAG 2.5.3 Label in Name (A).
**Fix** Make the visible text and the name agree: `<button aria-label="Still a problem — 3 people
agree">⚠ Still a problem (3)</button>` and `<button aria-label="Fine now — 0 people agree">✓ Fine
now (0)</button>`. This drops the thumb glyphs, which were doing the inversion, and the visible
string is now a prefix of the accessible name — which is what 2.5.3 requires.

### A-20 `cursor:help` + `title` is the only affordance on inference and severity markers
**Severity** Medium
**Where** `.inf { cursor:help }` :255 with `title="Inferred from train positions, not published
by AT"` (:893); `.wmini { cursor:help }` :93.
**Measured** `.inf` renders at 12 × 12 px. Both rely entirely on `title=`.
**User harm** The inferred-6-car marker is the visible carrier of the app's honesty principle —
and on the primary device (touch) it is a 12 px circle with no way to read what it says. The
principle is implemented in the data layer and lost at the glass.
**Principle** WCAG 1.4.13 Content on Hover or Focus (AA) — `title` tooltips are neither
dismissable, hoverable nor persistent; 2.5.8 (AA) for a 12 px target; and the product's own
honesty principle.
**Fix** Make inference a tappable, 24 px, labelled affordance: `<button class="inf"
aria-expanded="false">?<span class="sr-only">Why we say this is a 6-car</span></button>` that
toggles an inline line of text underneath — *"We work this out from two sets reporting GPS one
set-length apart. AT doesn't publish it, so we could be wrong."* Never a `title`. Same treatment
for `.wmini`.

---

## Low

### A-21 The status region is not a live region, so updates are silent to AT
**Severity** Low (Medium for P5)
**Where** `#stat` updated via `innerHTML` every 10 s in `tick()` :1293-1305; `#err` :1278;
`#note` :1310. `#toast` *does* have `role="status" aria-live="polite"` (:337) — correct, and the
model to copy.
**User harm** A screen-reader user is never told that the feed went stale, that an error occurred,
or that the count changed. `#err` in particular announces nothing when the fetch fails.
**Principle** WCAG 4.1.3 Status Messages (AA).
**Fix** `#stat` → `aria-live="polite" aria-atomic="true"`; `#err` → `role="alert"`. Guard against
chatter: only announce when the *state class* changes (fresh → stale → dead), not on every tick.

### A-22 `#hudbar` is a div-as-button carrying an `h1`
**Severity** Low
**Where** :321-325. It has `role="button" tabindex="0" aria-expanded aria-controls` and an Enter/
Space handler (:475-477) — genuinely well done. But it wraps `<h1>Auckland trains — live</h1>`.
**User harm** The page's only `h1` is inside a button, so heading navigation and button
navigation collide; the button's accessible name becomes the whole heading plus "▾".
**Principle** WCAG 1.3.1 Info and Relationships (A).
**Fix** Keep the `h1` as a sibling and put a real `<button>` beside it with
`aria-label="Show details"` / `"Hide details"`, or keep the whole-row hit target (which the
comment at :24-25 rightly defends) by making the row a `<button>` containing a `<span>` and
moving the `h1` out. The hit target must stay row-sized.

### A-23 Motion has no reduced-motion escape hatch
**Severity** Low
**Where** `.train { transition: transform 1.6s linear }` :64; `#chev` :31; `#toast` :286; the
`.livedot` pulse at :233-236.
**Measured** `prefers-reduced-motion` rules in the stylesheet: **0**.
**User harm** Up to ~80 markers animating continuously is a meaningful vestibular trigger, and
the map is the whole page.
**Principle** WCAG 2.3.3 Animation from Interactions (AAA) — not required at AA, but the
`.livedot` comment at :233-234 shows the intent already exists; it is simply unimplemented.
**Fix**
```css
@media (prefers-reduced-motion: reduce) {
  .train { transition: none; }
  #chev, #toast, .livedot { transition: none; animation: none; }
}
```
Markers then jump rather than glide — which is also more honest about a 10 s poll interval.

### A-24 Out-of-service units outnumber services in the arrivals list
**Severity** Low
**Where** `renderList()` sort :1360-1363; `.row.oos { opacity:.62 }` :66.
**Measured** Phone, 23:50 NZDT: **52 rows, 1 in service, 51 not in service**. The one actual
arrival is row 1; rows 2–52 are stabled stock.
**User harm** At off-peak the "Arrivals" sheet is 98 % depot inventory. The label promises
arrivals and delivers a fleet list. (The sort is correct — services first — and the legend toggle
exists, so this is a framing problem, not a logic one.)
**Principle** Nielsen #2 (match with the real world) — "Arrivals" sets an expectation the content
does not meet.
**Fix** Section the sheet with two sticky sub-headers rendered from the existing sort:
**"In service (1)"** and **"Not in service (51) — stabled stock and depot moves"**, with the
second collapsed by default on phones. No data change; it makes the existing honest distinction
visible instead of relying on a 0.62 opacity.

---

## Verified as correct — do not "fix" these

Measured or read and confirmed sound. Listed so a later implementer does not helpfully undo them.

| Thing | Why it is right |
|---|---|
| AT line colours vs dark background | Measured 3.76 (S-C) / 9.68 (E-W) / 7.48 (O-W) / 6.43 (HUIA) against `--bg` — all clear 1.4.11's 3:1. S-C red is the weakest and is correctly used only as a swatch/polyline, never as text. |
| `.stname { pointer-events:none }` :215 | Labels measured 49.7 px wide against a 12 px tightest station gap. Clickable labels demonstrably stole neighbours' clicks. |
| Capture-phase nearest-station handler :532-544 | The only mechanism that can resolve overlapping targets by distance. A-01 extends it; it must not be replaced by per-marker hit circles. |
| No "skip if a train is near" guard | 58 % of trains measured within 20 px of a station — the guard would disable station taps at exactly the stations people tap. |
| Label hide threshold `zoom < 11` :225 | Default view is zoom 11; a threshold of 12 hides every label at the only zoom most users see. |
| `logHtml()` as a pure string builder :977 | Leaflet's `setContent()` is not synchronous; any post-hoc DOM restore races and loses. |
| `hasFault()` / `n_neg` separation :804-807 | Praise must not render as a fault. Six surfaces depend on this. |
| Closed `BEHAVIOURS` list, no free text for `ruffians` :407-415 | A safeguard, enforced server-side. Not a UI convenience to be "improved". |
| Out-of-service units have no ETA/destination :757-765 | There is no trip to derive one from. Inventing one would be the exact lie the product exists not to tell. |
| Planner "unknown" copy :1655-1656 | The best-written string in the app. A-17 proposes copying it elsewhere, not changing it. |
| Warning triangle outside the rotating `<g>` :630-636 | A rotated warning sign reads as decoration. |
| Skull drops to bottom-right when a fault triangle is present :652-653 | Stacked, the two badges became one unreadable blob. |
| "Not sure" as a first-class carriage answer :1161-1165 | A forced guess points riders away from the wrong carriage — worse than no data. |
| 3-car/6-car length buttons | Measured **107 × 48 px** — the only controls in the app that already meet the 48 px bar. Use them as the size reference for everything else. |
