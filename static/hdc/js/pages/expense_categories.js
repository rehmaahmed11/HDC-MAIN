/* HDC ERP — pages/expense_categories.js
 *
 * Row rename for the Expense Categories admin list (loaded by
 * templates/hdc/expenses/expense_categories.html).
 *
 * The list used to render a live <input> in every row, which turned three
 * columns of names into a wall of text boxes and pushed each row's Save button
 * a third of a screen away from its Delete button. The row now shows the name
 * and reveals the rename field only while it is being edited:
 *
 *   • the pencil in the Actions cell opens the row's field, focused and
 *     selected, so the operator can type over the old name immediately;
 *   • one row is open at a time — opening another closes the first;
 *   • Escape or the ✕ restores the original name and closes the row;
 *   • with JavaScript off a <noscript> block in the template keeps the field
 *     visible and hides the controls that would have opened it, so a rename
 *     still works.
 *
 * The field and its Save button stay inside the row's <form>, so the shared
 * entry-result dialogue (core/dialog.js) and the double-submit lock
 * (core/forms.js) keep working exactly as they do for every other form.
 */
(function () {
    'use strict';

    var open = null;        // { form: HTMLFormElement, input: HTMLInputElement }

    function closeOpen() {
        if (!open) return;
        var row = open.form.closest ? open.form.closest('tr') : null;
        open.form.classList.add('d-none');
        open.form.classList.remove('d-flex');
        if (row) {
            var view = row.querySelector('.expense-category-name');
            if (view) view.classList.remove('d-none');
        }
        if (open.input) open.input.value = open.input.getAttribute('value') || '';
        open = null;
    }

    function wire() {
        var rows = document.querySelectorAll('.expense-category-table tbody tr');
        Array.prototype.forEach.call(rows, function (row) {
            var form = row.querySelector('.expense-category-edit-form');
            var name = row.querySelector('.expense-category-name');
            var editBtn = row.querySelector('.expense-category-edit-btn');
            if (!form || !name || !editBtn) return;

            var input = form.querySelector('input[name="category_name"]');
            var cancelBtn = form.querySelector('button[type="button"]');

            function openRow() {
                closeOpen();
                name.classList.add('d-none');
                form.classList.remove('d-none');
                form.classList.add('d-flex');
                open = { form: form, input: input };
                if (input) {
                    input.focus();
                    if (input.select) input.select();
                }
            }

            editBtn.addEventListener('click', openRow);
            if (cancelBtn) cancelBtn.addEventListener('click', closeOpen);
            form.addEventListener('keydown', function (e) {
                if (e.key === 'Escape' || e.key === 'Esc') {
                    e.preventDefault();
                    closeOpen();
                }
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wire);
    } else {
        wire();
    }
})();
