"""HDC services.aggregation — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from sqlalchemy import case, func

from hdc.extensions import db
from hdc.models.accounts import Expense, OwnerPayment
from hdc.models.materials import Purchase, UsageLogV2
from hdc.models.projects import Project, Stage
from hdc.models.subcontract import SubcontractPayment, Subcontractor
from hdc.models.workforce import Attendance, TimeEntry

def _aggregate_stage_costs(stage_ids):
    stage_ids = [int(sid) for sid in (stage_ids or []) if sid]
    if not stage_ids:
        return {}

    attendance_map = dict(
        db.session.query(
            Attendance.stage_id,
            func.coalesce(func.sum(Attendance.total_wage), 0.0)
        )
        .outerjoin(TimeEntry, TimeEntry.attendance_id == Attendance.id)
        .filter(Attendance.stage_id.in_(stage_ids), TimeEntry.id.is_(None))
        .group_by(Attendance.stage_id)
        .all()
    )
    time_map = dict(
        db.session.query(
            TimeEntry.stage_id,
            func.coalesce(func.sum(TimeEntry.wage_calculated), 0.0)
        )
        .filter(TimeEntry.stage_id.in_(stage_ids), TimeEntry.is_void == False)
        .group_by(TimeEntry.stage_id)
        .all()
    )
    material_map = dict(
        db.session.query(
            Purchase.stage_id,
            func.coalesce(func.sum(Purchase.total), 0.0)
        )
        .filter(Purchase.stage_id.in_(stage_ids))
        .group_by(Purchase.stage_id)
        .all()
    )
    material_v2_map = dict(
        db.session.query(
            UsageLogV2.stage_id,
            func.coalesce(func.sum(UsageLogV2.cost), 0.0)
        )
        .filter(UsageLogV2.stage_id.in_(stage_ids), UsageLogV2.is_void == False)
        .group_by(UsageLogV2.stage_id)
        .all()
    )
    expense_map = dict(
        db.session.query(
            Expense.stage_id,
            func.coalesce(func.sum(Expense.amount), 0.0)
        )
        .filter(Expense.stage_id.in_(stage_ids), Expense.is_void == False)
        .group_by(Expense.stage_id)
        .all()
    )
    stage_key = func.coalesce(SubcontractPayment.stage_id, Subcontractor.stage_id)
    subcontract_map = dict(
        db.session.query(
            stage_key.label('stage_id'),
            func.coalesce(func.sum(SubcontractPayment.amount), 0.0)
        )
        .select_from(Subcontractor)
        .join(SubcontractPayment, SubcontractPayment.subcontractor_id == Subcontractor.id)
        .filter(stage_key.in_(stage_ids), SubcontractPayment.is_void == False)
        .group_by(stage_key)
        .all()
    )

    out = {}
    for sid in stage_ids:
        labour = float(attendance_map.get(sid, 0.0) or 0.0) + float(time_map.get(sid, 0.0) or 0.0)
        material = float(material_map.get(sid, 0.0) or 0.0) + float(material_v2_map.get(sid, 0.0) or 0.0)
        expense = float(expense_map.get(sid, 0.0) or 0.0)
        subcontract = float(subcontract_map.get(sid, 0.0) or 0.0)
        total = labour + material + expense + subcontract
        out[sid] = {
            'labour': labour,
            'material': material,
            'expense': expense,
            'subcontract': subcontract,
            'total': total
        }
    return out


def _apply_aggregated_stage_costs(stages):
    stage_rows = [s for s in (stages or []) if s and s.id]
    if not stage_rows:
        return {}
    metrics = _aggregate_stage_costs([s.id for s in stage_rows])
    for s in stage_rows:
        m = metrics.get(s.id, {})
        labour = float(m.get('labour', 0.0) or 0.0)
        material = float(m.get('material', 0.0) or 0.0)
        expense = float(m.get('expense', 0.0) or 0.0)
        subcontract = float(m.get('subcontract', 0.0) or 0.0)
        total = float(m.get('total', 0.0) or 0.0)
        s._agg_stage_labour_cost = labour
        s._agg_stage_material_cost = material
        s._agg_stage_expense_cost = expense
        s._agg_stage_subcontract_cost = subcontract
        s._agg_stage_total_cost = total
        s._agg_stage_profit = float((s.contract_value or 0.0) - total)
    return metrics


def _aggregate_project_costs(project_ids):
    project_ids = [int(pid) for pid in (project_ids or []) if pid]
    if not project_ids:
        return {}

    received_map = dict(
        db.session.query(
            OwnerPayment.project_id,
            func.coalesce(func.sum(OwnerPayment.amount), 0.0)
        )
        .filter(OwnerPayment.project_id.in_(project_ids), OwnerPayment.is_void == False)
        .group_by(OwnerPayment.project_id)
        .all()
    )
    attendance_map = dict(
        db.session.query(
            Attendance.project_id,
            func.coalesce(func.sum(Attendance.total_wage), 0.0)
        )
        .outerjoin(TimeEntry, TimeEntry.attendance_id == Attendance.id)
        .filter(Attendance.project_id.in_(project_ids), TimeEntry.id.is_(None))
        .group_by(Attendance.project_id)
        .all()
    )
    time_map = dict(
        db.session.query(
            TimeEntry.project_id,
            func.coalesce(func.sum(TimeEntry.wage_calculated), 0.0)
        )
        .filter(TimeEntry.project_id.in_(project_ids), TimeEntry.is_void == False)
        .group_by(TimeEntry.project_id)
        .all()
    )
    material_map = dict(
        db.session.query(
            Purchase.project_id,
            func.coalesce(func.sum(Purchase.total), 0.0)
        )
        .filter(Purchase.project_id.in_(project_ids))
        .group_by(Purchase.project_id)
        .all()
    )
    material_v2_map = dict(
        db.session.query(
            UsageLogV2.project_id,
            func.coalesce(func.sum(UsageLogV2.cost), 0.0)
        )
        .filter(UsageLogV2.project_id.in_(project_ids), UsageLogV2.is_void == False)
        .group_by(UsageLogV2.project_id)
        .all()
    )
    expense_map = dict(
        db.session.query(
            Expense.project_id,
            func.coalesce(func.sum(Expense.amount), 0.0)
        )
        .filter(Expense.project_id.in_(project_ids), Expense.is_void == False)
        .group_by(Expense.project_id)
        .all()
    )
    subcontract_map = dict(
        db.session.query(
            Subcontractor.project_id,
            func.coalesce(func.sum(SubcontractPayment.amount), 0.0)
        )
        .join(SubcontractPayment, SubcontractPayment.subcontractor_id == Subcontractor.id)
        .filter(Subcontractor.project_id.in_(project_ids), SubcontractPayment.is_void == False)
        .group_by(Subcontractor.project_id)
        .all()
    )
    stage_contract_map = dict(
        db.session.query(
            Stage.project_id,
            func.coalesce(func.sum(
                case(
                    (Stage.contract_basis == 'Lump Sum', func.coalesce(Stage.lump_sum_value, 0.0)),
                    (Stage.contract_basis == 'Per Sq Ft',
                     (func.coalesce(Stage.rate_per_sqft, 0.0) - func.coalesce(Stage.discount_per_sqft, 0.0))
                     * func.coalesce(Stage.qty_sqft, 0.0)),
                    else_=0.0
                )
            ), 0.0)
        )
        .filter(Stage.project_id.in_(project_ids))
        .group_by(Stage.project_id)
        .all()
    )

    out = {}
    for pid in project_ids:
        labour = float(attendance_map.get(pid, 0.0) or 0.0) + float(time_map.get(pid, 0.0) or 0.0)
        material = float(material_map.get(pid, 0.0) or 0.0) + float(material_v2_map.get(pid, 0.0) or 0.0)
        expense = float(expense_map.get(pid, 0.0) or 0.0)
        subcontract = float(subcontract_map.get(pid, 0.0) or 0.0)
        received = float(received_map.get(pid, 0.0) or 0.0)
        out[pid] = {
            'labour': labour,
            'material': material,
            'expense': expense,
            'subcontract': subcontract,
            'received': received,
            'stage_contract': float(stage_contract_map.get(pid, 0.0) or 0.0)
        }
    return out


def _apply_aggregated_project_costs(projects):
    project_rows = [p for p in (projects or []) if p and p.id]
    if not project_rows:
        return {}
    metrics = _aggregate_project_costs([p.id for p in project_rows])
    for p in project_rows:
        m = metrics.get(p.id, {})
        labour = float(m.get('labour', 0.0) or 0.0)
        material = float(m.get('material', 0.0) or 0.0)
        expense = float(m.get('expense', 0.0) or 0.0)
        subcontract = float(m.get('subcontract', 0.0) or 0.0)
        received = float(m.get('received', 0.0) or 0.0)
        total = labour + material + expense + subcontract
        p._agg_total_received = received
        p._agg_total_labour_cost = labour
        p._agg_total_material_cost = material
        p._agg_total_expense_cost = expense
        p._agg_total_subcontract_cost = subcontract
        p._agg_total_cost = total
        p._agg_net_profit = float((p.owner_contract_value or 0.0) - total)
        p._agg_remaining_receivable = float((p.owner_contract_value or 0.0) - received)
        p._agg_stage_contract_value = float(m.get('stage_contract', 0.0) or 0.0)
    return metrics


def _running_projects_receivable_rows():
    projects = (Project.query
                .order_by(Project.name.asc(), Project.id.asc())
                .all())
    _apply_aggregated_project_costs(projects)
    rows = []
    for p in projects:
        status = str(getattr(p, 'status', '') or '').strip().lower()
        if status in ('completed', 'closed', 'cancelled', 'canceled', 'inactive'):
            continue
        rows.append({
            'id': int(p.id),
            'name': p.name,
            'status': (p.status or 'active'),
            'total_received': float(p.total_received or 0.0),
            'remaining_receivable': float(p.remaining_receivable or 0.0),
        })
    return rows
