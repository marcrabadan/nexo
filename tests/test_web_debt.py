"""Regression evidence for post-M5 compiler/tooling debt closure."""
import json
import importlib.util
import sys
from pathlib import Path

import pytest

from nexo.web_frontend import build, compile_web, WebError
from test_web_frontend import browser
from test_web_production import served, expect, require_optimizer
from test_web_tooling import vite, decoded_mappings

ROOT=Path(__file__).resolve().parents[1]
EXAMPLE=ROOT/'examples/web/keyed/app.nexo'


def test_key_clause_does_not_split_legacy_string_literals():
    js,_,_=compile_web('use nexo.web\npub component App:\n    view:\n        for item in ["one key two"]:\n            p(item)\n')
    assert 'one key two' in js


def test_independent_bundle_styles_have_distinct_namespaces():
    source=EXAMPLE.read_text()
    _,one,_=compile_web(source)
    _,two,_=compile_web(source.replace('padding: 24px','padding: 48px'))
    assert one.split('"')[1]!=two.split('"')[1]


def test_json_context_contracts_are_order_independent_and_analysis_is_repeatable():
    from nexo.web_model import load_program
    from nexo.web_codegen import compile_program
    source='''use nexo.web
type Product:
    id: int
    name: str
pub component App:
    provide Store
    store = inject(Store)
    view:
        for product in store.products key product.id:
            p(product.name)
context Store:
    state products: list[Product] = [Product(id: 1, name: "One")]
    fn clear():
        products = []
'''
    program=load_program(source)
    assert compile_program(program)==compile_program(program)


def test_public_release_generator_removes_previous_hashed_assets(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('web_docs_debt',ROOT/'docs/web/build.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    entry=tmp_path/'app.nexo';entry.write_text('use nexo.web\npub component App:\n    view:\n        h1("Before")\n')
    output=tmp_path/'docs/web-release-demo'
    before=build(entry,output,release=True)
    entry.write_text(entry.read_text().replace('Before','After'))
    after=build(entry,tmp_path/'next',release=True)
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setattr(module,'generate',lambda release:{'docs/web-release-demo/'+name:text for name,text in after.items()})
    monkeypatch.setattr(sys,'argv',['build.py','--release'])
    module.main()
    assert {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}==set(after)
    assert any(name not in after for name in before if name.startswith('assets/'))


@pytest.mark.parametrize('before,after',[
    ('key product.id','key true'),('key product.id','key "constant"'),
    ('event: WebEvent','event: str'),('event.value','event.target'),
    ('products = [Product(id: 2, name: "Two")]','products = [1]'),
])
def test_typed_debt_syntax_rejects_unsafe_or_ambiguous_inputs(before,after):
    with pytest.raises(WebError):compile_web(EXAMPLE.read_text().replace(before,after))


def test_real_browser_keyed_rows_events_static_markup_and_disposal(tmp_path,browser):
    source=EXAMPLE.read_text().replace('            h1(', '            h2("<img src=x onerror=alert(1)>")\n            h1(')
    entry=tmp_path/'app.nexo';entry.write_text(source)
    files=build(entry,tmp_path/'dist')
    assert 'appendStatic(' in files['app.js'] and '&lt;img' in files['app.js']
    with served(tmp_path/'dist') as base:
        page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base)
        expect(page.locator('#row-1 button')).to_have_text('Count: 0')
        page.locator('#row-1 button').click()
        page.evaluate('window.row=document.querySelector("#row-1");window.button=row.querySelector("button")')
        page.get_by_role('button',name='Reorder',exact=True).click()
        expect(page.locator('li span')).to_have_text(['Two updated','One'])
        expect(page.locator('#row-1 button')).to_have_text('Count: 1')
        assert page.evaluate('row===document.querySelector("#row-1") && button===row.querySelector("button")')
        page.get_by_label('Filter',exact=True).fill('<img src=x onerror=alert(1)>')
        expect(page.get_by_role('status')).to_have_text('<img src=x onerror=alert(1)>')
        page.get_by_label('Selected',exact=True).check()
        expect(page.get_by_text('Selected: true',exact=True)).to_be_visible()
        assert page.locator('img').count()==0
        page.get_by_role('button',name='Remove',exact=True).click()
        expect(page.locator('#row-1')).to_have_count(0)
        page.evaluate('button.click()')
        page.get_by_role('button',name='Restore',exact=True).click()
        expect(page.locator('#row-1 button')).to_have_text('Count: 0')
        assert not errors,errors
        page.close()


def test_real_browser_duplicate_keys_keep_last_good_dom_and_focus_moves(tmp_path,browser):
    source=EXAMPLE.read_text().replace('    fn remove():','    fn duplicate():\n        products = [Product(id: 1, name: "Duplicate"), Product(id: 1, name: "Duplicate")]\n\n    fn remove():').replace('            ul:', '            button("Duplicate", on_click: duplicate)\n            ul:')
    entry=tmp_path/'app.nexo';entry.write_text(source);build(entry,tmp_path/'dist')
    with served(tmp_path/'dist') as base:
        page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base);page.locator('#row-1 button').focus()
        # Trigger without moving focus to the external reorder control.
        page.get_by_role('button',name='Reorder',exact=True).evaluate('node=>node.click()')
        expect(page.locator('#row-1 button')).to_be_focused()
        page.evaluate('window.row=document.querySelector("#row-1")')
        page.get_by_role('button',name='Duplicate',exact=True).click()
        expect(page.locator('li span')).to_have_text(['Two updated','One'])
        assert page.evaluate('row===document.querySelector("#row-1")')
        assert len(errors)==1 and 'unique' in errors[0]
        page.get_by_role('button',name='Restore',exact=True).click()
        expect(page.locator('li span')).to_have_text(['One','Two'])
        page.close()


def test_real_browser_static_extraction_preserves_pre_and_legacy_nesting(tmp_path,browser):
    entry=tmp_path/'app.nexo'
    entry.write_text('use nexo.web\npub component App:\n    view:\n        main:\n            pre("\\nfirst\\rsecond")\n            p:\n                div("Legacy DOM nesting")\n')
    build(entry,tmp_path/'dist')
    with served(tmp_path/'dist') as base:
        page=browser.new_page();page.goto(base)
        assert page.locator('pre').text_content()=='\nfirst\rsecond'
        expect(page.locator('p > div')).to_have_text('Legacy DOM nesting')
        page.close()


def test_css_source_maps_include_nested_imports_and_release_originals(tmp_path):
    entry=tmp_path/'app.nexo'
    entry.write_text('use nexo.web\npub component App:\n    view:\n        main("Hello")\n    style:\n        @import "one.css";\n        main { padding: 12px; }\n')
    (tmp_path/'one.css').write_text('@import "two.css";\nmain { color: red; }\n')
    (tmp_path/'two.css').write_text('main { margin: 0; }\n')
    raw=build(entry,tmp_path/'raw',sourcemap=True)
    mapping=json.loads(raw['app.css.map']);mapped=list(decoded_mappings(mapping))
    assert ('nexo://project/two.css',1,1) in mapped
    assert ('nexo://project/one.css',2,1) in mapped
    assert ('nexo://project/app.nexo',7,1) in mapped
    release=build(entry,tmp_path/'release',sourcemap=True,release=True)
    maps=[json.loads(v) for k,v in release.items() if k.endswith('.css.map')]
    assert len(maps)==1 and 'nexo://project/two.css' in maps[0]['sources']
    assert 'main { margin: 0; }\n' in maps[0]['sourcesContent']
    build(entry,tmp_path/'raw')
    assert not (tmp_path/'raw/app.css.map').exists()


def test_real_browser_component_hmr_retains_siblings_and_local_state(tmp_path,browser):
    entry=tmp_path/'app.nexo';entry.write_text(EXAMPLE.read_text())
    with vite(entry,tmp_path) as (base,_):
        page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base)
        page.locator('#row-1 button').click()
        page.get_by_label('Filter',exact=True).fill('kept')
        page.evaluate('window.main=document.querySelector("main");window.input=document.querySelector("input");window.oldButton=document.querySelector("#row-1 button")')
        entry.write_text(EXAMPLE.read_text().replace('count += 1','count += 2'))
        expect(page.locator('#row-1 button')).to_have_text('Count: 1')
        # Wait for actual remount, rather than accepting the pre-update text.
        page.wait_for_function('()=>oldButton!==document.querySelector("#row-1 button")')
        assert page.evaluate('main===document.querySelector("main") && input===document.querySelector("input")')
        expect(page.get_by_role('status')).to_have_text('kept')
        page.evaluate('oldButton.click()')
        expect(page.locator('#row-1 button')).to_have_text('Count: 1')
        page.locator('#row-1 button').click()
        expect(page.locator('#row-1 button')).to_have_text('Count: 3')
        assert not errors,errors
        page.close()
