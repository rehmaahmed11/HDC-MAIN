"""Regression tests for per-user page permissions and stage-scoped data."""
import json
import os
import tempfile
import unittest

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.auth import ActivityLog, HDCUser, UserActivity
from hdc.models.projects import Project, Stage
from werkzeug.security import check_password_hash, generate_password_hash


class UserPermissionsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-user-access-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp.name, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp.name,
            'TESTING': True,
        })
        self.client = self.app.test_client()
        self.csrf = 'user-access-test-csrf'
        with self.app.app_context():
            admin = HDCUser.query.filter_by(username='admin').one()
            user = HDCUser(username='stage-supervisor', password_hash='unused', role='manager')
            first_project = Project(name='North House', project_code='NA-1')
            second_project = Project(name='South House', project_code='NA-2')
            db.session.add_all([user, first_project, second_project])
            db.session.flush()
            allowed = Stage(project_id=first_project.id, name='Foundation')
            denied = Stage(project_id=second_project.id, name='Roof')
            db.session.add_all([allowed, denied])
            db.session.commit()
            user.permissions_json = json.dumps({
                'dashboard': {'read': True, 'write': False},
                'stages': {'read': True, 'write': False},
                'projects': {'read': True, 'write': False},
                'stage_ledger': {'read': True, 'write': False},
                'stage_create': {'read': True, 'write': True},
                'workers': {'read': True, 'write': False},
            })
            user.stage_scope_enabled = True
            user.allowed_stage_ids_json = json.dumps([allowed.id])
            db.session.commit()
            self.admin_id = admin.id
            self.user_id = user.id
            self.allowed_stage_id = allowed.id
            self.denied_stage_id = denied.id
            self.allowed_project_id = first_project.id
        with self.client.session_transaction() as session:
            session['_user_id'] = str(self.user_id)
            session['_fresh'] = True
            session['_csrf_token'] = self.csrf

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.tmp.cleanup()

    def _sign_in_as_admin(self):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(self.admin_id)
            session['_fresh'] = True
            session['_csrf_token'] = self.csrf

    def test_custom_page_read_is_enforced_and_write_is_separate(self):
        self.assertEqual(self.client.get('/hdc/stages').status_code, 200)
        denied = self.client.get('/hdc/payroll')
        self.assertEqual(denied.status_code, 403)
        self.assertIn("don't have access to this page", denied.get_data(as_text=True))
        response = self.client.post('/hdc/workers', data={
            '_csrf_token': self.csrf, 'worker_code': 'NOPE', 'name': 'No write',
            'role_type': 'Mason',
        })
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            from hdc.models.workforce import Worker
            self.assertIsNone(Worker.query.filter_by(worker_code='NOPE').first())

    def test_explicit_custom_write_grant_can_create_operational_records(self):
        from hdc.models.workforce import Worker, WorkerTrade
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            grants = json.loads(user.permissions_json)
            grants['workers']['write'] = True
            user.permissions_json = json.dumps(grants)
            if not WorkerTrade.query.filter_by(name='Mason').first():
                db.session.add(WorkerTrade(name='Mason'))
            db.session.commit()
        response = self.client.post('/hdc/workers', data={
            '_csrf_token': self.csrf, 'worker_code': 'ALLOW-1', 'name': 'Allowed Worker',
            'role_type': 'Mason', 'daily_wage': '1000',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(Worker.query.filter_by(worker_code='ALLOW-1').first())

    def test_stage_scope_filters_lists_and_blocks_direct_other_stage_urls(self):
        response = self.client.get('/hdc/stages')
        body = response.get_data(as_text=True)
        self.assertIn('Foundation', body)
        self.assertNotIn('Roof', body)
        # Project lists are limited to projects with an assigned stage too.
        projects = self.client.get('/hdc/projects')
        self.assertIn('North House', projects.get_data(as_text=True))
        self.assertNotIn('South House', projects.get_data(as_text=True))
        self.assertEqual(self.client.get(f'/hdc/stage/{self.allowed_stage_id}/ledger').status_code, 200)
        self.assertEqual(self.client.get(f'/hdc/stage/{self.denied_stage_id}/ledger').status_code, 403)

    def test_stage_write_scope_is_separate_from_stage_read_scope(self):
        url = f'/hdc/stage/{self.allowed_stage_id}/status'
        data = {'_csrf_token': self.csrf, 'status': 'Paused'}
        # The page allows writes, but this particular stage is only in the read list.
        self.assertEqual(self.client.post(url, data=data).status_code, 403)
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            user.write_stage_ids_json = json.dumps([self.allowed_stage_id])
            db.session.commit()
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            stage = db.session.get(Stage, self.allowed_stage_id)
            self.assertEqual(stage.status, 'Paused')
        # A different stage still cannot be posted even when the page is writable.
        self.assertEqual(self.client.post(
            f'/hdc/stage/{self.denied_stage_id}/status', data=data).status_code, 403)

    def test_sidebar_only_renders_granted_pages(self):
        response = self.client.get('/hdc/')
        body = response.get_data(as_text=True)
        self.assertIn('Stages', body)
        self.assertIn('Workers', body)
        self.assertNotIn('Payroll', body)
        self.assertNotIn('Accounts &amp; Cash', body)

    def test_admin_saves_page_grants_and_stage_scope(self):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(self.admin_id)
            session['_fresh'] = True
        response = self.client.post('/hdc/users', data={
            '_csrf_token': self.csrf, 'action': 'configure_permissions',
            'user_id': str(self.user_id), 'custom_permissions': '1', 'role': 'staff',
            'read_stage_ledger': '1', 'write_stage_ledger': '1',
            'stage_scope_enabled': '1', 'read_stage_ids': str(self.allowed_stage_id),
            'write_stage_ids': str(self.allowed_stage_id),
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            grants = json.loads(user.permissions_json)
            self.assertEqual(user.role, 'staff')
            self.assertEqual(grants['stage_ledger'], {'read': True, 'write': True})
            self.assertTrue(user.stage_scope_enabled)
            self.assertEqual(json.loads(user.allowed_stage_ids_json), [self.allowed_stage_id])
            self.assertEqual(json.loads(user.write_stage_ids_json), [self.allowed_stage_id])

    def test_admin_page_explains_password_privacy_and_recovery(self):
        self._sign_in_as_admin()
        response = self.client.get('/hdc/users')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('passwords are stored as one-way hashes', body)
        self.assertIn('Current password: <strong>not viewable</strong>', body)
        self.assertIn('Password / reset', body)
        self.assertIn('Confirm new password', body)
        self.assertIn('Suspend', body)
        self.assertIn('Active', body)

    def test_admin_resets_password_with_confirmation_and_audit_redaction(self):
        old_password = 'Old#Password2026'
        new_password = 'New#Password2026'
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            user.password_hash = generate_password_hash(old_password)
            db.session.commit()
            old_hash = user.password_hash
            old_auth_version = user.auth_version
        self._sign_in_as_admin()

        mismatch = self.client.post('/hdc/users', data={
            '_csrf_token': self.csrf, 'action': 'reset_password',
            'user_id': str(self.user_id), 'new_password': new_password,
            'confirm_password': 'Other#Password2026',
        }, follow_redirects=True)
        self.assertIn('do not match', mismatch.get_data(as_text=True))
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            self.assertTrue(check_password_hash(user.password_hash, old_password))

        response = self.client.post('/hdc/users', data={
            '_csrf_token': self.csrf, 'action': 'reset_password',
            'user_id': str(self.user_id), 'new_password': new_password,
            'confirm_password': new_password,
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            self.assertTrue(check_password_hash(user.password_hash, new_password))
            self.assertFalse(check_password_hash(user.password_hash, old_password))
            self.assertEqual(user.auth_version, old_auth_version + 1)
            new_hash = user.password_hash
            change = (UserActivity.query
                      .filter_by(entity_type='hdc_user', entity_id=str(self.user_id), event_type='update')
                      .order_by(UserActivity.id.desc()).first())
            self.assertIsNotNone(change)
            changed = json.loads(change.changed_fields)
            self.assertEqual(changed['password_hash'], {'old': '[redacted]', 'new': '[redacted]'})
            event = ActivityLog.query.filter_by(entity_type='user_account', entity_id=str(self.user_id)).order_by(ActivityLog.id.desc()).first()
            self.assertIsNotNone(event)
            self.assertNotIn(old_hash, event.description)
            self.assertNotIn(new_hash, event.description)

    def test_suspend_blocks_new_login_and_invalidates_existing_session(self):
        password = 'Staff#Password2026'
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            user.password_hash = generate_password_hash(password)
            user.is_active = True
            user.permissions_json = None
            db.session.commit()

        user_client = self.app.test_client()
        user_client.get('/hdc/login')
        with user_client.session_transaction() as session:
            login_csrf = session['_csrf_token']
        login_response = user_client.post('/hdc/login', data={
            '_csrf_token': login_csrf, 'username': 'stage-supervisor', 'password': password,
        })
        self.assertEqual(login_response.status_code, 302)
        with user_client.session_transaction() as session:
            self.assertEqual(session.get('_user_id'), str(self.user_id))

        self._sign_in_as_admin()
        response = self.client.post('/hdc/users', data={
            '_csrf_token': self.csrf, 'action': 'set_status',
            'user_id': str(self.user_id), 'status': 'suspended',
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertFalse(db.session.get(HDCUser, self.user_id).is_active)

        # The suspended account's already-open session is no longer restored.
        self.assertEqual(user_client.get('/hdc/users').status_code, 302)
        fresh_client = self.app.test_client()
        fresh_client.get('/hdc/login')
        with fresh_client.session_transaction() as session:
            fresh_csrf = session['_csrf_token']
        failed = fresh_client.post('/hdc/login', data={
            '_csrf_token': fresh_csrf, 'username': 'stage-supervisor', 'password': password,
        })
        self.assertEqual(failed.status_code, 200)
        self.assertIn('Invalid username or password', failed.get_data(as_text=True))

        self.client.post('/hdc/users', data={
            '_csrf_token': self.csrf, 'action': 'set_status',
            'user_id': str(self.user_id), 'status': 'active',
        })
        with self.app.app_context():
            self.assertTrue(db.session.get(HDCUser, self.user_id).is_active)
        reactivated = fresh_client.post('/hdc/login', data={
            '_csrf_token': fresh_csrf, 'username': 'stage-supervisor', 'password': password,
        })
        self.assertEqual(reactivated.status_code, 302)

    def test_admin_cannot_suspend_own_account(self):
        self._sign_in_as_admin()
        response = self.client.post('/hdc/users', data={
            '_csrf_token': self.csrf, 'action': 'set_status',
            'user_id': str(self.admin_id), 'status': 'suspended',
        }, follow_redirects=True)
        self.assertIn('cannot suspend your own account', response.get_data(as_text=True))
        with self.app.app_context():
            self.assertTrue(db.session.get(HDCUser, self.admin_id).is_active)

    def test_admin_can_open_the_permissions_editor(self):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(self.admin_id)
            session['_fresh'] = True
        response = self.client.get('/hdc/users')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Configure custom access now', body)
        self.assertIn('Limit this user to selected stages', body)
        self.assertIn('purchase_usage', body)


if __name__ == '__main__':
    unittest.main()
