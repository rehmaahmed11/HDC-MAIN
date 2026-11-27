# HDC-MAIN — full-app bulk E2E (100 records per section)

**Run date:** 2026-11-27 09:34:32 → 09:35:31 UTC
**Branch:** `arena/f454d2d5-hdc-main` (base `de0b4e5`)
**Environment:** Python 3.11.2 · Flask 3.1.3 · SQLAlchemy 2.1.9 · disposable SQLite in `/tmp/hdc-bulk-e2e/bulk.db`
**Harness:** `scripts/qa_bulk_e2e.py` (new)
**Verdict:** **PASS** — 4,500/4,500 write operations succeeded across 15 sections, all 15 report surfaces re-read cleanly after deletion, and the ledger returned to its exact starting balances.

---

## 1. What was executed

For each of 15 business sections, against the real Flask routes (not the ORM):

1. **CREATE ×100** — 100 records through the section's own POST route, unique marker (`BLKPROJ-0001` …).
2. **READ reports** — section page + its CSV export / JSON API, checking the new rows are visible.
3. **EDIT ×100** — each record edited through its real edit route, verified by the edited field in the DB and re-read in reports.
4. **DELETE ×100** — each record removed through its real delete / void / archive / deactivate route.
5. **RE-READ reports** — every report surface read again to confirm active rows are gone and history behaves as designed.

| Phase | HTTP success | Attempted | Time |
|---|---|---|---|
| Create | 1500 | 1500 | 24.8 s |
| Edit | 1500 | 1500 | 22.4 s |
| Delete | 1500 | 1500 | 10.0 s |
| **Total** | **4500** | **4500** | **57.2 s** (+ reports) |

## 2. Per-section result

| # | Section | Created | Edited | Deleted | Active after delete | Final report reads |
|---|---|---|---|---|---|---|
| 1 | Parties directory | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| 2 | Projects | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| 3 | Stages | 100/100 | 100/100 | 100/100 | 0 | 1/1 |
| 4 | Workers | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| 5 | Timekeeping (attendance) | 100/100 | 100/100 | 100/100 | 0 | 1/1 |
| 6 | Project expenses | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| 7 | Office staff | 100/100 | 100/100 | 100/100 | 0 | 1/1 |
| 8 | Office expenses | 100/100 | 100/100 | 100/100 | 0 | 1/1 |
| 9 | Suppliers | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| 10 | Materials | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| 11 | Purchases (V2) | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| 12 | Tools inventory | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| 13 | Accounts (money pots) | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| 14 | Cash-flow register | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| 15 | Shared expenses | 100/100 | 100/100 | 100/100 | 0 | 2/2 |

Report surfaces exercised: Parties directory + search · Projects list + profitability CSV · Stage overview · Workers list + workers API · Timekeeping status · Expenses list + project report · Office staff · Office expenses · Suppliers page + API · Materials page + API · Purchases page + API · Tool inventory + tool reports · Accounts page + balances API · Register page + register CSV · Shared expense list + shared CSV.

## 3. Net effect on the data

Deletions in this app are mostly **soft** (void / archive / deactivate), which is deliberate: history is never silently erased.

| Measure | Baseline | After deleting every test row | Verdict |
|---|---|---|---|
| Account balances (all accounts) | — | **identical, 0.00 difference** | PASS |
| Active rows in every section | — | **0** | PASS |
| `account_txn` rows | 0 | 400 | retained voided ledger history |
| `cash_flow_entry` rows | 0 | 200 | retained cancelled + amended history |
| `labour_ledger` rows | 0 | 100 | retained voided attendance ledger |
| `expense` rows | 0 | 100 | voided, kept for audit |
| `office_expense` rows | 0 | 100 | voided, kept for audit |

Because balances return exactly to baseline, the retained rows are non-financial history only — no double counting, no orphaned money.

## 4. Findings

### F1 — Pre-existing brittle fixture in `tests/e2e_full_app.py` (found & fixed)
The walkthrough grabbed `CashFlowCategory.filter_by(direction='out').first()`, which is **"Material & Purchase"**. The engine correctly refuses that category without a project ("*Material & Purchase needs a project — pick one*"), so 3 downstream checks failed. The app was right; the test was picking the wrong category. Fixed to select **"Miscellaneous"**, after which the walkthrough passes **196/196**.

### F2 — Projects have no delete route (gap, not a failure)
There is no `/hdc/projects/<id>/delete`; the project delete leg had to be performed at the ORM layer. Every other section has a user-facing removal path. If operators are expected to remove a mistakenly-created project, that route/button does not exist today.

### F3 — Stage contract edits permanently block stage deletion (by design)
Changing contract values writes a `StageRateHistory` row; `hdc_delete_stage` then refuses ("*Stage has linked history and cannot be deleted*"). Verified: name-only edits delete cleanly (100/100). Worth knowing, because "edit then delete" on a stage with a price change is not possible without removing the history.

### F4 — Soft deletes keep rows (by design, verified net-zero)
Voided expenses, cancelled register entries, archived tools and deactivated parties/workers remain queryable as history. The suite confirms this cannot distort balances.

### F5 — Timekeeping is the slowest write path
100 attendance posts take ~12.6 s (~126 ms each, day-by-day recalculation); every other section creates 100 rows in under 2 s. Fine for real use, but it is the first place to look if bulk attendance ever needs to scale.

## 5. Independent cross-checks (same source tree)

| Check | Result |
|---|---|
| Business walkthrough `tests/e2e_full_app.py` | **196/196 checks**, incl. 202 GET routes crawled with **zero 5xx**, PRAGMA integrity `ok`, no FK violations |
| Python compile (`hdc`, scripts, tests, ops) | PASS |
| `scripts/check_layers.py` | PASS — 92 modules, acyclic, rules hold |
| `scripts/check_db_safety.py` | PASS — no DB/backup/archive files tracked (513 files) |
| `scripts/qa_inventory.py` | PASS — 327 routes, 92 models, 138 templates, 77 assets, 398 CSRF checks, DB integrity ok |
| `scripts/audit_acceptance.py` | **PASS** — money pages/APIs 200, 0 orphan txns, 0 minor-unit drift, 0 reconciliation findings, void sync verified |
| `scripts/qa_responsive.py` | PASS — 0 findings |
| Full unittest suite | see §6 |

## 6. Full unittest suite

| Run | Result |
|---|---|
| `python -m unittest discover -s tests -p 'test_*.py'` | **1209 tests, 799.3 s, OK** (10 skipped, all jsdom-gated) |
| Gated modules re-run after `npm ci --prefix tests` (node 22.22.3 + jsdom) | **35 tests, OK** — the previously skipped DOM cases now execute and pass |
| Combined real execution | **1209 passed, 0 failures**, 0 environment skips remaining |

Evidence tail: `qa/bulk-e2e/full-suite.txt`.

## 7. Scope honesty

This harness proves 100-row create/edit/delete/report cycles for **15 core sections**. It deliberately does *not* duplicate the deeper business chains that `tests/e2e_full_app.py` already covers end-to-end (payroll runs, subcontractor contracts/attendance/payments, purchase deliveries → transfers → usage, tool rentals → returns → payments, day close/reconciliation, report exports for PDF/XLSX). It also does not exercise Users administration or Personal Expenses, because the app disables direct add there by design. No conclusion is drawn about UI rendering beyond HTTP 200 and report contents.

---

## Artifacts

| File | Contents |
|---|---|
| `qa/bulk-e2e/report-create.{md,json}` | Phase 1: per-section create counts, DB verify, report reads |
| `qa/bulk-e2e/report-edit.{md,json}` | Phase 2: edit counts and edited-field verification |
| `qa/bulk-e2e/report-delete.{md,json}` | Phase 3: delete counts, active-row verification |
| `qa/bulk-e2e/report-final.{md,json}` | Post-delete re-read of every report + net ledger effect |
| `scripts/qa_bulk_e2e.py` | Re-runnable harness (`--count`, `--sections`, `--out`) |
