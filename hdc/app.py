"""HDC ERP application factory.

Builds the Flask app from the modular package. Behaviour is identical to
the legacy single-file app: same URLs, same endpoint names, same database.
"""
import os
import re

from flask import Flask, session, url_for
from werkzeug.routing import BuildError

from hdc.config import BASE_DIR, ensure_dirs, get_flask_config, settings_for_app
from hdc.extensions import (db, login_manager, _csrf_protect,
                            _ensure_db_runtime_ready, _inject_alert_count,
                            load_user)
from hdc.routes import register_all
from hdc.services.audit import register_audit_events
from hdc.core.bootstrap import _ensure_bootstrap_once
from hdc.utils.dates import _fmt_pkt


def _safe_url_for(endpoint, **values):
    """``url_for`` that degrades to ``'#'`` instead of raising BuildError.

    Data-driven pages (e.g. the Money Center flow inventory) render route
    names stored in Python data; a typo there must never take the whole
    page down with a 500.
    """
    try:
        return url_for(endpoint, **values)
    except BuildError:
        return "#"


# Server-side CSRF injection (audit Step 8).  The base template used to add
# the hidden token with JavaScript only, so with JS disabled every form POST
# came back 400.  These two patterns let the server do it for every POST form
# instead, which also covers any template added later.
_CSRF_FORM_RE = re.compile(
    r'<form\b(?=[^>]*\bmethod\s*=\s*["\']?\s*post\b)[^>]*>'
    # Skip forms that already carry the token (several templates add it by
    # hand); their hidden input sits right after the opening tag.
    r'(?!\s*<input\b[^>]*\bname\s*=\s*["\']?_csrf_token)',
    re.IGNORECASE,
)


def _csrf_hidden_input(token):
    return '<input type="hidden" name="_csrf_token" value="%s">' % token


def _inject_csrf_into_forms(response):
    """Add a hidden ``_csrf_token`` input to every POST form in HTML output.

    Mirrors what ``static/hdc/js/core/../base.html`` does in the browser, so a
    form submits correctly whether or not JavaScript ever runs.  The client
    side hook stays in place as a second layer.
    """
    if response.mimetype != 'text/html' or response.is_streamed:
        return response
    token = (session.get('_csrf_token') or '').strip()
    if not token:
        return response
    body = response.get_data(as_text=True)
    if '<form' not in body:
        return response
    new_body, added = _CSRF_FORM_RE.subn(
        lambda m: m.group(0) + _csrf_hidden_input(token), body)
    if added:
        response.set_data(new_body)
    return response


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

    # A trailing slash should never 404: ``/hdc/workers/`` is matched by the
    # ``/hdc/workers`` rule (audit 7.4).  Rules that *declare* a trailing
    # slash keep their own behaviour.
    app.url_map.strict_slashes = False

    # Same hook order as the original module: runtime self-heal first,
    # then CSRF enforcement.
    app.context_processor(_inject_alert_count)
    app.before_request(_ensure_db_runtime_ready)
    app.before_request(_csrf_protect)

    # …and the matching write side: every POST form gets the token inline so
    # forms keep working with JavaScript disabled.
    app.after_request(_inject_csrf_into_forms)

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
    # Defensive URL builder for data-driven links (Money Center flow cards).
    app.jinja_env.globals["safe_url_for"] = _safe_url_for

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
