"""Project access cascade: guided project → stage → reporting-section grants.

The cascade lives inside the strict record editor. Projects and stages keep
their exact record grants; the four reporting sections are stored under the
reserved `__report_sections__` key of record_permissions_json, keep their
report page buckets reachable, and act as an additional deny-by-default gate
for report views/exports once any section is stored.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from werkzeug.security import generate_password_hash
from werkzeug.datastructures import MultiDict

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.auth import HDCUser
from hdc.models.projects import Project, Stage
from hdc.models.workforce import TimeEntry, Worker
from hdc.services.record_permissions import (parse_record_permissions, parse_report_sections,
    record_permissions_from_form, report_section_allowed, report_section_page_grants)


class ProjectAccessCascadeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-cascade-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp.name, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp.name,
            'TESTING': True,
        })
        self.csrf = 'cascade-test-csrf'
        self.client = self.app.test_client()
        with self.app.app_context():
            self.admin_id = HDCUser.query.filter_by(username='admin').one().id
            user = HDCUser(username='cascade_user', role='staff',
                           password_hash=generate_password_hash('Staff@1234'),
                           record_scope_enabled=True)
            alpha = Project(name='CASCADE alpha project', project_code='CSC-A',
                            owner_lump_sum=5000, contract_type='lump_sum')
            beta = Project(name='CASCADE beta project', project_code='CSC-B')
            worker = Worker(name='CASCADE worker', worker_code='CSC-W1', role_type='Mason')
            db.session.add_all([user, alpha, beta, worker])
            db.session.flush()
            granted_stage = Stage(project_id=alpha.id, name='GRANTED-STAGE-ONE')
            hidden_stage = Stage(project_id=alpha.id, name='HIDDEN-STAGE-TWO')
            other_stage = Stage(project_id=beta.id, name='OTHER-PROJECT-STAGE')
            db.session.add_all([granted_stage, hidden_stage, other_stage])
            db.session.flush()
            db.session.commit()
            self.user_id = user.id
            self.project_id = alpha.id
            self.other_project_id = beta.id
            self.stage_id = granted_stage.id
            self.hidden_stage_id = hidden_stage.id
            self.other_stage_id = other_stage.id
        # Pages + records are granted; reporting sections decide the rest.
        self.configure(
            pages={'projects': (True, False), 'project_detail': (True, False),
                   'reports': (True, False), 'report_exports': (True, False),
                   'glance_report': (True, False)},
            records={'hdc_project': {'read': [self.project_id]},
                     'hdc_stage': {'read': [self.stage_id, self.hidden_stage_id]}},
            sections={self.project_id: {0: {'project_report': {'read': True},
                                            'report_exports': {'read': True}},
                                        self.stage_id: {'stage_report': {'read': True},
                                                        'glance_report': {'read': True, 'write': True}}}})
        self.sign_in(self.user_id)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.tmp.cleanup()

    def sign_in(self, uid):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(uid)
            session['_fresh'] = True
            session['_csrf_token'] = self.csrf

    def configure(self, *, pages=None, records=None, sections=None):
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            if pages is not None:
                user.permissions_json = json.dumps({key: {'read': read, 'write': write}
                                                    for key, (read, write) in pages.items()})
            if records is not None or sections is not None:
                if records is not None:
                    grants = dict(records)
                else:
                    grants = json.loads(user.record_permissions_json or '{}')
                if sections is not None:
                    grants['__report_sections__'] = sections
                user.record_permissions_json = json.dumps(grants)
            user.record_scope_enabled = True
            db.session.commit()

    def post(self, path, data=None):
        return self.client.post(path, data={'_csrf_token': self.csrf, **(data or {})})

    # ---- enforcement -------------------------------------------------------
    def test_project_report_requires_a_matching_section_grant(self):
        response = self.client.get('/hdc/reports',
                                   query_string={'section': 'project', 'project_id': self.project_id})
        self.assertEqual(response.status_code, 200)
        # Same project, section grants present but no project_report for it.
        self.configure(sections={self.project_id: {0: {'glance_report': {'read': True}}}})
        response = self.client.get('/hdc/reports',
                                   query_string={'section': 'project', 'project_id': self.project_id})
        self.assertEqual(response.status_code, 403)

    def test_project_report_grant_under_one_stage_covers_the_project(self):
        self.configure(sections={self.project_id: {self.stage_id: {'project_report': {'read': True}}}})
        response = self.client.get('/hdc/reports',
                                   query_string={'section': 'project', 'project_id': self.project_id})
        self.assertEqual(response.status_code, 200)
        response = self.client.get(f'/hdc/reports/project/{self.project_id}/csv')
        self.assertEqual(response.status_code, 200)

    def test_glance_report_section_gates_the_glance_page(self):
        response = self.client.get('/hdc/reports/glance')
        self.assertEqual(response.status_code, 200)
        self.configure(sections={self.project_id: {0: {'project_report': {'read': True}}}})
        response = self.client.get('/hdc/reports/glance')
        self.assertEqual(response.status_code, 403)

    def test_report_exports_section_gates_global_exports(self):
        response = self.client.get('/hdc/reports/export/profitability')
        self.assertEqual(response.status_code, 200)
        self.configure(sections={self.project_id: {0: {'project_report': {'read': True}}}})
        for path in ('/hdc/reports/export/profitability', '/hdc/reports/export/salary',
                     '/hdc/reports/export/materials'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403, path)

    def test_stage_report_gates_direct_urls_and_filters_rows(self):
        response = self.client.get('/hdc/reports',
                                   query_string={'section': 'stage', 'stage_id': self.stage_id})
        self.assertEqual(response.status_code, 200)
        # Stage is record-readable but has no stage_report section.
        response = self.client.get('/hdc/reports',
                                   query_string={'section': 'stage', 'stage_id': self.hidden_stage_id})
        self.assertEqual(response.status_code, 403)
        # Project-wide stage listing: only section-granted stages get rows.
        response = self.client.get('/hdc/reports',
                                   query_string={'section': 'stage', 'stage_project_id': self.project_id})
        body = response.get_data(as_text=True)
        stage_table = body.split('<th>Estimated</th>', 1)[1].split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        self.assertIn('GRANTED-STAGE-ONE', stage_table)
        self.assertNotIn('HIDDEN-STAGE-TWO', stage_table)

    def test_whole_project_section_entry_covers_every_stage_of_that_project(self):
        self.configure(sections={self.project_id: {0: {'stage_report': {'read': True}}}})
        response = self.client.get('/hdc/reports',
                                   query_string={'section': 'stage', 'stage_id': self.hidden_stage_id})
        self.assertEqual(response.status_code, 200)

    def test_write_grant_requires_the_write_flag(self):
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            self.assertTrue(report_section_allowed(
                user, 'stage_report', project_id=self.project_id, stage_id=self.stage_id,
                mode='read'))
            # Read-only stage_report must not imply write authority.
            self.assertFalse(report_section_allowed(
                user, 'stage_report', project_id=self.project_id, stage_id=self.stage_id,
                mode='write'))
            self.assertTrue(report_section_allowed(
                user, 'glance_report', mode='write'))  # setUp grants write on glance

    def test_legacy_strict_users_without_sections_keep_page_and_record_behavior(self):
        self.configure(sections=None)
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            user.record_permissions_json = json.dumps(
                {'hdc_project': {'read': [self.project_id]},
                 'hdc_stage': {'read': [self.stage_id, self.hidden_stage_id]}})
            db.session.commit()
        self.assertEqual(self.client.get('/hdc/reports/glance').status_code, 200)
        self.assertEqual(self.client.get('/hdc/reports/export/salary').status_code, 200)
        response = self.client.get('/hdc/reports',
                                   query_string={'section': 'project', 'project_id': self.project_id})
        self.assertEqual(response.status_code, 200)
        # Without any section stored the stage rows are page+record gated only.
        response = self.client.get('/hdc/reports',
                                   query_string={'section': 'stage', 'stage_id': self.hidden_stage_id})
        self.assertEqual(response.status_code, 200)

    def test_both_gates_apply_page_map_still_required(self):
        self.configure(pages={'projects': (True, False), 'project_detail': (True, False),
                              'glance_report': (True, False)})  # no 'reports' page
        response = self.client.get('/hdc/reports')
        self.assertEqual(response.status_code, 403)
        self.configure(pages={'projects': (True, False), 'project_detail': (True, False),
                              'reports': (True, False), 'report_exports': (True, False),
                              'glance_report': (True, False)})

    def test_ungranted_project_report_is_denied_even_with_page_and_record_access(self):
        # Record grant exists for the other project? No — only alpha is granted,
        # so record gate already blocks beta; the section gate must also bite
        # for a record-readable project without a report grant.
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            grants = json.loads(user.record_permissions_json)
            grants['hdc_project']['read'].append(self.other_project_id)
            user.record_permissions_json = json.dumps(grants)
            db.session.commit()
        response = self.client.get('/hdc/reports',
                                   query_string={'section': 'project', 'project_id': self.other_project_id})
        self.assertEqual(response.status_code, 403)

    # ---- storage & form round-trip ----------------------------------------
    def test_reserved_key_never_grants_rows(self):
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            parsed = parse_record_permissions(user)
            self.assertNotIn('__report_sections__', parsed)
            self.assertIn('hdc_project', parsed)
            self.assertIn(self.project_id, parsed['hdc_project']['read'])
            self.assertEqual(parse_report_sections(user)[self.project_id][0]['project_report'],
                             {'read': True, 'write': False})

    def test_report_section_page_grants_cover_report_buckets(self):
        pages = report_section_page_grants({'__report_sections__': {
            self.project_id: {0: {'project_report': {'read': True, 'write': False}},
                              self.stage_id: {'glance_report': {'read': True, 'write': True}}}}})
        self.assertTrue(pages['reports']['read'])
        self.assertTrue(pages['report_exports']['read'])
        self.assertTrue(pages['glance_report']['read'])
        self.assertTrue(pages['glance_report']['write'])

    def test_admin_saves_cascade_selections_and_discards_invalid_sections(self):
        self.sign_in(self.admin_id)
        response = self.post('/hdc/users', MultiDict([
            ('action', 'configure_permissions'), ('user_id', str(self.user_id)),
            ('role', 'staff'), ('record_scope_enabled', '1'),
            ('read_projects', '1'), ('read_reports', '1'),
            ('record_read_hdc_project', str(self.project_id)),
            ('record_read_hdc_stage', str(self.stage_id)),
            ('report_read_%d_0_project_report' % self.project_id, '1'),
            ('report_read_%d_%d_stage_report' % (self.project_id, self.stage_id), '1'),
            ('report_write_%d_%d_glance_report' % (self.project_id, self.stage_id), '1'),
            # Invalid: unknown project, unknown stage, unknown section key.
            ('report_read_999999_0_project_report', '1'),
            ('report_read_%d_999999_stage_report' % self.project_id, '1'),
            ('report_read_%d_0_not_a_section' % self.project_id, '1'),
        ]))
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            grants = json.loads(user.record_permissions_json)
            sections = grants['__report_sections__']
            self.assertEqual(list(sections), [str(self.project_id)])
            self.assertIn('0', sections[str(self.project_id)])
            self.assertIn(str(self.stage_id), sections[str(self.project_id)])
            self.assertNotIn('999999', json.dumps(sections))
            self.assertNotIn('not_a_section', json.dumps(sections))
            # Write on glance implies read; stage report stays read-only.
            glance = sections[str(self.project_id)][str(self.stage_id)]['glance_report']
            self.assertEqual(glance, {'read': True, 'write': True})
            self.assertEqual(sections[str(self.project_id)][str(self.stage_id)]['stage_report'],
                             {'read': True, 'write': False})
            # Saving sections keeps the report page buckets in the page map.
            pages = json.loads(user.permissions_json)
            self.assertTrue(pages['reports']['read'])
            self.assertTrue(pages['report_exports']['read'])
            self.assertTrue(pages['glance_report']['read'])

    def test_users_page_renders_the_cascade_for_saved_grants(self):
        self.sign_in(self.admin_id)
        body = self.client.get('/hdc/users').get_data(as_text=True)
        self.assertIn('data-record-cascade', body)
        self.assertIn('data-cascade-branch', body)
        self.assertIn('CASCADE alpha project', body)
        self.assertIn('GRANTED-STAGE-ONE', body)
        self.assertIn('report_read_%d_0_project_report' % self.project_id, body)
        self.assertIn('record_read_hdc_project', body)
        self.assertIn('record_read_hdc_stage', body)
        self.assertIn('All projects', body)

    def test_form_parsing_never_trusts_unchecked_or_foreign_fields(self):
        with self.app.app_context():
            form = MultiDict([
                ('report_read_%d_0_project_report' % self.project_id, '0'),  # unchecked value
                ('report_write_%d_%d_stage_report' % (self.project_id, self.stage_id), '1'),
            ])
            parsed = record_permissions_from_form(form)
            sections = parsed['__report_sections__'][self.project_id]
            self.assertNotIn(0, sections)  # value != '1' is absent → deny
            self.assertEqual(sections[self.stage_id]['stage_report'],
                             {'read': True, 'write': True})


if __name__ == '__main__':
    unittest.main()
