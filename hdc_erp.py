"""
Hadi Design and Construction Company â€” Construction ERP
Single-file Flask app with:
  Projects + Stage-based contracts, Workers + Labour Ledger,
  Attendance, Expenses, Materials + Purchases, Owner Payments,
  Subcontractors + Payments, Estimation Engine, Reports, User Management.
"""
import os, csv, io, ast, operator as op_module, json, calendar as pycal, shutil, zipfile, tempfile, secrets, threading
import sqlite3
from uuid import uuid4
from contextlib import contextmanager
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, Response, abort, send_file, session, has_request_context)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user,
                         login_required, logout_user, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import func, text, case, event, inspect as sa_inspect, and_, or_
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

# â”€â”€ App Setup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
BASE_DIR     = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__,
            template_folder='templates/hdc',
            static_folder='static/hdc',
            static_url_path='/hdc_static')

def _load_local_env():
    env_path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                key = k.strip()
                val = v.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as ex:
        print(f'[HDC ERP] Warning: failed to load .env: {ex}')

_load_local_env()

def _resolve_path(raw_path, default_path):
    val = (raw_path or '').strip()
    if not val:
        return os.path.abspath(default_path)
    val = os.path.expanduser(val)
    if not os.path.isabs(val):
        val = os.path.join(BASE_DIR, val)
    return os.path.abspath(val)

INSTANCE_DIR = _resolve_path(
    os.environ.get('HDC_INSTANCE_DIR'),
    os.path.join(BASE_DIR, 'hdc_instance')
)
os.makedirs(INSTANCE_DIR, exist_ok=True)
STAGE_DRAWINGS_DIR = os.path.join(INSTANCE_DIR, 'stage_drawings')
os.makedirs(STAGE_DRAWINGS_DIR, exist_ok=True)
DB_PATH = _resolve_path(
    os.environ.get('HDC_DB_PATH'),
    (os.path.join(INSTANCE_DIR, 'hdc_erp_integrated.db')
     if os.path.exists(os.path.join(INSTANCE_DIR, 'hdc_erp_integrated.db'))
     else os.path.join(INSTANCE_DIR, 'hdc_erp.db'))
)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

DEV_MODE = (str(os.environ.get('HDC_DEV_MODE', '')).strip().lower() in ('1', 'true', 'yes')
            or str(os.environ.get('FLASK_ENV', '')).strip().lower() == 'development')
# Keep session handling straightforward and predictable for local deployment.
app.secret_key = 'hdc_local_username_password_session_key'
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{DB_PATH}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'hdc_login'
login_manager.login_message_category = 'warning'

@event.listens_for(Engine, "connect")
def _sqlite_fast_pragmas(dbapi_connection, connection_record):
    try:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-20000")
        cur.close()
    except Exception:
        # Keep startup resilient even if pragma tuning fails.
        pass

PKT_ZONE = ZoneInfo('Asia/Karachi')

def _pkt_now():
    return datetime.now(PKT_ZONE)

def _pkt_now_naive():
    # Persist PKT wall-clock in DB (SQLite stores naive datetimes).
    return _pkt_now().replace(tzinfo=None)

def _pkt_today():
    return _pkt_now().date()

def _as_pkt(dt_value):
    if not dt_value:
        return None
    if isinstance(dt_value, datetime):
        if dt_value.tzinfo is None:
            return dt_value.replace(tzinfo=PKT_ZONE)
        return dt_value.astimezone(PKT_ZONE)
    return None

def _fmt_pkt(dt_value, fmt='%Y-%m-%d %H:%M:%S PKT'):
    pkt = _as_pkt(dt_value)
    return pkt.strftime(fmt) if pkt else ''


def _receipt_company_profile():
    name = (os.environ.get('HDC_COMPANY_NAME') or '').strip() or 'Hadi Design and Construction Company'
    tagline = (os.environ.get('HDC_COMPANY_TAGLINE') or '').strip() or 'Design | Build | Manage'
    address = (os.environ.get('HDC_COMPANY_ADDRESS') or '').strip() or 'Address not configured'
    phones_raw = (os.environ.get('HDC_COMPANY_PHONES') or '').strip()
    if not phones_raw:
        p1 = (os.environ.get('HDC_COMPANY_PHONE_1') or '').strip()
        p2 = (os.environ.get('HDC_COMPANY_PHONE_2') or '').strip()
        phones_raw = ', '.join([p for p in [p1, p2] if p]).strip()
    phones = [p.strip() for p in str(phones_raw).replace(';', ',').replace('|', ',').split(',') if p.strip()]
    if not phones:
        phones = ['Phone not configured']
    logo_url = (os.environ.get('HDC_COMPANY_LOGO_URL') or '').strip()
    if not logo_url:
        logo_url = (url_for('static', filename='img/company_logo.svg') if has_request_context() else '/hdc_static/img/company_logo.svg')
    return {
        'name': name,
        'tagline': tagline,
        'address': address,
        'phones': phones,
        'logo_url': logo_url,
    }


def _account_receipt_recent_entries(txn_row, limit=5):
    if not txn_row:
        return [], ''
    tx_dir = _account_tx_direction_for_type(txn_row.type)
    rel_type = _normalize_related_entity_type(txn_row.related_entity_type)
    rel_id = int(txn_row.related_entity_id or 0)
    party_name = _normalize_name_ci(txn_row.party_name or '')
    counter_account_id = 0
    if tx_dir == 'pay':
        counter_account_id = int(txn_row.to_account_id or 0)
    elif tx_dir == 'receive':
        counter_account_id = int(txn_row.from_account_id or 0)

    q = (AccountTransaction.query
         .filter(
             AccountTransaction.is_void == False,
             AccountTransaction.id != int(txn_row.id)
         ))
    scope_label = ''
    if rel_type and rel_id:
        q = q.filter(
            func.lower(func.coalesce(AccountTransaction.related_entity_type, '')) == rel_type,
            AccountTransaction.related_entity_id == rel_id
        )
        scope_label = (_account_entity_label(rel_type, rel_id) or f'{rel_type.title()} #{rel_id}')
    elif party_name:
        q = q.filter(func.lower(func.trim(func.coalesce(AccountTransaction.party_name, ''))) == party_name.lower())
        scope_label = party_name
    elif counter_account_id:
        q = q.filter(or_(
            and_(
                func.lower(func.coalesce(AccountTransaction.type, '')).in_(_ACCOUNT_TXN_RECEIVE_TYPES),
                AccountTransaction.from_account_id == counter_account_id
            ),
            and_(
                func.lower(func.coalesce(AccountTransaction.type, '')).in_(_ACCOUNT_TXN_PAY_TYPES),
                AccountTransaction.to_account_id == counter_account_id
            ),
            and_(
                func.lower(func.coalesce(AccountTransaction.type, '')) == 'transfer',
                or_(
                    AccountTransaction.from_account_id == counter_account_id,
                    AccountTransaction.to_account_id == counter_account_id
                )
            )
        ))
        acc = Account.query.get(counter_account_id)
        scope_label = (acc.name if acc else f'Account #{counter_account_id}')
    else:
        return [], ''

    rows = (q.order_by(AccountTransaction.date.desc(), AccountTransaction.id.desc())
            .limit(max(1, min(int(limit or 5), 20)))
            .all())
    out = []
    for r in rows:
        party = (r.party_name or '').strip() or (r.to_account.name if r.to_account else '-')
        out.append({
            'date': (r.date.isoformat() if r.date else ''),
            'type': str(r.type or '').replace('_', ' ').title(),
            'direction': _account_tx_direction_for_type(r.type),
            'party': party,
            'amount': float(r.amount or 0.0),
            'receipt_url': url_for('hdc_account_transaction_receipt', txn_id=r.id),
        })
    return out, scope_label


def _owner_payment_recent_entries(project_id, exclude_id=None, limit=5):
    if not project_id:
        return []
    q = (OwnerPayment.query
         .filter(
             OwnerPayment.project_id == int(project_id),
             OwnerPayment.is_void == False
         ))
    if exclude_id:
        q = q.filter(OwnerPayment.id != int(exclude_id))
    rows = q.order_by(OwnerPayment.date.desc(), OwnerPayment.id.desc()).limit(max(1, min(int(limit or 5), 20))).all()
    out = []
    for r in rows:
        out.append({
            'date': (r.date.isoformat() if r.date else ''),
            'type': 'Owner Payment Receipt',
            'direction': 'receive',
            'party': (r.project.client if r.project else 'Client'),
            'amount': float(r.amount or 0.0),
            'receipt_url': url_for('hdc_owner_payment_receipt', pid=int(project_id), oid=r.id),
        })
    return out


def _num_to_words_en(n):
    n = int(max(0, n or 0))
    under_20 = [
        'zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
        'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
        'seventeen', 'eighteen', 'nineteen'
    ]
    tens = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']
    scales = [(10**9, 'billion'), (10**6, 'million'), (10**3, 'thousand'), (100, 'hundred')]

    if n < 20:
        return under_20[n]
    if n < 100:
        return tens[n // 10] + ('' if (n % 10 == 0) else f"-{under_20[n % 10]}")
    for scale_val, scale_name in scales:
        if n >= scale_val:
            left = n // scale_val
            rem = n % scale_val
            return _num_to_words_en(left) + f" {scale_name}" + ('' if rem == 0 else f" {_num_to_words_en(rem)}")
    return str(n)


def _amount_to_words(amount):
    val = float(amount or 0.0)
    whole = int(abs(val))
    frac = int(round((abs(val) - whole) * 100))
    words = _num_to_words_en(whole) + ' rupees'
    if frac > 0:
        words += f' and {_num_to_words_en(frac)} paisa'
    if val < 0:
        words = 'minus ' + words
    return words + ' only'

# Make formatter available in all templates unconditionally.
app.jinja_env.globals['fmt_pkt'] = _fmt_pkt
app.jinja_env.filters['fmt_pkt'] = _fmt_pkt

def _activity_at_for(event_date):
    base_date = event_date if isinstance(event_date, date) else _pkt_today()
    now = _pkt_now()
    return datetime.combine(base_date, now.time().replace(microsecond=0))


def _csrf_token():
    tok = session.get('_csrf_token')
    if not tok:
        tok = secrets.token_urlsafe(32)
        session['_csrf_token'] = tok
    return tok


def _is_strong_password(pwd):
    pwd = str(pwd or '')
    if len(pwd) < 8:
        return False, 'Password must be at least 8 characters.'
    if not any(ch.islower() for ch in pwd):
        return False, 'Password must include a lowercase letter.'
    if not any(ch.isupper() for ch in pwd):
        return False, 'Password must include an uppercase letter.'
    if not any(ch.isdigit() for ch in pwd):
        return False, 'Password must include a number.'
    if not any((not ch.isalnum()) for ch in pwd):
        return False, 'Password must include a special character.'
    return True, ''

@app.context_processor
def _inject_alert_count():
    try:
        count = Alert.query.filter_by(resolved=False).count()
    except Exception:
        count = 0
    return dict(alert_count=count, fmt_pkt=_fmt_pkt, csrf_token=_csrf_token())


@app.before_request
def _ensure_db_runtime_ready():
    # Self-heal for long-running WSGI workers: if DB file is deleted after startup,
    # recreate a fresh schema on the next request.
    if os.path.exists(_DB_STORE):
        try:
            con = sqlite3.connect(_DB_STORE)
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='hdc_user'")
            ok = cur.fetchone() is not None
            cur.close()
            con.close()
            if ok:
                return None
            _ensure_bootstrap_once(force=True)
            return None
        except Exception as ex:
            app.logger.error('Runtime DB table self-heal failed: %s', ex)
            abort(500, description='Database schema check failed and auto-recovery failed.')
    try:
        _ensure_bootstrap_once(force=True)
    except Exception as ex:
        app.logger.error('Runtime DB self-heal failed: %s', ex)
        abort(500, description='Database is missing and auto-recovery failed.')
    return None


@app.before_request
def _csrf_protect():
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return None
    ep = (request.endpoint or '').strip()
    if ep in ('static',):
        return None
    # JSON endpoints may use token header from JS callers; do not hard-block legacy JSON posts here.
    if request.is_json:
        return None
    sent = (request.form.get('_csrf_token') or request.headers.get('X-CSRFToken') or '').strip()
    tok = (session.get('_csrf_token') or '').strip()
    if (not sent) or (not tok) or (sent != tok):
        abort(400, description='CSRF token missing or invalid.')
    return None


# â”€â”€ MODELS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class HDCUser(UserMixin, db.Model):
    __tablename__ = 'hdc_user'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role          = db.Column(db.String(20), default='admin')   # admin/manager/accountant
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
        return sum(e.amount for e in self.expenses)

    @property
    def total_tip_expense(self):
        return sum(
            e.amount for e in self.expenses
            if (e.category or '').strip().lower() == 'tip'
        )

    @property
    def total_expenses(self):        # alias for templates
        return self.total_expense_cost

    @property
    def total_labour_cost(self):
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
        cached = getattr(self, '_agg_total_material_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        purchase_total = sum(p.total for p in self.purchases)
        return purchase_total

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
        cached = getattr(self, '_agg_stage_expense_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        return sum(e.amount for e in Expense.query.filter_by(stage_id=self.id, is_void=False).all())

    @property
    def stage_tip_expense(self):
        return sum(
            e.amount for e in Expense.query.filter_by(stage_id=self.id, is_void=False).all()
            if (e.category or '').strip().lower() == 'tip'
        )

    @property
    def stage_material_cost(self):
        cached = getattr(self, '_agg_stage_material_cost', None)
        if cached is not None:
            return float(cached or 0.0)
        purchase_total = sum(p.total for p in Purchase.query.filter_by(stage_id=self.id).all())
        return purchase_total

    @property
    def stage_subcontract_cost(self):
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


class WorkerTrade(db.Model):
    __tablename__ = 'hdc_worker_trade'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(80), unique=True, nullable=False)
    active_status = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=_pkt_now_naive)


class ExpenseCategory(db.Model):
    __tablename__ = 'hdc_expense_category'
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

class StageDrawing(db.Model):
    __tablename__ = 'hdc_stage_drawing'
    id            = db.Column(db.Integer, primary_key=True)
    stage_id      = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name   = db.Column(db.String(255), unique=True, nullable=False)
    created_at    = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at    = db.Column(db.DateTime, default=_pkt_now_naive)

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


class MaterialUsage(db.Model):
    __tablename__ = 'hdc_material_usage'
    id         = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    stage_id   = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    material_id= db.Column(db.Integer, db.ForeignKey('hdc_material.id'), nullable=False)
    qty        = db.Column(db.Float, default=0.0)
    rate       = db.Column(db.Float, default=0.0)
    total      = db.Column(db.Float, default=0.0)
    used_at    = db.Column(db.Date, default=_pkt_today)
    activity_at= db.Column(db.DateTime, default=_pkt_now_naive)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    project  = db.relationship('Project', backref='material_usages')
    stage    = db.relationship('Stage', backref='material_usages')
    material = db.relationship('Material', backref='usages')


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


class Supplier(db.Model):
    __tablename__ = 'hdc_supplier'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    phone      = db.Column(db.String(30))
    status     = db.Column(db.String(20), default='active')
    is_void    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive)


class MaterialV2(db.Model):
    __tablename__ = 'hdc_material_v2'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    unit       = db.Column(db.String(20), default='KG')
    status     = db.Column(db.String(20), default='active')
    is_void    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive)


class PurchaseV2(db.Model):
    __tablename__ = 'hdc_purchase_v2'
    id             = db.Column(db.Integer, primary_key=True)
    supplier_id    = db.Column(db.Integer, db.ForeignKey('hdc_supplier.id'), nullable=False)
    material_id    = db.Column(db.Integer, db.ForeignKey('hdc_material_v2.id'), nullable=False)
    unit_price     = db.Column(db.Float, default=0.0)
    quantity       = db.Column(db.Float, default=0.0)
    total_amount   = db.Column(db.Float, default=0.0)
    payment_status = db.Column(db.String(20), default='unpaid')
    date           = db.Column(db.Date, default=_pkt_today)
    notes          = db.Column(db.String(300))
    challan_no     = db.Column(db.String(80))
    is_void        = db.Column(db.Boolean, default=False)
    void_reason    = db.Column(db.String(250))
    voided_at      = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    supplier = db.relationship('Supplier', backref='purchases_v2')
    material = db.relationship('MaterialV2', backref='purchases_v2')


class SupplierLedger(db.Model):
    __tablename__ = 'hdc_supplier_ledger'
    id             = db.Column(db.Integer, primary_key=True)
    supplier_id    = db.Column(db.Integer, db.ForeignKey('hdc_supplier.id'), nullable=False)
    entry_type     = db.Column(db.String(20), nullable=False)  # debit / credit
    amount         = db.Column(db.Float, default=0.0)
    reference_type = db.Column(db.String(50))
    reference_id   = db.Column(db.Integer)
    note           = db.Column(db.String(300))
    is_void        = db.Column(db.Boolean, default=False)
    void_reason    = db.Column(db.String(250))
    voided_at      = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    supplier = db.relationship('Supplier', backref='ledger_rows')


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


class Delivery(db.Model):
    __tablename__ = 'hdc_delivery'
    id             = db.Column(db.Integer, primary_key=True)
    purchase_id    = db.Column(db.Integer, db.ForeignKey('hdc_purchase_v2.id'), nullable=False)
    material_id    = db.Column(db.Integer, db.ForeignKey('hdc_material_v2.id'), nullable=False)
    project_id     = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    stage_id       = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    quantity       = db.Column(db.Float, default=0.0)
    date           = db.Column(db.Date, default=_pkt_today)
    notes          = db.Column(db.String(300))
    delivery_person= db.Column(db.String(120))
    is_void        = db.Column(db.Boolean, default=False)
    void_reason    = db.Column(db.String(250))
    voided_at      = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    purchase = db.relationship('PurchaseV2', backref='deliveries')
    material = db.relationship('MaterialV2', backref='deliveries')
    project  = db.relationship('Project', backref='deliveries_v2')
    stage    = db.relationship('Stage', backref='deliveries_v2')


class UsageLogV2(db.Model):
    __tablename__ = 'hdc_usage_log_v2'
    id         = db.Column(db.Integer, primary_key=True)
    purchase_id= db.Column(db.Integer, db.ForeignKey('hdc_purchase_v2.id'), nullable=True)
    material_id= db.Column(db.Integer, db.ForeignKey('hdc_material_v2.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    stage_id   = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    quantity   = db.Column(db.Float, default=0.0)
    cost       = db.Column(db.Float, default=0.0)
    date       = db.Column(db.Date, default=_pkt_today)
    notes      = db.Column(db.String(300))
    is_void    = db.Column(db.Boolean, default=False)
    void_reason= db.Column(db.String(250))
    voided_at  = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    purchase = db.relationship('PurchaseV2', backref='usage_logs_v2')
    material = db.relationship('MaterialV2', backref='usage_logs_v2')
    project  = db.relationship('Project', backref='usage_logs_v2')
    stage    = db.relationship('Stage', backref='usage_logs_v2')


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


class Material(db.Model):
    __tablename__ = 'hdc_material'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    unit       = db.Column(db.String(30), nullable=False, default='Nos')
    is_active  = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    purchases  = db.relationship('Purchase', backref='material', lazy=True)


class Purchase(db.Model):
    __tablename__ = 'hdc_purchase'
    id          = db.Column(db.Integer, primary_key=True)
    project_id  = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    stage_id    = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    material_id = db.Column(db.Integer, db.ForeignKey('hdc_material.id'), nullable=False)
    stage       = db.relationship('Stage', backref='purchase_records')
    entry_type  = db.Column(db.String(20), default='purchase')  # purchase / return
    date        = db.Column(db.Date, default=_pkt_today)
    qty         = db.Column(db.Float, default=0.0)
    rate        = db.Column(db.Float, default=0.0)
    total       = db.Column(db.Float, default=0.0)
    supplier_name = db.Column(db.String(120))
    return_ref  = db.Column(db.String(80))
    return_reason = db.Column(db.String(200))
    approved_by = db.Column(db.String(100))
    notes       = db.Column(db.String(200))
    activity_at = db.Column(db.DateTime, default=_pkt_now_naive)
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)


class CustomFormula(db.Model):
    __tablename__ = 'hdc_custom_formula'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    category    = db.Column(db.String(50), default='Custom')
    expression  = db.Column(db.String(500), nullable=False)
    variables   = db.Column(db.String(500))
    description = db.Column(db.String(200))
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)


# â”€â”€ Safe Math Evaluator â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_OPS = {
    ast.Add: op_module.add, ast.Sub: op_module.sub,
    ast.Mult: op_module.mul, ast.Div: op_module.truediv,
    ast.Pow: op_module.pow, ast.USub: op_module.neg
}

def _safe_eval(expr, variables=None):
    variables = variables or {}
    tree = ast.parse(expr, mode='eval')
    def _ev(n):
        if isinstance(n, ast.Constant): return n.value
        if isinstance(n, ast.Name):
            if n.id not in variables: raise ValueError(f"Unknown variable: {n.id}")
            return float(variables[n.id])
        if isinstance(n, ast.BinOp):
            op = _OPS.get(type(n.op))
            if not op: raise ValueError("Unsupported operator")
            return op(_ev(n.left), _ev(n.right))
        if isinstance(n, ast.UnaryOp):
            op = _OPS.get(type(n.op))
            if not op: raise ValueError("Unsupported unary operator")
            return op(_ev(n.operand))
        if isinstance(n, ast.Expression): return _ev(n.body)
        raise ValueError(f"Unsupported: {type(n).__name__}")
    return _ev(tree)


# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_DATE_FALLBACK_DEFAULT = object()

def _parse_date(s, fallback=_DATE_FALLBACK_DEFAULT):
    fallback_val = _pkt_today() if fallback is _DATE_FALLBACK_DEFAULT else fallback
    if not s:
        return fallback_val
    try:
        return datetime.strptime(s.strip(), '%Y-%m-%d').date()
    except Exception:
        return fallback_val

def _flt(v, default=0.0):
    try:
        if v is None:
            return float(default)
        if isinstance(v, (int, float)):
            return float(v)
        raw = str(v).strip()
        if not raw:
            return float(default)
        # Accept common human input like "2,500" for numeric fields.
        raw = raw.replace(',', '')
        return float(raw)
    except Exception:
        return default

_AUDIT_EXCLUDE_TABLES = {
    'hdc_user_activity',
    'hdc_runtime_flag'
}
_AUDIT_INTERNAL_TABLES = {
    'hdc_attendance_day',
    'hdc_labour_ledger'
}
_AUDIT_NOISY_UPDATE_FIELDS = {
    'hdc_time_entry': {'attendance_id', 'check_in', 'check_out', 'overtime', 'wage_calculated', 'activity_at'}
}

@contextmanager
def _audit_paused():
    depth = int(db.session.info.get('_audit_pause_depth', 0) or 0)
    db.session.info['_audit_pause_depth'] = depth + 1
    db.session.info['_audit_paused'] = True
    try:
        yield
    finally:
        new_depth = max(0, int(db.session.info.get('_audit_pause_depth', 1)) - 1)
        db.session.info['_audit_pause_depth'] = new_depth
        if new_depth == 0:
            db.session.info.pop('_audit_paused', None)

def _audit_actor_snapshot():
    if has_request_context() and getattr(current_user, 'is_authenticated', False):
        try:
            return int(current_user.id), str(current_user.username or '')
        except Exception:
            return None, 'system'
    return None, 'system'

def _audit_path():
    if has_request_context():
        try:
            return str(request.path or '')
        except Exception:
            return ''
    return ''

def _audit_repr(v):
    if isinstance(v, (datetime, date)):
        txt = v.isoformat(sep=' ') if isinstance(v, datetime) else v.isoformat()
    elif v is None:
        txt = ''
    else:
        txt = str(v)
    txt = txt.strip()
    if len(txt) > 80:
        txt = txt[:77] + '...'
    return txt

def _audit_entity_id(obj):
    val = getattr(obj, 'id', None)
    if val is None:
        return ''
    return str(val)

def _audit_entity_type(obj):
    return str(getattr(obj, '__tablename__', obj.__class__.__name__) or obj.__class__.__name__)

def _audit_change_map(obj):
    changed = {}
    try:
        insp = sa_inspect(obj)
        for attr in insp.mapper.column_attrs:
            key = attr.key
            hist = insp.attrs[key].history
            if not hist.has_changes():
                continue
            old_val = hist.deleted[0] if hist.deleted else None
            new_val = hist.added[0] if hist.added else getattr(obj, key, None)
            changed[key] = {'old': _audit_repr(old_val), 'new': _audit_repr(new_val)}
    except Exception:
        return {}
    return changed

def _audit_summary(event_type, entity_type, entity_id, changed=None):
    prefix = f"{event_type.title()} {entity_type}"
    if entity_id:
        prefix += f" #{entity_id}"
    changed = changed or {}
    if not changed:
        return prefix
    keys = list(changed.keys())[:6]
    suffix = ', '.join(keys)
    if len(changed.keys()) > 6:
        suffix += ', ...'
    return f"{prefix} ({suffix})"

def _audit_name_by_id(model_cls, obj_id, fallback='-'):
    try:
        if obj_id in (None, ''):
            return fallback
        rid = int(obj_id)
        row = db.session.get(model_cls, rid)
        if not row:
            return fallback
        return str(getattr(row, 'name', None) or getattr(row, 'username', None) or f'#{rid}')
    except Exception:
        return fallback

def _audit_humanize_timeentry(obj, event_type, changed):
    worker_name = _audit_name_by_id(Worker, getattr(obj, 'worker_id', None), fallback=f"Worker #{getattr(obj, 'worker_id', '')}")
    project_name = _audit_name_by_id(Project, getattr(obj, 'project_id', None), fallback='-')
    stage_name = _audit_name_by_id(Stage, getattr(obj, 'stage_id', None), fallback='-')
    hours = _flt(getattr(obj, 'hours', 0.0), 0.0)
    ot = _flt(getattr(obj, 'overtime', 0.0), 0.0)
    work_date = ''
    try:
        work_date = obj.check_in.date().isoformat() if getattr(obj, 'check_in', None) else ''
    except Exception:
        work_date = ''

    def _fmt_hours(v):
        try:
            return f"{float(_flt(v, 0.0)):.2f} hrs"
        except Exception:
            return f"{v}"

    actor = 'User'
    try:
        if has_request_context() and getattr(current_user, 'is_authenticated', False):
            actor = str(current_user.username or 'User').title()
    except Exception:
        pass

    if event_type == 'create':
        payload = {
            'worker': worker_name,
            'project': project_name,
            'stage': stage_name,
            'hours': f'{hours:.2f}',
            'overtime': f'{ot:.2f}',
            'date': work_date
        }
        summary = f"{actor} created attendance for {worker_name}: {project_name} -> {stage_name}, {_fmt_hours(hours)}"
        if ot > 0:
            summary += f" (OT {_fmt_hours(ot)})"
        if work_date:
            summary += f" on {work_date}"
        return summary, payload

    if event_type == 'update':
        parts = []
        if 'project_id' in changed:
            old_id = changed['project_id'].get('old')
            new_id = changed['project_id'].get('new')
            parts.append(f"project {_audit_name_by_id(Project, old_id, '-')} -> {_audit_name_by_id(Project, new_id, '-')}")
        if 'stage_id' in changed:
            old_id = changed['stage_id'].get('old')
            new_id = changed['stage_id'].get('new')
            parts.append(f"stage {_audit_name_by_id(Stage, old_id, '-')} -> {_audit_name_by_id(Stage, new_id, '-')}")
        if 'hours' in changed:
            parts.append(f"hours {_fmt_hours(changed['hours'].get('old', '0'))} -> {_fmt_hours(changed['hours'].get('new', '0'))}")
        if 'overtime' in changed:
            parts.append(f"overtime {_fmt_hours(changed['overtime'].get('old', '0'))} -> {_fmt_hours(changed['overtime'].get('new', '0'))}")
        if 'is_void' in changed:
            parts.append(f"status {'Voided' if str(changed['is_void'].get('new', '')).strip() in ('1', 'true', 'True') else 'Active'}")
        if not parts:
            parts.append('auto sync update')
        summary = f"{actor} updated attendance of {worker_name}: " + '; '.join(parts)
        if work_date:
            summary += f" | date {work_date}"
        payload = {'worker': worker_name, 'date': work_date, 'changes': changed}
        return summary, payload

    return _audit_summary(event_type, 'hdc_time_entry', _audit_entity_id(obj), changed), changed

def _audit_humanize_attendance_mark(obj, event_type, changed):
    worker_name = _audit_name_by_id(Worker, getattr(obj, 'worker_id', None), fallback=f"Worker #{getattr(obj, 'worker_id', '')}")
    status = str(getattr(obj, 'status', '') or '').strip().title() or '-'
    mark_date = ''
    try:
        mark_date = obj.date.isoformat() if getattr(obj, 'date', None) else ''
    except Exception:
        mark_date = ''

    if event_type == 'create':
        payload = {'worker': worker_name, 'status': status, 'date': mark_date}
        return f"Attendance status set: {worker_name} = {status}" + (f" on {mark_date}" if mark_date else ''), payload

    if event_type == 'update':
        old_status = (changed.get('status') or {}).get('old', '')
        new_status = (changed.get('status') or {}).get('new', '')
        if old_status or new_status:
            txt = f"Attendance status changed: {worker_name} {str(old_status).title() or '-'} -> {str(new_status).title() or '-'}"
        else:
            txt = f"Attendance mark updated: {worker_name}"
        if mark_date:
            txt += f" on {mark_date}"
        return txt, {'worker': worker_name, 'date': mark_date, 'changes': changed}

    return _audit_summary(event_type, 'hdc_attendance_mark', _audit_entity_id(obj), changed), changed

def _audit_custom_summary_payload(obj, event_type, changed):
    entity_type = _audit_entity_type(obj)
    if entity_type == 'hdc_time_entry':
        return _audit_humanize_timeentry(obj, event_type, changed or {})
    if entity_type == 'hdc_attendance_mark':
        return _audit_humanize_attendance_mark(obj, event_type, changed or {})
    return _audit_summary(event_type, entity_type, _audit_entity_id(obj), changed), changed

def _audit_is_noise_event(event_type, entity_type, changed):
    if entity_type in _AUDIT_INTERNAL_TABLES:
        return True
    if event_type != 'update':
        return False
    keys = set((changed or {}).keys())
    if not keys:
        return True
    noisy_fields = _AUDIT_NOISY_UPDATE_FIELDS.get(entity_type, set())
    if noisy_fields and keys.issubset(noisy_fields):
        return True
    return False

def _record_user_activity(event_type, entity_type, entity_id='', summary='', changed=None, force_commit=False):
    if db.session.info.get('_audit_paused'):
        return None
    try:
        uid, uname = _audit_actor_snapshot()
        row = UserActivity(
            user_id=uid,
            username=uname or 'system',
            event_type=str(event_type or 'system')[:20],
            entity_type=str(entity_type or 'unknown')[:80],
            entity_id=str(entity_id or '')[:80],
            changed_fields=json.dumps(changed or {}, ensure_ascii=False),
            summary=(summary or _audit_summary(event_type, entity_type, entity_id, changed))[:500],
            request_path=_audit_path()[:255]
        )
        db.session.info['_audit_skip'] = True
        db.session.add(row)
        if force_commit:
            db.session.commit()
        return row
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return None
    finally:
        db.session.info.pop('_audit_skip', None)

@event.listens_for(db.session.__class__, 'after_flush')
def _capture_user_activity_after_flush(session_obj, flush_context):
    if session_obj.info.get('_audit_paused') or session_obj.info.get('_audit_skip') or session_obj.info.get('_audit_reentry'):
        return
    uid, uname = _audit_actor_snapshot()
    path = _audit_path()
    rows = []

    def _track(obj, event_type):
        entity_type = _audit_entity_type(obj)
        if entity_type in _AUDIT_EXCLUDE_TABLES:
            return
        entity_id = _audit_entity_id(obj)
        changed = {}
        if event_type == 'update':
            changed = _audit_change_map(obj)
            if not changed:
                return
        if _audit_is_noise_event(event_type, entity_type, changed):
            return
        summary_txt, payload = _audit_custom_summary_payload(obj, event_type, changed)
        row = UserActivity(
            user_id=uid,
            username=(uname or 'system')[:80],
            event_type=event_type,
            entity_type=entity_type[:80],
            entity_id=entity_id[:80],
            changed_fields=json.dumps(payload or changed, ensure_ascii=False),
            summary=(summary_txt or _audit_summary(event_type, entity_type, entity_id, changed))[:500],
            request_path=path[:255]
        )
        rows.append(row)

    try:
        for obj in list(session_obj.new):
            _track(obj, 'create')
        for obj in list(session_obj.dirty):
            if obj in session_obj.new or obj in session_obj.deleted:
                continue
            try:
                if not session_obj.is_modified(obj, include_collections=False):
                    continue
            except Exception:
                pass
            _track(obj, 'update')
        for obj in list(session_obj.deleted):
            _track(obj, 'delete')

        if rows:
            session_obj.info['_audit_reentry'] = True
            for row in rows:
                session_obj.add(row)
    finally:
        session_obj.info.pop('_audit_reentry', None)

def _next_subcontractor_code():
    max_n = 0
    rows = db.session.query(Subcontractor.subcontractor_code).all()
    for (code,) in rows:
        if not code:
            continue
        code = str(code).strip().upper()
        if not code.startswith('SUB-'):
            continue
        try:
            n = int(code.split('-', 1)[1])
            if n > max_n:
                max_n = n
        except Exception:
            continue
    return f'SUB-{max_n + 1:04d}'

def _log_subcontract_event(sub, event_type, from_value='', to_value='', amount=0.0, notes='', project_id=None, stage_id=None):
    if not sub:
        return
    actor_id = None
    try:
        if current_user and getattr(current_user, 'is_authenticated', False):
            actor_id = current_user.id
    except Exception:
        actor_id = None
    db.session.add(SubcontractEvent(
        subcontractor_id=sub.id,
        project_id=project_id if project_id is not None else sub.project_id,
        stage_id=stage_id if stage_id is not None else sub.stage_id,
        actor_user_id=actor_id,
        event_type=(event_type or '').strip()[:40] or 'update',
        from_value=(from_value or '')[:250],
        to_value=(to_value or '')[:250],
        amount=float(amount or 0.0),
        notes=(notes or '')[:300],
        created_at=_pkt_now_naive()
    ))

def _ensure_subcontract_baseline_events(sub):
    if not sub:
        return 0
    created = 0
    has_create = (SubcontractEvent.query
                  .filter(SubcontractEvent.subcontractor_id == sub.id, SubcontractEvent.event_type == 'create')
                  .first())
    if not has_create:
        _log_subcontract_event(
            sub=sub,
            event_type='create',
            to_value=(sub.subcontractor_code or ''),
            notes='Backfill: subcontractor profile',
            project_id=sub.project_id,
            stage_id=sub.stage_id
        )
        created += 1
    if sub.stage_id and sub.project_id:
        has_shift = (SubcontractEvent.query
                     .filter(SubcontractEvent.subcontractor_id == sub.id,
                             SubcontractEvent.event_type.in_(['shift', 'reassign']),
                             SubcontractEvent.stage_id == sub.stage_id)
                     .first())
        if not has_shift:
            _log_subcontract_event(
                sub=sub,
                event_type='shift',
                from_value='backfill',
                to_value=(sub.stage_rel.name if sub.stage_rel else f'Stage#{sub.stage_id}'),
                notes='Backfill: stage assignment',
                project_id=sub.project_id,
                stage_id=sub.stage_id
            )
            created += 1
        has_price = (SubcontractEvent.query
                     .filter(SubcontractEvent.subcontractor_id == sub.id,
                             SubcontractEvent.event_type == 'price_update',
                             SubcontractEvent.stage_id == sub.stage_id)
                     .first())
        if not has_price:
            _log_subcontract_event(
                sub=sub,
                event_type='price_update',
                to_value=(sub.contract_type or ''),
                amount=float(sub.contract_value or 0.0),
                notes=f'Backfill: Rate {float(sub.rate_per_sqft or 0):.2f} | Sqft {float(sub.total_sqft or 0):.2f} | Lump {float(sub.lump_sum_amount or 0):.2f}',
                project_id=sub.project_id,
                stage_id=sub.stage_id
            )
            created += 1
    return created


def _extract_pct(text_value):
    try:
        return float(str(text_value or '').replace('%', '').strip() or 0.0)
    except Exception:
        return 0.0


def _subcontract_scope_stages(sub):
    stage_ids = set()
    if sub.stage_id:
        stage_ids.add(int(sub.stage_id))
    ev_rows = (SubcontractEvent.query
               .filter(SubcontractEvent.subcontractor_id == sub.id, SubcontractEvent.stage_id.isnot(None))
               .all())
    for r in ev_rows:
        if r.stage_id:
            stage_ids.add(int(r.stage_id))
    if not stage_ids:
        return []
    return Stage.query.filter(Stage.id.in_(list(stage_ids))).order_by(Stage.project_id.asc(), Stage.id.asc()).all()


def _subcontract_stage_snapshot(sub, stg):
    if (not sub) or (not stg):
        return {
            'contract': 0.0, 'progress': 0.0, 'payable': 0.0, 'paid': 0.0, 'balance': 0.0,
            'contract_balance': 0.0, 'live_balance': 0.0, 'advance_paid': 0.0
        }

    contract_val = 0.0
    if sub.stage_id == stg.id:
        contract_val = float(sub.contract_value or 0.0)
    if contract_val <= 0:
        pe = (SubcontractEvent.query
              .filter(SubcontractEvent.subcontractor_id == sub.id,
                      SubcontractEvent.stage_id == stg.id,
                      func.lower(SubcontractEvent.event_type) == 'price_update')
              .order_by(SubcontractEvent.created_at.desc(), SubcontractEvent.id.desc())
              .first())
        if pe and float(pe.amount or 0.0) > 0:
            contract_val = float(pe.amount or 0.0)
    if contract_val <= 0:
        contract_val = float(stg.contract_value or 0.0)

    progress = 0.0
    if sub.stage_id == stg.id:
        progress = float(sub.effective_progress_percentage or 0.0)
    if progress <= 0:
        pge = (SubcontractEvent.query
               .filter(SubcontractEvent.subcontractor_id == sub.id,
                       SubcontractEvent.stage_id == stg.id,
                       func.lower(SubcontractEvent.event_type) == 'progress')
               .order_by(SubcontractEvent.created_at.desc(), SubcontractEvent.id.desc())
               .first())
        if pge:
            progress = max(0.0, min(100.0, _extract_pct(pge.to_value)))
    if progress <= 0:
        progress = max(0.0, min(100.0, float(stg.progress or 0.0)))
    if progress <= 0 and (stg.status or '').strip().lower() in ('completed', 'complete'):
        progress = 100.0

    retention_pct = max(0.0, min(100.0, float(sub.retention_percentage or 0.0)))
    gross_payable = contract_val * progress / 100.0
    cap_payable = max(0.0, contract_val * (100.0 - retention_pct) / 100.0)
    payable = max(0.0, min(gross_payable, cap_payable))
    paid = float(db.session.query(func.coalesce(func.sum(SubcontractPayment.amount), 0.0))
                 .filter(
                     SubcontractPayment.is_void == False,
                     SubcontractPayment.subcontractor_id == sub.id,
                     SubcontractPayment.stage_id == stg.id,
                     func.lower(func.coalesce(SubcontractPayment.entry_type, 'payment')) != 'settlement'
                 )
                 .scalar() or 0.0)
    settled = float(db.session.query(func.coalesce(func.sum(SubcontractPayment.amount), 0.0))
                    .filter(
                        SubcontractPayment.is_void == False,
                        SubcontractPayment.subcontractor_id == sub.id,
                        SubcontractPayment.stage_id == stg.id,
                        func.lower(func.coalesce(SubcontractPayment.entry_type, 'payment')) == 'settlement'
                    )
                    .scalar() or 0.0)
    cleared = paid + settled
    live_balance = payable - cleared
    balance = max(0.0, payable - cleared)
    contract_balance = max(0.0, contract_val - cleared)
    return {
        'contract': contract_val,
        'progress': progress,
        'payable': payable,
        'paid': paid,
        'settled': settled,
        'cleared': cleared,
        'balance': balance,
        'contract_balance': contract_balance,
        'live_balance': live_balance,
        'advance_paid': max(0.0, -live_balance)
    }

def _reconcile_subcontract_links():
    """Keep stage execution pointers and subcontract pointers in sync."""
    changed = 0
    stages = Stage.query.all()
    for stg in stages:
        mode = (stg.execution_mode or 'company').strip().lower()
        if mode not in ('company', 'subcontractor'):
            stg.execution_mode = 'company'
            changed += 1
            mode = 'company'
        if mode == 'subcontractor':
            if not stg.assigned_subcontractor_id:
                stg.execution_mode = 'company'
                changed += 1
                continue
            sub = Subcontractor.query.get(stg.assigned_subcontractor_id)
            if not sub:
                stg.execution_mode = 'company'
                stg.assigned_subcontractor_id = None
                changed += 1
                continue
            if sub.stage_id != stg.id:
                sub.stage_id = stg.id
                changed += 1
            if sub.project_id != stg.project_id:
                sub.project_id = stg.project_id
                changed += 1
        else:
            if stg.assigned_subcontractor_id:
                stg.assigned_subcontractor_id = None
                changed += 1
    for sub in Subcontractor.query.all():
        if not sub.stage_id:
            continue
        stg = Stage.query.get(sub.stage_id)
        if (not stg) or (stg.assigned_subcontractor_id != sub.id) or ((stg.execution_mode or 'company').strip().lower() != 'subcontractor'):
            sub.stage_id = None
            changed += 1
    if changed:
        db.session.commit()
    return changed

def _parse_attendance_entries_payload(raw_payload):
    try:
        rows = json.loads(raw_payload or '[]')
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get('project_id') or 0)
            sid = int(row.get('stage_id') or 0)
        except Exception:
            pid = 0
            sid = 0
        items.append({
            'project_id': pid,
            'stage_id': sid,
            'hours': _flt(row.get('hours'), 0.0)
        })
    return items

def _recalculate_attendance_day(worker_id, work_date):
    day_start = datetime.combine(work_date, datetime.min.time())
    day_end = datetime.combine(work_date, datetime.max.time())
    entries = (TimeEntry.query
               .filter(TimeEntry.worker_id == worker_id,
                       TimeEntry.is_void == False,
                       TimeEntry.check_in >= day_start,
                       TimeEntry.check_in <= day_end)
               .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
               .all())
    day_row = AttendanceDay.query.filter_by(worker_id=worker_id, date=work_date).first()
    if not entries:
        if day_row:
            day_row.total_hours = 0.0
            day_row.day_value = 0.0
            day_row.overtime_hours = 0.0
            day_row.entry_count = 0
            day_row.is_void = True
            day_row.updated_at = _pkt_now_naive()
        return {'total_hours': 0.0, 'day': 0.0, 'overtime': 0.0}

    if not day_row:
        day_row = AttendanceDay(worker_id=worker_id, date=work_date, is_void=False)
        db.session.add(day_row)
        db.session.flush()

    worker = Worker.query.get(worker_id)
    total_hours = 0.0
    regular_done = 0.0
    base_dt = datetime.combine(work_date, datetime.min.time())
    for te in entries:
        hours = max(0.0, _flt(te.hours, 0.0))
        remaining_regular = max(0.0, 8.0 - regular_done)
        regular_hours = min(hours, remaining_regular)
        overtime_hours = max(0.0, hours - regular_hours)

        te.attendance_id = day_row.id
        te.check_in = base_dt + timedelta(hours=total_hours)
        te.check_out = te.check_in + timedelta(hours=hours)
        te.overtime = overtime_hours
        te.wage_calculated = _calc_time_wage(worker, regular_hours, overtime_hours, te.qty_sqft or 0.0, work_date=work_date)
        te.activity_at = te.check_in
        _sync_work_ledger_for_time_entry(te)

        total_hours += hours
        regular_done += regular_hours

    total_overtime = max(0.0, total_hours - 8.0)
    day_row.total_hours = total_hours
    day_row.day_value = 1.0 if total_hours >= 8.0 else 0.0
    day_row.overtime_hours = total_overtime
    day_row.entry_count = len(entries)
    day_row.is_void = False
    day_row.updated_at = _pkt_now_naive()
    return {'total_hours': total_hours, 'day': day_row.day_value, 'overtime': total_overtime}

def _has_recent_duplicate(model, seconds=12, timestamp_field='created_at', **eq_fields):
    q = db.session.query(model)
    for key, val in eq_fields.items():
        q = q.filter(getattr(model, key) == val)
    if hasattr(model, 'is_void') and 'is_void' not in eq_fields:
        q = q.filter(getattr(model, 'is_void') == False)
    ts_col = getattr(model, timestamp_field, None)
    if ts_col is not None:
        q = q.filter(ts_col >= (_pkt_now_naive() - timedelta(seconds=seconds)))
    return q.first() is not None

def _ensure_timeentry_unique_indexes():
    with db.engine.connect() as conn:
        try:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_time_entry_active_worker_checkin
                ON hdc_time_entry(worker_id, check_in)
                WHERE is_void = 0
            """))
            conn.commit()
        except Exception:
            conn.rollback()

def _normalize_trade_name(v):
    raw = (v or '').strip()
    if not raw:
        return ''
    return ' '.join(raw.split())

def _normalize_expense_category_name(v):
    raw = (v or '').strip()
    if not raw:
        return ''
    return ' '.join(raw.split())

def _trade_options():
    """Return active trade list from trade master."""
    master = [t.name.strip() for t in WorkerTrade.query.filter_by(active_status=True).order_by(WorkerTrade.name).all() if (t.name or '').strip()]

    seen = set()
    out = []
    for item in master:
        key = item.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return sorted(out, key=lambda x: x.lower())

def _expense_category_options():
    return [
        c.name.strip()
        for c in ExpenseCategory.query.filter_by(active_status=True).order_by(ExpenseCategory.name).all()
        if (c.name or '').strip()
    ]

def _ensure_expense_category(name):
    cname = _normalize_expense_category_name(name)
    if not cname:
        return None
    row = db.session.query(ExpenseCategory).filter(func.lower(ExpenseCategory.name) == cname.lower()).first()
    if row:
        if not row.active_status:
            row.active_status = True
            db.session.flush()
        return row
    row = ExpenseCategory(name=cname, active_status=True)
    db.session.add(row)
    db.session.flush()
    return row

def _ensure_expense_category_by_id(category_id):
    if not category_id:
        return None
    row = ExpenseCategory.query.get(int(category_id))
    if row and row.active_status:
        return row
    return None

def _category_name_from_id(category_id):
    row = ExpenseCategory.query.get(int(category_id)) if category_id else None
    return (row.name if row else '')

def log_action(user, action_type, description, entity_type, entity_id=''):
    try:
        uname = ''
        uid = None
        if user is not None:
            uid = getattr(user, 'id', None)
            uname = str(getattr(user, 'username', '') or '').strip()
        if not uname and has_request_context():
            uname = str(getattr(current_user, 'username', '') or '').strip()
            uid = uid or getattr(current_user, 'id', None)
        if not uname:
            uname = 'system'
        row = ActivityLog(
            user_id=uid,
            username=uname,
            action_type=(action_type or 'action')[:40],
            description=(description or '-')[:4000],
            entity_type=(entity_type or 'unknown')[:80],
            entity_id=str(entity_id or '')[:80],
            created_at=_pkt_now_naive()
        )
        db.session.add(row)
    except Exception:
        pass

def _is_linked_system_expense(exp):
    if not exp:
        return False
    if getattr(exp, 'tip_worker_id', None):
        return True
    remarks_u = ((getattr(exp, 'remarks', '') or '')).upper()
    return (
        'TIP_WORKER_ID:' in remarks_u
        or 'TIP_SUBCONTRACTOR_ID:' in remarks_u
        or 'SETTLE_WORKER_ID:' in remarks_u
        or 'SETTLE_SUBCONTRACTOR_ID:' in remarks_u
    )

def _office_expense_total():
    return float(
        db.session.query(func.coalesce(func.sum(OfficeExpense.amount), 0.0))
        .filter(OfficeExpense.is_void == False)
        .scalar() or 0.0
    )

def _personal_expense_total():
    return float(
        db.session.query(func.coalesce(func.sum(PersonalExpense.amount), 0.0))
        .filter(PersonalExpense.is_void == False)
        .scalar() or 0.0
    )

def _personal_expense_month():
    return float(
        db.session.query(func.coalesce(func.sum(PersonalExpense.amount), 0.0))
        .filter(
            PersonalExpense.is_void == False,
            func.strftime('%Y-%m', PersonalExpense.date) == _pkt_today().strftime('%Y-%m')
        )
        .scalar() or 0.0
    )

def _office_staff_ledger_snapshot(staff_id):
    today = _pkt_today()
    month_start = date(today.year, today.month, 1)
    month_days = float(pycal.monthrange(today.year, today.month)[1] or 30)
    month_end = date(today.year, today.month, int(month_days))

    staff_row = OfficeStaff.query.get(staff_id)
    monthly_basic_pay = float(getattr(staff_row, 'monthly_salary', 0.0) or 0.0)
    allowance_rows = (StaffAllowance.query
                      .filter(
                          StaffAllowance.staff_id == staff_id,
                          StaffAllowance.is_active == True
                      )
                      .all())
    monthly_allowances = float(sum(
        float(a.amount or 0.0)
        for a in allowance_rows
        if (not getattr(a, 'effective_date', None)) or (a.effective_date <= today)
    ))
    monthly_gross_pay = monthly_basic_pay + monthly_allowances
    per_day_basic = (monthly_basic_pay / month_days) if month_days > 0 else 0.0
    per_day_allowances = (monthly_allowances / month_days) if month_days > 0 else 0.0
    per_day_salary = per_day_basic + per_day_allowances

    units_map = {
        'present': 1.0,
        'absent': 0.0,
        'weekly_leave': 1.0,
        'leave': 1.0,
    }

    # Current month attendance for display purposes
    this_month_rows = (OfficeStaffAttendance.query
                       .filter(
                           OfficeStaffAttendance.staff_id == staff_id,
                           OfficeStaffAttendance.date >= month_start,
                           OfficeStaffAttendance.date <= month_end
                       )
                       .all())
    attendance_units = float(sum(units_map.get((r.status or '').strip().lower(), 0.0) for r in this_month_rows))
    earned_basic = attendance_units * per_day_basic
    earned_allowances = attendance_units * per_day_allowances
    earned = earned_basic + earned_allowances

    # All-time total earned (all attendance records, using current salary rate per day in each month)
    all_att_rows = (OfficeStaffAttendance.query
                    .filter(OfficeStaffAttendance.staff_id == staff_id)
                    .all())
    total_earned = 0.0
    for r in all_att_rows:
        units = units_map.get((r.status or '').strip().lower(), 0.0)
        if units > 0:
            row_month_days = float(pycal.monthrange(r.date.year, r.date.month)[1] or 30)
            allowance_for_row = float(sum(
                float(a.amount or 0.0)
                for a in allowance_rows
                if (not getattr(a, 'effective_date', None)) or (a.effective_date <= r.date)
            ))
            row_basic = (monthly_basic_pay / row_month_days)
            row_allowance = (allowance_for_row / row_month_days)
            total_earned += (row_basic + row_allowance) * units

    # All-time cumulative advances, payments, and settlements (not month-locked)
    advanced = float(db.session.query(func.coalesce(func.sum(OfficeStaffLedger.amount), 0.0))
                     .filter(
                         OfficeStaffLedger.staff_id == staff_id,
                         OfficeStaffLedger.entry_type == 'advance',
                         OfficeStaffLedger.is_void == False
                     )
                     .scalar() or 0.0)
    paid = float(db.session.query(func.coalesce(func.sum(OfficeStaffLedger.amount), 0.0))
                 .filter(
                     OfficeStaffLedger.staff_id == staff_id,
                     OfficeStaffLedger.entry_type == 'payment',
                     OfficeStaffLedger.is_void == False
                 )
                 .scalar() or 0.0)
    tip = float(db.session.query(func.coalesce(func.sum(OfficeStaffLedger.amount), 0.0))
                .filter(
                    OfficeStaffLedger.staff_id == staff_id,
                    OfficeStaffLedger.entry_type == 'tip',
                    OfficeStaffLedger.is_void == False
                )
                .scalar() or 0.0)
    settled = float(db.session.query(func.coalesce(func.sum(OfficeStaffLedger.amount), 0.0))
                    .filter(
                        OfficeStaffLedger.staff_id == staff_id,
                        OfficeStaffLedger.entry_type == 'settlement',
                        OfficeStaffLedger.is_void == False
                    )
                    .scalar() or 0.0)
    # Balance based on all-time earned vs all-time advances, payments, and write-offs.
    # Tip is gratis (above what was earned) and is tracked separately as a cash
    # expense — it does NOT reduce what the staff member is owed.
    balance = total_earned - advanced - paid - settled
    return {
        'month_start': month_start,
        'month_end': month_end,
        'month_days': month_days,
        'monthly_salary': monthly_basic_pay,
        'monthly_basic_pay': monthly_basic_pay,
        'monthly_allowances': monthly_allowances,
        'monthly_gross_pay': monthly_gross_pay,
        'per_day_salary': per_day_salary,
        'per_day_basic': per_day_basic,
        'per_day_allowances': per_day_allowances,
        'attendance_units': attendance_units,
        'earned': earned,
        'earned_basic': earned_basic,
        'earned_allowances': earned_allowances,
        'total_earned': total_earned,
        'advanced': advanced,
        'paid': paid,
        'tip': tip,
        'settled': settled,
        'balance': balance
    }

def _sync_office_staff_expense_from_ledger(staff_row, ledger_row):
    if not staff_row or not ledger_row:
        return
    entry_type = (ledger_row.entry_type or '').strip().lower()
    if entry_type not in ('advance', 'payment', 'tip'):
        return
    if entry_type == 'advance':
        category = 'Office Staff Advance'
    elif entry_type == 'tip':
        category = 'Office Staff Tip'
    else:
        category = 'Office Salary Payment'
    linked = (OfficeExpense.query
              .filter(OfficeExpense.office_staff_ledger_id == ledger_row.id)
              .first())
    notes = (ledger_row.notes or '').strip()
    remarks = f'{category} | {staff_row.name} ({staff_row.staff_code})'
    if notes:
        remarks = remarks + f' | {notes}'
    if linked:
        linked.office_staff_id = staff_row.id
        linked.date = ledger_row.date
        linked.category = category
        linked.amount = float(ledger_row.amount or 0.0)
        linked.remarks = remarks
        linked.is_void = False
        linked.void_reason = None
        linked.voided_at = None
        linked.activity_at = _activity_at_for(ledger_row.date)
        return linked
    linked = OfficeExpense(
        office_staff_id=staff_row.id,
        office_staff_ledger_id=ledger_row.id,
        date=ledger_row.date,
        category=category,
        amount=float(ledger_row.amount or 0.0),
        remarks=remarks,
        is_void=False,
        activity_at=_activity_at_for(ledger_row.date)
    )
    db.session.add(linked)
    return linked

def _remove_office_salary_expense_for_ledger(ledger_id):
    if not ledger_id:
        return
    rows = OfficeExpense.query.filter(
        OfficeExpense.office_staff_ledger_id == ledger_id,
        OfficeExpense.is_void == False
    ).all()
    for r in rows:
        r.is_void = True
        r.void_reason = 'Voided from office staff ledger'
        r.voided_at = _pkt_now_naive()

def _is_linked_office_salary_expense(exp):
    if not exp:
        return False
    return bool(getattr(exp, 'office_staff_ledger_id', None))

def _worker_payable_snapshot(worker_id):
    earned = float(db.session.query(func.coalesce(func.sum(TimeEntry.wage_calculated), 0.0))
                   .filter(TimeEntry.worker_id == worker_id, TimeEntry.is_void == False)
                   .scalar() or 0.0)
    advanced = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                     .filter(
                         LabourLedger.worker_id == worker_id,
                         LabourLedger.entry_type == 'advance',
                         LabourLedger.is_void == False
                     )
                     .scalar() or 0.0)
    salary_paid = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                 .filter(
                     LabourLedger.worker_id == worker_id,
                     LabourLedger.entry_type == 'payment',
                     LabourLedger.is_void == False
                 )
                 .scalar() or 0.0)
    tip_paid = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                .filter(
                    LabourLedger.worker_id == worker_id,
                    LabourLedger.entry_type == 'tip',
                    LabourLedger.is_void == False
                )
                .scalar() or 0.0)
    settled = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
               .filter(
                   LabourLedger.worker_id == worker_id,
                   LabourLedger.entry_type == 'settlement',
                   LabourLedger.is_void == False
               )
               .scalar() or 0.0)
    # Bug fix (2026-04-25): tip is gratis cash given on top of what the worker
    # earned, so it MUST NOT subtract from the worker's owed balance. The cash
    # outflow is still tracked (Expense + AccountTransaction); it just stays
    # informational in the worker ledger. Previously a worker with due 667
    # paid 700 (= 667 payment + 33 tip) ended up at balance -33 even though
    # the user's intent was "settle 667 and give 33 as a thank-you" -> 0.
    paid = salary_paid  # what the worker was paid against earnings
    balance = earned - advanced - salary_paid - settled
    payable = balance if balance > 0 else 0.0
    return {
        'earned': earned,
        'advanced': advanced,
        'paid': paid,
        'salary_paid': salary_paid,
        'tip_paid': tip_paid,
        'settled': settled,
        'balance': balance,
        'payable': payable
    }

def _worker_tip_expenses(worker):
    """Return tip expenses authoritatively belonging to a worker.

    Bug fix: Previously used ``LIKE '%TIP_WORKER_ID:N%'`` which matched any
    longer ID with ``N`` as a prefix (e.g. worker_id=1 matched
    ``TIP_WORKER_ID:10``). That caused tips for one worker to silently
    duplicate into other workers' ledgers. We now trust the ``tip_worker_id``
    foreign key as the single source of truth, and only fall back to
    well-delimited remark matching for legacy rows where the FK is NULL.
    """
    wid = int(worker.id)
    name_pat = f'%Tip for {worker.name}%'

    def _delim_patterns(tag):
        # Match the tag only when it ends the remarks or is followed by a
        # non-digit delimiter (space, pipe, comma, slash, hyphen, semicolon
        # or end-of-line). This prevents 'TIP_WORKER_ID:1' from matching
        # 'TIP_WORKER_ID:10', 'TIP_WORKER_ID:11', etc.
        return [
            Expense.remarks.ilike(f'%{tag}'),
            Expense.remarks.ilike(f'%{tag} %'),
            Expense.remarks.ilike(f'%{tag}|%'),
            Expense.remarks.ilike(f'%{tag},%'),
            Expense.remarks.ilike(f'%{tag};%'),
            Expense.remarks.ilike(f'%{tag}/%'),
            Expense.remarks.ilike(f'%{tag}-%'),
            Expense.remarks.ilike(f'%{tag}\n%'),
            Expense.remarks.ilike(f'%{tag}\r%'),
            Expense.remarks.ilike(f'%{tag}\t%'),
        ]

    tag = f'TIP_WORKER_ID:{wid}'
    legacy_match = or_(
        # Authoritative FK match
        Expense.tip_worker_id == wid,
        # Legacy fallback only when FK is missing
        and_(
            Expense.tip_worker_id.is_(None),
            or_(
                or_(*_delim_patterns(tag)),
                Expense.remarks.ilike(name_pat),
            )
        )
    )

    try:
        return (Expense.query
                .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
                .filter(
                    func.lower(ExpenseCategory.name) == 'tip',
                    legacy_match
                )
                .order_by(Expense.activity_at.asc(), Expense.id.asc())
                .all())
    except OperationalError:
        db.session.rollback()
        # Schema lacks tip_worker_id column entirely (very old DBs). Fall back
        # to remark-only matching but still using delimited patterns so the
        # prefix-substring leak cannot recur.
        return (Expense.query
                .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
                .filter(
                    func.lower(ExpenseCategory.name) == 'tip',
                    or_(
                        or_(*_delim_patterns(tag)),
                        Expense.remarks.ilike(name_pat),
                    )
                )
                .order_by(Expense.activity_at.asc(), Expense.id.asc())
                .all())

def _build_stage_event_ledger(stage):
    rows = []

    def _push(*, dt, event_type, source, notes, amount, impact, status, category, ref):
        rows.append({
            'dt': dt or _pkt_now_naive(),
            'event_type': event_type,
            'source': source or '-',
            'notes': notes or '-',
            'amount': float(amount or 0.0),
            'impact': float(impact or 0.0),
            'status': status or 'Active',
            'category': category or 'other',
            'ref': ref or '-'
        })

    time_rows = (TimeEntry.query
                 .filter(TimeEntry.stage_id == stage.id)
                 .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
                 .all())
    for t in time_rows:
        is_active = not bool(t.is_void)
        worker_name = t.worker.name if t.worker else f'Worker #{t.worker_id}'
        status = 'Active' if is_active else 'Voided'
        _push(
            dt=t.activity_at or t.check_in or t.created_at,
            event_type='Attendance Wage',
            source=worker_name,
            notes=f'Hours {t.hours or 0:.2f}, OT {t.overtime or 0:.2f}',
            amount=t.wage_calculated or 0.0,
            impact=(t.wage_calculated or 0.0) if is_active else 0.0,
            status=status,
            category='labour',
            ref=f'TIME#{t.id}'
        )

    ledger_rows = (LabourLedger.query
                   .filter(
                       LabourLedger.stage_id == stage.id,
                       LabourLedger.entry_type.in_(['advance', 'payment', 'tip', 'settlement'])
                   )
                   .order_by(LabourLedger.activity_at.asc(), LabourLedger.id.asc())
                   .all())
    for l in ledger_rows:
        worker_name = l.worker.name if l.worker else f'Worker #{l.worker_id}'
        status = 'Active' if not l.is_void else 'Voided'
        type_map = {
            'advance': 'Worker Advance',
            'payment': 'Worker Payment',
            'tip': 'Worker Tip',
            'settlement': 'Worker Settlement'
        }
        _push(
            dt=l.activity_at or l.created_at,
            event_type=type_map.get(l.entry_type, (l.entry_type or 'Ledger').title()),
            source=worker_name,
            notes=l.notes or '-',
            amount=l.amount or 0.0,
            impact=0.0,
            status=status,
            category='finance',
            ref=f'LEDGER#{l.id}'
        )

    expense_rows = (Expense.query
                    .filter(Expense.stage_id == stage.id)
                    .order_by(Expense.activity_at.asc(), Expense.id.asc())
                    .all())
    for e in expense_rows:
        cat = (e.category or 'Expense').strip()
        _push(
            dt=e.activity_at or e.created_at,
            event_type=f'Expense: {cat}',
            source=stage.project.name if stage.project else '-',
            notes=e.remarks or '-',
            amount=e.amount or 0.0,
            impact=e.amount or 0.0,
            status='Active',
            category='expense',
            ref=f'EXP#{e.id}'
        )

    purchase_rows = (Purchase.query
                     .filter(Purchase.stage_id == stage.id)
                     .order_by(Purchase.activity_at.asc(), Purchase.id.asc())
                     .all())
    for p in purchase_rows:
        mat_name = p.material.name if p.material else f'Material #{p.material_id}'
        ptype = (p.entry_type or 'purchase').strip().lower()
        is_return = (ptype == 'return') or float(p.total or 0.0) < 0
        _push(
            dt=p.activity_at or p.created_at,
            event_type=('Material Return' if is_return else 'Material Purchase'),
            source=mat_name,
            notes=f'Qty {p.qty or 0:.2f} @ {p.rate or 0:.2f}',
            amount=p.total or 0.0,
            impact=p.total or 0.0,
            status='Active',
            category='material',
            ref=f'PUR#{p.id}'
        )
    usage_v2_rows = (UsageLogV2.query
                     .filter(UsageLogV2.stage_id == stage.id, UsageLogV2.is_void == False)
                     .order_by(UsageLogV2.created_at.asc(), UsageLogV2.id.asc())
                     .all())
    for u in usage_v2_rows:
        mat_name = u.material.name if u.material else f'Material #{u.material_id}'
        unit_cost = (float(u.cost or 0.0) / float(u.quantity or 1.0)) if float(u.quantity or 0.0) > 0 else 0.0
        _push(
            dt=u.created_at or datetime.combine(u.date or _pkt_today(), datetime.min.time()),
            event_type='Material Usage (V2)',
            source=mat_name,
            notes=f'Qty {u.quantity or 0:.2f} @ {unit_cost:.2f}',
            amount=u.cost or 0.0,
            impact=u.cost or 0.0,
            status='Active',
            category='material',
            ref=f'USEV2#{u.id}'
        )

    subs = (Subcontractor.query
            .filter(Subcontractor.stage_id == stage.id)
            .order_by(Subcontractor.created_at.asc(), Subcontractor.id.asc())
            .all())
    for s in subs:
        _push(
            dt=s.created_at,
            event_type='Subcontract Assigned',
            source=s.name or f'Subcontractor #{s.id}',
            notes=f'{s.work_type or "Work"} | Contract registered (cost impact on payment/settlement)',
            amount=s.contract_value or 0.0,
            impact=0.0,
            status='Active',
            category='finance',
            ref=f'SUB#{s.id}'
        )
        att_rows = (SubcontractAttendance.query
                    .filter(SubcontractAttendance.subcontractor_id == s.id)
                    .order_by(SubcontractAttendance.activity_at.asc(), SubcontractAttendance.id.asc())
                    .all())
        for sa in att_rows:
            notes = f'Present {sa.present_count or 0}'
            if (sa.work_done_pct or 0) > 0:
                notes += f' | Progress +{(sa.work_done_pct or 0):.2f}%'
            if sa.notes:
                notes += f' | {sa.notes}'
            _push(
                dt=sa.activity_at or sa.created_at,
                event_type='Subcontract Attendance',
                source=s.name or f'Subcontractor #{s.id}',
                notes=notes,
                amount=0.0,
                impact=0.0,
                status='Active',
                category='other',
                ref=f'SATT#{sa.id}'
            )
    rows.sort(key=lambda r: (r['dt'], r['ref']))
    running = 0.0
    for r in rows:
        running += float(r['impact'] or 0.0)
        r['running_total'] = running

    totals = {
        'labour': 0.0,
        'material': 0.0,
        'expense': 0.0,
        'subcontract': 0.0,
        'finance': 0.0,
        'other': 0.0
    }
    for r in rows:
        key = r.get('category') or 'other'
        if key not in totals:
            key = 'other'
        totals[key] += float(r.get('impact') or 0.0)
    grand_total = running
    return rows, totals, grand_total

def _reconcile_worker_tip_ledger(worker):
    tips = _worker_tip_expenses(worker)
    for exp in tips:
        # Primary identity check: every reconciled tip ledger row carries
        # the originating expense id in its notes as ``TIP_EXPENSE_ID:N``.
        # Use that as the duplicate guard instead of fuzzy
        # date/amount/project matching so we never duplicate a tip into
        # the wrong worker's ledger again.
        exp_tag = f'TIP_EXPENSE_ID:{exp.id}'
        # Tag must end the string, or be followed by a non-digit delimiter
        # so TIP_EXPENSE_ID:1 cannot accidentally match TIP_EXPENSE_ID:10.
        exp_tag_clauses = [
            LabourLedger.notes.ilike(f'%{exp_tag}'),
            LabourLedger.notes.ilike(f'%{exp_tag} %'),
            LabourLedger.notes.ilike(f'%{exp_tag}|%'),
            LabourLedger.notes.ilike(f'%{exp_tag},%'),
            LabourLedger.notes.ilike(f'%{exp_tag};%'),
            LabourLedger.notes.ilike(f'%{exp_tag}/%'),
            LabourLedger.notes.ilike(f'%{exp_tag}-%'),
        ]
        exists = (LabourLedger.query
                  .filter(
                      LabourLedger.worker_id == worker.id,
                      LabourLedger.entry_type == 'tip',
                      LabourLedger.is_void == False,
                      or_(*exp_tag_clauses)
                  )
                  .first())
        if exists:
            continue
        # Belt-and-suspenders (2026-04-25): also block if there is already an
        # active tip ledger row for this worker on the same date and amount,
        # even if it doesn't carry the TIP_EXPENSE_ID tag yet (legacy rows
        # written by the accounts payment flow before the bug fix). Without
        # this guard the reconciler would write a second tip row and the
        # worker's balance would silently go over-paid by the tip amount.
        legacy = (LabourLedger.query
                  .filter(
                      LabourLedger.worker_id == worker.id,
                      LabourLedger.entry_type == 'tip',
                      LabourLedger.is_void == False,
                      LabourLedger.date == exp.date,
                      LabourLedger.amount == float(exp.amount or 0.0),
                  )
                  .first())
        if legacy:
            # Backfill the tag on the legacy row so future reconciler runs
            # find it via the primary check above.
            existing_notes = (legacy.notes or '').strip()
            if f'TIP_EXPENSE_ID:{exp.id}' not in existing_notes:
                legacy.notes = (existing_notes + ' | ' if existing_notes else '') + f'TIP_EXPENSE_ID:{exp.id}'
            continue
        notes = (exp.remarks or '') + f' | TIP_EXPENSE_ID:{exp.id}'
        db.session.add(LabourLedger(
            worker_id=worker.id,
            entry_type='tip',
            amount=float(exp.amount or 0.0),
            date=exp.date,
            activity_at=exp.activity_at or _activity_at_for(exp.date),
            project_id=exp.project_id,
            stage_id=exp.stage_id,
            notes=notes
        ))

def _is_pdf_upload(file_obj):
    if not file_obj:
        return False
    filename = (file_obj.filename or '').strip().lower()
    return filename.endswith('.pdf')

def _worker_rate_on(worker, on_date=None):
    """
    Resolve worker wage type/rate effective on the provided date.
    Falls back to current worker fields if no history record matches.
    """
    if on_date:
        wr = (WorkerRate.query
              .filter(WorkerRate.worker_id == worker.id,
                      WorkerRate.effective_from <= on_date)
              .order_by(WorkerRate.effective_from.desc(), WorkerRate.id.desc())
              .first())
        if wr:
            return (wr.wage_type or worker.wage_type), float(wr.rate or 0.0)

    # Fallback to current profile values
    wtype = worker.wage_type or 'daily'
    if wtype == 'hourly':
        return wtype, float(worker.hourly_rate or worker.hourly_wage or 0.0)
    if wtype == 'per_sqft':
        return wtype, float(worker.rate_per_sqft or 0.0)
    return 'daily', float(worker.base_daily_wage or 0.0)


def _calc_time_wage(worker, hours, overtime, qty_sqft=0.0, work_date=None):
    hours = _flt(hours, 0.0)
    overtime = _flt(overtime, 0.0)
    qty_sqft = _flt(qty_sqft, 0.0)
    wtype, base_rate = _worker_rate_on(worker, work_date)
    if wtype == 'per_sqft':
        return max(0.0, base_rate * qty_sqft)
    if wtype == 'hourly':
        return max(0.0, base_rate * hours)
    full_day = min(1.0, hours / 8.0) if hours > 0 else 0.0
    hourly = (base_rate / 8.0) if base_rate > 0 else 0.0
    return max(0.0, base_rate * full_day + (overtime or 0) * hourly)

def _sync_work_ledger_for_time_entry(te):
    if not te:
        return
    row = LabourLedger.query.filter_by(time_entry_id=te.id, entry_type='work').first()
    if te.is_void:
        if row and not row.is_void:
            row.is_void = True
            row.void_reason = te.void_reason or 'Time entry voided'
            row.voided_at = te.voided_at or _pkt_now_naive()
        return
    if not row:
        row = LabourLedger(
            worker_id=te.worker_id,
            entry_type='work',
            time_entry_id=te.id
        )
        db.session.add(row)
    row.worker_id = te.worker_id
    row.project_id = te.project_id
    row.stage_id = te.stage_id
    row.amount = float(te.wage_calculated or 0.0)
    row.date = te.check_in.date()
    row.activity_at = te.check_in
    row.notes = f'Time entry {te.check_in.date()}'
    row.is_void = False
    row.void_reason = None
    row.voided_at = None

def _void_orphan_work_ledgers_for_time_entry(te, reason='Time entry voided'):
    if not te:
        return
    cands = (LabourLedger.query
             .filter(
                 LabourLedger.worker_id == te.worker_id,
                 LabourLedger.entry_type == 'work',
                 LabourLedger.is_void == False,
                 LabourLedger.time_entry_id.is_(None),
                 LabourLedger.date == te.check_in.date(),
                 LabourLedger.project_id == te.project_id
             )
             .all())
    target_amt = float(te.wage_calculated or 0.0)
    for row in cands:
        if abs(float(row.amount or 0.0) - target_amt) <= 0.01:
            row.is_void = True
            row.void_reason = reason
            row.voided_at = _pkt_now_naive()

def _repair_worker_work_ledger_links(worker_id):
    rows = (LabourLedger.query
            .filter(
                LabourLedger.worker_id == worker_id,
                LabourLedger.entry_type == 'work',
                LabourLedger.is_void == False,
                LabourLedger.time_entry_id.is_(None)
            )
            .all())
    for row in rows:
        match = (TimeEntry.query
                 .filter(
                     TimeEntry.worker_id == row.worker_id,
                     TimeEntry.is_void == False,
                     TimeEntry.project_id == row.project_id,
                     TimeEntry.check_in >= datetime.combine(row.date, datetime.min.time()),
                     TimeEntry.check_in <= datetime.combine(row.date, datetime.max.time())
                 )
                 .order_by(TimeEntry.check_in.asc())
                 .all())
        linked = None
        for te in match:
            if abs(float(te.wage_calculated or 0.0) - float(row.amount or 0.0)) <= 0.01:
                linked = te
                break
        if linked:
            row.time_entry_id = linked.id
            row.stage_id = linked.stage_id
        else:
            row.is_void = True
            row.void_reason = 'Auto-void orphaned work ledger (missing active time entry)'
            row.voided_at = _pkt_now_naive()

def _reconcile_worker_time_entries(worker_id):
    day_entries = (TimeEntry.query
                   .filter(TimeEntry.worker_id == worker_id, TimeEntry.is_void == False)
                   .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
                   .all())
    # Merge historical split rows where OT was mistakenly saved as a second row
    # on the same project/stage immediately after base duty.
    for i in range(len(day_entries) - 1):
        base = day_entries[i]
        ot = day_entries[i + 1]
        if base.is_void or ot.is_void:
            continue
        if base.project_id != ot.project_id or base.stage_id != ot.stage_id:
            continue
        if base.check_in.date() != ot.check_in.date():
            continue
        if ot.check_in != base.check_out:
            continue
        if float(ot.overtime or 0.0) <= 0 or abs(float(ot.hours or 0.0) - float(ot.overtime or 0.0)) > 0.01:
            continue
        base.check_out = ot.check_out
        base.hours = float(base.hours or 0.0) + float(ot.hours or 0.0)
        base.overtime = float(base.overtime or 0.0) + float(ot.overtime or 0.0)
        base.wage_calculated = float(base.wage_calculated or 0.0) + float(ot.wage_calculated or 0.0)
        _sync_work_ledger_for_time_entry(base)
        ot.is_void = True
        ot.void_reason = 'Auto-merged with same project/stage base entry'
        ot.voided_at = _pkt_now_naive()
        _sync_work_ledger_for_time_entry(ot)
        _void_orphan_work_ledgers_for_time_entry(ot, reason=ot.void_reason)

    entries = (TimeEntry.query
               .filter(TimeEntry.worker_id == worker_id, TimeEntry.is_void == False)
               .order_by(TimeEntry.check_in.asc(), TimeEntry.id.desc())
               .all())
    keep_map = {}
    for te in entries:
        key = (te.worker_id, te.project_id, te.stage_id, te.check_in)
        if key not in keep_map:
            keep_map[key] = te
            _sync_work_ledger_for_time_entry(te)
            continue
        te.is_void = True
        te.void_reason = 'Auto-void duplicate time entry (same worker/project/stage/start time)'
        te.voided_at = _pkt_now_naive()
        _sync_work_ledger_for_time_entry(te)
        _void_orphan_work_ledgers_for_time_entry(te, reason=te.void_reason)

def _reconcile_all_time_entries_once():
    if _runtime_flag_get('time_entry_dedupe_done') == '1':
        return
    try:
        worker_ids = [wid for (wid,) in db.session.query(TimeEntry.worker_id).distinct().all() if wid]
        for wid in worker_ids:
            _reconcile_worker_time_entries(wid)
            _repair_worker_work_ledger_links(wid)
        db.session.commit()
        _runtime_flag_set('time_entry_dedupe_done', '1')
    except Exception as ex:
        app.logger.warning('Time-entry reconciliation skipped due to error: %s', ex)
        db.session.rollback()

def _timekeeping_status_dataset(status_date, status_view='assigned'):
    status_view = (status_view or 'assigned').strip().lower()
    if status_view not in ('assigned', 'not_assigned', 'absent'):
        status_view = 'assigned'

    workers = Worker.query.filter_by(active_status=True).all()
    active_worker_ids = [w.id for w in workers]
    day_start = datetime.combine(status_date, datetime.min.time())
    day_end = datetime.combine(status_date, datetime.max.time())

    assigned_count = 0
    absent_count = 0
    not_assigned_count = 0
    status_rows = []

    if not active_worker_ids:
        return dict(
            status_date=status_date,
            status_view=status_view,
            assigned_count=assigned_count,
            absent_count=absent_count,
            not_assigned_count=not_assigned_count,
            status_rows=status_rows
        )

    assigned_latest = {}
    assigned_entry_counts = {}
    assigned_ids = set()

    assigned_records = (db.session.query(TimeEntry, Worker, Project)
                        .join(Worker, TimeEntry.worker_id == Worker.id)
                        .join(Project, TimeEntry.project_id == Project.id)
                        .filter(Worker.active_status == True,
                                TimeEntry.is_void == False,
                                TimeEntry.check_in >= day_start,
                                TimeEntry.check_in <= day_end)
                        .order_by(TimeEntry.check_in.desc())
                        .all())

    for te, wk, pr in assigned_records:
        wid = wk.id
        assigned_ids.add(wid)
        assigned_entry_counts[wid] = assigned_entry_counts.get(wid, 0) + 1
        if wid not in assigned_latest:
            assigned_latest[wid] = (te, wk, pr)

    has_before_ids = {
        wid for (wid,) in db.session.query(TimeEntry.worker_id)
        .filter(TimeEntry.worker_id.in_(active_worker_ids),
                TimeEntry.is_void == False,
                TimeEntry.check_in < day_start)
        .distinct()
        .all()
    }
    has_any_ids = {
        wid for (wid,) in db.session.query(TimeEntry.worker_id)
        .filter(TimeEntry.worker_id.in_(active_worker_ids), TimeEntry.is_void == False)
        .distinct()
        .all()
    }
    manual_absent_ids = {
        wid for (wid,) in db.session.query(AttendanceMark.worker_id)
        .filter(
            AttendanceMark.worker_id.in_(active_worker_ids),
            AttendanceMark.date == status_date,
            AttendanceMark.status == 'absent'
        )
        .distinct()
        .all()
    }

    absent_ids = ((has_before_ids - assigned_ids) | manual_absent_ids) - assigned_ids
    not_assigned_ids = (set(active_worker_ids) - has_any_ids) - manual_absent_ids

    assigned_count = len(assigned_ids)
    absent_count = len(absent_ids)
    not_assigned_count = len(not_assigned_ids)

    if status_view == 'assigned':
        for wid, (te, wk, pr) in assigned_latest.items():
            status_rows.append({
                'worker_code': wk.worker_code,
                'worker_name': wk.name,
                'trade': wk.role_type or '',
                'project': pr.name if pr else '-',
                'stage': te.stage.name if te.stage_id and te.stage else '-',
                'entries_count': assigned_entry_counts.get(wid, 1)
            })
        status_rows.sort(key=lambda r: (r.get('worker_name') or '').lower())
    elif status_view == 'absent':
        worker_map = {w.id: w for w in workers}
        for wid in sorted(absent_ids, key=lambda x: (worker_map.get(x).name or '').lower() if worker_map.get(x) else ''):
            wk = worker_map.get(wid)
            if not wk:
                continue
            status_rows.append({
                'worker_code': wk.worker_code,
                'worker_name': wk.name,
                'trade': wk.role_type or '',
                'project': '-',
                'stage': '-',
                'entries_count': '-'
            })
    else:
        worker_map = {w.id: w for w in workers}
        for wid in sorted(not_assigned_ids, key=lambda x: (worker_map.get(x).name or '').lower() if worker_map.get(x) else ''):
            wk = worker_map.get(wid)
            if not wk:
                continue
            status_rows.append({
                'worker_code': wk.worker_code,
                'worker_name': wk.name,
                'trade': wk.role_type or '',
                'project': '-',
                'stage': '-',
                'entries_count': '-'
            })

    return dict(
        status_date=status_date,
        status_view=status_view,
        assigned_count=assigned_count,
        absent_count=absent_count,
        not_assigned_count=not_assigned_count,
        status_rows=status_rows
    )

def _refresh_alerts():
    today = _pkt_today()
    # Stage over-budget alerts
    for s in Stage.query.all():
        if (s.estimated_cost or 0) > 0 and s.actual_cost > (s.estimated_cost or 0):
            key = f"stage_over_{s.id}"
            if not Alert.query.filter_by(key=key, resolved=False).first():
                db.session.add(Alert(
                    key=key, level='danger',
                    message=f'Stage \"{s.name}\" exceeded estimated cost.',
                    project_id=s.project_id, stage_id=s.id))
        if s.end_date and s.end_date < today and str(s.status).lower() not in ('completed','complete'):
            key = f"stage_delay_{s.id}"
            if not Alert.query.filter_by(key=key, resolved=False).first():
                db.session.add(Alert(
                    key=key, level='warning',
                    message=f'Stage \"{s.name}\" is delayed.',
                    project_id=s.project_id, stage_id=s.id))
    # Project over-budget alerts
    for p in Project.query.all():
        if (p.budget_total or 0) > 0 and p.actual_cost > (p.budget_total or 0):
            key = f"project_over_{p.id}"
            if not Alert.query.filter_by(key=key, resolved=False).first():
                db.session.add(Alert(
                    key=key, level='danger',
                    message=f'Project \"{p.name}\" exceeded budget.',
                    project_id=p.id))
    db.session.commit()

_ESTIMATION_STORE = os.path.join(INSTANCE_DIR, 'project_estimations.json')
_DB_STORE = DB_PATH
_BACKUP_DIR = os.path.join(INSTANCE_DIR, 'backups')
os.makedirs(_BACKUP_DIR, exist_ok=True)

def _ensure_runtime_flags_table():
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS hdc_runtime_flag (
            key VARCHAR(80) PRIMARY KEY,
            value TEXT,
            updated_at DATETIME
        )
    """))
    db.session.commit()

def _runtime_flag_get(flag_key):
    row = db.session.execute(
        text("SELECT value FROM hdc_runtime_flag WHERE key = :k"),
        {"k": flag_key}
    ).fetchone()
    return (row[0] if row else None)

def _runtime_flag_set(flag_key, value='1'):
    db.session.execute(text("""
        INSERT INTO hdc_runtime_flag(key, value, updated_at)
        VALUES(:k, :v, :ts)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
    """), {"k": flag_key, "v": value, "ts": _pkt_now_naive()})
    db.session.commit()

def _migrate_legacy_done_markers_to_db():
    marker_to_flag = {
        'time_entry_migration.done': 'time_entry_migration_done',
        'time_entry_dedupe.done': 'time_entry_dedupe_done',
    }
    for fname, fkey in marker_to_flag.items():
        marker = os.path.join(INSTANCE_DIR, fname)
        if os.path.exists(marker):
            if _runtime_flag_get(fkey) != '1':
                _runtime_flag_set(fkey, '1')
            try:
                os.remove(marker)
            except OSError:
                pass

def _load_estimations():
    if not os.path.exists(_ESTIMATION_STORE):
        return []
    try:
        with open(_ESTIMATION_STORE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception as ex:
        app.logger.warning('Failed to load project estimations JSON: %s', ex)
    return []

def _save_estimations(items):
    try:
        with open(_ESTIMATION_STORE, 'w', encoding='utf-8') as f:
            json.dump(items, f, indent=2)
        return True
    except Exception as ex:
        app.logger.warning('Failed to save project estimations JSON: %s', ex)
        return False

def _backup_filename():
    return f"hdc_backup_{_pkt_now_naive().strftime('%Y%m%d_%H%M%S')}.zip"

def _quote_ident(name):
    return '"' + str(name or '').replace('"', '""') + '"'

def _safe_sheet_name(name, fallback='Sheet'):
    raw = str(name or '').strip() or fallback
    cleaned = ''.join(ch for ch in raw if ch not in r'[]:*?/\\')
    cleaned = cleaned[:31].strip() or fallback
    return cleaned

def _create_backup_xlsx(dst_path):
    wb = openpyxl.Workbook()
    if wb.active:
        wb.remove(wb.active)

    # Metadata sheet
    ws_meta = wb.create_sheet(title='README')
    ws_meta.append(['Generated At (PKT)', _pkt_now_naive().strftime('%Y-%m-%d %H:%M:%S')])
    ws_meta.append(['Database Path', _DB_STORE])
    ws_meta.append(['Note', 'This file is a tabular export backup (not a direct restore source).'])

    con = sqlite3.connect(_DB_STORE)
    cur = con.cursor()
    cur.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """)
    table_names = [r[0] for r in cur.fetchall()]

    used_sheet_names = {'README'}
    for tname in table_names:
        sheet_name = _safe_sheet_name(tname, fallback='Table')
        base_name = sheet_name
        suffix = 2
        while sheet_name in used_sheet_names:
            tail = f'_{suffix}'
            sheet_name = (base_name[: max(1, 31 - len(tail))] + tail)
            suffix += 1
        used_sheet_names.add(sheet_name)

        ws = wb.create_sheet(title=sheet_name)
        cur.execute(f"PRAGMA table_info({_quote_ident(tname)})")
        cols = [r[1] for r in cur.fetchall()]
        if not cols:
            ws.append(['(no columns detected)'])
            continue
        ws.append(cols)
        ws.freeze_panes = 'A2'

        col_csv = ', '.join(_quote_ident(c) for c in cols)
        q = f"SELECT {col_csv} FROM {_quote_ident(tname)}"
        for row in cur.execute(q):
            out = []
            for v in row:
                if isinstance(v, (bytes, bytearray)):
                    out.append(v.hex())
                else:
                    out.append(v)
            ws.append(out)

    con.close()
    wb.save(dst_path)

def _create_backup_zip(dst_path):
    fd, xlsx_path = tempfile.mkstemp(prefix='hdc_data_export_', suffix='.xlsx', dir=INSTANCE_DIR)
    os.close(fd)
    db_fd, db_snapshot_path = tempfile.mkstemp(prefix='hdc_db_snapshot_', suffix='.db', dir=INSTANCE_DIR)
    os.close(db_fd)
    try:
        _create_backup_xlsx(xlsx_path)
        # Take a consistent SQLite snapshot so WAL data is included in backup DB.
        src = sqlite3.connect(_DB_STORE)
        try:
            dst = sqlite3.connect(db_snapshot_path)
            try:
                src.backup(dst)
                dst.commit()
                # Sanity-check snapshot integrity and presence of critical tables.
                cur = dst.cursor()
                for t in ('hdc_user', 'hdc_project', 'hdc_account', 'hdc_account_txn'):
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,))
                    if cur.fetchone() is None:
                        raise RuntimeError(f'Backup snapshot missing required table: {t}')
                cur.close()
            finally:
                dst.close()
        finally:
            src.close()
        with zipfile.ZipFile(dst_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(db_snapshot_path):
                zf.write(db_snapshot_path, arcname='hdc_erp.db')
            if os.path.exists(_ESTIMATION_STORE):
                zf.write(_ESTIMATION_STORE, arcname='project_estimations.json')
            if os.path.exists(xlsx_path):
                zf.write(xlsx_path, arcname='hdc_data_export.xlsx')
    finally:
        try:
            if os.path.exists(db_snapshot_path):
                os.remove(db_snapshot_path)
        except Exception:
            pass
        try:
            if os.path.exists(xlsx_path):
                os.remove(xlsx_path)
        except Exception:
            pass

def _restore_from_paths(db_path, estimation_path=None):
    db.session.remove()
    db.engine.dispose()
    # If SQLite WAL sidecar files from an older DB remain, they can override
    # pages after restore and make imported data appear missing.
    try:
        for sidecar in (f"{_DB_STORE}-wal", f"{_DB_STORE}-shm"):
            if os.path.exists(sidecar):
                os.remove(sidecar)
    except Exception:
        pass
    shutil.copy2(db_path, _DB_STORE)
    if estimation_path and os.path.exists(estimation_path):
        shutil.copy2(estimation_path, _ESTIMATION_STORE)
    # Restored backups can be from older schema versions.
    # Force bootstrap/migrations so new columns (e.g. purchase_v2 usage links) exist immediately.
    _ensure_bootstrap_once(force=True)

def _restore_from_backup_zip(zip_path):
    tmpdir = os.path.join(BASE_DIR, f'_restore_extract_{uuid4().hex}')
    os.makedirs(tmpdir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                mname = (member.filename or '').replace('\\', '/')
                if not mname or mname.startswith('/') or '..' in mname.split('/'):
                    raise ValueError(f'Unsafe backup member path: {member.filename}')
                dest = os.path.abspath(os.path.join(tmpdir, mname))
                if not (dest == os.path.abspath(tmpdir) or dest.startswith(os.path.abspath(tmpdir) + os.sep)):
                    raise ValueError(f'Unsafe extraction target: {member.filename}')
            zf.extractall(tmpdir)
        db_file = os.path.join(tmpdir, 'hdc_erp.db')
        if not os.path.exists(db_file):
            raise ValueError('Backup ZIP must contain hdc_erp.db')
        try:
            con = sqlite3.connect(db_file)
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='hdc_user'")
            has_user = cur.fetchone() is not None
            cur.close()
            con.close()
        except Exception as ex:
            raise ValueError(f'Backup DB verification failed: {ex}')
        if not has_user:
            raise ValueError('Invalid backup: hdc_user table not found. This backup appears empty or corrupted.')
        est_file = os.path.join(tmpdir, 'project_estimations.json')
        _restore_from_paths(db_file, est_file if os.path.exists(est_file) else None)
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

def _list_backups():
    rows = []
    for name in os.listdir(_BACKUP_DIR):
        if not name.lower().endswith('.zip'):
            continue
        p = os.path.join(_BACKUP_DIR, name)
        try:
            st = os.stat(p)
            # Always display backup file times in Pakistan Standard Time.
            modified_pkt = datetime.fromtimestamp(st.st_mtime, PKT_ZONE)
            rows.append({
                'name': name,
                'size': st.st_size,
                'modified': modified_pkt
            })
        except OSError:
            continue
    rows.sort(key=lambda r: r['modified'], reverse=True)
    return rows

def _prune_saved_backups(keep_latest=10):
    try:
        keep = int(keep_latest or 0)
    except Exception:
        keep = 10
    keep = max(0, min(200, keep))
    rows = _list_backups()
    deleted = 0
    errors = 0
    for r in rows[keep:]:
        name = (r.get('name') or '').strip()
        if not name:
            continue
        p = os.path.join(_BACKUP_DIR, name)
        try:
            if os.path.exists(p):
                os.remove(p)
                deleted += 1
        except Exception:
            errors += 1
    return {'keep': keep, 'deleted': deleted, 'errors': errors}

def _cleanup_backup_temp_artifacts():
    removed_files = 0
    removed_dirs = 0
    # Temporary snapshot/export files that may remain after interrupted backups.
    try:
        for name in os.listdir(INSTANCE_DIR):
            p = os.path.join(INSTANCE_DIR, name)
            if not os.path.isfile(p):
                continue
            if (name.startswith('hdc_db_snapshot_') and name.endswith('.db')) or \
               (name.startswith('hdc_data_export_') and name.endswith('.xlsx')):
                try:
                    os.remove(p)
                    removed_files += 1
                except Exception:
                    pass
    except Exception:
        pass
    # Temporary extraction folders that may remain after interrupted restore.
    try:
        for name in os.listdir(BASE_DIR):
            p = os.path.join(BASE_DIR, name)
            if (not os.path.isdir(p)) or (not name.startswith('_restore_extract_')):
                continue
            try:
                shutil.rmtree(p, ignore_errors=True)
                removed_dirs += 1
            except Exception:
                pass
    except Exception:
        pass
    return {'files': removed_files, 'dirs': removed_dirs}

_WIPE_TARGETS = {
    'projects': {
        'label': 'Projects, Stages, Owner Payments',
        'tables': [
            'hdc_stage_drawing',
            'hdc_stage_rate_history',
            'hdc_stage',
            'hdc_stage_definition',
            'hdc_owner_payment',
            'hdc_project',
        ],
    },
    'workforce': {
        'label': 'Workers, Attendance, Timekeeping, Payroll',
        'tables': [
            'hdc_payroll_item',
            'hdc_payroll_run',
            'hdc_attendance_mark',
            'hdc_attendance_day',
            'hdc_time_entry',
            'hdc_attendance',
            'hdc_labour_rate_history',
            'hdc_labour_ledger',
            'hdc_worker_rate',
            'hdc_worker',
        ],
    },
    'materials': {
        'label': 'Materials, Purchases, Material Usage',
        'tables': [
            'hdc_usage_log_v2',
            'hdc_delivery',
            'hdc_supplier_ledger',
            'hdc_purchase_v2',
            'hdc_material_v2',
            'hdc_supplier',
            'hdc_material_usage',
            'hdc_purchase',
            'hdc_material',
        ],
    },
    'expenses': {
        'label': 'Expenses',
        'tables': [
            'hdc_expense',
        ],
    },
    'office_management': {
        'label': 'Office Management (Staff, Attendance, Office Expenses, Allowances, Categories)',
        'tables': [
            'hdc_office_staff_ledger',
            'hdc_office_staff_attendance',
            'hdc_staff_allowance',
            'hdc_allowance_category',
            'hdc_office_staff',
            'hdc_office_expense_category',
            'hdc_office_expense',
        ],
    },
    'personal_management': {
        'label': 'Personal Management (Personal Expenses + Categories)',
        'tables': [
            'hdc_personal_expense',
            'hdc_personal_expense_category',
        ],
    },
    'subcontract': {
        'label': 'Subcontractors, Events, Payments, Labour',
        'tables': [
            'hdc_subcontract_labour_payment',
            'hdc_subcontract_labour_worker',
            'hdc_subcontract_event',
            'hdc_subcontract_labour_attendance',
            'hdc_subcontract_attendance',
            'hdc_subcontract_payment',
            'hdc_subcontractor',
        ],
    },
    'formulas': {
        'label': 'Formulas and Estimations',
        'tables': [
            'hdc_estimation_stage',
            'hdc_estimation',
            'hdc_custom_formula',
        ],
    },
    'masters': {
        'label': 'Master Lists (Trades, Expense Categories)',
        'tables': [
            'hdc_worker_trade',
            'hdc_expense_category',
        ],
    },
    'alerts': {
        'label': 'Alerts and Activity Logs',
        'tables': [
            'hdc_alert',
            'hdc_activity_log',
            'hdc_user_activity',
        ],
    },
    'accounts': {
        'label': 'Accounts and Ledger',
        'tables': [
            'hdc_account_txn',
            'hdc_account',
            'hdc_runtime_flag',
        ],
    },
    'users': {
        'label': 'Users',
        'tables': [
            'hdc_user',
        ],
    },
}

def _wipe_selected_targets(target_keys):
    keys = [k for k in (target_keys or []) if k in _WIPE_TARGETS]
    if not keys:
        return {'targets': [], 'tables': 0}

    tables = []
    for k in keys:
        for t in _WIPE_TARGETS[k]['tables']:
            if t not in tables:
                tables.append(t)

    db.session.execute(text('PRAGMA foreign_keys=OFF'))
    try:
        for tbl in tables:
            db.session.execute(text(f'DELETE FROM {tbl}'))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        db.session.execute(text('PRAGMA foreign_keys=ON'))
        db.session.commit()

    # Recreate essential admin/default masters if they were wiped.
    # This is system reseed work; keep it out of user-facing activity logs.
    with _audit_paused():
        _bootstrap_hdc()

    # Reclaim space after large wipes.
    with db.engine.connect() as conn:
        conn.execute(text('VACUUM'))
        conn.commit()

    return {'targets': keys, 'tables': len(tables)}

def _normalize_estimation_rows(rows):
    normalized = []
    for r in (rows or []):
        stage = (r.get('stage_name') if isinstance(r, dict) else '') or ''
        stage = stage.strip()
        rtype = (r.get('type') if isinstance(r, dict) else '') or 'lump_sum'
        if rtype not in ('lump_sum', 'sqft'):
            rtype = 'lump_sum'
        rate = _flt(r.get('rate') if isinstance(r, dict) else 0)
        qty  = _flt(r.get('quantity') if isinstance(r, dict) else 0)
        if rate < 0: rate = 0
        if qty < 0: qty = 0
        total = rate if rtype == 'lump_sum' else rate * qty
        if not isinstance(total, (int, float)) or not (total >= 0):
            total = 0
        normalized.append({
            'stage_name': stage,
            'type': rtype,
            'rate': rate,
            'quantity': 0 if rtype == 'lump_sum' else qty,
            'total': total
        })
    return normalized

def _generate_project_code():
    return _next_project_code()

def _next_project_code():
    prefix = "HDC-"
    max_n = 0
    for p in Project.query.filter(Project.project_code.like(prefix + '%')).all():
        code = (p.project_code or '').strip().upper()
        if not code.startswith(prefix):
            continue
        suffix = code[len(prefix):]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    candidate = max_n + 1
    while True:
        code = f"HDC-{candidate:05d}"
        if not Project.query.filter_by(project_code=code).first():
            return code
        candidate += 1

def _next_worker_code():
    prefix = "HDC-WORKER-"
    max_n = 0
    for w in Worker.query.filter(Worker.worker_code.like(prefix + '%')).all():
        last = (w.worker_code or '').replace(prefix, '')
        if last.isdigit():
            max_n = max(max_n, int(last))
    return f"{prefix}{(max_n + 1):06d}"

def _next_office_staff_code():
    prefix = "HDC-OFFICE-"
    max_n = 0
    for s in OfficeStaff.query.filter(OfficeStaff.staff_code.like(prefix + '%')).all():
        last = (s.staff_code or '').replace(prefix, '')
        if last.isdigit():
            max_n = max(max_n, int(last))
    return f"{prefix}{(max_n + 1):06d}"


def _material_stock_map(project_id=None, stage_id=None):
    """
    Return stock summary per material id:
    { material_id: {'purchased': x, 'used': y, 'remaining': z} }
    """
    pur_q = db.session.query(
        Purchase.material_id,
        func.coalesce(func.sum(Purchase.qty), 0.0)
    )
    use_q = db.session.query(
        MaterialUsage.material_id,
        func.coalesce(func.sum(MaterialUsage.qty), 0.0)
    )
    if stage_id:
        pur_q = pur_q.filter(Purchase.stage_id == stage_id)
        use_q = use_q.filter(MaterialUsage.stage_id == stage_id)
    elif project_id:
        pur_q = pur_q.filter(Purchase.project_id == project_id)
        use_q = use_q.filter(MaterialUsage.project_id == project_id)

    purchases = dict(
        pur_q.group_by(Purchase.material_id).all()
    )
    usages = dict(
        use_q.group_by(MaterialUsage.material_id).all()
    )
    out = {}
    for m in Material.query.all():
        purchased = float(purchases.get(m.id, 0.0) or 0.0)
        used = float(usages.get(m.id, 0.0) or 0.0)
        out[m.id] = {
            'purchased': purchased,
            'used': used,
            'remaining': purchased - used
        }
    return out


def _material_stock_for_scope(material_id, project_id=None, stage_id=None):
    stock_map = _material_stock_map(project_id=project_id, stage_id=stage_id)
    row = stock_map.get(int(material_id or 0), {}) if material_id else {}
    return float(row.get('remaining', 0.0) or 0.0)


def _aggregate_stage_costs(stage_ids):
    stage_ids = [int(sid) for sid in (stage_ids or []) if sid]
    if not stage_ids:
        return {}

    attendance_map = dict(
        db.session.query(
            Attendance.stage_id,
            func.coalesce(func.sum(Attendance.total_wage), 0.0)
        )
        .outerjoin(TimeEntry, TimeEntry.attendance_id == Attendance.id)
        .filter(Attendance.stage_id.in_(stage_ids), TimeEntry.id.is_(None))
        .group_by(Attendance.stage_id)
        .all()
    )
    time_map = dict(
        db.session.query(
            TimeEntry.stage_id,
            func.coalesce(func.sum(TimeEntry.wage_calculated), 0.0)
        )
        .filter(TimeEntry.stage_id.in_(stage_ids), TimeEntry.is_void == False)
        .group_by(TimeEntry.stage_id)
        .all()
    )
    material_map = dict(
        db.session.query(
            Purchase.stage_id,
            func.coalesce(func.sum(Purchase.total), 0.0)
        )
        .filter(Purchase.stage_id.in_(stage_ids))
        .group_by(Purchase.stage_id)
        .all()
    )
    material_v2_map = dict(
        db.session.query(
            UsageLogV2.stage_id,
            func.coalesce(func.sum(UsageLogV2.cost), 0.0)
        )
        .filter(UsageLogV2.stage_id.in_(stage_ids), UsageLogV2.is_void == False)
        .group_by(UsageLogV2.stage_id)
        .all()
    )
    expense_map = dict(
        db.session.query(
            Expense.stage_id,
            func.coalesce(func.sum(Expense.amount), 0.0)
        )
        .filter(Expense.stage_id.in_(stage_ids), Expense.is_void == False)
        .group_by(Expense.stage_id)
        .all()
    )
    stage_key = func.coalesce(SubcontractPayment.stage_id, Subcontractor.stage_id)
    subcontract_map = dict(
        db.session.query(
            stage_key.label('stage_id'),
            func.coalesce(func.sum(SubcontractPayment.amount), 0.0)
        )
        .select_from(Subcontractor)
        .join(SubcontractPayment, SubcontractPayment.subcontractor_id == Subcontractor.id)
        .filter(stage_key.in_(stage_ids), SubcontractPayment.is_void == False)
        .group_by(stage_key)
        .all()
    )

    out = {}
    for sid in stage_ids:
        labour = float(attendance_map.get(sid, 0.0) or 0.0) + float(time_map.get(sid, 0.0) or 0.0)
        material = float(material_map.get(sid, 0.0) or 0.0) + float(material_v2_map.get(sid, 0.0) or 0.0)
        expense = float(expense_map.get(sid, 0.0) or 0.0)
        subcontract = float(subcontract_map.get(sid, 0.0) or 0.0)
        total = labour + material + expense + subcontract
        out[sid] = {
            'labour': labour,
            'material': material,
            'expense': expense,
            'subcontract': subcontract,
            'total': total
        }
    return out


def _apply_aggregated_stage_costs(stages):
    stage_rows = [s for s in (stages or []) if s and s.id]
    if not stage_rows:
        return {}
    metrics = _aggregate_stage_costs([s.id for s in stage_rows])
    for s in stage_rows:
        m = metrics.get(s.id, {})
        labour = float(m.get('labour', 0.0) or 0.0)
        material = float(m.get('material', 0.0) or 0.0)
        expense = float(m.get('expense', 0.0) or 0.0)
        subcontract = float(m.get('subcontract', 0.0) or 0.0)
        total = float(m.get('total', 0.0) or 0.0)
        s._agg_stage_labour_cost = labour
        s._agg_stage_material_cost = material
        s._agg_stage_expense_cost = expense
        s._agg_stage_subcontract_cost = subcontract
        s._agg_stage_total_cost = total
        s._agg_stage_profit = float((s.contract_value or 0.0) - total)
    return metrics


def _aggregate_project_costs(project_ids):
    project_ids = [int(pid) for pid in (project_ids or []) if pid]
    if not project_ids:
        return {}

    received_map = dict(
        db.session.query(
            OwnerPayment.project_id,
            func.coalesce(func.sum(OwnerPayment.amount), 0.0)
        )
        .filter(OwnerPayment.project_id.in_(project_ids), OwnerPayment.is_void == False)
        .group_by(OwnerPayment.project_id)
        .all()
    )
    attendance_map = dict(
        db.session.query(
            Attendance.project_id,
            func.coalesce(func.sum(Attendance.total_wage), 0.0)
        )
        .outerjoin(TimeEntry, TimeEntry.attendance_id == Attendance.id)
        .filter(Attendance.project_id.in_(project_ids), TimeEntry.id.is_(None))
        .group_by(Attendance.project_id)
        .all()
    )
    time_map = dict(
        db.session.query(
            TimeEntry.project_id,
            func.coalesce(func.sum(TimeEntry.wage_calculated), 0.0)
        )
        .filter(TimeEntry.project_id.in_(project_ids), TimeEntry.is_void == False)
        .group_by(TimeEntry.project_id)
        .all()
    )
    material_map = dict(
        db.session.query(
            Purchase.project_id,
            func.coalesce(func.sum(Purchase.total), 0.0)
        )
        .filter(Purchase.project_id.in_(project_ids))
        .group_by(Purchase.project_id)
        .all()
    )
    material_v2_map = dict(
        db.session.query(
            UsageLogV2.project_id,
            func.coalesce(func.sum(UsageLogV2.cost), 0.0)
        )
        .filter(UsageLogV2.project_id.in_(project_ids), UsageLogV2.is_void == False)
        .group_by(UsageLogV2.project_id)
        .all()
    )
    expense_map = dict(
        db.session.query(
            Expense.project_id,
            func.coalesce(func.sum(Expense.amount), 0.0)
        )
        .filter(Expense.project_id.in_(project_ids), Expense.is_void == False)
        .group_by(Expense.project_id)
        .all()
    )
    subcontract_map = dict(
        db.session.query(
            Subcontractor.project_id,
            func.coalesce(func.sum(SubcontractPayment.amount), 0.0)
        )
        .join(SubcontractPayment, SubcontractPayment.subcontractor_id == Subcontractor.id)
        .filter(Subcontractor.project_id.in_(project_ids), SubcontractPayment.is_void == False)
        .group_by(Subcontractor.project_id)
        .all()
    )
    stage_contract_map = dict(
        db.session.query(
            Stage.project_id,
            func.coalesce(func.sum(
                case(
                    (Stage.contract_basis == 'Lump Sum', func.coalesce(Stage.lump_sum_value, 0.0)),
                    (Stage.contract_basis == 'Per Sq Ft',
                     (func.coalesce(Stage.rate_per_sqft, 0.0) - func.coalesce(Stage.discount_per_sqft, 0.0))
                     * func.coalesce(Stage.qty_sqft, 0.0)),
                    else_=0.0
                )
            ), 0.0)
        )
        .filter(Stage.project_id.in_(project_ids))
        .group_by(Stage.project_id)
        .all()
    )

    out = {}
    for pid in project_ids:
        labour = float(attendance_map.get(pid, 0.0) or 0.0) + float(time_map.get(pid, 0.0) or 0.0)
        material = float(material_map.get(pid, 0.0) or 0.0) + float(material_v2_map.get(pid, 0.0) or 0.0)
        expense = float(expense_map.get(pid, 0.0) or 0.0)
        subcontract = float(subcontract_map.get(pid, 0.0) or 0.0)
        received = float(received_map.get(pid, 0.0) or 0.0)
        out[pid] = {
            'labour': labour,
            'material': material,
            'expense': expense,
            'subcontract': subcontract,
            'received': received,
            'stage_contract': float(stage_contract_map.get(pid, 0.0) or 0.0)
        }
    return out


def _apply_aggregated_project_costs(projects):
    project_rows = [p for p in (projects or []) if p and p.id]
    if not project_rows:
        return {}
    metrics = _aggregate_project_costs([p.id for p in project_rows])
    for p in project_rows:
        m = metrics.get(p.id, {})
        labour = float(m.get('labour', 0.0) or 0.0)
        material = float(m.get('material', 0.0) or 0.0)
        expense = float(m.get('expense', 0.0) or 0.0)
        subcontract = float(m.get('subcontract', 0.0) or 0.0)
        received = float(m.get('received', 0.0) or 0.0)
        total = labour + material + expense + subcontract
        p._agg_total_received = received
        p._agg_total_labour_cost = labour
        p._agg_total_material_cost = material
        p._agg_total_expense_cost = expense
        p._agg_total_subcontract_cost = subcontract
        p._agg_total_cost = total
        p._agg_net_profit = float((p.owner_contract_value or 0.0) - total)
        p._agg_remaining_receivable = float((p.owner_contract_value or 0.0) - received)
        p._agg_stage_contract_value = float(m.get('stage_contract', 0.0) or 0.0)
    return metrics


def _running_projects_receivable_rows():
    projects = (Project.query
                .order_by(Project.name.asc(), Project.id.asc())
                .all())
    _apply_aggregated_project_costs(projects)
    rows = []
    for p in projects:
        status = str(getattr(p, 'status', '') or '').strip().lower()
        if status in ('completed', 'closed', 'cancelled', 'canceled', 'inactive'):
            continue
        rows.append({
            'id': int(p.id),
            'name': p.name,
            'status': (p.status or 'active'),
            'total_received': float(p.total_received or 0.0),
            'remaining_receivable': float(p.remaining_receivable or 0.0),
        })
    return rows


def _run_admin_maintenance(project_id=None):
    stats = {
        'subcontract_link_updates': 0,
        'workers_processed': 0
    }
    stats['subcontract_link_updates'] = int(_reconcile_subcontract_links() or 0)
    q = db.session.query(TimeEntry.worker_id).filter(TimeEntry.worker_id.isnot(None))
    if project_id:
        q = q.filter(TimeEntry.project_id == project_id)
    worker_ids = [wid for (wid,) in q.distinct().all() if wid]
    for wid in worker_ids:
        _reconcile_worker_time_entries(wid)
        _repair_worker_work_ledger_links(wid)
    db.session.commit()
    stats['workers_processed'] = len(worker_ids)
    return stats

def _create_project_from_estimation(rows, project_meta, estimation_id=None):
    project_code = (project_meta.get('project_code') if project_meta else '') or ''
    name = (project_meta.get('name') if project_meta else '') or ''
    client = (project_meta.get('client') if project_meta else '') or ''
    client_phone = (project_meta.get('client_phone') if project_meta else '') or ''
    location = (project_meta.get('location') if project_meta else '') or ''
    project_code = (project_code or '').strip().upper()
    if not project_code or Project.query.filter_by(project_code=project_code).first():
        project_code = _next_project_code()
    if not name:
        name = project_code or f"Estimation Project {_pkt_now_naive().strftime('%Y-%m-%d %H:%M')}"
    grand_total = sum(r.get('total', 0) for r in rows)
    p = Project(
        project_code=project_code,
        name=name,
        client=client,
        client_phone=client_phone,
        location=location,
        total_constructed_sqft=0.0,
        owner_rate_per_sqft=0.0,
        owner_lump_sum=grand_total,
        contract_type='lump_sum',
        start_date=_pkt_today(),
        status='Active',
        estimation_id=estimation_id,
        budget_total=grand_total,
        planned_start=_pkt_today())
    db.session.add(p); db.session.commit()

    defs = {}
    for r in rows:
        stage_name = r.get('stage_name', '').strip()
        if not stage_name:
            continue
        basis = 'Lump Sum' if r.get('type') == 'lump_sum' else 'Per Sq Ft'
        rate = _flt(r.get('rate'))
        qty = _flt(r.get('quantity'))
        lump = rate if r.get('type') == 'lump_sum' else 0.0
        if r.get('type') == 'sqft':
            lump = 0.0
        def_id = defs.get(stage_name.lower())
        s = Stage(
            project_id=p.id,
            definition_id=def_id,
            name=stage_name,
            status='Active',
            estimated_cost=_flt(r.get('total')),
            progress=0.0,
            contract_basis=basis,
            rate_per_sqft=rate if r.get('type') == 'sqft' else 0.0,
            discount_per_sqft=0.0,
            qty_sqft=qty if r.get('type') == 'sqft' else 0.0,
            lump_sum_value=lump,
            original_contract_basis=basis,
            original_rate_per_sqft=rate if r.get('type') == 'sqft' else 0.0,
            original_discount_per_sqft=0.0,
            original_qty_sqft=qty if r.get('type') == 'sqft' else 0.0,
            original_lump_sum_value=lump)
        db.session.add(s)
    db.session.commit()
    return p

_UNIT_TO_M = {
    'mm': 0.001,
    'cm': 0.01,
    'm': 1.0,
    'ft': 0.3048,
    'in': 0.0254,
}

_UNIT_TO_MM = {
    'mm': 1.0,
    'cm': 10.0,
    'm': 1000.0,
    'ft': 304.8,
    'in': 25.4,
}

def _to_meters(value, unit):
    return float(value or 0) * _UNIT_TO_M.get(unit or 'm', 1.0)

def _to_mm(value, unit):
    return float(value or 0) * _UNIT_TO_MM.get(unit or 'mm', 1.0)

def _admin_only():
    if current_user.role != 'admin':
        flash('Admin access required.', 'danger')
        return True
    return False


# â”€â”€ Login Manager â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@login_manager.user_loader
def load_user(uid):
    return db.session.get(HDCUser, int(uid))


# â”€â”€ Auth â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/login', methods=['GET', 'POST'])
def hdc_login():
    if current_user.is_authenticated:
        return redirect(url_for('hdc_dashboard'))
    if request.method == 'POST':
        u = HDCUser.query.filter_by(username=request.form.get('username','').strip()).first()
        if u and check_password_hash(u.password_hash, request.form.get('password','')):
            login_user(u)
            log_action(u, 'login', f'User {u.username} logged in.', 'session', u.id)
            _record_user_activity(
                event_type='login',
                entity_type='session',
                entity_id=str(u.id),
                summary=f'User login: {u.username}',
                changed={},
                force_commit=True
            )
            return redirect(url_for('hdc_dashboard'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')

@app.route('/hdc/logout', methods=['POST'])
@login_required
def hdc_logout():
    log_action(current_user, 'logout', f'User {current_user.username} logged out.', 'session', current_user.id)
    _record_user_activity(
        event_type='logout',
        entity_type='session',
        entity_id=str(getattr(current_user, 'id', '') or ''),
        summary=f'User logout: {getattr(current_user, "username", "unknown")}',
        changed={},
        force_commit=True
    )
    logout_user()
    return redirect(url_for('hdc_login'))

@app.route('/')
def hdc_root():
    return redirect(url_for('hdc_dashboard'))


# â”€â”€ Dashboard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/')
@login_required
def hdc_dashboard():
    projects = Project.query.all()
    _apply_aggregated_project_costs(projects)
    active   = [p for p in projects if str(p.status).lower() in ('active','planned','active ')]
    project_ids = [int(p.id) for p in projects if p and p.id]
    received_map = {}
    if project_ids:
        received_map = dict(
            db.session.query(
                OwnerPayment.project_id,
                func.coalesce(func.sum(OwnerPayment.amount), 0.0)
            )
            .filter(OwnerPayment.project_id.in_(project_ids), OwnerPayment.is_void == False)
            .group_by(OwnerPayment.project_id)
            .all()
        )

    total_contract        = sum(p.owner_contract_value for p in projects)
    total_received        = sum(float(received_map.get(int(p.id), 0.0) or 0.0) for p in projects)
    total_cost            = sum(p.total_cost for p in projects)
    gross_margin          = sum(p.gross_margin for p in projects)
    office_expense_total  = _office_expense_total()
    personal_expense_total = _personal_expense_total()
    net_profit            = sum(p.net_profit for p in projects) - office_expense_total
    pending_receivable    = total_contract - total_received

    chart_projects  = projects[:8]
    chart_labels    = [p.name[:15] for p in chart_projects]
    chart_contracts = [round(p.owner_contract_value, 0) for p in chart_projects]
    chart_expenses  = [round(p.total_cost, 0) for p in chart_projects]
    chart_project_ids = [int(p.id) for p in chart_projects]

    exp_cats   = (db.session.query(ExpenseCategory.name, func.sum(Expense.amount))
                  .join(Expense, Expense.category_id == ExpenseCategory.id)
                  .filter(Expense.is_void == False)
                  .group_by(ExpenseCategory.name)
                  .all())
    pie_labels = [c[0] or 'Other' for c in exp_cats]
    pie_values = [round(c[1] or 0, 0) for c in exp_cats]

    today = _pkt_today()
    today_expenses = float(db.session.query(func.coalesce(func.sum(Expense.amount), 0.0))
                           .filter(Expense.date == today, Expense.is_void == False)
                           .scalar() or 0.0)
    start_dt = datetime.combine(today, datetime.min.time())
    end_dt = datetime.combine(today, datetime.max.time())
    workers_present = int(db.session.query(func.count(func.distinct(TimeEntry.worker_id)))
                          .filter(
                              TimeEntry.is_void == False,
                              TimeEntry.check_in >= start_dt,
                              TimeEntry.check_in <= end_dt
                          )
                          .scalar() or 0)
    delayed_stages = int(db.session.query(func.count(Stage.id))
                         .filter(
                             Stage.end_date.isnot(None),
                             Stage.end_date < today,
                             func.lower(func.trim(func.coalesce(Stage.status, ''))).notin_(['completed', 'complete'])
                         )
                         .scalar() or 0)
    budget_overruns = sum(1 for p in projects if (p.budget_total or 0) > 0 and p.actual_cost > (p.budget_total or 0))

    return render_template('dashboard.html',
        active_count=len(active), total_project_count=len(projects),
        total_contract=total_contract, total_received=total_received,
        total_expenses=total_cost, gross_margin=gross_margin,
        net_profit=net_profit, pending_receivable=pending_receivable,
        office_expense_total=office_expense_total,
        personal_expense_total=personal_expense_total,
        chart_labels=chart_labels, chart_contracts=chart_contracts,
        chart_expenses=chart_expenses, chart_project_ids=chart_project_ids,
        pie_labels=pie_labels, pie_values=pie_values,
        recent_projects=projects[:5],
        today_expenses=today_expenses, workers_present=workers_present,
        delayed_stages=delayed_stages, budget_overruns=budget_overruns)


@app.route('/hdc/kpi/<string:metric>')
@login_required
def hdc_kpi_detail(metric):
    metric = (metric or '').strip().lower()
    today = _pkt_today()
    page_title = 'KPI Detail'
    subtitle = ''
    columns = []
    rows = []
    grand_total = 0.0
    grand_total_label = 'Grand Total'
    value_format = 'number'
    show_table_footer = False
    breakdown_totals = None
    if metric == 'active_projects':
        active = [p for p in Project.query.order_by(Project.created_at.desc()).all() if str(p.status).lower().strip() in ('active', 'planned')]
        page_title = 'Active Projects'
        subtitle = f'{len(active)} active/planned project(s)'
        columns = [
            {'key': 'code', 'label': 'Code'},
            {'key': 'name', 'label': 'Project'},
            {'key': 'client', 'label': 'Owner/Client'},
            {'key': 'location', 'label': 'Location'},
            {'key': 'status', 'label': 'Status'},
        ]
        rows = [{
            'code': p.project_code,
            'name': p.name,
            'client': p.client or '-',
            'location': p.location or '-',
            'status': p.status or '-',
        } for p in active]
        grand_total = float(len(rows))
        grand_total_label = 'Active Projects Count'
    elif metric == 'total_expenses':
        projects = Project.query.order_by(Project.created_at.desc()).all()
        page_title = 'Total Expenses (PKR)'
        subtitle = 'Each spend event is listed below with project-wise subtotal rows'
        columns = [
            {'key': 'dt', 'label': 'Date/Time'},
            {'key': 'code', 'label': 'Code'},
            {'key': 'project', 'label': 'Project'},
            {'key': 'stage', 'label': 'Stage'},
            {'key': 'cost_head', 'label': 'Cost Head'},
            {'key': 'to_whom', 'label': 'To Whom'},
            {'key': 'details', 'label': 'Details'},
            {'key': 'amount', 'label': 'Amount', 'align': 'text-end', 'format': 'currency'},
        ]
        labour_sum = 0.0
        material_sum = 0.0
        expense_sum = 0.0
        subcontract_sum = 0.0
        grand_total = 0.0
        worker_name_map = {w.id: (w.name or f'Worker #{w.id}') for w in Worker.query.all()}
        for p in projects:
            project_rows = []
            project_total = 0.0

            time_rows = (TimeEntry.query
                         .filter(TimeEntry.project_id == p.id, TimeEntry.is_void == False)
                         .order_by(TimeEntry.activity_at.asc(), TimeEntry.id.asc())
                         .all())
            for t in time_rows:
                amount = float(t.wage_calculated or 0.0)
                if abs(amount) < 1e-9:
                    continue
                labour_sum += amount
                project_total += amount
                project_rows.append({
                    'dt': t.activity_at or t.check_in,
                    'code': p.project_code,
                    'project': p.name,
                    'stage': t.stage.name if t.stage else '-',
                    'cost_head': 'Labour',
                    'to_whom': worker_name_map.get(t.worker_id, f'Worker #{t.worker_id}'),
                    'details': f'Hours {float(t.hours or 0.0):,.2f} | OT {float(t.overtime or 0.0):,.2f}',
                    'amount': amount,
                })

            att_rows = (Attendance.query
                        .filter(Attendance.project_id == p.id)
                        .order_by(Attendance.activity_at.asc(), Attendance.id.asc())
                        .all())
            migrated_att_ids = {
                int(aid) for (aid,) in db.session.query(TimeEntry.attendance_id)
                .filter(
                    TimeEntry.project_id == p.id,
                    TimeEntry.attendance_id.isnot(None)
                )
                .all()
                if aid
            }
            for a in att_rows:
                if a.id in migrated_att_ids:
                    continue
                amount = float(a.total_wage or 0.0)
                if abs(amount) < 1e-9:
                    continue
                labour_sum += amount
                project_total += amount
                project_rows.append({
                    'dt': a.activity_at or datetime.combine(a.date, datetime.min.time()),
                    'code': p.project_code,
                    'project': p.name,
                    'stage': a.stage.name if a.stage else '-',
                    'cost_head': 'Labour (Legacy)',
                    'to_whom': worker_name_map.get(a.worker_id, f'Worker #{a.worker_id}'),
                    'details': f'Legacy attendance | Hours {float(a.hours_worked or 0.0):,.2f}',
                    'amount': amount,
                })

            pur_rows = (Purchase.query
                        .filter(Purchase.project_id == p.id)
                        .order_by(Purchase.activity_at.asc(), Purchase.id.asc())
                        .all())
            for pur in pur_rows:
                amount = float(pur.total or 0.0)
                if abs(amount) < 1e-9:
                    continue
                material_sum += amount
                project_total += amount
                mat_name = pur.material.name if pur.material else f'Material #{pur.material_id}'
                ptype = (pur.entry_type or 'purchase').strip().lower()
                is_return = (ptype == 'return') or amount < 0
                project_rows.append({
                    'dt': pur.activity_at or datetime.combine(pur.date or _pkt_today(), datetime.min.time()),
                    'code': p.project_code,
                    'project': p.name,
                    'stage': pur.stage.name if pur.stage else '-',
                    'cost_head': ('Material Return' if is_return else 'Material Purchase'),
                    'to_whom': pur.supplier_name or mat_name,
                    'details': f'{mat_name} | Qty {float(pur.qty or 0.0):,.2f} @ {float(pur.rate or 0.0):,.2f}',
                    'amount': amount,
                })

            exp_rows = (Expense.query
                        .filter(Expense.project_id == p.id)
                        .order_by(Expense.activity_at.asc(), Expense.id.asc())
                        .all())
            for e in exp_rows:
                amount = float(e.amount or 0.0)
                if abs(amount) < 1e-9:
                    continue
                expense_sum += amount
                project_total += amount
                to_whom = '-'
                if getattr(e, 'tip_worker_id', None):
                    to_whom = worker_name_map.get(e.tip_worker_id, f'Worker #{e.tip_worker_id}')
                elif e.remarks:
                    to_whom = e.remarks
                project_rows.append({
                    'dt': e.activity_at or datetime.combine(e.date or _pkt_today(), datetime.min.time()),
                    'code': p.project_code,
                    'project': p.name,
                    'stage': e.stage.name if e.stage else '-',
                    'cost_head': f'Expense: {(e.category or "General")}',
                    'to_whom': to_whom,
                    'details': e.remarks or '-',
                    'amount': amount,
                })

            sub_pay_rows = (SubcontractPayment.query
                            .join(Subcontractor, Subcontractor.id == SubcontractPayment.subcontractor_id)
                            .filter(Subcontractor.project_id == p.id, SubcontractPayment.is_void == False)
                            .order_by(SubcontractPayment.activity_at.asc(), SubcontractPayment.id.asc())
                            .all())
            for sp in sub_pay_rows:
                amount = float(sp.amount or 0.0)
                if abs(amount) < 1e-9:
                    continue
                subcontract_sum += amount
                project_total += amount
                srow = sp.subcontractor
                etype = (sp.entry_type or 'payment').strip().lower()
                project_rows.append({
                    'dt': sp.activity_at or sp.created_at,
                    'code': p.project_code,
                    'project': p.name,
                    'stage': sp.stage.name if sp.stage else (srow.stage_rel.name if srow and srow.stage_rel else '-'),
                    'cost_head': ('Subcontract Settlement' if etype == 'settlement' else 'Subcontract Payment'),
                    'to_whom': srow.name if srow else f'Subcontractor #{sp.subcontractor_id}',
                    'details': sp.notes or '-',
                    'amount': amount,
                })

            project_rows.sort(key=lambda r: (r.get('dt') or datetime.min, r.get('cost_head') or ''))
            rows.extend(project_rows)
            if project_rows:
                rows.append({
                    '_is_subtotal': True,
                    'dt': None,
                    'code': p.project_code,
                    'project': p.name,
                    'stage': '',
                    'cost_head': 'Subtotal',
                    'to_whom': '',
                    'details': f'Subtotal for {p.project_code}',
                    'amount': project_total,
                })
                grand_total += project_total

        grand_total_label = page_title
        value_format = 'currency'
        show_table_footer = True
        breakdown_totals = {
            'labour': labour_sum,
            'material': material_sum,
            'expense': expense_sum,
            'subcontract': subcontract_sum,
            'total': grand_total
        }
    elif metric in ('contract_value', 'total_received', 'pending_receivable', 'gross_margin', 'net_profit'):
        projects = Project.query.order_by(Project.created_at.desc()).all()
        _apply_aggregated_project_costs(projects)
        project_ids = [int(p.id) for p in projects if p and p.id]
        received_map = {}
        if project_ids:
            received_map = dict(
                db.session.query(
                    OwnerPayment.project_id,
                    func.coalesce(func.sum(OwnerPayment.amount), 0.0)
                )
                .filter(OwnerPayment.project_id.in_(project_ids), OwnerPayment.is_void == False)
                .group_by(OwnerPayment.project_id)
                .all()
            )
        metric_meta = {
            'contract_value': ('Contract Value (PKR)', 'Contract value by project', lambda p: float(p.owner_contract_value or 0.0)),
            'total_received': ('Total Received (PKR)', 'Owner payments received by project', lambda p: float(received_map.get(int(p.id), 0.0) or 0.0)),
            'pending_receivable': ('Pending Receivable (PKR)', 'Pending receivable by project', lambda p: float((p.owner_contract_value or 0.0) - float(received_map.get(int(p.id), 0.0) or 0.0))),
            'gross_margin': ('Gross Margin (PKR)', 'Gross margin by project', lambda p: float(p.gross_margin or 0.0)),
            'net_profit': ('Net Profit (PKR)', 'Net profit by project', lambda p: float(p.net_profit or 0.0)),
        }
        title, sub, value_fn = metric_meta[metric]
        page_title = title
        subtitle = sub
        columns = [
            {'key': 'code', 'label': 'Code'},
            {'key': 'project', 'label': 'Project'},
            {'key': 'client', 'label': 'Owner/Client'},
            {'key': 'status', 'label': 'Status'},
            {'key': 'amount', 'label': 'Amount', 'align': 'text-end', 'format': 'currency'},
        ]
        for p in projects:
            amount = value_fn(p)
            rows.append({
                'code': p.project_code,
                'project': p.name,
                'client': p.client or '-',
                'status': p.status or '-',
                'amount': amount,
            })
        if metric == 'net_profit':
            office_expense_total = _office_expense_total()
            if abs(office_expense_total) > 1e-9:
                rows.append({
                    'code': 'OFFICE',
                    'project': 'Office Overhead (Non-project)',
                    'client': '-',
                    'status': '-',
                    'amount': -float(office_expense_total or 0.0),
                })
        grand_total = float(sum(r['amount'] for r in rows))
        grand_total_label = title
        value_format = 'currency'
        show_table_footer = True
    elif metric == 'today_expenses':
        exp_rows = (Expense.query
                    .filter(Expense.date == today)
                    .order_by(Expense.activity_at.desc(), Expense.id.desc())
                    .all())
        page_title = "Today's Expenses"
        subtitle = today.isoformat()
        columns = [
            {'key': 'dt', 'label': 'Date/Time'},
            {'key': 'project', 'label': 'Project'},
            {'key': 'stage', 'label': 'Stage'},
            {'key': 'category', 'label': 'Category'},
            {'key': 'remarks', 'label': 'Remarks'},
            {'key': 'amount', 'label': 'Amount', 'align': 'text-end', 'format': 'currency'},
        ]
        for e in exp_rows:
            rows.append({
                'dt': e.activity_at or datetime.combine(e.date or today, datetime.min.time()),
                'project': e.project.name if getattr(e, 'project', None) else '-',
                'stage': e.stage.name if getattr(e, 'stage', None) else '-',
                'category': e.category or '-',
                'remarks': e.remarks or '-',
                'amount': float(e.amount or 0.0),
            })
        grand_total = float(sum(r['amount'] for r in rows))
        grand_total_label = "Today's Expenses Total"
        value_format = 'currency'
        show_table_footer = True
    elif metric == 'workers_present':
        start_dt = datetime.combine(today, datetime.min.time())
        end_dt = datetime.combine(today, datetime.max.time())
        entries = (TimeEntry.query
                   .filter(
                       TimeEntry.is_void == False,
                       TimeEntry.check_in >= start_dt,
                       TimeEntry.check_in <= end_dt
                   )
                   .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
                   .all())
        seen = set()
        page_title = 'Workers Present'
        subtitle = today.isoformat()
        columns = [
            {'key': 'check_in', 'label': 'Check In'},
            {'key': 'worker', 'label': 'Worker'},
            {'key': 'project', 'label': 'Project'},
            {'key': 'stage', 'label': 'Stage'},
            {'key': 'hours', 'label': 'Hours', 'align': 'text-end', 'format': 'float2'},
        ]
        for t in entries:
            if t.worker_id in seen:
                continue
            seen.add(t.worker_id)
            rows.append({
                'check_in': t.check_in,
                'worker': t.worker.name if t.worker else f'Worker #{t.worker_id}',
                'project': t.project.name if t.project else '-',
                'stage': t.stage.name if t.stage else '-',
                'hours': float(t.hours or 0.0),
            })
        grand_total = float(len(rows))
        grand_total_label = 'Workers Present Count'
    elif metric == 'delayed_stages':
        stage_rows = (Stage.query
                      .filter(Stage.end_date.isnot(None))
                      .order_by(Stage.end_date.asc(), Stage.id.asc())
                      .all())
        page_title = 'Delayed Stages'
        subtitle = f'As of {today.isoformat()}'
        columns = [
            {'key': 'project', 'label': 'Project'},
            {'key': 'stage', 'label': 'Stage'},
            {'key': 'planned_end', 'label': 'Planned End'},
            {'key': 'status', 'label': 'Status'},
            {'key': 'days_delayed', 'label': 'Days Delayed', 'align': 'text-end', 'format': 'number'},
        ]
        for s in stage_rows:
            if not s.end_date:
                continue
            if s.end_date >= today:
                continue
            if str(s.status or '').lower() in ('completed', 'complete'):
                continue
            days_delayed = (today - s.end_date).days
            rows.append({
                'project': s.project.name if s.project else '-',
                'stage': s.name,
                'planned_end': s.end_date.isoformat(),
                'status': s.status or '-',
                'days_delayed': int(days_delayed),
            })
        grand_total = float(len(rows))
        grand_total_label = 'Delayed Stages Count'
    elif metric == 'budget_overruns':
        projects = Project.query.order_by(Project.created_at.desc()).all()
        page_title = 'Budget Overruns'
        subtitle = 'Projects where actual cost is above budget'
        columns = [
            {'key': 'code', 'label': 'Code'},
            {'key': 'project', 'label': 'Project'},
            {'key': 'budget', 'label': 'Budget', 'align': 'text-end', 'format': 'currency'},
            {'key': 'actual_cost', 'label': 'Actual Cost', 'align': 'text-end', 'format': 'currency'},
            {'key': 'overrun', 'label': 'Overrun', 'align': 'text-end', 'format': 'currency'},
        ]
        for p in projects:
            budget = float(p.budget_total or 0.0)
            actual = float(p.actual_cost or 0.0)
            if budget <= 0 or actual <= budget:
                continue
            overrun = actual - budget
            rows.append({
                'code': p.project_code,
                'project': p.name,
                'budget': budget,
                'actual_cost': actual,
                'overrun': overrun,
            })
        grand_total = float(sum(float(r['overrun'] or 0.0) for r in rows))
        grand_total_label = 'Total Overrun'
        value_format = 'currency'
        show_table_footer = True
    else:
        flash('Unsupported KPI detail requested.', 'warning')
        return redirect(url_for('hdc_dashboard'))

    return render_template(
        'kpi_detail.html',
        metric=metric,
        page_title=page_title,
        subtitle=subtitle,
        columns=columns,
        rows=rows,
        grand_total=grand_total,
        grand_total_label=grand_total_label,
        value_format=value_format,
        show_table_footer=show_table_footer,
        breakdown_totals=breakdown_totals
    )


# â”€â”€ Projects â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/projects')
@login_required
def hdc_projects():
    projects = Project.query.order_by(Project.created_at.desc()).all()
    _apply_aggregated_project_costs(projects)
    return render_template('projects.html', projects=projects)

@app.route('/hdc/projects/add', methods=['GET', 'POST'])
@login_required
def hdc_add_project():
    if request.method == 'POST':
        code = (request.form.get('project_code','') or '').strip().upper()
        if not code or Project.query.filter_by(project_code=code).first():
            code = _next_project_code()
        name = (request.form.get('name','') or '').strip()
        if not name:
            name = " ".join(x for x in [
                (request.form.get('client','') or '').strip(),
                (request.form.get('location','') or '').strip()
            ] if x) or code
        total_sqft = _flt(request.form.get('total_sqft'))
        owner_rate = _flt(request.form.get('owner_rate'))
        owner_lump = _flt(request.form.get('lump_sum'))
        # Contract type is inferred on add form (field removed).
        if owner_lump > 0 and owner_rate > 0 and total_sqft > 0:
            inferred_contract_type = 'mixed'
        elif owner_lump > 0 and not (owner_rate > 0 and total_sqft > 0):
            inferred_contract_type = 'lump_sum'
        else:
            inferred_contract_type = 'sqft'
        p = Project(
            project_code=code,
            name=name,
            client=request.form.get('client','').strip(),
            client_phone=request.form.get('client_phone','').strip(),
            location=request.form.get('location','').strip(),
            total_constructed_sqft=total_sqft,
            owner_rate_per_sqft=owner_rate,
            owner_lump_sum=owner_lump,
            contract_type=inferred_contract_type,
            start_date=_parse_date(request.form.get('start_date')),
            status=request.form.get('status','active'),
            planned_start=_parse_date(request.form.get('start_date')),
            planned_end=_parse_date(request.form.get('planned_end')) if request.form.get('planned_end') else None)
        db.session.add(p); db.session.commit()
        flash(f'Project "{p.name}" added.', 'success')
        return redirect(url_for('hdc_project_detail', pid=p.id))
    return render_template('add_project.html', today=_pkt_today().isoformat())

@app.route('/hdc/projects/<int:pid>')
@login_required
def hdc_project_detail(pid):
    p        = Project.query.get_or_404(pid)
    workers  = Worker.query.filter_by(active_status=True).all()
    stages   = Stage.query.filter_by(project_id=pid).order_by(Stage.id).all()
    _apply_aggregated_stage_costs(stages)
    _apply_aggregated_project_costs([p])
    stage_defs = (StageDefinition.query
                  .filter_by(project_id=pid, active_status=True)
                  .order_by(StageDefinition.default_order, StageDefinition.id)
                  .all())
    materials  = Material.query.filter_by(is_active=True).all()
    subcontractor_pool = Subcontractor.query.order_by(Subcontractor.name.asc(), Subcontractor.id.asc()).all()
    recent_time_entries = (TimeEntry.query
                           .filter(TimeEntry.project_id == pid, TimeEntry.is_void == False)
                           .order_by(TimeEntry.check_in.desc(), TimeEntry.id.desc())
                           .limit(10)
                           .all())
    sqft_stages = [s for s in stages if (str(s.contract_basis or '').strip().lower() == 'per sq ft') and float(s.qty_sqft or 0.0) > 0]
    if sqft_stages:
        project_total_sqft = float(sum(float(s.qty_sqft or 0.0) for s in sqft_stages))
        owner_sqft_charges = float(sum((float(s.effective_rate or 0.0) * float(s.qty_sqft or 0.0)) for s in sqft_stages))
    else:
        project_total_sqft = float(p.total_constructed_sqft or 0.0)
        owner_sqft_charges = project_total_sqft * float(p.owner_rate_per_sqft or 0.0)
    owner_sqft_rate = (owner_sqft_charges / project_total_sqft) if project_total_sqft > 0 else 0.0
    sqft_metrics_valid = bool(project_total_sqft > 0 and owner_sqft_rate > 0)
    cost_per_sqft = (float(p.total_cost or 0.0) / project_total_sqft) if project_total_sqft > 0 else 0.0
    profit_per_sqft = ((owner_sqft_charges - float(p.total_cost or 0.0)) / project_total_sqft) if project_total_sqft > 0 else 0.0
    owner_payments = (OwnerPayment.query
                      .filter_by(project_id=pid, is_void=False)
                      .order_by(OwnerPayment.date.desc(), OwnerPayment.id.desc())
                      .all())
    owner_payments_voided = (OwnerPayment.query
                             .filter_by(project_id=pid, is_void=True)
                             .order_by(OwnerPayment.voided_at.desc(), OwnerPayment.id.desc())
                             .all())
    receiving_accounts = (Account.query
                          .filter(
                              Account.is_void == False,
                              func.lower(func.coalesce(Account.status, 'active')) == 'active',
                              func.lower(func.coalesce(Account.type, '')).in_(_ACCOUNT_COMPANY_TYPES)
                          )
                          .order_by(Account.name.asc(), Account.id.asc())
                          .all())
    project_receivable_rows = _running_projects_receivable_rows()
    return render_template('project_detail.html',
        p=p, workers=workers, stages=stages,
        stage_defs=stage_defs, materials=materials,
        subcontractor_pool=subcontractor_pool,
        receiving_accounts=receiving_accounts,
        project_receivable_rows=project_receivable_rows,
        owner_payments=owner_payments,
        owner_payments_voided=owner_payments_voided,
        recent_time_entries=recent_time_entries,
        project_total_sqft=project_total_sqft,
        owner_sqft_charges=owner_sqft_charges,
        owner_sqft_rate=owner_sqft_rate,
        sqft_metrics_valid=sqft_metrics_valid,
        cost_per_sqft=cost_per_sqft,
        profit_per_sqft=profit_per_sqft,
        show_cost_split=(request.args.get('show_cost_split') == '1'),
        today=_pkt_today().isoformat())

@app.route('/hdc/stage/<int:sid>/ledger')
@login_required
def hdc_stage_ledger(sid):
    s = Stage.query.get_or_404(sid)
    rows, totals, grand_total = _build_stage_event_ledger(s)
    stage_sqft = float(s.qty_sqft or 0.0)
    stage_is_sqft = (str(s.contract_basis or '').strip().lower() == 'per sq ft') and stage_sqft > 0
    stage_owner_sqft_rate = float(s.effective_rate or 0.0) if stage_is_sqft else 0.0
    stage_owner_sqft_charges = stage_sqft * stage_owner_sqft_rate if stage_is_sqft else 0.0
    stage_cost_per_sqft = (float(grand_total or 0.0) / stage_sqft) if stage_sqft > 0 else 0.0
    stage_profit_per_sqft = ((stage_owner_sqft_charges - float(grand_total or 0.0)) / stage_sqft) if stage_sqft > 0 else 0.0
    return render_template(
        'stage_ledger.html',
        s=s,
        p=s.project,
        rows=rows,
        totals=totals,
        grand_total=grand_total,
        stage_sqft=stage_sqft,
        stage_is_sqft=stage_is_sqft,
        stage_owner_sqft_rate=stage_owner_sqft_rate,
        stage_owner_sqft_charges=stage_owner_sqft_charges,
        stage_cost_per_sqft=stage_cost_per_sqft,
        stage_profit_per_sqft=stage_profit_per_sqft,
        show_cost_split=(request.args.get('show_cost_split') == '1')
    )


@app.route('/hdc/cost-entries/<string:scope>/<int:target_id>/<string:head>')
@login_required
def hdc_cost_entries(scope, target_id, head):
    scope = (scope or '').strip().lower()
    head = (head or '').strip().lower()
    if scope not in ('project', 'stage') or head not in ('labour', 'material', 'expense'):
        flash('Invalid cost detail request.', 'warning')
        return redirect(url_for('hdc_dashboard'))

    if scope == 'project':
        obj = Project.query.get_or_404(target_id)
        page_title = f'{obj.name} - {head.title()} Entries'
        subtitle = f'Project {obj.project_code}'
        back_url = url_for('hdc_project_detail', pid=obj.id, show_cost_split=1)
        filter_project_id = obj.id
        filter_stage_id = None
    else:
        obj = Stage.query.get_or_404(target_id)
        page_title = f'{obj.name} - {head.title()} Entries'
        subtitle = f'Project {obj.project.project_code if obj.project else ""}'
        back_url = url_for('hdc_stage_ledger', sid=obj.id, show_cost_split=1)
        filter_project_id = obj.project_id
        filter_stage_id = obj.id

    columns = [
        {'key': 'dt', 'label': 'Date/Time'},
        {'key': 'project', 'label': 'Project'},
        {'key': 'stage', 'label': 'Stage'},
        {'key': 'entry_type', 'label': 'Type'},
        {'key': 'to_whom', 'label': 'To/From'},
        {'key': 'details', 'label': 'Details'},
        {'key': 'amount', 'label': 'Amount', 'align': 'text-end', 'format': 'currency'},
    ]
    rows = []

    if head == 'labour':
        worker_name_map = {w.id: (w.name or f'Worker #{w.id}') for w in Worker.query.all()}
        tq = TimeEntry.query.filter(TimeEntry.is_void == False)
        if filter_project_id:
            tq = tq.filter(TimeEntry.project_id == filter_project_id)
        if filter_stage_id:
            tq = tq.filter(TimeEntry.stage_id == filter_stage_id)
        for t in tq.order_by(TimeEntry.activity_at.asc(), TimeEntry.id.asc()).all():
            rows.append({
                'dt': t.activity_at or t.check_in,
                'project': t.project.name if t.project else '-',
                'stage': t.stage.name if t.stage else '-',
                'entry_type': 'Time Entry',
                'to_whom': worker_name_map.get(t.worker_id, f'Worker #{t.worker_id}'),
                'details': f'Hours {float(t.hours or 0.0):,.2f} | OT {float(t.overtime or 0.0):,.2f}',
                'amount': float(t.wage_calculated or 0.0),
            })
        aq = Attendance.query
        if filter_project_id:
            aq = aq.filter(Attendance.project_id == filter_project_id)
        if filter_stage_id:
            aq = aq.filter(Attendance.stage_id == filter_stage_id)
        for a in aq.order_by(Attendance.activity_at.asc(), Attendance.id.asc()).all():
            rows.append({
                'dt': a.activity_at or datetime.combine(a.date, datetime.min.time()),
                'project': a.project.name if a.project else '-',
                'stage': a.stage.name if a.stage else '-',
                'entry_type': 'Legacy Attendance',
                'to_whom': worker_name_map.get(a.worker_id, f'Worker #{a.worker_id}'),
                'details': f'Hours {float(a.hours_worked or 0.0):,.2f}',
                'amount': float(a.total_wage or 0.0),
            })
    elif head == 'material':
        pq = Purchase.query
        if filter_project_id:
            pq = pq.filter(Purchase.project_id == filter_project_id)
        if filter_stage_id:
            pq = pq.filter(Purchase.stage_id == filter_stage_id)
        for pur in pq.order_by(Purchase.activity_at.asc(), Purchase.id.asc()).all():
            mat_name = pur.material.name if pur.material else f'Material #{pur.material_id}'
            ptype = (pur.entry_type or 'purchase').strip().lower()
            rows.append({
                'dt': pur.activity_at or datetime.combine(pur.date or _pkt_today(), datetime.min.time()),
                'project': pur.project.name if pur.project else '-',
                'stage': pur.stage.name if pur.stage else '-',
                'entry_type': ('Material Return' if ptype == 'return' else 'Material Purchase'),
                'to_whom': pur.supplier_name or mat_name,
                'details': f'{mat_name} | Qty {float(pur.qty or 0.0):,.2f} @ {float(pur.rate or 0.0):,.2f}',
                'amount': float(pur.total or 0.0),
            })
        uq = UsageLogV2.query.filter(UsageLogV2.is_void == False)
        if filter_project_id:
            uq = uq.filter(UsageLogV2.project_id == filter_project_id)
        if filter_stage_id:
            uq = uq.filter(UsageLogV2.stage_id == filter_stage_id)
        for u in uq.order_by(UsageLogV2.created_at.asc(), UsageLogV2.id.asc()).all():
            mat_name = u.material.name if u.material else f'Material #{u.material_id}'
            unit_cost = (float(u.cost or 0.0) / float(u.quantity or 1.0)) if float(u.quantity or 0.0) > 0 else 0.0
            rows.append({
                'dt': u.created_at or datetime.combine(u.date or _pkt_today(), datetime.min.time()),
                'project': u.project.name if u.project else '-',
                'stage': u.stage.name if u.stage else '-',
                'entry_type': 'Material Usage (V2)',
                'to_whom': mat_name,
                'details': f'{mat_name} | Qty {float(u.quantity or 0.0):,.2f} @ {unit_cost:,.2f}',
                'amount': float(u.cost or 0.0),
            })
    else:
        eq = Expense.query.filter(Expense.is_void == False)
        if filter_project_id:
            eq = eq.filter(Expense.project_id == filter_project_id)
        if filter_stage_id:
            eq = eq.filter(Expense.stage_id == filter_stage_id)
        for e in eq.order_by(Expense.activity_at.asc(), Expense.id.asc()).all():
            rows.append({
                'dt': e.activity_at or datetime.combine(e.date or _pkt_today(), datetime.min.time()),
                'project': e.project.name if getattr(e, 'project', None) else '-',
                'stage': e.stage.name if getattr(e, 'stage', None) else '-',
                'entry_type': f'Expense: {e.category or "General"}',
                'to_whom': (e.remarks or '-'),
                'details': e.remarks or '-',
                'amount': float(e.amount or 0.0),
            })

    rows.sort(key=lambda r: (r.get('dt') or datetime.min, r.get('entry_type') or ''))
    grand_total = float(sum(float(r.get('amount') or 0.0) for r in rows))
    return render_template(
        'kpi_detail.html',
        metric=f'{scope}_{head}',
        page_title=page_title,
        subtitle=subtitle,
        columns=columns,
        rows=rows,
        grand_total=grand_total,
        grand_total_label=f'{head.title()} Total',
        value_format='currency',
        show_table_footer=True,
        breakdown_totals=None,
        back_url=back_url
    )

@app.route('/hdc/projects/<int:pid>/edit', methods=['GET', 'POST'])
@login_required
def hdc_edit_project(pid):
    p = Project.query.get_or_404(pid)
    if request.method == 'POST':
        p.name                  = request.form.get('name','').strip()
        p.client                = request.form.get('client','').strip()
        p.client_phone          = request.form.get('client_phone','').strip()
        p.location              = request.form.get('location','').strip()
        p.total_constructed_sqft= _flt(request.form.get('total_sqft'))
        p.owner_rate_per_sqft   = _flt(request.form.get('owner_rate'))
        p.owner_lump_sum        = _flt(request.form.get('lump_sum'))
        p.contract_type         = request.form.get('contract_type','sqft')
        p.status                = request.form.get('status','active')
        p.planned_end           = _parse_date(request.form.get('planned_end')) if request.form.get('planned_end') else None
        db.session.commit()
        flash('Project updated.', 'success')
        return redirect(url_for('hdc_project_detail', pid=pid))
    return render_template('edit_project.html', p=p)

@app.route('/hdc/projects/<int:pid>/owner_payment', methods=['POST'])
@login_required
def hdc_add_owner_payment(pid):
    prj = Project.query.get_or_404(pid)
    pay_date = _parse_date(request.form.get('date'))
    amount = _flt(request.form.get('amount'))
    received_to_account_id = request.form.get('received_to_account_id', type=int)
    if amount <= 0:
        flash('Payment amount must be greater than zero.', 'danger')
        return redirect(url_for('hdc_project_detail', pid=pid))
    recv_acc = None
    if received_to_account_id:
        recv_acc = Account.query.get(int(received_to_account_id))
    if (not recv_acc) or recv_acc.is_void or str(recv_acc.status or 'active').strip().lower() != 'active' \
       or str(recv_acc.type or '').strip().lower() not in _ACCOUNT_COMPANY_TYPES:
        flash('Select a valid active receiving account (company/cash/bank).', 'danger')
        return redirect(url_for('hdc_project_detail', pid=pid))
    remarks = (request.form.get('remarks','') or '').strip()
    if _has_recent_duplicate(
        OwnerPayment,
        project_id=pid,
        received_to_account_id=int(recv_acc.id),
        amount=amount,
        date=pay_date,
        remarks=remarks
    ):
        flash('Duplicate owner payment prevented (same values submitted too quickly).', 'warning')
        return redirect(url_for('hdc_project_detail', pid=pid))
    op_row = OwnerPayment(
        project_id=pid, amount=amount,
        date=pay_date,
        received_to_account_id=int(recv_acc.id),
        activity_at=_activity_at_for(pay_date),
        remarks=remarks)
    db.session.add(op_row)
    db.session.flush()
    ok_txn, msg_txn, _ = _accounts_post_owner_receipt(op_row, project=prj, commit=False)
    if not ok_txn:
        db.session.rollback()
        flash(msg_txn or 'Unable to post owner payment in unified accounts.', 'danger')
        return redirect(url_for('hdc_project_detail', pid=pid))
    db.session.commit()
    flash('Payment recorded.', 'success')
    return redirect(url_for('hdc_project_detail', pid=pid))


@app.route('/hdc/projects/<int:pid>/owner_payment/<int:oid>/receipt')
@login_required
def hdc_owner_payment_receipt(pid, oid):
    op = OwnerPayment.query.get_or_404(oid)
    if int(op.project_id or 0) != int(pid):
        abort(404)
    prj = Project.query.get(op.project_id) if op.project_id else None
    receipt_id = f"RCPT-OP-{op.id:08d}"
    return render_template(
        'transaction_receipt.html',
        company_profile=_receipt_company_profile(),
        receipt_id=receipt_id,
        created_at=(op.activity_at or op.created_at or _pkt_now_naive()),
        tx_type='Owner Payment Receipt',
        party_name=((prj.client if prj else '') or 'Client'),
        project_name=(prj.name if prj else '-'),
        stage_name='-',
        account_used=((op.received_to_account.name if op.received_to_account else 'Company Cash')),
        amount=float(op.amount or 0.0),
        amount_words=_amount_to_words(op.amount or 0.0),
        note=(op.remarks or ''),
        reference_id=f'owner_payment#{op.id}',
        recent_entries=_owner_payment_recent_entries(pid, exclude_id=op.id, limit=5),
        recent_entries_title='Last 5 Owner Payment Entries',
        back_url=url_for('hdc_project_detail', pid=pid),
        print_label='Print / Save PDF'
    )

@app.route('/hdc/projects/<int:pid>/owner_payment/<int:oid>/delete', methods=['POST'])
@login_required
def hdc_delete_owner_payment(pid, oid):
    op = OwnerPayment.query.get_or_404(oid)
    if int(op.project_id or 0) != int(pid):
        flash('Payment does not belong to selected project.', 'danger')
        return redirect(url_for('hdc_project_detail', pid=pid))
    if op.is_void:
        flash('Payment is already voided.', 'info')
        return redirect(url_for('hdc_project_detail', pid=pid))
    op.is_void = True
    op.void_reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
    op.voided_at = _pkt_now_naive()
    _accounts_set_void_by_source('owner_payment', op.id, True)
    db.session.commit()
    flash('Payment voided.', 'warning')
    return redirect(url_for('hdc_project_detail', pid=pid))


@app.route('/hdc/projects/<int:pid>/owner_payment/<int:oid>/restore', methods=['POST'])
@login_required
def hdc_restore_owner_payment(pid, oid):
    op = OwnerPayment.query.get_or_404(oid)
    if int(op.project_id or 0) != int(pid):
        flash('Payment does not belong to selected project.', 'danger')
        return redirect(url_for('hdc_project_detail', pid=pid))
    if not op.is_void:
        flash('Payment is already active.', 'info')
        return redirect(url_for('hdc_project_detail', pid=pid))
    op.is_void = False
    op.void_reason = None
    op.voided_at = None
    _accounts_set_void_by_source('owner_payment', op.id, False)
    db.session.commit()
    flash('Payment restored.', 'success')
    return redirect(url_for('hdc_project_detail', pid=pid))

@app.route('/hdc/projects/<int:pid>/add_subcontractor', methods=['POST'])
@login_required
def hdc_add_subcontractor(pid):
    Project.query.get_or_404(pid)
    stage_id = request.form.get('stage_id', type=int)
    if stage_id:
        stage = Stage.query.get(stage_id)
        if (not stage) or (stage.project_id != pid):
            flash('Selected stage does not belong to this project.', 'danger')
            return redirect(url_for('hdc_project_detail', pid=pid))
    name = (request.form.get('name','') or '').strip()
    if not name:
        flash('Subcontractor name is required.', 'warning')
        return redirect(url_for('hdc_project_detail', pid=pid))
    code = _next_subcontractor_code()
    while Subcontractor.query.filter(func.lower(Subcontractor.subcontractor_code) == code.lower()).first():
        code = _next_subcontractor_code()
    sub = Subcontractor(
        subcontractor_code=code,
        project_id=pid, stage_id=None,
        name=name,
        phone=(request.form.get('phone','') or '').strip(),
        work_type='',
        contract_type='lump_sum',
        rate_per_sqft=0.0,
        total_sqft=0.0,
        lump_sum_amount=0.0,
        retention_percentage=0.0,
        work_done_percentage=0.0)
    db.session.add(sub)
    db.session.flush()
    _log_subcontract_event(
        sub=sub,
        event_type='create',
        to_value=f'{sub.subcontractor_code or ""} {sub.name}',
        notes='Subcontractor profile created'
    )
    db.session.commit()
    flash(f'Subcontractor added ({code}).', 'success')
    return redirect(url_for('hdc_project_detail', pid=pid))

@app.route('/hdc/subcontractor/<int:sid>/pay', methods=['POST'])
@login_required
def hdc_pay_subcontractor(sid):
    sub = Subcontractor.query.get_or_404(sid)
    return_to = (request.form.get('return_to') or '').strip().lower()
    pay_date = _parse_date(request.form.get('date'))
    amount = _flt(request.form.get('amount'))
    settle_shortfall = (request.form.get('settle_shortfall') or '').strip().lower() in ('1', 'true', 'on', 'yes')
    notes = (request.form.get('notes','') or '').strip()
    project_id = request.form.get('project_id', type=int)
    stage_id = request.form.get('stage_id', type=int)
    stg = None
    if stage_id:
        stg = Stage.query.get(stage_id)
        if not stg:
            flash('Selected stage is invalid.', 'danger')
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
        if project_id and stg.project_id != project_id:
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
        project_id = stg.project_id
    if project_id and (not Project.query.get(project_id)):
        flash('Selected project is invalid.', 'danger')
        return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
    if amount <= 0:
        flash('Payment amount must be greater than zero.', 'danger')
        return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))

    allowed_balance = float(sub.contract_balance or 0.0)
    scope_name = 'All Stages'
    if stg:
        snap = _subcontract_stage_snapshot(sub, stg)
        allowed_balance = float(snap.get('contract_balance') or snap.get('balance') or 0.0)
        scope_name = stg.name

    if amount > (allowed_balance + 1e-6):
        flash(f'Payment exceeds remaining contract balance ({allowed_balance:,.0f} PKR). Overpayment is blocked to avoid duplicate cost.', 'danger')
        return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))

    payment_part = min(amount, allowed_balance)
    settlement_part = 0.0
    if settle_shortfall and payment_part < allowed_balance:
        settlement_part = allowed_balance - payment_part

    scope_project_id = project_id or (stg.project_id if stg else sub.project_id)
    scope_stage_id = stage_id if stg else None
    if (settlement_part > 0) and not scope_project_id:
        flash('Select project/stage scope for settlement posting.', 'warning')
        return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))

    pay_row = None
    if payment_part > 0:
        if _has_recent_duplicate(
            SubcontractPayment,
            subcontractor_id=sid,
            project_id=project_id,
            stage_id=stage_id,
            entry_type='payment',
            amount=payment_part,
            date=pay_date,
            notes=notes
        ):
            flash('Duplicate subcontract payment prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
        pay_row = SubcontractPayment(
            subcontractor_id=sid,
            project_id=project_id,
            stage_id=stage_id,
            entry_type='payment',
            amount=payment_part,
            date=pay_date,
            activity_at=_activity_at_for(pay_date),
            notes=notes
        )
        db.session.add(pay_row)
        db.session.flush()
        ok_txn, msg_txn, _ = _accounts_post_subcontract_payment_row(pay_row, subcontractor_name=sub.name, commit=False)
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to post subcontract payment in unified accounts.', 'danger')
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
        _log_subcontract_event(
            sub=sub,
            event_type='payment',
            amount=payment_part,
            notes=f'{notes or "Subcontract payment recorded"} | Scope: {scope_name}',
            project_id=project_id,
            stage_id=stage_id
        )

    if settlement_part > 0:
        settlement_notes = (notes + ' | ' if notes else '') + f'Subcontract settlement shortfall for {sub.name} | SETTLE_SUBCONTRACTOR_ID:{sid}'
        if _has_recent_duplicate(
            SubcontractPayment,
            subcontractor_id=sid,
            project_id=project_id,
            stage_id=stage_id,
            entry_type='settlement',
            amount=settlement_part,
            date=pay_date,
            notes=settlement_notes
        ):
            flash('Duplicate subcontract settlement prevented.', 'warning')
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
        db.session.add(SubcontractPayment(
            subcontractor_id=sid,
            project_id=project_id,
            stage_id=stage_id,
            entry_type='settlement',
            amount=settlement_part,
            date=pay_date,
            activity_at=_activity_at_for(pay_date),
            notes=settlement_notes
        ))
        settlement_cat = _ensure_expense_category('Settlement')
        if _has_recent_duplicate(
            Expense,
            project_id=scope_project_id,
            stage_id=scope_stage_id,
            category_id=(settlement_cat.id if settlement_cat else None),
            amount=-settlement_part,
            date=pay_date,
            remarks=settlement_notes
        ):
            flash('Duplicate subcontract settlement expense prevented.', 'warning')
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
        db.session.add(Expense(
            project_id=scope_project_id,
            stage_id=scope_stage_id,
            category_id=(settlement_cat.id if settlement_cat else None),
            amount=-settlement_part,
            date=pay_date,
            activity_at=_activity_at_for(pay_date),
            remarks=settlement_notes
        ))
        _log_subcontract_event(
            sub=sub,
            event_type='settlement',
            amount=settlement_part,
            notes=f'Shortfall settled to close payable | Scope: {scope_name}',
            project_id=scope_project_id,
            stage_id=scope_stage_id
        )
    db.session.commit()
    if settlement_part > 0:
        flash(
            f'Payment recorded: {payment_part:,.0f} PKR cash paid and {settlement_part:,.0f} PKR shortfall settled.',
            'success'
        )
    else:
        flash(f'Payment to {sub.name} recorded.', 'success')
    if payment_part > 0 and pay_row is not None and pay_row.id:
        return redirect(url_for('hdc_subcontractor_payment_receipt', sid=sub.id, pid=pay_row.id))
    if return_to == 'ledger':
        return redirect(url_for('hdc_subcontractor_ledger', sid=sub.id))
    if return_to == 'payment_page':
        return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
    if sub.project_id:
        return redirect(url_for('hdc_project_detail', pid=sub.project_id))
    return redirect(url_for('hdc_subcontractor_ledger', sid=sub.id))


@app.route('/hdc/subcontractor/<int:sid>/payment/<int:pid>/receipt')
@login_required
def hdc_subcontractor_payment_receipt(sid, pid):
    sub = Subcontractor.query.get_or_404(sid)
    row = SubcontractPayment.query.get_or_404(pid)
    if row.subcontractor_id != sid:
        abort(404)
    receipt_id = f'RCPT-SP-{row.id:08d}'
    project_name = (row.project.name if row.project else '-')
    stage_name = (row.stage.name if row.stage else '-')
    recent = (
        SubcontractPayment.query
        .filter(SubcontractPayment.subcontractor_id == sid, SubcontractPayment.id != row.id, SubcontractPayment.is_void == False)
        .order_by(SubcontractPayment.activity_at.desc(), SubcontractPayment.id.desc())
        .limit(5).all()
    )
    recent_entries = [{
        'date': (r.date.strftime('%Y-%m-%d') if r.date else '-'),
        'type': (r.entry_type or 'payment').title(),
        'direction': 'pay',
        'party': sub.name,
        'amount': float(r.amount or 0),
        'receipt_url': url_for('hdc_subcontractor_payment_receipt', sid=sid, pid=r.id)
    } for r in recent]
    return render_template(
        'transaction_receipt.html',
        company_profile=_receipt_company_profile(),
        receipt_id=receipt_id,
        created_at=(row.activity_at or _pkt_now_naive()),
        tx_type=f'Subcontractor {(row.entry_type or "Payment").title()} Receipt',
        party_name=sub.name,
        project_name=project_name,
        stage_name=stage_name,
        account_used='-',
        amount=float(row.amount or 0),
        amount_words=_amount_to_words(row.amount or 0),
        note=(row.notes or ''),
        reference_id=f'subcontract_payment#{row.id}',
        recent_entries=recent_entries,
        recent_entries_title=f'Last 5 Subcontractor Entries – {sub.name}',
        back_url=url_for('hdc_subcontractor_payment_page', sid=sid),
        print_label='Print / Save PDF'
    )


@app.route('/hdc/subcontractor/<int:sid>/payments')
@login_required
def hdc_subcontractor_payment_page(sid):
    sub = Subcontractor.query.get_or_404(sid)
    stage_rows = _subcontract_scope_stages(sub)
    project_map = {}
    stage_options = []
    stage_snapshots = {}
    for st in stage_rows:
        if st.project:
            project_map[st.project.id] = st.project
        snap = _subcontract_stage_snapshot(sub, st)
        stage_snapshots[str(st.id)] = {
            'project_id': st.project_id,
            'stage_name': st.name,
            'project_name': st.project.name if st.project else f'Project#{st.project_id}',
            'payable': float(snap.get('payable') or 0.0),
            'paid': float(snap.get('paid') or 0.0),
            'settled': float(snap.get('settled') or 0.0),
            'cleared': float(snap.get('cleared') or 0.0),
            'balance': float(snap.get('balance') or 0.0),
            'contract_balance': float(snap.get('contract_balance') or 0.0),
            'live_balance': float(snap.get('live_balance') or 0.0),
            'advance_paid': float(snap.get('advance_paid') or 0.0),
            'progress': float(snap.get('progress') or 0.0),
            'contract': float(snap.get('contract') or 0.0)
        }
        stage_options.append(st)
    projects = sorted(project_map.values(), key=lambda p: (p.name or '').lower())

    payments = (SubcontractPayment.query
                .filter(SubcontractPayment.subcontractor_id == sub.id, SubcontractPayment.is_void == False)
                .order_by(SubcontractPayment.date.desc(), SubcontractPayment.id.desc())
                .all())
    return render_template(
        'subcontractor_payment.html',
        sub=sub,
        projects=projects,
        stage_options=stage_options,
        stage_snapshots=stage_snapshots,
        payments=payments
    )


@app.route('/hdc/subcontractor/<int:sid>/attendance', methods=['POST'])
@login_required
def hdc_subcontractor_attendance(sid):
    sub = Subcontractor.query.get_or_404(sid)
    att_date = _parse_date(request.form.get('date'))
    present_count = request.form.get('present_count', type=int)
    work_done_pct = _flt(request.form.get('work_done_pct'))
    notes = (request.form.get('notes', '') or '').strip()

    if present_count is None:
        present_count = 0
    present_count = max(0, int(present_count))
    work_done_pct = max(0.0, min(100.0, float(work_done_pct or 0.0)))

    row = SubcontractAttendance.query.filter_by(subcontractor_id=sid, date=att_date).first()
    if row:
        old_pc = int(row.present_count or 0)
        old_pct = float(row.work_done_pct or 0.0)
        row.present_count = present_count
        row.work_done_pct = work_done_pct
        row.notes = notes
        row.activity_at = _activity_at_for(att_date)
        msg = 'Subcontract attendance updated.'
    else:
        db.session.add(SubcontractAttendance(
            subcontractor_id=sid,
            date=att_date,
            present_count=present_count,
            work_done_pct=work_done_pct,
            notes=notes,
            activity_at=_activity_at_for(att_date)
        ))
        msg = 'Subcontract attendance recorded.'
        old_pc = 0
        old_pct = 0.0
    _log_subcontract_event(
        sub=sub,
        event_type='attendance',
        from_value=f'P{old_pc} +{old_pct:.2f}%',
        to_value=f'P{present_count} +{work_done_pct:.2f}%',
        notes=f'{att_date.isoformat()} | {notes or "-"}'
    )
    db.session.commit()
    flash(msg, 'success')
    if sub.project_id:
        return redirect(url_for('hdc_project_detail', pid=sub.project_id))
    return redirect(url_for('hdc_subcontractor_ledger', sid=sub.id))


@app.route('/hdc/subcontractor/<int:sid>/labour_attendance', methods=['POST'])
@login_required
def hdc_subcontractor_labour_attendance(sid):
    sub = Subcontractor.query.get_or_404(sid)
    return_to = (request.form.get('return_to') or '').strip().lower()
    att_date = _parse_date(request.form.get('date'))

    def _sub_scope_ids():
        scope_stage_ids = set()
        scope_project_ids = set()
        if sub.project_id:
            scope_project_ids.add(int(sub.project_id))
            stage_ids = (db.session.query(Stage.id)
                         .filter(Stage.project_id == sub.project_id)
                         .all())
            scope_stage_ids.update(int(sidv) for (sidv,) in stage_ids if sidv)
        if sub.stage_id:
            scope_stage_ids.add(int(sub.stage_id))
            sub_stage = Stage.query.get(sub.stage_id)
            if sub_stage and sub_stage.project_id:
                scope_project_ids.add(int(sub_stage.project_id))
        assigned_stage_rows = (Stage.query
                               .filter(Stage.assigned_subcontractor_id == sub.id)
                               .all())
        for srow in assigned_stage_rows:
            scope_stage_ids.add(int(srow.id))
            if srow.project_id:
                scope_project_ids.add(int(srow.project_id))
        hist_stage_ids = (db.session.query(SubcontractLabourAttendance.stage_id)
                          .filter(SubcontractLabourAttendance.subcontractor_id == sub.id)
                          .distinct()
                          .all())
        for (sidv,) in hist_stage_ids:
            if sidv:
                scope_stage_ids.add(int(sidv))
        if scope_stage_ids:
            hist_stage_rows = Stage.query.filter(Stage.id.in_(list(scope_stage_ids))).all()
            for srow in hist_stage_rows:
                if srow.project_id:
                    scope_project_ids.add(int(srow.project_id))
        return scope_project_ids, scope_stage_ids

    scope_project_ids, scope_stage_ids = _sub_scope_ids()

    if (request.form.get('bulk_mode') or '').strip() == '1':
        worker_ids_raw = request.form.getlist('worker_ids')
        scoped_workers = (SubcontractLabourWorker.query
                          .filter(SubcontractLabourWorker.subcontractor_id == sub.id)
                          .all())
        worker_map = {int(w.id): w for w in scoped_workers}
        updated_rows = 0
        skipped_rows = 0

        for wid_raw in worker_ids_raw:
            try:
                wid = int(wid_raw)
            except Exception:
                skipped_rows += 1
                continue

            labour_worker = worker_map.get(wid)
            if not labour_worker:
                skipped_rows += 1
                continue

            row_status = (request.form.get(f'attendance_status_{wid}') or 'not_assigned').strip().lower()
            if row_status not in ('present', 'absent', 'not_assigned'):
                row_status = 'not_assigned'
            notes = (request.form.get(f'notes_{wid}') or '').strip()
            project_id = request.form.get(f'project_id_{wid}', type=int)
            stage_id = request.form.get(f'stage_id_{wid}', type=int)
            entered_hours = max(0.0, float(_flt(request.form.get(f'working_hours_{wid}'), 0.0) or 0.0))
            existing_rows = (SubcontractLabourAttendance.query
                             .filter(SubcontractLabourAttendance.subcontractor_id == sub.id,
                                     SubcontractLabourAttendance.worker_id == labour_worker.id,
                                     SubcontractLabourAttendance.date == att_date)
                             .order_by(SubcontractLabourAttendance.id.asc())
                             .all())

            if row_status == 'not_assigned':
                for er in existing_rows:
                    db.session.delete(er)
                updated_rows += 1
                continue

            if not project_id or not stage_id:
                skipped_rows += 1
                continue
            stg = Stage.query.get(stage_id)
            if (not stg) or (int(stg.project_id or 0) != int(project_id or 0)):
                skipped_rows += 1
                continue
            if scope_project_ids and int(project_id) not in scope_project_ids:
                skipped_rows += 1
                continue
            if scope_stage_ids and int(stg.id) not in scope_stage_ids:
                skipped_rows += 1
                continue
            if row_status == 'present' and entered_hours <= 0:
                skipped_rows += 1
                continue

            total_hours = entered_hours if row_status == 'present' else 0.0
            regular_hours = min(8.0, total_hours)
            overtime_hours = max(0.0, total_hours - 8.0)
            labour_count = 1 if row_status == 'present' else 0
            wage_rate = max(0.0, float(labour_worker.daily_wage or 0.0))
            total_labour_paid = wage_rate if row_status == 'present' else 0.0
            attendance_status = row_status.capitalize()

            if existing_rows:
                row = existing_rows[0]
                old_count = int(row.labour_count or 0)
                old_paid = float(row.total_labour_paid or 0.0)
                for extra in existing_rows[1:]:
                    db.session.delete(extra)
                row.project_id = project_id
                row.stage_id = stg.id
                row.worker_id = labour_worker.id
                row.labour_count = labour_count
                row.wage_rate = wage_rate
                row.total_labour_paid = total_labour_paid
                row.attendance_status = attendance_status
                row.working_hours = regular_hours
                row.overtime_hours = overtime_hours
                row.notes = notes
                row.activity_at = _activity_at_for(att_date)
                row.updated_at = _pkt_now_naive()
            else:
                old_count = 0
                old_paid = 0.0
                db.session.add(SubcontractLabourAttendance(
                    subcontractor_id=sub.id,
                    project_id=project_id,
                    stage_id=stg.id,
                    worker_id=labour_worker.id,
                    date=att_date,
                    labour_count=labour_count,
                    wage_rate=wage_rate,
                    total_labour_paid=total_labour_paid,
                    attendance_status=attendance_status,
                    working_hours=regular_hours,
                    overtime_hours=overtime_hours,
                    notes=notes,
                    activity_at=_activity_at_for(att_date),
                    created_at=_pkt_now_naive(),
                    updated_at=_pkt_now_naive()
                ))

            _log_subcontract_event(
                sub=sub,
                event_type='labour_attendance',
                from_value=f'L{old_count} | Cost {old_paid:.2f}',
                to_value=f'L{labour_count} | Cost {total_labour_paid:.2f}',
                amount=total_labour_paid,
                notes=f'{att_date.isoformat()} | {stg.name} | Worker: {labour_worker.name} | {attendance_status} | Hrs:{regular_hours:.2f} OT:{overtime_hours:.2f} | {notes or "-"}',
                project_id=project_id,
                stage_id=stg.id
            )
            updated_rows += 1

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Duplicate labour attendance detected. Existing rows were kept unchanged.', 'warning')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        flash(f'Register saved for {updated_rows} worker(s). Skipped {skipped_rows} invalid row(s).', 'success')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id, sheet_date=att_date.isoformat()))

    project_id = request.form.get('project_id', type=int)
    stage_id = request.form.get('stage_id', type=int)
    worker_id = request.form.get('worker_id', type=int)
    labour_count = request.form.get('labour_count', type=int)
    wage_rate = _flt(request.form.get('wage_rate'))
    total_labour_paid = _flt(request.form.get('total_labour_paid'))
    attendance_status = (request.form.get('attendance_status', 'Present') or 'Present').strip().lower()
    working_hours = _flt(request.form.get('working_hours'))
    notes = (request.form.get('notes', '') or '').strip()

    if labour_count is None:
        labour_count = 0
    labour_count = max(0, int(labour_count))
    wage_rate = max(0.0, float(wage_rate or 0.0))
    total_labour_paid = max(0.0, float(total_labour_paid or 0.0))
    total_hours = max(0.0, float(working_hours or 0.0))
    working_hours = min(8.0, total_hours)
    overtime_hours = max(0.0, total_hours - 8.0)
    if attendance_status not in ('present', 'absent', 'not_assigned'):
        attendance_status = 'present'
    attendance_status = attendance_status.capitalize()
    if attendance_status == 'Not_assigned':
        attendance_status = 'Not Assigned'

    if attendance_status == 'Not Assigned':
        flash('Use the register sheet to mark Not Assigned.', 'warning')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

    if not project_id:
        flash('Project is required for labour attendance.', 'warning')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    if not stage_id:
        flash('Stage is required for labour attendance.', 'warning')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

    stg = Stage.query.get(stage_id) if stage_id else None
    if not stg:
        flash('No stage selected for labour attendance.', 'warning')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    if int(stg.project_id or 0) != int(project_id or 0):
        flash('Selected stage does not belong to selected project.', 'danger')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

    if scope_project_ids and int(project_id) not in scope_project_ids:
        flash('Selected project is outside subcontractor assigned scope.', 'danger')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    if scope_stage_ids and int(stg.id) not in scope_stage_ids:
        flash('Selected stage is outside subcontractor assigned scope.', 'danger')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

    labour_worker = None
    if worker_id:
        labour_worker = SubcontractLabourWorker.query.get(worker_id)
        if not labour_worker or labour_worker.subcontractor_id != sub.id:
            flash('Selected labour worker is invalid for this subcontractor.', 'danger')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

    if total_labour_paid <= 0 and labour_count > 0 and wage_rate > 0:
        total_labour_paid = float(labour_count) * float(wage_rate)
    if wage_rate <= 0 and labour_count > 0 and total_labour_paid > 0:
        wage_rate = float(total_labour_paid) / float(labour_count)

    row = SubcontractLabourAttendance.query.filter_by(
        subcontractor_id=sub.id,
        stage_id=stg.id,
        worker_id=labour_worker.id if labour_worker else None,
        date=att_date
    ).first()
    if labour_worker:
        conflict_q = SubcontractLabourAttendance.query.filter(
            SubcontractLabourAttendance.subcontractor_id == sub.id,
            SubcontractLabourAttendance.worker_id == labour_worker.id,
            SubcontractLabourAttendance.date == att_date
        )
        if row:
            conflict_q = conflict_q.filter(SubcontractLabourAttendance.id != row.id)
        conflict_row = conflict_q.first()
        if conflict_row:
            conflict_stage = conflict_row.stage.name if conflict_row.stage else f'Stage#{conflict_row.stage_id}'
            flash(
                f'Duplicate not allowed: "{labour_worker.name}" already has attendance on {att_date.isoformat()} in {conflict_stage}.',
                'warning'
            )
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    if row:
        old_count = int(row.labour_count or 0)
        old_paid = float(row.total_labour_paid or 0.0)
        row.project_id = project_id
        row.worker_id = labour_worker.id if labour_worker else None
        row.labour_count = labour_count
        row.wage_rate = wage_rate
        row.total_labour_paid = total_labour_paid
        row.attendance_status = attendance_status
        row.working_hours = working_hours
        row.overtime_hours = overtime_hours
        row.notes = notes
        row.activity_at = _activity_at_for(att_date)
        row.updated_at = _pkt_now_naive()
        msg = 'Subcontract labour attendance updated.'
    else:
        if _has_recent_duplicate(
            SubcontractLabourAttendance,
            subcontractor_id=sub.id,
            project_id=project_id,
            stage_id=stg.id,
            worker_id=labour_worker.id if labour_worker else None,
            date=att_date,
            labour_count=labour_count,
            wage_rate=wage_rate,
            total_labour_paid=total_labour_paid,
            attendance_status=attendance_status,
            working_hours=working_hours,
            overtime_hours=overtime_hours,
            notes=notes
        ):
            flash('Duplicate subcontract labour attendance prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        db.session.add(SubcontractLabourAttendance(
            subcontractor_id=sub.id,
            project_id=project_id,
            stage_id=stg.id,
            worker_id=labour_worker.id if labour_worker else None,
            date=att_date,
            labour_count=labour_count,
            wage_rate=wage_rate,
            total_labour_paid=total_labour_paid,
            attendance_status=attendance_status,
            working_hours=working_hours,
            overtime_hours=overtime_hours,
            notes=notes,
            activity_at=_activity_at_for(att_date),
            created_at=_pkt_now_naive(),
            updated_at=_pkt_now_naive()
        ))
        old_count = 0
        old_paid = 0.0
        msg = 'Subcontract labour attendance recorded.'

    _log_subcontract_event(
        sub=sub,
        event_type='labour_attendance',
        from_value=f'L{old_count} | Cost {old_paid:.2f}',
        to_value=f'L{labour_count} | Cost {total_labour_paid:.2f}',
        amount=total_labour_paid,
        notes=f'{att_date.isoformat()} | {stg.name} | Worker: {(labour_worker.name if labour_worker else "Bulk Entry")} | {attendance_status} | Hrs:{working_hours:.2f} OT:{overtime_hours:.2f} | {notes or "-"}',
        project_id=project_id,
        stage_id=stg.id
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Duplicate labour attendance detected. Existing row was kept unchanged.', 'warning')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    flash(msg, 'success')
    if return_to == 'ledger':
        return redirect(url_for('hdc_subcontractor_ledger', sid=sub.id))
    return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))


@app.route('/hdc/subcontractor/<int:sid>/labour_attendance/<int:rid>/delete', methods=['POST'])
@login_required
def hdc_subcontractor_labour_attendance_delete(sid, rid):
    sub = Subcontractor.query.get_or_404(sid)
    row = SubcontractLabourAttendance.query.get_or_404(rid)
    if row.subcontractor_id != sub.id:
        flash('Attendance row does not belong to this subcontractor.', 'danger')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    old_count = int(row.labour_count or 0)
    old_paid = float(row.total_labour_paid or 0.0)
    old_date = row.date
    old_stage_id = row.stage_id
    old_stage_name = row.stage.name if row.stage else f'Stage#{row.stage_id}'
    old_worker_name = row.worker.name if row.worker else 'Bulk Entry'
    db.session.delete(row)
    _log_subcontract_event(
        sub=sub,
        event_type='labour_attendance_delete',
        from_value=f'L{old_count} | Cost {old_paid:.2f}',
        to_value='Deleted',
        amount=0.0,
        notes=f'{old_date.isoformat() if old_date else "-"} | {old_stage_name} | Worker: {old_worker_name} | Attendance row deleted',
        project_id=sub.project_id,
        stage_id=old_stage_id
    )
    db.session.commit()
    flash('Subcontract labour attendance entry deleted.', 'success')
    return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))


@app.route('/hdc/subcontractor/<int:sid>/workers', methods=['POST'])
@login_required
def hdc_subcontractor_add_worker(sid):
    sub = Subcontractor.query.get_or_404(sid)
    name = (request.form.get('name') or '').strip()
    phone = (request.form.get('phone') or '').strip()
    trade = (request.form.get('trade') or '').strip()
    daily_wage = max(0.0, _flt(request.form.get('daily_wage')))
    if not name:
        flash('Worker name is required.', 'warning')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    exists = SubcontractLabourWorker.query.filter(
        SubcontractLabourWorker.subcontractor_id == sub.id,
        func.lower(SubcontractLabourWorker.name) == name.lower(),
        SubcontractLabourWorker.active_status == True
    ).first()
    if exists:
        flash('A labour worker with this name already exists for this subcontractor.', 'warning')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

    w = SubcontractLabourWorker(
        subcontractor_id=sub.id,
        name=name,
        phone=phone,
        trade=trade,
        daily_wage=daily_wage,
        active_status=True
    )
    db.session.add(w)
    _log_subcontract_event(
        sub=sub,
        event_type='labour_worker_add',
        to_value=name,
        notes=f'Labour worker added | trade={trade or "-"} | wage={daily_wage:.2f}'
    )
    db.session.commit()
    flash(f'Labour worker "{name}" added.', 'success')
    return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))


@app.route('/hdc/subcontractor/<int:sid>/workers/<int:wid>/edit', methods=['POST'])
@login_required
def hdc_subcontractor_worker_edit(sid, wid):
    sub = Subcontractor.query.get_or_404(sid)
    w = SubcontractLabourWorker.query.get_or_404(wid)
    if w.subcontractor_id != sub.id:
        flash('Worker does not belong to this subcontractor.', 'danger')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    old_name = w.name or ''
    old_trade = w.trade or ''
    old_wage = float(w.daily_wage or 0.0)
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Worker name is required.', 'warning')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    w.name = name
    w.phone = (request.form.get('phone') or '').strip()
    w.trade = (request.form.get('trade') or '').strip()
    w.daily_wage = max(0.0, _flt(request.form.get('daily_wage')))
    _log_subcontract_event(
        sub=sub,
        event_type='labour_worker_edit',
        from_value=f'{old_name}|{old_trade}|{old_wage:.2f}',
        to_value=f'{w.name}|{w.trade or "-"}|{float(w.daily_wage or 0.0):.2f}',
        notes='Labour worker profile edited'
    )
    db.session.commit()
    flash(f'Worker "{w.name}" updated.', 'success')
    return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))


@app.route('/hdc/subcontractor/<int:sid>/workers/<int:wid>/toggle', methods=['POST'])
@login_required
def hdc_subcontractor_worker_toggle(sid, wid):
    sub = Subcontractor.query.get_or_404(sid)
    w = SubcontractLabourWorker.query.get_or_404(wid)
    if w.subcontractor_id != sub.id:
        flash('Worker does not belong to this subcontractor.', 'danger')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    w.active_status = not bool(w.active_status)
    _log_subcontract_event(
        sub=sub,
        event_type='labour_worker_status',
        from_value='active' if not w.active_status else 'suspended',
        to_value='active' if w.active_status else 'suspended',
        notes=f'Worker {w.name}'
    )
    db.session.commit()
    flash(f'Worker "{w.name}" is now {"active" if w.active_status else "suspended"}.', 'success')
    return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))


@app.route('/hdc/subcontractor/<int:sid>/workers/<int:wid>/pay', methods=['POST'])
@login_required
def hdc_subcontractor_worker_pay(sid, wid):
    sub = Subcontractor.query.get_or_404(sid)
    w = SubcontractLabourWorker.query.get_or_404(wid)
    if w.subcontractor_id != sub.id:
        flash('Worker does not belong to this subcontractor.', 'danger')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    amount = max(0.0, _flt(request.form.get('amount')))
    pay_date = _parse_date(request.form.get('date'))
    notes = (request.form.get('notes') or '').strip()
    if amount <= 0:
        flash('Payment amount must be greater than zero.', 'warning')
        return redirect(url_for('hdc_subcontractor_worker_ledger', sid=sub.id, wid=w.id))
    if amount > (w.payable_balance or 0.0) + 0.01:
        flash('Payment exceeds worker payable balance.', 'danger')
        return redirect(url_for('hdc_subcontractor_worker_ledger', sid=sub.id, wid=w.id))
    if _has_recent_duplicate(
        SubcontractLabourPayment,
        subcontractor_id=sub.id,
        worker_id=w.id,
        amount=amount,
        date=pay_date,
        notes=notes
    ):
        flash('Duplicate worker payment prevented (same values submitted too quickly).', 'warning')
        return redirect(url_for('hdc_subcontractor_worker_ledger', sid=sub.id, wid=w.id))
    pay_entry = SubcontractLabourPayment(
        subcontractor_id=sub.id,
        worker_id=w.id,
        amount=amount,
        date=pay_date,
        notes=notes,
        is_void=False,
        activity_at=_activity_at_for(pay_date)
    )
    db.session.add(pay_entry)
    db.session.flush()
    ok_txn, msg_txn, _ = _accounts_post_subcontract_labour_payment_row(
        pay_entry, worker_name=w.name, subcontractor_name=sub.name, commit=False
    )
    if not ok_txn:
        db.session.rollback()
        flash(msg_txn or 'Unable to post labour payment in unified accounts.', 'danger')
        return redirect(url_for('hdc_subcontractor_worker_ledger', sid=sub.id, wid=w.id))
    _log_subcontract_event(
        sub=sub,
        event_type='labour_worker_payment',
        amount=amount,
        notes=f'Worker {w.name} | {notes or "-"}'
    )
    db.session.commit()
    flash(f'Payment recorded for {w.name}.', 'success')
    return redirect(url_for('hdc_subcontractor_worker_ledger', sid=sub.id, wid=w.id))


@app.route('/hdc/subcontractor/<int:sid>/workers/<int:wid>/ledger')
@login_required
def hdc_subcontractor_worker_ledger(sid, wid):
    sub = Subcontractor.query.get_or_404(sid)
    w = SubcontractLabourWorker.query.get_or_404(wid)
    if w.subcontractor_id != sub.id:
        flash('Worker does not belong to this subcontractor.', 'danger')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
    from_raw = (request.args.get('date_from') or '').strip()
    to_raw = (request.args.get('date_to') or '').strip()
    date_from = _parse_date(from_raw, fallback=None) if from_raw else None
    date_to = _parse_date(to_raw, fallback=None) if to_raw else None
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    aq = SubcontractLabourAttendance.query.filter(
        SubcontractLabourAttendance.subcontractor_id == sub.id,
        SubcontractLabourAttendance.worker_id == w.id
    )
    pq = SubcontractLabourPayment.query.filter(
        SubcontractLabourPayment.subcontractor_id == sub.id,
        SubcontractLabourPayment.worker_id == w.id
    )
    if date_from:
        aq = aq.filter(SubcontractLabourAttendance.date >= date_from)
        pq = pq.filter(SubcontractLabourPayment.date >= date_from)
    if date_to:
        aq = aq.filter(SubcontractLabourAttendance.date <= date_to)
        pq = pq.filter(SubcontractLabourPayment.date <= date_to)
    attendance_rows = aq.order_by(SubcontractLabourAttendance.date.desc(), SubcontractLabourAttendance.id.desc()).all()
    payment_rows = pq.order_by(SubcontractLabourPayment.date.desc(), SubcontractLabourPayment.id.desc()).all()
    earned = sum(float(r.total_labour_paid or 0.0) for r in attendance_rows)
    paid = sum(float(p.amount or 0.0) for p in payment_rows)
    payable = max(0.0, earned - paid)
    return render_template(
        'subcontractor_worker_ledger.html',
        sub=sub,
        worker=w,
        attendance_rows=attendance_rows,
        payment_rows=payment_rows,
        earned=earned,
        paid=paid,
        payable=payable,
        selected_from=from_raw,
        selected_to=to_raw
    )


@app.route('/hdc/subcontractor/<int:sid>/attendance_page')
@login_required
def hdc_subcontractor_attendance_page(sid):
    sub = Subcontractor.query.get_or_404(sid)
    filter_worker_id = request.args.get('worker_id', type=int)
    show_inactive = request.args.get('show_inactive', type=int) == 1
    sheet_date = _parse_date(request.args.get('sheet_date'), fallback=_pkt_today())
    status_view = (request.args.get('status_view') or 'assigned').strip().lower()
    if status_view not in ('assigned', 'absent', 'not_assigned'):
        status_view = 'assigned'
    status_project_id = request.args.get('status_project_id', type=int)
    status_stage_id = request.args.get('status_stage_id', type=int)
    raw_from = (request.args.get('date_from') or '').strip()
    raw_to = (request.args.get('date_to') or '').strip()
    date_from = _parse_date(raw_from, fallback=None) if raw_from else None
    date_to = _parse_date(raw_to, fallback=None) if raw_to else None
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    scope_stage_ids = set()
    scope_project_ids = set()
    if sub.project_id:
        scope_project_ids.add(int(sub.project_id))
        sub_project_stage_ids = (db.session.query(Stage.id)
                                 .filter(Stage.project_id == sub.project_id)
                                 .all())
        scope_stage_ids.update(int(sidv) for (sidv,) in sub_project_stage_ids if sidv)
    if sub.stage_id:
        scope_stage_ids.add(int(sub.stage_id))
        stg = Stage.query.get(sub.stage_id)
        if stg and stg.project_id:
            scope_project_ids.add(int(stg.project_id))
    assigned_stages = (Stage.query
                       .filter(Stage.assigned_subcontractor_id == sub.id)
                       .all())
    for srow in assigned_stages:
        scope_stage_ids.add(int(srow.id))
        if srow.project_id:
            scope_project_ids.add(int(srow.project_id))
    hist_stage_ids = (db.session.query(SubcontractLabourAttendance.stage_id)
                      .filter(SubcontractLabourAttendance.subcontractor_id == sub.id)
                      .distinct()
                      .all())
    for (sidv,) in hist_stage_ids:
        if sidv:
            scope_stage_ids.add(int(sidv))
    if scope_stage_ids:
        hist_stages = Stage.query.filter(Stage.id.in_(list(scope_stage_ids))).all()
        for srow in hist_stages:
            if srow.project_id:
                scope_project_ids.add(int(srow.project_id))

    stage_options = []
    if scope_stage_ids:
        stage_options = (Stage.query
                         .filter(Stage.id.in_(list(scope_stage_ids)))
                         .order_by(Stage.project_id.asc(), Stage.id.asc())
                         .all())
    project_options = []
    if scope_project_ids:
        project_options = (Project.query
                           .filter(Project.id.in_(list(scope_project_ids)))
                           .order_by(Project.name.asc(), Project.id.asc())
                           .all())
    selected_project_id = None
    if sub.project_id and int(sub.project_id) in scope_project_ids:
        selected_project_id = int(sub.project_id)
    elif sub.stage_id:
        sub_stage = Stage.query.get(sub.stage_id)
        if sub_stage and sub_stage.project_id and int(sub_stage.project_id) in scope_project_ids:
            selected_project_id = int(sub_stage.project_id)
    if selected_project_id is None and project_options:
        selected_project_id = int(project_options[0].id)
    wq = SubcontractLabourWorker.query.filter(SubcontractLabourWorker.subcontractor_id == sub.id)
    if not show_inactive:
        wq = wq.filter(SubcontractLabourWorker.active_status == True)
    labour_workers = wq.order_by(SubcontractLabourWorker.name.asc(), SubcontractLabourWorker.id.asc()).all()
    all_workers = (SubcontractLabourWorker.query
                   .filter(SubcontractLabourWorker.subcontractor_id == sub.id)
                   .order_by(SubcontractLabourWorker.name.asc(), SubcontractLabourWorker.id.asc())
                   .all())
    lq = SubcontractLabourAttendance.query.filter(SubcontractLabourAttendance.subcontractor_id == sub.id)
    if filter_worker_id:
        lq = lq.filter(SubcontractLabourAttendance.worker_id == filter_worker_id)
    if date_from:
        lq = lq.filter(SubcontractLabourAttendance.date >= date_from)
    if date_to:
        lq = lq.filter(SubcontractLabourAttendance.date <= date_to)
    labour_rows = lq.order_by(SubcontractLabourAttendance.date.desc(), SubcontractLabourAttendance.id.desc()).all()
    earned_q = db.session.query(
        SubcontractLabourAttendance.worker_id,
        func.coalesce(func.sum(SubcontractLabourAttendance.total_labour_paid), 0.0)
    ).filter(
        SubcontractLabourAttendance.subcontractor_id == sub.id,
        SubcontractLabourAttendance.worker_id.isnot(None)
    )
    paid_q = db.session.query(
        SubcontractLabourPayment.worker_id,
        func.coalesce(func.sum(SubcontractLabourPayment.amount), 0.0)
    ).filter(
        SubcontractLabourPayment.subcontractor_id == sub.id
    )
    if date_from:
        earned_q = earned_q.filter(SubcontractLabourAttendance.date >= date_from)
        paid_q = paid_q.filter(SubcontractLabourPayment.date >= date_from)
    if date_to:
        earned_q = earned_q.filter(SubcontractLabourAttendance.date <= date_to)
        paid_q = paid_q.filter(SubcontractLabourPayment.date <= date_to)
    earned_map = {
        int(wid): float(total or 0.0)
        for wid, total in earned_q.group_by(SubcontractLabourAttendance.worker_id).all()
        if wid
    }
    paid_map = {
        int(wid): float(total or 0.0)
        for wid, total in paid_q.group_by(SubcontractLabourPayment.worker_id).all()
        if wid
    }
    worker_stats = []
    for w in all_workers:
        earned = float(earned_map.get(w.id, 0.0) or 0.0)
        paid = float(paid_map.get(w.id, 0.0) or 0.0)
        worker_stats.append({'worker': w, 'earned': earned, 'paid': paid, 'payable': max(0.0, earned - paid)})

    sheet_worker_ids = [int(w.id) for w in labour_workers]
    day_rows = (SubcontractLabourAttendance.query
                .filter(SubcontractLabourAttendance.subcontractor_id == sub.id,
                        SubcontractLabourAttendance.date == sheet_date,
                        SubcontractLabourAttendance.worker_id.in_(sheet_worker_ids))
                .order_by(SubcontractLabourAttendance.id.desc())
                .all()) if sheet_worker_ids else []
    day_map = {}
    for r in day_rows:
        if not r.worker_id:
            continue
        wid = int(r.worker_id)
        if wid not in day_map:
            day_map[wid] = r

    daily_sheet_rows = []
    for w in labour_workers:
        r = day_map.get(int(w.id))
        if r:
            status_txt = (r.attendance_status or 'Present').strip()
            normalized = status_txt.lower()
            if normalized not in ('present', 'absent', 'not assigned'):
                status_txt = 'Absent'
            total_hours = float(r.working_hours or 0.0) + float(r.overtime_hours or 0.0)
            project_id = int(r.project_id) if r.project_id else None
            stage_id = int(r.stage_id) if r.stage_id else None
            remarks_txt = (r.notes or '')
        else:
            status_txt = 'Not Assigned'
            total_hours = 0.0
            project_id = None
            stage_id = None
            remarks_txt = ''
        daily_sheet_rows.append({
            'worker': w,
            'status': status_txt,
            'project_id': project_id,
            'stage_id': stage_id,
            'working_hours': total_hours,
            'remarks': remarks_txt
        })

    assigned_count = 0
    absent_count = 0
    not_assigned_count = 0
    status_rows = []
    project_map = {int(p.id): p for p in project_options}
    stage_map = {int(s.id): s for s in stage_options}
    for row in daily_sheet_rows:
        worker = row['worker']
        status_txt = (row.get('status') or 'Not Assigned').strip()
        normalized = status_txt.lower()
        if normalized == 'present':
            bucket = 'assigned'
            assigned_count += 1
        elif normalized == 'absent':
            bucket = 'absent'
            absent_count += 1
        else:
            bucket = 'not_assigned'
            not_assigned_count += 1

        if status_project_id and int(row.get('project_id') or 0) != int(status_project_id):
            continue
        if status_stage_id and int(row.get('stage_id') or 0) != int(status_stage_id):
            continue
        if bucket != status_view:
            continue

        p = project_map.get(int(row['project_id'])) if row.get('project_id') else None
        s = stage_map.get(int(row['stage_id'])) if row.get('stage_id') else None
        total_hours = float(row.get('working_hours') or 0.0)
        status_rows.append({
            'worker_id': int(worker.id),
            'worker_name': worker.name,
            'worker_trade': worker.trade or '-',
            'status': 'Assigned' if bucket == 'assigned' else ('Absent' if bucket == 'absent' else 'Not Assigned'),
            'project_id': int(row['project_id']) if row.get('project_id') else None,
            'stage_id': int(row['stage_id']) if row.get('stage_id') else None,
            'project_name': p.name if p else '-',
            'stage_name': s.name if s else '-',
            'working_hours': min(8.0, total_hours),
            'overtime_hours': max(0.0, total_hours - 8.0),
            'remarks': row.get('remarks') or '-'
        })

    return render_template(
        'subcontractor_attendance.html',
        sub=sub,
        sheet_date=sheet_date.isoformat(),
        status_view=status_view,
        status_project_id=status_project_id,
        status_stage_id=status_stage_id,
        assigned_count=assigned_count,
        absent_count=absent_count,
        not_assigned_count=not_assigned_count,
        status_rows=status_rows,
        project_options=project_options,
        selected_project_id=selected_project_id,
        stage_options=stage_options,
        labour_workers=labour_workers,
        all_workers=all_workers,
        daily_sheet_rows=daily_sheet_rows,
        worker_stats=worker_stats,
        labour_rows=labour_rows,
        selected_worker_id=filter_worker_id,
        selected_from=raw_from,
        selected_to=raw_to,
        show_inactive=show_inactive
    )


@app.route('/hdc/subcontractor/<int:sid>/progress', methods=['POST'])
@login_required
def hdc_subcontractor_progress(sid):
    sub = Subcontractor.query.get_or_404(sid)
    pct = _flt(request.form.get('work_done_percentage'))
    old_pct = float(sub.work_done_percentage or 0.0)
    sub.work_done_percentage = max(0.0, min(100.0, float(pct or 0.0)))
    _log_subcontract_event(
        sub=sub,
        event_type='progress',
        from_value=f'{old_pct:.2f}%',
        to_value=f'{float(sub.work_done_percentage or 0.0):.2f}%',
        notes='Manual progress update'
    )
    db.session.commit()
    flash(f'Work done updated for {sub.name}.', 'success')
    if sub.project_id:
        return redirect(url_for('hdc_project_detail', pid=sub.project_id))
    return redirect(url_for('hdc_subcontractors'))


@app.route('/hdc/subcontractor/<int:sid>/ledger')
@login_required
def hdc_subcontractor_ledger(sid):
    _ensure_subcontract_labour_attendance_schema()
    sub = Subcontractor.query.get_or_404(sid)
    event_type = (request.args.get('event_type') or '').strip().lower()
    stage_filter_id = request.args.get('stage_id', type=int)
    date_from = None
    date_to = None
    raw_from = (request.args.get('date_from') or '').strip()
    raw_to = (request.args.get('date_to') or '').strip()
    try:
        if raw_from:
            date_from = datetime.strptime(raw_from, '%Y-%m-%d').date()
    except Exception:
        date_from = None
    try:
        if raw_to:
            date_to = datetime.strptime(raw_to, '%Y-%m-%d').date()
    except Exception:
        date_to = None
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    q = SubcontractEvent.query.filter(SubcontractEvent.subcontractor_id == sub.id)
    if event_type:
        q = q.filter(func.lower(SubcontractEvent.event_type) == event_type)
    if date_from:
        q = q.filter(SubcontractEvent.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        q = q.filter(SubcontractEvent.created_at <= datetime.combine(date_to, datetime.max.time()))
    rows = q.order_by(SubcontractEvent.created_at.asc(), SubcontractEvent.id.asc()).all()
    q_base = SubcontractEvent.query.filter(SubcontractEvent.subcontractor_id == sub.id)
    if date_from:
        q_base = q_base.filter(SubcontractEvent.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        q_base = q_base.filter(SubcontractEvent.created_at <= datetime.combine(date_to, datetime.max.time()))
    base_rows = q_base.order_by(SubcontractEvent.created_at.asc(), SubcontractEvent.id.asc()).all()
    type_map = {
        'create': 'Profile Created',
        'shift': 'Shift Assigned',
        'reassign': 'Stage Reassigned',
        'unassign': 'Shift Back To Company',
        'status': 'Stage Status',
        'payment': 'Payment',
        'tip': 'Tip',
        'settlement': 'Settlement',
        'attendance': 'Attendance',
        'labour_attendance': 'Labour Attendance',
        'labour_worker_add': 'Worker Added',
        'labour_worker_edit': 'Worker Edited',
        'labour_worker_status': 'Worker Status',
        'labour_worker_payment': 'Worker Payment',
        'progress': 'Progress',
        'price_update': 'Price Update'
    }
    events = []
    for r in rows:
        detail_parts = []
        if r.from_value:
            detail_parts.append(f'From: {r.from_value}')
        if r.to_value:
            detail_parts.append(f'To: {r.to_value}')
        if r.notes:
            detail_parts.append(r.notes)
        events.append({
            'dt': r.created_at,
            'type': type_map.get((r.event_type or '').strip().lower(), (r.event_type or 'Event').title()),
            'detail': ' | '.join(detail_parts) if detail_parts else '-',
            'amount': float(r.amount or 0.0)
        })
    att_q = SubcontractAttendance.query.filter(SubcontractAttendance.subcontractor_id == sub.id)
    pay_q = SubcontractPayment.query.filter(SubcontractPayment.subcontractor_id == sub.id, SubcontractPayment.is_void == False)
    lab_q = SubcontractLabourAttendance.query.filter(SubcontractLabourAttendance.subcontractor_id == sub.id)
    if stage_filter_id:
        lab_q = lab_q.filter(SubcontractLabourAttendance.stage_id == stage_filter_id)
    if date_from:
        att_q = att_q.filter(SubcontractAttendance.date >= date_from)
        pay_q = pay_q.filter(SubcontractPayment.date >= date_from)
        lab_q = lab_q.filter(SubcontractLabourAttendance.date >= date_from)
    if date_to:
        att_q = att_q.filter(SubcontractAttendance.date <= date_to)
        pay_q = pay_q.filter(SubcontractPayment.date <= date_to)
        lab_q = lab_q.filter(SubcontractLabourAttendance.date <= date_to)
    att_rows = att_q.all()
    pay_rows = pay_q.all()
    lab_rows = lab_q.order_by(SubcontractLabourAttendance.date.desc(), SubcontractLabourAttendance.id.desc()).all()

    def _pct(v):
        try:
            return float(str(v or '').replace('%', '').strip() or 0.0)
        except Exception:
            return 0.0
    progress_events = [r for r in base_rows if (r.event_type or '').lower() == 'progress']
    progress_delta_signed = sum(_pct(r.to_value) - _pct(r.from_value) for r in progress_events)
    progress_delta_abs = sum(abs(_pct(r.to_value) - _pct(r.from_value)) for r in progress_events)

    payment_rows = [p for p in pay_rows if (p.entry_type or 'payment').strip().lower() != 'settlement']
    settlement_rows = [p for p in pay_rows if (p.entry_type or 'payment').strip().lower() == 'settlement']
    kpis = {
        'events_count': len(rows),
        'attendance_days': sum(1 for r in att_rows if (r.present_count or 0) > 0),
        'attendance_men': sum(int(r.present_count or 0) for r in att_rows),
        'progress_added': progress_delta_signed,
        'progress_changes_abs': progress_delta_abs,
        'attendance_progress': sum(float(r.work_done_pct or 0.0) for r in att_rows),
        'payments_total': sum(float(p.amount or 0.0) for p in payment_rows),
        'settled_total': sum(float(p.amount or 0.0) for p in settlement_rows),
        'shifts_count': sum(1 for r in base_rows if (r.event_type or '').lower() in ('shift', 'reassign')),
        'unassign_count': sum(1 for r in base_rows if (r.event_type or '').lower() == 'unassign'),
        'price_updates': sum(1 for r in base_rows if (r.event_type or '').lower() == 'price_update'),
        'labour_days': sum(1 for r in lab_rows if (r.labour_count or 0) > 0),
        'labour_men_total': sum(int(r.labour_count or 0) for r in lab_rows),
        'labour_cost_total': sum(float(r.total_labour_paid or 0.0) for r in lab_rows)
    }
    kpis['labour_avg_daily_cost'] = (kpis['labour_cost_total'] / kpis['labour_days']) if kpis['labour_days'] > 0 else 0.0
    kpis['labour_avg_men_per_day'] = (kpis['labour_men_total'] / kpis['labour_days']) if kpis['labour_days'] > 0 else 0.0
    payable_now = float(sub.payable_amount or 0.0)
    cleared_now = float(sub.total_cleared or 0.0)
    kpis['payment_coverage_pct'] = (cleared_now / payable_now * 100.0) if payable_now > 0 else 0.0
    kpis['sub_margin_payable_basis'] = payable_now - float(kpis['labour_cost_total'] or 0.0)
    kpis['sub_margin_paid_basis'] = cleared_now - float(kpis['labour_cost_total'] or 0.0)
    kpis['sub_margin_contract_basis'] = float(sub.contract_value or 0.0) - float(kpis['labour_cost_total'] or 0.0)
    kpis['sub_profit_loss_total'] = float(kpis['sub_margin_contract_basis'] or 0.0)
    owner_rate = float(sub.stage_rel.effective_rate or 0.0) if sub.stage_rel and (sub.stage_rel.contract_basis or '') == 'Per Sq Ft' else 0.0
    sub_rate = float(sub.rate_per_sqft or 0.0) if (sub.contract_type or '') == 'sqft' else 0.0
    kpis['rate_spread_sqft'] = (owner_rate - sub_rate) if (owner_rate > 0 and sub_rate > 0) else None
    owner_qty = float(sub.stage_rel.qty_sqft or 0.0) if sub.stage_rel else 0.0
    sub_qty = float(sub.total_sqft or 0.0) if (sub.contract_type or '') == 'sqft' else 0.0
    kpis['owner_qty'] = owner_qty if owner_qty > 0 else None
    kpis['sub_qty'] = sub_qty if sub_qty > 0 else None
    kpis['qty_gap'] = (owner_qty - sub_qty) if (owner_qty > 0 and sub_qty > 0) else None
    kpis['stage_completed_but_sub_lt100'] = bool(
        sub.stage_rel and (sub.stage_rel.status or '').strip().lower() in ('completed', 'complete')
        and float(sub.effective_progress_percentage or 0.0) < 100.0
    )
    stage_options = []
    if sub.project_id:
        stage_options = Stage.query.filter(Stage.project_id == sub.project_id).order_by(Stage.id.asc()).all()
    elif sub.stage_id:
        stg = Stage.query.get(sub.stage_id)
        if stg:
            stage_options = [stg]

    return render_template(
        'subcontractor_ledger.html',
        sub=sub,
        events=events,
        labour_rows=lab_rows,
        stage_options=stage_options,
        kpis=kpis,
        selected_event_type=event_type,
        selected_stage_id=stage_filter_id,
        selected_from=raw_from,
        selected_to=raw_to
    )


@app.route('/hdc/subcontractor/<int:sid>/events/rebuild', methods=['POST'])
@login_required
def hdc_subcontractor_events_rebuild(sid):
    sub = Subcontractor.query.get_or_404(sid)
    if (getattr(current_user, 'role', '') or '').lower() != 'admin':
        flash('Only admin can rebuild subcontractor history.', 'danger')
        return redirect(url_for('hdc_subcontractor_ledger', sid=sid))
    created = _ensure_subcontract_baseline_events(sub)
    db.session.commit()
    flash(f'Subcontractor history rebuilt. Added {created} missing baseline event(s).', 'success')
    return redirect(url_for('hdc_subcontractor_ledger', sid=sid))


@app.route('/hdc/stage/<int:sid>/shift/subcontractor', methods=['POST'])
@login_required
def hdc_stage_shift_to_subcontractor(sid):
    stg = Stage.query.get_or_404(sid)
    sub_id = request.form.get('subcontractor_id', type=int)
    if not sub_id:
        flash('Please select a subcontractor.', 'warning')
        return redirect(url_for('hdc_project_detail', pid=stg.project_id))
    sub = Subcontractor.query.get(sub_id)
    if not sub:
        flash('Selected subcontractor is invalid.', 'danger')
        return redirect(url_for('hdc_project_detail', pid=stg.project_id))
    from_desc = 'unassigned'
    if stg.assigned_subcontractor:
        prev = stg.assigned_subcontractor
        from_desc = f'{prev.subcontractor_code or ("SUB-" + str(prev.id))} {prev.name}'
    if sub.stage_id and sub.stage_id != stg.id:
        prev_stage = Stage.query.get(sub.stage_id)
        if prev_stage and prev_stage.assigned_subcontractor_id == sub.id:
            prev_stage.execution_mode = 'company'
            prev_stage.assigned_subcontractor_id = None
            _log_subcontract_event(
                sub=sub,
                event_type='unassign',
                from_value=prev_stage.name,
                to_value='company',
                notes='Auto-unassigned from previous stage due to reassignment',
                project_id=prev_stage.project_id,
                stage_id=prev_stage.id
            )

    # Optional subcontract pricing overrides while shifting.
    old_terms = f'{sub.contract_type}|R{float(sub.rate_per_sqft or 0):.2f}|Q{float(sub.total_sqft or 0):.2f}|L{float(sub.lump_sum_amount or 0):.2f}|Ret{float(sub.retention_percentage or 0):.2f}%'
    ctype = (request.form.get('contract_type') or sub.contract_type or 'lump_sum').strip().lower()
    if ctype not in ('lump_sum', 'sqft'):
        ctype = 'lump_sum'
    sub.contract_type = ctype
    raw_rate = (request.form.get('rate_per_sqft') or '').strip()
    raw_qty = (request.form.get('total_sqft') or '').strip()
    raw_lump = (request.form.get('lump_sum_amount') or '').strip()
    raw_ret = (request.form.get('retention_pct') or '').strip()
    if raw_rate:
        sub.rate_per_sqft = _flt(raw_rate)
    if raw_qty:
        sub.total_sqft = _flt(raw_qty)
    if raw_lump:
        sub.lump_sum_amount = _flt(raw_lump)
    if raw_ret:
        sub.retention_percentage = _flt(raw_ret)
    if sub.contract_type == 'sqft' and (sub.total_sqft or 0) <= 0 and (stg.qty_sqft or 0) > 0:
        sub.total_sqft = float(stg.qty_sqft or 0.0)

    prev_assigned = stg.assigned_subcontractor
    prev_assigned_id = stg.assigned_subcontractor_id
    if prev_assigned and prev_assigned.id != sub.id and prev_assigned.stage_id == stg.id:
        prev_assigned.stage_id = None
        _log_subcontract_event(
            sub=prev_assigned,
            event_type='unassign',
            from_value=stg.name,
            to_value='reassigned',
            notes=f'Stage reassigned to {sub.subcontractor_code or ("SUB-" + str(sub.id))} {sub.name}',
            project_id=stg.project_id,
            stage_id=stg.id
        )

    stg.execution_mode = 'subcontractor'
    stg.assigned_subcontractor_id = sub.id
    sub.project_id = stg.project_id
    sub.stage_id = stg.id
    ev_type = 'shift'
    if prev_assigned_id and prev_assigned_id != sub.id:
        ev_type = 'reassign'
    _log_subcontract_event(
        sub=sub,
        event_type=ev_type,
        from_value=from_desc,
        to_value=f'{stg.name}',
        notes=f'Shifted stage to subcontractor | type={sub.contract_type} rate={float(sub.rate_per_sqft or 0):.2f} sqft={float(sub.total_sqft or 0):.2f} lump={float(sub.lump_sum_amount or 0):.2f}',
        project_id=stg.project_id,
        stage_id=stg.id
    )
    _log_subcontract_event(
        sub=sub,
        event_type='price_update',
        from_value=old_terms,
        to_value=f'{sub.contract_type}',
        amount=float(sub.contract_value or 0.0),
        notes=f'Rate {float(sub.rate_per_sqft or 0):.2f} | Sqft {float(sub.total_sqft or 0):.2f} | Lump {float(sub.lump_sum_amount or 0):.2f} | Ret {float(sub.retention_percentage or 0):.2f}%',
        project_id=stg.project_id,
        stage_id=stg.id
    )
    db.session.commit()
    owner_rate = float(stg.effective_rate or 0.0) if (stg.contract_basis or '') == 'Per Sq Ft' else 0.0
    sub_rate = float(sub.rate_per_sqft or 0.0) if (sub.contract_type or '') == 'sqft' else 0.0
    margin_note = ''
    if owner_rate > 0 and sub_rate > 0:
        margin_note = f' Owner/Sub rate spread: {owner_rate - sub_rate:,.2f} per sqft.'
        if sub_rate > owner_rate:
            flash(
                f'Warning: subcontract rate ({sub_rate:,.2f}) is higher than owner rate ({owner_rate:,.2f}). '
                f'This stage may run at a loss.',
                'warning'
            )
    flash(f'Stage "{stg.name}" shifted to subcontractor {sub.name}.{margin_note}', 'success')
    return redirect(url_for('hdc_project_detail', pid=stg.project_id))


@app.route('/hdc/stage/<int:sid>/shift/company', methods=['POST'])
@login_required
def hdc_stage_shift_to_company(sid):
    stg = Stage.query.get_or_404(sid)
    prev_sub = stg.assigned_subcontractor
    stg.execution_mode = 'company'
    stg.assigned_subcontractor_id = None
    if prev_sub and prev_sub.stage_id == stg.id:
        prev_sub.stage_id = None
        _log_subcontract_event(
            sub=prev_sub,
            event_type='unassign',
            from_value=stg.name,
            to_value='company',
            notes='Shifted back to company execution',
            project_id=stg.project_id,
            stage_id=stg.id
        )
    db.session.commit()
    flash(f'Stage "{stg.name}" shifted to company execution.', 'success')
    return redirect(url_for('hdc_project_detail', pid=stg.project_id))


# â”€â”€ Stages â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/projects/<int:pid>/stage/add', methods=['GET', 'POST'])
@login_required
def hdc_add_stage(pid):
    p    = Project.query.get_or_404(pid)
    defs = (StageDefinition.query
            .filter_by(project_id=pid, active_status=True)
            .order_by(StageDefinition.default_order, StageDefinition.id)
            .all())
    if request.method == 'POST':
        def_id = request.form.get('definition_id', type=int) or None
        name   = request.form.get('name','').strip()
        if def_id and not name:
            sd   = StageDefinition.query.filter_by(id=def_id, project_id=pid).first()
            name = sd.name if sd else name
        basis  = request.form.get('contract_basis','')
        rate   = _flt(request.form.get('rate_per_sqft'))
        disc   = _flt(request.form.get('discount_per_sqft'))
        qty    = _flt(request.form.get('qty_sqft'))
        lump   = _flt(request.form.get('lump_sum_value'))
        est_cost = _flt(request.form.get('estimated_cost'))
        progress = _flt(request.form.get('progress'))
        start_d  = _parse_date(request.form.get('start_date')) if request.form.get('start_date') else None
        end_d    = _parse_date(request.form.get('end_date')) if request.form.get('end_date') else None
        if not basis and (rate > 0 or qty > 0):
            basis = 'Per Sq Ft'
        s = Stage(
            project_id=pid, definition_id=def_id, name=name,
            status=request.form.get('status','Active'),
            estimated_cost=est_cost, progress=progress,
            start_date=start_d, end_date=end_d,
            contract_basis=basis, rate_per_sqft=rate,
            discount_per_sqft=disc, qty_sqft=qty, lump_sum_value=lump,
            original_contract_basis=basis, original_rate_per_sqft=rate,
            original_discount_per_sqft=disc, original_qty_sqft=qty,
            original_lump_sum_value=lump)
        db.session.add(s); db.session.commit()
        flash(f'Stage "{s.name}" added.', 'success')
        return redirect(url_for('hdc_project_detail', pid=pid))
    return render_template('stage_form.html', p=p, stage=None, defs=defs, mode='add')

@app.route('/hdc/stage/<int:sid>/edit', methods=['GET', 'POST'])
@login_required
def hdc_edit_stage(sid):
    s = Stage.query.get_or_404(sid)
    p = s.project
    if request.method == 'POST':
        new_basis = request.form.get('contract_basis','')
        new_rate  = _flt(request.form.get('rate_per_sqft'))
        new_disc  = _flt(request.form.get('discount_per_sqft'))
        new_qty   = _flt(request.form.get('qty_sqft'))
        new_lump  = _flt(request.form.get('lump_sum_value'))
        changed   = (new_basis != s.contract_basis or new_rate != s.rate_per_sqft or
                     new_disc != s.discount_per_sqft or new_qty != s.qty_sqft or new_lump != s.lump_sum_value)
        if changed:
            db.session.add(StageRateHistory(
                stage_id=s.id,
                old_contract_basis=s.contract_basis, new_contract_basis=new_basis,
                old_rate_per_sqft=s.rate_per_sqft, new_rate_per_sqft=new_rate,
                old_discount_per_sqft=s.discount_per_sqft, new_discount_per_sqft=new_disc,
                old_qty_sqft=s.qty_sqft, new_qty_sqft=new_qty,
                old_lump_sum_value=s.lump_sum_value, new_lump_sum_value=new_lump,
                reason=request.form.get('reason','')))
        s.name           = request.form.get('name','').strip() or s.name
        s.status         = request.form.get('status', s.status)
        s.estimated_cost = _flt(request.form.get('estimated_cost'))
        s.progress       = _flt(request.form.get('progress'))
        s.start_date     = _parse_date(request.form.get('start_date')) if request.form.get('start_date') else None
        s.end_date       = _parse_date(request.form.get('end_date')) if request.form.get('end_date') else None
        s.contract_basis = new_basis; s.rate_per_sqft = new_rate
        s.discount_per_sqft = new_disc; s.qty_sqft = new_qty; s.lump_sum_value = new_lump
        db.session.commit()
        flash('Stage updated.', 'success')
        return redirect(url_for('hdc_project_detail', pid=p.id))
    return render_template('stage_form.html', p=p, stage=s, defs=[], mode='edit')

@app.route('/hdc/stage/<int:sid>/delete', methods=['POST'])
@login_required
def hdc_delete_stage(sid):
    s = Stage.query.get_or_404(sid)
    pid = s.project_id
    db.session.delete(s); db.session.commit()
    flash('Stage deleted.', 'success')
    return redirect(url_for('hdc_project_detail', pid=pid))

@app.route('/hdc/stage/<int:sid>/status', methods=['POST'])
@login_required
def hdc_stage_status(sid):
    s = Stage.query.get_or_404(sid)
    old_status = (s.status or '').strip().lower()
    s.status = request.form.get('status', s.status)
    new_status = (s.status or '').strip().lower()
    auto_note = ''
    auto_sub = (request.form.get('auto_sub_complete') or '').strip() == '1'
    if new_status in ('completed', 'complete') and s.assigned_subcontractor_id and not auto_sub:
        sub_chk = s.assigned_subcontractor
        if sub_chk and float(sub_chk.effective_progress_percentage or 0.0) < 100.0:
            s.status = old_status or s.status
            flash(f'Cannot complete stage "{s.name}" because subcontractor "{sub_chk.name}" is at {float(sub_chk.effective_progress_percentage or 0.0):.2f}%. Update Sub Completion % to 100 or use force complete action.', 'danger')
            return redirect(url_for('hdc_project_detail', pid=s.project_id))
    if auto_sub and new_status in ('completed', 'complete') and s.assigned_subcontractor_id:
        sub = s.assigned_subcontractor
        if sub:
            old_pct = float(sub.work_done_percentage or 0.0)
            if old_pct < 100.0:
                sub.work_done_percentage = 100.0
                _log_subcontract_event(
                    sub=sub,
                    event_type='progress',
                    from_value=f'{old_pct:.2f}%',
                    to_value='100.00%',
                    notes=f'Auto-updated on stage completion ({s.name})',
                    project_id=s.project_id,
                    stage_id=s.id
                )
                auto_note = f' Subcontractor progress auto-set to 100% ({sub.name}).'
    if old_status != new_status and s.assigned_subcontractor:
        _log_subcontract_event(
            sub=s.assigned_subcontractor,
            event_type='status',
            from_value=old_status or '-',
            to_value=new_status or '-',
            notes=f'Stage status changed: {s.name}',
            project_id=s.project_id,
            stage_id=s.id
        )
    db.session.commit()
    flash(f'Stage marked {s.status}.{auto_note}', 'success')
    return redirect(url_for('hdc_project_detail', pid=s.project_id))


@app.route('/hdc/stage/<int:sid>/sub-progress', methods=['POST'])
@login_required
def hdc_stage_sub_progress(sid):
    s = Stage.query.get_or_404(sid)
    sub = s.assigned_subcontractor
    if not sub:
        flash('No subcontractor assigned to this stage.', 'warning')
        return redirect(url_for('hdc_project_detail', pid=s.project_id))
    pct = _flt(request.form.get('work_done_percentage'))
    pct = max(0.0, min(100.0, float(pct or 0.0)))
    old_pct = float(sub.work_done_percentage or 0.0)
    sub.work_done_percentage = pct
    _log_subcontract_event(
        sub=sub,
        event_type='progress',
        from_value=f'{old_pct:.2f}%',
        to_value=f'{pct:.2f}%',
        notes=f'Stage-level completion update ({s.name})',
        project_id=s.project_id,
        stage_id=s.id
    )
    db.session.commit()
    flash(f'Subcontractor completion updated to {pct:.2f}% for stage "{s.name}".', 'success')
    return redirect(url_for('hdc_project_detail', pid=s.project_id))

@app.route('/hdc/stage/<int:sid>/drawings/upload', methods=['POST'])
@login_required
def hdc_stage_drawings_upload(sid):
    s = Stage.query.get_or_404(sid)
    files = request.files.getlist('drawings')
    uploaded = 0
    for f in files:
        if not f or not (f.filename or '').strip():
            continue
        if not _is_pdf_upload(f):
            continue
        original = secure_filename(f.filename or 'drawing.pdf') or 'drawing.pdf'
        stored = f"{uuid4().hex}.pdf"
        path = os.path.join(STAGE_DRAWINGS_DIR, stored)
        f.save(path)
        db.session.add(StageDrawing(
            stage_id=s.id,
            original_name=original,
            stored_name=stored
        ))
        uploaded += 1
    db.session.commit()
    if uploaded:
        flash(f'{uploaded} PDF drawing(s) uploaded.', 'success')
    else:
        flash('No valid PDF selected.', 'warning')
    return redirect(url_for('hdc_project_detail', pid=s.project_id))

@app.route('/hdc/stage/drawing/<int:did>/view')
@login_required
def hdc_stage_drawing_view(did):
    d = StageDrawing.query.get_or_404(did)
    path = os.path.join(STAGE_DRAWINGS_DIR, d.stored_name or '')
    if not os.path.exists(path):
        flash('Drawing file not found on disk.', 'danger')
        return redirect(url_for('hdc_project_detail', pid=d.stage.project_id))
    return send_file(path, mimetype='application/pdf', as_attachment=False, download_name=d.original_name)

@app.route('/hdc/stage/drawing/<int:did>/delete', methods=['POST'])
@login_required
def hdc_stage_drawing_delete(did):
    d = StageDrawing.query.get_or_404(did)
    pid = d.stage.project_id
    path = os.path.join(STAGE_DRAWINGS_DIR, d.stored_name or '')
    db.session.delete(d)
    db.session.commit()
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as ex:
        app.logger.warning('Could not remove stage drawing file: %s', ex)
    flash('Drawing deleted.', 'success')
    return redirect(url_for('hdc_project_detail', pid=pid))

@app.route('/hdc/stage/drawing/<int:did>/replace', methods=['POST'])
@login_required
def hdc_stage_drawing_replace(did):
    d = StageDrawing.query.get_or_404(did)
    pid = d.stage.project_id
    f = request.files.get('drawing_file')
    if not _is_pdf_upload(f):
        flash('Please upload a valid PDF file.', 'warning')
        return redirect(url_for('hdc_project_detail', pid=pid))
    old_path = os.path.join(STAGE_DRAWINGS_DIR, d.stored_name or '')
    new_original = secure_filename(f.filename or 'drawing.pdf') or 'drawing.pdf'
    new_stored = f"{uuid4().hex}.pdf"
    new_path = os.path.join(STAGE_DRAWINGS_DIR, new_stored)
    f.save(new_path)
    d.original_name = new_original
    d.stored_name = new_stored
    d.updated_at = _pkt_now_naive()
    db.session.commit()
    try:
        if os.path.exists(old_path):
            os.remove(old_path)
    except Exception as ex:
        app.logger.warning('Could not remove replaced stage drawing file: %s', ex)
    flash('Drawing replaced successfully.', 'success')
    return redirect(url_for('hdc_project_detail', pid=pid))

@app.route('/hdc/projects/<int:pid>/bulk_stages', methods=['POST'])
@login_required
def hdc_bulk_add_stages(pid):
    Project.query.get_or_404(pid)
    ids = request.form.getlist('definition_ids', type=int)
    added = 0
    for did in ids:
        sd = StageDefinition.query.filter_by(id=did, project_id=pid, active_status=True).first()
        if sd:
            db.session.add(Stage(project_id=pid, definition_id=did, name=sd.name))
            added += 1
    db.session.commit()
    flash(f'{added} stage(s) added.', 'success')
    return redirect(url_for('hdc_project_detail', pid=pid))

# Stage Library
@app.route('/hdc/stage-library', methods=['GET', 'POST'])
@login_required
def hdc_stage_library():
    pid = request.args.get('project_id', type=int)
    if request.method == 'POST':
        pid = request.form.get('project_id', type=int) or pid
    if not pid:
        flash('Select a project to manage its stage library.', 'warning')
        return redirect(url_for('hdc_projects'))
    project = Project.query.get_or_404(pid)

    if request.method == 'POST':
        action = request.form.get('action','add')
        if action == 'add':
            name = request.form.get('name','').strip()
            exists = StageDefinition.query.filter(
                StageDefinition.project_id == pid,
                func.lower(StageDefinition.name) == name.lower()
            ).first() if name else None
            if name and not exists:
                db.session.add(StageDefinition(
                    project_id=pid,
                    name=name,
                    default_order=_flt(request.form.get('order',0)),
                    active_status=True
                ))
                db.session.commit()
                flash(f'"{name}" added to library.', 'success')
            else:
                flash('Name empty or already exists.', 'warning')
        elif action == 'edit':
            sd = StageDefinition.query.filter_by(
                id=request.form.get('def_id', type=int),
                project_id=pid
            ).first()
            new_name = (request.form.get('name') or '').strip()
            if not sd:
                flash('Stage definition not found.', 'danger')
            elif not new_name:
                flash('Stage name is required.', 'warning')
            else:
                dupe = StageDefinition.query.filter(
                    StageDefinition.project_id == pid,
                    func.lower(StageDefinition.name) == new_name.lower(),
                    StageDefinition.id != sd.id
                ).first()
                if dupe:
                    flash('Another stage with this name already exists.', 'warning')
                else:
                    sd.name = new_name
                    sd.default_order = _flt(request.form.get('order', sd.default_order))
                    db.session.commit()
                    flash('Stage definition updated.', 'success')
        elif action == 'delete':
            sd = StageDefinition.query.filter_by(
                id=request.form.get('def_id', type=int),
                project_id=pid
            ).first()
            if sd:
                used = Stage.query.filter_by(definition_id=sd.id).count()
                if used > 0:
                    sd.active_status = False
                    db.session.commit()
                    flash('Stage has existing data, so it was suspended (not deleted).', 'warning')
                else:
                    db.session.delete(sd); db.session.commit()
                    flash('Stage definition removed.', 'success')
        elif action == 'toggle':
            sd = StageDefinition.query.filter_by(
                id=request.form.get('def_id', type=int),
                project_id=pid
            ).first()
            if sd:
                sd.active_status = not bool(sd.active_status)
                db.session.commit()
                flash(f'Stage definition {"reactivated" if sd.active_status else "suspended"}.', 'success')
        return redirect(url_for('hdc_stage_library', project_id=pid))
    defs = (StageDefinition.query
            .filter_by(project_id=pid)
            .order_by(StageDefinition.default_order, StageDefinition.id)
            .all())
    return render_template('stage_library.html', defs=defs, project=project)

@app.route('/hdc/stages')
@login_required
def hdc_stage_overview():
    projects = Project.query.order_by(Project.name).all()
    project_id = request.args.get('project_id', type=int)
    q = Stage.query
    if project_id:
        q = q.filter(Stage.project_id == project_id)
    stages = q.order_by(Stage.id.desc()).all()
    _apply_aggregated_stage_costs(stages)
    return render_template(
        'stage_overview.html',
        stages=stages,
        projects=projects,
        selected_project=project_id
    )


@app.route('/hdc/subcontractors', methods=['GET', 'POST'])
@login_required
def hdc_subcontractors():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('Subcontractor name is required.', 'warning')
            return redirect(url_for('hdc_subcontractors'))
        code = _next_subcontractor_code()
        while Subcontractor.query.filter(func.lower(Subcontractor.subcontractor_code) == code.lower()).first():
            code = _next_subcontractor_code()

        sub = Subcontractor(
            subcontractor_code=code,
            project_id=None,
            stage_id=None,
            name=name,
            phone=(request.form.get('phone') or '').strip(),
            work_type='',
            contract_type='lump_sum',
            rate_per_sqft=0.0,
            total_sqft=0.0,
            lump_sum_amount=0.0,
            retention_percentage=0.0,
            work_done_percentage=0.0
        )
        db.session.add(sub)
        db.session.flush()
        _log_subcontract_event(
            sub=sub,
            event_type='create',
            to_value=f'{sub.subcontractor_code or ""} {sub.name}',
            notes='Subcontractor profile created'
        )
        db.session.commit()
        flash(f'Subcontractor added ({code}).', 'success')
        return redirect(url_for('hdc_subcontractors'))

    sub_id = request.args.get('sub_id', type=int)
    project_id = request.args.get('project_id', type=int)
    stage_id = request.args.get('stage_id', type=int)

    q = Subcontractor.query
    if sub_id:
        q = q.filter(Subcontractor.id == sub_id)
    if project_id:
        q = q.filter(Subcontractor.project_id == project_id)
    if stage_id:
        q = q.filter(Subcontractor.stage_id == stage_id)

    subcontractors = q.order_by(Subcontractor.created_at.desc(), Subcontractor.id.desc()).all()
    all_subcontractors = Subcontractor.query.order_by(Subcontractor.name.asc(), Subcontractor.id.asc()).all()
    projects = Project.query.order_by(Project.name.asc(), Project.id.asc()).all()

    stage_q = Stage.query
    if project_id:
        stage_q = stage_q.filter(Stage.project_id == project_id)
    stages = stage_q.order_by(Stage.name.asc(), Stage.id.asc()).all()

    return render_template(
        'subcontractors.html',
        subcontractors=subcontractors,
        all_subcontractors=all_subcontractors,
        projects=projects,
        stages=stages,
        selected_sub_id=sub_id,
        selected_project_id=project_id,
        selected_stage_id=stage_id
    )


# â”€â”€ Workers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/workers', methods=['GET', 'POST'])
@login_required
def hdc_workers():
    if request.method == 'POST':
        code = request.form.get('worker_code','').strip()
        name = (request.form.get('name') or '').strip()
        role_type = (request.form.get('role_type') or '').strip()
        if not code or not name:
            flash('Worker code and name are required.', 'warning')
        elif Worker.query.filter_by(worker_code=code).first():
            flash('Worker code already exists.', 'danger')
        elif not role_type:
            flash('Please select trade from Trades list.', 'warning')
        else:
            valid_trade = db.session.query(WorkerTrade).filter(
                func.lower(WorkerTrade.name) == role_type.lower(),
                WorkerTrade.active_status == True
            ).first()
            if not valid_trade:
                flash('Selected trade is not available. Please add it in Trades section first.', 'warning')
                return redirect(url_for('hdc_workers'))
            w = Worker(worker_code=code,
                name=name,
                role_type=valid_trade.name,
                base_daily_wage=_flt(request.form.get('daily_wage')),
                wage_type=request.form.get('wage_type','daily'),
                hourly_rate=_flt(request.form.get('hourly_rate')),
                rate_per_sqft=_flt(request.form.get('rate_per_sqft')),
                active_status=True)
            db.session.add(w); db.session.commit()
            flash(f'Worker "{w.name}" added.', 'success')
        return redirect(url_for('hdc_workers'))
    workers = Worker.query.order_by(Worker.created_at.desc()).all()
    return render_template('workers.html', workers=workers, trade_options=_trade_options())

@app.route('/hdc/trades', methods=['GET', 'POST'])
@login_required
def hdc_trades():
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        if action == 'add_trade':
            trade_name = _normalize_trade_name(request.form.get('trade_name'))
            if not trade_name:
                flash('Trade name is required.', 'warning')
            else:
                existing = db.session.query(WorkerTrade).filter(func.lower(WorkerTrade.name) == trade_name.lower()).first()
                if existing:
                    if not existing.active_status:
                        existing.active_status = True
                        db.session.commit()
                        flash(f'Trade "{trade_name}" restored.', 'success')
                    else:
                        flash('Trade already exists.', 'warning')
                else:
                    db.session.add(WorkerTrade(name=trade_name, active_status=True))
                    db.session.commit()
                    flash(f'Trade "{trade_name}" added.', 'success')
        elif action == 'edit_trade':
            trade_id = request.form.get('trade_id', type=int)
            trade = WorkerTrade.query.get(trade_id)
            new_name = _normalize_trade_name(request.form.get('trade_name'))
            if not trade:
                flash('Trade not found.', 'danger')
            elif not new_name:
                flash('Trade name is required.', 'warning')
            else:
                dupe = db.session.query(WorkerTrade).filter(func.lower(WorkerTrade.name) == new_name.lower(), WorkerTrade.id != trade.id).first()
                if dupe:
                    flash('Another trade with this name already exists.', 'warning')
                else:
                    old_name = trade.name or ''
                    trade.name = new_name
                    Worker.query.filter(func.lower(Worker.role_type) == (old_name or '').lower()).update(
                        {Worker.role_type: new_name}, synchronize_session=False
                    )
                    db.session.commit()
                    flash(f'Trade updated to "{new_name}".', 'success')
        elif action == 'delete_trade':
            trade_id = request.form.get('trade_id', type=int)
            trade = WorkerTrade.query.get(trade_id)
            if not trade:
                flash('Trade not found.', 'danger')
            else:
                trade.active_status = False
                db.session.commit()
                flash(f'Trade "{trade.name}" removed from active list.', 'success')
        return redirect(url_for('hdc_trades'))
    trades = WorkerTrade.query.filter_by(active_status=True).order_by(WorkerTrade.name).all()
    return render_template('trades.html', trades=trades)

@app.route('/hdc/expense_categories', methods=['GET', 'POST'])
@login_required
def hdc_expense_categories():
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        if action == 'add_category':
            category_name = _normalize_expense_category_name(request.form.get('category_name'))
            if not category_name:
                flash('Category name is required.', 'warning')
            else:
                existing = db.session.query(ExpenseCategory).filter(
                    func.lower(ExpenseCategory.name) == category_name.lower()
                ).first()
                if existing:
                    if not existing.active_status:
                        existing.active_status = True
                        db.session.commit()
                        flash(f'Category "{category_name}" restored.', 'success')
                    else:
                        flash('Category already exists.', 'warning')
                else:
                    db.session.add(ExpenseCategory(name=category_name, active_status=True))
                    db.session.commit()
                    flash(f'Category "{category_name}" added.', 'success')
        elif action == 'edit_category':
            category_id = request.form.get('category_id', type=int)
            category = ExpenseCategory.query.get(category_id)
            new_name = _normalize_expense_category_name(request.form.get('category_name'))
            if not category:
                flash('Category not found.', 'danger')
            elif not new_name:
                flash('Category name is required.', 'warning')
            else:
                dupe = db.session.query(ExpenseCategory).filter(
                    func.lower(ExpenseCategory.name) == new_name.lower(),
                    ExpenseCategory.id != category.id
                ).first()
                if dupe:
                    flash('Another category with this name already exists.', 'warning')
                else:
                    category.name = new_name
                    db.session.commit()
                    flash(f'Category updated to "{new_name}".', 'success')
        elif action == 'delete_category':
            category_id = request.form.get('category_id', type=int)
            category = ExpenseCategory.query.get(category_id)
            if not category:
                flash('Category not found.', 'danger')
            else:
                category.active_status = False
                db.session.commit()
                flash(f'Category "{category.name}" removed from active list.', 'success')
        return redirect(url_for('hdc_expense_categories'))

    categories = ExpenseCategory.query.filter_by(active_status=True).order_by(ExpenseCategory.name).all()
    return render_template('expense_categories.html', categories=categories)

@app.route('/hdc/workers/<int:wid>/toggle', methods=['POST'])
@login_required
def hdc_toggle_worker(wid):
    w = Worker.query.get_or_404(wid)
    w.active_status = not w.active_status
    db.session.commit()
    flash(f'Worker "{w.name}" {"activated" if w.active_status else "suspended"}.', 'success')
    return redirect(url_for('hdc_workers'))

@app.route('/hdc/workers/<int:wid>/edit', methods=['POST'])
@login_required
def hdc_edit_worker(wid):
    w = Worker.query.get_or_404(wid)
    code = (request.form.get('worker_code') or '').strip()
    name = (request.form.get('name') or '').strip()
    role_type = (request.form.get('role_type') or '').strip()
    wage_type = (request.form.get('wage_type') or 'daily').strip()
    active_status = (request.form.get('active_status') or 'active').strip()

    if not code or not name:
        flash('Worker code and name are required.', 'warning')
        return redirect(url_for('hdc_workers'))

    code_exists = Worker.query.filter(Worker.worker_code == code, Worker.id != wid).first()
    if code_exists:
        flash('Worker code already exists for another worker.', 'danger')
        return redirect(url_for('hdc_workers'))

    valid_trade = db.session.query(WorkerTrade).filter(
        func.lower(WorkerTrade.name) == role_type.lower(),
        WorkerTrade.active_status == True
    ).first()
    if not valid_trade:
        flash('Selected trade is not available. Please use Trades section.', 'warning')
        return redirect(url_for('hdc_workers'))

    w.worker_code = code
    w.name = name
    w.role_type = valid_trade.name
    w.wage_type = wage_type if wage_type in ('daily', 'hourly', 'per_sqft') else 'daily'
    w.base_daily_wage = _flt(request.form.get('daily_wage'))
    w.hourly_rate = _flt(request.form.get('hourly_rate'))
    w.rate_per_sqft = _flt(request.form.get('rate_per_sqft'))
    w.active_status = (active_status == 'active')
    db.session.commit()
    flash(f'Worker "{w.name}" updated.', 'success')
    return redirect(url_for('hdc_workers'))

@app.route('/hdc/workers/<int:wid>/ledger')
@login_required
def hdc_worker_ledger(wid):
    w = Worker.query.get_or_404(wid)
    filter_show_voided = (request.args.get('show_voided') or '1').strip().lower() in ('1', 'true', 'on', 'yes')
    page = max(1, request.args.get('page', type=int) or 1)
    try:
        per_page = int(request.args.get('per_page', type=int) or 10)
    except Exception:
        per_page = 10
    per_page = max(10, min(per_page, 200))
    _reconcile_worker_time_entries(wid)
    _repair_worker_work_ledger_links(wid)
    _reconcile_worker_tip_ledger(w)
    db.session.commit()
    projects = Project.query.all()
    workers = Worker.query.order_by(Worker.name).all()
    # Full ledger ordered chronologically — running balance must reflect ALL
    # entries from the start, regardless of which page is shown.
    full_ledger = (LabourLedger.query
                   .filter(LabourLedger.worker_id == wid)
                   .order_by(LabourLedger.activity_at.asc(), LabourLedger.id.asc())
                   .all())
    if not filter_show_voided:
        full_ledger = [e for e in full_ledger if not bool(getattr(e, 'is_void', False))]
    running_balance_map = {}
    running_balance = 0.0
    for entry in full_ledger:
        delta = 0.0
        if not bool(getattr(entry, 'is_void', False)):
            et = str(getattr(entry, 'entry_type', '') or '').strip().lower()
            amt = float(getattr(entry, 'amount', 0.0) or 0.0)
            if et == 'work':
                delta = amt
            elif et in ('advance', 'payment', 'settlement'):
                delta = -amt
            # tip → delta stays 0 (gratis cash, neutral on the worker's
            # owed balance — see _worker_payable_snapshot for the rationale).
        running_balance += delta
        running_balance_map[int(getattr(entry, 'id', 0) or 0)] = float(running_balance)
    pg_total_items = len(full_ledger)
    pg_total_pages = max(1, (pg_total_items + per_page - 1) // per_page) if pg_total_items else 1
    page = min(page, pg_total_pages)
    start = (page - 1) * per_page
    ledger_entries = full_ledger[start:start + per_page]
    time_entries = (TimeEntry.query
                    .filter(TimeEntry.worker_id == wid, TimeEntry.is_void == False)
                    .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
                    .all())
    snap = _worker_payable_snapshot(wid)
    tip_rows = (LabourLedger.query
                .filter(
                    LabourLedger.worker_id == wid,
                    LabourLedger.entry_type == 'tip',
                    LabourLedger.is_void == False
                )
                .order_by(LabourLedger.activity_at.asc(), LabourLedger.id.asc())
                .all())
    settlement_rows = (LabourLedger.query
                       .filter(
                           LabourLedger.worker_id == wid,
                           LabourLedger.entry_type == 'settlement',
                           LabourLedger.is_void == False
                       )
                       .order_by(LabourLedger.activity_at.asc(), LabourLedger.id.asc())
                       .all())
    pg_query = {
        'show_voided': (1 if filter_show_voided else 0),
        'per_page': per_page,
    }
    return render_template(
        'worker_ledger.html',
        w=w,
        projects=projects,
        workers=workers,
        ledger_entries=ledger_entries,
        filter_show_voided=filter_show_voided,
        running_balance_map=running_balance_map,
        time_entries=time_entries,
        snap=snap,
        tip_rows=tip_rows,
        settlement_rows=settlement_rows,
        pg_page=page,
        pg_total_pages=pg_total_pages,
        pg_total_items=pg_total_items,
        pg_per_page=per_page,
        pg_endpoint='hdc_worker_ledger',
        pg_url_kwargs={'wid': wid},
        pg_query=pg_query,
    )

@app.route('/hdc/workers/<int:wid>/advance', methods=['GET', 'POST'])
@login_required
def hdc_worker_advance(wid):
    w = Worker.query.get_or_404(wid)
    projects = Project.query.all()
    if request.method == 'POST':
        entry_date = _parse_date(request.form.get('date'))
        amount = _flt(request.form.get('amount'))
        if amount <= 0:
            flash('Advance amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_worker_advance', wid=wid))
        project_id = request.form.get('project_id', type=int) or None
        stage_id = request.form.get('stage_id', type=int) or None
        notes = (request.form.get('notes','') or '').strip()
        if _has_recent_duplicate(
            LabourLedger,
            worker_id=wid,
            entry_type='advance',
            amount=amount,
            date=entry_date,
            project_id=project_id,
            stage_id=stage_id,
            notes=notes
        ):
            flash('Duplicate advance prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        adv_row = LabourLedger(
            worker_id=wid, entry_type='advance',
            amount=amount,
            date=entry_date,
            activity_at=_activity_at_for(entry_date),
            project_id=project_id,
            stage_id=stage_id,
            notes=notes)
        db.session.add(adv_row)
        db.session.flush()
        ok_txn, msg_txn, _ = _accounts_post_labour_ledger_row(adv_row, worker_name=w.name, commit=False)
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to post advance in unified accounts.', 'danger')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        db.session.commit()
        flash(f'Advance of {_flt(request.form.get("amount")):,.0f} recorded for {w.name}.', 'success')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    stages = Stage.query.all()
    return render_template('worker_advance.html', w=w, projects=projects, stages=stages, today=_pkt_today().isoformat())

@app.route('/hdc/workers/<int:wid>/payment', methods=['GET', 'POST'])
@login_required
def hdc_worker_payment(wid):
    w = Worker.query.get_or_404(wid)
    projects = Project.query.all()
    stages = Stage.query.all()
    snap = _worker_payable_snapshot(wid)
    if request.method == 'POST':
        entry_date = _parse_date(request.form.get('date'))
        amount = _flt(request.form.get('amount'))
        project_id = request.form.get('project_id', type=int) or None
        stage_id = request.form.get('stage_id', type=int) or None
        settle_shortfall = (request.form.get('settle_shortfall') or '').strip().lower() in ('1', 'true', 'on', 'yes')
        overpay_as_tip = (request.form.get('overpay_as_tip') or '').strip().lower() in ('1', 'true', 'on', 'yes')
        overpay_as_advance = (request.form.get('overpay_as_advance') or '').strip().lower() in ('1', 'true', 'on', 'yes')
        notes = (request.form.get('notes','') or '').strip()
        payment_row = None
        tip_row = None
        advance_row = None
        if amount <= 0:
            flash('Payment amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_worker_payment', wid=wid))

        current = _worker_payable_snapshot(wid)
        payable_now = float(current['payable'] or 0.0)
        payment_part = min(amount, payable_now)
        overpay_part = max(0.0, amount - payment_part)
        tip_part = 0.0
        advance_part = 0.0
        settlement_part = 0.0
        if settle_shortfall and amount < payable_now:
            settlement_part = payable_now - amount

        if overpay_as_tip and overpay_as_advance:
            flash('Select only one overpayment option: Tip or Excess as Advance.', 'warning')
            return redirect(url_for('hdc_worker_payment', wid=wid))

        if overpay_part > 0:
            if overpay_as_tip:
                tip_part = overpay_part
            elif overpay_as_advance:
                advance_part = overpay_part
            else:
                flash('Amount exceeds payable. Please choose Tip or Excess as Advance for the extra amount.', 'danger')
                return redirect(url_for('hdc_worker_payment', wid=wid))
        elif overpay_as_tip or overpay_as_advance:
            flash('Tip/Advance overpayment option applies only when amount exceeds payable.', 'warning')
            return redirect(url_for('hdc_worker_payment', wid=wid))

        if tip_part > 0 or settlement_part > 0:
            if not project_id or not stage_id:
                flash('For tip or shortfall settlement, select both project and stage for proper stage-level trace.', 'warning')
                return redirect(url_for('hdc_worker_payment', wid=wid))
            stg = Stage.query.get(stage_id)
            if (not stg) or (stg.project_id != project_id):
                flash('Selected stage does not belong to selected project.', 'danger')
                return redirect(url_for('hdc_worker_payment', wid=wid))

        if payment_part > 0:
            if _has_recent_duplicate(
                LabourLedger,
                worker_id=wid,
                entry_type='payment',
                amount=payment_part,
                date=entry_date,
                project_id=project_id,
                stage_id=stage_id,
                notes=notes
            ):
                flash('Duplicate payment prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_worker_ledger', wid=wid))
            payment_row = LabourLedger(
                worker_id=wid, entry_type='payment',
                amount=payment_part,
                date=entry_date,
                activity_at=_activity_at_for(entry_date),
                project_id=project_id,
                stage_id=stage_id,
                notes=notes or f'Worker payment for {w.name}'
            )
            db.session.add(payment_row)

        if tip_part > 0:
            tip_cat = _ensure_expense_category('Tip')
            tip_remarks = (notes + ' | ' if notes else '') + f'Tip for {w.name} via settlement overpayment | TIP_WORKER_ID:{wid}'
            if _has_recent_duplicate(
                Expense,
                project_id=project_id,
                stage_id=stage_id,
                tip_worker_id=wid,
                category_id=(tip_cat.id if tip_cat else None),
                amount=tip_part,
                date=entry_date,
                remarks=tip_remarks
            ):
                flash('Duplicate tip expense prevented.', 'warning')
                return redirect(url_for('hdc_worker_ledger', wid=wid))
            db.session.add(Expense(
                project_id=project_id,
                stage_id=stage_id,
                tip_worker_id=wid,
                category_id=(tip_cat.id if tip_cat else None),
                amount=tip_part,
                date=entry_date,
                activity_at=_activity_at_for(entry_date),
                remarks=tip_remarks
            ))
            if _has_recent_duplicate(
                LabourLedger,
                worker_id=wid,
                entry_type='tip',
                amount=tip_part,
                date=entry_date,
                project_id=project_id,
                stage_id=stage_id,
                notes=tip_remarks
            ):
                flash('Duplicate tip ledger record prevented.', 'warning')
                return redirect(url_for('hdc_worker_ledger', wid=wid))
            tip_row = LabourLedger(
                worker_id=wid,
                entry_type='tip',
                amount=tip_part,
                date=entry_date,
                activity_at=_activity_at_for(entry_date),
                project_id=project_id,
                stage_id=stage_id,
                notes=tip_remarks
            )
            db.session.add(tip_row)

        if advance_part > 0:
            advance_notes = (notes + ' | ' if notes else '') + f'Auto advance from overpayment for {w.name}'
            if _has_recent_duplicate(
                LabourLedger,
                worker_id=wid,
                entry_type='advance',
                amount=advance_part,
                date=entry_date,
                project_id=project_id,
                stage_id=stage_id,
                notes=advance_notes
            ):
                flash('Duplicate advance prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_worker_ledger', wid=wid))
            advance_row = LabourLedger(
                worker_id=wid,
                entry_type='advance',
                amount=advance_part,
                date=entry_date,
                activity_at=_activity_at_for(entry_date),
                project_id=project_id,
                stage_id=stage_id,
                notes=advance_notes
            )
            db.session.add(advance_row)

        if settlement_part > 0:
            settlement_remarks = (notes + ' | ' if notes else '') + f'Settlement shortfall for {w.name} | SETTLE_WORKER_ID:{wid}'
            settlement_cat = _ensure_expense_category('Settlement')
            if _has_recent_duplicate(
                Expense,
                project_id=project_id,
                stage_id=stage_id,
                category_id=(settlement_cat.id if settlement_cat else None),
                amount=-settlement_part,
                date=entry_date,
                remarks=settlement_remarks
            ):
                flash('Duplicate settlement expense prevented.', 'warning')
                return redirect(url_for('hdc_worker_ledger', wid=wid))
            db.session.add(Expense(
                project_id=project_id,
                stage_id=stage_id,
                category_id=(settlement_cat.id if settlement_cat else None),
                amount=-settlement_part,
                date=entry_date,
                activity_at=_activity_at_for(entry_date),
                remarks=settlement_remarks
            ))
            if _has_recent_duplicate(
                LabourLedger,
                worker_id=wid,
                entry_type='settlement',
                amount=settlement_part,
                date=entry_date,
                project_id=project_id,
                stage_id=stage_id,
                notes=settlement_remarks
            ):
                flash('Duplicate settlement ledger record prevented.', 'warning')
                return redirect(url_for('hdc_worker_ledger', wid=wid))
            db.session.add(LabourLedger(
                worker_id=wid,
                entry_type='settlement',
                amount=settlement_part,
                date=entry_date,
                activity_at=_activity_at_for(entry_date),
                project_id=project_id,
                stage_id=stage_id,
                notes=settlement_remarks
            ))

        db.session.flush()
        for row in [payment_row, tip_row, advance_row]:
            if not row:
                continue
            ok_txn, msg_txn, _ = _accounts_post_labour_ledger_row(row, worker_name=w.name, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to post worker payment in unified accounts.', 'danger')
                return redirect(url_for('hdc_worker_payment', wid=wid))
        db.session.commit()
        if tip_part > 0 and settlement_part > 0:
            flash(
                f'Payment recorded: {payment_part:,.0f} PKR cash paid, {tip_part:,.0f} PKR posted as Tip, and {settlement_part:,.0f} PKR shortfall settled to stage.',
                'success'
            )
        elif advance_part > 0 and settlement_part > 0:
            flash(
                f'Payment recorded: {payment_part:,.0f} PKR paid against payable, {advance_part:,.0f} PKR saved as Advance, and {settlement_part:,.0f} PKR shortfall settled to stage.',
                'success'
            )
        elif settlement_part > 0:
            flash(
                f'Payment recorded: {payment_part:,.0f} PKR cash paid and {settlement_part:,.0f} PKR shortfall settled to stage.',
                'success'
            )
        elif tip_part > 0:
            flash(
                f'Payment recorded: {payment_part:,.0f} PKR settled worker payable and {tip_part:,.0f} PKR posted as Tip expense.',
                'success'
            )
        elif advance_part > 0:
            flash(
                f'Payment recorded: {payment_part:,.0f} PKR settled worker payable and {advance_part:,.0f} PKR saved as worker advance.',
                'success'
            )
        else:
            flash(f'Payment of {payment_part:,.0f} PKR recorded for {w.name}.', 'success')
        receipt_row = payment_row or tip_row or advance_row
        if receipt_row and receipt_row.id:
            return redirect(url_for('hdc_worker_payment_receipt', wid=wid, lid=receipt_row.id))
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    return render_template(
        'worker_payment.html',
        w=w,
        projects=projects,
        stages=stages,
        today=_pkt_today().isoformat(),
        payable_amount=snap['payable'],
        earned_amount=snap['earned'],
        advanced_amount=snap['advanced'],
        paid_amount=snap['paid'],
        settled_amount=snap['settled']
    )

@app.route('/hdc/workers/<int:wid>/payment/<int:lid>/receipt')
@login_required
def hdc_worker_payment_receipt(wid, lid):
    w = Worker.query.get_or_404(wid)
    row = LabourLedger.query.get_or_404(lid)
    if row.worker_id != wid:
        abort(404)
    receipt_id = f'RCPT-WP-{row.id:08d}'
    project_name = (row.project.name if row.project else '-')
    stage_name = (row.stage.name if row.stage else '-')
    recent = (
        LabourLedger.query
        .filter(LabourLedger.worker_id == wid, LabourLedger.id != row.id, LabourLedger.is_void == False)
        .order_by(LabourLedger.activity_at.desc(), LabourLedger.id.desc())
        .limit(5).all()
    )
    recent_entries = [{
        'date': (r.date.strftime('%Y-%m-%d') if r.date else '-'),
        'type': (r.entry_type or '-').title(),
        'direction': 'pay',
        'party': w.name,
        'amount': float(r.amount or 0),
        'receipt_url': url_for('hdc_worker_payment_receipt', wid=wid, lid=r.id)
    } for r in recent]
    return render_template(
        'transaction_receipt.html',
        company_profile=_receipt_company_profile(),
        receipt_id=receipt_id,
        created_at=(row.activity_at or _pkt_now_naive()),
        tx_type=f'Worker {(row.entry_type or "Payment").title()} Receipt',
        party_name=w.name,
        project_name=project_name,
        stage_name=stage_name,
        account_used='-',
        amount=float(row.amount or 0),
        amount_words=_amount_to_words(row.amount or 0),
        note=(row.notes or ''),
        reference_id=f'worker_ledger#{row.id}',
        recent_entries=recent_entries,
        recent_entries_title=f'Last 5 Ledger Entries – {w.name}',
        back_url=url_for('hdc_worker_ledger', wid=wid),
        print_label='Print / Save PDF'
    )


@app.route('/hdc/workers/<int:wid>/ledger/<int:lid>/edit', methods=['GET', 'POST'])
@login_required
def hdc_worker_ledger_edit(wid, lid):
    w = Worker.query.get_or_404(wid)
    row = LabourLedger.query.get_or_404(lid)
    if row.worker_id != wid:
        flash('Ledger entry does not belong to selected worker.', 'danger')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    if row.entry_type == 'work':
        flash('Work entries cannot be edited.', 'warning')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    if row.is_void:
        flash('Voided entry cannot be edited.', 'warning')
        return redirect(url_for('hdc_worker_ledger', wid=wid))

    projects = Project.query.all()
    stages = Stage.query.all()
    if request.method == 'POST':
        entry_date = _parse_date(request.form.get('date'))
        amount = _flt(request.form.get('amount'))
        if amount <= 0:
            flash('Amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_worker_ledger_edit', wid=wid, lid=lid))
        row.amount = amount
        row.date = entry_date
        row.activity_at = _activity_at_for(entry_date)
        row.project_id = request.form.get('project_id', type=int) or None
        row.stage_id = request.form.get('stage_id', type=int) or None
        row.notes = (request.form.get('notes') or '').strip()
        ok_txn, msg_txn, _ = _accounts_upsert_labour_ledger_txn(w, row, commit=False)
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to sync ledger entry to accounts.', 'danger')
            return redirect(url_for('hdc_worker_ledger_edit', wid=wid, lid=lid))
        db.session.commit()
        flash('Ledger entry updated.', 'success')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    return render_template('worker_ledger_entry_edit.html', w=w, entry=row, projects=projects, stages=stages)

@app.route('/hdc/workers/<int:wid>/ledger/<int:lid>/void', methods=['POST'])
@login_required
def hdc_worker_ledger_void(wid, lid):
    Worker.query.get_or_404(wid)
    row = LabourLedger.query.get_or_404(lid)
    if row.worker_id != wid:
        flash('Ledger entry does not belong to selected worker.', 'danger')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    if row.entry_type == 'work':
        flash('Work entries cannot be voided.', 'warning')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    if row.is_void:
        flash('Ledger entry is already voided.', 'info')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
    row.is_void = True
    row.void_reason = reason
    row.voided_at = _pkt_now_naive()
    _accounts_set_void_by_source(f'labour_ledger_{row.entry_type}', row.id, True)
    _accounts_set_void_by_source('worker_payment', row.id, True)
    db.session.commit()
    flash('Ledger entry voided successfully.', 'success')
    return redirect(url_for('hdc_worker_ledger', wid=wid))


@app.route('/hdc/workers/<int:wid>/ledger/<int:lid>/restore', methods=['POST'])
@login_required
def hdc_worker_ledger_restore(wid, lid):
    Worker.query.get_or_404(wid)
    row = LabourLedger.query.get_or_404(lid)
    if row.worker_id != wid:
        flash('Ledger entry does not belong to selected worker.', 'danger')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    if row.entry_type not in ('advance', 'payment'):
        flash('Only advance/payment entries can be restored manually.', 'warning')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    if not row.is_void:
        flash('Ledger entry is already active.', 'info')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    row.is_void = False
    row.void_reason = None
    row.voided_at = None
    _accounts_set_void_by_source(f'labour_ledger_{row.entry_type}', row.id, False)
    _accounts_set_void_by_source('worker_payment', row.id, False)
    db.session.commit()
    flash('Ledger entry restored successfully.', 'success')
    return redirect(url_for('hdc_worker_ledger', wid=wid))

@app.route('/hdc/workers/<int:wid>/rate', methods=['GET', 'POST'])
@login_required
def hdc_worker_rate(wid):
    w = Worker.query.get_or_404(wid)
    if request.method == 'POST':
        wage_type = request.form.get('wage_type','daily')
        new_rate = _flt(request.form.get('new_rate'))
        eff_from = _parse_date(request.form.get('effective_from'))
        old_wage_type = w.wage_type or 'daily'
        old_rate = w.base_daily_wage if old_wage_type == 'daily' else (w.hourly_rate if old_wage_type == 'hourly' else w.rate_per_sqft)

        # Ensure historical baseline exists so backdated entries can resolve prior rates.
        if not WorkerRate.query.filter_by(worker_id=wid).first():
            baseline_from = w.created_at.date() if w.created_at else _pkt_today()
            db.session.add(WorkerRate(
                worker_id=wid, wage_type=old_wage_type, rate=old_rate,
                effective_from=baseline_from,
                reason='Initial baseline (auto snapshot)'
            ))

        db.session.add(WorkerRate(
            worker_id=wid, wage_type=wage_type, rate=new_rate,
            effective_from=eff_from,
            reason=request.form.get('reason','')))
        if wage_type == 'daily':
            db.session.add(LabourRateHistory(
                worker_id=wid, old_rate=old_rate, new_rate=new_rate,
                effective_from=eff_from,
                reason=request.form.get('reason','')))
        w.wage_type = wage_type
        if wage_type == 'daily':
            w.base_daily_wage = new_rate
        elif wage_type == 'hourly':
            w.hourly_rate = new_rate
        else:
            w.rate_per_sqft = new_rate
        db.session.commit()
        flash(f'Rate updated from {old_rate:,.0f} ? {new_rate:,.0f}.', 'success')
        return redirect(url_for('hdc_worker_ledger', wid=wid))
    return render_template('worker_rate.html', w=w, today=_pkt_today().isoformat())


# â”€â”€ Attendance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/attendance', methods=['GET', 'POST'])
@app.route('/hdc/timekeeping', methods=['GET', 'POST'])
@login_required
def hdc_attendance():
    if request.method == 'POST':
        if (request.form.get('bulk_mode') or '').strip() == '1':
            entry_date = _parse_date(request.form.get('date'))
            workers_bulk = (Worker.query
                            .filter_by(active_status=True)
                            .order_by(Worker.name.asc(), Worker.id.asc())
                            .all())
            day_start = datetime.combine(entry_date, datetime.min.time())
            day_end = datetime.combine(entry_date, datetime.max.time())
            updated_workers = 0
            skipped_workers = 0

            for wk in workers_bulk:
                wid = int(wk.id)
                row_status = (request.form.get(f'status_{wid}') or 'not_assigned').strip().lower()
                project_id = request.form.get(f'project_id_{wid}', type=int)
                stage_id = request.form.get(f'stage_id_{wid}', type=int)
                working_hours = max(0.0, float(_flt(request.form.get(f'working_hours_{wid}'), 0.0) or 0.0))
                overtime_hours = max(0.0, float(_flt(request.form.get(f'overtime_hours_{wid}'), 0.0) or 0.0))
                row_allocations = _parse_attendance_entries_payload(request.form.get(f'allocations_{wid}'))
                row_notes = (request.form.get(f'remarks_{wid}') or '').strip()

                if row_status not in ('present', 'absent', 'leave', 'not_assigned'):
                    row_status = 'not_assigned'

                active_entries = (TimeEntry.query
                                  .filter(TimeEntry.worker_id == wid,
                                          TimeEntry.is_void == False,
                                          TimeEntry.check_in >= day_start,
                                          TimeEntry.check_in <= day_end)
                                  .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
                                  .all())
                mark = AttendanceMark.query.filter_by(worker_id=wid, date=entry_date).first()

                if row_status == 'present':
                    normalized = []
                    total_hours = 0.0
                    if row_allocations:
                        for alloc in row_allocations:
                            pid = int(alloc.get('project_id') or 0)
                            sid = int(alloc.get('stage_id') or 0)
                            hrs = max(0.0, float(_flt(alloc.get('hours'), 0.0) or 0.0))
                            if (not pid) or (not sid) or hrs <= 0:
                                normalized = []
                                break
                            stg = Stage.query.get(sid)
                            if (not stg) or (int(stg.project_id or 0) != int(pid or 0)):
                                normalized = []
                                break
                            normalized.append({'project_id': pid, 'stage_id': sid, 'hours': hrs})
                            total_hours += hrs
                    else:
                        total_hours = working_hours + overtime_hours
                        if project_id and stage_id and total_hours > 0:
                            stg = Stage.query.get(stage_id)
                            if stg and (int(stg.project_id or 0) == int(project_id or 0)):
                                normalized = [{'project_id': int(project_id), 'stage_id': int(stage_id), 'hours': total_hours}]

                    if (not normalized) or total_hours <= 0 or total_hours > 24:
                        skipped_workers += 1
                        continue

                    for te in active_entries:
                        te.is_void = True
                        te.void_reason = 'Replaced by bulk attendance sheet'
                        te.voided_at = _pkt_now_naive()
                        _sync_work_ledger_for_time_entry(te)
                        _void_orphan_work_ledgers_for_time_entry(te, reason='Replaced by bulk attendance sheet')

                    running_hours = 0.0
                    regular_done = 0.0
                    for row in normalized:
                        hrs = row['hours']
                        remaining_regular = max(0.0, 8.0 - regular_done)
                        regular_hours = min(hrs, remaining_regular)
                        overtime_part = max(0.0, hrs - regular_hours)
                        check_in = day_start + timedelta(hours=running_hours)
                        check_out = check_in + timedelta(hours=hrs)
                        wage = _calc_time_wage(wk, regular_hours, overtime_part, 0.0, work_date=entry_date)
                        te = TimeEntry(
                            worker_id=wid,
                            project_id=row['project_id'],
                            stage_id=row['stage_id'],
                            check_in=check_in,
                            check_out=check_out,
                            hours=hrs,
                            overtime=overtime_part,
                            qty_sqft=0.0,
                            wage_calculated=wage,
                            legacy_calc=False,
                            attendance_id=None,
                            activity_at=check_in
                        )
                        db.session.add(te)
                        db.session.flush()
                        _sync_work_ledger_for_time_entry(te)
                        running_hours += hrs
                        regular_done += regular_hours

                    if mark:
                        mark.status = 'present'
                        mark.notes = row_notes
                        mark.activity_at = _activity_at_for(entry_date)
                    else:
                        db.session.add(AttendanceMark(
                            worker_id=wid,
                            date=entry_date,
                            status='present',
                            notes=row_notes,
                            activity_at=_activity_at_for(entry_date)
                        ))
                    updated_workers += 1
                    continue

                for te in active_entries:
                    te.is_void = True
                    te.void_reason = f'Bulk sheet set as {row_status.replace("_", " ")}'
                    te.voided_at = _pkt_now_naive()
                    _sync_work_ledger_for_time_entry(te)
                    _void_orphan_work_ledgers_for_time_entry(te, reason=f'Bulk sheet set as {row_status.replace("_", " ")}')

                if row_status in ('absent', 'leave'):
                    if mark:
                        mark.status = row_status
                        mark.notes = row_notes
                        mark.activity_at = _activity_at_for(entry_date)
                    else:
                        db.session.add(AttendanceMark(
                            worker_id=wid,
                            date=entry_date,
                            status=row_status,
                            notes=row_notes,
                            activity_at=_activity_at_for(entry_date)
                        ))
                    updated_workers += 1
                else:
                    if mark:
                        db.session.delete(mark)

            db.session.flush()
            for wk in workers_bulk:
                _recalculate_attendance_day(int(wk.id), entry_date)
            db.session.commit()
            flash(f'Bulk attendance sheet saved for {updated_workers} worker(s). Skipped {skipped_workers} invalid row(s).', 'success')
            return redirect(url_for('hdc_attendance', sheet_date=entry_date.isoformat()))

        entry_status = (request.form.get('entry_status') or 'present').strip().lower()
        wid = request.form.get('worker_id', type=int)
        if not wid:
            flash('Please select a valid worker.', 'danger')
            return redirect(url_for('hdc_attendance'))

        entry_date = _parse_date(request.form.get('date'))

        if entry_status == 'absent':
            day_start = datetime.combine(entry_date, datetime.min.time())
            day_end = datetime.combine(entry_date, datetime.max.time())
            has_time = TimeEntry.query.filter(
                TimeEntry.worker_id == wid,
                TimeEntry.is_void == False,
                TimeEntry.check_in >= day_start,
                TimeEntry.check_in <= day_end
            ).first()
            if has_time:
                flash('This worker already has a present entry on selected date.', 'warning')
                return redirect(url_for('hdc_attendance'))

            mark = AttendanceMark.query.filter_by(worker_id=wid, date=entry_date).first()
            notes = (request.form.get('absent_notes') or '').strip()
            if mark:
                mark.status = 'absent'
                mark.notes = notes
                mark.activity_at = _activity_at_for(entry_date)
            else:
                db.session.add(AttendanceMark(
                    worker_id=wid,
                    date=entry_date,
                    status='absent',
                    notes=notes,
                    activity_at=_activity_at_for(entry_date)
                ))
            db.session.commit()
            worker = Worker.query.get(wid)
            worker_label = worker.name if worker else f'Worker #{wid}'
            log_action(
                current_user,
                'update',
                f'{current_user.username.title()} updated attendance of {worker_label}: status set to Absent on {entry_date.isoformat()}',
                'attendance',
                wid
            )
            db.session.commit()
            flash('Absent marked successfully.', 'success')
            return redirect(url_for('hdc_attendance'))

        day_start = datetime.combine(entry_date, datetime.min.time())
        day_end = datetime.combine(entry_date, datetime.max.time())
        existing = TimeEntry.query.filter(
            TimeEntry.worker_id == wid,
            TimeEntry.is_void == False,
            TimeEntry.check_in >= day_start,
            TimeEntry.check_in <= day_end
        ).first()
        if existing:
            flash('Duplicate attendance blocked: this worker already has attendance for this date.', 'warning')
            return redirect(url_for('hdc_attendance'))

        items = _parse_attendance_entries_payload(request.form.get('entries_json'))
        if not items:
            flash('Please add at least one site/stage entry with hours.', 'danger')
            return redirect(url_for('hdc_attendance'))

        total_hours = 0.0
        normalized = []
        for row in items:
            pid = int(row.get('project_id') or 0)
            sid = int(row.get('stage_id') or 0)
            hrs = _flt(row.get('hours'), 0.0)
            if not pid or not sid:
                flash('Each entry must include a valid site and stage.', 'danger')
                return redirect(url_for('hdc_attendance'))
            if hrs <= 0:
                flash('Hours must be greater than 0 for every entry.', 'danger')
                return redirect(url_for('hdc_attendance'))
            stage = Stage.query.get(sid)
            if (not stage) or stage.project_id != pid:
                flash('Selected stage does not belong to selected site/project.', 'danger')
                return redirect(url_for('hdc_attendance'))
            normalized.append({'project_id': pid, 'stage_id': sid, 'hours': hrs})
            total_hours += hrs

        if total_hours > 24:
            flash('Total hours cannot exceed 24 in a single day.', 'danger')
            return redirect(url_for('hdc_attendance'))

        worker = Worker.query.get_or_404(wid)
        day_row = AttendanceDay.query.filter_by(worker_id=wid, date=entry_date).first()
        if not day_row:
            day_row = AttendanceDay(
                worker_id=wid,
                date=entry_date,
                total_hours=0.0,
                day_value=0.0,
                overtime_hours=0.0,
                entry_count=0,
                is_void=False
            )
            db.session.add(day_row)
            db.session.flush()
        else:
            day_row.is_void = False

        running_hours = 0.0
        regular_done = 0.0
        for row in normalized:
            hrs = row['hours']
            remaining_regular = max(0.0, 8.0 - regular_done)
            regular_hours = min(hrs, remaining_regular)
            overtime_hours = max(0.0, hrs - regular_hours)
            ci = day_start + timedelta(hours=running_hours)
            co = ci + timedelta(hours=hrs)
            wage = _calc_time_wage(worker, regular_hours, overtime_hours, 0.0, work_date=entry_date)
            te = TimeEntry(
                worker_id=wid,
                project_id=row['project_id'],
                stage_id=row['stage_id'],
                check_in=ci,
                check_out=co,
                hours=hrs,
                overtime=overtime_hours,
                qty_sqft=0.0,
                wage_calculated=wage,
                legacy_calc=False,
                attendance_id=day_row.id,
                activity_at=ci
            )
            db.session.add(te)
            db.session.flush()
            _sync_work_ledger_for_time_entry(te)
            running_hours += hrs
            regular_done += regular_hours

        summary = _recalculate_attendance_day(wid, entry_date)
        mark = AttendanceMark.query.filter_by(worker_id=wid, date=entry_date).first()
        if mark and mark.status == 'absent':
            db.session.delete(mark)
        worker_name = worker.name if worker else f'Worker #{wid}'
        entries_txt = []
        for row in normalized:
            proj = Project.query.get(row['project_id'])
            stg = Stage.query.get(row['stage_id'])
            entries_txt.append(f"{proj.name if proj else row['project_id']} -> {stg.name if stg else row['stage_id']} ({float(row['hours']):.2f} hrs)")
        log_action(
            current_user,
            'create',
            f'{current_user.username.title()} created attendance for {worker_name}: ' + '; '.join(entries_txt),
            'attendance',
            wid
        )
        db.session.commit()
        flash(f"Attendance saved: {summary['total_hours']:.2f}h, Day={summary['day']:.0f}, OT={summary['overtime']:.2f}h.", 'success')
        return redirect(url_for('hdc_attendance'))

    projects = Project.query.all()
    workers = Worker.query.filter_by(active_status=True).all()
    trade_options = _trade_options()
    stages = Stage.query.all()
    status_cards = _timekeeping_status_dataset(_pkt_today(), 'assigned')

    filter_project_id = request.args.get('project_id', type=int)
    filter_stage_id = request.args.get('stage_id', type=int)
    filter_worker_id = request.args.get('worker_id', type=int)
    filter_worker_name = (request.args.get('worker_name') or '').strip()
    filter_trade = (request.args.get('trade') or '').strip()
    filter_date_from = request.args.get('date_from')
    filter_date_to = request.args.get('date_to')
    filter_show_voided = (request.args.get('show_voided') or '').strip().lower() in ('1', 'true', 'on', 'yes')

    q = (db.session.query(TimeEntry, Worker, Project)
         .join(Worker, TimeEntry.worker_id == Worker.id)
         .join(Project, TimeEntry.project_id == Project.id))
    if not filter_show_voided:
        q = q.filter(TimeEntry.is_void == False)

    if filter_project_id:
        q = q.filter(TimeEntry.project_id == filter_project_id)
    if filter_stage_id:
        q = q.filter(TimeEntry.stage_id == filter_stage_id)
    if filter_worker_id:
        q = q.filter(TimeEntry.worker_id == filter_worker_id)
    if filter_worker_name:
        q = q.filter(Worker.name.ilike(f"%{filter_worker_name}%"))
    if filter_trade:
        q = q.filter(Worker.role_type.ilike(f"%{filter_trade}%"))
    if filter_date_from:
        q = q.filter(TimeEntry.check_in >= datetime.strptime(filter_date_from, '%Y-%m-%d'))
    if filter_date_to:
        q = q.filter(TimeEntry.check_in <= datetime.strptime(filter_date_to, '%Y-%m-%d') + timedelta(days=1))

    has_filters = any([
        filter_project_id, filter_stage_id, filter_worker_id,
        filter_worker_name, filter_trade, filter_date_from, filter_date_to,
        filter_show_voided
    ])
    if has_filters:
        records = q.order_by(TimeEntry.check_in.desc()).all()
    else:
        records = q.order_by(TimeEntry.check_in.desc()).limit(180).all()

    grouped = {}
    for te, wk, proj in records:
        key = (wk.id, te.check_in.date())
        row = grouped.get(key)
        if not row:
            row = {
                'date': te.check_in.date(),
                'worker': wk,
                'entries': [],
                'total_hours': 0.0,
                'overtime': 0.0,
                'total_wage': 0.0,
                'active_count': 0,
                'void_count': 0
            }
            grouped[key] = row
        row['entries'].append({
            'id': te.id,
            'project': proj,
            'stage': te.stage,
            'hours': float(te.hours or 0.0),
            'overtime': float(te.overtime or 0.0),
            'wage': float(te.wage_calculated or 0.0),
            'is_void': bool(te.is_void),
            'check_in': te.check_in
        })
        if te.is_void:
            row['void_count'] += 1
        else:
            row['active_count'] += 1
            row['total_hours'] += float(te.hours or 0.0)
            row['overtime'] += float(te.overtime or 0.0)
            row['total_wage'] += float(te.wage_calculated or 0.0)

    attendance_rows = list(grouped.values())
    attendance_rows.sort(key=lambda r: (r['date'], (r['worker'].name or '').lower()), reverse=True)
    for row in attendance_rows:
        row['day'] = 1 if row['total_hours'] >= 8.0 else 0
        row['status'] = 'Voided' if row['active_count'] == 0 else 'Active'
        row['entries'].sort(key=lambda e: e['check_in'])

    sheet_date = _parse_date(request.args.get('sheet_date'), fallback=_pkt_today())
    day_start = datetime.combine(sheet_date, datetime.min.time())
    day_end = datetime.combine(sheet_date, datetime.max.time())
    active_workers = (Worker.query
                      .filter_by(active_status=True)
                      .order_by(Worker.name.asc(), Worker.id.asc())
                      .all())
    worker_ids = [int(w.id) for w in active_workers]
    day_entries = (TimeEntry.query
                   .filter(TimeEntry.worker_id.in_(worker_ids),
                           TimeEntry.is_void == False,
                           TimeEntry.check_in >= day_start,
                           TimeEntry.check_in <= day_end)
                   .order_by(TimeEntry.worker_id.asc(), TimeEntry.check_in.asc(), TimeEntry.id.asc())
                   .all()) if worker_ids else []
    marks = (AttendanceMark.query
             .filter(AttendanceMark.worker_id.in_(worker_ids),
                     AttendanceMark.date == sheet_date)
             .all()) if worker_ids else []
    mark_map = {int(m.worker_id): m for m in marks}
    entry_map = {}
    for te in day_entries:
        entry_map.setdefault(int(te.worker_id), []).append(te)

    daily_sheet_rows = []
    for wk in active_workers:
        rows = entry_map.get(int(wk.id), [])
        mk = mark_map.get(int(wk.id))
        if rows:
            total_hours = sum(float(r.hours or 0.0) for r in rows)
            ot_hours = sum(float(r.overtime or 0.0) for r in rows)
            working_hours = max(0.0, total_hours - ot_hours)
            projects_txt = ', '.join(list(dict.fromkeys([(r.project.name if r.project else '-') for r in rows])))
            stages_txt = ', '.join(list(dict.fromkeys([(r.stage.name if r.stage else '-') for r in rows])))
            status_txt = 'Present'
            remarks_txt = (mk.notes if mk and (mk.notes or '').strip() else '-')
        elif mk and (mk.status or '').strip().lower() in ('absent', 'leave'):
            total_hours = 0.0
            ot_hours = 0.0
            working_hours = 0.0
            projects_txt = '-'
            stages_txt = '-'
            status_txt = (mk.status or '').strip().capitalize()
            remarks_txt = (mk.notes or '-')
        else:
            total_hours = 0.0
            ot_hours = 0.0
            working_hours = 0.0
            projects_txt = '-'
            stages_txt = '-'
            status_txt = 'Not Assigned'
            remarks_txt = '-'
        allocations = [{
            'project_id': int(r.project_id or 0),
            'stage_id': int(r.stage_id or 0),
            'hours': float(r.hours or 0.0)
        } for r in rows]
        daily_sheet_rows.append({
            'worker': wk,
            'status': status_txt,
            'project_name': projects_txt,
            'stage_name': stages_txt,
            'working_hours': working_hours,
            'overtime_hours': ot_hours,
            'remarks': remarks_txt,
            'allocations': allocations
        })

    return render_template('timekeeping.html',
        projects=projects, workers=workers, stages=stages, attendance_rows=attendance_rows,
        today=_pkt_today().isoformat(),
        sheet_date=sheet_date.isoformat(),
        daily_sheet_rows=daily_sheet_rows,
        status_cards_date=status_cards['status_date'].isoformat(),
        assigned_count=status_cards['assigned_count'],
        absent_count=status_cards['absent_count'],
        not_assigned_count=status_cards['not_assigned_count'],
        filter_project_id=filter_project_id,
        filter_stage_id=filter_stage_id,
        filter_worker_id=filter_worker_id,
        filter_worker_name=filter_worker_name,
        filter_trade=filter_trade,
        filter_date_from=filter_date_from,
        filter_date_to=filter_date_to,
        filter_show_voided=filter_show_voided,
        has_filters=has_filters,
        trade_options=trade_options)

@app.route('/hdc/timekeeping/status')
@login_required
def hdc_timekeeping_status():
    status_date = _parse_date(request.args.get('status_date'), fallback=_pkt_today())
    status_view = (request.args.get('status_view') or 'assigned').strip().lower()
    data = _timekeeping_status_dataset(status_date, status_view)
    return render_template(
        'timekeeping_status.html',
        status_date=data['status_date'].isoformat(),
        status_view=data['status_view'],
        assigned_count=data['assigned_count'],
        absent_count=data['absent_count'],
        not_assigned_count=data['not_assigned_count'],
        status_rows=data['status_rows']
    )

@app.route('/hdc/timekeeping/<int:tid>/edit', methods=['GET', 'POST'])
@login_required
def hdc_edit_attendance(tid):
    t = TimeEntry.query.get_or_404(tid)
    if t.is_void:
        flash('Voided time entry cannot be edited.', 'warning')
        return redirect(url_for('hdc_attendance'))

    projects = Project.query.all()
    stages = Stage.query.all()
    if request.method == 'POST':
        pid = request.form.get('project_id', type=int)
        sid = request.form.get('stage_id', type=int)
        hours = _flt(request.form.get('hours'), 0.0)
        if not pid or not sid:
            flash('Site/project and stage are required.', 'danger')
            return redirect(url_for('hdc_edit_attendance', tid=tid))
        if hours <= 0:
            flash('Hours must be greater than 0.', 'danger')
            return redirect(url_for('hdc_edit_attendance', tid=tid))

        stage = Stage.query.get(sid)
        if (not stage) or (stage.project_id != pid):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_edit_attendance', tid=tid))

        work_date = t.check_in.date()
        day_start = datetime.combine(work_date, datetime.min.time())
        day_end = datetime.combine(work_date, datetime.max.time())
        other_day_total = (db.session.query(func.sum(TimeEntry.hours))
                           .filter(TimeEntry.worker_id == t.worker_id,
                                   TimeEntry.is_void == False,
                                   TimeEntry.id != t.id,
                                   TimeEntry.check_in >= day_start,
                                   TimeEntry.check_in <= day_end)
                           .scalar()) or 0.0
        if float(other_day_total) + float(hours) > 24.0:
            flash('Total hours cannot exceed 24 in a single day.', 'danger')
            return redirect(url_for('hdc_edit_attendance', tid=tid))

        old_project = Project.query.get(t.project_id)
        old_stage = Stage.query.get(t.stage_id) if t.stage_id else None
        old_hours = float(t.hours or 0.0)
        t.project_id = pid
        t.stage_id = sid
        t.hours = hours
        t.qty_sqft = 0.0
        _recalculate_attendance_day(t.worker_id, work_date)
        new_project = Project.query.get(pid)
        new_stage = Stage.query.get(sid)
        worker_name = t.worker.name if t.worker else f'Worker #{t.worker_id}'
        log_action(
            current_user,
            'update',
            (
                f'{current_user.username.title()} updated attendance of {worker_name}: '
                f'project {(old_project.name if old_project else "-")} -> {(new_project.name if new_project else "-")}; '
                f'stage {(old_stage.name if old_stage else "-")} -> {(new_stage.name if new_stage else "-")}; '
                f'hours {old_hours:.2f} hrs -> {float(hours):.2f} hrs'
            ),
            'attendance',
            t.id
        )
        db.session.commit()
        flash('Attendance entry updated.', 'success')
        return redirect(url_for('hdc_attendance'))

    return render_template(
        'timekeeping_edit.html',
        t=t,
        projects=projects,
        stages=stages,
        today=t.check_in.date().isoformat()
    )

@app.route('/hdc/timekeeping/<int:tid>/delete', methods=['POST'])
@login_required
def hdc_delete_attendance(tid):
    t = TimeEntry.query.get_or_404(tid)
    if t.is_void:
        flash('Time entry already voided.', 'info')
        return redirect(url_for('hdc_attendance'))
    reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
    t.is_void = True
    t.void_reason = reason
    t.voided_at = _pkt_now_naive()
    _sync_work_ledger_for_time_entry(t)
    _void_orphan_work_ledgers_for_time_entry(t, reason=reason)
    _recalculate_attendance_day(t.worker_id, t.check_in.date())
    worker_name = t.worker.name if t.worker else f'Worker #{t.worker_id}'
    log_action(
        current_user,
        'void',
        f'{current_user.username.title()} voided attendance of {worker_name}: {float(t.hours or 0.0):.2f} hrs on {t.check_in.date().isoformat()}',
        'attendance',
        t.id
    )
    db.session.commit()
    flash('Time entry voided.', 'success')
    return redirect(url_for('hdc_attendance'))

@app.route('/hdc/timekeeping/<int:tid>/reactivate', methods=['POST'])
@login_required
def hdc_reactivate_attendance(tid):
    t = TimeEntry.query.get_or_404(tid)
    if not t.is_void:
        flash('Time entry is already active.', 'info')
        return redirect(url_for('hdc_attendance', show_voided=1))

    work_date = t.check_in.date()
    day_start = datetime.combine(work_date, datetime.min.time())
    day_end = datetime.combine(work_date, datetime.max.time())
    day_total_without_t = (db.session.query(func.sum(TimeEntry.hours))
                           .filter(TimeEntry.worker_id == t.worker_id,
                                   TimeEntry.is_void == False,
                                   TimeEntry.check_in >= day_start,
                                   TimeEntry.check_in <= day_end)
                           .scalar()) or 0.0
    if float(day_total_without_t) + float(t.hours or 0.0) > 24.0:
        flash('Cannot reactivate: total day hours would exceed 24.', 'warning')
        return redirect(url_for('hdc_attendance', show_voided=1))

    t.is_void = False
    t.void_reason = None
    t.voided_at = None
    _recalculate_attendance_day(t.worker_id, work_date)
    worker_name = t.worker.name if t.worker else f'Worker #{t.worker_id}'
    log_action(
        current_user,
        'update',
        f'{current_user.username.title()} reactivated attendance of {worker_name}: {float(t.hours or 0.0):.2f} hrs on {work_date.isoformat()}',
        'attendance',
        t.id
    )
    db.session.commit()
    flash('Time entry reactivated.', 'success')
    return redirect(url_for('hdc_attendance', show_voided=1))


# Ã¢â€â‚¬Ã¢â€â‚¬ Payroll Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
@app.route('/hdc/payroll')
@login_required
def hdc_payroll():
    return render_template('payroll.html')

@app.route('/hdc/payroll/generate', methods=['GET', 'POST'])
@login_required
def hdc_payroll_generate():
    if request.method == 'POST':
        action = (request.form.get('action') or 'generate').strip().lower()

        if action == 'pay_worker':
            run_id = request.form.get('run_id', type=int)
            worker_id = request.form.get('worker_id', type=int)
            run = PayrollRun.query.get_or_404(run_id)
            item = PayrollItem.query.filter_by(run_id=run.id, worker_id=worker_id).first()
            if not item:
                flash('Payroll item not found for selected worker.', 'warning')
                return redirect(url_for('hdc_payroll_generate', run_id=run.id))
            note_key = f'Payroll run #{run.id} '
            already_paid = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                                 .filter(
                                     LabourLedger.worker_id == worker_id,
                                     LabourLedger.entry_type == 'payment',
                                     LabourLedger.is_void == False,
                                     LabourLedger.notes.ilike(f'%{note_key}%')
                                 ).scalar() or 0.0)
            payable = float(item.net_pay or 0.0)
            balance = max(0.0, payable - already_paid)
            amount_in = _flt(request.form.get('amount'))
            amount = amount_in if amount_in > 0 else balance
            if amount <= 0 or balance <= 0:
                flash('No payable balance left for this worker in this payroll run.', 'info')
                return redirect(url_for('hdc_payroll_generate', run_id=run.id))
            if amount > balance + 1e-6:
                flash(f'Amount exceeds payroll balance ({balance:,.0f} PKR).', 'warning')
                return redirect(url_for('hdc_payroll_generate', run_id=run.id))
            pay_date = _pkt_today()
            notes = f'Payroll run #{run.id} manual payment ({run.date_from} to {run.date_to})'
            if _has_recent_duplicate(
                LabourLedger,
                worker_id=worker_id,
                entry_type='payment',
                amount=amount,
                date=pay_date,
                notes=notes
            ):
                flash('Duplicate payroll payment prevented.', 'warning')
                return redirect(url_for('hdc_payroll_generate', run_id=run.id))
            pay_row = LabourLedger(
                worker_id=worker_id,
                entry_type='payment',
                amount=amount,
                date=pay_date,
                project_id=None,
                stage_id=None,
                activity_at=_pkt_now_naive(),
                notes=notes
            )
            db.session.add(pay_row)
            db.session.flush()
            ok_txn, msg_txn, _ = _accounts_post_labour_ledger_row(pay_row, worker_name=(item.worker.name if item.worker else ''), commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to post payroll payment in unified accounts.', 'danger')
                return redirect(url_for('hdc_payroll_generate', run_id=run.id))
            db.session.commit()
            flash(f'Payroll payment recorded: {amount:,.0f} PKR.', 'success')
            return redirect(url_for('hdc_payroll_generate', run_id=run.id))

        if action == 'pay_all':
            run_id = request.form.get('run_id', type=int)
            run = PayrollRun.query.get_or_404(run_id)
            items = PayrollItem.query.filter_by(run_id=run.id).all()
            note_key = f'Payroll run #{run.id} '
            created = 0
            total_paid_now = 0.0
            for it in items:
                wid = int(it.worker_id or 0)
                if not wid:
                    continue
                already_paid = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                                     .filter(
                                         LabourLedger.worker_id == wid,
                                         LabourLedger.entry_type == 'payment',
                                         LabourLedger.is_void == False,
                                         LabourLedger.notes.ilike(f'%{note_key}%')
                                     ).scalar() or 0.0)
                payable = float(it.net_pay or 0.0)
                balance = max(0.0, payable - already_paid)
                if balance <= 0:
                    continue
                notes = f'Payroll run #{run.id} bulk payment ({run.date_from} to {run.date_to})'
                pay_row = LabourLedger(
                    worker_id=wid,
                    entry_type='payment',
                    amount=balance,
                    date=_pkt_today(),
                    project_id=None,
                    stage_id=None,
                    activity_at=_pkt_now_naive(),
                    notes=notes
                )
                db.session.add(pay_row)
                db.session.flush()
                wrow = Worker.query.get(wid)
                ok_txn, msg_txn, _ = _accounts_post_labour_ledger_row(pay_row, worker_name=(wrow.name if wrow else ''), commit=False)
                if not ok_txn:
                    db.session.rollback()
                    flash(msg_txn or 'Unable to post payroll payment in unified accounts.', 'danger')
                    return redirect(url_for('hdc_payroll_generate', run_id=run.id))
                created += 1
                total_paid_now += balance
            db.session.commit()
            if created:
                flash(f'Pay All completed: {created} worker(s), {total_paid_now:,.0f} PKR paid.', 'success')
            else:
                flash('No pending payroll balances found for this run.', 'info')
            return redirect(url_for('hdc_payroll_generate', run_id=run.id))

        date_from = _parse_date(request.form.get('date_from'))
        date_to = _parse_date(request.form.get('date_to'))
        start_dt = datetime.combine(date_from, datetime.min.time())
        end_dt = datetime.combine(date_to, datetime.max.time())

        entries = TimeEntry.query.filter(
            TimeEntry.is_void == False,
            TimeEntry.check_in >= start_dt,
            TimeEntry.check_in <= end_dt
        ).all()
        by_worker = {}
        for t in entries:
            info = by_worker.setdefault(t.worker_id, {'hours': 0.0, 'ot': 0.0, 'gross': 0.0})
            info['hours'] += float(t.hours or 0)
            info['ot'] += float(t.overtime or 0)
            info['gross'] += float(t.wage_calculated or 0)

        # Ensure payroll run contains all active workers for clearer salary-card output.
        for w in Worker.query.filter_by(active_status=True).all():
            by_worker.setdefault(w.id, {'hours': 0.0, 'ot': 0.0, 'gross': 0.0})

        run = PayrollRun(date_from=date_from, date_to=date_to, run_date=_pkt_today())
        db.session.add(run); db.session.commit()

        total_amount = 0.0
        for wid, info in by_worker.items():
            advances = sum(l.amount for l in LabourLedger.query.filter_by(worker_id=wid, entry_type='advance', is_void=False)
                           .filter(LabourLedger.date >= date_from, LabourLedger.date <= date_to).all())
            net = max(0.0, info['gross'] - advances)
            item = PayrollItem(
                run_id=run.id, worker_id=wid,
                total_hours=info['hours'], total_overtime=info['ot'],
                gross_amount=info['gross'], advance_deducted=advances, net_pay=net
            )
            db.session.add(item)
            total_amount += net
        run.total_amount = total_amount
        db.session.commit()
        flash('Payroll run created. No worker was paid automatically.', 'success')
        return redirect(url_for('hdc_payroll_generate', run_id=run.id))

    run_id = request.args.get('run_id', type=int)
    runs = PayrollRun.query.order_by(PayrollRun.run_date.desc()).all()
    selected_run = PayrollRun.query.get(run_id) if run_id else None
    items = PayrollItem.query.filter_by(run_id=run_id).all() if run_id else []

    payroll_summary_rows = []
    calendar_days = []
    day_status_rows = []
    worker_calendar_rows = []
    calendar_mode = (request.args.get('calendar_mode') or 'range').strip().lower()
    calendar_month = (request.args.get('calendar_month') or '')
    cal_label = ''
    totals = {
        'workers': 0,
        'work_entries': 0,
        'work_days': 0,
        'absent_days': 0,
        'not_assigned_days': 0,
        'total_wage': 0.0,
        'total_advance': 0.0,
        'total_payable': 0.0,
        'total_paid': 0.0,
        'total_balance': 0.0
    }

    if selected_run:
        range_start = selected_run.date_from
        range_end = selected_run.date_to
        if range_start > range_end:
            range_start, range_end = range_end, range_start

        # Calendar window can be payroll range or full month view.
        cal_start = range_start
        cal_end = range_end
        if calendar_mode == 'month':
            base_month = calendar_month or range_start.strftime('%Y-%m')
            try:
                y, m = [int(x) for x in base_month.split('-')]
                first_day = date(y, m, 1)
                last_day = date(y, m, pycal.monthrange(y, m)[1])
                cal_start, cal_end = first_day, last_day
                calendar_month = base_month
            except Exception:
                calendar_mode = 'range'
                calendar_month = range_start.strftime('%Y-%m')
        else:
            calendar_mode = 'range'
            calendar_month = range_start.strftime('%Y-%m')

        cal_label = f"{cal_start.isoformat()} to {cal_end.isoformat()}"

        workers = Worker.query.filter_by(active_status=True).order_by(Worker.name).all()
        worker_ids = [w.id for w in workers]
        totals['workers'] = len(workers)

        start_dt = datetime.combine(range_start, datetime.min.time())
        end_dt = datetime.combine(range_end, datetime.max.time())
        cal_start_dt = datetime.combine(cal_start, datetime.min.time())
        cal_end_dt = datetime.combine(cal_end, datetime.max.time())

        entries_period = (TimeEntry.query
                          .filter(TimeEntry.worker_id.in_(worker_ids),
                                  TimeEntry.is_void == False,
                                  TimeEntry.check_in >= start_dt,
                                  TimeEntry.check_in <= end_dt)
                          .order_by(TimeEntry.check_in.asc())
                          .all()) if worker_ids else []

        entries_calendar = (TimeEntry.query
                            .filter(TimeEntry.worker_id.in_(worker_ids),
                                    TimeEntry.is_void == False,
                                    TimeEntry.check_in >= cal_start_dt,
                                    TimeEntry.check_in <= cal_end_dt)
                            .order_by(TimeEntry.check_in.asc())
                            .all()) if worker_ids else []

        first_entry_raw = (db.session.query(TimeEntry.worker_id, func.min(func.date(TimeEntry.check_in)))
                           .filter(TimeEntry.worker_id.in_(worker_ids), TimeEntry.is_void == False)
                           .group_by(TimeEntry.worker_id)
                           .all()) if worker_ids else []
        first_entry_date_by_worker = {}
        for wid, dval in first_entry_raw:
            try:
                first_entry_date_by_worker[wid] = datetime.strptime(str(dval), '%Y-%m-%d').date()
            except Exception:
                pass

        worked_dates_period = {}
        work_entries_count = {}
        overtime_total = {}
        gross_total = {}
        for t in entries_period:
            wset = worked_dates_period.setdefault(t.worker_id, set())
            wset.add(t.check_in.date())
            work_entries_count[t.worker_id] = work_entries_count.get(t.worker_id, 0) + 1
            overtime_total[t.worker_id] = overtime_total.get(t.worker_id, 0.0) + float(t.overtime or 0.0)
            gross_total[t.worker_id] = gross_total.get(t.worker_id, 0.0) + float(t.wage_calculated or 0.0)

        worked_dates_calendar = {}
        for t in entries_calendar:
            worked_dates_calendar.setdefault(t.worker_id, set()).add(t.check_in.date())

        # Calendar day list
        d = cal_start
        while d <= cal_end:
            calendar_days.append(d)
            d += timedelta(days=1)

        # Day-wise work / absent / not assigned summary
        for d in calendar_days:
            work_cnt = 0
            absent_cnt = 0
            not_assigned_cnt = 0
            for w in workers:
                w_dates = worked_dates_calendar.get(w.id, set())
                if d in w_dates:
                    work_cnt += 1
                else:
                    first_d = first_entry_date_by_worker.get(w.id)
                    if first_d and first_d < d:
                        absent_cnt += 1
                    else:
                        not_assigned_cnt += 1
            day_status_rows.append({
                'date': d,
                'work_entries': work_cnt,
                'absent_entries': absent_cnt,
                'not_assigned_entries': not_assigned_cnt
            })
            totals['work_days'] += work_cnt
            totals['absent_days'] += absent_cnt
            totals['not_assigned_days'] += not_assigned_cnt

        worker_map = {w.id: w for w in workers}
        for wid, w in worker_map.items():
            w_days = worked_dates_period.get(wid, set())
            worked_day_count = len(w_days)
            gross = float(gross_total.get(wid, 0.0))
            ot = float(overtime_total.get(wid, 0.0))
            per_day_wage = (gross / worked_day_count) if worked_day_count > 0 else 0.0
            advances = sum(float(l.amount or 0.0) for l in LabourLedger.query
                           .filter_by(worker_id=wid, entry_type='advance', is_void=False)
                           .filter(LabourLedger.date >= range_start, LabourLedger.date <= range_end)
                           .all())
            payable = max(0.0, gross - advances)
            run_note_key = f'Payroll run #{selected_run.id} '
            paid = sum(float(l.amount or 0.0) for l in LabourLedger.query
                       .filter_by(worker_id=wid, entry_type='payment', is_void=False)
                       .filter(LabourLedger.notes.ilike(f'%{run_note_key}%'))
                       .all())
            balance = max(0.0, payable - paid)
            status = 'Not Paid' if (payable > 0 and balance > 0.01) else 'Paid'

            absent_entries = 0
            not_assigned_entries = 0
            first_d = first_entry_date_by_worker.get(wid)
            for d in calendar_days:
                if d in worked_dates_calendar.get(wid, set()):
                    continue
                if first_d and first_d < d:
                    absent_entries += 1
                else:
                    not_assigned_entries += 1

            payroll_summary_rows.append({
                'worker': w,
                'work_entries': int(work_entries_count.get(wid, 0)),
                'worked_days': worked_day_count,
                'per_day_wage': per_day_wage,
                'overtime_total': ot,
                'total_wage': gross,
                'advance': advances,
                'payable': payable,
                'paid': paid,
                'balance': balance,
                'payment_status': status,
                'absent_entries': absent_entries,
                'not_assigned_entries': not_assigned_entries
            })

            row_statuses = []
            w_dates = worked_dates_calendar.get(wid, set())
            for d in calendar_days:
                if d in w_dates:
                    code = 'W'
                else:
                    if first_d and first_d < d:
                        code = 'A'
                    else:
                        code = 'N'
                row_statuses.append({'date': d, 'code': code})
            worker_calendar_rows.append({'worker': w, 'statuses': row_statuses})

            totals['work_entries'] += int(work_entries_count.get(wid, 0))
            totals['total_wage'] += gross
            totals['total_advance'] += advances
            totals['total_payable'] += payable
            totals['total_paid'] += paid
            totals['total_balance'] += balance

        payroll_summary_rows.sort(key=lambda r: (r['worker'].name or '').lower())
        worker_calendar_rows.sort(key=lambda r: (r['worker'].name or '').lower())

    return render_template(
        'payroll_generate.html',
        runs=runs,
        selected_run=selected_run,
        items=items,
        payroll_summary_rows=payroll_summary_rows,
        calendar_days=calendar_days,
        day_status_rows=day_status_rows,
        worker_calendar_rows=worker_calendar_rows,
        calendar_mode=calendar_mode,
        calendar_month=calendar_month,
        cal_label=cal_label,
        totals=totals,
        today=_pkt_today().isoformat()
    )

@app.route('/hdc/payroll/<int:run_id>/delete', methods=['POST'])
@login_required
def hdc_payroll_delete(run_id):
    run = PayrollRun.query.get_or_404(run_id)
    items = PayrollItem.query.filter_by(run_id=run.id).all()
    worker_ids = [it.worker_id for it in items if it.worker_id]
    deleted_ledgers = 0

    # Remove payroll payment entries linked with run-id note pattern.
    if worker_ids:
        note_key = f'Payroll run #{run.id} '
        ledgers = (LabourLedger.query
                   .filter(LabourLedger.worker_id.in_(worker_ids),
                           LabourLedger.entry_type == 'payment',
                           LabourLedger.is_void == False,
                           LabourLedger.notes.ilike(f'%{note_key}%'))
                   .all())
        for l in ledgers:
            db.session.delete(l)
            deleted_ledgers += 1

    db.session.delete(run)
    db.session.commit()

    if deleted_ledgers > 0:
        flash(f'Payroll run #{run.id} deleted with {deleted_ledgers} linked payment entries.', 'success')
    else:
        flash(f'Payroll run #{run.id} deleted. (No linked payment entries found for auto-cleanup.)', 'warning')
    return redirect(url_for('hdc_payroll_salary_cards_page'))

@app.route('/hdc/payroll/salary-cards')
@login_required
def hdc_payroll_salary_cards_page():
    run_id = request.args.get('run_id', type=int)
    worker_id = request.args.get('worker_id', type=int)
    runs = PayrollRun.query.order_by(PayrollRun.run_date.desc()).all()
    selected_run = PayrollRun.query.get(run_id) if run_id else None
    workers = Worker.query.order_by(Worker.name).all()
    salary_rows = []
    if selected_run:
        item_by_worker = {it.worker_id: it for it in PayrollItem.query.filter_by(run_id=selected_run.id).all()}
        for w in workers:
            if worker_id and worker_id != w.id:
                continue
            it = item_by_worker.get(w.id)
            salary_rows.append({
                'worker': w,
                'net_pay': float(it.net_pay or 0.0) if it else 0.0
            })
    return render_template(
        'payroll_salary_cards.html',
        runs=runs,
        selected_run=selected_run,
        salary_rows=salary_rows,
        workers=workers,
        filter_worker_id=worker_id
    )

@app.route('/hdc/payroll/history')
@login_required
def hdc_payroll_history():
    runs = PayrollRun.query.order_by(PayrollRun.run_date.desc()).all()
    return render_template('payroll_history.html', runs=runs)

@app.route('/hdc/payroll/<int:run_id>/salary-cards')
@login_required
def hdc_payroll_salary_cards(run_id):
    run = PayrollRun.query.get_or_404(run_id)
    worker_id = request.args.get('worker_id', type=int)
    auto_print = request.args.get('autoprint', '1')

    start_dt = datetime.combine(run.date_from, datetime.min.time())
    end_dt = datetime.combine(run.date_to, datetime.max.time())
    label_period = f"{run.date_from.isoformat()} to {run.date_to.isoformat()}"

    cards = []
    total_workers = 0
    totals = {
        'work_entries': 0,
        'worked_days': 0,
        'overtime_total': 0.0,
        'total_wage': 0.0,
        'advance': 0.0,
        'payable': 0.0,
        'paid': 0.0,
        'balance': 0.0
    }

    workers_q = Worker.query
    if worker_id:
        workers_q = workers_q.filter(Worker.id == worker_id)
    workers = workers_q.order_by(Worker.name).all()

    for w in workers:
        total_workers += 1

        entry_rows = TimeEntry.query.filter(
            TimeEntry.worker_id == w.id,
            TimeEntry.is_void == False,
            TimeEntry.check_in >= start_dt,
            TimeEntry.check_in <= end_dt
        ).all()
        work_entries = len(entry_rows)
        worked_days = len({e.check_in.date() for e in entry_rows})
        overtime_total = sum(float(e.overtime or 0.0) for e in entry_rows)
        total_wage = sum(float(e.wage_calculated or 0.0) for e in entry_rows)
        advance = sum(float(l.amount or 0.0) for l in LabourLedger.query.filter(
            LabourLedger.worker_id == w.id,
            LabourLedger.entry_type == 'advance',
            LabourLedger.is_void == False,
            LabourLedger.date >= run.date_from,
            LabourLedger.date <= run.date_to
        ).all())
        payable = max(0.0, total_wage - advance)

        run_note_key = f'Payroll run #{run.id} '
        paid = sum(float(l.amount or 0.0) for l in LabourLedger.query.filter(
            LabourLedger.worker_id == w.id,
            LabourLedger.entry_type == 'payment',
            LabourLedger.is_void == False,
            LabourLedger.notes.ilike(f"%{run_note_key}%")
        ).all())
        balance = max(0.0, payable - paid)
        per_day_wage = (total_wage / worked_days) if worked_days > 0 else 0.0
        status = 'Paid' if balance <= 0.01 else 'Not Paid'

        cards.append({
            'worker': w,
            'work_entries': work_entries,
            'worked_days': worked_days,
            'per_day_wage': per_day_wage,
            'overtime_total': overtime_total,
            'total_wage': total_wage,
            'advance': advance,
            'payable': payable,
            'paid': paid,
            'balance': balance,
            'status': status
        })

        totals['work_entries'] += work_entries
        totals['worked_days'] += worked_days
        totals['overtime_total'] += overtime_total
        totals['total_wage'] += total_wage
        totals['advance'] += advance
        totals['payable'] += payable
        totals['paid'] += paid
        totals['balance'] += balance

    cards.sort(key=lambda c: (c['worker'].name or '').lower())
    title = 'Salary Card' if worker_id else 'Salary Cards Report'
    return render_template(
        'payroll_salary_cards_print.html',
        run=run,
        cards=cards,
        title=title,
        total_workers=total_workers,
        totals=totals,
        label_period=label_period,
        now=_pkt_now().strftime('%Y-%m-%d %H:%M'),
        auto_print=(str(auto_print).strip() != '0')
    )


# Ã¢â€â‚¬Ã¢â€â‚¬ Alerts Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
@app.route('/hdc/alerts')
@login_required
def hdc_alerts():
    _refresh_alerts()
    alerts = Alert.query.order_by(Alert.created_at.desc()).all()
    return render_template('alerts.html', alerts=alerts)

@app.route('/hdc/alerts/<int:aid>/resolve', methods=['POST'])
@login_required
def hdc_alerts_resolve(aid):
    a = Alert.query.get_or_404(aid)
    a.resolved = True
    db.session.commit()
    flash('Alert resolved.', 'success')
    return redirect(url_for('hdc_alerts'))


# â”€â”€ Expenses â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/expenses', methods=['GET', 'POST'])
@login_required
def hdc_expenses():
    if request.method == 'POST':
        pid = request.form.get('project_id', type=int)
        if not pid:
            flash('Project is required.', 'danger')
            return redirect(url_for('hdc_expenses'))
        sid = request.form.get('stage_id', type=int)
        if not sid:
            flash('Stage is required.', 'danger')
            return redirect(url_for('hdc_expenses'))
        stage = Stage.query.get(sid)
        if (not stage) or (stage.project_id != pid):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_expenses'))
        category_id = request.form.get('category_id', type=int)
        if not category_id:
            flash('Expense category is required.', 'danger')
            return redirect(url_for('hdc_expenses'))
        valid_category = _ensure_expense_category_by_id(category_id)
        if not valid_category:
            flash('Please select a valid active expense category.', 'danger')
            return redirect(url_for('hdc_expenses'))
        exp_date = _parse_date(request.form.get('date'))
        amount = _flt(request.form.get('amount'))
        if amount <= 0:
            flash('Expense amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_expenses'))
        remarks = (request.form.get('remarks','') or '').strip()
        if _has_recent_duplicate(
            Expense,
            project_id=pid,
            stage_id=sid,
            category_id=category_id,
            amount=amount,
            date=exp_date,
            remarks=remarks
        ):
            flash('Duplicate expense prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_expenses'))
        exp = Expense(
            project_id=pid, stage_id=sid,
            category_id=category_id,
            amount=amount,
            date=exp_date,
            activity_at=_activity_at_for(exp_date),
            remarks=remarks,
            is_void=False
        )
        db.session.add(exp)
        db.session.flush()
        ok_txn, msg_txn, _ = _accounts_post_expense_row(exp, commit=False)
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to post expense in unified accounts.', 'danger')
            return redirect(url_for('hdc_expenses'))
        log_action(
            current_user,
            'create',
            f'{current_user.username.title()} created expense: {valid_category.name}, {amount:,.2f} PKR, {stage.project.name} -> {stage.name} on {exp_date.isoformat()}',
            'expense',
            exp.id or ''
        )
        db.session.commit()
        flash('Expense added.', 'success')
        return redirect(url_for('hdc_expenses'))

    projects    = Project.query.all()
    project_id  = request.args.get('project_id', type=int)
    q = (db.session.query(Expense, Project, Stage)
         .join(Project, Expense.project_id == Project.id)
         .outerjoin(Stage, Expense.stage_id == Stage.id))
    q = q.filter(Expense.is_void == False)
    if project_id: q = q.filter(Expense.project_id == project_id)
    records = q.order_by(Expense.activity_at.desc(), Expense.date.desc()).all()
    total   = sum((r[0].amount or 0.0) for r in records)
    categories = ExpenseCategory.query.filter_by(active_status=True).order_by(ExpenseCategory.name.asc()).all()
    return render_template('expenses.html',
        projects=projects, records=records, total=total,
        selected_project=project_id, today=_pkt_today().isoformat(),
        categories=categories)

@app.route('/hdc/expenses/<int:eid>/edit', methods=['GET', 'POST'])
@login_required
def hdc_expense_edit(eid):
    exp = Expense.query.get_or_404(eid)
    if _is_linked_system_expense(exp):
        flash('Linked Tip/Settlement expense cannot be edited here. Please update the original payment/settlement entry.', 'warning')
        return redirect(url_for('hdc_expenses', project_id=exp.project_id))

    projects = Project.query.all()
    categories = ExpenseCategory.query.filter_by(active_status=True).order_by(ExpenseCategory.name.asc()).all()
    if request.method == 'POST':
        pid = request.form.get('project_id', type=int)
        sid = request.form.get('stage_id', type=int)
        if not pid:
            flash('Project is required.', 'danger')
            return redirect(url_for('hdc_expense_edit', eid=eid))
        if not sid:
            flash('Stage is required.', 'danger')
            return redirect(url_for('hdc_expense_edit', eid=eid))
        stage = Stage.query.get(sid)
        if (not stage) or (stage.project_id != pid):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_expense_edit', eid=eid))
        category_id = request.form.get('category_id', type=int)
        if not category_id:
            flash('Expense category is required.', 'danger')
            return redirect(url_for('hdc_expense_edit', eid=eid))
        valid_category = _ensure_expense_category_by_id(category_id)
        if not valid_category:
            flash('Please select a valid active expense category.', 'danger')
            return redirect(url_for('hdc_expense_edit', eid=eid))
        amount = _flt(request.form.get('amount'))
        if amount <= 0:
            flash('Expense amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_expense_edit', eid=eid))
        exp_date = _parse_date(request.form.get('date'))
        old_project = Project.query.get(exp.project_id)
        old_stage = Stage.query.get(exp.stage_id) if exp.stage_id else None
        old_cat = _category_name_from_id(exp.category_id)
        old_amount = float(exp.amount or 0.0)
        exp.project_id = pid
        exp.stage_id = sid
        exp.category_id = category_id
        exp.amount = amount
        exp.date = exp_date
        exp.activity_at = _activity_at_for(exp_date)
        exp.remarks = (request.form.get('remarks', '') or '').strip()
        log_action(
            current_user,
            'update',
            (
                f'{current_user.username.title()} updated expense: '
                f'Category {old_cat} -> {valid_category.name}; '
                f'Amount {old_amount:,.2f} -> {amount:,.2f} PKR; '
                f'Project/Stage {(old_project.name if old_project else "-")} -> {(old_stage.name if old_stage else "-")} '
                f'to {stage.project.name} -> {stage.name}'
            ),
            'expense',
            exp.id
        )
        db.session.commit()
        flash('Expense updated.', 'success')
        return redirect(url_for('hdc_expenses', project_id=pid))

    return render_template(
        'expense_edit.html',
        exp=exp,
        projects=projects,
        categories=categories,
        today=(exp.date or _pkt_today()).isoformat()
    )

@app.route('/hdc/expenses/<int:eid>/delete', methods=['POST'])
@login_required
def hdc_expense_delete(eid):
    exp = Expense.query.get_or_404(eid)
    if _is_linked_system_expense(exp):
        flash('Linked Tip/Settlement expense cannot be deleted here. Please update the original payment/settlement entry.', 'warning')
        return redirect(url_for('hdc_expenses', project_id=exp.project_id))
    pid = exp.project_id
    exp.is_void = True
    exp.void_reason = 'Voided by user'
    exp.voided_at = _pkt_now_naive()
    log_action(
        current_user,
        'void',
        f'{current_user.username.title()} voided expense #{exp.id}: {exp.category or "-"}, {float(exp.amount or 0.0):,.2f} PKR',
        'expense',
        exp.id
    )
    db.session.commit()
    flash('Expense voided.', 'success')
    return redirect(url_for('hdc_expenses', project_id=pid))


# —— Office Management ——————————————————————————————————————————————————————
@app.route('/hdc/office-management')
@login_required
def hdc_office_management():
    staff_count = int(OfficeStaff.query.count() or 0)
    active_staff_count = int(OfficeStaff.query.filter(OfficeStaff.active_status == True).count() or 0)
    office_expense_total = _office_expense_total()
    office_expense_month = float(
        db.session.query(func.coalesce(func.sum(OfficeExpense.amount), 0.0))
        .filter(
            OfficeExpense.is_void == False,
            func.strftime('%Y-%m', OfficeExpense.date) == _pkt_today().strftime('%Y-%m')
        )
        .scalar() or 0.0
    )
    allowance_categories_count = int(AllowanceCategory.query.filter(AllowanceCategory.is_active == True).count() or 0)
    return render_template(
        'office_management.html',
        staff_count=staff_count,
        active_staff_count=active_staff_count,
        office_expense_total=office_expense_total,
        office_expense_month=office_expense_month,
        allowance_categories_count=allowance_categories_count
    )

@app.route('/hdc/office-management/staff')
@login_required
def hdc_office_staff_home():
    staff_count = int(OfficeStaff.query.count() or 0)
    attendance_days = int(
        db.session.query(func.count(OfficeStaffAttendance.id))
        .filter(OfficeStaffAttendance.date == _pkt_today())
        .scalar() or 0
    )
    ledger_entries = int(db.session.query(func.count(OfficeStaffLedger.id)).scalar() or 0)
    return render_template(
        'office_staff_home.html',
        staff_count=staff_count,
        attendance_days=attendance_days,
        ledger_entries=ledger_entries
    )

@app.route('/hdc/office-management/staff/ledger', methods=['GET', 'POST'])
@login_required
def hdc_office_staff_ledger_list():
    if request.method == 'POST':
        code = (request.form.get('staff_code') or '').strip() or _next_office_staff_code()
        name = (request.form.get('name') or '').strip()
        role_type = (request.form.get('role_type') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        monthly_salary = _flt(request.form.get('monthly_salary'))
        if not name:
            flash('Staff name is required.', 'warning')
            return redirect(url_for('hdc_office_staff_ledger_list'))
        if OfficeStaff.query.filter(OfficeStaff.staff_code == code).first():
            flash('Staff code already exists.', 'danger')
            return redirect(url_for('hdc_office_staff_ledger_list'))
        row = OfficeStaff(
            staff_code=code,
            name=name,
            role_type=role_type,
            phone=phone,
            monthly_salary=monthly_salary,
            active_status=True
        )
        db.session.add(row)
        db.session.commit()
        flash(f'Office staff "{row.name}" added.', 'success')
        return redirect(url_for('hdc_office_staff_ledger_list'))

    staff_rows = OfficeStaff.query.order_by(OfficeStaff.created_at.desc(), OfficeStaff.id.desc()).all()
    snapshots = {int(s.id): _office_staff_ledger_snapshot(s.id) for s in staff_rows}
    return render_template(
        'office_staff_ledger_list.html',
        staff_rows=staff_rows,
        snapshots=snapshots,
        next_staff_code=_next_office_staff_code()
    )

@app.route('/hdc/office-management/staff/<int:sid>/edit', methods=['POST'])
@login_required
def hdc_office_staff_edit(sid):
    row = OfficeStaff.query.get_or_404(sid)
    code = (request.form.get('staff_code') or '').strip()
    name = (request.form.get('name') or '').strip()
    if not code or not name:
        flash('Staff code and name are required.', 'warning')
        return redirect(url_for('hdc_office_staff_ledger_list'))
    exists = OfficeStaff.query.filter(OfficeStaff.staff_code == code, OfficeStaff.id != sid).first()
    if exists:
        flash('Staff code already exists for another member.', 'danger')
        return redirect(url_for('hdc_office_staff_ledger_list'))
    row.staff_code = code
    row.name = name
    row.role_type = (request.form.get('role_type') or '').strip()
    row.phone = (request.form.get('phone') or '').strip()
    row.monthly_salary = _flt(request.form.get('monthly_salary'))
    row.active_status = (request.form.get('active_status') or 'active').strip().lower() == 'active'
    db.session.commit()
    flash(f'Office staff "{row.name}" updated.', 'success')
    return redirect(url_for('hdc_office_staff_ledger_list'))

@app.route('/hdc/office-management/staff/<int:sid>/toggle', methods=['POST'])
@login_required
def hdc_office_staff_toggle(sid):
    row = OfficeStaff.query.get_or_404(sid)
    row.active_status = not bool(row.active_status)
    db.session.commit()
    flash(f'Office staff "{row.name}" {"activated" if row.active_status else "suspended"}.', 'success')
    return redirect(url_for('hdc_office_staff_ledger_list'))

@app.route('/hdc/office-management/staff/<int:sid>/ledger', methods=['GET', 'POST'])
@login_required
def hdc_office_staff_ledger(sid):
    row = OfficeStaff.query.get_or_404(sid)
    if request.method == 'POST':
        entry_date = _parse_date(request.form.get('date'))
        entry_type = (request.form.get('entry_type') or 'advance').strip().lower()
        amount = _flt(request.form.get('amount'))
        notes = (request.form.get('notes') or '').strip()
        if entry_type not in ('advance', 'payment', 'settlement'):
            flash('Invalid ledger entry type.', 'danger')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        if amount <= 0:
            flash('Amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        if _has_recent_duplicate(
            OfficeStaffLedger,
            staff_id=sid,
            entry_type=entry_type,
            amount=amount,
            date=entry_date,
            notes=notes
        ):
            flash('Duplicate ledger entry prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        row_entry = OfficeStaffLedger(
            staff_id=sid,
            date=entry_date,
            entry_type=entry_type,
            amount=amount,
            notes=notes,
            activity_at=_activity_at_for(entry_date)
        )
        db.session.add(row_entry)
        db.session.flush()
        if entry_type in ('advance', 'payment'):
            _sync_office_staff_expense_from_ledger(row, row_entry)
            ok_txn, msg_txn, _ = _accounts_upsert_office_staff_ledger_txn(row, row_entry, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to post office staff ledger in unified accounts.', 'danger')
                return redirect(url_for('hdc_office_staff_ledger', sid=sid))
        db.session.commit()
        flash('Office staff ledger entry recorded.', 'success')
        return redirect(url_for('hdc_office_staff_ledger', sid=sid))

    ledger_entries = (OfficeStaffLedger.query
                      .filter(OfficeStaffLedger.staff_id == sid)
                      .order_by(OfficeStaffLedger.activity_at.asc(), OfficeStaffLedger.id.asc())
                      .all())
    snap = _office_staff_ledger_snapshot(sid)
    return render_template(
        'office_staff_ledger.html',
        staff=row,
        ledger_entries=ledger_entries,
        snap=snap,
        today=_pkt_today().isoformat()
    )

@app.route('/hdc/office-management/staff/<int:sid>/payment', methods=['GET', 'POST'])
@login_required
def hdc_office_staff_payment(sid):
    staff_row = OfficeStaff.query.get_or_404(sid)
    snap = _office_staff_ledger_snapshot(sid)
    if request.method == 'POST':
        entry_date = _parse_date(request.form.get('date'))
        amount = _flt(request.form.get('amount'))
        notes = (request.form.get('notes') or '').strip()
        if amount <= 0:
            flash('Payment amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_office_staff_payment', sid=sid))
        if _has_recent_duplicate(
            OfficeStaffLedger,
            staff_id=sid,
            entry_type='payment',
            amount=amount,
            date=entry_date,
            notes=notes
        ):
            flash('Duplicate payment prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_office_staff_payment', sid=sid))
        row_entry = OfficeStaffLedger(
            staff_id=sid,
            date=entry_date,
            entry_type='payment',
            amount=amount,
            notes=notes,
            activity_at=_activity_at_for(entry_date)
        )
        db.session.add(row_entry)
        db.session.flush()
        _sync_office_staff_expense_from_ledger(staff_row, row_entry)
        ok_txn, msg_txn, _ = _accounts_upsert_office_staff_ledger_txn(staff_row, row_entry, commit=False)
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to post office salary in unified accounts.', 'danger')
            return redirect(url_for('hdc_office_staff_payment', sid=sid))
        db.session.commit()
        flash(f'Salary payment of {amount:,.0f} PKR recorded for {staff_row.name} and posted to Office Expenses.', 'success')
        if row_entry and row_entry.id:
            return redirect(url_for('hdc_office_staff_payment_receipt', sid=sid, lid=row_entry.id))
        return redirect(url_for('hdc_office_staff_ledger', sid=sid))
    return render_template(
        'office_staff_payment.html',
        staff=staff_row,
        snap=snap,
        today=_pkt_today().isoformat()
    )

@app.route('/hdc/office-management/staff/<int:sid>/payment/<int:lid>/receipt')
@login_required
def hdc_office_staff_payment_receipt(sid, lid):
    staff_row = OfficeStaff.query.get_or_404(sid)
    row = OfficeStaffLedger.query.get_or_404(lid)
    if row.staff_id != sid:
        abort(404)
    receipt_id = f'RCPT-OS-{row.id:08d}'
    recent = (
        OfficeStaffLedger.query
        .filter(OfficeStaffLedger.staff_id == sid, OfficeStaffLedger.id != row.id, OfficeStaffLedger.is_void == False)
        .order_by(OfficeStaffLedger.activity_at.desc(), OfficeStaffLedger.id.desc())
        .limit(5).all()
    )
    recent_entries = [{
        'date': (r.date.strftime('%Y-%m-%d') if r.date else '-'),
        'type': (r.entry_type or 'payment').title(),
        'direction': 'pay',
        'party': staff_row.name,
        'amount': float(r.amount or 0),
        'receipt_url': url_for('hdc_office_staff_payment_receipt', sid=sid, lid=r.id)
    } for r in recent]
    return render_template(
        'transaction_receipt.html',
        company_profile=_receipt_company_profile(),
        receipt_id=receipt_id,
        created_at=(row.activity_at or _pkt_now_naive()),
        tx_type=f'Office Staff {(row.entry_type or "Payment").title()} Receipt',
        party_name=staff_row.name,
        project_name='-',
        stage_name='-',
        account_used='-',
        amount=float(row.amount or 0),
        amount_words=_amount_to_words(row.amount or 0),
        note=(row.notes or ''),
        reference_id=f'office_staff_ledger#{row.id}',
        recent_entries=recent_entries,
        recent_entries_title=f'Last 5 Ledger Entries – {staff_row.name}',
        back_url=url_for('hdc_office_staff_ledger', sid=sid),
        print_label='Print / Save PDF'
    )


@app.route('/hdc/office-management/staff/<int:sid>/ledger/<int:lid>/edit', methods=['GET', 'POST'])
@login_required
def hdc_office_staff_ledger_edit(sid, lid):
    row = OfficeStaff.query.get_or_404(sid)
    entry = OfficeStaffLedger.query.get_or_404(lid)
    if entry.staff_id != sid:
        flash('Ledger entry does not belong to selected staff.', 'danger')
        return redirect(url_for('hdc_office_staff_ledger', sid=sid))
    if entry.is_void:
        flash('Voided entry cannot be edited.', 'warning')
        return redirect(url_for('hdc_office_staff_ledger', sid=sid))
    if entry.entry_type not in ('advance', 'payment', 'tip', 'settlement'):
        flash('Only advance/payment/tip/settlement entries are editable.', 'warning')
        return redirect(url_for('hdc_office_staff_ledger', sid=sid))
    if request.method == 'POST':
        entry_date = _parse_date(request.form.get('date'))
        amount = _flt(request.form.get('amount'))
        if amount <= 0:
            flash('Amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_office_staff_ledger_edit', sid=sid, lid=lid))
        entry.date = entry_date
        entry.amount = amount
        entry.notes = (request.form.get('notes') or '').strip()
        entry.activity_at = _activity_at_for(entry_date)
        if entry.entry_type in ('advance', 'payment', 'tip'):
            _sync_office_staff_expense_from_ledger(row, entry)
            ok_txn, msg_txn, _ = _accounts_upsert_office_staff_ledger_txn(row, entry, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to sync office staff ledger in unified accounts.', 'danger')
                return redirect(url_for('hdc_office_staff_ledger_edit', sid=sid, lid=lid))
        db.session.commit()
        flash('Ledger entry updated.', 'success')
        return redirect(url_for('hdc_office_staff_ledger', sid=sid))
    return render_template('worker_ledger_entry_edit.html', w=row, entry=entry, projects=Project.query.order_by(Project.name.asc()).all(), stages=Stage.query.order_by(Stage.project_id.asc(), Stage.name.asc()).all())

@app.route('/hdc/office-management/staff/<int:sid>/ledger/<int:lid>/void', methods=['POST'])
@login_required
def hdc_office_staff_ledger_void(sid, lid):
    OfficeStaff.query.get_or_404(sid)
    entry = OfficeStaffLedger.query.get_or_404(lid)
    if entry.staff_id != sid:
        flash('Ledger entry does not belong to selected staff.', 'danger')
        return redirect(url_for('hdc_office_staff_ledger', sid=sid))
    if entry.is_void:
        flash('Ledger entry is already voided.', 'info')
        return redirect(url_for('hdc_office_staff_ledger', sid=sid))
    if entry.entry_type not in ('advance', 'payment', 'tip', 'settlement'):
        flash('Only advance/payment/tip/settlement entries can be voided.', 'warning')
        return redirect(url_for('hdc_office_staff_ledger', sid=sid))
    reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
    entry.is_void = True
    entry.void_reason = reason
    entry.voided_at = _pkt_now_naive()
    if entry.entry_type in ('advance', 'payment', 'tip'):
        _remove_office_salary_expense_for_ledger(entry.id)
        _accounts_set_void_by_source(f'office_staff_ledger_{entry.entry_type}', entry.id, True)
    db.session.commit()
    flash('Ledger entry voided.', 'success')
    return redirect(url_for('hdc_office_staff_ledger', sid=sid))

@app.route('/hdc/office-management/staff/attendance', methods=['GET', 'POST'])
@login_required
def hdc_office_staff_attendance():
    attendance_date = _parse_date(request.values.get('date'), fallback=_pkt_today())
    if request.method == 'POST':
        attendance_date = _parse_date(request.form.get('date'), fallback=_pkt_today())
        staff_rows = OfficeStaff.query.filter(OfficeStaff.active_status == True).order_by(OfficeStaff.name.asc()).all()
        for s in staff_rows:
            status = (request.form.get(f'status_{s.id}') or 'present').strip().lower()
            notes = (request.form.get(f'notes_{s.id}') or '').strip()
            if status not in ('present', 'absent', 'weekly_leave'):
                status = 'present'
            row = OfficeStaffAttendance.query.filter_by(staff_id=s.id, date=attendance_date).first()
            if not row:
                row = OfficeStaffAttendance(staff_id=s.id, date=attendance_date)
                db.session.add(row)
            row.status = status
            row.notes = notes
            row.activity_at = _activity_at_for(attendance_date)
            row.updated_at = _pkt_now_naive()
        db.session.commit()
        flash('Office attendance saved.', 'success')
        return redirect(url_for('hdc_office_staff_attendance', date=attendance_date.isoformat()))

    staff_rows = OfficeStaff.query.order_by(OfficeStaff.name.asc()).all()
    existing_rows = {
        int(r.staff_id): r
        for r in OfficeStaffAttendance.query.filter(OfficeStaffAttendance.date == attendance_date).all()
    }
    recent_records = (db.session.query(OfficeStaffAttendance, OfficeStaff)
                      .join(OfficeStaff, OfficeStaffAttendance.staff_id == OfficeStaff.id)
                      .order_by(OfficeStaffAttendance.activity_at.desc(), OfficeStaffAttendance.id.desc())
                      .limit(120)
                      .all())
    present_count = int(sum(1 for r in existing_rows.values() if (r.status or '').lower() == 'present'))
    absent_count = int(sum(1 for r in existing_rows.values() if (r.status or '').lower() == 'absent'))
    return render_template(
        'office_staff_attendance.html',
        staff_rows=staff_rows,
        existing_rows=existing_rows,
        recent_records=recent_records,
        attendance_date=attendance_date.isoformat(),
        present_count=present_count,
        absent_count=absent_count
    )

@app.route('/hdc/office-management/expenses', methods=['GET', 'POST'])
@login_required
def hdc_office_expenses():
    if request.method == 'POST':
        exp_date = _parse_date(request.form.get('date'))
        category = _normalize_expense_category_name(request.form.get('category'))
        amount = _flt(request.form.get('amount'))
        remarks = (request.form.get('remarks') or '').strip()
        if not category:
            flash('Expense category is required.', 'danger')
            return redirect(url_for('hdc_office_expenses'))
        if amount <= 0:
            flash('Expense amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_office_expenses'))
        if _has_recent_duplicate(
            OfficeExpense,
            date=exp_date,
            category=category,
            amount=amount,
            remarks=remarks,
            is_void=False
        ):
            flash('Duplicate office expense prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_office_expenses'))
        row = OfficeExpense(
            date=exp_date,
            category=category,
            amount=amount,
            remarks=remarks,
            is_void=False,
            activity_at=_activity_at_for(exp_date)
        )
        db.session.add(row)
        db.session.flush()
        ok_txn, msg_txn, _ = _accounts_upsert_office_expense_txn(row, commit=False)
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to post office expense in unified accounts.', 'danger')
            return redirect(url_for('hdc_office_expenses'))
        db.session.commit()
        flash('Office expense added.', 'success')
        return redirect(url_for('hdc_office_expenses'))

    category_filter = _normalize_expense_category_name(request.args.get('category'))
    q = db.session.query(OfficeExpense, OfficeStaff).outerjoin(
        OfficeStaff, OfficeExpense.office_staff_id == OfficeStaff.id
    ).filter(OfficeExpense.is_void == False)
    if category_filter:
        q = q.filter(func.lower(OfficeExpense.category) == category_filter.lower())
    records = q.order_by(OfficeExpense.activity_at.desc(), OfficeExpense.id.desc()).all()
    total = float(sum(float(r[0].amount or 0.0) for r in records))
    categories = _get_all_office_expense_categories()
    return render_template(
        'office_expenses.html',
        records=records,
        total=total,
        categories=categories,
        office_expense_categories=_get_all_office_expense_category_rows(),
        category_filter=category_filter,
        today=_pkt_today().isoformat()
    )

@app.route('/hdc/office-management/expenses/<int:eid>/edit', methods=['GET', 'POST'])
@login_required
def hdc_office_expense_edit(eid):
    row = OfficeExpense.query.get_or_404(eid)
    if row.is_void:
        flash('Office expense is already voided.', 'warning')
        return redirect(url_for('hdc_office_expenses'))
    if _is_linked_office_salary_expense(row):
        flash('Linked salary expense is managed from staff ledger payment. Edit it there.', 'warning')
        return redirect(url_for('hdc_office_expenses'))
    if request.method == 'POST':
        exp_date = _parse_date(request.form.get('date'))
        category = _normalize_expense_category_name(request.form.get('category'))
        amount = _flt(request.form.get('amount'))
        remarks = (request.form.get('remarks') or '').strip()
        if not category:
            flash('Expense category is required.', 'danger')
            return redirect(url_for('hdc_office_expense_edit', eid=eid))
        if amount <= 0:
            flash('Expense amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_office_expense_edit', eid=eid))
        row.date = exp_date
        row.category = category
        row.amount = amount
        row.remarks = remarks
        row.activity_at = _activity_at_for(exp_date)
        ok_txn, msg_txn, _ = _accounts_upsert_office_expense_txn(row, commit=False)
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to sync office expense in unified accounts.', 'danger')
            return redirect(url_for('hdc_office_expense_edit', eid=eid))
        db.session.commit()
        flash('Office expense updated.', 'success')
        return redirect(url_for('hdc_office_expenses'))
    return render_template(
        'office_expense_edit.html',
        row=row,
        today=(row.date or _pkt_today()).isoformat()
    )

@app.route('/hdc/office-management/expenses/<int:eid>/delete', methods=['POST'])
@login_required
def hdc_office_expense_delete(eid):
    row = OfficeExpense.query.get_or_404(eid)
    if row.is_void:
        flash('Office expense is already voided.', 'info')
        return redirect(url_for('hdc_office_expenses'))
    if _is_linked_office_salary_expense(row):
        flash('Linked salary expense is managed from staff ledger payment. Void it there.', 'warning')
        return redirect(url_for('hdc_office_expenses'))
    row.is_void = True
    row.void_reason = 'Voided from Office Expenses'
    row.voided_at = _pkt_now_naive()
    _accounts_set_void_by_source('office_expense', row.id, True)
    db.session.commit()
    flash('Office expense voided.', 'success')
    return redirect(url_for('hdc_office_expenses'))


# â”€â”€ Allowance Categories â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/office-management/allowance-categories', methods=['GET', 'POST'])
@login_required
def hdc_allowance_categories():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        description = (request.form.get('description') or '').strip()
        if not name:
            flash('Allowance category name is required.', 'danger')
            return redirect(url_for('hdc_allowance_categories'))
        if AllowanceCategory.query.filter(func.lower(AllowanceCategory.name) == name.lower()).first():
            flash('Allowance category with this name already exists.', 'danger')
            return redirect(url_for('hdc_allowance_categories'))
        row = AllowanceCategory(name=name, description=description, is_active=True)
        db.session.add(row)
        db.session.commit()
        flash(f'Allowance category "{name}" added.', 'success')
        return redirect(url_for('hdc_allowance_categories'))

    categories = AllowanceCategory.query.order_by(AllowanceCategory.name.asc()).all()
    return render_template('allowance_categories.html', categories=categories)


@app.route('/hdc/office-management/allowance-categories/<int:cid>/edit', methods=['GET', 'POST'])
@login_required
def hdc_allowance_category_edit(cid):
    row = AllowanceCategory.query.get_or_404(cid)
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        description = (request.form.get('description') or '').strip()
        if not name:
            flash('Allowance category name is required.', 'danger')
            return redirect(url_for('hdc_allowance_category_edit', cid=cid))
        existing = AllowanceCategory.query.filter(
            func.lower(AllowanceCategory.name) == name.lower(),
            AllowanceCategory.id != cid
        ).first()
        if existing:
            flash('Another allowance category with this name already exists.', 'danger')
            return redirect(url_for('hdc_allowance_category_edit', cid=cid))
        row.name = name
        row.description = description
        db.session.commit()
        flash(f'Allowance category "{name}" updated.', 'success')
        return redirect(url_for('hdc_allowance_categories'))
    return render_template('allowance_category_edit.html', category=row)


@app.route('/hdc/office-management/allowance-categories/<int:cid>/toggle', methods=['POST'])
@login_required
def hdc_allowance_category_toggle(cid):
    row = AllowanceCategory.query.get_or_404(cid)
    row.is_active = not bool(row.is_active)
    db.session.commit()
    status = 'activated' if row.is_active else 'deactivated'
    flash(f'Allowance category "{row.name}" {status}.', 'success')
    return redirect(url_for('hdc_allowance_categories'))


# â”€â”€ Staff Allowances â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/office-management/staff/<int:sid>/allowances', methods=['GET', 'POST'])
@login_required
def hdc_office_staff_allowances(sid):
    staff = OfficeStaff.query.get_or_404(sid)
    if request.method == 'POST':
        category_id = request.form.get('category_id', type=int)
        amount = _flt(request.form.get('amount'))
        effective_date = _parse_date(request.form.get('effective_date'), fallback=_pkt_today())

        if not category_id:
            flash('Allowance category is required.', 'danger')
            return redirect(url_for('hdc_office_staff_allowances', sid=sid))
        if amount <= 0:
            flash('Allowance amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_office_staff_allowances', sid=sid))

        category = AllowanceCategory.query.get_or_404(category_id)
        if not category.is_active:
            flash('Selected allowance category is not active.', 'danger')
            return redirect(url_for('hdc_office_staff_allowances', sid=sid))

        # Check if this category is already assigned to this staff
        existing = StaffAllowance.query.filter_by(
            staff_id=sid,
            category_id=category_id,
            is_active=True
        ).first()
        if existing:
            flash(f'Allowance category "{category.name}" is already assigned to this staff.', 'warning')
            return redirect(url_for('hdc_office_staff_allowances', sid=sid))

        allowance = StaffAllowance(
            staff_id=sid,
            category_id=category_id,
            amount=amount,
            effective_date=effective_date,
            is_active=True
        )
        db.session.add(allowance)
        db.session.commit()
        flash(f'Allowance "{category.name}" added to {staff.name}.', 'success')
        return redirect(url_for('hdc_office_staff_allowances', sid=sid))

    allowances = (db.session.query(StaffAllowance, AllowanceCategory)
                  .join(AllowanceCategory, StaffAllowance.category_id == AllowanceCategory.id)
                  .filter(StaffAllowance.staff_id == sid, StaffAllowance.is_active == True)
                  .order_by(AllowanceCategory.name.asc())
                  .all())

    available_categories = AllowanceCategory.query.filter(
        AllowanceCategory.is_active == True,
        ~AllowanceCategory.id.in_([a.category_id for a, _ in allowances])
    ).order_by(AllowanceCategory.name.asc()).all()

    total_allowances = sum(a.amount for a, _ in allowances)

    return render_template(
        'office_staff_allowances.html',
        staff=staff,
        allowances=allowances,
        available_categories=available_categories,
        total_allowances=total_allowances,
        today=_pkt_today().isoformat()
    )


@app.route('/hdc/office-management/staff/<int:sid>/allowances/<int:aid>/edit', methods=['GET', 'POST'])
@login_required
def hdc_office_staff_allowance_edit(sid, aid):
    staff = OfficeStaff.query.get_or_404(sid)
    allowance = StaffAllowance.query.get_or_404(aid)
    if allowance.staff_id != sid:
        abort(404)

    if request.method == 'POST':
        amount = _flt(request.form.get('amount'))
        effective_date = _parse_date(request.form.get('effective_date'))

        if amount <= 0:
            flash('Allowance amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_office_staff_allowance_edit', sid=sid, aid=aid))

        allowance.amount = amount
        allowance.effective_date = effective_date
        db.session.commit()
        flash('Allowance updated.', 'success')
        return redirect(url_for('hdc_office_staff_allowances', sid=sid))

    return render_template(
        'office_staff_allowance_edit.html',
        staff=staff,
        allowance=allowance,
        today=allowance.effective_date.isoformat() if allowance.effective_date else _pkt_today().isoformat()
    )


@app.route('/hdc/office-management/staff/<int:sid>/allowances/<int:aid>/remove', methods=['POST'])
@login_required
def hdc_office_staff_allowance_remove(sid, aid):
    staff = OfficeStaff.query.get_or_404(sid)
    allowance = StaffAllowance.query.get_or_404(aid)
    if allowance.staff_id != sid:
        abort(404)

    allowance.is_active = False
    db.session.commit()
    flash(f'Allowance removed from {staff.name}.', 'success')
    return redirect(url_for('hdc_office_staff_allowances', sid=sid))


# â”€â”€ Materials â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/materials', methods=['GET', 'POST'])
@login_required
def hdc_materials():
    flash('Materials module has moved to Purchase V2.', 'info')
    return redirect(url_for('hdc_purchase_v2_materials'))
    if request.method == 'POST':
        action = request.form.get('action','add')
        if action == 'add':
            name = request.form.get('name','').strip()
            unit = request.form.get('unit','Nos').strip()
            if not name:
                flash('Name is required.', 'danger')
            else:
                db.session.add(Material(name=name, unit=unit))
                db.session.commit()
                flash(f'Material "{name}" added.', 'success')
        elif action == 'toggle':
            m = Material.query.get(request.form.get('mid', type=int))
            if m:
                m.is_active = not m.is_active
                db.session.commit()
        return redirect(url_for('hdc_materials'))
    materials = Material.query.order_by(Material.name).all()
    stock_map = _material_stock_map()
    return render_template('materials.html', materials=materials, stock_map=stock_map)

@app.route('/hdc/materials/usage', methods=['GET', 'POST'])
@login_required
def hdc_material_usage():
    flash('Material Usage has moved to Purchase V2 Usage.', 'info')
    return redirect(url_for('hdc_purchase_v2_usage_page'))
    if request.method == 'POST':
        pid = request.form.get('project_id', type=int)
        sid = request.form.get('stage_id', type=int)
        mid = request.form.get('material_id', type=int)
        qty = _flt(request.form.get('qty'))
        if not pid or not sid or not mid:
            flash('Project, stage, and material are required.', 'danger')
            return redirect(url_for('hdc_material_usage'))
        if qty <= 0:
            flash('Usage quantity must be greater than 0.', 'danger')
            return redirect(url_for('hdc_material_usage'))

        p = Project.query.get(pid)
        m = Material.query.get(mid)
        if not p or not m:
            flash('Invalid project or material selected.', 'danger')
            return redirect(url_for('hdc_material_usage'))

        s = Stage.query.get(sid)
        if (not s) or (s.project_id != pid):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_material_usage'))

        available = _material_stock_for_scope(mid, project_id=pid, stage_id=sid)
        if qty > available:
            flash(f'Insufficient stock. Available: {available:,.2f} {m.unit}.', 'danger')
            return redirect(url_for('hdc_material_usage'))

        rate = 0.0
        total = 0.0
        used_date = _parse_date(request.form.get('used_at'))
        if _has_recent_duplicate(
            MaterialUsage,
            project_id=pid,
            stage_id=sid,
            material_id=mid,
            qty=qty,
            rate=rate,
            total=total,
            used_at=used_date
        ):
            flash('Duplicate material usage prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_material_usage'))
        db.session.add(MaterialUsage(
            project_id=pid, stage_id=sid, material_id=mid,
            qty=qty, rate=rate, total=total,
            used_at=used_date,
            activity_at=_activity_at_for(used_date)))
        db.session.commit()
        new_balance = available - qty
        flash(f'Material usage recorded. Remaining {m.name}: {new_balance:,.2f} {m.unit}.', 'success')
        return redirect(url_for('hdc_material_usage'))
    projects = Project.query.all()
    stages = Stage.query.all()
    materials = Material.query.filter_by(is_active=True).all()
    records = MaterialUsage.query.order_by(MaterialUsage.activity_at.desc(), MaterialUsage.used_at.desc()).all()
    stock_map = _material_stock_map()
    return render_template('material_usage.html',
        projects=projects, stages=stages, materials=materials, records=records,
        stock_map=stock_map, today=_pkt_today().isoformat())

@app.route('/hdc/purchases', methods=['GET', 'POST'])
@login_required
def hdc_purchases():
    flash('Legacy Purchases has moved to Purchase V2 Purchase Orders.', 'info')
    return redirect(url_for('hdc_purchase_v2_purchases'))
    if request.method == 'POST':
        action = (request.form.get('action') or 'purchase').strip().lower()
        if action not in ('purchase', 'return'):
            action = 'purchase'
        qty  = _flt(request.form.get('qty'))
        rate = _flt(request.form.get('rate'))
        purchase_date = _parse_date(request.form.get('date'))
        if qty <= 0:
            flash(('Return quantity' if action == 'return' else 'Purchase quantity') + ' must be greater than zero.', 'danger')
            return redirect(url_for('hdc_purchases'))
        if rate <= 0:
            flash(('Return rate' if action == 'return' else 'Purchase rate') + ' must be greater than zero.', 'danger')
            return redirect(url_for('hdc_purchases'))
        pid = request.form.get('project_id', type=int)
        sid = request.form.get('stage_id', type=int)
        mid = request.form.get('material_id', type=int)
        if not pid or not sid or not mid:
            flash('Project, stage, and material are required.', 'danger')
            return redirect(url_for('hdc_purchases'))
        stage = Stage.query.get(sid)
        if (not stage) or (stage.project_id != pid):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_purchases'))
        notes = (request.form.get('notes','') or '').strip()
        supplier_name = (request.form.get('supplier_name','') or '').strip()
        return_ref = (request.form.get('return_ref','') or '').strip()
        return_reason = (request.form.get('return_reason','') or '').strip()
        approved_by = (request.form.get('approved_by','') or '').strip()
        if action == 'return':
            m = Material.query.get(mid)
            available = _material_stock_for_scope(mid, project_id=pid, stage_id=sid)
            if qty > available + 1e-6:
                unit = m.unit if m else 'unit'
                flash(f'Cannot return more than available stock. Available: {available:,.2f} {unit}.', 'danger')
                return redirect(url_for('hdc_purchases'))
        signed_qty = -qty if action == 'return' else qty
        total = signed_qty * rate
        ptype = 'return' if action == 'return' else 'purchase'
        if _has_recent_duplicate(
            Purchase,
            project_id=pid,
            stage_id=sid,
            material_id=mid,
            entry_type=ptype,
            date=purchase_date,
            qty=signed_qty,
            rate=rate,
            total=total,
            return_ref=return_ref,
            notes=notes
        ):
            flash(f'Duplicate {ptype} prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_purchases'))
        db.session.add(Purchase(
            project_id=pid,
            stage_id=sid,
            material_id=mid,
            entry_type=ptype,
            date=purchase_date,
            activity_at=_activity_at_for(purchase_date),
            qty=signed_qty, rate=rate, total=total,
            supplier_name=supplier_name,
            return_ref=return_ref,
            return_reason=return_reason,
            approved_by=approved_by,
            notes=notes))
        db.session.commit()
        flash('Return to supplier recorded.' if action == 'return' else 'Purchase recorded.', 'success')
        return redirect(url_for('hdc_purchases'))

    projects   = Project.query.all()
    materials  = Material.query.filter_by(is_active=True).all()
    stages     = Stage.query.all()
    pid        = request.args.get('project_id', type=int)
    q = db.session.query(Purchase, Material, Project)\
        .join(Material, Purchase.material_id == Material.id)\
        .join(Project, Purchase.project_id == Project.id)
    if pid: q = q.filter(Purchase.project_id == pid)
    records = q.order_by(Purchase.activity_at.desc(), Purchase.date.desc()).all()
    total   = sum(r.Purchase.total for r in records)
    return render_template('purchases.html',
        projects=projects, materials=materials, stages=stages,
        records=records, total=total,
        selected_project=pid, today=_pkt_today().isoformat())

@app.route('/hdc/purchase-v2')
@login_required
def hdc_purchase_v2_page():
    material_count = int(db.session.query(func.count(MaterialV2.id)).filter(MaterialV2.is_void == False).scalar() or 0)
    purchase_count = int(db.session.query(func.count(PurchaseV2.id)).filter(PurchaseV2.is_void == False).scalar() or 0)
    purchase_total = float(db.session.query(func.coalesce(func.sum(PurchaseV2.total_amount), 0.0))
                           .filter(PurchaseV2.is_void == False).scalar() or 0.0)
    supplier_count = int(db.session.query(func.count(Supplier.id)).filter(Supplier.is_void == False).scalar() or 0)
    delivered_count = int(db.session.query(func.count(Delivery.id)).filter(Delivery.is_void == False).scalar() or 0)
    delivered_qty = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(Delivery.is_void == False).scalar() or 0.0)
    usage_count = int(db.session.query(func.count(UsageLogV2.id)).filter(UsageLogV2.is_void == False).scalar() or 0)
    usage_qty = float(db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0)).filter(UsageLogV2.is_void == False).scalar() or 0.0)
    usage_cost = float(db.session.query(func.coalesce(func.sum(UsageLogV2.cost), 0.0)).filter(UsageLogV2.is_void == False).scalar() or 0.0)
    integrity = _purchase_v2_integrity_report()
    return render_template(
        'purchase_v2.html',
        material_count=material_count,
        purchase_count=purchase_count,
        purchase_total=purchase_total,
        supplier_count=supplier_count,
        delivered_count=delivered_count,
        delivered_qty=delivered_qty,
        usage_count=usage_count,
        usage_qty=usage_qty,
        usage_cost=usage_cost,
        integrity=integrity
    )
@app.route('/hdc/purchase-v2/materials', methods=['GET', 'POST'])
@login_required
def hdc_purchase_v2_materials():
    if request.method == 'POST':
        name = _normalize_name_ci(request.form.get('name'))
        unit = (request.form.get('unit') or 'KG').strip().upper()
        if unit not in _MATERIAL_V2_UNITS:
            flash(f'Unit must be one of: {", ".join(_MATERIAL_V2_UNITS)}.', 'danger')
            return redirect(url_for('hdc_purchase_v2_materials'))
        row = _ensure_material_v2(name, unit)
        if not row:
            flash('Material name is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_materials'))
        row.unit = unit
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'create', f'{current_user.username.title()} added material {row.name} ({unit})', 'material_v2', row.id)
        db.session.commit()
        flash('Material saved.', 'success')
        return redirect(url_for('hdc_purchase_v2_materials'))
    rows = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
    material_ids = [int(m.id) for m in rows]
    purchased_map = {}
    if material_ids:
        purchased_map = dict(
            db.session.query(
                PurchaseV2.material_id,
                func.coalesce(func.sum(PurchaseV2.quantity), 0.0)
            ).filter(
                PurchaseV2.is_void == False,
                PurchaseV2.material_id.in_(material_ids)
            ).group_by(PurchaseV2.material_id).all()
        )
    materials = []
    totals = {
        'purchased_qty': 0.0,
        'delivered_qty': 0.0,
        'used_qty': 0.0,
        'available_qty': 0.0,
        'in_store_qty': 0.0
    }
    for m in rows:
        purchased = float(purchased_map.get(m.id, 0.0) or 0.0)
        delivered = _material_v2_delivered(m.id)
        used = _material_v2_used(m.id)
        available = max(0.0, delivered - used)
        in_store = max(0.0, purchased - delivered)
        materials.append({
            'row': m,
            'purchased_qty': purchased,
            'delivered_qty': delivered,
            'used_qty': used,
            'available_qty': available,
            'in_store_qty': in_store
        })
        totals['purchased_qty'] += purchased
        totals['delivered_qty'] += delivered
        totals['used_qty'] += used
        totals['available_qty'] += available
        totals['in_store_qty'] += in_store
    return render_template('purchase_v2_materials.html', materials=materials, material_units=_MATERIAL_V2_UNITS, totals=totals)
@app.route('/hdc/purchase-v2/materials/<int:material_id>/edit', methods=['POST'])
@login_required
def hdc_purchase_v2_material_edit(material_id):
    row = MaterialV2.query.get_or_404(material_id)
    name = _normalize_name_ci(request.form.get('name')) or row.name
    unit = (request.form.get('unit') or row.unit or 'KG').strip().upper()
    status = (request.form.get('status') or row.status or 'active').strip().lower()
    if status not in ('active', 'inactive'):
        status = 'active'
    if unit not in _MATERIAL_V2_UNITS:
        flash(f'Unit must be one of: {", ".join(_MATERIAL_V2_UNITS)}.', 'danger')
        return redirect(url_for('hdc_purchase_v2_materials'))
    exists = (MaterialV2.query
              .filter(
                  MaterialV2.id != row.id,
                  MaterialV2.is_void == False,
                  func.lower(MaterialV2.name) == name.lower()
              )
              .first())
    if exists:
        flash('Another material already uses this name.', 'danger')
        return redirect(url_for('hdc_purchase_v2_materials'))
    row.name = name
    row.unit = unit
    row.status = status
    row.updated_at = _pkt_now_naive()
    log_action(current_user, 'update', f'{current_user.username.title()} updated material #{row.id}: {row.name} ({row.unit})', 'material_v2', row.id)
    db.session.commit()
    flash('Material updated.', 'success')
    return redirect(url_for('hdc_purchase_v2_materials'))
@app.route('/hdc/purchase-v2/materials/<int:material_id>/delete', methods=['POST'])
@login_required
def hdc_purchase_v2_material_delete(material_id):
    row = MaterialV2.query.get_or_404(material_id)
    has_purchase = PurchaseV2.query.filter_by(material_id=row.id, is_void=False).first() is not None
    has_delivery = Delivery.query.filter_by(material_id=row.id, is_void=False).first() is not None
    has_usage = UsageLogV2.query.filter_by(material_id=row.id, is_void=False).first() is not None
    if has_purchase or has_delivery or has_usage:
        row.status = 'inactive'
        row.updated_at = _pkt_now_naive()
        db.session.commit()
        flash('Material has transactions, so it was set inactive instead of delete.', 'warning')
        return redirect(url_for('hdc_purchase_v2_materials'))
    row.is_void = True
    row.status = 'inactive'
    row.updated_at = _pkt_now_naive()
    log_action(current_user, 'delete', f'{current_user.username.title()} deleted material #{row.id}: {row.name}', 'material_v2', row.id)
    db.session.commit()
    flash('Material deleted.', 'success')
    return redirect(url_for('hdc_purchase_v2_materials'))
@app.route('/hdc/purchase-v2/purchases', methods=['GET', 'POST'])
@login_required
def hdc_purchase_v2_purchases():
    if request.method == 'POST':
        supplier_id = request.form.get('supplier_id', type=int)
        material_id = request.form.get('material_id', type=int)
        unit_price = max(0.0, _flt(request.form.get('unit_price'), 0.0))
        quantity = max(0.0, _flt(request.form.get('quantity'), 0.0))
        payment_status = (request.form.get('payment_status') or 'unpaid').strip().lower()
        if payment_status not in ('paid', 'unpaid'):
            payment_status = 'unpaid'
        _date_raw = (request.form.get('date') or '').strip()
        try:
            _date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else _pkt_today()
        except ValueError:
            _date = _pkt_today()
        _notes     = (request.form.get('notes') or '').strip() or None
        _challan   = (request.form.get('challan_no') or '').strip() or None
        supplier = Supplier.query.get(supplier_id) if supplier_id else None
        material = MaterialV2.query.get(material_id) if material_id else None
        if (not supplier) or supplier.is_void or (supplier.status or 'active').strip().lower() != 'active':
            flash('Valid supplier is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
            flash('Valid material is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        if unit_price <= 0 or quantity <= 0:
            flash('Unit price and quantity must be greater than 0.', 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        total_amount = float(unit_price * quantity)
        if _has_recent_duplicate(
            PurchaseV2,
            supplier_id=supplier.id,
            material_id=material.id,
            unit_price=unit_price,
            quantity=quantity,
            total_amount=total_amount,
            payment_status=payment_status,
            date=_date,
            notes=_notes,
            challan_no=_challan
        ):
            flash('Duplicate purchase prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        row = PurchaseV2(
            supplier_id=supplier.id,
            material_id=material.id,
            unit_price=unit_price,
            quantity=quantity,
            total_amount=total_amount,
            payment_status=payment_status,
            date=_date,
            notes=_notes,
            challan_no=_challan,
            is_void=False,
            created_at=_pkt_now_naive(),
            updated_at=_pkt_now_naive()
        )
        db.session.add(row)
        db.session.flush()
        _sync_purchase_v2_ledger(row)
        if payment_status == 'paid':
            ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
            if not ok_txn:
                db.session.rollback()
                return jsonify(ok=False, message=(msg_txn or 'Unable to post paid purchase in unified accounts.')), 400
        log_action(
            current_user,
            'create',
            f'{current_user.username.title()} created purchase #{row.id}: {supplier.name}, {material.name}, {quantity:.2f} x {unit_price:.2f} = {total_amount:.2f} ({payment_status})',
            'purchase_v2',
            row.id
        )
        db.session.commit()
        flash(f'Purchase #{row.id} recorded.', 'success')
        return redirect(url_for('hdc_purchase_v2_purchases'))
    supplier_id = request.args.get('supplier_id', type=int)
    q = PurchaseV2.query.filter(PurchaseV2.is_void == False)
    if supplier_id:
        q = q.filter(PurchaseV2.supplier_id == supplier_id)
    rows = q.order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc()).all()
    suppliers = Supplier.query.filter(Supplier.is_void == False).order_by(Supplier.name.asc()).all()
    materials = MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.name.asc()).all()
    purchase_ids = [int(r.id) for r in rows]
    delivered_map = {}
    if purchase_ids:
        delivered_map = dict(
            db.session.query(
                Delivery.purchase_id,
                func.coalesce(func.sum(Delivery.quantity), 0.0)
            ).filter(
                Delivery.is_void == False,
                Delivery.purchase_id.in_(purchase_ids)
            ).group_by(Delivery.purchase_id).all()
        )
    total = float(sum(float(r.total_amount or 0.0) for r in rows))
    return render_template(
        'purchase_v2_purchases.html',
        rows=rows,
        suppliers=suppliers,
        materials=materials,
        delivered_map=delivered_map,
        total=total,
        selected_supplier_id=supplier_id,
        today=_pkt_today().isoformat()
    )
@app.route('/hdc/purchase-v2/purchases/<int:purchase_id>/edit', methods=['POST'])
@login_required
def hdc_purchase_v2_purchase_edit(purchase_id):
    row = PurchaseV2.query.get_or_404(purchase_id)
    if row.is_void:
        flash('Purchase is already deleted.', 'danger')
        return redirect(url_for('hdc_purchase_v2_purchases'))
    supplier_id = request.form.get('supplier_id', type=int)
    material_id = request.form.get('material_id', type=int)
    unit_price = max(0.0, _flt(request.form.get('unit_price'), row.unit_price))
    quantity = max(0.0, _flt(request.form.get('quantity'), row.quantity))
    payment_status = (request.form.get('payment_status') or row.payment_status or 'unpaid').strip().lower()
    if payment_status not in ('paid', 'unpaid'):
        payment_status = 'unpaid'
    supplier = Supplier.query.get(supplier_id) if supplier_id else None
    material = MaterialV2.query.get(material_id) if material_id else None
    if (not supplier) or supplier.is_void or (supplier.status or 'active').strip().lower() != 'active':
        flash('Valid supplier is required.', 'danger')
        return redirect(url_for('hdc_purchase_v2_purchases'))
    if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
        flash('Valid material is required.', 'danger')
        return redirect(url_for('hdc_purchase_v2_purchases'))
    if unit_price <= 0 or quantity <= 0:
        flash('Unit price and quantity must be greater than 0.', 'danger')
        return redirect(url_for('hdc_purchase_v2_purchases'))
    delivered = _purchase_v2_delivered_qty(row.id)
    if quantity + 1e-9 < delivered:
        flash(f'Cannot set quantity below delivered quantity ({delivered:,.2f}).', 'danger')
        return redirect(url_for('hdc_purchase_v2_purchases'))
    if row.material_id != material.id and delivered > 0:
        flash('Cannot change material after deliveries are recorded for this purchase.', 'danger')
        return redirect(url_for('hdc_purchase_v2_purchases'))
    _date_raw = (request.form.get('date') or '').strip()
    try:
        _date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else (row.date or _pkt_today())
    except ValueError:
        _date = row.date or _pkt_today()
    _notes   = (request.form.get('notes') or '').strip() or None
    _challan = (request.form.get('challan_no') or '').strip() or None
    old_payment_status = (row.payment_status or 'unpaid').strip().lower()
    row.supplier_id = supplier.id
    row.material_id = material.id
    row.unit_price = unit_price
    row.quantity = quantity
    row.total_amount = float(unit_price * quantity)
    row.payment_status = payment_status
    row.date = _date
    row.notes = _notes
    row.challan_no = _challan
    row.updated_at = _pkt_now_naive()
    _sync_purchase_v2_ledger(row)
    if payment_status == 'paid':
        ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
        if not ok_txn and 'Duplicate source transaction' not in (msg_txn or ''):
            db.session.rollback()
            flash(msg_txn or 'Unable to post paid purchase in unified accounts.', 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
    elif old_payment_status == 'paid':
        _accounts_set_void_by_source('purchase_v2_paid', row.id, True)
    log_action(
        current_user,
        'update',
        f'{current_user.username.title()} updated purchase #{row.id}',
        'purchase_v2',
        row.id
    )
    db.session.commit()
    flash(f'Purchase #{row.id} updated.', 'success')
    return redirect(url_for('hdc_purchase_v2_purchases'))
@app.route('/hdc/purchase-v2/purchases/<int:purchase_id>/delete', methods=['POST'])
@login_required
def hdc_purchase_v2_purchase_delete(purchase_id):
    row = PurchaseV2.query.get_or_404(purchase_id)
    if row.is_void:
        flash('Purchase is already deleted.', 'warning')
        return redirect(url_for('hdc_purchase_v2_purchases'))
    delivered = _purchase_v2_delivered_qty(row.id)
    if delivered > 0:
        flash(f'Cannot delete purchase #{row.id}; delivery exists ({delivered:,.2f}).', 'danger')
        return redirect(url_for('hdc_purchase_v2_purchases'))
    row.is_void = True
    row.void_reason = 'Deleted by user from Purchase V2'
    row.voided_at = _pkt_now_naive()
    row.updated_at = _pkt_now_naive()
    _sync_purchase_v2_ledger(row)
    _accounts_set_void_by_source('purchase_v2_paid', row.id, True)
    log_action(current_user, 'delete', f'{current_user.username.title()} deleted purchase #{row.id}', 'purchase_v2', row.id)
    db.session.commit()
    flash(f'Purchase #{row.id} deleted.', 'success')
    return redirect(url_for('hdc_purchase_v2_purchases'))
@app.route('/hdc/purchase-v2/suppliers', methods=['GET', 'POST'])
@login_required
def hdc_purchase_v2_suppliers():
    if request.method == 'POST':
        name = _normalize_name_ci(request.form.get('name'))
        phone = (request.form.get('phone') or '').strip()
        opening_balance = max(0.0, _flt(request.form.get('opening_balance'), 0.0))
        if not name:
            flash('Supplier name is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_suppliers'))
        address = (request.form.get('address') or '').strip()
        row = _ensure_supplier_quick(name, phone)
        if not row:
            flash('Unable to save supplier.', 'danger')
            return redirect(url_for('hdc_purchase_v2_suppliers'))
        if address and hasattr(row, 'address'):
            row.address = address
        row.updated_at = _pkt_now_naive()
        if opening_balance > 0:
            db.session.add(SupplierLedger(
                supplier_id=row.id,
                entry_type='debit',
                amount=float(opening_balance),
                reference_type='opening_balance',
                reference_id=None,
                note='Opening balance imported',
                is_void=False,
                created_at=_pkt_now_naive()
            ))
        log_action(current_user, 'create', f'{current_user.username.title()} added supplier {row.name}', 'supplier', row.id)
        db.session.commit()
        flash('Supplier saved.', 'success')
        return redirect(url_for('hdc_purchase_v2_suppliers'))

    q = (request.args.get('q') or '').strip()
    query = Supplier.query.filter(Supplier.is_void == False)
    if q:
        ql = f'%{q.lower()}%'
        query = query.filter(or_(func.lower(Supplier.name).like(ql), func.lower(func.coalesce(Supplier.phone, '')).like(ql)))
    rows = query.order_by(Supplier.name.asc()).all()

    debit_rows = dict(db.session.query(
        SupplierLedger.supplier_id,
        func.coalesce(func.sum(SupplierLedger.amount), 0.0)
    ).filter(
        SupplierLedger.is_void == False,
        SupplierLedger.entry_type == 'debit'
    ).group_by(SupplierLedger.supplier_id).all())

    credit_kind_rows = db.session.query(
        SupplierLedger.supplier_id,
        func.lower(func.coalesce(SupplierLedger.reference_type, 'payment')),
        func.coalesce(func.sum(SupplierLedger.amount), 0.0)
    ).filter(
        SupplierLedger.is_void == False,
        SupplierLedger.entry_type == 'credit',
        func.lower(func.coalesce(SupplierLedger.reference_type, 'payment')).in_(['payment', 'tip', 'settlement'])
    ).group_by(SupplierLedger.supplier_id, func.lower(func.coalesce(SupplierLedger.reference_type, 'payment'))).all()

    credit_total_rows = dict(db.session.query(
        SupplierLedger.supplier_id,
        func.coalesce(func.sum(SupplierLedger.amount), 0.0)
    ).filter(
        SupplierLedger.is_void == False,
        SupplierLedger.entry_type == 'credit'
    ).group_by(SupplierLedger.supplier_id).all())

    credit_kind_map = {}
    for sid, rtype, amt in credit_kind_rows:
        credit_kind_map[(int(sid or 0), (rtype or 'payment'))] = float(amt or 0.0)
    purchase_total_rows = dict(db.session.query(
        PurchaseV2.supplier_id,
        func.coalesce(func.sum(PurchaseV2.total_amount), 0.0)
    ).filter(
        PurchaseV2.is_void == False
    ).group_by(PurchaseV2.supplier_id).all())

    suppliers = []
    for s in rows:
        debit = float(debit_rows.get(s.id, 0.0) or 0.0)
        credit = float(credit_total_rows.get(s.id, 0.0) or 0.0)
        suppliers.append({
            'id': s.id,
            'name': s.name,
            'phone': s.phone or '',
            'status': s.status or 'active',
            'balance': max(0.0, debit - credit),
            'purchase_total': float(purchase_total_rows.get(s.id, 0.0) or 0.0),
            'payment_total': float(credit_kind_map.get((s.id, 'payment'), 0.0) or 0.0),
            'tip_total': float(credit_kind_map.get((s.id, 'tip'), 0.0) or 0.0),
            'settlement_total': float(credit_kind_map.get((s.id, 'settlement'), 0.0) or 0.0),
        })

    return render_template('purchase_v2_suppliers.html', suppliers=suppliers, q=q)

@app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>')
@login_required
def hdc_purchase_v2_supplier_detail(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    _repair_supplier_purchase_v2_ledger(supplier.id)
    if db.session.new or db.session.dirty:
        db.session.commit()
    purchases = (PurchaseV2.query
                 .filter(PurchaseV2.supplier_id == supplier.id, PurchaseV2.is_void == False)
                 .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
                 .all())
    ledger_rows = (SupplierLedger.query
                   .filter(SupplierLedger.supplier_id == supplier.id, SupplierLedger.is_void == False)
                   .order_by(SupplierLedger.created_at.asc(), SupplierLedger.id.asc())
                   .all())
    purchase_total = float(sum(float(p.total_amount or 0.0) for p in purchases))
    debit_total = float(sum(float(r.amount or 0.0) for r in ledger_rows if (r.entry_type or '').strip().lower() == 'debit'))
    paid_total = float(sum(float(r.amount or 0.0) for r in ledger_rows if (r.entry_type or '').strip().lower() == 'credit'))
    net_balance = float(debit_total - paid_total)
    balance = max(0.0, net_balance)
    advance_total = max(0.0, -net_balance)
    purchase_ids = [int(p.id) for p in purchases]
    delivered_map = {}
    if purchase_ids:
        delivered_map = dict(
            db.session.query(
                Delivery.purchase_id,
                func.coalesce(func.sum(Delivery.quantity), 0.0)
            ).filter(
                Delivery.is_void == False,
                Delivery.purchase_id.in_(purchase_ids)
            ).group_by(Delivery.purchase_id).all()
        )
    payment_total = float(sum(float(r.amount or 0.0) for r in ledger_rows if (r.entry_type or '').strip().lower() == 'credit' and (r.reference_type or 'payment').strip().lower() == 'payment'))
    tip_total = float(sum(float(r.amount or 0.0) for r in ledger_rows if (r.entry_type or '').strip().lower() == 'credit' and (r.reference_type or '').strip().lower() == 'tip'))
    settlement_total = float(sum(float(r.amount or 0.0) for r in ledger_rows if (r.entry_type or '').strip().lower() == 'credit' and (r.reference_type or '').strip().lower() == 'settlement'))
    materials = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
    purchase_map = {int(p.id): p for p in purchases}
    ledger_items = []
    running_balance = 0.0
    for r in ledger_rows:
        entry_type = (r.entry_type or '').strip().lower()
        ref_type = (r.reference_type or '').strip().lower()
        ref_id = int(r.reference_id or 0)
        linked_purchase = purchase_map.get(ref_id) if ref_type == 'purchase_v2' and ref_id else None
        material_name = (linked_purchase.material.name if linked_purchase and linked_purchase.material else '-')
        qty = float(linked_purchase.quantity or 0.0) if linked_purchase else 0.0
        unit_price = float(linked_purchase.unit_price or 0.0) if linked_purchase else 0.0
        total_price = float(linked_purchase.total_amount or 0.0) if linked_purchase else 0.0
        amount = float(r.amount or 0.0)
        paid_amount = amount if entry_type == 'credit' else 0.0
        purchase_amount = amount if entry_type == 'debit' else 0.0
        if entry_type == 'debit':
            running_balance += amount
        elif entry_type == 'credit':
            running_balance -= amount
        balance_kind = 'payable' if running_balance > 1e-9 else ('advance' if running_balance < -1e-9 else 'settled')
        if entry_type == 'debit':
            entry_label = ('Opening Balance' if ref_type == 'opening_balance' else 'Purchase')
        else:
            entry_label = (ref_type.title() if ref_type else 'Payment')
        ledger_items.append({
            'id': r.id,
            'date': r.created_at,
            'supplier': supplier.name,
            'entry_label': entry_label,
            'material_name': material_name,
            'qty': qty,
            'unit_price': unit_price,
            'total_price': total_price,
            'purchase_amount': purchase_amount,
            'paid': paid_amount,
            'entry_type': entry_type,
            'balance_amount': running_balance,
            'balance_kind': balance_kind,
            'note': r.note or '',
            'reference_type': r.reference_type or '',
            'reference_id': r.reference_id
        })
    return render_template(
        'purchase_v2_supplier_detail.html',
        supplier=supplier,
        purchases=purchases,
        delivered_map=delivered_map,
        materials=materials,
        material_units=_MATERIAL_V2_UNITS,
        ledger_rows=ledger_rows,
        ledger_items=ledger_items,
        purchase_total=purchase_total,
        debit_total=debit_total,
        paid_total=paid_total,
        balance=balance,
        advance_total=advance_total,
        payment_total=payment_total,
        tip_total=tip_total,
        settlement_total=settlement_total
    )
@app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/purchase', methods=['POST'])
@login_required
def hdc_purchase_v2_supplier_purchase(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    if supplier.is_void or (supplier.status or 'active').strip().lower() != 'active':
        flash('Supplier is not active.', 'danger')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
    material_id = request.form.get('material_id', type=int)
    unit_price = max(0.0, _flt(request.form.get('unit_price'), 0.0))
    quantity = max(0.0, _flt(request.form.get('quantity'), 0.0))
    payment_status = (request.form.get('payment_status') or 'unpaid').strip().lower()
    if payment_status not in ('paid', 'unpaid'):
        payment_status = 'unpaid'
    material = MaterialV2.query.get(material_id) if material_id else None
    if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
        flash('Select a valid active material.', 'danger')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
    if unit_price <= 0 or quantity <= 0:
        flash('Unit price and quantity must be greater than 0.', 'danger')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
    total_amount = float(unit_price * quantity)
    row = PurchaseV2(
        supplier_id=supplier.id,
        material_id=material.id,
        unit_price=unit_price,
        quantity=quantity,
        total_amount=total_amount,
        payment_status=payment_status,
        is_void=False,
        created_at=_pkt_now_naive(),
        updated_at=_pkt_now_naive()
    )
    db.session.add(row)
    db.session.flush()
    _sync_purchase_v2_ledger(row)
    if (payment_status or '').strip().lower() == 'paid':
        ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to post paid purchase in unified accounts.', 'danger')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
    log_action(
        current_user,
        'create',
        f'{current_user.username.title()} created purchase #{row.id}: {supplier.name}, {material.name}, {quantity:.2f} x {unit_price:.2f} = {total_amount:.2f} ({payment_status})',
        'purchase_v2',
        row.id
    )
    db.session.commit()
    flash(f'Purchase #{row.id} recorded for {supplier.name}.', 'success')
    return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
@app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/payment', methods=['POST'])
@login_required
def hdc_purchase_v2_supplier_payment(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    amount = max(0.0, _flt(request.form.get('amount'), 0.0))
    entry_kind = ((request.form.get('entry_kind') or 'payment').strip().lower())
    note = (request.form.get('note') or '').strip()
    if entry_kind not in ('payment', 'tip', 'settlement'):
        entry_kind = 'payment'
    if amount <= 0:
        flash('Amount must be greater than 0.', 'danger')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))

    ledger_row = SupplierLedger(
        supplier_id=supplier.id,
        entry_type='credit',
        amount=amount,
        reference_type=entry_kind,
        reference_id=None,
        note=note or f'Supplier {entry_kind}',
        is_void=False,
        created_at=_pkt_now_naive()
    )
    db.session.add(ledger_row)
    db.session.flush()
    ok_txn, msg_txn, _ = _accounts_post_supplier_credit_row(ledger_row, supplier_name=supplier.name, commit=False)
    if not ok_txn:
        db.session.rollback()
        flash(msg_txn or 'Unable to post supplier payment in unified accounts.', 'danger')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
    _sync_supplier_po_payment_status(supplier.id)
    log_action(current_user, 'payment', f'{current_user.username.title()} recorded supplier {entry_kind}: {supplier.name}, {amount:.2f} PKR', f'supplier_{entry_kind}', supplier.id)
    db.session.commit()
    flash(f'{entry_kind.title()} posted successfully.', 'success')
    return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))

@app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/edit', methods=['POST'])
@login_required
def hdc_purchase_v2_supplier_edit(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    name = _normalize_name_ci(request.form.get('name')) or supplier.name
    phone = (request.form.get('phone') or '').strip()
    status = (request.form.get('status') or supplier.status or 'active').strip().lower()
    opening_balance_add = max(0.0, _flt(request.form.get('opening_balance_add'), 0.0))
    if status not in ('active', 'inactive'):
        status = 'active'

    exists = (Supplier.query
              .filter(
                  Supplier.id != supplier.id,
                  Supplier.is_void == False,
                  func.lower(Supplier.name) == name.lower()
              )
              .first())
    if exists:
        flash('Another supplier already uses this name.', 'danger')
        back = (request.referrer or '').strip()
        if '/hdc/purchase-v2/suppliers' in back:
            return redirect(back)
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))

    supplier.name = name
    supplier.phone = phone
    supplier.status = status
    supplier.updated_at = _pkt_now_naive()
    if opening_balance_add > 0:
        db.session.add(SupplierLedger(
            supplier_id=supplier.id,
            entry_type='debit',
            amount=float(opening_balance_add),
            reference_type='opening_balance',
            reference_id=None,
            note='Opening balance adjustment',
            is_void=False,
            created_at=_pkt_now_naive()
        ))
    log_action(current_user, 'update', f'{current_user.username.title()} updated supplier #{supplier.id}: {supplier.name}', 'supplier', supplier.id)
    db.session.commit()
    flash('Supplier updated.', 'success')
    back = (request.referrer or '').strip()
    if '/hdc/purchase-v2/suppliers' in back:
        return redirect(back)
    return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))

@app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/suspend', methods=['POST'])
@login_required
def hdc_purchase_v2_supplier_suspend(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    mode = (request.form.get('mode') or 'suspend').strip().lower()
    supplier.status = 'inactive' if mode == 'suspend' else 'active'
    supplier.updated_at = _pkt_now_naive()
    log_action(current_user, 'update', f'{current_user.username.title()} changed supplier #{supplier.id} status to {supplier.status}', 'supplier', supplier.id)
    db.session.commit()
    flash(f'Supplier status set to {supplier.status}.', 'success')
    back = (request.referrer or '').strip()
    if '/hdc/purchase-v2/suppliers' in back:
        return redirect(back)
    return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))

@app.route('/hdc/purchase-v2/delivered', methods=['GET', 'POST'])
@login_required
def hdc_purchase_v2_delivered():
    if request.method == 'POST':
        purchase_id = request.form.get('purchase_id', type=int)
        project_id = request.form.get('project_id', type=int)
        stage_id = request.form.get('stage_id', type=int)
        quantity = max(0.0, _flt(request.form.get('quantity'), 0.0))
        delivery_person = (request.form.get('delivery_person') or '').strip()
        purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
        project = Project.query.get(project_id) if project_id else None
        stage = Stage.query.get(stage_id) if stage_id else None
        if (not purchase) or purchase.is_void:
            flash('Valid purchase is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        if not project:
            flash('Valid project is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        if quantity <= 0:
            flash('Quantity must be greater than 0.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        delivered_so_far = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
                                 .filter(Delivery.purchase_id == purchase.id, Delivery.is_void == False).scalar() or 0.0)
        remaining_purchase_qty = max(0.0, float(purchase.quantity or 0.0) - delivered_so_far)
        if quantity > remaining_purchase_qty + 1e-9:
            flash(f'Cannot deliver more than remaining purchase quantity ({remaining_purchase_qty:,.2f}).', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        _del_date_raw = (request.form.get('date') or '').strip()
        try:
            _del_date = datetime.strptime(_del_date_raw, '%Y-%m-%d').date() if _del_date_raw else _pkt_today()
        except ValueError:
            _del_date = _pkt_today()
        _del_notes = (request.form.get('notes') or '').strip() or None
        row = Delivery(
            purchase_id=purchase.id,
            material_id=purchase.material_id,
            project_id=project.id,
            stage_id=stage.id if stage else None,
            quantity=quantity,
            date=_del_date,
            notes=_del_notes,
            delivery_person=delivery_person,
            is_void=False,
            created_at=_pkt_now_naive()
        )
        db.session.add(row)
        db.session.flush()
        log_action(
            current_user,
            'delivery',
            f'{current_user.username.title()} recorded delivery #{row.id}: {purchase.material.name if purchase.material else "-"} {quantity:.2f} to {project.name} -> {stage.name if stage else "-"}',
            'delivery',
            row.id
        )
        db.session.commit()
        flash(f'Delivery #{row.id} recorded.', 'success')
        return redirect(url_for('hdc_purchase_v2_delivered'))
    rows = (Delivery.query
            .filter(Delivery.is_void == False)
            .order_by(Delivery.created_at.asc(), Delivery.id.asc())
            .all())
    prefill_shift_material_id = request.args.get('shift_material_id', type=int)
    prefill_from_project_id = request.args.get('from_project_id', type=int)
    prefill_from_stage_id = request.args.get('from_stage_id', type=int)
    prefill_to_project_id = request.args.get('to_project_id', type=int)
    prefill_to_stage_id = request.args.get('to_stage_id', type=int)
    edit_delivery_id = request.args.get('edit_delivery_id', type=int)
    prefill_edit_row = None
    if edit_delivery_id:
        prefill_edit_row = Delivery.query.filter(
            Delivery.id == edit_delivery_id,
            Delivery.is_void == False
        ).first()
    total_qty = float(sum(float(r.quantity or 0.0) for r in rows))
    projects = Project.query.order_by(Project.name.asc()).all()
    stages = Stage.query.order_by(Stage.name.asc()).all()
    purchases = (PurchaseV2.query
                 .filter(PurchaseV2.is_void == False)
                 .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
                 .all())
    purchase_ids = [int(p.id) for p in purchases]
    delivered_map = {}
    if purchase_ids:
        delivered_map = dict(
            db.session.query(
                Delivery.purchase_id,
                func.coalesce(func.sum(Delivery.quantity), 0.0)
            ).filter(
                Delivery.is_void == False,
                Delivery.purchase_id.in_(purchase_ids)
            ).group_by(Delivery.purchase_id).all()
        )
    purchase_rows = []
    for p in purchases:
        delivered_qty = float(delivered_map.get(p.id, 0.0) or 0.0)
        remaining_qty = max(0.0, float(p.quantity or 0.0) - delivered_qty)
        if remaining_qty <= 1e-9:
            continue
        purchase_rows.append({
            'id': p.id,
            'supplier_name': (p.supplier.name if p.supplier else '-'),
            'material_name': (p.material.name if p.material else '-'),
            'material_id': int(p.material_id or 0),
            'unit': (p.material.unit if p.material else ''),
            'total_qty': float(p.quantity or 0.0),
            'delivered_qty': delivered_qty,
            'remaining_qty': remaining_qty
        })
    all_materials = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
    material_ids_with_stock = {int(pr['material_id']) for pr in purchase_rows}
    return render_template(
        'purchase_v2_delivered.html',
        rows=rows,
        total_qty=total_qty,
        projects=projects,
        stages=stages,
        purchase_rows=purchase_rows,
        all_materials=all_materials,
        material_ids_with_stock=material_ids_with_stock,
        today=_pkt_today().isoformat(),
        prefill_shift_material_id=prefill_shift_material_id,
        prefill_from_project_id=prefill_from_project_id,
        prefill_from_stage_id=prefill_from_stage_id,
        prefill_to_project_id=prefill_to_project_id,
        prefill_to_stage_id=prefill_to_stage_id,
        prefill_edit_row=prefill_edit_row
    )

@app.route('/hdc/purchase-v2/delivered/transfer', methods=['POST'])
@login_required
def hdc_purchase_v2_delivery_transfer():
    material_id = request.form.get('material_id', type=int)
    from_project_id = request.form.get('from_project_id', type=int)
    from_stage_id = request.form.get('from_stage_id', type=int)
    to_project_id = request.form.get('to_project_id', type=int)
    to_stage_id = request.form.get('to_stage_id', type=int)
    qty = max(0.0, _flt(request.form.get('quantity'), 0.0))
    note = (request.form.get('notes') or '').strip()
    material = MaterialV2.query.get(material_id) if material_id else None
    from_project = Project.query.get(from_project_id) if from_project_id else None
    to_project = Project.query.get(to_project_id) if to_project_id else None
    from_stage = Stage.query.get(from_stage_id) if from_stage_id else None
    to_stage = Stage.query.get(to_stage_id) if to_stage_id else None

    if (not material) or material.is_void:
        flash('Valid material is required for stock shift.', 'danger')
        return redirect(url_for('hdc_purchase_v2_delivered'))
    if not from_project or not to_project:
        flash('Both source and destination projects are required.', 'danger')
        return redirect(url_for('hdc_purchase_v2_delivered'))
    if from_stage_id and ((not from_stage) or int(from_stage.project_id or 0) != int(from_project.id)):
        flash('Source stage does not belong to source project.', 'danger')
        return redirect(url_for('hdc_purchase_v2_delivered'))
    if to_stage_id and ((not to_stage) or int(to_stage.project_id or 0) != int(to_project.id)):
        flash('Destination stage does not belong to destination project.', 'danger')
        return redirect(url_for('hdc_purchase_v2_delivered'))
    if int(from_project.id) == int(to_project.id) and int(from_stage_id or 0) == int(to_stage_id or 0):
        flash('Source and destination cannot be the same scope.', 'danger')
        return redirect(url_for('hdc_purchase_v2_delivered'))
    if qty <= 0:
        flash('Shift quantity must be greater than 0.', 'danger')
        return redirect(url_for('hdc_purchase_v2_delivered'))

    available = _material_v2_available(material.id, from_project.id, from_stage.id if from_stage else None)
    if qty > available + 1e-9:
        flash(f'Shift exceeds source available stock ({available:,.2f}).', 'danger')
        return redirect(url_for('hdc_purchase_v2_delivered'))

    _date_raw = (request.form.get('date') or '').strip()
    try:
        transfer_date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else _pkt_today()
    except ValueError:
        transfer_date = _pkt_today()

    moved_rows = _transfer_v2_material_between_scopes(
        material_id=material.id,
        from_project_id=from_project.id,
        from_stage_id=(from_stage.id if from_stage else None),
        to_project_id=to_project.id,
        to_stage_id=(to_stage.id if to_stage else None),
        quantity=qty,
        date_value=transfer_date,
        note=note
    )
    moved_qty = float(sum(float(q or 0.0) for _, q in moved_rows))
    if moved_qty + 1e-9 < qty:
        db.session.rollback()
        flash('Unable to allocate selected quantity from source deliveries. Try a smaller quantity.', 'danger')
        return redirect(url_for('hdc_purchase_v2_delivered'))

    log_action(
        current_user,
        'delivery',
        f'{current_user.username.title()} shifted {material.name} {moved_qty:.2f} from {from_project.name} -> {to_project.name}',
        'delivery_transfer',
        material.id
    )
    db.session.commit()
    flash(f'Stock shifted successfully: {moved_qty:,.2f} {material.unit or ""}.', 'success')
    return redirect(url_for('hdc_purchase_v2_delivered'))

@app.route('/hdc/purchase-v2/usage', methods=['GET', 'POST'])
@login_required
def hdc_purchase_v2_usage_page():
    if request.method == 'POST':
        purchase_id = request.form.get('purchase_id', type=int)
        material_id = request.form.get('material_id', type=int)
        project_id = request.form.get('project_id', type=int)
        stage_id = request.form.get('stage_id', type=int)
        quantity = max(0.0, _flt(request.form.get('quantity'), 0.0))
        purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
        material = MaterialV2.query.get(material_id) if material_id else None
        project = Project.query.get(project_id) if project_id else None
        stage = Stage.query.get(stage_id) if stage_id else None
        if (not purchase_id) or (not purchase) or purchase.is_void:
            flash('Valid purchase order is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        if (not material) or material.is_void:
            flash('Valid material is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        if int(purchase.material_id or 0) != int(material.id):
            flash('Selected purchase order does not match selected material.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        if not project:
            flash('Valid project is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        if not stage_id:
            flash('Stage is required for strict stock control.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        if quantity <= 0:
            flash('Quantity must be greater than 0.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        available = _purchase_v2_available_in_scope_qty(purchase.id, project.id, stage.id if stage else None)
        if quantity > available + 1e-9:
            flash(f'Usage exceeds available stock for selected purchase order in this stage ({available:,.2f}).', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        unit_price = float(purchase.unit_price or 0.0)
        cost = float(unit_price * quantity)
        _use_date_raw = (request.form.get('date') or '').strip()
        try:
            _use_date = datetime.strptime(_use_date_raw, '%Y-%m-%d').date() if _use_date_raw else _pkt_today()
        except ValueError:
            _use_date = _pkt_today()
        _use_notes = (request.form.get('notes') or '').strip() or None
        row = UsageLogV2(
            purchase_id=purchase.id,
            material_id=material.id,
            project_id=project.id,
            stage_id=stage.id if stage else None,
            quantity=quantity,
            cost=cost,
            date=_use_date,
            notes=_use_notes,
            is_void=False,
            created_at=_pkt_now_naive()
        )
        db.session.add(row)
        db.session.flush()
        log_action(
            current_user,
            'usage',
            f'{current_user.username.title()} recorded usage #{row.id}: PO#{purchase.id}, {material.name} {quantity:.2f} @ {unit_price:.2f} ({cost:.2f} PKR) on {project.name} -> {stage.name if stage else "-"}',
            'usage',
            row.id
        )
        db.session.commit()
        flash(f'Usage #{row.id} recorded.', 'success')
        return redirect(url_for('hdc_purchase_v2_usage_page'))
    rows = (UsageLogV2.query
            .filter(UsageLogV2.is_void == False)
            .order_by(UsageLogV2.created_at.asc(), UsageLogV2.id.asc())
            .all())
    total_qty = float(sum(float(r.quantity or 0.0) for r in rows))
    total_cost = float(sum(float(r.cost or 0.0) for r in rows))
    projects = Project.query.order_by(Project.name.asc()).all()
    stages = Stage.query.order_by(Stage.name.asc()).all()
    materials = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
    stock_rows = []
    material_available_map = {}
    for m in materials:
        delivered = _material_v2_delivered(m.id)
        used = _material_v2_used(m.id)
        available = max(0.0, delivered - used)
        stock_rows.append({
            'id': m.id,
            'name': m.name,
            'unit': m.unit,
            'delivered_qty': delivered,
            'used_qty': used,
            'available_qty': available
        })
        material_available_map[m.id] = available
    return render_template(
        'purchase_v2_usage.html',
        rows=rows,
        total_qty=total_qty,
        total_cost=total_cost,
        projects=projects,
        stages=stages,
        materials=materials,
        stock_rows=stock_rows,
        material_available_map=material_available_map,
        today=_pkt_today().isoformat()
    )

@app.route('/hdc/purchase-v2/stock')
@login_required
def hdc_purchase_v2_stock():
    project_id = request.args.get('project_id', type=int)
    stage_id = request.args.get('stage_id', type=int)
    material_id = request.args.get('material_id', type=int)

    project = Project.query.get(project_id) if project_id else None
    stage = Stage.query.get(stage_id) if stage_id else None
    if stage_id and ((not stage) or (project and int(stage.project_id or 0) != int(project.id))):
        flash('Selected stage does not belong to selected project.', 'danger')
        return redirect(url_for('hdc_purchase_v2_stock', project_id=project_id, material_id=material_id))

    projects = Project.query.order_by(Project.name.asc()).all()
    stages = Stage.query.order_by(Stage.name.asc()).all()
    materials = MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.name.asc()).all()
    if material_id:
        materials = [m for m in materials if int(m.id) == int(material_id)]

    scope_project_id = project.id if project else None
    scope_stage_id = stage.id if stage else None

    summary_rows = []
    for m in materials:
        sent_qty = _material_v2_delivered(m.id, scope_project_id, scope_stage_id)
        used_qty = _material_v2_used(m.id, scope_project_id, scope_stage_id)
        pending_qty = max(0.0, sent_qty - used_qty)
        if sent_qty <= 1e-9 and used_qty <= 1e-9 and material_id:
            summary_rows.append({
                'material': m,
                'sent_qty': 0.0,
                'used_qty': 0.0,
                'pending_qty': 0.0
            })
            continue
        if sent_qty <= 1e-9 and used_qty <= 1e-9:
            continue
        summary_rows.append({
            'material': m,
            'sent_qty': sent_qty,
            'used_qty': used_qty,
            'pending_qty': pending_qty
        })

    dq = Delivery.query.filter(Delivery.is_void == False)
    if project:
        dq = dq.filter(Delivery.project_id == project.id)
    if stage:
        dq = dq.filter(Delivery.stage_id == stage.id)
    if material_id:
        dq = dq.filter(Delivery.material_id == material_id)
    delivery_rows = dq.order_by(Delivery.created_at.asc(), Delivery.id.asc()).all()

    total_sent = float(sum(float(r.get('sent_qty') or 0.0) for r in summary_rows))
    total_used = float(sum(float(r.get('used_qty') or 0.0) for r in summary_rows))
    total_pending = float(sum(float(r.get('pending_qty') or 0.0) for r in summary_rows))
    pq = PurchaseV2.query.filter(PurchaseV2.is_void == False)
    if material_id:
        pq = pq.filter(PurchaseV2.material_id == material_id)
    total_purchased_qty = float(pq.with_entities(func.coalesce(func.sum(PurchaseV2.quantity), 0.0)).scalar() or 0.0)
    total_purchased_amount = float(pq.with_entities(func.coalesce(func.sum(PurchaseV2.total_amount), 0.0)).scalar() or 0.0)
    sent_of_purchased_pct = float((total_sent / total_purchased_qty) * 100.0) if total_purchased_qty > 1e-9 else 0.0
    used_of_sent_pct = float((total_used / total_sent) * 100.0) if total_sent > 1e-9 else 0.0

    return render_template(
        'purchase_v2_stock.html',
        projects=projects,
        stages=stages,
        materials=(MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.name.asc()).all()),
        summary_rows=summary_rows,
        delivery_rows=delivery_rows,
        selected_project_id=project_id,
        selected_stage_id=stage_id,
        selected_material_id=material_id,
        total_sent=total_sent,
        total_used=total_used,
        total_pending=total_pending,
        total_purchased_qty=total_purchased_qty,
        total_purchased_amount=total_purchased_amount,
        sent_of_purchased_pct=sent_of_purchased_pct,
        used_of_sent_pct=used_of_sent_pct
    )

@app.route('/hdc/purchase-v2/delivered/<int:delivery_id>/void', methods=['POST'])
@login_required
def hdc_purchase_v2_delivery_void(delivery_id):
    row = Delivery.query.get_or_404(delivery_id)
    if row.is_void:
        flash('Delivery already voided.', 'warning')
        return redirect(url_for('hdc_purchase_v2_delivered'))
    reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
    row.is_void = True
    row.void_reason = reason
    row.voided_at = _pkt_now_naive()
    log_action(current_user, 'void', f'{current_user.username.title()} voided delivery #{row.id}: {reason}', 'delivery', row.id)
    db.session.commit()
    flash(f'Delivery #{delivery_id} voided.', 'success')
    return redirect(url_for('hdc_purchase_v2_delivered'))


@app.route('/hdc/purchase-v2/delivered/<int:delivery_id>/edit', methods=['POST'])
@login_required
def hdc_purchase_v2_delivery_edit(delivery_id):
    row = Delivery.query.get_or_404(delivery_id)
    if row.is_void:
        flash('Cannot edit a voided delivery.', 'danger')
        return redirect(url_for('hdc_purchase_v2_delivered'))
    _date_raw = (request.form.get('date') or '').strip()
    try:
        _date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else (row.date or _pkt_today())
    except ValueError:
        _date = row.date or _pkt_today()
    qty = max(0.0, _flt(request.form.get('quantity'), row.quantity))
    if qty <= 0:
        flash('Quantity must be > 0.', 'danger')
        return redirect(url_for('hdc_purchase_v2_delivered'))
    purchase = PurchaseV2.query.get(row.purchase_id)
    if purchase:
        other_delivered = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
            .filter(Delivery.purchase_id == purchase.id, Delivery.is_void == False, Delivery.id != row.id).scalar() or 0.0)
        remaining = max(0.0, float(purchase.quantity or 0.0) - other_delivered)
        if qty > remaining + 1e-9:
            flash(f'Quantity exceeds remaining purchase qty ({remaining:,.2f}).', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
    row.date = _date
    row.quantity = qty
    row.delivery_person = (request.form.get('delivery_person') or row.delivery_person or '').strip()
    row.notes = (request.form.get('notes') or '').strip() or None
    log_action(current_user, 'update', f'{current_user.username.title()} edited delivery #{row.id}', 'delivery', row.id)
    db.session.commit()
    flash(f'Delivery #{delivery_id} updated.', 'success')
    return redirect(url_for('hdc_purchase_v2_delivered'))


@app.route('/hdc/purchase-v2/usage/<int:usage_id>/void', methods=['POST'])
@login_required
def hdc_purchase_v2_usage_void(usage_id):
    row = UsageLogV2.query.get_or_404(usage_id)
    if row.is_void:
        flash('Usage record already voided.', 'warning')
        return redirect(url_for('hdc_purchase_v2_usage_page'))
    reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
    row.is_void = True
    row.void_reason = reason
    row.voided_at = _pkt_now_naive()
    log_action(current_user, 'void', f'{current_user.username.title()} voided usage #{row.id}: {reason}', 'usage', row.id)
    db.session.commit()
    flash(f'Usage #{usage_id} voided.', 'success')
    return redirect(url_for('hdc_purchase_v2_usage_page'))


@app.route('/hdc/purchase-v2/usage/<int:usage_id>/edit', methods=['POST'])
@login_required
def hdc_purchase_v2_usage_edit(usage_id):
    row = UsageLogV2.query.get_or_404(usage_id)
    if row.is_void:
        flash('Cannot edit a voided usage record.', 'danger')
        return redirect(url_for('hdc_purchase_v2_usage_page'))
    _date_raw = (request.form.get('date') or '').strip()
    try:
        _date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else (row.date or _pkt_today())
    except ValueError:
        _date = row.date or _pkt_today()
    qty = max(0.0, _flt(request.form.get('quantity'), row.quantity))
    if qty <= 0:
        flash('Quantity must be > 0.', 'danger')
        return redirect(url_for('hdc_purchase_v2_usage_page'))
    project_id = request.form.get('project_id', type=int) or row.project_id
    stage_id   = request.form.get('stage_id', type=int) or None
    project = Project.query.get(project_id) if project_id else None
    stage = Stage.query.get(stage_id) if stage_id else None
    if not project:
        flash('Valid project is required.', 'danger')
        return redirect(url_for('hdc_purchase_v2_usage_page'))
    if not stage_id:
        flash('Stage is required for strict stock control.', 'danger')
        return redirect(url_for('hdc_purchase_v2_usage_page'))
    if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
        flash('Selected stage does not belong to selected project.', 'danger')
        return redirect(url_for('hdc_purchase_v2_usage_page'))
    if row.purchase_id:
        purchase = PurchaseV2.query.get(row.purchase_id)
        if (not purchase) or purchase.is_void:
            flash('Linked purchase order is missing/voided; cannot edit this usage row.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        available_for_edit = _purchase_v2_available_in_scope_qty(
            purchase.id,
            project.id,
            stage.id if stage else None,
            exclude_usage_id=row.id
        )
        if qty > available_for_edit + 1e-9:
            flash(f'Edited usage exceeds selected purchase order stock in this stage ({available_for_edit:,.2f}).', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        row.cost = float(float(purchase.unit_price or 0.0) * qty)
    else:
        del_q = db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(
            Delivery.material_id == row.material_id,
            Delivery.is_void == False,
            Delivery.project_id == project.id
        )
        use_q = db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0)).filter(
            UsageLogV2.material_id == row.material_id,
            UsageLogV2.is_void == False,
            UsageLogV2.project_id == project.id,
            UsageLogV2.id != row.id
        )
        if stage:
            del_q = del_q.filter(Delivery.stage_id == stage.id)
            use_q = use_q.filter(UsageLogV2.stage_id == stage.id)
        available_for_edit = max(0.0, float(del_q.scalar() or 0.0) - float(use_q.scalar() or 0.0))
        if qty > available_for_edit + 1e-9:
            flash(f'Edited usage exceeds available delivered stock ({available_for_edit:,.2f}).', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        avg_cost = _material_v2_weighted_cost(row.material_id)
        row.cost = float(avg_cost * qty)
    row.date = _date
    row.quantity = qty
    row.project_id = project_id
    row.stage_id = stage_id
    row.notes = (request.form.get('notes') or '').strip() or None
    log_action(current_user, 'update', f'{current_user.username.title()} edited usage #{row.id}', 'usage', row.id)
    db.session.commit()
    flash(f'Usage #{usage_id} updated.', 'success')
    return redirect(url_for('hdc_purchase_v2_usage_page'))


# Estimation defaults
CONCRETE_GRADES = {
    'M15': {'cement': 6.3,  'sand': 0.44, 'aggregate': 0.88},
    'M20': {'cement': 8.0,  'sand': 0.55, 'aggregate': 1.1},
    'M25': {'cement': 9.5,  'sand': 0.45, 'aggregate': 0.9},
    'M30': {'cement': 11.0, 'sand': 0.38, 'aggregate': 0.76},
}
CONCRETE_RATIOS = {
    'M15': (1, 2, 4),
    'M20': (1, 1.5, 3),
    'M25': (1, 1, 2),
    'M30': (1, 0.75, 1.5),
}
DEFAULT_DRY_FACTOR = 1.54
DEFAULT_BAG_CFT = 1.25
DEFAULT_WC_RATIO = 0.5

@app.route('/hdc/estimation', methods=['GET', 'POST'])
@login_required
def hdc_estimation():
    result   = None
    formulas = CustomFormula.query.all()
    tab      = request.form.get('tab', request.args.get('tab','concrete'))
    dim_unit = request.form.get('dim_unit', 'm')
    steel_length_unit = request.form.get('steel_length_unit', 'm')
    diameter_unit = request.form.get('diameter_unit', 'mm')
    dry_factor = _flt(request.form.get('dry_factor'), DEFAULT_DRY_FACTOR)
    bag_cft = _flt(request.form.get('bag_cft'), DEFAULT_BAG_CFT)
    wc_ratio = _flt(request.form.get('wc_ratio'), DEFAULT_WC_RATIO)

    if request.method == 'POST':
        calc_type = request.form.get('calc_type','concrete')
        tab       = calc_type

        if calc_type == 'concrete':
            L = _to_meters(_flt(request.form.get('length')), dim_unit)
            W = _to_meters(_flt(request.form.get('width')), dim_unit)
            H = _to_meters(_flt(request.form.get('height')), dim_unit)
            grade   = request.form.get('grade','M20')
            member  = request.form.get('member_type','Slab')
            vol_m3  = L * W * H
            vol_cft = vol_m3 * 35.3147

            c, s, a = CONCRETE_RATIOS.get(grade, CONCRETE_RATIOS['M20'])
            total_parts = float(c + s + a)
            dry_cft = vol_cft * dry_factor

            cement_cft = dry_cft * (c / total_parts)
            sand_cft = dry_cft * (s / total_parts)
            aggregate_cft = dry_cft * (a / total_parts)
            cement_bags = (cement_cft / bag_cft) if bag_cft else 0.0

            water_liters = None
            if wc_ratio and cement_bags:
                cement_kg = cement_bags * 50.0
                water_liters = cement_kg * wc_ratio

            result  = dict(
                type='Concrete', member=member, grade=grade,
                volume_m3=round(vol_m3,4), volume_cft=round(vol_cft,2),
                dry_factor=round(dry_factor,2), dry_cft=round(dry_cft,2),
                cement_bags=round(cement_bags,1),
                sand_cft=round(sand_cft,0),
                aggregate_cft=round(aggregate_cft,0),
                water_liters=round(water_liters,0) if water_liters is not None else None
            )

        elif calc_type == 'steel':
            dia    = _to_mm(_flt(request.form.get('diameter')), diameter_unit)
            length = _to_meters(_flt(request.form.get('steel_length')), steel_length_unit)
            rate   = _flt(request.form.get('rate_per_kg'))
            weight = (dia**2 / 162) * length
            result = dict(type='Steel', diameter=dia, length=length,
                          weight_kg=round(weight,2),
                          cost=round(weight*rate,2) if rate else None)

        elif calc_type == 'custom':
            fid     = request.form.get('formula_id', type=int)
            formula = CustomFormula.query.get(fid) if fid else None
            if formula:
                var_names = [v.strip() for v in (formula.variables or '').split(',') if v.strip()]
                var_vals  = {v: _flt(request.form.get(f'var_{v}')) for v in var_names}
                try:
                    result = dict(type='Custom', formula_name=formula.name,
                                  expression=formula.expression, variables=var_vals,
                                  result=round(_safe_eval(formula.expression, var_vals),4))
                except Exception as exc:
                    flash(f'Formula error: {exc}', 'danger')
            else:
                flash('Please select a valid formula.', 'warning')

    return render_template('estimation.html',
        result=result, tab=tab,
        dim_unit=dim_unit, steel_length_unit=steel_length_unit, diameter_unit=diameter_unit,
        dry_factor=dry_factor, bag_cft=bag_cft, wc_ratio=wc_ratio,
        grades=list(CONCRETE_GRADES.keys()),
        formulas=formulas,
        member_types=['Slab','Beam','Column','Footing','PCC','Wall'])


# Ã¢â€â‚¬Ã¢â€â‚¬ Project Estimation Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
@app.route('/project-estimation')
@app.route('/hdc/project-estimation')
@login_required
def hdc_project_estimation():
    stage_defs = []
    estimations = Estimation.query.order_by(Estimation.created_at.desc()).all()
    return render_template('project_estimation.html',
        stage_defs=stage_defs, estimations=estimations)

@app.route('/project-estimation/save', methods=['POST'])
@login_required
def hdc_project_estimation_save():
    data = request.get_json(silent=True) or {}
    mode = (data.get('mode') or '').strip()
    rows = _normalize_estimation_rows(data.get('rows'))
    project_meta = data.get('project_meta') if isinstance(data.get('project_meta'), dict) else {}

    if not rows:
        return jsonify(ok=False, error='Add at least one stage row before saving.')
    grand_total = sum(r.get('total', 0) for r in rows)
    if grand_total <= 0:
        return jsonify(ok=False, error='Total cannot be zero. Please enter rates or quantities.')
    for r in rows:
        if (r.get('total', 0) > 0) and not r.get('stage_name'):
            return jsonify(ok=False, error='Stage name is required for rows with value.')

    est_name = project_meta.get('project_code') or f"Estimation {_pkt_now_naive().strftime('%Y-%m-%d %H:%M')}"
    est = Estimation(name=est_name, total_cost=grand_total, status='draft')
    db.session.add(est); db.session.commit()
    for r in rows:
        db.session.add(EstimationStage(
            estimation_id=est.id,
            stage_name=r.get('stage_name',''),
            calc_type=r.get('type','lump_sum'),
            rate=_flt(r.get('rate')),
            quantity=_flt(r.get('quantity')),
            total=_flt(r.get('total'))
        ))
    db.session.commit()

    if mode == 'project':
        p = _create_project_from_estimation(rows, project_meta, estimation_id=est.id)
        est.status = 'finalized'
        est.project_id = p.id
        est.total_cost = grand_total
        est.updated_at = _pkt_now_naive()
        db.session.commit()
        return jsonify(ok=True, project_id=p.id)

    if mode == 'estimation':
        return jsonify(ok=True, message='Estimation saved.', id=est.id)

    return jsonify(ok=False, error='Invalid save mode.')

@app.route('/project-estimation/convert/<est_id>', methods=['POST'])
@login_required
def hdc_project_estimation_convert(est_id):
    est = Estimation.query.get(est_id)
    if not est:
        flash('Estimation not found.', 'danger')
        return redirect(url_for('hdc_project_estimation'))
    if est.status == 'finalized' and est.project_id:
        flash('Estimation already converted.', 'warning')
        return redirect(url_for('hdc_projects'))

    rows = [
        {
            'stage_name': s.stage_name,
            'type': s.calc_type,
            'rate': s.rate,
            'quantity': s.quantity,
            'total': s.total
        }
        for s in est.stages
    ]
    p = _create_project_from_estimation(rows, {'project_code': est.name}, estimation_id=est.id)
    est.status = 'finalized'
    est.project_id = p.id
    est.updated_at = _pkt_now_naive()
    db.session.commit()
    flash(f'Estimation converted to project "{p.name}".', 'success')
    return redirect(url_for('hdc_projects'))


# Ã¢â€â‚¬Ã¢â€â‚¬ Code Generators (API) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
@app.route('/hdc/api/next_project_code')
@login_required
def hdc_api_next_project_code():
    return jsonify(ok=True, code=_next_project_code())

@app.route('/hdc/api/next_worker_code')
@login_required
def hdc_api_next_worker_code():
    return jsonify(ok=True, code=_next_worker_code())

@app.route('/hdc/api/next_office_staff_code')
@login_required
def hdc_api_next_office_staff_code():
    return jsonify(ok=True, code=_next_office_staff_code())

@app.route('/hdc/estimation/formulas', methods=['GET', 'POST'])
@login_required
def hdc_formulas():
    if request.method == 'POST':
        action = request.form.get('action','add')
        if action == 'add':
            f = CustomFormula(
                name=request.form.get('name','').strip(),
                category=request.form.get('category','Custom').strip(),
                expression=request.form.get('expression','').strip(),
                variables=request.form.get('variables','').strip(),
                description=request.form.get('description','').strip())
            try:
                test_vars = {v.strip(): 1.0 for v in f.variables.split(',') if v.strip()}
                _safe_eval(f.expression, test_vars)
                db.session.add(f); db.session.commit()
                flash(f'Formula "{f.name}" added.', 'success')
            except Exception as exc:
                flash(f'Invalid expression: {exc}', 'danger')
        elif action == 'delete':
            fid = request.form.get('formula_id', type=int)
            ff  = CustomFormula.query.get(fid)
            if ff: db.session.delete(ff); db.session.commit(); flash('Formula deleted.', 'success')
        return redirect(url_for('hdc_formulas'))
    return render_template('formulas.html', formulas=CustomFormula.query.all())


# â”€â”€ Reports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/reports')
@login_required
def hdc_reports():
    projects = Project.query.all()
    _apply_aggregated_project_costs(projects)
    workers = Worker.query.order_by(Worker.name).all()
    trade_options = _trade_options()
    materials = Material.query.order_by(Material.name).all()
    stages = Stage.query.order_by(Stage.id).all()
    pid = request.args.get('project_id', type=int)
    selected = Project.query.get(pid) if pid else None
    section = request.args.get('section', 'project')

    worker_project_id = request.args.get('worker_project_id', type=int)
    worker_stage_id = request.args.get('worker_stage_id', type=int)
    worker_worker_id = request.args.get('worker_worker_id', type=int)
    worker_trade = (request.args.get('worker_trade') or '').strip()
    worker_date_from = request.args.get('worker_date_from')
    worker_date_to = request.args.get('worker_date_to')
    # Worker report safety: when project is "All", ignore stale stage filter values.
    if not worker_project_id:
        worker_stage_id = None
    elif worker_stage_id:
        st = Stage.query.get(worker_stage_id)
        if (not st) or int(st.project_id or 0) != int(worker_project_id):
            worker_stage_id = None

    material_project_id = request.args.get('material_project_id', type=int)
    material_stage_id = request.args.get('material_stage_id', type=int)
    material_id = request.args.get('material_id', type=int)
    material_date_from = request.args.get('material_date_from')
    material_date_to = request.args.get('material_date_to')

    time_project_id = request.args.get('time_project_id', type=int)
    time_stage_id = request.args.get('time_stage_id', type=int)
    time_worker_id = request.args.get('time_worker_id', type=int)
    time_worker_name = (request.args.get('time_worker_name') or '').strip()
    time_trade = (request.args.get('time_trade') or '').strip()
    time_date_from = request.args.get('time_date_from')
    time_date_to = request.args.get('time_date_to')

    stage_project_id = request.args.get('stage_project_id', type=int)
    stage_id = request.args.get('stage_id', type=int)
    expense_project_id = request.args.get('expense_project_id', type=int)
    expense_stage_id = request.args.get('expense_stage_id', type=int)
    expense_category = (request.args.get('expense_category') or '').strip()
    expense_date_from = request.args.get('expense_date_from')
    expense_date_to = request.args.get('expense_date_to')

    stage_rows = []
    if selected:
        selected_stages = Stage.query.filter_by(project_id=pid).order_by(Stage.id).all()
        _apply_aggregated_stage_costs(selected_stages)
        for s in selected_stages:
            stage_rows.append({
                'name': s.name,
                'status': s.status,
                'contract_value': s.contract_value,
                'labour': s.stage_labour_cost,
                'materials': s.stage_material_cost,
                'expenses': s.stage_expense_cost,
                'subcontracts': s.stage_subcontract_cost,
                'total_cost': s.stage_total_cost,
                'profit': s.stage_profit,
            })

    # Worker report (summary for all workers, detail for single worker)
    def _apply_worker_filters(q):
        if worker_project_id:
            q = q.filter(TimeEntry.project_id == worker_project_id)
        if worker_stage_id:
            q = q.filter(TimeEntry.stage_id == worker_stage_id)
        if worker_worker_id:
            q = q.filter(TimeEntry.worker_id == worker_worker_id)
        if worker_trade:
            q = q.filter(Worker.role_type.ilike(f"%{worker_trade}%"))
        if worker_date_from:
            q = q.filter(TimeEntry.check_in >= datetime.strptime(worker_date_from, '%Y-%m-%d'))
        if worker_date_to:
            q = q.filter(TimeEntry.check_in < datetime.strptime(worker_date_to, '%Y-%m-%d') + timedelta(days=1))
        return q

    worker_summary_rows = []
    worker_detail_rows = []
    worker_selected = Worker.query.get(worker_worker_id) if worker_worker_id else None
    worker_records = []

    if worker_worker_id:
        wdq = (db.session.query(
                func.date(TimeEntry.check_in).label('work_date'),
                Worker.name.label('worker_name'),
                Project.name.label('project_name'),
                Stage.name.label('stage_name'),
                func.coalesce(func.sum(TimeEntry.hours), 0.0).label('total_hours'),
                func.coalesce(func.sum(TimeEntry.wage_calculated), 0.0).label('total_wage')
              )
              .join(Worker, TimeEntry.worker_id == Worker.id)
              .join(Project, TimeEntry.project_id == Project.id)
              .outerjoin(Stage, TimeEntry.stage_id == Stage.id)
              .filter(TimeEntry.is_void == False))
        wdq = _apply_worker_filters(wdq)
        worker_detail_rows = (wdq.group_by(
                                func.date(TimeEntry.check_in),
                                Worker.name,
                                Project.name,
                                Stage.name
                              )
                              .order_by(func.date(TimeEntry.check_in).desc(), Project.name.asc(), Stage.name.asc())
                              .all())
        worker_grand_days = len({str(r.work_date) for r in worker_detail_rows if r.work_date})
        worker_grand_hours = sum(float(r.total_hours or 0) for r in worker_detail_rows)
        worker_grand_wage = sum(float(r.total_wage or 0) for r in worker_detail_rows)
    else:
        wsq = (db.session.query(
                Worker.id.label('worker_id'),
                Worker.name.label('worker_name'),
                Worker.role_type.label('worker_trade'),
                func.count(func.distinct(func.date(TimeEntry.check_in))).label('days_worked'),
                func.coalesce(func.sum(TimeEntry.hours), 0.0).label('total_hours'),
                func.coalesce(func.sum(TimeEntry.wage_calculated), 0.0).label('total_wage')
              )
              .join(Worker, TimeEntry.worker_id == Worker.id)
              .join(Project, TimeEntry.project_id == Project.id)
              .filter(TimeEntry.is_void == False))
        wsq = _apply_worker_filters(wsq)
        worker_summary_rows = (wsq.group_by(Worker.id, Worker.name, Worker.role_type)
                               .order_by(Worker.name.asc())
                               .all())
        worker_grand_days = sum(int(r.days_worked or 0) for r in worker_summary_rows)
        worker_grand_hours = sum(float(r.total_hours or 0) for r in worker_summary_rows)
        worker_grand_wage = sum(float(r.total_wage or 0) for r in worker_summary_rows)

    worker_total_wage = worker_grand_wage

    # Material report (purchases)
    mq = (db.session.query(Purchase, Material, Project)
          .join(Material, Purchase.material_id == Material.id)
          .join(Project, Purchase.project_id == Project.id))
    if material_project_id:
        mq = mq.filter(Purchase.project_id == material_project_id)
    if material_stage_id:
        mq = mq.filter(Purchase.stage_id == material_stage_id)
    if material_id:
        mq = mq.filter(Purchase.material_id == material_id)
    if material_date_from:
        mq = mq.filter(Purchase.date >= _parse_date(material_date_from))
    if material_date_to:
        mq = mq.filter(Purchase.date <= _parse_date(material_date_to))
    material_records = mq.order_by(Purchase.date.desc()).all()
    material_total_cost = sum(float(pur.total or 0) for pur, _, _ in material_records)

    # Time tracking report
    tq = (db.session.query(TimeEntry, Worker, Project)
          .join(Worker, TimeEntry.worker_id == Worker.id)
          .join(Project, TimeEntry.project_id == Project.id)
          .filter(TimeEntry.is_void == False))
    if time_project_id:
        tq = tq.filter(TimeEntry.project_id == time_project_id)
    if time_stage_id:
        tq = tq.filter(TimeEntry.stage_id == time_stage_id)
    if time_worker_id:
        tq = tq.filter(TimeEntry.worker_id == time_worker_id)
    if time_worker_name:
        tq = tq.filter(Worker.name.ilike(f"%{time_worker_name}%"))
    if time_trade:
        tq = tq.filter(Worker.role_type.ilike(f"%{time_trade}%"))
    if time_date_from:
        tq = tq.filter(TimeEntry.check_in >= datetime.strptime(time_date_from, '%Y-%m-%d'))
    if time_date_to:
        tq = tq.filter(TimeEntry.check_in <= datetime.strptime(time_date_to, '%Y-%m-%d') + timedelta(days=1))
    time_records = tq.order_by(TimeEntry.check_in.desc()).all()
    time_total_hours = sum(float(t.hours or 0) for t, _, _ in time_records)
    time_total_wage = sum(float(t.wage_calculated or 0) for t, _, _ in time_records)

    # Stage cost report
    stage_cost_rows = []
    stage_q = Stage.query
    if stage_project_id:
        stage_q = stage_q.filter(Stage.project_id == stage_project_id)
    if stage_id:
        stage_q = stage_q.filter(Stage.id == stage_id)
    stage_source = stage_q.all()
    _apply_aggregated_stage_costs(stage_source)
    for s in stage_source:
        stage_cost_rows.append({
            'project': s.project.name if s.project else '',
            'stage': s.name,
            'estimated': s.estimated_cost or 0,
            'actual': s.actual_cost or 0,
            'progress': s.progress or 0,
        })

    # Expense breakdown
    eq = Expense.query.join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
    eq = eq.filter(Expense.is_void == False)
    if expense_project_id:
        eq = eq.filter(Expense.project_id == expense_project_id)
    if expense_stage_id:
        eq = eq.filter(Expense.stage_id == expense_stage_id)
    if expense_category:
        eq = eq.filter(ExpenseCategory.name.ilike(f"%{expense_category}%"))
    if expense_date_from:
        eq = eq.filter(Expense.date >= _parse_date(expense_date_from))
    if expense_date_to:
        eq = eq.filter(Expense.date <= _parse_date(expense_date_to))
    expense_records = eq.order_by(Expense.date.desc()).all()
    expense_total = sum(float(e.amount or 0) for e in expense_records)

    return render_template('reports.html',
        projects=projects, workers=workers, materials=materials, stages=stages,
        trade_options=trade_options,
        selected=selected, pid=pid, section=section,
        stage_rows=stage_rows,
        worker_records=worker_records, worker_project_id=worker_project_id,
        worker_stage_id=worker_stage_id, worker_worker_id=worker_worker_id, worker_trade=worker_trade,
        worker_date_from=worker_date_from, worker_date_to=worker_date_to,
        worker_total_wage=worker_total_wage,
        worker_selected=worker_selected,
        worker_summary_rows=worker_summary_rows,
        worker_detail_rows=worker_detail_rows,
        worker_grand_days=worker_grand_days,
        worker_grand_hours=worker_grand_hours,
        worker_grand_wage=worker_grand_wage,
        material_records=material_records, material_project_id=material_project_id,
        material_stage_id=material_stage_id, material_id=material_id,
        material_date_from=material_date_from, material_date_to=material_date_to,
        material_total_cost=material_total_cost,
        time_records=time_records, time_project_id=time_project_id,
        time_stage_id=time_stage_id, time_worker_id=time_worker_id,
        time_worker_name=time_worker_name, time_trade=time_trade,
        time_date_from=time_date_from, time_date_to=time_date_to,
        time_total_hours=time_total_hours, time_total_wage=time_total_wage,
        stage_cost_rows=stage_cost_rows, stage_project_id=stage_project_id, stage_id=stage_id,
        expense_records=expense_records, expense_project_id=expense_project_id,
        expense_stage_id=expense_stage_id, expense_category=expense_category,
        expense_date_from=expense_date_from, expense_date_to=expense_date_to,
        expense_total=expense_total)


@app.route('/hdc/reports/glance')
@login_required
def hdc_reports_glance():
    today = _pkt_today()
    start_30 = today - timedelta(days=29)
    start_today_dt = datetime.combine(today, datetime.min.time())
    end_today_dt = start_today_dt + timedelta(days=1)

    projects = Project.query.order_by(Project.id.desc()).all()
    _apply_aggregated_project_costs(projects)
    stages = Stage.query.order_by(Stage.id.desc()).all()
    _apply_aggregated_stage_costs(stages)
    subcontractors = Subcontractor.query.order_by(Subcontractor.id.desc()).all()
    materials = Material.query.order_by(Material.name.asc(), Material.id.asc()).all()

    total_contract = float(sum(float(p.owner_contract_value or 0.0) for p in projects))
    total_cost = float(sum(float(p.total_cost or 0.0) for p in projects))
    total_received = float(sum(float(p.total_received or 0.0) for p in projects))
    office_expense_total = _office_expense_total()
    total_profit = float(sum(float(p.net_profit or 0.0) for p in projects)) - office_expense_total
    total_receivable = float(sum(float(p.remaining_receivable or 0.0) for p in projects))
    collection_pct = (total_received / total_contract * 100.0) if total_contract > 0 else 0.0

    active_projects = sum(1 for p in projects if (p.status or '').strip().lower() == 'active')
    completed_stages = sum(1 for s in stages if (s.status or '').strip().lower() in ('completed', 'complete'))
    delayed_stages = [
        s for s in stages
        if s.end_date and s.end_date < today and (s.status or '').strip().lower() not in ('completed', 'complete')
    ]
    delayed_stage_rows = sorted(
        delayed_stages,
        key=lambda s: (s.end_date or today, s.id or 0)
    )[:10]
    stage_over_budget_count = sum(
        1 for s in stages
        if float(s.estimated_cost or 0.0) > 0 and float(s.actual_cost or 0.0) > float(s.estimated_cost or 0.0)
    )
    project_budget_overrun_rows = []
    for p in projects:
        budget = float(p.budget_total or 0.0)
        actual = float(p.total_cost or 0.0)
        if budget > 0 and actual > budget:
            project_budget_overrun_rows.append({
                'project': p,
                'budget': budget,
                'actual': actual,
                'overrun': actual - budget
            })
    project_budget_overrun_rows = sorted(project_budget_overrun_rows, key=lambda r: r['overrun'], reverse=True)[:10]

    expense_30 = float(db.session.query(func.coalesce(func.sum(Expense.amount), 0.0))
                       .filter(Expense.date >= start_30, Expense.date <= today, Expense.is_void == False)
                       .scalar() or 0.0)
    sub_pay_30 = float(db.session.query(func.coalesce(func.sum(SubcontractPayment.amount), 0.0))
                       .filter(SubcontractPayment.date >= start_30, SubcontractPayment.date <= today, SubcontractPayment.is_void == False)
                       .scalar() or 0.0)
    labour_cash_30 = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                           .filter(
                               LabourLedger.is_void == False,
                               LabourLedger.entry_type.in_(['payment', 'advance']),
                               LabourLedger.date >= start_30,
                               LabourLedger.date <= today
                           )
                           .scalar() or 0.0)
    material_net_30 = float(db.session.query(func.coalesce(func.sum(Purchase.total), 0.0))
                            .filter(Purchase.date >= start_30, Purchase.date <= today)
                            .scalar() or 0.0)
    material_usage_30_old = float(db.session.query(func.coalesce(func.sum(MaterialUsage.total), 0.0))
                                  .filter(MaterialUsage.used_at >= start_30, MaterialUsage.used_at <= today)
                                  .scalar() or 0.0)
    material_usage_30_v2 = float(db.session.query(func.coalesce(func.sum(UsageLogV2.cost), 0.0))
                                 .filter(UsageLogV2.is_void == False, UsageLogV2.date >= start_30, UsageLogV2.date <= today)
                                 .scalar() or 0.0)
    material_usage_30 = material_usage_30_old + material_usage_30_v2
    received_30 = float(db.session.query(func.coalesce(func.sum(OwnerPayment.amount), 0.0))
                        .filter(OwnerPayment.date >= start_30, OwnerPayment.date <= today, OwnerPayment.is_void == False)
                        .scalar() or 0.0)
    office_expense_30 = float(db.session.query(func.coalesce(func.sum(OfficeExpense.amount), 0.0))
                              .filter(OfficeExpense.is_void == False, OfficeExpense.date >= start_30, OfficeExpense.date <= today)
                              .scalar() or 0.0)
    cash_out_30 = expense_30 + office_expense_30 + sub_pay_30 + labour_cash_30 + material_net_30
    cash_delta_30 = received_30 - cash_out_30

    active_workers = int(Worker.query.filter(Worker.active_status == True).count())
    workers_present_today = int(db.session.query(func.count(func.distinct(TimeEntry.worker_id)))
                                .filter(
                                    TimeEntry.is_void == False,
                                    TimeEntry.check_in >= start_today_dt,
                                    TimeEntry.check_in < end_today_dt
                                )
                                .scalar() or 0)
    work_hours_today = float(db.session.query(func.coalesce(func.sum(TimeEntry.hours), 0.0))
                             .filter(
                                 TimeEntry.is_void == False,
                                 TimeEntry.check_in >= start_today_dt,
                                 TimeEntry.check_in < end_today_dt
                             )
                             .scalar() or 0.0)
    wages_today = float(db.session.query(func.coalesce(func.sum(TimeEntry.wage_calculated), 0.0))
                        .filter(
                            TimeEntry.is_void == False,
                            TimeEntry.check_in >= start_today_dt,
                            TimeEntry.check_in < end_today_dt
                        )
                        .scalar() or 0.0)

    sub_labour_cost_map = {
        int(sid): float(total or 0.0)
        for sid, total in (
            db.session.query(
                SubcontractLabourAttendance.subcontractor_id,
                func.coalesce(func.sum(SubcontractLabourAttendance.total_labour_paid), 0.0)
            )
            .group_by(SubcontractLabourAttendance.subcontractor_id)
            .all()
        ) if sid
    }
    subcontract_total_payable = float(sum(float(s.payable_amount or 0.0) for s in subcontractors))
    subcontract_total_cleared = float(sum(float(s.total_cleared or 0.0) for s in subcontractors))
    subcontract_total_balance = float(sum(float(s.payable_balance or 0.0) for s in subcontractors))
    subcontract_watch_rows = []
    subcontract_loss_count = 0
    for s in subcontractors:
        labour_cost = float(sub_labour_cost_map.get(int(s.id), 0.0))
        pnl = float(s.contract_value or 0.0) - labour_cost
        if pnl < 0:
            subcontract_loss_count += 1
        if float(s.payable_balance or 0.0) > 0 or pnl < 0:
            subcontract_watch_rows.append({
                'sub': s,
                'labour_cost': labour_cost,
                'pnl': pnl
            })
    subcontract_watch_rows = sorted(
        subcontract_watch_rows,
        key=lambda r: (float(r['pnl']), -float(r['sub'].payable_balance or 0.0))
    )[:10]

    stock_map = _material_stock_map()
    low_stock_rows = []
    for m in materials:
        row = stock_map.get(m.id, {})
        low_stock_rows.append({
            'material': m,
            'purchased': float(row.get('purchased', 0.0) or 0.0),
            'used': float(row.get('used', 0.0) or 0.0),
            'remaining': float(row.get('remaining', 0.0) or 0.0)
        })
    low_stock_rows = sorted(low_stock_rows, key=lambda r: float(r['remaining']))[:10]
    out_of_stock_count = sum(1 for r in low_stock_rows if float(r['remaining'] or 0.0) <= 0)

    risk_project_rows = sorted(
        [
            {
                'project': p,
                'profit': float(p.net_profit or 0.0),
                'remaining': float(p.remaining_receivable or 0.0),
                'cost_ratio': ((float(p.total_cost or 0.0) / float(p.owner_contract_value or 1.0)) * 100.0) if float(p.owner_contract_value or 0.0) > 0 else 0.0
            }
            for p in projects
            if float(p.net_profit or 0.0) < 0 or float(p.remaining_receivable or 0.0) > 0
        ],
        key=lambda r: (float(r['profit']), -float(r['remaining']))
    )[:10]

    open_alerts = (Alert.query
                   .filter(Alert.resolved == False)
                   .order_by(Alert.created_at.desc(), Alert.id.desc())
                   .limit(12)
                   .all())

    latest_payroll = PayrollRun.query.order_by(PayrollRun.run_date.desc(), PayrollRun.id.desc()).first()
    latest_payroll_workers = 0
    if latest_payroll:
        latest_payroll_workers = int(db.session.query(func.count(PayrollItem.id))
                                     .filter(PayrollItem.run_id == latest_payroll.id)
                                     .scalar() or 0)

    return render_template(
        'reports_glance.html',
        today=today,
        start_30=start_30,
        active_projects=active_projects,
        project_count=len(projects),
        stage_count=len(stages),
        completed_stages=completed_stages,
        delayed_stages_count=len(delayed_stages),
        stage_over_budget_count=stage_over_budget_count,
        total_contract=total_contract,
        total_cost=total_cost,
        total_received=total_received,
        total_profit=total_profit,
        office_expense_total=office_expense_total,
        total_receivable=total_receivable,
        collection_pct=collection_pct,
        received_30=received_30,
        cash_out_30=cash_out_30,
        cash_delta_30=cash_delta_30,
        expense_30=expense_30,
        office_expense_30=office_expense_30,
        sub_pay_30=sub_pay_30,
        labour_cash_30=labour_cash_30,
        material_net_30=material_net_30,
        material_usage_30=material_usage_30,
        active_workers=active_workers,
        workers_present_today=workers_present_today,
        work_hours_today=work_hours_today,
        wages_today=wages_today,
        subcontract_count=len(subcontractors),
        subcontract_total_payable=subcontract_total_payable,
        subcontract_total_cleared=subcontract_total_cleared,
        subcontract_total_balance=subcontract_total_balance,
        subcontract_loss_count=subcontract_loss_count,
        out_of_stock_count=out_of_stock_count,
        risk_project_rows=risk_project_rows,
        delayed_stage_rows=delayed_stage_rows,
        project_budget_overrun_rows=project_budget_overrun_rows,
        subcontract_watch_rows=subcontract_watch_rows,
        low_stock_rows=low_stock_rows,
        open_alerts=open_alerts,
        latest_payroll=latest_payroll,
        latest_payroll_workers=latest_payroll_workers
    )


@app.route('/hdc/reports/export/profitability')
@login_required
def hdc_export_profitability():
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(['Code','Name','Client','Contract Value','Stage Value','Total Received',
                'Labour Cost','Material Cost','Expense Cost','Subcontract Cost',
                'Total Cost','Gross Margin','Net Profit','Remaining'])
    for p in Project.query.all():
        w.writerow([p.project_code, p.name, p.client or '',
                    p.owner_contract_value, p.stage_contract_value,
                    p.total_received, p.total_labour_cost, p.total_material_cost,
                    p.total_expense_cost, p.total_subcontract_cost,
                    p.total_cost, p.gross_margin, p.net_profit, p.remaining_receivable])
    out.seek(0)
    return Response(out.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=profitability_report.csv'})

@app.route('/hdc/reports/export/salary')
@login_required
def hdc_export_salary():
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(['Date','Worker Code','Worker Name','Role','Project','Stage','Hours','Overtime','Wage'])
    for t, wk, p in (db.session.query(TimeEntry, Worker, Project)
                     .join(Worker, TimeEntry.worker_id == Worker.id)
                     .join(Project, TimeEntry.project_id == Project.id)
                     .filter(TimeEntry.is_void == False)
                     .order_by(TimeEntry.check_in).all()):
        w.writerow([t.check_in.date(), wk.worker_code, wk.name, wk.role_type or '', p.name,
                    t.stage.name if t.stage_id else '',
                    t.hours, t.overtime, t.wage_calculated])
    out.seek(0)
    return Response(out.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=salary_report.csv'})

@app.route('/hdc/reports/export/materials')
@login_required
def hdc_export_materials():
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(['Date','Type','Project','Stage','Material','Unit','Qty','Rate','Total','Supplier','Return Ref','Reason','Approved By'])
    for pur, mat, p in (db.session.query(Purchase, Material, Project)
                        .join(Material, Purchase.material_id == Material.id)
                        .join(Project, Purchase.project_id == Project.id)
                        .order_by(Purchase.date).all()):
        w.writerow([pur.date, (pur.entry_type or 'purchase'), p.name, pur.stage.name if pur.stage_id else '',
                    mat.name, mat.unit, pur.qty, pur.rate, pur.total,
                    pur.supplier_name or '', pur.return_ref or '', pur.return_reason or '', pur.approved_by or ''])
    out.seek(0)
    return Response(out.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=materials_report.csv'})



# â”€â”€ Project Report Exports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _project_report_data(pid):
    """Gather all data needed for project exports."""
    p      = Project.query.get_or_404(pid)
    _apply_aggregated_project_costs([p])
    stages = Stage.query.filter_by(project_id=pid).order_by(Stage.id).all()
    _apply_aggregated_stage_costs(stages)

    attendance = (db.session.query(TimeEntry, Worker)
                  .join(Worker, TimeEntry.worker_id == Worker.id)
                  .filter(TimeEntry.project_id == pid)
                  .filter(TimeEntry.is_void == False)
                  .order_by(TimeEntry.check_in).all())

    expenses = Expense.query.filter_by(project_id=pid, is_void=False).order_by(Expense.date).all()

    purchases = (db.session.query(Purchase, Material)
                 .join(Material, Purchase.material_id == Material.id)
                 .filter(Purchase.project_id == pid)
                 .order_by(Purchase.date).all())

    sub_payments = (db.session.query(SubcontractPayment, Subcontractor)
                    .join(Subcontractor, SubcontractPayment.subcontractor_id == Subcontractor.id)
                    .filter(Subcontractor.project_id == pid, SubcontractPayment.is_void == False)
                    .order_by(SubcontractPayment.date).all())

    owner_payments = (OwnerPayment.query
                      .filter_by(project_id=pid, is_void=False)
                      .order_by(OwnerPayment.date)
                      .all())

    # Per-stage cost rollups (single source of truth: aggregated stage costs).
    stage_costs = {}
    for s in stages:
        stage_costs[s.id] = {
            'labour': float(s.stage_labour_cost or 0.0),
            'materials': float(s.stage_material_cost or 0.0),
            'expenses': float(s.stage_expense_cost or 0.0),
            'subcontracts': float(s.stage_subcontract_cost or 0.0)
        }

    # Totals (single source of truth: aggregated project costs).
    total_labour = float(p.total_labour_cost or 0.0)
    total_materials = float(p.total_material_cost or 0.0)
    total_expenses = float(p.total_expense_cost or 0.0)
    total_subcontracts = float(p.total_subcontract_cost or 0.0)
    total_cost = float(p.total_cost or 0.0)

    contract_value   = p.owner_contract_value
    profit           = contract_value - total_cost

    return dict(
        project=p, stages=stages, stage_costs=stage_costs,
        attendance=attendance, expenses=expenses,
        purchases=purchases, sub_payments=sub_payments,
        owner_payments=owner_payments,
        total_hours=sum(float(att.hours or 0) for att, _ in attendance),
        total_labour=total_labour, total_materials=total_materials,
        total_expenses=total_expenses, total_subcontracts=total_subcontracts,
        total_cost=total_cost, contract_value=contract_value, profit=profit,
    )


@app.route('/hdc/reports/project/<int:pid>/csv')
@login_required
def hdc_project_report_csv(pid):
    d   = _project_report_data(pid)
    p   = d['project']
    out = io.StringIO()
    w   = csv.writer(out)

    w.writerow(['HDC ERP â€“ Project Report', p.name])
    w.writerow(['Client', p.client or ''])
    w.writerow(['Location', p.location or ''])
    w.writerow(['Contract Type', p.contract_type])
    w.writerow(['Contract Value (Rs)', f"{d['contract_value']:,.2f}"])
    w.writerow(['Total Cost (Rs)', f"{d['total_cost']:,.2f}"])
    w.writerow(['Profit (Rs)', f"{d['profit']:,.2f}"])
    w.writerow([])

    # Stage Summary
    w.writerow(['== Stage Summary =='])
    w.writerow(['Stage', 'Status', 'Stage Value (Rs)', 'Labour (Rs)',
                'Materials (Rs)', 'Expenses (Rs)', 'Subcontracts (Rs)'])
    for s in d['stages']:
        sc = d['stage_costs'].get(s.id, {})
        w.writerow([s.name, s.status, f"{s.contract_value:,.2f}",
                    f"{sc.get('labour',0):,.2f}", f"{sc.get('materials',0):,.2f}",
                    f"{sc.get('expenses',0):,.2f}", f"{sc.get('subcontracts',0):,.2f}"])
    w.writerow([])

    # Attendance
    w.writerow(['== Attendance / Labour =='])
    w.writerow(['Date', 'Worker', 'Hours', 'Wage (Rs)', 'Stage', 'Notes'])
    for att, wkr in d['attendance']:
        wage = float(att.wage_calculated or 0)
        stage_name = att.stage.name if att.stage_id else ''
        w.writerow([att.check_in.date(), wkr.name, att.hours, f"{wage:.2f}", stage_name, ''])
    w.writerow([])

    # Expenses
    w.writerow(['== Expenses =='])
    w.writerow(['Date', 'Category', 'Amount (Rs)', 'Stage', 'Remarks'])
    for exp in d['expenses']:
        stage_name = exp.stage.name if exp.stage_id else ''
        w.writerow([exp.date, exp.category, f"{float(exp.amount):,.2f}", stage_name, exp.remarks or ''])
    w.writerow([])

    # Materials
    w.writerow(['== Material Purchases =='])
    w.writerow(['Date', 'Type', 'Material', 'Unit', 'Qty', 'Rate', 'Total (Rs)', 'Stage', 'Supplier', 'Return Ref', 'Reason', 'Approved By'])
    for pur, mat in d['purchases']:
        stage_name = pur.stage.name if pur.stage_id else ''
        w.writerow([pur.date, (pur.entry_type or 'purchase'), mat.name, mat.unit, pur.qty, pur.rate, f"{float(pur.total):,.2f}", stage_name,
                    pur.supplier_name or '', pur.return_ref or '', pur.return_reason or '', pur.approved_by or ''])
    w.writerow([])

    # Subcontract Payments
    w.writerow(['== Subcontract Payments =='])
    w.writerow(['Date', 'Subcontractor', 'Amount (Rs)', 'Stage', 'Notes'])
    for sp, sub in d['sub_payments']:
        stage_name = sub.stage_rel.name if sub.stage_id else ''
        w.writerow([sp.date, sub.name, f"{float(sp.amount):,.2f}", stage_name, sp.notes or ''])
    w.writerow([])

    # Owner Payments
    w.writerow(['== Owner Payments Received =='])
    w.writerow(['Date', 'Amount (Rs)', 'Remarks'])
    for op in d['owner_payments']:
        w.writerow([op.date, f"{float(op.amount):,.2f}", op.remarks or ''])

    out.seek(0)
    fname = f"report_{p.name.replace(' ','_')}.csv"
    return Response(out.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={fname}'})


@app.route('/hdc/reports/project/<int:pid>/xlsx')
@login_required
def hdc_project_report_xlsx(pid):
    d   = _project_report_data(pid)
    p   = d['project']
    wb  = openpyxl.Workbook()

    # â”€â”€ colour palette
    GREEN  = '87AF32'
    DKGREY = '2D2D2D'
    LTGREY = 'F2F2F2'
    WHITE  = 'FFFFFF'
    RED    = 'C0392B'

    def _hdr(ws, row, cols, label, bg=GREEN, fg=WHITE, bold=True, size=11):
        c = ws.cell(row=row, column=cols[0], value=label)
        c.font       = Font(bold=bold, color=fg, size=size)
        c.fill       = PatternFill('solid', fgColor=bg)
        c.alignment  = Alignment(horizontal='center', vertical='center', wrap_text=True)
        if len(cols) > 1:
            ws.merge_cells(start_row=row, start_column=cols[0],
                           end_row=row, end_column=cols[-1])

    def _row(ws, row, values, bg=None, bold=False, number_format=None):
        for col, val in enumerate(values, 1):
            c = ws.cell(row=row, column=col, value=val)
            c.font      = Font(bold=bold)
            c.alignment = Alignment(vertical='center')
            if bg:
                c.fill = PatternFill('solid', fgColor=bg)
            if number_format and isinstance(val, (int, float)):
                c.number_format = number_format

    thin = Side(style='thin', color='CCCCCC')
    def _border(ws, row, ncols):
        for col in range(1, ncols+1):
            ws.cell(row=row, column=col).border = Border(
                top=thin, bottom=thin, left=thin, right=thin)

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Sheet 1 â€“ Summary
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    ws = wb.active
    ws.title = 'Summary'
    ws.column_dimensions['A'].width = 28
    ws.column_dimensions['B'].width = 22
    ws.row_dimensions[1].height = 30

    _hdr(ws, 1, list(range(1, 3)), 'HDC ERP â€” Project Report', size=14)
    _row(ws, 2, ['Project', p.name], bg=LTGREY, bold=True)
    _row(ws, 3, ['Client', p.client or ''])
    _row(ws, 4, ['Location', p.location or ''])
    _row(ws, 5, ['Contract Type', p.contract_type.replace('_', ' ').title()])
    _row(ws, 6, ['Generated On', _pkt_now().strftime('%Y-%m-%d %H:%M')])
    ws.append([])

    _hdr(ws, 8, list(range(1, 3)), 'Financial Summary', bg=DKGREY)
    fin_rows = [
        ('Contract Value (Rs)', d['contract_value']),
        ('Total Labour Cost (Rs)', d['total_labour']),
        ('Total Material Cost (Rs)', d['total_materials']),
        ('Total Expenses (Rs)', d['total_expenses']),
        ('Total Subcontracts (Rs)', d['total_subcontracts']),
        ('Total Cost (Rs)', d['total_cost']),
        ('Profit / Loss (Rs)', d['profit']),
    ]
    for i, (lbl, val) in enumerate(fin_rows, 9):
        bg = LTGREY if i % 2 == 0 else WHITE
        if lbl.startswith('Total Cost'):
            bg = 'FFE0E0'
        if lbl.startswith('Profit'):
            bg = 'D6EDAF' if val >= 0 else 'FFD5D5'
        _row(ws, i, [lbl, val], bg=bg)
        ws.cell(row=i, column=2).number_format = '#,##0.00'
        _border(ws, i, 2)

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Sheet 2 â€“ Stage Breakdown
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    ws2 = wb.create_sheet('Stage Breakdown')
    hdrs = ['Stage', 'Type', 'Status', 'Stage Value (Rs)',
            'Labour (Rs)', 'Materials (Rs)', 'Expenses (Rs)', 'Subcontracts (Rs)', 'Total Cost (Rs)']
    for i, h in enumerate(hdrs, 1):
        c = ws2.cell(row=1, column=i, value=h)
        c.font  = Font(bold=True, color=WHITE)
        c.fill  = PatternFill('solid', fgColor=GREEN)
        c.alignment = Alignment(horizontal='center')
    ws2.row_dimensions[1].height = 20

    col_w = [28, 14, 12, 18, 16, 16, 16, 18, 18]
    for i, w2 in enumerate(col_w, 1):
        ws2.column_dimensions[get_column_letter(i)].width = w2

    for ri, s in enumerate(d['stages'], 2):
        sc    = d['stage_costs'].get(s.id, {})
        labour    = sc.get('labour', 0)
        mats      = sc.get('materials', 0)
        exp_cost  = sc.get('expenses', 0)
        subc      = sc.get('subcontracts', 0)
        stage_tot = labour + mats + exp_cost + subc
        vals = [s.name, s.contract_basis or '', s.status, s.contract_value,
                labour, mats, exp_cost, subc, stage_tot]
        bg = LTGREY if ri % 2 == 0 else WHITE
        for ci, val in enumerate(vals, 1):
            c = ws2.cell(row=ri, column=ci, value=val)
            c.fill = PatternFill('solid', fgColor=bg)
            c.alignment = Alignment(vertical='center')
            if ci >= 4:
                c.number_format = '#,##0.00'
            _border(ws2, ri, len(vals))

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Sheet 3 â€“ Attendance
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    ws3 = wb.create_sheet('Attendance')
    att_hdrs = ['Date', 'Worker', 'Hours', 'Wage (Rs)', 'Stage', 'Notes']
    for i, h in enumerate(att_hdrs, 1):
        c = ws3.cell(row=1, column=i, value=h)
        c.font = Font(bold=True, color=WHITE)
        c.fill = PatternFill('solid', fgColor=GREEN)
    for ci, w3 in enumerate([14, 24, 10, 14, 22, 30], 1):
        ws3.column_dimensions[get_column_letter(ci)].width = w3

    for ri, (att, wkr) in enumerate(d['attendance'], 2):
        wage = float(att.wage_calculated or 0)
        vals = [str(att.check_in.date()), wkr.name, float(att.hours or 0), wage,
                att.stage.name if att.stage_id else '', '']
        bg = LTGREY if ri % 2 == 0 else WHITE
        for ci, val in enumerate(vals, 1):
            c = ws3.cell(row=ri, column=ci, value=val)
            c.fill = PatternFill('solid', fgColor=bg)
            if ci in (3, 4):
                c.number_format = '#,##0.00'

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Sheet 4 â€“ Expenses
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    ws4 = wb.create_sheet('Expenses')
    exp_hdrs = ['Date', 'Category', 'Amount (Rs)', 'Stage', 'Description']
    for i, h in enumerate(exp_hdrs, 1):
        c = ws4.cell(row=1, column=i, value=h)
        c.font = Font(bold=True, color=WHITE)
        c.fill = PatternFill('solid', fgColor=GREEN)
    for ci, w4 in enumerate([14, 20, 16, 22, 36], 1):
        ws4.column_dimensions[get_column_letter(ci)].width = w4

    for ri, exp in enumerate(d['expenses'], 2):
        vals = [str(exp.date), exp.category, float(exp.amount or 0),
                exp.stage.name if exp.stage_id else '', exp.remarks or '']
        bg = LTGREY if ri % 2 == 0 else WHITE
        for ci, val in enumerate(vals, 1):
            c = ws4.cell(row=ri, column=ci, value=val)
            c.fill = PatternFill('solid', fgColor=bg)
            if ci == 3:
                c.number_format = '#,##0.00'

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Sheet 5 â€“ Materials
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    ws5 = wb.create_sheet('Materials')
    mat_hdrs = ['Date', 'Type', 'Material', 'Unit', 'Qty', 'Rate (Rs)', 'Total (Rs)', 'Stage', 'Supplier', 'Return Ref', 'Reason', 'Approved By']
    for i, h in enumerate(mat_hdrs, 1):
        c = ws5.cell(row=1, column=i, value=h)
        c.font = Font(bold=True, color=WHITE)
        c.fill = PatternFill('solid', fgColor=GREEN)
    for ci, w5 in enumerate([14, 12, 24, 10, 10, 14, 16, 22, 18, 14, 20, 18], 1):
        ws5.column_dimensions[get_column_letter(ci)].width = w5

    for ri, (pur, mat) in enumerate(d['purchases'], 2):
        vals = [str(pur.date), (pur.entry_type or 'purchase'), mat.name, mat.unit, float(pur.qty or 0),
                float(pur.rate or 0), float(pur.total or 0),
                pur.stage.name if pur.stage_id else '',
                pur.supplier_name or '',
                pur.return_ref or '',
                pur.return_reason or '',
                pur.approved_by or '']
        bg = LTGREY if ri % 2 == 0 else WHITE
        for ci, val in enumerate(vals, 1):
            c = ws5.cell(row=ri, column=ci, value=val)
            c.fill = PatternFill('solid', fgColor=bg)
            if ci in (5, 6, 7):
                c.number_format = '#,##0.00'

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Sheet 6 â€“ Subcontract Payments
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    ws6 = wb.create_sheet('Subcontract Payments')
    sp_hdrs = ['Date', 'Subcontractor', 'Amount (Rs)', 'Stage', 'Notes']
    for i, h in enumerate(sp_hdrs, 1):
        c = ws6.cell(row=1, column=i, value=h)
        c.font = Font(bold=True, color=WHITE)
        c.fill = PatternFill('solid', fgColor=GREEN)
    for ci, w6 in enumerate([14, 28, 16, 22, 34], 1):
        ws6.column_dimensions[get_column_letter(ci)].width = w6

    for ri, (sp, sub) in enumerate(d['sub_payments'], 2):
        vals = [str(sp.date), sub.name, float(sp.amount or 0),
                sub.stage_rel.name if sub.stage_id else '', sp.notes or '']
        bg = LTGREY if ri % 2 == 0 else WHITE
        for ci, val in enumerate(vals, 1):
            c = ws6.cell(row=ri, column=ci, value=val)
            c.fill = PatternFill('solid', fgColor=bg)
            if ci == 3:
                c.number_format = '#,##0.00'

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Sheet 7 â€“ Owner Payments
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    ws7 = wb.create_sheet('Owner Payments')
    op_hdrs = ['Date', 'Amount (Rs)', 'Remarks']
    for i, h in enumerate(op_hdrs, 1):
        c = ws7.cell(row=1, column=i, value=h)
        c.font = Font(bold=True, color=WHITE)
        c.fill = PatternFill('solid', fgColor=GREEN)
    for ci, w7 in enumerate([14, 16, 40], 1):
        ws7.column_dimensions[get_column_letter(ci)].width = w7

    for ri, op in enumerate(d['owner_payments'], 2):
        vals = [str(op.date), float(op.amount or 0), op.remarks or '']
        bg = LTGREY if ri % 2 == 0 else WHITE
        for ci, val in enumerate(vals, 1):
            c = ws7.cell(row=ri, column=ci, value=val)
            c.fill = PatternFill('solid', fgColor=bg)
            if ci == 2:
                c.number_format = '#,##0.00'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"report_{p.name.replace(' ','_')}.xlsx"
    return Response(buf.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename={fname}'})


@app.route('/hdc/reports/project/<int:pid>/pdf')
@login_required
def hdc_project_report_pdf(pid):
    d = _project_report_data(pid)
    return render_template('project_report_print.html', **d,
                           now=_pkt_now().strftime('%Y-%m-%d %H:%M'))


# â”€â”€ User Management â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/users', methods=['GET', 'POST'])
@login_required
def hdc_users():
    if _admin_only(): return redirect(url_for('hdc_dashboard'))
    if request.method == 'POST':
        action = request.form.get('action','add')
        if action == 'add':
            uname = request.form.get('username','').strip()
            raw_pwd = request.form.get('password', '')
            ok_pwd, pwd_msg = _is_strong_password(raw_pwd)
            if not uname:
                flash('Username is required.', 'warning')
            elif HDCUser.query.filter_by(username=uname).first():
                flash('Username already exists.', 'danger')
            elif not ok_pwd:
                flash(pwd_msg, 'danger')
            else:
                db.session.add(HDCUser(
                    username=uname,
                    password_hash=generate_password_hash(raw_pwd),
                    role=request.form.get('role','manager')))
                db.session.commit()
                flash(f'User "{uname}" created.', 'success')
        elif action == 'delete':
            uid = request.form.get('user_id', type=int)
            u   = HDCUser.query.get(uid)
            if u and u.id != current_user.id:
                db.session.delete(u); db.session.commit()
                flash('User deleted.', 'success')
            else:
                flash("Cannot delete your own account.", 'warning')
        elif action == 'reset_password':
            uid = request.form.get('user_id', type=int)
            u   = HDCUser.query.get(uid)
            if u:
                new_pwd = request.form.get('new_password', '')
                ok_pwd, pwd_msg = _is_strong_password(new_pwd)
                if not ok_pwd:
                    flash(pwd_msg, 'danger')
                else:
                    u.password_hash = generate_password_hash(new_pwd)
                    db.session.commit()
                    flash(f'Password reset for {u.username}.', 'success')
        return redirect(url_for('hdc_users'))
    users = HDCUser.query.order_by(HDCUser.created_at).all()
    return render_template('users.html', users=users)

@app.route('/hdc/event-recorder')
@login_required
def hdc_event_recorder():
    if _admin_only():
        return redirect(url_for('hdc_dashboard'))

    date_from_raw = (request.args.get('date_from') or '').strip()
    date_to_raw = (request.args.get('date_to') or '').strip()
    filter_user_id = request.args.get('user_id', type=int)
    filter_event = (request.args.get('event_type') or '').strip().lower()
    filter_entity = (request.args.get('entity_type') or '').strip().lower()
    show_internal = (request.args.get('show_internal') or '').strip().lower() in ('1', 'true', 'yes', 'on')

    q = ActivityLog.query
    if date_from_raw:
        d1 = _parse_date(date_from_raw, fallback=None)
        if d1:
            q = q.filter(ActivityLog.created_at >= datetime.combine(d1, datetime.min.time()))
    if date_to_raw:
        d2 = _parse_date(date_to_raw, fallback=None)
        if d2:
            q = q.filter(ActivityLog.created_at <= datetime.combine(d2, datetime.max.time()))
    if filter_user_id:
        q = q.filter(ActivityLog.user_id == filter_user_id)
    if filter_event:
        q = q.filter(ActivityLog.action_type == filter_event)
    if filter_entity:
        q = q.filter(ActivityLog.entity_type.ilike(f'%{filter_entity}%'))
    if not show_internal:
        q = q.filter(ActivityLog.entity_type != 'system')

    logs = q.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(600).all()
    users = HDCUser.query.order_by(HDCUser.username.asc()).all()
    event_options = ['create', 'update', 'void', 'login', 'logout', 'payment', 'delivery', 'usage']

    return render_template(
        'event_recorder.html',
        logs=logs,
        users=users,
        event_options=event_options,
        date_from=date_from_raw,
        date_to=date_to_raw,
        filter_user_id=filter_user_id,
        filter_event=filter_event,
        filter_entity=filter_entity,
        show_internal=show_internal
    )


@app.route('/hdc/accounts', methods=['GET', 'POST'])
@login_required
def hdc_accounts():
    if _admin_only():
        return redirect(url_for('hdc_dashboard'))

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action == 'create_account':
            name = (request.form.get('name') or '').strip()
            acc_group = _normalize_account_group(request.form.get('account_group'))
            acc_mode = _normalize_account_mode(request.form.get('account_mode'))
            acc_type = _resolve_account_type(
                group=acc_group,
                mode=acc_mode,
                explicit_type=request.form.get('type')
            )
            opening = _flt(request.form.get('opening_balance'), 0.0)
            row, msg = _create_account(
                name,
                acc_type,
                opening_balance=opening,
                bank_name=request.form.get('bank_name'),
                account_number=request.form.get('account_number'),
                iban=request.form.get('iban'),
                account_mode=acc_mode,
            )
            if not row:
                flash(msg or 'Unable to create account.', 'danger')
            else:
                db.session.commit()
                flash(f'Account created: {row.name}.', 'success')
            return redirect(url_for('hdc_accounts'))

        if action == 'update_account':
            account_id = request.form.get('account_id', type=int)
            row = Account.query.get(account_id) if account_id else None
            if not row or row.is_void:
                flash('Account not found.', 'danger')
                return redirect(url_for('hdc_accounts'))
            nm = _normalize_name_ci(request.form.get('name') or row.name)
            acc_group = _normalize_account_group(request.form.get('account_group') or _account_group_mode_for_row(row)[0])
            acc_mode = _normalize_account_mode(request.form.get('account_mode') or _account_group_mode_for_row(row)[1])
            tp = _resolve_account_type(
                group=acc_group,
                mode=acc_mode,
                explicit_type=request.form.get('type') or row.type
            )
            opening = _flt(request.form.get('opening_balance'), row.opening_balance)
            bank_name = _normalize_name_ci(request.form.get('bank_name') or row.bank_name or '')
            account_number = (request.form.get('account_number') or row.account_number or '').strip()
            iban = (request.form.get('iban') or row.iban or '').strip()
            if tp not in _ACCOUNT_TYPES:
                flash(f'Account type must be one of: {", ".join(_ACCOUNT_TYPES)}.', 'danger')
                return redirect(url_for('hdc_accounts'))
            if acc_mode == 'bank' and ((not bank_name) or (not account_number)):
                flash('Bank name and account number are required for bank accounts.', 'danger')
                return redirect(url_for('hdc_accounts'))
            if acc_mode != 'bank':
                bank_name = ''
                account_number = ''
                iban = ''
            dup = (Account.query
                   .filter(
                       Account.id != row.id,
                       Account.is_void == False,
                       func.lower(func.trim(Account.name)) == nm.lower()
                   ).first())
            if dup:
                flash('Another account with this name already exists.', 'danger')
                return redirect(url_for('hdc_accounts'))
            row.name = nm
            row.type = tp
            row.opening_balance = float(opening or 0.0)
            row.bank_name = bank_name or None
            row.account_number = account_number or None
            row.iban = iban or None
            db.session.commit()
            flash(f'Account updated: {row.name}.', 'success')
            return redirect(url_for('hdc_accounts'))

        if action == 'delete_account':
            account_id = request.form.get('account_id', type=int)
            row = Account.query.get(account_id) if account_id else None
            if not row or row.is_void:
                flash('Account not found.', 'danger')
                return redirect(url_for('hdc_accounts'))
            has_txn = db.session.query(AccountTransaction.id).filter(
                AccountTransaction.is_void == False,
                or_(AccountTransaction.from_account_id == row.id, AccountTransaction.to_account_id == row.id)
            ).first() is not None
            if has_txn:
                row.status = 'inactive'
                row.is_void = True
                db.session.commit()
                flash('Account has transactions, so it was archived instead of deleted.', 'warning')
                return redirect(url_for('hdc_accounts'))
            db.session.delete(row)
            db.session.commit()
            flash('Account deleted.', 'success')
            return redirect(url_for('hdc_accounts'))

        if action == 'suspend_account':
            account_id = request.form.get('account_id', type=int)
            row = Account.query.get(account_id) if account_id else None
            if (not row) or row.is_void:
                flash('Account not found.', 'danger')
                return redirect(url_for('hdc_accounts'))
            row.status = 'inactive'
            db.session.commit()
            flash(f'Account suspended: {row.name}.', 'warning')
            return redirect(url_for('hdc_accounts'))

        if action == 'activate_account':
            account_id = request.form.get('account_id', type=int)
            row = Account.query.get(account_id) if account_id else None
            if (not row) or row.is_void:
                flash('Account not found.', 'danger')
                return redirect(url_for('hdc_accounts'))
            row.status = 'active'
            db.session.commit()
            flash(f'Account activated: {row.name}.', 'success')
            return redirect(url_for('hdc_accounts'))

        if action == 'void_transaction':
            txn_id = request.form.get('transaction_id', type=int)
            reason = (request.form.get('void_reason') or '').strip() or 'Voided from Accounts'
            ok, msg, cnt = _accounts_toggle_transaction_void_state(txn_id, make_void=True, reason=reason)
            if not ok:
                flash(msg or 'Unable to void transaction.', 'danger')
            else:
                flash(f'Transaction voided ({cnt} row{"s" if cnt != 1 else ""}).', 'success')
            return redirect(request.referrer or url_for('hdc_accounts'))

        if action == 'restore_transaction':
            txn_id = request.form.get('transaction_id', type=int)
            ok, msg, cnt = _accounts_toggle_transaction_void_state(txn_id, make_void=False, reason='')
            if not ok:
                flash(msg or 'Unable to restore transaction.', 'danger')
            else:
                flash(f'Transaction restored ({cnt} row{"s" if cnt != 1 else ""}).', 'success')
            return redirect(request.referrer or url_for('hdc_accounts'))

        if action == 'update_transaction':
            txn_id = request.form.get('transaction_id', type=int)
            payload = {
                'date': (request.form.get('date') or '').strip(),
                'type': (request.form.get('type') or '').strip(),
                'amount': request.form.get('amount'),
                'from_account_id': request.form.get('from_account_id'),
                'to_account_id': request.form.get('to_account_id'),
                'executed_by_account_id': request.form.get('executed_by_account_id') or request.form.get('from_account_id'),
                'project_id': request.form.get('project_id'),
                'stage_id': request.form.get('stage_id'),
                'related_entity_type': request.form.get('related_entity_type'),
                'related_entity_id': request.form.get('related_entity_id'),
                'party_name': request.form.get('party_name'),
                'note': request.form.get('note'),
                'reference_id': request.form.get('reference_id'),
            }
            ok, msg, _ = _accounts_update_manual_transaction(txn_id, payload)
            if not ok:
                flash(msg or 'Unable to update transaction.', 'danger')
            else:
                flash('Transaction updated successfully.', 'success')
            return redirect(request.referrer or url_for('hdc_accounts'))

        if action == 'reverse_transaction':
            txn_id = request.form.get('transaction_id', type=int)
            reason = (request.form.get('reverse_reason') or '').strip()
            ok, msg, cnt = _account_reverse_transaction_group(txn_id, reason=reason)
            if not ok:
                flash(msg or 'Unable to create reversal entry.', 'danger')
            else:
                flash(f'Reversal entry posted ({cnt} row{"s" if cnt != 1 else ""}).', 'success')
            return redirect(request.referrer or url_for('hdc_accounts'))

        if action == 'create_transaction':
            req_tx_type = _normalize_account_tx_type(request.form.get('type') or request.form.get('transaction_type'))
            if req_tx_type == 'advance_to_person':
                flash('Advance To Worker is removed from Accounts. Use Worker payment/ledger instead.', 'warning')
                return redirect(url_for('hdc_accounts'))
            payload = {
                'date': (request.form.get('date') or '').strip(),
                'type': request.form.get('type') or request.form.get('transaction_type'),
                'amount': request.form.get('amount'),
                'from_account_id': request.form.get('from_account_id'),
                'to_account_id': request.form.get('to_account_id'),
                'executed_by_account_id': request.form.get('executed_by_account_id'),
                'project_id': request.form.get('project_id'),
                'stage_id': request.form.get('stage_id'),
                'related_entity_type': request.form.get('related_entity_type'),
                'related_entity_id': request.form.get('related_entity_id'),
                'party_name': request.form.get('party_name'),
                'category': request.form.get('category'),
                'note': request.form.get('note'),
                'reference_id': request.form.get('reference_id'),
                'excess_tip_amount': request.form.get('excess_tip_amount'),
                'excess_advance_amount': request.form.get('excess_advance_amount'),
                'settle_shortfall': request.form.get('settle_shortfall'),
                'expense_category_id': request.form.get('expense_category_id'),
                'office_target': request.form.get('office_target'),
                'office_expense_category': request.form.get('office_expense_category'),
            }
            # Duplicate detection is enforced inside _create_accounts_transaction_with_sync
            # using a typed comparison + recent-window check (avoids the string/int mismatch
            # the previous broad query had).
            ok, msg, rows = _create_accounts_transaction_with_sync(payload)
            if not ok:
                flash(msg or 'Unable to create transaction.', 'danger')
            else:
                flash(f'Transaction recorded ({len(rows)} row{"s" if len(rows) != 1 else ""}).', 'success')
                last_id = max([int(getattr(r, 'id', 0) or 0) for r in (rows or [])] or [0])
                if last_id > 0:
                    return redirect(url_for('hdc_accounts', print_txn_id=last_id))
            return redirect(url_for('hdc_accounts'))

    account_id = request.args.get('account_id', type=int)
    account_group = (request.args.get('account_group') or '').strip()
    account_group = (_normalize_account_group(account_group) if account_group else '')
    project_id = request.args.get('project_id', type=int)
    stage_id = request.args.get('stage_id', type=int)
    tx_type = (request.args.get('transaction_type') or request.args.get('type') or '').strip().lower()
    tx_direction = _normalize_account_tx_direction((request.args.get('direction') or '').strip().lower())
    category = (request.args.get('category') or '').strip().lower()
    group_id = (request.args.get('group_id') or '').strip()
    reference_id = (request.args.get('reference_id') or '').strip()
    metric = (request.args.get('metric') or '').strip().lower()
    drill_account_id = request.args.get('drill_account_id', type=int)
    drill_tx_type = (request.args.get('drill_tx_type') or '').strip().lower()
    effective_account_id = drill_account_id or account_id
    effective_tx_type = drill_tx_type or tx_type
    date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
    date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
    show_auto_person = (str(request.args.get('show_auto_person') or '').strip().lower() in ('1', 'true', 'yes', 'on'))
    print_txn_id = request.args.get('print_txn_id', type=int)
    edit_txn_id = request.args.get('edit_txn_id', type=int)

    edit_txn = None
    if edit_txn_id:
        edit_txn = AccountTransaction.query.get(edit_txn_id)
        if not edit_txn or edit_txn.is_void:
            flash('Transaction not found or voided.', 'danger')
            return redirect(url_for('hdc_accounts'))

    accounts = _list_accounts_with_balances()
    form_accounts = _list_accounts_with_balances(include_inactive=True)
    projects_list = Project.query.order_by(Project.name.asc()).all()
    workers_list = Worker.query.order_by(Worker.active_status.desc(), Worker.name.asc()).all()
    office_staff_list = OfficeStaff.query.order_by(OfficeStaff.active_status.desc(), OfficeStaff.name.asc()).all()
    suppliers_list = Supplier.query.filter(Supplier.is_void == False).order_by(Supplier.name.asc()).all()
    subcontractors_list = Subcontractor.query.order_by(Subcontractor.name.asc()).all()
    worker_option_rows = []
    for w in workers_list:
        worker_option_rows.append({
            'id': int(w.id),
            'label': f'{w.worker_code or ("W-" + str(w.id))} | {w.name} | {w.role_type or "Worker"}'
                     + (' [Suspended]' if not bool(getattr(w, 'active_status', True)) else '')
        })
    supplier_option_rows = []
    for s in suppliers_list:
        supplier_option_rows.append({
            'id': int(s.id),
            'label': f'{s.name}'
        })
    subcontractor_option_rows = []
    for s in subcontractors_list:
        code = (s.subcontractor_code or f'SUB-{s.id}')
        stage_name = (s.stage_rel.name if getattr(s, 'stage_rel', None) else '')
        project_name = ''
        project_id_for_option = int(getattr(s, 'project_id', 0) or 0)
        stage_id_for_option = int(getattr(s, 'stage_id', 0) or 0)
        if getattr(s, 'stage_rel', None) and getattr(s.stage_rel, 'project', None):
            project_name = s.stage_rel.project.name or ''
            if not project_id_for_option:
                project_id_for_option = int(getattr(s.stage_rel, 'project_id', 0) or 0)
        scope = ' / '.join([x for x in [project_name, stage_name] if x]).strip()
        subcontractor_option_rows.append({
            'id': int(s.id),
            'label': f'{code} | {s.name}' + (f' | {scope}' if scope else ''),
            'project_id': (project_id_for_option or None),
            'stage_id': (stage_id_for_option or None),
        })
    office_staff_option_rows = []
    for s in office_staff_list:
        snap = _office_staff_ledger_snapshot(s.id)
        pending_amt = max(0.0, float(snap.get('balance') or 0.0))
        office_staff_option_rows.append({
            'id': int(s.id),
            'label': f'{s.staff_code or ("OFF-" + str(s.id))} | {s.name} | Pending: {pending_amt:,.0f}',
        })
    project_owner_names = sorted({
        _normalize_name_ci(p.client or '')
        for p in projects_list
        if _normalize_name_ci(p.client or '')
    })
    project_client_meta = {}
    name_to_client_acc = {}
    for a in (form_accounts or []):
        nm = _normalize_name_ci(a.get('name') or '')
        if not nm:
            continue
        if str(a.get('type') or '').strip().lower() == 'client':
            name_to_client_acc[nm.lower()] = a
    for p in projects_list:
        cn = _normalize_name_ci(getattr(p, 'client', '') or '')
        acc = name_to_client_acc.get(cn.lower()) if cn else None
        project_client_meta[str(int(p.id))] = {
            'project_id': int(p.id),
            'project_name': (p.name or ''),
            'client_name': cn,
            'account_id': (int(acc.get('id')) if acc and acc.get('id') else None),
            'account_label': ((acc.get('name') or '') + ' (Client)' if acc else ''),
            'balance': (float(acc.get('current_balance') or 0.0) if acc else 0.0),
        }
    if not show_auto_person:
        accounts = [a for a in accounts if not (a.get('type') == 'person' and bool(a.get('auto_generated')))]
    inactive_accounts = _list_accounts_with_balances(include_inactive=True)
    inactive_accounts = [a for a in inactive_accounts if str(a.get('status') or '').strip().lower() == 'inactive']
    if not show_auto_person:
        inactive_accounts = [a for a in inactive_accounts if not (a.get('type') == 'person' and bool(a.get('auto_generated')))]
    history_rows = _account_transaction_history(
        account_id=effective_account_id or None,
        account_group=(account_group or None),
        date_from=date_from,
        date_to=date_to,
        tx_direction=(tx_direction or None),
        project_id=project_id or None,
        stage_id=stage_id or None,
        tx_type=(effective_tx_type or None),
        category=(category or None),
        group_id=(group_id or None),
        reference_id=(reference_id or None),
        limit=600
    )
    voided_rows = (AccountTransaction.query
                   .filter(AccountTransaction.is_void == True)
                   .order_by(AccountTransaction.date.desc(), AccountTransaction.id.desc())
                   .limit(200)
                   .all())
    running_map = _account_running_balance_rows(history_rows, account_id=(effective_account_id or None))
    kpis = _account_dashboard_kpis(date_from=date_from, date_to=date_to)
    latest_print_txn = None
    if print_txn_id:
        cand = AccountTransaction.query.get(int(print_txn_id))
        if cand and not cand.is_void:
            latest_print_txn = cand
    subgroup_cards = _account_dashboard_subgroups(metric)
    project_receivable_rows = _running_projects_receivable_rows()
    personal_expense_category_names = sorted({
        (r.name or '').strip()
        for r in PersonalExpenseCategory.query
        .filter(PersonalExpenseCategory.active_status == True)
        .all()
        if (r.name or '').strip()
    }, key=lambda x: x.lower())
    personal_expense_party_names = sorted({
        _normalize_name_ci(r.beneficiary_name or '')
        for r in PersonalExpense.query
        .filter(PersonalExpense.is_void == False)
        .all()
        if _normalize_name_ci(r.beneficiary_name or '')
    }, key=lambda x: x.lower())
    return render_template(
        'accounts.html',
        accounts=accounts,
        form_accounts=form_accounts,
        inactive_accounts=inactive_accounts,
        projects=projects_list,
        project_owner_names=project_owner_names,
        stages=Stage.query.order_by(Stage.project_id.asc(), Stage.name.asc()).all(),
        workers=workers_list,
        suppliers=suppliers_list,
        subcontractors=subcontractors_list,
        worker_option_rows=worker_option_rows,
        supplier_option_rows=supplier_option_rows,
        subcontractor_option_rows=subcontractor_option_rows,
        office_staff_option_rows=office_staff_option_rows,
        project_client_meta=project_client_meta,
        history_rows=history_rows,
        voided_rows=voided_rows,
        running_map=running_map,
        account_kpis=kpis,
        subgroup_cards=subgroup_cards,
        project_receivable_rows=project_receivable_rows,
        active_metric=metric,
        drill_account_id=drill_account_id,
        drill_tx_type=drill_tx_type,
        intent_matrix=_account_intent_field_matrix(),
        txn_types=_ACCOUNT_TXN_TYPES,
        txn_form_options=_ACCOUNT_TXN_FORM_OPTIONS,
        account_types=_ACCOUNT_TYPES,
        txn_categories=_ACCOUNT_TXN_CATEGORIES,
        filter_account_id=account_id,
        filter_account_group=account_group,
        filter_project_id=project_id,
        filter_stage_id=stage_id,
        filter_tx_type=tx_type,
        filter_direction=tx_direction,
        filter_category=category,
        filter_group_id=group_id,
        filter_reference_id=reference_id,
        show_auto_person=show_auto_person,
        filter_date_from=(date_from.isoformat() if date_from else ''),
        filter_date_to=(date_to.isoformat() if date_to else ''),
        latest_print_txn=latest_print_txn,
        account_tx_direction_for_type=_account_tx_direction_for_type,
        expense_categories=ExpenseCategory.query.filter(ExpenseCategory.active_status == True).order_by(ExpenseCategory.name.asc()).all(),
        personal_expense_category_names=personal_expense_category_names,
        personal_expense_party_names=personal_expense_party_names,
        office_expense_categories=[],
        edit_txn=edit_txn,
        today=_pkt_today().isoformat()
    )


def _get_all_office_expense_categories():
    from_expenses = {
        (r.category or '').strip()
        for r in OfficeExpense.query.filter(OfficeExpense.is_void == False).all()
        if (r.category or '').strip()
    }
    from_saved = {
        r.name.strip()
        for r in OfficeExpenseCategory.query.filter(OfficeExpenseCategory.active_status == True).all()
        if (r.name or '').strip()
    }
    return sorted(from_expenses | from_saved, key=lambda x: x.lower())


@app.route('/hdc/api/office_expense_categories', methods=['GET', 'POST'])
@login_required
def hdc_api_office_expense_categories():
    if request.method == 'GET':
        return jsonify({'categories': _get_all_office_expense_categories()})
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        name = _normalize_expense_category_name(data.get('name') or '')
        if not name:
            return jsonify({'ok': False, 'error': 'Category name is required.'}), 400
        existing = OfficeExpenseCategory.query.filter(
            func.lower(OfficeExpenseCategory.name) == name.lower()
        ).first()
        if existing:
            if not existing.active_status:
                existing.active_status = True
                db.session.commit()
        else:
            db.session.add(OfficeExpenseCategory(name=name, active_status=True))
            db.session.commit()
        return jsonify({'ok': True, 'name': name, 'categories': _get_all_office_expense_categories()})


def _get_all_office_expense_category_rows():
    return [
        {
            'id': int(r.id),
            'name': (r.name or '').strip(),
            'active_status': bool(r.active_status)
        }
        for r in OfficeExpenseCategory.query.order_by(OfficeExpenseCategory.name.asc()).all()
    ]


@app.route('/hdc/api/office_expense_categories/<int:category_id>', methods=['PATCH'])
@login_required
def hdc_api_office_expense_category(category_id):
    data = request.get_json(silent=True) or {}
    action = (data.get('action') or '').strip().lower()
    row = OfficeExpenseCategory.query.get_or_404(category_id)
    if action == 'suspend':
        row.active_status = False
        db.session.commit()
        return jsonify({'ok': True, 'categories': _get_all_office_expense_categories()})

    if action == 'activate':
        row.active_status = True
        db.session.commit()
        return jsonify({'ok': True, 'categories': _get_all_office_expense_categories()})

    if action == 'rename':
        name = _normalize_expense_category_name(data.get('name') or '')
        if not name:
            return jsonify({'ok': False, 'error': 'New category name is required.'}), 400
        existing = OfficeExpenseCategory.query.filter(
            func.lower(OfficeExpenseCategory.name) == name.lower(),
            OfficeExpenseCategory.id != int(category_id)
        ).first()
        if existing:
            return jsonify({'ok': False, 'error': 'A category with that name already exists.'}), 400
        row.name = name
        row.active_status = True
        db.session.commit()
        return jsonify({'ok': True, 'name': name, 'categories': _get_all_office_expense_categories()})

    return jsonify({'ok': False, 'error': 'Unsupported category action.'}), 400


# —— Personal Management ——————————————————————————————————————————————————————
@app.route('/hdc/personal-management')
@login_required
def hdc_personal_management():
    personal_expense_total = _personal_expense_total()
    personal_expense_month = _personal_expense_month()
    personal_categories = (PersonalExpenseCategory.query
                           .filter(PersonalExpenseCategory.active_status == True)
                           .order_by(PersonalExpenseCategory.name.asc())
                           .all())
    personal_expense_categories_count = int(PersonalExpenseCategory.query.filter(PersonalExpenseCategory.active_status == True).count() or 0)
    unique_beneficiaries = int(
        db.session.query(func.count(func.distinct(PersonalExpense.beneficiary_name)))
        .filter(PersonalExpense.is_void == False)
        .scalar() or 0
    )
    return render_template(
        'personal_management.html',
        personal_expense_total=personal_expense_total,
        personal_expense_month=personal_expense_month,
        personal_categories=personal_categories,
        personal_expense_categories_count=personal_expense_categories_count,
        unique_beneficiaries=unique_beneficiaries,
        today=_pkt_today().isoformat()
    )

@app.route('/hdc/personal-management/expenses', methods=['GET', 'POST'])
@login_required
def hdc_personal_expenses():
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action == 'add':
            flash('Direct add is disabled. Please record personal expenses from Accounts.', 'warning')
            return redirect(url_for('hdc_personal_expenses'))
    
    # GET: List expenses
    page = request.args.get('page', default=1, type=int) or 1
    page = max(1, page)
    per_page = 25
    date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
    date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
    category_filter = (request.args.get('category') or '').strip()
    
    q = PersonalExpense.query
    if (request.args.get('show_void') or '').strip().lower() != 'yes':
        q = q.filter(PersonalExpense.is_void == False)
    if date_from:
        q = q.filter(PersonalExpense.date >= date_from)
    if date_to:
        q = q.filter(PersonalExpense.date <= date_to)
    if category_filter:
        q = q.filter(func.lower(func.trim(func.coalesce(PersonalExpense.category, ''))) == category_filter.lower())
    
    rows = q.order_by(PersonalExpense.date.desc(), PersonalExpense.id.desc()).paginate(page=page, per_page=per_page)
    categories = PersonalExpenseCategory.query.filter(PersonalExpenseCategory.active_status == True).order_by(PersonalExpenseCategory.name.asc()).all()
    
    return render_template(
        'personal_expenses.html',
        rows=rows,
        categories=categories,
        filter_date_from=(date_from.isoformat() if date_from else ''),
        filter_date_to=(date_to.isoformat() if date_to else ''),
        filter_category=category_filter,
        show_void=(request.args.get('show_void') or '').strip().lower() == 'yes',
        today=_pkt_today().isoformat()
    )

@app.route('/hdc/personal-management/expenses/<int:eid>/void', methods=['GET', 'POST'])
@login_required
def hdc_personal_expense_void(eid):
    row = PersonalExpense.query.get_or_404(eid)
    if request.method == 'POST':
        void_reason = (request.form.get('void_reason') or '').strip()
        if not void_reason:
            flash('Void reason is required.', 'warning')
            return redirect(url_for('hdc_personal_expenses'))
        
        row.is_void = True
        row.void_reason = void_reason
        row.voided_at = _pkt_now_naive()
        _accounts_set_void_by_source('personal_expense', row.id, True)
        db.session.commit()
        flash(f'Personal expense to {row.beneficiary_name} voided.', 'success')
        return redirect(url_for('hdc_personal_expenses'))
    
    return render_template('personal_expense_void.html', row=row)

@app.route('/hdc/personal-management/categories', methods=['GET', 'POST'])
@login_required
def hdc_personal_expense_categories():
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        
        if action == 'add':
            name = (request.form.get('name') or '').strip()
            description = (request.form.get('description') or '').strip()
            
            if not name:
                flash('Category name is required.', 'warning')
                return redirect(url_for('hdc_personal_expense_categories'))
            
            existing = PersonalExpenseCategory.query.filter(
                func.lower(PersonalExpenseCategory.name) == name.lower()
            ).first()
            if existing:
                flash('A category with that name already exists.', 'danger')
                return redirect(url_for('hdc_personal_expense_categories'))
            
            cat = PersonalExpenseCategory(
                name=name,
                description=description,
                active_status=True
            )
            db.session.add(cat)
            db.session.commit()
            flash(f'Personal expense category "{name}" created.', 'success')
            return redirect(url_for('hdc_personal_expense_categories'))
    
    categories = PersonalExpenseCategory.query.order_by(PersonalExpenseCategory.name.asc()).all()
    return render_template('personal_expense_categories.html', categories=categories)

@app.route('/hdc/personal-management/categories/<int:cat_id>/suspend', methods=['POST'])
@login_required
def hdc_personal_category_suspend(cat_id):
    cat = PersonalExpenseCategory.query.get_or_404(cat_id)
    cat.active_status = False
    db.session.commit()
    flash(f'Category "{cat.name}" suspended.', 'success')
    return redirect(url_for('hdc_personal_expense_categories'))

@app.route('/hdc/personal-management/categories/<int:cat_id>/activate', methods=['POST'])
@login_required
def hdc_personal_category_activate(cat_id):
    cat = PersonalExpenseCategory.query.get_or_404(cat_id)
    cat.active_status = True
    db.session.commit()
    flash(f'Category "{cat.name}" activated.', 'success')
    return redirect(url_for('hdc_personal_expense_categories'))


@app.route('/hdc/accounts/<int:account_id>/ledger')
@login_required
def hdc_account_ledger(account_id):
    if _admin_only():
        return redirect(url_for('hdc_dashboard'))
    account = Account.query.get_or_404(account_id)
    if account.is_void:
        abort(404)
    rows = _account_ledger_rows(account.id)
    return render_template(
        'account_ledger.html',
        account=account,
        rows=rows
    )


@app.route('/hdc/accounts/transactions/<int:txn_id>/edit', methods=['GET', 'POST'])
@login_required
def hdc_account_transaction_edit(txn_id):
    if _admin_only():
        return redirect(url_for('hdc_dashboard'))
    row = AccountTransaction.query.get_or_404(txn_id)
    rows = _account_txn_group_rows(row)
    if len(rows) != 1:
        flash('Grouped/split transactions cannot be edited directly. Void/reverse and recreate.', 'warning')
        return redirect(url_for('hdc_accounts_entries'))
    if row.is_void:
        flash('Voided transaction cannot be edited.', 'warning')
        return redirect(url_for('hdc_accounts_entries'))

    if request.method == 'POST':
        payload = {
            'date': (request.form.get('date') or '').strip(),
            'type': (request.form.get('type') or '').strip(),
            'amount': request.form.get('amount'),
            'from_account_id': request.form.get('from_account_id'),
            'to_account_id': request.form.get('to_account_id'),
            'executed_by_account_id': request.form.get('executed_by_account_id') or request.form.get('from_account_id'),
            'project_id': request.form.get('project_id'),
            'stage_id': request.form.get('stage_id'),
            'related_entity_type': request.form.get('related_entity_type'),
            'related_entity_id': request.form.get('related_entity_id'),
            'party_name': request.form.get('party_name'),
            'note': request.form.get('note'),
            'reference_id': request.form.get('reference_id'),
        }
        ok, msg, _ = _accounts_update_manual_transaction(txn_id, payload)
        if not ok:
            flash(msg or 'Unable to update transaction.', 'danger')
            return redirect(url_for('hdc_account_transaction_edit', txn_id=txn_id))
        flash('Transaction updated successfully.', 'success')
        return redirect(url_for('hdc_accounts_entries'))

    return render_template(
        'accounts_transaction_edit.html',
        txn=row,
        accounts=_list_accounts_with_balances(include_inactive=True),
        projects=Project.query.order_by(Project.name.asc()).all(),
        stages=Stage.query.order_by(Stage.project_id.asc(), Stage.name.asc()).all(),
        txn_types=_ACCOUNT_TXN_TYPES
    )


@app.route('/hdc/accounts/entries', methods=['GET', 'POST'])
@login_required
def hdc_accounts_entries():
    if _admin_only():
        return redirect(url_for('hdc_dashboard'))

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action == 'update_transaction':
            txn_id = request.form.get('transaction_id', type=int)
            payload = {
                'date': (request.form.get('date') or '').strip(),
                'type': (request.form.get('type') or '').strip(),
                'amount': request.form.get('amount'),
                'from_account_id': request.form.get('from_account_id'),
                'to_account_id': request.form.get('to_account_id'),
                'executed_by_account_id': request.form.get('executed_by_account_id') or request.form.get('from_account_id'),
                'project_id': request.form.get('project_id'),
                'stage_id': request.form.get('stage_id'),
                'related_entity_type': request.form.get('related_entity_type'),
                'related_entity_id': request.form.get('related_entity_id'),
                'party_name': request.form.get('party_name'),
                'note': request.form.get('note'),
                'reference_id': request.form.get('reference_id'),
            }
            ok, msg, _ = _accounts_update_manual_transaction(txn_id, payload)
            if not ok:
                flash(msg or 'Unable to update transaction.', 'danger')
            else:
                flash('Transaction updated successfully.', 'success')
            return redirect(url_for('hdc_accounts_entries'))
        return redirect(url_for('hdc_accounts_entries'))

    edit_txn_id = request.args.get('edit_txn_id', type=int)
    edit_txn = None
    if edit_txn_id:
        edit_txn = AccountTransaction.query.get(edit_txn_id)
        if not edit_txn or edit_txn.is_void:
            flash('Transaction not found or voided.', 'danger')
            return redirect(url_for('hdc_accounts_entries'))

    account_id = request.args.get('account_id', type=int)
    account_group = (request.args.get('account_group') or '').strip()
    account_group = (_normalize_account_group(account_group) if account_group else '')
    project_id = request.args.get('project_id', type=int)
    stage_id = request.args.get('stage_id', type=int)
    tx_type = (request.args.get('transaction_type') or request.args.get('type') or '').strip().lower()
    tx_direction = _normalize_account_tx_direction((request.args.get('direction') or '').strip().lower())
    category = (request.args.get('category') or '').strip().lower()
    group_id = (request.args.get('group_id') or '').strip()
    reference_id = (request.args.get('reference_id') or '').strip()
    party_name = (request.args.get('party_name') or '').strip()
    worker_id_f = request.args.get('worker_id', type=int)
    date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
    date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
    print_txn_id = request.args.get('print_txn_id', type=int)
    show_auto_person = (str(request.args.get('show_auto_person') or '').strip().lower() in ('1', 'true', 'yes', 'on'))
    page = max(1, request.args.get('page', type=int) or 1)
    try:
        per_page = int(request.args.get('per_page', type=int) or 50)
    except Exception:
        per_page = 50
    per_page = max(10, min(per_page, 200))

    txn_form_options = list(_ACCOUNT_TXN_FORM_OPTIONS)
    if edit_txn and edit_txn.type not in [t['value'] for t in txn_form_options]:
        dir = _account_tx_direction_for_type(edit_txn.type)
        label = edit_txn.type.replace('_', ' ').title()
        txn_form_options.append({'value': edit_txn.type, 'label': label, 'direction': dir})

    accounts = _list_accounts_with_balances()
    form_accounts = _list_accounts_with_balances(include_inactive=True)
    if not show_auto_person:
        accounts = [a for a in accounts if not (a.get('type') == 'person' and bool(a.get('auto_generated')))]
        form_accounts = [a for a in form_accounts if not (a.get('type') == 'person' and bool(a.get('auto_generated')))]

    base_query = _account_transaction_history(
        account_id=(account_id or None),
        account_group=(account_group or None),
        date_from=date_from,
        date_to=date_to,
        tx_direction=(tx_direction or None),
        project_id=(project_id or None),
        stage_id=(stage_id or None),
        tx_type=(tx_type or None),
        category=(category or None),
        group_id=(group_id or None),
        reference_id=(reference_id or None),
        party_name=(party_name or None),
        worker_id=(worker_id_f or None),
        return_query=True
    )
    pg_total_items = base_query.count()
    pg_total_pages = max(1, (pg_total_items + per_page - 1) // per_page) if pg_total_items else 1
    page = min(page, pg_total_pages)
    history_rows = base_query.offset((page - 1) * per_page).limit(per_page).all()
    running_map = _account_running_balance_rows(history_rows, account_id=(account_id or None))
    latest_print_txn = None
    if print_txn_id:
        cand = AccountTransaction.query.get(int(print_txn_id))
        if cand and not cand.is_void:
            latest_print_txn = cand

    workers_for_filter = Worker.query.order_by(Worker.name.asc()).all()

    suppliers_list = Supplier.query.filter(Supplier.is_void == False).order_by(Supplier.name.asc()).all()
    subcontractors_list = Subcontractor.query.order_by(Subcontractor.name.asc()).all()
    office_staff_list = OfficeStaff.query.order_by(OfficeStaff.active_status.desc(), OfficeStaff.name.asc()).all()

    worker_option_rows = []
    for w in workers_for_filter:
        worker_option_rows.append({
            'id': int(w.id),
            'label': f'{w.worker_code or ("W-" + str(w.id))} | {w.name} | {w.role_type or "Worker"}' + (' [Suspended]' if not bool(getattr(w, 'active_status', True)) else '')
        })
    supplier_option_rows = []
    for s in suppliers_list:
        supplier_option_rows.append({
            'id': int(s.id),
            'label': f'{s.name}'
        })
    subcontractor_option_rows = []
    for s in subcontractors_list:
        code = (s.subcontractor_code or f'SUB-{s.id}')
        stage_name = (s.stage_rel.name if getattr(s, 'stage_rel', None) else '')
        project_name = ''
        project_id_for_option = int(getattr(s, 'project_id', 0) or 0)
        stage_id_for_option = int(getattr(s, 'stage_id', 0) or 0)
        if getattr(s, 'stage_rel', None) and getattr(s.stage_rel, 'project', None):
            project_name = s.stage_rel.project.name or ''
            if not project_id_for_option:
                project_id_for_option = int(getattr(s.stage_rel, 'project_id', 0) or 0)
        scope = ' / '.join([x for x in [project_name, stage_name] if x]).strip()
        subcontractor_option_rows.append({
            'id': int(s.id),
            'label': f'{code} | {s.name}' + (f' | {scope}' if scope else ''),
            'project_id': (project_id_for_option or None),
            'stage_id': (stage_id_for_option or None),
        })
    office_staff_option_rows = []
    for s in office_staff_list:
        snap = _office_staff_ledger_snapshot(s.id)
        pending_amt = max(0.0, float(snap.get('balance') or 0.0))
        office_staff_option_rows.append({
            'id': int(s.id),
            'label': f'{s.staff_code or ("OFF-" + str(s.id))} | {s.name} | Pending: {pending_amt:,.0f}',
        })

    pg_query = {
        'account_id': account_id or '',
        'account_group': account_group or '',
        'project_id': project_id or '',
        'stage_id': stage_id or '',
        'transaction_type': tx_type or '',
        'direction': tx_direction or '',
        'category': category or '',
        'group_id': group_id or '',
        'reference_id': reference_id or '',
        'party_name': party_name or '',
        'worker_id': worker_id_f or '',
        'date_from': (date_from.isoformat() if date_from else ''),
        'date_to': (date_to.isoformat() if date_to else ''),
        'show_auto_person': (1 if show_auto_person else 0),
        'per_page': per_page,
    }
    pg_query = {k: v for k, v in pg_query.items() if v not in ('', None)}

    personal_expense_category_names = sorted({
        (r.name or '').strip()
        for r in PersonalExpenseCategory.query
        .filter(PersonalExpenseCategory.active_status == True)
        .all()
        if (r.name or '').strip()
    }, key=lambda x: x.lower())
    personal_expense_party_names = sorted({
        _normalize_name_ci(r.beneficiary_name or '')
        for r in PersonalExpense.query
        .filter(PersonalExpense.is_void == False)
        .all()
        if _normalize_name_ci(r.beneficiary_name or '')
    }, key=lambda x: x.lower())

    project_receivable_rows = _running_projects_receivable_rows()

    project_client_meta = {}
    name_to_client_acc = {}
    for a in (form_accounts or []):
        nm = _normalize_name_ci(a.get('name') or '')
        if not nm:
            continue
        if str(a.get('type') or '').strip().lower() == 'client':
            name_to_client_acc[nm.lower()] = a
    for p in Project.query.order_by(Project.name.asc()).all():
        cn = _normalize_name_ci(getattr(p, 'client', '') or '')
        acc = name_to_client_acc.get(cn.lower()) if cn else None
        project_client_meta[str(int(p.id))] = {
            'project_id': int(p.id),
            'project_name': (p.name or ''),
            'client_name': cn,
            'account_id': (int(acc.get('id')) if acc and acc.get('id') else None),
            'account_label': ((acc.get('name') or '') + ' (Client)' if acc else ''),
            'balance': (float(acc.get('current_balance') or 0.0) if acc else 0.0),
        }

    return render_template(
        'accounts_entries.html',
        accounts=accounts,
        form_accounts=form_accounts,
        projects=Project.query.order_by(Project.name.asc()).all(),
        stages=Stage.query.order_by(Stage.project_id.asc(), Stage.name.asc()).all(),
        workers_for_filter=workers_for_filter,
        history_rows=history_rows,
        running_map=running_map,
        txn_types=_ACCOUNT_TXN_TYPES,
        txn_categories=_ACCOUNT_TXN_CATEGORIES,
        filter_account_id=account_id,
        filter_account_group=account_group,
        filter_project_id=project_id,
        filter_stage_id=stage_id,
        filter_tx_type=tx_type,
        filter_direction=tx_direction,
        filter_category=category,
        filter_group_id=group_id,
        filter_reference_id=reference_id,
        filter_party_name=party_name,
        filter_worker_id=worker_id_f,
        filter_date_from=(date_from.isoformat() if date_from else ''),
        filter_date_to=(date_to.isoformat() if date_to else ''),
        latest_print_txn=latest_print_txn,
        account_tx_direction_for_type=_account_tx_direction_for_type,
        show_auto_person=show_auto_person,
        pg_page=page,
        pg_total_pages=pg_total_pages,
        pg_total_items=pg_total_items,
        pg_per_page=per_page,
        pg_endpoint='hdc_accounts_entries',
        pg_url_kwargs={},
        pg_query=pg_query,
        suppliers=suppliers_list,
        subcontractors=subcontractors_list,
        office_staff=office_staff_list,
        edit_txn=edit_txn,
        txn_form_options=txn_form_options,
        worker_option_rows=worker_option_rows,
        supplier_option_rows=supplier_option_rows,
        subcontractor_option_rows=subcontractor_option_rows,
        office_staff_option_rows=office_staff_option_rows,
        intent_matrix=_account_intent_field_matrix(),
        expense_categories=ExpenseCategory.query.filter(ExpenseCategory.active_status == True).order_by(ExpenseCategory.name.asc()).all(),
        personal_expense_category_names=personal_expense_category_names,
        personal_expense_party_names=personal_expense_party_names,
        project_receivable_rows=project_receivable_rows,
        project_client_meta=project_client_meta,
        office_expense_categories=[],
        today=_pkt_today().isoformat()
    )


@app.route('/hdc/accounts/transactions/find')
@login_required
def hdc_account_transaction_find():
    """Look up any transaction by its internal ID and jump to its receipt.

    Accepts ?id=<n> in either pure numeric form or pasted as 'TXN #123' /
    'RCPT-ATX-00000123' / 'account_txn#123' — anything with digits in it.
    """
    if _admin_only():
        return redirect(url_for('hdc_dashboard'))
    raw = (request.args.get('id') or request.args.get('q') or '').strip()
    if not raw:
        flash('Enter a transaction ID to look up.', 'warning')
        return redirect(request.referrer or url_for('hdc_accounts'))
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if not digits:
        flash(f'Could not find a transaction ID inside "{raw}".', 'warning')
        return redirect(request.referrer or url_for('hdc_accounts'))
    try:
        tid = int(digits)
    except ValueError:
        flash(f'Invalid transaction ID "{raw}".', 'danger')
        return redirect(request.referrer or url_for('hdc_accounts'))
    row = AccountTransaction.query.get(tid)
    if not row:
        flash(f'Transaction #{tid} not found.', 'danger')
        return redirect(request.referrer or url_for('hdc_accounts'))
    return redirect(url_for('hdc_account_transaction_receipt', txn_id=tid))


@app.route('/hdc/accounts/transactions/<int:txn_id>/receipt')
@login_required
def hdc_account_transaction_receipt(txn_id):
    if _admin_only():
        return redirect(url_for('hdc_dashboard'))
    txn = AccountTransaction.query.get_or_404(txn_id)
    auto_print = (str(request.args.get('auto_print') or '').strip().lower() in ('1', 'true', 'yes', 'on'))
    receipt_id = f"RCPT-ATX-{txn.id:08d}"
    account_used = (txn.executed_by_account.name if txn.executed_by_account else '')
    if not account_used:
        account_used = (txn.from_account.name if txn.from_account else '')
    if not account_used:
        account_used = '-'
    party_label = (txn.party_name or '').strip()
    if not party_label:
        et = _normalize_related_entity_type(txn.related_entity_type)
        party_label = _account_entity_label(et, txn.related_entity_id)
    if not party_label:
        party_label = (txn.to_account.name if txn.to_account else '-')

    direction = _account_tx_direction_for_type(txn.type) or 'transfer'

    # All rows that belong to the same accounting group (e.g. internal split,
    # tip / settlement breakdown) so the receipt shows the full audit trail.
    group_rows = _account_txn_group_rows(txn)
    group_total = sum(float(r.amount or 0.0) for r in (group_rows or []) if not bool(getattr(r, 'is_void', False)))
    split_rows = []
    for r in (group_rows or []):
        split_rows.append({
            'id': int(r.id),
            'date': (r.date.isoformat() if r.date else ''),
            'type_label': str(r.type or '').replace('_', ' ').title(),
            'direction': _account_tx_direction_for_type(r.type),
            'from_account': (r.from_account.name if r.from_account else '-'),
            'to_account': (r.to_account.name if r.to_account else '-'),
            'executed_by': (r.executed_by_account.name if r.executed_by_account else '-'),
            'category': (r.category or '-'),
            'amount': float(r.amount or 0.0),
            'is_void': bool(getattr(r, 'is_void', False)),
            'is_current': (int(r.id) == int(txn.id)),
            'note': (r.note or ''),
            'receipt_url': url_for('hdc_account_transaction_receipt', txn_id=r.id),
        })

    recent_entries, recent_scope_label = _account_receipt_recent_entries(txn, limit=5)
    recent_title = 'Last 5 Related Entries'
    if recent_scope_label:
        recent_title = f'Last 5 Entries For: {recent_scope_label}'

    return render_template(
        'transaction_receipt.html',
        company_profile=_receipt_company_profile(),
        receipt_id=receipt_id,
        created_at=(txn.created_at or datetime.combine((txn.date or _pkt_today()), datetime.min.time())),
        txn_date=(txn.date.isoformat() if txn.date else '-'),
        tx_type=str(txn.type or '').replace('_', ' ').title(),
        tx_direction=direction,
        category=(txn.category or '-').replace('_', ' ').title(),
        party_name=party_label,
        related_entity_type=(_normalize_related_entity_type(txn.related_entity_type) or ''),
        related_entity_id=(int(txn.related_entity_id) if txn.related_entity_id else None),
        project_name=(txn.project.name if txn.project else '-'),
        stage_name=(txn.stage.name if txn.stage else '-'),
        account_used=account_used,
        from_account_name=(txn.from_account.name if txn.from_account else '-'),
        to_account_name=(txn.to_account.name if txn.to_account else '-'),
        executed_by_account_name=(txn.executed_by_account.name if txn.executed_by_account else '-'),
        amount=float(txn.amount or 0.0),
        amount_words=_amount_to_words(txn.amount or 0.0),
        note=(txn.note or ''),
        reference_id=(txn.reference_id or f'account_txn#{txn.id}'),
        group_id=(txn.group_id or ''),
        source_type=(txn.source_type or ''),
        source_id=(int(txn.source_id) if txn.source_id else None),
        is_void=bool(getattr(txn, 'is_void', False)),
        txn_id=int(txn.id),
        split_rows=split_rows,
        split_total=float(group_total or 0.0),
        recent_entries=recent_entries,
        recent_entries_title=recent_title,
        back_url=url_for('hdc_accounts'),
        print_label='Print / Save PDF',
        auto_print=auto_print
    )


def _accounts_reconciliation_findings():
    """Forensic scan of the accounts <-> source-row linkage.

    Returns a dict of categorized findings. Each finding lists concrete rows
    so the user can click through and fix the data. The same logic that the
    sync flow uses to write the linkage is mirrored here in reverse.
    """
    # Map: source_type prefix -> (Model, label, list_url_builder, void_attr)
    SOURCE_MAP = {
        'labour_ledger_advance':       (LabourLedger, 'Worker advance'),
        'labour_ledger_payment':       (LabourLedger, 'Worker payment'),
        'labour_ledger_tip':           (LabourLedger, 'Worker tip'),
        'worker_payment':              (LabourLedger, 'Worker payment'),
        'supplier_credit_payment':     (SupplierLedger, 'Supplier payment'),
        'supplier_credit_tip':         (SupplierLedger, 'Supplier tip'),
        'supplier_credit_settlement':  (SupplierLedger, 'Supplier settlement'),
        'subcontract_payment_payment': (SubcontractPayment, 'Subcontract payment'),
        'subcontract_payment_tip':     (SubcontractPayment, 'Subcontract tip'),
        'office_staff_ledger_advance': (OfficeStaffLedger, 'Office staff advance'),
        'office_staff_ledger_payment': (OfficeStaffLedger, 'Office staff payment'),
        'office_staff_ledger_tip':     (OfficeStaffLedger, 'Office staff tip'),
        'office_expense':              (OfficeExpense, 'Office expense'),
        'expense':                     (Expense, 'Expense'),
        'purchase_v2_paid':            (PurchaseV2, 'Purchase (paid)'),
        'owner_payment':               (OwnerPayment, 'Owner payment'),
    }

    def _base_source_type(s):
        s = (s or '').strip().lower()
        if not s:
            return ''
        return s.split(':', 1)[0]

    orphan_txns = []           # AccountTransaction rows pointing to a missing source row
    void_mismatch = []         # source vs txn void state disagrees
    duplicate_active = []      # exact-duplicate active txns
    group_inconsistent = []    # group_id splits with anomalies (mixed void / total mismatch)
    orphan_sources = []        # source rows that should have a paired txn but do not

    # ---- 1. Walk every AccountTransaction with a source linkage ----
    linked = (AccountTransaction.query
              .filter(AccountTransaction.source_id.isnot(None),
                      AccountTransaction.source_type.isnot(None))
              .all())
    for r in linked:
        base = _base_source_type(r.source_type)
        if base not in SOURCE_MAP:
            continue
        Model, label = SOURCE_MAP[base]
        src = Model.query.get(int(r.source_id or 0)) if r.source_id else None
        if not src:
            orphan_txns.append({
                'id': int(r.id),
                'date': (r.date.isoformat() if r.date else ''),
                'amount': float(r.amount or 0.0),
                'type': (r.type or ''),
                'party': (r.party_name or ''),
                'source_type': (r.source_type or ''),
                'source_id': int(r.source_id or 0),
                'label': label,
                'is_void': bool(getattr(r, 'is_void', False)),
                'receipt_url': url_for('hdc_account_transaction_receipt', txn_id=r.id),
            })
            continue
        src_void = bool(getattr(src, 'is_void', False))
        txn_void = bool(getattr(r, 'is_void', False))
        if src_void != txn_void:
            void_mismatch.append({
                'id': int(r.id),
                'date': (r.date.isoformat() if r.date else ''),
                'amount': float(r.amount or 0.0),
                'party': (r.party_name or ''),
                'source_type': (r.source_type or ''),
                'source_id': int(r.source_id or 0),
                'label': label,
                'txn_void': txn_void,
                'src_void': src_void,
                'receipt_url': url_for('hdc_account_transaction_receipt', txn_id=r.id),
            })

    # ---- 2. Duplicate active txns ----
    dup_q = (db.session.query(
                AccountTransaction.date,
                AccountTransaction.type,
                AccountTransaction.amount,
                AccountTransaction.from_account_id,
                AccountTransaction.to_account_id,
                AccountTransaction.related_entity_type,
                AccountTransaction.related_entity_id,
                AccountTransaction.party_name,
                AccountTransaction.reference_id,
                func.count(AccountTransaction.id).label('cnt'),
                func.group_concat(AccountTransaction.id).label('ids')
             )
             .filter(AccountTransaction.is_void == False)
             .group_by(
                AccountTransaction.date,
                AccountTransaction.type,
                AccountTransaction.amount,
                AccountTransaction.from_account_id,
                AccountTransaction.to_account_id,
                AccountTransaction.related_entity_type,
                AccountTransaction.related_entity_id,
                AccountTransaction.party_name,
                AccountTransaction.reference_id,
             )
             .having(func.count(AccountTransaction.id) > 1)
             .all())
    for row in dup_q:
        # Skip rows that share a group_id (legitimate splits like step1/step2).
        ids = [int(x) for x in str(row.ids or '').split(',') if str(x).strip().isdigit()]
        if not ids:
            continue
        member_rows = AccountTransaction.query.filter(AccountTransaction.id.in_(ids)).all()
        gids = {(m.group_id or '') for m in member_rows if (m.group_id or '')}
        # If every duplicate sits in the same group_id, skip — that's a designed split.
        if len(gids) == 1 and all(((m.group_id or '') == next(iter(gids))) for m in member_rows):
            continue
        duplicate_active.append({
            'count': int(row.cnt or 0),
            'ids': ids,
            'date': (row.date.isoformat() if row.date else ''),
            'type': (row.type or ''),
            'amount': float(row.amount or 0.0),
            'party': (row.party_name or ''),
            'reference_id': (row.reference_id or ''),
        })

    # ---- 3. Group split inconsistencies ----
    grp_q = (db.session.query(AccountTransaction.group_id,
                              func.count(AccountTransaction.id).label('cnt'))
             .filter(AccountTransaction.group_id.isnot(None),
                     AccountTransaction.group_id != '')
             .group_by(AccountTransaction.group_id)
             .having(func.count(AccountTransaction.id) > 1)
             .all())
    for g in grp_q:
        members = (AccountTransaction.query
                   .filter(AccountTransaction.group_id == g.group_id)
                   .all())
        active = [m for m in members if not bool(getattr(m, 'is_void', False))]
        voided = [m for m in members if bool(getattr(m, 'is_void', False))]
        if active and voided:
            group_inconsistent.append({
                'group_id': g.group_id,
                'kind': 'mixed_void',
                'detail': f'{len(active)} active and {len(voided)} voided rows in the same group',
                'ids': [int(m.id) for m in members],
            })
            continue
        # For step1+step2 splits, both should equal the headline amount.
        if len(active) == 2:
            amts = sorted({round(float(m.amount or 0.0), 2) for m in active})
            if len(amts) > 1:
                group_inconsistent.append({
                    'group_id': g.group_id,
                    'kind': 'amount_mismatch',
                    'detail': f'Step amounts differ: {amts}',
                    'ids': [int(m.id) for m in active],
                })

    # ---- 4. Source rows without an account txn ----
    def _has_txn_for(prefix, sid):
        like_pat = f'{prefix}:%'
        return db.session.query(AccountTransaction.id).filter(
            AccountTransaction.source_id == int(sid),
            or_(
                func.lower(func.coalesce(AccountTransaction.source_type, '')) == prefix,
                func.lower(func.coalesce(AccountTransaction.source_type, '')).like(like_pat),
            )
        ).first() is not None

    # Worker ledger payments / advances / tips — these always create a cash txn.
    for et, prefix, label in (
        ('payment',    'labour_ledger_payment',    'Worker payment'),
        ('advance',    'labour_ledger_advance',    'Worker advance'),
        ('tip',        'labour_ledger_tip',        'Worker tip'),
    ):
        rows = (LabourLedger.query
                .filter(LabourLedger.entry_type == et,
                        LabourLedger.is_void == False,
                        LabourLedger.amount > 0)
                .all())
        for r in rows:
            if _has_txn_for(prefix, r.id):
                continue
            w = Worker.query.get(int(r.worker_id or 0))
            orphan_sources.append({
                'family': label,
                'source_type': prefix,
                'source_id': int(r.id),
                'date': (r.date.isoformat() if r.date else ''),
                'amount': float(r.amount or 0.0),
                'party': (w.name if w else f'Worker #{r.worker_id}'),
            })

    # Office staff ledger.
    for et, prefix, label in (
        ('payment',    'office_staff_ledger_payment',    'Office staff payment'),
        ('advance',    'office_staff_ledger_advance',    'Office staff advance'),
        ('tip',        'office_staff_ledger_tip',        'Office staff tip'),
    ):
        rows = (OfficeStaffLedger.query
                .filter(OfficeStaffLedger.entry_type == et,
                        OfficeStaffLedger.is_void == False,
                        OfficeStaffLedger.amount > 0)
                .all())
        for r in rows:
            if _has_txn_for(prefix, r.id):
                continue
            s = OfficeStaff.query.get(int(r.staff_id or 0))
            orphan_sources.append({
                'family': label,
                'source_type': prefix,
                'source_id': int(r.id),
                'date': (r.date.isoformat() if r.date else ''),
                'amount': float(r.amount or 0.0),
                'party': (s.name if s else f'Staff #{r.staff_id}'),
            })

    # Owner payments and office expenses always pair to one cash txn.
    for r in OwnerPayment.query.filter(OwnerPayment.is_void == False, OwnerPayment.amount > 0).all():
        if _has_txn_for('owner_payment', r.id):
            continue
        orphan_sources.append({
            'family': 'Owner payment',
            'source_type': 'owner_payment',
            'source_id': int(r.id),
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'party': '-',
        })
    for r in OfficeExpense.query.filter(OfficeExpense.is_void == False, OfficeExpense.amount > 0).all():
        if _has_txn_for('office_expense', r.id):
            continue
        orphan_sources.append({
            'family': 'Office expense',
            'source_type': 'office_expense',
            'source_id': int(r.id),
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'party': '-',
        })

    return {
        'orphan_txns': orphan_txns,
        'void_mismatch': void_mismatch,
        'duplicate_active': duplicate_active,
        'group_inconsistent': group_inconsistent,
        'orphan_sources': orphan_sources,
        'totals': {
            'orphan_txns': len(orphan_txns),
            'void_mismatch': len(void_mismatch),
            'duplicate_active': len(duplicate_active),
            'group_inconsistent': len(group_inconsistent),
            'orphan_sources': len(orphan_sources),
        },
    }


@app.route('/hdc/accounts/reconciliation')
@login_required
def hdc_accounts_reconciliation():
    if _admin_only():
        return redirect(url_for('hdc_dashboard'))
    findings = _accounts_reconciliation_findings()
    total_issues = sum(findings['totals'].values())
    return render_template(
        'accounts_reconciliation.html',
        findings=findings,
        total_issues=total_issues,
        scanned_at=_pkt_now_naive(),
        back_url=url_for('hdc_accounts'),
    )


@app.route('/hdc/accounts/kpi/<string:metric>')
@login_required
def hdc_accounts_kpi_detail(metric):
    if _admin_only():
        return redirect(url_for('hdc_dashboard'))

    date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
    date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
    payable_head = (request.args.get('payable_head') or '').strip()
    spent_head = (request.args.get('spent_head') or '').strip()
    ctx = _account_kpi_detail_context(metric, date_from=date_from, date_to=date_to, payable_head=payable_head, spent_head=spent_head)
    return render_template(
        'accounts_kpi_detail.html',
        **ctx,
        filter_date_from=(date_from.isoformat() if date_from else ''),
        filter_date_to=(date_to.isoformat() if date_to else ''),
        filter_payable_head=payable_head,
        filter_spent_head=spent_head,
    )


@app.route('/hdc/settings', methods=['GET', 'POST'])
@login_required
def hdc_settings():
    if _admin_only(): return redirect(url_for('hdc_dashboard'))
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        try:
            if action == 'create_backup':
                name = _backup_filename()
                dst = os.path.join(_BACKUP_DIR, name)
                _create_backup_zip(dst)
                _cleanup_backup_temp_artifacts()
                flash(f'Backup created: {name} (contains DB + XLSX export).', 'success')
                return redirect(url_for('hdc_settings'))

            if action == 'prune_backups':
                keep_latest = request.form.get('keep_latest', type=int)
                info = _prune_saved_backups(keep_latest=(keep_latest if keep_latest is not None else 10))
                cleaned = _cleanup_backup_temp_artifacts()
                flash(
                    f'Backup cleanup complete. Kept latest {info["keep"]}, deleted {info["deleted"]} backup(s), '
                    f'errors {info["errors"]}. Temp cleaned: {cleaned["files"]} file(s), {cleaned["dirs"]} folder(s).',
                    ('warning' if (info['errors'] or 0) > 0 else 'success')
                )
                return redirect(url_for('hdc_settings'))

            if action == 'run_maintenance':
                pid = request.form.get('project_id', type=int)
                stats = _run_admin_maintenance(project_id=pid or None)
                flash(
                    f'Maintenance completed. Subcontract link updates: {stats["subcontract_link_updates"]}, workers reconciled: {stats["workers_processed"]}.',
                    'success'
                )
                return redirect(url_for('hdc_settings'))

            if action == 'accounts_backfill':
                stats = _run_accounts_backfill()
                flash(
                    f'Accounts backfill complete. Created rows: {stats.get("created", 0)}, '
                    f'Skipped: {stats.get("skipped", 0)}, Errors: {stats.get("errors", 0)}.',
                    ('warning' if (stats.get('errors', 0) or 0) > 0 else 'success')
                )
                return redirect(url_for('hdc_settings'))

            if action == 'restore_saved':
                name = os.path.basename((request.form.get('backup_name') or '').strip())
                if not name:
                    flash('Please select a backup to restore.', 'warning')
                    return redirect(url_for('hdc_settings'))
                src = os.path.join(_BACKUP_DIR, name)
                if not os.path.exists(src):
                    flash('Selected backup file not found.', 'danger')
                    return redirect(url_for('hdc_settings'))
                _restore_from_backup_zip(src)
                logout_user()
                flash('Backup restored successfully. Please login again.', 'success')
                return redirect(url_for('hdc_login'))

            if action == 'delete_backup':
                name = os.path.basename((request.form.get('backup_name') or '').strip())
                if (not name) or (not name.lower().endswith('.zip')):
                    flash('Please select a valid backup file to delete.', 'warning')
                    return redirect(url_for('hdc_settings'))
                src = os.path.join(_BACKUP_DIR, name)
                if not os.path.exists(src):
                    flash('Selected backup file not found.', 'danger')
                    return redirect(url_for('hdc_settings'))
                try:
                    os.remove(src)
                    flash(f'Backup deleted: {name}', 'success')
                except Exception as ex_rm:
                    flash(f'Unable to delete backup: {ex_rm}', 'danger')
                return redirect(url_for('hdc_settings'))

            if action == 'restore_upload':
                up = request.files.get('backup_file')
                if not up or not up.filename:
                    flash('Please choose a backup file (.zip or .db).', 'warning')
                    return redirect(url_for('hdc_settings'))
                ext = os.path.splitext(up.filename)[1].lower()
                with tempfile.TemporaryDirectory(dir=_BACKUP_DIR) as tmpdir:
                    upload_path = os.path.join(tmpdir, 'uploaded' + ext)
                    up.save(upload_path)
                    if ext == '.zip':
                        _restore_from_backup_zip(upload_path)
                    elif ext == '.db':
                        _restore_from_paths(upload_path)
                    else:
                        flash('Unsupported file type. Use .zip or .db', 'danger')
                        return redirect(url_for('hdc_settings'))
                logout_user()
                flash('Backup restored successfully. Please login again.', 'success')
                return redirect(url_for('hdc_login'))

            if action == 'wipe_data':
                selected = request.form.getlist('wipe_targets')
                if 'all' in selected:
                    selected = list(_WIPE_TARGETS.keys())
                selected = [k for k in selected if k in _WIPE_TARGETS]
                confirm_phrase = (request.form.get('wipe_confirm_text') or '').strip().upper()
                confirm_ok = request.form.get('wipe_confirm_check') == '1'
                if not selected:
                    flash('Select at least one data group to wipe.', 'warning')
                    return redirect(url_for('hdc_settings'))
                if confirm_phrase != 'WIPE' or not confirm_ok:
                    flash('Wipe cancelled. Tick confirmation and type WIPE exactly.', 'danger')
                    return redirect(url_for('hdc_settings'))
                users_wiped = 'users' in selected
                info = _wipe_selected_targets(selected)
                flash(
                    f'Wipe completed. Groups: {len(info["targets"])}, tables cleared: {info["tables"]}.',
                    'success'
                )
                if users_wiped:
                    logout_user()
                    flash('Users were wiped. Login with bootstrap admin credentials.', 'warning')
                    return redirect(url_for('hdc_login'))
                return redirect(url_for('hdc_settings'))
        except Exception as ex:
            flash(f'Settings operation failed: {ex}', 'danger')
            return redirect(url_for('hdc_settings'))
    return render_template('settings.html', backups=_list_backups(), wipe_targets=_WIPE_TARGETS)

@app.route('/hdc/settings/backup/<path:filename>/download')
@login_required
def hdc_settings_backup_download(filename):
    if _admin_only(): return redirect(url_for('hdc_dashboard'))
    safe_name = os.path.basename(filename or '')
    if not safe_name.lower().endswith('.zip'):
        abort(404)
    p = os.path.join(_BACKUP_DIR, safe_name)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, as_attachment=True, download_name=safe_name)


@app.route('/hdc/admin/maintenance/run', methods=['POST'])
@login_required
def hdc_admin_maintenance_run():
    if _admin_only():
        return redirect(url_for('hdc_dashboard'))
    pid = request.form.get('project_id', type=int)
    try:
        stats = _run_admin_maintenance(project_id=pid or None)
        flash(
            f'Maintenance completed. Subcontract link updates: {stats["subcontract_link_updates"]}, workers reconciled: {stats["workers_processed"]}.',
            'success'
        )
    except Exception as ex:
        db.session.rollback()
        app.logger.error('Admin maintenance failed: %s', ex)
        flash(f'Maintenance failed: {ex}', 'danger')
    return redirect(request.referrer or url_for('hdc_settings'))


# â”€â”€ API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/hdc/api/formula_vars/<int:fid>')
@login_required
def hdc_api_formula_vars(fid):
    f = CustomFormula.query.get_or_404(fid)
    return jsonify({'variables': [v.strip() for v in (f.variables or '').split(',') if v.strip()],
                    'expression': f.expression, 'name': f.name})

@app.route('/hdc/api/project_stages/<int:pid>')
@login_required
def hdc_api_project_stages(pid):
    stages = Stage.query.filter_by(project_id=pid).order_by(Stage.id).all()
    return jsonify([{'id': s.id, 'name': s.name} for s in stages])


@app.route('/hdc/api/material_stock/<int:mid>')
@login_required
def hdc_api_material_stock(mid):
    project_id = request.args.get('project_id', type=int)
    stage_id = request.args.get('stage_id', type=int)
    remaining = _material_stock_for_scope(mid, project_id=project_id, stage_id=stage_id)
    return jsonify({
        'material_id': mid,
        'project_id': project_id,
        'stage_id': stage_id,
        'remaining': float(remaining or 0.0)
    })


def _normalize_name_ci(v):
    return ' '.join(str(v or '').strip().split())

_MATERIAL_V2_UNITS = (
    'KG', 'G', 'TON', 'LTS', 'ML',
    'METER', 'CM', 'MM', 'FT', 'INCH',
    'SQ FT', 'SQ M', 'CFT',
    'PCS', 'NOS', 'BAG', 'BOX', 'ROLL', 'SHEET',
    'SET', 'PAIR', 'UNIT'
)

def _payload_int(payload, key):
    try:
        return int((payload.get(key) if payload is not None else None) or 0)
    except Exception:
        return 0


_ACCOUNT_TYPES = ('company', 'cash', 'bank', 'person', 'vendor', 'client')
_ACCOUNT_COMPANY_TYPES = ('company', 'cash', 'bank')
_ACCOUNT_PROJECT_FLOW_TYPES = ('client',)
_ACCOUNT_CREDIT_DEBIT_TYPES = ('person', 'vendor')
_ACCOUNT_TXN_TYPES = (
    'transfer',
    'expense_material',
    'expense_wage',
    'expense_subcontractor',
    'office_management_payment',
    'personal_management_payment',
    'expense_general',
    'project_income',
    'party_receipt',
    'client_payment',
    'party_payment',
    'advance_to_person',
    'purchase',
    'payroll',
)
_ACCOUNT_TXN_CATEGORIES = ('salary', 'expense', 'advance', 'personal', 'transfer', 'income', 'purchase', 'payroll')
_ACCOUNT_TXN_TYPE_DEFAULT_CATEGORY = {
    'transfer': 'transfer',
    'expense_material': 'expense',
    'expense_wage': 'expense',
    'expense_subcontractor': 'expense',
    'office_management_payment': 'expense',
    'personal_management_payment': 'personal',
    'expense_general': 'expense',
    'project_income': 'income',
    'party_receipt': 'income',
    'client_payment': 'income',
    'party_payment': 'expense',
    'advance_to_person': 'advance',
    'purchase': 'purchase',
    'payroll': 'payroll',
}
_ACCOUNT_TXN_CATEGORY_DEFAULT_TYPE = {
    'transfer': 'transfer',
    'salary': 'payroll',
    'payroll': 'payroll',
    'purchase': 'purchase',
    'income': 'project_income',
    'advance': 'advance_to_person',
    'expense': 'expense_general',
    'personal': 'personal_management_payment',
}
_ACCOUNT_TXN_FORM_OPTIONS = (
    {'value': 'receive_from_project', 'label': 'Receive from Project', 'direction': 'receive'},
    {'value': 'receive_intra_company', 'label': 'Transfer from Intra Company', 'direction': 'receive'},
    {'value': 'receive_from_credit_debit', 'label': 'Receive from Credit/Debit', 'direction': 'receive'},
    {'value': 'pay_to_project', 'label': 'Pay to Project', 'direction': 'pay'},
    {'value': 'pay_intra_company', 'label': 'Transfer to Intra Company', 'direction': 'pay'},
    {'value': 'pay_to_credit_debit', 'label': 'Pay to Credit/Debit', 'direction': 'pay'},
    {'value': 'purchase', 'label': 'Material Payment (Supplier)', 'direction': 'pay'},
    {'value': 'payroll', 'label': 'Wage / Payroll Payment', 'direction': 'pay'},
    {'value': 'expense_subcontractor', 'label': 'Subcontractor Payment', 'direction': 'pay'},
    {'value': 'office_management_payment', 'label': 'Pay To Office Management', 'direction': 'pay'},
    {'value': 'personal_management_payment', 'label': 'Pay To Personal Management (Party/Purpose)', 'direction': 'pay'},
    {'value': 'expense_general', 'label': 'General Expense', 'direction': 'pay'},
)
_ACCOUNT_TXN_RECEIVE_TYPES = (
    'project_income',
    'party_receipt',
    'client_payment',
)
_ACCOUNT_TXN_PAY_TYPES = (
    'expense_material',
    'expense_wage',
    'expense_subcontractor',
    'office_management_payment',
    'personal_management_payment',
    'expense_general',
    'purchase',
    'payroll',
    'party_payment',
    'advance_to_person',
)
_ACCOUNT_OUTGOING_SCOPE_REQUIRED_TYPES = (
    'expense_general',
)


def _normalize_account_group(v):
    g = (v or '').strip().lower()
    if g in ('project_in_flow', 'project-in-flow', 'project_inflow', 'project inflow'):
        return 'project_in_flow'
    if g in ('credit_debit', 'credit-debit', 'creditdebit', 'liability'):
        return 'credit_debit'
    # Backward compatibility for old "external" naming.
    if g == 'external':
        return 'credit_debit'
    return 'company'


def _normalize_account_mode(v):
    m = (v or '').strip().lower()
    return ('bank' if m == 'bank' else 'cash')


def _normalize_account_tx_type(v):
    tx = (v or '').strip().lower()
    alias_map = {
        'receive_from_project': 'project_income',
        'receive_intra_company': 'transfer',
        'receive_from_credit_debit': 'party_receipt',
        'pay_to_project': 'party_payment',
        'pay_intra_company': 'transfer',
        'pay_to_credit_debit': 'party_payment',
        'personal_payment': 'personal_management_payment',
    }
    return alias_map.get(tx, tx)


def _normalize_account_tx_direction(v):
    d = (v or '').strip().lower()
    if d in ('receive', 'pay'):
        return d
    return ''


def _account_tx_direction_for_type(tx_type):
    tx = _normalize_account_tx_type(tx_type)
    if tx in _ACCOUNT_TXN_RECEIVE_TYPES:
        return 'receive'
    if tx in _ACCOUNT_TXN_PAY_TYPES:
        return 'pay'
    if tx == 'transfer':
        return 'transfer'
    return ''


def _resolve_account_type(group=None, mode=None, explicit_type=None):
    tp = (explicit_type or '').strip().lower()
    if tp in _ACCOUNT_TYPES:
        return tp
    g = _normalize_account_group(group)
    m = _normalize_account_mode(mode)
    if g == 'company':
        return ('bank' if m == 'bank' else 'cash')
    if g == 'project_in_flow':
        return 'client'
    if g == 'credit_debit':
        return 'person'
    return 'person'


def _account_group_mode_for_row(acc_row):
    tp = ((acc_row.type if acc_row else '') or '').strip().lower()
    if tp in _ACCOUNT_COMPANY_TYPES:
        group = 'company'
    elif tp in _ACCOUNT_PROJECT_FLOW_TYPES:
        group = 'project_in_flow'
    else:
        group = 'credit_debit'
    mode = 'bank' if (((acc_row.bank_name or '').strip() and (acc_row.account_number or '').strip()) or tp == 'bank') else 'cash'
    return group, mode


def _account_expected_related_type(tx_type):
    tx = (tx_type or '').strip().lower()
    if tx in ('expense_material', 'purchase'):
        return 'supplier'
    if tx in ('expense_wage', 'payroll', 'advance_to_person'):
        return 'worker'
    if tx == 'expense_subcontractor':
        return 'subcontractor'
    return ''


def _account_requires_scope_tags(tx_type):
    tx = (tx_type or '').strip().lower()
    return tx in _ACCOUNT_OUTGOING_SCOPE_REQUIRED_TYPES


def _account_rows_active():
    return (Account.query
            .filter(Account.is_void == False, func.lower(func.coalesce(Account.status, 'active')) == 'active')
            .order_by(Account.name.asc(), Account.id.asc())
            .all())


def _account_balances_query(include_inactive=False):
    outgoing_sq = (db.session.query(
        AccountTransaction.from_account_id.label('account_id'),
        func.coalesce(func.sum(AccountTransaction.amount), 0.0).label('outgoing_total')
    ).filter(
        AccountTransaction.is_void == False
    ).group_by(AccountTransaction.from_account_id).subquery())
    incoming_sq = (db.session.query(
        AccountTransaction.to_account_id.label('account_id'),
        func.coalesce(func.sum(AccountTransaction.amount), 0.0).label('incoming_total')
    ).filter(
        AccountTransaction.is_void == False,
        AccountTransaction.to_account_id.isnot(None)
    ).group_by(AccountTransaction.to_account_id).subquery())
    q = (db.session.query(
            Account,
            func.coalesce(incoming_sq.c.incoming_total, 0.0).label('incoming_total'),
            func.coalesce(outgoing_sq.c.outgoing_total, 0.0).label('outgoing_total')
        )
        .outerjoin(incoming_sq, incoming_sq.c.account_id == Account.id)
        .outerjoin(outgoing_sq, outgoing_sq.c.account_id == Account.id)
        .filter(Account.is_void == False))
    if not include_inactive:
        q = q.filter(func.lower(func.coalesce(Account.status, 'active')) == 'active')
    return q.order_by(Account.name.asc(), Account.id.asc()).all()


def _account_balance_map():
    rows = _account_balances_query(include_inactive=False)
    mp = {}
    for a, incoming, outgoing in rows:
        opening = float(a.opening_balance or 0.0)
        incoming = float(incoming or 0.0)
        outgoing = float(outgoing or 0.0)
        mp[int(a.id)] = float(opening + incoming - outgoing)
    return mp


def _account_balance(account_id):
    if not account_id:
        return 0.0
    return float(_account_balance_map().get(int(account_id), 0.0) or 0.0)


def _list_accounts_with_balances(include_inactive=False):
    rows = _account_balances_query(include_inactive=include_inactive)
    items = []
    for a, incoming, outgoing in rows:
        opening = float(a.opening_balance or 0.0)
        incoming = float(incoming or 0.0)
        outgoing = float(outgoing or 0.0)
        acc_group, acc_mode = _account_group_mode_for_row(a)
        items.append({
            'id': int(a.id),
            'name': a.name,
            'type': (a.type or '').lower(),
            'account_group': acc_group,
            'account_mode': acc_mode,
            'status': (a.status or 'active').strip().lower(),
            'opening_balance': float(a.opening_balance or 0.0),
            'current_balance': float(opening + incoming - outgoing),
            'bank_name': (a.bank_name or ''),
            'account_number': (a.account_number or ''),
            'iban': (a.iban or ''),
            'auto_generated': bool(a.auto_generated),
            'auto_source': (a.auto_source or ''),
            'created_at': (a.created_at.isoformat(sep=' ') if a.created_at else '')
        })
    return items


def _create_account(name, acc_type, opening_balance=0.0, bank_name='', account_number='', iban='', auto_generated=False, auto_source='', account_mode=''):
    nm = _normalize_name_ci(name)
    tp = (acc_type or '').strip().lower()
    if not nm:
        return None, 'Account name is required.'
    if tp not in _ACCOUNT_TYPES:
        return None, f'Account type must be one of: {", ".join(_ACCOUNT_TYPES)}.'
    bank_name = _normalize_name_ci(bank_name)
    account_number = (account_number or '').strip()
    iban = (iban or '').strip()
    mode = _normalize_account_mode(account_mode or ('bank' if tp == 'bank' else 'cash'))
    if mode == 'bank':
        if not bank_name:
            return None, 'bank_name is required for bank accounts.'
        if not account_number:
            return None, 'account_number is required for bank accounts.'
    else:
        bank_name = ''
        account_number = ''
        iban = ''
    existing = (Account.query
                .filter(Account.is_void == False, func.lower(func.trim(Account.name)) == nm.lower())
                .first())
    if existing:
        return None, 'Account with this name already exists.'
    row = Account(
        name=nm,
        type=tp,
        opening_balance=float(opening_balance or 0.0),
        bank_name=bank_name or None,
        account_number=account_number or None,
        iban=iban or None,
        auto_generated=bool(auto_generated),
        auto_source=((auto_source or '').strip().lower() or None),
        status='active',
        is_void=False,
        created_at=_pkt_now_naive()
    )
    db.session.add(row)
    db.session.flush()
    return row, ''


def _account_get_or_create(name, acc_type='person', opening_balance=0.0, bank_name='', account_number='', iban='', auto_generated=False, auto_source='', account_mode=''):
    nm = _normalize_name_ci(name)
    if not nm:
        return None
    row = (Account.query
           .filter(Account.is_void == False, func.lower(func.trim(Account.name)) == nm.lower())
           .first())
    if row:
        return row
    row, _ = _create_account(
        nm,
        acc_type,
        opening_balance=opening_balance,
        bank_name=bank_name,
        account_number=account_number,
        iban=iban,
        auto_generated=auto_generated,
        auto_source=auto_source,
        account_mode=account_mode
    )
    return row


def _account_txn_source_exists(source_type, source_id):
    if not source_type or not source_id:
        return False
    return db.session.query(AccountTransaction.id).filter(
        AccountTransaction.source_type == str(source_type),
        AccountTransaction.source_id == int(source_id),
        AccountTransaction.is_void == False
    ).first() is not None


def _normalize_account_txn_payload(payload):
    data = payload or {}
    amount = max(0.0, _flt(data.get('amount'), 0.0))
    tx_type = _normalize_account_tx_type((data.get('type') or data.get('transaction_type') or ''))
    from_account_id = _payload_int(data, 'from_account_id')
    to_account_id = _payload_int(data, 'to_account_id')
    executed_by_account_id = _payload_int(data, 'executed_by_account_id') or from_account_id
    project_id = _payload_int(data, 'project_id')
    stage_id = _payload_int(data, 'stage_id')
    related_entity_type = (data.get('related_entity_type') or '').strip().lower()
    related_entity_id = _payload_int(data, 'related_entity_id')
    category = (data.get('category') or '').strip().lower()
    if (not tx_type) and category:
        tx_type = _ACCOUNT_TXN_CATEGORY_DEFAULT_TYPE.get(category, '')
    if tx_type:
        # Keep category deterministic from transaction type to prevent conflicting inputs.
        category = _ACCOUNT_TXN_TYPE_DEFAULT_CATEGORY.get(tx_type, category)
    if tx_type and (not related_entity_type):
        inferred_rel = _account_expected_related_type(tx_type)
        if inferred_rel:
            related_entity_type = inferred_rel
    party_name = _normalize_name_ci(data.get('party_name'))
    note = (data.get('note') or '').strip()
    reference_id = (data.get('reference_id') or '').strip()
    group_id = (data.get('group_id') or '').strip()
    source_type = (data.get('source_type') or '').strip()
    source_id = _payload_int(data, 'source_id')
    _date_raw = (data.get('date') or '').strip()
    tx_date = None
    date_parse_error = False
    try:
        tx_date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else None
    except Exception:
        tx_date = None
        date_parse_error = True
    return {
        'amount': amount,
        'type': tx_type,
        'from_account_id': from_account_id,
        'to_account_id': (to_account_id or None),
        'executed_by_account_id': executed_by_account_id,
        'project_id': (project_id or None),
        'stage_id': (stage_id or None),
        'related_entity_type': related_entity_type,
        'related_entity_id': (related_entity_id or None),
        'category': category,
        'party_name': party_name,
        'note': note,
        'reference_id': reference_id,
        'group_id': group_id,
        'source_type': source_type,
        'source_id': (source_id or None),
        'date': tx_date,
        '_date_raw': _date_raw,
        '_date_parse_error': date_parse_error,
    }


def _validate_account_transaction_payload(norm):
    amount = float(norm.get('amount') or 0.0)
    tx_type = (norm.get('type') or '').strip().lower()
    from_account_id = int(norm.get('from_account_id') or 0)
    to_account_id = int(norm.get('to_account_id') or 0)
    executed_by_account_id = int(norm.get('executed_by_account_id') or 0)
    project_id = int(norm.get('project_id') or 0)
    stage_id = int(norm.get('stage_id') or 0)
    category = (norm.get('category') or '').strip().lower()
    related_entity_type = _normalize_related_entity_type(norm.get('related_entity_type'))
    related_entity_id = int(norm.get('related_entity_id') or 0)
    party_name = _normalize_name_ci(norm.get('party_name'))
    tx_date = norm.get('date')
    date_raw = (norm.get('_date_raw') or '').strip()
    date_parse_error = bool(norm.get('_date_parse_error'))

    if amount <= 0:
        return False, 'Amount must be greater than 0.'
    if not date_raw:
        return False, 'date is required.'
    if date_parse_error:
        return False, 'date must be in YYYY-MM-DD format.'
    if not tx_date:
        return False, 'Valid date is required.'
    if not from_account_id:
        return False, 'from_account_id is required.'
    if not executed_by_account_id:
        return False, 'executed_by_account_id is required.'
    if tx_type not in _ACCOUNT_TXN_TYPES:
        return False, f'type must be one of: {", ".join(_ACCOUNT_TXN_TYPES)}.'
    if category not in _ACCOUNT_TXN_CATEGORIES:
        return False, f'Category must be one of: {", ".join(_ACCOUNT_TXN_CATEGORIES)}.'

    from_account = Account.query.get(from_account_id)
    exec_account = Account.query.get(executed_by_account_id)
    to_account = Account.query.get(to_account_id) if to_account_id else None
    if (not from_account) or from_account.is_void:
        return False, 'Valid from account is required.'
    if (not exec_account) or exec_account.is_void:
        return False, 'Valid executed_by account is required.'
    if to_account_id and ((not to_account) or to_account.is_void):
        return False, 'Selected to account is invalid.'
    if from_account_id and to_account_id and from_account_id == to_account_id:
        return False, 'from_account_id and to_account_id must be different.'
    if project_id and (Project.query.get(project_id) is None):
        return False, 'project_id is invalid.'
    stg = Stage.query.get(stage_id) if stage_id else None
    if stage_id and (not stg):
        return False, 'stage_id is invalid.'
    if stage_id and project_id and int(stg.project_id or 0) != int(project_id):
        return False, 'stage_id does not belong to selected project.'
    if _account_requires_scope_tags(tx_type):
        if not project_id:
            return False, f'project_id is required for outgoing payment type={tx_type}.'
        if not stage_id:
            return False, f'stage_id is required for outgoing payment type={tx_type}.'

    if tx_type in ('transfer', 'advance_to_person', 'project_income', 'client_payment', 'party_receipt'):
        if not to_account_id:
            return False, f'to_account_id is required for type={tx_type}.'
    if tx_type in ('expense_material', 'expense_wage', 'expense_subcontractor', 'expense_general', 'purchase', 'payroll', 'party_payment', 'personal_management_payment'):
        if (not to_account_id) and (not party_name):
            return False, f'to_account_id or party_name is required for type={tx_type}.'
    if tx_type in ('office_management_payment',):
        if (not to_account_id) and (not party_name):
            return False, f'to_account_id or party_name is required for type={tx_type}.'
    if tx_type in ('project_income',) and not project_id:
        return False, f'project_id is required for type={tx_type}.'
    expected_rel = _account_expected_related_type(tx_type)
    if expected_rel:
        if related_entity_type != expected_rel:
            return False, f'{tx_type} requires related_entity_type={expected_rel}.'
        if not related_entity_id:
            return False, f'related_entity_id is required for type={tx_type}.'
    return True, ''


def _check_overdraft_block(pending_rows):
    if not pending_rows:
        return True, ''
    bal = _account_balance_map()
    for r in pending_rows:
        from_id = int(r.get('from_account_id') or 0)
        amount = float(r.get('amount') or 0.0)
        if from_id:
            bal[from_id] = float(bal.get(from_id, 0.0) or 0.0) - amount
        to_id = int(r.get('to_account_id') or 0)
        if to_id:
            bal[to_id] = float(bal.get(to_id, 0.0) or 0.0) + amount
        if from_id and float(bal.get(from_id, 0.0) or 0.0) < -1e-9:
            acc = Account.query.get(from_id)
            tp = (acc.type or '').strip().lower() if acc else ''
            # Strict overdraft prevention is enforced on treasury accounts.
            if tp in ('company', 'cash', 'bank'):
                nm = acc.name if acc else f'#{from_id}'
                return False, f'Insufficient balance in account: {nm}.'
    return True, ''


def _check_overdraft_block_replace(existing_rows, replacement_rows):
    bal = _account_balance_map()
    for r in (existing_rows or []):
        if bool(getattr(r, 'is_void', False)):
            continue
        from_id = int(getattr(r, 'from_account_id', 0) or 0)
        to_id = int(getattr(r, 'to_account_id', 0) or 0)
        amt = float(getattr(r, 'amount', 0.0) or 0.0)
        if from_id:
            bal[from_id] = float(bal.get(from_id, 0.0) or 0.0) + amt
        if to_id:
            bal[to_id] = float(bal.get(to_id, 0.0) or 0.0) - amt
    for r in (replacement_rows or []):
        from_id = int((r.get('from_account_id') if isinstance(r, dict) else 0) or 0)
        to_id = int((r.get('to_account_id') if isinstance(r, dict) else 0) or 0)
        amt = float((r.get('amount') if isinstance(r, dict) else 0.0) or 0.0)
        if from_id:
            bal[from_id] = float(bal.get(from_id, 0.0) or 0.0) - amt
        if to_id:
            bal[to_id] = float(bal.get(to_id, 0.0) or 0.0) + amt
        if from_id and float(bal.get(from_id, 0.0) or 0.0) < -1e-9:
            acc = Account.query.get(from_id)
            tp = (acc.type or '').strip().lower() if acc else ''
            if tp in ('company', 'cash', 'bank'):
                nm = acc.name if acc else f'#{from_id}'
                return False, f'Insufficient balance in account: {nm}.'
    return True, ''


def _build_account_txn_rows(norm):
    amount = float(norm.get('amount') or 0.0)
    tx_type = (norm.get('type') or '').strip().lower()
    from_account_id = int(norm.get('from_account_id') or 0)
    to_account_id = int(norm.get('to_account_id') or 0) if norm.get('to_account_id') else None
    executed_by_account_id = int(norm.get('executed_by_account_id') or 0)
    project_id = int(norm.get('project_id') or 0) if norm.get('project_id') else None
    stage_id = int(norm.get('stage_id') or 0) if norm.get('stage_id') else None
    related_entity_type = (norm.get('related_entity_type') or '').strip().lower() or None
    related_entity_id = int(norm.get('related_entity_id') or 0) if norm.get('related_entity_id') else None
    category = (norm.get('category') or '').strip().lower()
    party_name = _normalize_name_ci(norm.get('party_name'))
    note = norm.get('note') or None
    reference_id = norm.get('reference_id') or None
    base_group = (norm.get('group_id') or '').strip() or uuid4().hex
    tx_date = norm.get('date') or _pkt_today()
    source_type = norm.get('source_type') or None
    source_id = norm.get('source_id') if norm.get('source_id') is not None else None

    # direct or indirect split by executor
    rows = []
    if from_account_id == executed_by_account_id:
        rows.append({
            'date': tx_date,
            'amount': amount,
            'type': tx_type,
            'from_account_id': from_account_id,
            'to_account_id': to_account_id,
            'executed_by_account_id': executed_by_account_id,
            'project_id': project_id,
            'stage_id': stage_id,
            'related_entity_type': related_entity_type,
            'related_entity_id': related_entity_id,
            'party_name': party_name if not to_account_id else (party_name or None),
            'category': category,
            'note': note,
            'reference_id': reference_id,
            'group_id': base_group,
            'source_type': (f'{source_type}:direct' if source_type else None),
            'source_id': source_id,
            'is_void': False,
            'created_at': _pkt_now_naive()
        })
        return rows

    # step 1: source -> executor (internal transfer)
    rows.append({
        'date': tx_date,
        'amount': amount,
        'type': 'transfer',
        'from_account_id': from_account_id,
        'to_account_id': executed_by_account_id,
        'executed_by_account_id': executed_by_account_id,
        'project_id': project_id,
        'stage_id': stage_id,
        'related_entity_type': related_entity_type,
        'related_entity_id': related_entity_id,
        'party_name': None,
        'category': 'transfer',
        'note': (note or 'Auto split (step 1)'),
        'reference_id': reference_id,
        'group_id': base_group,
        'source_type': (f'{source_type}:step1' if source_type else None),
        'source_id': source_id,
        'is_void': False,
        'created_at': _pkt_now_naive()
    })
    # step 2: executor -> final destination
    rows.append({
        'date': tx_date,
        'amount': amount,
        'type': tx_type,
        'from_account_id': executed_by_account_id,
        'to_account_id': to_account_id,
        'executed_by_account_id': executed_by_account_id,
        'project_id': project_id,
        'stage_id': stage_id,
        'related_entity_type': related_entity_type,
        'related_entity_id': related_entity_id,
        'party_name': party_name if not to_account_id else (party_name or None),
        'category': category,
        'note': (note or 'Auto split (step 2)'),
        'reference_id': reference_id,
        'group_id': base_group,
        'source_type': (f'{source_type}:step2' if source_type else None),
        'source_id': source_id,
        'is_void': False,
        'created_at': _pkt_now_naive()
    })
    return rows


def _create_account_transaction(payload, commit=True):
    norm = _normalize_account_txn_payload(payload)
    ok, msg = _validate_account_transaction_payload(norm)
    if not ok:
        return False, msg, []
    if norm.get('source_type') and norm.get('source_id'):
        if _account_txn_source_exists(f"{norm['source_type']}:direct", norm['source_id']) \
           or _account_txn_source_exists(f"{norm['source_type']}:step1", norm['source_id']):
            return False, 'Duplicate source transaction detected.', []
    rows = _build_account_txn_rows(norm)
    ok_bal, msg_bal = _check_overdraft_block(rows)
    if not ok_bal:
        return False, msg_bal, []

    created = []
    try:
        for r in rows:
            row = AccountTransaction(**r)
            db.session.add(row)
            created.append(row)
        db.session.flush()
        if commit:
            db.session.commit()
        return True, '', created
    except Exception as ex:
        db.session.rollback()
        return False, f'Transaction save failed: {ex}', []


def _account_transaction_history(account_id=None, account_group=None, date_from=None, date_to=None, category=None, group_id=None, reference_id=None, limit=500, project_id=None, stage_id=None, tx_type=None, tx_direction=None, party_name=None, worker_id=None, return_query=False):
    q = AccountTransaction.query.filter(AccountTransaction.is_void == False)
    if account_id:
        q = q.filter(or_(AccountTransaction.from_account_id == account_id, AccountTransaction.to_account_id == account_id))
    if account_group:
        grp = _normalize_account_group(account_group)
        g_ids = [int(a.id) for a in Account.query.filter(Account.is_void == False).all() if _account_group_mode_for_row(a)[0] == grp]
        if not g_ids:
            if return_query:
                return q.filter(AccountTransaction.id == 0)
            return []
        q = q.filter(or_(AccountTransaction.from_account_id.in_(g_ids), AccountTransaction.to_account_id.in_(g_ids)))
    if date_from:
        q = q.filter(AccountTransaction.date >= date_from)
    if date_to:
        q = q.filter(AccountTransaction.date <= date_to)
    if category:
        q = q.filter(func.lower(AccountTransaction.category) == str(category).strip().lower())
    if tx_type:
        q = q.filter(func.lower(func.coalesce(AccountTransaction.type, '')) == str(tx_type).strip().lower())
    dir_norm = _normalize_account_tx_direction(tx_direction)
    if dir_norm == 'receive':
        q = q.filter(or_(
            func.lower(func.coalesce(AccountTransaction.type, '')).in_(_ACCOUNT_TXN_RECEIVE_TYPES),
            func.lower(func.coalesce(AccountTransaction.type, '')) == 'transfer'
        ))
    elif dir_norm == 'pay':
        q = q.filter(or_(
            func.lower(func.coalesce(AccountTransaction.type, '')).in_(_ACCOUNT_TXN_PAY_TYPES),
            func.lower(func.coalesce(AccountTransaction.type, '')) == 'transfer'
        ))
    if project_id:
        q = q.filter(AccountTransaction.project_id == int(project_id))
    if stage_id:
        q = q.filter(AccountTransaction.stage_id == int(stage_id))
    if group_id:
        q = q.filter(AccountTransaction.group_id == str(group_id))
    if reference_id:
        q = q.filter(AccountTransaction.reference_id == str(reference_id))
    # Free-text Party / Vendor / Worker name search (substring, case-insensitive).
    if party_name:
        pat = f"%{str(party_name).strip().lower()}%"
        q = q.filter(func.lower(func.coalesce(AccountTransaction.party_name, '')).like(pat))
    # Worker FK filter: rows linked to a specific worker via related_entity, or
    # whose party_name matches the worker's name (legacy rows without FK link).
    if worker_id:
        try:
            w_obj = Worker.query.get(int(worker_id))
        except Exception:
            w_obj = None
        if not w_obj:
            if return_query:
                return q.filter(AccountTransaction.id == 0)
            return []
        wname_pat = f"%{(w_obj.name or '').strip().lower()}%"
        q = q.filter(or_(
            and_(
                func.lower(func.coalesce(AccountTransaction.related_entity_type, '')) == 'worker',
                AccountTransaction.related_entity_id == int(w_obj.id)
            ),
            func.lower(func.coalesce(AccountTransaction.party_name, '')).like(wname_pat),
        ))
    q = q.order_by(AccountTransaction.date.desc(), AccountTransaction.id.desc())
    if return_query:
        return q
    rows = q.limit(max(1, min(int(limit or 500), 2000))).all()
    return rows


def _account_txn_to_dict(r):
    return {
        'id': int(r.id),
        'date': (r.date.isoformat() if r.date else ''),
        'amount': float(r.amount or 0.0),
        'type': (r.type or ''),
        'from_account_id': (int(r.from_account_id) if r.from_account_id else None),
        'from_account_name': (r.from_account.name if r.from_account else ''),
        'to_account_id': (int(r.to_account_id) if r.to_account_id else None),
        'to_account_name': (r.to_account.name if r.to_account else ''),
        'executed_by_account_id': (int(r.executed_by_account_id) if r.executed_by_account_id else None),
        'executed_by_account_name': (r.executed_by_account.name if r.executed_by_account else ''),
        'project_id': (int(r.project_id) if r.project_id else None),
        'project_name': (r.project.name if r.project else ''),
        'stage_id': (int(r.stage_id) if r.stage_id else None),
        'stage_name': (r.stage.name if r.stage else ''),
        'related_entity_type': (r.related_entity_type or ''),
        'related_entity_id': (int(r.related_entity_id) if r.related_entity_id is not None else None),
        'party_name': (r.party_name or ''),
        'category': (r.category or ''),
        'note': (r.note or ''),
        'reference_id': (r.reference_id or ''),
        'group_id': (r.group_id or ''),
        'source_type': (r.source_type or ''),
        'source_id': (int(r.source_id) if r.source_id is not None else None),
        'created_at': (r.created_at.isoformat(sep=' ') if r.created_at else ''),
    }


def _account_source_type_base(v):
    s = (v or '').strip().lower()
    if not s:
        return ''
    return s.split(':', 1)[0].strip().lower()


def _account_txn_group_rows(txn_row):
    if not txn_row:
        return []
    gid = (txn_row.group_id or '').strip()
    if gid:
        return (AccountTransaction.query
                .filter(AccountTransaction.group_id == gid)
                .order_by(AccountTransaction.id.asc())
                .all())
    return [txn_row]


def _accounts_set_void_by_source(source_type, source_id, make_void=True):
    st = (source_type or '').strip().lower()
    sid = int(source_id or 0)
    if (not st) or (not sid):
        return 0
    rows = (AccountTransaction.query
            .filter(
                AccountTransaction.source_id == sid,
                or_(
                    func.lower(func.coalesce(AccountTransaction.source_type, '')) == st,
                    func.lower(func.coalesce(AccountTransaction.source_type, '')).like(f'{st}:%')
                )
            )
            .all())
    for r in rows:
        r.is_void = bool(make_void)
    return len(rows)


def _set_void_state_row(row, make_void=True, reason=''):
    if not row:
        return True, ''
    if not hasattr(row, 'is_void'):
        return True, ''
    row.is_void = bool(make_void)
    if hasattr(row, 'void_reason'):
        row.void_reason = ((reason or '').strip() if make_void else None)
    if hasattr(row, 'voided_at'):
        row.voided_at = (_pkt_now_naive() if make_void else None)
    return True, ''


def _sync_source_row_void_state(source_type, source_id, make_void=True, reason=''):
    st = (source_type or '').strip().lower()
    sid = int(source_id or 0)
    if (not st) or (not sid):
        return True, ''
    row = None
    handled = True
    if st == 'owner_payment':
        row = OwnerPayment.query.get(sid)
    elif st in ('labour_ledger_advance', 'labour_ledger_payment', 'labour_ledger_tip', 'worker_payment'):
        row = LabourLedger.query.get(sid)
    elif st in ('supplier_credit_payment', 'supplier_credit_tip', 'supplier_credit_settlement'):
        row = SupplierLedger.query.get(sid)
    elif st in ('subcontract_payment_payment', 'subcontract_payment_tip'):
        row = SubcontractPayment.query.get(sid)
    elif st in ('office_staff_ledger_advance', 'office_staff_ledger_payment', 'office_staff_ledger_tip'):
        row = OfficeStaffLedger.query.get(sid)
    elif st == 'office_expense':
        row = OfficeExpense.query.get(sid)
    elif st == 'expense':
        row = Expense.query.get(sid)
    elif st == 'purchase_v2_paid':
        row = PurchaseV2.query.get(sid)
        if not row:
            return False, f'Linked source row not found for {st}#{sid}.'
        if row.is_void:
            return False, f'Cannot sync {st}#{sid}; source purchase is void.'
        row.payment_status = ('unpaid' if make_void else 'paid')
        row.updated_at = _pkt_now_naive()
        _sync_purchase_v2_ledger(row)
        return True, ''
    else:
        handled = False
    if not handled:
        return True, ''
    if not row:
        return False, f'Linked source row not found for {st}#{sid}.'
    ok, msg = _set_void_state_row(row, make_void=make_void, reason=reason)
    if (not ok):
        return ok, msg
    if st in ('office_staff_ledger_advance', 'office_staff_ledger_payment', 'office_staff_ledger_tip'):
        staff_row = OfficeStaff.query.get(int(getattr(row, 'staff_id', 0) or 0))
        if make_void:
            _remove_office_salary_expense_for_ledger(row.id)
        elif staff_row:
            _sync_office_staff_expense_from_ledger(staff_row, row)
    return True, ''


def _accounts_update_manual_transaction(txn_id, payload):
    row = AccountTransaction.query.get(int(txn_id or 0))
    if not row:
        return False, 'Transaction not found.', 0
    rows = _account_txn_group_rows(row)
    if len(rows) != 1:
        return False, 'Grouped/split transactions cannot be edited directly. Void/reverse and recreate.', 0
    if row.is_void:
        return False, 'Voided transaction cannot be edited.', 0
    if row.source_type:
        flash('Warning: This transaction is synced from source. Changes may be overwritten by the source system.', 'warning')
    merged = {
        'date': ((row.date.isoformat() if row.date else _pkt_today().isoformat())),
        'type': (row.type or ''),
        'amount': float(row.amount or 0.0),
        'from_account_id': int(row.from_account_id or 0),
        'to_account_id': int(row.to_account_id or 0),
        'executed_by_account_id': int(row.executed_by_account_id or row.from_account_id or 0),
        'project_id': int(row.project_id or 0),
        'stage_id': int(row.stage_id or 0),
        'related_entity_type': (row.related_entity_type or ''),
        'related_entity_id': int(row.related_entity_id or 0),
        'party_name': (row.party_name or ''),
        'category': (row.category or ''),
        'note': (row.note or ''),
        'reference_id': (row.reference_id or ''),
        'group_id': (row.group_id or ''),
        'source_type': (row.source_type or ''),
        'source_id': (int(row.source_id) if row.source_id else 0),
    }
    for key in (
        'date', 'type', 'amount', 'from_account_id', 'to_account_id', 'executed_by_account_id',
        'project_id', 'stage_id', 'related_entity_type', 'related_entity_id',
        'party_name', 'note', 'reference_id'
    ):
        if key in (payload or {}):
            if key in ('related_entity_type', 'related_entity_id') and (payload.get(key) is None or str(payload.get(key)).strip() == ''):
                continue
            merged[key] = payload.get(key)
    if 'to_account_id' in (payload or {}) and not payload.get('to_account_id'):
        merged['to_account_id'] = ''
    if 'project_id' in (payload or {}) and not payload.get('project_id'):
        merged['project_id'] = ''
    if 'stage_id' in (payload or {}) and not payload.get('stage_id'):
        merged['stage_id'] = ''
    if 'executed_by_account_id' not in (payload or {}):
        merged['executed_by_account_id'] = merged.get('from_account_id')

    norm = _normalize_account_txn_payload(merged)
    ok, msg = _validate_account_transaction_payload(norm)
    if not ok:
        return False, msg, 0
    replacement_rows = _build_account_txn_rows(norm)
    if len(replacement_rows) != 1:
        return False, 'Only single-row transaction edit is supported.', 0
    ok_bal, msg_bal = _check_overdraft_block_replace([row], replacement_rows)
    if not ok_bal:
        return False, msg_bal, 0
    repl = replacement_rows[0]

    # Check if update would create duplicate
    duplicate = db.session.query(AccountTransaction.id).filter(
        AccountTransaction.id != row.id,
        AccountTransaction.date == repl['date'],
        AccountTransaction.type == repl['type'],
        AccountTransaction.amount == repl['amount'],
        AccountTransaction.from_account_id == repl['from_account_id'],
        AccountTransaction.to_account_id == repl['to_account_id'],
        AccountTransaction.project_id == repl['project_id'],
        AccountTransaction.stage_id == repl['stage_id'],
        AccountTransaction.party_name == repl['party_name'],
        AccountTransaction.reference_id == repl['reference_id'],
        AccountTransaction.is_void == False
    ).first()
    if duplicate:
        return False, 'Update would create a duplicate transaction.', 0

    row.date = repl.get('date') or row.date
    row.amount = float(repl.get('amount') or 0.0)
    row.type = (repl.get('type') or row.type)
    row.from_account_id = int(repl.get('from_account_id') or row.from_account_id)
    row.to_account_id = (int(repl.get('to_account_id')) if repl.get('to_account_id') else None)
    row.executed_by_account_id = int(repl.get('executed_by_account_id') or row.from_account_id)
    row.project_id = (int(repl.get('project_id')) if repl.get('project_id') else None)
    row.stage_id = (int(repl.get('stage_id')) if repl.get('stage_id') else None)
    row.related_entity_type = (repl.get('related_entity_type') or None)
    row.related_entity_id = (int(repl.get('related_entity_id')) if repl.get('related_entity_id') else None)
    row.party_name = (repl.get('party_name') or None)
    row.category = (repl.get('category') or row.category)
    row.note = (repl.get('note') or None)
    row.reference_id = (repl.get('reference_id') or None)

    _sync_account_transaction_source_update(row)
    db.session.commit()
    return True, '', 1


def _sync_account_transaction_source_update(txn_row):
    if not txn_row or not txn_row.source_type or not txn_row.source_id:
        return
    source_type = _account_source_type_base(txn_row.source_type)
    if source_type not in ('labour_ledger_advance', 'labour_ledger_payment', 'labour_ledger_tip'):
        return
    ledger_row = LabourLedger.query.get(int(txn_row.source_id or 0))
    if not ledger_row or ledger_row.is_void:
        return
    ledger_row.date = txn_row.date or ledger_row.date
    ledger_row.activity_at = _activity_at_for(ledger_row.date)
    ledger_row.amount = float(txn_row.amount or ledger_row.amount)
    ledger_row.project_id = txn_row.project_id
    ledger_row.stage_id = txn_row.stage_id
    ledger_row.notes = (txn_row.note or ledger_row.notes)
    if source_type == 'labour_ledger_advance':
        ledger_row.entry_type = 'advance'
    elif source_type == 'labour_ledger_payment':
        ledger_row.entry_type = 'payment'
    elif source_type == 'labour_ledger_tip':
        ledger_row.entry_type = 'tip'
    if str((txn_row.related_entity_type or '')).strip().lower() == 'worker' and txn_row.related_entity_id:
        ledger_row.worker_id = int(txn_row.related_entity_id)


def _accounts_toggle_transaction_void_state(txn_id, make_void=True, reason=''):
    row = AccountTransaction.query.get(int(txn_id or 0))
    if not row:
        return False, 'Transaction not found.', 0
    rows = _account_txn_group_rows(row)
    source_pairs = set()
    for r in rows:
        st_base = _account_source_type_base(r.source_type)
        sid = int(r.source_id or 0)
        if st_base and sid:
            source_pairs.add((st_base, sid))
    for st_base, sid in source_pairs:
        ok, msg = _sync_source_row_void_state(st_base, sid, make_void=make_void, reason=reason)
        if not ok:
            db.session.rollback()
            return False, (msg or 'Unable to sync source row state.'), 0
    for r in rows:
        r.is_void = bool(make_void)
    db.session.commit()
    return True, '', len(rows)


def _accounts_reconciliation_snapshot(limit=50):
    out = []

    # Paid purchases without active Accounts posting
    paid_ids = [int(r.id) for r in PurchaseV2.query.filter(PurchaseV2.is_void == False, func.lower(PurchaseV2.payment_status) == 'paid').all()]
    missing_paid = []
    for pid in paid_ids:
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == pid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'purchase_v2_paid:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like('purchase_v2_paid:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_paid.append(pid)
    out.append({'key': 'purchase_paid_missing_accounts', 'count': len(missing_paid), 'sample_ids': missing_paid[:limit]})

    # Unpaid/void purchases that still have active Accounts posting
    stale_paid = []
    candidate_acc = (AccountTransaction.query
                     .filter(
                         AccountTransaction.is_void == False,
                         or_(
                             func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'purchase_v2_paid:direct',
                             func.lower(func.coalesce(AccountTransaction.source_type, '')).like('purchase_v2_paid:%')
                         )
                     )
                     .all())
    for a in candidate_acc:
        pid = int(a.source_id or 0)
        p = PurchaseV2.query.get(pid) if pid else None
        if (not p) or p.is_void or ((p.payment_status or 'unpaid').strip().lower() != 'paid'):
            stale_paid.append(int(a.id))
    out.append({'key': 'purchase_accounts_stale_active', 'count': len(stale_paid), 'sample_ids': stale_paid[:limit]})

    # Owner payments without active Accounts posting
    owner_ids = [int(r.id) for r in OwnerPayment.query.filter(OwnerPayment.is_void == False).all()]
    missing_owner = []
    for oid in owner_ids:
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == oid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'owner_payment:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like('owner_payment:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_owner.append(oid)
    out.append({'key': 'owner_payment_missing_accounts', 'count': len(missing_owner), 'sample_ids': missing_owner[:limit]})

    # Worker ledger (advance/payment/tip) without active Accounts posting
    labour_ids = [
        int(r.id) for r in LabourLedger.query.filter(
            LabourLedger.is_void == False,
            LabourLedger.entry_type.in_(('advance', 'payment', 'tip'))
        ).all()
    ]
    missing_labour = []
    for lid in labour_ids:
        row = LabourLedger.query.get(lid)
        st1 = f"labour_ledger_{(row.entry_type or '').strip().lower()}"
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == lid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == f'{st1}:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like(f'{st1}:%'),
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'worker_payment:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like('worker_payment:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_labour.append(lid)
    out.append({'key': 'labour_ledger_missing_accounts', 'count': len(missing_labour), 'sample_ids': missing_labour[:limit]})

    # Subcontract payments without active Accounts posting
    sub_payment_ids = [
        int(r.id) for r in SubcontractPayment.query.filter(
            SubcontractPayment.is_void == False,
            func.lower(func.coalesce(SubcontractPayment.entry_type, 'payment')).in_(('payment', 'tip'))
        ).all()
    ]
    missing_sub_pay = []
    for spid in sub_payment_ids:
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == spid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'subcontract_payment_payment:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like('subcontract_payment_payment:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_sub_pay.append(spid)
    out.append({'key': 'subcontract_payment_missing_accounts', 'count': len(missing_sub_pay), 'sample_ids': missing_sub_pay[:limit]})

    # Office staff ledger cash entries without active Accounts posting
    office_ledger_ids = [
        int(r.id) for r in OfficeStaffLedger.query.filter(
            OfficeStaffLedger.is_void == False,
            func.lower(func.coalesce(OfficeStaffLedger.entry_type, '')).in_(('advance', 'payment', 'tip'))
        ).all()
    ]
    missing_office_ledger = []
    for oid in office_ledger_ids:
        row = OfficeStaffLedger.query.get(oid)
        st1 = f"office_staff_ledger_{(row.entry_type or '').strip().lower()}"
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == oid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == f'{st1}:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like(f'{st1}:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_office_ledger.append(oid)
    out.append({'key': 'office_staff_ledger_missing_accounts', 'count': len(missing_office_ledger), 'sample_ids': missing_office_ledger[:limit]})

    # Office expenses without active Accounts posting
    office_expense_ids = [
        int(r.id) for r in OfficeExpense.query.filter(
            OfficeExpense.is_void == False,
            OfficeExpense.office_staff_ledger_id.is_(None)
        ).all()
    ]
    missing_office_exp = []
    for oeid in office_expense_ids:
        has_acc = (db.session.query(AccountTransaction.id)
                   .filter(
                       AccountTransaction.is_void == False,
                       AccountTransaction.source_id == oeid,
                       or_(
                           func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'office_expense:direct',
                           func.lower(func.coalesce(AccountTransaction.source_type, '')).like('office_expense:%')
                       )
                   )
                   .first() is not None)
        if not has_acc:
            missing_office_exp.append(oeid)
    out.append({'key': 'office_expense_missing_accounts', 'count': len(missing_office_exp), 'sample_ids': missing_office_exp[:limit]})

    return out


def _accounts_forensic_report(limit=100):
    limit = max(1, min(int(limit or 100), 1000))
    out = {
        'reconciliation': _accounts_reconciliation_snapshot(limit=limit),
        'orphans': [],
    }
    scope_required_types = tuple(str(x).strip().lower() for x in (_ACCOUNT_OUTGOING_SCOPE_REQUIRED_TYPES or ()) if str(x).strip())
    outgoing_missing_scope_q = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        or_(AccountTransaction.project_id.is_(None), AccountTransaction.stage_id.is_(None))
    )
    if scope_required_types:
        outgoing_missing_scope_q = outgoing_missing_scope_q.filter(
            func.lower(func.coalesce(AccountTransaction.type, '')).in_(scope_required_types)
        )
    else:
        outgoing_missing_scope_q = outgoing_missing_scope_q.filter(text('1=0'))
    outgoing_missing_scope = (outgoing_missing_scope_q
                              .order_by(AccountTransaction.id.desc())
                              .limit(limit)
                              .all())
    out['orphans'].append({
        'key': 'outgoing_missing_project_or_stage',
        'count': len(outgoing_missing_scope),
        'sample_ids': [int(r.id) for r in outgoing_missing_scope[:limit]]
    })

    owner_missing_receive = (OwnerPayment.query
                             .filter(
                                 OwnerPayment.is_void == False,
                                 OwnerPayment.received_to_account_id.is_(None)
                             )
                             .order_by(OwnerPayment.id.desc())
                             .limit(limit)
                             .all())
    out['orphans'].append({
        'key': 'owner_payment_missing_receiving_account',
        'count': len(owner_missing_receive),
        'sample_ids': [int(r.id) for r in owner_missing_receive[:limit]]
    })
    return out


def _account_intent_field_matrix():
    out = {
        'transfer': {'from_account': True, 'to_account': True, 'project': False, 'stage': False, 'related': False},
        'expense_material': {'from_account': True, 'to_account': False, 'project': False, 'stage': False, 'related': True},
        # Wage/subcontractor payments may optionally carry project/stage;
        # project+stage become mandatory only when tip/settlement is used.
        'expense_wage': {'from_account': True, 'to_account': False, 'project': True, 'stage': True, 'related': True},
        'expense_subcontractor': {'from_account': True, 'to_account': False, 'project': True, 'stage': True, 'related': True},
        'office_management_payment': {'from_account': True, 'to_account': False, 'project': False, 'stage': False, 'related': True},
        'personal_management_payment': {'from_account': True, 'to_account': True, 'project': False, 'stage': False, 'related': False},
        'expense_general': {'from_account': True, 'to_account': False, 'project': True, 'stage': True, 'related': False},
        'project_income': {'from_account': True, 'to_account': True, 'project': True, 'stage': False, 'related': False},
        'party_receipt': {'from_account': True, 'to_account': True, 'project': False, 'stage': False, 'related': False},
        'client_payment': {'from_account': True, 'to_account': True, 'project': False, 'stage': False, 'related': False},
        'party_payment': {'from_account': True, 'to_account': True, 'project': False, 'stage': False, 'related': False},
        'advance_to_person': {'from_account': True, 'to_account': True, 'project': True, 'stage': True, 'related': True},
        'purchase': {'from_account': True, 'to_account': False, 'project': False, 'stage': False, 'related': True},
        'payroll': {'from_account': True, 'to_account': False, 'project': True, 'stage': True, 'related': True},
    }
    out['receive_from_project'] = dict(out['project_income'])
    out['receive_intra_company'] = dict(out['transfer'])
    out['receive_from_credit_debit'] = dict(out['party_receipt'])
    out['pay_to_project'] = dict(out['party_payment'])
    out['pay_intra_company'] = dict(out['transfer'])
    out['pay_to_credit_debit'] = dict(out['party_payment'])
    out['personal_payment'] = dict(out['personal_management_payment'])
    return out


def _normalize_related_entity_type(v):
    t = (v or '').strip().lower()
    if t in ('vendor', 'supplier'):
        return 'supplier'
    if t in ('subcontractor', 'sub_contractor', 'sub'):
        return 'subcontractor'
    if t in ('worker', 'labour', 'labor'):
        return 'worker'
    if t in ('project',):
        return 'project'
    if t in ('client',):
        return 'client'
    if t in ('office_staff', 'office-staff', 'officestaff', 'staff'):
        return 'office_staff'
    return t


def _account_entity_label(entity_type, entity_id):
    et = _normalize_related_entity_type(entity_type)
    eid = int(entity_id or 0)
    if not eid:
        return ''
    if et == 'worker':
        row = Worker.query.get(eid)
        return row.name if row else ''
    if et == 'supplier':
        row = Supplier.query.get(eid)
        return row.name if row else ''
    if et == 'subcontractor':
        row = Subcontractor.query.get(eid)
        return row.name if row else ''
    if et == 'project':
        row = Project.query.get(eid)
        return row.name if row else ''
    if et == 'office_staff':
        row = OfficeStaff.query.get(eid)
        return row.name if row else ''
    return ''


def _account_reference_links(txn_row):
    parts = []
    seen = set()
    def _push(label, url=''):
        k = (str(label or '').strip().lower(), str(url or '').strip())
        if (not k[0]) or (k in seen):
            return
        seen.add(k)
        parts.append({'label': label, 'url': url})
    if txn_row.project_id and txn_row.project:
        _push(f'Project: {txn_row.project.name}', url_for('hdc_project_detail', pid=txn_row.project_id))
    if txn_row.stage_id and txn_row.stage:
        _push(f'Stage: {txn_row.stage.name}', url_for('hdc_project_detail', pid=txn_row.project_id) if txn_row.project_id else '')
    et = _normalize_related_entity_type(txn_row.related_entity_type)
    eid = int(txn_row.related_entity_id or 0)
    if et == 'worker' and eid:
        nm = _account_entity_label(et, eid) or f'Worker #{eid}'
        _push(f'Worker: {nm}', url_for('hdc_worker_ledger', wid=eid))
    elif et == 'supplier' and eid:
        nm = _account_entity_label(et, eid) or f'Supplier #{eid}'
        _push(f'Supplier: {nm}', url_for('hdc_purchase_v2_supplier_detail', supplier_id=eid))
    elif et == 'subcontractor' and eid:
        nm = _account_entity_label(et, eid) or f'Subcontractor #{eid}'
        _push(f'Subcontractor: {nm}', url_for('hdc_subcontractor_ledger', sid=eid))
    elif et == 'project' and eid:
        nm = _account_entity_label(et, eid) or f'Project #{eid}'
        _push(f'Project: {nm}', url_for('hdc_project_detail', pid=eid))
    elif et == 'office_staff' and eid:
        nm = _account_entity_label(et, eid) or f'Office Staff #{eid}'
        _push(f'Office Staff: {nm}', url_for('hdc_office_staff_ledger', sid=eid))
    elif (txn_row.party_name or '').strip():
        _push(f'Party: {txn_row.party_name}', '')
    if (txn_row.reference_id or '').strip():
        _push(f'Ref: {txn_row.reference_id}', '')
    _push(f'Txn #{txn_row.id}', '')
    return parts


def _account_ledger_rows(account_id):
    aid = int(account_id or 0)
    account = Account.query.get(aid) if aid else None
    if not account:
        return []
    rows = (AccountTransaction.query
            .filter(or_(AccountTransaction.from_account_id == aid, AccountTransaction.to_account_id == aid))
            .all())
    rows = sorted(
        rows,
        key=lambda r: (
            (r.created_at or datetime.combine((r.date or _pkt_today()), datetime.min.time())),
            int(r.id or 0)
        )
    )
    running = float(account.opening_balance or 0.0)
    out = []
    for r in rows:
        amt = float(r.amount or 0.0)
        debit = (amt if int(r.to_account_id or 0) == aid else 0.0)
        credit = (amt if int(r.from_account_id or 0) == aid else 0.0)
        src_name = (r.from_account.name if getattr(r, 'from_account', None) else f'Account #{int(r.from_account_id or 0)}')
        dst_name = (
            (r.to_account.name if getattr(r, 'to_account', None) else '')
            or (r.party_name or '').strip()
            or 'Off-ledger Party'
        )
        if not r.is_void:
            running += debit
            running -= credit
        ts = (r.created_at or datetime.combine((r.date or _pkt_today()), datetime.min.time()))
        out.append({
            'id': int(r.id),
            'timestamp': ts,
            'tx_type': str(r.type or '').replace('_', ' ').title(),
            'current_amount': float(amt or 0.0),
            'flow': f'{src_name} -> {dst_name}',
            'debit': float(debit or 0.0),
            'credit': float(credit or 0.0),
            'running_balance': float(running),
            'references': _account_reference_links(r),
            'is_void': bool(r.is_void)
        })
    return out


def _account_reverse_transaction_group(txn_id, reason=''):
    row = AccountTransaction.query.get(int(txn_id or 0))
    if not row:
        return False, 'Transaction not found.', 0
    rows = _account_txn_group_rows(row)
    if not rows:
        return False, 'Transaction group not found.', 0
    if any(bool(r.is_void) for r in rows):
        return False, 'Voided transactions cannot be reversed.', 0
    if any((r.source_type or '').strip() and _account_source_type_base(r.source_type) != 'account_reversal' for r in rows):
        return False, 'This entry is synced from another module. Reverse it in its source module.', 0
    reverse_group_id = uuid4().hex
    created = 0
    try:
        for idx, r in enumerate(rows, start=1):
            dest_account_id = int(r.to_account_id or 0)
            src_account_id = int(r.from_account_id or 0)
            if not src_account_id:
                continue
            if not dest_account_id:
                party = _accounts_party_account((r.party_name or 'External Parties'), 'person')
                dest_account_id = int(party.id or 0) if party else 0
            if not dest_account_id:
                return False, 'Unable to resolve reversal counter-account.', created
            rev_note = f"Reversal of txn #{r.id}"
            if reason:
                rev_note = f"{rev_note} | {reason}"
            if r.note:
                rev_note = f"{rev_note} | {r.note}"
            rev = AccountTransaction(
                date=_pkt_today(),
                amount=float(r.amount or 0.0),
                type=(r.type or 'transfer'),
                from_account_id=dest_account_id,
                to_account_id=src_account_id,
                executed_by_account_id=dest_account_id,
                project_id=(r.project_id if r.project_id else None),
                stage_id=(r.stage_id if r.stage_id else None),
                related_entity_type=(r.related_entity_type or None),
                related_entity_id=(int(r.related_entity_id) if r.related_entity_id else None),
                party_name=(r.party_name or None),
                category=(r.category or 'transfer'),
                note=rev_note[:400],
                reference_id=(f"REV-{r.id}-{(r.reference_id or '').strip()}".strip('-')[:120]),
                group_id=reverse_group_id,
                source_type='account_reversal',
                source_id=None,
                is_void=False,
                created_at=_pkt_now_naive()
            )
            db.session.add(rev)
            created += 1
        if created <= 0:
            db.session.rollback()
            return False, 'No reversal rows were created.', 0
        db.session.commit()
        return True, '', created
    except Exception as ex:
        db.session.rollback()
        return False, f'Reversal save failed: {ex}', created


def _account_pending_snapshot(tx_type, related_entity_type=None, related_entity_id=None, project_id=None, stage_id=None):
    tx_type = _normalize_account_tx_type(tx_type)
    et = _normalize_related_entity_type(related_entity_type)
    eid = int(related_entity_id or 0)
    pid = int(project_id or 0)
    sid = int(stage_id or 0)
    snap = {
        'kind': '',
        'entity_type': et,
        'entity_id': (eid or None),
        'pending': 0.0,
        'paid': 0.0,
        'total': 0.0,
        'message': '',
    }
    if tx_type in ('expense_material', 'purchase') and et == 'supplier' and eid:
        debit = float(db.session.query(func.coalesce(func.sum(SupplierLedger.amount), 0.0))
                      .filter(
                          SupplierLedger.supplier_id == eid,
                          SupplierLedger.is_void == False,
                          func.lower(SupplierLedger.entry_type) == 'debit'
                      ).scalar() or 0.0)
        credit = float(db.session.query(func.coalesce(func.sum(SupplierLedger.amount), 0.0))
                       .filter(
                           SupplierLedger.supplier_id == eid,
                           SupplierLedger.is_void == False,
                           func.lower(SupplierLedger.entry_type) == 'credit'
                       ).scalar() or 0.0)
        pending = max(0.0, debit - credit)
        snap.update({
            'kind': 'supplier_payable',
            'pending': pending,
            'paid': credit,
            'total': debit,
            'message': f'Supplier payable pending: {pending:,.2f} PKR',
        })
        return snap
    if tx_type in ('expense_wage', 'payroll', 'advance_to_person') and et == 'worker' and eid:
        ws = _worker_payable_snapshot(eid)
        pending = float(ws.get('payable') or 0.0)
        paid = float(ws.get('paid') or 0.0)
        total = float(ws.get('earned') or 0.0)
        snap.update({
            'kind': 'worker_payable',
            'pending': pending,
            'paid': paid,
            'total': total,
            'message': f'Worker payable pending: {pending:,.2f} PKR',
        })
        return snap
    if tx_type == 'office_management_payment' and et == 'office_staff' and eid:
        staff = OfficeStaff.query.get(eid)
        if not staff:
            return snap
        ss = _office_staff_ledger_snapshot(eid)
        pending = max(0.0, float(ss.get('balance') or 0.0))
        paid = float(ss.get('paid') or 0.0) + float(ss.get('tip') or 0.0)
        total = float(ss.get('total_earned') or 0.0)
        snap.update({
            'kind': 'office_staff_payable',
            'pending': pending,
            'paid': paid,
            'total': total,
            'message': f'Office staff payable pending: {pending:,.2f} PKR',
        })
        return snap
    if tx_type == 'expense_subcontractor' and et == 'subcontractor' and eid:
        sub = Subcontractor.query.get(eid)
        if not sub:
            return snap
        if sid:
            st = Stage.query.get(sid)
            if st and (not pid or int(st.project_id or 0) == int(pid)):
                sss = _subcontract_stage_snapshot(sub, st)
                pending = max(0.0, float(sss.get('balance') or 0.0))
                paid = float(sss.get('paid') or 0.0)
                total = float(sss.get('payable') or 0.0)
                snap.update({
                    'kind': 'subcontract_stage_payable',
                    'pending': pending,
                    'paid': paid,
                    'total': total,
                    'message': f'Subcontractor stage pending: {pending:,.2f} PKR',
                })
                return snap
        pending = max(0.0, float(sub.payable_balance or 0.0))
        paid = float(sub.total_cleared or 0.0)
        total = float(sub.payable_amount or 0.0)
        snap.update({
            'kind': 'subcontract_payable',
            'pending': pending,
            'paid': paid,
            'total': total,
            'message': f'Subcontractor pending: {pending:,.2f} PKR',
        })
        return snap
    if tx_type in ('project_income', 'client_payment', 'party_receipt') and pid:
        p = Project.query.get(pid)
        if p:
            pending = max(0.0, float(p.remaining_receivable or 0.0))
            paid = float(p.total_received or 0.0)
            total = float(p.owner_contract_value or 0.0)
            snap.update({
                'kind': 'project_receivable',
                'entity_type': 'project',
                'entity_id': p.id,
                'pending': pending,
                'paid': paid,
                'total': total,
                'message': f'Project receivable pending: {pending:,.2f} PKR',
            })
        return snap
    return snap


def _compute_excess_split(amount, pending, requested_tip=0.0, requested_advance=0.0):
    amount = max(0.0, float(amount or 0.0))
    pending = max(0.0, float(pending or 0.0))
    tip_req = max(0.0, float(requested_tip or 0.0))
    adv_req = max(0.0, float(requested_advance or 0.0))
    payment_part = min(amount, pending)
    excess_part = max(0.0, amount - payment_part)
    if excess_part <= 1e-6:
        return True, '', payment_part, 0.0, 0.0, 0.0
    if tip_req > excess_part + 1e-6:
        return False, f'Tip amount cannot exceed excess ({excess_part:,.2f} PKR).', 0.0, 0.0, 0.0, excess_part
    if tip_req <= 1e-6 and adv_req <= 1e-6:
        adv_req = excess_part
    total_split = tip_req + adv_req
    if abs(total_split - excess_part) > 0.01:
        return False, f'Tip + Advance must equal excess ({excess_part:,.2f} PKR).', 0.0, 0.0, 0.0, excess_part
    return True, '', payment_part, tip_req, adv_req, excess_part


def _create_accounts_transaction_with_sync(payload):
    data = dict(payload or {})
    tx_type = _normalize_account_tx_type((data.get('type') or data.get('transaction_type') or ''))
    data['type'] = tx_type
    rel_type = _normalize_related_entity_type(data.get('related_entity_type'))
    rel_id = _payload_int(data, 'related_entity_id')
    project_id = _payload_int(data, 'project_id')
    stage_id = _payload_int(data, 'stage_id')
    expense_category_id = _payload_int(data, 'expense_category_id')
    amount = max(0.0, _flt(data.get('amount'), 0.0))

    source_type = None
    source_id = None
    split_txn_payloads = []

    # Duplicate guard (only for purely manual entries — synced flows use source_type/id below).
    try:
        from_acc_int = int(data.get('from_account_id') or 0) or None
    except Exception:
        from_acc_int = None
    try:
        to_acc_int = int(data.get('to_account_id') or 0) or None
    except Exception:
        to_acc_int = None
    tx_date_norm = _parse_date(data.get('date'))
    if _has_recent_duplicate(
        AccountTransaction,
        type=tx_type,
        amount=float(amount),
        date=tx_date_norm,
        from_account_id=from_acc_int,
        to_account_id=to_acc_int,
        project_id=(project_id or None),
        stage_id=(stage_id or None),
        related_entity_type=(rel_type or None),
        related_entity_id=(rel_id or None),
        party_name=(data.get('party_name') or None),
        reference_id=(data.get('reference_id') or None)
    ):
        return False, 'Duplicate transaction prevented (same values submitted within a few seconds).', []

    expected_rel = _account_expected_related_type(tx_type)
    if expected_rel and (not rel_type):
        rel_type = expected_rel
        data['related_entity_type'] = expected_rel

    # Hard-bind party_name to the selected entity's canonical name. This is the
    # single most important guard against the "1 person but 2 ledgers" class of
    # mistake: if the user picked Worker A but typed a different name in the
    # party_name field, we silently overwrite with the canonical name so the
    # AccountTransaction row, the LabourLedger row, and the entity's account
    # cannot disagree.
    if rel_type and rel_id:
        canonical_lbl = _account_entity_label(rel_type, rel_id)
        if canonical_lbl:
            typed_party = (data.get('party_name') or '').strip()
            if typed_party and _normalize_name_ci(typed_party).lower() != _normalize_name_ci(canonical_lbl).lower():
                # Refuse outright — surface the conflict so the user fixes it.
                return False, (
                    f'Party name "{typed_party}" does not match selected '
                    f'{rel_type} "{canonical_lbl}". Clear the party name field or pick the right entity.'
                ), []
            data['party_name'] = canonical_lbl
    try:
        if tx_type == 'project_income':
            if not int(project_id or 0):
                return False, 'Project is required for Project Income receipt.', []
            prj = Project.query.get(project_id)
            if not prj:
                return False, 'Valid project is required for Project Income receipt.', []
            client_name = _normalize_name_ci(prj.client or '')
            if not client_name:
                return False, 'Selected project has no client name. Update project client first.', []
            client_acc = _accounts_party_account(client_name, 'client')
            if not client_acc:
                return False, 'Unable to resolve project client account.', []
            data['from_account_id'] = int(client_acc.id)
            data['executed_by_account_id'] = int(client_acc.id)
            if not int(_payload_int(data, 'to_account_id') or 0):
                return False, 'Select Receive In (company account).', []
            data['related_entity_type'] = 'project'
            data['related_entity_id'] = int(prj.id)
            if not (data.get('party_name') or '').strip():
                data['party_name'] = client_name

        if tx_type in ('expense_material', 'purchase') and (rel_type != 'supplier' or not rel_id):
            return False, 'Select supplier in Related Entity for material/purchase payment sync.', []
        if tx_type in ('expense_wage', 'payroll', 'advance_to_person') and (rel_type != 'worker' or not rel_id):
            return False, 'Select worker in Related Entity for wage/payroll sync.', []
        if tx_type == 'expense_subcontractor' and (rel_type != 'subcontractor' or not rel_id):
            return False, 'Select subcontractor in Related Entity for subcontractor payment sync.', []
        if _account_requires_scope_tags(tx_type):
            if not int(project_id or 0):
                return False, 'Project is mandatory for outgoing payments.', []
            if not int(stage_id or 0):
                return False, 'Stage is mandatory for outgoing payments.', []

        if tx_type in ('expense_material', 'purchase'):
            if rel_type == 'supplier' and rel_id:
                pending = _account_pending_snapshot(tx_type, rel_type, rel_id, project_id=project_id, stage_id=stage_id)
                pend = float(pending.get('pending') or 0.0)
                s = Supplier.query.get(rel_id)
                if not s or s.is_void:
                    return False, 'Valid supplier is required.', []
                tx_date = _parse_date(data.get('date'))
                base_note = (data.get('note') or '').strip()
                requested_tip = max(0.0, _flt(data.get('excess_tip_amount'), 0.0))
                requested_advance = max(0.0, _flt(data.get('excess_advance_amount'), 0.0))
                ok_split, msg_split, payment_part, tip_part, advance_part, _ = _compute_excess_split(
                    amount, pend, requested_tip=requested_tip, requested_advance=requested_advance
                )
                if not ok_split:
                    return False, msg_split, []
                payable_part = payment_part + advance_part

                def _append_supplier_credit_row(ref_type, component_amount, component_note):
                    if component_amount <= 1e-6:
                        return
                    row = SupplierLedger(
                        supplier_id=s.id,
                        entry_type='credit',
                        amount=float(component_amount),
                        reference_type=ref_type,
                        reference_id=None,
                        note=component_note,
                        is_void=False,
                        created_at=_pkt_now_naive()
                    )
                    db.session.add(row)
                    db.session.flush()
                    split_txn_payloads.append({
                        'date': (tx_date.isoformat() if tx_date else (data.get('date') or _pkt_today().isoformat())),
                        'type': 'purchase',
                        'amount': float(component_amount),
                        'from_account_id': data.get('from_account_id'),
                        'to_account_id': data.get('to_account_id'),
                        'executed_by_account_id': data.get('executed_by_account_id') or data.get('from_account_id'),
                        'project_id': (project_id or None),
                        'stage_id': (stage_id or None),
                        'related_entity_type': 'supplier',
                        'related_entity_id': s.id,
                        'party_name': data.get('party_name') or s.name,
                        'category': 'purchase',
                        'note': component_note,
                        'reference_id': f'supplier_ledger#{row.id}',
                        'source_type': f'supplier_credit_{ref_type}',
                        'source_id': int(row.id),
                    })

                pay_note = (base_note or f'Accounts payment to supplier {s.name}')
                if payment_part <= 1e-6 and advance_part > 1e-6:
                    pay_note = (base_note + ' | ' if base_note else '') + f'Auto advance from overpayment for {s.name}'
                _append_supplier_credit_row('payment', payable_part, pay_note)
                _append_supplier_credit_row('tip', tip_part, (base_note + ' | ' if base_note else '') + f'Tip for supplier {s.name}')

        if tx_type in ('expense_wage', 'payroll', 'advance_to_person'):
            if rel_type == 'worker' and rel_id:
                pending = _account_pending_snapshot(tx_type, rel_type, rel_id, project_id=project_id, stage_id=stage_id)
                pend = float(pending.get('pending') or 0.0)
                w = Worker.query.get(rel_id)
                if not w or (not w.active_status):
                    return False, 'Valid active worker is required.', []
                # Payroll allows empty to_account_id, but split overpayment can create
                # advance_to_person rows that strictly require a worker party account.
                worker_to_account_id = _payload_int(data, 'to_account_id') or None
                if not worker_to_account_id:
                    worker_label = _normalize_name_ci(w.name) or f'Worker #{w.id}'
                    worker_acc = _accounts_party_account(worker_label, 'person')
                    worker_to_account_id = int(worker_acc.id) if worker_acc else None

                tx_date = _parse_date(data.get('date'))
                base_note = (data.get('note') or '').strip()
                settle_shortfall = (str(data.get('settle_shortfall') or '').strip().lower() in ('1', 'true', 'on', 'yes'))

                if tx_type == 'advance_to_person':
                    row = LabourLedger(
                        worker_id=w.id,
                        entry_type='advance',
                        amount=amount,
                        date=tx_date,
                        project_id=(project_id or None),
                        stage_id=(stage_id or None),
                        activity_at=_pkt_now_naive(),
                        notes=(base_note or f'Accounts advance for {w.name}')
                    )
                    db.session.add(row)
                    db.session.flush()
                    source_type = 'labour_ledger_advance'
                    source_id = int(row.id)
                else:
                    requested_tip = max(0.0, _flt(data.get('excess_tip_amount'), 0.0))
                    requested_advance = max(0.0, _flt(data.get('excess_advance_amount'), 0.0))
                    ok_split, msg_split, payment_part, tip_part, advance_part, _ = _compute_excess_split(
                        amount, pend, requested_tip=requested_tip, requested_advance=requested_advance
                    )
                    if not ok_split:
                        return False, msg_split, []
                    settlement_part = 0.0
                    if settle_shortfall and payment_part < pend:
                        settlement_part = max(0.0, pend - payment_part)

                    if (tip_part > 0 or settlement_part > 0):
                        if (not project_id) or (not stage_id):
                            return False, 'For tip or shortfall settlement, select both project and stage.', []
                        stg = Stage.query.get(stage_id)
                        if (not stg) or int(stg.project_id or 0) != int(project_id):
                            return False, 'Selected stage does not belong to selected project.', []

                    def _append_worker_cash_txn(entry_type, component_amount, component_note):
                        if component_amount <= 1e-6:
                            return
                        if _has_recent_duplicate(
                            LabourLedger,
                            worker_id=w.id,
                            entry_type=entry_type,
                            amount=float(component_amount),
                            date=tx_date,
                            notes=component_note
                        ):
                            return  # Skip duplicate
                        row = LabourLedger(
                            worker_id=w.id,
                            entry_type=entry_type,
                            amount=float(component_amount),
                            date=tx_date,
                            project_id=(project_id or None),
                            stage_id=(stage_id or None),
                            activity_at=_pkt_now_naive(),
                            notes=component_note
                        )
                        db.session.add(row)
                        db.session.flush()
                        split_txn_payloads.append({
                            'date': (tx_date.isoformat() if tx_date else (data.get('date') or _pkt_today().isoformat())),
                            'type': ('advance_to_person' if entry_type == 'advance' else 'payroll'),
                            'amount': float(component_amount),
                            'from_account_id': data.get('from_account_id'),
                            'to_account_id': worker_to_account_id,
                            'executed_by_account_id': data.get('executed_by_account_id') or data.get('from_account_id'),
                            'project_id': (project_id or None),
                            'stage_id': (stage_id or None),
                            'related_entity_type': 'worker',
                            'related_entity_id': w.id,
                            'party_name': data.get('party_name') or w.name,
                            'category': ('advance' if entry_type == 'advance' else 'payroll'),
                            'note': component_note,
                            'reference_id': f'labour_ledger#{row.id}',
                            'source_type': f'labour_ledger_{entry_type}',
                            'source_id': int(row.id),
                        })

                    _append_worker_cash_txn('payment', payment_part, (base_note or f'Accounts payment for {w.name}'))

                    if tip_part > 1e-6:
                        # Bug fix (2026-04-25): the tip ledger row used to be
                        # tagged only with TIP_WORKER_ID, but the periodic
                        # reconciler matches by TIP_EXPENSE_ID — so when the
                        # reconciler ran it could not see this row and wrote a
                        # second tip ledger entry for the same cash event,
                        # leaving the worker's balance over-paid by the tip.
                        # We now flush the Expense immediately to capture its
                        # id, then embed TIP_EXPENSE_ID:<exp.id> in BOTH the
                        # Expense.remarks and the LabourLedger row's notes so
                        # the reconciler's match-by-id check finds it.
                        tip_note_base = (base_note + ' | ' if base_note else '') + f'Tip for {w.name} | TIP_WORKER_ID:{w.id}'
                        tip_cat = _ensure_expense_category('Tip')
                        tip_exp = Expense(
                            project_id=(project_id or None),
                            stage_id=(stage_id or None),
                            tip_worker_id=w.id,
                            category_id=(tip_cat.id if tip_cat else None),
                            amount=float(tip_part),
                            date=tx_date,
                            activity_at=_activity_at_for(tx_date),
                            remarks=tip_note_base
                        )
                        db.session.add(tip_exp)
                        db.session.flush()
                        tip_note = tip_note_base + f' | TIP_EXPENSE_ID:{tip_exp.id}'
                        tip_exp.remarks = tip_note
                        _append_worker_cash_txn('tip', tip_part, tip_note)

                    _append_worker_cash_txn('advance', advance_part, (base_note + ' | ' if base_note else '') + f'Auto advance from overpayment for {w.name}')

                    if settlement_part > 1e-6:
                        settlement_note = (base_note + ' | ' if base_note else '') + f'Settlement shortfall for {w.name} | SETTLE_WORKER_ID:{w.id}'
                        settlement_cat = _ensure_expense_category('Settlement')
                        db.session.add(Expense(
                            project_id=(project_id or None),
                            stage_id=(stage_id or None),
                            category_id=(settlement_cat.id if settlement_cat else None),
                            amount=-float(settlement_part),
                            date=tx_date,
                            activity_at=_activity_at_for(tx_date),
                            remarks=settlement_note
                        ))
                        db.session.add(LabourLedger(
                            worker_id=w.id,
                            entry_type='settlement',
                            amount=float(settlement_part),
                            date=tx_date,
                            project_id=(project_id or None),
                            stage_id=(stage_id or None),
                            activity_at=_pkt_now_naive(),
                            notes=settlement_note
                        ))

        if tx_type == 'expense_subcontractor':
            if rel_type == 'subcontractor' and rel_id:
                pending = _account_pending_snapshot(tx_type, rel_type, rel_id, project_id=project_id, stage_id=stage_id)
                pend = float(pending.get('pending') or 0.0)
                s = Subcontractor.query.get(rel_id)
                if not s:
                    return False, 'Valid subcontractor is required.', []
                tx_date = _parse_date(data.get('date'))
                base_note = (data.get('note') or '').strip()
                settle_shortfall = (str(data.get('settle_shortfall') or '').strip().lower() in ('1', 'true', 'on', 'yes'))
                requested_tip = max(0.0, _flt(data.get('excess_tip_amount'), 0.0))
                requested_advance = max(0.0, _flt(data.get('excess_advance_amount'), 0.0))
                ok_split, msg_split, payment_part, tip_part, advance_part, _ = _compute_excess_split(
                    amount, pend, requested_tip=requested_tip, requested_advance=requested_advance
                )
                if not ok_split:
                    return False, msg_split, []
                settlement_part = 0.0
                if settle_shortfall and payment_part < pend:
                    settlement_part = max(0.0, pend - payment_part)
                if (tip_part > 0 or settlement_part > 0) and ((not project_id) or (not stage_id)):
                    return False, 'For subcontractor tip/shortfall settlement, select both project and stage.', []
                if stage_id:
                    stg = Stage.query.get(stage_id)
                    if (not stg) or int(stg.project_id or 0) != int(project_id):
                        return False, 'Selected stage does not belong to selected project.', []

                def _append_subcontract_cash(entry_type, component_amount, component_note):
                    if component_amount <= 1e-6:
                        return
                    row = SubcontractPayment(
                        subcontractor_id=s.id,
                        project_id=(project_id or None),
                        stage_id=(stage_id or None),
                        entry_type=entry_type,
                        amount=float(component_amount),
                        date=tx_date,
                        activity_at=_pkt_now_naive(),
                        notes=component_note
                    )
                    db.session.add(row)
                    db.session.flush()
                    split_txn_payloads.append({
                        'date': (tx_date.isoformat() if tx_date else (data.get('date') or _pkt_today().isoformat())),
                        'type': 'expense_subcontractor',
                        'amount': float(component_amount),
                        'from_account_id': data.get('from_account_id'),
                        'to_account_id': data.get('to_account_id'),
                        'executed_by_account_id': data.get('executed_by_account_id') or data.get('from_account_id'),
                        'project_id': (project_id or None),
                        'stage_id': (stage_id or None),
                        'related_entity_type': 'subcontractor',
                        'related_entity_id': s.id,
                        'party_name': data.get('party_name') or s.name,
                        'category': 'expense',
                        'note': component_note,
                        'reference_id': f'subcontract_payment#{row.id}',
                        'source_type': f'subcontract_payment_{entry_type}',
                        'source_id': int(row.id),
                    })
                    _log_subcontract_event(
                        sub=s,
                        event_type=('tip' if entry_type == 'tip' else 'payment'),
                        amount=float(component_amount),
                        notes=component_note or 'Subcontract payment recorded',
                        project_id=(project_id or None),
                        stage_id=(stage_id or None)
                    )

                _append_subcontract_cash('payment', payment_part, (base_note or f'Accounts payment to subcontractor {s.name}'))
                _append_subcontract_cash('payment', advance_part, (base_note + ' | ' if base_note else '') + f'Auto advance from overpayment for {s.name}')
                if tip_part > 1e-6:
                    tip_note = (base_note + ' | ' if base_note else '') + f'Tip for subcontractor {s.name} | TIP_SUBCONTRACTOR_ID:{s.id}'
                    tip_cat = _ensure_expense_category('Tip')
                    db.session.add(Expense(
                        project_id=(project_id or None),
                        stage_id=(stage_id or None),
                        category_id=(tip_cat.id if tip_cat else None),
                        amount=float(tip_part),
                        date=tx_date,
                        activity_at=_activity_at_for(tx_date),
                        remarks=tip_note
                    ))
                    _append_subcontract_cash('tip', tip_part, tip_note)

                if settlement_part > 1e-6:
                    settlement_note = (base_note + ' | ' if base_note else '') + f'Subcontract settlement shortfall for {s.name} | SETTLE_SUBCONTRACTOR_ID:{s.id}'
                    settlement_cat = _ensure_expense_category('Settlement')
                    db.session.add(SubcontractPayment(
                        subcontractor_id=s.id,
                        project_id=(project_id or None),
                        stage_id=(stage_id or None),
                        entry_type='settlement',
                        amount=float(settlement_part),
                        date=tx_date,
                        activity_at=_pkt_now_naive(),
                        notes=settlement_note
                    ))
                    db.session.add(Expense(
                        project_id=(project_id or None),
                        stage_id=(stage_id or None),
                        category_id=(settlement_cat.id if settlement_cat else None),
                        amount=-float(settlement_part),
                        date=tx_date,
                        activity_at=_activity_at_for(tx_date),
                        remarks=settlement_note
                    ))
                    _log_subcontract_event(
                        sub=s,
                        event_type='settlement',
                        amount=float(settlement_part),
                        notes='Shortfall settled from Accounts',
                        project_id=(project_id or None),
                        stage_id=(stage_id or None)
                    )

        if tx_type == 'office_management_payment':
            office_target = (data.get('office_target') or '').strip().lower()
            if office_target not in ('staff', 'expense'):
                office_target = ('staff' if rel_type == 'office_staff' and rel_id else 'expense')
            tx_date = _parse_date(data.get('date'))
            base_note = (data.get('note') or '').strip()
            if office_target == 'staff':
                if rel_type != 'office_staff' or (not rel_id):
                    return False, 'Select office staff in Related Entity for office staff payment.', []
                s = OfficeStaff.query.get(rel_id)
                if not s:
                    return False, 'Valid office staff is required.', []
                pending = _account_pending_snapshot(tx_type, rel_type, rel_id)
                pend = float(pending.get('pending') or 0.0)
                requested_tip = max(0.0, _flt(data.get('excess_tip_amount'), 0.0))
                requested_advance = max(0.0, _flt(data.get('excess_advance_amount'), 0.0))
                ok_split, msg_split, payment_part, tip_part, advance_part, _ = _compute_excess_split(
                    amount, pend, requested_tip=requested_tip, requested_advance=requested_advance
                )
                if not ok_split:
                    return False, msg_split, []

                staff_to_account_id = _payload_int(data, 'to_account_id') or None
                if not staff_to_account_id:
                    staff_acc = _accounts_party_account(_normalize_name_ci(s.name) or f'Office Staff #{s.id}', 'person')
                    staff_to_account_id = int(staff_acc.id) if staff_acc else None

                def _append_office_staff_cash(entry_type, component_amount, component_note):
                    if component_amount <= 1e-6:
                        return
                    row = OfficeStaffLedger(
                        staff_id=s.id,
                        date=tx_date,
                        entry_type=entry_type,
                        amount=float(component_amount),
                        notes=component_note,
                        activity_at=_activity_at_for(tx_date)
                    )
                    db.session.add(row)
                    db.session.flush()
                    _sync_office_staff_expense_from_ledger(s, row)
                    split_txn_payloads.append({
                        'date': (tx_date.isoformat() if tx_date else (data.get('date') or _pkt_today().isoformat())),
                        'type': 'office_management_payment',
                        'amount': float(component_amount),
                        'from_account_id': data.get('from_account_id'),
                        'to_account_id': staff_to_account_id,
                        'executed_by_account_id': data.get('executed_by_account_id') or data.get('from_account_id'),
                        'project_id': None,
                        'stage_id': None,
                        'related_entity_type': 'office_staff',
                        'related_entity_id': s.id,
                        'party_name': data.get('party_name') or s.name,
                        'category': 'expense',
                        'note': component_note,
                        'reference_id': f'office_staff_ledger#{row.id}',
                        'source_type': f'office_staff_ledger_{entry_type}',
                        'source_id': int(row.id),
                    })

                _append_office_staff_cash('payment', payment_part, (base_note or f'Office salary payment for {s.name}'))
                if tip_part > 1e-6:
                    tip_note = (base_note + ' | ' if base_note else '') + f'Tip for office staff {s.name} | TIP_OFFICE_STAFF_ID:{s.id}'
                    _append_office_staff_cash('tip', tip_part, tip_note)
                _append_office_staff_cash('advance', advance_part, (base_note + ' | ' if base_note else '') + f'Auto advance from overpayment for {s.name}')
            else:
                expense_kind = _normalize_expense_category_name(data.get('office_expense_category')) or 'Office General'
                exp = OfficeExpense(
                    date=tx_date,
                    category=expense_kind,
                    amount=float(amount),
                    remarks=((base_note or data.get('party_name') or f'Office expense via Accounts: {expense_kind}') or '').strip(),
                    is_void=False,
                    activity_at=_activity_at_for(tx_date)
                )
                db.session.add(exp)
                db.session.flush()
                source_type = 'office_expense'
                source_id = int(exp.id)
                data['party_name'] = (data.get('party_name') or expense_kind)
                data['related_entity_type'] = None
                data['related_entity_id'] = None

        if tx_type == 'personal_management_payment':
            tx_date = _parse_date(data.get('date'))
            base_note = (data.get('note') or '').strip()
            beneficiary_name = _normalize_name_ci(data.get('party_name') or '')
            to_account_id = _payload_int(data, 'to_account_id')
            to_acc = (Account.query.get(to_account_id) if to_account_id else None)
            if (not beneficiary_name) and to_acc and (not bool(getattr(to_acc, 'is_void', False))):
                beneficiary_name = _normalize_name_ci(to_acc.name or '')
            if not beneficiary_name:
                beneficiary_name = 'Self'
            if not to_account_id:
                ben_acc = _accounts_party_account(beneficiary_name, 'person')
                if ben_acc and ben_acc.id:
                    to_account_id = int(ben_acc.id)
                    data['to_account_id'] = to_account_id
            if _has_recent_duplicate(
                PersonalExpense,
                beneficiary_name=beneficiary_name,
                date=tx_date,
                amount=float(amount),
                category='Accounts Payment',
                remarks=(base_note or f'Accounts personal payment to {beneficiary_name}')
            ):
                return False, 'Duplicate personal expense entry prevented (same values submitted too quickly).', []
            personal_row = PersonalExpense(
                beneficiary_name=beneficiary_name,
                beneficiary_type='other',
                date=tx_date,
                category='Accounts Payment',
                amount=float(amount),
                remarks=(base_note or f'Accounts personal payment to {beneficiary_name}'),
                is_void=False,
                activity_at=_activity_at_for(tx_date)
            )
            db.session.add(personal_row)
            db.session.flush()
            source_type = 'personal_expense'
            source_id = int(personal_row.id)
            data['party_name'] = beneficiary_name
            data['related_entity_type'] = None
            data['related_entity_id'] = None

        if tx_type == 'expense_general':
            tx_date = _parse_date(data.get('date'))
            cat = _ensure_expense_category_by_id(expense_category_id)
            if (not cat) or (not bool(getattr(cat, 'active_status', True))):
                return False, 'Select a valid active Expense Category for General Expense.', []
            exp = Expense(
                project_id=(project_id or None),
                stage_id=(stage_id or None),
                category_id=int(cat.id),
                amount=float(amount),
                date=tx_date,
                activity_at=_activity_at_for(tx_date),
                remarks=((data.get('note') or data.get('party_name') or f'Accounts general expense: {cat.name}') or '').strip()
            )
            db.session.add(exp)
            db.session.flush()
            source_type = 'expense'
            source_id = int(exp.id)

        if tx_type in ('project_income', 'client_payment'):
            # Mirror received money into Project module owner receipts when project context exists.
            if project_id:
                prj = Project.query.get(project_id)
                if not prj:
                    return False, 'Valid project is required for owner receipt sync.', []
                pending = _account_pending_snapshot('project_income', 'project', project_id, project_id=project_id, stage_id=stage_id)
                pend = float(pending.get('pending') or 0.0)
                if pend > 0 and amount > (pend + 1e-6):
                    return False, f'Amount exceeds project receivable pending ({pend:,.2f} PKR).', []
                row = OwnerPayment(
                    project_id=prj.id,
                    amount=amount,
                    date=_parse_date(data.get('date')),
                    received_to_account_id=_payload_int(data, 'to_account_id') or None,
                    remarks=(data.get('note') or data.get('party_name') or f'Accounts receipt for {prj.name}'),
                    activity_at=_pkt_now_naive(),
                )
                db.session.add(row)
                db.session.flush()
                source_type = 'owner_payment'
                source_id = int(row.id)

        if split_txn_payloads:
            created = []
            for p in split_txn_payloads:
                ok, msg, rows = _create_account_transaction(p, commit=False)
                if not ok:
                    db.session.rollback()
                    return False, msg, []
                created.extend(rows or [])
            db.session.commit()
            return True, '', created

        if source_type and source_id:
            data['source_type'] = source_type
            data['source_id'] = source_id

        ok, msg, rows = _create_account_transaction(data, commit=False)
        if not ok:
            db.session.rollback()
            return False, msg, []
        db.session.commit()
        return True, '', rows
    except Exception as ex:
        db.session.rollback()
        return False, f'Transaction sync failed: {ex}', []


def _account_dashboard_kpis(date_from=None, date_to=None):
    bal = _account_balance_map()
    rows = _account_rows_active()
    by_id = {int(a.id): a for a in rows}
    company_owned_total = 0.0
    cash_total = 0.0
    bank_total = 0.0
    receivable = 0.0
    payable = 0.0
    for aid, value in bal.items():
        acc = by_id.get(int(aid))
        tp = (acc.type or '').strip().lower() if acc else ''
        if tp in _ACCOUNT_COMPANY_TYPES:
            company_owned_total += float(value or 0.0)
        if tp in ('company', 'cash'):
            cash_total += float(value or 0.0)
        if tp == 'bank':
            bank_total += float(value or 0.0)
        if tp in _ACCOUNT_PROJECT_FLOW_TYPES:
            if float(value or 0.0) > 0:
                receivable += float(value or 0.0)
    _, payable_total = _accounts_payable_breakdown_rows()
    payable = float(payable_total or 0.0)

    q = db.session.query(
        func.coalesce(func.sum(case((func.lower(AccountTransaction.category) == 'income', AccountTransaction.amount), else_=0.0)), 0.0),
        func.coalesce(func.sum(case((func.lower(AccountTransaction.category).in_(('expense', 'purchase', 'payroll', 'advance')), AccountTransaction.amount), else_=0.0)), 0.0)
    ).filter(AccountTransaction.is_void == False)
    if date_from:
        q = q.filter(AccountTransaction.date >= date_from)
    if date_to:
        q = q.filter(AccountTransaction.date <= date_to)
    income_total, expense_total = q.first() or (0.0, 0.0)
    income_total = float(income_total or 0.0)
    expense_total = float(expense_total or 0.0)

    sq = db.session.query(
        func.coalesce(func.sum(case((
            and_(
                func.lower(func.coalesce(AccountTransaction.category, '')).in_(('expense', 'purchase', 'payroll', 'advance')),
                or_(
                    func.lower(func.coalesce(AccountTransaction.type, '')).in_(('payroll', 'expense_wage', 'expense_subcontractor', 'advance_to_person', 'office_management_payment')),
                    func.lower(func.coalesce(AccountTransaction.category, '')).in_(('payroll', 'advance'))
                )
            ),
            AccountTransaction.amount
        ), else_=0.0)), 0.0),
        func.coalesce(func.sum(case((
            and_(
                func.lower(func.coalesce(AccountTransaction.category, '')).in_(('expense', 'purchase', 'payroll', 'advance')),
                func.lower(func.coalesce(AccountTransaction.type, '')) == 'expense_material'
            ),
            AccountTransaction.amount
        ), else_=0.0)), 0.0),
        func.coalesce(func.sum(case((
            and_(
                func.lower(func.coalesce(AccountTransaction.category, '')).in_(('expense', 'purchase', 'payroll', 'advance')),
                func.lower(func.coalesce(AccountTransaction.category, '')) == 'purchase'
            ),
            AccountTransaction.amount
        ), else_=0.0)), 0.0),
    ).filter(AccountTransaction.is_void == False)
    if date_from:
        sq = sq.filter(AccountTransaction.date >= date_from)
    if date_to:
        sq = sq.filter(AccountTransaction.date <= date_to)
    labour_expense_total, material_expense_total, purchased_expense_total = sq.first() or (0.0, 0.0, 0.0)
    labour_expense_total = float(labour_expense_total or 0.0)
    material_expense_total = float(material_expense_total or 0.0)
    purchased_expense_total = float(purchased_expense_total or 0.0)
    entries_q = db.session.query(func.count(AccountTransaction.id)).filter(AccountTransaction.is_void == False)
    if date_from:
        entries_q = entries_q.filter(AccountTransaction.date >= date_from)
    if date_to:
        entries_q = entries_q.filter(AccountTransaction.date <= date_to)
    entries_total = int(entries_q.scalar() or 0)

    return {
        'company_owned_total': float(company_owned_total),
        'cash_total': float(cash_total),
        'bank_total': float(bank_total),
        'receivable_total': float(receivable),
        'payable_total': float(payable),
        'received_total': income_total,
        'spent_total': expense_total,
        'labour_expense_total': labour_expense_total,
        'material_expense_total': material_expense_total,
        'purchased_expense_total': purchased_expense_total,
        'net_cashflow': float(income_total - expense_total),
        'entries_total': entries_total,
        'expense_total': expense_total,
        'income_total': income_total,
        'net_profit': float(income_total - expense_total),
    }


def _accounts_payable_breakdown_rows():
    rows = []
    total = 0.0

    workers = (Worker.query
               .filter(Worker.active_status == True)
               .order_by(Worker.name.asc(), Worker.id.asc())
               .all())
    for w in workers:
        snap = _worker_payable_snapshot(w.id)
        pending = float(snap.get('payable') or 0.0)
        if pending <= 1e-6:
            continue
        rows.append({
            'kind': 'Worker Wages',
            'name': (w.name or f'Worker #{w.id}'),
            'detail': (w.worker_code or f'W-{w.id}'),
            'amount': pending,
        })
        total += pending

    subs = Subcontractor.query.order_by(Subcontractor.name.asc(), Subcontractor.id.asc()).all()
    for s in subs:
        pending = max(0.0, float(s.payable_balance or 0.0))
        if pending <= 1e-6:
            continue
        scope = []
        if getattr(s, 'project', None) and getattr(s.project, 'name', None):
            scope.append(s.project.name)
        if getattr(s, 'stage_rel', None) and getattr(s.stage_rel, 'name', None):
            scope.append(s.stage_rel.name)
        rows.append({
            'kind': 'Subcontractor Wages',
            'name': (s.name or f'Subcontractor #{s.id}'),
            'detail': (' / '.join(scope) if scope else (s.subcontractor_code or f'SUB-{s.id}')),
            'amount': pending,
        })
        total += pending

    debit_map = dict(
        db.session.query(
            SupplierLedger.supplier_id,
            func.coalesce(func.sum(SupplierLedger.amount), 0.0)
        )
        .filter(
            SupplierLedger.is_void == False,
            func.lower(func.coalesce(SupplierLedger.entry_type, '')) == 'debit'
        )
        .group_by(SupplierLedger.supplier_id)
        .all()
    )
    credit_map = dict(
        db.session.query(
            SupplierLedger.supplier_id,
            func.coalesce(func.sum(SupplierLedger.amount), 0.0)
        )
        .filter(
            SupplierLedger.is_void == False,
            func.lower(func.coalesce(SupplierLedger.entry_type, '')) == 'credit'
        )
        .group_by(SupplierLedger.supplier_id)
        .all()
    )
    suppliers = (Supplier.query
                 .filter(Supplier.is_void == False)
                 .order_by(Supplier.name.asc(), Supplier.id.asc())
                 .all())
    for s in suppliers:
        debit = float(debit_map.get(s.id, 0.0) or 0.0)
        credit = float(credit_map.get(s.id, 0.0) or 0.0)
        pending = max(0.0, debit - credit)
        if pending <= 1e-6:
            continue
        rows.append({
            'kind': 'Purchase Pending',
            'name': (s.name or f'Supplier #{s.id}'),
            'detail': (s.phone or '-'),
            'amount': pending,
        })
        total += pending

    rows = sorted(rows, key=lambda r: float(r.get('amount') or 0.0), reverse=True)
    return rows, float(total)


def _account_kpi_detail_context(metric, date_from=None, date_to=None, payable_head='', spent_head=''):
    metric = (metric or '').strip().lower()
    kpis = _account_dashboard_kpis(date_from=date_from, date_to=date_to)
    accounts = _list_accounts_with_balances()
    company_types = _ACCOUNT_COMPANY_TYPES

    def _tx_rows_for_categories(categories):
        q = AccountTransaction.query.filter(AccountTransaction.is_void == False)
        if date_from:
            q = q.filter(AccountTransaction.date >= date_from)
        if date_to:
            q = q.filter(AccountTransaction.date <= date_to)
        cats = [str(c or '').strip().lower() for c in (categories or []) if str(c or '').strip()]
        if cats:
            q = q.filter(func.lower(func.coalesce(AccountTransaction.category, '')).in_(tuple(cats)))
        return q.order_by(AccountTransaction.date.desc(), AccountTransaction.id.desc()).limit(1200).all()

    page_title = 'Accounts KPI Detail'
    subtitle = 'Detailed records'
    grand_total = 0.0
    columns = []
    rows = []
    row_kind = 'generic'
    payable_cards = []
    spent_cards = []
    active_payable_head = (payable_head or '').strip()
    active_spent_head = (spent_head or '').strip()
    valid_metrics = {
        'company_total', 'received', 'spent', 'net', 'cash', 'bank',
        'receivable', 'payable', 'income', 'expenses'
    }
    if metric not in valid_metrics:
        metric = 'company_total'

    if metric in ('company_total', 'cash', 'bank', 'receivable', 'payable'):
        if metric == 'company_total':
            page_title = 'Company-Owned Accounts Total'
            subtitle = 'All active company/cash/bank accounts with current balances'
            items = [a for a in accounts if (a.get('type') in company_types)]
            grand_total = float(kpis.get('company_owned_total') or 0.0)
        elif metric == 'cash':
            page_title = 'Cash Accounts Balance'
            subtitle = 'All active company/cash accounts with current balances'
            items = [a for a in accounts if (a.get('type') in ('company', 'cash'))]
            grand_total = float(kpis.get('cash_total') or 0.0)
        elif metric == 'bank':
            page_title = 'Bank Accounts Balance'
            subtitle = 'All active bank accounts with current balances'
            items = [a for a in accounts if (a.get('type') == 'bank')]
            grand_total = float(kpis.get('bank_total') or 0.0)
        elif metric == 'receivable':
            page_title = 'Receivable Balances'
            subtitle = 'Positive balances in Project-in-Flow (client) accounts'
            items = [a for a in accounts if (a.get('type') in _ACCOUNT_PROJECT_FLOW_TYPES and float(a.get('current_balance') or 0.0) > 0.0)]
            grand_total = float(kpis.get('receivable_total') or 0.0)
        else:
            page_title = 'Payable Balances'
            subtitle = 'Current pending payables from wages, subcontractors, and purchases'
            payable_rows, payable_total = _accounts_payable_breakdown_rows()
            grouped = {}
            for r in (payable_rows or []):
                k = str(r.get('kind') or 'Payable').strip() or 'Payable'
                grouped[k] = float(grouped.get(k, 0.0) or 0.0) + float(r.get('amount') or 0.0)
            payable_cards = [
                {'head': k, 'amount': float(v or 0.0)}
                for k, v in grouped.items()
            ]
            payable_cards = sorted(payable_cards, key=lambda x: float(x.get('amount') or 0.0), reverse=True)
            if active_payable_head:
                head_l = active_payable_head.strip().lower()
                payable_rows = [r for r in payable_rows if str(r.get('kind') or '').strip().lower() == head_l]
                filtered_total = float(sum(float(r.get('amount') or 0.0) for r in payable_rows))
                grand_total = filtered_total
                subtitle = f'{active_payable_head} pending'
            else:
                grand_total = float(payable_total or 0.0)

        if metric == 'payable':
            row_kind = 'payable'
            columns = [
                {'key': 'kind', 'label': 'Payable Head'},
                {'key': 'name', 'label': 'Name'},
                {'key': 'detail', 'label': 'Detail'},
                {'key': 'amount', 'label': 'Pending Amount', 'align': 'text-end', 'format': 'currency'},
            ]
            rows = payable_rows
        else:
            items = sorted(items, key=lambda a: abs(float(a.get('current_balance') or 0.0)), reverse=True)
            row_kind = 'account'
            columns = [
                {'key': 'name', 'label': 'Account'},
                {'key': 'type', 'label': 'Account Class'},
                {'key': 'bank_detail', 'label': 'Bank Detail'},
                {'key': 'opening_balance', 'label': 'Opening', 'align': 'text-end', 'format': 'currency'},
                {'key': 'current_balance', 'label': 'Current', 'align': 'text-end', 'format': 'currency'},
            ]
            rows = []
            for a in items:
                curr = float(a.get('current_balance') or 0.0)
                b_detail = '-'
                if a.get('type') == 'bank':
                    b_detail = f"{a.get('bank_name') or '-'} / {a.get('account_number') or '-'}"
                rows.append({
                    'name': a.get('name') or '-',
                    'type': str(a.get('account_group') or '').replace('_', ' ').title(),
                    'bank_detail': b_detail,
                    'opening_balance': float(a.get('opening_balance') or 0.0),
                    'current_balance': curr,
                })
    else:
        def _spent_bucket(tx):
            tx_type = str(getattr(tx, 'type', '') or '').strip().lower()
            tx_cat = str(getattr(tx, 'category', '') or '').strip().lower()
            rel_type = str(getattr(tx, 'related_entity_type', '') or '').strip().lower()
            source_type = str(getattr(tx, 'source_type', '') or '').strip().lower()
            if tx_type in ('payroll', 'expense_wage', 'advance_to_person'):
                return 'labour_workers'
            if tx_type == 'expense_material' or tx_cat == 'purchase':
                return 'material'
            if (tx_type == 'office_management_payment' and rel_type == 'office_staff') or source_type.startswith('office_staff_ledger_'):
                return 'office_staff'
            if (tx_type == 'office_management_payment') or source_type.startswith('office_expense'):
                return 'office_expenses'
            if getattr(tx, 'project_id', None) is not None:
                return 'project_expenses'
            return 'other'

        if metric in ('received', 'income'):
            page_title = 'Total Received'
            subtitle = 'Income transactions in selected date range'
            tx_rows = _tx_rows_for_categories(('income',))
            grand_total = float(kpis.get('received_total') or 0.0)
        elif metric in ('spent', 'expenses'):
            page_title = 'Total Spent'
            subtitle = 'Expense, purchase, payroll, and advance transactions in selected date range'
            tx_rows = _tx_rows_for_categories(('expense', 'purchase', 'payroll', 'advance'))
            grand_total = float(kpis.get('spent_total') or 0.0)
            spent_q = AccountTransaction.query.filter(
                AccountTransaction.is_void == False,
                func.lower(func.coalesce(AccountTransaction.category, '')).in_(('expense', 'purchase', 'payroll', 'advance'))
            )
            if date_from:
                spent_q = spent_q.filter(AccountTransaction.date >= date_from)
            if date_to:
                spent_q = spent_q.filter(AccountTransaction.date <= date_to)
            spent_rows = spent_q.all()
            labour_workers_spent = 0.0
            material_spent = 0.0
            office_staff_spent = 0.0
            office_expenses_spent = 0.0
            project_expenses_spent = 0.0
            for tx in spent_rows:
                amount = float(getattr(tx, 'amount', 0.0) or 0.0)
                bucket = _spent_bucket(tx)
                if bucket == 'labour_workers':
                    labour_workers_spent += amount
                elif bucket == 'material':
                    material_spent += amount
                elif bucket == 'office_staff':
                    office_staff_spent += amount
                elif bucket == 'office_expenses':
                    office_expenses_spent += amount
                elif bucket == 'project_expenses':
                    project_expenses_spent += amount
            spent_cards = [
                {'key': 'all', 'head': 'Total Spent', 'amount': float(grand_total or 0.0), 'tone': 'kpi-red'},
                {'key': 'labour_workers', 'head': 'Labour / Workers', 'amount': float(labour_workers_spent or 0.0), 'tone': 'kpi-orange'},
                {'key': 'material', 'head': 'Material', 'amount': float(material_spent or 0.0), 'tone': 'kpi-teal'},
                {'key': 'office_staff', 'head': 'Office Staff', 'amount': float(office_staff_spent or 0.0), 'tone': 'kpi-blue'},
                {'key': 'office_expenses', 'head': 'Office Expenses', 'amount': float(office_expenses_spent or 0.0), 'tone': 'kpi-purple'},
                {'key': 'project_expenses', 'head': 'Project Expenses', 'amount': float(project_expenses_spent or 0.0), 'tone': 'kpi-teal'},
            ]
            if active_spent_head and active_spent_head != 'all':
                allowed = {'labour_workers', 'material', 'office_staff', 'office_expenses', 'project_expenses'}
                selected = active_spent_head.strip().lower()
                if selected in allowed:
                    tx_rows = [t for t in tx_rows if _spent_bucket(t) == selected]
                    selected_card = next((c for c in spent_cards if str(c.get('key') or '').lower() == selected), None)
                    if selected_card:
                        grand_total = float(selected_card.get('amount') or 0.0)
                        subtitle = f'{selected_card.get("head") or "Spent"} in selected date range'
        else:
            page_title = 'Net Cashflow'
            subtitle = 'Income minus spent (expense/purchase/payroll/advance)'
            tx_rows = _tx_rows_for_categories(('income', 'expense', 'purchase', 'payroll', 'advance'))
            grand_total = float(kpis.get('net_cashflow') or 0.0)

        row_kind = 'transaction'
        columns = [
            {'key': 'date', 'label': 'Date'},
            {'key': 'type', 'label': 'Type'},
            {'key': 'category', 'label': 'Category'},
            {'key': 'from_account', 'label': 'From'},
            {'key': 'to_account', 'label': 'To / Party'},
            {'key': 'project', 'label': 'Project'},
            {'key': 'stage', 'label': 'Stage'},
            {'key': 'amount', 'label': 'Amount', 'align': 'text-end', 'format': 'currency'},
            {'key': 'note', 'label': 'Note'},
            {'key': 'reference_id', 'label': 'Reference'},
        ]
        rows = []
        for r in tx_rows:
            amt = float(r.amount or 0.0)
            if metric == 'net' and str(r.category or '').strip().lower() != 'income':
                amt = -amt
            rows.append({
                'date': (r.date.isoformat() if r.date else ''),
                'type': str(r.type or '').replace('_', ' ').title(),
                'category': str(r.category or '').title(),
                'from_account': (r.from_account.name if r.from_account else '-'),
                'to_account': ((r.to_account.name if r.to_account else '') or (r.party_name or '-')),
                'project': (r.project.name if r.project else '-'),
                'stage': (r.stage.name if r.stage else '-'),
                'amount': amt,
                'note': (r.note or '-'),
                'reference_id': (r.reference_id or '-'),
            })

    return {
        'metric': metric,
        'page_title': page_title,
        'subtitle': subtitle,
        'columns': columns,
        'rows': rows,
        'row_kind': row_kind,
        'grand_total': float(grand_total or 0.0),
        'payable_cards': payable_cards,
        'spent_cards': spent_cards,
        'active_payable_head': active_payable_head,
        'active_spent_head': active_spent_head,
    }


def _account_dashboard_subgroups(metric):
    metric = (metric or '').strip().lower()
    rows = _list_accounts_with_balances()
    out = []
    if metric == 'cash':
        for a in rows:
            if a['type'] in ('company', 'cash'):
                out.append({'label': a['name'], 'value': float(a['current_balance'] or 0.0), 'account_id': int(a['id'])})
    elif metric == 'bank':
        for a in rows:
            if a['type'] == 'bank':
                out.append({'label': a['name'], 'value': float(a['current_balance'] or 0.0), 'account_id': int(a['id'])})
    elif metric == 'receivable':
        for a in rows:
            if a['type'] in _ACCOUNT_PROJECT_FLOW_TYPES and float(a['current_balance'] or 0.0) > 0:
                out.append({'label': a['name'], 'value': float(a['current_balance'] or 0.0), 'account_id': int(a['id'])})
    elif metric == 'payable':
        payable_rows, _ = _accounts_payable_breakdown_rows()
        grouped = {}
        for r in payable_rows:
            k = str(r.get('kind') or 'Payable')
            grouped[k] = float(grouped.get(k, 0.0) or 0.0) + float(r.get('amount') or 0.0)
        for k, v in grouped.items():
            out.append({'label': k, 'value': float(v or 0.0), 'tx_type': ''})
    elif metric in ('expenses', 'income', 'net'):
        q = (db.session.query(
            func.lower(func.coalesce(AccountTransaction.type, '')).label('tx_type'),
            func.coalesce(func.sum(AccountTransaction.amount), 0.0).label('amt')
        )
        .filter(AccountTransaction.is_void == False)
        .group_by(func.lower(func.coalesce(AccountTransaction.type, ''))))
        if metric == 'income':
            q = q.filter(func.lower(AccountTransaction.category) == 'income')
        elif metric == 'expenses':
            q = q.filter(func.lower(AccountTransaction.category).in_(('expense', 'purchase', 'payroll', 'advance')))
        for tx_type, amt in q.all():
            out.append({'label': tx_type or 'unknown', 'value': float(amt or 0.0), 'tx_type': tx_type or ''})
    out.sort(key=lambda x: float(x.get('value', 0.0) or 0.0), reverse=True)
    return out


def _account_running_balance_rows(history_rows, account_id=None):
    if not account_id:
        return {int(r.id): None for r in history_rows}
    ordered = (AccountTransaction.query
               .filter(
                   AccountTransaction.is_void == False,
                   or_(AccountTransaction.from_account_id == int(account_id), AccountTransaction.to_account_id == int(account_id))
               )
               .order_by(AccountTransaction.date.asc(), AccountTransaction.id.asc())
               .all())
    bal = 0.0
    run_map = {}
    base_opening = float(Account.query.get(int(account_id)).opening_balance or 0.0) if account_id else 0.0
    bal += base_opening
    for r in ordered:
        if int(r.from_account_id or 0) == int(account_id):
            bal -= float(r.amount or 0.0)
        if int(r.to_account_id or 0) == int(account_id):
            bal += float(r.amount or 0.0)
        run_map[int(r.id)] = float(bal)
    return {int(r.id): run_map.get(int(r.id)) for r in history_rows}


def _accounts_post_transaction(payload, source_type=None, source_id=None, commit=False):
    if source_type:
        payload = dict(payload or {})
        payload['source_type'] = source_type
        payload['source_id'] = source_id
    return _create_account_transaction(payload or {}, commit=commit)


def _accounts_upsert_purchase_paid_txn(purchase_row, supplier_name='', commit=False):
    if not purchase_row:
        return False, 'Purchase row is required.', []
    company = _accounts_default_company_cash()
    supplier_acc = _accounts_party_account(supplier_name, 'vendor')
    tx_date = (purchase_row.date.isoformat() if purchase_row.date else _pkt_today().isoformat())
    existing = (AccountTransaction.query
                .filter(
                    AccountTransaction.source_id == int(purchase_row.id),
                    func.lower(func.coalesce(AccountTransaction.source_type, '')).in_(('purchase_v2_paid:direct', 'purchase_v2_paid:step2'))
                )
                .order_by(AccountTransaction.id.desc())
                .first())
    if existing:
        existing.date = _parse_date(tx_date)
        existing.type = 'purchase'
        existing.amount = float(purchase_row.total_amount or 0.0)
        existing.from_account_id = (company.id if company else existing.from_account_id)
        existing.to_account_id = (supplier_acc.id if supplier_acc else existing.to_account_id)
        existing.executed_by_account_id = (company.id if company else existing.executed_by_account_id)
        existing.related_entity_type = 'supplier'
        existing.related_entity_id = int(purchase_row.supplier_id or 0) or None
        existing.party_name = supplier_name or existing.party_name
        existing.category = 'purchase'
        existing.note = f'Purchase #{purchase_row.id} marked paid'
        existing.reference_id = f'purchase_v2#{purchase_row.id}'
        existing.is_void = False
        # If split transaction exists (step1), keep same group active.
        grp = (existing.group_id or '').strip()
        if grp:
            for r in AccountTransaction.query.filter(AccountTransaction.group_id == grp).all():
                r.is_void = False
        if commit:
            db.session.commit()
        return True, '', [existing]

    payload = {
        'date': tx_date,
        'type': 'purchase',
        'amount': float(purchase_row.total_amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (supplier_acc.id if supplier_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'related_entity_type': 'supplier',
        'related_entity_id': purchase_row.supplier_id,
        'party_name': supplier_name,
        'category': 'purchase',
        'note': f'Purchase #{purchase_row.id} marked paid',
        'reference_id': f'purchase_v2#{purchase_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='purchase_v2_paid', source_id=purchase_row.id, commit=commit)


def _accounts_party_account(label, acc_type='person'):
    nm = _normalize_name_ci(label)
    if not nm:
        return _accounts_default_external_parties()
    source = 'worker' if acc_type == 'person' else acc_type
    return _account_get_or_create(nm, acc_type, auto_generated=True, auto_source=source)


def _accounts_post_owner_receipt(owner_payment_row, project=None, commit=False):
    company = _accounts_default_company_cash()
    recv_id = int(getattr(owner_payment_row, 'received_to_account_id', 0) or 0)
    if recv_id:
        recv_acc = Account.query.get(recv_id)
        if recv_acc and (not recv_acc.is_void) and str(recv_acc.status or 'active').strip().lower() == 'active' \
           and str(recv_acc.type or '').strip().lower() in _ACCOUNT_COMPANY_TYPES:
            company = recv_acc
    client_name = _normalize_name_ci((project.client if project else '') or 'Client')
    client_acc = _accounts_party_account(client_name, 'client')
    payload = {
        'date': (owner_payment_row.date.isoformat() if owner_payment_row and owner_payment_row.date else _pkt_today().isoformat()),
        'type': 'project_income',
        'amount': float(owner_payment_row.amount or 0.0),
        'from_account_id': (client_acc.id if client_acc else None),
        'to_account_id': (company.id if company else None),
        'executed_by_account_id': (client_acc.id if client_acc else None),
        'project_id': (owner_payment_row.project_id if owner_payment_row else None),
        'related_entity_type': 'project',
        'related_entity_id': (owner_payment_row.project_id if owner_payment_row else None),
        'party_name': (project.name if project else 'Owner'),
        'category': 'income',
        'note': f'Owner/client receipt #{owner_payment_row.id}',
        'reference_id': f'owner_payment#{owner_payment_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='owner_payment', source_id=(owner_payment_row.id if owner_payment_row else None), commit=commit)


def _accounts_post_expense_row(expense_row, commit=False):
    company = _accounts_default_company_cash()
    ext = _accounts_default_external_parties()
    cat_name = (expense_row.expense_category.name if expense_row and expense_row.expense_category else '')
    tx_type = 'expense_material' if ('material' in (cat_name or '').strip().lower()) else 'expense_general'
    payload = {
        'date': (expense_row.date.isoformat() if expense_row and expense_row.date else _pkt_today().isoformat()),
        'type': tx_type,
        'amount': float(expense_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (ext.id if ext else None),
        'executed_by_account_id': (company.id if company else None),
        'project_id': (expense_row.project_id if expense_row else None),
        'stage_id': (expense_row.stage_id if expense_row else None),
        'party_name': (expense_row.remarks or cat_name or 'Expense Party'),
        'category': 'expense',
        'note': f'Expense posting #{expense_row.id}',
        'reference_id': f'expense#{expense_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='expense', source_id=(expense_row.id if expense_row else None), commit=commit)


def _accounts_post_personal_expense_row(personal_expense_row, commit=False):
    company = _accounts_default_company_cash()
    ext = _accounts_default_external_parties()
    beneficiary_name = (personal_expense_row.beneficiary_name or '').strip()
    beneficiary_acc = _accounts_party_account(beneficiary_name, 'person')
    payload = {
        'date': (personal_expense_row.date.isoformat() if personal_expense_row and personal_expense_row.date else _pkt_today().isoformat()),
        # Personal disbursements are non-project payouts; use party_payment to avoid project/stage scope enforcement.
        'type': 'party_payment',
        'amount': float(personal_expense_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (beneficiary_acc.id if beneficiary_acc else ext.id if ext else None),
        'executed_by_account_id': (company.id if company else None),
        'party_name': beneficiary_name,
        'category': 'personal',
        'note': f'Personal expense: {personal_expense_row.category} - {personal_expense_row.remarks or ""}',
        'reference_id': f'personal_expense#{personal_expense_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='personal_expense', source_id=(personal_expense_row.id if personal_expense_row else None), commit=commit)


def _accounts_post_labour_ledger_row(ledger_row, worker_name='', commit=False):
    et = (ledger_row.entry_type or '').strip().lower()
    if et not in ('payment', 'tip', 'advance'):
        return True, '', []
    company = _accounts_default_company_cash()
    worker_label = _normalize_name_ci(worker_name) or f'Worker #{ledger_row.worker_id}'
    worker_acc = _accounts_party_account(worker_label, 'person')
    tx_type = 'advance_to_person' if et == 'advance' else 'payroll'
    category = 'advance' if et == 'advance' else 'payroll'
    payload = {
        'date': (ledger_row.date.isoformat() if ledger_row and ledger_row.date else _pkt_today().isoformat()),
        'type': tx_type,
        'amount': float(ledger_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (worker_acc.id if worker_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'project_id': (ledger_row.project_id if ledger_row else None),
        'stage_id': (ledger_row.stage_id if ledger_row else None),
        'related_entity_type': 'worker',
        'related_entity_id': (ledger_row.worker_id if ledger_row else None),
        'party_name': worker_label,
        'category': category,
        'note': (ledger_row.notes or f'Labour {et}'),
        'reference_id': f'labour_ledger#{ledger_row.id}',
    }
    return _accounts_post_transaction(payload, source_type=f'labour_ledger_{et}', source_id=(ledger_row.id if ledger_row else None), commit=commit)


def _accounts_upsert_labour_ledger_txn(worker_row, ledger_row, commit=False):
    if (not worker_row) or (not ledger_row) or bool(getattr(ledger_row, 'is_void', False)):
        return True, '', []
    et = (ledger_row.entry_type or '').strip().lower()
    if et not in ('payment', 'tip', 'advance'):
        return True, '', []
    st = f'labour_ledger_{et}'
    existing = (AccountTransaction.query
                .filter(
                    AccountTransaction.source_id == int(ledger_row.id),
                    or_(
                        func.lower(func.coalesce(AccountTransaction.source_type, '')) == f'{st}:direct',
                        func.lower(func.coalesce(AccountTransaction.source_type, '')).like(f'{st}:%')
                    )
                )
                .order_by(AccountTransaction.id.desc())
                .first())
    company = _accounts_default_company_cash()
    worker_label = _normalize_name_ci(getattr(worker_row, 'name', '') or f'Worker #{ledger_row.worker_id}')
    worker_acc = _accounts_party_account(worker_label, 'person')
    if existing:
        existing.date = (ledger_row.date or existing.date or _pkt_today())
        existing.type = 'advance_to_person' if et == 'advance' else 'payroll'
        existing.amount = float(ledger_row.amount or 0.0)
        existing.from_account_id = int(company.id if company else existing.from_account_id)
        existing.to_account_id = int(worker_acc.id if worker_acc else (existing.to_account_id or 0)) or None
        existing.executed_by_account_id = int(company.id if company else existing.executed_by_account_id)
        existing.project_id = (ledger_row.project_id if ledger_row.project_id else None)
        existing.stage_id = (ledger_row.stage_id if ledger_row.stage_id else None)
        existing.related_entity_type = 'worker'
        existing.related_entity_id = int(getattr(ledger_row, 'worker_id', 0) or 0) or None
        existing.party_name = worker_label
        existing.category = 'advance' if et == 'advance' else 'payroll'
        existing.note = (ledger_row.notes or f'Labour {et}')
        existing.reference_id = f'labour_ledger#{ledger_row.id}'
        existing.is_void = False
        if commit:
            db.session.commit()
        return True, '', [existing]
    payload = {
        'date': ((ledger_row.date or _pkt_today()).isoformat()),
        'type': 'advance_to_person' if et == 'advance' else 'payroll',
        'amount': float(ledger_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (worker_acc.id if worker_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'project_id': (ledger_row.project_id if ledger_row else None),
        'stage_id': (ledger_row.stage_id if ledger_row else None),
        'related_entity_type': 'worker',
        'related_entity_id': (ledger_row.worker_id if ledger_row else None),
        'party_name': worker_label,
        'category': 'advance' if et == 'advance' else 'payroll',
        'note': (ledger_row.notes or f'Labour {et}'),
        'reference_id': f'labour_ledger#{ledger_row.id}',
    }
    return _accounts_post_transaction(payload, source_type=st, source_id=ledger_row.id, commit=commit)


def _accounts_post_supplier_credit_row(supplier_ledger_row, supplier_name='', commit=False):
    company = _accounts_default_company_cash()
    label = _normalize_name_ci(supplier_name) or f'Supplier #{supplier_ledger_row.supplier_id}'
    supplier_acc = _accounts_party_account(label, 'vendor')
    payload = {
        'date': _pkt_today().isoformat(),
        'type': 'purchase',
        'amount': float(supplier_ledger_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (supplier_acc.id if supplier_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'related_entity_type': 'supplier',
        'related_entity_id': (supplier_ledger_row.supplier_id if supplier_ledger_row else None),
        'party_name': label,
        'category': 'purchase',
        'note': (supplier_ledger_row.note or 'Supplier payment'),
        'reference_id': f'supplier_ledger#{supplier_ledger_row.id}',
    }
    source = f"supplier_credit_{(supplier_ledger_row.reference_type or 'payment').strip().lower()}"
    return _accounts_post_transaction(payload, source_type=source, source_id=(supplier_ledger_row.id if supplier_ledger_row else None), commit=commit)


def _accounts_post_subcontract_payment_row(payment_row, subcontractor_name='', commit=False):
    if payment_row is None or bool(getattr(payment_row, 'is_void', False)):
        return True, '', []
    et = (payment_row.entry_type or '').strip().lower()
    if et != 'payment':
        return True, '', []
    company = _accounts_default_company_cash()
    label = _normalize_name_ci(subcontractor_name) or f'Subcontractor #{payment_row.subcontractor_id}'
    sub_acc = _accounts_party_account(label, 'person')
    tx_type = 'expense_subcontractor' if (payment_row and payment_row.project_id) else 'expense_general'
    payload = {
        'date': (payment_row.date.isoformat() if payment_row and payment_row.date else _pkt_today().isoformat()),
        'type': tx_type,
        'amount': float(payment_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (sub_acc.id if sub_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'project_id': (payment_row.project_id if payment_row else None),
        'stage_id': (payment_row.stage_id if payment_row else None),
        'related_entity_type': 'subcontractor',
        'related_entity_id': (payment_row.subcontractor_id if payment_row else None),
        'party_name': label,
        'category': 'expense',
        'note': (payment_row.notes or 'Subcontractor payment'),
        'reference_id': f'subcontract_payment#{payment_row.id}',
    }
    source = f'subcontract_payment_{et}'
    return _accounts_post_transaction(payload, source_type=source, source_id=(payment_row.id if payment_row else None), commit=commit)


def _accounts_post_subcontract_labour_payment_row(payment_row, worker_name='', subcontractor_name='', commit=False):
    if payment_row is None or bool(getattr(payment_row, 'is_void', False)):
        return True, '', []
    company = _accounts_default_company_cash()
    label = _normalize_name_ci(worker_name) or f'Sub-Labour Worker #{payment_row.worker_id}'
    worker_acc = _accounts_party_account(label, 'person')
    sub_label = _normalize_name_ci(subcontractor_name) or f'Subcontractor #{payment_row.subcontractor_id}'
    payload = {
        'date': (payment_row.date.isoformat() if payment_row and payment_row.date else _pkt_today().isoformat()),
        'type': 'payroll',
        'amount': float(payment_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (worker_acc.id if worker_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'related_entity_type': 'subcontractor_labour_worker',
        'related_entity_id': (payment_row.worker_id if payment_row else None),
        'party_name': label,
        'category': 'payroll',
        'note': (payment_row.notes or f'Sub-labour payment: {label} via {sub_label}'),
        'reference_id': f'sub_labour_payment#{payment_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='subcontract_labour_payment', source_id=(payment_row.id if payment_row else None), commit=commit)


def _accounts_upsert_office_staff_ledger_txn(staff_row, ledger_row, commit=False):
    if (not staff_row) or (not ledger_row) or bool(getattr(ledger_row, 'is_void', False)):
        return True, '', []
    et = (ledger_row.entry_type or '').strip().lower()
    if et not in ('advance', 'payment', 'tip'):
        return True, '', []
    st = f'office_staff_ledger_{et}'
    existing = (AccountTransaction.query
                .filter(
                    AccountTransaction.source_id == int(ledger_row.id),
                    or_(
                        func.lower(func.coalesce(AccountTransaction.source_type, '')) == f'{st}:direct',
                        func.lower(func.coalesce(AccountTransaction.source_type, '')).like(f'{st}:%')
                    )
                )
                .order_by(AccountTransaction.id.desc())
                .first())
    company = _accounts_default_company_cash()
    staff_label = _normalize_name_ci(getattr(staff_row, 'name', '') or f'Office Staff #{ledger_row.staff_id}')
    staff_acc = _accounts_party_account(staff_label, 'person')
    if existing:
        existing.date = (ledger_row.date or existing.date or _pkt_today())
        existing.amount = float(ledger_row.amount or 0.0)
        existing.type = 'office_management_payment'
        existing.from_account_id = int(company.id if company else existing.from_account_id)
        existing.to_account_id = int(staff_acc.id if staff_acc else (existing.to_account_id or 0)) or None
        existing.executed_by_account_id = int(company.id if company else existing.executed_by_account_id)
        existing.project_id = None
        existing.stage_id = None
        existing.related_entity_type = 'office_staff'
        existing.related_entity_id = int(getattr(ledger_row, 'staff_id', 0) or 0) or None
        existing.party_name = staff_label
        existing.category = 'expense'
        existing.note = (ledger_row.notes or f'Office staff {et}')
        existing.reference_id = f'office_staff_ledger#{ledger_row.id}'
        existing.is_void = False
        if commit:
            db.session.commit()
        return True, '', [existing]
    payload = {
        'date': ((ledger_row.date or _pkt_today()).isoformat()),
        'type': 'office_management_payment',
        'amount': float(ledger_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (staff_acc.id if staff_acc else None),
        'executed_by_account_id': (company.id if company else None),
        'related_entity_type': 'office_staff',
        'related_entity_id': int(getattr(ledger_row, 'staff_id', 0) or 0) or None,
        'party_name': staff_label,
        'category': 'expense',
        'note': (ledger_row.notes or f'Office staff {et}'),
        'reference_id': f'office_staff_ledger#{ledger_row.id}',
    }
    return _accounts_post_transaction(payload, source_type=st, source_id=ledger_row.id, commit=commit)


def _accounts_upsert_office_expense_txn(expense_row, commit=False):
    if (not expense_row) or bool(getattr(expense_row, 'is_void', False)):
        return True, '', []
    existing = (AccountTransaction.query
                .filter(
                    AccountTransaction.source_id == int(expense_row.id),
                    or_(
                        func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'office_expense:direct',
                        func.lower(func.coalesce(AccountTransaction.source_type, '')).like('office_expense:%')
                    )
                )
                .order_by(AccountTransaction.id.desc())
                .first())
    company = _accounts_default_company_cash()
    ext = _accounts_default_external_parties()
    party = _normalize_name_ci(
        (expense_row.category or 'Office Expense') + (f" | {expense_row.remarks}" if (expense_row.remarks or '').strip() else '')
    )[:120]
    if existing:
        existing.date = (expense_row.date or existing.date or _pkt_today())
        existing.amount = float(expense_row.amount or 0.0)
        existing.type = 'office_management_payment'
        existing.from_account_id = int(company.id if company else existing.from_account_id)
        existing.to_account_id = int(ext.id if ext else (existing.to_account_id or 0)) or None
        existing.executed_by_account_id = int(company.id if company else existing.executed_by_account_id)
        existing.project_id = None
        existing.stage_id = None
        existing.related_entity_type = None
        existing.related_entity_id = None
        existing.party_name = party
        existing.category = 'expense'
        existing.note = (expense_row.remarks or f'Office expense: {expense_row.category or "-"}')
        existing.reference_id = f'office_expense#{expense_row.id}'
        existing.is_void = False
        if commit:
            db.session.commit()
        return True, '', [existing]
    payload = {
        'date': ((expense_row.date or _pkt_today()).isoformat()),
        'type': 'office_management_payment',
        'amount': float(expense_row.amount or 0.0),
        'from_account_id': (company.id if company else None),
        'to_account_id': (ext.id if ext else None),
        'executed_by_account_id': (company.id if company else None),
        'party_name': party,
        'category': 'expense',
        'note': (expense_row.remarks or f'Office expense: {expense_row.category or "-"}'),
        'reference_id': f'office_expense#{expense_row.id}',
    }
    return _accounts_post_transaction(payload, source_type='office_expense', source_id=expense_row.id, commit=commit)


def _sync_supplier_po_payment_status(supplier_id):
    """FIFO auto-update PurchaseV2.payment_status after a supplier payment."""
    pos = (PurchaseV2.query
           .filter(PurchaseV2.supplier_id == supplier_id, PurchaseV2.is_void == False)
           .order_by(PurchaseV2.order_date, PurchaseV2.id)
           .all())
    total_credits = float(db.session.query(func.coalesce(func.sum(SupplierLedger.amount), 0.0))
                          .filter(
                              SupplierLedger.supplier_id == supplier_id,
                              SupplierLedger.entry_type == 'credit',
                              SupplierLedger.is_void == False
                          )
                          .scalar() or 0.0)
    remaining = total_credits
    for po in pos:
        po_total = float(po.total_amount or 0.0)
        if po_total <= 0:
            continue
        if remaining >= po_total - 0.01:
            po.payment_status = 'paid'
            remaining -= po_total
        else:
            po.payment_status = 'unpaid'


def _accounts_default_company_cash():
    return _account_get_or_create('Company Cash', 'company', opening_balance=0.0)


def _accounts_default_external_parties():
    # Backward-compatible function name; standardized control account label.
    row = _account_get_or_create('Credit/Debit Control', 'person', opening_balance=0.0)
    if row:
        return row
    # Fallback to legacy name if create/get fails unexpectedly.
    return _account_get_or_create('External Parties', 'person', opening_balance=0.0)


def _mark_auto_generated_person_accounts():
    # Best-effort labeling for already-created worker/person accounts.
    worker_names = {(_normalize_name_ci(w.name)).lower() for w in Worker.query.filter_by(active_status=True).all() if _normalize_name_ci(w.name)}
    if not worker_names:
        return
    rows = (Account.query
            .filter(
                Account.is_void == False,
                func.lower(func.coalesce(Account.type, '')) == 'person',
                or_(Account.auto_generated == False, Account.auto_generated.is_(None))
            ).all())
    changed = 0
    for a in rows:
        nm = (_normalize_name_ci(a.name)).lower()
        if nm in worker_names:
            a.auto_generated = True
            a.auto_source = a.auto_source or 'worker'
            changed += 1
    if changed:
        db.session.commit()


def _run_accounts_backfill():
    company = _accounts_default_company_cash()
    external = _accounts_default_external_parties()
    if (not company) or (not external):
        return {'created': 0, 'skipped': 0, 'errors': 1}

    created = 0
    skipped = 0
    errors = 0

    # Owner receipts (incoming): Credit/Debit control -> Company
    for r in OwnerPayment.query.filter(OwnerPayment.is_void == False).order_by(OwnerPayment.id.asc()).all():
        st = 'owner_payment'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        recv = Account.query.get(int(r.received_to_account_id or 0)) if getattr(r, 'received_to_account_id', None) else None
        recv_id = (int(recv.id) if recv and (not recv.is_void)
                   and str(recv.status or 'active').strip().lower() == 'active'
                   and str(recv.type or '').strip().lower() in _ACCOUNT_COMPANY_TYPES
                   else int(company.id))
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': 'project_income',
            'from_account_id': external.id,
            'to_account_id': recv_id,
            'executed_by_account_id': external.id,
            'project_id': (r.project_id or None),
            'stage_id': None,
            'party_name': (r.project.name if r.project else 'Owner'),
            'category': 'income',
            'note': f'Backfill owner receipt #{r.id}',
            'reference_id': f'owner_payment#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-owner-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Expenses: Company -> Credit/Debit control
    for r in Expense.query.filter(Expense.is_void == False).order_by(Expense.id.asc()).all():
        if float(r.amount or 0.0) <= 0:
            skipped += 1
            continue
        st = 'expense'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        party = _normalize_name_ci(
            (r.expense_category.name if r.expense_category else '')
            or (r.remarks or '')
            or 'Expense Party'
        )
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': 'expense_general',
            'from_account_id': company.id,
            'executed_by_account_id': company.id,
            'project_id': (r.project_id or None),
            'stage_id': (r.stage_id or None),
            'party_name': party,
            'category': 'expense',
            'note': f'Backfill expense #{r.id}',
            'reference_id': f'expense#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-exp-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Office staff ledger cash entries: Company -> Office Staff
    for r in OfficeStaffLedger.query.filter(OfficeStaffLedger.is_void == False).order_by(OfficeStaffLedger.id.asc()).all():
        et = (r.entry_type or '').strip().lower()
        if et not in ('advance', 'payment', 'tip'):
            continue
        st = f'office_staff_ledger_{et}'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        staff = OfficeStaff.query.get(int(r.staff_id or 0))
        party = _normalize_name_ci(staff.name if staff else f'Office Staff #{r.staff_id}') or 'Office Staff'
        staff_acc = _accounts_party_account(party, 'person')
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': 'party_payment',
            'from_account_id': company.id,
            'to_account_id': (staff_acc.id if staff_acc else None),
            'executed_by_account_id': company.id,
            'party_name': party,
            'category': 'expense',
            'note': f'Backfill office staff ledger #{r.id} ({et})',
            'reference_id': f'office_staff_ledger#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-offstaff-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Office expenses: Company -> Credit/Debit control
    for r in OfficeExpense.query.filter(OfficeExpense.is_void == False).order_by(OfficeExpense.id.asc()).all():
        if float(r.amount or 0.0) <= 0:
            skipped += 1
            continue
        if int(getattr(r, 'office_staff_ledger_id', 0) or 0):
            # Already represented by office staff ledger cash postings.
            skipped += 1
            continue
        st = 'office_expense'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': 'party_payment',
            'from_account_id': company.id,
            'to_account_id': external.id,
            'executed_by_account_id': company.id,
            'party_name': _normalize_name_ci((r.category or 'Office Expense') + (f' | {r.remarks}' if (r.remarks or '').strip() else '')),
            'category': 'expense',
            'note': f'Backfill office expense #{r.id}',
            'reference_id': f'office_expense#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-offexp-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Labour ledger cash payouts/advances/tips: Company -> Worker
    for r in LabourLedger.query.filter(LabourLedger.is_void == False).order_by(LabourLedger.id.asc()).all():
        et = (r.entry_type or '').strip().lower()
        # Settlement entries are non-cash adjustments and must not create Accounts cash transactions.
        if et not in ('payment', 'tip', 'advance'):
            continue
        st = f'labour_ledger_{et}'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        party = (r.worker.name if r.worker else f'Worker #{r.worker_id or ""}').strip() or 'Worker'
        worker_acc = _accounts_party_account(party, 'person')
        cat = 'advance' if et == 'advance' else 'payroll'
        tx_type = 'advance_to_person' if et == 'advance' else 'payroll'
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': tx_type,
            'from_account_id': company.id,
            'to_account_id': (worker_acc.id if worker_acc else None),
            'executed_by_account_id': company.id,
            'project_id': (r.project_id or None),
            'stage_id': (r.stage_id or None),
            'related_entity_type': 'worker',
            'related_entity_id': (r.worker_id or None),
            'party_name': party,
            'category': cat,
            'note': f'Backfill labour ledger #{r.id} ({et})',
            'reference_id': f'labour_ledger#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-lab-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Subcontract payments: Company -> Subcontractor
    for r in SubcontractPayment.query.filter(SubcontractPayment.is_void == False).order_by(SubcontractPayment.id.asc()).all():
        et = (r.entry_type or 'payment').strip().lower()
        st = f'subcontract_payment_{et}'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        party = (r.subcontractor.name if r.subcontractor else f'Subcontractor #{r.subcontractor_id or ""}').strip() or 'Subcontractor'
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.amount or 0.0),
            'type': 'expense_subcontractor',
            'from_account_id': company.id,
            'executed_by_account_id': company.id,
            'project_id': (r.project_id or (r.subcontractor.project_id if r.subcontractor else None) or None),
            'stage_id': (r.stage_id or (r.subcontractor.stage_id if r.subcontractor else None) or None),
            'related_entity_type': 'subcontractor',
            'related_entity_id': (r.subcontractor_id or None),
            'party_name': party,
            'category': 'expense',
            'note': f'Backfill subcontract payment #{r.id} ({et})',
            'reference_id': f'subcontract_payment#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-subpay-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Supplier credits (payments): Company -> Supplier
    for r in SupplierLedger.query.filter(
        SupplierLedger.is_void == False,
        func.lower(SupplierLedger.entry_type) == 'credit'
    ).order_by(SupplierLedger.id.asc()).all():
        rt = (r.reference_type or 'payment').strip().lower()
        st = f'supplier_credit_{rt}'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        party = (r.supplier.name if r.supplier else f'Supplier #{r.supplier_id or ""}').strip() or 'Supplier'
        payload = {
            'date': (_pkt_today().isoformat()),
            'amount': float(r.amount or 0.0),
            'type': 'purchase',
            'from_account_id': company.id,
            'executed_by_account_id': company.id,
            'related_entity_type': 'supplier',
            'related_entity_id': (r.supplier_id or None),
            'party_name': party,
            'category': 'purchase',
            'note': f'Backfill supplier ledger credit #{r.id}',
            'reference_id': f'supplier_ledger#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-supled-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    # Paid purchase v2 entries: Company -> Supplier
    for r in PurchaseV2.query.filter(
        PurchaseV2.is_void == False,
        func.lower(PurchaseV2.payment_status) == 'paid'
    ).order_by(PurchaseV2.id.asc()).all():
        st = 'purchase_v2_paid'
        sid = int(r.id)
        if _account_txn_source_exists(f'{st}:direct', sid):
            skipped += 1
            continue
        party = (r.supplier.name if r.supplier else f'Supplier #{r.supplier_id or ""}').strip() or 'Supplier'
        payload = {
            'date': (r.date.isoformat() if r.date else ''),
            'amount': float(r.total_amount or 0.0),
            'type': 'purchase',
            'from_account_id': company.id,
            'executed_by_account_id': company.id,
            'related_entity_type': 'supplier',
            'related_entity_id': (r.supplier_id or None),
            'party_name': party,
            'category': 'purchase',
            'note': f'Backfill paid purchase_v2 #{r.id}',
            'reference_id': f'purchase_v2#{r.id}',
            'source_type': st,
            'source_id': sid,
            'group_id': f'bf-pv2-{r.id}'
        }
        ok, _, rows = _create_account_transaction(payload, commit=False)
        if ok:
            created += len(rows)
        else:
            errors += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        errors += 1

    return {'created': created, 'skipped': skipped, 'errors': errors}

def _ensure_supplier_quick(name, phone=''):
    sname = _normalize_name_ci(name)
    if not sname:
        return None
    existing = Supplier.query.filter(func.lower(Supplier.name) == sname.lower(), Supplier.is_void == False).first()
    if existing:
        if phone and not (existing.phone or '').strip():
            existing.phone = phone.strip()
        existing.updated_at = _pkt_now_naive()
        return existing
    row = Supplier(name=sname, phone=(phone or '').strip(), status='active', is_void=False, created_at=_pkt_now_naive(), updated_at=_pkt_now_naive())
    db.session.add(row)
    db.session.flush()
    return row

def _ensure_material_v2(name, unit='KG'):
    mname = _normalize_name_ci(name)
    if not mname:
        return None
    existing = MaterialV2.query.filter(func.lower(MaterialV2.name) == mname.lower(), MaterialV2.is_void == False).first()
    if existing:
        return existing
    row = MaterialV2(name=mname, unit=(unit or 'KG').upper(), status='active', is_void=False, created_at=_pkt_now_naive(), updated_at=_pkt_now_naive())
    db.session.add(row)
    db.session.flush()
    return row

def _supplier_balance(supplier_id):
    debit = float(db.session.query(func.coalesce(func.sum(SupplierLedger.amount), 0.0))
                  .filter(SupplierLedger.supplier_id == supplier_id,
                          SupplierLedger.is_void == False,
                          SupplierLedger.entry_type == 'debit').scalar() or 0.0)
    credit = float(db.session.query(func.coalesce(func.sum(SupplierLedger.amount), 0.0))
                   .filter(SupplierLedger.supplier_id == supplier_id,
                           SupplierLedger.is_void == False,
                           SupplierLedger.entry_type == 'credit').scalar() or 0.0)
    return max(0.0, debit - credit)

def _material_v2_delivered(material_id, project_id=None, stage_id=None):
    q = db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(
        Delivery.material_id == material_id, Delivery.is_void == False
    )
    if project_id:
        q = q.filter(Delivery.project_id == project_id)
    if stage_id:
        q = q.filter(Delivery.stage_id == stage_id)
    return float(q.scalar() or 0.0)

def _material_v2_used(material_id, project_id=None, stage_id=None):
    q = db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0)).filter(
        UsageLogV2.material_id == material_id, UsageLogV2.is_void == False
    )
    if project_id:
        q = q.filter(UsageLogV2.project_id == project_id)
    if stage_id:
        q = q.filter(UsageLogV2.stage_id == stage_id)
    return float(q.scalar() or 0.0)

def _material_v2_available(material_id, project_id=None, stage_id=None):
    return max(0.0, _material_v2_delivered(material_id, project_id, stage_id) - _material_v2_used(material_id, project_id, stage_id))

def _purchase_v2_integrity_report():
    eps = 1e-6
    issues = []

    total_purchased_qty = float(db.session.query(func.coalesce(func.sum(PurchaseV2.quantity), 0.0))
                                .filter(PurchaseV2.is_void == False).scalar() or 0.0)
    total_sent_qty = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
                           .filter(Delivery.is_void == False).scalar() or 0.0)
    total_used_qty = float(db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0))
                           .filter(UsageLogV2.is_void == False).scalar() or 0.0)

    if total_sent_qty > total_purchased_qty + eps:
        issues.append(
            f'Global sent ({total_sent_qty:,.2f}) is greater than global purchased ({total_purchased_qty:,.2f}).'
        )
    if total_used_qty > total_sent_qty + eps:
        issues.append(
            f'Global used ({total_used_qty:,.2f}) is greater than global sent ({total_sent_qty:,.2f}).'
        )

    purchase_rows = (db.session.query(
        PurchaseV2.id,
        PurchaseV2.quantity,
        func.coalesce(func.sum(Delivery.quantity), 0.0).label('delivered_qty')
    ).outerjoin(
        Delivery, and_(Delivery.purchase_id == PurchaseV2.id, Delivery.is_void == False)
    ).filter(
        PurchaseV2.is_void == False
    ).group_by(PurchaseV2.id, PurchaseV2.quantity).all())
    for pr in purchase_rows:
        ordered = float(pr.quantity or 0.0)
        delivered = float(pr.delivered_qty or 0.0)
        if delivered > ordered + eps:
            issues.append(f'PO #{pr.id} delivered ({delivered:,.2f}) exceeds ordered ({ordered:,.2f}).')

    delivered_by_material = dict(
        db.session.query(
            Delivery.material_id,
            func.coalesce(func.sum(Delivery.quantity), 0.0)
        ).filter(
            Delivery.is_void == False
        ).group_by(Delivery.material_id).all()
    )
    used_by_material = dict(
        db.session.query(
            UsageLogV2.material_id,
            func.coalesce(func.sum(UsageLogV2.quantity), 0.0)
        ).filter(
            UsageLogV2.is_void == False
        ).group_by(UsageLogV2.material_id).all()
    )
    material_rows = MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.id.asc()).all()
    for mr in material_rows:
        delivered = float(delivered_by_material.get(mr.id, 0.0) or 0.0)
        used = float(used_by_material.get(mr.id, 0.0) or 0.0)
        if used > delivered + eps:
            mlabel = f'{mr.name} ({mr.unit or ""})'.strip()
            issues.append(f'Material {mlabel} used ({used:,.2f}) exceeds sent ({delivered:,.2f}).')

    return {
        'ok': len(issues) == 0,
        'issues': issues[:20],
        'issue_count': len(issues),
        'total_purchased_qty': total_purchased_qty,
        'total_sent_qty': total_sent_qty,
        'total_used_qty': total_used_qty
    }

def _material_v2_scope_stock_rows(project_id, stage_id=None):
    materials = MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.name.asc(), MaterialV2.id.asc()).all()
    rows = []
    for m in materials:
        available = _material_v2_available(m.id, project_id, stage_id)
        if available <= 1e-9:
            continue
        rows.append({
            'id': int(m.id),
            'name': m.name,
            'unit': (m.unit or ''),
            'available_qty': float(available)
        })
    return rows

def _delivery_scope_qty_by_purchase(purchase_id, project_id, stage_id=None):
    q = db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(
        Delivery.purchase_id == purchase_id,
        Delivery.is_void == False,
        Delivery.project_id == project_id
    )
    if stage_id:
        q = q.filter(Delivery.stage_id == stage_id)
    return float(q.scalar() or 0.0)

def _transfer_v2_material_between_scopes(material_id, from_project_id, from_stage_id, to_project_id, to_stage_id, quantity, date_value=None, note=''):
    qty_left = float(quantity or 0.0)
    if qty_left <= 0:
        return []
    purchases = (PurchaseV2.query
                 .filter(PurchaseV2.material_id == material_id, PurchaseV2.is_void == False)
                 .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
                 .all())
    transfer_ref = f"TRF-{_pkt_now_naive().strftime('%Y%m%d%H%M%S%f')}"
    moved_rows = []
    for p in purchases:
        if qty_left <= 1e-9:
            break
        scope_qty = _delivery_scope_qty_by_purchase(p.id, from_project_id, from_stage_id)
        if scope_qty <= 1e-9:
            continue
        take = min(scope_qty, qty_left)
        from_scope = f'P{from_project_id}' + (f'-S{from_stage_id}' if from_stage_id else '')
        to_scope = f'P{to_project_id}' + (f'-S{to_stage_id}' if to_stage_id else '')
        note_out = f'Transfer out {transfer_ref}: {from_scope} -> {to_scope}'
        note_in = f'Transfer in {transfer_ref}: {from_scope} -> {to_scope}'
        if note:
            note_out = f'{note_out} | {note}'
            note_in = f'{note_in} | {note}'
        out_row = Delivery(
            purchase_id=p.id,
            material_id=material_id,
            project_id=from_project_id,
            stage_id=from_stage_id,
            quantity=-float(take),
            date=(date_value or _pkt_today()),
            notes=note_out,
            delivery_person='Stock Transfer',
            is_void=False,
            created_at=_pkt_now_naive()
        )
        in_row = Delivery(
            purchase_id=p.id,
            material_id=material_id,
            project_id=to_project_id,
            stage_id=to_stage_id,
            quantity=float(take),
            date=(date_value or _pkt_today()),
            notes=note_in,
            delivery_person='Stock Transfer',
            is_void=False,
            created_at=_pkt_now_naive()
        )
        db.session.add(out_row)
        db.session.add(in_row)
        moved_rows.append((p.id, float(take)))
        qty_left -= float(take)
    return moved_rows

def _material_v2_weighted_cost(material_id):
    rows = (db.session.query(PurchaseV2)
            .filter(PurchaseV2.material_id == material_id, PurchaseV2.is_void == False)
            .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
            .all())
    total_qty = sum(float(r.quantity or 0.0) for r in rows)
    total_cost = sum(float(r.total_amount or 0.0) for r in rows)
    if total_qty > 0:
        return float(total_cost / total_qty)
    last = (db.session.query(PurchaseV2)
            .filter(PurchaseV2.material_id == material_id, PurchaseV2.is_void == False)
            .order_by(PurchaseV2.created_at.desc(), PurchaseV2.id.desc())
            .first())
    return float(last.unit_price if last else 0.0)

def _purchase_v2_delivered_qty(purchase_id):
    return float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
                 .filter(Delivery.purchase_id == purchase_id, Delivery.is_void == False)
                 .scalar() or 0.0)


def _purchase_v2_delivered_to_scope_qty(purchase_id, project_id, stage_id):
    if (not purchase_id) or (not project_id):
        return 0.0
    q = db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(
        Delivery.purchase_id == purchase_id,
        Delivery.project_id == project_id,
        Delivery.is_void == False
    )
    if stage_id:
        q = q.filter(Delivery.stage_id == stage_id)
    return float(q.scalar() or 0.0)


def _purchase_v2_used_in_scope_qty(purchase_id, project_id, stage_id, exclude_usage_id=None):
    if (not purchase_id) or (not project_id):
        return 0.0
    q = db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0)).filter(
        UsageLogV2.purchase_id == purchase_id,
        UsageLogV2.project_id == project_id,
        UsageLogV2.is_void == False
    )
    if stage_id:
        q = q.filter(UsageLogV2.stage_id == stage_id)
    if exclude_usage_id:
        q = q.filter(UsageLogV2.id != exclude_usage_id)
    return float(q.scalar() or 0.0)


def _purchase_v2_scope_remaining_map(material_id, project_id, stage_id, exclude_usage_id=None):
    if (not material_id) or (not project_id):
        return {}
    purchases = (PurchaseV2.query
                 .filter(
                     PurchaseV2.material_id == material_id,
                     PurchaseV2.is_void == False
                 )
                 .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
                 .all())
    purchase_ids = [int(p.id) for p in purchases]
    if not purchase_ids:
        return {}

    del_q = (db.session.query(
        Delivery.purchase_id,
        func.coalesce(func.sum(Delivery.quantity), 0.0)
    ).filter(
        Delivery.is_void == False,
        Delivery.project_id == project_id,
        Delivery.purchase_id.in_(purchase_ids)
    ))
    if stage_id:
        del_q = del_q.filter(Delivery.stage_id == stage_id)
    del_q = del_q.group_by(Delivery.purchase_id)
    delivered_map = {int(pid): float(qty or 0.0) for pid, qty in del_q.all()}

    use_q = (db.session.query(
        UsageLogV2.purchase_id,
        func.coalesce(func.sum(UsageLogV2.quantity), 0.0)
    ).filter(
        UsageLogV2.is_void == False,
        UsageLogV2.project_id == project_id,
        UsageLogV2.purchase_id.in_(purchase_ids)
    ))
    if stage_id:
        use_q = use_q.filter(UsageLogV2.stage_id == stage_id)
    if exclude_usage_id:
        use_q = use_q.filter(UsageLogV2.id != exclude_usage_id)
    linked_used_map = {int(pid): float(qty or 0.0) for pid, qty in use_q.group_by(UsageLogV2.purchase_id).all()}

    legacy_q = db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0)).filter(
        UsageLogV2.is_void == False,
        UsageLogV2.project_id == project_id,
        UsageLogV2.material_id == material_id,
        UsageLogV2.purchase_id.is_(None)
    )
    if stage_id:
        legacy_q = legacy_q.filter(UsageLogV2.stage_id == stage_id)
    if exclude_usage_id:
        legacy_q = legacy_q.filter(UsageLogV2.id != exclude_usage_id)
    legacy_unlinked_qty = float(legacy_q.scalar() or 0.0)

    remaining_map = {}
    carry = max(0.0, legacy_unlinked_qty)
    for p in purchases:
        pid = int(p.id)
        delivered = float(delivered_map.get(pid, 0.0) or 0.0)
        linked_used = float(linked_used_map.get(pid, 0.0) or 0.0)
        remaining = max(0.0, delivered - linked_used)
        if carry > 1e-9 and remaining > 1e-9:
            take = min(remaining, carry)
            remaining -= take
            carry -= take
        remaining_map[pid] = max(0.0, remaining)
    return remaining_map


def _purchase_v2_available_in_project_qty(purchase_id, project_id, exclude_usage_id=None):
    purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
    if (not purchase) or purchase.is_void:
        return 0.0
    remaining_map = _purchase_v2_scope_remaining_map(purchase.material_id, project_id, None, exclude_usage_id=exclude_usage_id)
    return float(remaining_map.get(int(purchase.id), 0.0) or 0.0)


def _purchase_v2_available_in_scope_qty(purchase_id, project_id, stage_id, exclude_usage_id=None):
    purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
    if (not purchase) or purchase.is_void:
        return 0.0
    remaining_map = _purchase_v2_scope_remaining_map(
        purchase.material_id,
        project_id,
        stage_id,
        exclude_usage_id=exclude_usage_id
    )
    return float(remaining_map.get(int(purchase.id), 0.0) or 0.0)

def _sync_purchase_v2_ledger(purchase_row):
    if not purchase_row:
        return
    rows = (SupplierLedger.query
            .filter(
                SupplierLedger.reference_type == 'purchase_v2',
                SupplierLedger.reference_id == purchase_row.id
            )
            .order_by(SupplierLedger.id.asc())
            .all())
    active_rows = [r for r in rows if not bool(r.is_void)]
    should_be_unpaid = (not bool(purchase_row.is_void)) and ((purchase_row.payment_status or 'unpaid').strip().lower() == 'unpaid')
    if should_be_unpaid:
        if active_rows:
            keep = active_rows[0]
            keep.supplier_id = purchase_row.supplier_id
            keep.entry_type = 'debit'
            keep.amount = float(purchase_row.total_amount or 0.0)
            keep.note = f'Unpaid purchase #{purchase_row.id}'
            for extra in active_rows[1:]:
                extra.is_void = True
                extra.void_reason = 'Merged duplicate purchase ledger rows'
                extra.voided_at = _pkt_now_naive()
        else:
            db.session.add(SupplierLedger(
                supplier_id=purchase_row.supplier_id,
                entry_type='debit',
                amount=float(purchase_row.total_amount or 0.0),
                reference_type='purchase_v2',
                reference_id=purchase_row.id,
                note=f'Unpaid purchase #{purchase_row.id}',
                is_void=False,
                created_at=_pkt_now_naive()
            ))
    else:
        for row in active_rows:
            row.is_void = True
            row.void_reason = 'Purchase changed to paid or void'
            row.voided_at = _pkt_now_naive()

def _repair_supplier_purchase_v2_ledger(supplier_id):
    if not supplier_id:
        return
    purchases = (PurchaseV2.query
                 .filter(PurchaseV2.supplier_id == supplier_id, PurchaseV2.is_void == False)
                 .order_by(PurchaseV2.id.asc())
                 .all())
    for p in purchases:
        _sync_purchase_v2_ledger(p)

@app.route('/api/v2/purchase/suppliers', methods=['GET', 'POST'])
@login_required
def api_v2_suppliers():
    if request.method == 'POST':
        payload = request.get_json(silent=True) or request.form
        name = _normalize_name_ci(payload.get('name'))
        phone = (payload.get('phone') or '').strip()
        if not name:
            return jsonify(ok=False, message='Supplier name is required.'), 400
        row = _ensure_supplier_quick(name, phone)
        if not row:
            return jsonify(ok=False, message='Invalid supplier data.'), 400
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'create', f'{current_user.username.title()} added supplier {row.name}', 'supplier', row.id)
        db.session.commit()
        return jsonify(ok=True, id=row.id, name=row.name)
    rows = Supplier.query.filter_by(is_void=False).order_by(Supplier.name.asc()).all()
    credit_rows = (db.session.query(
            SupplierLedger.supplier_id,
            func.lower(func.coalesce(SupplierLedger.reference_type, 'payment')),
            func.coalesce(func.sum(SupplierLedger.amount), 0.0)
        )
        .filter(
            SupplierLedger.is_void == False,
            SupplierLedger.entry_type == 'credit',
            func.lower(func.coalesce(SupplierLedger.reference_type, 'payment')).in_(['payment', 'tip', 'settlement'])
        )
        .group_by(SupplierLedger.supplier_id, func.lower(func.coalesce(SupplierLedger.reference_type, 'payment')))
        .all())
    credit_map = {}
    for sid, rtype, amount in credit_rows:
        credit_map[(int(sid or 0), (rtype or 'payment'))] = float(amount or 0.0)
    return jsonify(ok=True, items=[{
        'id': r.id,
        'name': r.name,
        'phone': r.phone or '',
        'status': r.status or 'active',
        'balance': _supplier_balance(r.id),
        'payment_total': float(credit_map.get((r.id, 'payment'), 0.0) or 0.0),
        'tip_total': float(credit_map.get((r.id, 'tip'), 0.0) or 0.0),
        'settlement_total': float(credit_map.get((r.id, 'settlement'), 0.0) or 0.0)
    } for r in rows])

@app.route('/api/v2/purchase/suppliers/<int:supplier_id>', methods=['PUT', 'DELETE'])
@login_required
def api_v2_supplier_item(supplier_id):
    row = Supplier.query.get_or_404(supplier_id)
    if request.method == 'PUT':
        payload = request.get_json(silent=True) or request.form
        name = _normalize_name_ci(payload.get('name') if payload else row.name) or row.name
        phone = (payload.get('phone') or '').strip() if payload else (row.phone or '')
        status = ((payload.get('status') if payload else row.status) or 'active').strip().lower()
        if status not in ('active', 'inactive'):
            status = 'active'
        exists = (Supplier.query
                  .filter(
                      Supplier.id != row.id,
                      Supplier.is_void == False,
                      func.lower(Supplier.name) == name.lower()
                  )
                  .first())
        if exists:
            return jsonify(ok=False, message='Another supplier already uses this name.'), 400
        row.name = name
        row.phone = phone
        row.status = status
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'update', f'{current_user.username.title()} updated supplier #{row.id}: {row.name}', 'supplier', row.id)
        db.session.commit()
        return jsonify(ok=True, id=row.id)
    has_purchase = PurchaseV2.query.filter_by(supplier_id=row.id, is_void=False).first() is not None
    has_ledger = SupplierLedger.query.filter_by(supplier_id=row.id, is_void=False).first() is not None
    if has_purchase or has_ledger:
        return jsonify(ok=False, message='Supplier has transactions; set status inactive instead of delete.'), 400
    row.is_void = True
    row.status = 'inactive'
    row.updated_at = _pkt_now_naive()
    log_action(current_user, 'delete', f'{current_user.username.title()} deleted supplier #{row.id}: {row.name}', 'supplier', row.id)
    db.session.commit()
    return jsonify(ok=True, id=row.id)

@app.route('/api/v2/purchase/materials', methods=['GET', 'POST'])
@login_required
def api_v2_materials():
    if request.method == 'POST':
        payload = request.get_json(silent=True) or request.form
        name = _normalize_name_ci(payload.get('name'))
        unit = (payload.get('unit') or 'KG').strip().upper()
        if unit not in _MATERIAL_V2_UNITS:
            return jsonify(ok=False, message=f'Unit must be one of: {", ".join(_MATERIAL_V2_UNITS)}.'), 400
        row = _ensure_material_v2(name, unit)
        if not row:
            return jsonify(ok=False, message='Material name is required.'), 400
        row.unit = unit
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'create', f'{current_user.username.title()} added material {row.name} ({unit})', 'material_v2', row.id)
        db.session.commit()
        return jsonify(ok=True, id=row.id, name=row.name, unit=row.unit)
    rows = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
    items = []
    for r in rows:
        delivered = _material_v2_delivered(r.id)
        used = _material_v2_used(r.id)
        items.append({
            'id': r.id,
            'name': r.name,
            'unit': r.unit,
            'status': r.status or 'active',
            'delivered_qty': delivered,
            'used_qty': used,
            'available_qty': max(0.0, delivered - used)
        })
    return jsonify(ok=True, units=list(_MATERIAL_V2_UNITS), items=items)

@app.route('/api/v2/purchase/materials/<int:material_id>', methods=['PUT', 'DELETE'])
@login_required
def api_v2_material_item(material_id):
    row = MaterialV2.query.get_or_404(material_id)
    if request.method == 'PUT':
        payload = request.get_json(silent=True) or request.form
        name = _normalize_name_ci(payload.get('name') if payload else row.name) or row.name
        unit = ((payload.get('unit') if payload else row.unit) or 'KG').strip().upper()
        status = ((payload.get('status') if payload else row.status) or 'active').strip().lower()
        if status not in ('active', 'inactive'):
            status = 'active'
        if unit not in _MATERIAL_V2_UNITS:
            return jsonify(ok=False, message=f'Unit must be one of: {", ".join(_MATERIAL_V2_UNITS)}.'), 400
        exists = (MaterialV2.query
                  .filter(
                      MaterialV2.id != row.id,
                      MaterialV2.is_void == False,
                      func.lower(MaterialV2.name) == name.lower()
                  )
                  .first())
        if exists:
            return jsonify(ok=False, message='Another material already uses this name.'), 400
        row.name = name
        row.unit = unit
        row.status = status
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'update', f'{current_user.username.title()} updated material #{row.id}: {row.name} ({row.unit})', 'material_v2', row.id)
        db.session.commit()
        return jsonify(ok=True, id=row.id)
    has_purchase = PurchaseV2.query.filter_by(material_id=row.id, is_void=False).first() is not None
    has_delivery = Delivery.query.filter_by(material_id=row.id, is_void=False).first() is not None
    has_usage = UsageLogV2.query.filter_by(material_id=row.id, is_void=False).first() is not None
    if has_purchase or has_delivery or has_usage:
        return jsonify(ok=False, message='Material has transactions; set status inactive instead of delete.'), 400
    row.is_void = True
    row.status = 'inactive'
    row.updated_at = _pkt_now_naive()
    log_action(current_user, 'delete', f'{current_user.username.title()} deleted material #{row.id}: {row.name}', 'material_v2', row.id)
    db.session.commit()
    return jsonify(ok=True, id=row.id)

@app.route('/api/v2/purchase/purchases', methods=['GET', 'POST'])
@login_required
def api_v2_purchases():
    if request.method == 'POST':
        payload = request.get_json(silent=True) or request.form
        supplier_id = _payload_int(payload, 'supplier_id')
        material_id = _payload_int(payload, 'material_id')
        quick_supplier = _normalize_name_ci(payload.get('supplier_name'))
        unit_price = max(0.0, _flt(payload.get('unit_price'), 0.0))
        quantity = max(0.0, _flt(payload.get('quantity'), 0.0))
        payment_status = (payload.get('payment_status') or 'unpaid').strip().lower()
        if payment_status not in ('paid', 'unpaid'):
            payment_status = 'unpaid'
        supplier = Supplier.query.get(supplier_id) if supplier_id else None
        if not supplier and quick_supplier:
            supplier = _ensure_supplier_quick(quick_supplier, payload.get('supplier_phone'))
        if (not supplier) or supplier.is_void or (supplier.status or 'active').strip().lower() != 'active':
            return jsonify(ok=False, message='Valid supplier is required.'), 400
        material = MaterialV2.query.get(material_id) if material_id else None
        if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
            return jsonify(ok=False, message='Valid material is required.'), 400
        if unit_price <= 0 or quantity <= 0:
            return jsonify(ok=False, message='Unit price and quantity must be greater than 0.'), 400
        total_amount = float(unit_price * quantity)
        row = PurchaseV2(
            supplier_id=supplier.id,
            material_id=material.id,
            unit_price=unit_price,
            quantity=quantity,
            total_amount=total_amount,
            payment_status=payment_status,
            is_void=False,
            created_at=_pkt_now_naive(),
            updated_at=_pkt_now_naive()
        )
        db.session.add(row)
        db.session.flush()
        _sync_purchase_v2_ledger(row)
        if payment_status == 'paid':
            ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
            if not ok_txn and 'Duplicate source transaction' not in (msg_txn or ''):
                db.session.rollback()
                return jsonify(ok=False, message=(msg_txn or 'Unable to post paid purchase in unified accounts.')), 400
        log_action(
            current_user,
            'create',
            f'{current_user.username.title()} created purchase #{row.id}: {supplier.name}, {material.name}, {quantity:.2f} x {unit_price:.2f} = {total_amount:.2f} ({payment_status})',
            'purchase_v2',
            row.id
        )
        db.session.commit()
        return jsonify(ok=True, id=row.id, total_amount=total_amount)
    rows = (PurchaseV2.query
            .filter_by(is_void=False)
            .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
            .limit(400)
            .all())
    return jsonify(ok=True, items=[{
        'id': r.id,
        'supplier_id': r.supplier_id,
        'material_id': r.material_id,
        'supplier': r.supplier.name if r.supplier else '-',
        'material': r.material.name if r.material else '-',
        'unit': (r.material.unit if r.material else ''),
        'unit_price': float(r.unit_price or 0.0),
        'quantity': float(r.quantity or 0.0),
        'total_amount': float(r.total_amount or 0.0),
        'payment_status': r.payment_status,
        'delivered_qty': _purchase_v2_delivered_qty(r.id),
        'created_at': (r.created_at.isoformat(sep=' ') if r.created_at else '')
    } for r in rows])

@app.route('/api/v2/purchase/purchases/<int:purchase_id>', methods=['PUT', 'DELETE'])
@login_required
def api_v2_purchase_item(purchase_id):
    row = PurchaseV2.query.get_or_404(purchase_id)
    if row.is_void:
        return jsonify(ok=False, message='Purchase is already deleted.'), 400
    if request.method == 'PUT':
        payload = request.get_json(silent=True) or request.form
        supplier_id = _payload_int(payload, 'supplier_id')
        material_id = _payload_int(payload, 'material_id')
        unit_price = max(0.0, _flt(payload.get('unit_price'), row.unit_price))
        quantity = max(0.0, _flt(payload.get('quantity'), row.quantity))
        payment_status = (payload.get('payment_status') or row.payment_status or 'unpaid').strip().lower()
        if payment_status not in ('paid', 'unpaid'):
            payment_status = 'unpaid'
        supplier = Supplier.query.get(supplier_id) if supplier_id else None
        material = MaterialV2.query.get(material_id) if material_id else None
        if (not supplier) or supplier.is_void or (supplier.status or 'active').strip().lower() != 'active':
            return jsonify(ok=False, message='Valid supplier is required.'), 400
        if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
            return jsonify(ok=False, message='Valid material is required.'), 400
        if unit_price <= 0 or quantity <= 0:
            return jsonify(ok=False, message='Unit price and quantity must be greater than 0.'), 400
        delivered = _purchase_v2_delivered_qty(row.id)
        if quantity + 1e-9 < delivered:
            return jsonify(ok=False, message=f'Cannot set quantity below delivered quantity ({delivered:.2f}).'), 400
        if row.material_id != material.id and delivered > 0:
            return jsonify(ok=False, message='Cannot change material after deliveries are recorded for this purchase.'), 400
        old_payment_status = (row.payment_status or 'unpaid').strip().lower()
        row.supplier_id = supplier.id
        row.material_id = material.id
        row.unit_price = unit_price
        row.quantity = quantity
        row.total_amount = float(unit_price * quantity)
        row.payment_status = payment_status
        row.updated_at = _pkt_now_naive()
        _sync_purchase_v2_ledger(row)
        if payment_status == 'paid':
            ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
            if not ok_txn and 'Duplicate source transaction' not in (msg_txn or ''):
                db.session.rollback()
                return jsonify(ok=False, message=(msg_txn or 'Unable to post paid purchase in unified accounts.')), 400
        elif old_payment_status == 'paid':
            _accounts_set_void_by_source('purchase_v2_paid', row.id, True)
        log_action(
            current_user,
            'update',
            f'{current_user.username.title()} updated purchase #{row.id}: {supplier.name}, {material.name}, {quantity:.2f} x {unit_price:.2f} ({payment_status})',
            'purchase_v2',
            row.id
        )
        db.session.commit()
        return jsonify(ok=True, id=row.id, total_amount=float(row.total_amount or 0.0))
    delivered = _purchase_v2_delivered_qty(row.id)
    if delivered > 0:
        return jsonify(ok=False, message=f'Cannot delete purchase #{row.id}; delivery exists ({delivered:.2f}).'), 400
    row.is_void = True
    row.void_reason = 'Deleted by user from Purchase V2'
    row.voided_at = _pkt_now_naive()
    row.updated_at = _pkt_now_naive()
    _sync_purchase_v2_ledger(row)
    _accounts_set_void_by_source('purchase_v2_paid', row.id, True)
    log_action(current_user, 'delete', f'{current_user.username.title()} deleted purchase #{row.id}', 'purchase_v2', row.id)
    db.session.commit()
    return jsonify(ok=True, id=row.id)

@app.route('/api/v2/purchase/payments', methods=['POST'])
@login_required
def api_v2_payments():
    payload = request.get_json(silent=True) or request.form
    supplier_id = _payload_int(payload, 'supplier_id')
    amount = max(0.0, _flt(payload.get('amount'), 0.0))
    note = (payload.get('note') or '').strip()
    entry_kind = ((payload.get('entry_kind') if payload else 'payment') or 'payment').strip().lower()
    if entry_kind not in ('payment', 'tip', 'settlement'):
        entry_kind = 'payment'
    supplier = Supplier.query.get(supplier_id) if supplier_id else None
    if (not supplier) or supplier.is_void:
        return jsonify(ok=False, message='Valid supplier is required.'), 400
    if amount <= 0:
        return jsonify(ok=False, message='Amount must be greater than 0.'), 400
    title_map = {'payment': 'payment', 'tip': 'tip', 'settlement': 'settlement'}
    pretty = title_map.get(entry_kind, 'payment')
    ledger_row = SupplierLedger(
        supplier_id=supplier.id,
        entry_type='credit',
        amount=amount,
        reference_type=entry_kind,
        reference_id=None,
        note=note or f'Supplier {pretty}',
        is_void=False,
        created_at=_pkt_now_naive()
    )
    db.session.add(ledger_row)
    db.session.flush()
    ok_txn, msg_txn, _ = _accounts_post_supplier_credit_row(ledger_row, supplier_name=supplier.name, commit=False)
    if not ok_txn:
        db.session.rollback()
        return jsonify(ok=False, message=(msg_txn or 'Unable to post supplier payment in unified accounts.')), 400
    log_action(
        current_user,
        'payment',
        f'{current_user.username.title()} recorded supplier {pretty}: {supplier.name}, {amount:.2f} PKR',
        f'supplier_{entry_kind}',
        supplier.id
    )
    db.session.commit()
    return jsonify(ok=True, supplier_id=supplier.id, balance=_supplier_balance(supplier.id), entry_kind=entry_kind)

@app.route('/api/v2/purchase/suppliers/<int:supplier_id>/balance', methods=['GET'])
@login_required
def api_v2_supplier_balance(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    return jsonify(ok=True, supplier_id=supplier.id, supplier=supplier.name, balance=_supplier_balance(supplier.id))

@app.route('/api/v2/purchase/material-available', methods=['GET'])
@login_required
def api_v2_material_available():
    material_id = request.args.get('material_id', type=int)
    project_id = request.args.get('project_id', type=int)
    stage_id = request.args.get('stage_id', type=int)
    if not material_id:
        return jsonify(ok=False, message='material_id is required.'), 400
    material = MaterialV2.query.get(material_id)
    if (not material) or material.is_void:
        return jsonify(ok=False, message='Valid material is required.'), 400
    if stage_id and (not project_id):
        return jsonify(ok=False, message='project_id is required when stage_id is provided.'), 400
    project = Project.query.get(project_id) if project_id else None
    stage = Stage.query.get(stage_id) if stage_id else None
    if project_id and not project:
        return jsonify(ok=False, message='Valid project is required.'), 400
    if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
        return jsonify(ok=False, message='Selected stage does not belong to selected project.'), 400
    available = _material_v2_available(material.id, project.id if project else None, stage.id if stage else None)
    return jsonify(ok=True, material_id=material.id, available_qty=available)


@app.route('/api/v2/purchase/usage-po-options', methods=['GET'])
@login_required
def api_v2_usage_po_options():
    material_id = request.args.get('material_id', type=int)
    project_id = request.args.get('project_id', type=int)
    stage_id = request.args.get('stage_id', type=int)
    if not material_id:
        return jsonify(ok=False, message='material_id is required.'), 400
    if not project_id:
        return jsonify(ok=False, message='project_id is required.'), 400
    if not stage_id:
        return jsonify(ok=False, message='stage_id is required.'), 400
    material = MaterialV2.query.get(material_id)
    project = Project.query.get(project_id)
    stage = Stage.query.get(stage_id)
    if (not material) or material.is_void:
        return jsonify(ok=False, message='Valid material is required.'), 400
    if not project:
        return jsonify(ok=False, message='Valid project is required.'), 400
    if (not stage) or int(stage.project_id or 0) != int(project.id):
        return jsonify(ok=False, message='Selected stage does not belong to selected project.'), 400

    rows = (PurchaseV2.query
            .filter(
                PurchaseV2.material_id == material.id,
                PurchaseV2.is_void == False
            )
            .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
            .all())
    remaining_map = _purchase_v2_scope_remaining_map(material.id, project.id, stage.id)
    items = []
    for p in rows:
        delivered_qty = _purchase_v2_delivered_to_scope_qty(p.id, project.id, stage.id)
        used_qty = _purchase_v2_used_in_scope_qty(p.id, project.id, stage.id)
        remaining_qty = float(remaining_map.get(int(p.id), 0.0) or 0.0)
        if remaining_qty <= 1e-9:
            continue
        items.append({
            'id': int(p.id),
            'supplier': (p.supplier.name if p.supplier else '-'),
            'unit_price': float(p.unit_price or 0.0),
            'remaining_qty': float(remaining_qty),
            'delivered_qty': float(delivered_qty),
            'used_qty': float(used_qty),
            'unit': (p.material.unit if p.material else material.unit or ''),
            'challan_no': (p.challan_no or ''),
            'date': (p.date.isoformat() if p.date else ''),
        })
    return jsonify(ok=True, material_id=material.id, project_id=project.id, stage_id=stage.id, items=items)

@app.route('/api/v2/purchase/deliveries', methods=['GET', 'POST'])
@login_required
def api_v2_deliveries():
    if request.method == 'POST':
        payload = request.get_json(silent=True) or request.form
        purchase_id = _payload_int(payload, 'purchase_id')
        project_id = _payload_int(payload, 'project_id')
        stage_id = _payload_int(payload, 'stage_id')
        quantity = max(0.0, _flt(payload.get('quantity'), 0.0))
        delivery_person = (payload.get('delivery_person') or '').strip()
        purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
        project = Project.query.get(project_id) if project_id else None
        stage = Stage.query.get(stage_id) if stage_id else None
        if (not purchase) or purchase.is_void:
            return jsonify(ok=False, message='Valid purchase is required.'), 400
        if not project:
            return jsonify(ok=False, message='Valid project is required.'), 400
        if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
            return jsonify(ok=False, message='Selected stage does not belong to selected project.'), 400
        if quantity <= 0:
            return jsonify(ok=False, message='Quantity must be greater than 0.'), 400
        delivered_so_far = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
                                 .filter(Delivery.purchase_id == purchase.id, Delivery.is_void == False).scalar() or 0.0)
        remaining_purchase_qty = max(0.0, float(purchase.quantity or 0.0) - delivered_so_far)
        if quantity > remaining_purchase_qty + 1e-9:
            return jsonify(ok=False, message=f'Cannot deliver more than remaining purchase quantity ({remaining_purchase_qty:.2f}).'), 400
        row = Delivery(
            purchase_id=purchase.id,
            material_id=purchase.material_id,
            project_id=project.id,
            stage_id=stage.id if stage else None,
            quantity=quantity,
            delivery_person=delivery_person,
            is_void=False,
            created_at=_pkt_now_naive()
        )
        db.session.add(row)
        db.session.flush()
        log_action(
            current_user,
            'delivery',
            f'{current_user.username.title()} recorded delivery #{row.id}: {purchase.material.name if purchase.material else "-"} {quantity:.2f} to {project.name} -> {stage.name if stage else "-"}',
            'delivery',
            row.id
        )
        db.session.commit()
        return jsonify(ok=True, id=row.id)
    rows = (Delivery.query
            .filter_by(is_void=False)
            .order_by(Delivery.created_at.asc(), Delivery.id.asc())
            .limit(400)
            .all())
    return jsonify(ok=True, items=[{
        'id': r.id,
        'purchase_id': r.purchase_id,
        'material_id': r.material_id,
        'project_id': r.project_id,
        'stage_id': r.stage_id,
        'material': r.material.name if r.material else '-',
        'project': r.project.name if r.project else '-',
        'stage': r.stage.name if r.stage else '-',
        'quantity': float(r.quantity or 0.0),
        'delivery_person': r.delivery_person or '',
        'created_at': (r.created_at.isoformat(sep=' ') if r.created_at else '')
    } for r in rows])

@app.route('/api/v2/purchase/deliveries/<int:delivery_id>', methods=['DELETE'])
@login_required
def api_v2_delivery_item(delivery_id):
    row = Delivery.query.get_or_404(delivery_id)
    if row.is_void:
        return jsonify(ok=False, message='Delivery is already deleted.'), 400
    row.is_void = True
    row.void_reason = 'Deleted by user from Purchase V2'
    row.voided_at = _pkt_now_naive()
    log_action(current_user, 'delete', f'{current_user.username.title()} deleted delivery #{row.id}', 'delivery', row.id)
    db.session.commit()
    return jsonify(ok=True, id=row.id)

@app.route('/api/v2/purchase/usage', methods=['GET', 'POST'])
@login_required
def api_v2_usage():
    if request.method == 'POST':
        payload = request.get_json(silent=True) or request.form
        purchase_id = _payload_int(payload, 'purchase_id')
        material_id = _payload_int(payload, 'material_id')
        project_id = _payload_int(payload, 'project_id')
        stage_id = _payload_int(payload, 'stage_id')
        quantity = max(0.0, _flt(payload.get('quantity'), 0.0))
        purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
        material = MaterialV2.query.get(material_id) if material_id else None
        project = Project.query.get(project_id) if project_id else None
        stage = Stage.query.get(stage_id) if stage_id else None
        if (not purchase_id) or (not purchase) or purchase.is_void:
            return jsonify(ok=False, message='Valid purchase order is required.'), 400
        if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
            return jsonify(ok=False, message='Valid material is required.'), 400
        if int(purchase.material_id or 0) != int(material.id):
            return jsonify(ok=False, message='Selected purchase order does not match selected material.'), 400
        if not project:
            return jsonify(ok=False, message='Valid project is required.'), 400
        if not stage_id:
            return jsonify(ok=False, message='stage_id is required for strict stock control.'), 400
        if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
            return jsonify(ok=False, message='Selected stage does not belong to selected project.'), 400
        if quantity <= 0:
            return jsonify(ok=False, message='Quantity must be greater than 0.'), 400
        available = _purchase_v2_available_in_scope_qty(purchase.id, project.id, stage.id if stage else None)
        if quantity > available + 1e-9:
            return jsonify(ok=False, message=f'Usage exceeds available stock for selected purchase order in this stage ({available:.2f}).'), 400
        unit_price = float(purchase.unit_price or 0.0)
        cost = float(unit_price * quantity)
        row = UsageLogV2(
            purchase_id=purchase.id,
            material_id=material.id,
            project_id=project.id,
            stage_id=stage.id if stage else None,
            quantity=quantity,
            cost=cost,
            is_void=False,
            created_at=_pkt_now_naive()
        )
        db.session.add(row)
        db.session.flush()
        log_action(
            current_user,
            'usage',
            f'{current_user.username.title()} recorded usage #{row.id}: PO#{purchase.id}, {material.name} {quantity:.2f} @ {unit_price:.2f} ({cost:.2f} PKR) on {project.name} -> {stage.name if stage else "-"}',
            'usage',
            row.id
        )
        db.session.commit()
        return jsonify(ok=True, id=row.id, cost=cost)
    rows = (UsageLogV2.query
            .filter_by(is_void=False)
            .order_by(UsageLogV2.created_at.asc(), UsageLogV2.id.asc())
            .limit(400)
            .all())
    return jsonify(ok=True, items=[{
        'id': r.id,
        'material_id': r.material_id,
        'project_id': r.project_id,
        'stage_id': r.stage_id,
        'material': r.material.name if r.material else '-',
        'project': r.project.name if r.project else '-',
        'stage': r.stage.name if r.stage else '-',
        'purchase_id': r.purchase_id,
        'unit_price': float((r.purchase.unit_price if r.purchase else 0.0) or 0.0),
        'challan_no': (r.purchase.challan_no if r.purchase else ''),
        'quantity': float(r.quantity or 0.0),
        'cost': float(r.cost or 0.0),
        'created_at': (r.created_at.isoformat(sep=' ') if r.created_at else '')
    } for r in rows])

@app.route('/api/v2/purchase/usage/<int:usage_id>', methods=['DELETE'])
@login_required
def api_v2_usage_item(usage_id):
    row = UsageLogV2.query.get_or_404(usage_id)
    if row.is_void:
        return jsonify(ok=False, message='Usage row is already deleted.'), 400
    row.is_void = True
    row.void_reason = 'Deleted by user from Purchase V2'
    row.voided_at = _pkt_now_naive()
    log_action(current_user, 'delete', f'{current_user.username.title()} deleted usage #{row.id}', 'usage', row.id)
    db.session.commit()
    return jsonify(ok=True, id=row.id)

@app.route('/api/v2/purchase/supplier-ledger', methods=['GET'])
@login_required
def api_v2_supplier_ledger():
    supplier_id = request.args.get('supplier_id', type=int)
    q = SupplierLedger.query.filter(SupplierLedger.is_void == False)
    if supplier_id:
        q = q.filter(SupplierLedger.supplier_id == supplier_id)
    rows = (q.order_by(SupplierLedger.created_at.asc(), SupplierLedger.id.asc())
            .limit(500)
            .all())
    items = []
    for r in rows:
        signed = float(r.amount or 0.0)
        if (r.entry_type or '').strip().lower() == 'credit':
            signed = -signed
        items.append({
            'id': r.id,
            'supplier_id': r.supplier_id,
            'supplier': (r.supplier.name if r.supplier else '-'),
            'entry_type': (r.entry_type or '').strip().lower(),
            'amount': float(r.amount or 0.0),
            'signed_amount': signed,
            'reference_type': r.reference_type or '',
            'reference_id': r.reference_id,
            'note': r.note or '',
            'created_at': (r.created_at.isoformat(sep=' ') if r.created_at else '')
        })
    return jsonify(ok=True, items=items)

@app.route('/api/v2/purchase/material-stock', methods=['GET'])
@login_required
def api_v2_material_stock_list():
    rows = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
    items = []
    for m in rows:
        delivered = _material_v2_delivered(m.id)
        used = _material_v2_used(m.id)
        items.append({
            'id': m.id,
            'name': m.name,
            'unit': m.unit,
            'delivered_qty': delivered,
            'used_qty': used,
            'available_qty': max(0.0, delivered - used)
        })
    return jsonify(ok=True, items=items)

@app.route('/api/v2/purchase/material-stock-scope', methods=['GET'])
@login_required
def api_v2_material_stock_scope():
    project_id = request.args.get('project_id', type=int)
    stage_id = request.args.get('stage_id', type=int)
    if not project_id:
        return jsonify(ok=False, message='project_id is required.'), 400
    project = Project.query.get(project_id)
    if not project:
        return jsonify(ok=False, message='Valid project is required.'), 400
    stage = Stage.query.get(stage_id) if stage_id else None
    if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
        return jsonify(ok=False, message='Selected stage does not belong to selected project.'), 400
    return jsonify(ok=True, items=_material_v2_scope_stock_rows(project.id, stage.id if stage else None))

@app.route('/api/v2/purchase/kpis', methods=['GET'])
@login_required
def api_v2_kpis():
    total_purchase = float(db.session.query(func.coalesce(func.sum(PurchaseV2.total_amount), 0.0))
                           .filter(PurchaseV2.is_void == False).scalar() or 0.0)
    unpaid = float(db.session.query(func.coalesce(func.sum(PurchaseV2.total_amount), 0.0))
                   .filter(PurchaseV2.is_void == False, PurchaseV2.payment_status == 'unpaid').scalar() or 0.0)
    delivered_qty = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
                          .filter(Delivery.is_void == False).scalar() or 0.0)
    used_qty = float(db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0))
                     .filter(UsageLogV2.is_void == False).scalar() or 0.0)
    used_cost = float(db.session.query(func.coalesce(func.sum(UsageLogV2.cost), 0.0))
                      .filter(UsageLogV2.is_void == False).scalar() or 0.0)
    return jsonify(ok=True, purchase_total=total_purchase, unpaid_total=unpaid, delivered_qty=delivered_qty, used_qty=used_qty, used_cost=used_cost)

@app.route('/api/v2/purchase/recalculate-stock', methods=['POST'])
@login_required
def api_v2_recalculate_stock():
    materials = MaterialV2.query.filter_by(is_void=False).all()
    rows = []
    for m in materials:
        delivered = _material_v2_delivered(m.id)
        used = _material_v2_used(m.id)
        rows.append({'material_id': m.id, 'material': m.name, 'delivered': delivered, 'used': used, 'available': max(0.0, delivered - used)})
    log_action(current_user, 'update', f'{current_user.username.title()} recalculated stock ledger for {len(rows)} material(s).', 'stock', len(rows))
    db.session.commit()
    return jsonify(ok=True, items=rows)

@app.route('/api/v2/purchase', methods=['GET'])
@login_required
def api_v2_purchase_root():
    return jsonify(
        ok=True,
        endpoints=[
            '/api/v2/purchase/suppliers',
            '/api/v2/purchase/suppliers/<id>',
            '/api/v2/purchase/supplier-ledger',
            '/api/v2/purchase/materials',
            '/api/v2/purchase/materials/<id>',
            '/api/v2/purchase/material-stock',
            '/api/v2/purchase/material-stock-scope',
            '/api/v2/purchase/purchases',
            '/api/v2/purchase/purchases/<id>',
            '/api/v2/purchase/payments',
            '/api/v2/purchase/suppliers/<id>/balance',
            '/api/v2/purchase/usage-po-options',
            '/api/v2/purchase/deliveries',
            '/api/v2/purchase/deliveries/<id>',
            '/api/v2/purchase/usage',
            '/api/v2/purchase/usage/<id>',
            '/api/v2/purchase/kpis',
            '/api/v2/purchase/recalculate-stock'
        ]
    )


# â”€â”€ Bootstrap â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/api/accounts/create_account', methods=['POST'])
@login_required
def api_accounts_create_account():
    if _admin_only():
        return jsonify(ok=False, message='Admin access required.'), 403
    payload = request.get_json(silent=True) or request.form
    acc_group = _normalize_account_group(payload.get('account_group'))
    acc_mode = _normalize_account_mode(payload.get('account_mode'))
    acc_type = _resolve_account_type(group=acc_group, mode=acc_mode, explicit_type=payload.get('type'))
    row, msg = _create_account(
        payload.get('name'),
        acc_type,
        opening_balance=_flt(payload.get('opening_balance'), 0.0),
        bank_name=payload.get('bank_name'),
        account_number=payload.get('account_number'),
        iban=payload.get('iban'),
        account_mode=acc_mode,
    )
    if not row:
        return jsonify(ok=False, message=(msg or 'Unable to create account.')), 400
    db.session.commit()
    acc_group, acc_mode = _account_group_mode_for_row(row)
    return jsonify(ok=True, account={
        'id': int(row.id),
        'name': row.name,
        'type': row.type,
        'account_group': acc_group,
        'account_mode': acc_mode,
        'opening_balance': float(row.opening_balance or 0.0),
        'bank_name': (row.bank_name or ''),
        'account_number': (row.account_number or ''),
        'iban': (row.iban or ''),
        'auto_generated': bool(row.auto_generated),
        'auto_source': (row.auto_source or ''),
        'created_at': (row.created_at.isoformat(sep=' ') if row.created_at else ''),
    })


@app.route('/api/accounts/list_accounts_with_balances', methods=['GET'])
@login_required
def api_accounts_list_accounts_with_balances():
    if _admin_only():
        return jsonify(ok=False, message='Admin access required.'), 403
    include_inactive = (str(request.args.get('include_inactive') or '').strip().lower() in ('1', 'true', 'yes', 'on'))
    return jsonify(ok=True, items=_list_accounts_with_balances(include_inactive=include_inactive))


@app.route('/api/accounts/account/<int:account_id>', methods=['PUT', 'DELETE'])
@login_required
def api_accounts_account_item(account_id):
    if _admin_only():
        return jsonify(ok=False, message='Admin access required.'), 403
    row = Account.query.get_or_404(account_id)
    if request.method == 'DELETE':
        has_txn = db.session.query(AccountTransaction.id).filter(
            AccountTransaction.is_void == False,
            or_(AccountTransaction.from_account_id == row.id, AccountTransaction.to_account_id == row.id)
        ).first() is not None
        if has_txn:
            row.status = 'inactive'
            row.is_void = True
            db.session.commit()
            return jsonify(ok=True, deleted=False, message='Account has transactions and was archived instead.')
        db.session.delete(row)
        db.session.commit()
        return jsonify(ok=True, deleted=True)

    payload = request.get_json(silent=True) or request.form
    nm = _normalize_name_ci(payload.get('name') or row.name)
    current_group, current_mode = _account_group_mode_for_row(row)
    acc_group = _normalize_account_group(payload.get('account_group') or current_group)
    acc_mode = _normalize_account_mode(payload.get('account_mode') or current_mode)
    tp = _resolve_account_type(group=acc_group, mode=acc_mode, explicit_type=payload.get('type') or row.type)
    opening = _flt(payload.get('opening_balance'), row.opening_balance)
    bank_name = _normalize_name_ci(payload.get('bank_name') or row.bank_name or '')
    account_number = (payload.get('account_number') or row.account_number or '').strip()
    iban = (payload.get('iban') or row.iban or '').strip()
    if not nm:
        return jsonify(ok=False, message='Account name is required.'), 400
    if tp not in _ACCOUNT_TYPES:
        return jsonify(ok=False, message=f'Account type must be one of: {", ".join(_ACCOUNT_TYPES)}.'), 400
    if acc_mode == 'bank':
        if not bank_name or not account_number:
            return jsonify(ok=False, message='bank_name and account_number are required for bank accounts.'), 400
    else:
        bank_name = ''
        account_number = ''
        iban = ''
    dup = (Account.query
           .filter(
               Account.id != row.id,
               Account.is_void == False,
               func.lower(func.trim(Account.name)) == nm.lower()
           ).first())
    if dup:
        return jsonify(ok=False, message='Another account with this name already exists.'), 400
    row.name = nm
    row.type = tp
    row.opening_balance = float(opening or 0.0)
    row.bank_name = bank_name or None
    row.account_number = account_number or None
    row.iban = iban or None
    row.status = (payload.get('status') or row.status or 'active').strip().lower()
    db.session.commit()
    acc_group, acc_mode = _account_group_mode_for_row(row)
    return jsonify(ok=True, account={
        'id': int(row.id),
        'name': row.name,
        'type': row.type,
        'account_group': acc_group,
        'account_mode': acc_mode,
        'opening_balance': float(row.opening_balance or 0.0),
        'bank_name': (row.bank_name or ''),
        'account_number': (row.account_number or ''),
        'iban': (row.iban or ''),
        'status': (row.status or 'active'),
    })


@app.route('/api/accounts/create_transaction', methods=['POST'])
@login_required
def api_accounts_create_transaction():
    if _admin_only():
        return jsonify(ok=False, message='Admin access required.'), 403
    payload = request.get_json(silent=True) or request.form
    ok, msg, rows = _create_accounts_transaction_with_sync(payload)
    if not ok:
        return jsonify(ok=False, message=(msg or 'Unable to create transaction.')), 400
    return jsonify(ok=True, created_count=len(rows), items=[_account_txn_to_dict(r) for r in rows])


@app.route('/api/accounts/transaction_history', methods=['GET'])
@login_required
def api_accounts_transaction_history():
    if _admin_only():
        return jsonify(ok=False, message='Admin access required.'), 403
    account_id = request.args.get('account_id', type=int)
    account_group = (request.args.get('account_group') or '').strip()
    account_group = (_normalize_account_group(account_group) if account_group else '')
    project_id = request.args.get('project_id', type=int)
    stage_id = request.args.get('stage_id', type=int)
    tx_type = (request.args.get('transaction_type') or request.args.get('type') or '').strip().lower()
    tx_direction = _normalize_account_tx_direction((request.args.get('direction') or '').strip().lower())
    category = (request.args.get('category') or '').strip().lower()
    group_id = (request.args.get('group_id') or '').strip()
    reference_id = (request.args.get('reference_id') or '').strip()
    limit = request.args.get('limit', type=int) or 500
    date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
    date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
    rows = _account_transaction_history(
        account_id=(account_id or None),
        account_group=(account_group or None),
        date_from=date_from,
        date_to=date_to,
        project_id=(project_id or None),
        stage_id=(stage_id or None),
        tx_type=(tx_type or None),
        tx_direction=(tx_direction or None),
        category=(category or None),
        group_id=(group_id or None),
        reference_id=(reference_id or None),
        limit=limit
    )
    return jsonify(ok=True, count=len(rows), items=[_account_txn_to_dict(r) for r in rows])


@app.route('/api/accounts/dashboard_summary', methods=['GET'])
@login_required
def api_accounts_dashboard_summary():
    if _admin_only():
        return jsonify(ok=False, message='Admin access required.'), 403
    date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
    date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
    return jsonify(ok=True, items=_account_dashboard_kpis(date_from=date_from, date_to=date_to))


@app.route('/api/accounts/dashboard_subgroups', methods=['GET'])
@login_required
def api_accounts_dashboard_subgroups():
    if _admin_only():
        return jsonify(ok=False, message='Admin access required.'), 403
    metric = (request.args.get('metric') or '').strip().lower()
    return jsonify(ok=True, items=_account_dashboard_subgroups(metric))


@app.route('/api/accounts/pending_context', methods=['GET'])
@login_required
def api_accounts_pending_context():
    if _admin_only():
        return jsonify(ok=False, message='Admin access required.'), 403
    tx_type = (request.args.get('type') or request.args.get('transaction_type') or '').strip().lower()
    related_entity_type = (request.args.get('related_entity_type') or '').strip().lower()
    related_entity_id = request.args.get('related_entity_id', type=int) or 0
    project_id = request.args.get('project_id', type=int) or 0
    stage_id = request.args.get('stage_id', type=int) or 0
    snap = _account_pending_snapshot(
        tx_type,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        project_id=project_id,
        stage_id=stage_id
    )
    return jsonify(ok=True, item=snap)


@app.route('/api/accounts/reconciliation_summary', methods=['GET'])
@login_required
def api_accounts_reconciliation_summary():
    if _admin_only():
        return jsonify(ok=False, message='Admin access required.'), 403
    limit = request.args.get('limit', type=int) or 50
    limit = max(1, min(int(limit), 200))
    items = _accounts_reconciliation_snapshot(limit=limit)
    total_issues = int(sum(int(i.get('count') or 0) for i in items))
    return jsonify(ok=True, total_issues=total_issues, items=items)


@app.route('/api/accounts/forensic_report', methods=['GET'])
@login_required
def api_accounts_forensic_report():
    if _admin_only():
        return jsonify(ok=False, message='Admin access required.'), 403
    limit = request.args.get('limit', type=int) or 100
    limit = max(1, min(int(limit), 500))
    report = _accounts_forensic_report(limit=limit)
    total_orphans = int(sum(int(i.get('count') or 0) for i in report.get('orphans', [])))
    total_recon = int(sum(int(i.get('count') or 0) for i in report.get('reconciliation', [])))
    return jsonify(ok=True, total_issues=(total_orphans + total_recon), report=report)


def _run_migrations():
    """Safely add new columns to existing tables (SQLite-compatible)."""
    with db.engine.connect() as conn:
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_worker_trade (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(80) UNIQUE NOT NULL,
                    active_status BOOLEAN DEFAULT 1,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_expense_category (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(80) UNIQUE NOT NULL,
                    active_status BOOLEAN DEFAULT 1,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_stage_drawing (
                    id INTEGER PRIMARY KEY,
                    stage_id INTEGER NOT NULL REFERENCES hdc_stage(id),
                    original_name VARCHAR(255) NOT NULL,
                    stored_name VARCHAR(255) NOT NULL UNIQUE,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_attendance_mark (
                    id INTEGER PRIMARY KEY,
                    worker_id INTEGER NOT NULL REFERENCES hdc_worker(id),
                    date DATE NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'absent',
                    notes VARCHAR(250),
                    activity_at DATETIME,
                    created_at DATETIME,
                    CONSTRAINT uq_attendance_mark_worker_date UNIQUE (worker_id, date)
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_attendance_day (
                    id INTEGER PRIMARY KEY,
                    worker_id INTEGER NOT NULL REFERENCES hdc_worker(id),
                    date DATE NOT NULL,
                    total_hours FLOAT DEFAULT 0,
                    day_value FLOAT DEFAULT 0,
                    overtime_hours FLOAT DEFAULT 0,
                    entry_count INTEGER DEFAULT 0,
                    is_void BOOLEAN DEFAULT 0,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_attendance_day_worker_date UNIQUE (worker_id, date)
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_subcontract_attendance (
                    id INTEGER PRIMARY KEY,
                    subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                    date DATE NOT NULL,
                    present_count INTEGER DEFAULT 0,
                    work_done_pct FLOAT DEFAULT 0,
                    notes VARCHAR(250),
                    activity_at DATETIME,
                    created_at DATETIME,
                    CONSTRAINT uq_subcontract_attendance_sub_date UNIQUE (subcontractor_id, date)
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_subcontract_labour_worker (
                    id INTEGER PRIMARY KEY,
                    subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                    name VARCHAR(120) NOT NULL,
                    phone VARCHAR(30),
                    trade VARCHAR(80),
                    daily_wage FLOAT DEFAULT 0,
                    active_status BOOLEAN DEFAULT 1,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_subcontract_labour_payment (
                    id INTEGER PRIMARY KEY,
                    subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                    worker_id INTEGER NOT NULL REFERENCES hdc_subcontract_labour_worker(id),
                    amount FLOAT DEFAULT 0,
                    date DATE,
                    notes VARCHAR(250),
                    activity_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_subcontract_labour_attendance (
                    id INTEGER PRIMARY KEY,
                    subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                    project_id INTEGER REFERENCES hdc_project(id),
                    stage_id INTEGER NOT NULL REFERENCES hdc_stage(id),
                    worker_id INTEGER REFERENCES hdc_subcontract_labour_worker(id),
                    date DATE NOT NULL,
                    labour_count INTEGER DEFAULT 0,
                    wage_rate FLOAT DEFAULT 0,
                    total_labour_paid FLOAT DEFAULT 0,
                    attendance_status VARCHAR(20) DEFAULT 'Present',
                    working_hours FLOAT DEFAULT 0,
                    overtime_hours FLOAT DEFAULT 0,
                    notes VARCHAR(250),
                    activity_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_sub_labour_sub_stage_worker_date UNIQUE (subcontractor_id, stage_id, worker_id, date)
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_subcontract_event (
                    id INTEGER PRIMARY KEY,
                    subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                    project_id INTEGER REFERENCES hdc_project(id),
                    stage_id INTEGER REFERENCES hdc_stage(id),
                    actor_user_id INTEGER REFERENCES hdc_user(id),
                    event_type VARCHAR(40) NOT NULL,
                    from_value VARCHAR(250),
                    to_value VARCHAR(250),
                    amount FLOAT DEFAULT 0,
                    notes VARCHAR(300),
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_office_staff (
                    id INTEGER PRIMARY KEY,
                    staff_code VARCHAR(20) UNIQUE NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    role_type VARCHAR(80),
                    phone VARCHAR(30),
                    monthly_salary FLOAT DEFAULT 0,
                    active_status BOOLEAN DEFAULT 1,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_office_staff_attendance (
                    id INTEGER PRIMARY KEY,
                    staff_id INTEGER NOT NULL REFERENCES hdc_office_staff(id),
                    date DATE NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'present',
                    notes VARCHAR(250),
                    activity_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_office_staff_attendance_staff_date UNIQUE (staff_id, date)
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_office_staff_ledger (
                    id INTEGER PRIMARY KEY,
                    staff_id INTEGER NOT NULL REFERENCES hdc_office_staff(id),
                    date DATE,
                    entry_type VARCHAR(20) NOT NULL,
                    amount FLOAT DEFAULT 0,
                    notes TEXT,
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    activity_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_office_expense (
                    id INTEGER PRIMARY KEY,
                    date DATE,
                    category VARCHAR(80),
                    amount FLOAT DEFAULT 0,
                    remarks VARCHAR(250),
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    activity_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_activity_log (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER REFERENCES hdc_user(id),
                    username VARCHAR(80),
                    action_type VARCHAR(40) NOT NULL,
                    description TEXT NOT NULL,
                    entity_type VARCHAR(80) NOT NULL,
                    entity_id VARCHAR(80),
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_supplier (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(120) NOT NULL,
                    phone VARCHAR(30),
                    status VARCHAR(20) DEFAULT 'active',
                    is_void BOOLEAN DEFAULT 0,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_material_v2 (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(120) NOT NULL,
                    unit VARCHAR(20) DEFAULT 'KG',
                    status VARCHAR(20) DEFAULT 'active',
                    is_void BOOLEAN DEFAULT 0,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_purchase_v2 (
                    id INTEGER PRIMARY KEY,
                    supplier_id INTEGER NOT NULL REFERENCES hdc_supplier(id),
                    material_id INTEGER NOT NULL REFERENCES hdc_material_v2(id),
                    unit_price FLOAT DEFAULT 0,
                    quantity FLOAT DEFAULT 0,
                    total_amount FLOAT DEFAULT 0,
                    payment_status VARCHAR(20) DEFAULT 'unpaid',
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_supplier_ledger (
                    id INTEGER PRIMARY KEY,
                    supplier_id INTEGER NOT NULL REFERENCES hdc_supplier(id),
                    entry_type VARCHAR(20) NOT NULL,
                    amount FLOAT DEFAULT 0,
                    reference_type VARCHAR(50),
                    reference_id INTEGER,
                    note VARCHAR(300),
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_delivery (
                    id INTEGER PRIMARY KEY,
                    purchase_id INTEGER NOT NULL REFERENCES hdc_purchase_v2(id),
                    material_id INTEGER NOT NULL REFERENCES hdc_material_v2(id),
                    project_id INTEGER NOT NULL REFERENCES hdc_project(id),
                    stage_id INTEGER REFERENCES hdc_stage(id),
                    quantity FLOAT DEFAULT 0,
                    delivery_person VARCHAR(120),
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_usage_log_v2 (
                    id INTEGER PRIMARY KEY,
                    purchase_id INTEGER REFERENCES hdc_purchase_v2(id),
                    material_id INTEGER NOT NULL REFERENCES hdc_material_v2(id),
                    project_id INTEGER NOT NULL REFERENCES hdc_project(id),
                    stage_id INTEGER REFERENCES hdc_stage(id),
                    quantity FLOAT DEFAULT 0,
                    cost FLOAT DEFAULT 0,
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        for stmt in [
            "ALTER TABLE hdc_attendance ADD COLUMN stage_id INTEGER REFERENCES hdc_stage(id)",
            "ALTER TABLE hdc_attendance ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_expense ADD COLUMN stage_id INTEGER REFERENCES hdc_stage(id)",
            "ALTER TABLE hdc_expense ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_expense ADD COLUMN tip_worker_id INTEGER REFERENCES hdc_worker(id)",
            "ALTER TABLE hdc_expense ADD COLUMN category_id INTEGER REFERENCES hdc_expense_category(id)",
            "ALTER TABLE hdc_expense ADD COLUMN is_void BOOLEAN DEFAULT 0",
            "ALTER TABLE hdc_expense ADD COLUMN void_reason VARCHAR(250)",
            "ALTER TABLE hdc_expense ADD COLUMN voided_at DATETIME",
            "ALTER TABLE hdc_subcontractor ADD COLUMN stage_id INTEGER REFERENCES hdc_stage(id)",
            "ALTER TABLE hdc_subcontractor ADD COLUMN phone VARCHAR(30)",
            "ALTER TABLE hdc_subcontractor ADD COLUMN subcontractor_code VARCHAR(20)",
            "ALTER TABLE hdc_subcontractor ADD COLUMN work_done_percentage FLOAT DEFAULT 0",
            "ALTER TABLE hdc_owner_payment ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_project ADD COLUMN client_phone VARCHAR(30)",
            "ALTER TABLE hdc_stage_definition ADD COLUMN project_id INTEGER REFERENCES hdc_project(id)",
            "ALTER TABLE hdc_stage_definition ADD COLUMN active_status BOOLEAN DEFAULT 1",
            "ALTER TABLE hdc_subcontract_payment ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_subcontract_payment ADD COLUMN project_id INTEGER REFERENCES hdc_project(id)",
            "ALTER TABLE hdc_subcontract_payment ADD COLUMN stage_id INTEGER REFERENCES hdc_stage(id)",
            "ALTER TABLE hdc_subcontract_payment ADD COLUMN entry_type VARCHAR(20)",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_time_entry ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_time_entry ADD COLUMN is_void BOOLEAN DEFAULT 0",
            "ALTER TABLE hdc_time_entry ADD COLUMN void_reason VARCHAR(250)",
            "ALTER TABLE hdc_time_entry ADD COLUMN voided_at DATETIME",
            "ALTER TABLE hdc_material_usage ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_purchase ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_purchase ADD COLUMN entry_type VARCHAR(20)",
            "ALTER TABLE hdc_purchase ADD COLUMN supplier_name VARCHAR(120)",
            "ALTER TABLE hdc_purchase ADD COLUMN return_ref VARCHAR(80)",
            "ALTER TABLE hdc_purchase ADD COLUMN return_reason VARCHAR(200)",
            "ALTER TABLE hdc_purchase ADD COLUMN approved_by VARCHAR(100)",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN stage_id INTEGER REFERENCES hdc_stage(id)",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN time_entry_id INTEGER REFERENCES hdc_time_entry(id)",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN is_void BOOLEAN DEFAULT 0",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN void_reason VARCHAR(250)",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN voided_at DATETIME",
            "ALTER TABLE hdc_project ADD COLUMN client VARCHAR(100)",
            "ALTER TABLE hdc_project ADD COLUMN estimation_id INTEGER REFERENCES hdc_estimation(id)",
            "ALTER TABLE hdc_project ADD COLUMN budget_total FLOAT",
            "ALTER TABLE hdc_project ADD COLUMN planned_start DATE",
            "ALTER TABLE hdc_project ADD COLUMN planned_end DATE",
            "ALTER TABLE hdc_stage ADD COLUMN estimated_cost FLOAT",
            "ALTER TABLE hdc_stage ADD COLUMN progress FLOAT",
            "ALTER TABLE hdc_stage ADD COLUMN execution_mode VARCHAR(20) DEFAULT 'company'",
            "ALTER TABLE hdc_stage ADD COLUMN assigned_subcontractor_id INTEGER REFERENCES hdc_subcontractor(id)",
            "ALTER TABLE hdc_stage ADD COLUMN start_date DATE",
            "ALTER TABLE hdc_stage ADD COLUMN end_date DATE",
            "ALTER TABLE hdc_worker ADD COLUMN wage_type VARCHAR(20)",
            "ALTER TABLE hdc_worker ADD COLUMN hourly_rate FLOAT",
            "ALTER TABLE hdc_worker ADD COLUMN rate_per_sqft FLOAT",
            "ALTER TABLE hdc_subcontract_labour_attendance ADD COLUMN worker_id INTEGER REFERENCES hdc_subcontract_labour_worker(id)",
            "ALTER TABLE hdc_subcontract_labour_attendance ADD COLUMN attendance_status VARCHAR(20) DEFAULT 'Present'",
            "ALTER TABLE hdc_subcontract_labour_attendance ADD COLUMN working_hours FLOAT DEFAULT 0",
            "ALTER TABLE hdc_subcontract_labour_attendance ADD COLUMN overtime_hours FLOAT DEFAULT 0",
            "ALTER TABLE hdc_office_expense ADD COLUMN office_staff_id INTEGER REFERENCES hdc_office_staff(id)",
            "ALTER TABLE hdc_office_expense ADD COLUMN office_staff_ledger_id INTEGER REFERENCES hdc_office_staff_ledger(id)",
            "ALTER TABLE hdc_subcontract_labour_payment ADD COLUMN is_void BOOLEAN DEFAULT 0",
            "ALTER TABLE hdc_subcontract_labour_payment ADD COLUMN void_reason VARCHAR(250)",
            "ALTER TABLE hdc_subcontract_labour_payment ADD COLUMN voided_at DATETIME",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                pass   # column already exists or table doesn't exist yet
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_stage_definition_project_name_idx ON hdc_stage_definition(project_id, name)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_subcontractor_code_idx ON hdc_subcontractor(subcontractor_code)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_event_sub_time ON hdc_subcontract_event(subcontractor_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_labour_sub_time ON hdc_subcontract_labour_attendance(subcontractor_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_labour_worker_time ON hdc_subcontract_labour_attendance(worker_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_sub_labour_sub_stage_worker_or_bulk_date
                ON hdc_subcontract_labour_attendance(subcontractor_id, stage_id, ifnull(worker_id, -1), date)
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_sub_labour_sub_worker_daily
                ON hdc_subcontract_labour_attendance(subcontractor_id, worker_id, date)
                WHERE worker_id IS NOT NULL
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_lab_pay_worker_time ON hdc_subcontract_labour_payment(worker_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_time_entry_active_key
                ON hdc_time_entry(worker_id, project_id, stage_id, check_in)
                WHERE is_void = 0
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_office_staff_code_idx ON hdc_office_staff(staff_code)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_office_staff_attendance_staff_date_idx ON hdc_office_staff_attendance(staff_id, date)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_office_staff_ledger_staff_date ON hdc_office_staff_ledger(staff_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_office_expense_date ON hdc_office_expense(date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_office_expense_staff_ledger ON hdc_office_expense(office_staff_ledger_id, office_staff_id, date)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_expense_project_date ON hdc_expense(project_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_expense_stage_date ON hdc_expense(stage_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_owner_payment_project_date ON hdc_owner_payment(project_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_time_entry_active_project_checkin
                ON hdc_time_entry(project_id, check_in, id)
                WHERE is_void = 0
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_time_entry_active_worker_checkin
                ON hdc_time_entry(worker_id, check_in, id)
                WHERE is_void = 0
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_payment_sub_date ON hdc_subcontract_payment(subcontractor_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_purchase_project_date ON hdc_purchase(project_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_expense_category_name_ci ON hdc_expense_category(lower(trim(name)))"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_name_ci ON hdc_supplier(lower(trim(name))) WHERE is_void = 0"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_material_v2_name_ci ON hdc_material_v2(lower(trim(name))) WHERE is_void = 0"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_activity_log_created_at ON hdc_activity_log(created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_activity_log_user_created ON hdc_activity_log(user_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_activity_log_entity ON hdc_activity_log(entity_type, entity_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_purchase_v2_mat_date ON hdc_purchase_v2(material_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_delivery_mat_scope ON hdc_delivery(material_id, project_id, stage_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_usage_v2_mat_scope ON hdc_usage_log_v2(material_id, project_id, stage_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_usage_v2_purchase_scope ON hdc_usage_log_v2(purchase_id, project_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        # Rebuild stage definition table if legacy schema has global UNIQUE(name).
        try:
            idx_rows = conn.execute(text("PRAGMA index_list('hdc_stage_definition')")).fetchall()
            has_global_name_unique = False
            for row in idx_rows:
                idx_name = row[1]
                is_unique = int(row[2]) == 1
                if not is_unique:
                    continue
                cols = conn.execute(text(f"PRAGMA index_info('{idx_name}')")).fetchall()
                idx_cols = [c[2] for c in cols]
                if idx_cols == ['name']:
                    has_global_name_unique = True
                    break
            if has_global_name_unique:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS hdc_stage_definition_new (
                        id INTEGER PRIMARY KEY,
                        project_id INTEGER REFERENCES hdc_project(id),
                        name VARCHAR(120) NOT NULL,
                        default_order INTEGER DEFAULT 0,
                        active_status BOOLEAN DEFAULT 1,
                        created_at DATETIME,
                        CONSTRAINT uq_stage_definition_project_name UNIQUE (project_id, name)
                    )
                """))
                conn.execute(text("""
                    INSERT INTO hdc_stage_definition_new (id, project_id, name, default_order, active_status, created_at)
                    SELECT id, NULL, name, default_order, 1, created_at FROM hdc_stage_definition
                """))
                conn.execute(text("DROP TABLE hdc_stage_definition"))
                conn.execute(text("ALTER TABLE hdc_stage_definition_new RENAME TO hdc_stage_definition"))
                conn.commit()
        except Exception:
            conn.rollback()

        # Rebuild subcontractor table if legacy schema enforces NOT NULL project_id.
        try:
            cols = conn.execute(text("PRAGMA table_info('hdc_subcontractor')")).fetchall()
            proj_notnull = False
            for c in cols:
                # cid, name, type, notnull, dflt_value, pk
                if str(c[1]).lower() == 'project_id' and int(c[3] or 0) == 1:
                    proj_notnull = True
                    break
            if proj_notnull:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS hdc_subcontractor_new (
                        id INTEGER PRIMARY KEY,
                        subcontractor_code VARCHAR(20),
                        project_id INTEGER REFERENCES hdc_project(id),
                        stage_id INTEGER REFERENCES hdc_stage(id),
                        name VARCHAR(100) NOT NULL,
                        phone VARCHAR(30),
                        work_type VARCHAR(100),
                        contract_type VARCHAR(20) DEFAULT 'lump_sum',
                        rate_per_sqft FLOAT DEFAULT 0,
                        total_sqft FLOAT DEFAULT 0,
                        lump_sum_amount FLOAT DEFAULT 0,
                        retention_percentage FLOAT DEFAULT 0,
                        work_done_percentage FLOAT DEFAULT 0,
                        created_at DATETIME
                    )
                """))
                conn.execute(text("""
                    INSERT INTO hdc_subcontractor_new (
                        id, subcontractor_code, project_id, stage_id, name, phone, work_type,
                        contract_type, rate_per_sqft, total_sqft, lump_sum_amount,
                        retention_percentage, work_done_percentage, created_at
                    )
                    SELECT
                        id, subcontractor_code, project_id, stage_id, name, phone, work_type,
                        contract_type, rate_per_sqft, total_sqft, lump_sum_amount,
                        retention_percentage, work_done_percentage, created_at
                    FROM hdc_subcontractor
                """))
                conn.execute(text("DROP TABLE hdc_subcontractor"))
                conn.execute(text("ALTER TABLE hdc_subcontractor_new RENAME TO hdc_subcontractor"))
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_subcontractor_code_idx ON hdc_subcontractor(subcontractor_code)"))
                conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.commit()
        except Exception:
            try:
                conn.execute(text("PRAGMA foreign_keys=ON"))
            except Exception:
                pass
            conn.rollback()

        # Rebuild subcontract labour attendance table if legacy UNIQUE key does not include worker_id.
        try:
            idx_rows = conn.execute(text("PRAGMA index_list('hdc_subcontract_labour_attendance')")).fetchall()
            needs_rebuild = True
            for row in idx_rows:
                idx_name = row[1]
                is_unique = int(row[2]) == 1
                if not is_unique:
                    continue
                cols = conn.execute(text(f"PRAGMA index_info('{idx_name}')")).fetchall()
                idx_cols = [c[2] for c in cols]
                if idx_cols == ['subcontractor_id', 'stage_id', 'worker_id', 'date']:
                    needs_rebuild = False
                    break
            if needs_rebuild:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS hdc_subcontract_labour_attendance_new (
                        id INTEGER PRIMARY KEY,
                        subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                        project_id INTEGER REFERENCES hdc_project(id),
                        stage_id INTEGER NOT NULL REFERENCES hdc_stage(id),
                        worker_id INTEGER REFERENCES hdc_subcontract_labour_worker(id),
                        date DATE NOT NULL,
                        labour_count INTEGER DEFAULT 0,
                        wage_rate FLOAT DEFAULT 0,
                        total_labour_paid FLOAT DEFAULT 0,
                        attendance_status VARCHAR(20) DEFAULT 'Present',
                        working_hours FLOAT DEFAULT 0,
                        overtime_hours FLOAT DEFAULT 0,
                        notes VARCHAR(250),
                        activity_at DATETIME,
                        created_at DATETIME,
                        updated_at DATETIME,
                        CONSTRAINT uq_sub_labour_sub_stage_worker_date UNIQUE (subcontractor_id, stage_id, worker_id, date)
                    )
                """))
                conn.execute(text("""
                    INSERT INTO hdc_subcontract_labour_attendance_new (
                        id, subcontractor_id, project_id, stage_id, worker_id, date, labour_count,
                        wage_rate, total_labour_paid, attendance_status, working_hours, overtime_hours,
                        notes, activity_at, created_at, updated_at
                    )
                    SELECT
                        id, subcontractor_id, project_id, stage_id, worker_id, date, labour_count,
                        wage_rate, total_labour_paid, 'Present', 0, 0, notes, activity_at, created_at, updated_at
                    FROM hdc_subcontract_labour_attendance
                """))
                conn.execute(text("DROP TABLE hdc_subcontract_labour_attendance"))
                conn.execute(text("ALTER TABLE hdc_subcontract_labour_attendance_new RENAME TO hdc_subcontract_labour_attendance"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_labour_sub_time ON hdc_subcontract_labour_attendance(subcontractor_id, date, id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_labour_worker_time ON hdc_subcontract_labour_attendance(worker_id, date, id)"))
                conn.execute(text("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_sub_labour_sub_stage_worker_or_bulk_date
                    ON hdc_subcontract_labour_attendance(subcontractor_id, stage_id, ifnull(worker_id, -1), date)
                """))
                conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.commit()
        except Exception:
            try:
                conn.execute(text("PRAGMA foreign_keys=ON"))
            except Exception:
                pass
            conn.rollback()

        # Expense category migration: text -> category_id, then rebuild to drop legacy text column.
        try:
            cols = conn.execute(text("PRAGMA table_info('hdc_expense')")).fetchall()
            col_names = [str(c[1]).lower() for c in cols]
            has_category = 'category' in col_names
            has_category_id = 'category_id' in col_names
            if has_category_id:
                # Ensure default fallback category exists.
                conn.execute(text("""
                    INSERT INTO hdc_expense_category(name, active_status, created_at)
                    SELECT 'Misc', 1, CURRENT_TIMESTAMP
                    WHERE NOT EXISTS (
                        SELECT 1 FROM hdc_expense_category WHERE lower(trim(name)) = 'misc'
                    )
                """))
                if has_category:
                    conn.execute(text("""
                        INSERT INTO hdc_expense_category(name, active_status, created_at)
                        SELECT DISTINCT trim(category), 1, CURRENT_TIMESTAMP
                        FROM hdc_expense
                        WHERE category IS NOT NULL AND trim(category) <> ''
                          AND lower(trim(category)) NOT IN (
                              SELECT lower(trim(name)) FROM hdc_expense_category
                          )
                    """))
                    conn.execute(text("""
                        UPDATE hdc_expense
                        SET category_id = (
                            SELECT id FROM hdc_expense_category c
                            WHERE lower(trim(c.name)) = lower(trim(hdc_expense.category))
                            LIMIT 1
                        )
                        WHERE category_id IS NULL
                          AND category IS NOT NULL
                          AND trim(category) <> ''
                    """))
                conn.execute(text("""
                    UPDATE hdc_expense
                    SET category_id = (
                        SELECT id FROM hdc_expense_category
                        WHERE lower(trim(name)) = 'misc'
                        LIMIT 1
                    )
                    WHERE category_id IS NULL
                """))
                # Rebuild only if legacy text column still exists.
                if has_category:
                    conn.execute(text("PRAGMA foreign_keys=OFF"))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS hdc_expense_new (
                            id INTEGER PRIMARY KEY,
                            project_id INTEGER NOT NULL REFERENCES hdc_project(id),
                            stage_id INTEGER REFERENCES hdc_stage(id),
                            tip_worker_id INTEGER REFERENCES hdc_worker(id),
                            category_id INTEGER NOT NULL REFERENCES hdc_expense_category(id),
                            amount FLOAT DEFAULT 0,
                            date DATE,
                            remarks VARCHAR(200),
                            is_void BOOLEAN DEFAULT 0,
                            void_reason VARCHAR(250),
                            voided_at DATETIME,
                            activity_at DATETIME,
                            created_at DATETIME
                        )
                    """))
                    conn.execute(text("""
                        INSERT INTO hdc_expense_new (
                            id, project_id, stage_id, tip_worker_id, category_id,
                            amount, date, remarks, is_void, void_reason, voided_at,
                            activity_at, created_at
                        )
                        SELECT
                            id, project_id, stage_id, tip_worker_id,
                            COALESCE(
                                category_id,
                                (SELECT id FROM hdc_expense_category WHERE lower(trim(name)) = 'misc' LIMIT 1)
                            ),
                            amount, date, remarks,
                            COALESCE(is_void, 0), void_reason, voided_at,
                            activity_at, created_at
                        FROM hdc_expense
                    """))
                    conn.execute(text("DROP TABLE hdc_expense"))
                    conn.execute(text("ALTER TABLE hdc_expense_new RENAME TO hdc_expense"))
                    conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_expense_project_date ON hdc_expense(project_id, date, id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_expense_stage_date ON hdc_expense(stage_id, date, id)"))
                conn.commit()
        except Exception:
            try:
                conn.execute(text("PRAGMA foreign_keys=ON"))
            except Exception:
                pass
            conn.rollback()

        # Backfill activity timestamps for legacy rows.
        backfills = [
            "UPDATE hdc_owner_payment SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_subcontract_payment SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_subcontract_payment SET entry_type = COALESCE(NULLIF(entry_type, ''), 'payment')",
            "UPDATE hdc_labour_ledger SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_expense SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_purchase SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_purchase SET entry_type = COALESCE(NULLIF(entry_type, ''), 'purchase')",
            "UPDATE hdc_material_usage SET activity_at = COALESCE(activity_at, created_at, datetime(used_at || ' 12:00:00'))",
            "UPDATE hdc_attendance SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_time_entry SET activity_at = COALESCE(activity_at, check_in, created_at)",
            "UPDATE hdc_time_entry SET is_void = COALESCE(is_void, 0)",
            "UPDATE hdc_labour_ledger SET is_void = COALESCE(is_void, 0)",
            "UPDATE hdc_subcontractor SET work_done_percentage = COALESCE(work_done_percentage, 0)",
            "UPDATE hdc_subcontractor SET subcontractor_code = COALESCE(NULLIF(subcontractor_code, ''), ('SUB-' || printf('%04d', id)))",
            "UPDATE hdc_stage SET execution_mode = COALESCE(NULLIF(execution_mode, ''), 'company')",
            "UPDATE hdc_subcontract_attendance SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_subcontract_labour_attendance SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_subcontract_labour_attendance SET updated_at = COALESCE(updated_at, activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_subcontract_labour_attendance SET total_labour_paid = COALESCE(total_labour_paid, 0)",
            "UPDATE hdc_subcontract_labour_attendance SET wage_rate = COALESCE(wage_rate, 0)",
            "UPDATE hdc_subcontract_labour_attendance SET labour_count = COALESCE(labour_count, 0)",
            "UPDATE hdc_subcontract_labour_attendance SET attendance_status = COALESCE(NULLIF(attendance_status, ''), CASE WHEN COALESCE(labour_count, 0) > 0 THEN 'Present' ELSE 'Absent' END)",
            "UPDATE hdc_subcontract_labour_attendance SET working_hours = COALESCE(working_hours, 0)",
            "UPDATE hdc_subcontract_labour_attendance SET overtime_hours = COALESCE(overtime_hours, 0)",
            "UPDATE hdc_subcontract_labour_payment SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))"
        ]
        for stmt in backfills:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass
        try:
            conn.execute(text("""
                INSERT INTO hdc_subcontract_event
                    (subcontractor_id, project_id, stage_id, actor_user_id, event_type, from_value, to_value, amount, notes, created_at)
                SELECT
                    s.id, s.project_id, s.stage_id, NULL, 'create', '', COALESCE(s.subcontractor_code, ''), 0,
                    'Backfill: subcontractor profile', COALESCE(s.created_at, CURRENT_TIMESTAMP)
                FROM hdc_subcontractor s
                WHERE NOT EXISTS (
                    SELECT 1 FROM hdc_subcontract_event e
                    WHERE e.subcontractor_id = s.id AND e.event_type = 'create'
                )
            """))
            conn.commit()
        except Exception:
            pass

        try:
            conn.execute(text("""
                INSERT INTO hdc_subcontract_event
                    (subcontractor_id, project_id, stage_id, actor_user_id, event_type, from_value, to_value, amount, notes, created_at)
                SELECT
                    s.id, s.project_id, s.stage_id, NULL, 'shift', 'backfill',
                    COALESCE(st.name, ('STAGE#' || s.stage_id)), 0,
                    'Backfill: stage assignment',
                    COALESCE(st.created_at, s.created_at, CURRENT_TIMESTAMP)
                FROM hdc_subcontractor s
                LEFT JOIN hdc_stage st ON st.id = s.stage_id
                WHERE s.stage_id IS NOT NULL
                  AND s.project_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM hdc_subcontract_event e
                    WHERE e.subcontractor_id = s.id
                      AND e.stage_id = s.stage_id
                      AND e.event_type IN ('shift', 'reassign')
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                INSERT INTO hdc_subcontract_event
                    (subcontractor_id, project_id, stage_id, actor_user_id, event_type, from_value, to_value, amount, notes, created_at)
                SELECT
                    s.id, s.project_id, s.stage_id, NULL, 'price_update', '',
                    COALESCE(s.contract_type, ''),
                    CASE
                        WHEN COALESCE(s.contract_type, '') = 'sqft' THEN COALESCE(s.rate_per_sqft, 0) * COALESCE(s.total_sqft, 0)
                        ELSE COALESCE(s.lump_sum_amount, 0)
                    END,
                    ('Backfill: Rate ' || COALESCE(s.rate_per_sqft, 0) || ' | Sqft ' || COALESCE(s.total_sqft, 0) || ' | Lump ' || COALESCE(s.lump_sum_amount, 0)),
                    COALESCE(s.created_at, CURRENT_TIMESTAMP)
                FROM hdc_subcontractor s
                WHERE s.stage_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM hdc_subcontract_event e
                    WHERE e.subcontractor_id = s.id
                      AND e.stage_id = s.stage_id
                      AND e.event_type = 'price_update'
                )
            """))
            conn.commit()
        except Exception:
            pass


def _ensure_table_columns_sqlite(table_name, columns):
    """Best-effort column auto-heal for SQLite tables on legacy DB files."""
    if not table_name or not columns:
        return
    with db.engine.connect() as conn:
        try:
            rows = conn.execute(text(f"PRAGMA table_info('{table_name}')")).fetchall()
            existing = {str(r[1]).lower() for r in rows}
        except Exception:
            return
        for col_name, col_ddl in columns.items():
            key = str(col_name or '').strip().lower()
            if not key or key in existing:
                continue
            try:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_ddl}"))
                conn.commit()
                existing.add(key)
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass


def _ensure_subcontract_labour_attendance_schema():
    _ensure_table_columns_sqlite('hdc_subcontract_labour_attendance', {
        'worker_id': "worker_id INTEGER REFERENCES hdc_subcontract_labour_worker(id)",
        'attendance_status': "attendance_status VARCHAR(20) DEFAULT 'Present'",
        'working_hours': "working_hours FLOAT DEFAULT 0",
        'overtime_hours': "overtime_hours FLOAT DEFAULT 0",
    })


def _migrate_attendance_to_time_entries():
    """Convert legacy attendance into time entries (one-time)."""
    if _runtime_flag_get('time_entry_migration_done') == '1':
        return
    try:
        records = Attendance.query.order_by(Attendance.id).all()
        for a in records:
            if TimeEntry.query.filter_by(attendance_id=a.id).first():
                continue
            check_in = datetime.combine(a.date, datetime.strptime('09:00', '%H:%M').time())
            hours = float(a.hours_worked or 0)
            check_out = check_in + timedelta(hours=hours)
            te = TimeEntry(
                worker_id=a.worker_id,
                project_id=a.project_id,
                stage_id=a.stage_id,
                check_in=check_in,
                check_out=check_out,
                hours=hours,
                overtime=float(a.overtime_hours or 0),
                wage_calculated=float(a.total_wage or 0),
                legacy_calc=True,
                attendance_id=a.id,
                activity_at=check_in
            )
            db.session.add(te)
        db.session.commit()
        _runtime_flag_set('time_entry_migration_done', '1')
    except Exception:
        db.session.rollback()

def _ensure_purchase_v2_schema():
    _ensure_table_columns_sqlite('hdc_purchase_v2', {
        'date':       "date DATE",
        'notes':      "notes VARCHAR(300)",
        'challan_no': "challan_no VARCHAR(80)",
    })
    _ensure_table_columns_sqlite('hdc_delivery', {
        'date':  "date DATE",
        'notes': "notes VARCHAR(300)",
    })
    _ensure_table_columns_sqlite('hdc_usage_log_v2', {
        'purchase_id': "purchase_id INTEGER",
        'date':  "date DATE",
        'notes': "notes VARCHAR(300)",
    })
    _ensure_table_columns_sqlite('hdc_supplier', {
        'address': "address VARCHAR(250)",
        'email':   "email VARCHAR(120)",
    })
    _ensure_table_columns_sqlite('hdc_office_expense', {
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'void_reason': "void_reason VARCHAR(250)",
        'voided_at': "voided_at DATETIME",
    })
    try:
        with db.engine.connect() as conn:
            conn.execute(text("UPDATE hdc_office_expense SET is_void = COALESCE(is_void, 0)"))
            conn.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _ensure_owner_payment_void_schema():
    _ensure_table_columns_sqlite('hdc_owner_payment', {
        'received_to_account_id': "received_to_account_id INTEGER REFERENCES hdc_account(id)",
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'void_reason': "void_reason VARCHAR(250)",
        'voided_at': "voided_at DATETIME",
    })
    try:
        with db.engine.connect() as conn:
            conn.execute(text("UPDATE hdc_owner_payment SET is_void = COALESCE(is_void, 0)"))
            conn.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _ensure_subcontract_payment_void_schema():
    _ensure_table_columns_sqlite('hdc_subcontract_payment', {
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'void_reason': "void_reason VARCHAR(250)",
        'voided_at': "voided_at DATETIME",
    })
    try:
        with db.engine.connect() as conn:
            conn.execute(text("UPDATE hdc_subcontract_payment SET is_void = COALESCE(is_void, 0)"))
            conn.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _ensure_accounts_schema():
    with db.engine.connect() as conn:
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_account (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(120) NOT NULL,
                    type VARCHAR(20) NOT NULL,
                    opening_balance FLOAT DEFAULT 0,
                    bank_name VARCHAR(120),
                    account_number VARCHAR(80),
                    iban VARCHAR(80),
                    auto_generated BOOLEAN DEFAULT 0,
                    auto_source VARCHAR(40),
                    status VARCHAR(20) DEFAULT 'active',
                    is_void BOOLEAN DEFAULT 0,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_account_txn (
                    id INTEGER PRIMARY KEY,
                    date DATE NOT NULL,
                    amount FLOAT NOT NULL,
                    type VARCHAR(40),
                    from_account_id INTEGER NOT NULL REFERENCES hdc_account(id),
                    to_account_id INTEGER REFERENCES hdc_account(id),
                    executed_by_account_id INTEGER NOT NULL REFERENCES hdc_account(id),
                    project_id INTEGER REFERENCES hdc_project(id),
                    stage_id INTEGER REFERENCES hdc_stage(id),
                    related_entity_type VARCHAR(40),
                    related_entity_id INTEGER,
                    party_name VARCHAR(120),
                    category VARCHAR(20) NOT NULL,
                    note VARCHAR(400),
                    reference_id VARCHAR(120),
                    group_id VARCHAR(64),
                    source_type VARCHAR(80),
                    source_id INTEGER,
                    is_void BOOLEAN DEFAULT 0,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass

        idx_sql = [
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_from_date_id ON hdc_account_txn(from_account_id, date, id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_to_date_id ON hdc_account_txn(to_account_id, date, id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_exec_date_id ON hdc_account_txn(executed_by_account_id, date, id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_stage_date_id ON hdc_account_txn(stage_id, date, id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_group_id ON hdc_account_txn(group_id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_reference_id ON hdc_account_txn(reference_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_hdc_account_txn_source ON hdc_account_txn(source_type, source_id) WHERE source_type IS NOT NULL AND source_id IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_hdc_account_name_active_ci ON hdc_account(lower(trim(name))) WHERE is_void = 0",
        ]
        for sql in idx_sql:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

    _ensure_table_columns_sqlite('hdc_account', {
        'name': "name VARCHAR(120)",
        'type': "type VARCHAR(20)",
        'opening_balance': "opening_balance FLOAT DEFAULT 0",
        'bank_name': "bank_name VARCHAR(120)",
        'account_number': "account_number VARCHAR(80)",
        'iban': "iban VARCHAR(80)",
        'auto_generated': "auto_generated BOOLEAN DEFAULT 0",
        'auto_source': "auto_source VARCHAR(40)",
        'status': "status VARCHAR(20) DEFAULT 'active'",
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'created_at': "created_at DATETIME",
    })
    _ensure_table_columns_sqlite('hdc_account_txn', {
        'date': "date DATE",
        'amount': "amount FLOAT DEFAULT 0",
        'type': "type VARCHAR(40)",
        'from_account_id': "from_account_id INTEGER",
        'to_account_id': "to_account_id INTEGER",
        'executed_by_account_id': "executed_by_account_id INTEGER",
        'project_id': "project_id INTEGER REFERENCES hdc_project(id)",
        'stage_id': "stage_id INTEGER REFERENCES hdc_stage(id)",
        'related_entity_type': "related_entity_type VARCHAR(40)",
        'related_entity_id': "related_entity_id INTEGER",
        'party_name': "party_name VARCHAR(120)",
        'category': "category VARCHAR(20)",
        'note': "note VARCHAR(400)",
        'reference_id': "reference_id VARCHAR(120)",
        'group_id': "group_id VARCHAR(64)",
        'source_type': "source_type VARCHAR(80)",
        'source_id': "source_id INTEGER",
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'created_at': "created_at DATETIME",
    })


def _bootstrap_accounts_backfill_once():
    if _runtime_flag_get('accounts_backfill_done') == '1':
        return
    try:
        stats = _run_accounts_backfill()
        if int(stats.get('errors', 0) or 0) == 0:
            _runtime_flag_set('accounts_backfill_done', '1')
    except Exception:
        db.session.rollback()


def _backfill_owner_payment_receiving_accounts():
    try:
        company = _accounts_default_company_cash()
        rows = OwnerPayment.query.filter(OwnerPayment.received_to_account_id.is_(None)).all()
        changed = 0
        for op in rows:
            acc_id = None
            linked = (AccountTransaction.query
                      .filter(
                          AccountTransaction.source_id == int(op.id),
                          AccountTransaction.is_void == False,
                          or_(
                              func.lower(func.coalesce(AccountTransaction.source_type, '')) == 'owner_payment:direct',
                              func.lower(func.coalesce(AccountTransaction.source_type, '')).like('owner_payment:%')
                          )
                      )
                      .order_by(AccountTransaction.id.asc())
                      .first())
            if linked and linked.to_account_id:
                acc_id = int(linked.to_account_id)
            elif company and company.id:
                acc_id = int(company.id)
            if acc_id:
                op.received_to_account_id = acc_id
                changed += 1
        if changed > 0:
            db.session.commit()
    except Exception:
        db.session.rollback()


def _backfill_accounts_scope_from_references():
    try:
        rows = (AccountTransaction.query
                .filter(
                    AccountTransaction.is_void == False,
                    func.lower(func.coalesce(AccountTransaction.type, '')).in_(
                        ('expense_material', 'expense_wage', 'expense_subcontractor',
                         'expense_general', 'purchase', 'payroll', 'advance_to_person')
                    ),
                    or_(AccountTransaction.project_id.is_(None), AccountTransaction.stage_id.is_(None))
                )
                .all())
        changed = 0
        for t in rows:
            ref = (t.reference_id or '').strip().lower()
            if ref.startswith('labour_ledger#'):
                try:
                    lid = int(ref.split('#', 1)[1].strip())
                except Exception:
                    lid = 0
                if lid:
                    lrow = LabourLedger.query.get(lid)
                    if lrow:
                        if t.project_id is None and lrow.project_id:
                            t.project_id = int(lrow.project_id)
                            changed += 1
                        if t.stage_id is None and lrow.stage_id:
                            t.stage_id = int(lrow.stage_id)
                            changed += 1
        if changed > 0:
            db.session.commit()
    except Exception:
        db.session.rollback()


def _bootstrap_hdc():
    db.create_all()
    _run_migrations()
    _ensure_subcontract_labour_attendance_schema()
    _ensure_purchase_v2_schema()
    _ensure_owner_payment_void_schema()
    _ensure_subcontract_payment_void_schema()
    _ensure_accounts_schema()
    _ensure_runtime_flags_table()
    _migrate_legacy_done_markers_to_db()
    _migrate_attendance_to_time_entries()
    _reconcile_all_time_entries_once()
    _ensure_timeentry_unique_indexes()
    admin_username = (os.environ.get('HDC_BOOTSTRAP_ADMIN_USERNAME') or 'admin').strip() or 'admin'
    if not HDCUser.query.filter_by(username=admin_username).first():
        admin_pwd = os.environ.get('HDC_BOOTSTRAP_ADMIN_PASSWORD', '').strip()
        if not admin_pwd:
            admin_pwd = (os.environ.get('HDC_DEFAULT_ADMIN_PASSWORD') or 'Admin@1234').strip()
        ok_pwd, pwd_msg = _is_strong_password(admin_pwd)
        if not ok_pwd:
            raise RuntimeError(f'Invalid bootstrap admin password: {pwd_msg}')
        db.session.add(HDCUser(
            username=admin_username,
            password_hash=generate_password_hash(admin_pwd),
            role='admin'))
        db.session.commit()
        print(f"[HDC ERP] Initial admin created: {admin_username}")
    if not WorkerTrade.query.filter_by(active_status=True).first():
        default_trades = [
            'Mason', 'Labor', 'Carpenter', 'Steel Fixer',
            'Electrician', 'Plumber', 'Painter', 'Tile Fixer', 'Supervisor'
        ]
        for tr in default_trades:
            exists = db.session.query(WorkerTrade).filter(func.lower(WorkerTrade.name) == tr.lower()).first()
            if exists:
                exists.active_status = True
            else:
                db.session.add(WorkerTrade(name=tr, active_status=True))
        db.session.commit()
    if not ExpenseCategory.query.filter_by(active_status=True).first():
        default_categories = ['Food', 'Transport', 'Material', 'Labour', 'Vehicle', 'Misc', 'Tip']
        for cat in default_categories:
            exists = db.session.query(ExpenseCategory).filter(func.lower(ExpenseCategory.name) == cat.lower()).first()
            if exists:
                exists.active_status = True
            else:
                db.session.add(ExpenseCategory(name=cat, active_status=True))
        db.session.commit()
    _bootstrap_accounts_backfill_once()
    _backfill_owner_payment_receiving_accounts()
    _backfill_accounts_scope_from_references()
    _mark_auto_generated_person_accounts()

_HDC_BOOTSTRAP_DONE = False
_HDC_BOOTSTRAP_LOCK = threading.Lock()

def _ensure_bootstrap_once(force=False):
    global _HDC_BOOTSTRAP_DONE
    if _HDC_BOOTSTRAP_DONE and not force:
        return
    with _HDC_BOOTSTRAP_LOCK:
        if _HDC_BOOTSTRAP_DONE and not force:
            return
        with app.app_context():
            _bootstrap_hdc()
        if not os.path.exists(_DB_STORE):
            raise RuntimeError(f'Database file was not created at required path: {_DB_STORE}')
        _HDC_BOOTSTRAP_DONE = True

_ensure_bootstrap_once()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
