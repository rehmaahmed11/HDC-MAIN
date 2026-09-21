"""Searchable combo boxes on the Accounts Hub transaction forms.

The New Transaction surfaces reached from the Accounts Hub (the Create
Transaction form on /hdc/accounts plus its Update Transaction modal, and the
Money Center quick-entry form) turn every party / person / account / project /
stage pick into a type-to-search combo box built on ``HDCComboList``
(static/hdc/js/core/combo.js).

Three contracts are guarded here:

* the widget itself behaves (strict pick-from-list, blur revert, mirroring
  of programmatic select changes, required-ownership transfer) — verified by
  running the real combo.js under Node with a minimal DOM stub
  (tests/combo_widget_harness.js);
* every page that should offer searchable combos renders both the input and
  the select it mirrors;
* the page script actually wires every rendered input to its select, so the
  template and the extracted JS cannot drift apart silently.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app
from hdc.extensions import db

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# page url -> (template path, page script path)
COMBO_PAGES = {
    '/hdc/accounts': (
        'templates/hdc/accounts/accounts.html',
        'static/hdc/js/pages/accounts_workspace.js',
    ),
    '/hdc/accounts/money-center': (
        'templates/hdc/accounts/money_center.html',
        'static/hdc/js/pages/money_center.js',
    ),
}

# The searchable combo pairs each page promises: combo input id -> mirrored
# select id (the select keeps its name= and is what actually gets posted).
EXPECTED_COMBO_PAIRS = {
    '/hdc/accounts': [
        ('from_account_input', 'from_account'),
        ('to_account_input', 'to_account'),
        ('project_id_input', 'project_id'),
        ('stage_id_input', 'stage_id'),
        ('related_entity_id_input', 'related_entity_id'),
        ('edit_from_account_input', 'edit_from_account'),
        ('edit_to_account_input', 'edit_to_account'),
        ('edit_project_id_input', 'edit_project_id'),
        ('edit_stage_id_input', 'edit_stage_id'),
        ('edit_related_id_input', 'edit_related_id'),
    ],
    '/hdc/accounts/money-center': [
        ('mc_from_account_input', 'mc_from_account'),
        ('mc_to_account_input', 'mc_to_account'),
        ('mc_project_input', 'mc_project'),
        ('mc_stage_input', 'mc_stage'),
        ('mc_related_id_input', 'mc_related_id'),
        ('mc_exp_cat_input', 'mc_exp_cat'),
    ],
}


class ComboWidgetHarnessTestCase(unittest.TestCase):
    """The core widget, executed for real under Node."""

    @unittest.skipIf(shutil.which('node') is None, 'node is not installed')
    def test_combo_widget_behaviours(self):
        result = subprocess.run(
            ['node', os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'combo_widget_harness.js'), REPO_ROOT],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            'combo.js harness failed:\n%s%s' % (result.stdout, result.stderr),
        )


class TransactionComboPagesTestCase(unittest.TestCase):
    """Every party/person pick on the transaction forms is a combo pair."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-combo-pages-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp.name, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp.name,
            'TESTING': True,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.client.get('/hdc/login')
        with self.client.session_transaction() as session:
            token = session['_csrf_token']
        response = self.client.post('/hdc/login', data={
            'username': 'admin',
            'password': os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD'],
            '_csrf_token': token,
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.tmp.cleanup()

    def test_pages_render_and_wire_every_combo_pair(self):
        for url, (template, script) in COMBO_PAGES.items():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, url)
                html = response.get_data(as_text=True)

                with open(os.path.join(REPO_ROOT, template), encoding='utf-8') as fh:
                    template_source = fh.read()
                with open(os.path.join(REPO_ROOT, script), encoding='utf-8') as fh:
                    script_source = fh.read()

                for input_id, select_id in EXPECTED_COMBO_PAIRS[url]:
                    # The searchable input is rendered next to its select...
                    self.assertIn('id="%s"' % input_id, html,
                                  '%s does not render the combo input %s' % (url, input_id))
                    # ...both live in the template itself...
                    self.assertIn('id="%s"' % input_id, template_source,
                                  '%s carries %s but the template does not' % (template, input_id))
                    self.assertIn('id="%s"' % select_id, template_source,
                                  '%s: combo input %s has no select to mirror'
                                  % (template, input_id))
                    # ...and the page script wires the pair to HDCComboList.
                    self.assertRegex(
                        script_source,
                        r"attachCombo\(\s*'%s'\s*,\s*'%s'" % (re.escape(input_id), re.escape(select_id)),
                        '%s never attaches %s to %s — the combo input would '
                        'post nothing and the select would stay hidden'
                        % (script, input_id, select_id),
                    )

    def test_money_center_loader_functions_are_untouched_by_combo_wiring(self):
        """The combo wiring must not creep into the loader functions the
        Money Center node test executes in a bare sandbox."""
        path = os.path.join(REPO_ROOT, 'static/hdc/js/pages/money_center.js')
        with open(path, encoding='utf-8') as fh:
            source = fh.read()
        for name in ('loadWorkerOptions', 'loadSupplierOptions',
                     'loadSubcontractorOptions', 'loadOfficeStaffOptions',
                     'loadAllOptions', 'loadRelatedOptions'):
            match = re.search(
                r'(?:async )?function %s\([^)]*\) \{.*?^\}' % re.escape(name),
                source, re.S | re.M,
            )
            self.assertIsNotNone(match, name)
            self.assertNotIn('hdc:sync-combos', match.group(),
                             '%s must stay free of combo wiring' % name)


if __name__ == '__main__':
    unittest.main()
