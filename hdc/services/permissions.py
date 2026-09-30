"""Per-user page and stage access control.

Permissions are intentionally server-side.  The sidebar uses this same catalog
for visibility, while the request guard and ORM query scope enforce it even if
a user guesses a URL or calls an endpoint directly.
"""
import json
import re

from flask import current_app, g, has_request_context, request
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

# Tree keys are stable storage identifiers. Paths use regexes because several
# pages have numeric route parameters; the longest matching expression wins.
PAGE_TREE = [
    {'id': 'overview', 'label': 'Overview', 'children': [
        {'id': 'dashboard', 'label': 'Dashboard', 'paths': [r'^/hdc/$', r'^/hdc/kpi(?:/|$)']},
        {'id': 'alerts', 'label': 'Alerts', 'paths': [r'^/hdc/alerts(?:/|$)']},
    ]},
    {'id': 'projects_section', 'label': 'Projects & Estimation', 'children': [
        {'id': 'projects', 'label': 'Projects', 'paths': [r'^/hdc/projects$']},
        {'id': 'project_create', 'label': 'Create project', 'paths': [r'^/hdc/projects/add$', r'^/hdc/api/next_project_code$']},
        {'id': 'project_costs', 'label': 'Project cost drilldowns', 'paths': [r'^/hdc/cost-entries(?:/|$)']},
        {'id': 'project_detail', 'label': 'Project details & edit', 'paths': [r'^/hdc/projects/\d+(?:/|$)']},
        {'id': 'project_financials', 'label': 'Project financial summaries & pricing fields', 'paths': []},
        {'id': 'project_receipts', 'label': 'Project owner receipts & payments', 'paths': [r'^/hdc/projects/\d+/owner_payment(?:/|$)']},
        {'id': 'stages', 'label': 'Stages', 'paths': [r'^/hdc/stages$', r'^/hdc/api/project_stages/']},
        {'id': 'stage_create', 'label': 'Add / edit / remove stages', 'paths': [r'^/hdc/projects/\d+/(?:stage/add|bulk_stages)$', r'^/hdc/stage/\d+/(?:edit|delete|status|sub-progress)$']},
        {'id': 'stage_ledger', 'label': 'Stage ledger', 'paths': [r'^/hdc/stage/\d+/ledger$']},
        {'id': 'stage_drawings', 'label': 'Stage drawings', 'paths': [r'^/hdc/stage/(?:\d+/drawings|drawing/\d+)(?:/|$)']},
        {'id': 'stage_library', 'label': 'Stage library', 'paths': [r'^/hdc/stage-library$']},
        {'id': 'estimation', 'label': 'Estimation', 'paths': [r'^/hdc/estimation(?:/|$)', r'^/project-estimation(?:/|$)', r'^/hdc/api/formula_vars(?:/|$)']},
        {'id': 'project_estimation', 'label': 'Project estimation', 'paths': [r'^/hdc/project-estimation(?:/|$)']},
    ]},
    {'id': 'workforce_section', 'label': 'Workforce', 'children': [
        {'id': 'subcontractors', 'label': 'Subcontractors', 'paths': [r'^/hdc/subcontractors$', r'^/hdc/projects/\d+/add_subcontractor$']},
        {'id': 'subcontractor_attendance', 'label': 'Subcontractor attendance & progress', 'paths': [r'^/hdc/subcontractor/\d+/(?:attendance|labour_attendance|team_attendance|attendance_page|progress)(?:/|$)', r'^/hdc/stage/\d+/shift/']},
        {'id': 'subcontractor_workers', 'label': 'Subcontractor teams & worker ledgers', 'paths': [r'^/hdc/subcontractor/\d+/workers(?:/|$)']},
        {'id': 'subcontractor_payments', 'label': 'Subcontractor payments & ledger', 'paths': [r'^/hdc/subcontractor/\d+/(?:pay|payments|ledger|events)(?:/|$)', r'^/hdc/subcontractor/\d+/payment/']},
        {'id': 'workers', 'label': 'Workers', 'paths': [r'^/hdc/workers$', r'^/hdc/workers/\d+/(?:toggle|edit)$', r'^/hdc/api/next_worker_code$']},
        {'id': 'worker_ledgers', 'label': 'Worker ledgers, rates & payments', 'paths': [r'^/hdc/workers/\d+/(?:ledger|advance|payment|rate)(?:/|$)']},
        {'id': 'trades', 'label': 'Trades', 'paths': [r'^/hdc/trades$']},
        {'id': 'timekeeping', 'label': 'Timekeeping & attendance', 'paths': [r'^/hdc/(?:timekeeping|attendance)$']},
        {'id': 'timekeeping_records', 'label': 'Timekeeping status & corrections', 'paths': [r'^/hdc/timekeeping/(?:status|\d+/(?:edit|delete|reactivate))(?:/|$)']},
        {'id': 'payroll', 'label': 'Payroll overview', 'paths': [r'^/hdc/payroll$']},
        {'id': 'payroll_generation', 'label': 'Payroll generation & deletion', 'paths': [r'^/hdc/payroll/(?:generate|\d+/delete)$']},
        {'id': 'payroll_history', 'label': 'Payroll history & salary cards', 'paths': [r'^/hdc/payroll/(?:history|salary-cards)(?:/|$)', r'^/hdc/payroll/\d+/salary-cards$']},
    ]},
    {'id': 'costs_section', 'label': 'Costs & Overheads', 'children': [
        {'id': 'expenses', 'label': 'Expenses', 'paths': [r'^/hdc/expenses(?:/|$)']},
        {'id': 'expense_categories', 'label': 'Expense categories', 'paths': [r'^/hdc/expense_categories$']},
        {'id': 'office_management', 'label': 'Office management overview', 'paths': [r'^/hdc/office-management$']},
        {'id': 'office_staff', 'label': 'Office staff directory & profiles', 'paths': [r'^/hdc/office-management/staff(?:$|/(?!ledger|attendance|\d+/ledger))', r'^/hdc/office-management/staff/\d+/(?:edit|toggle)$', r'^/hdc/api/next_office_staff_code$']},
        {'id': 'office_staff_ledger', 'label': 'Office staff ledgers, payments & receipts', 'paths': [r'^/hdc/office-management/staff/(?:ledger|\d+/(?:ledger|payment|ledger/\d+))(?:/|$)']},
        {'id': 'office_attendance', 'label': 'Office staff attendance', 'paths': [r'^/hdc/office-management/staff/attendance$']},
        {'id': 'office_expenses', 'label': 'Office expenses', 'paths': [r'^/hdc/office-management/expenses(?:/|$)', r'^/hdc/api/office_expense_categories(?:/|$)']},
        {'id': 'office_allowances', 'label': 'Office staff allowances', 'paths': [r'^/hdc/office-management/(?:allowance-categories|staff/\d+/allowances)(?:/|$)']},
        {'id': 'personal_management', 'label': 'Personal management', 'paths': [r'^/hdc/personal-management(?:/|$)']},
    ]},
    {'id': 'materials_section', 'label': 'Materials & Tools', 'children': [
        {'id': 'purchases', 'label': 'Purchases overview', 'paths': [r'^/hdc/purchase-v2$', r'^/hdc/purchases$', r'^/hdc/materials$', r'^/api/v2/purchase$']},
        {'id': 'purchase_materials', 'label': 'Purchase materials & stock', 'paths': [r'^/hdc/purchase-v2/materials(?:/|$)', r'^/hdc/purchase-v2/stock$', r'^/hdc/materials(?:/|$)', r'^/hdc/api/material_stock/', r'^/api/v2/purchase/(?:materials|material-stock|material-stock-scope|material-available)(?:/|$)']},
        {'id': 'purchase_suppliers', 'label': 'Purchase suppliers & supplier ledger', 'paths': [r'^/hdc/purchase-v2/suppliers(?:/|$)', r'^/api/v2/purchase/(?:suppliers|supplier-ledger)(?:/|$)']},
        {'id': 'purchase_orders', 'label': 'Purchase orders & payments', 'paths': [r'^/hdc/purchase-v2/purchases(?:/|$)', r'^/api/v2/purchase/(?:purchases|payments|kpis|recalculate-stock|usage-po-options)(?:/|$)']},
        {'id': 'purchase_deliveries', 'label': 'Deliveries & transfers', 'paths': [r'^/hdc/purchase-v2/delivered(?:/|$)', r'^/api/v2/purchase/deliveries(?:/|$)']},
        {'id': 'purchase_usage', 'label': 'Stage material usage', 'paths': [r'^/hdc/purchase-v2/usage(?:/|$)', r'^/hdc/materials/usage(?:/|$)', r'^/api/v2/purchase/usage(?:/|$)']},
        {'id': 'tools', 'label': 'HDC tools dashboard & rentals', 'paths': [r'^/hdc/tool-rental(?:/|$)', r'^/hdc/api/tool-rental/']},
        {'id': 'tool_inventory', 'label': 'Tool inventory, purchase & scrap', 'paths': [r'^/hdc/tool-rental/inventory(?:/|$)', r'^/hdc/tool-rental/category/']},
        {'id': 'tool_tracking', 'label': 'Tool tracking & reports', 'paths': [r'^/hdc/tool-rental/(?:tracking|reports)(?:/|$)']},
    ]},
    {'id': 'parties_section', 'label': 'Parties', 'children': [
        {'id': 'parties', 'label': 'All parties', 'paths': [r'^/hdc/parties$']},
    ]},
    {'id': 'reports_section', 'label': 'Reports', 'children': [
        {'id': 'reports', 'label': 'Reports', 'paths': [r'^/hdc/reports$']},
        {'id': 'glance_report', 'label': 'Glance report', 'paths': [r'^/hdc/reports/glance$']},
        {'id': 'report_exports', 'label': 'Report exports', 'paths': [r'^/hdc/reports/(?:export/|project/)']},
    ]},
    {'id': 'accounts_section', 'label': 'Accounts & Cash', 'children': [
        {'id': 'accounts', 'label': 'Accounts hub & transactions', 'paths': [r'^/hdc/accounts(?:$|/(?!manage|entries|shared|cashflow|money-center))', r'^/api/accounts/']},
        {'id': 'accounts_manage', 'label': 'Manage accounts', 'paths': [r'^/hdc/accounts/(?:manage|new|\d+/edit)']},
        {'id': 'accounts_entries', 'label': 'All entries & reconciliation', 'paths': [r'^/hdc/accounts/(?:entries|reconciliation|kpi|transactions)']},
        {'id': 'cashflow', 'label': 'Cash flow & day close', 'paths': [r'^/hdc/accounts/cashflow(?:/|$)']},
        {'id': 'money_center', 'label': 'Money Center', 'paths': [r'^/hdc/accounts/money-center(?:/|$)', r'^/hdc/api/(?:workers|suppliers|subcontractors|office_staff|expense_categories)(?:/|$)']},
        {'id': 'shared_expenses', 'label': 'Shared expenses', 'paths': [r'^/hdc/accounts/shared(?:/|$)']},
    ]},
    {'id': 'admin_section', 'label': 'Administration', 'children': [
        {'id': 'users', 'label': 'User management', 'paths': [r'^/hdc/users(?:/|$)']},
        {'id': 'event_recorder', 'label': 'Event recorder', 'paths': [r'^/hdc/event-recorder$']},
        {'id': 'settings', 'label': 'Settings & backups', 'paths': [r'^/hdc/settings(?:/|$)', r'^/hdc/admin/']},
        {'id': 'system_lookups', 'label': 'System lookup APIs', 'paths': [r'^/hdc/api/']},
    ]},
]

PAGE_CATALOG = [page for section in PAGE_TREE for page in section['children']]
_PAGE_MATCHERS = sorted(
    [(re.compile(path), page['id']) for page in PAGE_CATALOG for path in page['paths']],
    key=lambda item: len(item[0].pattern), reverse=True)
PAGE_BY_ID = {page['id']: page for page in PAGE_CATALOG}


def page_id_for_path(path):
    path = str(path or '')
    path = '/hdc/' if path.rstrip('/') == '/hdc' else path.rstrip('/')
    for matcher, page_id in _PAGE_MATCHERS:
        if matcher.search(path):
            return page_id
    return None


def parse_permissions(user):
    try:
        value = json.loads(getattr(user, 'permissions_json', None) or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def has_custom_permissions(user):
    # Strict data access can never fall back to a broad base role, even if a
    # damaged/legacy record has no page map stored alongside its row grants.
    return bool((getattr(user, 'permissions_json', None) or '').strip() or
                getattr(user, 'record_scope_enabled', False))


def permission_for(user, page_id, mode='read'):
    """Return True/False for configured access, or None for role defaults."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if (getattr(user, 'role', '') or '').strip().lower() == 'admin':
        return True
    if not has_custom_permissions(user):
        return None
    if page_id in {'users', 'event_recorder', 'settings'}:
        return False  # Administration is never delegable to a restricted user.
    configured = parse_permissions(user)
    # These independent subsections did not exist in older page maps. Keep
    # their old parent-page behavior only outside strict exact-data mode.
    legacy_parents = {
        'project_financials': ('projects', 'project_detail', 'project_create', 'project_edit'),
        'project_receipts': ('project_detail',),
    }
    if (not getattr(user, 'record_scope_enabled', False) and page_id not in configured and
            page_id in legacy_parents):
        return any(permission_for(user, parent, mode) is True for parent in legacy_parents[page_id])
    value = configured.get(page_id, {})
    if not isinstance(value, dict):
        return False
    if mode == 'write':
        return value.get('write') is True and value.get('read') is True
    return value.get('read') is True


def permission_editor_grants(user):
    configured = parse_permissions(user)
    if has_custom_permissions(user) and not getattr(user, 'record_scope_enabled', False):
        for page in ('project_financials', 'project_receipts'):
            if page not in configured:
                configured[page] = {'read': permission_for(user, page) is True,
                                    'write': permission_for(user, page, 'write') is True}
    return configured


def may_access_path(user, path, mode='read'):
    page_id = page_id_for_path(path)
    if page_id is None:
        if has_custom_permissions(user) and (getattr(user, 'role', '') or '').strip().lower() != 'admin' and (
                str(path or '').startswith(('/hdc/', '/api/', '/project-estimation'))):
            return False  # New routes fail closed until assigned to a page bucket.
        return None
    return permission_for(user, page_id, mode)


def _stage_ids_from_json(raw):
    try:
        values = json.loads(raw or '[]')
        if not isinstance(values, list):
            return set()
        return {int(value) for value in values
                if not isinstance(value, bool) and len(str(value)) <= 19 and str(value).isascii()
                and str(value).isdigit() and 0 < int(value) <= 9223372036854775807}
    except (TypeError, ValueError):
        return set()


def user_stage_scope(user, mode='read'):
    """Return allowed stage IDs for this operation, None if unrestricted.

    Write-scoped stages are also readable, but read-only stage grants never
    widen write access.
    """
    if not user or (getattr(user, 'role', '') or '').strip().lower() == 'admin':
        return None
    if not bool(getattr(user, 'stage_scope_enabled', False)):
        return None
    readable = _stage_ids_from_json(getattr(user, 'allowed_stage_ids_json', None))
    writable = _stage_ids_from_json(getattr(user, 'write_stage_ids_json', None))
    return writable if mode == 'write' else readable | writable


def _cached_scope(mode='read'):
    if not has_request_context() or not current_user.is_authenticated:
        return None
    key = '_hdc_permission_stage_scope_' + mode
    if not hasattr(g, key):
        scope = user_stage_scope(current_user, mode)
        if scope is None:
            setattr(g, key, None)
        else:
            # Resolve project containers once, without recursively applying the
            # same ORM scope. This keeps project lists from revealing projects
            # that contain none of the user's assigned stages.
            from hdc.models.projects import Stage
            rows = current_app.extensions['sqlalchemy'].session.query(Stage.project_id).filter(
                Stage.id.in_(scope) if scope else False
            ).execution_options(hdc_skip_access_scope=True).distinct().all()
            setattr(g, key, (scope, {int(row[0]) for row in rows if row[0] is not None}))
    return getattr(g, key)


def stage_is_allowed(stage_id, mode='read'):
    scope = user_stage_scope(current_user, mode)
    if scope is None:
        return True
    try:
        return stage_id is not None and int(stage_id) in scope
    except (TypeError, ValueError, OverflowError):
        return False


_QUERY_SCOPE_INSTALLED = False


def install_permission_query_scope():
    """Apply stage/project row filters to ORM reads and writes for scoped users."""
    global _QUERY_SCOPE_INSTALLED
    if _QUERY_SCOPE_INSTALLED:
        return
    _QUERY_SCOPE_INSTALLED = True

    @event.listens_for(Session, 'do_orm_execute')
    def _scope_stage_records(execute_state):
        if (execute_state.execution_options.get('hdc_skip_access_scope') or
                execute_state.is_column_load):
            return
        # During Flask-Login's first user lookup, consulting current_user here
        # would recursively start another lookup. Until its request-local user
        # slot is populated, permission filtering must therefore stay off.
        if not has_request_context() or '_login_user' not in g:
            return
        user = g.get('_login_user')
        if not user or not getattr(user, 'is_authenticated', False):
            return
        mode = 'read' if request.method in ('GET', 'HEAD', 'OPTIONS') else 'write'
        scoped = _cached_scope(mode)
        if scoped is None:
            return
        stage_ids, project_ids = scoped
        statement = execute_state.statement
        for mapper in current_app.extensions['sqlalchemy'].Model.registry.mappers:
            model = mapper.class_
            if model.__name__ == 'Stage':
                statement = statement.options(with_loader_criteria(
                    model, model.id.in_(stage_ids) if stage_ids else model.id < 0,
                    include_aliases=True))
            elif model.__name__ == 'Project':
                statement = statement.options(with_loader_criteria(
                    model, model.id.in_(project_ids) if project_ids else model.id < 0,
                    include_aliases=True))
            if 'stage_id' in model.__dict__:
                col = getattr(model, 'stage_id')
                statement = statement.options(with_loader_criteria(
                    model, col.in_(stage_ids) if stage_ids else col < 0,
                    include_aliases=True))
            if 'project_id' in model.__dict__ and model.__name__ != 'Stage':
                col = getattr(model, 'project_id')
                statement = statement.options(with_loader_criteria(
                    model, col.in_(project_ids) if project_ids else col < 0,
                    include_aliases=True))
        execute_state.statement = statement


def install_permission_template_helpers(application):
    application.jinja_env.globals['hdc_page_allowed'] = lambda page_id, mode='read': permission_for(current_user, page_id, mode) is not False
    application.jinja_env.globals['hdc_page_catalog'] = PAGE_TREE
    application.jinja_env.globals['hdc_page_id_for_path'] = page_id_for_path


def permission_landing_url(user):
    """Land on a granted screen instead of a forbidden dashboard after login."""
    if not has_custom_permissions(user) or (getattr(user, 'role', '') or '').strip().lower() == 'admin':
        return '/hdc/'
    for path in (
        '/hdc/', '/hdc/projects', '/hdc/stages', '/hdc/workers', '/hdc/timekeeping',
        '/hdc/timekeeping/status', '/hdc/subcontractors', '/hdc/expenses', '/hdc/payroll',
        '/hdc/trades', '/hdc/estimation', '/hdc/project-estimation', '/hdc/alerts',
        '/hdc/office-management', '/hdc/office-management/staff',
        '/hdc/office-management/staff/attendance', '/hdc/office-management/staff/ledger',
        '/hdc/office-management/expenses', '/hdc/personal-management',
        '/hdc/purchase-v2', '/hdc/purchase-v2/materials', '/hdc/purchase-v2/suppliers',
        '/hdc/purchase-v2/purchases', '/hdc/purchase-v2/delivered', '/hdc/purchase-v2/usage',
        '/hdc/tool-rental', '/hdc/tool-rental/inventory', '/hdc/tool-rental/tracking',
        '/hdc/parties', '/hdc/reports', '/hdc/reports/glance', '/hdc/accounts',
        '/hdc/accounts/manage', '/hdc/accounts/entries', '/hdc/accounts/cashflow',
        '/hdc/accounts/money-center', '/hdc/accounts/shared',
    ):
        if may_access_path(user, path) is True:
            return path
    # A user may be given only a detail/ledger page, without its list page.
    from hdc.models.projects import Project, Stage
    if permission_for(user, 'project_detail') is True:
        row = Project.query.order_by(Project.id).first()
        if row:
            return f'/hdc/projects/{row.id}'
    if permission_for(user, 'stage_ledger') is True:
        row = Stage.query.order_by(Stage.id).first()
        if row:
            return f'/hdc/stage/{row.id}/ledger'
    return '/hdc/access'
