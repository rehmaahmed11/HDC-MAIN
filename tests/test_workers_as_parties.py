#!/usr/bin/env python3
"""Workers as parties + the consolidated worker statement.

The request behind this file was: *"tip to workers, advance to workers, and
manage their all ledger clearly — workers are also parties so add them in
parties as a worker party, and for right now keep the previous worker
setting too."*

It pins four promises:

 1. **Workers land in the Parties directory** typed ``worker``, automatically,
    and the old Workers module keeps working exactly as before.
 2. **The sync never destroys a classification** — a name already filed as a
    lender stays a lender; a deactivated row is revived, never duplicated.
 3. **The statement shows every kind of money in one list** — work, advance,
    payment, tip and settlement — with a running balance.
 4. **Tips stay neutral** — a tip is gratis cash on top of the wage, so it
    never reduces the balance due. That one rule is the difference between
    "the worker is owed 5,000" and "the worker is owed 0".

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m pytest tests/test_workers_as_parties.py -q
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app                                     # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.cashflow import CashFlowParty                      # noqa: E402
from hdc.models.projects import Project, Stage                     # noqa: E402
from hdc.models.workforce import LabourLedger, Worker, WorkerTrade  # noqa: E402
from hdc.services.cashflow_register import (                       # noqa: E402
    WORKER_PARTY_TYPES, sync_workers_as_parties,
)
from hdc.services.ledger import (                                  # noqa: E402
    WORKER_LEDGER_TYPE_VALUES, _worker_payable_snapshot, worker_statement,
    worker_type_summary,
)
from hdc.utils.dates import _pkt_today                             # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


class WorkersAsPartiesTestCase(unittest.TestCase):
    """One isolated app + database per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-worker-party-test-')
        self.db_path = os.path.join(self.tmp, 'test.db')
        self.app = create_app({'HDC_DB_PATH': self.db_path,
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        r = self.client.get('/hdc/login')
        token = self._csrf(r.get_data(as_text=True))
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        }, follow_redirects=True)
        self.project = Project(name='Site A', project_code='P-A', client='Owner A',
                               location='Karachi', contract_type='lump_sum',
                               owner_lump_sum=1_000_000)
        db.session.add(self.project)
        db.session.flush()
        self.stage = Stage(project_id=self.project.id, name='Foundation')
        db.session.add(self.stage)
        # The bootstrap already seeds the standard trades; reuse one so a new
        # Worker row passes the "trade must exist" check in the route.
        self.trade = WorkerTrade.query.filter_by(active_status=True).first()
        if self.trade is None:
            self.trade = WorkerTrade(name='Mason', active_status=True)
            db.session.add(self.trade)
            db.session.commit()
        self.trade_name = self.trade.name
        self.worker = Worker(worker_code='W-001', name='Rashid Khan',
                             role_type=self.trade_name, base_daily_wage=1_500.0,
                             wage_type='daily', active_status=True)
        db.session.add(self.worker)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _csrf(html):
        import re
        m = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
        return m.group(1) if m else ''

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _ledger(self, entry_type, amount, notes=''):
        row = LabourLedger(worker_id=self.worker.id, entry_type=entry_type,
                           amount=amount, date=_pkt_today(),
                           project_id=self.project.id, stage_id=self.stage.id,
                           notes=notes or f'{entry_type} test')
        db.session.add(row)
        db.session.commit()
        return row

    def _party(self, name):
        return (CashFlowParty.query
                .filter(db.func.lower(db.func.trim(CashFlowParty.name)) == name.lower())
                .first())

    # ------------------------------------------------- parties: worker sync
    def test_worker_party_type_exists_in_the_vocabulary(self):
        self.assertIn('worker', WORKER_PARTY_TYPES)

    def test_sync_files_the_worker_as_a_party(self):
        created, reactivated, total = sync_workers_as_parties()
        db.session.commit()
        self.assertEqual(created, 1)
        self.assertEqual(reactivated, 0)
        self.assertEqual(total, 1)
        party = self._party('Rashid Khan')
        self.assertIsNotNone(party)
        self.assertEqual((party.party_type or '').strip().lower(), 'worker')
        self.assertTrue(party.is_active)

    def test_sync_is_idempotent(self):
        sync_workers_as_parties()
        db.session.commit()
        created, reactivated, total = sync_workers_as_parties()
        db.session.commit()
        self.assertEqual(created, 0)
        self.assertEqual(total, 1)
        self.assertEqual(CashFlowParty.query.filter_by(name='Rashid Khan').count(), 1)

    def test_sync_revives_a_hidden_party(self):
        sync_workers_as_parties()
        db.session.commit()
        party = self._party('Rashid Khan')
        party.is_active = False
        db.session.commit()
        _created, reactivated, _total = sync_workers_as_parties()
        db.session.commit()
        self.assertEqual(reactivated, 1)
        self.assertTrue(self._party('Rashid Khan').is_active)

    def test_sync_never_reclassifies_an_existing_type(self):
        """A supplier who happens to share a name with a worker stays a supplier."""
        db.session.add(CashFlowParty(name='Rashid Khan', party_type='lender',
                                     is_active=True))
        db.session.commit()
        sync_workers_as_parties()
        db.session.commit()
        self.assertEqual((self._party('Rashid Khan').party_type or '').lower(), 'lender')
        self.assertEqual(CashFlowParty.query.filter_by(name='Rashid Khan').count(), 1)

    def test_parties_page_shows_the_worker(self):
        resp = self.client.get('/hdc/parties')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('Rashid Khan', html)
        self.assertIn('Worker / Labour', html)
        # the directory links straight through to the worker's statement
        self.assertIn(f'/hdc/workers/{self.worker.id}/statement', html)

    def test_parties_page_is_still_reachable_after_sync(self):
        self.assertEqual(self.client.get('/hdc/parties').status_code, 200)
        self.assertEqual(self.client.get('/hdc/parties?q=Rashid').status_code, 200)

    def test_sync_workers_action_endpoint(self):
        token = self._token()
        resp = self.client.post('/hdc/parties',
                                data={'_csrf_token': token, 'action': 'sync_workers'},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(self._party('Rashid Khan'))
        self.assertEqual((self._party('Rashid Khan').party_type or '').lower(), 'worker')

    def test_creating_a_worker_through_the_ui_files_the_party(self):
        token = self._token()
        resp = self.client.post('/hdc/workers', data={
            '_csrf_token': token, 'worker_code': 'W-002', 'name': 'Imran Ali',
            'role_type': self.trade_name, 'daily_wage': 1200, 'wage_type': 'daily',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        party = self._party('Imran Ali')
        self.assertIsNotNone(party, 'a newly added worker must appear in Parties')
        self.assertEqual((party.party_type or '').lower(), 'worker')

    # --------------------------------------------- previous settings intact
    def test_previous_worker_settings_still_work(self):
        """The Workers module is untouched — every old page still answers."""
        for url in ('/hdc/workers', '/hdc/trades',
                    f'/hdc/workers/{self.worker.id}/ledger',
                    f'/hdc/workers/{self.worker.id}/advance',
                    f'/hdc/workers/{self.worker.id}/payment',
                    f'/hdc/workers/{self.worker.id}/rate'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200, url)

    # --------------------------------------------------- consolidated ledger
    def test_statement_lists_every_kind_of_entry(self):
        self._ledger('advance', 1_000.0)
        self._ledger('payment', 400.0)
        self._ledger('tip', 250.0)
        self._ledger('settlement', 100.0)
        data = worker_statement(self.worker.id)
        kinds = {r['entry_type'] for r in data['rows']}
        self.assertEqual(kinds, {'advance', 'payment', 'tip', 'settlement'})

    def test_tips_never_reduce_the_balance(self):
        self._ledger('advance', 500.0)
        self._ledger('tip', 2_000.0)
        data = worker_statement(self.worker.id)
        self.assertAlmostEqual(data['totals']['tip'], 2_000.0, places=2)
        tip_rows = [r for r in data['rows'] if r['entry_type'] == 'tip']
        self.assertEqual(len(tip_rows), 1)
        # The running balance before and after the tip row is identical.
        adv_row = [r for r in data['rows'] if r['entry_type'] == 'advance'][0]
        self.assertAlmostEqual(tip_rows[0]['running_balance'],
                               adv_row['running_balance'], places=2)
        snap = _worker_payable_snapshot(self.worker.id)
        self.assertAlmostEqual(snap['balance'], -500.0, places=2)

    def test_statement_running_balance_matches_the_payable_snapshot(self):
        self._ledger('advance', 300.0)
        self._ledger('payment', 120.0)
        self._ledger('settlement', 80.0)
        data = worker_statement(self.worker.id)
        snap = _worker_payable_snapshot(self.worker.id)
        self.assertAlmostEqual(data['totals']['closing_balance'],
                               snap['balance'], places=2)
        self.assertAlmostEqual(data['totals']['balance'], snap['balance'], places=2)

    def test_statement_type_filter(self):
        self._ledger('advance', 300.0)
        self._ledger('payment', 120.0)
        data = worker_statement(self.worker.id, entry_types=['advance'])
        self.assertEqual({r['entry_type'] for r in data['rows']}, {'advance'})
        # filtering must not change the balance a row reports
        full = worker_statement(self.worker.id)
        adv_full = [r for r in full['rows'] if r['entry_type'] == 'advance'][0]
        self.assertAlmostEqual(data['rows'][0]['running_balance'],
                               adv_full['running_balance'], places=2)

    def test_statement_can_hide_voided_rows(self):
        row = self._ledger('advance', 700.0)
        row.is_void = True
        db.session.commit()
        self.assertEqual(len(worker_statement(self.worker.id, include_void=False)['rows']), 0)
        self.assertEqual(len(worker_statement(self.worker.id, include_void=True)['rows']), 1)

    def test_statement_page_shows_voided_by_default(self):
        row = self._ledger('advance', 700.0)
        row.is_void = True
        row.void_reason = 'entered twice'
        db.session.commit()
        html = self.client.get(
            f'/hdc/workers/{self.worker.id}/statement').get_data(as_text=True)
        self.assertIn('entered twice', html)

    def test_type_summary_counts_each_kind(self):
        self._ledger('advance', 100.0)
        self._ledger('advance', 200.0)
        self._ledger('tip', 50.0)
        summary, _snap = worker_type_summary(self.worker.id)
        self.assertEqual(summary['advance']['count'], 2)
        self.assertAlmostEqual(summary['advance']['amount'], 300.0, places=2)
        self.assertAlmostEqual(summary['tip']['amount'], 50.0, places=2)

    def test_every_ledger_kind_has_a_label(self):
        self.assertEqual(set(WORKER_LEDGER_TYPE_VALUES),
                         {'work', 'advance', 'payment', 'tip', 'settlement'})

    # ------------------------------------------------------- statement page
    def test_statement_page_renders(self):
        self._ledger('advance', 500.0)
        self._ledger('tip', 200.0)
        resp = self.client.get(f'/hdc/workers/{self.worker.id}/statement')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:500])
        html = resp.get_data(as_text=True)
        self.assertIn('Rashid Khan', html)
        self.assertIn('Advance', html)
        self.assertIn('Tips do not reduce what is owed', html)

    def test_statement_page_filters(self):
        self._ledger('advance', 500.0)
        self._ledger('payment', 250.0)
        resp = self.client.get(f'/hdc/workers/{self.worker.id}/statement?type=advance')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('Advance', html)

    def test_ledger_page_still_renders_with_the_new_filters(self):
        self._ledger('advance', 500.0)
        resp = self.client.get(f'/hdc/workers/{self.worker.id}/ledger')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:500])
        html = resp.get_data(as_text=True)
        self.assertIn('Statement', html)
        self.assertIn('Open full statement', html)
        resp = self.client.get(f'/hdc/workers/{self.worker.id}/ledger?type=advance')
        self.assertEqual(resp.status_code, 200)


if __name__ == '__main__':
    unittest.main()
