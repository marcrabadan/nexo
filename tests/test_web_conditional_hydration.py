"""M6e conditional ownership, initial DOM claims and branch replacement."""
from pathlib import Path

import pytest
from test_web_frontend import browser
from test_web_hydration import project, served, deferred
from nexo.web_ssr import SSRApplication

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT/'examples/web/conditional-hydration/app.nexo'


def test_server_markers_only_in_hydration_mode(tmp_path):
    entry = project(tmp_path, EXAMPLE.read_text())
    app = SSRApplication.from_file(entry, hydrate=True)
    response = app.response('/')
    assert response.status == 200
    assert response.body.count(b'<!--nexo-if:') == 2
    assert response.body.count(b'<!--/nexo-if:') == 2
    assert b'Increment the parent to project content' in response.body
    source = '''use nexo.web
pub page Home:
    path = "/"
    view:
        if true:
            p("Yes")
        else:
            p("No")
'''
    app = SSRApplication.from_file(project(tmp_path, source))
    assert b'nexo-if:' not in app.response('/').body
    assert b'<p>Yes</p>' in app.response('/').body


def test_real_browser_claims_initial_branch_then_preserves_stable_choice(tmp_path, browser):
    entry = project(tmp_path, EXAMPLE.read_text());app = SSRApplication.from_file(entry, hydrate=True)
    with served(entry) as base:
        page = browser.new_page();path = deferred(page, app)
        page.goto(base + '/')
        page.evaluate('window.nodes=[...document.querySelector("#app").querySelectorAll("*")];window.records=[];window.observer=new MutationObserver(items=>records.push(...items));observer.observe(document.querySelector("#app"),{childList:true,characterData:true,subtree:true});')
        page.evaluate('async path=>{window.m=await import(path);window.handle=m.hydrate(document.querySelector("#app"));}', path)
        assert page.evaluate('records.length===0 && nodes.every((el,i)=>el===document.querySelector("#app").querySelectorAll("*")[i])')
        page.get_by_role('button', name='Increment local', exact=True).click()
        page.evaluate('window.panel=document.querySelector("section");window.heading=document.querySelector("h1");')
        page.get_by_role('button', name='Increment parent', exact=True).click()
        assert page.evaluate('document.querySelector("section")===panel && document.querySelector("h1")===heading')
        assert page.get_by_text('Local: 1', exact=True).count() == 1
        assert page.get_by_text('Projected parent: 1', exact=True).count() == 1
        page.get_by_role('button', name='Increment parent', exact=True).click()
        assert page.evaluate('document.querySelector("section")===panel')
        assert page.get_by_text('Projected parent: 2', exact=True).count() == 1
        assert page.get_by_text('Local: 1', exact=True).count() == 1
        page.close()


def test_real_browser_branch_disposal_resets_children_and_cleans_slots(tmp_path, browser):
    entry = project(tmp_path, EXAMPLE.read_text())
    with served(entry) as base:
        page = browser.new_page();page.goto(base + '/')
        page.wait_for_function('document.querySelector("#app").dataset.nexoHydration==="ready"')
        page.get_by_role('button', name='Increment parent', exact=True).click()
        page.get_by_role('button', name='Increment local', exact=True).click()
        page.evaluate('window.oldPanel=document.querySelector("section");window.oldButton=[...oldPanel.querySelectorAll("button")][0];window.oldText=oldPanel.textContent;')
        page.get_by_role('button', name='Toggle panel', exact=True).click()
        assert page.get_by_text('Panel hidden', exact=True).count() == 1
        page.get_by_role('button', name='Increment parent', exact=True).click()
        page.evaluate('oldButton.click()')
        assert page.evaluate('oldPanel.textContent===oldText')
        page.get_by_role('button', name='Toggle panel', exact=True).click()
        assert page.get_by_text('Local: 0', exact=True).count() == 1
        assert page.get_by_text('Projected parent: 2', exact=True).count() == 1
        assert page.evaluate('document.querySelector("section")!==oldPanel')
        for _ in range(6):
            page.get_by_role('button', name='Toggle panel', exact=True).click()
            page.get_by_role('button', name='Toggle panel', exact=True).click()
        page.get_by_role('button', name='Increment local', exact=True).click()
        assert page.get_by_text('Local: 1', exact=True).count() == 1
        page.close()


CHOICES = '''use nexo.web
pub page Home:
    path = "/"
    state number: int = -1
    fn next():
        number += 1
    fn previous():
        number -= 1
    view:
        main:
            button("Next", on_click: next)
            button("Previous", on_click: previous)
            input(value: "Unrelated", aria_label: "Outside")
            if number > 0:
                p("Positive {number}")
            elif number == 0:
                p("Zero")
            else:
                p("Negative")
            if number > 2:
                p("Optional {number}")
'''


def test_real_browser_elif_else_empty_branch_and_unrelated_focus(tmp_path, browser):
    with served(project(tmp_path, CHOICES)) as base:
        page = browser.new_page();page.goto(base + '/')
        page.wait_for_function('document.querySelector("#app").dataset.nexoHydration==="ready"')
        assert page.get_by_text('Negative', exact=True).count() == 1
        outside = page.get_by_role('textbox', name='Outside');outside.fill('Keep me');outside.focus()
        page.evaluate('window.outside=document.activeElement;window.nextButton=document.querySelector("button");nextButton.click();')
        assert page.get_by_text('Zero', exact=True).count() == 1
        assert page.evaluate('document.activeElement===outside')
        page.evaluate('nextButton.click()')
        assert page.get_by_text('Positive 1', exact=True).count() == 1
        page.evaluate('window.positive=[...document.querySelectorAll("p")][0];nextButton.click()')
        assert page.get_by_text('Positive 2', exact=True).count() == 1
        assert page.evaluate('document.querySelector("p")===positive')
        page.evaluate('nextButton.click()')
        assert page.get_by_text('Optional 3', exact=True).count() == 1
        page.evaluate('[...document.querySelectorAll("button")].find(el=>el.textContent==="Previous").click()')
        assert page.get_by_text('Optional 3', exact=True).count() == 0
        assert page.get_by_text('Positive 2', exact=True).count() == 1
        assert outside.input_value() == 'Keep me'
        assert page.evaluate('document.activeElement===outside')
        page.close()


@pytest.mark.parametrize('mutation', [
    'walker.currentNode.data="nexo-if:999:0"',
    'walker.currentNode.data=walker.currentNode.data.replace(/:0$/,":1")',
    'walker.currentNode.remove()',
    'while(walker.nextNode()){if(walker.currentNode.data.startsWith("/nexo-if:")){walker.currentNode.remove();break;}}',
    'walker.currentNode.after(document.createComment("unexpected"))',
])
def test_real_browser_marker_mismatch_is_atomic(tmp_path, browser, mutation):
    entry = project(tmp_path, EXAMPLE.read_text());app = SSRApplication.from_file(entry, hydrate=True)
    with served(entry) as base:
        page = browser.new_page();path = deferred(page, app);page.goto(base + '/')
        page.evaluate('window.walker=document.createTreeWalker(document.querySelector("#app"),NodeFilter.SHOW_COMMENT);walker.nextNode();' + mutation)
        before = page.locator('#app').inner_html()
        error = page.evaluate('''async path=>{const m=await import(path);try{m.hydrate(document.querySelector('#app'));return 'unexpected';}catch(error){return error.message;}}''', path)
        assert error == 'NW6005: hydration mismatch'
        page.get_by_role('button', name='Toggle panel', exact=True).click()
        assert page.locator('#app').inner_html() == before
        page.close()


def test_real_browser_root_destroy_disposes_current_branches(tmp_path, browser):
    entry = project(tmp_path, EXAMPLE.read_text());app = SSRApplication.from_file(entry, hydrate=True)
    with served(entry) as base:
        page = browser.new_page();path = deferred(page, app);page.goto(base + '/')
        page.evaluate('async path=>{window.m=await import(path);window.handle=m.hydrate(document.querySelector("#app"));}', path)
        page.get_by_role('button', name='Increment parent', exact=True).click()
        page.get_by_role('button', name='Toggle panel', exact=True).click()
        page.get_by_role('button', name='Toggle panel', exact=True).click()
        before = page.locator('#app').inner_html()
        page.evaluate('handle.destroy();handle.destroy()')
        for name in ('Increment local', 'Increment parent', 'Toggle panel'):
            page.get_by_role('button', name=name, exact=True).click()
        assert page.locator('#app').inner_html() == before
        page.close()


FAILURE = '''use nexo.web
pub page Home:
    path = "/"
    state visible: bool = false
    fn toggle():
        visible = not visible
    view:
        button("Toggle", on_click: toggle)
        if visible:
            Broken()
        else:
            Counter()
component Broken:
    state number: int = 9223372036854775807 + 1
    view:
        p("{number}")
component Counter:
    state count: int = 0
    fn increment():
        count += 1
    view:
        p("Old count: {count}")
        button("Old increment", on_click: increment)
'''


def test_real_browser_failed_new_branch_preserves_old_behavior(tmp_path, browser):
    with served(project(tmp_path, FAILURE)) as base:
        page = browser.new_page();errors=[];page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/');page.wait_for_function('document.querySelector("#app").dataset.nexoHydration==="ready"')
        page.evaluate('window.oldButton=[...document.querySelectorAll("button")].find(el=>el.textContent==="Old increment");')
        page.get_by_role('button', name='Toggle', exact=True).click()
        assert errors and 'Nexo int overflow' in errors[-1]
        assert page.evaluate('document.contains(oldButton)')
        page.get_by_role('button', name='Old increment', exact=True).click()
        assert page.get_by_text('Old count: 1', exact=True).count() == 1
        page.get_by_role('button', name='Toggle', exact=True).click()
        page.get_by_role('button', name='Toggle', exact=True).click()
        page.get_by_role('button', name='Old increment', exact=True).click()
        assert page.get_by_text('Old count: 2', exact=True).count() == 1
        page.close()


def test_real_browser_conditional_ssr_without_javascript(tmp_path, browser):
    with served(project(tmp_path, EXAMPLE.read_text())) as base:
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page();page.goto(base + '/')
        assert page.get_by_text('Local: 0', exact=True).count() == 1
        assert page.get_by_text('Increment the parent to project content', exact=True).count() == 1
        assert page.get_by_text('Projected parent: 0', exact=True).count() == 0
        context.close()


def test_real_browser_nested_regions_share_parent_without_crossing_bounds(tmp_path, browser):
    source = '''use nexo.web
pub page Home:
    path = "/"
    state visible: bool = true
    state number: int = 0
    fn toggle():
        visible = not visible
    fn next():
        number += 1
    view:
        main:
            button("Toggle", on_click: toggle)
            button("Next", on_click: next)
            p("Outside")
            if visible:
                h2("Outer")
                if number > 0:
                    p("Dynamic {number}")
                else:
                    p("Waiting")
                p("Stable")
            else:
                p("Hidden")
'''
    with served(project(tmp_path, source)) as base:
        page = browser.new_page();page.goto(base + '/')
        page.wait_for_function('document.querySelector("#app").dataset.nexoHydration==="ready"')
        page.evaluate('window.heading=document.querySelector("h2");window.stable=[...document.querySelectorAll("p")].find(el=>el.textContent==="Stable");')
        page.get_by_role('button', name='Next', exact=True).click()
        assert page.get_by_text('Dynamic 1', exact=True).count() == 1
        assert page.evaluate('document.querySelector("h2")===heading && document.contains(stable)')
        page.get_by_role('button', name='Next', exact=True).click()
        page.evaluate('window.oldDynamic=[...document.querySelectorAll("p")].find(el=>el.textContent==="Dynamic 2");')
        page.get_by_role('button', name='Toggle', exact=True).click()
        page.get_by_role('button', name='Next', exact=True).click()
        assert page.evaluate('oldDynamic.textContent==="Dynamic 2"')
        page.get_by_role('button', name='Toggle', exact=True).click()
        assert page.get_by_text('Dynamic 3', exact=True).count() == 1
        assert page.evaluate('document.querySelector("h2")!==heading && !document.contains(stable)')
        page.close()


@pytest.mark.parametrize('mutation', [
    'end.remove()',
    'start.before(end)',
    'end.parentNode.append(end)',
    'start.data="corrupted"',
])
def test_real_browser_live_marker_damage_cannot_remove_unrelated_dom(tmp_path, browser, mutation):
    source = EXAMPLE.read_text().replace('                p("Panel hidden")', '                p("Panel hidden")\n            p("Outside tail")')
    with served(project(tmp_path, source)) as base:
        page = browser.new_page();errors=[];page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/');page.wait_for_function('document.querySelector("#app").dataset.nexoHydration==="ready"')
        page.evaluate('''() => {
          const walker=document.createTreeWalker(document.querySelector('#app'),NodeFilter.SHOW_COMMENT);
          walker.nextNode();window.start=walker.currentNode;
          const closing='/nexo-if:'+start.data.split(':')[1];
          while(walker.nextNode()){if(walker.currentNode.data===closing){window.end=walker.currentNode;break;}}
        }''')
        page.evaluate(mutation)
        page.get_by_role('button', name='Toggle panel', exact=True).click()
        assert errors and 'NW6005' in errors[-1]
        assert page.get_by_role('heading', name='Hydrated conditional ownership', exact=True).count() == 1
        assert page.get_by_text('Outside tail', exact=True).count() == 1
        page.get_by_role('button', name='Increment local', exact=True).click()
        assert page.get_by_text('Local: 1', exact=True).count() == 1
        page.close()
