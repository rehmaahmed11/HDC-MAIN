"""HDC routes: Office management: staff, attendance, ledger, expenses, allowances.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func

from hdc.extensions import _money_write_required, db
from hdc.models.office import AllowanceCategory, OfficeExpense, OfficeExpenseCategory, OfficeStaff, OfficeStaffAttendance, OfficeStaffLedger, StaffAllowance
from hdc.models.projects import Project, Stage
from hdc.services.accounts import _accounts_set_void_by_source, _accounts_upsert_office_expense_txn, _accounts_upsert_office_staff_ledger_txn
from hdc.services.ledger import _get_all_office_expense_categories, _get_all_office_expense_category_rows, _is_linked_office_salary_expense, _office_expense_total, _office_staff_ledger_snapshot, _remove_office_salary_expense_for_ledger, _sync_office_staff_expense_from_ledger
from hdc.services.lookups import _next_office_staff_code
from hdc.services.receipts import _receipt_company_profile
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _activity_at_for, _amount_to_words, _flt, _parse_date
from hdc.utils.normalize import _normalize_expense_category_name

def register(app):
    """Register Office management: staff, attendance, ledger, expenses, allowances."""
    # —— Office Management ——————————————————————————————————————————————————————
    @app.route('/hdc/office-management')
    @login_required
    def hdc_office_management():
        staff_count = int(OfficeStaff.query.count() or 0)
        active_staff_count = int(OfficeStaff.query.filter(OfficeStaff.active_status == True).count() or 0)
        office_expense_total = _office_expense_total()
        office_expense_month = float(
            db.session.query(func.coalesce(func.sum(OfficeExpense.amount), 0.0))
            .filter(
                OfficeExpense.is_void == False,
                func.strftime('%Y-%m', OfficeExpense.date) == _pkt_today().strftime('%Y-%m')
            )
            .scalar() or 0.0
        )
        allowance_categories_count = int(AllowanceCategory.query.filter(AllowanceCategory.is_active == True).count() or 0)
        return render_template('office/office_management.html',
            staff_count=staff_count,
            active_staff_count=active_staff_count,
            office_expense_total=office_expense_total,
            office_expense_month=office_expense_month,
            allowance_categories_count=allowance_categories_count
        )


    @app.route('/hdc/office-management/staff')
    @login_required
    def hdc_office_staff_home():
        staff_count = int(OfficeStaff.query.count() or 0)
        attendance_days = int(
            db.session.query(func.count(OfficeStaffAttendance.id))
            .filter(OfficeStaffAttendance.date == _pkt_today())
            .scalar() or 0
        )
        ledger_entries = int(db.session.query(func.count(OfficeStaffLedger.id)).scalar() or 0)
        return render_template('office/office_staff_home.html',
            staff_count=staff_count,
            attendance_days=attendance_days,
            ledger_entries=ledger_entries
        )


    @app.route('/hdc/office-management/staff/ledger', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_ledger_list():
        if request.method == 'POST':
            code = (request.form.get('staff_code') or '').strip() or _next_office_staff_code()
            name = (request.form.get('name') or '').strip()
            role_type = (request.form.get('role_type') or '').strip()
            phone = (request.form.get('phone') or '').strip()
            monthly_salary = _flt(request.form.get('monthly_salary'))
            if not name:
                flash('Staff name is required.', 'warning')
                return redirect(url_for('hdc_office_staff_ledger_list'))
            if OfficeStaff.query.filter(OfficeStaff.staff_code == code).first():
                flash('Staff code already exists.', 'danger')
                return redirect(url_for('hdc_office_staff_ledger_list'))
            row = OfficeStaff(
                staff_code=code,
                name=name,
                role_type=role_type,
                phone=phone,
                monthly_salary=monthly_salary,
                active_status=True
            )
            db.session.add(row)
            db.session.commit()
            flash(f'Office staff "{row.name}" added.', 'success')
            return redirect(url_for('hdc_office_staff_ledger_list'))

        staff_rows = OfficeStaff.query.order_by(OfficeStaff.created_at.desc(), OfficeStaff.id.desc()).all()
        snapshots = {int(s.id): _office_staff_ledger_snapshot(s.id) for s in staff_rows}
        return render_template('office/office_staff_ledger_list.html',
            staff_rows=staff_rows,
            snapshots=snapshots,
            next_staff_code=_next_office_staff_code()
        )


    @app.route('/hdc/office-management/staff/<int:sid>/edit', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_edit(sid):
        row = OfficeStaff.query.get_or_404(sid)
        code = (request.form.get('staff_code') or '').strip()
        name = (request.form.get('name') or '').strip()
        if not code or not name:
            flash('Staff code and name are required.', 'warning')
            return redirect(url_for('hdc_office_staff_ledger_list'))
        exists = OfficeStaff.query.filter(OfficeStaff.staff_code == code, OfficeStaff.id != sid).first()
        if exists:
            flash('Staff code already exists for another member.', 'danger')
            return redirect(url_for('hdc_office_staff_ledger_list'))
        row.staff_code = code
        row.name = name
        row.role_type = (request.form.get('role_type') or '').strip()
        row.phone = (request.form.get('phone') or '').strip()
        row.monthly_salary = _flt(request.form.get('monthly_salary'))
        row.active_status = (request.form.get('active_status') or 'active').strip().lower() == 'active'
        db.session.commit()
        flash(f'Office staff "{row.name}" updated.', 'success')
        return redirect(url_for('hdc_office_staff_ledger_list'))


    @app.route('/hdc/office-management/staff/<int:sid>/toggle', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_toggle(sid):
        row = OfficeStaff.query.get_or_404(sid)
        row.active_status = not bool(row.active_status)
        db.session.commit()
        flash(f'Office staff "{row.name}" {"activated" if row.active_status else "suspended"}.', 'success')
        return redirect(url_for('hdc_office_staff_ledger_list'))


    @app.route('/hdc/office-management/staff/<int:sid>/ledger', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_ledger(sid):
        row = OfficeStaff.query.get_or_404(sid)
        if request.method == 'POST':
            entry_date = _parse_date(request.form.get('date'))
            entry_type = (request.form.get('entry_type') or 'advance').strip().lower()
            amount = _flt(request.form.get('amount'))
            notes = (request.form.get('notes') or '').strip()
            if entry_type not in ('advance', 'payment', 'settlement'):
                flash('Invalid ledger entry type.', 'danger')
                return redirect(url_for('hdc_office_staff_ledger', sid=sid))
            if amount <= 0:
                flash('Amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_office_staff_ledger', sid=sid))
            if _has_recent_duplicate(
                OfficeStaffLedger,
                staff_id=sid,
                entry_type=entry_type,
                amount=amount,
                date=entry_date,
                notes=notes
            ):
                flash('Duplicate ledger entry prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_office_staff_ledger', sid=sid))
            row_entry = OfficeStaffLedger(
                staff_id=sid,
                date=entry_date,
                entry_type=entry_type,
                amount=amount,
                notes=notes,
                activity_at=_activity_at_for(entry_date)
            )
            db.session.add(row_entry)
            db.session.flush()
            if entry_type in ('advance', 'payment'):
                _sync_office_staff_expense_from_ledger(row, row_entry)
                ok_txn, msg_txn, _ = _accounts_upsert_office_staff_ledger_txn(row, row_entry, commit=False)
                if not ok_txn:
                    db.session.rollback()
                    flash(msg_txn or 'Unable to post office staff ledger in unified accounts.', 'danger')
                    return redirect(url_for('hdc_office_staff_ledger', sid=sid))
            db.session.commit()
            flash('Office staff ledger entry recorded.', 'success')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))

        ledger_entries = (OfficeStaffLedger.query
                          .filter(OfficeStaffLedger.staff_id == sid)
                          .order_by(OfficeStaffLedger.activity_at.asc(), OfficeStaffLedger.id.asc())
                          .all())
        snap = _office_staff_ledger_snapshot(sid)
        return render_template('office/office_staff_ledger.html',
            staff=row,
            ledger_entries=ledger_entries,
            snap=snap,
            today=_pkt_today().isoformat()
        )


    @app.route('/hdc/office-management/staff/<int:sid>/payment', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_payment(sid):
        staff_row = OfficeStaff.query.get_or_404(sid)
        snap = _office_staff_ledger_snapshot(sid)
        if request.method == 'POST':
            entry_date = _parse_date(request.form.get('date'))
            amount = _flt(request.form.get('amount'))
            notes = (request.form.get('notes') or '').strip()
            if amount <= 0:
                flash('Payment amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_office_staff_payment', sid=sid))
            if _has_recent_duplicate(
                OfficeStaffLedger,
                staff_id=sid,
                entry_type='payment',
                amount=amount,
                date=entry_date,
                notes=notes
            ):
                flash('Duplicate payment prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_office_staff_payment', sid=sid))
            row_entry = OfficeStaffLedger(
                staff_id=sid,
                date=entry_date,
                entry_type='payment',
                amount=amount,
                notes=notes,
                activity_at=_activity_at_for(entry_date)
            )
            db.session.add(row_entry)
            db.session.flush()
            _sync_office_staff_expense_from_ledger(staff_row, row_entry)
            ok_txn, msg_txn, _ = _accounts_upsert_office_staff_ledger_txn(staff_row, row_entry, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to post office salary in unified accounts.', 'danger')
                return redirect(url_for('hdc_office_staff_payment', sid=sid))
            db.session.commit()
            flash(f'Salary payment of {amount:,.0f} PKR recorded for {staff_row.name} and posted to Office Expenses.', 'success')
            if row_entry and row_entry.id:
                return redirect(url_for('hdc_office_staff_payment_receipt', sid=sid, lid=row_entry.id))
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        return render_template('office/office_staff_payment.html',
            staff=staff_row,
            snap=snap,
            today=_pkt_today().isoformat()
        )


    @app.route('/hdc/office-management/staff/<int:sid>/payment/<int:lid>/receipt')
    @login_required
    def hdc_office_staff_payment_receipt(sid, lid):
        staff_row = OfficeStaff.query.get_or_404(sid)
        row = OfficeStaffLedger.query.get_or_404(lid)
        if row.staff_id != sid:
            abort(404)
        receipt_id = f'RCPT-OS-{row.id:08d}'
        recent = (
            OfficeStaffLedger.query
            .filter(OfficeStaffLedger.staff_id == sid, OfficeStaffLedger.id != row.id, OfficeStaffLedger.is_void == False)
            .order_by(OfficeStaffLedger.activity_at.desc(), OfficeStaffLedger.id.desc())
            .limit(5).all()
        )
        recent_entries = [{
            'date': (r.date.strftime('%Y-%m-%d') if r.date else '-'),
            'type': (r.entry_type or 'payment').title(),
            'direction': 'pay',
            'party': staff_row.name,
            'amount': float(r.amount or 0),
            'receipt_url': url_for('hdc_office_staff_payment_receipt', sid=sid, lid=r.id)
        } for r in recent]
        return render_template('accounts/transaction_receipt.html',
            company_profile=_receipt_company_profile(),
            receipt_id=receipt_id,
            created_at=(row.activity_at or _pkt_now_naive()),
            tx_type=f'Office Staff {(row.entry_type or "Payment").title()} Receipt',
            party_name=staff_row.name,
            project_name='-',
            stage_name='-',
            account_used='-',
            amount=float(row.amount or 0),
            amount_words=_amount_to_words(row.amount or 0),
            note=(row.notes or ''),
            reference_id=f'office_staff_ledger#{row.id}',
            recent_entries=recent_entries,
            recent_entries_title=f'Last 5 Ledger Entries – {staff_row.name}',
            back_url=url_for('hdc_office_staff_ledger', sid=sid),
            print_label='Print / Save PDF'
        )


    @app.route('/hdc/office-management/staff/<int:sid>/ledger/<int:lid>/edit', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_ledger_edit(sid, lid):
        row = OfficeStaff.query.get_or_404(sid)
        entry = OfficeStaffLedger.query.get_or_404(lid)
        if entry.staff_id != sid:
            flash('Ledger entry does not belong to selected staff.', 'danger')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        if entry.is_void:
            flash('Voided entry cannot be edited.', 'warning')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        if entry.entry_type not in ('advance', 'payment', 'tip', 'settlement'):
            flash('Only advance/payment/tip/settlement entries are editable.', 'warning')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        if request.method == 'POST':
            entry_date = _parse_date(request.form.get('date'))
            amount = _flt(request.form.get('amount'))
            if amount <= 0:
                flash('Amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_office_staff_ledger_edit', sid=sid, lid=lid))
            entry.date = entry_date
            entry.amount = amount
            entry.notes = (request.form.get('notes') or '').strip()
            entry.activity_at = _activity_at_for(entry_date)
            if entry.entry_type in ('advance', 'payment', 'tip'):
                _sync_office_staff_expense_from_ledger(row, entry)
                ok_txn, msg_txn, _ = _accounts_upsert_office_staff_ledger_txn(row, entry, commit=False)
                if not ok_txn:
                    db.session.rollback()
                    flash(msg_txn or 'Unable to sync office staff ledger in unified accounts.', 'danger')
                    return redirect(url_for('hdc_office_staff_ledger_edit', sid=sid, lid=lid))
            db.session.commit()
            flash('Ledger entry updated.', 'success')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        return render_template('workers/worker_ledger_entry_edit.html', w=row, entry=entry, projects=Project.query.order_by(Project.name.asc()).all(), stages=Stage.query.order_by(Stage.project_id.asc(), Stage.name.asc()).all())


    @app.route('/hdc/office-management/staff/<int:sid>/ledger/<int:lid>/void', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_ledger_void(sid, lid):
        OfficeStaff.query.get_or_404(sid)
        entry = OfficeStaffLedger.query.get_or_404(lid)
        if entry.staff_id != sid:
            flash('Ledger entry does not belong to selected staff.', 'danger')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        if entry.is_void:
            flash('Ledger entry is already voided.', 'info')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        if entry.entry_type not in ('advance', 'payment', 'tip', 'settlement'):
            flash('Only advance/payment/tip/settlement entries can be voided.', 'warning')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
        entry.is_void = True
        entry.void_reason = reason
        entry.voided_at = _pkt_now_naive()
        if entry.entry_type in ('advance', 'payment', 'tip'):
            _remove_office_salary_expense_for_ledger(entry.id)
            _accounts_set_void_by_source(f'office_staff_ledger_{entry.entry_type}', entry.id, True)
        db.session.commit()
        flash('Ledger entry voided.', 'success')
        return redirect(url_for('hdc_office_staff_ledger', sid=sid))


    @app.route('/hdc/office-management/staff/attendance', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_attendance():
        attendance_date = _parse_date(request.values.get('date'), fallback=_pkt_today())
        if request.method == 'POST':
            attendance_date = _parse_date(request.form.get('date'), fallback=_pkt_today())
            staff_rows = OfficeStaff.query.filter(OfficeStaff.active_status == True).order_by(OfficeStaff.name.asc()).all()
            for s in staff_rows:
                status = (request.form.get(f'status_{s.id}') or 'present').strip().lower()
                notes = (request.form.get(f'notes_{s.id}') or '').strip()
                if status not in ('present', 'absent', 'weekly_leave'):
                    status = 'present'
                row = OfficeStaffAttendance.query.filter_by(staff_id=s.id, date=attendance_date).first()
                if not row:
                    row = OfficeStaffAttendance(staff_id=s.id, date=attendance_date)
                    db.session.add(row)
                row.status = status
                row.notes = notes
                row.activity_at = _activity_at_for(attendance_date)
                row.updated_at = _pkt_now_naive()
            db.session.commit()
            flash('Office attendance saved.', 'success')
            return redirect(url_for('hdc_office_staff_attendance', date=attendance_date.isoformat()))

        staff_rows = OfficeStaff.query.order_by(OfficeStaff.name.asc()).all()
        existing_rows = {
            int(r.staff_id): r
            for r in OfficeStaffAttendance.query.filter(OfficeStaffAttendance.date == attendance_date).all()
        }
        recent_records = (db.session.query(OfficeStaffAttendance, OfficeStaff)
                          .join(OfficeStaff, OfficeStaffAttendance.staff_id == OfficeStaff.id)
                          .order_by(OfficeStaffAttendance.activity_at.desc(), OfficeStaffAttendance.id.desc())
                          .limit(120)
                          .all())
        present_count = int(sum(1 for r in existing_rows.values() if (r.status or '').lower() == 'present'))
        absent_count = int(sum(1 for r in existing_rows.values() if (r.status or '').lower() == 'absent'))
        return render_template('office/office_staff_attendance.html',
            staff_rows=staff_rows,
            existing_rows=existing_rows,
            recent_records=recent_records,
            attendance_date=attendance_date.isoformat(),
            present_count=present_count,
            absent_count=absent_count
        )


    @app.route('/hdc/office-management/expenses', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_office_expenses():
        if request.method == 'POST':
            exp_date = _parse_date(request.form.get('date'))
            category = _normalize_expense_category_name(request.form.get('category'))
            amount = _flt(request.form.get('amount'))
            remarks = (request.form.get('remarks') or '').strip()
            if not category:
                flash('Expense category is required.', 'danger')
                return redirect(url_for('hdc_office_expenses'))
            if amount <= 0:
                flash('Expense amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_office_expenses'))
            if _has_recent_duplicate(
                OfficeExpense,
                date=exp_date,
                category=category,
                amount=amount,
                remarks=remarks,
                is_void=False
            ):
                flash('Duplicate office expense prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_office_expenses'))
            row = OfficeExpense(
                date=exp_date,
                category=category,
                amount=amount,
                remarks=remarks,
                is_void=False,
                activity_at=_activity_at_for(exp_date)
            )
            db.session.add(row)
            db.session.flush()
            ok_txn, msg_txn, _ = _accounts_upsert_office_expense_txn(row, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to post office expense in unified accounts.', 'danger')
                return redirect(url_for('hdc_office_expenses'))
            db.session.commit()
            flash('Office expense added.', 'success')
            return redirect(url_for('hdc_office_expenses'))

        category_filter = _normalize_expense_category_name(request.args.get('category'))
        q = db.session.query(OfficeExpense, OfficeStaff).outerjoin(
            OfficeStaff, OfficeExpense.office_staff_id == OfficeStaff.id
        ).filter(OfficeExpense.is_void == False)
        if category_filter:
            q = q.filter(func.lower(OfficeExpense.category) == category_filter.lower())
        records = q.order_by(OfficeExpense.activity_at.desc(), OfficeExpense.id.desc()).all()
        total = float(sum(float(r[0].amount or 0.0) for r in records))
        categories = _get_all_office_expense_categories()
        return render_template('office/office_expenses.html',
            records=records,
            total=total,
            categories=categories,
            office_expense_categories=_get_all_office_expense_category_rows(),
            category_filter=category_filter,
            today=_pkt_today().isoformat()
        )


    @app.route('/hdc/office-management/expenses/<int:eid>/edit', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_office_expense_edit(eid):
        row = OfficeExpense.query.get_or_404(eid)
        if row.is_void:
            flash('Office expense is already voided.', 'warning')
            return redirect(url_for('hdc_office_expenses'))
        if _is_linked_office_salary_expense(row):
            flash('Linked salary expense is managed from staff ledger payment. Edit it there.', 'warning')
            return redirect(url_for('hdc_office_expenses'))
        if request.method == 'POST':
            exp_date = _parse_date(request.form.get('date'))
            category = _normalize_expense_category_name(request.form.get('category'))
            amount = _flt(request.form.get('amount'))
            remarks = (request.form.get('remarks') or '').strip()
            if not category:
                flash('Expense category is required.', 'danger')
                return redirect(url_for('hdc_office_expense_edit', eid=eid))
            if amount <= 0:
                flash('Expense amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_office_expense_edit', eid=eid))
            row.date = exp_date
            row.category = category
            row.amount = amount
            row.remarks = remarks
            row.activity_at = _activity_at_for(exp_date)
            ok_txn, msg_txn, _ = _accounts_upsert_office_expense_txn(row, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to sync office expense in unified accounts.', 'danger')
                return redirect(url_for('hdc_office_expense_edit', eid=eid))
            db.session.commit()
            flash('Office expense updated.', 'success')
            return redirect(url_for('hdc_office_expenses'))
        return render_template('office/office_expense_edit.html',
            row=row,
            today=(row.date or _pkt_today()).isoformat()
        )


    @app.route('/hdc/office-management/expenses/<int:eid>/delete', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_office_expense_delete(eid):
        row = OfficeExpense.query.get_or_404(eid)
        if row.is_void:
            flash('Office expense is already voided.', 'info')
            return redirect(url_for('hdc_office_expenses'))
        if _is_linked_office_salary_expense(row):
            flash('Linked salary expense is managed from staff ledger payment. Void it there.', 'warning')
            return redirect(url_for('hdc_office_expenses'))
        row.is_void = True
        row.void_reason = 'Voided from Office Expenses'
        row.voided_at = _pkt_now_naive()
        _accounts_set_void_by_source('office_expense', row.id, True)
        db.session.commit()
        flash('Office expense voided.', 'success')
        return redirect(url_for('hdc_office_expenses'))


    # â”€â”€ Allowance Categories â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/hdc/office-management/allowance-categories', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_allowance_categories():
        if request.method == 'POST':
            name = (request.form.get('name') or '').strip()
            description = (request.form.get('description') or '').strip()
            if not name:
                flash('Allowance category name is required.', 'danger')
                return redirect(url_for('hdc_allowance_categories'))
            if AllowanceCategory.query.filter(func.lower(AllowanceCategory.name) == name.lower()).first():
                flash('Allowance category with this name already exists.', 'danger')
                return redirect(url_for('hdc_allowance_categories'))
            row = AllowanceCategory(name=name, description=description, is_active=True)
            db.session.add(row)
            db.session.commit()
            flash(f'Allowance category "{name}" added.', 'success')
            return redirect(url_for('hdc_allowance_categories'))

        categories = AllowanceCategory.query.order_by(AllowanceCategory.name.asc()).all()
        return render_template('office/allowance_categories.html', categories=categories)


    @app.route('/hdc/office-management/allowance-categories/<int:cid>/edit', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_allowance_category_edit(cid):
        row = AllowanceCategory.query.get_or_404(cid)
        if request.method == 'POST':
            name = (request.form.get('name') or '').strip()
            description = (request.form.get('description') or '').strip()
            if not name:
                flash('Allowance category name is required.', 'danger')
                return redirect(url_for('hdc_allowance_category_edit', cid=cid))
            existing = AllowanceCategory.query.filter(
                func.lower(AllowanceCategory.name) == name.lower(),
                AllowanceCategory.id != cid
            ).first()
            if existing:
                flash('Another allowance category with this name already exists.', 'danger')
                return redirect(url_for('hdc_allowance_category_edit', cid=cid))
            row.name = name
            row.description = description
            db.session.commit()
            flash(f'Allowance category "{name}" updated.', 'success')
            return redirect(url_for('hdc_allowance_categories'))
        return render_template('office/allowance_category_edit.html', category=row)


    @app.route('/hdc/office-management/allowance-categories/<int:cid>/toggle', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_allowance_category_toggle(cid):
        row = AllowanceCategory.query.get_or_404(cid)
        row.is_active = not bool(row.is_active)
        db.session.commit()
        status = 'activated' if row.is_active else 'deactivated'
        flash(f'Allowance category "{row.name}" {status}.', 'success')
        return redirect(url_for('hdc_allowance_categories'))


    # â”€â”€ Staff Allowances â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/hdc/office-management/staff/<int:sid>/allowances', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_allowances(sid):
        staff = OfficeStaff.query.get_or_404(sid)
        if request.method == 'POST':
            category_id = request.form.get('category_id', type=int)
            amount = _flt(request.form.get('amount'))
            effective_date = _parse_date(request.form.get('effective_date'), fallback=_pkt_today())

            if not category_id:
                flash('Allowance category is required.', 'danger')
                return redirect(url_for('hdc_office_staff_allowances', sid=sid))
            if amount <= 0:
                flash('Allowance amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_office_staff_allowances', sid=sid))

            category = AllowanceCategory.query.get_or_404(category_id)
            if not category.is_active:
                flash('Selected allowance category is not active.', 'danger')
                return redirect(url_for('hdc_office_staff_allowances', sid=sid))

            # Check if this category is already assigned to this staff
            existing = StaffAllowance.query.filter_by(
                staff_id=sid,
                category_id=category_id,
                is_active=True
            ).first()
            if existing:
                flash(f'Allowance category "{category.name}" is already assigned to this staff.', 'warning')
                return redirect(url_for('hdc_office_staff_allowances', sid=sid))

            allowance = StaffAllowance(
                staff_id=sid,
                category_id=category_id,
                amount=amount,
                effective_date=effective_date,
                is_active=True
            )
            db.session.add(allowance)
            db.session.commit()
            flash(f'Allowance "{category.name}" added to {staff.name}.', 'success')
            return redirect(url_for('hdc_office_staff_allowances', sid=sid))

        allowances = (db.session.query(StaffAllowance, AllowanceCategory)
                      .join(AllowanceCategory, StaffAllowance.category_id == AllowanceCategory.id)
                      .filter(StaffAllowance.staff_id == sid, StaffAllowance.is_active == True)
                      .order_by(AllowanceCategory.name.asc())
                      .all())

        available_categories = AllowanceCategory.query.filter(
            AllowanceCategory.is_active == True,
            ~AllowanceCategory.id.in_([a.category_id for a, _ in allowances])
        ).order_by(AllowanceCategory.name.asc()).all()

        total_allowances = sum(a.amount for a, _ in allowances)

        return render_template('office/office_staff_allowances.html',
            staff=staff,
            allowances=allowances,
            available_categories=available_categories,
            total_allowances=total_allowances,
            today=_pkt_today().isoformat()
        )


    @app.route('/hdc/office-management/staff/<int:sid>/allowances/<int:aid>/edit', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_allowance_edit(sid, aid):
        staff = OfficeStaff.query.get_or_404(sid)
        allowance = StaffAllowance.query.get_or_404(aid)
        if allowance.staff_id != sid:
            abort(404)

        if request.method == 'POST':
            amount = _flt(request.form.get('amount'))
            effective_date = _parse_date(request.form.get('effective_date'))

            if amount <= 0:
                flash('Allowance amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_office_staff_allowance_edit', sid=sid, aid=aid))

            allowance.amount = amount
            allowance.effective_date = effective_date
            db.session.commit()
            flash('Allowance updated.', 'success')
            return redirect(url_for('hdc_office_staff_allowances', sid=sid))

        return render_template('office/office_staff_allowance_edit.html',
            staff=staff,
            allowance=allowance,
            today=allowance.effective_date.isoformat() if allowance.effective_date else _pkt_today().isoformat()
        )


    @app.route('/hdc/office-management/staff/<int:sid>/allowances/<int:aid>/remove', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_office_staff_allowance_remove(sid, aid):
        staff = OfficeStaff.query.get_or_404(sid)
        allowance = StaffAllowance.query.get_or_404(aid)
        if allowance.staff_id != sid:
            abort(404)

        allowance.is_active = False
        db.session.commit()
        flash(f'Allowance removed from {staff.name}.', 'success')
        return redirect(url_for('hdc_office_staff_allowances', sid=sid))


    @app.route('/hdc/api/office_expense_categories', methods=['GET', 'POST'])
    @login_required
    @_money_write_required(api=True)
    def hdc_api_office_expense_categories():
        if request.method == 'GET':
            return jsonify({'categories': _get_all_office_expense_categories()})
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            name = _normalize_expense_category_name(data.get('name') or '')
            if not name:
                return jsonify({'ok': False, 'error': 'Category name is required.'}), 400
            existing = OfficeExpenseCategory.query.filter(
                func.lower(OfficeExpenseCategory.name) == name.lower()
            ).first()
            if existing:
                if not existing.active_status:
                    existing.active_status = True
                    db.session.commit()
            else:
                db.session.add(OfficeExpenseCategory(name=name, active_status=True))
                db.session.commit()
            return jsonify({'ok': True, 'name': name, 'categories': _get_all_office_expense_categories()})


    @app.route('/hdc/api/office_expense_categories/<int:category_id>', methods=['PATCH'])
    @login_required
    @_money_write_required(api=True)
    def hdc_api_office_expense_category(category_id):
        data = request.get_json(silent=True) or {}
        action = (data.get('action') or '').strip().lower()
        row = OfficeExpenseCategory.query.get_or_404(category_id)
        if action == 'suspend':
            row.active_status = False
            db.session.commit()
            return jsonify({'ok': True, 'categories': _get_all_office_expense_categories()})

        if action == 'activate':
            row.active_status = True
            db.session.commit()
            return jsonify({'ok': True, 'categories': _get_all_office_expense_categories()})

        if action == 'rename':
            name = _normalize_expense_category_name(data.get('name') or '')
            if not name:
                return jsonify({'ok': False, 'error': 'New category name is required.'}), 400
            existing = OfficeExpenseCategory.query.filter(
                func.lower(OfficeExpenseCategory.name) == name.lower(),
                OfficeExpenseCategory.id != int(category_id)
            ).first()
            if existing:
                return jsonify({'ok': False, 'error': 'A category with that name already exists.'}), 400
            row.name = name
            row.active_status = True
            db.session.commit()
            return jsonify({'ok': True, 'name': name, 'categories': _get_all_office_expense_categories()})

        return jsonify({'ok': False, 'error': 'Unsupported category action.'}), 400
