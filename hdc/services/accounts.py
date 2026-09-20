"""HDC services.accounts — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from datetime import datetime
from uuid import uuid4

from flask import flash, url_for
from sqlalchemy import and_, case, func, or_, text

from hdc.core.flags import _runtime_flag_get, _runtime_flag_set
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction, Expense, OwnerPayment, PersonalExpense
from hdc.models.materials import PurchaseV2, Supplier, SupplierLedger
from hdc.models.office import OfficeExpense, OfficeStaff, OfficeStaffLedger
from hdc.models.projects import Project, Stage
from hdc.models.subcontract import SubcontractPayment, Subcontractor
from hdc.models.workforce import LabourLedger, Worker
from hdc.services.ledger import _office_staff_ledger_snapshot, _remove_office_salary_expense_for_ledger, _sync_office_staff_expense_from_ledger, _worker_payable_snapshot
from hdc.services.lookups import _ensure_expense_category, _ensure_expense_category_by_id
from hdc.services.purchase import _sync_purchase_v2_ledger
from hdc.services.subcontract import _log_subcontract_event, _subcontract_stage_snapshot
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _activity_at_for, _flt, _parse_date, _payload_int
from hdc.utils.money import sync_money_fields
from hdc.utils.normalize import _normalize_account_group, _normalize_account_mode, _normalize_account_tx_direction, _normalize_account_tx_type, _normalize_expense_category_name, _normalize_name_ci, _normalize_related_entity_type

def _accounts_reconciliation_findings():
    """Forensic scan of the accounts <-> source-row linkage.

    Returns a dict of categorized findings. Each finding lists concrete rows
    so the user can click through and fix the data. The same logic that the
    sync flow uses to write the linkage is mirrored here in reverse.
    """
    # Map: source_type prefix -> (Model, label, list_url_builder, void_attr)
    SOURCE_MAP = {
        'labour_ledger_advance':       (LabourLedger, 'Worker advance'),
        'labour_ledger_payment':       (LabourLedger, 'Worker payment'),
        'labour_ledger_tip':           (LabourLedger, 'Worker tip'),
        'worker_payment':              (LabourLedger, 'Worker payment'),
        'supplier_credit_payment':     (SupplierLedger, 'Supplier payment'),
        'supplier_credit_tip':         (SupplierLedger, 'Supplier tip'),
        'supplier_credit_settlement':  (SupplierLedger, 'Supplier settlement'),
        'subcontract_payment_payment': (SubcontractPayment, 'Subcontract payment'),
        'subcontract_payment_tip':     (SubcontractPayment, 'Subcontract tip'),
        'office_staff_ledger_advance': (OfficeStaffLedger, 'Office staff advance'),
        'office_staff_ledger_payment': (OfficeStaffLedger, 'Office staff payment'),
        'office_staff_ledger_tip':     (OfficeStaffLedger, 'Office staff tip'),
        'office_expense':              (OfficeExpense, 'Office expense'),
        'expense':                     (Expense, 'Expense'),
        'purchase_v2_paid':            (PurchaseV2, 'Purchase (paid)'),
        'owner_payment':               (OwnerPayment, 'Owner payment'),
    }

    def _base_source_type(s):
        s = (s or '').strip().lower()
        if not s:
            return ''
        return s.split(':', 1)[0]

    orphan_txns = []           # AccountTransaction rows pointing to a missing source row
    void_mismatch = []         # source vs txn void state disagrees
    duplicate_active = []      # exact-duplicate active txns
    group_inconsistent = []    # group_id splits with anomalies (mixed void / total mismatch)
    orphan_sources = []        # source rows that should have a paired txn but do not

    # ---- 1. Walk every AccountTransaction with a source linkage ----
    linked = (AccountTransaction.query
              .filter(AccountTransaction.source_id.isnot(None),
                      AccountTransaction.source_type.isnot(None))
              .all())
    for r in linked:
        base = _base_source_type(r.source_type)
        if base not in SOURCE_MAP:
            continue
        Model, label = SOURCE_MAP[base]
        src = Model.query.get(int(r.source_id or 0)) if r.source_id else None
        if not src:
            orphan_txns.append({
                'id': int(r.id),
                'date': (r.date.isoformat() if r.date else ''),
                'amount': float(r.amount or 0.0),
                'type': (r.type or ''),
                'party': (r.party_name or ''),
                'source_type': (r.source_type or ''),
                'source_id': int(r.source_id or 0),
                'label': label,
                'is_void': bool(getattr(r, 'is_void', False)),
                'receipt_url': url_for('hdc_account_transaction_receipt', txn_id=r.id),
            })
            continue
        src_void = bool(getattr(src, 'is_void', False))
        txn_void = bool(getattr(r, 'is_void', False))
        if src_void != txn_void:
            void_mismatch.append({
                'id': int(r.id),
                'date': (r.date.isoformat() if r.date else ''),
                'amount': float(r.amount or 0.0),
                'party': (r.party_name or ''),
                'source_type': (r.source_type or ''),
                'source_id': int(r.source_id or 0),
                'label': label,
                'txn_void': txn_void,
                'src_void': src_void,
                'receipt_url': url_for('hdc_account_transaction_receipt', txn_id=r.id),
            })

    # ---- 2. Duplicate active txns ----
    dup_q = (db.session.query(
                AccountTransaction.date,
                AccountTransaction.type,
                AccountTransaction.amount,
                AccountTransaction.from_account_id,
                AccountTransaction.to_account_id,
                AccountTransaction.related_entity_type,
                AccountTransaction.related_entity_id,
                AccountTransaction.party_name,
                AccountTransaction.reference_id,
                func.count(AccountTransaction.id).label('cnt'),
                func.group_concat(AccountTransaction.id).label('ids')
             )
             .filter(AccountTransaction.is_void == False)
             .group_by(
                AccountTransaction.date,
                AccountTransaction.type,
                AccountTransaction.amount,
                AccountTransaction.from_account_id,
                AccountTransaction.to_account_id,
                AccountTransaction.related_entity_type,
                AccountTransaction.related_entity_id,
                AccountTransaction.party_name,
                AccountTransaction.reference_id,
             )
             .having(func.count(AccountTransaction.id) > 1)
             .all())
    for row in dup_q:
        # Skip rows that share a group_id (legitimate splits like step1/step2).
        ids = [int(x) for x in str(row.ids or '').split(',') if str(x).strip().isdigit()]
        if not ids:
            continue
        member_rows = AccountTransaction.query.filter(AccountTransaction.id.in_(ids)).all()
        gids = {(m.group_id or '') for m in member_rows if (m.group_id or '')}
        # If every duplicate sits in the same group_id, skip — that's a designed split.
        if len(gids) == 1 and all(((m.group_id or '') == next(iter(gids))) for m in member_rows):
            continue
        duplicate_active.append({
            'count': int(row.cnt or 0),
            'ids': ids,
            'date': (row.date.isoformat() if row.date else ''),
            'type': (row.type or ''),
            'amount': float(row.amount or 0.0),
            'party': (row.party_name or ''),
            'reference_id': (row.reference_id or ''),
        })

    # ---- 3. Group split inconsistencies ----
    grp_q = (db.session.query(AccountTransaction.group_id,
                              func.count(AccountTransaction.id).label('cnt'))
             .filter(AccountTransaction.group_id.isnot(None),
                     AccountTransaction.group_id != '')
             .group_by(AccountTransaction.group_id)
             .having(func.count(AccountTransaction.id) > 1)
             .all())
    for g in grp_q:
        members = (AccountTransaction.query
                   .filter(AccountTransaction.group_id == g.group_id)
                   .all())
        active = [m for m in members if not bool(getattr(m, 'is_void', False))]
        voided = [m for m in members if bool(getattr(m, 'is_void', False))]
        if active and voided:
            group_inconsistent.append({
                'group_id': g.group_id,
                'kind': 'mixed_void',
                'detail': f'{len(active)} active and {len(voided)} voided rows in the same group',
                'ids': [int(m.id) for m in members],
            })
            continue
        # For step1+step2 splits, both should equal the headline amount.
        if len(active) == 2:
            amts = sorted({round(float(m.amount or 0.0), 2) for m in active})
            if len(amts) > 1:
                group_inconsistent.append({
                    'group_id': g.group_id,
                    'kind': 'amount_mismatch',
                    'detail': f'Step amounts differ: {amts}',
                    'ids': [int(m.id) for m in active],
                })

    # ---- 4. Source rows without an account txn ----
    def _has_txn_for(prefix, sid):
        like_pat = f'{prefix}:%'
        return db.session.query(AccountTransaction.id).filter(
            AccountTransaction.source_id == int(sid),
            or_(
                func.lower(func.coalesce(AccountTransaction.source_type, '')) == prefix,
                func.lower(func.coalesce(AccountTransaction.source_type, '')).like(like_pat),
            )
        ).first() is not None

    # Worker ledger payments / advances / tips — these always create a cash txn.
    for et, prefix, label in (
        ('payment',    'labour_ledger_payment',    'Worker payment'),
        ('advance',    'labour_ledger_advance',    'Worker advance'),
        ('tip',        'labour_ledger_tip',        'Worker tip'),
    ):
        rows = (LabourLedger.query
                .filter(LabourLedger.entry_type == et,
                        LabourLedger.is_void == False,
                        LabourLedger.amount > 0)
                .all())
        for r in rows:
            if _has_txn_for(prefix, r.id):
                continue
            w = Worker.query.get(int(r.worker_id or 0))
            orphan_sources.append({
                'family': label,
                'source_type': prefix,
                'source_id': int(r.id),
                'date': (r.date.isoformat() if r.date else ''),
                'amount': float(r.amount or 0.0),
                'party': (w.name if w else f'Worker #{r.worker_id}'),
            })

    # Office staff ledger.
    for et, prefix, label in (
        ('payment',    'office_staff_ledger_payment',    'Office staff payment'),
        ('advance',    'office_staff_ledger_advance',    'Office staff advance'),
        ('tip',        'office_staff_ledger_tip',        'Office staff tip'),
    ):
        rows = (OfficeStaffLedger.query
                .filter(OfficeStaffLedger.entry_type == et,
                        OfficeStaffLedger.is_void == False,
                        OfficeStaffLedger.amount > 0)
                .all())
        for r in rows:
            if _has_txn_for(prefix, r.id):
                continue
            s = OfficeStaff.query.get(int(r.staff_id or 0))
            orphan_sources.append({
                'family': label,
                'source_type': prefix,
                'source_id': int(r.id),
                'date': (r.date.isoformat() if r.date else ''),
                'amount': float(r.amount or 0.0),
                'party': (s.name if s else f'Staff #{r.staff_id}'),
            })

    # Owner payments and office expenses always pair to one cash txn.
    for r in OwnerPayment.query.filter(OwnerPayment.is_void == False, OwnerPayment.amount > 0).all():
        if _has_txn_for('owner_payment', r.id):
            continue
        orphan_sources.append({
            'family': 'Owner payment',
            'source_type': 'owner_payment',
            'source_id': int(r.id),
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'party': '-',
        })
    for r in OfficeExpense.query.filter(OfficeExpense.is_void == False, OfficeExpense.amount > 0).all():
        if _has_txn_for('office_expense', r.id):
            continue
        orphan_sources.append({
            'family': 'Office expense',
            'source_type': 'office_expense',
            'source_id': int(r.id),
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'party': '-',
        })

    return {
        'orphan_txns': orphan_txns,
        'void_mismatch': void_mismatch,
        'duplicate_active': duplicate_active,
        'group_inconsistent': group_inconsistent,
        'orphan_sources': orphan_sources,
        'totals': {
            'orphan_txns': len(orphan_txns),
            'void_mismatch': len(void_mismatch),
            'duplicate_active': len(duplicate_active),
            'group_inconsistent': len(group_inconsistent),
            'orphan_sources': len(orphan_sources),
        },
    }


_ACCOUNT_TYPES = ('company', 'cash', 'bank', 'person', 'vendor', 'client')


_ACCOUNT_COMPANY_TYPES = ('company', 'cash', 'bank')


_ACCOUNT_PROJECT_FLOW_TYPES = ('client',)


_ACCOUNT_CREDIT_DEBIT_TYPES = ('person', 'vendor')


_ACCOUNT_TXN_TYPES = (
    'transfer',
    'expense_material',
    'expense_wage',
    'expense_subcontractor',
    'office_management_payment',
    'personal_management_payment',
    'expense_general',
    'project_income',
    'party_receipt',
    'client_payment',
    'party_payment',
    'advance_to_person',
    'purchase',
    'payroll',
)


_ACCOUNT_TXN_CATEGORIES = ('salary', 'expense', 'advance', 'personal', 'transfer', 'income', 'purchase', 'payroll')


_ACCOUNT_TXN_TYPE_DEFAULT_CATEGORY = {
    'transfer': 'transfer',
    'expense_material': 'expense',
    'expense_wage': 'expense',
    'expense_subcontractor': 'expense',
    'office_management_payment': 'expense',
    'personal_management_payment': 'personal',
    'expense_general': 'expense',
    'project_income': 'income',
    'party_receipt': 'income',
    'client_payment': 'income',
    'party_payment': 'expense',
    'advance_to_person': 'advance',
    'purchase': 'purchase',
    'payroll': 'payroll',
}


_ACCOUNT_TXN_CATEGORY_DEFAULT_TYPE = {
    'transfer': 'transfer',
    'salary': 'payroll',
    'payroll': 'payroll',
    'purchase': 'purchase',
    'income': 'project_income',
    'advance': 'advance_to_person',
    'expense': 'expense_general',
    'personal': 'personal_management_payment',
}


_ACCOUNT_TXN_FORM_OPTIONS = (
    {'value': 'receive_from_project', 'label': 'Receive from Project', 'direction': 'receive'},
    {'value': 'receive_intra_company', 'label': 'Transfer from Intra Company', 'direction': 'receive'},
    {'value': 'receive_from_credit_debit', 'label': 'Receive from Credit/Debit', 'direction': 'receive'},
    {'value': 'pay_to_project', 'label': 'Pay to Project', 'direction': 'pay'},
    {'value': 'pay_intra_company', 'label': 'Transfer to Intra Company', 'direction': 'pay'},
    {'value': 'pay_to_credit_debit', 'label': 'Pay to Credit/Debit', 'direction': 'pay'},
    {'value': 'purchase', 'label': 'Material Payment (Supplier)', 'direction': 'pay'},
    {'value': 'payroll', 'label': 'Wage / Payroll Payment', 'direction': 'pay'},
    {'value': 'expense_subcontractor', 'label': 'Subcontractor Payment', 'direction': 'pay'},
    {'value': 'office_management_payment', 'label': 'Pay To Office Management', 'direction': 'pay'},
    {'value': 'personal_management_payment', 'label': 'Pay To Personal Management (Party/Purpose)', 'direction': 'pay'},
    {'value': 'expense_general', 'label': 'General Expense', 'direction': 'pay'},
)


_ACCOUNT_TXN_RECEIVE_TYPES = (
    'project_income',
    'party_receipt',
    'client_payment',
)


_ACCOUNT_TXN_PAY_TYPES = (
    'expense_material',
    'expense_wage',
    'expense_subcontractor',
    'office_management_payment',
    'personal_management_payment',
    'expense_general',
    'purchase',
    'payroll',
    'party_payment',
    'advance_to_person',
)


_ACCOUNT_OUTGOING_SCOPE_REQUIRED_TYPES = (
    'expense_general',
)


def _account_tx_direction_for_type(tx_type):
    tx = _normalize_account_tx_type(tx_type)
    if tx in _ACCOUNT_TXN_RECEIVE_TYPES:
        return 'receive'
    if tx in _ACCOUNT_TXN_PAY_TYPES:
        return 'pay'
    if tx == 'transfer':
        return 'transfer'
    return ''


def _resolve_account_type(group=None, mode=None, explicit_type=None):
    tp = (explicit_type or '').strip().lower()
    if tp in _ACCOUNT_TYPES:
        return tp
    g = _normalize_account_group(group)
    m = _normalize_account_mode(mode)
    if g == 'company':
        return ('bank' if m == 'bank' else 'cash')
    if g == 'project_in_flow':
        return 'client'
    if g == 'credit_debit':
        return 'person'
    return 'person'


def _account_group_mode_for_row(acc_row):
    tp = ((acc_row.type if acc_row else '') or '').strip().lower()
    if tp in _ACCOUNT_COMPANY_TYPES:
        group = 'company'
    elif tp in _ACCOUNT_PROJECT_FLOW_TYPES:
        group = 'project_in_flow'
    else:
        group = 'credit_debit'
    mode = 'bank' if (((acc_row.bank_name or '').strip() and (acc_row.account_number or '').strip()) or tp == 'bank') else 'cash'
    return group, mode


def _account_expected_related_type(tx_type):
    tx = (tx_type or '').strip().lower()
    if tx in ('expense_material', 'purchase'):
        return 'supplier'
    if tx in ('expense_wage', 'payroll', 'advance_to_person'):
        return 'worker'
    if tx == 'expense_subcontractor':
        return 'subcontractor'
    return ''


def _account_requires_scope_tags(tx_type):
    tx = (tx_type or '').strip().lower()
    return tx in _ACCOUNT_OUTGOING_SCOPE_REQUIRED_TYPES


def _account_rows_active():
    return (Account.query
            .filter(Account.is_void == False, func.lower(func.coalesce(Account.status, 'active')) == 'active')
            .order_by(Account.name.asc(), Account.id.asc())
            .all())


def _account_balances_query(include_inactive=False):
    outgoing_sq = (db.session.query(
        AccountTransaction.from_account_id.label('account_id'),
        func.coalesce(func.sum(AccountTransaction.amount), 0.0).label('outgoing_total')
    ).filter(
        AccountTransaction.is_void == False
    ).group_by(AccountTransaction.from_account_id).subquery())
    incoming_sq = (db.session.query(
        AccountTransaction.to_account_id.label('account_id'),
        func.coalesce(func.sum(AccountTransaction.amount), 0.0).label('incoming_total')
    ).filter(
        AccountTransaction.is_void == False,
        AccountTransaction.to_account_id.isnot(None)
    ).group_by(AccountTransaction.to_account_id).subquery())
    q = (db.session.query(
            Account,
            func.coalesce(incoming_sq.c.incoming_total, 0.0).label('incoming_total'),
            func.coalesce(outgoing_sq.c.outgoing_total, 0.0).label('outgoing_total')
        )
        .outerjoin(incoming_sq, incoming_sq.c.account_id == Account.id)
        .outerjoin(outgoing_sq, outgoing_sq.c.account_id == Account.id)
        .filter(Account.is_void == False))
    if not include_inactive:
        q = q.filter(func.lower(func.coalesce(Account.status, 'active')) == 'active')
    return q.order_by(Account.name.asc(), Account.id.asc()).all()


def _account_balance_map():
    rows = _account_balances_query(include_inactive=False)
    mp = {}
    for a, incoming, outgoing in rows:
        opening = float(a.opening_balance or 0.0)
        incoming = float(incoming or 0.0)
        outgoing = float(outgoing or 0.0)
        mp[int(a.id)] = float(opening + incoming - outgoing)
    return mp


def _account_balance(account_id):
    if not account_id:
        return 0.0
    return float(_account_balance_map().get(int(account_id), 0.0) or 0.0)


def _list_accounts_with_balances(include_inactive=False):
    rows = _account_balances_query(include_inactive=include_inactive)
    items = []
    for a, incoming, outgoing in rows:
        opening = float(a.opening_balance or 0.0)
        incoming = float(incoming or 0.0)
        outgoing = float(outgoing or 0.0)
        acc_group, acc_mode = _account_group_mode_for_row(a)
        items.append({
            # lets the list templates tag the row for "entered by" lookup
            '_hdc_entity': 'hdc_account',
            'id': int(a.id),
            'name': a.name,
            'type': (a.type or '').lower(),
            'account_group': acc_group,
            'account_mode': acc_mode,
            'status': (a.status or 'active').strip().lower(),
            'opening_balance': float(a.opening_balance or 0.0),
            'current_balance': float(opening + incoming - outgoing),
            'bank_name': (a.bank_name or ''),
            'account_number': (a.account_number or ''),
            'iban': (a.iban or ''),
            'auto_generated': bool(a.auto_generated),
            'auto_source': (a.auto_source or ''),
            'created_at': (a.created_at.isoformat(sep=' ') if a.created_at else '')
        })
    return items


def _create_account(name, acc_type, opening_balance=0.0, bank_name='', account_number='', iban='', auto_generated=False, auto_source='', account_mode=''):
    nm = _normalize_name_ci(name)
    tp = (acc_type or '').strip().lower()
    if not nm:
        return None, 'Account name is required.'
    if tp not in _ACCOUNT_TYPES:
        return None, f'Account type must be one of: {", ".join(_ACCOUNT_TYPES)}.'
    bank_name = _normalize_name_ci(bank_name)
    account_number = (account_number or '').strip()
    iban = (iban or '').strip()
    mode = _normalize_account_mode(account_mode or ('bank' if tp == 'bank' else 'cash'))
    if mode == 'bank':
        if not bank_name:
            return None, 'bank_name is required for bank accounts.'
        if not account_number:
            return None, 'account_number is required for bank accounts.'
    else:
        bank_name = ''
        account_number = ''
        iban = ''
    existing = (Account.query
                .filter(Account.is_void == False, func.lower(func.trim(Account.name)) == nm.lower())
                .first())
    if existing:
        return None, 'Account with this name already exists.'
    row = Account(
        name=nm,
        type=tp,
        opening_balance=float(opening_balance or 0.0),
        bank_name=bank_name or None,
        account_number=account_number or None,
        iban=iban or None,
        auto_generated=bool(auto_generated),
        auto_source=((auto_source or '').strip().lower() or None),
        status='active',
        is_void=False,
        created_at=_pkt_now_naive()
    )
    # Keep the integer paisa mirror in step with the legacy float column.
    sync_money_fields(row, 'opening_balance', 'opening_balance_minor')
    db.session.add(row)
    db.session.flush()
    return row, ''


def _account_get_or_create(name, acc_type='person', opening_balance=0.0, bank_name='', account_number='', iban='', auto_generated=False, auto_source='', account_mode=''):
    nm = _normalize_name_ci(name)
    if not nm:
        return None
    row = (Account.query
           .filter(Account.is_void == False, func.lower(func.trim(Account.name)) == nm.lower())
           .first())
    if row:
        return row
    row, _ = _create_account(
        nm,
        acc_type,
        opening_balance=opening_balance,
        bank_name=bank_name,
        account_number=account_number,
        iban=iban,
        auto_generated=auto_generated,
        auto_source=auto_source,
        account_mode=account_mode
    )
    return row


def _account_txn_source_exists(source_type, source_id):
    if not source_type or not source_id:
        return False
    return db.session.query(AccountTransaction.id).filter(
        AccountTransaction.source_type == str(source_type),
        AccountTransaction.source_id == int(source_id),
        AccountTransaction.is_void == False
    ).first() is not None


def _normalize_account_txn_payload(payload):
    data = payload or {}
    amount = max(0.0, _flt(data.get('amount'), 0.0))
    tx_type = _normalize_account_tx_type((data.get('type') or data.get('transaction_type') or ''))
    from_account_id = _payload_int(data, 'from_account_id')
    to_account_id = _payload_int(data, 'to_account_id')
    executed_by_account_id = _payload_int(data, 'executed_by_account_id') or from_account_id
    project_id = _payload_int(data, 'project_id')
    stage_id = _payload_int(data, 'stage_id')
    related_entity_type = (data.get('related_entity_type') or '').strip().lower()
    related_entity_id = _payload_int(data, 'related_entity_id')
    category = (data.get('category') or '').strip().lower()
    if (not tx_type) and category:
        tx_type = _ACCOUNT_TXN_CATEGORY_DEFAULT_TYPE.get(category, '')
    if tx_type:
        # Keep category deterministic from transaction type to prevent conflicting inputs.
        category = _ACCOUNT_TXN_TYPE_DEFAULT_CATEGORY.get(tx_type, category)
    if tx_type and (not related_entity_type):
        inferred_rel = _account_expected_related_type(tx_type)
        if inferred_rel:
            related_entity_type = inferred_rel
    party_name = _normalize_name_ci(data.get('party_name'))
    note = (data.get('note') or '').strip()
    reference_id = (data.get('reference_id') or '').strip()
    group_id = (data.get('group_id') or '').strip()
    source_type = (data.get('source_type') or '').strip()
    source_id = _payload_int(data, 'source_id')
    _date_raw = (data.get('date') or '').strip()
    tx_date = None
    date_parse_error = False
    try:
        tx_date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else None
    except Exception:
        tx_date = None
        date_parse_error = True
    return {
        'amount': amount,
        'type': tx_type,
        'from_account_id': from_account_id,
        'to_account_id': (to_account_id or None),
        'executed_by_account_id': executed_by_account_id,
        'project_id': (project_id or None),
        'stage_id': (stage_id or None),
        'related_entity_type': related_entity_type,
        'related_entity_id': (related_entity_id or None),
        'category': category,
        'party_name': party_name,
        'note': note,
        'reference_id': reference_id,
        'group_id': group_id,
        'source_type': source_type,
        'source_id': (source_id or None),
        'date': tx_date,
        '_date_raw': _date_raw,
        '_date_parse_error': date_parse_error,
    }


def _validate_account_transaction_payload(norm):
    amount = float(norm.get('amount') or 0.0)
    tx_type = (norm.get('type') or '').strip().lower()
    from_account_id = int(norm.get('from_account_id') or 0)
    to_account_id = int(norm.get('to_account_id') or 0)
    executed_by_account_id = int(norm.get('executed_by_account_id') or 0)
    project_id = int(norm.get('project_id') or 0)
    stage_id = int(norm.get('stage_id') or 0)
    category = (norm.get('category') or '').strip().lower()
    related_entity_type = _normalize_related_entity_type(norm.get('related_entity_type'))
    related_entity_id = int(norm.get('related_entity_id') or 0)
    party_name = _normalize_name_ci(norm.get('party_name'))
    tx_date = norm.get('date')
    date_raw = (norm.get('_date_raw') or '').strip()
    date_parse_error = bool(norm.get('_date_parse_error'))

    if amount <= 0:
        return False, 'Amount must be greater than 0.'
    if not date_raw:
        return False, 'date is required.'
    if date_parse_error:
        return False, 'date must be in YYYY-MM-DD format.'
    if not tx_date:
        return False, 'Valid date is required.'
    if not from_account_id:
        return False, 'from_account_id is required.'
    if not executed_by_account_id:
        return False, 'executed_by_account_id is required.'
    if tx_type not in _ACCOUNT_TXN_TYPES:
        return False, f'type must be one of: {", ".join(_ACCOUNT_TXN_TYPES)}.'
    if category not in _ACCOUNT_TXN_CATEGORIES:
        return False, f'Category must be one of: {", ".join(_ACCOUNT_TXN_CATEGORIES)}.'

    from_account = Account.query.get(from_account_id)
    exec_account = Account.query.get(executed_by_account_id)
    to_account = Account.query.get(to_account_id) if to_account_id else None
    if (not from_account) or from_account.is_void:
        return False, 'Valid from account is required.'
    if (not exec_account) or exec_account.is_void:
        return False, 'Valid executed_by account is required.'
    if to_account_id and ((not to_account) or to_account.is_void):
        return False, 'Selected to account is invalid.'
    if from_account_id and to_account_id and from_account_id == to_account_id:
        return False, 'from_account_id and to_account_id must be different.'
    if project_id and (Project.query.get(project_id) is None):
        return False, 'project_id is invalid.'
    stg = Stage.query.get(stage_id) if stage_id else None
    if stage_id and (not stg):
        return False, 'stage_id is invalid.'
    if stage_id and project_id and int(stg.project_id or 0) != int(project_id):
        return False, 'stage_id does not belong to selected project.'
    if _account_requires_scope_tags(tx_type):
        if not project_id:
            return False, f'project_id is required for outgoing payment type={tx_type}.'
        if not stage_id:
            return False, f'stage_id is required for outgoing payment type={tx_type}.'

    if tx_type in ('transfer', 'advance_to_person', 'project_income', 'client_payment', 'party_receipt'):
        if not to_account_id:
            return False, f'to_account_id is required for type={tx_type}.'
    if tx_type in ('expense_material', 'expense_wage', 'expense_subcontractor', 'expense_general', 'purchase', 'payroll', 'party_payment', 'personal_management_payment'):
        if (not to_account_id) and (not party_name):
            return False, f'to_account_id or party_name is required for type={tx_type}.'
    if tx_type in ('office_management_payment',):
        if (not to_account_id) and (not party_name):
            return False, f'to_account_id or party_name is required for type={tx_type}.'
    if tx_type in ('project_income',) and not project_id:
        return False, f'project_id is required for type={tx_type}.'
    expected_rel = _account_expected_related_type(tx_type)
    if expected_rel:
        if related_entity_type != expected_rel:
            return False, f'{tx_type} requires related_entity_type={expected_rel}.'
        if not related_entity_id:
            return False, f'related_entity_id is required for type={tx_type}.'
    return True, ''


def _check_overdraft_block(pending_rows):
    """Overdraft protection using exact minor units (paisa) for smooth, accurate blocking.

    Enhanced for Money Center: uses to_minor/from_minor for exact paisa math,
    so Rs. 100.00 - Rs. 100.00 = 0 exactly, not 1e-10 floating error.
    Treasury accounts (company/cash/bank) cannot go negative; other types can.
    """
    if not pending_rows:
        return True, ''
    # Build exact minor-unit balance map
    try:
        from hdc.utils.money import to_minor as _to_minor
        bal_float = _account_balance_map()
        bal_minor = {}
        for aid, bval in bal_float.items():
            try:
                bal_minor[int(aid)] = _to_minor(bval)
            except Exception:
                bal_minor[int(aid)] = int(round(float(bval or 0.0) * 100))
    except Exception:
        bal_float = _account_balance_map()
        bal_minor = {int(k): int(round(float(v or 0.0) * 100)) for k, v in bal_float.items()}

    for r in pending_rows:
        from_id = int(r.get('from_account_id') or 0)
        to_id = int(r.get('to_account_id') or 0)
        amt = r.get('amount') or 0.0
        try:
            from hdc.utils.money import to_minor as _to_minor
            amt_minor = _to_minor(amt)
        except Exception:
            amt_minor = int(round(float(amt or 0.0) * 100))

        if from_id:
            bal_minor[from_id] = int(bal_minor.get(from_id, 0) or 0) - amt_minor
        if to_id:
            bal_minor[to_id] = int(bal_minor.get(to_id, 0) or 0) + amt_minor

        if from_id and int(bal_minor.get(from_id, 0) or 0) < 0:
            acc = Account.query.get(from_id)
            tp = (acc.type or '').strip().lower() if acc else ''
            if tp in ('company', 'cash', 'bank'):
                nm = acc.name if acc else f'#{from_id}'
                # Convert minor back to readable
                try:
                    from hdc.utils.money import from_minor as _from_minor
                    bal_readable = float(_from_minor(bal_minor.get(from_id, 0)))
                except Exception:
                    bal_readable = float(bal_minor.get(from_id, 0) or 0) / 100.0
                return False, f'Insufficient balance in account: {nm}. Would be {bal_readable:,.2f} PKR after this transaction. Overdraft blocked for treasury accounts.'
    return True, ''


def _check_overdraft_block_replace(existing_rows, replacement_rows):
    """Overdraft check for edit flow using exact minor units."""
    try:
        from hdc.utils.money import to_minor as _to_minor, from_minor as _from_minor
        bal_float = _account_balance_map()
        bal_minor = {}
        for aid, bval in bal_float.items():
            try:
                bal_minor[int(aid)] = _to_minor(bval)
            except Exception:
                bal_minor[int(aid)] = int(round(float(bval or 0.0) * 100))
    except Exception:
        bal_float = _account_balance_map()
        bal_minor = {int(k): int(round(float(v or 0.0) * 100)) for k, v in bal_float.items()}

    for r in (existing_rows or []):
        if bool(getattr(r, 'is_void', False)):
            continue
        from_id = int(getattr(r, 'from_account_id', 0) or 0)
        to_id = int(getattr(r, 'to_account_id', 0) or 0)
        amt = float(getattr(r, 'amount', 0.0) or 0.0)
        try:
            from hdc.utils.money import to_minor as _to_minor
            amt_minor = _to_minor(amt)
        except Exception:
            amt_minor = int(round(float(amt or 0.0) * 100))
        if from_id:
            bal_minor[from_id] = int(bal_minor.get(from_id, 0) or 0) + amt_minor
        if to_id:
            bal_minor[to_id] = int(bal_minor.get(to_id, 0) or 0) - amt_minor

    for r in (replacement_rows or []):
        from_id = int((r.get('from_account_id') if isinstance(r, dict) else 0) or 0)
        to_id = int((r.get('to_account_id') if isinstance(r, dict) else 0) or 0)
        amt_raw = (r.get('amount') if isinstance(r, dict) else 0.0) or 0.0
        try:
            from hdc.utils.money import to_minor as _to_minor
            amt_minor = _to_minor(amt_raw)
        except Exception:
            amt_minor = int(round(float(amt_raw or 0.0) * 100))
        if from_id:
            bal_minor[from_id] = int(bal_minor.get(from_id, 0) or 0) - amt_minor
        if to_id:
            bal_minor[to_id] = int(bal_minor.get(to_id, 0) or 0) + amt_minor
        if from_id and int(bal_minor.get(from_id, 0) or 0) < 0:
            acc = Account.query.get(from_id)
            tp = (acc.type or '').strip().lower() if acc else ''
            if tp in ('company', 'cash', 'bank'):
                nm = acc.name if acc else f'#{from_id}'
                try:
                    from hdc.utils.money import from_minor as _from_minor
                    bal_readable = float(_from_minor(bal_minor.get(from_id, 0)))
                except Exception:
                    bal_readable = float(bal_minor.get(from_id, 0) or 0) / 100.0
                return False, f'Insufficient balance in account: {nm}. Would be {bal_readable:,.2f} PKR after edit. Overdraft blocked.'
    return True, ''


def _build_account_txn_rows(norm):
    amount = float(norm.get('amount') or 0.0)
    tx_type = (norm.get('type') or '').strip().lower()
    from_account_id = int(norm.get('from_account_id') or 0)
    to_account_id = int(norm.get('to_account_id') or 0) if norm.get('to_account_id') else None
    executed_by_account_id = int(norm.get('executed_by_account_id') or 0)
    project_id = int(norm.get('project_id') or 0) if norm.get('project_id') else None
    stage_id = int(norm.get('stage_id') or 0) if norm.get('stage_id') else None
    related_entity_type = (norm.get('related_entity_type') or '').strip().lower() or None
    related_entity_id = int(norm.get('related_entity_id') or 0) if norm.get('related_entity_id') else None
    category = (norm.get('category') or '').strip().lower()
    party_name = _normalize_name_ci(norm.get('party_name'))
    note = norm.get('note') or None
    reference_id = norm.get('reference_id') or None
    base_group = (norm.get('group_id') or '').strip() or uuid4().hex
    tx_date = norm.get('date') or _pkt_today()
    source_type = norm.get('source_type') or None
    source_id = norm.get('source_id') if norm.get('source_id') is not None else None

    # direct or indirect split by executor
    rows = []
    if from_account_id == executed_by_account_id:
        rows.append({
            'date': tx_date,
            'amount': amount,
            'type': tx_type,
            'from_account_id': from_account_id,
            'to_account_id': to_account_id,
            'executed_by_account_id': executed_by_account_id,
            'project_id': project_id,
            'stage_id': stage_id,
            'related_entity_type': related_entity_type,
            'related_entity_id': related_entity_id,
            'party_name': party_name if not to_account_id else (party_name or None),
            'category': category,
            'note': note,
            'reference_id': reference_id,
            'group_id': base_group,
            'source_type': (f'{source_type}:direct' if source_type else None),
            'source_id': source_id,
            'is_void': False,
            'created_at': _pkt_now_naive()
        })
        return rows

    # step 1: source -> executor (internal transfer)
    rows.append({
        'date': tx_date,
        'amount': amount,
        'type': 'transfer',
        'from_account_id': from_account_id,
        'to_account_id': executed_by_account_id,
        'executed_by_account_id': executed_by_account_id,
        'project_id': project_id,
        'stage_id': stage_id,
        'related_entity_type': related_entity_type,
        'related_entity_id': related_entity_id,
        'party_name': None,
        'category': 'transfer',
        'note': (note or 'Auto split (step 1)'),
        'reference_id': reference_id,
        'group_id': base_group,
        'source_type': (f'{source_type}:step1' if source_type else None),
        'source_id': source_id,
        'is_void': False,
        'created_at': _pkt_now_naive()
    })
    # step 2: executor -> final destination
    rows.append({
        'date': tx_date,
        'amount': amount,
        'type': tx_type,
        'from_account_id': executed_by_account_id,
        'to_account_id': to_account_id,
        'executed_by_account_id': executed_by_account_id,
        'project_id': project_id,
        'stage_id': stage_id,
        'related_entity_type': related_entity_type,
        'related_entity_id': related_entity_id,
        'party_name': party_name if not to_account_id else (party_name or None),
        'category': category,
        'note': (note or 'Auto split (step 2)'),
        'reference_id': reference_id,
        'group_id': base_group,
        'source_type': (f'{source_type}:step2' if source_type else None),
        'source_id': source_id,
        'is_void': False,
        'created_at': _pkt_now_naive()
    })
    return rows


def _create_account_transaction(payload, commit=True):
    norm = _normalize_account_txn_payload(payload)
    ok, msg = _validate_account_transaction_payload(norm)
    if not ok:
        return False, msg, []
    if norm.get('source_type') and norm.get('source_id'):
        if _account_txn_source_exists(f"{norm['source_type']}:direct", norm['source_id']) \
           or _account_txn_source_exists(f"{norm['source_type']}:step1", norm['source_id']):
            return False, 'Duplicate source transaction detected.', []
    rows = _build_account_txn_rows(norm)
    ok_bal, msg_bal = _check_overdraft_block(rows)
    if not ok_bal:
        return False, msg_bal, []

    created = []
    try:
        for r in rows:
            row = AccountTransaction(**r)
            db.session.add(row)
            created.append(row)
        db.session.flush()
        if commit:
            db.session.commit()
        return True, '', created
    except Exception as ex:
        db.session.rollback()
        return False, f'Transaction save failed: {ex}', []


def _account_transaction_history(account_id=None, account_group=None, date_from=None, date_to=None, category=None, group_id=None, reference_id=None, limit=500, project_id=None, stage_id=None, tx_type=None, tx_direction=None, party_name=None, worker_id=None, return_query=False):
    q = AccountTransaction.query.filter(AccountTransaction.is_void == False)
    if account_id:
        q = q.filter(or_(AccountTransaction.from_account_id == account_id, AccountTransaction.to_account_id == account_id))
    if account_group:
        grp = _normalize_account_group(account_group)
        g_ids = [int(a.id) for a in Account.query.filter(Account.is_void == False).all() if _account_group_mode_for_row(a)[0] == grp]
        if not g_ids:
            if return_query:
                return q.filter(AccountTransaction.id == 0)
            return []
        q = q.filter(or_(AccountTransaction.from_account_id.in_(g_ids), AccountTransaction.to_account_id.in_(g_ids)))
    if date_from:
        q = q.filter(AccountTransaction.date >= date_from)
    if date_to:
        q = q.filter(AccountTransaction.date <= date_to)
    if category:
        q = q.filter(func.lower(AccountTransaction.category) == str(category).strip().lower())
    if tx_type:
        q = q.filter(func.lower(func.coalesce(AccountTransaction.type, '')) == str(tx_type).strip().lower())
    dir_norm = _normalize_account_tx_direction(tx_direction)
    if dir_norm == 'receive':
        q = q.filter(or_(
            func.lower(func.coalesce(AccountTransaction.type, '')).in_(_ACCOUNT_TXN_RECEIVE_TYPES),
            func.lower(func.coalesce(AccountTransaction.type, '')) == 'transfer'
        ))
    elif dir_norm == 'pay':
        q = q.filter(or_(
            func.lower(func.coalesce(AccountTransaction.type, '')).in_(_ACCOUNT_TXN_PAY_TYPES),
            func.lower(func.coalesce(AccountTransaction.type, '')) == 'transfer'
        ))
    if project_id:
        q = q.filter(AccountTransaction.project_id == int(project_id))
    if stage_id:
        q = q.filter(AccountTransaction.stage_id == int(stage_id))
    if group_id:
        q = q.filter(AccountTransaction.group_id == str(group_id))
    if reference_id:
        q = q.filter(AccountTransaction.reference_id == str(reference_id))
    # Free-text Party / Vendor / Worker name search (substring, case-insensitive).
    if party_name:
        pat = f"%{str(party_name).strip().lower()}%"
        q = q.filter(func.lower(func.coalesce(AccountTransaction.party_name, '')).like(pat))
    # Worker FK filter: rows linked to a specific worker via related_entity, or
    # whose party_name matches the worker's name (legacy rows without FK link).
    if worker_id:
        try:
            w_obj = Worker.query.get(int(worker_id))
        except Exception:
            w_obj = None
        if not w_obj:
            if return_query:
                return q.filter(AccountTransaction.id == 0)
            return []
        wname_pat = f"%{(w_obj.name or '').strip().lower()}%"
        q = q.filter(or_(
            and_(
                func.lower(func.coalesce(AccountTransaction.related_entity_type, '')) == 'worker',
                AccountTransaction.related_entity_id == int(w_obj.id)
            ),
            func.lower(func.coalesce(AccountTransaction.party_name, '')).like(wname_pat),
        ))
    q = q.order_by(AccountTransaction.date.desc(), AccountTransaction.id.desc())
    if return_query:
        return q
    rows = q.limit(max(1, min(int(limit or 500), 2000))).all()
    return rows


def _account_txn_to_dict(r):
    return {
        'id': int(r.id),
        'date': (r.date.isoformat() if r.date else ''),
        'amount': float(r.amount or 0.0),
        'type': (r.type or ''),
        'from_account_id': (int(r.from_account_id) if r.from_account_id else None),
        'from_account_name': (r.from_account.name if r.from_account else ''),
        'to_account_id': (int(r.to_account_id) if r.to_account_id else None),
        'to_account_name': (r.to_account.name if r.to_account else ''),
        'executed_by_account_id': (int(r.executed_by_account_id) if r.executed_by_account_id else None),
        'executed_by_account_name': (r.executed_by_account.name if r.executed_by_account else ''),
        'project_id': (int(r.project_id) if r.project_id else None),
        'project_name': (r.project.name if r.project else ''),
        'stage_id': (int(r.stage_id) if r.stage_id else None),
        'stage_name': (r.stage.name if r.stage else ''),
        'related_entity_type': (r.related_entity_type or ''),
        'related_entity_id': (int(r.related_entity_id) if r.related_entity_id is not None else None),
        'party_name': (r.party_name or ''),
        'category': (r.category or ''),
        'note': (r.note or ''),
        'reference_id': (r.reference_id or ''),
        'group_id': (r.group_id or ''),
        'source_type': (r.source_type or ''),
        'source_id': (int(r.source_id) if r.source_id is not None else None),
        'created_at': (r.created_at.isoformat(sep=' ') if r.created_at else ''),
    }


def _account_source_type_base(v):
    s = (v or '').strip().lower()
    if not s:
        return ''
    return s.split(':', 1)[0].strip().lower()


def _account_txn_group_rows(txn_row):
    if not txn_row:
        return []
    gid = (txn_row.group_id or '').strip()
    if gid:
        return (AccountTransaction.query
                .filter(AccountTransaction.group_id == gid)
                .order_by(AccountTransaction.id.asc())
                .all())
    return [txn_row]


def _accounts_set_void_by_source(source_type, source_id, make_void=True):
    st = (source_type or '').strip().lower()
    sid = int(source_id or 0)
    if (not st) or (not sid):
        return 0
    rows = (AccountTransaction.query
            .filter(
                AccountTransaction.source_id == sid,
                or_(
                    func.lower(func.coalesce(AccountTransaction.source_type, '')) == st,
                    func.lower(func.coalesce(AccountTransaction.source_type, '')).like(f'{st}:%')
                )
            )
            .all())
    for r in rows:
        r.is_void = bool(make_void)
    return len(rows)


def _set_void_state_row(row, make_void=True, reason=''):
    if not row:
        return True, ''
    if not hasattr(row, 'is_void'):
        return True, ''
    row.is_void = bool(make_void)
    if hasattr(row, 'void_reason'):
        row.void_reason = ((reason or '').strip() if make_void else None)
    if hasattr(row, 'voided_at'):
        row.voided_at = (_pkt_now_naive() if make_void else None)
    return True, ''


def _sync_source_row_void_state(source_type, source_id, make_void=True, reason=''):
    st = (source_type or '').strip().lower()
    sid = int(source_id or 0)
    if (not st) or (not sid):
        return True, ''
    row = None
    handled = True
    if st == 'owner_payment':
        row = OwnerPayment.query.get(sid)
    elif st in ('labour_ledger_advance', 'labour_ledger_payment', 'labour_ledger_tip', 'worker_payment'):
        row = LabourLedger.query.get(sid)
    elif st in ('supplier_credit_payment', 'supplier_credit_tip', 'supplier_credit_settlement'):
        row = SupplierLedger.query.get(sid)
    elif st in ('subcontract_payment_payment', 'subcontract_payment_tip'):
        row = SubcontractPayment.query.get(sid)
    elif st in ('office_staff_ledger_advance', 'office_staff_ledger_payment', 'office_staff_ledger_tip'):
        row = OfficeStaffLedger.query.get(sid)
    elif st == 'office_expense':
        row = OfficeExpense.query.get(sid)
    elif st == 'expense':
        row = Expense.query.get(sid)
    elif st == 'purchase_v2_paid':
        row = PurchaseV2.query.get(sid)
        if not row:
            return False, f'Linked source row not found for {st}#{sid}.'
        if row.is_void:
            return False, f'Cannot sync {st}#{sid}; source purchase is void.'
        row.payment_status = ('unpaid' if make_void else 'paid')
        row.updated_at = _pkt_now_naive()
        _sync_purchase_v2_ledger(row)
        return True, ''
    else:
        handled = False
    if not handled:
        return True, ''
    if not row:
        return False, f'Linked source row not found for {st}#{sid}.'
    ok, msg = _set_void_state_row(row, make_void=make_void, reason=reason)
    if (not ok):
        return ok, msg
    if st in ('office_staff_ledger_advance', 'office_staff_ledger_payment', 'office_staff_ledger_tip'):
        staff_row = OfficeStaff.query.get(int(getattr(row, 'staff_id', 0) or 0))
        if make_void:
            _remove_office_salary_expense_for_ledger(row.id)
        elif staff_row:
            _sync_office_staff_expense_from_ledger(staff_row, row)
    return True, ''


def _accounts_update_manual_transaction(txn_id, payload):
    row = AccountTransaction.query.get(int(txn_id or 0))
    if not row:
        return False, 'Transaction not found.', 0
    rows = _account_txn_group_rows(row)
    if len(rows) != 1:
        return False, 'Grouped/split transactions cannot be edited directly. Void/reverse and recreate.', 0
    if row.is_void:
        return False, 'Voided transaction cannot be edited.', 0
    if row.source_type:
        flash('Warning: This transaction is synced from source. Changes may be overwritten by the source system.', 'warning')
    merged = {
        'date': ((row.date.isoformat() if row.date else _pkt_today().isoformat())),
        'type': (row.type or ''),
        'amount': float(row.amount or 0.0),
        'from_account_id': int(row.from_account_id or 0),
        'to_account_id': int(row.to_account_id or 0),
        'executed_by_account_id': int(row.executed_by_account_id or row.from_account_id or 0),
        'project_id': int(row.project_id or 0),
        'stage_id': int(row.stage_id or 0),
        'related_entity_type': (row.related_entity_type or ''),
        'related_entity_id': int(row.related_entity_id or 0),
        'party_name': (row.party_name or ''),
        'category': (row.category or ''),
        'note': (row.note or ''),
        'reference_id': (row.reference_id or ''),
        'group_id': (row.group_id or ''),
        'source_type': (row.source_type or ''),
        'source_id': (int(row.source_id) if row.source_id else 0),
    }
    for key in (
        'date', 'type', 'amount', 'from_account_id', 'to_account_id', 'executed_by_account_id',
        'project_id', 'stage_id', 'related_entity_type', 'related_entity_id',
        'party_name', 'note', 'reference_id'
    ):
        if key in (payload or {}):
            if key in ('related_entity_type', 'related_entity_id') and (payload.get(key) is None or str(payload.get(key)).strip() == ''):
                continue
            merged[key] = payload.get(key)
    if 'to_account_id' in (payload or {}) and not payload.get('to_account_id'):
        merged['to_account_id'] = ''
    if 'project_id' in (payload or {}) and not payload.get('project_id'):
        merged['project_id'] = ''
    if 'stage_id' in (payload or {}) and not payload.get('stage_id'):
        merged['stage_id'] = ''
    if 'executed_by_account_id' not in (payload or {}):
        merged['executed_by_account_id'] = merged.get('from_account_id')

    norm = _normalize_account_txn_payload(merged)
    ok, msg = _validate_account_transaction_payload(norm)
    if not ok:
        return False, msg, 0
    replacement_rows = _build_account_txn_rows(norm)
    if len(replacement_rows) != 1:
        return False, 'Only single-row transaction edit is supported.', 0
    ok_bal, msg_bal = _check_overdraft_block_replace([row], replacement_rows)
    if not ok_bal:
        return False, msg_bal, 0
    repl = replacement_rows[0]

    # Check if update would create duplicate
    duplicate = db.session.query(AccountTransaction.id).filter(
        AccountTransaction.id != row.id,
        AccountTransaction.date == repl['date'],
        AccountTransaction.type == repl['type'],
        AccountTransaction.amount == repl['amount'],
        AccountTransaction.from_account_id == repl['from_account_id'],
        AccountTransaction.to_account_id == repl['to_account_id'],
        AccountTransaction.project_id == repl['project_id'],
        AccountTransaction.stage_id == repl['stage_id'],
        AccountTransaction.party_name == repl['party_name'],
        AccountTransaction.reference_id == repl['reference_id'],
        AccountTransaction.is_void == False
    ).first()
    if duplicate:
        return False, 'Update would create a duplicate transaction.', 0

    row.date = repl.get('date') or row.date
    row.amount = float(repl.get('amount') or 0.0)
    row.type = (repl.get('type') or row.type)
    row.from_account_id = int(repl.get('from_account_id') or row.from_account_id)
    row.to_account_id = (int(repl.get('to_account_id')) if repl.get('to_account_id') else None)
    row.executed_by_account_id = int(repl.get('executed_by_account_id') or row.from_account_id)
    row.project_id = (int(repl.get('project_id')) if repl.get('project_id') else None)
    row.stage_id = (int(repl.get('stage_id')) if repl.get('stage_id') else None)
    row.related_entity_type = (repl.get('related_entity_type') or None)
    row.related_entity_id = (int(repl.get('related_entity_id')) if repl.get('related_entity_id') else None)
    row.party_name = (repl.get('party_name') or None)
    row.category = (repl.get('category') or row.category)
    row.note = (repl.get('note') or None)
    row.reference_id = (repl.get('reference_id') or None)

    _sync_account_transaction_source_update(row)
    db.session.commit()
    return True, '', 1


def _sync_account_transaction_source_update(txn_row):
    if not txn_row or not txn_row.source_type or not txn_row.source_id:
        return
    source_type = _account_source_type_base(txn_row.source_type)
    if source_type not in ('labour_ledger_advance', 'labour_ledger_payment', 'labour_ledger_tip'):
        return
    ledger_row = LabourLedger.query.get(int(txn_row.source_id or 0))
    if not ledger_row or ledger_row.is_void:
        return
    ledger_row.date = txn_row.date or ledger_row.date
    ledger_row.activity_at = _activity_at_for(ledger_row.date)
    ledger_row.amount = float(txn_row.amount or ledger_row.amount)
    ledger_row.project_id = txn_row.project_id
    ledger_row.stage_id = txn_row.stage_id
    ledger_row.notes = (txn_row.note or ledger_row.notes)
    if source_type == 'labour_ledger_advance':
        ledger_row.entry_type = 'advance'
    elif source_type == 'labour_ledger_payment':
        ledger_row.entry_type = 'payment'
    elif source_type == 'labour_ledger_tip':
        ledger_row.entry_type = 'tip'
    if str((txn_row.related_entity_type or '')).strip().lower() == 'worker' and txn_row.related_entity_id:
        ledger_row.worker_id = int(txn_row.related_entity_id)


def _accounts_toggle_transaction_void_state(txn_id, make_void=True, reason=''):
    row = AccountTransaction.query.get(int(txn_id or 0))
    if not row:
        return False, 'Transaction not found.', 0
    rows = _account_txn_group_rows(row)
    source_pairs = set()
    for r in rows:
        st_base = _account_source_type_base(r.source_type)
        sid = int(r.source_id or 0)
        if st_base and sid:
            source_pairs.add((st_base, sid))
    for st_base, sid in source_pairs:
        ok, msg = _sync_source_row_void_state(st_base, sid, make_void=make_void, reason=reason)
        if not ok:
            db.session.rollback()
            return False, (msg or 'Unable to sync source row state.'), 0
    for r in rows:
        r.is_void = bool(make_void)
    db.session.commit()
    return True, '', len(rows)


def _accounts_reconciliation_snapshot(limit=50):
    out = []

    # Paid purchases without active Accounts posting
    paid_ids = [int(r.id) for r in PurchaseV2.query.filter(PurchaseV2.is_void == False, func.lower(PurchaseV2.payment_status) == 'paid').all()]
    missing_paid = []
    for pid in paid_ids:
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == pid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'purchase_v2_paid:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like('purchase_v2_paid:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_paid.append(pid)
    out.append({'key': 'purchase_paid_missing_accounts', 'count': len(missing_paid), 'sample_ids': missing_paid[:limit]})

    # Unpaid/void purchases that still have active Accounts posting
    stale_paid = []
    candidate_acc = (AccountTransaction.query
                     .filter(
                         AccountTransaction.is_void == False,
                         or_(
                             func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'purchase_v2_paid:direct',
                             func.lower(func.coalesce(AccountTransaction.source_type, '')).like('purchase_v2_paid:%')
                         )
                     )
                     .all())
    for a in candidate_acc:
        pid = int(a.source_id or 0)
        p = PurchaseV2.query.get(pid) if pid else None
        if (not p) or p.is_void or ((p.payment_status or 'unpaid').strip().lower() != 'paid'):
            stale_paid.append(int(a.id))
    out.append({'key': 'purchase_accounts_stale_active', 'count': len(stale_paid), 'sample_ids': stale_paid[:limit]})

    # Owner payments without active Accounts posting
    owner_ids = [int(r.id) for r in OwnerPayment.query.filter(OwnerPayment.is_void == False).all()]
    missing_owner = []
    for oid in owner_ids:
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == oid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'owner_payment:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like('owner_payment:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_owner.append(oid)
    out.append({'key': 'owner_payment_missing_accounts', 'count': len(missing_owner), 'sample_ids': missing_owner[:limit]})

    # Worker ledger (advance/payment/tip) without active Accounts posting
    labour_ids = [
        int(r.id) for r in LabourLedger.query.filter(
            LabourLedger.is_void == False,
            LabourLedger.entry_type.in_(('advance', 'payment', 'tip'))
        ).all()
    ]
    missing_labour = []
    for lid in labour_ids:
        row = LabourLedger.query.get(lid)
        st1 = f"labour_ledger_{(row.entry_type or '').strip().lower()}"
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == lid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == f'{st1}:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like(f'{st1}:%'),
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'worker_payment:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like('worker_payment:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_labour.append(lid)
    out.append({'key': 'labour_ledger_missing_accounts', 'count': len(missing_labour), 'sample_ids': missing_labour[:limit]})

    # Subcontract payments without active Accounts posting
    sub_payment_ids = [
        int(r.id) for r in SubcontractPayment.query.filter(
            SubcontractPayment.is_void == False,
            func.lower(func.coalesce(SubcontractPayment.entry_type, 'payment')).in_(('payment', 'tip'))
        ).all()
    ]
    missing_sub_pay = []
    for spid in sub_payment_ids:
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == spid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'subcontract_payment_payment:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like('subcontract_payment_payment:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_sub_pay.append(spid)
    out.append({'key': 'subcontract_payment_missing_accounts', 'count': len(missing_sub_pay), 'sample_ids': missing_sub_pay[:limit]})

    # Office staff ledger cash entries without active Accounts posting
    office_ledger_ids = [
        int(r.id) for r in OfficeStaffLedger.query.filter(
            OfficeStaffLedger.is_void == False,
            func.lower(func.coalesce(OfficeStaffLedger.entry_type, '')).in_(('advance', 'payment', 'tip'))
        ).all()
    ]
    missing_office_ledger = []
    for oid in office_ledger_ids:
        row = OfficeStaffLedger.query.get(oid)
        st1 = f"office_staff_ledger_{(row.entry_type or '').strip().lower()}"
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == oid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == f'{st1}:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like(f'{st1}:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_office_ledger.append(oid)
    out.append({'key': 'office_staff_ledger_missing_accounts', 'count': len(missing_office_ledger), 'sample_ids': missing_office_ledger[:limit]})

    # Office expenses without active Accounts posting
    office_expense_ids = [
        int(r.id) for r in OfficeExpense.query.filter(
            OfficeExpense.is_void == False,
            OfficeExpense.office_staff_ledger_id.is_(None)
        ).all()
    ]
    missing_office_exp = []
    for oeid in office_expense_ids:
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == oeid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'office_expense:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like('office_expense:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_office_exp.append(oeid)
    out.append({'key': 'office_expense_missing_accounts', 'count': len(missing_office_exp), 'sample_ids': missing_office_exp[:limit]})

    return out


def _accounts_forensic_report(limit=100):
    limit = max(1, min(int(limit or 100), 1000))
    out = {
        'reconciliation': _accounts_reconciliation_snapshot(limit=limit),
        'orphans': [],
    }
    scope_required_types = tuple(str(x).strip().lower() for x in (_ACCOUNT_OUTGOING_SCOPE_REQUIRED_TYPES or ()) if str(x).strip())
    outgoing_missing_scope_q = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        or_(AccountTransaction.project_id.is_(None), AccountTransaction.stage_id.is_(None))
    )
    if scope_required_types:
        outgoing_missing_scope_q = outgoing_missing_scope_q.filter(
            func.lower(func.coalesce(AccountTransaction.type, '')).in_(scope_required_types)
        )
    else:
        outgoing_missing_scope_q = outgoing_missing_scope_q.filter(text('1=0'))
    outgoing_missing_scope = (outgoing_missing_scope_q
                              .order_by(AccountTransaction.id.desc())
                              .limit(limit)
                              .all())
    out['orphans'].append({
        'key': 'outgoing_missing_project_or_stage',
        'count': len(outgoing_missing_scope),
        'sample_ids': [int(r.id) for r in outgoing_missing_scope[:limit]]
    })

    owner_missing_receive = (OwnerPayment.query
                             .filter(
                                 OwnerPayment.is_void == False,
                                 OwnerPayment.received_to_account_id.is_(None)
                             )
                             .order_by(OwnerPayment.id.desc())
                             .limit(limit)
                             .all())
    out['orphans'].append({
        'key': 'owner_payment_missing_receiving_account',
        'count': len(owner_missing_receive),
        'sample_ids': [int(r.id) for r in owner_missing_receive[:limit]]
    })
    return out


def _account_intent_field_matrix():
    out = {
        'transfer': {'from_account': True, 'to_account': True, 'project': False, 'stage': False, 'related': False},
        'expense_material': {'from_account': True, 'to_account': False, 'project': False, 'stage': False, 'related': True},
        # Wage/subcontractor payments may optionally carry project/stage;
        # project+stage become mandatory only when tip/settlement is used.
        'expense_wage': {'from_account': True, 'to_account': False, 'project': True, 'stage': True, 'related': True},
        'expense_subcontractor': {'from_account': True, 'to_account': False, 'project': True, 'stage': True, 'related': True},
        'office_management_payment': {'from_account': True, 'to_account': False, 'project': False, 'stage': False, 'related': True},
        'personal_management_payment': {'from_account': True, 'to_account': True, 'project': False, 'stage': False, 'related': False},
        'expense_general': {'from_account': True, 'to_account': False, 'project': True, 'stage': True, 'related': False},
        'project_income': {'from_account': True, 'to_account': True, 'project': True, 'stage': False, 'related': False},
        'party_receipt': {'from_account': True, 'to_account': True, 'project': False, 'stage': False, 'related': False},
        'client_payment': {'from_account': True, 'to_account': True, 'project': False, 'stage': False, 'related': False},
        'party_payment': {'from_account': True, 'to_account': True, 'project': False, 'stage': False, 'related': False},
        'advance_to_person': {'from_account': True, 'to_account': True, 'project': True, 'stage': True, 'related': True},
        'purchase': {'from_account': True, 'to_account': False, 'project': False, 'stage': False, 'related': True},
        'payroll': {'from_account': True, 'to_account': False, 'project': True, 'stage': True, 'related': True},
    }
    out['receive_from_project'] = dict(out['project_income'])
    out['receive_intra_company'] = dict(out['transfer'])
    out['receive_from_credit_debit'] = dict(out['party_receipt'])
    out['pay_to_project'] = dict(out['party_payment'])
    out['pay_intra_company'] = dict(out['transfer'])
    out['pay_to_credit_debit'] = dict(out['party_payment'])
    out['personal_payment'] = dict(out['personal_management_payment'])
    return out


def _account_entity_label(entity_type, entity_id):
    et = _normalize_related_entity_type(entity_type)
    eid = int(entity_id or 0)
    if not eid:
        return ''
    if et == 'worker':
        row = Worker.query.get(eid)
        return row.name if row else ''
    if et == 'supplier':
        row = Supplier.query.get(eid)
        return row.name if row else ''
    if et == 'subcontractor':
        row = Subcontractor.query.get(eid)
        return row.name if row else ''
    if et == 'project':
        row = Project.query.get(eid)
        return row.name if row else ''
    if et == 'office_staff':
        row = OfficeStaff.query.get(eid)
        return row.name if row else ''
    return ''


def _account_reference_links(txn_row):
    parts = []
    seen = set()
    def _push(label, url=''):
        k = (str(label or '').strip().lower(), str(url or '').strip())
        if (not k[0]) or (k in seen):
            return
        seen.add(k)
        parts.append({'label': label, 'url': url})
    if txn_row.project_id and txn_row.project:
        _push(f'Project: {txn_row.project.name}', url_for('hdc_project_detail', pid=txn_row.project_id))
    if txn_row.stage_id and txn_row.stage:
        _push(f'Stage: {txn_row.stage.name}', url_for('hdc_project_detail', pid=txn_row.project_id) if txn_row.project_id else '')
    et = _normalize_related_entity_type(txn_row.related_entity_type)
    eid = int(txn_row.related_entity_id or 0)
    if et == 'worker' and eid:
        nm = _account_entity_label(et, eid) or f'Worker #{eid}'
        _push(f'Worker: {nm}', url_for('hdc_worker_ledger', wid=eid))
    elif et == 'supplier' and eid:
        nm = _account_entity_label(et, eid) or f'Supplier #{eid}'
        _push(f'Supplier: {nm}', url_for('hdc_purchase_v2_supplier_detail', supplier_id=eid))
    elif et == 'subcontractor' and eid:
        nm = _account_entity_label(et, eid) or f'Subcontractor #{eid}'
        _push(f'Subcontractor: {nm}', url_for('hdc_subcontractor_ledger', sid=eid))
    elif et == 'project' and eid:
        nm = _account_entity_label(et, eid) or f'Project #{eid}'
        _push(f'Project: {nm}', url_for('hdc_project_detail', pid=eid))
    elif et == 'office_staff' and eid:
        nm = _account_entity_label(et, eid) or f'Office Staff #{eid}'
        _push(f'Office Staff: {nm}', url_for('hdc_office_staff_ledger', sid=eid))
    elif (txn_row.party_name or '').strip():
        _push(f'Party: {txn_row.party_name}', '')
    if (txn_row.reference_id or '').strip():
        _push(f'Ref: {txn_row.reference_id}', '')
    _push(f'Txn #{txn_row.id}', '')
    return parts


def _account_ledger_rows(account_id):
    aid = int(account_id or 0)
    account = Account.query.get(aid) if aid else None
    if not account:
        return []
    rows = (AccountTransaction.query
            .filter(or_(AccountTransaction.from_account_id == aid, AccountTransaction.to_account_id == aid))
            .all())
    rows = sorted(
        rows,
        key=lambda r: (
            (r.created_at or datetime.combine((r.date or _pkt_today()), datetime.min.time())),
            int(r.id or 0)
        )
    )
    running = float(account.opening_balance or 0.0)
    out = []
    for r in rows:
        amt = float(r.amount or 0.0)
        debit = (amt if int(r.to_account_id or 0) == aid else 0.0)
        credit = (amt if int(r.from_account_id or 0) == aid else 0.0)
        src_name = (r.from_account.name if getattr(r, 'from_account', None) else f'Account #{int(r.from_account_id or 0)}')
        dst_name = (
            (r.to_account.name if getattr(r, 'to_account', None) else '')
            or (r.party_name or '').strip()
            or 'Off-ledger Party'
        )
        if not r.is_void:
            running += debit
            running -= credit
        ts = (r.created_at or datetime.combine((r.date or _pkt_today()), datetime.min.time()))
        out.append({
            '_hdc_entity': 'hdc_account_txn',
            'id': int(r.id),
            'timestamp': ts,
            'tx_type': str(r.type or '').replace('_', ' ').title(),
            'current_amount': float(amt or 0.0),
            'flow': f'{src_name} -> {dst_name}',
            'debit': float(debit or 0.0),
            'credit': float(credit or 0.0),
            'running_balance': float(running),
            'references': _account_reference_links(r),
            'is_void': bool(r.is_void)
        })
    return out


def _account_reverse_transaction_group(txn_id, reason=''):
    row = AccountTransaction.query.get(int(txn_id or 0))
    if not row:
        return False, 'Transaction not found.', 0
    rows = _account_txn_group_rows(row)
    if not rows:
        return False, 'Transaction group not found.', 0
    if any(bool(r.is_void) for r in rows):
        return False, 'Voided transactions cannot be reversed.', 0
    if any((r.source_type or '').strip() and _account_source_type_base(r.source_type) != 'account_reversal' for r in rows):
        return False, 'This entry is synced from another module. Reverse it in its source module.', 0
    reverse_group_id = uuid4().hex
    created = 0
    try:
        for idx, r in enumerate(rows, start=1):
            dest_account_id = int(r.to_account_id or 0)
            src_account_id = int(r.from_account_id or 0)
            if not src_account_id:
                continue
            if not dest_account_id:
                party = _accounts_party_account((r.party_name or 'External Parties'), 'person')
                dest_account_id = int(party.id or 0) if party else 0
            if not dest_account_id:
                return False, 'Unable to resolve reversal counter-account.', created
            rev_note = f"Reversal of txn #{r.id}"
            if reason:
                rev_note = f"{rev_note} | {reason}"
            if r.note:
                rev_note = f"{rev_note} | {r.note}"
            rev = AccountTransaction(
                date=_pkt_today(),
                amount=float(r.amount or 0.0),
                type=(r.type or 'transfer'),
                from_account_id=dest_account_id,
                to_account_id=src_account_id,
                executed_by_account_id=dest_account_id,
                project_id=(r.project_id if r.project_id else None),
                stage_id=(r.stage_id if r.stage_id else None),
                related_entity_type=(r.related_entity_type or None),
                related_entity_id=(int(r.related_entity_id) if r.related_entity_id else None),
                party_name=(r.party_name or None),
                category=(r.category or 'transfer'),
                note=rev_note[:400],
                reference_id=(f"REV-{r.id}-{(r.reference_id or '').strip()}".strip('-')[:120]),
                group_id=reverse_group_id,
                source_type='account_reversal',
                source_id=None,
                is_void=False,
                created_at=_pkt_now_naive()
            )
            db.session.add(rev)
            created += 1
        if created <= 0:
            db.session.rollback()
            return False, 'No reversal rows were created.', 0
        db.session.commit()
        return True, '', created
    except Exception as ex:
        db.session.rollback()
        return False, f'Reversal save failed: {ex}', created


def _account_pending_snapshot(tx_type, related_entity_type=None, related_entity_id=None, project_id=None, stage_id=None):
    tx_type = _normalize_account_tx_type(tx_type)
    et = _normalize_related_entity_type(related_entity_type)
    eid = int(related_entity_id or 0)
    pid = int(project_id or 0)
    sid = int(stage_id or 0)
    snap = {
        'kind': '',
        'entity_type': et,
        'entity_id': (eid or None),
        'pending': 0.0,
        'paid': 0.0,
        'total': 0.0,
        'message': '',
    }
    if tx_type in ('expense_material', 'purchase') and et == 'supplier' and eid:
        debit = float(db.session.query(func.coalesce(func.sum(SupplierLedger.amount), 0.0))
                      .filter(
                          SupplierLedger.supplier_id == eid,
                          SupplierLedger.is_void == False,
                          func.lower(SupplierLedger.entry_type) == 'debit'
                      ).scalar() or 0.0)
        credit = float(db.session.query(func.coalesce(func.sum(SupplierLedger.amount), 0.0))
                       .filter(
                           SupplierLedger.supplier_id == eid,
                           SupplierLedger.is_void == False,
                           func.lower(SupplierLedger.entry_type) == 'credit'
                       ).scalar() or 0.0)
        pending = max(0.0, debit - credit)
        snap.update({
            'kind': 'supplier_payable',
            'pending': pending,
            'paid': credit,
            'total': debit,
            'message': f'Supplier payable pending: {pending:,.2f} PKR',
        })
        return snap
    if tx_type in ('expense_wage', 'payroll', 'advance_to_person') and et == 'worker' and eid:
        ws = _worker_payable_snapshot(eid)
        pending = float(ws.get('payable') or 0.0)
        paid = float(ws.get('paid') or 0.0)
        total = float(ws.get('earned') or 0.0)
        snap.update({
            'kind': 'worker_payable',
            'pending': pending,
            'paid': paid,
            'total': total,
            'message': f'Worker payable pending: {pending:,.2f} PKR',
        })
        return snap
    if tx_type == 'office_management_payment' and et == 'office_staff' and eid:
        staff = OfficeStaff.query.get(eid)
        if not staff:
            return snap
        ss = _office_staff_ledger_snapshot(eid)
        pending = max(0.0, float(ss.get('balance') or 0.0))
        paid = float(ss.get('paid') or 0.0) + float(ss.get('tip') or 0.0)
        total = float(ss.get('total_earned') or 0.0)
        snap.update({
            'kind': 'office_staff_payable',
            'pending': pending,
            'paid': paid,
            'total': total,
            'message': f'Office staff payable pending: {pending:,.2f} PKR',
        })
        return snap
    if tx_type == 'expense_subcontractor' and et == 'subcontractor' and eid:
        sub = Subcontractor.query.get(eid)
        if not sub:
            return snap
        if sid:
            st = Stage.query.get(sid)
            if st and (not pid or int(st.project_id or 0) == int(pid)):
                sss = _subcontract_stage_snapshot(sub, st)
                pending = max(0.0, float(sss.get('balance') or 0.0))
                paid = float(sss.get('paid') or 0.0)
                total = float(sss.get('payable') or 0.0)
                snap.update({
                    'kind': 'subcontract_stage_payable',
                    'pending': pending,
                    'paid': paid,
                    'total': total,
                    'message': f'Subcontractor stage pending: {pending:,.2f} PKR',
                })
                return snap
        pending = max(0.0, float(sub.payable_balance or 0.0))
        paid = float(sub.total_cleared or 0.0)
        total = float(sub.payable_amount or 0.0)
        snap.update({
            'kind': 'subcontract_payable',
            'pending': pending,
            'paid': paid,
            'total': total,
            'message': f'Subcontractor pending: {pending:,.2f} PKR',
        })
        return snap
    if tx_type in ('project_income', 'client_payment', 'party_receipt') and pid:
        p = Project.query.get(pid)
        if p:
            pending = max(0.0, float(p.remaining_receivable or 0.0))
            paid = float(p.total_received or 0.0)
            total = float(p.owner_contract_value or 0.0)
            snap.update({
                'kind': 'project_receivable',
                'entity_type': 'project',
                'entity_id': p.id,
                'pending': pending,
                'paid': paid,
                'total': total,
                'message': f'Project receivable pending: {pending:,.2f} PKR',
            })
        return snap
    return snap


def _compute_excess_split(amount, pending, requested_tip=0.0, requested_advance=0.0):
    amount = max(0.0, float(amount or 0.0))
    pending = max(0.0, float(pending or 0.0))
    tip_req = max(0.0, float(requested_tip or 0.0))
    adv_req = max(0.0, float(requested_advance or 0.0))
    payment_part = min(amount, pending)
    excess_part = max(0.0, amount - payment_part)
    if excess_part <= 1e-6:
        return True, '', payment_part, 0.0, 0.0, 0.0
    if tip_req > excess_part + 1e-6:
        return False, f'Tip amount cannot exceed excess ({excess_part:,.2f} PKR).', 0.0, 0.0, 0.0, excess_part
    if tip_req <= 1e-6 and adv_req <= 1e-6:
        adv_req = excess_part
    total_split = tip_req + adv_req
    if abs(total_split - excess_part) > 0.01:
        return False, f'Tip + Advance must equal excess ({excess_part:,.2f} PKR).', 0.0, 0.0, 0.0, excess_part
    return True, '', payment_part, tip_req, adv_req, excess_part


def _create_accounts_transaction_with_sync(payload):
    data = dict(payload or {})
    tx_type = _normalize_account_tx_type((data.get('type') or data.get('transaction_type') or ''))
    data['type'] = tx_type
    rel_type = _normalize_related_entity_type(data.get('related_entity_type'))
    rel_id = _payload_int(data, 'related_entity_id')
    project_id = _payload_int(data, 'project_id')
    stage_id = _payload_int(data, 'stage_id')
    expense_category_id = _payload_int(data, 'expense_category_id')
    amount = max(0.0, _flt(data.get('amount'), 0.0))

    source_type = None
    source_id = None
    split_txn_payloads = []

    # Duplicate guard (only for purely manual entries — synced flows use source_type/id below).
    try:
        from_acc_int = int(data.get('from_account_id') or 0) or None
    except Exception:
        from_acc_int = None
    try:
        to_acc_int = int(data.get('to_account_id') or 0) or None
    except Exception:
        to_acc_int = None
    tx_date_norm = _parse_date(data.get('date'))
    if _has_recent_duplicate(
        AccountTransaction,
        type=tx_type,
        amount=float(amount),
        date=tx_date_norm,
        from_account_id=from_acc_int,
        to_account_id=to_acc_int,
        project_id=(project_id or None),
        stage_id=(stage_id or None),
        related_entity_type=(rel_type or None),
        related_entity_id=(rel_id or None),
        party_name=(data.get('party_name') or None),
        reference_id=(data.get('reference_id') or None)
    ):
        return False, 'Duplicate transaction prevented (same values submitted within a few seconds).', []

    expected_rel = _account_expected_related_type(tx_type)
    if expected_rel and (not rel_type):
        rel_type = expected_rel
        data['related_entity_type'] = expected_rel

    # Hard-bind party_name to the selected entity's canonical name. This is the
    # single most important guard against the "1 person but 2 ledgers" class of
    # mistake: if the user picked Worker A but typed a different name in the
    # party_name field, we silently overwrite with the canonical name so the
    # AccountTransaction row, the LabourLedger row, and the entity's account
    # cannot disagree.
    if rel_type and rel_id:
        canonical_lbl = _account_entity_label(rel_type, rel_id)
        if canonical_lbl:
            typed_party = (data.get('party_name') or '').strip()
            if typed_party and _normalize_name_ci(typed_party).lower() != _normalize_name_ci(canonical_lbl).lower():
                # Refuse outright — surface the conflict so the user fixes it.
                return False, (
                    f'Party name "{typed_party}" does not match selected '
                    f'{rel_type} "{canonical_lbl}". Clear the party name field or pick the right entity.'
                ), []
            data['party_name'] = canonical_lbl
    try:
        if tx_type == 'project_income':
            if not int(project_id or 0):
                return False, 'Project is required for Project Income receipt.', []
            prj = Project.query.get(project_id)
            if not prj:
                return False, 'Valid project is required for Project Income receipt.', []
            client_name = _normalize_name_ci(prj.client or '')
            if not client_name:
                return False, 'Selected project has no client name. Update project client first.', []
            client_acc = _accounts_party_account(client_name, 'client')
            if not client_acc:
                return False, 'Unable to resolve project client account.', []
            data['from_account_id'] = int(client_acc.id)
            data['executed_by_account_id'] = int(client_acc.id)
            if not int(_payload_int(data, 'to_account_id') or 0):
                return False, 'Select Receive In (company account).', []
            data['related_entity_type'] = 'project'
            data['related_entity_id'] = int(prj.id)
            if not (data.get('party_name') or '').strip():
                data['party_name'] = client_name

        if tx_type in ('expense_material', 'purchase') and (rel_type != 'supplier' or not rel_id):
            return False, 'Select supplier in Related Entity for material/purchase payment sync.', []
        if tx_type in ('expense_wage', 'payroll', 'advance_to_person') and (rel_type != 'worker' or not rel_id):
            return False, 'Select worker in Related Entity for wage/payroll sync.', []
        if tx_type == 'expense_subcontractor' and (rel_type != 'subcontractor' or not rel_id):
            return False, 'Select subcontractor in Related Entity for subcontractor payment sync.', []
        if _account_requires_scope_tags(tx_type):
            if not int(project_id or 0):
                return False, 'Project is mandatory for outgoing payments.', []
            if not int(stage_id or 0):
                return False, 'Stage is mandatory for outgoing payments.', []

        if tx_type in ('expense_material', 'purchase'):
            if rel_type == 'supplier' and rel_id:
                pending = _account_pending_snapshot(tx_type, rel_type, rel_id, project_id=project_id, stage_id=stage_id)
                pend = float(pending.get('pending') or 0.0)
                s = Supplier.query.get(rel_id)
                if not s or s.is_void:
                    return False, 'Valid supplier is required.', []
                tx_date = _parse_date(data.get('date'))
                base_note = (data.get('note') or '').strip()
                requested_tip = max(0.0, _flt(data.get('excess_tip_amount'), 0.0))
                requested_advance = max(0.0, _flt(data.get('excess_advance_amount'), 0.0))
                ok_split, msg_split, payment_part, tip_part, advance_part, _ = _compute_excess_split(
                    amount, pend, requested_tip=requested_tip, requested_advance=requested_advance
                )
                if not ok_split:
                    return False, msg_split, []
                payable_part = payment_part + advance_part

                def _append_supplier_credit_row(ref_type, component_amount, component_note):
                    if component_amount <= 1e-6:
                        return
                    row = SupplierLedger(
                        supplier_id=s.id,
                        entry_type='credit',
                        amount=float(component_amount),
                        reference_type=ref_type,
                        reference_id=None,
                        note=component_note,
                        is_void=False,
                        created_at=_pkt_now_naive()
                    )
                    db.session.add(row)
                    db.session.flush()
                    split_txn_payloads.append({
                        'date': (tx_date.isoformat() if tx_date else (data.get('date') or _pkt_today().isoformat())),
                        'type': 'purchase',
                        'amount': float(component_amount),
                        'from_account_id': data.get('from_account_id'),
                        'to_account_id': data.get('to_account_id'),
                        'executed_by_account_id': data.get('executed_by_account_id') or data.get('from_account_id'),
                        'project_id': (project_id or None),
                        'stage_id': (stage_id or None),
                        'related_entity_type': 'supplier',
                        'related_entity_id': s.id,
                        'party_name': data.get('party_name') or s.name,
                        'category': 'purchase',
                        'note': component_note,
                        'reference_id': f'supplier_ledger#{row.id}',
                        'source_type': f'supplier_credit_{ref_type}',
                        'source_id': int(row.id),
                    })

                pay_note = (base_note or f'Accounts payment to supplier {s.name}')
                if payment_part <= 1e-6 and advance_part > 1e-6:
                    pay_note = (base_note + ' | ' if base_note else '') + f'Auto advance from overpayment for {s.name}'
                _append_supplier_credit_row('payment', payable_part, pay_note)
                _append_supplier_credit_row('tip', tip_part, (base_note + ' | ' if base_note else '') + f'Tip for supplier {s.name}')

        if tx_type in ('expense_wage', 'payroll', 'advance_to_person'):
            if rel_type == 'worker' and rel_id:
                pending = _account_pending_snapshot(tx_type, rel_type, rel_id, project_id=project_id, stage_id=stage_id)
                pend = float(pending.get('pending') or 0.0)
                w = Worker.query.get(rel_id)
                if not w or (not w.active_status):
                    return False, 'Valid active worker is required.', []
                # Payroll allows empty to_account_id, but split overpayment can create
                # advance_to_person rows that strictly require a worker party account.
                worker_to_account_id = _payload_int(data, 'to_account_id') or None
                if not worker_to_account_id:
                    worker_label = _normalize_name_ci(w.name) or f'Worker #{w.id}'
                    worker_acc = _accounts_party_account(worker_label, 'person')
                    worker_to_account_id = int(worker_acc.id) if worker_acc else None

                tx_date = _parse_date(data.get('date'))
                base_note = (data.get('note') or '').strip()
                settle_shortfall = (str(data.get('settle_shortfall') or '').strip().lower() in ('1', 'true', 'on', 'yes'))

                if tx_type == 'advance_to_person':
                    row = LabourLedger(
                        worker_id=w.id,
                        entry_type='advance',
                        amount=amount,
                        date=tx_date,
                        project_id=(project_id or None),
                        stage_id=(stage_id or None),
                        activity_at=_pkt_now_naive(),
                        notes=(base_note or f'Accounts advance for {w.name}')
                    )
                    db.session.add(row)
                    db.session.flush()
                    source_type = 'labour_ledger_advance'
                    source_id = int(row.id)
                else:
                    requested_tip = max(0.0, _flt(data.get('excess_tip_amount'), 0.0))
                    requested_advance = max(0.0, _flt(data.get('excess_advance_amount'), 0.0))
                    ok_split, msg_split, payment_part, tip_part, advance_part, _ = _compute_excess_split(
                        amount, pend, requested_tip=requested_tip, requested_advance=requested_advance
                    )
                    if not ok_split:
                        return False, msg_split, []
                    settlement_part = 0.0
                    if settle_shortfall and payment_part < pend:
                        settlement_part = max(0.0, pend - payment_part)

                    if (tip_part > 0 or settlement_part > 0):
                        if (not project_id) or (not stage_id):
                            return False, 'For tip or shortfall settlement, select both project and stage.', []
                        stg = Stage.query.get(stage_id)
                        if (not stg) or int(stg.project_id or 0) != int(project_id):
                            return False, 'Selected stage does not belong to selected project.', []

                    def _append_worker_cash_txn(entry_type, component_amount, component_note):
                        if component_amount <= 1e-6:
                            return
                        if _has_recent_duplicate(
                            LabourLedger,
                            worker_id=w.id,
                            entry_type=entry_type,
                            amount=float(component_amount),
                            date=tx_date,
                            notes=component_note
                        ):
                            return  # Skip duplicate
                        row = LabourLedger(
                            worker_id=w.id,
                            entry_type=entry_type,
                            amount=float(component_amount),
                            date=tx_date,
                            project_id=(project_id or None),
                            stage_id=(stage_id or None),
                            activity_at=_pkt_now_naive(),
                            notes=component_note
                        )
                        db.session.add(row)
                        db.session.flush()
                        split_txn_payloads.append({
                            'date': (tx_date.isoformat() if tx_date else (data.get('date') or _pkt_today().isoformat())),
                            'type': ('advance_to_person' if entry_type == 'advance' else 'payroll'),
                            'amount': float(component_amount),
                            'from_account_id': data.get('from_account_id'),
                            'to_account_id': worker_to_account_id,
                            'executed_by_account_id': data.get('executed_by_account_id') or data.get('from_account_id'),
                            'project_id': (project_id or None),
                            'stage_id': (stage_id or None),
                            'related_entity_type': 'worker',
                            'related_entity_id': w.id,
                            'party_name': data.get('party_name') or w.name,
                            'category': ('advance' if entry_type == 'advance' else 'payroll'),
                            'note': component_note,
                            'reference_id': f'labour_ledger#{row.id}',
                            'source_type': f'labour_ledger_{entry_type}',
                            'source_id': int(row.id),
                        })

                    _append_worker_cash_txn('payment', payment_part, (base_note or f'Accounts payment for {w.name}'))

                    if tip_part > 1e-6:
                        # Bug fix (2026-04-25): the tip ledger row used to be
                        # tagged only with TIP_WORKER_ID, but the periodic
                        # reconciler matches by TIP_EXPENSE_ID — so when the
                        # reconciler ran it could not see this row and wrote a
                        # second tip ledger entry for the same cash event,
                        # leaving the worker's balance over-paid by the tip.
                        # We now flush the Expense immediately to capture its
                        # id, then embed TIP_EXPENSE_ID:<exp.id> in BOTH the
                        # Expense.remarks and the LabourLedger row's notes so
                        # the reconciler's match-by-id check finds it.
                        tip_note_base = (base_note + ' | ' if base_note else '') + f'Tip for {w.name} | TIP_WORKER_ID:{w.id}'
                        tip_cat = _ensure_expense_category('Tip')
                        tip_exp = Expense(
                            project_id=(project_id or None),
                            stage_id=(stage_id or None),
                            tip_worker_id=w.id,
                            category_id=(tip_cat.id if tip_cat else None),
                            amount=float(tip_part),
                            date=tx_date,
                            activity_at=_activity_at_for(tx_date),
                            remarks=tip_note_base
                        )
                        db.session.add(tip_exp)
                        db.session.flush()
                        tip_note = tip_note_base + f' | TIP_EXPENSE_ID:{tip_exp.id}'
                        tip_exp.remarks = tip_note
                        _append_worker_cash_txn('tip', tip_part, tip_note)

                    _append_worker_cash_txn('advance', advance_part, (base_note + ' | ' if base_note else '') + f'Auto advance from overpayment for {w.name}')

                    if settlement_part > 1e-6:
                        settlement_note = (base_note + ' | ' if base_note else '') + f'Settlement shortfall for {w.name} | SETTLE_WORKER_ID:{w.id}'
                        settlement_cat = _ensure_expense_category('Settlement')
                        db.session.add(Expense(
                            project_id=(project_id or None),
                            stage_id=(stage_id or None),
                            category_id=(settlement_cat.id if settlement_cat else None),
                            amount=-float(settlement_part),
                            date=tx_date,
                            activity_at=_activity_at_for(tx_date),
                            remarks=settlement_note
                        ))
                        db.session.add(LabourLedger(
                            worker_id=w.id,
                            entry_type='settlement',
                            amount=float(settlement_part),
                            date=tx_date,
                            project_id=(project_id or None),
                            stage_id=(stage_id or None),
                            activity_at=_pkt_now_naive(),
                            notes=settlement_note
                        ))

        if tx_type == 'expense_subcontractor':
            if rel_type == 'subcontractor' and rel_id:
                pending = _account_pending_snapshot(tx_type, rel_type, rel_id, project_id=project_id, stage_id=stage_id)
                pend = float(pending.get('pending') or 0.0)
                s = Subcontractor.query.get(rel_id)
                if not s:
                    return False, 'Valid subcontractor is required.', []
                tx_date = _parse_date(data.get('date'))
                base_note = (data.get('note') or '').strip()
                settle_shortfall = (str(data.get('settle_shortfall') or '').strip().lower() in ('1', 'true', 'on', 'yes'))
                requested_tip = max(0.0, _flt(data.get('excess_tip_amount'), 0.0))
                requested_advance = max(0.0, _flt(data.get('excess_advance_amount'), 0.0))
                ok_split, msg_split, payment_part, tip_part, advance_part, _ = _compute_excess_split(
                    amount, pend, requested_tip=requested_tip, requested_advance=requested_advance
                )
                if not ok_split:
                    return False, msg_split, []
                settlement_part = 0.0
                if settle_shortfall and payment_part < pend:
                    settlement_part = max(0.0, pend - payment_part)
                if (tip_part > 0 or settlement_part > 0) and ((not project_id) or (not stage_id)):
                    return False, 'For subcontractor tip/shortfall settlement, select both project and stage.', []
                if stage_id:
                    stg = Stage.query.get(stage_id)
                    if (not stg) or int(stg.project_id or 0) != int(project_id):
                        return False, 'Selected stage does not belong to selected project.', []

                def _append_subcontract_cash(entry_type, component_amount, component_note):
                    if component_amount <= 1e-6:
                        return
                    row = SubcontractPayment(
                        subcontractor_id=s.id,
                        project_id=(project_id or None),
                        stage_id=(stage_id or None),
                        entry_type=entry_type,
                        amount=float(component_amount),
                        date=tx_date,
                        activity_at=_pkt_now_naive(),
                        notes=component_note
                    )
                    db.session.add(row)
                    db.session.flush()
                    split_txn_payloads.append({
                        'date': (tx_date.isoformat() if tx_date else (data.get('date') or _pkt_today().isoformat())),
                        'type': 'expense_subcontractor',
                        'amount': float(component_amount),
                        'from_account_id': data.get('from_account_id'),
                        'to_account_id': data.get('to_account_id'),
                        'executed_by_account_id': data.get('executed_by_account_id') or data.get('from_account_id'),
                        'project_id': (project_id or None),
                        'stage_id': (stage_id or None),
                        'related_entity_type': 'subcontractor',
                        'related_entity_id': s.id,
                        'party_name': data.get('party_name') or s.name,
                        'category': 'expense',
                        'note': component_note,
                        'reference_id': f'subcontract_payment#{row.id}',
                        'source_type': f'subcontract_payment_{entry_type}',
                        'source_id': int(row.id),
                    })
                    _log_subcontract_event(
                        sub=s,
                        event_type=('tip' if entry_type == 'tip' else 'payment'),
                        amount=float(component_amount),
                        notes=component_note or 'Subcontract payment recorded',
                        project_id=(project_id or None),
                        stage_id=(stage_id or None)
                    )

                _append_subcontract_cash('payment', payment_part, (base_note or f'Accounts payment to subcontractor {s.name}'))
                _append_subcontract_cash('payment', advance_part, (base_note + ' | ' if base_note else '') + f'Auto advance from overpayment for {s.name}')
                if tip_part > 1e-6:
                    tip_note = (base_note + ' | ' if base_note else '') + f'Tip for subcontractor {s.name} | TIP_SUBCONTRACTOR_ID:{s.id}'
                    tip_cat = _ensure_expense_category('Tip')
                    db.session.add(Expense(
                        project_id=(project_id or None),
                        stage_id=(stage_id or None),
                        category_id=(tip_cat.id if tip_cat else None),
                        amount=float(tip_part),
                        date=tx_date,
                        activity_at=_activity_at_for(tx_date),
                        remarks=tip_note
                    ))
                    _append_subcontract_cash('tip', tip_part, tip_note)

                if settlement_part > 1e-6:
                    settlement_note = (base_note + ' | ' if base_note else '') + f'Subcontract settlement shortfall for {s.name} | SETTLE_SUBCONTRACTOR_ID:{s.id}'
                    settlement_cat = _ensure_expense_category('Settlement')
                    db.session.add(SubcontractPayment(
                        subcontractor_id=s.id,
                        project_id=(project_id or None),
                        stage_id=(stage_id or None),
                        entry_type='settlement',
                        amount=float(settlement_part),
                        date=tx_date,
                        activity_at=_pkt_now_naive(),
                        notes=settlement_note
                    ))
                    db.session.add(Expense(
                        project_id=(project_id or None),
                        stage_id=(stage_id or None),
                        category_id=(settlement_cat.id if settlement_cat else None),
                        amount=-float(settlement_part),
                        date=tx_date,
                        activity_at=_activity_at_for(tx_date),
                        remarks=settlement_note
                    ))
                    _log_subcontract_event(
                        sub=s,
                        event_type='settlement',
                        amount=float(settlement_part),
                        notes='Shortfall settled from Accounts',
                        project_id=(project_id or None),
                        stage_id=(stage_id or None)
                    )

        if tx_type == 'office_management_payment':
            office_target = (data.get('office_target') or '').strip().lower()
            if office_target not in ('staff', 'expense'):
                office_target = ('staff' if rel_type == 'office_staff' and rel_id else 'expense')
            tx_date = _parse_date(data.get('date'))
            base_note = (data.get('note') or '').strip()
            if office_target == 'staff':
                if rel_type != 'office_staff' or (not rel_id):
                    return False, 'Select office staff in Related Entity for office staff payment.', []
                s = OfficeStaff.query.get(rel_id)
                if not s:
                    return False, 'Valid office staff is required.', []
                pending = _account_pending_snapshot(tx_type, rel_type, rel_id)
                pend = float(pending.get('pending') or 0.0)
                requested_tip = max(0.0, _flt(data.get('excess_tip_amount'), 0.0))
                requested_advance = max(0.0, _flt(data.get('excess_advance_amount'), 0.0))
                ok_split, msg_split, payment_part, tip_part, advance_part, _ = _compute_excess_split(
                    amount, pend, requested_tip=requested_tip, requested_advance=requested_advance
                )
                if not ok_split:
                    return False, msg_split, []

                staff_to_account_id = _payload_int(data, 'to_account_id') or None
                if not staff_to_account_id:
                    staff_acc = _accounts_party_account(_normalize_name_ci(s.name) or f'Office Staff #{s.id}', 'person')
                    staff_to_account_id = int(staff_acc.id) if staff_acc else None

                def _append_office_staff_cash(entry_type, component_amount, component_note):
                    if component_amount <= 1e-6:
                        return
                    row = OfficeStaffLedger(
                        staff_id=s.id,
                        date=tx_date,
                        entry_type=entry_type,
                        amount=float(component_amount),
                        notes=component_note,
                        activity_at=_activity_at_for(tx_date)
                    )
                    db.session.add(row)
                    db.session.flush()
                    _sync_office_staff_expense_from_ledger(s, row)
                    split_txn_payloads.append({
                        'date': (tx_date.isoformat() if tx_date else (data.get('date') or _pkt_today().isoformat())),
                        'type': 'office_management_payment',
                        'amount': float(component_amount),
                        'from_account_id': data.get('from_account_id'),
                        'to_account_id': staff_to_account_id,
                        'executed_by_account_id': data.get('executed_by_account_id') or data.get('from_account_id'),
                        'project_id': None,
                        'stage_id': None,
                        'related_entity_type': 'office_staff',
                        'related_entity_id': s.id,
                        'party_name': data.get('party_name') or s.name,
                        'category': 'expense',
                        'note': component_note,
                        'reference_id': f'office_staff_ledger#{row.id}',
                        'source_type': f'office_staff_ledger_{entry_type}',
                        'source_id': int(row.id),
                    })

                _append_office_staff_cash('payment', payment_part, (base_note or f'Office salary payment for {s.name}'))
                if tip_part > 1e-6:
                    tip_note = (base_note + ' | ' if base_note else '') + f'Tip for office staff {s.name} | TIP_OFFICE_STAFF_ID:{s.id}'
                    _append_office_staff_cash('tip', tip_part, tip_note)
                _append_office_staff_cash('advance', advance_part, (base_note + ' | ' if base_note else '') + f'Auto advance from overpayment for {s.name}')
            else:
                expense_kind = _normalize_expense_category_name(data.get('office_expense_category')) or 'Office General'
                exp = OfficeExpense(
                    date=tx_date,
                    category=expense_kind,
                    amount=float(amount),
                    remarks=((base_note or data.get('party_name') or f'Office expense via Accounts: {expense_kind}') or '').strip(),
                    is_void=False,
                    activity_at=_activity_at_for(tx_date)
                )
                db.session.add(exp)
                db.session.flush()
                source_type = 'office_expense'
                source_id = int(exp.id)
                data['party_name'] = (data.get('party_name') or expense_kind)
                data['related_entity_type'] = None
                data['related_entity_id'] = None

        if tx_type == 'personal_management_payment':
            tx_date = _parse_date(data.get('date'))
            base_note = (data.get('note') or '').strip()
            beneficiary_name = _normalize_name_ci(data.get('party_name') or '')
            to_account_id = _payload_int(data, 'to_account_id')
            to_acc = (Account.query.get(to_account_id) if to_account_id else None)
            if (not beneficiary_name) and to_acc and (not bool(getattr(to_acc, 'is_void', False))):
                beneficiary_name = _normalize_name_ci(to_acc.name or '')
            if not beneficiary_name:
                beneficiary_name = 'Self'
            if not to_account_id:
                ben_acc = _accounts_party_account(beneficiary_name, 'person')
                if ben_acc and ben_acc.id:
                    to_account_id = int(ben_acc.id)
                    data['to_account_id'] = to_account_id
            if _has_recent_duplicate(
                PersonalExpense,
                beneficiary_name=beneficiary_name,
                date=tx_date,
                amount=float(amount),
                category='Accounts Payment',
                remarks=(base_note or f'Accounts personal payment to {beneficiary_name}')
            ):
                return False, 'Duplicate personal expense entry prevented (same values submitted too quickly).', []
            personal_row = PersonalExpense(
                beneficiary_name=beneficiary_name,
                beneficiary_type='other',
                date=tx_date,
                category='Accounts Payment',
                amount=float(amount),
                remarks=(base_note or f'Accounts personal payment to {beneficiary_name}'),
                is_void=False,
                activity_at=_activity_at_for(tx_date)
            )
            db.session.add(personal_row)
            db.session.flush()
            source_type = 'personal_expense'
            source_id = int(personal_row.id)
            data['party_name'] = beneficiary_name
            data['related_entity_type'] = None
            data['related_entity_id'] = None

        if tx_type == 'expense_general':
            tx_date = _parse_date(data.get('date'))
            cat = _ensure_expense_category_by_id(expense_category_id)
            if (not cat) or (not bool(getattr(cat, 'active_status', True))):
                return False, 'Select a valid active Expense Category for General Expense.', []
            exp = Expense(
                project_id=(project_id or None),
                stage_id=(stage_id or None),
                category_id=int(cat.id),
                amount=float(amount),
                date=tx_date,
                activity_at=_activity_at_for(tx_date),
                remarks=((data.get('note') or data.get('party_name') or f'Accounts general expense: {cat.name}') or '').strip()
            )
            db.session.add(exp)
            db.session.flush()
            source_type = 'expense'
            source_id = int(exp.id)

        if tx_type in ('project_income', 'client_payment'):
            # Mirror received money into Project module owner receipts when project context exists.
            if project_id:
                prj = Project.query.get(project_id)
                if not prj:
                    return False, 'Valid project is required for owner receipt sync.', []
                pending = _account_pending_snapshot('project_income', 'project', project_id, project_id=project_id, stage_id=stage_id)
                pend = float(pending.get('pending') or 0.0)
                if pend > 0 and amount > (pend + 1e-6):
                    return False, f'Amount exceeds project receivable pending ({pend:,.2f} PKR).', []
                row = OwnerPayment(
                    project_id=prj.id,
                    amount=amount,
                    date=_parse_date(data.get('date')),
                    received_to_account_id=_payload_int(data, 'to_account_id') or None,
                    remarks=(data.get('note') or data.get('party_name') or f'Accounts receipt for {prj.name}'),
                    activity_at=_pkt_now_naive(),
                )
                db.session.add(row)
                db.session.flush()
                source_type = 'owner_payment'
                source_id = int(row.id)

        if split_txn_payloads:
            created = []
            for p in split_txn_payloads:
                ok, msg, rows = _create_account_transaction(p, commit=False)
                if not ok:
                    db.session.rollback()
                    return False, msg, []
                created.extend(rows or [])
            db.session.commit()
            return True, '', created

        if source_type and source_id:
            data['source_type'] = source_type
            data['source_id'] = source_id

        ok, msg, rows = _create_account_transaction(data, commit=False)
        if not ok:
            db.session.rollback()
            return False, msg, []
        db.session.commit()
        return True, '', rows
    except Exception as ex:
        db.session.rollback()
        return False, f'Transaction sync failed: {ex}', []


def _account_dashboard_kpis(date_from=None, date_to=None):
    bal = _account_balance_map()
    rows = _account_rows_active()
    by_id = {int(a.id): a for a in rows}
    company_owned_total = 0.0
    cash_total = 0.0
    bank_total = 0.0
    receivable = 0.0
    payable = 0.0
    for aid, value in bal.items():
        acc = by_id.get(int(aid))
        tp = (acc.type or '').strip().lower() if acc else ''
        if tp in _ACCOUNT_COMPANY_TYPES:
            company_owned_total += float(value or 0.0)
        if tp in ('company', 'cash'):
            cash_total += float(value or 0.0)
        if tp == 'bank':
            bank_total += float(value or 0.0)
        if tp in _ACCOUNT_PROJECT_FLOW_TYPES:
            if float(value or 0.0) > 0:
                receivable += float(value or 0.0)
    _, payable_total = _accounts_payable_breakdown_rows()
    payable = float(payable_total or 0.0)

    q = db.session.query(
        func.coalesce(func.sum(case((func.lower(AccountTransaction.category) == 'income', AccountTransaction.amount), else_=0.0)), 0.0),
        func.coalesce(func.sum(case((func.lower(AccountTransaction.category).in_(('expense', 'purchase', 'payroll', 'advance')), AccountTransaction.amount), else_=0.0)), 0.0)
    ).filter(AccountTransaction.is_void == False)
    if date_from:
        q = q.filter(AccountTransaction.date >= date_from)
    if date_to:
        q = q.filter(AccountTransaction.date <= date_to)
    income_total, expense_total = q.first() or (0.0, 0.0)
    income_total = float(income_total or 0.0)
    expense_total = float(expense_total or 0.0)

    sq = db.session.query(
        func.coalesce(func.sum(case((
            and_(
                func.lower(func.coalesce(AccountTransaction.category, '')).in_(('expense', 'purchase', 'payroll', 'advance')),
                or_(
                    func.lower(func.coalesce(AccountTransaction.type, '')).in_(('payroll', 'expense_wage', 'expense_subcontractor', 'advance_to_person', 'office_management_payment')),
                    func.lower(func.coalesce(AccountTransaction.category, '')).in_(('payroll', 'advance'))
                )
            ),
            AccountTransaction.amount
        ), else_=0.0)), 0.0),
        func.coalesce(func.sum(case((
            and_(
                func.lower(func.coalesce(AccountTransaction.category, '')).in_(('expense', 'purchase', 'payroll', 'advance')),
                func.lower(func.coalesce(AccountTransaction.type, '')) == 'expense_material'
            ),
            AccountTransaction.amount
        ), else_=0.0)), 0.0),
        func.coalesce(func.sum(case((
            and_(
                func.lower(func.coalesce(AccountTransaction.category, '')).in_(('expense', 'purchase', 'payroll', 'advance')),
                func.lower(func.coalesce(AccountTransaction.category, '')) == 'purchase'
            ),
            AccountTransaction.amount
        ), else_=0.0)), 0.0),
    ).filter(AccountTransaction.is_void == False)
    if date_from:
        sq = sq.filter(AccountTransaction.date >= date_from)
    if date_to:
        sq = sq.filter(AccountTransaction.date <= date_to)
    labour_expense_total, material_expense_total, purchased_expense_total = sq.first() or (0.0, 0.0, 0.0)
    labour_expense_total = float(labour_expense_total or 0.0)
    material_expense_total = float(material_expense_total or 0.0)
    purchased_expense_total = float(purchased_expense_total or 0.0)
    entries_q = db.session.query(func.count(AccountTransaction.id)).filter(AccountTransaction.is_void == False)
    if date_from:
        entries_q = entries_q.filter(AccountTransaction.date >= date_from)
    if date_to:
        entries_q = entries_q.filter(AccountTransaction.date <= date_to)
    entries_total = int(entries_q.scalar() or 0)

    return {
        'company_owned_total': float(company_owned_total),
        'cash_total': float(cash_total),
        'bank_total': float(bank_total),
        'receivable_total': float(receivable),
        'payable_total': float(payable),
        'received_total': income_total,
        'spent_total': expense_total,
        'labour_expense_total': labour_expense_total,
        'material_expense_total': material_expense_total,
        'purchased_expense_total': purchased_expense_total,
        'net_cashflow': float(income_total - expense_total),
        'entries_total': entries_total,
        'expense_total': expense_total,
        'income_total': income_total,
        'net_profit': float(income_total - expense_total),
    }


def _accounts_payable_breakdown_rows():
    rows = []
    total = 0.0

    workers = (Worker.query
               .filter(Worker.active_status == True)
               .order_by(Worker.name.asc(), Worker.id.asc())
               .all())
    for w in workers:
        snap = _worker_payable_snapshot(w.id)
        pending = float(snap.get('payable') or 0.0)
        if pending <= 1e-6:
            continue
        rows.append({
            'kind': 'Worker Wages',
            'name': (w.name or f'Worker #{w.id}'),
            'detail': (w.worker_code or f'W-{w.id}'),
            'amount': pending,
        })
        total += pending

    subs = Subcontractor.query.order_by(Subcontractor.name.asc(), Subcontractor.id.asc()).all()
    for s in subs:
        pending = max(0.0, float(s.payable_balance or 0.0))
        if pending <= 1e-6:
            continue
        scope = []
        if getattr(s, 'project', None) and getattr(s.project, 'name', None):
            scope.append(s.project.name)
        if getattr(s, 'stage_rel', None) and getattr(s.stage_rel, 'name', None):
            scope.append(s.stage_rel.name)
        rows.append({
            'kind': 'Subcontractor Wages',
            'name': (s.name or f'Subcontractor #{s.id}'),
            'detail': (' / '.join(scope) if scope else (s.subcontractor_code or f'SUB-{s.id}')),
            'amount': pending,
        })
        total += pending

    debit_map = dict(
        db.session.query(
            SupplierLedger.supplier_id,
            func.coalesce(func.sum(SupplierLedger.amount), 0.0)
        )
        .filter(
            SupplierLedger.is_void == False,
            func.lower(func.coalesce(SupplierLedger.entry_type, '')) == 'debit'
        )
        .group_by(SupplierLedger.supplier_id)
        .all()
    )
    credit_map = dict(
        db.session.query(
            SupplierLedger.supplier_id,
            func.coalesce(func.sum(SupplierLedger.amount), 0.0)
        )
        .filter(
            SupplierLedger.is_void == False,
            func.lower(func.coalesce(SupplierLedger.entry_type, '')) == 'credit'
        )
        .group_by(SupplierLedger.supplier_id)
        .all()
    )
    suppliers = (Supplier.query
                 .filter(Supplier.is_void == False)
                 .order_by(Supplier.name.asc(), Supplier.id.asc())
                 .all())
    for s in suppliers:
        debit = float(debit_map.get(s.id, 0.0) or 0.0)
        credit = float(credit_map.get(s.id, 0.0) or 0.0)
        pending = max(0.0, debit - credit)
        if pending <= 1e-6:
            continue
        rows.append({
            'kind': 'Purchase Pending',
            'name': (s.name or f'Supplier #{s.id}'),
            'detail': (s.phone or '-'),
            'amount': pending,
        })
        total += pending

    rows = sorted(rows, key=lambda r: float(r.get('amount') or 0.0), reverse=True)
    return rows, float(total)


def _account_kpi_detail_context(metric, date_from=None, date_to=None, payable_head='', spent_head=''):
    metric = (metric or '').strip().lower()
    kpis = _account_dashboard_kpis(date_from=date_from, date_to=date_to)
    accounts = _list_accounts_with_balances()
    company_types = _ACCOUNT_COMPANY_TYPES

    def _tx_rows_for_categories(categories):
        q = AccountTransaction.query.filter(AccountTransaction.is_void == False)
        if date_from:
            q = q.filter(AccountTransaction.date >= date_from)
        if date_to:
            q = q.filter(AccountTransaction.date <= date_to)
        cats = [str(c or '').strip().lower() for c in (categories or []) if str(c or '').strip()]
        if cats:
            q = q.filter(func.lower(func.coalesce(AccountTransaction.category, '')).in_(tuple(cats)))
        return q.order_by(AccountTransaction.date.desc(), AccountTransaction.id.desc()).limit(1200).all()

    page_title = 'Accounts KPI Detail'
    subtitle = 'Detailed records'
    grand_total = 0.0
    columns = []
    rows = []
    row_kind = 'generic'
    payable_cards = []
    spent_cards = []
    active_payable_head = (payable_head or '').strip()
    active_spent_head = (spent_head or '').strip()
    valid_metrics = {
        'company_total', 'received', 'spent', 'net', 'cash', 'bank',
        'receivable', 'payable', 'income', 'expenses'
    }
    if metric not in valid_metrics:
        metric = 'company_total'

    if metric in ('company_total', 'cash', 'bank', 'receivable', 'payable'):
        if metric == 'company_total':
            page_title = 'Company-Owned Accounts Total'
            subtitle = 'All active company/cash/bank accounts with current balances'
            items = [a for a in accounts if (a.get('type') in company_types)]
            grand_total = float(kpis.get('company_owned_total') or 0.0)
        elif metric == 'cash':
            page_title = 'Cash Accounts Balance'
            subtitle = 'All active company/cash accounts with current balances'
            items = [a for a in accounts if (a.get('type') in ('company', 'cash'))]
            grand_total = float(kpis.get('cash_total') or 0.0)
        elif metric == 'bank':
            page_title = 'Bank Accounts Balance'
            subtitle = 'All active bank accounts with current balances'
            items = [a for a in accounts if (a.get('type') == 'bank')]
            grand_total = float(kpis.get('bank_total') or 0.0)
        elif metric == 'receivable':
            page_title = 'Receivable Balances'
            subtitle = 'Positive balances in Project-in-Flow (client) accounts'
            items = [a for a in accounts if (a.get('type') in _ACCOUNT_PROJECT_FLOW_TYPES and float(a.get('current_balance') or 0.0) > 0.0)]
            grand_total = float(kpis.get('receivable_total') or 0.0)
        else:
            page_title = 'Payable Balances'
            subtitle = 'Current pending payables from wages, subcontractors, and purchases'
            payable_rows, payable_total = _accounts_payable_breakdown_rows()
            grouped = {}
            for r in (payable_rows or []):
                k = str(r.get('kind') or 'Payable').strip() or 'Payable'
                grouped[k] = float(grouped.get(k, 0.0) or 0.0) + float(r.get('amount') or 0.0)
            payable_cards = [
                {'head': k, 'amount': float(v or 0.0)}
                for k, v in grouped.items()
            ]
            payable_cards = sorted(payable_cards, key=lambda x: float(x.get('amount') or 0.0), reverse=True)
            if active_payable_head:
                head_l = active_payable_head.strip().lower()
                payable_rows = [r for r in payable_rows if str(r.get('kind') or '').strip().lower() == head_l]
                filtered_total = float(sum(float(r.get('amount') or 0.0) for r in payable_rows))
                grand_total = filtered_total
                subtitle = f'{active_payable_head} pending'
            else:
                grand_total = float(payable_total or 0.0)

        if metric == 'payable':
            row_kind = 'payable'
            columns = [
                {'key': 'kind', 'label': 'Payable Head'},
                {'key': 'name', 'label': 'Name'},
                {'key': 'detail', 'label': 'Detail'},
                {'key': 'amount', 'label': 'Pending Amount', 'align': 'text-end', 'format': 'currency'},
            ]
            rows = payable_rows
        else:
            items = sorted(items, key=lambda a: abs(float(a.get('current_balance') or 0.0)), reverse=True)
            row_kind = 'account'
            columns = [
                {'key': 'name', 'label': 'Account'},
                {'key': 'type', 'label': 'Account Class'},
                {'key': 'bank_detail', 'label': 'Bank Detail'},
                {'key': 'opening_balance', 'label': 'Opening', 'align': 'text-end', 'format': 'currency'},
                {'key': 'current_balance', 'label': 'Current', 'align': 'text-end', 'format': 'currency'},
            ]
            rows = []
            for a in items:
                curr = float(a.get('current_balance') or 0.0)
                b_detail = '-'
                if a.get('type') == 'bank':
                    b_detail = f"{a.get('bank_name') or '-'} / {a.get('account_number') or '-'}"
                rows.append({
                    'name': a.get('name') or '-',
                    'type': str(a.get('account_group') or '').replace('_', ' ').title(),
                    'bank_detail': b_detail,
                    'opening_balance': float(a.get('opening_balance') or 0.0),
                    'current_balance': curr,
                })
    else:
        def _spent_bucket(tx):
            tx_type = str(getattr(tx, 'type', '') or '').strip().lower()
            tx_cat = str(getattr(tx, 'category', '') or '').strip().lower()
            rel_type = str(getattr(tx, 'related_entity_type', '') or '').strip().lower()
            source_type = str(getattr(tx, 'source_type', '') or '').strip().lower()
            if tx_type in ('payroll', 'expense_wage', 'advance_to_person'):
                return 'labour_workers'
            if tx_type == 'expense_material' or tx_cat == 'purchase':
                return 'material'
            if (tx_type == 'office_management_payment' and rel_type == 'office_staff') or source_type.startswith('office_staff_ledger_'):
                return 'office_staff'
            if (tx_type == 'office_management_payment') or source_type.startswith('office_expense'):
                return 'office_expenses'
            if getattr(tx, 'project_id', None) is not None:
                return 'project_expenses'
            return 'other'

        if metric in ('received', 'income'):
            page_title = 'Total Received'
            subtitle = 'Income transactions in selected date range'
            tx_rows = _tx_rows_for_categories(('income',))
            grand_total = float(kpis.get('received_total') or 0.0)
        elif metric in ('spent', 'expenses'):
            page_title = 'Total Spent'
            subtitle = 'Expense, purchase, payroll, and advance transactions in selected date range'
            tx_rows = _tx_rows_for_categories(('expense', 'purchase', 'payroll', 'advance'))
            grand_total = float(kpis.get('spent_total') or 0.0)
            spent_q = AccountTransaction.query.filter(
                AccountTransaction.is_void == False,
                func.lower(func.coalesce(AccountTransaction.category, '')).in_(('expense', 'purchase', 'payroll', 'advance'))
            )
            if date_from:
                spent_q = spent_q.filter(AccountTransaction.date >= date_from)
            if date_to:
                spent_q = spent_q.filter(AccountTransaction.date <= date_to)
            spent_rows = spent_q.all()
            labour_workers_spent = 0.0
            material_spent = 0.0
            office_staff_spent = 0.0
            office_expenses_spent = 0.0
            project_expenses_spent = 0.0
            for tx in spent_rows:
                amount = float(getattr(tx, 'amount', 0.0) or 0.0)
                bucket = _spent_bucket(tx)
                if bucket == 'labour_workers':
                    labour_workers_spent += amount
                elif bucket == 'material':
                    material_spent += amount
                elif bucket == 'office_staff':
                    office_staff_spent += amount
                elif bucket == 'office_expenses':
                    office_expenses_spent += amount
                elif bucket == 'project_expenses':
                    project_expenses_spent += amount
            spent_cards = [
                {'key': 'all', 'head': 'Total Spent', 'amount': float(grand_total or 0.0), 'tone': 'kpi-red'},
                {'key': 'labour_workers', 'head': 'Labour / Workers', 'amount': float(labour_workers_spent or 0.0), 'tone': 'kpi-orange'},
                {'key': 'material', 'head': 'Material', 'amount': float(material_spent or 0.0), 'tone': 'kpi-teal'},
                {'key': 'office_staff', 'head': 'Office Staff', 'amount': float(office_staff_spent or 0.0), 'tone': 'kpi-blue'},
                {'key': 'office_expenses', 'head': 'Office Expenses', 'amount': float(office_expenses_spent or 0.0), 'tone': 'kpi-purple'},
                {'key': 'project_expenses', 'head': 'Project Expenses', 'amount': float(project_expenses_spent or 0.0), 'tone': 'kpi-teal'},
            ]
            if active_spent_head and active_spent_head != 'all':
                allowed = {'labour_workers', 'material', 'office_staff', 'office_expenses', 'project_expenses'}
                selected = active_spent_head.strip().lower()
                if selected in allowed:
                    tx_rows = [t for t in tx_rows if _spent_bucket(t) == selected]
                    selected_card = next((c for c in spent_cards if str(c.get('key') or '').lower() == selected), None)
                    if selected_card:
                        grand_total = float(selected_card.get('amount') or 0.0)
                        subtitle = f'{selected_card.get("head") or "Spent"} in selected date range'
        else:
            page_title = 'Net Cashflow'
            subtitle = 'Income minus spent (expense/purchase/payroll/advance)'
            tx_rows = _tx_rows_for_categories(('income', 'expense', 'purchase', 'payroll', 'advance'))
            grand_total = float(kpis.get('net_cashflow') or 0.0)

        row_kind = 'transaction'
        columns = [
            {'key': 'date', 'label': 'Date'},
            {'key': 'type', 'label': 'Type'},
            {'key': 'category', 'label': 'Category'},
            {'key': 'from_account', 'label': 'From'},
            {'key': 'to_account', 'label': 'To / Party'},
            {'key': 'project', 'label': 'Project'},
            {'key': 'stage', 'label': 'Stage'},
            {'key': 'amount', 'label': 'Amount', 'align': 'text-end', 'format': 'currency'},
            {'key': 'note', 'label': 'Note'},
            {'key': 'reference_id', 'label': 'Reference'},
        ]
        rows = []
        for r in tx_rows:
            amt = float(r.amount or 0.0)
            if metric == 'net' and str(r.category or '').strip().lower() != 'income':
                amt = -amt
            rows.append({
                'date': (r.date.isoformat() if r.date else ''),
                'type': str(r.type or '').replace('_', ' ').title(),
                'category': str(r.category or '').title(),
                'from_account': (r.from_account.name if r.from_account else '-'),
                'to_account': ((r.to_account.name if r.to_account else '') or (r.party_name or '-')),
                'project': (r.project.name if r.project else '-'),
                'stage': (r.stage.name if r.stage else '-'),
                'amount': amt,
                'note': (r.note or '-'),
                'reference_id': (r.reference_id or '-'),
            })

    return {
        'metric': metric,
        'page_title': page_title,
        'subtitle': subtitle,
        'columns': columns,
        'rows': rows,
        'row_kind': row_kind,
        'grand_total': float(grand_total or 0.0),
        'payable_cards': payable_cards,
        'spent_cards': spent_cards,
        'active_payable_head': active_payable_head,
        'active_spent_head': active_spent_head,
    }


def _account_dashboard_subgroups(metric):
    metric = (metric or '').strip().lower()
    rows = _list_accounts_with_balances()
    out = []
    if metric == 'cash':
        for a in rows:
            if a['type'] in ('company', 'cash'):
                out.append({'label': a['name'], 'value': float(a['current_balance'] or 0.0), 'account_id': int(a['id'])})
    elif metric == 'bank':
        for a in rows:
            if a['type'] == 'bank':
                out.append({'label': a['name'], 'value': float(a['current_balance'] or 0.0), 'account_id': int(a['id'])})
    elif metric == 'receivable':
        for a in rows:
            if a['type'] in _ACCOUNT_PROJECT_FLOW_TYPES and float(a['current_balance'] or 0.0) > 0:
                out.append({'label': a['name'], 'value': float(a['current_balance'] or 0.0), 'account_id': int(a['id'])})
    elif metric == 'payable':
        payable_rows, _ = _accounts_payable_breakdown_rows()
        grouped = {}
        for r in payable_rows:
            k = str(r.get('kind') or 'Payable')
            grouped[k] = float(grouped.get(k, 0.0) or 0.0) + float(r.get('amount') or 0.0)
        for k, v in grouped.items():
            out.append({'label': k, 'value': float(v or 0.0), 'tx_type': ''})
    elif metric in ('expenses', 'income', 'net'):
        q = (db.session.query(
            func.lower(func.coalesce(AccountTransaction.type, '')).label('tx_type'),
            func.coalesce(func.sum(AccountTransaction.amount), 0.0).label('amt')
        )
        .filter(AccountTransaction.is_void == False)
        .group_by(func.lower(func.coalesce(AccountTransaction.type, ''))))
        if metric == 'income':
            q = q.filter(func.lower(AccountTransaction.category) == 'income')
        elif metric == 'expenses':
            q = q.filter(func.lower(AccountTransaction.category).in_(('expense', 'purchase', 'payroll', 'advance')))
        for tx_type, amt in q.all():
            out.append({'label': tx_type or 'unknown', 'value': float(amt or 0.0), 'tx_type': tx_type or ''})
    out.sort(key=lambda x: float(x.get('value', 0.0) or 0.0), reverse=True)
    return out


def _account_running_balance_rows(history_rows, account_id=None):
    if not account_id:
        return {int(r.id): None for r in history_rows}
    ordered = (AccountTransaction.query
               .filter(
                   AccountTransaction.is_void == False,
                   or_(AccountTransaction.from_account_id == int(account_id), AccountTransaction.to_account_id == int(account_id))
               )
               .order_by(AccountTransaction.date.asc(), AccountTransaction.id.asc())
               .all())
    bal = 0.0
    run_map = {}
    base_opening = float(Account.query.get(int(account_id)).opening_balance or 0.0) if account_id else 0.0
    bal += base_opening
    for r in ordered:
        if int(r.from_account_id or 0) == int(account_id):
            bal -= float(r.amount or 0.0)
        if int(r.to_account_id or 0) == int(account_id):
            bal += float(r.amount or 0.0)
        run_map[int(r.id)] = float(bal)
    return {int(r.id): run_map.get(int(r.id)) for r in history_rows}


def _accounts_post_transaction(payload, source_type=None, source_id=None, commit=False):
    if source_type:
        payload = dict(payload or {})
        payload['source_type'] = source_type
        payload['source_id'] = source_id
    return _create_account_transaction(payload or {}, commit=commit)


def _accounts_upsert_purchase_paid_txn(purchase_row, supplier_name='', commit=False):
    if not purchase_row:
        return False, 'Purchase row is required.', []
    company = _accounts_default_company_cash()
    supplier_acc = _accounts_party_account(supplier_name, 'vendor')
    tx_date = (purchase_row.date.isoformat() if purchase_row.date else _pkt_today().isoformat())
    existing = (AccountTransaction.query
                .filter(
                    AccountTransaction.source_id == int(purchase_row.id),
                    func.lower(func.coalesce(AccountTransaction.source_type, '')).in_(('purchase_v2_paid:direct', 'purchase_v2_paid:step2'))
                )
                .order_by(AccountTransaction.id.desc())
                .first())
    if existing:
        existing.date = _parse_date(tx_date)
        existing.type = 'purchase'
        existing.amount = float(purchase_row.total_amount or 0.0)
        existing.from_account_id = (company.id if company else existing.from_account_id)
        existing.to_account_id = (supplier_acc.id if supplier_acc else existing.to_account_id)
        existing.executed_by_account_id = (company.id if company else existing.executed_by_account_id)
        existing.related_entity_type = 'supplier'
        existing.related_entity_id = int(purchase_row.supplier_id or 0) or None
        existing.party_name = supplier_name or existing.party_name
        existing.category = 'purchase'
        existing.note = f'Purchase #{purchase_row.id} marked paid'
        existing.reference_id = f'purchase_v2#{purchase_row.id}'
        existing.is_void = False
        # If split transaction exists (step1), keep same group active.
        grp = (existing.group_id or '').strip()
        if grp:
            for r in AccountTransaction.query.filter(AccountTransaction.group_id == grp).all():
                r.is_void = False
        if commit:
            db.session.commit()
        return True, '', [existing]

    payload = {
        'date': tx_date,
        'type': 'purchase',
        'amount': float(purchase_row.total_amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (supplier_acc.id if supplier_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'related_entity_type': 'supplier',
        'related_entity_id': purchase_row.supplier_id,
        'party_name': supplier_name,
        'category': 'purchase',
        'note': f'Purchase #{purchase_row.id} marked paid',
        'reference_id': f'purchase_v2#{purchase_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='purchase_v2_paid', source_id=purchase_row.id, commit=commit)


def _accounts_party_account(label, acc_type='person'):
    nm = _normalize_name_ci(label)
    if not nm:
        return _accounts_default_external_parties()
    source = 'worker' if acc_type == 'person' else acc_type
    return _account_get_or_create(nm, acc_type, auto_generated=True, auto_source=source)


def _accounts_post_owner_receipt(owner_payment_row, project=None, commit=False):
    company = _accounts_default_company_cash()
    recv_id = int(getattr(owner_payment_row, 'received_to_account_id', 0) or 0)
    if recv_id:
        recv_acc = Account.query.get(recv_id)
        if recv_acc and (not recv_acc.is_void) and str(recv_acc.status or 'active').strip().lower() == 'active' \
           and str(recv_acc.type or '').strip().lower() in _ACCOUNT_COMPANY_TYPES:
            company = recv_acc
    client_name = _normalize_name_ci((project.client if project else '') or 'Client')
    client_acc = _accounts_party_account(client_name, 'client')
    payload = {
        'date': (owner_payment_row.date.isoformat() if owner_payment_row and owner_payment_row.date else _pkt_today().isoformat()),
        'type': 'project_income',
        'amount': float(owner_payment_row.amount or 0.0),
        'from_account_id': (client_acc.id if client_acc else None),
        'to_account_id': (company.id if company else None),
        'executed_by_account_id': (client_acc.id if client_acc else None),
        'project_id': (owner_payment_row.project_id if owner_payment_row else None),
        'related_entity_type': 'project',
        'related_entity_id': (owner_payment_row.project_id if owner_payment_row else None),
        'party_name': (project.name if project else 'Owner'),
        'category': 'income',
        'note': f'Owner/client receipt #{owner_payment_row.id}',
        'reference_id': f'owner_payment#{owner_payment_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='owner_payment', source_id=(owner_payment_row.id if owner_payment_row else None), commit=commit)


def _accounts_post_expense_row(expense_row, commit=False):
    company = _accounts_default_company_cash()
    ext = _accounts_default_external_parties()
    cat_name = (expense_row.expense_category.name if expense_row and expense_row.expense_category else '')
    tx_type = 'expense_material' if ('material' in (cat_name or '').strip().lower()) else 'expense_general'
    payload = {
        'date': (expense_row.date.isoformat() if expense_row and expense_row.date else _pkt_today().isoformat()),
        'type': tx_type,
        'amount': float(expense_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (ext.id if ext else None),
        'executed_by_account_id': (company.id if company else None),
        'project_id': (expense_row.project_id if expense_row else None),
        'stage_id': (expense_row.stage_id if expense_row else None),
        'party_name': (expense_row.remarks or cat_name or 'Expense Party'),
        'category': 'expense',
        'note': f'Expense posting #{expense_row.id}',
        'reference_id': f'expense#{expense_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='expense', source_id=(expense_row.id if expense_row else None), commit=commit)


def _accounts_post_personal_expense_row(personal_expense_row, commit=False):
    company = _accounts_default_company_cash()
    ext = _accounts_default_external_parties()
    beneficiary_name = (personal_expense_row.beneficiary_name or '').strip()
    beneficiary_acc = _accounts_party_account(beneficiary_name, 'person')
    payload = {
        'date': (personal_expense_row.date.isoformat() if personal_expense_row and personal_expense_row.date else _pkt_today().isoformat()),
        # Personal disbursements are non-project payouts; use party_payment to avoid project/stage scope enforcement.
        'type': 'party_payment',
        'amount': float(personal_expense_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (beneficiary_acc.id if beneficiary_acc else ext.id if ext else None),
        'executed_by_account_id': (company.id if company else None),
        'party_name': beneficiary_name,
        'category': 'personal',
        'note': f'Personal expense: {personal_expense_row.category} - {personal_expense_row.remarks or ""}',
        'reference_id': f'personal_expense#{personal_expense_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='personal_expense', source_id=(personal_expense_row.id if personal_expense_row else None), commit=commit)


def _accounts_post_labour_ledger_row(ledger_row, worker_name='', commit=False):
    et = (ledger_row.entry_type or '').strip().lower()
    if et not in ('payment', 'tip', 'advance'):
        return True, '', []
    company = _accounts_default_company_cash()
    worker_label = _normalize_name_ci(worker_name) or f'Worker #{ledger_row.worker_id}'
    worker_acc = _accounts_party_account(worker_label, 'person')
    tx_type = 'advance_to_person' if et == 'advance' else 'payroll'
    category = 'advance' if et == 'advance' else 'payroll'
    payload = {
        'date': (ledger_row.date.isoformat() if ledger_row and ledger_row.date else _pkt_today().isoformat()),
        'type': tx_type,
        'amount': float(ledger_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (worker_acc.id if worker_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'project_id': (ledger_row.project_id if ledger_row else None),
        'stage_id': (ledger_row.stage_id if ledger_row else None),
        'related_entity_type': 'worker',
        'related_entity_id': (ledger_row.worker_id if ledger_row else None),
        'party_name': worker_label,
        'category': category,
        'note': (ledger_row.notes or f'Labour {et}'),
        'reference_id': f'labour_ledger#{ledger_row.id}',
    }
    return _accounts_post_transaction(payload, source_type=f'labour_ledger_{et}', source_id=(ledger_row.id if ledger_row else None), commit=commit)


def _accounts_upsert_labour_ledger_txn(worker_row, ledger_row, commit=False):
    if (not worker_row) or (not ledger_row) or bool(getattr(ledger_row, 'is_void', False)):
        return True, '', []
    et = (ledger_row.entry_type or '').strip().lower()
    if et not in ('payment', 'tip', 'advance'):
        return True, '', []
    st = f'labour_ledger_{et}'
    existing = (AccountTransaction.query
                .filter(
                    AccountTransaction.source_id == int(ledger_row.id),
                    or_(
                        func.lower(func.coalesce(AccountTransaction.source_type, '')) == f'{st}:direct',
                        func.lower(func.coalesce(AccountTransaction.source_type, '')).like(f'{st}:%')
                    )
                )
                .order_by(AccountTransaction.id.desc())
                .first())
    company = _accounts_default_company_cash()
    worker_label = _normalize_name_ci(getattr(worker_row, 'name', '') or f'Worker #{ledger_row.worker_id}')
    worker_acc = _accounts_party_account(worker_label, 'person')
    if existing:
        existing.date = (ledger_row.date or existing.date or _pkt_today())
        existing.type = 'advance_to_person' if et == 'advance' else 'payroll'
        existing.amount = float(ledger_row.amount or 0.0)
        existing.from_account_id = int(company.id if company else existing.from_account_id)
        existing.to_account_id = int(worker_acc.id if worker_acc else (existing.to_account_id or 0)) or None
        existing.executed_by_account_id = int(company.id if company else existing.executed_by_account_id)
        existing.project_id = (ledger_row.project_id if ledger_row.project_id else None)
        existing.stage_id = (ledger_row.stage_id if ledger_row.stage_id else None)
        existing.related_entity_type = 'worker'
        existing.related_entity_id = int(getattr(ledger_row, 'worker_id', 0) or 0) or None
        existing.party_name = worker_label
        existing.category = 'advance' if et == 'advance' else 'payroll'
        existing.note = (ledger_row.notes or f'Labour {et}')
        existing.reference_id = f'labour_ledger#{ledger_row.id}'
        existing.is_void = False
        if commit:
            db.session.commit()
        return True, '', [existing]
    payload = {
        'date': ((ledger_row.date or _pkt_today()).isoformat()),
        'type': 'advance_to_person' if et == 'advance' else 'payroll',
        'amount': float(ledger_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (worker_acc.id if worker_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'project_id': (ledger_row.project_id if ledger_row else None),
        'stage_id': (ledger_row.stage_id if ledger_row else None),
        'related_entity_type': 'worker',
        'related_entity_id': (ledger_row.worker_id if ledger_row else None),
        'party_name': worker_label,
        'category': 'advance' if et == 'advance' else 'payroll',
        'note': (ledger_row.notes or f'Labour {et}'),
        'reference_id': f'labour_ledger#{ledger_row.id}',
    }
    return _accounts_post_transaction(payload, source_type=st, source_id=ledger_row.id, commit=commit)


def _accounts_post_supplier_credit_row(supplier_ledger_row, supplier_name='', commit=False):
    company = _accounts_default_company_cash()
    label = _normalize_name_ci(supplier_name) or f'Supplier #{supplier_ledger_row.supplier_id}'
    supplier_acc = _accounts_party_account(label, 'vendor')
    payload = {
        'date': _pkt_today().isoformat(),
        'type': 'purchase',
        'amount': float(supplier_ledger_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (supplier_acc.id if supplier_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'related_entity_type': 'supplier',
        'related_entity_id': (supplier_ledger_row.supplier_id if supplier_ledger_row else None),
        'party_name': label,
        'category': 'purchase',
        'note': (supplier_ledger_row.note or 'Supplier payment'),
        'reference_id': f'supplier_ledger#{supplier_ledger_row.id}',
    }
    source = f"supplier_credit_{(supplier_ledger_row.reference_type or 'payment').strip().lower()}"
    return _accounts_post_transaction(payload, source_type=source, source_id=(supplier_ledger_row.id if supplier_ledger_row else None), commit=commit)


def _accounts_post_subcontract_payment_row(payment_row, subcontractor_name='', commit=False):
    if payment_row is None or bool(getattr(payment_row, 'is_void', False)):
        return True, '', []
    et = (payment_row.entry_type or '').strip().lower()
    if et != 'payment':
        return True, '', []
    company = _accounts_default_company_cash()
    label = _normalize_name_ci(subcontractor_name) or f'Subcontractor #{payment_row.subcontractor_id}'
    sub_acc = _accounts_party_account(label, 'person')
    tx_type = 'expense_subcontractor' if (payment_row and payment_row.project_id) else 'expense_general'
    payload = {
        'date': (payment_row.date.isoformat() if payment_row and payment_row.date else _pkt_today().isoformat()),
        'type': tx_type,
        'amount': float(payment_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (sub_acc.id if sub_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'project_id': (payment_row.project_id if payment_row else None),
        'stage_id': (payment_row.stage_id if payment_row else None),
        'related_entity_type': 'subcontractor',
        'related_entity_id': (payment_row.subcontractor_id if payment_row else None),
        'party_name': label,
        'category': 'expense',
        'note': (payment_row.notes or 'Subcontractor payment'),
        'reference_id': f'subcontract_payment#{payment_row.id}',
    }
    source = f'subcontract_payment_{et}'
    return _accounts_post_transaction(payload, source_type=source, source_id=(payment_row.id if payment_row else None), commit=commit)


def _accounts_post_subcontract_labour_payment_row(payment_row, worker_name='', subcontractor_name='', commit=False):
    if payment_row is None or bool(getattr(payment_row, 'is_void', False)):
        return True, '', []
    company = _accounts_default_company_cash()
    label = _normalize_name_ci(worker_name) or f'Sub-Labour Worker #{payment_row.worker_id}'
    worker_acc = _accounts_party_account(label, 'person')
    sub_label = _normalize_name_ci(subcontractor_name) or f'Subcontractor #{payment_row.subcontractor_id}'
    payload = {
        'date': (payment_row.date.isoformat() if payment_row and payment_row.date else _pkt_today().isoformat()),
        'type': 'payroll',
        'amount': float(payment_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (worker_acc.id if worker_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'related_entity_type': 'subcontractor_labour_worker',
        'related_entity_id': (payment_row.worker_id if payment_row else None),
        'party_name': label,
        'category': 'payroll',
        'note': (payment_row.notes or f'Sub-labour payment: {label} via {sub_label}'),
        'reference_id': f'sub_labour_payment#{payment_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='subcontract_labour_payment', source_id=(payment_row.id if payment_row else None), commit=commit)


def _accounts_upsert_office_staff_ledger_txn(staff_row, ledger_row, commit=False):
    if (not staff_row) or (not ledger_row) or bool(getattr(ledger_row, 'is_void', False)):
        return True, '', []
    et = (ledger_row.entry_type or '').strip().lower()
    if et not in ('advance', 'payment', 'tip'):
        return True, '', []
    st = f'office_staff_ledger_{et}'
    existing = (AccountTransaction.query
                .filter(
                    AccountTransaction.source_id == int(ledger_row.id),
                    or_(
                        func.lower(func.coalesce(AccountTransaction.source_type, '')) == f'{st}:direct',
                        func.lower(func.coalesce(AccountTransaction.source_type, '')).like(f'{st}:%')
                    )
                )
                .order_by(AccountTransaction.id.desc())
                .first())
    company = _accounts_default_company_cash()
    staff_label = _normalize_name_ci(getattr(staff_row, 'name', '') or f'Office Staff #{ledger_row.staff_id}')
    staff_acc = _accounts_party_account(staff_label, 'person')
    if existing:
        existing.date = (ledger_row.date or existing.date or _pkt_today())
        existing.amount = float(ledger_row.amount or 0.0)
        existing.type = 'office_management_payment'
        existing.from_account_id = int(company.id if company else existing.from_account_id)
        existing.to_account_id = int(staff_acc.id if staff_acc else (existing.to_account_id or 0)) or None
        existing.executed_by_account_id = int(company.id if company else existing.executed_by_account_id)
        existing.project_id = None
        existing.stage_id = None
        existing.related_entity_type = 'office_staff'
        existing.related_entity_id = int(getattr(ledger_row, 'staff_id', 0) or 0) or None
        existing.party_name = staff_label
        existing.category = 'expense'
        existing.note = (ledger_row.notes or f'Office staff {et}')
        existing.reference_id = f'office_staff_ledger#{ledger_row.id}'
        existing.is_void = False
        if commit:
            db.session.commit()
        return True, '', [existing]
    payload = {
        'date': ((ledger_row.date or _pkt_today()).isoformat()),
        'type': 'office_management_payment',
        'amount': float(ledger_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (staff_acc.id if staff_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'related_entity_type': 'office_staff',
        'related_entity_id': int(getattr(ledger_row, 'staff_id', 0) or 0) or None,
        'party_name': staff_label,
        'category': 'expense',
        'note': (ledger_row.notes or f'Office staff {et}'),
        'reference_id': f'office_staff_ledger#{ledger_row.id}',
    }
    return _accounts_post_transaction(payload, source_type=st, source_id=ledger_row.id, commit=commit)


def _accounts_upsert_office_expense_txn(expense_row, commit=False):
    if (not expense_row) or bool(getattr(expense_row, 'is_void', False)):
        return True, '', []
    existing = (AccountTransaction.query
                .filter(
                    AccountTransaction.source_id == int(expense_row.id),
                    or_(
                        func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'office_expense:direct',
                        func.lower(func.coalesce(AccountTransaction.source_type, '')).like('office_expense:%')
                    )
                )
                .order_by(AccountTransaction.id.desc())
                .first())
    company = _accounts_default_company_cash()
    ext = _accounts_default_external_parties()
    party = _normalize_name_ci(
        (expense_row.category or 'Office Expense') + (f" | {expense_row.remarks}" if (expense_row.remarks or '').strip() else '')
    )[:120]
    if existing:
        existing.date = (expense_row.date or existing.date or _pkt_today())
        existing.amount = float(expense_row.amount or 0.0)
        existing.type = 'office_management_payment'
        existing.from_account_id = int(company.id if company else existing.from_account_id)
        existing.to_account_id = int(ext.id if ext else (existing.to_account_id or 0)) or None
        existing.executed_by_account_id = int(company.id if company else existing.executed_by_account_id)
        existing.project_id = None
        existing.stage_id = None
        existing.related_entity_type = None
        existing.related_entity_id = None
        existing.party_name = party
        existing.category = 'expense'
        existing.note = (expense_row.remarks or f'Office expense: {expense_row.category or "-"}')
        existing.reference_id = f'office_expense#{expense_row.id}'
        existing.is_void = False
        if commit:
            db.session.commit()
        return True, '', [existing]
    payload = {
        'date': ((expense_row.date or _pkt_today()).isoformat()),
        'type': 'office_management_payment',
        'amount': float(expense_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (ext.id if ext else None),
        'executed_by_account_id': (company.id if company else None),
        'party_name': party,
        'category': 'expense',
        'note': (expense_row.remarks or f'Office expense: {expense_row.category or "-"}'),
        'reference_id': f'office_expense#{expense_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='office_expense', source_id=expense_row.id, commit=commit)


def _accounts_default_company_cash():
    return _account_get_or_create('Company Cash', 'company', opening_balance=0.0)


def _accounts_default_external_parties():
    # Backward-compatible function name; standardized control account label.
    row = _account_get_or_create('Credit/Debit Control', 'person', opening_balance=0.0)
    if row:
        return row
    # Fallback to legacy name if create/get fails unexpectedly.
    return _account_get_or_create('External Parties', 'person', opening_balance=0.0)


def _mark_auto_generated_person_accounts():
    # Best-effort labeling for already-created worker/person accounts.
    worker_names = {(_normalize_name_ci(w.name)).lower() for w in Worker.query.filter_by(active_status=True).all() if _normalize_name_ci(w.name)}
    if not worker_names:
        return
    rows = (Account.query
            .filter(
                Account.is_void == False,
                func.lower(func.coalesce(Account.type, '')) == 'person',
                or_(Account.auto_generated == False, Account.auto_generated.is_(None))
            ).all())
    changed = 0
    for a in rows:
        nm = (_normalize_name_ci(a.name)).lower()
        if nm in worker_names:
            a.auto_generated = True
            a.auto_source = a.auto_source or 'worker'
            changed += 1
    if changed:
        db.session.commit()


def _run_accounts_backfill():
    company = _accounts_default_company_cash()
    external = _accounts_default_external_parties()
    if (not company) or (not external):
        return {'created': 0, 'skipped': 0, 'errors': 1}

    created = 0
    skipped = 0
    errors = 0

    # Owner receipts (incoming): Credit/Debit control -> Company
    for r in OwnerPayment.query.filter(OwnerPayment.is_void == False).order_by(OwnerPayment.id.asc()).all():
        st = 'owner_payment'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        recv = Account.query.get(int(r.received_to_account_id or 0)) if getattr(r, 'received_to_account_id', None) else None
        recv_id = (int(recv.id) if recv and (not recv.is_void)
                   and str(recv.status or 'active').strip().lower() == 'active'
                   and str(recv.type or '').strip().lower() in _ACCOUNT_COMPANY_TYPES
                   else int(company.id))
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': 'project_income',
            'from_account_id': external.id,
            'to_account_id': recv_id,
            'executed_by_account_id': external.id,
            'project_id': (r.project_id or None),
            'stage_id': None,
            'party_name': (r.project.name if r.project else 'Owner'),
            'category': 'income',
            'note': f'Backfill owner receipt #{r.id}',
            'reference_id': f'owner_payment#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-owner-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Expenses: Company -> Credit/Debit control
    for r in Expense.query.filter(Expense.is_void == False).order_by(Expense.id.asc()).all():
        if float(r.amount or 0.0) <= 0:
            skipped += 1
            continue
        st = 'expense'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        party = _normalize_name_ci(
            (r.expense_category.name if r.expense_category else '')
            or (r.remarks or '')
            or 'Expense Party'
        )
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': 'expense_general',
            'from_account_id': company.id,
            'executed_by_account_id': company.id,
            'project_id': (r.project_id or None),
            'stage_id': (r.stage_id or None),
            'party_name': party,
            'category': 'expense',
            'note': f'Backfill expense #{r.id}',
            'reference_id': f'expense#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-exp-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Office staff ledger cash entries: Company -> Office Staff
    for r in OfficeStaffLedger.query.filter(OfficeStaffLedger.is_void == False).order_by(OfficeStaffLedger.id.asc()).all():
        et = (r.entry_type or '').strip().lower()
        if et not in ('advance', 'payment', 'tip'):
            continue
        st = f'office_staff_ledger_{et}'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        staff = OfficeStaff.query.get(int(r.staff_id or 0))
        party = _normalize_name_ci(staff.name if staff else f'Office Staff #{r.staff_id}') or 'Office Staff'
        staff_acc = _accounts_party_account(party, 'person')
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': 'party_payment',
            'from_account_id': company.id,
            'to_account_id': (staff_acc.id if staff_acc else None),
            'executed_by_account_id': company.id,
            'party_name': party,
            'category': 'expense',
            'note': f'Backfill office staff ledger #{r.id} ({et})',
            'reference_id': f'office_staff_ledger#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-offstaff-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Office expenses: Company -> Credit/Debit control
    for r in OfficeExpense.query.filter(OfficeExpense.is_void == False).order_by(OfficeExpense.id.asc()).all():
        if float(r.amount or 0.0) <= 0:
            skipped += 1
            continue
        if int(getattr(r, 'office_staff_ledger_id', 0) or 0):
            # Already represented by office staff ledger cash postings.
            skipped += 1
            continue
        st = 'office_expense'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': 'party_payment',
            'from_account_id': company.id,
            'to_account_id': external.id,
            'executed_by_account_id': company.id,
            'party_name': _normalize_name_ci((r.category or 'Office Expense') + (f' | {r.remarks}' if (r.remarks or '').strip() else '')),
            'category': 'expense',
            'note': f'Backfill office expense #{r.id}',
            'reference_id': f'office_expense#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-offexp-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Labour ledger cash payouts/advances/tips: Company -> Worker
    for r in LabourLedger.query.filter(LabourLedger.is_void == False).order_by(LabourLedger.id.asc()).all():
        et = (r.entry_type or '').strip().lower()
        # Settlement entries are non-cash adjustments and must not create Accounts cash transactions.
        if et not in ('payment', 'tip', 'advance'):
            continue
        st = f'labour_ledger_{et}'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        party = (r.worker.name if r.worker else f'Worker #{r.worker_id or ""}').strip() or 'Worker'
        worker_acc = _accounts_party_account(party, 'person')
        cat = 'advance' if et == 'advance' else 'payroll'
        tx_type = 'advance_to_person' if et == 'advance' else 'payroll'
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': tx_type,
            'from_account_id': company.id,
            'to_account_id': (worker_acc.id if worker_acc else None),
            'executed_by_account_id': company.id,
            'project_id': (r.project_id or None),
            'stage_id': (r.stage_id or None),
            'related_entity_type': 'worker',
            'related_entity_id': (r.worker_id or None),
            'party_name': party,
            'category': cat,
            'note': f'Backfill labour ledger #{r.id} ({et})',
            'reference_id': f'labour_ledger#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-lab-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Subcontract payments: Company -> Subcontractor
    for r in SubcontractPayment.query.filter(SubcontractPayment.is_void == False).order_by(SubcontractPayment.id.asc()).all():
        et = (r.entry_type or 'payment').strip().lower()
        st = f'subcontract_payment_{et}'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        party = (r.subcontractor.name if r.subcontractor else f'Subcontractor #{r.subcontractor_id or ""}').strip() or 'Subcontractor'
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': 'expense_subcontractor',
            'from_account_id': company.id,
            'executed_by_account_id': company.id,
            'project_id': (r.project_id or (r.subcontractor.project_id if r.subcontractor else None) or None),
            'stage_id': (r.stage_id or (r.subcontractor.stage_id if r.subcontractor else None) or None),
            'related_entity_type': 'subcontractor',
            'related_entity_id': (r.subcontractor_id or None),
            'party_name': party,
            'category': 'expense',
            'note': f'Backfill subcontract payment #{r.id} ({et})',
            'reference_id': f'subcontract_payment#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-subpay-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Supplier credits (payments): Company -> Supplier
    for r in SupplierLedger.query.filter(
        SupplierLedger.is_void == False,
        func.lower(SupplierLedger.entry_type) == 'credit'
    ).order_by(SupplierLedger.id.asc()).all():
        rt = (r.reference_type or 'payment').strip().lower()
        st = f'supplier_credit_{rt}'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        party = (r.supplier.name if r.supplier else f'Supplier #{r.supplier_id or ""}').strip() or 'Supplier'
        payload = {
            'date': (_pkt_today().isoformat()),
            'amount': float(r.amount or 0.0),
            'type': 'purchase',
            'from_account_id': company.id,
            'executed_by_account_id': company.id,
            'related_entity_type': 'supplier',
            'related_entity_id': (r.supplier_id or None),
            'party_name': party,
            'category': 'purchase',
            'note': f'Backfill supplier ledger credit #{r.id}',
            'reference_id': f'supplier_ledger#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-supled-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Paid purchase v2 entries: Company -> Supplier
    for r in PurchaseV2.query.filter(
        PurchaseV2.is_void == False,
        func.lower(PurchaseV2.payment_status) == 'paid'
    ).order_by(PurchaseV2.id.asc()).all():
        st = 'purchase_v2_paid'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        party = (r.supplier.name if r.supplier else f'Supplier #{r.supplier_id or ""}').strip() or 'Supplier'
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.total_amount or 0.0),
            'type': 'purchase',
            'from_account_id': company.id,
            'executed_by_account_id': company.id,
            'related_entity_type': 'supplier',
            'related_entity_id': (r.supplier_id or None),
            'party_name': party,
            'category': 'purchase',
            'note': f'Backfill paid purchase_v2 #{r.id}',
            'reference_id': f'purchase_v2#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-pv2-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        errors += 1

    return {'created': created, 'skipped': skipped, 'errors': errors}


def _bootstrap_accounts_backfill_once():
    if _runtime_flag_get('accounts_backfill_done') == '1':
        return
    try:
        stats = _run_accounts_backfill()
        if int(stats.get('errors', 0) or 0) == 0:
            _runtime_flag_set('accounts_backfill_done', '1')
    except Exception:
        db.session.rollback()


def _backfill_owner_payment_receiving_accounts():
    try:
        company = _accounts_default_company_cash()
        rows = OwnerPayment.query.filter(OwnerPayment.received_to_account_id.is_(None)).all()
        changed = 0
        for op in rows:
            acc_id = None
            linked = (AccountTransaction.query
                      .filter(
                          AccountTransaction.source_id == int(op.id),
                          AccountTransaction.is_void == False,
                          or_(
                              func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'owner_payment:direct',
                              func.lower(func.coalesce(AccountTransaction.source_type, '')).like('owner_payment:%')
                          )
                      )
                      .order_by(AccountTransaction.id.asc())
                      .first())
            if linked and linked.to_account_id:
                acc_id = int(linked.to_account_id)
            elif company and company.id:
                acc_id = int(company.id)
            if acc_id:
                op.received_to_account_id = acc_id
                changed += 1
        if changed > 0:
            db.session.commit()
    except Exception:
        db.session.rollback()


def _backfill_accounts_scope_from_references():
    try:
        rows = (AccountTransaction.query
                .filter(
                    AccountTransaction.is_void == False,
                    func.lower(func.coalesce(AccountTransaction.type, '')).in_(
                        ('expense_material', 'expense_wage', 'expense_subcontractor',
                         'expense_general', 'purchase', 'payroll', 'advance_to_person')
                    ),
                    or_(AccountTransaction.project_id.is_(None), AccountTransaction.stage_id.is_(None))
                )
                .all())
        changed = 0
        for t in rows:
            ref = (t.reference_id or '').strip().lower()
            if ref.startswith('labour_ledger#'):
                try:
                    lid = int(ref.split('#', 1)[1].strip())
                except Exception:
                    lid = 0
                if lid:
                    lrow = LabourLedger.query.get(lid)
                    if lrow:
                        if t.project_id is None and lrow.project_id:
                            t.project_id = int(lrow.project_id)
                            changed += 1
                        if t.stage_id is None and lrow.stage_id:
                            t.stage_id = int(lrow.stage_id)
                            changed += 1
        if changed > 0:
            db.session.commit()
    except Exception:
        db.session.rollback()
