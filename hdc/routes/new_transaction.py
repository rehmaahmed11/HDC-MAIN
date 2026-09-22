"""HDC routes: the New Transaction page and its ``+ Add New …`` JSON endpoints.

The Cash Flow register (``/hdc/accounts/cashflow/register``) is where money is
recorded and stays the system of record.  This module adds the *focused* entry
surface for that same engine — one transaction, nothing else on screen — plus
the small JSON endpoints the Account / Party / Project pickers call when the
entity a user needs does not exist yet.

  GET  /hdc/accounts/new-transaction                  the form
  POST /hdc/accounts/new-transaction                  post one entry
  POST /hdc/accounts/new-transaction/account          create an account
  POST /hdc/accounts/new-transaction/party            create a party
  POST /hdc/accounts/new-transaction/project          create a project
  GET  /hdc/accounts/new-transaction/subcategories    subcategories of a category

Nothing here re-implements accounting.  Posting goes through
``hdc.services.transaction_entry.create_entry_from_form`` →
``hdc.services.cashflow_register.save_manual_cash_flow_entry``, so the register's
validation, balance guard, day lock, exactly-once posting and audit trail all
apply unchanged.  The create endpoints reuse the same get-or-create services the
rest of the app uses and never overwrite an existing row.
"""

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from hdc.extensions import _admin_only, db
from hdc.services.cashflow_register import (
    CF_DIRECTION_LABELS,
    subcategory_options,
)
from hdc.services.transaction_entry import (
    clear_entry_form,
    create_cashflow_party,
    create_entry_from_form,
    create_money_account,
    create_project_quick,
    entry_form_context,
    pop_entry_form,
    stash_entry_form,
)

NEW_TXN_ENDPOINT = 'hdc_new_transaction'
MONEY_ACCESS_MESSAGE = 'Admin/Accountant access required.'


def _json_denied():
    """403 for the JSON pickers (a redirect would hand the script an HTML page)."""
    return jsonify(ok=False, message=MONEY_ACCESS_MESSAGE), 403


def register(app):
    """Register the New Transaction page and its create-on-the-fly endpoints."""

    @app.route('/hdc/accounts/new-transaction', methods=['GET', 'POST'])
    @login_required
    def hdc_new_transaction():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            if action != 'create_entry':
                flash('Unknown action — nothing was saved.', 'danger')
                return redirect(url_for(NEW_TXN_ENDPOINT))
            try:
                entry, created = create_entry_from_form(request.form, actor=current_user)
                db.session.commit()
            except ValueError as exc:
                # Keep the submission so the re-rendered form is not empty.
                db.session.rollback()
                stash_entry_form(request.form, str(exc))
                flash(str(exc), 'danger')
                return redirect(url_for(NEW_TXN_ENDPOINT))
            except Exception as exc:  # pragma: no cover - defensive
                db.session.rollback()
                stash_entry_form(request.form, 'Unexpected error — your entry was not saved.')
                flash(f'Unable to save the transaction: {exc}', 'danger')
                return redirect(url_for(NEW_TXN_ENDPOINT))

            clear_entry_form()
            if not created:
                flash('That transaction was already recorded (duplicate submission ignored).', 'info')
            else:
                flash(
                    f"{CF_DIRECTION_LABELS.get(entry.direction, entry.direction)} recorded: "
                    f"{entry.amount:,.2f} PKR (entry #{entry.id}).",
                    'success',
                )
            return redirect(url_for(NEW_TXN_ENDPOINT))

        values, error = pop_entry_form()
        return render_template('accounts/new_transaction.html',
                               **entry_form_context(values or None, error or None))

    # ── + Add New Account ────────────────────────────────────────────────────
    @app.route('/hdc/accounts/new-transaction/account', methods=['POST'])
    @login_required
    def hdc_new_transaction_account():
        if _admin_only():
            return _json_denied()
        payload = request.get_json(silent=True) or request.form
        try:
            row, created = create_money_account(
                payload.get('name'),
                mode=payload.get('mode') or 'cash',
                bank_name=payload.get('bank_name'),
                account_number=payload.get('account_number'),
                opening_balance=payload.get('opening_balance') or 0,
            )
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            return jsonify(ok=False, message=str(exc)), 400
        except Exception as exc:  # pragma: no cover - defensive
            db.session.rollback()
            return jsonify(ok=False, message=f'Unable to create the account: {exc}'), 400
        return jsonify(
            ok=True,
            created=bool(created),
            message=('Account added.' if created else 'That account already existed — selected it.'),
            item={
                'id': int(row.id),
                'name': row.name,
                'type': (row.type or '').strip().lower(),
                'label': f"{row.name} ({'Bank' if (row.type or '').strip().lower() == 'bank' else 'Cash'})",
            },
        )

    # ── + Add New Party ──────────────────────────────────────────────────────
    @app.route('/hdc/accounts/new-transaction/party', methods=['POST'])
    @login_required
    def hdc_new_transaction_party():
        if _admin_only():
            return _json_denied()
        payload = request.get_json(silent=True) or request.form
        try:
            row, created = create_cashflow_party(
                payload.get('name'),
                party_type=payload.get('party_type') or 'other',
                phone=payload.get('phone'),
            )
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            return jsonify(ok=False, message=str(exc)), 400
        except Exception as exc:  # pragma: no cover - defensive
            db.session.rollback()
            return jsonify(ok=False, message=f'Unable to create the party: {exc}'), 400
        return jsonify(
            ok=True,
            created=bool(created),
            message=('Party added.' if created else 'That party already existed — selected it.'),
            item={
                'id': int(row.id),
                'name': row.name,
                'party_type': (row.party_type or 'other'),
                'label': row.name,
            },
        )

    # ── + Add New Project ────────────────────────────────────────────────────
    @app.route('/hdc/accounts/new-transaction/project', methods=['POST'])
    @login_required
    def hdc_new_transaction_project():
        if _admin_only():
            return _json_denied()
        payload = request.get_json(silent=True) or request.form
        try:
            row, created = create_project_quick(
                payload.get('name'),
                client=payload.get('client'),
                location=payload.get('location'),
            )
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            return jsonify(ok=False, message=str(exc)), 400
        except Exception as exc:  # pragma: no cover - defensive
            db.session.rollback()
            return jsonify(ok=False, message=f'Unable to create the project: {exc}'), 400
        return jsonify(
            ok=True,
            created=bool(created),
            message=('Project added.'
                     if created else 'That project already existed — selected it.'),
            item={
                'id': int(row.id),
                'name': row.name,
                'project_code': row.project_code,
                # The owner: a project receipt is booked against this name, so
                # the form can fill it in the moment the project is created.
                'client': (row.client or ''),
                'label': row.name,
            },
        )

    # ── category → subcategory dependency (authoritative) ────────────────────
    @app.route('/hdc/accounts/new-transaction/subcategories')
    @login_required
    def hdc_new_transaction_subcategories():
        """Subcategories belonging to one category.

        The form renders every subcategory up front (so it works without
        JavaScript and filters with no round-trip); this is the authoritative
        lookup for callers that only hold a category id.
        """
        if _admin_only():
            return _json_denied()
        category_id = request.args.get('category_id', type=int)
        if not category_id:
            return jsonify(ok=True, items=[])
        rows = subcategory_options(category_id)
        return jsonify(ok=True, items=[{'id': int(r.id), 'name': r.name} for r in rows])
