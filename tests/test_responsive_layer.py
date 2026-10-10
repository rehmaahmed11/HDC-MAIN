"""The adaptive layer must reach every page, and must stay switched on.

Everything the phone/tablet layout does hangs off two files that
`templates/hdc/shared/base.html` loads:

* `static/hdc/css/responsive.css`      — the breakpoint layer itself
* `static/hdc/js/core/responsive_tables.js` — turns tables into record cards

If either stops being linked, or stops being linked *last*, the app silently
falls back to the desktop layout and the operator on a phone is back to
sideways scrolling.  These tests make that failure loud instead.
"""
import os
import re
import subprocess
import sys
import unittest

from qa_support import IsolatedAppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, 'scripts')
CSS_REL = 'static/hdc/css/responsive.css'
JS_REL = 'static/hdc/js/core/responsive_tables.js'


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as handle:
        return handle.read()


class StaticGateTest(unittest.TestCase):
    """scripts/qa_responsive.py is the single source of truth for the layer."""

    def test_responsive_gate_passes(self):
        result = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'qa_responsive.py'), '--json'],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class LayoutContractTest(unittest.TestCase):
    def setUp(self):
        self.base = _read('templates', 'hdc', 'shared', 'base.html')

    def test_stylesheet_is_loaded_last(self):
        """Later rules win; the adaptive layer has to come after hdc.css."""
        links = re.findall(r'<link[^>]+href="([^"]+\.css[^"]*)"', self.base)
        self.assertTrue(any('responsive.css' in href for href in links),
                        'base.html does not load responsive.css')
        self.assertIn('responsive.css', links[-1])

    def test_assets_are_served_from_the_local_static_folder(self):
        """No CDN: the app must work offline on a site with no internet."""
        for rel in (CSS_REL, JS_REL):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)

    def test_script_loads_after_the_audit_column(self):
        """core/audit.js appends 'Entered by' asynchronously; the label pass
        has to run after it, or that column is the one cell without a title."""
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', self.base)
        audit = [i for i, s in enumerate(scripts) if s.endswith('core/audit.js')]
        tables = [i for i, s in enumerate(scripts) if s.startswith('/hdc_static/js/core/responsive_tables.js')]
        self.assertTrue(audit and tables, 'both scripts must be loaded by base.html')
        self.assertGreater(tables[0], audit[0])

    def test_javascript_is_valid(self):
        result = subprocess.run(['node', '--check', os.path.join(ROOT, JS_REL)],
                                capture_output=True, text=True)
        if result.returncode != 0 and 'No such file or directory' in (result.stderr or ''):
            self.skipTest('node is not available in this environment')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_viewport_lets_the_browser_do_its_job(self):
        match = re.search(r'<meta name="viewport" content="([^"]*)"', self.base)
        self.assertIsNotNone(match)
        content = match.group(1)
        self.assertIn('width=device-width', content)
        self.assertIn('viewport-fit=cover', content)
        self.assertNotIn('maximum-scale', content)
        self.assertNotIn('user-scalable=no', content)


class RenderedPageTest(IsolatedAppTest):
    """A real page must actually ship the layer, stamped against staleness."""

    def test_pages_ship_the_adaptive_layer(self):
        for url in ('/hdc/', '/hdc/projects', '/hdc/workers', '/hdc/accounts'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            body = response.get_data(as_text=True)
            self.assertRegex(body, r'/hdc_static/css/responsive\.css\?v=\d+',
                             '%s does not link the stamped adaptive stylesheet' % url)
            self.assertRegex(body, r'/hdc_static/js/core/responsive_tables\.js\?v=\d+',
                             '%s does not load the stamped table script' % url)

    def test_asset_stamp_is_a_positive_integer(self):
        stamp = self.app.jinja_env.globals['hdc_asset_stamp']
        self.assertIsInstance(stamp, int)
        self.assertGreater(stamp, 0)


if __name__ == '__main__':
    unittest.main()
