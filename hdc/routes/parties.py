"""HDC routes: Parties — directory, categories + per-party financial ledger.

  /hdc/parties                 ledger-style list, user-defined category cards
  /hdc/parties/<id>            one party's dated transaction statement

Parties live in ``hdc_cash_flow_party`` — the same table the CF Register and
the New Transaction *Party / Person* picker read.  Money lives in
``hdc_account_txn``: a payment tagged with the party name is one ledger row,
so the party statement and Accounts → All Entries show the same entries.

Categories are **data, not code**: they live in ``hdc_party_category`` and are
created, renamed, recoloured, retired and deleted from this page (see
``hdc.services.party_categories``).  The six buckets the directory shipped
with are seeded rows, so the operator can change them like any other.
"""

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required

from hdc.extensions import _money_write_required, db
from hdc.models.cashflow import CashFlowParty, PartyCategory
from hdc.services.cashflow_register import (
    party_type_label, save_cf_party, sync_workers_as_parties,
    update_cf_party,
)
from hdc.services.parties import (
    build_party_directory, party_category_name, party_ledger,
    sync_project_clients_as_parties, type_choice_groups,
)
from hdc.services.party_categories import (
    PARTY_CATEGORY_COLOURS, PARTY_CATEGORY_ICONS, bulk_assign_category,
    category_by_id, delete_party_category,
    effective_party_category, save_party_category, set_party_category_active,
    update_party_category,
)


def _posted_category(form):
    """``category_id`` from a party form: '' = automatic, None = not posted."""
    if 'category_id' not in form:
        return None
    return (form.get('category_id') or '').strip()


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
                        category_id=_posted_category(request.form),
                    )
                    db.session.commit()
                    label = party_type_label(row.party_type)
                    cat = effective_party_category(row)
                    filed = f' under "{cat.name}"' if cat is not None else ''
                    if created:
                        hint = (' Use "New Project" on this ledger to create their first project.'
                                if (row.party_type or '').strip().lower() == 'client' else '')
                        flash(f'Party "{row.name}" added as {label}{filed}. It is now '
                              f'available in the Party / Person pickers.{hint}', 'success')
                    else:
                        flash(f'That party already existed — it is listed as '
                              f'{label}{filed}.', 'info')
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

                elif action == 'sync_clients':
                    created, reactivated, total, linked = sync_project_clients_as_parties()
                    db.session.commit()
                    linked_note = (f' {linked} project(s) gained their direct party link.'
                                   if linked else '')
                    if created or reactivated or linked:
                        flash(f'Project clients synced: {created} added, {reactivated} '
                              f're-activated, out of {total} distinct clients on the books.'
                              f'{linked_note}', 'success')
                    else:
                        flash(f'All {total} project clients are already in the directory.', 'info')

                elif action == 'edit_party':
                    party = db.session.get(
                        CashFlowParty, request.form.get('party_id', type=int) or 0)
                    if party is None:
                        flash('Party not found.', 'danger')
                    else:
                        name = (request.form.get('name') or '').strip()
                        ptype = (request.form.get('party_type') or 'other').strip()
                        update_cf_party(
                            party,
                            name=name,
                            party_type=ptype,
                            phone=(request.form.get('phone') or '').strip(),
                            note=(request.form.get('note') or '').strip(),
                            category_id=_posted_category(request.form),
                            commit=False,
                        )
                        db.session.commit()
                        flash(f'Party "{party.name}" updated.', 'success')

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

                # ── categories: create / rename / retire / delete ─────────
                elif action == 'add_category':
                    cat = save_party_category(
                        (request.form.get('name') or '').strip(),
                        description=(request.form.get('description') or '').strip() or None,
                        icon=(request.form.get('icon') or '').strip() or None,
                        colour=(request.form.get('colour') or '').strip() or None,
                    )
                    db.session.commit()
                    flash(f'Category "{cat.name}" created — it is now a filter card '
                          f'and an option in every party form.', 'success')

                elif action == 'edit_category':
                    cat = db.session.get(
                        PartyCategory, request.form.get('category_id', type=int) or 0)
                    if cat is None:
                        flash('Category not found.', 'danger')
                    else:
                        old_name = cat.name
                        update_party_category(
                            cat,
                            name=(request.form.get('name') or '').strip(),
                            description=(request.form.get('description') or '').strip(),
                            icon=(request.form.get('icon') or '').strip(),
                            colour=(request.form.get('colour') or '').strip(),
                            commit=False,
                        )
                        db.session.commit()
                        flash(f'Category "{old_name}" updated to "{cat.name}" — '
                              f'the parties filed under it stay put.', 'success')

                elif action == 'toggle_category':
                    cat = db.session.get(
                        PartyCategory, request.form.get('category_id', type=int) or 0)
                    if cat is None:
                        flash('Category not found.', 'danger')
                    else:
                        set_party_category_active(cat, not bool(cat.is_active))
                        db.session.commit()
                        if cat.is_active:
                            flash(f'Category "{cat.name}" is active again — offered in '
                                  f'new party forms.', 'success')
                        else:
                            flash(f'Category "{cat.name}" hidden from new assignments. '
                                  f'Parties already filed under it keep their card.',
                                  'success')

                elif action == 'delete_category':
                    cat = db.session.get(
                        PartyCategory, request.form.get('category_id', type=int) or 0)
                    if cat is None:
                        flash('Category not found.', 'danger')
                    else:
                        delete_party_category(cat)
                        db.session.commit()
                        flash(f'Category "{cat.name}" deleted.', 'success')

                elif action == 'assign_category':
                    raw_ids = request.form.getlist('party_ids')
                    category_id = (request.form.get('category_id') or '').strip()
                    cat = category_by_id(category_id) if category_id else None
                    if category_id and cat is None:
                        raise ValueError('Choose a valid category to assign.')
                    updated, missing = bulk_assign_category(raw_ids, cat)
                    db.session.commit()
                    target = f'"{cat.name}"' if cat is not None else 'Automatic (by type)'
                    note = f' ({missing} row(s) were no longer there)' if missing else ''
                    flash(f'{updated} party(ies) filed under {target}.{note}',
                          'success')

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
        c_added, c_revived, client_total, c_linked = sync_project_clients_as_parties()
        if auto_added or auto_revived or c_added or c_revived or c_linked:
            db.session.commit()

        directory = build_party_directory(
            search=search, category=category, party_id=party_id, status=status)
        directory['counts']['worker_total'] = worker_total
        directory['counts']['auto_added'] = auto_added
        directory['counts']['client_total'] = client_total
        directory['counts']['client_auto_added'] = c_added
        directory['counts']['client_auto_revived'] = c_revived

        categories = directory['categories']
        active_category = next(
            (c for c in categories if c['key'] == category), None)

        return render_template(
            'parties/parties_directory.html',
            rows=directory['rows'],
            counts=directory['counts'],
            categories=categories,
            category=category,
            category_label=(active_category['name'] if active_category
                            else ('Uncategorised' if category == 'uncategorised'
                                  else 'All Parties')),
            party_options=directory['party_options'],
            name_options=directory['name_options'],
            client_name_options=directory.get('client_name_options') or [],
            type_choices=type_choice_groups(),
            category_colours=PARTY_CATEGORY_COLOURS,
            category_icons=PARTY_CATEGORY_ICONS,
            search=search,
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
        category = effective_party_category(party)
        # Keep the owner-specific shortcuts tied to the *type* (behaviour), not
        # to the operator's category: a lender filed under "Cement Dealers"
        # must still get the loan links.
        type_bucket = (party.party_type or 'other').strip().lower()
        return render_template(
            'parties/party_ledger.html',
            party=party,
            category=(category.key if category is not None else ''),
            category_label=party_category_name(party),
            type_label=party_type_label(party.party_type),
            is_project_owner=(type_bucket == 'client'),
            statement=statement,
            include_void=include_void,
        )
