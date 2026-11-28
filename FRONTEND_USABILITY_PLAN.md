# HDC ERP — Layout & Front-End Usability Improvement Plan

> **Status:** Audit-backed plan only — zero runtime code changed yet.  
> **Baseline:** `main` @ `807642e` · **Scope:** 138 templates (117 full pages + 21 partials), 10 CSS files, 20 JS modules.  
> **Companion docs:** [`CSS_UI_AUDIT.md`](CSS_UI_AUDIT.md), [`UI_ROOT_CAUSE_AUDIT.md`](UI_ROOT_CAUSE_AUDIT.md), [`RESPONSIVE_DESIGN.md`](RESPONSIVE_DESIGN.md), [`USER_PERMISSIONS_PLAN.md`](USER_PERMISSIONS_PLAN.md).

---

## 1. Executive Summary & Usability Diagnosis

HDC ERP already has strong foundations: offline-bundled assets, a token-based palette in `hdc.css`, an adaptive mobile layer (`responsive.css` + `responsive_tables.js`), a searchable combo widget (`core/combo.js`), and a unified save-verdict modal (`core/dialog.js`).

However, as the system grew to **117 screens across 17 functional domains**, usability friction accumulated in **seven structural areas**. Every figure below is measured directly from the current codebase (`807642e`):

| Area | Verified Current State (`807642e`) | Why It Hurts Daily Usability |
|---|---|---|
| **1. App Shell & Navigation** | `#sidebar` is `position: fixed` off-canvas at **every** screen width (`layout.js` closes it on every page load). Only **2 of 17** domains have a section sub-nav; **46 pages** hand-roll `← Back` buttons; **53 of 117 pages** render a duplicate page heading below `.topbar-title`. | Desktop/laptop operators must click the hamburger on *every* cross-module jump; deep pages lack breadcrumbs or section tabs; duplicate headings waste 50–72px above the fold. |
| **2. Page Layout Consistency** | **42 pages** stack multi-row create forms *above* data tables; **33** use `col-md-4/8` split panes; **only 5** use `shared/_page_head.html`; detail screens like `project_detail.html` (948 lines) and `tool_inventory.html` (1,036 lines) stack 10+ cards vertically. | Looking up a PO or expense requires scrolling past a 600px blank form; `col-md-4` forms squeeze on tablets (`768–1199px`); master-detail pages become 5,000px scroll walls. |
| **3. Form & Data-Entry Speed** | **15 templates** still render twin "Search X" + `<select>` inputs (`attendance.html`, `timekeeping.html`, `expenses.html`, `purchase_v2_*`) instead of `combo_select`; `/hdc/users` (85 options) & subcategories (38 options) remain bare selects; no `"Save & Add Another"` or live PKR formatting. | Twin inputs double form height and break Tab order; typing `450000` vs `45000` without live thousand separators invites zero-typos; repetitive batch entry requires extra clicks. |
| **4. Tables, Filters & Actions** | Only `.topbar` uses `position: sticky` (**0 sticky table headers**); only **11 templates** use `shared/_pagination.html`; **58 destructive actions** use raw `confirm()`; **110 icon-only buttons/links** lack `aria-label`. | Column headers disappear when scrolling ledgers; filter bars behave differently per module; browser `confirm()` gives zero context on *which* record/amount is being deleted. |
| **5. Theme & Visual Consistency** | `data-bs-theme` and `color-scheme: dark` are set **nowhere**; **26 of 52 modals** miss `.hdc-modal`; **236 hardcoded light classes** (`bg-light`, `bg-white`, `text-dark`) + inline hex colours remain. | Half of modal dialogs, pagination links, input groups, and read-only fields turn bright white or low-contrast in dark mode; `.card` vs `.hdc-card` surfaces drift visually. |
| **6. Mobile Field Ergonomics** | Tables stack cleanly into cards (`responsive_tables.js`), **but** multi-field filter forms & inline create forms remain expanded at top of phone screens, and primary CTAs scroll off-screen. | Field supervisors on phones still scroll 1–2 screenfuls of filters/forms before seeing the first record card or reaching the primary action. |
| **7. Frontend Asset Health** | `accounts.css` loads globally on all 117 pages; **8 custom CSS links** omit `?v={{ hdc_asset_stamp }}`; **22 templates** contain **964 lines** of `<style>`; **52 templates** contain **5,438 lines** of inline `<script>`; `.hdc-card` blurs every card; badges drop to `0.58rem` (~9.3px). | Stale CSS caches after deploys, paint lag on 20-card screens, sub-10px unreadable micro-text on 1080p monitors, and brittle 1,500-line inline scripts. |

---

## 2. Design Principles & Non-Negotiable Guardrails

1. **Speed for Repeat Operators First, Clarity for Occasional Users Second:** Data-entry operators live on keyboard Tab/Enter flows and compact scannable ledgers; managers and owners need clear KPIs, breadcrumbs, and self-explanatory actions.
2. **One Pattern Per Job:** One page header macro, one section sub-nav rail, one searchable combo pattern (`combo_select` / `combo_field`), one filter bar pattern, one confirmation/result dialog (`core/dialog.js`), and one themed card/modal surface.
3. **Preserve Existing CI & Test Contracts:**
   - Register every new template or partial in `DOMAIN_OF` inside `scripts/reorganize_frontend.py` (`python scripts/reorganize_frontend.py --check`).
   - Keep `responsive.css` loaded last and passing `python scripts/qa_responsive.py` + `qa_browser.py` (zero horizontal overflow at `390px` and `1440px`).
   - Preserve sidebar group IDs, order, and role gating pinned by `tests/test_sidebar_navigation.py`.
   - Extend `tests/test_frontend_config_contract.py` whenever inline `<script>` logic moves to `static/hdc/js/pages/`.
   - Keep all assets 100% offline/vendored (`tests/test_offline_assets.py`).

---

## 3. Workstream 1 — App Shell, Navigation & Wayfinding

### 3.1 Dockable Sidebar on Wide Desktop (`≥ 1200px`), Drawer on Tablet/Mobile (`< 1200px`)
* **Problem:** `templates/hdc/shared/base.html` + `static/hdc/js/core/layout.js` force `#sidebar` to start collapsed (`translateX(-100%)`) and close on every navigation, even on 1440px–1920px monitors.
* **Target Layout:**
  ```text
  Desktop (≥ 1200px, pinned mode — default on wide screens):
  ┌───────────────────┬──────────────────────────────────────────────────────────┐
  │ HDC ERP      [📌] │ [≡] Projects › Ali Residency (PRJ-014)   [🔍 Jump Ctrl+K] [🔔] [🌙] [👤] │
  ├───────────────────┤──────────────────────────────────────────────────────────┤
  │ ▸ Overview        │ [Overview] [Stages (6)] [Financials & Ledger] [Materials]│
  │ ▾ Projects & Est. │ ──────────────────────────────────────────────────────── │
  │   • Projects  ●   │ KPI Strip + Active Tab Content                           │
  │   • Stages        │                                                          │
  │   • Estimation    │                                                          │
  │ ▸ Workforce       │                                                          │
  │ ▸ Materials/Tools │                                                          │
  │ ▸ Accounts & Cash │                                                          │
  └───────────────────┴──────────────────────────────────────────────────────────┘
  ```
* **Implementation Details:**
  - Add a **Pin / Unpin toggle** (`localStorage['hdc_sidebar_mode'] = 'docked' | 'overlay'`, defaulting to `'docked'` at `≥ 1200px` and `'overlay'` below `1200px`).
  - When docked at `≥ 1200px`, `#wrapper` gives `#sidebar` its `234px` flex column (`position: sticky; top: 0; height: 100dvh`), hides `.sidebar-backdrop`, and lets operators switch modules in **1 click instead of 2**. Operators working on ultra-wide spreadsheets can still unpin (`[📌]`) for full-width mode.
  - Keep `<noscript>` fallback and all `sidebar_sections` / `EXPECTED_GROUPS` untouched so `tests/test_sidebar_navigation.py` passes cleanly.

### 3.2 Unified Breadcrumbs, Page Header & Elimination of Duplicate Titles
* **Problem:** 53 of 117 pages render a second heading inside `<main>` right below `.topbar-title`, and 46 pages place ad-hoc `← Back` buttons in different corners.
* **Plan:**
  - Upgrade `.topbar-title` in `templates/hdc/shared/base.html` to support a structured **Breadcrumb Bar** (`{% block breadcrumbs %}`) that automatically falls back to `Sidebar Section › Page Heading` when not overridden, and shows `Projects › Ali Residency › Stage: Grey Structure` on deep routes.
  - Expand `templates/hdc/shared/_page_head.html` to handle optional subtitle, count pill, status badge, and right-aligned `.hdc-page-head-actions` — then retire bespoke `.acc-page-header`, `.rm-header`, `.se-header`, and duplicate `<h2>`/`<h5>` wrappers across all list/hub screens.

### 3.3 Shared Section Sub-Navigation Rail (`_section_nav.html`)
* **Problem:** HDC Tools (`_tools_nav.html`) and Shared Expenses (`_nav.html`) are easy to navigate because sibling pages sit in a top pill rail. Purchase v2 (12 pages), Accounts (18 pages), Office Management (13 pages), Workforce/Payroll, and Projects/Stages lack this rail.
* **Plan:**
  - Extract `.tools-subnav` in `hdc.css` / `responsive.css` into a universal `.hdc-subnav` component + `templates/hdc/shared/_section_nav.html` macro (horizontally scrollable with fade cues on mobile, permission-gated via `hdc_page_allowed`).
  - Roll out section rails to **5 major multi-screen modules**:
    1. **Purchases & Inventory:** `Overview` · `Purchase Orders` · `Deliveries` · `Usage` · `Stock` · `Materials` · `Categories` · `Suppliers` · `Supplier ⇄ Site`
    2. **Accounts & Cash:** `Hub` · `Record Money` · `Transaction History` · `Balances` · `Daily Cashflow` · `Day Close` · `All Entries`
    3. **Projects & Estimation:** `Projects` · `Stages Overview` · `Stage Library` · `Estimation` · `Formulas`
    4. **Workforce & Payroll:** `Workers` · `Labour Card` · `Attendance` · `Subcontractors` · `Payroll` · `Trades`
    5. **Office Management:** `Overview` · `Staff Home` · `Attendance` · `Allowances` · `Staff Ledgers` · `Office Expenses`

### 3.4 Global Quick-Jump / Command Palette (`Ctrl+K` or `/`)
* **Plan:** Add a compact search trigger in `.topbar-right` (`Jump to… Ctrl+K`) backed by a lightweight core script (`static/hdc/js/core/quick_jump.js`):
  - **Instant tier (0 network requests):** Fuzzy-searches all visible links in `#sidebarGroups` plus common actions (*Record Money*, *New Purchase*, *Mark Attendance*, *New Rental*, *Add Project*).
  - **Entity tier (debounced GET, permission-scoped):** Optional `/hdc/api/quick_search?q=` returning top 5 matching Projects, Parties, Workers, Accounts, or Rentals the current user is permitted to read.

---

## 4. Workstream 2 — Standardizing Page Layout Archetypes & Visual Hierarchy

Today, identical CRUD + register workflows are laid out four different ways. Every screen should map cleanly to **one of four canonical ERP archetypes**:

### Archetype A — Register / Ledger Page (List + Filter + On-Demand Create)
* **Applies to:** `purchase_v2_purchases.html`, `purchase_v2_delivered.html`, `purchase_v2_usage.html`, `expenses.html`, `workers.html`, `subcontractors.html`, `projects.html`, `office_expenses.html`, `personal_expenses.html`, etc.
* **Layout Change:** Stop forcing a 400–650px blank creation form above the table on initial load (currently affecting **42 pages**):
  ```text
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ Purchase Orders (148)              [Export CSV]  [+ New Purchase ▾]      │
  ├──────────────────────────────────────────────────────────────────────────┤
  │ [+ Collapsible Quick-Entry Drawer / Panel — closed by default when       │
  │    browsing; remembers "pin open" toggle for full-time entry operators]  │
  ├──────────────────────────────────────────────────────────────────────────┤
  │ [Category ▾] [Supplier (Combo)] [Material (Combo)] [Date ▾] [Apply][Clr] │
  │ Active filters: [Supplier: Al-Madina Steel ×] [This Month ×] · Rs 1.42M  │
  ├──────────────────────────────────────────────────────────────────────────┤
  │ Sticky Table Header: PO# | Date | Supplier | Material | Qty | Rate | ... │
  │ Rows + Pinned Page/Filtered Total Strip + Shared Pagination              │
  └──────────────────────────────────────────────────────────────────────────┘
  ```
* **Why both personas win:** Managers/auditors see the filter bar and records **above the fold immediately**; data-entry operators click **"Pin Form Open"** once (stored per page in `localStorage`) to keep rapid inline batch entry open permanently.

### Archetype B — Dedicated High-Frequency Entry Workspace
* **Applies to:** `money_center.html` (*Record Money*), `tool_txn.html` (*Tool Transaction*), `attendance.html`, `subcontractor_attendance.html`.
* **Layout:** Follow and refine `money_center.html`'s proven `col-12 col-xl-8` (focused entry form) + `col-12 col-xl-4` (live context: account balances / current stock / last 5 entries recorded in this session so the operator has instant visual confirmation without leaving the form).
* **Breakpoint Fix:** Upgrade existing `col-md-4` / `col-md-8` split panes (33 pages) to `col-lg-5 col-xl-4` / `col-lg-7 col-xl-8` so forms never crush into 240px columns on `768px–1024px` tablets.

### Archetype C — Module Overview / Action Hub
* **Applies to:** `dashboard.html`, `accounts_hub.html`, `purchase_v2.html`, `tool_dashboard.html`, `office_management.html`.
* **Layout Rules:**
  1. **Needs-Attention Strip at Top** (e.g., negative balances, unchecked yesterday day-close, purchase integrity mismatches, overdue tool rentals, budget overruns) — only rendered when count > 0, with direct "Resolve →" links.
  2. **Primary KPI Row (max 4–6 cards visible):** Consolidate `purchase_v2.html`'s 9 competing KPI tiles (currently split across a 5-card row + 4-card row where some cards hold counts and others hold text labels like `"Item-wise"` or `"By Material"`) into **4 metric KPIs** + **clean navigation/action tiles**.
  3. **Primary Task Cards** (`Record Money`, `New Purchase`, `Record Delivery`, etc.) with clear verbs.

### Archetype D — Tabbed Master-Detail Entity Workspace
* **Applies to:** `project_detail.html` (948 lines), `tool_inventory.html` (1,036 lines), `tool_rental_detail.html` (850 lines), `party_ledger.html` (616 lines), `purchase_v2_supplier_detail.html` (539 lines).
* **Problem:** `project_detail.html` renders client metadata, 6 financial KPIs, 3 cost-split KPIs, collapsible insights, stages table, owner receipts, subcontractor terms, drawings, expenses, and material usage in one continuous vertical page.
* **Layout Change:**
  - Pin a compact **Entity Summary Strip** at top (Code, Name, Status, Client/Party link, 4 headline financial numbers, Primary Actions).
  - Organize sections into URL-hash / query-synced **Workspace Tabs**:
    - *Project Detail:* `Overview & Stages` · `Payments & Receipts` · `Subcontractors` · `Costs & Materials` · `Drawings & Files`
    - *Rental Detail:* `Items & Status` · `Return / Transfer` · `Payments & Ledger` · `Audit Trail`
  - Operators jump straight to the tab they need without scrolling 4,000px, and URL hash persistence (`#payments`) keeps links shareable and survives form POST redirects.

### Fluid Ergonomic Widths on Large Monitors
* Add `max-width: 1640px; margin-inline: auto;` to `.main-content` by default so 3-column forms and 5-column tables don't stretch edge-to-edge on 24"–27" 1920px/2560px monitors, with an opt-in `.main-content-fluid` block/class for ultra-wide 14-column audit matrices (`tool_audit_sheet.html`, `reports_glance.html`, `stage_overview.html`).

---

## 5. Workstream 3 — Form & Data-Entry Ergonomics

### 5.1 Retire the 15 Twin "Search + Select" Hacks & Finish `combo_select` Coverage
* **Current Friction:** 15 templates (`attendance.html`, `timekeeping.html`, `expenses.html`, `expense_edit.html`, `purchase_v2_delivered.html`, `purchase_v2_stock.html`, `purchase_v2_usage.html`, `subcontractor_attendance.html`, `labour_card.html`, `worker_ledger_entry_edit.html`, `reports.html`, `accounts.html`, `accounts_entries.html`, `tool_new_rental.html`, `tool_rental_detail.html`) still pair a manual `"Type to search..."` `<input>` above a native `<select>`.
* **Action:**
  1. Replace every twin filter+select pair with `combo_select(..., toggle=true)` (using `depends_on` for `Project → Stage` cascading, which `core/combo.js` already supports natively!). In `attendance.html` alone, this cuts **6 stacked inputs down to 3** and fixes keyboard Tab flow.
  2. Convert the remaining bare long `<select>`s (`purchase_v2_purchases.html` Supplier & Material; `/hdc/users` 85-option permission pickers; Cashflow/Money Center/New Transaction 17-option category & 38-option subcategory selects; Shared Expenses 21-option category filter).
  3. Convert remaining recurring-name plain inputs (`linked_party_name` on Account create/edit, `delivery_person` in Purchase Delivery/Stock edit modals, `counter_name` on Tool Audit count, `client` on Project add/edit) to `combo_field()`.

### 5.2 High-Speed Operator Input Helpers (`core/forms.js`)
* **Live Currency Readout & Guardrails:** Attach a lightweight non-intrusive helper (`data-hdc-money`) to PKR amount inputs that displays formatted words/commas beneath or beside the input as the operator types (`450000` → `Rs. 450,000 · Four Lakh Fifty Thousand`), preventing 10x zero-entry mistakes without breaking native numeric submission.
* **"Save & Add Another" + Sticky Context:** On high-volume forms (*Attendance*, *Expenses*, *Purchase Orders*, *Record Money*), add a secondary submit button (`Save & Next` / `Ctrl+Shift+Enter`) that preserves sticky header fields (`Date`, `Project`, `Account`) and auto-focuses the first line-item control after `core/dialog.js` is acknowledged with `Enter`.
* **Keyboard Shortcuts:**
  - `Ctrl+Enter` / `Cmd+Enter` inside any `<form>` submits that form.
  - Auto-focus first editable control whenever a Bootstrap `.modal` opens (`shown.bs.modal` listener in `core/forms.js`).
* **Progressive Disclosure on Conditional Fields:**
  - In `workers.html` (Add/Edit Worker), show only the active rate field (*Daily Wage* vs *Hourly Rate* vs *Rate per Sq Ft*) matching `wage_type`, hiding or collapsing the irrelevant two so operators never populate the wrong column.
  - Apply the same pattern to contract-type fields on Add/Edit Project (*Lump Sum* vs *Per Sq Ft* vs *Cost Plus*).
* **Inline Field Error Highlighting:** When `core/dialog.js` blocks a submission on client-side validation, also apply `.is-invalid` to the offending controls and scroll/focus the first invalid field on modal dismiss so the user lands directly on the box to fix.

---

## 6. Workstream 4 — Tables, Ledgers, Filtering & Safe Confirmations

### 6.1 Scannable Financial Tables & Sticky Headers
* **Sticky Table Headers:** Add `@media (min-width: 768px)` sticky `thead th` rules in `hdc.css` so column labels stay visible while scrolling 50–100 ledger rows (automatically compatible with `.table-responsive` when vertical scroll or window scroll is aligned).
* **Tabular Numerals & Right Alignment:** Enforce `font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap;` via a shared `.num` / `.hdc-amount` utility across all PKR, quantity, rate, and balance columns (currently many tables left-align numbers, e.g., `projects.html` Contract Value / Received / Net Profit).
* **Readable Typography Floor:** Raise micro-badges and compact table text from `0.58rem–0.68rem` (~9.3px–10.8px) to a **`0.75rem` (12px) minimum**, reclaiming space via tighter cell padding (`0.4rem 0.55rem`) rather than shrinking glyphs.
* **Pinned Totals Footer (`<tfoot>`):** Standardize a visually distinct sticky/bottom summary row showing **Page Subtotal** and (when paginated/filtered) **Filtered Total** so operators never confuse a 50-row page sum with the full ledger total.

### 6.2 Unified Filter Bar & Pagination Rollout
* **Shared Filter Pattern (`templates/hdc/shared/_filter_bar.html`):**
  - Left: primary search/entity combos + quick date presets (`Today` · `7d` · `This Month` · `Last Month` · `Custom`).
  - Right: single `Apply` (`btn-mint`) + `Reset` (shown only when ≥1 filter is active — removing duplicate buttons like `Clear` + `All Categories` on `purchase_v2_purchases.html`).
  - Active filter summary pills beneath the bar (`[Supplier: X ×] [Oct 2026 ×] — 24 matching rows`) so operators always see *why* rows are hidden.
  - Remove full-screen blocking overlays for local filtering (e.g., `#workerFilterLoadingOverlay` in `workers.html`).
* **Pagination Coverage:** Expand `templates/hdc/shared/_pagination.html` beyond the current **11 templates** to remaining unbounded or arbitrary-60-cap tables (`attendance.html`, `expenses.html`, `office_expenses.html`, `subcontractors`, `tool_rental` lists), and add a `Per page: 25 | 50 | 100` selector.

### 6.3 Contextual Confirmation Dialogs (Replacing 58 Native `confirm()` Alerts) & Row Actions
* **Problem:** 58 forms/buttons call `onclick="return confirm('Delete this record?')"` — an unstyled browser popup that hides record details and looks jarring next to `core/dialog.js`.
* **Plan:**
  - Add declarative `data-hdc-confirm` support directly into `static/hdc/js/core/dialog.js`:
    ```html
    <form method="POST" action="..."
          data-hdc-confirm="Delete attendance record?"
          data-hdc-confirm-detail="Ali Hassan · 8h on 2026-11-28 (Rs. 2,500)"
          data-hdc-confirm-cta="Delete Record"
          data-hdc-confirm-tone="danger">
    ```
  - Renders in the same themed modal shell with `Cancel` (focused by default for destructive safety, `Esc` closes) and red `Delete / Void` button.
* **Row Action Tidy-Up:**
  - Keep the **1–2 primary row actions** visible (e.g., `Open` / `Edit`), and collapse 3+ secondary/destructive actions (`Void`, `Reverse`, `History`, `Delete`) into a compact Bootstrap dropdown (`⋯`) on dense rows so action columns stop consuming 180px+ of table width.
  - Add explicit `aria-label` + `title` to all **110 icon-only buttons/links** and **37 `.btn-close` buttons**.

---

## 7. Workstream 5 — Theme Unification, Dark Mode Root-Cause Fix & CSS/JS Hygiene

### 7.1 Fix Dark Mode at the Root (Eliminate Split-Brain Styling)
1. **Sync Bootstrap 5.3 Native Theme (`base.html` + `core/theme.js`):**
   - Set `document.documentElement.setAttribute('data-bs-theme', t)` alongside `data-theme` in both the inline `<head>` boot script (`base.html:27`) and `static/hdc/js/core/theme.js`.
   - Add `:root, [data-theme="light"] { color-scheme: light; }` and `[data-theme="dark"] { color-scheme: dark; }` in `hdc.css` so native date pickers, scrollbars, and number spinners match the active theme.
2. **Theme All 52 Modals & Forgotten Bootstrap Surfaces Globally (`hdc.css`):**
   - Style `.modal-content` directly from `var(--modal-bg)` / `var(--card-border)` in `hdc.css` (making `.hdc-modal` an optional alias rather than a mandatory opt-in that **26 of 52 modals** currently miss), and add `background: var(--modal-bg)` to `.hdc-mini-modal .modal-content` in `new_transaction.css`.
   - Add token rules (~25 lines in `hdc.css`) for `.page-link`, `.input-group-text`, `.progress`, `.list-group-item`, and `.dropdown-menu`.
3. **Replace the 236 Hardcoded Light Utilities & Inline Hex Styles:**
   - Introduce semantic surface classes (`.hdc-surface-subtle`, `.hdc-input-readonly`, `.hdc-badge-neutral`) backed by CSS tokens, replacing `bg-light`, `bg-white`, `text-dark`, `alert-light`, and the 35+ inline hex `style="..."` colours across Accounts, Shared Expenses, Purchase v2, Projects, and Tools.
   - Harmonize `.card` and `.hdc-card` so no box looks alien next to its neighbour.

### 7.2 Stylesheet & Script Consolidation
1. **Stop Global CSS Bleed:** Move `accounts.css` out of `templates/hdc/shared/base.html` into only the Accounts templates that use `.acc-*` classes (~11 KB saved on 95+ non-accounts pages).
2. **100% Cache-Busting Parity:** Append `?v={{ hdc_asset_stamp }}` to the **8 unstamped CSS `<link>` tags** (`accounts.html`, `accounts_entries.html`, `cashflow_register.html`, `money_center.html`, `new_transaction.html`, `login.html`) and page JS `<script>` tags.
3. **Drop Expensive Card Blur:** Remove `backdrop-filter: blur(8px)` from `.hdc-card` (keeping crisp opaque/semi-opaque token backgrounds) to eliminate scroll/paint jank on multi-table screens like `/hdc/tool-rental/tracking` and `/hdc/reports/glance`.
4. **Extract Inline `<style>` (964 lines across 22 templates) & Oversized Inline `<script>` (5,438 lines across 52 templates):**
   - Priority CSS extractions: `print.css` (shared print rules + `transaction_receipt.html`, `payroll_salary_cards_print.html`, `project_report_print.html`), `expenses.css`, `tool_rental.css`.
   - Priority JS extractions (guarded by `tests/test_frontend_config_contract.py` JSON config blocks):
     - `tool_new_rental.html` (**1,536 lines** inline JS → `static/hdc/js/pages/tool_new_rental.js`)
     - `tool_inventory.html` (**508 lines** → `static/hdc/js/pages/tool_inventory.js`)
     - `purchase_v2_stock.html` (286 lines), `timekeeping.html` (233 lines), `account_edit.html` (196 lines), `subcontractor_attendance.html` (159 lines), `purchase_v2_usage.html` (154 lines), `expenses.html` (152 lines), `purchase_v2_purchases.html` (135 lines).

---

## 8. Workstream 6 — Mobile & Field-Site Usability Enhancements

`static/hdc/css/responsive.css` and `static/hdc/js/core/responsive_tables.js` already prevent horizontal overflow and stack 982+ tables into mobile cards. The next mobile usability leap focuses on **vertical scroll reduction and thumb ergonomics** below `768px`:

1. **Collapsible Mobile Filter Sheet / Accordion (`< 768px`):**
   - Wrap multi-control `GET` filter forms in a mobile-collapsible bar showing `Filters` + active filter count badge (`Filters · 2 active`). Operators see the first record card immediately on load and tap once to adjust filters.
2. **Enhanced Mobile Record-Card Header & Footer (`responsive_tables.js` + `responsive.css`):**
   - Promote the row's primary PKR amount / status badge into the card top-right header next to the lead title so an operator scanning 30 cards reads `Title + Amount + Status` on a single line.
   - Render the action cell (`<td>` holding buttons/forms) as a clean full-width footer action strip with comfortable `40px` touch spacing so `Edit` and `Delete` are never accidentally mis-tapped.
3. **Sticky Bottom Primary CTA on Mobile Entry Screens:**
   - Pin the primary submit bar (`Save Transaction`, `Save Purchase`, `Save Attendance`) at the bottom of the viewport (`padding-bottom: env(safe-area-inset-bottom)`) on long mobile forms so operators don't have to scroll past summary blocks to submit.
4. **Right Mobile Keyboards (`inputmode`):**
   - Add `inputmode="decimal"` on rate/amount/quantity inputs and `inputmode="tel"` on phone fields across templates so Android/iOS immediately pops the numeric keypad.

---

## 9. Workstream 7 — Accessibility, Empty States & Permission-Clean UX

1. **Accessible Names & Label Associations:**
   - Add `aria-label="Close"` to all 37 bare `.btn-close` buttons, descriptive `aria-label` + `title` to all 110 icon-only links/buttons, and explicit `for=`/`id=` bindings to the 15 unlabelled modal/filter controls identified by `scripts/css_dom_audit.py`.
2. **Keyboard Focus & Reduced Motion:**
   - Add a clear global `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }` rule covering `.kpi-card` links, `.hub-big-card`, `.hdc-subnav` pills, and custom buttons, and extend `@media (prefers-reduced-motion: reduce)` across hover transforms and animations.
3. **Actionable Empty States & Dismissible Onboarding Hints:**
   - Replace generic `"No records yet."` rows with a shared `empty_state(icon, title, hint, cta_label, cta_href, can_create)` macro that guides new users without cluttering populated screens.
   - Convert permanent bottom instructional alerts (such as the `Projects ↔ Parties integration` banner on `projects.html`) into compact `ⓘ How this syncs` popover/collapsible `<details>` hints.
4. **Permission-Clean Controls (Aligning with `USER_PERMISSIONS_PLAN.md`):**
   - Replace the client-side URL-regex link-hiding script at the bottom of `base.html` (`lines 428–437`) with server-rendered `hdc_page_allowed` / `hdc_record_allowed` checks + subtle `"Read-only view"` indicators so restricted operators never experience flash-of-visible-buttons or silent 403 dropdown failures.

---

## 10. Phased Implementation Roadmap (Prioritized by Leverage & Low Risk)

Each phase is independently shippable, testable, and reversible without breaking ongoing operations:

| Phase | Scope | Effort | User Impact | Key Files Touched |
|---|---|---|---|---|
| **Phase 1 — Foundation & Instant Visual Fixes** | • Bridge `data-bs-theme` + `color-scheme: dark` (`theme.js`, `base.html`)<br>• Theme `.modal-content`, `.page-link`, `.input-group-text`, `.progress` globally in `hdc.css`<br>• Scope `accounts.css` out of `base.html`; add `?v={{ hdc_asset_stamp }}` to all 8 unstamped CSS links; remove `backdrop-filter` blur; raise `0.75rem` font floor<br>• Replace `bg-light`/`bg-white`/`text-dark` & inline hex colours with tokens<br>• Add `aria-label` to 37 `.btn-close` + 110 icon buttons + global `:focus-visible` | **S–M** (1–2 PRs) | **Immediate** across all 117 pages (fixes every broken dark-mode modal/box, improves readability & paint speed) | `base.html`, `core/theme.js`, `hdc.css`, `new_transaction.css`, targeted templates |
| **Phase 2 — App Shell, Docked Sidebar & Section Navigation** | • Dockable desktop sidebar (`≥ 1200px` pinned mode + pin toggle, mobile/tablet overlay unchanged)<br>• Topbar Breadcrumbs + unified `shared/_page_head.html` rollout (retire 53 duplicate headings & 46 ad-hoc Back buttons)<br>• Shared `shared/_section_nav.html` rolled out to *Purchases*, *Accounts*, *Projects*, *Workforce*, *Office*<br>• `Ctrl+K` Quick-Jump menu (`core/quick_jump.js`) | **M** (2–3 PRs) | **Very High** — cuts cross-page navigation clicks in half and makes every multi-page module self-orienting | `base.html`, `core/layout.js`, `hdc.css`, `responsive.css`, `shared/_page_head.html`, `shared/_section_nav.html`, `scripts/reorganize_frontend.py` |
| **Phase 3 — Form Speed, Combos & Safe Confirmations** | • Convert all 15 twin "Search + Select" templates and remaining bare long `<select>`s / plain name fields to `combo_select` / `combo_field`<br>• Add `HDCDialog.confirm()` (`data-hdc-confirm`) and migrate all 58 `confirm()` calls<br>• Add live PKR readout (`data-hdc-money`), `Ctrl+Enter` submit, `"Save & Add Another"`, modal auto-focus, `inputmode="decimal"`, and conditional field disclosure (`workers.html`, `add_project.html`) | **M** (2–3 PRs) | **Very High** for daily operators — faster entry, fewer ledger spelling duplicates, fewer zero-typos, safe deletes | `core/combo.js`, `core/dialog.js`, `core/forms.js`, `shared/_combo_field.html`, 25–30 form templates |
| **Phase 4 — Register Layout, Sticky Tables & Mobile Density** | • Standardize **Archetype A** on the 42 form-above-table pages (collapsible/pinnable entry drawer + records above the fold)<br>• Sticky desktop `thead`, right-aligned tabular numerals, pinned subtotal/filtered-total footers<br>• Shared `_filter_bar.html` (active chips + date presets) & expand `_pagination.html` beyond 11 pages<br>• Mobile `< 768px` collapsible filter pill + enhanced mobile card header/footer actions | **M–L** (3–4 PRs) | **High** — puts records above the fold on both laptop and phone, makes long ledgers effortless to scan | `hdc.css`, `responsive.css`, `core/responsive_tables.js`, `shared/_filter_bar.html`, `shared/_pagination.html`, register templates |
| **Phase 5 — Master-Detail Tabs & Asset Modularization** | • Convert 850–1,000+ line scroll-wall detail pages (`project_detail.html`, `tool_inventory.html`, `tool_rental_detail.html`, `party_ledger.html`) into tabbed **Archetype D** workspaces<br>• Simplify `purchase_v2.html` & module hubs (**Archetype C**)<br>• Extract remaining inline `<style>` (964 lines, incl. shared `print.css`) and large inline `<script>` blocks (`tool_new_rental.html`, `tool_inventory.html`, `purchase_v2_*`) with `test_frontend_config_contract.py` coverage | **L** (3–4 PRs) | **High** — turns the heaviest screens into fast, maintainable workspaces and completes clean CSS/JS separation | `projects/`, `tool_rental/`, `purchase/`, `parties/`, `static/hdc/css/`, `static/hdc/js/pages/` |

---

## 11. Verification & Acceptance Criteria (Every Phase)

Every PR implementing a slice of this plan must pass both existing automated gates and measurable UX checks:

1. **Automated DOM & CSS Audit (`scripts/css_dom_audit.py`):**
   - `0` unwrapped responsive tables on screen views.
   - `0` bare `.modal-content` boxes in dark mode (`data-bs-theme="dark"` + global `.modal-content` token rule).
   - `0` unnamed icon-only buttons or `.btn-close` controls (`aria-label` coverage = 100%).
   - `0` unlabelled form inputs.
   - `0` data-backed `<select>` elements with `> 12` options left without `combo_select`, and `0` twin "Type to search" + `<select>` hacks remaining.
2. **Responsive & Browser Release Gates:**
   - `python scripts/qa_responsive.py` passes.
   - `python scripts/qa_browser.py` passes at both `1440px` and `390px` in light **and** dark themes (`scrollWidth <= innerWidth` on 100% of routes).
3. **Frontend Structure & JS Syntax Gates:**
   - `python scripts/reorganize_frontend.py --check` passes.
   - `find static -type f -name '*.js' -print0 | xargs -0 -r -n1 node --check` passes.
   - Full Python + JSDOM test suite passes (`test_sidebar_navigation`, `test_list_page_consistency`, `test_responsive_layer`, `test_responsive_tables_js`, `test_combo_widget`, `test_name_combo_fields`, `test_frontend_config_contract`, `dialog_harness.js`).
4. **Usability Benchmarks (Operator Task Checks):**
   - **Cross-module switch on desktop (`≥ 1200px`):** `1 click` (down from `2 clicks + drawer wait`).
   - **Sibling page switch inside Purchases / Accounts / Projects / Office:** `1 click` via `.hdc-subnav` rail (no hunting for `← Back` or re-opening the drawer).
   - **First visible data row on Register pages (`1366×768` laptop & `390×844` phone):** visible **above the fold** without scrolling past a blank multi-row entry form.
   - **Keyboard-only entry on Attendance / Purchase / Record Money:** complete an entire record using `Tab` → type-to-search combos → `Ctrl+Enter` → `Enter` without touching the mouse.
