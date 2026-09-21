"""The New Transaction form: dependency, add-on-the-fly, validation, no data loss.

The form is a *front end* to the Cash Flow register engine
(``hdc/services/cashflow_register.py``).  These tests pin the behaviours the
redesign promises, and — just as importantly — that the engine underneath is
still the only thing that decides what gets posted.

Covered:

* the focused page renders, and the register renders the *same* form partial;
* category → subcategory is the database's dependency, per direction, and a
  stale pair is rejected server-side rather than silently dropped;
* Account / Party / Project are searchable combos that can create what is
  missing, and the create endpoints reuse the existing models and de-duplicate;
* every route of validation (empty / zero / negative / garbage amount, missing
  account, same From and To, wrong-direction category, unknown ids) cannot save;
* a rejected POST keeps what the user typed, so the form is never wiped;
* a double submission cannot post the money twice.
"""

import html as _html
import os
import re
import shutil
import subprocess
import tempfile
import unittest

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from flask import g

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.cashflow import CashFlowEntry, CashFlowParty, CashFlowSubcategory
from hdc.models.projects import Project
from hdc.services.cashflow_register import category_options, subcategory_options

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'new_transaction_harness.js')

NEW_TXN_URL = '/hdc/accounts/new-transaction'
REGISTER_URL = '/hdc/accounts/cashflow/register'
FORM_PARTIAL = 'templates/hdc/accounts/_new_transaction_form.html'
PAGE_SCRIPT = 'static/hdc/js/pages/new_transaction.js'


def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as handle:
        return handle.read()


class NewTransactionTestCase(unittest.TestCase):
    """A real database, a signed-in admin, and the seeded register vocabulary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-new-txn-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp.name, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp.name,
            'TESTING': True,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.actor = 'admin'
        self._login()

        self.cash = self._account('Company Cash', 'cash', 500000.0)
        self.drawer = self._account('Drawer Cash', 'cash', 100000.0)
        self.bank = self._account('MCB 1109', 'bank', 250000.0,
                                  bank_name='MCB', account_number='1109')
        self.project = Project(project_code='HDC-T-1', name='JPS KhanPur 5 Marla',
                               client='JPS', status='Active')
        db.session.add(self.project)
        from hdc.services.cashflow_register import save_cf_party
        self.party, _ = save_cf_party('Zubair Transport', party_type='supplier')
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _account(self, name, acc_type, opening=0.0, **kw):
        """Get-or-create a treasury account with a real opening balance."""
        from hdc.services.accounts import _create_account
        from hdc.utils.money import sync_money_fields

        row = Account.query.filter_by(name=name).first()
        if row is None:
            row, message = _create_account(name, acc_type, opening_balance=opening, **kw)
            self.assertIsNotNone(row, message)
        row.opening_balance = float(opening)
        sync_money_fields(row, 'opening_balance', 'opening_balance_minor')
        db.session.commit()
        return row

    def _login(self):
        self.client.get('/hdc/login')
        with self.client.session_transaction() as session:
            token = session['_csrf_token']
        response = self.client.post('/hdc/login', data={
            'username': 'admin',
            'password': os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD'],
            '_csrf_token': token,
        })
        self.assertEqual(response.status_code, 302)

    def _page_tokens(self, url=NEW_TXN_URL):
        html = self.client.get(url).get_data(as_text=True)
        csrf = re.search(r'name="_csrf_token" value="([^"]+)"', html)
        key = re.search(r'name="_idempotency_key" value="([^"]+)"', html)
        self.assertIsNotNone(csrf, 'the form must carry a CSRF token')
        self.assertIsNotNone(key, 'the form must carry an idempotency key')
        return csrf.group(1), key.group(1)

    def _submit(self, data, url=NEW_TXN_URL, follow=False, keep_key=None):
        csrf, key = self._page_tokens(url)
        payload = dict(data)
        payload.setdefault('action', 'create_entry')
        payload['_csrf_token'] = csrf
        payload['_idempotency_key'] = key if keep_key is None else keep_key
        return self.client.post(url, data=payload, follow_redirects=follow)

    def _error_text(self, response):
        html = response.get_data(as_text=True)
        match = re.search(r'<div class="hdc-txn-alert-msg">(.*?)</div>', html, re.S)
        return (match.group(1).strip() if match else '')

    def assertOptionSelected(self, html, value, msg=''):
        """The <option> for ``value`` is the selected one (attributes may wrap)."""
        pattern = r'<option[^>]*value="%s"[^>]*\bselected\b' % re.escape(str(value))
        if not re.search(pattern, html):
            self.fail('%s: option value=%r is not selected in the rendered page'
                      % (msg or 'missing selection', value))

    def assertHtmlContains(self, html, needle, msg=''):
        """assertIn with a readable failure (the page HTML is huge)."""
        if needle not in html:
            self.fail('%s: %r not found in the rendered page' % (msg or 'missing', needle))

    def _categories(self):
        return {c.name: c for c in category_options()}

    def _subcategories(self, name):
        return {s.name: s for s in subcategory_options(self._categories()[name].id)}

    def _json(self, url, payload):
        return self.client.post(url, json=payload,
                                headers={'X-CSRFToken': self._csrf_header()})

    def _csrf_header(self):
        with self.client.session_transaction() as session:
            return session['_csrf_token']

    # ── the page and the shared form ─────────────────────────────────────────

    def test_focused_page_renders_the_complete_form(self):
        html = self.client.get(NEW_TXN_URL).get_data(as_text=True)
        for needle in (
            'data-hdc-txn-form',
            'name="_idempotency_key"',
            # the three directions
            'value="in"', 'value="out"', 'value="transfer"',
            # simple controls
            'id="txnDate"', 'id="txnAmount"', 'id="txnReference"', 'id="txnNote"',
            # the searchable database pickers
            'id="txnAccountInput"', 'id="txnAccount"',
            'id="txnToAccountInput"', 'id="txnToAccount"',
            'id="txnPartyInput"', 'id="txnParty"',
            'id="txnProjectInput"', 'id="txnProject"',
            'id="txnCategory"', 'id="txnSubcategory"',
            # the add-on-the-fly modals
            'txnNewAccountModal', 'txnNewPartyModal', 'txnNewProjectModal',
        ):
            self.assertHtmlContains(html, needle, 'form field')
        self.assertIn('/hdc_static/css/new_transaction.css', html)
        self.assertIn('/hdc_static/js/pages/new_transaction.js', html)

    def test_register_and_focused_page_render_the_same_form_partial(self):
        register = self.client.get(REGISTER_URL).get_data(as_text=True)
        self.assertIn('data-hdc-txn-form', register)
        self.assertIn('/hdc_static/js/pages/new_transaction.js', register)
        template = _read('templates/hdc/accounts/cashflow_register.html')
        self.assertIn('{% include "accounts/_new_transaction_form.html" %}', template)

    def test_the_pickers_render_the_database_vocabulary(self):
        html = _html.unescape(self.client.get(NEW_TXN_URL).get_data(as_text=True))
        for account in ('Company Cash', 'Drawer Cash', 'MCB 1109'):
            self.assertHtmlContains(html, account, 'account option')
        self.assertHtmlContains(html, 'JPS KhanPur 5 Marla', 'project option')
        for category in ('Material & Purchase', 'Owner / Client Receipt'):
            self.assertHtmlContains(html, category, 'category option')

    def test_subcategory_options_carry_their_parent_category(self):
        """The dependency the JS filters on comes from the DB relationship."""
        cats = self._categories()
        rows = (CashFlowSubcategory.query
                .filter(CashFlowSubcategory.category_id == cats['Material & Purchase'].id)
                .all())
        self.assertTrue(rows)
        html = self.client.get(NEW_TXN_URL).get_data(as_text=True)
        for row in rows:
            self.assertHtmlContains(html, 'data-category="%d"' % row.category_id,
                                    'subcategory parent id')

    # ── Money Out ────────────────────────────────────────────────────────────

    def test_money_out_posts_with_category_subcategory_party_and_project(self):
        cat = self._categories()['Material & Purchase']
        sub = self._subcategories('Material & Purchase')['Cement']
        self._submit({
            'direction': 'out', 'date': '2026-09-21', 'amount': '1,25,000.50',
            'account_id': self.cash.id, 'category_id': cat.id, 'subcategory_id': sub.id,
            'party_name': 'Ahmed Cement Supplier', 'project_id': self.project.id,
            'reference': 'SLIP-1', 'description': 'Cement 100 bags', 'note': 'journey',
        })
        entry = CashFlowEntry.query.order_by(CashFlowEntry.id.desc()).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.direction, 'out')
        self.assertEqual(entry.amount_minor, 12500050)
        self.assertEqual(entry.category_id, cat.id)
        self.assertEqual(entry.subcategory_id, sub.id)
        self.assertEqual(entry.party_name, 'Ahmed Cement Supplier')
        self.assertEqual(entry.project_id, self.project.id)
        self.assertEqual(entry.reference, 'SLIP-1')
        self.assertIsNotNone(entry.account_tx_id, 'the entry must post to the ledger')
        tx = db.session.get(AccountTransaction, entry.account_tx_id)
        self.assertEqual(tx.amount_minor, entry.amount_minor)

    # ── Money In ─────────────────────────────────────────────────────────────

    def test_money_in_accepts_income_categories_only(self):
        income = self._categories()['Owner / Client Receipt']
        self._submit({
            'direction': 'in', 'date': '2026-09-21', 'amount': '50000',
            'account_id': self.cash.id, 'category_id': income.id,
            'party_name': 'Abdul Rehman', 'project_id': self.project.id,
        })
        entry = CashFlowEntry.query.order_by(CashFlowEntry.id.desc()).first()
        self.assertEqual(entry.direction, 'in')
        self.assertEqual(entry.category_id, income.id)

        # An expense category on a receipt is refused by the server.
        response = self._submit({
            'direction': 'in', 'date': '2026-09-21', 'amount': '10',
            'account_id': self.cash.id,
            'category_id': self._categories()['Material & Purchase'].id,
        }, follow=True)
        self.assertIn('not allowed for', self._error_text(response))

    def test_income_subcategories_come_from_the_income_category(self):
        subs = self._subcategories('Owner / Client Receipt')
        self.assertEqual(set(subs), {'Project Payment', 'Advance Received', 'Final Settlement'})

    # ── Internal Transfer ────────────────────────────────────────────────────

    def test_transfer_needs_no_category_party_or_project(self):
        self._submit({
            'direction': 'transfer', 'date': '2026-09-21', 'amount': '50,000',
            'account_id': self.cash.id, 'destination_account_id': self.bank.id,
            'reference': 'TRF-1', 'description': 'Cash to bank',
        })
        entry = CashFlowEntry.query.order_by(CashFlowEntry.id.desc()).first()
        self.assertEqual(entry.direction, 'transfer')
        self.assertEqual(entry.amount_minor, 5000000)
        self.assertIsNone(entry.category_id)
        self.assertIsNone(entry.subcategory_id)
        self.assertIsNone(entry.party_name)
        self.assertEqual(entry.destination_account_id, self.bank.id)
        self.assertIsNotNone(entry.account_tx_id)

    def test_transfer_refuses_the_same_account_on_both_legs(self):
        before = CashFlowEntry.query.count()
        response = self._submit({
            'direction': 'transfer', 'date': '2026-09-21', 'amount': '10',
            'account_id': self.cash.id, 'destination_account_id': self.cash.id,
        }, follow=True)
        self.assertIn('cannot be the same', self._error_text(response))
        self.assertEqual(CashFlowEntry.query.count(), before)

    def test_transfer_requires_a_destination(self):
        before = CashFlowEntry.query.count()
        response = self._submit({
            'direction': 'transfer', 'date': '2026-09-21', 'amount': '10',
            'account_id': self.cash.id,
        }, follow=True)
        self.assertIn('destination', self._error_text(response))
        self.assertEqual(CashFlowEntry.query.count(), before)

    # ── category → subcategory dependency, server-side ───────────────────────

    def test_stale_category_subcategory_pair_is_rejected(self):
        """A subcategory from another category must not be silently accepted."""
        before = CashFlowEntry.query.count()
        mason = self._subcategories('Labour & Wages')['Mason']
        response = self._submit({
            'direction': 'out', 'date': '2026-09-21', 'amount': '100',
            'account_id': self.cash.id,
            'category_id': self._categories()['Material & Purchase'].id,
            'subcategory_id': mason.id,
        }, follow=True)
        self.assertIn('not a subcategory of', self._error_text(response))
        self.assertEqual(CashFlowEntry.query.count(), before)

    def test_subcategory_of_the_right_category_is_accepted(self):
        cat = self._categories()['Labour & Wages']
        sub = self._subcategories('Labour & Wages')['Mason']
        self._submit({
            'direction': 'out', 'date': '2026-09-21', 'amount': '900',
            'account_id': self.cash.id, 'category_id': cat.id, 'subcategory_id': sub.id,
        })
        entry = CashFlowEntry.query.order_by(CashFlowEntry.id.desc()).first()
        self.assertEqual(entry.subcategory_id, sub.id)

    def test_subcategory_endpoint_is_scoped_to_its_category(self):
        cats = self._categories()
        response = self.client.get(NEW_TXN_URL + '/subcategories?category_id=%d' % cats['Labour & Wages'].id)
        self.assertEqual(response.status_code, 200)
        names = {item['name'] for item in response.get_json()['items']}
        self.assertIn('Mason', names)
        self.assertNotIn('Cement', names)

    # ── validation ───────────────────────────────────────────────────────────

    def test_invalid_amounts_never_post(self):
        cat = self._categories()['Miscellaneous']
        for raw, expected in (('', 'greater than zero'),
                              ('0', 'greater than zero'),
                              ('-5000', 'greater than zero'),
                              ('abc', 'valid number'),
                              ('1.2.3', 'valid number')):
            with self.subTest(amount=raw):
                before = CashFlowEntry.query.count()
                response = self._submit({
                    'direction': 'out', 'date': '2026-09-21', 'amount': raw,
                    'account_id': self.cash.id, 'category_id': cat.id,
                }, follow=True)
                self.assertIn(expected, self._error_text(response))
                self.assertEqual(CashFlowEntry.query.count(), before, raw)

    def test_missing_account_and_unknown_ids_never_post(self):
        cat = self._categories()['Miscellaneous']
        cases = (
            ({'account_id': ''}, 'valid active cash or bank account'),
            ({'account_id': '99999999'}, 'valid active cash or bank account'),
            ({'category_id': ''}, 'cash flow category'),
            ({'category_id': '99999999'}, 'cash flow category'),
            ({'project_id': '99999999'}, 'project no longer exists'),
        )
        for extra, expected in cases:
            with self.subTest(**extra):
                before = CashFlowEntry.query.count()
                payload = {'direction': 'out', 'date': '2026-09-21', 'amount': '100',
                           'account_id': self.cash.id, 'category_id': cat.id}
                payload.update(extra)
                response = self._submit(payload, follow=True)
                self.assertIn(expected, self._error_text(response))
                self.assertEqual(CashFlowEntry.query.count(), before, extra)

    def test_direction_is_required(self):
        before = CashFlowEntry.query.count()
        response = self._submit({
            'direction': '', 'date': '2026-09-21', 'amount': '100',
            'account_id': self.cash.id,
        }, follow=True)
        self.assertIn('Choose Received, Spent, or Transfer', self._error_text(response))
        self.assertEqual(CashFlowEntry.query.count(), before)

    # ── the form is never wiped by a rejection ───────────────────────────────

    def test_rejected_submission_replays_every_field(self):
        """The whole point: a server error must not empty the form."""
        cat = self._categories()['Material & Purchase']
        sub = self._subcategories('Material & Purchase')['Cement']
        response = self._submit({
            'direction': 'out', 'date': '2026-09-21', 'amount': 'not-money',
            'account_id': self.cash.id, 'category_id': cat.id, 'subcategory_id': sub.id,
            'party_name': 'Zubair Transport', 'project_id': self.project.id,
            'reference': 'SLIP-9', 'description': 'Cement 50 bags', 'note': 'keep me',
        }, follow=True)
        html = response.get_data(as_text=True)

        self.assertHtmlContains(html, 'This transaction was not saved.')
        self.assertHtmlContains(html, 'value="not-money"', 'amount')
        self.assertHtmlContains(html, 'value="SLIP-9"', 'reference')
        self.assertHtmlContains(html, 'value="Cement 50 bags"', 'description')
        self.assertHtmlContains(html, 'keep me', 'note')
        self.assertHtmlContains(html, '>Zubair Transport<', 'party option')
        # direction, category, subcategory, account and project stay selected
        self.assertOptionSelected(html, 'out', 'direction')
        self.assertOptionSelected(html, cat.id, 'category')
        self.assertOptionSelected(html, sub.id, 'subcategory')
        self.assertOptionSelected(html, self.cash.id, 'account')
        self.assertOptionSelected(html, self.project.id, 'project')

    def test_a_clean_render_does_not_replay_an_old_draft(self):
        self._submit({'direction': 'out', 'date': '2026-09-21', 'amount': '',
                      'account_id': self.cash.id,
                      'category_id': self._categories()['Miscellaneous'].id}, follow=True)
        fresh = self.client.get(NEW_TXN_URL).get_data(as_text=True)
        self.assertNotIn('This transaction was not saved.', fresh)

    def test_register_page_also_replays_a_rejected_submission(self):
        cat = self._categories()['Miscellaneous']
        self._submit({'direction': 'out', 'date': '2026-09-21', 'amount': 'abc',
                      'account_id': self.cash.id, 'category_id': cat.id},
                     url=REGISTER_URL, follow=False)
        html = self.client.get(REGISTER_URL).get_data(as_text=True)
        self.assertIn('must be a valid number', html)
        self.assertIn('This transaction was not saved.', html)

    # ── add-on-the-fly: Account / Party / Project ────────────────────────────

    def test_add_account_creates_it_and_never_duplicates(self):
        response = self._json(NEW_TXN_URL + '/account',
                              {'name': 'Easypaisa Rizwan', 'mode': 'cash',
                               'opening_balance': '25,000'})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['created'])
        self.assertEqual(body['item']['name'], 'Easypaisa Rizwan')
        account_id = body['item']['id']
        self.assertAlmostEqual(
            float(db.session.get(Account, account_id).opening_balance), 25000.0, places=2)

        again = self._json(NEW_TXN_URL + '/account',
                           {'name': 'easypaisa rizwan', 'mode': 'cash'})
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.get_json()['created'])
        self.assertEqual(again.get_json()['item']['id'], account_id,
                         'a case-only difference must reuse the existing account')
        self.assertEqual(Account.query.filter(
            Account.type == 'cash', Account.name.ilike('Easypaisa%')).count(), 1)

        # …and the new account is immediately usable in the form.
        self._submit({'direction': 'out', 'date': '2026-09-21', 'amount': '500',
                      'account_id': account_id,
                      'category_id': self._categories()['Miscellaneous'].id})
        entry = CashFlowEntry.query.order_by(CashFlowEntry.id.desc()).first()
        self.assertEqual(entry.account_id, account_id)

    def test_add_account_validates(self):
        for payload, expected in (
            ({'name': '   '}, 'Account name is required'),
            ({'name': 'Banky', 'mode': 'bank'}, 'bank_name is required'),
            ({'name': 'Banky', 'mode': 'bank', 'bank_name': 'HBL',
              'account_number': '1', 'opening_balance': 'lots'}, 'valid number'),
            ({'name': 'Weird', 'mode': 'crypto'}, 'Cash account or a Bank account'),
        ):
            with self.subTest(payload=payload):
                response = self._json(NEW_TXN_URL + '/account', payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.get_json()['message'])

    def test_add_party_creates_it_and_never_duplicates(self):
        response = self._json(NEW_TXN_URL + '/party',
                              {'name': 'New Vendor Co', 'party_type': 'supplier',
                               'phone': '0300 1234567'})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body['created'])
        self.assertEqual(body['item']['party_type'], 'supplier')
        self.assertEqual(CashFlowParty.query.filter(
            CashFlowParty.name.ilike('New Vendor%')).count(), 1)

        again = self._json(NEW_TXN_URL + '/party', {'name': 'new vendor co'})
        self.assertFalse(again.get_json()['created'])

        bad = self._json(NEW_TXN_URL + '/party', {'name': 'X', 'party_type': 'hacker'})
        self.assertEqual(bad.status_code, 400)

        # The new party can be attached to an entry straight away.
        self._submit({'direction': 'out', 'date': '2026-09-21', 'amount': '750',
                      'account_id': self.cash.id,
                      'category_id': self._categories()['Miscellaneous'].id,
                      'party_name': 'New Vendor Co'})
        entry = CashFlowEntry.query.order_by(CashFlowEntry.id.desc()).first()
        self.assertEqual(entry.party_name, 'New Vendor Co')
        self.assertEqual(entry.party_type, 'supplier')

        # Posting the name alone (party_type defaults to 'other') must not
        # re-file a known supplier as "other".
        self._submit({'direction': 'out', 'date': '2026-09-21', 'amount': '400',
                      'account_id': self.cash.id,
                      'category_id': self._categories()['Miscellaneous'].id,
                      'party_name': 'New Vendor Co', 'party_type': 'other'})
        later = CashFlowEntry.query.order_by(CashFlowEntry.id.desc()).first()
        self.assertEqual(later.party_type, 'supplier')
        self.assertEqual(db.session.get(CashFlowParty, later.party_id).party_type, 'supplier')

    def test_add_project_creates_it_with_a_generated_code(self):
        response = self._json(NEW_TXN_URL + '/project',
                              {'name': 'Al Riaz Plaza', 'client': 'Riaz',
                               'location': 'Gulberg'})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body['created'])
        self.assertTrue(body['item']['project_code'])
        row = db.session.get(Project, body['item']['id'])
        self.assertEqual(row.name, 'Al Riaz Plaza')
        self.assertEqual(row.client, 'Riaz')
        self.assertEqual(row.status, 'Active')

        again = self._json(NEW_TXN_URL + '/project', {'name': 'al riaz plaza'})
        self.assertFalse(again.get_json()['created'])
        self.assertEqual(Project.query.filter(Project.name.ilike('Al Riaz%')).count(), 1)

        blank = self._json(NEW_TXN_URL + '/project', {'name': ''})
        self.assertEqual(blank.status_code, 400)

        self._submit({'direction': 'out', 'date': '2026-09-21', 'amount': '300',
                      'account_id': self.cash.id,
                      'category_id': self._categories()['Miscellaneous'].id,
                      'project_id': row.id})
        entry = CashFlowEntry.query.order_by(CashFlowEntry.id.desc()).first()
        self.assertEqual(entry.project_id, row.id)

    # ── duplicates and permissions ───────────────────────────────────────────

    def test_double_submission_posts_once(self):
        cat = self._categories()['Miscellaneous']
        csrf, key = self._page_tokens()
        payload = {'action': 'create_entry', 'direction': 'out', 'date': '2026-09-21',
                   'amount': '111', 'account_id': self.cash.id, 'category_id': cat.id,
                   'description': 'double click probe',
                   '_csrf_token': csrf, '_idempotency_key': key}
        before = CashFlowEntry.query.count()
        self.client.post(NEW_TXN_URL, data=payload, follow_redirects=False)
        self.client.post(NEW_TXN_URL, data=payload, follow_redirects=False)
        self.assertEqual(CashFlowEntry.query.count(), before + 1)

    def test_non_admin_cannot_reach_the_entry_surface(self):
        """The create endpoints are money writes, not a way round the register."""
        from werkzeug.security import generate_password_hash
        from hdc.models.auth import HDCUser

        staff = HDCUser(username='staff-txn',
                        password_hash=generate_password_hash('Staff@1234'),
                        role='staff')
        db.session.add(staff)
        db.session.commit()

        # Log out for real.  ``flask_login`` caches the resolved user on ``g``,
        # which belongs to the app context this test pushed, so clearing the
        # session alone would keep the admin "signed in" for every later call.
        with self.client.session_transaction() as session:
            session.clear()
        g.pop('_login_user', None)
        staff_client = self.app.test_client()
        staff_client.get('/hdc/login')
        with staff_client.session_transaction() as session:
            token = session['_csrf_token']
        staff_client.post('/hdc/login', data={'username': 'staff-txn',
                                              'password': 'Staff@1234',
                                              '_csrf_token': token})

        page = staff_client.get(NEW_TXN_URL)
        self.assertEqual(page.status_code, 302, 'the page is admin-only')
        self.assertEqual(staff_client.get(REGISTER_URL).status_code, 302)
        for endpoint in ('/account', '/party', '/project'):
            with self.subTest(endpoint=endpoint):
                response = staff_client.post(
                    NEW_TXN_URL + endpoint, json={'name': 'Should Not Exist'},
                    headers={'X-CSRFToken': token})
                self.assertEqual(response.status_code, 403)
                self.assertFalse(response.get_json()['ok'])
        self.assertIsNone(CashFlowParty.query.filter_by(name='Should Not Exist').first())
        self.assertIsNone(Project.query.filter_by(name='Should Not Exist').first())

    # ── the page script is the behaviour ─────────────────────────────────────

    def test_page_script_is_wired_without_inline_logic(self):
        """Template and script cannot drift apart silently."""
        template = _read(FORM_PARTIAL)
        script = _read(PAGE_SCRIPT)
        page = _read('templates/hdc/accounts/new_transaction.html')

        # every combo pair the form renders is attached by the script
        for input_id, select_id in (('txnAccountInput', 'txnAccount'),
                                    ('txnToAccountInput', 'txnToAccount'),
                                    ('txnPartyInput', 'txnParty'),
                                    ('txnProjectInput', 'txnProject')):
            self.assertIn('id="%s"' % input_id, template, input_id)
            self.assertIn('id="%s"' % select_id, template, select_id)
            self.assertRegex(
                script, r"attachCombo\(\s*'%s'\s*,\s*'%s'" % (input_id, select_id),
                '%s must attach %s to %s' % (PAGE_SCRIPT, input_id, select_id))

        # the add-new action row exists for each database picker
        for label in ('+ Add New Account', '+ Add New Party', '+ Add New Project'):
            self.assertIn(label, script, label)

        # the page loads the extracted script, not an inline copy
        self.assertNotIn('function initForm', page)
        self.assertIn('new_transaction.js', page)

    def test_subcategory_dependency_is_not_hard_coded_in_the_script(self):
        script = _read(PAGE_SCRIPT)
        for name in ('Cement', 'Mason', 'Steel / Saria', 'Material & Purchase'):
            self.assertNotIn(name, script,
                             'the dependency must come from the data, not the script')

    def test_templates_carry_no_hard_coded_category_lists(self):
        for path in (FORM_PARTIAL, 'templates/hdc/accounts/new_transaction.html'):
            source = _read(*path.split('/'))
            for name in ('Cement', 'Steel / Saria', 'Owner / Client Receipt',
                         'Scrap & Salvage Sale'):
                self.assertNotIn(name, source,
                                 '%s hard-codes the category vocabulary' % path)


if __name__ == '__main__':
    unittest.main()


class NewTransactionHarnessTestCase(unittest.TestCase):
    """The controller itself, executed for real under Node."""

    @unittest.skipIf(shutil.which('node') is None, 'node is not installed')
    def test_form_behaviour_in_a_real_dom_stub(self):
        """Direction gating, the category dependency, the add-new flow, the
        no-result copy and the submit guards — all driven through the real
        ``new_transaction.js`` (plus ``combo.js``) rather than re-implemented."""
        result = subprocess.run(
            ['node', HARNESS, REPO_ROOT],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            'new_transaction.js harness failed:\n%s%s' % (result.stdout, result.stderr),
        )
        self.assertIn('checks passed', result.stdout)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
