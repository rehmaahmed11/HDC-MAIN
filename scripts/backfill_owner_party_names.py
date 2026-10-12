#!/usr/bin/env python3
"""Re-file owner-linked ledger rows under the owner's name.

Why this exists
---------------
Owner receipts (Projects page, CF Register receipts) and own-site HDC Tools
receipts/discounts were posted with the *project name* as ``party_name``.  The
owner's Parties statement matches ``party_name`` to the owner, so those rows
were missing from it even though the owner's account balance included them.
New postings now use the owner's name.  This script repairs the rows posted
before that change.

What it changes
---------------
Only ``hdc_account_txn.party_name`` on rows that are:

  * ``project_income`` rows (owner receipts) tied to a project whose ``client``
    is filled in, or
  * tool-rental rows (payments/discounts) of *internal* rentals whose project
    has a ``client``,

and whose ``party_name`` currently equals the project name.  Amounts, accounts,
dates, types and voids are never touched.  Voided rows are included so a later
restore shows the right owner.

Usage
-----
Dry run is the default and writes nothing::

    python3 scripts/backfill_owner_party_names.py

Apply::

    python3 scripts/backfill_owner_party_names.py --apply

Run with the same environment as the web app (HDC_DB_PATH).  Take a copy of the
database file first if you want a guaranteed way back.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from hdc.app import create_app                                     # noqa: E402
from hdc.config import get_runtime_settings                        # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.accounts import AccountTransaction                 # noqa: E402
from hdc.models.projects import Project                            # noqa: E402
from hdc.models.tool_rental import ToolRental                      # noqa: E402
from hdc.services.tool_rental import rental_party_name             # noqa: E402
from hdc.utils.normalize import _normalize_name_ci                 # noqa: E402


def _planned_changes():
    """Yield ``(txn, new_party_name)`` for every row that should be re-filed."""
    rows = AccountTransaction.query.filter(
        AccountTransaction.project_id.isnot(None)).all()
    for txn in rows:
        project = db.session.get(Project, int(txn.project_id))
        if project is None:
            continue
        owner = _normalize_name_ci(project.client or '')
        if not owner:
            continue
        current = (txn.party_name or '').strip()
        if txn.type == 'project_income':
            expected = owner
        elif (txn.related_entity_type == 'tool_rental'
              and txn.related_entity_id):
            rental = db.session.get(ToolRental, int(txn.related_entity_id))
            if rental is None or rental.renter_type != 'internal':
                continue
            expected = rental_party_name(rental)
        else:
            continue
        if current.lower() == expected.lower() or current.lower() != project.name.lower():
            continue
        yield txn, expected


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true',
                        help='Write the new party names (default: dry run).')
    args = parser.parse_args()

    app = create_app()
    settings = get_runtime_settings()
    with app.app_context():
        print(f"db_path : {settings.db_path}")
        changes = list(_planned_changes())
        print(f"rows to re-file: {len(changes)}")
        for txn, expected in changes[:50]:
            print(f"  txn #{txn.id:<6} {txn.type:<16} Rs {float(txn.amount or 0):>12,.2f}  "
                  f"{txn.party_name!r} -> {expected!r}{'  (void)' if txn.is_void else ''}")
        if len(changes) > 50:
            print(f"  ... and {len(changes) - 50} more")
        if not args.apply:
            print('result  : dry run -- nothing written. Re-run with --apply.')
            return 0
        for txn, expected in changes:
            txn.party_name = expected[:120]
        db.session.commit()
        print(f"result  : {len(changes)} row(s) re-filed.")
        return 0


if __name__ == '__main__':
    sys.exit(main())
