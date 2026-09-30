/* Exact record permissions: no implied children, edits, or deletion grants. */
(function () {
    'use strict';

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }
    function button(text, className) {
        var node = el('button', className, text);
        node.type = 'button';
        return node;
    }

    document.querySelectorAll('[data-record-editor]').forEach(function (editor) {
        var form = editor.closest('form');
        var scope = editor.querySelector('[name="record_scope_enabled"]');
        var controls = editor.querySelector('[data-record-controls]');
        var type = editor.querySelector('[data-record-type]');
        var search = editor.querySelector('[data-record-search]');
        var results = editor.querySelector('[data-record-results]');
        var grants = editor.querySelector('[data-record-grants]');
        var next = editor.querySelector('[data-record-next]');
        var status = editor.querySelector('[data-record-status]');
        var custom = form.querySelector('[name="custom_permissions"]');
        var requestNumber = 0;
        var nextPage = 1;

        function updateCount() {
            var count = grants.querySelectorAll('[data-record-mode="read"]:checked').length;
            editor.querySelector('[data-record-count]').textContent = count + ' selected record' + (count === 1 ? '' : 's');
            editor.querySelector('[data-record-empty]').hidden = grants.children.length > 0;
        }
        function activate() {
            controls.disabled = !scope.checked;
            if (scope.checked && custom) custom.checked = true;
        }
        scope.addEventListener('change', activate);
        if (custom) custom.addEventListener('change', function () {
            if (scope.checked && !custom.checked) {
                custom.checked = true;
                status.textContent = 'Strict data mode requires custom page permissions. Disable strict mode first to restore role defaults.';
            }
        });
        activate();
        updateCount();

        function cardFor(resource) {
            return Array.from(grants.children).find(function (card) {
                return card.getAttribute('data-record-resource') === resource;
            });
        }
        function ensureCard(resource, label) {
            var card = cardFor(resource);
            if (card) return card;
            card = el('section', 'record-grant-card mb-2');
            card.setAttribute('data-record-resource', resource);
            var header = el('div', 'd-flex justify-content-between align-items-center gap-2 mb-2');
            header.appendChild(el('strong', '', label));
            var createLabel = el('label', 'small d-flex align-items-center gap-2 mb-0');
            var create = el('input', 'form-check-input mt-0');
            create.type = 'checkbox';
            create.name = 'record_create_' + resource;
            create.value = '1';
            create.setAttribute('data-record-create-grant', '');
            createLabel.appendChild(create);
            createLabel.appendChild(el('span', '', 'Create new'));
            header.appendChild(createLabel);
            card.appendChild(header);
            var heading = el('div', 'record-row record-row-head small text-muted');
            ['Record', 'Read', 'Edit', 'Delete/void', ''].forEach(function (text) {
                heading.appendChild(el('span', '', text));
            });
            card.appendChild(heading);
            var rows = el('div');
            rows.setAttribute('data-record-rows', '');
            card.appendChild(rows);
            grants.appendChild(card);
            return card;
        }
        function selected(resource, id) {
            var card = cardFor(resource);
            return card && Array.from(card.querySelectorAll('[data-record-id]')).some(function (row) {
                return row.getAttribute('data-record-id') === String(id);
            });
        }
        function addRecord(resource, label, record) {
            if (selected(resource, record.id)) return;
            var card = ensureCard(resource, label);
            var row = el('div', 'record-row');
            row.setAttribute('data-record-id', String(record.id));
            row.appendChild(el('span', 'record-label', record.label));
            ['read', 'write', 'delete'].forEach(function (mode) {
                var toggle = el('label', 'record-toggle');
                var input = el('input', 'form-check-input mt-0');
                input.type = 'checkbox';
                input.name = 'record_' + mode + '_' + resource;
                input.value = String(record.id);
                input.checked = mode === 'read';
                input.setAttribute('data-record-mode', mode);
                input.setAttribute('aria-label', (mode === 'write' ? 'Edit' : mode === 'delete' ? 'Delete or void' : 'Read') + ' ' + record.label);
                toggle.appendChild(input);
                row.appendChild(toggle);
            });
            var remove = button('×', 'btn btn-sm btn-link text-danger p-1');
            remove.setAttribute('data-record-remove', '');
            remove.setAttribute('aria-label', 'Remove ' + record.label);
            row.appendChild(remove);
            card.querySelector('[data-record-rows]').appendChild(row);
            updateCount();
        }
        grants.addEventListener('change', function (event) {
            var input = event.target;
            var mode = input.getAttribute('data-record-mode');
            var row = input.closest('[data-record-id]');
            if (row && mode) {
                if (mode !== 'read' && input.checked) row.querySelector('[data-record-mode="read"]').checked = true;
                if (mode === 'read' && !input.checked) {
                    row.querySelector('[data-record-mode="write"]').checked = false;
                    row.querySelector('[data-record-mode="delete"]').checked = false;
                }
            }
            updateCount();
        });
        grants.addEventListener('click', function (event) {
            var remove = event.target.closest('[data-record-remove]');
            if (!remove) return;
            var card = remove.closest('[data-record-resource]');
            remove.closest('[data-record-id]').remove();
            if (!card.querySelector('[data-record-id]') && !card.querySelector('[data-record-create-grant]').checked) card.remove();
            updateCount();
        });
        editor.querySelector('[data-record-clear]').addEventListener('click', function () {
            if (!window.confirm('Remove all data grants? A strict user will have no access to business records.')) return;
            grants.replaceChildren();
            results.replaceChildren();
            next.hidden = true;
            updateCount();
        });
        editor.querySelector('[data-record-create]').addEventListener('click', function () {
            var label = type.options[type.selectedIndex].textContent;
            ensureCard(type.value, label).querySelector('[data-record-create-grant]').checked = true;
            status.textContent = 'Create is allowed only for ' + label + '. Existing rows and related data are not granted.';
            updateCount();
        });

        function find(page, append) {
            var resource = type.value;
            var label = type.options[type.selectedIndex].textContent;
            var number = ++requestNumber;
            var query = new URLSearchParams({ resource: resource, search: search.value.trim(), page: String(page) });
            if (!append) results.replaceChildren();
            next.hidden = true;
            status.textContent = 'Finding records…';
            editor.querySelector('[data-record-find]').disabled = true;
            return window.fetch(editor.getAttribute('data-record-endpoint') + '?' + query.toString(), {
                headers: { Accept: 'application/json' }, credentials: 'same-origin'
            }).then(function (response) {
                if (!response.ok) throw new Error('Records could not be loaded. Check your connection and try again.');
                return response.json();
            }).then(function (data) {
                if (number !== requestNumber) return;
                if (!data.ok) throw new Error(data.message || 'Records could not be loaded.');
                data.records.forEach(function (record) {
                    var row = el('div', 'record-result');
                    row.appendChild(el('span', '', record.label));
                    var select = button(selected(resource, record.id) ? 'Selected' : 'Select', 'btn btn-sm btn-outline-primary flex-shrink-0');
                    select.addEventListener('click', function () {
                        addRecord(resource, label, record);
                        select.textContent = 'Selected';
                    });
                    row.appendChild(select);
                    results.appendChild(row);
                });
                status.textContent = data.records.length ? 'Select only the records this user needs. Other records remain blocked.' : 'No matching records. Try a different name, date or ID.';
                nextPage = data.next_page;
                next.hidden = !data.has_more;
            }).catch(function (error) {
                if (number === requestNumber) status.textContent = error.message;
            }).finally(function () {
                if (number === requestNumber) editor.querySelector('[data-record-find]').disabled = false;
            });
        }
        editor.querySelector('[data-record-find]').addEventListener('click', function () { find(1, false); });
        next.addEventListener('click', function () { find(nextPage, true); });
        type.addEventListener('change', function () {
            requestNumber++;
            editor.querySelector('[data-record-find]').disabled = false;
            results.replaceChildren();
            next.hidden = true;
            status.textContent = 'Find and select individual records for this data type.';
        });
        search.addEventListener('keydown', function (event) {
            if (event.key === 'Enter') { event.preventDefault(); find(1, false); }
        });
    });
})();
