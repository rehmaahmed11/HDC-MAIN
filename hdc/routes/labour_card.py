"""HDC route: Labour Card — one worker, one date range, one payable.

Read-only page (GET, so a card URL can be bookmarked and printed):
pick a worker from the searchable combo, pick the dates, and the card
below shows the site-wise days / overtime / amount, the advances and
payments in between, the final Payable, the sites' Fuel & Food spend
(information only, never in the worker's totals) and the last 4 weeks
week-by-week.  All arithmetic lives in ``services.labour_card``; this
handler only parses the filters and renders.
"""

from datetime import timedelta

from flask import flash, render_template, request
from flask_login import login_required

from hdc.extensions import db
from hdc.models.workforce import Worker
from hdc.services.labour_card import labour_card
from hdc.services.timekeeping import (
    _reconcile_worker_time_entries, _reconcile_worker_tip_ledger,
    _repair_worker_work_ledger_links,
)
from hdc.utils.dates import _pkt_today
from hdc.utils.format import _parse_date


def register(app):
    """Register the Labour Card page."""

    @app.route('/hdc/labour-card')
    @login_required
    def hdc_labour_card():
        workers = Worker.query.order_by(Worker.name).all()
        worker_options = []
        for entry in workers:
            label = f'{entry.name} ({entry.worker_code})'
            if entry.role_type:
                label += f' — {entry.role_type}'
            if not entry.active_status:
                label += ' (inactive)'
            worker_options.append((entry.id, label))

        today = _pkt_today()
        submitted = any(key in request.args
                        for key in ('worker_id', 'date_from', 'date_to'))
        if submitted:
            raw_from = (request.args.get('date_from') or '').strip()
            raw_to = (request.args.get('date_to') or '').strip()
            date_from = _parse_date(raw_from) if raw_from else None
            date_to = _parse_date(raw_to) if raw_to else None
        else:
            # First visit: default to the current week (Monday → today),
            # which is the card an operator opens at payment time.
            date_from = today - timedelta(days=today.weekday())
            date_to = today
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from

        worker_id = request.args.get('worker_id', type=int) or None
        worker = None
        card = None
        if worker_id:
            worker = Worker.query.get(worker_id)
            if worker is None:
                flash('Selected worker was not found. Please pick a worker '
                      'from the list.', 'warning')
                worker_id = None
            else:
                # Same read-time repair the Ledger/Statement pages run, so
                # every screen reconciles split overtime entries and tip
                # mirrors the same way before showing a figure.
                _reconcile_worker_time_entries(worker.id)
                _repair_worker_work_ledger_links(worker.id)
                _reconcile_worker_tip_ledger(worker)
                db.session.commit()
                card = labour_card(worker.id, date_from=date_from,
                                   date_to=date_to)

        return render_template(
            'workers/labour_card.html',
            worker_options=worker_options,
            selected_worker_id=worker_id,
            w=worker,
            card=card,
            date_from=(date_from.isoformat() if date_from else ''),
            date_to=(date_to.isoformat() if date_to else ''),
            today=today.isoformat(),
        )
