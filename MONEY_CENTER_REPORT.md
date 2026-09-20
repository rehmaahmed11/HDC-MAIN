# Money Center — Unified Smooth Money Handling from Accounts Section

## Overview
This report documents the implementation of unified money handling from the **Accounts Section** as requested: "Read all app and see what are money related points in/out transfer all types all modules and handle them from Account Section and make them smooth."

## Full Money Inventory (22 Flows)

### IN — Money Coming Into Company (6 flows)
| # | Flow ID | Label | Module | Tx Type | Posting Path | Pending Calc | UX |
|---|---------|-------|--------|---------|--------------|--------------|----|
| 1 | `owner_receipt` | Owner / Client Receipt | projects | `project_income` | `_accounts_post_owner_receipt` → from client account → company cash/bank | `Project.remaining_receivable = contract - received` | Receiving account required, auto client account, amount capped at pending |
| 2 | `client_payment` | Client Payment (Generic) | accounts | `client_payment` | Direct Accounts posting | Same as owner_receipt | Generic client income |
| 3 | `party_receipt` | Receive from Credit/Debit Party | accounts | `party_receipt` | Direct type=party_receipt, from party → company | Supplier ledger pending | Refunds, rental income |
| 4 | `tool_rental_income` | Tool Rental Income | tool_rental | `party_receipt` | `post_tool_rental_payment_to_accounts` → creates `ToolRentalAccountTxn` link + `AccountTransaction` | `ToolRental.total - sum(payments)` | Receiving account must be company/cash/bank, void sync via `void_tool_rental_payment_in_accounts` |
| 5 | `cashflow_in` | Cash Flow Register - Money In | cashflow_register | `party_receipt / project_income / client_payment` | `CashFlowEntry` (immutable doc) → 1 `AccountTransaction`, day lock prevents back-date | Daily expected vs counted via `CashDayLock` | Category, party, project scoped, audit trail |
| 6 | `supplier_refund` | Supplier Refund / Settlement Credit | purchase_v2 | `party_receipt` (negative debit) | Settlement → negative Expense + SupplierLedger credit + void sync | `Supplier payable = debit - credit` | Reduces payable |

### OUT — Money Going Out of Company (13 flows)
| # | Flow ID | Label | Module | Tx Type | Posting Path | Pending Calc | Excess Handling |
|---|---------|-------|--------|---------|--------------|--------------|-----------------|
| 1 | `worker_payment` | Worker Wage Payment | workers/payroll/timekeeping | `payroll` | `_accounts_post_labour_ledger_row` → company → worker person account | `_worker_payable_snapshot: earned - (paid+advance+tip)` | Overpayment split: payment_part=min(amount,pending), excess=tip+advance, tip requires project+stage, settlement negative Expense |
| 2 | `worker_advance` | Worker Advance | workers | `advance_to_person` | LabourLedger advance → `advance_to_person` | Future deduction | N/A |
| 3 | `worker_tip` | Worker Tip / Bonus | workers | `payroll (tip)` | Expense Tip with `TIP_WORKER_ID` + LabourLedger tip, `TIP_EXPENSE_ID` embedded for reconciler dedup | Extra over payable | Requires project+stage |
| 4 | `supplier_payment` | Supplier / Material Payment | purchase_v2/materials | `purchase` | `_accounts_post_supplier_credit_row` + `_accounts_upsert_purchase_paid_txn`, PurchaseV2 total=qty*unit_price | Supplier payable = debit - credit | Overpayment auto advance |
| 5 | `subcontractor_payment` | Subcontractor Payment | subcontractors | `expense_subcontractor` | `_accounts_post_subcontract_payment_row`, contract_balance cap, settlement negative Expense | `_subcontract_stage_snapshot` or `payable_balance` | Overpayment split tip/advance, shortfall settlement optional |
| 6 | `subcontractor_labour_payment` | Subcontractor Labour Worker Payment | subcontractors | `payroll` | `_accounts_post_subcontract_labour_payment_row` | Sub-labour attendance | N/A |
| 7 | `office_staff_payment` | Office Staff Salary Payment | office | `office_management_payment` | `_accounts_upsert_office_staff_ledger_txn` + OfficeExpense mirror via `_sync_office_staff_expense_from_ledger` | `_office_staff_ledger_snapshot` balance | Tip/advance split |
| 8 | `office_staff_advance` | Office Staff Advance | office | `office_management_payment` | OfficeStaffLedger advance | Office staff balance | N/A |
| 9 | `office_expense` | Office Expense | office | `office_management_payment` | `_accounts_upsert_office_expense_txn` → company → External Parties | Direct expense | N/A |
| 10 | `material_expense` | Material / Site Expense | expenses | `expense_material / expense_general` | `_accounts_post_expense_row`, Tip/Settlement blocked from direct edit | Direct | N/A |
| 11 | `personal_expense` | Personal / Party Payment | accounts/personal_management | `personal_management_payment / party_payment` | Accounts can create PersonalExpense, type=party_payment | Direct | Beneficiary auto-resolved to person account |
| 12 | `purchase_paid` | Purchase (Paid) - Materials | purchase_v2 | `purchase` | `_accounts_upsert_purchase_paid_txn`, upsert on existing | payment_status | Marks purchase paid |
| 13 | `tool_purchase` | Tool Purchase | tool_rental | `expense_general / purchase` | Via Expense or PurchaseV2, ToolRentalAccountTxn for income only | N/A | N/A |

### TRANSFER — Internal Movement (3 flows)
| # | Flow ID | Label | Module | Tx Type | Posting Path | Overdraft | Notes |
|---|---------|-------|--------|---------|--------------|-----------|-------|
| 1 | `transfer_company` | Internal Transfer (Company Accounts) | accounts/cashflow_register | `transfer` | Direct transfer: both company/cash/bank, split by executor if different | Blocked if from would go negative | Both must be company type, from≠to |
| 2 | `transfer_split` | Split Transfer (Source → Executor → Destination) | accounts | `transfer (step1) + actual type (step2)` | `_build_account_txn_rows`: if from≠executed_by, creates 2 rows same group_id | Checked both steps | Example: Bank→Cash (step1), Cash→Worker (step2), same group_id |
| 3 | `advance_person` | Advance to Person (Credit/Debit) | accounts/workers | `advance_to_person` | Creates receivable, recovered from future wages | Blocked | Worker advance balance |

## Single Source of Truth — How It Works

### Unified Ledger
- All money flows post to `hdc_account_txn` via `hdc/services/accounts.py` engine
- Balances derived as `opening + incoming - outgoing`, never stored
- Every module's money posting goes through `_create_accounts_transaction_with_sync`

### Overdraft Protection (Enhanced)
- Now uses exact minor units (paisa) via `to_minor`/`from_minor` from `hdc/utils/money.py`
- `Rs. 100.00 - Rs. 100.00 = 0` exactly, not `1e-10` floating error
- Treasury accounts (`company`, `cash`, `bank`) cannot go negative
- Checked on both steps of split transfers sequentially
- Readable error: "Would be -X PKR after this transaction. Overdraft blocked for treasury accounts."

### Duplicate Guard
- `source_type:source_id` unique constraint (e.g., `labour_ledger_payment:123:direct`)
- `_has_recent_duplicate` time-window check (few seconds) on AccountTransaction
- Group_id handling: splits with same group_id not considered duplicates

### Void Sync
- `_accounts_set_void_by_source(source_type, source_id, make_void)` voids all linked txns
- `_sync_source_row_void_state` syncs source row void ↔ txn void
- LabourLedger void ↔ AccountTransaction void + Expense auto removed
- OfficeStaffLedger void → `_remove_office_salary_expense_for_ledger` + void txn
- SubcontractPayment void ↔ Expense void + event log
- OwnerPayment void ↔ txn void
- ToolRentalPayment void via `void_tool_rental_payment_in_accounts` voids both sides
- PurchaseV2 void/unpaid → voids txn, paid → creates

### Receipt Consistency
- Every transaction generates receipt via `_receipt_company_profile`
- Split transactions linked by `group_id`, receipt shows both legs
- `ToolRentalAccountTxn` links rental payment to ledger txn

### Reconciliation
- Forensic scan: `orphan_txns`, `void_mismatch`, `duplicate_active`, `group_inconsistent`, `orphan_sources`
- `_accounts_reconciliation_snapshot` checks: purchase_paid_missing, stale_active, owner_missing, labour_missing, etc.
- Day Close: `CashDayLock` compares counted vs expected, locks day, carries closing forward

## Smooth UX — Money Center (New Primary Entry)

### Route: `/hdc/accounts/money-center`
**Design:** 3-step guided flow — Direction → Type → Details

**Step 1 — Direction:**
- Money IN (green): Owner receipt, rental income, refund, party receipt
- Money OUT (red): Worker, supplier, subcontractor, office, expense
- Transfer (blue): Cash ↔ Bank internal

**Step 2 — Type:**
- Dynamic grid filtered by direction
- Each card shows icon, label, tx_type, pending kind, intent
- Color-coded by direction

**Step 3 — Details:**
- Date, Amount with real-time excess calculation
- Pending snapshot: `Rs. X pending | Total: Y | Paid: Z | message`
- Excess split UI: Tip / Advance auto-calc when amount > pending
- From Account with balance hints (pos/neg/warn)
- To Account (company for IN, party for OUT)
- Project / Stage with dynamic loading via `/hdc/api/project_stages/<id>`
- Related Entity (worker/supplier/subcontractor/office_staff) with search
- Office Target (staff/expense) toggle
- Expense Category selectors
- Party/Purpose off-ledger
- Reference ID, Note
- Settle shortfall checkbox for payroll/subcontractor
- Single "Post to Unified Ledger" button with protection badges

**Additional Features:**
- Treasury Accounts chips with balances (pos/neg/zero)
- Recent ledger timeline (last 15)
- Pending payables across all modules with Pay/Receive buttons that prefill form
  - Workers: `_worker_payable_snapshot`
  - Subcontractors: `payable_balance`
  - Suppliers: `debit - credit` from SupplierLedger
  - Office Staff: `_office_staff_ledger_snapshot`
  - Projects Receivable: `remaining_receivable`
  - Tool Rentals Receivable: `total - sum(payments)`
- Money flows inventory tabs: IN / OUT / TRANSFER / Pending / How It Works
- Each flow card documents: module, posting path, tx_type, category, overdraft, duplicate guard, void sync, pending calc, quick action
- How It Works tab: guarantees, inventory count, smooth UX map, posting paths table

### Enhanced Existing Pages

**Accounts Hub (`/hdc/accounts/hub`):**
- Added Money Center highlight section with gradient, badges, stats
- New toolbar button: Money Center — Smooth Entry (green)
- Explains Money Center as new primary entry

**Transactions (`/hdc/accounts`):**
- Added Money Center banner at top with green gradient
- Quick Actions now includes Money Center button
- Existing intent_matrix, direction filter, counterparty group, excess split, balance hints remain but now link to Money Center as smoother alternative

**Sidebar (`base.html`):**
- Added Money Center as first item under Accounts & Cash with "New" badge
- Order: Money Center → Hub → Manage → Transactions → All Entries → CF Register → Cash Flow → Day Close

### New Service: `hdc/services/money_hub.py`

**Functions:**
- `get_all_money_flows()` — 22 flows sorted by direction
- `get_money_flows_by_direction(direction)` — filter IN/OUT/TRANSFER
- `get_money_flows_grouped()` — dict with in/out/transfer
- `get_pending_payables_detailed()` — aggregates pending across workers, subcontractors, suppliers, office_staff, projects_receivable, tool_rentals_receivable with totals and counts
- `get_money_accounts()` — active treasury accounts with balances + minor units
- `get_money_kpis(date_from, date_to)` — unified KPIs: accounts + payables + receivables + today in/out/transfer + recon health + overdraft
- `get_money_flow_diagram()` — nodes and edges for visualization
- `get_smooth_entry_config()` — config for smooth entry: intent, direction, required/optional fields, account_filter, pending_entity, overdraft_check, tips

### New Routes: `hdc/routes/money_center.py`

**Pages:**
- `GET/POST /hdc/accounts/money-center` — Money Center page with quick transaction posting, including tool rental income special handling

**JSON APIs:**
- `GET /hdc/accounts/money-center/api/flows?direction=in|out|transfer` — flows
- `GET /hdc/accounts/money-center/api/pending` — pending payables detailed
- `GET /hdc/accounts/money-center/api/kpis` — KPIs
- `GET /hdc/accounts/money-center/api/accounts` — money accounts
- `GET /hdc/accounts/money-center/api/entry-config` — entry config + intent_matrix
- `POST /hdc/accounts/money-center/api/quick-post` — quick post JSON
- `GET /hdc/accounts/money-center/api/diagram` — flow diagram
- `GET /hdc/api/workers` — worker options (active)
- `GET /hdc/api/suppliers` — supplier options (not void)
- `GET /hdc/api/subcontractors` — subcontractor options with project_id/stage_id
- `GET /hdc/api/office_staff` — office staff options
- `GET /hdc/api/expense_categories` — expense categories
- `GET /hdc/api/project_stages/<project_id>` — stages for project

**Additional APIs in `api_accounts.py`:**
- `GET /api/accounts/money_flows?direction=` — all or filtered flows
- `GET /api/accounts/money_pending` — pending
- `GET /api/accounts/money_kpis` — KPIs
- `GET /api/accounts/money_accounts` — accounts
- `GET /api/accounts/money_entry_config` — entry config

### Enhanced: `hdc/services/accounts.py`

**Overdraft Protection Improved:**
- `_check_overdraft_block` now uses `to_minor` for exact paisa math
- `_check_overdraft_block_replace` same
- Balance map converted to minor units before arithmetic
- Error messages include readable balance after transaction
- Prevents floating-point edge cases

## Acceptance Criteria Met

✅ **Inventory all money types (in/out/transfer) across modules:**
- 22 flows documented in `MONEY_FLOWS` list with id, direction, label, module, route, tx_type, category, accounts_type, source_model, posting, overdraft, duplicate_guard, void_sync, pending_calc, icon, color, quick_action, accounts_entry_intent
- Grouped: IN=6, OUT=13, TRANSFER=3

✅ **Ensure they are posted/voided via Accounts ledger with single source of truth:**
- Every flow's posting path documented and implemented via `_create_accounts_transaction_with_sync` or specialized helpers that call it
- `hdc_account_txn` is sole ledger, balances derived
- All existing modules already post via accounts service, Money Center adds unified entry that also uses same service

✅ **Overdraft protection:**
- Enhanced to use minor units exact
- Treasury accounts (company/cash/bank) cannot go negative
- Checked sequentially for split transfers
- Readable error messages

✅ **Duplicate guard:**
- `source_type:source_id` unique + `_has_recent_duplicate` time-window
- Group_id split handling
- Documented per flow

✅ **Reconciliation:**
- Forensic scan existing + Money Center shows recon issues count
- Day Close and Reconciliation Check linked
- Health KPI shows overdraft + recon issues

✅ **Smooth UX from Accounts section:**
- Money Center: 3-step stepper (Direction → Type → Details)
- Real-time pending payable snapshot via `/api/accounts/pending_context`
- Balance hints with color (pos/neg/warn)
- Excess split auto-calc
- Pending payables list with Pay/Receive buttons that prefill form
- Treasury accounts chips
- Recent timeline
- Flows inventory with quick actions
- Sidebar primary entry with New badge
- Hub highlight
- Transactions page banner linking to Money Center
- Single "Post to Unified Ledger" button

## Files Changed / Added

**New Files:**
- `hdc/services/money_hub.py` — full inventory + helpers (22 flows, pending, KPIs, entry config, diagram)
- `hdc/routes/money_center.py` — Money Center page + APIs + entity options
- `templates/hdc/accounts/money_center.html` — smooth UI with stepper, pending, flows, KPIs
- `MONEY_CENTER_REPORT.md` — this report

**Modified Files:**
- `hdc/routes/__init__.py` — register money_center
- `hdc/routes/accounts_manage.py` — hub context includes money flows + pending
- `hdc/routes/api_accounts.py` — add money hub APIs
- `hdc/services/accounts.py` — overdraft check uses minor units exact
- `templates/hdc/shared/base.html` — sidebar adds Money Center first with New badge
- `templates/hdc/accounts/accounts_hub.html` — adds Money Center highlight section + toolbar button
- `templates/hdc/accounts/accounts.html` — adds Money Center banner + quick action button

## How to Use Money Center (Smooth Flow)

1. **Open Money Center** from sidebar (Accounts & Cash → Money Center) or Hub or Transactions banner
2. **See KPIs:** Company total, today in/out, payable/receivable, health
3. **Step 1:** Click Money IN / OUT / Transfer direction card
4. **Step 2:** Select transaction type from grid (e.g., Wage Payment, Supplier Payment, Receive from Project, Transfer)
5. **Step 3:** Enter date, amount — pending auto-shows if related entity selected, excess split auto-calculates
6. **Select accounts:** From (company) with balance hint, To (company for IN, party for OUT)
7. **Project/Stage:** Select if needed, stages load dynamically
8. **Related Entity:** Worker/supplier/subcontractor/office_staff — pending calc shown
9. **Post:** Single button posts to unified ledger with overdraft block, duplicate guard, void sync
10. **Pending tab:** See all payables/receivables, click Pay/Receive to prefill form with amount and entity
11. **Flows tabs:** Browse all 22 flows with documentation, click Quick Action to jump to form
12. **How It Works:** Understand single source of truth, guarantees, posting paths

All money handled smoothly from Accounts section, no need to jump between modules for basic money operations, but modules remain accessible via Open Module links.

## Technical Notes

- **Backward compatible:** Existing routes untouched, Money Center is additional
- **No breaking changes:** All existing posting paths remain
- **Exact money:** Uses `to_minor`/`from_minor` for paisa exactness
- **Security:** All routes admin-only check via `_admin_only()`, login_required
- **Performance:** Pending aggregation limited to 500 per entity, recent txns 20, tool rentals 100
- **Extensibility:** Add new flow by appending to `MONEY_FLOWS` list, entry config, and typeConfigByIntent in template
