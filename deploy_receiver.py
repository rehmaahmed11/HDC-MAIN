#!/usr/bin/env python3
"""GitHub-webhook deploy receiver for HDC ERP on PythonAnywhere.

This is a *separate* WSGI application from the ERP.  It deliberately imports
nothing from ``hdc`` and nothing from PyPI -- only the standard library.  Two
reasons:

1. It must keep answering the phone when a pushed commit breaks the ERP.  If
   the receiver lived inside ``hdc.app.create_app()`` a bad deploy would take
   the fix-forward path down with it.
2. It must never touch the database.  It has no ORM, no models, no login and
   no session; it only verifies a webhook signature and launches
   ``ops/pythonanywhere/deploy.sh``, which updates tracked code and leaves the
   SQLite database and instance files alone.

Endpoints (mounted at ``/deploy/*`` by the PythonAnywhere WSGI dispatcher, or
at the root when the receiver gets its own web app):

    POST /deploy/github   GitHub "push" webhook, HMAC-SHA256 verified
    POST /deploy/trigger  signed manual deploy / rollback (bearer token)
    GET  /deploy/health   public liveness and last deploy state
    GET  /deploy/status   health plus the tail of the deploy log (bearer token)

Nothing here runs a destructive Git command; the guards live in
``ops/pythonanywhere/deploy.sh`` and ``scripts/check_db_safety.py``.

Configuration (environment, normally loaded from ``production.env`` by the
WSGI file):

    HDC_DEPLOY_WEBHOOK_SECRET   required; the secret you paste into GitHub
    HDC_APP_DIR                 required; the Git checkout to update
    HDC_DEPLOY_REPO             owner/name allowed to deploy (recommended)
    HDC_DEPLOY_BRANCH           branch to auto-deploy (default: main)
    HDC_DEPLOY_SCRIPT           deploy script
                                (default: <HDC_APP_DIR>/ops/pythonanywhere/deploy.sh)
    HDC_DEPLOY_STATE_DIR        log/state dir
                                (default: <HDC_INSTANCE_DIR>/deploy)
    HDC_DEPLOY_TOKEN            bearer token for /deploy/trigger and /deploy/status
    HDC_DEPLOY_ALLOW_MANUAL     1 to enable the manual trigger (default: 0)
    HDC_DEPLOY_DISABLED         1 to switch the receiver off (default: 0)
    HDC_DEPLOY_SYNC             1 to deploy inside the request; debug only
    HDC_DEPLOY_COOLDOWN_SECONDS ignore further pushes for this long (default: 45)
    HDC_DEPLOY_STALE_SECONDS  a "running" deploy older than this is reported
                              as stale instead (default: 900)
    HDC_DEPLOY_MAX_PAYLOAD      reject larger request bodies (default: 1048576)

Run ``python3 deploy_receiver.py`` on PythonAnywhere to print the resolved
configuration and the last deploy state; it is the quickest way to check that
the WSGI file is wired up correctly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass

ROUTES = ('github', 'trigger', 'health', 'status')
STATE_FILE_NAME = 'deploy_state.json'
LOG_FILE_NAME = 'deploy.log'
MAX_LOG_BYTES = 512 * 1024
LOG_TAIL_BYTES = 64 * 1024
SHA_RE = re.compile(r'^[0-9a-f]{7,40}$')
REF_SAFE_RE = re.compile(r'^[A-Za-z0-9._/\-]{1,200}$')


def _flag(name, default='0'):
    return str(os.environ.get(name, default)).strip().lower() in ('1', 'true', 'yes', 'on')


def _int(name, default):
    try:
        return int(str(os.environ.get(name, default)).strip() or default)
    except ValueError:
        return default


@dataclass
class Config:
    """Receiver settings, resolved from the environment on every request.

    Resolving per request (instead of at import) means a fix to
    ``production.env`` plus a reload is enough to change behaviour; the
    receiver never has to be redeployed to be reconfigured.
    """

    enabled: bool
    reason: str
    secret: str
    token: str
    app_dir: str
    script: str
    branch: str
    repo: str
    state_dir: str
    allow_manual: bool
    sync: bool
    cooldown: int
    max_payload: int
    stale_seconds: int
    env_file: str

    @classmethod
    def from_env(cls):
        app_dir = os.environ.get('HDC_APP_DIR', '').strip()
        state_dir = os.environ.get('HDC_DEPLOY_STATE_DIR', '').strip()
        if not state_dir:
            instance = os.environ.get('HDC_INSTANCE_DIR', '').strip() or (
                os.path.join(app_dir, 'hdc_instance') if app_dir else '')
            state_dir = os.path.join(instance, 'deploy') if instance else ''
        secret = os.environ.get('HDC_DEPLOY_WEBHOOK_SECRET', '').strip()
        enabled, reason = True, ''
        if _flag('HDC_DEPLOY_DISABLED'):
            enabled, reason = False, 'HDC_DEPLOY_DISABLED is set'
        elif not secret:
            enabled, reason = False, 'HDC_DEPLOY_WEBHOOK_SECRET is not set'
        elif not app_dir:
            enabled, reason = False, 'HDC_APP_DIR is not set'
        elif not os.path.isdir(os.path.join(app_dir, '.git')):
            enabled, reason = False, 'HDC_APP_DIR is not a Git checkout'
        elif not state_dir:
            enabled, reason = False, 'HDC_INSTANCE_DIR/HDC_DEPLOY_STATE_DIR is not set'
        return cls(
            enabled=enabled,
            reason=reason,
            secret=secret,
            token=os.environ.get('HDC_DEPLOY_TOKEN', '').strip(),
            app_dir=app_dir,
            script=os.environ.get('HDC_DEPLOY_SCRIPT', '').strip() or os.path.join(
                app_dir, 'ops', 'pythonanywhere', 'deploy.sh'),
            branch=os.environ.get('HDC_DEPLOY_BRANCH', '').strip() or 'main',
            repo=os.environ.get('HDC_DEPLOY_REPO', '').strip().lower(),
            state_dir=state_dir,
            allow_manual=_flag('HDC_DEPLOY_ALLOW_MANUAL'),
            sync=_flag('HDC_DEPLOY_SYNC'),
            cooldown=_int('HDC_DEPLOY_COOLDOWN_SECONDS', 45),
            max_payload=_int('HDC_DEPLOY_MAX_PAYLOAD', 1048576),
            stale_seconds=_int('HDC_DEPLOY_STALE_SECONDS', 900),
            env_file=os.environ.get('HDC_DEPLOY_ENV_FILE',
                                   os.path.expanduser('~/.config/hdc/production.env')),
        )


class Ctx:
    """One request: the WSGI environ plus its response writer."""

    def __init__(self, environ, start_response):
        self.environ = environ
        self.start_response = start_response

    def json(self, status, payload, extra_headers=()):
        body = json.dumps(payload, separators=(',', ':'), default=str).encode('utf-8')
        headers = [
            ('Content-Type', 'application/json; charset=utf-8'),
            ('Content-Length', str(len(body))),
            ('Cache-Control', 'no-store'),
            ('X-Content-Type-Options', 'nosniff'),
            ('Referrer-Policy', 'no-referrer'),
        ]
        headers.extend(extra_headers)
        self.start_response(status, headers)
        return [body]

    def error(self, status, message):
        return self.json(status, {'status': 'error',
                                  'code': int(status.split(' ', 1)[0]),
                                  'message': message})


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def read_body(ctx, limit):
    """Return ``(body, error)``; the body is capped so one request cannot
    exhaust the free tier's memory."""
    try:
        length = int(ctx.environ.get('CONTENT_LENGTH') or 0)
    except (TypeError, ValueError):
        return None, 'invalid Content-Length'
    if length <= 0:
        return None, 'empty body'
    if limit and length > limit:
        return None, f'payload larger than {limit} bytes'
    stream = ctx.environ.get('wsgi.input')
    if stream is None:
        return None, 'no input stream'
    try:
        body = stream.read(length)
    except Exception as exc:  # pragma: no cover - transport level failure
        return None, f'unreadable body: {exc.__class__.__name__}'
    if len(body) != length:
        return None, 'truncated body'
    return body, ''


def signature_ok(secret, body, header_value):
    """Validate GitHub's ``X-Hub-Signature-256`` in constant time."""
    if not header_value:
        return False
    provided = header_value.strip()
    if provided.lower().startswith('sha256='):
        provided = provided[len('sha256='):]
    expected = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip().lower())


def token_ok(expected, ctx):
    if not expected:
        return False
    supplied = (ctx.environ.get('HTTP_AUTHORIZATION') or '').strip()
    if supplied.lower().startswith('bearer '):
        supplied = supplied[len('bearer '):].strip()
    return bool(supplied) and hmac.compare_digest(expected, supplied)


def match_route(ctx):
    """Map a path to a route name, with or without the ``/deploy`` prefix.

    The free tier has a single web app, so the receiver is normally mounted
    beside the ERP by the WSGI dispatcher; a paid account can give it its own
    domain and no prefix.  Matching both keeps one code path.
    """
    raw = ((ctx.environ.get('SCRIPT_NAME') or '') + (ctx.environ.get('PATH_INFO') or ''))
    raw = raw.strip('/')
    parts = raw.split('/')
    if len(parts) >= 2 and parts[0] == 'deploy':
        candidate = parts[1]
    elif len(parts) == 1:
        candidate = parts[0]
    else:
        candidate = ''
    return candidate if candidate in ROUTES else None


def state_path(cfg):
    return os.path.join(cfg.state_dir, STATE_FILE_NAME)


def load_state(cfg):
    try:
        with open(state_path(cfg), 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(cfg, payload):
    """Atomic write, and best effort: a state file must never break a deploy."""
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        path = state_path(cfg)
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.write('\n')
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def log_tail(cfg, lines=120):
    try:
        with open(os.path.join(cfg.state_dir, LOG_FILE_NAME), 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
            text = handle.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return []
    return text[-lines:]


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def reap_finished_child(pid):
    """True when this process's own deploy child has already exited.

    The child is a direct child of the web worker and nobody calls wait() for
    it, so after it exits it stays a zombie and ``os.kill(pid, 0)`` still
    succeeds.  Reaping here is what stops /deploy/health from reporting a
    finished deploy as "running" forever.
    """
    try:
        waited, _status = os.waitpid(int(pid), os.WNOHANG)
    except (ChildProcessError, OSError, ValueError, TypeError):
        return False
    return waited != 0


def deploy_in_progress(cfg, state):
    """Is a deploy actually running?  Also flags a lost/stale child.

    Returns ``(running, status_override)`` where status_override is '' when the
    recorded state can be trusted.
    """
    if not state or state.get('status') != 'running':
        return False, ''
    pid = state.get('pid')
    if pid and reap_finished_child(pid):
        return False, 'ended'
    if not pid_alive(pid):
        return False, 'lost'
    started = float(state.get('started_epoch') or 0)
    if started and time.time() - started > cfg.stale_seconds:
        return False, 'stale'
    return True, ''


def rotate_log(cfg):
    try:
        path = os.path.join(cfg.state_dir, LOG_FILE_NAME)
        if os.path.getsize(path) > MAX_LOG_BYTES:
            os.replace(path, path + '.1')
    except OSError:
        pass


def deploy_environment(cfg, overrides):
    """Environment for deploy.sh: inherit the app's env, add deploy context."""
    env = dict(os.environ)
    env['HDC_DEPLOY_SOURCE'] = 'webhook'
    for key, value in overrides.items():
        if value:
            env[str(key)] = str(value)
    if cfg.app_dir:
        env['HDC_APP_DIR'] = cfg.app_dir
    if cfg.branch:
        env['HDC_DEPLOY_BRANCH'] = cfg.branch
    if cfg.state_dir:
        env['HDC_DEPLOY_STATE_FILE'] = state_path(cfg)
    if cfg.env_file and os.path.isfile(cfg.env_file):
        env.setdefault('HDC_DEPLOY_ENV_FILE', cfg.env_file)
    # Code-only pushes should not wait on PyPI: deploy.sh skips pip when
    # requirements.txt is byte-identical to the deployed version.
    env.setdefault('HDC_PIP_AUTOSKIP', '1')
    return env


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


# --------------------------------------------------------------------------
# deploy launch
# --------------------------------------------------------------------------

def launch(cfg, ctx, overrides, trigger, sha):
    """Record a "running" state, then start deploy.sh detached."""
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        rotate_log(cfg)
        log_path = os.path.join(cfg.state_dir, LOG_FILE_NAME)
        env = deploy_environment(cfg, overrides)
        revision = sha or env.get('HDC_DEPLOY_REVISION', '')
        state = {
            'status': 'running',
            'trigger': trigger,
            'sha': revision,
            'branch': cfg.branch,
            'started_at': stamp(),
            'started_epoch': time.time(),
            'log': log_path,
        }
        save_state(cfg, state)
        # The log header belongs to the receiver, not to the child, so a deploy
        # always has a start line even when the child cannot be launched.
        with open(log_path, 'ab', buffering=0) as handle:
            handle.write(f'\n===== HDC deploy started {stamp()} ({trigger}) '
                         f'rev={revision or "branch tip"} =====\n'.encode('utf-8'))
        if cfg.sync:
            started = time.time()
            with open(log_path, 'ab', buffering=0) as handle:
                code = subprocess.run(
                    ['bash', cfg.script], cwd=cfg.app_dir, env=env,
                    stdin=subprocess.DEVNULL, stdout=handle,
                    stderr=subprocess.STDOUT, check=False).returncode
            elapsed = round(time.time() - started, 1)
            save_state(cfg, dict(state, status='ok' if code == 0 else 'failed',
                                 exit_code=code, seconds=elapsed, finished_at=stamp()))
            if code != 0:
                return ctx.json('500 Internal Server Error',
                                {'status': 'failed', 'exit_code': code,
                                 'log_tail': log_tail(cfg, 40)})
            return ctx.json('200 OK', {'status': 'deployed', 'seconds': elapsed})
        pid = spawn_detached(cfg.script, cfg.app_dir, env, log_path)
        save_state(cfg, dict(state, pid=pid))
        return ctx.json('202 Accepted', {'status': 'accepted', 'trigger': trigger,
                                         'sha': revision, 'pid': pid})
    except OSError as exc:
        return ctx.error('500 Internal Server Error', f'could not start deploy: {exc}')


def spawn_detached(script, cwd, env, log_path):
    """Start the deploy so it outlives this request.

    ``start_new_session`` puts the child in its own process group, so neither
    the uWSGI worker that served the webhook nor the reload triggered at the
    end of a successful deploy takes the running deploy down with it.
    """
    with open(log_path, 'ab', buffering=0) as handle:
        proc = subprocess.Popen(
            ['bash', script],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return proc.pid


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------

def push_is_for_us(cfg, payload):
    """Gate a push payload on branch, repository and payload shape."""
    ref = payload.get('ref')
    if ref != f'refs/heads/{cfg.branch}':
        return False, '', f'ignored ref {ref!r} (deploys {cfg.branch} only)'
    if payload.get('deleted') is True:
        return False, '', 'branch deletion'
    repo = (payload.get('repository') or {}).get('full_name', '')
    if cfg.repo and str(repo).lower() != cfg.repo:
        return False, '', f'repository {repo!r} is not {cfg.repo!r}'
    sha = str(payload.get('after') or '').strip().lower()
    if not SHA_RE.match(sha):
        return False, '', 'no commit id in payload'
    if set(sha) == {'0'}:
        return False, '', 'null commit id'
    return True, sha, ''


def handle_webhook(cfg, ctx):
    body, error = read_body(ctx, cfg.max_payload)
    if body is None:
        return ctx.error('400 Bad Request', error)
    if not signature_ok(cfg.secret, body, ctx.environ.get('HTTP_X_HUB_SIGNATURE_256', '')):
        # Terse on purpose: never echo the payload or the expected digest.
        return ctx.error('401 Unauthorized', 'invalid or missing webhook signature')
    try:
        payload = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return ctx.error('400 Bad Request', 'body is not valid JSON')
    event = (ctx.environ.get('HTTP_X_GITHUB_EVENT') or '').strip().lower()
    if event and event != 'push':
        # "ping" and friends must still return 2xx so GitHub keeps the hook
        # marked as healthy.
        return ctx.json('200 OK', {'status': 'ignored', 'reason': f'{event} event'})
    if not isinstance(payload, dict):
        return ctx.error('400 Bad Request', 'payload is not an object')
    accepted, sha, reason = push_is_for_us(cfg, payload)
    if not accepted:
        return ctx.json('200 OK', {'status': 'ignored', 'reason': reason})

    state = load_state(cfg)
    running, _override = deploy_in_progress(cfg, state)
    if running:
        return ctx.json('202 Accepted', {'status': 'accepted',
                                         'reason': 'a deploy is already running',
                                         'sha': sha})
    last_start = float(state.get('started_epoch') or 0)
    since_last = time.time() - last_start if last_start else None
    if (cfg.cooldown > 0 and since_last is not None
            and since_last < cfg.cooldown and state.get('status') != 'failed'):
        return ctx.json('200 OK', {'status': 'ignored',
                                   'reason': f'deployed {round(since_last, 1)}s ago, '
                                             f'cooldown is {cfg.cooldown}s'})
    return launch(cfg, ctx, {'HDC_DEPLOY_SHA': sha}, trigger='push', sha=sha)


def handle_trigger(cfg, ctx):
    """Manual deploy or rollback.  Off unless HDC_DEPLOY_ALLOW_MANUAL=1."""
    if not cfg.allow_manual:
        return ctx.error('403 Forbidden',
                         'manual trigger disabled (set HDC_DEPLOY_ALLOW_MANUAL=1)')
    if not token_ok(cfg.token, ctx):
        return ctx.error('401 Unauthorized', 'missing or wrong bearer token')
    body, error = read_body(ctx, cfg.max_payload)
    if body is None and error not in ('empty body',):
        return ctx.error('400 Bad Request', error)
    payload = {}
    if body:
        try:
            parsed = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return ctx.error('400 Bad Request', 'body is not valid JSON')
        if isinstance(parsed, dict):
            payload = parsed
    overrides = {}
    revision = str(payload.get('revision') or ctx.environ.get('HTTP_X_HDC_REVISION') or '').strip()
    ref = str(payload.get('ref') or ctx.environ.get('HTTP_X_HDC_REF') or '').strip()
    if revision:
        if not SHA_RE.match(revision.lower()):
            return ctx.error('400 Bad Request', 'revision must be a 7-40 hex character commit id')
        overrides['HDC_DEPLOY_REVISION'] = revision.lower()
    if ref:
        # Allowed straight through to git, so keep it to a conservative charset
        # and never let it start with '-' (that would read as an option).
        if not REF_SAFE_RE.match(ref) or ref.startswith('-') or '..' in ref:
            return ctx.error('400 Bad Request', 'ref contains unsupported characters')
        overrides['HDC_DEPLOY_REF'] = ref
    if str(payload.get('force') or '').lower() in ('1', 'true', 'yes'):
        overrides['HDC_DEPLOY_FORCE'] = '1'
    return launch(cfg, ctx, overrides, trigger='manual', sha=revision)


def handle_health(cfg, ctx, include_log):
    if include_log and not token_ok(cfg.token, ctx):
        return ctx.error('401 Unauthorized', 'missing or wrong bearer token')
    state = load_state(cfg)
    running, override = deploy_in_progress(cfg, state)
    if override:
        state = dict(state)
        state['status'] = override
        state['note'] = (
            'the deploy process ended without writing a result; read '
            f'{os.path.join(cfg.state_dir, LOG_FILE_NAME)}, then re-run '
            'deploy.sh from a PythonAnywhere Bash console')
    response = {
        'status': 'ok',
        'deploy_enabled': True,
        'branch': cfg.branch,
        'repo': cfg.repo or '(not restricted)',
        'script': os.path.basename(cfg.script),
        'manual_allowed': cfg.allow_manual,
        'deploy_running': running,
        'deploy': state,
    }
    if include_log:
        response['log_tail'] = log_tail(cfg)
    return ctx.json('200 OK', response)


def application(environ, start_response):
    """WSGI entrypoint referenced from the PythonAnywhere WSGI file."""
    ctx = Ctx(environ, start_response)
    cfg = Config.from_env()
    route = match_route(ctx)
    if route is None:
        return ctx.error('404 Not Found', 'unknown deploy route')
    if not cfg.enabled:
        # Health stays reachable so you can see *why* deploys do nothing.
        if route in ('health', 'status'):
            return ctx.json('503 Service Unavailable',
                            {'status': 'disabled', 'reason': cfg.reason,
                             'deploy_enabled': False})
        return ctx.json('503 Service Unavailable',
                        {'status': 'disabled', 'reason': cfg.reason})
    method = (environ.get('REQUEST_METHOD') or 'GET').upper()
    try:
        if route == 'github':
            if method != 'POST':
                return ctx.error('405 Method Not Allowed', 'use POST')
            return handle_webhook(cfg, ctx)
        if route == 'trigger':
            if method != 'POST':
                return ctx.error('405 Method Not Allowed', 'use POST')
            return handle_trigger(cfg, ctx)
        if route == 'health':
            if method != 'GET':
                return ctx.error('405 Method Not Allowed', 'use GET')
            return handle_health(cfg, ctx, include_log=False)
        if method != 'GET':
            return ctx.error('405 Method Not Allowed', 'use GET')
        return handle_health(cfg, ctx, include_log=True)
    except Exception:  # pragma: no cover - defensive
        errors = environ.get('wsgi.errors')
        if errors is not None:
            try:
                traceback.print_exc(file=errors)
            except Exception:
                pass
        # The payload is never reflected back; details go to the error log.
        return ctx.error('500 Internal Server Error', 'internal deploy receiver error')


def selfcheck(stream=None):
    """Print the resolved configuration; used by the CLI entrypoint below."""
    stream = stream or sys.stdout
    cfg = Config.from_env()
    print(f'enabled:      {cfg.enabled}' + (f'  ({cfg.reason})' if cfg.reason else ''), file=stream)
    print(f'app dir:      {cfg.app_dir or "(unset)"}', file=stream)
    print(f'script:       {cfg.script}', file=stream)
    print(f'branch:       {cfg.branch}', file=stream)
    print(f'repo:         {cfg.repo or "(not restricted)"}', file=stream)
    print(f'state dir:    {cfg.state_dir or "(unset)"}', file=stream)
    print(f'env file:     {cfg.env_file} (exists: '
          f'{os.path.isfile(cfg.env_file) if cfg.env_file else False})', file=stream)
    print(f'secret set:   {bool(cfg.secret)}', file=stream)
    print(f'manual:       {cfg.allow_manual} (token set: {bool(cfg.token)})', file=stream)
    state = load_state(cfg) if cfg.state_dir else {}
    print(f'last deploy:  {json.dumps(state, sort_keys=True, default=str) if state else "(none yet)"}',
          file=stream)
    return 0 if cfg.enabled else 1


if __name__ == '__main__':
    sys.exit(selfcheck())
