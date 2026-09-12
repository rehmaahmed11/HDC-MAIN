#!/usr/bin/env python3
"""Tests for the GitHub-webhook deploy receiver (standard library only).

Run:  python3 tests/test_deploy_receiver.py
"""

import hashlib
import hmac
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import deploy_receiver  # noqa: E402

SECRET = 'test-webhook-secret'


def sign(body, secret=SECRET):
    return 'sha256=' + hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()


def call(method, path, body=b'', headers=None, app=None):
    environ = {
        'REQUEST_METHOD': method,
        'PATH_INFO': path,
        'SCRIPT_NAME': '',
        'SERVER_NAME': 'hdc.test',
        'SERVER_PORT': '443',
        'SERVER_PROTOCOL': 'HTTP/1.1',
        'wsgi.url_scheme': 'https',
        'wsgi.input': io.BytesIO(body),
        'wsgi.errors': io.StringIO(),
        'CONTENT_LENGTH': str(len(body)),
        'CONTENT_TYPE': 'application/json',
    }
    for key, value in (headers or {}).items():
        environ[key] = value
    captured = {}

    def start_response(status, response_headers):
        captured['status'] = status
        captured['headers'] = dict(response_headers)

    chunks = (app or deploy_receiver.application)(environ, start_response)
    data = b''.join(chunks)
    payload = json.loads(data.decode('utf-8')) if data[:1] in (b'{', b'[') else data
    return captured['status'], payload, captured['headers']


def push_payload(sha='abc123abc123abc123abc123abc123abc123ab12', branch='main',
                 repo='rehmaahmed11/HDC-MAIN', **extra):
    payload = {
        'ref': f'refs/heads/{branch}',
        'after': sha,
        'before': '0' * 40,
        'repository': {'full_name': repo},
        'created': False,
        'deleted': False,
    }
    payload.update(extra)
    return json.dumps(payload).encode('utf-8')


class ReceiverTestCase(unittest.TestCase):
    """Each test gets a scratch state dir and a fake deploy script."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.app_dir = os.path.join(self.tmp, 'HDC-MAIN')
        os.makedirs(os.path.join(self.app_dir, '.git'))
        self.script = os.path.join(self.tmp, 'deploy.sh')
        with open(self.script, 'w', encoding='utf-8') as handle:
            handle.write('#!/usr/bin/env bash\nexit 0\n')
        self.state_dir = os.path.join(self.tmp, 'deploy')
        env = {
            'HDC_APP_DIR': self.app_dir,
            'HDC_DEPLOY_SCRIPT': self.script,
            'HDC_DEPLOY_STATE_DIR': self.state_dir,
            'HDC_DEPLOY_WEBHOOK_SECRET': SECRET,
            'HDC_DEPLOY_REPO': 'rehmaahmed11/hdc-main',
            'HDC_DEPLOY_BRANCH': 'main',
            'HDC_DEPLOY_TOKEN': 'test-bearer-token',
            'HDC_DEPLOY_ALLOW_MANUAL': '1',
            'HDC_DEPLOY_COOLDOWN_SECONDS': '45',
            'HDC_DEPLOY_STALE_SECONDS': '900',
            'HDC_DEPLOY_ENV_FILE': os.path.join(self.tmp, 'missing-production.env'),
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ('HDC_DEPLOY_DISABLED', 'HDC_DEPLOY_SYNC', 'HDC_DEPLOY_STATE_FILE'):
            os.environ.pop(key, None)
        # Never really launch bash from the test suite.
        spawn = mock.patch.object(deploy_receiver, 'spawn_detached',
                                  return_value=424242)
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)

    def write_state(self, values):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, 'deploy_state.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(values, handle)

    def read_state(self):
        path = os.path.join(self.state_dir, 'deploy_state.json')
        if not os.path.exists(path):
            return {}
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)


class TestSignatureAndRouting(ReceiverTestCase):

    def test_valid_push_starts_a_deploy(self):
        body = push_payload()
        status, payload, _ = call('POST', '/deploy/github', body,
                                 {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                  'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '202 Accepted')
        self.assertEqual(payload['status'], 'accepted')
        self.assertEqual(self.spawn.call_count, 1)
        env = self.spawn.call_args.args[2]
        self.assertEqual(env['HDC_DEPLOY_SHA'],
                         'abc123abc123abc123abc123abc123abc123ab12')
        self.assertEqual(env['HDC_DEPLOY_SOURCE'], 'webhook')
        self.assertEqual(env['HDC_PIP_AUTOSKIP'], '1')

    def test_bare_path_without_prefix_also_works(self):
        body = push_payload()
        status, _, _ = call('POST', '/github', body,
                           {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                            'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '202 Accepted')

    def test_bad_signature_rejected(self):
        body = push_payload()
        status, payload, _ = call('POST', '/deploy/github', body,
                                 {'HTTP_X_HUB_SIGNATURE_256': 'sha256=' + '0' * 64,
                                  'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '401 Unauthorized')
        self.assertEqual(self.spawn.call_count, 0)
        self.assertNotIn('sha256', json.dumps(payload))

    def test_missing_signature_rejected(self):
        status, payload, _ = call('POST', '/deploy/github', push_payload(),
                                 {'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '401 Unauthorized')
        self.assertEqual(self.spawn.call_count, 0)

    def test_wrong_secret_rejected(self):
        body = push_payload()
        status, _, _ = call('POST', '/deploy/github', body,
                           {'HTTP_X_HUB_SIGNATURE_256': sign(body, 'other-secret'),
                            'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '401 Unauthorized')

    def test_unknown_route_is_404(self):
        status, _, _ = call('GET', '/deploy/nope')
        self.assertEqual(status, '404 Not Found')

    def test_get_on_webhook_is_405(self):
        status, _, _ = call('GET', '/deploy/github')
        self.assertEqual(status, '405 Method Not Allowed')

    def test_oversized_payload_rejected(self):
        with mock.patch.dict(os.environ, {'HDC_DEPLOY_MAX_PAYLOAD': '64'}):
            body = push_payload()
            status, payload, _ = call('POST', '/deploy/github', body,
                                     {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                      'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '400 Bad Request')
        self.assertIn('larger than', payload['message'])

    def test_disabled_without_secret(self):
        with mock.patch.dict(os.environ, {'HDC_DEPLOY_WEBHOOK_SECRET': ''}):
            body = push_payload()
            status, payload, _ = call('POST', '/deploy/github', body,
                                     {'HTTP_X_HUB_SIGNATURE_256': sign(body)})
            self.assertEqual(status, '503 Service Unavailable')
            self.assertEqual(payload['status'], 'disabled')
            health_status, health, _ = call('GET', '/deploy/health')
            self.assertEqual(health_status, '503 Service Unavailable')
            self.assertIn('HDC_DEPLOY_WEBHOOK_SECRET', health['reason'])

    def test_emergency_disabled_flag(self):
        with mock.patch.dict(os.environ, {'HDC_DEPLOY_DISABLED': '1'}):
            body = push_payload()
            status, _, _ = call('POST', '/deploy/github', body,
                               {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '503 Service Unavailable')
        self.assertEqual(self.spawn.call_count, 0)


class TestPushFiltering(ReceiverTestCase):

    def test_other_branch_ignored(self):
        body = push_payload(branch='feature/payroll-v2')
        status, payload, _ = call('POST', '/deploy/github', body,
                                 {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                  'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '200 OK')
        self.assertEqual(payload['status'], 'ignored')
        self.assertIn('feature/payroll-v2', payload['reason'])
        self.assertEqual(self.spawn.call_count, 0)

    def test_other_repository_ignored(self):
        body = push_payload(repo='someone-else/HDC-MAIN')
        status, payload, _ = call('POST', '/deploy/github', body,
                                 {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                  'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '200 OK')
        self.assertEqual(payload['status'], 'ignored')
        self.assertEqual(self.spawn.call_count, 0)

    def test_ping_event_acknowledged_without_deploy(self):
        body = push_payload()
        status, payload, _ = call('POST', '/deploy/github', body,
                                 {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                  'HTTP_X_GITHUB_EVENT': 'ping'})
        self.assertEqual(status, '200 OK')
        self.assertEqual(payload['status'], 'ignored')
        self.assertEqual(self.spawn.call_count, 0)

    def test_branch_deletion_ignored(self):
        body = push_payload(deleted=True)
        status, payload, _ = call('POST', '/deploy/github', body,
                                 {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                  'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '200 OK')
        self.assertEqual(payload['status'], 'ignored')

    def test_null_sha_ignored(self):
        body = push_payload(sha='0' * 40)
        status, payload, _ = call('POST', '/deploy/github', body,
                                 {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                  'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(payload['status'], 'ignored')
        self.assertEqual(self.spawn.call_count, 0)

    def test_invalid_json_rejected(self):
        body = b'{not json'
        status, _, _ = call('POST', '/deploy/github', body,
                           {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                            'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '400 Bad Request')

    def test_cooldown_absorbs_duplicate_deliveries(self):
        self.write_state({'status': 'ok', 'started_epoch': __import__('time').time()})
        body = push_payload()
        status, payload, _ = call('POST', '/deploy/github', body,
                                 {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                  'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '200 OK')
        self.assertEqual(payload['status'], 'ignored')
        self.assertIn('cooldown', payload['reason'])
        self.assertEqual(self.spawn.call_count, 0)

    def test_failed_last_deploy_is_not_blocked_by_cooldown(self):
        self.write_state({'status': 'failed',
                          'started_epoch': __import__('time').time()})
        body = push_payload()
        status, _, _ = call('POST', '/deploy/github', body,
                           {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                            'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '202 Accepted')

    def test_running_deploy_is_not_overlapped(self):
        self.write_state({'status': 'running', 'pid': 4242})
        body = push_payload()
        with mock.patch.object(deploy_receiver, 'pid_alive', return_value=True):
            status, payload, _ = call('POST', '/deploy/github', body,
                                     {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                      'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '202 Accepted')
        self.assertIn('already running', payload['reason'])
        self.assertEqual(self.spawn.call_count, 0)


class TestManualTrigger(ReceiverTestCase):

    def test_token_required(self):
        status, _, _ = call('POST', '/deploy/trigger', b'{}')
        self.assertEqual(status, '401 Unauthorized')

    def test_wrong_token_rejected(self):
        status, _, _ = call('POST', '/deploy/trigger', b'{}',
                           {'HTTP_AUTHORIZATION': 'Bearer nope'})
        self.assertEqual(status, '401 Unauthorized')

    def test_rollback_revision_passed_through(self):
        status, payload, _ = call(
            'POST', '/deploy/trigger',
            json.dumps({'revision': 'deadbeef1234567890'}).encode(),
            {'HTTP_AUTHORIZATION': 'Bearer test-bearer-token'})
        self.assertEqual(status, '202 Accepted')
        env = self.spawn.call_args.args[2]
        self.assertEqual(env['HDC_DEPLOY_REVISION'], 'deadbeef1234567890')

    def test_bad_revision_rejected(self):
        status, payload, _ = call(
            'POST', '/deploy/trigger',
            json.dumps({'revision': '; rm -rf /'}).encode(),
            {'HTTP_AUTHORIZATION': 'Bearer test-bearer-token'})
        self.assertEqual(status, '400 Bad Request')
        self.assertEqual(self.spawn.call_count, 0)

    def test_dangerous_ref_rejected(self):
        for ref in ('--upload-pack=evil', 'a..b', 'heads/../..'):
            with self.subTest(ref=ref):
                status, _, _ = call(
                    'POST', '/deploy/trigger', json.dumps({'ref': ref}).encode(),
                    {'HTTP_AUTHORIZATION': 'Bearer test-bearer-token'})
                self.assertEqual(status, '400 Bad Request')
        self.assertEqual(self.spawn.call_count, 0)

    def test_manual_disabled_by_default(self):
        with mock.patch.dict(os.environ, {'HDC_DEPLOY_ALLOW_MANUAL': '0'}):
            status, payload, _ = call('POST', '/deploy/trigger', b'{}',
                                     {'HTTP_AUTHORIZATION': 'Bearer test-bearer-token'})
        self.assertEqual(status, '403 Forbidden')

    def test_force_flag_forwarded(self):
        status, _, _ = call('POST', '/deploy/trigger',
                            json.dumps({'force': True}).encode(),
                            {'HTTP_AUTHORIZATION': 'Bearer test-bearer-token'})
        self.assertEqual(status, '202 Accepted')
        self.assertEqual(self.spawn.call_args.args[2]['HDC_DEPLOY_FORCE'], '1')


class TestHealthAndState(ReceiverTestCase):

    def test_health_exposes_state_but_not_secrets(self):
        self.write_state({'status': 'ok', 'sha': 'abc123'})
        status, payload, headers = call('GET', '/deploy/health')
        self.assertEqual(status, '200 OK')
        self.assertTrue(payload['deploy_enabled'])
        self.assertEqual(payload['deploy']['sha'], 'abc123')
        body = json.dumps(payload)
        for leak in (SECRET, 'test-bearer-token', 'HDC_SECRET_KEY'):
            self.assertNotIn(leak, body)
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_health_reports_lost_child(self):
        self.write_state({'status': 'running', 'pid': 999999})
        _, payload, _ = call('GET', '/deploy/health')
        self.assertEqual(payload['deploy']['status'], 'lost')
        self.assertFalse(payload['deploy_running'])
        self.assertIn('Bash console', payload['deploy']['note'])

    def test_zombie_child_is_not_forever_running(self):
        """An unreaped child must not block every future deploy."""
        import subprocess
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        time.sleep(0.6)  # it has exited by now but is still a zombie
        self.write_state({'status': 'running', 'pid': child.pid,
                          'started_epoch': time.time()})
        self.assertTrue(deploy_receiver.pid_alive(child.pid),
                        'the zombie should still answer to signal 0')
        with mock.patch.object(deploy_receiver, 'pid_alive',
                               side_effect=deploy_receiver.pid_alive):
            _, payload, _ = call('GET', '/deploy/health')
        self.assertNotEqual(payload['deploy']['status'], 'running')
        self.assertFalse(payload['deploy_running'])
        child.wait(timeout=5)

    def test_stale_running_state_is_reported_as_stale(self):
        self.write_state({'status': 'running', 'pid': 4242,
                          'started_epoch': time.time() - 4000})
        with mock.patch.object(deploy_receiver, 'pid_alive', return_value=True), \
             mock.patch.object(deploy_receiver, 'reap_finished_child', return_value=False):
            _, payload, _ = call('GET', '/deploy/health')
            self.assertEqual(payload['deploy']['status'], 'stale')
            body = push_payload()
            status, accepted, _ = call('POST', '/deploy/github', body,
                                       {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                        'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '202 Accepted')
        self.assertNotIn('already running', json.dumps(accepted))

    def test_status_needs_token(self):
        status, _, _ = call('GET', '/deploy/status')
        self.assertEqual(status, '401 Unauthorized')
        status, payload, _ = call('GET', '/deploy/status', b'',
                                  {'HTTP_AUTHORIZATION': 'Bearer test-bearer-token'})
        self.assertEqual(status, '200 OK')
        self.assertIn('log_tail', payload)

    def test_state_written_on_launch_and_log_created(self):
        body = push_payload()
        call('POST', '/deploy/github', body,
             {'HTTP_X_HUB_SIGNATURE_256': sign(body), 'HTTP_X_GITHUB_EVENT': 'push'})
        state = self.read_state()
        self.assertEqual(state['status'], 'running')
        self.assertEqual(state['trigger'], 'push')
        self.assertEqual(state['pid'], 424242)
        with open(os.path.join(self.state_dir, 'deploy.log'), encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn('HDC deploy started', text)

    def test_state_file_points_at_the_deploy_script(self):
        body = push_payload()
        call('POST', '/deploy/github', body,
             {'HTTP_X_HUB_SIGNATURE_256': sign(body), 'HTTP_X_GITHUB_EVENT': 'push'})
        launched_script = self.spawn.call_args.args[0]
        self.assertEqual(launched_script, self.script)
        self.assertEqual(self.read_state()['status'], 'running')

    def test_sync_mode_reports_failure_with_log_tail(self):
        with open(self.script, 'w', encoding='utf-8') as handle:
            handle.write('#!/usr/bin/env bash\necho "boom: layer check failed"\nexit 1\n')
        with mock.patch.dict(os.environ, {'HDC_DEPLOY_SYNC': '1'}):
            body = push_payload()
            status, payload, _ = call('POST', '/deploy/github', body,
                                      {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                       'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '500 Internal Server Error')
        self.assertEqual(payload['exit_code'], 1)
        self.assertTrue(any('boom' in line for line in payload['log_tail']))
        self.assertEqual(self.read_state()['status'], 'failed')

    def test_corrupt_state_file_does_not_break_health(self):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, 'deploy_state.json'), 'w',
                  encoding='utf-8') as handle:
            handle.write('{"truncated')
        status, payload, _ = call('GET', '/deploy/health')
        self.assertEqual(status, '200 OK')
        self.assertEqual(payload['deploy'], {})


class TestIsolationContract(ReceiverTestCase):
    """The receiver must stay independent of the ERP it deploys."""

    def test_no_third_party_or_erp_imports(self):
        with open(os.path.join(ROOT, 'deploy_receiver.py'), encoding='utf-8') as handle:
            source = handle.read()
        for forbidden in ('import flask', 'from flask', 'from hdc', 'import hdc',
                          'sqlalchemy', 'Flask('):
            self.assertNotIn(forbidden, source.lower())

    def test_no_database_paths_used(self):
        with open(os.path.join(ROOT, 'deploy_receiver.py'), encoding='utf-8') as handle:
            source = handle.read()
        # The receiver must not read/write the ERP database itself.
        self.assertNotIn('sqlite3', source)
        self.assertNotIn('HDC_DB_PATH', source)
        self.assertIsNone(re.search(r'\bopen\([^)]*\.db', source))

    def test_errors_never_leak_tracebacks(self):
        with mock.patch.object(deploy_receiver, 'handle_webhook',
                               side_effect=RuntimeError('secret internal detail')):
            body = push_payload()
            status, payload, _ = call('POST', '/deploy/github', body,
                                      {'HTTP_X_HUB_SIGNATURE_256': sign(body),
                                       'HTTP_X_GITHUB_EVENT': 'push'})
        self.assertEqual(status, '500 Internal Server Error')
        self.assertNotIn('secret internal detail', json.dumps(payload))

    def test_selfcheck_runs_without_traceback(self):
        code = deploy_receiver.selfcheck(stream=io.StringIO())
        self.assertIn(code, (0, 1))


if __name__ == '__main__':
    unittest.main(verbosity=2)
