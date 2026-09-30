"""HDC models.auth — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from flask_login import UserMixin

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive

# --- MODELS ----------------------------------------------------------------

class HDCUser(UserMixin, db.Model):
    __tablename__ = 'hdc_user'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role          = db.Column(db.String(20), default='admin')   # admin/manager/accountant
    # NULL permissions preserve the role-based access policy for upgraded users.
    # A JSON object means this account uses the explicit per-page permission map.
    permissions_json = db.Column(db.Text, nullable=True)
    stage_scope_enabled = db.Column(db.Boolean, default=False, nullable=False)
    allowed_stage_ids_json = db.Column(db.Text, nullable=True)  # readable stages
    write_stage_ids_json = db.Column(db.Text, nullable=True)
    # Optional exact-record allow-list; empty selections deny all business rows.
    # Read/Edit/Delete lists and Create flags are independent for each data type.
    record_scope_enabled = db.Column(db.Boolean, default=False, nullable=False)
    record_permissions_json = db.Column(db.Text, nullable=True)
    created_at    = db.Column(db.DateTime, default=_pkt_now_naive)


class UserActivity(db.Model):
    __tablename__ = 'hdc_user_activity'
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)
    username         = db.Column(db.String(80))
    event_type       = db.Column(db.String(20), nullable=False)  # create/update/delete/login/logout/system
    entity_type      = db.Column(db.String(80), nullable=False)
    entity_id        = db.Column(db.String(80))
    changed_fields   = db.Column(db.Text)
    summary          = db.Column(db.String(500))
    request_path     = db.Column(db.String(255))
    created_at       = db.Column(db.DateTime, default=_pkt_now_naive)

    user = db.relationship('HDCUser', backref='activity_logs')


class ActivityLog(db.Model):
    __tablename__ = 'hdc_activity_log'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('hdc_user.id'), nullable=True)
    username    = db.Column(db.String(80))
    action_type = db.Column(db.String(40), nullable=False)
    description = db.Column(db.Text, nullable=False)
    entity_type = db.Column(db.String(80), nullable=False)
    entity_id   = db.Column(db.String(80))
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)

    user = db.relationship('HDCUser', backref='clean_activity_logs')
