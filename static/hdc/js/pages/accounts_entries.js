// Accounts — All Entries page JS (filters, the entry form and the void /
// restore panel).  Extracted from templates/hdc/accounts/accounts_entries.html
// (audit 7.3).  Server data comes from the JSON config block the template
// renders just above the script tag.
var __hdcEntriesConfig = (function () {
    try {
        return JSON.parse(document.getElementById('accountsEntriesConfig').textContent) || {};
    } catch (e) { return {}; }
})();
var intentMatrix = __hdcEntriesConfig.intentMatrix;
var workerOptionRows = __hdcEntriesConfig.workerOptionRows;
var supplierOptionRows = __hdcEntriesConfig.supplierOptionRows;
var subcontractorOptionRows = __hdcEntriesConfig.subcontractorOptionRows;
var officeStaffOptionRows = __hdcEntriesConfig.officeStaffOptionRows;
var projectsData = __hdcEntriesConfig.projectsData;
var projectsNames = __hdcEntriesConfig.projectsNames;
var projectReceivableRows = __hdcEntriesConfig.projectReceivableRows;
var projectClientMeta = __hdcEntriesConfig.projectClientMeta;
var personalExpenseCategoryNames = __hdcEntriesConfig.personalExpenseCategoryNames;
var personalExpensePartyNames = __hdcEntriesConfig.personalExpensePartyNames;
var txnForm = document.getElementById('txn_form');
var txnDirectionWrap = document.getElementById('txn_direction_wrap');
var txnDirection = document.getElementById('txn_direction');
var txnType = document.getElementById('txn_type');
var amountInput = (txnForm ? txnForm.querySelector('input[name="amount"]') : null);
var refInput = (txnForm ? txnForm.querySelector('input[name="reference_id"]') : null);
var refWrap = document.getElementById('reference_wrap');
var noteInput = (txnForm ? txnForm.querySelector('textarea[name="note"]') : null);
var resetNotice = document.getElementById('txn_reset_notice');
var toWrap = document.getElementById('to_wrap');
var fromSel = document.getElementById('from_account');
var fromBalanceHint = document.getElementById('from_account_balance_hint');
var fromLabel = document.getElementById('from_account_label');
var toAccountSelect = document.getElementById('to_account');
var toLabel = document.getElementById('to_account_label');
var projectWrap = document.getElementById('project_wrap');
var stageWrap = document.getElementById('stage_wrap');
var projectSelect = document.getElementById('project_id');
var stageSelect = document.getElementById('stage_id');
var receiveSourceWrap = document.getElementById('receive_source_wrap');
var receiveSourceKind = document.getElementById('receive_source_kind');
var relatedWrap = document.getElementById('related_wrap');
var relatedTypeSelect = document.getElementById('related_entity_type');
var relatedIdSelect = document.getElementById('related_entity_id');
var relatedHelp = document.getElementById('related_help');
var officeTargetWrap = document.getElementById('office_target_wrap');
var officeTargetSelect = document.getElementById('office_target');
var officeExpenseCategoryWrap = document.getElementById('office_expense_category_wrap');
var officeExpenseCategoryInput = document.getElementById('office_expense_category');
var partyNameInput = document.querySelector('input[name="party_name"]');
var partyNameWrap = document.getElementById('party_name_wrap');
var pendingInfo = document.getElementById('pending_info');
var stageLoadSeq = 0;
var excessSplitWrap = document.getElementById('excess_split_wrap');
var settleShortfallWrap = document.getElementById('settle_shortfall_wrap');
var excessTipInput = document.getElementById('excess_tip_amount');
var excessAdvanceInput = document.getElementById('excess_advance_amount');
var excessSplitInfo = document.getElementById('excess_split_info');
var settleShortfallInput = document.getElementById('settle_shortfall');
var expenseCategoryWrap = document.getElementById('expense_category_wrap');
var expenseCategoryInput = document.getElementById('expense_category_id');
var latestPendingItem = null;

// Prevent mouse-wheel from changing numeric inputs while focused (common source of accidental amount edits).
document.addEventListener('wheel', function (e) {
    var active = document.activeElement;
    if (!active) return;
    if (
        active.tagName === 'INPUT' &&
        active.type === 'number' &&
        active.closest &&
        active.closest('#txn_form')
    ) {
        if (e.target === active) e.preventDefault();
        active.blur();
    }
}, { capture: true, passive: false });

// ── the field rules ────────────────────────────────────────────────────────
// Which fields this form shows is NOT decided here: every type's answer comes
// from the server (intentMatrix = hdc.services.accounts._account_intent_field_matrix,
// editable in Settings → Cash Flow).  Looking a rule up first by the type the
// user actually picked, then by the ledger type it normalises to, is what stops
// a "Pay to Project" asking for a Related Entity it can never have.
function intentRule(tx) {
    var raw = String(tx || '').toLowerCase();
    if (!raw) return {};
    if (intentMatrix && intentMatrix[raw]) return intentMatrix[raw];
    var normalized = normalizeTxnType(raw);
    if (intentMatrix && intentMatrix[normalized]) return intentMatrix[normalized];
    return {};
}
function ruleFlag(m, name, fallback) {
    if (!m || typeof m[name] === 'undefined' || m[name] === null) return !!fallback;
    return !!m[name];
}
function ruleText(m, name) {
    if (!m) return '';
    return String(m[name] || '').trim();
}
function normalizeTxnType(tx) {
    var t = String(tx || '').toLowerCase();
    if (t === 'receive_from_project') return 'project_income';
    if (t === 'receive_intra_company') return 'transfer';
    if (t === 'receive_from_credit_debit') return 'party_receipt';
    if (t === 'pay_to_project') return 'party_payment';
    if (t === 'pay_intra_company') return 'transfer';
    if (t === 'pay_to_credit_debit') return 'party_payment';
    return t;
}
var counterpartyGroupByTxn = {
    receive_from_project: 'project_in_flow',
    receive_intra_company: 'company',
    receive_from_credit_debit: 'credit_debit',
    pay_to_project: 'project_in_flow',
    pay_intra_company: 'company',
    pay_to_credit_debit: 'credit_debit',
    transfer: 'company',
    project_income: 'project_in_flow',
    party_receipt: 'credit_debit',
    client_payment: 'credit_debit',
    party_payment: 'credit_debit',
    personal_management_payment: 'credit_debit',
    purchase: 'credit_debit',
    expense_subcontractor: 'credit_debit',
    expense_general: 'credit_debit',
    expense_material: 'credit_debit',
    payroll: 'credit_debit',
    expense_wage: 'credit_debit',
    advance_to_person: 'credit_debit'
};
var treasuryAccountTypes = { company: true, cash: true, bank: true };
var fromBaseOptions = Array.prototype.slice.call((fromSel && fromSel.options) ? fromSel.options : []).map(function (o) { return o.cloneNode(true); });
var toBaseOptions = Array.prototype.slice.call((toAccountSelect && toAccountSelect.options) ? toAccountSelect.options : []).map(function (o) { return o.cloneNode(true); });
function money2(v) {
    var n = Number(v || 0) || 0;
    return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function labelWithBalance(baseLabel, balance) {
    return String(baseLabel || 'Account') + ' | Bal: ' + money2(balance);
}
function applyOptionBalanceMeta(opt, meta) {
    if (!opt || !meta) return;
    var bal = Number(meta.current_balance || 0) || 0;
    var baseLabel = String(opt.getAttribute('data-label') || '').trim();
    if (!baseLabel) {
        baseLabel = String(opt.textContent || '').split('| Bal:')[0].trim() || ('Account #' + (opt.value || ''));
        opt.setAttribute('data-label', baseLabel);
    }
    opt.setAttribute('data-balance', String(bal));
    opt.textContent = labelWithBalance(baseLabel, bal);
}
function applyBalanceMapToOptionCollection(optionCollection, balanceMap) {
    Array.prototype.slice.call(optionCollection || []).forEach(function (opt) {
        if (!opt || !opt.value) return;
        var meta = balanceMap[String(opt.value)];
        if (!meta) return;
        applyOptionBalanceMeta(opt, meta);
    });
}
function setSelectedBalanceHint(hintEl, selectEl, prefix) {
    if (!hintEl || !selectEl) return;
    var opt = selectEl.options[selectEl.selectedIndex];
    if (!opt || !opt.value) {
        hintEl.classList.add('d-none');
        hintEl.textContent = '';
        hintEl.classList.remove('text-success', 'text-danger', 'text-muted');
        return;
    }
    var label = String(opt.getAttribute('data-label') || opt.textContent || 'Selected account').split('| Bal:')[0].trim();
    var bal = Number(opt.getAttribute('data-balance') || 0) || 0;
    hintEl.classList.remove('d-none', 'text-success', 'text-danger', 'text-muted');
    hintEl.classList.add((bal < 0) ? 'text-danger' : ((bal > 0) ? 'text-success' : 'text-muted'));
    hintEl.textContent = (prefix || 'Selected Account') + ': ' + label + ' | Available: ' + money2(bal) + ' PKR';
}
function fromAccountHintPrefix() {
    var dir = txnDirection ? (txnDirection.value || '') : '';
    if (dir === 'receive') return 'Source Account';
    if (dir === 'pay') return 'Paying Account';
    return 'From Account';
}
var realtimeBalanceMap = {};
var realtimeBalanceFetchPromise = null;
var realtimeBalanceFetchedAt = 0;
function refreshAccountBalancesRealtime(force) {
    var now = Date.now();
    if (!force && (now - realtimeBalanceFetchedAt) < 8000 && Object.keys(realtimeBalanceMap).length) {
        applyBalanceMapToOptionCollection(fromBaseOptions, realtimeBalanceMap);
        applyBalanceMapToOptionCollection(toBaseOptions, realtimeBalanceMap);
        applyBalanceMapToOptionCollection((fromSel && fromSel.options), realtimeBalanceMap);
        applyBalanceMapToOptionCollection((toAccountSelect && toAccountSelect.options), realtimeBalanceMap);
        setSelectedBalanceHint(fromBalanceHint, fromSel, fromAccountHintPrefix());
        return Promise.resolve(realtimeBalanceMap);
    }
    if (realtimeBalanceFetchPromise) return realtimeBalanceFetchPromise;
    realtimeBalanceFetchPromise = fetch('/api/accounts/list_accounts_with_balances?include_inactive=1')
        .then(function (r) { return r.json(); })
        .then(function (j) {
            var map = {};
            var items = (j && j.items) ? j.items : [];
            (items || []).forEach(function (it) {
                map[String(it.id)] = it;
            });
            realtimeBalanceMap = map;
            realtimeBalanceFetchedAt = Date.now();
            applyBalanceMapToOptionCollection(fromBaseOptions, realtimeBalanceMap);
            applyBalanceMapToOptionCollection(toBaseOptions, realtimeBalanceMap);
            applyBalanceMapToOptionCollection((fromSel && fromSel.options), realtimeBalanceMap);
            applyBalanceMapToOptionCollection((toAccountSelect && toAccountSelect.options), realtimeBalanceMap);
            setSelectedBalanceHint(fromBalanceHint, fromSel, fromAccountHintPrefix());
            return realtimeBalanceMap;
        })
        .catch(function () { return realtimeBalanceMap; })
        .finally(function () {
            realtimeBalanceFetchPromise = null;
        });
    return realtimeBalanceFetchPromise;
}
function optionAccountType(opt) {
    if (!opt) return '';
    return String(opt.getAttribute('data-type') || '').trim().toLowerCase();
}
function optionAccountGroup(opt) {
    if (!opt) return '';
    return String(opt.getAttribute('data-group') || '').trim().toLowerCase();
}
function _isPersonalSuggestionValue(v) {
    var val = String(v || '');
    return (val.indexOf('pmcat::') === 0 || val.indexOf('pmparty::') === 0);
}
function _appendPersonalSuggestionOption(selectEl, value, text, keepValue) {
    if (!selectEl) return;
    var label = String(text || '').trim();
    var opt = document.createElement('option');
    opt.value = String(value || '');
    opt.textContent = label;
    if (keepValue && String(keepValue) === String(value)) opt.selected = true;
    selectEl.appendChild(opt);
}
function _syncPersonalSuggestionToParty() {
    if (!partyNameInput || !toAccountSelect) return;
    var selOpt = toAccountSelect.options[toAccountSelect.selectedIndex];
    if (!selOpt || !selOpt.value) {
        var val = partyNameInput.value || '';
        if (_isPersonalSuggestionValue(val)) {
            var parts = val.split('::', 2);
            if (parts.length === 2) {
                var kind = parts[0];
                var name = parts[1];
                if (kind === 'pmcat') {
                    partyNameInput.value = 'Personal Expense: ' + name;
                } else if (kind === 'pmparty') {
                    partyNameInput.value = name;
                }
            }
        }
    }
}
function _syncPartyToPersonalSuggestion() {
    if (!partyNameInput || !toAccountSelect) return;
    var selOpt = toAccountSelect.options[toAccountSelect.selectedIndex];
    if (selOpt && selOpt.value) {
        partyNameInput.value = '';
    }
}
function updateTxnUI() {
    if (!txnType) return;
    var rawTx = String(txnType.value || '').toLowerCase();
    var tx = normalizeTxnType(rawTx);
    var m = intentRule(rawTx);
    var hasType = !!(txnType.value || '');
    var dir = ruleText(m, 'direction') || ((ruleFlag(m, 'to_account', false)) ? 'receive' : 'pay');
    if (txnDirection) txnDirection.value = dir;
    applyReceiveSourceKindUI();
    if (txnDirectionWrap) txnDirectionWrap.classList.toggle('d-none', false);
    var isReceive = (dir === 'receive');
    var isTransfer = (tx === 'transfer');
    var isPayroll = (tx === 'payroll');
    var isExpenseWage = (tx === 'expense_wage');
    var isExpenseSubcontractor = (tx === 'expense_subcontractor');

    if (fromLabel) fromLabel.textContent = isReceive ? 'From Account' : 'Paying Account';
    if (toLabel) toLabel.textContent = isReceive ? 'To Account' : 'Receiving Account';

    /* ── every field below is shown because its rule says so ───────────── */
    var showFrom = ruleFlag(m, 'from_account', true);
    var showTo = ruleFlag(m, 'to_account', true);
    var fromWrap = document.getElementById('from_wrap');
    if (fromWrap) fromWrap.classList.toggle('d-none', !showFrom);
    if (fromSel) fromSel.required = showFrom;
    if (toWrap) toWrap.classList.toggle('d-none', !hasType || !showTo);

    var isWorkerProjectStageScope = (isPayroll || isExpenseWage || isExpenseSubcontractor);
    var workerProjectStageActive = true;
    if (isWorkerProjectStageScope) {
        var tipAmount = Number((excessTipInput ? excessTipInput.value : 0) || 0) || 0;
        var settleActive = !!(settleShortfallInput && settleShortfallInput.checked);
        workerProjectStageActive = (tipAmount > 0.000001 || settleActive);
    }
    var showProjectScope = (ruleFlag(m, 'project', false) && (!isWorkerProjectStageScope || workerProjectStageActive));
    var showStageScope = (ruleFlag(m, 'stage', false) && (!isWorkerProjectStageScope || workerProjectStageActive));
    var hasProject = !!(projectSelect && projectSelect.value);
    if (projectWrap) projectWrap.classList.toggle('d-none', (!hasType || !showProjectScope));
    if (stageWrap) stageWrap.classList.toggle('d-none', (!hasType || !showStageScope || !hasProject));
    if (relatedWrap) relatedWrap.classList.toggle('d-none', !hasType || !ruleFlag(m, 'related', false));
    if (receiveSourceWrap) receiveSourceWrap.classList.toggle('d-none', !isReceive);
    if (officeTargetWrap) officeTargetWrap.classList.toggle('d-none', !hasType || !ruleFlag(m, 'office_target', false));
    if (officeExpenseCategoryWrap) officeExpenseCategoryWrap.classList.toggle('d-none', !hasType || !ruleFlag(m, 'office_target', false));
    if (partyNameWrap) partyNameWrap.classList.toggle('d-none', !hasType || !ruleFlag(m, 'party_name', false));
    if (refWrap) refWrap.classList.toggle('d-none', !hasType || !ruleFlag(m, 'reference', true));
    if (excessSplitWrap) excessSplitWrap.classList.toggle('d-none', !isPayroll);
    if (settleShortfallWrap) settleShortfallWrap.classList.toggle('d-none', !isPayroll);
    if (expenseCategoryWrap) expenseCategoryWrap.classList.toggle('d-none', !hasType || !ruleFlag(m, 'expense_category', false));
    if (amountInput) amountInput.min = '0.01';

    if (toAccountSelect) {
        toAccountSelect.required = ruleFlag(m, 'to_account_required', false);
        toAccountSelect.classList.toggle('d-none', false);
    }
    if (partyNameInput) {
        /* Required when the rule says so, and — for the types that use the
           off-ledger party as their payee — when no To Account was picked. */
        var partyRequired = ruleFlag(m, 'party_name_required', false);
        var partyOrTo = partyRequired && !(toAccountSelect && toAccountSelect.value);
        partyNameInput.required = (partyOrTo || (partyRequired && !ruleFlag(m, 'to_account', true)));
    }
    if (projectSelect) projectSelect.required = showProjectScope && ruleFlag(m, 'project_required', false);
    if (stageSelect) stageSelect.required = showStageScope && ruleFlag(m, 'stage_required', false);
    if (expenseCategoryInput) expenseCategoryInput.required = ruleFlag(m, 'expense_category', false);

    var relType = ruleFlag(m, 'related', false) ? ruleText(m, 'related_type') : '';
    if (relatedTypeSelect && relatedIdSelect) {
        relatedTypeSelect.value = relType;
        relatedIdSelect.innerHTML = '<option value="">Select entity</option>';
        if (relType) {
            var options = [];
            if (relType === 'worker') options = workerOptionRows || [];
            else if (relType === 'supplier') options = supplierOptionRows || [];
            else if (relType === 'subcontractor') options = subcontractorOptionRows || [];
            else if (relType === 'office_staff') options = officeStaffOptionRows || [];
            options.forEach(function (r) {
                var o = document.createElement('option');
                o.value = String(r.id || '');
                o.textContent = r.label || ('#' + r.id);
                relatedIdSelect.appendChild(o);
            });
        }
    }
    if (relatedIdSelect) relatedIdSelect.required = !!relType;
    if (relatedHelp) {
        relatedHelp.classList.toggle('d-none', !relType);
        if (relType) {
            relatedHelp.textContent = 'Select the ' + String(relType || 'entity').replace('_', ' ') + ' for this transaction.';
        }
    }
    if (pendingInfo) pendingInfo.classList.add('d-none');
    if (resetNotice) resetNotice.classList.add('d-none');
    refreshAccountBalancesRealtime(true);
}
function applyReceiveSourceKindUI() {
    var dir = txnDirection ? (txnDirection.value || '') : '';
    if (receiveSourceWrap) receiveSourceWrap.classList.toggle('d-none', !dir);
    if (receiveSourceKind) {
        receiveSourceKind.required = !!dir;
        if (!dir) {
            receiveSourceKind.value = '';
        } else if (!receiveSourceKind.value) {
            receiveSourceKind.value = (dir === 'receive' ? 'project_in_flow' : 'credit_debit');
        }
    }
}
function updateBankInfo(sel) {
    var bankInfo = document.getElementById('bank_info');
    if (!bankInfo) return;
    var opt = sel.options[sel.selectedIndex];
    if (!opt || !opt.value) {
        bankInfo.classList.add('d-none');
        return;
    }
    var bank = String(opt.getAttribute('data-bank') || '').trim();
    var ac = String(opt.getAttribute('data-acno') || '').trim();
    var iban = String(opt.getAttribute('data-iban') || '').trim();
    if (bank || ac || iban) {
        bankInfo.classList.remove('d-none');
        bankInfo.textContent = 'Bank: ' + (bank || '-') + ' | A/C: ' + (ac || '-') + (iban ? (' | IBAN: ' + iban) : '');
    } else {
        bankInfo.classList.add('d-none');
        bankInfo.textContent = '';
    }
}
if (txnType) txnType.addEventListener('change', updateTxnUI);
if (fromSel) fromSel.addEventListener('change', function () { updateBankInfo(fromSel); });
if (toAccountSelect) {
    toAccountSelect.addEventListener('change', function () {
        _syncPersonalSuggestionToParty();
        updateBankInfo(toAccountSelect);
        updateTxnUI();
    });
    toAccountSelect.addEventListener('focus', function () { refreshAccountBalancesRealtime(true); });
    toAccountSelect.addEventListener('mousedown', function () { refreshAccountBalancesRealtime(true); });
}
updateTxnUI();
updateBankInfo(fromSel);
setSelectedBalanceHint(fromBalanceHint, fromSel, fromAccountHintPrefix());
// Edit-entry panel — only runs on the edit render, where the server fills in
// the config block's "editTxn" (audit 7.3: this was an inline script with six
// Jinja values interpolated straight into JavaScript source).
(function () {
    var edit = __hdcEntriesConfig.editTxn;
    if (!edit) return;

    function asValue(value) {
        return (value === null || value === undefined) ? '' : String(value);
    }

    document.addEventListener('DOMContentLoaded', function () {
        var host = document.getElementById('txn_form_host');
        if (host) host.classList.remove('d-none');

        function setAndNotify(id, value) {
            var el = document.getElementById(id);
            if (!el) return;
            el.value = asValue(value);
            el.dispatchEvent(new Event('change'));
        }

        setAndNotify('txn_type', edit.type);
        setAndNotify('from_account', edit.fromAccountId);
        setAndNotify('to_account', edit.toAccountId);
        setAndNotify('project_id', edit.projectId);

        // Stage and related-entity values may need a tick after the project
        // change has rebuilt those selects.
        setTimeout(function () {
            var stageEl = document.getElementById('stage_id');
            if (stageEl) stageEl.value = asValue(edit.stageId);
            var relTypeEl = document.getElementById('related_entity_type');
            if (relTypeEl) relTypeEl.value = asValue(edit.relatedType);
            var relIdEl = document.getElementById('related_entity_id');
            if (relIdEl) relIdEl.value = asValue(edit.relatedId);
        }, 80);

        // Scroll the edit form into view so the user actually sees it.
        setTimeout(function () {
            if (host && typeof host.scrollIntoView === 'function') {
                host.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }, 120);
    });
})();
// Combo-list wiring for the worker selector (was a small inline script).
(function () {
    if (window.HDCComboList) {
        window.HDCComboList.attach('entriesWorkerInput', 'entriesWorkerSelect', { includeEmpty: true });
    }
})();
