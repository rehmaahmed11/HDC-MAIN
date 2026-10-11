/* HDC Tool Transaction — the one tool transaction form.
 *
 * Reads its data from <script id="toolTxnConfig" type="application/json">
 * (rendered by hdc/routes/tool_rental.py from
 * hdc/services/tool_txn_entry.py) and does four jobs:
 *
 *   1. show the three groups (Rent Out / Return / Maintenance) and, under the
 *      chosen one, its transactions — then show only the panels the chosen
 *      transaction declares, and disable (not just hide) everything else, so a
 *      hidden panel can never post;
 *   2. build/validate the multi-item lines — against the store's real
 *      availability, not a guess — including per-piece (serial) selection;
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

    /* A no-charge rental is a physical issue, not a receivable.  The mode
       comes from the server's billing registry; never infer it from a label. */
    function syncBilling() {
        var select = document.getElementById('billing_type');
        var mode = select && BILLING[select.value];
        var free = !!(mode && !mode.charges_rent);
        var status = document.getElementById('billingStatus');
        if (status && mode) status.textContent = mode.status;
        Array.prototype.forEach.call(document.querySelectorAll('.txn-line-rate, .transfer-rate'), function (input) {
            if (free) {
                if (!input.readOnly) input.dataset.paidRate = input.value;
                input.value = '0';
            } else if (input.readOnly) {
                input.value = input.dataset.paidRate || '';
            }
            input.readOnly = free;
            input.setAttribute('aria-label', free ? 'No charge — rate is zero' : 'Rental rate');
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

    /* ── tool lines (rent out / issue) ──────────────────────────────────── */
    function serialOptionsFor(toolId) {
        var tool = TOOLS_BY_ID[String(toolId)];
        return (tool && tool.serials) || [];
    }

    function buildLineRow() {
        var row = document.createElement('div');
        row.className = 'tool-txn-line';
        row.innerHTML =
            '<div class="row g-2 align-items-end">' +
            '  <div class="col-md-5">' +
            '    <label class="form-label small fw-semibold">Tool (in store)</label>' +
            '    <select class="form-select txn-line-tool" required><option value=""></option></select>' +
            '  </div>' +
            '  <div class="col-4 col-md-2">' +
            '    <label class="form-label small">Quantity</label>' +
            '    <input type="number" class="form-control txn-line-qty" min="0.1" step="0.1" placeholder="Qty">' +
            '  </div>' +
            '  <div class="col-4 col-md-2">' +
            '    <label class="form-label small">Rate</label>' +
            '    <input type="number" class="form-control txn-line-rate" min="0" step="0.01" placeholder="Saved rate">' +
            '  </div>' +
            '  <div class="col-4 col-md-2 d-flex align-items-end">' +
            '    <button type="button" class="btn btn-sm btn-outline-danger txn-line-remove" title="Remove item">' +
            '      <i class="fas fa-trash" aria-hidden="true"></i><span class="visually-hidden">Remove</span>' +
            '    </button>' +
            '  </div>' +
            '  <div class="col-12">' +
            '    <div class="small text-muted txn-line-avail"></div>' +
            '    <select class="form-select form-select-sm txn-line-serials mt-1 d-none" multiple size="3" aria-label="Individual pieces to issue"></select>' +
            '  </div>' +
            '  <div class="col-12">' +
            '    <input type="text" class="form-control form-control-sm txn-line-note" placeholder="Condition / remarks (optional)">' +
            '  </div>' +
            '  <input type="hidden" class="txn-line-id">' +
            '  <input type="hidden" class="txn-line-ids">' +
            '  <input type="hidden" class="txn-line-tool-hidden">' +
            '  <input type="hidden" class="txn-line-note-hidden">' +
            '</div>';

        var toolSelect = row.querySelector('.txn-line-tool');
        TOOLS.forEach(function (tool) {
            var option = document.createElement('option');
            option.value = String(tool.id);
            option.textContent = tool.name + (tool.code ? ' (' + tool.code + ')' : '') +
                ' — ' + tool.availableQty + ' available';
            option.setAttribute('data-avail', String(tool.availableQty));
            option.setAttribute('data-rate', String(tool.rate || 0));
            toolSelect.appendChild(option);
        });
        toolSelect.addEventListener('change', function () { onLineToolChange(row); });
        row.querySelector('.txn-line-qty').addEventListener('input', function () { validateLine(row); });
        row.querySelector('.txn-line-serials').addEventListener('change', function () { onLineSerialsChange(row); });
        row.querySelector('.txn-line-remove').addEventListener('click', function () {
            row.remove();
            if (!document.querySelectorAll('#lineItemsContainer .tool-txn-line').length) addLineRow();
        });
        return row;
    }

    function onLineToolChange(row) {
        var select = row.querySelector('.txn-line-tool');
        var tool = TOOLS_BY_ID[String(select.value)];
        var avail = row.querySelector('.txn-line-avail');
        var serialBox = row.querySelector('.txn-line-serials');
        var rateInput = row.querySelector('.txn-line-rate');
        var chosen = tool ? serialOptionsFor(tool.id) : [];
        serialBox.innerHTML = '';
        chosen.forEach(function (serial) {
            var option = document.createElement('option');
            option.value = String(serial.id);
            option.textContent = serial.serial_number + ' (' + (serial.condition || 'good') + ')';
            serialBox.appendChild(option);
        });
        serialBox.classList.toggle('d-none', !chosen.length);
        if (tool) {
            avail.textContent = 'In store: ' + tool.availableQty + ' ' + (tool.unit || '') +
                (tool.serials.length ? ' · ' + tool.serials.length + ' marked piece(s)' : '');
            if (rateInput && !rateInput.value) rateInput.value = tool.rate || '';
        } else {
            avail.textContent = '';
        }
        validateLine(row);
        syncBilling();
    }

    function onLineSerialsChange(row) {
        var serialBox = row.querySelector('.txn-line-serials');
        var count = serialBox ? serialBox.selectedOptions.length : 0;
        var qty = row.querySelector('.txn-line-qty');
        if (count > 0 && qty) {
            qty.value = String(count);
            qty.readOnly = true;
        } else if (qty) {
            qty.readOnly = false;
        }
        validateLine(row);
    }

    function validateLine(row) {
        var toolSelect = row.querySelector('.txn-line-tool');
        var qty = row.querySelector('.txn-line-qty');
        var avail = row.querySelector('.txn-line-avail');
        row.dataset.invalid = '';
        if (!toolSelect || !toolSelect.value) return;
        var tool = TOOLS_BY_ID[String(toolSelect.value)];
        if (!tool) return;
        var qtyValue = parseFloat(qty.value || '0');
        var serialBox = row.querySelector('.txn-line-serials');
        var serialCount = serialBox ? serialBox.selectedOptions.length : 0;
        if (serialCount > qtyValue + 0.001 || qtyValue > tool.availableQty + 0.001) {
            row.dataset.invalid = '1';
            if (avail) {
                avail.textContent = 'Only ' + tool.availableQty + ' available in store.';
                avail.classList.add('text-danger');
            }
        } else if (avail) {
            avail.classList.remove('text-danger');
        }
    }

    function validateLines() {
        var lines = document.querySelectorAll('#lineItemsContainer .tool-txn-line');
        var messages = [];
        var claimed = {};
        Array.prototype.forEach.call(lines, function (row) {
            var toolSelect = row.querySelector('.txn-line-tool');
            if (!toolSelect || !toolSelect.value) return;
            var tool = TOOLS_BY_ID[String(toolSelect.value)];
            var qty = parseFloat(row.querySelector('.txn-line-qty').value || '0');
            var serialBox = row.querySelector('.txn-line-serials');
            var serialCount = serialBox ? serialBox.selectedOptions.length : 0;
            if (!qty && !serialCount) messages.push('Enter a quantity or tick pieces for ' + tool.name + '.');
            if (qty > tool.availableQty + 0.001) messages.push('Only ' + tool.availableQty + ' of ' + tool.name + ' is in store.');
            if (serialCount > qty + 0.001 && qty > 0) messages.push('More pieces ticked than the quantity for ' + tool.name + '.');
            if (serialBox) {
                Array.prototype.forEach.call(serialBox.selectedOptions, function (option) {
                    if (claimed[option.value]) {
                        messages.push('Piece ' + option.textContent + ' is ticked twice.');
                    }
                    claimed[option.value] = true;
                });
            }
        });
        return messages;
    }

    function collectLineFields() {
        var container = document.getElementById('lineItemsContainer');
        if (!container) return;
        Array.prototype.forEach.call(
            container.querySelectorAll('.txn-line-tool-hidden, .txn-line-qty-hidden, .txn-line-rate-hidden, .txn-line-ids-hidden, .txn-line-note-hidden'),
            function (node) { node.remove(); });
        var lines = container.querySelectorAll('.tool-txn-line');
        Array.prototype.forEach.call(lines, function (row) {
            var toolSelect = row.querySelector('.txn-line-tool');
            if (!toolSelect || !toolSelect.value) return;
            var serialBox = row.querySelector('.txn-line-serials');
            var serialIds = serialBox ? Array.prototype.map.call(serialBox.selectedOptions, function (o) { return o.value; }) : [];
            addHidden(row, 'tool_id[]', toolSelect.value, 'txn-line-tool-hidden');
            // qty[] and rate[] are emitted for every tool_id[] row, even when
            // blank: the create route zips the three lists positionally and
            // tolerates a blank value, but not a missing slot.
            addHidden(row, 'qty[]', row.querySelector('.txn-line-qty').value, 'txn-line-qty-hidden');
            addHidden(row, 'rate[]', row.querySelector('.txn-line-rate').value, 'txn-line-rate-hidden');
            addHidden(row, 'row_serial_ids[]', serialIds.join(','), 'txn-line-ids-hidden');
            var note = row.querySelector('.txn-line-note').value;
            addHidden(row, 'item_notes[]', note, 'txn-line-note-hidden');
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

    function addLineRow() {
        var container = document.getElementById('lineItemsContainer');
        if (!container) return;
        container.appendChild(buildLineRow());
    }

    /* ── transfer: holders and the lines moving out ─────────────────────── */
    function renderTransferHolders() {
        var list = document.getElementById('transferHoldersList');
        var select = document.getElementById('transferSourceSelect');
        if (!list || !select) return;
        list.innerHTML = '';
        var key = select.value;
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
            return;
        }
        var wrap = document.createElement('div');
        wrap.className = 'table-responsive';
        wrap.innerHTML = '<table class="table table-sm table-bordered table-striped align-middle tool-txn-sheet">' +
            '<caption class="caption-top">Tools currently at ' + escapeHtml(sources[0].holder) +
            ' — edit the Move qty column; zero leaves a tool here.</caption>' +
            '<thead><tr><th scope="col">#</th><th scope="col">Rental</th><th scope="col">Tool</th>' +
            '<th scope="col">At source</th><th scope="col">Move qty</th>' +
            '<th scope="col">New rate (PKR)</th><th scope="col">Exact pieces</th>' +
            '<th scope="col">Action</th></tr></thead><tbody></tbody></table>';
        var tbody = wrap.querySelector('tbody');
        var index = 0;
        sources.forEach(function (source) {
            source.lines.forEach(function (line) {
                index++;
                var row = document.createElement('tr');
                row.className = 'transfer-line-row';
                row.innerHTML =
                    '<td>' + index + '</td><td>' + escapeHtml(source.rentalCode) + '</td>' +
                    '<td><strong>' + escapeHtml(line.name) + '</strong><br><small>' + escapeHtml(line.code) + '</small>' +
                    '<input type="hidden" class="transfer-item-id" value="' + Number(line.itemId) + '"></td>' +
                    '<td>' + Number(line.qty) + ' ' + escapeHtml(line.unit || '') + '</td>' +
                    '<td><input type="number" class="form-control form-control-sm transfer-qty" aria-label="Move quantity of ' +
                    escapeHtml(line.name) + '" min="0" step="0.1" max="' + Number(line.qty) + '" value="' + Number(line.qty) + '"></td>' +
                    '<td><input type="number" class="form-control form-control-sm transfer-rate" min="0" step="0.01" value="' +
                    Number(line.dailyRate || line.rate || 0) + '"></td>' +
                    '<td><select class="form-select form-select-sm transfer-serials" multiple size="2" aria-label="Pieces to move"></select></td>' +
                    '<td><button type="button" class="btn btn-sm btn-outline-danger transfer-remove">Leave here</button></td>';
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
                row.querySelector('.transfer-remove').addEventListener('click', function () { row.remove(); });
                tbody.appendChild(row);
            });
        });
        container.appendChild(wrap);
        syncBilling();
    }

    function collectTransferFields() {
        var container = document.getElementById('transferLinesContainer');
        if (!container) return;
        Array.prototype.forEach.call(container.querySelectorAll('.transfer-post'), function (node) { node.remove(); });
        Array.prototype.forEach.call(container.querySelectorAll('.transfer-line-row'), function (row) {
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
        if (!rows.length) return ['Tick at least one holder and keep at least one tool line to transfer.'];
        var moving = 0;
        Array.prototype.forEach.call(rows, function (row) {
            if (parseFloat(row.querySelector('.transfer-qty').value || '0') > 0) moving++;
        });
        if (!moving) return ['Enter a quantity for at least one tool to transfer.'];
        var errors = [];
        Array.prototype.forEach.call(rows, function (row) {
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
        if (!document.querySelector('#lineItemsContainer .tool-txn-line')) {
            return ['Add at least one tool item.'];
        }
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
    var addBtn = document.getElementById('addLineBtn');
    if (addBtn) addBtn.addEventListener('click', addLineRow);
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
    addLineRow();
    renderMaintenanceReturn();
    renderDamageSerials();
    renderMaintenanceSend();
    syncDateFields();
})();
