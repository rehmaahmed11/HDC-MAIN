"""HDC services.subcontract — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from flask_login import current_user
from sqlalchemy import func, text

from hdc.core.schema import _ensure_table_columns_sqlite
from hdc.extensions import db
from hdc.models.projects import Stage
from hdc.models.subcontract import (
    SubcontractEvent, SubcontractLabourAttendance, SubcontractPayment,
    SubcontractTeamAttendance, Subcontractor,
)
from hdc.utils.dates import _pkt_now_naive


def subcontract_scope_ids(sub):
    """Return ``(project_ids, stage_ids)`` a subcontractor may charge labour to.

    Same scope rules as the legacy daily labour-attendance route: the project
    the sub belongs to, the stage it is assigned to, every stage that points
    back at it, and the projects of those stages.
    """
    scope_stage_ids = set()
    scope_project_ids = set()
    if sub and sub.project_id:
        scope_project_ids.add(int(sub.project_id))
        stage_ids = (db.session.query(Stage.id)
                     .filter(Stage.project_id == sub.project_id)
                     .all())
        scope_stage_ids.update(int(v) for (v,) in stage_ids if v)
    if sub and sub.stage_id:
        scope_stage_ids.add(int(sub.stage_id))
        sub_stage = Stage.query.get(sub.stage_id)
        if sub_stage and sub_stage.project_id:
            scope_project_ids.add(int(sub_stage.project_id))
    if sub:
        for srow in (Stage.query
                     .filter(Stage.assigned_subcontractor_id == sub.id)
                     .all()):
            scope_stage_ids.add(int(srow.id))
            if srow.project_id:
                scope_project_ids.add(int(srow.project_id))
        if scope_stage_ids:
            for srow in Stage.query.filter(Stage.id.in_(list(scope_stage_ids))).all():
                if srow.project_id:
                    scope_project_ids.add(int(srow.project_id))
    return scope_project_ids, scope_stage_ids


def sub_labour_rollup(sub_id, date_from=None, date_to=None, stage_id=None):
    """Combine the simple crew summary rows and the legacy daily rows.

    This is the single source for subcontractor labour everywhere (attendance
    page, ledger KPIs, project/reports P&L).  Both sources are summed exactly
    once so no reader can disagree with the attendance page.
    """
    tq = SubcontractTeamAttendance.query.filter(
        SubcontractTeamAttendance.subcontractor_id == sub_id)
    lq = SubcontractLabourAttendance.query.filter(
        SubcontractLabourAttendance.subcontractor_id == sub_id)
    if date_from:
        tq = tq.filter(SubcontractTeamAttendance.date >= date_from)
        lq = lq.filter(SubcontractLabourAttendance.date >= date_from)
    if date_to:
        tq = tq.filter(SubcontractTeamAttendance.date <= date_to)
        lq = lq.filter(SubcontractLabourAttendance.date <= date_to)
    if stage_id:
        tq = tq.filter(SubcontractTeamAttendance.stage_id == stage_id)
        lq = lq.filter(SubcontractLabourAttendance.stage_id == stage_id)
    team_rows = tq.all()
    legacy_rows = lq.all()

    summary_days = sum(int(r.days_count or 0) for r in team_rows)
    summary_man_days = sum(int(r.days_count or 0) * int(r.workers_count or 0) for r in team_rows)
    summary_cost = sum(float(r.total_amount or 0.0) for r in team_rows)
    legacy_days = sum(1 for r in legacy_rows if (r.labour_count or 0) > 0)
    legacy_man_days = sum(int(r.labour_count or 0) for r in legacy_rows)
    legacy_cost = sum(float(r.total_labour_paid or 0.0) for r in legacy_rows)
    man_days = summary_man_days + legacy_man_days
    cost = float(summary_cost) + float(legacy_cost)
    return {
        'crew_rows': len(team_rows),
        'legacy_rows': len(legacy_rows),
        'summary_days': int(summary_days),
        'legacy_days': int(legacy_days),
        'days': int(summary_days) + int(legacy_days),
        'summary_man_days': int(summary_man_days),
        'legacy_man_days': int(legacy_man_days),
        'man_days': int(man_days),
        'summary_cost': float(summary_cost),
        'legacy_cost': float(legacy_cost),
        'cost': float(cost),
        'avg_rate': (cost / man_days) if man_days > 0 else 0.0,
        'workers_peak': max([int(r.workers_count or 0) for r in team_rows] or [0]),
        'team_rows': team_rows,
    }

def _next_subcontractor_code():
    max_n = 0
    rows = db.session.query(Subcontractor.subcontractor_code).all()
    for (code,) in rows:
        if not code:
            continue
        code = str(code).strip().upper()
        if not code.startswith('SUB-'):
            continue
        try:
            n = int(code.split('-', 1)[1])
            if n > max_n:
                max_n = n
        except Exception:
            continue
    return f'SUB-{max_n + 1:04d}'


def _log_subcontract_event(sub, event_type, from_value='', to_value='', amount=0.0, notes='', project_id=None, stage_id=None):
    if not sub:
        return
    actor_id = None
    try:
        if current_user and getattr(current_user, 'is_authenticated', False):
            actor_id = current_user.id
    except Exception:
        actor_id = None
    db.session.add(SubcontractEvent(
        subcontractor_id=sub.id,
        project_id=project_id if project_id is not None else sub.project_id,
        stage_id=stage_id if stage_id is not None else sub.stage_id,
        actor_user_id=actor_id,
        event_type=(event_type or '').strip()[:40] or 'update',
        from_value=(from_value or '')[:250],
        to_value=(to_value or '')[:250],
        amount=float(amount or 0.0),
        notes=(notes or '')[:300],
        created_at=_pkt_now_naive()
    ))


def _ensure_subcontract_baseline_events(sub):
    if not sub:
        return 0
    created = 0
    has_create = (SubcontractEvent.query
                  .filter(SubcontractEvent.subcontractor_id == sub.id, SubcontractEvent.event_type == 'create')
                  .first())
    if not has_create:
        _log_subcontract_event(
            sub=sub,
            event_type='create',
            to_value=(sub.subcontractor_code or ''),
            notes='Backfill: subcontractor profile',
            project_id=sub.project_id,
            stage_id=sub.stage_id
        )
        created += 1
    if sub.stage_id and sub.project_id:
        has_shift = (SubcontractEvent.query
                     .filter(SubcontractEvent.subcontractor_id == sub.id,
                             SubcontractEvent.event_type.in_(['shift', 'reassign']),
                             SubcontractEvent.stage_id == sub.stage_id)
                     .first())
        if not has_shift:
            _log_subcontract_event(
                sub=sub,
                event_type='shift',
                from_value='backfill',
                to_value=(sub.stage_rel.name if sub.stage_rel else f'Stage#{sub.stage_id}'),
                notes='Backfill: stage assignment',
                project_id=sub.project_id,
                stage_id=sub.stage_id
            )
            created += 1
        has_price = (SubcontractEvent.query
                     .filter(SubcontractEvent.subcontractor_id == sub.id,
                             SubcontractEvent.event_type == 'price_update',
                             SubcontractEvent.stage_id == sub.stage_id)
                     .first())
        if not has_price:
            _log_subcontract_event(
                sub=sub,
                event_type='price_update',
                to_value=(sub.contract_type or ''),
                amount=float(sub.contract_value or 0.0),
                notes=f'Backfill: Rate {float(sub.rate_per_sqft or 0):.2f} | Sqft {float(sub.total_sqft or 0):.2f} | Lump {float(sub.lump_sum_amount or 0):.2f}',
                project_id=sub.project_id,
                stage_id=sub.stage_id
            )
            created += 1
    return created


def _extract_pct(text_value):
    try:
        return float(str(text_value or '').replace('%', '').strip() or 0.0)
    except Exception:
        return 0.0


def _subcontract_scope_stages(sub):
    stage_ids = set()
    if sub.stage_id:
        stage_ids.add(int(sub.stage_id))
    ev_rows = (SubcontractEvent.query
               .filter(SubcontractEvent.subcontractor_id == sub.id, SubcontractEvent.stage_id.isnot(None))
               .all())
    for r in ev_rows:
        if r.stage_id:
            stage_ids.add(int(r.stage_id))
    if not stage_ids:
        return []
    return Stage.query.filter(Stage.id.in_(list(stage_ids))).order_by(Stage.project_id.asc(), Stage.id.asc()).all()


def _subcontract_stage_snapshot(sub, stg):
    if (not sub) or (not stg):
        return {
            'contract': 0.0, 'progress': 0.0, 'payable': 0.0, 'paid': 0.0, 'balance': 0.0,
            'contract_balance': 0.0, 'live_balance': 0.0, 'advance_paid': 0.0
        }

    contract_val = 0.0
    if sub.stage_id == stg.id:
        contract_val = float(sub.contract_value or 0.0)
    if contract_val <= 0:
        pe = (SubcontractEvent.query
              .filter(SubcontractEvent.subcontractor_id == sub.id,
                      SubcontractEvent.stage_id == stg.id,
                      func.lower(SubcontractEvent.event_type) == 'price_update')
              .order_by(SubcontractEvent.created_at.desc(), SubcontractEvent.id.desc())
              .first())
        if pe and float(pe.amount or 0.0) > 0:
            contract_val = float(pe.amount or 0.0)
    if contract_val <= 0:
        contract_val = float(stg.contract_value or 0.0)

    progress = 0.0
    if sub.stage_id == stg.id:
        progress = float(sub.effective_progress_percentage or 0.0)
    if progress <= 0:
        pge = (SubcontractEvent.query
               .filter(SubcontractEvent.subcontractor_id == sub.id,
                       SubcontractEvent.stage_id == stg.id,
                       func.lower(SubcontractEvent.event_type) == 'progress')
               .order_by(SubcontractEvent.created_at.desc(), SubcontractEvent.id.desc())
               .first())
        if pge:
            progress = max(0.0, min(100.0, _extract_pct(pge.to_value)))
    if progress <= 0:
        progress = max(0.0, min(100.0, float(stg.progress or 0.0)))
    if progress <= 0 and (stg.status or '').strip().lower() in ('completed', 'complete'):
        progress = 100.0

    retention_pct = max(0.0, min(100.0, float(sub.retention_percentage or 0.0)))
    gross_payable = contract_val * progress / 100.0
    cap_payable = max(0.0, contract_val * (100.0 - retention_pct) / 100.0)
    payable = max(0.0, min(gross_payable, cap_payable))
    paid = float(db.session.query(func.coalesce(func.sum(SubcontractPayment.amount), 0.0))
                 .filter(
                     SubcontractPayment.is_void == False,
                     SubcontractPayment.subcontractor_id == sub.id,
                     SubcontractPayment.stage_id == stg.id,
                     func.lower(func.coalesce(SubcontractPayment.entry_type, 'payment')) != 'settlement'
                 )
                 .scalar() or 0.0)
    settled = float(db.session.query(func.coalesce(func.sum(SubcontractPayment.amount), 0.0))
                    .filter(
                        SubcontractPayment.is_void == False,
                        SubcontractPayment.subcontractor_id == sub.id,
                        SubcontractPayment.stage_id == stg.id,
                        func.lower(func.coalesce(SubcontractPayment.entry_type, 'payment')) == 'settlement'
                    )
                    .scalar() or 0.0)
    cleared = paid + settled
    live_balance = payable - cleared
    balance = max(0.0, payable - cleared)
    contract_balance = max(0.0, contract_val - cleared)
    return {
        'contract': contract_val,
        'progress': progress,
        'payable': payable,
        'paid': paid,
        'settled': settled,
        'cleared': cleared,
        'balance': balance,
        'contract_balance': contract_balance,
        'live_balance': live_balance,
        'advance_paid': max(0.0, -live_balance)
    }


def _reconcile_subcontract_links():
    """Keep stage execution pointers and subcontract pointers in sync."""
    changed = 0
    stages = Stage.query.all()
    for stg in stages:
        mode = (stg.execution_mode or 'company').strip().lower()
        if mode not in ('company', 'subcontractor'):
            stg.execution_mode = 'company'
            changed += 1
            mode = 'company'
        if mode == 'subcontractor':
            if not stg.assigned_subcontractor_id:
                stg.execution_mode = 'company'
                changed += 1
                continue
            sub = Subcontractor.query.get(stg.assigned_subcontractor_id)
            if not sub:
                stg.execution_mode = 'company'
                stg.assigned_subcontractor_id = None
                changed += 1
                continue
            if sub.stage_id != stg.id:
                sub.stage_id = stg.id
                changed += 1
            if sub.project_id != stg.project_id:
                sub.project_id = stg.project_id
                changed += 1
        else:
            if stg.assigned_subcontractor_id:
                stg.assigned_subcontractor_id = None
                changed += 1
    for sub in Subcontractor.query.all():
        if not sub.stage_id:
            continue
        stg = Stage.query.get(sub.stage_id)
        if (not stg) or (stg.assigned_subcontractor_id != sub.id) or ((stg.execution_mode or 'company').strip().lower() != 'subcontractor'):
            sub.stage_id = None
            changed += 1
    if changed:
        db.session.commit()
    return changed


def _ensure_subcontract_labour_attendance_schema():
    _ensure_table_columns_sqlite('hdc_subcontract_labour_attendance', {
        'worker_id': "worker_id INTEGER REFERENCES hdc_subcontract_labour_worker(id)",
        'attendance_status': "attendance_status VARCHAR(20) DEFAULT 'Present'",
        'working_hours': "working_hours FLOAT DEFAULT 0",
        'overtime_hours': "overtime_hours FLOAT DEFAULT 0",
    })


def _ensure_subcontract_payment_void_schema():
    _ensure_table_columns_sqlite('hdc_subcontract_payment', {
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'void_reason': "void_reason VARCHAR(250)",
        'voided_at': "voided_at DATETIME",
    })
    try:
        with db.engine.connect() as conn:
            conn.execute(text("UPDATE hdc_subcontract_payment SET is_void = COALESCE(is_void, 0)"))
            conn.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
