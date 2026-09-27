"""Concurrent clients sharing one SQLite file, not one SQLAlchemy session."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
import time
from qa_support import IsolatedAppTest
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.cashflow import CashFlowEntry
from hdc.services.cashflow_register import save_manual_cash_flow_entry, category_options
from hdc.services.accounts import _account_balance


class ConcurrentPostingTest(IsolatedAppTest):
    def _parallel(self, same_key=True, amount=100):
        with self.app.app_context():
            cash=Account.query.filter_by(name='Company Cash').one();cash.opening_balance=150
            db.session.commit();cash_id=cash.id
            category=next(c.id for c in category_options('out') if c.name=='Miscellaneous')
        barrier=Barrier(2)
        from hdc.services.accounts import _create_account_transaction
        def slow_post(*args,**kwargs):
            # Widen a realistic validation-to-write race; not a timing assertion.
            time.sleep(0.15)
            return _create_account_transaction(*args,**kwargs)
        def submit(i):
            with self.app.app_context():
                barrier.wait(timeout=10)
                try:
                    entry,created=save_manual_cash_flow_entry(direction='out',amount=amount,
                        account_id=cash_id,category_id=category,actor='qa',
                        idempotency_key='same-key' if same_key else f'key-{i}')
                    return ('ok',entry.id,created)
                except ValueError as exc:
                    db.session.rollback();return ('rejected',str(exc))
                finally:db.session.remove()
        with patch('hdc.services.accounts._create_account_transaction',side_effect=slow_post):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(submit,range(2)))
        return cash_id,results

    def test_same_key_concurrent_retry_posts_once(self):
        cash,results=self._parallel(amount=50)
        with self.app.app_context():
            self.assertEqual(CashFlowEntry.query.count(),1,results)
            self.assertEqual(AccountTransaction.query.filter_by(is_void=False).count(),1,results)
            self.assertEqual(_account_balance(cash),100)
        self.assertEqual(sorted(r[2] for r in results),[False,True])

    def test_distinct_concurrent_outflows_cannot_overdraw(self):
        cash,results=self._parallel(same_key=False,amount=100)
        with self.app.app_context():
            self.assertEqual(CashFlowEntry.query.count(),1,results)
            self.assertEqual(_account_balance(cash),50)
        self.assertEqual(sorted(r[0] for r in results),['ok','rejected'])

    def test_concurrent_deliveries_cannot_exceed_purchase(self):
        from hdc.models.projects import Project,Stage
        from hdc.models.materials import Delivery
        supplier=self.api('/api/v2/purchase/suppliers',{'name':'Race supplier'})['id']
        material=self.api('/api/v2/purchase/materials',{'name':'Race material','unit':'KG'})['id']
        purchase=self.api('/api/v2/purchase/purchases',dict(supplier_id=supplier,material_id=material,unit_price=10,quantity=10))['id']
        with self.app.app_context():
            p=Project(project_code='RACE',name='Race site',client='QA');db.session.add(p);db.session.flush()
            stage=Stage(project_id=p.id,name='Race stage');db.session.add(stage);db.session.commit()
            payload=dict(purchase_id=purchase,project_id=p.id,stage_id=stage.id,quantity=6)
        barrier=Barrier(2)
        def submit(i):
            client=self.app.test_client()
            with client.session_transaction() as session:
                session['_user_id']='1';session['_fresh']=True;session['_csrf_token']='race-token'
            barrier.wait(timeout=10)
            return client.post('/api/v2/purchase/deliveries',json=payload,headers={'X-CSRFToken':'race-token'}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,range(2)))
        self.assertEqual(sorted(results),[200,400])
        with self.app.app_context():self.assertEqual(sum(r.quantity for r in Delivery.query.all()),6)

    def test_different_keys_preserve_legitimate_repeated_transactions(self):
        cash,results=self._parallel(same_key=False,amount=50)
        self.assertEqual([r[0] for r in results],['ok','ok'])
        with self.app.app_context():
            self.assertEqual(CashFlowEntry.query.count(),2)
            self.assertEqual(_account_balance(cash),50)
