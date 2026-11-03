"""M6d attach existing scalar DOM, fail-closed mismatches and disposal."""
from contextlib import contextmanager
from pathlib import Path
import re
import subprocess
import sys
import threading

import pytest
from test_web_frontend import browser
from nexo.web_frontend import WebError
from nexo.web_ssr import SSRApplication, local_server

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT/'examples/web/hydration/app.nexo'


def project(tmp_path, source=None):
    entry = tmp_path/'app.nexo'
    entry.write_text(EXAMPLE.read_text() if source is None else source)
    return entry


@contextmanager
def served(entry):
    server = local_server(entry, 0, hydrate=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown();server.server_close();thread.join()


def test_opt_in_payload_precision_assets_and_determinism(tmp_path):
    entry = project(tmp_path)
    with pytest.raises(WebError):
        SSRApplication.from_file(entry)  # ordinary SSR still rejects actions
    app = SSRApplication.from_file(entry, hydrate=True)
    other = SSRApplication.from_file(entry, hydrate=True)
    response = app.response('/counter/9223372036854775807/')
    assert response.status == 200
    assert b'Count: 9223372036854775807' in response.body
    assert b'&quot;start&quot;:&quot;9223372036854775807&quot;' in response.body
    assert b'on_click=' not in response.body and b'<script type="module" src="/_nexo/hydrate-' in response.body
    assert "script-src 'self'" in dict(response.headers)['Content-Security-Policy']
    path = re.search(rb'src="([^"]+)"', response.body).group(1).decode()
    js = app.response(path)
    assert js.status == 200 and dict(js.headers)['Content-Type'].startswith('text/javascript')
    assert other.response(path) == js
    assert app.response(path, 'HEAD').body == b''
    assert app.response(path, 'HEAD').headers == js.headers


@pytest.mark.parametrize('change', [
    ('count += 1', 'count = arbitrary_call()'),
    ('component Counter(start: int):', 'component Counter(start: list[int]):'),
    ('state count: int = start', 'state count: int = start\n    load result = api.get("/api")'),
])
def test_unsupported_features_fail_at_startup(tmp_path, change):
    with pytest.raises(WebError):
        SSRApplication.from_file(project(tmp_path, EXAMPLE.read_text().replace(*change)), hydrate=True)


def deferred(page, app):
    # Instrument only automatic boot; the actual generated hydrate export is kept.
    path, js = next(iter(app._client_assets.items()))
    js = js.split('const target=document.querySelector("#app");')[0]
    page.route('**' + path, lambda route: route.fulfill(body=js, content_type='text/javascript'))
    return path


def test_real_browser_preserves_nodes_focus_and_typed_input(tmp_path, browser):
    entry = project(tmp_path); app = SSRApplication.from_file(entry, hydrate=True)
    with served(entry) as base:
        page = browser.new_page(); path = deferred(page, app)
        page.goto(base + '/counter/7/')
        page.get_by_role('textbox', name='Label').fill('Typed before hydration')
        page.evaluate('''() => {
          window.before=[...document.querySelector('#app').querySelectorAll('*')];
          window.focused=document.activeElement;window.changes=[];
          window.observer=new MutationObserver(records=>changes.push(...records));
          observer.observe(document.querySelector('#app'),{childList:true,subtree:true,characterData:true});
        }''')
        page.evaluate('async path => {window.module=await import(path);window.handle=module.hydrate(document.querySelector("#app"));}', path)
        assert page.evaluate('before.every((el,i)=>el===document.querySelector("#app").querySelectorAll("*")[i])')
        assert page.evaluate('document.activeElement===focused && changes.length===0')
        assert page.get_by_role('textbox', name='Label').input_value() == 'Typed before hydration'
        page.get_by_role('button', name='Increment').click()
        assert page.locator('h2').inner_text() == 'Count: 8'
        page.get_by_role('textbox', name='Label').fill('<script>safe text</script>')
        assert page.locator('section p').inner_text() == 'Label: <script>safe text</script>'
        assert page.locator('section script').count() == 0
        assert page.evaluate('module.hydrate(document.querySelector("#app"))===handle')
        page.get_by_role('button', name='Increment').click()
        assert page.locator('h2').inner_text() == 'Count: 9'
        page.evaluate('handle.destroy();handle.destroy()')
        page.get_by_role('button', name='Increment').click()
        assert page.locator('h2').inner_text() == 'Count: 9'
        page.close()


@pytest.mark.parametrize('mutation', [
    'document.querySelector("h2").textContent="Changed"',
    'document.querySelector("section").append(document.createElement("span"))',
    'document.querySelector("button").setAttribute("onclick","return false")',
    'document.querySelector("#app").dataset.nexoBuild="old"',
    'document.querySelector("#app").dataset.nexoProps="{bad"',
    'document.querySelector("#app").dataset.nexoProps=JSON.stringify({start:"9223372036854775808"})',
    'document.querySelector("#app").dataset.nexoPage="toString"',
])
def test_real_browser_mismatch_attaches_nothing_and_preserves_tree(tmp_path, browser, mutation):
    entry = project(tmp_path); app = SSRApplication.from_file(entry, hydrate=True)
    with served(entry) as base:
        page = browser.new_page(); path = deferred(page, app)
        page.goto(base + '/counter/7/')
        page.evaluate(mutation)
        before = page.locator('#app').inner_html()
        result = page.evaluate('''async path => {
          const {hydrate}=await import(path);
          try{hydrate(document.querySelector('#app'));return 'unexpected';}catch(error){return error.message;}
        }''', path)
        assert result == 'NW6005: hydration mismatch'
        assert page.locator('#app').inner_html() == before
        page.get_by_role('button', name='Increment').click()
        assert page.locator('#app').inner_html() == before
        page.close()


def test_real_browser_empty_text_slots_props_and_fresh_owner(tmp_path, browser):
    source = '''use nexo.web
pub layout Shell:
    view:
        slot()
pub page Home:
    path = "/"
    layout = Shell
    state count: int = 0
    fn increment():
        count += 1
    view:
        main:
            button("Increment", on_click: increment)
            Child(label: "", number: count):
                p("Caller: {count}")
component Child(label: str, number: int):
    view:
        h1(label)
        p("Child: {number}")
        slot()
'''
    entry = project(tmp_path, source); app = SSRApplication.from_file(entry, hydrate=True)
    with served(entry) as base:
        page = browser.new_page(); path = deferred(page, app)
        page.goto(base + '/')
        page.evaluate('async path=>{window.m=await import(path);window.first=m.hydrate(document.querySelector("#app"));first.destroy();window.second=m.hydrate(document.querySelector("#app"));first.destroy();}', path)
        assert page.evaluate('m.hydrate(document.querySelector("#app"))===second')
        page.get_by_role('button', name='Increment').click()
        assert page.locator('p').all_text_contents() == ['Child: 1', 'Caller: 1']
        assert page.locator('h1').inner_text() == ''
        page.close()


def test_real_browser_auto_boot_and_no_javascript_fallback(tmp_path, browser):
    with served(project(tmp_path)) as base:
        context = browser.new_context()
        page = context.new_page(); page.goto(base + '/counter/11/')
        page.wait_for_function('document.querySelector("#app").dataset.nexoHydration==="ready"')
        page.get_by_role('button', name='Increment').click()
        assert page.locator('h2').inner_text() == 'Count: 12'
        page.get_by_role('link', name='Home', exact=True).click()
        page.wait_for_function('document.querySelector("#app").dataset.nexoHydration==="ready"')
        assert page.locator('h2').inner_text() == 'Count: 0'
        context.close()
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page();page.goto(base + '/counter/11/')
        assert page.locator('h2').inner_text() == 'Count: 11'
        page.get_by_role('link', name='Home', exact=True).click()
        assert page.locator('h2').inner_text() == 'Count: 0'
        context.close()


def test_cli_hydration_flag():
    result = subprocess.run([sys.executable, '-m', 'nexo', 'web', 'serve', '--help'], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0 and '--hydrate' in result.stdout


def test_hydrated_responses_use_compiled_snapshot_only(tmp_path, monkeypatch):
    import socket
    app = SSRApplication.from_file(project(tmp_path), hydrate=True)
    path = next(iter(app._client_assets))
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected request-time I/O')
    monkeypatch.setattr(Path, 'read_text', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    assert app.response('/counter/20/').status == 200
    assert app.response(path).status == 200


def test_real_browser_checkbox_edits_and_typed_change_event(tmp_path, browser):
    source = '''use nexo.web
pub page Home:
    path = "/"
    state enabled: bool = true
    fn toggle(event: WebEvent):
        enabled = event.checked
    view:
        input(type: "checkbox", aria_label: "Enabled", checked: enabled, on_change: toggle)
        p("Enabled: {enabled}")
'''
    entry = project(tmp_path, source); app = SSRApplication.from_file(entry, hydrate=True)
    with served(entry) as base:
        page = browser.new_page(); path = deferred(page, app)
        page.goto(base + '/')
        checkbox = page.get_by_role('checkbox', name='Enabled')
        checkbox.uncheck()  # updates the live property; SSR attribute remains true
        page.evaluate('async path=>{const m=await import(path);m.hydrate(document.querySelector("#app"));}', path)
        assert not checkbox.is_checked()
        assert page.locator('p').inner_text() == 'Enabled: true'
        checkbox.check();checkbox.uncheck()
        assert page.locator('p').inner_text() == 'Enabled: false'
        page.close()


def test_real_browser_prototype_named_route_prop(tmp_path, browser):
    source = '''use nexo.web
pub page Detail(__proto__: int):
    path = "/detail/{__proto__}"
    state count: int = __proto__
    fn increment():
        count += 1
    view:
        p("{count}")
        button("Increment", on_click: increment)
'''
    with served(project(tmp_path, source)) as base:
        page = browser.new_page();page.goto(base + '/detail/4/')
        page.wait_for_function('document.querySelector("#app").dataset.nexoHydration==="ready"')
        page.get_by_role('button', name='Increment').click()
        assert page.locator('p').inner_text() == '5'
        page.close()


def test_host_metadata_does_not_rewrite_nested_app_id(tmp_path):
    source = EXAMPLE.read_text().replace('h2("Count: {count}")', 'div(id: "app"):\n                h2("Count: {count}")')
    app = SSRApplication.from_file(project(tmp_path, source), hydrate=True)
    response = app.response('/counter/7/')
    assert response.status == 200
    assert response.body.count(b'data-nexo-build=') == 1
    assert b'<div id="app"><h2>Count: 7</h2></div>' in response.body
