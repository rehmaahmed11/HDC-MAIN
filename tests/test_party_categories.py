#!/usr/bin/env python3
"""User-defined Parties categories (sidebar → Parties).

The directory used to file every party by mapping its ``party_type`` to one of
six hard-coded buckets.  The field asked for categories *we define ourselves*:
create one, name it anything, then file parties into it — and keep growing the
list later.

These cases pin the whole workflow:

* the six shipped buckets are seeded as **rows** and can be renamed;
* a brand-new category is created from the page and immediately becomes a KPI
  card, a filter and an option in Add / Edit Party;
* a party is filed at creation, moved by Edit, moved in bulk, and returned to
  "Automatic (by type)" by clearing the field;
* a category can be retired (not offered for new filing) and deleted only
  once no party is filed under it;
* a rename keeps the parties and the stable ``?category=`` key;
* the legacy type mapping still files automatic syncs (workers, rentals,
  project clients) when nobody has filed the party by hand;
* only money-writing roles may manage categories.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_party_categories -v
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

from hdc.app import create_app                                      # noqa: E402
from hdc.extensions import db                                       # noqa: E402
from hdc.models.auth import HDCUser                                 # noqa: E402
from hdc.models.cashflow import CashFlowParty, PartyCategory        # noqa: E402
from hdc.services.cashflow_register import ensure_party, save_cf_party  # noqa: E402
from hdc.services.party_categories import (                        # noqa: E402
    DEFAULT_PARTY_CATEGORIES, category_by_key, default_party_category_for_type,
)

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
PARTIES_URL = '/hdc/parties'


class PartyCategoriesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-party-categories-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp,
                               'TESTING': True})
        self.client = self.app.test_client()
        self._login()
        with self.app.app_context():
            self.users = {}
            admin = HDCUser.query.filter_by(username='admin').one()
            self.users['admin'] = admin.id
            for role in ('accountant', 'staff', 'manager'):
                user = HDCUser(username='pc-' + role, password_hash='unused',
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

    def _post(self, data, follow=True):
        data = dict(data)
        data.setdefault('_csrf_token', self._token())
        return self.client.post(PARTIES_URL, data=data, follow_redirects=follow)

    def _create_category(self, name='Cement Dealers', **extra):
        payload = {'action': 'add_category', 'name': name,
                   'icon': 'fa-truck-field', 'colour': 'kpi-orange',
                   'description': 'Material vendors on credit'}
        payload.update(extra)
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            return PartyCategory.query.filter_by(name=name).one().id

    @staticmethod
    def _ledger_table(html):
        start = html.index('id="parties-ledger"')
        table = html.index('<table', start)
        return html[table:html.index('</table>', table)]

    def _filter(self, **params):
        from urllib.parse import urlencode
        url = PARTIES_URL + (('?' + urlencode(params)) if params else '')
        return self.client.get(url).get_data(as_text=True)

    def test_page_uses_unique_element_ids(self):
        """The Add/Edit forms and the filter share a page — ids must not clash.

        The shared party macro generates an id from its ``id_prefix`` (the Add
        form's category select is ``partyCategory``); the filter select is
        deliberately ``partyCategoryFilter``.  A duplicate id would make the
        label and any scripting point at the wrong control.
        """
        html = self.client.get(PARTIES_URL).get_data(as_text=True)
        ids = re.findall(r'\bid="([^"]+)"', html)
        duplicates = {value for value in ids if ids.count(value) > 1}
        self.assertFalse(duplicates, duplicates)

    # ── the seeded buckets are rows, not code ───────────────────────────────
    def test_shipped_buckets_are_seeded_rows_that_can_be_renamed(self):
        with self.app.app_context():
            keys = {c.key for c in PartyCategory.query.all()}
        self.assertEqual(keys, {key for key, *_ in DEFAULT_PARTY_CATEGORIES})

        with self.app.app_context():
            loan_id = category_by_key('loan').id
        response = self._post({
            'action': 'edit_category', 'category_id': loan_id,
            'name': 'Financiers & Lenders', 'icon': 'fa-handshake',
            'colour': 'kpi-blue', 'description': 'People who fund us'})
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            row = db.session.get(PartyCategory, loan_id)
            self.assertEqual(row.name, 'Financiers & Lenders')
            # The key survives a rename so old filter links keep working.
            self.assertEqual(row.key, 'loan')
        html = self._filter(category='loan')
        self.assertIn('Financiers &amp; Lenders', html)
        self.assertNotIn('<div class="kpi-label">Loan Parties</div>', html)

    # ── create → file parties → filter ──────────────────────────────────────
    def test_create_category_then_file_a_party_in_it_when_adding(self):
        category_id = self._create_category()
        self.assertIn('Cement Dealers', self.client.get(PARTIES_URL).get_data(as_text=True))

        self._post({'action': 'add_party', 'name': 'Hilal Cement Store',
                    'party_type': 'supplier', 'category_id': category_id})
        with self.app.app_context():
            party = CashFlowParty.query.filter_by(name='Hilal Cement Store').one()
            self.assertEqual(int(party.category_id), category_id)

        custom_table = self._ledger_table(self._filter(category='cement-dealers'))
        supplier_table = self._ledger_table(self._filter(category='supplier'))
        loan_table = self._ledger_table(self._filter(category='loan'))
        self.assertIn('Hilal Cement Store', custom_table)
        self.assertNotIn('Hilal Cement Store', supplier_table)
        self.assertNotIn('Hilal Cement Store', loan_table)

        page = self._filter(category='cement-dealers')
        self.assertIn('Cement Dealers', page)
        self.assertIn('Hilal Cement Store', page)

    def test_party_can_be_moved_by_edit_and_returned_to_automatic(self):
        category_id = self._create_category()
        with self.app.app_context():
            party, _ = save_cf_party('Mover Co', party_type='lender')
            db.session.commit()
            party_id = party.id
            self.assertEqual(party.category_id, default_party_category_for_type('lender').id)

        self._post({'action': 'edit_party', 'party_id': party_id,
                    'name': 'Mover Co', 'party_type': 'lender',
                    'category_id': category_id, 'phone': '', 'note': ''})
        with self.app.app_context():
            self.assertEqual(int(db.session.get(CashFlowParty, party_id).category_id),
                             category_id)

        # '' is the form's "Automatic — from the party type".
        self._post({'action': 'edit_party', 'party_id': party_id,
                    'name': 'Mover Co', 'party_type': 'lender',
                    'category_id': '', 'phone': '', 'note': ''})
        with self.app.app_context():
            self.assertIsNone(db.session.get(CashFlowParty, party_id).category_id)
        self.assertIn('Mover Co', self._ledger_table(self._filter(category='loan')))
        self.assertNotIn('Mover Co', self._ledger_table(self._filter(category='cement-dealers')))

    def test_edit_window_keeps_an_automatic_party_automatic(self):
        """Opening Edit to change a phone number must not pin the category."""
        with self.app.app_context():
            party, _ = save_cf_party('Auto Form Co', party_type='lender')
            db.session.commit()
            party_id = party.id

        html = self.client.get(PARTIES_URL).get_data(as_text=True)
        button = re.search(r'<button[^>]*data-party-id="%d"[^>]*>' % party_id,
                           html)
        self.assertIsNotNone(button)
        self.assertIn('data-party-category=""', button.group(0))

        # The modal submits '' for Automatic; the filing falls back to
        # Loan Parties and stays free to follow a later type change.
        self._post({'action': 'edit_party', 'party_id': party_id,
                    'name': 'Auto Form Co', 'party_type': 'lender',
                    'category_id': '', 'phone': '0300-0000000', 'note': ''})
        with self.app.app_context():
            row = db.session.get(CashFlowParty, party_id)
            self.assertIsNone(row.category_id)
            self.assertTrue(row.category_auto)
        self.assertIn('Auto Form Co',
                      self._ledger_table(self._filter(category='loan')))

    def test_bulk_assign_files_every_selected_party(self):
        category_id = self._create_category()
        with self.app.app_context():
            ids = []
            for name in ('Bulk One', 'Bulk Two', 'Bulk Three'):
                party, _ = save_cf_party(name, party_type='other')
                ids.append(party.id)
            db.session.commit()

        response = self._post({
            'action': 'assign_category', 'category_id': category_id,
            'party_ids': [str(pid) for pid in ids[:2]]})
        self.assertIn('2 party(ies) filed', response.get_data(as_text=True))
        with self.app.app_context():
            filed = {int(p.category_id) for p in CashFlowParty.query.filter(
                CashFlowParty.id.in_(ids[:2])).all()}
            untouched = db.session.get(CashFlowParty, ids[2]).category_id
        self.assertEqual(filed, {category_id})
        self.assertNotEqual(untouched, category_id)

        table = self._ledger_table(self._filter(category='cement-dealers'))
        self.assertIn('Bulk One', table)
        self.assertIn('Bulk Two', table)
        self.assertNotIn('Bulk Three', table)

        # Empty category id = every selected party goes back to Automatic.
        self._post({'action': 'assign_category', 'category_id': '',
                    'party_ids': [str(pid) for pid in ids[:2]]})
        with self.app.app_context():
            for pid in ids[:2]:
                self.assertIsNone(db.session.get(CashFlowParty, pid).category_id)

    # ── retire / delete ─────────────────────────────────────────────────────
    def test_retired_category_is_not_offered_but_keeps_its_parties(self):
        category_id = self._create_category('Retired Folder')
        with self.app.app_context():
            party, _ = save_cf_party('Retired Party Co', party_type='supplier',
                                     category_id=category_id)
            db.session.commit()
            party_id = party.id

        self._post({'action': 'toggle_category', 'category_id': category_id})
        with self.app.app_context():
            self.assertFalse(db.session.get(PartyCategory, category_id).is_active)
            self.assertEqual(int(db.session.get(CashFlowParty, party_id).category_id),
                             category_id)

        html = self.client.get(PARTIES_URL).get_data(as_text=True)
        # Still a card/filter (it holds parties), but no longer an option in
        # the Add Party form: the category list is included twice (add + edit).
        self.assertIn('Retired Folder', html)
        self.assertIn('Retired', html)
        add_form = next(body for body in re.findall(
            r'<form\b[^>]*>(.*?)</form>', html, re.S)
            if 'name="action" value="add_party"' in body)
        self.assertNotIn('Retired Folder', add_form)

        # Retiring is not deleting: the party is still shown under the card.
        self.assertIn('Retired Party Co',
                      self._ledger_table(self._filter(category='retired-folder')))

    def test_category_with_parties_cannot_be_deleted_until_empty(self):
        category_id = self._create_category('Delete Me')
        with self.app.app_context():
            save_cf_party('Sticky Party', party_type='other',
                          category_id=category_id)
            db.session.commit()

        response = self._post({'action': 'delete_category',
                               'category_id': category_id})
        self.assertIn('still has 1 party', response.get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(PartyCategory, category_id))

        # Clear the filing, then the empty category can go.
        with self.app.app_context():
            party = CashFlowParty.query.filter_by(name='Sticky Party').one()
            party.category_id = None
            db.session.commit()
        self._post({'action': 'delete_category', 'category_id': category_id})
        with self.app.app_context():
            self.assertIsNone(db.session.get(PartyCategory, category_id))

    def test_deleting_every_category_is_not_undone_by_a_restart(self):
        """The seed is a one-time convenience, not a policy the app re-imposes."""
        with self.app.app_context():
            from hdc.services.party_categories import (
                delete_party_category, ensure_party_category_seed,
            )
            for cat in PartyCategory.query.all():
                delete_party_category(cat, commit=False)
            db.session.commit()
            self.assertEqual(PartyCategory.query.count(), 0)
            self.assertEqual(ensure_party_category_seed(), 0)
            self.assertEqual(PartyCategory.query.count(), 0)

    # ── legacy automatic filing still works ─────────────────────────────────
    def test_automatic_syncs_land_in_the_type_bucket(self):
        with self.app.app_context():
            lender, _ = save_cf_party('Auto Lender', party_type='lender')
            customer, _ = ensure_party('Auto Renter', party_type='rental')
            staff, _ = save_cf_party('Auto Staff', party_type='staff')
            db.session.commit()
            self.assertEqual(lender.category_id,
                             default_party_category_for_type('lender').id)
            self.assertEqual(customer.category_id,
                             default_party_category_for_type('rental').id)
            self.assertEqual(staff.category_id,
                             default_party_category_for_type('staff').id)

    def test_auto_filing_follows_a_type_upgrade(self):
        """A party created as `other` then upgraded to supplier moves cards."""
        with self.app.app_context():
            party, _ = save_cf_party('Upgraded Co', party_type='other')
            db.session.commit()
            self.assertEqual(db.session.get(PartyCategory, party.category_id).key,
                             'other')
            save_cf_party('Upgraded Co', party_type='supplier')
            db.session.commit()
            self.assertEqual(db.session.get(PartyCategory, party.category_id).key,
                             'supplier')
            self.assertTrue(party.category_auto)

    def test_manual_filing_is_not_reset_by_an_automatic_sync(self):
        category_id = self._create_category('Special Folder')
        with self.app.app_context():
            party, _ = save_cf_party('Dual Role Co', party_type='lender',
                                     category_id=category_id)
            db.session.commit()
            ensure_party('Dual Role Co', party_type='rental')
            db.session.commit()
            self.assertEqual(int(party.category_id), category_id)

    # ── permissions ─────────────────────────────────────────────────────────
    def test_staff_cannot_create_categories(self):
        self._as('staff')
        self._post({'action': 'add_category', 'name': 'Staff Made This'})
        with self.app.app_context():
            self.assertIsNone(PartyCategory.query.filter_by(
                name='Staff Made This').first())


if __name__ == '__main__':
    unittest.main()
