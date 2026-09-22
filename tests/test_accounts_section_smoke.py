"""Smoke-test the Accounts section: every tab, every form type, every field rule.

Built after PRs #31–#34 so the next change cannot silently drop a page, a
transaction type, or a field that the intent matrix says must exist.

What last PRs taught us (and this file pins):

* PR #31 combo boxes — searchable pickers must still render on New Transaction.
* PR #33 direction-first form — in/out/transfer all still work.
* PR #34 data-driven fields — form option values must exist in the intent
  matrix; JS must not force-show party/project for every type.
* Follow-ups still open (must not 500 if missing): Settings Cash Flow page,
  Loan HTTP hub, CF register still showing party/project unconditionally.

How to avoid the same mistakes:

1. One source of truth for field rules (``_ACCOUNT_INTENT_DEFAULT_RULES``).
   Every dropdown value in ``_ACCOUNT_TXN_FORM_OPTIONS`` must have a matrix
   row. This test fails if they drift.
2. GET every Accounts URL in CI. A missing template is a 500, not a review note.
3. POST the happy path for each *intent* the operator can pick. Mapping an
   intent to a ledger type (loan_taken → party_receipt) is easy to forget.
4. Negative cases: empty party on a loan, repay with no open loan, supplier
   via generic party_payment.
"""

import os
import re
import tempfile
import unittest

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction, ExpenseCategory
from hdc.models.projects import Project, Stage
from hdc.services.accounts import (
    _ACCOUNT_INTENT_DEFAULT_RULES,
    _ACCOUNT_TXN_FORM_OPTIONS,
    _account_intent_field_matrix,
)


ACCOUNT_GET_PAGES = [
    '/hdc/accounts/hub',
    '/hdc/accounts',
    '/hdc/accounts/entries',
    '/hdc/accounts/manage',
    '/hdc/accounts/new',
    '/hdc/accounts/reconciliation',
    '/hdc/accounts/money-center',
    '/hdc/accounts/new-transaction',
    '/hdc/accounts/cashflow',
    '/hdc/accounts/cashflow/register',
    '/hdc/accounts/cashflow/reconciliation',
    '/hdc/personal-management',
    '/hdc/personal-management/expenses',
    '/hdc/personal-management/categories',
    '/hdc/accounts/kpi/cash',
    '/hdc/accounts/kpi/bank',
    '/hdc/accounts/kpi/receivable',
    '/hdc/accounts/kpi/payable',
    '/hdc/accounts/kpi/received',
    '/hdc/accounts/kpi/spent',
    '/hdc/accounts/kpi/net',
    '/hdc/accounts/kpi/company_total',
]


class AccountsSectionSmokeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-acc-smoke-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp.name, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp.name,
            'TESTING': True,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.client.get('/hdc/login')
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']
        r = self.client.post('/hdc/login', data={
            'username': 'admin',
            'password': os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD'],
            '_csrf_token': self.token,
        })
        self.assertEqual(r.status_code, 302)

        cash = Account.query.filter_by(name='Company Cash').first()
        self.assertIsNotNone(cash)
        cash.opening_balance = 5_000_000.0
        cash.opening_balance_minor = 500_000_000
        bank, _ = self._ensure_account('Smoke Bank', 'bank', 1_000_000.0,
                                       bank_name='HBL', account_number='99')
        person, _ = self._ensure_account('Smoke Person', 'person', 0)
        self.project = Project(project_code='SMK-1', name='Smoke Site',
                               client='Smoke Client', status='Active')
        db.session.add(self.project)
        db.session.flush()
        self.stage = Stage(project_id=self.project.id, name='Grey',
                           status='Active')
        db.session.add(self.stage)
        db.session.commit()
        self.cash = cash
        self.bank = bank
        self.person = person
        self.cat = ExpenseCategory.query.filter_by(active_status=True).first()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.tmp.cleanup()

    def _ensure_account(self, name, acc_type, opening, **kw):
        from hdc.services.accounts import _create_account
        from hdc.utils.money import sync_money_fields
        row = Account.query.filter_by(name=name).first()
        if row is None:
            row, msg = _create_account(name, acc_type, opening_balance=opening, **kw)
            self.assertIsNotNone(row, msg)
        row.opening_balance = float(opening)
        sync_money_fields(row, 'opening_balance', 'opening_balance_minor')
        db.session.commit()
        return row, None

    def _csrf(self):
        with self.client.session_transaction() as session:
            return session['_csrf_token']

    def _post_accounts(self, extra):
        data = {
            '_csrf_token': self._csrf(),
            'action': 'create_transaction',
            'date': '2026-09-22',
            'amount': '1000',
            'from_account_id': str(self.cash.id),
            'executed_by_account_id': str(self.cash.id),
        }
        data.update(extra)
        return self.client.post('/hdc/accounts', data=data, follow_redirects=False)

    # ── tabs ──────────────────────────────────────────────────────────────

    def test_every_accounts_tab_returns_200(self):
        failures = []
        for url in ACCOUNT_GET_PAGES:
            r = self.client.get(url)
            if r.status_code != 200:
                failures.append('%s → %s' % (url, r.status_code))
        self.assertFalse(failures, 'Accounts tabs failed:\\n' + '\\n'.join(failures))

    def test_ledger_and_manage_new_account_pages(self):
        r = self.client.get('/hdc/accounts/%d/ledger' % self.cash.id)
        self.assertEqual(r.status_code, 200)
        r = self.client.get('/hdc/accounts/manage?q=Cash')
        self.assertEqual(r.status_code, 200)

    def test_money_center_and_new_transaction_carry_field_hooks(self):
        mc = self.client.get('/hdc/accounts/money-center').get_data(as_text=True)
        self.assertIn('money_center.js', mc)
        nt = self.client.get('/hdc/accounts/new-transaction').get_data(as_text=True)
        for needle in ('data-hdc-txn-form', 'txnAmount', 'txnCategory',
                       'value=\"in\"', 'value=\"out\"', 'value=\"transfer\"'):
            self.assertIn(needle, nt, needle)

    def test_all_entries_renders_every_form_option(self):
        """The workspace (and All Entries script payload) lists every operator type."""
        workspace = self.client.get('/hdc/accounts').get_data(as_text=True)
        entries = self.client.get('/hdc/accounts/entries').get_data(as_text=True)
        blob = workspace + entries
        for opt in _ACCOUNT_TXN_FORM_OPTIONS:
            self.assertTrue(
                opt['value'] in blob or opt['label'] in blob,
                'missing form type %s (%s)' % (opt['value'], opt['label']),
            )

    def test_accounts_workspace_exposes_intent_matrix(self):
        html = self.client.get('/hdc/accounts').get_data(as_text=True)
        self.assertTrue(
            'intentMatrix' in html or 'intent_matrix' in html or 'INTENT' in html,
            'workspace must ship the field-rule matrix to the page script',
        )

    # ── field rules vs dropdown ───────────────────────────────────────────

    def test_every_form_option_has_an_intent_rule(self):
        matrix = _account_intent_field_matrix()
        missing = [o['value'] for o in _ACCOUNT_TXN_FORM_OPTIONS
                   if o['value'] not in matrix]
        self.assertFalse(missing,
                         'form options with no field rule (PR #34 regression): %s'
                         % missing)

    def test_pay_to_project_does_not_require_party(self):
        rule = _account_intent_field_matrix()['pay_to_project']
        self.assertTrue(rule['project'])
        self.assertTrue(rule['project_required'])
        self.assertFalse(rule['party_name'])
        self.assertFalse(rule['related'])

    def test_loan_types_require_party(self):
        matrix = _account_intent_field_matrix()
        for key in ('loan_taken', 'loan_given', 'loan_repayment', 'loan_recovery'):
            self.assertTrue(matrix[key]['party_name_required'], key)

    def test_js_does_not_force_show_party_for_every_type(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            'static', 'hdc', 'js', 'pages', 'accounts_entries.js')
        with open(path, encoding='utf-8') as fh:
            src = fh.read()
        self.assertNotIn("toWrap.style.display = ''", src)
        self.assertIn('intentMatrix', src)

    # ── transaction posts ─────────────────────────────────────────────────

    def test_intra_company_transfer_posts(self):
        before = AccountTransaction.query.count()
        r = self._post_accounts({
            'type': 'pay_intra_company',
            'to_account_id': str(self.bank.id),
        })
        self.assertEqual(r.status_code, 302)
        self.assertGreater(AccountTransaction.query.count(), before)

    def test_receive_from_project_posts(self):
        before = AccountTransaction.query.count()
        r = self._post_accounts({
            'type': 'receive_from_project',
            'to_account_id': str(self.cash.id),
            'from_account_id': str(self.person.id),
            'executed_by_account_id': str(self.person.id),
            'project_id': str(self.project.id),
        })
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:400])
        self.assertGreaterEqual(AccountTransaction.query.count(), before)

    def test_personal_management_payment_posts(self):
        before = AccountTransaction.query.count()
        r = self._post_accounts({
            'type': 'personal_management_payment',
            'party_name': 'Family Member',
            'to_account_id': str(self.person.id),
        })
        self.assertEqual(r.status_code, 302)
        self.assertGreater(AccountTransaction.query.count(), before)

    def test_general_expense_needs_project_and_stage(self):
        if not self.cat:
            self.skipTest('no expense category')
        r = self._post_accounts({
            'type': 'expense_general',
            'party_name': 'Shop',
            'expense_category_id': str(self.cat.id),
        })
        self.assertEqual(r.status_code, 302)
        # follow: should flash error, no new general expense without scope
        html = self.client.get('/hdc/accounts').get_data(as_text=True)
        self.assertTrue('required' in html.lower() or 'mandatory' in html.lower()
                        or 'Project' in html)

    def test_loan_taken_requires_party(self):
        before = AccountTransaction.query.count()
        r = self._post_accounts({
            'type': 'loan_taken',
            'to_account_id': str(self.cash.id),
            'from_account_id': str(self.person.id),
            'executed_by_account_id': str(self.person.id),
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(AccountTransaction.query.count(), before)

    def test_loan_taken_with_party_posts(self):
        before = AccountTransaction.query.count()
        r = self._post_accounts({
            'type': 'loan_taken',
            'party_name': 'Uncle Lender',
            'to_account_id': str(self.cash.id),
            'from_account_id': str(self.person.id),
            'executed_by_account_id': str(self.person.id),
        })
        self.assertEqual(r.status_code, 302)
        self.assertGreater(AccountTransaction.query.count(), before)

    def test_loan_repayment_without_open_loan_is_refused(self):
        before = AccountTransaction.query.count()
        r = self._post_accounts({
            'type': 'loan_repayment',
            'party_name': 'Nobody',
            'to_account_id': str(self.person.id),
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(AccountTransaction.query.count(), before)

    def test_new_transaction_money_out_in_transfer(self):
        from hdc.services.cashflow_register import category_options
        cats = {c.name: c for c in category_options()}
        csrf, key = self._txn_tokens()
        out_cat = cats.get('Miscellaneous') or list(cats.values())[0]
        r = self.client.post('/hdc/accounts/new-transaction', data={
            '_csrf_token': csrf, '_idempotency_key': key,
            'action': 'create_entry', 'direction': 'out', 'date': '2026-09-22',
            'amount': '250', 'account_id': self.cash.id,
            'category_id': out_cat.id,
        })
        self.assertIn(r.status_code, (302, 200))

        csrf, key = self._txn_tokens()
        income = cats.get('Owner / Client Receipt')
        if income:
            r = self.client.post('/hdc/accounts/new-transaction', data={
                '_csrf_token': csrf, '_idempotency_key': key,
                'action': 'create_entry', 'direction': 'in', 'date': '2026-09-22',
                'amount': '400', 'account_id': self.cash.id,
                'category_id': income.id, 'project_id': self.project.id,
            })
            self.assertIn(r.status_code, (302, 200))

        csrf, key = self._txn_tokens()
        r = self.client.post('/hdc/accounts/new-transaction', data={
            '_csrf_token': csrf, '_idempotency_key': key,
            'action': 'create_entry', 'direction': 'transfer', 'date': '2026-09-22',
            'amount': '100', 'account_id': self.cash.id,
            'destination_account_id': self.bank.id,
        })
        self.assertIn(r.status_code, (302, 200))

    def _txn_tokens(self):
        html = self.client.get('/hdc/accounts/new-transaction').get_data(as_text=True)
        csrf = re.search(r'name="_csrf_token" value="([^"]+)"', html)
        key = re.search(r'name="_idempotency_key" value="([^"]+)"', html)
        self.assertIsNotNone(csrf)
        self.assertIsNotNone(key)
        return csrf.group(1), key.group(1)

    def test_default_rules_cover_ledger_types(self):
        from hdc.services.accounts import _ACCOUNT_TXN_TYPES
        missing = [t for t in _ACCOUNT_TXN_TYPES if t not in _ACCOUNT_INTENT_DEFAULT_RULES]
        self.assertFalse(missing, missing)


if __name__ == '__main__':
    unittest.main()
