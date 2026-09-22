"""HDC models.cashflow — the cash flow register, reconciliation and day close.

Ported from the AMS *accounts* model (``models/cash.py`` +
``blueprints/accounts/``) and adapted to HDC's conventions:

* every table is prefixed ``hdc_`` like the rest of the schema;
* timestamps are PKT-naive (``_pkt_now_naive``);
* entries keep HDC's project / stage scoping, which AMS does not have;
* **balances are not stored here.**  AMS mutates ``Account.balance`` on every
  post/void; HDC keeps deriving balances from the ledger, so the register only
  ever writes double-entry rows into ``hdc_account_txn`` and reads balances
  back from it.  See ``CASHFLOW_MODEL.md`` for the reasoning.

The register is deliberately a *layer on top of* the unified ledger rather
than a second source of truth: a ``CashFlowEntry`` is the user-facing
document (category, subcategory, party, reference, who and why) and its
``account_tx_id`` is the financial posting that actually moves money.

Lifecycle (immutable / void-only)::

    Created  -> active, posts one AccountTransaction
    Amended  -> old entry voided + reversing txn, NEW entry + txn posted
    Voided   -> entry + txn voided, audit row written with the reason
    Restored -> entry + txn un-voided (only when the day is not locked)
"""

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive

__all__ = [
    "AccountReconciliation",
    "CashDayAccountPosition",
    "CashDayLock",
    "CashFlowCategory",
    "CashFlowEntry",
    "CashFlowEntryAudit",
    "CashFlowParty",
    "CashFlowSubcategory",
]


class CashFlowCategory(db.Model):
    """User-managed cash-flow head (e.g. *Material*, *Wages*, *Fuel*).

    Categories are configuration, not code: names live in the database so the
    business can add heads without a deploy.  ``direction`` restricts where a
    category may be used (``in`` = money received, ``out`` = money spent,
    ``both`` = allowed on either side).

    A category also carries the **field rules** for its entries, so the entry
    form can ask only for what this kind of transaction actually needs (and the
    engine can enforce it):

    ``party_mode`` / ``project_mode``
        ``none``     — the field is not shown at all
        ``optional`` — shown, may be left empty (default)
        ``required`` — shown, and the entry is rejected without it
    ``party_types``
        Comma-separated allowed ``CashFlowParty.party_type`` values (empty =
        any).  A loan category, for example, allows ``lender,borrower``.
    ``loan_effect``
        Empty for an ordinary category.  ``take`` / ``give`` / ``repay`` /
        ``recover`` mark the four loan movements, so an entry booked on this
        category is mirrored into the loan ledger (``hdc_loan``).
    ``project_effect``
        Empty for an ordinary category.  ``receipt`` marks a category as *money
        received from a project's owner/client*, which makes the project — not
        the typed party name — the subject of the entry:

        * the project becomes mandatory (it is the whole point of the entry);
        * the owner/client name is **derived from** ``Project.client`` instead
          of being retyped, so one client can never fork into four spellings;
        * the entry is mirrored into ``hdc_owner_payment``, which is what
          ``Project.total_received`` / ``remaining_receivable`` are computed
          from — without this the money lands in the ledger but the project
          still reads as fully unpaid.

        This is the project-side twin of ``loan_effect``: same hook, same
        "the document and its side-effect are written in one transaction" rule.
    """

    __tablename__ = 'hdc_cash_flow_category'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    direction = db.Column(db.String(10), default='both', index=True)      # in | out | both
    is_active = db.Column(db.Boolean, default=True, index=True)
    sort_order = db.Column(db.Integer, default=0)
    notes = db.Column(db.String(300))
    # NULL means "not configured" and reads as ``optional`` — it has to be
    # distinguishable from an explicit choice so the shipped defaults can be
    # filled in once without ever overwriting a rule the operator set.
    party_mode = db.Column(db.String(10))                                 # none | optional | required
    project_mode = db.Column(db.String(10))                               # none | optional | required
    party_types = db.Column(db.String(200))                               # CSV of allowed party types
    loan_effect = db.Column(db.String(12), index=True)                    # take | give | repay | recover
    project_effect = db.Column(db.String(12), index=True)                 # receipt
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    #: the only accepted values for the two ``*_mode`` columns
    FIELD_MODES = ('none', 'optional', 'required')

    @property
    def party_mode_value(self):
        value = (self.party_mode or 'optional').strip().lower()
        return value if value in self.FIELD_MODES else 'optional'

    @property
    def project_mode_value(self):
        value = (self.project_mode or 'optional').strip().lower()
        return value if value in self.FIELD_MODES else 'optional'

    @property
    def allowed_party_types(self):
        """Allowed ``party_type`` values as a tuple (empty = any)."""
        raw = (self.party_types or '').strip()
        if not raw:
            return ()
        out = []
        for chunk in raw.replace(';', ',').split(','):
            item = chunk.strip().lower()
            if item and item not in out:
                out.append(item)
        return tuple(out)

    @property
    def is_loan(self):
        return bool((self.loan_effect or '').strip())

    @property
    def project_effect_value(self):
        """``receipt`` or ``''`` — normalised, so callers never re-parse it."""
        value = (self.project_effect or '').strip().lower()
        return value if value in ('receipt',) else ''

    @property
    def is_project_receipt(self):
        """Is this the 'client pays for a project' category?"""
        return self.project_effect_value == 'receipt'

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<CashFlowCategory {self.id}:{self.name!r}>"


class CashFlowSubcategory(db.Model):
    """Optional second level under a :class:`CashFlowCategory` (e.g. *Cement*)."""

    __tablename__ = 'hdc_cash_flow_subcategory'
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_category.id'), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, index=True)
    notes = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    category = db.relationship('CashFlowCategory', foreign_keys=[category_id], backref='subcategories')

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<CashFlowSubcategory {self.id}:{self.name!r}>"


class CashFlowParty(db.Model):
    """Reusable counterparty names for the register (client, supplier, worker...).

    This is where every name the entry form offers in its *Party / Person*
    picker is stored — the list is the table, so a new counterparty exists the
    moment it is added (Settings → Cash Flow, the register, or the form's
    ``+ Add New Party``), and deactivating a row hides it without touching the
    entries that already reference it.

    ``party_type`` (``client | supplier | worker | staff | subcontractor |
    lender | borrower | other``) is the *criterion* the field rules use: a
    category can allow only some types (a loan category allows ``lender`` /
    ``borrower``), which is what makes the party list shorten itself to the
    people who make sense for the transaction being booked.
    """

    __tablename__ = 'hdc_cash_flow_party'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    party_type = db.Column(db.String(40), default='other', index=True)
    phone = db.Column(db.String(40))
    note = db.Column(db.String(300))
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<CashFlowParty {self.id}:{self.name!r}>"


class CashFlowEntry(db.Model):
    """One user-entered cash movement (received / spent / transfer).

    Never edited in place — see the module docstring.  ``amount_minor`` is the
    authoritative value; ``amount`` is the legacy float mirror kept in step by
    :func:`hdc.utils.money.sync_money_fields`.
    """

    __tablename__ = 'hdc_cash_flow_entry'
    id = db.Column(db.Integer, primary_key=True)
    direction = db.Column(db.String(10), nullable=False, index=True)      # in | out | transfer
    amount = db.Column(db.Float, default=0.0)
    amount_minor = db.Column(db.BigInteger, nullable=True, index=True)

    account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=True, index=True)
    destination_account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=True, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_category.id'), nullable=True, index=True)
    subcategory_id = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_subcategory.id'), nullable=True, index=True)
    party_id = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_party.id'), nullable=True, index=True)
    party_name = db.Column(db.String(160), index=True)
    party_type = db.Column(db.String(40), index=True)

    description = db.Column(db.String(200))
    note = db.Column(db.String(500), index=True)
    reference = db.Column(db.String(80), index=True)
    date_posted = db.Column(db.DateTime, default=_pkt_now_naive, index=True)

    # HDC-specific scope tags (AMS has no project/stage dimension).
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True, index=True)
    stage_id = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True, index=True)

    created_by = db.Column(db.String(80), index=True)
    updated_by = db.Column(db.String(80))
    source_type = db.Column(db.String(50), default='MANUAL_CASH_FLOW', index=True)
    source_id = db.Column(db.Integer, nullable=True, index=True)

    # The financial posting this document produced.  One entry -> one row in
    # hdc_account_txn; money never moves without one.
    account_tx_id = db.Column(db.Integer, db.ForeignKey('hdc_account_txn.id'), nullable=True, index=True)

    is_void = db.Column(db.Boolean, default=False, index=True)
    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by = db.Column(db.String(80))
    void_reason = db.Column(db.String(300))

    # Set when this entry amends (replaces) another one, and when it has been
    # replaced — the immutable-ledger paper trail.
    amends_entry_id = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_entry.id'), nullable=True, index=True)
    superseded_by_entry_id = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_entry.id'), nullable=True, index=True)

    reconciliation_id = db.Column(db.Integer, db.ForeignKey('hdc_account_reconciliation.id'), nullable=True, index=True)
    idempotency_key = db.Column(db.String(64), nullable=True, index=True)
    revision = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive, index=True)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive, index=True)

    account = db.relationship('Account', foreign_keys=[account_id])
    destination_account = db.relationship('Account', foreign_keys=[destination_account_id])
    category = db.relationship('CashFlowCategory', foreign_keys=[category_id])
    subcategory = db.relationship('CashFlowSubcategory', foreign_keys=[subcategory_id])
    party = db.relationship('CashFlowParty', foreign_keys=[party_id])
    account_tx = db.relationship('AccountTransaction', foreign_keys=[account_tx_id])
    project = db.relationship('Project', foreign_keys=[project_id])
    stage = db.relationship('Stage', foreign_keys=[stage_id])

    @property
    def entry_date(self):
        """Calendar date (PKT) the entry was posted on — used for day grouping."""
        return (self.date_posted or _pkt_now_naive()).date()

    @property
    def signed_amount_minor(self):
        """``+`` for money in, ``-`` for money out, ``0`` for a transfer."""
        minor = int(self.amount_minor if self.amount_minor is not None else round(float(self.amount or 0.0) * 100))
        if self.direction == 'out':
            return -minor
        if self.direction == 'transfer':
            return 0
        return minor

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<CashFlowEntry {self.id}:{self.direction}:{self.amount}>"


class CashFlowEntryAudit(db.Model):
    """Append-only history for :class:`CashFlowEntry` (created/edited/voided/restored)."""

    __tablename__ = 'hdc_cash_flow_entry_audit'
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_entry.id'), nullable=False, index=True)
    action = db.Column(db.String(20), nullable=False, index=True)
    before_json = db.Column(db.Text)
    after_json = db.Column(db.Text)
    reason = db.Column(db.String(300))
    changed_by = db.Column(db.String(80))
    changed_at = db.Column(db.DateTime, default=_pkt_now_naive, index=True)

    entry = db.relationship('CashFlowEntry', foreign_keys=[entry_id], backref='audit_trail')


class AccountReconciliation(db.Model):
    """Immutable per-account closing snapshot for one reconciliation date.

    The full carry chain is stored so any closing can be re-derived without
    replaying history::

        previous_balance -> opening -> + in -> - out -> expected
                                              -> actual (counted)
                                              -> difference -> final

    All money columns have a ``*_minor`` twin; the minor column is
    authoritative.  Rows are never updated or deleted.
    """

    __tablename__ = 'hdc_account_reconciliation'
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=False, index=True)
    previous_reconciliation_id = db.Column(db.Integer, db.ForeignKey('hdc_account_reconciliation.id'), nullable=True, index=True)
    adjustment_transaction_id = db.Column(db.Integer, nullable=True, index=True)
    reconciliation_date = db.Column(db.Date, nullable=False, index=True)
    period_start_at = db.Column(db.DateTime, nullable=True)
    period_end_at = db.Column(db.DateTime, nullable=True)

    previous_balance = db.Column(db.Float, default=0.0)
    opening_balance = db.Column(db.Float, default=0.0)
    transaction_in = db.Column(db.Float, default=0.0)
    transaction_out = db.Column(db.Float, default=0.0)
    transaction_net = db.Column(db.Float, default=0.0)
    expected_balance = db.Column(db.Float, default=0.0)   # calculated closing before adjustment
    actual_balance = db.Column(db.Float, default=0.0)     # physically counted closing
    difference = db.Column(db.Float, default=0.0)         # actual - expected
    final_reconciled_balance = db.Column(db.Float, default=0.0)

    previous_balance_minor = db.Column(db.BigInteger, nullable=True)
    opening_balance_minor = db.Column(db.BigInteger, nullable=True)
    transaction_in_minor = db.Column(db.BigInteger, nullable=True)
    transaction_out_minor = db.Column(db.BigInteger, nullable=True)
    transaction_net_minor = db.Column(db.BigInteger, nullable=True)
    expected_balance_minor = db.Column(db.BigInteger, nullable=True)
    actual_balance_minor = db.Column(db.BigInteger, nullable=True)
    difference_minor = db.Column(db.BigInteger, nullable=True)
    final_reconciled_balance_minor = db.Column(db.BigInteger, nullable=True)

    difference_type = db.Column(db.String(20), default='Matched', index=True)   # Matched | Loss | Excess
    status = db.Column(db.String(20), default='Reconciled', index=True)
    note = db.Column(db.String(500))
    created_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=_pkt_now_naive, index=True)

    account = db.relationship('Account', foreign_keys=[account_id], backref='reconciliations')


class CashDayLock(db.Model):
    """Day-level lock for the Daily Cash & Bank Reconciliation page.

    Once a financial day is verified and locked, the counted total for that
    day is authoritative and each account's counted closing is carried forward
    as the next day's opening position.
    """

    __tablename__ = 'hdc_cash_day_lock'
    id = db.Column(db.Integer, primary_key=True)
    lock_date = db.Column(db.Date, nullable=False, unique=True, index=True)
    total_expected = db.Column(db.Float, default=0.0)
    total_counted = db.Column(db.Float, default=0.0)
    difference = db.Column(db.Float, default=0.0)
    note = db.Column(db.String(500))
    locked_by = db.Column(db.String(80))
    locked_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)


class CashDayAccountPosition(db.Model):
    """Per-account position for one financial day on the reconciliation page.

    Stores the ledger-computed movement columns plus the user's physically
    counted figure.  When ``is_locked`` is true, ``counted`` is the
    authoritative closing for that account and rolls forward as the next
    day's opening.
    """

    __tablename__ = 'hdc_cash_day_position'
    id = db.Column(db.Integer, primary_key=True)
    position_date = db.Column(db.Date, nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=False, index=True)
    account_name = db.Column(db.String(120))

    opening = db.Column(db.Float, default=0.0)
    opening_minor = db.Column(db.BigInteger, nullable=True)
    amount_in = db.Column(db.Float, default=0.0)
    amount_in_minor = db.Column(db.BigInteger, nullable=True)
    amount_out = db.Column(db.Float, default=0.0)
    amount_out_minor = db.Column(db.BigInteger, nullable=True)
    transfer_in = db.Column(db.Float, default=0.0)
    transfer_in_minor = db.Column(db.BigInteger, nullable=True)
    transfer_out = db.Column(db.Float, default=0.0)
    transfer_out_minor = db.Column(db.BigInteger, nullable=True)
    expected_closing = db.Column(db.Float, default=0.0)
    expected_closing_minor = db.Column(db.BigInteger, nullable=True)

    counted = db.Column(db.Float, nullable=True)          # NULL until entered
    counted_minor = db.Column(db.BigInteger, nullable=True)
    difference = db.Column(db.Float, nullable=True)
    difference_minor = db.Column(db.BigInteger, nullable=True)

    is_locked = db.Column(db.Boolean, default=False, index=True)
    locked_by = db.Column(db.String(80))
    locked_at = db.Column(db.DateTime, nullable=True)
    updated_by = db.Column(db.String(80))
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    __table_args__ = (
        db.UniqueConstraint('position_date', 'account_id', name='uq_hdc_cash_day_position'),
    )

    account = db.relationship('Account', foreign_keys=[account_id])
