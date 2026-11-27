#!/usr/bin/env python3
"""Isolated destructive lifecycle probes. Never accepts an external database."""
import json, os, tempfile, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
with tempfile.TemporaryDirectory(prefix='hdc-cross-module-qa-') as tmp:
 os.environ.update(HDC_ENV='test', HDC_SECRET_KEY='scratch-test-only', HDC_BOOTSTRAP_ADMIN_PASSWORD='Scratch-test-only-123!', HDC_INSTANCE_DIR=tmp, HDC_DB_PATH=tmp+'/scratch.db')
 from hdc.app import create_app
 from hdc.extensions import db
 from hdc.models.auth import HDCUser
 from hdc.models.projects import Project, Stage
 from hdc.models.accounts import Account, AccountTransaction, Expense, ExpenseCategory
 from hdc.services.accounts import _account_balance, _accounts_reconciliation_findings
 from hdc.utils.dates import _pkt_today
 from hdc.models.tool_rental import Tool, ToolRental, ToolRentalItem
 from hdc.services.tool_tracking import tool_ledger
 from sqlalchemy import text
 app=create_app({'TESTING':True}); client=app.test_client(); result={}
 with app.app_context(): uid=HDCUser.query.filter_by(username='admin').one().id
 with client.session_transaction() as s: s['_user_id']=str(uid); s['_fresh']=True; s['_csrf_token']='scratch-csrf'
 def post(url,**data):
  try:
   r=client.post(url,data={'_csrf_token':'scratch-csrf',**data}); return {'status':r.status_code}
  except Exception as e:
   db.session.rollback(); return {'exception':type(e).__name__, 'message':str(e).split('\n')[0]}
 with app.app_context():
  p=Project(project_code='QA-P',name='Scratch Project'); db.session.add(p);db.session.flush()
  s=Stage(project_id=p.id,name='Scratch Stage');db.session.add(s);db.session.flush()
  db.session.commit(); sid=s.id
  cash=Account.query.filter_by(name='Company Cash').one(); cash.opening_balance=1000;db.session.commit()
  cat=ExpenseCategory.query.filter_by(active_status=True).first(); created=post('/hdc/expenses',project_id=p.id,stage_id=sid,category_id=cat.id,amount=100,date=_pkt_today().isoformat()); exp=Expense.query.filter_by(project_id=p.id,stage_id=sid).one(); eid=exp.id
  et=AccountTransaction.query.filter(AccountTransaction.source_type.like('expense%'),AccountTransaction.source_id==eid).first(); etid=et.id if et else None
  res=post(f'/hdc/stage/{sid}/delete')
  db.session.expire_all(); x=db.session.get(Expense,eid);res.update(stage_exists=db.session.get(Stage,sid) is not None,expense_exists=x is not None,expense_stage_id=x.stage_id if x else None,expense_create=created,ledger_exists=bool(etid and db.session.get(AccountTransaction,etid)),ledger_stage_id=db.session.get(AccountTransaction,etid).stage_id if etid else None,foreign_key_check=[list(x) for x in db.session.execute(text('PRAGMA foreign_key_check'))]); result['stage_delete_with_expense']=res
  cash=Account.query.filter_by(name='Company Cash').one(); a=Account(name='Scratch void-only account',type='person'); db.session.add(a);db.session.flush(); aid=a.id
  txn=AccountTransaction(from_account_id=cash.id,to_account_id=aid,executed_by_account_id=cash.id,amount=30,type='party_payment',category='expense',is_void=True);db.session.add(txn);db.session.commit(); tid=txn.id
  res=post('/hdc/accounts/manage',action='delete_account',account_id=str(aid)); db.session.expire_all();res.update(account_exists=db.session.get(Account,aid) is not None,transaction_exists=db.session.get(AccountTransaction,tid) is not None,transaction_to_account_id=db.session.get(AccountTransaction,tid).to_account_id,foreign_key_check=[list(x) for x in db.session.execute(text('PRAGMA foreign_key_check'))]);res['cash_before_restore']=_account_balance(cash.id);res['restore']=post('/hdc/accounts',action='restore_transaction',transaction_id=tid);db.session.expire_all();res['transaction_void_after_restore']=db.session.get(AccountTransaction,tid).is_void;res['cash_after_restore']=_account_balance(cash.id);result['delete_account_only_voided_history']=res
  t=Tool(tool_code='QA-TOOL',name='Scratch tool',total_quantity=5,unit='pcs',is_void=False);db.session.add(t);db.session.flush(); toolid=t.id
  def create_rental(name):
   response=post('/hdc/tool-rental/create',txn_type='new_rental',renter_type='external',customer_name=name,billing_type='no_charge',rental_date=_pkt_today().isoformat(),**{'tool_id[]':str(toolid),'qty[]':'5','rate[]':'0'})
   rental=ToolRental.query.filter_by(customer_name=name).one()
   return response,rental.id
  create1,r1id=create_rental('Scratch customer A')
  cancel=post(f'/hdc/tool-rental/{r1id}/void',void_reason='scratch test')
  create2,r2id=create_rental('Scratch customer B')
  restore=post(f'/hdc/tool-rental/{r1id}/restore');db.session.expire_all(); ledger=tool_ledger(); row=next(v for v in ledger['tools'] if v['tool_id']==toolid); restore.update(create_first=create1,cancel=cancel,create_second=create2,owned=db.session.get(Tool,toolid).total_quantity,pending_sum=db.session.get(Tool,toolid).rented_out_qty,rental_restored=not db.session.get(ToolRental,r1id).is_void,ledger={k:row.get(k) for k in ('owned_qty','out_qty','in_store_qty')},reconciled=ledger['totals'].get('reconciled'));result['restore_after_reissue']=restore
  wipe=post('/hdc/settings',action='wipe_data',wipe_targets='expenses',wipe_confirm_text='WIPE',wipe_confirm_check='1');db.session.expire_all(); tx=db.session.get(AccountTransaction,etid)
  with app.test_request_context('/'):
   findings=_accounts_reconciliation_findings()
  wipe.update(expense_exists=db.session.get(Expense,eid) is not None,ledger_exists=tx is not None,ledger_is_void=tx.is_void if tx else None,cash_balance=_account_balance(cash.id),reported_orphan=any(i['id']==etid for i in findings['orphan_txns']),foreign_key_check=[list(x) for x in db.session.execute(text('PRAGMA foreign_key_check'))]);result['wipe_expenses_without_accounts']=wipe
  s2=Stage(project_id=p.id,name='Stage for wipe');db.session.add(s2);db.session.commit();s2id=s2.id
  exp2=Expense(project_id=p.id,stage_id=s2id,amount=100);db.session.add(exp2);db.session.commit();e2id=exp2.id
  project_id=p.id
  wipep=post('/hdc/settings',action='wipe_data',wipe_targets='projects',wipe_confirm_text='WIPE',wipe_confirm_check='1');db.session.expire_all(); e2=db.session.get(Expense,e2id);wipep.update(project_exists=db.session.get(Project,project_id) is not None,expense_project_id=e2.project_id if e2 else None,expense_stage_id=e2.stage_id if e2 else None,foreign_key_check=[list(x) for x in db.session.execute(text('PRAGMA foreign_key_check'))]);result['wipe_projects_without_dependencies']=wipep
  db.session.remove();db.engine.dispose()
 print(json.dumps(result,indent=2))
