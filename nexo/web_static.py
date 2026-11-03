"""Experimental M6a build-time HTML renderer (RFC-0002).

Only analyzed, scalar, noninteractive component trees enter this renderer. No
source JS, core interpreter, network access or user function is executed.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser

from . import ast as A
from . import web_frontend as F


def text(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def escaped(value):
    if '\0' in value:
        F.fail(1, 'Static HTML cannot represent a NUL character.', 'NW6001')
    # Literal CR is normalized to LF by HTML tokenization; a reference is not.
    return html.escape(value, quote=True).replace('\r', '&#13;')


def evaluate(expr, values, line):
    if isinstance(expr, A.Num) and not expr.is_float:
        result = int(expr.text.replace('_', ''))
    elif isinstance(expr, A.Bool):
        return expr.value
    elif isinstance(expr, A.Str):
        return ''.join(p if isinstance(p, str) else text(evaluate(p, values, line)) for p in expr.parts)
    elif isinstance(expr, A.Name) and expr.name in values:
        return values[expr.name]
    elif isinstance(expr, A.ListLit):
        return [evaluate(item, values, line) for item in expr.items]
    elif isinstance(expr, A.Attr):
        obj = evaluate(expr.obj, values, line)
        return obj.values[expr.name] if isinstance(obj, ServerContext) else obj[expr.name]
    elif isinstance(expr, A.Call):
        from .web_model import reference
        name = reference(expr.func, line)
        if '$type:' + name not in values:
            F.fail(line, 'Only typed record constructors are supported.', 'NW6001')
        return {arg.name: evaluate(arg.value, values, line) for arg in expr.args}
    elif isinstance(expr, A.Unary):
        # The minimum signed integer is represented by unary minus in the AST.
        if expr.op == '-' and isinstance(expr.operand, A.Num):
            result = -int(expr.operand.text.replace('_', ''))
        else:
            operand = evaluate(expr.operand, values, line)
            if expr.op == 'not':
                return not operand
            result = -operand
    elif isinstance(expr, A.BinOp) and expr.op in ('+', '-', '*'):
        left = evaluate(expr.left, values, line)
        right = evaluate(expr.right, values, line)
        result = left + right if expr.op == '+' else left - right if expr.op == '-' else left * right
    elif isinstance(expr, A.BoolOp):
        left = evaluate(expr.left, values, line)
        return (left and evaluate(expr.right, values, line)) if expr.op == 'and' else (left or evaluate(expr.right, values, line))
    elif isinstance(expr, A.Compare) and len(expr.ops) == 1:
        left, right = [evaluate(p, values, line) for p in expr.operands]
        return {'==': left == right, '!=': left != right, '<': left < right,
                '>': left > right, '<=': left <= right, '>=': left >= right}[expr.ops[0]]
    else:
        F.fail(line, 'Expression is outside the static scalar subset.', 'NW6001')
    if type(result) is int and not -(2**63) <= result < 2**63:
        F.fail(line, 'Static integer computation exceeds the Nexo int range.', 'NW6002')
    return result


def validate(program, routed=False, allow_error=False, interactive=False):
    from .web_codegen import members, at
    # Check every loaded declaration and every branch, even when not rendered.
    mode = 'SSR' if allow_error else 'Static builds'
    kinds = ('component', 'page', 'layout', 'context') if routed else ('component', 'context')
    for unit in program.units:
        for d in unit.declarations.values():
            if (d.kind not in kinds or (d.actions and not interactive and d.kind != 'context') or d.requests
                    or (routed and d.metadata.get('error') and not allow_error)):
                at(d, f'{mode} require supported declarations without client actions or resources.', 'NW6001')
            if interactive:
                from .web_codegen import context_action
                for body in d.actions.values():
                    for stmt, line in body:
                        if isinstance(stmt, A.Assign):
                            continue
                        if isinstance(stmt, A.ExprStmt) and (context_action(stmt.expr, d.symbols, line) or (isinstance(stmt.expr, A.Call) and isinstance(stmt.expr.func, A.Name) and stmt.expr.func.name == 'navigate')):
                            continue
                        at(d, 'Unsupported hydration action.', 'NW6005', line)
            for node in members(d.view):
                if (node.tag == 'a' and not routed) or (not interactive and any(k.startswith('on_') for k, _ in node.attrs)):
                    at(d, f'{mode}: unsupported loop, link or event handler for this rendering mode.', 'NW6001', node.line)


class ServerContext:
    def __init__(self, values):
        self.values = values


def render(program, exports, *, entry=None, props=None, routed=False, link=None, allow_error=False, interactive=False, state=None):
    validate(program, routed, allow_error, interactive)

    from .web_state import checked, key_token, patch
    entry = entry or program.root.declarations[exports[0]]
    overrides, _ = patch({name: p.type for name, p in entry.states.items()}, state or {})

    def initial(d, supplied):
        types = {key: value for key, value in d.symbols.items() if key.startswith('$type:')}
        props = {name: supplied[name] if name in supplied else evaluate(p.default, {}, p.line)
                 for name, p in d.props.items()}
        values = {**types, **props}
        for name, p in d.states.items():
            value = overrides[name] if d is entry and name in overrides else evaluate(p.default, {**types, **props}, p.line)
            values[name] = checked(p.type, value)
        return values

    def component(d, supplied, slot=None, parent_env=None):
        try:
            env = dict(parent_env or {})
            for ctx in d.providers:
                env[ctx.symbol] = ServerContext(initial(ctx, {}))
            values = initial(d, supplied)
            for alias, ctx in d.injects.items():
                values[alias] = env[ctx.symbol]
            return '<div data-nexo="' + d.scope + '">' + nodes(d, d.view, values, slot, env) + '</div>'
        except F.WebError as error:
            if not getattr(error, 'file', None):
                error.file = str(d.unit.path or '<web>')
            raise

    def nodes(d, items, values, slot, env):
        output = []
        for node in items:
            if node.tag == '#if':
                choice = -1
                body = []
                for index, (condition, candidate) in enumerate(node.branches):
                    if condition is None or evaluate(condition, values, node.line):
                        choice, body = index, candidate
                        break
                if interactive:
                    output.append(f'<!--nexo-if:{node.line}:{choice}-->')
                output.append(nodes(d, body, values, slot, env))
                if interactive:
                    output.append(f'<!--/nexo-if:{node.line}-->')
            elif node.tag == '#for':
                rows = evaluate(node.text, values, node.line)
                tokens = []
                for index, row in enumerate(rows):
                    local = {**values, node.variable: row}
                    token = key_token(evaluate(node.key, local, node.line)) if node.key is not None else 'u' + str(index)
                    tokens.append(token)
                if len(set(tokens)) != len(tokens):
                    F.fail(node.line, 'Duplicate list keys.', 'NW6005')
                if interactive:
                    output.append(f'<!--nexo-list:{node.line}-->')
                for row, token in zip(rows, tokens):
                    if interactive:
                        output.append(f'<!--nexo-row:{node.line}:{token}-->')
                    output.append(nodes(d, node.children, {**values, node.variable: row}, slot, env))
                    if interactive:
                        output.append(f'<!--/nexo-row:{node.line}:{token}-->')
                if interactive:
                    output.append(f'<!--/nexo-list:{node.line}-->')
            elif node.tag == 'slot':
                output.append('<div>' + (slot(env) if slot else '') + '</div>')
            elif id(node) in d.children:
                supplied = {k: evaluate(v, values, node.line) for k, v in node.attrs}
                output.append(component(d.children[id(node)], supplied,
                                        lambda ignored: '<div data-nexo="' + d.scope + '">' +
                                        nodes(d, node.children, values, slot, env) + '</div>', env))
            else:
                attrs = dict(node.attrs)
                rendered = []
                if node.tag == 'button' and 'type' not in attrs:
                    rendered.append(' type="button"')
                for key, expr in node.attrs:
                    if key.startswith('on_'):
                        continue
                    value = evaluate(expr, values, node.line)
                    if key == 'href':
                        value = link(value, node.line)
                    if key in ('disabled', 'checked'):
                        if value:
                            rendered.append(' ' + key)
                    else:
                        rendered.append(' ' + key.replace('_', '-') + '="' + escaped(text(value)) + '"')
                output.append('<' + node.tag + ''.join(rendered) + '>')
                if node.tag not in F.VOID:
                    if node.text is not None:
                        value = text(evaluate(node.text, values, node.line))
                        # HTML discards the first LF immediately following <pre>.
                        if node.tag == 'pre' and value.startswith('\n'):
                            output.append('\n')
                        output.append(escaped(value))
                    output.append(nodes(d, node.children, values, slot, env))
                    output.append('</' + node.tag + '>')
        return ''.join(output)

    def page(env):
        return component(entry, props or {}, parent_env=env)

    render_page = page
    parent = entry.layout
    while parent:
        def wrapped(env, layout=parent, child=render_page):
            return component(layout, {}, child, env)
        render_page = wrapped
        parent = parent.layout
    markup = render_page({})
    # Reject trees HTML parsing would repair differently from direct DOM creation.
    # Inspect the expanded tree so component wrappers and projected slots count.
    class Nesting(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []

        def handle_starttag(self, tag, attrs):
            blocks = {'div', 'main', 'section', 'article', 'header', 'footer', 'nav',
                      'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li',
                      'form', 'fieldset', 'pre', 'hr'}
            if (('p' in self.stack and tag in blocks)
                    or (tag in ('form', 'button', 'label', 'li') and tag in self.stack
                        and not (tag == 'li' and self.stack[-1] in ('ul', 'ol')))
                    or (tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') and any(t in self.stack for t in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')))):
                F.fail(1, 'Static HTML nesting would be repaired by the browser; restructure the component tree.', 'NW6001')
            if tag not in F.VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()

    Nesting().feed(markup)
    return markup


def assets(files, program, exports):
    markup = render(program, exports)
    document = files['index.html'].replace('<div id="app"></div>', '<div id="app">' + markup + '</div>')
    document = document.replace('<script type="module" src="./main.js"></script>', '')
    document = document.replace("script-src 'self'", "script-src 'none'").replace("connect-src 'self'", "connect-src 'none'")
    return {'index.html': document, 'app.css': files['app.css']}
