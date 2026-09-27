#!/usr/bin/env python3
"""Bounded synthetic query-count/latency benchmark; never accepts a live DB."""
import argparse
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from qa_support import IsolatedAppTest
from hdc.extensions import db
from hdc.models.projects import Project
from hdc.models.workforce import Worker
from hdc.models.accounts import Account,AccountTransaction
from sqlalchemy import event
from hdc.utils.dates import _pkt_today


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);args=ap.parse_args()
    fixture=IsolatedAppTest();fixture.setUp()
    try:
        with fixture.app.app_context():
            db.session.add_all([Worker(worker_code=f'LOAD-{i}',name=f'Load worker {i}',base_daily_wage=1000) for i in range(200)])
            db.session.add_all([Project(project_code=f'LOAD-{i}',name=f'Load site {i}',client='Synthetic') for i in range(100)])
            cash=Account.query.filter_by(name='Company Cash').one();cash.opening_balance=1000000
            party=Account(name='Load counterparty',type='person');db.session.add(party);db.session.flush()
            db.session.add_all([AccountTransaction(date=_pkt_today(),type='party_payment',amount=10,
                from_account_id=cash.id,to_account_id=party.id,executed_by_account_id=cash.id,
                category='expense',is_void=False) for _ in range(2000)])
            db.session.commit();engine=db.engine
        results=[]
        for path in ('/hdc/','/hdc/projects','/hdc/projects/1','/hdc/workers','/hdc/workers/1/ledger',
                     '/hdc/accounts','/hdc/accounts/entries','/hdc/reports','/api/accounts/transaction_history',
                     '/hdc/api/row_actors?e=hdc_worker&ids='+','.join(map(str,range(1,201)))):
            queries=[]
            def count(*args):queries.append(1)
            event.listen(engine,'before_cursor_execute',count)
            start=time.perf_counter()
            try:response=fixture.client.get(path)
            finally:event.remove(engine,'before_cursor_execute',count)
            results.append({'path':path,'status':response.status_code,'queries':len(queries),
                            'milliseconds':round((time.perf_counter()-start)*1000,2),'bytes':len(response.data)})
            assert response.status_code==200,(path,response.status_code)
        Path(args.output).write_text(json.dumps({'fixture':{'projects':100,'workers':200,'ledger_rows':2000},
                                                'measurements':results},indent=2)+'\n')
        for row in results:print(row)
    finally:fixture.doCleanups()


if __name__=='__main__':main()
