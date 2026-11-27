# HDC ERP master QA — initial assessment and continuation ledger

**Assessment date:** 2026-11-27 UTC  
**Source:** `2d8362c3828fe62a976b88444e837c2a0fcb921e`, branch `arena/f4e4ed98-hdc-main`  
**Verdict: NOT READY FOR AN UNCONDITIONAL RELEASE APPROVAL.** The broad automated suite passed, but the standalone business walkthrough failed and real-browser, exhaustive input/permission coverage, and production-like validation remain incomplete. This is an initial assessment, not the requested complete certification.

## Scope and safety

Baseline Git status was clean. No production system or real database was targeted. The suite was run with `HDC_ENV=test` and explicit disposable paths under `/tmp/hdc-master-suite`; individual fixture tests and the inventory, acceptance, E2E and performance scripts also create temporary databases. No application behavior was changed. Python 3.11.2, Node 22.22.3, Flask 3.1.3, Flask-SQLAlchemy 3.1.1 and SQLAlchemy 2.1.9 were used, with Python dependencies installed in `/tmp/hdc-qa-venv` and jsdom installed under ignored `tests/node_modules`.

## Inventory and coverage artifacts

- `inventory.json`: runtime route/method inventory, model columns, relationships and model-declared constraints/indexes, template compilation and source-discovered forms and endpoint references, assets, environment variable names, tests, and bounded admin GET/CSRF checks. Guard detection is heuristic source text, **not an authorization proof**. Dynamic URLs and actions are not exhaustively exercised. Inspect `database` and `repeat_bootstrap` for fresh-database checks.
- `routes.csv`: all 327 registered URL rules with methods, arguments, source-derived metadata and limited checks. The functional coverage column is deliberately not marked as verified. Rules may share URLs/endpoints; automatic HEAD/OPTIONS are included in method lists. `DEFERRED` is not a failure and does not necessarily mean a dynamic GET: non-GET rules are also deferred.
- `template-controls.csv`: 2,182 static HTML start tags for forms, inputs, selects, textareas and buttons across source templates, with line locations and literal attributes. This is a **discovery aid**, not a rendered DOM or exhaustive field inventory: Jinja branches/macros, JavaScript-generated controls, text labels, validation, persistence and relationships require further analysis. Rows are not individually tested.
- `unittest-results.txt`: filtered per-test output and final suite summary; some result lines were interleaved with application stdout and may not appear in this filtered list. The suite's actual summary is authoritative for the total.
- `e2e-results.txt`: complete standalone HTTP-driven walkthrough output, including failures.
- `acceptance.json`: clean-fixture money acceptance results and status-by-route crawl.
- `performance.json`: synthetic measurements, fixture size, response status, query counts and payload sizes.

Existing `qa/QA_HARDENING_REPORT.md` and other reports predate this source snapshot; their results are historical, not evidence for this run. For example its 249-route / 76-model / 101-template inventory differs from the current 327 / 92 / 138.

## Executed checks

| Check | Result and evidence | Limits |
|---|---|---|
| Python suite | **VERIFIED:** `python -m unittest discover -s tests -p 'test_*.py' -v`: **1,199 tests, 967.038s, OK**, no skips reported. | Passing assertions are bounded by each test; not every route or input is proven. |
| Runtime discovery | **VERIFIED:** `python scripts/qa_inventory.py --output qa/master-qa/inventory.json` passed: 327 rules, 92 mapped models, 138 templates, 77 assets; all templates compiled, all assets returned 200; 398 missing/invalid CSRF checks passed. | Parameter-free admin GETs: 130 × 200, 7 × 302, 4 × 400; the last are not automatically treated as errors. CSRF checks use placeholder IDs and do not prove all auth rules. |
| Database, fresh fixture | **VERIFIED:** SQLite integrity `ok`, no FK violations, WAL and FK enforcement active; repeat bootstrap preserves admin identity. | Not a production database, nor all deployed legacy schemas. |
| Financial acceptance | **VERIFIED:** `python scripts/audit_acceptance.py --json qa/master-qa/acceptance.json`; 141 parameter-free GETs had no 5xx, specified money pages/APIs returned 200, orphan/drift checks zero and sampled void synchronization passed. | Synthetic clean data; no claim that all ledger flows are reconciled. |
| HTTP business walkthrough | **FAILED:** `python tests/e2e_full_app.py`: 192/195 checks passed; see QA-001 and `e2e-results.txt`. Includes parameterized routes and crawl reported as 202 GET URLs. | Uses Flask test client, not an interactive browser. Failed branch did not exercise its intended CF void/restore. |
| Static checks | **VERIFIED:** `python -m compileall -q hdc hdc_erp.py wsgi.py deploy_hook.py wsgi_dispatch_snippet.py scripts tests ops`; `python scripts/check_layers.py` (92 modules, acyclic); `python scripts/check_db_safety.py`; `python scripts/reorganize_frontend.py --check`; `python scripts/qa_responsive.py`; `find static -type f -name '*.js' -print0 | xargs -0 -r -n1 node --check`; `pip check`: all exited 0. | Static checks do not substitute for interaction or accessibility testing. |
| Bounded performance | **VERIFIED as measurements only:** `python scripts/qa_performance.py --output qa/master-qa/performance.json`; 100 projects, 200 workers and 2,000 synthetic ledger rows; 10 sampled requests returned 200. | Single test-client samples, no concurrency, no thresholds, no production claims. |
| Real browser | **BLOCKED:** no Chromium/Chrome/Firefox binary found. Installing Playwright succeeded but `playwright install chromium` failed: connection reset to `cdn.playwright.dev`, outside available outbound hosts. | No actual browser, visual, keyboard, screen-reader, mobile or console review was performed in this run. Existing jsdom checks are not real-browser validation. |

Commands above used `/tmp/hdc-qa-venv/bin/python`, with the suite explicitly configured for a disposable test instance and database. No credentials from a live environment were used.

## Confirmed issue and classification

### QA-001 — P2 / test-suite reliability — standalone full-business walkthrough fails after selecting an incompatible cash-flow category

**Status:** confirmed test failure; application defect **not established**.  
**Reproduce:** from the repository root run `/tmp/hdc-qa-venv/bin/python tests/e2e_full_app.py`. The script owns a temporary database and exits 1.  
**Precondition/input:** at `tests/e2e_full_app.py:534–537` the cash-flow outflow selects the first `direction='out'` category and submits 4,000 without project or party. On this fresh fixture that category is `Material & Purchase`.  
**Expected:** walkthrough expects the outflow to post, cash to decrease and then the entry to exist for void/restore.  
**Actual:** POST redirects with the danger flash `"Material & Purchase" needs a project — pick one.` The expected balance is 950,700 but actual is 954,700. Looking up the missing outflow then raises an `AttributeError` in the soft-check wrapper. The final result is 192/195 and exit 1. These are three related failed checks, not three independently established application defects.  
**Evidence and cause:** `e2e-results.txt`; `hdc/services/cashflow_register.py:881–885` enforces required project; the seeded rule in the same service around line 2346 requires both project and supplier for Material & Purchase. The rejection is consistent with current configured rules; choosing `.first()` makes the scenario dependent on seed order.  
**Impact:** full-business release signal is red and the intended cash-flow void/restore scenario does not run. The separate acceptance test does exercise a bounded void/restore path.  
**Recommendation:** after authorization, update the walkthrough to select an explicitly appropriate category, or supply all valid category-specific fields, assert creation before dereferencing, and rerun the full walkthrough. Do not relax the business rule merely to make the test pass. No fix was made.

## Domain coverage ledger

All items below are **partial**, even where existing tests pass. The walkthrough is an HTTP/DB test rather than browser-driven. Consult the per-test output and E2E transcript for exact cases; a passing area does not imply every operation, record scope or input state is verified.

| Area | Evidence performed | Remaining work |
|---|---|---|
| Auth/users/permissions | Full suite includes authentication, role, exact-data, project cascade, security boundary and password tests; inventory CSRF probes. | Interactive login, every role × route/action/record, expired sessions, IDOR matrix and adversarial review. |
| Projects/stages | Suite and E2E create project/stage, inspect costs and reports. | Every form, stage state, permissions, UI and failure case. |
| Workers/labour/payroll | Suite and E2E cover worker, timekeeping, payments and payroll examples. | Full boundary/correction/concurrency matrix and browser verification. |
| Subcontractors | Suite and walkthrough cover selected stage/team/attendance/payment flows. | All combinations, UI states and scope restrictions. |
| Purchases/materials | Suite and walkthrough exercise selected supplier, purchase, delivery and usage flows. | Cross-scope stock concurrency, full invalid input matrix, browser workflow. |
| Accounts/cash flow | Suite, acceptance checks, E2E transfer and day-close examples; QA-001 fails. | Resolve walkthrough, verify all categories, reversals, races and end-to-end cross-module reconciliation. |
| Shared expenses | Suite includes shared expense tests. | Full UI-driven multi-head, settlement, permissions and reconciliation scenarios. |
| Tools/rentals | Extensive suite plus HTTP walkthrough of a rental. | Browser lifecycle for every serial/photo/location state, financial and concurrency boundaries. |
| Office/parties | Suite and walkthrough cover selected operations. | Complete UI/action and cross-identity/role matrix. |
| Reports/backups/recovery | Suite covers backup/recovery and reports; walkthrough checks sample reports and exports. | Production-like restore rehearsal, data-specific validation, file/browser checks and every report. |
| Security | Existing security/permissions tests and generic CSRF probes passed. | Independent adversarial assessment, exhaustive API/record authorization, uploads, session and deploy review. Not a penetration test. |
| Accessibility/responsiveness | Static responsive gate and existing DOM tests pass. | Real browsers, keyboard, focus, contrast, screen readers and device/viewport checks blocked. |
| Deployment and load | Existing automated tests; bounded synthetic performance sample. | Current production configuration review, realistic concurrency, sustained load and defined acceptance thresholds. |

## Continuation plan, in priority order

1. Restore browser availability in an environment permitted to install Chromium, then run `python scripts/qa_browser.py --output qa/master-qa/browser.json`, `scripts/qa_responsive_pages.py` and `scripts/qa_responsive_functional.py` after inspecting their options and isolation. Capture viewport/browser versions, console/network failures, screenshots and accessibility evidence. Browser smoke scripts alone are not exhaustive.
2. With authorization, address QA-001 in the **test scenario**, rerun `tests/e2e_full_app.py`, and verify actual CF row, cash balance and void/restore synchronization. If application behavior is suspected wrong, first document a separate reproducible defect.
3. Expand `routes.csv` into per-method, per-role and real-ID test cases; explicitly cover missing/invalid input, CSRF, authorization, content types, persistence, rollback and response shape. Resolve redirects and non-200 responses individually rather than assuming they are healthy.
4. Render all templates with representative records and each role, cross-check `template-controls.csv` against the DOM, add labels, dependencies, constraints, valid/invalid/boundary values and evidence for each control. Inventory JS-created controls, menus, modals and dynamic API calls separately.
5. Execute the 12 requested business workflows through real UI with independently checked DB, stock, ledger, audit, report and authorization outcomes; include partial, reversed, voided, restored, duplicate and concurrent cases. Record each workflow's data and assertions.
6. Run isolated legacy-schema upgrades, backup/restore and recovery rehearsals, multi-user security tests, accessibility and realistic multi-user load with agreed thresholds. Record all untested versions and environments.
7. Review inventory for orphaned/unreachable pages, dead links, unused APIs, undeclared constraints and documentation drift; distinguish actual defects from unproven suspicions. Produce an updated release decision only after these gaps have evidence.
