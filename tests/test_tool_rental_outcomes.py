"""Tools > Rentals: the record says *how* the tools left a rental.

``status='returned'`` covers two different stories — pieces came back to the
store, or pieces were handed straight to the next site / customer through a
Transfer Rental.  The hub now shows "returned to store" vs "transferred to
<next holder> (RENT-xxxx)" on every row, plus "From <previous holder>" on the
rental that received them, and can filter by either outcome.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_tool_rental_outcomes -v
"""

from __future__ import annotations

import html as html_mod
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


class ToolRentalOutcomesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-outcomes-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp, 'outcomes.db'),
            'HDC_INSTANCE_DIR': self.tmp,
            'TESTING': True,
        })
        self.pop = seed.run_scenario(self.app, quiet=True)
        self.client = self.pop.client

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_service_separates_store_returns_from_handovers(self):
        from hdc.extensions import db
        from hdc.models.tool_rental import ToolRental, ToolRentalReturn, ToolRentalTransfer
        from hdc.services.tool_rental import rental_outcomes, rental_outcome_filter
        with self.app.app_context():
            rentals = ToolRental.query.filter_by(is_void=False).all()
            outcomes = rental_outcomes(rentals)
            transfers = ToolRentalTransfer.query.filter(
                ToolRentalTransfer.to_rental_id.isnot(None)).all()
            self.assertTrue(transfers, 'seed scenario made no transfer rentals')

            for t in transfers:
                src = outcomes[int(t.rental_id)]
                dst = outcomes[int(t.to_rental_id)]
                target = db.session.get(ToolRental, t.to_rental_id)
                # the source row names the next holder and links its rental
                self.assertTrue(any(n['rental_id'] == int(t.to_rental_id)
                                    and n['rental_code'] == target.rental_code
                                    and n['label'] == t.to_location_label
                                    for n in src['next']))
                self.assertGreaterEqual(src['transferred_out'], float(t.qty_transferred or 0))
                # the destination row names where it came from
                self.assertTrue(any(p['rental_id'] == int(t.rental_id) for p in dst['prev']))

            # a hand-over is never counted as a store return
            for r in rentals:
                store_qty = sum(float(x.total_tools_returned or 0)
                                for x in ToolRentalReturn.query.filter_by(rental_id=r.id).all())
                self.assertAlmostEqual(outcomes[int(r.id)]['returned_to_store'], store_qty, places=2)

            transferred = rental_outcome_filter(rentals, 'transferred', outcomes)
            self.assertEqual({int(r.id) for r in transferred},
                             {int(t.rental_id) for t in transfers})
            self.assertEqual(rental_outcome_filter(rentals, 'nonsense', outcomes), rentals)

    def test_hub_page_marks_transferred_and_previous_holder(self):
        from hdc.extensions import db
        from hdc.models.tool_rental import ToolRental, ToolRentalTransfer
        with self.app.app_context():
            t = (ToolRentalTransfer.query
                 .filter(ToolRentalTransfer.to_rental_id.isnot(None)).first())
            src_code = db.session.get(ToolRental, t.rental_id).rental_code
            dst_code = db.session.get(ToolRental, t.to_rental_id).rental_code
            to_label = t.to_location_label

        page = self.client.get('/hdc/tool-rental?per_page=250')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('rental-transferred-next', html)
        self.assertIn('transferred to', html)
        self.assertIn(f'({dst_code})', html)
        self.assertIn('rental-from-prev', html)
        self.assertIn(f'({src_code})', html)
        self.assertIn(html_mod.escape(to_label), html)
        # filter dropdown is offered
        self.assertIn('name="outcome"', html)
        self.assertIn('Returned to store', html)

        only = self.client.get('/hdc/tool-rental?outcome=transferred&per_page=250')
        self.assertEqual(only.status_code, 200)
        only_html = only.get_data(as_text=True)
        self.assertIn(src_code, only_html)
        self.assertIn('rental-transferred-next', only_html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
