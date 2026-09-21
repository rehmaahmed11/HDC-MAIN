"""HDC models.loans — money borrowed from, or lent to, a person.

The register (``hdc_cash_flow_entry``) records *money movements*.  A loan is the
**contract** those movements belong to: "Rs 5,00,000 taken from Mr. Akram on
1 Sep, repayable by 31 Dec".  One loan has many movements (the money in, the
part payments, the interest), and the outstanding balance is always derived from
them — never stored — for the same reason balances are never stored on
``Account``: a stored figure is a second source of truth that drifts away from
the ledger.

Vocabulary (kept deliberately small):

``Loan.direction``
    ``received``  we took the loan  — money came **in**, we owe the person
    ``given``     we gave the loan  — money went **out**, the person owes us

``LoanMovement.kind``
    ``disbursement``  principal handed over in either direction
    ``repayment``     principal/interest paid back on a loan we *took*
    ``recovery``      principal/interest received back on a loan we *gave*
    ``writeoff``      the remaining principal is written off (no money moves)
    ``adjustment``    a correction posted by hand

Outstanding principal (paisa)::

    sum(disbursement principal) - sum(repayment/recovery principal) - writeoffs

Interest is tracked beside the principal (``interest_amount``) and never reduces
the outstanding principal — paying interest settles nothing, which is exactly
how a lender's statement reads.

Every movement carries the register entry and/or ledger transaction that moved
the money, so a loan row can always be traced back to the double-entry posting
that produced it (and voids flow back the other way).
"""

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive

__all__ = [
    "LOAN_DIRECTIONS",
    "LOAN_DIRECTION_LABELS",
    "LOAN_MOVEMENT_KINDS",
    "LOAN_MOVEMENT_LABELS",
    "LOAN_STATUSES",
    "Loan",
    "LoanMovement",
]

#: ``received`` = we took the loan, ``given`` = we gave it.
LOAN_DIRECTIONS = ("received", "given")

LOAN_DIRECTION_LABELS = {
    "received": "Loan Taken (we owe)",
    "given": "Loan Given (they owe)",
}

LOAN_MOVEMENT_KINDS = ("disbursement", "repayment", "recovery", "writeoff", "adjustment")

LOAN_MOVEMENT_LABELS = {
    "disbursement": "Principal paid out",
    "repayment": "Repayment made",
    "recovery": "Recovery received",
    "writeoff": "Written off",
    "adjustment": "Adjustment",
}

LOAN_STATUSES = ("open", "closed", "void")


class Loan(db.Model):
    """One loan contract with one counterparty (a person, bank or supplier)."""

    __tablename__ = 'hdc_loan'

    id = db.Column(db.Integer, primary_key=True)
    loan_code = db.Column(db.String(30), index=True)
    direction = db.Column(db.String(10), nullable=False, index=True)   # received | given
    party_id = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_party.id'),
                         nullable=True, index=True)
    party_name = db.Column(db.String(160), nullable=False, index=True)
    party_type = db.Column(db.String(40), index=True)                  # lender | borrower | ...
    phone = db.Column(db.String(40))
    party_account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'),
                                 nullable=True, index=True)

    # The agreed principal.  ``*_minor`` (paisa) is authoritative for arithmetic;
    # the float column is the legacy/UI mirror (see hdc.utils.money).
    principal = db.Column(db.Float, default=0.0)
    principal_minor = db.Column(db.BigInteger, nullable=True, index=True)
    interest_rate = db.Column(db.Float, default=0.0)                   # % per year, 0 = interest free
    purpose = db.Column(db.String(200))

    start_date = db.Column(db.Date, index=True)
    due_date = db.Column(db.Date, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'),
                           nullable=True, index=True)                  # treasury account of the opening leg

    status = db.Column(db.String(10), default='open', index=True)       # open | closed | void
    closed_at = db.Column(db.DateTime, nullable=True)
    closed_by = db.Column(db.String(80))
    close_reason = db.Column(db.String(300))
    note = db.Column(db.String(500))

    created_by = db.Column(db.String(80), index=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive, index=True)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    party = db.relationship('CashFlowParty', foreign_keys=[party_id])
    account = db.relationship('Account', foreign_keys=[account_id])
    movements = db.relationship(
        'LoanMovement',
        foreign_keys='LoanMovement.loan_id',
        backref='loan',
        order_by='LoanMovement.date.asc(), LoanMovement.id.asc()',
    )

    # ── derived figures (never stored) ───────────────────────────────────────
    @property
    def is_received(self):
        """True when we took this loan (money came in)."""
        return (self.direction or '').strip().lower() == 'received'

    @property
    def direction_label(self):
        return LOAN_DIRECTION_LABELS.get((self.direction or '').strip().lower(),
                                         self.direction or '')

    def _principal_minor(self):
        if self.principal_minor is not None:
            return int(self.principal_minor)
        return int(round(float(self.principal or 0.0) * 100))

    @property
    def active_movements(self):
        return [m for m in (self.movements or []) if not m.is_void]

    @property
    def disbursed_minor(self):
        """Principal actually handed over so far."""
        return sum(m.principal_minor_value for m in self.active_movements
                   if m.kind == 'disbursement')

    @property
    def settled_minor(self):
        """Principal paid back / recovered so far."""
        return sum(m.principal_minor_value for m in self.active_movements
                   if m.kind in ('repayment', 'recovery'))

    @property
    def written_off_minor(self):
        return sum(m.principal_minor_value for m in self.active_movements
                   if m.kind == 'writeoff')

    @property
    def interest_minor(self):
        """Interest paid (received loans) or earned (given loans), to date."""
        return sum(m.interest_minor_value for m in self.active_movements
                   if m.kind in ('repayment', 'recovery'))

    @property
    def outstanding_minor(self):
        """Principal still owed — what we owe (received) or are owed (given)."""
        minor = self.disbursed_minor - self.settled_minor - self.written_off_minor
        return minor if minor > 0 else 0

    @property
    def settled_percent(self):
        disbursed = self.disbursed_minor
        if disbursed <= 0:
            return 100.0 if self.outstanding_minor <= 0 else 0.0
        closed_part = disbursed - self.outstanding_minor
        return max(0.0, min(100.0, round(closed_part * 100.0 / disbursed, 1)))

    def days_to_due(self, today=None):
        """Days until ``due_date`` (negative = overdue), or ``None`` if open-ended."""
        if not self.due_date:
            return None
        today = today or _pkt_now_naive().date()
        return (self.due_date - today).days

    def is_overdue(self, today=None):
        return bool(self.status == 'open' and self.outstanding_minor > 0
                    and (self.days_to_due(today) or 0) < 0)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Loan {self.id}:{self.direction}:{self.party_name!r}:{self.principal}>"


class LoanMovement(db.Model):
    """One money movement against a :class:`Loan` (always backed by a posting).

    ``amount`` is what moved; ``principal_amount`` / ``interest_amount`` split
    that same amount between principal and interest (they add up to ``amount``
    for repayments and recoveries, and ``principal_amount == amount`` for a
    disbursement).
    """

    __tablename__ = 'hdc_loan_movement'

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('hdc_loan.id'), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False, index=True)     # see LOAN_MOVEMENT_KINDS
    date = db.Column(db.Date, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'),
                           nullable=True, index=True)

    amount = db.Column(db.Float, default=0.0)
    amount_minor = db.Column(db.BigInteger, nullable=True, index=True)
    principal_amount = db.Column(db.Float, default=0.0)
    principal_amount_minor = db.Column(db.BigInteger, nullable=True)
    interest_amount = db.Column(db.Float, default=0.0)
    interest_amount_minor = db.Column(db.BigInteger, nullable=True)

    # Traceability: the register document and the double-entry posting behind
    # this movement.  Either may be absent (writeoffs move no money).
    cash_flow_entry_id = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_entry.id'),
                                   nullable=True, index=True)
    account_txn_id = db.Column(db.Integer, db.ForeignKey('hdc_account_txn.id'),
                               nullable=True, index=True)
    source_type = db.Column(db.String(40), index=True)   # cash_flow_register | accounts_entries | manual
    reference = db.Column(db.String(80), index=True)
    note = db.Column(db.String(500))

    is_void = db.Column(db.Boolean, default=False, index=True)
    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by = db.Column(db.String(80))
    void_reason = db.Column(db.String(300))

    created_by = db.Column(db.String(80), index=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive, index=True)

    account = db.relationship('Account', foreign_keys=[account_id])
    cash_flow_entry = db.relationship('CashFlowEntry', foreign_keys=[cash_flow_entry_id])
    account_txn = db.relationship('AccountTransaction', foreign_keys=[account_txn_id])

    @property
    def amount_minor_value(self):
        if self.amount_minor is not None:
            return int(self.amount_minor)
        return int(round(float(self.amount or 0.0) * 100))

    @property
    def principal_minor_value(self):
        if self.principal_amount_minor is not None:
            return int(self.principal_amount_minor)
        if self.principal_amount:
            return int(round(float(self.principal_amount or 0.0) * 100))
        return self.amount_minor_value

    @property
    def interest_minor_value(self):
        if self.interest_amount_minor is not None:
            return int(self.interest_amount_minor)
        return int(round(float(self.interest_amount or 0.0) * 100))

    @property
    def kind_label(self):
        return LOAN_MOVEMENT_LABELS.get((self.kind or '').strip().lower(), self.kind or '')

    @property
    def cash_direction(self):
        """``in`` / ``out`` / ``''`` — which way the money actually moved.

        A disbursement follows the loan's own direction (we took it: in; we gave
        it: out); a repayment is always money out and a recovery always money in.
        Write-offs and adjustments move no money at all.
        """
        kind = (self.kind or '').strip().lower()
        received = bool(self.loan.is_received) if self.loan else True
        if kind == 'disbursement':
            return 'in' if received else 'out'
        if kind == 'repayment':
            return 'out'
        if kind == 'recovery':
            return 'in'
        return ''

    @property
    def money_in(self):
        """True when this movement brought money into the treasury."""
        return self.cash_direction == 'in'

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<LoanMovement {self.id}:{self.kind}:{self.amount}>"
