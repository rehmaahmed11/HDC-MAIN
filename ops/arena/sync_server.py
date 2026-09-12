#!/usr/bin/env python3
"""Stdlib-only Git-over-HTTP sync server, for pairing a local checkout with
this sandbox without going through GitHub.

The sandbox cannot dial outward to your machine (egress is allowlisted to
github.com and pypi.org), but you CAN reach this sandbox over its preview
HTTPS URL.  So the connection is made in the direction that works: your
machine pushes here, and pulls back from here.

    your laptop  --git push-->  preview HTTPS  -->  this server  -->  bare repo
                                                                      |
    me editing the working clone <-------------------------------------+

Usage
-----
    # generate a token, seed the bare repo from the current checkout, serve
    TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
    SYNC_TOKEN="$TOKEN" SYNC_REPO=~/arena-sync/hdc.git \
    SYNC_SEED_FROM=/home/user/HDC-MAIN \
      python3 ops/arena/sync_server.py

Then on YOUR machine:

    git remote add arena "https://arena:$TOKEN@<port>-<sandbox>.e2b.app/hdc.git"
    git push arena HEAD:refs/heads/main      # send me your working tree
    git pull arena main                      # take my edits back

Auth is HTTP Basic with the fixed username ``arena`` and the token as the
password.  The server only ever serves the one repo named ``hdc.git``; it
will not walk the filesystem.
"""

import base64
import hmac
import os
import subprocess
import sys
from wsgiref.simple_server import make_server

TOKEN = os.environ.get('SYNC_TOKEN', '')
USER = os.environ.get('SYNC_USER', 'arena')
REPO_NAME = os.environ.get('SYNC_REPO_NAME', 'hdc.git')
REPO_PATH = os.path.abspath(os.environ.get('SYNC_REPO', os.path.expanduser('~/arena-sync/hdc.git')))
SEED_FROM = os.environ.get('SYNC_SEED_FROM', '')
PORT = int(os.environ.get('SYNC_PORT', os.environ.get('PORT', '8000')))
GIT_HTTP_BACKEND = os.path.join(
    subprocess.run(['git', '--exec-path'], capture_output=True, text=True, check=True)
    .stdout.strip(), 'git-http-backend')


def seed_repo():
    """Create the bare repo, optionally seeding it from an existing checkout."""
    if os.path.exists(os.path.join(REPO_PATH, 'HEAD')):
        print(f'[sync] bare repo already exists at {REPO_PATH}')
    else:
        os.makedirs(REPO_PATH, exist_ok=True)
        subprocess.run(['git', 'init', '--bare', '--initial-branch=main', REPO_PATH],
                       check=True, capture_output=True)
        print(f'[sync] created bare repo at {REPO_PATH}')
    # Allow pushes over HTTP and keep a worktree checkout in sync afterwards.
    subprocess.run(['git', 'config', 'http.receivepack', 'true'], cwd=REPO_PATH, check=True)
    subprocess.run(['git', 'config', 'receive.denyCurrentBranch', 'ignore'], cwd=REPO_PATH, check=True)

    if SEED_FROM and os.path.isdir(os.path.join(SEED_FROM, '.git')):
        # Push whatever branch the seed checkout is on into the bare repo so
        # the user starts from real code, not an empty repository.
        shallow = subprocess.run(['git', 'rev-parse', '--is-shallow-repository'],
                                 cwd=SEED_FROM, capture_output=True, text=True).stdout.strip()
        if shallow == 'true':
            print('[sync] WARNING: seed checkout is a shallow clone; pushes from it '
                  'are rejected by git. Run `git fetch --unshallow` in it first.')
        result = subprocess.run(
            ['git', 'push', '--force', REPO_PATH, 'refs/heads/*:refs/heads/*'],
            cwd=SEED_FROM, capture_output=True, text=True)
        if result.returncode == 0:
            print(f'[sync] seeded from {SEED_FROM}')
        else:
            print(f'[sync] WARNING: seeding from {SEED_FROM} failed:')
            print((result.stderr or result.stdout).strip())


def _unauthorized(start_response):
    start_response('401 Unauthorized', [
        ('Content-Type', 'text/plain'),
        ('WWW-Authenticate', 'Basic realm="hdc-sync"'),
        ('Content-Length', '12'),
    ])
    return [b'Unauthorized']


def _check_auth(environ):
    header = environ.get('HTTP_AUTHORIZATION', '')
    if not header.lower().startswith('basic '):
        return False
    try:
        decoded = base64.b64decode(header.split(None, 1)[1]).decode('utf-8')
    except Exception:
        return False
    if ':' not in decoded:
        return False
    user, _, password = decoded.partition(':')
    # Constant-time compare on both halves; never short-circuit on the user.
    ok_user = hmac.compare_digest(user, USER)
    ok_pass = hmac.compare_digest(password, TOKEN)
    return ok_user and ok_pass


def application(environ, start_response):
    if not TOKEN:
        start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
        return [b'SYNC_TOKEN is not set; refusing to serve an unauthenticated git repo.']

    path = environ.get('PATH_INFO', '')
    if path.rstrip('/') == '':
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [f'hdc sync server ok\nrepo: /{REPO_NAME}\n'.encode()]

    # Only ever serve the single configured repo name.  Reject NUL bytes and
    # traversal before anything reaches git-http-backend: a NUL in PATH_INFO
    # would otherwise blow up inside subprocess with an unhandled ValueError.
    expected = f'/{REPO_NAME}'
    if ('\x00' in path) or ('..' in path) or (not path.startswith(expected)):
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'no such repo']

    if not _check_auth(environ):
        return _unauthorized(start_response)

    cgi_env = {
        'REQUEST_METHOD': environ.get('REQUEST_METHOD', 'GET'),
        'QUERY_STRING': environ.get('QUERY_STRING', ''),
        'CONTENT_TYPE': environ.get('CONTENT_TYPE', ''),
        'CONTENT_LENGTH': environ.get('CONTENT_LENGTH', ''),
        # git-http-backend resolves the repository from PATH_INFO joined onto
        # GIT_PROJECT_ROOT, so it must include the repo name, e.g.
        # /hdc.git/info/refs -- not just /info/refs.
        'PATH_INFO': path,
        'GIT_PROJECT_ROOT': os.path.dirname(REPO_PATH),
        'GIT_HTTP_EXPORT_ALL': '1',
        'GIT_COMMITTER_NAME': 'arena-sync',
        'GIT_COMMITTER_EMAIL': 'arena-sync@localhost',
    }

    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
    except ValueError:
        length = 0
    body = environ['wsgi.input'].read(length) if length else b''

    proc = subprocess.run([GIT_HTTP_BACKEND], input=body, env=cgi_env, capture_output=True)
    raw = proc.stdout

    # git-http-backend emits a CGI header block, then a blank line, then body.
    header_blob, _, payload = raw.partition(b'\r\n\r\n')
    if not _:
        header_blob, _, payload = raw.partition(b'\n\n')
    headers = []
    status = '200 OK'
    for line in header_blob.split(b'\n'):
        line = line.strip()
        if not line:
            continue
        text = line.decode('latin-1')
        if text.lower().startswith('status:'):
            line = text.split(':', 1)[1].strip()
            parts = line.split(None, 1)
            if len(parts) == 2:            # git sends "404 not found"
                status = line
            elif parts and parts[0].isdigit() and len(parts[0]) == 3:
                reason = {'200': 'OK', '401': 'Unauthorized', '403': 'Forbidden',
                          '404': 'Not Found',
                          '500': 'Internal Server Error'}.get(parts[0], 'OK')
                status = f'{parts[0]} {reason}'
            else:
                status = '500 Internal Server Error'
            continue
        if ':' in text:
            k, v = text.split(':', 1)
            # Content-Length is recomputed from the payload below, and
            # Transfer-Encoding is unusable for a WSGI app that returns one
            # buffer, so neither is passed through.
            if k.lower() not in ('transfer-encoding', 'content-length'):
                headers.append((k.strip(), v.strip()))
    headers.append(('Content-Length', str(len(payload))))
    start_response(status, headers)
    return [payload]


def main():
    if not TOKEN:
        print('error: set SYNC_TOKEN (e.g. SYNC_TOKEN=$(python3 -c '
              '"import secrets;print(secrets.token_urlsafe(24))"))', file=sys.stderr)
        return 2
    seed_repo()
    print(f'[sync] serving {REPO_NAME} on 0.0.0.0:{PORT}  (user={USER})')
    print(f'[sync] clone/push URL path: /{REPO_NAME}')
    server = make_server('0.0.0.0', PORT, application)
    # git's smart HTTP client wants keep-alive; Content-Length is always set.
    server.protocol_version = 'HTTP/1.1'
    server.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
