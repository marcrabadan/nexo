"""Optional real-browser QA: pip install playwright==1.56.0; playwright install chromium.
Start python -m http.server 8765 --bind 127.0.0.1 --directory docs first.
"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        errors=[]
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://127.0.0.1:8765/ai-guide.html')
        page.wait_for_function('!document.querySelector("#profile").disabled')
        expected = json.loads((Path('docs')/page.get_attribute('main', 'data-bundle')/'prompts.json').read_text())['prompts']
        for profile in ('developer','architect','agentic','beginner'):
            page.select_option('#profile',profile)
            for experience in ('beginner','developer','expert'):
                page.select_option('#experience',experience)
                for mode in ('compact','full'):
                    page.check(f'input[name="mode"][value="{mode}"]')
                    for access in ('offline','url'):
                        page.check(f'input[name="access"][value="{access}"]')
                        key=f'{profile}-{mode}-{access}'+('' if experience=='developer' else '-'+experience)
                        assert page.locator('#prompt').input_value() == expected[key]['text']
        page.wait_for_function('!document.querySelector("#score-readiness").disabled')
        page.select_option('#experience','beginner')
        assert 'what do you still need to learn' in page.locator('label[for="question-0"]').inner_text()
        page.select_option('#experience','expert')
        assert 'domain assumptions' in page.locator('label[for="question-0"]').inner_text()
        page.fill('#example-search','database')
        assert page.locator('.verified-example:visible').count()==1
        page.fill('#example-search','')
        assert page.locator('.verified-example').count()==5
        page.click('#make-readiness')
        assert 'Missing information' in page.locator('#project-report').input_value()
        page.get_by_text('Production readiness assessment',exact=True).click()
        page.wait_for_function('!document.querySelector("#score-readiness").disabled')
        page.click('#score-readiness')
        assert json.loads(page.locator('#production-report').input_value())['score']==0
        page.select_option('#review-functional','Passed')
        page.click('#score-readiness')
        assert json.loads(page.locator('#production-report').input_value())['score']==0
        page.fill('#review-functional-evidence','Reviewed test log')
        page.click('#score-readiness')
        assert json.loads(page.locator('#production-report').input_value())['score']==12.5
        page.get_by_text('Architecture, ADRs and deterministic workflow',exact=True).click()
        for key in ('context','decision','alternatives','consequences','cost_complexity','reconsider_when'):
            page.fill('#adr-'+key,'Review this proposed choice')
        page.click('#make-adr')
        assert 'Status: Proposed' in page.locator('#adr-report').input_value()
        page.get_by_text('Local token and cost comparison',exact=True).click()
        for key,value in [('baseline-tokens','1000'),('output-tokens','100'),('input-rate','2'),('output-rate','4')]:
            page.fill('#'+key,value)
        page.click('#estimate-cost')
        assert '0.002400' in page.locator('#cost-report').inner_text()
        page.wait_for_function('!document.querySelector("#context-version").disabled')
        options=page.locator('#context-version option').evaluate_all('(items)=>items.map(item=>({value:item.value,revision:item.dataset.revision}))')
        archive=next(item for item in options if item['value']!=page.get_attribute('main','data-bundle'))
        page.select_option('#context-version',archive['value'])
        page.wait_for_function('!document.querySelector("#profile").disabled')
        assert archive['revision'] in page.locator('#prompt').input_value()
        page.select_option('#context-version',page.get_attribute('main','data-bundle'))
        page.wait_for_function('!document.querySelector("#profile").disabled')
        assert page.get_attribute('main','data-revision') in page.locator('#prompt').input_value()
        page.evaluate('Object.defineProperty(navigator, "clipboard", {configurable:true,value:{writeText:async text=>{window.copied=text;}}})')
        page.click('#copy-prompt')
        page.wait_for_function('document.querySelector("#copy-status").textContent.includes("Prompt copied")')
        assert page.evaluate('window.copied') == page.locator('#prompt').input_value()
        page.evaluate('Object.defineProperty(navigator, "clipboard", {configurable:true,value:{writeText:async()=>{throw Error("denied")}}})')
        page.click('#copy-prompt')
        page.wait_for_function('document.querySelector("#copy-status").textContent.includes("Command+C")')
        assert page.evaluate('document.activeElement.id') == 'prompt'
        assert page.evaluate('document.querySelector("#prompt").selectionEnd') > 100
        page.keyboard.press('Tab')
        assert page.evaluate('document.activeElement.id') == 'copy-prompt'
        for width in (375,768,1024,1100,1280,1440):
            page.set_viewport_size({'width':width,'height':950})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),width
        page.set_viewport_size({'width':1440,'height':1000})
        page.evaluate('window.scrollTo(0,0)')
        page.screenshot(path='/tmp/nexo-ai-guide-desktop.png',full_page=True)
        page.click('#theme')
        page.screenshot(path='/tmp/nexo-ai-guide-light.png',full_page=True)
        page.set_viewport_size({'width':375,'height':850})
        page.click('#menu')
        assert page.get_attribute('#menu','aria-expanded') == 'true'
        assert page.locator('#nav a[href="ai-guide.html"]').is_visible()
        page.screenshot(path='/tmp/nexo-ai-guide-mobile.png',full_page=True)
        assert not errors,errors
        fallback = context.new_page()
        fallback.route('**/prompts.json',lambda route:route.abort())
        fallback.goto('http://127.0.0.1:8765/ai-guide.html')
        fallback.wait_for_function('document.querySelector("#copy-status").textContent.includes("switching unavailable")')
        assert fallback.locator('#profile').is_disabled()
        assert 'fn welcome' in fallback.locator('#prompt').input_value()
        mismatch = context.new_page()
        mismatch.route('**/prompts.json', lambda route: route.fulfill(json={'revision':'incorrect','prompts':{}}))
        mismatch.goto('http://127.0.0.1:8765/ai-guide.html')
        mismatch.wait_for_function('document.querySelector("#copy-status").textContent.includes("switching unavailable")')
        assert mismatch.locator('#profile').is_disabled()
        nojs = browser.new_context(java_script_enabled=False).new_page()
        nojs.goto('http://127.0.0.1:8765/ai-guide.html')
        assert 'fn welcome' in nojs.locator('#prompt').input_value()
        assert nojs.locator('noscript').is_visible()
        browser.close()
    print('Browser QA passed: 48 variants, version selection, example filtering, readiness, ADR, cost comparison, copy success/denial, keyboard fallback, six viewport widths, theme/menu, fetch failure and no JavaScript.')

if __name__ == '__main__':
    main()
