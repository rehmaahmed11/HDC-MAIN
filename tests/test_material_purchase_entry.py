"""Itemised material purchases on Accounts → Spent, across all entry surfaces.

These tests deliberately reject the old amount-only contract. Purchases stay
one cash movement; stock and supplier bill settlement remain module-owned.
"""
import csv
import io
import json
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from werkzeug.datastructures import MultiDict

from qa_support import IsolatedAppTest
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.cashflow import CashFlowCategory, CashFlowEntry, CashFlowEntryAudit, CashFlowEntryItem
from hdc.models.materials import PurchaseV2, SupplierLedger
from hdc.models.projects import Project
from hdc.services.accounts import _account_balance_map
from hdc.services.cashflow_register import (
    amend_manual_cash_flow_entry, cash_flow_item_dicts, category_field_rules,
    register_row_dicts, register_rows, restore_manual_cash_flow_entry,
    save_manual_cash_flow_entry, subcategory_options, void_manual_cash_flow_entry,
)
from hdc.services.transaction_entry import create_entry_from_form

ROOT = Path(__file__).resolve().parents[1]
SURFACES = ('/hdc/accounts/money-center', '/hdc/accounts/new-transaction',
            '/hdc/accounts/cashflow/register')


class MaterialPurchaseEntryTest(IsolatedAppTest):
    def setUp(self):
        super().setUp()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.addCleanup(db.session.remove)
        self.cash = Account(name='Material cash', type='cash', status='active', is_void=False,
                            opening_balance=500000, opening_balance_minor=50000000)
        self.project = Project(name='Islamabad site', project_code='MAT-TEST', client='Site owner', status='Active')
        db.session.add_all([self.cash, self.project])
        db.session.commit()
        self.category = CashFlowCategory.query.filter_by(name='Material & Purchase').one()
        self.materials = {s.name: s.id for s in subcategory_options(self.category.id)}

    def payload(self, **changes):
        data = dict(direction='out', date='2026-10-17', amount='25030',
                    account_id=str(self.cash.id), project_id=str(self.project.id),
                    category_id=str(self.category.id), party_name='Site materials supplier',
                    party_type='supplier', reference='', description='Site materials',
                    subcategory_ids=[str(self.materials['Cement']), str(self.materials['Steel / Saria'])],
                    material_quantities=['20', '0.1'], material_units=['bags', 'kg'],
                    material_unit_prices=['1,250', '300'],
                    material_specifications=['OPC 50 kg bags', 'Grade 60'])
        data.update(changes)
        return data

    def post(self, payload, url=SURFACES[0], *, key=None):
        return self.client.post(url, data=dict(payload, action='create_entry',
                                _csrf_token=self.token, _idempotency_key=key or ''),
                                follow_redirects=True)

    def save(self, **changes):
        return create_entry_from_form(MultiDict(self.payload(**changes)), actor='admin')[0]

    def counts(self):
        return tuple(model.query.count() for model in
                     (CashFlowEntry, CashFlowEntryItem, CashFlowEntryAudit, AccountTransaction))

    def test_all_surfaces_require_complete_purchase_and_post_one_movement(self):
        for url in SURFACES:
            with self.subTest(url=url):
                before = self.counts()
                response = self.post(dict(direction='out', date='2026-10-17', amount='25030',
                                         account_id=self.cash.id, category_id=self.category.id), url)
                self.assertIn('needs a project', response.get_data(as_text=True))
                self.assertEqual(self.counts(), before)
                response = self.post(self.payload(reference=url), url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('This transaction was not saved.', response.get_data(as_text=True))
                after = self.counts()
                self.assertEqual(after, (before[0] + 1, before[1] + 2, before[2] + 1, before[3] + 1))
        self.assertEqual(_account_balance_map()[self.cash.id], 500000 - 3 * 25030)
        self.assertEqual(PurchaseV2.query.count(), 0, 'no silent stock/PO posting')
        self.assertEqual(SupplierLedger.query.count(), 0, 'no second supplier settlement')

    def test_details_are_stored_audited_printed_and_exported(self):
        entry = self.save(reference='MAT-123')
        items = entry.items
        self.assertEqual([i.item_name for i in items], ['Cement', 'Steel / Saria'])
        self.assertEqual([i.quantity for i in items], [Decimal('20.000'), Decimal('0.100')])
        self.assertEqual([i.unit_price_minor for i in items], [125000, 30000])
        self.assertEqual([i.line_total_minor for i in items], [2500000, 3000])
        self.assertEqual(entry.amount_minor, 2503000)
        self.assertEqual(entry.account_tx.amount_minor, entry.amount_minor)
        self.assertEqual(entry.account_tx.project_id, self.project.id)
        snapshot = json.loads(CashFlowEntryAudit.query.filter_by(entry_id=entry.id).one().after_json)
        self.assertEqual(snapshot['items'][0]['unit'], 'bags')
        self.assertEqual(snapshot['items'][1]['line_total_minor'], 3000)
        self.assertEqual(snapshot['items'][1]['specification'], 'Grade 60')
        with self.app.test_request_context():
            from flask import url_for
            receipt_url = url_for('hdc_account_transaction_receipt', txn_id=entry.account_tx_id)
        receipt = self.client.get(receipt_url).get_data(as_text=True)
        for value in ('Material Purchase Details', 'Islamabad site', 'OPC 50 kg bags', 'Grade 60', 'bags', '1,250.00', '25,000.00'):
            self.assertIn(value, receipt)
        history = self.client.get(SURFACES[2]).get_data(as_text=True)
        self.assertIn('20 bags × Rs. 1,250.00 = Rs. 25,000.00', history)
        exported = self.client.get(SURFACES[2] + '/export').get_data(as_text=True)
        rows = list(csv.DictReader(io.StringIO(exported)))
        self.assertEqual(rows[0]['Amount (PKR)'], '25030.00')
        self.assertIn('Cement (OPC 50 kg bags): 20 bags x 1250.00 = 25000.00 PKR', rows[0]['Material Details'])
        self.assertEqual([r.id for r in register_rows(search='Grade 60')], [entry.id])

    def test_missing_required_fields_and_blank_rows_never_post(self):
        cases = [
            ({'project_id': ''}, 'needs a project'),
            ({'project_id': '99999999'}, 'project no longer exists'),
            ({'party_name': ''}, 'needs a party'),
            ({'party_name': '', 'party_id': '999999'}, 'needs a party'),
            ({'party_type': 'worker'}, 'Supplier / Vendor'),
            ({'subcategory_ids': ['', str(self.materials['Steel / Saria'])]}, 'choose a material name'),
            ({'subcategory_ids': ['0', str(self.materials['Steel / Saria'])]}, 'searchable list'),
            ({'material_quantities': ['', '0.1']}, 'quantity must be'),
            ({'material_quantities': ['20']}, 'Each material row'),
            ({'material_units': ['', 'kg']}, 'enter a unit'),
            ({'material_units': ['x' * 31, 'kg']}, 'enter a unit'),
            ({'material_unit_prices': ['', '300']}, 'unit price must be'),
            ({'material_specifications': ['x' * 121, '']}, '120 characters'),
        ]
        for change, message in cases:
            with self.subTest(change=change):
                before = self.counts()
                response = self.post(self.payload(**change))
                self.assertIn(message, response.get_data(as_text=True))
                self.assertEqual(self.counts(), before)
                self.assertEqual(_account_balance_map()[self.cash.id], 500000)
        bare = self.payload()
        for field in ('subcategory_ids', 'material_quantities', 'material_units',
                      'material_unit_prices', 'material_specifications'):
            bare.pop(field)
        self.assertIn('Add at least one material', self.post(bare).get_data(as_text=True))
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_quantity_rate_and_total_validation_is_server_authoritative(self):
        for field, bad_values in (
            ('material_quantities', ['0', '-2', 'NaN', 'Infinity', '1e3', '1.2345', '1000000001', 'abc']),
            ('material_unit_prices', ['0', '-2', 'NaN', 'Infinity', 'abc', '1000000000001']),
        ):
            for bad in bad_values:
                with self.subTest(field=field, bad=bad):
                    data = self.payload()
                    data[field][0] = bad
                    with self.assertRaises(ValueError):
                        create_entry_from_form(MultiDict(data))
                    db.session.rollback()
                    self.assertEqual(self.counts(), (0, 0, 0, 0))
        with self.assertRaisesRegex(ValueError, 'must match the material total'):
            self.save(amount='25000', material_line_totals=['25000', '0'])
        db.session.rollback()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_fractional_quantities_and_half_paisa_round_once_per_line(self):
        entry = self.save(amount='25.05', material_quantities=['2.5', '0.125'],
                          material_unit_prices=['10.01', '0.12'])
        self.assertEqual([i.line_total_minor for i in entry.items], [2503, 2])
        self.assertEqual(entry.amount_minor, 2505)
        entry = self.save(amount='3.13', material_quantities=['3', '1'],
                          material_unit_prices=['1.005', '0.10'])
        self.assertEqual([i.unit_price_minor for i in entry.items], [101, 10])
        self.assertEqual(entry.amount_minor, 313)

    def test_inactive_wrong_category_duplicate_and_too_many_items_rejected(self):
        labour = CashFlowCategory.query.filter_by(name='Labour & Wages').one()
        wrong_id = subcategory_options(labour.id)[0].id
        for ids, message in (([wrong_id, self.materials['Steel / Saria']], 'not a subcategory'),
                             ([self.materials['Cement']] * 2, 'only once')):
            with self.subTest(ids=ids):
                self.assertIn(message, self.post(self.payload(subcategory_ids=ids)).get_data(as_text=True))
        item = subcategory_options(self.category.id)[0]
        item.is_active = False
        db.session.commit()
        self.assertIn('no longer available', self.post(self.payload(subcategory_ids=[item.id, item.id])).get_data(as_text=True))
        with self.assertRaisesRegex(ValueError, 'no more than 20'):
            save_manual_cash_flow_entry(direction='out', amount=100, account_id=self.cash.id,
                category_id=self.category.id, project_id=self.project.id, party_name='Supplier',
                material_items=[dict(subcategory_id=self.materials['Cement'], quantity=1, unit='bag', unit_price=5)] * 21)
        db.session.rollback()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_drafts_keep_blank_row_positions_and_all_purchase_details(self):
        for url in SURFACES:
            with self.subTest(url=url):
                data = self.payload(subcategory_ids=['', str(self.materials['Steel / Saria'])])
                html = self.post(data, url).get_data(as_text=True)
                for element, value in (('txnMaterialQty0', '20'), ('txnMaterialQty1', '0.1'),
                                       ('txnMaterialPrice0', '1,250'), ('txnMaterialPrice1', '300'),
                                       ('txnMaterialUnit0', 'bags'), ('txnMaterialSpec1', 'Grade 60')):
                    self.assertRegex(html, rf'id="{element}"[^>]*value="{re.escape(value)}"')
                first = re.search(r'<select[^>]*id="txnMaterialItemSelect0".*?</select>', html, re.S).group()
                second = re.search(r'<select[^>]*id="txnMaterialItemSelect1".*?</select>', html, re.S).group()
                self.assertNotIn('selected', first)
                self.assertRegex(second, rf'value="{self.materials["Steel / Saria"]}"[^>]*selected')
                fresh = self.client.get(url).get_data(as_text=True)
                self.assertNotIn('txnMaterialItemInput1', fresh)

    def test_idempotency_void_restore_and_amend_keep_details(self):
        data = self.payload()
        self.post(data, key='material-double-click')
        self.post(data, key='material-double-click')
        self.assertEqual(self.counts(), (1, 2, 1, 1))
        original = CashFlowEntry.query.one()
        original_items = cash_flow_item_dicts(original.items)
        void_manual_cash_flow_entry(original, reason='Check receipt', actor='admin')
        self.assertEqual(_account_balance_map()[self.cash.id], 500000)
        restore_manual_cash_flow_entry(original, actor='admin')
        self.assertEqual(cash_flow_item_dicts(original.items), original_items)
        self.assertEqual(_account_balance_map()[self.cash.id], 474970)
        replacement, old = amend_manual_cash_flow_entry(original, description='Corrected note', reason='Note typo')
        self.assertEqual(cash_flow_item_dicts(replacement.items), original_items)
        self.assertTrue(old.is_void)
        self.assertEqual(_account_balance_map()[self.cash.id], 474970)
        with self.assertRaisesRegex(ValueError, 'must match the material total'):
            amend_manual_cash_flow_entry(replacement, amount=30000, reason='Wrong amount only')
        db.session.rollback()
        self.assertFalse(db.session.get(CashFlowEntry, replacement.id).is_void)
        self.assertEqual(_account_balance_map()[self.cash.id], 474970)
        corrected, _ = amend_manual_cash_flow_entry(replacement, amount=32000, reason='Corrected material',
            material_items=[dict(subcategory_id=self.materials['Sand / Crush'], quantity=4,
                                 unit='loads', unit_price=8000, specification='Crush only')])
        self.assertEqual(corrected.subcategory_id, self.materials['Sand / Crush'])
        self.assertEqual(len(corrected.items), 1)
        self.assertEqual(corrected.items[0].line_total_minor, 3200000)
        self.assertEqual(_account_balance_map()[self.cash.id], 468000)

    def test_old_optional_rules_cannot_hide_purchase_requirements(self):
        self.category.party_mode = 'none'
        self.category.project_mode = 'optional'
        db.session.commit()
        rules = category_field_rules(self.category)
        self.assertEqual(rules['party_mode'], 'required')
        self.assertEqual(rules['project_mode'], 'required')
        self.assertEqual(rules['party_types'], ('supplier',))
        for url in SURFACES:
            page = self.client.get(url).get_data(as_text=True)
            option = re.search(rf'<option value="{self.category.id}"\s+data-direction=.*?>', page, re.S).group()
            self.assertIn('data-project-mode="required"', option)
            self.assertIn('data-party-mode="required"', option)

    def test_existing_name_only_rows_upgrade_without_invented_quantities(self):
        # Simulate history that predates itemised purchases, not a new entry.
        entry, _ = save_manual_cash_flow_entry(direction='out', amount=123, account_id=self.cash.id,
            category_id=self.category.id, subcategory_id=self.materials['Cement'], enforce_category_rules=False)
        entry.items.append(CashFlowEntryItem(subcategory_id=self.materials['Cement'], item_name='Historic cement', sort_order=0))
        db.session.add(entry)
        db.session.commit()
        entry_id = entry.id
        db.session.remove()
        with db.engine.begin() as conn:
            for column in ('quantity', 'unit', 'unit_price_minor', 'line_total_minor', 'specification'):
                conn.execute(text(f'ALTER TABLE hdc_cash_flow_entry_item DROP COLUMN {column}'))
        from hdc.core.schema import _ensure_cashflow_schema
        for _ in range(2):
            _ensure_cashflow_schema()
        legacy = db.session.get(CashFlowEntry, entry_id)
        self.assertEqual(legacy.amount_minor, 12300)
        self.assertEqual(legacy.items[0].item_name, 'Historic cement')
        self.assertIsNone(legacy.items[0].quantity)
        self.assertIsNone(legacy.items[0].unit_price_minor)
        self.assertEqual(register_row_dicts([legacy])[0]['material_items'][0]['quantity'], '')
        self.assertIn('Quantity / price not recorded.', self.client.get(SURFACES[2]).get_data(as_text=True))
        self.assertEqual(db.session.execute(text('PRAGMA foreign_key_check')).all(), [])
        corrected, original = amend_manual_cash_flow_entry(legacy, reference='HIST-123', reason='Reference typo')
        self.assertTrue(original.is_void)
        self.assertEqual(corrected.amount_minor, 12300)
        self.assertIsNone(corrected.items[0].quantity, 'do not invent quantities for history')
        self.assertEqual(_account_balance_map()[corrected.account_id], 499877)

    def test_real_rendered_form_in_jsdom(self):
        if not shutil.which('node') or not (ROOT / 'tests/node_modules/jsdom').exists():
            self.skipTest('Install tests/package.json and Node to run DOM regression tests.')
        page = self.client.get(SURFACES[0]).get_data(as_text=True)
        html_path = Path(self.tmp.name) / 'material-form.html'
        html_path.write_text(page)
        result = subprocess.run(['node', str(ROOT / 'tests/material_purchase_harness.js'), str(ROOT),
                                 str(html_path)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        self.assertIn('material purchase DOM checks passed', result.stdout)
