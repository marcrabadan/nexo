"""Nexo Web M1–M5 and experimental M6a: a separate, deliberately bounded browser compiler (RFC-0001).

Core expression parsing is shared with Nexo. Browser-only contextual blocks do
not change the core grammar, interpreter, HTTP module, or WASI build target.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import html
import json
from pathlib import Path
import re
import sys

from . import ast as A
from .parser import parse
from .lexer import KEYWORDS
from .errors import NexoError

IDENT = r'[A-Za-z_][A-Za-z_0-9]*'
TAGS = set('main section article header footer nav div span h1 h2 h3 h4 h5 h6 p button label input ul ol li strong em small form fieldset legend output pre code br hr'.split())
ATTRS = {'class', 'id', 'title', 'role', 'type', 'name', 'placeholder', 'value', 'checked', 'disabled', 'aria_label', 'aria_live', 'aria_describedby', 'aria_labelledby', 'aria_hidden', 'tabindex', 'for'}
VOID = {'input', 'br', 'hr'}


class WebError(Exception):
    def __init__(self, line, message, code='NW1001'):
        self.line, self.message, self.code = line, message, code
        super().__init__(message)


def fail(line, message, code='NW1001'):
    raise WebError(line, message + ' Fix: use the documented browser syntax in docs/rfc/0001-nexo-web.md.', code)


@dataclass
class Element:
    tag: str
    text: A.Expr | None = None
    attrs: list = field(default_factory=list)
    children: list = field(default_factory=list)
    line: int = 1


@dataclass
class Component:
    name: str
    public: bool
    states: dict = field(default_factory=dict)
    actions: dict = field(default_factory=dict)
    view: list = field(default_factory=list)
    css: str = ''
    scope: str = ''


def core(source, line):
    try:
        body = parse(source + '\n', '<web-expression>').body
        if len(body) != 1:
            fail(line, 'Expected exactly one statement.')
        return body[0]
    except (NexoError, IndexError, RecursionError):
        fail(line, 'Invalid Nexo expression.')


def expression(source, line):
    node = core(source, line)
    if not isinstance(node, A.ExprStmt):
        fail(line, 'Expected a value expression.')
    return node.expr


def value(expr, states, line):
    """Typed expression lowering plus exact static dependencies; no eval/raw JS."""
    if isinstance(expr, A.Num) and not expr.is_float:
        n = int(expr.text.replace('_', ''))
        if not -(2**63) <= n < 2**63:
            fail(line, 'Integer literal exceeds the Nexo int range.', 'NW1002')
        return f'{n}n', 'int', set()
    if isinstance(expr, A.Bool):
        return str(expr.value).lower(), 'bool', set()
    if isinstance(expr, A.Str):
        parts, deps = [], set()
        for p in expr.parts:
            if isinstance(p, str):
                parts.append(json.dumps(p, ensure_ascii=True))
            else:
                js, typ, names = value(p, states, line)
                if typ not in ('str','int','bool'):fail(line,'Interpolation requires scalar data.','NW3001')
                parts.append(f'String({js})')
                deps |= names
        return '(' + '+'.join(parts or ['""']) + ')', 'str', deps
    if isinstance(expr, A.Name) and expr.name in states:
        binding = states[expr.name]
        if hasattr(binding, 'code'):
            if binding.type in ('context','resource'):
                fail(line, 'Read a context/resource field or call a declared operation.', 'NW2004')
            return binding.code, binding.type, {binding.dep} if binding.dep else set()
        return f's[{json.dumps(expr.name)}]', binding[0], {expr.name}
    if isinstance(expr, A.NoneLit):
        return 'null', 'none', set()
    from .web_data import resource_value, constructor
    result=resource_value(expr, states, line)
    if result is not None:return result
    result=constructor(expr,states,line)
    if result is not None:return result
    if isinstance(expr,A.ListLit):
        values=[value(item,states,line) for item in expr.items]
        if not values or any(v[1]!=values[0][1] for v in values):fail(line,'JSON list literals require a nonempty homogeneous type.', 'NW3001')
        return 'Object.freeze(['+','.join(v[0] for v in values)+'])',('list',values[0][1]),set().union(*(v[2] for v in values))
    if isinstance(expr, A.Attr):
        if isinstance(expr.obj,A.Name):
            binding = states.get(expr.obj.name)
            if binding and getattr(binding, 'type', None) == 'context' and expr.name in binding.context.states:
                return f'{binding.code}.values[{json.dumps(expr.name)}]', binding.context.states[expr.name].type, {f'@{expr.obj.name}.{expr.name}'}
        js,typ,deps=value(expr.obj,states,line)
        if isinstance(typ,tuple) and typ[0]=='record' and expr.name in dict(typ[2]):
            return js+'['+json.dumps(expr.name)+']',dict(typ[2])[expr.name],deps
        fail(line,'Unknown record field or unguarded request error.', 'NW3001')
    if isinstance(expr,A.Compare) and len(expr.ops)==1 and expr.ops[0] in ('==','!=','<','>','<=','>='):
        left,lt,ld=value(expr.operands[0],states,line);right,rt,rd=value(expr.operands[1],states,line)
        if (lt==rt and lt in ('int','str','bool')) or (lt=='error?' and rt=='none' and expr.ops[0] in ('==','!=')):
            op={'==':'===','!=':'!=='}.get(expr.ops[0],expr.ops[0])
            return '('+left+op+right+')','bool',ld|rd
    if isinstance(expr,A.BoolOp):
        left,lt,ld=value(expr.left,states,line);right,rt,rd=value(expr.right,states,line)
        if lt==rt=='bool':return '('+left+('&&' if expr.op=='and' else '||')+right+')','bool',ld|rd
    if isinstance(expr, A.Unary) and expr.op == 'not':
        js, typ, deps = value(expr.operand, states, line)
        if typ == 'bool':
            return f'!({js})', typ, deps
    if isinstance(expr, A.Unary) and expr.op == '-':
        if isinstance(expr.operand, A.Num) and not expr.operand.is_float and int(expr.operand.text.replace('_', '')) == 2**63:
            return '-9223372036854775808n', 'int', set()
        js, typ, deps = value(expr.operand, states, line)
        if typ == 'int':
            return f'int(-({js}))', typ, deps
    if isinstance(expr, A.BinOp) and expr.op in ('+', '-', '*'):
        left, lt, ld = value(expr.left, states, line)
        right, rt, rd = value(expr.right, states, line)
        if lt == rt == 'int':
            return f'int(({left}){expr.op}({right}))', 'int', ld | rd
        if lt == rt == 'str' and expr.op == '+':
            return f'({left}+{right})', 'str', ld | rd
    fail(line, 'Unknown name, unsupported expression, or incompatible value types.', 'NW1002')


def scoped_css(css, scope, line):
    """Native CSS @scope avoids rewriting selectors, variables and query syntax.

    M1 rejects global at-rules and external resources; no pretending
    a regex is a complete CSS parser. Quoted/commented braces are tokenized.
    """
    tokens = re.findall(r'/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]|[^{}"\'/]+|.', css)
    depth = 0
    plain = ''
    for token in tokens:
        if token.startswith('/*'):
            continue
        if token.startswith(('"', "'")):
            continue
        plain += token
        if token == '{':
            depth += 1
        if token == '}':
            depth -= 1
            if depth < 0:
                fail(line, 'Unbalanced CSS braces.', 'NW1003')
    if depth or '/*' in plain or '\\' in plain:
        fail(line, 'Unbalanced CSS or unsupported CSS escape.', 'NW1003')
    if re.search(r'(?:url|src|image-set|image|expression)\s*\(', plain, re.I):
        fail(line, 'External CSS resources are outside M1.', 'NW1003')
    if any(m.lower() not in ('media', 'container', 'supports') for m in re.findall(r'@([\w-]+)', plain)):
        fail(line, 'Only @media, @container and @supports are supported in M1 CSS.', 'NW1003')
    return f'@scope ([data-nexo="{scope}"]) to ([data-nexo]) {{\n{css}\n}}\n'


def parse_web(source):
    from .web_model import load_program
    return list(load_program(source).root.declarations.values())


def compile_web(source, source_path=None):
    from .web_codegen import compile_program
    from .web_model import load_program
    return compile_program(load_program(source, source_path))


def artifacts(source_path, sourcemap=False, observe=False, hmr=None, static=False, routes=None):
    from .web_model import load_program
    from .web_codegen import compile_program
    source=Path(source_path).read_text(encoding='utf-8')
    program=load_program(source,source_path);mapping={} if sourcemap else None
    css_mapping={} if sourcemap else None
    js,css,exports=compile_program(program,mapping,observe,css_mapping,hmr)
    files=browser_assets(js,css,exports)
    if static:
        if any(d.kind == 'page' and d.public for u in program.units for d in u.declarations.values()):
            from .web_ssg import assets
            return assets(files, program, routes), program
        if routes is not None:
            fail(1, 'Route enumeration requires a routed static application.', 'NW6003')
        from .web_static import assets
        return assets(files, program, exports), program
    if sourcemap:
        files['app.js']+='//# sourceMappingURL=./app.js.map\n'
        files['app.js.map']=json.dumps(mapping,sort_keys=True,ensure_ascii=True)+'\n'
        files['app.css']+='/*# sourceMappingURL=./app.css.map */\n'
        files['app.css.map']=json.dumps(css_mapping,sort_keys=True,ensure_ascii=True)+'\n'
    return files,program


def build(source_path, output, sourcemap=False, release=False, observe=False, static=False, routes=None, pwa=False):
    if pwa and not static:
        fail(1, 'PWA generation requires a public --static build.', 'NW6010')
    if routes is not None and not static:
        fail(1, 'Route enumeration requires --static.', 'NW6003')
    if static and (sourcemap or release or observe):
        fail(1, 'Static builds cannot combine sourcemap, release or observe options.', 'NW6001')
    files,program=artifacts(source_path,sourcemap,observe,static=static,routes=routes)
    if static:
        if pwa:
            from .web_pwa import augment
            files = augment(files)
        from .web_static_output import write_output
        return write_output(files, output)
    if release:
        from .web_release import optimize
        from .web_static_output import write_output
        from .modules import project_root
        return write_output(optimize(files,project_root(str(program.root.path)),sourcemap,observe),output)
    directory=Path(output)
    if (directory/'nexo-manifest.json').exists():
        from .web_static_output import write_output
        return write_output(files,directory)
    directory.mkdir(parents=True,exist_ok=True)
    for name,content in files.items():(directory/name).write_text(content,encoding='utf-8')
    # Rebuilding without maps must not leave a previous source-bearing map behind.
    if not sourcemap:
        for name in ('app.js.map','app.css.map'):(directory/name).unlink(missing_ok=True)
    return files


def browser_assets(js, css, exports):
    return {'app.js': js, 'app.css': css,
             'main.js': f'import {{ {exports[0]} }} from "./app.js";\n{exports[0]}(document.querySelector("#app"));\n',
             'index.html': '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; connect-src \'self\'; script-src \'self\'; style-src \'self\'; base-uri \'none\'; form-action \'none\'"><title>'+html.escape('Nexo Web' if exports[0] == 'mountApp' else exports[0])+'</title><link rel="stylesheet" href="./app.css"></head><body><div id="app"></div><script type="module" src="./main.js"></script></body></html>\n'}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='nexo web', description='Experimental M1–M6 browser compiler; independent of WASI.')
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check')
    check.add_argument('--json', action='store_true')
    check.add_argument('file')
    compile_cmd = sub.add_parser('build')
    compile_cmd.add_argument('file')
    compile_cmd.add_argument('-o', '--output', default='dist')
    compile_cmd.add_argument('--sourcemap',action='store_true')
    compile_cmd.add_argument('--release',action='store_true')
    compile_cmd.add_argument('--observe',action='store_true')
    compile_cmd.add_argument('--static', action='store_true', help='Experimental M6 static HTML and routed SSG (RFC-0002/0003).')
    compile_cmd.add_argument('--pwa', action='store_true', help='Opt-in offline public static site (RFC-0009).')
    compile_cmd.add_argument('--routes', metavar='JSON', help='JSON array of concrete dynamic route paths for --static.')
    dev=sub.add_parser('dev');dev.add_argument('file');dev.add_argument('--port',type=int,default=5173)
    preview=sub.add_parser('preview');preview.add_argument('directory',nargs='?',default='dist');preview.add_argument('--port',type=int,default=4173)
    serve=sub.add_parser('serve', help='Experimental request-time SSR on loopback (RFC-0004).')
    serve.add_argument('file');serve.add_argument('--port', type=int, default=4174)
    serve.add_argument('--loader', help='Explicit trusted Python module:async_function for initial state.')
    serve.add_argument('--wasm', action='store_true', help='Enable import-free WASM worker capability.')
    serve.add_argument('--subscriptions', action='store_true', help='Enable same-origin typed EventSource integration.')
    serve.add_argument('--hydrate', action='store_true', help='Opt-in typed structural hydration (RFC-0005–0008).')
    new=sub.add_parser('new');new.add_argument('directory')
    audit=sub.add_parser('audit');audit.add_argument('--security',action='store_true');audit.add_argument('--accessibility',action='store_true');audit.add_argument('--json',action='store_true');audit.add_argument('file')
    sub.add_parser('lsp')
    args = parser.parse_args(argv)
    if args.command=='audit' and not (args.security or args.accessibility):parser.error('audit requires --security and/or --accessibility')
    try:
        if args.command == 'serve':
            from .web_ssr import serve, load_loader
            return serve(args.file, args.port, hydrate=args.hydrate, loader=load_loader(args.loader) if args.loader else None, wasm=args.wasm, subscriptions=args.subscriptions)
        if args.command in ('new','dev','preview'):
            from .web_tooling import command
            return command(args)
        if args.command=='audit':
            from .web_audit import audit as security_audit
            report=security_audit(args.file,security=args.security,accessibility=args.accessibility)
            if args.json:print(json.dumps(report,sort_keys=True))
            else:
                for finding in report['diagnostics']:print(f"{finding['file']}:{finding['line']}: {finding['code']}: {finding['message']}")
                print('Frontend source audit only; backend authorization, sessions and Shield runtime profiles are not assessed.')
            return int(bool(report['diagnostics']))
        if args.command=='lsp':
            from .web_lsp import serve
            return serve()
        if args.command == 'check':
            compile_web(Path(args.file).read_text(encoding='utf-8'), args.file)
            if args.json:
                print(json.dumps({'schema_version': 1, 'diagnostics': []}, sort_keys=True))
        else:
            build(args.file,args.output,args.sourcemap,args.release,args.observe,args.static,args.routes,args.pwa)
        return 0
    except (WebError, OSError, UnicodeError) as error:
        from .web_diagnostics import diagnostic as render_diagnostic
        diagnostic=render_diagnostic(error,getattr(args,'file',getattr(args,'directory','<web>')))
        if args.command=='audit':diagnostic['snippet']='[redacted: security review location]'
        if getattr(args, 'json', False):
            print(json.dumps({'schema_version': 1, 'diagnostics': [diagnostic]}, sort_keys=True))
        else:
            print(f'{diagnostic["file"]}:{diagnostic["line"]}: {diagnostic["code"]}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
