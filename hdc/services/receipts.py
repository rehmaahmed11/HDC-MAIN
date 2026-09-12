"""HDC services.receipts — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import os

from flask import has_request_context, url_for
from sqlalchemy import and_, func, or_

from hdc.models.accounts import Account, AccountTransaction, OwnerPayment
from hdc.services.accounts import _ACCOUNT_TXN_PAY_TYPES, _ACCOUNT_TXN_RECEIVE_TYPES, _account_entity_label, _account_tx_direction_for_type
from hdc.utils.normalize import _normalize_name_ci, _normalize_related_entity_type

def _receipt_company_profile():
    name = (os.environ.get('HDC_COMPANY_NAME') or '').strip() or 'Hadi Design and Construction Company'
    tagline = (os.environ.get('HDC_COMPANY_TAGLINE') or '').strip() or 'Design | Build | Manage'
    address = (os.environ.get('HDC_COMPANY_ADDRESS') or '').strip() or 'Address not configured'
    phones_raw = (os.environ.get('HDC_COMPANY_PHONES') or '').strip()
    if not phones_raw:
        p1 = (os.environ.get('HDC_COMPANY_PHONE_1') or '').strip()
        p2 = (os.environ.get('HDC_COMPANY_PHONE_2') or '').strip()
        phones_raw = ', '.join([p for p in [p1, p2] if p]).strip()
    phones = [p.strip() for p in str(phones_raw).replace(';', ',').replace('|', ',').split(',') if p.strip()]
    if not phones:
        phones = ['Phone not configured']
    logo_url = (os.environ.get('HDC_COMPANY_LOGO_URL') or '').strip()
    if not logo_url:
        logo_url = (url_for('static', filename='img/company_logo.svg') if has_request_context() else '/hdc_static/img/company_logo.svg')
    return {
        'name': name,
        'tagline': tagline,
        'address': address,
        'phones': phones,
        'logo_url': logo_url,
    }


def _account_receipt_recent_entries(txn_row, limit=5):
    if not txn_row:
        return [], ''
    tx_dir = _account_tx_direction_for_type(txn_row.type)
    rel_type = _normalize_related_entity_type(txn_row.related_entity_type)
    rel_id = int(txn_row.related_entity_id or 0)
    party_name = _normalize_name_ci(txn_row.party_name or '')
    counter_account_id = 0
    if tx_dir == 'pay':
        counter_account_id = int(txn_row.to_account_id or 0)
    elif tx_dir == 'receive':
        counter_account_id = int(txn_row.from_account_id or 0)

    q = (AccountTransaction.query
         .filter(
             AccountTransaction.is_void == False,
             AccountTransaction.id != int(txn_row.id)
         ))
    scope_label = ''
    if rel_type and rel_id:
        q = q.filter(
            func.lower(func.coalesce(AccountTransaction.related_entity_type, '')) == rel_type,
            AccountTransaction.related_entity_id == rel_id
        )
        scope_label = (_account_entity_label(rel_type, rel_id) or f'{rel_type.title()} #{rel_id}')
    elif party_name:
        q = q.filter(func.lower(func.trim(func.coalesce(AccountTransaction.party_name, ''))) == party_name.lower())
        scope_label = party_name
    elif counter_account_id:
        q = q.filter(or_(
            and_(
                func.lower(func.coalesce(AccountTransaction.type, '')).in_(_ACCOUNT_TXN_RECEIVE_TYPES),
                AccountTransaction.from_account_id == counter_account_id
            ),
            and_(
                func.lower(func.coalesce(AccountTransaction.type, '')).in_(_ACCOUNT_TXN_PAY_TYPES),
                AccountTransaction.to_account_id == counter_account_id
            ),
            and_(
                func.lower(func.coalesce(AccountTransaction.type, '')) == 'transfer',
                or_(
                    AccountTransaction.from_account_id == counter_account_id,
                    AccountTransaction.to_account_id == counter_account_id
                )
            )
        ))
        acc = Account.query.get(counter_account_id)
        scope_label = (acc.name if acc else f'Account #{counter_account_id}')
    else:
        return [], ''

    rows = (q.order_by(AccountTransaction.date.desc(), AccountTransaction.id.desc())
            .limit(max(1, min(int(limit or 5), 20)))
            .all())
    out = []
    for r in rows:
        party = (r.party_name or '').strip() or (r.to_account.name if r.to_account else '-')
        out.append({
            'date': (r.date.isoformat() if r.date else ''),
            'type': str(r.type or '').replace('_', ' ').title(),
            'direction': _account_tx_direction_for_type(r.type),
            'party': party,
            'amount': float(r.amount or 0.0),
            'receipt_url': url_for('hdc_account_transaction_receipt', txn_id=r.id),
        })
    return out, scope_label


def _owner_payment_recent_entries(project_id, exclude_id=None, limit=5):
    if not project_id:
        return []
    q = (OwnerPayment.query
         .filter(
             OwnerPayment.project_id == int(project_id),
             OwnerPayment.is_void == False
         ))
    if exclude_id:
        q = q.filter(OwnerPayment.id != int(exclude_id))
    rows = q.order_by(OwnerPayment.date.desc(), OwnerPayment.id.desc()).limit(max(1, min(int(limit or 5), 20))).all()
    out = []
    for r in rows:
        out.append({
            'date': (r.date.isoformat() if r.date else ''),
            'type': 'Owner Payment Receipt',
            'direction': 'receive',
            'party': (r.project.client if r.project else 'Client'),
            'amount': float(r.amount or 0.0),
            'receipt_url': url_for('hdc_owner_payment_receipt', pid=int(project_id), oid=r.id),
        })
    return out
