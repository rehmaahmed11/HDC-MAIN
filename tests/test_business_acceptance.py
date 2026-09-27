"""One HTTP-driven business dataset, independently reconciled at each boundary."""
import csv
import io
from datetime import datetime

from qa_support import IsolatedAppTest
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction, Expense, ExpenseCategory
from hdc.models.projects import Project, Stage
from hdc.models.workforce import Worker, WorkerRate, TimeEntry, LabourLedger, PayrollItem
from hdc.models.materials import PurchaseV2, SupplierLedger, Delivery, UsageLogV2
from hdc.models.subcontract import Subcontractor, SubcontractTeamAttendance, SubcontractPayment
from hdc.models.cashflow import CashFlowEntry, CashDayLock
from hdc.services.accounts import _account_balance, _accounts_reconciliation_findings
from hdc.services.ledger import _worker_payable_snapshot
from hdc.services.purchase import _supplier_balance
from hdc.services.aggregation import _aggregate_project_costs
from hdc.services.cashflow_register import (category_options, save_manual_cash_flow_entry,
    day_positions, save_counted_position, lock_cash_day, unlock_cash_day)
from hdc.utils.dates import _pkt_today


class BusinessAcceptanceTest(IsolatedAppTest):
    def test_complete_business_scenario_reconciles(self):
        day = _pkt_today(); ds = day.isoformat()
        with self.app.app_context():
            cash = Account.query.filter_by(name='Company Cash').one().id
            category = ExpenseCategory.query.filter_by(active_status=True).first().id
        self.api(f'/api/accounts/account/{cash}', {'opening_balance': 100000}, 'PUT')
        self.form('/hdc/projects/add', project_code='QA-E2E', name='Release Site', client='QA Owner',
                  lump_sum=20000, start_date=ds)
        with self.app.app_context():
            project = Project.query.filter_by(project_code='QA-E2E').one()
            pid = project.id; self.assertEqual(project.owner_contract_value, 20000)
        self.form(f'/hdc/projects/{pid}/stage/add', name='Structure', contract_basis='Lump Sum',
                  lump_sum_value=10000, status='Active')
        with self.app.app_context():
            stage = Stage.query.filter_by(project_id=pid).one(); sid=stage.id
            self.assertEqual(stage.contract_value, 10000)
        scope = dict(project_id=pid, stage_id=sid, date=ds)
        self.form('/hdc/workers', worker_code='QA-W', name='Release Worker', role_type='Mason',
                  wage_type='daily', daily_wage=800)
        with self.app.app_context():
            wid = Worker.query.filter_by(worker_code='QA-W').one().id
        self.form(f'/hdc/workers/{wid}/rate', wage_type='daily', new_rate=1000,
                  effective_from=ds, reason='QA rate')
        self.form('/hdc/timekeeping', **dict(bulk_mode='1', date=ds, **{
            f'status_{wid}':'present',f'project_id_{wid}':pid, f'stage_id_{wid}':sid,
            f'working_hours_{wid}':8, f'overtime_hours_{wid}':2}))
        with self.app.app_context():
            entry = TimeEntry.query.filter_by(worker_id=wid, is_void=False).one()
            self.assertEqual(entry.wage_calculated, 1250) # 1000 + 2 * (1000/8)
            self.assertTrue(WorkerRate.query.filter_by(worker_id=wid, rate=1000).first())
        self.form(f'/hdc/workers/{wid}/advance', **scope, amount=200, notes='QA advance')
        self.form(f'/hdc/workers/{wid}/payment', **scope, amount=500, notes='QA wages')
        with self.app.app_context():
            snap = _worker_payable_snapshot(wid)
            self.assertEqual(snap['balance'], 550)
            self.assertEqual(LabourLedger.query.filter_by(worker_id=wid, entry_type='payment', is_void=False).one().amount, 500)
        supplier = self.api('/api/v2/purchase/suppliers', {'name':'Release Supplier'})['id']
        material = self.api('/api/v2/purchase/materials', {'name':'QA Cement','unit':'KG'})['id']
        purchase = self.api('/api/v2/purchase/purchases', dict(supplier_id=supplier,material_id=material,
                            unit_price=100,quantity=10))['id']
        delivery = self.api('/api/v2/purchase/deliveries', dict(scope,purchase_id=purchase,quantity=6))['id']
        self.api('/api/v2/purchase/deliveries', dict(scope,purchase_id=purchase,quantity=5),expected=400)
        self.api('/api/v2/purchase/deliveries', dict(scope,purchase_id=purchase,quantity=4))
        usage = self.api('/api/v2/purchase/usage', dict(scope,purchase_id=purchase,material_id=material,quantity=4))['id']
        self.api('/api/v2/purchase/payments', dict(supplier_id=supplier,amount=300,note='QA payment'))
        with self.app.app_context():
            self.assertEqual(db.session.get(PurchaseV2,purchase).total_amount,1000)
            self.assertEqual(sum(r.quantity for r in Delivery.query.filter_by(purchase_id=purchase)),10)
            self.assertEqual(db.session.get(UsageLogV2,usage).cost,400)
            self.assertEqual(_supplier_balance(supplier),700)
        self.form('/hdc/expenses', **scope, category_id=category,amount=50,remarks='QA expense')
        with self.app.app_context():
            self.assertEqual(Expense.query.filter_by(project_id=pid,is_void=False).one().amount,50)
        self.form(f'/hdc/projects/{pid}/add_subcontractor', name='Release Contractor')
        with self.app.app_context():
            subid=Subcontractor.query.filter_by(name='Release Contractor').one().id
        self.form(f'/hdc/stage/{sid}/shift/subcontractor',subcontractor_id=subid,
                  contract_type='lump_sum',lump_sum_amount=2000)
        self.form(f'/hdc/subcontractor/{subid}/team_attendance',**scope,
                  worker_type='Mason',days_count=1,workers_count=2,wage_rate=100)
        self.form(f'/hdc/subcontractor/{subid}/pay',**scope,amount=400,notes='QA sub payment')
        with self.app.app_context():
            self.assertEqual(SubcontractTeamAttendance.query.filter_by(subcontractor_id=subid).one().total_amount,200)
            self.assertEqual(SubcontractPayment.query.filter_by(subcontractor_id=subid,is_void=False).one().amount,400)
            self.assertEqual(db.session.get(Subcontractor,subid).contract_balance,1600)
        self.api('/api/accounts/create_account',dict(name='QA Secondary Cash',account_group='company',account_mode='cash'))
        with self.app.app_context():
            secondary=Account.query.filter_by(name='QA Secondary Cash').one().id
            income=next(c for c in category_options('in') if c.name=='Other Income')
            # Exercise the same canonical service as the transaction form; HTTP form
            # contracts are separately covered by test_new_transaction_form.
            receipt,_=save_manual_cash_flow_entry(direction='in',amount=200,account_id=cash,
                        category_id=income.id,description='QA receipt',actor='admin')
            transfer,_=save_manual_cash_flow_entry(direction='transfer',amount=100,account_id=cash,
                        destination_account_id=secondary,description='QA transfer',actor='admin')
            db.session.commit(); receipt_id=receipt.id
            expected=100000-200-500-300-50-400+200-100 # independently calculated
            self.assertEqual(_account_balance(cash),expected)
            self.assertEqual(_account_balance(secondary),100)
            metrics=_aggregate_project_costs([pid])[pid]
            self.assertEqual({k:metrics[k] for k in ('labour','material','expense','subcontract')},
                             dict(labour=1250,material=400,expense=50,subcontract=400))
            for key,value in _accounts_reconciliation_findings().items():
                if isinstance(value,list):self.assertEqual(value,[],key)
        self.form('/hdc/accounts/cashflow/register',action='void_entry',entry_id=receipt_id,reason='QA void')
        with self.app.app_context():self.assertEqual(_account_balance(cash),expected-200)
        self.form('/hdc/accounts/cashflow/register',action='restore_entry',entry_id=receipt_id)
        with self.app.app_context():
            self.assertEqual(_account_balance(cash),expected)
            positions=day_positions(day)
            for pos in positions:
                # A perfect counted close must agree with independently reconciled ledger.
                save_counted_position(day,pos.account_id,pos.expected_closing_minor/100,actor='admin')
            lock_cash_day(day,actor='admin');db.session.commit()
            self.assertTrue(CashDayLock.query.first())
            unlock_cash_day(day);db.session.commit()
        self.form('/hdc/payroll/generate',date_from=ds,date_to=ds)
        with self.app.app_context():
            payroll=PayrollItem.query.filter_by(worker_id=wid).one()
            # Payroll net is period earnings minus advances; paid salary is separate.
            self.assertEqual(payroll.net_pay,1050)
            runid=payroll.run_id
        self.form('/hdc/payroll/generate',action='pay_all',run_id=runid)
        with self.app.app_context():
            self.assertEqual(_worker_payable_snapshot(wid)['balance'],0)
            self.assertEqual(_account_balance(cash),expected-550)
            count=LabourLedger.query.filter_by(worker_id=wid,entry_type='payment').count()
        self.form('/hdc/payroll/generate',action='pay_worker',run_id=runid,worker_id=wid)
        with self.app.app_context():
            self.assertEqual(LabourLedger.query.filter_by(worker_id=wid,entry_type='payment').count(),count)
        for url in (f'/hdc/projects/{pid}', f'/hdc/workers/{wid}/ledger', '/hdc/payroll',
                    '/hdc/accounts/cashflow','/hdc/accounts/reconciliation',
                    f'/hdc/reports?project_id={pid}',f'/hdc/reports?section=worker&worker_worker_id={wid}'):
            self.assertEqual(self.client.get(url).status_code,200,url)
        rows=list(csv.DictReader(io.StringIO(self.client.get('/hdc/reports/export/profitability').get_data(as_text=True))))
        row=next(r for r in rows if r['Code']=='QA-E2E')
        self.assertEqual(float(row['Total Cost']),2100)
        # Stage contract (10,000) supersedes the project fallback contract.
        self.assertEqual(float(row['Contract Value']),10000)
        self.assertEqual(float(row['Net Profit']),7900)
        wages=list(csv.DictReader(io.StringIO(self.client.get('/hdc/reports/export/salary').get_data(as_text=True))))
        self.assertEqual(float(wages[0]['Wage']),1250)
        with self.app.app_context():
            from sqlalchemy import text
            self.assertEqual(db.session.execute(text('PRAGMA integrity_check')).scalar(),'ok')
            self.assertEqual(db.session.execute(text('PRAGMA foreign_key_check')).all(),[])
