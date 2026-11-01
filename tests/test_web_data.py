"""M3 real HTTP/browser acceptance, contracts and lifecycle/security regressions."""
from collections import Counter
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

import pytest
from test_web_frontend import browser
from nexo.web_frontend import build, compile_web, WebError

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'examples/web/data/app.nexo'
SOURCE=APP.read_text()

def expect(actual):
    from playwright.sync_api import expect as assertion
    return assertion(actual)


@contextmanager
def served(folder):
    counts=Counter();writes=[];lock=threading.Lock()
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(folder),**kwargs)
        def log_message(self,*args):pass
        def send_json(self,value,status=200,content_type='application/json'):
            payload=json.dumps(value).encode();self.send_response(status)
            self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(payload)));self.end_headers()
            try:self.wfile.write(payload)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_GET(self):
            if not self.path.startswith('/api/'):return super().do_GET()
            with lock:counts[self.path]+=1;count=counts[self.path]
            if self.path.startswith('/api/slow'):time.sleep(.6)
            if self.path=='/api/retry' and count==1:return self.send_json({},503)
            if self.path=='/api/forbidden':return self.send_json({},403)
            if self.path=='/api/prototype':return self.send_json({'__proto__':'safe','constructor':1})
            if self.path=='/api/invalid':return self.send_json([{'id':'wrong','name':'x'}])
            if self.path=='/api/unsafe-int':return self.send_json([{'id':9007199254740992,'name':'x'}])
            if self.path=='/api/extra':return self.send_json([{'id':1,'name':'x','secret':'no'}])
            if self.path=='/api/text':return self.send_json([],content_type='text/plain')
            if self.path=='/api/large':return self.send_json([{'id':1,'name':'x'*1048576}])
            if self.path=='/api/redirect':
                self.send_response(302);self.send_header('Location','/api/products');self.end_headers();return
            return self.send_json([{'id':1,'name':'<img src=x onerror=alert(1)>'},*writes])
        def do_POST(self):
            length=int(self.headers.get('Content-Length','0'));body=json.loads(self.rfile.read(length))
            writes.append({'id':len(writes)+2,'name':body['name']})
            with lock:counts[self.command]+=1
            if self.headers.get('X-CSRF-Token')!='test-token':return self.send_json({},403)
            return self.send_json(writes[-1])
        do_PUT=do_POST
        do_PATCH=do_POST
        def do_DELETE(self):
            with lock:counts['DELETE']+=1
            if self.headers.get('X-CSRF-Token')!='test-token':return self.send_json({},403)
            self.send_json({'id':1,'name':'deleted'})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/',counts
    finally:server.shutdown();server.server_close();thread.join()


def prepare(folder,source=SOURCE,token=True,main=None):
    entry=folder/'app.nexo';entry.write_text(source);assets=build(entry,folder)
    if token:
        index=folder/'index.html';index.write_text(index.read_text().replace('<title>','<meta name="nexo-csrf-token" content="test-token"><title>'))
    (folder/'main.js').write_text(main or 'import {App} from "./app.js";window.app=App(document.querySelector("#app"));')
    return assets


def test_contracts_determinism_and_csp(tmp_path):
    a=compile_web(SOURCE,APP);b=compile_web(SOURCE,APP)
    assert a==b
    assert 'fetch(' in a[0] and 'XMLHttpRequest' not in a[0] and 'innerHTML' not in a[0]
    assert "connect-src 'self'" in prepare(tmp_path)['index.html']


@pytest.mark.parametrize('old,new,code',[
 ('as: list[Product]','as: Missing','NW3001'),
 ('as: list[Product],','', 'NW3001'),
 ('api.get(','api.post(', 'NW3002'),
 ('retries: 1','retries: 4','NW3002'),
 ('cache_ms: 1000','cache_ms: 60001','NW3002'),
 ('timeout_ms: 5000','timeout_ms: 0','NW3002'),
 ('invalidate: [products]','invalidate: [create]','NW3002'),
 ('body: NewProduct(name: "Nexo handbook")','body: NewProduct(name: 7)','NW3001'),
 ('    name: str','    name: str?', 'NW3001'),
 ('li(product.name)','li(product.missing)','NW3001'),
 ('elif products.ready:','elif true:','NW3002'),
 ('        endpoint,','        "https://evil.invalid/api",','NW3003'),
 ('        as: Product,','        as: Product,\n        retries: 1,','NW3002'),
])
def test_data_diagnostics(old,new,code):
    with pytest.raises(WebError) as error:compile_web(SOURCE.replace(old,new),APP)
    assert error.value.code==code
    assert 'Fix:' in str(error.value)


def test_real_browser_load_mutation_invalidation_escape_and_cleanup(tmp_path,browser):
    prepare(tmp_path)
    with served(tmp_path) as (base,counts):
        page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(base)
        expect(page.locator('li')).to_have_text(['<img src=x onerror=alert(1)>'])
        assert page.locator('img').count()==0
        page.evaluate('window.old=document.querySelector("li")')
        page.get_by_role('button',name='Create product',exact=True).click()
        expect(page.get_by_text('Created: Nexo handbook',exact=True)).to_be_visible()
        expect(page.locator('li')).to_have_text(['<img src=x onerror=alert(1)>','Nexo handbook'])
        assert counts['POST']==1 and counts['/api/products']==2
        page.evaluate('window.oldButton=document.querySelector("button");app.destroy();app.destroy();oldButton.click()')
        expect(page.locator('#app')).to_be_empty()
        assert counts['/api/products']==2 and not errors
        page.close()


@pytest.mark.parametrize('endpoint,kind',[
 ('invalid','Validation'),('unsafe-int','Validation'),('extra','Validation'),
 ('text','Validation'),('large','Limit'),('forbidden','Http'),('redirect','Network'),
])
def test_real_browser_response_failures(tmp_path,browser,endpoint,kind):
    prepare(tmp_path,SOURCE.replace('"/api/products"','"/api/'+endpoint+'"').replace('retries: 1','retries: 0'))
    with served(tmp_path) as (base,counts):
        page=browser.new_page();page.goto(base)
        expect(page.get_by_role('alert')).to_contain_text(kind+':')
        assert page.locator('li').count()==0
        assert counts['/api/'+endpoint]==1
        if endpoint=='redirect':assert counts['/api/products']==0
        page.close()


def test_real_browser_timeout_cancel_retry_and_stale_completion(tmp_path,browser):
    source=SOURCE.replace('timeout_ms: 5000','timeout_ms: 200').replace('retries: 1','retries: 0')
    prepare(tmp_path,source)
    with served(tmp_path) as (base,counts):
        page=browser.new_page();page.goto(base)
        expect(page.locator('li')).to_have_count(1)
        page.evaluate('app.update({endpoint:"/api/slow",writable:true})')
        expect(page.get_by_role('alert')).to_contain_text('Timeout:')
        page.evaluate('app.update({endpoint:"/api/products",writable:true})')
        expect(page.locator('li')).to_have_count(1)
        page.wait_for_timeout(700)
        expect(page.locator('li')).to_have_count(1) # Timed-out response cannot replace current data.
        page.evaluate('app.update({endpoint:"/api/slow2",writable:true})')
        page.get_by_role('button',name='Cancel read',exact=True).click()
        expect(page.get_by_role('alert')).to_contain_text('Cancelled:')
        page.evaluate('app.destroy()');page.wait_for_timeout(700)
        expect(page.locator('#app')).to_be_empty();page.close()
    prepare(tmp_path,SOURCE.replace('"/api/products"','"/api/retry"'))
    with served(tmp_path) as (base,counts):
        page=browser.new_page();page.goto(base);expect(page.locator('li')).to_have_count(1)
        assert counts['/api/retry']==2;page.close()


def test_real_browser_dedupe_consumer_cancel_cache_and_root_isolation(tmp_path,browser):
    source='''use nexo.web
component Read:
    load first = api.get("/api/slow", as: str, cache_ms: 1000)
    view:
        if first.error != none:
            p(first.error.kind)
        button("Cancel", on_click: first.cancel)
pub component App:
    state visible: bool = true
    fn toggle():
        visible = not visible
    view:
        button("Toggle", on_click: toggle)
        if visible:
            Read()
            Read()
'''
    # Contract mismatch must not be cached; use scalar-free list[Product] for successful cache behavior.
    source=source.replace('component Read:', 'type Product:\n    id: int\n    name: str\ncomponent Read:').replace('as: str','as: list[Product]').replace('        if first.error', '        if first.ready:\n            p("Ready")\n        if first.error')
    prepare(tmp_path,source)
    with served(tmp_path) as (base,counts):
        page=browser.new_page();page.goto(base)
        page.get_by_role('button',name='Cancel',exact=True).first.click()
        expect(page.get_by_text('Cancelled',exact=True)).to_be_visible()
        expect(page.get_by_text('Ready',exact=True)).to_be_visible()
        assert counts['/api/slow']==1
        page.get_by_role('button',name='Toggle',exact=True).click()
        page.get_by_role('button',name='Toggle',exact=True).click()
        expect(page.get_by_text('Ready',exact=True)).to_have_count(2)
        assert counts['/api/slow']==1 # same root cache, both remounted readers
        page.evaluate('import("./app.js").then(({App})=>window.second=App(document.querySelector("#app")))')
        expect(page.get_by_text('Ready',exact=True)).to_have_count(4)
        assert counts['/api/slow']==2 # second root cannot share cache/session data
        page.close()


@pytest.mark.parametrize('verb',['post','put','patch','delete'])
def test_real_browser_mutation_verbs_and_csrf(tmp_path,browser,verb):
    source=SOURCE.replace('api.post(',f'api.{verb}(')
    prepare(tmp_path,source,token=False)
    with served(tmp_path) as (base,counts):
        page=browser.new_page();page.goto(base);expect(page.locator('li')).to_have_count(1)
        page.get_by_role('button',name='Create product',exact=True).click()
        expect(page.get_by_role('alert')).to_contain_text('Security:')
        assert counts[verb.upper()]==0
        page.evaluate('const meta=document.createElement("meta");meta.name="nexo-csrf-token";meta.content="test-token";document.head.append(meta)')
        page.get_by_role('button',name='Create product',exact=True).click()
        expect(page.get_by_text('Created:',exact=False)).to_be_visible()
        assert counts[verb.upper()]==1
        page.close()


def test_imported_contracts_privacy_recursion_and_layout_slots(tmp_path):
    (tmp_path/'nexo.toml').write_text('[project]\nname="contracts"\n')
    (tmp_path/'models.nexo').write_text('use nexo.web\npub type Product:\n    id: int\n    name: str\n')
    source=SOURCE.replace('pub type Product:\n    id: int\n    name: str\n','use models\n').replace('as: list[Product]','as: list[models.Product]').replace('as: Product','as: models.Product')
    compile_web(source,tmp_path/'app.nexo')
    (tmp_path/'models.nexo').write_text('use nexo.web\ntype Product:\n    id: int\n    name: str\n')
    with pytest.raises(WebError,match='private'):compile_web(source,tmp_path/'app.nexo')
    with pytest.raises(WebError,match='Recursive'):
        compile_web(SOURCE.replace('    id: int','    id: Product'),APP)
    with pytest.raises(WebError,match='unconditional'):
        compile_web('use nexo.web\nlayout Shell:\n    view:\n        if true:\n            slot()\npub page Home:\n    path = "/"\n    layout = Shell\n    view:\n        p("Home")\n')


def test_real_browser_local_demo_server_and_public_read_demo(tmp_path,browser):
    import importlib.util
    spec=importlib.util.spec_from_file_location('m3_demo_server',APP.parent/'server.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    build(APP,tmp_path)
    server=module.create_server(tmp_path,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}/'
    try:
        first=browser.new_context();second=browser.new_context()
        page=first.new_page();page.goto(base);expect(page.locator('li')).to_have_text(['Nexo guide'])
        assert first.cookies()[0]['httpOnly'] and first.cookies()[0]['sameSite']=='Strict'
        page.get_by_role('button',name='Create product',exact=True).click()
        expect(page.locator('li')).to_have_text(['Nexo guide','Nexo handbook'])
        other=second.new_page();other.goto(base);expect(other.locator('li')).to_have_text(['Nexo guide'])
        # Backend checks remain effective when browser resource helpers are bypassed.
        response=first.request.post(base+'api/products',headers={'Origin':base.rstrip('/'),'X-CSRF-Token':'wrong'},data={'name':'bad'})
        assert response.status==403
        token=page.locator('meta[name="nexo-csrf-token"]').get_attribute('content')
        response=first.request.post(base+'api/products',headers={'Origin':'https://evil.invalid','X-CSRF-Token':token},data={'name':'bad'})
        assert response.status==403
        first.close();second.close()
    finally:server.shutdown();server.server_close();thread.join()
    with served(ROOT/'docs') as (base,_):
        page=browser.new_page();page.goto(base+'web-m3-demo/')
        expect(page.locator('li')).to_have_text(['Nexo handbook','Typed browser data'])
        expect(page.get_by_role('button',name='Create product',exact=True)).to_be_disabled()
        assert page.locator('ul > li').count()==2
        page.close()


def test_real_browser_dynamic_request_security_and_dedup_disabled(tmp_path,browser):
    prepare(tmp_path,SOURCE.replace('cache_ms: 1000','cache_ms: 1000, dedupe: false'))
    with served(tmp_path) as (base,counts):
        page=browser.new_page();page.goto(base);expect(page.locator('li')).to_have_count(1)
        page.evaluate('app.update({endpoint:"https://evil.invalid/products",writable:true})')
        expect(page.get_by_role('alert')).to_contain_text('Security:')
        assert counts['/api/products']==1
        page.evaluate('app.update({endpoint:"/api/products",writable:true})')
        expect(page.locator('li')).to_have_count(1)
        page.get_by_role('button',name='Reload',exact=True).click()
        expect(page.locator('li')).to_have_count(1)
        assert counts['/api/products']==2 # explicit reload bypasses cache
        page.close()


@pytest.mark.parametrize('kind',['Validation','Limit'])
def test_real_browser_request_validation_before_network(tmp_path,browser,kind):
    if kind=='Validation':
        source=SOURCE.replace('pub type NewProduct:\n    name: str','pub type NewProduct:\n    name: int').replace('NewProduct(name: "Nexo handbook")','NewProduct(name: 9007199254740992)')
    else:source=SOURCE.replace('NewProduct(name: "Nexo handbook")','NewProduct(name: '+json.dumps('x'*66000)+')')
    prepare(tmp_path,source)
    with served(tmp_path) as (base,counts):
        page=browser.new_page();page.goto(base);expect(page.locator('li')).to_have_count(1)
        page.get_by_role('button',name='Create product',exact=True).click()
        expect(page.get_by_role('alert')).to_contain_text(kind+':')
        assert counts['POST']==0;page.close()


def test_real_browser_last_consumer_disposal_aborts_fetch(tmp_path,browser):
    prepare(tmp_path,SOURCE.replace('"/api/products"','"/api/slow"'))
    with served(tmp_path) as (base,counts):
        page=browser.new_page();page.goto(base)
        expect(page.get_by_text('Loading...',exact=True)).to_be_visible()
        with page.expect_event('requestfailed',predicate=lambda req:'/api/slow' in req.url):
            page.evaluate('app.destroy()')
        page.wait_for_timeout(700)
        expect(page.locator('#app')).to_be_empty();page.close()


def test_real_browser_json_special_field_names_are_data(tmp_path,browser):
    source='''use nexo.web
type Payload:
    __proto__: str
    constructor: int
pub component App:
    load result = api.get("/api/prototype", as: Payload)
    view:
        if result.ready:
            p("{result.data.__proto__}:{result.data.constructor}")
'''
    prepare(tmp_path,source)
    with served(tmp_path) as (base,_):
        page=browser.new_page();page.goto(base)
        expect(page.locator('p')).to_have_text('safe:1');page.close()


def test_guide_m3_project_is_reconstructible_with_local_api(tmp_path):
    import hashlib
    versions=json.loads((ROOT/'docs/ai-guide/versions.json').read_text())
    folder=ROOT/'docs/ai-guide/versions'/('v1-'+versions['current'][:20])
    example=next(e for e in json.loads((folder/'web-examples.json').read_text())['examples'] if e['id']=='web-data')
    assert set(example['files'])=={'nexo.toml','server.py'}
    entry=tmp_path/example['entry_file'];entry.write_text(example['source'])
    for name,text in example['files'].items():(tmp_path/name).write_text(text)
    js,css,_=compile_web(entry.read_text(),entry)
    assert hashlib.sha256(js.encode()).hexdigest()==example['verification']['javascript_sha256']
    assert hashlib.sha256(css.encode()).hexdigest()==example['verification']['css_sha256']
    assert example['files']['server.py']==(APP.parent/'server.py').read_text()
