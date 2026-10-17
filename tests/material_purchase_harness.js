/* Run the real controller + combos against the server's actual form HTML. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

(async () => {
  const [root, htmlFile] = process.argv.slice(2);
  const dom = new JSDOM(fs.readFileSync(htmlFile, 'utf8'), {
    url: 'https://preview.e2b.app/hdc/accounts/money-center',
    runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const { window } = dom;
  const doc = window.document;
  if (doc.readyState === 'loading') {
    await new Promise(resolve => doc.addEventListener('DOMContentLoaded', resolve, { once: true }));
  }
  window.HTMLElement.prototype.scrollIntoView = function () {};
  for (const script of ['core/combo.js', 'pages/new_transaction.js']) {
    window.eval(fs.readFileSync(path.join(root, 'static/hdc/js', script), 'utf8'));
  }
  const byId = id => doc.getElementById(id);
  const change = (el, value) => {
    el.value = value;
    el.dispatchEvent(new window.Event('change', { bubbles: true }));
  };
  const type = (el, value) => {
    el.value = value;
    el.dispatchEvent(new window.Event('input', { bubbles: true }));
  };
  const form = byId('hdcTxnForm');
  const rows = () => [...form.querySelectorAll('[data-material-item-row]')];
  const field = (row, name) => row.querySelector(`[data-material-detail="${name}"]`);
  const option = (select, label) => [...select.options].find(o => o.textContent.trim() === label).value;
  const category = byId('txnCategory');
  const materialCategory = option(category, 'Material & Purchase');
  const qtyError = row => row.querySelector('[data-material-error]');
  let submits = 0;
  let posted;
  form.submit = () => { submits++; posted = new window.FormData(form); };
  const submit = () => form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));

  assert.equal(byId('txnBody').hidden, true);
  change(byId('txnDirection'), 'out');
  change(category, materialCategory);
  assert.equal(byId('txnMaterialItemsField').hidden, false);
  assert.equal(byId('txnProjectField').hidden, false);
  assert.equal(byId('txnPartyField').hidden, false);
  assert.equal(byId('txnProjectField').closest('details'), null, 'project must not be inside collapsed More details');
  assert.equal(byId('txnPartyField').closest('details'), null, 'supplier must not be inside collapsed More details');
  assert.equal(byId('txnProjectReq').hidden, false);
  assert.equal(byId('txnPartyReq').hidden, false);
  assert.equal(byId('txnPartyLabelText').textContent, 'Supplier / vendor');
  assert.equal(byId('txnAmount').readOnly, true);
  assert.equal(byId('txnSubcategory').disabled, true);
  assert.equal(form.noValidate, true, 'inline validation owns focus, not hidden combo selects');

  change(byId('txnAccount'), [...byId('txnAccount').options].find(o => o.textContent.includes('Material cash')).value);
  submit();
  assert.equal(submits, 0);
  assert.match(form.querySelector('[data-error-for="project_id"]').textContent, /project/);
  assert.match(form.querySelector('[data-error-for="party_name"]').textContent, /supplier/);
  assert.match(form.querySelector('[data-error-for="subcategory_ids"]').textContent, /material name/);
  assert.equal(byId('txnSaveBtn').disabled, false, 'invalid form can be corrected immediately');

  change(byId('txnProject'), [...byId('txnProject').options].find(o => o.value).value);
  // The real combo supports newly created supplier options; no network needed.
  const supplier = doc.createElement('option');
  supplier.value = supplier.textContent = 'DOM Supplier';
  supplier.setAttribute('data-party-type', 'supplier');
  byId('txnParty').appendChild(supplier);
  change(byId('txnParty'), supplier.value);
  const first = rows()[0];
  const firstSelect = first.querySelector('[data-material-select]');
  change(firstSelect, option(firstSelect, 'Cement'));
  type(field(first, 'quantity'), '2.5');
  type(field(first, 'unit'), 'bags');
  type(field(first, 'unit_price'), '10.01');
  type(field(first, 'specification'), 'OPC <50 kg>');
  assert.equal(qtyError(first).hidden, true, 'corrected quantities clear stale errors');
  assert.equal(field(first, 'quantity').hasAttribute('aria-invalid'), false);
  assert.equal(form.querySelector('[data-error-for="project_id"]').hidden, true);
  assert.equal(form.querySelector('[data-error-for="party_name"]').hidden, true);
  assert.equal(form.querySelector('[data-error-for="account_id"]').hidden, true);
  assert.equal(byId('txnAmount').value, '25.03', 'half-paisa rounds up exactly');
  assert.equal(first.querySelector('[data-material-line-total]').textContent, '25.03');

  byId('txnAddMaterialItem').click();
  assert.equal(rows().length, 2);
  const second = rows()[1];
  for (const input of second.querySelectorAll('[data-material-detail]')) {
    assert.equal(input.value, '', 'new row must not copy the first row values');
    assert.equal(form.querySelectorAll('#' + input.id).length, 1, 'cloned ids are unique');
    assert.ok(second.querySelector('label[for="' + input.id + '"]'), 'every cloned control keeps its label');
  }
  const secondSelect = second.querySelector('[data-material-select]');
  change(secondSelect, option(secondSelect, 'Steel / Saria'));
  type(field(second, 'quantity'), '0.125');
  type(field(second, 'unit'), 'kg');
  type(field(second, 'unit_price'), '0.12');
  assert.equal(byId('txnAmount').value, '25.05');
  assert.equal(byId('txnMaterialTotal').textContent, 'Rs. 25.05');

  for (const bad of ['', '0', '-1', 'NaN', 'Infinity', '1.2345', '1000000001']) {
    type(field(second, 'quantity'), bad);
    submit();
    assert.equal(submits, 0, 'invalid quantity must not submit: ' + bad);
    assert.equal(qtyError(second).hidden, false);
    assert.equal(field(second, 'quantity').getAttribute('aria-invalid'), 'true');
    assert.equal(doc.activeElement, field(second, 'quantity'), 'focus the editable row, not readonly total');
  }
  type(field(second, 'quantity'), '0.125');
  for (const bad of ['', '0', '-2', 'NaN', 'abc', '1000000000001']) {
    type(field(second, 'unit_price'), bad);
    submit();
    assert.equal(submits, 0, 'invalid unit price must not submit: ' + bad);
  }
  type(field(second, 'unit_price'), '0.12');
  type(field(second, 'unit'), '');
  submit();
  assert.equal(submits, 0);
  assert.match(qtyError(second).textContent, /Enter a unit/);
  type(field(second, 'unit'), 'kg');

  // Every detail control is disabled outside material purchases; old values
  // cannot leak into a transfer or an unrelated expense.
  change(byId('txnDirection'), 'transfer');
  assert.equal(byId('txnMaterialItemsField').hidden, true);
  assert.equal(byId('txnAmount').readOnly, false);
  assert.deepEqual(new window.FormData(form).getAll('material_quantities'), []);
  assert.deepEqual(new window.FormData(form).getAll('subcategory_ids'), []);
  change(byId('txnDirection'), 'out');
  change(category, option(category, 'Fuel & Transport'));
  assert.equal(byId('txnMaterialItemsField').hidden, true);
  assert.deepEqual(new window.FormData(form).getAll('material_units'), []);
  change(category, materialCategory);
  // Category changes deliberately clear names that don't belong, but keep
  // quantities/rates available while the operator reselects the materials.
  change(firstSelect, option(firstSelect, 'Cement'));
  change(secondSelect, option(secondSelect, 'Steel / Saria'));
  assert.equal(byId('txnAmount').value, '25.05');
  assert.equal(field(first, 'specification').value, 'OPC <50 kg>');

  // Removing a row recalculates the total; reset clears every row field.
  second.querySelector('[data-remove-material-item]').click();
  assert.equal(rows().length, 1);
  assert.equal(byId('txnAmount').value, '25.03');
  type(field(first, 'quantity'), '3');
  type(field(first, 'unit_price'), '1.005');
  assert.equal(byId('txnAmount').value, '3.03', 'price is rounded before multiplying');
  submit();
  assert.equal(submits, 1);
  assert.deepEqual(posted.getAll('material_quantities'), ['3']);
  assert.deepEqual(posted.getAll('material_units'), ['bags']);
  assert.deepEqual(posted.getAll('material_unit_prices'), ['1.005']);
  assert.equal(posted.get('amount'), '3.03');
  assert.equal(byId('txnSaveBtn').disabled, true);
  submit();
  assert.equal(submits, 1, 'double submission stays locked');

  byId('txnResetBtn').click();
  assert.equal(rows().length, 1);
  assert.equal(byId('txnSaveBtn').disabled, false);
  for (const input of rows()[0].querySelectorAll('[data-material-detail]')) assert.equal(input.value, '');
  assert.equal(byId('txnMaterialItemsField').hidden, true);
  assert.equal(byId('txnBody').hidden, true);
  dom.window.close();
  console.log('material purchase DOM checks passed');
})().catch(error => { console.error(error); process.exit(1); });
