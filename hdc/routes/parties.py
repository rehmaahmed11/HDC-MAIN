"""HDC routes: Parties — directory + per-party financial ledger.

  /hdc/parties                 ledger-style list, KPI category filters, search
  /hdc/parties/<id>            one party's dated transaction statement

Parties live in ``hdc_cash_flow_party`` — the same table the CF Register and
the New Transaction *Party / Person* picker read.  Money lives in
``hdc_account_txn``: a payment tagged with the party name is one ledger row,
so the party statement and Accounts → All Entries show the same entries.
"""

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required

from hdc.extensions import _money_write_required, db
from hdc.models.cashflow import CashFlowParty
from hdc.services.cashflow_register import (
    party_type_label, save_cf_party, sync_workers_as_parties,
)
from hdc.services.parties import (
    PARTY_CATEGORIES, build_party_directory, category_label, party_category,
    party_ledger, type_choice_groups,
)


def register(app):
    """Register the Parties directory and the per-party ledger."""

    @app.route('/hdc/parties', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_parties():
        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            try:
                if action == 'add_party':
                    row, created = save_cf_party(
                        (request.form.get('name') or '').strip(),
                        party_type=(request.form.get('party_type') or 'other').strip(),
                        phone=(request.form.get('phone') or '').strip() or None,
                        note=(request.form.get('note') or '').strip() or None,
                    )
                    db.session.commit()
                    label = party_type_label(row.party_type)
                    if created:
                        flash(f'Party "{row.name}" added as {label}. It is now '
                              f'available in the Party / Person pickers.', 'success')
                    else:
                        flash(f'That party already existed — it is listed under '
                              f'{label}.', 'info')
                    return redirect(url_for('hdc_party_ledger', party_id=row.id))

                elif action == 'sync_workers':
                    created, reactivated, total = sync_workers_as_parties()
                    db.session.commit()
                    if created or reactivated:
                        flash(f'Workers synced: {created} added, {reactivated} '
                              f're-activated, out of {total} workers on the books.',
                              'success')
                    else:
                        flash(f'All {total} workers are already in the directory.', 'info')

                elif action == 'toggle_party':
                    party = db.session.get(
                        CashFlowParty, request.form.get('party_id', type=int) or 0)
                    if party is None:
                        flash('Party not found.', 'danger')
                    else:
                        party.is_active = not bool(party.is_active)
                        db.session.commit()
                        if party.is_active:
                            flash(f'"{party.name}" reactivated — it is offered in the pickers again.',
                                  'success')
                        else:
                            flash(f'"{party.name}" deactivated — hidden from new transactions; '
                                  f'existing entries keep pointing at it.', 'success')
                else:
                    flash('Unknown action.', 'danger')
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), 'danger')
            except Exception as exc:
                db.session.rollback()
                flash(f'Unable to complete the action: {exc}', 'danger')
            return redirect(url_for('hdc_parties'))

        search = (request.args.get('q') or '').strip()
        category = (request.args.get('category') or '').strip().lower()
        status = (request.args.get('status') or '').strip().lower()
        party_id = request.args.get('party_id', type=int)

        auto_added, auto_revived, worker_total = sync_workers_as_parties()
        if auto_added or auto_revived:
            db.session.commit()

        directory = build_party_directory(
            search=search, category=category, party_id=party_id, status=status)
        directory['counts']['worker_total'] = worker_total
        directory['counts']['auto_added'] = auto_added

        return render_template(
            'parties/parties_directory.html',
            rows=directory['rows'],
            counts=directory['counts'],
            party_options=directory['party_options'],
            name_options=directory['name_options'],
            type_choices=type_choice_groups(),
            categories=PARTY_CATEGORIES,
            search=search,
            category=category,
            category_label=category_label(category) if category else 'All Parties',
            status=status,
            selected_party_id=party_id or '',
        )

    @app.route('/hdc/parties/<int:party_id>')
    @login_required
    def hdc_party_ledger(party_id):
        party = db.session.get(CashFlowParty, int(party_id or 0))
        if party is None:
            flash('Party not found.', 'danger')
            return redirect(url_for('hdc_parties'))

        include_void = (request.args.get('show_voided') or '').strip() in ('1', 'true', 'yes')
        statement = party_ledger(party, include_void=include_void)
        return render_template(
            'parties/party_ledger.html',
            party=party,
            category=party_category(party.party_type),
            category_label=category_label(party_category(party.party_type)),
            type_label=party_type_label(party.party_type),
            statement=statement,
            include_void=include_void,
        )
