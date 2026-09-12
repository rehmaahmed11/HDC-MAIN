"""HDC config — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import os

# â”€â”€ App Setup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
BASE_DIR     = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..'))


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


STAGE_DRAWINGS_DIR = os.path.join(INSTANCE_DIR, 'stage_drawings')


DB_PATH = _resolve_path(
    os.environ.get('HDC_DB_PATH'),
    (os.path.join(INSTANCE_DIR, 'hdc_erp_integrated.db')
     if os.path.exists(os.path.join(INSTANCE_DIR, 'hdc_erp_integrated.db'))
     else os.path.join(INSTANCE_DIR, 'hdc_erp.db'))
)


DEV_MODE = (str(os.environ.get('HDC_DEV_MODE', '')).strip().lower() in ('1', 'true', 'yes')
            or str(os.environ.get('FLASK_ENV', '')).strip().lower() == 'development')


_ESTIMATION_STORE = os.path.join(INSTANCE_DIR, 'project_estimations.json')


_DB_STORE = DB_PATH


_BACKUP_DIR = os.path.join(INSTANCE_DIR, 'backups')




def ensure_dirs():
    """Create runtime directories (called by the app factory, not at import)."""
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    os.makedirs(STAGE_DRAWINGS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(_BACKUP_DIR, exist_ok=True)


def get_flask_config():
    """Flask config dict derived from the environment (12-factor style)."""
    secret = (os.environ.get("HDC_SECRET_KEY") or "").strip()
    if not secret:
        secret = "hdc_local_username_password_session_key"
        print("[HDC ERP] WARNING: HDC_SECRET_KEY is not set; using the "
              "built-in dev key. Set HDC_SECRET_KEY in production.")
    return {
        "SECRET_KEY": secret,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{DB_PATH}",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
    }
