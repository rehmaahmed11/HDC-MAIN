/* Project Estimation module */
(function () {
    var boot = window.HDC_PROJECT_ESTIMATION_BOOT || {};
    var tbody = document.getElementById('peTbody');
    var addBtn = document.getElementById('peAddRow');
    var saveEstBtn = document.getElementById('peSaveEstimation');
    var saveProjBtn = document.getElementById('peSaveProject');
    var autoCodeBtn = document.getElementById('peAutoCode');
    var grandTotalEl = document.getElementById('peGrandTotal');
    var alertWrap = document.getElementById('peAlert');
    var draftKey = 'hdc_project_estimation_draft';

    if (!tbody || !addBtn || !saveEstBtn || !saveProjBtn) return;

    function showAlert(type, msg) {
        if (!alertWrap) return;
        alertWrap.innerHTML = '<div class="alert alert-' + type + ' alert-dismissible fade show py-2" role="alert">'
            + msg + '<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>';
    }

    function parseNumber(input) {
        var raw = (input.value || '').toString().trim();
        if (raw === '') return { value: 0, valid: true };
        var n = parseFloat(raw);
        if (isNaN(n)) return { value: 0, valid: false };
        if (n < 0) n = 0;
        return { value: n, valid: true };
    }

    function setInvalid(input, isInvalid) {
        if (!input) return;
        if (isInvalid) input.classList.add('is-invalid');
        else input.classList.remove('is-invalid');
    }

    function rowTemplate(data) {
        var tr = document.createElement('tr');
        tr.innerHTML =
            '<td><input type="text" class="form-control pe-stage" list="' + boot.stageOptionsId + '" placeholder="Stage name"></td>' +
            '<td>' +
                '<select class="form-select pe-type">' +
                    '<option value="lump_sum">Lump Sum</option>' +
                    '<option value="sqft">Sqft</option>' +
                '</select>' +
            '</td>' +
            '<td><input type="number" min="0" step="0.01" class="form-control pe-rate" value="0"></td>' +
            '<td><input type="number" min="0" step="0.01" class="form-control pe-qty" value="0"></td>' +
            '<td class="fw-bold pe-total">0</td>' +
            '<td class="text-end"><button class="btn btn-sm btn-outline-danger pe-remove"><i class="fas fa-times"></i></button></td>';

        if (data) {
            var stage = tr.querySelector('.pe-stage');
            var type = tr.querySelector('.pe-type');
            var rate = tr.querySelector('.pe-rate');
            var qty = tr.querySelector('.pe-qty');
            if (data.stage_name) stage.value = data.stage_name;
            if (data.type) type.value = data.type;
            if (typeof data.rate === 'number') rate.value = data.rate;
            if (typeof data.quantity === 'number') qty.value = data.quantity;
        }
        return tr;
    }

    function updateRow(tr) {
        var typeEl = tr.querySelector('.pe-type');
        var rateEl = tr.querySelector('.pe-rate');
        var qtyEl = tr.querySelector('.pe-qty');
        var totalEl = tr.querySelector('.pe-total');
        var typeVal = (typeEl.value || 'lump_sum');

        var rateParsed = parseNumber(rateEl);
        var qtyParsed = parseNumber(qtyEl);

        setInvalid(rateEl, !rateParsed.valid);
        setInvalid(qtyEl, !qtyParsed.valid);

        if (typeVal === 'lump_sum') {
            qtyEl.value = 0;
            qtyEl.disabled = true;
            qtyParsed.value = 0;
            setInvalid(qtyEl, false);
        } else {
            qtyEl.disabled = false;
        }

        var total = 0;
        try {
            if (typeVal === 'lump_sum') total = rateParsed.value;
            else total = rateParsed.value * qtyParsed.value;
        } catch (e) {
            total = 0;
        }

        if (!isFinite(total) || isNaN(total)) total = 0;
        totalEl.textContent = total.toLocaleString();
        totalEl.dataset.total = total.toString();
        updateGrandTotal();
    }

    function updateGrandTotal() {
        var sum = 0;
        tbody.querySelectorAll('.pe-total').forEach(function (el) {
            var v = parseFloat(el.dataset.total || '0');
            if (!isNaN(v)) sum += v;
        });
        grandTotalEl.textContent = sum.toLocaleString();
    }

    function addRow(data) {
        var tr = rowTemplate(data);
        tbody.appendChild(tr);
        bindRow(tr);
        updateRow(tr);
    }

    function bindRow(tr) {
        tr.querySelector('.pe-remove').addEventListener('click', function () {
            tr.remove();
            updateGrandTotal();
            saveDraft();
        });
        tr.querySelectorAll('input, select').forEach(function (el) {
            el.addEventListener('input', function () {
                updateRow(tr);
                saveDraft();
            });
            el.addEventListener('change', function () {
                updateRow(tr);
                saveDraft();
            });
        });
    }

    function collectRows() {
        var rows = [];
        var hasInvalid = false;
        tbody.querySelectorAll('tr').forEach(function (tr) {
            var stageEl = tr.querySelector('.pe-stage');
            var typeEl = tr.querySelector('.pe-type');
            var rateEl = tr.querySelector('.pe-rate');
            var qtyEl = tr.querySelector('.pe-qty');
            var totalEl = tr.querySelector('.pe-total');

            var stage = (stageEl.value || '').trim();
            var rateParsed = parseNumber(rateEl);
            var qtyParsed = parseNumber(qtyEl);

            var total = parseFloat(totalEl.dataset.total || '0');
            if (!isFinite(total) || isNaN(total)) total = 0;

            setInvalid(rateEl, !rateParsed.valid);
            setInvalid(qtyEl, !qtyParsed.valid);

            var needsStage = (rateParsed.value > 0 || qtyParsed.value > 0);
            if (needsStage && !stage) {
                setInvalid(stageEl, true);
                hasInvalid = true;
            } else {
                setInvalid(stageEl, false);
            }

            rows.push({
                stage_name: stage,
                type: typeEl.value || 'lump_sum',
                rate: rateParsed.value,
                quantity: (typeEl.value === 'lump_sum') ? 0 : qtyParsed.value,
                total: total
            });
        });
        return { rows: rows, hasInvalid: hasInvalid };
    }

    function getProjectMeta() {
        return {
            project_code: (document.getElementById('peProjectCode').value || '').trim(),
            client: (document.getElementById('peClient').value || '').trim(),
            location: (document.getElementById('peLocation').value || '').trim()
        };
    }

    function saveDraft() {
        try {
            var payload = {
                project_meta: getProjectMeta(),
                rows: collectRows().rows
            };
            localStorage.setItem(draftKey, JSON.stringify(payload));
        } catch (e) {}
    }

    function loadDraft() {
        try {
            var raw = localStorage.getItem(draftKey);
            if (!raw) return false;
            var d = JSON.parse(raw);
            if (!d || !Array.isArray(d.rows)) return false;
            if (d.project_meta) {
                document.getElementById('peProjectCode').value = d.project_meta.project_code || '';
                document.getElementById('peClient').value = d.project_meta.client || '';
                document.getElementById('peLocation').value = d.project_meta.location || '';
            }
            tbody.innerHTML = '';
            d.rows.forEach(function (r) { addRow(r); });
            if (d.rows.length === 0) addRow();
            return true;
        } catch (e) {
            return false;
        }
    }

    function validateBeforeSave(rowsInfo) {
        var rows = rowsInfo.rows;
        if (rows.length === 0) {
            showAlert('warning', 'Add at least one stage row before saving.');
            return false;
        }
        if (rowsInfo.hasInvalid) {
            showAlert('warning', 'Please fix highlighted fields before saving.');
            return false;
        }
        var total = rows.reduce(function (s, r) { return s + (r.total || 0); }, 0);
        if (total <= 0) {
            showAlert('warning', 'Total cannot be zero. Please enter rates or quantities.');
            return false;
        }
        return true;
    }

    function postSave(mode) {
        var rowsInfo = collectRows();
        if (!validateBeforeSave(rowsInfo)) return;
        var payload = {
            mode: mode,
            project_meta: getProjectMeta(),
            rows: rowsInfo.rows
        };
        fetch(boot.endpoints.save, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).then(function (r) { return r.json(); })
        .then(function (data) {
            if (!data || !data.ok) {
                showAlert('danger', (data && data.error) ? data.error : 'Unable to save. Please try again.');
                return;
            }
            if (mode === 'project') {
                window.location.href = boot.endpoints.projects;
                return;
            }
            showAlert('success', data.message || 'Estimation saved.');
            saveDraft();
        }).catch(function () {
            showAlert('danger', 'Unable to save. Please try again.');
        });
    }

    addBtn.addEventListener('click', function () {
        addRow();
        saveDraft();
    });
    saveEstBtn.addEventListener('click', function () { postSave('estimation'); });
    saveProjBtn.addEventListener('click', function () { postSave('project'); });

    if (autoCodeBtn && boot.endpoints && boot.endpoints.nextProjectCode) {
        autoCodeBtn.addEventListener('click', function () {
            var client = (document.getElementById('peClient').value || '');
            var location = (document.getElementById('peLocation').value || '');
            var url = boot.endpoints.nextProjectCode + '?client=' + encodeURIComponent(client) + '&location=' + encodeURIComponent(location);
            fetch(url).then(function (r) { return r.json(); })
                .then(function (d) {
                    if (!d || !d.ok) {
                        showAlert('warning', (d && d.error) ? d.error : 'Unable to generate code.');
                        return;
                    }
                    var input = document.getElementById('peProjectCode');
                    input.value = d.code;
                    input.readOnly = true;
                    saveDraft();
                })
                .catch(function () { showAlert('danger', 'Unable to generate code.'); });
        });
    }

    if (!loadDraft()) {
        addRow();
    }
})();
