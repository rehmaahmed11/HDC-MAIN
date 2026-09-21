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
  models/               61 ORM models, one file per domain (auth, projects,
                        workforce, office, subcontract, materials, accounts,
                        cashflow)
  services/             business engines: accounts, accounts_manage (account
                        master list + classification editor), purchase,
                        timekeeping, ledger, subcontract, aggregation,
                        reporting, audit, actors (row traceability), lookups,
                        backups, estimation, receipts, cashflow,
                        cashflow_register, account_classification
  core/                 runtime platform: flags, schema/migrations,
                        bootstrap, admin ops (restore/wipe/maintenance)
  routes/               one file per page group; register(app) each
                        (auth, dashboard, projects, subcontractors, workers,
                        timekeeping, payroll, expenses, office, materials,
                        purchase_v2, estimation, reports, users, accounts,
                        accounts_manage, cashflow, cashflow_register, settings,
                        api_purchase, api_accounts, api_actors)
hdc_erp.py              backward-compat shim: app, db, models, helpers
wsgi.py                 gunicorn/PythonAnywhere entrypoint (env-configured)
deploy_hook.py          standalone stdlib-only WSGI app: GitHub push webhook ->
                        git pull + touch WSGI to reload (mounted at /deploy
                        on PythonAnywhere, see wsgi_dispatch_snippet.py)
ops/pythonanywhere/     install_deploy_hook.py: one command that writes the
                        secret + WSGI file and prints the webhook values
templates/hdc/<domain>/ 89 Jinja pages, one folder per feature
static/hdc/             css/hdc.css, css/accounts.css (Accounts section look,
                        ported from the AMS accounts UI), img/, js/core/*.js,
                        js/pages/*.js
scripts/                split_monolith, reorganize_frontend, parity_check,
                        check_layers, check_db_safety, reset_admin_password,
                        labour_audit (read-only labour/payroll consistency
                        audit), make_labour_audit_fixture (seeds a throwaway DB
                        with every known labour bug to verify that audit)
                        seed_cashflow_register_demo (throwaway Cash Flow demo data)
                        (reproducible conversion + verification + ops CLIs)
ops/                    arena/ (local<->sandbox git pairer for agent sessions)
LABOUR_AUDIT.md         labour wage/payment/tip/advance findings + live DB run
ROW_TRACEABILITY.md     how every list shows who entered the row + grey voids
CASHFLOW_MODEL.md       Cash Flow register + day close: what was ported from
                        the AMS accounts model, what was deliberately not, and
                        why (derived vs stored balance, void+replace, minor
                        units, period locks)
tests/                  differential smoke test vs the pre-split baseline
```

**Dependency rule:** `routes → services/core → models → utils`, never
upwards. `scripts/check_layers.py` enforces it (acyclic, 70 modules).

**Scale (keep these honest — the audits check them):** 98 templates,
73 models, 70 modules.

## Accounts &amp; Cash

The money side of the app is one section with a single ledger
(`hdc_account_txn`) behind it. Nothing is stored twice, so balances cannot
disagree between screens. `/hdc/accounts/hub` is the landing page and explains
each of these in the UI.

### Who may do what

Money **writes** require role `admin` or `accountant`. This is enforced by
`_money_write_required()` in `hdc/extensions.py`, applied at the top of every
money-moving handler (HTML handlers redirect to the dashboard with a message;
JSON handlers return **403**). Reads are deliberately wider than writes: staff
may *view* the operational pages (workers, payroll, expenses, subcontractors,
purchase-v2, tools, office, reports, timekeeping) but the Accounts section,
Money Center, `/hdc/settings` and `/hdc/users` stay `admin`/`accountant` only.

The matrix is written down as data in `ACCESS_MATRIX` (also in
`hdc/extensions.py`) and pinned by
`tests/test_money_permissions.py::test_read_matrix_is_complete_and_matches_enforcement`,
which fails if a new route lands in no bucket.

| Page | URL | Job |
|---|---|---|
| **Accounts Hub** | `/hdc/accounts/hub` | Landing page: today's in/out, what needs attention, and what every other page is for |
| **Money Center** | `/hdc/accounts/money-center` | One workspace for recording money — Direction → Type → Details, with pendings per party and a quick-post API |
| **Manage Accounts** | `/hdc/accounts/manage` | The account master list — every account, live balances, classification groups, filters, suspend/archive/restore, CSV |
| **Add / Edit Account** | `/hdc/accounts/new`, `/hdc/accounts/<id>/edit` | Full form for details *and* the Category → Subcategory → Account Type → Channel hierarchy |
| **Transactions** | `/hdc/accounts` | Transaction workspace: KPI tiles with drilldown, project receivables, the full double-entry form |
| **All Entries** | `/hdc/accounts/entries` | Every ledger row with filters, receipts, void/restore and reversals |
| **CF Register** | `/hdc/accounts/cashflow/register` | **Where you record** money in / out / transfer — categorised, party-tagged, project-scoped, immutable (void + replace), audited |
| **Cash Flow** | `/hdc/accounts/cashflow` | **Where you read** — the daily in/out report over the whole ledger, including flows other modules posted |
| **Day Close** | `/hdc/accounts/cashflow/reconciliation` | Counted cash vs ledger per account, then lock the day; a locked day rolls its counted closing forward and rejects back-dated entries |
| **Reconciliation Check** | `/hdc/accounts/reconciliation` | Forensic scan of ledger rows vs the source records that created them |

### Day Close: the large-difference rule

Closing a day with a cash difference larger than
`HDC_DAY_CLOSE_DIFFERENCE_THRESHOLD` (**5,000 PKR** by default, set the env var
to change it, `0` disables the rule) requires an explicit confirmation **and** a
written reason. Without both, `lock_cash_day()` refuses and nothing is written.
The Hub lists recent closes and highlights any locked day above the threshold
with its reason. This exists because a −492,000 PKR difference could previously
be locked silently.

### Supplier payments have one path

Paying a party that keeps its own ledger (supplier, subcontractor, worker) must
go through **that party's own payment entry** — *Pay supplier/materials* on the
supplier page, or the worker/subcontractor payment screen. The generic
`party_payment` type refuses those parties with an explanatory message, because
it would move the cash and leave the payable untouched (so the same supplier
could be paid twice). Ordinary party payments that have no payable — expenses
paid straight to a person — still work as before.

Two rules worth knowing before changing this area:

* **Balances are derived, never stored.** `opening + in − out` from the ledger,
  on every read. The register writes double-entry rows and reads balances back.
* **Nothing with history is deleted.** An account with posted transactions is
  *archived* (`is_void`), and archiving is reversible via restore — recreating
  an account instead would start a second ledger and split its history.

The classification hierarchy lives in
`hdc/services/account_classification.py` and is the single source of truth: the
server validates against the same registry the browser renders, so a
contradictory account (a client receivable holding a bank account number) cannot
be saved. `hdc/services/accounts_manage.py` holds the list/edit/archive rules;
`hdc/routes/accounts_manage.py` stays thin.

### Money-write permissions

**Operational money writes require role `admin` or `accountant`.** Staff,
manager, blank, and unrecognized roles are denied. The role check runs on the
server before record lookup or mutation; hiding a button is not authorization.

| Area | Admin | Accountant | Staff / other roles |
|---|---|---|---|
| Operational money writes | Allowed | Allowed | Denied |
| Existing operational reads | Unchanged | Unchanged | Unchanged |
| Accounts, Money Center, cash-flow administration, settings and user administration | Existing admin access | Still denied wherever admin-only | Still denied wherever admin-only |

This covers worker payments/advances/rates/ledger corrections, payroll,
expenses and categories, subcontractor contracts/payments/attendance, office
staff/salary/allowances/expenses, purchases/payments/delivery/usage/transfers,
tool inventory/rentals/payments/returns, and wage-affecting timekeeping. Owner
receipts (including void/restore), stage status/subcontractor-progress updates,
personal-expense voids/categories, legacy
material routes and purchase/office write APIs use the same guard. This policy
does **not** grant accountants access to the admin-only Accounts workspace.

On mixed read/write routes, `GET`, `HEAD`, and `OPTIONS` keep existing access.
`POST`, `PUT`, `PATCH`, and `DELETE` are guarded: denied HTML requests redirect
to the dashboard with **“Admin/Accountant access required.”**; denied API writes
return JSON `403` with `ok: false`. Login and CSRF checks remain required.
Existing staff views (payroll, workers, expenses, reports, purchasing, tools,
office, etc.) are unchanged; the finer per-page read matrix remains the
separate audit Step 15 decision, not a newly implemented admin-configurable
permissions system. Unrelated project/estimation/drawing workflows are not
reclassified by this money-posting guard.

For a new operational finance write route, use `@_money_write_required()`
**below** `@login_required` (use `@_money_write_required(api=True)` for JSON APIs).
Keep stricter `_admin_only()` checks where they already exist. Regression
coverage is in `tests/test_money_permissions.py`:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_money_permissions.py' -v
```

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

The full suite (deploy hook, database guard, labour fixes, traceability):
`.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`.

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

## Deploy

Pushing to `main` deploys itself. One small stdlib-only file,
[`deploy_hook.py`](deploy_hook.py), is mounted at `/deploy` on PythonAnywhere
(via [`wsgi_dispatch_snippet.py`](wsgi_dispatch_snippet.py)): GitHub's push
webhook verifies the `X-Hub-Signature-256` HMAC, runs `git pull --ff-only`,
and touches the WSGI file so the site reloads.

**Setup is one command** — no file is edited by hand and nothing is pasted
into `/var/www`:

```bash
cd ~/HDC-MAIN
python3 ops/pythonanywhere/install_deploy_hook.py   # then click Reload once
```

It detects your username and paths, creates `deploy_secret.txt` (0600,
gitignored), writes the WSGI dispatch file (backing up the old one and
carrying over its `os.environ` lines), and prints the exact GitHub webhook
values including your real Payload URL. `--check` diagnoses without writing,
`--dry-run` shows the file it would write, `--restore` puts the backup back.

Open `/deploy` (or `/deploy/health`) in a browser at any time: it prints the
detected repo, WSGI file, secret and branch — flagging anything `MISSING` —
plus the last few deploys. A failed pull logs `FAILED` and replies with the
one command that fixes it (a dirty server checkout is stashed, never
silently reset), so a deploy never half-applies. Databases and instance data
live outside Git and are never pulled or reset, and `scripts/check_db_safety.py`
(run by CI) fails any commit that tries to track a `.db`. Rollback is
`git checkout <commit>` in a Bash console, then Reload. Full setup and daily
operations: [`ops/pythonanywhere/README.md`](ops/pythonanywhere/README.md) or
`helpbook.txt` sections 2–6.
