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
        var cascade = editor.querySelector('[data-record-cascade]');
        var searchBlock = editor.querySelector('[data-record-search-block]');
        var findButton = editor.querySelector('[data-record-find]');
        var createRow = editor.querySelector('[data-record-create-row]');
        var cascadeData = { projects: [], stages: {} };
        try {
            var rawData = cascade && cascade.querySelector('[data-cascade-data]');
            if (rawData) cascadeData = JSON.parse(rawData.textContent) || cascadeData;
        } catch (error) { /* keep empty dropdowns */ }
        cascadeData.projects = Array.isArray(cascadeData.projects) ? cascadeData.projects : [];
        cascadeData.stages = cascadeData.stages && typeof cascadeData.stages === 'object' ? cascadeData.stages : {};
        var branchesBox = cascade && cascade.querySelector('[data-cascade-branches]');
        var gatePicker = cascade && cascade.querySelector('[data-cascade-picker]');
        var gateBoxes = cascade ? Array.prototype.slice.call(cascade.querySelectorAll('[data-cascade-gate]')) : [];

        function usesCascade() {
            return type.value === 'hdc_project' || type.value === 'hdc_stage';
        }
        function gateChecked(mode) {
            var box = gateBoxes.find(function (input) { return input.getAttribute('data-cascade-gate') === mode; });
            return !!(box && box.checked);
        }
        function branchList() {
            return branchesBox ? Array.prototype.slice.call(branchesBox.querySelectorAll('[data-cascade-branch]')) : [];
        }
        function updatePickerVisibility() {
            if (!gatePicker) return;
            gatePicker.hidden = !(gateChecked('read') || gateChecked('write') || branchList().length > 0);
        }
        function rebuildSelect(select, placeholder, options, chosen) {
            select.replaceChildren();
            var head = el('option', '', placeholder);
            head.value = '';
            select.appendChild(head);
            options.forEach(function (option) {
                if (chosen.has(String(option.id))) return;
                var node = el('option', '', option.label);
                node.value = String(option.id);
                select.appendChild(node);
            });
        }
        function refreshPickers() {
            if (!branchesBox) return;
            var chosenProjects = new Set();
            branchList().forEach(function (branch) {
                var pid = branch.getAttribute('data-project-id');
                if (pid) chosenProjects.add(pid);
            });
            if (gatePicker) {
                var projectSelect = gatePicker.querySelector('[data-cascade-project]');
                if (projectSelect) rebuildSelect(projectSelect, 'Select a project…', cascadeData.projects, chosenProjects);
            }
            branchList().forEach(function (branch) {
                var pid = branch.getAttribute('data-project-id');
                var select = branch.querySelector('[data-cascade-stage]');
                if (!pid || !select) return;
                var chosenStages = new Set();
                Array.prototype.forEach.call(branch.querySelectorAll('[data-cascade-stage-row]'), function (row) {
                    chosenStages.add(row.getAttribute('data-stage-id'));
                });
                var options = (cascadeData.stages[pid] || []).slice();
                options.push({ id: 0, label: '(Whole project)' });
                rebuildSelect(select, 'Select a stage…', options, chosenStages);
            });
        }
        function syncTypeView() {
            if (!cascade) return;
            var guided = usesCascade();
            cascade.hidden = !guided;
            // Keep the data-type selector visible; only its search controls hide.
            if (searchBlock) searchBlock.hidden = guided;
            if (findButton) findButton.hidden = guided;
            if (createRow) createRow.hidden = guided;
            if (guided) {
                results.replaceChildren();
                next.hidden = true;
                updatePickerVisibility();
            }
        }
        function toggleButton(kind) {
            var node = button('×', 'btn btn-sm btn-link text-danger p-1');
            node.setAttribute('data-cascade-remove', kind);
            return node;
        }
        function recordToggle(mode, resource, id, label, checked) {
            var toggle = el('label', 'record-toggle');
            var input = el('input', 'form-check-input mt-0');
            input.type = 'checkbox';
            input.name = 'record_' + mode + '_' + resource;
            input.value = String(id);
            input.checked = !!checked;
            input.setAttribute('data-record-mode', mode);
            input.setAttribute('aria-label', (mode === 'write' ? 'Edit' : mode === 'delete' ? 'Delete or void' : 'Read') + ' ' + label);
            toggle.appendChild(input);
            return toggle;
        }
        function sectionRow(pid, sid, key, label) {
            var row = el('div', 'cascade-section-row');
            row.appendChild(el('span', '', label));
            ['read', 'write'].forEach(function (mode) {
                var toggle = el('label', 'record-toggle');
                var input = el('input', 'form-check-input mt-0');
                input.type = 'checkbox';
                input.name = 'report_' + mode + '_' + pid + '_' + sid + '_' + key;
                input.value = '1';
                input.setAttribute('data-report-section', mode);
                input.setAttribute('aria-label', (mode === 'write' ? 'Write ' : 'Read ') + label);
                toggle.appendChild(input);
                row.appendChild(toggle);
            });
            return row;
        }
        var sectionItems = [
            ['project_report', 'Project report'],
            ['stage_report', 'Stage cost report'],
            ['glance_report', 'Glance report'],
            ['report_exports', 'Report exports']
        ];
        function addStage(branch, sid) {
            var pid = branch.getAttribute('data-project-id');
            if (!pid || !branchesBox) return;
            sid = String(sid);
            var exists = Array.prototype.some.call(branch.querySelectorAll('[data-cascade-stage-row]'), function (row) {
                return row.getAttribute('data-stage-id') === sid;
            });
            if (exists) return;
            var numeric = Number(sid);
            var label = numeric === 0 ? '(Whole project)' :
                ((cascadeData.stages[pid] || []).find(function (stage) { return String(stage.id) === sid; }) || { label: 'Stage #' + sid }).label;
            var projectRead = branch.querySelector('[data-record-mode="read"]');
            var projectWrite = branch.querySelector('[data-record-mode="write"]');
            var seedRead = !!(projectRead && projectRead.checked) || !!(projectWrite && projectWrite.checked);
            var seedWrite = !!(projectWrite && projectWrite.checked);
            if (!seedRead && !seedWrite) seedRead = true;
            var node = el('div', 'cascade-stage');
            node.setAttribute('data-cascade-stage-row', '');
            node.setAttribute('data-stage-id', sid);
            var head = el('div', 'record-row');
            head.appendChild(el('span', 'record-label', label));
            if (numeric !== 0) {
                head.setAttribute('data-record-id', sid);
                ['read', 'write', 'delete'].forEach(function (mode) {
                    head.appendChild(recordToggle(mode, 'hdc_stage', sid, label,
                        mode === 'read' ? seedRead : mode === 'write' ? seedWrite : false));
                });
            } else {
                head.appendChild(el('span'));
                head.appendChild(el('span'));
                head.appendChild(el('span'));
            }
            head.appendChild(toggleButton('stage'));
            node.appendChild(head);
            var sections = el('div', 'cascade-sections');
            var sectionsHead = el('div', 'cascade-section-head small text-muted');
            sectionsHead.appendChild(el('span', '', 'Reporting sections of ' + label));
            sectionsHead.appendChild(el('span', '', 'Read'));
            sectionsHead.appendChild(el('span', '', 'Write'));
            sections.appendChild(sectionsHead);
            sectionItems.forEach(function (entry) {
                sections.appendChild(sectionRow(pid, sid, entry[0], entry[1]));
            });
            node.appendChild(sections);
            branch.appendChild(node);
            refreshPickers();
            updateCount();
            status.textContent = label + ' added. Choose its reporting sections, then save access.';
        }
        function addBranch(pid) {
            if (!branchesBox) return;
            pid = String(pid);
            var exists = branchList().some(function (branch) {
                return branch.getAttribute('data-project-id') === pid;
            });
            if (exists) return;
            var info = (cascadeData.projects || []).find(function (project) { return String(project.id) === pid; });
            if (!info) return;
            var seedRead = gateChecked('read') || gateChecked('write');
            var seedWrite = gateChecked('write');
            var branch = el('section', 'cascade-branch record-grant-card mb-2');
            branch.setAttribute('data-cascade-branch', '');
            branch.setAttribute('data-project-id', pid);
            var head = el('div', 'record-row cascade-project-row');
            head.setAttribute('data-record-id', pid);
            var name = el('span', 'record-label');
            var icon = el('i', 'fas fa-folder-tree me-1 text-warning');
            name.appendChild(icon);
            name.appendChild(document.createTextNode(info.label));
            head.appendChild(name);
            ['read', 'write', 'delete'].forEach(function (mode) {
                head.appendChild(recordToggle(mode, 'hdc_project', pid, info.label,
                    mode === 'read' ? seedRead : mode === 'write' ? seedWrite : false));
            });
            head.appendChild(toggleButton('project'));
            branch.appendChild(head);
            var picker = el('div', 'cascade-picker');
            picker.setAttribute('data-cascade-picker', '');
            var label = el('label', 'small fw-semibold');
            label.appendChild(document.createTextNode('Stages '));
            var select = el('select', 'form-select form-select-sm mt-1');
            select.setAttribute('data-cascade-stage', '');
            select.setAttribute('aria-label', 'Choose a stage of ' + info.label);
            label.appendChild(select);
            picker.appendChild(label);
            branch.appendChild(picker);
            branchesBox.appendChild(branch);
            refreshPickers();
            updatePickerVisibility();
            updateCount();
            status.textContent = 'Project added. Choose its stages and reporting sections, then save access.';
        }
        if (cascade) {
            if (gatePicker) {
                var projectSelect = gatePicker.querySelector('[data-cascade-project]');
                if (projectSelect) projectSelect.addEventListener('change', function () {
                    if (!projectSelect.value) return;
                    addBranch(projectSelect.value);
                    projectSelect.value = '';
                });
            }
            gateBoxes.forEach(function (box) {
                box.addEventListener('change', function () {
                    var read = gateBoxes.find(function (input) { return input.getAttribute('data-cascade-gate') === 'read'; });
                    var write = gateBoxes.find(function (input) { return input.getAttribute('data-cascade-gate') === 'write'; });
                    if (box === write && write.checked) read.checked = true;
                    if (box === read && !read.checked) write.checked = false;
                    updatePickerVisibility();
                });
            });
            cascade.addEventListener('change', function (event) {
                var select = event.target.closest ? event.target.closest('[data-cascade-stage]') : null;
                if (!select || !select.value) return;
                addStage(select.closest('[data-cascade-branch]'), select.value);
                select.value = '';
            });
            cascade.addEventListener('click', function (event) {
                var remove = event.target.closest('[data-cascade-remove]');
                if (!remove) return;
                if (remove.getAttribute('data-cascade-remove') === 'project') {
                    var branch = remove.closest('[data-cascade-branch]');
                    var stageCount = branch.querySelectorAll('[data-cascade-stage-row]').length;
                    if (stageCount && !window.confirm('Remove this project together with its ' + stageCount +
                            ' stage and reporting grant' + (stageCount === 1 ? '' : 's') + '?')) return;
                    branch.remove();
                } else {
                    remove.closest('[data-cascade-stage-row]').remove();
                }
                refreshPickers();
                updatePickerVisibility();
                updateCount();
            });
        }

        function updateCount() {
            var count = grants.querySelectorAll('[data-record-mode="read"]:checked').length;
            editor.querySelector('[data-record-count]').textContent = count + ' selected record' + (count === 1 ? '' : 's');
            var hasGrant = grants.querySelectorAll('[data-record-id]').length > 0 ||
                editor.querySelectorAll('[data-record-create-grant]:checked').length > 0 ||
                (branchList().length > 0);
            editor.querySelector('[data-record-empty]').hidden = hasGrant;
            var cascadeEmpty = editor.querySelector('[data-cascade-empty]');
            if (cascadeEmpty) cascadeEmpty.hidden = branchList().length > 0;
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
        syncTypeView();
        refreshPickers();
        updatePickerVisibility();

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
            var sectionMode = input.getAttribute('data-report-section');
            if (sectionMode) {
                var sectionRowNode = input.closest('.cascade-section-row');
                var readBox = sectionRowNode.querySelector('[data-report-section="read"]');
                var writeBox = sectionRowNode.querySelector('[data-report-section="write"]');
                if (sectionMode === 'write' && writeBox.checked) readBox.checked = true;
                if (sectionMode === 'read' && !readBox.checked) writeBox.checked = false;
                updateCount();
                return;
            }
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
            Array.prototype.forEach.call(grants.children, function (child) {
                if (!child.hasAttribute('data-record-cascade')) child.remove();
            });
            if (branchesBox) branchesBox.replaceChildren();
            gateBoxes.forEach(function (box) { box.checked = false; });
            if (cascade) cascade.querySelectorAll('[data-record-create-grant]').forEach(function (box) { box.checked = false; });
            refreshPickers();
            updatePickerVisibility();
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
            syncTypeView();
            status.textContent = usesCascade()
                ? 'Use the Project access cascade below: choose a project, then its stages and reporting sections.'
                : 'Find and select individual records for this data type.';
        });
        search.addEventListener('keydown', function (event) {
            if (event.key === 'Enter') { event.preventDefault(); find(1, false); }
        });
    });
})();
