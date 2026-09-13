"""HDC ERP application factory.

Builds the Flask app from the modular package. Behaviour is identical to
the legacy single-file app: same URLs, same endpoint names, same database.
"""
import os

from flask import Flask

from hdc.config import BASE_DIR, ensure_dirs, get_flask_config, settings_for_app
from hdc.extensions import (db, login_manager, _csrf_protect,
                            _ensure_db_runtime_ready, _inject_alert_count,
                            load_user)
from hdc.routes import register_all
from hdc.services.audit import register_audit_events
from hdc.core.bootstrap import _ensure_bootstrap_once
from hdc.utils.dates import _fmt_pkt


def create_app(config_overrides=None):
    """Create and fully initialise one isolated HDC Flask application."""
    settings = settings_for_app(config_overrides)
    ensure_dirs(settings)
    app = Flask(
        __name__,
        template_folder=os.path.join(BASE_DIR, "templates", "hdc"),
        static_folder=os.path.join(BASE_DIR, "static", "hdc"),
        static_url_path="/hdc_static",
    )
    app.config.update(get_flask_config(settings))
    if config_overrides:
        app.config.update(config_overrides)
    # Path-bearing services read this per-app value instead of global module
    # constants.  This is what makes two scratch apps safe in one process.
    app.extensions['hdc_settings'] = settings

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.user_loader(load_user)

    # Same hook order as the original module: runtime self-heal first,
    # then CSRF enforcement.
    app.context_processor(_inject_alert_count)
    app.before_request(_ensure_db_runtime_ready)
    app.before_request(_csrf_protect)

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault(
            'Permissions-Policy',
            'camera=(), microphone=(), geolocation=()'
        )
        if app.config.get('SESSION_COOKIE_SECURE'):
            response.headers.setdefault(
                'Strict-Transport-Security',
                'max-age=31536000; includeSubDomains'
            )
        return response

    # Template formatter, available unconditionally (as before).
    app.jinja_env.globals["fmt_pkt"] = _fmt_pkt
    app.jinja_env.filters["fmt_pkt"] = _fmt_pkt

    # Row traceability: every list row carries the id of the user who entered
    # it (``{{ row|hdc_row_attrs }}``) so the audit column on the page can be
    # filled in, and templates can look the actors up directly with
    # ``actor_map(rows)``.  See hdc/services/actors.py.
    from hdc.services.actors import actor_map, actor_for, row_attrs_html
    app.jinja_env.globals["actor_map"] = actor_map
    app.jinja_env.globals["actor_for"] = actor_for
    app.jinja_env.filters["hdc_row_attrs"] = row_attrs_html

    register_audit_events()
    register_all(app)
    _ensure_bootstrap_once(app)
    return app
