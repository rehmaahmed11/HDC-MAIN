# Cross-module deletion safety — fixes and verification

This follows the initial findings in `AUDIT.md`; that report and `probe-results.txt` are historical pre-fix evidence. `post-fix-results.txt` is the same isolated probe after these changes. No production data was accessed or repaired.

## Changes

- **Projects / Expenses wipes (TPA-01/02):** before deleting, reserve the SQLite writer and inspect actual incoming foreign keys outside the selected tables. Refuse a wipe if surviving data depends on it. Refuse deletion of expenses, owner payments, or tool payment/discount sources in these protected wipes when Accounts retains a linked polymorphic transaction. Keep FK enforcement enabled and defer constraint checking until the wipe transaction commits. Unsafe operations roll back and Settings displays the failure. An empty expenses or projects wipe still works. Other wipe-only paths have not been redesigned in this change.
- **Historical accounts (TPA-03):** both account deletion routes use the same service. Any actual incoming SQLite foreign-key reference, including voided entries and executing-account links, causes archive instead of hard deletion. Transaction restoration rejects archived/missing accounts and missing required recipient/party information; restore the account first when appropriate.
- **Tool reinstatement (TPA-04):** reserve the SQLite writer before checking, aggregate required quantities across duplicate tool lines, inspect unscoped stock and serial availability, and reject reinstatement before mutating the rental if insufficient. Create and edit routes also reserve the writer before their stock checks. In-store maintenance pieces cannot be assigned by reinstatement.
- **Stage deletion (TPA-05):** reserve the writer and check actual incoming foreign-key references before deleting. If any linked history exists, keep the stage and show a warning; an empty stage can still be deleted.

These safeguards do not automatically repair previously corrupted data. Partial wipes of other groups, legacy databases whose schema lacks declared foreign keys, and all other cross-module deletion paths still require independent review. A schema-level check cannot discover every polymorphic or denormalized reference. Existing backups and copies should be evaluated by an authorized administrator before any data repair.

## Verification

- New isolated regression tests: `python -m unittest discover -s tests -p 'test_cross_module_deletion_safety.py' -v` — **10 passed**. Covers all five findings, both account routes, legacy missing-party restoration, available-stock reinstatement, and safe empty operations.
- Full Python suite: `python -m unittest discover -s tests -p 'test_*.py' -v` — **1,209 tests passed** in 999.205 seconds, no skips reported.
- Post-fix rerun of `python qa/tools-projects-accounts/probe.py`: linked stage and accounts remained intact, unsafe wipes retained their source records, rental stayed cancelled when stock was unavailable, FK check was clean, and tool stock reconciled. See `post-fix-results.txt`. This probe uses seeded fixture data for some states and Flask's test client.
- Runtime inventory and financial acceptance passed after the core fixes: 327 rules, 398 CSRF probes, all templates/assets checked, fresh DB integrity clean, and sampled reconciliation/void-sync checks passed. Layer check, DB safety, frontend organization, Python compilation and `git diff --check` passed.
- Earlier standalone HTTP business walkthrough had an unrelated pre-existing cash-flow test-scenario failure (`../master-qa/REPORT.md` QA-001); it was not fixed or rerun as part of this change. Browser testing remains blocked by unavailable Chromium in this environment. Full production-data and concurrency verification remains outstanding.
