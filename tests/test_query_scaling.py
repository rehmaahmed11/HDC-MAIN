"""Worker list and actor lookups must batch, without changing wage semantics."""
from qa_support import IsolatedAppTest
from hdc.extensions import db
from hdc.models.workforce import Worker
from sqlalchemy import event


class QueryScalingTest(IsolatedAppTest):
    def test_worker_list_and_actor_queries_are_bounded(self):
        with self.app.app_context():
            db.session.add_all([Worker(worker_code=f'Q{i}',name=f'QA Worker {i}') for i in range(50)])
            db.session.commit();engine=db.engine
        for path in ('/hdc/workers','/hdc/api/row_actors?e=hdc_worker&ids='+','.join(map(str,range(1,51)))):
            statements=[]
            def capture(*args):statements.append(args[2])
            event.listen(engine,'before_cursor_execute',capture)
            try:response=self.client.get(path)
            finally:event.remove(engine,'before_cursor_execute',capture)
            self.assertEqual(response.status_code,200)
            self.assertLessEqual(len(statements),15,(path,len(statements)))

    def test_accounts_worker_payables_do_not_query_per_worker(self):
        with self.app.app_context():
            db.session.add_all([Worker(worker_code=f'A{i}',name=f'Accounts Worker {i}') for i in range(50)])
            db.session.commit();engine=db.engine
        statements=[]
        def capture(*args):statements.append(args[2])
        event.listen(engine,'before_cursor_execute',capture)
        try:response=self.client.get('/hdc/accounts')
        finally:event.remove(engine,'before_cursor_execute',capture)
        self.assertEqual(response.status_code,200)
        self.assertLess(len(statements),100)

    def test_project_contract_aggregation_does_not_lazy_load_stages(self):
        from hdc.models.projects import Project,Stage
        with self.app.app_context():
            projects=[Project(project_code=f'P{i}',name=f'Site {i}',client='QA',owner_lump_sum=5000,contract_type='lump_sum') for i in range(30)]
            db.session.add_all(projects);db.session.flush()
            db.session.add(Stage(project_id=projects[0].id,name='Contract',contract_basis='Lump Sum',lump_sum_value=1000))
            db.session.commit();engine=db.engine
        statements=[]
        def capture(*args):statements.append(args[2])
        event.listen(engine,'before_cursor_execute',capture)
        try:response=self.client.get('/hdc/projects')
        finally:event.remove(engine,'before_cursor_execute',capture)
        self.assertEqual(response.status_code,200)
        self.assertLessEqual(len(statements),15)
