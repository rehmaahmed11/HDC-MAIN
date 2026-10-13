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

    def test_expense_categories_rows_group_their_actions(self):
        """The page is an admin list: name on the left, its buttons on the right.

        The layout contract this locks down is the one that was wrong before:
        a live text box in every row pushed the Save (tick) button ~330px away
        from the row's Delete (trash) button, across a 136px Actions column
        that held a single icon.  Both row actions now live in the Actions
        cell, and the rename field only appears while a row is being edited.
        """
        response = self.client.get('/hdc/expense_categories')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('expense-category-page', html)
        self.assertIn('expense-category-edit-form', html)
        # the add form is a one-line labelled quick-add, not a wide card stub
        self.assertIn('expense-category-quick-add', html)
        self.assertIn('expense-category-new-name', html)

        with self.app.app_context():
            self.assertIsNotNone(ExpenseCategory.query.filter_by(active_status=True).first())
        self.form('/hdc/expense_categories', action='add_category', category_name='Row Contract')
        html = self.client.get('/hdc/expense_categories').get_data(as_text=True)
        row = html.split('data-hdc-label="Row Contract"', 1)[1].split('</tr>', 1)[0]
        actions = row.split('expense-category-row-actions', 1)[1]
        self.assertIn('fa-pen', actions, 'the rename button belongs in the Actions cell')
        self.assertIn('fa-trash', actions, 'the delete button belongs in the Actions cell')
        # one live input per row, not a text box behind every name
        self.assertEqual(row.count('<input type="text"'), 1)
        self.assertIn('expense-category-name', row)

    def test_expense_categories_rename_round_trip(self):
        self.form('/hdc/expense_categories', action='add_category', category_name='Rename Me')
        with self.app.app_context():
            cat = ExpenseCategory.query.filter_by(name='Rename Me').one()
            cat_id = cat.id
        self.form('/hdc/expense_categories', action='edit_category',
                  category_id=cat_id, category_name='Renamed')
        with self.app.app_context():
            self.assertIsNone(ExpenseCategory.query.filter_by(name='Rename Me').first())
            self.assertIsNotNone(ExpenseCategory.query.filter_by(name='Renamed').first())
        self.form('/hdc/expense_categories', action='delete_category', category_id=cat_id)
        with self.app.app_context():
            self.assertFalse(ExpenseCategory.query.get(cat_id).active_status)


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
