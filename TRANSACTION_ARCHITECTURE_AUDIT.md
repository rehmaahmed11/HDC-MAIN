# Transaction architecture audit after PR #70

**Date:** 2026-10-03  
**Scope:** the uniform transaction pattern introduced by PR #70, plus every
module-specific money flow that appears in the Accounts/Money Center inventory.

## Executive verdict

PR #70 is correct about the shared entry surfaces:

* `/hdc/accounts/new-transaction`, `/hdc/accounts/money-center`, and the Cash
  Flow Register render the same form;
* all three marshal through `create_entry_from_form()` and then
  `save_manual_cash_flow_entry()`;
* that engine writes the cash-flow document (`CashFlowEntry`) and the
  authoritative cash movement (`AccountTransaction`) together, with the
  register row linked to its one ledger row.

The uniform form is a **uniform cash-movement pattern**, not a replacement for
feature-specific settlement forms. A worker payment, supplier payment,
subcontractor settlement, office-staff payment, tool-rental payment, and shared
expense have a second authoritative ledger (payable, receivable, loan, split,
or inventory meaning). A generic cash entry can move money, but it cannot safely
settle one of those balances merely because a matching party or project was
selected. Those workflows therefore remain on their owning module pages.

The audit found and fixed three inconsistencies in the shared engine:

1. a register reference was kept on `CashFlowEntry` but dropped from the linked
   `AccountTransaction.reference_id`;
2. the idempotency key protected only one rendered form, so the same active
   document reference could be posted again from another form/page;
3. project receipts were mirrored into `OwnerPayment`, but the shared path did
   not apply the existing Accounts rule for an over-receipt against a positive
   pending receivable.

The fixes are covered by register, amendment, and project-receipt tests. A
project with no established contract value remains permissive, matching the
existing Accounts behaviour; it is not silently treated as a zero receivable
that rejects every advance.

## Posting invariants

| Invariant | Result of the audit |
|---|---|
| One shared-form post has one cash document and one linked ledger posting | **Pass.** The three shared surfaces use the same service; the register stores `account_tx_id`, and the ledger row stores the `cash_flow_entry_*` source link. |
| Ledger is the cash authority | **Pass.** Balances and day close derive from active `AccountTransaction` rows. `CashFlowEntry` supplies the operator-facing document and filters. |
| Register reference is traceable in both layers | **Fixed.** The normalized reference now populates both `CashFlowEntry.reference` and `AccountTransaction.reference_id`. |
| Same active movement cannot be entered twice by document reference | **Fixed.** A non-empty reference is checked against active rows with the same direction, source account, minor-unit amount, and calendar date. Voided rows are ignored so amend/replace can reuse the original reference. The existing idempotency key remains the fast retry guard. |
| Project receipt updates project receivable | **Pass.** `project_effect='receipt'` derives the client, writes the linked `OwnerPayment`, and synchronizes void/restore. The shared path now also rejects an amount above a positive pending receivable, using the same condition as Accounts. |
| Generic party/project fields settle a feature payable automatically | **Intentionally no.** A generic cash entry is not a worker/supplier/subcontractor/office settlement. This prevents a payment from appearing both as an unallocated cash movement and as a feature-ledger settlement. |

## Transaction matrix

### A. Categories available on the uniform form

These are the 16 seeded cash-flow categories, followed by the shared form's
category-free internal transfer movement. Every category row is selectable on
the shared form; the `CashFlowCategory` field rules determine which optional
context is shown. `source ledger` means a second module-owned record is updated
inside the same posting operation.

| Uniform category / movement | Direction | Shared engine effect | Source ledger or side-effect | Status / boundary |
|---|---:|---|---|---|
| Owner / Client Receipt | In | `CashFlowEntry` + `AccountTransaction` | `OwnerPayment` mirror; client is derived from the selected project | **Feature-aware and complete.** Project required; positive pending receivable cap; zero-contract project remains allowed. |
| Scrap & Salvage Sale | In | Generic cash receipt | None | **Generic cash only.** |
| Loan Received | In | Register cash receipt | `LoanMovement` via loan effect `take` | **Feature-aware.** Loan party/rules and loan service remain authoritative for the loan balance. |
| Loan Recovery | In | Register cash receipt | `LoanMovement` via loan effect `recover` | **Feature-aware.** Requires the loan workflow's valid borrower/open-loan meaning. |
| Other Income | In | Generic cash receipt | None | **Generic cash only.** |
| Material & Purchase | Out | Generic cash payment | None | **Generic cash only.** A supplier name is context, not a `SupplierLedger` settlement. |
| Labour & Wages | Out | Generic cash payment | None | **Generic cash only.** It does not create a `LabourLedger` payment. |
| Subcontractor Payment | Out | Generic cash payment | None | **Generic cash only.** It does not create a `SubcontractPayment`. |
| Fuel & Transport | Out | Generic cash payment | None | **Generic cash only.** |
| Equipment & Machinery | Out | Generic cash payment | None | **Generic cash only.** |
| Office Expense | Out | Generic cash payment | None | **Generic cash only.** The seeded rule hides party/project context. |
| Staff Salary | Out | Generic cash payment | None | **Generic cash only.** It does not create an office-staff salary row. |
| Personal Expense | Out | Generic cash payment | None | **Generic cash only.** |
| Loan Given | Out | Register cash payment | `LoanMovement` via loan effect `give` | **Feature-aware.** |
| Loan Repayment | Out | Register cash payment | `LoanMovement` via loan effect `repay` | **Feature-aware.** |
| Internal Transfer | Transfer | One register document + one transfer ledger row | None | **Generic treasury movement.** Source and destination must be different active money accounts. |

The generic category list deliberately does **not** promise that a selected
party is a payable party. Existing acceptance tests allow a bare `Material &
Purchase` entry and a generic entry with a supplier; that behaviour is retained.
If the product later wants supplier settlement from this form, it must add an
explicit source-ledger effect and settlement identity, not infer it from
`party_type`.

### B. Feature-specific flows that must stay on their owning surfaces

These flows are in the broader Money Center business-flow inventory, but are
not generic categories that the uniform form should silently reinterpret.

| Business flow | Owning surface / path | Cash posting authority | Feature record that must also change | Uniform-form conclusion |
|---|---|---|---|---|
| Project receipt entered from the Projects page | Projects owner-payment route | Accounts posting helper (`_accounts_post_owner_receipt`) | `OwnerPayment` | Keep the route for the project-first workflow. The shared receipt category now produces the same project-side meaning, but legacy project-page rows remain a separate document surface. |
| Worker wage, advance, and tip/bonus | Workers, payroll, and timekeeping routes | `_accounts_post_labour_ledger_row` / Accounts transaction engine | `LabourLedger` plus the appropriate `Expense` rows | Keep feature route; generic Labour & Wages is cash-only. Overpayment splits and tip linkage cannot be inferred by the generic form. |
| Supplier/material payment and supplier refund/credit | Purchase v2, supplier, and purchase API routes | Supplier/Accounts posting helpers and purchase-paid upsert | `SupplierLedger`, `PurchaseV2`, and settlement expense rows where applicable | Keep feature route; generic Material & Purchase does not clear supplier payable. |
| Subcontractor payment | Subcontractor route | `_accounts_post_subcontract_payment_row` | `SubcontractPayment` plus expense/event synchronization | Keep feature route; contract balance and settlement semantics are feature-specific. |
| Subcontractor labour-worker payment | Subcontractor route | `_accounts_post_subcontract_labour_payment_row` | `SubcontractLabourPayment` | Keep feature route; generic payroll-like cash is not this ledger. |
| Office staff salary and advance | Office staff ledger route | `_accounts_upsert_office_staff_ledger_txn` | `OfficeStaffLedger` and mirrored `OfficeExpense` where applicable | Keep feature route; generic Staff Salary is cash-only. |
| Standalone office expense | Office expense route | Office Accounts helper | `OfficeExpense` | Keep feature route for its source row and void synchronization. |
| Tool-rental customer payment | Tool Rental route/service | `post_tool_rental_payment_to_accounts` | `ToolRentalPayment` and `ToolRentalAccountTxn` | Keep feature route; the rental balance is not a generic client/project receipt. |
| Shared expense payment | Shared Expenses route/service | `post_expense_to_accounts()` calls `save_manual_cash_flow_entry()` | `SharedExpense`, shares, and the linked `cf_entry_id`/`txn_id` | The cash side already uses the authoritative shared engine. The wrapper must remain because split allocation is feature meaning. |
| Shared-party settlement | Shared Expenses route/service | `post_settlement_to_accounts()` calls the shared engine | `SharedSettlement` and linked cash/ledger ids | Keep wrapper; a normal transfer does not settle the split ledger. |
| Purchase v2 marked paid | Purchase v2 route/service | Purchase paid upsert through Accounts | `PurchaseV2` payment status and supplier linkage | Keep feature route; it must be idempotent against the purchase source id. |
| General/material Expense entry | Expenses route | `_accounts_post_expense_row` | `Expense` | Keep feature route when the project P&L/source row is intended. A generic cash outflow does not create this row. |
| Personal/party payment from Accounts | Accounts/personal-management route | `_accounts_post_personal_expense_row` or direct Accounts engine | `PersonalExpense` when applicable | Keep feature route when the personal source row is required. Generic Personal Expense is cash-only. |
| Direct Accounts transfer and executor-split transfer | Accounts transaction form | `_create_account_transaction` with optional `group_id` split | AccountTransaction group rows | Keep direct Accounts workflow; the shared form's transfer is intentionally one cash-flow document and does not model executor splits. |
| Tool purchase/inventory cost | Tool inventory / purchase paths | Expense or purchase posting path | Tool/purchase source record | Keep feature route; rental income and inventory cost are different meanings. |

### C. Loan and shared-engine wrappers

Loans and shared expenses are important exceptions to the simple “generic versus
feature page” split:

* the **loan** category is selected on the uniform form, but its `loan_effect`
  hook calls `hdc.services.loans` in the same transaction; the form therefore
  does not bypass the loan ledger;
* **shared expenses** call the same `save_manual_cash_flow_entry()` engine from
  their service, then attach the resulting register/ledger ids to the split
  record; they do not write a second cash transaction;
* other feature modules use the Accounts posting helpers directly because their
  source row is created first and must be linked to the ledger atomically. That
  is an intentional source-ledger workflow, not an accidental second generic
  form.

## Findings and follow-up boundaries

### Fixed in this audit

* **Reference propagation:** `_cf_build_tx_payload()` now passes the register's
  normalized reference into `AccountTransaction.reference_id`.
* **Cross-page duplicate protection:** active exact-reference duplicates are
  blocked by direction, source account, amount in minor units, and day. Voided
  history does not block an amendment/replacement.
* **Project receipt cap:** a receipt is refused only when Accounts-style pending
  receivable is positive and the submitted amount exceeds it. A project with no
  contract value is not capped by an artificial zero.
* **Effect coverage:** tests assert the ledger reference, duplicate/amend
  behaviour, project mirror, positive-receivable rejection, zero-contract
  permissiveness, and void/restore effects.

### Deliberately not inferred from a generic cash tag

A generic project-linked outflow still writes `project_id`/`stage_id` onto the
cash-flow and ledger rows, but it does **not** create an `Expense` source row.
Consequently it does not currently update the project's material/expense P&L
KPIs. This is an open product decision, not a hidden side effect to guess from
“Material & Purchase” or from a party name. If required later, add an explicit
`project_effect='cost'` (with source-row identity and void/amend rules) rather
than making every project-tagged cash payment an expense.

Likewise, the generic form does not automatically settle workers, suppliers,
subcontractors, or office staff. The Money Center's pending links intentionally
send operators to those owning workflows, preventing one cash movement from
leaving the payable open or creating a second settlement.

## Verification

Targeted coverage added in `tests/test_cashflow_register.py`:

* `test_register_reference_is_copied_to_the_linked_ledger_row`;
* `test_exact_reference_duplicate_guard_ignores_voided_rows`;
* `test_positive_project_receivable_cannot_be_over_received`;
* `test_zero_contract_project_remains_permissive`.

The pre-existing deep category tests continue to assert that generic
`Material & Purchase` remains optional for party/project, while project receipts
and loan categories retain their feature rules.
