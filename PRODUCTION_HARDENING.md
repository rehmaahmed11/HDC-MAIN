# Production hardening

This follow-up pass makes the modular app safe to deploy without changing its
URLs or database schema.

## Configuration and factory isolation

- `.env` is loaded before settings are constructed, while real environment
  variables still take precedence.
- `create_app()` builds per-app filesystem settings and supports separate
  SQLite URIs/paths in one process.
- Bootstrap and runtime schema checks use the active SQLAlchemy engine rather
  than one module-global database path.
- Production (`HDC_ENV=prod`) requires `HDC_SECRET_KEY` and requires an
  explicit `HDC_BOOTSTRAP_ADMIN_PASSWORD` when creating the first admin.
- Session and remember-me cookies are HttpOnly and SameSite=Lax; Secure is
  enabled in production.

## Request security

- CSRF checks apply to JSON mutations as well as form mutations. Clients may
  send `X-CSRFToken`, `X-CSRF-Token`, or `_csrf_token` in the JSON payload.
- The shared frontend automatically adds `X-CSRFToken` to same-origin
  mutating `fetch()` calls.
- **CSRF tokens are injected server-side as well.** `_inject_csrf_into_forms`
  (an `after_request` hook in `hdc/app.py`) adds a hidden `_csrf_token` input to
  every `method="post"` form in the rendered HTML, so forms still submit with
  **JavaScript disabled**. Previously the token was added by an inline script
  only, and all 147 forms returned HTTP 400 when JS was off. The client-side
  hook stays in place as a second layer (it also covers `fetch`).

## Authorization

- Money **writes** require role `admin` or `accountant`, enforced by
  `_money_write_required()` in `hdc/extensions.py` and applied to every
  money-moving handler (HTML handlers redirect to the dashboard with a flash;
  JSON APIs return **403**). Staff, manager, blank, `NULL` and unknown roles are
  denied *before* the handler runs, so a denied request cannot mutate anything.
- Reads are deliberately wider than writes, and the split is written down as
  data in `ACCESS_MATRIX` (`hdc/extensions.py`), matched longest-prefix-first:
  staff may read the operational pages, while `/hdc/accounts`,
  `/hdc/accounts/money-center`, `/hdc/settings` and `/hdc/users` stay
  `admin`/`accountant`-only. `tests/test_money_permissions.py` fails if a new
  route lands in no bucket.
- Login is still required before any of the above, and admin-only restrictions
  inside Accounts and Settings are unchanged.

## Viewable passwords (a deliberate trade-off)

Administrators can view an account's current password (User Management ->
Password column). This is the opposite of the usual rule - passwords are normally
unrecoverable - so it is contained rather than done the obvious way:

- **Not a plain text column.** `hdc_user.password_vault` holds a Fernet token. The
  key comes from `HDC_PASSWORD_VAULT_KEY` or, when unset, is derived (HKDF) from
  `HDC_SECRET_KEY`. This repository has already had a production database
  committed to it once (see *repository hygiene* below); with encrypted copies
  that kind of leak, or a stray backup, exposes no password by itself.
- **Login never touches it.** Authentication still uses `password_hash`.
- **Only the admin role**, `POST`-only with CSRF, `Cache-Control: no-store`. A
  user holding a custom grant of the user-management page still cannot use it
  (administration is not delegable).
- **Audited without leaking.** Each view writes an Event Recorder row; the audit
  listener redacts both `password_hash` and `password_vault`, and a test checks
  the database file contains no plain text password anywhere.
- **Never wrong, never silent.** A copy is shown only if it still matches the
  current hash. Without the `cryptography` package or a key nothing is stored
  (no downgrade to plain text), the old copy is cleared on the next password
  change, and the page says why viewing is off.
- **Honest about history.** One-way hashes cannot be reversed: accounts created
  before this change show *Not saved yet* until their password is set again. The
  app deliberately does **not** harvest passwords at login to backfill them.

Residual risk, accepted by choice: anyone with admin access (or an admin
session) can read every user's password, and the server's key plus a database
copy together recover them all. Password reuse by users elsewhere makes that
worse. Use a trusted channel to hand passwords over, keep the admin role to
trusted people, and rotate a password (Password / reset) if it was exposed.

## Money-flow policy decisions

- **Large day-close differences need a reason.** `lock_cash_day()` refuses to
  lock a day whose cash difference exceeds
  `HDC_DAY_CLOSE_DIFFERENCE_THRESHOLD` (default 5,000 PKR) unless the caller
  confirms it *and* supplies a written reason. Previously a −492,000 PKR
  difference could be locked silently.
- **Supplier payments have exactly one path.** `party_payment` and its aliases
  refuse supplier/subcontractor/worker parties, because that type moves cash
  without reducing the payable. The refusal names the correct screen.
  See README → *Accounts & Cash*.
- Login attempts are throttled per IP and username within each worker. A
  shared reverse-proxy limiter is still recommended for multi-worker or
  multi-instance deployments.
- Standard security response headers are added, including nosniff,
  same-origin framing, referrer policy, permissions policy, and HSTS in
  production. Framing stays `X-Frame-Options: SAMEORIGIN` unless
  `HDC_FRAME_ANCESTORS` is set to a CSP source list (for example
  `HDC_FRAME_ANCESTORS="'self' https://portal.example"`), which switches to
  `Content-Security-Policy: frame-ancestors …` so the app can be embedded in
  that portal; leave it unset unless embedding is actually wanted.

## Data correctness and repository hygiene

- Profitability exports now use the same aggregated cost path as the reports
  UI, including Purchase V2 usage.
- Project and stage fallback cost properties exclude voided expenses and
  include active Purchase V2 usage.
- The tracked source archive containing the production database, WAL files,
  and home-directory files was removed. The `.gitignore` continues to block
  future database, backup, archive, and environment files.

## Verification

- Webhook deploys: `deploy_hook.py` (stdlib only, HMAC-checked, no DB
  access) runs `git pull` and touches the WSGI file to reload; unit tests
  cover signature rejection, branch filtering, and the pull/reload/log flow,
  and `scripts/check_db_safety.py` keeps databases out of Git ("code moves,
  data does not").
- 70-module layer check passes.
- Python and JavaScript syntax checks pass.
- Fresh production-mode boot passes with explicit secrets.
- Production boot rejects missing secret/admin bootstrap credentials.
- JSON CSRF rejects missing tokens and accepts the frontend header.
- Factory creates two isolated scratch databases in one process.
- Legacy-vs-new parity: 192/192 URL rules, 78/78 template targets, and 120
  smoke values match with zero errors.
- Full suite green: `python -m unittest discover -s tests` — 289 tests, OK.
- The extracted page scripts are checked against the data their templates render by
  `tests/test_frontend_config_contract.py`: every config key a script reads must be
  present in the page's JSON block, the edit renders must carry the edit payload, and
  the templates must stay free of inline logic.
- Acceptance re-run (`scripts/audit_acceptance.py`): 114 no-argument GET routes
  crawled with **0 responses ≥ 500**, money pages/APIs all 200, ledger
  invariants `orphan_txns = minor_unit_drift = cf_orphan_links = 0`,
  reconciliation findings all zero, and voiding from either side keeps
  `hdc_account_txn.is_void == hdc_cash_flow_entry.is_void`.
- `tests/smoke_worker.py`: 87 reads, 16 writes, **0 errors**.

## 2026-09-27 verified release-gate pass

The executed suite is now **383 tests, all passing** (36 added since the
347-test baseline). Current inventory: 249 rules, 76 ORM models, 101 templates,
76 application modules. The dated [QA report](qa/QA_HARDENING_REPORT.md) is the
current evidence; the earlier numbers above describe historical runs.

Additional verified fixes include:

- Purchase V2 finite/type validation and consumed-delivery protection.
- Payroll salary capped by canonical worker debt, including salary already
  paid outside a payroll run; straight-time overtime/period net unchanged.
- SQLite writer reservation before money/key/stock validation, with concurrent
  same-key, overdraw and delivery regression tests.
- Cross-process bootstrap coordination for cold multi-worker startup, verified
  with three independent processes and real non-preloaded Gunicorn.
- Restore validation **before** target replacement, WAL-aware recovery on
  migration failure, and noncolliding backup names. **Stop/quiesce other workers
  for restoration and restart them afterwards. This is not online multi-worker
  restore support.** Test live-data upgrades on a copy before deployment.
- Locally served, licensed Bootstrap/Chart.js/icons/fonts; 54 desktop/mobile
  browser renders and seven interaction flows, including JS-disabled forms.
- Controlled invalid report dates and JSON roots; PDF header checking.
- Measured query reductions without changing financial semantics.

CI now includes the isolated browser runner and explicit acceptance/inventory
checks. Local gates pass; actual GitHub execution, live-data rehearsal and
production business acceptance are not implied. See the report's scope and risk
register before deployment. Shared proxy login throttling remains recommended.
