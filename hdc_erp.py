"""Hadi Design and Construction Company - Construction ERP.

Backward-compatibility shim: the application now lives in the
`hdc` package (see MODULARIZATION_PLAN.md). This module preserves the
original import surface (`import hdc_erp`, `hdc_erp.app`, `hdc_erp.db`,
all models and helpers) so existing scripts, wsgi.py and tooling keep
working unchanged.
"""
import os

from hdc.app import create_app
from hdc.config import BASE_DIR, DB_PATH, DEV_MODE, INSTANCE_DIR, STAGE_DRAWINGS_DIR, _BACKUP_DIR, _DB_STORE, _ESTIMATION_STORE, _load_local_env, _resolve_path
from hdc.core.admin import _WIPE_TARGETS, _restore_from_backup_zip, _restore_from_paths, _run_admin_maintenance, _wipe_selected_targets
from hdc.core.bootstrap import _HDC_BOOTSTRAP_DONE, _HDC_BOOTSTRAP_LOCK, _bootstrap_hdc, _ensure_bootstrap_once, _migrate_legacy_done_markers_to_db
from hdc.core.flags import _runtime_flag_get, _runtime_flag_set
from hdc.core.schema import _ensure_accounts_schema, _ensure_owner_payment_void_schema, _ensure_purchase_v2_schema, _ensure_runtime_flags_table, _ensure_table_columns_sqlite, _ensure_timeentry_unique_indexes, _run_migrations
from hdc.extensions import _admin_only, _csrf_protect, _csrf_token, _ensure_db_runtime_ready, _inject_alert_count, _sqlite_fast_pragmas, db, load_user, login_manager
from hdc.models.accounts import Account, AccountTransaction, Alert, Expense, ExpenseCategory, OwnerPayment, PersonalExpense, PersonalExpenseCategory
from hdc.models.auth import ActivityLog, HDCUser, UserActivity
from hdc.models.materials import Delivery, Material, MaterialUsage, MaterialV2, Purchase, PurchaseV2, Supplier, SupplierLedger, UsageLogV2
from hdc.models.office import AllowanceCategory, OfficeExpense, OfficeExpenseCategory, OfficeStaff, OfficeStaffAttendance, OfficeStaffLedger, StaffAllowance
from hdc.models.projects import CustomFormula, Estimation, EstimationStage, Project, Stage, StageDefinition, StageDrawing, StageRateHistory
from hdc.models.subcontract import SubcontractAttendance, SubcontractEvent, SubcontractLabourAttendance, SubcontractLabourPayment, SubcontractLabourWorker, SubcontractPayment, Subcontractor
from hdc.models.workforce import Attendance, AttendanceDay, AttendanceMark, LabourLedger, LabourRateHistory, PayrollItem, PayrollRun, TimeEntry, Worker, WorkerRate, WorkerTrade
from hdc.models.cashflow import AccountReconciliation, CashDayAccountPosition, CashDayLock, CashFlowCategory, CashFlowEntry, CashFlowEntryAudit, CashFlowParty, CashFlowSubcategory
from hdc.services.accounts import _ACCOUNT_COMPANY_TYPES, _ACCOUNT_CREDIT_DEBIT_TYPES, _ACCOUNT_OUTGOING_SCOPE_REQUIRED_TYPES, _ACCOUNT_PROJECT_FLOW_TYPES, _ACCOUNT_TXN_CATEGORIES, _ACCOUNT_TXN_CATEGORY_DEFAULT_TYPE, _ACCOUNT_TXN_FORM_OPTIONS, _ACCOUNT_TXN_PAY_TYPES, _ACCOUNT_TXN_RECEIVE_TYPES, _ACCOUNT_TXN_TYPES, _ACCOUNT_TXN_TYPE_DEFAULT_CATEGORY, _ACCOUNT_TYPES, _account_balance, _account_balance_map, _account_balances_query, _account_dashboard_kpis, _account_dashboard_subgroups, _account_entity_label, _account_expected_related_type, _account_get_or_create, _account_group_mode_for_row, _account_intent_field_matrix, _account_kpi_detail_context, _account_ledger_rows, _account_pending_snapshot, _account_reference_links, _account_requires_scope_tags, _account_reverse_transaction_group, _account_rows_active, _account_running_balance_rows, _account_source_type_base, _account_transaction_history, _account_tx_direction_for_type, _account_txn_group_rows, _account_txn_source_exists, _account_txn_to_dict, _accounts_default_company_cash, _accounts_default_external_parties, _accounts_forensic_report, _accounts_party_account, _accounts_payable_breakdown_rows, _accounts_post_expense_row, _accounts_post_labour_ledger_row, _accounts_post_owner_receipt, _accounts_post_personal_expense_row, _accounts_post_subcontract_labour_payment_row, _accounts_post_subcontract_payment_row, _accounts_post_supplier_credit_row, _accounts_post_transaction, _accounts_reconciliation_findings, _accounts_reconciliation_snapshot, _accounts_set_void_by_source, _accounts_toggle_transaction_void_state, _accounts_update_manual_transaction, _accounts_upsert_labour_ledger_txn, _accounts_upsert_office_expense_txn, _accounts_upsert_office_staff_ledger_txn, _accounts_upsert_purchase_paid_txn, _backfill_accounts_scope_from_references, _backfill_owner_payment_receiving_accounts, _bootstrap_accounts_backfill_once, _build_account_txn_rows, _check_overdraft_block, _check_overdraft_block_replace, _compute_excess_split, _create_account, _create_account_transaction, _create_accounts_transaction_with_sync, _list_accounts_with_balances, _mark_auto_generated_person_accounts, _normalize_account_txn_payload, _resolve_account_type, _run_accounts_backfill, _set_void_state_row, _sync_account_transaction_source_update, _sync_source_row_void_state, _validate_account_transaction_payload
from hdc.services.aggregation import _aggregate_project_costs, _aggregate_stage_costs, _apply_aggregated_project_costs, _apply_aggregated_stage_costs, _running_projects_receivable_rows
from hdc.services.audit import _AUDIT_EXCLUDE_TABLES, _AUDIT_INTERNAL_TABLES, _AUDIT_NOISY_UPDATE_FIELDS, _audit_actor_snapshot, _audit_change_map, _audit_custom_summary_payload, _audit_entity_id, _audit_entity_type, _audit_humanize_attendance_mark, _audit_humanize_timeentry, _audit_is_noise_event, _audit_name_by_id, _audit_path, _audit_paused, _audit_repr, _audit_summary, _capture_user_activity_after_flush, _record_user_activity, log_action
from hdc.services.backups import _backup_filename, _cleanup_backup_temp_artifacts, _create_backup_xlsx, _create_backup_zip, _list_backups, _prune_saved_backups
from hdc.services.estimation import CONCRETE_GRADES, CONCRETE_RATIOS, DEFAULT_BAG_CFT, DEFAULT_DRY_FACTOR, DEFAULT_WC_RATIO, _create_project_from_estimation, _load_estimations, _normalize_estimation_rows, _save_estimations
from hdc.services.ledger import _get_all_office_expense_categories, _get_all_office_expense_category_rows, _is_linked_office_salary_expense, _is_linked_system_expense, _office_expense_total, _office_staff_ledger_snapshot, _personal_expense_month, _personal_expense_total, _remove_office_salary_expense_for_ledger, _sync_office_staff_expense_from_ledger, _worker_payable_snapshot, _worker_tip_expenses
from hdc.services.lookups import _category_name_from_id, _ensure_expense_category, _ensure_expense_category_by_id, _expense_category_options, _generate_project_code, _next_office_staff_code, _next_project_code, _next_worker_code, _trade_options
from hdc.services.purchase import _MATERIAL_V2_UNITS, _delivery_scope_qty_by_purchase, _ensure_material_v2, _ensure_supplier_quick, _material_stock_for_scope, _material_stock_map, _material_v2_available, _material_v2_delivered, _material_v2_scope_stock_rows, _material_v2_used, _material_v2_weighted_cost, _purchase_v2_available_in_project_qty, _purchase_v2_available_in_scope_qty, _purchase_v2_delivered_qty, _purchase_v2_delivered_to_scope_qty, _purchase_v2_integrity_report, _purchase_v2_scope_remaining_map, _purchase_v2_used_in_scope_qty, _repair_supplier_purchase_v2_ledger, _supplier_balance, _sync_purchase_v2_ledger, _sync_supplier_po_payment_status, _transfer_v2_material_between_scopes
from hdc.services.receipts import _account_receipt_recent_entries, _owner_payment_recent_entries, _receipt_company_profile
from hdc.services.reporting import _build_stage_event_ledger, _project_report_data, _refresh_alerts
from hdc.services.subcontract import _ensure_subcontract_baseline_events, _ensure_subcontract_labour_attendance_schema, _ensure_subcontract_payment_void_schema, _extract_pct, _log_subcontract_event, _next_subcontractor_code, _reconcile_subcontract_links, _subcontract_scope_stages, _subcontract_stage_snapshot
from hdc.services.timekeeping import _calc_time_wage, _has_recent_duplicate, _migrate_attendance_to_time_entries, _parse_attendance_entries_payload, _recalculate_attendance_day, _reconcile_all_time_entries_once, _reconcile_worker_time_entries, _reconcile_worker_tip_ledger, _repair_worker_work_ledger_links, _sync_work_ledger_for_time_entry, _timekeeping_status_dataset, _void_orphan_work_ledgers_for_time_entry, _worker_rate_on
from hdc.utils.dates import PKT_ZONE, _as_pkt, _fmt_pkt, _pkt_now, _pkt_now_naive, _pkt_today
from hdc.utils.format import _DATE_FALLBACK_DEFAULT, _OPS, _UNIT_TO_M, _UNIT_TO_MM, _activity_at_for, _amount_to_words, _flt, _is_pdf_upload, _is_strong_password, _num_to_words_en, _parse_date, _payload_int, _quote_ident, _safe_eval, _safe_sheet_name, _to_meters, _to_mm
from hdc.utils.normalize import _normalize_account_group, _normalize_account_mode, _normalize_account_tx_direction, _normalize_account_tx_type, _normalize_expense_category_name, _normalize_name_ci, _normalize_related_entity_type, _normalize_trade_name


app = create_app()

# View functions are defined inside each domain module's register(app).
# Re-expose them as attributes so `hdc_erp.<endpoint>` keeps working.
for _rule in app.url_map.iter_rules():
    if _rule.endpoint not in globals():
        globals()[_rule.endpoint] = app.view_functions[_rule.endpoint]
del _rule


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
