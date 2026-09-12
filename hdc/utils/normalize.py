"""HDC utils.normalize — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

def _normalize_trade_name(v):
    raw = (v or '').strip()
    if not raw:
        return ''
    return ' '.join(raw.split())


def _normalize_expense_category_name(v):
    raw = (v or '').strip()
    if not raw:
        return ''
    return ' '.join(raw.split())


def _normalize_name_ci(v):
    return ' '.join(str(v or '').strip().split())


def _normalize_account_group(v):
    g = (v or '').strip().lower()
    if g in ('project_in_flow', 'project-in-flow', 'project_inflow', 'project inflow'):
        return 'project_in_flow'
    if g in ('credit_debit', 'credit-debit', 'creditdebit', 'liability'):
        return 'credit_debit'
    # Backward compatibility for old "external" naming.
    if g == 'external':
        return 'credit_debit'
    return 'company'


def _normalize_account_mode(v):
    m = (v or '').strip().lower()
    return ('bank' if m == 'bank' else 'cash')


def _normalize_account_tx_type(v):
    tx = (v or '').strip().lower()
    alias_map = {
        'receive_from_project': 'project_income',
        'receive_intra_company': 'transfer',
        'receive_from_credit_debit': 'party_receipt',
        'pay_to_project': 'party_payment',
        'pay_intra_company': 'transfer',
        'pay_to_credit_debit': 'party_payment',
        'personal_payment': 'personal_management_payment',
    }
    return alias_map.get(tx, tx)


def _normalize_account_tx_direction(v):
    d = (v or '').strip().lower()
    if d in ('receive', 'pay'):
        return d
    return ''


def _normalize_related_entity_type(v):
    t = (v or '').strip().lower()
    if t in ('vendor', 'supplier'):
        return 'supplier'
    if t in ('subcontractor', 'sub_contractor', 'sub'):
        return 'subcontractor'
    if t in ('worker', 'labour', 'labor'):
        return 'worker'
    if t in ('project',):
        return 'project'
    if t in ('client',):
        return 'client'
    if t in ('office_staff', 'office-staff', 'officestaff', 'staff'):
        return 'office_staff'
    return t
