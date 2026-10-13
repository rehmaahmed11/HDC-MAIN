#!/usr/bin/env python3
"""The Expense Categories row-rename behaviour, in a DOM.

The rename field is revealed by the page script
(``static/hdc/js/pages/expense_categories.js``) — the server-side test can only
prove the markup shipped, not that pressing the pencil does the right thing.
This test renders the real page with the Flask client and runs it under Node
(``tests/expense_categories_harness.js``), the same approach
``test_purchase_delivery_js.py`` and ``test_sidebar_groups_js.py`` use.

What is pinned:

* every row is read-only until its pencil is pressed — one live input per row,
  no text box behind every name;
* the pencil opens the field focused and selected;
* only one row is open at a time;
* Escape and the ✕ both close the row and put the original name back;
* the Save button stays inside the row's ``<form>`` (so the entry-result
  dialogue and the double-submit lock keep working) and both row buttons sit in
  the Actions cell together.

Run with:
    python -m unittest tests.test_expense_categories_js -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from qa_support import IsolatedAppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'expense_categories_harness.js')
PAGE_SCRIPT = os.path.join(REPO_ROOT, 'static', 'hdc', 'js', 'pages',
                           'expense_categories.js')


class ExpenseCategoriesHarnessTest(IsolatedAppTest):
    def _rendered_page(self):
        for name in ('Site Utilities', 'Food & Refreshment', 'Petrol / Diesel'):
            self.form('/hdc/expense_categories', action='add_category', category_name=name)
        response = self.client.get('/hdc/expense_categories')
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_page_script_is_linked(self):
        html = self._rendered_page()
        self.assertIn('/hdc_static/js/pages/expense_categories.js', html)

    @unittest.skipIf(shutil.which('node') is None, 'node is not installed')
    def test_row_rename_behaviour_in_a_dom(self):
        html = self._rendered_page()
        with tempfile.TemporaryDirectory(prefix='hdc-expense-js-') as tmp:
            page = os.path.join(tmp, 'page.html')
            with open(page, 'w', encoding='utf-8') as handle:
                handle.write(html)
            result = subprocess.run(
                ['node', HARNESS, page],
                capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
            )
        if result.returncode != 0 and 'Cannot find module' in result.stderr:
            self.skipTest('jsdom is not installed (cd tests && npm install)')
        self.assertEqual(
            result.returncode, 0,
            'expense categories harness failed:\n%s%s' % (result.stdout, result.stderr),
        )

    def test_page_script_stays_free_of_jquery_and_inline_handlers(self):
        with open(PAGE_SCRIPT, encoding='utf-8') as handle:
            source = handle.read()
        for banned in ('$(', 'jQuery', 'onclick='):
            self.assertNotIn(banned, source,
                             'page scripts must stay dependency-free')


if __name__ == '__main__':
    unittest.main()
