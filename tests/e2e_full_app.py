#!/usr/bin/env python3
"""Full-application end-to-end business test (HTTP-driven, isolated scratch DB).

Walks the whole construction business through the real web routes:
accounts -> projects/stages -> client receipts -> workers/rates/timekeeping ->
advances, wages, tips, overpay-as-advance, ledger void/restore -> payroll ->
subcontractors (contract, team attendance, labour workers, attendance,
payments, progress) -> suppliers/materials/purchases/delivery/transfer/usage/
supplier payment -> expenses (add/edit/void) -> office staff/salary/expenses ->
tool rental (inventory, rent, pay, return) -> Money Center / Transactions /
CF Register (in, out, transfer, void/restore) -> day close -> reconciliation ->
reports/exports -> crawl of every GET page for server errors.

Every step is a *soft* check: the run continues and prints a full report.
Each POST also inspects flashed 'danger'/'warning' messages, because this app
answers most rejected forms with a 302 + flash rather than a 4xx.

Usage:  .venv/bin/python tests/e2e_full_app.py      (exit 1 if anything failed)
"""
import csv
import io
import os
import re
import sys
import tempfile
import traceback
from datetime import timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix='hdc-e2e-')
os.environ.update({'HDC_ENV': 'test', 'HDC_SECRET_KEY': 'e2e-only',
                   'HDC_BOOTSTRAP_ADMIN_PASSWORD': 'E2e-Test-Only-123',
                   'HDC_DB_PATH': os.path.join(TMP, 'e2e.db'),
                   'HDC_INSTANCE_DIR': TMP})

from hdc.app import create_app  # noqa: E402
from hdc.extensions import db  # noqa: E402
from hdc.models import *  # noqa: E402,F401,F403
from hdc.models import (Account, AccountTransaction, Expense, ExpenseCategory,  # noqa: E402
                        OwnerPayment, Project, Stage, Worker, TimeEntry, LabourLedger,
                        PayrollItem, Subcontractor, SubcontractPayment,
                        SubcontractTeamAttendance, SubcontractLabourWorker,
                        SubcontractLabourAttendance, Supplier, MaterialV2, PurchaseV2,
                        Delivery, UsageLogV2, OfficeStaff, OfficeStaffLedger,
                        OfficeExpense, Tool, ToolRental, ToolRentalPayment,
                        CashFlowEntry, CashDayLock)
from hdc.services.accounts import _account_balance, _accounts_reconciliation_findings  # noqa: E402
from hdc.services.ledger import _worker_payable_snapshot  # noqa: E402
from hdc.services.purchase import _supplier_balance  # noqa: E402
from hdc.services.aggregation import _aggregate_project_costs  # noqa: E402
from hdc.utils.dates import _pkt_today  # noqa: E402

app = create_app({'TESTING': True})
client = app.test_client()
RESULTS = []          # (section, name, ok, detail)
SECTION = ['setup']


def section(name):
    SECTION[0] = name
    print(f'\n== {name}')


def check(name, cond, detail=''):
    RESULTS.append((SECTION[0], name, bool(cond), '' if cond else str(detail)[:400]))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ('' if cond else f'  -> {str(detail)[:300]}'))
    return bool(cond)


def q(fn):
    """Run fn inside an app context and return its value (None on error)."""
    with app.app_context():
        try:
            return fn()
        except Exception as ex:  # noqa: BLE001
            check('db-query', False, f'{ex!r} {traceback.format_exc()[-300:]}')
            return None


def _flashes():
    with client.session_transaction() as s:
        fl = list(s.get('_flashes') or [])
        s.pop('_flashes', None)
    return fl


TOKEN = ['']


def login():
    client.get('/hdc/login')
    with client.session_transaction() as s:
        TOKEN[0] = s['_csrf_token']
    r = client.post('/hdc/login', data={'username': 'admin', 'password': 'E2e-Test-Only-123',
                                        '_csrf_token': TOKEN[0]})
    with client.session_transaction() as s:
        TOKEN[0] = s.get('_csrf_token', TOKEN[0])
    return r.status_code


def form(_label, _url, data=None, expect_ok=True, **kw):
    name, url = _label, _url
    d = dict(data or {}, **kw)
    d['_csrf_token'] = TOKEN[0]
    _flashes()
    r = client.post(url, data=d)
    fl = _flashes()
    bad = [m for c, m in fl if c in ('danger', 'error') or (
        c == 'warning' and not re.search(r'void|restor|remov|delet|reactivat', m, re.I))]
    ok = r.status_code in (200, 302) and not bad
    if r.status_code == 200:  # re-rendered form: look for inline error alerts
        body = r.get_data(as_text=True)
        m = re.search(r'alert-danger[^>]*>(.*?)<', body, re.S)
        if m and m.group(1).strip():
            ok, bad = False, [m.group(1).strip()]
    if expect_ok:
        check(name, ok, f'status={r.status_code} flashes={fl}')
    else:
        check(name + ' (rejected as expected)', not ok, f'status={r.status_code} flashes={fl}')
    return r


def api(name, url, payload, method='POST', expect=200):
    r = client.open(url, method=method, json=payload, headers={'X-CSRFToken': TOKEN[0]})
    check(name, r.status_code == expect, f'status={r.status_code} body={r.get_data(as_text=True)[:250]}')
    try:
        return r.get_json() or {}
    except Exception:  # noqa: BLE001
        return {}


def get_ok(name, url):
    r = client.get(url)
    check(name, r.status_code in (200, 302), f'status={r.status_code}')
    return r


def near(a, b, tol=0.01):
    try:
        return abs(float(a) - float(b)) <= tol
    except Exception:  # noqa: BLE001
        return False


def run():
    today = _pkt_today()
    D = today.isoformat()
    D1 = (today - timedelta(days=2)).isoformat()
    D2 = (today - timedelta(days=1)).isoformat()

    # ------------------------------------------------------------------ setup
    section('Login & accounts setup')
    check('admin login', login() == 302)
    cash = q(lambda: Account.query.filter_by(name='Company Cash').one().id)
    check('Company Cash auto-created', cash)
    api('fund Company Cash opening 1,000,000', f'/api/accounts/account/{cash}',
        {'opening_balance': 1000000}, 'PUT')
    api('create bank account', '/api/accounts/create_account',
        {'name': 'E2E Bank', 'account_group': 'company', 'account_mode': 'bank',
         'bank_name': 'HBL', 'account_number': '1234567'})
    bank = q(lambda: Account.query.filter_by(name='E2E Bank').one().id)
    check('bank account exists', bank)
    check('cash balance = 1,000,000', near(q(lambda: _account_balance(cash)), 1000000),
          q(lambda: _account_balance(cash)))

    # --------------------------------------------------------------- projects
    section('Projects & stages')
    form('create project A (per sqft)', '/hdc/projects/add', project_code='E2E-A',
         name='Villa DHA', client='Mr Khan', client_phone='0300-1234567', location='Lahore',
         total_sqft=2000, owner_rate=3000, start_date=D1, status='active')
    form('create project B (lump sum)', '/hdc/projects/add', project_code='E2E-B',
         name='Shop Gulberg', client='Ms Ali', lump_sum=500000, start_date=D1, status='active')
    pa = q(lambda: Project.query.filter_by(project_code='E2E-A').one().id)
    pb = q(lambda: Project.query.filter_by(project_code='E2E-B').one().id)
    check('both projects saved', pa and pb)
    check('project A contract = 6,000,000',
          near(q(lambda: db.session.get(Project, pa).owner_contract_value), 6000000),
          q(lambda: db.session.get(Project, pa).owner_contract_value))
    form('stage A1 Grey Structure (per sqft)', f'/hdc/projects/{pa}/stage/add',
         name='Grey Structure', contract_basis='Per Sq Ft', rate_per_sqft=1500, qty_sqft=2000,
         estimated_cost=2000000, status='Active')
    form('stage A2 Finishing (lump)', f'/hdc/projects/{pa}/stage/add', name='Finishing',
         contract_basis='Lump Sum', lump_sum_value=1500000, status='Active')
    form('stage B1 Renovation (lump)', f'/hdc/projects/{pb}/stage/add', name='Renovation',
         contract_basis='Lump Sum', lump_sum_value=500000, status='Active')
    sa1 = q(lambda: Stage.query.filter_by(project_id=pa, name='Grey Structure').one().id)
    sa2 = q(lambda: Stage.query.filter_by(project_id=pa, name='Finishing').one().id)
    sb1 = q(lambda: Stage.query.filter_by(project_id=pb).one().id)
    check('stages saved', sa1 and sa2 and sb1)
    check('stage A1 contract = 3,000,000',
          near(q(lambda: db.session.get(Stage, sa1).contract_value), 3000000),
          q(lambda: db.session.get(Stage, sa1).contract_value))
    form('edit project A location', f'/hdc/projects/{pa}/edit', project_code='E2E-A',
         name='Villa DHA', client='Mr Khan', location='DHA Phase 6', total_sqft=2000,
         owner_rate=3000, start_date=D1, status='active', contract_type='per_sqft')
    check('project edit persisted',
          q(lambda: db.session.get(Project, pa).location) == 'DHA Phase 6',
          q(lambda: db.session.get(Project, pa).location))
    form('stage status update', f'/hdc/stage/{sa1}/status', status='Active')

    # ---------------------------------------------------------- client money
    section('Client payments (receiving)')
    form('receive 500,000 from client A into Cash', f'/hdc/projects/{pa}/owner_payment',
         date=D1, amount=500000, received_to_account_id=cash, remarks='1st installment')
    form('receive 300,000 from client A into Bank', f'/hdc/projects/{pa}/owner_payment',
         date=D2, amount=300000, received_to_account_id=bank, remarks='2nd installment')
    form('receive 100,000 from client B into Cash', f'/hdc/projects/{pb}/owner_payment',
         date=D2, amount=100000, received_to_account_id=cash, remarks='advance')
    check('3 owner payments recorded', q(lambda: OwnerPayment.query.count()) == 3,
          q(lambda: OwnerPayment.query.count()))
    check('bank balance = 300,000', near(q(lambda: _account_balance(bank)), 300000),
          q(lambda: _account_balance(bank)))
    op_b = q(lambda: OwnerPayment.query.filter_by(project_id=pb).one().id)
    form('void client B receipt', f'/hdc/projects/{pb}/owner_payment/{op_b}/delete',
         void_reason='e2e void test')
    check('cash excludes voided receipt', near(q(lambda: _account_balance(cash)), 1500000),
          q(lambda: _account_balance(cash)))
    form('restore client B receipt', f'/hdc/projects/{pb}/owner_payment/{op_b}/restore')
    CASH = 1600000
    check('cash = 1,600,000 after restore', near(q(lambda: _account_balance(cash)), CASH),
          q(lambda: _account_balance(cash)))
    form('reject zero-amount receipt', f'/hdc/projects/{pa}/owner_payment', date=D,
         amount=0, received_to_account_id=cash, expect_ok=False)

    # ---------------------------------------------------------------- workers
    section('Workers, rates, timekeeping')
    form('add trade', '/hdc/trades', action='add', trade_name='Steel Fixer')
    form('worker Mason daily 1500', '/hdc/workers', worker_code='E2E-W1', name='Aslam Mason',
         role_type='Mason', wage_type='daily', daily_wage=1500)
    form('worker Labour daily 1000', '/hdc/workers', worker_code='E2E-W2', name='Bilal Labour',
         role_type='Labor', wage_type='daily', daily_wage=1000)
    form('worker Electrician hourly 250', '/hdc/workers', worker_code='E2E-W3',
         name='Chand Electrician', role_type='Electrician', wage_type='hourly', hourly_rate=250)
    w1 = q(lambda: Worker.query.filter_by(worker_code='E2E-W1').one().id)
    w2 = q(lambda: Worker.query.filter_by(worker_code='E2E-W2').one().id)
    w3 = q(lambda: Worker.query.filter_by(worker_code='E2E-W3').one().id)
    check('3 workers saved', w1 and w2 and w3)
    form('duplicate worker code rejected', '/hdc/workers', worker_code='E2E-W1', name='Dup',
         role_type='Mason', wage_type='daily', daily_wage=1, expect_ok=False)
    form('rate change W1 -> 1600 from D2', f'/hdc/workers/{w1}/rate', wage_type='daily',
         new_rate=1600, effective_from=D2, reason='increment')

    def tk(day, rows):
        data = {'bulk_mode': '1', 'date': day}
        for wid, (st, pid, sid, hrs, ot) in rows.items():
            data.update({f'status_{wid}': st, f'project_id_{wid}': pid, f'stage_id_{wid}': sid,
                         f'working_hours_{wid}': hrs, f'overtime_hours_{wid}': ot})
        return data
    form(f'attendance day1 ({D1})', '/hdc/timekeeping',
         tk(D1, {w1: ('present', pa, sa1, 8, 0), w2: ('present', pa, sa1, 8, 0),
                 w3: ('present', pb, sb1, 6, 0)}))
    form(f'attendance day2 ({D2}) with overtime + absent', '/hdc/timekeeping',
         tk(D2, {w1: ('present', pa, sa1, 8, 2), w2: ('absent', '', '', 0, 0),
                 w3: ('present', pa, sa2, 8, 0)}))
    form(f'attendance day3 ({D})', '/hdc/timekeeping',
         tk(D, {w1: ('present', pa, sa2, 8, 0), w2: ('present', pb, sb1, 8, 0),
                w3: ('absent', '', '', 0, 0)}))

    def wages(wid):
        return round(sum(t.wage_calculated or 0 for t in
                         TimeEntry.query.filter_by(worker_id=wid, is_void=False)), 2)
    # W1: 1500 (old rate) + 1600+2*200 (OT) + 1600 = 5100
    check('W1 wages = 5,100 (rate history + overtime)', near(q(lambda: wages(w1)), 5100),
          q(lambda: wages(w1)))
    check('W2 wages = 2,000 (absent not paid)', near(q(lambda: wages(w2)), 2000),
          q(lambda: wages(w2)))
    check('W3 wages = 3,500 (6h+8h @250)', near(q(lambda: wages(w3)), 3500),
          q(lambda: wages(w3)))
    form('re-posting same day does not duplicate', '/hdc/timekeeping',
         tk(D, {w1: ('present', pa, sa2, 8, 0), w2: ('present', pb, sb1, 8, 0),
                w3: ('absent', '', '', 0, 0)}))
    check('W1 still 3 entries', q(lambda: TimeEntry.query.filter_by(worker_id=w1, is_void=False).count()) == 3,
          q(lambda: TimeEntry.query.filter_by(worker_id=w1, is_void=False).count()))
    te = q(lambda: TimeEntry.query.filter_by(worker_id=w3, is_void=False)
           .order_by(TimeEntry.check_in).first().id)
    form('void a time entry', f'/hdc/timekeeping/{te}/delete', void_reason='wrong entry')
    check('W3 wages drop to 2,000', near(q(lambda: wages(w3)), 2000), q(lambda: wages(w3)))
    form('reactivate time entry', f'/hdc/timekeeping/{te}/reactivate')
    check('W3 wages back to 3,500', near(q(lambda: wages(w3)), 3500), q(lambda: wages(w3)))

    # ------------------------------------------------ advances/wages/tips
    section('Advances, wage payments, tips')
    form('advance 1,000 to W1', f'/hdc/workers/{w1}/advance', date=D, amount=1000,
         project_id=pa, stage_id=sa1, notes='advance')
    check('W1 payable = 4,100 after advance', near(q(lambda: _worker_payable_snapshot(w1)['balance']), 4100),
          q(lambda: _worker_payable_snapshot(w1)))
    form('pay W1 2,000 wages', f'/hdc/workers/{w1}/payment', date=D, amount=2000,
         project_id=pa, stage_id=sa1, notes='wages')
    check('W1 payable = 2,100', near(q(lambda: _worker_payable_snapshot(w1)['balance']), 2100),
          q(lambda: _worker_payable_snapshot(w1)))
    form('pay W2 2,500 (500 over) as TIP', f'/hdc/workers/{w2}/payment', date=D, amount=2500,
         project_id=pb, stage_id=sb1, overpay_as_tip='1', notes='wages+tip')
    check('W2 payable = 0 after tip', near(q(lambda: _worker_payable_snapshot(w2)['balance']), 0),
          q(lambda: _worker_payable_snapshot(w2)))
    check('W2 tip ledger row = 500',
          near(q(lambda: sum(l.amount for l in LabourLedger.query.filter_by(
              worker_id=w2, entry_type='tip', is_void=False))), 500),
          q(lambda: [(l.entry_type, l.amount) for l in LabourLedger.query.filter_by(worker_id=w2)]))
    form('pay W3 4,000 (500 over) as ADVANCE', f'/hdc/workers/{w3}/payment', date=D,
         amount=4000, project_id=pa, stage_id=sa2, overpay_as_advance='1')
    check('W3 payable = -500 (advance carried)',
          near(q(lambda: _worker_payable_snapshot(w3)['balance']), -500),
          q(lambda: _worker_payable_snapshot(w3)))
    form('overpay with no option is refused', f'/hdc/workers/{w2}/payment', date=D,
         amount=999, project_id=pb, stage_id=sb1, expect_ok=False)
    lid = q(lambda: LabourLedger.query.filter_by(worker_id=w1, entry_type='payment',
                                                 is_void=False).first().id)
    form('void W1 payment', f'/hdc/workers/{w1}/ledger/{lid}/void', void_reason='test')
    check('W1 payable back to 4,100', near(q(lambda: _worker_payable_snapshot(w1)['balance']), 4100),
          q(lambda: _worker_payable_snapshot(w1)))
    form('restore W1 payment', f'/hdc/workers/{w1}/ledger/{lid}/restore')
    check('W1 payable = 2,100 again', near(q(lambda: _worker_payable_snapshot(w1)['balance']), 2100),
          q(lambda: _worker_payable_snapshot(w1)))
    CASH -= 1000 + 2000 + 2500 + 4000
    check(f'cash = {CASH:,} after labour payments', near(q(lambda: _account_balance(cash)), CASH),
          q(lambda: _account_balance(cash)))

    # ---------------------------------------------------------------- payroll
    section('Payroll')
    form('generate payroll D1..D', '/hdc/payroll/generate', date_from=D1, date_to=D)
    run_id = q(lambda: PayrollItem.query.filter_by(worker_id=w1).first().run_id)
    check('payroll run created', run_id, 'no PayrollItem for W1')
    if run_id:
        form('pay all from payroll', '/hdc/payroll/generate', action='pay_all', run_id=run_id)
        check('W1 fully settled by payroll',
              near(q(lambda: _worker_payable_snapshot(w1)['balance']), 0),
              q(lambda: _worker_payable_snapshot(w1)))
        check('W3 advance not over-deducted/paid',
              q(lambda: _worker_payable_snapshot(w3)['balance']) <= 0,
              q(lambda: _worker_payable_snapshot(w3)))
        n = q(lambda: LabourLedger.query.filter_by(entry_type='payment').count())
        form('pay_worker twice is idempotent', '/hdc/payroll/generate', action='pay_worker',
             run_id=run_id, worker_id=w1)
        check('no duplicate payroll payment',
              q(lambda: LabourLedger.query.filter_by(entry_type='payment').count()) == n)
    CASH -= 2100
    check(f'cash = {CASH:,} after payroll', near(q(lambda: _account_balance(cash)), CASH),
          q(lambda: _account_balance(cash)))

    # ---------------------------------------------------------- subcontract
    section('Subcontractors')
    form('add subcontractor to stage A1', f'/hdc/projects/{pa}/add_subcontractor',
         stage_id=sa1, name='Rehman Builders', phone='0301-5555555')
    sub = q(lambda: Subcontractor.query.filter_by(name='Rehman Builders').one().id)
    check('subcontractor saved', sub)
    form('shift stage A1 to subcontractor (per sqft 400 x 2000)',
         f'/hdc/stage/{sa1}/shift/subcontractor', subcontractor_id=sub,
         contract_type='sqft', rate_per_sqft=400, total_sqft=2000, retention_pct=0)
    check('contract value 800,000',
          near(q(lambda: db.session.get(Subcontractor, sub).contract_value), 800000),
          q(lambda: {k: v for k, v in vars(db.session.get(Subcontractor, sub)).items()
                     if 'contract' in k or 'total' in k}))
    form('team attendance 3 masons x 2 days @1200', f'/hdc/subcontractor/{sub}/team_attendance',
         project_id=pa, stage_id=sa1, date=D, worker_type='Mason', period_from=D2, period_to=D,
         days_count=2, workers_count=3, wage_rate=1200)
    check('team attendance total 7,200',
          near(q(lambda: sum(r.total_amount for r in SubcontractTeamAttendance.query
                             .filter_by(subcontractor_id=sub))), 7200),
          q(lambda: [r.total_amount for r in SubcontractTeamAttendance.query.all()]))
    form('add sub labour worker', f'/hdc/subcontractor/{sub}/workers', name='Sub Labour 1',
         trade='Labour', daily_wage=900, phone='')
    slw = q(lambda: SubcontractLabourWorker.query.filter_by(subcontractor_id=sub).first().id)
    check('sub labour worker saved', slw)
    if slw:
        form('sub labour attendance (bulk)', f'/hdc/subcontractor/{sub}/labour_attendance',
             {'bulk_mode': '1', 'date': D, 'worker_ids': str(slw),
              f'attendance_status_{slw}': 'present', f'project_id_{slw}': pa,
              f'stage_id_{slw}': sa1, f'working_hours_{slw}': 8})
        check('sub labour attendance row',
              q(lambda: SubcontractLabourAttendance.query.filter_by(worker_id=slw).count()) >= 1)
        form('pay sub labour worker 900', f'/hdc/subcontractor/{sub}/workers/{slw}/pay',
             date=D, amount=900, project_id=pa, stage_id=sa1, notes='labour pay')
    form('progress 25%', f'/hdc/subcontractor/{sub}/progress', work_done_percentage=25)
    form('stage sub-progress 25%', f'/hdc/stage/{sa1}/sub-progress', subcontractor_id=sub,
         work_done_percentage=25)
    form('pay subcontractor 150,000 from Bank?', f'/hdc/subcontractor/{sub}/pay', date=D,
         amount=150000, project_id=pa, stage_id=sa1, notes='running bill 1')
    check('sub payment recorded',
          near(q(lambda: sum(p.amount for p in SubcontractPayment.query
                             .filter_by(subcontractor_id=sub, is_void=False))), 150000))
    CASH -= 900 + 150000
    check(f'cash = {CASH:,} after subcontract', near(q(lambda: _account_balance(cash)), CASH),
          q(lambda: _account_balance(cash)))
    form('second subcontractor on stage B1', f'/hdc/projects/{pb}/add_subcontractor',
         stage_id=sb1, name='Tile Masters')
    sub2 = q(lambda: Subcontractor.query.filter_by(name='Tile Masters').one().id)
    form('shift B1 to sub2 lump sum 200,000', f'/hdc/stage/{sb1}/shift/subcontractor',
         subcontractor_id=sub2, contract_type='lump_sum', lump_sum_amount=200000)
    form('pay sub2 50,000', f'/hdc/subcontractor/{sub2}/pay', date=D, amount=50000,
         project_id=pb, stage_id=sb1)
    CASH -= 50000

    # ------------------------------------------------------------- purchases
    section('Suppliers, materials, purchases, delivery, usage')
    form('supplier Cement Co (opening 10,000)', '/hdc/purchase-v2/suppliers',
         name='Cement Co', phone='0302-1111111', opening_balance=10000, address='Raiwind')
    form('supplier Steel Traders', '/hdc/purchase-v2/suppliers', name='Steel Traders')
    s1 = q(lambda: Supplier.query.filter_by(name='Cement Co').one().id)
    s2 = q(lambda: Supplier.query.filter_by(name='Steel Traders').one().id)
    form('material Cement (bag)', '/hdc/purchase-v2/materials', name='Cement', unit='Bag')
    form('material Steel (KG)', '/hdc/purchase-v2/materials', name='Steel', unit='KG')
    m1 = q(lambda: MaterialV2.query.filter_by(name='Cement').one().id)
    m2 = q(lambda: MaterialV2.query.filter_by(name='Steel').one().id)
    check('suppliers & materials saved', all([s1, s2, m1, m2]))
    form('purchase 200 bags @1,300 (unpaid, multi-item form)', '/hdc/purchase-v2/purchases',
         {'supplier_id': s1, 'payment_status': 'unpaid', 'date': D2, 'challan_no': 'CH-1',
          'material_id[]': [m1], 'quantity[]': [200], 'unit_price[]': [1300], 'notes[]': ['']})
    form('purchase 1000kg steel @280 (unpaid)', '/hdc/purchase-v2/purchases',
         {'supplier_id': s2, 'payment_status': 'unpaid', 'date': D2,
          'material_id[]': [m2], 'quantity[]': [1000], 'unit_price[]': [280]})
    po1 = q(lambda: PurchaseV2.query.filter_by(supplier_id=s1).one().id)
    po2 = q(lambda: PurchaseV2.query.filter_by(supplier_id=s2).one().id)
    check('supplier1 balance = 270,000 (10k opening + 260k)', near(q(lambda: _supplier_balance(s1)), 270000),
          q(lambda: _supplier_balance(s1)))
    form('deliver 150 bags to A1', '/hdc/purchase-v2/delivered', purchase_id=po1, project_id=pa,
         stage_id=sa1, quantity=150, date=D2, delivery_person='Driver')
    form('deliver 50 bags to B1', '/hdc/purchase-v2/delivered', purchase_id=po1, project_id=pb,
         stage_id=sb1, quantity=50, date=D2)
    form('over-delivery refused', '/hdc/purchase-v2/delivered', purchase_id=po1, project_id=pa,
         stage_id=sa1, quantity=1, date=D2, expect_ok=False)
    form('deliver 1000kg steel to A1', '/hdc/purchase-v2/delivered', purchase_id=po2,
         project_id=pa, stage_id=sa1, quantity=1000, date=D2)
    form('transfer 20 bags A1 -> A2', '/hdc/purchase-v2/delivered/transfer', purchase_id=po1,
         material_id=m1, from_project_id=pa, from_stage_id=sa1, to_project_id=pa,
         to_stage_id=sa2, quantity=20, date=D)
    form('use 100 bags on A1', '/hdc/purchase-v2/usage', purchase_id=po1, material_id=m1,
         project_id=pa, stage_id=sa1, quantity=100, date=D)
    form('use 500kg steel on A1', '/hdc/purchase-v2/usage', purchase_id=po2, material_id=m2,
         project_id=pa, stage_id=sa1, quantity=500, date=D)
    form('over-usage refused (A1 has 30 bags left)', '/hdc/purchase-v2/usage', purchase_id=po1,
         material_id=m1, project_id=pa, stage_id=sa1, quantity=31, date=D, expect_ok=False)
    check('usage cost cement = 130,000',
          near(q(lambda: sum(u.cost for u in UsageLogV2.query.filter_by(material_id=m1,
                                                                        is_void=False))), 130000),
          q(lambda: [(u.quantity, u.cost) for u in UsageLogV2.query.all()]))
    u2 = q(lambda: UsageLogV2.query.filter_by(material_id=m2).first().id)
    form('void steel usage', f'/hdc/purchase-v2/usage/{u2}/void', void_reason='test')
    form('re-use 400kg steel', '/hdc/purchase-v2/usage', purchase_id=po2, material_id=m2,
         project_id=pa, stage_id=sa1, quantity=400, date=D)
    form('pay Cement Co 100,000', f'/hdc/purchase-v2/suppliers/{s1}/payment', amount=100000,
         date=D, entry_kind='payment', note='part payment', from_account_id=cash)
    check('supplier1 balance = 170,000', near(q(lambda: _supplier_balance(s1)), 170000),
          q(lambda: _supplier_balance(s1)))
    form('pay Steel Traders 280,000 (full)', f'/hdc/purchase-v2/suppliers/{s2}/payment',
         amount=280000, date=D, entry_kind='payment', from_account_id=cash)
    check('supplier2 balance = 0', near(q(lambda: _supplier_balance(s2)), 0),
          q(lambda: _supplier_balance(s2)))
    CASH -= 380000
    check(f'cash = {CASH:,} after supplier payments', near(q(lambda: _account_balance(cash)), CASH),
          q(lambda: _account_balance(cash)))
    for u in ('/hdc/purchase-v2/stock', f'/hdc/purchase-v2/suppliers/{s1}', '/api/v2/purchase/kpis',
              '/api/v2/purchase/material-stock', '/api/v2/purchase/supplier-ledger'):
        get_ok(f'GET {u}', u)

    # --------------------------------------------------------------- expenses
    section('Expenses')
    form('expense category add', '/hdc/expense_categories', action='add_category',
         category_name='E2E Transport')
    cat = q(lambda: ExpenseCategory.query.filter_by(name='E2E Transport').one().id)
    check('category saved', cat)
    form('expense 5,000 on A1', '/hdc/expenses', project_id=pa, stage_id=sa1, category_id=cat,
         amount=5000, date=D, remarks='truck')
    form('expense 2,000 on B1', '/hdc/expenses', project_id=pb, stage_id=sb1, category_id=cat,
         amount=2000, date=D, remarks='fuel')
    e2 = q(lambda: Expense.query.filter_by(project_id=pb, remarks='fuel').one().id)
    form('edit expense 2,000 -> 2,500', f'/hdc/expenses/{e2}/edit', project_id=pb,
         stage_id=sb1, category_id=cat, amount=2500, date=D, remarks='fuel')
    check('expense edit persisted', near(q(lambda: db.session.get(Expense, e2).amount), 2500),
          q(lambda: db.session.get(Expense, e2).amount))
    form('void expense', f'/hdc/expenses/{e2}/delete', void_reason='test')
    check('expense voided', q(lambda: db.session.get(Expense, e2).is_void))
    CASH -= 5000
    check(f'cash = {CASH:,} after expenses', near(q(lambda: _account_balance(cash)), CASH),
          q(lambda: _account_balance(cash)))
    form('expense without amount refused', '/hdc/expenses', project_id=pa, stage_id=sa1,
         category_id=cat, amount='', date=D, expect_ok=False)

    # ----------------------------------------------------------------- office
    section('Office management')
    form('office staff add', '/hdc/office-management/staff', staff_code='E2E-S1',
         name='Office Clerk', role_type='Clerk', phone='0303', monthly_salary=40000)
    st = q(lambda: OfficeStaff.query.filter_by(name='Office Clerk').one().id)
    check('staff saved', st)
    if st:
        form('staff salary payment 20,000', f'/hdc/office-management/staff/{st}/ledger', date=D,
             entry_type='payment', amount=20000, notes='half salary')
        check('staff ledger row', q(lambda: OfficeStaffLedger.query.filter_by(staff_id=st).count()) >= 1)
        get_ok('staff ledger page', f'/hdc/office-management/staff/{st}/ledger')
    form('office expense 3,000', '/hdc/office-management/expenses', date=D, category='Utilities',
         amount=3000, remarks='electricity bill')
    check('office expense saved', q(lambda: OfficeExpense.query.count()) >= 1)
    form('allowance category', '/hdc/office-management/allowance-categories', name='Fuel',
         description='fuel allowance')
    CASH -= 23000
    check(f'cash = {CASH:,} after office', near(q(lambda: _account_balance(cash)), CASH),
          q(lambda: _account_balance(cash)))

    # ------------------------------------------------------------ tool rental
    section('Tool rental')
    form('tool category', '/hdc/tool-rental/inventory', action='add_category', category_name='Shuttering')
    tcat = q(lambda: ToolCategory.query.first().id)
    form('tool inventory: 50 steel props', '/hdc/tool-rental/inventory', action='add_tool',
         name='Steel Prop', tool_code='TP-1', category_id=tcat or '', total_quantity=50,
         purchase_cost=2000, rental_rate_per_day=20, unit='pcs', condition='good')
    tool = q(lambda: Tool.query.filter_by(name='Steel Prop').one().id)
    check('tool saved', tool)
    if tool:
        form('rent 10 props to external customer', '/hdc/tool-rental/create',
             {'renter_type': 'external', 'customer_name': 'Neighbour Contractor',
              'customer_phone': '0304', 'billing_type': 'per_day', 'rental_date': D2,
              'expected_return_date': D, 'tool_id[]': [tool], 'qty[]': [10], 'rate[]': [20]})
        rent = q(lambda: ToolRental.query.first().id)
        check('rental saved', rent)
        if rent:
            get_ok('rental detail', f'/hdc/tool-rental/{rent}')
            items = q(lambda: [i.id for i in ToolRentalItem.query.filter_by(rental_id=rent)])
            form('return all props + receive payment', f'/hdc/tool-rental/{rent}/return',
                 {'return_date': D, 'return_type': 'full', 'rental_item_id[]': items,
                  'qty_returned[]': [10] * len(items), 'payment_type': 'full',
                  'amount_paid': 400, 'received_to_account_id': cash, 'payment_mode': 'cash'})
            check('tool payment recorded', q(lambda: ToolRentalPayment.query.count()) >= 1,
                  q(lambda: ToolRentalPayment.query.count()))

    # --------------------------------------------------- accounts / cashflow
    section('Accounts: transfer, CF register, money center')
    CASH = q(lambda: _account_balance(cash))  # tool income may vary by billing days
    form('CF register: money IN 25,000 (other income)', '/hdc/accounts/cashflow/register',
         action='create_entry', direction='in', amount=25000, account_id=cash, date=D,
         party_name='Scrap buyer', description='scrap sale',
         category_id=q(lambda: CashFlowCategory.query.filter_by(direction='in', name='Other Income').first().id))
    form('CF register: money OUT 4,000', '/hdc/accounts/cashflow/register', action='create_entry',
         direction='out', amount=4000, account_id=cash, date=D, description='misc',
         category_id=q(lambda: CashFlowCategory.query.filter_by(direction='out').first().id))
    form('CF register: TRANSFER 50,000 cash -> bank', '/hdc/accounts/cashflow/register',
         action='create_entry', direction='transfer', amount=50000, account_id=cash,
         destination_account_id=bank, date=D, description='deposit')
    check('cash moved +25k -4k -50k', near(q(lambda: _account_balance(cash)), CASH + 25000 - 4000 - 50000),
          f'{q(lambda: _account_balance(cash))} vs {CASH + 25000 - 4000 - 50000}')
    check('bank = 350,000', near(q(lambda: _account_balance(bank)), 350000),
          q(lambda: _account_balance(bank)))
    eid = q(lambda: CashFlowEntry.query.filter_by(direction='out').order_by(CashFlowEntry.id.desc()).first().id)
    if eid:
        form('CF void entry', '/hdc/accounts/cashflow/register', action='void_entry',
             entry_id=eid, reason='mistake')
        form('CF restore entry', '/hdc/accounts/cashflow/register', action='restore_entry',
             entry_id=eid)
    form('overdraft refused (transfer 99,999,999)', '/hdc/accounts/cashflow/register',
         action='create_entry', direction='transfer', amount=99999999, account_id=cash,
         destination_account_id=bank, date=D, expect_ok=False)
    for u in ('/hdc/accounts/hub', '/hdc/accounts/money-center', '/hdc/accounts',
              '/hdc/accounts/entries', '/hdc/accounts/manage', '/hdc/accounts/cashflow',
              '/hdc/accounts/cashflow/register', '/hdc/accounts/cashflow/reconciliation',
              f'/hdc/accounts/{cash}/ledger', f'/hdc/accounts/{bank}/ledger',
              '/api/accounts/dashboard_summary', '/api/accounts/pending_context',
              '/api/accounts/list_accounts_with_balances', '/api/accounts/transaction_history'):
        get_ok(f'GET {u}', u)

    section('Reconciliation & day close')
    findings = q(_accounts_reconciliation_findings) or {}
    for k, v in findings.items():
        if isinstance(v, list):
            check(f'reconciliation: no "{k}" findings', v == [], v[:3])
    from hdc.services.cashflow_register import day_positions, save_counted_position, lock_cash_day, unlock_cash_day

    def close_day():
      with app.test_request_context():
        for p in day_positions(today):
            save_counted_position(today, p.account_id, p.expected_closing_minor / 100, actor='admin')
        lock_cash_day(today, actor='admin')
        db.session.commit()
        return CashDayLock.query.count()
    check('day close with matching counts locks the day', q(close_day))
    form('back-dated entry refused on locked day', '/hdc/accounts/cashflow/register',
         action='create_entry', direction='out', amount=10, account_id=cash, date=D,
         category_id=q(lambda: CashFlowCategory.query.filter_by(direction='out').first().id),
         expect_ok=False)
    def _unlock():
        with app.test_request_context():
            unlock_cash_day(today); db.session.commit()
        return True
    check('unlock day', q(_unlock))

    # ------------------------------------------------------ project costing
    section('Project costing & reports')
    m = q(lambda: _aggregate_project_costs([pa, pb])) or {}
    ma, mb = m.get(pa, {}), m.get(pb, {})
    print('   project A metrics:', {k: ma.get(k) for k in ('labour', 'material', 'expense', 'subcontract')})
    print('   project B metrics:', {k: mb.get(k) for k in ('labour', 'material', 'expense', 'subcontract')})
    check('A material cost = 130,000 cement + 112,000 steel', near(ma.get('material'), 242000), ma.get('material'))
    check('A expense = 5,000', near(ma.get('expense'), 5000), ma.get('expense'))
    check('B expense = 500 (W2 tip only; voided fuel excluded)', near(mb.get('expense', 0), 500), mb.get('expense'))
    check('A subcontract >= 150,000', (ma.get('subcontract') or 0) >= 150000, ma.get('subcontract'))
    check('B subcontract = 50,000', near(mb.get('subcontract'), 50000), mb.get('subcontract'))
    r = client.get('/hdc/reports/export/profitability')
    check('profitability CSV', r.status_code == 200)
    rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    check('profitability has both projects', {'E2E-A', 'E2E-B'} <= {x.get('Code') for x in rows},
          [x.get('Code') for x in rows])
    for u in (f'/hdc/projects/{pa}', f'/hdc/projects/{pb}', f'/hdc/stage/{sa1}/ledger',
              f'/hdc/reports?project_id={pa}', f'/hdc/reports/project/{pa}/csv',
              '/hdc/reports/export/salary', '/hdc/reports/export/materials', '/hdc/reports/glance',
              f'/hdc/workers/{w1}/ledger', f'/hdc/workers/{w2}/ledger',
              f'/hdc/subcontractor/{sub}' if False else '/hdc/subcontractors'):
        get_ok(f'GET {u}', u)

    # --------------------------------------------------------------- crawl
    section('Crawl every GET route (no 5xx allowed)')
    ids = {'pid': pa, 'sid': sa1, 'wid': w1, 'tid': te, 'eid': e2, 'supplier_id': s1,
           'material_id': m1, 'purchase_id': po1, 'account_id': cash, 'aid': 1, 'run_id': run_id or 1,
           'rental_id': 1, 'tool_id': tool or 1, 'staff_id': st or 1, 'txn_id': 1, 'id': 1,
           'lid': 1, 'oid': op_b, 'cid': 1, 'did': 1, 'rid': 1, 'delivery_id': 1, 'usage_id': 1}
    seen, errors = 0, []
    for rule in app.url_map.iter_rules():
        if 'GET' not in rule.methods or rule.endpoint == 'static' or 'logout' in rule.rule \
                or 'deploy' in rule.rule or 'backup' in rule.rule:
            continue
        url = rule.rule
        for arg in rule.arguments:
            val = ids.get(arg, 1)
            url = re.sub(r'<(?:[a-z]+:)?%s>' % arg, str(val), url)
        if '<' in url:
            continue
        try:
            code = client.get(url).status_code
        except Exception as ex:  # noqa: BLE001
            code = f'EXC {ex!r}'[:150]
        seen += 1
        if not isinstance(code, int) or code >= 500:
            errors.append((url, code))
    check(f'crawled {seen} GET routes without server error', not errors, errors[:15])
    for url, code in errors:
        print('     5xx:', url, code)
    login()  # in case a crawled route logged us out

    section('Database integrity')
    from sqlalchemy import text
    check('PRAGMA integrity_check ok',
          q(lambda: db.session.execute(text('PRAGMA integrity_check')).scalar()) == 'ok')
    check('no FK violations', q(lambda: db.session.execute(text('PRAGMA foreign_key_check')).all()) == [])


if __name__ == '__main__':
    try:
        run()
    except Exception:  # noqa: BLE001
        check('runner crashed', False, traceback.format_exc())
        traceback.print_exc()
    passed = sum(1 for r in RESULTS if r[2])
    failed = [r for r in RESULTS if not r[2]]
    print(f'\n==== {passed}/{len(RESULTS)} checks passed, {len(failed)} failed')
    for sec, name, _, det in failed:
        print(f'FAIL [{sec}] {name}: {det}')
    sys.exit(1 if failed else 0)
