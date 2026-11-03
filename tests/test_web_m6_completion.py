"""Integrated remaining M6 capabilities: typed state, lists, context and host IO."""
from pathlib import Path
import pytest
from test_web_frontend import browser
from test_web_hydration import project,served,deferred
from nexo.web_ssr import SSRApplication

ROOT=Path(__file__).resolve().parents[1]


def test_real_browser_keyed_lists_preserve_rows_and_dispose(tmp_path,browser):
    source=(ROOT/'examples/web/keyed/app.nexo').read_text().replace('pub component App:', 'pub page App:\n    path = "/"')
    entry=project(tmp_path,source);app=SSRApplication.from_file(entry,hydrate=True)
    with served(entry) as base:
        page=browser.new_page();path=deferred(page,app);page.goto(base+'/')
        page.evaluate('window.first=document.querySelector("#row-1");window.second=document.querySelector("#row-2");')
        page.evaluate('async path=>{window.m=await import(path);window.handle=m.hydrate(document.querySelector("#app"));}',path)
        assert page.evaluate('document.querySelector("#row-1")===first')
        page.locator('#row-1 button').click()
        page.get_by_role('button',name='Reorder',exact=True).click()
        assert page.evaluate('document.querySelector("#row-1")===first && document.querySelector("#row-2")===second')
        assert page.locator('li').evaluate_all('rows=>rows.map(row=>row.id)')==['row-2','row-1']
        assert page.locator('#row-1 button').inner_text()=='Count: 1'
        assert page.locator('#row-2 span').inner_text()=='Two updated'
        page.get_by_role('button',name='Remove',exact=True).click()
        page.evaluate('first.querySelector("button").click()')
        assert page.evaluate('first.querySelector("button").textContent')=='Count: 1'
        page.get_by_role('button',name='Restore',exact=True).click()
        assert page.locator('#row-1 button').inner_text()=='Count: 0'
        assert page.evaluate('document.querySelector("#row-1")!==first')
        before=page.locator('ul').inner_html()
        error=page.evaluate('''()=>{try{handle.update({products:[{id:1n,name:'A'},{id:1n,name:'B'}]});return 'unexpected';}catch(error){return error.message;}}''')
        assert 'Duplicate' in error and page.locator('ul').inner_html()==before
        page.evaluate('handle.update({products:[]})')
        assert page.locator('li').count()==0
        page.evaluate('handle.update({products:[{id:3n,name:"Three"}]})')
        assert page.locator('#row-3 span').inner_text()=='Three'
        page.close()


CONTEXT='''use nexo.web
pub context Visits:
    state count: int = 0
    fn increment():
        count += 1
pub layout Shell:
    provide Visits
    v = inject(Visits)
    view:
        header:
            p("Layout {v.count}")
            button("Shared increment", on_click: v.increment)
        slot()
pub page Home:
    path = "/"
    v = inject(Visits)
    state visible: bool = true
    fn toggle():
        visible = not visible
    fn increment():
        v.increment()
    view:
        main:
            button("Toggle", on_click: toggle)
            button("Page increment", on_click: increment)
            p("Page {v.count}")
            if visible:
                Child:
                    p("Slot {v.count}")
component Child:
    v = inject(Visits)
    view:
        section:
            p("Child {v.count}")
            slot()
'''.replace('    path = "/"','    path = "/"\n    layout = Shell')


def test_real_browser_context_is_shared_owned_and_request_isolated(tmp_path,browser):
    entry=project(tmp_path,CONTEXT);app=SSRApplication.from_file(entry,hydrate=True)
    with served(entry) as base:
        page=browser.new_page();path=deferred(page,app);page.goto(base+'/')
        page.evaluate('async path=>{window.m=await import(path);window.handle=m.hydrate(document.querySelector("#app"));}',path)
        page.get_by_role('button',name='Shared increment',exact=True).click()
        assert page.get_by_text('Layout 1',exact=True).count()==1
        assert page.get_by_text('Page 1',exact=True).count()==1
        assert page.get_by_text('Child 1',exact=True).count()==1
        assert page.get_by_text('Slot 1',exact=True).count()==1
        page.evaluate('window.old=document.querySelector("section");window.text=old.textContent;')
        page.get_by_role('button',name='Toggle',exact=True).click()
        page.get_by_role('button',name='Page increment',exact=True).click()
        assert page.evaluate('old.textContent===text')
        page.get_by_role('button',name='Toggle',exact=True).click()
        assert page.get_by_text('Slot 2',exact=True).count()==1
        page.evaluate('handle.context("Visits").setState({count:10n})')
        assert page.get_by_text('Page 10',exact=True).count()==1
        assert b'Layout 0' in app.response('/').body
        page.close()


from contextlib import contextmanager
import asyncio
import hashlib
import json
import threading
from urllib.request import urlopen
from nexo.web_ssr import local_server
from nexo.web_subscriptions import EventChannel
from nexo.web_frontend import build
from nexo.web_tooling import preview_server


@contextmanager
def custom_server(entry, **options):
    server=local_server(entry,0,hydrate=True,**options)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}'
    finally:
        for channel in options.get('channels',{}).values():channel.close()
        server.shutdown();server.server_close();thread.join()


DATA='''use nexo.web
type Product:
    id: int
    name: str
pub page Detail(id: int):
    path = "/detail/{id}"
    state title: str = "Initial"
    state products: list[Product] = []
    state notice: str = "Waiting"
    state result: int = 0
    view:
        main:
            h1(title)
            p(notice, id: "notice")
            p("Result {result}", id: "result")
            ul:
                for product in products key product.id:
                    li("{product.id}: {product.name}")
'''


def test_async_loader_is_typed_isolated_and_times_out(tmp_path):
    entry=project(tmp_path,DATA)
    async def loader(request):
        await asyncio.sleep(0)
        with pytest.raises(TypeError):request.params['id']=0
        return {'title':f'Item {request.params["id"]}','products':[{'id':9223372036854775807,'name':'<safe>'}]}
    app=SSRApplication.from_file(entry,hydrate=True,loader=loader)
    assert b'9223372036854775807: &lt;safe&gt;' in app.response('/detail/2/').body
    async def concurrent():
        return await asyncio.gather(*(app.aresponse(f'/detail/{i}/') for i in range(20)))
    responses=asyncio.run(concurrent())
    assert all(response.status==200 and f'<h1>Item {i}</h1>'.encode() in response.body for i,response in enumerate(responses))
    async def invalid(request):return {'title':'safe','products':[{'id':True,'name':'bad'}]}
    bad=SSRApplication.from_file(entry,hydrate=True,loader=invalid)
    assert bad.response('/detail/2/').status==500
    async def slow(request):await asyncio.sleep(1);return {}
    timed=SSRApplication.from_file(entry,loader=slow,loader_timeout=0.01)
    assert timed.response('/detail/2/').status==500


def test_real_browser_server_data_transfers_losslessly_and_updates_atomically(tmp_path,browser):
    entry=project(tmp_path,DATA)
    async def loader(request):return {'title':'Loaded <title>','products':[{'id':9223372036854775807,'name':'Server record'}]}
    with custom_server(entry,loader=loader) as base:
        page=browser.new_page();page.goto(base+'/detail/7/')
        page.wait_for_selector('#app[data-nexo-hydration="ready"]')
        assert page.locator('li').inner_text()=='9223372036854775807: Server record'
        assert page.locator('h1').inner_text()=='Loaded <title>'
        app=SSRApplication.from_file(entry,hydrate=True,loader=loader)
        path=next(iter(app._client_assets))
        error=page.evaluate('''async path=>{window.m=await import(path);window.handle=m.hydrate(document.querySelector('#app'));try{handle.update({title:'Changed',result:'not int'});return 'unexpected';}catch(error){return error.message;}}''',path)
        assert error!='unexpected' and page.locator('h1').inner_text()=='Loaded <title>'
        page.evaluate('handle.update({products:[{id:1n,name:"Client"}],result:42})')
        assert page.locator('li').inner_text()=='1: Client'
        assert page.locator('#result').inner_text()=='Result 42'
        page.close()


ADD=bytes.fromhex('0061736d0100000001070160027f7f017f030201000707010361646400000a09010700200020016a0b')
SPIN=bytes.fromhex('0061736d010000000105016000017f03020100070801047370696e00000a0b01090003400c000b41000b')
IMPORT=bytes.fromhex('0061736d01000000010401600000020b0103656e7603666f6f0000')


def test_real_browser_wasm_integrity_numeric_calls_timeout_and_disposal(tmp_path,browser):
    entry=project(tmp_path,DATA)
    assets={'/add.wasm':('application/wasm',ADD),'/spin.wasm':('application/wasm',SPIN),'/import.wasm':('application/wasm',IMPORT)}
    with custom_server(entry,wasm=True,assets=assets) as base:
        page=browser.new_page();page.goto(base+'/detail/1/')
        page.wait_for_selector('#app[data-nexo-hydration="ready"]')
        app=SSRApplication.from_file(entry,hydrate=True,wasm=True,assets=assets);path=next(iter(app._client_assets))
        result=page.evaluate('''async options=>{window.m=await import(options.path);window.handle=m.hydrate(document.querySelector('#app'));window.wasm=await m.loadWasm(handle,'/add.wasm',{sha256:options.hash,functions:{add:{params:['i32','i32'],result:'i32'}}});const value=await wasm.call('add',20,22);handle.update({result:value});return value;}''',{'path':path,'hash':hashlib.sha256(ADD).hexdigest()})
        assert result==42 and page.locator('#result').inner_text()=='Result 42'
        wrong=page.evaluate('''async()=>{try{await m.loadWasm(handle,'/add.wasm',{sha256:'0'.repeat(64),functions:{add:{params:['i32','i32'],result:'i32'}}});return 'bad';}catch(error){return error.message;}}''')
        assert 'integrity' in wrong
        failed=page.evaluate('''async hash=>{try{await m.loadWasm(handle,'/import.wasm',{sha256:hash,functions:{foo:{params:[],result:'void'}}});return 'bad';}catch(error){return error.message;}}''',hashlib.sha256(IMPORT).hexdigest())
        assert 'failed' in failed
        stopped=page.evaluate('''async hash=>{const spin=await m.loadWasm(handle,'/spin.wasm',{sha256:hash,timeoutMs:1000,functions:{spin:{params:[],result:'i32'}}});try{await spin.call('spin');return 'bad';}catch(error){return error.message;}}''',hashlib.sha256(SPIN).hexdigest())
        assert 'closed' in stopped
        page.evaluate('handle.destroy()')
        closed=page.evaluate('''async()=>{try{await wasm.call('add',1,2);return 'bad';}catch(error){return error.message;}}''')
        assert 'unavailable' in closed or 'closed' in closed
        page.close()


def test_event_channel_typed_replay_and_close():
    schema={'kind':'record','fields':{'notice':{'kind':'str'},'number':{'kind':'int'}}}
    channel=EventChannel(schema,capacity=2,heartbeat=0.05)
    first=channel.publish({'notice':'One','number':9223372036854775807})
    channel.publish({'notice':'Two','number':2});channel.publish({'notice':'Three','number':3})
    iterator=channel.events(first);next(iterator);frame=next(iterator)
    assert b'Two' in frame
    with pytest.raises(ValueError):channel.publish({'notice':'Bad','number':True})
    iterator.close();assert channel.clients==0
    channel.close()
    with pytest.raises(RuntimeError):channel.publish({'notice':'Closed','number':0})


def test_real_browser_sse_updates_state_and_stops_on_root_destroy(tmp_path,browser):
    schema={'kind':'record','fields':{'notice':{'kind':'str'}}}
    channel=EventChannel(schema,heartbeat=0.05);channel.publish({'notice':'First'})
    entry=project(tmp_path,DATA)
    with custom_server(entry,channels={'/events':channel}) as base:
        page=browser.new_page();page.goto(base+'/detail/1/')
        page.wait_for_selector('#app[data-nexo-hydration="ready"]')
        app=SSRApplication.from_file(entry,hydrate=True,channels={'/events':channel});path=next(iter(app._client_assets))
        page.evaluate('''async options=>{window.m=await import(options.path);window.handle=m.hydrate(document.querySelector('#app'));window.subscription=m.connectEvents(handle,'/events',{schema:options.schema});}''',{'path':path,'schema':schema})
        page.get_by_text("First",exact=True).wait_for()
        channel.publish({'notice':'Second <safe>'})
        page.get_by_text("Second <safe>",exact=True).wait_for()
        page.evaluate('handle.destroy()')
        assert page.evaluate('subscription.closed')
        channel.publish({'notice':'Third'})
        assert page.locator('#notice').inner_text()=='Second <safe>'
        page.close()


@contextmanager
def public_preview(directory):
    server=preview_server(directory,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}',server
    finally:server.shutdown();server.server_close();thread.join()


def test_real_browser_pwa_public_routes_work_offline_and_cache_is_owned(tmp_path,browser):
    entry=project(tmp_path,'''use nexo.web
pub page Home:
    path = "/"
    view:
        h1("Public home")
        a("Guide", href: "/guide")
pub page Guide:
    path = "/guide"
    view:
        h1("Public guide")
        a("Home", href: "/")
pub page NotFound:
    not_found = true
    view:
        h1("Missing")
''')
    files=build(entry,tmp_path/'dist',static=True,pwa=True)
    manifest=json.loads(files['nexo-manifest.json']);assert manifest['pwa']['policy']=='explicit-public-precache-only'
    with public_preview(tmp_path/'dist') as (base,server):
        context=browser.new_context();page=context.new_page();page.goto(base+'/')
        page.wait_for_selector('html[data-nexo-pwa="ready"]')
        page.reload()
        keys=page.evaluate('caches.keys()');assert any(manifest['pwa']['cache'] in key for key in keys)
        assert page.evaluate('!!navigator.serviceWorker.controller')
        # Stop the real origin: cache behavior must survive an actual outage,
        # independently of browser automation's offline emulation.
        server.shutdown();server.server_close()
        response=page.goto(base+'/guide/')
        assert response.status==200 and page.locator('h1').inner_text()=='Public guide'
        response=page.goto(base+'/unknown/')
        assert response.status==404 and page.locator('h1').inner_text()=='Missing'
        assert not page.evaluate('caches.match("/api/private").then(value=>!!value)')
        context.close()


def test_real_browser_integrated_example_runs_all_host_adapters(browser):
    import runpy
    example=runpy.run_path(str(ROOT/'examples/web/m6/server.py'))
    channel=EventChannel({'kind':'record','fields':{'notice':{'kind':'str'}}},heartbeat=0.05)
    channel.publish({'notice':'Integrated event'})
    boot=(ROOT/'examples/web/m6/host.js').read_text().replace('@@SHA256@@',hashlib.sha256(ADD).hexdigest())
    with custom_server(ROOT/'examples/web/m6/app.nexo',loader=example['load'],wasm=True,
                       channels={'/events':channel},assets={'/m6-host.js':('text/javascript',boot),'/add.wasm':('application/wasm',ADD)},client_boot='/m6-host.js') as base:
        page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(base+'/')
        assert page.locator('h2').inner_text()=='Async server snapshot'
        page.get_by_text('WASM result: 42',exact=True).wait_for()
        page.get_by_text('Integrated event',exact=True).wait_for()
        page.locator('#row-1 button').click()
        page.get_by_role('button',name='Reorder products',exact=True).click()
        assert page.locator('#row-1 button').inner_text()=='Local count: 1'
        assert page.get_by_text('Shared interactions: 1',exact=True).count()==1
        assert page.locator('#row-2 h3').inner_text()=='Two updated'
        page.get_by_role('button',name='Toggle list',exact=True).click()
        page.get_by_role('button',name='Toggle list',exact=True).click()
        assert page.locator('#row-1 button').inner_text()=='Local count: 0'
        assert not errors
        page.close()
    channel.close()


def test_real_browser_unkeyed_rows_recreate_and_nested_ranges_claim(tmp_path,browser):
    source='''use nexo.web
pub page Home:
    path = "/"
    state items: list[int] = [1, 2]
    fn reorder():
        items = [2, 1]
    view:
        main:
            button("Reorder", on_click: reorder)
            for item in items:
                section:
                    for number in [3, 4] key number:
                        p("{item}:{number}")
                    Counter()
component Counter:
    state count: int = 0
    fn increment():
        count += 1
    view:
        button("Count {count}", on_click: increment)
'''
    entry=project(tmp_path,source)
    with served(entry) as base:
        page=browser.new_page();page.goto(base+'/')
        page.wait_for_selector('#app[data-nexo-hydration="ready"]')
        page.get_by_role('button',name='Count 0',exact=True).first.click()
        page.evaluate('window.old=document.querySelector("section")')
        page.get_by_role('button',name='Reorder',exact=True).click()
        assert page.locator('section').first.locator('p').all_text_contents()==['2:3','2:4']
        assert page.get_by_role('button',name='Count 0',exact=True).count()==2
        assert page.evaluate('!old.isConnected')
        page.close()


def test_public_pwa_opt_in_and_default_capabilities(tmp_path):
    from nexo.web_frontend import WebError
    entry=project(tmp_path,'use nexo.web\npub component Home:\n    view:\n        h1("Public")\n')
    plain=build(entry,tmp_path/'plain',static=True)
    assert 'service-worker.js' not in plain and "script-src 'none'" in plain['index.html']
    with pytest.raises(WebError):build(entry,tmp_path/'invalid',pwa=True)
    entry.write_text(entry.read_text().replace('pub component Home:', 'pub page Home:\n    path = \"/\"'))
    app=SSRApplication.from_file(entry)
    assert "worker-src" not in dict(app.response('/').headers)['Content-Security-Policy']


def test_real_browser_throwing_cleanup_still_disposes_every_owner_and_allows_reattach(tmp_path,browser):
    entry=project(tmp_path);app=SSRApplication.from_file(entry,hydrate=True)
    with served(entry) as base:
        page=browser.new_page();page.goto(base+'/counter/1/')
        page.wait_for_selector('#app[data-nexo-hydration="ready"]')
        path=next(iter(app._client_assets))
        result=page.evaluate('''async path=>{
          window.m=await import(path);window.root=document.querySelector('#app');window.handle=m.hydrate(root);
          window.button=root.querySelector('button');window.before=button.textContent;window.cleanupCount=0;
          handle.onDestroy(()=>cleanupCount++);handle.onDestroy(()=>{throw new Error('host cleanup failed');});handle.onDestroy(()=>cleanupCount++);
          try{handle.destroy();return 'unexpected';}catch(error){return {name:error.name,count:error.errors.length};}
        }''',path)
        assert result=={'name':'AggregateError','count':1}
        assert page.evaluate('cleanupCount')==2
        page.evaluate('button.click()')
        assert page.evaluate('button.textContent===before')
        assert page.evaluate('''()=>{try{handle.snapshot();return false;}catch{return true;}}''')
        page.evaluate('handle.destroy();window.next=m.hydrate(root)')
        assert page.evaluate('next!==handle')
        page.get_by_role('button',name='Increment',exact=True).click()
        assert page.get_by_text('Count: 2',exact=True).count()==1
        page.close()
