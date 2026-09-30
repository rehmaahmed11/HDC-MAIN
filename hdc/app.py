"""HDC ERP application factory.

Builds the Flask app from the modular package. Behaviour is identical to
the legacy single-file app: same URLs, same endpoint names, same database.
"""
import os
import re

from flask import Flask, render_template, request, session, url_for
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

    from hdc.services.permissions import (
        install_permission_query_scope, install_permission_template_helpers,
        may_access_path, stage_is_allowed, user_stage_scope,
    )
    from hdc.services.record_permissions import (
        enforce_record_request, install_record_permission_scope,
        install_record_template_helpers,
    )
    install_permission_template_helpers(app)
    install_permission_query_scope()
    install_record_template_helpers(app)
    install_record_permission_scope()

    @app.before_request
    def _enforce_user_permissions():
        from flask import abort, jsonify
        from flask_login import current_user
        if not current_user.is_authenticated:
            return None
        if request.path.rstrip('/') in ('/', '/hdc/login', '/hdc/logout', '/hdc/access') or request.path.startswith(('/hdc_static/', '/static/')):
            return None
        mode = 'read' if request.method in ('GET', 'HEAD', 'OPTIONS') else 'write'
        permission = may_access_path(current_user, request.path, mode)
        if permission is False:
            if request.is_json or request.path.startswith('/api/') or '/api/' in request.path:
                return jsonify(ok=False, message='You do not have permission to access this page.'), 403
            abort(403, description='You do not have permission to access this page.')

        enforce_record_request(current_user)

        # Enforce direct stage URLs and submitted stage references as well as
        # filtering all ORM result sets (so forms/APIs cannot bypass the scope).
        scope = user_stage_scope(current_user, mode)
        if scope is not None:
            # A newly-created stage cannot be included in the administrator's
            # existing allow-list safely; stage-limited users may edit only
            # stages that were explicitly assigned to them.
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and re.match(
                    r'^/hdc/projects/(?:add|\d+/(?:stage/add|bulk_stages))$', request.path):
                abort(403, description='Stage-limited users cannot create or bulk-replace project stages.')
            stage_match = re.match(r'^/hdc/stage/(\d+)(?:/|$)', request.path)
            requested_stage = (stage_match.group(1) if stage_match else None)
            if requested_stage is None:
                requested_stage = request.values.get('stage_id', type=int)
            if requested_stage is None and request.is_json:
                payload = request.get_json(silent=True) or {}
                if isinstance(payload, dict):
                    requested_stage = payload.get('stage_id')
            if requested_stage not in (None, '') and not stage_is_allowed(requested_stage, mode):
                if request.is_json or '/api/' in request.path:
                    return jsonify(ok=False, message='This stage is outside your assigned access.'), 403
                abort(403, description='This stage is outside your assigned access.')
        return None

    @app.errorhandler(403)
    def _permission_denied(error):
        # A denial may be raised at flush after a handler staged several writes.
        # Roll back before rendering (context processors also issue queries).
        db.session.rollback()
        if request.is_json or request.path.startswith('/api/') or '/api/' in request.path:
            return {'ok': False, 'message': 'You do not have permission to access this page.'}, 403
        return render_template('shared/forbidden.html'), 403

    # …and the matching write side: every POST form gets the token inline so
    # forms keep working with JavaScript disabled.
    app.after_request(_inject_csrf_into_forms)

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        # Clickjacking: the app refuses to be framed by default.  A deployment
        # that has to embed it somewhere (an intranet portal, a hosted preview)
        # opts in with HDC_FRAME_ANCESTORS — an explicit CSP source list — and
        # then the modern header is used instead of SAMEORIGIN.
        frame_ancestors = (app.config.get('HDC_FRAME_ANCESTORS') or '').strip()
        if frame_ancestors:
            response.headers.setdefault(
                'Content-Security-Policy', f'frame-ancestors {frame_ancestors}')
        else:
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

    # Tools inventory: ``{{ stock_stats|stock_lookup(tool_id) }}`` returns the
    # purchased / scrapped totals for one tool without a query per row.
    from hdc.services.tool_rental import stock_stats_for
    app.jinja_env.filters["stock_lookup"] = stock_stats_for

    register_audit_events()
    register_all(app)
    _ensure_bootstrap_once(app)
    return app
