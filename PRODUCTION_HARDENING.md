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

- 53-module layer check passes.
- Python and JavaScript syntax checks pass.
- Fresh production-mode boot passes with explicit secrets.
- Production boot rejects missing secret/admin bootstrap credentials.
- JSON CSRF rejects missing tokens and accepts the frontend header.
- Factory creates two isolated scratch databases in one process.
- Legacy-vs-new parity: 192/192 URL rules, 78/78 template targets, and 120
  smoke values match with zero errors.
