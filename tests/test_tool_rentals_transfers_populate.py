"""Populate-and-verify smoke: HDC Tools new rentals + transfers only.

Runs ``scripts/seed_tools_rentals_transfers.py`` on a throwaway database, through
the real HTTP forms, and asserts every check it makes (accepted rentals and
transfers, per-place book quantities on Tools > Audit, rows on Tools >
Tracking, and owned = in store + out for every tool).

The regression it guards: a partial hand-over from the Transfer Rental flow
used to leave the moved pieces on the *source* rental as well as on the new
one (Steel Shuttering Prop: 210 out instead of 170 once two holders had split
the line).  See ``hdc/services/tool_tracking.py`` (``handover`` events).

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_tool_rentals_transfers_populate -v
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    'seed_tools_rentals_transfers',
    os.path.join(ROOT, 'scripts', 'seed_tools_rentals_transfers.py'))
seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed)


class ToolRentalsTransfersPopulateSmokeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-populate-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp, 'populate.db'),
            'HDC_INSTANCE_DIR': self.tmp,
            'TESTING': True,
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_rentals_and_transfers_populate_audit_and_tracking(self):
        pop = seed.run_scenario(self.app, quiet=True)
        self.assertTrue(pop.checks, 'the scenario ran no checks at all')
        failed = [f'{label}: {detail}' for ok, label, detail in pop.checks if not ok]
        self.assertEqual(failed, [], 'populate/verify checks failed')

    def test_split_line_keeps_source_and_destination_quantities_exact(self):
        """Two holders at one site hand different tools to a third site."""
        pop = seed.run_scenario(self.app, quiet=True)
        from hdc.services.tool_tracking import tool_ledger
        with self.app.app_context():
            ledger = tool_ledger()
            prp = next(t for t in ledger['tools'] if t['code'] == seed.TOOLS['PRP'][0])
            self.assertEqual(float(prp['out_qty']), 170.0)
            self.assertEqual(float(prp['in_store_qty']), 30.0)
            self.assertFalse(prp['unaccounted'])
            # every holding of the prop sums to what the source rentals still owe
            per_rental = {}
            for holding in prp['holdings']:
                per_rental[holding['rental'].rental_code] = (
                    per_rental.get(holding['rental'].rental_code, 0.0) + float(holding['qty']))
            self.assertEqual(per_rental.get('RENT-00001'), 50.0)
        self.assertTrue(pop.checks)


if __name__ == '__main__':
    unittest.main(verbosity=2)
