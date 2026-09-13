#!/usr/bin/env python3
"""Tests for the minimal auto-deploy webhook (deploy_hook.py).

The hook is stdlib-only: GitHub pushes to main -> ``git pull`` -> touch the
WSGI file to reload. These tests pin the gating (bad signature rejected,
non-main refs ignored) and the happy path (pull + reload + log line) without
touching the network or a real checkout.

Run:  python3 tests/test_deploy_hook.py
"""

import hashlib
import hmac
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import deploy_hook  # noqa: E402

SECRET = 'test-hook-secret'


def sign(body, secret=SECRET):
    return 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_environ(method='GET', body=b'', signature=None, path='/deploy'):
    env = {
        'REQUEST_METHOD': method,
        'PATH_INFO': path,
        'CONTENT_LENGTH': str(len(body)),
        'wsgi.input': io.BytesIO(body),
    }
    if signature is not None:
        env['HTTP_X_HUB_SIGNATURE_256'] = signature
    return env


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        secret_file = os.path.join(self.tmp.name, 'deploy_secret.txt')
        with open(secret_file, 'w') as handle:
            handle.write(SECRET + '\n')
        self.patchers = [
            mock.patch.object(deploy_hook, 'REPO_DIR', self.tmp.name),
            mock.patch.object(deploy_hook, 'WSGI_FILE',
                               os.path.join(self.tmp.name, 'app_wsgi.py')),
            mock.patch.object(deploy_hook, 'SECRET_FILE', secret_file),
            mock.patch.object(deploy_hook, 'LOG_FILE',
                               os.path.join(self.tmp.name, 'deploy.log')),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.status = None

    def call(self, environ):
        def start_response(status, _headers):
            self.status = status

        return b''.join(deploy_hook.application(environ, start_response))

    def test_get_before_any_deploy(self):
        body = self.call(make_environ('GET'))
        self.assertEqual(self.status, '200 OK')
        self.assertIn(b'no deploys yet', body)

    def test_get_shows_last_lines_of_log(self):
        with open(deploy_hook.LOG_FILE, 'w') as handle:
            for i in range(10):
                handle.write(f'line {i}\n')
        body = self.call(make_environ('GET'))
        self.assertEqual(self.status, '200 OK')
        self.assertNotIn(b'line 0', body)
        self.assertIn(b'line 9', body)

    def test_post_with_bad_signature_is_rejected(self):
        payload = json.dumps({'ref': 'refs/heads/main', 'after': 'a' * 40}).encode()
        body = self.call(make_environ('POST', payload, signature='sha256=wrong'))
        self.assertEqual(self.status, '401 Unauthorized')
        self.assertEqual(body, b'bad signature')
        with open(deploy_hook.LOG_FILE) as handle:
            self.assertIn('REJECTED', handle.read())

    def test_post_without_signature_is_rejected(self):
        payload = json.dumps({'ref': 'refs/heads/main', 'after': 'a' * 40}).encode()
        body = self.call(make_environ('POST', payload))
        self.assertEqual(self.status, '401 Unauthorized')
        self.assertEqual(body, b'bad signature')

    def test_post_to_other_branch_is_ignored(self):
        payload = json.dumps({'ref': 'refs/heads/feature', 'after': 'b' * 40}).encode()
        body = self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '200 OK')
        self.assertIn(b'ignored', body)

    def test_post_to_main_pulls_and_reloads(self):
        payload = json.dumps({'ref': 'refs/heads/main', 'after': 'c' * 40}).encode()
        with mock.patch.object(deploy_hook.subprocess, 'check_output') as check_output, \
                mock.patch.object(deploy_hook.os, 'utime') as utime:
            check_output.side_effect = [b'Updating abc..def\nFast-forward\n', b'abc1234\n']
            body = self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '200 OK')
        self.assertEqual(body, b'deployed abc1234')
        pull_call = check_output.call_args_list[0]
        self.assertEqual(pull_call.args[0][:3], ['git', 'pull', 'origin'])
        self.assertEqual(pull_call.kwargs.get('cwd'), self.tmp.name)
        utime.assert_called_once_with(deploy_hook.WSGI_FILE, None)
        with open(deploy_hook.LOG_FILE) as handle:
            self.assertIn('OK deployed abc1234', handle.read())

    def test_failed_pull_returns_500_and_logs(self):
        payload = json.dumps({'ref': 'refs/heads/main', 'after': 'd' * 40}).encode()
        with mock.patch.object(deploy_hook.subprocess, 'check_output') as check_output:
            check_output.side_effect = subprocess.CalledProcessError(
                1, ['git', 'pull'], output=b'CONFLICT boom')
            body = self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '500 Internal Server Error')
        with open(deploy_hook.LOG_FILE) as handle:
            self.assertIn('FAILED', handle.read())


if __name__ == '__main__':
    unittest.main()
