#!/usr/bin/env python3
"""Seed a realistic HDC Tools demo so the position dashboard has something to show.

Creates own tools, two sites, internal rentals (some transferred site to site),
external customer rentals, partial returns and one overdue rental — then the
dashboard can demonstrate: total owned vs in store vs own sites vs customers,
the Site1 > Site2 chain, and the "needs attention" panel.

Idempotent: skips work if the demo tools already exist.

Usage:
    python3 scripts/seed_tools_demo.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_INSTANCE_DIR', '/home/user/HDC-MAIN/hdc_instance')

from datetime import timedelta                                            # noqa: E402

from hdc.app import create_app                                            # noqa: E402
from hdc.extensions import db                                             # noqa: E402
from hdc.models.projects import Project, Stage                            # noqa: E402
from hdc.models.tool_rental import (                                      # noqa: E402
    Tool, ToolCategory, ToolRental, ToolRentalItem, ToolRentalTransfer,
    ToolRentalTransferItem,
)
from hdc.services.tool_rental import create_movement_log                  # noqa: E402
from hdc.services.tool_tracking import (                                  # noqa: E402
    allocate_transfer_qty, record_transfer_items, tools_reconciliation,
    tool_ledger,
)
from hdc.utils.dates import _pkt_today                                    # noqa: E402

DEMO_CODE = 'TOOL-DEMO-VIB'


def _get_or_create_project(code, name, client, location):
    proj = Project.query.filter_by(project_code=code).first()
    if not proj:
        proj = Project(project_code=code, name=name, client=client, location=location)
        db.session.add(proj)
        db.session.flush()
    return proj


def _get_or_create_stage(proj, name):
    stage = Stage.query.filter_by(project_id=proj.id, name=name).first()
    if not stage:
        stage = Stage(project_id=proj.id, name=name)
        db.session.add(stage)
        db.session.flush()
    return stage


def _get_or_create_category(name):
    cat = ToolCategory.query.filter_by(name=name).first()
    if not cat:
        cat = ToolCategory(name=name, active_status=True)
        db.session.add(cat)
        db.session.flush()
    return cat


def _get_or_create_tool(code, name, qty, rate, cost, cat, condition='good', unit='pcs'):
    tool = Tool.query.filter_by(tool_code=code).first()
    if tool:
        return tool
    tool = Tool(tool_code=code, name=name, category_id=cat.id, unit=unit,
                total_quantity=qty, purchase_cost=cost, rental_rate_per_day=rate,
                condition=condition, status='active', is_void=False)
    db.session.add(tool)
    db.session.flush()
    create_movement_log(tool_id=tool.id, rental_id=None, movement_type='purchase_in',
                        from_label='Supplier / Purchase', to_label='Warehouse / Store',
                        qty=qty, notes=f'Initial stock: {qty} {unit}')
    return tool


def _create_rental(code, tool_qty, renter_type, rental_date, project=None, stage=None,
                   customer_name=None, customer_phone=None, billing='per_day',
                   expected_return=None):
    if ToolRental.query.filter_by(rental_code=code).first():
        return None
    rental = ToolRental(rental_code=code, renter_type=renter_type,
                        project_id=project.id if project else None,
                        stage_id=stage.id if stage else None,
                        customer_name=customer_name, customer_phone=customer_phone,
                        rental_date=rental_date, expected_return_date=expected_return,
                        billing_type=billing, status='active',
                        payment_status='no_charge' if billing == 'no_charge' else 'unpaid',
                        is_void=False)
    db.session.add(rental)
    db.session.flush()
    total_qty = 0.0
    total_amt = 0.0
    for tool, qty in tool_qty:
        rate = 0.0 if billing == 'no_charge' else float(tool.rental_rate_per_day or 0)
        amount = qty * rate
        db.session.add(ToolRentalItem(rental_id=rental.id, tool_id=tool.id,
                                      qty_rented=qty, qty_returned=0.0, qty_pending=qty,
                                      rate=rate, amount=amount))
        total_qty += qty
        total_amt += amount
        origin = (f'{project.name}' + (f' > {stage.name}' if stage else '')) if project else customer_name
        create_movement_log(tool_id=tool.id, rental_id=rental.id, movement_type='rental_out',
                            from_label='Warehouse / Store', to_label=origin, qty=qty,
                            notes=f'Rented out on {code}')
    rental.total_rented_qty = total_qty
    rental.total_returned_qty = 0.0
    rental.total_amount = total_amt
    rental.total_paid = 0.0
    db.session.flush()
    return rental


def _transfer(rental, to_project, to_stage, qty_total, transfer_date, notes):
    existing = ToolRentalTransfer.query.filter_by(rental_id=rental.id).all()
    if any((t.to_project_id or 0) == to_project.id for t in existing):
        return None
    origin = (f'{rental.project.name}' + (f' > {rental.stage.name}' if rental.stage else '')
              if rental.project_id else (rental.customer_name or 'External'))
    to_label = to_project.name + (f' > {to_stage.name}' if to_stage else '')
    transfer = ToolRentalTransfer(
        rental_id=rental.id,
        from_type='site' if rental.project_id else 'customer',
        from_project_id=rental.project_id, from_stage_id=rental.stage_id,
        from_customer_name=None if rental.project_id else rental.customer_name,
        from_location_label=origin,
        to_type='site', to_project_id=to_project.id,
        to_stage_id=to_stage.id if to_stage else None,
        to_location_label=to_label, qty_transferred=qty_total,
        transfer_date=transfer_date, notes=notes)
    db.session.add(transfer)
    db.session.flush()
    pending = [(it, float(it.qty_pending or 0)) for it in rental.items
               if float(it.qty_pending or 0) > 0]
    allocations = allocate_transfer_qty(pending, qty_total)
    for item, take in allocations:
        create_movement_log(tool_id=item.tool_id, rental_id=rental.id,
                            movement_type='site_transfer', from_label=origin,
                            to_label=to_label, qty=take, transfer_id=transfer.id,
                            notes=f'Transfer {origin} > {to_label}')
    record_transfer_items(transfer, allocations)
    return transfer


def _return(rental, qty_by_code, return_date, notes):
    for item in rental.items:
        tool = db.session.get(Tool, int(item.tool_id))
        take = float(qty_by_code.get(tool.tool_code, 0))
        if take <= 0:
            continue
        take = min(take, float(item.qty_pending or 0))
        item.qty_returned = float(item.qty_returned or 0) + take
        item.qty_pending = max(0.0, float(item.qty_rented or 0) - float(item.qty_returned or 0))
        create_movement_log(tool_id=item.tool_id, rental_id=rental.id,
                            movement_type='return_in', from_label='Site',
                            to_label='Warehouse / Store', qty=take,
                            notes=notes)
    rental.total_returned_qty = sum(float(i.qty_returned or 0) for i in rental.items)
    db.session.flush()


def main():
    app = create_app()
    with app.app_context():
        if Tool.query.filter_by(tool_code=DEMO_CODE).first():
            print('tools demo already seeded')
            return

        today = _pkt_today()
        site_a = _get_or_create_project('DEMO-SITE-A', 'Gulshan Villa Block A', 'Mr. Ahmed', 'Karachi')
        site_b = _get_or_create_project('DEMO-SITE-B', 'DHA Phase 6 Tower', 'Mr. Khan', 'Karachi')
        site_c = _get_or_create_project('DEMO-SITE-C', 'Bahria Town Duplex', 'Mrs. Raza', 'Hyderabad')
        stage_a1 = _get_or_create_stage(site_a, 'Foundation')
        stage_a2 = _get_or_create_stage(site_a, 'Grey Structure')
        stage_b1 = _get_or_create_stage(site_b, 'Slab Work')
        db.session.commit()

        power = _get_or_create_category('Power Tools')
        shutter = _get_or_create_category('Shuttering & Scaffolding')
        hand = _get_or_create_category('Hand Tools')

        vib = _get_or_create_tool(DEMO_CODE, 'Concrete Vibrator', 24, 1200, 45000, power)
        cutter = _get_or_create_tool('TOOL-DEMO-CUT', 'Tile Cutting Machine', 12, 900, 28000, power)
        hammer = _get_or_create_tool('TOOL-DEMO-HMR', 'Jack Hammer', 8, 1500, 62000, power)
        welder = _get_or_create_tool('TOOL-DEMO-WLD', 'Welding Machine', 10, 1100, 38000, power)
        grinder = _get_or_create_tool('TOOL-DEMO-GRD', 'Angle Grinder', 30, 400, 9500, power)
        prop = _get_or_create_tool('TOOL-DEMO-PRP', 'Steel Shuttering Prop', 200, 60, 3200, shutter, unit='nos')
        plate = _get_or_create_tool('TOOL-DEMO-PLT', 'Shuttering Steel Plate', 150, 80, 5200, shutter, unit='nos')
        scaffold = _get_or_create_tool('TOOL-DEMO-SCF', 'Scaffolding Frame Set', 90, 150, 7800, shutter, unit='set')
        mixer = _get_or_create_tool('TOOL-DEMO-MIX', 'Concrete Mixer Drum', 4, 2500, 185000, power)
        theodolite = _get_or_create_tool('TOOL-DEMO-THE', 'Theodolite / Total Station', 3, 2000, 240000, hand)
        broken = _get_or_create_tool('TOOL-DEMO-BRK', 'Old Rebar Bender', 2, 0, 55000, power, condition='damaged')
        db.session.commit()

        # --- internal: own sites, some no-charge (included in contract) ---
        r1 = _create_rental('RENT-DEMO-001', [(prop, 120), (plate, 80), (vib, 6)],
                            'internal', today - timedelta(days=26), project=site_a,
                            stage=stage_a2, billing='no_charge',
                            expected_return=today + timedelta(days=10))
        r2 = _create_rental('RENT-DEMO-002', [(scaffold, 60), (grinder, 8), (cutter, 4)],
                            'internal', today - timedelta(days=18), project=site_b,
                            stage=stage_b1, billing='no_charge',
                            expected_return=today + timedelta(days=6))
        r3 = _create_rental('RENT-DEMO-003', [(hammer, 5), (welder, 4)],
                            'internal', today - timedelta(days=44), project=site_a,
                            stage=stage_a1, billing='no_charge')   # long out, no due date
        r4 = _create_rental('RENT-DEMO-004', [(mixer, 2), (theodolite, 1)],
                            'internal', today - timedelta(days=9), project=site_c,
                            billing='no_charge', expected_return=today + timedelta(days=20))
        db.session.commit()

        # site-to-site chain: props A > B (partial), scaffolding B stays
        if r1:
            _transfer(r1, site_b, stage_b1, 40, today - timedelta(days=7),
                      'Slab pour needed extra props')
        if r3:
            _transfer(r3, site_c, None, 2, today - timedelta(days=5),
                      'Jack hammers moved to Hyderabad')
        db.session.commit()

        # --- external: other customers, fee based ---
        c1 = _create_rental('RENT-DEMO-101', [(vib, 10), (grinder, 12)], 'external',
                            today - timedelta(days=12), customer_name='Ali Traders',
                            customer_phone='0300-2345678', billing='per_day',
                            expected_return=today + timedelta(days=3))
        c2 = _create_rental('RENT-DEMO-102', [(plate, 40), (prop, 50)], 'external',
                            today - timedelta(days=21), customer_name='Bilal Construction Co',
                            customer_phone='0321-7654321', billing='fixed_fee',
                            expected_return=today - timedelta(days=4))   # OVERDUE
        c3 = _create_rental('RENT-DEMO-103', [(cutter, 3), (welder, 2)], 'external',
                            today - timedelta(days=6), customer_name='Usman Steel Works',
                            customer_phone='0333-9988776', billing='per_day',
                            expected_return=today + timedelta(days=8))
        db.session.commit()

        # partial returns: some stock is back in store
        if c1:
            _return(c1, {'TOOL-DEMO-GRD': 5}, today - timedelta(days=3), 'Partial return — 5 grinders back')
        if r1:
            _return(r1, {'TOOL-DEMO-PLT': 20}, today - timedelta(days=2), 'De-shuttering done on 2nd floor')
        db.session.commit()

        # mark the overdue rental properly for the attention panel
        if c2:
            c2.status = 'overdue'
            db.session.commit()

        ledger = tool_ledger()
        recon = tools_reconciliation(ledger)
        t = ledger['totals']
        print('--- HDC Tools demo seeded ---')
        print(f"owned            : {t['owned_qty']:g}")
        print(f"in store         : {recon['in_store']:g}")
        print(f"sent own projects: {recon['own_project']:g}  ({t['own_project_count']} sites)")
        print(f"sent customers   : {recon['customer']:g}  ({t['customer_count']} customers)")
        print(f"total sent       : {recon['total_sent']:g}")
        print(f"balanced         : {recon['balanced']}")
        print(f"open rentals     : {t['open_rentals']}  overdue: {t['overdue_rentals']}")
        print('View it at /hdc/tool-rental/dashboard')


if __name__ == '__main__':
    main()
