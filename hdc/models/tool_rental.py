"""HDC Tool Rental models — inventory, rentals, partial returns, transfers, tracking.

Design goals:
- Own tools bought and tracked centrally.
- Rent to internal sites (projects/stages) — sometimes no_charge (contract included),
  sometimes fee-based.
- Rent to external normal customers.
- Support 50 tools rented, returned full or partial, paid full or partial, credit.
- Flexible toggle: full / partial for tools AND money, auto calc pendings.
- Site-to-site movement: Site1 > Site2 > Site3 chain, current site green active.
- KPI + reporting searchable by site, tool, date range, customer, status.
- Stock life-cycle: buy more of a tool (purchase in) and throw away what is
  broken (scrap out), so ``total_quantity`` always has a documented reason.
"""

from sqlalchemy import func

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive, _pkt_today

# Why a piece left the store without coming back.  Stored on ToolScrap so the
# write-off report can group by reason (insurance / owner claim / scrap sale).
TOOL_SCRAP_REASONS = (
    ('damaged', 'Damaged beyond repair'),
    ('lost', 'Lost / missing'),
    ('worn_out', 'Worn out / end of life'),
    ('obsolete', 'Obsolete / replaced by newer model'),
    ('sold_as_scrap', 'Sold as scrap'),
    ('other', 'Other'),
)
TOOL_SCRAP_REASON_LABELS = dict(TOOL_SCRAP_REASONS)

# Movement types that mean "stock came in" / "stock left for good".
MOVEMENT_PURCHASE_IN = 'purchase_in'
MOVEMENT_SCRAP_OUT = 'scrap_out'

# Why a customer was charged less than the rental earned.  Stored on
# ToolRentalDiscount so the concession report can group by reason and so a
# discount is never an unexplained hole in the receivable.
TOOL_DISCOUNT_REASONS = (
    ('goodwill', 'Goodwill / courtesy'),
    ('negotiated', 'Negotiated rate'),
    ('damage', 'Damaged / short tool compensation'),
    ('delay', 'Late delivery compensation'),
    ('long_term', 'Long-term / repeat customer'),
    ('staff', 'Staff / internal courtesy'),
    ('other', 'Other'),
)
TOOL_DISCOUNT_REASON_LABELS = dict(TOOL_DISCOUNT_REASONS)


class ToolCategory(db.Model):
    __tablename__ = 'hdc_tool_category'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    description = db.Column(db.String(300))
    active_status = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    tools = db.relationship('Tool', backref='category', lazy=True)


class Tool(db.Model):
    __tablename__ = 'hdc_tool'
    id = db.Column(db.Integer, primary_key=True)
    tool_code = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(150), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_category.id'), nullable=True)
    description = db.Column(db.String(500))
    unit = db.Column(db.String(30), default='pcs')
    total_quantity = db.Column(db.Float, default=0.0)  # owned
    purchase_cost = db.Column(db.Float, default=0.0)  # per unit cost
    rental_rate_per_day = db.Column(db.Float, default=0.0)
    condition = db.Column(db.String(30), default='good')  # good / maintenance / damaged / lost
    status = db.Column(db.String(30), default='active')  # active / retired / maintenance
    is_void = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    @property
    def rented_out_qty(self):
        """Sum of pending qty across active rentals."""
        from hdc.models.tool_rental import ToolRentalItem
        val = db.session.query(func.coalesce(func.sum(ToolRentalItem.qty_pending), 0.0)).filter(
            ToolRentalItem.tool_id == self.id
        ).scalar() or 0.0
        return float(val)

    @property
    def available_qty(self):
        return max(0.0, float(self.total_quantity or 0.0) - self.rented_out_qty)

    @property
    def utilization_pct(self):
        tot = float(self.total_quantity or 0.0)
        if tot <= 0:
            return 0.0
        return (self.rented_out_qty / tot) * 100.0

    # ---- stock life-cycle helpers (purchase in / scrap out) ----
    # These are convenience reads for one tool at a time (detail pages).  List
    # pages and the ledger use the grouped aggregates in
    # ``hdc.services.tool_rental.tool_stock_aggregates`` instead, so a 500-row
    # inventory never turns into 2,000 queries.
    @property
    def purchased_qty(self):
        """Total pieces ever recorded as purchased (0 for legacy tools that were
        entered before the stock register existed)."""
        from hdc.models.tool_rental import ToolPurchase
        val = db.session.query(func.coalesce(func.sum(ToolPurchase.qty), 0.0)).filter(
            ToolPurchase.tool_id == self.id
        ).scalar() or 0.0
        return float(val)

    @property
    def scrapped_qty(self):
        """Total pieces thrown away / lost / sold as scrap."""
        from hdc.models.tool_rental import ToolScrap
        val = db.session.query(func.coalesce(func.sum(ToolScrap.qty), 0.0)).filter(
            ToolScrap.tool_id == self.id
        ).scalar() or 0.0
        return float(val)

    @property
    def purchase_value(self):
        from hdc.models.tool_rental import ToolPurchase
        val = db.session.query(func.coalesce(func.sum(ToolPurchase.total_cost), 0.0)).filter(
            ToolPurchase.tool_id == self.id
        ).scalar() or 0.0
        return float(val)

    @property
    def scrapped_value(self):
        from hdc.models.tool_rental import ToolScrap
        val = db.session.query(func.coalesce(func.sum(ToolScrap.value_written_off), 0.0)).filter(
            ToolScrap.tool_id == self.id
        ).scalar() or 0.0
        return float(val)


class ToolPurchase(db.Model):
    """Stock coming *in*: buying more of a tool we already own.

    One row per purchase event (a supplier bill, a local market run, the
    opening stock of a newly created tool).  ``tool.total_quantity`` is the
    running balance and this table is the reason it moved, so "why do we own
    53 grinders?" always has an answer.
    """
    __tablename__ = 'hdc_tool_purchase'
    id = db.Column(db.Integer, primary_key=True)
    purchase_code = db.Column(db.String(30), unique=True, nullable=False)
    tool_id = db.Column(db.Integer, db.ForeignKey('hdc_tool.id'), nullable=False, index=True)

    purchase_date = db.Column(db.Date, default=_pkt_today)
    qty = db.Column(db.Float, default=0.0)
    unit_cost = db.Column(db.Float, default=0.0)        # price paid per unit
    total_cost = db.Column(db.Float, default=0.0)       # qty * unit_cost
    supplier = db.Column(db.String(150))
    reference = db.Column(db.String(120))               # bill / invoice no
    notes = db.Column(db.String(300))

    is_opening_stock = db.Column(db.Boolean, default=False)
    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    tool = db.relationship('Tool', foreign_keys=[tool_id])


class ToolScrap(db.Model):
    """Stock going *out for good*: broken, lost, worn out or sold as scrap.

    Only pieces that are actually in the store can be scrapped — a tool that is
    rented out has to be returned first, otherwise the balance would break.
    """
    __tablename__ = 'hdc_tool_scrap'
    id = db.Column(db.Integer, primary_key=True)
    scrap_code = db.Column(db.String(30), unique=True, nullable=False)
    tool_id = db.Column(db.Integer, db.ForeignKey('hdc_tool.id'), nullable=False, index=True)

    scrap_date = db.Column(db.Date, default=_pkt_today)
    qty = db.Column(db.Float, default=0.0)
    reason = db.Column(db.String(30), default='damaged')
    unit_cost = db.Column(db.Float, default=0.0)          # cost basis at scrap time
    value_written_off = db.Column(db.Float, default=0.0)  # qty * unit_cost
    reference = db.Column(db.String(120))
    notes = db.Column(db.String(300))

    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    tool = db.relationship('Tool', foreign_keys=[tool_id])

    @property
    def reason_label(self):
        return TOOL_SCRAP_REASON_LABELS.get((self.reason or '').strip().lower(), self.reason or '-')


class ToolRental(db.Model):
    __tablename__ = 'hdc_tool_rental'
    id = db.Column(db.Integer, primary_key=True)
    rental_code = db.Column(db.String(30), unique=True, nullable=False)

    # renter
    renter_type = db.Column(db.String(20), default='internal')  # internal / external
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    stage_id = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    customer_name = db.Column(db.String(150), nullable=True)
    customer_phone = db.Column(db.String(40), nullable=True)
    customer_address = db.Column(db.String(300), nullable=True)

    rental_date = db.Column(db.Date, default=_pkt_today)
    expected_return_date = db.Column(db.Date, nullable=True)

    # billing
    billing_type = db.Column(db.String(30), default='fixed_fee')  # no_charge / fixed_fee / per_day / per_hour
    billing_notes = db.Column(db.String(300))

    total_rented_qty = db.Column(db.Float, default=0.0)
    total_amount = db.Column(db.Float, default=0.0)
    total_paid = db.Column(db.Float, default=0.0)
    # Concessions granted on this rental (cash never moved for this part).
    # Mirrors ``hdc_tool_rental_discount``; see services.tool_rental.
    total_discount = db.Column(db.Float, default=0.0)
    total_returned_qty = db.Column(db.Float, default=0.0)

    # computed helpers (not stored, but we store cached totals above for speed)
    status = db.Column(db.String(30), default='active')  # active / partially_returned / returned / overdue / closed / credit
    payment_status = db.Column(db.String(30), default='unpaid')  # unpaid / partial / paid / credit / no_charge

    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    is_void = db.Column(db.Boolean, default=False)

    project = db.relationship('Project', foreign_keys=[project_id])
    stage = db.relationship('Stage', foreign_keys=[stage_id])

    items = db.relationship('ToolRentalItem', backref='rental', lazy=True, cascade='all, delete-orphan')
    returns = db.relationship('ToolRentalReturn', backref='rental', lazy=True, cascade='all, delete-orphan')
    payments = db.relationship('ToolRentalPayment', backref='rental', lazy=True, cascade='all, delete-orphan')
    discounts = db.relationship('ToolRentalDiscount', backref='rental', lazy=True,
                                cascade='all, delete-orphan',
                                foreign_keys='ToolRentalDiscount.rental_id')
    transfers = db.relationship('ToolRentalTransfer', backref='rental', lazy=True,
                                 cascade='all, delete-orphan',
                                 foreign_keys='ToolRentalTransfer.rental_id')

    @property
    def total_pending_tools(self):
        return max(0.0, float(self.total_rented_qty or 0.0) - float(self.total_returned_qty or 0.0))

    @property
    def total_pending_amount(self):
        """Still owed = earned - cash received - discount granted."""
        if (self.billing_type or '').lower() == 'no_charge':
            return 0.0
        return max(0.0, float(self.total_amount or 0.0)
                   - float(self.total_paid or 0.0)
                   - float(self.total_discount or 0.0))

    @property
    def total_settled_amount(self):
        """Cash + concession — what the customer has cleared in total."""
        return float(self.total_paid or 0.0) + float(self.total_discount or 0.0)

    @property
    def current_location_label(self):
        """Best-effort current location: last transfer to_location else initial rental location."""
        last_transfer = (ToolRentalTransfer.query
                         .filter_by(rental_id=self.id)
                         .order_by(ToolRentalTransfer.transfer_date.desc(), ToolRentalTransfer.id.desc())
                         .first())
        if last_transfer:
            return last_transfer.to_location_label
        # initial
        if self.renter_type == 'internal' and self.project:
            if self.stage:
                return f"{self.project.name} > {self.stage.name}"
            return self.project.name
        return self.customer_name or "External"

    @property
    def tracking_chain(self):
        """List of labels in order: initial -> transfers."""
        chain = []
        # initial location
        if self.renter_type == 'internal' and self.project:
            if self.stage:
                chain.append(f"{self.project.name} ({self.stage.name})")
            else:
                chain.append(self.project.name)
        else:
            chain.append(self.customer_name or "External Customer")

        transfers = (ToolRentalTransfer.query
                     .filter_by(rental_id=self.id)
                     .order_by(ToolRentalTransfer.transfer_date.asc(), ToolRentalTransfer.id.asc())
                     .all())
        for t in transfers:
            chain.append(t.to_location_label)
        return chain


class ToolRentalItem(db.Model):
    __tablename__ = 'hdc_tool_rental_item'
    id = db.Column(db.Integer, primary_key=True)
    rental_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental.id'), nullable=False, index=True)
    tool_id = db.Column(db.Integer, db.ForeignKey('hdc_tool.id'), nullable=False, index=True)

    qty_rented = db.Column(db.Float, default=0.0)
    qty_returned = db.Column(db.Float, default=0.0)
    qty_pending = db.Column(db.Float, default=0.0)  # cached = rented - returned

    rate = db.Column(db.Float, default=0.0)  # per unit rate
    amount = db.Column(db.Float, default=0.0)  # qty_rented * rate (or days calc)

    notes = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    tool = db.relationship('Tool', foreign_keys=[tool_id])


class ToolRentalReturn(db.Model):
    __tablename__ = 'hdc_tool_rental_return'
    id = db.Column(db.Integer, primary_key=True)
    rental_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental.id'), nullable=False, index=True)
    return_date = db.Column(db.Date, default=_pkt_today)
    return_type = db.Column(db.String(20), default='partial')  # full / partial
    payment_type = db.Column(db.String(20), default='partial')  # full / partial / credit / no_payment

    total_tools_returned = db.Column(db.Float, default=0.0)  # in this transaction
    amount_paid = db.Column(db.Float, default=0.0)  # money paid in this return

    notes = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)

    items = db.relationship('ToolRentalReturnItem', backref='return_record', lazy=True, cascade='all, delete-orphan')


class ToolRentalReturnItem(db.Model):
    __tablename__ = 'hdc_tool_rental_return_item'
    id = db.Column(db.Integer, primary_key=True)
    return_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_return.id'), nullable=False, index=True)
    rental_item_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_item.id'), nullable=False, index=True)
    tool_id = db.Column(db.Integer, db.ForeignKey('hdc_tool.id'), nullable=False)

    qty_returned = db.Column(db.Float, default=0.0)
    condition_notes = db.Column(db.String(300))

    tool = db.relationship('Tool', foreign_keys=[tool_id])
    rental_item = db.relationship('ToolRentalItem', foreign_keys=[rental_item_id])


class ToolRentalPayment(db.Model):
    __tablename__ = 'hdc_tool_rental_payment'
    id = db.Column(db.Integer, primary_key=True)
    rental_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental.id'), nullable=False, index=True)
    return_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_return.id'), nullable=True)

    payment_date = db.Column(db.Date, default=_pkt_today)
    amount = db.Column(db.Float, default=0.0)
    # Discount granted in the same settlement as this payment.  It clears part
    # of the receivable but is NOT cash, so it is never added to ``amount``.
    discount = db.Column(db.Float, default=0.0)
    payment_mode = db.Column(db.String(30), default='cash')  # cash / bank / online / credit
    received_to_account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=True)  # which cash/bank account received
    reference = db.Column(db.String(120))
    notes = db.Column(db.String(300))
    is_void = db.Column(db.Boolean, default=False)
    void_reason = db.Column(db.String(250))
    voided_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)

    received_to_account = db.relationship('Account', foreign_keys=[received_to_account_id])


class ToolRentalDiscount(db.Model):
    """A concession granted on a tool rental — money the customer never pays.

    Two flavours, both landing in this one table so the rental's outstanding
    amount always has a documented reason for shrinking:

    * **standalone / waive-off** (``payment_id`` is NULL) — "forget the last
      2,000 PKR", granted on its own from the rental page;
    * **settlement discount** (``payment_id`` set) — granted in the same breath
      as a payment, e.g. cash 8,000 + discount 2,000 clears a 10,000 balance.

    Voiding a payment voids its discount with it, so the receivable and the
    Accounts ledger move together (nothing is stored twice).
    """

    __tablename__ = 'hdc_tool_rental_discount'
    id = db.Column(db.Integer, primary_key=True)
    discount_code = db.Column(db.String(30), unique=True, nullable=False)
    rental_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental.id'), nullable=False, index=True)
    payment_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_payment.id'), nullable=True, index=True)
    return_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_return.id'), nullable=True, index=True)

    discount_date = db.Column(db.Date, default=_pkt_today)
    amount = db.Column(db.Float, default=0.0)
    reason = db.Column(db.String(40), default='goodwill')
    notes = db.Column(db.String(500))

    is_void = db.Column(db.Boolean, default=False)
    void_reason = db.Column(db.String(250))
    voided_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)

    payment = db.relationship('ToolRentalPayment', foreign_keys=[payment_id])

    @property
    def reason_label(self):
        return TOOL_DISCOUNT_REASON_LABELS.get(
            (self.reason or '').strip().lower(),
            (self.reason or '').replace('_', ' ').title() or 'Other')

    @property
    def origin(self):
        """``payment`` when granted with a payment, else ``waive_off``."""
        return 'payment' if self.payment_id else 'waive_off'


class ToolRentalAccountTxn(db.Model):
    """Link between tool rental payment and unified ledger transaction for reconciliation."""
    __tablename__ = 'hdc_tool_rental_account_txn'
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_payment.id'), nullable=False, index=True)
    account_txn_id = db.Column(db.Integer, db.ForeignKey('hdc_account_txn.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)


class ToolRentalTransfer(db.Model):
    """Site-to-site or site-to-customer movement within a rental lifecycle."""
    __tablename__ = 'hdc_tool_rental_transfer'
    id = db.Column(db.Integer, primary_key=True)
    rental_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental.id'), nullable=False, index=True)

    from_type = db.Column(db.String(20), default='site')  # site / customer / warehouse
    from_project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    from_stage_id = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    from_customer_name = db.Column(db.String(150), nullable=True)
    from_location_label = db.Column(db.String(300))

    to_type = db.Column(db.String(20), default='site')
    to_project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    to_stage_id = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    to_customer_name = db.Column(db.String(150), nullable=True)
    to_location_label = db.Column(db.String(300))
    # The brand-new rental a "Transfer Rental" spawned for the destination, so
    # a hand-over chain can be walked a > b > c across rentals (NULL for a
    # plain site-to-site move that stays inside one rental).
    to_rental_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental.id'), nullable=True, index=True)

    qty_transferred = db.Column(db.Float, default=0.0)
    transfer_date = db.Column(db.Date, default=_pkt_today)
    notes = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)

    from_project = db.relationship('Project', foreign_keys=[from_project_id])
    to_project = db.relationship('Project', foreign_keys=[to_project_id])
    from_stage = db.relationship('Stage', foreign_keys=[from_stage_id])
    to_stage = db.relationship('Stage', foreign_keys=[to_stage_id])

    # Per-tool split of this transfer.  Without it a transfer can only say
    # "20 pcs moved" and the dashboard cannot tell *which* tool is now on
    # which site.  Legacy transfers have no rows: the tracker then falls back
    # to "the whole pending line moved" (see hdc/services/tool_tracking.py).
    items = db.relationship('ToolRentalTransferItem', backref='transfer',
                            lazy=True, cascade='all, delete-orphan')


class ToolRentalTransferItem(db.Model):
    """One tool line inside a site-to-site transfer (qty precision per tool)."""
    __tablename__ = 'hdc_tool_rental_transfer_item'
    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_transfer.id'), nullable=False, index=True)
    rental_item_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_item.id'), nullable=False, index=True)
    tool_id = db.Column(db.Integer, db.ForeignKey('hdc_tool.id'), nullable=False, index=True)
    qty_transferred = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    tool = db.relationship('Tool', foreign_keys=[tool_id])
    rental_item = db.relationship('ToolRentalItem', foreign_keys=[rental_item_id])


class ToolSerial(db.Model):
    """Individual serial-numbered piece of a tool type.

    Every physical piece of a multi-quantity tool (e.g. wheelbarrow #1, #2, #3)
    gets one row here so the system can track *which exact piece* is where —
    rented out, returned, transferred between sites, or still in the store.

    ``serial_number`` is the human-readable tag on the piece (sticker, painted
    number, barcode — whatever the warehouse uses).  It is unique per tool type
    so two different tool types can both have a "WB-001" without collision.
    """
    __tablename__ = 'hdc_tool_serial'
    id = db.Column(db.Integer, primary_key=True)
    serial_number = db.Column(db.String(50), nullable=False)
    tool_id = db.Column(db.Integer, db.ForeignKey('hdc_tool.id'), nullable=False, index=True)

    # current location of this exact piece
    is_in_store = db.Column(db.Boolean, default=True)     # True = still in warehouse
    current_rental_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental.id'), nullable=True)
    current_location_label = db.Column(db.String(300))    # human-readable "where is it now"

    # status for the tool-status dialog
    status = db.Column(db.String(30), default='in_store')  # in_store / rented / returned / transferred / maintenance / damaged / lost

    # notes / condition of this specific piece
    condition = db.Column(db.String(30), default='good')  # good / maintenance / damaged / lost
    notes = db.Column(db.String(300))

    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    tool = db.relationship('Tool', backref='serials', foreign_keys=[tool_id])
    current_rental = db.relationship('ToolRental', foreign_keys=[current_rental_id])

    __table_args__ = (
        db.UniqueConstraint('tool_id', 'serial_number', name='uq_tool_serial'),
    )

    @property
    def serial_label(self):
        """Combined label like 'WB-001' or 'Jack Hammer SN-042'."""
        tool = self.tool
        if tool and tool.tool_code:
            return f"{tool.tool_code} / {self.serial_number}"
        return self.serial_number

    @property
    def status_label(self):
        labels = {
            'in_store': 'In Store',
            'rented': 'Rented Out',
            'returned': 'Returned',
            'transferred': 'Transferred',
            'maintenance': 'In Maintenance',
            'damaged': 'Damaged',
            'lost': 'Lost',
        }
        return labels.get(self.status, self.status or '-')


class ToolSerialMovement(db.Model):
    """Movement history for an individual serial-numbered piece.

    Every time a serial moves (rental out, return in, site transfer, store
    return, status change) a row is logged here so the piece's full history
    can be shown in the tool-status dialog.
    """
    __tablename__ = 'hdc_tool_serial_movement'
    id = db.Column(db.Integer, primary_key=True)
    serial_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_serial.id'), nullable=False, index=True)
    rental_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental.id'), nullable=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_transfer.id'), nullable=True)
    return_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_return.id'), nullable=True)

    movement_type = db.Column(db.String(30), default='rental_out')
    from_location_label = db.Column(db.String(300))
    to_location_label = db.Column(db.String(300))
    notes = db.Column(db.String(500))

    timestamp = db.Column(db.DateTime, default=_pkt_now_naive)
    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)

    serial = db.relationship('ToolSerial', backref='movements', foreign_keys=[serial_id])


class ToolMovementLog(db.Model):
    """Global tool tracking: every rental, return, transfer, purchase and scrap
    logs here for the 'where are all tools' view."""
    __tablename__ = 'hdc_tool_movement_log'
    id = db.Column(db.Integer, primary_key=True)
    tool_id = db.Column(db.Integer, db.ForeignKey('hdc_tool.id'), nullable=False, index=True)
    rental_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental.id'), nullable=True, index=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_transfer.id'), nullable=True)
    return_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_return.id'), nullable=True)

    movement_type = db.Column(db.String(30), default='rental_out')  # rental_out / return_in / site_transfer / external_transfer / purchase_in / scrap_out / adjustment
    from_location_label = db.Column(db.String(300))
    to_location_label = db.Column(db.String(300))
    qty = db.Column(db.Float, default=0.0)

    timestamp = db.Column(db.DateTime, default=_pkt_now_naive, index=True)
    notes = db.Column(db.String(500))

    tool = db.relationship('Tool', foreign_keys=[tool_id])
    rental = db.relationship('ToolRental', foreign_keys=[rental_id])


# --------------------------------------------------------------------------- #
# Physical audit — "what the site actually counted" vs "what the book says"
# --------------------------------------------------------------------------- #
#: Movement type used when a physical count changes what we own.  The stock
#: registers (``hdc_tool_purchase`` / ``hdc_tool_scrap``) carry the money-side
#: story; this is the movement-log marker that says "an audit did this".
MOVEMENT_AUDIT_ADJUST = 'adjustment'

#: One count sheet per place (the warehouse, one own site, one outside
#: customer).  ``draft`` while someone is still typing numbers, ``counted``
#: once saved with the discrepancies left open, ``adjusted`` once the losses /
#: overages have been posted to the stock registers.
TOOL_AUDIT_STATUSES = (
    ('draft', 'Count in progress'),
    ('counted', 'Counted — awaiting action'),
    ('adjusted', 'Closed'),
    ('void', 'Cancelled'),
)
TOOL_AUDIT_STATUS_LABELS = dict(TOOL_AUDIT_STATUSES)

#: Per-line verdict, recomputed every time the sheet is saved.
TOOL_AUDIT_LINE_STATES = ('pending', 'match', 'short', 'extra', 'adjusted')
TOOL_AUDIT_LINE_LABELS = {
    'pending': 'Not counted',
    'match': 'Matches',
    'short': 'Short (loss)',
    'extra': 'Extra found',
    'adjusted': 'Adjusted',
}

#: What a site answers when a piece is missing.  Mapped onto the scrap
#: reasons so a write-off from an audit lands in the same register as a
#: scrap typed in by hand — one place to explain every missing piece.
TOOL_AUDIT_LOSS_REASONS = ('lost', 'damaged', 'worn_out', 'sold_as_scrap', 'other')


class ToolAudit(db.Model):
    """One physical verification of one location.

    The book side is never stored as a second truth: expected quantities are
    read from :func:`hdc.services.tool_tracking.tool_ledger` at save time and
    snapshotted onto each line, so the sheet can be reopened later and still
    show what the system believed *on the day of the count*.
    """

    __tablename__ = 'hdc_tool_audit'
    id = db.Column(db.Integer, primary_key=True)
    audit_code = db.Column(db.String(30), unique=True, nullable=False)

    audit_date = db.Column(db.Date, default=_pkt_today)

    # where the count happened — the same three location kinds the tracker uses
    loc_type = db.Column(db.String(20), default='store')     # store/own_project/customer
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True, index=True)
    stage_id = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    customer_name = db.Column(db.String(150), nullable=True)
    location_label = db.Column(db.String(300))

    # who counted / paperwork
    counter_name = db.Column(db.String(150))
    reference = db.Column(db.String(120))                     # count sheet / memo no
    notes = db.Column(db.String(500))

    status = db.Column(db.String(20), default='draft')
    # cached roll-up so list pages never re-add every line
    total_lines = db.Column(db.Float, default=0.0)
    counted_lines = db.Column(db.Float, default=0.0)
    discrepancy_lines = db.Column(db.Float, default=0.0)
    shortage_qty = db.Column(db.Float, default=0.0)
    overage_qty = db.Column(db.Float, default=0.0)
    damaged_qty = db.Column(db.Float, default=0.0)
    write_off_value = db.Column(db.Float, default=0.0)
    adjusted_lines = db.Column(db.Float, default=0.0)

    adjusted_at = db.Column(db.DateTime)
    void_reason = db.Column(db.String(250))
    is_void = db.Column(db.Boolean, default=False)
    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)

    project = db.relationship('Project', foreign_keys=[project_id])
    stage = db.relationship('Stage', foreign_keys=[stage_id])
    lines = db.relationship('ToolAuditLine', backref='audit', lazy=True,
                            cascade='all, delete-orphan', order_by='ToolAuditLine.tool_name')

    @property
    def status_label(self):
        return TOOL_AUDIT_STATUS_LABELS.get(self.status, self.status or '-')

    @property
    def is_open(self):
        """Still editable: a count nobody has closed yet."""
        return (self.status or 'draft') in ('draft', 'counted') and not self.is_void

    @property
    def net_variance(self):
        """Overage minus shortage — what the count added up to overall."""
        return round(float(self.overage_qty or 0.0) - float(self.shortage_qty or 0.0), 2)

    @property
    def has_discrepancy(self):
        return (self.shortage_qty or 0) > 0.001 or (self.overage_qty or 0) > 0.001


class ToolAuditLine(db.Model):
    """One tool on one count sheet: expected, counted, and what we did about it."""

    __tablename__ = 'hdc_tool_audit_line'
    id = db.Column(db.Integer, primary_key=True)
    audit_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_audit.id'), nullable=False, index=True)
    tool_id = db.Column(db.Integer, db.ForeignKey('hdc_tool.id'), nullable=False, index=True)

    tool_name = db.Column(db.String(150))       # snapshot, so the sheet reads right later
    tool_code = db.Column(db.String(30))
    unit = db.Column(db.String(30))

    book_qty = db.Column(db.Float, default=0.0)     # what the system expected here
    counted_qty = db.Column(db.Float)                # NULL until someone counts it
    damaged_qty = db.Column(db.Float, default=0.0)   # counted, but broken / unusable
    variance = db.Column(db.Float, default=0.0)      # counted - book (negative = loss)
    status = db.Column(db.String(20), default='pending')

    # what the Adjust button did about this line (losses go to the scrap register)
    adjusted_qty = db.Column(db.Float, default=0.0)
    adjust_reason = db.Column(db.String(30))
    scrap_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_scrap.id'), nullable=True)

    notes = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive, onupdate=_pkt_now_naive)
    adjusted_at = db.Column(db.DateTime)
    adjusted_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)

    tool = db.relationship('Tool', foreign_keys=[tool_id])
    scrap = db.relationship('ToolScrap', foreign_keys=[scrap_id])

    @property
    def is_counted(self):
        return self.counted_qty is not None

    @property
    def status_label(self):
        return TOOL_AUDIT_LINE_LABELS.get(self.status, self.status or '-')

    @property
    def open_variance(self):
        """Variance still to act on — an adjusted line no longer counts."""
        if self.status == 'adjusted':
            return 0.0
        return round(float(self.variance or 0.0), 2)
