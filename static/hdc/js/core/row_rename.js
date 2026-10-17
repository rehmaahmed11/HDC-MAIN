/* HDC ERP — core/row_rename.js: in-place renaming for the app's "list of names"
 * screens (Trades, Expense Categories).
 *
 * Both pages render one durable record per row: the saved name, a Created date
 * and the row's actions.  Renaming happens IN the row instead of on a separate
 * edit screen, because the record is a single word — sending the operator to
 * another page to change it costs more than it saves.
 *
 * The markup contract (shared by both templates so they cannot drift):
 *
 *   <table data-hdc-rename-table>
 *     <tr>
 *       ...
 *       <span class="hdc-rename-view">Cement</span>
 *       <form class="hdc-rename-form d-none" method="POST" data-hdc-dialog>…</form>
 *       ...
 *       <td class="hdc-rename-actions">
 *         <button class="hdc-rename-open">✎</button>
 *         …delete form…
 *       </td>
 *
 * Behaviour:
 *   • every row starts read-only — no live text box behind every name;
 *   • the pencil opens that row's field, focused and selected so typing
 *     replaces the old name immediately;
 *   • only one row is open at a time;
 *   • Escape or the ✕ restores the original name and closes the row;
 *   • with JavaScript off a <noscript> block in each template keeps the field
 *     visible, so a rename still works.
 *
 * The field and its Save button stay inside the row's <form>, so the shared
 * entry-result dialogue (core/dialog.js) and the double-submit lock
 * (core/forms.js) keep working exactly as they do for every other form.
 */
(function () {
    'use strict';

    var TABLE_SELECTOR = '[data-hdc-rename-table]';
    var FORM_SELECTOR = '.hdc-rename-form';
    var VIEW_SELECTOR = '.hdc-rename-view';
    var OPEN_SELECTOR = '.hdc-rename-open';

    var open = null;        // { form, input, view }

    function closeOpen() {
        if (!open) return;
        open.form.classList.add('d-none');
        open.form.classList.remove('d-flex');
        if (open.view) open.view.classList.remove('d-none');
        if (open.input) open.input.value = open.input.getAttribute('value') || '';
        open = null;
    }

    function wireRow(row) {
        var form = row.querySelector(FORM_SELECTOR);
        var view = row.querySelector(VIEW_SELECTOR);
        var button = row.querySelector(OPEN_SELECTOR);
        if (!form || !view || !button) return;

        var input = form.querySelector('input[type="text"]');
        var cancel = form.querySelector('button[type="button"]');

        function openRow() {
            closeOpen();
            view.classList.add('d-none');
            form.classList.remove('d-none');
            form.classList.add('d-flex');
            open = { form: form, input: input, view: view };
            if (input) {
                input.focus();
                if (input.select) input.select();
            }
        }

        button.addEventListener('click', openRow);
        if (cancel) cancel.addEventListener('click', closeOpen);
        form.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' || event.key === 'Esc') {
                event.preventDefault();
                closeOpen();
            }
        });
    }

    function wire() {
        var rows = document.querySelectorAll(TABLE_SELECTOR + ' tbody tr');
        Array.prototype.forEach.call(rows, wireRow);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wire);
    } else {
        wire();
    }
})();
