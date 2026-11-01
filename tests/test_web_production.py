"""M5 release reproducibility, real-browser accessibility/telemetry and deployment."""
from contextlib import contextmanager
import functools
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest
from test_web_frontend import browser
from test_web_data import served as api_server
from nexo.web_frontend import build,WebError
from nexo.web_release import read_release,MANIFEST
from nexo.web_audit import audit
from nexo.web_tooling import preview_server

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'examples/web/production/app.nexo'
COUNTER=ROOT/'examples/web/counter.nexo'
AXE=ROOT/'nexo/web_tools/node_modules/axe-core/axe.min.js'


@pytest.fixture(autouse=True)
def require_optimizer():
    if not (ROOT/'nexo/web_tools/node_modules/vite/package.json').exists():
        if os.environ.get('NEXO_WEB_REQUIRE_VITE')=='1':pytest.fail('Pinned Vite is required')
        pytest.skip('Optional release optimizer unavailable')


def expect(value):
    from playwright.sync_api import expect as assertion
    return assertion(value)


@contextmanager
def served(folder):
    handler=functools.partial(SimpleHTTPRequestHandler,directory=str(folder))
    server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/'
    finally:server.shutdown();server.server_close();thread.join()


def test_release_is_relocatable_minified_and_integrity_checked(tmp_path):
    outputs=[]
    for name in ('one','two'):
        folder=tmp_path/name;folder.mkdir();entry=folder/'app.nexo';entry.write_text(COUNTER.read_text())
        outputs.append(build(entry,folder/'dist',release=True))
    assert outputs[0]==outputs[1]
    files=outputs[0];manifest=json.loads(files[MANIFEST]);raw=build(COUNTER,tmp_path/'debug')
    js=[name for name in manifest['files'] if name.endswith('.js')]
    assert len(js)==1 and manifest['files'][js[0]]['bytes']<len(raw['app.js'].encode())
    assert 'integrity="sha384-' in files['index.html'] and 'unsafe-inline' not in files['index.html']
    assert not any(name.endswith('.map') for name in files)
    assert 'nexo:web' not in files[js[0]]
    assert read_release(tmp_path/'one/dist')==files
    (tmp_path/'one/dist'/js[0]).write_text('changed')
    with pytest.raises(WebError,match='modified'):read_release(tmp_path/'one/dist')


def test_release_maps_preserve_original_sources_and_are_relocatable(tmp_path):
    outputs=[]
    for name in ('one','two'):
        folder=tmp_path/name;shutil.copytree(ROOT/'examples/web/composition',folder)
        outputs.append(build(folder/'app.nexo',folder/'dist',sourcemap=True,release=True))
    assert outputs[0]==outputs[1]
    maps=[json.loads(text) for name,text in outputs[0].items() if name.endswith('.map')]
    assert len(maps)==1
    assert {'nexo://project/app.nexo','nexo://project/session.nexo','nexo://project/ui/widgets.nexo'}<=set(maps[0]['sources'])
    assert not any('nexo-map-source' in content for content in maps[0]['sourcesContent'])
    assert any('pub page User' in content for content in maps[0]['sourcesContent'])
    plain=build(ROOT/'examples/web/composition/app.nexo',tmp_path/'plain',release=True)
    assert {name for name in outputs[0] if name.endswith('.js')}.isdisjoint(name for name in plain if name.endswith('.js'))


def test_release_rebuild_removes_old_assets_and_preserves_unrelated_files(tmp_path):
    out=tmp_path/'dist';entry=tmp_path/'app.nexo';entry.write_text(COUNTER.read_text())
    first=build(entry,out,release=True)
    entry.write_text(COUNTER.read_text().replace('count += 1','count += 2'))
    second=build(entry,out,release=True)
    assert set(read_release(out))==set(second)
    assert not any((out/name).exists() for name in first if name not in second)
    (out/'keep.txt').write_text('user file')
    with pytest.raises(WebError,match='unrelated'):build(entry,out,release=True)
    assert (out/'keep.txt').read_text()=='user file'
    with pytest.raises(WebError,match='dedicated'):build(entry,entry,release=True)
    assert 'count += 2' in entry.read_text()


@pytest.mark.parametrize('source,code',[
 ('button("")','NW5101'),('input()','NW5101'),('button("Go", tabindex: 2)','NW5102'),('button("Go", aria_hidden: "true")','NW5103')
])
def test_static_accessibility_audit(source,code,tmp_path):
    entry=tmp_path/'app.nexo';entry.write_text('use nexo.web\npub component App:\n    view:\n        '+source+'\n')
    result=audit(entry,security=False,accessibility=True)
    assert code in {finding['code'] for finding in result['diagnostics']}
    assert result['accessibility']=='Static checks only'


def test_accessible_input_labels_and_audit_cli(tmp_path):
    entry=tmp_path/'app.nexo';entry.write_text('use nexo.web\npub component App:\n    view:\n        main:\n            label("Name", for: "name")\n            input(id: "name")\n            h2("Search", id: "search-label")\n            input(aria_labelledby: "search-label")\n')
    assert not audit(entry,accessibility=True)['diagnostics']
    result=subprocess.run([sys.executable,'-m','nexo','web','audit','--accessibility','--json',str(entry)],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode==0 and not json.loads(result.stdout)['diagnostics']


def test_real_browser_release_accessibility_keyboard_and_subpath(tmp_path,browser):
    out=tmp_path/'site/app';build(APP,out,release=True)
    if not AXE.exists():pytest.fail('Pinned axe-core required for accessibility verification')
    shutil.copy(AXE,tmp_path/'axe.js')
    with served(tmp_path) as base:
        page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base+'site/app/')
        expect(page.get_by_role('button',name='Increment',exact=True)).to_be_visible()
        page.keyboard.press('Tab');expect(page.get_by_role('button',name='Increment',exact=True)).to_be_focused()
        page.keyboard.press('Enter');expect(page.get_by_role('status')).to_have_text('Count: 1')
        page.add_script_tag(url=base+'axe.js')
        results=page.evaluate("async()=>{const result=await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa','best-practice']}});return {violations:result.violations,incomplete:result.incomplete.map(x=>x.id)};}")
        assert results['violations']==[],results['violations']
        scoped=page.evaluate("()=>{const outside=document.createElement('button');outside.textContent='Outside';document.body.append(outside);const inside=getComputedStyle(document.querySelector('main button')).backgroundColor,external=getComputedStyle(outside).backgroundColor;outside.remove();return {inside,external};}")
        assert scoped['inside']=='rgb(13, 71, 161)' and scoped['external']!=scoped['inside']
        assert not errors,errors
        page.close()


def test_real_browser_observation_is_opt_in_redacted_and_local(tmp_path,browser):
    source=(ROOT/'examples/web/data/app.nexo').read_text().replace('cache_ms: 1000','cache_ms: 0')
    entry=tmp_path/'app.nexo';entry.write_text(source)
    for enabled in (False,True):
        build(entry,tmp_path/'dist',release=True,observe=enabled)
        with api_server(tmp_path/'dist') as (base,_):
            page=browser.new_page();requests=[];page.on('request',lambda req:requests.append(req.url))
            page.add_init_script("window.events=[];window.addEventListener('nexo:web',e=>window.events.push(e.detail));")
            page.goto(base);expect(page.locator('li')).to_have_count(1)
            events=page.evaluate('events')
            if enabled:
                assert any(e['kind']=='request' and e['phase']=='ready' for e in events)
                assert any(e['kind']=='mount' and e['phase']=='ready' for e in events)
                assert all(set(e)<={'schema_version','kind','phase','operation','duration_ms','code','status'} for e in events)
                assert '/api/' not in json.dumps(events) and 'img' not in json.dumps(events)
            else:assert events==[]
            assert all(url.startswith(base) for url in requests)
            page.close()


def test_real_browser_release_preview_and_sri_tampering(tmp_path,browser):
    files=build(APP,tmp_path/'dist',release=True)
    server=preview_server(tmp_path/'dist',0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}/'
    try:
        page=browser.new_page();page.goto(base);expect(page.get_by_role('button',name='Increment',exact=True)).to_be_visible()
        assert page.request.get(base+'app.nexo').status==404
        page.close()
    finally:server.shutdown();server.server_close();thread.join()
    # A static host cannot bypass browser SRI if an asset changes after publication.
    js=next(name for name in files if name.endswith('.js'))
    (tmp_path/'dist'/js).write_text('window.compromised=true;')
    with served(tmp_path/'dist') as base:
        page=browser.new_page();page.goto(base);page.wait_for_load_state()
        assert page.evaluate('window.compromised===undefined')
        assert page.get_by_role('button',name='Increment',exact=True).count()==0
        page.close()


@contextmanager
def nginx_container(tmp_path):
    import uuid
    docker=shutil.which('docker')
    if docker is None:
        if os.environ.get('NEXO_WEB_REQUIRE_DOCKER')=='1':pytest.fail('Docker required for deployment checks')
        pytest.skip('Optional local Docker daemon unavailable')
    env=dict(os.environ)
    for name in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):env.pop(name,None)
    env['BUILDX_CONFIG']=str(tmp_path/'buildx-state')
    prefix=[docker,'--host=unix:///var/run/docker.sock']
    def run(*args,timeout=120):return subprocess.run([*prefix,*args],env=env,capture_output=True,text=True,timeout=timeout)
    if run('info','--format','{{.ServerVersion}}').returncode:
        if os.environ.get('NEXO_WEB_REQUIRE_DOCKER')=='1':pytest.fail('Managed local Docker daemon unavailable')
        pytest.skip('Optional local Docker daemon unavailable')
    context=tmp_path/'container';context.mkdir();build(APP,context/'dist',release=True)
    for name in ('Dockerfile','nginx.conf'):shutil.copy(APP.parent/name,context/name)
    tag='nexo-web-check-'+uuid.uuid4().hex[:12];container=None
    try:
        result=run('build','--pull=false','-t',tag,str(context))
        assert result.returncode==0,result.stderr
        result=run('run','-d','--read-only','--user','101:101','--cap-drop','ALL','--security-opt','no-new-privileges',
                   '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777','-p','127.0.0.1::8080',tag)
        assert result.returncode==0,result.stderr
        container=result.stdout.strip()
        result=run('exec',container,'nginx','-t');assert result.returncode==0,result.stderr+run('logs',container).stderr
        port=json.loads(run('inspect','--format','{{json .NetworkSettings.Ports}}',container).stdout)['8080/tcp'][0]['HostPort']
        from urllib.request import urlopen
        base=f'http://127.0.0.1:{port}/'
        for attempt in range(40):
            try:
                with urlopen(base+'healthz',timeout=.3) as response:
                    if response.status==200:break
            except OSError:pass
            threading.Event().wait(.05)
        else:pytest.fail('Nginx readiness failed: '+run('logs',container).stderr)
        yield base,run,container,context/'dist'
    finally:
        if container:run('rm','-f',container)
        run('image','rm',tag)


def test_real_browser_nginx_readonly_deployment_headers_health_logs(tmp_path,browser):
    with nginx_container(tmp_path) as (base,docker,container,dist):
        assert docker('inspect','--format','{{.Config.User}} {{.HostConfig.ReadonlyRootfs}}',container).stdout.strip()=='101:101 true'
        status=docker('exec',container,'cat','/proc/1/status').stdout
        assert 'NoNewPrivs:\t1' in status and 'CapEff:\t0000000000000000' in status
        page=browser.new_page();page.goto(base)
        expect(page.get_by_role('button',name='Increment',exact=True)).to_be_visible()
        page.get_by_role('button',name='Increment',exact=True).click()
        expect(page.get_by_role('status')).to_have_text('Count: 1')
        health=page.request.get(base+'healthz');assert health.status==200 and health.text()=='ok\n'
        manifest=json.loads((dist/MANIFEST).read_text())
        for name in manifest['files']:
            response=page.request.get(base+name)
            assert response.status==200 and "frame-ancestors 'none'" in response.headers['content-security-policy']
            assert response.headers['x-content-type-options']=='nosniff'
            assert response.headers['cache-control']==('public, max-age=31536000, immutable' if name.startswith('assets/') else 'no-cache')
        asset=next(name for name in manifest['files'] if name.endswith('.js'))
        compressed=page.request.get(base+asset,headers={'Accept-Encoding':'gzip'})
        assert compressed.headers.get('content-encoding')=='gzip'
        assert 'Accept-Encoding' in compressed.headers.get('vary','')
        assert len(compressed.body())==manifest['files'][asset]['bytes']
        denied=page.request.get(base+'.env');assert denied.status==403 and 'content-security-policy' in denied.headers
        missing=page.request.get(base+'assets/missing.js')
        assert missing.status==404 and missing.headers['cache-control']=='no-store'
        assert page.request.post(base,data='denied').status==403
        logs=docker('logs',container).stdout.splitlines()
        records=[json.loads(line) for line in logs if line.startswith('{')]
        assert records and all(set(record)=={'status','bytes','duration','method'} for record in records)
        page.close()



def test_switching_release_to_development_removes_maps_and_manifest(tmp_path):
    out=tmp_path/'dist'
    released=build(APP,out,release=True,sourcemap=True)
    assert any(name.endswith('.map') for name in released)
    development=build(APP,out)
    assert set(development)=={'index.html','app.js','app.css','main.js'}
    assert not (out/MANIFEST).exists() and not (out/'assets').exists()


def test_unrelated_label_does_not_name_an_input(tmp_path):
    entry=tmp_path/'app.nexo';entry.write_text('use nexo.web\npub component App:\n    view:\n        label("Other field")\n        input()\n')
    assert any(finding['code']=='NW5101' for finding in audit(entry,accessibility=True)['diagnostics'])


def test_real_browser_release_composition_routes_and_context(tmp_path,browser):
    build(ROOT/'examples/web/composition/app.nexo',tmp_path/'dist',release=True)
    with served(tmp_path/'dist') as base:
        page=browser.new_page();page.goto(base)
        page.get_by_role('button',name='Shared +',exact=True).click()
        expect(page.locator('header p')).to_contain_text('Shared visits: 1')
        page.get_by_role('link',name='User 7',exact=True).click()
        expect(page.get_by_role('heading',name='User 7',exact=True)).to_be_visible()
        expect(page.locator('main')).to_contain_text('Shared on another page: 1')
        assert page.evaluate('document.activeElement.tagName')=='MAIN'
        page.get_by_role('link',name='Home',exact=True).click()
        expect(page.locator('article')).to_contain_text('Local count: 0')
        page.close()


def test_real_browser_release_mutation_csrf_and_invalidation(tmp_path,browser):
    build(ROOT/'examples/web/data/app.nexo',tmp_path/'dist',release=True)
    with api_server(tmp_path/'dist') as (base,counts):
        page=browser.new_page();page.goto(base);expect(page.locator('li')).to_have_count(1)
        page.get_by_role('button',name='Create product',exact=True).click()
        expect(page.get_by_role('alert')).to_contain_text('Security:')
        assert counts['POST']==0
        page.evaluate('const m=document.createElement("meta");m.name="nexo-csrf-token";m.content="test-token";document.head.append(m)')
        page.get_by_role('button',name='Create product',exact=True).click()
        expect(page.locator('li')).to_have_count(2)
        expect(page.get_by_text('Created: Nexo handbook',exact=True)).to_be_visible()
        assert counts['POST']==1 and counts['/api/products']==2
        page.close()


def test_real_browser_observation_sanitizes_foreign_error_codes(tmp_path,browser):
    build(ROOT/'examples/web/data/app.nexo',tmp_path/'dist',release=True,observe=True)
    with served(tmp_path/'dist') as base:
        page=browser.new_page()
        page.add_init_script("window.events=[];window.addEventListener('nexo:web',e=>events.push(e.detail));window.fetch=()=>Promise.reject({kind:'fixture-private-value',status:500});")
        page.goto(base);expect(page.get_by_role('alert')).to_be_visible()
        events=page.evaluate('events')
        assert any(event.get('code')=='Unknown' for event in events)
        assert 'fixture-private-value' not in json.dumps(events)
        page.close()


def test_quality_report_freshness_and_gate_rejects_regressions():
    import importlib.util
    import copy
    from nexo.web_frontend import artifacts
    from nexo.web_release import optimizer_revision,input_revision
    report=json.loads((ROOT/'docs/web/quality-report.json').read_text())
    assert report['optimizer_sha256']==optimizer_revision()
    for fixture in report['fixtures'].values():
        raw,_=artifacts(ROOT/fixture['entry'])
        assert fixture['input_sha256']==input_revision(raw)
    spec=importlib.util.spec_from_file_location('web_measure',ROOT/'scripts/web/measure.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert not module.checks(report,report['budgets'])
    regressed=copy.deepcopy(report)
    regressed['fixtures']['counter']['release']['javascript']['gzip_bytes']=report['budgets']['javascript_gzip_bytes_max']+1
    regressed['counter']['release']['text_mutations']=[101]
    regressed['accessibility']['violations']=['test-rule']
    assert len(module.checks(regressed,report['budgets']))==3


@pytest.mark.parametrize('theme',['light','dark'])
def test_real_browser_public_docs_accessibility(tmp_path,browser,theme):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(ROOT/'docs'),**kwargs)
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path=='/__quality_axe.js':
                data=AXE.read_bytes();self.send_response(200);self.send_header('Content-Type','text/javascript');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            else:super().do_GET()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        page=browser.new_page(viewport={'width':1280,'height':900})
        for name in ('web.html','ai-guide.html'):
            page.goto(f'http://127.0.0.1:{server.server_port}/'+name)
            page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
            page.add_script_tag(url=f'http://127.0.0.1:{server.server_port}/__quality_axe.js')
            violations=page.evaluate("async()=>{const r=await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa','best-practice']}});return r.violations.map(x=>({id:x.id,nodes:x.nodes.slice(0,3).map(n=>({target:n.target,summary:n.failureSummary}))}));}")
            assert violations==[],(name,theme,violations)
        page.close()
    finally:server.shutdown();server.server_close();thread.join()


def test_individual_guide_project_reconstructs_production_sources(tmp_path):
    import hashlib
    versions=json.loads((ROOT/'docs/ai-guide/versions.json').read_text())
    folder=ROOT/'docs/ai-guide/versions'/('v1-'+versions['current'][:20])
    payload=json.loads((folder/'web-examples/web-production.json').read_text())
    example=payload['example']
    entry=tmp_path/example['entry_file'];entry.write_text(example['source'])
    for name,source in example['files'].items():(tmp_path/name).write_text(source)
    from nexo.web_frontend import compile_web
    js,css,_=compile_web(entry.read_text(),entry)
    assert hashlib.sha256(js.encode()).hexdigest()==example['verification']['javascript_sha256']
    assert {'nexo.toml','Dockerfile','nginx.conf','README.md'}==set(example['files'])
    assert payload['revision']==versions['current']


def test_docs_and_source_audit_work_without_node(monkeypatch):
    import importlib.util
    monkeypatch.setenv('PATH','/nonexistent')
    assert not audit(APP,accessibility=True)['diagnostics']
    spec=importlib.util.spec_from_file_location('web_docs',ROOT/'docs/web/build.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert 'docs/web-release-demo/index.html' in module.generate()
