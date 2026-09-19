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
"""

from sqlalchemy import func

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive, _pkt_today


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
    transfers = db.relationship('ToolRentalTransfer', backref='rental', lazy=True, cascade='all, delete-orphan')

    @property
    def total_pending_tools(self):
        return max(0.0, float(self.total_rented_qty or 0.0) - float(self.total_returned_qty or 0.0))

    @property
    def total_pending_amount(self):
        if (self.billing_type or '').lower() == 'no_charge':
            return 0.0
        return max(0.0, float(self.total_amount or 0.0) - float(self.total_paid or 0.0))

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
    payment_mode = db.Column(db.String(30), default='cash')  # cash / bank / online / credit
    reference = db.Column(db.String(120))
    notes = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)


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

    qty_transferred = db.Column(db.Float, default=0.0)
    transfer_date = db.Column(db.Date, default=_pkt_today)
    notes = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    created_by = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)

    from_project = db.relationship('Project', foreign_keys=[from_project_id])
    to_project = db.relationship('Project', foreign_keys=[to_project_id])
    from_stage = db.relationship('Stage', foreign_keys=[from_stage_id])
    to_stage = db.relationship('Stage', foreign_keys=[to_stage_id])


class ToolMovementLog(db.Model):
    """Global tool tracking: every rental, return, transfer logs here for 'where are all tools' view."""
    __tablename__ = 'hdc_tool_movement_log'
    id = db.Column(db.Integer, primary_key=True)
    tool_id = db.Column(db.Integer, db.ForeignKey('hdc_tool.id'), nullable=False, index=True)
    rental_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental.id'), nullable=True, index=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_transfer.id'), nullable=True)
    return_id = db.Column(db.Integer, db.ForeignKey('hdc_tool_rental_return.id'), nullable=True)

    movement_type = db.Column(db.String(30), default='rental_out')  # rental_out / return_in / site_transfer / external_transfer / purchase_in / adjustment
    from_location_label = db.Column(db.String(300))
    to_location_label = db.Column(db.String(300))
    qty = db.Column(db.Float, default=0.0)

    timestamp = db.Column(db.DateTime, default=_pkt_now_naive, index=True)
    notes = db.Column(db.String(500))

    tool = db.relationship('Tool', foreign_keys=[tool_id])
    rental = db.relationship('ToolRental', foreign_keys=[rental_id])
