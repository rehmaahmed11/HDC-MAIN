"""HDC models.accounts — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from sqlalchemy import event

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.money import sync_money_fields

class ExpenseCategory(db.Model):
    __tablename__ = 'hdc_expense_category'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(80), unique=True, nullable=False)
    active_status = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=_pkt_now_naive)


class PersonalExpense(db.Model):
    __tablename__ = 'hdc_personal_expense'
    id                   = db.Column(db.Integer, primary_key=True)
    beneficiary_name     = db.Column(db.String(100), nullable=False)
    beneficiary_type     = db.Column(db.String(50))  # 'worker', 'staff', 'other'
    beneficiary_id       = db.Column(db.Integer, nullable=True)
    date                 = db.Column(db.Date, default=_pkt_today)
    category             = db.Column(db.String(80))
    amount               = db.Column(db.Float, default=0.0)
    remarks              = db.Column(db.String(250))
    is_void              = db.Column(db.Boolean, default=False)
    void_reason          = db.Column(db.String(250))
    voided_at            = db.Column(db.DateTime, nullable=True)
    activity_at          = db.Column(db.DateTime, default=_pkt_now_naive)
    created_at           = db.Column(db.DateTime, default=_pkt_now_naive)


class PersonalExpenseCategory(db.Model):
    __tablename__ = 'hdc_personal_expense_category'
    id                   = db.Column(db.Integer, primary_key=True)
    name                 = db.Column(db.String(80), nullable=False, unique=True)
    description          = db.Column(db.String(250))
    active_status        = db.Column(db.Boolean, default=True)
    created_at           = db.Column(db.DateTime, default=_pkt_now_naive)


class Alert(db.Model):
    __tablename__ = 'hdc_alert'
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(120), nullable=True)
    level      = db.Column(db.String(20), default='warning')  # info / warning / danger
    message    = db.Column(db.Text, nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    stage_id   = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    resolved   = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    project = db.relationship('Project', backref='alerts')
    stage   = db.relationship('Stage', backref='alerts')


class Expense(db.Model):
    __tablename__ = 'hdc_expense'
    id         = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    stage_id   = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    tip_worker_id = db.Column(db.Integer, db.ForeignKey('hdc_worker.id'), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey('hdc_expense_category.id'), nullable=True)
    stage      = db.relationship('Stage', backref='expense_records')
    expense_category = db.relationship('ExpenseCategory', backref='expense_records')
    amount     = db.Column(db.Float, default=0.0)
    date       = db.Column(db.Date, default=_pkt_today)
    remarks    = db.Column(db.String(200))
    is_void    = db.Column(db.Boolean, default=False)
    void_reason= db.Column(db.String(250))
    voided_at  = db.Column(db.DateTime, nullable=True)
    activity_at= db.Column(db.DateTime, default=_pkt_now_naive)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    @property
    def category(self):
        return (self.expense_category.name if self.expense_category else '')


class Account(db.Model):
    """A pot of money or a counterparty ledger.

    ``type`` remains the compatibility surface used by every existing screen
    (company / cash / bank / person / vendor / client).  The ``class_*`` /
    ``channel`` columns below are the additive, controlled classification
    hierarchy ported from the AMS accounts model — see
    ``hdc/services/account_classification.py`` for the authoritative registry.
    They are optional: legacy rows load and work with them NULL.
    """
    __tablename__ = 'hdc_account'
    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(120), nullable=False)
    type            = db.Column(db.String(20), nullable=False)  # company / cash / bank / person / vendor / client
    opening_balance = db.Column(db.Float, default=0.0)
    opening_balance_minor = db.Column(db.BigInteger, nullable=True)  # authoritative paisa mirror
    bank_name       = db.Column(db.String(120))
    account_number  = db.Column(db.String(80))
    iban            = db.Column(db.String(80))
    auto_generated  = db.Column(db.Boolean, default=False)
    auto_source     = db.Column(db.String(40))
    status          = db.Column(db.String(20), default='active')  # active / inactive / archived
    is_void         = db.Column(db.Boolean, default=False)
    created_at      = db.Column(db.DateTime, default=_pkt_now_naive)

    # --- Controlled classification hierarchy (additive) -------------------
    class_category       = db.Column(db.String(50), index=True)     # Assets, Liabilities, ...
    class_subcategory    = db.Column(db.String(80), index=True)     # Cash, Bank, Client Receivables, ...
    class_account_type   = db.Column(db.String(100), index=True)    # Main Cash, Operating Bank, ...
    channel              = db.Column(db.String(30), index=True)     # cash/bank/digital_wallet/ledger_only/other
    # Channel-specific details (only the subset relevant to `channel` is used).
    cash_location        = db.Column(db.String(120))
    cash_responsible     = db.Column(db.String(120))
    wallet_provider      = db.Column(db.String(100))
    wallet_number        = db.Column(db.String(80))
    wallet_holder        = db.Column(db.String(120))
    # Linked entity.  HDC resolves persons through the ledger itself, so the
    # link is descriptive (type + name) rather than a hard FK.
    linked_entity_type   = db.Column(db.String(30), index=True)     # none/client/supplier/worker/partner/party
    linked_party_name    = db.Column(db.String(160))
    note                 = db.Column(db.String(500))
    updated_by           = db.Column(db.String(80))
    updated_at           = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)


def _sync_txn_minor_units(_mapper, _connection, target):
    """Keep ``amount_minor`` (authoritative paisa) in step with ``amount``.

    Registered on both insert and update so *every* module that posts to the
    unified ledger — payroll, expenses, purchases, subcontract, office and the
    cash flow register — gets the exact integer mirror for free, even if it
    never heard of the cash flow layer.  Defensive by design: a bad value must
    never turn a posting into a 500.
    """
    try:
        sync_money_fields(target, 'amount', 'amount_minor')
    except Exception:
        pass


class AccountTransaction(db.Model):
    __tablename__ = 'hdc_account_txn'
    id                    = db.Column(db.Integer, primary_key=True)
    date                  = db.Column(db.Date, default=_pkt_today)
    amount                = db.Column(db.Float, default=0.0)
    amount_minor          = db.Column(db.BigInteger, nullable=True)  # authoritative paisa mirror
    type                  = db.Column(db.String(40), default='expense_general')
    from_account_id       = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=False)
    to_account_id         = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=True)
    executed_by_account_id= db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=False)
    project_id            = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    stage_id              = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    related_entity_type   = db.Column(db.String(40))
    related_entity_id     = db.Column(db.Integer, nullable=True)
    party_name            = db.Column(db.String(120))
    category              = db.Column(db.String(20), nullable=False)  # salary / expense / advance / personal / transfer
    note                  = db.Column(db.String(400))
    reference_id          = db.Column(db.String(120))
    group_id              = db.Column(db.String(64))
    source_type           = db.Column(db.String(80))
    source_id             = db.Column(db.Integer, nullable=True)
    is_void               = db.Column(db.Boolean, default=False)
    created_at            = db.Column(db.DateTime, default=_pkt_now_naive)

    # --- Immutability / audit (additive, ported from the AMS ledger) -------
    # A posted row is never edited in place.  Corrections void the row and
    # post a compensating reversal; these two columns tie the pair together so
    # the original document stays readable in the ledger.
    reversal_of_txn_id    = db.Column(db.Integer, db.ForeignKey('hdc_account_txn.id'), nullable=True, index=True)
    reversed_by_txn_id    = db.Column(db.Integer, db.ForeignKey('hdc_account_txn.id'), nullable=True, index=True)
    reconciliation_id     = db.Column(db.Integer, db.ForeignKey('hdc_account_reconciliation.id'), nullable=True, index=True)
    # Why the row was voided / adjusted, and who did it.
    void_reason           = db.Column(db.String(300))
    voided_by             = db.Column(db.String(80))
    voided_at             = db.Column(db.DateTime, nullable=True)
    reason                = db.Column(db.String(300))
    # Double-submit guard: a retried or double-clicked form cannot post twice.
    idempotency_key       = db.Column(db.String(64), nullable=True, index=True)
    updated_at            = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    from_account = db.relationship('Account', foreign_keys=[from_account_id], backref='outgoing_txns')
    to_account = db.relationship('Account', foreign_keys=[to_account_id], backref='incoming_txns')
    executed_by_account = db.relationship('Account', foreign_keys=[executed_by_account_id], backref='executed_txns')
    project = db.relationship('Project', backref='account_transactions')
    stage = db.relationship('Stage', backref='account_transactions')


event.listen(AccountTransaction, 'before_insert', _sync_txn_minor_units)
event.listen(AccountTransaction, 'before_update', _sync_txn_minor_units)


class OwnerPayment(db.Model):
    """Money received from a project's owner/client.

    This table is the *only* thing ``Project.total_received`` and
    ``Project.remaining_receivable`` are computed from, which makes it the
    system of record for "has this client paid us?".

    Two surfaces write it, and both must, or the project silently reads as
    unpaid while the cash sits in the bank:

    * the Projects page owner-payment form (``_accounts_post_owner_receipt``);
    * the Cash Flow register / New Transaction form, whenever the category is
      tagged ``project_effect='receipt'`` — those rows carry
      ``source_entry_id`` so the register entry and this mirror can always find
      each other (idempotency on create, and void/restore in both directions).
    """

    __tablename__ = 'hdc_owner_payment'
    id         = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    received_to_account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=True)
    #: the CashFlowEntry this row mirrors (NULL for Projects-page receipts)
    source_entry_id = db.Column(db.Integer, db.ForeignKey('hdc_cash_flow_entry.id'),
                                nullable=True, index=True)
    amount     = db.Column(db.Float, default=0.0)
    date       = db.Column(db.Date, default=_pkt_today)
    remarks    = db.Column(db.String(200))
    is_void    = db.Column(db.Boolean, default=False)
    void_reason= db.Column(db.String(250))
    voided_at  = db.Column(db.DateTime, nullable=True)
    activity_at= db.Column(db.DateTime, default=_pkt_now_naive)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    received_to_account = db.relationship('Account', foreign_keys=[received_to_account_id], backref='owner_receipts')


class AccountIntentRule(db.Model):
    """Which fields one *transaction type* shows on Accounts → All Entries.

    The entry form is driven by the type the user picks first: a transfer needs
    two accounts, a supplier payment needs the supplier, a loan needs the person
    and nothing else.  Those rules used to live in the page's JavaScript, which
    meant every field was rendered and most of them were force-shown — asking
    "Related Entity" and "Party / Purpose" even on a type that has neither.

    Each row here is one type's answer, editable in **Settings → Cash Flow**
    without a deploy.  ``hdc.services.accounts._account_intent_field_matrix()``
    merges these rows over the shipped defaults, and the page renders the result
    as the ``intentMatrix`` JSON the script obeys — so what is asked for is
    always the data, never the code.
    """

    __tablename__ = 'hdc_account_intent_rule'

    id = db.Column(db.Integer, primary_key=True)
    tx_type = db.Column(db.String(60), nullable=False, unique=True, index=True)
    label = db.Column(db.String(120))
    direction = db.Column(db.String(12))          # receive | pay | transfer | ''

    show_from_account = db.Column(db.Boolean, default=True)
    show_to_account = db.Column(db.Boolean, default=True)
    to_account_required = db.Column(db.Boolean, default=False)

    show_project = db.Column(db.Boolean, default=True)
    project_required = db.Column(db.Boolean, default=False)
    show_stage = db.Column(db.Boolean, default=True)
    stage_required = db.Column(db.Boolean, default=False)

    show_related = db.Column(db.Boolean, default=True)
    related_type = db.Column(db.String(30))       # worker | supplier | subcontractor | office_staff

    show_party = db.Column(db.Boolean, default=True)
    party_required = db.Column(db.Boolean, default=False)
    show_reference = db.Column(db.Boolean, default=True)
    # The two one-off pickers that only some types have.
    show_expense_category = db.Column(db.Boolean, default=False)
    show_office_target = db.Column(db.Boolean, default=False)

    is_active = db.Column(db.Boolean, default=True, index=True)
    sort_order = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    def as_dict(self):
        """The shape the page script reads (kept in one place on purpose)."""
        return {
            'direction': self.direction or '',
            'from_account': bool(self.show_from_account),
            'to_account': bool(self.show_to_account),
            'to_account_required': bool(self.to_account_required),
            'project': bool(self.show_project),
            'project_required': bool(self.project_required),
            'stage': bool(self.show_stage),
            'stage_required': bool(self.stage_required),
            'related': bool(self.show_related),
            'related_type': (self.related_type or ''),
            'party_name': bool(self.show_party),
            'party_name_required': bool(self.party_required),
            'reference': bool(self.show_reference),
            'expense_category': bool(self.show_expense_category),
            'office_target': bool(self.show_office_target),
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<AccountIntentRule {self.tx_type!r}>"
