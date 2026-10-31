"""Executable M1 compiler, diagnostics, security and real Chromium regressions."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import threading

import pytest

from nexo.web_frontend import WebError, build, compile_web

ROOT = Path(__file__).resolve().parents[1]
COUNTER = ROOT/'examples/web/counter.nexo'


def test_counter_determinism_and_security(tmp_path):
    first = build(COUNTER, tmp_path/'one')
    second = build(COUNTER, tmp_path/'two')
    assert first == second
    assert '@scope ([data-nexo="nw-' in first['app.css']
    assert 'document.createTextNode' in first['app.js']
    assert 'innerHTML' not in first['app.js']
    assert 'eval(' not in first['app.js']
    assert "script-src 'self'" in first['index.html']
    assert '<script type="module" src=' in first['index.html']


@pytest.mark.parametrize('before,after,code', [
    ('int = 0', 'int = "wrong"', 'NW1002'),
    ('count += 1', 'count += "wrong"', 'NW1002'),
    ('Count: {count}', 'Count: {missing}', 'NW1002'),
    ('on_click: increment', 'on_click: unknown', 'NW1002'),
    ('p("Count: {count}")', 'script("alert(1)")', 'NW1004'),
    ('on_click: increment', 'onclick: "alert(1)"', 'NW1004'),
    ('on_click: increment', 'href: "javascript:alert(1)"', 'NW1004'),
    ('padding: 24px;', '@import "https://example.com/style.css";', 'NW1003'),
    ('padding: 24px;', 'background: url("https://example.com/a");', 'NW1003'),
    ('padding: 24px;', 'background-image: image-set("https://example.invalid/a" 1x);', 'NW1003'),
    ('padding: 24px;', 'background-image: -webkit-image-set("https://example.invalid/a" 1x);', 'NW1003'),
    ('padding: 24px;', 'background-image: IMAGE("https://example.invalid/a");', 'NW1003'),
    ('padding: 24px;', 'background-image: src("https://example.invalid/a");', 'NW1003'),
    ('state count: int = 0', 'state count: int = 0\n    state if: int = 0', 'NW1001'),
    ('    view:', '    fn if():\n        count += 1\n\n    view:', 'NW1001'),
    ('count += 1', 'http.get("/secret")', 'NW1001'),
    ('pub component Counter:', 'pub page Counter:', 'NW2003'),
    ('state count: int = 0', 'state count: int = secret()', 'NW1002'),
    ('    view:', '    load products = api.get("/products")\n\n    view:', 'NW1001'),
])
def test_rejections(before, after, code):
    with pytest.raises(WebError) as error:
        compile_web(COUNTER.read_text().replace(before, after))
    assert error.value.code == code
    assert 'Fix:' in str(error.value)


def test_cli_machine_readable(tmp_path):
    for source, expected in [(COUNTER.read_text(), 0), (COUNTER.read_text().replace('int = 0', 'bool = 0'), 1)]:
        app = tmp_path/'app.nexo'
        app.write_text(source)
        result = subprocess.run([sys.executable, '-m', 'nexo', 'web', 'check', '--json', str(app)], cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == expected, result.stderr
        data = json.loads(result.stdout)
        assert data['schema_version'] == 1
        assert bool(data['diagnostics']) == bool(expected)
        if expected:
            assert data['diagnostics'][0]['line'] == 4


def test_css_queries_and_tokens():
    source = COUNTER.read_text().replace('padding: 24px;', '--space: 24px; padding: var(--space);').replace('        }', '        }\n        @media (min-width: 1px) { main { color: red; } }\n        @container (width > 1px) { p { color: blue; } }')
    _, css, _ = compile_web(source)
    assert '@media' in css and '@container' in css and 'var(--space)' in css


@pytest.fixture
def browser():
    # Missing browser/dependency is an explicit skip, never simulated browser success.
    required = os.environ.get('NEXO_WEB_REQUIRE_BROWSER') == '1'
    try:
        import playwright.sync_api as playwright
    except ImportError:
        if required:
            pytest.fail('Playwright is required by NEXO_WEB_REQUIRE_BROWSER=1')
        pytest.skip('Playwright is unavailable')
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=shutil.which("chromium"))
        except playwright.Error as error:
            if required:
                pytest.fail(f'Chromium is required: {error}')
            pytest.skip(f'Chromium unavailable: {error}')
        yield browser
        browser.close()


def test_real_browser_reactivity_instances_cleanup_and_scoping(tmp_path, browser):
    source = COUNTER.read_text().replace('state count: int = 0', 'state count: int = 0\n    state untouched: int = 9\n    state label: str = "<img src=x onerror=alert(1)>"').replace('count += 1', 'count += 1\n        count += 1').replace('            button', '            p("Other: {untouched}")\n            p(label)\n            button').replace('padding: 24px;', 'padding: 24px; color: rgb(1, 2, 3);')
    app = tmp_path/'counter.nexo'
    app.write_text(source)
    build(app, tmp_path)
    (tmp_path/'main.js').write_text('import {Counter} from "./app.js"; window.one = Counter(document.querySelector("#app")); window.two = Counter(document.querySelector("#app"));')
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(f'http://127.0.0.1:{server.server_port}/')
        page.wait_for_selector('button')
        assert page.locator('main').count() == 2
        assert page.locator('img').count() == 0
        assert page.locator('main').first.locator('p').nth(2).inner_text() == '<img src=x onerror=alert(1)>'
        assert page.locator('main').first.evaluate('(n) => getComputedStyle(n).padding') == '24px'
        page.evaluate('''() => {
          window.text = document.querySelector('p').firstChild;
          window.records = [];
          new MutationObserver(r => records.push(...r.map(m => m.target.data))).observe(document.querySelector('main'), {subtree:true, characterData:true});
          const outside = document.createElement('main'); outside.id = 'outside'; document.body.append(outside);
        }''')
        page.locator('button').first.click()
        assert page.locator('p').first.inner_text() == 'Count: 2'
        assert page.locator('main').nth(1).locator('p').first.inner_text() == 'Count: 0'
        assert page.evaluate('text === document.querySelector("p").firstChild')
        assert page.evaluate('records') == ['Count: 2']
        assert page.locator('#outside').evaluate('(n) => getComputedStyle(n).padding') == '0px'
        page.evaluate('''() => {window.detached = document.querySelector('button'); one.destroy(); one.destroy(); detached.click();}''')
        assert page.locator('button').count() == 1
        page.locator('button').click()
        assert page.locator('main p').first.inner_text() == 'Count: 2'
        assert page.evaluate('records') == ['Count: 2']
        assert not errors
        # Keyboard activation follows native button behavior.
        page.locator('button').focus()
        page.keyboard.press('Enter')
        assert page.locator('main p').first.inner_text() == 'Count: 4'
    finally:
        page.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_website_and_ai_guide_canonical_sources_are_aligned():
    import importlib.util
    spec = importlib.util.spec_from_file_location('web_build', ROOT/'docs/web/build.py')
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    for name, expected in generator.generate().items():
        assert (ROOT/name).read_text() == expected
    catalog = json.loads((ROOT/'docs/ai-guide/source/examples.json').read_text())
    assert catalog['frontend_examples'][0]['path'] == 'examples/web/counter.nexo'
    versions = json.loads((ROOT/'docs/ai-guide/versions.json').read_text())
    topic = ROOT/'docs/ai-guide/versions'/('v1-'+versions['current'][:20])/'nexo-web.txt'
    assert COUNTER.read_text() in topic.read_text()
    assert 'Proposed only:' in topic.read_text()


def test_website_browser_layout_theme_navigation_and_demo(browser):
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(ROOT/'docs')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        for width in (360, 768, 1440):
            page.set_viewport_size({'width': width, 'height': 900})
            page.goto(base+'/web.html')
            assert page.title() == 'Nexo Web — compiler-first browser UI'
            assert page.locator('h1').inner_text() == 'Nexo Web'
            assert page.locator('nav[aria-label="Main"] a[aria-current="page"]').inner_text() == 'Nexo Web'
            for theme in ('light', 'dark'):
                page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme)
            page.keyboard.press('Tab')
            assert page.evaluate('document.activeElement.classList.contains("skip")')
        page.goto(base+'/web-demo/')
        page.wait_for_selector('button')
        assert page.locator('button').count() == 2
        page.locator('button').first.click()
        assert page.locator('main p').first.inner_text() == 'Count: 1'
        assert page.locator('main p').nth(1).inner_text() == 'Count: 0'
        assert not errors
    finally:
        page.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_signed_integer_boundaries_and_static_bindings():
    source = COUNTER.read_text().replace('int = 0', 'int = -9223372036854775808')
    js, _, _ = compile_web(source)
    assert '-9223372036854775808n' in js
    with pytest.raises(WebError, match='range'):
        compile_web(source.replace('-9223372036854775808', '9223372036854775808'))
    # Only the interpolated count subscribes; static headings/buttons do not.
    assert js.count('bind(["count"]') == 1
    assert 'bind([]' not in js


def test_real_browser_skips_unchanged_values_and_updates_typed_attributes(tmp_path, browser):
    source = COUNTER.read_text().replace('state count: int = 0', 'state count: int = 0\n    state disabled: bool = false\n    state title: str = "Initial"')
    source = source.replace('count += 1', 'count += 1\n        count -= 1\n        disabled = true\n        title = "Updated"')
    source = source.replace('p("Count: {count}")', 'p("Count: {count}", title: title)\n            input(disabled: disabled)')
    app = tmp_path/'attributes.nexo'
    app.write_text(source)
    build(app, tmp_path)
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page = browser.new_page()
    try:
        page.goto(f'http://127.0.0.1:{server.server_port}/')
        page.wait_for_selector('button')
        assert page.locator('p').get_attribute('title') == 'Initial'
        assert not page.locator('input').is_disabled()
        page.evaluate('''() => {
          window.changes = [];
          new MutationObserver(records => changes.push(...records.map(r => r.type === 'attributes' ? r.attributeName : 'text'))).observe(document.querySelector('main'), {subtree:true, characterData:true, attributes:true});
        }''')
        page.locator('button').click()
        assert page.locator('p').inner_text() == 'Count: 0'
        assert page.locator('p').get_attribute('title') == 'Updated'
        assert page.locator('input').is_disabled()
        assert page.evaluate('changes') == ['title', 'disabled']
        page.evaluate('changes.length = 0')
        page.locator('button').click()
        assert page.evaluate('changes') == []
    finally:
        page.close()
        server.shutdown()
        server.server_close()
        thread.join()
