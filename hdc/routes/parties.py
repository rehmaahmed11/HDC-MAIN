"""HDC routes: Parties — the shared counterparty directory (sidebar module).

  /hdc/parties            list, search, add and activate/deactivate parties

Parties live in one table — ``hdc_cash_flow_party``, the same table the CF
Register and the New Transaction *Party / Person* picker read — so a party
added here exists everywhere at once:

* **Loan Parties** (``lender`` / ``borrower``) are what the loan Money In /
  Money Out categories in the CF Register offer: *Loan Received*, *Loan
  Given*, *Loan Repayment* and *Loan Recovery* shorten their party list to
  these people.
* **External Customers** (``rental``) are the HDC Tools rental parties.
  An external tool rental (and its payments) syncs its customer here
  automatically, and every ``rental`` party is offered back on the HDC
  Tools customer combo.
* **Other Parties** — clients, suppliers, workers, staff, subcontractors —
  round out the picker's vocabulary.

Directory reads are open to every signed-in role; adding or changing a
party is an admin/accountant money write (``_money_write_required``).
"""

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func

from hdc.extensions import _money_write_required, db
from hdc.models.cashflow import CashFlowEntry, CashFlowParty
from hdc.models.loans import Loan
from hdc.models.tool_rental import ToolRental
from hdc.services.cashflow_register import (
    LOAN_PARTY_TYPES,
    RENTAL_PARTY_TYPES,
    PARTY_TYPES,
    party_type_label,
    save_cf_party,
)

#: The three buckets the directory shows, in rendering order.
#: ``(key, label, icon, hint, type values)`` — an empty type tuple means
#: "everything not claimed by the other buckets".
PARTY_GROUPS = (
    ('loan', 'Loan Parties', 'fa-money-bill-transfer',
     ('Loan Money In / Money Out in the CF Register — Loan Received, Loan Given, '
      'Loan Repayment and Loan Recovery — offer only these parties.'),
     tuple(LOAN_PARTY_TYPES)),
    ('rental', 'External Customers', 'fa-screwdriver-wrench',
     ('Rental parties for HDC Tools. External tool rentals and their payments '
      'are synced here automatically; add a customer here before renting to them.'),
     tuple(RENTAL_PARTY_TYPES)),
    ('other', 'Other Parties', 'fa-users',
     ('Everyone else the Party / Person picker offers — clients, suppliers, '
      'workers, staff, subcontractors.'),
     ()),
)


def _party_bucket(party_type):
    """Which directory group a ``party_type`` belongs to."""
    ptype = (party_type or 'other').strip().lower() or 'other'
    if ptype in LOAN_PARTY_TYPES:
        return 'loan'
    if ptype in RENTAL_PARTY_TYPES:
        return 'rental'
    return 'other'


def _entry_counts():
    """``{lower(trim(party_name)): register entries}`` for every party."""
    rows = (db.session.query(func.lower(func.trim(CashFlowEntry.party_name)),
                             func.count(CashFlowEntry.id))
            .filter(CashFlowEntry.party_name.isnot(None))
            .group_by(func.lower(func.trim(CashFlowEntry.party_name)))
            .all())
    return {key: int(count) for key, count in rows if key}


def _loan_counts():
    """``{lower(trim(party_name)): non-void loans}`` per counterparty."""
    rows = (db.session.query(func.lower(func.trim(Loan.party_name)),
                             func.count(Loan.id))
            .filter(Loan.status != 'void')
            .group_by(func.lower(func.trim(Loan.party_name)))
            .all())
    return {key: int(count) for key, count in rows if key}


def _rental_stats():
    """``{lower(trim(customer_name)): (rentals, paid)}`` for external rentals."""
    rows = (db.session.query(func.lower(func.trim(ToolRental.customer_name)),
                             func.count(ToolRental.id),
                             func.coalesce(func.sum(ToolRental.total_paid), 0.0))
            .filter(ToolRental.renter_type == 'external',
                    ToolRental.is_void == False,  # noqa: E712
                    ToolRental.customer_name.isnot(None),
                    func.trim(func.coalesce(ToolRental.customer_name, '')) != '')
            .group_by(func.lower(func.trim(ToolRental.customer_name)))
            .all())
    return {key: (int(count), float(paid or 0.0))
            for key, count, paid in rows if key}


def register(app):
    """Register the Parties directory page (sidebar module)."""

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

                elif action == 'toggle_party':
                    party = db.session.get(CashFlowParty, request.form.get('party_id', type=int) or 0)
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

        # ── listing ────────────────────────────────────────────────────────
        search = (request.args.get('q') or '').strip()
        needle = search.lower()

        parties = CashFlowParty.query.order_by(CashFlowParty.name.asc()).all()
        entry_counts = _entry_counts()
        loan_counts = _loan_counts()
        rental_stats = _rental_stats()

        groups = []
        for key, label, icon, hint, type_values in PARTY_GROUPS:
            rows = []
            for party in parties:
                if key == 'other':
                    if _party_bucket(party.party_type) != 'other':
                        continue
                elif _party_bucket(party.party_type) != key:
                    continue
                if needle and needle not in (party.name or '').lower() \
                        and needle not in (party.phone or '').lower():
                    continue
                pname_key = (party.name or '').strip().lower()
                rentals, paid = rental_stats.get(pname_key, (0, 0.0))
                rows.append({
                    'party': party,
                    'type_label': party_type_label(party.party_type),
                    'entries': entry_counts.get(pname_key, 0),
                    'loans': loan_counts.get(pname_key, 0),
                    'rentals': rentals,
                    'rental_paid': paid,
                })
            active_rows = sum(1 for r in rows if r['party'].is_active)
            groups.append({
                'key': key,
                'label': label,
                'icon': icon,
                'hint': hint,
                'rows': rows,
                'active_count': active_rows,
                'total_count': len(rows),
            })

        def _count(bucket):
            return sum(1 for p in parties
                       if p.is_active and _party_bucket(p.party_type) == bucket)

        active_total = sum(1 for p in parties if p.is_active)

        type_choices = [
            ('loan', 'Loan Parties',
             [(value, lbl) for value, lbl in PARTY_TYPES if value in LOAN_PARTY_TYPES]),
            ('rental', 'External Customers (HDC Tools)',
             [(value, lbl) for value, lbl in PARTY_TYPES if value in RENTAL_PARTY_TYPES]),
            ('other', 'Other Parties',
             [(value, lbl) for value, lbl in PARTY_TYPES
              if value not in LOAN_PARTY_TYPES and value not in RENTAL_PARTY_TYPES]),
        ]

        return render_template(
            'parties/parties_directory.html',
            groups=groups,
            type_choices=type_choices,
            search=search,
            counts={
                'loan': _count('loan'),
                'rental': _count('rental'),
                'other': _count('other'),
                'active_total': active_total,
            },
        )
