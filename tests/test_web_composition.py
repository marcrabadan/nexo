"""M2 compiler and real-browser composition/routing/context acceptance tests."""
from contextlib import contextmanager
import functools
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading

import pytest
from test_web_frontend import browser
from nexo.web_frontend import compile_web,build,WebError
from nexo.web_model import load_program

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'examples/web/composition/app.nexo'
BASIC='''use nexo.web

component Label(text: str):
    view:
        p(text)

pub component App:
    view:
        Label(text: "Hello")
'''


@contextmanager
def served(folder):
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(SimpleHTTPRequestHandler,directory=str(folder)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/'
    finally:server.shutdown();server.server_close();thread.join()


def title_is(page,value):
    from playwright.sync_api import expect
    expect(page).to_have_title(value)


def test_m2_imports_css_and_determinism(tmp_path):
    outputs=[]
    for name in ('one','two'):
        project=tmp_path/name;shutil.copytree(APP.parent,project)
        outputs.append(build(project/'app.nexo',project/'dist'))
    assert outputs[0]==outputs[1]
    program=load_program(APP.read_text(),APP)
    assert {p.name for p in program.dependencies} >= {'app.nexo','session.nexo','widgets.nexo','card.css','nexo.toml'}
    assert len(program.units)==3
    assert '@import' not in outputs[0]['app.css']
    assert 'border-radius: 12px' in outputs[0]['app.css']
    assert 'mountApp' in outputs[0]['main.js']
    assert 'innerHTML' not in outputs[0]['app.js']


@pytest.mark.parametrize('old,new,code',[
    ('Label(text: "Hello")','Label()','NW2002'),
    ('Label(text: "Hello")','Label(text: 3)','NW2002'),
    ('Label(text: "Hello")','Label(other: "Hello")','NW2002'),
    ('    view:\n        p(text)','    fn overwrite():\n        text = "Bad"\n\n    view:\n        p(text)','NW1001'),
    ('        Label(text: "Hello")','        Label(text: "Hello"):\n            p("Unsupported content")','NW2005'),
    ('        Label(text: "Hello")','        slot()','NW2005'),
])
def test_props_and_slots_diagnostics(old,new,code):
    source=BASIC.replace(old,new)
    # A slot on a component is valid; test invalid arguments instead.
    if new=='        slot()':source=source.replace('slot()','slot("bad")')
    with pytest.raises(WebError) as error:compile_web(source)
    assert error.value.code==code
    assert 'Fix:' in str(error.value)


def test_context_missing_provider_and_readonly_shared_state():
    context='''use nexo.web
context Session:
    state count: int = 0
    fn increment():
        count += 1
pub component App:
    session = inject(Session)
    view:
        p("{session.count}")
'''
    with pytest.raises(WebError,match='Missing provider'):compile_web(context)
    compile_web(context.replace('    session = inject','    provide Session\n    session = inject'))
    with pytest.raises(WebError):compile_web(context.replace('    session = inject','    provide Session\n    fn bad():\n        session.count = 9\n    session = inject'))


@pytest.mark.parametrize('addition',[
    '''pub page Other(name: str):
    path = "/users/{name}"
    view:
        p(name)
''',
    '''pub page Other:
    path = "/unused/{id}"
    view:
        p("Bad parameter")
''',
])
def test_ambiguous_or_invalid_routes(addition):
    source='''use nexo.web
pub page User(id: int):
    path = "/users/{id}"
    view:
        p("{id}")
'''
    with pytest.raises(WebError) as error:compile_web(source+addition)
    assert error.value.code=='NW2003'


def test_import_privacy_cycles_and_project_boundary(tmp_path):
    (tmp_path/'nexo.toml').write_text('[project]\nname="demo"\n')
    entry=tmp_path/'app.nexo'
    module=tmp_path/'widget.nexo'
    source='use nexo.web\nuse widget\npub component App:\n    view:\n        widget.Label(text: "Hello")\n'
    module.write_text(BASIC.split('pub component App:')[0])
    with pytest.raises(WebError,match='private'):compile_web(source,entry)
    module.write_text('use nexo.web\nuse app\n')
    entry.write_text(source)
    with pytest.raises(WebError,match='cycle'):compile_web(source,entry)
    outside=tmp_path.parent/'outside.nexo';outside.write_text(BASIC)
    module.unlink();module.symlink_to(outside)
    with pytest.raises(WebError,match='project root'):compile_web(source,entry)


def test_css_import_boundaries_and_cycles(tmp_path):
    app=tmp_path/'app.nexo'
    source='use nexo.web\npub component App:\n    view:\n        p("Hello")\n    style:\n        @import "./a.css";\n'
    (tmp_path/'a.css').write_text('@import "./a.css";')
    with pytest.raises(WebError,match='cycle'):compile_web(source,app)
    (tmp_path/'a.css').write_text('@import "../outside.css";')
    with pytest.raises(WebError,match='project root'):compile_web(source,app)


def test_recursive_components_and_layout_cycles():
    with pytest.raises(WebError,match='Recursive'):
        compile_web('use nexo.web\npub component App:\n    view:\n        App()\n')
    with pytest.raises(WebError,match='Layout cycle'):
        compile_web('use nexo.web\nlayout Shell:\n    layout = Shell\n    view:\n        slot()\npub page Home:\n    path = "/"\n    layout = Shell\n    view:\n        p("Home")\n')


def test_real_browser_composition_routes_slots_and_cleanup(tmp_path,browser):
    build(APP,tmp_path)
    (tmp_path/'main.js').write_text('import {mountApp} from "./app.js"; window.app=mountApp(document.querySelector("#app"));')
    with served(tmp_path) as base:
        page=browser.new_page();errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.goto(base);page.wait_for_selector('article')
            assert page.title()=='Nexo Web — Home'
            assert page.locator('article').evaluate('(n)=>getComputedStyle(n).borderRadius')=='12px'
            page.evaluate('window.shell=document.querySelector("header"); window.old=document.querySelector("article button");')
            page.get_by_role('button',name='Shared +',exact=True).click()
            assert 'Shared visits: 1' in page.locator('header p').inner_text()
            assert 'Shared in slot: 1' in page.locator('article').inner_text()
            page.get_by_role('button',name='Local +',exact=True).click()
            assert 'Local count: 1' in page.locator('article').inner_text()
            page.get_by_role('button',name='Rename',exact=True).click()
            assert page.locator('article h2').inner_text()=='Updated count'
            assert 'Updated count: 1' in page.locator('article').inner_text()
            page.get_by_role('button',name='Layout +',exact=True).click()
            page.get_by_role('link',name='User 7',exact=True).click()
            title_is(page, 'Nexo Web — User 7')
            assert page.evaluate('shell===document.querySelector("header")')
            assert page.evaluate('document.activeElement.tagName')=='MAIN'
            assert 'layout clicks: 1' in page.locator('header p').inner_text()
            assert page.get_by_role('heading',name='User 7',exact=True).is_visible()
            assert 'Shared on another page: 1' in page.locator('main').inner_text()
            page.evaluate('old.click()')
            assert 'Shared visits: 1' in page.locator('header p').inner_text()
            page.evaluate('location.hash="/users/8"')
            title_is(page, 'Nexo Web — User 8')
            assert page.get_by_role('heading',name='User 8',exact=True).is_visible()
            page.evaluate('location.hash="/users/not-an-int"')
            title_is(page, 'Nexo Web — Not found')
            assert page.get_by_role('heading',name='Page not found').is_visible()
            page.go_back();title_is(page, 'Nexo Web — User 8')
            page.get_by_role('link',name='Home',exact=True).click()
            page.wait_for_selector('article')
            assert 'Local count: 0' in page.locator('article').inner_text()
            assert 'Shared visits: 1' in page.locator('header p').inner_text()
            page.evaluate('app.destroy(); app.destroy(); location.hash="/users/9"')
            page.wait_for_timeout(50)
            assert page.locator('#app').inner_text()==''
            assert not errors
        finally:page.close()


def test_real_browser_provider_isolation_and_host_props(tmp_path,browser):
    source='''use nexo.web
context Session:
    state count: int = 0
    fn increment():
        count += 1
pub component App(label: str = "Counter"):
    provide Session
    session = inject(Session)
    view:
        p("{label}: {session.count}")
        button("+", on_click: session.increment)
'''
    app=tmp_path/'app.nexo';app.write_text(source);build(app,tmp_path)
    (tmp_path/'main.js').write_text('import {App} from "./app.js"; window.one=App(document.querySelector("#app"),{label:"One"}); window.two=App(document.querySelector("#app"),{label:"Two"}); window.App=App;')
    with served(tmp_path) as base:
        page=browser.new_page()
        try:
            page.goto(base);page.wait_for_selector('button')
            page.locator('button').first.click()
            assert page.locator('p').all_inner_texts()==['One: 1','Two: 0']
            page.evaluate('one.update({label:"Changed"})')
            assert page.locator('p').first.inner_text()=='Changed: 1'
            assert page.evaluate('''() => {try{App(document.body,{label:42});return false;}catch(e){return e instanceof TypeError;}}''')
            page.evaluate('one.destroy(); two.destroy()')
            assert page.locator('#app').inner_text()==''
        finally:page.close()


def test_real_browser_errors_parameter_validation_and_nested_layout_lifetime(tmp_path,browser):
    project=tmp_path/'project';shutil.copytree(APP.parent,project)
    source=(project/'app.nexo').read_text()+'''\npub page Broken:
    path = "/broken"
    layout = Shell
    state value: int = 9223372036854775807 + 1
    view:
        p("{value}")

pub page Echo(text: str):
    path = "/echo/{text}"
    title = "Echo {text}"
    view:
        p(text)

pub page Flag(enabled: bool):
    path = "/flag/{enabled}"
    view:
        p("{enabled}")
'''
    (project/'app.nexo').write_text(source);build(project/'app.nexo',tmp_path/'dist')
    with served(tmp_path/'dist') as base:
        page=browser.new_page()
        try:
            page.goto(base+'#/users/7');page.wait_for_selector('button')
            page.get_by_role('button',name='Nested +',exact=True).click()
            page.evaluate('window.nested=document.querySelector("section h2"); location.hash="/users/8"')
            title_is(page, 'Nexo Web — User 8')
            assert page.evaluate('nested===document.querySelector("section h2")')
            assert 'Nested layout clicks: 1' in page.locator('body').inner_text()
            for path in ['/users/9223372036854775808','/users/7.5','/users/%2F','/users/%00']:
                page.evaluate('(p)=>location.hash=p',path)
                title_is(page, 'Nexo Web — Not found')
            page.evaluate('location.hash="/broken"')
            title_is(page, 'Nexo Web — Error')
            assert page.locator('header').count()==0
            page.evaluate('location.hash="/"');page.wait_for_selector('article')
            assert 'Shared visits: 0' in page.locator('header p').inner_text()
            page.evaluate('location.hash="/echo/%3Cimg%20src=x%20onerror=alert(1)%3E"')
            title_is(page, 'Echo <img src=x onerror=alert(1)>')
            assert page.locator('#app p').inner_text()=='<img src=x onerror=alert(1)>'
            assert page.locator('img').count()==0
            page.evaluate('location.hash="/flag/true"');page.get_by_text('true',exact=True).wait_for()
            page.evaluate('location.hash="/flag/not-bool"');title_is(page, 'Nexo Web — Not found')
        finally:page.close()


def test_real_browser_nearest_provider_and_cross_owner_batching(tmp_path,browser):
    source='''use nexo.web
context Count:
    state value: int = 0
    fn increment():
        value += 1
component Reader:
    count = inject(Count)
    view:
        p("Value: {count.value}")
        button("+", on_click: count.increment)
component Private:
    provide Count
    view:
        Reader()
pub component App:
    provide Count
    count = inject(Count)
    fn twice():
        count.increment()
        count.increment()
    view:
        Reader()
        Private()
        button("Twice", on_click: twice)
'''
    path=tmp_path/'app.nexo';path.write_text(source);build(path,tmp_path)
    with served(tmp_path) as base:
        page=browser.new_page()
        try:
            page.goto(base);page.wait_for_selector('button')
            page.evaluate('''() => {window.records=[];new MutationObserver(r=>records.push(...r.map(v=>v.target.data))).observe(document.querySelector('p'),{subtree:true,characterData:true});}''')
            page.get_by_role('button',name='Twice',exact=True).click()
            assert page.locator('p').all_inner_texts()==['Value: 2','Value: 0']
            assert page.evaluate('records')==['Value: 2']
            page.get_by_role('button',name='+',exact=True).nth(1).click()
            assert page.locator('p').all_inner_texts()==['Value: 2','Value: 1']
        finally:page.close()


def test_guide_m2_project_is_reconstructible_and_dependencies_are_hashed(tmp_path):
    import hashlib
    versions=json.loads((ROOT/'docs/ai-guide/versions.json').read_text())
    folder=ROOT/'docs/ai-guide/versions'/('v1-'+versions['current'][:20])
    pack=json.loads((folder/'web-examples.json').read_text())
    example=next(e for e in pack['examples'] if e['id']=='web-composition')
    assert 'app.nexo' not in example['files']  # entry source is not duplicated
    assert set(example['files'])=={'nexo.toml','session.nexo','ui/widgets.nexo','ui/card.css'}
    entry=tmp_path/example['entry_file'];entry.write_text(example['source'])
    for name,text in example['files'].items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    js,css,_=compile_web(entry.read_text(),entry)
    assert hashlib.sha256(js.encode()).hexdigest()==example['verification']['javascript_sha256']
    assert hashlib.sha256(css.encode()).hexdigest()==example['verification']['css_sha256']
    manifest=json.loads((folder/'manifest.json').read_text())
    for name in ('app.nexo','session.nexo','ui/widgets.nexo','ui/card.css','nexo.toml'):
        assert 'examples/web/composition/'+name in manifest['sources']
    assert 'nexo/web_router.js' in manifest['sources']


def test_reserved_module_alias_and_escaped_route_pattern(tmp_path):
    (tmp_path/'widget.nexo').write_text('use nexo.web\npub context Count:\n    state value: int = 0\n')
    source='use nexo.web\nuse widget\npub component App:\n    state widget: int = 0\n    view:\n        p("Hello")\n'
    with pytest.raises(WebError,match='alias'):compile_web(source,tmp_path/'app.nexo')
    with pytest.raises(WebError,match='braces'):
        compile_web('use nexo.web\npub page User(id: int):\n    path = "/users/{{id}"\n    view:\n        p("Hello")\n')
