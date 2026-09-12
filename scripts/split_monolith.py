#!/usr/bin/env python3
"""Split the HDC monolith (hdc_erp.py) into the modular hdc/ package.

See MODULARIZATION_PLAN.md sections 4, 5 and 11. Every one of the 532
top-level statements of hdc_erp.py is assigned exactly one home module.
Function/class bodies move byte-identical except for the explicit,
logged transforms in REFACTOR_NOTES.md (current_app logger, bootstrap
app param, decorator strip for factory registration, 8 model local
imports).

Usage:
    python3 scripts/split_monolith.py [--check] [--write]

--check : classify + verify (acyclic, complete, no missing imports), no writes.
--write : (default) also write the hdc/ package files.
"""
import ast
import os
import sys

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_PATH = os.path.join(BASE_DIR, "hdc_erp.py")
PKG_DIR = os.path.join(BASE_DIR, "hdc")

# --------------------------------------------------------------------------
# 1. Classification tables
# --------------------------------------------------------------------------

MODEL_MAP = {
    "HDCUser": "auth", "UserActivity": "auth", "ActivityLog": "auth",
    "Project": "projects", "Stage": "projects", "StageDefinition": "projects",
    "StageRateHistory": "projects", "StageDrawing": "projects",
    "Estimation": "projects", "EstimationStage": "projects",
    "CustomFormula": "projects",
    "Worker": "workforce", "WorkerTrade": "workforce", "WorkerRate": "workforce",
    "LabourRateHistory": "workforce", "Attendance": "workforce",
    "TimeEntry": "workforce", "AttendanceDay": "workforce",
    "AttendanceMark": "workforce", "LabourLedger": "workforce",
    "PayrollRun": "workforce", "PayrollItem": "workforce",
    "OfficeStaff": "office", "OfficeStaffAttendance": "office",
    "OfficeStaffLedger": "office", "StaffAllowance": "office",
    "AllowanceCategory": "office", "OfficeExpense": "office",
    "OfficeExpenseCategory": "office",
    "Subcontractor": "subcontract", "SubcontractPayment": "subcontract",
    "SubcontractAttendance": "subcontract",
    "SubcontractLabourAttendance": "subcontract",
    "SubcontractLabourWorker": "subcontract",
    "SubcontractLabourPayment": "subcontract",
    "SubcontractEvent": "subcontract",
    "Material": "materials", "Purchase": "materials",
    "MaterialUsage": "materials", "Supplier": "materials",
    "MaterialV2": "materials", "PurchaseV2": "materials",
    "Delivery": "materials", "UsageLogV2": "materials",
    "SupplierLedger": "materials",
    "Account": "accounts", "AccountTransaction": "accounts",
    "OwnerPayment": "accounts", "Expense": "accounts",
    "ExpenseCategory": "accounts", "PersonalExpense": "accounts",
    "PersonalExpenseCategory": "accounts", "Alert": "accounts",
}

# Explicit route overrides (checked before prefix rules).
ROUTE_EXPLICIT = {
    "hdc_login": "auth", "hdc_logout": "auth", "hdc_root": "auth",
    "hdc_dashboard": "dashboard", "hdc_kpi_detail": "dashboard",
    "hdc_cost_entries": "dashboard",
    "hdc_project_report_csv": "reports", "hdc_project_report_xlsx": "reports",
    "hdc_project_report_pdf": "reports",
    "hdc_api_next_project_code": "estimation",
    "hdc_api_next_worker_code": "estimation",
    "hdc_api_next_office_staff_code": "estimation",
    "hdc_api_formula_vars": "estimation",
    "hdc_api_project_stages": "projects",
    "hdc_api_material_stock": "materials",
    "hdc_project_estimation": "estimation",
    "hdc_project_estimation_save": "estimation",
    "hdc_project_estimation_convert": "estimation",
    "hdc_users": "users", "hdc_event_recorder": "users",
    "hdc_add_subcontractor": "subcontractors",
    "hdc_trades": "workers",
    "hdc_expense_categories": "expenses",
    "hdc_expenses": "expenses", "hdc_expense_edit": "expenses",
    "hdc_expense_delete": "expenses",
    "hdc_alerts": "expenses", "hdc_alerts_resolve": "expenses",
    "hdc_add_stage": "projects", "hdc_edit_stage": "projects",
    "hdc_materials": "materials", "hdc_material_usage": "materials",
    "hdc_purchases": "materials",
}


def route_module(name):
    if name in ROUTE_EXPLICIT:
        return ROUTE_EXPLICIT[name]
    if name.startswith("api_v2_"):
        return "api_purchase"
    if name.startswith("api_accounts_"):
        return "api_accounts"
    if "subcontractor" in name or name.startswith("hdc_stage_shift"):
        return "subcontractors"
    if (name.startswith("hdc_worker") or name.startswith("hdc_toggle_worker")
            or name.startswith("hdc_edit_worker")):
        return "workers"
    if "payroll" in name:
        return "payroll"
    if "purchase_v2" in name:
        return "purchase_v2"
    if "office" in name or "allowance" in name:
        return "office"
    if "personal" in name or "ccount" in name:
        return "accounts"
    if "timekeeping" in name or "attendance" in name:
        return "timekeeping"
    if (name.startswith("hdc_project") or name.startswith("hdc_add_project")
            or name.startswith("hdc_edit_project") or "owner_payment" in name
            or name.startswith("hdc_stage") or name.startswith("hdc_bulk")
            or name.startswith("hdc_delete_stage")):
        return "projects"
    if "settings" in name or "maintenance" in name:
        return "settings"
    if "estimation" in name or "formula" in name:
        return "estimation"
    if "report" in name or "export" in name or "glance" in name:
        return "reports"
    return "??"

# Explicit helper overrides (checked before prefix rules).
HELPER_EXPLICIT = {
    # utils
    "_pkt_now": "utils.dates", "_pkt_now_naive": "utils.dates",
    "_pkt_today": "utils.dates", "_as_pkt": "utils.dates",
    "_fmt_pkt": "utils.dates",
    "_normalize_name_ci": "utils.normalize",
    "_normalize_account_tx_type": "utils.normalize",
    "_normalize_account_mode": "utils.normalize",
    "_normalize_account_group": "utils.normalize",
    "_normalize_account_tx_direction": "utils.normalize",
    "_normalize_related_entity_type": "utils.normalize",
    "_normalize_trade_name": "utils.normalize",
    "_normalize_expense_category_name": "utils.normalize",
    "_flt": "utils.format", "_parse_date": "utils.format",
    "_safe_eval": "utils.format", "_num_to_words_en": "utils.format",
    "_amount_to_words": "utils.format", "_activity_at_for": "utils.format",
    "_quote_ident": "utils.format", "_safe_sheet_name": "utils.format",
    "_payload_int": "utils.format", "_to_meters": "utils.format",
    "_to_mm": "utils.format", "_is_pdf_upload": "utils.format",
    "_is_strong_password": "utils.format",
    # extensions (Flask glue + hooks)
    "_sqlite_fast_pragmas": "extensions",
    "_csrf_token": "extensions", "_admin_only": "extensions",
    "_inject_alert_count": "extensions",
    "_ensure_db_runtime_ready": "extensions", "_csrf_protect": "extensions",
    "load_user": "extensions",
    # config
    "_load_local_env": "config", "_resolve_path": "config",
    # core
    "_runtime_flag_get": "core.flags", "_runtime_flag_set": "core.flags",
    "_ensure_table_columns_sqlite": "core.schema",
    "_run_migrations": "core.schema",
    "_ensure_purchase_v2_schema": "core.schema",
    "_ensure_owner_payment_void_schema": "core.schema",
    "_ensure_accounts_schema": "core.schema",
    "_ensure_runtime_flags_table": "core.schema",
    "_ensure_timeentry_unique_indexes": "core.schema",
    "_migrate_legacy_done_markers_to_db": "core.bootstrap",
    "_bootstrap_hdc": "core.bootstrap",
    "_ensure_bootstrap_once": "core.bootstrap",
    "_restore_from_paths": "core.admin",
    "_wipe_selected_targets": "core.admin",
    "_run_admin_maintenance": "core.admin",
    # services.receipts
    "_receipt_company_profile": "services.receipts",
    "_account_receipt_recent_entries": "services.receipts",
    "_owner_payment_recent_entries": "services.receipts",
    # services.lookups
    "_generate_project_code": "services.lookups",
    "_ensure_expense_category": "services.lookups",
    "_ensure_expense_category_by_id": "services.lookups",
    "_category_name_from_id": "services.lookups",
    "_trade_options": "services.lookups",
    "_expense_category_options": "services.lookups",
    # services.ledger extras
    "_get_all_office_expense_categories": "services.ledger",
    "_get_all_office_expense_category_rows": "services.ledger",
    # services.timekeeping extras
    "_reconcile_worker_tip_ledger": "services.timekeeping",
    "_ensure_subcontract_labour_attendance_schema": "services.subcontract",
    "_ensure_subcontract_payment_void_schema": "services.subcontract",
    # services.backups leftovers
    "_backup_filename": "services.backups",
    "_cleanup_backup_temp_artifacts": "services.backups",
    # services.accounts stragglers (don't match _account* prefix)
    "_normalize_account_txn_payload": "services.accounts",
    "_resolve_account_type": "services.accounts",
    "_list_accounts_with_balances": "services.accounts",
    "_create_account": "services.accounts",
    "_validate_account_transaction_payload": "services.accounts",
    "_check_overdraft_block": "services.accounts",
    "_check_overdraft_block_replace": "services.accounts",
    "_build_account_txn_rows": "services.accounts",
    "_create_account_transaction": "services.accounts",
    "_set_void_state_row": "services.accounts",
    "_sync_account_transaction_source_update": "services.accounts",
    "_compute_excess_split": "services.accounts",
    "_run_accounts_backfill": "services.accounts",
    "_bootstrap_accounts_backfill_once": "services.accounts",
    "_backfill_owner_payment_receiving_accounts": "services.accounts",
    "_backfill_accounts_scope_from_references": "services.accounts",
    "_mark_auto_generated_person_accounts": "services.accounts",
    "_create_accounts_transaction_with_sync": "services.accounts",
    "_sync_source_row_void_state": "services.accounts",
    # services.purchase stragglers
    "_sync_purchase_v2_ledger": "services.purchase",
    "_sync_supplier_po_payment_status": "services.purchase",
    "_supplier_balance": "services.purchase",
    "_delivery_scope_qty_by_purchase": "services.purchase",
    "_transfer_v2_material_between_scopes": "services.purchase",
    "_repair_supplier_purchase_v2_ledger": "services.purchase",
    "_material_stock_map": "services.purchase",
    "_material_stock_for_scope": "services.purchase",
    # services.timekeeping stragglers
    "_has_recent_duplicate": "services.timekeeping",
    "_migrate_attendance_to_time_entries": "services.timekeeping",
    # services.reporting stragglers
    "_running_projects_receivable_rows": "services.aggregation",
    "_project_report_data": "services.reporting",
    "_build_stage_event_ledger": "services.reporting",
    "_refresh_alerts": "services.reporting",
    # services.estimation
    "_load_estimations": "services.estimation",
    "_save_estimations": "services.estimation",
    "_create_project_from_estimation": "services.estimation",
    # services.audit
    "_audit_paused": "services.audit",
    "_ensure_subcontract_baseline_events": "services.subcontract",
    "_restore_from_backup_zip": "core.admin",
    "_prune_saved_backups": "services.backups",
    "_normalize_estimation_rows": "services.estimation",
    "_ensure_supplier_quick": "services.purchase",
    "_ensure_material_v2": "services.purchase",
}


def helper_module(name):
    if name in HELPER_EXPLICIT:
        return HELPER_EXPLICIT[name]
    if name.startswith(("_audit", "_record_user_activity",
                         "_capture_user_activity", "log_action")):
        return "services.audit"
    if name.startswith(("_account", "_accounts_")):
        return "services.accounts"
    if name.startswith(("_purchase_v2", "_material_v2")):
        return "services.purchase"
    if name.startswith(("_calc_time_wage", "_sync_work_ledger",
                         "_void_orphan", "_repair_worker",
                         "_reconcile_worker_time_entries", "_reconcile_all",
                         "_timekeeping_status", "_recalculate_attendance",
                         "_parse_attendance", "_worker_rate_on")):
        return "services.timekeeping"
    if name.startswith(("_aggregate", "_apply_aggregated")):
        return "services.aggregation"
    if name.startswith(("_subcontract", "_log_subcontract", "_extract_pct",
                         "_reconcile_subcontract")):
        return "services.subcontract"
    if name.startswith(("_next_subcontractor_code",)):
        return "services.subcontract"
    if name.startswith(("_worker_payable", "_worker_tip",
                         "_office_staff_ledger", "_sync_office",
                         "_remove_office", "_is_linked",
                         "_office_expense_total", "_personal_expense")):
        return "services.ledger"
    if name.startswith("_next_"):
        return "services.lookups"
    if name.startswith(("_create_backup", "_list_backups")):
        return "services.backups"
    return "??"

# Assigns (module-level globals) -> home module. Special-cased names
# (app creation etc.) are handled separately, not moved verbatim.
ASSIGN_MAP = {
    "BASE_DIR": "config", "INSTANCE_DIR": "config",
    "STAGE_DRAWINGS_DIR": "config", "DB_PATH": "config",
    "DEV_MODE": "config", "_ESTIMATION_STORE": "config",
    "_DB_STORE": "config", "_BACKUP_DIR": "config",
    "PKT_ZONE": "utils.dates",
    "_OPS": "utils.format", "_DATE_FALLBACK_DEFAULT": "utils.format",
    "_UNIT_TO_M": "utils.format", "_UNIT_TO_MM": "utils.format",
    "_AUDIT_EXCLUDE_TABLES": "services.audit",
    "_AUDIT_INTERNAL_TABLES": "services.audit",
    "_AUDIT_NOISY_UPDATE_FIELDS": "services.audit",
    "CONCRETE_GRADES": "services.estimation",
    "CONCRETE_RATIOS": "services.estimation",
    "DEFAULT_DRY_FACTOR": "services.estimation",
    "DEFAULT_BAG_CFT": "services.estimation",
    "DEFAULT_WC_RATIO": "services.estimation",
    "_MATERIAL_V2_UNITS": "services.purchase",
    "_HDC_BOOTSTRAP_DONE": "core.bootstrap",
    "_HDC_BOOTSTRAP_LOCK": "core.bootstrap",
    "_WIPE_TARGETS": "core.admin",
}

# Top-level assigns intentionally NOT moved (provided by hand-written code):
# db/login_manager are created unbound in the extensions prologue; `app`
# creation and app.config/app.jinja lines belong to the factory.
SKIP_ASSIGNS = {"db", "login_manager", "app"}

# ...but they still resolve to a home module for import computation.
SKIP_ASSIGN_MODULE = {"db": "hdc.extensions",
                      "login_manager": "hdc.extensions"}

ACCOUNT_ASSIGN_PREFIX = "_ACCOUNT_"

# Model properties needing function-local imports (class.method -> lines).
MODEL_LOCAL_IMPORTS = {
    ("Project", "total_received"):
        ["from hdc.models.accounts import OwnerPayment"],
    ("Project", "total_labour_cost"):
        ["from hdc.models.workforce import Attendance, TimeEntry"],
    ("Stage", "stage_labour_cost"):
        ["from hdc.models.workforce import Attendance, TimeEntry"],
    ("Stage", "stage_expense_cost"):
        ["from hdc.models.accounts import Expense"],
    ("Stage", "stage_tip_expense"):
        ["from hdc.models.accounts import Expense"],
    ("Stage", "stage_material_cost"):
        ["from hdc.models.materials import Purchase"],
    ("Stage", "stage_subcontract_cost"):
        ["from hdc.models.subcontract import Subcontractor"],
    ("OfficeStaff", "balance_due"):
        ["from hdc.services.ledger import _office_staff_ledger_snapshot"],
}

# Names satisfied by function-local imports (excluded from module imports).
LOCAL_IMPORT_SATISFIES = {
    "models.projects": {"Attendance", "TimeEntry", "OwnerPayment", "Expense",
                        "Purchase", "Subcontractor"},
    "models.office": {"_office_staff_ledger_snapshot"},
    "extensions": {"HDCUser", "_ensure_bootstrap_once", "Alert"},
}

# (target, func) -> local import lines inserted as first statements.
# Keeps extensions.py free of top-level models/bootstrap imports (no cycles).
HOOK_LOCAL_IMPORTS = {
    ("extensions", "load_user"):
        ["from hdc.models.auth import HDCUser"],
    ("extensions", "_ensure_db_runtime_ready"):
        ["from hdc.core.bootstrap import _ensure_bootstrap_once"],
    ("extensions", "_inject_alert_count"):
        ["from hdc.models.accounts import Alert"],
}

# Extra names forcibly added to a module's import set (from transforms).
EXTRA_IMPORTS = {
    "extensions": {"current_app"},
    "core.bootstrap": {"current_app"},
    "services.timekeeping": {"current_app"},
    "services.estimation": {"current_app"},
    "routes.projects": {"current_app"},
    "routes.settings": {"current_app"},
}

STDLIB_FROM = {"uuid", "contextlib", "datetime", "zoneinfo"}

BUILTINS = set(dir(__builtins__)) | {
    "True", "False", "None", "NotImplemented", "Ellipsis",
    "Exception", "BaseException",
}


# --------------------------------------------------------------------------
# 2. Parse + classify
# --------------------------------------------------------------------------

MONOLITH_MARKER = "Single-file Flask app"


def load_tree():
    with open(SRC_PATH, "r", encoding="utf-8") as f:
        src = f.read()
    if MONOLITH_MARKER not in src or len(src.splitlines()) < 15000:
        print("REFUSING to run: hdc_erp.py is not the original monolith "
              "(already split?). Restore it from git to re-run:")
        print("  git show fab3e8c:hdc_erp.py > hdc_erp.py")
        sys.exit(2)
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    return src, lines, tree


def is_route_func(node):
    if not isinstance(node, ast.FunctionDef):
        return False
    for d in node.decorator_list:
        if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and d.func.attr == "route"):
            return True
    return False


def classify(tree):
    """Return list of (node, kind, target) for every top-level node."""
    plan = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            plan.append((node, "class", "models." + MODEL_MAP[node.name]))
        elif isinstance(node, ast.FunctionDef):
            if is_route_func(node):
                plan.append((node, "route", "routes." + route_module(node.name)))
            else:
                plan.append((node, "def", helper_module(node.name)))
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets
                     if isinstance(t, ast.Name)]
            if not names:
                plan.append((node, "special", None))  # app.config[...] etc.
            elif len(names) == 1 and names[0] in ASSIGN_MAP:
                plan.append((node, "assign", ASSIGN_MAP[names[0]]))
            elif (len(names) == 1
                    and names[0].startswith(ACCOUNT_ASSIGN_PREFIX)):
                plan.append((node, "assign", "services.accounts"))
            elif names == ["app"] or (len(names) == 1
                                      and names[0] in SKIP_ASSIGNS):
                plan.append((node, "special", None))  # factory/prologue
            else:
                plan.append((node, "assign", "??"))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            plan.append((node, "import", None))
        elif isinstance(node, ast.Expr):
            plan.append((node, "expr", None))
        elif isinstance(node, ast.If):
            plan.append((node, "main", None))
        else:
            plan.append((node, "unknown", "??"))
    return plan


def check_complete(plan):
    errors = []
    for node, kind, target in plan:
        if target == "??":
            name = getattr(node, "name", ast.unparse(node)[:60])
            errors.append(f"unmapped {kind}: line {node.lineno} {name}")
    return errors


# --------------------------------------------------------------------------
# 3. Source slicing + transforms
# --------------------------------------------------------------------------

def slice_node(lines, tree, node):
    idx = tree.body.index(node)
    if idx == 0:
        start = 0
    else:
        start = tree.body[idx - 1].end_lineno
    seg = lines[start:node.end_lineno]
    # Drop leading blank lines (keep comments attached to this node).
    while seg and seg[0].strip() == "":
        seg = seg[1:]
    return seg


def strip_decorator_lines(seg, prefixes):
    out = []
    for ln in seg:
        s = ln.strip()
        if any(s.startswith(p) for p in prefixes):
            continue
        out.append(ln)
    return out


def apply_transforms(target, name, seg):
    seg = list(seg)
    # T1: app.logger -> current_app.logger
    text = "".join(seg)
    if "app.logger" in text:
        text = text.replace("app.logger", "current_app.logger")
        seg = text.splitlines(keepends=True)
    # T2: bootstrap app param
    if target == "core.bootstrap" and name == "_ensure_bootstrap_once":
        out = []
        for ln in seg:
            if ln.startswith("def _ensure_bootstrap_once(force=False):"):
                out.append("def _ensure_bootstrap_once(app=None, force=False):\n")
            elif ln.strip() == "with app.app_context():":
                indent = ln[:len(ln) - len(ln.lstrip())]
                out.append(f"{indent}_app_obj = (app if app is not None\n")
                out.append(f"{indent}           else current_app._get_current_object())\n")
                out.append(f"{indent}with _app_obj.app_context():\n")
            else:
                out.append(ln)
        seg = out
    # T3: strip factory-registered decorators
    if target == "extensions" and name in (
            "_inject_alert_count", "_ensure_db_runtime_ready", "_csrf_protect"):
        seg = strip_decorator_lines(seg, ("@app.",))
    if target == "extensions" and name == "load_user":
        seg = strip_decorator_lines(seg, ("@login_manager.",))
    if target == "services.audit" and name == "_capture_user_activity_after_flush":
        seg = strip_decorator_lines(seg, ("@event.listens_for",))
    # T6: function-local imports (keeps import graph acyclic)
    for imp in HOOK_LOCAL_IMPORTS.get((target, name), []):
        seg = insert_after_def(seg, 4, imp)
    # T4: model local imports (handled per property below)
    return seg


def insert_after_def(seg, indent_spaces, import_line):
    """Insert an import as first statement of the (single) def in seg."""
    out = []
    inserted = False
    pad = " " * indent_spaces
    i = 0
    while i < len(seg):
        out.append(seg[i])
        if not inserted and seg[i].lstrip().startswith("def ") \
                and seg[i].rstrip().endswith(":"):
            # skip over a docstring if present
            j = i + 1
            while j < len(seg) and seg[j].strip() == "":
                out.append(seg[j])
                j += 1
            if j < len(seg) and seg[j].lstrip().startswith(('"""', "'''")):
                quote = seg[j].lstrip()[:3]
                out.append(seg[j])
                j += 1
                if quote not in seg[j - 1].lstrip()[3:]:
                    while j < len(seg) and quote not in seg[j]:
                        out.append(seg[j])
                        j += 1
                    if j < len(seg):
                        out.append(seg[j])
                        j += 1
                i = j - 1
            out.append(f"{pad}{import_line}\n")
            inserted = True
        i += 1
    assert inserted, "insert_after_def failed"
    return out


def apply_model_local_imports(class_name, seg):
    key_hits = {m: imps for (c, m), imps in MODEL_LOCAL_IMPORTS.items()
                if c == class_name}
    if not key_hits:
        return seg
    lines = list(seg)
    # find each target method def line, insert imports as first statements
    out = []
    i = 0
    pending = None
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        if pending is None:
            for meth in key_hits:
                if ln.startswith(f"    def {meth}(") and ln.rstrip().endswith(":"):
                    pending = meth
                    break
            i += 1
            continue
        # inside target method: insert after def line (+docstring)
        if ln.strip() == "" or ln.lstrip().startswith(('"""', "'''", "#")):
            i += 1
            continue
        # first real statement reached -> insert before it
        for imp in key_hits[pending]:
            out.insert(len(out) - 1, f"        {imp}\n")
        pending = None
        i += 1
    assert pending is None, f"local import insert failed for {class_name}"
    return out


def indent_block(seg, spaces):
    pad = " " * spaces
    return [(pad + ln if ln.strip() else ln) for ln in seg]


# --------------------------------------------------------------------------
# 4. Import computation
# --------------------------------------------------------------------------

def build_alias_map(tree):
    """Map used-name -> import statement info from original imports."""
    alias = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                local = a.asname or a.name.split(".")[0]
                alias[local] = ("import", a.name, a.asname)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                local = a.asname or a.name
                alias[local] = ("from", node.module, a.name, a.asname)
    return alias


def free_names(node):
    """Names loaded in node that resolve to module level (need imports)."""
    free = set()
    bound = [set()]
    glob = set()

    def bind_args(a):
        for arg in (list(a.posonlyargs) + list(a.args)
                    + list(a.kwonlyargs)):
            bound[-1].add(arg.arg)
        if a.vararg:
            bound[-1].add(a.vararg.arg)
        if a.kwarg:
            bound[-1].add(a.kwarg.arg)

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, n):
            bound[-1].add(n.name)  # nested def binds in enclosing scope
            for d in n.decorator_list:
                self.visit(d)
            for d in (list(n.args.defaults)
                      + [d for d in n.args.kw_defaults if d is not None]):
                self.visit(d)
            bound.append(set())
            bind_args(n.args)
            for s in n.body:
                self.visit(s)
            bound.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, n):
            bound.append(set())
            bind_args(n.args)
            self.visit(n.body)
            bound.pop()

        def visit_ClassDef(self, n):
            bound[-1].add(n.name)
            for d in n.decorator_list:
                self.visit(d)
            for b in n.bases:
                self.visit(b)
            for k in n.keywords:
                self.visit(k)
            bound.append(set())
            for s in n.body:
                self.visit(s)
            bound.pop()

        def _comp(self, n, elt_fn):
            bound.append(set())
            for i, gen in enumerate(n.generators):
                self.visit(gen.iter)
                self.visit(gen.target)
                for cond in gen.ifs:
                    self.visit(cond)
            elt_fn()
            bound.pop()

        def visit_ListComp(self, n):
            self._comp(n, lambda: self.visit(n.elt))

        def visit_SetComp(self, n):
            self._comp(n, lambda: self.visit(n.elt))

        def visit_DictComp(self, n):
            self._comp(n, lambda: (self.visit(n.key),
                                   self.visit(n.value)))

        def visit_GeneratorExp(self, n):
            self._comp(n, lambda: self.visit(n.elt))

        def visit_Global(self, n):
            for nm in n.names:
                glob.add(nm)
                free.add(nm)

        def visit_Name(self, n):
            if isinstance(n.ctx, ast.Load):
                if n.id in glob or not any(n.id in s for s in bound):
                    free.add(n.id)
            else:
                if n.id in glob:
                    free.add(n.id)
                else:
                    bound[-1].add(n.id)

        def visit_Import(self, n):
            for a in n.names:
                bound[-1].add(a.asname or a.name.split(".")[0])

        def visit_ImportFrom(self, n):
            for a in n.names:
                if a.name != "*":
                    bound[-1].add(a.asname or a.name)

        def visit_ExceptHandler(self, n):
            if n.name:
                bound[-1].add(n.name)
            self.generic_visit(n)

    V().visit(node)
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        free.discard(node.name)
    return free


# Names injectable by transforms (not present in original imports).
FORCED_ALIAS = {
    "current_app": ("from", "flask", "current_app", None),
}


def compute_module_imports(modules_nodes, name_to_module, alias_map):
    """modules_nodes: target -> list of nodes. Returns target -> import block."""
    # names defined per module
    defined = {}
    for target, nodes in modules_nodes.items():
        defs = set(PROLOGUE_DEFINES.get(target, ()))
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                defs.add(n.name)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        defs.add(t.id)
        defined[target] = defs

    blocks = {}
    problems = []
    for target, nodes in sorted(modules_nodes.items()):
        used = set()
        for n in nodes:
            used |= free_names(n)
        used |= EXTRA_IMPORTS.get(target, set())
        used -= LOCAL_IMPORT_SATISFIES.get(target, set())
        used -= defined.get(target, set())
        used -= BUILTINS
        used -= {"__name__", "__file__"}
        if target.startswith("routes.") or target in ("extensions",
                                                     "core.bootstrap",
                                                     "services.estimation",
                                                     "services.timekeeping"):
            # routes: register(app) parameter; others: the T1-T3 transforms
            # remove every remaining global `app` use (verified post-write).
            used.discard("app")
        # partition
        plain_imports = {}   # local -> (mod, asname)
        from_imports = {}    # (from_mod, name, asname)
        hdc_imports = {}     # from_mod -> set(names)
        for nm in sorted(used):
            if nm in name_to_module and name_to_module[nm] != target:
                hdc_imports.setdefault(name_to_module[nm], set()).add(nm)
            elif nm in name_to_module and name_to_module[nm] == target:
                continue
            elif nm in FORCED_ALIAS:
                info = FORCED_ALIAS[nm]
                from_imports[(info[1], info[2], info[3])] = True
                continue
            elif nm in alias_map:
                info = alias_map[nm]
                if info[0] == "import":
                    plain_imports[nm] = (info[1], info[2])
                else:
                    from_imports[(info[1], info[2], info[3])] = True
            elif nm in ("db", "login_manager"):
                hdc_imports.setdefault("hdc.extensions", set()).add(nm)
            elif nm in ("app",):
                problems.append(f"{target}: unresolved global 'app'")
            else:
                # attribute roots like 'os' handled via alias; anything left
                # is either a missed move or a builtin-ish name.
                problems.append(f"{target}: unresolved name '{nm}'")
        # render
        std_plain, third_plain = [], []
        for local, (mod, asname) in sorted(plain_imports.items(),
                                           key=lambda kv: kv[1][0]):
            stmt = f"import {mod}" + (f" as {asname}" if asname else "")
            (std_plain if "." not in mod and mod in
             ("os", "csv", "io", "ast", "json", "calendar", "shutil",
              "zipfile", "tempfile", "secrets", "threading", "sqlite3",
              "operator") else third_plain).append(stmt)
        std_from, third_from = {}, {}
        for (mod, name, asname) in sorted(from_imports):
            item = name + (f" as {asname}" if asname else "")
            bucket = std_from if mod.split(".")[0] in STDLIB_FROM | {
                "os", "sys"} else third_from
            bucket.setdefault(mod, []).append(item)
        lines = []
        for stmt in std_plain:
            lines.append(stmt)
        for mod, items in sorted(std_from.items()):
            lines.append(f"from {mod} import {', '.join(sorted(items))}")
        if lines:
            lines.append("")
        for stmt in third_plain:
            lines.append(stmt)
        if third_plain and third_from:
            pass
        for mod, items in sorted(third_from.items()):
            lines.append(f"from {mod} import {', '.join(sorted(items))}")
        if third_plain or third_from:
            lines.append("")
        for mod in sorted(hdc_imports):
            names = sorted(hdc_imports[mod])
            lines.append(f"from {mod} import {', '.join(names)}")
        while lines and lines[-1] == "":
            lines.pop()
        blocks[target] = "\n".join(lines)
    return blocks, problems


# --------------------------------------------------------------------------
# 5. Static prologues / epilogues / hand-written files
# --------------------------------------------------------------------------

PROLOGUES = {
    "extensions": '''\
db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "hdc_login"
login_manager.login_message_category = "warning"

''',
}

# Names defined by prologues (count as module-local for import purposes).
PROLOGUE_DEFINES = {
    "extensions": {"db", "login_manager"},
}

# Extra imports required by prologues themselves.
PROLOGUE_IMPORTS = {
    "extensions": ["from flask_login import LoginManager",
                   "from flask_sqlalchemy import SQLAlchemy"],
}

EPILOGUES = {
    "services.audit": '''\n
_AUDIT_EVENTS_REGISTERED = False


def register_audit_events():
    """Attach the audit after_flush listener (called by the app factory)."""
    global _AUDIT_EVENTS_REGISTERED
    if _AUDIT_EVENTS_REGISTERED:
        return
    event.listen(db.session.__class__, "after_flush",
                 _capture_user_activity_after_flush)
    _AUDIT_EVENTS_REGISTERED = True

''',
    "core.bootstrap": '''\n
def reset_bootstrap_for_tests():
    """Allow a fresh bootstrap (test-only: fresh app + scratch DB)."""
    global _HDC_BOOTSTRAP_DONE
    _HDC_BOOTSTRAP_DONE = False

''',
    "config": '''\n
def ensure_dirs():
    """Create runtime directories (called by the app factory, not at import)."""
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    os.makedirs(STAGE_DRAWINGS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(_BACKUP_DIR, exist_ok=True)


def get_flask_config():
    """Flask config dict derived from the environment (12-factor style)."""
    secret = (os.environ.get("HDC_SECRET_KEY") or "").strip()
    if not secret:
        secret = "hdc_local_username_password_session_key"
        print("[HDC ERP] WARNING: HDC_SECRET_KEY is not set; using the "
              "built-in dev key. Set HDC_SECRET_KEY in production.")
    return {
        "SECRET_KEY": secret,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{DB_PATH}",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
    }

''',
}


def render_shim(name_to_module):
    by_mod = {}
    for nm in sorted(name_to_module):
        mod = name_to_module[nm]
        if mod.startswith("hdc.routes."):
            continue  # view funcs live inside register(app); exposed below
        by_mod.setdefault(mod, []).append(nm)
    lines = [
        '"""Hadi Design and Construction Company - Construction ERP.',
        "",
        "Backward-compatibility shim: the application now lives in the",
        "`hdc` package (see MODULARIZATION_PLAN.md). This module preserves the",
        "original import surface (`import hdc_erp`, `hdc_erp.app`, `hdc_erp.db`,",
        "all models and helpers) so existing scripts, wsgi.py and tooling keep",
        "working unchanged.",
        '"""',
        "import os",
        "",
        "from hdc.app import create_app",
    ]
    for mod in sorted(by_mod):
        names = [n for n in by_mod[mod]]
        lines.append(f"from {mod} import {', '.join(names)}")
    lines.extend([
        "",
        "",
        "app = create_app()",
        "",
        "# View functions are defined inside each domain module's register(app).",
        "# Re-expose them as attributes so `hdc_erp.<endpoint>` keeps working.",
        "for _rule in app.url_map.iter_rules():",
        "    if _rule.endpoint not in globals():",
        "        globals()[_rule.endpoint] = app.view_functions[_rule.endpoint]",
        "del _rule",
        "",
        "",
        'if __name__ == "__main__":',
        '    port = int(os.environ.get("PORT", 5000))',
        '    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)',
        "",
    ])
    return "\n".join(lines)


ROUTE_DOCS = {
    "auth": "Login, logout and the root redirect.",
    "dashboard": "Dashboard, KPI drill-down and cost-entries explorer.",
    "projects": "Projects, stages, drawings, stage library, owner payments.",
    "subcontractors": "Subcontractors, labour teams, ledgers and stage shifts.",
    "workers": "Workers, trades, ledgers, advances, payments and rates.",
    "timekeeping": "Attendance / timekeeping day sheet and corrections.",
    "payroll": "Payroll runs, salary cards and history.",
    "expenses": "Site expenses and alerts.",
    "office": "Office management: staff, attendance, ledger, expenses, allowances.",
    "materials": "Legacy (v1) materials, usage and purchases.",
    "purchase_v2": "Purchase V2 pages: materials, orders, suppliers, stock.",
    "estimation": "Estimation engine, project estimation and code APIs.",
    "reports": "Reports and CSV/XLSX/print exports.",
    "users": "User management and event recorder.",
    "accounts": "Unified Accounts pages and personal management.",
    "settings": "Settings, backups, maintenance and misc APIs.",
    "api_purchase": "JSON API: /api/v2/purchase/*.",
    "api_accounts": "JSON API: /api/accounts/*.",
}

INIT_FILES = {
    "hdc/__init__.py": '''"""Hadi Design & Construction ERP — modular package.

Created by splitting the legacy single-file app (see MODULARIZATION_PLAN.md).
Use :func:`hdc.app.create_app` to build the Flask application.
"""

__version__ = "2.0.0-modular"
''',
    "hdc/utils/__init__.py": '"""Pure helpers: dates, formatting, normalizers."""\n',
    "hdc/models/__init__.py": None,  # generated re-exports
    "hdc/services/__init__.py": '"""Business engines (accounts, purchase, payroll, ...)."""\n',
    "hdc/core/__init__.py": '"""Runtime platform: flags, schema, bootstrap, admin ops."""\n',
    "hdc/routes/__init__.py": None,  # generated register_all
}


def render_models_init():
    lines = ['"""All ORM models, re-exported from their domain modules."""', ""]
    by_mod = {}
    for cls, mod in sorted(MODEL_MAP.items(), key=lambda kv: (kv[1], kv[0])):
        by_mod.setdefault(mod, []).append(cls)
    for mod in sorted(by_mod):
        lines.append(f"from hdc.models.{mod} import {', '.join(by_mod[mod])}")
    lines.append("")
    lines.append("__all__ = [")
    for cls in sorted(MODEL_MAP):
        lines.append(f'    "{cls}",')
    lines.append("]")
    lines.append("")
    return "\n".join(lines)


def render_routes_init(route_modules):
    lines = ['"""Route registration: every domain module exposes register(app)."""',
             ""]
    for mod in sorted(route_modules):
        lines.append(f"from hdc.routes.{mod} import register as _register_{mod}")
    lines.append("")
    lines.append("")
    lines.append("def register_all(app):")
    lines.append('    """Attach all domain routes to the Flask app."""')
    for mod in sorted(route_modules):
        lines.append(f"    _register_{mod}(app)")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 6. Driver
# --------------------------------------------------------------------------

def build_plan():
    src, lines, tree = load_tree()
    plan = classify(tree)
    errors = check_complete(plan)
    if errors:
        print("CLASSIFICATION ERRORS:")
        for e in errors:
            print("  " + e)
        sys.exit(1)
    modules_nodes = {}
    for node, kind, target in plan:
        if kind in ("def", "class", "route", "assign"):
            modules_nodes.setdefault(target, []).append(node)
    # order nodes within each module by original line number
    for target in modules_nodes:
        modules_nodes[target].sort(key=lambda n: n.lineno)
    alias_map = build_alias_map(tree)
    name_to_module = build_name_map(plan)
    blocks, problems = compute_module_imports(modules_nodes, name_to_module,
                                              alias_map)
    return src, lines, tree, plan, modules_nodes, blocks, problems


def build_name_map(plan):
    m = {}
    for cls, mod in MODEL_MAP.items():
        m[cls] = f"hdc.models.{mod}"
    for nm, mod in ASSIGN_MAP.items():
        m[nm] = f"hdc.{mod}"
    m.update(SKIP_ASSIGN_MODULE)
    for node, kind, target in plan:
        if kind in ("def", "route", "class") and hasattr(node, "name"):
            m[node.name] = "hdc." + target
        elif kind == "assign":
            for t in node.targets:
                if isinstance(t, ast.Name):
                    m[t.id] = "hdc." + target
    return m


def main():
    write = "--check" not in sys.argv
    src, lines, tree, plan, modules_nodes, blocks, problems = build_plan()

    # module-level cycle check
    name_to_module = build_name_map(plan)
    # rebuild function->module for edge analysis
    func_mod = {}
    for node, kind, target in plan:
        if kind in ("def", "route") and isinstance(
                node, ast.FunctionDef):
            short = target if "." not in target else target
            func_mod[node.name] = ("hdc." + target) if not target.startswith(
                "hdc.") else target
    for cls, mod in MODEL_MAP.items():
        func_mod[cls] = f"hdc.models.{mod}"
    for nm, mod in ASSIGN_MAP.items():
        func_mod[nm] = f"hdc.{mod}"
    for star in ("_ACCOUNT_TXN_TYPES", "_ACCOUNT_TYPES"):
        func_mod[star] = "hdc.services.accounts"
    hnames = {n.name for n in tree.body
              if isinstance(n, ast.FunctionDef)}
    edges = {}
    for node, kind, target in plan:
        if kind not in ("def", "route") or not isinstance(
                node, ast.FunctionDef):
            continue
        src_mod = "hdc." + target
        # skip local-import-satisfied callees
        skip = set()
        if target == "extensions" and node.name == "_ensure_db_runtime_ready":
            skip.add("_ensure_bootstrap_once")
        for nd in ast.walk(node):
            if (isinstance(nd, ast.Call) and isinstance(nd.func, ast.Name)
                    and nd.func.id in hnames
                    and nd.func.id != node.name
                    and nd.func.id not in skip):
                dst = func_mod.get(nd.func.id)
                if dst and dst != src_mod and not dst.startswith(
                        "hdc.routes"):
                    edges.setdefault(src_mod, set()).add(dst)
    # find SCCs
    nodes = set(edges) | {d for ds in edges.values() for d in ds}
    idx, low, ctr, stack, on, sccs = {}, {}, [0], [], set(), []

    def sc(v):
        idx[v] = low[v] = ctr[0]
        ctr[0] += 1
        stack.append(v)
        on.add(v)
        for w in sorted(edges.get(v, ())):
            if w not in idx:
                sc(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    sys.setrecursionlimit(10000)
    for v in sorted(nodes):
        if v not in idx:
            sc(v)
    cycles = [c for c in sccs if len(c) > 1]
    if cycles:
        print("MODULE CYCLES DETECTED:")
        for c in cycles:
            print("  " + " <-> ".join(sorted(c)))
        sys.exit(1)

    if problems:
        print("IMPORT PROBLEMS:")
        for p in sorted(set(problems)):
            print("  " + p)
        sys.exit(1)

    n_funcs = sum(1 for _, k, _ in plan if k in ("def", "route"))
    n_class = sum(1 for _, k, _ in plan if k == "class")
    n_assign = sum(1 for _, k, _ in plan if k == "assign")
    print(f"modules: {len(modules_nodes)}  funcs: {n_funcs}  "
          f"classes: {n_class}  assigns: {n_assign}")
    print("cycles: none  unresolved imports: none")
    for target in sorted(modules_nodes):
        print(f"  {target:22s} {len(modules_nodes[target]):4d} nodes")

    if not write:
        return
    # ---- write files ----
    os.makedirs(PKG_DIR, exist_ok=True)
    for sub in ("utils", "models", "services", "core", "routes"):
        os.makedirs(os.path.join(PKG_DIR, sub), exist_ok=True)
    for rel, content in INIT_FILES.items():
        if content is None:
            continue
        with open(os.path.join(BASE_DIR, rel), "w", encoding="utf-8") as f:
            f.write(content)
    with open(os.path.join(PKG_DIR, "models", "__init__.py"), "w",
              encoding="utf-8") as f:
        f.write(render_models_init())
    route_mods = sorted({t.split(".", 1)[1] for t in modules_nodes
                         if t.startswith("routes.")})
    with open(os.path.join(PKG_DIR, "routes", "__init__.py"), "w",
              encoding="utf-8") as f:
        f.write(render_routes_init(route_mods))

    for target, nodes in sorted(modules_nodes.items()):
        is_route = target.startswith("routes.")
        if is_route:
            doc = ROUTE_DOCS[target.split(".", 1)[1]]
            header = (f'"""HDC routes: {doc}\n\nMoved verbatim from hdc_erp.py; each handler keeps its\n'
                      f'original @app.route decorator and endpoint name.\n"""\n\n')
        else:
            header = (f'"""HDC {target} — moved verbatim from hdc_erp.py.\n\nSee MODULARIZATION_PLAN.md for the module map.\n"""\n\n')
        parts = [header]
        import_block = blocks.get(target, "")
        for stmt in PROLOGUE_IMPORTS.get(target, []):
            if stmt not in import_block:
                import_block = (import_block + "\n" + stmt
                                if import_block else stmt)
        if import_block:
            parts.append(import_block + "\n\n")
        if target in PROLOGUES:
            parts.append(PROLOGUES[target])
        if is_route:
            parts.append("def register(app):\n")
            parts.append(f'    """Register {doc}"""\n')
        for node in nodes:
            seg = slice_node(lines, tree, node)
            nm = getattr(node, "name", None)
            if isinstance(node, ast.ClassDef):
                seg = apply_model_local_imports(node.name, seg)
            elif isinstance(node, ast.FunctionDef):
                seg = apply_transforms(target, node.name, seg)
            elif isinstance(node, ast.Assign) and target == "config":
                targets = [t.id for t in node.targets
                           if isinstance(t, ast.Name)]
                if "BASE_DIR" in targets:
                    # T7: config.py lives one directory deeper than hdc_erp.py
                    seg = ["".join(seg).replace(
                        "os.path.abspath(os.path.dirname(__file__))",
                        "os.path.abspath(os.path.join("
                        "\n    os.path.dirname(__file__), '..'))")]
            if is_route:
                seg = indent_block(seg, 4)
            parts.append("".join(seg))
            parts.append("\n\n")
        if target in EPILOGUES:
            parts.append(EPILOGUES[target])
        rel = "hdc/" + target.replace(".", "/") + ".py"
        with open(os.path.join(BASE_DIR, rel), "w", encoding="utf-8") as f:
            f.write("".join(parts).rstrip() + "\n")
        print(f"wrote {rel}")
    with open(SRC_PATH, "w", encoding="utf-8") as f:
        f.write(render_shim(build_name_map(plan)))
    print("wrote hdc_erp.py (compat shim)")
    print("done.")


if __name__ == "__main__":
    main()
