"""Execute hostile but harmless request boundaries against disposable data."""
import io
from qa_support import IsolatedAppTest
from hdc.extensions import db
from hdc.models.auth import HDCUser
from hdc.models.projects import Project, Stage, StageDrawing
from werkzeug.security import check_password_hash


class SecurityBoundaryTest(IsolatedAppTest):
    def test_json_root_shape_is_rejected_for_every_json_mutation(self):
        import re
        cases = [(re.sub(r'<(?:[^:>]+:)?[^>]+>', '1', rule.rule), method)
                 for rule in self.app.url_map.iter_rules() if '/api/' in rule.rule
                 for method in sorted(rule.methods & {'POST','PUT','PATCH','DELETE'})]
        self.assertGreater(len(cases), 20)
        for url,method in cases:
            for payload in ([1],123,'bad'):
                with self.subTest(url=url,payload=payload):
                    response=self.client.open(url,method=method,json=payload,headers={'X-CSRFToken':self.token})
                    self.assertEqual(response.status_code,400)
                    self.assertFalse(response.get_json()['ok'])

    def test_invalid_report_dates_return_controlled_error(self):
        for field in ('worker_date_from','worker_date_to','time_date_from','time_date_to'):
            for value in ('not-a-date','2026-02-30','99999999','9999-12-31'):
                if value=='9999-12-31' and field.endswith('_from'):continue
                with self.subTest(field=field,value=value):
                    response=self.client.get('/hdc/reports',query_string={field:value})
                    self.assertEqual(response.status_code,400)
                    self.assertNotIn(b'Traceback',response.data)

    def test_upload_checks_content_and_sanitizes_paths(self):
        with self.app.app_context():
            p=Project(project_code='UPLOAD',name='Upload site',client='QA');db.session.add(p);db.session.flush()
            s=Stage(project_id=p.id,name='Stage');db.session.add(s);db.session.commit();sid=s.id
        for content,name in ((b'<script>alert(1)</script>','evil.pdf'),(b'%PDF-1.4\n','evil.py')):
            self.form(f'/hdc/stage/{sid}/drawings/upload',drawings=(io.BytesIO(content),name))
        with self.app.app_context():self.assertEqual(StageDrawing.query.count(),0)
        self.form(f'/hdc/stage/{sid}/drawings/upload',drawings=(io.BytesIO(b'%PDF-1.4\n%%EOF'),'../../safe.pdf'))
        with self.app.app_context():
            row=StageDrawing.query.one();did=row.id
            self.assertNotIn('/',row.original_name);self.assertNotIn('..',row.stored_name)
        response=self.client.get(f'/hdc/stage/drawing/{did}/view')
        self.assertEqual(response.status_code,200);self.assertEqual(response.mimetype,'application/pdf')
        self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
        response.close()

    def test_login_password_logout_and_open_redirect(self):
        with self.app.app_context():
            user=HDCUser.query.filter_by(username='admin').one()
            self.assertNotEqual(user.password_hash,'Release-Test-Only-123')
            self.assertTrue(check_password_hash(user.password_hash,'Release-Test-Only-123'))
        self.form('/hdc/logout')
        self.assertEqual(self.client.get('/hdc/accounts').status_code,302)
        for username,password in (('',''),('admin','wrong'),("' OR 1=1 --",'anything')):
            response=self.client.post('/hdc/login',data={'username':username,'password':password,'_csrf_token':self.token})
            self.assertEqual(response.status_code,200)
            with self.client.session_transaction() as s:self.assertNotIn('_user_id',s)
        response=self.client.post('/hdc/login?next=https://evil.example/',data={
            'username':'admin','password':'Release-Test-Only-123','_csrf_token':self.token})
        self.assertEqual(response.status_code,302);self.assertNotIn('evil.example',response.location)

    def test_repeated_failed_login_blocks_valid_password_within_worker(self):
        self.form('/hdc/logout')
        for _ in range(5):
            self.client.post('/hdc/login',data={'username':'admin','password':'wrong','_csrf_token':self.token})
        response=self.client.post('/hdc/login',data={'username':'admin','password':'Release-Test-Only-123','_csrf_token':self.token})
        self.assertIn(b'Too many failed login attempts',response.data)
        with self.client.session_transaction() as s:self.assertNotIn('_user_id',s)
