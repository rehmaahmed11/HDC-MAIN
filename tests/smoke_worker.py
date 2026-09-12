#!/usr/bin/env python3
"""Boot one app copy (baseline or refactored), exercise it, dump JSON results.

Usage: smoke_worker.py <app_dir> <db_path> <instance_dir> <out_json>
"""
import json
import os
import re
import sys
import traceback

app_dir, db_path, inst_dir, out_path = sys.argv[1:5]
sys.path.insert(0, app_dir)
os.environ["HDC_DB_PATH"] = db_path
os.environ["HDC_INSTANCE_DIR"] = inst_dir
os.chdir(app_dir)

import hdc_erp  # noqa: E402

app = hdc_erp.app
client = app.test_client()
res = {"reads": {}, "writes": {}, "counts": {}, "errors": []}


def step(bucket, key, fn):
    try:
        res[bucket][key] = fn()
    except Exception as e:  # noqa: BLE001 - differential harness records all
        res[bucket][key] = f"ERROR: {e!r}"
        res["errors"].append(f"{bucket}:{key}: {traceback.format_exc()[-500:]}")


def get(path):
    return client.get(path, follow_redirects=False).status_code


# ---- login ----
r = client.get("/hdc/login")
html = r.get_data(as_text=True)
m = re.search(r'name="_csrf_token" value="([^"]+)"', html)
token = m.group(1) if m else ""
res["writes"]["login_page"] = r.status_code
step("writes", "login", lambda: client.post(
    "/hdc/login",
    data={"username": "admin", "password": "Admin@1234",
          "_csrf_token": token},
    follow_redirects=False).status_code)


def post_form(path, data):
    d = dict(data)
    d["_csrf_token"] = token
    return client.post(path, data=d, follow_redirects=False).status_code


READ_PAGES = [
    "/", "/hdc/", "/hdc/kpi/active_projects", "/hdc/kpi/net_profit",
    "/hdc/kpi/today_expenses", "/hdc/kpi/workers_present",
    "/hdc/kpi/delayed_stages", "/hdc/kpi/budget_overruns",
    "/hdc/kpi/total_expenses",
    "/hdc/projects", "/hdc/projects/add", "/hdc/stages",
    "/hdc/stage-library", "/hdc/subcontractors",
    "/hdc/workers", "/hdc/trades", "/hdc/expense_categories",
    "/hdc/attendance", "/hdc/timekeeping", "/hdc/timekeeping/status",
    "/hdc/payroll", "/hdc/payroll/generate", "/hdc/payroll/salary-cards",
    "/hdc/payroll/history", "/hdc/alerts", "/hdc/expenses",
    "/hdc/office-management", "/hdc/office-management/staff",
    "/hdc/office-management/staff/ledger",
    "/hdc/office-management/staff/attendance",
    "/hdc/office-management/expenses",
    "/hdc/office-management/allowance-categories",
    "/hdc/materials", "/hdc/materials/usage", "/hdc/purchases",
    "/hdc/purchase-v2", "/hdc/purchase-v2/materials",
    "/hdc/purchase-v2/purchases", "/hdc/purchase-v2/suppliers",
    "/hdc/purchase-v2/delivered", "/hdc/purchase-v2/usage",
    "/hdc/purchase-v2/stock", "/hdc/estimation",
    "/hdc/project-estimation", "/hdc/estimation/formulas",
    "/hdc/reports", "/hdc/reports/glance",
    "/hdc/reports/export/profitability", "/hdc/reports/export/salary",
    "/hdc/reports/export/materials",
    "/hdc/users", "/hdc/event-recorder", "/hdc/accounts",
    "/hdc/accounts/entries", "/hdc/accounts/reconciliation",
    "/hdc/accounts/kpi/cash", "/hdc/personal-management",
    "/hdc/personal-management/expenses",
    "/hdc/personal-management/categories", "/hdc/settings",
    "/api/v2/purchase", "/api/v2/purchase/kpis",
    "/api/v2/purchase/material-stock", "/api/v2/purchase/supplier-ledger",
    "/api/accounts/dashboard_summary", "/api/accounts/dashboard_subgroups",
    "/api/accounts/pending_context",
    "/api/accounts/reconciliation_summary",
    "/api/accounts/forensic_report",
    "/api/accounts/list_accounts_with_balances",
    "/api/accounts/transaction_history",
    "/hdc/api/next_project_code", "/hdc/api/next_worker_code",
    "/hdc/api/next_office_staff_code",
    "/hdc/api/office_expense_categories",
    "/hdc/api/material_stock/1",
]

for page in READ_PAGES:
    step("reads", f"GET {page}", lambda p=page: get(p))

# ---- writes (fixed deterministic payloads) ----
# Fund a company cash account first (expenses post into unified accounts
# and the overdraft block rejects unfunded cash accounts).
step("writes", "POST account(json)", lambda: client.post(
    "/api/accounts/create_account",
    json={"name": "Smoke Cash", "account_group": "company",
          "account_mode": "cash"}).status_code)
with app.app_context():
    acc = hdc_erp.Account.query.filter_by(name="Smoke Cash").first()
    accid = acc.id if acc else 0
    cc = hdc_erp.Account.query.filter_by(name="Company Cash").first()
    ccid = cc.id if cc else 0
res["writes"]["account_id"] = accid
# Fund the auto-created Company Cash (expenses post from it).
step("writes", "PUT fund Company Cash", lambda: client.put(
    f"/api/accounts/account/{ccid}",
    json={"opening_balance": 100000}).status_code)

step("writes", "POST project", lambda: post_form("/hdc/projects/add", {
    "name": "Smoke Project", "client": "Smoke Client",
    "client_phone": "0300-0000000", "location": "Lahore",
    "total_sqft": "1000", "owner_rate": "5000",
    "start_date": "2026-09-01", "status": "active"}))

with app.app_context():
    p = hdc_erp.Project.query.filter_by(name="Smoke Project").first()
    pid = p.id if p else 0
    cat = hdc_erp.ExpenseCategory.query.filter_by(
        active_status=True).first()
    cat_id = cat.id if cat else 0
res["writes"]["project_id"] = pid

step("writes", "POST stage", lambda: post_form(
    f"/hdc/projects/{pid}/stage/add", {
        "name": "Grey Structure", "contract_basis": "Per Sq Ft",
        "rate_per_sqft": "100", "qty_sqft": "1000",
        "estimated_cost": "50000", "status": "Active"}))
with app.app_context():
    s = hdc_erp.Stage.query.filter_by(project_id=pid).first()
    sid = s.id if s else 0
res["writes"]["stage_id"] = sid

step("writes", "POST worker", lambda: post_form("/hdc/workers", {
    "worker_code": "W-001", "name": "Smoke Worker",
    "role_type": "Mason", "wage_type": "daily",
    "daily_wage": "1000"}))
with app.app_context():
    w = hdc_erp.Worker.query.filter_by(worker_code="W-001").first()
    wid = w.id if w else 0
res["writes"]["worker_id"] = wid

step("writes", "POST expense", lambda: post_form("/hdc/expenses", {
    "project_id": str(pid), "stage_id": str(sid),
    "category_id": str(cat_id), "amount": "500",
    "date": "2026-09-01", "remarks": "smoke"}))
step("writes", "POST supplier", lambda: post_form(
    "/hdc/purchase-v2/suppliers", {"name": "Smoke Supplier",
                                   "phone": "0300-1111111"}))
with app.app_context():
    sup = hdc_erp.Supplier.query.filter_by(name="Smoke Supplier").first()
    supid = sup.id if sup else 0
res["writes"]["supplier_id"] = supid

step("writes", "POST material", lambda: post_form(
    "/hdc/purchase-v2/materials", {"name": "Cement", "unit": "KG"}))
step("writes", "POST timekeeping", lambda: post_form("/hdc/timekeeping", {
    "bulk_mode": "1", "date": "2026-09-01",
    f"status_{wid}": "present", f"project_id_{wid}": str(pid),
    f"stage_id_{wid}": str(sid), f"working_hours_{wid}": "8",
    f"overtime_hours_{wid}": "0"}))

# ---- detail reads on created rows ----
DETAIL = [
    f"/hdc/projects/{pid}", f"/hdc/projects/{pid}/edit",
    f"/hdc/stage/{sid}/ledger", f"/hdc/stage/{sid}/edit",
    f"/hdc/workers/{wid}/ledger", f"/hdc/workers/{wid}/advance",
    f"/hdc/workers/{wid}/payment", f"/hdc/workers/{wid}/rate",
    f"/hdc/purchase-v2/suppliers/{supid}",
    f"/hdc/accounts/{accid}/ledger",
    f"/hdc/reports/project/{pid}/csv",
]
for page in DETAIL:
    step("reads", f"GET {page}", lambda p=page: get(p))

# ---- table counts ----
TABLES = ["Project", "Stage", "Worker", "Expense", "Supplier",
          "MaterialV2", "Account", "AccountTransaction", "LabourLedger",
          "TimeEntry", "UserActivity", "ActivityLog", "OwnerPayment",
          "Subcontractor", "PurchaseV2", "Delivery", "UsageLogV2"]
with app.app_context():
    for t in TABLES:
        try:
            res["counts"][t] = getattr(hdc_erp, t).query.count()
        except Exception as e:  # noqa: BLE001
            res["counts"][t] = f"ERROR: {e!r}"

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(res, f, indent=1, sort_keys=True)
print(f"wrote {out_path}: {len(res['reads'])} reads, "
      f"{len(res['writes'])} writes, {len(res['counts'])} counts, "
      f"{len(res['errors'])} errors")
