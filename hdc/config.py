"""Application configuration and per-app runtime paths.

Configuration is read from the environment (and an optional local ``.env``)
without baking a database path into the application package.  The legacy
module-level constants are retained for compatibility with ``hdc_erp``;
request-time code should use :func:`get_runtime_settings` instead so multiple
factory-created apps can safely coexist in one process.
"""

from dataclasses import dataclass
import os


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _load_local_env():
    """Load simple KEY=VALUE pairs from ``.env`` without overriding env vars."""
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


# This must happen before settings are constructed.  The original monolith
# supported .env files and production deployments rely on env precedence.
_load_local_env()


def _resolve_path(raw_path, default_path):
    val = (raw_path or '').strip()
    if not val:
        return os.path.abspath(default_path)
    val = os.path.expanduser(val)
    if not os.path.isabs(val):
        val = os.path.join(BASE_DIR, val)
    return os.path.abspath(val)


@dataclass(frozen=True)
class AppSettings:
    """Filesystem settings belonging to one Flask application instance."""

    base_dir: str
    instance_dir: str
    stage_drawings_dir: str
    db_path: str
    estimation_store: str
    backup_dir: str


def load_settings(instance_dir=None, db_path=None):
    """Build settings from current environment values.

    This is intentionally called for every ``create_app`` invocation rather
    than only at module import time.  It makes app-factory tests and command
    line tools with separate databases deterministic.
    """
    _load_local_env()
    raw_instance = (instance_dir if instance_dir is not None
                    else os.environ.get('HDC_INSTANCE_DIR'))
    resolved_instance = _resolve_path(
        raw_instance,
        os.path.join(BASE_DIR, 'hdc_instance')
    )

    raw_db = db_path if db_path is not None else os.environ.get('HDC_DB_PATH')
    if raw_db is None or not str(raw_db).strip():
        raw_db = (
            os.path.join(resolved_instance, 'hdc_erp_integrated.db')
            if os.path.exists(os.path.join(resolved_instance, 'hdc_erp_integrated.db'))
            else os.path.join(resolved_instance, 'hdc_erp.db')
        )
    resolved_db = _resolve_path(raw_db, os.path.join(resolved_instance, 'hdc_erp.db'))

    return AppSettings(
        base_dir=BASE_DIR,
        instance_dir=resolved_instance,
        stage_drawings_dir=os.path.join(resolved_instance, 'stage_drawings'),
        db_path=resolved_db,
        estimation_store=os.path.join(resolved_instance, 'project_estimations.json'),
        backup_dir=os.path.join(resolved_instance, 'backups'),
    )


# Compatibility values for ``import hdc_erp`` and older scripts.  New app
# instances store their own settings in ``app.extensions`` below.
_DEFAULT_SETTINGS = load_settings()
INSTANCE_DIR = _DEFAULT_SETTINGS.instance_dir
STAGE_DRAWINGS_DIR = _DEFAULT_SETTINGS.stage_drawings_dir
DB_PATH = _DEFAULT_SETTINGS.db_path
DEV_MODE = (
    str(os.environ.get('HDC_DEV_MODE', '')).strip().lower() in ('1', 'true', 'yes')
    or str(os.environ.get('FLASK_ENV', '')).strip().lower() == 'development'
)
_ESTIMATION_STORE = _DEFAULT_SETTINGS.estimation_store
_DB_STORE = _DEFAULT_SETTINGS.db_path
_BACKUP_DIR = _DEFAULT_SETTINGS.backup_dir


def settings_for_app(config_overrides=None):
    """Build filesystem settings while honoring factory configuration overrides.

    ``HDC_DB_PATH``/``HDC_INSTANCE_DIR`` are the preferred explicit path
    overrides.  A local SQLite URI is also recognized because Flask test
    factories commonly pass ``SQLALCHEMY_DATABASE_URI`` directly.
    """
    overrides = config_overrides or {}
    instance_dir = overrides.get('HDC_INSTANCE_DIR')
    db_path = overrides.get('HDC_DB_PATH')
    if db_path is None:
        uri = str(overrides.get('SQLALCHEMY_DATABASE_URI') or '')
        prefix = 'sqlite:///'
        if uri.startswith(prefix):
            raw = uri[len(prefix):].split('?', 1)[0]
            if raw and raw != ':memory:':
                db_path = raw
    return load_settings(instance_dir=instance_dir, db_path=db_path)


def get_runtime_settings():
    """Return settings for the active Flask app, or compatibility defaults."""
    try:
        from flask import current_app
        return current_app.extensions.get('hdc_settings', _DEFAULT_SETTINGS)
    except (ImportError, RuntimeError):
        return _DEFAULT_SETTINGS


def ensure_dirs(settings=None):
    """Create runtime directories for one app's settings."""
    settings = settings or get_runtime_settings()
    os.makedirs(settings.instance_dir, exist_ok=True)
    os.makedirs(settings.stage_drawings_dir, exist_ok=True)
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    os.makedirs(settings.backup_dir, exist_ok=True)


def get_flask_config(settings=None):
    """Return secure Flask defaults derived from environment settings."""
    settings = settings or get_runtime_settings()
    secret = (os.environ.get('HDC_SECRET_KEY') or '').strip()
    environment = (os.environ.get('HDC_ENV') or 'dev').strip().lower()
    if not secret:
        if environment in ('prod', 'production'):
            raise RuntimeError(
                'HDC_SECRET_KEY must be set when HDC_ENV=prod/production.'
            )
        secret = 'hdc_local_username_password_session_key'
        print('[HDC ERP] WARNING: HDC_SECRET_KEY is not set; using the '
              'built-in dev key. Set HDC_SECRET_KEY in production.')

    secure_cookie = environment in ('prod', 'production')
    return {
        'SECRET_KEY': secret,
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{settings.db_path}',
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        # Explicit cookie policy; production is expected to run behind HTTPS.
        'SESSION_COOKIE_HTTPONLY': True,
        'SESSION_COOKIE_SAMESITE': 'Lax',
        'SESSION_COOKIE_SECURE': secure_cookie,
        'REMEMBER_COOKIE_HTTPONLY': True,
        'REMEMBER_COOKIE_SAMESITE': 'Lax',
        'REMEMBER_COOKIE_SECURE': secure_cookie,
        # Lightweight per-worker login throttling.  Put a reverse-proxy or
        # shared limiter in front of multi-worker deployments as well.
        'HDC_LOGIN_MAX_ATTEMPTS': int(os.environ.get('HDC_LOGIN_MAX_ATTEMPTS', '5') or 5),
        'HDC_LOGIN_WINDOW_SECONDS': int(os.environ.get('HDC_LOGIN_WINDOW_SECONDS', '300') or 300),
        'HDC_LOGIN_LOCKOUT_SECONDS': int(os.environ.get('HDC_LOGIN_LOCKOUT_SECONDS', '900') or 900),
    }
