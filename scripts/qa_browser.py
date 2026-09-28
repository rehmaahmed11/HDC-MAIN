#!/usr/bin/env python3
"""Real-browser release smoke, owning its server and throwaway DB.

Optional development dependency: pip install playwright; playwright install chromium
python scripts/qa_browser.py --output qa/browser.json [--executable /path/to/chromium]
No target URL option: this script cannot mutate an existing deployment.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
PAGES=['/hdc/','/hdc/projects','/hdc/workers','/hdc/timekeeping','/hdc/payroll/generate',
       '/hdc/expenses','/hdc/subcontractors','/hdc/purchase-v2/suppliers','/hdc/purchase-v2/purchases',
       '/hdc/purchase-v2/delivered','/hdc/purchase-v2/usage','/hdc/purchase-v2/stock',
       '/hdc/accounts','/hdc/accounts/manage','/hdc/accounts/new-transaction',
       '/hdc/accounts/money-center','/hdc/accounts/cashflow','/hdc/accounts/cashflow/register',
       '/hdc/accounts/cashflow/reconciliation','/hdc/accounts/reconciliation',
       '/hdc/office-management','/hdc/tool-rental','/hdc/project-estimation',
       '/hdc/reports','/hdc/settings','/hdc/users']


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',required=True)
    ap.add_argument('--executable');args=ap.parse_args()
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server
    result={'pages':[],'flows':[],'page_errors':[],'console_errors':[],'failed_requests':[],
            'bad_responses':[],'third_party_requests':[],'download_navigations':[]}
    with tempfile.TemporaryDirectory(prefix='hdc-browser-owned-') as tmp:
        os.environ.update(HDC_ENV='test',HDC_SECRET_KEY='browser-test-only',
                          HDC_BOOTSTRAP_ADMIN_PASSWORD='Browser-Test-Only-123',
                          HDC_INSTANCE_DIR=tmp,HDC_DB_PATH=tmp+'/qa.db')
        from hdc.app import create_app
        from hdc.extensions import db
        from hdc.models.projects import Project, Stage
        from hdc.models.materials import Supplier
        from hdc.models.subcontract import Subcontractor
        app=create_app({'TESTING':True})
        server=make_server('0.0.0.0',0,app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path=args.executable,
                    args=['--no-sandbox','--disable-dev-shm-usage'])
                context=browser.new_context(viewport={'width':1440,'height':1000})
                page=context.new_page();page.set_default_timeout(12000)
                page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                page.on('console',lambda m:result['console_errors'].append(m.text) if m.type=='error' else None)
                def request_failed(r):
                    # Chromium reports navigation-to-attachment as ERR_ABORTED;
                    # the actual downloaded bytes are asserted below.
                    key = 'download_navigations' if r.url == base+'/hdc/reports/export/profitability' and r.failure == 'net::ERR_ABORTED' else 'failed_requests'
                    result[key].append({'url':r.url,'failure':r.failure})
                page.on('requestfailed',request_failed)
                page.on('response',lambda r:result['bad_responses'].append({'url':r.url,'status':r.status}) if r.status>=400 else None)
                page.on('request',lambda r:result['third_party_requests'].append(r.url) if not r.url.startswith(base) else None)
                def login(p):
                    p.goto(base+'/hdc/login');p.locator('[name=username]').fill('admin')
                    p.locator('[name=password]').fill('Browser-Test-Only-123')
                    p.locator('button[type=submit]').click();p.wait_for_url(base+'/hdc/')
                login(page);result['flows'].append('login via visible form')
                # Real input, browser validation, actual form submit, DB query.
                hostile='<img src=x onerror=window.qaXSS=1> QA اردو'
                page.goto(base+'/hdc/projects/add');page.locator('[name=project_code]').fill('BROWSER-QA')
                page.locator('[name=name]').fill(hostile);page.locator('[name=client]').fill('Browser Owner')
                page.locator('form').filter(has=page.locator('[name=project_code]')).locator('button[type=submit]').click();page.wait_for_url('**/hdc/projects/*')
                with app.app_context():
                    row=Project.query.filter_by(project_code='BROWSER-QA').one();pid=row.id
                    assert row.name==hostile
                page.reload();assert page.evaluate('window.qaXSS || 0')==0
                assert hostile in page.locator('body').inner_text()
                result['flows'].append('create project; persistent Unicode and escaped HTML/XSS payload')
                page.goto(base+'/hdc/purchase-v2/suppliers')
                page.locator('form[method=POST] [name=name],form[method=post] [name=name]').first.fill('Browser Supplier')
                page.locator('form[method=POST] [name=phone],form[method=post] [name=phone]').first.fill('03001234567')
                page.locator('form').filter(has=page.locator('[name=name]')).first.locator('button[type=submit]').click()
                page.wait_for_load_state('networkidle')
                with app.app_context():supplier_id=Supplier.query.filter_by(name='Browser Supplier').one().id
                button=page.locator(f'[data-id="{supplier_id}"]').first;button.click()
                page.locator('#supplierEditModal.show').wait_for();page.locator('#supplierEditName').fill('Browser Supplier Edited')
                page.locator('#supplierEditForm button[type=submit]').click();page.wait_for_load_state('networkidle')
                with app.app_context():assert db.session.get(Supplier,supplier_id).name=='Browser Supplier Edited'
                result['flows'].append('supplier create + Bootstrap edit modal + persisted update')
                # Stage sqft panel: it must render inside the card (not rowed
                # into the 18-column stages table, which clipped the fields)
                # and its horizontal term fields must share one line.
                with app.app_context():
                    stage=Stage(project_id=pid,name='QA Sqft Stage',status='Active',
                                execution_mode='subcontractor',contract_basis='Per Sq Ft',
                                rate_per_sqft=100,qty_sqft=2000)
                    db.session.add(stage);db.session.flush()
                    member=Subcontractor(name='QA Sqft Sub',contract_type='sqft',rate_per_sqft=20,
                                         total_sqft=100,project_id=pid,stage_id=stage.id)
                    db.session.add(member);db.session.flush()
                    stage.assigned_subcontractor_id=member.id
                    db.session.commit();stage_id=stage.id
                page.goto(base+f'/hdc/projects/{pid}')
                page.evaluate("document.querySelectorAll('#stage-subs-%d').forEach(e=>e.classList.remove('collapse'))" % stage_id)
                page.wait_for_timeout(200)
                layout=page.evaluate('''(sid) => {
                    const wrap=document.getElementById('stage-subs-'+sid);
                    const panel=wrap.querySelector('.stage-panel');
                    const card=panel.parentElement;
                    const fields=Array.from(panel.querySelectorAll('.stage-terms-grid [class*="col-"]'));
                    const boxes=fields.map(c=>c.querySelector('input,select')||c);
                    const tops=boxes.map(b=>Math.round(b.getBoundingClientRect().top));
                    const strip=panel.querySelector('.stage-sqft-strip');
                    return {fields:fields.length,
                        distinct_field_tops:new Set(tops).size,
                        panel_right:Math.round(panel.getBoundingClientRect().right),
                        card_right:Math.round(card.getBoundingClientRect().right),
                        field_right:Math.max.apply(null,boxes.map(b=>Math.round(b.getBoundingClientRect().right))),
                        strip_text:strip?strip.textContent.replace(/\\s+/g,' ').trim():'',
                        inside_table:!!wrap.closest('table'),
                        horizontal_overflow:document.documentElement.scrollWidth>innerWidth+1};
                }''',stage_id)
                assert layout['fields']==5,layout
                assert layout['distinct_field_tops']==1,layout
                assert layout['field_right']<=layout['card_right']+1,layout
                assert not layout['inside_table'],layout
                assert not layout['horizontal_overflow'],layout
                assert '2,000' in layout['strip_text'] and '100' in layout['strip_text'],layout
                result['stage_panel_layout']=layout
                result['flows'].append('stage sqft panel stays inside its card with aligned term fields')
                # Actual fetch wrapper must automatically attach CSRF.
                answer=page.evaluate('''async () => { const r=await fetch('/api/v2/purchase/materials', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({name:'Browser Material',unit:'KG'})});
                    return {status:r.status,body:await r.json()}; }''')
                assert answer['status']==200 and answer['body']['ok'];result['flows'].append('same-origin fetch mutation with automatic CSRF')
                for viewport in ({'width':1440,'height':1000},{'width':390,'height':844}):
                    page.set_viewport_size(viewport)
                    for path in PAGES+[f'/hdc/projects/{pid}']:
                        start=time.perf_counter();resp=page.goto(base+path);page.wait_for_load_state('networkidle')
                        assert resp.status==200,(path,resp.status)
                        result['pages'].append({'path':path,'width':viewport['width'],'status':resp.status,
                            'milliseconds':round((time.perf_counter()-start)*1000),
                            'horizontal_overflow':page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')})
                page.set_viewport_size({'width':1440,'height':1000});page.goto(base+'/hdc/projects')
                page.locator('#themeToggle').click();assert page.locator('html').get_attribute('data-theme')=='dark'
                page.reload();assert page.locator('html').get_attribute('data-theme')=='dark'
                page.locator('#themeToggle').click();assert page.locator('html').get_attribute('data-theme')=='light'
                result['flows'].append('theme toggle and persistence')
                # Real download stream rather than checking HTTP 200 alone.
                with page.expect_download() as download:
                    page.evaluate("location.href='/hdc/reports/export/profitability'")
                text=Path(download.value.path()).read_text();assert 'BROWSER-QA' in text
                result['flows'].append('profitability CSV browser download contains created project')
                nojs=browser.new_context(java_script_enabled=False);p2=nojs.new_page();login(p2)
                p2.goto(base+'/hdc/projects/add');p2.locator('[name=project_code]').fill('NOJS-QA')
                p2.locator('[name=name]').fill('No JavaScript Site');p2.locator('form').filter(has=p2.locator('[name=project_code]')).locator('button[type=submit]').click()
                with app.app_context():assert Project.query.filter_by(project_code='NOJS-QA').count()==1
                result['flows'].append('login and create form with JavaScript disabled / server-rendered CSRF')
                nojs.close();browser.close()
        finally:
            server.shutdown();thread.join(timeout=5)
            with app.app_context():db.session.remove();db.engine.dispose()
    Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
    assert not any(result[k] for k in ('page_errors','console_errors','failed_requests','bad_responses','third_party_requests')),result
    assert not any(row['horizontal_overflow'] for row in result['pages'])
    print(f"Browser PASS: {len(result['pages'])} desktop/mobile renders, {len(result['flows'])} verified flows")


if __name__=='__main__':main()
