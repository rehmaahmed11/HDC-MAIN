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
        """The page is an admin list like every other one in the app.

        Two things are pinned here.  The page chrome: it uses the shared page
        head and the standard full-width `hdc-card` / `hdc-table` shell, not a
        page-specific centred "workspace" with a hero tile and a quick-add bar
        — that shell was the only one of its kind left in the sidebar.  And the
        row shape: a read-only name, its rename field revealed only while the
        row is being edited, and both row actions grouped in one Actions cell
        (they used to sit a third of a screen apart, one in the name cell).
        """
        response = self.client.get('/hdc/expense_categories')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        # shared page head (title, row count, the one Add action)
        self.assertIn('d-flex justify-content-between align-items-center mt-2 mb-3', html)
        self.assertIn('Expense Categories (', html)
        self.assertIn('addExpenseCategoryModal', html)
        # standard table card + shared row-rename contract
        self.assertIn('card hdc-card', html)
        self.assertIn('table hdc-table', html)
        self.assertIn('data-hdc-rename-table', html)
        self.assertIn('/hdc_static/js/core/row_rename.js', html)
        # the page-specific workspace chrome is retired for good
        for retired in ('expense-category-page', 'expense-category-hero',
                        'expense-category-quick-add', 'expense-category-idx'):
            self.assertNotIn(retired, html)

        with self.app.app_context():
            self.assertIsNotNone(ExpenseCategory.query.filter_by(active_status=True).first())
        self.form('/hdc/expense_categories', action='add_category', category_name='Row Contract')
        html = self.client.get('/hdc/expense_categories').get_data(as_text=True)
        row = html.split('data-hdc-label="Row Contract"', 1)[1].split('</tr>', 1)[0]
        actions = row.split('hdc-rename-actions', 1)[1]
        self.assertIn('fa-pen', actions, 'the rename button belongs in the Actions cell')
        self.assertIn('fa-trash', actions, 'the delete button belongs in the Actions cell')
        # one live input per row, not a text box behind every name
        self.assertEqual(row.count('<input type="text"'), 1)
        self.assertIn('hdc-rename-view', row)

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
        html = response.get_data(as_text=True)
        # same page shape and row contract as Expense Categories
        self.assertIn('d-flex justify-content-between align-items-center mt-2 mb-3', html)
        self.assertIn('data-hdc-rename-table', html)
        self.assertIn('addTradeModal', html)
        self.assertIn('/hdc_static/js/core/row_rename.js', html)
        self.assertNotIn('trades-quick-add', html)
        self.assertNotIn('trades-shell', html)

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
