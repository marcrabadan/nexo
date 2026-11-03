"""M6c request-time HTML, WSGI semantics, isolation and real navigation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import socket
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from test_web_frontend import browser
from nexo.web_frontend import WebError
from nexo.web_ssr import SSRApplication, local_server

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT/'examples/web/ssr/app.nexo'


def project(tmp_path, source=None):
    entry = tmp_path/'app.nexo'
    entry.write_text(EXAMPLE.read_text() if source is None else source)
    return entry


@contextmanager
def served(entry):
    server = local_server(entry, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown();server.server_close();thread.join()


def test_on_demand_routes_and_head_metadata(tmp_path):
    app = SSRApplication.from_file(project(tmp_path))
    response = app.response('/members/123456789/')
    assert response.status == 200
    assert b'<h1>Member 123456789</h1>' in response.body
    assert b'<title>Member 123456789</title>' in response.body
    assert b'content="Request-time profile for member 123456789."' in response.body
    assert b'href="/members/123456790/"' in response.body
    assert b'<header>' in response.body and b'<script' not in response.body
    headers = dict(response.headers)
    assert headers['Content-Length'] == str(len(response.body))
    assert headers['Cache-Control'] == 'no-store'
    assert "script-src 'none'" in headers['Content-Security-Policy']
    head = app.response('/members/123456789/', 'HEAD')
    assert head.status == 200 and head.body == b'' and head.headers == response.headers
    assert app.response('/members/0/').body != app.response('/members/1/').body


def test_compiles_once_and_requests_do_no_io(tmp_path, monkeypatch):
    entry = project(tmp_path)
    app = SSRApplication.from_file(entry)
    entry.write_text('invalid source after startup')
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected per-request file or network access')
    monkeypatch.setattr(Path, 'read_text', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    assert app.response('/members/20/').status == 200
    assert app.response('/members/21/').status == 200
    assert app.response('/_nexo/app.css').status == 200


def test_concurrent_request_isolation(tmp_path):
    app = SSRApplication.from_file(project(tmp_path))
    values = list(range(80))
    def request(value):
        response = app.response(f'/members/{value}/')
        assert response.status == 200
        assert f'<h1>Member {value}</h1>'.encode() in response.body
        assert f'<title>Member {value}</title>'.encode() in response.body
        return response.body
    with ThreadPoolExecutor(max_workers=8) as pool:
        output = list(pool.map(request, values))
    assert len(set(output)) == len(values)
    assert app.response('/members/0/').body == output[0]


@pytest.mark.parametrize('path', ['/members/01/', '/members/9223372036854775808/', '/missing/'])
def test_not_found_for_unmatched_or_mistyped_routes(tmp_path, path):
    response = SSRApplication.from_file(project(tmp_path)).response(path)
    assert response.status == 404 and b'<h1>Page not found</h1>' in response.body


@pytest.mark.parametrize('path', ['/../x', '/%2e%2e/x', '/a%2fb', '/%2537', '/members/7?secret=hidden',
                                  '//other/', '/%00', '/%FF', 'https://other/', '/app.nexo'])
def test_invalid_paths_do_not_echo_input(tmp_path, path):
    app = SSRApplication.from_file(project(tmp_path))
    response = app.response(path)
    assert response.status == 400
    assert response.body == app.response('/%00').body
    assert b'hidden' not in response.body and b'app.nexo' not in response.body


def test_redirect_css_methods_and_wsgi_decoding(tmp_path):
    app = SSRApplication.from_file(project(tmp_path))
    response = app.response('/members/%37')
    assert response.status == 308 and dict(response.headers)['Location'] == '/members/7/'
    assert response.body == b''
    css = app.response('/_nexo/app.css')
    assert css.status == 200 and dict(css.headers)['Content-Type'].startswith('text/css')
    assert b'@scope' in css.body
    assert app.response('/_nexo/app.css', 'HEAD').headers == css.headers
    assert app.response('/_nexo/app.css', 'HEAD').body == b''
    method = app.response('/', 'POST')
    assert method.status == 405 and dict(method.headers)['Allow'] == 'GET, HEAD'
    captured = []
    body = app({'PATH_INFO': '/members/7/', 'REQUEST_METHOD': 'HEAD'}, lambda *args: captured.append(args))
    assert captured[0][0] == '200 OK' and body == [b'']
    captured.clear()
    app({'PATH_INFO': '/members/%37/'}, lambda *args: captured.append(args))
    assert captured[0][0] == '400 Bad Request'  # decoded WSGI input must not be decoded twice
    captured.clear()
    app({'PATH_INFO': '/', 'QUERY_STRING': 'x=1'}, lambda *args: captured.append(args))
    assert captured[0][0] == '400 Bad Request'


def test_safe_error_page_and_failure_isolation(tmp_path):
    app = SSRApplication.from_file(project(tmp_path))
    response = app.response('/members/9223372036854775807/')
    assert response.status == 500
    assert b'Unable to render this page' in response.body
    assert b'<title>Rendering error</title>' in response.body
    assert b'NW6002' not in response.body and b'app.nexo' not in response.body
    assert app.response('/members/8/').status == 200
    source = EXAMPLE.read_text().replace('h1("Unable to render this page")', 'h1(9223372036854775807 + 1)')
    app = SSRApplication.from_file(project(tmp_path, source))
    response = app.response('/members/9223372036854775807/', 'HEAD')
    assert response.status == 500 and response.body == b''
    assert app.response('/members/9223372036854775807/').body.count(b'Server error') == 1


@pytest.mark.parametrize('source', [
    EXAMPLE.read_text().replace('state initial: int = id', 'state initial: int = id\n    fn increment():\n        initial += 1'),
    EXAMPLE.read_text() + '\ncomponent Hidden:\n    state value: int = 0\n    fn change():\n        value += 1\n    view:\n        p("Hidden")\n',
    EXAMPLE.read_text().replace('/members/{id}', '/member.data/{id}'),
    'use nexo.web\npub component App:\n    view:\n        p("No page")\n',
])
def test_unsupported_programs_fail_at_startup(tmp_path, source):
    with pytest.raises(WebError):
        SSRApplication.from_file(project(tmp_path, source))


def test_local_http_statuses_and_host_origin_checks(tmp_path):
    with served(project(tmp_path)) as base:
        with urlopen(base + '/members/15') as response:
            assert response.url == base + '/members/15/'
            assert b'<h1>Member 15</h1>' in response.read()
        with urlopen(Request(base + '/members/15/', method='HEAD')) as response:
            assert response.read() == b'' and int(response.headers['Content-Length']) > 0
        for request, status in ((Request(base + '/', method='POST', data=b'x'), 405),
                                (Request(base + '/', headers={'Host': 'other.test'}), 403),
                                (Request(base + '/', headers={'Origin': 'https://other.test'}), 403),
                                (Request(base + '/unknown/'), 404),
                                (Request(base + '/members/9223372036854775807/'), 500)):
            with pytest.raises(HTTPError) as error:
                urlopen(request)
            assert error.value.code == status
            error.value.close()


def test_cli_serve_validation_and_startup(tmp_path):
    entry = project(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'nexo', 'web', 'serve', str(entry), '--port', '-1'],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 1 and 'NW6004' in result.stderr
    process = subprocess.Popen([sys.executable, '-m', 'nexo', 'web', 'serve', str(entry), '--port', '0'],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline()
        assert 'Nexo Web SSR http://127.0.0.1:' in line
        base = line.split()[3]
        with urlopen(base + 'members/27/') as response:
            assert b'<h1>Member 27</h1>' in response.read()
    finally:
        process.terminate();process.communicate(timeout=10)


def test_real_browser_ssr_dynamic_navigation_without_javascript(tmp_path, browser):
    with served(project(tmp_path)) as base:
        context = browser.new_context(java_script_enabled=False)
        try:
            page = context.new_page()
            scripts = []
            page.on('request', lambda request: scripts.append(request.url) if request.resource_type == 'script' else None)
            page.goto(base + '/members/12000/')
            assert page.title() == 'Member 12000'
            assert page.locator('h1').inner_text() == 'Member 12000'
            assert page.locator('header').evaluate('(el) => getComputedStyle(el).backgroundColor') == 'rgb(237, 244, 255)'
            # WebKit can stall its implicit load wait with JavaScript disabled.
            page.get_by_role('link', name='Next member').click(no_wait_after=True)
            page.wait_for_url(base + '/members/12001/', wait_until='domcontentloaded')
            assert page.url == base + '/members/12001/'
            assert page.title() == 'Member 12001'
            page.go_back()
            assert page.locator('h1').inner_text() == 'Member 12000'
            response = page.goto(base + '/missing/')
            assert response.status == 404 and page.locator('h1').inner_text() == 'Page not found'
            assert not scripts and page.locator('script').count() == 0
        finally:
            context.close()


def test_idle_preconnection_does_not_block_requests(tmp_path):
    server = local_server(project(tmp_path), 0)
    accepted = threading.Event()
    original = server.get_request

    def get_request():
        connection = original()
        accepted.set()
        return connection

    server.get_request = get_request
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    idle = socket.create_connection(server.server_address, timeout=2)
    try:
        assert accepted.wait(2)
        with urlopen(f'http://127.0.0.1:{server.server_port}/members/42/', timeout=8) as response:
            assert response.status == 200
            assert b'Member 42' in response.read()
    finally:
        idle.close()
        server.shutdown();server.server_close();thread.join()
