#!/usr/bin/env python3
"""Regression tests for the row traceability feature.

Every list row is tagged with ``data-hdc-ent`` / ``data-hdc-id`` (rendered by
the ``hdc_row_attrs`` Jinja filter); ``static/hdc/js/core/audit.js`` then fills
in an "Entered by" column from ``hdc_user_activity`` and voided rows are greyed
out by CSS.  These tests pin the filter's behaviour (models, query rows, tuples
and view-model dicts) and the JSON endpoint the browser calls.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \
        python -m unittest tests.test_row_traceability -v
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

from hdc.app import create_app                                    # noqa: E402
from hdc.extensions import db                                     # noqa: E402
from hdc.models.accounts import Expense                           # noqa: E402
from hdc.models.auth import UserActivity                          # noqa: E402
from hdc.models.projects import Project, Stage                    # noqa: E402
from hdc.models.workforce import (                                 # noqa: E402
    LabourLedger, TimeEntry, Worker, WorkerTrade,
)
from hdc.services.actors import (                                 # noqa: E402
    _derive_labour_ledger, actor_map, actor_payload, entity_type_of,
    row_attrs_html, row_is_void,
)

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


class _Obj:
    """Stand-in for an ORM row (the filter only needs these attributes)."""

    def __init__(self, table, row_id, **attrs):
        self.__tablename__ = table
        self.id = row_id
        for key, val in attrs.items():
            setattr(self, key, val)


class RowTraceabilityTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-trace-test-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()
        if not WorkerTrade.query.filter_by(name='Mason').first():
            db.session.add(WorkerTrade(name='Mason', active_status=True))
            db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self):
        page = self.client.get('/hdc/login').get_data(as_text=True)
        token = re.search(r'name="_csrf_token" value="([^"]+)"', page)
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD,
            '_csrf_token': token.group(1) if token else '',
        }, follow_redirects=True)

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _add_worker(self, code='W-001', name='Ali Raza'):
        self.client.post('/hdc/workers', data={
            'worker_code': code, 'name': name, 'role_type': 'Mason',
            'wage_type': 'daily', 'daily_wage': '1000',
            '_csrf_token': self._token(),
        }, follow_redirects=True)
        return Worker.query.filter_by(worker_code=code).first()


class TestRowAttrsFilter(RowTraceabilityTestCase):
    def test_model_row(self):
        html = str(row_attrs_html(_Obj('hdc_worker', 7, name='Ali')))
        self.assertIn('data-hdc-ent="hdc_worker"', html)
        self.assertIn('data-hdc-id="7"', html)
        self.assertIn('data-hdc-label="Ali"', html)
        self.assertNotIn('data-hdc-void', html)

    def test_query_row_tuple_resolves_first_model(self):
        """``(Expense, Project, Stage)`` style queries tag the entry row."""
        worker = _Obj('hdc_worker', 9, name='Rowed')
        row = (worker, _Obj('hdc_project', 3, name='Site'))
        html = str(row_attrs_html(row))
        self.assertIn('data-hdc-ent="hdc_worker"', html)
        self.assertIn('data-hdc-id="9"', html)

    def test_view_model_dict_with_marker(self):
        html = str(row_attrs_html({'_hdc_entity': 'hdc_account', 'id': 4,
                                   'name': 'Cash', 'status': 'active'}))
        self.assertIn('data-hdc-ent="hdc_account"', html)
        self.assertIn('data-hdc-id="4"', html)

    def test_view_model_dict_with_nested_model(self):
        html = str(row_attrs_html({'row': _Obj('hdc_material_v2', 12, name='Cement'),
                                   'delivered_qty': 3}))
        self.assertIn('data-hdc-ent="hdc_material_v2"', html)
        self.assertIn('data-hdc-id="12"', html)

    def test_voided_dict_row_is_marked(self):
        html = str(row_attrs_html({'_hdc_entity': 'hdc_time_entry', 'id': 5,
                                   'status': 'Voided'}))
        self.assertIn('data-hdc-void="1"', html)

    def test_explicit_entity_and_id(self):
        html = str(row_attrs_html({'id': 2, 'name': 'Steel'},
                                  entity_type='hdc_supplier', row_id=2))
        self.assertIn('data-hdc-ent="hdc_supplier"', html)
        self.assertIn('data-hdc-id="2"', html)

    def test_non_rows_render_nothing(self):
        for value in (None, 'text', 12, {'id': 3, 'name': 'no marker'},
                      {'a': 1}):
            self.assertEqual(str(row_attrs_html(value)).strip(), '',
                             f'{value!r} should not be tagged')

    def test_void_helpers_understand_dicts_and_models(self):
        self.assertTrue(row_is_void({'status': 'voided'}))
        self.assertTrue(row_is_void({'is_void': True}))
        self.assertFalse(row_is_void({'status': 'active'}))
        self.assertTrue(row_is_void(_Obj('hdc_labour_ledger', 1, is_void=True,
                                         void_reason='duplicate')))
        self.assertEqual(entity_type_of({'row': _Obj('hdc_supplier', 1)}),
                         'hdc_supplier')


class TestDerivedLabourLedgerActors(RowTraceabilityTestCase):
    """The labour ledger is resolved through its mirrored rows."""

    def test_tip_ledger_row_follows_its_expense(self):
        from datetime import date, datetime
        worker = self._add_worker()
        project = Project(name='Tip Site', project_code='TS-1', client='Owner',
                          location='Nowhere', contract_type='lump_sum',
                          owner_lump_sum=1000)
        db.session.add(project)
        db.session.flush()
        expense = Expense(project_id=project.id, stage_id=None, amount=25.0,
                          date=date(2026, 8, 1), remarks='Tip for Ali',
                          tip_worker_id=worker.id, is_void=False)
        db.session.add(expense)
        db.session.flush()
        ledger = LabourLedger(
            worker_id=worker.id, entry_type='tip', amount=25.0,
            date=date(2026, 8, 1),
            notes=f'Tip for Ali | TIP_WORKER_ID:{worker.id} | TIP_EXPENSE_ID:{expense.id}')
        db.session.add(ledger)
        db.session.add(UserActivity(
            entity_type='hdc_expense', entity_id=str(expense.id),
            username='admin', event_type='create',
            created_at=datetime(2026, 8, 1, 10, 0)))
        db.session.commit()

        # the derivation path reads the linked expense's activity rows
        derived = _derive_labour_ledger([ledger.id]).get(ledger.id)
        self.assertIsNotNone(derived)
        self.assertEqual(derived['created_by'], 'admin')
        self.assertTrue(derived['derived'])
        # and the public lookup always answers with *some* trail
        self.assertTrue(actor_map([ledger]).get(ledger.id)['created_by'])


class TestRowActorsEndpoint(RowTraceabilityTestCase):
    def test_requires_login(self):
        # Drop the app context pushed in setUp: Flask reuses it for the test
        # client's requests, which would carry the logged-in user with it.
        self.ctx.pop()
        try:
            resp = self.app.test_client().get('/hdc/api/row_actors?e=hdc_worker&ids=1')
        finally:
            self.ctx = self.app.app_context()
            self.ctx.push()
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/hdc/login', resp.headers.get('Location', ''))

    def test_returns_actor_for_created_row(self):
        worker = self._add_worker()
        resp = self.client.get(
            f'/hdc/api/row_actors?e=hdc_worker&ids={worker.id}')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        info = data['actors']['hdc_worker'][str(worker.id)]
        self.assertEqual(info['created_by'], 'admin')
        self.assertTrue(info['created_at'])
        self.assertEqual(info['events'], 1)

    def test_unknown_ids_are_still_well_formed(self):
        payload = actor_payload({'hdc_worker': [99999]})
        self.assertIn('99999', payload['hdc_worker'])
        self.assertEqual(payload['hdc_worker']['99999']['events'], 0)


class TestListPagesCarryRowTags(RowTraceabilityTestCase):
    def test_workers_page_tags_rows_and_loads_audit_js(self):
        self._add_worker()
        html = self.client.get('/hdc/workers').get_data(as_text=True)
        self.assertIn('data-hdc-ent="hdc_worker"', html)
        self.assertIn('js/core/audit.js', html)

    def test_void_rows_carry_the_grey_flag(self):
        worker = self._add_worker()
        from datetime import date
        from hdc.models.workforce import LabourLedger
        db.session.add(LabourLedger(worker_id=worker.id, entry_type='advance',
                                    amount=500.0, date=date(2026, 8, 1),
                                    is_void=True, void_reason='entered twice'))
        db.session.commit()
        html = self.client.get(f'/hdc/workers/{worker.id}/ledger').get_data(as_text=True)
        self.assertIn('data-hdc-void="1"', html)
        self.assertIn('data-hdc-void-reason="entered twice"', html)

    def test_timekeeping_tags_each_detailed_entry(self):
        """The grouped day row and every nested time entry use its own id."""
        from datetime import datetime

        worker = self._add_worker()
        project = Project(
            name='Trace Site', project_code='TR-1', client='Owner',
            location='Nowhere', contract_type='lump_sum', owner_lump_sum=1000,
        )
        db.session.add(project)
        db.session.flush()
        stage = Stage(project_id=project.id, name='Foundation')
        db.session.add(stage)
        db.session.flush()
        entry = TimeEntry(
            worker_id=worker.id, project_id=project.id, stage_id=stage.id,
            check_in=datetime(2026, 8, 1, 8), check_out=datetime(2026, 8, 1, 16),
            hours=8, overtime=0, wage_calculated=1000,
        )
        db.session.add(entry)
        db.session.commit()

        html = self.client.get('/hdc/timekeeping').get_data(as_text=True)
        marker = f'data-hdc-ent="hdc_time_entry" data-hdc-id="{entry.id}"'
        self.assertGreaterEqual(html.count(marker), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
