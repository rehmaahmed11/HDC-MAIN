"""HDC services.reporting — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from datetime import datetime

from hdc.extensions import db
from hdc.models.accounts import Alert, Expense, OwnerPayment
from hdc.models.materials import Material, Purchase, UsageLogV2
from hdc.models.projects import Project, Stage
from hdc.models.subcontract import SubcontractAttendance, SubcontractPayment, Subcontractor
from hdc.models.workforce import LabourLedger, TimeEntry, Worker
from hdc.services.aggregation import _apply_aggregated_project_costs, _apply_aggregated_stage_costs
from hdc.utils.dates import _pkt_now_naive, _pkt_today

def _build_stage_event_ledger(stage):
    rows = []

    def _push(*, dt, event_type, source, notes, amount, impact, status, category, ref,
              entity=None, row_id=None):
        row = {
            'dt': dt or _pkt_now_naive(),
            'event_type': event_type,
            'source': source or '-',
            'notes': notes or '-',
            'amount': float(amount or 0.0),
            'impact': float(impact or 0.0),
            'status': status or 'Active',
            'category': category or 'other',
            'ref': ref or '-'
        }
        # lets the ledger table show who entered this row (hdc_row_attrs)
        if entity and row_id is not None:
            row['_hdc_entity'] = entity
            row['_hdc_id'] = int(row_id)
        rows.append(row)

    time_rows = (TimeEntry.query
                 .filter(TimeEntry.stage_id == stage.id)
                 .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
                 .all())
    for t in time_rows:
        is_active = not bool(t.is_void)
        worker_name = t.worker.name if t.worker else f'Worker #{t.worker_id}'
        status = 'Active' if is_active else 'Voided'
        _push(
            dt=t.activity_at or t.check_in or t.created_at,
            event_type='Attendance Wage',
            source=worker_name,
            notes=f'Hours {t.hours or 0:.2f}, OT {t.overtime or 0:.2f}',
            amount=t.wage_calculated or 0.0,
            impact=(t.wage_calculated or 0.0) if is_active else 0.0,
            status=status,
            category='labour',
            ref=f'TIME#{t.id}',
            entity='hdc_time_entry', row_id=t.id
        )

    ledger_rows = (LabourLedger.query
                   .filter(
                       LabourLedger.stage_id == stage.id,
                       LabourLedger.entry_type.in_(['advance', 'payment', 'tip', 'settlement'])
                   )
                   .order_by(LabourLedger.activity_at.asc(), LabourLedger.id.asc())
                   .all())
    for l in ledger_rows:
        worker_name = l.worker.name if l.worker else f'Worker #{l.worker_id}'
        status = 'Active' if not l.is_void else 'Voided'
        type_map = {
            'advance': 'Worker Advance',
            'payment': 'Worker Payment',
            'tip': 'Worker Tip',
            'settlement': 'Worker Settlement'
        }
        _push(
            dt=l.activity_at or l.created_at,
            event_type=type_map.get(l.entry_type, (l.entry_type or 'Ledger').title()),
            source=worker_name,
            notes=l.notes or '-',
            amount=l.amount or 0.0,
            impact=0.0,
            status=status,
            category='finance',
            ref=f'LEDGER#{l.id}',
            entity='hdc_labour_ledger', row_id=l.id
        )

    expense_rows = (Expense.query
                    .filter(Expense.stage_id == stage.id)
                    .order_by(Expense.activity_at.asc(), Expense.id.asc())
                    .all())
    for e in expense_rows:
        cat = (e.category or 'Expense').strip()
        _push(
            dt=e.activity_at or e.created_at,
            event_type=f'Expense: {cat}',
            source=stage.project.name if stage.project else '-',
            notes=e.remarks or '-',
            amount=e.amount or 0.0,
            impact=e.amount or 0.0,
            status='Active',
            category='expense',
            ref=f'EXP#{e.id}',
            entity='hdc_expense', row_id=e.id
        )

    purchase_rows = (Purchase.query
                     .filter(Purchase.stage_id == stage.id)
                     .order_by(Purchase.activity_at.asc(), Purchase.id.asc())
                     .all())
    for p in purchase_rows:
        mat_name = p.material.name if p.material else f'Material #{p.material_id}'
        ptype = (p.entry_type or 'purchase').strip().lower()
        is_return = (ptype == 'return') or float(p.total or 0.0) < 0
        _push(
            dt=p.activity_at or p.created_at,
            event_type=('Material Return' if is_return else 'Material Purchase'),
            source=mat_name,
            notes=f'Qty {p.qty or 0:.2f} @ {p.rate or 0:.2f}',
            amount=p.total or 0.0,
            impact=p.total or 0.0,
            status='Active',
            category='material',
            ref=f'PUR#{p.id}',
            entity='hdc_purchase', row_id=p.id
        )
    usage_v2_rows = (UsageLogV2.query
                     .filter(UsageLogV2.stage_id == stage.id, UsageLogV2.is_void == False)
                     .order_by(UsageLogV2.created_at.asc(), UsageLogV2.id.asc())
                     .all())
    for u in usage_v2_rows:
        mat_name = u.material.name if u.material else f'Material #{u.material_id}'
        unit_cost = (float(u.cost or 0.0) / float(u.quantity or 1.0)) if float(u.quantity or 0.0) > 0 else 0.0
        _push(
            dt=u.created_at or datetime.combine(u.date or _pkt_today(), datetime.min.time()),
            event_type='Material Usage (V2)',
            source=mat_name,
            notes=f'Qty {u.quantity or 0:.2f} @ {unit_cost:.2f}',
            amount=u.cost or 0.0,
            impact=u.cost or 0.0,
            status='Active',
            category='material',
            ref=f'USEV2#{u.id}',
            entity='hdc_usage_log_v2', row_id=u.id
        )

    subs = (Subcontractor.query
            .filter(Subcontractor.stage_id == stage.id)
            .order_by(Subcontractor.created_at.asc(), Subcontractor.id.asc())
            .all())
    for s in subs:
        _push(
            dt=s.created_at,
            event_type='Subcontract Assigned',
            source=s.name or f'Subcontractor #{s.id}',
            notes=f'{s.work_type or "Work"} | Contract registered (cost impact on payment/settlement)',
            amount=s.contract_value or 0.0,
            impact=0.0,
            status='Active',
            category='finance',
            ref=f'SUB#{s.id}',
            entity='hdc_subcontractor', row_id=s.id
        )
        att_rows = (SubcontractAttendance.query
                    .filter(SubcontractAttendance.subcontractor_id == s.id)
                    .order_by(SubcontractAttendance.activity_at.asc(), SubcontractAttendance.id.asc())
                    .all())
        for sa in att_rows:
            notes = f'Present {sa.present_count or 0}'
            if (sa.work_done_pct or 0) > 0:
                notes += f' | Progress +{(sa.work_done_pct or 0):.2f}%'
            if sa.notes:
                notes += f' | {sa.notes}'
            _push(
                dt=sa.activity_at or sa.created_at,
                event_type='Subcontract Attendance',
                source=s.name or f'Subcontractor #{s.id}',
                notes=notes,
                amount=0.0,
                impact=0.0,
                status='Active',
                category='other',
                ref=f'SATT#{sa.id}',
                entity='hdc_subcontract_attendance', row_id=sa.id
            )
    rows.sort(key=lambda r: (r['dt'], r['ref']))
    running = 0.0
    for r in rows:
        running += float(r['impact'] or 0.0)
        r['running_total'] = running

    totals = {
        'labour': 0.0,
        'material': 0.0,
        'expense': 0.0,
        'subcontract': 0.0,
        'finance': 0.0,
        'other': 0.0
    }
    for r in rows:
        key = r.get('category') or 'other'
        if key not in totals:
            key = 'other'
        totals[key] += float(r.get('impact') or 0.0)
    grand_total = running
    return rows, totals, grand_total


def _refresh_alerts():
    today = _pkt_today()
    # Stage over-budget alerts
    for s in Stage.query.all():
        if (s.estimated_cost or 0) > 0 and s.actual_cost > (s.estimated_cost or 0):
            key = f"stage_over_{s.id}"
            if not Alert.query.filter_by(key=key, resolved=False).first():
                db.session.add(Alert(
                    key=key, level='danger',
                    message=f'Stage \"{s.name}\" exceeded estimated cost.',
                    project_id=s.project_id, stage_id=s.id))
        if s.end_date and s.end_date < today and str(s.status).lower() not in ('completed','complete'):
            key = f"stage_delay_{s.id}"
            if not Alert.query.filter_by(key=key, resolved=False).first():
                db.session.add(Alert(
                    key=key, level='warning',
                    message=f'Stage \"{s.name}\" is delayed.',
                    project_id=s.project_id, stage_id=s.id))
    # Project over-budget alerts
    for p in Project.query.all():
        if (p.budget_total or 0) > 0 and p.actual_cost > (p.budget_total or 0):
            key = f"project_over_{p.id}"
            if not Alert.query.filter_by(key=key, resolved=False).first():
                db.session.add(Alert(
                    key=key, level='danger',
                    message=f'Project \"{p.name}\" exceeded budget.',
                    project_id=p.id))
    db.session.commit()


# â”€â”€ Project Report Exports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _project_report_data(pid):
    """Gather all data needed for project exports."""
    p      = Project.query.get_or_404(pid)
    _apply_aggregated_project_costs([p])
    stages = Stage.query.filter_by(project_id=pid).order_by(Stage.id).all()
    _apply_aggregated_stage_costs(stages)

    attendance = (db.session.query(TimeEntry, Worker)
                  .join(Worker, TimeEntry.worker_id == Worker.id)
                  .filter(TimeEntry.project_id == pid)
                  .filter(TimeEntry.is_void == False)
                  .order_by(TimeEntry.check_in).all())

    expenses = Expense.query.filter_by(project_id=pid, is_void=False).order_by(Expense.date).all()

    purchases = (db.session.query(Purchase, Material)
                 .join(Material, Purchase.material_id == Material.id)
                 .filter(Purchase.project_id == pid)
                 .order_by(Purchase.date).all())

    sub_payments = (db.session.query(SubcontractPayment, Subcontractor)
                    .join(Subcontractor, SubcontractPayment.subcontractor_id == Subcontractor.id)
                    .filter(Subcontractor.project_id == pid, SubcontractPayment.is_void == False)
                    .order_by(SubcontractPayment.date).all())

    owner_payments = (OwnerPayment.query
                      .filter_by(project_id=pid, is_void=False)
                      .order_by(OwnerPayment.date)
                      .all())

    # Per-stage cost rollups (single source of truth: aggregated stage costs).
    stage_costs = {}
    for s in stages:
        stage_costs[s.id] = {
            'labour': float(s.stage_labour_cost or 0.0),
            'materials': float(s.stage_material_cost or 0.0),
            'expenses': float(s.stage_expense_cost or 0.0),
            'subcontracts': float(s.stage_subcontract_cost or 0.0)
        }

    # Totals (single source of truth: aggregated project costs).
    total_labour = float(p.total_labour_cost or 0.0)
    total_materials = float(p.total_material_cost or 0.0)
    total_expenses = float(p.total_expense_cost or 0.0)
    total_subcontracts = float(p.total_subcontract_cost or 0.0)
    total_cost = float(p.total_cost or 0.0)

    contract_value   = p.owner_contract_value
    profit           = contract_value - total_cost

    return dict(
        project=p, stages=stages, stage_costs=stage_costs,
        attendance=attendance, expenses=expenses,
        purchases=purchases, sub_payments=sub_payments,
        owner_payments=owner_payments,
        total_hours=sum(float(att.hours or 0) for att, _ in attendance),
        total_labour=total_labour, total_materials=total_materials,
        total_expenses=total_expenses, total_subcontracts=total_subcontracts,
        total_cost=total_cost, contract_value=contract_value, profit=profit,
    )
