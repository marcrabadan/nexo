"""Build public Nexo Web page and real demo from the acceptance source. --check."""
import argparse
import html
import json
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from nexo.web_frontend import compile_web, browser_assets
from nexo.web_model import load_program


def generate(release=False):
    source = (ROOT/'examples/web/counter.nexo').read_text()
    js, css, _ = compile_web(source)
    m2_path=ROOT/'examples/web/composition/app.nexo'
    m2_source=m2_path.read_text()
    m3_path=ROOT/'examples/web/data/app.nexo'
    m3_source=m3_path.read_text()
    pages = {name: (ROOT/'docs/web'/template).read_text()
             for name, template in [('web.html', 'page.tmpl'),
                                    ('web-guide.html', 'guide.tmpl'),
                                    ('web-api.html', 'api.tmpl'),
                                    ('web-development.html', 'development.tmpl'),
                                    ('web-deployment.html', 'deployment.tmpl')]}
    extra={'docs/web-m2-demo/'+name:text for name,text in browser_assets(*compile_web(m2_source,m2_path)).items()}
    keyed=ROOT/'examples/web/keyed/app.nexo'
    extra.update({'docs/web-keyed-demo/'+name:text for name,text in browser_assets(*compile_web(keyed.read_text(),keyed)).items()})
    extra['docs/web-keyed-demo/source/app.nexo']=keyed.read_text()
    for path in load_program(m2_source,m2_path).dependencies:
        extra['docs/web-m2-demo/source/'+path.relative_to(m2_path.parent).as_posix()]=path.read_text()
    extra.update({'docs/web-m3-demo/'+name:text for name,text in browser_assets(*compile_web(m3_source,m3_path)).items()})
    extra['docs/web-m3-demo/main.js']='import {App} from "./app.js";\nApp(document.querySelector("#app"),{endpoint:new URL("./products.json",location.href).pathname,writable:false});\n'
    extra['docs/web-m3-demo/products.json']='[{"id":1,"name":"Nexo handbook"},{"id":2,"name":"Typed browser data"}]\n'
    for name in ('app.nexo','nexo.toml','server.py'):
        extra['docs/web-m3-demo/source/'+name]=(m3_path.parent/name).read_text()
    from nexo.web_frontend import artifacts
    from nexo.web_release import optimize,read_release,optimizer_revision,input_revision
    production=ROOT/'examples/web/production/app.nexo'
    raw,_=artifacts(production)
    if release:optimized=optimize(raw,production.parent)
    else:optimized=read_release(ROOT/'docs/web-release-demo',raw)
    extra.update({'docs/web-release-demo/'+name:text for name,text in optimized.items()})
    for name in ('app.nexo','nexo.toml','nginx.conf','Dockerfile','README.md'):
        extra['docs/web/production/'+name]=(production.parent/name).read_text()
    report=json.loads((ROOT/'docs/web/quality-report.json').read_text())
    budgets=json.loads((ROOT/'docs/web/quality/budgets.json').read_text())
    if report['optimizer_sha256']!=optimizer_revision() or report['measurement_sha256']!=hashlib.sha256((ROOT/'scripts/web/measure.py').read_bytes()).hexdigest() or report['budgets']!=budgets or report['failures']:
        raise ValueError('Stale/failed Web measurement: run scripts/web/measure.py --output docs/web/quality-report.json --check')
    rows=[]
    for name,item in report['fixtures'].items():
        entry=ROOT/item['entry'];current,_=artifacts(entry)
        if item['input_sha256']!=input_revision(current):raise ValueError('Stale Web measurement fixture: '+name)
        before=item['development']['javascript']['gzip_bytes'];after=item['release']['javascript']['gzip_bytes']
        rows.append('<tr><th scope="row">'+html.escape(name)+'</th><td>'+str(before)+'</td><td>'+str(after)+'</td></tr>')
    replacements = {'COUNTER': html.escape(source), 'COMPOSITION': html.escape(m2_source),
                    'DATA': html.escape(m3_source), 'PRODUCTION': html.escape(production.read_text()),
                    'QUALITY_ROWS': ''.join(rows)}
    for name, page in pages.items():
        for key, value in replacements.items():
            page = page.replace('@@'+key+'@@', value)
        extra['docs/'+name] = page
    return {**extra, 'docs/web-demo/app.js': js, 'docs/web-demo/app.css': css,
            'docs/web-demo/main.js': 'import {Counter} from "./app.js";\nCounter(document.querySelector("#app"));\nCounter(document.querySelector("#app"));\n',
            'docs/web-demo/index.html': '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'self\'; base-uri \'none\'; form-action \'none\'"><title>Nexo Web Counter demo</title><link rel="stylesheet" href="app.css"></head><body><h1>Two independent Nexo components</h1><p>Each button updates its own count. <a href="../web.html">Nexo Web documentation</a>.</p><div id="app"></div><script type="module" src="main.js"></script></body></html>\n'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--release',action='store_true',help='Explicitly rebuild the optimized public demo with optional pinned tooling')
    args = parser.parse_args()
    generated=generate(args.release)
    if args.release and not args.check:
        from nexo.web_release import write_release
        prefix='docs/web-release-demo/'
        write_release({name.removeprefix(prefix):content for name,content in generated.items() if name.startswith(prefix)},ROOT/prefix)
    for name, content in generated.items():
        target = ROOT/name
        if args.check:
            if not target.exists() or target.read_text() != content:
                raise SystemExit('Stale Nexo Web artifact: '+name)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)


if __name__ == '__main__':
    main()
