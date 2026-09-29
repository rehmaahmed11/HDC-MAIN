#!/usr/bin/env python3
"""The collapsible sidebar groups (``core/sidebar_groups.js``) in a browser-like DOM.

``shared/base.html`` renders the groups with the current page's section open and
the rest merely *rendered*; the script decides what the user actually sees:

* the group holding the current page is always opened;
* every other group follows the browser's saved choice, defaulting to open on a
  wide screen and closed on a phone (the drawer is small there);
* a later class change — Bootstrap's ``show``, the footer "Expand / Collapse all
  sections" button, a heading click — is written back to ``localStorage``;
* the mid-animation ``collapsing`` class is never persisted;
* corrupt or read-only storage cannot break the drawer, and pages without a
  drawer (login) can load the file safely.

Those rules are JavaScript, so they are checked by running the real file under
Node against a minimal DOM (``tests/sidebar_groups_harness.js``) — the same
approach ``test_combo_widget.py`` uses for ``core/combo.js``.  The server side
(grouping, highlights, role gating, icons) is pinned in
``test_sidebar_navigation.py``.

Run with:
    python -m unittest tests.test_sidebar_groups_js -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'sidebar_groups_harness.js')


class SidebarGroupsHarnessTestCase(unittest.TestCase):
    @unittest.skipIf(shutil.which('node') is None, 'node is not installed')
    def test_sidebar_group_behaviours(self):
        result = subprocess.run(
            ['node', HARNESS, REPO_ROOT],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            'sidebar_groups.js harness failed:\n%s%s' % (result.stdout, result.stderr),
        )
        self.assertIn('all assertions passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
