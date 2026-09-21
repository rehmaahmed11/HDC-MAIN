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
  production.

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
