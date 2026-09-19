"""Route registration: every domain module exposes register(app)."""

from hdc.routes.accounts import register as _register_accounts
from hdc.routes.accounts_manage import register as _register_accounts_manage
from hdc.routes.api_accounts import register as _register_api_accounts
from hdc.routes.api_actors import register as _register_api_actors
from hdc.routes.api_purchase import register as _register_api_purchase
from hdc.routes.auth import register as _register_auth
from hdc.routes.cashflow import register as _register_cashflow
from hdc.routes.cashflow_register import register as _register_cashflow_register
from hdc.routes.dashboard import register as _register_dashboard
from hdc.routes.estimation import register as _register_estimation
from hdc.routes.expenses import register as _register_expenses
from hdc.routes.materials import register as _register_materials
from hdc.routes.office import register as _register_office
from hdc.routes.payroll import register as _register_payroll
from hdc.routes.projects import register as _register_projects
from hdc.routes.purchase_v2 import register as _register_purchase_v2
from hdc.routes.reports import register as _register_reports
from hdc.routes.settings import register as _register_settings
from hdc.routes.subcontractors import register as _register_subcontractors
from hdc.routes.timekeeping import register as _register_timekeeping
from hdc.routes.users import register as _register_users
from hdc.routes.workers import register as _register_workers
from hdc.routes.tool_rental import register as _register_tool_rental


def register_all(app):
    """Attach all domain routes to the Flask app."""
    _register_accounts(app)
    _register_accounts_manage(app)
    _register_api_accounts(app)
    _register_api_actors(app)
    _register_api_purchase(app)
    _register_auth(app)
    _register_cashflow(app)
    _register_cashflow_register(app)
    _register_dashboard(app)
    _register_estimation(app)
    _register_expenses(app)
    _register_materials(app)
    _register_office(app)
    _register_payroll(app)
    _register_projects(app)
    _register_purchase_v2(app)
    _register_reports(app)
    _register_settings(app)
    _register_subcontractors(app)
    _register_timekeeping(app)
    _register_users(app)
    _register_workers(app)
    _register_tool_rental(app)
