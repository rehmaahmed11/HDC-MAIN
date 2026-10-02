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
  models/               76 ORM models, one file per domain (auth, projects,
                        workforce, office, subcontract, materials, accounts,
                        cashflow, shared_expenses)
  services/             business engines: accounts, accounts_manage (account
                        master list + classification editor), purchase,
                        timekeeping, ledger, subcontract, aggregation,
                        reporting, audit, actors (row traceability), lookups,
                        backups, estimation, receipts, cashflow,
                        cashflow_register, account_classification,
                        shared_expenses (split engine + party balances)
  core/                 runtime platform: flags, schema/migrations,
                        bootstrap, admin ops (restore/wipe/maintenance)
  routes/               one file per page group; register(app) each
                        (auth, dashboard, projects, subcontractors, workers,
                        timekeeping, payroll, expenses, office, parties,
                        materials,
                        purchase_v2, estimation, reports, users, accounts,
                        accounts_manage, cashflow, cashflow_register,
                        shared_expenses, settings, api_purchase, api_accounts,
                        api_actors)
hdc_erp.py              backward-compat shim: app, db, models, helpers
wsgi.py                 gunicorn/PythonAnywhere entrypoint (env-configured)
deploy_hook.py          standalone stdlib-only WSGI app: GitHub push webhook ->
                        git pull + touch WSGI to reload (mounted at /deploy
                        on PythonAnywhere, see wsgi_dispatch_snippet.py)
ops/pythonanywhere/     install_deploy_hook.py: one command that writes the
                        secret + WSGI file and prints the webhook values
templates/hdc/<domain>/ 101 Jinja templates, one folder per feature
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
SHARED_EXPENSES.md      Shared Expenses: one bill split across FBM / HDC /
                        Home / N heads, why the money still moves only through
                        Accounts (link or post), and how the balances work
tests/                  differential smoke test vs the pre-split baseline
```

**Dependency rule:** `routes → services/core → models → utils`, never
upwards. `scripts/check_layers.py` enforces it (acyclic, 76 modules).

**Scale (runtime/source inventory, 2026-09-27):** 101 templates,
76 ORM models, 76 application modules, 249 registered URL rules. See
[`qa/QA_HARDENING_REPORT.md`](qa/QA_HARDENING_REPORT.md) for current test
evidence, fixes and explicitly unverified release scope. Regenerate the
machine-readable inventory with `python scripts/qa_inventory.py --output qa/inventory.json`.

**Operator training:** [`DATA_ENTRY_OPERATOR_TRAINING.md`](DATA_ENTRY_OPERATOR_TRAINING.md)
contains the end-to-end workflow chart, one-by-one sample data-entry sequence,
manual checks, safety notes, and operator sign-off list.

## Accounts &amp; Cash

The money side of the app is one section with a single ledger
(`hdc_account_txn`) behind it. Nothing is stored twice, so balances cannot
disagree between screens. `/hdc/accounts/hub` is the landing page and explains
each of these in the UI.

### Who may do what

By default, money **writes** require role `admin` or `accountant`. This is
enforced by `_money_write_required()` in `hdc/extensions.py`. An administrator
can delegate specific operational page writes with the per-user access editor;
settings, event audit, maintenance and user/role administration remain
non-delegable. Reads are deliberately wider by default: staff may *view*
operational pages, while each custom user can be narrowed to selected pages.

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
| **Shared Expenses** | `/hdc/accounts/shared` | Ledger of bills several heads share (car fuel split FBM / HDC / Home / N ways): who paid, each head's slice, running balances, settlements. The money itself is an ordinary accounts entry — linked or posted from here |

### Day Close: the large-difference rule

Closing a day with a cash difference larger than
`HDC_DAY_CLOSE_DIFFERENCE_THRESHOLD` (**5,000 PKR** by default, set the env var
to change it, `0` disables the rule) requires an explicit confirmation **and** a
written reason. Without both, `lock_cash_day()` refuses and nothing is written.
The Hub lists recent closes and highlights any locked day above the threshold
with its reason. This exists because a −492,000 PKR difference could previously
be locked silently.

### Shared expenses: one bill, several heads

Car fuel, a workshop bill, a utility that serves FBM, HDC and Home at once: the
money leaves **one** account, but the cost belongs to **several** heads. That is
what [`/hdc/accounts/shared`](SHARED_EXPENSES.md) keeps — and it is an
*allocation* ledger, never a second wallet:

* **The money is always an `hdc_account_txn` row.** A bill either **links** an
  entry already recorded in Accounts, or **posts** a new one through the same
  register engine (`source_type='shared_expense'`), which means the day lock,
  the overdraft block, the idempotency guard and the audit trail all apply
  exactly as they do on the Cash Flow Register. A bill with no accounts entry is
  shown as *Not in Accounts* instead of being counted as paid.
* **The split is per expense, not a fixed trio.** Any number of heads can share
  one bill (three, four, more); the split is equal, custom rupee figures, or
  percentages, and the shares always re-add to the total to the paisa.
* **Balances are derived** — `shares − paid − settled` per head, on every read;
  nothing is stored twice, so the module can never disagree with Accounts.
* **Settling up between heads** is a row here *and* (optionally) a transfer in
  the accounts ledger, so a head paying another head moves real money.

The module is `admin`-only, like the rest of the Accounts section.

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

**Default operational money writes require role `admin` or `accountant`.**
Staff, manager, blank, and unrecognized roles are denied unless an administrator
explicitly grants Write for that page in the custom access map. The server checks
this before record lookup or mutation; hiding a button is not authorization.

| Area | Admin | Accountant | Staff / manager / other roles |
|---|---|---|---|
| Operational money writes | Allowed | Allowed by role default | Denied by default; allow per page with an explicit custom Write grant |
| Existing operational reads | Unchanged | Unchanged | Unchanged by default; custom access can narrow pages |
| Accounts / shared expenses | Existing admin access | Existing default restrictions | Denied by default; can be delegated page-by-page |
| Settings, event audit, maintenance, user/role administration | Admin-only | Admin-only | Admin-only |

This covers worker payments/advances/rates/ledger corrections, payroll,
expenses and categories, subcontractor contracts/payments/attendance, office
staff/salary/allowances/expenses, purchases/payments/delivery/usage/transfers,
tool inventory/rentals/payments/returns, and wage-affecting timekeeping. Owner
receipts (including void/restore), stage status/subcontractor-progress updates,
personal-expense voids/categories, legacy
material routes and purchase/office write APIs use the same guard. On mixed read/write routes, `GET`, `HEAD`, and `OPTIONS` are checked against
read access; `POST`, `PUT`, `PATCH`, and `DELETE` require write access. The User
Management screen now supports a per-user custom matrix across the sidebar and
its subpages, with independent Read and Write switches. A custom matrix is
fail-closed for unassigned application routes; legacy users with no custom map
keep their existing role defaults. Users/role administration, settings, event
audit and maintenance remain non-delegable admin operations. CSRF and existing
money-write rules still apply; an explicit custom page grant can delegate
operational writes while preserving those stricter admin boundaries.

The same editor can assign separate stage Read and Write lists. Write access
automatically includes Read for those stages. For scoped users, direct stage
URLs and submitted stage IDs are checked against the current operation, and
stage-linked ORM rows are filtered on the server; projects with no assigned
stages are removed from project lists. New/bulk stages cannot be created by a
stage-limited user because the new record could not safely inherit the existing
allow-list.
Stage scope complements page access; global master data still follows its own
page-level Read/Write grant. Regression coverage is in
`tests/test_user_permissions.py`.

### Exact selected-record access

For a non-admin user who must see **only the individual records you choose**, go
to **Users → Access → Only selected data (strict)**. The existing page/stage
controls remain available; exact-data mode adds an independent, deny-by-default
record gate.

1. Select the pages the user may read or write. Strict mode always requires an
   explicit page map and never restores broad role defaults.
2. For **Projects** / **Stages**, use the guided **Project access** cascade:
   tick **Read** or **Write** at the top to reveal all projects, then pick a
   project, its stages (or *Whole project*), and finally each reporting section
   (**Project report**, **Stage cost report**, **Glance report**, **Report
   exports**) with its own Read / Write. Every level is stored explicitly —
   revealing a project never grants its stages for you. For all other data
   types, search by name, code, date or `#ID` and select individual records;
   that picker is paginated and available only to administrators.
3. Set **Read**, **Edit**, and **Delete/void** for each selected record. Edit and
   Delete/void include Read, but do not include each other.
4. Leave **Create new** unchecked unless this user may create records of that
   type. Creation does not grant existing records, related records, or permanent
   access to new records; assign their IDs separately for later visits.
5. Save access. Changes apply on the user's next request.

**Reporting sections.** Section grants are stored inside the exact-data JSON
under a reserved key (no schema migration). A grant under any stage of a
project covers that project's report; a *Whole project* entry covers all of
its stages; Glance and Exports are enabled once granted anywhere. Saving a
section automatically keeps the matching report page switches (Reports,
Glance, Report exports) on in the page map — both gates still apply, and
revoking the page or the section is enough to block it. Strict users saved
**before** the cascade existed keep their previous page+record behavior until
the first reporting section is stored for them, so existing accounts and the
older regressions are unaffected. Report views, exports and stage rows are
enforced server-side; the reports landing cards, project dropdowns, export
buttons and per-project report links hide what the user cannot open.

**Example: one project only.** Enable strict mode, allow Read on Projects and
Project details, then select that one project with Read. Leave other pages,
records and Create permissions unchecked. The user cannot see other projects,
stages, attendance, workers, payments or related financial records. Project
pricing/financial summaries and owner receipts have separate page switches.
A selected attendance entry or stage can render placeholder parent labels
without revealing an unassigned worker/project name.

Page access never grants records, and a record grant never opens a denied page.
Lists, direct URLs, APIs, exports, relationship loads, aliases and aggregate
queries share the server-side record filter. A separate mutation guard validates
Create/Edit/Delete, primary keys and submitted/generated record references;
ordinary Read access is never write authority. Unreviewed bulk/raw SQL paths
fail closed for strict users. Administrator roles and non-delegable administrative
operations retain their existing boundaries.

Linked workflows may require additional explicitly readable records and separate
action/Create grants for bookkeeping they change. Hidden history must not be
mistaken for absent history: bounded internal scalar checks still prevent unsafe
overdrafts, duplicate postings, stock reductions and deletions without returning
unassigned rows or totals. Read-only strict views do not automatically repair or
create business records. Displayed aggregates use assigned data only.

This is **record-level access**, not a general field-level ACL. Leave strict mode
off to retain existing role/page behavior. Older non-strict custom project maps
retain their former subsection access until those switches are explicitly saved.
If stage limiting is also enabled, both restrictions apply; leave it off when
assigning a project with no stages. Empty or malformed strict grants deny access.

Regression coverage: `tests/test_exact_data_permissions.py` (including single
records, independent actions, references, rollback, hidden-history integrity,
admin saving/search, navigation and legacy compatibility).

For a new operational finance write route, use `@_money_write_required()`
**below** `@login_required` (use `@_money_write_required(api=True)` for JSON APIs).
Keep stricter `_admin_only()` checks where they already exist. Regression
coverage is in `tests/test_money_permissions.py`:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_money_permissions.py' -v
```

## Viewing user passwords

Administrators can see any account's current password on **User Management**
(`/hdc/users`, the eye button in the *Password* column). That is a deliberate
convenience trade-off, so it is built to keep the risk small:

- **Login is unchanged.** It checks the one-way `password_hash`. The viewable
  copy (`hdc_user.password_vault`) is a separate *encrypted* token (Fernet, from
  the `cryptography` package) that only the admin reveal route decrypts. The key
  is `HDC_PASSWORD_VAULT_KEY`, or - when that is unset - derived from
  `HDC_SECRET_KEY`, so a copy of the database or a backup does not reveal any
  password on its own.
- **Admins only**, through `POST /hdc/users/<id>/password` with a CSRF token; the
  response is `no-store`. The page never contains a password: it is fetched on
  click and hides again after 30 seconds.
- **Every view is logged** in the Event Recorder (filter *Event type -> View*):
  who looked at whose account, never the password itself.
- **Existing passwords cannot be recovered.** They were saved only as one-way
  hashes, so accounts created before this feature show *Not saved yet* until an
  admin sets a new password once (*Password / reset*). Passwords set by the app
  from now on - new user, reset, first admin, `scripts/reset_admin_password.py` -
  are viewable. A copy is only shown while it still matches the account's
  current hash, so a stale password is never displayed.
- **Missing library or key.** Without the `cryptography` package or a key the
  site keeps working (logins, creating users, resets) and the page says why
  viewing is off. Install with `pip install -r requirements.txt`; the deploy
  webhook only pulls and reloads (see `helpbook.txt` section 1).
- **Rotating the key** (or `HDC_SECRET_KEY` when no dedicated key is set) makes
  the saved copies unreadable. Logins are unaffected, and an account is viewable
  again once its password is set again.

Anyone with admin access can see every password, so keep admin accounts to
people you trust. Tests: `tests/test_password_viewing.py`.

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
only), `HDC_PASSWORD_VAULT_KEY` (optional, see *Viewing user passwords*), `PORT`,
plus an optional `.env` file for local development. Session cookies are HttpOnly,
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
