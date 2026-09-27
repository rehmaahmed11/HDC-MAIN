"""Multiple stage members must not replace each other or share progress."""
import test_subcontract_team_attendance as attendance_tests
import unittest
from hdc.extensions import db
from hdc.models.subcontract import Subcontractor
from hdc.services.subcontract import _reconcile_subcontract_links


class MultiSubcontractorTests(unittest.TestCase):
    tearDown = attendance_tests.TeamAttendanceTestCase.tearDown
    _login = attendance_tests.TeamAttendanceTestCase._login
    _csrf = staticmethod(attendance_tests.TeamAttendanceTestCase._csrf)
    _token = attendance_tests.TeamAttendanceTestCase._token
    _make_project = attendance_tests.TeamAttendanceTestCase._make_project
    _make_subcontractor = attendance_tests.TeamAttendanceTestCase._make_subcontractor
    def setUp(self):
        attendance_tests.TeamAttendanceTestCase.setUp(self)
        self.stage.execution_mode = 'subcontractor'
        self.stage.assigned_subcontractor_id = self.sub.id
        self.second = Subcontractor(name='Second crew', contract_type='lump_sum', lump_sum_amount=2000)
        db.session.add(self.second)
        db.session.commit()

    def post(self, action, **data):
        data['_csrf_token'] = self._token()
        response = self.client.post(f'/hdc/stage/{self.stage.id}/{action}', data=data)
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()

    def assign(self):
        self.post('shift/subcontractor', subcontractor_id=self.second.id)

    def test_members_survive_assignment_and_reconciliation(self):
        self.assign()
        self.assign()
        self.assertEqual([s.id for s in self.stage.assigned_subcontractors], [self.sub.id, self.second.id])
        _reconcile_subcontract_links()
        self.assertEqual(self.second.stage_id, self.stage.id)
        self.assertEqual(self.sub.lump_sum_amount, 1000000)
        self.assertEqual(self.second.lump_sum_amount, 2000)
        response = self.client.get(f'/hdc/projects/{self.project.id}')
        self.assertEqual(response.status_code, 200)

    def test_progress_and_completion_are_member_specific(self):
        self.assign()
        self.post('sub-progress', subcontractor_id=self.sub.id, work_done_percentage=100)
        self.assertEqual(self.second.work_done_percentage, 0)
        self.post('status', status='completed')
        self.assertNotEqual(self.stage.status, 'completed')
        self.post('status', status='completed', auto_sub_complete='1')
        self.assertEqual(self.second.work_done_percentage, 100)

    def test_remove_one_then_all(self):
        self.assign()
        self.post('shift/company', subcontractor_id=self.sub.id)
        self.assertIsNone(self.sub.stage_id)
        self.assertEqual(self.stage.assigned_subcontractor_id, self.second.id)
        self.assertEqual(self.stage.execution_mode, 'subcontractor')
        self.post('shift/company')
        self.assertIsNone(self.second.stage_id)
        self.assertEqual(self.stage.execution_mode, 'company')

    def test_reject_silent_move_and_invalid_terms(self):
        self.second.stage_id = self.other_stage.id
        db.session.commit()
        self.assign()
        self.assertEqual(self.second.stage_id, self.other_stage.id)
        self.second.stage_id = None
        db.session.commit()
        self.post('shift/subcontractor', subcontractor_id=self.second.id, lump_sum_amount='nan')
        self.assertIsNone(self.second.stage_id)
        self.assertEqual(self.second.lump_sum_amount, 2000)

    def test_ambiguous_or_foreign_progress_does_not_change_primary(self):
        self.assign()
        self.post('sub-progress', work_done_percentage=90)
        self.assertEqual(self.sub.work_done_percentage, 0)
        self.post('sub-progress', subcontractor_id=999999, work_done_percentage=90)
        self.assertEqual(self.sub.work_done_percentage, 0)
