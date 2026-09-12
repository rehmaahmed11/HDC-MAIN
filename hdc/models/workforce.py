"""HDC models.workforce — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive, _pkt_today

class WorkerTrade(db.Model):
    __tablename__ = 'hdc_worker_trade'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(80), unique=True, nullable=False)
    active_status = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=_pkt_now_naive)


class Worker(db.Model):
    __tablename__ = 'hdc_worker'
    id             = db.Column(db.Integer, primary_key=True)
    worker_code    = db.Column(db.String(20), unique=True, nullable=False)
    name           = db.Column(db.String(100), nullable=False)
    role_type      = db.Column(db.String(50))
    base_daily_wage= db.Column(db.Float, default=0.0)
    wage_type      = db.Column(db.String(20), default='daily')  # daily / hourly / per_sqft
    hourly_rate    = db.Column(db.Float, default=0.0)
    rate_per_sqft  = db.Column(db.Float, default=0.0)
    active_status  = db.Column(db.Boolean, default=True)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    attendance   = db.relationship('Attendance', backref='worker', lazy=True)
    ledger       = db.relationship('LabourLedger', backref='worker', lazy=True)
    rate_history = db.relationship('LabourRateHistory', backref='worker', lazy=True)
    time_entries = db.relationship('TimeEntry', backref='worker', lazy=True)
    wage_history = db.relationship('WorkerRate', backref='worker', lazy=True)

    @property
    def hourly_wage(self):
        if self.wage_type == 'hourly' and (self.hourly_rate or 0) > 0:
            return self.hourly_rate or 0
        return (self.base_daily_wage or 0) / 8.0

    @property
    def total_earned(self):
        time_total = sum(t.wage_calculated or 0 for t in self.time_entries if not t.is_void)
        migrated_attendance_ids = {
            int(t.attendance_id) for t in self.time_entries
            if getattr(t, 'attendance_id', None)
        }
        legacy_total = sum(
            a.total_wage for a in self.attendance
            if a.id not in migrated_attendance_ids
        )
        return time_total + legacy_total

    @property
    def total_advanced(self):
        return sum(e.amount for e in self.ledger if e.entry_type == 'advance' and not e.is_void)

    @property
    def total_paid(self):
        cash_paid = sum(e.amount for e in self.ledger if e.entry_type in ('payment', 'tip') and not e.is_void)
        return cash_paid

    @property
    def total_settled(self):
        return sum(e.amount for e in self.ledger if e.entry_type == 'settlement' and not e.is_void)

    @property
    def balance_due(self):
        """Amount still owed to the worker."""
        return self.total_earned - self.total_advanced - self.total_paid - self.total_settled


class LabourLedger(db.Model):
    __tablename__ = 'hdc_labour_ledger'
    id         = db.Column(db.Integer, primary_key=True)
    worker_id  = db.Column(db.Integer, db.ForeignKey('hdc_worker.id'), nullable=False)
    date       = db.Column(db.Date, default=_pkt_today)
    entry_type = db.Column(db.String(20), nullable=False)   # advance / payment / adjustment
    amount     = db.Column(db.Float, default=0.0)
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    stage_id   = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    time_entry_id = db.Column(db.Integer, db.ForeignKey('hdc_time_entry.id'), nullable=True)
    notes      = db.Column(db.Text)
    is_void    = db.Column(db.Boolean, default=False)
    void_reason= db.Column(db.String(250))
    voided_at  = db.Column(db.DateTime, nullable=True)
    activity_at= db.Column(db.DateTime, default=_pkt_now_naive)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    project = db.relationship('Project', backref='labour_ledger_entries')
    stage   = db.relationship('Stage', backref='labour_ledger_entries')


class LabourRateHistory(db.Model):
    __tablename__ = 'hdc_labour_rate_history'
    id             = db.Column(db.Integer, primary_key=True)
    worker_id      = db.Column(db.Integer, db.ForeignKey('hdc_worker.id'), nullable=False)
    old_rate       = db.Column(db.Float, default=0.0)
    new_rate       = db.Column(db.Float, default=0.0)
    effective_from = db.Column(db.Date, default=_pkt_today)
    reason         = db.Column(db.Text)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)


class Attendance(db.Model):
    __tablename__ = 'hdc_attendance'
    id                = db.Column(db.Integer, primary_key=True)
    project_id        = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    worker_id         = db.Column(db.Integer, db.ForeignKey('hdc_worker.id'), nullable=False)
    stage_id          = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    stage             = db.relationship('Stage', backref='attendance_records')
    date              = db.Column(db.Date, nullable=False)
    hours_worked      = db.Column(db.Float, default=8.0)
    overtime_hours    = db.Column(db.Float, default=0.0)
    full_day_equivalent=db.Column(db.Float, default=1.0)
    total_wage        = db.Column(db.Float, default=0.0)
    activity_at       = db.Column(db.DateTime, default=_pkt_now_naive)
    created_at        = db.Column(db.DateTime, default=_pkt_now_naive)
    __table_args__ = (db.UniqueConstraint('project_id', 'worker_id', 'date', name='uq_attendance'),)


class TimeEntry(db.Model):
    __tablename__ = 'hdc_time_entry'
    id             = db.Column(db.Integer, primary_key=True)
    worker_id      = db.Column(db.Integer, db.ForeignKey('hdc_worker.id'), nullable=False)
    project_id     = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    stage_id       = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    check_in       = db.Column(db.DateTime, nullable=False)
    check_out      = db.Column(db.DateTime, nullable=False)
    hours          = db.Column(db.Float, default=0.0)
    overtime       = db.Column(db.Float, default=0.0)
    qty_sqft       = db.Column(db.Float, default=0.0)
    wage_calculated= db.Column(db.Float, default=0.0)
    legacy_calc    = db.Column(db.Boolean, default=False)
    attendance_id  = db.Column(db.Integer, nullable=True)
    is_void        = db.Column(db.Boolean, default=False)
    void_reason    = db.Column(db.String(250))
    voided_at      = db.Column(db.DateTime, nullable=True)
    activity_at    = db.Column(db.DateTime, default=_pkt_now_naive)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    project = db.relationship('Project', backref='time_entries')
    stage   = db.relationship('Stage', backref='time_entries')


class AttendanceDay(db.Model):
    __tablename__ = 'hdc_attendance_day'
    id            = db.Column(db.Integer, primary_key=True)
    worker_id     = db.Column(db.Integer, db.ForeignKey('hdc_worker.id'), nullable=False)
    date          = db.Column(db.Date, nullable=False)
    total_hours   = db.Column(db.Float, default=0.0)
    day_value     = db.Column(db.Float, default=0.0)  # 1 if >= 8h else 0
    overtime_hours= db.Column(db.Float, default=0.0)
    entry_count   = db.Column(db.Integer, default=0)
    is_void       = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at    = db.Column(db.DateTime, default=_pkt_now_naive)

    worker = db.relationship('Worker', backref='attendance_days')
    __table_args__ = (db.UniqueConstraint('worker_id', 'date', name='uq_attendance_day_worker_date'),)


class AttendanceMark(db.Model):
    __tablename__ = 'hdc_attendance_mark'
    id         = db.Column(db.Integer, primary_key=True)
    worker_id  = db.Column(db.Integer, db.ForeignKey('hdc_worker.id'), nullable=False)
    date       = db.Column(db.Date, nullable=False)
    status     = db.Column(db.String(20), nullable=False, default='absent')  # absent
    notes      = db.Column(db.String(250))
    activity_at= db.Column(db.DateTime, default=_pkt_now_naive)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    worker = db.relationship('Worker', backref='attendance_marks')
    __table_args__ = (db.UniqueConstraint('worker_id', 'date', name='uq_attendance_mark_worker_date'),)


class WorkerRate(db.Model):
    __tablename__ = 'hdc_worker_rate'
    id             = db.Column(db.Integer, primary_key=True)
    worker_id      = db.Column(db.Integer, db.ForeignKey('hdc_worker.id'), nullable=False)
    wage_type      = db.Column(db.String(20), default='daily')  # daily / hourly / per_sqft
    rate           = db.Column(db.Float, default=0.0)
    effective_from = db.Column(db.Date, default=_pkt_today)
    reason         = db.Column(db.Text)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)


class PayrollRun(db.Model):
    __tablename__ = 'hdc_payroll_run'
    id          = db.Column(db.Integer, primary_key=True)
    date_from   = db.Column(db.Date, nullable=False)
    date_to     = db.Column(db.Date, nullable=False)
    run_date    = db.Column(db.Date, default=_pkt_today)
    total_amount= db.Column(db.Float, default=0.0)
    status      = db.Column(db.String(20), default='posted')  # posted / draft
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)

    items = db.relationship('PayrollItem', backref='run', lazy=True, cascade='all, delete-orphan')


class PayrollItem(db.Model):
    __tablename__ = 'hdc_payroll_item'
    id            = db.Column(db.Integer, primary_key=True)
    run_id        = db.Column(db.Integer, db.ForeignKey('hdc_payroll_run.id'), nullable=False)
    worker_id     = db.Column(db.Integer, db.ForeignKey('hdc_worker.id'), nullable=False)
    total_hours   = db.Column(db.Float, default=0.0)
    total_overtime= db.Column(db.Float, default=0.0)
    gross_amount  = db.Column(db.Float, default=0.0)
    advance_deducted = db.Column(db.Float, default=0.0)
    net_pay       = db.Column(db.Float, default=0.0)
    created_at    = db.Column(db.DateTime, default=_pkt_now_naive)

    worker = db.relationship('Worker', backref='payroll_items')
