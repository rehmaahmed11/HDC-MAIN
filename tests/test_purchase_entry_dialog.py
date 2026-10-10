"""The entry-result dialogue box on Purchases, Delivery and Usage.

Those three screens of the Purchase V2 section record stock one entry at a time,
and until now every entry reported itself the same way: the server redirected,
``flash()`` dropped a sentence into the banner strip at the top of a long page,
and the operator — eyes still on the form they had just filled — saw nothing.
A saved purchase and a rejected one looked identical.

The fix is a dialogue box that must be acknowledged: ``core/dialog.js`` reads
the flash queue (mounted per page by ``shared/_result_dialog.html``) and states
the verdict, and the same box refuses an incomplete entry *before* it is posted.

What is pinned here:

* the three sections mount the dialog, and the banner stays for no-JavaScript
  browsers (so the message is never lost, only upgraded);
* the mount must sit in ``extra_js`` — after base.html pops the flash queue —
  because a second ``get_flashed_messages()`` returns nothing;
* the JSON payload carries the real sentence and the right tone for a save, a
  warning (duplicate prevention) and each kind of refusal;
* the entry forms opt in (``data-hdc-dialog``) and carry the page rules that
  know the stock limits locally;
* pages outside the three sections are untouched (opt-in, not a global sweep);
* ``dialog.js`` is loaded before ``forms.js``, which is what lets a blocked
  submit release the double-click lock instead of eating the retry.

The behaviour itself is JavaScript, so it is checked by running the real files
under Node (``tests/dialog_harness.js``) — the same approach
``test_sidebar_groups_js.py`` uses.

Run with:
    python -m unittest tests.test_purchase_entry_dialog -v
"""
import json
import os
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Dialog-Test-1234')

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.materials import MaterialV2, PurchaseV2, Supplier
from hdc.models.projects import Project, Stage
from hdc.utils.dates import _pkt_now_naive

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD_ID = 'hdcResultDialogData'
DIALOG_URL = '/hdc/purchase-v2/purchases'
DELIVERY_URL = '/hdc/purchase-v2/delivered'
USAGE_URL = '/hdc/purchase-v2/usage'

# url -> (template, the form that must opt in to the dialog)
SECTIONS = (
    (DIALOG_URL, 'templates/hdc/purchase/purchase_v2_purchases.html', 'purchaseV2Form'),
    (DELIVERY_URL, 'templates/hdc/purchase/purchase_v2_delivered.html', 'deliveryForm'),
    (USAGE_URL, 'templates/hdc/purchase/purchase_v2_usage.html', 'usageV2Form'),
)


def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as handle:
        return handle.read()


def payload_in(body):
    """The flash payload the page handed to core/dialog.js, or None."""
    match = re.search(r'<script id="%s" type="application/json">(.*?)</script>' % PAYLOAD_ID,
                      body, re.S)
    return json.loads(match.group(1)) if match else None


def kinds_and_text(payload):
    return [(item['kind'], item['text']) for item in (payload or {}).get('items', [])]


class PurchaseEntryDialogFixture(unittest.TestCase):
    """Shared disposable app with one supplier, material, project and stage."""

    def setUp(self):
        self.tmp = __import__('tempfile').TemporaryDirectory(prefix='hdc-entry-dialog-')
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'dialog.db')
        env = patch.dict(os.environ, {'HDC_ENV': 'test', 'HDC_SECRET_KEY': 'qa-only',
                                      'HDC_BOOTSTRAP_ADMIN_PASSWORD': 'Dialog-Test-1234',
                                      'HDC_DB_PATH': self.path, 'HDC_INSTANCE_DIR': self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.app = create_app({'TESTING': True, 'HDC_DB_PATH': self.path,
                               'HDC_INSTANCE_DIR': self.tmp.name})
        self.addCleanup(self.dispose)
        self.client = self.app.test_client()
        self.login()
        with self.app.app_context():
            self.supplier = Supplier(name='Dialog Supplier')
            self.material = MaterialV2(name='Dialog Cement', unit='BAG')
            project = Project(project_code='DLG-1', name='Dialog Project')
            db.session.add_all([self.supplier, self.material, project])
            db.session.flush()
            stage = Stage(name='Dialog Slab', project_id=project.id)
            db.session.add(stage)
            db.session.commit()
            self.supplier_id, self.material_id = self.supplier.id, self.material.id
            self.project_id, self.stage_id = project.id, stage.id
            self.token = self.csrf_token()

    def dispose(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def login(self):
        self.client.get('/hdc/login')
        with self.client.session_transaction() as session:
            token = session['_csrf_token']
        response = self.client.post('/hdc/login', data={'username': 'admin',
                                      'password': 'Dialog-Test-1234', '_csrf_token': token})
        self.assertEqual(response.status_code, 302)

    def csrf_token(self):
        with self.client.session_transaction() as session:
            return session['_csrf_token']

    def post(self, url, **data):
        response = self.client.post(url, data=dict(data, _csrf_token=self.token),
                                    follow_redirects=True)
        self.assertIn(response.status_code, (200, 302), response.get_data(as_text=True)[:300])
        return response.get_data(as_text=True)

    def make_purchase(self, quantity=100.0):
        with self.app.app_context():
            row = PurchaseV2(supplier_id=self.supplier_id, material_id=self.material_id,
                             unit_price=50.0, quantity=quantity, total_amount=50.0 * quantity,
                             payment_status='unpaid', date=_pkt_now_naive().date(),
                             is_void=False, created_at=_pkt_now_naive(),
                             updated_at=_pkt_now_naive())
            db.session.add(row)
            db.session.commit()
            return row.id


class DialogMountTest(PurchaseEntryDialogFixture):
    """Every section of the trio behaves the same way."""

    def test_each_section_mounts_the_dialog_and_opts_its_entry_form_in(self):
        for url, template, form_id in SECTIONS:
            body = self.client.get(url).get_data(as_text=True)
            with self.subTest(url=url):
                self.assertIn('id="flashMessages"', body, 'banner is still rendered (no-JS fallback)')
                self.assertIn('core/dialog.js', body, 'the dialog engine is loaded')
                source = _read(template)
                self.assertIn("{% include 'shared/_result_dialog.html' %}", source)
                match = re.search(r'<form[^>]*id="%s"[^>]*>' % form_id, source)
                self.assertTrue(match, '%s not found in %s' % (form_id, template))
                self.assertIn('data-hdc-dialog', match.group(0),
                              '%s must be validated by the dialog before it posts' % form_id)

    def test_payload_is_mounted_after_the_banner_not_before_it(self):
        """base.html pops the flash queue once; a mount above it would be empty.

        A silent failure is the danger here: the dialog would simply never open
        and every save would go back to reporting itself in the banner only.
        """
        body = self.post(DIALOG_URL, supplier_id='',
                         **{'material_id[]': [str(self.material_id)], 'quantity[]': ['5'],
                            'unit_price[]': ['100']})
        banner_at, payload_at = body.find('id="flashMessages"'), body.find('id="%s"' % PAYLOAD_ID)
        self.assertGreater(payload_at, -1, 'the payload must be present when a save reports')
        self.assertGreater(payload_at, banner_at, 'and only because it is emitted after the pop')

    def test_pages_outside_the_three_sections_are_untouched(self):
        """The dialog is mounted per section on purpose.

        The Materials screen shares the flash plumbing but has its own busy
        layout; it must keep the plain banner until someone opts it in.
        """
        body = self.post('/hdc/purchase-v2/materials', name='', unit='BAG')
        self.assertIn('Material name is required.', body, 'the banner still reports there')
        self.assertIsNone(payload_in(body), 'without a dialog mount, no payload is written')

    def test_dialog_script_is_loaded_once_and_before_forms_js(self):
        base = _read('templates/hdc/shared/base.html')
        scripts = re.findall(r'<script src="(/hdc_static/js/core/[a-z_]+\.js)"></script>', base)
        self.assertEqual(scripts.count('/hdc_static/js/core/dialog.js'), 1,
                         'loaded exactly once, by the shared layout')
        self.assertLess(scripts.index('/hdc_static/js/core/dialog.js'),
                        scripts.index('/hdc_static/js/core/forms.js'),
                        'a blocked submit has to release the forms.js double-click lock')

    def test_dialog_asset_is_served_and_parses(self):
        response = self.client.get('/hdc_static/js/core/dialog.js')
        self.assertEqual(response.status_code, 200)
        self.assertIn('hdcDialog', response.get_data(as_text=True))
        if shutil.which('node') is None:
            self.skipTest('node is not installed')
        result = subprocess.run(['node', '--check',
                                 os.path.join(REPO_ROOT, 'static/hdc/js/core/dialog.js')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dialog_has_theme_aware_styling(self):
        """An unstyled dialog would read as part of the form behind it."""
        css = _read('static/hdc/css/hdc.css')
        for hook in ('.hdc-result-dialog', '.hdc-result-icon', '.hdc-result-bad', '.hdc-result-ok'):
            self.assertIn(hook, css, hook + ' missing from hdc.css')
        self.assertIn('[data-theme="dark"] .hdc-result-dialog', css, 'dark mode must be covered')


class DialogServerResultTest(PurchaseEntryDialogFixture):
    """The verdict the server flashed, as the dialog receives it."""

    def test_saved_purchase_reports_success(self):
        body = self.post(DIALOG_URL, supplier_id=str(self.supplier_id), payment_status='unpaid',
                         **{'material_id[]': [str(self.material_id)], 'quantity[]': ['10'],
                            'unit_price[]': ['250'], 'notes[]': ['']})
        self.assertEqual(kinds_and_text(payload_in(body)),
                         [('success', '1 purchase item(s) recorded successfully.')])

    def test_missing_supplier_reports_the_refusal_in_danger_tone(self):
        body = self.post(DIALOG_URL, supplier_id='', payment_status='unpaid',
                         **{'material_id[]': [str(self.material_id)], 'quantity[]': ['10'],
                            'unit_price[]': ['250'], 'notes[]': ['']})
        kind, text = kinds_and_text(payload_in(body))[0]
        self.assertEqual((kind, text), ('danger', 'Valid supplier is required.'))
        self.assertIn('alert alert-danger', body, 'the banner stays as the no-JS fallback')

    def test_duplicate_prevention_is_not_filed_as_a_plain_save(self):
        """A warning has to change the headline, or the dialog lies.

        The server rolls the batch back and flashes 'warning'; shown under
        "Entry Saved" the operator would move on and the PO would not exist.
        """
        args = dict(supplier_id=str(self.supplier_id), payment_status='unpaid', date='2026-01-05',
                    **{'material_id[]': [str(self.material_id)], 'quantity[]': ['10'],
                       'unit_price[]': ['250'], 'notes[]': ['']})
        self.post(DIALOG_URL, **args)
        body = self.post(DIALOG_URL, **args)
        kinds = [kind for kind, _ in kinds_and_text(payload_in(body))]
        self.assertEqual(kinds, ['warning'], 'the second, identical entry is refused')
        self.assertIn('Duplicate purchase prevented', kinds_and_text(payload_in(body))[0][1])
        with self.app.app_context():
            self.assertEqual(PurchaseV2.query.filter(PurchaseV2.is_void == False).count(), 1,
                             'and only one row exists')

    def test_over_delivery_reports_the_stock_limit(self):
        purchase_id = self.make_purchase(quantity=10)
        body = self.post(DELIVERY_URL, purchase_id=str(purchase_id), project_id=str(self.project_id),
                         stage_id=str(self.stage_id), quantity='999', delivery_person='', notes='')
        kinds = kinds_and_text(payload_in(body))
        self.assertEqual(len(kinds), 1)
        self.assertEqual(kinds[0][0], 'danger')
        self.assertIn('Cannot deliver more than remaining purchase quantity (10.00)', kinds[0][1])

    def test_successful_delivery_reports_success(self):
        purchase_id = self.make_purchase(quantity=10)
        body = self.post(DELIVERY_URL, purchase_id=str(purchase_id), project_id=str(self.project_id),
                         stage_id=str(self.stage_id), quantity='4', delivery_person='Imran', notes='')
        kind, text = kinds_and_text(payload_in(body))[0]
        self.assertEqual(kind, 'success')
        self.assertRegex(text, r'^Delivery #\d+ recorded\.$')

    def test_usage_beyond_stage_stock_reports_the_refusal(self):
        purchase_id = self.make_purchase(quantity=10)
        self.post(DELIVERY_URL, purchase_id=str(purchase_id), project_id=str(self.project_id),
                  stage_id=str(self.stage_id), quantity='4', delivery_person='', notes='')
        body = self.post(USAGE_URL, purchase_id=str(purchase_id), material_id=str(self.material_id),
                         project_id=str(self.project_id), stage_id=str(self.stage_id), quantity='9')
        kind, text = kinds_and_text(payload_in(body))[0]
        self.assertEqual(kind, 'danger')
        self.assertIn('Usage exceeds available stock for selected purchase order in this stage (4.00)', text)

    def test_successful_usage_reports_success(self):
        purchase_id = self.make_purchase(quantity=10)
        self.post(DELIVERY_URL, purchase_id=str(purchase_id), project_id=str(self.project_id),
                  stage_id=str(self.stage_id), quantity='4', delivery_person='', notes='')
        body = self.post(USAGE_URL, purchase_id=str(purchase_id), material_id=str(self.material_id),
                         project_id=str(self.project_id), stage_id=str(self.stage_id), quantity='4')
        kind, text = kinds_and_text(payload_in(body))[0]
        self.assertEqual(kind, 'success')
        self.assertRegex(text, r'^Usage #\d+ recorded\.$')

    def test_missing_stage_is_reported_as_its_own_refusal(self):
        purchase_id = self.make_purchase(quantity=10)
        body = self.post(USAGE_URL, purchase_id=str(purchase_id), material_id=str(self.material_id),
                         project_id=str(self.project_id), stage_id='', quantity='1')
        self.assertEqual(kinds_and_text(payload_in(body)),
                         [('danger', 'Stage is required for strict stock control.')])

    def test_payload_is_json_safe_against_markup_in_a_flashed_message(self):
        """Whatever the server flashes is data for core/dialog.js, never markup.

        The message is injected straight into the session so this pins the
        template escaping itself, rather than whatever sanitising one particular
        model happens to apply to its names.
        """
        hostile = '</script><img src=x onerror=alert(1)>'
        with self.client.session_transaction() as session:
            session['_flashes'] = [('danger', hostile)]   # the shape flash() itself stores
        body = self.client.get(DIALOG_URL).get_data(as_text=True)
        kind, text = kinds_and_text(payload_in(body))[0]
        self.assertEqual((kind, text), ('danger', hostile), 'the sentence arrives intact')
        self.assertNotIn('<img src=x onerror=alert(1)>', body,
                         'but only ever escaped, so it cannot close the payload early')

    def test_a_plain_page_load_has_no_dialog_to_play(self):
        for url, _template, _form in SECTIONS:
            with self.subTest(url=url):
                self.assertIsNone(payload_in(self.client.get(url).get_data(as_text=True)))


class DialogPageRulesTest(unittest.TestCase):
    """The client-side rules each section owes (they know the stock locally)."""

    def test_every_section_registers_a_save_rule(self):
        for url, template, form_id in SECTIONS:
            with self.subTest(template=template):
                source = _read(template)
                self.assertIn('hdcDialog.beforeSave', source, 'the form must be checked before it posts')
                self.assertIn("'%s'" % form_id, source)
                self.assertIn('window.hdcDialog', source, 'and degrade quietly when JS is off')

    def test_delivery_and_usage_rules_quote_the_stock_that_is_left(self):
        for template in ('templates/hdc/purchase/purchase_v2_delivered.html',
                         'templates/hdc/purchase/purchase_v2_usage.html'):
            source = _read(template)
            with self.subTest(template=template):
                self.assertIn('toLocaleString', source, 'quantities in the message are formatted')
                self.assertIn('maximumFractionDigits', source)

    def test_blank_purchase_rows_are_the_rule_to_judge_not_the_browser_s(self):
        """`required` on every item cell would block a deliberately blank row.

        The rows carry data-hdc-dialog-skip so the page rule decides what a
        complete item is (a leftover empty row is not an error), while the
        native attributes stay for browsers without JavaScript.
        """
        source = _read('templates/hdc/purchase/purchase_v2_purchases.html')
        self.assertEqual(source.count('data-hdc-dialog-skip="true"'), 3,
                         'material, quantity and rate of the item row')
        self.assertEqual(source.count('required data-hdc-dialog-skip'), 3)

    def test_a_failed_edit_does_not_stack_two_dialogs(self):
        """The delivery page re-opens the editor on load; the verdict comes first."""
        source = _read('templates/hdc/purchase/purchase_v2_delivered.html')
        self.assertIn('hdcDialog.hasPendingResult()', source)


class DialogHarnessTest(unittest.TestCase):
    @unittest.skipIf(shutil.which('node') is None, 'node is not installed')
    def test_dialog_js_behaviours(self):
        result = subprocess.run(
            ['node', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dialog_harness.js'),
             REPO_ROOT],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0,
                         'dialog_harness.js failed:\n%s%s' % (result.stdout, result.stderr))
        self.assertIn('assertions groups passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
