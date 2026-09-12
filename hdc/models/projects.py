"""HDC models.projects — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from sqlalchemy import func

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive, _pkt_today

class Estimation(db.Model):
    __tablename__ = 'hdc_estimation'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(120), nullable=False)
    total_cost  = db.Column(db.Float, default=0.0)
    status      = db.Column(db.String(20), default='draft')  # draft / finalized
    project_id  = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at  = db.Column(db.DateTime, default=_pkt_now_naive)

    stages = db.relationship('EstimationStage', backref='estimation', lazy=True, cascade='all, delete-orphan')


class EstimationStage(db.Model):
    __tablename__ = 'hdc_estimation_stage'
    id             = db.Column(db.Integer, primary_key=True)
    estimation_id  = db.Column(db.Integer, db.ForeignKey('hdc_estimation.id'), nullable=False)
    stage_name     = db.Column(db.String(120), nullable=False)
    calc_type      = db.Column(db.String(20), default='lump_sum')  # lump_sum / sqft
    rate           = db.Column(db.Float, default=0.0)
    quantity       = db.Column(db.Float, default=0.0)
    total          = db.Column(db.Float, default=0.0)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)


class Project(db.Model):
    __tablename__ = 'hdc_project'
    id                    = db.Column(db.Integer, primary_key=True)
    project_code          = db.Column(db.String(20), unique=True, nullable=False)
    name                  = db.Column(db.String(100), nullable=False)
    client                = db.Column(db.String(100))
    client_phone          = db.Column(db.String(30))
    location              = db.Column(db.String(200))
    total_constructed_sqft= db.Column(db.Float, default=0.0)
    owner_rate_per_sqft   = db.Column(db.Float, default=0.0)
    owner_lump_sum        = db.Column(db.Float, default=0.0)
    contract_type         = db.Column(db.String(20), default='sqft')
    start_date            = db.Column(db.Date, default=_pkt_today)
    status                = db.Column(db.String(20), default='Active')
    estimation_id         = db.Column(db.Integer, db.ForeignKey('hdc_estimation.id'), nullable=True)
    budget_total          = db.Column(db.Float, default=0.0)
    planned_start         = db.Column(db.Date, default=_pkt_today)
    planned_end           = db.Column(db.Date, nullable=True)
    created_at            = db.Column(db.DateTime, default=_pkt_now_naive)

    stages           = db.relationship('Stage', backref='project', lazy=True, cascade='all, delete-orphan')
    owner_payments   = db.relationship('OwnerPayment', backref='project', lazy=True, cascade='all, delete-orphan')
    expenses         = db.relationship('Expense', backref='project', lazy=True, cascade='all, delete-orphan')
    attendance_records = db.relationship('Attendance', backref='project', lazy=True, cascade='all, delete-orphan')
    subcontractors   = db.relationship('Subcontractor', backref='project', lazy=True, cascade='all, delete-orphan')
    purchases        = db.relationship('Purchase', backref='project', lazy=True, cascade='all, delete-orphan')

    @property
    def stage_contract_value(self):
        cached = getattr(self, '_agg_stage_contract_value', None)
        if cached is not None:
            return float(cached or 0.0)
        return sum(s.contract_value for s in self.stages)

    @property
    def owner_contract_value(self):
        sv = self.stage_contract_value
        if sv > 0:
            return sv
        if self.contract_type == 'sqft':
            return (self.total_constructed_sqft or 0) * (self.owner_rate_per_sqft or 0)
        elif self.contract_type == 'lump_sum':
            return self.owner_lump_sum or 0
        return ((self.total_constructed_sqft or 0) * (self.owner_rate_per_sqft or 0)) + (self.owner_lump_sum or 0)

    @property
    def total_received(self):
        from hdc.models.accounts import OwnerPayment
        cached = getattr(self, '_agg_total_received', None)
        if cached is not None:
            return float(cached or 0.0)
        return float(db.session.query(func.coalesce(func.sum(OwnerPayment.amount), 0.0))
                     .filter(OwnerPayment.project_id == self.id, OwnerPayment.is_void == False)
                     .scalar() or 0.0)

    @property
    def total_expense_cost(self):
        cached = getattr(self, '_agg_total_expense_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        return sum(
            e.amount for e in self.expenses
            if not getattr(e, 'is_void', False)
        )

    @property
    def total_tip_expense(self):
        return sum(
            e.amount for e in self.expenses
            if not getattr(e, 'is_void', False)
            and (e.category or '').strip().lower() == 'tip'
        )

    @property
    def total_expenses(self):        # alias for templates
        return self.total_expense_cost

    @property
    def total_labour_cost(self):
        from hdc.models.workforce import Attendance, TimeEntry
        cached = getattr(self, '_agg_total_labour_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        legacy = (db.session.query(func.coalesce(func.sum(Attendance.total_wage), 0.0))
                  .outerjoin(TimeEntry, TimeEntry.attendance_id == Attendance.id)
                  .filter(
                      Attendance.project_id == self.id,
                      TimeEntry.id.is_(None)
                  )
                  .scalar() or 0.0)
        time_cost = sum(t.wage_calculated or 0 for t in TimeEntry.query.filter_by(project_id=self.id, is_void=False).all())
        return legacy + time_cost

    @property
    def total_material_cost(self):
        from hdc.models.materials import Purchase, UsageLogV2
        cached = getattr(self, '_agg_total_material_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        legacy_total = sum(
            p.total for p in Purchase.query.filter_by(project_id=self.id).all()
        )
        v2_total = (db.session.query(func.coalesce(func.sum(UsageLogV2.cost), 0.0))
                    .filter(UsageLogV2.project_id == self.id,
                            UsageLogV2.is_void == False)
                    .scalar() or 0.0)
        return float(legacy_total or 0.0) + float(v2_total or 0.0)

    @property
    def total_subcontract_cost(self):
        cached = getattr(self, '_agg_total_subcontract_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        # Expense-side subcontract cost should be cash-cleared, not contract commitment.
        return sum(float(s.total_cleared or 0.0) for s in self.subcontractors)

    @property
    def total_cost(self):
        cached = getattr(self, '_agg_total_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        return (self.total_labour_cost + self.total_subcontract_cost +
                self.total_expense_cost + self.total_material_cost)

    @property
    def gross_margin(self):
        return self.owner_contract_value - self.total_subcontract_cost

    @property
    def net_profit(self):
        cached = getattr(self, '_agg_net_profit', None)
        if cached is not None:
            return float(cached or 0.0)
        return self.owner_contract_value - self.total_cost

    @property
    def actual_cost(self):
        return self.total_cost

    @property
    def remaining_receivable(self):
        cached = getattr(self, '_agg_remaining_receivable', None)
        if cached is not None:
            return float(cached or 0.0)
        return self.owner_contract_value - self.total_received


class StageDefinition(db.Model):
    __tablename__ = 'hdc_stage_definition'
    id            = db.Column(db.Integer, primary_key=True)
    project_id    = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=True)
    project       = db.relationship('Project', backref='stage_definitions')
    name          = db.Column(db.String(120), nullable=False)
    default_order = db.Column(db.Integer, default=0)
    active_status = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=_pkt_now_naive)
    __table_args__ = (db.UniqueConstraint('project_id', 'name', name='uq_stage_definition_project_name'),)


class Stage(db.Model):
    __tablename__ = 'hdc_stage'
    id                        = db.Column(db.Integer, primary_key=True)
    project_id                = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    definition_id             = db.Column(db.Integer, db.ForeignKey('hdc_stage_definition.id'), nullable=True)
    definition                = db.relationship('StageDefinition', backref='stages')
    name                      = db.Column(db.String(120), nullable=False)
    status                    = db.Column(db.String(30), default='Active')
    estimated_cost            = db.Column(db.Float, default=0.0)
    progress                  = db.Column(db.Float, default=0.0)
    execution_mode            = db.Column(db.String(20), default='company')  # company / subcontractor
    assigned_subcontractor_id = db.Column(db.Integer, db.ForeignKey('hdc_subcontractor.id'), nullable=True)
    start_date                = db.Column(db.Date, nullable=True)
    end_date                  = db.Column(db.Date, nullable=True)
    contract_basis            = db.Column(db.String(30), nullable=True)   # Per Sq Ft / Lump Sum
    rate_per_sqft             = db.Column(db.Float, default=0.0)
    discount_per_sqft         = db.Column(db.Float, default=0.0)
    qty_sqft                  = db.Column(db.Float, default=0.0)
    lump_sum_value            = db.Column(db.Float, default=0.0)
    original_contract_basis   = db.Column(db.String(30), nullable=True)
    original_rate_per_sqft    = db.Column(db.Float, default=0.0)
    original_discount_per_sqft= db.Column(db.Float, default=0.0)
    original_qty_sqft         = db.Column(db.Float, default=0.0)
    original_lump_sum_value   = db.Column(db.Float, default=0.0)
    created_at                = db.Column(db.DateTime, default=_pkt_now_naive)

    rate_history = db.relationship('StageRateHistory', backref='stage', lazy=True, cascade='all, delete-orphan')
    drawings = db.relationship('StageDrawing', backref='stage', lazy=True, cascade='all, delete-orphan')
    assigned_subcontractor = db.relationship('Subcontractor', foreign_keys=[assigned_subcontractor_id], post_update=True)

    @property
    def effective_rate(self):
        return (self.rate_per_sqft or 0) - (self.discount_per_sqft or 0)

    @property
    def contract_value(self):
        if self.contract_basis == 'Lump Sum':
            return self.lump_sum_value or 0
        if self.contract_basis == 'Per Sq Ft':
            return self.effective_rate * (self.qty_sqft or 0)
        return 0.0

    @property
    def original_value(self):
        if self.original_contract_basis == 'Lump Sum':
            return self.original_lump_sum_value or 0
        if self.original_contract_basis == 'Per Sq Ft':
            eff = (self.original_rate_per_sqft or 0) - (self.original_discount_per_sqft or 0)
            return eff * (self.original_qty_sqft or 0)
        return 0.0

    @property
    def stage_labour_cost(self):
        from hdc.models.workforce import Attendance, TimeEntry
        cached = getattr(self, '_agg_stage_labour_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        legacy = (db.session.query(func.coalesce(func.sum(Attendance.total_wage), 0.0))
                  .outerjoin(TimeEntry, TimeEntry.attendance_id == Attendance.id)
                  .filter(
                      Attendance.stage_id == self.id,
                      TimeEntry.id.is_(None)
                  )
                  .scalar() or 0.0)
        time_cost = sum(t.wage_calculated or 0 for t in TimeEntry.query.filter_by(stage_id=self.id, is_void=False).all())
        return legacy + time_cost

    @property
    def stage_expense_cost(self):
        from hdc.models.accounts import Expense
        cached = getattr(self, '_agg_stage_expense_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        return sum(e.amount for e in Expense.query.filter_by(stage_id=self.id, is_void=False).all())

    @property
    def stage_tip_expense(self):
        from hdc.models.accounts import Expense
        return sum(
            e.amount for e in Expense.query.filter_by(stage_id=self.id, is_void=False).all()
            if (e.category or '').strip().lower() == 'tip'
        )

    @property
    def stage_material_cost(self):
        from hdc.models.materials import Purchase, UsageLogV2
        cached = getattr(self, '_agg_stage_material_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        legacy_total = sum(
            p.total for p in Purchase.query.filter_by(stage_id=self.id).all()
        )
        v2_total = (db.session.query(func.coalesce(func.sum(UsageLogV2.cost), 0.0))
                    .filter(UsageLogV2.stage_id == self.id,
                            UsageLogV2.is_void == False)
                    .scalar() or 0.0)
        return float(legacy_total or 0.0) + float(v2_total or 0.0)

    @property
    def stage_subcontract_cost(self):
        from hdc.models.subcontract import Subcontractor
        cached = getattr(self, '_agg_stage_subcontract_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        # Expense-side stage subcontract cost should be cash-cleared, not contract commitment.
        return sum(float(s.total_cleared or 0.0) for s in Subcontractor.query.filter_by(stage_id=self.id).all())

    @property
    def stage_total_cost(self):
        cached = getattr(self, '_agg_stage_total_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        return (self.stage_labour_cost + self.stage_expense_cost +
                self.stage_material_cost + self.stage_subcontract_cost)

    @property
    def stage_profit(self):
        cached = getattr(self, '_agg_stage_profit', None)
        if cached is not None:
            return float(cached or 0.0)
        return self.contract_value - self.stage_total_cost

    @property
    def actual_cost(self):
        return self.stage_total_cost


class StageRateHistory(db.Model):
    __tablename__ = 'hdc_stage_rate_history'
    id                      = db.Column(db.Integer, primary_key=True)
    stage_id                = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=False)
    changed_at              = db.Column(db.Date, default=_pkt_today)
    old_contract_basis      = db.Column(db.String(30))
    new_contract_basis      = db.Column(db.String(30))
    old_rate_per_sqft       = db.Column(db.Float, default=0.0)
    new_rate_per_sqft       = db.Column(db.Float, default=0.0)
    old_discount_per_sqft   = db.Column(db.Float, default=0.0)
    new_discount_per_sqft   = db.Column(db.Float, default=0.0)
    old_qty_sqft            = db.Column(db.Float, default=0.0)
    new_qty_sqft            = db.Column(db.Float, default=0.0)
    old_lump_sum_value      = db.Column(db.Float, default=0.0)
    new_lump_sum_value      = db.Column(db.Float, default=0.0)
    reason                  = db.Column(db.Text)


class StageDrawing(db.Model):
    __tablename__ = 'hdc_stage_drawing'
    id            = db.Column(db.Integer, primary_key=True)
    stage_id      = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name   = db.Column(db.String(255), unique=True, nullable=False)
    created_at    = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at    = db.Column(db.DateTime, default=_pkt_now_naive)


class CustomFormula(db.Model):
    __tablename__ = 'hdc_custom_formula'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    category    = db.Column(db.String(50), default='Custom')
    expression  = db.Column(db.String(500), nullable=False)
    variables   = db.Column(db.String(500))
    description = db.Column(db.String(200))
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)
