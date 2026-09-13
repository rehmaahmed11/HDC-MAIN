# HDC ERP — Complete End-to-End Application Report

**Date:** 2026-09-13 · **Repo:** `rehmaahmed11/HDC-MAIN` · **Branch reviewed:** `main` (commit `22cc2c6`)

This report walks the whole application **head to toe**: what it is, how every
section works, every input/output and calculation, how it is deployed, how the
GitHub webhook deploys it, and a prioritised list of suggestions at the end.

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Tech stack & architecture](#2-tech-stack--architecture)
3. [Startup sequence & database self-migration](#3-startup-sequence--database-self-migration)
4. [Authentication, roles & security](#4-authentication-roles--security)
5. [Feature-by-feature walkthrough (inputs → calculation → outputs)](#5-feature-by-feature-walkthrough)
   - 5.1 Dashboard · 5.2 Projects & Stages · 5.3 Estimation · 5.4 Timekeeping/Attendance
   - 5.5 Workers, Advances, Payments, Tips · 5.6 Payroll · 5.7 Subcontractors & subcontract labour
   - 5.8 Materials & Purchases (V1 legacy) · 5.9 Purchase V2 (suppliers, POs, deliveries, usage, stock)
   - 5.10 Expenses & Alerts · 5.11 Office Management · 5.12 Personal Management
   - 5.13 Unified Accounts · 5.14 Reports & exports · 5.15 Settings, backups, maintenance
   - 5.16 Audit trail & row traceability
6. [Deployment — how the app reaches production](#6-deployment)
7. [The GitHub webhook — exact mechanics](#7-the-github-webhook)
8. [CI pipeline](#8-ci-pipeline)
9. [Calculation quick-reference (every formula in one table)](#9-calculation-quick-reference)
10. [Verification evidence](#10-verification-evidence)
11. [Suggestions (prioritised)](#11-suggestions-prioritised)

---

## 1. Executive summary

HDC ERP is a **single-deployable construction-company ERP** for a Pakistani
builder (all money in **PKR**, all dates/times in **Asia/Karachi**). It runs as
one Flask + SQLite application on **PythonAnywhere**, deployed by a **GitHub
push webhook** (`git pull` + WSGI touch — no CI server needed).

It tracks the full business loop:

**Estimate → Project & Stages → Attendance (timekeeping) → Worker wages →
Advances/Payments/Tips → Payroll runs → Subcontractors & their labour →
Material purchases, deliveries, usage, stock → Expenses → Office staff &
salaries → Personal expenses → a unified double-entry-style Accounts ledger →
Reports/Dashboard/Backups.**

- **~23,000 lines of Python** in a clean layered package (`hdc/`):
  models → services → routes, plus 82 Jinja templates.
- **Every cash movement mirrors into one unified ledger**
  (`hdc_account_txn`) — worker payments, advances, tips, supplier payments,
  owner receipts, office salaries, personal expenses — with overdraft
  protection on treasury accounts.
- **Money integrity was professionally audited** (see `LABOUR_AUDIT.md`):
  16 findings, 14 fixed and pinned by **127 passing regression tests**.
- **The database migrates itself on every boot** (idempotent
  `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE` healers + one-off flagged data
  migrations) — no Alembic, no manual migration step.
- Deploys are **atomic-ish and safe for data**: `scripts/check_db_safety.py`
  (run in CI) guarantees the live database can never be overwritten by a push.

---

## 2. Tech stack & architecture

| Layer | Technology |
|---|---|
| Web framework | Flask 3 (app-factory pattern, `hdc/app.py`) |
| ORM / DB | Flask-SQLAlchemy 3 + SQLite (WAL mode, foreign keys ON) |
| Auth | Flask-Login, Werkzeug password hashing, per-worker login throttle |
| Templates | 82 Jinja2 templates in `templates/hdc/` (server-rendered, vanilla JS) |
| Exports | openpyxl (XLSX), CSV, print-to-PDF pages |
| Production | Gunicorn / PythonAnywhere WSGI |
| Config | Environment variables + optional `.env` (`HDC_DB_PATH`, `HDC_INSTANCE_DIR`, `HDC_SECRET_KEY`, `HDC_ENV`, …) |

### Module map (all under `hdc/`)

```
hdc/
├── app.py            create_app(): factory, security headers, hooks, bootstrap
├── config.py         env-driven per-app settings (DB path, instance dir, cookies)
├── extensions.py     db, login_manager, CSRF, DB-runtime self-heal hook
├── core/
│   ├── bootstrap.py  one-time per-app init: schema healers, migrations, seeds
│   ├── schema.py     the whole hand-rolled migration suite (1,195 lines)
│   ├── flags.py      hdc_runtime_flag key/value store (guards one-off migrations)
│   └── admin.py      maintenance, wipe/restore, password reset helpers
├── models/           11 model modules, ~35 tables (all prefixed hdc_)
├── services/         business logic: accounts, timekeeping, ledger, purchase,
│                     estimation, aggregation, reporting, backups, audit, actors…
├── routes/           20 blueprinted-ish route modules (~100 URLs)
└── utils/            dates (PKT), formatting, normalisation
```

Supporting files: `wsgi.py` (gunicorn entry), `wsgi_dispatch_snippet.py`
(PythonAnywhere WSGI that routes `/deploy` → hook, everything else → app),
`deploy_hook.py` (the webhook), `ops/pythonanywhere/install_deploy_hook.py`
(one-command installer), `scripts/` (audit CLI, DB-safety guard, password
reset, parity/split tooling), `.github/workflows/ci.yml`.

**Design rules the codebase follows:** routes stay thin and call services;
services never touch `request`; per-app settings live in
`app.extensions['hdc_settings']` so tests can run many apps/DBs in one process;
every list row is soft-voidable (`is_void` + reason + timestamp — nothing is
hard-deleted except payroll runs and their mirrored payments get voided too).

---

## 3. Startup sequence & database self-migration

`create_app()` → `_ensure_bootstrap_once(app)` runs **on every boot/reload**,
and `before_request _ensure_db_runtime_ready` re-runs it if the DB file was
swapped out from under a running process. Order:

1. `db.create_all()` — creates any **missing tables** from the models.
2. `_run_migrations()` — idempotent DDL: `CREATE TABLE IF NOT EXISTS` for
   ~20 tables, ~60 `ALTER TABLE … ADD COLUMN` statements (errors swallowed =
   "column already exists"), index creation, and three table **rebuilds** for
   legacy shapes (stage definitions per-project uniqueness, subcontractor
   nullable `project_id`, subcontract labour attendance with `worker_id`).
3. Dedicated healers: `_ensure_timeentry_attendance_day_schema`,
   `_ensure_purchase_v2_schema`, `_ensure_owner_payment_void_schema`,
   `_ensure_accounts_schema`, `_ensure_subcontract_*`, `_ensure_runtime_flags_table`.
4. **Data backfills** (idempotent SQL): `activity_at` timestamps for legacy
   rows, `is_void` defaults, auto subcontractor codes, expense
   text-category → `category_id` migration with a table rebuild, subcontract
   event backfill.
5. **One-off flagged migrations** (guarded in `hdc_runtime_flag`, so they run
   exactly once ever):
   - `time_entry_migration_done` — converts legacy `hdc_attendance` rows into
     `hdc_time_entry` rows (09:00 check-in, wage preserved, `attendance_id` link).
   - `time_entry_dedupe_done` — merges duplicate/overlapping time entries,
     repairs orphaned wage-ledger links.
6. Seeds: first admin user (`HDC_BOOTSTRAP_ADMIN_USERNAME` /
   `HDC_BOOTSTRAP_ADMIN_PASSWORD`, refuses to boot in prod without a password),
   9 default trades, 7 default expense categories (incl. **Tip**),
   accounts backfill (company cash + external parties + per-person accounts).

**Verified behaviour (this review):** dropping a column from a live DB and
rebooting re-adds it automatically with data intact; boots are idempotent
(no duplicate seeds); the legacy-attendance migration runs exactly once.

> Caveat: `db.create_all()` never adds columns to *existing* tables — every new
> model column must also get an `ALTER TABLE` line in `hdc/core/schema.py`.
> The codebase has followed that discipline; see suggestion #1 to make it
> structural.

---

## 4. Authentication, roles & security

| Aspect | Implementation |
|---|---|
| Login | `/hdc/login` (POST), Werkzeug `check_password_hash`; logs to activity + audit trail |
| Throttling | Per-worker in-memory: 5 failures / 300 s → 900 s lockout (env-tunable) |
| Roles | `hdc_user.role`: admin / manager / accountant; admin-only guard on user management & maintenance |
| CSRF | Token injected via context processor; enforced **on all POST/PUT/PATCH/DELETE** (form field, `X-CSRFToken` header, or JSON body) |
| Cookies | `HttpOnly`, `SameSite=Lax`, `Secure` in prod; HSTS when secure |
| Headers | `nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy`, `Permissions-Policy` (camera/mic/geo off) |
| Secrets | `HDC_SECRET_KEY` **required in prod** (boot fails otherwise); dev falls back with a loud warning |
| Passwords | Strength-checked at bootstrap; CLI reset via `scripts/reset_admin_password.py` |

All pages require login except `/hdc/login` and the deploy-hook endpoints
(which are separately HMAC-protected — see §7).

---

## 5. Feature-by-feature walkthrough

Legend for each section: **Inputs** (what the user/other sections feed in) →
**Processing** (the actual calculation/logic in the code) → **Outputs**
(what appears where).

### 5.1 Dashboard (`/hdc/`, `hdc/routes/dashboard.py`)

- **Inputs:** all projects + aggregated costs, owner payments, expenses,
  today's time entries, stage end-dates, budgets.
- **Processing (KPIs):**
  - `total_contract = Σ project.owner_contract_value`
  - `total_received = Σ non-void OwnerPayment`
  - `total_cost = Σ (labour + subcontract + expenses + material)` per project
  - `net_profit = Σ project.net_profit − office_expense_total`
  - `pending_receivable = total_contract − total_received`
  - `workers_present` = distinct workers with a time entry today
  - `delayed_stages` = stages past `end_date` and not completed
  - `budget_overruns` = projects where `actual_cost > budget_total`
- **Outputs:** KPI cards, per-project contract-vs-cost bar chart (top 8),
  expense-category pie, today's expense total, recent projects; drill-down
  `/hdc/kpi/<metric>` pages and `/hdc/cost-entries/<scope>/<id>/<head>` that
  itemise exactly which rows make up any cost head.

### 5.2 Projects & Stages (`routes/projects.py`)

- **Inputs:** project code (auto-incrementing `P-###`), name, client, phone,
  location, contract type (`sqft` with rate & constructed sqft, `lump_sum`,
  or hybrid), budget, planned dates; stages from a per-project **stage
  library** (bulk-add), each stage: basis (`Per Sq Ft` / `Lump Sum`), rate,
  discount, qty, lump value, execution mode (company / subcontractor), dates,
  progress, drawings (uploaded files stored in the instance dir).
- **Processing (model properties on `Project` / `Stage`):**
  - `Stage.effective_rate = rate_per_sqft − discount_per_sqft`
  - `Stage.contract_value = lump_sum_value` **or** `effective_rate × qty_sqft`
  - `Stage.original_value` — same formula against the frozen original fields
    (every rate edit writes `StageRateHistory` first)
  - `Project.owner_contract_value = Σ stage values`, falling back to
    `sqft × rate` or `lump_sum` when stages are missing
  - `*_cost` rollups: labour (time entries + unmigrated legacy attendance),
    material (legacy purchases + usage-log V2 cost), expenses (non-void),
    subcontract (cash-cleared payments) → `total_cost`
  - `net_profit = owner_contract_value − total_cost`,
    `remaining_receivable = owner_contract_value − total_received`
  - Aggregation service (`services/aggregation.py`) computes these in **bulk
    SQL** for list pages and caches them on the objects (`_agg_*`).
- **Outputs:** project list/detail pages with profit/receivable per project,
  stage ledger (`/hdc/stage/<id>/ledger`) itemising every cost, owner-payment
  receipts (printable), drawings viewer, rate-change history.

### 5.3 Estimation (`routes/estimation.py`)

- **Inputs:** stage rows with `type ∈ {lump_sum, sqft}`, rate, quantity;
  custom formulas (name, expression, variables).
- **Processing:** `total = rate` (lump) **or** `rate × qty` (sqft); rows saved
  to a JSON store (`project_estimations.json`); "convert" creates a real
  Project + Stages from an estimation (`_create_project_from_estimation`).
- **Outputs:** estimation page totals, project codes auto-issued
  (`/hdc/api/next_project_code` etc.), formula variable API.

### 5.4 Timekeeping / Attendance (`routes/timekeeping.py` + `services/timekeeping.py`)

- **Inputs (per worker per day):** status (`present/absent/leave/not_assigned`),
  one or more project→stage allocations each with **hours** and, for per-sqft
  workers, **qty_sqft performed**; remarks. Bulk sheet for the whole crew;
  single-entry form; edit/void/reactivate per entry.
- **Processing — the wage engine:**
  1. Day is sliced into entries; the **first 8 hours of the day are regular**,
     hours beyond 8 are overtime (per entry via `regular_done` accumulator).
  2. `_calc_time_wage(worker, regular_h, ot_h, qty, work_date)`:
     - `daily`: `base_rate × min(1, hours/8) + ot_h × (base_rate/8)`
     - `hourly`: `hourly_rate × (hours + ot)` — overtime at straight time
       (documented company practice)
     - `per_sqft`: `rate_per_sqft × qty_sqft` (hours ignored)
  3. **Rates are date-aware:** `_worker_rate_on(worker, work_date)` resolves
     the `WorkerRate` history row effective that day; falls back to profile
     only when no history exists.
  4. Each entry mirrors to `LabourLedger` (`entry_type='work'`) via
     `_sync_work_ledger_for_time_entry`; voiding an entry voids its mirror.
  5. `_recalculate_attendance_day` rebuilds the per-day summary
     (`AttendanceDay`: total hours, OT, day value = 1 if ≥ 8 h) and re-links
     entries via `attendance_day_id`.
  6. Edit/void/reactivate recalculates the whole day and enforces a 24 h cap.
- **Outputs:** per-day wage on every time entry, day summary cards
  (assigned/absent/not-assigned, live status view), attendance history with
  void trail, wages feeding Workers → Payroll → Project costs.

### 5.5 Workers — ledger, advances, payments, tips (`routes/workers.py`)

- **Inputs:** worker (code, trade, wage type + rate), ledger rows of types
  `advance / payment / tip / settlement` (dates, project/stage tags, notes),
  rate changes (type, new rate, effective-from, reason).
- **Processing — one balance formula everywhere** (the core invariant,
  `LABOUR_AUDIT #1`):
  ```
  earned   = Σ active time-entry wages + unmigrated legacy attendance wages
  advanced = Σ active 'advance' rows
  paid     = Σ active 'payment' rows          (tips NOT deducted)
  settled  = Σ active 'settlement' rows
  balance  = earned − advanced − paid − settled
  payable  = max(0, balance)
  tips     = tracked separately, informational only (gratis cash)
  ```
  Both `Worker.balance_due` (ORM property) and
  `_worker_payable_snapshot()` (SQL service) implement exactly this and were
  verified to agree.
- **Payment screen logic:** `payment_part = min(amount, payable_now)`;
  any excess must be classified as **Tip** or **Excess-as-Advance** (or the
  post is rejected); a short-pay flagged "settle shortfall" writes a negative
  `settlement` expense so the stage is credited; tips/settlements mirror into
  `hdc_expense` (tagged `TIP_WORKER_ID:` / `TIP_EXPENSE_ID:` /
  `SETTLE_WORKER_ID:`) so voiding/restoring a ledger row moves its expense
  with it and the **tip reconciler** (`_reconcile_worker_tip_ledger`, runs on
  ledger page load) can never duplicate or resurrect a tip.
- **Rate changes:** write `WorkerRate` (date-effective) + `LabourRateHistory`
  for daily workers, auto-baseline on first change — historical days keep
  their historical rate on recalculation (verified end-to-end).
- **Ledger page:** chronological running balance where `work` rows take their
  value from the linked time entry (`LABOUR_AUDIT #10` fix), advances/payments/
  settlements subtract, tips show as neutral rows; edit/void/restore with
  mirrored-expense sync; duplicate guard (12 s window) on all money posts.
- **Outputs:** printable receipts (`RCPT-WP-########`), per-worker ledger,
  workers list with due balances, everything mirrored into unified accounts.

### 5.6 Payroll (`routes/payroll.py`)

- **Inputs:** date range (`from`/`to`), optional per-worker manual payments.
- **Processing:**
  - **Overlapping runs are refused** — a run whose window intersects an
    existing run is rejected (`LABOUR_AUDIT #7`).
  - Gross per worker = `Σ wage_calculated` of the worker's active time entries
    inside the window; all active workers get an item (zero-filled) for clear
    salary cards.
  - `advance_deducted` = advances **dated inside the window**;
    `net_pay = max(0, gross − advances)`; advances above gross are reported as
    **"carried fwd"** instead of vanishing (`#8`).
  - Payments (single or "Pay All") write `LabourLedger` payment rows keyed by
    note `Payroll run #N …`, capped at the run balance, mirrored to accounts;
    **deleting a run voids those payments *and* their account transactions**
    (`#4`).
- **Outputs:** run summary (per worker: days, OT, gross, advance, carried,
  payable, paid, balance, W/A/N calendar), printable salary cards, salary-card
  history, XLSX salary export (Reports).

### 5.7 Subcontractors (`routes/subcontractors.py` + `models/subcontract.py`)

Two layers: (a) the subcontractor's **contract** with the company, (b) the
subcontractor's **own labour** (their workers, attendance, payments).

- **Inputs:** contract (sqft rate × sqft or lump sum, retention %, progress %),
  payments/settlements, daily attendance (headcount + work-done %), labour
  workers (name/trade/daily wage), per-worker-per-day-per-stage labour
  attendance (count, wage rate, hours, OT).
- **Processing (model properties):**
  ```
  contract_value        = rate×sqft | lump_sum
  retention_amount      = contract_value × retention% / 100
  logged_progress       = Σ daily work_done_pct   (clamped 0..100)
  effective_progress    = manual % if set else logged %
  gross_payable         = contract_value × effective_progress / 100
  payable_amount        = min(gross_payable, contract_value − retention)
  total_cleared         = Σ non-void payments (incl. settlements)
  payable_balance       = max(0, payable_amount − total_cleared)
  live_balance          = payable_amount − total_cleared   (negative ⇒ advance)
  ```
  Subcontract labour worker: `earned = Σ attendance.total_labour_paid`
  (count × wage recorded per row), `balance = max(0, earned − paid)`.
  Every contract mutation (create/shift/reassign/price/progress) writes a
  `SubcontractEvent` for the stage event ledger.
- **Outputs:** subcontractor list with payable/advance badges, payment
  receipts, ledger page, attendance & labour-attendance pages, stage shift
  between company/subcontractor execution, events rebuild tool.

### 5.8 Materials & Purchases V1 (legacy, `routes/materials.py`)

- Simple `Material` catalogue + `Purchase` rows (project/stage, qty × rate =
  total, supplier name, purchase/return entry types) and `MaterialUsage`
  (qty × rate = total consumed).
- **Stock:** `purchased − used` per material (`/hdc/api/material_stock/<id>`).
- Still feeds `Project.total_material_cost` for legacy data; day-to-day work
  uses Purchase V2 below.

### 5.9 Purchase V2 — suppliers, POs, deliveries, usage, stock (`routes/purchase_v2.py`, `routes/api_purchase.py`, `services/purchase.py`)

- **Inputs:** suppliers (name/phone/address), materials (name/unit), purchase
  orders (supplier × material × unit_price × quantity, payment status,
  challan no), deliveries (PO → project/stage, quantity, person), usage logs
  (material + scope + quantity, cost auto-priced), stock transfers between
  projects/stages, supplier payments.
- **Processing:**
  - **Delivered** = Σ deliveries (per material, optionally per project/stage).
  - **Used** = Σ usage logs (same scope).
  - **Available = max(0, delivered − used)** — you cannot log usage of stock
    that was never delivered to that scope; the UI offers only POs with
    remaining scope quantity (`_purchase_v2_scope_remaining_map`).
  - **Weighted unit cost** = `Σ(purchase qty × unit price) / Σ purchased qty`
    per material; usage `cost = quantity × weighted cost`.
  - **Supplier balance** = Σ debits (purchases) − Σ credits (payments) from
    `SupplierLedger` (kept in sync automatically, with a repair tool).
  - Integrity report flags: usage > delivered, delivery > ordered,
    per-material and global (shown in the purchase dashboard).
- **Outputs:** stock KPIs page (purchased/delivered/used/available per
  material & scope), supplier ledger + balances, delivered-quantities page,
  usage page, JSON API for the whole flow (`/api/v2/purchase/*`).

### 5.10 Expenses & Alerts (`routes/expenses.py`)

- **Inputs:** expense (project required, stage optional, category, amount,
  date, remarks; tip expenses additionally carry `tip_worker_id`).
- **Processing:** soft void; category manager; every expense mirrors to
  accounts (`expense_general` / `expense_material` by category).
- **Alerts** (`hdc_alert` + `_refresh_alerts` in reporting service): generated
  warnings (e.g. over-budget/overdue conditions) shown in the navbar badge and
  resolvable from `/hdc/alerts`.

### 5.11 Office Management (`routes/office.py` + `services/ledger.py`)

- **Inputs:** staff (code, role, **monthly salary**), daily attendance
  (present / absent / weekly_leave), allowances (category + monthly amount +
  effective date), ledger rows (advance / payment / adjustment / settlement),
  office expenses.
- **Processing (`_office_staff_ledger_snapshot`):**
  ```
  per_day_basic  = monthly_salary / days_in_that_month
  per_day_allow  = Σ active allowances (effective ≤ date) / days_in_month
  units          = present=1, weekly_leave/leave=1, absent=0
  month_earned   = units × (per_day_basic + per_day_allow)
  total_earned   = same formula over ALL attendance rows, each month priced
                   with its own day-count and allowance-effective-date
  balance        = total_earned − advances − payments − settlements
  (tips tracked separately, gratis — same rule as workers)
  ```
  Every staff advance/payment/tip **auto-mirrors to an `OfficeExpense`** row
  (category "Office Staff Advance / Tip / Office Salary Payment") so office
  cash outflow appears in office expense reports; voiding the ledger row voids
  the mirrored expense.
- **Outputs:** staff ledger with running balance, payment receipts, attendance
  sheet, allowance manager, office expenses page, monthly vs all-time earned.

### 5.12 Personal Management (`/hdc/personal-management/*`)

- Personal expenses with beneficiary (worker / staff / other), categories,
  soft void — mirrors to accounts as non-project `party_payment` payouts.

### 5.13 Unified Accounts (`routes/accounts.py`, `routes/api_accounts.py`, `services/accounts.py` — the largest module, 3,700 lines)

The financial backbone. **Every** money event elsewhere in the app posts here
automatically (worker payment/advance/tip, subcontractor payment, supplier
payment/credit, owner receipt, office salary, expense, personal expense) with
a unique `(source_type, source_id)` so a source can be voided/edited/updated
exactly once.

- **Inputs:** accounts (company/cash/bank/person/vendor/client, opening
  balances, bank fields, auto-generated party accounts), transactions
  (date, amount, type, from-account, to-account, executed-by, project/stage
  tags, party, category, note, reference, group id).
- **Processing:**
  - `account balance = opening + Σ incoming − Σ outgoing` (non-void txns,
    computed in bulk SQL).
  - **Overdraft block:** a transaction that would take a
    company/cash/bank account below zero is rejected (also simulated across
    grouped multi-row transactions and on edits).
  - `type` matrix (`_account_intent_field_matrix`) drives which fields are
    required per transaction type (e.g. wage payments require worker,
    material purchases require supplier/project…).
  - Dashboard KPIs: company-owned total, cash, bank, receivables (positive
    person/vendor/client balances), payables breakdown, income/expense/net
    cashflow for any date range, split into labour/material/purchase heads.
  - **Reconciliation** compares every source row against its mirrored txn
    (amount/date/void-state) and lists mismatches; **forensic report** lists
    orphans and duplicates; running balances per account ledger.
  - Edit / void / reverse (group-aware) with source-row sync both directions.
- **Outputs:** accounts list with balances, per-account ledgers with running
  balance, transaction receipts, entries screen, reconciliation page, KPI
  drill-downs, JSON API (`/api/accounts/*`).

### 5.14 Reports & exports (`routes/reports.py`)

- **At a glance** (`/hdc/reports/glance`): one row per project — contract,
  received, labour, material, subcontract, expenses, total cost, profit,
  receivable.
- **Exports:** profitability XLSX, salary XLSX, materials XLSX, per-project
  CSV / XLSX / printable PDF report (project report data built by
  `services/reporting.py`), stage **event ledger** (who changed what/when).

### 5.15 Settings, backups, maintenance (`routes/settings.py`, `services/backups.py`, `core/admin.py`)

- Create **zip backup** (DB + instance files), list/download/restore backups,
  prune to latest N, temp-artifact cleanup.
- Maintenance run (vacuum-ish integrity work), guarded data wipe targets
  (admin-only, CSRF-protected), `check_db_safety` rules documented in-app.

### 5.16 Audit trail & row traceability (`services/audit.py`, `services/actors.py`)

- **Two logs:** `ActivityLog` (human-readable actions) and `UserActivity`
  (structured create/update/delete/login events with changed-field maps),
  written via SQLAlchemy session events so nothing is missed.
- **Row traceability:** every list row renders with `hdc_row_attrs`
  (`data-entity` / `data-id`); the page queries `/hdc/api/row_actors` to show
  **who entered/last-changed each row**, voided rows grey out with their
  reason (see `ROW_TRACEABILITY.md`).

---

## 6. Deployment

**Target:** PythonAnywhere (free tier compatible), single web app.

```
GitHub main ──push──► GitHub webhook ──POST /deploy──► deploy_hook.py
                                                          │ git pull --ff-only
                                                          ▼
                                              touch WSGI file ⇒ PA reloads
                                                          ▼
                                wsgi_dispatch_snippet.py (the WSGI file)
                                  ├─ /deploy, /deploy/health, /deploy/status → deploy_hook.py
                                  └─ everything else → hdc.app.create_app()
```

- **Local/other hosts:** `wsgi.py` = `create_app()` for gunicorn;
  `HDC_DB_PATH` / `HDC_INSTANCE_DIR` / `HDC_SECRET_KEY` / `HDC_ENV=prod` /
  `HDC_BOOTSTRAP_ADMIN_PASSWORD` come from the environment (`.env` supported).
- **One-time install:** `python3 ops/pythonanywhere/install_deploy_hook.py`
  detects username/repo/WSGI paths, generates `deploy_secret.txt` (0600,
  gitignored), writes `/var/www/<user>_pythonanywhere_com_wsgi.py` from the
  snippet (backing up the old one, preserving virtualenv/env lines), and
  prints the exact webhook values to paste into GitHub. Modes: `--check`,
  `--dry-run`, `--force`, `--print-secret`, `--restore`.
- **Data safety:** code and data never mix — `*.db`, backups, instance files
  are gitignored and `scripts/check_db_safety.py` (CI-enforced) fails any
  commit that tracks a database/archive, so a deploy can never overwrite the
  live database.
- **Lazy app import:** the WSGI dispatcher imports the real app on first
  request and *defensively* — if the app is broken, `/deploy` still answers
  with the log, which is exactly the page you need when the site is down.

---

## 7. The GitHub webhook

`deploy_hook.py` — a stdlib-only WSGI mini-app mounted at `/deploy`:

1. **GET `/deploy` (or `/deploy/health`, `/deploy/status`)** — no secret
   needed: prints detected repo/WSGI/secret/branch state, git state
   (branch, HEAD, clean/dirty) and the last 8 deploy log lines.
2. **POST `/deploy`** (GitHub push):
   - Requires `deploy_secret.txt`; 503 with fix instructions if missing.
   - Verifies `X-Hub-Signature-256` as an **HMAC-SHA256 of the raw body**
     against the secret, using `hmac.compare_digest` (constant-time);
     401 on mismatch — unsigned or forged posts are rejected.
   - Payload `ref` must be `refs/heads/main` (branch overridable) — other
     branches are acknowledged and ignored; an undecodable payload still
     deploys (the pull itself is idempotent).
   - Runs `git pull --ff-only origin main` in the repo dir; on failure
     returns 500 with an **exact fix command** (stash hint for dirty tree,
     reset hint for diverged checkout, network hint for DNS/auth).
   - On success: logs `OK deployed <sha>: <summary>` to `deploy.log` and
     **`os.utime()`s the WSGI file** — PythonAnywhere watches that mtime and
     reloads the app. That touch *is* the deploy.
3. Everything is logged to `<repo>/deploy.log`; GitHub's "Recent Deliveries"
   shows the same outcomes from the other side.

Failure modes covered: missing secret, bad signature, wrong branch, dirty
working tree, diverged history, missing WSGI file (pull succeeds but reload
is skipped with a warning), git absent.

---

## 8. CI pipeline

`.github/workflows/ci.yml` runs on every push/PR: Python 3.11 → install deps →
`compileall` → **layer check** (`check_layers.py`: models↛routes etc.) →
**DB-safety guard** → full unittest suite (127 tests incl. deploy-hook and
labour-audit regressions) → frontend organisation check → `node --check` on
every JS file → **fresh-database smoke flow** (`smoke_worker.py` boots the
app on a throwaway DB and exercises the core flows).

---

## 9. Calculation quick-reference

| # | Where | Formula |
|---|---|---|
| 1 | Daily wage | `rate × min(1, h/8) + OT_h × rate/8` |
| 2 | Hourly wage | `rate × (h + OT_h)` (straight-time OT) |
| 3 | Per-sqft wage | `rate × qty_sqft` |
| 4 | Regular vs OT split | first 8 h of the **day** regular, remainder OT |
| 5 | Historical rate | latest `WorkerRate` with `effective_from ≤ work_date` |
| 6 | Worker balance | `earned − advances − payments − settlements` (tips excluded) |
| 7 | Payroll net | `max(0, Σ window wages − Σ window advances)`; overflow ⇒ "carried fwd" |
| 8 | Stage value | `lump` or `(rate − discount) × qty`; frozen originals kept |
| 9 | Project contract | `Σ stage values` → fallback `sqft×rate` / `lump_sum` |
| 10 | Project cost | labour + material + expenses + subcontract(cash-cleared) |
| 11 | Project profit | `owner_contract_value − total_cost`; dashboard subtracts office expenses globally |
| 12 | Subcontract payable | `min(contract×progress%, contract×(1−retention%))` |
| 13 | Subcontract balance | `max(0, payable − cleared)`; negative ⇒ advance |
| 14 | Sub-labour balance | `max(0, Σ(count×wage) − paid)` |
| 15 | Material V2 available | `max(0, delivered − used)` per scope |
| 16 | Weighted material cost | `Σ(qty×price)/Σqty`; usage cost `= qty × weighted` |
| 17 | Supplier balance | Σ debits − Σ credits |
| 18 | Office staff earned | per month: `units × (salary + allowances)/days_in_month`, priced per historical month |
| 19 | Office staff balance | `total_earned − advances − payments − settlements` (tips excluded) |
| 20 | Account balance | `opening + incoming − outgoing` (non-void) |
| 21 | Estimation row | `rate` (lump) or `rate × qty` |
| 22 | Attendance day value | `1` if day total ≥ 8 h else `0` |

---

## 10. Verification evidence

Run during this review (2026-09-13):

- **127 unit tests + 3 subtests pass** (`python -m pytest tests/`), including
  the labour-audit regression suite and deploy-hook tests.
- **Read-only audit CLI** (`scripts/labour_audit.py`) correctly flags all 19
  seeded CRITICAL/HIGH defects in the generated fixture DB and exits 1.
- **Independent end-to-end scenario** (fresh DB, real code paths): 22/22
  checks passed — daily/hourly/per-sqft wages incl. OT, half-days, backdated
  rate changes keeping historical days at the old rate, tip reconciler
  idempotency, tips-excluded balance agreement between model and service,
  payroll window math, legacy-attendance migration exactly-once, and
  automatic schema healing of a dropped column on reboot.

---

## 11. Suggestions (prioritised)

### High value, low risk

1. **Adopt Alembic (or a schema-version table) for migrations.** The hand-run
   `ALTER TABLE` list works and is disciplined, but it depends on developers
   remembering to add a line per new column, and `db.create_all()` will *not*
   add columns to existing tables. A tiny `schema_version` check that fails
   loudly on mismatch (or Alembic autogenerate) removes the whole class of
   "fresh DB works, upgraded DB missing a column" risk.
2. **Store money as `Numeric(14,2)` (or integer paisa) instead of Float.**
   All amounts are `Float` today; cents-level drift is possible across
   aggregates. `Decimal` columns + a rounding helper at boundaries would make
   totals exact. (SQLite stores numerics fine; SQLAlchemy handles conversion.)
3. **Finish LABOUR_AUDIT #11:** `_recalculate_attendance_day` rewrites
   check-in/out to synthetic times (09:00 + running hours). Preserve the real
   timestamps (store the sequence in a separate column) — this also removes
   the edge case where a 24 h day pushes an entry to next-day midnight and it
   drops out of its own day on the next recalc.
4. **Replace the 12-second duplicate window (#14) with idempotency keys** —
   a hidden form token (or hash of worker+type+amount+date+notes with a
   unique index) would stop double-submissions at any speed, not just fast
   ones.
5. **Backfill a `WorkerRate` baseline for every existing worker** (closes
   #16): a one-off bootstrap step `INSERT … SELECT` from current profile
   values guarantees no worker's history can be rewritten by a future profile
   edit.

### Worth doing

6. **Off-site database backups.** Backups are excellent but live next to the
   DB on the same host. A weekly download reminder (or `rclone`/S3 push from
   a scheduled job) protects against host loss. PythonAnywhere free accounts
   can at least email a backup.
7. **Multi-worker hardening:** the login throttle is per-process memory (fine
   on PA's single worker; noted in config) and SQLite is single-writer. If
   concurrency grows, move sessions/throttle to the DB or a shared store.
8. **Performance:** the Workers list evaluates per-worker property sums
   (N+1). `_worker_payable_snapshot`/`_apply_aggregated_project_costs` style
   bulk queries for the workers list would keep pages fast as history grows.
   Add `INDEX hdc_labour_ledger(worker_id, entry_type, is_void)`.
9. **Per-sqft guard rail:** if a per-sqft worker is marked present with
   `qty_sqft = 0`, they earn 0 silently. A save-time warning (or requiring
   qty for `per_sqft` workers on the bulk sheet) would prevent accidental
   zero-wage days.
10. **Estimation store consolidation:** the legacy `/project-estimation` JSON
    file store duplicates the DB `Estimation` model. Migrating the JSON rows
    into the DB (one-time script) removes a file that sits outside backup
    zip logic consistency.
11. **Deploy status page:** GET `/deploy` prints paths and git state without
    a secret. Low risk (informational only), but gating it behind the same
    secret (or a random status URL) is a one-line hardening.
12. **Payroll advance semantics:** payroll nets only *in-window* advances and
    correctly shows "carried fwd", but a payroll payment does not deduct
    from the *global* worker balance view. A note on the salary card linking
    to the worker ledger would avoid "why is the balance not zero?" questions.

### Nice to have

13. Add a `SKIP` lock/retry around webhook deploys (two rapid pushes can race
    two pulls; `--ff-only` makes this safe today but a simple lockfile would
    serialise them).
14. Expose `advance_carried` and `tip_paid` on the printable salary card for
    full transparency to workers.
15. Add OpenAPI documentation for the two JSON API surfaces
    (`/api/v2/purchase/*`, `/api/accounts/*`) — they are consistent enough to
    document mechanically.

---

*Report generated from a full static trace of every module plus live
end-to-end execution tests. Scratch verification scripts were kept outside the
repository; no production code was modified during this review.*
