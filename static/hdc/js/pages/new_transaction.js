/* HDC ERP — pages/new_transaction.js
 *
 * The New Transaction controller.  One file drives both entry surfaces:
 *   * templates/hdc/accounts/new_transaction.html   (the focused page)
 *   * templates/hdc/accounts/cashflow_register.html (the register's entry card)
 *
 * It is scoped to a `[data-hdc-txn-form]` element, so a page may render the
 * form once (today) and the script still works if it ever renders it twice.
 *
 * What it does, in the order the rules matter:
 *   1. Direction is the first decision.  Until one is chosen the rest of the
 *      form is hidden; the fields that appear are only the ones that direction
 *      genuinely needs, so a transfer never asks for a category.
 *   2. Category → Subcategory is a real dependency read from the options the
 *      server rendered (each subcategory carries its parent's id).  Changing the
 *      category clears a subcategory that is no longer valid.  The same options
 *      carry the category's *field rules* (party / project: none | optional |
 *      required, plus the party types it is for), so the category also decides
 *      which of those fields exist on screen and which parties are offered.

 *   3. Account / Party / Project are searchable comboboxes built on the shared
 *      HDCComboList widget, each with "+ Add New …" that opens a small modal,
 *      saves over JSON, refreshes the picker and selects the new row — without
 *      reloading, so nothing typed is ever lost.
 *   4. Validation runs before submit and puts the message next to the field.
 *      The server validates the same rules again and is authoritative.
 *
 * No inline logic in the templates; this file is the behaviour.
 */
(function () {
    'use strict';

    var DIRECTION_IN = 'in';
    var DIRECTION_OUT = 'out';
    var DIRECTION_TRANSFER = 'transfer';

    /* Which add-new endpoint backs which picker. */
    var ADD_ENDPOINTS = {
        account: '/hdc/accounts/new-transaction/account',
        party: '/hdc/accounts/new-transaction/party',
        project: '/hdc/accounts/new-transaction/project'
    };

    function byId(id) { return document.getElementById(id); }

    /* ── money parsing (mirrors hdc/utils/money.to_minor) ─────────────────── */
    function parseAmount(raw) {
        var text = String(raw == null ? '' : raw).trim();
        if (!text) return { ok: false, empty: true };
        var negative = false;
        var body = text;
        var paren = /^\((.*)\)$/.exec(body);
        if (paren) { negative = true; body = paren[1]; }
        body = body.replace(/(rs|pkr|₨)\.?/gi, '').replace(/[\s,]/g, '');
        if (body.charAt(0) === '-') { negative = true; body = body.slice(1); }
        if (body.charAt(0) === '+') { body = body.slice(1); }
        if (!/^\d*(\.\d*)?$/.test(body) || !/\d/.test(body)) return { ok: false };
        var value = Math.round(parseFloat(body) * 100);
        if (!isFinite(value)) return { ok: false };
        if (negative) value = -value;
        return { ok: true, minor: value };
    }

    function formatAmount(minor) {
        var negative = minor < 0;
        var abs = Math.abs(minor);
        var whole = String(Math.floor(abs / 100));
        var cents = abs % 100;
        var grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
        return (negative ? '-' : '') + grouped + (cents ? '.' + (cents < 10 ? '0' + cents : cents) : '');
    }

    function fetchJson(url, payload) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(payload || {})
        }).then(function (response) {
            return response.json().catch(function () { return { ok: false, message: 'Unexpected server response.' }; })
                .then(function (data) {
                    if (!response.ok || data.ok === false) {
                        throw new Error(data && data.message ? data.message : 'Request failed.');
                    }
                    return data;
                });
        });
    }

    /* ── inline field errors ─────────────────────────────────────────────── */
    function errorNode(form, field) {
        return form.querySelector('[data-error-for="' + field + '"]');
    }

    function setFieldError(form, field, message, control) {
        var node = errorNode(form, field);
        if (node) {
            node.textContent = message || '';
            node.hidden = !message;
        }
        var el = control || form.querySelector('[name="' + field + '"]');
        if (!el) return;
        if (message) el.setAttribute('aria-invalid', 'true');
        else el.removeAttribute('aria-invalid');
    }

    function clearErrors(form) {
        form.querySelectorAll('.hdc-field-error').forEach(function (node) {
            node.textContent = '';
            node.hidden = true;
        });
        form.querySelectorAll('[aria-invalid]').forEach(function (el) {
            el.removeAttribute('aria-invalid');
        });
    }

    /* ── toast-style feedback for the add-new modals ─────────────────────── */
    function flashPickerMessage(form, text) {
        var host = form.querySelector('#txnActionHint');
        if (!host) return;
        host.textContent = text;
        host.classList.add('hdc-txn-actions-note-flash');
        window.setTimeout(function () {
            host.classList.remove('hdc-txn-actions-note-flash');
        }, 4000);
    }

    function initForm(form) {
        var directionSelect = byId('txnDirection');
        if (!directionSelect) return;

        var directionGroup = byId('txnDirectionGroup');
        var body = byId('txnBody');
        var accountField = byId('txnAccountField');
        var accountLabel = byId('txnAccountLabel');
        var accountInput = byId('txnAccountInput');
        var accountSelect = byId('txnAccount');
        var toField = byId('txnToField');
        var toInput = byId('txnToAccountInput');
        var toSelect = byId('txnToAccount');
        var categoryBlock = byId('txnCategoryBlock');
        var categoryLegend = byId('txnCategoryLegend');
        var categorySelect = byId('txnCategory');
        var subcategorySelect = byId('txnSubcategory');
        var subcategoryHint = byId('txnSubcategoryHint');
        var whoBlock = byId('txnWhoBlock');
        var partyField = byId('txnPartyField');
        var partySelect = byId('txnParty');
        var partyInput = byId('txnPartyInput');
        var partyTypeField = byId('txnPartyType');
        var partyReq = byId('txnPartyReq');
        var partyHint = byId('txnPartyHint');
        var projectField = byId('txnProjectField');
        var projectSelect = byId('txnProject');
        var projectInput = byId('txnProjectInput');
        var projectReq = byId('txnProjectReq');
        var projectHint = byId('txnProjectHint');
        var rulesHelp = byId('txnRulesHelp');
        var amountInput = byId('txnAmount');
        var dateInput = byId('txnDate');
        var saveButton = byId('txnSaveBtn');
        var resetButton = byId('txnResetBtn');
        var actionHint = byId('txnActionHint');
        var amountLegend = byId('txnAmountLegend');

        var combos = {};
        var pendingAdd = null;   /* 'account' | 'party' | 'project' */

        /* ── 1. direction ────────────────────────────────────────────────── */

        function currentDirection() {
            return (directionSelect.value || '').trim();
        }

        function setDirection(value, opts) {
            var options = opts || {};
            directionSelect.value = value || '';
            if (options.silent) return;
            // Programmatic change is a real change: it must fire so anything
            // listening (including the combo's own mirrors) stays in step.
            directionSelect.dispatchEvent(new Event('change', { bubbles: true }));
        }

        function paintDirectionButtons() {
            if (!directionGroup) return;
            var value = currentDirection();
            Array.prototype.forEach.call(
                directionGroup.querySelectorAll('.hdc-dir-btn'),
                function (btn) {
                    var on = btn.getAttribute('data-direction') === value;
                    btn.classList.toggle('active', on);
                    btn.setAttribute('aria-checked', on ? 'true' : 'false');
                    btn.setAttribute('tabindex', on || (!value && btn === directionGroup.firstElementChild) ? '0' : '-1');
                });
        }

        function applyDirection() {
            var direction = currentDirection();
            var isTransfer = direction === DIRECTION_TRANSFER;
            var knowsDirection = direction === DIRECTION_IN || direction === DIRECTION_OUT || isTransfer;

            paintDirectionButtons();

            /* Progressive reveal: no direction yet → one decision only. */
            if (body) body.hidden = !knowsDirection;
            if (!knowsDirection) {
                if (actionHint) actionHint.textContent = 'Choose a direction to start.';
                if (resetButton) resetButton.hidden = true;
                return;
            }
            if (resetButton) resetButton.hidden = false;

            /* Account vs From/To */
            if (accountField) accountField.hidden = false;
            if (accountLabel) {
                accountLabel.innerHTML = (isTransfer ? 'From account' : 'Account') +
                    ' <span class="hdc-req" aria-hidden="true">*</span>';
            }
            if (accountInput) {
                accountInput.placeholder = isTransfer
                    ? 'Search the account money leaves…'
                    : 'Search account… (e.g. MCB, cash)';
            }
            if (accountSelect) accountSelect.required = true;
            if (toField) toField.hidden = !isTransfer;
            if (toSelect) toSelect.disabled = !isTransfer;
            if (isTransfer && toInput) toInput.required = true;
            else if (toInput) toInput.required = false;

            /* Category / party / project are money-in/out concepts only. */
            if (categoryBlock) categoryBlock.hidden = isTransfer;
            if (whoBlock) whoBlock.hidden = isTransfer;
            if (categorySelect) categorySelect.disabled = isTransfer;
            if (subcategorySelect) subcategorySelect.disabled = isTransfer;
            if (partySelect) partySelect.disabled = isTransfer;
            if (partyInput) partyInput.disabled = isTransfer;
            if (projectSelect) projectSelect.disabled = isTransfer;
            if (projectInput) projectInput.disabled = isTransfer;
            if (isTransfer) {
                /* Nothing below applies to a transfer; hide rather than ask. */
                setFieldVisible('party', false);
                setFieldVisible('project', false);
            }

            if (amountLegend) {
                amountLegend.textContent = isTransfer
                    ? 'Amount and the two accounts'
                    : 'Amount and account';
            }
            if (categoryLegend) {
                categoryLegend.textContent = direction === DIRECTION_IN ? 'Income category' : 'Expense category';
            }
            if (actionHint) {
                actionHint.textContent = isTransfer
                    ? 'Money moves between two of our own accounts — no category or party is needed.'
                    : 'Category drives the subcategory list. Party and project are optional.';
            }

            filterCategories();
            applyCategoryRules();
        }

        /* ── 2. category → subcategory ───────────────────────────────────── */

        function filterCategories() {
            if (!categorySelect) return;
            var direction = currentDirection();
            Array.prototype.forEach.call(categorySelect.options, function (option) {
                if (!option.value) return;
                var allowed = (option.getAttribute('data-direction') || 'both').toLowerCase();
                var keep = allowed === 'both' || allowed === direction;
                option.hidden = !keep;
                option.disabled = !keep;
            });
            /* A category that no longer applies must not stay selected. */
            var selected = categorySelect.options[categorySelect.selectedIndex];
            if (selected && selected.value && (selected.hidden || selected.disabled)) {
                categorySelect.value = '';
                setFieldError(form, 'category_id', '');
            }
            filterSubcategories();
        }

        /* ── 2b. the category's field rules ───────────────────────────────
           A category says whether a party / project belongs to it at all
           ("none" hides the field outright), whether it is mandatory, and
           which party types it is for.  Everything below reads those rules
           from the option the server rendered — the form never decides for
           itself which kind of transaction needs what. */

        function selectedCategoryOption() {
            if (!categorySelect || categorySelect.selectedIndex < 0) return null;
            var option = categorySelect.options[categorySelect.selectedIndex];
            return (option && option.value) ? option : null;
        }

        function categoryRule(name, fallback) {
            var option = selectedCategoryOption();
            if (!option) return fallback;
            var value = (option.getAttribute('data-' + name) || '').trim().toLowerCase();
            return value || fallback;
        }

        function allowedPartyTypes() {
            var raw = categoryRule('party-types', '');
            if (!raw) return [];
            return raw.split(',').map(function (part) { return part.trim(); }).filter(Boolean);
        }

        function setFieldVisible(kind, visible) {
            var field = kind === 'party' ? partyField : projectField;
            var control = kind === 'party' ? partySelect : projectSelect;
            var input = kind === 'party' ? partyInput : projectInput;
            var hint = kind === 'party' ? partyHint : projectHint;
            if (field) field.hidden = !visible;
            /* Disabled controls are not submitted: a hidden field must not
               smuggle a stale value into the entry. */
            if (control) control.disabled = !visible || currentDirection() === DIRECTION_TRANSFER;
            if (input) {
                input.disabled = !visible || currentDirection() === DIRECTION_TRANSFER;
                input.required = false;
            }
            if (!visible) {
                setFieldError(form, kind === 'party' ? 'party_name' : 'project_id', '');
                if (hint) { hint.hidden = true; hint.textContent = ''; }
            }
        }

        function setRequiredMark(node, required) {
            if (node) node.hidden = !required;
        }

        function setFieldHint(node, text) {
            if (!node) return;
            if (text) {
                node.textContent = text;
                node.hidden = false;
            } else {
                node.textContent = '';
                node.hidden = true;
            }
        }

        function partyTypeLabel(value) {
            var labels = {
                client: 'Client / Owner',
                supplier: 'Supplier / Vendor',
                worker: 'Worker / Labour',
                staff: 'Office Staff',
                subcontractor: 'Subcontractor',
                lender: 'Loan Giver / Financier',
                borrower: 'Loan Taker / Borrower',
                other: 'Other'
            };
            return labels[value] || value;
        }

        /* Hide the parties the chosen category is not for — the list shortens
           itself to the people who make sense, and "other" always stays. */
        function filterPartyTypes(allowed) {
            if (!partySelect) return;
            var selectedOption = partySelect.options[partySelect.selectedIndex];
            Array.prototype.forEach.call(partySelect.options, function (option) {
                if (!option.value) return;
                var type = (option.getAttribute('data-party-type') || 'other').toLowerCase();
                var keep = !allowed.length || allowed.indexOf(type) !== -1 || type === 'other';
                if (option.style) option.style.display = keep ? '' : 'none';
                option.disabled = !keep;
            });
            if (selectedOption && selectedOption.value && selectedOption.disabled) {
                partySelect.value = '';
                if (partyInput) partyInput.value = '';
                if (partyTypeField) partyTypeField.value = 'other';
            }
        }

        function applyCategoryRules() {
            if (currentDirection() === DIRECTION_TRANSFER || !currentDirection()) {
                setFieldVisible('party', false);
                setFieldVisible('project', false);
                setRequiredMark(partyReq, false);
                setRequiredMark(projectReq, false);
                if (rulesHelp) { rulesHelp.textContent = ''; rulesHelp.hidden = true; }
                return;
            }
            var hasCategory = !!(categorySelect && categorySelect.value);
            var partyMode = hasCategory ? categoryRule('party-mode', 'optional') : 'none';
            var projectMode = hasCategory ? categoryRule('project-mode', 'optional') : 'none';
            var allowed = hasCategory ? allowedPartyTypes() : [];
            var loanEffect = hasCategory ? categoryRule('loan-effect', '') : '';

            var showParty = partyMode !== 'none';
            var showProject = projectMode !== 'none';
            setFieldVisible('party', showParty);
            setFieldVisible('project', showProject);

            var partyRequired = showParty && partyMode === 'required';
            var projectRequired = showProject && projectMode === 'required';
            if (partySelect) partySelect.required = partyRequired;
            if (projectSelect) projectSelect.required = projectRequired;
            setRequiredMark(partyReq, partyRequired);
            setRequiredMark(projectReq, projectRequired);

            if (showParty && allowed.length) {
                filterPartyTypes(allowed);
                var names = allowed.map(partyTypeLabel).join(' / ');
                setFieldHint(partyHint, partyRequired
                    ? 'Required for this category — pick the ' + names + '.'
                    : 'For this category the party is usually the ' + names + '.');
            } else {
                filterPartyTypes([]);
                setFieldHint(partyHint, '');
            }
            setFieldHint(projectHint, projectRequired
                ? 'Required for this category — the cost or receipt must land on a project.'
                : '');
            if (rulesHelp) {
                var explained = [];
                if (loanEffect === 'take') explained.push('money received as a loan — the person is a Loan Giver');
                if (loanEffect === 'give') explained.push('money given as a loan — the person is a Loan Taker');
                if (loanEffect === 'repay') explained.push('repaying a loan we took');
                if (loanEffect === 'recover') explained.push('a borrower paying us back');
                if (explained.length) {
                    rulesHelp.textContent = 'Loan ledger: ' + explained.join('; ') +
                        '. The amount is tracked against that person’s loan (Accounts → Loans).';
                    rulesHelp.hidden = false;
                } else {
                    rulesHelp.textContent = '';
                    rulesHelp.hidden = true;
                }
            }
        }

        function filterSubcategories() {
            if (!subcategorySelect) return;
            var categoryId = categorySelect ? categorySelect.value : '';
            var visible = 0;
            Array.prototype.forEach.call(subcategorySelect.options, function (option) {
                if (!option.value) return;
                var owns = !!categoryId && option.getAttribute('data-category') === categoryId;
                option.hidden = !owns;
                option.disabled = !owns;
                if (owns) visible += 1;
            });
            /* The dependency, enforced on the control itself: a subcategory
               that does not belong to the chosen category can never be sent. */
            var selected = subcategorySelect.options[subcategorySelect.selectedIndex];
            if (selected && selected.value && (selected.hidden || selected.disabled)) {
                subcategorySelect.value = '';
            }
            if (subcategoryHint) {
                if (!categoryId) {
                    subcategoryHint.textContent = 'Pick a category first.';
                    subcategoryHint.hidden = false;
                } else if (!visible) {
                    subcategoryHint.textContent = 'No subcategories for this category yet.';
                    subcategoryHint.hidden = false;
                } else {
                    subcategoryHint.textContent = '';
                    subcategoryHint.hidden = true;
                }
            }
            setFieldError(form, 'subcategory_id', '');
        }

        /* ── 3. account / party / project pickers ────────────────────────── */

        function accountLabelFor(account) {
            return account.name + ' (' + (account.type === 'bank' ? 'Bank' : 'Cash') + ')';
        }

        function addOption(select, value, label, attrs) {
            if (!select) return null;
            var existing = Array.prototype.find.call(select.options, function (option) {
                return option.value === String(value);
            });
            if (existing) return existing;
            var option = document.createElement('option');
            option.value = String(value);
            option.text = label;
            Object.keys(attrs || {}).forEach(function (key) {
                option.setAttribute(key, attrs[key]);
            });
            select.appendChild(option);
            return option;
        }

        function selectValue(select, value) {
            if (!select) return;
            select.value = String(value);
            select.dispatchEvent(new Event('change', { bubbles: true }));
        }

        function attachCombo(inputId, selectId, comboOptions) {
            if (!window.HDCComboList) return null;
            var input = byId(inputId);
            var select = byId(selectId);
            if (!input || !select) return null;
            var combo = window.HDCComboList.attach(input, select, comboOptions);
            if (combo && comboOptions && comboOptions.onAdd) {
                input.addEventListener('focus', function () {
                    input.setAttribute('aria-expanded', 'true');
                });
                input.addEventListener('blur', function () {
                    input.setAttribute('aria-expanded', 'false');
                });
            }
            return combo;
        }

        function ensureCombos() {
            if (!window.HDCComboList) return;
            if (!combos.account && accountInput && accountSelect) {
                combos.account = attachCombo('txnAccountInput', 'txnAccount', {
                    strict: true, maxItems: 50,
                    emptyText: 'No accounts found.',
                    addLabel: '+ Add New Account',
                    onAdd: function () { openAddModal('account'); }
                });
            }
            if (!combos.toAccount && toInput && toSelect) {
                combos.toAccount = attachCombo('txnToAccountInput', 'txnToAccount', {
                    strict: true, maxItems: 50, includeEmpty: true,
                    emptyText: 'No accounts found.',
                    addLabel: '+ Add New Account',
                    onAdd: function () { openAddModal('account', 'to'); }
                });
            }
            if (!combos.party && partyInput && partySelect) {
                combos.party = attachCombo('txnPartyInput', 'txnParty', {
                    strict: true, maxItems: 50, includeEmpty: true,
                    emptyText: 'No parties found.',
                    addLabel: '+ Add New Party',
                    onAdd: function () { openAddModal('party'); }
                });
            }
            if (!combos.project && projectInput && projectSelect) {
                combos.project = attachCombo('txnProjectInput', 'txnProject', {
                    strict: true, maxItems: 50, includeEmpty: true,
                    emptyText: 'No projects found.',
                    addLabel: '+ Add New Project',
                    onAdd: function () { openAddModal('project'); }
                });
            }
        }

        /* ── 4. "+ Add New …" modals ─────────────────────────────────────── */

        var MODAL_IDS = {
            account: 'txnNewAccountModal',
            party: 'txnNewPartyModal',
            project: 'txnNewProjectModal'
        };
        var MODAL_FORMS = {
            account: 'txnNewAccountForm',
            party: 'txnNewPartyForm',
            project: 'txnNewProjectForm'
        };

        function openAddModal(kind, target) {
            pendingAdd = { kind: kind, target: target || 'primary' };
            var modalElement = byId(MODAL_IDS[kind]);
            var modalForm = byId(MODAL_FORMS[kind]);
            if (modalForm) {
                modalForm.reset();
                var error = modalForm.querySelector('[data-modal-error]');
                if (error) { error.textContent = ''; error.hidden = true; }
                var busy = modalForm.querySelector('.hdc-save-busy');
                var idle = modalForm.querySelector('.hdc-save-idle');
                if (busy) busy.hidden = true;
                if (idle) idle.hidden = false;
                modalForm.dataset.submitted = '0';
            }
            if (kind === 'account') syncBankFields();
            if (kind === 'party') prefillPartyFromQuery();
            if (kind === 'project') prefillProjectFromQuery();

            if (modalElement && window.bootstrap && window.bootstrap.Modal) {
                window.bootstrap.Modal.getOrCreateInstance(modalElement).show();
            } else if (modalElement) {
                modalElement.classList.add('show');
                modalElement.style.display = 'block';
            }
        }

        /* Whatever the user had already typed is the best guess for the new name. */
        function prefillPartyFromQuery() {
            var nameInput = byId('txnNpName');
            if (!nameInput) return;
            nameInput.value = partyInput ? partyInput.value.trim() : '';
            var select = byId('txnNpType');
            if (select) select.value = currentDirection() === DIRECTION_IN ? 'client' : 'supplier';
        }

        function prefillProjectFromQuery() {
            var nameInput = byId('txnNprName');
            if (nameInput) nameInput.value = projectInput ? projectInput.value.trim() : '';
        }

        function syncBankFields() {
            var mode = byId('txnNaMode');
            var block = byId('txnNaBankFields');
            if (!mode || !block) return;
            var isBank = mode.value === 'bank';
            block.hidden = !isBank;
            var bankName = byId('txnNaBankName');
            var bankNumber = byId('txnNaBankNumber');
            if (bankName) bankName.required = isBank;
            if (bankNumber) bankNumber.required = isBank;
        }

        function modalError(modalForm, message) {
            var node = modalForm.querySelector('[data-modal-error]');
            if (!node) return;
            node.textContent = message || '';
            node.hidden = !message;
        }

        function modalBusy(modalForm, busy) {
            var busyNode = modalForm.querySelector('.hdc-save-busy');
            var idleNode = modalForm.querySelector('.hdc-save-idle');
            if (busyNode) busyNode.hidden = !busy;
            if (idleNode) idleNode.hidden = !!busy;
            var submit = modalForm.querySelector('button[type="submit"]');
            if (submit) submit.disabled = !!busy;
        }

        function closeModal(kind) {
            var modalElement = byId(MODAL_IDS[kind]);
            if (modalElement && window.bootstrap && window.bootstrap.Modal) {
                var instance = window.bootstrap.Modal.getInstance(modalElement);
                if (instance) instance.hide();
            } else if (modalElement) {
                modalElement.classList.remove('show');
                modalElement.style.display = 'none';
            }
        }

        function payloadFor(kind) {
            if (kind === 'account') {
                return {
                    name: valueOf('txnNaName'),
                    mode: valueOf('txnNaMode') || 'cash',
                    bank_name: valueOf('txnNaBankName'),
                    account_number: valueOf('txnNaBankNumber'),
                    opening_balance: valueOf('txnNaOpening') || 0
                };
            }
            if (kind === 'party') {
                return {
                    name: valueOf('txnNpName'),
                    party_type: valueOf('txnNpType') || 'other',
                    phone: valueOf('txnNpPhone')
                };
            }
            return {
                name: valueOf('txnNprName'),
                client: valueOf('txnNprClient'),
                location: valueOf('txnNprLocation')
            };
        }

        function valueOf(id) {
            var el = byId(id);
            return el ? String(el.value || '').trim() : '';
        }

        /* After a create: refresh the picker, select the new row, keep focus in
           the transaction form.  Nothing here touches any other field. */
        function selectCreated(kind, item) {
            var target = pendingAdd && pendingAdd.target === 'to' ? 'to' : 'primary';
            if (kind === 'account') {
                var attrs = {};
                var select = target === 'to' ? toSelect : accountSelect;
                var input = target === 'to' ? toInput : accountInput;
                addOption(select, item.id, accountLabelFor(item), attrs);
                ensureCombos();
                var combo = target === 'to' ? combos.toAccount : combos.account;
                selectValue(select, item.id);
                if (combo && combo.syncFromSelect) combo.syncFromSelect();
                else if (input) input.value = accountLabelFor(item);
                if (input) input.focus();
            } else if (kind === 'party') {
                addOption(partySelect, item.name, item.name, { 'data-party-type': item.party_type || 'other' });
                ensureCombos();
                selectValue(partySelect, item.name);
                if (combos.party && combos.party.syncFromSelect) combos.party.syncFromSelect();
                if (partyTypeField) partyTypeField.value = item.party_type || 'other';
                if (partyInput) partyInput.focus();
            } else {
                addOption(projectSelect, item.id, item.name, {});
                ensureCombos();
                selectValue(projectSelect, item.id);
                if (combos.project && combos.project.syncFromSelect) combos.project.syncFromSelect();
                if (projectInput) projectInput.focus();
            }
        }

        function wireModal(kind) {
            var modalForm = byId(MODAL_FORMS[kind]);
            if (!modalForm) return;
            modalForm.addEventListener('submit', function (event) {
                event.preventDefault();
                if (modalForm.dataset.submitted === '1') return;
                var payload = payloadFor(kind);
                if (!payload.name) {
                    modalError(modalForm, kind === 'account' ? 'Account name is required.'
                        : (kind === 'party' ? 'Party name is required.' : 'Project name is required.'));
                    var first = modalForm.querySelector('input');
                    if (first) first.focus();
                    return;
                }
                modalForm.dataset.submitted = '1';
                modalBusy(modalForm, true);
                modalError(modalForm, '');
                fetchJson(ADD_ENDPOINTS[kind], payload).then(function (data) {
                    modalBusy(modalForm, false);
                    modalForm.dataset.submitted = '0';
                    closeModal(kind);
                    selectCreated(kind, data.item || {});
                    flashPickerMessage(form, data.message || 'Saved and selected.');
                    pendingAdd = null;
                }).catch(function (error) {
                    modalBusy(modalForm, false);
                    modalForm.dataset.submitted = '0';
                    modalError(modalForm, error.message || 'Could not save — please try again.');
                });
            });

            /* Escape / backdrop dismiss must not leave a half-open state. */
            var modalElement = byId(MODAL_IDS[kind]);
            if (modalElement) {
                modalElement.addEventListener('hidden.bs.modal', function () {
                    modalForm.dataset.submitted = '0';
                    pendingAdd = null;
                });
            }
        }

        /* ── 5. validation + submit ──────────────────────────────────────── */

        function validate() {
            clearErrors(form);
            var firstBad = null;
            function fail(field, message, control) {
                setFieldError(form, field, message);
                if (!firstBad) firstBad = control || form.querySelector('[name="' + field + '"]');
            }

            var direction = currentDirection();
            if (!direction) {
                fail('direction', 'Choose Money In, Money Out or Internal Transfer.', directionSelect);
            }

            if (!dateInput || !dateInput.value) fail('date', 'Date is required.', dateInput);

            var amount = parseAmount(amountInput ? amountInput.value : '');
            if (!amount.ok) {
                fail('amount', amount.empty ? 'Amount is required.' : 'Amount must be a valid number.', amountInput);
            } else if (amount.minor <= 0) {
                fail('amount', 'Amount must be greater than 0.', amountInput);
            }

            var accountId = accountSelect ? accountSelect.value : '';
            if (!accountId) {
                fail('account_id', direction === DIRECTION_TRANSFER
                    ? 'Choose the account the money leaves.' : 'Choose the account.', accountInput);
            }

            if (direction === DIRECTION_TRANSFER) {
                var toId = toSelect ? toSelect.value : '';
                if (!toId) fail('destination_account_id', 'Choose the account the money goes to.', toInput);
                else if (toId === accountId) {
                    fail('destination_account_id', 'From and To must be different accounts.', toInput);
                }
            } else if (direction) {
                if (categorySelect && !categorySelect.value) {
                    fail('category_id',
                        direction === DIRECTION_IN ? 'Choose an income category.' : 'Choose an expense category.',
                        categorySelect);
                }
                var sub = subcategorySelect && subcategorySelect.value
                    ? subcategorySelect.options[subcategorySelect.selectedIndex] : null;
                if (sub && categorySelect && sub.getAttribute('data-category') !== categorySelect.value) {
                    /* Belt and braces: the picker clears this itself, but a
                       hand-edited DOM must not be able to smuggle it through. */
                    subcategorySelect.value = '';
                    fail('subcategory_id', 'That subcategory does not belong to the chosen category.',
                        subcategorySelect);
                }
                /* The category decides whether a party / project is required;
                   the server checks the same rules again. */
                var option = selectedCategoryOption();
                if (option) {
                    if (option.getAttribute('data-project-mode') === 'required'
                        && (!projectSelect || !projectSelect.value)) {
                        fail('project_id', 'This category needs a project — pick one.', projectInput);
                    }
                    if (option.getAttribute('data-party-mode') === 'required'
                        && (!partySelect || !partySelect.value)) {
                        fail('party_name', 'This category needs a party — choose who it is for.',
                            partyInput);
                    }
                }
            }

            if (firstBad && firstBad.focus) firstBad.focus();
            if (firstBad && firstBad.scrollIntoView) {
                firstBad.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }
            return !firstBad;
        }

        function submitState(busy) {
            if (!saveButton) return;
            var idle = saveButton.querySelector('.hdc-save-idle');
            var spin = saveButton.querySelector('.hdc-save-busy');
            if (spin) spin.hidden = !busy;
            if (idle) idle.hidden = !!busy;
            saveButton.disabled = !!busy;
            saveButton.setAttribute('aria-busy', busy ? 'true' : 'false');
        }

        /* ── wiring ──────────────────────────────────────────────────────── */

        if (directionGroup) {
            directionGroup.hidden = false;
            /* The buttons are an enhancement; the select is the control. */
            directionSelect.classList.add('hdc-txn-native-hidden');
            directionGroup.addEventListener('click', function (event) {
                var button = event.target.closest('.hdc-dir-btn');
                if (!button) return;
                setDirection(button.getAttribute('data-direction'));
            });
            directionGroup.addEventListener('keydown', function (event) {
                var keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
                if (!(event.key in keys)) return;
                event.preventDefault();
                var buttons = Array.prototype.slice.call(directionGroup.querySelectorAll('.hdc-dir-btn'));
                var index = buttons.indexOf(document.activeElement);
                if (index === -1) index = 0;
                var next = (index + keys[event.key] + buttons.length) % buttons.length;
                buttons[next].focus();
                setDirection(buttons[next].getAttribute('data-direction'));
            });
        }

        directionSelect.addEventListener('change', applyDirection);
        if (categorySelect) {
            categorySelect.addEventListener('change', function () {
                filterSubcategories();
                applyCategoryRules();
            });
        }
        if (amountInput) {
            amountInput.addEventListener('blur', function () {
                var parsed = parseAmount(amountInput.value);
                if (parsed.ok && parsed.minor > 0) amountInput.value = formatAmount(parsed.minor);
            });
        }
        if (toSelect) {
            toSelect.addEventListener('change', function () {
                if (toSelect.value && toSelect.value === accountSelect.value && accountInput) {
                    setFieldError(form, 'destination_account_id',
                        'From and To must be different accounts.', toInput);
                } else {
                    setFieldError(form, 'destination_account_id', '');
                }
            });
        }
        if (resetButton) {
            resetButton.addEventListener('click', function () {
                form.reset();
                clearErrors(form);
                setDirection('', { silent: true });
                ensureCombos();
                if (combos.account && combos.account.syncFromSelect) combos.account.syncFromSelect();
                if (combos.toAccount && combos.toAccount.syncFromSelect) combos.toAccount.syncFromSelect();
                if (combos.party && combos.party.syncFromSelect) combos.party.syncFromSelect();
                if (combos.project && combos.project.syncFromSelect) combos.project.syncFromSelect();
                applyDirection();
                applyCategoryRules();
                directionSelect.focus();
            });
        }

        form.addEventListener('submit', function (event) {
            event.preventDefault();
            if (!validate()) {
                flashPickerMessage(form, 'Some details need fixing — see the highlighted fields.');
                return;
            }
            submitState(true);
            /* Let the validation pass above own the "did the user mean this?"
               decision; the submit that follows is the real one. */
            form.submit();
        });

        /* A server-side rejection: open the section that caused it and put the
           focus there, so the message is never off-screen. */
        (function focusServerError() {
            var host = byId('txnServerError');
            if (!host) return;
            setDirection(currentDirection(), { silent: false });
            if (body) body.hidden = false;
            host.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }());

        ['account', 'party', 'project'].forEach(wireModal);
        var bankMode = byId('txnNaMode');
        if (bankMode) bankMode.addEventListener('change', syncBankFields);

        ensureCombos();
        applyDirection();
        applyCategoryRules();
    }

    function boot() {
        document.querySelectorAll('[data-hdc-txn-form]').forEach(initForm);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
