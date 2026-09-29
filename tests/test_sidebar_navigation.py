#!/usr/bin/env python3
"""Regression tests for the grouped, collapsible sidebar (shared/base.html).

The drawer used to be a flat list of 19 rows plus two loose sections.  It is
now built from one ``sidebar_sections`` table in the template: every option
lives under a group heading, each heading opens/closes its list with an arrow,
the group holding the current page is opened and highlighted, and the open/
closed choice is remembered by ``core/sidebar_groups.js``.

These tests pin the parts that break silently:

* every option still exists, exactly once, and under the expected group;
* every heading owns a collapse target, and the current page's group is open;
* the "where am I" rule — longest matching URL prefix — keeps a child page
  (/hdc/accounts/manage) from highlighting its parent (Accounts Hub);
* role gating (staff never sees Accounts/Administration, accountants see
  Accounts but not Administration);
* every Font Awesome class on the page exists in the vendored CSS, and the new
  script is served from ``/hdc_static`` and parses.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \
        python -m unittest tests.test_sidebar_navigation -v
"""

from __future__ import annotations

import html as html_module
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
from hdc.models.auth import HDCUser                                # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
FONT_AWESOME = os.path.join(ROOT, 'static/hdc/vendor/fontawesome/css/all.min.css')

# The option set the sidebar must keep offering, in rendering order.
# (group id, heading, option hrefs in order)
EXPECTED_GROUPS = (
    ('overview', 'Overview', ('/hdc/', '/hdc/alerts')),
    ('projects', 'Projects & Estimation',
     ('/hdc/projects', '/hdc/stages', '/hdc/estimation', '/hdc/project-estimation')),
    ('workforce', 'Workforce',
     ('/hdc/subcontractors', '/hdc/workers', '/hdc/trades', '/hdc/timekeeping',
      '/hdc/payroll')),
    ('costs', 'Costs & Overheads',
     ('/hdc/expenses', '/hdc/expense_categories', '/hdc/office-management',
      '/hdc/personal-management')),
    ('materials', 'Materials & Tools',
     ('/hdc/purchase-v2', '/hdc/tool-rental/dashboard')),
    ('parties', 'Parties', ('/hdc/parties',)),
    ('reports', 'Reports', ('/hdc/reports', '/hdc/reports/glance')),
    ('accounts', 'Accounts & Cash',
     ('/hdc/accounts/hub', '/hdc/accounts/manage', '/hdc/accounts/entries',
      '/hdc/accounts/shared')),
    ('administration', 'Administration',
     ('/hdc/users', '/hdc/event-recorder', '/hdc/settings')),
)
# Groups every signed-in role sees; the rest are gated.
COMMON_GROUP_COUNT = 7
GATED_GROUPS = {
    'admin': ('accounts', 'administration'),
    'accountant': ('accounts',),
    'staff': (),
    'manager': (),
}

# page URL -> (row that must be highlighted, group that must be open)
HIGHLIGHT_CASES = (
    ('/hdc/', '/hdc/', 'overview'),
    ('/hdc/alerts', '/hdc/alerts', 'overview'),
    ('/hdc/projects', '/hdc/projects', 'projects'),
    ('/hdc/stages', '/hdc/stages', 'projects'),
    ('/hdc/estimation', '/hdc/estimation', 'projects'),
    ('/hdc/project-estimation', '/hdc/project-estimation', 'projects'),
    ('/hdc/subcontractors', '/hdc/subcontractors', 'workforce'),
    ('/hdc/workers', '/hdc/workers', 'workforce'),
    ('/hdc/trades', '/hdc/trades', 'workforce'),
    ('/hdc/timekeeping', '/hdc/timekeeping', 'workforce'),
    ('/hdc/payroll', '/hdc/payroll', 'workforce'),
    ('/hdc/expenses', '/hdc/expenses', 'costs'),
    ('/hdc/expense_categories', '/hdc/expense_categories', 'costs'),
    ('/hdc/office-management', '/hdc/office-management', 'costs'),
    ('/hdc/personal-management', '/hdc/personal-management', 'costs'),
    ('/hdc/purchase-v2', '/hdc/purchase-v2', 'materials'),
    ('/hdc/tool-rental/inventory', '/hdc/tool-rental/dashboard', 'materials'),
    ('/hdc/parties', '/hdc/parties', 'parties'),
    ('/hdc/reports', '/hdc/reports', 'reports'),
    ('/hdc/reports/glance', '/hdc/reports/glance', 'reports'),
    ('/hdc/accounts/hub', '/hdc/accounts/hub', 'accounts'),
    ('/hdc/accounts/manage', '/hdc/accounts/manage', 'accounts'),
    ('/hdc/accounts/entries', '/hdc/accounts/entries', 'accounts'),
    ('/hdc/accounts/shared/report', '/hdc/accounts/shared', 'accounts'),
    ('/hdc/accounts/cashflow', '/hdc/accounts/hub', 'accounts'),
    ('/hdc/users', '/hdc/users', 'administration'),
    ('/hdc/event-recorder', '/hdc/event-recorder', 'administration'),
    ('/hdc/settings', '/hdc/settings', 'administration'),
)

NAV_ITEM_RE = re.compile(
    r'<li class="nav-item(?P<active> active)?">\s*<a href="(?P<href>[^"]+)"', re.S)
NAV_GROUP_RE = re.compile(
    r'<li class="nav-group(?P<current> has-current)?"\s*\n\s*'
    r'data-hdc-nav-section="(?P<id>[a-z]+)">\s*\n'
    r'\s*<button type="button" class="nav-group-toggle" data-bs-toggle="collapse"\s*\n'
    r'\s*data-bs-target="#(?P<target>[^"]+)"\s*\n'
    r'\s*aria-controls="(?P<controls>[^"]+)"\s*\n'
    r'\s*aria-expanded="(?P<expanded>true|false)">(?P<body>.*?)</ul>\s*\n\s*</li>',
    re.S)
GROUP_ID_RE = re.compile(r'<li class="nav-group(?: has-current)?"\s*\n'
                         r'\s*data-hdc-nav-section="([a-z]+)">')


class SidebarNavigationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-sidebar-test-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp, 'TESTING': True})
        # No long-lived app context: Flask-Login caches the signed-in user on
        # ``g``, so one context shared by the whole test would ignore the role
        # changes these tests make between requests.
        self.client = self.app.test_client()
        self.client.get('/hdc/login')
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']
        res = self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD,
            '_csrf_token': self.token})
        self.assertEqual(res.status_code, 302)
        self.users = {'admin': None}
        with self.app.app_context():
            admin = HDCUser.query.filter_by(username='admin').one()
            self.users['admin'] = admin.id
            for role in ('accountant', 'staff', 'manager'):
                user = HDCUser(username='sidebar-' + role, password_hash='unused',
                               role=role)
                db.session.add(user)
                db.session.flush()
                self.users[role] = user.id
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────
    def _as(self, role):
        with self.client.session_transaction() as session:
            session.clear()
            session['_csrf_token'] = self.token
            session['_user_id'] = str(self.users[role])
            session['_fresh'] = True

    def _nav(self, path, role='admin'):
        self._as(role)
        res = self.client.get(path)
        self.assertEqual(res.status_code, 200, path)
        html = res.get_data(as_text=True)
        start = html.index('<nav id="sidebar"')
        return html[start:html.index('</nav>', start)]

    def _groups(self, nav):
        return list(NAV_GROUP_RE.finditer(nav))

    def _items(self, nav):
        return list(NAV_ITEM_RE.finditer(nav))

    def _active_hrefs(self, nav):
        return [m.group('href') for m in self._items(nav) if m.group('active')]

    def _current_group_ids(self, nav):
        return [g.group('id') for g in self._groups(nav) if g.group('current')]

    # ── option set and grouping ──────────────────────────────────────────
    def test_every_option_is_kept_exactly_once(self):
        nav = self._nav('/hdc/')
        hrefs = [m.group('href') for m in self._items(nav)]
        self.assertEqual(len(hrefs), len(set(hrefs)), 'duplicate option rendered')
        for _gid, _label, options in EXPECTED_GROUPS:
            for option in options:
                self.assertEqual(hrefs.count(option), 1, option)

    def test_options_are_rendered_under_their_group_in_order(self):
        nav = self._nav('/hdc/')
        self.assertEqual([m.group(1) for m in GROUP_ID_RE.finditer(nav)],
                         [group[0] for group in EXPECTED_GROUPS])
        groups = self._groups(nav)
        self.assertEqual(len(groups), len(EXPECTED_GROUPS))
        for group, (group_id, heading, options) in zip(groups, EXPECTED_GROUPS):
            self.assertEqual(group.group('id'), group_id)
            self.assertIn(heading, html_module.unescape(group.group('body')))
            self.assertEqual([m.group('href') for m in self._items(group.group('body'))],
                             list(options), group_id)

    def test_accounts_group_states_the_money_badge(self):
        nav = self._nav('/hdc/')
        self.assertIn('Money</span>', nav)          # badge kept from the old nav
        self.assertIn('audit</span>', nav)          # All Entries note kept

    # ── collapse plumbing ────────────────────────────────────────────────
    def test_every_heading_owns_its_collapse_target(self):
        nav = self._nav('/hdc/')
        for group in self._groups(nav):
            self.assertEqual(group.group('target'), group.group('controls'))
            self.assertIn('id="%s"' % group.group('target'), nav)
            self.assertIn('class="sidebar-subnav collapse', group.group('body'))
            self.assertIn('nav-group-arrow', group.group('body'))

    def test_current_group_is_open_and_marked(self):
        nav = self._nav('/hdc/accounts/manage')
        self.assertEqual(self._current_group_ids(nav), ['accounts'])
        groups = [g for g in self._groups(nav) if g.group('id') == 'accounts']
        self.assertEqual(groups[0].group('expanded'), 'true')
        self.assertRegex(nav, r'class="sidebar-subnav collapse show"\s*\n\s*'
                              r'id="sidebarSection-accounts"')
        # only the current group is open in the server-rendered markup
        self.assertEqual(nav.count('sidebar-subnav collapse show'), 1)

    def test_expand_all_control_uses_the_shared_collapse_helper(self):
        nav = self._nav('/hdc/')
        self.assertIn('data-hdc-collapse-group=".sidebar-subnav"', nav)
        self.assertIn('data-expand-label="Expand all sections"', nav)
        self.assertIn('data-collapse-label="Collapse all sections"', nav)
        self.assertIn('data-hdc-collapse-icon', nav)

    # ── "where am I" ─────────────────────────────────────────────────────
    def test_current_page_highlights_the_right_row_and_group(self):
        for path, href, group_id in HIGHLIGHT_CASES:
            nav = self._nav(path)
            self.assertEqual(self._active_hrefs(nav), [href], path)
            self.assertEqual(self._current_group_ids(nav), [group_id], path)
            self.assertRegex(nav, r'nav-item active">\s*<a href="%s" aria-current="page"'
                             % re.escape(href))

    def test_child_page_beats_its_parent(self):
        nav = self._nav('/hdc/accounts/manage')
        self.assertIn('<a href="/hdc/accounts/manage" aria-current="page"', nav)
        self.assertNotIn('<a href="/hdc/accounts/hub" aria-current="page"', nav)

    def test_page_without_its_own_row_highlights_its_entry_point(self):
        # Cash Flow is reached from the Hub; tool pages from the Tools dashboard.
        for path, href in (('/hdc/accounts/cashflow', '/hdc/accounts/hub'),
                           ('/hdc/tool-rental/reports', '/hdc/tool-rental/dashboard')):
            nav = self._nav(path)
            self.assertEqual(self._active_hrefs(nav), [href], path)

    # ── role gating ──────────────────────────────────────────────────────
    def test_roles_only_see_the_groups_they_may_use(self):
        for role, extra in GATED_GROUPS.items():
            nav = self._nav('/hdc/', role=role)
            expected = [g[0] for g in EXPECTED_GROUPS[:COMMON_GROUP_COUNT]] + list(extra)
            self.assertEqual([g.group('id') for g in self._groups(nav)],
                             expected, role)
            hrefs = [m.group('href') for m in self._items(nav)]
            for _gid, _label, options in EXPECTED_GROUPS[:COMMON_GROUP_COUNT]:
                for option in options:
                    self.assertIn(option, hrefs, (role, option))
            self.assertEqual('/hdc/accounts/hub' in hrefs, 'accounts' in extra, role)
            self.assertEqual('/hdc/settings' in hrefs, role == 'admin', role)

    # ── assets ───────────────────────────────────────────────────────────
    def test_sidebar_script_is_served_and_parses(self):
        res = self.client.get('/hdc_static/js/core/sidebar_groups.js')
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn('hdc_sidebar_sections', body)          # storage key
        self.assertIn('has-current', body)
        self.assertIn('openAll', body)                       # the real script, not a 404
        with open(os.path.join(ROOT, 'templates/hdc/shared/base.html'),
                  encoding='utf-8') as handle:
            self.assertIn('src="/hdc_static/js/core/sidebar_groups.js"', handle.read())
        if shutil.which('node'):
            result = subprocess.run(
                ['node', '--check',
                 os.path.join(ROOT, 'static/hdc/js/core/sidebar_groups.js')],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_icon_classes_exist_in_the_vendored_font_awesome(self):
        with open(FONT_AWESOME, encoding='utf-8') as handle:
            css = handle.read()
        with open(os.path.join(ROOT, 'templates/hdc/shared/base.html'),
                  encoding='utf-8') as handle:
            page = handle.read()
        icons = sorted(set(re.findall(r'\bfa-[a-z0-9-]+', page)))
        missing = [i for i in icons
                   if ('.%s:before' % i) not in css and ('.%s,' % i) not in css]
        self.assertEqual(missing, [], 'icons missing from Font Awesome 6.4.0')
        self.assertIn('fa-chevron-down', icons)      # the group arrow
        self.assertIn('fa-angles-down', icons)       # the expand-all arrow

    def test_collapse_helper_uses_icons_that_exist(self):
        with open(os.path.join(ROOT, 'static/hdc/js/core/collapse_groups.js'),
                  encoding='utf-8') as handle:
            js = handle.read()
        with open(FONT_AWESOME, encoding='utf-8') as handle:
            css = handle.read()
        swapped = re.findall(r"'(fa-[a-z0-9-]+)'", js)
        self.assertTrue(swapped)
        for icon in swapped:
            self.assertIn('.%s:before' % icon, css, icon)


if __name__ == '__main__':
    unittest.main()
