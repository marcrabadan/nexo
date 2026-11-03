#!/usr/bin/env python3
"""M6 demo contract E2E against an existing deployment; starts no application server."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


def base_url(value):
    url=urlsplit(value)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('','/'):
        raise argparse.ArgumentTypeError('Use an HTTP(S) origin URL without credentials, query, fragment or path prefix')
    return value.rstrip('/')+'/'


def ssr(page,base):
    response=page.goto(base)
    assert response.status==200, f'Root status {response.status}'
    assert page.locator('h2').inner_text()=='Async server snapshot'
    assert page.locator('#row-1 h3').inner_text()=='One'
    assert page.locator('#row-2 h3').inner_text()=='Two'
    assert page.locator('#app').get_attribute('data-nexo-hydration')!='ready'


def hydrated(page,base):
    page.goto(base);page.locator('#app[data-nexo-hydration="ready"]').wait_for()
    page.evaluate('window.originalRow=document.querySelector("#row-1")')
    page.locator('#row-1 button').click()
    page.get_by_role('button',name='Reorder products',exact=True).click()
    assert page.evaluate('document.querySelector("#row-1")===originalRow')
    assert page.locator('#row-1 button').inner_text()=='Local count: 1'
    assert page.get_by_text('Shared interactions: 1',exact=True).count()==1
    assert page.locator('#row-2 h3').inner_text()=='Two updated'
    page.get_by_role('button',name='Toggle list',exact=True).click()
    assert page.locator('#row-1').count()==0
    page.get_by_role('button',name='Toggle list',exact=True).click()
    assert page.locator('#row-1 button').inner_text()=='Local count: 0'


def wasm(page,base):
    page.goto(base)
    assert page.evaluate('window.isSecureContext'), 'WASM integrity requires HTTPS with a trusted certificate, or loopback; HTTP Kubernetes service DNS is not a secure context'
    page.get_by_text('WASM result: 42',exact=True).wait_for()


def events(page,base):
    from playwright.sync_api import expect
    page.goto(base);notice=page.locator('#notice')
    expect(notice).to_have_text(__import__('re').compile(r'Public (event stream connected|snapshot [0-9]+)'))
    before=notice.inner_text()
    expect(notice).not_to_have_text(before)


def http_boundary(page,base):
    response=page.goto(base)
    csp=response.headers.get('content-security-policy','')
    assert "default-src 'none'" in csp and "script-src 'self'" in csp, 'Ingress must preserve application CSP'
    assert response.headers.get('x-content-type-options')=='nosniff'
    assert 'no-store' in response.headers.get('cache-control','')
    response=page.goto(base+'__nexo_e2e_missing__')
    assert response.status==404, f'Unknown route status {response.status}'


CASES=(('ssr_without_javascript',ssr,False),('hydration_lists_context',hydrated,True),
       ('wasm_worker',wasm,True),('live_sse',events,True),('http_ingress_headers_404',http_boundary,False))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',type=base_url,default=os.environ.get('NEXO_E2E_BASE_URL'),required=not os.environ.get('NEXO_E2E_BASE_URL'))
    parser.add_argument('--browser',choices=('chromium','firefox','webkit'),default=os.environ.get('NEXO_E2E_BROWSER','chromium'))
    parser.add_argument('--results',type=Path,default=Path('/results'))
    parser.add_argument('--timeout-ms',type=int,default=15000)
    parser.add_argument('--executable-path',help='Optional local browser binary; omitted in the Job image.')
    parser.add_argument('--artifact-grace-seconds',type=int,default=int(os.environ.get('NEXO_E2E_ARTIFACT_GRACE_SECONDS','0')))
    args=parser.parse_args(argv)
    if not 1000<=args.timeout_ms<=120000 or not 0<=args.artifact_grace_seconds<=600:parser.error('Timeout/grace outside supported bounds')
    output=args.results.resolve();output.mkdir(parents=True,exist_ok=True)
    (output/'report.json').unlink(missing_ok=True)
    cases=[];browser_version=None;launch_error=None
    from playwright.sync_api import sync_playwright,expect
    expect.set_options(timeout=args.timeout_ms)
    try:
        with sync_playwright() as playwright:
            options={'headless':True,'timeout':30000}
            if args.executable_path:options['executable_path']=args.executable_path
            browser=getattr(playwright,args.browser).launch(**options);browser_version=browser.version
            try:
                for name,test,javascript in CASES:
                    started=time.monotonic();context=browser.new_context(java_script_enabled=javascript,ignore_https_errors=False)
                    context.set_default_timeout(args.timeout_ms);page=context.new_page();errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    context.tracing.start(screenshots=True,snapshots=True,sources=False)
                    error=None
                    try:
                        test(page,args.base_url)
                        assert not errors, 'Browser errors: '+'; '.join(errors)
                    except Exception as exc:
                        error=str(exc)
                        try:page.screenshot(path=str(output/(name+'.png')),timeout=5000,full_page=True)
                        except Exception:pass
                    finally:
                        context.tracing.stop(path=str(output/(name+'.zip')) if error else None)
                        context.close()
                    cases.append({'name':name,'status':'failed' if error else 'passed','error':error,'seconds':round(time.monotonic()-started,3)})
                    print(f"{name}: {cases[-1]['status']}",flush=True)
            finally:browser.close()
    except Exception as exc:launch_error=str(exc)
    passed=not launch_error and len(cases)==len(CASES) and all(case['status']=='passed' for case in cases)
    report={'schema_version':1,'status':'passed' if passed else 'failed','scope':'deployed M6 demo contract, not arbitrary application or production certification',
            'base_url':args.base_url,'browser':args.browser,'browser_version':browser_version,'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'tests':cases,'runner_error':launch_error,'tls_verification':True}
    suite=ET.Element('testsuite',name='deployed-m6',tests=str(len(cases)+(1 if launch_error else 0)),failures=str(sum(case['status']=='failed' for case in cases)),errors='1' if launch_error else '0')
    for case in cases:
        node=ET.SubElement(suite,'testcase',name=case['name'],time=str(case['seconds']))
        if case['error']:ET.SubElement(node,'failure').text=case['error']
    if launch_error:ET.SubElement(ET.SubElement(suite,'testcase',name='runner'),'error').text=launch_error
    ET.ElementTree(suite).write(output/'junit.xml',encoding='utf-8',xml_declaration=True)
    temporary=output/'report.tmp';temporary.write_text(json.dumps(report,indent=2)+'\n');temporary.replace(output/'report.json')
    print('NEXO_E2E_FINISHED '+report['status'],flush=True)
    # Bounded artifact collection window, used only by the Kubernetes Job.
    for _ in range(args.artifact_grace_seconds):
        if (output/'.collected').exists():break
        time.sleep(1)
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
