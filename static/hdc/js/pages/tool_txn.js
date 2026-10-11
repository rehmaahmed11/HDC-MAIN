/* HDC Tool Transaction — the one uniform equipment transaction form.
 *
 * Reads its data from <script id="toolTxnConfig" type="application/json">
 * (rendered by hdc/routes/tool_rental.py from
 * hdc/services/tool_txn_entry.py) and does four jobs:
 *
 *   1. show only the panels the chosen transaction needs, and disable (not
 *      just hide) everything else, so a hidden panel can never post;
 *   2. build/validate the multi-item lines — against the store's real
 *      availability, not a guess — including per-piece (serial) selection;
 *   3. copy the one visible transaction date into the endpoint's own date
 *      field, and stamp the reference / responsible person onto the notes, so
 *      the record that gets created carries the number on the printed form;
 *   4. lock the submit button on the first submit and confirm consequential
 *      transactions, so a double click cannot post twice (the server also
 *      refuses a reference it has already recorded).
 *
 * No business rule lives here: quantities, availability, money and locations
 * are all re-checked by the server routes this form posts to.
 */
(function () {
    'use strict';

    var CONFIG = {};
    var configNode = document.getElementById('toolTxnConfig');
    if (configNode) {
        try { CONFIG = JSON.parse(configNode.textContent || '{}'); } catch (err) { CONFIG = {}; }
    }

    var TYPES = CONFIG.types || [];
    var TOOLS = CONFIG.tools || [];
    var SOURCES = CONFIG.transferSources || [];
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
            document.getElementById('txnFormTitle').textContent = entry.label;
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
        syncDamageSource();
        syncDateFields();
        showError('');
    }

    function syncDestinationPanels() {
        var entry = currentEntry();
        if (!entry) return;
        var customer = document.getElementById('destinationCustomerPanel');
        var site = document.getElementById('destinationSitePanel');
        var renterType = entry.renter_type || null;
        var kind = entry.kind;
        var needsDestination = (kind === 'create' || kind === 'transfer') && !!renterType;
        if (customer) customer.classList.toggle('d-none', !needsDestination || renterType !== 'external');
        if (site) site.classList.toggle('d-none', !needsDestination || renterType !== 'internal');
        if (customer) setEnabled(customer, needsDestination && renterType === 'external');
        if (site) setEnabled(site, needsDestination && renterType === 'internal');
    }

    function syncReturnMode() {
        var entry = currentEntry();
        var hidden = document.getElementById('returnCondition');
        if (entry && entry.return_type && hidden) hidden.value = entry.return_type;
        if (entry && entry.return_type === 'full') {
            Array.prototype.forEach.call(document.querySelectorAll('.return-qty-input'), function (input) {
                input.value = input.getAttribute('data-pending') || input.value;
                input.readOnly = true;
            });
        } else {
            Array.prototype.forEach.call(document.querySelectorAll('.return-qty-input'), function (input) {
                input.readOnly = false;
            });
        }
    }

    function syncTransferSettlement() {
        var select = document.getElementById('settle_choice');
        var fields = document.getElementById('transferPayNowFields');
        if (!select || !fields) return;
        var payNow = select.value === 'pay_now';
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
        var settlement = document.getElementById('returnDateHidden');
        if (settlement) settlement.value = date.value;
    }

    /* ── tool lines (rent out / issue) ──────────────────────────────────── */
    function serialOptionsFor(toolId) {
        var tool = TOOLS_BY_ID[String(toolId)];
        return (tool && tool.serials) || [];
    }

    function buildLineRow() {
        var row = document.createElement('div');
        row.className = 'tool-txn-line';
        var id = 'txnLine' + (lineIndex++);
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
        container.querySelectorAll('.txn-line-tool-hidden, .txn-line-qty-hidden, .txn-line-rate-hidden, .txn-line-ids-hidden, .txn-line-note-hidden').forEach(function (node) {
            node.remove();
        });
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
        var row = buildLineRow();
        container.appendChild(row);
    }

    /* ── transfer: holders and the lines moving out ─────────────────────── */
    function renderTransferHolders() {
        var list = document.getElementById('transferHoldersList');
        var select = document.getElementById('transferSourceSelect');
        if (!list || !select) return;
        list.innerHTML = '';
        var key = select.value;
        var matching = SOURCES.filter(function (source) { return String(source.key) === String(key); });
        if (!matching.length) {
            list.innerHTML = '<span class="small text-muted">Choose a site / holder above.</span>';
            renderTransferLines([]);
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
        if (!sources.length) {
            container.innerHTML = '<span class="small text-muted">Tick a holder to see the tools to move.</span>';
            return;
        }
        sources.forEach(function (source) {
            var heading = document.createElement('div');
            heading.className = 'small fw-semibold mt-2';
            heading.textContent = source.rentalCode + ' · ' + source.holder;
            container.appendChild(heading);
            source.lines.forEach(function (line) {
                var row = document.createElement('div');
                row.className = 'row g-2 align-items-end border rounded p-2 mt-1 transfer-line-row';
                row.innerHTML =
                    '<div class="col-md-4"><div class="small fw-semibold">' + line.name +
                    (line.code ? ' (' + line.code + ')' : '') + '</div>' +
                    '<div class="small text-muted">' + line.qty + ' out · rate ' + line.rate + '</div>' +
                    '<input type="hidden" class="transfer-item-id" value="' + line.itemId + '"></div>' +
                    '<div class="col-4 col-md-2"><label class="form-label small mb-0">Move qty</label>' +
                    '<input type="number" class="form-control form-control-sm transfer-qty" min="0" step="0.1" max="' + line.qty + '" value="' + line.qty + '"></div>' +
                    '<div class="col-4 col-md-2"><label class="form-label small mb-0">New rate</label>' +
                    '<input type="number" class="form-control form-control-sm transfer-rate" min="0" step="0.01" value="' + (line.dailyRate || line.rate) + '"></div>' +
                    '<div class="col-4 col-md-2 d-flex align-items-end">' +
                    '<button type="button" class="btn btn-sm btn-outline-danger transfer-remove">Remove</button></div>' +
                    '<div class="col-12"><select class="form-select form-select-sm transfer-serials" multiple size="2" aria-label="Pieces to move"></select></div>';
                var serialBox = row.querySelector('.transfer-serials');
                (line.serials || []).forEach(function (serial) {
                    var option = document.createElement('option');
                    option.value = String(serial.id);
                    option.textContent = serial.serial_number + ' (' + (serial.condition || 'good') + ')';
                    option.selected = true;
                    serialBox.appendChild(option);
                });
                if (!(line.serials || []).length) serialBox.classList.add('d-none');
                row.querySelector('.transfer-remove').addEventListener('click', function () { row.remove(); });
                container.appendChild(row);
            });
        });
    }

    function collectTransferFields() {
        var container = document.getElementById('transferLinesContainer');
        if (!container) return;
        container.querySelectorAll('.transfer-post').forEach(function (node) { node.remove(); });
        Array.prototype.forEach.call(container.querySelectorAll('.transfer-line-row'), function (row) {
            var qty = parseFloat(row.querySelector('.transfer-qty').value || '0');
            var itemId = row.querySelector('.transfer-item-id').value;
            if (!qty || qty <= 0) return;
            addHidden(row, 'transfer_item_id[]', itemId, 'transfer-post');
            addHidden(row, 'transfer_qty_' + itemId, String(qty), 'transfer-post');
            var rate = row.querySelector('.transfer-rate').value;
            if (rate) addHidden(row, 'transfer_rate_' + itemId, rate, 'transfer-post');
            var serialBox = row.querySelector('.transfer-serials');
            if (serialBox && serialBox.selectedOptions.length) {
                var ids = Array.prototype.map.call(serialBox.selectedOptions, function (o) { return o.value; });
                addHidden(row, 'transfer_serial_ids_' + itemId + '[]', ids.join(','), 'transfer-post');
            }
        });
    }

    function validateTransfer() {
        var rowCount = document.querySelectorAll('#transferLinesContainer .transfer-line-row').length;
        if (!rowCount) return ['Tick at least one holder and keep at least one tool line to transfer.'];
        var moving = 0;
        Array.prototype.forEach.call(document.querySelectorAll('#transferLinesContainer .transfer-line-row'), function (row) {
            if (parseFloat(row.querySelector('.transfer-qty').value || '0') > 0) moving++;
        });
        if (!moving) return ['Enter a quantity for at least one tool to transfer.'];
        return [];
    }

    /* ── returns ────────────────────────────────────────────────────────── */
    var returnRequest = 0;

    function loadReturnItems(rentalId) {
        var box = document.getElementById('returnItemRows');
        if (!box) return;
        if (!rentalId) {
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
        box.innerHTML = '';
        if (!items.length) {
            box.innerHTML = '<div class="small text-muted">Nothing is still out on this rental.</div>';
            return;
        }
        items.forEach(function (item) {
            var row = document.createElement('div');
            row.className = 'tool-txn-line';
            row.innerHTML =
                '<div class="row g-2 align-items-end">' +
                '  <div class="col-md-4"><div class="fw-semibold small">' + item.tool_name +
                ' (' + item.tool_code + ')</div>' +
                '    <div class="small text-muted">Issued ' + item.qty_rented +
                ' · already back ' + item.qty_returned + ' · outstanding ' +
                '    <span class="fw-bold return-outstanding">' + item.qty_pending + '</span></div>' +
                '    <input type="hidden" class="return-item-id" value="' + item.rental_item_id + '"></div>' +
                '  <div class="col-4 col-md-2"><label class="form-label small mb-0">Returning now</label>' +
                '    <input type="number" class="form-control form-control-sm return-qty-input" min="0" step="0.1" ' +
                '           max="' + item.qty_pending + '" data-pending="' + item.qty_pending + '" placeholder="0"></div>' +
                '  <div class="col-8 col-md-3"><label class="form-label small mb-0">Condition</label>' +
                '    <input type="text" class="form-control form-control-sm return-condition" placeholder="Good / damaged…"></div>' +
                '  <div class="col-md-3">' +
                '    <select class="form-select form-select-sm return-serials d-none" multiple size="2" aria-label="Exact pieces coming back"></select>' +
                '  </div>' +
                '</div>';
            row.querySelector('.return-item-id').dataset.pending = String(item.qty_pending);
            var serialBox = row.querySelector('.return-serials');
            fetch('/hdc/api/tool-rental/serials/out-of-store?tool_id=' + encodeURIComponent(item.tool_id) +
                  '&rental_id=' + encodeURIComponent(document.getElementById('returnRentalSelect').value),
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
            box.appendChild(row);
        });
        var hint = document.createElement('div');
        hint.className = 'small text-muted';
        hint.textContent = 'Leave a quantity blank for a tool that did not come back. “Full return” takes everything still outstanding.';
        box.appendChild(hint);
        syncReturnMode();
    }

    function collectReturnFields() {
        var container = document.getElementById('returnItemRows');
        if (!container) return;
        container.querySelectorAll('.return-post').forEach(function (node) { node.remove(); });
        Array.prototype.forEach.call(container.querySelectorAll('.tool-txn-line'), function (row) {
            var itemId = row.querySelector('.return-item-id');
            if (!itemId) return;
            var qty = parseFloat(row.querySelector('.return-qty-input').value || '0');
            var serialBox = row.querySelector('.return-serials');
            var serialIds = serialBox ? Array.prototype.map.call(serialBox.selectedOptions, function (o) { return o.value; }) : [];
            if (!qty && !serialIds.length) return;
            addHidden(row, 'rental_item_id[]', itemId.value, 'return-post');
            addHidden(row, 'qty_returned[]', serialIds.length ? String(serialIds.length) : String(qty), 'return-post');
            addHidden(row, 'condition_notes[]', row.querySelector('.return-condition').value, 'return-post');
            if (serialIds.length) {
                addHidden(row, 'return_serial_ids_' + itemId.value + '[]', serialIds.join(','), 'return-post');
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
        var toolId = toolSelect.value;
        box.innerHTML = '';
        var tool = TOOLS_BY_ID[String(toolId)];
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

    /* ── submit ─────────────────────────────────────────────────────────── */
    function collectAll() {
        collectLineFields();
        collectTransferFields();
        collectReturnFields();
    }

    form.addEventListener('submit', function (event) {
        if (submitted) {
            event.preventDefault();
            return;
        }
        var entry = currentEntry();
        if (!entry) return;
        showError('');
        var problems = [];
        var rentalId = '';

        if (entry.needs_rental) {
            var pickerId = entry.kind === 'return' ? 'returnRentalSelect'
                : (entry.kind === 'payment' ? 'paymentRentalSelect' : 'adjustRentalSelect');
            var picker = document.getElementById(pickerId);
            if (!picker || !picker.value) {
                problems.push('Choose the rental this transaction belongs to.');
                if (picker) picker.focus();
            } else {
                rentalId = picker.value;
            }
        }
        if (entry.kind === 'create' || entry.kind === 'transfer') {
            if (entry.renter_type === 'external') {
                var customer = document.getElementById('txnCustomerNameInput');
                if (!customer || !customer.value.trim()) problems.push('Enter the customer name.');
            } else if (entry.renter_type === 'internal') {
                var project = document.getElementById('txnProjectSelect');
                if (!project || !project.value) problems.push('Choose the site / project.');
            }
        }
        if (entry.kind === 'create') {
            var lineProblems = validateLines();
            if (!document.querySelector('#lineItemsContainer .tool-txn-line')) {
                problems.push('Add at least one tool item.');
            }
            problems = problems.concat(lineProblems);
        }
        if (entry.kind === 'transfer') {
            problems = problems.concat(validateTransfer());
        }
        if (entry.kind === 'return' && entry.return_type !== 'full') {
            var anyQty = false;
            Array.prototype.forEach.call(document.querySelectorAll('#returnItemRows .return-qty-input'), function (input) {
                if (parseFloat(input.value || '0') > 0) anyQty = true;
            });
            if (!anyQty) problems.push('Enter the quantity coming back — or choose Full Return to close the whole issue.');
        }
        if (entry.kind === 'damage_loss') {
            var damageSource = document.getElementById('damage_source').value;
            if (damageSource === 'rental') {
                var damageRental = document.getElementById('damageRentalSelect');
                var damageItem = document.getElementById('damageRentalItem');
                if (!damageRental || !damageRental.value) problems.push('Choose the rental the damage happened on.');
                if (damageItem && damageItem.classList.contains('d-none')) {
                    problems.push('Choose the tool line the damage happened on.');
                } else if (damageItem && !damageItem.value) {
                    problems.push('Choose the tool line the damage happened on.');
                }
            } else {
                var damageTool = document.getElementById('damageToolSelect');
                var tickedPieces = document.querySelectorAll('#damageSerialList input:checked').length;
                var damageQty = parseFloat((document.getElementById('damage_qty') || {}).value || '0');
                if (!damageTool || !damageTool.value) problems.push('Choose the tool that was damaged or lost.');
                if (!tickedPieces && !damageQty) problems.push('Enter a quantity or tick the exact pieces.');
            }
        }
        if (entry.kind === 'maintenance_out') {
            if (!document.querySelectorAll('#maintenanceSendList input:checked').length) {
                problems.push('Tick at least one piece to send for maintenance.');
            }
        }
        if (entry.kind === 'maintenance_in') {
            if (!document.querySelectorAll('#maintenanceReturnList input:checked').length) {
                problems.push('Tick at least one piece coming back from maintenance.');
            }
        }
        if (entry.kind === 'payment') {
            var amount = parseFloat((document.getElementById('amount') || {}).value || '0');
            if (!(amount > 0)) problems.push('Enter the amount received.');
            var account = document.getElementById('paymentAccount');
            if (!account || !account.value) problems.push('Choose the cash / bank account receiving the money.');
        }
        if (problems.length) {
            event.preventDefault();
            showError(problems[0]);
            return;
        }

        if (!entry.needs_rental) {
            form.action = entry.action;
        } else {
            form.action = entry.action.replace('__RENTAL_ID__', encodeURIComponent(rentalId));
        }
        collectAll();

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
    if (typeSelect) typeSelect.addEventListener('change', applyPanels);
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
    if (returnSelect) returnSelect.addEventListener('change', function () { loadReturnItems(this.value); });
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

    applyPanels();
    addLineRow();
    renderMaintenanceReturn();
    renderDamageSerials();
    renderMaintenanceSend();
    syncDateFields();
})();
