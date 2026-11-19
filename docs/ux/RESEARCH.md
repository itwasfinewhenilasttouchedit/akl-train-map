# RESEARCH — HCD basis for the Auckland live train map refresh

Date: 2026-11-19. Subject: `https://akl-trains.fly.dev` / `web/index.html` (1644 lines,
single file, Leaflet 1.9.4 from CDN, served by FastAPI).

This document states who this is for, what they are actually trying to do, the physical
conditions they do it in, and the standards the audit and proposal are measured against.
Everything downstream (AUDIT.md, PROPOSAL.md, PRIORITISED_PLAN.md, mockup.html) traces
back to something here.

---

## 1. Skills loaded and used

| Skill | Used for |
|---|---|
| `reference/at-open-transit-data` | Domain truth: what AT publishes and what it does not. Decisive for the design, because the UI's honesty rules exist to cover specific gaps in AT's feeds (no consist data, no vehicle on unrostered trips, `stop_time_update` being a past event, `trip_headsign` being operational junk). |
| `design/hcd-ui-review` | **Created during this task** — no design or UX skill existed in the library. It captures the review method used here: measure the rendered page rather than the stylesheet, compute contrast arithmetically, cite WCAG 2.2 by number, use Nielsen's heuristics as the non-accessibility lens, read the repo's DECISIONS/MEMORY files before proposing anything, and tag every plan item safe-mechanical vs needs-careful-review. |

Also read before proposing, per that skill's Rule 5: `DECISIONS.md` (452 lines, 24 recorded
decisions) and `MEMORY.md` (413 lines). These are load-bearing. Several things that look
like defects in the current UI are scars from measured fixes, and section 7 lists what is
therefore off the table.

## 2. Method actually used (not asserted — executed)

- Rendered the live production app in headless Chromium at **390×844** (`device_scale_factor=2`,
  `is_mobile`, `has_touch`) and at **1280×900**, and measured `getBoundingClientRect()` on every
  interactive element, every overlay, the legend, the arrivals rows, the popup, the report form
  at both 3-car and 6-car, and the station board.
- Measured the real **tab order** by pressing Tab 14 times inside the open journey planner and
  recording `document.activeElement` each time.
- Computed **pixel distances between all 48 station dots** at the default zoom 11 via
  `map.latLngToContainerPoint()`, and the distance from every live train marker to its nearest
  station dot — i.e. measured the ambiguity rate directly rather than estimating it.
- Computed **WCAG contrast ratios** for all 32 colour pairs in the stylesheet, plus the four AT
  line colours against `--bg` and `--panel`.
- Enumerated every **font-size actually painted** via `getComputedStyle` across the whole DOM.
- Checked `document.styleSheets` for `:focus` rules and `prefers-reduced-motion` rules.
- Tested **1.4.10 Reflow** at 320×256 (the 400%-zoom equivalent).

Raw measurements are in the findings themselves. The numbers in AUDIT.md are observed values,
not estimates.

## 3. Standards and frameworks applied

### 3.1 WCAG 2.2 (W3C Recommendation)
Source: <https://www.w3.org/TR/WCAG22/>, fetched 2026-11-19, plus
<https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum> for 2.5.8.
Target conformance for this proposal: **Level AA**, with 2.5.5 (AAA) adopted voluntarily for
the primary one-handed controls because of the use context in §5.

Criteria that actually fire on this app, quoted from the spec:

| SC | Level | Normative requirement (abbreviated from the spec text) |
|---|---|---|
| **1.3.1** Info and Relationships | A | Structure and relationships conveyed visually are programmatically determinable. |
| **1.4.1** Use of Color | A | "Color is not used as the only visual means of conveying information, indicating an action, prompting a response, or distinguishing a visual element." |
| **1.4.3** Contrast (Minimum) | AA | Text has a contrast ratio of **at least 4.5:1** (3:1 for large-scale text). |
| **1.4.10** Reflow | AA | No loss of content or functionality, and no two-dimensional scrolling, at a width equivalent to **320 CSS px** / height **256 CSS px**. Note 2 explicitly exempts content needing 2-D layout — *maps are named in that exemption; the chrome around the map is not*. |
| **1.4.11** Non-text Contrast | AA | UI components and the graphical objects needed to understand content have a contrast ratio of **at least 3:1** against adjacent colours. |
| **1.4.13** Content on Hover or Focus | AA | Additional content triggered by hover/focus must be dismissable, hoverable and persistent. |
| **2.1.1** Keyboard | A | All functionality operable through a keyboard interface. |
| **2.4.3** Focus Order | A | Focusable components receive focus in an order that preserves meaning and operability. |
| **2.4.7** Focus Visible | AA | Any keyboard-operable interface has a mode where the focus indicator is visible. |
| **2.4.11** Focus Not Obscured (Minimum) | AA | **New in 2.2.** "When a user interface component receives keyboard focus, the component is not entirely hidden due to author-created content." |
| **2.5.3** Label in Name | A | The accessible name contains the visible label text. |
| **2.5.7** Dragging Movements | AA | **New in 2.2.** All functionality that uses a dragging movement can be achieved by a single pointer without dragging, unless dragging is essential. |
| **2.5.8** Target Size (Minimum) | AA | **New in 2.2.** "The size of the target for pointer inputs is at least **24 by 24 CSS pixels**", except — *Spacing*: undersized targets are positioned so that a 24px-diameter circle centred on each does not intersect another target's circle; *Equivalent*: the function is achievable via another conforming control on the same page; *Inline*; *User Agent Control*; *Essential*. The Understanding doc states the requirement is **independent of page zoom** — "the user can pinch in" is not a defence. |
| **3.2.4** Consistent Identification | AA | Components with the same functionality are identified consistently. |
| **3.3.2** Labels or Instructions | A | Labels or instructions are provided when content requires user input. |
| **4.1.2** Name, Role, Value | A | For all UI components, name and role can be programmatically determined; states/properties set by the user can be programmatically set; notification of changes is available. |
| **4.1.3** Status Messages | AA | Status messages can be programmatically determined through role or properties without receiving focus. |

Two criteria deliberately **not** claimed:
- **2.5.5 Target Size (Enhanced), AAA (44×44)** is adopted for the primary pill controls and
  sheet rows only, as a context-driven choice (§5), not as a conformance claim.
- **2.4.13 Focus Appearance, AAA** informs the focus-ring spec in PROPOSAL.md (2px perimeter,
  3:1 against the unfocused state) because it is cheap to meet here and the alternative is a
  browser default ring that is invisible on `#0d1117`.

### 3.2 Nielsen's 10 usability heuristics
Used as the non-accessibility lens. The four that do real work on a live-data product:

1. **Visibility of system status** — "warming", "stale", and "genuinely zero" must be three
   distinguishable states. `DECISIONS.md` 2026-11-19 already fixed the worst instance of this
   (`age_s ?? 999`); the principle generalises and the proposal extends it.
2. **Match between the system and the real world** — the rider's vocabulary is platform,
   carriage, "the 7:42". Operational vocabulary (trip_id, headsign, consist, OOS) must not leak.
3. **Recognition rather than recall** — a glyph whose meaning is only explained in a `title=`
   attribute is recall, and `title=` does not exist on a touch device at all.
4. **Error prevention** over error messages — e.g. the station disambiguation chooser in
   PROPOSAL.md prevents a mis-tap instead of making the user undo one.

### 3.3 Norman: gulf of execution / evaluation
Stated because the physical context in §5 makes both gulfs wider than a desk-bound product's.
The gulf of evaluation is the sharper one here: after a tap, the user must be able to tell in
under a second whether they got the thing they aimed at. Today they often cannot (AUDIT A-01).

### 3.4 Touch target guidance beyond WCAG
- WCAG 2.2 **2.5.8** sets the floor at 24×24 CSS px (AA), 2.5.6 at 44×44 (AAA).
- Apple HIG gives a 44×44 pt minimum hit region; Material Design 3 gives 48×48 dp.
- This proposal uses **48×48 CSS px** for primary one-handed targets, **44×44** for secondary,
  and never goes below the 24×24 AA floor anywhere. Rationale is in §5, not fashion: the user is
  standing on a moving/windy platform and the published minima assume a seated, stable hand.

## 4. Who the users are

No accounts, no identity, no IP retention — a deliberate decision (`DECISIONS.md` 2026-11-19).
So there is no analytics segmentation to lean on. These personas are derived from the app's own
feature set, the seeded and real report data (10 reports as of 2026-11-19; 9 seeded + 1 genuine
public submission: set 1087, `praise`, "Smells great!"), and the known pain points.

### P1 — The rushing commuter (primary, ~80% of sessions by intent)
On a platform or walking to one. Wants one number: *when is my train, and is it late*. Will
give the app 5–15 seconds. One hand on the phone, the other holding a bag/coffee/child.
Already knows the network; does not need to be taught what the Western line is.
**Failure mode**: taps the wrong station, gets a departure board for Parnell when they wanted
The Strand, and does not notice because both boards look identical apart from one word.

### P2 — The avoidance-seeker (the app's differentiator)
Has been in a bad carriage before. Wants to know whether the train arriving is the one with
reports against it, and which carriage to avoid. This is the only reason to use this app over
AT's own. Needs the pee-train / skull / praised vocabulary to be instantly readable at arm's
length and in sunlight, because the decision window is the ~20 seconds between seeing the train
and boarding it.

### P3 — The reporter (low volume, high value)
Just got off, or is sitting in the bad carriage now. Willing to spend 60–90 seconds. Often
reporting *while still on the moving train* — one hand, vibration, poor signal. The carriage
picker is the critical widget and the "Not sure" option is a first-class answer, not a cop-out
(`DECISIONS.md` 2026-11-19).

### P4 — The enthusiast / operations-curious
Wants the whole fleet, including the ~60 out-of-service units, unit numbers, stabling
locations, 6-car couples. Desktop-leaning. This user is why the hollow-square OOS class exists
and must not be designed away — but their needs must never cost P1 a tap.

### P5 — The screen-reader or motor-impaired rider
Not hypothetical: a public transport tool is exactly the product class where this matters most,
and the current app is effectively unusable for them (AUDIT A-04, A-05, A-09, A-13). Includes
sighted users with tremor, one-handed users, and anyone on a bus using VoiceOver.

## 5. Contexts of use — and what each one forces

| Condition | Design consequence |
|---|---|
| **One-handed, thumb reach** | On a 390×844 phone the comfortable thumb arc covers roughly the bottom 55% of the screen. Primary actions therefore live at the **bottom**, never the top. The current HUD puts the only persistent controls at the top (AUDIT A-10). |
| **Sunlight on an OLED at partial brightness** | Effective contrast collapses. 4.5:1 in a lab is marginal outdoors. The proposal targets **≥7:1 for primary text** and ≥4.5:1 for every secondary string, and bans the 10px type that currently accounts for 48 painted elements. |
| **Wind / movement / vibration** | Tap accuracy degrades. This is the justification for 48px primary targets and for a **disambiguation chooser** rather than merely a wider hit radius — a wider radius makes a confident wrong answer more likely, which violates the product's honesty principle at the interaction layer. |
| **Rushing; ≤15 s budget** | The answer to "when is my train" must be reachable in **one tap from cold**, and must be legible without expanding anything. |
| **Poor/4G signal, Fly trial machine cold-starts (~5.8 s first byte)** | Loading and warming are *normal* states, not edge cases, and need designed treatments (`MEMORY.md`: the machine stops after ~5 min on the trial plan). |
| **Glanceable from arm's length** | Badge semantics must survive at ~0.5 m. The current `.wmini` warning glyph measures **9.9 × 13 px**. |

## 6. Jobs to be done

Phrased as jobs, with the current cost and the target cost. "Taps" means deliberate pointer
actions from a cold load on a phone.

| # | Job | Persona | Today | Target |
|---|---|---|---|---|
| **J1** | *When is the next train from where I'm standing, and is it late?* | P1 | GPS lookup is buried inside the journey planner (`#jpgps`, only reachable after opening `#jpwrap`). 2 taps minimum, plus a permission prompt, plus manual station choice if the tap lands wrong. | **1 tap** from a persistent "Near me" control; board opens with station name confirmed. |
| **J2** | *Which train is this one in front of me, and should I get on it?* | P1/P2 | Tap the train marker — but measured, **58% of live trains sit within 20 px of a station dot**, and the station wins inside 10 px by design. So the tap is a coin flip at the exact moment it matters. | Deterministic: an ambiguous tap opens a **chooser** naming both candidates; no guessing. |
| **J3** | *Does this train have reports against it, and which carriage?* | P2 | Badge is a 9.9×13 px glyph in the arrivals list with the meaning only in `title=` (unavailable on touch), or a full label inside a popup that must be opened first. | Status legible without opening anything; wording visible, not hover-only. |
| **J4** | *Report what I just experienced, tied to the right set and carriage.* | P3 | Form works and the carriage picker is genuinely good (48px length buttons, 45×41px car buttons). But Cancel/Send are **24 px tall** — below the 2.5.8 AA floor — and the form is not a dialog to a screen reader. | All controls ≥44px; proper `role="dialog"` with focus management. |
| **J5** | *Plan a trip and know whether the train assigned will be a bad one.* | P2 | Works, and the honesty copy is correct. But at 320px the planner results read "Train not assigned yet" for every row with no explanation of why that is normal at that hour. | Same honesty, better framing: unknown is a designed state with an explanation, not an apology. |
| **J6** | *Confirm or clear someone else's report.* | P2 | Vote buttons measured **68.7×21.2 px with a 4 px gap** — fails 2.5.8 on both size and spacing, inside a Leaflet popup that re-renders every 10 s. | ≥44px targets, ≥8px gap, stable during refresh. |
| **J7** | *See the whole fleet including stabled stock.* | P4 | Works well. Hollow squares, legend toggle, honest absence of ETA. | Unchanged in substance. |

## 7. Constraints the design must respect (and the decisions behind them)

These are not negotiable, and several are recorded decisions with measured evidence behind them.

1. **Single self-contained HTML file.** Leaflet from CDN, FastAPI static serve. No React/Vue/
   Tailwind, no build step. Every proposal below is plain HTML/CSS/vanilla JS and is implementable
   in-place in `web/index.html`.
2. **Dark theme stays.** `--bg:#0d1117`, `--panel:#161b22` (the file actually declares
   `--panel:#161c24` at line 13 — see AUDIT A-18).
3. **AT line colours are not ours to redesign**: S-C `#D52923`, E-W `#97C93D`, O-W `#00AEEF`,
   HUIA `#c678dd`. Measured against `--bg`: 3.76 / 9.68 / 7.48 / 6.43 — all clear 1.4.11's 3:1
   for graphical objects, so no change is needed or permitted. S-C red at 3.76 is the weakest and
   must never be used as *text* colour (it would fail 1.4.3); it is used as a swatch and a
   polyline only, which is correct.
4. **The honesty principle is load-bearing.** Never show a confident value the data does not
   support. Specifically preserved: "Train not assigned yet — we can't tell you which set this
   will be until it's running"; the inferred-6-car `?` marker; out-of-service units having no
   destination or ETA; `warming` never rendering as a number.
5. **Mobile is primary.** Phone layout decisions beat desktop elegance where they conflict.
6. **Anonymity.** No accounts, no identity, no IP retention. Nothing in this proposal introduces
   a login, a profile, or any persistent identifier beyond the existing random `voterToken`.

### Decisions I verified and will NOT propose reverting

Read from `DECISIONS.md`/`MEMORY.md` and confirmed in code. Each of these looks like a candidate
for "improvement" and each is right as it stands:

- **`.stname { pointer-events:none }`** (index.html:215). Labels are ~49.7 px wide (measured)
  while the tightest station pair is 12 px apart. Making labels clickable meant whichever
  rendered last swallowed its neighbour's clicks *and* the dot beneath. Keep.
- **The capture-phase nearest-station click handler** (index.html:525-537). Leaflet markers
  consume clicks entirely; `preferCanvas:true` means station dots have no DOM node. A
  nearest-wins proximity test is the only thing that can resolve overlapping targets correctly.
  The proposal **extends** this with a chooser; it does not replace it.
- **No "skip if a train is near" guard.** An earlier version had one and it disabled station
  clicks precisely at stations, because a stopped train is 0–1 px from the dot. Never re-add.
- **Station labels hidden below zoom 11, not 12.** Default view is zoom 11; a threshold of 12
  made every label invisible at the only zoom most visitors ever see.
- **Report log renders inline from `LOGDATA` inside the popup markup.** Leaflet's
  `setContent()` does not swap the DOM synchronously, so any "restore it afterwards" approach
  races and loses. Keep the pure-string-builder pattern (`logHtml`).
- **Praise is not a fault** — `n_neg` / `worst_neg` / `top_fault` separation, green `praised`
  badge, no warning triangle. Six surfaces had to change together to get this right.
- **"Ruffians" has no free-text field and a closed `BEHAVIOURS` list,** stripped server-side.
  This is a safeguard, not a convenience, and the proposal keeps the closed list verbatim.
- **Resolved reports stay visible with a badge** rather than being deleted.
- **Two marker shapes** (filled arrow in service, hollow square out of service) with
  deliberately no ETA/destination on the latter.
- **Fault triangle outside the rotating `<g>`**; skull moves to bottom-right when both badges
  are present, because stacked they became an unreadable blob.

## 8. What success looks like

Measurable acceptance criteria for the refresh, so PRIORITISED_PLAN.md items can be verified
rather than argued about:

| Metric | Today (measured) | Target |
|---|---|---|
| Interactive elements below 24×24 CSS px on a main path | ≥11 distinct controls | 0 |
| Primary one-handed controls below 48×48 | all of them (34.1 px pills) | 0 |
| Ambiguous map taps (train within 20 px of a station dot) resolved deterministically | 0% — silent nearest-wins | 100% via chooser |
| Text contrast failures (1.4.3) | 1 (`.btn.primary`, 3.88:1) | 0 |
| Non-text contrast failures (1.4.11) | `--line` borders at 1.36:1 on panel | 0 |
| `:focus` style rules in the stylesheet | **0** | full set, 3:1 ring |
| `prefers-reduced-motion` blocks | **0** | 1, covering all transitions |
| Overlays with `role="dialog"` + focus trap + focus return | 0 of 3 | 3 of 3 |
| Smallest painted font size | 10 px (48 elements) | 11 px floor, 12 px for anything a rider reads |
| Focusable elements on the page at rest (phone) | 81 (52 are undifferentiated map markers) | map markers grouped behind one managed entry point |
| Taps from cold load to "next departures from where I'm standing" | 2 + permission + manual correction | 1 + permission |
