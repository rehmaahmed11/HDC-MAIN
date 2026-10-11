/* HDC Tool Transaction — the one tool transaction form.
 *
 * Reads its data from <script id="toolTxnConfig" type="application/json">
 * (rendered by hdc/routes/tool_rental.py from
 * hdc/services/tool_txn_entry.py) and does four jobs:
 *
 *   1. show the three groups (Rent Out / Return / Maintenance) and, under the
 *      chosen one, its transactions — then show only the panels the chosen
 *      transaction declares, and disable (not just hide) everything else, so a
 *      hidden panel can never post.  Renting out is asked step by step: from
 *      where (store / site / customer) and to whom pick the transaction, then
 *      the tools, the customer / site and the rent type follow in page order;
 *   2. build/validate the multi-item lines — a searchable tick list of every
 *      issuable tool, with the ticked tools in excel-style rows (tool,
 *      pieces, qty, rate) — against the store's real availability, not a
 *      guess, including per-piece (serial) selection;
 *   3. copy the one visible transaction date into the endpoint's own date
 *      field, and stamp the reference / responsible person onto the notes, so
 *      the record that gets created carries the number on the printed form;
 *   4. lock the submit button on the first submit and confirm consequential
 *      transactions, so a double click cannot post twice (the server also
 *      refuses a reference it has already recorded).
 *
 * There is no per-transaction branch here.  Every transaction arrives with its
 * own contract — its panels, the ``checks`` to run before posting, the
 * ``picker`` it chooses a rental from — and this script looks each name up in
 * the CHECKS / COLLECTORS tables below.  Adding a transaction is adding data,
 * not code.  No business rule lives here either: quantities, availability,
 * money and locations are all re-checked by the server routes this form posts
 * to.
 */
(function () {
    'use strict';

    var CONFIG = {};
    var configNode = document.getElementById('toolTxnConfig');
    if (configNode) {
        try { CONFIG = JSON.parse(configNode.textContent || '{}'); } catch (err) { CONFIG = {}; }
    }

    var GROUPS = CONFIG.groups || [];
    var TYPES = [];
    GROUPS.forEach(function (group) {
        (group.options || []).forEach(function (option) {
            option.group = option.group || group.key;
            TYPES.push(option);
        });
    });
    var TOOLS = CONFIG.tools || [];
    var SOURCES = CONFIG.transferSources || [];
    var BILLING = {};
    (CONFIG.billingModes || []).forEach(function (mode) { BILLING[mode.key] = mode; });

    function moneyStatus(type, amount) {
        var mode = BILLING[type];
        if (mode && !mode.charges_rent) return mode.status;
        return 'Rent charged — ' + Number(amount || 0).toFixed(2) + ' PKR currently due.';
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
        });
    }
    var MAINTENANCE_PIECES = CONFIG.maintenancePieces || [];
    var TOOLS_BY_ID = {};
    TOOLS.forEach(function (tool) { TOOLS_BY_ID[String(tool.id)] = tool; });

    var form = document.getElementById('toolTxnForm');
    if (!form) return;

    var typeSelect = document.getElementById('txnType');
    var errorBox = document.getElementById('txnFormError');
    var submitButton = document.getElementById('txnSubmitButton');
    var submitLabel = document.getElementById('txnSubmitLabel');
    var submitted = false;
    var lineIndex = 0;
    var activeGroup = GROUPS.length ? GROUPS[0].key : '';

    function entryFor(key) {
        for (var i = 0; i < TYPES.length; i++) {
            if (TYPES[i].key === key) return TYPES[i];
        }
        return null;
    }

    function currentEntry() {
        return entryFor(typeSelect ? typeSelect.value : '');
    }

    function showError(message) {
        if (!errorBox) return;
        errorBox.textContent = message || '';
        errorBox.classList.toggle('d-none', !message);
    }

    function setEnabled(root, enabled) {
        if (!root) return;
        Array.prototype.forEach.call(root.querySelectorAll('input, select, textarea, button'), function (control) {
            if (control === submitButton || control === document.getElementById('txnResetButton')) return;
            control.disabled = !enabled;
        });
    }

    /* ── the three groups ───────────────────────────────────────────────── */
    function groupTabs() {
        return document.querySelectorAll('#txnGroupTabs [data-txn-group]');
    }

    function paintGroupTabs() {
        Array.prototype.forEach.call(groupTabs(), function (tab) {
            var active = tab.getAttribute('data-txn-group') === activeGroup;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
        });
    }

    /* Show only the chosen group's transactions, so the dropdown stays as
       short as the tab the operator is looking at. */
    function filterTypeOptions() {
        if (!typeSelect) return;
        Array.prototype.forEach.call(typeSelect.querySelectorAll('option'), function (option) {
            var wanted = !activeGroup || option.getAttribute('data-group') === activeGroup;
            option.hidden = !wanted;
            option.disabled = !wanted;
        });
        Array.prototype.forEach.call(typeSelect.querySelectorAll('optgroup'), function (group) {
            var wanted = !activeGroup || group.getAttribute('data-txn-group') === activeGroup;
            group.hidden = !wanted;
            group.disabled = !wanted;
        });
    }

    function chooseGroup(groupKey) {
        var entry = currentEntry();
        activeGroup = groupKey;
        if (!entry || entry.group !== groupKey) {
            var first = TYPES.filter(function (t) { return t.group === groupKey; })[0];
            if (first && typeSelect) typeSelect.value = first.key;
        }
        paintGroupTabs();
        filterTypeOptions();
        applyPanels();
    }

    /* ── panels ─────────────────────────────────────────────────────────── */
    function applyPanels() {
        var entry = currentEntry();
        var panels = entry ? (entry.panels || []) : [];
        Array.prototype.forEach.call(document.querySelectorAll('[data-txn-panels]'), function (block) {
            var names = (block.getAttribute('data-txn-panels') || '').split(/\s+/);
            var wanted = names.indexOf('common') !== -1;
            if (!wanted) {
                for (var i = 0; i < names.length; i++) {
                    if (panels.indexOf(names[i]) !== -1) { wanted = true; break; }
                }
            }
            block.classList.toggle('d-none', !wanted);
            setEnabled(block, wanted);
        });
        if (entry) {
            var group = groupFor(entry.group);
            document.getElementById('txnFormTitle').textContent =
                group ? group.label + ' · ' + entry.label : entry.label;
            document.getElementById('txnTypeHelp').textContent = entry.help || '';
            if (submitLabel) submitLabel.textContent = entry.submit_label || 'Save';
            if (submitButton) {
                submitButton.classList.remove('btn-success', 'btn-primary', 'btn-warning', 'btn-danger', 'btn-secondary');
                submitButton.classList.add(entry.submit_class || 'btn-primary');
            }
        }
        syncDestinationPanels();
        syncRentOutRadios();
        syncTransferLocationFilter();
        syncReturnMode();
        syncTransferSettlement();
        syncBilling();
        syncReturnSettlement();
        syncDamageSource();
        syncDateFields();
        showError('');
    }

    function groupFor(key) {
        for (var i = 0; i < GROUPS.length; i++) {
            if (GROUPS[i].key === key) return GROUPS[i];
        }
        return null;
    }

    /* Which of the two destination blocks applies is data too: the chosen
       transaction's renter_type decides, and each block says whose it is. */
    function syncDestinationPanels() {
        var entry = currentEntry();
        var renterType = entry ? (entry.renter_type || '') : '';
        Array.prototype.forEach.call(document.querySelectorAll('[data-renter-type]'), function (block) {
            var visible = !block.classList.contains('d-none');
            var wanted = visible && block.getAttribute('data-renter-type') === renterType;
            block.classList.toggle('d-none', !wanted);
            setEnabled(block, wanted);
        });
    }

    function syncReturnMode() {
        var entry = currentEntry();
        var hidden = document.getElementById('returnCondition');
        if (entry && entry.return_type && hidden) hidden.value = entry.return_type;
        var full = !!(entry && entry.return_type === 'full');
        Array.prototype.forEach.call(document.querySelectorAll('.return-qty-input'), function (input) {
            if (full) {
                if (!input.readOnly) input.dataset.partialQty = input.value;
                input.value = input.getAttribute('data-pending') || input.value;
                input.readOnly = true;
            } else {
                if (input.readOnly) input.value = input.dataset.partialQty || '';
                input.readOnly = false;
            }
        });
    }

    /* A no-rent answer means the rate cells above go grey and disabled: the
       tools are a physical issue, not a receivable.  The mode comes from the
       server's billing registry; never infer it from a label.  The rate cells
       carry no field names — the submit copies their (zero) values into
       hidden fields — so disabling them can never lose a posting. */
    function syncBilling() {
        var select = document.getElementById('billing_type');
        var mode = select && BILLING[select.value];
        var free = !!(mode && !mode.charges_rent);
        var status = document.getElementById('billingStatus');
        if (status && mode) status.textContent = mode.status;
        Array.prototype.forEach.call(document.querySelectorAll('.txn-line-rate, .transfer-rate'), function (input) {
            if (free) {
                if (!input.classList.contains('rate-off')) input.dataset.paidRate = input.value;
                input.value = '0';
            } else if (input.classList.contains('rate-off')) {
                input.value = input.dataset.paidRate || '';
            }
            input.classList.toggle('rate-off', free);
            // A rate cell stays disabled while its panel is hidden, whatever
            // the rent type — a hidden panel never posts.
            input.disabled = free || !!input.closest('.d-none');
            input.setAttribute('aria-label', free ? 'No rent — rate is zero' : 'Rental rate');
        });
    }

    function syncReturnSettlement() {
        var select = document.getElementById('returnRentalSelect');
        var option = select && select.options[select.selectedIndex];
        var fields = document.getElementById('returnCashFields');
        var status = document.getElementById('returnSettlementStatus');
        var free = !!(option && option.value && BILLING[option.dataset.billingType] &&
                     !BILLING[option.dataset.billingType].charges_rent);
        if (status) status.textContent = option && option.value
            ? moneyStatus(option.dataset.billingType, option.dataset.amount) : 'Choose a rental to see its rent status.';
        if (fields) {
            fields.classList.toggle('d-none', free);
            setEnabled(fields, !free && !fields.closest('.d-none'));
        }
    }

    function syncTransferSettlement() {
        var select = document.getElementById('settle_choice');
        var fields = document.getElementById('transferPayNowFields');
        var status = document.getElementById('transferSettlementStatus');
        if (!select || !fields) return;
        var sources = tickedTransferSources();
        var due = sources.reduce(function (sum, source) { return sum + Number(source.amountDue || 0); }, 0);
        var free = sources.length && sources.every(function (source) {
            return BILLING[source.billingType] && !BILLING[source.billingType].charges_rent;
        });
        if (status) status.textContent = !sources.length ? 'Choose a source to see its rent status.'
            : (free ? BILLING[sources[0].billingType].status
               : moneyStatus('', due) + (sources.some(function (source) {
                   return BILLING[source.billingType] && !BILLING[source.billingType].charges_rent;
               }) ? ' No-charge holders have no balance to settle.' : ''));
        if (free) select.value = 'add_credit';
        select.disabled = !!free || !sources.length || !!select.closest('.d-none');
        var payNow = !free && select.value === 'pay_now';
        fields.classList.toggle('d-none', !payNow);
        setEnabled(fields, payNow && !fields.closest('.d-none'));
    }

    function syncDamageSource() {
        var select = document.getElementById('damage_source');
        if (!select) return;
        var fromRental = select.value === 'rental';
        var storeFields = document.getElementById('damageStoreFields');
        var rentalField = document.getElementById('damageRentalField');
        if (storeFields) { storeFields.classList.toggle('d-none', fromRental); setEnabled(storeFields, !fromRental); }
        if (rentalField) { rentalField.classList.toggle('d-none', !fromRental); setEnabled(rentalField, fromRental); }
        var hint = document.getElementById('damageRentalHint');
        if (hint) {
            hint.textContent = fromRental
                ? 'The pieces are returned off the rental line and written off in the same step, so stock and location stay balanced.'
                : 'In-store pieces are written off through the existing scrap register.';
        }
    }

    /* One visible transaction date fills the endpoint's own date field. */
    function syncDateFields() {
        var date = document.getElementById('transaction_date');
        if (!date) return;
        ['returnDateHidden', 'paymentDateHidden', 'discountDateHidden'].forEach(function (id) {
            var hidden = document.getElementById(id);
            if (hidden) hidden.value = date.value;
        });
    }

    /* The rental picker of the chosen transaction, found by the name the
       registry gave it — never by a hard-coded id. */
    function pickerSelect() {
        var entry = currentEntry();
        if (!entry || !entry.picker) return null;
        return document.querySelector('[data-rental-picker="' + entry.picker + '"] select[name="rental_id"]');
    }

    /* ── rent-out setup: from where, and to whom ───────────────────────────
       Steps 1 + 2 of renting out.  The two answers pick the transaction: the
       store means a create, a site or a customer means a transfer — and To
       decides the renter type.  The option is found in the registry by kind +
       renter type, never by a hard-coded key; choosing the transaction
       directly (tabs / dropdown) answers From/To back the same way. */
    function rentOutPanel() {
        return document.getElementById('rentOutSourcePanel');
    }

    function rentOutFrom() {
        var checked = document.querySelector('input[name="rentout_from"]:checked');
        return checked ? checked.value : 'store';
    }

    function rentOutTo() {
        var checked = document.querySelector('input[name="rentout_to"]:checked');
        return checked ? checked.value : 'external';
    }

    function rentOutFromLocType() {
        var checked = document.querySelector('input[name="rentout_from"]:checked');
        return checked ? (checked.getAttribute('data-loc-type') || '') : '';
    }

    function rentOutFromRadio(value) {
        return document.querySelector('input[name="rentout_from"][value="' + value + '"]');
    }

    function rentOutToRadio(value) {
        return document.querySelector('input[name="rentout_to"][value="' + value + '"]');
    }

    function retargetRentOut() {
        if (!typeSelect) return;
        var wantKind = rentOutFrom() === 'store' ? 'create' : 'transfer';
        var wantRenter = rentOutTo();
        var target = null;
        TYPES.forEach(function (option) {
            if (option.kind === wantKind && option.renter_type === wantRenter) target = option;
        });
        if (!target) return;
        if (typeSelect.value !== target.key) typeSelect.value = target.key;
        activeGroup = target.group;
        paintGroupTabs();
        filterTypeOptions();
        applyPanels();
    }

    function syncRentOutRadios() {
        var panel = rentOutPanel();
        if (!panel || panel.classList.contains('d-none')) return;
        var entry = currentEntry();
        if (!entry) return;
        var fromRadio = null;
        if (entry.kind === 'create') {
            fromRadio = rentOutFromRadio('store');
        } else if (entry.kind === 'transfer') {
            var current = rentOutFrom();
            fromRadio = rentOutFromRadio(
                (current === 'site' || current === 'customer') ? current : defaultRentOutFrom());
        }
        if (fromRadio) fromRadio.checked = true;
        var toRadio = rentOutToRadio(entry.renter_type || rentOutTo());
        if (toRadio) toRadio.checked = true;
    }

    /* A transfer picked straight from the dropdown keeps the operator's
       site/customer answer; with no answer yet, whichever side actually
       holds tools wins, so the location list is never empty by default. */
    function defaultRentOutFrom() {
        var counts = {};
        SOURCES.forEach(function (source) {
            counts[source.locType] = (counts[source.locType] || 0) + 1;
        });
        var siteRadio = rentOutFromRadio('site');
        var customerRadio = rentOutFromRadio('customer');
        var siteLoc = siteRadio ? siteRadio.getAttribute('data-loc-type') : '';
        var customerLoc = customerRadio ? customerRadio.getAttribute('data-loc-type') : '';
        var hasSite = !!siteLoc && !!counts[siteLoc];
        var hasCustomer = !!customerLoc && !!counts[customerLoc];
        if (hasSite && !hasCustomer) return 'site';
        if (hasCustomer && !hasSite) return 'customer';
        return 'site';
    }

    /* A transfer From a site only offers site locations; From a customer only
       customer locations.  The location an option stands for is resolved
       through the transfer sources the server sent, so the filter can never
       disagree with the data.  The combo widget hides hidden options on its
       own; clearing the select through a change event keeps its visible
       label and the holder list in sync. */
    function transferSourceLocType(sourceKey) {
        for (var i = 0; i < SOURCES.length; i++) {
            if (String(SOURCES[i].key) === String(sourceKey)) return SOURCES[i].locType || '';
        }
        return '';
    }

    function syncTransferLocationFilter() {
        var select = document.getElementById('transferSourceSelect');
        if (!select) return;
        var want = rentOutFromLocType();
        Array.prototype.forEach.call(select.options, function (option) {
            if (!option.value) { option.hidden = false; return; }
            var loc = option.getAttribute('data-loc-type') || transferSourceLocType(option.value);
            option.hidden = !!want && !!loc && loc !== want;
        });
        var current = select.options[select.selectedIndex];
        if (current && current.value && current.hidden) {
            select.value = '';
            select.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }

    /* ── store tools: searchable tick list → excel-style selected rows ────
       Step 3 of renting out from the store.  Every issuable tool is listed
       with a checkbox and a search box; each tick adds one excel-style row
       (tool, pieces / serial, qty, rate).  One row per tool, in tick order. */
    function storeToolById(toolId) {
        return TOOLS_BY_ID[String(toolId)];
    }

    function storeSelectedIds() {
        var ids = {};
        Array.prototype.forEach.call(
            document.querySelectorAll('#storeSelectedRows .store-line-row'), function (row) {
                ids[String(row.getAttribute('data-tool-id'))] = true;
            });
        return ids;
    }

    function renderStoreToolList() {
        var list = document.getElementById('storeToolList');
        var search = document.getElementById('storeToolSearch');
        if (!list) return;
        var query = search ? search.value.trim().toLowerCase() : '';
        var selected = storeSelectedIds();
        list.innerHTML = '';
        var shown = 0;
        TOOLS.forEach(function (tool) {
            var haystack = ((tool.name || '') + ' ' + (tool.code || '') + ' ' +
                (tool.categoryName || '')).toLowerCase();
            if (query && haystack.indexOf(query) === -1) return;
            shown++;
            var row = document.createElement('label');
            row.className = 'store-tool-row';
            var box = document.createElement('input');
            box.type = 'checkbox';
            box.className = 'form-check-input store-tool-check';
            box.value = String(tool.id);
            box.checked = !!selected[String(tool.id)];
            box.setAttribute('aria-label', 'Select ' + tool.name);
            box.addEventListener('change', function () {
                if (box.checked) addStoreRow(tool.id);
                else removeStoreRow(tool.id);
            });
            var name = document.createElement('span');
            name.innerHTML = '<strong>' + escapeHtml(tool.name) + '</strong>' +
                (tool.code ? ' <small class="text-muted">(' + escapeHtml(tool.code) + ')</small>' : '') +
                (tool.categoryName && tool.categoryName !== tool.name
                    ? '<br><small class="text-muted">' + escapeHtml(tool.categoryName) + '</small>' : '');
            var meta = document.createElement('span');
            meta.className = 'store-tool-meta small text-muted';
            meta.textContent = String(tool.availableQty) + ' ' + (tool.unit || 'pcs') +
                ' in store · rate ' + (tool.rate || 0);
            row.appendChild(box);
            row.appendChild(name);
            row.appendChild(meta);
            list.appendChild(row);
        });
        if (!shown) {
            list.innerHTML = '<div class="small text-muted p-2">' +
                (TOOLS.length ? 'No tool matches this search.' : 'Nothing is in the store right now.') +
                '</div>';
        }
        updateStoreCount();
    }

    function updateStoreCount() {
        var node = document.getElementById('storeToolCount');
        if (!node) return;
        var total = TOOLS.length;
        var count = document.querySelectorAll('#storeSelectedRows .store-line-row').length;
        node.textContent = total ? (count + ' of ' + total + ' ticked') : 'No tools in store';
    }

    function addStoreRow(toolId) {
        var tool = storeToolById(toolId);
        var tbody = document.getElementById('storeSelectedRows');
        if (!tool || !tbody) return;
        if (tbody.querySelector('.store-line-row[data-tool-id="' + tool.id + '"]')) return;
        var row = document.createElement('tr');
        row.className = 'store-line-row';
        row.setAttribute('data-tool-id', String(tool.id));
        row.innerHTML =
            '<td class="store-line-idx"></td>' +
            '<td><strong>' + escapeHtml(tool.name) + '</strong>' +
            (tool.code ? '<br><small class="text-muted">' + escapeHtml(tool.code) + '</small>' : '') +
            '<br><small class="text-muted">' + escapeHtml(String(tool.availableQty)) + ' ' +
            escapeHtml(tool.unit || 'pcs') + ' in store</small></td>' +
            '<td class="store-line-pieces"></td>' +
            '<td><input type="number" class="form-control form-control-sm store-line-qty" min="0.1" step="0.1" max="' +
            escapeHtml(String(tool.availableQty)) + '" placeholder="Qty" aria-label="Quantity of ' +
            escapeHtml(tool.name) + '"></td>' +
            '<td><input type="number" class="form-control form-control-sm txn-line-rate" min="0" step="0.01" value="' +
            escapeHtml(String(tool.rate == null ? '' : tool.rate)) + '" aria-label="Rate for ' +
            escapeHtml(tool.name) + '"></td>' +
            '<td class="col-action"><button type="button" class="btn btn-sm btn-outline-danger store-line-remove" ' +
            'title="Remove" aria-label="Remove ' + escapeHtml(tool.name) + '">' +
            '<i class="fas fa-times" aria-hidden="true"></i></button></td>';
        var piecesCell = row.querySelector('.store-line-pieces');
        var serials = tool.serials || [];
        if (serials.length) {
            var serialBox = document.createElement('select');
            serialBox.className = 'form-select form-select-sm store-line-serials';
            serialBox.multiple = true;
            serialBox.size = 2;
            serialBox.setAttribute('aria-label', 'Pieces of ' + tool.name);
            serials.forEach(function (serial) {
                var option = document.createElement('option');
                option.value = String(serial.id);
                option.textContent = serial.serial_number + ' (' + (serial.condition || 'good') + ')';
                serialBox.appendChild(option);
            });
            serialBox.addEventListener('change', function () { onStoreSerialsChange(row); });
            piecesCell.appendChild(serialBox);
        } else {
            piecesCell.innerHTML = '<span class="small text-muted">Qty only</span>';
        }
        row.querySelector('.store-line-remove').addEventListener('click', function () {
            removeStoreRow(tool.id);
        });
        tbody.appendChild(row);
        renumberStoreRows();
        updateStoreCount();
        toggleStoreEmpty();
        syncBilling();
    }

    function removeStoreRow(toolId) {
        var tbody = document.getElementById('storeSelectedRows');
        if (!tbody) return;
        var row = tbody.querySelector('.store-line-row[data-tool-id="' + toolId + '"]');
        if (row) row.remove();
        var box = document.querySelector('#storeToolList .store-tool-check[value="' + toolId + '"]');
        if (box) box.checked = false;
        renumberStoreRows();
        updateStoreCount();
        toggleStoreEmpty();
    }

    function renumberStoreRows() {
        Array.prototype.forEach.call(
            document.querySelectorAll('#storeSelectedRows .store-line-row'), function (row, index) {
                var cell = row.querySelector('.store-line-idx');
                if (cell) cell.textContent = String(index + 1);
            });
    }

    function toggleStoreEmpty() {
        var empty = document.getElementById('storeSelectedEmpty');
        var sheet = document.getElementById('storeSelectedSheet');
        var hasRows = !!document.querySelector('#storeSelectedRows .store-line-row');
        if (empty) empty.classList.toggle('d-none', hasRows);
        if (sheet) sheet.classList.toggle('d-none', !hasRows);
    }

    function onStoreSerialsChange(row) {
        var serialBox = row.querySelector('.store-line-serials');
        var qty = row.querySelector('.store-line-qty');
        if (!serialBox || !qty) return;
        var count = serialBox.selectedOptions.length;
        if (count > 0) {
            qty.value = String(count);
            qty.readOnly = true;
        } else {
            qty.readOnly = false;
        }
    }

    function validateLines() {
        var rows = document.querySelectorAll('#storeSelectedRows .store-line-row');
        if (!rows.length) return ['Tick at least one tool from the store list.'];
        var messages = [];
        Array.prototype.forEach.call(rows, function (row) {
            var tool = storeToolById(row.getAttribute('data-tool-id'));
            if (!tool) return;
            var qtyInput = row.querySelector('.store-line-qty');
            var qty = parseFloat((qtyInput && qtyInput.value) || '0');
            var serialBox = row.querySelector('.store-line-serials');
            var serialCount = serialBox ? serialBox.selectedOptions.length : 0;
            if (!qty && !serialCount) {
                messages.push('Enter a quantity for ' + tool.name + ' — or tick its pieces.');
            } else if (qty > tool.availableQty + 0.001) {
                messages.push('Only ' + tool.availableQty + ' of ' + tool.name + ' is in store.');
            } else if (serialCount && Math.abs(serialCount - qty) > 0.001) {
                messages.push('Quantity must match the ticked pieces for ' + tool.name + '.');
            }
        });
        return messages;
    }

    function collectLineFields() {
        var tbody = document.getElementById('storeSelectedRows');
        if (!tbody) return;
        Array.prototype.forEach.call(tbody.querySelectorAll('.store-post'), function (node) {
            node.remove();
        });
        Array.prototype.forEach.call(tbody.querySelectorAll('.store-line-row'), function (row) {
            var serialBox = row.querySelector('.store-line-serials');
            var serialIds = serialBox
                ? Array.prototype.map.call(serialBox.selectedOptions, function (o) { return o.value; })
                : [];
            var qtyInput = row.querySelector('.store-line-qty');
            var rateInput = row.querySelector('.txn-line-rate');
            var cell = row.querySelector('td');
            addHidden(cell, 'tool_id[]', row.getAttribute('data-tool-id'), 'store-post');
            // qty[] and rate[] are emitted for every tool_id[] row, even when
            // blank: the create route zips the three lists positionally and
            // tolerates a blank value, but not a missing slot.
            addHidden(cell, 'qty[]', (qtyInput && qtyInput.value) || '', 'store-post');
            addHidden(cell, 'rate[]', (rateInput && rateInput.value) || '', 'store-post');
            addHidden(cell, 'row_serial_ids[]', serialIds.join(','), 'store-post');
        });
    }

    function addHidden(parent, name, value, className) {
        var input = document.createElement('input');
        input.type = 'hidden';
        input.name = name;
        input.value = value == null ? '' : value;
        input.className = className || '';
        parent.appendChild(input);
        return input;
    }

    /* ── transfer: holders and the lines moving out ─────────────────────── */
    function renderTransferHolders() {
        var list = document.getElementById('transferHoldersList');
        var select = document.getElementById('transferSourceSelect');
        if (!list || !select) return;
        list.innerHTML = '';
        var key = select.value;
        var current = select.options[select.selectedIndex];
        if (current && current.value && current.hidden) {
            // The From filter just hid this location: drop the stale choice
            // instead of showing holders the operator can no longer pick.
            select.value = '';
            select.dispatchEvent(new Event('change', { bubbles: true }));
            return;
        }
        var selected = SOURCES.filter(function (source) { return String(source.key) === String(key); })[0];
        var matching = selected ? SOURCES.filter(function (source) {
            return source.locationKey === selected.locationKey;
        }) : [];
        if (!matching.length) {
            list.innerHTML = '<span class="small text-muted">Choose a site / holder above.</span>';
            renderTransferLines();
            return;
        }
        matching.forEach(function (source) {
            var row = document.createElement('label');
            row.className = 'form-check d-flex align-items-center gap-2 mb-1 transfer-holder-row';
            var box = document.createElement('input');
            box.type = 'checkbox';
            box.className = 'form-check-input transfer-holder-checkbox';
            box.name = 'source_rental_id[]';
            box.value = source.key;
            box.checked = true;
            box.addEventListener('change', renderTransferLines);
            var text = document.createElement('span');
            text.className = 'small';
            text.textContent = source.rentalCode + ' · ' + source.holder + ' · ' + source.pending + ' out';
            row.appendChild(box);
            row.appendChild(text);
            list.appendChild(row);
        });
        var selectAll = document.getElementById('selectAllTransferHolders');
        if (selectAll) selectAll.checked = true;
        renderTransferLines();
    }

    function tickedTransferSources() {
        var list = document.getElementById('transferHoldersList');
        if (!list) return [];
        var ticked = {};
        Array.prototype.forEach.call(list.querySelectorAll('.transfer-holder-checkbox'), function (box) {
            if (box.checked) ticked[String(box.value)] = true;
        });
        return SOURCES.filter(function (source) { return ticked[String(source.key)]; });
    }

    function renderTransferLines() {
        var container = document.getElementById('transferLinesContainer');
        if (!container) return;
        container.innerHTML = '';
        var sources = tickedTransferSources();
        syncTransferSettlement();
        if (!sources.length) {
            container.textContent = 'Tick a holder to see the tools at this site / customer.';
            updateTransferCount();
            return;
        }
        var wrap = document.createElement('div');
        wrap.className = 'table-responsive';
        wrap.innerHTML = '<table class="table table-sm table-bordered table-striped align-middle tool-txn-sheet">' +
            '<caption class="caption-top">Tools currently at ' + escapeHtml(sources[0].holder) +
            ' — tick the rows to move, unticked rows stay here.</caption>' +
            '<thead><tr><th scope="col" class="col-pick">' +
            '<input type="checkbox" class="form-check-input transfer-pick-all" checked aria-label="Tick all tools">' +
            '</th><th scope="col" class="col-idx">#</th><th scope="col" class="col-rental">Rental</th>' +
            '<th scope="col">Tool</th>' +
            '<th scope="col" class="col-source">At source</th><th scope="col" class="col-qty">Move qty</th>' +
            '<th scope="col" class="col-rate">New rate (PKR)</th><th scope="col" class="col-pieces">Exact pieces</th>' +
            '</tr></thead><tbody></tbody></table>';
        var tbody = wrap.querySelector('tbody');
        var index = 0;
        sources.forEach(function (source) {
            source.lines.forEach(function (line) {
                index++;
                var row = document.createElement('tr');
                row.className = 'transfer-line-row';
                row.setAttribute('data-search', (source.rentalCode + ' ' + line.name + ' ' +
                    (line.code || '')).toLowerCase());
                row.innerHTML =
                    '<td class="col-pick"><input type="checkbox" class="form-check-input transfer-pick" checked aria-label="Move ' +
                    escapeHtml(line.name) + '"></td>' +
                    '<td>' + index + '</td><td>' + escapeHtml(source.rentalCode) + '</td>' +
                    '<td><strong>' + escapeHtml(line.name) + '</strong><br><small>' + escapeHtml(line.code) + '</small>' +
                    '<input type="hidden" class="transfer-item-id" value="' + Number(line.itemId) + '"></td>' +
                    '<td>' + Number(line.qty) + ' ' + escapeHtml(line.unit || '') + '</td>' +
                    '<td><input type="number" class="form-control form-control-sm transfer-qty" aria-label="Move quantity of ' +
                    escapeHtml(line.name) + '" min="0" step="0.1" max="' + Number(line.qty) + '" value="' + Number(line.qty) + '"></td>' +
                    '<td><input type="number" class="form-control form-control-sm transfer-rate" aria-label="New rate for ' +
                    escapeHtml(line.name) + '" min="0" step="0.01" value="' +
                    Number(line.dailyRate || line.rate || 0) + '"></td>' +
                    '<td><select class="form-select form-select-sm transfer-serials" multiple size="2" aria-label="Pieces to move"></select></td>';
                var serialBox = row.querySelector('.transfer-serials');
                (line.serials || []).forEach(function (serial) {
                    var option = document.createElement('option');
                    option.value = String(serial.id);
                    option.textContent = serial.serial_number + ' (' + (serial.condition || 'good') + ')';
                    // Do not preselect all: quantity changes must not accidentally
                    // override the operator's requested quantity on the server.
                    serialBox.appendChild(option);
                });
                if (!(line.serials || []).length) serialBox.classList.add('d-none');
                serialBox.addEventListener('change', function () {
                    if (serialBox.selectedOptions.length) row.querySelector('.transfer-qty').value = serialBox.selectedOptions.length;
                });
                row.querySelector('.transfer-pick').addEventListener('change', function () {
                    updateTransferPickAll();
                    updateTransferCount();
                });
                tbody.appendChild(row);
            });
        });
        wrap.querySelector('.transfer-pick-all').addEventListener('change', function () {
            var checked = this.checked;
            Array.prototype.forEach.call(tbody.querySelectorAll('.transfer-line-row'), function (row) {
                if (row.style.display === 'none') return; // search-hidden rows keep their tick
                row.querySelector('.transfer-pick').checked = checked;
            });
            updateTransferCount();
        });
        container.appendChild(wrap);
        applyTransferSearch();
        syncBilling();
    }

    function updateTransferPickAll() {
        var all = document.querySelector('#transferLinesContainer .transfer-pick-all');
        if (!all) return;
        var boxes = document.querySelectorAll('#transferLinesContainer .transfer-pick');
        var ticked = document.querySelectorAll('#transferLinesContainer .transfer-pick:checked');
        all.checked = boxes.length > 0 && boxes.length === ticked.length;
    }

    function applyTransferSearch() {
        var input = document.getElementById('transferLinesSearch');
        var query = input ? input.value.trim().toLowerCase() : '';
        Array.prototype.forEach.call(
            document.querySelectorAll('#transferLinesContainer .transfer-line-row'), function (row) {
                var hay = row.getAttribute('data-search') || '';
                row.style.display = (!query || hay.indexOf(query) !== -1) ? '' : 'none';
            });
        updateTransferCount();
    }

    function updateTransferCount() {
        var node = document.getElementById('transferLinesCount');
        if (!node) return;
        var rows = document.querySelectorAll('#transferLinesContainer .transfer-line-row');
        if (!rows.length) { node.textContent = ''; return; }
        var ticked = 0;
        Array.prototype.forEach.call(rows, function (row) {
            var pick = row.querySelector('.transfer-pick');
            if (pick && pick.checked) ticked++;
        });
        node.textContent = ticked + ' of ' + rows.length + ' ticked to move';
    }

    function collectTransferFields() {
        var container = document.getElementById('transferLinesContainer');
        if (!container) return;
        Array.prototype.forEach.call(container.querySelectorAll('.transfer-post'), function (node) { node.remove(); });
        Array.prototype.forEach.call(container.querySelectorAll('.transfer-line-row'), function (row) {
            var pick = row.querySelector('.transfer-pick');
            if (pick && !pick.checked) return; // unticked rows stay at the holder
            var qty = parseFloat(row.querySelector('.transfer-qty').value || '0');
            var itemId = row.querySelector('.transfer-item-id').value;
            if (!qty || qty <= 0) return;
            addHidden(row.querySelector('td'), 'transfer_item_id[]', itemId, 'transfer-post');
            addHidden(row.querySelector('td'), 'transfer_qty_' + itemId, String(qty), 'transfer-post');
            var rate = row.querySelector('.transfer-rate').value;
            if (rate) addHidden(row.querySelector('td'), 'transfer_rate_' + itemId, rate, 'transfer-post');
            var serialBox = row.querySelector('.transfer-serials');
            if (serialBox && serialBox.selectedOptions.length) {
                var ids = Array.prototype.map.call(serialBox.selectedOptions, function (o) { return o.value; });
                addHidden(row.querySelector('td'), 'transfer_serial_ids_' + itemId + '[]', ids.join(','), 'transfer-post');
            }
        });
    }

    function validateTransfer() {
        var rows = document.querySelectorAll('#transferLinesContainer .transfer-line-row');
        var ticked = [];
        Array.prototype.forEach.call(rows, function (row) {
            var pick = row.querySelector('.transfer-pick');
            if (!pick || pick.checked) ticked.push(row);
        });
        if (!ticked.length) {
            var select = document.getElementById('transferSourceSelect');
            var offered = 0;
            if (select) {
                Array.prototype.forEach.call(select.options, function (option) {
                    if (option.value && !option.hidden) offered++;
                });
            }
            if (!offered) {
                var from = rentOutFrom();
                var where = from === 'customer' ? 'any customer'
                    : (from === 'site' ? 'any site' : 'the selected source');
                return ['No tools are currently out at ' + where +
                    ' — switch "From where?" above or rent from the store.'];
            }
            if (select && !select.value) return ['Choose the current site / holder to transfer from.'];
            return ['Tick at least one holder and keep at least one tool ticked to transfer.'];
        }
        var moving = 0;
        Array.prototype.forEach.call(ticked, function (row) {
            if (parseFloat(row.querySelector('.transfer-qty').value || '0') > 0) moving++;
        });
        if (!moving) return ['Enter a quantity for at least one ticked tool to transfer.'];
        var errors = [];
        Array.prototype.forEach.call(ticked, function (row) {
            var qty = Number(row.querySelector('.transfer-qty').value);
            var max = Number(row.querySelector('.transfer-qty').max);
            var serials = row.querySelector('.transfer-serials');
            if (!Number.isFinite(qty) || qty < 0 || qty > max) errors.push('Move quantity must be between 0 and the stock at source.');
            if (serials && serials.selectedOptions.length && serials.selectedOptions.length !== qty) {
                errors.push('Move quantity must match the number of exact pieces selected.');
            }
        });
        if (textValue('settle_choice') === 'pay_now' && !textValue('transferAccount') &&
            tickedTransferSources().some(function (source) { return Number(source.amountDue || 0) > 0; })) {
            errors.push('Choose the cash / bank account receiving rent from the previous holder.');
        }
        return errors;
    }

    /* ── returns ────────────────────────────────────────────────────────── */
    var returnRequest = 0;

    function loadReturnItems(rentalId) {
        var box = document.getElementById('returnItemRows');
        if (!box) return;
        if (!rentalId) {
            ++returnRequest; // invalidate an in-flight fetch when the selection is cleared
            box.innerHTML = '<div class="small text-muted">Choose a rental to see the tools still out.</div>';
            return;
        }
        var request = ++returnRequest;
        box.innerHTML = '<div class="small text-muted">Loading the outstanding tools…</div>';
        fetch('/hdc/api/tool-rental/rental/' + encodeURIComponent(rentalId) + '/pending-items',
              { credentials: 'same-origin' })
            .then(function (response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.json();
            })
            .then(function (items) {
                if (request !== returnRequest) return;
                renderReturnItems(items || []);
            })
            .catch(function () {
                if (request !== returnRequest) return;
                box.innerHTML = '<div class="small text-danger">Could not load this rental’s tools. Try again.</div>';
            });
    }

    function renderReturnItems(items) {
        var box = document.getElementById('returnItemRows');
        if (!box) return;
        var picker = pickerSelect();
        box.innerHTML = '';
        if (!items.length) {
            box.innerHTML = '<div class="small text-muted">Nothing is still out on this rental.</div>';
            return;
        }
        var wrap = document.createElement('div');
        wrap.className = 'table-responsive';
        wrap.innerHTML = '<table class="table table-sm table-bordered table-striped align-middle tool-txn-sheet">' +
            '<caption class="caption-top">Tools still at the selected site / customer</caption>' +
            '<thead><tr><th scope="col">#</th><th scope="col">Tool</th><th scope="col">Issued</th>' +
            '<th scope="col">Already back</th><th scope="col">Still out</th><th scope="col">Returning now</th>' +
            '<th scope="col">Condition</th><th scope="col">Exact pieces</th></tr></thead><tbody></tbody></table>';
        var tbody = wrap.querySelector('tbody');
        items.forEach(function (item, index) {
            var row = document.createElement('tr');
            row.className = 'tool-txn-line';
            row.innerHTML =
                '<td>' + (index + 1) + '</td><td><strong>' + escapeHtml(item.tool_name) +
                '</strong><br><small>' + escapeHtml(item.tool_code) + '</small>' +
                '<input type="hidden" class="return-item-id" value="' + Number(item.rental_item_id) + '"></td>' +
                '<td>' + Number(item.qty_rented) + '</td><td>' + Number(item.qty_returned) + '</td>' +
                '<td class="fw-bold return-outstanding">' + Number(item.qty_pending) + '</td>' +
                '<td><input type="number" class="form-control form-control-sm return-qty-input" aria-label="Returning ' +
                escapeHtml(item.tool_name) + '" min="0" step="0.1" max="' + Number(item.qty_pending) +
                '" data-pending="' + Number(item.qty_pending) + '" placeholder="0"></td>' +
                '<td><input type="text" class="form-control form-control-sm return-condition" aria-label="Condition" placeholder="Condition / notes"></td>' +
                '<td><select class="form-select form-select-sm return-serials d-none" multiple size="2" aria-label="Exact pieces coming back"></select></td>';
            row.querySelector('.return-item-id').dataset.pending = String(item.qty_pending);
            var serialBox = row.querySelector('.return-serials');
            var rentalId = picker ? picker.value : '';
            fetch('/hdc/api/tool-rental/serials/out-of-store?tool_id=' + encodeURIComponent(item.tool_id) +
                  '&rental_id=' + encodeURIComponent(rentalId),
                  { credentials: 'same-origin' })
                .then(function (response) { return response.ok ? response.json() : null; })
                .then(function (data) {
                    if (!data || !data.tools) return;
                    var serials = [];
                    (data.tools || []).forEach(function (tool) {
                        (tool.serials || []).forEach(function (serial) {
                            if (String(tool.tool_id) === String(item.tool_id) &&
                                String(serial.rental_item_id) === String(item.rental_item_id)) {
                                serials.push(serial);
                            }
                        });
                    });
                    if (!serials.length) return;
                    serialBox.classList.remove('d-none');
                    serials.forEach(function (serial) {
                        var option = document.createElement('option');
                        option.value = String(serial.serial_id);
                        option.textContent = serial.serial_number;
                        serialBox.appendChild(option);
                    });
                    serialBox.addEventListener('change', function () {
                        var qty = row.querySelector('.return-qty-input');
                        if (serialBox.selectedOptions.length) {
                            qty.value = String(serialBox.selectedOptions.length);
                            qty.readOnly = true;
                        } else {
                            qty.readOnly = false;
                        }
                    });
                })
                .catch(function () { /* the quantity path still works */ });
            tbody.appendChild(row);
        });
        box.appendChild(wrap);
        var hint = document.createElement('div');
        hint.className = 'small text-muted';
        hint.textContent = 'Leave a quantity blank for a tool that did not come back. “Full Return” takes everything still outstanding.';
        box.appendChild(hint);
        syncReturnMode();
    }

    function collectReturnFields() {
        var container = document.getElementById('returnItemRows');
        if (!container) return;
        Array.prototype.forEach.call(container.querySelectorAll('.return-post'), function (node) { node.remove(); });
        Array.prototype.forEach.call(container.querySelectorAll('.tool-txn-line'), function (row) {
            var itemId = row.querySelector('.return-item-id');
            if (!itemId) return;
            var qty = parseFloat(row.querySelector('.return-qty-input').value || '0');
            var serialBox = row.querySelector('.return-serials');
            var serialIds = serialBox ? Array.prototype.map.call(serialBox.selectedOptions, function (o) { return o.value; }) : [];
            if (!qty && !serialIds.length) return;
            addHidden(row.querySelector('td'), 'rental_item_id[]', itemId.value, 'return-post');
            addHidden(row.querySelector('td'), 'qty_returned[]', serialIds.length ? String(serialIds.length) : String(qty), 'return-post');
            addHidden(row.querySelector('td'), 'condition_notes[]', row.querySelector('.return-condition').value, 'return-post');
            if (serialIds.length) {
                addHidden(row.querySelector('td'), 'return_serial_ids_' + itemId.value + '[]', serialIds.join(','), 'return-post');
            }
        });
    }

    /* ── damage / maintenance pickers ───────────────────────────────────── */
    function renderDamageSerials() {
        var toolSelect = document.getElementById('damageToolSelect');
        var box = document.getElementById('damageSerialList');
        if (!toolSelect || !box) return;
        var tool = TOOLS_BY_ID[String(toolSelect.value)];
        box.innerHTML = '';
        if (!tool) {
            box.innerHTML = '<span class="small text-muted">Choose a tool to list the pieces in the store.</span>';
            return;
        }
        if (!tool.serials.length) {
            box.innerHTML = '<span class="small text-muted">No individual piece markings for this tool — use the quantity.</span>';
            return;
        }
        tool.serials.forEach(function (serial) {
            var label = document.createElement('label');
            label.className = 'form-check';
            label.innerHTML = '<input class="form-check-input" type="checkbox" name="damage_serial_ids[]" value="' +
                serial.id + '"><span class="form-check-label small">' + serial.serial_number +
                ' <span class="text-muted">(' + (serial.condition || 'good') + ')</span></span>';
            box.appendChild(label);
        });
    }

    function renderMaintenanceSend() {
        var toolSelect = document.getElementById('maintenanceToolSelect');
        var box = document.getElementById('maintenanceSendList');
        if (!toolSelect || !box) return;
        box.innerHTML = '';
        var tool = TOOLS_BY_ID[String(toolSelect.value)];
        if (!tool) {
            box.innerHTML = '<span class="small text-muted">Choose a tool to list the pieces in the store.</span>';
            return;
        }
        if (!tool.serials.length) {
            box.innerHTML = '<span class="small text-muted">No piece markings to send for this tool.</span>';
            return;
        }
        tool.serials.forEach(function (serial) {
            var label = document.createElement('label');
            label.className = 'form-check';
            label.innerHTML = '<input class="form-check-input" type="checkbox" name="maintenance_serial_ids[]" value="' +
                serial.id + '"><span class="form-check-label small">' + serial.serial_number +
                ' <span class="text-muted">(' + (serial.condition || 'good') + ')</span></span>';
            box.appendChild(label);
        });
    }

    function renderMaintenanceReturn() {
        var box = document.getElementById('maintenanceReturnList');
        if (!box) return;
        box.innerHTML = '';
        if (!MAINTENANCE_PIECES.length) {
            box.innerHTML = '<span class="small text-muted">Nothing is with the workshop right now.</span>';
            return;
        }
        MAINTENANCE_PIECES.forEach(function (piece) {
            var label = document.createElement('label');
            label.className = 'form-check';
            label.innerHTML = '<input class="form-check-input" type="checkbox" name="maintenance_serial_ids[]" value="' +
                piece.serialId + '"><span class="form-check-label small">' + piece.toolName +
                ' (' + piece.toolCode + ') — ' + piece.serialNumber +
                (piece.since ? ' <span class="text-muted">since ' + piece.since + '</span>' : '') + '</span>';
            box.appendChild(label);
        });
    }

    function loadDamageRentalItems(rentalId) {
        var select = document.getElementById('damageRentalItem');
        if (!select) return;
        select.innerHTML = '';
        select.classList.add('d-none');
        if (!rentalId) return;
        fetch('/hdc/api/tool-rental/rental/' + encodeURIComponent(rentalId) + '/pending-items',
              { credentials: 'same-origin' })
            .then(function (response) { return response.ok ? response.json() : []; })
            .then(function (items) {
                (items || []).forEach(function (item) {
                    var option = document.createElement('option');
                    option.value = String(item.rental_item_id);
                    option.textContent = item.tool_name + ' (' + item.tool_code + ') — ' +
                        item.qty_pending + ' out';
                    select.appendChild(option);
                });
                select.classList.toggle('d-none', !(items || []).length);
            })
            .catch(function () { /* leave the select hidden */ });
    }

    /* ── the checks a transaction declares, by name ─────────────────────── */
    function textValue(id) {
        var node = document.getElementById(id);
        return node ? String(node.value || '').trim() : '';
    }

    function numberValue(id) {
        var node = document.getElementById(id);
        return parseFloat((node || {}).value || '0') || 0;
    }

    function checkCustomer() {
        var input = document.getElementById('txnCustomerNameInput');
        return (input && input.value.trim()) ? [] : ['Enter the customer name.'];
    }

    function checkProject() {
        return textValue('txnProjectSelect') ? [] : ['Choose the site / project.'];
    }

    function checkRental() {
        var picker = pickerSelect();
        if (picker && picker.value) return [];
        if (picker) picker.focus();
        return ['Choose the rental this transaction belongs to.'];
    }

    function checkLines() {
        return validateLines();
    }

    function checkReturnItems() {
        var entry = currentEntry();
        if (entry && entry.return_type === 'full') return [];
        var coming = 0;
        Array.prototype.forEach.call(document.querySelectorAll('#returnItemRows .return-qty-input'), function (input) {
            if (parseFloat(input.value || '0') > 0) coming++;
        });
        if (!coming) {
            return ['Enter the quantity coming back — or choose Full Return to close the whole issue.'];
        }
        var errors = [];
        Array.prototype.forEach.call(document.querySelectorAll('#returnItemRows .return-qty-input'), function (input) {
            var qty = Number(input.value || 0);
            if (!Number.isFinite(qty) || qty < 0 || qty > Number(input.max)) {
                errors.push('Returning quantity cannot exceed the tools still out.');
            }
        });
        return errors;
    }

    function checkPayment() {
        var problems = [];
        if (!(numberValue('amount') > 0)) problems.push('Enter the amount received.');
        if (!textValue('paymentAccount')) problems.push('Choose the cash / bank account receiving the money.');
        return problems;
    }

    function checkAdjustment() {
        return numberValue('adjust_amount') > 0 ? [] : ['Enter the amount to write off.'];
    }

    function checkDamage() {
        var source = textValue('damage_source') || 'store';
        if (source === 'rental') {
            if (!textValue('damageRentalSelect')) return ['Choose the rental the damage happened on.'];
            var item = document.getElementById('damageRentalItem');
            if (!item || item.classList.contains('d-none') || !item.value) {
                return ['Choose the tool line the damage happened on.'];
            }
            return [];
        }
        var problems = [];
        if (!textValue('damageToolSelect')) problems.push('Choose the tool that was damaged or lost.');
        var ticked = document.querySelectorAll('#damageSerialList input:checked').length;
        if (!ticked && !numberValue('damage_qty')) problems.push('Enter a quantity or tick the exact pieces.');
        return problems;
    }

    function checkMaintenanceSend() {
        return document.querySelectorAll('#maintenanceSendList input:checked').length
            ? [] : ['Tick at least one piece to send for maintenance.'];
    }

    function checkMaintenanceReturn() {
        return document.querySelectorAll('#maintenanceReturnList input:checked').length
            ? [] : ['Tick at least one piece coming back from maintenance.'];
    }

    var CHECKS = {
        customer: checkCustomer,
        project: checkProject,
        rental: checkRental,
        lines: checkLines,
        transfer: validateTransfer,
        return_items: checkReturnItems,
        payment: checkPayment,
        adjustment: checkAdjustment,
        damage: checkDamage,
        maintenance_send: checkMaintenanceSend,
        maintenance_return: checkMaintenanceReturn
    };

    /* Which section posts what — again by the names the registry declares. */
    var COLLECTORS = {
        lines: collectLineFields,
        transfer: collectTransferFields,
        return_items: collectReturnFields
    };

    /* ── submit ─────────────────────────────────────────────────────────── */
    form.addEventListener('submit', function (event) {
        if (submitted) {
            event.preventDefault();
            return;
        }
        var entry = currentEntry();
        if (!entry) return;
        showError('');

        var problems = [];
        (entry.checks || []).forEach(function (name) {
            var check = CHECKS[name];
            if (check) problems = problems.concat(check() || []);
        });
        if (problems.length) {
            event.preventDefault();
            showError(problems[0]);
            return;
        }

        var rentalId = '';
        if (entry.picker) {
            var picker = pickerSelect();
            rentalId = picker ? picker.value : '';
        }
        form.action = entry.needs_rental
            ? entry.action.replace('__RENTAL_ID__', encodeURIComponent(rentalId))
            : entry.action;

        (entry.checks || []).forEach(function (name) {
            var collector = COLLECTORS[name];
            if (collector) collector();
        });

        if (entry.confirm && typeof window.confirm === 'function') {
            if (!window.confirm(entry.confirm)) {
                event.preventDefault();
                return;
            }
        }
        submitted = true;
        if (submitButton) {
            submitButton.disabled = true;
            submitButton.classList.add('disabled');
            var caption = submitButton.querySelector('span');
            if (caption) caption.textContent = 'Saving…';
        }
    });

    form.addEventListener('reset', function () {
        submitted = false;
        if (submitButton) submitButton.disabled = false;
        setTimeout(function () {
            showError('');
            // The browser resets native controls; the script-built pickers
            // (store tick list + rows, transfer holders + rows) reset here.
            var storeSearch = document.getElementById('storeToolSearch');
            if (storeSearch) storeSearch.value = '';
            var transferSearch = document.getElementById('transferLinesSearch');
            if (transferSearch) transferSearch.value = '';
            var tbody = document.getElementById('storeSelectedRows');
            if (tbody) tbody.innerHTML = '';
            renderStoreToolList();
            toggleStoreEmpty();
            renderTransferHolders();
            applyPanels();
        }, 0);
    });

    /* ── wire everything ────────────────────────────────────────────────── */
    if (typeSelect) {
        typeSelect.addEventListener('change', function () {
            var entry = currentEntry();
            if (entry) activeGroup = entry.group;
            paintGroupTabs();
            filterTypeOptions();
            applyPanels();
        });
    }
    Array.prototype.forEach.call(groupTabs(), function (tab) {
        tab.addEventListener('click', function () {
            chooseGroup(tab.getAttribute('data-txn-group'));
        });
    });
    Array.prototype.forEach.call(
        document.querySelectorAll('input[name="rentout_from"], input[name="rentout_to"]'),
        function (radio) {
            radio.addEventListener('change', retargetRentOut);
        });
    var storeSearch = document.getElementById('storeToolSearch');
    if (storeSearch) storeSearch.addEventListener('input', renderStoreToolList);
    var storeSelectVisible = document.getElementById('storeSelectVisibleBtn');
    if (storeSelectVisible) storeSelectVisible.addEventListener('click', function () {
        Array.prototype.forEach.call(
            document.querySelectorAll('#storeToolList .store-tool-check'), function (box) {
                if (!box.checked) {
                    box.checked = true;
                    addStoreRow(box.value);
                }
            });
    });
    var transferSearch = document.getElementById('transferLinesSearch');
    if (transferSearch) transferSearch.addEventListener('input', applyTransferSearch);
    var transferSelect = document.getElementById('transferSourceSelect');
    if (transferSelect) transferSelect.addEventListener('change', renderTransferHolders);
    var selectAllHolders = document.getElementById('selectAllTransferHolders');
    if (selectAllHolders) {
        selectAllHolders.addEventListener('change', function () {
            Array.prototype.forEach.call(document.querySelectorAll('#transferHoldersList .transfer-holder-checkbox'), function (box) {
                box.checked = selectAllHolders.checked;
            });
            renderTransferLines();
        });
    }
    var settleChoice = document.getElementById('settle_choice');
    if (settleChoice) settleChoice.addEventListener('change', syncTransferSettlement);
    var billingSelect = document.getElementById('billing_type');
    if (billingSelect) billingSelect.addEventListener('change', syncBilling);
    var damageSource = document.getElementById('damage_source');
    if (damageSource) damageSource.addEventListener('change', syncDamageSource);
    var damageTool = document.getElementById('damageToolSelect');
    if (damageTool) damageTool.addEventListener('change', renderDamageSerials);
    var damageRental = document.getElementById('damageRentalSelect');
    if (damageRental) damageRental.addEventListener('change', function () { loadDamageRentalItems(this.value); });
    var maintenanceTool = document.getElementById('maintenanceToolSelect');
    if (maintenanceTool) maintenanceTool.addEventListener('change', renderMaintenanceSend);
    var dateInput = document.getElementById('transaction_date');
    if (dateInput) dateInput.addEventListener('change', syncDateFields);
    var returnSelect = document.getElementById('returnRentalSelect');
    if (returnSelect) returnSelect.addEventListener('change', function () { loadReturnItems(this.value); syncReturnSettlement(); });
    var paymentSelect = document.getElementById('paymentRentalSelect');
    if (paymentSelect) paymentSelect.addEventListener('change', function () {
        var option = paymentSelect.options[paymentSelect.selectedIndex];
        var info = document.getElementById('paymentRentalInfo');
        if (info) info.textContent = option ? ('Outstanding: ' + (option.getAttribute('data-amount') || '0') + ' PKR') : '';
        var outstanding = document.getElementById('paymentOutstandingInfo');
        if (outstanding) {
            outstanding.textContent = option
                ? ('Remaining outstanding balance: ' + (option.getAttribute('data-amount') || '0') +
                   ' PKR on ' + (option.getAttribute('data-holder') || 'this rental') +
                   '. A payment never moves a tool.')
                : 'Pick a rental to see what is still outstanding.';
        }
        var amount = document.getElementById('amount');
        if (amount && option) amount.placeholder = option.getAttribute('data-amount') || '';
    });
    var adjustSelect = document.getElementById('adjustRentalSelect');
    if (adjustSelect) adjustSelect.addEventListener('change', function () {
        var option = adjustSelect.options[adjustSelect.selectedIndex];
        var info = document.getElementById('adjustRentalInfo');
        if (info) info.textContent = option ? ('Outstanding: ' + (option.getAttribute('data-amount') || '0') + ' PKR') : '';
        var amount = document.getElementById('adjust_amount');
        if (amount && option) amount.max = option.getAttribute('data-amount') || '';
    });

    paintGroupTabs();
    filterTypeOptions();
    applyPanels();
    renderStoreToolList();
    toggleStoreEmpty();
    renderMaintenanceReturn();
    renderDamageSerials();
    renderMaintenanceSend();
    syncDateFields();
})();
