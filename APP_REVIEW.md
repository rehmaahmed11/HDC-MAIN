# HDC ERP — full read-through notes

Repo extracted from `all_files.zip` (146 entries) on 2026-09-11. Notes below are the result of
reading the whole app: `hdc_erp.py` (20,101 lines), 82 templates, static JS/CSS, and the live
SQLite database (`hdc_instance/hdc_erp.db`, 54 tables).

---

## 1. What the app is

**Hadi Design & Construction (HDC) — single-file construction ERP** for a Pakistani builder
(all money PKR, all timestamps `Asia/Karachi`).

- Stack: Flask 3 + Flask-Login + Flask-SQLAlchemy + SQLite (WAL) + openpyxl; gunicorn/`wsgi.py`
  for PythonAnywhere (`hdc_erp.py` is importable as `app`; `python hdc_erp.py` also runs a dev
  server on `$PORT`/5000, bound `0.0.0.0`).
- Everything lives in one module: **54 models/tables, 189 endpoints, 38 JSON API endpoints,
  82 Jinja templates**. Templates + static are mounted as `templates/hdc` and `/hdc_static`.
- Two subsystems were bolted on top of the original project/stage core:
  1. **Unified Accounts** — a double-entry-ish ledger (`Account` + `AccountTransaction`) that every
     money movement must post into.
  2. **Purchase V2** — supplier → purchase order → delivery → stage-scoped material usage with
     FIFO-ish remaining-qty maths, replacing the older `Material`/`Purchase` pair (which is now
     empty in production but still drives some cost rollups — see §7).

## 2. Directory layout

```
hdc_erp.py                    entire backend (models, business logic, routes, exports, bootstrap)
wsgi.py                       PythonAnywhere entrypoint; hard-codes HDC_DB_PATH to a Windows path
requirements.txt              Flask, Flask-Login, Flask-SQLAlchemy, SQLAlchemy, openpyxl, gunicorn
templates/hdc/*.html          82 pages; base.html = sidebar + topbar + dark/light theme
static/hdc/css/hdc.css        697 lines, CSS-variable theming (light/dark)
static/hdc/js/hdc.js          sidebar/theme + generic typeahead helpers
static/hdc/js/project_estimation.js   estimation sheet UI
hdc_instance/hdc_erp.db       LIVE production database (+ -wal/-shm in the zip)
hdc_instance/backups/         zip backups (db + xlsx export), pruning, restore
hdc_instance/stage_drawings/  uploaded stage drawings
attached_assets/              empty
backupprevios.zip             older snapshot of the same tree (April/May state)
.vimrc/.bashrc/.profile/.gitconfig/.pythonstartup.py/.cache/.local/.ipython/.virtualenvs
                              PythonAnywhere home-dir junk that got zipped along with the app
```

## 3. Configuration & startup (hdc_erp.py:29-108, 19700-20101)

- `_load_local_env()` reads an optional `.env` next to the file (only sets vars not already in env).
- Paths resolve through `_resolve_path()`: `HDC_INSTANCE_DIR`, `HDC_DB_PATH` override; otherwise
  `hdc_instance/hdc_erp_integrated.db` if present, else `hdc_instance/hdc_erp.db`.
  ⚠️ `wsgi.py` sets `HDC_DB_PATH` to `e:\WORKINGS\current working\rep hdc\hdc\hdc_instance\hdc_erp_integrated.db`
  — a Windows path. It only works on the machine it was written for; on Linux it becomes a relative,
  literal directory name.
- `app.secret_key = 'hdc_local_username_password_session_key'` — hard-coded (see §7).
- SQLite pragmas on every connection: `foreign_keys=ON`, `journal_mode=WAL`, `synchronous=NORMAL`,
  `temp_store=MEMORY`, `cache_size=-20000`.
- `_bootstrap_hdc()` runs at import (guarded by a lock + `_HDC_BOOTSTRAP_DONE`) and on every request
  via `_ensure_db_runtime_ready()` self-heal:
  `create_all()` → `_run_migrations()` → per-feature schema heals (`_ensure_*_schema`,
  `_ensure_table_columns_sqlite` = ALTER-TABLE auto-heal) → runtime-flag table → legacy
  `.done` marker migration → `_migrate_attendance_to_time_entries()` →
  `_reconcile_all_time_entries_once()` → unique indexes → seed admin user + default trades and
  expense categories → accounts backfills.
  Migrations are therefore **self-healing and idempotent, flag-gated in `hdc_runtime_flag`** — there
  is no Alembic.
- Bootstrap admin: `HDC_BOOTSTRAP_ADMIN_USERNAME` (default `admin`) with
  `HDC_BOOTSTRAP_ADMIN_PASSWORD`/`HDC_DEFAULT_ADMIN_PASSWORD` (default **`Admin@1234`**), which must
  pass `_is_strong_password()` or startup raises.

## 4. Domain model (grouped)

| Area | Models / tables |
|---|---|
| Auth & audit | `HDCUser` (admin/manager/accountant), `UserActivity` (auto audit), `ActivityLog` (human-readable `log_action`) |
| Projects | `Project`, `StageDefinition`, `Stage`, `StageRateHistory`, `StageDrawing`, `Estimation`, `EstimationStage`, `CustomFormula` |
| Workers | `Worker`, `WorkerTrade`, `WorkerRate`, `LabourRateHistory`, `Attendance`(legacy), `TimeEntry`, `AttendanceDay`, `AttendanceMark`, `LabourLedger`, `PayrollRun`, `PayrollItem` |
| Office staff | `OfficeStaff`, `OfficeStaffAttendance`, `OfficeStaffLedger`, `StaffAllowance`, `AllowanceCategory`, `OfficeExpense`, `OfficeExpenseCategory` |
| Subcontract | `Subcontractor`, `SubcontractPayment`, `SubcontractAttendance`, `SubcontractLabourAttendance`, `SubcontractLabourWorker`, `SubcontractLabourPayment`, `SubcontractEvent` (append-only history) |
| Materials v1 | `Material`, `Purchase`, `MaterialUsage` |
| Materials v2 | `Supplier`, `MaterialV2`, `PurchaseV2`, `Delivery`, `UsageLogV2`, `SupplierLedger` |
| Money | `Account`, `AccountTransaction`, `OwnerPayment`, `Expense`, `ExpenseCategory`, `PersonalExpense`, `PersonalExpenseCategory`, `Alert` |

Conventions that pervade the code:

- **Soft delete everywhere**: `is_void` + `void_reason` + `voided_at`. Nothing is destroyed by the UI.
- `activity_at` is the "real" event time used for ordering/receipts; `created_at` is row creation.
- Cached aggregates: model properties read `self._agg_*` first, so a route can bulk-load
  `_aggregate_project_costs()` / `_aggregate_stage_costs()` and avoid N+1 queries.
- `Project.owner_contract_value` prefers the sum of stage contract values, falling back to
  `sqft × rate` / lump sum. `Stage.contract_value` = `Lump Sum` value or `(rate − discount) × sqft`,
  with `original_*` columns + `StageRateHistory` preserving re-pricing.
- Money formatting via `_flt()` accepts `"2,500"`; `_fmt_pkt` context function for templates.

## 5. The three engines

### 5.1 Timekeeping → wages (`hdc_attendance`, ~7799-8440)
- Single entry point `/hdc/timekeeping` (alias `/hdc/attendance`) with a **bulk day sheet**: per
  worker status (`present/absent/leave/not_assigned`), optional **multi-project hour allocations**
  (`allocations_<wid>` JSON), hours ≤ 24 enforced, day-split at the 8-hour boundary
  (`regular_hours` / `overtime_part`) across allocations.
- Wage rule `_calc_time_wage(worker, hours, ot, qty_sqft, work_date)`:
  - `per_sqft` → rate × qty_sqft; `hourly` → rate × hours;
  - `daily` → `base × min(1, hours/8) + ot × (base/8)` (half-day = half pay, OT at 1/8 daily rate).
- Rates are **point-in-time correct**: `_worker_rate_on()` picks `WorkerRate.effective_from ≤ date`,
  falling back to the worker profile.
- Each `TimeEntry` is mirrored 1:1 into a `LabourLedger(entry_type='work')` row
  (`_sync_work_ledger_for_time_entry`) — ledger = worker's money view, time entry = effort view.
- Duplicate/repair machinery: `_has_recent_duplicate()` (12-second window) blocks double submits;
  `_reconcile_worker_time_entries()` merges OT saved as a stray second row and voids exact
  duplicates; `_repair_worker_work_ledger_links()` re-links or voids orphaned `work` ledger rows.
  Both run once at boot (flag-gated) and on demand via admin "Run maintenance".
- `AttendanceDay` is a per-worker/day rollup (`total_hours`, `day_value` = 1 if ≥ 8h, entry_count).
- Legacy `Attendance` still supported: every cost rollup uses
  `Attendance LEFT JOIN TimeEntry ON attendance_id WHERE TimeEntry.id IS NULL` + all `TimeEntry`,
  so migrated rows are never double-counted. Production has `hdc_attendance = 0 rows`,
  `hdc_time_entry = 6421` (only 1801 active) → migration complete, heavy void churn from re-submitting days.

### 5.2 Unified Accounts (`hdc_erp.py:14030-17800`)
- `Account.type ∈ (company, cash, bank, person, vendor, client)`, mapped from UI *group*
  (`company` / `project_in_flow` / `credit_debit`) × *mode* (`cash`/`bank`) by `_resolve_account_type`.
- Balance = `opening_balance + Σincoming(to_account) − Σoutgoing(from_account)` over
  non-void txns (`_account_balances_query`).
- 14 transaction `type`s ↔ 8 `category` values, with two deterministic maps
  (`_ACCOUNT_TXN_TYPE_DEFAULT_CATEGORY`, `_ACCOUNT_TXN_CATEGORY_DEFAULT_TYPE`) so a caller can never
  send a contradictory pair; legacy form verbs (`receive_from_project`, `pay_to_credit_debit`, …)
  alias onto canonical types.
- `_account_intent_field_matrix()` is the contract the UI and API share: which of
  from/to/project/stage/related-entity each type needs. `_validate_account_transaction_payload()`
  enforces it (dates `YYYY-MM-DD`, non-zero amount, stage must belong to project, some types need
  `to_account`, expense types need `to_account` **or** `party_name`, `project_income` needs project,
  `expense_material/purchase`→supplier, `expense_wage/payroll/advance_to_person`→worker,
  `expense_subcontractor`→subcontractor, `expense_general` requires project **and** stage).
- Guards, in order, inside `_create_accounts_transaction_with_sync()`:
  1. 12-second duplicate detection on the full tuple;
  2. **party_name hard-bind**: if a typed party name disagrees with the selected entity's canonical
     name the write is *rejected* (comment calls this "the single most important guard against the
     1 person but 2 ledgers class of mistake");
  3. **overdraft block** — negative balance refused for `company/cash/bank` accounts only
     (`_check_overdraft_block`, `_check_overdraft_block_replace` re-adds the rows being replaced);
  4. **overpayment splitting** — `_compute_excess_split()` carves `amount` into
     `payment + tip + advance`; tip can't exceed the excess, and tip+advance must equal it
     (within 0.01); auto-advance when only excess exists.
- **Two-way sync with source documents**: `_accounts_post_*` / `_accounts_upsert_*` helpers write the
  ledger row for expenses, labour ledger, owner receipts, supplier credits, subcontract payments,
  office-staff ledger, office expenses, personal expenses; `source_type`+`source_id` keep them glued.
  `group_id` (uuid4) groups the multi-row legs of one intent, so `_accounts_toggle_transaction_void_state`
  voids/restores the whole group and `_sync_source_row_void_state` pushes the state back onto the source
  row (void is mirrored both directions).
- Auto-generated party accounts (`_accounts_party_account`, `auto_generated`/`auto_source`) keep one
  account per canonical name; `_run_accounts_backfill()` (one-time + manual button) retro-creates
  ledger rows for pre-existing history.
- Health tooling: `_accounts_reconciliation_snapshot()` (paid purchases with no ledger posting, stale
  postings, owner payments without receipt account, …) and `_accounts_forensic_report()` (+ missing
  scope tags), surfaced at `/hdc/accounts/reconciliation` and `/api/accounts/forensic_report`.
- Manual entry UI (`accounts.html`, 2251 lines + `/api/accounts/*`) does live balance refresh,
  type-aware field show/hide, personal-party suggestions, and a printable receipt
  (`transaction_receipt.html`, 647 lines, amount in words via `_amount_to_words`).

### 5.3 Purchase V2 (`hdc_erp.py:10022-11250`, `17600-18530`)
- Flow: `PurchaseV2` (supplier × material × qty × unit price, `challan_no`, paid/unpaid) →
  `Delivery` (qty onto a **project/stage** scope) → `UsageLogV2` (qty consumed, cost = qty × PO
  unit price) → `SupplierLedger` debit/credit + Accounts posting.
- Stock is **scope-based, not warehouse-based**: `_purchase_v2_scope_remaining_map()` walks purchases
  oldest-first, `remaining = delivered − linked usage`, floors at 0, then absorbs legacy usages with
  `purchase_id IS NULL` by consuming from the earliest POs (`carry` loop). Per-PO availability is
  checked before saving usage/edit (`_purchase_v2_available_in_scope_qty`, `_material_v2_available`).
- Unpaid PO ⇒ one active `SupplierLedger(debit)` row (`_sync_purchase_v2_ledger` de-dupes and voids
  extras); marking paid voids the debit and posts the cash leg (`_accounts_upsert_purchase_paid_txn`)
  and `_sync_supplier_po_payment_status()` keeps status derived from cash.
- `_purchase_v2_integrity_report()` + `/api/v2/purchase/recalculate-stock` for repair, plus a
  delivered→project "transfer" endpoint (`/hdc/purchase-v2/delivered/transfer`) to move a delivery
  between stages/projects.
- Full JSON twin of the UI at `/api/v2/purchase/*` (suppliers, materials, purchases, payments,
  deliveries, usage, ledger, stock, kpis) — used by the SPA-ish pages and easy to script against.

## 6. Cross-cutting behaviour

- **Audit**: SQLAlchemy `after_flush` hook (`_capture_user_activity_after_flush`) auto-records
  create/update/delete of *any* model with a before/after `changed_fields` JSON, request path and
  actor. Noise is suppressed (`_AUDIT_EXCLUDE_TABLES`, `_AUDIT_INTERNAL_TABLES`,
  `_AUDIT_NOISY_UPDATE_FIELDS` for churning time-entry fields) and human-readable summaries are
  special-cased for TimeEntry/AttendanceMark. `_audit_paused()` context manager suppresses it for
  system reseed work. Separately, `log_action()` writes operator-sentence rows
  ("Bilal created purchase #12 …") shown in **Event Recorder** (`/hdc/event-recorder`).
- **Subcontractor event log**: `SubcontractEvent` is append-only and rebuilt-able
  (`/hdc/subcontractor/<sid>/events/rebuild`, admin-only) via `_ensure_subcontract_baseline_events()`.
  Stage↔subcontractor pointers are reconciled in both directions by `_reconcile_subcontract_links()`.
- **Progress/retention**: subcontract amount owed = contract value × `work_done_percentage`, with
  `retention_percentage`; `total_cleared` (cash) vs `contract_balance` (committed) are deliberately
  different numbers — cost rollups use cash-cleared, not commitments.
- **Payroll** (`/hdc/payroll/generate`): window-based `PayrollRun`/`PayrollItem` (gross − advances =
  net), then per-worker or pay-all actions that create `LabourLedger payment` rows *and* Accounts
  postings in the same transaction, each capped at the remaining payable, tracked by the
  `Payroll run #N ` note prefix, with salary-card print pages.
- **Alerts** (`_refresh_alerts`): stage over-estimate, stage delayed past `end_date`, project over
  budget; keyed (`stage_over_{id}`) so they don't duplicate; unread count injected into every page.
- **Backups** (`/hdc/settings`): zip of DB + full-workbook XLSX export, list/prune-keep-latest,
  download, restore (extract → swap → forced logout), temp-artifact cleanup, plus the destructive
  `_WIPE_TARGETS` / `_wipe_selected_targets()` selector (12 named targets, `PRAGMA foreign_keys=OFF`,
  `DELETE FROM`, re-bootstrap, `VACUUM`).
- **Reports**: profitability / salary / materials CSV, per-project CSV/XLSX/PDF-ish print page,
  `/hdc/reports/glance` (30-day + today view with receivables collection %), KPI drill-down pages
  (`/hdc/kpi/<metric>`, `/hdc/accounts/kpi/<metric>`) that expand any dashboard tile into rows.
- **Estimation**: `/hdc/estimation` calculator tabs (concrete with grade ratios + dry factor + bags +
  water by w/c, steel `(d²/162)×L`, custom AST-safe formulas via `_safe_eval`) and `/hdc/project-estimation`
  sheet → `save` → `convert/<est_id>` which creates a Project with stages and a generated
  `HDC-000NN` code (`_next_project_code`, `/hdc/api/next_*` helpers for worker/staff codes).
- **Security posture**: all 189 endpoints except `hdc_login` and `hdc_root` are `@login_required`;
  CSRF token in `<meta>` + hidden field, enforced in `@app.before_request` for POST/PUT/PATCH/DELETE
  — **but `request.is_json` is exempt**, and `_admin_only()` gates only 24 handlers, so `manager`
  and `accountant` roles are effectively equivalent to admin on most routes (role only hides sidebar
  links + 2 flows). scrypt password hashing, strong-password rule for new users.

## 7. Findings / risks (each verified against the shipped code or DB)

1. **`/hdc/reports/export/profitability` understates cost — profit is overstated.** That route
   (line 11928) never calls `_apply_aggregated_project_costs()`, so `Project.total_material_cost`
   falls back to the model property, which sums **legacy `hdc_purchase` only** — and
   `hdc_purchase` has **0 rows** in production (all material is in Purchase V2). Measured on
   `HDC-00001`: CSV says `Material Cost 0, Total Cost 749,064.83, Net Profit 9,178,435.17`; the app
   screen says `Total Cost 4,395,794` and material `3,647,229` (= `hdc_usage_log_v2` cost).
   Same for `gross_margin`/per-sqft figures in CSV paths. `Stage.stage_material_cost` has the same
   v1-only blind spot.
2. **Voided rows leak into property-based totals.** `Project.total_expense_cost` sums
   `self.expenses` with **no `is_void` filter**, and `Project.total_material_cost` sums
   `self.purchases` the same way; the aggregate path filters them. Project 1: property `95,659.50`
   vs aggregate `95,159.50` (one voided 500 PKR expense). Any screen that renders
   `p.total_*` without applying the aggregation can disagree with the KPI/dashboards.
   Worker-side properties (`Worker.total_earned/total_advanced/total_paid/total_settled`) *do* filter
   `is_void`, so the inconsistency is per-model rather than a global rule.
3. **Fixed/hard-coded secrets**: `app.secret_key` literal, default admin password `Admin@1234`
   documented in code; session cookie has no `SESSION_COOKIE_HTTPONLY`/`Secure`/`SAMESITE` config,
   no rate limit on `/hdc/login`, and no CSRF check for JSON bodies (§before_request exemption) —
   which matters because 38 `/api/*` endpoints accept JSON POSTs that mutate money rows.
4. **Role model is cosmetic.** `HDCUser.role` is stored and used in exactly 3 places
   (`_admin_only()` × 24 routes, one subcontractor rebuild check, sidebar `{% if %}`); a
   `manager`/`accountant` can still wipe data, restore backups and edit users by URL if they know
   the endpoints, since those guards are only `_admin_only()` on some routes. Verify which of
   `/hdc/settings`, `/hdc/users`, `/hdc/admin/maintenance/run`, `/hdc/api/v2/*` you actually want gated.
5. **`wsgi.py` DB path is a Windows absolute path** — the app will silently create a
   `e:\WORKINGS\...`-named file on Linux unless `HDC_DB_PATH` is exported. It also overrides the
   "integrated DB" default, so it can point at a *different* DB than the one you back up.
6. **Zip contains the live DB + WAL, and home-dir junk.** Shipping `hdc_erp.db` with real client
   names/phone numbers, worker ledgers and 23k audit rows in a repo named "MAIN" is a data-leak and
   repo-bloat issue; `.bashrc/.vimrc/.gitconfig/.cache/.local/.virtualenvs` shouldn't be in the tree
   (a `.gitignore` has been added to keep DB/backups/archives out of git).
7. **WAL checkpoint side-effect of this extraction**: reading `hdc_instance/hdc_erp.db` while
   `-wal`/`-shm` were present folded the log into the main DB file and removed them
   (`integrity_check: ok`, row counts unchanged: time_entry 6421 / labour_ledger 6907 /
   account_txn 1380 / user_activity 23431). No data lost, but if you diff against the zip you will
   see the files differ.
8. **Scale/legibility risk from the single file**: 20k lines, 181 `db.session.commit()` sites,
   163 legacy `Model.query.get()` calls (SQLAlchemy 2.0 deprecation warnings on every request),
   80 `except Exception: pass` blocks (some swallow real ledger errors — e.g. the pragma and audit
   hooks are fine, but `_bootstrap_hdc`'s backfills silently rollback), and multi-route decorators
   (`/hdc/attendance` + `/hdc/timekeeping` share one function) make drift easy — as findings 1–2 show.
9. **Performance**: list pages are aggregated (good), but `/hdc/kpi/<metric>`, `/hdc/reports` and
   the Accounts entry page still materialise whole tables (`Project.query.all()`, all
   `TimeEntry`/`LabourLedger` rows per project) and `_account_balance_map()` recomputes every
   account's full history on each call (it is called inside overdraft checks and per-option in
   dropdowns). At 6.4k time entries and 6.9k ledger rows this is fine; at 10× it will hurt.
10. **Data hygiene**: 4,620 of 6,421 `TimeEntry` rows are voided (bulk-sheet re-entry) and
    `hdc_alert`, `hdc_payroll_run`, `hdc_estimation`, `hdc_custom_formula`, `hdc_material*` (v1),
    `hdc_subcontract_attendance` are all **empty** — v1 material and payroll-run features are dead in
    practice while still wired into cost maths and exports.

11. **Cosmetic but noisy**: the source contains 2,296 double-encoded UTF-8 sequences (mojibake like
    `â€”` where section dividers use `—`, and `Ã¢â€â‚¬` blocks around comment rules) — the file is valid
    UTF-8 but the comments/`__doc__` read as garbage, which came from an editor/terminal round-trip on
    PythonAnywhere. A one-off `f.encode('cp1252',errors='ignore').decode('utf-8',errors='ignore')`
    style sweep on comment lines only would clean it. `wsgi.py`/`requirements.txt` also use CRLF.

## 8. Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# NEVER point this at the real DB unless you mean it: bootstrap mutates schema + backfills rows
python hdc_erp.py            # 0.0.0.0:$PORT (5000 default) → /hdc/login
# isolated copy used for this read-through:
cp hdc_instance/hdc_erp.db /tmp/hdc/hdc_erp.db
HDC_DB_PATH=/tmp/hdc/hdc_erp.db PORT=5055 python hdc_erp.py
```
Verified with a scratch copy of your DB: login works and all 30 main pages return 200
(`/hdc/`, projects, stages, subcontractors, workers, timekeeping, expenses, payroll, purchase-v2 ×5,
accounts ×3, reports ×2, office/personal management, estimation, alerts, users, settings,
event-recorder, stage-library, project detail) — plus `/api/v2/purchase/kpis` returns live JSON
(`purchase_total 12,567,202.33`, `unpaid_total 12,567,202.33`, `used_cost 10,031,891.83`,
`delivered_qty 193,491.8`, `used_qty 192,821.3`).

## 9. Production data at a glance (from the shipped DB)

12 active projects (5 Marla → 29 Marla homes, FBM Warehouse, Allied School, Al Riaz Plaza),
77 workers, 11 trades, 18 subcontractors, 4 office staff, 38 v2 materials, 20 suppliers,
181 POs (12.57M PKR, all unpaid), 229 deliveries, 202 usage rows (10.03M PKR),
1,380 account txns across 102 accounts (2 bank, 5 cash, 7 client, 87 person),
131 income txns = 26.35M received, 6,907 labour ledger rows (3.85M work, 2.98M paid, 812k advanced),
timekeeping covering 2026-02-27 → 2026-09-11.
