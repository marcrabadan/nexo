"""M4 compiler maps, audit/LSP and actual Vite/Chromium development lifecycle."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from urllib.request import Request,urlopen

import pytest
from test_web_frontend import browser
from nexo.web_frontend import build,WebError
from nexo.web_tooling import scaffold,preview_server
from nexo.web_bridge import compile_entry
from nexo.web_audit import audit

ROOT=Path(__file__).resolve().parents[1]
COUNTER=(ROOT/'examples/web/counter.nexo').read_text()


def expect(actual):
    from playwright.sync_api import expect as assertion
    return assertion(actual)


def decoded_mappings(mapping):
    alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    source=line=column=0
    for segment in mapping['mappings'].split(';'):
        if not segment:yield None;continue
        values=[];value=shift=0
        for letter in segment:
            digit=alphabet.index(letter);value|=(digit&31)<<shift
            if digit&32:shift+=5
            else:
                values.append(-(value>>1) if value&1 else value>>1);value=shift=0
        source+=values[1];line+=values[2];column+=values[3]
        yield mapping['sources'][source],line+1,column+1


def test_source_maps_relocatable_and_actions_map_to_source(tmp_path):
    values=[]
    for folder in (tmp_path/'one',tmp_path/'two'):
        folder.mkdir();entry=folder/'app.nexo';entry.write_text(COUNTER)
        values.append(build(entry,folder/'dist',True))
    assert values[0]==values[1]
    files=values[0]
    assert 'import.meta.hot' not in files['app.js'] and '/@vite/client' not in files['index.html']
    mapping=json.loads(files['app.js.map'])
    assert mapping['version']==3 and mapping['sources']==['nexo://project/app.nexo']
    assert mapping['sourcesContent']==[COUNTER]
    index=next(i for i,line in enumerate(files['app.js'].splitlines()) if 'api.set("count"' in line)
    assert list(decoded_mappings(mapping))[index]==('nexo://project/app.nexo',COUNTER.splitlines().index('        count += 1')+1,1)
    build(tmp_path/'one/app.nexo',tmp_path/'one/dist')
    assert not (tmp_path/'one/dist/app.js.map').exists()


def test_scaffold_and_style_only_identity(tmp_path):
    project=tmp_path/'demo';scaffold(project)
    assert (project/'app.nexo').read_text()==COUNTER
    assert (project/'package-lock.json').read_bytes()==(ROOT/'nexo/web_tools/package-lock.json').read_bytes()
    first=compile_entry(project/'app.nexo')
    (project/'app.nexo').write_text((project/'app.nexo').read_text().replace('24px','40px'))
    second=compile_entry(project/'app.nexo')
    assert first['javascript']==second['javascript'] and first['css']!=second['css']
    with pytest.raises(WebError,match='already exists'):scaffold(project)


def test_audit_is_redacted_and_honest(tmp_path):
    entry=tmp_path/'app.nexo'
    entry.write_text(COUNTER.replace('    state count:', '    state api_key: str = "fixture-not-a-real-credential"\n    state count:'))
    result=audit(entry)
    assert result['diagnostics'][0]['code']=='NW4101'
    assert 'fixture-not-a-real-credential' not in json.dumps(result)
    assert result['backend_authorization']=='Not assessed'
    assert '6-second' in result['shield']['loopback']
    entry.write_text((ROOT/'examples/web/data/app.nexo').read_text())
    assert [d['code'] for d in audit(entry)['diagnostics']]==['NW4102']
    entry.write_text(COUNTER);assert not audit(entry)['diagnostics']


def test_json_diagnostics_have_source_locations(tmp_path):
    entry=tmp_path/'app.nexo';entry.write_text(COUNTER.replace('count += 1','count += "bad"'))
    out=subprocess.run([sys.executable,'-m','nexo','web','check','--json',str(entry)],cwd=ROOT,capture_output=True,text=True)
    assert out.returncode==1
    d=json.loads(out.stdout)['diagnostics'][0]
    assert d['column']==9 and d['snippet']=='        count += "bad"' and d['end_column']>d['column']


def test_browser_lsp_unsaved_diagnostics_outline_and_safe_completions(tmp_path):
    from lsp_client import Client
    entry=tmp_path/'app.nexo';entry.write_text(COUNTER)
    c=Client([sys.executable,'-m','nexo','web','lsp'],cwd=ROOT);uri=entry.as_uri()
    try:
        caps=c.request('initialize',{'capabilities':{}})['result']['capabilities']
        assert not caps['documentFormattingProvider'] and caps['hoverProvider']
        c.notify('textDocument/didOpen',{'textDocument':{'uri':uri,'version':1,'text':COUNTER,'languageId':'nexo'}})
        assert c.diagnostics(uri)==[]
        symbols=c.request('textDocument/documentSymbol',{'textDocument':{'uri':uri}})['result']
        assert symbols[0]['range']['end']['line']>=max(c['range']['end']['line'] for c in symbols[0]['children'])
        assert symbols[0]['name']=='Counter' and {v['name'] for v in symbols[0]['children']}=={'count','increment'}
        position={'textDocument':{'uri':uri},'position':{'line':6,'character':9}}
        labels={v['label'] for v in c.request('textDocument/completion',position)['result']}
        assert {'load','mutation','component','count'}<=labels
        assert 'http' not in labels and 'secret' not in labels
        broken=COUNTER.replace('count += 1','count += "😀bad"')
        c.notify('textDocument/didChange',{'textDocument':{'uri':uri,'version':2},'contentChanges':[{'text':broken}]})
        [d]=c.diagnostics(uri)
        assert d['code']=='NW1002' and d['range']['start']=={'line':6,'character':8}
    finally:assert c.close()==0


@contextmanager
def vite(entry,tmp_path):
    if not (ROOT/'nexo/web_tools/node_modules/vite/package.json').exists():
        if os.environ.get('NEXO_WEB_REQUIRE_VITE')=='1':pytest.fail('Pinned Vite required: npm ci --prefix nexo/web_tools')
        pytest.skip('Optional Vite is unavailable')
    log=(tmp_path/'dev.log').open('w+')
    proc=subprocess.Popen([sys.executable,'-m','nexo','web','dev',str(entry),'--port','0'],cwd=ROOT,stdout=subprocess.PIPE,stderr=log,text=True)
    ready=[];event=threading.Event()
    def read():
        for line in proc.stdout:
            if line.startswith('Nexo Web dev http://'):ready.append(line.split()[3]);event.set();return
        event.set()
    thread=threading.Thread(target=read,daemon=True);thread.start()
    try:
        if not event.wait(25) or not ready:
            log.flush();log.seek(0);pytest.fail('Vite did not start: '+log.read())
        yield ready[0],proc
    finally:
        proc.terminate()
        try:proc.wait(timeout=8)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        proc.stdout.close();log.close();thread.join(timeout=1)
        if ready:
            import socket
            from urllib.parse import urlsplit
            try:
                connection=socket.create_connection(('127.0.0.1',urlsplit(ready[0]).port),timeout=.5)
            except OSError:pass
            else:
                connection.close();pytest.fail('Development listener survived its CLI owner')


def test_real_browser_vite_hmr_recovery_boundaries_and_maps(tmp_path,browser):
    entry=tmp_path/'app.nexo';entry.write_text(COUNTER)
    (tmp_path/'.env').write_text('VITE_PRIVATE_TEST=should-never-be-served\n')
    with vite(entry,tmp_path) as (base,proc):
        page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base);expect(page.get_by_role('button',name='+',exact=True)).to_be_visible()
        page.get_by_role('button',name='+',exact=True).click();expect(page.locator('p')).to_have_text('Count: 1')
        page.evaluate('window.originalMain=document.querySelector("main");window.oldButton=document.querySelector("button")')
        entry.write_text(COUNTER.replace('24px','40px'))
        expect(page.locator('main')).to_have_css('padding','40px')
        expect(page.locator('p')).to_have_text('Count: 1')
        assert page.evaluate('originalMain===document.querySelector("main")')
        entry.write_text(COUNTER.replace('count += 1','count += "bad"'))
        expect(page.get_by_role('alert')).to_contain_text('NW1002')
        expect(page.locator('p')).to_have_text('Count: 1')
        entry.write_text(COUNTER.replace('count += 1','count += 2'))
        expect(page.get_by_role('alert')).to_have_count(0)
        expect(page.locator('p')).to_have_text('Count: 0')
        page.evaluate('oldButton.click()');expect(page.locator('p')).to_have_text('Count: 0')
        page.get_by_role('button',name='+',exact=True).click();expect(page.locator('p')).to_have_text('Count: 2')
        assert not page.evaluate('originalMain===document.querySelector("main")')
        import base64
        code=page.request.get(base+'@id/__x00__nexo:app').text()
        marker='//# sourceMappingURL=data:application/json;base64,'
        mapping=json.loads(base64.b64decode(code.rsplit(marker,1)[1].strip()))
        assert any('count += 2' in (source or '') for source in mapping['sourcesContent'])
        assert 'should-never-be-served' not in code
        response=page.request.get(base);assert "unsafe-inline" not in response.headers['content-security-policy']
        assert page.request.get(base,headers={'Host':'evil.invalid'}).status==403
        assert page.request.get(base,headers={'Origin':'https://evil.invalid'}).status==403
        assert page.request.get(base+'.env').status in (403,404)
        assert page.request.get(base+'@fs'+str(tmp_path/'.env')).status in (403,404)
        assert page.request.get(base+'app.nexo').status in (403,404)
        assert not errors,errors
        page.close()


def test_preview_only_serves_built_assets(tmp_path):
    entry=tmp_path/'app.nexo';entry.write_text(COUNTER);build(entry,tmp_path)
    (tmp_path/'.env').write_text('not served')
    server=preview_server(tmp_path,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}/'
    try:
        with urlopen(base) as response:assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
        from urllib.error import HTTPError
        for path in ('.env','app.nexo','../app.nexo'):
            with pytest.raises(HTTPError) as error:urlopen(base+path)
            assert error.value.code==404
        with pytest.raises(HTTPError) as error:urlopen(Request(base,headers={'Host':'evil.invalid'}))
        assert error.value.code==403
    finally:server.shutdown();server.server_close();thread.join()


def test_real_browser_vite_imported_css_and_missing_module_recovery(tmp_path,browser):
    (tmp_path/'nexo.toml').write_text('[project]\nname="hmr"\n')
    entry=tmp_path/'app.nexo';entry.write_text('use nexo.web\nuse widget\npub component App:\n    view:\n        widget.Card()\n')
    widget=tmp_path/'widget.nexo'
    widget.write_text('use nexo.web\npub component Card:\n    state count: int = 0\n    fn increment():\n        count += 1\n    view:\n        button("Count: {count}", on_click: increment)\n    style:\n        @import "./card.css";\n')
    css=tmp_path/'card.css';css.write_text('button { padding: 12px; }')
    with vite(entry,tmp_path) as (base,_):
        page=browser.new_page();page.goto(base)
        page.get_by_role('button',name='Count: 0',exact=True).click()
        expect(page.get_by_role('button',name='Count: 1',exact=True)).to_be_visible()
        css.write_text('button { padding: 32px; }')
        expect(page.locator('button')).to_have_css('padding','32px')
        expect(page.locator('button')).to_have_text('Count: 1')
        entry.write_text('use nexo.web\nuse missing\npub component App:\n    view:\n        missing.Label()\n')
        expect(page.get_by_role('alert')).to_contain_text('NW2001')
        (tmp_path/'missing.nexo').write_text('use nexo.web\npub component Label:\n    view:\n        h1("Recovered module")\n')
        expect(page.get_by_role('heading',name='Recovered module')).to_be_visible()
        page.close()


def test_browser_lsp_imported_diagnostic_and_local_definition(tmp_path):
    from lsp_client import Client
    entry=tmp_path/'app.nexo';entry.write_text('use nexo.web\nuse widget\npub component App:\n    view:\n        widget.Card()\n')
    widget=tmp_path/'widget.nexo';widget.write_text(COUNTER.replace('pub component Counter:', 'pub component Card:').replace('count += 1','count += "bad"'))
    c=Client([sys.executable,'-m','nexo','web','lsp'],cwd=ROOT);uri=entry.as_uri()
    try:
        c.request('initialize',{'capabilities':{}})
        c.notify('textDocument/didOpen',{'textDocument':{'uri':uri,'version':1,'text':entry.read_text(),'languageId':'nexo'}})
        [d]=c.diagnostics(uri)
        assert d['relatedInformation'][0]['location']['uri']==widget.as_uri()
        assert d['range']['start']=={'line':0,'character':0}
        c.notify('textDocument/didChange',{'textDocument':{'uri':uri,'version':2},'contentChanges':[{'text':COUNTER}]})
        assert c.diagnostics(uri)==[]
        pos={'textDocument':{'uri':uri},'position':{'line':12,'character':35}}
        # Local identifier on a known source line; definition stays in this document.
        index=COUNTER.splitlines().index('            button("+", on_click: increment)')
        pos['position']={'line':index,'character':COUNTER.splitlines()[index].index('increment')}
        result=c.request('textDocument/definition',pos)['result']
        assert result['uri']==uri and result['range']['start']['line']==5
    finally:assert c.close()==0



def test_audit_compile_errors_also_redact_source(tmp_path):
    entry=tmp_path/'app.nexo'
    entry.write_text(COUNTER.replace('state count: int = 0','state api_key: int = "fixture-private-literal"'))
    result=subprocess.run([sys.executable,'-m','nexo','web','audit','--security','--json',str(entry)],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode==1 and 'fixture-private-literal' not in result.stdout
    assert json.loads(result.stdout)['diagnostics'][0]['snippet'].startswith('[redacted:')
