#!/usr/bin/env python3
"""Local Nexo Web quality measurements. Timing is evidence, not a universal SLO."""
import argparse
from contextlib import contextmanager
import functools
import gzip
import hashlib
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from nexo.web_frontend import artifacts
from nexo.web_release import optimize,optimizer_revision,input_revision

FIXTURES={'counter':'examples/web/counter.nexo','composition':'examples/web/composition/app.nexo',
          'data':'examples/web/data/app.nexo','production':'examples/web/production/app.nexo'}


@contextmanager
def serve(folder):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(folder)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/'
    finally:server.shutdown();server.server_close();thread.join()


def size(files,suffix):
    values=[text.encode() for name,text in files.items() if name.endswith(suffix)]
    return {'bytes':sum(map(len,values)),'gzip_bytes':sum(len(gzip.compress(value,mtime=0)) for value in values)}


def summary(samples):
    ordered=sorted(samples)
    return {'samples':samples,'median':statistics.median(samples),'p95':ordered[max(0,int(len(ordered)*.95+.999)-1)]}


def checks(report,budget):
    failed=[]
    for name,item in report['fixtures'].items():
        if item['release']['javascript']['gzip_bytes']>budget['javascript_gzip_bytes_max']:failed.append(name+': JS gzip budget')
        if item['release']['css']['gzip_bytes']>budget['css_gzip_bytes_max']:failed.append(name+': CSS gzip budget')
    for mode,item in report['counter'].items():
        if max(item['text_mutations'])>budget['counter_text_mutations_max'] or not item['node_identity_preserved']:failed.append(mode+': DOM update budget')
    if len(report['accessibility']['violations'])>budget['axe_violations_max']:failed.append('axe violations')
    return failed


def measure(iterations=5):
    from playwright.sync_api import sync_playwright,expect
    budget=json.loads((ROOT/'docs/web/quality/budgets.json').read_text())
    report={'schema_version':1,'benchmark':'nexo-web-local-m5','optimizer_sha256':optimizer_revision(),'measurement_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'environment':{'platform':platform.platform(),'machine':platform.machine(),'python':platform.python_version(),
                           'node':subprocess.check_output(['node','--version'],text=True).strip()},
            'method':{'iterations':iterations,'warmup_runs':1,'network':'loopback HTTP without compression',
                      'browser_cache':'new context per run','os_cache':'not controlled','cpu_throttle':'none added',
                      'timings':'browser NavigationTiming DCL and synchronous event loop; no timing CI threshold',
                      'gzip':'estimated per-file gzip mtime=0, not transferred compressed bytes'},
            'fixtures':{},'counter':{},'budgets':budget}
    with tempfile.TemporaryDirectory(prefix='nexo-web-measure-') as temporary:
        folder=Path(temporary)
        for name,path in FIXTURES.items():
            entry=ROOT/path;raw,program=artifacts(entry);release=optimize(raw,entry.parent)
            for mode,files in [('development',raw),('release',release)]:
                out=folder/name/mode;out.mkdir(parents=True)
                for asset,text in files.items():
                    target=out/asset;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text)
            report['fixtures'][name]={'entry':path,'source_sha256':hashlib.sha256(entry.read_bytes()).hexdigest(),
                                     'input_sha256':input_revision(raw),
                                     **{mode:{'javascript':size(files,'.js'),'css':size(files,'.css')} for mode,files in [('development',raw),('release',release)]}}
        axe=ROOT/'nexo/web_tools/node_modules/axe-core/axe.min.js'
        if not axe.exists():raise RuntimeError('Pinned axe-core missing; run npm ci --prefix nexo/web_tools')
        shutil.copy(axe,folder/'axe.js')
        with serve(folder) as base,sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path=shutil.which('chromium'))
            report['environment']['browser']=browser.version
            try:
                for mode in ('development','release'):
                    navigation=[];updates=[];mutations=[];identities=True;requests=[]
                    for run in range(iterations+1):
                        context=browser.new_context(service_workers='block');page=context.new_page()
                        try:
                            page.goto(base+'counter/'+mode+'/');expect(page.get_by_role('button',name='+',exact=True)).to_be_visible()
                            result=page.evaluate('''(clicks)=>{
                              const text=document.querySelector('p').firstChild,heading=document.querySelector('h1'),button=document.querySelector('button');
                              const observer=new MutationObserver(()=>{});observer.observe(document.querySelector('#app'),{subtree:true,childList:true,characterData:true,attributes:true});
                              const start=performance.now();for(let i=0;i<clicks;i++)button.click();const duration=performance.now()-start;
                              const changes=observer.takeRecords();observer.disconnect();
                              return {navigation:performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd,
                                duration,mutations:changes.length,identity:text===document.querySelector('p').firstChild&&heading===document.querySelector('h1'),
                                count:document.querySelector('p').textContent,requests:performance.getEntriesByType('resource').filter(e=>/\\.(js|css)$/.test(new URL(e.name).pathname)).length};
                            }''',budget['counter_clicks'])
                            if result['count']!='Count: '+str(budget['counter_clicks']):raise RuntimeError('Counter update failed')
                            if run:
                                navigation.append(round(result['navigation'],3));updates.append(round(result['duration'],3));mutations.append(result['mutations']);identities&=result['identity'];requests.append(result['requests'])
                        finally:context.close()
                    report['counter'][mode]={'dcl_ms':summary(navigation),'clicks_ms':summary(updates),
                                             'text_mutations':mutations,'node_identity_preserved':identities,'js_css_requests':requests}
                page=browser.new_page();page.goto(base+'production/release/');expect(page.get_by_role('button',name='Increment',exact=True)).to_be_visible()
                page.add_script_tag(url=base+'axe.js')
                report['accessibility']=page.evaluate("async()=>{const r=await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa','best-practice']}});return {engine:'axe-core',version:axe.version,scope:'Production example, initial state, Chromium',violations:r.violations.map(x=>x.id),incomplete:r.incomplete.map(x=>x.id),passed_rules:r.passes.length};}")
                page.close()
            finally:browser.close()
    report['failures']=checks(report,budget)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--iterations',type=int,default=5);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    if not 3<=args.iterations<=30:parser.error('iterations must be 3..30')
    report=measure(args.iterations);args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print(json.dumps({'report':str(args.output),'failures':report['failures']}))
    return int(args.check and bool(report['failures']))


if __name__=='__main__':raise SystemExit(main())
