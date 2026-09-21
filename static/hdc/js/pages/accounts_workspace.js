// Accounts workspace JS — KPI matrix, the single entry form and the
// receipt modals.  Extracted from templates/hdc/accounts/accounts.html
// (audit 7.3) so the browser can cache it and the template stays small.
(function () {
    // Server data for this page, rendered as JSON just above the script tag.
    var __hdcCfg = (function () {
        try {
            return JSON.parse(document.getElementById('accountsWorkspaceConfig').textContent) || {};
        } catch (e) { return {}; }
    })();

    var accountGroup = document.getElementById('account_group');
    var accountMode = document.getElementById('account_mode');
    var accountNameLabel = document.getElementById('account_name_label');
    var accountNameInput = document.getElementById('account_name_input');
    var bankFields = document.getElementById('bank_fields');
    var bankName = document.getElementById('bank_name');
    var bankNo = document.getElementById('account_number');
    function updateAccountTypeUI() {
        var grp = accountGroup ? (accountGroup.value || 'company') : 'company';
        var mode = accountMode ? (accountMode.value || 'cash') : 'cash';
        var isCompany = (grp === 'company');
        var isBank = isCompany && mode === 'bank';
        if (!bankFields) return;
        bankFields.classList.toggle('d-none', !isBank);
        if (bankName) bankName.required = isBank;
        if (bankNo) bankNo.required = isBank;
        if (accountMode) {
            var modeWrap = accountMode.closest('.mb-2');
            if (modeWrap) modeWrap.classList.toggle('d-none', !isCompany);
            accountMode.required = isCompany;
            if (!isCompany) accountMode.value = 'cash';
        }
        if (accountNameLabel) {
            if (grp === 'project_in_flow') accountNameLabel.textContent = 'Client / Inflow Name';
            else if (grp === 'credit_debit') accountNameLabel.textContent = 'Liability/Party Name';
            else accountNameLabel.textContent = 'Account Name';
        }
        if (accountNameInput) {
            if (grp === 'project_in_flow') {
                accountNameInput.placeholder = 'e.g. Client / Owner Account';
            } else if (grp === 'credit_debit') {
                accountNameInput.placeholder = 'e.g. Supplier / Worker / Payable Control';
            } else {
                accountNameInput.placeholder = 'e.g. Company Cash Drawer 1';
            }
        }
    }
    if (accountGroup) accountGroup.addEventListener('change', updateAccountTypeUI);
    if (accountMode) accountMode.addEventListener('change', updateAccountTypeUI);
    updateAccountTypeUI();

    // Move create-transaction form into modal so main Accounts view stays clean.
    var txnCreateModalBody = document.getElementById('txnCreateModalBody');
    var txnFormHost = document.getElementById('txn_form_host');
    if (txnCreateModalBody && txnFormHost) {
        var movedForm = txnFormHost.querySelector('#txn_form');
        if (movedForm) txnCreateModalBody.appendChild(movedForm);
    }
    var intentMatrix = __hdcCfg.intentMatrix;
    var workerOptionRows = __hdcCfg.workerOptionRows;
    var supplierOptionRows = __hdcCfg.supplierOptionRows;
    var subcontractorOptionRows = __hdcCfg.subcontractorOptionRows;
    var officeStaffOptionRows = __hdcCfg.officeStaffOptionRows;
    var projectsData = __hdcCfg.projectsData;
    var projectsNames = __hdcCfg.projectsNames;
    var projectReceivableRows = __hdcCfg.projectReceivableRows;
    var projectClientMeta = __hdcCfg.projectClientMeta;
    var personalExpenseCategoryNames = __hdcCfg.personalExpenseCategoryNames;
    var personalExpensePartyNames = __hdcCfg.personalExpensePartyNames;
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
    // Block on any focused number input, regardless of which form it lives in.
    document.addEventListener('wheel', function (e) {
        var active = document.activeElement;
        if (!active) return;
        if (active.tagName === 'INPUT' && active.type === 'number') {
            e.preventDefault();
        }
    }, { capture: true, passive: false });
    var relatedTypeByTxn = {
        expense_material: 'supplier',
        purchase: 'supplier',
        expense_subcontractor: 'subcontractor',
        expense_wage: 'worker',
        payroll: 'worker',
        advance_to_person: 'worker',
        office_management_payment: 'office_staff'
    };
    var toAccountRequiredByTxn = {
        transfer: true,
        advance_to_person: true,
        project_income: true,
        party_receipt: true,
        client_payment: true,
        receive_from_project: true,
        receive_intra_company: true,
        receive_from_credit_debit: true
    };
    var toOrPartyRequiredByTxn = {
        expense_material: true,
        expense_wage: true,
        expense_subcontractor: true,
        office_management_payment: true,
        personal_management_payment: true,
        expense_general: true,
        purchase: true,
        payroll: true,
        party_payment: true
    };
    var hideReferenceByTxn = {
        payroll: true,
        expense_wage: true
    };
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
    var stageRequiredByTxn = {
        expense_material: false,
        expense_wage: true,
        expense_subcontractor: true,
        expense_general: true,
        purchase: false,
        payroll: true,
        advance_to_person: true
    };
    var projectRequiredByTxn = {
        project_income: true,
        expense_general: true
    };
    var expenseCategoryRequiredByTxn = {
        expense_general: true
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
            applyBalanceMapToOptionCollection((editFromAccount && editFromAccount.options), realtimeBalanceMap);
            applyBalanceMapToOptionCollection((editToAccount && editToAccount.options), realtimeBalanceMap);
            setSelectedBalanceHint(fromBalanceHint, fromSel, fromAccountHintPrefix());
            setSelectedBalanceHint(editFromBalanceHint, editFromAccount, 'Paying Account');
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
                applyBalanceMapToOptionCollection((editFromAccount && editFromAccount.options), realtimeBalanceMap);
                applyBalanceMapToOptionCollection((editToAccount && editToAccount.options), realtimeBalanceMap);
                setSelectedBalanceHint(fromBalanceHint, fromSel, fromAccountHintPrefix());
                setSelectedBalanceHint(editFromBalanceHint, editFromAccount, 'Paying Account');
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
        if (!label) return;
        var opt = document.createElement('option');
        opt.value = String(value || '');
        opt.textContent = label;
        opt.setAttribute('data-personal-suggestion', '1');
        opt.setAttribute('data-personal-label', label.replace(/^\[[^\]]+\]\s*/, '').trim());
        if (keepValue && String(keepValue) === String(opt.value)) opt.selected = true;
        selectEl.appendChild(opt);
    }
    function _injectPersonalSuggestions(selectEl, keepValue) {
        if (!selectEl) return;
        if (!Array.isArray(personalExpenseCategoryNames)) personalExpenseCategoryNames = [];
        if (!Array.isArray(personalExpensePartyNames)) personalExpensePartyNames = [];
        personalExpenseCategoryNames.forEach(function (name) {
            var nm = String(name || '').trim();
            if (!nm) return;
            _appendPersonalSuggestionOption(selectEl, 'pmcat::' + nm, '[Category] ' + nm, keepValue);
        });
        personalExpensePartyNames.forEach(function (name) {
            var nm = String(name || '').trim();
            if (!nm) return;
            _appendPersonalSuggestionOption(selectEl, 'pmparty::' + nm, '[Person] ' + nm, keepValue);
        });
    }
    function _selectedPersonalSuggestionLabel() {
        if (!toAccountSelect) return '';
        var opt = toAccountSelect.options[toAccountSelect.selectedIndex];
        if (!opt) return '';
        var val = String(opt.value || '');
        if (!_isPersonalSuggestionValue(val)) return '';
        var lbl = String(opt.getAttribute('data-personal-label') || '').trim();
        if (lbl) return lbl;
        var bits = val.split('::');
        return String((bits.length > 1 ? bits.slice(1).join('::') : '') || '').trim();
    }
    function _syncPersonalSuggestionToParty() {
        var tx = normalizeTxnType(txnType ? (txnType.value || '') : '');
        if (tx !== 'personal_management_payment') return;
        var picked = _selectedPersonalSuggestionLabel();
        if (!picked) return;
        if (partyNameInput && !String(partyNameInput.value || '').trim()) {
            partyNameInput.value = picked;
        }
    }
    function refillAccountSelect(selectEl, baseOptions, predicateFn, placeholderText, keepValue) {
        if (!selectEl) return;
        selectEl.innerHTML = '';
        var ph = document.createElement('option');
        ph.value = '';
        ph.textContent = placeholderText || 'Select';
        selectEl.appendChild(ph);
        var matched = false;
        baseOptions.forEach(function (srcOpt) {
            var val = String(srcOpt.value || '');
            if (!val) return;
            if (predicateFn && !predicateFn(srcOpt)) return;
            var o = srcOpt.cloneNode(true);
            if (keepValue && String(keepValue) === val) {
                o.selected = true;
                matched = true;
            }
            selectEl.appendChild(o);
        });
        if (!matched) selectEl.value = '';
    }
    function applyDirectionAccountFilters() {
        var dir = txnDirection ? (txnDirection.value || '') : '';
        var srcKind = receiveSourceKind ? (receiveSourceKind.value || '') : '';
        var tx = normalizeTxnType(txnType ? (txnType.value || '') : '');
        var keepFrom = fromSel ? (fromSel.value || '') : '';
        var keepTo = toAccountSelect ? (toAccountSelect.value || '') : '';
        if (dir === 'receive') {
            if (fromLabel) fromLabel.textContent = (tx === 'transfer' ? 'Receive From (Company Account)' : 'Received From (Party Account)');
            if (toLabel) toLabel.textContent = 'Receive In (Company Account)';
            refillAccountSelect(
                fromSel,
                fromBaseOptions,
                function (opt) {
                    if (tx === 'transfer') return !!treasuryAccountTypes[optionAccountType(opt)];
                    if (tx === 'project_income') return optionAccountGroup(opt) === 'project_in_flow';
                    if (tx === 'party_receipt' || tx === 'client_payment') {
                        if (srcKind === 'company') return !!treasuryAccountTypes[optionAccountType(opt)];
                        if (srcKind === 'project_in_flow') return optionAccountGroup(opt) === 'project_in_flow';
                        if (srcKind === 'credit_debit') return optionAccountGroup(opt) === 'credit_debit';
                    }
                    if (srcKind === 'company') return !!treasuryAccountTypes[optionAccountType(opt)];
                    if (treasuryAccountTypes[optionAccountType(opt)]) return false;
                    if (srcKind === 'project_in_flow') return optionAccountGroup(opt) === 'project_in_flow';
                    if (srcKind === 'credit_debit') return optionAccountGroup(opt) === 'credit_debit';
                    return true;
                },
                'Select party account',
                keepFrom
            );
            refillAccountSelect(
                toAccountSelect,
                toBaseOptions,
                function (opt) {
                    if (tx === 'transfer') return !!treasuryAccountTypes[optionAccountType(opt)];
                    return !!treasuryAccountTypes[optionAccountType(opt)];
                },
                'Select company account',
                keepTo
            );
        } else if (dir === 'pay') {
            if (fromLabel) fromLabel.textContent = 'Pay From (Company Account)';
            if (toLabel) {
                toLabel.textContent = (tx === 'transfer'
                    ? 'Pay To (Company Account)'
                    : (tx === 'personal_management_payment'
                        ? 'To Account / Category / Person (optional for off-ledger party)'
                        : 'To Account (optional for off-ledger party)'));
            }
            refillAccountSelect(
                fromSel,
                fromBaseOptions,
                function (opt) { return !!treasuryAccountTypes[optionAccountType(opt)]; },
                'Select company account',
                keepFrom
            );
            refillAccountSelect(
                toAccountSelect,
                toBaseOptions,
                function (opt) {
                    if (tx === 'transfer') return !!treasuryAccountTypes[optionAccountType(opt)];
                    if (tx === 'personal_management_payment') return false;
                    if (srcKind === 'company') return !!treasuryAccountTypes[optionAccountType(opt)];
                    if (srcKind === 'project_in_flow') return optionAccountGroup(opt) === 'project_in_flow';
                    if (srcKind === 'credit_debit') return optionAccountGroup(opt) === 'credit_debit';
                    return true;
                },
                (tx === 'personal_management_payment' ? 'Off-ledger Party (or choose category/person below)' : 'Off-ledger Party'),
                keepTo
            );
            if (tx === 'personal_management_payment') {
                _injectPersonalSuggestions(toAccountSelect, keepTo);
            }
        } else {
            if (fromLabel) fromLabel.textContent = 'From Account';
            if (toLabel) toLabel.textContent = 'To Account (optional for off-ledger party)';
            refillAccountSelect(fromSel, fromBaseOptions, function () { return true; }, 'Select', keepFrom);
            refillAccountSelect(toAccountSelect, toBaseOptions, function () { return true; }, 'Off-ledger Party', keepTo);
        }
        applyBalanceMapToOptionCollection((fromSel && fromSel.options), realtimeBalanceMap);
        applyBalanceMapToOptionCollection((toAccountSelect && toAccountSelect.options), realtimeBalanceMap);
        setSelectedBalanceHint(fromBalanceHint, fromSel, fromAccountHintPrefix());
    }
    var projectBaseOptions = Array.prototype.slice.call((projectSelect && projectSelect.options) ? projectSelect.options : []).map(function (o) { return o.cloneNode(true); });
    function receivableMapByProjectId() {
        var mp = {};
        (projectReceivableRows || []).forEach(function (r) {
            var key = String(r.id || '');
            if (!key) return;
            mp[key] = r;
        });
        return mp;
    }
    function applyProjectOptionsForReceiveSource() {
        if (!projectSelect) return;
        var dir = txnDirection ? (txnDirection.value || '') : '';
        var srcKind = receiveSourceKind ? (receiveSourceKind.value || '') : '';
        var keepValue = String(projectSelect.value || '');
        projectSelect.innerHTML = '';
        if (dir === 'receive' && srcKind === 'project_in_flow') {
            var ph = document.createElement('option');
            ph.value = '';
            ph.textContent = 'Select Project';
            projectSelect.appendChild(ph);
            var mp = receivableMapByProjectId();
            (projectReceivableRows || []).forEach(function (r) {
                var o = document.createElement('option');
                var pid = String(r.id || '');
                if (!pid) return;
                o.value = pid;
                o.textContent = (r.name || ('Project #' + pid)) + ' | Remaining: ' + money2(r.remaining_receivable || 0);
                if (keepValue && keepValue === pid) o.selected = true;
                projectSelect.appendChild(o);
            });
            if (projectSelect.options.length <= 1) {
                projectBaseOptions.forEach(function (baseOpt) {
                    if (!baseOpt || !baseOpt.value) return;
                    var pid = String(baseOpt.value || '');
                    var rec = mp[pid];
                    var o = baseOpt.cloneNode(true);
                    if (rec) o.textContent = (baseOpt.textContent || ('Project #' + pid)) + ' | Remaining: ' + money2(rec.remaining_receivable || 0);
                    projectSelect.appendChild(o);
                });
            }
        } else {
            projectBaseOptions.forEach(function (o) {
                projectSelect.appendChild(o.cloneNode(true));
            });
            if (keepValue) projectSelect.value = keepValue;
        }
    }
    var suppressVitalReset = true;
    function setResetNotice(msg) {
        if (!resetNotice) return;
        if (!msg) {
            resetNotice.classList.add('d-none');
            resetNotice.textContent = '';
            return;
        }
        resetNotice.classList.remove('d-none');
        resetNotice.textContent = msg;
    }
    function hasEnteredDependentValues(ignoreKey) {
        if (amountInput && ignoreKey !== 'amount' && String(amountInput.value || '').trim()) return true;
        if (toAccountSelect && ignoreKey !== 'to_account_id' && String(toAccountSelect.value || '').trim()) return true;
        if (projectSelect && ignoreKey !== 'project_id' && String(projectSelect.value || '').trim()) return true;
        if (stageSelect && ignoreKey !== 'stage_id' && String(stageSelect.value || '').trim()) return true;
        if (relatedIdSelect && ignoreKey !== 'related_entity_id' && String(relatedIdSelect.value || '').trim()) return true;
        if (partyNameInput && ignoreKey !== 'party_name' && String(partyNameInput.value || '').trim()) return true;
        if (refInput && ignoreKey !== 'reference_id' && String(refInput.value || '').trim()) return true;
        if (noteInput && ignoreKey !== 'note' && String(noteInput.value || '').trim()) return true;
        return false;
    }
    function clearPendingInfo() {
        if (!pendingInfo) return;
        pendingInfo.classList.add('d-none');
        pendingInfo.textContent = '';
        latestPendingItem = null;
    }
    function isWorkerPayType(t) {
        var tx = normalizeTxnType(t);
        return (tx === 'payroll' || tx === 'expense_wage');
    }
    function isSubcontractorPayType(t) {
        return normalizeTxnType(t) === 'expense_subcontractor';
    }
    function isOfficeStaffPayType(t) {
        var tx = normalizeTxnType(t);
        var target = officeTargetSelect ? (officeTargetSelect.value || 'staff') : 'staff';
        return (tx === 'office_management_payment' && target === 'staff');
    }
    function isPayableSplitType(t) {
        var tx = normalizeTxnType(t);
        return (tx === 'payroll' || tx === 'expense_wage' || tx === 'expense_subcontractor' || isOfficeStaffPayType(tx) || tx === 'purchase' || tx === 'expense_material');
    }
    function payablePendingAmount() {
        if (!latestPendingItem) return 0;
        return Number(latestPendingItem.pending || 0) || 0;
    }
    function syncExcessSplit(forceResetTip) {
        if (!amountInput || !excessTipInput || !excessAdvanceInput) return;
        var t = normalizeTxnType(txnType ? (txnType.value || '') : '');
        if (!isPayableSplitType(t)) {
            excessTipInput.value = '0';
            excessAdvanceInput.value = '0';
            if (excessSplitInfo) excessSplitInfo.textContent = '';
            return;
        }
        var payable = payablePendingAmount();
        var amt = Number(amountInput.value || 0) || 0;
        var excess = Math.max(0, amt - payable);
        if (excess <= 0.000001) {
            excessTipInput.value = '0';
            excessAdvanceInput.value = '0';
            if (excessSplitInfo) {
                excessSplitInfo.textContent = 'No excess. Amount is within payable/pending.';
            }
            return;
        }
        var tip = Number(excessTipInput.value || 0) || 0;
        if (forceResetTip && tip < 0) tip = 0;
        if (tip < 0) tip = 0;
        if (tip > excess) tip = excess;
        var adv = excess - tip;
        excessTipInput.value = String(Math.round(tip * 100) / 100);
        excessAdvanceInput.value = String(Math.round(adv * 100) / 100);
        if (excessSplitInfo) {
            excessSplitInfo.textContent =
                'Payable: ' + payable.toLocaleString() +
                ' | Excess: ' + excess.toLocaleString() +
                ' = Tip: ' + tip.toLocaleString() +
                ' + Advance: ' + adv.toLocaleString();
        }
    }
    function resetDependentFields(reason, preserveMap) {
        var keep = preserveMap || {};
        if (amountInput && !Object.prototype.hasOwnProperty.call(keep, 'amount')) amountInput.value = '';
        if (toAccountSelect && !Object.prototype.hasOwnProperty.call(keep, 'to_account_id')) toAccountSelect.value = '';
        if (projectSelect && !Object.prototype.hasOwnProperty.call(keep, 'project_id')) projectSelect.value = '';
        if (stageSelect && !Object.prototype.hasOwnProperty.call(keep, 'stage_id')) {
            stageSelect.innerHTML = '<option value=\"\">Select stage</option>';
            stageSelect.value = '';
        }
        if (relatedIdSelect && !Object.prototype.hasOwnProperty.call(keep, 'related_entity_id')) {
            relatedIdSelect.innerHTML = '<option value=\"\">Select entity</option>';
            relatedIdSelect.value = '';
        }
        if (partyNameInput && !Object.prototype.hasOwnProperty.call(keep, 'party_name')) partyNameInput.value = '';
        if (refInput && !Object.prototype.hasOwnProperty.call(keep, 'reference_id')) refInput.value = '';
        if (noteInput && !Object.prototype.hasOwnProperty.call(keep, 'note')) noteInput.value = '';
        if (excessTipInput && !Object.prototype.hasOwnProperty.call(keep, 'excess_tip_amount')) excessTipInput.value = '0';
        if (excessAdvanceInput && !Object.prototype.hasOwnProperty.call(keep, 'excess_advance_amount')) excessAdvanceInput.value = '0';
        if (settleShortfallInput && !Object.prototype.hasOwnProperty.call(keep, 'settle_shortfall')) settleShortfallInput.checked = false;
        if (expenseCategoryInput && !Object.prototype.hasOwnProperty.call(keep, 'expense_category_id')) expenseCategoryInput.value = '';
        if (officeTargetSelect && !Object.prototype.hasOwnProperty.call(keep, 'office_target')) officeTargetSelect.value = 'staff';
        if (officeExpenseCategoryInput && !Object.prototype.hasOwnProperty.call(keep, 'office_expense_category')) officeExpenseCategoryInput.value = '';
        clearPendingInfo();
        setResetNotice(reason || 'Form details were reset after vital field change.');
    }
    function filterTxnTypeOptionsByDirection() {
        if (!txnType) return;
        var dir = txnDirection ? (txnDirection.value || '') : '';
        var firstAllowed = '';
        Array.prototype.slice.call(txnType.options).forEach(function (opt, idx) {
            if (idx === 0) return;
            var od = (opt.getAttribute('data-direction') || '').toLowerCase();
            var allow = !!dir && (od === dir);
            opt.disabled = !allow;
            opt.hidden = !allow;
            if (allow && !firstAllowed) firstAllowed = opt.value || '';
        });
        var current = txnType.value || '';
        var currentOpt = current ? txnType.querySelector('option[value=\"' + current.replace(/"/g, '\\"') + '\"]') : null;
        var currentAllowed = !!(currentOpt && !currentOpt.disabled);
        if (!currentAllowed) {
            txnType.value = firstAllowed || '';
        }
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
        filterTxnTypeOptionsByDirection();
        applyDirectionAccountFilters();
        applyProjectOptionsForReceiveSource();
    }
    function applyCounterpartyGroupByTxn() {
        if (!receiveSourceKind) return;
        var t = txnType ? (txnType.value || '') : '';
        var wanted = counterpartyGroupByTxn[t] || counterpartyGroupByTxn[normalizeTxnType(t)] || '';
        if (!wanted) return;
        var hasOpt = !!receiveSourceKind.querySelector('option[value="' + wanted.replace(/"/g, '\\"') + '"]');
        if (hasOpt) receiveSourceKind.value = wanted;
    }
    function applyCleanTypeLayout(rawType, normalizedType, hasType, intentMeta) {
        var counterpartyKnown = !!(counterpartyGroupByTxn[rawType] || counterpartyGroupByTxn[normalizedType]);
        // Direction is the first-step control and should stay visible always.
        if (txnDirectionWrap) txnDirectionWrap.classList.remove('d-none');
        if (receiveSourceWrap) receiveSourceWrap.classList.toggle('d-none', !!(hasType && counterpartyKnown));
        var toReq = !!(toAccountRequiredByTxn[rawType] || toAccountRequiredByTxn[normalizedType]);
        var hasExpectedRelated = !!(relatedTypeByTxn[normalizedType] || relatedTypeByTxn[rawType]);
        var toSelected = !!(toAccountSelect && String(toAccountSelect.value || '').trim());
        if (partyNameWrap) partyNameWrap.classList.toggle('d-none', (!hasType || toReq || toSelected || hasExpectedRelated));
        if (partyNameInput) {
            if (toReq || toSelected || hasExpectedRelated) partyNameInput.value = '';
            partyNameInput.required = !!(
                hasType &&
                !toReq &&
                !toSelected &&
                !hasExpectedRelated &&
                (toOrPartyRequiredByTxn[rawType] || toOrPartyRequiredByTxn[normalizedType])
            );
        }
    }
    function bindProjectIncomeClientAccount() {
        if (!fromSel) return;
        var dir = txnDirection ? (txnDirection.value || '') : '';
        var srcKind = receiveSourceKind ? (receiveSourceKind.value || '') : '';
        var tx = normalizeTxnType(txnType ? (txnType.value || '') : '');
        var isProjectIncomeFlow = (dir === 'receive' && srcKind === 'project_in_flow' && tx === 'project_income');
        if (!isProjectIncomeFlow) {
            fromSel.required = true;
            // Clear stale project-income helper state when user switches to non-project receipt/payment.
            if (fromBalanceHint) {
                fromBalanceHint.classList.add('d-none');
                fromBalanceHint.classList.remove('text-success', 'text-danger', 'text-muted');
                fromBalanceHint.textContent = '';
            }
            return;
        }
        var pid = projectSelect ? String(projectSelect.value || '') : '';
        var meta = (pid ? projectClientMeta[pid] : null) || null;
        if (meta && meta.account_id) {
            refillAccountSelect(
                fromSel,
                fromBaseOptions,
                function (opt) { return String(opt.value || '') === String(meta.account_id); },
                'Auto-linked project client',
                String(meta.account_id)
            );
            applyBalanceMapToOptionCollection((fromSel && fromSel.options), realtimeBalanceMap);
            setSelectedBalanceHint(fromBalanceHint, fromSel, 'Project Client (Auto)');
            if (partyNameInput) partyNameInput.value = String(meta.client_name || '');
            fromSel.required = false;
        } else {
            refillAccountSelect(
                fromSel,
                fromBaseOptions,
                function () { return false; },
                'Auto-linked on save from selected project client',
                ''
            );
            if (fromBalanceHint) {
                fromBalanceHint.classList.remove('d-none', 'text-success', 'text-danger');
                fromBalanceHint.classList.add('text-muted');
                fromBalanceHint.textContent = 'Project client account will be auto-linked on save.';
            }
            if (partyNameInput && meta && meta.client_name) partyNameInput.value = String(meta.client_name || '');
            fromSel.required = false;
        }
    }
    function optionRowsForType(type) {
        if (type === 'worker') return workerOptionRows || [];
        if (type === 'supplier') return supplierOptionRows || [];
        if (type === 'subcontractor') return subcontractorOptionRows || [];
        if (type === 'office_staff') return officeStaffOptionRows || [];
        return [];
    }
    function filterOptionRowsByScope(type, rows, projectId, stageId) {
        var out = rows || [];
        if (type !== 'subcontractor') return out;
        var pid = String(projectId || '');
        var sid = String(stageId || '');
        if (sid) {
            out = out.filter(function (r) { return String(r.stage_id || '') === sid; });
        }
        if (pid) {
            out = out.filter(function (r) {
                var rpid = String(r.project_id || '');
                return (!rpid || rpid === pid);
            });
        }
        return out;
    }
    function refillEntitySelect(entityType, keepValue) {
        if (!relatedIdSelect) return;
        var pid = projectSelect ? (projectSelect.value || '') : '';
        var sid = stageSelect ? (stageSelect.value || '') : '';
        relatedIdSelect.innerHTML = '<option value=\"\">Select entity</option>';
        filterOptionRowsByScope(entityType, optionRowsForType(entityType), pid, sid).forEach(function (r) {
            var o = document.createElement('option');
            o.value = String(r.id || '');
            o.textContent = r.label || ('#' + r.id);
            if (keepValue && String(keepValue) === String(r.id)) o.selected = true;
            relatedIdSelect.appendChild(o);
        });
    }
    function fetchPendingInfo() {
        if (!pendingInfo) return;
        var t = txnType ? (txnType.value || '') : '';
        var et = relatedTypeSelect ? (relatedTypeSelect.value || '') : '';
        var eid = relatedIdSelect ? (relatedIdSelect.value || '') : '';
        var pid = projectSelect ? (projectSelect.value || '') : '';
        var sid = stageSelect ? (stageSelect.value || '') : '';
        if (!t) {
            pendingInfo.classList.add('d-none');
            pendingInfo.textContent = '';
            return;
        }
        var qs = new URLSearchParams({
            type: t,
            related_entity_type: et,
            related_entity_id: eid,
            project_id: pid,
            stage_id: sid
        });
        fetch('/api/accounts/pending_context?' + qs.toString())
            .then(function (r) { return r.json(); })
            .then(function (j) {
                var it = (j && j.item) ? j.item : null;
                latestPendingItem = it || null;
                if (!it || (!it.message && !(it.pending > 0 || it.total > 0 || it.paid > 0))) {
                    pendingInfo.classList.add('d-none');
                    pendingInfo.textContent = '';
                    syncExcessSplit(true);
                    return;
                }
                pendingInfo.classList.remove('d-none');
                pendingInfo.textContent = (it.message || 'Pending') + ' | Total: ' + Number(it.total || 0).toLocaleString() + ' | Paid: ' + Number(it.paid || 0).toLocaleString();
                syncExcessSplit(true);
            })
            .catch(function () {
                latestPendingItem = null;
                syncExcessSplit(true);
            });
    }
    function loadProjectStages(projectId, selectedStageId) {
        if (!stageSelect) return Promise.resolve();
        var reqSeq = ++stageLoadSeq;
        stageSelect.innerHTML = '<option value=\"\">Select stage</option>';
        if (!projectId) return Promise.resolve();
        return fetch('/hdc/api/project_stages/' + encodeURIComponent(projectId))
            .then(function (r) { return r.json(); })
            .then(function (items) {
                if (reqSeq !== stageLoadSeq) return;
                var seen = {};
                (items || []).forEach(function (st) {
                    var key = String(st.id || '');
                    if (!key || seen[key]) return;
                    seen[key] = true;
                    var opt = document.createElement('option');
                    opt.value = key;
                    opt.textContent = st.name || ('Stage #' + st.id);
                    if (selectedStageId && String(selectedStageId) === String(st.id)) opt.selected = true;
                    stageSelect.appendChild(opt);
                });
            })
            .catch(function () {});
    }
    function updateTxnUI() {
        var rawT = txnType ? (txnType.value || '') : '';
        var t = normalizeTxnType(rawT);
        var m = intentMatrix[t] || {};
        var hasType = !!rawT;
        var isOfficeMgmt = (hasType && t === 'office_management_payment');
        var officeTarget = officeTargetSelect ? (officeTargetSelect.value || 'staff') : 'staff';
        applyCounterpartyGroupByTxn();
        // Keep account dropdowns in sync with currently selected direction + type + counterparty group.
        applyDirectionAccountFilters();
        if (officeTargetWrap) officeTargetWrap.classList.toggle('d-none', !isOfficeMgmt);
        if (toWrap) toWrap.classList.toggle('d-none', (!hasType || !m.to_account));
        if (toAccountSelect) toAccountSelect.required = !!(toAccountRequiredByTxn[rawT] || toAccountRequiredByTxn[t]);
        var isWorkerProjectStageScope = (isWorkerPayType(t) || isSubcontractorPayType(t));
        var workerProjectStageActive = true;
        if (isWorkerProjectStageScope) {
            var tipAmount = Number((excessTipInput ? excessTipInput.value : 0) || 0) || 0;
            var settleActive = !!(settleShortfallInput && settleShortfallInput.checked);
            workerProjectStageActive = (tipAmount > 0.000001 || settleActive);
        }
        var showProjectScope = (!!m.project && (!isWorkerProjectStageScope || workerProjectStageActive));
        var showStageScope = (!!m.stage && (!isWorkerProjectStageScope || workerProjectStageActive));
        if (projectWrap) projectWrap.classList.toggle('d-none', (!hasType || !showProjectScope));
        if (projectSelect) projectSelect.required = !!(showProjectScope && (hasType && (projectRequiredByTxn[rawT] || projectRequiredByTxn[t])));
        var hasProject = !!(projectSelect && projectSelect.value);
        if (stageWrap) stageWrap.classList.toggle('d-none', (!hasType || !showStageScope || !hasProject));
        if (stageSelect) stageSelect.required = (showStageScope && hasProject && !!stageRequiredByTxn[t]);
        var showExpenseCategory = hasType && !!(expenseCategoryRequiredByTxn[rawT] || expenseCategoryRequiredByTxn[t]);
        if (expenseCategoryWrap) expenseCategoryWrap.classList.toggle('d-none', !showExpenseCategory);
        if (expenseCategoryInput) {
            expenseCategoryInput.required = !!showExpenseCategory;
            if (!showExpenseCategory) expenseCategoryInput.value = '';
        }
        var showOfficeExpenseCategory = (isOfficeMgmt && officeTarget === 'expense');
        if (officeExpenseCategoryWrap) officeExpenseCategoryWrap.classList.toggle('d-none', !showOfficeExpenseCategory);
        if (officeExpenseCategoryInput) {
            officeExpenseCategoryInput.required = !!showOfficeExpenseCategory;
            if (!showOfficeExpenseCategory) officeExpenseCategoryInput.value = '';
        }
        if (relatedWrap) {
            var showRelated = (!hasType || !m.related) ? false : true;
            if (isOfficeMgmt && officeTarget === 'expense') showRelated = false;
            relatedWrap.classList.toggle('d-none', !showRelated);
        }
        var showWorkerSplit = hasType && isPayableSplitType(t);
        var showSettleShortfall = hasType && (isWorkerPayType(t) || isSubcontractorPayType(t));
        if (excessSplitWrap) excessSplitWrap.classList.toggle('d-none', !showWorkerSplit);
        if (settleShortfallWrap) settleShortfallWrap.classList.toggle('d-none', !showSettleShortfall);
        var hideRef = !!(hideReferenceByTxn[rawT] || hideReferenceByTxn[t]);
        if (refWrap) refWrap.classList.toggle('d-none', !!hideRef);
        if (hideRef && refInput) refInput.value = '';
        var expectedRel = relatedTypeByTxn[t] || relatedTypeByTxn[rawT] || '';
        if (isOfficeMgmt) {
            expectedRel = (officeTarget === 'staff' ? 'office_staff' : '');
        }
        if (relatedTypeSelect) {
            relatedTypeSelect.value = expectedRel;
        }
        if (relatedIdSelect) {
            if (expectedRel) {
                refillEntitySelect(expectedRel, relatedIdSelect.value || '');
                relatedIdSelect.required = true;
            } else {
                relatedIdSelect.required = false;
                relatedIdSelect.innerHTML = '<option value=\"\">Select entity</option>';
            }
        }
        if (relatedHelp) {
            if (expectedRel) {
                relatedHelp.classList.remove('d-none');
                relatedHelp.textContent = 'Required for this transaction: ' + expectedRel.charAt(0).toUpperCase() + expectedRel.slice(1);
            } else {
                relatedHelp.classList.add('d-none');
                relatedHelp.textContent = '';
            }
        }
        if (projectSelect && projectSelect.value) {
            loadProjectStages(projectSelect.value, stageSelect ? stageSelect.value : '');
        }
        applyCleanTypeLayout(rawT, t, hasType, m);
        bindProjectIncomeClientAccount();
        if (!showWorkerSplit) {
            if (excessTipInput) excessTipInput.value = '0';
            if (excessAdvanceInput) excessAdvanceInput.value = '0';
            if (excessSplitInfo) excessSplitInfo.textContent = '';
        }
        if (!showSettleShortfall) {
            if (settleShortfallInput) settleShortfallInput.checked = false;
        }
        fetchPendingInfo();
    }
        if (txnDirection) {
            txnDirection.addEventListener('change', function () {
                if (!suppressVitalReset && hasEnteredDependentValues('txn_direction')) {
                    resetDependentFields('Direction changed, so dependent fields were reset for consistency.', { txn_direction: txnDirection.value || '' });
                } else {
                    setResetNotice('');
                }
                applyReceiveSourceKindUI();
                updateTxnUI();
            });
        }
    if (receiveSourceKind) {
        receiveSourceKind.addEventListener('change', function () {
            if (!suppressVitalReset && hasEnteredDependentValues('receive_source_kind')) {
                resetDependentFields('Received-from type changed, so dependent fields were reset for consistency.', {
                    receive_source_kind: receiveSourceKind.value || '',
                    txn_direction: (txnDirection ? (txnDirection.value || '') : '')
                });
            } else {
                setResetNotice('');
            }
            applyReceiveSourceKindUI();
            updateTxnUI();
        });
    }
    if (txnType) {
        txnType.addEventListener('change', function () {
            if (!suppressVitalReset && hasEnteredDependentValues('type')) {
                resetDependentFields('Transaction type changed, so dependent fields were reset for consistency.', {
                    type: txnType.value || '',
                    txn_direction: (txnDirection ? (txnDirection.value || '') : '')
                });
            } else {
                setResetNotice('');
            }
            updateTxnUI();
        });
    }
    if (officeTargetSelect) {
        officeTargetSelect.addEventListener('change', function () {
            if (!suppressVitalReset && hasEnteredDependentValues('office_target')) {
                resetDependentFields('Office target changed, so dependent fields were reset.', {
                    office_target: officeTargetSelect.value || 'staff',
                    type: (txnType ? (txnType.value || '') : ''),
                    txn_direction: (txnDirection ? (txnDirection.value || '') : '')
                });
            } else {
                setResetNotice('');
            }
            updateTxnUI();
        });
    }
    if (fromSel) {
        fromSel.addEventListener('change', function () {
            if (!suppressVitalReset && hasEnteredDependentValues('from_account_id')) {
                resetDependentFields('From account changed, so dependent fields were reset for consistency.', {
                    from_account_id: fromSel.value || '',
                    type: (txnType ? (txnType.value || '') : ''),
                    txn_direction: (txnDirection ? (txnDirection.value || '') : '')
                });
            } else {
                setResetNotice('');
            }
            updateBankInfo(fromSel);
            setSelectedBalanceHint(fromBalanceHint, fromSel, fromAccountHintPrefix());
            updateTxnUI();
        });
        fromSel.addEventListener('focus', function () { refreshAccountBalancesRealtime(true); });
        fromSel.addEventListener('mousedown', function () { refreshAccountBalancesRealtime(true); });
    }
    if (projectSelect) {
        projectSelect.addEventListener('change', function () {
            if (!suppressVitalReset && hasEnteredDependentValues('project_id')) {
                resetDependentFields('Project changed, so stage/entity/payment details were reset.', {
                    project_id: projectSelect.value || '',
                    type: (txnType ? (txnType.value || '') : ''),
                    from_account_id: (fromSel ? (fromSel.value || '') : ''),
                    txn_direction: (txnDirection ? (txnDirection.value || '') : '')
                });
            } else {
                setResetNotice('');
            }
            var pid = projectSelect.value || '';
            loadProjectStages(pid, '');
            bindProjectIncomeClientAccount();
            updateTxnUI();
        });
    }
    if (stageSelect) {
        stageSelect.addEventListener('change', function () {
            if (!suppressVitalReset && hasEnteredDependentValues('stage_id')) {
                resetDependentFields('Stage changed, so dependent fields were reset for consistency.', {
                    stage_id: stageSelect.value || '',
                    project_id: (projectSelect ? (projectSelect.value || '') : ''),
                    type: (txnType ? (txnType.value || '') : ''),
                    from_account_id: (fromSel ? (fromSel.value || '') : ''),
                    txn_direction: (txnDirection ? (txnDirection.value || '') : '')
                });
            } else {
                setResetNotice('');
            }
            updateTxnUI();
        });
    }
    if (relatedIdSelect) {
        relatedIdSelect.addEventListener('change', function () {
            var selectedLabel = '';
            if (relatedIdSelect.selectedIndex >= 0) {
                selectedLabel = (relatedIdSelect.options[relatedIdSelect.selectedIndex] || {}).text || '';
            }
            var partyVisible = !!(partyNameWrap && !partyNameWrap.classList.contains('d-none'));
            if (partyNameInput && selectedLabel && partyVisible) {
                partyNameInput.value = selectedLabel;
            } else if (partyNameInput && !partyVisible) {
                partyNameInput.value = '';
            }
            setResetNotice('');
            fetchPendingInfo();
        });
    }
    if (amountInput) {
        amountInput.addEventListener('input', function () {
            syncExcessSplit(false);
        });
    }
    if (excessTipInput) {
        excessTipInput.addEventListener('input', function () {
            syncExcessSplit(false);
        });
    }
    if (txnForm) {
        txnForm.addEventListener('submit', function (e) {
            var t = txnType ? (txnType.value || '') : '';
            var nt = normalizeTxnType(t);
            if (nt === 'personal_management_payment' && toAccountSelect) {
                var selectedVal = String(toAccountSelect.value || '');
                if (_isPersonalSuggestionValue(selectedVal)) {
                    var picked = _selectedPersonalSuggestionLabel();
                    if (partyNameInput && picked && !String(partyNameInput.value || '').trim()) {
                        partyNameInput.value = picked;
                    }
                    toAccountSelect.value = '';
                }
            }
            if (nt === 'expense_general' && expenseCategoryInput && !String(expenseCategoryInput.value || '').trim()) {
                e.preventDefault();
                alert('Please select an Expense Category for General Expense.');
                return;
            }
            if (nt === 'office_management_payment') {
                var officeTarget = officeTargetSelect ? (officeTargetSelect.value || 'staff') : 'staff';
                if (officeTarget === 'expense' && officeExpenseCategoryInput && !String(officeExpenseCategoryInput.value || '').trim()) {
                    e.preventDefault();
                    alert('Please enter Office Expense Category.');
                    return;
                }
                if (officeTarget === 'staff' && relatedIdSelect && !String(relatedIdSelect.value || '').trim()) {
                    e.preventDefault();
                    alert('Please select Office Staff.');
                    return;
                }
            }
            var isWorkerType = isWorkerPayType(t);
            var isOfficeStaffType = isOfficeStaffPayType(t);
            var isSplitType = isPayableSplitType(t);
            if (!isSplitType && !isSubcontractorPayType(t)) return;
            var pid = projectSelect ? (projectSelect.value || '') : '';
            var sid = stageSelect ? (stageSelect.value || '') : '';
            if (settleShortfallInput && settleShortfallInput.checked && (!pid || !sid)) {
                e.preventDefault();
                alert('For shortfall settlement, please select both project and stage.');
                return;
            }
            if (!isSplitType) return;
            var payable = payablePendingAmount();
            var amt = Number((amountInput && amountInput.value) || 0) || 0;
            var excess = Math.max(0, amt - payable);
            var tip = Number((excessTipInput && excessTipInput.value) || 0) || 0;
            var adv = Number((excessAdvanceInput && excessAdvanceInput.value) || 0) || 0;
            if (tip < -0.000001 || adv < -0.000001) {
                e.preventDefault();
                alert('Tip and Advance cannot be negative.');
                return;
            }
            if (Math.abs((tip + adv) - excess) > 0.01) {
                e.preventDefault();
                alert('Tip + Advance must match excess amount over payable.');
                return;
            }
            var needsProjectStageScope = (isWorkerType || isSubcontractorPayType(t));
            if (!isOfficeStaffType && needsProjectStageScope && (tip > 0.01 || (settleShortfallInput && settleShortfallInput.checked)) && (!pid || !sid)) {
                e.preventDefault();
                alert('For tip or shortfall settlement, please select both project and stage.');
                return;
            }
        });
    }

    var bankInfo = document.getElementById('bank_info');
    function updateBankInfo(sel) {
        if (!sel || !bankInfo) return;
        var opt = sel.options[sel.selectedIndex];
        if (!opt) return;
        var bank = opt.getAttribute('data-bank') || '';
        var ac = opt.getAttribute('data-acno') || '';
        var iban = opt.getAttribute('data-iban') || '';
        if (bank || ac || iban) {
            bankInfo.classList.remove('d-none');
            bankInfo.textContent = 'Bank: ' + (bank || '-') + ' | A/C: ' + (ac || '-') + (iban ? (' | IBAN: ' + iban) : '');
        } else {
            bankInfo.classList.add('d-none');
            bankInfo.textContent = '';
        }
    }
    var toSel = document.getElementById('to_account');
    if (fromSel) fromSel.addEventListener('change', function () { updateBankInfo(fromSel); });
    if (toSel) {
        toSel.addEventListener('change', function () {
            _syncPersonalSuggestionToParty();
            updateBankInfo(toSel);
            updateTxnUI();
        });
        toSel.addEventListener('focus', function () { refreshAccountBalancesRealtime(true); });
        toSel.addEventListener('mousedown', function () { refreshAccountBalancesRealtime(true); });
    }
    applyReceiveSourceKindUI();
    updateTxnUI();
    updateBankInfo(fromSel);
    refreshAccountBalancesRealtime(true);
    setSelectedBalanceHint(fromBalanceHint, fromSel, fromAccountHintPrefix());
    suppressVitalReset = false;

    var editForm = document.getElementById('account_edit_form');
    var editButtons = document.querySelectorAll('.account-edit-btn');
    editButtons.forEach(function (btn) {
        btn.addEventListener('click', function () {
            var id = btn.getAttribute('data-id') || '';
            var name = btn.getAttribute('data-name') || '';
            var type = btn.getAttribute('data-type') || '';
            var group = btn.getAttribute('data-group') || '';
            var mode = btn.getAttribute('data-mode') || '';
            var opening = btn.getAttribute('data-opening') || '0';
            var bank = btn.getAttribute('data-bank') || '';
            var acno = btn.getAttribute('data-acno') || '';
            var iban = btn.getAttribute('data-iban') || '';

            var n = prompt('Account name:', name);
            if (n === null) return;
            n = (n || '').trim();
            if (!n) { alert('Name is required.'); return; }

            var inferredGroup = group || ((type === 'company' || type === 'cash' || type === 'bank') ? 'company' : ((type === 'client') ? 'project_in_flow' : 'credit_debit'));
            var g = prompt('Account group (company/project_in_flow/credit_debit):', inferredGroup);
            if (g === null) return;
            g = (g || '').trim().toLowerCase();
            if (g === 'external') g = 'credit_debit';
            if (g !== 'company' && g !== 'project_in_flow' && g !== 'credit_debit') { alert('Group must be company, project_in_flow, or credit_debit.'); return; }

            var m = 'cash';
            if (g === 'company') {
                m = prompt('Account mode (bank/cash):', mode || (type === 'bank' ? 'bank' : 'cash'));
                if (m === null) return;
                m = (m || '').trim().toLowerCase();
                if (m !== 'bank' && m !== 'cash') { alert('Mode must be bank or cash.'); return; }
            }

            var t = (g === 'company' ? (m === 'bank' ? 'bank' : 'cash') : (g === 'project_in_flow' ? 'client' : 'person'));

            var op = prompt('Opening balance:', opening);
            if (op === null) return;

            var bname = bank;
            var bacno = acno;
            var biban = iban;
            if (m === 'bank') {
                bname = prompt('Bank name:', bank || '') || '';
                bacno = prompt('Account number:', acno || '') || '';
                biban = prompt('IBAN (optional):', iban || '') || '';
                if (!bname.trim() || !bacno.trim()) {
                    alert('Bank name and account number are required for bank account.');
                    return;
                }
            } else {
                bname = '';
                bacno = '';
                biban = '';
            }

            document.getElementById('edit_account_id').value = id;
            document.getElementById('edit_name').value = n;
            document.getElementById('edit_type').value = t;
            document.getElementById('edit_group').value = g;
            document.getElementById('edit_mode').value = m;
            document.getElementById('edit_opening').value = op;
            document.getElementById('edit_bank').value = bname;
            document.getElementById('edit_acno').value = bacno;
            document.getElementById('edit_iban').value = biban;
            editForm.submit();
        });
    });

    var txnEditButtons = document.querySelectorAll('.txn-edit-btn');
    var txnEditModalEl = document.getElementById('txnEditModal');
    var txnEditModal = (txnEditModalEl && window.bootstrap && bootstrap.Modal) ? new bootstrap.Modal(txnEditModalEl) : null;
    var editTxnId = document.getElementById('edit_txn_id');
    var editTxnType = document.getElementById('edit_txn_type');
    var editTxnDate = document.getElementById('edit_txn_date');
    var editTxnAmount = document.getElementById('edit_txn_amount');
    var editFromAccount = document.getElementById('edit_from_account');
    var editFromBalanceHint = document.getElementById('edit_from_account_balance_hint');
    var editToAccount = document.getElementById('edit_to_account');
    var editProject = document.getElementById('edit_project_id');
    var editStageWrap = document.getElementById('edit_stage_wrap');
    var editStage = document.getElementById('edit_stage_id');
    var editRelatedWrap = document.getElementById('edit_related_wrap');
    var editRelatedType = document.getElementById('edit_related_type');
    var editRelatedId = document.getElementById('edit_related_id');
    var editRelatedHelp = document.getElementById('edit_related_help');
    var editParty = document.getElementById('edit_party_name');
    var editNote = document.getElementById('edit_note');
    var editRef = document.getElementById('edit_reference_id');
    var editInfo = document.getElementById('edit_txn_info');
    var editToWrap = document.getElementById('edit_to_wrap');
    var editProjectWrap = document.getElementById('edit_project_wrap');
    var editStageLoadSeq = 0;
    var editRelatedTypeByTxn = {
        expense_material: 'supplier',
        purchase: 'supplier',
        expense_subcontractor: 'subcontractor',
        expense_wage: 'worker',
        payroll: 'worker',
        advance_to_person: 'worker',
        office_management_payment: 'office_staff'
    };
    function editOptionRowsForType(type) {
        if (type === 'worker') return workerOptionRows || [];
        if (type === 'supplier') return supplierOptionRows || [];
        if (type === 'subcontractor') return subcontractorOptionRows || [];
        if (type === 'office_staff') return officeStaffOptionRows || [];
        return [];
    }
    var latestReceiptTxnId = __hdcCfg.latestReceiptTxnId;
    var saveReceiptModalEl = document.getElementById('saveReceiptModal');
    var saveReceiptModal = (saveReceiptModalEl && window.bootstrap && bootstrap.Modal) ? new bootstrap.Modal(saveReceiptModalEl) : null;
    var btnViewReceiptNow = document.getElementById('btnViewReceiptNow');
    var btnPrintReceiptNow = document.getElementById('btnPrintReceiptNow');
    function latestReceiptBaseUrl() {
        if (!latestReceiptTxnId) return '';
        return '/hdc/accounts/transactions/' + encodeURIComponent(String(latestReceiptTxnId)) + '/receipt';
    }
    if (btnViewReceiptNow) {
        btnViewReceiptNow.addEventListener('click', function () {
            var base = latestReceiptBaseUrl();
            if (!base) return;
            window.open(base, '_blank', 'noopener');
        });
    }
    if (btnPrintReceiptNow) {
        btnPrintReceiptNow.addEventListener('click', function () {
            var base = latestReceiptBaseUrl();
            if (!base) return;
            window.open(base + '?auto_print=1', '_blank', 'noopener');
        });
    }
    if (latestReceiptTxnId && saveReceiptModal) {
        setTimeout(function () { saveReceiptModal.show(); }, 250);
        try {
            var u = new URL(window.location.href);
            u.searchParams.delete('print_txn_id');
            window.history.replaceState({}, document.title, u.toString());
        } catch (e) {}
    }
    function editRefillEntitySelect(entityType, keepValue) {
        if (!editRelatedId) return;
        var pid = editProject ? (editProject.value || '') : '';
        var sid = editStage ? (editStage.value || '') : '';
        editRelatedId.innerHTML = '<option value=\"\">Select entity</option>';
        filterOptionRowsByScope(entityType, editOptionRowsForType(entityType), pid, sid).forEach(function (r) {
            var o = document.createElement('option');
            o.value = String(r.id || '');
            o.textContent = r.label || ('#' + r.id);
            if (keepValue && String(keepValue) === String(r.id)) o.selected = true;
            editRelatedId.appendChild(o);
        });
    }
    function editLoadProjectStages(projectId, selectedStageId) {
        if (!editStage) return Promise.resolve();
        var reqSeq = ++editStageLoadSeq;
        editStage.innerHTML = '<option value=\"\">Select stage</option>';
        if (!projectId) return Promise.resolve();
        return fetch('/hdc/api/project_stages/' + encodeURIComponent(projectId))
            .then(function (r) { return r.json(); })
            .then(function (items) {
                if (reqSeq !== editStageLoadSeq) return;
                var seen = {};
                (items || []).forEach(function (st) {
                    var key = String(st.id || '');
                    if (!key || seen[key]) return;
                    seen[key] = true;
                    var opt = document.createElement('option');
                    opt.value = key;
                    opt.textContent = st.name || ('Stage #' + st.id);
                    if (selectedStageId && String(selectedStageId) === String(st.id)) opt.selected = true;
                    editStage.appendChild(opt);
                });
            })
            .catch(function () {});
    }
    function editUpdateTxnUI() {
        var t = editTxnType ? (editTxnType.value || '') : '';
        var m = intentMatrix[t] || {};
        if (editToWrap) editToWrap.classList.toggle('d-none', !m.to_account);
        if (editProjectWrap) editProjectWrap.classList.toggle('d-none', !m.project);
        if (editProject) editProject.required = !!m.project;
        var hasProject = !!(editProject && editProject.value);
        var needsStage = !!m.stage;
        if (editStageWrap) editStageWrap.classList.toggle('d-none', (!needsStage || !hasProject));
        if (editStage) editStage.required = (needsStage && hasProject);
        if (editRelatedWrap) editRelatedWrap.classList.toggle('d-none', !m.related);
        var expectedRel = editRelatedTypeByTxn[t] || '';
        if (editRelatedType) editRelatedType.value = expectedRel;
        if (editRelatedId) {
            if (expectedRel) {
                editRefillEntitySelect(expectedRel, editRelatedId.value || '');
                editRelatedId.required = true;
            } else {
                editRelatedId.required = false;
                editRelatedId.innerHTML = '<option value=\"\">Select entity</option>';
            }
        }
        if (editRelatedHelp) {
            if (expectedRel) {
                editRelatedHelp.classList.remove('d-none');
                editRelatedHelp.textContent = 'Required for this transaction: ' + expectedRel.charAt(0).toUpperCase() + expectedRel.slice(1);
            } else {
                editRelatedHelp.classList.add('d-none');
                editRelatedHelp.textContent = '';
            }
        }
    }
    if (editTxnType) {
        editTxnType.addEventListener('change', function () {
            editUpdateTxnUI();
        });
    }
    if (editProject) {
        editProject.addEventListener('change', function () {
            var pid = editProject.value || '';
            editLoadProjectStages(pid, '').then(function () { editUpdateTxnUI(); });
        });
    }
    if (editFromAccount) {
        editFromAccount.addEventListener('change', function () {
            setSelectedBalanceHint(editFromBalanceHint, editFromAccount, 'Paying Account');
        });
        editFromAccount.addEventListener('focus', function () { refreshAccountBalancesRealtime(true); });
        editFromAccount.addEventListener('mousedown', function () { refreshAccountBalancesRealtime(true); });
    }
    if (editToAccount) {
        editToAccount.addEventListener('focus', function () { refreshAccountBalancesRealtime(true); });
        editToAccount.addEventListener('mousedown', function () { refreshAccountBalancesRealtime(true); });
    }
    txnEditButtons.forEach(function (btn) {
        btn.addEventListener('click', function () {
            var sourceType = (btn.getAttribute('data-source') || '').trim();
            var groupId = (btn.getAttribute('data-group') || '').trim();
            if (!txnEditModal) return;
            if (sourceType) {
                alert('This entry is synced from another module. Edit it in the source module.');
                return;
            }
            if (groupId) {
                var groupRows = document.querySelectorAll('tr td code');
                var sameGroupCount = 0;
                groupRows.forEach(function (cd) { if ((cd.textContent || '').trim() === groupId) sameGroupCount += 1; });
                if (sameGroupCount > 1) {
                    alert('Split/group transaction cannot be edited directly. Void and recreate from Accounts.');
                    return;
                }
            }
            var txType = btn.getAttribute('data-type') || '';
            var relatedType = btn.getAttribute('data-related-type') || '';
            var relatedId = btn.getAttribute('data-related-id') || '';
            var projectId = btn.getAttribute('data-project') || '';
            var stageId = btn.getAttribute('data-stage') || '';

            if (editTxnId) editTxnId.value = btn.getAttribute('data-id') || '';
            if (editTxnDate) editTxnDate.value = btn.getAttribute('data-date') || '';
            if (editTxnAmount) editTxnAmount.value = btn.getAttribute('data-amount') || '';
            if (editTxnType) editTxnType.value = txType;
            if (editFromAccount) editFromAccount.value = btn.getAttribute('data-from') || '';
            if (editToAccount) editToAccount.value = btn.getAttribute('data-to') || '';
            if (editProject) editProject.value = projectId;
            if (editParty) editParty.value = btn.getAttribute('data-party') || '';
            if (editNote) editNote.value = btn.getAttribute('data-note') || '';
            if (editRef) editRef.value = btn.getAttribute('data-ref') || '';
            if (editRelatedType) editRelatedType.value = relatedType;
            if (editRelatedId) editRelatedId.value = relatedId;
            if (editInfo) editInfo.textContent = 'Editing manual Accounts entry #' + (btn.getAttribute('data-id') || '');
            setSelectedBalanceHint(editFromBalanceHint, editFromAccount, 'Paying Account');
            refreshAccountBalancesRealtime(true);

            editUpdateTxnUI();
            if (projectId) {
                editLoadProjectStages(projectId, stageId).then(function () {
                    if (editStage) editStage.value = stageId || '';
                });
            } else if (editStage) {
                editStage.innerHTML = '<option value=\"\">Select stage</option>';
                editStage.value = '';
            }
            editRefillEntitySelect((relatedType || editRelatedType.value || ''), relatedId || '');
            txnEditModal.show();
        });
    });
})();

// ── Office Expense Category Searchable Combo + Add New ──────────────────────
(function () {
    var input = document.getElementById('office_expense_category');
    var datalist = document.getElementById('officeExpCatList');
    var addBtn = document.getElementById('officeExpCatAddBtn');
    var allCats = [];

    function buildDatalist(cats) {
        if (!datalist) return;
        datalist.innerHTML = '';
        (cats || []).forEach(function (c) {
            var opt = document.createElement('option');
            opt.value = c;
            datalist.appendChild(opt);
        });
    }

    function loadCategories(cb) {
        fetch('/hdc/api/office_expense_categories', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                allCats = data.categories || [];
                buildDatalist(allCats);
                if (cb) cb(allCats);
            })
            .catch(function () {});
    }

    // Load on page start
    loadCategories();

    // Reload when field becomes visible (office_expense target is chosen)
    var wrap = document.getElementById('office_expense_category_wrap');
    if (wrap) {
        var observer = new MutationObserver(function () {
            if (!wrap.classList.contains('d-none')) loadCategories();
        });
        observer.observe(wrap, { attributes: true, attributeFilter: ['class'] });
    }

    // ── Add New Category Modal ──
    if (addBtn) {
        addBtn.addEventListener('click', function () {
            var current = (input ? (input.value || '').trim() : '');
            var newName = window.prompt('Enter new office expense category name:', current || '');
            if (!newName || !newName.trim()) return;
            fetch('/hdc/api/office_expense_categories', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: newName.trim() })
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.ok) {
                    allCats = data.categories || allCats;
                    buildDatalist(allCats);
                    if (input) input.value = data.name || newName.trim();
                    alert('Category "' + (data.name || newName.trim()) + '" saved permanently.');
                } else {
                    alert('Error: ' + (data.error || 'Could not save category.'));
                }
            })
            .catch(function () { alert('Network error saving category.'); });
        });
    }
})();

// Edit-transaction panel — only runs on the edit render, where the server
// fills in the config block's "editTxn" (audit 7.3: this used to be an inline
// script carrying 7 Jinja values).  It has its own IIFE so it can read the
// config itself rather than depend on anything above.
(function () {
    'use strict';
    var cfg;
    try {
        cfg = JSON.parse(document.getElementById('accountsWorkspaceConfig').textContent) || {};
    } catch (e) {
        return;
    }
    var __hdcEdit = cfg.editTxn;
    if (!__hdcEdit) return;

    document.addEventListener('DOMContentLoaded', function () {


        // Make sure the form host is visible (it's hidden by default in create mode)
        var host = document.getElementById('txn_form_host');
        if (host) host.classList.remove('d-none');

        // Set txn_type — its change handler builds the rest of the dynamic form
        var txnTypeEl = document.getElementById('txn_type');
        if (txnTypeEl) {
            txnTypeEl.value = __hdcEdit.type;
            txnTypeEl.dispatchEvent(new Event('change'));
        }
        // Set from_account
        var fromEl = document.getElementById('from_account');
        if (fromEl) { fromEl.value = __hdcEdit.fromAccountId; fromEl.dispatchEvent(new Event('change')); }
        // Set to_account
        var toEl = document.getElementById('to_account');
        if (toEl) { toEl.value = __hdcEdit.toAccountId; toEl.dispatchEvent(new Event('change')); }
        // Set project (then trigger stage dropdown rebuild, then set stage)
        var projEl = document.getElementById('project_id');
        if (projEl) {
            projEl.value = __hdcEdit.projectId;
            projEl.dispatchEvent(new Event('change'));
        }
        // Stage and related-entity values may need a tick after project change rebuilds them
        setTimeout(function() {
            var stageEl = document.getElementById('stage_id');
            if (stageEl) stageEl.value = __hdcEdit.stageId;
            var relTypeEl = document.getElementById('related_entity_type');
            if (relTypeEl) relTypeEl.value = __hdcEdit.relatedType;
            var relIdEl = document.getElementById('related_entity_id');
            if (relIdEl) relIdEl.value = __hdcEdit.relatedId;
        }, 80);

        // Scroll the edit form into view so the user actually sees it
        setTimeout(function() {
            if (host && typeof host.scrollIntoView === 'function') {
                host.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }, 120);
    });
})();
