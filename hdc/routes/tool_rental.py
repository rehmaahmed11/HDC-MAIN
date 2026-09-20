"""HDC Tool Rental routes - main hub, inventory, rentals, returns, transfers, tracking, reports.
Now includes Accounts integration: payment receiving in Cash/Bank accounts.
"""

from datetime import datetime
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from hdc.extensions import db
from hdc.models.accounts import Account
from hdc.models.projects import Project, Stage
from hdc.models.tool_rental import (
    Tool, ToolCategory, ToolMovementLog, ToolRental, ToolRentalItem,
    ToolRentalPayment, ToolRentalReturn, ToolRentalReturnItem, ToolRentalTransfer
)
from hdc.services.tool_rental import (
    _ensure_tool_category, _next_rental_code, _next_tool_code,
    create_movement_log, get_rental_tracking_chain,
    get_receiving_accounts, global_tool_locations,
    post_tool_rental_payment_to_accounts, recalc_rental_totals,
    search_rentals, tool_kpis, void_tool_rental_payment_in_accounts
)
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _flt, _amount_to_words
from hdc.services.receipts import _receipt_company_profile
from hdc.services.timekeeping import _has_recent_duplicate


def register(app):

    # ------------------ MAIN DASHBOARD ------------------
    @app.route('/hdc/tool-rental')
    @login_required
    def hdc_tool_rental():
        kpis = tool_kpis()
        filters = {
            'project_id': request.args.get('project_id', type=int),
            'tool_id': request.args.get('tool_id', type=int),
            'renter_type': (request.args.get('renter_type') or '').strip() or None,
            'status': (request.args.get('status') or '').strip() or None,
            'payment_status': (request.args.get('payment_status') or '').strip() or None,
            'billing_type': (request.args.get('billing_type') or '').strip() or None,
            'date_from': (request.args.get('date_from') or '').strip() or None,
            'date_to': (request.args.get('date_to') or '').strip() or None,
            'search_text': (request.args.get('q') or '').strip() or None,
        }
        rentals = search_rentals(filters)
        projects = Project.query.order_by(Project.name.asc()).all()
        tools = Tool.query.filter(Tool.is_void==False).order_by(Tool.name.asc()).all()
        categories = ToolCategory.query.order_by(ToolCategory.name.asc()).all()
        stages = Stage.query.order_by(Stage.name.asc()).all()
        receiving_accounts = get_receiving_accounts()

        return render_template('tool_rental/tool_rental.html',
            kpis=kpis,
            rentals=rentals,
            projects=projects,
            stages=stages,
            tools=tools,
            categories=categories,
            receiving_accounts=receiving_accounts,
            filters=filters,
            today=_pkt_today().isoformat()
        )

    # ------------------ INVENTORY ------------------
    @app.route('/hdc/tool-rental/inventory', methods=['GET','POST'])
    @login_required
    def hdc_tool_rental_inventory():
        if request.method == 'POST':
            action = (request.form.get('action') or 'add').strip()
            if action == 'add_category':
                name = (request.form.get('category_name') or '').strip()
                if not name:
                    flash('Category name required.', 'danger')
                else:
                    _ensure_tool_category(name)
                    db.session.commit()
                    flash(f'Category {name} saved.', 'success')
                return redirect(url_for('hdc_tool_rental_inventory'))

            name = (request.form.get('name') or '').strip()
            if not name:
                flash('Tool name required.', 'danger')
                return redirect(url_for('hdc_tool_rental_inventory'))
            code = (request.form.get('tool_code') or '').strip().upper() or _next_tool_code()
            if Tool.query.filter_by(tool_code=code).first():
                code = _next_tool_code()
            category_id = request.form.get('category_id', type=int)
            total_qty = max(0.0, _flt(request.form.get('total_quantity')))
            purchase_cost = max(0.0, _flt(request.form.get('purchase_cost')))
            rate = max(0.0, _flt(request.form.get('rental_rate_per_day')))
            unit = (request.form.get('unit') or 'pcs').strip()
            condition = (request.form.get('condition') or 'good').strip()
            description = (request.form.get('description') or '').strip()

            tool = Tool(
                tool_code=code,
                name=name,
                category_id=category_id,
                description=description,
                unit=unit,
                total_quantity=total_qty,
                purchase_cost=purchase_cost,
                rental_rate_per_day=rate,
                condition=condition,
                status='active',
                is_void=False
            )
            db.session.add(tool)
            db.session.flush()
            create_movement_log(
                tool_id=tool.id,
                rental_id=None,
                movement_type='purchase_in',
                from_label='Supplier / Purchase',
                to_label='Warehouse / Store',
                qty=total_qty,
                notes=f'Initial stock: {total_qty} {unit}'
            )
            db.session.commit()
            flash(f'Tool {tool.name} ({code}) added with {total_qty} qty.', 'success')
            return redirect(url_for('hdc_tool_rental_inventory'))

        q = (request.args.get('q') or '').strip()
        category_id = request.args.get('category_id', type=int)
        tq = Tool.query.filter(Tool.is_void==False)
        if q:
            ql = f"%{q.lower()}%"
            tq = tq.filter(func.lower(Tool.name).like(ql) | func.lower(Tool.tool_code).like(ql))
        if category_id:
            tq = tq.filter(Tool.category_id==category_id)
        tools = tq.order_by(Tool.name.asc()).all()
        categories = ToolCategory.query.order_by(ToolCategory.name.asc()).all()
        kpis = tool_kpis()
        return render_template('tool_rental/tool_inventory.html',
            tools=tools,
            categories=categories,
            kpis=kpis,
            q=q,
            selected_category=category_id
        )

    @app.route('/hdc/tool-rental/inventory/<int:tool_id>/edit', methods=['POST'])
    @login_required
    def hdc_tool_rental_tool_edit(tool_id):
        tool = Tool.query.get_or_404(tool_id)
        name = (request.form.get('name') or tool.name).strip()
        code = (request.form.get('tool_code') or tool.tool_code).strip().upper()
        if not name:
            flash('Tool name required.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory'))
        exists = Tool.query.filter(Tool.id!=tool.id, func.lower(Tool.tool_code)==code.lower()).first()
        if exists:
            flash('Another tool already uses this code.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory'))
        old_qty = float(tool.total_quantity or 0)
        new_qty = max(0.0, _flt(request.form.get('total_quantity'), old_qty))
        diff = new_qty - old_qty

        tool.name = name
        tool.tool_code = code
        tool.category_id = request.form.get('category_id', type=int)
        tool.unit = (request.form.get('unit') or tool.unit).strip()
        tool.total_quantity = new_qty
        tool.purchase_cost = max(0.0, _flt(request.form.get('purchase_cost'), tool.purchase_cost))
        tool.rental_rate_per_day = max(0.0, _flt(request.form.get('rental_rate_per_day'), tool.rental_rate_per_day))
        tool.condition = (request.form.get('condition') or tool.condition).strip()
        tool.status = (request.form.get('status') or tool.status).strip()
        tool.description = (request.form.get('description') or tool.description).strip()
        tool.updated_at = _pkt_now_naive()

        if abs(diff) > 0.001:
            create_movement_log(
                tool_id=tool.id,
                rental_id=None,
                movement_type='adjustment',
                from_label='Warehouse / Store',
                to_label='Warehouse / Store',
                qty=diff,
                notes=f'Stock adjusted from {old_qty} to {new_qty}'
            )
        db.session.commit()
        flash(f'Tool {tool.name} updated.', 'success')
        return redirect(url_for('hdc_tool_rental_inventory'))

    @app.route('/hdc/tool-rental/inventory/<int:tool_id>/delete', methods=['POST'])
    @login_required
    def hdc_tool_rental_tool_delete(tool_id):
        tool = Tool.query.get_or_404(tool_id)
        pending = db.session.query(func.coalesce(func.sum(ToolRentalItem.qty_pending),0.0)).filter(ToolRentalItem.tool_id==tool.id).scalar() or 0.0
        if float(pending) > 0.001:
            flash(f'Cannot delete {tool.name}: {pending} qty still rented out.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory'))
        tool.is_void = True
        tool.status = 'retired'
        db.session.commit()
        flash(f'Tool {tool.name} archived.', 'success')
        return redirect(url_for('hdc_tool_rental_inventory'))

    # ------------------ CREATE RENTAL ------------------
    @app.route('/hdc/tool-rental/create', methods=['POST'])
    @login_required
    def hdc_tool_rental_create():
        renter_type = (request.form.get('renter_type') or 'internal').strip().lower()
        if renter_type not in ('internal','external'):
            renter_type = 'internal'
        project_id = request.form.get('project_id', type=int) if renter_type=='internal' else None
        stage_id = request.form.get('stage_id', type=int) if renter_type=='internal' else None
        customer_name = (request.form.get('customer_name') or '').strip() if renter_type=='external' else None
        customer_phone = (request.form.get('customer_phone') or '').strip() if renter_type=='external' else None
        customer_address = (request.form.get('customer_address') or '').strip() if renter_type=='external' else None

        if renter_type=='internal' and not project_id:
            flash('Select a project/site for internal rental.', 'danger')
            return redirect(url_for('hdc_tool_rental'))
        if renter_type=='external' and not customer_name:
            flash('Customer name required for external rental.', 'danger')
            return redirect(url_for('hdc_tool_rental'))
        if renter_type == 'internal' and stage_id:
            stage = db.session.get(Stage, stage_id)
            if not stage or stage.project_id != project_id:
                flash('Selected stage does not belong to the selected project/site.', 'danger')
                return redirect(url_for('hdc_tool_rental'))

        billing_type = (request.form.get('billing_type') or 'fixed_fee').strip().lower()
        if billing_type not in ('no_charge','fixed_fee','per_day','per_hour'):
            billing_type = 'fixed_fee'

        rental_date_raw = (request.form.get('rental_date') or '').strip()
        try:
            rental_date = datetime.strptime(rental_date_raw, '%Y-%m-%d').date() if rental_date_raw else _pkt_today()
        except:
            rental_date = _pkt_today()
        expected_raw = (request.form.get('expected_return_date') or '').strip()
        try:
            expected_date = datetime.strptime(expected_raw, '%Y-%m-%d').date() if expected_raw else None
        except:
            expected_date = None

        tool_ids = request.form.getlist('tool_id[]') or request.form.getlist('tool_id')
        qtys = request.form.getlist('qty[]') or request.form.getlist('qty')
        rates = request.form.getlist('rate[]') or request.form.getlist('rate')
        notes_list = request.form.getlist('item_notes[]') or request.form.getlist('item_notes')

        if not tool_ids or len(tool_ids)!=len(qtys):
            flash('Add at least one tool item.', 'danger')
            return redirect(url_for('hdc_tool_rental'))

        parsed_items = []
        total_rented_qty = 0.0
        total_amount = 0.0
        for idx, (tid_raw, qty_raw) in enumerate(zip(tool_ids, qtys)):
            try:
                tid = int(tid_raw)
            except:
                flash(f'Invalid tool at row {idx+1}.', 'danger')
                return redirect(url_for('hdc_tool_rental'))
            tool = Tool.query.get(tid)
            if not tool or tool.is_void:
                flash(f'Tool not found at row {idx+1}.', 'danger')
                return redirect(url_for('hdc_tool_rental'))
            qty = max(0.0, _flt(qty_raw))
            if qty <= 0:
                flash(f'Quantity must be >0 at row {idx+1}.', 'danger')
                return redirect(url_for('hdc_tool_rental'))
            if qty > tool.available_qty + 0.001:
                flash(f'Not enough stock for {tool.name}: available {tool.available_qty}, requested {qty}.', 'danger')
                return redirect(url_for('hdc_tool_rental'))
            rate_raw = rates[idx] if idx < len(rates) else tool.rental_rate_per_day
            rate = max(0.0, _flt(rate_raw, tool.rental_rate_per_day))
            if billing_type == 'no_charge':
                rate = 0.0
            amount = qty * rate
            note = (notes_list[idx] if idx < len(notes_list) else '').strip()
            parsed_items.append((tool, qty, rate, amount, note))
            total_rented_qty += qty
            total_amount += amount

        rental_code = _next_rental_code()
        rental = ToolRental(
            rental_code=rental_code,
            renter_type=renter_type,
            project_id=project_id,
            stage_id=stage_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_address=customer_address,
            rental_date=rental_date,
            expected_return_date=expected_date,
            billing_type=billing_type,
            billing_notes=(request.form.get('billing_notes') or '').strip(),
            total_rented_qty=total_rented_qty,
            total_amount=total_amount if billing_type!='no_charge' else 0.0,
            total_paid=0.0,
            total_returned_qty=0.0,
            status='active',
            payment_status='no_charge' if billing_type=='no_charge' else 'unpaid',
            notes=(request.form.get('notes') or '').strip(),
            created_by=current_user.id if hasattr(current_user,'id') else None
        )
        db.session.add(rental)
        db.session.flush()

        for tool, qty, rate, amount, note in parsed_items:
            item = ToolRentalItem(
                rental_id=rental.id,
                tool_id=tool.id,
                qty_rented=qty,
                qty_returned=0.0,
                qty_pending=qty,
                rate=rate,
                amount=amount if billing_type!='no_charge' else 0.0,
                notes=note
            )
            db.session.add(item)
            db.session.flush()
            from_label = 'Warehouse / Store'
            if renter_type == 'internal' and project_id:
                proj = db.session.get(Project, project_id)
                to_label = proj.name if proj else 'Internal Site'
                if stage_id:
                    st = db.session.get(Stage, stage_id)
                    if st:
                        to_label += f" > {st.name}"
            else:
                to_label = customer_name or 'External Customer'
            create_movement_log(
                tool_id=tool.id,
                rental_id=rental.id,
                movement_type='rental_out',
                from_label=from_label,
                to_label=to_label,
                qty=qty,
                notes=f'Rental {rental_code} out'
            )
        db.session.commit()
        flash(f'Rental {rental_code} created: {total_rented_qty} tools.', 'success')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

    # ------------------ RENTAL DETAIL ------------------
    @app.route('/hdc/tool-rental/<int:rental_id>')
    @login_required
    def hdc_tool_rental_detail(rental_id):
        rental = ToolRental.query.get_or_404(rental_id)
        recalc_rental_totals(rental.id)
        db.session.commit()

        items = ToolRentalItem.query.filter_by(rental_id=rental.id).all()
        returns = (ToolRentalReturn.query
                   .filter_by(rental_id=rental.id)
                   .order_by(ToolRentalReturn.return_date.desc(), ToolRentalReturn.id.desc())
                   .all())
        payments = (ToolRentalPayment.query
                    .filter_by(rental_id=rental.id)
                    .order_by(ToolRentalPayment.payment_date.desc(), ToolRentalPayment.id.desc())
                    .all())
        transfers = (ToolRentalTransfer.query
                     .filter_by(rental_id=rental.id)
                     .order_by(ToolRentalTransfer.transfer_date.desc(), ToolRentalTransfer.id.desc())
                     .all())
        tracking_chain = get_rental_tracking_chain(rental.id)
        movement_logs = (ToolMovementLog.query
                         .filter_by(rental_id=rental.id)
                         .order_by(ToolMovementLog.timestamp.asc(), ToolMovementLog.id.asc())
                         .all())
        projects = Project.query.order_by(Project.name.asc()).all()
        stages = Stage.query.order_by(Stage.name.asc()).all()
        tools = Tool.query.filter(Tool.is_void==False).order_by(Tool.name.asc()).all()
        receiving_accounts = get_receiving_accounts()

        pending_tools = float(rental.total_rented_qty or 0) - float(rental.total_returned_qty or 0)
        pending_amount = float(rental.total_amount or 0) - float(rental.total_paid or 0) if rental.billing_type!='no_charge' else 0.0

        # account txns for this rental payments
        from hdc.models.accounts import AccountTransaction
        from hdc.models.tool_rental import ToolRentalAccountTxn
        payment_ids = [p.id for p in payments]
        acct_links = {}
        if payment_ids:
            links = (ToolRentalAccountTxn.query
                     .filter(ToolRentalAccountTxn.payment_id.in_(payment_ids))
                     .all())
            for link in links:
                acct_links.setdefault(link.payment_id, []).append(link.account_txn_id)

        return render_template('tool_rental/tool_rental_detail.html',
            rental=rental,
            items=items,
            returns=returns,
            payments=payments,
            transfers=transfers,
            tracking_chain=tracking_chain,
            movement_logs=movement_logs,
            projects=projects,
            stages=stages,
            tools=tools,
            receiving_accounts=receiving_accounts,
            pending_tools=pending_tools,
            pending_amount=pending_amount,
            acct_links=acct_links,
            today=_pkt_today().isoformat()
        )

    # ------------------ RETURN (FULL/PARTIAL) WITH ACCOUNTS ------------------
    @app.route('/hdc/tool-rental/<int:rental_id>/return', methods=['POST'])
    @login_required
    def hdc_tool_rental_return(rental_id):
        rental = ToolRental.query.get_or_404(rental_id)
        if rental.is_void:
            flash('Rental is voided.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        return_date_raw = (request.form.get('return_date') or '').strip()
        try:
            return_date = datetime.strptime(return_date_raw, '%Y-%m-%d').date() if return_date_raw else _pkt_today()
        except:
            return_date = _pkt_today()

        return_type = (request.form.get('return_condition') or request.form.get('return_type') or 'partial').strip().lower()
        if return_type not in ('full','partial'):
            return_type = 'partial'

        payment_type = (request.form.get('payment_condition') or request.form.get('payment_type') or 'partial').strip().lower()
        if payment_type not in ('full','partial','credit','no_payment'):
            payment_type = 'partial'

        rental_item_ids = request.form.getlist('rental_item_id[]') or request.form.getlist('rental_item_id')
        qty_returned_list = request.form.getlist('qty_returned[]') or request.form.getlist('qty_returned')
        condition_notes_list = request.form.getlist('condition_notes[]') or request.form.getlist('condition_notes')

        if return_type == 'full':
            rental_items = ToolRentalItem.query.filter_by(rental_id=rental.id).all()
            rental_item_ids = [str(i.id) for i in rental_items if float(i.qty_pending or 0) > 0.001]
            qty_returned_list = [str(float(ToolRentalItem.query.get(int(ri)).qty_pending or 0)) for ri in rental_item_ids]
        else:
            if not rental_item_ids or len(rental_item_ids)!=len(qty_returned_list):
                flash('For partial return, specify qty for each tool.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        total_tools_returned = 0.0
        parsed_returns = []
        for idx, (ri_id_raw, qty_raw) in enumerate(zip(rental_item_ids, qty_returned_list)):
            try:
                ri_id = int(ri_id_raw)
            except:
                continue
            r_item = ToolRentalItem.query.get(ri_id)
            if not r_item or int(r_item.rental_id)!=int(rental.id):
                flash(f'Invalid rental item {ri_id_raw}.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
            qty_ret = max(0.0, _flt(qty_raw))
            if qty_ret <= 0:
                continue
            if qty_ret > float(r_item.qty_pending or 0) + 0.001:
                flash(f'Return qty {qty_ret} exceeds pending {r_item.qty_pending} for {r_item.tool.name}.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
            cond_note = (condition_notes_list[idx] if idx < len(condition_notes_list) else '').strip()
            parsed_returns.append((r_item, qty_ret, cond_note))
            total_tools_returned += qty_ret

        if total_tools_returned <= 0:
            flash('No tools marked for return.', 'warning')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        amount_paid_raw = (request.form.get('amount_paid') or '0').strip()
        amount_paid = max(0.0, _flt(amount_paid_raw))

        if payment_type == 'full':
            pending_amt = float(rental.total_amount or 0) - float(rental.total_paid or 0)
            amount_paid = max(0.0, pending_amt) if rental.billing_type!='no_charge' else 0.0
        elif payment_type == 'no_payment' or rental.billing_type=='no_charge':
            amount_paid = 0.0
        elif payment_type == 'credit':
            amount_paid = max(0.0, _flt(request.form.get('amount_paid') or 0))

        # receiving account for this return's payment
        received_to_account_id = request.form.get('received_to_account_id', type=int)
        recv_acc = None
        if amount_paid > 0:
            if received_to_account_id:
                recv_acc = Account.query.get(int(received_to_account_id))
            if (not recv_acc) or recv_acc.is_void or str(recv_acc.status or 'active').lower() != 'active' or str(recv_acc.type or '').lower() not in ('company','cash','bank'):
                flash('Select a valid Cash/Bank receiving account for payment.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        # duplicate guard
        if _has_recent_duplicate(ToolRentalReturn, rental_id=rental.id, total_tools_returned=total_tools_returned, amount_paid=amount_paid, return_date=return_date):
            flash('Duplicate return prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        ret_rec = ToolRentalReturn(
            rental_id=rental.id,
            return_date=return_date,
            return_type=return_type,
            payment_type=payment_type,
            total_tools_returned=total_tools_returned,
            amount_paid=amount_paid if rental.billing_type!='no_charge' else 0.0,
            notes=(request.form.get('notes') or '').strip(),
            created_by=current_user.id if hasattr(current_user,'id') else None
        )
        db.session.add(ret_rec)
        db.session.flush()

        for r_item, qty_ret, cond_note in parsed_returns:
            ret_item = ToolRentalReturnItem(
                return_id=ret_rec.id,
                rental_item_id=r_item.id,
                tool_id=r_item.tool_id,
                qty_returned=qty_ret,
                condition_notes=cond_note
            )
            db.session.add(ret_item)
            r_item.qty_returned = float(r_item.qty_returned or 0) + qty_ret
            r_item.qty_pending = max(0.0, float(r_item.qty_rented or 0) - float(r_item.qty_returned or 0))
            from_label = rental.current_location_label or 'Site'
            to_label = 'Warehouse / Store'
            create_movement_log(
                tool_id=r_item.tool_id,
                rental_id=rental.id,
                movement_type='return_in',
                from_label=from_label,
                to_label=to_label,
                qty=qty_ret,
                return_id=ret_rec.id,
                notes=f'Return {ret_rec.id}: {qty_ret} pcs'
            )

        pay_record = None
        if amount_paid > 0 and rental.billing_type!='no_charge':
            pay_record = ToolRentalPayment(
                rental_id=rental.id,
                return_id=ret_rec.id,
                payment_date=return_date,
                amount=amount_paid,
                payment_mode=(request.form.get('payment_mode') or 'cash').strip().lower(),
                received_to_account_id=int(recv_acc.id) if recv_acc else None,
                reference=(request.form.get('payment_reference') or '').strip(),
                notes=f'Payment on return {ret_rec.id}',
                created_by=current_user.id if hasattr(current_user,'id') else None
            )
            db.session.add(pay_record)
            db.session.flush()
            # post to accounts
            ok_acc, msg_acc, _ = post_tool_rental_payment_to_accounts(pay_record, rental=rental, commit=False)
            if not ok_acc:
                db.session.rollback()
                flash(msg_acc or 'Unable to post rental payment in accounts.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
        elif payment_type=='credit' and rental.billing_type!='no_charge':
            rental.payment_status = 'credit'

        recalc_rental_totals(rental.id)
        if payment_type=='credit' and float(rental.total_pending_amount or 0) > 0:
            rental.payment_status = 'credit'

        db.session.commit()
        recv_name = recv_acc.name if recv_acc else '-'
        flash(f'Return recorded: {total_tools_returned} tools, {amount_paid:.2f} PKR paid to {recv_name}. Pending tools: {rental.total_pending_tools:.0f}, Pending amount: {rental.total_pending_amount:.2f}', 'success')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

    # ------------------ ADD PAYMENT WITH ACCOUNTS ------------------
    @app.route('/hdc/tool-rental/<int:rental_id>/payment', methods=['POST'])
    @login_required
    def hdc_tool_rental_payment(rental_id):
        rental = ToolRental.query.get_or_404(rental_id)
        if rental.billing_type=='no_charge':
            flash('This rental is No Charge (contract included) - no payment needed.', 'info')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
        amount = max(0.0, _flt(request.form.get('amount')))
        if amount <=0:
            flash('Payment amount must be >0.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        received_to_account_id = request.form.get('received_to_account_id', type=int)
        recv_acc = None
        if received_to_account_id:
            recv_acc = Account.query.get(int(received_to_account_id))
        if (not recv_acc) or recv_acc.is_void or str(recv_acc.status or 'active').lower() != 'active' or str(recv_acc.type or '').lower() not in ('company','cash','bank'):
            flash('Select a valid Cash/Bank receiving account (Company/Cash/Bank).', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        pay_date_raw = (request.form.get('payment_date') or '').strip()
        try:
            pay_date = datetime.strptime(pay_date_raw, '%Y-%m-%d').date() if pay_date_raw else _pkt_today()
        except:
            pay_date = _pkt_today()

        if _has_recent_duplicate(ToolRentalPayment, rental_id=rental.id, amount=amount, payment_date=pay_date, received_to_account_id=recv_acc.id):
            flash('Duplicate payment prevented.', 'warning')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        payment_mode = (request.form.get('payment_mode') or 'cash').strip().lower()
        reference = (request.form.get('reference') or '').strip()
        notes = (request.form.get('notes') or '').strip()

        pay = ToolRentalPayment(
            rental_id=rental.id,
            payment_date=pay_date,
            amount=amount,
            payment_mode=payment_mode,
            received_to_account_id=int(recv_acc.id),
            reference=reference,
            notes=notes,
            created_by=current_user.id if hasattr(current_user,'id') else None
        )
        db.session.add(pay)
        db.session.flush()

        ok_acc, msg_acc, _ = post_tool_rental_payment_to_accounts(pay, rental=rental, commit=False)
        if not ok_acc:
            db.session.rollback()
            flash(msg_acc or 'Unable to post payment in accounts.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        recalc_rental_totals(rental.id)
        db.session.commit()
        flash(f'Payment {amount:.2f} PKR received in {recv_acc.name} (Cash/Bank). Pending: {rental.total_pending_amount:.2f}', 'success')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

    @app.route('/hdc/tool-rental/payment/<int:payment_id>/void', methods=['POST'])
    @login_required
    def hdc_tool_rental_payment_void(payment_id):
        pay = ToolRentalPayment.query.get_or_404(payment_id)
        if pay.is_void:
            flash('Payment already voided.', 'info')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=pay.rental_id))
        pay.is_void = True
        pay.void_reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
        pay.voided_at = _pkt_now_naive()
        void_tool_rental_payment_in_accounts(pay.id)
        recalc_rental_totals(pay.rental_id)
        db.session.commit()
        flash('Payment voided and removed from accounts.', 'warning')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=pay.rental_id))

    @app.route('/hdc/tool-rental/payment/<int:payment_id>/receipt')
    @login_required
    def hdc_tool_rental_payment_receipt(payment_id):
        pay = ToolRentalPayment.query.get_or_404(payment_id)
        rental = pay.rental
        receipt_id = f"RCPT-TR-{pay.id:08d}"
        party_name = rental.customer_name if rental.renter_type=='external' else (rental.project.name if rental.project else 'Internal Site')
        return render_template('accounts/transaction_receipt.html',
            company_profile=_receipt_company_profile(),
            receipt_id=receipt_id,
            created_at=(pay.created_at or _pkt_now_naive()),
            tx_type='Tool Rental Payment Receipt',
            party_name=party_name,
            project_name=(rental.project.name if rental.project else '-'),
            stage_name=(rental.stage.name if rental.stage else '-'),
            account_used=(pay.received_to_account.name if pay.received_to_account else 'Company Cash'),
            amount=float(pay.amount or 0.0),
            amount_words=_amount_to_words(pay.amount or 0.0),
            note=(pay.notes or f'Rental {rental.rental_code}'),
            reference_id=f'tool_rental_payment#{pay.id}',
            recent_entries=[],
            recent_entries_title='',
            back_url=url_for('hdc_tool_rental_detail', rental_id=rental.id),
            print_label='Print / Save PDF'
        )

    # ------------------ TRANSFER SITE TO SITE ------------------
    @app.route('/hdc/tool-rental/<int:rental_id>/transfer', methods=['POST'])
    @login_required
    def hdc_tool_rental_transfer(rental_id):
        rental = ToolRental.query.get_or_404(rental_id)
        if rental.status in ('returned','closed'):
            flash('Rental already closed, cannot transfer.', 'warning')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        from_label = rental.current_location_label or 'Unknown'
        to_type = (request.form.get('to_type') or 'site').strip().lower()
        if to_type not in ('site','customer','warehouse'):
            to_type = 'site'

        to_project_id = request.form.get('to_project_id', type=int) if to_type=='site' else None
        to_stage_id = request.form.get('to_stage_id', type=int) if to_type=='site' else None
        to_customer_name = (request.form.get('to_customer_name') or '').strip() if to_type=='customer' else None

        if to_type=='site' and not to_project_id:
            flash('Select destination project/site.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
        if to_type=='customer' and not to_customer_name:
            flash('Enter destination customer name.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
        if to_type == 'site' and to_stage_id:
            stage = db.session.get(Stage, to_stage_id)
            if not stage or stage.project_id != to_project_id:
                flash('Selected stage does not belong to the destination project/site.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        if to_type=='site' and to_project_id:
            proj = db.session.get(Project, to_project_id)
            to_label = proj.name if proj else f'Project #{to_project_id}'
            if to_stage_id:
                st = db.session.get(Stage, to_stage_id)
                if st:
                    to_label += f" > {st.name}"
        elif to_type=='customer':
            to_label = to_customer_name
        else:
            to_label = 'Warehouse / Store'

        qty_transferred = max(0.0, _flt(request.form.get('qty_transferred'), rental.total_pending_tools))
        if qty_transferred <=0:
            qty_transferred = float(rental.total_pending_tools or 0)

        transfer_date_raw = (request.form.get('transfer_date') or '').strip()
        try:
            transfer_date = datetime.strptime(transfer_date_raw, '%Y-%m-%d').date() if transfer_date_raw else _pkt_today()
        except:
            transfer_date = _pkt_today()

        from_project_id = None
        from_stage_id = None
        last_transfer = (ToolRentalTransfer.query
                         .filter_by(rental_id=rental.id)
                         .order_by(ToolRentalTransfer.transfer_date.desc(), ToolRentalTransfer.id.desc())
                         .first())
        if last_transfer:
            from_project_id = last_transfer.to_project_id
            from_stage_id = last_transfer.to_stage_id
        else:
            from_project_id = rental.project_id
            from_stage_id = rental.stage_id

        transfer = ToolRentalTransfer(
            rental_id=rental.id,
            from_type='site' if from_project_id else 'customer',
            from_project_id=from_project_id,
            from_stage_id=from_stage_id,
            from_customer_name=rental.customer_name if not from_project_id else None,
            from_location_label=from_label,
            to_type=to_type,
            to_project_id=to_project_id,
            to_stage_id=to_stage_id,
            to_customer_name=to_customer_name,
            to_location_label=to_label,
            qty_transferred=qty_transferred,
            transfer_date=transfer_date,
            notes=(request.form.get('notes') or '').strip(),
            created_by=current_user.id if hasattr(current_user,'id') else None
        )
        db.session.add(transfer)
        db.session.flush()

        rental_items = ToolRentalItem.query.filter_by(rental_id=rental.id).filter(ToolRentalItem.qty_pending>0).all()
        for ri in rental_items:
            create_movement_log(
                tool_id=ri.tool_id,
                rental_id=rental.id,
                movement_type='site_transfer' if to_type=='site' else 'external_transfer',
                from_label=from_label,
                to_label=to_label,
                qty=float(ri.qty_pending or 0),
                transfer_id=transfer.id,
                notes=f'Transfer {from_label} > {to_label}'
            )
        db.session.commit()
        chain_str = " > ".join(rental.tracking_chain)
        flash(f'Tools transferred: {from_label} to {to_label} ({qty_transferred} qty). Chain: {chain_str}', 'success')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

    # ------------------ GLOBAL TRACKING ------------------
    @app.route('/hdc/tool-rental/tracking')
    @login_required
    def hdc_tool_rental_tracking():
        tool_id = request.args.get('tool_id', type=int)
        project_id = request.args.get('project_id', type=int)
        q = (request.args.get('q') or '').strip() or None
        locations = global_tool_locations(search_tool_id=tool_id, search_project_id=project_id, search_text=q)
        projects = Project.query.order_by(Project.name.asc()).all()
        tools = Tool.query.filter(Tool.is_void==False).order_by(Tool.name.asc()).all()
        kpis = tool_kpis()
        return render_template('tool_rental/tool_tracking.html',
            locations=locations,
            projects=projects,
            tools=tools,
            selected_tool=tool_id,
            selected_project=project_id,
            q=q,
            kpis=kpis
        )

    # ------------------ REPORTING ------------------
    @app.route('/hdc/tool-rental/reports')
    @login_required
    def hdc_tool_rental_reports():
        filters = {
            'project_id': request.args.get('project_id', type=int),
            'tool_id': request.args.get('tool_id', type=int),
            'renter_type': (request.args.get('renter_type') or '').strip() or None,
            'status': (request.args.get('status') or '').strip() or None,
            'payment_status': (request.args.get('payment_status') or '').strip() or None,
            'billing_type': (request.args.get('billing_type') or '').strip() or None,
            'date_from': (request.args.get('date_from') or '').strip() or None,
            'date_to': (request.args.get('date_to') or '').strip() or None,
            'search_text': (request.args.get('q') or '').strip() or None,
        }
        rentals = search_rentals(filters)
        total_rented = sum(float(r.total_rented_qty or 0) for r in rentals)
        total_returned = sum(float(r.total_returned_qty or 0) for r in rentals)
        total_pending_tools = sum(float(r.total_pending_tools or 0) for r in rentals)
        total_amount = sum(float(r.total_amount or 0) for r in rentals if r.billing_type!='no_charge')
        total_paid = sum(float(r.total_paid or 0) for r in rentals)
        total_pending_amount = sum(float(r.total_pending_amount or 0) for r in rentals)

        tool_breakdown = {}
        for r in rentals:
            for item in r.items:
                tid = item.tool_id
                if tid not in tool_breakdown:
                    tool_breakdown[tid] = {'tool': item.tool, 'rented':0, 'returned':0, 'pending':0, 'amount':0}
                tool_breakdown[tid]['rented'] += float(item.qty_rented or 0)
                tool_breakdown[tid]['returned'] += float(item.qty_returned or 0)
                tool_breakdown[tid]['pending'] += float(item.qty_pending or 0)
                tool_breakdown[tid]['amount'] += float(item.amount or 0)

        site_breakdown = {}
        for r in rentals:
            key = r.project_id or 0
            label = r.project.name if r.project else (r.customer_name or 'External')
            if key not in site_breakdown:
                site_breakdown[key] = {'label': label, 'rented':0, 'pending_tools':0, 'amount':0, 'paid':0, 'pending_amount':0, 'count':0}
            site_breakdown[key]['rented'] += float(r.total_rented_qty or 0)
            site_breakdown[key]['pending_tools'] += float(r.total_pending_tools or 0)
            site_breakdown[key]['amount'] += float(r.total_amount or 0) if r.billing_type!='no_charge' else 0
            site_breakdown[key]['paid'] += float(r.total_paid or 0)
            site_breakdown[key]['pending_amount'] += float(r.total_pending_amount or 0)
            site_breakdown[key]['count'] += 1

        projects = Project.query.order_by(Project.name.asc()).all()
        tools = Tool.query.filter(Tool.is_void==False).order_by(Tool.name.asc()).all()
        kpis = tool_kpis()
        receiving_accounts = get_receiving_accounts()

        return render_template('tool_rental/tool_reports.html',
            rentals=rentals,
            filters=filters,
            total_rented=total_rented,
            total_returned=total_returned,
            total_pending_tools=total_pending_tools,
            total_amount=total_amount,
            total_paid=total_paid,
            total_pending_amount=total_pending_amount,
            tool_breakdown=tool_breakdown,
            site_breakdown=site_breakdown,
            projects=projects,
            tools=tools,
            receiving_accounts=receiving_accounts,
            kpis=kpis
        )

    # ------------------ API ------------------
    @app.route('/hdc/api/tool-rental/tools-available')
    @login_required
    def hdc_api_tools_available():
        tools = Tool.query.filter(Tool.is_void==False, Tool.status=='active').order_by(Tool.name.asc()).all()
        data = []
        for t in tools:
            data.append({
                'id': t.id,
                'code': t.tool_code,
                'name': t.name,
                'unit': t.unit,
                'total_qty': float(t.total_quantity or 0),
                'available_qty': float(t.available_qty),
                'rented_out': float(t.rented_out_qty),
                'rate': float(t.rental_rate_per_day or 0)
            })
        return jsonify(data)

    @app.route('/hdc/api/tool-rental/rental/<int:rental_id>/pending-items')
    @login_required
    def hdc_api_rental_pending_items(rental_id):
        rental = ToolRental.query.get_or_404(rental_id)
        items = []
        for it in rental.items:
            if float(it.qty_pending or 0) > 0.001:
                items.append({
                    'rental_item_id': it.id,
                    'tool_id': it.tool_id,
                    'tool_name': it.tool.name if it.tool else '-',
                    'tool_code': it.tool.tool_code if it.tool else '-',
                    'qty_rented': float(it.qty_rented or 0),
                    'qty_returned': float(it.qty_returned or 0),
                    'qty_pending': float(it.qty_pending or 0),
                    'rate': float(it.rate or 0)
                })
        return jsonify(items)

    @app.route('/hdc/api/tool-rental/receiving-accounts')
    @login_required
    def hdc_api_receiving_accounts():
        accs = get_receiving_accounts()
        return jsonify([{'id': a.id, 'name': a.name, 'type': a.type, 'balance': float(a.opening_balance or 0)} for a in accs])
