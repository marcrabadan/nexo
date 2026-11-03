"""M6a static output, capability boundaries and browser rendering."""
import subprocess
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading
import sys
from pathlib import Path

import pytest

from nexo.web_frontend import WebError, build
from test_web_frontend import browser

ROOT = Path(__file__).resolve().parents[1]
SOURCE = '''use nexo.web
pub component App:
    state count: int = 9223372036854775807
    state shown: bool = true
    view:
        main:
            Card(label: "<unsafe & \\"quoted\\">", count: count):
                p("Caller {shown}")
            if shown:
                input(type: "checkbox", value: "safe", checked: true, disabled: false)
            else:
                p("Hidden")
    style:
        main { color: rgb(12, 34, 56); }
component Card(label: str, count: int):
    view:
        h1(label)
        p("{count}")
        slot()
'''


def source(tmp_path, content=SOURCE):
    path = tmp_path / 'app.nexo'
    path.write_text(content)
    return path


def test_static_determinism_escaping_and_output_switch(tmp_path):
    path = source(tmp_path)
    files = build(path, tmp_path/'dist', static=True)
    assert files == build(path, tmp_path/'other', static=True)
    assert set(files) == {'index.html', 'app.css'}
    page = files['index.html']
    assert '9223372036854775807' in page and 'Caller true' in page
    assert '&lt;unsafe &amp;' in page and '<unsafe' not in page
    assert ' checked' in page and ' disabled' not in page and 'Hidden' not in page
    assert '<script' not in page and "script-src 'none'" in page
    build(path, tmp_path/'dist', sourcemap=True)
    build(path, tmp_path/'dist', static=True)
    assert {p.name for p in (tmp_path/'dist').iterdir()} == set(files)
    build(path, tmp_path/'dist')
    assert 'main.js' in {p.name for p in (tmp_path/'dist').iterdir()}


@pytest.mark.parametrize('content', [
    SOURCE.replace('state count: int = 9223372036854775807', 'state count: int = 0\n    fn increment():\n        count += 1'),
    SOURCE.replace('main:', 'main:\n            a("Link", href: "/")'),
    SOURCE.replace('if shown:', 'if false:').replace('p("Hidden")', 'a("Hidden link", href: "/")'),
    SOURCE + '\ncomponent Hidden:\n    state value: int = 0\n    fn change():\n        value += 1\n    view:\n        p("Hidden")\n',
])
def test_reject_unsupported_before_writing(tmp_path, content):
    path = source(tmp_path, content)
    output = tmp_path/'dist'
    with pytest.raises(WebError, match='Static builds') as error:
        build(path, output, static=True)
    assert error.value.code == 'NW6001'
    assert not output.exists()


def test_overflow_and_unrelated_output_preserved(tmp_path):
    path = source(tmp_path, SOURCE.replace('p("{count}")', 'p(count + 1)'))
    with pytest.raises(WebError) as error:
        build(path, tmp_path/'dist', static=True)
    assert error.value.code == 'NW6002'
    path.write_text(SOURCE)
    output = tmp_path/'dist'
    output.mkdir()
    (output/'notes.txt').write_text('keep')
    with pytest.raises(WebError):
        build(path, output, static=True)
    assert (output/'notes.txt').read_text() == 'keep'


def test_cli_and_incompatible_flags(tmp_path):
    path = source(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'nexo', 'web', 'build', str(path),
                             '--static', '-o', str(tmp_path/'dist')], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for option in ('sourcemap', 'release', 'observe'):
        with pytest.raises(WebError) as error:
            build(path, tmp_path/'dist', static=True, **{option: True})
        assert error.value.code == 'NW6001'


def test_real_browser_without_javascript(tmp_path, browser):
    build(source(tmp_path), tmp_path/'dist', static=True)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path/'dist'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        requests = []
        page.on('request', lambda request: requests.append(request.resource_type))
        page.goto(f'http://127.0.0.1:{server.server_port}/')
        assert page.locator('h1').inner_text() == '<unsafe & "quoted">'
        assert page.locator('p').all_text_contents() == ['9223372036854775807', 'Caller true']
        assert page.locator('input').is_checked()
        assert page.locator('script').count() == 0
        assert 'script' not in requests
        assert page.locator('main').evaluate('(node) => getComputedStyle(node).color') == 'rgb(12, 34, 56)'
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_imports_slots_and_html_parser_boundaries(tmp_path):
    (tmp_path/'widgets.nexo').write_text('''use nexo.web
pub component Box(label: str):
    view:
        section:
            h2(label)
            slot()
''')
    path = source(tmp_path, '''use nexo.web
use widgets
pub component App:
    view:
        widgets.Box(label: "Imported"):
            pre("\\n  spaces\\n<escaped>")
''')
    files = build(path, tmp_path/'dist', static=True)
    assert '<pre>\n\n  spaces\n&lt;escaped&gt;</pre>' in files['index.html']
    (tmp_path/'widgets.nexo').write_text((tmp_path/'widgets.nexo').read_text().replace('section:', 'p:'))
    with pytest.raises(WebError, match='nesting'):
        build(path, tmp_path/'other', static=True)


def test_scalar_semantics_and_short_circuit(tmp_path):
    path = source(tmp_path, '''use nexo.web
pub component App(start: int = -9223372036854775808):
    state initial: int = start
    view:
        p("{initial}")
        if false and 9223372036854775807 + 1 == 0:
            p("Never")
        elif not false and 2 * 3 - 1 >= 5:
            p("Selected")
        else:
            p("Never either")
''')
    page = build(path, tmp_path/'dist', static=True)['index.html']
    assert '-9223372036854775808' in page and 'Selected' in page
    assert 'Never' not in page
