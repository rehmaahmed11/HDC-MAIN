# Focused QA audit: HDC Tools, projects, accounts and deletion

**Date:** 2026-11-27 UTC  
**Code:** `2d8362c3828fe62a976b88444e837c2a0fcb921e`, branch `arena/f4e4ed98-hdc-main`  
**Verdict:** Material data-integrity defects reproduced. Do not treat these lifecycle paths as release-ready. This is a focused audit, not proof that every feature of the application is correct or incorrect.

## Safety and method

All destructive operations used a temporary SQLite database and instance directory, with an isolated admin session; no production data was inspected or modified. The reproducible script `probe.py` owns its temporary database and does not accept an external DB. Run it with `python qa/tools-projects-accounts/probe.py` after installing `requirements.txt`; `probe-results.txt` contains this run's results. Operations were executed through Flask's HTTP test client, not a real browser. Some initial fixtures (account transaction, tool inventory and the expense used in the projects-wipe scenario) were seeded directly through the ORM; other operations noted below used actual application routes. A 302 is not in itself success: persisted rows, values and integrity were checked separately.

The earlier master QA run passed 1,199 automated tests but did not cover these exact sequences; see `../master-qa/REPORT.md`. Browser testing remains blocked in this environment. Existing tests and passing `PRAGMA foreign_key_check` cannot prove business-history completeness.

## Confirmed findings

### TPA-01 — Critical: wiping Projects alone leaves dangling project and stage references

**Where:** Settings wipe (`hdc/routes/settings.py:187–205`), targets in `hdc/core/admin.py:124–142` and `_wipe_selected_targets` around lines 303–335.  
**Reproduction:** in the scratch fixture, create a project, a stage and an expense referring to both. As admin POST `/hdc/settings` with `action=wipe_data`, `wipe_targets=projects`, confirmation checked and `wipe_confirm_text=WIPE`.  
**Expected:** refuse a partial wipe with dependent records, or apply a documented, consistent cross-module cleanup preserving any required history. The resulting database must not contain references to nonexistent projects or stages.  
**Actual:** the request redirects and the project and stage are removed, while the expense remains with its old project and stage IDs. `PRAGMA foreign_key_check` reports violations for `hdc_expense` referencing both missing records and for an `hdc_account_txn` referencing the missing project. The settings path takes a safety snapshot before wiping, but that does not prevent corruption of the active dataset.  
**Impact:** expense, account, reporting and other data can be detached from their actual project; subsequent operations can behave unpredictably. Other dependent tables and other wipe combinations were not exhaustively tested.  
**Suggested remediation:** explicitly map dependencies for every wipe target; reject unsafe partial selections or atomically resolve all dependent records using a documented accounting/history policy. Test every single-target and relevant combination with FK checks and cross-module balance checks. Avoid reusing deleted identifiers as a repair strategy.

### TPA-02 — High: wiping Expenses alone leaves an active cash-ledger entry whose source was deleted

**Where:** `hdc/core/admin.py:174–179, 303–335`; accounts reconciliation in `hdc/services/accounts.py:32–100`.  
**Reproduction:** create a project/stage expense of PKR 100 through `/hdc/expenses`, producing its linked account transaction. As admin POST Settings wipe with only `expenses` selected.  
**Expected:** the application should prevent the operation or make the source and ledger treatment consistent and explicit, without leaving active unexplained money.  
**Actual:** expense row is deleted; its account transaction still exists and is active. The reconciliation service reports it as an orphan. Foreign-key check is empty because the source linkage is a polymorphic `source_type`/`source_id`, not a database foreign key.  
**Impact:** cash and financial reports can continue to include a payment whose expense record was removed; reconciliation shows an orphan but does not resolve it. The script had other entries in its balance; the evidence is the surviving active source-linked transaction, not an inference from the aggregate alone.  
**Suggested remediation:** define explicit wipe semantics: block if linked financial history exists, or use an auditable, atomic cross-module reversal/retention strategy. Include ledger balance, source linkage, audit and restore tests. Do not silently remove real financial history.

### TPA-03 — High: deleting an account with only voided history detaches that history and permits a broken restore

**Where:** account deletion in `hdc/services/accounts_manage.py:652–680` and also `hdc/routes/accounts.py:115–133`; restore in `hdc/services/accounts.py:1422–1452`. Both deletion paths only check **active** transactions and only the from/to roles.  
**Reproduction:** seed a valid voided PKR 30 transaction whose destination is a person account, with Company Cash as the paying/executing account. POST `/hdc/accounts/manage` with `action=delete_account` for that person account. Then POST `/hdc/accounts` with `action=restore_transaction` and `transaction_id` for the historical row.  
**Expected:** any referenced account, including voided history, should be retained/archived, and restoration should reject missing or invalid account relationships.  
**Actual:** the person account is hard-deleted; the voided transaction remains, but its `to_account_id` is set to NULL by the ORM relationship. The admin can then restore that row to active. In the test, Company Cash decreases from PKR 900 to PKR 870, while the original receiving account no longer exists. `foreign_key_check` reports no violation because the nullable reference was cleared.  
**Impact:** historical identity is lost and an incomplete transaction can become financially active. This finding was reproduced with an ORM-seeded transaction; the exact UI sequence used to create and void that specific type was not independently verified in this probe.  
**Suggested remediation:** consistently archive accounts with *any* historical reference, including voided rows and all account-reference columns/tables; validate all required parties and balances before restoration. Add tests for both account deletion routes, voided history, destination/source/executing accounts, and restoring after attempted deletion.

### TPA-04 — High: restoring a cancelled tool rental can issue the same stock twice

**Where:** restore route `hdc/routes/tool_rental.py:3747–3757`; `restore_tool_rental` in `hdc/services/tool_rental.py` following `void_tool_rental` around line 2940.  
**Reproduction:** seed a tool with five owned pieces. Through `/hdc/tool-rental/create`, issue five to customer A; cancel the rental via its void route; issue all five to customer B via the create route; then POST the restore route for A.  
**Expected:** restoring A must refuse or otherwise resolve the shortage because all five pieces are already assigned to B; it must never over-allocate inventory.  
**Actual:** A is restored successfully. Tool ledger reports owned 5, out 10, in store 0 and `reconciled=false`. The restore method reconstitutes pending quantities without first checking aggregate available stock; it assigns whatever in-store serial candidates it finds, without requiring enough candidates.  
**Impact:** duplicated commitments and inaccurate availability/holder information; reconciliation flags the discrepancy after it has occurred. Exact serial-history consequences were not separately assessed.  
**Suggested remediation:** before any state mutation, validate available quantities and exact serial availability atomically for every item; handle multiple lines of the same tool and concurrency. Reject restoration if unavailable and retain cancellation state, with regression tests including reissue, partial return, scrapping, serials and concurrent restores.

### TPA-05 — High: deleting a stage silently removes stage attribution from expense and accounts history

**Where:** `hdc/routes/projects.py:701–708`; relationships in `hdc/models/accounts.py:61–70, 175–179`.  
**Reproduction:** create a project and stage; POST a PKR 100 expense for that stage through `/hdc/expenses`; POST `/hdc/stage/<id>/delete`.  
**Expected:** prevent deletion when dependent operational/financial history exists, or preserve stage identity and clearly documented history.  
**Actual:** stage is deleted; expense and its account transaction remain, but their `stage_id` becomes NULL. The database foreign-key check still passes.  
**Impact:** project-level expense can remain while stage-level costs, filters and traceability lose attribution. Other kinds of stage dependencies may instead block deletion or behave differently and were not individually tested.  
**Suggested remediation:** dependency-aware stage archive or deletion guard, with consistent handling of expenses, ledger, labour, materials, tools, contracts, drawings and reports; test the before/after stage and project totals and history.

## Additional risk requiring verification

- The Accounts reconciliation source map includes tool-rental discounts but does not list `tool_rental_payment` (`hdc/services/accounts.py:35–65`); unknown source types are skipped. This is a **coverage limitation identified by inspection**, not a reproduced orphan for this source type. Test missing tool-payment source rows and void mismatches independently before relying on the reconciliation dashboard as a complete orphan scan.
- Settings can wipe several other domains separately while financial and operational links span domains. TPA-01 and TPA-02 establish two unsafe combinations only; do not generalize the exact result to every combination without testing.
- Hard deletion and intentional soft voiding are different: a retained voided record and audit trail are often appropriate. A row is an integrity defect when active balances, source links, ownership or historical attribution become inconsistent—not simply because history still exists.

## Operational precautions

Until fixes are reviewed and deployed, avoid partial Settings wipes and avoid deleting stages or accounts with any historical use. Do not restore a cancelled tool rental after its stock has been reissued without an independently verified stock check. If real data might already be affected, first take and verify a backup and investigate a copy with an authorized administrator; do not run destructive repairs or this scratch-only script against live data.

## Remaining audit and retest plan

1. Add regression cases for each finding and review intended retention/accounting policy before application changes. Rerun probes and verify both database and UI/report outcomes.
2. Inventory each destructive/void/restore/admin-wipe path across all modules and dependencies, including reverse links, nullable FKs and polymorphic `source_type/source_id`. Test each with active, voided, linked, partial and concurrent histories.
3. Exercise tool purchases, serials, transfers, returns, damage/loss, rental payments/discounts, project/stage cost reports and account reconciliation end to end, including deleting or voiding from each side.
4. Complete browser-driven interaction, access-control/IDOR, upload, accessibility and realistic concurrency testing. The current environment had no working browser; no browser sign-off is claimed.
5. If an authorized, sanitized copy of real data becomes available, perform a read-only integrity and reconciliation review against that copy. No real project, tool, or account records were provided or examined here.
