"""Batch site-expense entry and the compact Trades workspace.

The Record Expenses form must accept several expense lines (food, petrol, ...)
for one site in a single submission, while legacy single-field payloads keep
working. The Trades page keeps its add/rename/delete contract.
"""
from qa_support import IsolatedAppTest
from werkzeug.datastructures import MultiDict
from hdc.models.accounts import Account, Expense, ExpenseCategory
from hdc.models.projects import Project, Stage
from hdc.models.workforce import WorkerTrade
from hdc.utils.dates import _pkt_today


class ExpenseBatchTest(IsolatedAppTest):
    def _setup_site(self):
        ds = _pkt_today().isoformat()
        # Expense posting goes through unified accounts; fund the cash pot first.
        with self.app.app_context():
            cash = Account.query.filter_by(name='Company Cash').one().id
        self.api(f'/api/accounts/account/{cash}', {'opening_balance': 100000}, 'PUT')
        self.form('/hdc/expense_categories', action='add_category', category_name='Food')
        self.form('/hdc/expense_categories', action='add_category', category_name='Petrol')
        self.form('/hdc/projects/add', project_code='QA-BATCH', name='Batch Site',
                  client='QA Owner', lump_sum=20000, start_date=ds)
        with self.app.app_context():
            pid = Project.query.filter_by(project_code='QA-BATCH').one().id
        self.form(f'/hdc/projects/{pid}/stage/add', name='Structure',
                  contract_basis='Lump Sum', lump_sum_value=10000, status='Active')
        with self.app.app_context():
            sid = Stage.query.filter_by(project_id=pid).one().id
            food = ExpenseCategory.query.filter_by(name='Food').one().id
            petrol = ExpenseCategory.query.filter_by(name='Petrol').one().id
        return ds, pid, sid, food, petrol

    def _post_batch(self, pairs):
        data = MultiDict(list(pairs) + [('_csrf_token', self.token)])
        response = self.client.post('/hdc/expenses', data=data)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:300])
        return response

    def test_batch_records_many_expenses_in_one_post(self):
        ds, pid, sid, food, petrol = self._setup_site()
        self._post_batch([
            ('project_id', str(pid)), ('stage_id', str(sid)), ('date', ds),
            ('item_category_id', str(food)), ('item_amount', '500'), ('item_remarks', 'Lunch'),
            ('item_category_id', str(petrol)), ('item_amount', '1,200'), ('item_remarks', 'Site fuel'),
            ('item_category_id', ''), ('item_amount', ''), ('item_remarks', ''),  # blank line ignored
        ])
        with self.app.app_context():
            rows = Expense.query.filter_by(project_id=pid, is_void=False).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual({r.amount for r in rows}, {500.0, 1200.0})
            self.assertEqual({r.remarks for r in rows}, {'Lunch', 'Site fuel'})
            self.assertEqual({r.category for r in rows}, {'Food', 'Petrol'})

    def test_invalid_line_rejects_the_whole_batch(self):
        ds, pid, sid, food, petrol = self._setup_site()
        self._post_batch([
            ('project_id', str(pid)), ('stage_id', str(sid)), ('date', ds),
            ('item_category_id', str(food)), ('item_amount', '500'), ('item_remarks', 'Lunch'),
            ('item_category_id', ''), ('item_amount', '300'), ('item_remarks', 'No category'),
        ])
        with self.app.app_context():
            self.assertEqual(Expense.query.filter_by(project_id=pid, is_void=False).count(), 0)

    def test_all_blank_lines_are_rejected(self):
        ds, pid, sid, food, petrol = self._setup_site()
        self._post_batch([
            ('project_id', str(pid)), ('stage_id', str(sid)), ('date', ds),
            ('item_category_id', ''), ('item_amount', ''), ('item_remarks', ''),
        ])
        with self.app.app_context():
            self.assertEqual(Expense.query.filter_by(project_id=pid, is_void=False).count(), 0)

    def test_legacy_single_expense_payload_still_works(self):
        ds, pid, sid, food, petrol = self._setup_site()
        self.form('/hdc/expenses', project_id=pid, stage_id=sid, date=ds,
                  category_id=food, amount=50, remarks='single legacy row')
        with self.app.app_context():
            row = Expense.query.filter_by(project_id=pid, is_void=False).one()
            self.assertEqual(row.amount, 50.0)
            self.assertEqual(row.remarks, 'single legacy row')

    def test_resubmitted_batch_is_skipped_as_duplicate(self):
        ds, pid, sid, food, petrol = self._setup_site()
        batch = [
            ('project_id', str(pid)), ('stage_id', str(sid)), ('date', ds),
            ('item_category_id', str(food)), ('item_amount', '500'), ('item_remarks', 'Lunch'),
        ]
        self._post_batch(batch)
        self._post_batch(batch)  # double-click / re-submit
        with self.app.app_context():
            self.assertEqual(Expense.query.filter_by(project_id=pid, is_void=False).count(), 1)

    def test_expenses_page_renders_batch_form(self):
        response = self.client.get('/hdc/expenses')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('item_category_id', html)
        self.assertIn('expItems', html)


class TradesPageTest(IsolatedAppTest):
    def test_trades_page_add_rename_delete_and_render(self):
        response = self.client.get('/hdc/trades')
        self.assertEqual(response.status_code, 200)
        self.assertIn('trades-quick-add', response.get_data(as_text=True))

        self.form('/hdc/trades', action='add_trade', trade_name='Welder')
        with self.app.app_context():
            trade = WorkerTrade.query.filter_by(name='Welder').one()
            tid = trade.id
            self.assertTrue(trade.active_status)

        self.form('/hdc/trades', action='edit_trade', trade_id=tid, trade_name='Senior Welder')
        with self.app.app_context():
            trade = WorkerTrade.query.get(tid)
            self.assertEqual(trade.name, 'Senior Welder')

        self.form('/hdc/trades', action='delete_trade', trade_id=tid)
        with self.app.app_context():
            trade = WorkerTrade.query.get(tid)
            self.assertFalse(trade.active_status)

        response = self.client.get('/hdc/trades')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('value="Senior Welder"', response.get_data(as_text=True))
