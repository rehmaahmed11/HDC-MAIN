#!/usr/bin/env python3
"""Seed a little realistic demo data into the running DB for preview purposes.

Idempotent: skips work if demo accounts already exist.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_INSTANCE_DIR', '/home/user/HDC-MAIN/hdc_instance')

from datetime import date, timedelta

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.projects import Project
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.services.accounts import _create_account, _create_accounts_transaction_with_sync


def main():
    app = create_app()
    with app.app_context():
        if Account.query.filter_by(name='Demo Cash Box').first():
            print('already seeded')
            return

        cash, _ = _create_account('Demo Cash Box', 'cash', opening_balance=50000.0)
        bank, _ = _create_account('Demo Meezan Bank', 'bank', bank_name='Meezan Bank',
                                  account_number='PK0000123456')
        db.session.commit()

        project = Project(project_code='DEMO-001', name='Demo Villa Karachi',
                          client='Demo Owner', created_at=_pkt_now_naive())
        db.session.add(project)
        db.session.commit()

        # A small party / client account to receive from.
        client_acc = Account.query.filter_by(name='Demo Owner').first()
        if not client_acc:
            client_acc, _ = _create_account('Demo Owner', 'client')
            db.session.commit()
        person_acc, _ = _create_account('Demo Tea Supplier', 'person')
        db.session.commit()

        today = _pkt_today()
        entries = []
        # 12 days of demo flow
        for i in range(12, 0, -1):
            d = today - timedelta(days=i)
            entries.append({
                'date': d.isoformat(),
                'type': 'project_income',
                'amount': 25000 + i * 1500,
                'from_account_id': client_acc.id,
                'to_account_id': cash.id if i % 3 else bank.id,
                'executed_by_account_id': client_acc.id,
                'project_id': project.id,
                'related_entity_type': 'project',
                'related_entity_id': project.id,
                'category': 'income',
                'note': f'Owner payment day {13 - i}',
                'reference_id': f'demo-inc-{i}',
            })
            if i % 2 == 0:
                entries.append({
                    'date': d.isoformat(),
                    'type': 'party_payment',
                    'amount': 4000 + i * 250,
                    'from_account_id': cash.id,
                    'to_account_id': person_acc.id,
                    'executed_by_account_id': cash.id,
                    'party_name': 'Demo Tea Supplier',
                    'category': 'expense',
                    'note': f'Office tea & snacks {13 - i}',
                    'reference_id': f'demo-exp-{i}',
                })
            if i % 4 == 0:
                entries.append({
                    'date': d.isoformat(),
                    'type': 'transfer',
                    'amount': 8000,
                    'from_account_id': cash.id,
                    'to_account_id': bank.id,
                    'executed_by_account_id': cash.id,
                    'category': 'transfer',
                    'note': 'Deposit cash into bank',
                    'reference_id': f'demo-xfer-{i}',
                })

        for e in entries:
            ok, msg, _rows = _create_accounts_transaction_with_sync(e)
            if not ok:
                print('skip', e.get('note'), msg)
        print(f'seeded {len(entries)} entries')
        db.session.commit()


if __name__ == '__main__':
    main()
