"""Compiler-only JSON protocol used by the optional Vite plugin. No user JS runs."""
import argparse
import hashlib
import json
from pathlib import Path

from .web_frontend import artifacts,WebError
from .web_diagnostics import diagnostic


def compile_entry(file,scope_seed=None):
    try:
        hmr={'scope_seed':scope_seed} if scope_seed else {}
        files,program=artifacts(file,True,hmr=hmr)
        mapping=json.loads(files['app.js.map'])
        return {'schema_version':1,'diagnostics':[],'assets':files,'hmr':hmr,
                'dependencies':[str(p) for p in sorted(program.dependencies)],
                'bootstrap':('mountApp' if any(d.public and d.kind=='page' for u in program.units for d in u.declarations.values()) else next(d.symbol for d in program.root.declarations.values() if d.public and d.kind=='component')),
                'javascript':hashlib.sha256((files['app.js']+mapping['mappings']).encode()).hexdigest(),
                'css':hashlib.sha256(files['app.css'].encode()).hexdigest()}
    except (WebError,OSError,UnicodeError) as error:
        return {'schema_version':1,'diagnostics':[diagnostic(error,file)]}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('file');parser.add_argument('--scope-seed');args=parser.parse_args()
    result=compile_entry(str(Path(args.file).resolve()),args.scope_seed)
    print(json.dumps(result,sort_keys=True))
    return int(bool(result['diagnostics']))


if __name__=='__main__':raise SystemExit(main())
