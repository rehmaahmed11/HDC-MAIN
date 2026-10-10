# UI ROOT-CAUSE AUDIT — CSS of every page, section, box, window, dialog box & searchable combo

**Date:** 2026-10-10 · **Branch:** `arena/c1e0e436-hdc-main`
**Scope:** all 125 rendered `GET /hdc/**` pages, 9 stylesheets (3,234 lines), 18 templates
with inline `<style>` blocks, every Bootstrap modal window (44 `.modal-content` blocks),
the result-dialog system (`core/dialog.js`), and the searchable combo widget
(`core/combo.js` + `shared/_combo_field.html`, 65 live combo inputs).

**How it was produced (reproducible):**

```bash
rm -rf /tmp/hdc_audit
HDC_DB_PATH=/tmp/hdc_audit/audit.db HDC_INSTANCE_DIR=/tmp/hdc_audit python3 scripts/seed_tools_demo.py
HDC_DB_PATH=/tmp/hdc_audit/audit.db HDC_INSTANCE_DIR=/tmp/hdc_audit python3 scripts/css_dom_audit.py
```

`scripts/css_dom_audit.py` logs in as admin and inspects the **real rendered DOM** of every
page — every number below is backed by that output. 75 of 125 pages come back clean;
the other 50 share the same handful of root causes.

---

## THE ROOT CAUSES — *why* the UI misbehaves

### Root cause 1 — Dark mode is split-brain: Bootstrap never hears about the theme

The app ships **Bootstrap 5.3.2**, which has its own native dark mode driven by
`data-bs-theme="dark"`. But `static/hdc/js/core/theme.js` only sets the **custom**
attribute:

```js
document.documentElement.setAttribute('data-theme', t);   // Bootstrap ignores this
```

`data-bs-theme` is set **nowhere** in the codebase (verified by grep across all templates
and JS). Consequence: every Bootstrap surface keeps its light-mode `--bs-*` variables in
dark mode unless `hdc.css` manually re-skins it. The app has been compensating with
piecemeal overrides (`.hdc-modal`, `[data-theme="dark"] .card`, `[data-theme="dark"] .table`,
`.btn-close { filter: invert(1) }` …) — and **every surface the patch list forgot is a bug**.
This single omission is the parent of root causes 2 and 3.

**Fix (one line + cleanup):** make `applyTheme()` also set
`document.documentElement.setAttribute('data-bs-theme', t)` and map the token palette onto
`--bs-body-bg`/`--bs-body-color`; the per-component patches then become redundant instead
of mandatory.

### Root cause 2 — Half the modal windows are missing the dark-mode patch class

Dialog windows are only themed when `.modal-content` also carries `hdc-modal`
(`hdc.css:832 — .hdc-modal { background: var(--modal-bg) !important; … }`).
Audit of all 44 `.modal-content` blocks: **22 have the class, 22 do not.** The 22 bare
ones render as white boxes with wrong borders in dark mode:

| Template | Bare modals |
|---|---|
| `accounts/accounts.html` | 4 (incl. edit-transaction + bank modals) |
| `accounts/_new_transaction_modals.html` | 3 (Add Account / Add Party / Add Project mini-modals) |
| `purchase/purchase_v2_stock.html` | 3 |
| `purchase/purchase_v2_delivered.html` | 3 |
| `purchase/purchase_v2_usage.html` | 2 |
| `accounts/cashflow_register.html` | 2 |
| `timekeeping/timekeeping.html`, `purchase_v2_suppliers.html`, `purchase_v2_supplier_detail.html`, `purchase_v2_purchases.html`, `purchase_v2_materials.html` | 1 each |

`new_transaction.css:237` styles `.hdc-mini-modal .modal-content` with **border and radius
only — no background token**, so the New-Transaction "Add new …" dialogs stay white too.
The result dialog (`core/dialog.js` → `.hdc-result-dialog`) is correctly tokenised and is
*not* affected.

### Root cause 3 — Bootstrap components used but never themed at all

Usage counted in templates vs. rules present in any stylesheet:

| Component | Used | Theme rules | Dark-mode state |
|---|---|---|---|
| `.page-link` / `.pagination` | 12 / 3 | **0** | white pager boxes |
| `.input-group-text` | 13 | **0** | light grey addon boxes |
| `.progress` | 6 | **0** | light track |
| `.list-group-item` | 3 | 1 (dark only) | partial |
| `.card` (Bootstrap) | 956 class refs | 1 dark rule | background patched, headers/footers not |

Note the app also has a **second, parallel card system** (`.hdc-card`/`.hdc-card-header`,
fully tokenised). Pages mix both, which is why some boxes follow the theme and the
neighbouring box doesn't.

### Root cause 4 — 35 hard-coded hex colours in inline `style=` attributes (16 pages)

These bypass the token system entirely and are invisible to both theme modes:

- `/hdc/accounts` → `#059669 #10b981 #ecfdf5 #f0fdf4` (the pale-green tints are the worst: near-invisible text in dark mode)
- `/hdc/accounts/hub`, `/manage`, `/new`, `/1/edit` → `#8b5cf6 #f59e0b #0891b2 #10b981 #fff`
- the whole `/hdc/accounts/shared/*` section → `#10b981 #3b82f6 #8b5cf6 #f59e0b #06b6d4`
- `/hdc/tool-rental/tool/1` → `#16a34a #7c3aed #f59e0b`
- `/hdc/projects`, `/hdc/stages` → `#dee2e6` borders; `/hdc/purchase-v2` → `#fff`

### Root cause 5 — CSS fragmentation: ~29% of page CSS lives outside the stylesheets

18 templates carry inline `<style>` blocks totalling **≈1,180 lines** (transaction_receipt 352,
expenses 124, trades 110, salary-cards print 103, project report print 81, …) plus ~200
scattered `style="…"` attributes (subcontractor_attendance 11, project_detail 11,
purchase_v2_purchases 9, accounts_entries 9 …). This is why "the same box" looks different
per page: section headers, filter bars and summary tiles are re-declared per template and
drift. Also note `accounts.css` is loaded globally from `base.html` on **every** page, so
its 384 lines leak outside the accounts section.

### Root cause 6 — The searchable-combo rule is implemented well but not finished

The widget itself is **sound** — this audit specifically re-verified the classic failure
modes and none apply:

- menu is appended to `<body>` with `position:fixed`, so **no card/table/modal
  `overflow:hidden` can clip it**;
- `z-index:10500` beats Bootstrap's modal (1055), so combos inside dialog windows layer correctly;
- scroll listener uses capture (`window.addEventListener('scroll', placeMenu, true)`),
  so the menu tracks the input even when a `modal-body` (`max-height + overflow-y:auto`)
  scrolls;
- all menu colours come from tokens (`var(--card-bg)` etc.), so it follows dark mode;
- `required` is mirrored from the hidden select to the visible input, and the widget
  degrades to a plain posting text box without JS.

What's wrong is **coverage** — pages that never got the widget:

**(a) Data-backed long `<select>`s still bare (operator must scroll):**

| Page | Select | Options |
|---|---|---|
| `/hdc/users` | two permission selects | **85 each** |
| `/hdc/accounts/cashflow/register`, `/money-center`, `/new-transaction` | `subcategory_id(s)` / material item | 38 |
| same three pages | `category_id` | 17 |
| `/hdc/purchase-v2/materials` | `unit` (create + edit modal) | 22 |
| `/hdc/accounts` | `txn_type` / `edit_txn_type` | 17 |
| `/hdc/accounts/shared*` | `category` filter | 21 |

(Enumerations like `txn_type`/`unit` are exempt under the documented rule, but the
**38-option subcategory/material lists and the 85-option selects on /hdc/users are
data-backed records and violate it**.)

**(b) Genuine name-selection fields still plain text (should be combos):**

- `/hdc/accounts` — `party_name` (main form **and** edit-transaction modal), `bank_name`
- `/hdc/accounts/new` + `/hdc/accounts/1/edit` — `linked_party_name`, `bank_name`
- `/hdc/purchase-v2/delivered` + `/stock` — `delivery_person` (edit modals; the create form already has a combo)
- `/hdc/tool-rental/audit/count` — `counter_name` (site in-charge, recurring person)
- `/hdc/subcontractors` — name filter input

(The DOM scan flags 80 fields in total, but most are *correct* per the rule's exemptions —
fields that define/rename a master record (`trades`, `expense_categories`, material/supplier
create forms), phones, amounts, and free-text search filters. The list above is the
curated set of real violations.)

**(c) One combo accessibility gap:** `transferToolSearchSelect` on `/hdc/tool-rental/new`
has no label/aria-label.

### Root cause 7 — Accessibility debt on boxes and buttons

- **65 icon-only buttons with no accessible name** (`/hdc/tool-rental/inventory` alone has 32;
  accounts 4, office-management 3, purchase stock 3, …) — invisible to screen readers,
  no tooltip.
- **15 unlabelled form controls** (`entriesPartyName`, `partyName`/`party_id` on parties,
  retention/rate fields on project detail & stage add, `keep_latest` on settings, …).
- **4 tables not wrapped in `.table-responsive`** → horizontal overflow on phones:
  `/hdc/accounts/shared/report` (2) and `/hdc/reports/project/1/pdf` (2 — print view, low priority).

---

## Section-by-section summary

| Section | Pages | State of boxes / windows / dialogs / combos |
|---|---|---|
| Dashboard, Reports, Settings | 6 | clean except 1 unnamed icon button (reports), `keep_latest` unlabelled |
| Accounts (core) | 14 | worst section: 22 un-themed dialog boxes concentrated here + purchase; pale-green hex tints; `party_name`/`bank_name` plain; 17/38-option bare selects |
| New Transaction / Money Center / Cashflow Register | 8 | mini-modals white in dark mode; 38-option subcategory selects bare; otherwise strong combo usage |
| Shared Expenses | 7 | heaviest hex offender (every page 1–4 hard-coded colours); 21-option category filter bare |
| Purchase v2 | 8 | 11 bare modal windows; `delivery_person` edit fields plain; unnamed icon buttons on every list page |
| Projects / Stages / Estimation | 10 | `#dee2e6` hex borders; unlabelled rate/retention inputs; stage pickers correctly combo'd |
| Tools / Rental | 15 | combos widely adopted (best section for the rule); 32 unnamed icon buttons on inventory; `counter_name` plain; 1 unlabelled combo |
| Workers / Timekeeping / Payroll / Subcontractors | 15 | combos adopted; worker/trade create fields correctly plain (they define names); 1 bare timekeeping modal |
| Office / Personal / Parties / Users | 12 | users page has the two 85-option selects; party filter unlabelled |
| Auth / prints / exports / APIs | 30 | clean (print styles intentionally inline & hex — acceptable for paper) |

---

## Recommended fix order (highest leverage first)

1. **One-line theme bridge:** set `data-bs-theme` alongside `data-theme` in `theme.js`
   (+ map `--bs-body-bg`/`--bs-body-color` to the tokens). Kills the whole class of
   "white box in dark mode" bugs at the source.
2. **Normalize dialogs:** add `hdc-modal` to the 22 bare `.modal-content` blocks (or make
   the rule generic: `.modal-content { background: var(--modal-bg); … }`), and give
   `.hdc-mini-modal .modal-content` a background token.
3. **Theme the forgotten components:** `.page-link`, `.input-group-text`, `.progress`,
   list-group hover states — ~20 lines in `hdc.css`.
4. **Replace the 35 inline hex colours** with the existing tokens / utility classes.
5. **Finish the combo rule:** wire `combo_select` onto the 85-option users selects, the
   38-option subcategory/material selects, and convert the curated plain name fields in 6(b).
6. **Name the 65 icon buttons** (`aria-label` + `title`) and label the 15 bare controls.
7. **(Longer term)** migrate the 18 inline `<style>` blocks into the per-page stylesheets
   and collapse `.card` vs `.hdc-card` into one card system.

---

## Appendix — per-page findings (all 125 rendered pages)

*"Combos" = live searchable combo inputs found in the rendered DOM. "clean" = none of the
six automated checks fired. Plain-name flags here are raw scanner output — see §6(b) for
which ones are real violations vs. rule-exempt.*

| Page | Combos | Findings |
|---|---|---|
| `/hdc/` | — | clean |
| `/hdc/access` | — | clean |
| `/hdc/accounts` | 11 | 4 unnamed icon button(s); hex: #059669 #10b981 #ecfdf5 #f0fdf4; long selects: type(17), type(17); plain name fields: bank_name, party_name |
| `/hdc/accounts/1/edit` | — | hex: #0891b2 #8b5cf6; plain name fields: bank_name, linked_party_name, name |
| `/hdc/accounts/1/ledger` | — | clean |
| `/hdc/accounts/cashflow` | — | plain name fields: party_name |
| `/hdc/accounts/cashflow/export` | — | clean |
| `/hdc/accounts/cashflow/reconciliation` | — | clean |
| `/hdc/accounts/cashflow/register` | 4 | long selects: category_id(17), subcategory_ids(38), subcategory_id(38), category_id(17) |
| `/hdc/accounts/cashflow/register/export` | — | clean |
| `/hdc/accounts/cashflow/report` | — | plain name fields: party_name |
| `/hdc/accounts/entries` | 2 | unlabelled: entriesPartyName; long selects: transaction_type(16) |
| `/hdc/accounts/hub` | — | hex: #10b981 #8b5cf6 #fff |
| `/hdc/accounts/manage` | — | hex: #8b5cf6 #f59e0b |
| `/hdc/accounts/manage/export` | — | clean |
| `/hdc/accounts/money-center` | 4 | long selects: category_id(17), subcategory_ids(38), subcategory_id(38) |
| `/hdc/accounts/money-center/api/accounts` | — | clean |
| `/hdc/accounts/money-center/api/diagram` | — | clean |
| `/hdc/accounts/money-center/api/entry-config` | — | clean |
| `/hdc/accounts/money-center/api/flows` | — | clean |
| `/hdc/accounts/money-center/api/kpis` | — | clean |
| `/hdc/accounts/money-center/api/pending` | — | clean |
| `/hdc/accounts/new` | — | hex: #0891b2 #8b5cf6; plain name fields: bank_name, linked_party_name, name |
| `/hdc/accounts/new-transaction` | 4 | long selects: category_id(17), subcategory_ids(38), subcategory_id(38) |
| `/hdc/accounts/new-transaction/subcategories` | — | clean |
| `/hdc/accounts/reconciliation` | — | clean |
| `/hdc/accounts/shared` | — | hex: #10b981 #3b82f6 #f59e0b; long selects: category(21) |
| `/hdc/accounts/shared/expenses` | — | hex: #8b5cf6; long selects: category(21) |
| `/hdc/accounts/shared/expenses.csv` | — | clean |
| `/hdc/accounts/shared/expenses/new` | — | hex: #10b981 #3b82f6 #8b5cf6; plain name fields: amount_party_1, amount_party_2, amount_party_3, new_party_name, percent_party_1, percent_party_2, percent_party_3 |
| `/hdc/accounts/shared/parties` | — | hex: #10b981 #8b5cf6; plain name fields: name |
| `/hdc/accounts/shared/report` | — | 2 unwrapped table(s); hex: #06b6d4 #10b981 #8b5cf6 #f59e0b; long selects: category(21) |
| `/hdc/accounts/shared/report.csv` | — | clean |
| `/hdc/accounts/shared/settlements` | — | hex: #10b981 #8b5cf6 |
| `/hdc/accounts/transactions/find` | — | clean |
| `/hdc/alerts` | — | 1 unnamed icon button(s) |
| `/hdc/api/expense_categories` | — | clean |
| `/hdc/api/next_office_staff_code` | — | clean |
| `/hdc/api/next_project_code` | — | clean |
| `/hdc/api/next_worker_code` | — | clean |
| `/hdc/api/office_expense_categories` | — | clean |
| `/hdc/api/office_staff` | — | clean |
| `/hdc/api/project_stages/1` | — | clean |
| `/hdc/api/row_actors` | — | clean |
| `/hdc/api/subcontractors` | — | clean |
| `/hdc/api/suppliers` | — | clean |
| `/hdc/api/tool-rental/audit` | — | clean |
| `/hdc/api/tool-rental/dashboard` | — | clean |
| `/hdc/api/tool-rental/receiving-accounts` | — | clean |
| `/hdc/api/tool-rental/rental/1/pending-items` | — | clean |
| `/hdc/api/tool-rental/serials/in-store` | — | clean |
| `/hdc/api/tool-rental/serials/inventory-summary` | — | clean |
| `/hdc/api/tool-rental/serials/out-of-store` | — | clean |
| `/hdc/api/tool-rental/serials/tool/1/available` | — | clean |
| `/hdc/api/tool-rental/tools-available` | — | clean |
| `/hdc/api/tool-rental/tools/1/summary` | — | clean |
| `/hdc/api/workers` | — | clean |
| `/hdc/attendance` | — | clean |
| `/hdc/estimation` | — | clean |
| `/hdc/estimation/formulas` | — | plain name fields: name |
| `/hdc/event-recorder` | — | clean |
| `/hdc/expense_categories` | — | unlabelled: category_name; plain name fields: category_name |
| `/hdc/expenses` | — | clean |
| `/hdc/login` | — | clean |
| `/hdc/materials` | — | clean |
| `/hdc/materials/usage` | — | clean |
| `/hdc/office-management` | — | 3 unnamed icon button(s); plain name fields: name |
| `/hdc/office-management/allowance-categories` | — | 1 unnamed icon button(s); plain name fields: name |
| `/hdc/office-management/expenses` | 1 | clean |
| `/hdc/office-management/staff` | — | clean |
| `/hdc/office-management/staff/attendance` | — | clean |
| `/hdc/office-management/staff/ledger` | — | unlabelled: staff_code; 2 unnamed icon button(s); plain name fields: name, staff_code |
| `/hdc/parties` | 2 | unlabelled: partyName, party_id |
| `/hdc/parties/1` | — | clean |
| `/hdc/payroll` | — | 1 unnamed icon button(s) |
| `/hdc/payroll/generate` | — | clean |
| `/hdc/payroll/history` | — | clean |
| `/hdc/payroll/salary-cards` | — | clean |
| `/hdc/personal-management` | — | clean |
| `/hdc/personal-management/categories` | — | 1 unnamed icon button(s); plain name fields: name |
| `/hdc/personal-management/expenses` | — | clean |
| `/hdc/project-estimation` | — | clean |
| `/hdc/projects` | — | hex: #dee2e6 |
| `/hdc/projects/1` | — | unlabelled: date, lump_sum_amount, rate_per_sqft, retention_pct; 2 unnamed icon button(s); plain name fields: name |
| `/hdc/projects/1/edit` | — | plain name fields: client, client_phone |
| `/hdc/projects/1/stage/add` | 1 | unlabelled: sub_lump_sum_amount, sub_rate_per_sqft, sub_retention_pct |
| `/hdc/projects/add` | — | plain name fields: client, client_phone, name |
| `/hdc/purchase-v2` | — | hex: #fff |
| `/hdc/purchase-v2/delivered` | 1 | 2 unnamed icon button(s); plain name fields: delivery_person |
| `/hdc/purchase-v2/materials` | — | 1 unnamed icon button(s); long selects: unit(22), unit(22); plain name fields: name |
| `/hdc/purchase-v2/purchases` | — | 1 unnamed icon button(s) |
| `/hdc/purchase-v2/stock` | 3 | 3 unnamed icon button(s); plain name fields: delivery_person |
| `/hdc/purchase-v2/suppliers` | — | 2 unnamed icon button(s); plain name fields: name |
| `/hdc/purchase-v2/usage` | 1 | 2 unnamed icon button(s) |
| `/hdc/purchases` | — | clean |
| `/hdc/reports` | — | 1 unnamed icon button(s) |
| `/hdc/reports/export/materials` | — | clean |
| `/hdc/reports/export/profitability` | — | clean |
| `/hdc/reports/export/salary` | — | clean |
| `/hdc/reports/glance` | — | clean |
| `/hdc/reports/project/1/csv` | — | clean |
| `/hdc/reports/project/1/pdf` | — | 2 unwrapped table(s); hex: #f0f0f0 |
| `/hdc/reports/project/1/xlsx` | — | clean |
| `/hdc/settings` | — | unlabelled: keep_latest |
| `/hdc/stage-library` | — | clean |
| `/hdc/stages` | — | 1 unnamed icon button(s); hex: #dee2e6 |
| `/hdc/subcontractors` | 1 | plain name fields: name |
| `/hdc/timekeeping` | — | clean |
| `/hdc/timekeeping/status` | — | clean |
| `/hdc/tool-rental` | 1 | clean |
| `/hdc/tool-rental/1` | 5 | clean |
| `/hdc/tool-rental/audit` | — | clean |
| `/hdc/tool-rental/audit/count` | — | plain name fields: counter_name |
| `/hdc/tool-rental/dashboard` | — | clean |
| `/hdc/tool-rental/inventory` | 6 | 32 unnamed icon button(s); plain name fields: category_name, name |
| `/hdc/tool-rental/inventory/1/serials/create` | — | clean |
| `/hdc/tool-rental/new` | 10 | unlabelled: transferToolSearchSelect; 1 unnamed icon button(s); plain name fields: customer_address, customer_phone |
| `/hdc/tool-rental/reports` | 3 | clean |
| `/hdc/tool-rental/serials` | 1 | clean |
| `/hdc/tool-rental/tool/1` | 1 | 2 unnamed icon button(s); hex: #16a34a #7c3aed #f59e0b |
| `/hdc/tool-rental/tracking` | 2 | clean |
| `/hdc/trades` | — | unlabelled: trade_name; plain name fields: trade_name |
| `/hdc/users` | — | long selects: (85), (85); plain name fields: username |
| `/hdc/users/access-data` | — | clean |
| `/hdc/workers` | 1 | 2 unnamed icon button(s); plain name fields: name, worker_code |


---

# PASS 2 — Window sizing, field placement & CSS garbage removal (applied)

**Date:** 2026-10-10 · verified by re-rendering all 125 pages + 38 frontend/smoke tests passing.

## 1. Window (dialog) sizing & placement — audited all 44 modals

Scan: every `modal-dialog` size class vs. the fields/tables inside it, plus
centering and scroll behaviour.

**Found:**
- 33 of 44 windows were **not vertically centred** (stuck to top of viewport) while 11 were — inconsistent placement.
- Form-heavy windows (`accountCreateModal` 7 fields, tool `editModal` 11 fields, worker modals 7–8 fields) had **no scroll guard**: on short screens the footer buttons went off-screen.
- Two windows had a **2-column field grid squeezed into the 500 px default width**:
  `tool_inventory editModal` (11 fields, `col-6/col-8` grid) and the reusable
  Add-Stock purchase modal in `_stock_forms.html` (9 fields, `col-6` grid).
- Field placement inside page forms is largely healthy (CSS grid in New Transaction,
  `col-md` wrappers elsewhere). `allocationModal`'s "15 fields" flag was a scanner
  false positive (4 real fields) — left unchanged.

**Fixed — a global "intelligent window system" block in `hdc.css`:**
1. **Every** dialog is now vertically centred (equivalent of `modal-dialog-centered`, app-wide, no per-template class needed).
2. Non-scrollable dialog bodies are capped at `72vh` with their own scrollbar — a window can never run off-screen; header + action buttons always stay visible.
3. **All** `.modal-content` now themed from design tokens (`--modal-bg`, `--card-border`, `--text`) — this also retires the Pass-1 problem of the 22 dialogs missing the `hdc-modal` patch class (kept as a harmless alias).
4. `tool_inventory editModal` and `_stock_forms` purchase modal upgraded to `modal-lg` so their 2-column grids have room.

## 2. CSS clutter & garbage removed

Cross-referenced all 455 class selectors in the 9 stylesheets against every
template, JS file and Python route/service (including dynamically built names
like `se-{{ tone }}`, `kpi-slate` from `parties.py` — those were kept).

**Deleted (provably dead, ~110 lines):**
- `accounts.css`: entire `.acc-guide*` block (5 selectors — old hub guide cards).
- `money_center.css`: legacy "today's numbers" KPI block — `.rm-kpis`, `.rm-kpi`,
  `.rm-kpi-label`, `.rm-kpi-value`, `.rm-kpi-sub` + in/out/dark variants,
  `.rm-panel-total`, `.rm-note`, and the orphaned media-query line.
- `hdc.css`: `.sidebar-group-label` (+`::after`) — superseded by `.nav-group-label`;
  dead `div[data-hdc-void="1"].hdc-void-row` selector.
- `new_transaction.css`: `.hdc-txn-fieldset[hidden]`, `.hdc-txn-more-summary-note`.

**Checked and deliberately kept:**
- `se-rose` / `se-emerald` — built dynamically via `se-{{ balance.tone }}`.
- `acc-badge-blue/cyan`, `kpi-slate`, `is-*`, `has-*` — referenced from templates/Python.
- Apparent "duplicate selectors" (`#sidebar` ×4, `.modal-dialog` ×2 …) — media-query responsive variants, not duplicates.
- `accounts.css` global load in `base.html` — it is the shared section-layout
  system for both Accounts **and** Shared Expenses pages; not garbage.

## 3. Verification
- `scripts/css_dom_audit.py` re-run: all **125 pages render** with no new flags.
- CSS brace balance check: all 9 files clean.
- `pytest`: template hygiene, offline assets, combo widget, name-combo fields,
  frontend config contract, smoke + sidebar navigation — **38 tests passed**.

**Net diff:** 6 files, +45 / −115 lines (CSS got smaller while gaining the global window system).
