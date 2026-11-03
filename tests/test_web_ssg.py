"""Finite routed static generation, output ownership and real navigation."""
from contextlib import contextmanager
import json
import shutil
from pathlib import Path
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from test_web_frontend import browser

from nexo.web_frontend import WebError, build
from nexo.web_release import MANIFEST
from nexo.web_static_output import read_output
from nexo.web_ssg import canonical
from nexo.web_tooling import preview_server

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT/'examples/web/ssg/app.nexo'
PATHS = ['/members/7', '/members/9223372036854775807']


def project(tmp_path, source=None, paths=None):
    entry = tmp_path/'app.nexo'
    entry.write_text(EXAMPLE.read_text() if source is None else source)
    routes = tmp_path/'routes.json'
    routes.write_text(json.dumps(PATHS if paths is None else paths))
    return entry, routes


@contextmanager
def served(directory):
    server = preview_server(directory, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown();server.server_close();thread.join()


def test_routes_layouts_metadata_links_and_relocatability(tmp_path):
    outputs = []
    for name in ('one', 'two'):
        root = tmp_path/name;root.mkdir()
        entry, routes = project(root)
        outputs.append(build(entry, root/'dist', static=True, routes=routes))
    assert outputs[0] == outputs[1]
    files = outputs[0]
    manifest = json.loads(files[MANIFEST])
    assert manifest['format'] == 'nexo-web-static'
    assert manifest['routes'] == {'/': 'index.html', '/guide/': 'guide/index.html',
                                  '/members/7/': 'members/7/index.html',
                                  '/members/9223372036854775807/': 'members/9223372036854775807/index.html'}
    page = files['members/7/index.html']
    assert '<title>Member 7</title>' in page
    assert '<meta name="description" content="The profile for member 7.">' in page
    assert '<header>' in page and '<h1>Member 7</h1>' in page
    assert 'href="/guide/"' in page and 'href="/_nexo/app.css"' in page
    assert '<script' not in page and not any(n.endswith('.js') for n in files)
    assert read_output(tmp_path/'one/dist') == files
    assert '<title>Page not found</title>' in files['404.html']


def test_static_routes_need_no_enumeration(tmp_path):
    entry, _ = project(tmp_path, '''use nexo.web
pub page Home:
    path = "/"
    view:
        a("Guide", href: "/guide")
pub page Guide:
    path = "/guide"
    view:
        h1("Guide")
''')
    files = build(entry, tmp_path/'dist', static=True)
    assert '<a href="/guide/">Guide</a>' in files['index.html']
    assert 'guide/index.html' in files


@pytest.mark.parametrize('paths', [None, [], ['/unknown'], ['/members/01'], ['/members/9223372036854775808'],
                                   ['/members/7', '/members/%37/'], ['/members/7', '/members/7/'],
                                   [42], {'paths': PATHS}])
def test_missing_invalid_and_duplicate_enumerations_do_not_replace_output(tmp_path, paths):
    entry, routes = project(tmp_path)
    output = tmp_path/'dist'
    before = build(entry, output, static=True, routes=routes)
    routes.write_text(json.dumps(paths))
    with pytest.raises(WebError) as error:
        build(entry, output, static=True, routes=None if paths is None else routes)
    assert error.value.code == 'NW6003'
    assert read_output(output) == before


@pytest.mark.parametrize('path', ['https://other.test/', '//server', '/../x', '/%2e%2e/x', '/a%2fb',
                                  '/a%5cb', '/%00', '/%2530', '/nul', '/COM1', '/_nexo/a',
                                  '/a?x', '/a#x', '/a//b', '/%GG', '/%FF', '/%20', '/é'])
def test_unsafe_and_unsupported_output_paths(path):
    with pytest.raises(WebError) as error:
        canonical(path)
    assert error.value.code == 'NW6003'


def test_shadowing_case_collisions_and_unbuilt_links(tmp_path):
    entry, routes = project(tmp_path)
    entry.write_text(entry.read_text().replace('pub page Member(id: int):', '''pub page Special:
    path = "/members/7"
    view:
        h1("Special")

pub page Member(id: int):'''))
    files = build(entry, tmp_path/'dist', static=True, routes=routes)
    assert 'Special' in files['members/7/index.html']  # static specificity beats parameter
    routes.write_text('["/members/7"]')
    with pytest.raises(WebError, match='Enumerate at least one'):
        build(entry, tmp_path/'other', static=True, routes=routes)
    entry.write_text(EXAMPLE.read_text().replace('id: int', 'id: str').replace('initial: int', 'initial: str'))
    routes.write_text('["/members/7", "/members/A", "/members/a"]')
    with pytest.raises(WebError, match='collide'):
        build(entry, tmp_path/'other', static=True, routes=routes)
    entry.write_text(EXAMPLE.read_text().replace('href: "/guide"', 'href: "/missing"'))
    routes.write_text(json.dumps(PATHS))
    with pytest.raises(WebError, match='no generated page'):
        build(entry, tmp_path/'other', static=True, routes=routes)


def test_typed_bool_routes_and_metadata_escaping(tmp_path):
    entry, routes = project(tmp_path, '''use nexo.web
pub page Home:
    path = "/"
    view:
        a("True", href: "/flag/true")
pub page Flag(enabled: bool):
    path = "/flag/{enabled}"
    title = "Flag {enabled} <unsafe>"
    description = "A & B"
    view:
        h1("{enabled}")
''', ['/flag/true', '/flag/false'])
    files = build(entry, tmp_path/'dist', static=True, routes=routes)
    assert '<title>Flag true &lt;unsafe&gt;</title>' in files['flag/true/index.html']
    assert 'content="A &amp; B"' in files['flag/true/index.html']
    assert '<h1>false</h1>' in files['flag/false/index.html']
    routes.write_text('["/flag/True"]')
    with pytest.raises(WebError):
        build(entry, tmp_path/'other', static=True, routes=routes)


def test_rebuild_integrity_and_switch_to_client_and_standalone(tmp_path):
    entry, routes = project(tmp_path)
    output = tmp_path/'dist'
    build(entry, output, static=True, routes=routes)
    routes.write_text('["/members/7"]')
    build(entry, output, static=True, routes=routes)
    assert not (output/'members/9223372036854775807').exists()
    build(entry, output)
    assert not (output/'members').exists() and not (output/MANIFEST).exists()
    build(entry, output, static=True, routes=routes)
    entry.write_text('use nexo.web\npub component App:\n    view:\n        p("Standalone")\n')
    build(entry, output, static=True)
    assert {p.name for p in output.iterdir()} == {'index.html', 'app.css'}
    assert 'Standalone' in (output/'index.html').read_text()


def test_modified_unrelated_and_symlink_outputs_are_preserved(tmp_path):
    entry, routes = project(tmp_path)
    output = tmp_path/'dist'
    build(entry, output, static=True, routes=routes)
    (output/'guide/index.html').write_text('modified')
    with pytest.raises(WebError, match='modified'):
        build(entry, output, static=True, routes=routes)
    assert (output/'guide/index.html').read_text() == 'modified'
    output = tmp_path/'clean'
    build(entry, output, static=True, routes=routes)
    (output/'notes').mkdir()
    with pytest.raises(WebError):
        build(entry, output, static=True, routes=routes)
    assert (output/'notes').is_dir()
    (output/'notes').rmdir()
    (output/'guide/index.html').unlink()
    (output/'guide/index.html').symlink_to(entry)
    with pytest.raises(WebError):
        build(entry, output, static=True, routes=routes)
    assert (output/'guide/index.html').is_symlink()


def test_cli_routes_and_invalid_json(tmp_path):
    entry, routes = project(tmp_path)
    command = [sys.executable, '-m', 'nexo', 'web', 'build', str(entry),
               '--static', '--routes', str(routes), '-o', str(tmp_path/'dist')]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    routes.write_text('not json')
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 1 and 'NW6003' in result.stderr
    with pytest.raises(WebError, match='requires --static'):
        build(entry, tmp_path/'other', routes=routes)


def test_preview_nested_pages_redirects_and_fallback_status(tmp_path):
    entry, routes = project(tmp_path)
    build(entry, tmp_path/'dist', static=True, routes=routes)
    with served(tmp_path/'dist') as base:
        with urlopen(base + '/members/7') as response:
            assert response.url.endswith('/members/7/')
            assert '<h1>Member 7</h1>' in response.read().decode()
            assert "script-src 'none'" in response.headers['Content-Security-Policy']
        with urlopen(base + '/_nexo/app.css') as response:
            assert 'text/css' in response.headers['Content-Type']
        with pytest.raises(HTTPError) as error:
            urlopen(base + '/app.nexo')
        assert error.value.code == 404
        assert '<h1>Page not found</h1>' in error.value.read().decode()


def test_real_browser_navigation_and_deep_links_without_javascript(tmp_path, browser):
    entry, routes = project(tmp_path)
    build(entry, tmp_path/'dist', static=True, routes=routes)
    with served(tmp_path/'dist') as base:
        context = browser.new_context(java_script_enabled=False)
        try:
            page = context.new_page()
            page.goto(base + '/members/9223372036854775807/')
            assert page.title() == 'Member 9223372036854775807'
            assert page.locator('h1').inner_text() == 'Member 9223372036854775807'
            assert page.locator('meta[name="description"]').get_attribute('content') == 'The profile for member 9223372036854775807.'
            assert page.locator('header').evaluate('(el) => getComputedStyle(el).backgroundColor') == 'rgb(237, 244, 255)'
            page.get_by_role('link', name='Guide', exact=True).click()
            assert page.url == base + '/guide/' and page.title() == 'Static site guide'
            page.get_by_role('link', name='Home', exact=True).click()
            assert page.url == base + '/'
            page.go_back()
            assert page.url == base + '/guide/'
            assert page.locator('script').count() == 0
        finally:
            context.close()


def test_real_browser_projected_css_and_client_tree_parity(tmp_path, browser):
    source = '''use nexo.web
pub layout Outer:
    view:
        header:
            p("Outer")
        slot()
pub layout Inner:
    layout = Outer
    view:
        section:
            slot()
pub page Home:
    path = "/"
    layout = Inner
    view:
        main:
            Card:
                p("Projected")
    style:
        p { color: rgb(1, 2, 3); }
component Card:
    view:
        p("Receiver")
        slot()
    style:
        p { color: rgb(4, 5, 6); }
'''
    entry, _ = project(tmp_path, source)
    build(entry, tmp_path/'static', static=True)
    build(entry, tmp_path/'client')
    with served(tmp_path/'static') as static, served(tmp_path/'client') as client:
        static_context = browser.new_context(java_script_enabled=False)
        client_context = browser.new_context()
        try:
            pages = [static_context.new_page(), client_context.new_page()]
            snapshots = []
            for page, base in zip(pages, (static, client)):
                page.goto(base + '/')
                assert page.get_by_text('Projected', exact=True).evaluate('(el) => getComputedStyle(el).color') == 'rgb(1, 2, 3)'
                assert page.get_by_text('Receiver', exact=True).evaluate('(el) => getComputedStyle(el).color') == 'rgb(4, 5, 6)'
                snapshots.append(page.locator('#app').evaluate('''root => [...root.querySelectorAll('*')].map(el => [el.tagName, el.dataset.nexo || '', el.children.length ? '' : el.textContent])'''))
            assert snapshots[0] == snapshots[1]
        finally:
            static_context.close();client_context.close()


def test_switch_from_and_to_existing_optimized_release(tmp_path):
    from nexo.web_release import read_release
    from nexo.web_static_output import write_output
    entry, routes = project(tmp_path)
    output = tmp_path/'dist'
    shutil.copytree(ROOT/'docs/web-release-demo', output)
    release = read_release(output)
    build(entry, output, static=True, routes=routes)
    assert not (output/'assets').exists()
    write_output(release, output)
    assert read_release(output) == release
    assert not (output/'members').exists()


def test_staged_rename_failure_restores_previous_static_build(tmp_path, monkeypatch):
    entry, routes = project(tmp_path)
    output = tmp_path/'dist'
    before = build(entry, output, static=True, routes=routes)
    routes.write_text('["/members/7"]')
    rename = Path.rename
    def fail_stage(path, destination):
        if path.name == 'next' and path.parent.name.startswith('.nexo-static-'):
            raise OSError('injected stage rename failure')
        return rename(path, destination)
    monkeypatch.setattr(Path, 'rename', fail_stage)
    with pytest.raises(OSError, match='injected'):
        build(entry, output, static=True, routes=routes)
    assert read_output(output) == before
    assert not list(tmp_path.glob('.nexo-static-*'))
