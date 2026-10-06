#!/usr/bin/env python3
"""The Parties module (sidebar → Parties): directory + per-party ledger.

Parties live in a single table (``hdc_cash_flow_party``) that every
*Party / Person* picker reads.  The directory at ``/hdc/parties`` is one
ledger-style list with KPI category filters and a searchable name combo:

* **Loan Parties** (``lender`` / ``borrower``) — what the loan Money In /
  Money Out categories in the CF Register offer (*Loan Received*, *Loan
  Given*, *Loan Repayment*, *Loan Recovery*).
* **External Customers** (``rental``) — the HDC Tools rental parties.  An
  external tool rental syncs its customer here automatically, and every
  rental party is offered back in the HDC Tools customer search.
* **Workers** — synced from the Workers module.
* **Clients / Owners** (``client``) — the project owners whose projects we
  build.
* **Suppliers / Vendors** (``supplier``) — material, tool and service vendors.
* **Other Parties** — office staff, subcontractors, one-off names.

Opening a party (``/hdc/parties/<id>``) shows that name's dated
``hdc_account_txn`` rows — the same payments as Accounts → All Entries.

Covered:

* the page renders for every role, but only admin/accountant can write;
* adding a loan party lands it in the New Transaction party picker, and the
  loan categories carry ``data-party-types`` restricted to Loan Parties so
  Money In / Money Out shortens the list to them;
* creating an external HDC Tools rental (or posting its payment) puts the
  customer under External Customers;
* ``ensure_party`` (the auto-sync) never reclassifies a party that already
  has a specific type, and ``known_tool_customers`` offers rental parties
  added by hand;
* deactivate hides a party without touching history.

Like the sidebar / money-permission suites, DB work happens inside short
``app_context()`` blocks — a long-lived context would keep Flask-Login's
cached user on ``g`` and ignore the role switches these tests make.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_parties_module -v
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from sqlalchemy import func                                        # noqa: E402

from hdc.app import create_app                                     # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.accounts import Account, AccountTransaction        # noqa: E402
from hdc.models.auth import HDCUser                                # noqa: E402
from hdc.models.cashflow import CashFlowParty                      # noqa: E402
from hdc.models.projects import Project                            # noqa: E402
from hdc.models.tool_rental import (                               # noqa: E402
    Tool, ToolCategory, ToolRental, ToolRentalItem, ToolRentalPayment,
)
from hdc.services.cashflow_register import (                       # noqa: E402
    ensure_party, save_cf_party,
)
from hdc.services.tool_rental import known_tool_customers         # noqa: E402
from hdc.utils.dates import _pkt_today                            # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
PARTIES_URL = '/hdc/parties'
NEW_TXN_URL = '/hdc/accounts/new-transaction'

#: The four loan Money In / Money Out categories and the party types each
#: one must offer (what makes the picker show *Loan Parties* there).
LOAN_CATEGORIES = {
    'Loan Received': 'lender',
    'Loan Given': 'borrower',
    'Loan Repayment': 'lender',
    'Loan Recovery': 'borrower',
}


class PartiesModuleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-parties-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp,
                               'TESTING': True})
        self.client = self.app.test_client()
        self._login()

        with self.app.app_context():
            site = Project(name='Site A', project_code='P-A', client='Owner A')
            cat = ToolCategory(name='Power Tools', active_status=True)
            db.session.add_all([site, cat])
            db.session.flush()
            tool = Tool(tool_code='TOOL-0001', name='Vibrator', unit='pcs',
                        category_id=cat.id, total_quantity=10,
                        purchase_cost=10_000, rental_rate_per_day=500,
                        status='active', is_void=False)
            db.session.add(tool)
            db.session.flush()
            self.tool_id = tool.id
            # Roles: the directory is readable by everyone, writable by money roles.
            self.users = {}
            admin = HDCUser.query.filter_by(username='admin').one()
            self.users['admin'] = admin.id
            for role in ('accountant', 'staff', 'manager'):
                user = HDCUser(username='parties-' + role, password_hash='unused',
                               role=role)
                db.session.add(user)
                db.session.flush()
                self.users[role] = user.id
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _login(self):
        self.client.get('/hdc/login')
        with self.client.session_transaction() as session:
            token = session['_csrf_token']
        response = self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD,
            '_csrf_token': token})
        self.assertEqual(response.status_code, 302)

    def _token(self):
        with self.client.session_transaction() as session:
            return session.get('_csrf_token', '')

    def _as(self, role):
        token = self._token()
        with self.client.session_transaction() as session:
            session.clear()
            session['_csrf_token'] = token
            session['_user_id'] = str(self.users[role])
            session['_fresh'] = True

    def _filter(self, **params):
        qs = '&'.join('%s=%s' % item for item in params.items() if item[1] not in (None, ''))
        url = PARTIES_URL + (('?' + qs) if qs else '')
        return self.client.get(url).get_data(as_text=True)

    @staticmethod
    def _ledger_table(html):
        """The directory table only — the add-party combo lists every name."""
        start = html.index('id="parties-ledger"')
        table = html.index('<table', start)
        return html[table:html.index('</table>', table)]

    def _create_external_rental(self, customer='Zeesab Hardware'):
        token = self._token()
        response = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': token,
            'renter_type': 'external',
            'billing_type': 'fixed_fee',
            'customer_name': customer,
            'customer_phone': '0321-9999999',
            'rental_date': _pkt_today().isoformat(),
            'tool_id[]': [str(self.tool_id)],
            'qty[]': ['2'],
            'rate[]': ['500'],
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            rental = (ToolRental.query
                      .filter_by(renter_type='external', customer_name=customer)
                      .order_by(ToolRental.id.desc()).first())
        self.assertIsNotNone(rental, 'the external rental was not created')
        return rental

    # ── the directory page ───────────────────────────────────────────────────
    def test_page_renders_kpis_ledger_and_combos_for_every_role(self):
        for role in ('admin', 'accountant', 'staff', 'manager'):
            self._as(role)
            response = self.client.get(PARTIES_URL)
            self.assertEqual(response.status_code, 200, role)
            html = response.get_data(as_text=True)
            for label in ('Loan Parties', 'External Customers', 'Workers',
                          'Clients / Owners', 'Suppliers / Vendors',
                          'Other Parties', 'All Parties'):
                self.assertIn(label, html, (role, label))
            self.assertIn('id="parties-ledger"', html, role)
            self.assertIn('Add Party', html, role)
            self.assertIn('data-hdc-combo="partyName"', html, role)
            self.assertIn('data-hdc-combo="partyNameFilter"', html, role)
            self.assertIn('name="category"', html, role)
            self.assertIn('name="q"', html, role)
            self.assertRegex(html, r'<select[^>]*name="party_type"', role)

    def test_add_party_lands_in_the_right_category_and_opens_its_ledger(self):
        token = self._token()
        response = self.client.post(PARTIES_URL, data={
            '_csrf_token': token, 'action': 'add_party',
            'name': 'Chacha Lender', 'party_type': 'lender',
            'phone': '0300-1112223'}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Chacha Lender', body)
        self.assertIn('Financials — by date', body)
        with self.app.app_context():
            party = CashFlowParty.query.filter_by(name='Chacha Lender').one()
            self.assertEqual(party.party_type, 'lender')
            party_id = party.id

        html = self.client.get(PARTIES_URL).get_data(as_text=True)
        self.assertIn('Chacha Lender', html)
        self.assertIn('/hdc/parties/%d' % party_id, html)
        self.assertIn('Chacha Lender', self._ledger_table(self._filter(category='loan')))
        self.assertNotIn('Chacha Lender', self._ledger_table(self._filter(category='rental')))

    def test_toggle_party_hides_and_restores_it(self):
        with self.app.app_context():
            party, _ = save_cf_party('Hidden Supplier Co', party_type='supplier')
            db.session.commit()
            party_id = party.id
        token = self._token()

        self.client.post(PARTIES_URL, data={
            '_csrf_token': token, 'action': 'toggle_party',
            'party_id': party_id}, follow_redirects=True)
        with self.app.app_context():
            self.assertFalse(db.session.get(CashFlowParty, party_id).is_active)

        self.client.post(PARTIES_URL, data={
            '_csrf_token': token, 'action': 'toggle_party',
            'party_id': party_id}, follow_redirects=True)
        with self.app.app_context():
            self.assertTrue(db.session.get(CashFlowParty, party_id).is_active)

    def test_clients_and_suppliers_get_a_category_of_their_own(self):
        """A project owner / supplier is filed under its own bucket, not 'Other'."""
        with self.app.app_context():
            save_cf_party('Al-Rehman Builders', party_type='client')
            save_cf_party('City Cement Depot', party_type='supplier')
            save_cf_party('Office Helper', party_type='staff')
            db.session.commit()

        client_table = self._ledger_table(self._filter(category='client'))
        supplier_table = self._ledger_table(self._filter(category='supplier'))
        other_table = self._ledger_table(self._filter(category='other'))

        self.assertIn('Al-Rehman Builders', client_table)
        self.assertNotIn('Al-Rehman Builders', supplier_table)
        self.assertNotIn('Al-Rehman Builders', other_table)

        self.assertIn('City Cement Depot', supplier_table)
        self.assertNotIn('City Cement Depot', client_table)
        self.assertNotIn('City Cement Depot', other_table)

        # …while the types without a bucket still share the catch-all.
        self.assertIn('Office Helper', other_table)

        # …and each party's own page is labelled by its bucket.
        with self.app.app_context():
            client_id = CashFlowParty.query.filter_by(name='Al-Rehman Builders').one().id
            supplier_id = CashFlowParty.query.filter_by(name='City Cement Depot').one().id
        self.assertIn('Clients / Owners',
                      self.client.get('/hdc/parties/%d' % client_id).get_data(as_text=True))
        self.assertIn('Suppliers / Vendors',
                      self.client.get('/hdc/parties/%d' % supplier_id).get_data(as_text=True))

    def test_party_category_files_every_type_into_a_bucket(self):
        from hdc.services.parties import PARTY_CATEGORY_KEYS, party_category
        for value, expected in (('lender', 'loan'), ('borrower', 'loan'),
                                ('rental', 'rental'), ('worker', 'worker'),
                                ('client', 'client'), ('supplier', 'supplier'),
                                ('staff', 'other'), ('subcontractor', 'other'),
                                ('other', 'other'), ('', 'other'), (None, 'other')):
            self.assertEqual(party_category(value), expected, value)
        # every bucket the page offers is one a party can actually land in
        self.assertIn('client', PARTY_CATEGORY_KEYS)
        self.assertIn('supplier', PARTY_CATEGORY_KEYS)

    def test_staff_can_read_the_directory_but_not_write_it(self):
        self._as('staff')
        token = self._token()
        response = self.client.post(PARTIES_URL, data={
            '_csrf_token': token, 'action': 'add_party',
            'name': 'Staff Should Not Save', 'party_type': 'other'},
            follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.assertIsNone(CashFlowParty.query
                              .filter_by(name='Staff Should Not Save').first())

    # ── Money In / Out → Loan Parties ────────────────────────────────────────
    def test_loan_categories_offer_only_loan_party_types(self):
        with self.app.app_context():
            save_cf_party('Uncle Lender', party_type='lender')
            save_cf_party('Cousin Borrower', party_type='borrower')
            save_cf_party('General Supplier', party_type='supplier')
            db.session.commit()

        html = self.client.get(NEW_TXN_URL).get_data(as_text=True)
        for name, expected in LOAN_CATEGORIES.items():
            match = re.search(
                r'<option value="\d+"[^>]*data-party-types="([^"]*)"[^>]*>'
                r'\s*%s\s*</option>' % re.escape(name), html, re.S)
            self.assertIsNotNone(match, name)
            allowed = [p for p in match.group(1).split(',') if p]
            self.assertEqual(allowed, [expected], name)
            # …which is exactly the Loan Parties pair, never e.g. 'supplier'.
            self.assertIn(expected, ('lender', 'borrower'), name)

        # The picker's option list carries the loan parties with their type,
        # so the form's filter can shorten the list to them.
        self.assertIn('data-party-type="lender"', html)
        self.assertIn('data-party-type="borrower"', html)

    def test_new_transaction_script_labels_the_party_types(self):
        with open(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'static/hdc/js/pages/new_transaction.js'), encoding='utf-8') as fh:
            source = fh.read()
        self.assertIn("lender: 'Loan Giver / Financier'", source)
        self.assertIn("borrower: 'Loan Taker / Borrower'", source)
        self.assertIn("rental: 'External Customer (HDC Tools)'", source)

    # ── HDC Tools → External Customers ───────────────────────────────────────
    def test_external_rental_syncs_customer_to_external_customers(self):
        self._create_external_rental('Zeesab Hardware')
        with self.app.app_context():
            party = CashFlowParty.query.filter_by(name='Zeesab Hardware').one()
            self.assertEqual(party.party_type, 'rental')

        html = self.client.get(PARTIES_URL).get_data(as_text=True)
        self.assertIn('Zeesab Hardware', html)
        self.assertIn('Zeesab Hardware', self._ledger_table(self._filter(category='rental')))
        self.assertNotIn('Zeesab Hardware', self._ledger_table(self._filter(category='loan')))

    def test_rental_payment_syncs_an_older_rentals_customer(self):
        # A rental that predates the directory: only the payment syncs it.
        with self.app.app_context():
            rental = ToolRental(
                rental_code='RENT-90001', renter_type='external',
                customer_name='Old Timers Tools', customer_phone='0300-0000000',
                rental_date=_pkt_today(), billing_type='fixed_fee',
                status='active', payment_status='unpaid', is_void=False)
            db.session.add(rental)
            db.session.flush()
            db.session.add(ToolRentalItem(
                rental_id=rental.id, tool_id=self.tool_id,
                qty_rented=1, qty_returned=0, qty_pending=1,
                rate=500, amount=500))
            rental.total_rented_qty = 1
            rental.total_amount = 500
            payment = ToolRentalPayment(rental_id=rental.id, amount=500.0,
                                        payment_date=_pkt_today(),
                                        payment_mode='cash')
            db.session.add(payment)
            db.session.commit()
            payment_id, rental_id = payment.id, rental.id

            from hdc.services.tool_rental import post_tool_rental_payment_to_accounts
            ok, message, _txns = post_tool_rental_payment_to_accounts(
                db.session.get(ToolRentalPayment, payment_id),
                rental=db.session.get(ToolRental, rental_id),
                commit=True)
            self.assertTrue(ok, message)

            party = CashFlowParty.query.filter_by(name='Old Timers Tools').one()
            self.assertEqual(party.party_type, 'rental')

    def test_known_tool_customers_offers_parties_added_by_hand(self):
        with self.app.app_context():
            save_cf_party('Added Before First Rental', party_type='rental')
            db.session.commit()
            names = known_tool_customers()
        self.assertIn('Added Before First Rental', names)

    def test_ensure_party_never_reclassifies_a_specific_type(self):
        with self.app.app_context():
            lender, _ = save_cf_party('Multi Deal Khan', party_type='lender')
            blank, created = save_cf_party('Unknown Type Co', party_type='other')
            self.assertTrue(created)
            db.session.commit()

            # The same person renting a tool must stay a Loan Party.
            row, created = ensure_party('multi deal khan', party_type='rental')
            self.assertFalse(created)
            self.assertEqual(row.id, lender.id)
            self.assertEqual(row.party_type, 'lender')

            # An unclassified party picks up the sync's type…
            row, _ = ensure_party('Unknown Type Co', party_type='rental')
            self.assertEqual(row.id, blank.id)
            self.assertEqual(row.party_type, 'rental')

            # …and a hidden party reactivates on new activity.
            row.is_active = False
            db.session.flush()
            row, _ = ensure_party('UNKNOWN TYPE CO', party_type='rental')
            self.assertTrue(row.is_active)

            # Unknown types never invent a classification.
            row, _ = ensure_party('Weird Type Co', party_type='hacker')
            self.assertEqual(row.party_type, 'other')

    def test_search_and_name_combo_filter_the_ledger(self):
        with self.app.app_context():
            save_cf_party('Akram Lender', party_type='lender')
            save_cf_party('Bilal Hardware', party_type='rental', phone='0300-555')
            db.session.commit()
            bilal_id = CashFlowParty.query.filter_by(name='Bilal Hardware').one().id

        self.assertIn('Akram Lender', self._ledger_table(self._filter(q='akram')))
        self.assertNotIn('Bilal Hardware', self._ledger_table(self._filter(q='akram')))
        self.assertIn('Bilal Hardware', self._ledger_table(self._filter(q='0300-555')))
        named = self._ledger_table(self._filter(party_id=str(bilal_id)))
        self.assertIn('Bilal Hardware', named)
        self.assertNotIn('Akram Lender', named)

    def test_party_payment_shows_on_ledger_and_in_all_entries(self):
        """One posted row: party statement and Accounts → All Entries agree."""
        with self.app.app_context():
            party, _ = save_cf_party('Khan Traders', party_type='supplier')
            db.session.commit()
            party_id = party.id
            cash = Account.query.filter(
                func.lower(Account.type) == 'cash',
                Account.is_void == False).first()  # noqa: E712
            if cash is None:
                cash = Account(name='Parties Test Cash', type='cash', status='active')
                db.session.add(cash)
                db.session.flush()
            txn = AccountTransaction(
                date=_pkt_today(), amount=2500, type='party_payment',
                from_account_id=cash.id, executed_by_account_id=cash.id,
                party_name='Khan Traders', category='expense',
                note='Paint bill', reference_id='SMOKE-PARTY-1')
            db.session.add(txn)
            db.session.commit()
            txn_id = txn.id

        ledger = self.client.get('/hdc/parties/%d' % party_id)
        self.assertEqual(ledger.status_code, 200)
        html = ledger.get_data(as_text=True)
        self.assertIn('Khan Traders', html)
        self.assertIn('Paint bill', html)
        self.assertIn('2,500', html)
        self.assertIn('Party Payment', html)
        self.assertIn('Running Balance', html)
        self.assertIn('party_name=Khan', html)

        entries = self.client.get('/hdc/accounts/entries?party_name=Khan%20Traders')
        self.assertEqual(entries.status_code, 200)
        entries_html = entries.get_data(as_text=True)
        self.assertIn('Khan Traders', entries_html)
        self.assertIn('id="txn-%d"' % txn_id, entries_html)
        self.assertIn('data-hdc-combo="entriesPartyName"', entries_html)

        directory = self.client.get(PARTIES_URL).get_data(as_text=True)
        self.assertIn('Khan Traders', directory)
        self.assertIn('2,500', directory)


if __name__ == '__main__':
    unittest.main()
