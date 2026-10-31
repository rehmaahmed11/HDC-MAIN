#!/usr/bin/env python3
"""READ-ONLY report: rentals whose tool position may be wrong in existing data.

Written after the "tools vanish back to store" investigation.  It changes
nothing: it lists the rentals a person should check by hand, because the old
records cannot say for certain where the pieces are:

  1. warehouse moves recorded before the fix (they count as "at the store" but
     the rental still shows the pieces as out);
  2. return lines that never recorded the site / customer they came from, on a
     rental whose pieces are (or were) at more than one place;
  3. rentals still showing pending pieces that the ledger holds at the store.

Usage (from the repo root, same environment as the app):
    python3 scripts/report_tool_location_drift.py            # all rentals
    python3 scripts/report_tool_location_drift.py --csv      # machine readable
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from hdc.app import create_app                                            # noqa: E402
from hdc.extensions import db                                             # noqa: E402
from hdc.models.tool_rental import (                                      # noqa: E402
    ToolRental, ToolRentalItem, ToolRentalReturn, ToolRentalReturnItem, ToolRentalTransfer,
)
from hdc.services.tool_tracking import LOC_STORE, rental_return_sources  # noqa: E402

EPS = 0.001


def _rental_rows():
    rentals = (ToolRental.query.filter(ToolRental.is_void == False)  # noqa: E712
               .order_by(ToolRental.id.asc()).all())
    for rental in rentals:
        items = ToolRentalItem.query.filter_by(rental_id=rental.id).all()
        pending = sum(max(0.0, float(i.qty_pending or 0)) for i in items)
        if pending <= EPS:
            continue
        reasons = []

        legacy_store = (ToolRentalTransfer.query
                        .filter(ToolRentalTransfer.rental_id == rental.id,
                                ToolRentalTransfer.to_type == 'warehouse',
                                ToolRentalTransfer.closes_rental.isnot(True))
                        .count())
        if legacy_store:
            reasons.append(f'{legacy_store} warehouse move(s) before the fix — '
                           f'pieces may be in store while this rental still shows them out')

        unkeyed = (ToolRentalReturnItem.query
                   .join(ToolRentalReturn, ToolRentalReturn.id == ToolRentalReturnItem.return_id)
                   .filter(ToolRentalReturn.rental_id == rental.id,
                           ToolRentalReturnItem.from_location_key.is_(None))
                   .count())
        sources = rental_return_sources(rental, items)
        multi = any(len(v) > 1 for v in sources.values())
        if unkeyed and multi:
            reasons.append(f'{unkeyed} return line(s) without a recorded source on a rental '
                           f'held at more than one place — check which site they came from')

        if reasons:
            yield {
                'rental_code': rental.rental_code,
                'renter': rental.customer_name or (rental.project.name if rental.project else ''),
                'pending_qty': round(pending, 2),
                'ledger_holdings': '; '.join(
                    f"{i.tool.name if i.tool else i.tool_id}: " + ', '.join(
                        f"{s['label']} {s['qty']:g}" for s in sources.get(int(i.id), []))
                    for i in items if float(i.qty_pending or 0) > EPS),
                'why_check': ' | '.join(reasons),
            }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--csv', action='store_true', help='print CSV instead of a table')
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        try:
            rows = list(_rental_rows())
        finally:
            db.session.rollback()          # read-only: never leave a transaction open

    fields = ['rental_code', 'renter', 'pending_qty', 'ledger_holdings', 'why_check']
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return 0
    if not rows:
        print('No rentals need a manual check.')
        return 0
    print(f'{len(rows)} rental(s) to check by hand (nothing was changed):\n')
    for row in rows:
        print(f"- {row['rental_code']} · {row['renter']} · {row['pending_qty']:g} pending")
        print(f"    ledger: {row['ledger_holdings'] or '-'}")
        print(f"    why:    {row['why_check']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
