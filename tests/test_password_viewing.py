"""Administrators can view an account's current password - and nothing else can.

The viewable copy is an encrypted token next to the login hash (see
``hdc/services/password_vault.py``).  These tests pin the feature *and* its
guard rails:

* every path that sets a password keeps the copy in step with the hash;
* only an administrator can read it, over POST with a CSRF token, uncached;
* the page never carries a password, the audit trail never holds one, and no
  plain-text password ever reaches the database file;
* a stale, missing or unreadable copy is reported honestly instead of shown;
* a server without the crypto library or key keeps working (logins, creating
  users, resets) and says why viewing is off.
"""
import html
import json
import re
import sqlite3
import sys
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from flask import Flask
from werkzeug.security import check_password_hash, generate_password_hash

from qa_support import IsolatedAppTest
from hdc.extensions import db
from hdc.models.auth import ActivityLog, HDCUser, UserActivity
from hdc.services import password_vault
from hdc.services.password_vault import (assign_password, open_password, seal_password,
                                         vault_problem, viewable_password)

PASSWORD = 'Viewable#Pass2026'
NEW_PASSWORD = 'Replaced#Pass2026'
OTHER_PASSWORD = 'Another#Pass2026x'
ADMIN_PASSWORD = 'Release-Test-Only-123'     # qa_support bootstraps the admin with this
_MISSING = object()


@contextmanager
def without_crypto_library():
    """Make ``from cryptography.fernet import Fernet`` fail, as on a bare virtualenv.

    Only this one ``sys.modules`` key is touched: restoring the whole dict could
    unload application modules that were first imported inside the block.
    """
    saved = sys.modules.get('cryptography.fernet', _MISSING)
    sys.modules['cryptography.fernet'] = None
    try:
        yield
    finally:
        if saved is _MISSING:
            sys.modules.pop('cryptography.fernet', None)
        else:
            sys.modules['cryptography.fernet'] = saved


class PasswordVaultServiceTest(unittest.TestCase):
    """The encryption helper on its own, inside a bare Flask application context."""

    def setUp(self):
        self.shell = Flask('vault-service-test')
        self.shell.config.update(SECRET_KEY='service-test-secret', HDC_PASSWORD_VAULT_KEY='')
        context = self.shell.app_context()
        context.push()
        self.addCleanup(context.pop)

    def test_round_trip_keeps_unicode_symbols_and_spaces(self):
        for password in ('Abc#1234', 'Pässwörd#2026', 'ü' * 40 + 'A1#', '  spaced Out#1 ', 'p\u200bw#A1b2c3'):
            with self.subTest(password=password):
                token = seal_password(password)
                self.assertNotIn(password, token)
                self.assertEqual(open_password(token), password)

    def test_a_stored_token_is_never_the_plain_password_and_never_repeats(self):
        first, second = seal_password(PASSWORD), seal_password(PASSWORD)
        self.assertNotEqual(first, second)
        self.assertNotIn(PASSWORD, first + second)

    def test_damaged_or_foreign_tokens_read_as_nothing(self):
        token = seal_password(PASSWORD)
        damaged = token[:-4] + ('AAAA' if not token.endswith('AAAA') else 'BBBB')
        for bad in (damaged, 'not a token', 'ünïcode', token[:20]):
            with self.subTest(token=bad):
                self.assertIsNone(open_password(bad))
        self.shell.config['HDC_PASSWORD_VAULT_KEY'] = 'a-different-key'
        self.assertIsNone(open_password(token))
        self.shell.config['HDC_PASSWORD_VAULT_KEY'] = ''
        self.assertEqual(open_password(token), PASSWORD)

    def test_nothing_to_store_means_no_copy(self):
        for empty in (None, ''):
            self.assertIsNone(seal_password(empty))
            self.assertIsNone(open_password(empty))

    def test_a_dedicated_key_decouples_the_vault_from_the_session_secret(self):
        self.shell.config['HDC_PASSWORD_VAULT_KEY'] = 'vault-key-1'
        token = seal_password(PASSWORD)
        self.shell.config['SECRET_KEY'] = 'rotated-session-secret'
        self.assertEqual(open_password(token), PASSWORD)
        # Without a dedicated key the session secret is what protects the copy.
        self.shell.config['HDC_PASSWORD_VAULT_KEY'] = ''
        self.assertIsNone(open_password(token))

    def test_missing_library_is_reported_and_nothing_is_stored(self):
        self.assertEqual(vault_problem(), '')
        with without_crypto_library():
            self.assertEqual(vault_problem(), password_vault.MSG_NO_LIBRARY)
            self.assertIsNone(seal_password(PASSWORD))
            self.assertIsNone(open_password('anything'))
        self.assertEqual(vault_problem(), '')

    def test_missing_key_is_reported_and_nothing_is_stored(self):
        self.shell.config.update(SECRET_KEY='', HDC_PASSWORD_VAULT_KEY='')
        self.assertEqual(vault_problem(), password_vault.MSG_NO_KEY)
        self.assertIsNone(seal_password(PASSWORD))

    def test_outside_an_application_context_means_unavailable_not_a_crash(self):
        result = {}

        def run():
            result.update(problem=vault_problem(), sealed=seal_password(PASSWORD),
                          opened=open_password('x'))
        worker = threading.Thread(target=run)
        worker.start()
        worker.join()
        self.assertEqual(result, {'problem': password_vault.MSG_NO_KEY, 'sealed': None, 'opened': None})

    def test_assign_password_sets_the_hash_and_the_copy_together(self):
        user = SimpleNamespace(password_hash=None, password_vault=None)
        assign_password(user, PASSWORD)
        self.assertTrue(check_password_hash(user.password_hash, PASSWORD))
        self.assertEqual(viewable_password(user), PASSWORD)
        assign_password(user, NEW_PASSWORD)
        self.assertTrue(check_password_hash(user.password_hash, NEW_PASSWORD))
        self.assertEqual(viewable_password(user), NEW_PASSWORD)

    def test_a_copy_that_no_longer_matches_the_hash_is_never_returned(self):
        user = SimpleNamespace(password_hash=None, password_vault=None)
        assign_password(user, PASSWORD)
        user.password_hash = generate_password_hash('Changed#Elsewhere1')
        self.assertIsNone(viewable_password(user))
        user.password_hash = 'unused'              # not even a real hash
        self.assertIsNone(viewable_password(user))
        user.password_hash, user.password_vault = None, None
        self.assertIsNone(viewable_password(user))

    def test_a_corrupt_hash_row_reads_as_no_match_instead_of_raising(self):
        user = SimpleNamespace(password_hash=None, password_vault=None)
        assign_password(user, PASSWORD)
        for broken in ('scrypt:x:y:z$salt$hash', 'pbkdf2:sha256:notanumber$salt$hash', '$$',
                       'scrypt:99999999999:8:1$salt$abcd'):
            with self.subTest(hash=broken):
                user.password_hash = broken
                self.assertIsNone(viewable_password(user))

    def test_setting_a_password_without_the_vault_drops_the_old_copy(self):
        user = SimpleNamespace(password_hash=None, password_vault=None)
        assign_password(user, PASSWORD)
        self.assertTrue(user.password_vault)
        with without_crypto_library():
            assign_password(user, NEW_PASSWORD)
        self.assertIsNone(user.password_vault)      # never left describing the old password
        self.assertTrue(check_password_hash(user.password_hash, NEW_PASSWORD))


class PasswordViewingFlowTest(IsolatedAppTest):
    """The feature through the real routes, with a real admin session."""

    # -- helpers -------------------------------------------------------------

    def add_user(self, username='ali', role='staff', password=PASSWORD):
        self.form('/hdc/users', action='add', username=username, password=password, role=role)
        return self.user_row(username)['id']

    def add_hash_only_user(self, username, role='manager', password='Old#Manager2026'):
        """An account as production has them today: a hash and no viewable copy."""
        with self.app.app_context():
            user = HDCUser(username=username, role=role,
                           password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            return user.id

    def user_row(self, username):
        with self.app.app_context():
            user = HDCUser.query.filter_by(username=username).one()
            return {'id': user.id, 'hash': user.password_hash, 'vault': user.password_vault}

    def reveal(self, user_id, expected=200, client=None, token=None):
        response = (client or self.client).post(
            f'/hdc/users/{user_id}/password', headers={'X-CSRFToken': token or self.token})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True)[:300])
        return response

    def reset(self, user_id, new_password, confirm=None):
        return self.form('/hdc/users', action='reset_password', user_id=str(user_id),
                         new_password=new_password,
                         confirm_password=new_password if confirm is None else confirm)

    def client_for(self, username, password):
        client = self.app.test_client()
        client.get('/hdc/login')
        with client.session_transaction() as session:
            token = session['_csrf_token']
        response = client.post('/hdc/login', data={'username': username, 'password': password,
                                                   '_csrf_token': token})
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertIn('_user_id', session)
            return client, session['_csrf_token']

    def users_page(self):
        response = self.client.get('/hdc/users')
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    @staticmethod
    def password_cell(page, username):
        match = re.search(r'<td[^>]*data-password-cell[^>]*data-username="%s"[^>]*>(.*?)</td>'
                          % re.escape(username), page, re.S)
        assert match, f'no password cell for {username}'
        return match.group(1)

    def assert_absent(self, secret, haystack, where):
        """Like assertNotIn, but never prints the (huge) page or database dump."""
        if secret in haystack:
            self.fail(f'a secret leaked into {where}')

    def database_dump(self):
        con = sqlite3.connect(self.path)
        try:
            return '\n'.join(con.iterdump())
        finally:
            con.close()

    def view_events(self):
        with self.app.app_context():
            return [(row.username, row.entity_type, row.entity_id, row.description)
                    for row in ActivityLog.query.filter_by(action_type='view').order_by(ActivityLog.id)]

    # -- the feature ---------------------------------------------------------

    def test_admin_sees_a_new_users_password_and_it_is_stored_encrypted(self):
        user_id = self.add_user()
        data = self.reveal(user_id).get_json()
        self.assertEqual(data, {'ok': True, 'password': PASSWORD})
        row = self.user_row('ali')
        self.assertTrue(row['vault'])
        self.assertNotIn(PASSWORD, row['vault'])
        self.assertTrue(check_password_hash(row['hash'], PASSWORD))      # login still uses the hash

    def test_the_password_that_is_shown_is_the_one_that_logs_in(self):
        user_id = self.add_user()
        shown = self.reveal(user_id).get_json()['password']
        self.client_for('ali', shown)          # asserts the login succeeded

    def test_resetting_replaces_the_viewable_password(self):
        user_id = self.add_user()
        self.reset(user_id, NEW_PASSWORD)
        self.assertEqual(self.reveal(user_id).get_json()['password'], NEW_PASSWORD)
        # Rejected resets (mismatch, too weak) leave the saved copy untouched.
        self.reset(user_id, OTHER_PASSWORD, confirm='something else')
        self.reset(user_id, 'weak')
        self.assertEqual(self.reveal(user_id).get_json()['password'], NEW_PASSWORD)

    def test_the_bootstrap_admins_password_is_viewable(self):
        admin_id = self.user_row('admin')['id']
        self.assertEqual(self.reveal(admin_id).get_json()['password'], ADMIN_PASSWORD)

    def test_account_set_before_this_feature_is_explained_not_guessed(self):
        legacy_id = self.add_hash_only_user('old_manager')
        response = self.reveal(legacy_id, expected=409)
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertNotIn('password', data)
        self.assertIn('before password viewing existed', data['message'])
        self.assertIn('Set a new password', data['message'])
        cell = self.password_cell(self.users_page(), 'old_manager')
        self.assertIn('Not saved yet', cell)
        self.assertNotIn('data-password-toggle', cell)
        self.assertEqual(self.view_events(), [])      # nothing was disclosed, nothing to record
        # One reset is all it takes to make that account viewable.
        self.reset(legacy_id, NEW_PASSWORD)
        self.assertEqual(self.reveal(legacy_id).get_json()['password'], NEW_PASSWORD)

    def test_a_copy_that_no_longer_matches_is_never_shown(self):
        user_id = self.add_user()
        with self.app.app_context():
            user = db.session.get(HDCUser, user_id)
            # A path that knows nothing about the vault (raw SQL, an old script).
            user.password_hash = generate_password_hash('Changed#Elsewhere1')
            db.session.commit()
        response = self.reveal(user_id, expected=409)
        self.assertNotIn(PASSWORD, response.get_data(as_text=True))
        self.assertIn('no longer matches', response.get_json()['message'])
        self.assertEqual(self.view_events(), [])

    def test_a_corrupt_hash_row_gives_a_clean_refusal_not_a_server_error(self):
        user_id = self.add_user()
        with self.app.app_context():
            db.session.get(HDCUser, user_id).password_hash = 'scrypt:x:y:z$salt$hash'
            db.session.commit()
        response = self.reveal(user_id, expected=409)
        self.assertNotIn(PASSWORD, response.get_data(as_text=True))
        self.assertEqual(self.client.get('/hdc/users').status_code, 200)

    def test_users_page_marks_each_account_by_whether_it_can_be_viewed(self):
        self.add_user('ali')
        self.add_hash_only_user('old_manager')
        page = self.users_page()
        self.assertIn('<th>Password</th>', page)
        self.assertIn('data-password-toggle', self.password_cell(page, 'ali'))
        self.assertIn('Not saved yet', self.password_cell(page, 'old_manager'))
        self.assertIn('data-password-toggle', self.password_cell(page, 'admin'))
        self.assertIn(f'/hdc/users/{self.user_row("ali")["id"]}/password', page)

    def test_users_page_explains_viewing_instead_of_claiming_passwords_are_hidden(self):
        page = self.users_page()
        self.assertIn('Viewing passwords:', page)
        self.assertIn('every view is recorded in the Event Recorder', page)
        for stale_claim in ('not viewable', 'cannot be viewed', 'Password viewing is off'):
            if stale_claim in page:
                self.fail(f'users page still says {stale_claim!r}')

    def test_the_users_page_never_carries_a_password_or_a_stored_token(self):
        user_id = self.add_user()
        self.reveal(user_id)
        self.reset(user_id, NEW_PASSWORD)
        self.reveal(user_id)
        page = self.users_page()
        for secret in (PASSWORD, NEW_PASSWORD, ADMIN_PASSWORD):
            self.assert_absent(secret, page, 'the users page')
        self.assert_absent('gAAAAA', page, 'the users page (stored token)')   # every Fernet token starts like this

    # -- who may look, and how --------------------------------------------------

    def test_only_administrators_can_view_passwords(self):
        victim = self.add_user('victim')
        viewers = [(role, None) for role in ('manager', 'accountant', 'staff')]
        # Even an explicit grant of the user-management page cannot delegate this.
        viewers.append(('staff', {'users': {'read': True, 'write': True}}))
        for index, (role, grants) in enumerate(viewers):
            username = f'viewer{index}-{role}'
            with self.subTest(role=role, custom_grants=bool(grants)):
                with self.app.app_context():
                    user = HDCUser(username=username, role=role)
                    assign_password(user, OTHER_PASSWORD)
                    if grants:
                        user.permissions_json = json.dumps(grants)
                    db.session.add(user)
                    db.session.commit()
                client, token = self.client_for(username, OTHER_PASSWORD)
                response = client.post(f'/hdc/users/{victim}/password', headers={'X-CSRFToken': token})
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(PASSWORD, response.get_data(as_text=True))
        self.assertEqual(self.view_events(), [])

    def test_signing_in_is_required(self):
        victim = self.add_user()
        stranger = self.app.test_client()
        stranger.get('/hdc/login')
        with stranger.session_transaction() as session:
            token = session['_csrf_token']
        response = stranger.post(f'/hdc/users/{victim}/password', headers={'X-CSRFToken': token})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/hdc/login', response.location)
        self.assertNotIn(PASSWORD, response.get_data(as_text=True))

    def test_the_csrf_token_is_required(self):
        victim = self.add_user()
        for headers in ({}, {'X-CSRFToken': 'forged'}):
            with self.subTest(headers=headers):
                response = self.client.post(f'/hdc/users/{victim}/password', headers=headers)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(PASSWORD, response.get_data(as_text=True))
        self.assertEqual(self.view_events(), [])

    def test_a_plain_link_or_prefetch_cannot_trigger_a_view(self):
        victim = self.add_user()
        self.assertEqual(self.client.get(f'/hdc/users/{victim}/password').status_code, 405)

    def test_unknown_account_is_a_404(self):
        self.assertEqual(self.reveal(999999, expected=404).get_json()['message'], 'User not found.')

    def test_nothing_the_endpoint_returns_may_be_cached(self):
        victim = self.add_user()
        legacy = self.add_hash_only_user('old_manager')
        for response in (self.reveal(victim), self.reveal(legacy, expected=409),
                         self.reveal(999999, expected=404)):
            cache = response.headers['Cache-Control']
            self.assertIn('no-store', cache)
            self.assertIn('private', cache)
            self.assertEqual(response.headers['Pragma'], 'no-cache')

    # -- audit trail and data at rest --------------------------------------------

    def test_every_view_is_recorded_and_the_record_never_holds_the_password(self):
        user_id = self.add_user()
        self.reveal(user_id)
        self.reveal(user_id)
        events = self.view_events()
        self.assertEqual(len(events), 2)
        for username, entity_type, entity_id, description in events:
            self.assertEqual((username, entity_type, entity_id), ('admin', 'user_account', str(user_id)))
            self.assertEqual(description, 'Password viewed for user ali.')
        page = self.client.get('/hdc/event-recorder?event_type=view').get_data(as_text=True)
        self.assertIn('Password viewed for user ali.', page)
        self.assertIn('<option value="view" selected>View</option>', page)
        self.assert_absent(PASSWORD, page, 'the event recorder')

    def test_the_audit_trail_redacts_both_password_columns(self):
        user_id = self.add_user()
        self.reset(user_id, NEW_PASSWORD)
        with self.app.app_context():
            token = db.session.get(HDCUser, user_id).password_vault
            changes = [json.loads(row.changed_fields) for row in UserActivity.query.filter_by(
                entity_type='hdc_user', entity_id=str(user_id), event_type='update')]
            redacted = {'old': '[redacted]', 'new': '[redacted]'}
            self.assertTrue(any(change.get('password_vault') == redacted for change in changes), changes)
            self.assertTrue(any(change.get('password_hash') == redacted for change in changes), changes)
            everything = ' '.join(str(row.changed_fields) + str(row.summary) for row in UserActivity.query)
            self.assertNotIn(token, everything)
            self.assertNotIn(NEW_PASSWORD, everything)

    def test_no_plain_text_password_is_ever_written_to_the_database(self):
        user_id = self.add_user()
        self.reset(user_id, NEW_PASSWORD)
        self.reveal(user_id)
        dump = self.database_dump()
        for secret in (PASSWORD, NEW_PASSWORD):
            self.assert_absent(secret, dump, 'the database file')
        token = self.user_row('ali')['vault']
        holders = [line for line in dump.splitlines() if token in line]
        self.assertTrue(holders)
        # The encrypted copy lives in the user row only - not in logs or audit tables.
        self.assertTrue(all(line.startswith('INSERT INTO "hdc_user"') for line in holders), holders)

    # -- degraded servers ------------------------------------------------------------

    def test_a_server_without_the_crypto_library_keeps_working_and_says_why(self):
        with without_crypto_library():
            user_id = self.add_user()                           # still created
            row = self.user_row('ali')
            self.assertIsNone(row['vault'])                     # nothing stored, never plain text
            self.assertTrue(check_password_hash(row['hash'], PASSWORD))
            self.client_for('ali', PASSWORD)                    # logins are unaffected
            page = html.unescape(self.users_page())
            self.assertIn('Password viewing is off on this server', page)
            self.assertIn('The "cryptography" package is not installed', page)
            self.assertIn('Unavailable', self.password_cell(page, 'ali'))
            data = self.reveal(user_id, expected=409).get_json()
            self.assertIn('unavailable', data['message'])
        # Once the problem is fixed, the next password set is viewable again.
        self.reset(user_id, NEW_PASSWORD)
        self.assertEqual(self.reveal(user_id).get_json()['password'], NEW_PASSWORD)

    def test_resetting_while_the_vault_is_down_drops_the_old_copy(self):
        user_id = self.add_user()
        self.assertTrue(self.user_row('ali')['vault'])
        with without_crypto_library():
            self.reset(user_id, NEW_PASSWORD)
        self.assertIsNone(self.user_row('ali')['vault'])
        self.reveal(user_id, expected=409)
        self.client_for('ali', NEW_PASSWORD)

    def test_a_copy_made_under_another_key_reads_as_unavailable_and_nothing_breaks(self):
        user_id = self.add_user()
        self.app.config['HDC_PASSWORD_VAULT_KEY'] = 'rotated-vault-key'
        data = self.reveal(user_id, expected=409).get_json()
        self.assertNotIn('password', data)
        self.assertEqual(self.client.get('/hdc/users').status_code, 200)
        self.client_for('ali', PASSWORD)                        # key rotation never affects logins
        self.reset(user_id, NEW_PASSWORD)                       # re-saved under the new key
        self.assertEqual(self.reveal(user_id).get_json()['password'], NEW_PASSWORD)


if __name__ == '__main__':
    unittest.main()
