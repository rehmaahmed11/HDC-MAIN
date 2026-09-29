"""HDC models.shared_expenses — one cost, split across several sharing heads.

FBM, HDC, Home (and whoever else) regularly pay for something together: car
fuel, a workshop bill, a utility that serves all three.  The money leaves *one*
account, but the cost belongs to *several* heads, and "who has borne how much"
has to be answerable.

This module owns the **allocation** side of that, and only that:

``SharedParty``
    A sharing head / ledger — FBM, HDC, Home, a person, a site.  Optionally
    linked to one ``hdc_account`` row so settling up between heads is a real
    transfer in the accounts ledger rather than a note.
``SharedExpense``
    One bill: date, title ("Car Fuel"), total, who paid, how it was split, and
    the link to the accounts entry that actually moved the money.
``SharedExpenseShare``
    One party's slice of one bill, stored in exact minor units (paisa) so the
    slices always re-add to the total.
``SharedSettlement``
    One head paying another head (or an account) to square up.

**Money never lives here.**  Every rupee that moves is a row in
``hdc_account_txn`` created by the accounts engine (``services.cashflow_register``
/ ``services.accounts``).  A ``SharedExpense`` *links* to such a row — either one
recorded earlier in Accounts, or one this module posts through the very same
engine — so the Accounts section stays the single ledger and nothing is counted
twice: the ledger holds the full payment once, and the shares here explain who
that payment was for.
"""

from sqlalchemy import UniqueConstraint, event

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.money import sync_money_fields

#: kinds of sharing head — presentation only, the name is what identifies it
SHARED_PARTY_KINDS = ('business', 'home', 'person', 'other')

#: how one bill is divided.  ``equal`` = even split, ``custom`` = a rupee figure
#: per party, ``percent`` = a share of 100% per party.
SHARED_SPLIT_MODES = ('equal', 'custom', 'percent')


class SharedParty(db.Model):
    """One sharing head — a ledger in the Shared Expenses module.

    Balances are never stored: they are derived from the shares, the payments
    and the settlements on every read, the same rule the accounts ledger
    follows.  ``account_id`` is optional and only used to pre-fill the two sides
    of a settlement transfer.
    """

    __tablename__ = 'hdc_shared_party'

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(80), nullable=False, unique=True)
    short_code  = db.Column(db.String(20))
    kind        = db.Column(db.String(20), default='business')   # see SHARED_PARTY_KINDS
    account_id  = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=True)
    #: pre-ticked on a new shared expense's party list
    is_default  = db.Column(db.Boolean, default=True)
    sort_order  = db.Column(db.Integer, default=0)
    status      = db.Column(db.String(20), default='active')     # active / inactive
    note        = db.Column(db.String(250))
    created_by  = db.Column(db.String(80))
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at  = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    account = db.relationship('Account', foreign_keys=[account_id])

    @property
    def is_active(self):
        return (self.status or 'active').strip().lower() == 'active'

    @property
    def label(self):
        """``HDC (HD)`` when a short code is set, otherwise just the name."""
        code = (self.short_code or '').strip()
        return f'{self.name} ({code})' if code else (self.name or '')

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'<SharedParty {self.id} {self.name}>'


def _sync_shared_minor(_mapper, _connection, target):
    """Keep every ``amount`` / ``amount_minor`` pair in step, on any write.

    Same defensive hook the unified ledger uses: a bad value must never turn a
    posting into a 500, so the sync is best-effort and the float column stays
    the UI surface while the integer column is authoritative for arithmetic.
    """
    try:
        sync_money_fields(target, 'amount', 'amount_minor')
    except Exception:
        pass


def _sync_shared_total_minor(_mapper, _connection, target):
    try:
        sync_money_fields(target, 'total_amount', 'total_amount_minor')
    except Exception:
        pass


class SharedExpense(db.Model):
    """One bill whose cost is divided between several sharing heads.

    ``total_amount_minor`` is the authoritative figure; the sum of the shares'
    own ``amount_minor`` always equals it exactly (the split engine hands the
    odd paisa to the first participants rather than letting the rounding drift).

    The money side is one of:

    * ``cf_entry_id`` / ``txn_id`` set — linked to a real accounts entry,
      either picked from Accounts or posted by this module through the accounts
      engine (``source_type='shared_expense'``);
    * neither set — an allocation recorded ahead of the accounts entry.  It is
      shown everywhere with a *Not in Accounts* warning rather than being
      silently treated as paid.
    """

    __tablename__ = 'hdc_shared_expense'

    id                 = db.Column(db.Integer, primary_key=True)
    date               = db.Column(db.Date, default=_pkt_today, index=True)
    title              = db.Column(db.String(160), nullable=False)
    category           = db.Column(db.String(80), index=True)
    total_amount       = db.Column(db.Float, default=0.0)
    total_amount_minor = db.Column(db.BigInteger, nullable=True)
    #: who bore the cost.  NULL + an account means "paid from that account and
    #: not yet credited to any head" — the balances view says so explicitly.
    payer_party_id     = db.Column(db.Integer, db.ForeignKey('hdc_shared_party.id'), nullable=True, index=True)
    paid_from_account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=True)
    #: the accounts document + its ledger row, when there is one
    cf_entry_id        = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_entry.id'), nullable=True, index=True)
    txn_id             = db.Column(db.Integer, db.ForeignKey('hdc_account_txn.id'), nullable=True, index=True)
    split_mode         = db.Column(db.String(12), default='equal')   # see SHARED_SPLIT_MODES
    reference          = db.Column(db.String(120))
    #: double-submit guard: a retried "save" form cannot create two expenses
    idempotency_key    = db.Column(db.String(64), nullable=True, index=True)
    note               = db.Column(db.String(400))
    is_void            = db.Column(db.Boolean, default=False, index=True)
    void_reason        = db.Column(db.String(300))
    voided_by          = db.Column(db.String(80))
    voided_at          = db.Column(db.DateTime, nullable=True)
    created_by         = db.Column(db.String(80))
    created_at         = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at         = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    payer_party       = db.relationship('SharedParty', foreign_keys=[payer_party_id])
    paid_from_account = db.relationship('Account', foreign_keys=[paid_from_account_id])
    cash_flow_entry   = db.relationship('CashFlowEntry', foreign_keys=[cf_entry_id])
    ledger_txn        = db.relationship('AccountTransaction', foreign_keys=[txn_id])
    shares            = db.relationship(
        'SharedExpenseShare', backref='expense', cascade='all, delete-orphan',
        order_by='SharedExpenseShare.position, SharedExpenseShare.id',
        foreign_keys='SharedExpenseShare.expense_id')

    @property
    def total_minor(self):
        """Authoritative total in paisa (falls back to the float column)."""
        if self.total_amount_minor is not None:
            return int(self.total_amount_minor)
        try:
            from hdc.utils.money import to_minor
            return to_minor(self.total_amount or 0)
        except Exception:
            return 0

    @property
    def is_linked(self):
        return bool(self.txn_id or self.cf_entry_id)

    @property
    def party_count(self):
        return len(self.shares or [])

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'<SharedExpense {self.id} {self.title} {self.total_amount}>'


class SharedExpenseShare(db.Model):
    """One head's slice of one shared expense (exact paisa in ``amount_minor``)."""

    __tablename__ = 'hdc_shared_share'
    __table_args__ = (
        UniqueConstraint('expense_id', 'party_id', name='uq_shared_share_expense_party'),
    )

    id          = db.Column(db.Integer, primary_key=True)
    expense_id  = db.Column(db.Integer, db.ForeignKey('hdc_shared_expense.id'), nullable=False, index=True)
    party_id    = db.Column(db.Integer, db.ForeignKey('hdc_shared_party.id'), nullable=False, index=True)
    amount      = db.Column(db.Float, default=0.0)
    amount_minor = db.Column(db.BigInteger, nullable=True)
    #: share of 100%, in basis points (3333 = 33.33%) — kept for the percent split
    percent_bp  = db.Column(db.Integer, nullable=True)
    position    = db.Column(db.Integer, default=0)
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)

    party = db.relationship('SharedParty', foreign_keys=[party_id])

    @property
    def minor(self):
        if self.amount_minor is not None:
            return int(self.amount_minor)
        try:
            from hdc.utils.money import to_minor
            return to_minor(self.amount or 0)
        except Exception:
            return 0

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'<SharedShare exp{self.expense_id} party{self.party_id} {self.amount}>'


class SharedSettlement(db.Model):
    """One head squaring up with another head (or with an account).

    Exactly one of ``to_party_id`` / ``to_account_id`` is set.  A settlement may
    carry the accounts transfer that moved the money (``txn_id``); when it does
    not, the module says so instead of pretending the cash moved.
    """

    __tablename__ = 'hdc_shared_settlement'

    id            = db.Column(db.Integer, primary_key=True)
    date          = db.Column(db.Date, default=_pkt_today, index=True)
    from_party_id = db.Column(db.Integer, db.ForeignKey('hdc_shared_party.id'), nullable=False, index=True)
    to_party_id   = db.Column(db.Integer, db.ForeignKey('hdc_shared_party.id'), nullable=True, index=True)
    to_account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=True)
    amount        = db.Column(db.Float, default=0.0)
    amount_minor  = db.Column(db.BigInteger, nullable=True)
    note          = db.Column(db.String(300))
    cf_entry_id   = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_entry.id'), nullable=True, index=True)
    txn_id        = db.Column(db.Integer, db.ForeignKey('hdc_account_txn.id'), nullable=True, index=True)
    is_void       = db.Column(db.Boolean, default=False, index=True)
    void_reason   = db.Column(db.String(300))
    voided_by     = db.Column(db.String(80))
    voided_at     = db.Column(db.DateTime, nullable=True)
    created_by    = db.Column(db.String(80))
    created_at    = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at    = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    from_party = db.relationship('SharedParty', foreign_keys=[from_party_id])
    to_party   = db.relationship('SharedParty', foreign_keys=[to_party_id])
    to_account = db.relationship('Account', foreign_keys=[to_account_id])
    cash_flow_entry = db.relationship('CashFlowEntry', foreign_keys=[cf_entry_id])
    ledger_txn      = db.relationship('AccountTransaction', foreign_keys=[txn_id])

    @property
    def minor(self):
        if self.amount_minor is not None:
            return int(self.amount_minor)
        try:
            from hdc.utils.money import to_minor
            return to_minor(self.amount or 0)
        except Exception:
            return 0

    @property
    def is_linked(self):
        return bool(self.txn_id or self.cf_entry_id)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'<SharedSettlement {self.id} {self.from_party_id}->{self.to_party_id or self.to_account_id}>'


event.listen(SharedExpense, 'before_insert', _sync_shared_total_minor)
event.listen(SharedExpense, 'before_update', _sync_shared_total_minor)
event.listen(SharedExpenseShare, 'before_insert', _sync_shared_minor)
event.listen(SharedExpenseShare, 'before_update', _sync_shared_minor)
event.listen(SharedSettlement, 'before_insert', _sync_shared_minor)
event.listen(SharedSettlement, 'before_update', _sync_shared_minor)
