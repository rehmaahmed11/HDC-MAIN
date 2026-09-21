"""HDC routes: Dashboard, KPI drill-down and cost-entries explorer.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from datetime import datetime

from flask import flash, redirect, render_template, url_for
from flask_login import login_required
from sqlalchemy import func

from hdc.extensions import db
from hdc.models.accounts import Expense, ExpenseCategory, OwnerPayment
from hdc.models.materials import Purchase, UsageLogV2
from hdc.models.projects import Project, Stage
from hdc.models.subcontract import SubcontractPayment, Subcontractor
from hdc.models.workforce import Attendance, TimeEntry, Worker
from hdc.services.aggregation import _apply_aggregated_project_costs
from hdc.services.ledger import _office_expense_total, _personal_expense_total
from hdc.utils.dates import _pkt_today

def register(app):
    """Register Dashboard, KPI drill-down and cost-entries explorer."""
    # --- Dashboard -------------------------------------------------------------
    @app.route('/hdc/')
    @login_required
    def hdc_dashboard():
        projects = Project.query.all()
        _apply_aggregated_project_costs(projects)
        active   = [p for p in projects if str(p.status).lower() in ('active','planned','active ')]
        project_ids = [int(p.id) for p in projects if p and p.id]
        received_map = {}
        if project_ids:
            received_map = dict(
                db.session.query(
                    OwnerPayment.project_id,
                    func.coalesce(func.sum(OwnerPayment.amount), 0.0)
                )
                .filter(OwnerPayment.project_id.in_(project_ids), OwnerPayment.is_void == False)
                .group_by(OwnerPayment.project_id)
                .all()
            )

        total_contract        = sum(p.owner_contract_value for p in projects)
        total_received        = sum(float(received_map.get(int(p.id), 0.0) or 0.0) for p in projects)
        total_cost            = sum(p.total_cost for p in projects)
        gross_margin          = sum(p.gross_margin for p in projects)
        office_expense_total  = _office_expense_total()
        personal_expense_total = _personal_expense_total()
        net_profit            = sum(p.net_profit for p in projects) - office_expense_total
        pending_receivable    = total_contract - total_received

        chart_projects  = projects[:8]
        chart_labels    = [p.name[:15] for p in chart_projects]
        chart_contracts = [round(p.owner_contract_value, 0) for p in chart_projects]
        chart_expenses  = [round(p.total_cost, 0) for p in chart_projects]
        chart_project_ids = [int(p.id) for p in chart_projects]

        exp_cats   = (db.session.query(ExpenseCategory.name, func.sum(Expense.amount))
                      .join(Expense, Expense.category_id == ExpenseCategory.id)
                      .filter(Expense.is_void == False)
                      .group_by(ExpenseCategory.name)
                      .all())
        pie_labels = [c[0] or 'Other' for c in exp_cats]
        pie_values = [round(c[1] or 0, 0) for c in exp_cats]

        today = _pkt_today()
        today_expenses = float(db.session.query(func.coalesce(func.sum(Expense.amount), 0.0))
                               .filter(Expense.date == today, Expense.is_void == False)
                               .scalar() or 0.0)
        start_dt = datetime.combine(today, datetime.min.time())
        end_dt = datetime.combine(today, datetime.max.time())
        workers_present = int(db.session.query(func.count(func.distinct(TimeEntry.worker_id)))
                              .filter(
                                  TimeEntry.is_void == False,
                                  TimeEntry.check_in >= start_dt,
                                  TimeEntry.check_in <= end_dt
                              )
                              .scalar() or 0)
        delayed_stages = int(db.session.query(func.count(Stage.id))
                             .filter(
                                 Stage.end_date.isnot(None),
                                 Stage.end_date < today,
                                 func.lower(func.trim(func.coalesce(Stage.status, ''))).notin_(['completed', 'complete'])
                             )
                             .scalar() or 0)
        budget_overruns = sum(1 for p in projects if (p.budget_total or 0) > 0 and p.actual_cost > (p.budget_total or 0))

        return render_template('dashboard/dashboard.html',
            active_count=len(active), total_project_count=len(projects),
            total_contract=total_contract, total_received=total_received,
            total_expenses=total_cost, gross_margin=gross_margin,
            net_profit=net_profit, pending_receivable=pending_receivable,
            office_expense_total=office_expense_total,
            personal_expense_total=personal_expense_total,
            chart_labels=chart_labels, chart_contracts=chart_contracts,
            chart_expenses=chart_expenses, chart_project_ids=chart_project_ids,
            pie_labels=pie_labels, pie_values=pie_values,
            recent_projects=projects[:5],
            today_expenses=today_expenses, workers_present=workers_present,
            delayed_stages=delayed_stages, budget_overruns=budget_overruns)


    @app.route('/hdc/kpi/<string:metric>')
    @login_required
    def hdc_kpi_detail(metric):
        metric = (metric or '').strip().lower()
        today = _pkt_today()
        page_title = 'KPI Detail'
        subtitle = ''
        columns = []
        rows = []
        grand_total = 0.0
        grand_total_label = 'Grand Total'
        value_format = 'number'
        show_table_footer = False
        breakdown_totals = None
        if metric == 'active_projects':
            active = [p for p in Project.query.order_by(Project.created_at.desc()).all() if str(p.status).lower().strip() in ('active', 'planned')]
            page_title = 'Active Projects'
            subtitle = f'{len(active)} active/planned project(s)'
            columns = [
                {'key': 'code', 'label': 'Code'},
                {'key': 'name', 'label': 'Project'},
                {'key': 'client', 'label': 'Owner/Client'},
                {'key': 'location', 'label': 'Location'},
                {'key': 'status', 'label': 'Status'},
            ]
            rows = [{
                'code': p.project_code,
                'name': p.name,
                'client': p.client or '-',
                'location': p.location or '-',
                'status': p.status or '-',
            } for p in active]
            grand_total = float(len(rows))
            grand_total_label = 'Active Projects Count'
        elif metric == 'total_expenses':
            projects = Project.query.order_by(Project.created_at.desc()).all()
            page_title = 'Total Expenses (PKR)'
            subtitle = 'Each spend event is listed below with project-wise subtotal rows'
            columns = [
                {'key': 'dt', 'label': 'Date/Time'},
                {'key': 'code', 'label': 'Code'},
                {'key': 'project', 'label': 'Project'},
                {'key': 'stage', 'label': 'Stage'},
                {'key': 'cost_head', 'label': 'Cost Head'},
                {'key': 'to_whom', 'label': 'To Whom'},
                {'key': 'details', 'label': 'Details'},
                {'key': 'amount', 'label': 'Amount', 'align': 'text-end', 'format': 'currency'},
            ]
            labour_sum = 0.0
            material_sum = 0.0
            expense_sum = 0.0
            subcontract_sum = 0.0
            grand_total = 0.0
            worker_name_map = {w.id: (w.name or f'Worker #{w.id}') for w in Worker.query.all()}
            for p in projects:
                project_rows = []
                project_total = 0.0

                time_rows = (TimeEntry.query
                             .filter(TimeEntry.project_id == p.id, TimeEntry.is_void == False)
                             .order_by(TimeEntry.activity_at.asc(), TimeEntry.id.asc())
                             .all())
                for t in time_rows:
                    amount = float(t.wage_calculated or 0.0)
                    if abs(amount) < 1e-9:
                        continue
                    labour_sum += amount
                    project_total += amount
                    project_rows.append({
                        'dt': t.activity_at or t.check_in,
                        'code': p.project_code,
                        'project': p.name,
                        'stage': t.stage.name if t.stage else '-',
                        'cost_head': 'Labour',
                        'to_whom': worker_name_map.get(t.worker_id, f'Worker #{t.worker_id}'),
                        'details': f'Hours {float(t.hours or 0.0):,.2f} | OT {float(t.overtime or 0.0):,.2f}',
                        'amount': amount,
                    })

                att_rows = (Attendance.query
                            .filter(Attendance.project_id == p.id)
                            .order_by(Attendance.activity_at.asc(), Attendance.id.asc())
                            .all())
                migrated_att_ids = {
                    int(aid) for (aid,) in db.session.query(TimeEntry.attendance_id)
                    .filter(
                        TimeEntry.project_id == p.id,
                        TimeEntry.attendance_id.isnot(None)
                    )
                    .all()
                    if aid
                }
                for a in att_rows:
                    if a.id in migrated_att_ids:
                        continue
                    amount = float(a.total_wage or 0.0)
                    if abs(amount) < 1e-9:
                        continue
                    labour_sum += amount
                    project_total += amount
                    project_rows.append({
                        'dt': a.activity_at or datetime.combine(a.date, datetime.min.time()),
                        'code': p.project_code,
                        'project': p.name,
                        'stage': a.stage.name if a.stage else '-',
                        'cost_head': 'Labour (Legacy)',
                        'to_whom': worker_name_map.get(a.worker_id, f'Worker #{a.worker_id}'),
                        'details': f'Legacy attendance | Hours {float(a.hours_worked or 0.0):,.2f}',
                        'amount': amount,
                    })

                pur_rows = (Purchase.query
                            .filter(Purchase.project_id == p.id)
                            .order_by(Purchase.activity_at.asc(), Purchase.id.asc())
                            .all())
                for pur in pur_rows:
                    amount = float(pur.total or 0.0)
                    if abs(amount) < 1e-9:
                        continue
                    material_sum += amount
                    project_total += amount
                    mat_name = pur.material.name if pur.material else f'Material #{pur.material_id}'
                    ptype = (pur.entry_type or 'purchase').strip().lower()
                    is_return = (ptype == 'return') or amount < 0
                    project_rows.append({
                        'dt': pur.activity_at or datetime.combine(pur.date or _pkt_today(), datetime.min.time()),
                        'code': p.project_code,
                        'project': p.name,
                        'stage': pur.stage.name if pur.stage else '-',
                        'cost_head': ('Material Return' if is_return else 'Material Purchase'),
                        'to_whom': pur.supplier_name or mat_name,
                        'details': f'{mat_name} | Qty {float(pur.qty or 0.0):,.2f} @ {float(pur.rate or 0.0):,.2f}',
                        'amount': amount,
                    })

                exp_rows = (Expense.query
                            .filter(Expense.project_id == p.id)
                            .order_by(Expense.activity_at.asc(), Expense.id.asc())
                            .all())
                for e in exp_rows:
                    amount = float(e.amount or 0.0)
                    if abs(amount) < 1e-9:
                        continue
                    expense_sum += amount
                    project_total += amount
                    to_whom = '-'
                    if getattr(e, 'tip_worker_id', None):
                        to_whom = worker_name_map.get(e.tip_worker_id, f'Worker #{e.tip_worker_id}')
                    elif e.remarks:
                        to_whom = e.remarks
                    project_rows.append({
                        'dt': e.activity_at or datetime.combine(e.date or _pkt_today(), datetime.min.time()),
                        'code': p.project_code,
                        'project': p.name,
                        'stage': e.stage.name if e.stage else '-',
                        'cost_head': f'Expense: {(e.category or "General")}',
                        'to_whom': to_whom,
                        'details': e.remarks or '-',
                        'amount': amount,
                    })

                sub_pay_rows = (SubcontractPayment.query
                                .join(Subcontractor, Subcontractor.id == SubcontractPayment.subcontractor_id)
                                .filter(Subcontractor.project_id == p.id, SubcontractPayment.is_void == False)
                                .order_by(SubcontractPayment.activity_at.asc(), SubcontractPayment.id.asc())
                                .all())
                for sp in sub_pay_rows:
                    amount = float(sp.amount or 0.0)
                    if abs(amount) < 1e-9:
                        continue
                    subcontract_sum += amount
                    project_total += amount
                    srow = sp.subcontractor
                    etype = (sp.entry_type or 'payment').strip().lower()
                    project_rows.append({
                        'dt': sp.activity_at or sp.created_at,
                        'code': p.project_code,
                        'project': p.name,
                        'stage': sp.stage.name if sp.stage else (srow.stage_rel.name if srow and srow.stage_rel else '-'),
                        'cost_head': ('Subcontract Settlement' if etype == 'settlement' else 'Subcontract Payment'),
                        'to_whom': srow.name if srow else f'Subcontractor #{sp.subcontractor_id}',
                        'details': sp.notes or '-',
                        'amount': amount,
                    })

                project_rows.sort(key=lambda r: (r.get('dt') or datetime.min, r.get('cost_head') or ''))
                rows.extend(project_rows)
                if project_rows:
                    rows.append({
                        '_is_subtotal': True,
                        'dt': None,
                        'code': p.project_code,
                        'project': p.name,
                        'stage': '',
                        'cost_head': 'Subtotal',
                        'to_whom': '',
                        'details': f'Subtotal for {p.project_code}',
                        'amount': project_total,
                    })
                    grand_total += project_total

            grand_total_label = page_title
            value_format = 'currency'
            show_table_footer = True
            breakdown_totals = {
                'labour': labour_sum,
                'material': material_sum,
                'expense': expense_sum,
                'subcontract': subcontract_sum,
                'total': grand_total
            }
        elif metric in ('contract_value', 'total_received', 'pending_receivable', 'gross_margin', 'net_profit'):
            projects = Project.query.order_by(Project.created_at.desc()).all()
            _apply_aggregated_project_costs(projects)
            project_ids = [int(p.id) for p in projects if p and p.id]
            received_map = {}
            if project_ids:
                received_map = dict(
                    db.session.query(
                        OwnerPayment.project_id,
                        func.coalesce(func.sum(OwnerPayment.amount), 0.0)
                    )
                    .filter(OwnerPayment.project_id.in_(project_ids), OwnerPayment.is_void == False)
                    .group_by(OwnerPayment.project_id)
                    .all()
                )
            metric_meta = {
                'contract_value': ('Contract Value (PKR)', 'Contract value by project', lambda p: float(p.owner_contract_value or 0.0)),
                'total_received': ('Total Received (PKR)', 'Owner payments received by project', lambda p: float(received_map.get(int(p.id), 0.0) or 0.0)),
                'pending_receivable': ('Pending Receivable (PKR)', 'Pending receivable by project', lambda p: float((p.owner_contract_value or 0.0) - float(received_map.get(int(p.id), 0.0) or 0.0))),
                'gross_margin': ('Gross Margin (PKR)', 'Gross margin by project', lambda p: float(p.gross_margin or 0.0)),
                'net_profit': ('Net Profit (PKR)', 'Net profit by project', lambda p: float(p.net_profit or 0.0)),
            }
            title, sub, value_fn = metric_meta[metric]
            page_title = title
            subtitle = sub
            columns = [
                {'key': 'code', 'label': 'Code'},
                {'key': 'project', 'label': 'Project'},
                {'key': 'client', 'label': 'Owner/Client'},
                {'key': 'status', 'label': 'Status'},
                {'key': 'amount', 'label': 'Amount', 'align': 'text-end', 'format': 'currency'},
            ]
            for p in projects:
                amount = value_fn(p)
                rows.append({
                    'code': p.project_code,
                    'project': p.name,
                    'client': p.client or '-',
                    'status': p.status or '-',
                    'amount': amount,
                })
            if metric == 'net_profit':
                office_expense_total = _office_expense_total()
                if abs(office_expense_total) > 1e-9:
                    rows.append({
                        'code': 'OFFICE',
                        'project': 'Office Overhead (Non-project)',
                        'client': '-',
                        'status': '-',
                        'amount': -float(office_expense_total or 0.0),
                    })
            grand_total = float(sum(r['amount'] for r in rows))
            grand_total_label = title
            value_format = 'currency'
            show_table_footer = True
        elif metric == 'today_expenses':
            exp_rows = (Expense.query
                        .filter(Expense.date == today)
                        .order_by(Expense.activity_at.desc(), Expense.id.desc())
                        .all())
            page_title = "Today's Expenses"
            subtitle = today.isoformat()
            columns = [
                {'key': 'dt', 'label': 'Date/Time'},
                {'key': 'project', 'label': 'Project'},
                {'key': 'stage', 'label': 'Stage'},
                {'key': 'category', 'label': 'Category'},
                {'key': 'remarks', 'label': 'Remarks'},
                {'key': 'amount', 'label': 'Amount', 'align': 'text-end', 'format': 'currency'},
            ]
            for e in exp_rows:
                rows.append({
                    'dt': e.activity_at or datetime.combine(e.date or today, datetime.min.time()),
                    'project': e.project.name if getattr(e, 'project', None) else '-',
                    'stage': e.stage.name if getattr(e, 'stage', None) else '-',
                    'category': e.category or '-',
                    'remarks': e.remarks or '-',
                    'amount': float(e.amount or 0.0),
                })
            grand_total = float(sum(r['amount'] for r in rows))
            grand_total_label = "Today's Expenses Total"
            value_format = 'currency'
            show_table_footer = True
        elif metric == 'workers_present':
            start_dt = datetime.combine(today, datetime.min.time())
            end_dt = datetime.combine(today, datetime.max.time())
            entries = (TimeEntry.query
                       .filter(
                           TimeEntry.is_void == False,
                           TimeEntry.check_in >= start_dt,
                           TimeEntry.check_in <= end_dt
                       )
                       .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
                       .all())
            seen = set()
            page_title = 'Workers Present'
            subtitle = today.isoformat()
            columns = [
                {'key': 'check_in', 'label': 'Check In'},
                {'key': 'worker', 'label': 'Worker'},
                {'key': 'project', 'label': 'Project'},
                {'key': 'stage', 'label': 'Stage'},
                {'key': 'hours', 'label': 'Hours', 'align': 'text-end', 'format': 'float2'},
            ]
            for t in entries:
                if t.worker_id in seen:
                    continue
                seen.add(t.worker_id)
                rows.append({
                    'check_in': t.check_in,
                    'worker': t.worker.name if t.worker else f'Worker #{t.worker_id}',
                    'project': t.project.name if t.project else '-',
                    'stage': t.stage.name if t.stage else '-',
                    'hours': float(t.hours or 0.0),
                })
            grand_total = float(len(rows))
            grand_total_label = 'Workers Present Count'
        elif metric == 'delayed_stages':
            stage_rows = (Stage.query
                          .filter(Stage.end_date.isnot(None))
                          .order_by(Stage.end_date.asc(), Stage.id.asc())
                          .all())
            page_title = 'Delayed Stages'
            subtitle = f'As of {today.isoformat()}'
            columns = [
                {'key': 'project', 'label': 'Project'},
                {'key': 'stage', 'label': 'Stage'},
                {'key': 'planned_end', 'label': 'Planned End'},
                {'key': 'status', 'label': 'Status'},
                {'key': 'days_delayed', 'label': 'Days Delayed', 'align': 'text-end', 'format': 'number'},
            ]
            for s in stage_rows:
                if not s.end_date:
                    continue
                if s.end_date >= today:
                    continue
                if str(s.status or '').lower() in ('completed', 'complete'):
                    continue
                days_delayed = (today - s.end_date).days
                rows.append({
                    'project': s.project.name if s.project else '-',
                    'stage': s.name,
                    'planned_end': s.end_date.isoformat(),
                    'status': s.status or '-',
                    'days_delayed': int(days_delayed),
                })
            grand_total = float(len(rows))
            grand_total_label = 'Delayed Stages Count'
        elif metric == 'budget_overruns':
            projects = Project.query.order_by(Project.created_at.desc()).all()
            page_title = 'Budget Overruns'
            subtitle = 'Projects where actual cost is above budget'
            columns = [
                {'key': 'code', 'label': 'Code'},
                {'key': 'project', 'label': 'Project'},
                {'key': 'budget', 'label': 'Budget', 'align': 'text-end', 'format': 'currency'},
                {'key': 'actual_cost', 'label': 'Actual Cost', 'align': 'text-end', 'format': 'currency'},
                {'key': 'overrun', 'label': 'Overrun', 'align': 'text-end', 'format': 'currency'},
            ]
            for p in projects:
                budget = float(p.budget_total or 0.0)
                actual = float(p.actual_cost or 0.0)
                if budget <= 0 or actual <= budget:
                    continue
                overrun = actual - budget
                rows.append({
                    'code': p.project_code,
                    'project': p.name,
                    'budget': budget,
                    'actual_cost': actual,
                    'overrun': overrun,
                })
            grand_total = float(sum(float(r['overrun'] or 0.0) for r in rows))
            grand_total_label = 'Total Overrun'
            value_format = 'currency'
            show_table_footer = True
        else:
            flash('Unsupported KPI detail requested.', 'warning')
            return redirect(url_for('hdc_dashboard'))

        return render_template('dashboard/kpi_detail.html',
            metric=metric,
            page_title=page_title,
            subtitle=subtitle,
            columns=columns,
            rows=rows,
            grand_total=grand_total,
            grand_total_label=grand_total_label,
            value_format=value_format,
            show_table_footer=show_table_footer,
            breakdown_totals=breakdown_totals
        )


    @app.route('/hdc/cost-entries/<string:scope>/<int:target_id>/<string:head>')
    @login_required
    def hdc_cost_entries(scope, target_id, head):
        scope = (scope or '').strip().lower()
        head = (head or '').strip().lower()
        if scope not in ('project', 'stage') or head not in ('labour', 'material', 'expense'):
            flash('Invalid cost detail request.', 'warning')
            return redirect(url_for('hdc_dashboard'))

        if scope == 'project':
            obj = Project.query.get_or_404(target_id)
            page_title = f'{obj.name} - {head.title()} Entries'
            subtitle = f'Project {obj.project_code}'
            back_url = url_for('hdc_project_detail', pid=obj.id, show_cost_split=1)
            filter_project_id = obj.id
            filter_stage_id = None
        else:
            obj = Stage.query.get_or_404(target_id)
            page_title = f'{obj.name} - {head.title()} Entries'
            subtitle = f'Project {obj.project.project_code if obj.project else ""}'
            back_url = url_for('hdc_stage_ledger', sid=obj.id, show_cost_split=1)
            filter_project_id = obj.project_id
            filter_stage_id = obj.id

        columns = [
            {'key': 'dt', 'label': 'Date/Time'},
            {'key': 'project', 'label': 'Project'},
            {'key': 'stage', 'label': 'Stage'},
            {'key': 'entry_type', 'label': 'Type'},
            {'key': 'to_whom', 'label': 'To/From'},
            {'key': 'details', 'label': 'Details'},
            {'key': 'amount', 'label': 'Amount', 'align': 'text-end', 'format': 'currency'},
        ]
        rows = []

        if head == 'labour':
            worker_name_map = {w.id: (w.name or f'Worker #{w.id}') for w in Worker.query.all()}
            tq = TimeEntry.query.filter(TimeEntry.is_void == False)
            if filter_project_id:
                tq = tq.filter(TimeEntry.project_id == filter_project_id)
            if filter_stage_id:
                tq = tq.filter(TimeEntry.stage_id == filter_stage_id)
            for t in tq.order_by(TimeEntry.activity_at.asc(), TimeEntry.id.asc()).all():
                rows.append({
                    'dt': t.activity_at or t.check_in,
                    'project': t.project.name if t.project else '-',
                    'stage': t.stage.name if t.stage else '-',
                    'entry_type': 'Time Entry',
                    'to_whom': worker_name_map.get(t.worker_id, f'Worker #{t.worker_id}'),
                    'details': f'Hours {float(t.hours or 0.0):,.2f} | OT {float(t.overtime or 0.0):,.2f}',
                    'amount': float(t.wage_calculated or 0.0),
                })
            aq = Attendance.query
            if filter_project_id:
                aq = aq.filter(Attendance.project_id == filter_project_id)
            if filter_stage_id:
                aq = aq.filter(Attendance.stage_id == filter_stage_id)
            for a in aq.order_by(Attendance.activity_at.asc(), Attendance.id.asc()).all():
                rows.append({
                    'dt': a.activity_at or datetime.combine(a.date, datetime.min.time()),
                    'project': a.project.name if a.project else '-',
                    'stage': a.stage.name if a.stage else '-',
                    'entry_type': 'Legacy Attendance',
                    'to_whom': worker_name_map.get(a.worker_id, f'Worker #{a.worker_id}'),
                    'details': f'Hours {float(a.hours_worked or 0.0):,.2f}',
                    'amount': float(a.total_wage or 0.0),
                })
        elif head == 'material':
            pq = Purchase.query
            if filter_project_id:
                pq = pq.filter(Purchase.project_id == filter_project_id)
            if filter_stage_id:
                pq = pq.filter(Purchase.stage_id == filter_stage_id)
            for pur in pq.order_by(Purchase.activity_at.asc(), Purchase.id.asc()).all():
                mat_name = pur.material.name if pur.material else f'Material #{pur.material_id}'
                ptype = (pur.entry_type or 'purchase').strip().lower()
                rows.append({
                    'dt': pur.activity_at or datetime.combine(pur.date or _pkt_today(), datetime.min.time()),
                    'project': pur.project.name if pur.project else '-',
                    'stage': pur.stage.name if pur.stage else '-',
                    'entry_type': ('Material Return' if ptype == 'return' else 'Material Purchase'),
                    'to_whom': pur.supplier_name or mat_name,
                    'details': f'{mat_name} | Qty {float(pur.qty or 0.0):,.2f} @ {float(pur.rate or 0.0):,.2f}',
                    'amount': float(pur.total or 0.0),
                })
            uq = UsageLogV2.query.filter(UsageLogV2.is_void == False)
            if filter_project_id:
                uq = uq.filter(UsageLogV2.project_id == filter_project_id)
            if filter_stage_id:
                uq = uq.filter(UsageLogV2.stage_id == filter_stage_id)
            for u in uq.order_by(UsageLogV2.created_at.asc(), UsageLogV2.id.asc()).all():
                mat_name = u.material.name if u.material else f'Material #{u.material_id}'
                unit_cost = (float(u.cost or 0.0) / float(u.quantity or 1.0)) if float(u.quantity or 0.0) > 0 else 0.0
                rows.append({
                    'dt': u.created_at or datetime.combine(u.date or _pkt_today(), datetime.min.time()),
                    'project': u.project.name if u.project else '-',
                    'stage': u.stage.name if u.stage else '-',
                    'entry_type': 'Material Usage (V2)',
                    'to_whom': mat_name,
                    'details': f'{mat_name} | Qty {float(u.quantity or 0.0):,.2f} @ {unit_cost:,.2f}',
                    'amount': float(u.cost or 0.0),
                })
        else:
            eq = Expense.query.filter(Expense.is_void == False)
            if filter_project_id:
                eq = eq.filter(Expense.project_id == filter_project_id)
            if filter_stage_id:
                eq = eq.filter(Expense.stage_id == filter_stage_id)
            for e in eq.order_by(Expense.activity_at.asc(), Expense.id.asc()).all():
                rows.append({
                    'dt': e.activity_at or datetime.combine(e.date or _pkt_today(), datetime.min.time()),
                    'project': e.project.name if getattr(e, 'project', None) else '-',
                    'stage': e.stage.name if getattr(e, 'stage', None) else '-',
                    'entry_type': f'Expense: {e.category or "General"}',
                    'to_whom': (e.remarks or '-'),
                    'details': e.remarks or '-',
                    'amount': float(e.amount or 0.0),
                })

        rows.sort(key=lambda r: (r.get('dt') or datetime.min, r.get('entry_type') or ''))
        grand_total = float(sum(float(r.get('amount') or 0.0) for r in rows))
        return render_template('dashboard/kpi_detail.html',
            metric=f'{scope}_{head}',
            page_title=page_title,
            subtitle=subtitle,
            columns=columns,
            rows=rows,
            grand_total=grand_total,
            grand_total_label=f'{head.title()} Total',
            value_format='currency',
            show_table_footer=True,
            breakdown_totals=None,
            back_url=back_url
        )
