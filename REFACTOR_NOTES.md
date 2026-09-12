# Refactor notes — what changed beyond pure moves

The original conversion (MODULARIZATION_PLAN.md) was **moves only** except
for the explicit items below. The follow-up production-hardening pass is
listed separately at the end of this document; it intentionally fixes
configuration, security, and reporting defects identified by APP_REVIEW.md.

## Code transforms (applied by `scripts/split_monolith.py`)

| ID | Change | Sites | Why |
|---|---|---|---|
| T1 | `app.logger` → `current_app.logger` | 7: `extensions._ensure_db_runtime_ready` (2), `services/estimation._load/_save_estimations` (2), `services/timekeeping._reconcile_all_time_entries_once` (1), `routes/projects` drawing delete/replace (2), `routes/settings` maintenance (1) | No global `app` anymore; all call sites run inside app/request context, so `current_app` is identical |
| T2 | `_ensure_bootstrap_once(force)` → `_ensure_bootstrap_once(app=None, force)`; `with app.app_context()` → resolve-or-use `current_app` | 1 (`core/bootstrap.py`) | Factory passes its app; request-context callers (runtime hook, restore) resolve via `current_app` |
| T3 | Strip `@app.context_processor`, `@app.before_request` (×2), `@login_manager.user_loader`, and the audit `@event.listens_for` decorator; the factory registers them instead | 5 functions (`extensions.py` ×4, `services/audit.py` ×1) | Decorators need the app object; registration order in `create_app()` matches the original file order exactly |
| T4 | Function-local imports in 8 model properties (`Project.total_received/total_labour_cost`, `Stage.stage_labour_cost/stage_expense_cost/stage_tip_expense/stage_material_cost/stage_subcontract_cost`, `OfficeStaff.balance_due`) | 8 | Keeps `models/*` importable without cycles (the only cross-model/service references in 53 classes) |
| T5 | Function-local imports in `extensions.py`: `HDCUser` in `load_user`, `_ensure_bootstrap_once` in `_ensure_db_runtime_ready`, `Alert` in `_inject_alert_count` | 3 | Keeps `extensions` (which `models` import) free of top-level models/bootstrap imports |
| T6 | `BASE_DIR` resolves one directory higher in `hdc/config.py` | 1 | `config.py` sits inside `hdc/`; without this every path would point into the package |
| T7 | Factory passes **absolute** `template_folder`/`static_folder` | `hdc/app.py` | Relative paths would resolve against `hdc/` instead of the repo root |

## New (additive) code

- `hdc/app.py::create_app()` — the application factory (wiring only).
- `hdc/extensions.py` prologue — `db = SQLAlchemy()` / `login_manager =
  LoginManager()` **unbound**, bound by the factory via `init_app()` (same
  objects, same behaviour; enables test apps).
- `hdc/config.py` epilogue — `ensure_dirs()` (the five `os.makedirs` calls,
  now factory-triggered instead of import-triggered) and
  `get_flask_config()` (env-driven `SECRET_KEY` with the original literal
  kept as a loudly-warned dev fallback; same SQLite URI).
- `services/audit.py::register_audit_events()` — attaches the `after_flush`
  listener from the factory with a once-guard (production behaviour
  identical; safe when tests build several apps in one process).
- `core/bootstrap.py::reset_bootstrap_for_tests()` — test-only reset of the
  `_HDC_BOOTSTRAP_DONE` flag.
- `hdc_erp.py` — compatibility shim: `app = create_app()`, re-exports of
  all 311 moved non-route symbols, plus a loop exposing the 189 view
  functions as attributes (they now live inside `register(app)`).
- `wsgi.py` — `create_app()` with **no machine-specific path** (deletes the
  old Windows-only `HDC_DB_PATH` override; configure via environment).

## Frontend moves (applied by `scripts/reorganize_frontend.py`)

- `templates/hdc/*.html` (82) → `templates/hdc/<domain>/*.html` (17
  folders); `render_template('x.html')` → `render_template('<domain>/x.html')`
  (78 call sites, incl. multi-line calls); `{% extends "base.html" %}` → `{%
  extends "shared/base.html" %}`; `{% include '_pagination.html' %}` → `{
  % include 'shared/_pagination.html' %}`. No template logic touched.
- `static/hdc/js/hdc.js` (245 lines) → `static/hdc/js/core/{layout,theme,
  forms,combo}.js`, split exactly at the original section banners into
  independent IIFEs (verified: sections share no variables); `base.html`
  loads the four files. `project_estimation.js` → `static/hdc/js/pages/`.
  All bundles pass `node --check` and serve 200.

## Actual module sizes (measured after the split)

Backend: 46 code modules + 5 `__init__` + factory + shim.
Largest: `services/accounts.py` (~3,900 lines), `routes/subcontractors.py`
(~1,500), `routes/purchase_v2.py`, `routes/accounts.py`, `routes/office.py`.
Route counts per module (final): accounts 14, api_accounts 10, api_purchase
20, auth 3, dashboard 3, estimation 9, expenses 6, materials 4, office 22,
payroll 6, projects 22, purchase_v2 21, reports 8, settings 3,
subcontractors 19, timekeeping 5, users 2, workers 12 (= 189).

These differ slightly from the plan's pre-split estimates (accounts 14 not
23 — the two office-expense-category APIs and three misc `hdc/api/*`
endpoints were filed under their true domains: office/materials/projects).

## Deliberately NOT changed

- URLs, endpoint names, template variables, form fields, JSON shapes.
- Database schema (same 54 tables/columns/indexes; same SQLite file opens).
- Transaction boundaries (all 181 `commit()` sites moved with their code).
- Auth/roles/CSRF behaviour, password rules, bootstrap seeds and backfills
  were unchanged by the original conversion. Production hardening below
  intentionally strengthens CSRF and bootstrap-secret handling.
- External surface: `import hdc_erp`, `hdc_erp.app`, `hdc_erp.db`, all models
  and helpers, `python hdc_erp.py`, `wsgi:app` all keep working.

## Verification (all green, see plan §12)

- `scripts/parity_check.py`: 192/192 URL rules identical, 78/78 templates
  (in domain folders), all resolve to files.
- `tests/smoke_test.py`: 120 differential values (87 page/API reads incl.
  detail pages, 14 writes, 17 table counts) identical baseline-vs-new, with
  sanity asserts proving writes genuinely persist (project/stage/worker/
  expense/time-entry/account all land).
- `scripts/check_layers.py`: 53 modules, acyclic, layering rules hold.
- `node --check` on all JS bundles; static assets serve 200.
