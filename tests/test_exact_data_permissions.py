"""Exact access is deny-by-default, non-inheriting and enforced on the server."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from flask import jsonify, request
from flask_login import current_user
from sqlalchemy import func, select, text
from sqlalchemy.orm import aliased, selectinload
from werkzeug.datastructures import MultiDict
from werkzeug.security import generate_password_hash

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction, Expense, OwnerPayment
from hdc.models.auth import HDCUser
from hdc.models.materials import Delivery, MaterialV2, PurchaseV2, Supplier, UsageLogV2
from hdc.models.projects import Project, Stage, StageDrawing
from hdc.models.workforce import TimeEntry, Worker, WorkerRate
from hdc.models.cashflow import CashDayAccountPosition, CashDayLock
from hdc.services.record_permissions import integrity_query, parse_record_permissions, record_allowed, record_catalog, record_models


class ExactDataPermissionsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-exact-access-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp.name, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp.name, 'TESTING': True,
        })
        self.csrf = 'exact-data-test-csrf'
        self.client = self.app.test_client()
        with self.app.app_context():
            self.admin_id = HDCUser.query.filter_by(username='admin').one().id
            user = HDCUser(username='restricted', role='staff',
                           password_hash=generate_password_hash('Staff@1234'), record_scope_enabled=True)
            first = Project(name='ONLY chosen project', project_code='ACL-1', client='Chosen client',
                            owner_lump_sum=1234567, contract_type='lump_sum')
            other = Project(name='PRIVATE other project', project_code='ACL-2')
            empty = Project(name='Allowed project without stages', project_code='ACL-3')
            worker = Worker(name='PRIVATE worker one', worker_code='ACL-W1', role_type='Mason')
            second_worker = Worker(name='PRIVATE worker two', worker_code='ACL-W2', role_type='Mason')
            material = MaterialV2(name='ONLY material one', unit='KG')
            other_material = MaterialV2(name='PRIVATE material two', unit='KG')
            account = Account(name='ONLY account', type='cash', opening_balance=10000)
            db.session.add_all([user, first, other, empty, worker, second_worker, material, other_material, account])
            db.session.flush()
            stage = Stage(project_id=first.id, name='PRIVATE stage one')
            another_stage = Stage(project_id=first.id, name='PRIVATE stage two')
            db.session.add_all([stage, another_stage])
            db.session.flush()
            entry = TimeEntry(worker_id=worker.id, project_id=first.id, stage_id=stage.id,
                              check_in=datetime(2026, 9, 28, 8), check_out=datetime(2026, 9, 28, 16),
                              hours=8, wage_calculated=1111, legacy_calc=False)
            another_entry = TimeEntry(worker_id=second_worker.id, project_id=first.id, stage_id=another_stage.id,
                                      check_in=datetime(2026, 9, 29, 8), check_out=datetime(2026, 9, 29, 15),
                                      hours=7, wage_calculated=98765, legacy_calc=False)
            expense = Expense(project_id=first.id, stage_id=stage.id,
                              amount=543210, remarks='PRIVATE expense')
            receipt = OwnerPayment(project_id=first.id, amount=456789, remarks='PRIVATE owner receipt')
            db.session.add_all([entry, another_entry, expense, receipt])
            db.session.commit()
            self.user_id = user.id
            self.project_id = first.id
            self.other_project_id = other.id
            self.empty_project_id = empty.id
            self.stage_id = stage.id
            self.other_stage_id = another_stage.id
            self.worker_id = worker.id
            self.other_worker_id = second_worker.id
            self.entry_id = entry.id
            self.other_entry_id = another_entry.id
            self.expense_id = expense.id
            self.material_id = material.id
            self.other_material_id = other_material.id
            self.account_id = account.id
        self.configure(pages={'projects': (True, False), 'project_detail': (True, False)},
                       records={'hdc_project': {'read': [self.project_id]}})
        self.sign_in(self.user_id)

        # Test-only known lookup bucket: exercise ORM boundaries independently
        # of any particular production handler's implementation.
        @self.app.route('/hdc/api/exact-test', methods=['GET', 'POST'])
        def probe():
            action = request.values.get('action', 'read')
            if action == 'read':
                project_alias = aliased(Project)
                projects = Project.query.options(selectinload(Project.stages)).order_by(Project.id).all()
                return jsonify(projects=[{'id': p.id, 'name': p.name, 'stages': [s.id for s in p.stages]} for p in projects],
                               alias_ids=[p.id for p in db.session.query(project_alias).all()],
                               counts={table: model.query.count() for table, model in record_models().items()},
                               expense_total=db.session.query(func.coalesce(func.sum(Expense.amount), 0)).scalar(),
                               worker_ids=[w.id for w in Worker.query.all()])
            if action == 'raw':
                db.session.execute(text('SELECT name FROM hdc_project')).all()
            elif action == 'core':
                db.session.execute(select(Project.__table__)).all()
            elif action == 'bulk':
                Project.query.update({'location': 'BULK forbidden'})
            elif action == 'write':
                row = db.session.get(Project, int(request.form['id']))
                row.location = 'UPDATED selected project'
                db.session.commit()
            elif action == 'reassign':
                stage = db.session.get(Stage, int(request.form['id']))
                stage.project_id = int(request.form['value'])
                db.session.commit()
            elif action == 'change-pk':
                row = db.session.get(Project, int(request.form['id']))
                row.id = int(request.form['value'])
                db.session.commit()
            elif action == 'delete-object':
                row = db.session.get(MaterialV2, int(request.form['id']))
                db.session.delete(row)
                db.session.commit()
            elif action == 'void-object':
                row = db.session.get(MaterialV2, int(request.form['id']))
                row.is_void = True
                db.session.commit()
            elif action == 'partial':
                project = db.session.get(Project, self.project_id)
                project.location = 'MUST ROLL BACK'
                material = db.session.get(MaterialV2, self.material_id)
                material.name = 'DENIED SECOND MUTATION'
                db.session.commit()
            elif action == 'overdraft':
                from hdc.services.accounts import _account_balance_map, _check_overdraft_block
                ok, message = _check_overdraft_block([{'from_account_id': self.account_id, 'amount': 2000}])
                return jsonify(allowed=ok, message=message, visible_balance=_account_balance_map().get(self.account_id))
            elif action == 'integrity-entity':
                integrity_query(Project.query).all()
            elif action == 'integrity-alias':
                integrity_query(db.session.query(aliased(Project))).all()
            elif action == 'integrity-bulk':
                integrity_query(Project.query).update({'location': 'NOT ALLOWED'})
            elif action == 'delete-project':
                db.session.delete(db.session.get(Project, int(request.form['id'])))
                db.session.commit()
            elif action == 'change-expense':
                row = db.session.get(Expense, self.expense_id)
                row.amount = float(row.amount) + 1
                if request.form.get('sync'):
                    from hdc.services.accounts import _accounts_sync_expense_row
                    _accounts_sync_expense_row(row)
                db.session.commit()
            elif action == 'change-price':
                row = db.session.get(Project, self.project_id)
                row.owner_lump_sum += 1
                db.session.commit()
            elif action == 'create-journal':
                row = AccountTransaction(date=datetime(2026, 9, 28).date(), type='party_payment',
                    amount=100, category='expense', from_account_id=self.account_id, executed_by_account_id=self.account_id,
                    related_entity_type=request.form.get('actor_type'),
                    related_entity_id=int(request.form['actor_id']) if request.form.get('actor_id') else None)
                db.session.add(row)
                db.session.commit()
            elif action == 'auth-escalation':
                current_user.role = 'admin'
                current_user.record_scope_enabled = False
                with db.session.no_autoflush:
                    Project.query.all()
                db.session.commit()
            elif action == 'create-rollback':
                row = MaterialV2(name='Will not survive', unit='KG')
                db.session.add(row)
                db.session.flush()
                row_id = row.id
                db.session.rollback()
                return jsonify(allowed=record_allowed(current_user, 'hdc_material_v2', row_id))
            elif action == 'nested-rollback':
                parent = MaterialV2(name='Survives savepoint', unit='KG')
                db.session.add(parent)
                db.session.flush()
                with db.session.begin_nested() as savepoint:
                    rolled = MaterialV2(name='Rolled back savepoint', unit='KG')
                    db.session.add(rolled)
                    db.session.flush()
                    rolled_id = rolled.id
                    savepoint.rollback()
                parent_id = parent.id
                return jsonify(parent_allowed=record_allowed(current_user, 'hdc_material_v2', parent_id),
                               rolled_allowed=record_allowed(current_user, 'hdc_material_v2', rolled_id),
                               visible=[row.id for row in MaterialV2.query.all()])
            elif action == 'refresh':
                row = db.session.get(Project, self.project_id)
                db.session.expire(row)
                return jsonify(name=row.name)
            elif action == 'change-time':
                row = db.session.get(TimeEntry, self.entry_id)
                row.hours += 1
                db.session.commit()
            return jsonify(ok=True)

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

    def configure(self, *, pages=None, records=None, stage_scope=None):
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            if pages is not None:
                user.permissions_json = json.dumps({key: {'read': read, 'write': write}
                                                    for key, (read, write) in pages.items()})
            if records is not None:
                user.record_permissions_json = json.dumps(records)
            if stage_scope is not None:
                user.stage_scope_enabled = True
                user.allowed_stage_ids_json = json.dumps(stage_scope)
            db.session.commit()

    def allow_probe(self, write=True, records=None):
        self.configure(pages={'system_lookups': (True, write)}, records=records)

    def post(self, path, data=None):
        return self.client.post(path, data={'_csrf_token': self.csrf, **(data or {})})

    def test_one_project_visible_other_projects_and_subsections_denied(self):
        response = self.client.get('/hdc/projects')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('ONLY chosen project', body)
        self.assertNotIn('PRIVATE other project', body)
        self.assertNotIn('Contract Value', body)
        self.assertIn('href="/hdc/projects"', body)  # sidebar uses path catalog for every link
        detail = self.client.get(f'/hdc/projects/{self.project_id}')
        self.assertEqual(detail.status_code, 200)
        body = detail.get_data(as_text=True)
        for forbidden in ('PRIVATE stage one', 'PRIVATE worker one', 'PRIVATE owner receipt',
                          'Project Stages', 'Recent Time Entries', 'Owner Payments', 'Financial Breakdown'):
            self.assertNotIn(forbidden, body)
        for path in (f'/hdc/projects/{self.other_project_id}', '/hdc/stages', '/hdc/timekeeping', '/hdc/payroll'):
            self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_project_without_stages_can_be_assigned_independently(self):
        self.configure(records={'hdc_project': {'read': [self.empty_project_id]}})
        self.assertEqual(self.client.get(f'/hdc/projects/{self.empty_project_id}').status_code, 200)
        body = self.client.get('/hdc/projects').get_data(as_text=True)
        self.assertIn('Allowed project without stages', body)
        self.assertNotIn('ONLY chosen project', body)

    def test_data_grant_never_opens_a_blocked_page(self):
        self.configure(pages={'projects': (True, False)})
        self.assertEqual(self.client.get(f'/hdc/projects/{self.project_id}').status_code, 403)

    def test_page_grant_never_opens_unselected_records_or_parent_children(self):
        self.configure(pages={'projects': (True, False), 'project_detail': (True, False),
                              'stages': (True, False), 'timekeeping': (True, False),
                              'project_receipts': (True, False)})
        body = self.client.get(f'/hdc/projects/{self.project_id}').get_data(as_text=True)
        self.assertNotIn('PRIVATE stage one', body)
        self.assertNotIn('PRIVATE owner receipt', body)
        self.assertNotIn('98765', body)

    def test_financial_summary_is_a_separate_subsection(self):
        self.configure(pages={'projects': (True, False), 'project_detail': (True, False),
                              'project_financials': (True, False)})
        body = self.client.get(f'/hdc/projects/{self.project_id}').get_data(as_text=True)
        self.assertIn('Financial Breakdown', body)
        self.assertIn('1,234,567', body)
        self.assertNotIn('543,210', body)
        self.assertNotIn('456,789', body)

    def test_queries_aliases_aggregates_and_relationships_are_scoped(self):
        self.allow_probe()
        payload = self.client.get('/hdc/api/exact-test').get_json()
        self.assertEqual(payload['alias_ids'], [self.project_id])
        self.assertEqual(payload['projects'], [{'id': self.project_id, 'name': 'ONLY chosen project', 'stages': []}])
        self.assertEqual(payload['expense_total'], 0)
        self.assertEqual(payload['worker_ids'], [])
        for table, count in payload['counts'].items():
            self.assertEqual(count, 1 if table == 'hdc_project' else 0, table)

    def test_stage_grant_does_not_implicitly_grant_its_project(self):
        self.allow_probe()
        self.configure(records={'hdc_stage': {'read': [self.stage_id]}})
        payload = self.client.get('/hdc/api/exact-test').get_json()
        self.assertEqual(payload['counts']['hdc_stage'], 1)
        self.assertEqual(payload['projects'], [])
        self.assertEqual(payload['counts']['hdc_project'], 0)

    def test_one_attendance_record_readable_without_parent_names_or_other_entries(self):
        self.configure(pages={'timekeeping': (True, False)}, records={'hdc_time_entry': {'read': [self.entry_id]}})
        response = self.client.get('/hdc/timekeeping')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('1,111', body)
        self.assertIn(f'Worker #{self.worker_id}', body)
        self.assertNotIn('PRIVATE worker one', body)
        self.assertNotIn('ONLY chosen project', body)
        self.assertNotIn('98,765', body)

    def test_empty_or_malformed_data_map_denies_every_record(self):
        self.allow_probe()
        for raw in ('{}', '[]', 'broken json', '{"hdc_project":{"read":"all"}}'):
            with self.subTest(raw=raw):
                with self.app.app_context():
                    db.session.get(HDCUser, self.user_id).record_permissions_json = raw
                    db.session.commit()
                payload = self.client.get('/hdc/api/exact-test').get_json()
                self.assertEqual(payload['projects'], [])
                self.assertTrue(all(value == 0 for value in payload['counts'].values()))

    def test_strict_mode_without_page_map_does_not_restore_role_defaults(self):
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            user.permissions_json = None
            user.role = 'accountant'
            db.session.commit()
        self.assertEqual(self.client.get('/hdc/projects').status_code, 403)
        self.assertEqual(self.client.get('/hdc/accounts').status_code, 403)
        self.assertEqual(self.client.get('/hdc/access').status_code, 200)

    def test_record_read_does_not_allow_edit_even_on_writable_page(self):
        self.configure(pages={'project_detail': (True, True)})
        response = self.post(f'/hdc/projects/{self.project_id}/edit', {'location': 'DENIED'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(f'/hdc/projects/{self.project_id}/edit').status_code, 403)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Project, self.project_id).location)

    def test_page_write_is_required_in_addition_to_record_edit(self):
        self.configure(records={'hdc_project': {'write': [self.project_id]}})
        self.assertEqual(self.post(f'/hdc/projects/{self.project_id}/edit', {'location': 'DENIED'}).status_code, 403)

    def test_selected_project_edit_works_but_does_not_change_financial_fields(self):
        self.configure(pages={'project_detail': (True, True)},
                       records={'hdc_project': {'write': [self.project_id]}})
        self.assertEqual(self.post(f'/hdc/projects/{self.project_id}/edit', {'location': 'Permitted edit'}).status_code, 302)
        self.assertEqual(self.post(f'/hdc/projects/{self.project_id}/edit', {'owner_rate': '99'}).status_code, 403)
        with self.app.app_context():
            project = db.session.get(Project, self.project_id)
            self.assertEqual(project.location, 'Permitted edit')
            self.assertEqual(project.owner_lump_sum, 1234567)

    def test_identity_map_and_non_route_mutations_still_require_edit(self):
        self.allow_probe()
        response = self.post('/hdc/api/exact-test', {'action': 'write', 'id': self.project_id})
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Project, self.project_id).location)

    def test_partial_multi_record_changes_are_rolled_back(self):
        self.allow_probe()
        self.configure(records={'hdc_project': {'write': [self.project_id]}, 'hdc_material_v2': {'read': [self.material_id]}})
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'partial'}).status_code, 403)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Project, self.project_id).location)
            self.assertEqual(db.session.get(MaterialV2, self.material_id).name, 'ONLY material one')

    def test_record_cannot_be_reassigned_to_an_unselected_parent(self):
        self.allow_probe()
        self.configure(records={'hdc_project': {'read': [self.project_id]}, 'hdc_stage': {'write': [self.stage_id]}})
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'reassign', 'id': self.stage_id,
                                                       'value': self.other_project_id}).status_code, 403)
        with self.app.app_context():
            self.assertEqual(db.session.get(Stage, self.stage_id).project_id, self.project_id)

    def test_primary_key_cannot_be_changed_to_expand_a_grant(self):
        self.allow_probe()
        self.configure(records={'hdc_project': {'write': [self.project_id]}})
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'change-pk', 'id': self.project_id, 'value': 9999}).status_code, 403)

    def test_delete_and_void_are_not_implied_by_edit(self):
        self.allow_probe()
        self.configure(records={'hdc_material_v2': {'write': [self.material_id]}})
        for action in ('delete-object', 'void-object'):
            self.assertEqual(self.post('/hdc/api/exact-test', {'action': action, 'id': self.material_id}).status_code, 403)
        with self.app.app_context():
            self.assertFalse(db.session.get(MaterialV2, self.material_id).is_void)

    def test_delete_grant_is_independent_of_edit(self):
        self.configure(pages={'purchase_materials': (True, True)},
                       records={'hdc_material_v2': {'delete': [self.material_id]}})
        path = f'/api/v2/purchase/materials/{self.material_id}'
        response = self.client.put(path, json={'_csrf_token': self.csrf, 'name': 'DENIED edit'})
        self.assertEqual(response.status_code, 403)
        response = self.client.delete(path, json={'_csrf_token': self.csrf})
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.assertTrue(db.session.get(MaterialV2, self.material_id).is_void)
            self.assertFalse(db.session.get(MaterialV2, self.other_material_id).is_void)

    def test_single_material_api_returns_only_that_record(self):
        self.configure(pages={'purchase_materials': (True, False)},
                       records={'hdc_material_v2': {'read': [self.material_id]}})
        response = self.client.get('/api/v2/purchase/materials')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.get_json()['items']], [self.material_id])

    def test_new_records_blocked_unless_create_is_explicit(self):
        self.configure(pages={'purchase_materials': (True, True)}, records={})
        data = {'_csrf_token': self.csrf, 'name': 'Created with permission', 'unit': 'KG'}
        response = self.client.post('/api/v2/purchase/materials', json=data)
        self.assertEqual(response.status_code, 403)
        self.configure(records={'hdc_material_v2': {'create': True}})
        response = self.client.post('/api/v2/purchase/materials', json=data)
        self.assertEqual(response.status_code, 200)
        # Create does not silently assign every newly created/existing row for
        # later requests. The administrator must choose its ID explicitly.
        self.assertEqual(self.client.get('/api/v2/purchase/materials').get_json()['items'], [])

    def test_form_query_json_and_nested_refs_cannot_use_unselected_data(self):
        self.allow_probe()
        for response in (
            self.client.get('/hdc/api/exact-test', query_string={'project_id': self.other_project_id}),
            self.post('/hdc/api/exact-test', {'stage_id': self.other_stage_id}),
            self.client.post('/hdc/api/exact-test', json={'_csrf_token': self.csrf, 'items': [{'project_id': self.other_project_id}]}),
            self.post('/hdc/api/exact-test', {'entries_json': json.dumps([{'stage_id': self.other_stage_id}])}),
        ):
            self.assertEqual(response.status_code, 403)

    def test_invalid_ids_fail_closed_without_500(self):
        self.allow_probe()
        for value in ('-1', 'broken', '1.5', '99999999999999999999999'):
            self.assertEqual(self.client.get('/hdc/api/exact-test', query_string={'stage_id': value}).status_code, 403)

    def test_optional_empty_references_are_allowed(self):
        self.allow_probe()
        response = self.post('/hdc/api/exact-test', {'project_id': '', 'stage_id': '', 'account_id': '0'})
        self.assertEqual(response.status_code, 200)

    def test_raw_core_and_bulk_paths_cannot_bypass_the_record_guard(self):
        self.allow_probe()
        for action in ('raw', 'core', 'bulk'):
            self.assertEqual(self.post('/hdc/api/exact-test', {'action': action}).status_code, 403)

    def test_stage_scope_remains_an_additional_ceiling(self):
        self.configure(pages={'stages': (True, False), 'stage_ledger': (True, False)},
                       records={'hdc_project': {'read': [self.project_id]}, 'hdc_stage': {'read': [self.stage_id, self.other_stage_id]}},
                       stage_scope=[self.stage_id])
        self.assertEqual(self.client.get(f'/hdc/stage/{self.stage_id}/ledger').status_code, 200)
        self.assertEqual(self.client.get(f'/hdc/stage/{self.other_stage_id}/ledger').status_code, 403)

    def test_trailing_slashes_do_not_bypass_or_break_granted_pages(self):
        self.assertEqual(self.client.get('/hdc/projects/').status_code, 200)
        self.assertEqual(self.client.get(f'/hdc/projects/{self.other_project_id}/').status_code, 403)

    def test_admin_can_save_exact_selections_and_invalid_rows_are_discarded(self):
        self.sign_in(self.admin_id)
        response = self.post('/hdc/users', MultiDict([
            ('action', 'configure_permissions'), ('user_id', str(self.user_id)), ('role', 'staff'),
            ('record_scope_enabled', '1'), ('read_projects', '1'),
            ('record_write_hdc_project', str(self.project_id)), ('record_read_hdc_project', '999999'),
            ('record_delete_hdc_material_v2', str(self.material_id)), ('record_create_hdc_expense', '1'),
        ]))
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            self.assertTrue(user.record_scope_enabled)
            self.assertTrue(user.permissions_json)  # enabled even without custom checkbox
            grants = json.loads(user.record_permissions_json)
            self.assertEqual(grants['hdc_project']['read'], [self.project_id])
            self.assertEqual(grants['hdc_project']['write'], [self.project_id])
            self.assertEqual(grants['hdc_material_v2']['delete'], [self.material_id])
            self.assertTrue(grants['hdc_expense']['create'])
        body = self.client.get('/hdc/users').get_data(as_text=True)
        self.assertIn('Only selected data (strict)', body)
        self.assertIn('ONLY chosen project', body)
        self.assertIn('record_write_hdc_project', body)
        self.assertIn('user_access.js', body)

    def test_picker_is_paginated_searchable_and_admin_only(self):
        self.assertEqual(self.client.get('/hdc/users/access-data?resource=hdc_project').status_code, 403)
        self.sign_in(self.admin_id)
        response = self.client.get('/hdc/users/access-data', query_string={'resource': 'hdc_project', 'search': 'ONLY'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.get_json()['records']], [str(self.project_id)])
        for resource in ('hdc_user', 'hdc_user_activity', 'not_a_table'):
            self.assertEqual(self.client.get('/hdc/users/access-data', query_string={'resource': resource}).status_code, 400)

    def test_non_admin_cannot_delegate_administration_even_with_a_page_grant(self):
        self.configure(pages={'users': (True, True), 'settings': (True, True), 'event_recorder': (True, True)})
        for path in ('/hdc/users', '/hdc/settings', '/hdc/event-recorder'):
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_login_lands_on_the_first_granted_page_not_denied_dashboard(self):
        with self.client.session_transaction() as session:
            session.pop('_user_id', None)
        response = self.post('/hdc/login', {'username': 'restricted', 'password': 'Staff@1234'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/hdc/projects')
        self.assertEqual(self.client.get('/').headers['Location'], '/hdc/projects')
        self.assertEqual(self.post('/hdc/logout').status_code, 302)

    def test_detail_only_login_and_empty_access_have_safe_landing(self):
        self.configure(pages={'project_detail': (True, False)})
        self.assertEqual(self.client.get('/hdc/login').headers['Location'], f'/hdc/projects/{self.project_id}')
        self.configure(pages={})
        self.assertEqual(self.client.get('/hdc/login').headers['Location'], '/hdc/access')
        self.assertEqual(self.client.get('/hdc/access').status_code, 200)

    def test_admin_and_unconfigured_legacy_users_remain_unrestricted(self):
        self.sign_in(self.admin_id)
        self.assertEqual(self.client.get(f'/hdc/projects/{self.other_project_id}').status_code, 200)
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            user.record_scope_enabled = False
            user.permissions_json = None
            db.session.commit()
        self.sign_in(self.user_id)
        self.assertEqual(self.client.get(f'/hdc/projects/{self.other_project_id}').status_code, 200)

    def test_catalog_covers_every_business_model_but_not_auth(self):
        with self.app.app_context():
            catalog = {row['id'] for row in record_catalog()}
            self.assertEqual(catalog, set(record_models()))
            self.assertIn('hdc_tool_purchase', catalog)
            self.assertIn('hdc_tool_scrap', catalog)
            self.assertNotIn('hdc_user', catalog)

    def test_parsing_never_treats_truthy_strings_or_booleans_as_grants(self):
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            user.record_permissions_json = json.dumps({'hdc_project': {'read': [True, -1, 1.5], 'create': 'true'}})
            self.assertEqual(parse_record_permissions(user)['hdc_project'], {'read': [], 'write': [], 'delete': [], 'create': False})


    def add_hidden_transaction(self, *, source=False):
        with self.app.app_context():
            if source:
                db.session.get(Account, self.account_id).opening_balance = 10000000
            txn = AccountTransaction(date=datetime(2026, 9, 28).date(), type='party_payment',
                amount=9000, category='expense', from_account_id=self.account_id, executed_by_account_id=self.account_id,
                source_type='expense:direct' if source else None, source_id=self.expense_id if source else None)
            db.session.add(txn)
            db.session.commit()
            return txn.id

    def add_stock(self):
        with self.app.app_context():
            supplier = Supplier(name='PRIVATE supplier', status='active')
            db.session.add(supplier)
            db.session.flush()
            purchase = PurchaseV2(supplier_id=supplier.id, material_id=self.material_id,
                                 quantity=10, unit_price=10, total_amount=100)
            db.session.add(purchase)
            db.session.flush()
            delivery = Delivery(purchase_id=purchase.id, material_id=self.material_id,
                                project_id=self.project_id, stage_id=self.stage_id, quantity=10)
            usage = UsageLogV2(purchase_id=purchase.id, material_id=self.material_id,
                              project_id=self.project_id, stage_id=self.stage_id, quantity=8, cost=80)
            db.session.add_all([delivery, usage])
            db.session.commit()
            return supplier.id, purchase.id, delivery.id, usage.id

    def test_hidden_transactions_still_prevent_overdraft_without_disclosing_balance(self):
        self.add_hidden_transaction()
        self.allow_probe(records={'hdc_account': {'read': [self.account_id]}})
        result = self.post('/hdc/api/exact-test', {'action': 'overdraft'}).get_json()
        self.assertFalse(result['allowed'])
        self.assertEqual(result['visible_balance'], 10000)
        self.assertEqual(result['message'], 'Insufficient balance. This change is blocked.')

    def test_internal_integrity_flag_cannot_load_entities_aliases_or_mutate(self):
        self.allow_probe()
        for action in ('integrity-entity', 'integrity-alias', 'integrity-bulk'):
            self.assertEqual(self.post('/hdc/api/exact-test', {'action': action}).status_code, 403)

    def test_parent_delete_does_not_silently_omit_unassigned_children(self):
        self.allow_probe(records={'hdc_project': {'delete': [self.project_id]}})
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'delete-project', 'id': self.project_id}).status_code, 403)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Project, self.project_id))
            self.assertIsNotNone(db.session.get(Stage, self.stage_id))

    def test_empty_parent_delete_is_allowed_without_other_record_grants(self):
        self.allow_probe(records={'hdc_project': {'delete': [self.empty_project_id]}})
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'delete-project', 'id': self.empty_project_id}).status_code, 200)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Project, self.empty_project_id))
            self.assertIsNotNone(db.session.get(Project, self.project_id))

    def test_expense_edit_cannot_lose_or_duplicate_a_hidden_bookkeeping_mirror(self):
        txn_id = self.add_hidden_transaction(source=True)
        self.allow_probe(records={'hdc_expense': {'write': [self.expense_id]}})
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'change-expense'}).status_code, 403)
        with self.app.app_context():
            self.assertEqual(db.session.get(Expense, self.expense_id).amount, 543210)
            self.assertEqual(db.session.get(AccountTransaction, txn_id).amount, 9000)

    def test_readable_mirror_needs_its_own_edit_grant_when_synced(self):
        txn_id = self.add_hidden_transaction(source=True)
        self.allow_probe(records={'hdc_expense': {'write': [self.expense_id]},
                                  'hdc_account_txn': {'read': [txn_id]}})
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'change-expense', 'sync': '1'}).status_code, 403)
        self.allow_probe(records={'hdc_expense': {'write': [self.expense_id]},
                                  'hdc_account_txn': {'write': [txn_id]},
                                  'hdc_project': {'read': [self.project_id]}, 'hdc_stage': {'read': [self.stage_id]}})
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'change-expense', 'sync': '1'}).status_code, 200)

    def test_financial_field_writes_cannot_bypass_the_finance_page_off_route(self):
        self.allow_probe(records={'hdc_project': {'write': [self.project_id]}})
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'change-price'}).status_code, 403)
        with self.app.app_context():
            self.assertEqual(db.session.get(Project, self.project_id).owner_lump_sum, 1234567)

    def test_related_entity_references_cannot_link_to_unassigned_records(self):
        self.allow_probe(records={'hdc_account_txn': {'create': True}, 'hdc_account': {'read': [self.account_id]}})
        for body in ({'related_entity_type': 'worker', 'related_entity_id': self.other_worker_id},
                     {'related_entity_type': 'unknown', 'related_entity_id': self.worker_id},
                     {'source_type': 'expense', 'source_id': self.expense_id}):
            self.assertEqual(self.post('/hdc/api/exact-test', body).status_code, 403)
        # References synthesized by a handler are also checked at flush.
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'create-journal',
                         'actor_type': 'worker', 'actor_id': self.other_worker_id}).status_code, 403)

    def test_authorization_is_not_bypassed_by_mutating_the_auth_object(self):
        self.allow_probe()
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'auth-escalation'}).status_code, 403)
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            self.assertEqual(user.role, 'staff')
            self.assertTrue(user.record_scope_enabled)

    def test_create_rollback_clears_request_local_access(self):
        self.allow_probe(records={'hdc_material_v2': {'create': True}})
        response = self.post('/hdc/api/exact-test', {'action': 'create-rollback'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['allowed'])

    def test_nested_rollback_keeps_only_surviving_outer_creation_access(self):
        self.allow_probe(records={'hdc_material_v2': {'create': True}})
        response = self.post('/hdc/api/exact-test', {'action': 'nested-rollback'})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue(result['parent_allowed'])
        self.assertFalse(result['rolled_allowed'])
        self.assertEqual(len(result['visible']), 1)

    def test_selected_column_refresh_remains_scoped(self):
        self.allow_probe()
        response = self.post('/hdc/api/exact-test', {'action': 'refresh'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['name'], 'ONLY chosen project')

    def test_hidden_lock_still_blocks_ledger_creation(self):
        with self.app.app_context():
            db.session.add(CashDayLock(lock_date=datetime(2026, 9, 28).date(), locked_by='PRIVATE locker'))
            db.session.commit()
        self.allow_probe(records={'hdc_account': {'read': [self.account_id]}, 'hdc_account_txn': {'create': True}})
        response = self.post('/hdc/api/exact-test', {'action': 'create-journal'})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('PRIVATE locker', response.get_data(as_text=True))

    def test_hidden_usage_limits_new_usage_but_does_not_expose_full_totals(self):
        _, purchase_id, _, _ = self.add_stock()
        self.configure(pages={'purchase_usage': (True, True)}, records={
            'hdc_purchase_v2': {'read': [purchase_id]}, 'hdc_material_v2': {'read': [self.material_id]},
            'hdc_project': {'read': [self.project_id]}, 'hdc_stage': {'read': [self.stage_id]},
            'hdc_usage_log_v2': {'create': True}})
        data = {'_csrf_token': self.csrf, 'purchase_id': purchase_id, 'material_id': self.material_id,
                'project_id': self.project_id, 'stage_id': self.stage_id, 'quantity': 3}
        response = self.client.post('/api/v2/purchase/usage', json=data)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('2.00', response.get_json()['message'])
        data['quantity'] = 1
        self.assertEqual(self.client.post('/api/v2/purchase/usage', json=data).status_code, 200)
        self.assertEqual(self.client.get('/api/v2/purchase/usage').get_json()['items'], [])

    def test_hidden_delivery_still_prevents_overdelivery(self):
        _, purchase_id, _, _ = self.add_stock()
        self.configure(pages={'purchase_deliveries': (True, True)}, records={
            'hdc_purchase_v2': {'read': [purchase_id]}, 'hdc_project': {'read': [self.project_id]},
            'hdc_stage': {'read': [self.stage_id]}, 'hdc_material_v2': {'read': [self.material_id]},
            'hdc_delivery': {'create': True}})
        response = self.client.post('/api/v2/purchase/deliveries', json={
            '_csrf_token': self.csrf, 'purchase_id': purchase_id, 'project_id': self.project_id,
            'stage_id': self.stage_id, 'quantity': 1})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('0.00', response.get_json()['message'])

    def test_hidden_material_transactions_still_prevent_deletion(self):
        self.add_stock()
        self.configure(pages={'purchase_materials': (True, True)},
                       records={'hdc_material_v2': {'delete': [self.material_id]}})
        response = self.client.delete(f'/api/v2/purchase/materials/{self.material_id}',
                                      json={'_csrf_token': self.csrf})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('PRIVATE supplier', response.get_data(as_text=True))
        with self.app.app_context():
            self.assertFalse(db.session.get(MaterialV2, self.material_id).is_void)

    def test_hidden_usage_still_prevents_delivery_void(self):
        _, purchase_id, delivery_id, _ = self.add_stock()
        self.configure(pages={'purchase_deliveries': (True, True)}, records={
            'hdc_delivery': {'delete': [delivery_id]}, 'hdc_purchase_v2': {'read': [purchase_id]}})
        response = self.client.delete(f'/api/v2/purchase/deliveries/{delivery_id}',
                                      json={'_csrf_token': self.csrf})
        self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            self.assertFalse(db.session.get(Delivery, delivery_id).is_void)

    def test_attendance_buttons_and_parent_links_match_exact_grants(self):
        self.configure(pages={'timekeeping': (True, True), 'timekeeping_records': (True, True),
                              'worker_ledgers': (True, True)},
                       records={'hdc_time_entry': {'read': [self.entry_id]}})
        body = self.client.get('/hdc/timekeeping').get_data(as_text=True)
        self.assertNotIn(f'/hdc/timekeeping/{self.entry_id}/edit', body)
        self.assertNotIn(f'/hdc/timekeeping/{self.entry_id}/delete', body)
        self.assertNotIn(f'/hdc/workers/{self.worker_id}/ledger', body)

    def test_selected_stage_works_without_a_parent_project_grant(self):
        self.configure(pages={'stage_ledger': (True, False)}, records={'hdc_stage': {'read': [self.stage_id]}})
        response = self.client.get(f'/hdc/stage/{self.stage_id}/ledger')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn(f'Project #{self.project_id}', body)
        self.assertNotIn('ONLY chosen project', body)
        self.assertNotIn(f'href="/hdc/projects/{self.project_id}"', body)

    def test_readonly_account_view_does_not_create_financial_positions(self):
        self.configure(pages={'accounts_manage': (True, False)}, records={'hdc_account': {'read': [self.account_id]}})
        response = self.client.get('/hdc/accounts/manage')
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.assertEqual(CashDayAccountPosition.query.count(), 0)

    def test_editing_attendance_does_not_ignore_hidden_daily_siblings(self):
        with self.app.app_context():
            db.session.add(TimeEntry(worker_id=self.worker_id, project_id=self.project_id, stage_id=self.stage_id,
                check_in=datetime(2026, 9, 28, 16), check_out=datetime(2026, 9, 28, 17), hours=1))
            db.session.commit()
        self.allow_probe(records={'hdc_time_entry': {'write': [self.entry_id]}, 'hdc_worker': {'read': [self.worker_id]}})
        self.assertEqual(self.post('/hdc/api/exact-test', {'action': 'change-time'}).status_code, 403)

    def test_legacy_custom_project_maps_preserve_existing_subsections(self):
        with self.app.app_context():
            user = db.session.get(HDCUser, self.user_id)
            user.record_scope_enabled = False
            user.permissions_json = json.dumps({'project_detail': {'read': True, 'write': False}})
            db.session.commit()
        body = self.client.get(f'/hdc/projects/{self.project_id}').get_data(as_text=True)
        self.assertIn('1,234,567', body)
        self.assertIn('Owner Payments', body)

    def test_record_picker_preserves_signed_64_bit_ids_as_strings(self):
        large_id = 9007199254741017
        with self.app.app_context():
            db.session.add(MaterialV2(id=large_id, name='Large exact record ID', unit='KG'))
            db.session.commit()
        self.sign_in(self.admin_id)
        response = self.client.get('/hdc/users/access-data', query_string={
            'resource': 'hdc_material_v2', 'search': str(large_id)})
        self.assertEqual(response.get_json()['records'][0]['id'], str(large_id))

    def test_synced_source_edit_cannot_overdraw_an_unassigned_parent_account(self):
        txn_id = self.add_hidden_transaction(source=True)
        with self.app.app_context():
            db.session.get(Account, self.account_id).opening_balance = 10000
            db.session.commit()
        self.allow_probe(records={'hdc_expense': {'write': [self.expense_id]},
            'hdc_account_txn': {'write': [txn_id]}, 'hdc_project': {'read': [self.project_id]},
            'hdc_stage': {'read': [self.stage_id]}})
        response = self.post('/hdc/api/exact-test', {'action': 'change-expense', 'sync': '1'})
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            self.assertEqual(db.session.get(AccountTransaction, txn_id).amount, 9000)
            self.assertEqual(db.session.get(Expense, self.expense_id).amount, 543210)

    def test_drawing_view_and_delete_do_not_require_or_reveal_hidden_parents(self):
        from hdc.config import get_runtime_settings
        with self.app.app_context():
            drawing = StageDrawing(stage_id=self.stage_id, original_name='selected.pdf', stored_name='selected.pdf')
            db.session.add(drawing)
            db.session.commit()
            drawing_id = drawing.id
            path = os.path.join(get_runtime_settings().stage_drawings_dir, 'selected.pdf')
            with open(path, 'wb') as handle:
                handle.write(b'%PDF-1.4\nONLY selected file\n')
        self.configure(pages={'stage_drawings': (True, True)}, records={'hdc_stage_drawing': {'read': [drawing_id]}})
        response = self.client.get(f'/hdc/stage/drawing/{drawing_id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'ONLY selected file', response.data)
        self.assertEqual(self.post(f'/hdc/stage/drawing/{drawing_id}/delete').status_code, 403)
        self.assertTrue(os.path.exists(path))
        self.configure(records={'hdc_stage_drawing': {'delete': [drawing_id]}})
        response = self.post(f'/hdc/stage/drawing/{drawing_id}/delete')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(os.path.exists(path))
        with self.app.app_context():
            self.assertIsNone(db.session.get(StageDrawing, drawing_id))

    def test_new_worker_code_does_not_collide_with_hidden_namespace(self):
        with self.app.app_context():
            db.session.add(Worker(name='PRIVATE namespace worker', worker_code='HDC-WORKER-000008'))
            db.session.commit()
        self.configure(pages={'workers': (True, True)}, records={'hdc_worker': {'create': True}})
        response = self.client.get('/hdc/api/next_worker_code')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['code'], 'HDC-WORKER-000009')
        self.configure(records={})
        self.assertEqual(self.client.get('/hdc/api/next_worker_code').status_code, 403)
