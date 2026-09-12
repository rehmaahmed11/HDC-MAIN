"""HDC models.accounts — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive, _pkt_today

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
    __tablename__ = 'hdc_account'
    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(120), nullable=False)
    type            = db.Column(db.String(20), nullable=False)  # company / cash / bank / person / vendor / client
    opening_balance = db.Column(db.Float, default=0.0)
    bank_name       = db.Column(db.String(120))
    account_number  = db.Column(db.String(80))
    iban            = db.Column(db.String(80))
    auto_generated  = db.Column(db.Boolean, default=False)
    auto_source     = db.Column(db.String(40))
    status          = db.Column(db.String(20), default='active')
    is_void         = db.Column(db.Boolean, default=False)
    created_at      = db.Column(db.DateTime, default=_pkt_now_naive)


class AccountTransaction(db.Model):
    __tablename__ = 'hdc_account_txn'
    id                    = db.Column(db.Integer, primary_key=True)
    date                  = db.Column(db.Date, default=_pkt_today)
    amount                = db.Column(db.Float, default=0.0)
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

    from_account = db.relationship('Account', foreign_keys=[from_account_id], backref='outgoing_txns')
    to_account = db.relationship('Account', foreign_keys=[to_account_id], backref='incoming_txns')
    executed_by_account = db.relationship('Account', foreign_keys=[executed_by_account_id], backref='executed_txns')
    project = db.relationship('Project', backref='account_transactions')
    stage = db.relationship('Stage', backref='account_transactions')


class OwnerPayment(db.Model):
    __tablename__ = 'hdc_owner_payment'
    id         = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    received_to_account_id = db.Column(db.Integer, db.ForeignKey('hdc_account.id'), nullable=True)
    amount     = db.Column(db.Float, default=0.0)
    date       = db.Column(db.Date, default=_pkt_today)
    remarks    = db.Column(db.String(200))
    is_void    = db.Column(db.Boolean, default=False)
    void_reason= db.Column(db.String(250))
    voided_at  = db.Column(db.DateTime, nullable=True)
    activity_at= db.Column(db.DateTime, default=_pkt_now_naive)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    received_to_account = db.relationship('Account', foreign_keys=[received_to_account_id], backref='owner_receipts')
