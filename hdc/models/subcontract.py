"""HDC models.subcontract — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive, _pkt_today

class Subcontractor(db.Model):
    __tablename__ = 'hdc_subcontractor'
    id                  = db.Column(db.Integer, primary_key=True)
    subcontractor_code  = db.Column(db.String(20), unique=True)
    project_id          = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    stage_id            = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    stage_rel           = db.relationship('Stage', foreign_keys=[stage_id], backref='subcontractor_records')
    name                = db.Column(db.String(100), nullable=False)
    phone               = db.Column(db.String(30))
    work_type           = db.Column(db.String(100))
    contract_type       = db.Column(db.String(20), default='lump_sum')
    rate_per_sqft       = db.Column(db.Float, default=0.0)
    total_sqft          = db.Column(db.Float, default=0.0)
    lump_sum_amount     = db.Column(db.Float, default=0.0)
    retention_percentage= db.Column(db.Float, default=0.0)
    work_done_percentage= db.Column(db.Float, default=0.0)
    created_at          = db.Column(db.DateTime, default=_pkt_now_naive)

    payments = db.relationship('SubcontractPayment', backref='subcontractor', lazy=True, cascade='all, delete-orphan')
    attendance_logs = db.relationship('SubcontractAttendance', backref='subcontractor', lazy=True, cascade='all, delete-orphan')
    labour_attendance_logs = db.relationship('SubcontractLabourAttendance', backref='subcontractor', lazy=True, cascade='all, delete-orphan')
    labour_workers = db.relationship('SubcontractLabourWorker', backref='subcontractor', lazy=True, cascade='all, delete-orphan')

    @property
    def contract_value(self):
        if self.contract_type == 'sqft':
            return (self.rate_per_sqft or 0) * (self.total_sqft or 0)
        return self.lump_sum_amount or 0

    @property
    def total_paid(self):
        return sum(
            p.amount for p in self.payments
            if (not getattr(p, 'is_void', False)) and (p.entry_type or 'payment').strip().lower() != 'settlement'
        )

    @property
    def total_settled(self):
        return sum(
            p.amount for p in self.payments
            if (not getattr(p, 'is_void', False)) and (p.entry_type or 'payment').strip().lower() == 'settlement'
        )

    @property
    def total_cleared(self):
        return float(self.total_paid or 0.0) + float(self.total_settled or 0.0)

    @property
    def retention_amount(self):
        return self.contract_value * (self.retention_percentage or 0) / 100

    @property
    def attendance_days(self):
        return len([r for r in (self.attendance_logs or []) if (r.present_count or 0) > 0])

    @property
    def attendance_headcount(self):
        return sum((r.present_count or 0) for r in (self.attendance_logs or []))

    @property
    def logged_progress_percentage(self):
        total = sum((r.work_done_pct or 0.0) for r in (self.attendance_logs or []))
        return max(0.0, min(100.0, float(total or 0.0)))

    @property
    def effective_progress_percentage(self):
        manual = float(self.work_done_percentage or 0.0)
        logged = float(self.logged_progress_percentage or 0.0)
        # Manual stage-level progress should take precedence when provided.
        base = manual if manual > 0 else logged
        return max(0.0, min(100.0, base))

    @property
    def gross_payable_amount(self):
        return (self.contract_value or 0.0) * (self.effective_progress_percentage or 0.0) / 100.0

    @property
    def payable_amount(self):
        cap = (self.contract_value or 0.0) - (self.retention_amount or 0.0)
        cap = max(0.0, cap)
        return max(0.0, min(self.gross_payable_amount or 0.0, cap))

    @property
    def payable_balance(self):
        return max(0.0, (self.payable_amount or 0.0) - (self.total_cleared or 0.0))

    @property
    def live_balance(self):
        # Positive => still payable, Negative => advance paid.
        return float(self.payable_amount or 0.0) - float(self.total_cleared or 0.0)

    @property
    def advance_paid(self):
        return max(0.0, -float(self.live_balance or 0.0))

    @property
    def contract_balance(self):
        return max(0.0, (self.contract_value or 0.0) - (self.total_cleared or 0.0))

    @property
    def balance_due(self):
        # Backward compatibility for existing templates.
        return self.payable_balance

    @property
    def labour_days_logged(self):
        return len([r for r in (self.labour_attendance_logs or []) if (r.labour_count or 0) > 0])

    @property
    def labour_headcount_total(self):
        return sum(int(r.labour_count or 0) for r in (self.labour_attendance_logs or []))

    @property
    def labour_cost_total(self):
        return sum(float(r.total_labour_paid or 0.0) for r in (self.labour_attendance_logs or []))


class SubcontractPayment(db.Model):
    __tablename__ = 'hdc_subcontract_payment'
    id               = db.Column(db.Integer, primary_key=True)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey('hdc_subcontractor.id'), nullable=False)
    project_id       = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    stage_id         = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    entry_type       = db.Column(db.String(20), default='payment')  # payment / settlement
    amount           = db.Column(db.Float, default=0.0)
    date             = db.Column(db.Date, default=_pkt_today)
    notes            = db.Column(db.String(200))
    is_void          = db.Column(db.Boolean, default=False)
    void_reason      = db.Column(db.String(250))
    voided_at        = db.Column(db.DateTime)
    activity_at      = db.Column(db.DateTime, default=_pkt_now_naive)
    created_at       = db.Column(db.DateTime, default=_pkt_now_naive)

    project = db.relationship('Project')
    stage = db.relationship('Stage')


class SubcontractAttendance(db.Model):
    __tablename__ = 'hdc_subcontract_attendance'
    id               = db.Column(db.Integer, primary_key=True)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey('hdc_subcontractor.id'), nullable=False)
    date             = db.Column(db.Date, default=_pkt_today, nullable=False)
    present_count    = db.Column(db.Integer, default=0)
    work_done_pct    = db.Column(db.Float, default=0.0)
    notes            = db.Column(db.String(250))
    activity_at      = db.Column(db.DateTime, default=_pkt_now_naive)
    created_at       = db.Column(db.DateTime, default=_pkt_now_naive)

    __table_args__ = (db.UniqueConstraint('subcontractor_id', 'date', name='uq_subcontract_attendance_sub_date'),)


class SubcontractLabourAttendance(db.Model):
    __tablename__ = 'hdc_subcontract_labour_attendance'
    id               = db.Column(db.Integer, primary_key=True)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey('hdc_subcontractor.id'), nullable=False)
    project_id       = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    stage_id         = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=False)
    worker_id        = db.Column(db.Integer, db.ForeignKey('hdc_subcontract_labour_worker.id'), nullable=True)
    date             = db.Column(db.Date, default=_pkt_today, nullable=False)
    labour_count     = db.Column(db.Integer, default=0)
    wage_rate        = db.Column(db.Float, default=0.0)
    total_labour_paid= db.Column(db.Float, default=0.0)
    attendance_status = db.Column(db.String(20), default='Present')
    working_hours    = db.Column(db.Float, default=0.0)
    overtime_hours   = db.Column(db.Float, default=0.0)
    notes            = db.Column(db.String(250))
    activity_at      = db.Column(db.DateTime, default=_pkt_now_naive)
    created_at       = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at       = db.Column(db.DateTime, default=_pkt_now_naive)

    project = db.relationship('Project')
    stage = db.relationship('Stage')
    worker = db.relationship('SubcontractLabourWorker', backref='attendance_rows')

    __table_args__ = (db.UniqueConstraint('subcontractor_id', 'stage_id', 'worker_id', 'date', name='uq_sub_labour_sub_stage_worker_date'),)


class SubcontractEvent(db.Model):
    __tablename__ = 'hdc_subcontract_event'
    id               = db.Column(db.Integer, primary_key=True)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey('hdc_subcontractor.id'), nullable=False)
    project_id       = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    stage_id         = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    actor_user_id    = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)
    event_type       = db.Column(db.String(40), nullable=False)  # create/shift/reassign/unassign/payment/attendance/progress/price_update
    from_value       = db.Column(db.String(250))
    to_value         = db.Column(db.String(250))
    amount           = db.Column(db.Float, default=0.0)
    notes            = db.Column(db.String(300))
    created_at       = db.Column(db.DateTime, default=_pkt_now_naive)

    subcontractor = db.relationship('Subcontractor', backref='event_logs')
    project = db.relationship('Project')
    stage = db.relationship('Stage')
    actor = db.relationship('HDCUser')


class SubcontractLabourWorker(db.Model):
    __tablename__ = 'hdc_subcontract_labour_worker'
    id               = db.Column(db.Integer, primary_key=True)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey('hdc_subcontractor.id'), nullable=False)
    name             = db.Column(db.String(120), nullable=False)
    phone            = db.Column(db.String(30))
    trade            = db.Column(db.String(80))
    daily_wage       = db.Column(db.Float, default=0.0)
    active_status    = db.Column(db.Boolean, default=True)
    created_at       = db.Column(db.DateTime, default=_pkt_now_naive)

    @property
    def total_earned(self):
        return sum(float(r.total_labour_paid or 0.0) for r in (self.attendance_rows or []))

    @property
    def total_paid(self):
        return sum(float(p.amount or 0.0) for p in (self.payments or []))

    @property
    def payable_balance(self):
        return max(0.0, float(self.total_earned or 0.0) - float(self.total_paid or 0.0))


class SubcontractLabourPayment(db.Model):
    __tablename__ = 'hdc_subcontract_labour_payment'
    id               = db.Column(db.Integer, primary_key=True)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey('hdc_subcontractor.id'), nullable=False)
    worker_id        = db.Column(db.Integer, db.ForeignKey('hdc_subcontract_labour_worker.id'), nullable=False)
    amount           = db.Column(db.Float, default=0.0)
    date             = db.Column(db.Date, default=_pkt_today)
    notes            = db.Column(db.String(250))
    is_void          = db.Column(db.Boolean, default=False)
    void_reason      = db.Column(db.String(250))
    voided_at        = db.Column(db.DateTime, nullable=True)
    activity_at      = db.Column(db.DateTime, default=_pkt_now_naive)
    created_at       = db.Column(db.DateTime, default=_pkt_now_naive)

    worker = db.relationship('SubcontractLabourWorker', backref='payments')
    subcontractor = db.relationship('Subcontractor', backref='labour_worker_payments')
