"""Regression cases for destructive cross-module lifecycle paths."""
from sqlalchemy import text
from qa_support import IsolatedAppTest
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction, Expense, ExpenseCategory
from hdc.models.projects import Project, Stage
from hdc.models.tool_rental import Tool, ToolRental
from hdc.services.accounts import _account_balance
from hdc.services.tool_tracking import tool_ledger
from hdc.utils.dates import _pkt_today


class CrossModuleDeletionSafetyTest(IsolatedAppTest):
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            p = Project(project_code='SAFE-QA', name='Safety Project')
            db.session.add(p); db.session.flush()
            stage = Stage(project_id=p.id, name='Work stage')
            db.session.add(stage); db.session.flush()
            self.pid, self.sid = p.id, stage.id
            cash = Account.query.filter_by(name='Company Cash').one()
            cash.opening_balance = 1000
            self.cash = cash.id
            self.category = ExpenseCategory.query.filter_by(active_status=True).first().id
            db.session.commit()

    def _expense(self):
        self.form('/hdc/expenses', project_id=self.pid, stage_id=self.sid,
                  category_id=self.category, amount=100, date=_pkt_today().isoformat())
        with self.app.app_context():
            exp = Expense.query.filter_by(project_id=self.pid).one()
            tx = AccountTransaction.query.filter(AccountTransaction.source_type.like('expense%'),
                                                 AccountTransaction.source_id == exp.id).one()
            return exp.id, tx.id

    def _wipe(self, target):
        return self.client.post('/hdc/settings', data={'_csrf_token': self.token, 'action': 'wipe_data',
            'wipe_targets': target, 'wipe_confirm_text': 'WIPE', 'wipe_confirm_check': '1'}, follow_redirects=True)

    def test_stage_with_expense_and_account_history_is_not_deleted(self):
        eid, tid = self._expense()
        response = self.client.post(f'/hdc/stage/{self.sid}/delete', data={'_csrf_token': self.token}, follow_redirects=True)
        self.assertIn(b'linked history', response.data)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Stage, self.sid))
            self.assertEqual(db.session.get(Expense, eid).stage_id, self.sid)
            self.assertEqual(db.session.get(AccountTransaction, tid).stage_id, self.sid)
            self.assertEqual(db.session.execute(text('PRAGMA foreign_key_check')).all(), [])

    def test_empty_stage_can_still_be_deleted(self):
        self.form(f'/hdc/stage/{self.sid}/delete')
        with self.app.app_context(): self.assertIsNone(db.session.get(Stage, self.sid))

    def test_project_wipe_with_dependencies_is_refused_atomically(self):
        eid, tid = self._expense()
        response = self._wipe('projects')
        self.assertIn(b'Cannot wipe', response.data)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Project, self.pid))
            self.assertEqual(db.session.get(Expense, eid).project_id, self.pid)
            self.assertEqual(db.session.get(AccountTransaction, tid).stage_id, self.sid)
            self.assertEqual(db.session.execute(text('PRAGMA foreign_key_check')).all(), [])

    def test_expense_wipe_with_account_history_is_refused_atomically(self):
        eid, tid = self._expense()
        response = self._wipe('expenses')
        self.assertIn(b'Cannot wipe', response.data)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Expense, eid))
            self.assertFalse(db.session.get(AccountTransaction, tid).is_void)
            self.assertEqual(_account_balance(self.cash), 900)

    def test_empty_expense_wipe_is_allowed(self):
        response = self._wipe('expenses')
        self.assertIn(b'Wipe completed', response.data)

    def test_empty_project_and_stages_can_be_wiped(self):
        response = self._wipe('projects')
        self.assertIn(b'Wipe completed', response.data)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Project, self.pid))
            self.assertEqual(db.session.execute(text('PRAGMA foreign_key_check')).all(), [])

    def test_rental_restores_when_original_stock_is_available(self):
        with self.app.app_context():
            tool = Tool(tool_code='SAFE-RESTORE', name='Restorable tool', total_quantity=2, unit='pcs', is_void=False)
            db.session.add(tool); db.session.commit(); tool_id = tool.id
        self.form('/hdc/tool-rental/create', txn_type='new_rental', renter_type='external',
                  customer_name='Restorable holder', billing_type='no_charge', rental_date=_pkt_today().isoformat(),
                  **{'tool_id[]': tool_id, 'qty[]': '2', 'rate[]': '0'})
        with self.app.app_context(): rid = ToolRental.query.filter_by(customer_name='Restorable holder').one().id
        self.form(f'/hdc/tool-rental/{rid}/void', void_reason='Cancelled')
        self.form(f'/hdc/tool-rental/{rid}/restore')
        with self.app.app_context():
            self.assertFalse(db.session.get(ToolRental, rid).is_void)
            self.assertTrue(tool_ledger()['totals']['reconciled'])

    def test_voided_account_history_is_archived_on_both_routes(self):
        for route in ('/hdc/accounts', '/hdc/accounts/manage'):
            with self.subTest(route=route), self.app.app_context():
                account = Account(name='History ' + route, type='person')
                db.session.add(account); db.session.flush()
                tx = AccountTransaction(from_account_id=self.cash, to_account_id=account.id,
                        executed_by_account_id=self.cash, amount=30, type='party_payment',
                        category='expense', is_void=True)
                db.session.add(tx); db.session.commit(); aid, tid = account.id, tx.id
                self.form(route, action='delete_account', account_id=aid)
                db.session.expire_all()
                self.assertTrue(db.session.get(Account, aid).is_void)
                self.assertEqual(db.session.get(AccountTransaction, tid).to_account_id, aid)
                self.form('/hdc/accounts', action='restore_transaction', transaction_id=tid)
                db.session.expire_all()
                self.assertTrue(db.session.get(AccountTransaction, tid).is_void)
                self.assertEqual(_account_balance(self.cash), 1000)

    def test_legacy_transaction_missing_party_cannot_be_restored(self):
        with self.app.app_context():
            tx = AccountTransaction(from_account_id=self.cash, executed_by_account_id=self.cash,
                        amount=30, type='party_payment', category='expense', is_void=True)
            db.session.add(tx); db.session.commit(); tid = tx.id
            self.form('/hdc/accounts', action='restore_transaction', transaction_id=tid)
            db.session.expire_all()
            self.assertTrue(db.session.get(AccountTransaction, tid).is_void)
            self.assertEqual(_account_balance(self.cash), 1000)

    def test_rental_restore_fails_after_stock_is_reissued(self):
        with self.app.app_context():
            tool = Tool(tool_code='SAFE-TOOL', name='Safety tool', total_quantity=5, unit='pcs', is_void=False)
            db.session.add(tool); db.session.commit(); tool_id=tool.id
        def issue(name):
            self.form('/hdc/tool-rental/create', txn_type='new_rental', renter_type='external',
                      customer_name=name, billing_type='no_charge', rental_date=_pkt_today().isoformat(),
                      **{'tool_id[]': tool_id, 'qty[]': '5', 'rate[]': '0'})
            with self.app.app_context(): return ToolRental.query.filter_by(customer_name=name).one().id
        first = issue('First holder')
        self.form(f'/hdc/tool-rental/{first}/void', void_reason='Cancelled')
        issue('Second holder')
        response = self.client.post(f'/hdc/tool-rental/{first}/restore', data={'_csrf_token': self.token}, follow_redirects=True)
        self.assertIn(b'insufficient available stock', response.data)
        with self.app.app_context():
            self.assertTrue(db.session.get(ToolRental, first).is_void)
            ledger = tool_ledger()
            row = next(r for r in ledger['tools'] if r['tool_id'] == tool_id)
            self.assertEqual((row['owned_qty'], row['out_qty']), (5, 5))
            self.assertTrue(ledger['totals']['reconciled'])
