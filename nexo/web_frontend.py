"""Nexo Web M1–M2: a separate, deliberately bounded browser compiler (RFC-0001).

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
ATTRS = {'class', 'id', 'title', 'role', 'type', 'name', 'placeholder', 'disabled', 'aria_label', 'aria_live', 'aria_describedby', 'aria_hidden', 'tabindex', 'for'}
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
                js, _, names = value(p, states, line)
                parts.append(f'String({js})')
                deps |= names
        return '(' + '+'.join(parts or ['""']) + ')', 'str', deps
    if isinstance(expr, A.Name) and expr.name in states:
        binding = states[expr.name]
        if hasattr(binding, 'code'):
            if binding.type == 'context':
                fail(line, 'Read a context field or call a declared context action.', 'NW2004')
            return binding.code, binding.type, {binding.dep} if binding.dep else set()
        return f's[{json.dumps(expr.name)}]', binding[0], {expr.name}
    if isinstance(expr, A.Attr) and isinstance(expr.obj, A.Name):
        binding = states.get(expr.obj.name)
        if binding and getattr(binding, 'type', None) == 'context' and expr.name in binding.context.states:
            return f'{binding.code}.values[{json.dumps(expr.name)}]', binding.context.states[expr.name].type, {f'@{expr.obj.name}.{expr.name}'}
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


def build(source_path, output):
    source = Path(source_path).read_text(encoding='utf-8')
    js, css, exports = compile_web(source, source_path)
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    files = browser_assets(js, css, exports)
    for name, content in files.items():
        (directory/name).write_text(content, encoding='utf-8')
    return files


def browser_assets(js, css, exports):
    return {'app.js': js, 'app.css': css,
             'main.js': f'import {{ {exports[0]} }} from "./app.js";\n{exports[0]}(document.querySelector("#app"));\n',
             'index.html': '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'self\'; base-uri \'none\'; form-action \'none\'"><title>'+html.escape('Nexo Web' if exports[0] == 'mountApp' else exports[0])+'</title><link rel="stylesheet" href="./app.css"></head><body><div id="app"></div><script type="module" src="./main.js"></script></body></html>\n'}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='nexo web', description='Experimental M1–M2 browser compiler; independent of WASI.')
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check')
    check.add_argument('--json', action='store_true')
    check.add_argument('file')
    compile_cmd = sub.add_parser('build')
    compile_cmd.add_argument('file')
    compile_cmd.add_argument('-o', '--output', default='dist')
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            compile_web(Path(args.file).read_text(encoding='utf-8'), args.file)
            if args.json:
                print(json.dumps({'schema_version': 1, 'diagnostics': []}, sort_keys=True))
        else:
            build(args.file, args.output)
        return 0
    except (WebError, OSError, UnicodeError) as error:
        diagnostic = {'code': getattr(error, 'code', 'NW1000'), 'file': getattr(error, 'file', None) or args.file, 'line': getattr(error, 'line', 1), 'severity': 'error', 'message': str(error)}
        if getattr(args, 'json', False):
            print(json.dumps({'schema_version': 1, 'diagnostics': [diagnostic]}, sort_keys=True))
        else:
            print(f'{diagnostic["file"]}:{diagnostic["line"]}: {diagnostic["code"]}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
