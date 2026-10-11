#!/usr/bin/env python3
"""Tools: the Stage picker offers the chosen site / project's stages only.

The Rent Out transaction page (``/hdc/tool-rental/transaction``) used to list
every stage of every project and site under "Stage (optional)" — nothing tied
it to the chosen site — and the New Rental / rental-detail transfer forms
showed every stage until a site was picked.  All three now declare the
dependency in their markup (``combo_select(..., depends_on=<project select>,
depends_key='project-id')``) and ``core/combo.js`` enforces it:

* no site chosen  → no stage offered (the list says "Choose the site / project
  first");
* site chosen     → exactly that site's stages;
* a typed label of another site's stage is not accepted, and a chosen stage is
  cleared when the site changes.

The server already refuses a stage that does not belong to the posted project;
that guard is pinned here too, so the scoping holds even without JavaScript.

The DOM cases need ``node`` + ``jsdom`` (``npm ci --prefix tests``) and skip
without them; the markup and server checks run everywhere.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_tool_stage_scope -v
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

from hdc.app import create_app                                     # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.projects import Project, Stage                     # noqa: E402
from hdc.models.tool_rental import (                               # noqa: E402
    Tool, ToolCategory, ToolRental, ToolRentalItem,
)
from hdc.services.tool_rental import ensure_all_tools_serials      # noqa: E402
from hdc.utils.dates import _pkt_today                             # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
HARNESS = os.path.join(TESTS_DIR, 'tool_stage_scope_harness.js')


def _jsdom_available() -> bool:
    if shutil.which('node') is None:
        return False
    try:
        probe = subprocess.run(['node', '-e', 'require("jsdom")'], cwd=TESTS_DIR,
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


JSDOM_AVAILABLE = _jsdom_available()


class ToolStageScopeTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-stage-scope-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp,
            'TESTING': True,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()

        self.site_a = Project(name='Alpha Tower', project_code='P-A', client='Owner A',
                              location='Karachi', contract_type='lump_sum',
                              owner_lump_sum=1_000_000)
        self.site_b = Project(name='Bravo Plaza', project_code='P-B', client='Owner B',
                              location='Lahore', contract_type='lump_sum',
                              owner_lump_sum=1_000_000)
        db.session.add_all([self.site_a, self.site_b])
        db.session.flush()
        # Same stage names on both sites — the classic way a global list misleads.
        self.stages_a = [Stage(project_id=self.site_a.id, name=n,
                               contract_basis='Lump Sum', lump_sum_value=1)
                         for n in ('Foundation', 'Structure')]
        self.stages_b = [Stage(project_id=self.site_b.id, name=n,
                               contract_basis='Lump Sum', lump_sum_value=1)
                         for n in ('Foundation', 'Finishing')]
        db.session.add_all(self.stages_a + self.stages_b)
        cat = ToolCategory(name='Power Tools', active_status=True)
        db.session.add(cat)
        db.session.flush()
        self.tool = Tool(tool_code='TOOL-9001', name='Vibrator', unit='pcs',
                         category_id=cat.id, total_quantity=6, purchase_cost=1000.0,
                         rental_rate_per_day=100.0, condition='good', status='active',
                         is_void=False)
        db.session.add(self.tool)
        db.session.commit()
        ensure_all_tools_serials(commit=True)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def _login(self):
        page = self.client.get('/hdc/login')
        match = re.search(r'name="_csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD,
            '_csrf_token': match.group(1) if match else '',
        }, follow_redirects=True)

    def _csrf(self, html):
        match = re.search(r'name="csrf-token" content="([^"]+)"', html) or \
            re.search(r'name="_csrf_token" value="([^"]+)"', html)
        return match.group(1) if match else ''

    def _rental(self):
        rental = ToolRental(
            rental_code='RENT-SCOPE-1', renter_type='external', customer_name='Scope Customer',
            rental_date=_pkt_today(), billing_type='per_day', total_rented_qty=1,
            total_amount=100.0, total_paid=0.0, total_returned_qty=0.0,
            status='active', payment_status='unpaid', notes='')
        db.session.add(rental)
        db.session.flush()
        db.session.add(ToolRentalItem(rental_id=rental.id, tool_id=self.tool.id,
                                      qty_rented=1, qty_returned=0.0, qty_pending=1,
                                      rate=100.0, amount=100.0, notes=''))
        db.session.commit()
        return rental

    def _pages(self):
        rental = self._rental()
        return [
            ('/hdc/tool-rental/transaction', 'txnProjectSelect', 'txnStageSelect'),
            ('/hdc/tool-rental/new', 'rentalProjectSelect', 'rentalStageSelect'),
            (f'/hdc/tool-rental/{rental.id}', 'transferProjectSelect', 'transferStageSelect'),
        ]

    # ------------------------------------------------------------ markup
    def test_every_tools_stage_picker_declares_its_project(self):
        for url, project_id, stage_id in self._pages():
            with self.subTest(url=url):
                html = self.client.get(url).get_data(as_text=True)
                match = re.search(r'<select id="%s"[^>]*>' % stage_id, html)
                self.assertIsNotNone(match, f'{stage_id} rendered on {url}')
                tag = match.group(0)
                self.assertIn(f'data-hdc-depends-on="{project_id}"', tag)
                self.assertIn('data-hdc-depends-key="project-id"', tag)
                # Each stage option carries its own project, so the scoping is
                # driven by the data, not by page code.
                for stage in self.stages_a + self.stages_b:
                    self.assertRegex(
                        html, r'<option value="%d" data-project-id="%d"' % (stage.id, stage.project_id))
                # The old page-local "show everything until a site is picked"
                # filters are gone.
                self.assertNotIn('!projectId ||', html)
                self.assertNotIn('!project.value ||', html)

    # ------------------------------------------------------------ browser
    def test_stage_list_follows_the_chosen_site_in_the_browser(self):
        if not JSDOM_AVAILABLE:
            self.skipTest('needs node + jsdom (npm ci --prefix tests)')
        for url, project_id, stage_id in self._pages():
            with self.subTest(url=url):
                html = self.client.get(url).get_data(as_text=True)
                page = os.path.join(self.tmp, 'page.html')
                spec_file = os.path.join(self.tmp, 'spec.json')
                with open(page, 'w', encoding='utf-8') as handle:
                    handle.write(html)
                with open(spec_file, 'w', encoding='utf-8') as handle:
                    json.dump({
                        'url': url,
                        'projectSelect': project_id,
                        'stageSelect': stage_id,
                        'projectA': self.site_a.id,
                        'projectB': self.site_b.id,
                        'projectBName': self.site_b.name,
                        'stagesA': [s.id for s in self.stages_a],
                        'stagesB': [s.id for s in self.stages_b],
                    }, handle)
                result = subprocess.run(['node', HARNESS, REPO_ROOT, page, spec_file],
                                        capture_output=True, text=True, timeout=180,
                                        cwd=TESTS_DIR)
                self.assertEqual(result.returncode, 0,
                                 f'{url}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}')
                self.assertIn('HDC-HARNESS-OK', result.stdout)

    # ------------------------------------------------------------ server
    def test_server_refuses_a_stage_from_another_site(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        before = ToolRental.query.count()
        response = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._csrf(html),
            'txn_type': 'issue_own_site',
            'renter_type': 'internal',
            'project_id': str(self.site_a.id),
            'stage_id': str(self.stages_b[1].id),   # Finishing — belongs to site B
            'rental_date': _pkt_today().isoformat(),
            'billing_type': 'no_charge',
            'tool_id[]': [str(self.tool.id)],
            'qty[]': ['1'],
            'rate[]': ['0'],
        }, follow_redirects=True)
        self.assertIn('does not belong to the selected project/site',
                      response.get_data(as_text=True))
        self.assertEqual(ToolRental.query.count(), before)


if __name__ == '__main__':
    unittest.main()
