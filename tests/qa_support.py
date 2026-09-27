"""Disposable, explicitly configured fixture for release-gate tests."""
import os
import tempfile
import unittest
from unittest.mock import patch

from hdc.app import create_app
from hdc.extensions import db


class IsolatedAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-release-test-')
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'qa.db')
        self.env = patch.dict(os.environ, {'HDC_ENV': 'test', 'HDC_SECRET_KEY': 'release-test-only',
                                          'HDC_BOOTSTRAP_ADMIN_PASSWORD': 'Release-Test-Only-123',
                                          'HDC_DB_PATH': self.path, 'HDC_INSTANCE_DIR': self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = create_app({'TESTING': True, 'HDC_DB_PATH': self.path,
                               'HDC_INSTANCE_DIR': self.tmp.name})
        self.addCleanup(self.dispose)
        self.client = self.app.test_client()
        self.login()

    def dispose(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def login(self):
        self.client.get('/hdc/login')
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']
        response = self.client.post('/hdc/login', data={'username': 'admin',
                                    'password': 'Release-Test-Only-123', '_csrf_token': self.token})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIn('_user_id', session)
            self.token = session['_csrf_token']

    def form(self, url, **data):
        response = self.client.post(url, data=dict(data, _csrf_token=self.token))
        self.assertEqual(response.status_code, 302, (url, response.get_data(as_text=True)[:300]))
        return response

    def api(self, url, data, method='POST', expected=200):
        response = self.client.open(url, method=method, json=data, headers={'X-CSRFToken': self.token})
        self.assertEqual(response.status_code, expected, (url, response.get_data(as_text=True)[:300]))
        return response.get_json()
