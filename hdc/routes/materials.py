"""HDC routes: Legacy (v1) materials, usage and purchases.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from hdc.extensions import _money_write_required, db
from hdc.models.materials import Material, MaterialUsage, Purchase
from hdc.models.projects import Project, Stage
from hdc.services.purchase import _material_stock_for_scope, _material_stock_map
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.utils.dates import _pkt_today
from hdc.utils.format import _activity_at_for, _flt, _parse_date

def register(app):
    """Register Legacy (v1) materials, usage and purchases."""
    # --- Materials -------------------------------------------------------------
    @app.route('/hdc/materials', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_materials():
        flash('Materials are now managed in Purchases.', 'info')
        return redirect(url_for('hdc_purchase_v2_materials'))
        if request.method == 'POST':
            action = request.form.get('action','add')
            if action == 'add':
                name = request.form.get('name','').strip()
                unit = request.form.get('unit','Nos').strip()
                if not name:
                    flash('Name is required.', 'danger')
                else:
                    db.session.add(Material(name=name, unit=unit))
                    db.session.commit()
                    flash(f'Material "{name}" added.', 'success')
            elif action == 'toggle':
                m = Material.query.get(request.form.get('mid', type=int))
                if m:
                    m.is_active = not m.is_active
                    db.session.commit()
            return redirect(url_for('hdc_materials'))
        materials = Material.query.order_by(Material.name).all()
        stock_map = _material_stock_map()
        return render_template('materials/materials.html', materials=materials, stock_map=stock_map)


    @app.route('/hdc/materials/usage', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_material_usage():
        flash('Material Usage is now managed in Purchases.', 'info')
        return redirect(url_for('hdc_purchase_v2_usage_page'))
        if request.method == 'POST':
            pid = request.form.get('project_id', type=int)
            sid = request.form.get('stage_id', type=int)
            mid = request.form.get('material_id', type=int)
            qty = _flt(request.form.get('qty'))
            if not pid or not sid or not mid:
                flash('Project, stage, and material are required.', 'danger')
                return redirect(url_for('hdc_material_usage'))
            if qty <= 0:
                flash('Usage quantity must be greater than 0.', 'danger')
                return redirect(url_for('hdc_material_usage'))

            p = Project.query.get(pid)
            m = Material.query.get(mid)
            if not p or not m:
                flash('Invalid project or material selected.', 'danger')
                return redirect(url_for('hdc_material_usage'))

            s = Stage.query.get(sid)
            if (not s) or (s.project_id != pid):
                flash('Selected stage does not belong to selected project.', 'danger')
                return redirect(url_for('hdc_material_usage'))

            available = _material_stock_for_scope(mid, project_id=pid, stage_id=sid)
            if qty > available:
                flash(f'Insufficient stock. Available: {available:,.2f} {m.unit}.', 'danger')
                return redirect(url_for('hdc_material_usage'))

            rate = 0.0
            total = 0.0
            used_date = _parse_date(request.form.get('used_at'))
            if _has_recent_duplicate(
                MaterialUsage,
                project_id=pid,
                stage_id=sid,
                material_id=mid,
                qty=qty,
                rate=rate,
                total=total,
                used_at=used_date
            ):
                flash('Duplicate material usage prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_material_usage'))
            db.session.add(MaterialUsage(
                project_id=pid, stage_id=sid, material_id=mid,
                qty=qty, rate=rate, total=total,
                used_at=used_date,
                activity_at=_activity_at_for(used_date)))
            db.session.commit()
            new_balance = available - qty
            flash(f'Material usage recorded. Remaining {m.name}: {new_balance:,.2f} {m.unit}.', 'success')
            return redirect(url_for('hdc_material_usage'))
        projects = Project.query.all()
        stages = Stage.query.all()
        materials = Material.query.filter_by(is_active=True).all()
        records = MaterialUsage.query.order_by(MaterialUsage.activity_at.desc(), MaterialUsage.used_at.desc()).all()
        stock_map = _material_stock_map()
        return render_template('materials/material_usage.html',
            projects=projects, stages=stages, materials=materials, records=records,
            stock_map=stock_map, today=_pkt_today().isoformat())


    @app.route('/hdc/purchases', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_purchases():
        flash('Purchases are now managed in Purchase Orders.', 'info')
        return redirect(url_for('hdc_purchase_v2_purchases'))
        if request.method == 'POST':
            action = (request.form.get('action') or 'purchase').strip().lower()
            if action not in ('purchase', 'return'):
                action = 'purchase'
            qty  = _flt(request.form.get('qty'))
            rate = _flt(request.form.get('rate'))
            purchase_date = _parse_date(request.form.get('date'))
            if qty <= 0:
                flash(('Return quantity' if action == 'return' else 'Purchase quantity') + ' must be greater than zero.', 'danger')
                return redirect(url_for('hdc_purchases'))
            if rate <= 0:
                flash(('Return rate' if action == 'return' else 'Purchase rate') + ' must be greater than zero.', 'danger')
                return redirect(url_for('hdc_purchases'))
            pid = request.form.get('project_id', type=int)
            sid = request.form.get('stage_id', type=int)
            mid = request.form.get('material_id', type=int)
            if not pid or not sid or not mid:
                flash('Project, stage, and material are required.', 'danger')
                return redirect(url_for('hdc_purchases'))
            stage = Stage.query.get(sid)
            if (not stage) or (stage.project_id != pid):
                flash('Selected stage does not belong to selected project.', 'danger')
                return redirect(url_for('hdc_purchases'))
            notes = (request.form.get('notes','') or '').strip()
            supplier_name = (request.form.get('supplier_name','') or '').strip()
            return_ref = (request.form.get('return_ref','') or '').strip()
            return_reason = (request.form.get('return_reason','') or '').strip()
            approved_by = (request.form.get('approved_by','') or '').strip()
            if action == 'return':
                m = Material.query.get(mid)
                available = _material_stock_for_scope(mid, project_id=pid, stage_id=sid)
                if qty > available + 1e-6:
                    unit = m.unit if m else 'unit'
                    flash(f'Cannot return more than available stock. Available: {available:,.2f} {unit}.', 'danger')
                    return redirect(url_for('hdc_purchases'))
            signed_qty = -qty if action == 'return' else qty
            total = signed_qty * rate
            ptype = 'return' if action == 'return' else 'purchase'
            if _has_recent_duplicate(
                Purchase,
                project_id=pid,
                stage_id=sid,
                material_id=mid,
                entry_type=ptype,
                date=purchase_date,
                qty=signed_qty,
                rate=rate,
                total=total,
                return_ref=return_ref,
                notes=notes
            ):
                flash(f'Duplicate {ptype} prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_purchases'))
            db.session.add(Purchase(
                project_id=pid,
                stage_id=sid,
                material_id=mid,
                entry_type=ptype,
                date=purchase_date,
                activity_at=_activity_at_for(purchase_date),
                qty=signed_qty, rate=rate, total=total,
                supplier_name=supplier_name,
                return_ref=return_ref,
                return_reason=return_reason,
                approved_by=approved_by,
                notes=notes))
            db.session.commit()
            flash('Return to supplier recorded.' if action == 'return' else 'Purchase recorded.', 'success')
            return redirect(url_for('hdc_purchases'))

        projects   = Project.query.all()
        materials  = Material.query.filter_by(is_active=True).all()
        stages     = Stage.query.all()
        pid        = request.args.get('project_id', type=int)
        q = db.session.query(Purchase, Material, Project)\
            .join(Material, Purchase.material_id == Material.id)\
            .join(Project, Purchase.project_id == Project.id)
        if pid: q = q.filter(Purchase.project_id == pid)
        records = q.order_by(Purchase.activity_at.desc(), Purchase.date.desc()).all()
        total   = sum(r.Purchase.total for r in records)
        return render_template('materials/purchases.html',
            projects=projects, materials=materials, stages=stages,
            records=records, total=total,
            selected_project=pid, today=_pkt_today().isoformat())


    @app.route('/hdc/api/material_stock/<int:mid>')
    @login_required
    def hdc_api_material_stock(mid):
        project_id = request.args.get('project_id', type=int)
        stage_id = request.args.get('stage_id', type=int)
        remaining = _material_stock_for_scope(mid, project_id=project_id, stage_id=stage_id)
        return jsonify({
            'material_id': mid,
            'project_id': project_id,
            'stage_id': stage_id,
            'remaining': float(remaining or 0.0)
        })
