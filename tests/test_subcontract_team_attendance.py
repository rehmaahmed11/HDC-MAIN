#!/usr/bin/env python3
"""Regression tests for the simple subcontractor crew attendance.

Pins the behaviour from SUBCONTRACT_SIMPLE_ATTENDANCE_PLAN.md:
  - Mason 17 days x 10 workers x 2,000 = 340,000
  - Labour 30 days x 10 workers x 1,200 = 360,000
  - the shared roll-up (crew + legacy rows) feeds the attendance page,
    ledger KPIs and reports exactly once (no double count, no gap)
  - nothing on the payment side moves (informational only)

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \
        python -m unittest tests.test_subcontract_team_attendance -v
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app                                    # noqa: E402
from hdc.extensions import db                                     # noqa: E402
from hdc.models.accounts import AccountTransaction                # noqa: E402
from hdc.models.projects import Project, Stage                    # noqa: E402
from hdc.models.subcontract import (                              # noqa: E402
    SubcontractLabourWorker, SubcontractTeamAttendance, Subcontractor,
)
from hdc.models.workforce import WorkerTrade                      # noqa: E402
from hdc.services.subcontract import sub_labour_rollup           # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
D0 = date(2026, 9, 1)


class TeamAttendanceTestCase(unittest.TestCase):
    """One isolated app + database per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-team-att-test-')
        self.db_path = os.path.join(self.tmp, 'test.db')
        self.app = create_app({'HDC_DB_PATH': self.db_path,
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()
        self.project, self.stage, self.other_stage = self._make_project()
        self.sub = self._make_subcontractor()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def _login(self):
        r = self.client.get('/hdc/login')
        token = self._csrf(r.get_data(as_text=True))
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        }, follow_redirects=True)

    @staticmethod
    def _csrf(html):
        m = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
        return m.group(1) if m else ''

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _make_project(self):
        if not WorkerTrade.query.filter_by(name='Mason').first():
            db.session.add(WorkerTrade(name='Mason', active_status=True))
        if not WorkerTrade.query.filter_by(name='Labour').first():
            db.session.add(WorkerTrade(name='Labour', active_status=True))
        p = Project(name='Test Site', project_code='T-01', client='Owner',
                    location='Nowhere', contract_type='lump_sum',
                    owner_lump_sum=2_000_000)
        db.session.add(p)
        db.session.flush()
        s1 = Stage(project_id=p.id, name='Foundation', contract_basis='Lump Sum',
                   lump_sum_value=500_000)
        s2 = Stage(project_id=p.id, name='Super Structure', contract_basis='Lump Sum',
                   lump_sum_value=700_000)
        db.session.add_all([s1, s2])
        db.session.commit()
        return p, s1, s2

    def _make_subcontractor(self):
        sub = Subcontractor(
            subcontractor_code='SUB-9001',
            project_id=self.project.id,
            stage_id=self.stage.id,
            name='Raza Plastering',
            phone='0300-1111111',
            work_type='Plastering',
            contract_type='lump_sum',
            lump_sum_amount=1_000_000.0,
        )
        db.session.add(sub)
        db.session.commit()
        return sub

    def _post_team(self, worker_type, days, workers, rate, total=None,
                   stage=None, total_manual=False, **extra):
        stage = stage or self.stage
        data = {
            'worker_type': worker_type,
            'date': D0.isoformat(),
            'days_count': str(days),
            'workers_count': str(workers),
            'wage_rate': ('%.2f' % rate) if not total_manual else '0',
            'project_id': str(stage.project_id),
            'stage_id': str(stage.id),
            '_csrf_token': self._token(),
        }
        if total is not None:
            data['total_amount'] = str(total)
        if total_manual:
            data['total_manual'] = '1'
        data.update(extra)
        return self.client.post(
            f'/hdc/subcontractor/{self.sub.id}/team_attendance',
            data=data, follow_redirects=True)

    def _att_page(self, **query):
        qs = '&'.join(f'{k}={v}' for k, v in query.items() if v is not None)
        url = f'/hdc/subcontractor/{self.sub.id}/attendance_page'
        return self.client.get(url + (('?' + qs) if qs else ''), follow_redirects=True)

    def _ledger(self):
        return self.client.get(f'/hdc/subcontractor/{self.sub.id}/ledger',
                               follow_redirects=True)

    def _page_text(self, r):
        self.assertEqual(r.status_code, 200, f'page {r.request.path} not 200')
        return r.get_data(as_text=True)

    # ------------------------------------------------------------- tests
    def test_mason_and_labour_totals(self):
        """Mason 17x10x2000=340,000 and Labour 30x10x1200=360,000 on the page."""
        r1 = self._post_team('Mason', 17, 10, 2000)
        self.assertIn('Crew attendance recorded.', self._page_text(r1))
        r2 = self._post_team('Labour', 30, 10, 1200, stage=self.other_stage)
        self.assertIn('Crew attendance recorded.', self._page_text(r2))

        rows = {r.worker_type: r for r in SubcontractTeamAttendance.query.all()}
        self.assertAlmostEqual(rows['Mason'].total_amount, 340_000.0)
        self.assertAlmostEqual(rows['Labour'].total_amount, 360_000.0)
        self.assertEqual(rows['Mason'].man_days, 170)
        self.assertEqual(rows['Labour'].man_days, 300)

        html = self._page_text(self._att_page())
        self.assertIn('Crew Attendance (Simple)', html)
        self.assertIn('340,000', html)
        self.assertIn('360,000', html)
        # Combined roll-up: 17+30 days, 470 man-days, 700,000 cost.
        self.assertIn('Labour cost (crew + legacy register): <strong>700,000</strong>', html)

    def test_manual_total_back_derives_rate(self):
        r = self._post_team('Mason', 17, 10, 0, total=330_000, total_manual=True)
        self.assertIn('Crew attendance recorded.', self._page_text(r))
        row = SubcontractTeamAttendance.query.one()
        self.assertAlmostEqual(row.total_amount, 330_000.0)
        self.assertTrue(row.total_manual)
        self.assertAlmostEqual(row.wage_rate, 330_000.0 / 170.0)

    def test_rollup_combines_crew_and_legacy_without_double_count(self):
        """Crew rows and the old per-person register feed one cost, once each."""
        self._post_team('Mason', 17, 10, 2000)
        # Legacy per-person row: 5 men x 1,000 = 5,000 on the same day.
        w = SubcontractLabourWorker(subcontractor_id=self.sub.id, name='Ali',
                                    trade='Mason', daily_wage=1000.0)
        db.session.add(w)
        db.session.flush()
        self.client.post(
            f'/hdc/subcontractor/{self.sub.id}/labour_attendance',
            data={
                'date': D0.isoformat(),
                'worker_id': str(w.id),
                'labour_count': '5',
                'wage_rate': '1000',
                'total_labour_paid': '5000',
                'attendance_status': 'present',
                'working_hours': '8',
                'project_id': str(self.project.id),
                'stage_id': str(self.stage.id),
                '_csrf_token': self._token(),
            }, follow_redirects=True)

        roll = sub_labour_rollup(self.sub.id)
        self.assertEqual(roll['crew_rows'], 1)
        self.assertEqual(roll['legacy_rows'], 1)
        self.assertAlmostEqual(roll['cost'], 345_000.0)
        self.assertEqual(roll['man_days'], 175)

        ledger_html = self._page_text(self._ledger())
        self.assertIn('345,000', ledger_html)
        # Ledger labour usage shows crew days (17) and man-days (175).
        self.assertIn('Man-days: 175', ledger_html)

    def test_ledger_and_reports_use_rollup(self):
        self._post_team('Mason', 17, 10, 2000)
        self._post_team('Labour', 30, 10, 1200, stage=self.other_stage)

        ledger_html = self._page_text(self._ledger())
        self.assertIn('700,000', ledger_html)
        self.assertIn('Crew entries: 2', ledger_html)

        # Give the sub some progress so the subcontract watch row (contract -
        # labour cost) shows on the reports page.
        self.sub.work_done_percentage = 50.0
        db.session.commit()
        reports = self._page_text(self.client.get('/hdc/reports/glance', follow_redirects=True))
        self.assertIn('700,000', reports)

    def test_no_payment_side_effect(self):
        """Crew rows are informational: they must not post to unified accounts."""
        before = AccountTransaction.query.count()
        self._post_team('Mason', 17, 10, 2000)
        after = AccountTransaction.query.count()
        self.assertEqual(before, after, 'crew attendance must not post account rows')
        # And the subcontractor's payable/cleared are untouched.
        sub = Subcontractor.query.get(self.sub.id)
        self.assertEqual(sub.total_paid, 0.0)
        self.assertEqual(sub.total_cleared, 0.0)

    def test_duplicate_submit_is_ignored(self):
        r1 = self._post_team('Mason', 17, 10, 2000)
        self.assertIn('Crew attendance recorded.', self._page_text(r1))
        r2 = self._post_team('Mason', 17, 10, 2000)
        self.assertIn('Duplicate crew attendance prevented', self._page_text(r2))
        self.assertEqual(SubcontractTeamAttendance.query.count(), 1)

    def test_validation_and_scope(self):
        # Missing trade
        r = self.client.post(f'/hdc/subcontractor/{self.sub.id}/team_attendance',
                             data={'worker_type': '', 'date': D0.isoformat(),
                                   'days_count': '5', 'workers_count': '2',
                                   'wage_rate': '1000',
                                   'project_id': str(self.project.id),
                                   'stage_id': str(self.stage.id),
                                   '_csrf_token': self._token()},
                             follow_redirects=True)
        self.assertIn('Worker type is required', self._page_text(r))
        # Out-of-scope stage: build a foreign project/stage the sub has no link to.
        p2 = Project(name='Other Site', project_code='T-02', client='Owner',
                     location='Elsewhere', contract_type='lump_sum',
                     owner_lump_sum=100_000)
        db.session.add(p2)
        db.session.flush()
        s3 = Stage(project_id=p2.id, name='Basement', contract_basis='Lump Sum',
                   lump_sum_value=50_000)
        db.session.add(s3)
        db.session.commit()
        r2 = self._post_team('Mason', 5, 2, 1000, stage=s3)
        self.assertIn('outside subcontractor assigned scope', self._page_text(r2))
        self.assertEqual(SubcontractTeamAttendance.query.count(), 0)

    def test_delete_removes_cost_from_rollup(self):
        self._post_team('Mason', 17, 10, 2000)
        self._post_team('Labour', 30, 10, 1200, stage=self.other_stage)
        row = SubcontractTeamAttendance.query.filter_by(worker_type='Mason').one()
        r = self.client.post(
            f'/hdc/subcontractor/{self.sub.id}/team_attendance/{row.id}/delete',
            data={'_csrf_token': self._token()}, follow_redirects=True)
        self.assertIn('Crew attendance entry deleted.', self._page_text(r))
        roll = sub_labour_rollup(self.sub.id)
        self.assertEqual(roll['crew_rows'], 1)
        self.assertAlmostEqual(roll['cost'], 360_000.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
