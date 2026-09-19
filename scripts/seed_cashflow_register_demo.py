#!/usr/bin/env python3
"""Seed a throwaway database with realistic Cash Flow register data.

Exists so the register, the day-close page and the immutability rules can be
looked at on real-looking data without touching production.  Mirrors
``scripts/make_labour_audit_fixture.py`` in spirit: it writes to the database
named on the command line (or ``HDC_DB_PATH``), never to the default one
unless you say so explicitly.

Usage::

    HDC_DB_PATH=/tmp/cf_demo.db HDC_INSTANCE_DIR=/tmp/cf_demo_inst \\
        .venv/bin/python scripts/seed_cashflow_register_demo.py

    # then
    HDC_DB_PATH=/tmp/cf_demo.db HDC_INSTANCE_DIR=/tmp/cf_demo_inst \\
        .venv/bin/python hdc_erp.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')
os.environ.setdefault('HDC_SECRET_KEY', 'demo-only-secret')

from hdc.app import create_app                                    # noqa: E402
from hdc.extensions import db                                     # noqa: E402
from hdc.utils.dates import _pkt_today                            # noqa: E402

DB_PATH = os.environ.get('HDC_DB_PATH') or '/tmp/cf_demo.db'
INSTANCE = os.environ.get('HDC_INSTANCE_DIR') or '/tmp/cf_demo_inst'

if not os.environ.get('HDC_DB_PATH'):
    print(f'[seed] HDC_DB_PATH not set — using {DB_PATH}')

ACTOR = 'admin'


def main() -> int:
    app = create_app({'HDC_DB_PATH': DB_PATH, 'HDC_INSTANCE_DIR': INSTANCE})
    with app.app_context():
        from hdc.models.accounts import Account
        from hdc.services.accounts import _create_account
        from hdc.services.cashflow_register import (
            amend_manual_cash_flow_entry, category_options,
            lock_cash_day, save_counted_position, save_manual_cash_flow_entry,
            void_manual_cash_flow_entry,
        )

        def get_or_create(name, acc_type, opening=0.0, **kw):
            row = Account.query.filter_by(name=name).first()
            if row:
                return row
            row, _ = _create_account(name, acc_type, opening_balance=opening, **kw)
            db.session.commit()
            if not row:
                row = Account.query.filter_by(name=name).first()
            return row

        cash = get_or_create('Site Cash', 'cash', 150000.0)
        bank = get_or_create('Meezan Bank — Site A/c', 'bank', 480000.0,
                             bank_name='Meezan Bank', account_number='0201-0102-1234')
        _ = bank

        cats = {c.name: c for c in category_options()}
        sub_of = {}
        for c in cats.values():
            for s in c.subcategories:
                sub_of.setdefault(c.name, []).append(s)

        today = _pkt_today()
        posted = 0

        def at(day_offset, hour=10):
            return datetime.combine(today - timedelta(days=day_offset),
                                    datetime.min.time()).replace(hour=hour)

        plan = [
            # (days ago, direction, amount, account, category, subcategory, party, description)
            (6, 'in', '5,00,000.00', cash, 'Owner / Client Receipt', 'Project Payment', 'Mr. Akram (Client)', 'Gulberg villa — 2nd running bill'),
            (6, 'out', '1,85,000.00', cash, 'Material & Purchase', 'Cement', 'Al-Kabir Store', 'Cement 500 bags @ 370'),
            (5, 'out', '92,500.75', cash, 'Material & Purchase', 'Steel / Saria', 'Pak Steel Traders', 'Saria 60mm — 2.5 ton'),
            (5, 'transfer', '3,00,000.00', cash, None, None, '', 'Cash deposited to bank'),
            (4, 'out', '45,000.00', cash, 'Labour & Wages', 'Mason', 'Rafiq & team', 'Weekly masonry wages'),
            (4, 'in', '2,50,000.00', cash, 'Owner / Client Receipt', 'Advance Received', 'Mr. Akram (Client)', 'Advance against 3rd bill'),
            (3, 'out', '18,750.00', cash, 'Fuel & Transport', 'Diesel / Petrol', 'PSO Pump', 'Diesel for mixer + generator'),
            (3, 'out', '12,400.00', cash, 'Office Expense', 'Stationery', 'City Books', 'Site registers + files'),
            (2, 'out', '1,20,000.00', cash, 'Subcontractor Payment', None, 'Sajid Electric Works', 'Wiring — 2nd floor, 40%'),
            (2, 'in', '1,75,000.00', cash, 'Owner / Client Receipt', 'Project Payment', 'Mr. Akram (Client)', 'Part payment'),
            (1, 'out', '67,300.50', cash, 'Material & Purchase', 'Sand / Crush', 'Kohinoor Crush', 'Crush + sand, 6 trolleys'),
            (1, 'out', '8,900.00', cash, 'Miscellaneous', None, '', 'Chai / refreshment for labour'),
            (0, 'out', '24,600.00', cash, 'Equipment & Machinery', None, 'Zahid Machinery', 'Mixer rental — 1 week'),
            (0, 'in', '1,00,000.00', cash, 'Owner / Client Receipt', 'Project Payment', 'Mr. Akram (Client)', 'Running payment'),
        ]

        for days_ago, direction, amount, account, cat_name, sub_name, party, desc in plan:
            if direction == 'transfer':
                dest = Account.query.filter_by(name='Meezan Bank — Site A/c').first()
                save_manual_cash_flow_entry(
                    direction='transfer', amount=amount, account_id=account.id,
                    destination_account_id=(dest.id if dest else None),
                    description=desc, date_posted=at(days_ago), actor=ACTOR)
            else:
                sub = None
                if sub_name and cat_name in sub_of:
                    sub = next((s for s in sub_of[cat_name] if s.name == sub_name), None)
                save_manual_cash_flow_entry(
                    direction=direction, amount=amount, account_id=account.id,
                    category_id=(cats[cat_name].id if cat_name in cats else None),
                    subcategory_id=(sub.id if sub else None),
                    party_name=party or None, description=desc,
                    date_posted=at(days_ago), actor=ACTOR)
            posted += 1
        db.session.commit()

        # A realistic correction: the crush bill was entered 7,300.50 too high.
        from hdc.models.cashflow import CashFlowEntry
        wrong = (CashFlowEntry.query
                 .filter(CashFlowEntry.description.like('%Crush + sand%'))
                 .order_by(CashFlowEntry.id.desc()).first())
        if wrong is not None:
            amend_manual_cash_flow_entry(
                wrong, amount='60,000.00', reason='Trolley count was 6 not 7 — corrected from slip',
                actor=ACTOR)
            db.session.commit()

        # A cancelled entry: the machinery rental was paid from the bank instead.
        stray = (CashFlowEntry.query
                 .filter(CashFlowEntry.description.like('%Mixer rental%'))
                 .order_by(CashFlowEntry.id.desc()).first())
        if stray is not None and not stray.is_void:
            void_manual_cash_flow_entry(stray, reason='Paid from bank instead — duplicate entry',
                                        actor=ACTOR)
            db.session.commit()

        # Close yesterday with a genuine 500-rupee cash shortage.
        yesterday = today - timedelta(days=1)
        from hdc.services.cashflow_register import day_positions
        positions = day_positions(yesterday)
        db.session.commit()
        for p in positions:
            expected = float(p.expected_closing)
            # count the site cash 500 short, everything else exact
            counted = (expected - 500) if p.account_id == cash.id else expected
            save_counted_position(yesterday, p.account_id, counted, actor=ACTOR)
        db.session.commit()
        lock_cash_day(yesterday, actor=ACTOR, note='Day closed — site cash counted short by 500')
        db.session.commit()

        print(f'[seed] database: {DB_PATH}')
        print('[seed] accounts:  Site Cash, Meezan Bank — Site A/c')
        print(f'[seed] entries:   {posted} recorded (1 amended, 1 voided)')
        print(f'[seed] day close: {yesterday.isoformat()} locked with a -500.00 difference')
        print('[seed] log in with admin / Admin@1234 → Accounts → CF Register, Day Close')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
