#!/usr/bin/env python3
"""Attach already-posted project receipts to their projects.

Why this exists
---------------
The Accounts -> New Transaction form used to accept a project receipt where the
project was named but the counterparty was typed by hand.  Those entries posted
to the cash ledger correctly -- the cash balance was never wrong -- but nothing
was written to the project side, so the project still showed the money as
outstanding and the Receivable KPI ignored it.

The form no longer allows that: on the "Owner / Client Receipt" head the project
is mandatory and the owner is taken from the project.  This script applies the
same repair backwards, to the entries that were saved before the rule existed.

What it does
------------
For every register entry on a receipt category that carries a project and has no
mirrored payment row yet, it writes the matching ``OwnerPayment`` -- the same row
the form now writes as part of saving.  Nothing else is touched:

  * cash and bank balances are NOT changed (the ledger side was always right);
  * payments entered on the Projects page or the Accounts quick-post are left
    exactly as they are, so nothing is counted twice;
  * voided entries are mirrored as voided rows, not resurrected;
  * re-running is safe -- each entry is matched by its back-link and skipped the
    second time.

Usage
-----
Always look first.  ``--dry-run`` writes nothing::

    python3 scripts/repair_project_receipts.py --dry-run

Then apply::

    python3 scripts/repair_project_receipts.py --apply

Target a specific database explicitly (recommended on PythonAnywhere, where the
web app reads HDC_DB_PATH from the .env file in the checkout)::

    HDC_DB_PATH=/home/yourname/HDC-MAIN/hdc_instance/hdc_erp.db \
      python3 scripts/repair_project_receipts.py --apply

Run this with the same environment as the web app, otherwise you will edit a
different SQLite file than the one the site reads.  Take a copy of the database
file before ``--apply`` if you want a guaranteed way back.
"""

import argparse
import os
import sys

# Allow running as `python3 scripts/repair_project_receipts.py` from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from hdc.app import create_app                                     # noqa: E402
from hdc.config import get_runtime_settings                        # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.accounts import OwnerPayment                       # noqa: E402
from hdc.models.cashflow import CashFlowCategory, CashFlowEntry     # noqa: E402
from hdc.models.projects import Project                            # noqa: E402
from hdc.services.cashflow_register import (                       # noqa: E402
    backfill_project_receipt_owner_payments,
)


def _preview():
    """List the entries that would be repaired, newest project first."""
    receipt_ids = [int(c.id) for c in CashFlowCategory.query.all()
                   if (c.project_effect or '').strip().lower() == 'receipt']
    if not receipt_ids:
        print('categories   : no receipt category found — nothing to do')
        return []
    linked = {int(r.source_entry_id) for r in OwnerPayment.query
              .filter(OwnerPayment.source_entry_id.isnot(None)).all()}
    rows = (CashFlowEntry.query
            .filter(CashFlowEntry.category_id.in_(receipt_ids),
                    CashFlowEntry.project_id.isnot(None))
            .order_by(CashFlowEntry.project_id, CashFlowEntry.id)
            .all())
    return [r for r in rows if int(r.id) not in linked]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true',
                      help='Show what would be repaired and exit without writing.')
    mode.add_argument('--apply', action='store_true',
                      help='Write the missing project payment rows.')
    args = parser.parse_args()

    app = create_app()
    settings = get_runtime_settings()
    with app.app_context():
        print(f"db_path      : {settings.db_path}")
        pending = _preview()
        if not pending:
            print('pending      : none — every project receipt is already attached')
            return 0

        print(f"pending      : {len(pending)} entr{'y' if len(pending) == 1 else 'ies'}")
        total = 0.0
        by_project = {}
        for entry in pending:
            total += float(entry.amount or 0.0)
            by_project.setdefault(int(entry.project_id), []).append(entry)
        for project_id, entries in by_project.items():
            project = db.session.get(Project, project_id)
            name = project.name if project else f'#{project_id}'
            amount = sum(float(e.amount or 0.0) for e in entries)
            voids = sum(1 for e in entries if e.is_void)
            suffix = f" ({voids} voided)" if voids else ''
            print(f"  - {name:<28} {len(entries):>3} entries  Rs {amount:>14,.2f}{suffix}")
        print(f"total        : Rs {total:,.2f} across {len(by_project)} project(s)")

        if args.dry_run:
            print('result       : dry run — nothing written')
            print('next         : re-run with --apply to attach these to their projects')
            return 0

        stats = backfill_project_receipt_owner_payments(commit=True)
        print(f"result       : {stats['created']} payment row(s) created, "
              f"{stats['skipped']} already linked")
        print('next         : reload the web app and check Projects → Received / Remaining')
        return 0


if __name__ == '__main__':
    sys.exit(main())
