# HDC ERP — Monolith → Modular Conversion Plan

**Status:** Approved for implementation (this branch implements Phase 1 in full).
**Date:** 2026-09-12
**Source analyzed:** `hdc_erp.py` (20,101 lines), 82 templates, `static/hdc/*`, `wsgi.py`, `requirements.txt`, plus `APP_REVIEW.md`.

---

## Table of contents

1. [Why: what is wrong with the monolith today](#1-why-what-is-wrong-with-the-monolith-today)
2. [Goals & non-goals](#2-goals--non-goals)
3. [Target architecture (modular monolith)](#3-target-architecture-modular-monolith)
4. [Module inventory — exactly what moves where](#4-module-inventory--exactly-what-moves-where)
5. [Dependency rules (what may import what)](#5-dependency-rules-what-may-import-what)
6. [Backend plan](#6-backend-plan)
7. [Database plan](#7-database-plan)
8. [Frontend plan](#8-frontend-plan)
9. [Configuration & environments plan](#9-configuration--environments-plan)
10. [Compatibility strategy (zero-downtime, zero-behaviour-change)](#10-compatibility-strategy-zero-downtime-zero-behaviour-change)
11. [Conversion procedure (reproducible, scripted)](#11-conversion-procedure-reproducible-scripted)
12. [Testing & verification plan](#12-testing--verification-plan)
13. [Rollout & rollback plan](#13-rollout--rollback-plan)
14. [Risks & mitigations](#14-risks--mitigations)
15. [Phase 2+ roadmap (after this conversion)](#15-phase-2-roadmap-after-this-conversion)
16. [Acceptance criteria](#16-acceptance-criteria)

---

## 1. Why: what is wrong with the monolith today

Measured facts about the current code (verified by AST analysis, not estimates):

| Fact | Value | Why it hurts updates |
|---|---|---|
| Backend in one file | `hdc_erp.py` = **20,101 lines / 907 KB** | Any backend change (even a label fix in payroll) requires editing, reviewing and re-deploying the *entire* backend. Merge conflicts are near-certain with >1 developer. A typo anywhere can break app startup everywhere. |
| Route handlers in one file | **189 view functions, 191 `@app.route` decorators** | Frontend page work (add a field to one page) means touching the same file as money-movement logic. No way to review/ship "only the Purchase V2 UI". |
| Business logic mixed with routes | **218 helper functions** interleaved with routes | Copy/paste drift (APP_REVIEW findings #1–#2: profitability export and property totals disagree) because there is no single owning module per calculation. |
| Models in one block | **53 model classes**, lines 368–1585 | Any DB change requires understanding the whole file; schema self-heals (`_ensure_*_schema`) are scattered across the file instead of one migrations home. |
| `db.session.commit()` sites | **181** | Transaction boundaries are invisible; two features can half-commit together. |
| All templates flat | **82 files in one folder** `templates/hdc/` | No ownership per feature; renaming/moving anything is guesswork. |
| All shared JS in one file | `static/hdc/js/hdc.js` (245 lines) | Page-specific JS grows into global scope; one bad line breaks sidebar/theme on every page. |
| Config hard-coded | `app.secret_key` literal; `wsgi.py` hard-codes a **Windows-only** `HDC_DB_PATH` (`e:\WORKINGS\...`) | Cannot run the same code on Linux/PythonAnywhere vs Windows without editing source. Secrets can't rotate without a code change. |
| No app factory | `app = Flask(...)` + `_ensure_bootstrap_once()` execute **at import time** | Impossible to create a test app with a scratch DB without importing (and mutating) the production DB path. This is the #1 reason there are no tests. |
| `request.is_json` CSRF exemption + cosmetic roles | see APP_REVIEW §6–§7 | Security fixes need surgical changes, but every change ships the whole file, so fixes are delayed. |

**In short:** frontend, backend and database code are physically fused. Updating one part always risks the other two, review is all-or-nothing, and testing is impossible. The fix is physical separation into modules with enforced dependency direction — while keeping behaviour 100% identical.

---

## 2. Goals & non-goals

### Goals (Phase 1 — this conversion)

1. **Backend split by domain:** one Python module per feature (projects, workers, accounts, purchase, …) so a change to payroll never touches purchase code.
2. **Database layer split:** one models module per domain + one migrations home; **zero schema changes** in Phase 1 (same 54 tables, same columns).
3. **Frontend split by domain:** templates grouped in per-feature folders; static JS/CSS given per-feature homes with a shared core.
4. **App factory:** `create_app(config)` so dev / test / prod are just configuration, and tests can boot a scratch DB.
5. **Config from environment:** no hard-coded paths/secrets; `wsgi.py` works on any OS.
6. **100% behaviour parity:** same URLs, same endpoint names, same templates rendered, same DB writes. Proven by an automated parity harness (baseline vs refactored).
7. **Backward compatibility:** `import hdc_erp` and `from hdc_erp import app, db, <Model>` keep working via a thin shim, so existing scripts, `wsgi.py`, cron jobs and muscle memory don't break.

### Non-goals (explicitly deferred to Phase 2+)

- ❌ No microservices / no separate frontend repo — this stays a **modular monolith** (one deployable, many modules). Correct first step; extraction later if ever needed.
- ❌ No URL or endpoint renames (would break bookmarks, `url_for`, templates for zero benefit now).
- ❌ No schema changes, no Alembic yet (migrations are only *relocated*, not rewritten).
- ❌ No behaviour/bug fixes (including APP_REVIEW findings #1–#11) — parity first, fixes second, so any regression is attributable.
- ❌ No new features.

---

## 3. Target architecture (modular monolith)

```
┌────────────────────────────────────────────────────────────┐
│                        hdc_erp.py                          │  thin compat shim
│            (re-exports app, db, models, helpers)           │  (old import path keeps working)
└───────────────────────────┬────────────────────────────────┘
                            │ imports
┌───────────────────────────▼────────────────────────────────┐
│                        hdc/  package                        │
│  ┌──────────┐  ┌────────────┐  ┌──────────────────────────┐ │
│  │ config.py│  │extensions.py│  │        app.py            │ │
│  │ env,dirs │  │db, login,   │  │ create_app() factory:    │ │
│  │ secret,  │  │pragmas, CSRF│  │ config→extensions→models │ │
│  │ DB path  │  │context hooks│  │ →services→routes→boot   │ │
│  └──────────┘  └────────────┘  └──────────────────────────┘ │
│  ┌──────────┐  ┌────────────┐  ┌──────────────────────────┐ │
│  │  utils/  │  │  models/   │  │       services/          │ │
│  │ PURE     │  │ 1 file per │  │ 1 file per ENGINE:       │ │
│  │ helpers  │  │ domain,    │  │ accounts, purchase,      │ │
│  │ no Flask │  │ no routes  │  │ timekeeping, payroll…    │ │
│  └──────────┘  └────────────┘  └──────────────────────────┘ │
│  ┌──────────────────────┐  ┌──────────────────────────────┐ │
│  │  routes/ (1 per page │  │  core/ (flags, schema,       │ │
│  │  group, register(app)│  │  bootstrap, admin ops)       │ │
│  └──────────────────────┘  └──────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ templates/hdc/  │  │  static/hdc/     │  │ hdc_instance/    │
│ <domain>/*.html │  │ core.js +        │  │ (unchanged —     │
│ 1 folder/feature│  │ <domain>.js/css  │  │ same SQLite DB)  │
└─────────────────┘  └──────────────────┘  └──────────────────┘
```

**Why a modular monolith and not microservices:** one SQLite DB, one deployment (PythonAnywhere), a 2–3 person team. Microservices would add network/ops failure modes with no payoff. The modular monolith gives 90% of the benefit (independent review, test and deploy *boundaries inside one process*) at ~5% of the cost.

**Why `register(app)` route modules instead of Flask Blueprints (for now):** Blueprint endpoints are forcibly prefixed (`accounts.hdc_accounts`), which would require rewriting every `url_for('hdc_…')` in 82 templates + all Python redirects — a high-risk, zero-benefit rename. `register(app)` keeps **every endpoint name byte-identical** while still giving one-file-per-feature. Blueprint migration (with URL prefixes like `/api/v2`) is a clean Phase-2 follow-up *after* parity is proven.

---

## 4. Module inventory — exactly what moves where

Line counts below are measured from the current file, so reviewers can verify completeness (every one of the 532 top-level statements has exactly one home).

### 4.1 `hdc/config.py` — all configuration in one place (~90 lines)

Moves in: `BASE_DIR`, `_load_local_env()`, `_resolve_path()`, `INSTANCE_DIR`, `STAGE_DRAWINGS_DIR`, `DB_PATH`, `DEV_MODE`, `_ESTIMATION_STORE`, `_DB_STORE`, `_BACKUP_DIR` (+ `os.makedirs` side effects, now inside `ensure_dirs()`).

| Change | Before | After |
|---|---|---|
| Secret key | literal `'hdc_local_username_password_session_key'` | `HDC_SECRET_KEY` env var; dev-only fallback with a loud warning |
| DB path | `_resolve_path(...)` inline + `wsgi.py` Windows override | `Settings.from_env()`; `wsgi.py` sets **no** path |
| Instance dir | env or default | same, but created by `ensure_dirs()` called from factory, not import |

### 4.2 `hdc/extensions.py` — Flask extensions (~60 lines)

Moves in: `db = SQLAlchemy()`, `login_manager = LoginManager()` (**unbound** — bound in factory via `init_app`), `_sqlite_fast_pragmas` (SQLite `connect` listener), `load_user`, `_csrf_token`, `_csrf_protect`, `_inject_alert_count`, `_ensure_db_runtime_ready` logic (as `register_hooks(app)`).

### 4.3 `hdc/utils/` — pure helpers, no Flask, no models (~500 lines)

| File | Contents |
|---|---|
| `dates.py` | `PKT_ZONE`, `_pkt_now`, `_pkt_now_naive`, `_pkt_today`, `_as_pkt`, `_fmt_pkt` |
| `format.py` | `_flt`, `_parse_date`, `_safe_eval` + `_OPS`, `_num_to_words_en`, `_amount_to_words`, `_activity_at_for`, `_quote_ident`, `_safe_sheet_name`, `_payload_int`, `_to_meters`, `_to_mm`, `_is_pdf_upload`, `_DATE_FALLBACK_DEFAULT` |
| `normalize.py` | all leaf `_normalize_*` (`_normalize_name_ci`, `_normalize_account_*`, `_normalize_related_entity_type`, `_normalize_trade_name`, `_normalize_expense_category_name`) |
| `core.py` | `_audit_paused`, `_admin_only`, `_load_local_env`, `_resolve_path` re-export |

### 4.4 `hdc/models/` — one file per domain (~1,220 lines total, unchanged code)

| File | Classes |
|---|---|
| `auth.py` | `HDCUser`, `UserActivity`, `ActivityLog` |
| `projects.py` | `Project`, `Stage`, `StageDefinition`, `StageRateHistory`, `StageDrawing`, `Estimation`, `EstimationStage`, `CustomFormula` |
| `workforce.py` | `Worker`, `WorkerTrade`, `WorkerRate`, `LabourRateHistory`, `Attendance`, `TimeEntry`, `AttendanceDay`, `AttendanceMark`, `LabourLedger`, `PayrollRun`, `PayrollItem` |
| `office.py` | `OfficeStaff`, `OfficeStaffAttendance`, `OfficeStaffLedger`, `StaffAllowance`, `AllowanceCategory`, `OfficeExpense`, `OfficeExpenseCategory` |
| `subcontract.py` | `Subcontractor`, `SubcontractPayment`, `SubcontractAttendance`, `SubcontractLabourAttendance`, `SubcontractLabourWorker`, `SubcontractLabourPayment`, `SubcontractEvent` |
| `materials.py` | `Material`, `Purchase`, `MaterialUsage`, `Supplier`, `MaterialV2`, `PurchaseV2`, `Delivery`, `UsageLogV2`, `SupplierLedger` (+ `_MATERIAL_V2_UNITS`) |
| `accounts.py` | `Account`, `AccountTransaction`, `OwnerPayment`, `Expense`, `ExpenseCategory`, `PersonalExpense`, `PersonalExpenseCategory`, `Alert` |
| `__init__.py` | re-exports all 53 classes (single import point) |

Three cross-model references (`Project`→`Attendance/TimeEntry/OwnerPayment`, `Stage`→`Attendance/Expense/Purchase/Subcontractor/TimeEntry`, `OfficeStaff`→`_office_staff_ledger_snapshot`) become **function-local imports** so model files stay cycle-free (verified: only 2 classes + 1 property need this).

### 4.5 `hdc/services/` — business engines (~6,500 lines)

| File | Engine | Approx. functions |
|---|---|---|
| `audit.py` | audit trail (`_record_user_activity`, `after_flush` hook, `log_action`, `_AUDIT_*` sets) | 15 |
| `lookups.py` | codes & dropdowns (`_next_*`, `_trade_options`, `_ensure_expense_category*`, `_generate_project_code`) | 18 |
| `aggregation.py` | cost rollups (`_aggregate_*`, `_apply_aggregated_*`, `_running_projects_receivable_rows`) | 5 |
| `timekeeping.py` | wage engine (`_calc_time_wage`, `_worker_rate_on`, reconcile/repair, `_migrate_attendance_to_time_entries`) | 14 |
| `ledger.py` | worker/office snapshots & sync (`_worker_payable_snapshot`, `_sync_office_*`, `_worker_tip_*`, `_personal_expense_*`) | 12 |
| `subcontract.py` | subcontract events & reconcile (`_log_subcontract_event`, `_reconcile_subcontract_links`, `_subcontract_*`, `_ensure_subcontract_*_schema`) | 8 |
| `purchase.py` | Purchase-V2 engine (scope stock, FIFO maths, supplier ledger sync, integrity report) | 20 |
| `accounts.py` | **Unified Accounts engine** (validation matrix, overdraft block, excess split, two-way sync, backfill, forensic report, all `_ACCOUNT_*` maps) | 65 |
| `receipts.py` | receipt data (`_receipt_company_profile`, `_account_receipt_recent_entries`, `_owner_payment_recent_entries`) | 3 |
| `reporting.py` | `_project_report_data`, `_build_stage_event_ledger`, `_refresh_alerts` (+ estimation units `CONCRETE_*`, `_UNIT_*`) | 5 |
| `backups.py` | backup builders (`_create_backup_xlsx/zip`, `_list_backups`, `_backup_filename`, `_cleanup_*`) | 7 |
| `estimation.py` | project-estimation file store (`_load/_save_estimations`, `_create_project_from_estimation`) | 3 |

### 4.6 `hdc/core/` — runtime platform (~900 lines)

| File | Contents |
|---|---|
| `flags.py` | `_runtime_flag_get/set` (DB-backed feature flags — low level, everyone may use) |
| `schema.py` | `_ensure_table_columns_sqlite`, `_run_migrations`, all `_ensure_*_schema` (the future Alembic home) |
| `bootstrap.py` | `_bootstrap_hdc`, `_ensure_bootstrap_once`, `_migrate_legacy_done_markers_to_db`, seed data, backfill orchestration |
| `admin.py` | `_restore_from_paths`, `_wipe_selected_targets` + `_WIPE_TARGETS`, `_run_admin_maintenance` (destructive ops, isolated on purpose) |

### 4.7 `hdc/routes/` — one file per page group (~10,500 lines, endpoint names frozen)

| File | Routes | # |
|---|---|---|
| `auth.py` | login, logout, `/` | 3 |
| `dashboard.py` | dashboard, KPI detail, cost-entries drill-down | 3 |
| `projects.py` | projects, stages, drawings, stage library, owner payments, project-stages API | 22 |
| `subcontractors.py` | subcontractors + labour + ledger + shift routes | 19 |
| `workers.py` | workers, trades, ledgers, advances, payments, rates | 12 |
| `timekeeping.py` | attendance/timekeeping sheet, status, edit/delete/reactivate | 5 |
| `payroll.py` | payroll, generate, delete, salary cards, history | 6 |
| `expenses.py` | expenses CRUD + expense categories + alerts | 6 |
| `office.py` | office management (staff, ledger, attendance, expenses, allowances) + office-expense-category APIs | 22 |
| `materials.py` | v1 materials/usage/purchases + material-stock API | 4 |
| `purchase_v2.py` | Purchase-V2 pages (materials, POs, suppliers, delivered, usage, stock) | 21 |
| `estimation.py` | estimation, project-estimation, formulas, `next_*` code APIs | 9 |
| `reports.py` | reports, glance, CSV/XLSX/PDF exports | 8 |
| `users.py` | users, event recorder | 2 |
| `accounts.py` | accounts pages, ledger, entries, receipt, reconciliation, KPI, personal management | 14 |
| `settings.py` | settings, backup download, maintenance | 3 |
| `api_purchase.py` | `/api/v2/purchase/*` JSON API | 20 |
| `api_accounts.py` | `/api/accounts/*` JSON API | 10 |
| `__init__.py` | `register_all(app)` — imports every module, calls `register(app)` | — |

Each file exposes exactly one public symbol: `def register(app): ...` containing today's `@app.route` handlers verbatim (decorators unchanged, bodies unchanged).

### 4.8 `hdc/app.py` — the factory (~80 lines)

```python
def create_app(config=None):
    app = Flask(__name__, template_folder=..., static_folder=..., static_url_path='/hdc_static')
    app.config.from_object(config or Settings.from_env())
    init_extensions(app)      # db.init_app, login, pragmas, hooks
    register_all(app)         # all 189 endpoints
    ensure_bootstrap_once(app)
    return app
```

### 4.9 `hdc_erp.py` — compatibility shim (~40 lines)

```python
from hdc.app import create_app
app = create_app()            # same object name/path as before
from hdc.extensions import db # noqa
from hdc.models import *      # noqa — all 53 models
# + key helpers re-exported for scripts that imported them
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
```

### 4.10 Frontend moves

- `templates/hdc/*.html` (82 files) → `templates/hdc/<domain>/*.html` (same 18 domains as routes; `base.html` + `_pagination.html` → `templates/hdc/shared/`). All `render_template('x.html')` calls updated to `render_template('<domain>/x.html')`; `{% extends "base.html" %}` → `{% extends "shared/base.html" %}` (scripted + verified by rendering every page in the smoke test).
- `static/hdc/js/hdc.js` (245 lines, global) → `static/hdc/js/core/{layout.js, theme.js, forms.js}` + `static/hdc/js/pages/project_estimation.js` (moved, unchanged); `base.html` loads the core bundle; feature JS stays per-page. `hdc.css` stays shared (theming is global by design) with a documented section header per domain.

---

## 5. Dependency rules (what may import what)

Enforced by construction (and documented for future reviews):

```
routes/  →  services/, core/, models/, utils/, extensions, config
services/ (upper: accounts, reporting, receipts, admin)
         →  services/ (lower: purchase, timekeeping, ledger, subcontract, aggregation, lookups, audit, backups, estimation)
         →  models/, utils/, core/{flags,schema}, extensions, config
core/bootstrap, core/admin → everything except routes/
models/  →  extensions, utils/{dates,format,normalize} ONLY (+3 function-local imports, see §4.4)
utils/   →  stdlib + extensions ONLY (no models, no services, no Flask app)
config, extensions → stdlib + Flask ONLY
```

**Cycle-freedom is proven, not hoped:** AST analysis of all 218 helpers shows the function call graph is a DAG (0 strongly-connected components >1, 0 self-loops); routes never call routes (0 route→route edges); only 2 model classes reference other models. The six module-level back-edges found during planning are resolved by construction:

| Back-edge | Resolution |
|---|---|
| lookups ↔ accounts (`_normalize_*`) | leaf normalizers moved down to `utils/normalize.py` |
| `utils.format` → accounts (receipt fetchers) | moved up to `services/receipts.py` |
| services → bootstrap (`_runtime_flag_*`) | moved down to `core/flags.py` |
| services → bootstrap (`_ensure_table_columns_sqlite`) | moved down to `core/schema.py` |
| `_ensure_expense_category*` mis-grouped in bootstrap | correctly placed in `services/lookups.py` |
| backups → bootstrap (`_restore`, `_wipe`) | moved up to `services`-level `core/admin.py` |

After the split, `scripts/check_layers.py` re-verifies: no module imports from a higher layer, and the package import graph is acyclic.

---

## 6. Backend plan

1. **Extract in dependency order:** config → extensions → utils → models → core/{flags,schema} → services (leaf→top) → core/{bootstrap,admin} → routes → app factory → shim.
2. **No logic edits during the move:** function bodies moved byte-identical (except: 3 local imports in models, decorator lines unchanged inside `register(app)`, import header per file). Any required touch-up is logged in `REFACTOR_NOTES.md`.
3. **Per-file import headers generated** from measured name usage (AST): each module imports exactly the stdlib/third-party names and `hdc.*` symbols it uses — no star imports except the intentional re-export shims.
4. **Transaction boundaries unchanged:** all 181 `commit()` sites move with their functions; no new commits, no removed commits.
5. **Hooks preserved:** both `before_request` handlers, the `context_processor`, the `after_flush` audit listener and the SQLite `connect` listener are registered by the factory in the same order.

## 7. Database plan

- **Zero schema change:** same SQLite file, same 54 tables, same columns/indexes. `db.create_all()` + the existing idempotent self-heals run from `core/schema.py` + `core/bootstrap.py` exactly as today.
- **One models home:** `hdc/models/` owns every table definition; routes/services may query but never define tables.
- **One migrations home:** every `_ensure_*_schema`, `_run_migrations`, `_migrate_*` lives in `hdc/core/` — the future Alembic versions directory. Next schema change = add one function here, not scattered edits.
- **DB file compatibility:** the refactored app opens the existing production DB file unmodified (verified: bootstrap on a *copy* of prod schema + fresh DB both succeed; row counts unchanged).
- **WAL/pragma behaviour unchanged** (same `connect` listener in `extensions.py`).

## 8. Frontend plan

- **URLs frozen:** all 191 rules byte-identical (verified by URL-map diff).
- **Endpoint names frozen:** all 189 `url_for('hdc_…')` targets still resolve (verified by building every URL on both apps and diffing).
- **Templates relocated, not rewritten:** pure moves + path updates in `render_template`/`extends`/`include` (scripted; every page rendered in smoke test).
- **Static:** shared core (`layout/theme/forms`) + per-page files; `base.html` updated once; no page may add new global JS (documented convention).
- **Result for FE updates:** editing the payroll UI = `routes/payroll.py` + `templates/hdc/payroll/*` (+ optional `static/hdc/js/pages/payroll.js`). Nothing else is touched, reviewed, or risked.

## 9. Configuration & environments plan

| Item | Plan |
|---|---|
| `wsgi.py` | `from hdc.app import create_app; app = create_app()` — **delete the Windows path**; honour `HDC_DB_PATH`/`HDC_INSTANCE_DIR` env only |
| Secrets | `HDC_SECRET_KEY` required in prod; dev fallback warns loudly; bootstrap admin password still env-driven, unchanged default |
| `.env` | same `_load_local_env()` semantics, called by `Settings.from_env()` |
| `requirements.txt` | de-duplicated, lowercase, pinned minimums (`Flask>=3,<4` etc.), LF endings |
| `.gitignore` | DBs, backups, archives, venvs, caches, `.env` (prod DB must never be committed again) |
| `README.md` | rewritten: what/where/run/test/deploy + module map |

Environments after conversion: local dev (`HDC_ENV=dev`), test (`HDC_ENV=test` + temp DB — used by the harness), production (`HDC_ENV=prod`, gunicorn + `wsgi:app`).

## 10. Compatibility strategy (zero-downtime, zero-behaviour-change)

1. **Old import path works:** `import hdc_erp; hdc_erp.app` — the shim preserves module name, `app`, `db`, all models, all helpers' importability.
2. **Same process model:** `python hdc_erp.py` still serves `0.0.0.0:$PORT`; `wsgi:app` still the gunicorn target.
3. **Same DB file:** point `HDC_DB_PATH` at the existing file; bootstrap is idempotent and flag-gated, so re-running heals is a no-op.
4. **Deploy = swap code, keep DB:** stop → replace tree → start. Rollback = previous tree (DB untouched by the conversion, so fully reversible).
5. **No data migration, no re-seed, no backup-restore dance.**

## 11. Conversion procedure (reproducible, scripted)

The conversion is performed by `scripts/split_monolith.py` (kept in the repo), which:

1. Parses `hdc_erp.py` with `ast`; slices **532 top-level statements** by source segment (leading comments travel with their statement).
2. Assigns each statement to its target module per the inventory (§4) — mapping tables are explicit (no heuristics in the final run).
3. Computes per-module imports from measured `Name` usage: stdlib/third-party subset + `from hdc.… import …` for moved symbols; wraps route files as `def register(app):` with original decorators re-indented (bodies untouched).
4. Applies the 3 model local-import fixes + template path rewrites + `base.html` static paths.
5. Writes `hdc_erp.py` shim, new `wsgi.py`, cleaned `requirements.txt`, `.gitignore`, `README.md`.
6. Runs `scripts/check_layers.py` (acyclic, layering rules) and `scripts/parity_check.py` (URL map + endpoint + template-target diff vs `git show HEAD:hdc_erp.py` baseline).

Manual review then confirms the diff is *moves only* (`git diff --stat` + spot-read of each new file header/footer).

## 12. Testing & verification plan

| # | Check | How | Pass bar |
|---|---|---|---|
| 1 | Imports clean | `python -c "import hdc_erp, hdc.app, wsgi"` on fresh interpreter | no warnings except pre-existing SQLAlchemy 2.0 `query.get` ones |
| 2 | Layer check | `scripts/check_layers.py` | acyclic; 0 violations |
| 3 | URL-map parity | `scripts/parity_check.py` baseline-vs-new | 191/191 rules identical (rule, methods, endpoint) |
| 4 | Endpoint parity | same harness | 189/189 `url_for` targets build identical URLs |
| 5 | Template-target parity | same harness | 78/78 `render_template` targets resolve to existing files |
| 6 | Fresh-DB boot | boot with empty temp DB, default admin | login OK; bootstrap seeds trades/categories; no exceptions |
| 7 | Smoke: all pages | `tests/smoke_test.py` — login + GET every main page + key APIs, baseline vs new | identical status codes (all 200/302 as baseline) |
| 8 | Smoke: writes | create project→stage→worker→time entry→expense→PO→delivery→usage→account txn on scratch DB, both apps | identical row counts per table + identical key totals |
| 9 | Prod-shape boot | boot on a **schema-only copy** (structure, no PII rows) | boots, pages render, no migration drift |
| 10 | Static/template refs | every `extends/include/url_for/static` target exists | 0 missing (scripted scan) |

Manual spot-check (human): login → dashboard → one project → timekeeping submit → accounts entry → purchase-v2 flow → settings backup list (read-only), on the refactored branch with a scratch DB.

## 13. Rollout & rollback plan

1. Merge this branch → `main` after review + green checks (§12).
2. Deploy to PythonAnywhere: pull, `pip install -r requirements.txt`, set `HDC_SECRET_KEY` + `HDC_DB_PATH` env, restart web app. **DB file untouched.**
3. Verify: login, dashboard totals vs pre-deploy screenshot, one read-only page per domain.
4. **Rollback:** re-deploy previous commit; DB is byte-compatible both ways (no schema change), so rollback is instant and safe.

## 14. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| A moved function references a forgotten global | Medium | Import/startup error | AST-computed imports + fresh-interpreter import test + parity harness; failure mode is loud, not silent |
| Circular import at runtime | Low | Startup error | DAG proven in advance; topological module order; `check_layers.py` in CI |
| Template path typo after move | Medium | 500 on one page | Scripted rewrite + every-page smoke test on both apps |
| Behaviour drift from "while I'm here" edits | Medium | Silent Money-math change | **Rule: moves only.** Any fix needs its own commit after parity is green |
| Hidden dynamic `url_for`/template names | Low | Missed rename | Verified: 0 dynamic `url_for(` calls; all 78 template names are string literals |
| Review fatigue (large diff) | High | Rubber-stamp | Diff is machine-generated moves; review = headers/footers + harness output, not line-by-line |
| sqlite import-time side effects in tests | Low | Test pollution | Factory + `HDC_DB_PATH` temp file per test run; never the real DB |

## 15. Phase 2+ roadmap (after this conversion)

1. **Fix APP_REVIEW findings** in isolation per module (profitability export cost bug, void-filter inconsistencies, secrets/cookies, role enforcement, JSON CSRF).
2. **Flask Blueprints** with URL prefixes (`/hdc/...`, `/api/v2/...`) + endpoint rename codemod, now safe because modules are already split.
3. **Alembic migrations** replacing `_ensure_*_schema` heals; `core/schema.py` becomes `migrations/versions/`.
4. **Per-module test suites** (`tests/test_accounts.py`, …) using the factory + scratch DB; CI runs harness on every PR.
5. **Remove `Model.query.get`** (163 sites) → `db.session.get` in one codemod per models file.
6. **Split `services/accounts.py`** (largest file) into `accounts/{validation,posting,query,backfill}.py` once tests pin behaviour.
7. Optional: extract read-only reporting API / background jobs — only if scale demands.

## 16. Acceptance criteria

- [ ] `hdc/` package exists per §4 inventory; `hdc_erp.py` is a shim (<60 lines of real code).
- [ ] All 12 verification checks in §12 pass; outputs pasted in the PR.
- [ ] `git diff --stat` shows the change as moves + shims + docs (no logic edits; `REFACTOR_NOTES.md` lists the only exceptions: 3 local imports + template paths + config).
- [ ] Fresh-DB boot + login + one write-flow works on the new code.
- [ ] README documents the new structure; rollback = redeploy previous commit.

---

*End of plan. Implementation on this branch follows §11 exactly; any deviation is recorded in `REFACTOR_NOTES.md`.*
