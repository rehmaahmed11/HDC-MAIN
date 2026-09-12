"""HDC ERP application factory.

Builds the Flask app from the modular package. Behaviour is identical to
the legacy single-file app: same URLs, same endpoint names, same database.
"""
import os

from flask import Flask

from hdc.config import BASE_DIR, ensure_dirs, get_flask_config
from hdc.extensions import (db, login_manager, _csrf_protect,
                            _ensure_db_runtime_ready, _inject_alert_count,
                            load_user)
from hdc.routes import register_all
from hdc.services.audit import register_audit_events
from hdc.core.bootstrap import _ensure_bootstrap_once
from hdc.utils.dates import _fmt_pkt


def create_app(config_overrides=None):
    """Create and fully initialise the HDC Flask application."""
    ensure_dirs()
    app = Flask(
        __name__,
        template_folder=os.path.join(BASE_DIR, "templates", "hdc"),
        static_folder=os.path.join(BASE_DIR, "static", "hdc"),
        static_url_path="/hdc_static",
    )
    app.config.update(get_flask_config())
    if config_overrides:
        app.config.update(config_overrides)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.user_loader(load_user)

    # Same hook order as the original module: runtime self-heal first,
    # then CSRF enforcement.
    app.context_processor(_inject_alert_count)
    app.before_request(_ensure_db_runtime_ready)
    app.before_request(_csrf_protect)

    # Template formatter, available unconditionally (as before).
    app.jinja_env.globals["fmt_pkt"] = _fmt_pkt
    app.jinja_env.filters["fmt_pkt"] = _fmt_pkt

    register_audit_events()
    register_all(app)
    _ensure_bootstrap_once(app)
    return app
