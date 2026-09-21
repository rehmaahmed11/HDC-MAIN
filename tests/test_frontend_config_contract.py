"""The contract between a page template and the static script it loads.

Audit Step 12 moved the inline logic of the big Accounts pages into cacheable
files under ``static/hdc/js/pages/``.  The server data those scripts used to
receive as interpolated Jinja now arrives through a
``<script id="…Config" type="application/json">`` block.

That move introduced a new failure mode with no natural error message: the
template keeps rendering, the script keeps loading, and nothing happens —
because the script reads a config key the template does not send.  (It caught
one during the Step 12 follow-up: the edit panel of the accounts workspace read
``editTxn`` through a variable that was out of scope, so the edit form silently
stopped pre-filling.)

These tests make that failure loud:

* every key a page script reads must be present in the JSON the page renders;
* the edit renders must carry the full edit payload;
* each extracted asset must be served, and must parse as JavaScript;
* the templates must no longer carry inline logic.
"""

import json
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
from hdc.models.accounts import Account, AccountTransaction

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as handle:
        return handle.read()

# template -> (config element id, script that reads it, variable it reads through)
PAGES = (
    {
        'name': 'accounts workspace',
        'template': 'templates/hdc/accounts/accounts.html',
        'url': '/hdc/accounts',
        'config_id': 'accountsWorkspaceConfig',
        'script': 'static/hdc/js/pages/accounts_workspace.js',
        'style': 'static/hdc/css/accounts_workspace.css',
        'variables': ('__hdcCfg',),
    },
    {
        'name': 'accounts entries',
        'template': 'templates/hdc/accounts/accounts_entries.html',
        'url': '/hdc/accounts/entries',
        'config_id': 'accountsEntriesConfig',
        'script': 'static/hdc/js/pages/accounts_entries.js',
        'style': 'static/hdc/css/accounts_entries.css',
        'variables': ('__hdcEntriesConfig',),
    },
    {
        'name': 'money center',
        'template': 'templates/hdc/accounts/money_center.html',
        'url': '/hdc/accounts/money-center',
        'config_id': 'moneyCenterConfig',
        'script': 'static/hdc/js/pages/money_center.js',
        'style': 'static/hdc/css/money_center.css',
        'variables': ('__cfg',),
    },
)

# The edit-only payloads, read through their own variable inside the script.
# template -> the variable the panel reads through, the config key holding the
# payload, and a source anchor that marks where the panel starts (so only the
# panel's own reads are checked).
EDIT_PAYLOADS = {
    'templates/hdc/accounts/accounts.html': {
        'variable': '__hdcEdit', 'key': 'editTxn', 'anchor': 'cfg.editTxn',
    },
    'templates/hdc/accounts/accounts_entries.html': {
        'variable': '__hdcEntriesConfig', 'key': 'editTxn',
        'anchor': '__hdcEntriesConfig.editTxn',
        # This panel renames the payload to a local `edit`, so its reads have
        # to be collected through that alias as well.
        'alias': 'edit', 'alias_binding': 'var edit = __hdcEntriesConfig.editTxn;',
    },
}


def _config_block(html, config_id):
    """Return the decoded JSON of the named config block, or fail loudly."""
    match = re.search(
        r'<script id="%s" type="application/json">\s*(.*?)\s*</script>' % re.escape(config_id),
        html,
        re.S,
    )
    if not match:
        raise AssertionError('no <script id="%s" type="application/json"> block rendered' % config_id)
    try:
        return json.loads(match.group(1))
    except ValueError as exc:  # pragma: no cover - the failure message is the point
        raise AssertionError(
            'the %s config block is not valid JSON (%s). Every interpolated value '
            'needs a |tojson filter.' % (config_id, exc)
        )


def _keys_read(script_source, variable):
    """Every ``variable.key`` the script reads, in source order."""
    return sorted(set(re.findall(r'\b%s\.([A-Za-z_$][\w$]*)' % re.escape(variable), script_source)))


class FrontendConfigContractTestCase(unittest.TestCase):
    """What a page promises the script it loads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-frontend-contract-')
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
        # An edit render needs one posted row to point at.
        account = Account.query.first()
        self.txn = AccountTransaction(
            type='party_payment',
            category='expense',
            amount=100.0,
            amount_minor=10000,
            from_account_id=account.id,
            executed_by_account_id=account.id,
            is_void=False,
            party_name='Frontend Contract Probe',
        )
        db.session.add(self.txn)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.tmp.cleanup()

    # -- the contract itself ------------------------------------------------

    def test_every_config_key_a_script_reads_is_rendered_by_its_page(self):
        checked = 0
        for page in PAGES:
            with self.subTest(page=page['name']):
                checked += 1
                response = self.client.get(page['url'])
                self.assertEqual(response.status_code, 200, page['url'])
                config = _config_block(response.get_data(as_text=True), page['config_id'])
                source = _read(page['script'])
                missing = set()
                for variable in page['variables']:
                    missing |= set(_keys_read(source, variable)) - set(config)
                self.assertTrue(_keys_read(source, page['variables'][0]),
                                '%s reads nothing from the config - was it renamed?' % page['script'])
                self.assertEqual(
                    missing,
                    set(),
                    '%s reads %s from %s but the template does not send it'
                    % (page['script'], sorted(missing), page['config_id']),
                )
        self.assertEqual(checked, len(PAGES), 'expected to check %d pages' % len(PAGES))

    def test_edit_renders_carry_the_keys_the_edit_panel_reads(self):
        covered = 0
        for page in PAGES:
            payload = EDIT_PAYLOADS.get(page['template'])
            if not payload:
                continue
            covered += 1
            variable, payload_key = payload['variable'], payload['key']
            with self.subTest(page=page['name']):
                url = '%s?edit_txn_id=%d' % (page['url'], self.txn.id)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, url)
                config = _config_block(response.get_data(as_text=True), page['config_id'])
                self.assertIsInstance(config.get(payload_key), dict,
                                      '%s should render a %s object on the edit render' % (url, payload_key))
                source = _read(page['script'])
                # Only the keys read through this payload variable, so the
                # top-level keys read through `variable` are not required here.
                section = source[source.index(payload['anchor']):]
                # The anchor line itself reads `variable.payload_key`; that is
                # the payload, not one of the fields inside it.
                reads = set(_keys_read(section, variable)) - {payload_key}
                if payload.get('alias'):
                    # Guard against the silent no-op: if the panel stops
                    # binding the payload to this alias, fail rather than
                    # quietly stop checking its fields.
                    self.assertIn(payload['alias_binding'], source,
                                  'the %s panel no longer binds %s to `%s` - update EDIT_PAYLOADS'
                                  % (page['name'], payload['anchor'], payload['alias']))
                    reads |= set(_keys_read(section, payload['alias']))
                missing = reads - set(config[payload_key])
                self.assertEqual(missing, set(),
                                 '%s pre-fills %s from keys the page does not send' % (page['script'], sorted(missing)))
        # A guard against the classic silent no-op: if the templates are ever
        # renamed, this test must fail rather than quietly check nothing.
        self.assertEqual(covered, len(EDIT_PAYLOADS),
                         'expected to check %d edit renders, checked %d' % (len(EDIT_PAYLOADS), covered))

    def test_edit_payload_is_absent_on_a_plain_render(self):
        covered = 0
        for page in PAGES:
            payload = EDIT_PAYLOADS.get(page['template'])
            if not payload:
                continue
            covered += 1
            payload_key = payload['key']
            with self.subTest(page=page['name']):
                response = self.client.get(page['url'])
                config = _config_block(response.get_data(as_text=True), page['config_id'])
                self.assertIsNone(config.get(payload_key),
                                  'a plain render must not carry an %s payload' % payload_key)
        self.assertEqual(covered, len(EDIT_PAYLOADS),
                         'expected to check %d edit renders, checked %d' % (len(EDIT_PAYLOADS), covered))

    # -- the assets themselves ----------------------------------------------

    def test_extracted_assets_are_served_and_parse(self):
        for page in PAGES:
            with self.subTest(page=page['name']):
                for asset in (page['script'], page['style']):
                    path = os.path.join(REPO_ROOT, asset)
                    self.assertTrue(os.path.isfile(path), '%s is missing' % asset)
                    served = self.client.get('/hdc_static/' + asset.split('static/hdc/', 1)[1])
                    try:
                        self.assertEqual(served.status_code, 200, '%s is not served' % asset)
                    finally:
                        served.close()  # release the file handle the static route opened

    @unittest.skipIf(shutil.which('node') is None, 'node is not installed')
    def test_extracted_scripts_parse_as_javascript(self):
        for page in PAGES:
            with self.subTest(page=page['name']):
                result = subprocess.run(
                    ['node', '--check', os.path.join(REPO_ROOT, page['script'])],
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0,
                                 '%s does not parse:\n%s' % (page['script'], result.stderr.strip()))

    def test_templates_carry_data_not_logic(self):
        """The extracted templates must have no inline logic left."""
        for page in PAGES:
            with self.subTest(page=page['name']):
                source = _read(page['template'])
                self.assertIn(page['script'].replace('static/hdc/', '/hdc_static/'), source,
                              '%s does not load %s' % (page['template'], page['script']))
                inline = [
                    block for attrs, block in re.findall(
                        r'<script\b([^>]*)>(.*?)</script>', source, re.S,
                    )
                    if 'src=' not in attrs and 'application/json' not in attrs
                ]
                self.assertEqual(inline, [],
                                 '%s still carries %d inline script block(s)' % (page['template'], len(inline)))


if __name__ == '__main__':
    unittest.main()
