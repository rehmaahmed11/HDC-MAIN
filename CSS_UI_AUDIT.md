# CSS / UI AUDIT — HDC ERP

**Scope:** every section, every sub-section, every form and every page of the
Flask app in `hdc/` + `templates/hdc/` + `static/hdc/`.
**Audited:** 112 templates, 231 `<form>` elements (159 of them POST), 7
stylesheets (2 816 lines / 78.7 KB), 13 page/core scripts, 104 rendered pages.
**Date:** 2026-09-29 · **Branch:** `arena/01a0edbd-hdc-main`

---

## 0. How this audit was produced (reproducible)

```bash
# 1. seed a demo database so list pages have rows
HDC_DB_PATH=/tmp/hdc_audit/audit.db HDC_INSTANCE_DIR=/tmp/hdc_audit \
    python3 scripts/seed_tools_demo.py

# 2. render every GET /hdc page as admin and inspect the real DOM
HDC_DB_PATH=/tmp/hdc_audit/audit.db python3 scripts/css_dom_audit.py
```

`scripts/css_dom_audit.py` (new, added by this audit) logs in, resolves every
`GET /hdc/**` route to a concrete URL from seeded data, and reports per page:

| Check | What it catches |
|---|---|
| tables not inside `.table-responsive` | horizontal overflow on phones |
| controls with no label / placeholder / aria | unlabelled inputs |
| icon-only buttons with no accessible name | unnamed buttons |
| inline `style="…#hex"` | colours that break dark mode |
| `<select>` with > 12 options | long dropdowns that should be searchable |
| free-text **name** fields with no combo input | violations of the rule in §1 |

The JSON it prints is the evidence behind every number in this document.

---

## 1. THE RULE — every name field is a searchable combo box

> **Rule.** If the operator must choose an existing data-backed name — a
> customer/client, supplier, party, person, account, site/project, stage, tool,
> category, rental source, or any other named record — render a **type-to-search
> combo box** (visible text input + hidden source `<select>`, wired by
> `static/hdc/js/core/combo.js`). Never leave a long data-backed name list as a
> bare `<select>` that the operator must scroll.
>
> Use a **strict select-valued combo** (`combo_select`) when choosing from
> closed-set data: the hidden select owns `name=` and posts the chosen option
> value (normally a record id; some location pickers use a composite key), and
> typing without selecting a real option is not a choice. Use a **name-valued combo**
> (`combo_field`) for recurring customer/supplier names that may be new: the
> visible input owns `name=` and still posts a newly typed name. Enumerations,
> dates, quantities, free-text search queries, and fields that define/rename a
> master record are not pickers and stay native/plain text as appropriate.

### 1.1 Why

The tools section was the clearest offender, exactly as reported: the customer
name on a new rental and the "To Customer Name" on a site transfer were free
text with no way to see who this shop has ever rented to. Operators retyped
`Bilal Traders`, `bilal traders`, `Bilal Traders ` and `Bilal Trader` into
four different strings, which then flowed into Accounts as four different
customer ledgers. The same happened with tool suppliers.

### 1.2 What now exists to enforce it

| Piece | Role |
|---|---|
| `templates/hdc/shared/_combo_field.html` | `combo_field()` (name-valued) and strict `combo_select()` (closed-set value) macros — one place that renders the pair correctly |
| `static/hdc/js/core/combo.js` | `HDCComboList` widget **plus** auto-wire: `input[data-hdc-combo]` is wired on `DOMContentLoaded`; strict record/key selections, manual repeated pickers, dependent hidden options, and programmatic select updates are supported |
| `templates/hdc/shared/base.html` | `<noscript>` fallback: strict combos show their named `<select>`; name-valued combos keep their named text input visible |
| `hdc/services/tool_rental.py` | `known_tool_customers()` / `known_tool_suppliers()` — the distinct names the combo offers |
| `tests/test_name_combo_fields.py` | Regression tests keep Tools name/id pickers searchable and verify their posted values |

### 1.3 Fixed in this change (HDC Tools)

| Page | Field | Before | After |
|---|---|---|---|
| `/hdc/tool-rental/new` — Create New Rental | `customer_name` | bare `<input type="text">` | searchable combo over every customer ever rented to |
| `/hdc/tool-rental/<id>` — Move Tools (transfer) | `to_customer_name` | bare `<input type="text">` | searchable combo, same list |
| `/hdc/tool-rental/<id>` — Submit Return | `received_to_account_id` | bare `<select>` (scroll a full account list) | searchable combo, still posts the account **id** |
| `/hdc/tool-rental/<id>` — Add Payment Only | `received_to_account_id` | bare `<select>` | searchable combo |
| `/hdc/tool-rental/inventory` — Add New Tool → Opening purchase | `supplier` | bare `<input type="text">` | searchable combo (tool purchases ∪ materials supplier master) |
| `/hdc/tool-rental/tool/<id>` — Add Stock modal | `supplier` | bare `<input type="text">` | searchable combo |

`customer_name` and `to_customer_name` share one list
(`known_tool_customers()`), so the transfer target is the same spelling the
rental was created with.

### 1.3.1 HDC Tools data-backed pickers (rule reaffirmed 2026-10-05)

Apply the same searchable picker anywhere the Tools section selects an
existing name/id, including filters and modal forms—not just create forms:

* **Rental create:** source rental, site/project (option label includes its
  client), stage, tool lines, receiving account, and the existing/new customer
  name field. The transfer flow also exposes its dynamic multi-select tool
  rows through the shared combo so a picked suggestion checks the matching row;
  per-tool quantities/rates remain on those rows.
* **Rental detail / transfer:** destination site/project, dependent stage,
  customer, and receiving accounts.
* **Inventory / stock:** category and supplier pickers plus tool selection in
  Add Stock and Scrap dialogs. The repeated tool line on New Rental is also a
  strict combo, including rows added dynamically.
* **Tracking / Reports / Rentals search:** tool and site filters, plus customer
  suggestions in the rental-code/customer search fields.

Keep enum choices (billing, status, condition, payment mode), dates, quantities,
and master-data definition fields (for example the new Tool Name input) as
ordinary controls. Existing-record id pickers use `combo_select()`; a typed
client/customer/supplier name that is allowed to create a new name uses
`combo_field()`.

### 1.4 Compliance matrix — the rest of the app

Legend: ✅ searchable combo · ⚠️ plain `<select>` (long list, scrollable) ·
❌ free-text name box · ➖ not a "pick an existing name" field (see 1.5)

| Section / page | Name fields | State |
|---|---|---|
| `/hdc/accounts` (hub txn form) | from/to account, project, stage, related entity | ✅ 11 combos |
| `/hdc/accounts` (edit txn modal) | from/to account, project, stage, related | ✅ |
| `/hdc/accounts` — `party_name` (off-ledger party) | free text | ❌ **fix** |
| `/hdc/accounts` — `bank_name` | free text | ➖ (attribute of the account) |
| `/hdc/accounts/new`, `/hdc/accounts/<id>/edit` | account `name`, `linked_party_name` | ❌ **fix** (linked party should be a party combo) |
| `/hdc/accounts/new-transaction`, `/hdc/accounts/cashflow/register` | account, party, project | ✅ 4 combos |
| `/hdc/accounts/entries` | worker | ✅ · `party_name` ❌ **fix** |
| `/hdc/accounts/cashflow`, `/hdc/accounts/cashflow/report` | `party_name` | ❌ **fix** |
| `/hdc/accounts/money-center` | 6 combos | ✅ · `mc_party_name` ❌ **fix** |
| `/hdc/accounts/shared/parties` | party `name` (create/edit/rename rows) | ❌ **fix** |
| `/hdc/accounts/shared/expenses/new` | `new_party_name`, split rows | ❌ **fix** |
| `/hdc/accounts/shared/expenses`, `/ledger`, `/report`, `/settlements` | `party_id`, `from/to_party_id`, `category` | ⚠️ **fix** |
| `/hdc/tool-rental` | customer search suggestions | ✅ |
| `/hdc/tool-rental/new` | source rental, site/client, stage, tool, customer, receiving account | ✅ (expanded 2026-10-05) |
| `/hdc/tool-rental/<id>` | destination site/stage/customer, receiving accounts | ✅ (expanded 2026-10-05) |
| `/hdc/tool-rental/inventory`, `/tool/<id>` | category, tool, supplier pickers | ✅ |
| `/hdc/tool-rental/tracking`, `/reports` | site/tool filters, customer suggestions | ✅ (expanded 2026-10-05) |
| `/hdc/workers`, `/hdc/workers/<id>/*` | worker picker | ✅ · create/edit `name`, `worker_code` ➖ (that row *is* the new worker) |
| `/hdc/subcontractors` | filter | ✅ · create/edit `name` ➖ |
| `/hdc/subcontractors/<id>/attendance` | worker select, worker type | ⚠️ **fix** |
| `/hdc/payroll/salary-cards` | worker filter | ✅ |
| `/hdc/timekeeping`, `/hdc/attendance` | project / stage / worker filters | ✅ |
| `/hdc/office-management/staff/*` | staff pickers, ledger `name`/`staff_code` | ⚠️/❌ **fix** |
| `/hdc/purchase-v2/suppliers` | supplier `name` (create/edit) | ❌ **fix** (rename should offer existing names) |
| `/hdc/purchase-v2/purchases` | `supplier_id` | ⚠️ **fix** |
| `/hdc/purchase-v2/delivered`, `/stock` | `delivery_person` | ❌ **fix** (recurring drivers/transporters) |
| `/hdc/purchase-v2/materials` | material `name` | ➖ (defines a new material) |
| `/hdc/projects/add`, `/edit` | `client`, `client_phone` | ❌ **fix** — a client is a returning name |
| `/hdc/projects/<id>/stage/add` | stage `name` | ➖ (defines a new stage) |
| `/hdc/trades`, `/hdc/expense_categories`, `/hdc/estimation/formulas`, `/hdc/personal-management/categories`, `/hdc/office-management/allowance-categories`, `/hdc/tool-rental/inventory` category/tool `name` | master-data names | ➖ (defines a new row) |
| `/hdc/users` | `username` | ➖ (defines a new user) |

**Original DOM-audit totals (2026-09-29; historical baseline):** 44 combo
inputs across 16 pages — up from 38 across 13 before that audit (the six
pairs were `rentalCustomerName`, `transferCustomerName`, `recvAccReturn`,
`recvAccPayment`, `toolSupplier`, `purchaseModalSupplier`). These figures
predate the HDC Tools selector expansion in §1.3.1 and should not be treated as
the current combo count. The original estimate was 81 name-ish fields still
without a combo, of which ≈ 25 were rule violations; that count also needs a
fresh DOM audit after the expansion.

### 1.5 Deliberate exceptions (do not "fix" these)

A combo is only right when the field *references* an existing name. When the
field **defines** a new row, a searchable list actively hurts:

* `workers.name`, `subcontractors.name`, `suppliers.name`, `materials.name`,
  `tool.name`, `trades.trade_name`, `expense_categories.category_name`,
  `allowance_categories.name`, `projects.name`, `stage.name`,
  `users.username` — the value being typed **is** the new record.
* `accounts.bank_name`, `accounts.account_number` — attributes, not names.
* `party_name` on an off-ledger row (e.g. "Petrol", "Donation") — one-off
  purpose text, not a party. Only `linked_party_name` should be a combo.

---

## 2. CSS architecture findings

### A1 — `accounts.css` (11.3 KB) is loaded on **every** page
`templates/hdc/shared/base.html` links `accounts.css` globally, but only 6 of
112 templates are accounts pages. Every dashboard, tools and timekeeping view
downloads and parses 11 KB of accounts CSS it never uses.
**Fix:** move the `<link>` out of `base.html` into the accounts templates
(next to the existing `accounts_workspace.css` / `accounts_entries.css`
links).

### A2 — 924 lines of CSS live inside templates
15 templates carry `<style>` blocks, the largest being
`accounts/transaction_receipt.html` (352 lines), `workers/trades.html` (110),
`payroll/payroll_salary_cards_print.html` (103),
`projects/project_report_print.html` (81), `expenses/expenses.html` (124).
They are uncacheable, unreviewable in a stylesheet diff, and invisible to any
CSS tooling.
**Fix:** extract to `static/hdc/css/<page>.css` and link them, the way
`accounts_workspace.css` was already extracted (audit 7.3).

### A3 — `.btn-mini` is defined twice with different values
`accounts_workspace.css:15` → `padding .1rem .32rem; font-size .68rem`
`accounts_entries.css:26` → `padding .08rem .3rem; font-size .67rem`
They never collide today only because the two stylesheets are loaded on
different pages. One day someone loads both and gets whichever comes last.
**Fix:** one definition in `hdc.css`.

### A4 — `shared_expenses.css` restates chrome selectors
It re-declares `body`, `#sidebar`, `#page-content`, `.topbar`,
`.sidebar-backdrop` (and `.acc-filter-bar`, `.acc-section` from
`accounts.css`). Any change to the shell now has to be made in three files.
**Fix:** delete the duplicates; the shell belongs to `hdc.css` only.

### A5 — no cache-busting on the two global stylesheets
`hdc.css` and `accounts.css` are linked without `?v=`, while
`shared_expenses.css` correctly uses `?v={{ se_asset_stamp }}`. After a deploy,
operators keep the old CSS until a hard refresh.
**Fix:** add an asset stamp helper and use it for every stylesheet.

---

## 3. Theming / dark-mode findings

### T1 — 354 hardcoded hex colours in templates, 63 of them in live inline styles
The worst pages: `/hdc/accounts/hub` (44 inline styles, 15 distinct hex),
`/hdc/accounts/money-center` (63 / 11), `/hdc/tool-rental/dashboard` (50 / 3),
`/hdc/tool-rental/reports` (33 / 2), `/hdc/accounts` (8 / 4).
Example: `style="background:#10b981"` on the hub, and
`style="background:#7c3aed"` for the "customers" segment on the tools
dashboard. In dark mode these stay saturated light-theme colours against a
`#0f172a` card — the KPI gradient cards look fine, the inline chips do not.
**Fix:** add tokens (`--chip-own`, `--chip-customer`, `--chip-store`,
`--pos-*`, `--neg-*`) to `:root` / `[data-theme="dark"]` in `hdc.css` and
reference them.

### T2 — the combo menu had hardcoded cream colours
`combo.js` injected `#fff3e0` / `#f3b66d` / `#87af32` and switched to `#2b2b2b`
for dark. **Fixed in this change:** every colour now comes from
`var(--card-bg)`, `var(--card-border)`, `var(--accent)`, `var(--text)`,
`var(--text-muted)`, `var(--shadow-card)`, so the menu is a real themed
surface in both modes.

### T3 — `--void-row-bg: #d8d8d8` is the one intentional light-only token
Fine as-is (it is a deliberate grey-out), but it should be documented next to
the token so nobody "fixes" it into a translucent dark value.

### T4 — no `color-scheme` declaration
`<html data-theme="dark">` never sets `color-scheme: dark`, so native form
controls, scrollbars and the date/number spinners stay light-themed in dark
mode.
**Fix:** `[data-theme="dark"] { color-scheme: dark; }`.

---

## 4. Layout & responsive findings

### R1 — 5 tables are not inside `.table-responsive`
`/hdc/accounts/hub` (1 of 2), `/hdc/accounts/shared/report` (2 of 4),
`/hdc/reports/project/<id>/pdf` (2 of 2 — a print page, lower priority).
On a 375 px phone these tables push the whole page sideways.
**Fix:** wrap in `<div class="table-responsive">`.

### R2 — only three breakpoints, and no tablet tier
`hdc.css` has `@media (max-width: 992px)` and `(max-width: 576px)` only;
`accounts.css` adds `768px`, `accounts_entries.css` `1400px`. There is no
`768–991px` tier, which is exactly where the 4-column KPI strips and the
side-by-side tools forms (rentals hub = filters + list + create form) get
uncomfortable.
**Fix:** add a `@media (max-width: 991px)` tier that stacks the tools
`col-md-4` columns and the accounts hub.

### R3 — `#sidebar` uses `overflow-x: hidden` but nothing else does
Only the sidebar clips overflow. A wide table inside a `.hdc-card` therefore
spills out of the card's rounded border instead of scrolling — which is why R1
matters more than usual here.

### R4 — dense tables go down to `.58rem` (≈ 9.3 px)
`.badge-mini` `.58rem`, `.btn-mini` `.67rem`, `.entries-compact .small`
`.68rem`, `.hdc-table thead th` `.74rem`. On a 1080p laptop screen this is
below the comfortable reading floor for the operators who use these grids all
day.
**Fix:** raise the floor to `.72rem` and recover density from padding, not
font size.

### R5 — `backdrop-filter: blur(8px)` on `.hdc-card`
Applied to *every* card. `/hdc/tool-rental/tracking` renders 23 tables and
`/hdc/reports/glance` 7; with hundreds of cards this is a real paint cost on
low-end machines, and it also makes text slightly soft.
**Fix:** drop the blur from `.hdc-card` (keep the translucent background) or
gate it behind `@supports` + a `prefers-reduced-transparency` query.

---

## 5. Accessibility findings

### X1 — 73 icon-only buttons have no accessible name
35 of them are `<button class="btn-close">` (Bootstrap renders a bare ×, so a
screen reader announces only "button") and only 6 of the 41 `btn-close`
buttons in the app carry an `aria-label`. The rest are edit/delete/view icons.
Concentrated on `/hdc/tool-rental/inventory` (31, one per tool-row modal) and
`/hdc/tool-rental/tracking` (11), plus scattered icons on purchase-v2,
office-management, projects, workers, accounts and reports.
**Fix:** `aria-label="Close"` / `title="Edit tool"` on each. One global rule
cannot fix this — the name has to say *what* the button does.

### X3 — 11 form controls have no label, placeholder or aria
`/hdc/projects/<id>` (4: `date`, `lump_sum_amount`, `rate_per_sqft`,
`retention_pct`), `/hdc/projects/<id>/stage/add` (3),
`/hdc/office-management/staff/ledger` (`staff_code`),
`/hdc/expense_categories` (`category_name`), `/hdc/trades`
(`trade_name`), `/hdc/settings` (`keep_latest`). These are almost all
edit-modal inputs whose label lives in a sibling `<div>` that the parser
cannot associate.
**Fix:** give the input an `id` and the label a `for=`.

### X4 — `:focus-visible` is styled for the sidebar and nothing else
6 rules in `hdc.css` + 1 in `new_transaction.css`. Form controls rely on
Bootstrap's `:focus` box-shadow, which is fine, but custom controls
(`.btn-mini`, `.tools-chip`, row-click handlers, `.kpi-card` links) have no
visible keyboard focus at all.
**Fix:** one global `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }`
(now also applied to combo inputs by this change).

### X5 — `prefers-reduced-motion` only covers the drawer
`hdc.css` disables transitions for `#sidebar`, `.sidebar-backdrop` and
`.nav-group-arrow` — but `.kpi-card` lifts on hover, `.hdc-table tbody tr`
transitions, and `shared_expenses.css` runs a `se-flash` keyframe animation.
**Fix:** extend the query to `*`.

---

## 6. Print findings

### P1 — only 3 pages have print CSS
`transaction_receipt.html`, `payroll_salary_cards_print.html`,
`project_report_print.html` (+ the shell rule in `hdc.css` and the
`shared_expenses.css` block). Every other page — `/hdc/accounts/entries`,
`/hdc/tool-rental/dashboard`, `/hdc/reports/glance`, `/hdc/projects/<id>` —
prints the KPI gradient cards, the filter forms, the pagination bar and the
action buttons.
**Fix:** a shared `static/hdc/css/print.css` that hides `.hdc-card::before/::after`,
`.kpi-card`, filter forms, `.btn`, `._pagination` and forces
`-webkit-print-color-adjust: exact` only where a total row needs its fill.

### P2 — print pages still emit `d-none`-hidden combo selects
Not a bug today, but any page that adopts the §1 macros should hide
`[data-hdc-combo-source]` in print as well.

---

## 7. Prioritised fix list

| # | Finding | Effort | Impact |
|---|---|---|---|
| 1 | §1.4 — the ≈ 25 remaining ❌/⚠️ name fields (accounts `party_name` / `linked_party_name`, shared-expense party pickers, project `client`, purchase `delivery_person`, office-staff pickers, subcontractor attendance worker) | M | **High** — this is the rule |
| 2 | A1 — stop loading `accounts.css` globally | S | High (11 KB off every page) |
| 3 | T1 — replace the 63 live inline hex colours with tokens | M | High (dark mode) |
| 4 | R1 — wrap the 5 unwrapped tables | S | High (mobile) |
| 5 | X1 — `aria-label` / `title` on 73 unnamed buttons | S | High (a11y) |
| 6 | T4 — `color-scheme: dark` | S | Medium |
| 7 | A5 — asset-stamp every stylesheet | S | Medium |
| 8 | A2 — extract the 924 inline CSS lines | M | Medium |
| 9 | R4 — raise the `.58–.68rem` font floor | S | Medium |
| 10 | A3 + A4 — de-duplicate `.btn-mini` and the shell selectors | S | Low |
| 11 | R2 — add the 768–991px tier | M | Medium |
| 12 | R5 — drop `backdrop-filter` from `.hdc-card` | S | Medium (perf) |
| 13 | X4 + X5 — global `:focus-visible`, extend reduced-motion | S | Medium |
| 14 | P1 — shared `print.css` | M | Medium |
| 15 | X3 — `for=`/`id=` on the 11 unlabelled controls | S | Medium |

---

## 8. What changed in the code for this audit

| File | Change |
|---|---|
| `templates/hdc/shared/_combo_field.html` | **new** — `combo_field` / `combo_select` macros implementing the rule |
| `static/hdc/js/core/combo.js` | themed (token-based) menu CSS; `data-hdc-combo` auto-wire pass |
| `templates/hdc/shared/base.html` | `<noscript>` fallback for combo pairs |
| `templates/hdc/tool_rental/tool_rental.html` | `customer_name` → searchable combo |
| `templates/hdc/tool_rental/tool_rental_detail.html` | `to_customer_name` + both `received_to_account_id` → searchable combos |
| `templates/hdc/tool_rental/tool_inventory.html` | opening-purchase `supplier` → searchable combo |
| `templates/hdc/tool_rental/_stock_forms.html` | purchase-modal `supplier` → searchable combo |
| `templates/hdc/tool_rental/tool_position.html` | passes the supplier list to the modal |
| `hdc/services/tool_rental.py` | `known_tool_customers()`, `known_tool_suppliers()`, `MAX_COMBO_OPTIONS` |
| `hdc/routes/tool_rental.py` | feeds the new context to the four pages |
| `tests/test_name_combo_fields.py` | **new** — 10 tests pinning the rule and the post paths |
| `scripts/css_dom_audit.py` | **new** — the DOM audit that produced this document |

**Tests:** 505 pass (495 before this change + 10 new), including
`tests/combo_widget_harness.js` under Node and the whole tools/tracking
suite.
