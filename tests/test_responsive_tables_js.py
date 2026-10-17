#!/usr/bin/env python3
"""The phone view of a list row, in a DOM.

PR #135 replaced the two bespoke category workspaces with the shared page
head, table shell and row-rename component.  The rename component keeps its
Save / Cancel buttons inside a `d-none` form, and `responsive_tables.js` — the
script that turns a table row into a record card on a phone — counted those
hidden buttons as content.  The NAME cell was therefore "a bank of buttons"
and the Created date became the card title.

This test renders the real pages and runs the real script under Node
(``tests/responsive_tables_harness.js``, jsdom).  It pins the decision:
name = card title, date = labelled field, actions = visible.

Run with:
    python -m unittest tests.test_responsive_tables_js -v
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
                       'responsive_tables_harness.js')


class ResponsiveTablesHarnessTest(IsolatedAppTest):
    def _run(self, url, seed):
        for payload in seed:
            self.form(url, **payload)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        with tempfile.TemporaryDirectory(prefix='hdc-rt-') as tmp:
            page = os.path.join(tmp, 'page.html')
            with open(page, 'w', encoding='utf-8') as handle:
                handle.write(response.get_data(as_text=True))
            result = subprocess.run(
                ['node', HARNESS, page],
                capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
            )
        if result.returncode != 0 and 'Cannot find module' in result.stderr:
            self.skipTest('jsdom is not installed (cd tests && npm install)')
        self.assertEqual(
            result.returncode, 0,
            'responsive tables harness failed for %s:\n%s%s'
            % (url, result.stdout, result.stderr),
        )

    def test_expense_categories_titles_the_name_not_the_date(self):
        self._run('/hdc/expense_categories', [
            {'action': 'add_category', 'category_name': 'Site Utilities'},
            {'action': 'add_category', 'category_name': 'Material Transport'},
        ])

    def test_trades_titles_the_name_not_the_date(self):
        self._run('/hdc/trades', [
            {'action': 'add_trade', 'trade_name': 'Mason'},
            {'action': 'add_trade', 'trade_name': 'Electrician'},
        ])


if __name__ == '__main__':
    unittest.main()
