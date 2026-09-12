# HDC ERP — Hadi Design & Construction (modular)

Single-deployable, multi-module construction ERP for a Pakistani builder
(all money PKR, all timestamps `Asia/Karachi`):
projects + stage contracts, workers + labour ledger, timekeeping → wages,
unified double-entry-ish accounts, Purchase V2 (supplier → PO → delivery →
stage-scoped usage), payroll, subcontractors, office/personal management,
estimation engine, reports and backups.

Stack: **Flask 3 + Flask-Login + Flask-SQLAlchemy + SQLite (WAL) + openpyxl**,
served by gunicorn (`wsgi:app`).

> This repo was converted from a 20,101-line single-file app into a modular
> monolith with zero behaviour change. Read
> [`MODULARIZATION_PLAN.md`](MODULARIZATION_PLAN.md) for the why/how,
> [`REFACTOR_NOTES.md`](REFACTOR_NOTES.md) for every non-move edit, and
> [`APP_REVIEW.md`](APP_REVIEW.md) for the deep functional review.

## Layout

```
hdc/                    the application package (import here, not hdc_erp)
  app.py                create_app() factory (config -> extensions -> routes)
  config.py             env loading, paths, secret, DB URI  (12-factor)
  extensions.py         db, login_manager, pragmas, CSRF + request hooks
  utils/                pure helpers: dates, format, normalize
  models/               53 ORM models, one file per domain (auth, projects,
                        workforce, office, subcontract, materials, accounts)
  services/             business engines: accounts, purchase, timekeeping,
                        ledger, subcontract, aggregation, reporting, audit,
                        lookups, backups, estimation, receipts
  core/                 runtime platform: flags, schema/migrations,
                        bootstrap, admin ops (restore/wipe/maintenance)
  routes/               one file per page group; register(app) each
                        (auth, dashboard, projects, subcontractors, workers,
                        timekeeping, payroll, expenses, office, materials,
                        purchase_v2, estimation, reports, users, accounts,
                        settings, api_purchase, api_accounts)
hdc_erp.py              backward-compat shim: app, db, models, helpers
wsgi.py                 gunicorn/PythonAnywhere entrypoint (env-configured)
templates/hdc/<domain>/ 82 Jinja pages, one folder per feature
static/hdc/             css/hdc.css, img/, js/core/*.js, js/pages/*.js
scripts/                split_monolith, reorganize_frontend, parity_check,
                        check_layers (reproducible conversion + verification)
tests/                  differential smoke test vs the pre-split baseline
```

**Dependency rule:** `routes → services/core → models → utils`, never
upwards. `scripts/check_layers.py` enforces it (acyclic, 53 modules).

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# development (fresh scratch DB so bootstrap never touches real data)
HDC_DB_PATH=/tmp/hdc_dev.db HDC_INSTANCE_DIR=/tmp/hdc_dev_inst \
  .venv/bin/python hdc_erp.py        # 0.0.0.0:$PORT (5000) -> /hdc/login

# production (gunicorn)
HDC_SECRET_KEY='<strong-random>' HDC_DB_PATH=/srv/hdc/hdc_instance/hdc_erp.db \
  gunicorn 'wsgi:app' --bind 0.0.0.0:5000 --workers 2

# default bootstrap admin (change immediately): admin / Admin@1234
```

Config is environment-only: `HDC_DB_PATH`, `HDC_INSTANCE_DIR`,
`HDC_SECRET_KEY`, `HDC_BOOTSTRAP_ADMIN_USERNAME`,
`HDC_BOOTSTRAP_ADMIN_PASSWORD`, `PORT`, plus optional `.env` file.
Never commit the real database (see `.gitignore`).

## Test

```bash
.venv/bin/python scripts/parity_check.py   # URL map + template parity vs baseline
.venv/bin/python tests/smoke_test.py       # 120-value differential read/write test
.venv/bin/python scripts/check_layers.py   # layering rules + acyclic imports
```

`smoke_test.py` boots the pre-split baseline from a git worktree and the new
package side by side (fresh scratch DBs), logs in, GETs ~90 pages/APIs,
writes a project → stage → worker → expense → time-entry → supplier →
material → account flow, and diffs every status code and table count.

## Working with modules

- **Frontend change (one feature):** `hdc/routes/<domain>.py` +
  `templates/hdc/<domain>/*.html` (+ optional `static/hdc/js/pages/<x>.js`).
  Endpoint names (`url_for('hdc_…')`) are frozen — do not rename.
- **Backend change:** put logic in `hdc/services/<engine>.py`, keep routes
  thin, respect the layer rule above.
- **Database change:** edit `hdc/models/<domain>.py` + add the heal to
  `hdc/core/schema.py` (the future Alembic home). No other module may define
  tables.
- **After any change:** run the three checks above.

## Deploy / rollback

Deploy: pull → `pip install -r requirements.txt` → set env → restart.
The DB file is untouched by this conversion, so rollback is just redeploying
the previous commit.
