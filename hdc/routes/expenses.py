"""HDC routes: Site expenses and alerts.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from hdc.extensions import _money_write_required, db
from hdc.models.accounts import Alert, Expense, ExpenseCategory
from hdc.models.projects import Project, Stage
from hdc.services.accounts import _accounts_post_expense_row
from hdc.services.audit import log_action
from hdc.services.ledger import _is_linked_system_expense
from hdc.services.lookups import _category_name_from_id, _ensure_expense_category_by_id
from hdc.services.reporting import _refresh_alerts
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _activity_at_for, _flt, _parse_date
from hdc.utils.normalize import _normalize_expense_category_name

def register(app):
    """Register Site expenses and alerts."""
    @app.route('/hdc/expense_categories', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_expense_categories():
        if request.method == 'POST':
            action = (request.form.get('action') or '').strip()
            if action == 'add_category':
                category_name = _normalize_expense_category_name(request.form.get('category_name'))
                if not category_name:
                    flash('Category name is required.', 'warning')
                else:
                    existing = db.session.query(ExpenseCategory).filter(
                        func.lower(ExpenseCategory.name) == category_name.lower()
                    ).first()
                    if existing:
                        if not existing.active_status:
                            existing.active_status = True
                            db.session.commit()
                            flash(f'Category "{category_name}" restored.', 'success')
                        else:
                            flash('Category already exists.', 'warning')
                    else:
                        db.session.add(ExpenseCategory(name=category_name, active_status=True))
                        db.session.commit()
                        flash(f'Category "{category_name}" added.', 'success')
            elif action == 'edit_category':
                category_id = request.form.get('category_id', type=int)
                category = ExpenseCategory.query.get(category_id)
                new_name = _normalize_expense_category_name(request.form.get('category_name'))
                if not category:
                    flash('Category not found.', 'danger')
                elif not new_name:
                    flash('Category name is required.', 'warning')
                else:
                    dupe = db.session.query(ExpenseCategory).filter(
                        func.lower(ExpenseCategory.name) == new_name.lower(),
                        ExpenseCategory.id != category.id
                    ).first()
                    if dupe:
                        flash('Another category with this name already exists.', 'warning')
                    else:
                        category.name = new_name
                        db.session.commit()
                        flash(f'Category updated to "{new_name}".', 'success')
            elif action == 'delete_category':
                category_id = request.form.get('category_id', type=int)
                category = ExpenseCategory.query.get(category_id)
                if not category:
                    flash('Category not found.', 'danger')
                else:
                    category.active_status = False
                    db.session.commit()
                    flash(f'Category "{category.name}" removed from active list.', 'success')
            return redirect(url_for('hdc_expense_categories'))

        categories = ExpenseCategory.query.filter_by(active_status=True).order_by(ExpenseCategory.name).all()
        return render_template('expenses/expense_categories.html', categories=categories)


    # Ã¢â€â‚¬Ã¢â€â‚¬ Alerts Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    @app.route('/hdc/alerts')
    @login_required
    def hdc_alerts():
        _refresh_alerts()
        alerts = Alert.query.order_by(Alert.created_at.desc()).all()
        return render_template('expenses/alerts.html', alerts=alerts)


    @app.route('/hdc/alerts/<int:aid>/resolve', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_alerts_resolve(aid):
        a = Alert.query.get_or_404(aid)
        a.resolved = True
        db.session.commit()
        flash('Alert resolved.', 'success')
        return redirect(url_for('hdc_alerts'))


    # â”€â”€ Expenses â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/hdc/expenses', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_expenses():
        if request.method == 'POST':
            pid = request.form.get('project_id', type=int)
            if not pid:
                flash('Project is required.', 'danger')
                return redirect(url_for('hdc_expenses'))
            sid = request.form.get('stage_id', type=int)
            if not sid:
                flash('Stage is required.', 'danger')
                return redirect(url_for('hdc_expenses'))
            stage = Stage.query.get(sid)
            if (not stage) or (stage.project_id != pid):
                flash('Selected stage does not belong to selected project.', 'danger')
                return redirect(url_for('hdc_expenses'))
            category_id = request.form.get('category_id', type=int)
            if not category_id:
                flash('Expense category is required.', 'danger')
                return redirect(url_for('hdc_expenses'))
            valid_category = _ensure_expense_category_by_id(category_id)
            if not valid_category:
                flash('Please select a valid active expense category.', 'danger')
                return redirect(url_for('hdc_expenses'))
            exp_date = _parse_date(request.form.get('date'))
            amount = _flt(request.form.get('amount'))
            if amount <= 0:
                flash('Expense amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_expenses'))
            remarks = (request.form.get('remarks','') or '').strip()
            if _has_recent_duplicate(
                Expense,
                project_id=pid,
                stage_id=sid,
                category_id=category_id,
                amount=amount,
                date=exp_date,
                remarks=remarks
            ):
                flash('Duplicate expense prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_expenses'))
            exp = Expense(
                project_id=pid, stage_id=sid,
                category_id=category_id,
                amount=amount,
                date=exp_date,
                activity_at=_activity_at_for(exp_date),
                remarks=remarks,
                is_void=False
            )
            db.session.add(exp)
            db.session.flush()
            ok_txn, msg_txn, _ = _accounts_post_expense_row(exp, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to post expense in unified accounts.', 'danger')
                return redirect(url_for('hdc_expenses'))
            log_action(
                current_user,
                'create',
                f'{current_user.username.title()} created expense: {valid_category.name}, {amount:,.2f} PKR, {stage.project.name} -> {stage.name} on {exp_date.isoformat()}',
                'expense',
                exp.id or ''
            )
            db.session.commit()
            flash('Expense added.', 'success')
            return redirect(url_for('hdc_expenses'))

        projects    = Project.query.all()
        project_id  = request.args.get('project_id', type=int)
        q = (db.session.query(Expense, Project, Stage)
             .join(Project, Expense.project_id == Project.id)
             .outerjoin(Stage, Expense.stage_id == Stage.id))
        q = q.filter(Expense.is_void == False)
        if project_id: q = q.filter(Expense.project_id == project_id)
        records = q.order_by(Expense.activity_at.desc(), Expense.date.desc()).all()
        total   = sum((r[0].amount or 0.0) for r in records)
        categories = ExpenseCategory.query.filter_by(active_status=True).order_by(ExpenseCategory.name.asc()).all()
        return render_template('expenses/expenses.html',
            projects=projects, records=records, total=total,
            selected_project=project_id, today=_pkt_today().isoformat(),
            categories=categories)


    @app.route('/hdc/expenses/<int:eid>/edit', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_expense_edit(eid):
        exp = Expense.query.get_or_404(eid)
        if _is_linked_system_expense(exp):
            flash('Linked Tip/Settlement expense cannot be edited here. Please update the original payment/settlement entry.', 'warning')
            return redirect(url_for('hdc_expenses', project_id=exp.project_id))

        projects = Project.query.all()
        categories = ExpenseCategory.query.filter_by(active_status=True).order_by(ExpenseCategory.name.asc()).all()
        if request.method == 'POST':
            pid = request.form.get('project_id', type=int)
            sid = request.form.get('stage_id', type=int)
            if not pid:
                flash('Project is required.', 'danger')
                return redirect(url_for('hdc_expense_edit', eid=eid))
            if not sid:
                flash('Stage is required.', 'danger')
                return redirect(url_for('hdc_expense_edit', eid=eid))
            stage = Stage.query.get(sid)
            if (not stage) or (stage.project_id != pid):
                flash('Selected stage does not belong to selected project.', 'danger')
                return redirect(url_for('hdc_expense_edit', eid=eid))
            category_id = request.form.get('category_id', type=int)
            if not category_id:
                flash('Expense category is required.', 'danger')
                return redirect(url_for('hdc_expense_edit', eid=eid))
            valid_category = _ensure_expense_category_by_id(category_id)
            if not valid_category:
                flash('Please select a valid active expense category.', 'danger')
                return redirect(url_for('hdc_expense_edit', eid=eid))
            amount = _flt(request.form.get('amount'))
            if amount <= 0:
                flash('Expense amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_expense_edit', eid=eid))
            exp_date = _parse_date(request.form.get('date'))
            old_project = Project.query.get(exp.project_id)
            old_stage = Stage.query.get(exp.stage_id) if exp.stage_id else None
            old_cat = _category_name_from_id(exp.category_id)
            old_amount = float(exp.amount or 0.0)
            exp.project_id = pid
            exp.stage_id = sid
            exp.category_id = category_id
            exp.amount = amount
            exp.date = exp_date
            exp.activity_at = _activity_at_for(exp_date)
            exp.remarks = (request.form.get('remarks', '') or '').strip()
            log_action(
                current_user,
                'update',
                (
                    f'{current_user.username.title()} updated expense: '
                    f'Category {old_cat} -> {valid_category.name}; '
                    f'Amount {old_amount:,.2f} -> {amount:,.2f} PKR; '
                    f'Project/Stage {(old_project.name if old_project else "-")} -> {(old_stage.name if old_stage else "-")} '
                    f'to {stage.project.name} -> {stage.name}'
                ),
                'expense',
                exp.id
            )
            db.session.commit()
            flash('Expense updated.', 'success')
            return redirect(url_for('hdc_expenses', project_id=pid))

        return render_template('expenses/expense_edit.html',
            exp=exp,
            projects=projects,
            categories=categories,
            today=(exp.date or _pkt_today()).isoformat()
        )


    @app.route('/hdc/expenses/<int:eid>/delete', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_expense_delete(eid):
        exp = Expense.query.get_or_404(eid)
        if _is_linked_system_expense(exp):
            flash('Linked Tip/Settlement expense cannot be deleted here. Please update the original payment/settlement entry.', 'warning')
            return redirect(url_for('hdc_expenses', project_id=exp.project_id))
        pid = exp.project_id
        exp.is_void = True
        exp.void_reason = 'Voided by user'
        exp.voided_at = _pkt_now_naive()
        log_action(
            current_user,
            'void',
            f'{current_user.username.title()} voided expense #{exp.id}: {exp.category or "-"}, {float(exp.amount or 0.0):,.2f} PKR',
            'expense',
            exp.id
        )
        db.session.commit()
        flash('Expense voided.', 'success')
        return redirect(url_for('hdc_expenses', project_id=pid))
