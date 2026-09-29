"""Regressions found by tests/e2e_full_app.py (full business walk-through)."""
from qa_support import IsolatedAppTest
from hdc.extensions import db
from hdc.models.accounts import Account, Expense, ExpenseCategory
from hdc.models.projects import Project, Stage
from hdc.models.subcontract import Subcontractor, SubcontractLabourWorker, SubcontractLabourPayment
from hdc.services.accounts import _account_balance
from hdc.utils.dates import _pkt_today


class E2ERegressionTest(IsolatedAppTest):
    def _scope(self):
        with self.app.app_context():
            cash = Account.query.filter_by(name='Company Cash').one().id
        self.api(f'/api/accounts/account/{cash}', {'opening_balance': 100000}, 'PUT')
        self.form('/hdc/projects/add', project_code='RG', name='Reg', client='C', lump_sum=1000)
        with self.app.app_context():
            pid = Project.query.filter_by(project_code='RG').one().id
        self.form(f'/hdc/projects/{pid}/stage/add', name='S', contract_basis='Lump Sum', lump_sum_value=1000)
        with self.app.app_context():
            sid = Stage.query.filter_by(project_id=pid).one().id
        return cash, pid, sid

    def test_expense_edit_and_void_move_the_cash_ledger(self):
        cash, pid, sid = self._scope()
        with self.app.app_context():
            cat = ExpenseCategory.query.filter_by(active_status=True).first().id
        ds = _pkt_today().isoformat()
        self.form('/hdc/expenses', project_id=pid, stage_id=sid, category_id=cat, amount=2000, date=ds)
        with self.app.app_context():
            eid = Expense.query.one().id
            self.assertEqual(_account_balance(cash), 98000)
        self.form(f'/hdc/expenses/{eid}/edit', project_id=pid, stage_id=sid, category_id=cat,
                  amount=2500, date=ds)
        with self.app.app_context():
            self.assertEqual(_account_balance(cash), 97500)
        self.form(f'/hdc/expenses/{eid}/delete')
        with self.app.app_context():
            self.assertEqual(_account_balance(cash), 100000)

    def test_subcontractor_labour_worker_can_be_paid(self):
        cash, pid, sid = self._scope()
        self.form(f'/hdc/projects/{pid}/add_subcontractor', stage_id=sid, name='Sub')
        with self.app.app_context():
            sub = Subcontractor.query.one().id
        self.form(f'/hdc/subcontractor/{sub}/workers', name='L1', trade='Labor', daily_wage=900)
        with self.app.app_context():
            w = SubcontractLabourWorker.query.one().id
        ds = _pkt_today().isoformat()
        self.form(f'/hdc/subcontractor/{sub}/labour_attendance', bulk_mode='1', date=ds, worker_ids=str(w),
                  **{f'attendance_status_{w}': 'present', f'project_id_{w}': pid,
                     f'stage_id_{w}': sid, f'working_hours_{w}': 8})
        self.form(f'/hdc/subcontractor/{sub}/workers/{w}/pay', date=ds, amount=900)
        with self.app.app_context():
            self.assertEqual(SubcontractLabourPayment.query.filter_by(is_void=False).count(), 1)
            self.assertEqual(_account_balance(cash), 99100)
