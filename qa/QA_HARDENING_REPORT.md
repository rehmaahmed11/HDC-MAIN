# HDC-MAIN QA / HARDENING REPORT

## 1. Executive status

| Item | Result |
|---|---|
| Application | HDC-MAIN modular Flask construction ERP |
| Date | 2026-09-27 |
| Base commit | `5ebda39e88a626dbb9a9102ee104cbb1e48acc1d` plus this working patch |
| Branch | `arena/01a0e0c4-hdc-main` |
| Environment | Python 3.11, isolated virtualenv, disposable SQLite files; Chromium 153 via Playwright |
| Baseline | 347 tests passed |
| Final full suite | **383 tests passed**, 196.669 seconds, **no skips** in this Linux run |
| Added tests | **36** unittest methods, plus real-browser and performance runners |
| Confirmed defects | **14 records below: all FIXED**; P0: 3, P1: 4, P2: 6, P3: 1 |
| Production data | None supplied, accessed, reset, migrated or repaired |
| Release status | **Executed local release gates PASS; suitable for staging validation. Not an unconditional production certification.** |

The unfinished browser, cross-module reconciliation, concurrency, recovery,
migration, security-boundary and performance work from the first pass has now
been exercised as described below. Several additional critical bugs were found
and fixed rather than accepting the original green baseline.

**Scope honesty:** this report does not claim every field combination, every
legacy schema ever deployed, or every one of the 206 inventoried form tags has
been exhaustively exercised. Dynamic routes have selected real-ID coverage,
not universal full-flow coverage. Live-data upgrade/recovery, sustained load,
all legacy payment retry surfaces and exhaustive adversarial testing remain
release risks. See sections 4 and 12. No unexecuted feature is marked PASS.

## 2. Test statistics and evidence

| Check | Executed result |
|---|---|
| Python compile | PASS: app, shim and WSGI; scripts/tests/ops also compiled during verification |
| Layer check | PASS: 76 modules, acyclic, dependency rules hold |
| Full suite | **383 / 383**; `python -m unittest discover -s tests -p 'test_*.py' -v` |
| Runtime inventory | 249 URL rules, 61 API-like rules, 76 ORM mappings, 101 templates |
| GET crawl | 116 parameter-free URLs: 106 × 200, seven redirects, three required-query 400s, **zero 500s/exceptions** |
| CSRF | **300** missing/invalid-token checks over **150** registered unsafe method/route combinations |
| Malformed JSON roots | 22 API mutation/method combinations × three scalar/array shapes = **66 controlled JSON 400 responses** |
| Templates / assets | All 101 templates compile; all **63** local static/vendor files fetched successfully |
| Browser | **54 renders**: 27 major pages at 1440px and 390px; **seven interaction flows** |
| Browser errors | **0 page exceptions, 0 console errors, 0 failed application requests, 0 HTTP errors, 0 third-party requests** in the executed browser run |
| Responsive smoke | No document-level horizontal overflow on those 54 renders; not a pixel-perfect visual review |
| Legacy-style smoke | 95 read entries, 16 write/result entries, 17 model counts, zero exceptions. Write/result entries include login and IDs; not 16 independently reconciled mutations |
| Acceptance invariants | Orphan ledger references 0; paisa drift 0; cash-flow orphan links 0; all reconciliation finding groups 0; void/restore synchronization PASS |
| DB checks | `integrity_check=ok`, empty `foreign_key_check`, WAL active, FK enforcement enabled |
| Production WSGI | Real Gunicorn **two workers without preload**, cold database; ten login responses 200, production headers/cookie attributes verified |
| Parallel cold start | Three independent processes initialize one DB; all succeed, exactly one admin, integrity/FKs clean |
| Tooling | `pip check`, frontend organization, JS syntax, Git whitespace check and repository database-safety check PASS |

Evidence in this directory:

- `inventory.json`: route/method/module/template/call inventory, model metadata,
  template forms/links, assets, test names, environment-variable names, documents.
- `test_results.json`, `test_matrix.csv`: executed results and bounded scope.
- `browser.json`: individual viewports/pages/timings, interaction names and errors.
- `performance_before.json`, `performance.json`: query counts and timings.
- `acceptance.json`, `smoke.json`, `production_startup.json`.

Inventory is not certification: source-derived guards/calls are only discovery
information. Chromium reports navigation to a successful attachment download as
`ERR_ABORTED`; this is classified separately as a download navigation, and the
actual downloaded CSV bytes are verified. No genuine failed request is ignored.

Final post-fix rerun: runtime inventory (including all 383 test methods),
financial acceptance, smoke and all 54 browser renders/seven flows passed
after the bootstrap fix. Compile, layer, database-repository safety, frontend
organization, dependency consistency and JavaScript syntax checks also passed.
The disposable production verification server was stopped after testing.

## 3. Defects reproduced, fixed and regression-protected

### QA-001 — P1 — Purchase V2 accepts infinite accounting values — FIXED

**Route/files:** `/api/v2/purchase/purchases`, related writes;
`hdc/routes/api_purchase.py`, `hdc/utils/validation.py`.

Authenticated, valid-CSRF requests with quantity/price `Infinity`, or finite
`1e308 × 1e308`, returned 200 and could persist infinite purchase totals and
supplier debits. Float conversion and positive checks were insufficient.
Boundary checks now reject non-finite numbers, malformed IDs and overflowing
products before mutation. Ordinary numeric strings, including commas, remain
supported. Regression: `test_purchase_api_validation.py` checks HTTP status,
JSON result and unchanged business-table snapshots, valid debit of 502 and
failed paid-purchase ledger rollback.

### QA-002 — P2 — Purchase JSON field/shape crashes — FIXED

Lists/scalars caused `.get()` errors; list-valued phones caused `.strip()`
errors; object/bool names could be persisted as text. Purchase field validation
now rejects these with JSON 400 responses. Regression reproduces the prior
exceptions/incorrect acceptance and proves no business-row mutation.

### QA-003 — P1 — Consumed delivery can disappear — FIXED

**Routes:** API delivery DELETE and HTML delivery void/edit.

PO four units, delivery four, usage three: deleting the delivery previously
returned 200 and left usage without supporting stock. Edit lacked the same
consumption lower bound. `validate_delivery_reduction()` in the purchase service
uses the canonical stage/FIFO remaining-stock calculation. Test proves both
surfaces preserve used stock, reducing only the unused unit succeeds, and
voiding usage first permits delivery void. History is retained.

### QA-004 — P1 — CDN outage breaks functional UI — FIXED

**Files:** base/login templates, shared CSS, `static/hdc/vendor/`.

Actual browser requests to Bootstrap, Font Awesome, Chart.js and Google Fonts
failed; dashboard raised `Chart is not defined`, and Bootstrap interactions
could not function. Critical dependencies are now self-hosted at the same
versions, with licenses. Jakarta Latin fonts are local; system fallback remains
for other glyphs. No frontend framework was replaced.

`test_offline_assets.py` failed before the fix, now verifies local links,
resource paths and licenses. `scripts/qa_browser.py` verifies a working supplier
edit modal, forms, charts' page execution and zero external runtime requests.

### QA-005 — P0 — Restore can destroy the current DB before validation — FIXED

**Files:** `hdc/core/admin.py`; settings raw `.db` and ZIP restore paths.

A corrupt raw upload replaced the target before SQLite rejected it; an empty
SQLite file could be accepted and bootstrapped over the real records. Restore
now validates read-only integrity, required HDC tables and FK consistency
before touching the target. It keeps a WAL-aware recovery snapshot until
migration completes, restoring both DB and estimation data on failure.
Extraction uses an isolated runtime directory, not the checkout root.

Seven recovery tests cover invalid raw files, empty/partial/orphan databases,
archive traversal/missing DB, WAL backup/restore, pruning, injected migration
failure and cross-filesystem configuration. Diff review caught an introduced
cross-device replacement issue; a test reproduced it with `/dev/shm`, and
snapshots now reside on the target DB filesystem so replacement remains atomic.
**Multi-worker restore still requires a maintenance window; see section 11.**

### QA-006 — P2 — Same-second backups overwrite one another — FIXED

`_backup_filename()` used seconds alone. A frozen-clock test reproduced identical
names. Names now include a random UUID suffix; the regression checks two
same-second calls differ. Existing backup listing/pruning continue to work.

### QA-007 — P1 — Payroll pays wages already paid outside the run — FIXED

**Routes:** `/hdc/payroll/generate` pay-worker/pay-all and payroll balance views.

Earnings 1,250 − advance 200 − manual salary 500 = **550 actually owed**.
Payroll net remains 1,050 (period earnings minus advances), but Pay All formerly
paid that entire amount, leaving worker debt **−500**. Run-note-only payment
counting ignored the canonical worker ledger.

The ledger service now caps payment at the smaller of run balance and actual
worker payable. Period gross/net and straight-time overtime are unchanged.
Summary/card balances use the same cap; the form explains the distinction.
The full business regression proves Pay All pays only 550, remaining debt is
zero, cash falls by exactly 550, and another pay-worker request does not pay
again. Permission fixtures now include real wage debt rather than an unsupported
synthetic payroll item; their authorization assertions were not weakened.

### QA-008 — P0 — Concurrent cash-flow writes bypass idempotency and balance checks — FIXED

Two independent sessions with the same persistent key both posted. Two distinct
100-PKR outflows against 150 PKR also both posted. Tests widened the actual
validation-to-write race and observed two committed rows.

Python SQLite's legacy transaction mode does not BEGIN on SELECT. A shared
`begin_sqlite_write()` primitive now reserves the SQLite writer **before**
validation/key lookup in cash-flow posting, canonical account posting and
permission-guarded operational mutations. The second writer reads committed
state instead of the first writer's old balance. This works across connections,
not merely via a Python mutex. Caller commit/rollback ownership is preserved.

Four concurrency tests verify one same-key posting, safe rejection of the
second overdraw, two legitimate distinct-key transactions, and two delivery
requests that cannot exceed the PO. This does **not** invent idempotency keys for
legacy forms that lack them, nor prevent intentionally distinct repeated payments.

### QA-009 — P2 — Other APIs crash on JSON roots that are not objects — FIXED

Accounts and office category endpoints reproduced scalar/array `.get()` errors.
All current JSON mutation contracts use objects. The request hook now rejects
non-object/malformed JSON after CSRF validation, with a consistent JSON 400.
Regression traverses all 22 registered API unsafe-method combinations with
three wrong root shapes. Nested field fuzzing is not universally covered.

### QA-010 — P2 — Report date filters cause server errors — FIXED

**Route:** `/hdc/reports` worker/time date filters.

Invalid dates raised `ValueError`; maximum end-date overflowed when adding the
exclusive next-day bound. Request validation now returns a controlled 400 for
invalid/unrepresentable bounds. Tests cover malformed/impossible dates and
end-of-calendar overflow. No valid-date report formula changed.

### QA-011 — P2 — PDF uploads validate only filename — FIXED

HTML/script bytes named `evil.pdf` were stored. `_is_pdf_upload()` now requires
both the extension and `%PDF-` signature, preserving stream position. Tests
verify rejection, sanitized traversal filenames, valid header persistence and
PDF/nosniff download. This is file-type checking, **not malware scanning or a
complete PDF parser**.

### QA-012 — P2 — Demonstrated N+1 queries — FIXED

At 200 workers/100 projects/2,000 transactions: worker list 605 queries, Accounts
1,539, project list 112. Worker relationships are now select-in loaded; canonical
payable snapshots use three grouped queries rather than seven per worker; stage
contract aggregation is installed before profit evaluation triggers lazy loads.

After: workers **8**, Accounts **42**, projects **12**. Query-bound regressions,
all 38 labour regressions and the cross-module financial test pass. No global
cache or stale stored balance was introduced. Legacy wages, voided migrations,
NULL handling and informational tips retain their existing arithmetic.

### QA-013 — P3 — Deployment tests pollute module state — FIXED

PythonAnywhere fixtures removed real `hdc` modules and imported a fake package,
then left the real module registry missing. Full-suite failure injection could
patch a newly imported function rather than the function under test. Fixture
cleanup now restores the saved module objects. The recovery failure-injection
test passes both alone and in the full suite. Production behavior unchanged.

### QA-014 — P0 — Multiple workers fail cold production boot — FIXED

Real Gunicorn without preload exited code 3: concurrent workers created the
same initial admin and hit a username UNIQUE failure. A three-process regression
also failed before correction.

Bootstrap now retains the existing thread lock and adds a database-path-specific
POSIX advisory lock around schema healing/seeding. Independent workers serialize
DDL and seeds; the OS releases the lock on process exit. The lock inode is kept
and Git-ignored. In-memory databases and non-POSIX development retain their
previous behavior. All three cold-start processes now succeed with one admin;
real two-worker Gunicorn without preload serves ten successful requests.

## 4. Deferred scope / follow-up decisions

The following are not silently counted as PASS:

- Exhaustive valid/invalid permutations of every form and dynamic route, and all
  nested JSON field types outside covered endpoints. The inventory makes this
  remaining surface explicit.
- A live-data upgrade rehearsal: no production/older deployment copy supplied.
  Synthetic missing-column/partial-table/legacy-attendance upgrades were tested.
- Universal persistent idempotency for worker/supplier/subcontractor legacy
  payment forms. Current keyed cash-flow retries are tested; non-keyed repeated
  transactions may be legitimate and need a deliberate UI/API contract.
- Full threat-model coverage of IDOR, all export formula cells, malicious PDF
  internals, decompression resource exhaustion, and compromised signed sessions.
- Sustained multi-user load and very large data volumes beyond the measured
  synthetic fixture; non-POSIX multi-process deployments.
- Differential parity with the pre-modularization application: **BLOCKED** by
  absence of a legitimate baseline; none was fabricated.

These are explicit scope/risk entries, not known unfixed reproduced P0/P1 defects
in the exercised flows. Staging/business-owner acceptance remains necessary.

## 5. Database integrity / recovery / migration

PASS on disposable data:

- Fresh startup, current schema, repeated startup, partially initialized feature
  table, selected older missing columns, legacy attendance conversion and
  already-voided migrated rows.
- Backup taken before missing-column reconstruction; three healing runs preserve
  historic project data, one admin and nonduplicated index names.
- `PRAGMA integrity_check`, `foreign_key_check`, WAL and connection FK enforcement.
- Paid purchase failure injection leaves no orphan purchase/debit/posting.
- Invalid restores preserve the target; migration failure restores prior DB and
  estimation bytes; valid ZIP restores project data and login/read access across
  workers/accounts/expenses/purchase/reports. XLSX backup opens successfully.
- Parallel boot, keyed money posting, overdraw prevention and delivery limits.

Not tested: every historical schema, actual production records, interrupted
power-loss at every restore step, or restoring while other workers are active.
Existing drawing-file backup coverage was not expanded or certified.

## 6. Security

Authentication tests execute hashed-password verification, correct/incorrect/
empty/malicious-name login, logout access denial, worker-local throttling and
external `next` redirect refusal. Production missing-secret and missing-first-
admin-password configurations raise errors. Actual production responses verify
Secure/HttpOnly/SameSite=Lax plus HSTS, nosniff, SAMEORIGIN, referrer/permissions
policies.

All existing money-role matrix tests pass (admin/accountant/staff/manager/blank/
NULL/unknown roles), including denied-write DB snapshots and direct requests.
All unsafe methods reject missing/invalid CSRF. Actual browser fetch supplies
CSRF; JS-disabled forms work. A stored HTML payload in a Unicode project name
renders literally and does not execute. PDF content/path checks and backup
traversal tests pass. No real credentials added; high-signal private-key/token
pattern scan returned no matching tracked files. Repository DB safety passed.
This is not a complete history/entropy secret scan or all-field SQLi/IDOR audit.

## 7. Frontend

The original browser download issue was resolved using Chromium from the
available npm registry with its required libraries. No browser binary or local
QA dependency was added to production requirements.

Executed browser flows:

1. Real login.
2. Create project; verify persisted Unicode/hostile text and XSS nonexecution.
3. Create supplier; open Bootstrap edit modal; submit update; query persisted row.
4. Real same-origin fetch mutation with automatically injected CSRF.
5. Dark/light switch and persistence across reload.
6. Download profitability CSV; verify the created project in actual bytes.
7. Login and create project with JavaScript disabled and server-rendered CSRF.

Major pages render at desktop/mobile sizes without application errors or
document overflow. This is interaction/geometry evidence, not certification of
every modal, keyboard path, screen reader or visual detail. Browser regression
runner is now included as a separate CI job, without `continue-on-error`.

## 8. Backend / performance / architecture

No endpoint/table renaming, schema redesign, framework replacement or wage-policy
change. Business stock/payroll rules remain in services. Layer check passes.
The runtime inventory traces route modules, calls, templates and ORM mappings.
New guards reject demonstrated failure cases without changing successful flows.

Measured fixture: 100 projects, 200 workers, 2,000 ledger rows. Query reductions:

| Page | Before | After |
|---|---:|---:|
| Dashboard | 119 | 19 |
| Projects | 112 | 12 |
| Project detail | 133 | 33 |
| Workers | 605 | 8 |
| Accounts | 1,539 | 42 |
| Entries | 129 | 29 |
| Reports | 121 | 21 |
| 200-row actor lookup | 3 | 3 |

Measured server requests all return 200. Timings are recorded in JSON, not used
as universal SLAs. Lists remain unpaginated in some modules; synthetic empty
worker histories do not model every real-life workload. Legacy SQLAlchemy
`Query.get()` warnings remain deliberately unchanged.

## 9. Accounting and full business scenario

`test_business_acceptance.py` uses one controlled dataset and real HTTP mutations
for project/stage/contracts, worker/rate/timekeeping, advance/payment,
supplier/material/PO/delivery/usage/payment, expense, subcontractor contract/team
attendance/payment, account creation, void/restore and payroll. Cash receipt,
transfer and day-close setup use their canonical services; separate form tests
and browser flows cover HTTP/UI contracts. This is **not** a claim that the
entire business scenario was clicked through in one browser session.

Independent expected values:

| Boundary | Expected / verified |
|---|---:|
| Worker regular wage | 1,000 |
| Two straight-time overtime hours | 250 |
| Earned | 1,250 |
| Advance / manual payment | 200 / 500 |
| Actual debt before payroll | **550** |
| Purchase | 10 × 100 = **1,000** |
| Deliveries | 6 + 4 = **10**; attempted extra 5 rejected |
| Usage cost | 4 × 100 = **400** |
| Supplier payment / remaining payable | 300 / **700** |
| Project expense | **50** |
| Subcontract contract / paid / remaining | 2,000 / 400 / **1,600** |
| Crew attendance cost (informational) | 1 × 2 × 100 = **200**, not double-posted as project payment |
| Main cash before final payroll | 100,000 − 200 − 500 − 300 − 50 − 400 + 200 − 100 = **98,650** |
| Secondary cash | **100** |
| Final payroll outflow / main cash | **550 / 98,100**; worker debt becomes **0** |
| Project cost | 1,250 + 400 + 50 + 400 = **2,100** |
| Stage contract used as project contract | **10,000** (supersedes project fallback, existing documented semantics) |
| Profitability export net profit | 10,000 − 2,100 = **7,900** |

Reconciliation findings are empty; voiding/restoring the receipt changes cash
by exactly 200 in each direction. Day counts match the ledger, lock/unlock
works, salary CSV shows 1,250, and profitability CSV matches independent costs.
Existing cash-flow/category/receipt/money-center tests also pass.

## 10. Labour / payroll

All 38 prior labour audit tests pass after the grouped-query change: hourly/
daily straight-time OT, per-sqft work, legacy wages, voided migrated attendance,
rate fallback, tips/expense identity and synchronization, advances carried
forward, settlements, payroll overlap and payroll void behavior.

New end-to-end coverage prevents salary duplication across worker-screen and
payroll payments. `net_pay` remains period-scoped; actual payable is capped by
all-time debt. Tips remain informational; no OT premium was introduced. This
report does not reconcile any live payroll dataset.

## 11. Deployment / operational runbook

- Final real Gunicorn cold boot: **two workers without preload**, ten successful
  requests. The earlier preloaded boot also passed, but had hidden the race now
  protected by the three-process regression.
- Production secrets are mandatory where documented; failure tests pass.
- Existing webhook signature/branch/dirty-tree/pull-failure/PythonAnywhere
  installer tests pass. No production webhook or GitHub merge was triggered.
- CI now runs runtime inventory, financial acceptance, checks smoke JSON for
  failures, and has a real-browser job. Workflow changes have been locally
  exercised by equivalent commands, **not yet run on GitHub**.
- Keep SQLite on a local filesystem with the runtime directory writable by the
  app user. Do not delete a bootstrap lock while workers may be using it.
- **Restore only in a maintenance window:** stop/quiesce other workers, take an
  independent external backup, restore/verify, then restart all workers before
  admitting traffic. Per-process connection disposal cannot close another
  worker's database handle. This patch does not claim safe online multi-worker
  file replacement or protection from power loss during restore.
- Configure a shared reverse-proxy login limiter; per-worker throttling alone
  does not provide one global attempt budget.

## 12. Remaining risk register

| Risk | Severity / likelihood | Impact | Mitigation now | Required action |
|---|---|---|---|---|
| Live historic data differs from fixtures | Potential P0/P1 / unknown | Migration/reconciliation error | Synthetic upgrades + rollback/integrity tests | Rehearse on a backed-up production copy before deployment |
| Online multi-worker restore | Potential P0 / if performed | Lost/stale writes | Validated restore + recovery snapshot; documented quiescence | Maintenance window and worker restart; no online restore certification |
| Non-keyed legacy retries | Potential P1 / unknown | Legitimate/retried payment indistinguishable | Cash-flow keys + serialized financial writes, existing guards | Design explicit persistent tokens per legacy UI/API; test retry semantics |
| Untested form/field/security permutations | Potential P1/P2 / unknown | Validation/authorization/export issues | 383 tests, route/CSRF matrix, selected browser/fuzz cases | Continue long-tail matrix; do not label all 206 forms PASS |
| Per-worker brute-force limits | Potential P2 / known limitation | Distributed attempt budget | Worker throttle | Shared deployment limiter |
| Very large/sustained workloads | Potential P2 / unknown | Latency/lock contention | N+1 fixes + bounded benchmark | Soak tests with representative volume, observe SQLite wait times |
| Browser/platform variation | Potential P2 / unknown | UI differences | Chromium desktop/mobile + JS-disabled tests | Safari/Firefox, keyboard/accessibility and business-user acceptance |
| No legitimate legacy parity baseline | Unknown / unavailable | Undetected historical drift | Existing regression fixtures | Supply baseline if differential parity is required |

### Reproduce safely

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
export HDC_ENV=test HDC_SECRET_KEY=isolated-qa-only
export HDC_BOOTSTRAP_ADMIN_PASSWORD=Isolated-QA-Only-123
export HDC_INSTANCE_DIR="$(mktemp -d /tmp/hdc-qa-XXXXXX)"
export HDC_DB_PATH="$HDC_INSTANCE_DIR/qa.db"
.venv/bin/python -m compileall -q hdc hdc_erp.py wsgi.py
.venv/bin/python scripts/check_layers.py
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python scripts/qa_inventory.py --output qa/inventory.json
.venv/bin/python scripts/audit_acceptance.py --json qa/acceptance.json
.venv/bin/python scripts/qa_performance.py --output qa/performance.json
.venv/bin/pip install playwright==1.63.0
.venv/bin/playwright install chromium
.venv/bin/python scripts/qa_browser.py --output qa/browser.json
.venv/bin/python scripts/check_db_safety.py
```

The browser and benchmark scripts own disposable servers/databases and cannot
be pointed at an existing deployment. If a browser is already installed, pass
`--executable /path/to/chromium`; sandbox execution used Chromium from
`@sparticuz/chromium@153.0.0` with its library directory, outside the repository.
