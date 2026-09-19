"""Business logic for HDC Tool Rental."""

from datetime import date, datetime
from sqlalchemy import func, or_

from hdc.extensions import db
from hdc.models.accounts import Account
from hdc.models.tool_rental import (
    Tool, ToolCategory, ToolMovementLog, ToolRental, ToolRentalAccountTxn, ToolRentalItem,
    ToolRentalPayment, ToolRentalReturn, ToolRentalReturnItem, ToolRentalTransfer
)
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.normalize import _normalize_name_ci

_ACCOUNT_COMPANY_TYPES = ('company', 'cash', 'bank')


def _next_tool_code():
    last = db.session.query(Tool).order_by(Tool.id.desc()).first()
    nxt = (last.id + 1) if last else 1
    return f"TOOL-{nxt:04d}"

def _next_rental_code():
    last = db.session.query(ToolRental).order_by(ToolRental.id.desc()).first()
    nxt = (last.id + 1) if last else 1
    return f"RENT-{nxt:05d}"

def _ensure_tool_category(name):
    name = (name or '').strip()
    if not name:
        return None
    existing = ToolCategory.query.filter(func.lower(ToolCategory.name) == name.lower()).first()
    if existing:
        return existing
    cat = ToolCategory(name=name, active_status=True)
    db.session.add(cat)
    db.session.flush()
    return cat

def recalc_rental_totals(rental_id):
    rental = db.session.get(ToolRental, rental_id)
    if not rental:
        return
    items = ToolRentalItem.query.filter_by(rental_id=rental.id).all()
    total_rented = sum(float(i.qty_rented or 0) for i in items)
    total_returned = sum(float(i.qty_returned or 0) for i in items)
    total_pending = max(0.0, total_rented - total_returned)
    total_amount = sum(float(i.amount or 0) for i in items)

    total_paid = db.session.query(func.coalesce(func.sum(ToolRentalPayment.amount), 0.0)).filter(
        ToolRentalPayment.rental_id == rental.id,
        ToolRentalPayment.is_void == False
    ).scalar() or 0.0

    rental.total_rented_qty = total_rented
    rental.total_returned_qty = total_returned
    rental.total_amount = total_amount if total_amount>0 else float(rental.total_amount or 0)
    rental.total_paid = float(total_paid)

    if rental.is_void:
        rental.status = 'closed'
    elif total_pending <= 0.001:
        rental.status = 'returned'
    elif total_returned > 0:
        rental.status = 'partially_returned'
    else:
        if rental.expected_return_date and rental.expected_return_date < _pkt_today():
            rental.status = 'overdue'
        else:
            rental.status = 'active'

    if (rental.billing_type or '').lower() == 'no_charge':
        rental.payment_status = 'no_charge'
    else:
        pending_amt = max(0.0, float(rental.total_amount or 0) - float(total_paid))
        if pending_amt <= 0.001 and float(rental.total_amount or 0) > 0:
            rental.payment_status = 'paid'
        elif float(total_paid) > 0 and pending_amt > 0:
            rental.payment_status = 'partial'
        elif (rental.payment_status or '') == 'credit':
            if pending_amt <= 0.001:
                rental.payment_status = 'paid'
            else:
                rental.payment_status = 'credit'
        else:
            rental.payment_status = 'unpaid' if float(rental.total_amount or 0) > 0 else 'unpaid'

    if total_pending <= 0.001 and rental.payment_status in ('paid','no_charge'):
        rental.status = 'closed'

    db.session.flush()

def create_movement_log(tool_id, rental_id, movement_type, from_label, to_label, qty, transfer_id=None, return_id=None, notes=""):
    log = ToolMovementLog(
        tool_id=tool_id,
        rental_id=rental_id,
        transfer_id=transfer_id,
        return_id=return_id,
        movement_type=movement_type,
        from_location_label=from_label,
        to_location_label=to_label,
        qty=float(qty or 0),
        timestamp=_pkt_now_naive(),
        notes=notes
    )
    db.session.add(log)
    db.session.flush()
    return log

def get_tool_tracking_chain(tool_id):
    logs = (ToolMovementLog.query
            .filter_by(tool_id=tool_id)
            .order_by(ToolMovementLog.timestamp.asc(), ToolMovementLog.id.asc())
            .all())
    return logs

def get_rental_tracking_chain(rental_id):
    rental = db.session.get(ToolRental, rental_id)
    if not rental:
        return []
    chain = []
    if rental.renter_type == 'internal' and rental.project:
        initial = rental.project.name
        if rental.stage:
            initial += f" / {rental.stage.name}"
    else:
        initial = rental.customer_name or "External"
    chain.append({'label': initial, 'type': 'rental_start', 'date': rental.rental_date, 'is_current': False})

    transfers = (ToolRentalTransfer.query
                 .filter_by(rental_id=rental.id)
                 .order_by(ToolRentalTransfer.transfer_date.asc(), ToolRentalTransfer.id.asc())
                 .all())
    for t in transfers:
        chain.append({
            'label': t.to_location_label,
            'type': 'transfer',
            'date': t.transfer_date,
            'is_current': False,
            'transfer': t
        })
    if chain:
        chain[-1]['is_current'] = True
    return chain

def tool_kpis():
    total_tools = db.session.query(func.coalesce(func.sum(Tool.total_quantity), 0.0)).filter(Tool.is_void==False).scalar() or 0.0
    total_tool_types = db.session.query(func.count(Tool.id)).filter(Tool.is_void==False).scalar() or 0
    rented_out = db.session.query(func.coalesce(func.sum(ToolRentalItem.qty_pending), 0.0)).scalar() or 0.0
    available = max(0.0, float(total_tools) - float(rented_out))
    active_rentals = db.session.query(func.count(ToolRental.id)).filter(ToolRental.status.in_(['active','partially_returned','overdue']), ToolRental.is_void==False).scalar() or 0
    overdue_rentals = db.session.query(func.count(ToolRental.id)).filter(ToolRental.status=='overdue', ToolRental.is_void==False).scalar() or 0
    total_amount = db.session.query(func.coalesce(func.sum(ToolRental.total_amount), 0.0)).filter(ToolRental.is_void==False, ToolRental.billing_type!='no_charge').scalar() or 0.0
    total_paid = db.session.query(func.coalesce(func.sum(ToolRental.total_paid), 0.0)).filter(ToolRental.is_void==False).scalar() or 0.0
    pending_amount = max(0.0, float(total_amount) - float(total_paid))
    pending_tools = db.session.query(func.coalesce(func.sum(ToolRentalItem.qty_pending), 0.0)).scalar() or 0.0
    internal_rentals = db.session.query(func.count(ToolRental.id)).filter(ToolRental.renter_type=='internal', ToolRental.is_void==False).scalar() or 0
    external_rentals = db.session.query(func.count(ToolRental.id)).filter(ToolRental.renter_type=='external', ToolRental.is_void==False).scalar() or 0
    credit_rentals = db.session.query(func.count(ToolRental.id)).filter(ToolRental.payment_status=='credit', ToolRental.is_void==False).scalar() or 0
    credit_amount = db.session.query(func.coalesce(func.sum(ToolRental.total_amount - ToolRental.total_paid), 0.0)).filter(ToolRental.payment_status=='credit', ToolRental.is_void==False).scalar() or 0.0
    return {
        'total_tools': float(total_tools),
        'total_tool_types': int(total_tool_types),
        'rented_out': float(rented_out),
        'available': float(available),
        'active_rentals': int(active_rentals),
        'overdue_rentals': int(overdue_rentals),
        'total_amount': float(total_amount),
        'total_paid': float(total_paid),
        'pending_amount': float(pending_amount),
        'pending_tools': float(pending_tools),
        'internal_rentals': int(internal_rentals),
        'external_rentals': int(external_rentals),
        'credit_rentals': int(credit_rentals),
        'credit_amount': float(credit_amount),
    }

def search_rentals(filters):
    q = ToolRental.query.filter(ToolRental.is_void==False)
    if filters.get('project_id'):
        q = q.filter(ToolRental.project_id == filters['project_id'])
    if filters.get('renter_type'):
        q = q.filter(ToolRental.renter_type == filters['renter_type'])
    if filters.get('status'):
        q = q.filter(ToolRental.status == filters['status'])
    if filters.get('payment_status'):
        q = q.filter(ToolRental.payment_status == filters['payment_status'])
    if filters.get('billing_type'):
        q = q.filter(ToolRental.billing_type == filters['billing_type'])
    if filters.get('date_from'):
        try:
            df = datetime.strptime(filters['date_from'], '%Y-%m-%d').date()
            q = q.filter(ToolRental.rental_date >= df)
        except:
            pass
    if filters.get('date_to'):
        try:
            dt = datetime.strptime(filters['date_to'], '%Y-%m-%d').date()
            q = q.filter(ToolRental.rental_date <= dt)
        except:
            pass
    if filters.get('search_text'):
        txt = f"%{filters['search_text'].lower()}%"
        q = q.filter(or_(
            func.lower(ToolRental.rental_code).like(txt),
            func.lower(func.coalesce(ToolRental.customer_name,'')).like(txt),
            func.lower(func.coalesce(ToolRental.customer_phone,'')).like(txt),
        ))
    if filters.get('tool_id'):
        q = q.join(ToolRentalItem, ToolRentalItem.rental_id == ToolRental.id).filter(ToolRentalItem.tool_id == filters['tool_id'])
    q = q.order_by(ToolRental.rental_date.desc(), ToolRental.id.desc())
    return q.all()

def global_tool_locations(search_tool_id=None, search_project_id=None, search_text=None):
    tq = Tool.query.filter(Tool.is_void==False)
    if search_tool_id:
        tq = tq.filter(Tool.id == search_tool_id)
    if search_text:
        txt = f"%{search_text.lower()}%"
        tq = tq.filter(or_(
            func.lower(Tool.name).like(txt),
            func.lower(Tool.tool_code).like(txt),
        ))
    tools = tq.order_by(Tool.name.asc()).all()
    result = []
    for tool in tools:
        last_log = (ToolMovementLog.query
                    .filter_by(tool_id=tool.id)
                    .order_by(ToolMovementLog.timestamp.desc(), ToolMovementLog.id.desc())
                    .first())
        chain_logs = (ToolMovementLog.query
                      .filter_by(tool_id=tool.id)
                      .order_by(ToolMovementLog.timestamp.asc(), ToolMovementLog.id.asc())
                      .all())
        if search_project_id:
            has_match = False
            if last_log and last_log.rental_id:
                rental = db.session.get(ToolRental, last_log.rental_id)
                if rental and int(rental.project_id or 0) == int(search_project_id):
                    has_match = True
            if not has_match:
                exists = (db.session.query(ToolRentalItem.id)
                          .join(ToolRental, ToolRental.id == ToolRentalItem.rental_id)
                          .filter(ToolRentalItem.tool_id == tool.id,
                                  ToolRental.project_id == search_project_id,
                                  ToolRentalItem.qty_pending > 0)
                          .first())
                if exists:
                    has_match = True
            if not has_match:
                continue
        current_label = last_log.to_location_label if last_log else "Warehouse / Store"
        result.append({
            'tool': tool,
            'current_label': current_label,
            'last_log': last_log,
            'chain': chain_logs,
            'rented_out_qty': tool.rented_out_qty,
            'available_qty': tool.available_qty,
        })
    return result

def get_receiving_accounts():
    return (Account.query
            .filter(
                Account.is_void == False,
                func.lower(func.coalesce(Account.status, 'active')) == 'active',
                func.lower(func.coalesce(Account.type, '')).in_(_ACCOUNT_COMPANY_TYPES)
            )
            .order_by(Account.name.asc(), Account.id.asc())
            .all())

def _get_or_create_customer_account(rental):
    from hdc.services.accounts import _account_get_or_create
    if rental.renter_type == 'internal' and rental.project:
        name = _normalize_name_ci(rental.project.client or rental.project.name or f'Project#{rental.project_id}')
        acc_type = 'client'
        auto_source = 'client'
    else:
        name = _normalize_name_ci(rental.customer_name or f'Customer Rental#{rental.id}')
        acc_type = 'client'
        auto_source = 'client'
    if not name:
        name = f'ToolRental#{rental.rental_code}'
    acc = _account_get_or_create(name, acc_type, auto_generated=True, auto_source=auto_source)
    return acc

def post_tool_rental_payment_to_accounts(payment, rental=None, commit=False):
    from hdc.services.accounts import _create_account_transaction, _accounts_default_company_cash
    if not payment or float(payment.amount or 0) <= 0:
        return False, 'Payment amount must be >0', []
    if payment.is_void:
        return False, 'Payment is voided', []
    rental = rental or db.session.get(ToolRental, int(payment.rental_id or 0))
    if not rental:
        return False, 'Rental not found', []

    recv_acc = None
    if payment.received_to_account_id:
        recv_acc = Account.query.get(int(payment.received_to_account_id))
    if not recv_acc:
        recv_acc = _accounts_default_company_cash()
    if not recv_acc or recv_acc.is_void or str(recv_acc.status or 'active').lower() != 'active' or str(recv_acc.type or '').lower() not in _ACCOUNT_COMPANY_TYPES:
        return False, 'Select valid receiving account (Cash/Bank/Company)', []

    cust_acc = _get_or_create_customer_account(rental)
    if not cust_acc:
        return False, 'Unable to resolve customer account', []

    from hdc.models.accounts import AccountTransaction
    existing = (AccountTransaction.query
                .filter(
                    func.lower(func.coalesce(AccountTransaction.source_type,'')).like('tool_rental_payment%'),
                    AccountTransaction.source_id == int(payment.id),
                    AccountTransaction.is_void == False
                )
                .first())
    if existing:
        return True, 'Already posted', [existing]

    pay_date = payment.payment_date or _pkt_today()
    date_str = pay_date.isoformat() if hasattr(pay_date, 'isoformat') else str(pay_date)

    note_raw = payment.notes or ''
    mode_raw = payment.payment_mode or ''
    note_txt = (f'Tool Rental Receipt {rental.rental_code} - {note_raw} [{mode_raw}]').strip()[:400]

    party_name = rental.customer_name if rental.renter_type == 'external' else (rental.project.name if rental.project else 'Internal Site')

    payload = {
        'date': date_str,
        'amount': float(payment.amount or 0),
        'type': 'party_receipt',
        'from_account_id': cust_acc.id,
        'to_account_id': recv_acc.id,
        'executed_by_account_id': cust_acc.id,
        'project_id': rental.project_id,
        'stage_id': rental.stage_id,
        'related_entity_type': 'tool_rental',
        'related_entity_id': rental.id,
        'party_name': party_name,
        'category': 'income',
        'note': note_txt,
        'reference_id': f'tool_rental_payment#{payment.id}',
        'source_type': 'tool_rental_payment',
        'source_id': payment.id,
        'group_id': f'tool-rent-{rental.id}-pay-{payment.id}'
    }

    ok, msg, txns = _create_account_transaction(payload, commit=False)
    if not ok:
        return False, msg, []

    for txn in txns:
        link = ToolRentalAccountTxn(payment_id=payment.id, account_txn_id=txn.id)
        db.session.add(link)
    db.session.flush()
    if commit:
        db.session.commit()
    return True, '', txns

def void_tool_rental_payment_in_accounts(payment_id):
    from hdc.services.accounts import _accounts_set_void_by_source
    _accounts_set_void_by_source('tool_rental_payment', int(payment_id), True)
    return True
