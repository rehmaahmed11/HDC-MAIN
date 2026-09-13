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
> [`REFACTOR_NOTES.md`](REFACTOR_NOTES.md) for every non-move edit,
> [`PRODUCTION_HARDENING.md`](PRODUCTION_HARDENING.md) for deployment/security
> hardening, [`APP_REVIEW.md`](APP_REVIEW.md) for the deep functional review,
> and [`LABOUR_AUDIT.md`](LABOUR_AUDIT.md) for the labour wage/payment/tip/advance
> consistency audit (+ `scripts/labour_audit.py` to run it on live data).

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
                        actors (row traceability), lookups, backups,
                        estimation, receipts
  core/                 runtime platform: flags, schema/migrations,
                        bootstrap, admin ops (restore/wipe/maintenance)
  routes/               one file per page group; register(app) each
                        (auth, dashboard, projects, subcontractors, workers,
                        timekeeping, payroll, expenses, office, materials,
                        purchase_v2, estimation, reports, users, accounts,
                        settings, api_purchase, api_accounts)
hdc_erp.py              backward-compat shim: app, db, models, helpers
wsgi.py                 gunicorn/PythonAnywhere entrypoint (env-configured)
deploy_receiver.py      standalone stdlib-only WSGI app: GitHub webhook ->
                        deploy.sh (mounted at /deploy/* on PythonAnywhere)
templates/hdc/<domain>/ 82 Jinja pages, one folder per feature
static/hdc/             css/hdc.css, img/, js/core/*.js, js/pages/*.js
scripts/                split_monolith, reorganize_frontend, parity_check,
                        check_layers, check_db_safety, reset_admin_password,
                        labour_audit (read-only labour/payroll consistency
                        audit), make_labour_audit_fixture (seeds a throwaway DB
                        with every known labour bug to verify that audit)
                        (reproducible conversion + verification + ops CLIs)
ops/                    pythonanywhere/ (setup.py, deploy.sh, sync.sh, webhook
                        receiver wiring), arena/ (local<->sandbox git pairer)
LABOUR_AUDIT.md         labour wage/payment/tip/advance findings + live DB run
ROW_TRACEABILITY.md     how every list shows who entered the row + grey voids
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

# production (gunicorn; terminate HTTPS at the reverse proxy)
HDC_ENV=prod \
HDC_SECRET_KEY='<strong-random>' \
HDC_BOOTSTRAP_ADMIN_USERNAME='admin' \
HDC_BOOTSTRAP_ADMIN_PASSWORD='<strong-random-password>' \
HDC_DB_PATH=/srv/hdc/hdc_instance/hdc_erp.db \
  gunicorn 'wsgi:app' --bind 0.0.0.0:5000 --workers 2
```

Production refuses to start without `HDC_SECRET_KEY`, and refuses to create
its first admin without `HDC_BOOTSTRAP_ADMIN_PASSWORD`. The development-only
fallback admin password is not used when `HDC_ENV=prod`.

Config is environment-driven: `HDC_ENV`, `HDC_DB_PATH`, `HDC_INSTANCE_DIR`,
`HDC_SECRET_KEY`, `HDC_BOOTSTRAP_ADMIN_USERNAME`,
`HDC_BOOTSTRAP_ADMIN_PASSWORD`, `HDC_DEFAULT_ADMIN_PASSWORD` (development
only), `PORT`, plus an optional `.env` file for local development. Session cookies are HttpOnly,
SameSite=Lax, and Secure in production. Never commit a real database or
backup archive (see `.gitignore`).

## Test

```bash
.venv/bin/python scripts/check_layers.py   # layering rules + acyclic imports
.venv/bin/python -m compileall -q hdc hdc_erp.py wsgi.py

# Differential checks require an external copy of the pre-split app:
.venv/bin/python scripts/parity_check.py --baseline /path/to/legacy-copy
.venv/bin/python tests/smoke_test.py --baseline /path/to/legacy-copy
```

Deploy automation has its own suite (webhook receiver, `deploy.sh`, and the
database guard): `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`.

The differential harness boots the legacy baseline and new package side by
side (fresh scratch DBs), logs in, GETs ~90 pages/APIs, writes a project →
stage → worker → expense → time-entry → supplier → material → account flow,
and diffs every status code and table count. The baseline is intentionally
not shipped in this repository because it included a live database archive.

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

Pushing to `main` deploys itself: a GitHub webhook hits `/deploy/github` on
PythonAnywhere, verifies the `X-Hub-Signature-256` HMAC, and runs
`ops/pythonanywhere/deploy.sh` detached — back up the DB, update every tracked
file, install requirements only when they changed, run checks and migrations,
then touch the WSGI file to reload. Databases and instance data live outside
Git and are never pulled, reset or cleaned, and `scripts/check_db_safety.py`
aborts any deploy whose incoming commit tries to track a `.db`. Rollback is a
signed `POST /deploy/trigger {"revision": "<commit>"}` (or the same script with
`HDC_DEPLOY_REVISION=...`); code moves, data does not. Full setup, limits and
troubleshooting: [`ops/pythonanywhere/README.md`](ops/pythonanywhere/README.md).
