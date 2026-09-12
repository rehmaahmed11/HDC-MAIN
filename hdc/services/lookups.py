"""HDC services.lookups — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from sqlalchemy import func

from hdc.extensions import db
from hdc.models.accounts import ExpenseCategory
from hdc.models.office import OfficeStaff
from hdc.models.projects import Project
from hdc.models.workforce import Worker, WorkerTrade
from hdc.utils.normalize import _normalize_expense_category_name

def _trade_options():
    """Return active trade list from trade master."""
    master = [t.name.strip() for t in WorkerTrade.query.filter_by(active_status=True).order_by(WorkerTrade.name).all() if (t.name or '').strip()]

    seen = set()
    out = []
    for item in master:
        key = item.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return sorted(out, key=lambda x: x.lower())


def _expense_category_options():
    return [
        c.name.strip()
        for c in ExpenseCategory.query.filter_by(active_status=True).order_by(ExpenseCategory.name).all()
        if (c.name or '').strip()
    ]


def _ensure_expense_category(name):
    cname = _normalize_expense_category_name(name)
    if not cname:
        return None
    row = db.session.query(ExpenseCategory).filter(func.lower(ExpenseCategory.name) == cname.lower()).first()
    if row:
        if not row.active_status:
            row.active_status = True
            db.session.flush()
        return row
    row = ExpenseCategory(name=cname, active_status=True)
    db.session.add(row)
    db.session.flush()
    return row


def _ensure_expense_category_by_id(category_id):
    if not category_id:
        return None
    row = ExpenseCategory.query.get(int(category_id))
    if row and row.active_status:
        return row
    return None


def _category_name_from_id(category_id):
    row = ExpenseCategory.query.get(int(category_id)) if category_id else None
    return (row.name if row else '')


def _generate_project_code():
    return _next_project_code()


def _next_project_code():
    prefix = "HDC-"
    max_n = 0
    for p in Project.query.filter(Project.project_code.like(prefix + '%')).all():
        code = (p.project_code or '').strip().upper()
        if not code.startswith(prefix):
            continue
        suffix = code[len(prefix):]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    candidate = max_n + 1
    while True:
        code = f"HDC-{candidate:05d}"
        if not Project.query.filter_by(project_code=code).first():
            return code
        candidate += 1


def _next_worker_code():
    prefix = "HDC-WORKER-"
    max_n = 0
    for w in Worker.query.filter(Worker.worker_code.like(prefix + '%')).all():
        last = (w.worker_code or '').replace(prefix, '')
        if last.isdigit():
            max_n = max(max_n, int(last))
    return f"{prefix}{(max_n + 1):06d}"


def _next_office_staff_code():
    prefix = "HDC-OFFICE-"
    max_n = 0
    for s in OfficeStaff.query.filter(OfficeStaff.staff_code.like(prefix + '%')).all():
        last = (s.staff_code or '').replace(prefix, '')
        if last.isdigit():
            max_n = max(max_n, int(last))
    return f"{prefix}{(max_n + 1):06d}"
