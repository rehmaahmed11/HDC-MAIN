#!/usr/bin/env python3
"""The Delivery / Usage register JS (``pages/purchase_delivery.js``) in a DOM.

Two of the field requests in ``DELIVERY_USAGE_REGISTER_SPEC.md`` are pure
front-end behaviour, so the server-side test
(``test_delivery_filters_and_newest_first.py``) can only prove the data reached
the page — not that the pop-up reacts to it.  This test closes that gap by
running the real script under Node against a minimal DOM
(``tests/purchase_delivery_harness.js``), the same approach
``test_sidebar_groups_js.py`` and ``test_combo_widget.py`` use.

What is pinned:

* the Pending Purchases pop-up filters by site, material, supplier and free
  text, on its own and combined — and the site filter means "purchase orders
  that already delivered to this site";
* the summary line, the "nothing matches" row and the Reset-filters button all
  follow the filter;
* an empty pending list never shows the "nothing matches" row;
* the All Sites / All Stages buttons clear only their own filter, keep the rest
  and re-run the search;
* the stage dropdown narrows to the selected site and drops a stage that falls
  outside it;
* a page without the pop-up or the filter bar loads the script safely.

Run with:
    python -m unittest tests.test_purchase_delivery_js -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'purchase_delivery_harness.js')
SCRIPT = os.path.join(REPO_ROOT, 'static', 'hdc', 'js', 'pages',
                      'purchase_delivery.js')


class PurchaseDeliveryHarnessTestCase(unittest.TestCase):
    @unittest.skipIf(shutil.which('node') is None, 'node is not installed')
    def test_register_behaviours(self):
        result = subprocess.run(
            ['node', HARNESS, REPO_ROOT],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            'purchase_delivery.js harness failed:\n%s%s' % (result.stdout, result.stderr),
        )
        self.assertIn('all assertions passed', result.stdout)

    @unittest.skipIf(shutil.which('node') is None, 'node is not installed')
    def test_script_passes_node_syntax_check(self):
        result = subprocess.run(['node', '--check', SCRIPT],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_both_registers_load_the_shared_script(self):
        """The two stock registers must bind the same implementation."""
        for template in ('purchase_v2_delivered.html', 'purchase_v2_usage.html'):
            with self.subTest(template=template):
                path = os.path.join(REPO_ROOT, 'templates', 'hdc', 'purchase', template)
                with open(path, encoding='utf-8') as handle:
                    source = handle.read()
                self.assertIn('/hdc_static/js/pages/purchase_delivery.js', source)
                self.assertIn('hdcPurchaseDelivery.bindAllScopeButtons', source)
        with open(os.path.join(REPO_ROOT, 'templates', 'hdc', 'purchase',
                               'purchase_v2_delivered.html'), encoding='utf-8') as handle:
            self.assertIn('hdcPurchaseDelivery.bindPendingPopup', handle.read())


if __name__ == '__main__':
    unittest.main()
