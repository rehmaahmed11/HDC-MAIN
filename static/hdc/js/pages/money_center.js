// Money Center JS — smooth handling.
// Extracted from templates/hdc/accounts/money_center.html (audit 7.3).
// Server data arrives via the <script id="moneyCenterConfig" type="application/json">
// block rendered just above this file.
(function () {
  'use strict';
  var __cfgEl = document.getElementById('moneyCenterConfig');
  var __cfg = __cfgEl ? JSON.parse(__cfgEl.textContent) : {};
  var entryConfig = __cfg.entryConfig || {};
  var intentMatrix = __cfg.intentMatrix || {};
// ── Money Center JS — smooth handling ──


let currentDirection = '';
let currentType = '';

const typeConfigByIntent = {
  'receive_from_project': { label: 'Receive from Project', icon: 'fa-building', color: '#10b981', bg: '#ecfdf5', direction: 'receive', tx_type: 'project_income', related: 'project', pending: 'project_receivable' },
  'receive_from_credit_debit': { label: 'Receive from Party', icon: 'fa-hand-holding-dollar', color: '#06b6d4', bg: '#cffafe', direction: 'receive', tx_type: 'party_receipt', related: '', pending: '' },
  'receive_intra_company': { label: 'Transfer from Intra-Company', icon: 'fa-right-left', color: '#3b82f6', bg: '#eff6ff', direction: 'receive', tx_type: 'transfer', related: '', pending: '' },
  'payroll': { label: 'Wage / Payroll Payment', icon: 'fa-helmet-safety', color: '#f43f5e', bg: '#fff1f2', direction: 'pay', tx_type: 'payroll', related: 'worker', pending: 'worker_payable' },
  'purchase': { label: 'Supplier / Material Payment', icon: 'fa-truck-field', color: '#f43f5e', bg: '#fff1f2', direction: 'pay', tx_type: 'purchase', related: 'supplier', pending: 'supplier_payable' },
  'expense_subcontractor': { label: 'Subcontractor Payment', icon: 'fa-people-group', color: '#f43f5e', bg: '#fff1f2', direction: 'pay', tx_type: 'expense_subcontractor', related: 'subcontractor', pending: 'subcontract_payable' },
  'office_management_payment': { label: 'Office Management Payment', icon: 'fa-user-tie', color: '#64748b', bg: '#f1f5f9', direction: 'pay', tx_type: 'office_management_payment', related: 'office_staff', pending: 'office_staff_payable' },
  'personal_management_payment': { label: 'Personal / Party Payment', icon: 'fa-user', color: '#64748b', bg: '#f1f5f9', direction: 'pay', tx_type: 'personal_management_payment', related: '', pending: '' },
  'expense_general': { label: 'General Expense', icon: 'fa-receipt', color: '#64748b', bg: '#f1f5f9', direction: 'pay', tx_type: 'expense_general', related: '', pending: '' },
  'pay_intra_company': { label: 'Transfer to Intra-Company', icon: 'fa-right-left', color: '#3b82f6', bg: '#eff6ff', direction: 'pay', tx_type: 'transfer', related: '', pending: '' },
  'pay_to_credit_debit': { label: 'Pay to Party', icon: 'fa-arrow-up', color: '#64748b', bg: '#f1f5f9', direction: 'pay', tx_type: 'party_payment', related: '', pending: '' },
};

let workerOptions = [], supplierOptions = [], subcontractorOptions = [], officeStaffOptions = [], expenseCategories = [], officeExpCats = [];
let pendingCache = {};

function mcSelectDirection(dir) {
  currentDirection = dir;
  document.querySelectorAll('.dir-btn').forEach(b => b.classList.remove('active'));
  const btn = document.querySelector(`.dir-btn[data-direction="${dir}"]`);
  if (btn) btn.classList.add('active');

  // Update step indicator
  document.getElementById('step1-dot').className = 'step-dot done';
  document.getElementById('step1-dot').innerHTML = '<i class="fas fa-check"></i>';
  document.getElementById('step1-label').className = 'step-label';
  document.getElementById('step1-2-line').className = 'step-line done';
  document.getElementById('step2-dot').className = 'step-dot active';
  document.getElementById('step2-label').className = 'step-label active';

  // Build type grid
  const grid = document.getElementById('mc-type-grid');
  grid.innerHTML = '';
  Object.entries(typeConfigByIntent).forEach(([intent, cfg]) => {
    if (cfg.direction !== dir) return;
    // For transfer, both pay and receive transfer intents
    if (dir === 'transfer' && cfg.direction !== 'pay' && cfg.direction !== 'receive') return;
    if (dir === 'transfer' && !intent.includes('intra_company')) return;
    // Filter logic
    if (dir === 'receive' && !intent.startsWith('receive_')) return;
    if (dir === 'pay' && intent.startsWith('receive_')) return;
    if (dir === 'transfer' && !intent.includes('intra_company')) return;

    const div = document.createElement('div');
    div.className = 'type-btn';
    div.style.setProperty('--flow-color', cfg.color);
    div.style.setProperty('--flow-bg', cfg.bg);
    div.dataset.intent = intent;
    div.innerHTML = `<i class="fas ${cfg.icon}" style="color:${cfg.color};"></i><div><div class="t-title">${cfg.label}</div><div class="t-desc">${cfg.tx_type} · ${cfg.pending || 'no pending'} · ${intent}</div></div>`;
    div.onclick = () => mcSelectType(intent);
    grid.appendChild(div);
  });

  // Special: for transfer, show both receive and pay intra company
  if (dir === 'transfer') {
    ['receive_intra_company', 'pay_intra_company'].forEach(intent => {
      if (grid.querySelector(`[data-intent="${intent}"]`)) return;
      const cfg = typeConfigByIntent[intent];
      if (!cfg) return;
      const div = document.createElement('div');
      div.className = 'type-btn';
      div.style.setProperty('--flow-color', cfg.color);
      div.style.setProperty('--flow-bg', cfg.bg);
      div.dataset.intent = intent;
      div.innerHTML = `<i class="fas ${cfg.icon}" style="color:${cfg.color};"></i><div><div class="t-title">${cfg.label}</div><div class="t-desc">${cfg.tx_type} · internal treasury</div></div>`;
      div.onclick = () => mcSelectType(intent);
      grid.appendChild(div);
    });
  }

  document.getElementById('mc-step1').classList.add('d-none');
  document.getElementById('mc-step2').classList.remove('d-none');
  document.getElementById('mc-step3').classList.add('d-none');
}

function mcBackToStep(step) {
  if (step === 1) {
    document.getElementById('mc-step1').classList.remove('d-none');
    document.getElementById('mc-step2').classList.add('d-none');
    document.getElementById('mc-step3').classList.add('d-none');
    document.getElementById('step1-dot').className = 'step-dot active';
    document.getElementById('step1-dot').textContent = '1';
    document.getElementById('step1-label').className = 'step-label active';
    document.getElementById('step1-2-line').className = 'step-line';
    document.getElementById('step2-dot').className = 'step-dot';
    document.getElementById('step2-label').className = 'step-label';
    document.getElementById('step2-3-line').className = 'step-line';
    document.getElementById('step3-dot').className = 'step-dot';
    document.getElementById('step3-label').className = 'step-label';
  } else if (step === 2) {
    document.getElementById('mc-step1').classList.add('d-none');
    document.getElementById('mc-step2').classList.remove('d-none');
    document.getElementById('mc-step3').classList.add('d-none');
    document.getElementById('step2-dot').className = 'step-dot active';
    document.getElementById('step2-label').className = 'step-label active';
    document.getElementById('step2-3-line').className = 'step-line';
    document.getElementById('step3-dot').className = 'step-dot';
    document.getElementById('step3-label').className = 'step-label';
  }
}

function mcSelectType(intent) {
  currentType = intent;
  document.querySelectorAll('.type-btn').forEach(b => b.classList.remove('active'));
  const btn = document.querySelector(`.type-btn[data-intent="${intent}"]`);
  if (btn) btn.classList.add('active');

  const cfg = typeConfigByIntent[intent] || { label: intent, icon: 'fa-money-bill', color: '#64748b', bg: '#f1f5f9', direction: currentDirection, tx_type: intent, related: '', pending: '' };
  document.getElementById('mc_type').value = cfg.tx_type;
  document.getElementById('mc_related_type').value = cfg.related || '';

  document.getElementById('step2-dot').className = 'step-dot done';
  document.getElementById('step2-dot').innerHTML = '<i class="fas fa-check"></i>';
  document.getElementById('step2-label').className = 'step-label';
  document.getElementById('step2-3-line').className = 'step-line done';
  document.getElementById('step3-dot').className = 'step-dot active';
  document.getElementById('step3-label').className = 'step-label active';

  // Show selected type info
  const info = document.getElementById('mc-selected-type-info');
  info.innerHTML = `<i class="fas ${cfg.icon} me-1" style="color:${cfg.color};"></i><strong>${cfg.label}</strong> — Tx: <code>${cfg.tx_type}</code> · Direction: ${cfg.direction} · Related: ${cfg.related || 'none'} · Pending: ${cfg.pending || 'none'}<br><small class="text-muted">Intent: ${intent} · Posting: ${entryConfig[intent] ? entryConfig[intent].tips?.[0] || '' : ''}</small>`;

  // Update labels
  const fromLabel = document.getElementById('mc_from_label');
  const toLabel = document.getElementById('mc_to_label');
  const relatedLabel = document.getElementById('mc_related_label');
  if (cfg.direction === 'receive') {
    fromLabel.textContent = 'From Account (Party / Source) *';
    toLabel.textContent = 'Receive In (Company Account) *';
  } else if (cfg.tx_type === 'transfer') {
    fromLabel.textContent = 'From Account (Company) *';
    toLabel.textContent = 'To Account (Company) *';
  } else {
    fromLabel.textContent = 'Pay From (Company Account) *';
    toLabel.textContent = 'To Account (optional)';
  }

  // Show/hide sections based on type
  const isWorker = cfg.related === 'worker';
  const isSupplier = cfg.related === 'supplier';
  const isSub = cfg.related === 'subcontractor';
  const isOfficeStaff = cfg.related === 'office_staff';
  const isOffice = cfg.tx_type === 'office_management_payment';
  const isExpenseGeneral = cfg.tx_type === 'expense_general';
  const isTransfer = cfg.tx_type === 'transfer';

  document.getElementById('mc_related_wrap').classList.toggle('d-none', !cfg.related);
  if (cfg.related) {
    relatedLabel.textContent = `Related ${cfg.related.charAt(0).toUpperCase()+cfg.related.slice(1)} *`;
    document.getElementById('mc_related_help').textContent = `Select ${cfg.related} for pending calculation and auto party name`;
    loadRelatedOptions(cfg.related);
  }

  document.getElementById('mc_office_target_wrap').classList.toggle('d-none', !isOffice);
  document.getElementById('mc_office_cat_wrap').classList.toggle('d-none', !(isOffice && document.getElementById('mc_office_target').value === 'expense'));
  document.getElementById('mc_exp_cat_wrap').classList.toggle('d-none', !isExpenseGeneral);
  document.getElementById('mc_project_wrap').classList.toggle('d-none', isTransfer && !isExpenseGeneral);
  document.getElementById('mc_to_wrap').classList.toggle('d-none', false);

  // Show excess split for payable types
  const showExcess = ['payroll','purchase','expense_subcontractor','office_management_payment'].includes(cfg.tx_type);
  document.getElementById('mc_excess_wrap').classList.toggle('d-none', !showExcess);
  document.getElementById('mc_settle_wrap').classList.toggle('d-none', !(['payroll','expense_subcontractor'].includes(cfg.tx_type)));

  // Project required for some
  const projectRequired = ['project_income','expense_general'].includes(cfg.tx_type);
  document.getElementById('mc_project').required = projectRequired;

  // Filter from/to accounts
  filterAccountsByType(cfg);

  document.getElementById('mc-step1').classList.add('d-none');
  document.getElementById('mc-step2').classList.add('d-none');
  document.getElementById('mc-step3').classList.remove('d-none');

  // Load expense categories if needed
  if (isExpenseGeneral) loadExpenseCategories();
  if (isOffice) loadOfficeCategories();
}

function filterAccountsByType(cfg) {
  const fromSel = document.getElementById('mc_from_account');
  const toSel = document.getElementById('mc_to_account');
  // For now keep all, but could filter by company type
  // Company accounts should be from for OUT, to for IN
  // This is smooth - show all but hint
}

function loadRelatedOptions(type) {
  const sel = document.getElementById('mc_related_id');
  sel.innerHTML = '<option value="">Select</option>';
  let opts = [];
  if (type === 'worker') opts = workerOptions;
  else if (type === 'supplier') opts = supplierOptions;
  else if (type === 'subcontractor') opts = subcontractorOptions;
  else if (type === 'office_staff') opts = officeStaffOptions;

  opts.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.id;
    opt.textContent = o.label || o.name || `#${o.id}`;
    sel.appendChild(opt);
  });
}

function mcQuickSelectType(intent) {
  const cfg = typeConfigByIntent[intent];
  if (!cfg) return;
  mcSelectDirection(cfg.direction);
  setTimeout(() => mcSelectType(intent), 100);
  document.querySelector('.quick-entry-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function mcPayEntity(entityType, id, name, pending) {
  let intent = 'payroll';
  if (entityType === 'supplier') intent = 'purchase';
  else if (entityType === 'subcontractor') intent = 'expense_subcontractor';
  else if (entityType === 'office_staff') intent = 'office_management_payment';
  mcQuickSelectType(intent);
  setTimeout(() => {
    document.getElementById('mc_related_id').value = id;
    document.getElementById('mc_amount').value = pending.toFixed(2);
    document.getElementById('mc_party_name').value = name;
    // Value was set straight on the select; mirror it into the combo input.
    document.dispatchEvent(new Event('hdc:sync-combos'));
    // Trigger pending fetch
    fetchPending();
  }, 300);
}

function mcReceiveProject(projectId, projectName, pending) {
  mcQuickSelectType('receive_from_project');
  setTimeout(() => {
    document.getElementById('mc_project').value = projectId;
    document.getElementById('mc_amount').value = pending.toFixed(2);
    document.getElementById('mc_party_name').value = projectName;
    // Value was set straight on the select; mirror it into the combo input.
    document.dispatchEvent(new Event('hdc:sync-combos'));
    fetchPending();
  }, 300);
}

// ── Fetch helpers ──
function fetchPending() {
  const type = document.getElementById('mc_type').value;
  const relatedType = document.getElementById('mc_related_type').value;
  const relatedId = document.getElementById('mc_related_id').value;
  const projectId = document.getElementById('mc_project').value;
  const stageId = document.getElementById('mc_stage').value;
  if (!type) return;

  const qs = new URLSearchParams({ type, related_entity_type: relatedType, related_entity_id: relatedId, project_id: projectId, stage_id: stageId });
  fetch(`/api/accounts/pending_context?${qs}`)
    .then(r => r.json())
    .then(j => {
      const item = j.item;
      if (!item || (!item.pending && !item.total)) {
        document.getElementById('mc_pending_wrap').classList.add('d-none');
        return;
      }
      document.getElementById('mc_pending_wrap').classList.remove('d-none');
      document.getElementById('mc_pending_label').textContent = item.kind ? item.kind.replace(/_/g,' ').toUpperCase() : 'Pending';
      document.getElementById('mc_pending_value').textContent = `Rs. ${Number(item.pending||0).toLocaleString()} pending`;
      document.getElementById('mc_pending_breakdown').textContent = `Total: ${Number(item.total||0).toLocaleString()} | Paid: ${Number(item.paid||0).toLocaleString()} | ${item.message||''}`;
      pendingCache = item;
      updateExcessInfo();
    });
}

function updateExcessInfo() {
  const amt = parseFloat(document.getElementById('mc_amount').value || '0') || 0;
  const pending = pendingCache.pending || 0;
  const excess = Math.max(0, amt - pending);
  const el = document.getElementById('mc_excess_info');
  if (!el) return;
  if (excess > 0.01) {
    const tip = parseFloat(document.getElementById('mc_tip').value || '0') || 0;
    const adv = parseFloat(document.getElementById('mc_advance').value || '0') || 0;
    el.innerHTML = `<span class="text-warning">Excess: Rs. ${excess.toLocaleString()} = Tip ${tip.toLocaleString()} + Advance ${adv.toLocaleString()}</span>`;
    document.getElementById('mc_excess_wrap').classList.remove('d-none');
  } else {
    el.textContent = amt > 0 ? `Amount within pending (${pending.toLocaleString()}), no excess.` : '';
  }
}

// ── Account balance hints ──
function updateBalanceHint(selectId, hintId) {
  const sel = document.getElementById(selectId);
  const hint = document.getElementById(hintId);
  if (!sel || !hint) return;
  const opt = sel.options[sel.selectedIndex];
  if (!opt || !opt.value) { hint.classList.add('d-none'); return; }
  const bal = parseFloat(opt.dataset.balance || '0') || 0;
  hint.classList.remove('d-none', 'pos', 'neg', 'warn');
  if (bal < -0.01) {
    hint.className = 'balance-hint neg';
    hint.textContent = `⚠️ Overdrawn: Rs. ${bal.toLocaleString()} — further outflows blocked`;
  } else if (bal < 1000) {
    hint.className = 'balance-hint warn';
    hint.textContent = `Low balance: Rs. ${bal.toLocaleString()}`;
  } else {
    hint.className = 'balance-hint pos';
    hint.textContent = `Available: Rs. ${bal.toLocaleString()}`;
  }
}

// ── Event listeners ──
document.getElementById('mc_from_account')?.addEventListener('change', () => { updateBalanceHint('mc_from_account','mc_from_balance'); });
document.getElementById('mc_amount')?.addEventListener('input', () => { updateExcessInfo(); });
document.getElementById('mc_tip')?.addEventListener('input', () => { updateExcessInfo(); });
document.getElementById('mc_related_id')?.addEventListener('change', () => { fetchPending(); });
document.getElementById('mc_project')?.addEventListener('change', () => {
  const pid = document.getElementById('mc_project').value;
  if (!pid) { document.getElementById('mc_stage_wrap').classList.add('d-none'); fetchPending(); return; }
  fetch(`/hdc/api/project_stages/${pid}`).then(r=>r.json()).then(items=>{
    const stageSel = document.getElementById('mc_stage');
    stageSel.innerHTML = '<option value="">Select stage</option>';
    (items||[]).forEach(st=>{
      const opt = document.createElement('option');
      opt.value = st.id;
      opt.textContent = st.name;
      stageSel.appendChild(opt);
    });
    document.getElementById('mc_stage_wrap').classList.remove('d-none');
    fetchPending();
  });
});
document.getElementById('mc_stage')?.addEventListener('change', () => fetchPending());
document.getElementById('mc_office_target')?.addEventListener('change', (e)=>{
  const isExp = e.target.value === 'expense';
  document.getElementById('mc_office_cat_wrap').classList.toggle('d-none', !isExp);
  document.getElementById('mc_related_wrap').classList.toggle('d-none', isExp);
});

// ── Load options from APIs ──
function loadWorkerOptions() {
  return fetch('/hdc/api/workers').then(r=>r.json()).then(j=>{
    workerOptions = j.items || j || [];
    if (!Array.isArray(workerOptions)) workerOptions = [];
  }).catch(()=>{});
}
function loadSupplierOptions() {
  return fetch('/hdc/api/suppliers').then(r=>r.json()).then(j=>{
    supplierOptions = j.items || j || [];
    if (!Array.isArray(supplierOptions)) supplierOptions = [];
  }).catch(()=>{});
}
function loadSubcontractorOptions() {
  return fetch('/hdc/api/subcontractors').then(r=>r.json()).then(j=>{
    subcontractorOptions = j.items || j || [];
    if (!Array.isArray(subcontractorOptions)) subcontractorOptions = [];
  }).catch(()=>{});
}
function loadOfficeStaffOptions() {
  return fetch('/hdc/api/office_staff').then(r=>r.json()).then(j=>{
    officeStaffOptions = j.items || j || [];
    if (!Array.isArray(officeStaffOptions)) officeStaffOptions = [];
  }).catch(()=>{});
}
function loadExpenseCategories() {
  fetch('/hdc/api/expense_categories').then(r=>r.json()).then(j=>{
    const cats = j.items || j.categories || j || [];
    const sel = document.getElementById('mc_exp_cat');
    if (!sel) return;
    sel.innerHTML = '<option value="">Select category</option>';
    (Array.isArray(cats)?cats:[]).forEach(c=>{
      const opt = document.createElement('option');
      opt.value = c.id || c.value;
      opt.textContent = c.name || c.label || c;
      sel.appendChild(opt);
    });
  }).catch(()=>{});
}
function loadOfficeCategories() {
  fetch('/hdc/api/office_expense_categories').then(r=>r.json()).then(j=>{
    const cats = j.categories || [];
    const dl = document.getElementById('mcOfficeCatList');
    if (!dl) return;
    dl.innerHTML = '';
    cats.forEach(c=>{
      const opt = document.createElement('option');
      opt.value = c;
      dl.appendChild(opt);
    });
  }).catch(()=>{});
}

// Load every party feed once, including office staff.
async function loadAllOptions() {
  await Promise.all([
    loadWorkerOptions(),
    loadSupplierOptions(),
    loadSubcontractorOptions(),
    loadOfficeStaffOptions(),
  ]);

  // The user may choose a type before its feed finishes loading.
  const relatedType = document.getElementById('mc_related_type').value;
  if (relatedType) {
    const sel = document.getElementById('mc_related_id');
    const selected = sel.value;
    loadRelatedOptions(relatedType);
    sel.value = selected;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadAllOptions();
  loadOfficeCategories();
});
})();

// ── Searchable combo boxes (HDCComboList) ───────────────────────────────────
// Every party / person / account / project / stage / category pick in the
// quick-entry form becomes a type-to-search combo box.  The <select> keeps
// its name= so the posted payload and the server contract are unchanged; the
// input carries no name and is never submitted.  Strict mode means a value
// can only be set by picking it from the filtered list.
(function () {
  function attachCombo(inputId, selectId, comboOpts) {
    if (!window.HDCComboList) return null;
    var input = document.getElementById(inputId);
    var select = document.getElementById(selectId);
    if (!input || !select) return null;
    return window.HDCComboList.attach(input, select, comboOpts);
  }
  var mcCombos = {
    fromAccount: attachCombo('mc_from_account_input', 'mc_from_account', { strict: true, maxItems: 50 }),
    toAccount: attachCombo('mc_to_account_input', 'mc_to_account', { strict: true, maxItems: 50, includeEmpty: true }),
    project: attachCombo('mc_project_input', 'mc_project', { strict: true, includeEmpty: true }),
    stage: attachCombo('mc_stage_input', 'mc_stage', { strict: true, includeEmpty: true }),
    relatedEntity: attachCombo('mc_related_id_input', 'mc_related_id', { strict: true, maxItems: 50 }),
    expenseCategory: attachCombo('mc_exp_cat_input', 'mc_exp_cat', { strict: true, includeEmpty: true })
  };
  function syncMcCombos() {
    Object.keys(mcCombos).forEach(function (key) {
      if (mcCombos[key] && mcCombos[key].syncFromSelect) mcCombos[key].syncFromSelect();
    });
  }
  // Quick-pay / quick-receive buttons set the selects programmatically; they
  // dispatch this event so the combo inputs mirror the chosen values.
  document.addEventListener('hdc:sync-combos', syncMcCombos);
})();
