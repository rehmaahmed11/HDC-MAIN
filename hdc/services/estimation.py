"""HDC services.estimation — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import json
import os

from flask import current_app

from hdc.config import get_runtime_settings
from hdc.extensions import db
from hdc.models.projects import Project, Stage
from hdc.services.lookups import _next_project_code
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _flt

def _load_estimations():
    estimation_store = get_runtime_settings().estimation_store
    if not os.path.exists(estimation_store):
        return []
    try:
        with open(estimation_store, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception as ex:
        current_app.logger.warning('Failed to load project estimations JSON: %s', ex)
    return []


def _save_estimations(items):
    estimation_store = get_runtime_settings().estimation_store
    try:
        with open(estimation_store, 'w', encoding='utf-8') as f:
            json.dump(items, f, indent=2)
        return True
    except Exception as ex:
        current_app.logger.warning('Failed to save project estimations JSON: %s', ex)
        return False


def _normalize_estimation_rows(rows):
    normalized = []
    for r in (rows or []):
        stage = (r.get('stage_name') if isinstance(r, dict) else '') or ''
        stage = stage.strip()
        rtype = (r.get('type') if isinstance(r, dict) else '') or 'lump_sum'
        if rtype not in ('lump_sum', 'sqft'):
            rtype = 'lump_sum'
        rate = _flt(r.get('rate') if isinstance(r, dict) else 0)
        qty  = _flt(r.get('quantity') if isinstance(r, dict) else 0)
        if rate < 0: rate = 0
        if qty < 0: qty = 0
        total = rate if rtype == 'lump_sum' else rate * qty
        if not isinstance(total, (int, float)) or not (total >= 0):
            total = 0
        normalized.append({
            'stage_name': stage,
            'type': rtype,
            'rate': rate,
            'quantity': 0 if rtype == 'lump_sum' else qty,
            'total': total
        })
    return normalized


def _create_project_from_estimation(rows, project_meta, estimation_id=None):
    project_code = (project_meta.get('project_code') if project_meta else '') or ''
    name = (project_meta.get('name') if project_meta else '') or ''
    client = (project_meta.get('client') if project_meta else '') or ''
    client_phone = (project_meta.get('client_phone') if project_meta else '') or ''
    location = (project_meta.get('location') if project_meta else '') or ''
    project_code = (project_code or '').strip().upper()
    if not project_code or Project.query.filter_by(project_code=project_code).first():
        project_code = _next_project_code()
    if not name:
        name = project_code or f"Estimation Project {_pkt_now_naive().strftime('%Y-%m-%d %H:%M')}"
    grand_total = sum(r.get('total', 0) for r in rows)
    p = Project(
        project_code=project_code,
        name=name,
        client=client,
        client_phone=client_phone,
        location=location,
        total_constructed_sqft=0.0,
        owner_rate_per_sqft=0.0,
        owner_lump_sum=grand_total,
        contract_type='lump_sum',
        start_date=_pkt_today(),
        status='Active',
        estimation_id=estimation_id,
        budget_total=grand_total,
        planned_start=_pkt_today())
    db.session.add(p); db.session.commit()

    defs = {}
    for r in rows:
        stage_name = r.get('stage_name', '').strip()
        if not stage_name:
            continue
        basis = 'Lump Sum' if r.get('type') == 'lump_sum' else 'Per Sq Ft'
        rate = _flt(r.get('rate'))
        qty = _flt(r.get('quantity'))
        lump = rate if r.get('type') == 'lump_sum' else 0.0
        if r.get('type') == 'sqft':
            lump = 0.0
        def_id = defs.get(stage_name.lower())
        s = Stage(
            project_id=p.id,
            definition_id=def_id,
            name=stage_name,
            status='Active',
            estimated_cost=_flt(r.get('total')),
            progress=0.0,
            contract_basis=basis,
            rate_per_sqft=rate if r.get('type') == 'sqft' else 0.0,
            discount_per_sqft=0.0,
            qty_sqft=qty if r.get('type') == 'sqft' else 0.0,
            lump_sum_value=lump,
            original_contract_basis=basis,
            original_rate_per_sqft=rate if r.get('type') == 'sqft' else 0.0,
            original_discount_per_sqft=0.0,
            original_qty_sqft=qty if r.get('type') == 'sqft' else 0.0,
            original_lump_sum_value=lump)
        db.session.add(s)
    db.session.commit()
    return p


# Estimation defaults
CONCRETE_GRADES = {
    'M15': {'cement': 6.3,  'sand': 0.44, 'aggregate': 0.88},
    'M20': {'cement': 8.0,  'sand': 0.55, 'aggregate': 1.1},
    'M25': {'cement': 9.5,  'sand': 0.45, 'aggregate': 0.9},
    'M30': {'cement': 11.0, 'sand': 0.38, 'aggregate': 0.76},
}


CONCRETE_RATIOS = {
    'M15': (1, 2, 4),
    'M20': (1, 1.5, 3),
    'M25': (1, 1, 2),
    'M30': (1, 0.75, 1.5),
}


DEFAULT_DRY_FACTOR = 1.54


DEFAULT_BAG_CFT = 1.25


DEFAULT_WC_RATIO = 0.5
