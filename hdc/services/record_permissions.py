"""Explicit, non-inheriting access to individual business records.

Page permissions decide which screens/actions are reachable. This optional
second gate decides which rows exist for a user. An empty or malformed map is
DENY ALL, not unrestricted. Parent/child records never inherit a grant.

The ORM read filter covers lists, aggregates, aliases and relationship loads.
The flush guard is deliberately separate: reading a row (or having it in the
identity map) is not authority to change, delete, void or reassign it.
"""
import json
import re
from datetime import date, datetime, timedelta

from flask import abort, current_app, g, has_request_context, request
from sqlalchemy import and_, event, false, func, inspect, or_
from sqlalchemy.orm import Session, with_loader_criteria

from hdc.extensions import db

_AUTH_TABLES = frozenset({'hdc_user', 'hdc_user_activity', 'hdc_activity_log'})
_GROUPS = {
    'projects': 'Projects & Estimation', 'workforce': 'Workforce',
    'subcontract': 'Subcontractors', 'office': 'Office',
    'materials': 'Materials & Purchases', 'tool_rental': 'Tools',
    'accounts': 'Accounts & Expenses', 'cashflow': 'Cash Flow',
    'loans': 'Loans', 'shared_expenses': 'Shared Expenses',
}
_LABELS = {
    'hdc_project': 'Projects', 'hdc_stage': 'Stages',
    'hdc_stage_definition': 'Stage library definitions',
    'hdc_stage_drawing': 'Stage drawings', 'hdc_worker': 'Workers',
    'hdc_time_entry': 'Attendance / time entries',
    'hdc_attendance': 'Legacy attendance', 'hdc_attendance_mark': 'Attendance status marks',
    'hdc_attendance_day': 'Attendance day summaries', 'hdc_labour_ledger': 'Worker ledger entries',
    'hdc_office_staff': 'Office staff', 'hdc_office_staff_attendance': 'Office attendance',
    'hdc_account': 'Accounts', 'hdc_account_txn': 'Account transactions',
    'hdc_owner_payment': 'Owner receipts', 'hdc_expense': 'Project expenses',
    'hdc_purchase_v2': 'Purchase orders', 'hdc_material_v2': 'Purchase materials',
    'hdc_usage_log_v2': 'Material usage entries', 'hdc_supplier': 'Suppliers',
    'hdc_subcontractor': 'Subcontractors', 'hdc_cash_flow_entry': 'Cash flow entries',
    'hdc_shared_expense': 'Shared expenses', 'hdc_shared_share': 'Shared expense shares',
}


def _is_admin(user):
    return (getattr(user, 'role', '') or '').strip().lower() == 'admin'


def _unwrap(user):
    return user._get_current_object() if hasattr(user, '_get_current_object') else user


def exact_access_enabled(user):
    user = _unwrap(user)
    if has_request_context():
        policy = g.get('_hdc_record_policy')
        if policy and policy['user'] is user:
            return policy['active']
    return bool(user and getattr(user, 'is_authenticated', False) and
                not _is_admin(user) and getattr(user, 'record_scope_enabled', False))


def _authenticated_id(user):
    state = inspect(_unwrap(user))
    return state.identity[0] if state.identity else None


def record_models():
    """All registered business models, including stock/support records.

    Storage keys are table names, not user-controlled class/import names. New
    models automatically start with no grants for strict users.
    """
    return {mapper.class_.__tablename__: mapper.class_
            for mapper in db.Model.registry.mappers
            if getattr(mapper.class_, '__tablename__', None) not in _AUTH_TABLES
            and 'id' in mapper.class_.__dict__}


def record_catalog():
    catalog = []
    for table, model in record_models().items():
        domain = model.__module__.rsplit('.', 1)[-1]
        label = _LABELS.get(table, re.sub(r'(?<!^)(?=[A-Z])', ' ', model.__name__))
        catalog.append({'id': table, 'label': label, 'group': _GROUPS.get(domain, 'Other data')})
    return sorted(catalog, key=lambda item: (item['group'], item['label']))


def _ids(values):
    if not isinstance(values, (list, tuple, set)):
        return set()
    result = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        text = str(value)
        if len(text) <= 19 and text.isascii() and text.isdigit() and 0 < int(text) <= 9223372036854775807:
            result.add(int(text))
    return result


def parse_record_permissions(user):
    try:
        value = json.loads(getattr(user, 'record_permissions_json', None) or '{}')
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    result = {}
    for table, grant in value.items():
        if not isinstance(grant, dict):
            continue
        write_ids = _ids(grant.get('write'))
        delete_ids = _ids(grant.get('delete'))
        result[table] = {
            'read': sorted(_ids(grant.get('read')) | write_ids | delete_ids),
            'write': sorted(write_ids), 'delete': sorted(delete_ids),
            'create': grant.get('create') is True,
        }
    return result


def _request_user():
    # Never recursively resolve Flask-Login while its user_loader is querying.
    if not has_request_context() or '_login_user' not in g:
        return None
    user = g.get('_login_user')
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    policy = g.get('_hdc_record_policy')
    if not policy or policy['user'] is not user:
        active = exact_access_enabled(user)
        g._hdc_record_policy = {'user': user, 'active': active}
        if active:
            g._hdc_exact_record_grants = parse_record_permissions(user)
    return user if g._hdc_record_policy['active'] else None


def _grants(user):
    user = _unwrap(user)
    if has_request_context() and g.get('_login_user') is user:
        if not hasattr(g, '_hdc_exact_record_grants'):
            g._hdc_exact_record_grants = parse_record_permissions(user)
        return g._hdc_exact_record_grants
    return parse_record_permissions(user)


def record_ids(user, table, mode='read'):
    """None means legacy/admin unrestricted; an empty set means no records."""
    if not exact_access_enabled(user):
        return None
    ids = set(_grants(user).get(table, {}).get(mode, []))
    if mode in ('read', 'write') and has_request_context() and _grants(user).get(table, {}).get('create'):
        ids |= g.get('_hdc_created_record_ids', {}).get(table, set())
    return ids


def record_allowed(user, table, row_id=None, mode='read'):
    if not exact_access_enabled(user):
        return True
    grant = _grants(user).get(table, {})
    if mode == 'create':
        return grant.get('create') is True and table in record_models()
    clean = _ids([row_id])
    return bool(clean and clean.issubset(record_ids(user, table, mode)))


def record_label(row):
    """Admin-only picker labels: stored columns only, no lazy/N+1 loads."""
    parts = [f'#{row.id}']
    columns = set(inspect(type(row)).columns.keys())
    for key in ('project_code', 'worker_code', 'staff_code', 'subcontractor_code', 'name',
                'original_name', 'reference_id', 'entry_type', 'description', 'notes'):
        if key in columns:
            value = getattr(row, key, None)
            if value not in (None, ''):
                parts.append(str(value)[:90])
                if len(parts) >= 4:
                    break
    for key in ('project_id', 'stage_id', 'worker_id', 'staff_id', 'subcontractor_id', 'account_id'):
        if key in columns and getattr(row, key, None):
            parts.append(f'{key.removesuffix("_id").replace("_", " ")} #{getattr(row, key)}')
    for key in ('date', 'check_in', 'amount', 'total', 'hours'):
        if key in columns:
            value = getattr(row, key, None)
            if value is not None:
                parts.append(value.isoformat() if isinstance(value, (date, datetime)) else f'{key}: {value}')
    return ' · '.join(parts)


def record_permissions_from_form(form):
    """Validate the admin's selections against existing rows, not just JSON."""
    result = {}
    for table, model in record_models().items():
        read_ids = _ids(form.getlist('record_read_' + table))
        write_ids = _ids(form.getlist('record_write_' + table))
        delete_ids = _ids(form.getlist('record_delete_' + table))
        all_ids = read_ids | write_ids | delete_ids
        valid = set()
        for start in range(0, len(all_ids), 400):
            chunk = sorted(all_ids)[start:start + 400]
            valid.update(row[0] for row in db.session.query(model.id).filter(model.id.in_(chunk)).all())
        create = form.get('record_create_' + table) == '1'
        if valid or create:
            result[table] = {'read': sorted(all_ids & valid), 'write': sorted(write_ids & valid),
                             'delete': sorted(delete_ids & valid), 'create': create}
    return result


def record_grant_cards(user, catalog=None):
    """Batch the saved picker rows once per model, not once per user/row."""
    catalog = catalog or record_catalog()
    models = record_models()
    result = []
    grants = parse_record_permissions(user)
    for resource in catalog:
        table = resource['id']
        grant = grants.get(table)
        if not grant or not (grant['read'] or grant['create']):
            continue
        rows = {}
        ids = grant['read']
        for start in range(0, len(ids), 400):
            rows.update({row.id: row for row in models[table].query.filter(
                models[table].id.in_(ids[start:start + 400])).all()})
        result.append({**resource, 'create': grant['create'], 'records': [
            {'id': rid, 'label': record_label(rows[rid]) if rid in rows else f'#{rid} (record no longer exists)',
             'read': True, 'write': rid in grant['write'], 'delete': rid in grant['delete']}
            for rid in ids]})
    return result


# Each numeric URL component is a read reference. Mutating the row additionally
# requires its Write/Delete grant at flush (including hidden side effects).
# Match ALL patterns so nested URLs check both their container and target.
_ROUTE_REFERENCES = [
    (r'^/hdc/projects/(\d+)(?:/|$)', 'hdc_project'),
    (r'^/hdc/projects/\d+/owner_payment/(\d+)(?:/|$)', 'hdc_owner_payment'),
    (r'^/hdc/stage/(\d+)(?:/|$)', 'hdc_stage'),
    (r'^/hdc/stage/drawing/(\d+)(?:/|$)', 'hdc_stage_drawing'),
    (r'^/hdc/(?:workers)/(\d+)(?:/|$)', 'hdc_worker'),
    (r'^/hdc/workers/\d+/(?:ledger|payment)/(\d+)(?:/|$)', 'hdc_labour_ledger'),
    (r'^/hdc/subcontractor/(\d+)(?:/|$)', 'hdc_subcontractor'),
    (r'^/hdc/subcontractor/\d+/workers/(\d+)(?:/|$)', 'hdc_subcontract_labour_worker'),
    (r'^/hdc/subcontractor/\d+/payment/(\d+)(?:/|$)', 'hdc_subcontract_payment'),
    (r'^/hdc/subcontractor/\d+/team_attendance/(\d+)(?:/|$)', 'hdc_subcontract_team_attendance'),
    (r'^/hdc/subcontractor/\d+/labour_attendance/(\d+)(?:/|$)', 'hdc_subcontract_labour_attendance'),
    (r'^/hdc/timekeeping/(\d+)(?:/|$)', 'hdc_time_entry'),
    (r'^/hdc/expenses/(\d+)(?:/|$)', 'hdc_expense'),
    (r'^/hdc/alerts/(\d+)(?:/|$)', 'hdc_alert'),
    (r'^/hdc/personal-management/expenses/(\d+)(?:/|$)', 'hdc_personal_expense'),
    (r'^/hdc/personal-management/categories/(\d+)(?:/|$)', 'hdc_personal_expense_category'),
    (r'^/hdc/office-management/staff/(\d+)(?:/|$)', 'hdc_office_staff'),
    (r'^/hdc/office-management/staff/\d+/(?:ledger|payment)/(\d+)(?:/|$)', 'hdc_office_staff_ledger'),
    (r'^/hdc/office-management/staff/\d+/allowances/(\d+)(?:/|$)', 'hdc_staff_allowance'),
    (r'^/hdc/office-management/expenses/(\d+)(?:/|$)', 'hdc_office_expense'),
    (r'^/hdc/office-management/allowance-categories/(\d+)(?:/|$)', 'hdc_allowance_category'),
    (r'^/hdc/accounts/(\d+)(?:/|$)', 'hdc_account'),
    (r'^/api/accounts/account/(\d+)(?:/|$)', 'hdc_account'),
    (r'^/hdc/accounts/transactions/(\d+)(?:/|$)', 'hdc_account_txn'),
    (r'^/hdc/accounts/shared/expenses/(\d+)(?:/|$)', 'hdc_shared_expense'),
    (r'^/(?:hdc/purchase-v2|api/v2/purchase)/materials/(\d+)(?:/|$)', 'hdc_material_v2'),
    (r'^/(?:hdc/purchase-v2|api/v2/purchase)/suppliers/(\d+)(?:/|$)', 'hdc_supplier'),
    (r'^/(?:hdc/purchase-v2|api/v2/purchase)/purchases/(\d+)(?:/|$)', 'hdc_purchase_v2'),
    (r'^/(?:hdc/purchase-v2/delivered|api/v2/purchase/deliveries)/(\d+)(?:/|$)', 'hdc_delivery'),
    (r'^/(?:hdc/purchase-v2|api/v2/purchase)/usage/(\d+)(?:/|$)', 'hdc_usage_log_v2'),
    (r'^/hdc/tool-rental/(?:inventory|tool)/(\d+)(?:/|$)', 'hdc_tool'),
    (r'^/hdc/tool-rental/category/(\d+)(?:/|$)', 'hdc_tool_category'),
    (r'^/hdc/tool-rental/(\d+)(?:/|$)', 'hdc_tool_rental'),
    (r'^/hdc/tool-rental/payment/(\d+)(?:/|$)', 'hdc_tool_rental_payment'),
    (r'^/hdc/api/tool-rental/rental/(\d+)(?:/|$)', 'hdc_tool_rental'),
    (r'^/hdc/payroll/(\d+)(?:/|$)', 'hdc_payroll_run'),
    (r'^/hdc/reports/project/(\d+)(?:/|$)', 'hdc_project'),
    (r'^/project-estimation/convert/(\d+)(?:/|$)', 'hdc_estimation'),
    (r'^/hdc/api/project_stages/(\d+)(?:/|$)', 'hdc_project'),
    (r'^/hdc/api/material_stock/(\d+)(?:/|$)', 'hdc_material'),
    (r'^/hdc/api/formula_vars/(\d+)(?:/|$)', 'hdc_custom_formula'),
    (r'^/hdc/api/office_expense_categories/(\d+)(?:/|$)', 'hdc_office_expense_category'),
]
_ROUTE_REFERENCES = [(re.compile(pattern), table) for pattern, table in _ROUTE_REFERENCES]
_REFERENCE_FIELDS = {
    'project_id': 'hdc_project', 'from_project_id': 'hdc_project', 'to_project_id': 'hdc_project',
    'stage_id': 'hdc_stage', 'from_stage_id': 'hdc_stage', 'to_stage_id': 'hdc_stage',
    'worker_id': 'hdc_worker', 'worker_ids': 'hdc_worker', 'trade_id': 'hdc_worker_trade',
    'staff_id': 'hdc_office_staff', 'subcontractor_id': 'hdc_subcontractor',
    'subcontractor_ids': 'hdc_subcontractor', 'definition_ids': 'hdc_stage_definition',
    'def_id': 'hdc_stage_definition', 'account_id': 'hdc_account',
    'from_account_id': 'hdc_account', 'to_account_id': 'hdc_account',
    'destination_account_id': 'hdc_account', 'received_to_account_id': 'hdc_account',
    'executed_by_account_id': 'hdc_account', 'source_account_id': 'hdc_account',
    'received_in_account_id': 'hdc_account', 'purchase_id': 'hdc_purchase_v2',
    'supplier_id': 'hdc_supplier', 'delivery_id': 'hdc_delivery',
    'usage_id': 'hdc_usage_log_v2', 'tool_id': 'hdc_tool', 'rental_id': 'hdc_tool_rental',
    'loan_id': 'hdc_loan', 'txn_id': 'hdc_account_txn', 'entry_id': 'hdc_cash_flow_entry',
}


def _deny():
    abort(403, description='This record or action is outside your assigned access.')


def _check_reference(user, table, value):
    if value in (None, '', 0, '0'):
        return
    values = value if isinstance(value, (list, tuple)) else str(value).split(',')
    for part in values:
        if part in (None, '', 0, '0'):
            continue
        if not record_allowed(user, table, part):
            _deny()


def _check_payload(user, payload, depth=0):
    if depth > 12:
        _deny()
    if isinstance(payload, dict):
        for kind_field, id_field, resolver in (
                ('related_entity_type', 'related_entity_id', related_record_table),
                ('source_type', 'source_id', source_record_table)):
            value = payload.get(id_field)
            if isinstance(value, list):
                value = value[0] if len(value) == 1 else (None if not value else value)
            if value not in (None, '', 0, '0'):
                kind = payload.get(kind_field, '')
                if isinstance(kind, list):
                    kind = kind[0] if len(kind) == 1 else ''
                table = resolver(kind)
                if not table:
                    _deny()
                _check_reference(user, table, value)
        for key, value in payload.items():
            table = _REFERENCE_FIELDS.get(key)
            if key == 'material_id':
                table = 'hdc_material' if request.path.startswith('/hdc/materials') or request.path == '/hdc/purchases' else 'hdc_material_v2'
            if key == 'party_id':
                table = 'hdc_shared_party' if '/shared/' in request.path else 'hdc_cash_flow_party'
            if table:
                _check_reference(user, table, value)
            if isinstance(value, (dict, list)):
                _check_payload(user, value, depth + 1)
    elif isinstance(payload, list):
        for value in payload:
            _check_payload(user, value, depth + 1)


def enforce_record_request(user):
    # Freeze authorization before the handler can alter the ORM auth object.
    _request_user()
    if not exact_access_enabled(user):
        return
    target = None
    for matcher, table in _ROUTE_REFERENCES:
        match = matcher.search(request.path)
        if match:
            target = (table, match.group(1))
            if not record_allowed(user, table, match.group(1)):
                _deny()
    # Check common row actions before handler side effects (notably files),
    # while the flush guard also catches less obvious/multiple-row changes.
    operation = request.path.rstrip('/').rsplit('/', 1)[-1]
    mode = None
    if request.method == 'DELETE' or operation in ('delete', 'void', 'remove', 'suspend'):
        mode = 'delete'
    elif request.method in ('PUT', 'PATCH') or operation in (
            'edit', 'replace', 'status', 'progress', 'toggle', 'restore', 'reactivate', 'activate'):
        mode = 'write'
    if target and mode and not record_allowed(user, *target, mode=mode):
        _deny()
    code_targets = {'hdc_api_next_worker_code': 'hdc_worker', 'hdc_api_next_office_staff_code': 'hdc_office_staff'}
    if request.endpoint in code_targets and not record_allowed(user, code_targets[request.endpoint], mode='create'):
        _deny()
    if request.endpoint == 'hdc_stage_drawings_upload' and not record_allowed(user, 'hdc_stage_drawing', mode='create'):
        _deny()
    cost_match = re.match(r'^/hdc/cost-entries/(project|stage)/(\d+)(?:/|$)', request.path)
    if cost_match and not record_allowed(user, 'hdc_' + cost_match.group(1), cost_match.group(2)):
        _deny()
    _check_payload(user, request.args.to_dict(flat=False))
    _check_payload(user, request.form.to_dict(flat=False))
    if request.is_json:
        _check_payload(user, request.get_json(silent=True) or {})
    # Bulk attendance uses a JSON string inside a regular form field.
    for field in ('entries_json', 'entries', 'payload', *[key for key in request.form if key.startswith('allocations_')]):
        raw = request.form.get(field)
        if raw and raw.lstrip().startswith(('[', '{')):
            try:
                _check_payload(user, json.loads(raw))
            except (TypeError, ValueError):
                _deny()


def readonly_exact_request():
    from flask_login import current_user
    return (has_request_context() and request.method in ('GET', 'HEAD', 'OPTIONS') and
            exact_access_enabled(current_user))


def integrity_message(generic, detailed):
    from flask_login import current_user
    return generic if has_request_context() and exact_access_enabled(current_user) else detailed


def integrity_query(query):
    """Trusted scalar-only checks, never data shown to a restricted user.

    A hidden child must still stop an unsafe deletion, overdraft or duplicate
    allocation. Callers may obtain counts/IDs/aggregates for validation, but
    cannot load unassigned ORM objects with this flag. It never bypasses the
    mutation guard. This option is set by server code, not request input.
    """
    return query.execution_options(hdc_integrity_scalar=True, hdc_skip_access_scope=True)


_SOURCE_PREFIXES = {
    'hdc_expense': ('expense',), 'hdc_owner_payment': ('owner_payment',),
    'hdc_personal_expense': ('personal_expense',),
    'hdc_labour_ledger': ('labour_ledger_', 'worker_payment'),
    'hdc_office_staff_ledger': ('office_staff_ledger_',), 'hdc_office_expense': ('office_expense',),
    'hdc_supplier_ledger': ('supplier_credit_',), 'hdc_purchase_v2': ('purchase_v2_paid',),
    'hdc_subcontract_payment': ('subcontract_payment_',),
    'hdc_subcontract_labour_payment': ('subcontract_labour_payment',),
    'hdc_tool_rental_payment': ('tool_rental_payment',),
    'hdc_shared_expense': ('shared_expense',), 'hdc_shared_settlement': ('shared_settlement',),
    'hdc_cash_flow_entry': ('cash_flow_entry_',),
}


def source_record_table(kind):
    kind = str(kind or '').strip().lower()
    references = {**_SOURCE_PREFIXES, 'hdc_loan': ('loan',), 'hdc_account_txn': ('account_reversal',)}
    return next((table for table, prefixes in references.items()
                 if any(kind.startswith(prefix) for prefix in prefixes)), None)


def related_record_table(kind):
    aliases = {
        'worker': 'hdc_worker', 'labour': 'hdc_worker', 'labor': 'hdc_worker',
        'supplier': 'hdc_supplier', 'vendor': 'hdc_supplier',
        'subcontractor': 'hdc_subcontractor', 'sub_contractor': 'hdc_subcontractor', 'sub': 'hdc_subcontractor',
        'office_staff': 'hdc_office_staff', 'office-staff': 'hdc_office_staff', 'officestaff': 'hdc_office_staff',
        'staff': 'hdc_office_staff', 'project': 'hdc_project',
        'subcontractor_labour_worker': 'hdc_subcontract_labour_worker',
        'account': 'hdc_account', 'client': 'hdc_account', 'loan': 'hdc_loan',
        'party': 'hdc_cash_flow_party', 'cash_flow_party': 'hdc_cash_flow_party',
    }
    return aliases.get(str(kind or '').strip().lower())


def record_code_query(query, table):
    # Namespace uniqueness is not the user's visible record list. Only a
    # permitted creator may use the complete scalar code/id namespace.
    user = _request_user()
    if user and not record_allowed(user, table, mode='create'):
        return query.filter(false())
    return integrity_query(query)


def require_complete_grant(user, model, query, mode='read'):

    """A hidden linked row must not be mistaken for a nonexistent row."""
    all_ids = {row[0] for row in integrity_query(query).all()}
    if not all_ids:
        return
    granted = record_ids(user, model.__tablename__, mode)
    if granted is None:
        return
    visible = {row[0] for row in query.all()}
    if not all_ids.issubset(granted & visible):
        _deny()


def _require_financial_links(user, obj, state):
    table = state.mapper.local_table.name
    prefixes = _SOURCE_PREFIXES.get(table, ())
    models = record_models()
    for mirror_table in ('hdc_account_txn', 'hdc_cash_flow_entry') if prefixes and state.identity else ():
        model = models[mirror_table]
        source = func.lower(func.coalesce(model.source_type, ''))
        query = db.session.query(model.id).filter(
            model.source_id == state.identity[0], or_(*[source.startswith(prefix, autoescape=True) for prefix in prefixes]))
        require_complete_grant(user, model, query)
    if table in ('hdc_account_txn', 'hdc_cash_flow_entry') and getattr(obj, 'source_id', None):
        source_table = source_record_table(obj.source_type)
        if not source_table or not record_allowed(user, source_table, obj.source_id):
            _deny()
    if table == 'hdc_account_txn':
        group_ids = {obj.group_id} | set(state.attrs.group_id.history.deleted)
        group_ids -= {None, ''}
        if group_ids:
            model = models[table]
            require_complete_grant(user, model, db.session.query(model.id).filter(model.group_id.in_(group_ids)))
    if table == 'hdc_purchase_v2' and state.identity:
        model = models['hdc_supplier_ledger']
        require_complete_grant(user, model, db.session.query(model.id).filter(
            model.reference_type == 'purchase_v2', model.reference_id == state.identity[0]))
    if table in ('hdc_cash_day_lock', 'hdc_cash_day_position', 'hdc_account_reconciliation'):
        from hdc.utils.dates import _pkt_today
        day = getattr(obj, {'hdc_cash_day_lock': 'lock_date', 'hdc_cash_day_position': 'position_date',
                            'hdc_account_reconciliation': 'reconciliation_date'}[table]) or _pkt_today()
        account = models['hdc_account']
        accounts = db.session.query(account.id).filter(account.is_void == False,
            func.lower(account.type).in_(('cash', 'bank', 'company')))
        if table != 'hdc_cash_day_lock':
            accounts = accounts.filter(account.id == obj.account_id)
        require_complete_grant(user, account, accounts)
        account_ids = [row[0] for row in accounts.all()]
        for history_table, date_column in (
                ('hdc_account_txn', 'date'), ('hdc_cash_flow_entry', 'date_posted'),
                ('hdc_cash_day_position', 'position_date'), ('hdc_account_reconciliation', 'reconciliation_date')):
            model = models[history_table]
            if history_table == 'hdc_account_txn':
                scope = or_(model.from_account_id.in_(account_ids), model.to_account_id.in_(account_ids))
            else:
                scope = model.account_id.in_(account_ids)
            require_complete_grant(user, model, db.session.query(model.id).filter(
                scope, func.date(getattr(model, date_column)) <= day.isoformat()))
    if table in ('hdc_loan', 'hdc_loan_movement'):

        model = models['hdc_loan_movement']
        loan_id = obj.loan_id if table == 'hdc_loan_movement' else (state.identity[0] if state.identity else None)
        if loan_id:
            require_complete_grant(user, model, db.session.query(model.id).filter(model.loan_id == loan_id))
    if table == 'hdc_stage' and state.identity and any(state.attrs[key].history.has_changes() for key in (
            'status', 'execution_mode', 'assigned_subcontractor_id', 'progress')):
        model = models['hdc_subcontractor']
        require_complete_grant(user, model, db.session.query(model.id).filter(model.stage_id == state.identity[0]))
    # A time entry's work-ledger mirror is linked by a real FK, not source_type.
    if table == 'hdc_time_entry' and state.identity:
        model = models['hdc_labour_ledger']
        require_complete_grant(user, model, db.session.query(model.id).filter(
            model.time_entry_id == state.identity[0]))
    # Explicit journal/mirror pointers must remain assignable as well.
    for field in ('cf_entry_id', 'source_entry_id', 'account_tx_id', 'account_txn_id', 'cash_flow_entry_id', 'attendance_day_id'):
        if field not in state.mapper.columns or not getattr(obj, field, None):
            continue
        column = state.mapper.columns[field]
        for fk in column.foreign_keys:
            if not record_allowed(user, fk.column.table.name, getattr(obj, field)):
                _deny()


def _require_time_history(user, obj, state):
    """Attendance recalculation must never silently omit hidden daily rows."""
    if state.mapper.local_table.name != 'hdc_time_entry':
        return
    models = record_models()
    entry_model = models['hdc_time_entry']
    dates = {obj.check_in.date()} if obj.check_in else set()
    dates.update(value.date() for value in state.attrs.check_in.history.deleted if value)
    worker_ids = {obj.worker_id} | set(state.attrs.worker_id.history.deleted)
    for worker_id in worker_ids - {None}:
        if not record_allowed(user, 'hdc_worker', worker_id):
            _deny()
        for day in dates:
            start = datetime.combine(day, datetime.min.time())
            query = db.session.query(entry_model.id).filter(
                entry_model.worker_id == worker_id, entry_model.is_void == False,
                entry_model.check_in >= start, entry_model.check_in < start + timedelta(days=1))
            require_complete_grant(user, entry_model, query)
        rate_model = models['hdc_worker_rate']
        for day in dates:
            query = db.session.query(rate_model.id).filter(rate_model.worker_id == worker_id,
                rate_model.effective_from <= day).order_by(rate_model.effective_from.desc(), rate_model.id.desc()).limit(1)
            require_complete_grant(user, rate_model, query)



def _validate_pending_cash_balances(session):
    """Validate source edits/voids as well as the dedicated posting endpoints.

    Only scalar account metadata and actual balances are consulted. No hidden
    account object, transaction or total is returned to the caller.
    """
    from hdc.utils.money import to_minor
    deltas = {}
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        state = inspect(obj)
        if state.mapper.local_table.name != 'hdc_account_txn':
            continue
        if obj not in session.new and obj not in session.deleted and not session.is_modified(obj, include_collections=False):
            continue
        def original(key):
            history = state.attrs[key].history
            return history.deleted[0] if history.deleted else getattr(obj, key, None)
        def apply(from_id, to_id, amount, sign):
            minor = to_minor(amount or 0)
            for account_id, direction in ((from_id, -1), (to_id, 1)):
                if account_id:
                    deltas[int(account_id)] = deltas.get(int(account_id), 0) + sign * direction * minor
        if obj not in session.new and not original('is_void'):
            apply(original('from_account_id'), original('to_account_id'), original('amount'), -1)
        if obj not in session.deleted and not obj.is_void:
            apply(obj.from_account_id, obj.to_account_id, obj.amount, 1)
    decreasing = {key for key, value in deltas.items() if value < 0}
    if not decreasing:
        return
    account = record_models()['hdc_account']
    money_ids = {row[0] for row in integrity_query(db.session.query(account.id).filter(
        account.id.in_(decreasing), account.is_void == False,
        func.lower(account.type).in_(('cash', 'bank', 'company')))).all()}
    if not money_ids:
        return
    from hdc.services.accounts import _account_balance_map
    balances = _account_balance_map(integrity=True, account_ids=money_ids)
    if any(to_minor(balances.get(aid, 0)) + deltas[aid] < 0 for aid in money_ids):
        abort(403, description='Insufficient balance. This change is blocked.')


def _require_visible_dependents(user, obj, state, mode):
    """Deletion must not bypass hidden references/cascade safety checks."""
    parent_table = state.mapper.local_table.name
    parent_id = state.identity[0]
    for table, model in record_models().items():
        if mode == 'read' and table == 'hdc_cash_flow_entry_audit':
            continue
        for column in model.__table__.columns:
            if not any(fk.column.table.name == parent_table and fk.column.name == 'id' for fk in column.foreign_keys):
                continue
            allowed = record_ids(user, table, mode)
            query = db.session.query(model.id).filter(getattr(model, column.key) == parent_id)
            if allowed:
                query = query.filter(model.id.not_in(allowed))
            if integrity_query(query.limit(1)).first() is not None:
                _deny()


def _audit_criterion(model, user):
    table = model.__tablename__
    if table == 'hdc_user':
        return model.id == _authenticated_id(user)
    if table == 'hdc_user_activity':
        clauses = [and_(model.entity_type == table, model.entity_id.in_([str(i) for i in grant['read']]))
                   for table, grant in _grants(user).items() if grant['read'] and table in record_models()]
        return or_(*clauses) if clauses else false()
    return false()


def _voiding(state):
    for key in ('is_void', 'voided_at'):
        if key in state.attrs:
            history = state.attrs[key].history
            if history.has_changes() and history.added and history.added[0]:
                return True
    if 'status' in state.attrs:
        history = state.attrs.status.history
        if history.has_changes() and history.added and str(history.added[0]).lower() in (
                'void', 'voided', 'cancelled', 'canceled', 'deleted'):
            return True
    return False


def _validate_foreign_keys(user, obj, state, *, creating):
    if 'related_entity_id' in state.mapper.columns and (creating or any(
            state.attrs[key].history.has_changes() for key in ('related_entity_id', 'related_entity_type'))):
        value = getattr(obj, 'related_entity_id', None)
        if value:
            table = related_record_table(getattr(obj, 'related_entity_type', None))
            if not table or not record_allowed(user, table, value):
                _deny()
    if state.mapper.local_table.name == 'hdc_supplier_ledger' and getattr(obj, 'reference_type', '') == 'purchase_v2':
        if not record_allowed(user, 'hdc_purchase_v2', obj.reference_id):
            _deny()
        model = record_models()['hdc_supplier_ledger']
        require_complete_grant(user, model, db.session.query(model.id).filter(
            model.reference_type == 'purchase_v2', model.reference_id == obj.reference_id))
    for column in state.mapper.columns:
        if not column.foreign_keys:
            continue
        if not creating and not state.attrs[column.key].history.has_changes():
            continue
        value = getattr(obj, column.key, None)
        if value is None:
            continue
        for foreign_key in column.foreign_keys:
            table = foreign_key.column.table.name
            if table == 'hdc_user':
                if int(value) != _authenticated_id(user):
                    _deny()
            elif table not in _AUTH_TABLES and not record_allowed(user, table, value):
                # A just-created referenced row is allowed only within this
                # transaction, if its type has an explicit Create grant.
                pending_ids = g.get('_hdc_created_record_ids', {}).get(table, set())
                if value not in pending_ids:
                    _deny()


_INSTALLED = False


def install_record_permission_scope():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    @event.listens_for(Session, 'do_orm_execute')
    def _scope_exact_records(execute_state):
        # commit()/rollback() expire the logged-in ORM instance. Refreshing
        # only that authenticated user's columns must not consult its expired
        # role again and recursively start the same refresh. Other models and
        # other users' refreshes still pass through the row filter.
        if execute_state.is_column_load and has_request_context():
            authenticated = g.get('_login_user')
            mapper = execute_state.bind_mapper
            params = execute_state.parameters
            authenticated_state = inspect(authenticated, raiseerr=False) if authenticated is not None else None
            identity = authenticated_state.identity if authenticated_state is not None else None
            if (mapper is not None and mapper.local_table.name == 'hdc_user' and identity and
                    isinstance(params, dict) and len(params) == 1 and
                    list(params.values()) == [identity[0]]):
                return
        user = _request_user()
        if user is None:
            return
        if execute_state.session.is_modified(user, include_collections=False):
            _deny()
        # No raw/Core SQL escape hatch in an operational strict-user request.
        # All current operational data paths use ORM queries; administration
        # and migrations are not affected by this request-local restriction.
        if not execute_state.is_orm_statement or not execute_state.is_select:
            # Bulk DML bypasses instance/FK/void checks. Current operational
            # writers use normal ORM instances; unreviewed bulk paths fail shut.
            _deny()
        if execute_state.execution_options.get('hdc_integrity_scalar'):
            for description in execute_state.statement.column_descriptions:
                entity = inspect(description.get('expr'), raiseerr=False)
                if entity is not None and (getattr(entity, 'is_mapper', False) or getattr(entity, 'is_aliased_class', False)):
                    _deny()
            return
        if execute_state.is_column_load:
            mapper = execute_state.bind_mapper
            state = execute_state.load_options._refresh_state
            if mapper is None or state is None or not state.identity:
                _deny()
            table = mapper.local_table.name
            if table == 'hdc_user_activity':
                model = mapper.class_
                if db.session.query(model.id).filter(model.id == state.identity[0]).scalar() is None:
                    _deny()
            elif table in _AUTH_TABLES or not record_allowed(user, table, state.identity[0]):
                _deny()
        mode = 'read'
        statement = execute_state.statement
        for mapper in db.Model.registry.mappers:
            model = mapper.class_
            table = getattr(model, '__tablename__', '')
            if table in _AUTH_TABLES:
                criterion = _audit_criterion(model, user) if mode == 'read' else false()
            elif 'id' in model.__dict__:
                ids = record_ids(user, table, mode)
                criterion = model.id.in_(ids) if ids else false()
            else:
                criterion = false()
            statement = statement.options(with_loader_criteria(model, criterion, include_aliases=True))
        execute_state.statement = statement

    @event.listens_for(Session, 'before_flush')
    def _guard_exact_record_changes(session, flush_context, instances):
        user = _request_user()
        if user is None:
            return
        from hdc.services.permissions import may_access_path, permission_for
        for obj in list(session.new) + list(session.dirty) + list(session.deleted):
            state = inspect(obj)
            table = state.mapper.local_table.name
            creating = obj in session.new
            deleting = obj in session.deleted
            if not creating and not deleting and not session.is_modified(obj, include_collections=False):
                continue
            # Append-only audit records are an internal effect of an authorized
            # action. This exemption never permits changing old audit/auth rows.
            if table in ('hdc_user_activity', 'hdc_activity_log') and creating:
                if getattr(obj, 'user_id', None) != _authenticated_id(user):
                    _deny()
                continue
            if table in _AUTH_TABLES or may_access_path(user, request.path, 'write') is not True:
                _deny()
            mode = 'create' if creating else 'delete' if deleting or _voiding(state) else 'write'
            original_id = state.identity[0] if state.identity else None
            if not record_allowed(user, table, original_id, mode):
                _deny()
            if creating and getattr(obj, 'id', None) is not None and _positive_id(obj.id) is None:
                _deny()
            if not creating and getattr(obj, 'id', None) != original_id:
                _deny()  # A grant must never be moved to a different primary key.
            if table == 'hdc_project' and not permission_for(user, 'project_financials', 'write'):
                defaults = {'total_constructed_sqft': 0, 'owner_rate_per_sqft': 0,
                            'owner_lump_sum': 0, 'budget_total': 0, 'contract_type': 'sqft'}
                for key, default in defaults.items():
                    if (creating and getattr(obj, key, None) not in (None, default)) or (
                            not creating and state.attrs[key].history.has_changes()):
                        _deny()
            if table in ('hdc_account_txn', 'hdc_cash_flow_entry'):
                from hdc.utils.dates import _pkt_today
                field = 'date' if table == 'hdc_account_txn' else 'date_posted'
                values = [getattr(obj, field, None), *state.attrs[field].history.deleted]
                lock_model = record_models()['hdc_cash_day_lock']
                for value in values:
                    day = value.date() if isinstance(value, datetime) else (value or _pkt_today())
                    if integrity_query(db.session.query(lock_model.id).filter(lock_model.lock_date == day).limit(1)).first():
                        _deny()
            _require_time_history(user, obj, state)
            _require_financial_links(user, obj, state)
            if not creating and mode == 'delete':
                _require_visible_dependents(user, obj, state, 'delete' if deleting else 'read')
            _validate_foreign_keys(user, obj, state, creating=creating)
        _validate_pending_cash_balances(session)
        # Retain the authorized new instances so generated IDs can be used by
        # explicitly authorized bookkeeping in the same transaction only.
        g._hdc_new_records = list(session.new)

    @event.listens_for(Session, 'after_transaction_create')
    def _snapshot_creation_grants(session, transaction):
        if transaction.nested and has_request_context():
            transaction._hdc_created_before_savepoint = {
                table: set(ids) for table, ids in g.get('_hdc_created_record_ids', {}).items()}

    @event.listens_for(Session, 'after_soft_rollback')
    def _restore_surviving_creation_grants(session, transaction):
        if transaction.nested and has_request_context():
            g._hdc_created_record_ids = getattr(transaction, '_hdc_created_before_savepoint', {})

    @event.listens_for(Session, 'after_rollback')
    def _forget_rolled_back_records(session):
        if has_request_context():
            g.pop('_hdc_new_records', None)
            g.pop('_hdc_created_record_ids', None)

    @event.listens_for(Session, 'after_flush_postexec')
    def _remember_created_records(session, flush_context):
        if _request_user() is None:
            return
        created = g.setdefault('_hdc_created_record_ids', {})
        for row in g.pop('_hdc_new_records', []):
            table = getattr(row, '__tablename__', '')
            if table not in _AUTH_TABLES and getattr(row, 'id', None) is not None:
                created.setdefault(table, set()).add(row.id)


def install_record_template_helpers(application):
    from flask_login import current_user
    application.jinja_env.globals['hdc_record_allowed'] = lambda table, row_id=None, mode='read': record_allowed(
        current_user, table, row_id, mode)
    application.jinja_env.globals['hdc_exact_access'] = lambda: exact_access_enabled(current_user)
