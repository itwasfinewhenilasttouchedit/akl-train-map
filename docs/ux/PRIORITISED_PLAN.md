# PRIORITISED PLAN

Ordered by **user value ÷ implementation cost**, in waves. Every item traces to an `A-nn`
finding in `AUDIT.md` and carries a risk tag:

- **safe-mechanical** — CSS values, attributes, copy strings, markup wrappers. No logic, no
  state, no hit-testing. Verifiable by looking at the result.
- **needs-careful-review** — touches hit-testing, focus/state machines, Leaflet's popup
  lifecycle, or race-prone paths. `MEMORY.md` records several hard-won fixes in exactly these
  areas; these items must be measured after the change, not assumed.

Cost is engineering effort on the existing single file: **S** ≤ 30 min, **M** ≤ 2 h, **L** ≤ half a day.

Waves are independently shippable. Wave 1 alone closes every Level A/AA contrast and
target-size failure on the main paths.

---

## Wave 1 — Fix what is measurably broken (safe, high value, low cost)

Pure CSS and attribute work. No behavioural change, no layout restructure. If only one wave
ever ships, this is the one.

| # | Change | Closes | Cost | Risk |
|---|---|---|---|---|
| 1.1 | `.btn.primary` fill `#2f6feb` → `--accent:#1F5BD7`, text `#FFFFFF`, add 1px `--edge` border | A-08 (3.88:1 → 5.94:1) | S | safe-mechanical |
| 1.2 | Introduce `--edge:#626D7E`; apply to all inputs, selects, textareas, `.btn`, `.vote`, `#lenpick .len`, `#carpick .car`, `.panel`. Rename the old `--line` → `--rule`, keep it for in-panel dividers only | A-07 (1.36:1 → 3.27:1) | S | safe-mechanical |
| 1.3 | Define the missing `--red`: add `--focus:#7FB3FF` and use it for `.vote.on`, `#lenpick .len.on`, `#carpick .car.on` + `inset 0 0 0 1px` | A-18 (selected state currently falls back to `currentColor`) | S | safe-mechanical |
| 1.4 | Global `:focus-visible { outline:3px solid #7FB3FF; outline-offset:2px }` + `.sr-only` utility | A-05 (0 focus rules today) | S | safe-mechanical |
| 1.5 | `@media (prefers-reduced-motion: reduce)` block disabling `.train` transition, `#chev`, `#toast`, `.livedot` | A-23 (0 such rules today) | S | safe-mechanical |
| 1.6 | `.vote { min-height:44px; padding:10px 14px; font-size:13px }`, `.lv { gap:10px; flex-wrap:wrap }` | A-02 (68.7×21.2 @4px gap) | S | safe-mechanical |
| 1.7 | `.btn { min-height:44px; padding:11px 16px; font-size:14px }`; `#fb .acts` → Send full-width 52px on its own row, Cancel below | A-03 (22–24px today) | S | safe-mechanical |
| 1.8 | `#sheetbtn`/`#planbtn` → `min-height:52px; padding:0 20px; font-size:15px` | A-03 (34.1px today) | S | safe-mechanical |
| 1.9 | Type floor: raise every 9/9.5/10/10.5px rule to the §5.2 scale. `.stname` 10 → 11.5px (keep the 5-layer halo verbatim) | A-15 (254 of ~268 elements at ≤10.5px) | M | safe-mechanical |
| 1.10 | Interim overlap fix: `#list { bottom:56px; max-height:calc(100vh - 166px) }` on phones | A-11 (sheet renders under the pills) | S | safe-mechanical |
| 1.11 | `#stat` → `aria-live="polite" aria-atomic="true"`; `#err` → `role="alert"` | A-21 | S | safe-mechanical |
| 1.12 | Vote button text/label agreement: `⚠ Still a problem (3)` / `✓ Fine now (0)`, `aria-label` extending the visible text | A-19 (2.5.3) | S | safe-mechanical |
| 1.13 | Board rows: `set not assigned` → `chip--unknown` "Set unknown" with the planner's canonical sentence in an expandable line | A-17 (3.2.4) | S | safe-mechanical |
| 1.14 | Severity pips `●○○ / ●●○ / ●●●` added to `.flag`, `.wmini`, `.jrisk` | A-14 (1.4.1) | S | safe-mechanical |

**Verification for Wave 1** — rerun the measurement script: every control ≥44px, zero contrast
pairs below threshold, `:focus-visible` rules present, `prefers-reduced-motion` present. Each is
a number, not a judgement.

---

## Wave 2 — Make ambiguity explicit (the headline fix)

The user's own top complaint. Highest user value in the whole plan, and the only wave that
touches the hit-testing code `MEMORY.md` warns about.

| # | Change | Closes | Cost | Risk |
|---|---|---|---|---|
| 2.1 | Refactor `stationNear()` → `candidatesNear()` returning **all** candidates within the radius, stations *and* train markers, sorted by distance | A-01 | M | **needs-careful-review** |
| 2.2 | 0 candidates → no-op; 1 → act immediately (byte-identical to today's behaviour); 2+ → chooser | A-01 | M | **needs-careful-review** |
| 2.3 | Build the chooser: `role="menu"`, 48px rows, nearest-first, line colour bar, certainty chip, Escape = dismiss **without acting** | A-01, A-13 | M | **needs-careful-review** |
| 2.4 | Desktop: chooser threshold 10px instead of 20px (a mouse is precise) | A-01 | S | safe-mechanical |
| 2.5 | Marker accessible names via `<svg role="img" aria-label>` built from the data `popupFor()` already has | A-09 | M | safe-mechanical |
| 2.6 | Remove markers from the sequential tab order (`keyboard:false`); arrivals list becomes the keyboard equivalent | A-09 (52 nameless tab stops) | S | **needs-careful-review** |
| 2.7 | Arrivals rows, `#worst .w` rows and `.brow` rows → real `<button>`, `min-height:48px`, data-built accessible names | A-13, A-16 | M | safe-mechanical |

**Review notes for 2.1–2.3, 2.6.** These are the exact areas with recorded scars:
- Do **not** add a "skip if a train is near" guard — 58% of trains measured within 20px of a
  station dot; the guard disables station taps at the stations people actually tap.
- Do **not** replace the proximity test with per-marker hit circles — they fire on whatever is
  drawn last, not what is nearest, which was the original bug.
- Keep `.stname { pointer-events:none }`.
- After 2.6, verify that popups still open by click (Leaflet markers consume clicks entirely;
  `keyboard:false` must not disable pointer handling).
- Measure after: re-run the ambiguity script and confirm every 2+-candidate tap opens a chooser
  and every 1-candidate tap does not.

---

## Wave 3 — Move the controls into the thumb

Layout restructure. Highest effort, and the change a user would describe as "the refresh".
Depends on Wave 1's tokens but not on Wave 2.

| # | Change | Closes | Cost | Risk |
|---|---|---|---|---|
| 3.1 | Bottom action bar: Near me / Arrivals / Plan / More, 64px + safe-area, 48px targets, `aria-current` | A-10 | L | safe-mechanical |
| 3.2 | Status strip at top, 40px, non-interactive, live-region, with word + dot (not dot alone) | A-10, A-21, A-14 | M | safe-mechanical |
| 3.3 | HUD contents (legend, toggles, most-reported, data notes) → the "More" sheet; retire `#hud`/`#hudbar`/`#chev` and the `hudCollapsed` localStorage key | A-10, A-12, A-22 | M | **needs-careful-review** |
| 3.4 | Sheet component: 70vh cap, internal scroll, `overscroll-behavior:contain`, anchored above the bar via `--bar-h`, strict z-ladder | A-11, A-12 | M | safe-mechanical |
| 3.5 | "Near me" promoted to a first-class bar action, reusing the existing `#jpgps` branch logic verbatim | J1 (2 taps + correction → 1 tap) | M | safe-mechanical |
| 3.6 | Arrivals sheet sectioned: "In service (n)" / "Not in service (n)" with the second collapsed by default on phones | A-24 | S | safe-mechanical |
| 3.7 | Reflow: below 400px, strip to one line, bar drops labels but keeps 48px icon targets | A-12 (33% of a 320px viewport today) | M | safe-mechanical |
| 3.8 | Desktop: 320px left rail replacing the stacked `#hud` + `#list`; Leaflet popup retained on desktop | — | M | safe-mechanical |

**Review note for 3.3**: retiring `#hud` removes the `#hudbar` keyboard handler at index.html:475
and the `hudCollapsed` persistence at :462. Check nothing else reads that key, and keep the
`isPhone()` breakpoint at `max-width:640px` so CSS and JS stay in sync.

---

## Wave 4 — Dialog semantics and the detail sheet

| # | Change | Closes | Cost | Risk |
|---|---|---|---|---|
| 4.1 | `role="dialog" aria-modal="true" aria-labelledby` on `#fb`, `#st`, `#jp`; add `id="jptitle"`/`id="fbtitle"` to their `<h2>`s | A-04 | S | safe-mechanical |
| 4.2 | Focus management: store the opener, focus the title on open, trap Tab, return focus on close — for all three overlays | A-04 (focus measured on `#sheetbtn`, behind the overlay) | M | **needs-careful-review** |
| 4.3 | Phone: replace the Leaflet popup with a bottom detail sheet; desktop keeps the popup | A-03, A-11 | L | **needs-careful-review** |
| 4.4 | Reports log moves into the detail sheet, still rendered inline from `LOGDATA` by `logHtml()` | A-02 | M | **needs-careful-review** |
| 4.5 | `.inf` and `.wmini` → 24px `<button aria-expanded>` revealing real text; delete every `title=`-only affordance | A-20, A-06 (1.4.13) | M | safe-mechanical |
| 4.6 | Report form controls to 48px/16px; 6-car carriage picker to 7×48px cells | A-03 | S | safe-mechanical |
| 4.7 | Skip link + `<main>` / `<nav aria-label="Main actions">` landmarks; move `<h1>` out of `#hudbar` | A-22 | S | safe-mechanical |

**Review note for 4.3/4.4**: `logHtml()` must stay a pure string builder rendered as part of the
sheet's markup from cached `LOGDATA`. Leaflet's `setContent()` is not synchronous and any
"restore the log afterwards" approach races and loses — this is a recorded bug, not a theory.
Also preserve `refreshFlags()`'s three-surface rebuild (markers, list, popup/sheet): miss one and
warnings vanish for the first 10 seconds.

---

## Wave 5 — Decluttering and polish

| # | Change | Closes | Cost | Risk |
|---|---|---|---|---|
| 5.1 | Label collision suppression: hide a label when its nearest neighbour is <34px, interchange stations win, stable tiebreak; all labels return at zoom ≥13 | A-01 (cosmetic half), A-15 | M | **needs-careful-review** |
| 5.2 | OOS markers default off below zoom 12, with the count in the status strip as the one-tap way back; persist in localStorage | "reads as cluttered", A-24 | M | safe-mechanical |
| 5.3 | Full empty/loading/error/unknown state copy from PROPOSAL §8, including `[Try again]` as a real button | A-17 | M | safe-mechanical |
| 5.4 | `prefers-contrast: more` block (`--edge:#8A99AD`, +0.4px badge strokes) | — | S | safe-mechanical |
| 5.5 | Planner: swap button, 48px selects, and the "nothing is rostered yet" group explanation | A-03, A-17 | S | safe-mechanical |
| 5.6 | Departure-board rows gain a 4px line-colour bar and become buttons | A-16 | S | safe-mechanical |
| 5.7 | `#toast` repositioned to `bottom: calc(var(--bar-h) + 12px)` | A-11 | S | safe-mechanical |

**Review note for 5.1**: the existing `.lowzoom` rule (index.html:225) must remain as the floor —
the threshold is `< 11` because the default view *is* zoom 11, and `< 12` made every label
invisible on first load. The new rule is additive, operating at zoom ≥ 11 only.

---

## Dependency graph

```
Wave 1 (tokens, sizes, a11y basics)
   ├──> Wave 3 (bar/sheets need --bar-h, --tap, z-ladder)
   │       └──> Wave 4.3/4.4 (detail sheet needs the sheet component)
   ├──> Wave 2 (chooser rows need the row component + focus ring)
   └──> Wave 5 (polish assumes the token set)

Wave 2 and Wave 3 are independent of each other and can run in parallel.
Wave 4.1/4.2/4.5/4.6/4.7 need only Wave 1.
```

## Effort and value summary

| Wave | Cost | Items | Value | Risk profile |
|---|---|---|---|---|
| 1 | ~1 day | 14 | Closes every A/AA contrast + target-size failure on main paths | 14 safe-mechanical, 0 careful |
| 2 | ~1 day | 7 | Fixes the user's #1 complaint; makes map taps deterministic | 3 safe, 4 careful |
| 3 | ~2 days | 8 | One-handed operation; J1 from 2 taps to 1 | 7 safe, 1 careful |
| 4 | ~1.5 days | 7 | Makes the app usable with a keyboard/screen reader at all | 5 safe, 3 careful |
| 5 | ~1 day | 7 | The "less cluttered" perception; complete state coverage | 6 safe, 1 careful |

**Minimum useful slice**: Wave 1 + items 2.1–2.3. One day of safe CSS plus one careful
refactor, and it closes the two things the user actually complains about (hard-to-tap stations,
competing controls) along with every measurable accessibility failure on the main paths.

## Acceptance checks (all numeric — rerun the measurement script)

| Check | Before | After |
|---|---|---|
| Controls < 24×24 on a main path | ≥11 | 0 |
| Primary controls < 48×48 | all | 0 |
| Text pairs < 4.5:1 | 1 (`.btn.primary` 3.88) | 0 |
| Component boundaries < 3:1 | `--line` 1.36 | 0 |
| `:focus-visible` rules | 0 | ≥1 global |
| `prefers-reduced-motion` blocks | 0 | 1 |
| Overlays with dialog role + focus trap + return | 0 / 3 | 3 / 3 |
| Tab stops before the first real control (phone) | 52 markers | 0 |
| Smallest painted font-size | 10px × 48 elements | 11.5px |
| Ambiguous taps (2+ candidates) resolved explicitly | 0% | 100% |
| Sheet rows obscured by the pills/bar | 34px of them | 0 |
| HUD share of a 320px viewport | 33% | ≤13% (40px strip) |
