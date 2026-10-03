"""Record Money (Money Center) coverage: the feeds, the page and posting.

The page used to carry its own Step-2 grid of one tile per transaction type,
with a script that loaded four dropdown feeds.  It now renders the shared New
Transaction form (the same partial as /hdc/accounts/new-transaction and the CF
register) and posts it through the same engine, so these tests pin *that*
contract: one form, one posting path, and the module links that settle the
balances the entry form does not own.
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
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.cashflow import CashFlowCategory, CashFlowEntry, CashFlowParty
from hdc.models.materials import Supplier, SupplierLedger
from hdc.models.office import OfficeStaff
from hdc.models.projects import Project
from hdc.models.subcontract import Subcontractor
from hdc.models.workforce import Worker


class MoneyCenterOptionsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-money-center-test-')
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
            token = session['_csrf_token']
        response = self.client.post('/hdc/login', data={
            'username': 'admin',
            'password': os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD'],
            '_csrf_token': token,
        })
        self.assertEqual(response.status_code, 302)
        self.rows = {
            'workers': Worker(worker_code='MC-W1', name='Options Worker'),
            'suppliers': Supplier(name='Options Supplier'),
            'subcontractors': Subcontractor(subcontractor_code='MC-S1', name='Options Contractor'),
            'office_staff': OfficeStaff(staff_code='MC-O1', name='Options Staff'),
        }
        db.session.add_all(self.rows.values())
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.tmp.cleanup()

    def _feed(self, family):
        response = self.client.get('/hdc/api/' + family)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        payload = response.get_json()
        self.assertTrue(payload['ok'], payload)
        self.assertIsInstance(payload['items'], list)
        return payload

    def _page(self):
        response = self.client.get('/hdc/accounts/money-center')
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_all_four_feeds_return_seeded_options(self):
        for family, row in self.rows.items():
            with self.subTest(family=family):
                items = self._feed(family)['items']
                item = next(item for item in items if item['id'] == row.id)
                self.assertIn(row.name, item['label'])

    def test_inactive_workers_staff_and_void_suppliers_are_excluded(self):
        self.rows['workers'].active_status = False
        self.rows['office_staff'].active_status = False
        self.rows['suppliers'].is_void = True
        db.session.commit()
        for family in ('workers', 'office_staff', 'suppliers'):
            with self.subTest(family=family):
                self.assertEqual(self._feed(family)['items'], [])

    # ── the page: one form, one posting path ────────────────────────────────

    def _form_tokens(self):
        html = self._page()
        csrf = re.search(r'name="_csrf_token" value="([^"]+)"', html)
        key = re.search(r'name="_idempotency_key" value="([^"]+)"', html)
        self.assertIsNotNone(csrf, 'the shared form must carry a CSRF token')
        self.assertIsNotNone(key, 'the shared form must carry an idempotency key')
        return {'_csrf_token': csrf.group(1), '_idempotency_key': key.group(1)}

    def test_page_renders_the_shared_entry_form(self):
        """Record Money is the form, not a menu of little forms."""
        html = self._page()
        for needle in ('data-hdc-txn-form', 'id="hdcTxnForm"', 'id="txnDirection"',
                       'id="txnDate"', 'id="txnAmount"', 'id="txnAccount"',
                       'id="txnCategory"', 'id="txnSubcategory"', 'id="txnParty"',
                       'id="txnProject"', 'txnNewAccountModal', 'txnNewPartyModal',
                       'txnNewProjectModal', 'name="action" value="create_entry"'):
            self.assertIn(needle, html, needle)
        self.assertIn('/hdc_static/js/pages/new_transaction.js', html)
        self.assertIn('/hdc_static/css/new_transaction.css', html)

    def test_the_per_type_tile_picker_is_gone(self):
        """One tile + one mini-form per transaction type was the clutter."""
        html = self._page()
        for gone in ('type-btn', 'mcSelectDirection', 'mcQuickSelectType',
                     'typeConfigByIntent', 'money_center.js', 'mc_from_account',
                     'mc_type_grid', 'step-dot'):
            self.assertNotIn(gone, html, gone)

    def test_the_page_does_not_talk_to_developers(self):
        html = self._page()
        for gone in ('unified ledger', 'How It Works', 'intent_matrix',
                     'hdc_account_txn', 'Posting:', 'overdraft block',
                     'void sync', 'source_type'):
            self.assertNotIn(gone, html, gone)

    def test_every_entry_surface_renders_the_same_partial(self):
        for url in ('/hdc/accounts/money-center', '/hdc/accounts/new-transaction',
                    '/hdc/accounts/cashflow/register'):
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200, url)
                self.assertIn('data-hdc-txn-form', page.get_data(as_text=True), url)

    def test_a_deep_link_prefills_the_one_form(self):
        category = CashFlowCategory.query.filter_by(name='Labour & Wages').first()
        self.assertIsNotNone(category, 'the register seeds its default categories')
        db.session.add(CashFlowParty(name='Prefill Worker', party_type='worker'))
        db.session.commit()

        html = self.client.get(
            '/hdc/accounts/money-center'
            '?direction=out&category=Labour+%26+Wages&amount=1250.50&party=Prefill+Worker'
        ).get_data(as_text=True)

        self.assertRegex(html, r'<option value="out" selected')
        self.assertRegex(html, r'<option[^>]*value="%d"[^>]*\bselected\b' % category.id)
        self.assertIn('value="1250.50"', html)
        self.assertRegex(html, r'<option[^>]*value="Prefill Worker"[^>]*\bselected\b')

    def test_a_deep_link_that_no_longer_resolves_opens_a_clean_form(self):
        html = self.client.get(
            '/hdc/accounts/money-center'
            '?direction=bogus&category=Nope&project_id=999999&amount=abc&date=31-13-2026'
        ).get_data(as_text=True)
        self.assertIn('data-hdc-txn-form', html)
        direction_select = html.split('id="txnDirection"', 1)[1].split('</select>', 1)[0]
        self.assertNotIn('selected', direction_select)

    def test_posting_the_form_records_exactly_one_entry(self):
        account = Account.query.filter_by(name='Company Cash').first()
        account.opening_balance = 100000.0
        account.opening_balance_minor = 10000000
        category = CashFlowCategory.query.filter_by(name='Miscellaneous').first()
        db.session.commit()

        before = CashFlowEntry.query.count()
        payload = {
            'action': 'create_entry', 'direction': 'out', 'date': '2026-02-01',
            'amount': '250', 'account_id': account.id, 'category_id': category.id,
            'description': 'posted through Record Money',
        }
        payload.update(self._form_tokens())
        response = self.client.post('/hdc/accounts/money-center', data=payload)
        self.assertEqual(response.status_code, 302)

        entry = CashFlowEntry.query.filter_by(
            description='posted through Record Money').one()
        self.assertEqual(entry.direction, 'out')
        self.assertAlmostEqual(float(entry.amount), 250.0, places=2)
        self.assertIsNotNone(entry.account_tx_id)

        # The same idempotency key means a double-click cannot post twice.
        self.client.post('/hdc/accounts/money-center', data=payload)
        self.assertEqual(CashFlowEntry.query.count(), before + 1)

    def test_open_balances_link_to_the_page_that_settles_them(self):
        """A pending row must point at the entry that clears it — a supplier
        payable at the supplier's own page, a project receipt back here."""
        supplier = self.rows['suppliers']
        db.session.add(SupplierLedger(supplier_id=supplier.id, entry_type='debit',
                                      amount=4500.0, reference_type='purchase',
                                      note='probe payable'))
        project = Project(project_code='MC-P1', name='Receivable Site',
                          client='Receivable Client', status='Active',
                          contract_type='lump_sum', owner_lump_sum=900000.0)
        db.session.add(project)
        db.session.commit()

        html = self._page()
        self.assertIn('/hdc/purchase-v2/suppliers/%d' % supplier.id, html)
        self.assertIn('project_id=%d' % project.id, html)
        self.assertIn('#record-money-form', html)
        self.assertNotIn('mcPayEntity', html)


class MoneyCenterRegressionTestCase(unittest.TestCase):
    """The regression tests the audit asked for in Step 10.

    Each one reproduces a shipped bug so it can never come back:
      10.2 every MONEY_FLOWS route builds (Step 1 — the 500 page)
      10.3 a supplier payment reduces the payable (Step 2)
      10.4 voiding from All Entries voids the CF document (Step 3)
      10.5 that void records reason/user/time (Step 4)
      10.6 quick-post cannot silently skip a supplier payable (Step 15.3)
      10.7/10.8 the Money Center renders and its feeds answer
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-money-regress-')
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
        self.client.post('/hdc/login', data={
            'username': 'admin',
            'password': os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD'],
            '_csrf_token': self.token,
        })
        cash = Account.query.filter(Account.name == 'Company Cash').first()
        self.assertIsNotNone(cash, 'bootstrap must seed Company Cash')
        cash.opening_balance = 1000000.0
        cash.opening_balance_minor = 100000000
        counterparty = Account(name='Regression Party', type='credit_debit')
        self.supplier = Supplier(name='Regression Supplier')
        db.session.add_all([counterparty, self.supplier])
        db.session.commit()
        self.cash = cash
        self.counterparty = counterparty
        self.cash_id = int(cash.id)
        self.counterparty_id = int(counterparty.id)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.tmp.cleanup()

    # ---- 10.2 -----------------------------------------------------------
    def test_every_money_flow_route_builds(self):
        """A typo in a flow's route used to 500 the whole page (audit 4.1)."""
        from flask import url_for
        from hdc.services.money_hub import get_all_money_flows
        flows = get_all_money_flows()
        self.assertTrue(flows, 'the money flow inventory must not be empty')
        with self.app.test_request_context('/'):
            for flow in flows:
                route = (flow.get('route') or '')
                if not route.startswith('hdc_'):
                    continue
                with self.subTest(flow=flow.get('id'), route=route):
                    url_for(route)      # raises BuildError on regression
        # and the page itself must render for a real user
        self.assertEqual(self.client.get('/hdc/accounts/money-center').status_code, 200)

    # ---- 10.3 -----------------------------------------------------------
    def test_supplier_payment_posts_and_reduces_the_payable(self):
        """Step 2: 'Record payment' used to crash (Order_date) and lose money."""
        from hdc.models.materials import SupplierLedger
        from hdc.services.accounts import _account_pending_snapshot

        db.session.add(SupplierLedger(
            supplier_id=self.supplier.id, entry_type='debit', amount=120000.0,
            reference_type='purchase', note='regression purchase'))
        db.session.commit()
        self.assertAlmostEqual(
            float(_account_pending_snapshot('purchase', 'supplier', self.supplier.id)['pending']),
            120000.0, places=2)

        response = self.client.post(
            f'/hdc/purchase-v2/suppliers/{self.supplier.id}/payment',
            data={'_csrf_token': self.token, 'amount': '30000', 'entry_kind': 'payment',
                  'note': 'regression payment'})
        self.assertEqual(response.status_code, 302)

        credits = SupplierLedger.query.filter_by(
            supplier_id=self.supplier.id, entry_type='credit').all()
        self.assertEqual(len(credits), 1, 'the payment must be written to the supplier ledger')
        self.assertAlmostEqual(float(credits[0].amount), 30000.0, places=2)

        # ...and the payable is now smaller, so the supplier cannot be paid twice.
        self.assertAlmostEqual(
            float(_account_pending_snapshot('purchase', 'supplier', self.supplier.id)['pending']),
            90000.0, places=2)

    # ---- 10.4 + 10.5 ----------------------------------------------------
    def test_void_from_all_entries_voids_the_cash_flow_entry(self):
        """Step 3: the register stayed active while the ledger row was voided."""
        from hdc.models.cashflow import CashFlowEntry
        from hdc.services.cashflow_register import (
            category_options, save_manual_cash_flow_entry)

        categories = category_options('out')
        self.assertTrue(categories, 'the register needs at least one category')
        entry, _ = save_manual_cash_flow_entry(
            direction='out', amount=5000.0,
            account_id=self.cash.id, destination_account_id=self.counterparty.id,
            category_id=int(categories[0].id), description='regression out')
        db.session.commit()
        txn_id = int(entry.account_tx_id)
        self.assertFalse(entry.is_void)

        # The Void button lives on the All Entries page but posts to
        # /hdc/accounts, which is where the void handler is registered.
        response = self.client.post('/hdc/accounts', data={
            '_csrf_token': self.token, 'action': 'void_transaction',
            'transaction_id': txn_id, 'void_reason': 'regression void'})
        self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        entry = db.session.get(CashFlowEntry, entry.id)
        txn = db.session.get(AccountTransaction, txn_id)
        self.assertTrue(txn.is_void, 'the ledger row must be voided')
        self.assertTrue(entry.is_void,
                        'the Cash Flow document must follow the ledger row (audit 5.2)')
        # 10.5 — the audit trail is persisted, not just asked for.
        self.assertEqual(txn.void_reason, 'regression void')
        self.assertTrue((txn.voided_by or '').strip())
        self.assertIsNotNone(txn.voided_at)
        self.assertEqual(entry.void_reason, 'regression void')

    # ---- 10.6 -----------------------------------------------------------
    def test_quick_post_party_payment_with_supplier_is_refused(self):
        """Decision 15.3: never record one-sided money that skips the payable."""
        response = self.client.post(
            '/hdc/accounts/money-center/api/quick-post',
            json={
                'date': '2026-02-03', 'type': 'party_payment', 'amount': 7000,
                'from_account_id': self.cash.id, 'to_account_id': self.counterparty.id,
                'related_entity_type': 'supplier', 'related_entity_id': self.supplier.id,
            },
            headers={'X-CSRFToken': self.token})
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertFalse(payload['ok'])
        self.assertIn('payable', payload['message'].lower())
        self.assertEqual(AccountTransaction.query.count(), 0,
                         'a refused quick-post must not write anything')

    # ---- 10.7 -----------------------------------------------------------
    def test_staff_cannot_post_money_from_the_money_center(self):
        from hdc.models.auth import HDCUser
        staff = HDCUser(username='regress-staff', role='staff', password_hash='x')
        db.session.add(staff)
        db.session.commit()

        # A *fresh* client, and a fresh app context for the request:
        # Flask-Login caches the resolved user on ``g``, which lives on the
        # app context this test case keeps pushed, so reusing either would
        # silently keep authenticating as the admin who logged in above.
        token = 'money-center-staff-csrf'
        staff_id = int(staff.id)
        self.ctx.pop()
        try:
            staff_client = self.app.test_client()
            with staff_client.session_transaction() as session:
                session.clear()
                session['_csrf_token'] = token
                session['_user_id'] = str(staff_id)
                session['_fresh'] = True
            response = staff_client.post(
                '/hdc/accounts/money-center/api/quick-post',
                json={'date': '2026-02-04', 'type': 'party_payment', 'amount': 100,
                      'from_account_id': self.cash_id,
                      'to_account_id': self.counterparty_id},
                headers={'X-CSRFToken': token})
        finally:
            self.ctx.push()
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
        self.assertEqual(AccountTransaction.query.count(), 0,
                         'a denied staff post must not write anything')

    # ---- 10.8 -----------------------------------------------------------
    def test_money_center_feeds_and_assets_are_served(self):
        """The page's dropdown feeds and its extracted JS/CSS must all answer 200."""
        for url in ('/hdc/api/workers', '/hdc/api/suppliers',
                    '/hdc/api/subcontractors', '/hdc/api/office_staff',
                    '/hdc_static/js/pages/new_transaction.js',
                    '/hdc_static/css/money_center.css'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
