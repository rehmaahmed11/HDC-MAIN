# RESPONSIVE / ADAPTIVE DESIGN — HDC ERP

**Goal:** every page, module, form and table fits every screen — 320px phone,
360/390/414px phone, phablet, tablet (portrait and landscape), small laptop,
laptop and wide desktop — with **nothing cropped off** and **less scrolling**
than before, because data-entry operators work from phones too.

**How:** one stylesheet and one script, loaded last by
`templates/hdc/shared/base.html`. **No template was rewritten and the desktop
layout is untouched** — the laptop view the team already works with is
unchanged; everything new only fires below 992px.

| File | Role |
|---|---|
| `static/hdc/css/responsive.css` | the breakpoint layer (new) |
| `static/hdc/js/core/responsive_tables.js` | turns ordinary tables into record cards on phones (new) |
| `templates/hdc/shared/base.html` | loads both, last; `viewport-fit=cover` |
| `hdc/app.py` | `hdc_asset_stamp` — cache-buster so phones pick up new CSS/JS |
| `scripts/qa_responsive.py` | static gate (syntax, breakpoint coverage, load order) |
| `tests/test_responsive_layer.py` | regression tests for the above |

---

## 1. The four ideas

### 1.1 Nothing is cropped off
A global guard (`overflow-x: clip` on `html`/`body`, `min-width: 0` on flex and
grid children, `max-width: 100%` on media and canvases, `overflow-wrap` on
text) means no element can push the page sideways. `clip` is used instead of
`hidden` on purpose: `hidden` would turn `<html>` into a scroll container and
break the sticky top bar.

Every wide table in the app declares a `min-width` (audit sheets 940px, the
history register 940px, tracking 820px, stage members 880px…) so its columns
keep their shape on a laptop. On a small screen that floor *is* the overflow.
Below 992px the floor is removed: a table that already fits is unaffected, and
an over-wide one wraps into the viewport instead of running off it.

### 1.2 Tables become record cards on phones
`core/responsive_tables.js` copies each column heading onto every cell
(`data-hdc-label`), marks the cell that leads the row (date / code / name) as
the card title, and marks the table `hdc-rt-stack`. Below 768px
`responsive.css` then renders **one row = one card**:

```
┌──────────────────────────────────────┐
│ 2026-10-10 · TXN-1042                │  ← lead cell (card title)
├──────────────────────────────────────┤
│ ACCOUNT            AMOUNT            │
│ Company Cash       45,000            │
│ PROJECT            STATUS            │
│ Ali Residency      Posted            │
└──────────────────────────────────────┘
```

The operator scrolls **down** through records instead of sideways through
columns, and every value keeps its column name — nothing is hidden.

* Blank cells and `—` placeholders are dropped from the card (they come back
  on tablet/desktop), which is why a 16-column stages row shows about 8 lines.
* Fields flow into as many columns as the screen allows: 2-up on a 360px
  phone, 3-up on a 414px phone, more on a tablet.
* Rows that genuinely span the table — a totals row, a "Project A — 3 stages"
  group heading — are kept as full-width strips between the cards, so the
  operator still gets the total.
* A table built out of `colspan`/`rowspan` (a matrix, a printable grid) keeps
  its shape: it is a spreadsheet, not a list.

Measured across **121 rendered pages / 1 038 tables** (seeded demo database):

| Result | Tables |
|---|---|
| become record cards | 982 |
| keep the grid (merged cells / no `<thead>`) | 6 |
| single "no records" cell | 49 |
| totals / group rows preserved as strips | 15 |
| cells left unlabelled | **0** |
| cards without a title | **0** |

Average fields per card: **5.9**.

### 1.3 Forms get shorter, not smaller
* **Filter bars** (`form[method="GET"]`) stop stacking one control per line and
  flow into an auto-fit grid: 2 per line on a 360px phone, 3 on a 414px phone,
  4–5 on a tablet. A twelve-filter ledger search goes from twelve lines to
  four.
* **Entry forms:** `col-md-1/2/3` fields (dates, amounts, quantities — 228
  `col-md-3` in the codebase) sit two per line on phones instead of one.
* Spacing, labels and card padding tighten; `mb-3` goes 16px → 9.6px, `g-3`
  16px → 9.6px, label 0.8rem → 0.74rem.
* **Every input stays 16px** so iOS does not zoom the page the moment a field
  is focused — the single most annoying thing on a phone data-entry form.
* Inputs inside tables (attendance, audit sheets, delivery rows) fill their
  field instead of staying capped at ~110px and centred.

### 1.4 The important things stay on screen
* Top bar stays sticky, and shrinks 56px → 52px on small screens.
* **Modals become full-screen sheets on phones**: header pinned top, body
  scrolls, action buttons pinned bottom — the Save button can never end up
  under the fold.
* The primary action of each page goes full width on phones.
* `100dvh` is used for the navigation drawer and phone dialogs, so the
  **Logout** and **Save** buttons are not hidden under mobile Safari's
  toolbar (the classic `100vh` bug).
* `env(safe-area-inset-*)` padding keeps controls clear of notches and home
  bars.

---

## 2. Breakpoints

| Range | What changes |
|---|---|
| ≥ 1400px | nothing — wide desktop |
| 992–1399px | **nothing** — the laptop view that already works |
| 768–991px | density pass: tighter cells/cards/headers, tables fit the width, filters 4–5-up, KPI 2-up |
| 576–767px | record cards, 2-up filters, compact shell, scrollable sub-nav rails |
| ≤ 575px | full-screen modals, full-width page actions |
| ≤ 419px | smaller KPI type, tighter buttons and tabs |
| ≤ 359px | 320px phones: single-column entry fields, minimal padding |
| landscape ≤ 560px tall | reduced chrome: 46px top bar, 76vh modal bodies |
| `(hover: none)` | thumb-sized targets: 40px buttons, 38px pagination and tabs |
| `(prefers-reduced-motion)` | no transitions |

Capability queries (touch, reduced motion, `dvh`) are used instead of width
where they are the honest question.

---

## 3. Escape hatches

A page that must keep its table shape opts out once:

```html
<table class="hdc-no-stack">        <!-- or wrap in <div data-hdc-no-stack> -->
```

No CSS fight, no `!important` war.

---

## 4. Verifying it

```bash
# static gate: syntax, breakpoint coverage, load order, viewport tag
python scripts/qa_responsive.py            # or --json

# regression tests
python -m unittest tests.test_responsive_layer

# the browser gate already asserts no page overflows horizontally at 390px
python scripts/qa_browser.py --output qa/browser.json   # needs playwright
```

`scripts/qa_browser.py` renders every page at 1440px **and 390px** and fails
the release if `document.documentElement.scrollWidth > innerWidth`.

### Manual check on a real phone
Open the app on the phone and check: nothing scrolls sideways; a ledger row is
one card; a filter bar is 2–3 controls per line; a modal is full-screen with
Save pinned at the bottom; the drawer's Logout button is reachable.

If a phone shows the old layout after a deploy, the `?v=` stamp
(`hdc_asset_stamp`, derived from file mtimes in `hdc/app.py`) is what forces
the refresh — a hard reload picks it up immediately.

---

## 5. What was deliberately NOT changed

* **Desktop and laptop output.** Every strong rule sits behind
  `@media screen and (max-width: 991.98px)`.
* **Print.** Every strong rule is guarded with `screen and`, so salary cards,
  project reports and receipts print exactly as before.
* **Pinch-zoom.** `maximum-scale` is not set: operators zoom into dense
  ledgers, and blocking that is an accessibility failure.
* **Any template.** 110+ templates were not touched; the two-line change to
  `base.html` is the whole integration surface.
