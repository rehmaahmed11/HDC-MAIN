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


class MultiSubcontractorSelectionTests(MultiSubcontractorTests):
    """Several subcontractors can be picked for a stage in one go."""

    def setUp(self):
        MultiSubcontractorTests.setUp(self)
        self.third = Subcontractor(name="O'Neil crew", contract_type='lump_sum', lump_sum_amount=3000)
        db.session.add(self.third)
        db.session.commit()

    def _post_form(self, url, data):
        payload = {'_csrf_token': self._token()}
        payload.update(data)
        response = self.client.post(url, data=payload)
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        return response

    def test_add_several_in_one_request_keeps_existing_member(self):
        self._post_form(f'/hdc/stage/{self.stage.id}/shift/subcontractor',
                        {'subcontractor_ids': [str(self.second.id), str(self.third.id)],
                         'retention_pct': '5'})
        self.assertEqual([s.id for s in self.stage.assigned_subcontractors],
                         [self.sub.id, self.second.id, self.third.id])
        self.assertEqual(self.stage.assigned_subcontractor_id, self.sub.id)
        self.assertEqual(self.second.retention_percentage, 5)
        self.assertEqual(self.third.retention_percentage, 5)
        # Blank terms keep each subcontractor's own contract value.
        self.assertEqual(self.second.lump_sum_amount, 2000)
        self.assertEqual(self.third.lump_sum_amount, 3000)
        page = self.client.get(f'/hdc/projects/{self.project.id}').get_data(as_text=True)
        self.assertIn(f'id="stage-subs-{self.stage.id}"', page)
        self.assertIn('Add selected subcontractors', page)

    def test_busy_subcontractor_skipped_others_added(self):
        self.third.stage_id = self.other_stage.id
        db.session.commit()
        self._post_form(f'/hdc/stage/{self.stage.id}/shift/subcontractor',
                        {'subcontractor_ids': [str(self.second.id), str(self.third.id)]})
        self.assertEqual(self.second.stage_id, self.stage.id)
        self.assertEqual(self.third.stage_id, self.other_stage.id)

    def test_invalid_terms_reject_whole_batch(self):
        self._post_form(f'/hdc/stage/{self.stage.id}/shift/subcontractor',
                        {'subcontractor_ids': [str(self.second.id), str(self.third.id)],
                         'retention_pct': '150'})
        self.assertIsNone(self.second.stage_id)
        self.assertIsNone(self.third.stage_id)

    def test_add_stage_with_multiple_subcontractors(self):
        from hdc.models.projects import Stage
        self._post_form(f'/hdc/projects/{self.project.id}/stage/add',
                        {'name': 'Plaster', 'contract_basis': 'Lump Sum', 'lump_sum_value': '9000',
                         'rate_per_sqft': '', 'status': 'Active',
                         'subcontractor_ids': [str(self.second.id), str(self.third.id)],
                         'sub_lump_sum_amount': '4000'})
        stage = Stage.query.filter_by(name='Plaster').one()
        self.assertEqual(stage.lump_sum_value, 9000)
        self.assertEqual(stage.execution_mode, 'subcontractor')
        self.assertEqual([s.id for s in stage.assigned_subcontractors], [self.second.id, self.third.id])
        self.assertEqual(self.second.lump_sum_amount, 4000)

    def test_edit_stage_adds_members(self):
        self._post_form(f'/hdc/stage/{self.stage.id}/edit',
                        {'name': self.stage.name, 'status': self.stage.status or 'Active',
                         'contract_basis': self.stage.contract_basis or '',
                         'lump_sum_value': str(self.stage.lump_sum_value or 0),
                         'subcontractor_ids': [str(self.third.id)]})
        self.assertEqual([s.id for s in self.stage.assigned_subcontractors], [self.sub.id, self.third.id])
        page = self.client.get(f'/hdc/stage/{self.stage.id}/edit').get_data(as_text=True)
        self.assertIn('Currently assigned', page)


class StageSqftPanelLayoutTests(MultiSubcontractorTests):
    """The sqft panel must not live inside the horizontally scrolling table.

    It used to be a ``<tr>`` inside the 18-column stages table, so the wide
    table column stretched the panel past the card edge: the sqft fields were
    clipped and the horizontal fields no longer lined up.
    """

    def setUp(self):
        MultiSubcontractorTests.setUp(self)
        self.stage.qty_sqft = 2000
        self.sub.contract_type = 'sqft'
        self.sub.rate_per_sqft = 20
        self.sub.total_sqft = 100
        db.session.commit()

    def test_panel_renders_outside_the_stages_table(self):
        page = self.client.get(f'/hdc/projects/{self.project.id}').get_data(as_text=True)
        marker = f'class="collapse stage-panel-wrap" id="stage-subs-{self.stage.id}"'
        self.assertIn(marker, page)
        head = page[:page.index(marker)]
        self.assertEqual(
            head.count('<table'), head.count('</table>'),
            'stage panel must render outside the stages table, which clips its fields')
        # The old row-based panel is gone.
        self.assertNotIn('stage-subs-row', page)

    def test_allocated_remaining_strip_and_shared_terms_grid(self):
        page = self.client.get(f'/hdc/projects/{self.project.id}').get_data(as_text=True)
        self.assertIn('stage-sqft-strip', page)
        for expected in ('2,000 sqft', '100 sqft', '1,900 sqft'):
            self.assertIn(expected, page)
        # Five equally sized fields in one grid row keep horizontal fields aligned.
        self.assertIn('stage-terms-grid', page)
        field_class = page.count('class="col-6 col-lg"')
        self.assertGreaterEqual(field_class, 5)
        self.assertEqual(field_class % 5, 0,
                         'every terms grid must hold five equally sized fields')

    def test_blank_sqft_still_auto_assigns_only_the_remaining(self):
        self.post('shift/subcontractor', subcontractor_ids=[str(self.second.id)],
                  contract_type='sqft', rate_per_sqft='25')
        self.assertEqual(self.second.contract_type, 'sqft')
        self.assertEqual(self.second.total_sqft, 1900)
