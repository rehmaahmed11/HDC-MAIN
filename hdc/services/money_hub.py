"""HDC services.money_hub — unified money inventory and smooth Accounts handling.

This module is the single source of truth for "what are money related points
in/out/transfer all types all modules". Every money flow in the system is
inventoried here with its direction, module, posting path, and UX hints so
the Accounts section can handle everything smoothly from one place.

Money flows are grouped into three directions (company perspective):
  IN  - money coming into company treasury (cash/bank)
  OUT - money going out of company treasury
  TRANSFER - internal movement between company accounts

Each flow documents:
  - where it originates (which module/route)
  - how it posts to the unified ledger (hdc_account_txn)
  - its AccountTransaction type
  - overdraft/duplicate/void behavior
  - pending calculation

The Money Center page (/hdc/accounts/money-center) uses this to render
a smooth, unified entry point for all money operations.
"""

from datetime import date
from typing import Dict, List

from sqlalchemy import func

from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction, Expense, OwnerPayment, PersonalExpense
from hdc.models.cashflow import CashFlowEntry
from hdc.models.materials import PurchaseV2, Supplier, SupplierLedger
from hdc.models.office import OfficeExpense, OfficeStaff, OfficeStaffLedger
from hdc.models.projects import Project, Stage
from hdc.models.subcontract import SubcontractPayment, Subcontractor
from hdc.models.tool_rental import ToolRental, ToolRentalPayment
from hdc.models.workforce import LabourLedger, Worker
from hdc.services.accounts import (
    _ACCOUNT_COMPANY_TYPES,
    _account_balance_map,
    _account_dashboard_kpis,
    _accounts_payable_breakdown_rows,
    _list_accounts_with_balances,
)
from hdc.services.cashflow_register import day_positions, day_totals
from hdc.services.ledger import _office_staff_ledger_snapshot, _worker_payable_snapshot
from hdc.services.subcontract import _subcontract_stage_snapshot
from hdc.utils.dates import _pkt_today
from hdc.utils.money import from_minor, to_minor


# ── Full money inventory ──────────────────────────────────────────────────

MONEY_FLOWS = [
    # ── IN ────────────────────────────────────────────────────────────────
    {
        "id": "owner_receipt",
        "direction": "in",
        "label": "Owner / Client Receipt",
        "module": "projects",
        "route": "hdc_projects",
        "tx_type": "project_income",
        "category": "income",
        "accounts_type": "Company Cash/Bank <- Client Account",
        "source_model": "OwnerPayment",
        "posting": "_accounts_post_owner_receipt -> AccountTransaction type=project_income, from=client account, to=company account",
        "overdraft": "N/A (incoming)",
        "duplicate_guard": "Unique per project/date/amount; recent duplicate check + source_type:source_id",
        "void_sync": "OwnerPayment.is_void <-> AccountTransaction.is_void via _accounts_set_void_by_source",
        "pending_calc": "Project.remaining_receivable = contract - received",
        "icon": "fa-building",
        "color": "emerald",
        "needs_receiving_account": True,
        "quick_action": "Record Receipt",
        "accounts_entry_intent": "receive_from_project",
    },
    {
        "id": "client_payment",
        "direction": "in",
        "label": "Client Payment (Generic)",
        "module": "accounts",
        "route": "hdc_accounts",
        "tx_type": "client_payment",
        "category": "income",
        "accounts_type": "Company <- Client",
        "source_model": "AccountTransaction direct",
        "posting": "Direct Accounts posting type=client_payment",
        "overdraft": "N/A (incoming)",
        "duplicate_guard": "_has_recent_duplicate on AccountTransaction",
        "void_sync": "Void via Accounts All Entries",
        "pending_calc": "Same as owner_receipt",
        "icon": "fa-hand-holding-dollar",
        "color": "emerald",
        "needs_receiving_account": True,
        "quick_action": "Record Income",
        "accounts_entry_intent": "receive_from_project",
    },
    {
        "id": "party_receipt",
        "direction": "in",
        "label": "Receive from Credit/Debit Party",
        "module": "accounts",
        "route": "hdc_accounts",
        "tx_type": "party_receipt",
        "category": "income",
        "accounts_type": "Company <- Person/Vendor/Client",
        "source_model": "AccountTransaction direct",
        "posting": "Direct type=party_receipt, from=party account, to=company",
        "overdraft": "N/A (incoming)",
        "duplicate_guard": "_has_recent_duplicate",
        "void_sync": "Void via Accounts",
        "pending_calc": "Supplier credit/debit ledger pending",
        "icon": "fa-arrow-down",
        "color": "cyan",
        "needs_receiving_account": True,
        "quick_action": "Receive from Party",
        "accounts_entry_intent": "receive_from_credit_debit",
    },
    {
        "id": "tool_rental_income",
        "direction": "in",
        "label": "Tool Rental Income",
        "module": "tool_rental",
        "route": "hdc_tool_rental",
        "tx_type": "party_receipt",
        "category": "income",
        "accounts_type": "Company <- Customer (via ToolRentalAccountTxn link)",
        "source_model": "ToolRentalPayment + ToolRentalAccountTxn",
        "posting": "post_tool_rental_payment_to_accounts -> AccountTransaction + ToolRentalAccountTxn link",
        "overdraft": "N/A (incoming)",
        "duplicate_guard": "ToolRentalAccountTxn unique per payment, source_type check",
        "void_sync": "void_tool_rental_payment_in_accounts voids both sides",
        "pending_calc": "ToolRental.total_amount - sum(payments)",
        "icon": "fa-screwdriver-wrench",
        "color": "emerald",
        "needs_receiving_account": True,
        "quick_action": "Record Rental Payment",
        "accounts_entry_intent": "receive_from_credit_debit",
    },
    {
        "id": "cashflow_in",
        "direction": "in",
        "label": "Cash Flow Register - Money In",
        "module": "cashflow_register",
        "route": "hdc_cashflow_register",
        "tx_type": "party_receipt / project_income / client_payment",
        "category": "income",
        "accounts_type": "Company <- Various (documented via CashFlowEntry)",
        "source_model": "CashFlowEntry (immutable doc) -> AccountTransaction",
        "posting": "CashFlowEntry creates 1 AccountTransaction; day lock prevents back-date",
        "overdraft": "N/A for in; out checked",
        "duplicate_guard": "CashFlowEntry unique doc + AccountTransaction duplicate guard",
        "void_sync": "CashFlowEntry void only (no edit) -> voids linked AccountTransaction",
        "pending_calc": "Daily expected vs counted via CashDayLock",
        "icon": "fa-book",
        "color": "emerald",
        "needs_receiving_account": True,
        "quick_action": "Record in Register",
        "accounts_entry_intent": "receive_from_credit_debit",
    },
    {
        "id": "supplier_refund",
        "direction": "in",
        "label": "Supplier Refund / Settlement Credit",
        "module": "purchase_v2",
        "route": "hdc_purchase_v2_suppliers",
        "tx_type": "party_receipt (negative debit)",
        "category": "income",
        "accounts_type": "Company <- Supplier (settlement reduces payable)",
        "source_model": "SupplierLedger entry_type=credit + Expense negative",
        "posting": "Settlement creates negative Expense + SupplierLedger credit + AccountTransaction void sync",
        "overdraft": "N/A (incoming reduces payable)",
        "duplicate_guard": "SupplierLedger recent duplicate + source_type",
        "void_sync": "Void Expense + SupplierLedger + AccountTransaction together",
        "pending_calc": "Supplier payable = debit - credit",
        "icon": "fa-rotate-left",
        "color": "cyan",
        "needs_receiving_account": False,
        "quick_action": "Record Settlement",
        "accounts_entry_intent": "receive_from_credit_debit",
    },

    # ── OUT ───────────────────────────────────────────────────────────────
    {
        "id": "worker_payment",
        "direction": "out",
        "label": "Worker Wage Payment",
        "module": "workers / payroll / timekeeping",
        "route": "hdc_workers",
        "tx_type": "payroll",
        "category": "payroll",
        "accounts_type": "Company -> Worker (person account)",
        "source_model": "LabourLedger entry_type=payment + Expense (wage)",
        "posting": "_accounts_post_labour_ledger_row -> type=payroll, from=company, to=worker person account; overpayment split tip/advance",
        "overdraft": "Blocked if company/cash/bank <0 after posting (minor units exact)",
        "duplicate_guard": "_has_recent_duplicate on LabourLedger + AccountTransaction source_type:source_id unique",
        "void_sync": "LabourLedger.is_void <-> AccountTransaction.is_void; Expense auto removed; payroll caps at balance",
        "pending_calc": "_worker_payable_snapshot: earned - (paid+advance+tip)",
        "icon": "fa-helmet-safety",
        "color": "rose",
        "needs_receiving_account": False,
        "quick_action": "Pay Worker",
        "accounts_entry_intent": "payroll",
        "excess_handling": "Overpayment auto-split: payment_part=min(amount,pending), excess=amount-payment_part, tip+advance must equal excess",
    },
    {
        "id": "worker_advance",
        "direction": "out",
        "label": "Worker Advance",
        "module": "workers",
        "route": "hdc_workers",
        "tx_type": "advance_to_person",
        "category": "advance",
        "accounts_type": "Company -> Worker",
        "source_model": "LabourLedger entry_type=advance",
        "posting": "_accounts_post_labour_ledger_row type=advance_to_person",
        "overdraft": "Blocked if insufficient company balance",
        "duplicate_guard": "Recent duplicate + source_type:source_id",
        "void_sync": "LabourLedger void <-> AccountTransaction void",
        "pending_calc": "Advances increase worker payable negative (future deduction)",
        "icon": "fa-hand-holding",
        "color": "amber",
        "needs_receiving_account": False,
        "quick_action": "Give Advance",
        "accounts_entry_intent": "payroll",
    },
    {
        "id": "worker_tip",
        "direction": "out",
        "label": "Worker Tip / Bonus",
        "module": "workers",
        "route": "hdc_workers",
        "tx_type": "payroll (tip)",
        "category": "payroll",
        "accounts_type": "Company -> Worker + Expense Tip",
        "source_model": "LabourLedger entry_type=tip + Expense category=Tip",
        "posting": "Creates Expense with TIP_WORKER_ID + LabourLedger tip; TIP_EXPENSE_ID embedded for reconciler dedup",
        "overdraft": "Blocked if insufficient",
        "duplicate_guard": "Reconciler matches by TIP_EXPENSE_ID to prevent double tip",
        "void_sync": "Tip Expense + LabourLedger + AccountTransaction void together",
        "pending_calc": "Tip is extra over payable, requires project+stage",
        "icon": "fa-gift",
        "color": "rose",
        "needs_receiving_account": False,
        "quick_action": "Give Tip",
        "accounts_entry_intent": "payroll",
    },
    {
        "id": "supplier_payment",
        "direction": "out",
        "label": "Supplier / Material Payment",
        "module": "purchase_v2 / materials",
        "route": "hdc_purchase_v2_suppliers",
        "tx_type": "purchase",
        "category": "purchase",
        "accounts_type": "Company -> Supplier (vendor/person account)",
        "source_model": "SupplierLedger entry_type=credit + PurchaseV2 paid status",
        "posting": "_accounts_post_supplier_credit_row type=purchase + _accounts_upsert_purchase_paid_txn; PurchaseV2 total=qty*unit_price",
        "overdraft": "Blocked if insufficient",
        "duplicate_guard": "SupplierLedger recent duplicate + AccountTransaction source_type:source_id",
        "void_sync": "SupplierLedger void <-> AccountTransaction void; PurchaseV2 paid->unpaid on void",
        "pending_calc": "Supplier payable = sum(debit) - sum(credit) from SupplierLedger",
        "icon": "fa-truck-field",
        "color": "rose",
        "needs_receiving_account": False,
        "quick_action": "Pay Supplier",
        "accounts_entry_intent": "purchase",
        "excess_handling": "Overpayment auto advance: payment_part=min(amount,pending), advance=excess",
    },
    {
        "id": "subcontractor_payment",
        "direction": "out",
        "label": "Subcontractor Payment",
        "module": "subcontractors",
        "route": "hdc_subcontractors",
        "tx_type": "expense_subcontractor",
        "category": "expense",
        "accounts_type": "Company -> Subcontractor",
        "source_model": "SubcontractPayment entry_type=payment + Expense",
        "posting": "_accounts_post_subcontract_payment_row type=expense_subcontractor; contract_balance cap; settlement negative Expense",
        "overdraft": "Blocked",
        "duplicate_guard": "Contract balance guard + recent duplicate + source_type",
        "void_sync": "SubcontractPayment void <-> AccountTransaction void + Expense void + event log",
        "pending_calc": "_subcontract_stage_snapshot or Subcontractor.payable_balance",
        "icon": "fa-people-group",
        "color": "rose",
        "needs_receiving_account": False,
        "quick_action": "Pay Subcontractor",
        "accounts_entry_intent": "expense_subcontractor",
        "excess_handling": "Overpayment split tip/advance, shortfall settlement optional",
    },
    {
        "id": "subcontractor_labour_payment",
        "direction": "out",
        "label": "Subcontractor Labour Worker Payment",
        "module": "subcontractors",
        "route": "hdc_subcontractors",
        "tx_type": "payroll",
        "category": "payroll",
        "accounts_type": "Company -> Sub-labour Worker",
        "source_model": "SubcontractLabourPayment",
        "posting": "_accounts_post_subcontract_labour_payment_row type=payroll",
        "overdraft": "Blocked",
        "duplicate_guard": "Recent duplicate",
        "void_sync": "Payment void <-> AccountTransaction void",
        "pending_calc": "Sub-labour worker payable from attendance",
        "icon": "fa-hard-hat",
        "color": "rose",
        "needs_receiving_account": False,
        "quick_action": "Pay Sub-Worker",
        "accounts_entry_intent": "payroll",
    },
    {
        "id": "office_staff_payment",
        "direction": "out",
        "label": "Office Staff Salary Payment",
        "module": "office",
        "route": "hdc_office_staff_ledger_list",
        "tx_type": "office_management_payment",
        "category": "expense",
        "accounts_type": "Company -> Office Staff + OfficeExpense mirror",
        "source_model": "OfficeStaffLedger entry_type=payment + OfficeExpense mirrored via _sync_office_staff_expense_from_ledger",
        "posting": "_accounts_upsert_office_staff_ledger_txn type=office_management_payment + OfficeExpense row",
        "overdraft": "Blocked",
        "duplicate_guard": "Recent duplicate + source_type",
        "void_sync": "OfficeStaffLedger void -> _remove_office_salary_expense_for_ledger + AccountTransaction void",
        "pending_calc": "_office_staff_ledger_snapshot balance",
        "icon": "fa-user-tie",
        "color": "rose",
        "needs_receiving_account": False,
        "quick_action": "Pay Office Staff",
        "accounts_entry_intent": "office_management_payment",
    },
    {
        "id": "office_staff_advance",
        "direction": "out",
        "label": "Office Staff Advance",
        "module": "office",
        "route": "hdc_office_staff_ledger_list",
        "tx_type": "office_management_payment",
        "category": "expense",
        "accounts_type": "Company -> Office Staff",
        "source_model": "OfficeStaffLedger entry_type=advance",
        "posting": "_accounts_upsert_office_staff_ledger_txn",
        "overdraft": "Blocked",
        "duplicate_guard": "Recent duplicate",
        "void_sync": "Ledger void <-> AccountTransaction void",
        "pending_calc": "Office staff balance",
        "icon": "fa-hand-holding-dollar",
        "color": "amber",
        "needs_receiving_account": False,
        "quick_action": "Advance to Staff",
        "accounts_entry_intent": "office_management_payment",
    },
    {
        "id": "office_expense",
        "direction": "out",
        "label": "Office Expense",
        "module": "office",
        "route": "hdc_office_expenses",
        "tx_type": "office_management_payment",
        "category": "expense",
        "accounts_type": "Company -> External Parties (Credit/Debit Control)",
        "source_model": "OfficeExpense standalone",
        "posting": "_accounts_upsert_office_expense_txn type=office_management_payment",
        "overdraft": "Blocked",
        "duplicate_guard": "Recent duplicate + source_type:source_id",
        "void_sync": "OfficeExpense void <-> AccountTransaction void",
        "pending_calc": "N/A - direct expense",
        "icon": "fa-building-columns",
        "color": "slate",
        "needs_receiving_account": False,
        "quick_action": "Record Office Expense",
        "accounts_entry_intent": "office_management_payment",
    },
    {
        "id": "material_expense",
        "direction": "out",
        "label": "Material / Site Expense",
        "module": "expenses",
        "route": "hdc_expenses",
        "tx_type": "expense_material / expense_general",
        "category": "expense",
        "accounts_type": "Company -> External Parties",
        "source_model": "Expense with category Material/General + _accounts_post_expense_row",
        "posting": "_accounts_post_expense_row type=expense_material or expense_general; Tip/Settlement categories blocked from direct edit",
        "overdraft": "Blocked",
        "duplicate_guard": "Recent duplicate + _is_linked_system_expense guard",
        "void_sync": "Expense void <-> AccountTransaction void",
        "pending_calc": "N/A",
        "icon": "fa-cubes",
        "color": "slate",
        "needs_receiving_account": False,
        "quick_action": "Record Expense",
        "accounts_entry_intent": "expense_general",
    },
    {
        "id": "personal_expense",
        "direction": "out",
        "label": "Personal / Party Payment",
        "module": "accounts / personal_management",
        "route": "hdc_personal_management",
        "tx_type": "personal_management_payment / party_payment",
        "category": "personal",
        "accounts_type": "Company -> Person (beneficiary)",
        "source_model": "PersonalExpense + _accounts_post_personal_expense_row",
        "posting": "Accounts transaction can create PersonalExpense; type=party_payment or personal_management_payment",
        "overdraft": "Blocked",
        "duplicate_guard": "PersonalExpense recent duplicate + AccountTransaction duplicate",
        "void_sync": "PersonalExpense void <-> AccountTransaction void",
        "pending_calc": "N/A",
        "icon": "fa-user",
        "color": "slate",
        "needs_receiving_account": True,
        "quick_action": "Pay Personal",
        "accounts_entry_intent": "personal_management_payment",
    },
    {
        "id": "purchase_paid",
        "direction": "out",
        "label": "Purchase (Paid) - Materials",
        "module": "purchase_v2",
        "route": "hdc_purchase_v2_purchases",
        "tx_type": "purchase",
        "category": "purchase",
        "accounts_type": "Company -> Supplier",
        "source_model": "PurchaseV2 payment_status=paid, total_amount=qty*unit_price",
        "posting": "_accounts_upsert_purchase_paid_txn type=purchase, upsert on existing",
        "overdraft": "Blocked",
        "duplicate_guard": "source_id=PurchaseV2.id + source_type=purchase_v2_paid",
        "void_sync": "PurchaseV2 void/unpaid -> voids AccountTransaction; paid-> creates",
        "pending_calc": "PurchaseV2 payment_status",
        "icon": "fa-cart-shopping",
        "color": "rose",
        "needs_receiving_account": False,
        "quick_action": "Mark Paid",
        "accounts_entry_intent": "purchase",
    },
    {
        "id": "tool_purchase",
        "direction": "out",
        "label": "Tool Purchase",
        "module": "tool_rental",
        "route": "hdc_tool_rental_inventory",
        "tx_type": "expense_general / purchase",
        "category": "expense",
        "accounts_type": "Company -> Supplier/External",
        "source_model": "Tool model (purchase cost)",
        "posting": "Currently via Expense or PurchaseV2; ToolRentalAccountTxn for rental income only",
        "overdraft": "Blocked",
        "duplicate_guard": "Expense/Purchase guards",
        "void_sync": "Via Expense/Purchase void",
        "pending_calc": "N/A",
        "icon": "fa-tools",
        "color": "slate",
        "needs_receiving_account": False,
        "quick_action": "Record Tool Purchase",
        "accounts_entry_intent": "expense_general",
    },

    # ── TRANSFER ──────────────────────────────────────────────────────────
    {
        "id": "transfer_company",
        "direction": "transfer",
        "label": "Internal Transfer (Company Accounts)",
        "module": "accounts / cashflow_register",
        "route": "hdc_accounts",
        "tx_type": "transfer",
        "category": "transfer",
        "accounts_type": "Company Account -> Company Account (cash<->bank, bank<->bank, cash<->cash)",
        "source_model": "AccountTransaction type=transfer or CashFlowEntry direction=transfer",
        "posting": "Direct transfer: from_account_id and to_account_id both company/cash/bank; split by executor if different",
        "overdraft": "Blocked if from account would go negative",
        "duplicate_guard": "_has_recent_duplicate + group_id split handling",
        "void_sync": "Void both legs if group_id split; CashFlowEntry void sync",
        "pending_calc": "N/A - balance check only",
        "icon": "fa-right-left",
        "color": "blue",
        "needs_receiving_account": True,
        "quick_action": "Transfer Money",
        "accounts_entry_intent": "pay_intra_company",
    },
    {
        "id": "transfer_split",
        "direction": "transfer",
        "label": "Split Transfer (Source -> Executor -> Destination)",
        "module": "accounts",
        "route": "hdc_accounts",
        "tx_type": "transfer (step1) + actual type (step2)",
        "category": "transfer + actual",
        "accounts_type": "Source Company -> Executor Company -> Final Destination (group_id links both)",
        "source_model": "AccountTransaction group_id with step1=transfer, step2=actual",
        "posting": "_build_account_txn_rows: if from != executed_by, creates 2 rows with same group_id",
        "overdraft": "Checked on both steps sequentially",
        "duplicate_guard": "Group_id ensures both steps treated as one; duplicate check on group",
        "void_sync": "Void by group_id: _account_txn_group_rows + _accounts_set_void_by_source",
        "pending_calc": "N/A",
        "icon": "fa-code-branch",
        "color": "blue",
        "needs_receiving_account": True,
        "quick_action": "Transfer via Executor",
        "accounts_entry_intent": "pay_intra_company",
    },
    {
        "id": "advance_person",
        "direction": "transfer",
        "label": "Advance to Person (Credit/Debit)",
        "module": "accounts / workers",
        "route": "hdc_accounts",
        "tx_type": "advance_to_person",
        "category": "advance",
        "accounts_type": "Company -> Person Account (worker/supplier/etc) - creates receivable",
        "source_model": "AccountTransaction type=advance_to_person + LabourLedger advance",
        "posting": "Creates advance that increases payable negative; recovered from future wages",
        "overdraft": "Blocked",
        "duplicate_guard": "Recent duplicate + source_type",
        "void_sync": "Advance void <-> LabourLedger void",
        "pending_calc": "Worker advance balance",
        "icon": "fa-hand-holding-hand",
        "color": "amber",
        "needs_receiving_account": True,
        "quick_action": "Give Advance",
        "accounts_entry_intent": "pay_to_credit_debit",
    },
]


def get_all_money_flows() -> List[Dict]:
    """Return full inventory, sorted by direction."""
    return sorted(MONEY_FLOWS, key=lambda x: ({"in": 0, "out": 1, "transfer": 2}.get(x["direction"], 9), x["label"]))


def get_money_flows_by_direction(direction: str) -> List[Dict]:
    direction = (direction or "").strip().lower()
    if direction not in ("in", "out", "transfer"):
        return get_all_money_flows()
    return [f for f in MONEY_FLOWS if f["direction"] == direction]


def get_money_flows_grouped() -> Dict[str, List[Dict]]:
    return {
        "in": get_money_flows_by_direction("in"),
        "out": get_money_flows_by_direction("out"),
        "transfer": get_money_flows_by_direction("transfer"),
    }


def get_pending_payables_detailed():
    """Aggregate pending across all payable modules for Money Center.

    Returns dict with:
      - workers: list of {id, name, pending, paid, total}
      - subcontractors: list
      - suppliers: list
      - office_staff: list
      - projects_receivable: list
      - tool_rentals_receivable: list
      - totals: {payable_total, receivable_total, etc}
    """
    today = _pkt_today()

    # Workers
    workers_pending = []
    try:
        workers = Worker.query.filter(Worker.active_status == True).order_by(Worker.name.asc()).all()
        for w in workers:
            snap = _worker_payable_snapshot(w.id)
            pending = float(snap.get("payable") or 0.0)
            if pending > 0.01:
                workers_pending.append({
                    "id": w.id,
                    "name": w.name,
                    "code": w.worker_code or f"W-{w.id}",
                    "pending": pending,
                    "paid": float(snap.get("paid") or 0.0),
                    "total": float(snap.get("earned") or 0.0),
                    "type": "worker",
                })
    except Exception:
        pass
    workers_pending = sorted(workers_pending, key=lambda x: x["pending"], reverse=True)

    # Subcontractors
    subs_pending = []
    try:
        subs = Subcontractor.query.order_by(Subcontractor.name.asc()).all()
        for s in subs:
            pending = max(0.0, float(s.payable_balance or 0.0))
            if pending > 0.01:
                proj_name = s.project.name if getattr(s, "project", None) and getattr(s.project, "name", None) else ""
                stage_name = s.stage_rel.name if getattr(s, "stage_rel", None) and getattr(s.stage_rel, "name", None) else ""
                subs_pending.append({
                    "id": s.id,
                    "name": s.name,
                    "code": s.subcontractor_code or f"SUB-{s.id}",
                    "pending": pending,
                    "paid": float(s.total_cleared or 0.0),
                    "total": float(s.payable_amount or 0.0),
                    "project": proj_name,
                    "stage": stage_name,
                    "type": "subcontractor",
                })
    except Exception:
        pass
    subs_pending = sorted(subs_pending, key=lambda x: x["pending"], reverse=True)

    # Suppliers
    suppliers_pending = []
    try:
        debit_map = dict(
            db.session.query(
                SupplierLedger.supplier_id,
                func.coalesce(func.sum(SupplierLedger.amount), 0.0)
            ).filter(
                SupplierLedger.is_void == False,
                func.lower(SupplierLedger.entry_type) == "debit"
            ).group_by(SupplierLedger.supplier_id).all()
        )
        credit_map = dict(
            db.session.query(
                SupplierLedger.supplier_id,
                func.coalesce(func.sum(SupplierLedger.amount), 0.0)
            ).filter(
                SupplierLedger.is_void == False,
                func.lower(SupplierLedger.entry_type) == "credit"
            ).group_by(SupplierLedger.supplier_id).all()
        )
        suppliers = Supplier.query.filter(Supplier.is_void == False).order_by(Supplier.name.asc()).all()
        for s in suppliers:
            debit = float(debit_map.get(s.id, 0.0) or 0.0)
            credit = float(credit_map.get(s.id, 0.0) or 0.0)
            pending = max(0.0, debit - credit)
            if pending > 0.01:
                suppliers_pending.append({
                    "id": s.id,
                    "name": s.name,
                    "code": s.phone or f"SUP-{s.id}",
                    "pending": pending,
                    "paid": credit,
                    "total": debit,
                    "type": "supplier",
                })
    except Exception:
        pass
    suppliers_pending = sorted(suppliers_pending, key=lambda x: x["pending"], reverse=True)

    # Office Staff
    office_pending = []
    try:
        staff_list = OfficeStaff.query.filter(OfficeStaff.is_void == False).order_by(OfficeStaff.name.asc()).all()
        for st in staff_list:
            snap = _office_staff_ledger_snapshot(st.id)
            pending = max(0.0, float(snap.get("balance") or 0.0))
            if pending > 0.01:
                office_pending.append({
                    "id": st.id,
                    "name": st.name,
                    "code": st.staff_code or f"OFF-{st.id}",
                    "pending": pending,
                    "paid": float(snap.get("paid") or 0.0) + float(snap.get("tip") or 0.0),
                    "total": float(snap.get("total_earned") or 0.0),
                    "type": "office_staff",
                })
    except Exception:
        pass
    office_pending = sorted(office_pending, key=lambda x: x["pending"], reverse=True)

    # Projects Receivable
    projects_receivable = []
    try:
        projects = Project.query.filter(Project.is_void == False).order_by(Project.name.asc()).all()
        for p in projects:
            pending = max(0.0, float(p.remaining_receivable or 0.0))
            if pending > 0.01:
                projects_receivable.append({
                    "id": p.id,
                    "name": p.name,
                    "code": p.project_code or f"PRJ-{p.id}",
                    "client": p.client or "",
                    "pending": pending,
                    "paid": float(p.total_received or 0.0),
                    "total": float(p.owner_contract_value or 0.0),
                    "type": "project",
                })
    except Exception:
        pass
    projects_receivable = sorted(projects_receivable, key=lambda x: x["pending"], reverse=True)

    # Tool Rentals Receivable
    tool_rentals_receivable = []
    try:
        rentals = ToolRental.query.filter(ToolRental.is_void == False).order_by(ToolRental.id.desc()).limit(100).all()
        for r in rentals:
            total = float(r.total_amount or 0.0)
            if total <= 0:
                continue
            paid = float(db.session.query(func.coalesce(func.sum(ToolRentalPayment.amount), 0.0))
                         .filter(ToolRentalPayment.rental_id == r.id, ToolRentalPayment.is_void == False).scalar() or 0.0)
            pending = max(0.0, total - paid)
            if pending > 0.01:
                tool_rentals_receivable.append({
                    "id": r.id,
                    "name": f"Rental #{r.id} - {r.customer_name or 'Customer'}",
                    "code": f"TR-{r.id}",
                    "pending": pending,
                    "paid": paid,
                    "total": total,
                    "type": "tool_rental",
                })
    except Exception:
        pass
    tool_rentals_receivable = sorted(tool_rentals_receivable, key=lambda x: x["pending"], reverse=True)

    total_payable = (
        sum(x["pending"] for x in workers_pending)
        + sum(x["pending"] for x in subs_pending)
        + sum(x["pending"] for x in suppliers_pending)
        + sum(x["pending"] for x in office_pending)
    )
    total_receivable = (
        sum(x["pending"] for x in projects_receivable)
        + sum(x["pending"] for x in tool_rentals_receivable)
    )

    return {
        "workers": workers_pending,
        "subcontractors": subs_pending,
        "suppliers": suppliers_pending,
        "office_staff": office_pending,
        "projects_receivable": projects_receivable,
        "tool_rentals_receivable": tool_rentals_receivable,
        "totals": {
            "workers_pending": sum(x["pending"] for x in workers_pending),
            "subcontractors_pending": sum(x["pending"] for x in subs_pending),
            "suppliers_pending": sum(x["pending"] for x in suppliers_pending),
            "office_staff_pending": sum(x["pending"] for x in office_pending),
            "projects_receivable": sum(x["pending"] for x in projects_receivable),
            "tool_rentals_receivable": sum(x["pending"] for x in tool_rentals_receivable),
            "total_payable": total_payable,
            "total_receivable": total_receivable,
            "net_position": total_receivable - total_payable,
            "workers_count": len(workers_pending),
            "subcontractors_count": len(subs_pending),
            "suppliers_count": len(suppliers_pending),
            "office_staff_count": len(office_pending),
            "projects_count": len(projects_receivable),
            "tool_rentals_count": len(tool_rentals_receivable),
        },
    }


def get_money_accounts():
    """Get active treasury accounts with balances for smooth selection."""
    try:
        accounts = _list_accounts_with_balances(include_inactive=False)
        company_accounts = [a for a in accounts if a["type"] in _ACCOUNT_COMPANY_TYPES]
        all_accounts = accounts
        balance_map = _account_balance_map()
        # Enrich with exact minor units
        for acc in company_accounts:
            acc["balance_minor"] = to_minor(acc.get("current_balance") or 0.0)
        for acc in all_accounts:
            acc["balance_minor"] = to_minor(acc.get("current_balance") or 0.0)
        return {
            "company_accounts": sorted(company_accounts, key=lambda x: x["current_balance"], reverse=True),
            "all_accounts": all_accounts,
            "balance_map": balance_map,
        }
    except Exception:
        return {"company_accounts": [], "all_accounts": [], "balance_map": {}}


def get_money_kpis(date_from=None, date_to=None):
    """Unified KPIs for Money Center: includes accounts + payables + receivables."""
    kpis = _account_dashboard_kpis(date_from=date_from, date_to=date_to)
    pending = get_pending_payables_detailed()
    accounts_info = get_money_accounts()

    # Cashflow today
    today = _pkt_today()
    today_in = today_out = today_transfer = 0.0
    today_count = 0
    try:
        from hdc.services.cashflow_register import register_rows, register_summary
        rows = register_rows(date_from=today, date_to=today, limit=None)
        summary = register_summary(rows)
        today_in = float(summary.get("total_in") or 0.0)
        today_out = float(summary.get("total_out") or 0.0)
        today_transfer = float(summary.get("total_transfer") or 0.0)
        today_count = int(summary.get("count") or 0)
    except Exception:
        pass

    # Reconciliation health
    recon_issues = 0
    try:
        from hdc.services.accounts import _accounts_reconciliation_snapshot
        recon = _accounts_reconciliation_snapshot(limit=100)
        recon_issues = sum(int(r.get("count") or 0) for r in recon)
    except Exception:
        pass

    return {
        **kpis,
        "pending": pending["totals"],
        "today_in": today_in,
        "today_out": today_out,
        "today_transfer": today_transfer,
        "today_count": today_count,
        "company_accounts": accounts_info["company_accounts"],
        "company_accounts_count": len(accounts_info["company_accounts"]),
        "recon_issues": recon_issues,
        "has_overdraft": any(float(a.get("current_balance") or 0.0) < -0.01 for a in accounts_info["company_accounts"]),
    }


def get_money_flow_diagram():
    """Return a diagram structure showing how money flows through modules into Accounts."""
    return {
        "nodes": [
            {"id": "client", "label": "Client / Owner", "type": "external", "direction": "in"},
            {"id": "supplier", "label": "Supplier", "type": "external", "direction": "both"},
            {"id": "worker", "label": "Worker", "type": "external", "direction": "out"},
            {"id": "subcontractor", "label": "Subcontractor", "type": "external", "direction": "out"},
            {"id": "office_staff", "label": "Office Staff", "type": "external", "direction": "out"},
            {"id": "tool_customer", "label": "Tool Rental Customer", "type": "external", "direction": "in"},
            {"id": "company_cash", "label": "Company Cash", "type": "company", "direction": "both"},
            {"id": "company_bank", "label": "Company Bank", "type": "company", "direction": "both"},
            {"id": "credit_debit", "label": "Credit/Debit Control", "type": "control", "direction": "both"},
            {"id": "ledger", "label": "Unified Ledger (hdc_account_txn)", "type": "ledger", "direction": "both"},
            {"id": "cf_register", "label": "CF Register (CashFlowEntry)", "type": "document", "direction": "both"},
            {"id": "day_close", "label": "Day Close (CashDayLock)", "type": "control", "direction": "none"},
        ],
        "edges": [
            {"from": "client", "to": "company_cash", "label": "Owner Receipt (project_income)", "direction": "in"},
            {"from": "client", "to": "company_bank", "label": "Owner Receipt", "direction": "in"},
            {"from": "tool_customer", "to": "company_cash", "label": "Tool Rental Payment", "direction": "in"},
            {"from": "company_cash", "to": "worker", "label": "Wage/Advance/Tip (payroll)", "direction": "out"},
            {"from": "company_bank", "to": "worker", "label": "Wage via Bank", "direction": "out"},
            {"from": "company_cash", "to": "supplier", "label": "Material Payment (purchase)", "direction": "out"},
            {"from": "company_cash", "to": "subcontractor", "label": "Subcontractor Payment", "direction": "out"},
            {"from": "company_cash", "to": "office_staff", "label": "Office Salary", "direction": "out"},
            {"from": "company_cash", "to": "credit_debit", "label": "General Expense", "direction": "out"},
            {"from": "company_cash", "to": "company_bank", "label": "Internal Transfer", "direction": "transfer"},
            {"from": "company_bank", "to": "company_cash", "label": "Internal Transfer", "direction": "transfer"},
            {"from": "cf_register", "to": "ledger", "label": "1 doc -> 1 txn", "direction": "both"},
            {"from": "ledger", "to": "day_close", "label": "Reconciliation", "direction": "none"},
        ],
    }


def get_smooth_entry_config():
    """Return config for smooth money entry from Accounts section.

    Each entry type has:
      - intent: the form intent used in accounts.html JS
      - direction: receive/pay/transfer
      - required_fields: what UI must show
      - account_filter: which accounts to show in from/to dropdowns
      - pending_entity: what pending snapshot to show
      - overdraft_check: whether to check company balance
      - duplicate_guard: description
      - tips: UX hints for smooth handling
    """
    return {
        "receive_from_project": {
            "label": "Receive from Project (Owner Receipt)",
            "direction": "receive",
            "icon": "fa-building",
            "color": "emerald",
            "required": ["from_account (auto client)", "to_account (company)", "project", "amount", "date"],
            "optional": ["note", "reference_id"],
            "account_filter": {"from": "client (auto)", "to": "company/cash/bank"},
            "pending": "project_receivable",
            "overdraft": False,
            "tips": ["Client account auto-resolved from project", "Receiving account must be active company type", "Amount capped at pending receivable"],
        },
        "receive_from_credit_debit": {
            "label": "Receive from Party (Supplier/Client/Worker)",
            "direction": "receive",
            "icon": "fa-hand-holding-dollar",
            "color": "cyan",
            "required": ["from_account (party)", "to_account (company)", "amount", "date"],
            "optional": ["project", "stage", "note", "reference_id", "party_name"],
            "account_filter": {"from": "person/vendor/client", "to": "company/cash/bank"},
            "pending": "supplier_payable or project_receivable",
            "overdraft": False,
            "tips": ["Select party account that owes money", "Use for refunds, rental income, generic receipts"],
        },
        "receive_intra_company": {
            "label": "Transfer from Intra-Company (Internal)",
            "direction": "receive",
            "icon": "fa-right-left",
            "color": "blue",
            "required": ["from_account (company)", "to_account (company)", "amount", "date"],
            "optional": ["note", "reference_id"],
            "account_filter": {"from": "company/cash/bank", "to": "company/cash/bank"},
            "pending": None,
            "overdraft": True,
            "tips": ["Both accounts must be company type", "From and To must be different", "Overdraft blocked if from would go negative"],
        },
        "payroll": {
            "label": "Wage / Payroll Payment",
            "direction": "pay",
            "icon": "fa-helmet-safety",
            "color": "rose",
            "required": ["from_account (company)", "related_entity (worker)", "amount", "date"],
            "optional": ["to_account (auto worker)", "project", "stage", "excess_tip", "excess_advance", "settle_shortfall", "note"],
            "account_filter": {"from": "company/cash/bank", "to": "person (auto worker)"},
            "pending": "worker_payable",
            "overdraft": True,
            "tips": [
                "Worker's pending payable shown automatically",
                "Overpayment: payment_part=min(amount,pending), excess split into tip+advance",
                "Tip requires project+stage",
                "Shortfall settlement creates negative expense",
            ],
        },
        "purchase": {
            "label": "Material / Supplier Payment",
            "direction": "pay",
            "icon": "fa-truck-field",
            "color": "rose",
            "required": ["from_account (company)", "related_entity (supplier)", "amount", "date"],
            "optional": ["to_account (auto supplier)", "project", "stage", "excess_tip", "excess_advance", "note"],
            "account_filter": {"from": "company/cash/bank", "to": "vendor/person (auto supplier)"},
            "pending": "supplier_payable",
            "overdraft": True,
            "tips": ["Supplier pending = debit - credit from SupplierLedger", "Overpayment auto-creates advance"],
        },
        "expense_subcontractor": {
            "label": "Subcontractor Payment",
            "direction": "pay",
            "icon": "fa-people-group",
            "color": "rose",
            "required": ["from_account (company)", "related_entity (subcontractor)", "amount", "date"],
            "optional": ["to_account", "project", "stage", "excess_tip", "settle_shortfall", "note"],
            "account_filter": {"from": "company/cash/bank", "to": "person/vendor"},
            "pending": "subcontract_payable",
            "overdraft": True,
            "tips": ["Contract balance cap enforced", "Stage-specific pending if stage selected"],
        },
        "office_management_payment": {
            "label": "Office Management Payment",
            "direction": "pay",
            "icon": "fa-user-tie",
            "color": "slate",
            "required": ["from_account (company)", "amount", "date", "office_target (staff/expense)"],
            "optional": ["related_entity (office_staff if staff)", "to_account", "excess_tip", "office_expense_category", "note"],
            "account_filter": {"from": "company/cash/bank", "to": "person/external"},
            "pending": "office_staff_payable if staff",
            "overdraft": True,
            "tips": ["Staff target: needs office_staff related entity, mirrors to OfficeExpense", "Expense target: needs category, creates OfficeExpense row"],
        },
        "personal_management_payment": {
            "label": "Personal / Party Payment",
            "direction": "pay",
            "icon": "fa-user",
            "color": "slate",
            "required": ["from_account (company)", "to_account (person) or party_name", "amount", "date"],
            "optional": ["note", "reference_id"],
            "account_filter": {"from": "company/cash/bank", "to": "person"},
            "pending": None,
            "overdraft": True,
            "tips": ["Beneficiary name auto-resolved to person account", "Creates PersonalExpense row"],
        },
        "expense_general": {
            "label": "General Expense",
            "direction": "pay",
            "icon": "fa-receipt",
            "color": "slate",
            "required": ["from_account (company)", "expense_category_id", "amount", "date", "project", "stage"],
            "optional": ["to_account", "party_name", "note"],
            "account_filter": {"from": "company/cash/bank", "to": "external/person"},
            "pending": None,
            "overdraft": True,
            "tips": ["Project+stage mandatory for general expense", "Creates Expense row linked to category"],
        },
        "pay_intra_company": {
            "label": "Transfer to Intra-Company",
            "direction": "pay",
            "icon": "fa-right-left",
            "color": "blue",
            "required": ["from_account (company)", "to_account (company)", "amount", "date"],
            "optional": ["note", "reference_id", "executed_by_account"],
            "account_filter": {"from": "company/cash/bank", "to": "company/cash/bank"},
            "pending": None,
            "overdraft": True,
            "tips": ["Internal transfer between treasury accounts", "If executed_by different from from, creates split group_id"],
        },
        "pay_to_credit_debit": {
            "label": "Pay to Credit/Debit Party",
            "direction": "pay",
            "icon": "fa-arrow-up",
            "color": "slate",
            "required": ["from_account (company)", "to_account (party)", "amount", "date"],
            "optional": ["note", "reference_id"],
            "account_filter": {"from": "company/cash/bank", "to": "person/vendor/client"},
            "pending": None,
            "overdraft": True,
            "tips": ["Generic pay to party", "Use for refunds to client, etc."],
        },
    }
