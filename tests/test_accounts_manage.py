#!/usr/bin/env python3
"""Regression tests for the unified Accounts list / edit layer.

Covers the pages added to unify the Accounts section with the AMS accounts UI:

  * Manage Accounts  /hdc/accounts/manage   — every account in one list
  * Accounts hub     /hdc/accounts/hub      — landing page / section map
  * Add + Edit       /hdc/accounts/new, /hdc/accounts/<id>/edit

and the service behind them (``hdc/services/accounts_manage.py``):

  * derived balances      — never stored, always opening + in − out
  * classification        — the registry is enforced, not trusted from the form
  * archive-vs-delete     — an account with history is archived, never removed
  * restore               — archiving is reversible without splitting the ledger
  * meaningful warnings   — only money pots count as "overdrawn"; a client
                            ledger in credit is normal double-entry

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \
        python -m unittest tests.test_accounts_manage -v
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
from hdc.models.accounts import Account, AccountTransaction         # noqa: E402
from hdc.models.auth import HDCUser                                 # noqa: E402
from hdc.services.accounts_manage import (                          # noqa: E402
    account_classification_for, account_groups, create_account,
    delete_account, list_manage_accounts, manage_summary,
    restore_account, set_account_status, update_account,
)
from hdc.utils.dates import _pkt_now_naive, _pkt_today               # noqa: E402
from werkzeug.security import generate_password_hash                # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


def _csrf(client, url):
    page = client.get(url).get_data(as_text=True)
    token = re.search(r'name="_csrf_token" value="([^"]+)"', page)
    return token.group(1) if token else ''


class AccountsManageTestCase(unittest.TestCase):
    """Service-level rules for the account master list."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-accmanage-test-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()

        self.cash = self._mk_account('Site Cash Box', 'cash', opening=100000.0)
        self.bank = self._mk_account('Meezan Operating', 'bank', opening=50000.0,
                                     bank_name='Meezan', account_number='1234')
        self.client_acc = self._mk_account('Mr. Akram (Client)', 'client')

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def _login(self):
        token = _csrf(self.client, '/hdc/login')
        res = self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        })
        self.assertIn(res.status_code, (200, 302), 'login failed')

    def _mk_account(self, name, acc_type, opening=0.0, bank_name='',
                    account_number='', status='active'):
        row = Account(name=name, type=acc_type, opening_balance=float(opening),
                      bank_name=bank_name or None, account_number=account_number or None,
                      status=status, is_void=False, created_at=_pkt_now_naive())
        db.session.add(row)
        db.session.commit()
        return Account.query.filter_by(name=name).first()

    def _post_txn(self, from_acc, to_acc, amount, category='transfer'):
        txn = AccountTransaction(
            date=_pkt_today(), amount=float(amount), type='transfer',
            from_account_id=int(from_acc.id), to_account_id=int(to_acc.id),
            executed_by_account_id=int(from_acc.id), category=category,
            is_void=False, created_at=_pkt_now_naive())
        db.session.add(txn)
        db.session.commit()
        return txn

    def _post(self, url, data, referrer=None):
        payload = dict(data)
        payload['_csrf_token'] = _csrf(self.client, referrer or url)
        return self.client.post(url, data=payload)

    # ── listing ──────────────────────────────────────────────────────────

    def test_list_returns_every_active_account_with_derived_balance(self):
        rows = list_manage_accounts(show='active')
        names = {r['name'] for r in rows}
        self.assertIn('Site Cash Box', names)
        self.assertIn('Meezan Operating', names)

        by_name = {r['name']: r for r in rows}
        self.assertEqual(by_name['Site Cash Box']['current_balance'], 100000.0)
        self.assertEqual(by_name['Meezan Operating']['account_mode'], 'bank')

    def test_balance_is_derived_from_the_ledger_not_stored(self):
        """A posted transfer must move both balances immediately."""
        self._post_txn(self.cash, self.bank, 30000.0)
        rows = {r['name']: r for r in list_manage_accounts(show='active')}
        self.assertEqual(rows['Site Cash Box']['current_balance'], 70000.0)
        self.assertEqual(rows['Meezan Operating']['current_balance'], 80000.0)

    def test_voided_transactions_do_not_affect_the_balance(self):
        txn = self._post_txn(self.cash, self.bank, 30000.0)
        txn.is_void = True
        db.session.commit()
        rows = {r['name']: r for r in list_manage_accounts(show='active')}
        self.assertEqual(rows['Site Cash Box']['current_balance'], 100000.0)
        self.assertEqual(rows['Meezan Operating']['current_balance'], 50000.0)

    def test_show_filters_partition_the_accounts(self):
        set_account_status(self.bank.id, 'inactive')
        delete_account(self.client_acc.id)      # no txns -> hard delete

        self.assertIn('Site Cash Box', {r['name'] for r in list_manage_accounts(show='active')})
        self.assertIn('Meezan Operating', {r['name'] for r in list_manage_accounts(show='inactive')})
        self.assertNotIn('Meezan Operating', {r['name'] for r in list_manage_accounts(show='active')})
        # 'all' is the union of the buckets
        everything = {r['name'] for r in list_manage_accounts(show='all')}
        self.assertIn('Site Cash Box', everything)
        self.assertIn('Meezan Operating', everything)

    def test_unknown_show_mode_falls_back_to_active(self):
        rows = list_manage_accounts(show='nonsense')
        self.assertEqual({r['name'] for r in rows},
                         {r['name'] for r in list_manage_accounts(show='active')})

    def test_auto_person_accounts_can_be_hidden(self):
        auto = self._mk_account('Auto Worker Ledger', 'person')
        auto.auto_generated = True
        auto.auto_source = 'worker'
        db.session.commit()

        with_auto = {r['name'] for r in list_manage_accounts(show='active', include_auto_person=True)}
        without = {r['name'] for r in list_manage_accounts(show='active', include_auto_person=False)}
        self.assertIn('Auto Worker Ledger', with_auto)
        self.assertNotIn('Auto Worker Ledger', without)

    # ── classification ───────────────────────────────────────────────────

    def test_legacy_account_gets_a_classification_without_being_written_to(self):
        """A bare legacy row must still display a valid classification."""
        bare = Account(name='Legacy Bare', type='cash', opening_balance=0.0,
                       status='active', is_void=False, created_at=_pkt_now_naive())
        db.session.add(bare)
        db.session.commit()

        info = account_classification_for(bare)
        self.assertEqual(info['category'], 'Assets')
        self.assertEqual(info['channel'], 'cash')
        # derived on read only — the row itself was not modified
        db.session.refresh(bare)
        self.assertIsNone(bare.class_category)

    def test_groups_are_ordered_and_carry_counts_and_balances(self):
        groups = account_groups(list_manage_accounts(show='active'))
        self.assertTrue(groups)
        for g in groups:
            self.assertGreater(g['count'], 0)
            self.assertIn('balance', g)
        # the cash + bank accounts land in Assets
        asset_groups = [g for g in groups if g['category'] == 'Assets']
        self.assertTrue(asset_groups)

    def test_summary_splits_money_by_channel(self):
        # Bootstrap seeds its own default accounts (Company Cash, Credit/Debit
        # Control), so assert on the ones this test created rather than a total.
        rows = list_manage_accounts(show='active')
        summary = manage_summary(rows)
        mine = [r for r in rows if r['name'] in
                ('Site Cash Box', 'Meezan Operating', 'Mr. Akram (Client)')]
        self.assertEqual(len(mine), 3)
        self.assertGreaterEqual(summary['total_accounts'], 3)
        self.assertGreaterEqual(summary['bank_count'], 1)

        bank = next(r for r in mine if r['name'] == 'Meezan Operating')
        self.assertEqual(bank['channel'], 'bank')
        self.assertEqual(bank['current_balance'], 50000.0)
        self.assertIn(bank['name'],
                      [g for grp in account_groups(mine) for g in grp['accounts']])
        # the money total equals the sum of the derived balances
        self.assertAlmostEqual(
            summary['total_balance'],
            sum(float(r['current_balance'] or 0.0) for r in rows), places=2)

    # ── the "overdrawn" warning must not cry wolf ────────────────────────

    def test_client_ledger_in_credit_is_not_reported_as_overdrawn(self):
        """A negative client balance is an advance received, not an overdraft."""
        # Pay the client account out so its ledger runs negative.
        self._post_txn(self.client_acc, self.cash, 25000.0, category='income')
        rows = list_manage_accounts(show='active')
        by_name = {r['name']: r for r in rows}
        self.assertLess(by_name['Mr. Akram (Client)']['current_balance'], 0)

        summary = manage_summary(rows)
        self.assertEqual(summary['negative_count'], 0,
                         'a party ledger in credit must not count as overdrawn')
        self.assertGreaterEqual(summary['party_credit_count'], 1)

    def test_overdrawn_cash_account_is_reported(self):
        self._post_txn(self.cash, self.bank, 150000.0)   # cash opened at 100k
        summary = manage_summary(list_manage_accounts(show='active'))
        self.assertEqual(summary['negative_count'], 1)
        self.assertEqual(summary['negative_accounts'][0]['name'], 'Site Cash Box')

    # ── create ───────────────────────────────────────────────────────────

    def test_create_bank_account_stores_classification_and_paisa_mirror(self):
        row, msg = create_account({
            'name': 'HBL Savings',
            'class_category': 'Assets', 'class_subcategory': 'Bank',
            'class_account_type': 'Savings Bank', 'channel': 'bank',
            'bank_name': 'HBL', 'account_number': '99887766',
            'opening_balance': '250,000.50',
        })
        self.assertIsNotNone(row, msg)
        self.assertEqual(row.type, 'bank')
        self.assertEqual(row.class_account_type, 'Savings Bank')
        self.assertEqual(row.channel, 'bank')
        self.assertEqual(row.opening_balance_minor, 25000050)

    def test_create_wallet_account(self):
        row, msg = create_account({
            'name': 'Easypaisa Wallet',
            'class_category': 'Assets', 'class_subcategory': 'Digital Wallet',
            'class_account_type': 'Mobile Wallet', 'channel': 'digital_wallet',
            'wallet_provider': 'Easypaisa', 'wallet_number': '03001234567',
            'opening_balance': '1500',
        })
        self.assertIsNotNone(row, msg)
        self.assertEqual(row.channel, 'digital_wallet')
        self.assertEqual(row.wallet_provider, 'Easypaisa')
        self.assertIsNone(row.bank_name, 'wallet must not carry bank details')

    def test_create_rejects_bank_channel_without_bank_details(self):
        row, msg = create_account({
            'name': 'Incomplete Bank', 'class_category': 'Assets',
            'class_subcategory': 'Bank', 'class_account_type': 'Operating Bank',
            'channel': 'bank',
        })
        self.assertIsNone(row)
        self.assertIn('Bank name is required', msg)

    def test_create_rejects_wallet_without_provider(self):
        row, msg = create_account({
            'name': 'Incomplete Wallet', 'class_category': 'Assets',
            'class_subcategory': 'Digital Wallet', 'class_account_type': 'Mobile Wallet',
            'channel': 'digital_wallet',
        })
        self.assertIsNone(row)
        self.assertIn('Wallet provider is required', msg)

    def test_create_rejects_illegal_classification_combination(self):
        """Client Ledger is ledger_only — the registry must refuse a bank channel."""
        row, msg = create_account({
            'name': 'Impossible Account', 'class_category': 'Assets',
            'class_subcategory': 'Client Receivables', 'class_account_type': 'Client Ledger',
            'channel': 'bank', 'bank_name': 'X', 'account_number': '1',
        })
        self.assertIsNone(row)
        self.assertIn('Invalid classification', msg)

    def test_create_rejects_duplicate_and_empty_name(self):
        row, msg = create_account({
            'name': 'Site Cash Box', 'class_category': 'Assets',
            'class_subcategory': 'Cash', 'class_account_type': 'Main Cash', 'channel': 'cash',
        })
        self.assertIsNone(row)
        self.assertIn('already exists', msg)

        row, msg = create_account({'name': '   ', 'class_category': 'Assets',
                                   'class_subcategory': 'Cash',
                                   'class_account_type': 'Main Cash', 'channel': 'cash'})
        self.assertIsNone(row)
        self.assertIn('name is required', msg)

    def test_create_clears_irrelevant_channel_details(self):
        """A cash account must not silently keep submitted bank fields."""
        row, msg = create_account({
            'name': 'Petty Drawer', 'class_category': 'Assets',
            'class_subcategory': 'Cash', 'class_account_type': 'Petty Cash',
            'channel': 'cash', 'cash_location': 'Head office',
            'bank_name': 'Should Be Dropped', 'account_number': '999',
            'iban': 'PK00X',
        })
        self.assertIsNotNone(row, msg)
        self.assertIsNone(row.bank_name)
        self.assertIsNone(row.account_number)
        self.assertIsNone(row.iban)
        self.assertEqual(row.cash_location, 'Head office')

    def test_registry_forces_the_required_linked_entity(self):
        """A Client Ledger must be linked to a client even if the form says none."""
        row, msg = create_account({
            'name': 'Client Ledger X', 'class_category': 'Assets',
            'class_subcategory': 'Client Receivables', 'class_account_type': 'Client Ledger',
            'channel': 'ledger_only', 'linked_entity_type': 'none',
        })
        self.assertIsNotNone(row, msg)
        self.assertEqual(row.linked_entity_type, 'client')

    # ── update ───────────────────────────────────────────────────────────

    def test_update_reclassifies_and_renames(self):
        row, msg = update_account(self.bank.id, {
            'name': 'Meezan Collection', 'class_category': 'Assets',
            'class_subcategory': 'Bank', 'class_account_type': 'Collection Bank',
            'channel': 'bank', 'bank_name': 'Meezan', 'account_number': '1234',
            'opening_balance': '75000',
        })
        self.assertIsNotNone(row, msg)
        self.assertEqual(row.name, 'Meezan Collection')
        self.assertEqual(row.class_account_type, 'Collection Bank')
        self.assertEqual(row.opening_balance, 75000.0)
        self.assertEqual(row.opening_balance_minor, 7500000)

    def test_update_keeps_stored_values_when_fields_are_blank(self):
        """A partial payload must not wipe the account."""
        row, msg = update_account(self.bank.id, {'name': 'Meezan Operating'})
        self.assertIsNotNone(row, msg)
        self.assertEqual(row.bank_name, 'Meezan')
        self.assertEqual(row.account_number, '1234')
        self.assertEqual(row.opening_balance, 50000.0)

    def test_update_rejects_a_name_taken_by_another_account(self):
        row, msg = update_account(self.bank.id, {'name': 'Site Cash Box'})
        self.assertIsNone(row)
        self.assertIn('already exists', msg)

    def test_update_rejects_unknown_account(self):
        row, msg = update_account(999999, {'name': 'Ghost'})
        self.assertIsNone(row)
        self.assertIn('not found', msg)

    # ── status ───────────────────────────────────────────────────────────

    def test_suspend_then_reactivate(self):
        row, msg = set_account_status(self.cash.id, 'inactive')
        self.assertIsNotNone(row, msg)
        self.assertEqual(row.status, 'inactive')

        row, msg = set_account_status(self.cash.id, 'active')
        self.assertIsNotNone(row, msg)
        self.assertEqual(row.status, 'active')

    def test_rejects_unknown_status(self):
        row, msg = set_account_status(self.cash.id, 'deleted')
        self.assertIsNone(row)
        self.assertIn('must be', msg)

    # ── delete / archive / restore ───────────────────────────────────────

    def test_delete_without_history_removes_the_row(self):
        _row, msg, archived = delete_account(self.client_acc.id)
        self.assertFalse(archived)
        self.assertIn('deleted', msg)
        self.assertIsNone(Account.query.get(self.client_acc.id))

    def test_delete_with_history_archives_and_preserves_the_ledger(self):
        self._post_txn(self.cash, self.bank, 10000.0)
        before = AccountTransaction.query.filter_by(from_account_id=self.cash.id).count()

        _row, msg, archived = delete_account(self.cash.id)
        self.assertTrue(archived)
        self.assertIn('archived', msg)

        row = Account.query.get(self.cash.id)
        self.assertIsNotNone(row, 'the row must survive — it carries history')
        self.assertTrue(row.is_void)
        self.assertEqual(row.status, 'archived')
        self.assertEqual(
            AccountTransaction.query.filter_by(from_account_id=self.cash.id).count(),
            before, 'ledger rows must be untouched')

    def test_archived_account_appears_in_the_archived_bucket_only(self):
        self._post_txn(self.cash, self.bank, 10000.0)
        delete_account(self.cash.id)

        active = {r['name'] for r in list_manage_accounts(show='active')}
        archived = {r['name'] for r in list_manage_accounts(show='archived')}
        self.assertNotIn('Site Cash Box', active)
        self.assertIn('Site Cash Box', archived)

        row = next(r for r in list_manage_accounts(show='archived')
                   if r['name'] == 'Site Cash Box')
        self.assertEqual(row['status'], 'archived')
        self.assertGreater(row['txn_count'], 0)

    def test_status_change_is_refused_on_an_archived_account(self):
        self._post_txn(self.cash, self.bank, 10000.0)
        delete_account(self.cash.id)
        row, msg = set_account_status(self.cash.id, 'active')
        self.assertIsNone(row)
        self.assertIn('archived', msg)

    def test_restore_brings_the_account_back_with_history_intact(self):
        self._post_txn(self.cash, self.bank, 10000.0)
        delete_account(self.cash.id)

        row, msg = restore_account(self.cash.id)
        self.assertIsNotNone(row, msg)
        self.assertFalse(row.is_void)
        self.assertEqual(row.status, 'active')
        self.assertEqual(row.opening_balance, 100000.0)
        # balance is still derived from the same ledger — nothing was lost
        listed = {r['name']: r for r in list_manage_accounts(show='active')}
        self.assertEqual(listed['Site Cash Box']['current_balance'], 90000.0)

    def test_restore_refuses_when_the_name_was_taken_meanwhile(self):
        self._post_txn(self.cash, self.bank, 10000.0)
        delete_account(self.cash.id)
        self._mk_account('Site Cash Box', 'cash', opening=1.0)   # squats the name

        row, msg = restore_account(self.cash.id)
        self.assertIsNone(row)
        self.assertIn('already named', msg)


class AccountsManageRouteTestCase(unittest.TestCase):
    """The three new pages render and stay admin-only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-accroute-test-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()
        self.cash = Account(name='Route Cash', type='cash', opening_balance=5000.0,
                            status='active', is_void=False, created_at=_pkt_now_naive())
        db.session.add(self.cash)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self):
        token = _csrf(self.client, '/hdc/login')
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        })

    def _post(self, url, data, referrer=None):
        payload = dict(data)
        payload['_csrf_token'] = _csrf(self.client, referrer or url)
        return self.client.post(url, data=payload)

    def test_hub_manage_and_new_pages_render(self):
        for url in ('/hdc/accounts/hub', '/hdc/accounts/manage', '/hdc/accounts/new'):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, url)

    def test_manage_lists_the_account_and_explains_itself(self):
        html = self.client.get('/hdc/accounts/manage?show=all').get_data(as_text=True)
        self.assertIn('Route Cash', html)
        self.assertIn('Manage Accounts', html)
        # the AMS-style building blocks are present
        self.assertIn('acc-stat', html)
        self.assertIn('acc-chip', html)
        self.assertIn('acc-filter-bar', html)

    def test_show_all_includes_every_status_bucket(self):
        for mode in ('active', 'inactive', 'archived', 'all'):
            res = self.client.get(f'/hdc/accounts/manage?show={mode}')
            self.assertEqual(res.status_code, 200, mode)

    def test_edit_page_renders_with_the_classification_tree(self):
        html = self.client.get(f'/hdc/accounts/{self.cash.id}/edit').get_data(as_text=True)
        self.assertIn('Route Cash', html)
        self.assertIn('accClassificationTree', html)
        self.assertIn('Operating Bank', html)      # registry projected into the page

    def test_edit_unknown_account_404s(self):
        self.assertEqual(self.client.get('/hdc/accounts/999999/edit').status_code, 404)

    def test_hub_explains_what_each_page_is_for(self):
        html = self.client.get('/hdc/accounts/hub').get_data(as_text=True)
        self.assertIn('What Each Page Is For', html)
        for label in ('Manage Accounts', 'CF Register', 'Cash Flow', 'Day Close', 'All Entries'):
            self.assertIn(label, html)
        # the Cash Flow vs CF Register distinction the section was missing
        self.assertIn('Cash Flow vs CF Register', html)

    def test_csv_export_returns_the_list(self):
        res = self.client.get('/hdc/accounts/manage/export?show=all')
        self.assertEqual(res.status_code, 200)
        self.assertIn('text/csv', res.headers.get('Content-Type', ''))
        body = res.get_data(as_text=True)
        self.assertIn('Route Cash', body)
        self.assertIn('Current Balance', body)

    def test_post_actions_redirect_back_to_the_list(self):
        res = self._post('/hdc/accounts/manage?show=active',
                         {'action': 'suspend_account', 'account_id': self.cash.id},
                         referrer='/hdc/accounts/manage')
        self.assertEqual(res.status_code, 302)
        self.assertEqual(Account.query.get(self.cash.id).status, 'inactive')

    def test_pages_are_admin_only(self):
        db.session.add(HDCUser(username='staff', role='accountant',
                               password_hash=generate_password_hash(ADMIN_PASSWORD)))
        db.session.commit()

        # Flask's test clients share one cookie jar per app, so the admin
        # session from setUp would otherwise leak into the "staff" client and
        # the page would legitimately answer 200.  Log out first — that clears
        # the shared session cookie for every client on this app.
        token = _csrf(self.client, '/hdc/accounts/hub')
        self.client.post('/hdc/logout', data={'_csrf_token': token})
        self.assertEqual(self.client.get('/hdc/accounts/hub').status_code, 302,
                         'logout must end the admin session')

        client = self.app.test_client()
        token = _csrf(client, '/hdc/login')
        self.assertTrue(token, 'login page must expose a CSRF token')
        res = client.post('/hdc/login', data={'username': 'staff',
                                              'password': ADMIN_PASSWORD,
                                              '_csrf_token': token})
        self.assertEqual(res.status_code, 302, 'staff login should succeed')

        for url in ('/hdc/accounts/hub', '/hdc/accounts/manage',
                    '/hdc/accounts/new', f'/hdc/accounts/{self.cash.id}/edit',
                    '/hdc/accounts/manage/export'):
            res = client.get(url)
            self.assertEqual(res.status_code, 302, f'{url} must redirect a non-admin')
            location = res.headers.get('Location', '')
            self.assertIn('/hdc/', location)
            self.assertNotIn('/hdc/accounts', location,
                             'must be bounced away from the Accounts section')
            # _admin_only() flashes the reason; it survives on the session and
            # is rendered by the page we land on.
            page = client.get(location).get_data(as_text=True)
            self.assertIn('Admin access required', page, url)


if __name__ == '__main__':
    unittest.main(verbosity=2)
