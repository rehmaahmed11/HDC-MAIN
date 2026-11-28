"""Regression tests for frontend & layout usability upgrades."""
from __future__ import annotations

import glob
import os
import re
import unittest

from qa_support import IsolatedAppTest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


class UsabilityUpgradesTests(IsolatedAppTest):
    def test_global_shell_usability_controls(self):
        res = self.client.get('/hdc/')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn('data-bs-theme=', html)
        self.assertIn('id="sidebarPin"', html)
        self.assertIn('id="quickJumpBtn"', html)
        self.assertIn('topbar-breadcrumb', html)
        self.assertIn('/hdc_static/js/core/layout.js', html)
        self.assertIn('/hdc_static/js/core/quick_jump.js', html)
        # accounts.css must NOT leak onto non-accounts routes
        self.assertNotIn('/hdc_static/css/accounts.css', html)

    def test_accounts_css_loaded_only_on_accounts_routes(self):
        acc = self.client.get('/hdc/accounts')
        self.assertEqual(acc.status_code, 200)
        self.assertIn('/hdc_static/css/accounts.css', acc.get_data(as_text=True))

        purch = self.client.get('/hdc/purchase-v2')
        self.assertEqual(purch.status_code, 200)
        self.assertNotIn('/hdc_static/css/accounts.css', purch.get_data(as_text=True))

    def test_section_subnav_present_across_major_modules(self):
        routes = [
            '/hdc/purchase-v2',
            '/hdc/purchase-v2/purchases',
            '/hdc/accounts',
            '/hdc/projects',
            '/hdc/workers',
            '/hdc/attendance',
            '/hdc/payroll',
            '/hdc/office-management/staff',
        ]
        for route in routes:
            with self.subTest(route=route):
                res = self.client.get(route)
                self.assertEqual(res.status_code, 200)
                html = res.get_data(as_text=True)
                self.assertIn('hdc-subnav', html)

    def test_collapsible_entry_drawers_rendered_on_heavy_registers(self):
        routes = [
            '/hdc/purchase-v2/purchases',
            '/hdc/purchase-v2/delivered',
            '/hdc/purchase-v2/usage',
            '/hdc/expenses',
        ]
        for route in routes:
            with self.subTest(route=route):
                res = self.client.get(route)
                self.assertEqual(res.status_code, 200)
                html = res.get_data(as_text=True)
                self.assertIn('data-hdc-entry-drawer=', html)
                self.assertIn('data-hdc-entry-toggle', html)
                self.assertIn('data-hdc-entry-pin', html)

    def test_all_templates_have_accessible_close_and_icon_buttons(self):
        pattern = os.path.join(REPO_ROOT, 'templates', 'hdc', '**', '*.html')
        templates = sorted(glob.glob(pattern, recursive=True))
        self.assertGreater(len(templates), 100)
        for path in templates:
            rel = os.path.relpath(path, REPO_ROOT)
            with open(path, 'r', encoding='utf-8') as fh:
                txt = fh.read()
            for m in re.finditer(r'<button\b[^>]*\bbtn-close\b[^>]*>', txt):
                self.assertIn('aria-label=', m.group(0), f'{rel}: bare .btn-close missing aria-label')
            for m in re.finditer(r'<(button|a)\b([^>]*)>(\s*<i\b[^>]*>\s*</i>\s*)</\1>', txt, flags=re.S):
                self.assertIn('aria-label=', m.group(2), f'{rel}: icon-only <{m.group(1)}> missing aria-label')


if __name__ == '__main__':
    unittest.main()
