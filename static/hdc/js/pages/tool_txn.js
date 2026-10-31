/* HDC Tool Transaction — the one tool transaction form.
 *
 * Reads its data from <script id="toolTxnConfig" type="application/json">
 * (rendered by hdc/routes/tool_rental.py from
 * hdc/services/tool_txn_entry.py) and does four jobs:
 *
 *   1. open the shared entry dialog from one of the three group cards (Rent
 *      Out / Return / Maintenance), select a Return or Maintenance transaction,
 *      and show only the panels that transaction declares. The transaction
 *      selector stays hidden for Rent Out, where the existing From / To choices
 *      select the issue or transfer action. Every other panel is disabled as
 *      well as hidden, so it can never leak into a submit;
 *   2. build/validate the multi-item lines — a searchable tick list of every
 *      issuable tool, with the ticked tools in excel-style rows (tool,
 *      pieces, qty, rate) — against the store's real availability, not a
 *      guess, including per-piece (serial) selection: the Pieces / serial
 *      cell of every sheet (store, transfer, return) is one *tick per piece
 *      marking*, with a search box and a tick-all-shown shortcut, never a
 *      ctrl-click multi-select.  On a return the sheet starts from *who is
 *      returning* and lists everything that holder still holds, each row
 *      carrying the issue it belongs to — one load, several issues;
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
    /* The return sheet: everything every holder still has out, one row per
       rental line, each carrying the issue it belongs to. */
    var RETURN_ROWS = CONFIG.returnRows || [];
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

    /* ── the three entry cards ──────────────────────────────────────────── */
    function groupCards() {
        return document.querySelectorAll('#txnGroupCards [data-txn-group]');
    }

    function paintGroupCards() {
        Array.prototype.forEach.call(groupCards(), function (card) {
            var active = card.getAttribute('data-txn-group') === activeGroup;
            card.classList.toggle('active', active);
            card.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
    }

    /* Show only the chosen group's transactions in the selector. Rent Out
       hides that selector; its existing From / To choices pick issue vs.
       transfer and the destination type. */
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
        paintGroupCards();
        filterTypeOptions();
        applyPanels();
    }

    function syncTransactionField(entry) {
        var field = document.getElementById('txnTypeField');
        if (!field) return;
        var visible = !!entry && entry.group !== 'rent_out';
        field.classList.toggle('d-none', !visible);
        field.setAttribute('aria-hidden', visible ? 'false' : 'true');
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
        syncTransactionField(entry);
        if (entry) {
            var group = groupFor(entry.group);
            var title = document.getElementById('txnFormTitle');
            var help = document.getElementById('txnTypeHelp');
            if (title) title.textContent = group ? group.label + ' · ' + entry.label : entry.label;
            if (help) help.textContent = entry.help || '';
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
            var check = input.closest('tr').querySelector('.return-tool-check');
            if (check) { check.disabled = full; check.checked = full || Number(input.value) > 0; }

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

    /* Rent is optional and always settles ONE issue: the holder's issues that
       still owe something are offered here, and the cash fields appear only
       when one of them is chosen.  A no-charge holder has no rent to collect,
       whatever the form was left holding. */
    function syncReturnSettlement() {
        var select = document.getElementById('returnSettleRental');
        var fields = document.getElementById('returnCashFields');
        var status = document.getElementById('returnSettlementStatus');
        var holder = returnHolderSelect();
        var rows = returnRowsForHolder(holder ? holder.value : '');
        var entries = returnSettleEntries(rows).filter(function (entry) {
            return entry.amountDue > 0 && !(BILLING[entry.billingType] && !BILLING[entry.billingType].charges_rent);
        });
        if (select) {
            var previous = select.value;
            select.innerHTML = '';
            select.add(new Option(entries.length ? 'Choose an issue' : 'No rent to collect', ''));
            entries.forEach(function (entry) {
                select.add(new Option(entry.code + ' · ' + Number(entry.amountDue).toFixed(2) + ' PKR due',
                                      String(entry.id)));
            });
            var keep = entries.some(function (entry) { return String(entry.id) === String(previous); });
            select.value = keep ? previous : (entries.length === 1 ? String(entries[0].id) : '');
            select.disabled = !entries.length || !!select.closest('.d-none');
        }
        var chosen = entries.filter(function (entry) {
            return select && String(entry.id) === String(select.value);
        })[0];
        if (status) {
            status.textContent = !rows.length
                ? 'Choose a client or project / site to see its rent status.'
                : (entries.length
                    ? (chosen ? moneyStatus(chosen.billingType, chosen.amountDue)
                              : 'Choose the issue the rent is being collected against.')
                    : 'No rent due on this holder’s issues — a return here only moves the tools.');
        }
        if (fields) {
            fields.classList.toggle('d-none', !chosen);
            setEnabled(fields, !!chosen && !fields.closest('.d-none'));
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
        // A block stays disabled while its own panel is hidden: switching the
        // source must never re-enable fields on a panel the operator cannot see.
        if (storeFields) {
            storeFields.classList.toggle('d-none', fromRental);
            setEnabled(storeFields, !fromRental && !storeFields.closest('.d-none'));
        }
        if (rentalField) {
            rentalField.classList.toggle('d-none', !fromRental);
            setEnabled(rentalField, fromRental && !rentalField.closest('.d-none'));
        }
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
       Steps 1 + 2 of renting out. The two answers pick the transaction: the
       store means a create, a site or a customer means a transfer — and To
       decides the renter type. The option is found in the registry by kind +
       renter type, never by a hard-coded key. */
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
        paintGroupCards();
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

    /* If the operator chooses To before From, prefer whichever source type
       actually holds tools so the location list is never empty by default. */
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

    /* ── the piece picker: tick the exact pieces ─────────────────────────
       One control for every place the form asks *which individual pieces*
       are moving — the Pieces / serial cell of the store sheet, the Exact
       pieces cell of the transfer sheet and of the return sheet.  It is one
       checkbox per serial marking (a plain multi-select needed ctrl-clicks
       and simply had no tick to give), with a search box when a tool has
       many markings and a "tick all shown" shortcut.

       Nothing about posting changes: the same hidden fields carry the ticked
       ids the routes already read (``row_serial_ids[]``,
       ``transfer_serial_ids_<item>[]``, ``return_serial_ids_<item>[]``), so a
       ticked piece means exactly what a ctrl-clicked option meant before. */
    //: Above this many markings a picker offers its own search box.
    var PIECE_SEARCH_FROM = 8;

    function buildPiecePicker(pieces, options) {
        options = options || {};
        pieces = pieces || [];
        if (!pieces.length) return null;
        var wrap = document.createElement('div');
        wrap.className = 'pieces-picker' + (options.className ? ' ' + options.className : '');
        wrap.setAttribute('data-piece-picker', '');

        var label = options.label || 'Pieces';
        var bar = document.createElement('div');
        bar.className = 'pieces-picker-bar';
        if (pieces.length > PIECE_SEARCH_FROM) {
            var search = document.createElement('input');
            search.type = 'search';
            search.className = 'form-control form-control-sm pieces-picker-search';
            search.placeholder = options.searchPlaceholder || 'Find a piece…';
            search.setAttribute('aria-label', 'Find a piece of ' + label);
            search.addEventListener('input', function () { applyPieceSearch(wrap); });
            bar.appendChild(search);
        }
        var allButton = document.createElement('button');
        allButton.type = 'button';
        allButton.className = 'btn btn-sm btn-outline-secondary pieces-picker-all';
        allButton.addEventListener('click', function () {
            var tick = allButton.getAttribute('data-tick') !== '0';
            piecePickerVisibleRows(wrap).forEach(function (row) {
                row.querySelector('.pieces-pick-box').checked = tick;
            });
            paintPiecePicker(wrap);
            if (options.onChange) options.onChange(piecePickerIds(wrap));
        });
        bar.appendChild(allButton);
        var count = document.createElement('span');
        count.className = 'pieces-picker-count small text-muted';
        count.setAttribute('role', 'status');
        count.setAttribute('aria-live', 'polite');
        bar.appendChild(count);
        wrap.appendChild(bar);

        var list = document.createElement('div');
        list.className = 'pieces-picker-list';
        list.setAttribute('role', 'group');
        list.setAttribute('aria-label', label);
        pieces.forEach(function (piece) {
            var row = document.createElement('label');
            row.className = 'pieces-pick';
            row.setAttribute('data-search', (String(piece.serial_number || '') + ' ' +
                String(piece.condition || '')).toLowerCase());
            var box = document.createElement('input');
            box.type = 'checkbox';
            box.className = 'form-check-input pieces-pick-box';
            box.value = String(piece.id);
            box.addEventListener('change', function () {
                paintPiecePicker(wrap);
                if (options.onChange) options.onChange(piecePickerIds(wrap));
            });
            var name = document.createElement('span');
            name.className = 'pieces-pick-name';
            name.textContent = String(piece.serial_number || '');
            var note = document.createElement('span');
            note.className = 'pieces-pick-note';
            note.textContent = String(piece.condition || 'good');
            row.appendChild(box);
            row.appendChild(name);
            row.appendChild(note);
            list.appendChild(row);
        });
        wrap.appendChild(list);
        var empty = document.createElement('div');
        empty.className = 'pieces-picker-empty small text-muted d-none';
        empty.textContent = options.emptyText || 'No piece matches this search.';
        wrap.appendChild(empty);

        paintPiecePicker(wrap);
        return wrap;
    }

    function piecePickerBoxes(picker) {
        if (!picker) return [];
        return Array.prototype.slice.call(picker.querySelectorAll('.pieces-pick-box'));
    }

    /* The ticked pieces, in the order they are listed — the ids the hidden
       fields post and the count the quantity cell follows. */
    function piecePickerIds(picker) {
        return piecePickerBoxes(picker).filter(function (box) {
            return box.checked;
        }).map(function (box) { return box.value; });
    }

    function piecePickerVisibleRows(picker) {
        return Array.prototype.filter.call(picker.querySelectorAll('.pieces-pick'), function (row) {
            return !row.classList.contains('pieces-pick-off');
        });
    }

    function applyPieceSearch(picker) {
        var input = picker.querySelector('.pieces-picker-search');
        var query = input ? input.value.trim().toLowerCase() : '';
        Array.prototype.forEach.call(picker.querySelectorAll('.pieces-pick'), function (row) {
            var hay = row.getAttribute('data-search') || '';
            row.classList.toggle('pieces-pick-off', !!query && hay.indexOf(query) === -1);
        });
        paintPiecePicker(picker);
    }

    function paintPiecePicker(picker) {
        if (!picker) return;
        var boxes = piecePickerBoxes(picker);
        var ticked = boxes.filter(function (box) { return box.checked; }).length;
        var visible = piecePickerVisibleRows(picker);
        var allTicked = visible.length > 0 && visible.every(function (row) {
            return row.querySelector('.pieces-pick-box').checked;
        });
        var count = picker.querySelector('.pieces-picker-count');
        if (count) count.textContent = ticked + ' of ' + boxes.length + ' ticked';
        var allButton = picker.querySelector('.pieces-picker-all');
        if (allButton) {
            allButton.setAttribute('data-tick', allTicked ? '0' : '1');
            allButton.textContent = allTicked ? 'Clear all shown' : 'Tick all shown';
            allButton.disabled = !visible.length;
        }
        var empty = picker.querySelector('.pieces-picker-empty');
        if (empty) empty.classList.toggle('d-none', visible.length > 0);
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
        var picker = buildPiecePicker(tool.serials || [], {
            className: 'store-line-serials',
            label: 'Pieces of ' + tool.name,
            onChange: function () { onStoreSerialsChange(row); }
        });
        if (picker) {
            piecesCell.appendChild(picker);
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

    /* Ticked pieces are the quantity: the row posts exactly those markings,
       so a typed quantity would only contradict them.  Clearing every tick
       hands the quantity cell back to the operator. */
    function onStoreSerialsChange(row) {
        var picker = row.querySelector('.store-line-serials');
        var qty = row.querySelector('.store-line-qty');
        if (!picker || !qty) return;
        var ids = piecePickerIds(picker);
        var toolCheck = row.querySelector('.return-tool-check');
        if (toolCheck) toolCheck.checked = ids.length > 0 || Number(qty.value) > 0;
        if (ids.length) {
            qty.value = String(ids.length);
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
            var picker = row.querySelector('.store-line-serials');
            var serialCount = picker ? piecePickerIds(picker).length : 0;
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
            var picker = row.querySelector('.store-line-serials');
            var serialIds = picker ? piecePickerIds(picker) : [];
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
                    '<td class="col-pieces transfer-pieces"></td>';
                var picker = buildPiecePicker(line.serials || [], {
                    className: 'transfer-serials',
                    label: 'Pieces of ' + line.name + ' to move',
                    // Nothing is ticked to begin with: a tick is a deliberate
                    // "move *these* pieces", and a quantity typed by hand must
                    // keep deciding the movement until the operator ticks.
                    onChange: function (ids) {
                        if (!ids.length) return;
                        row.querySelector('.transfer-qty').value = String(ids.length);
                        var pick = row.querySelector('.transfer-pick');
                        if (pick && !pick.checked) {
                            // Ticking pieces is choosing the row: never leave a
                            // ticked piece on a row that would not be posted.
                            pick.checked = true;
                            updateTransferPickAll();
                            updateTransferCount();
                        }
                    }
                });
                if (picker) row.querySelector('.transfer-pieces').appendChild(picker);
                else row.querySelector('.transfer-pieces').innerHTML =
                    '<span class="small text-muted">Qty only</span>';
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
            var picker = row.querySelector('.transfer-serials');
            var ids = picker ? piecePickerIds(picker) : [];
            if (ids.length) {
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
            var picker = row.querySelector('.transfer-serials');
            var ids = picker ? piecePickerIds(picker) : [];
            if (!Number.isFinite(qty) || qty < 0 || qty > max) errors.push('Move quantity must be between 0 and the stock at source.');
            if (ids.length && ids.length !== qty) {
                errors.push('Move quantity must match the number of exact pieces ticked.');
            }
        });
        if (textValue('settle_choice') === 'pay_now' && !textValue('transferAccount') &&
            tickedTransferSources().some(function (source) { return Number(source.amountDue || 0) > 0; })) {
            errors.push('Choose the cash / bank account receiving rent from the previous holder.');
        }
        return errors;
    }

    /* ── returns ──────────────────────────────────────────────────────────
       A return starts from *who is returning*, and the sheet then shows every
       tool that site or customer still holds — across all of its open issues.
       No issue picker stands between the two: the issue a row settles is
       printed under the tool's code, so one load can close several issues.  A site with three open issues is
       one place to send the truck to, so one submission can close all three:
       each row carries its own issue, and the server writes one return record
       per issue in a single database transaction. */
    function returnHolderSelect() {
        return document.getElementById('returnHolderSelect');
    }

    function returnRowsForHolder(key) {
        if (!key) return [];
        return RETURN_ROWS.filter(function (row) { return row.holderKey === key; });
    }

    /* One entry per issue among these rows, with what that issue still owes. */
    function returnSettleEntries(rows) {
        var byRental = {};
        var entries = [];
        rows.forEach(function (row) {
            var key = String(row.rentalId);
            if (byRental[key]) return;
            byRental[key] = {
                id: row.rentalId,
                code: row.rentalCode,
                billingType: row.billingType || '',
                amountDue: Number(row.amountDue || 0)
            };
            entries.push(byRental[key]);
        });
        return entries;
    }

    function returnPiecePicker(row, item) {
        var cell = row.querySelector('.return-pieces');
        if (!cell) return;
        var serials = (item.serials || []).map(function (serial) {
            return { id: serial.id, serial_number: serial.serialNumber,
                     condition: serial.condition || 'good' };
        });
        var picker = buildPiecePicker(serials, {
            className: 'return-serials',
            label: 'Pieces of ' + item.toolName + ' coming back',
            onChange: function () { syncReturnPieceQty(row); }
        });
        if (picker) cell.appendChild(picker);
        else cell.innerHTML = '<span class="small text-muted">No serial markings for this tool — use the quantity.</span>';
    }

    function renderReturnSheet() {
        var box = document.getElementById('returnItemRows');
        if (!box) return;
        var holder = returnHolderSelect();
        var rows = returnRowsForHolder(holder ? holder.value : '');
        var info = document.getElementById('returnRentalInfo');
        var issues = returnSettleEntries(rows).length;
        var pending = rows.reduce(function (sum, row) { return sum + Number(row.pending || 0); }, 0);
        if (info) {
            info.textContent = rows.length
                ? (pending + ' piece(s) still out with this holder across ' + issues + ' issue(s).')
                : '';
        }
        box.innerHTML = '';
        if (!rows.length) {
            box.innerHTML = '<div class="small text-muted">Choose a client or project / site to see every tool still out with them.</div>';
            syncReturnSettlement();
            return;
        }
        var wrap = document.createElement('div');
        wrap.className = 'table-responsive';
        wrap.innerHTML = '<table class="table table-sm table-bordered table-striped align-middle tool-txn-sheet" id="returnSheet">' +
            '<caption class="caption-top">Tools still at the selected site / customer</caption>' +
            '<thead><tr><th scope="col" class="col-pick">Select</th><th scope="col" class="col-idx">#</th>' +
            '<th scope="col">Tool</th>' +
            '<th scope="col" class="col-qty">Issued</th><th scope="col" class="col-qty">Already back</th>' +
            '<th scope="col" class="col-qty">Still out</th><th scope="col" class="col-qty">Returning now</th>' +
            '<th scope="col">Condition</th><th scope="col" class="col-pieces">Exact pieces</th>' +
            '</tr></thead><tbody></tbody></table>';
        var tbody = wrap.querySelector('tbody');
        rows.forEach(function (item, index) {
            var row = document.createElement('tr');
            row.className = 'tool-txn-line';
            var pendingQty = Number(item.pending);
            row.innerHTML =
                '<td><input type="checkbox" class="form-check-input return-tool-check" aria-label="Return ' + escapeHtml(item.toolName) + '"></td>' +
                '<td>' + (index + 1) + '</td>' +
                '<td><strong>' + escapeHtml(item.toolName) + '</strong><br><small>' + escapeHtml(item.toolCode) + '</small>' +
                '<br><small class="text-muted">Issue <span class="tool-txn-mono">' + escapeHtml(item.rentalCode) + '</span></small>' +
                '<input type="hidden" class="return-item-id" value="' + Number(item.itemId) + '">' +
                '<input type="hidden" class="return-rental-id" value="' + Number(item.rentalId) + '">' +
                '<input type="hidden" class="return-location-key" value="' + escapeHtml(item.locationKey || '') + '"></td>' +
                '<td>' + Number(item.issued) + '</td><td>' + Number(item.returned) + '</td>' +
                '<td class="fw-bold return-outstanding">' + pendingQty + '</td>' +
                '<td><input type="number" class="form-control form-control-sm return-qty-input" aria-label="Returning ' +
                escapeHtml(item.toolName) + '" min="0" step="0.1" max="' + pendingQty +
                '" data-pending="' + pendingQty + '" placeholder="0"></td>' +
                '<td><input type="text" class="form-control form-control-sm return-condition" aria-label="Condition" placeholder="Condition / notes"></td>' +
                '<td class="col-pieces return-pieces"></td>';
            row.querySelector('.return-item-id').dataset.pending = String(pendingQty);
            var toolCheck = row.querySelector('.return-tool-check');
            var quantity = row.querySelector('.return-qty-input');
            toolCheck.addEventListener('change', function () {
                quantity.value = toolCheck.checked ? String(pendingQty) : '';
                if (!toolCheck.checked) {
                    Array.prototype.forEach.call(row.querySelectorAll('.pieces-pick-box'), function (box) { box.checked = false; });
                    quantity.readOnly = false;
                }
                syncReturnSettlement();
            });
            quantity.addEventListener('input', function () { toolCheck.checked = Number(quantity.value) > 0; });
            returnPiecePicker(row, item);
            tbody.appendChild(row);
        });
        box.appendChild(wrap);
        var hint = document.createElement('div');
        hint.className = 'small text-muted';
        hint.textContent = 'Tick a tool to add it to this return; adjust its quantity or tick the exact pieces. Untick to remove it. “Full Return” takes everything still out, from every issue listed.';
        box.appendChild(hint);
        syncReturnMode();
        syncReturnSettlement();
    }

    /* Ticked pieces are the quantity coming back; untick them all and the
       typed quantity decides again.  A *full* return takes everything still
       outstanding, so its quantity cells stay exactly as the panel set them. */
    function syncReturnPieceQty(row) {
        var picker = row.querySelector('.return-serials');
        var qty = row.querySelector('.return-qty-input');
        if (!picker || !qty) return;
        var entry = currentEntry();
        if (entry && entry.return_type === 'full') return;
        var ids = piecePickerIds(picker);
        var toolCheck = row.querySelector('.return-tool-check');
        if (toolCheck) toolCheck.checked = ids.length > 0 || Number(qty.value) > 0;
        if (ids.length) {
            qty.value = String(ids.length);
            qty.readOnly = true;
        } else {
            qty.readOnly = false;
        }
    }

    function collectReturnFields() {
        var container = document.getElementById('returnItemRows');
        if (!container) return;
        Array.prototype.forEach.call(container.querySelectorAll('.return-post'), function (node) { node.remove(); });
        Array.prototype.forEach.call(container.querySelectorAll('.tool-txn-line'), function (row) {
            var itemId = row.querySelector('.return-item-id');
            if (!itemId) return;
            var qty = parseFloat(row.querySelector('.return-qty-input').value || '0');
            var picker = row.querySelector('.return-serials');
            var serialIds = picker ? piecePickerIds(picker) : [];
            if (!qty && !serialIds.length) return;
            var rentalId = row.querySelector('.return-rental-id');
            if (rentalId) addHidden(row.querySelector('td'), 'return_rental_id[]', rentalId.value, 'return-post');
            addHidden(row.querySelector('td'), 'rental_item_id[]', itemId.value, 'return-post');
            addHidden(row.querySelector('td'), 'qty_returned[]', serialIds.length ? String(serialIds.length) : String(qty), 'return-post');
            addHidden(row.querySelector('td'), 'condition_notes[]', row.querySelector('.return-condition').value, 'return-post');
            addHidden(row.querySelector('td'), 'from_location_key[]', row.querySelector('.return-location-key').value, 'return-post');
            // Parallel lists keep split lines distinct even within one holder.
            addHidden(row.querySelector('td'), 'return_serial_ids[]', serialIds.join(','), 'return-post');
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

    /* Who is returning comes before what is coming back: without a holder the
       sheet has nothing to list, and the server would have no rows to post. */
    function checkReturnHolder() {
        var holder = document.getElementById('returnHolderSelect');
        if (!holder || !holder.value) {
            if (holder) holder.focus();
            return ['Choose who is returning the tools (client or project / site).'];
        }
        if (!document.querySelectorAll('#returnItemRows .tool-txn-line').length) {
            return ['Nothing is still out with this holder — pick another one.'];
        }
        return [];
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
        return_holder: checkReturnHolder,
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

    /* ── posting: the entry goes in the background, the dialogue box reports it ──
       A refused entry must leave the form exactly as the operator filled it in,
       so the browser never navigates away from it. The post is sent with fetch;
       the server answers with the outcome and its messages (see
       _txn_post_answers_as_data in hdc/routes/tool_rental.py), and that answer
       decides what the entry-result dialogue box says:
         saved      → "Entry Saved"; OK clears the form for the next entry
         duplicate  → "Already Recorded"; OK clears the form as well
         failed     → "Entry Not Saved"; the form stays as it is, fix the options
         notice     → nothing was posted; the form stays as it is              */
    var SAVING_LABEL = 'Saving…';
    var NOT_SAVED_NOTE = 'Everything you entered is still on the form. Fix the options listed, then save again.';

    function setSaving(saving) {
        if (submitButton) {
            submitButton.disabled = saving;
            submitButton.classList.toggle('disabled', saving);
        }
        // Clear would wipe the entry that is on its way; it waits for the answer.
        var reset = document.getElementById('txnResetButton');
        if (reset) reset.disabled = saving;
        if (submitLabel) {
            var entry = currentEntry();
            submitLabel.textContent = saving ? SAVING_LABEL
                : (entry ? entry.submit_label : (submitLabel.textContent || 'Save'));
        }
    }

    /* Run `then` once the operator has acknowledged the dialogue box (or at
       once when the dialogue box cannot be shown). */
    function whenAcknowledged(then) {
        var node = document.getElementById('hdcResultDialog');
        if (!then) return;
        if (!node || !window.bootstrap) { then(); return; }
        node.addEventListener('hidden.bs.modal', function once() {
            node.removeEventListener('hidden.bs.modal', once);
            then();
        });
    }

    function showResult(result, then) {
        if (!window.hdcDialog) {
            window.alert([result.title].concat(result.lines || []).join('\n'));
            if (then) then();
            return;
        }
        hdcDialog.show({
            kind: result.kind,
            title: result.title,
            lines: result.lines,
            note: result.note,
            cta: result.cta
        });
        whenAcknowledged(then);
    }

    function clearForNextEntry() {
        // A recorded entry changes the stock and the holders on the page, and
        // its reference is spent, so the page is loaded again for the next one.
        window.location.reload();
    }

    function finishPost(status, answer) {
        var outcome = (answer && answer.outcome) || '';
        var lines = ((answer && answer.items) || []).map(function (item) { return item.text; });

        if (outcome === 'saved') {
            showResult({
                kind: 'success',
                title: 'Entry Saved',
                lines: lines.length ? lines : ['The transaction was recorded.'],
                note: 'The form is cleared for the next entry.',
                cta: 'OK'
            }, clearForNextEntry);
            return;
        }
        if (outcome === 'duplicate') {
            showResult({
                kind: 'warning',
                title: 'Already Recorded',
                lines: lines,
                note: 'The form is cleared for the next entry.',
                cta: 'OK'
            }, clearForNextEntry);
            return;
        }

        // Nothing was recorded: unlock the form and keep every entry on it.
        submitted = false;
        setSaving(false);
        if (outcome === 'signed_out') {
            showResult({
                kind: 'danger',
                title: 'Entry Not Saved',
                lines: ['Your session has ended, so this entry was not recorded.'],
                note: 'Log in again in another tab, then come back and save. The form has not been cleared.',
                cta: 'Fix Entry'
            });
            return;
        }
        if (!lines.length) {
            if (status === 0) {
                lines = ['The server could not be reached. Nothing was recorded; check the connection and save again.'];
            } else if (status === 403) {
                lines = ['You do not have permission to record this entry.'];
            } else if (status >= 400) {
                lines = ['The server refused this entry (error ' + status + '). Nothing was recorded.'];
            } else {
                lines = ['The server did not confirm this entry, so nothing was recorded.'];
            }
        }
        showError(lines[0]);
        showResult({
            kind: outcome === 'failed' || !outcome ? 'danger' : 'warning',
            title: 'Entry Not Saved',
            lines: lines,
            note: NOT_SAVED_NOTE,
            cta: outcome === 'failed' || !outcome ? 'Fix Entry' : 'OK'
        });
    }

    function postEntry(action) {
        fetch(action, {
            method: 'POST',
            body: new FormData(form),
            credentials: 'same-origin',
            // The header asks the server for the outcome as data, not a redirect.
            headers: { 'Accept': 'application/json', 'X-HDC-Entry-Dialog': '1' }
        }).then(function (response) {
            // The server's answer is JSON; anything else (an error page) is
            // reported by its status below.
            return response.json().catch(function () { return null; }).then(function (answer) {
                return { status: response.status, answer: answer };
            });
        }, function () {
            return { status: 0, answer: null };
        }).then(function (result) {
            finishPost(result.status, result.answer);
        });
    }

    /* ── submit ─────────────────────────────────────────────────────────── */
    form.addEventListener('submit', function (event) {
        // Every save is sent by postEntry(); the browser never submits the form.
        event.preventDefault();
        if (submitted) return;
        var entry = currentEntry();
        if (!entry) return;
        showError('');

        var problems = [];
        (entry.checks || []).forEach(function (name) {
            var check = CHECKS[name];
            if (check) problems = problems.concat(check() || []);
        });
        if (problems.length) {
            showError(problems[0]);
            showResult({
                kind: 'danger',
                title: 'Entry Not Saved',
                lines: problems.length === 1
                    ? ['Fix this before saving:', problems[0]]
                    : [problems.length + ' things need attention before saving:'].concat(problems),
                note: NOT_SAVED_NOTE,
                cta: 'Fix Entry'
            });
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
            if (!window.confirm(entry.confirm)) return;
        }
        submitted = true;
        setSaving(true);
        postEntry(form.action);
    });

    form.addEventListener('reset', function () {
        submitted = false;
        if (submitButton) {
            submitButton.disabled = false;
            submitButton.classList.remove('disabled');
        }
        setTimeout(function () {
            showError('');
            // The browser resets native controls; the script-built pickers
            // (store tick list + rows, transfer holders + rows, the return
            // sheet) reset here.
            var clearReturnHolder = document.getElementById('returnHolderSelect');
            if (clearReturnHolder) {
                clearReturnHolder.value = '';
                clearReturnHolder.dispatchEvent(new Event('change'));
            }
            var storeSearch = document.getElementById('storeToolSearch');
            if (storeSearch) storeSearch.value = '';
            var transferSearch = document.getElementById('transferLinesSearch');
            if (transferSearch) transferSearch.value = '';
            var tbody = document.getElementById('storeSelectedRows');
            if (tbody) tbody.innerHTML = '';
            renderStoreToolList();
            toggleStoreEmpty();
            renderTransferHolders();
            var entry = currentEntry();
            if (!entry || entry.group !== activeGroup) {
                var first = TYPES.filter(function (t) { return t.group === activeGroup; })[0];
                if (first && typeSelect) typeSelect.value = first.key;
            }
            paintGroupCards();
            filterTypeOptions();
            applyPanels();
        }, 0);
    });

    /* ── wire everything ────────────────────────────────────────────────── */
    if (typeSelect) {
        typeSelect.addEventListener('change', function () {
            var entry = currentEntry();
            if (entry) activeGroup = entry.group;
            paintGroupCards();
            filterTypeOptions();
            applyPanels();
        });
    }
    Array.prototype.forEach.call(groupCards(), function (card) {
        card.addEventListener('click', function () {
            chooseGroup(card.getAttribute('data-txn-group'));
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
    var returnHolder = document.getElementById('returnHolderSelect');
    if (returnHolder) returnHolder.addEventListener('change', renderReturnSheet);
    var returnSettle = document.getElementById('returnSettleRental');
    if (returnSettle) returnSettle.addEventListener('change', syncReturnSettlement);
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

    renderReturnSheet();
    paintGroupCards();
    filterTypeOptions();
    applyPanels();
    renderStoreToolList();
    toggleStoreEmpty();
    renderMaintenanceReturn();
    renderDamageSerials();
    renderMaintenanceSend();
    syncDateFields();
})();
