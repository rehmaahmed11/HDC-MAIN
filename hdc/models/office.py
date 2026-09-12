"""HDC models.office — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive, _pkt_today

class OfficeStaff(db.Model):
    __tablename__ = 'hdc_office_staff'
    id             = db.Column(db.Integer, primary_key=True)
    staff_code     = db.Column(db.String(20), unique=True, nullable=False)
    name           = db.Column(db.String(100), nullable=False)
    role_type      = db.Column(db.String(80))
    phone          = db.Column(db.String(30))
    monthly_salary = db.Column(db.Float, default=0.0)
    active_status  = db.Column(db.Boolean, default=True)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    attendance_records = db.relationship('OfficeStaffAttendance', backref='staff', lazy=True)
    ledger_entries = db.relationship('OfficeStaffLedger', backref='staff', lazy=True)
    allowances = db.relationship('StaffAllowance', back_populates='staff', lazy=True)

    @property
    def total_advanced(self):
        return sum(
            e.amount for e in self.ledger_entries
            if e.entry_type == 'advance' and not e.is_void
        )

    @property
    def total_paid(self):
        return sum(
            e.amount for e in self.ledger_entries
            if e.entry_type == 'payment' and not e.is_void
        )

    @property
    def balance_due(self):
        from hdc.services.ledger import _office_staff_ledger_snapshot
        snap = _office_staff_ledger_snapshot(self.id)
        return float(snap.get('balance', 0.0) or 0.0)

    @property
    def total_allowances(self):
        return sum(
            a.amount for a in self.allowances
            if a.is_active
        )


class OfficeStaffAttendance(db.Model):
    __tablename__ = 'hdc_office_staff_attendance'
    id         = db.Column(db.Integer, primary_key=True)
    staff_id   = db.Column(db.Integer, db.ForeignKey('hdc_office_staff.id'), nullable=False)
    date       = db.Column(db.Date, nullable=False)
    status     = db.Column(db.String(20), nullable=False, default='present')  # present / absent / weekly_leave
    notes      = db.Column(db.String(250))
    activity_at= db.Column(db.DateTime, default=_pkt_now_naive)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive)
    __table_args__ = (db.UniqueConstraint('staff_id', 'date', name='uq_office_staff_attendance_staff_date'),)


class OfficeStaffLedger(db.Model):
    __tablename__ = 'hdc_office_staff_ledger'
    id         = db.Column(db.Integer, primary_key=True)
    staff_id   = db.Column(db.Integer, db.ForeignKey('hdc_office_staff.id'), nullable=False)
    date       = db.Column(db.Date, default=_pkt_today)
    entry_type = db.Column(db.String(20), nullable=False)  # advance / payment / adjustment / settlement
    amount     = db.Column(db.Float, default=0.0)
    notes      = db.Column(db.Text)
    is_void    = db.Column(db.Boolean, default=False)
    void_reason= db.Column(db.String(250))
    voided_at  = db.Column(db.DateTime, nullable=True)
    activity_at= db.Column(db.DateTime, default=_pkt_now_naive)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)


class OfficeExpense(db.Model):
    __tablename__ = 'hdc_office_expense'
    id         = db.Column(db.Integer, primary_key=True)
    office_staff_id = db.Column(db.Integer, db.ForeignKey('hdc_office_staff.id'), nullable=True)
    office_staff_ledger_id = db.Column(db.Integer, db.ForeignKey('hdc_office_staff_ledger.id'), nullable=True)
    date       = db.Column(db.Date, default=_pkt_today)
    category   = db.Column(db.String(80))
    amount     = db.Column(db.Float, default=0.0)
    remarks    = db.Column(db.String(250))
    is_void    = db.Column(db.Boolean, default=False)
    void_reason= db.Column(db.String(250))
    voided_at  = db.Column(db.DateTime, nullable=True)
    activity_at= db.Column(db.DateTime, default=_pkt_now_naive)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)


class AllowanceCategory(db.Model):
    __tablename__ = 'hdc_allowance_category'
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(100), nullable=False, unique=True)
    description    = db.Column(db.String(250))
    is_active      = db.Column(db.Boolean, default=True)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    allowances = db.relationship('StaffAllowance', backref='category', lazy=True)


class StaffAllowance(db.Model):
    __tablename__ = 'hdc_staff_allowance'
    id             = db.Column(db.Integer, primary_key=True)
    staff_id       = db.Column(db.Integer, db.ForeignKey('hdc_office_staff.id'), nullable=False)
    category_id    = db.Column(db.Integer, db.ForeignKey('hdc_allowance_category.id'), nullable=False)
    amount         = db.Column(db.Float, default=0.0)
    effective_date = db.Column(db.Date, default=_pkt_today)
    is_active      = db.Column(db.Boolean, default=True)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    staff = db.relationship('OfficeStaff', back_populates='allowances')


class OfficeExpenseCategory(db.Model):
    __tablename__ = 'hdc_office_expense_category'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(80), nullable=False, unique=True)
    active_status = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)
