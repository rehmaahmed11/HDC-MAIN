# Expense Categories — UI / CSS audit

**Section:** `/hdc/expense_categories` (`templates/hdc/expenses/expense_categories.html`
+ the `.expense-category-*` block in `static/hdc/css/hdc.css`).
**Trigger:** PR #122 ("Polish expense category form widths") left the page looking
off — fields stretched, actions stranded.
**Answered:** 2026-10-13 · branch `arena/c3a03c19-hdc-main` · fixes in the same PR.

The page is an admin list of names. The app already has a polished, consistent
way to build one — the **Trades** workspace (`templates/hdc/workers/trades.html`),
and the same idiom in *Allowance Categories*, *Staff Ledger*, *Suppliers*: a
read-only name in the row, and **every row action grouped as icon buttons in a
right-aligned Actions cell**. Expense Categories was the only list in the app
that put a live text box inside every row and split the row's actions across two
columns. That is what read as unprofessional.

---

## 1. What was actually on screen (1440px viewport, measured)

`.main-content` is `px-4` (24px each side), so the content area is 1392px wide.
Widths below come from the resolved cascade (`scripts/css_cascade_probe.py`) plus
the table/flex box model; text-dependent widths (buttons) are marked ≈.

| Element | Before (PR #122) | Why |
|---|---|---|
| page shell | 1088px (`68rem`), centred | `hdc.css:788` |
| **add card** | **704px** (`44rem`), **left-aligned** | `hdc.css:805` |
| add form | grid `minmax(0,1fr) auto`, `align-items:end`, gap 12px | `hdc.css:807` |
| **add field** | **≈537px** for a ~20-character name | `max-width:100%` → fills the `1fr` track (`hdc.css:815`) |
| add button | ≈120px, bottom-aligned, parked at the card's right edge | `hdc.css:816` |
| **table card** | 1088px → **its right edge is 384px away from the add card's** | `hdc.css:799` vs `805` |
| `#` column | 64px (`4rem`) for a 1–2 digit number, plain text | `hdc.css:818` |
| **Actions column** | **136px (`8.5rem`) holding one ≈31px button** — ~105px of empty column | `hdc.css:819` |
| **row name cell** | **a live input in every row, 512px (`32rem`)** | `hdc.css:820–821` |
| row actions | Save (tick) sits 8px right of the 512px field; **Delete (trash) is ≈330px further right**, across an empty 886px name column | `hdc.css:825` |

### The four defects, in the order they hit the eye

1. **The two cards disagree.** The add card stops 384px short of the table's
   right edge, so the page has no vertical line to read down — the "Add
   Category" button lands in the middle of the page while the table's Actions
   column starts 250px further right.
2. **The name field is sized for the card, not for the data.** A `1fr` track in
   a 704px card is ≈537px wide; the names are `Food & Refreshment`,
   `Petrol / Diesel`. The label sits top-left, the button bottom-right, and
   ≈400px of the field is empty at all times.
3. **The table is a wall of text boxes.** Every row renders its own always-live
   `<input>` (512px). Nothing signals which row is editable — all of them look
   editable, none look committed. Trades shows the name and reveals the field
   only when the pencil is pressed.
4. **The row's two actions are in different columns.** Save lives in the name
   cell (it must, it belongs to that row's `<form>`), Delete lives 330px away in
   a 136px Actions column that is mostly empty. Grouping them is the whole point
   of an Actions column.

---

## 2. The app-wide side effect (this is the part that was not in the PR's remit)

PR #122 also added a blanket cap to the **shared** control rule:

```css
.form-control, .form-select { max-width: min(100%, 36rem); }   /* hdc.css:787, removed */
```

That rule is inherited by ~98 templates. It is invisible on pages built from
Bootstrap grid columns (a `col-md-4` in a 1392px row is ~340px, under the cap),
but it silently clamps every **full-width** field that is wider than 576px.

Measured on `/hdc/accounts`, "Edit Transaction Entry" dialog
(`modal-lg` → `--bs-modal-width: 800px`, `.modal-body` padding `1rem`, so 768px
of usable width):

```
input#edit_from_account_input   width = 100%               ← bootstrap
                                max-width = min(100%, 36rem) ← hdc.css:787
```

The dialog's three `col-md-4` fields (≈250px) were unaffected; the full-width
*From Account* / *To Account* fields and their `<select>`s dropped from 768px to
576px — **192px short of the dialog's own footer buttons**. In a `modal-xl`
(1140px → 1108px of body) a `col-md-8` field (≈730px) or any full-width control
was clamped the same way. Six dialogs ship `modal-xl`
(`accounts.html:577`, `purchase_v2_delivered.html:406`, `tool_reports.html:314`,
`tool_serial_tracking.html:166`, `tool_tracking.html:118`, `tool_txn.html:150`).

The rule also locked itself in: `tests/test_responsive_layer.py` pinned the
exact text of the global rule, so the side effect could not be noticed by CI —
only by looking at the pages.

**A field that needs a readable length caps itself on its own wrapper.** The app
already does this (`hdc.css` `.stage-form-card .form-control` /
`.stage-terms-grid .form-select`); Expense Categories now does the same.

---

## 2b. Follow-up (2026-10-17): the page was aligned to the app, not beside it

The rebuild above fixed the *table*, but it also gave the page a private
"workspace" shell that no other screen used: a centred 840px column, a hero
tile, a count pill and a one-line quick-add bar. Trades had been cloned from
the same recipe, so the app's two newest list screens looked like a different
product from Projects, Workers, Materials, Allowance Categories and
Personal Expense Categories — which all use:

* a page head that is a **direct child of `<main class="main-content">`**
  (`d-flex justify-content-between align-items-center mt-2 mb-3`), with the
  title + row count in an `<h5>` and the page's one action as its own child
  (so `responsive.css` can make that action full-width on a phone);
* one full-width `card hdc-card` holding a `table hdc-table` in a
  `.table-responsive` wrapper;
* the add form in a Bootstrap modal, like Workers / Allowance Categories /
  Personal Expense Categories.

That shape is now a macro, `templates/hdc/shared/_page_head.html`, and both
name lists render through it. No page CSS sets a shell width any more.

| | Before (this follow-up) | After |
|---|---|---|
| shell | 840px centred, unique to two pages | full width, same as every other list |
| heading | hero icon tile + subtitle + count pill | standard `<h5>` + icon + `(N)` |
| add form | always-open one-line bar | modal, opened by the page-head action |
| table | `expense-category-*` custom names | shared `hdc-table`; `hdc-rename-*` row hooks |
| Trades | its own duplicate of the same shell | same macro, same row component |

**Bugs fixed while aligning them**

* **Personal Expense Categories** — the heading row spread the title, the Back
  link and the Add button across the full page width (Back landed mid-page).
  Back and Add are now one right-aligned button group.
* **Allowance Categories** — the Add/Create buttons used `btn-purple` /
  `text-purple`, classes that exist in no stylesheet, so the Add button
  rendered unstyled. Both now use `btn-mint` and the shared page head.
* **Phone record cards hid every row action.** `responsive_tables.js` marked a
  cell `data-hdc-rt-empty` from its *text* alone; the Actions cell of a list
  row holds only icon buttons, so it was treated as blank and
  `responsive.css` (`td[data-hdc-rt-empty="1"] { display: none }`) removed all
  of View / Edit / Rename / Delete from every record card in the app. A cell
  now counts as empty only when it has neither text **nor** controls.

---

## 3. What changed

`templates/hdc/expenses/expense_categories.html` — rebuilt on the Trades idiom;
`static/hdc/css/hdc.css` — the blanket cap removed, the page block rewritten.

| | After (1440px) |
|---|---|
| shell | `max-width: 52.5rem` (840px) — **retired in §2b**: the page now uses the standard full-width list shell |
| hero | icon tile + title + "N active" pill (the heading row previously held one `<h5>` inside a `justify-content-between` with nothing to justify) |
| add card + table | identical 840px edges (both are children of the shell) |
| quick add | one flex line: uppercase label → field → button, `gap:10px`, wraps on phones |
| name field | `max-width: 320px` (was ≈537px) |
| row | read-only name; the rename field appears only while a row is being edited (one at a time, Escape/✕ cancels) |
| Actions column | `width: 110px` — two 32px square icon buttons, 4px apart (was 136px holding one) |
| index | 28px rounded chip in a 64px cell (was plain text in a 64px cell) |
| no-JS | `<noscript>` keeps the rename field visible, exactly like Trades |

Server behaviour is untouched: the same `add_category` / `edit_category` /
`delete_category` POSTs, the same `data-hdc-dialog` result dialogue, the same
`hdc_row_attrs` traceability attributes, the same `confirm()` before a delete.

---

## 4. How to reproduce the measurements

```bash
# capture a page (throwaway DB, no server)
python3 - <<'EOF'
import os, tempfile
tmp = tempfile.mkdtemp()
os.environ.update(HDC_ENV='test', HDC_SECRET_KEY='probe-only',
                  HDC_BOOTSTRAP_ADMIN_PASSWORD='Probe-Test-Only-123',
                  HDC_DB_PATH=tmp + '/p.db', HDC_INSTANCE_DIR=tmp)
from hdc.app import create_app
c = create_app({'TESTING': True}).test_client()
c.get('/hdc/login')
with c.session_transaction() as s: tok = s['_csrf_token']
c.post('/hdc/login', data={'username': 'admin', 'password': 'Probe-Test-Only-123',
                          '_csrf_token': tok})
open('/tmp/page.html', 'w').write(c.get('/hdc/expense_categories').get_data(as_text=True))
EOF

# which rule wins, at which viewport
python scripts/css_cascade_probe.py --html /tmp/page.html \
    --selector ".hdc-rename-actions" \
    --selector ".hdc-rename-form .form-control" \
    --selector ".hdc-rename-icon-btn" --width 1440 --width 390
```

`scripts/css_cascade_probe.py` resolves origin order, specificity, source order
and width media queries, and prints the winning declaration with `file:line`.
It does not run layout — text-dependent widths still need a browser
(`scripts/qa_browser.py`).

## 5. Verification

* `tests/test_list_page_consistency.py` — the four category lists (Expense,
  Trades, Allowance, Personal) must all import and call the shared page-head
  macro, use `card hdc-card` + `table hdc-table` + `.table-responsive`, open
  their add form in a modal, never re-introduce a retired workspace class, and
  never use a class no stylesheet defines. The rendered-page half checks the
  served HTML: exactly one page head inside `<main>`, the standard shell, and
  the shared row-rename hooks on both name lists.
* `tests/test_expense_batch.py` — add/rename/delete round trip plus a structural
  test: both row actions must live inside the Actions cell, and a row must not
  ship a live text box behind its name.
* `tests/test_expense_categories_js.py` + `tests/expense_categories_harness.js` —
  the real page under jsdom: every row starts read-only, the pencil opens the
  field focused and selected, one row at a time, Escape and ✕ restore the old
  name, Save stays inside the form, and both row buttons share the Actions cell.
  The harness now loads the same `core/row_rename.js` Trades loads, and a test
  asserts both templates carry the same hooks.
* `tests/test_responsive_layer.py` — the test that pinned the global 36rem cap is
  replaced by one that keeps the **shared** `.form-control`/`.form-select` rule
  free of any width cap, so the app-wide side effect cannot come back unnoticed.
* `python scripts/css_dom_audit.py` — `/hdc/expense_categories`: 1 table, 0
  unwrapped, 0 unlabelled controls, 0 icon buttons without an accessible name,
  0 hardcoded colours.
* Live check on a throwaway database: login → add → rename → delete, all three
  POSTs answer 302 and the list reflects the change.

The renamed row's server contract is unchanged, so nothing else in the app has
to know this page moved: the same POST actions, the same flash messages, the
same `hdc_row_attrs` row attributes.
