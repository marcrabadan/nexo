"""Build public Nexo Web page and real demo from the acceptance source. --check."""
import argparse
import html
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from nexo.web_frontend import compile_web, browser_assets
from nexo.web_model import load_program


def generate():
    source = (ROOT/'examples/web/counter.nexo').read_text()
    js, css, _ = compile_web(source)
    m2_path=ROOT/'examples/web/composition/app.nexo'
    m2_source=m2_path.read_text()
    page = (ROOT/'docs/web/page.tmpl').read_text().replace('@@COUNTER@@', html.escape(source)).replace('@@COMPOSITION@@',html.escape(m2_source))
    extra={'docs/web-m2-demo/'+name:text for name,text in browser_assets(*compile_web(m2_source,m2_path)).items()}
    for path in load_program(m2_source,m2_path).dependencies:
        extra['docs/web-m2-demo/source/'+path.relative_to(m2_path.parent).as_posix()]=path.read_text()
    return {**extra, 'docs/web.html': page, 'docs/web-demo/app.js': js, 'docs/web-demo/app.css': css,
            'docs/web-demo/main.js': 'import {Counter} from "./app.js";\nCounter(document.querySelector("#app"));\nCounter(document.querySelector("#app"));\n',
            'docs/web-demo/index.html': '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'self\'; base-uri \'none\'; form-action \'none\'"><title>Nexo Web Counter demo</title><link rel="stylesheet" href="app.css"></head><body><h1>Two independent Nexo components</h1><p>Each button updates its own count. <a href="../web.html">Nexo Web documentation</a>.</p><div id="app"></div><script type="module" src="main.js"></script></body></html>\n'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for name, content in generate().items():
        target = ROOT/name
        if args.check:
            if not target.exists() or target.read_text() != content:
                raise SystemExit('Stale Nexo Web artifact: '+name)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)


if __name__ == '__main__':
    main()
