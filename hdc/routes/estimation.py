"""HDC routes: Estimation engine, project estimation and code APIs.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from hdc.extensions import db
from hdc.models.projects import CustomFormula, Estimation, EstimationStage
from hdc.services.estimation import CONCRETE_GRADES, CONCRETE_RATIOS, DEFAULT_BAG_CFT, DEFAULT_DRY_FACTOR, DEFAULT_WC_RATIO, _create_project_from_estimation, _normalize_estimation_rows
from hdc.services.lookups import _next_office_staff_code, _next_project_code, _next_worker_code
from hdc.utils.dates import _pkt_now_naive
from hdc.utils.format import _flt, _safe_eval, _to_meters, _to_mm

def register(app):
    """Register Estimation engine, project estimation and code APIs."""
    @app.route('/hdc/estimation', methods=['GET', 'POST'])
    @login_required
    def hdc_estimation():
        result   = None
        formulas = CustomFormula.query.all()
        tab      = request.form.get('tab', request.args.get('tab','concrete'))
        dim_unit = request.form.get('dim_unit', 'm')
        steel_length_unit = request.form.get('steel_length_unit', 'm')
        diameter_unit = request.form.get('diameter_unit', 'mm')
        dry_factor = _flt(request.form.get('dry_factor'), DEFAULT_DRY_FACTOR)
        bag_cft = _flt(request.form.get('bag_cft'), DEFAULT_BAG_CFT)
        wc_ratio = _flt(request.form.get('wc_ratio'), DEFAULT_WC_RATIO)

        if request.method == 'POST':
            calc_type = request.form.get('calc_type','concrete')
            tab       = calc_type

            if calc_type == 'concrete':
                L = _to_meters(_flt(request.form.get('length')), dim_unit)
                W = _to_meters(_flt(request.form.get('width')), dim_unit)
                H = _to_meters(_flt(request.form.get('height')), dim_unit)
                grade   = request.form.get('grade','M20')
                member  = request.form.get('member_type','Slab')
                vol_m3  = L * W * H
                vol_cft = vol_m3 * 35.3147

                c, s, a = CONCRETE_RATIOS.get(grade, CONCRETE_RATIOS['M20'])
                total_parts = float(c + s + a)
                dry_cft = vol_cft * dry_factor

                cement_cft = dry_cft * (c / total_parts)
                sand_cft = dry_cft * (s / total_parts)
                aggregate_cft = dry_cft * (a / total_parts)
                cement_bags = (cement_cft / bag_cft) if bag_cft else 0.0

                water_liters = None
                if wc_ratio and cement_bags:
                    cement_kg = cement_bags * 50.0
                    water_liters = cement_kg * wc_ratio

                result  = dict(
                    type='Concrete', member=member, grade=grade,
                    volume_m3=round(vol_m3,4), volume_cft=round(vol_cft,2),
                    dry_factor=round(dry_factor,2), dry_cft=round(dry_cft,2),
                    cement_bags=round(cement_bags,1),
                    sand_cft=round(sand_cft,0),
                    aggregate_cft=round(aggregate_cft,0),
                    water_liters=round(water_liters,0) if water_liters is not None else None
                )

            elif calc_type == 'steel':
                dia    = _to_mm(_flt(request.form.get('diameter')), diameter_unit)
                length = _to_meters(_flt(request.form.get('steel_length')), steel_length_unit)
                rate   = _flt(request.form.get('rate_per_kg'))
                weight = (dia**2 / 162) * length
                result = dict(type='Steel', diameter=dia, length=length,
                              weight_kg=round(weight,2),
                              cost=round(weight*rate,2) if rate else None)

            elif calc_type == 'custom':
                fid     = request.form.get('formula_id', type=int)
                formula = CustomFormula.query.get(fid) if fid else None
                if formula:
                    var_names = [v.strip() for v in (formula.variables or '').split(',') if v.strip()]
                    var_vals  = {v: _flt(request.form.get(f'var_{v}')) for v in var_names}
                    try:
                        result = dict(type='Custom', formula_name=formula.name,
                                      expression=formula.expression, variables=var_vals,
                                      result=round(_safe_eval(formula.expression, var_vals),4))
                    except Exception as exc:
                        flash(f'Formula error: {exc}', 'danger')
                else:
                    flash('Please select a valid formula.', 'warning')

        return render_template('estimation/estimation.html',
            result=result, tab=tab,
            dim_unit=dim_unit, steel_length_unit=steel_length_unit, diameter_unit=diameter_unit,
            dry_factor=dry_factor, bag_cft=bag_cft, wc_ratio=wc_ratio,
            grades=list(CONCRETE_GRADES.keys()),
            formulas=formulas,
            member_types=['Slab','Beam','Column','Footing','PCC','Wall'])


    # Ã¢â€â‚¬Ã¢â€â‚¬ Project Estimation Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    @app.route('/project-estimation')
    @app.route('/hdc/project-estimation')
    @login_required
    def hdc_project_estimation():
        stage_defs = []
        estimations = Estimation.query.order_by(Estimation.created_at.desc()).all()
        return render_template('estimation/project_estimation.html',
            stage_defs=stage_defs, estimations=estimations)


    @app.route('/project-estimation/save', methods=['POST'])
    @login_required
    def hdc_project_estimation_save():
        data = request.get_json(silent=True) or {}
        mode = (data.get('mode') or '').strip()
        rows = _normalize_estimation_rows(data.get('rows'))
        project_meta = data.get('project_meta') if isinstance(data.get('project_meta'), dict) else {}

        if not rows:
            return jsonify(ok=False, error='Add at least one stage row before saving.')
        grand_total = sum(r.get('total', 0) for r in rows)
        if grand_total <= 0:
            return jsonify(ok=False, error='Total cannot be zero. Please enter rates or quantities.')
        for r in rows:
            if (r.get('total', 0) > 0) and not r.get('stage_name'):
                return jsonify(ok=False, error='Stage name is required for rows with value.')

        est_name = project_meta.get('project_code') or f"Estimation {_pkt_now_naive().strftime('%Y-%m-%d %H:%M')}"
        est = Estimation(name=est_name, total_cost=grand_total, status='draft')
        db.session.add(est); db.session.commit()
        for r in rows:
            db.session.add(EstimationStage(
                estimation_id=est.id,
                stage_name=r.get('stage_name',''),
                calc_type=r.get('type','lump_sum'),
                rate=_flt(r.get('rate')),
                quantity=_flt(r.get('quantity')),
                total=_flt(r.get('total'))
            ))
        db.session.commit()

        if mode == 'project':
            p = _create_project_from_estimation(rows, project_meta, estimation_id=est.id)
            est.status = 'finalized'
            est.project_id = p.id
            est.total_cost = grand_total
            est.updated_at = _pkt_now_naive()
            db.session.commit()
            return jsonify(ok=True, project_id=p.id)

        if mode == 'estimation':
            return jsonify(ok=True, message='Estimation saved.', id=est.id)

        return jsonify(ok=False, error='Invalid save mode.')


    @app.route('/project-estimation/convert/<est_id>', methods=['POST'])
    @login_required
    def hdc_project_estimation_convert(est_id):
        est = Estimation.query.get(est_id)
        if not est:
            flash('Estimation not found.', 'danger')
            return redirect(url_for('hdc_project_estimation'))
        if est.status == 'finalized' and est.project_id:
            flash('Estimation already converted.', 'warning')
            return redirect(url_for('hdc_projects'))

        rows = [
            {
                'stage_name': s.stage_name,
                'type': s.calc_type,
                'rate': s.rate,
                'quantity': s.quantity,
                'total': s.total
            }
            for s in est.stages
        ]
        p = _create_project_from_estimation(rows, {'project_code': est.name}, estimation_id=est.id)
        est.status = 'finalized'
        est.project_id = p.id
        est.updated_at = _pkt_now_naive()
        db.session.commit()
        flash(f'Estimation converted to project "{p.name}".', 'success')
        return redirect(url_for('hdc_projects'))


    # Ã¢â€â‚¬Ã¢â€â‚¬ Code Generators (API) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    @app.route('/hdc/api/next_project_code')
    @login_required
    def hdc_api_next_project_code():
        return jsonify(ok=True, code=_next_project_code())


    @app.route('/hdc/api/next_worker_code')
    @login_required
    def hdc_api_next_worker_code():
        return jsonify(ok=True, code=_next_worker_code())


    @app.route('/hdc/api/next_office_staff_code')
    @login_required
    def hdc_api_next_office_staff_code():
        return jsonify(ok=True, code=_next_office_staff_code())


    @app.route('/hdc/estimation/formulas', methods=['GET', 'POST'])
    @login_required
    def hdc_formulas():
        if request.method == 'POST':
            action = request.form.get('action','add')
            if action == 'add':
                f = CustomFormula(
                    name=request.form.get('name','').strip(),
                    category=request.form.get('category','Custom').strip(),
                    expression=request.form.get('expression','').strip(),
                    variables=request.form.get('variables','').strip(),
                    description=request.form.get('description','').strip())
                try:
                    test_vars = {v.strip(): 1.0 for v in f.variables.split(',') if v.strip()}
                    _safe_eval(f.expression, test_vars)
                    db.session.add(f); db.session.commit()
                    flash(f'Formula "{f.name}" added.', 'success')
                except Exception as exc:
                    flash(f'Invalid expression: {exc}', 'danger')
            elif action == 'delete':
                fid = request.form.get('formula_id', type=int)
                ff  = CustomFormula.query.get(fid)
                if ff: db.session.delete(ff); db.session.commit(); flash('Formula deleted.', 'success')
            return redirect(url_for('hdc_formulas'))
        return render_template('estimation/formulas.html', formulas=CustomFormula.query.all())


    # â”€â”€ API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/hdc/api/formula_vars/<int:fid>')
    @login_required
    def hdc_api_formula_vars(fid):
        f = CustomFormula.query.get_or_404(fid)
        return jsonify({'variables': [v.strip() for v in (f.variables or '').split(',') if v.strip()],
                        'expression': f.expression, 'name': f.name})
