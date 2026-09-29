#!/usr/bin/env python3
"""Regression tests for the Shared Expenses module (Accounts section).

What is pinned down here, in the order the module is used:

  * **the split engine** — integer paisa, odd paisa handed out, percent and
    custom splits that must re-add to the total, duplicates refused;
  * **one ledger, never two** — paying a shared expense from the form posts
    exactly one ``hdc_account_txn`` row (through the Cash Flow register
    engine) and links it; splitting it creates no money of its own;
  * **derived ledgers** — balances are shares − paid − settled out + settled in,
    the whole module balances to zero, and voiding one side moves both;
  * **settlements** — recording, the optional Accounts transfer, void/restore;
  * **access** — the module sits inside the admin-only Accounts section;
  * **exports** — CSV of the expense list and of the share report.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_shared_expenses -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app                                       # noqa: E402
from hdc.extensions import db                                        # noqa: E402
from hdc.models.accounts import Account, AccountTransaction          # noqa: E402
from hdc.models.auth import HDCUser                                  # noqa: E402
from hdc.models.cashflow import CashFlowEntry                        # noqa: E402
from hdc.models.shared_expenses import (                             # noqa: E402
    SharedExpense, SharedExpenseShare, SharedParty, SharedSettlement,
)
from hdc.services.cashflow_register import (                         # noqa: E402
    category_options, save_manual_cash_flow_entry,
)
from hdc.services.shared_expenses import (                           # noqa: E402
    balance_state, compute_split, ensure_shared_expense_seed_data,
    link_accounts_entry, ledger_lines, module_summary, money_text,
    party_balances, report_matrix, save_settlement, save_shared_expense,
    settlement_query, void_settlement, void_shared_expense,
)
from hdc.utils.dates import _pkt_now_naive                           # noqa: E402
from hdc.utils.money import from_minor, to_minor                     # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPLIT_HARNESS = os.path.join(REPO_ROOT, 'tests', 'shared_expense_split_harness.js')


def _csrf(client, url):
    page = client.get(url).get_data(as_text=True)
    token = re.search(r'name="_csrf_token" value="([^"]+)"', page)
    return token.group(1) if token else ''


class SplitEngineTestCase(unittest.TestCase):
    """Pure arithmetic — the part that must never be off by a paisa."""

    def test_equal_split_hands_out_the_odd_paisa(self):
        rows = compute_split('equal', 500000, [{'party_id': 1}, {'party_id': 2}, {'party_id': 3}])
        self.assertEqual([r['amount_minor'] for r in rows], [166667, 166667, 166666])
        self.assertEqual(sum(r['amount_minor'] for r in rows), 500000)

    def test_equal_split_of_five_paisa_between_three(self):
        rows = compute_split('equal', 5, [{'party_id': i} for i in (1, 2, 3)])
        self.assertEqual([r['amount_minor'] for r in rows], [2, 2, 1])
        self.assertEqual(sum(r['amount_minor'] for r in rows), 5)

    def test_equal_split_readds_to_total_for_many_sizes(self):
        for total in range(1, 40):
            rows = compute_split('equal', total, [{'party_id': i} for i in range(1, 6)])
            self.assertEqual(sum(r['amount_minor'] for r in rows), total, f'total {total}')

    def test_percent_split_readds_to_total(self):
        rows = compute_split('percent', 500000, [
            {'party_id': 1, 'percent': '33.33'},
            {'party_id': 2, 'percent': '33.33'},
            {'party_id': 3, 'percent': '33.34'},
        ])
        self.assertEqual(sum(r['amount_minor'] for r in rows), 500000)
        # 33.33% of 5,000 is 1,666.50 exactly; the last slice takes the balance.
        self.assertEqual([r['amount_minor'] for r in rows], [166650, 166650, 166700])

    def test_percent_must_add_up_to_100(self):
        with self.assertRaises(ValueError) as ctx:
            compute_split('percent', 100000, [{'party_id': 1, 'percent': '60'},
                                              {'party_id': 2, 'percent': '30'}])
        self.assertIn('100', str(ctx.exception))

    def test_custom_split_must_match_the_total(self):
        rows = compute_split('custom', 500000, [
            {'party_id': 1, 'amount': '2000'},
            {'party_id': 2, 'amount': '1500'},
            {'party_id': 3, 'amount': '1500'},
        ])
        self.assertEqual(sum(r['amount_minor'] for r in rows), 500000)
        with self.assertRaises(ValueError) as ctx:
            compute_split('custom', 500000, [
                {'party_id': 1, 'amount': '2000'},
                {'party_id': 2, 'amount': '1000'},
            ])
        self.assertIn('add', str(ctx.exception).lower())

    def test_refuses_empty_and_duplicate_parties(self):
        with self.assertRaises(ValueError):
            compute_split('equal', 1000, [])
        with self.assertRaises(ValueError):
            compute_split('equal', 1000, [{'party_id': 1}, {'party_id': 1}])

    def test_percent_split_reads_the_amount_when_no_percentage_is_typed(self):
        """The form shows both columns live, so a percentage the operator did
        not type is worked out from the rupees rather than refused."""
        rows = compute_split('percent', 900000, [
            {'party_id': 1, 'amount': '4,500'},
            {'party_id': 2, 'amount': '2,700'},
            {'party_id': 3, 'amount': '1,800'},
        ])
        self.assertEqual([r['amount_minor'] for r in rows], [450000, 270000, 180000])
        self.assertEqual([r['percent_bp'] for r in rows], [5000, 3000, 2000])

    def test_custom_split_reads_the_percentage_when_no_amount_is_typed(self):
        rows = compute_split('custom', 900000, [
            {'party_id': 1, 'percent': '50'},
            {'party_id': 2, 'percent': '30'},
            {'party_id': 3, 'percent': '20'},
        ])
        self.assertEqual(sum(r['amount_minor'] for r in rows), 900000)
        self.assertEqual([r['amount_minor'] for r in rows], [450000, 270000, 180000])

    def test_a_row_with_neither_figure_is_still_refused(self):
        with self.assertRaises(ValueError) as ctx:
            compute_split('percent', 900000, [
                {'party_id': 1, 'percent': '50'},
                {'party_id': 2},
            ])
        self.assertIn('percentage', str(ctx.exception).lower())
        with self.assertRaises(ValueError) as ctx:
            compute_split('custom', 900000, [
                {'party_id': 1, 'amount': '4500'},
                {'party_id': 2},
            ])
        self.assertIn('amount', str(ctx.exception).lower())

    def test_percentages_derived_from_an_odd_paisa_split_say_so(self):
        """A three-way equal split is 33.333…% each.  Rounded to the two
        decimals a percentage carries it adds up to 99.99%, so the engine says
        so instead of quietly shaving a rupee off somebody's share — which is
        exactly why Equal split is there for an even divide."""
        with self.assertRaises(ValueError) as ctx:
            compute_split('percent', 500000, [
                {'party_id': 1, 'amount': '1,666.67'},
                {'party_id': 2, 'amount': '1,666.67'},
                {'party_id': 3, 'amount': '1,666.66'},
            ])
        self.assertIn('99.99', str(ctx.exception))

    def test_balance_state_reads_like_a_sentence(self):
        self.assertEqual(balance_state(0)['state'], 'settled')
        self.assertIn('Owes', balance_state(150000)['text'])
        self.assertIn('Gets back', balance_state(-150000)['text'])
        self.assertIn('1,500.00', balance_state(150000)['text'])


class SharedExpenseAppTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-shared-test-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()
        self.cash = self._mk_account('Test Cash', 'cash', opening=500000.0)
        # The seed runs on bootstrap in production; the test fixtures created
        # their own DB after that, so run it once here.
        ensure_shared_expense_seed_data()
        self.parties = {p.name: p for p in SharedParty.query.all()}
        self.actor = 'admin'

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def _login(self, username='admin'):
        # Start from a clean session: the login page only renders its CSRF token
        # when nobody is signed in.
        with self.client.session_transaction() as sess:
            sess.clear()
        token = _csrf(self.client, '/hdc/login')
        res = self.client.post('/hdc/login', data={
            'username': username, 'password': ADMIN_PASSWORD, '_csrf_token': token,
        })
        self.assertIn(res.status_code, (200, 302), 'login failed')

    def _act_as(self, user):
        """Sign the test client in as ``user`` without touching the login form.

        ``g`` belongs to the *app* context, and this class keeps one pushed for
        the whole test (see ``setUp``).  flask_login caches the loaded user on
        ``g._login_user``, so the identity from the first request would win for
        every later one; drop the cache whenever the session changes.
        """
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['_csrf_token'] = 'shared-test'
            sess['_user_id'] = str(user.id)
            sess['_fresh'] = True
        from flask import g
        g.pop('_login_user', None)

    def _mk_account(self, name, acc_type, opening=0.0, status='active'):
        row = Account(name=name, type=acc_type, opening_balance=float(opening),
                      status=status, is_void=False, created_at=_pkt_now_naive())
        db.session.add(row)
        db.session.commit()
        return Account.query.filter_by(name=name).first()

    def _post(self, url, data, referrer=None):
        payload = dict(data)
        payload['_csrf_token'] = _csrf(self.client, referrer or url)
        return self.client.post(url, data=payload, follow_redirects=False)

    def _new_expense_form(self, **overrides):
        parts = {name: str(p.id) for name, p in self.parties.items()}
        form = {
            'party_ids': [parts['FBM'], parts['HDC'], parts['Home']],
            'date': '2026-09-01',
            'title': 'Car Fuel',
            'category': 'Car Fuel',
            'total_amount': '5000',
            'split_mode': 'equal',
            'money_source': 'post',
            'account_id': str(self.cash.id),
            'idempotency_key': 'test-key-1',
        }
        form.update(overrides)
        return form

    def _expense(self, **overrides):
        """Create a shared expense through the service (3-way equal split)."""
        payload = {
            'date': '2026-09-01', 'title': 'Car Fuel', 'category': 'Car Fuel',
            'total_amount': '5000', 'split_mode': 'equal',
            'payer_party_id': str(self.parties['FBM'].id),
            'money_source': 'none', 'idempotency_key': overrides.pop('idempotency_key', None),
        }
        payload.update(overrides)

        class _Form(dict):
            def getlist(self, key):
                value = self.get(key)
                if value is None:
                    return []
                return value if isinstance(value, list) else [value]

        form = _Form({
            'party_ids': [str(self.parties[n].id) for n in ('FBM', 'HDC', 'Home')],
        })
        expense, _created = save_shared_expense(payload=payload, form=form,
                                                actor=self.actor, commit=True)
        return expense

    def _apply_expense_form(self, expense_id, **overrides):
        parts = {name: str(p.id) for name, p in self.parties.items()}
        form = self._new_expense_form(**overrides)
        form['party_ids'] = [parts['FBM'], parts['HDC'], parts['Home']]
        return self._post(f'/hdc/accounts/shared/expenses/{expense_id}/edit', form,
                          referrer=f'/hdc/accounts/shared/expenses/{expense_id}/edit')

    # ── pages + access ───────────────────────────────────────────────────

    def test_ledger_seeds_the_named_heads(self):
        names = {p.name for p in SharedParty.query.all()}
        self.assertTrue({'FBM', 'HDC', 'Home'}.issubset(names))

    def test_pages_render_for_admin(self):
        expense = self._expense()
        for url in ('/hdc/accounts/shared',
                    '/hdc/accounts/shared/expenses',
                    '/hdc/accounts/shared/expenses/new',
                    f'/hdc/accounts/shared/expenses/{expense.id}',
                    f'/hdc/accounts/shared/expenses/{expense.id}/edit',
                    '/hdc/accounts/shared/report',
                    '/hdc/accounts/shared/settlements',
                    '/hdc/accounts/shared/parties'):
            with self.subTest(url=url):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 200)
                self.assertIn('Shared Expense', res.get_data(as_text=True))

    def test_staff_cannot_reach_the_module(self):
        """The module lives in the admin-only Accounts section, so staff is out."""
        self.assertEqual((HDCUser.query.filter_by(username='admin').one().role or ''),
                         'admin')
        user = HDCUser(username='shared-staff', password_hash='unused', role='staff')
        db.session.add(user)
        db.session.commit()
        self._act_as(user)
        for url in ('/hdc/accounts/shared', '/hdc/accounts/shared/expenses',
                    '/hdc/accounts/shared/parties'):
            with self.subTest(url=url):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 302)
                self.assertEqual(res.headers['Location'], '/hdc/')

    def test_route_prefix_is_classified_in_the_access_matrix(self):
        from hdc.extensions import _access_matrix_roles
        self.assertEqual(_access_matrix_roles('/hdc/accounts/shared', 'read'),
                         {'admin', 'accountant'})

    # ── posting through Accounts ─────────────────────────────────────────

    def test_paying_a_shared_expense_posts_exactly_one_ledger_row(self):
        before = AccountTransaction.query.count()
        res = self._post('/hdc/accounts/shared/expenses/new',
                         self._new_expense_form(), referrer='/hdc/accounts/shared/expenses/new')
        self.assertEqual(res.status_code, 302)
        expense = SharedExpense.query.order_by(SharedExpense.id.desc()).first()
        self.assertIsNotNone(expense)
        self.assertEqual(expense.total_minor, 500000)
        self.assertEqual(AccountTransaction.query.count(), before + 1,
                         'one payment, one ledger row — the split adds none')
        self.assertIsNotNone(expense.cf_entry_id)
        entry = db.session.get(CashFlowEntry, expense.cf_entry_id)
        self.assertEqual(entry.source_type, 'shared_expense')
        self.assertEqual(int(entry.source_id), int(expense.id))
        self.assertEqual(entry.direction, 'out')
        self.assertEqual(int(entry.account_tx_id), int(expense.txn_id))

    def test_double_submit_cannot_create_two_expenses(self):
        form = self._new_expense_form(idempotency_key='double-click')
        self._post('/hdc/accounts/shared/expenses/new', form,
                   referrer='/hdc/accounts/shared/expenses/new')
        first = SharedExpense.query.order_by(SharedExpense.id.desc()).first()
        self._post('/hdc/accounts/shared/expenses/new', form,
                   referrer='/hdc/accounts/shared/expenses/new')
        self.assertEqual(SharedExpense.query.count(), 1)
        self.assertEqual(SharedExpense.query.first().id, first.id)

    def test_bad_split_is_refused_and_nothing_is_posted(self):
        form = self._new_expense_form(split_mode='custom', total_amount='5000',
                                      idempotency_key='bad-split')
        form['party_ids'] = [str(self.parties['FBM'].id), str(self.parties['HDC'].id)]
        form[f"amount_party_{self.parties['FBM'].id}"] = '1000'
        form[f"amount_party_{self.parties['HDC'].id}"] = '1000'
        res = self._post('/hdc/accounts/shared/expenses/new', form,
                         referrer='/hdc/accounts/shared/expenses/new')
        self.assertEqual(res.status_code, 302)
        self.assertEqual(SharedExpense.query.count(), 0,
                         'a split that does not add up must not create a record')
        self.assertEqual(AccountTransaction.query.count(), 0,
                         'and must not post money either')

    def test_overdraft_is_refused_by_the_accounts_engine(self):
        broke = self._mk_account('Empty Cash', 'cash', opening=10.0)
        form = self._new_expense_form(account_id=str(broke.id), idempotency_key='broke')
        self._post('/hdc/accounts/shared/expenses/new', form,
                   referrer='/hdc/accounts/shared/expenses/new')
        self.assertEqual(SharedExpense.query.count(), 0)
        self.assertEqual(AccountTransaction.query.count(), 0)

    def test_unlinked_expense_can_be_linked_later(self):
        expense = self._expense()
        self.assertIsNone(expense.cf_entry_id)
        cats = {c.name: c for c in category_options()}
        entry, _ = save_manual_cash_flow_entry(
            direction='out', amount=5000, account_id=self.cash.id,
            category_id=cats['Material & Purchase'].id,
            description='Car fuel (paid in Accounts)', actor=self.actor)
        db.session.commit()
        link_accounts_entry(expense, cf_entry_id=entry.id)
        db.session.commit()
        self.assertEqual(int(expense.cf_entry_id), int(entry.id))
        self.assertEqual(int(expense.txn_id), int(entry.account_tx_id))

    def test_one_payment_cannot_fund_two_shared_expenses(self):
        cats = {c.name: c for c in category_options()}
        entry, _ = save_manual_cash_flow_entry(
            direction='out', amount=1000, account_id=self.cash.id,
            category_id=cats['Material & Purchase'].id, actor=self.actor)
        db.session.commit()
        first = self._expense(title='First', total_amount='1000')
        link_accounts_entry(first, cf_entry_id=entry.id)
        db.session.commit()
        second = self._expense(title='Second', total_amount='1000')
        with self.assertRaises(ValueError):
            link_accounts_entry(second, cf_entry_id=entry.id)

    # ── derived ledgers ──────────────────────────────────────────────────

    def test_balances_follow_shares_and_payments(self):
        expense = self._expense(payer_party_id=str(self.parties['FBM'].id))
        rows, outside, totals = party_balances()
        by_name = {r['party_name']: r for r in rows}
        self.assertEqual(by_name['FBM']['paid_minor'], 500000)
        self.assertEqual(by_name['FBM']['share_minor'], 166667)
        self.assertEqual(by_name['FBM']['balance_minor'], 166667 - 500000)
        self.assertEqual(by_name['HDC']['balance_minor'], 166667)
        self.assertEqual(by_name['Home']['balance_minor'], 166666)
        self.assertEqual(totals['balance_minor'], 0)
        self.assertTrue(totals['balanced'])
        self.assertEqual(outside['unattributed_minor'], 0)

    def test_unattributed_payment_is_reported_not_hidden(self):
        self._expense(payer_party_id=None)
        rows, outside, totals = party_balances()
        self.assertEqual(outside['unattributed_minor'], 500000)
        self.assertEqual(outside['balance_minor'], -500000)
        self.assertTrue(totals['balanced'])

    def test_voiding_the_expense_removes_it_from_the_ledger(self):
        expense = self._expense()
        self.assertTrue(ledger_lines())
        void_shared_expense(expense, reason='Duplicate entry', actor=self.actor)
        self.assertEqual(ledger_lines(), [])
        rows, _outside, totals = party_balances()
        self.assertEqual(totals['balance_minor'], 0)

    def test_voiding_a_posted_expense_voids_its_accounts_entry(self):
        form = self._new_expense_form(idempotency_key='void-me')
        self._post('/hdc/accounts/shared/expenses/new', form,
                   referrer='/hdc/accounts/shared/expenses/new')
        expense = SharedExpense.query.first()
        entry = db.session.get(CashFlowEntry, expense.cf_entry_id)
        void_shared_expense(expense, reason='Cancelled', actor=self.actor,
                            void_linked_entry=True)
        db.session.commit()
        self.assertTrue(expense.is_void)
        self.assertTrue(entry.is_void)
        tx = db.session.get(AccountTransaction, entry.account_tx_id)
        self.assertTrue(tx.is_void)
        self.assertEqual(AccountTransaction.query.filter_by(is_void=False).count(), 0)

    def test_report_matrix_totals_match_the_expenses(self):
        self._expense(title='Fuel', total_amount='5000')
        self._expense(title='Electricity', total_amount='3000',
                      category='Utilities', idempotency_key=None)
        parties, rows, totals = report_matrix()
        self.assertEqual(len(rows), 2)
        self.assertEqual(totals['total_minor'], 800000)
        for party in parties:
            shares = sum(r['cells'].get(int(party.id), 0) for r in rows)
            self.assertEqual(shares, totals['cells'][int(party.id)])

    def test_summary_counts_unlinked_expenses(self):
        self._expense()
        summary = module_summary()
        self.assertEqual(summary['unlinked_count'], 1)
        self.assertEqual(summary['total_minor'], 500000)
        # FBM fronted the bill, so FBM is owed the two other shares.
        self.assertEqual(summary['owed_in_minor'], 333333)
        self.assertEqual(summary['owed_out_minor'], 333333)

    # ── settlements ──────────────────────────────────────────────────────

    def test_settlement_moves_the_balances_and_can_post_a_transfer(self):
        expense = self._expense(payer_party_id=str(self.parties['HDC'].id))
        bank = self._mk_account('Settle Bank', 'bank', opening=100000.0)
        fbm = self.parties['FBM']
        settlement = save_settlement(payload={
            'date': '2026-09-05',
            'from_party_id': str(fbm.id),
            'to_party_id': str(self.parties['HDC'].id),
            'amount': '1666.67',
            'money_source': 'post',
            'from_account_id': str(self.cash.id),
            'settlement_to_account_id': str(bank.id),
        }, actor=self.actor)
        rows, _o, totals = party_balances()
        by_name = {r['party_name']: r for r in rows}
        self.assertEqual(by_name['FBM']['balance_minor'], 0)          # share paid off
        # HDC fronted the whole bill: 1,666.67 share - 5,000 paid + 1,666.67 received.
        self.assertEqual(by_name['HDC']['balance_minor'], -166666)
        self.assertTrue(totals['balanced'])
        self.assertIsNotNone(settlement.cf_entry_id)
        entry = db.session.get(CashFlowEntry, settlement.cf_entry_id)
        self.assertEqual(entry.direction, 'transfer')
        self.assertEqual(entry.source_type, 'shared_settlement')
        self.assertEqual(expense.party_count, 3)

        void_settlement(settlement, reason='Wrong month', actor=self.actor)
        rows, _o, _t = party_balances()
        by_name = {r['party_name']: r for r in rows}
        self.assertEqual(by_name['FBM']['balance_minor'], 166667)
        self.assertTrue(settlement.is_void)
        self.assertTrue(db.session.get(CashFlowEntry, settlement.cf_entry_id).is_void)

    def test_settlement_needs_exactly_one_receiver(self):
        fbm = self.parties['FBM']
        with self.assertRaises(ValueError):
            save_settlement(payload={
                'date': '2026-09-05', 'from_party_id': str(fbm.id),
                'amount': '100',
            }, actor=self.actor)
        with self.assertRaises(ValueError):
            save_settlement(payload={
                'date': '2026-09-05', 'from_party_id': str(fbm.id),
                'to_party_id': str(self.parties['HDC'].id),
                'to_account_id': str(self.cash.id), 'amount': '100',
            }, actor=self.actor)

    def test_settlement_query_hides_voided_by_default(self):
        fbm = self.parties['FBM']
        save_settlement(payload={
            'date': '2026-09-05', 'from_party_id': str(fbm.id),
            'to_party_id': str(self.parties['HDC'].id), 'amount': '500',
        }, actor=self.actor)
        self.assertEqual(settlement_query().count(), 1)
        self.assertEqual(settlement_query(status='all').count(), 1)

    # ── exports + pages with data ────────────────────────────────────────

    def test_csv_exports(self):
        self._expense()
        for url in ('/hdc/accounts/shared/expenses.csv',
                    '/hdc/accounts/shared/report.csv'):
            with self.subTest(url=url):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 200)
                self.assertIn('text/csv', res.headers['Content-Type'])
                body = res.get_data(as_text=True)
                self.assertIn('Car Fuel', body)

    def test_pages_render_with_a_voided_expense(self):
        expense = self._expense()
        void_shared_expense(expense, reason='test', actor=self.actor)
        for url in ('/hdc/accounts/shared?status=all',
                    '/hdc/accounts/shared/report?status=all'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_ledger_page_shows_the_balance_sentence(self):
        self._expense()
        page = self.client.get('/hdc/accounts/shared').get_data(as_text=True)
        self.assertIn('Owes', page)
        self.assertIn('Gets back', page)

    def test_new_page_renders_the_split_table(self):
        page = self.client.get('/hdc/accounts/shared/expenses/new').get_data(as_text=True)
        for name in ('FBM', 'HDC', 'Home'):
            self.assertIn(name, page)
        self.assertIn('equal split', page.lower())
        self.assertIn('money_source', page)

    def test_a_new_form_starts_with_the_default_heads_ticked(self):
        """Untouched form: the default heads are already in the split, so the
        rupees appear the moment the total is typed."""
        page = self.client.get('/hdc/accounts/shared/expenses/new').get_data(as_text=True)
        ticked = re.findall(r'name="party_ids" value="(\d+)"\s+class="form-check-input '
                            r'se-party-tick"\s+checked', page)
        expected = sorted(str(p.id) for p in self.parties.values() if p.is_default)
        self.assertEqual(sorted(ticked), expected)
        self.assertTrue(expected, 'the fixture must seed default sharing heads')

    def test_a_refused_form_keeps_the_heads_the_operator_ticked(self):
        """After a refusal the draft comes back exactly as typed — including a
        head the operator deliberately left out."""
        parts = {name: str(p.id) for name, p in self.parties.items()}
        form = self._new_expense_form(split_mode='custom')
        form['party_ids'] = [parts['FBM'], parts['HDC']]      # Home left out
        form['amount_party_' + parts['FBM']] = '1000'
        form['amount_party_' + parts['HDC']] = '1000'
        res = self._post('/hdc/accounts/shared/expenses/new', form)
        self.assertEqual(res.status_code, 302, 'a short custom split must be refused')
        page = self.client.get('/hdc/accounts/shared/expenses/new?restore=1')\
            .get_data(as_text=True)
        self.assertIn('Nothing was saved', page)
        ticked = re.findall(r'name="party_ids" value="(\d+)"\s+class="form-check-input '
                            r'se-party-tick"\s+checked', page)
        self.assertEqual(sorted(ticked), sorted([parts['FBM'], parts['HDC']]),
                         'a restored draft must not re-tick heads on its own')
        self.assertIn('value="1000"', page, 'the typed amounts come back too')

    # ── row traceability + audit ─────────────────────────────────────────

    def test_activity_is_recorded_for_a_new_expense(self):
        from hdc.models.auth import UserActivity
        self._expense()
        rows = UserActivity.query.filter(
            UserActivity.entity_type == 'hdc_shared_expense').all()
        self.assertTrue(rows, 'creating a shared expense must land in the activity trail')


class SplitTableScriptTestCase(unittest.TestCase):
    """The split table's live figures, executed for real under Node.

    The page script works every figure out while the operator types; the server
    works them out again when the form is posted.  Should the two ever drift
    apart, the screen shows one thing and the ledger keeps another — so the
    harness drives the *real* ``shared_expenses.js`` in a DOM stub, writes out
    the figures it ends up with, and those figures are pushed through the real
    split engine here.
    """

    @unittest.skipIf(shutil.which('node') is None, 'node is not installed')
    def test_the_live_split_is_what_the_engine_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, 'split.json')
            result = subprocess.run(
                ['node', SPLIT_HARNESS, REPO_ROOT, out_path],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(
                result.returncode, 0,
                'shared_expenses.js harness failed:\n%s%s' % (result.stdout, result.stderr))
            self.assertIn('checks passed', result.stdout)
            with open(out_path, encoding='utf-8') as handle:
                payload = json.load(handle)

        self.assertTrue(payload['cases'], 'the harness recorded no split')
        for case in payload['cases']:
            with self.subTest(case=case['name']):
                total_minor = to_minor(case['total'])
                # Both columns are posted for every row, exactly as the form does.
                participants = [{'party_id': row['party_id'],
                                 'amount': row['amount'],
                                 'percent': row['percent']} for row in case['rows']]
                if case['refused']:
                    with self.assertRaises(ValueError):
                        compute_split(case['mode'], total_minor, participants)
                    self.assertEqual(case['state'], 'bad',
                                     'the page must say so before the server has to')
                    continue
                rows = compute_split(case['mode'], total_minor, participants)
                saved = {int(r['party_id']): int(r['amount_minor']) for r in rows}
                shown = {int(r['party_id']): to_minor(r['amount']) for r in case['rows']}
                self.assertEqual(saved, shown,
                                 'what the operator saw is not what would be saved')
                self.assertEqual(sum(saved.values()), total_minor,
                                 'the saved slices must re-add to the total')
                self.assertEqual(case['state'], 'ok')

    def test_the_form_ships_the_script_and_the_style(self):
        page_path = os.path.join(REPO_ROOT, 'templates', 'hdc',
                                 'shared_expenses', 'expense_form.html')
        with open(page_path, encoding='utf-8') as handle:
            page = handle.read()
        self.assertIn('id="seSplitCheck"', page)
        for row_field in ('se-party-tick', 'se-amount', 'se-percent'):
            with self.subTest(field=row_field):
                self.assertIn(row_field, page)
        nav_path = os.path.join(REPO_ROOT, 'templates', 'hdc',
                                'shared_expenses', '_nav.html')
        with open(nav_path, encoding='utf-8') as handle:
            self.assertIn('js/pages/shared_expenses.js', handle.read())
        css_path = os.path.join(REPO_ROOT, 'static', 'hdc', 'css', 'shared_expenses.css')
        with open(css_path, encoding='utf-8') as handle:
            css = handle.read()
        self.assertIn('.se-split-off', css)
        self.assertIn('input.se-auto', css)


if __name__ == '__main__':
    unittest.main(verbosity=2)
