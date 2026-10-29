"""Conservative, opt-in function effects. Reports are never authority (D182)."""
from __future__ import annotations

from collections import ChainMap, deque
from dataclasses import dataclass, field, fields, is_dataclass
import json
from pathlib import Path

from . import ast as A, errors
from .lint import front_end_modules
from .lsp import span_range
from .modules import bound_name, is_file_module
from .security_context import permits
from .stdlib import policy

CATALOG = json.loads((Path(__file__).parent / 'stdlib/effects.json').read_text())
# Bound total propagated facts, not iterations: long sparse call chains stay cheap.
MAX_FACTS = 200_000
MAX_UNITS = 10_000


@dataclass(frozen=True, order=True)
class Fact:
    file: str
    line: int
    col: int
    end_col: int
    operation: str
    capability: str = ''
    target: str = ''
    reason: str = ''


@dataclass
class Unit:
    file: str
    name: str
    span: object
    body: list
    env: dict = field(default_factory=dict)
    facts: set = field(default_factory=set)
    calls: set = field(default_factory=set)
    defaults: list = field(default_factory=list)


def children(n):
    if isinstance(n, (list, tuple)):
        return n
    if is_dataclass(n) and not isinstance(n, errors.Span):
        return [getattr(n, f.name) for f in fields(n) if f.name != 'span']
    return []


def shallow(body):
    """Declarations in a unit, excluding function and type bodies."""
    todo = list(reversed(body))
    while todo:
        n = todo.pop()
        if isinstance(n, A.Stmt):
            yield n
        if isinstance(n, (A.FnDef, A.TypeDef)):
            continue
        todo.extend(reversed(children(n)))


def literal(expr):
    if isinstance(expr, A.Str) and all(isinstance(p, str) for p in expr.parts):
        return ''.join(expr.parts)
    return None


def literal_value(expr):
    """Avoid claiming purity when a builtin can force a lazy/native value."""
    if isinstance(expr, (A.Num, A.UnitLit, A.Bool, A.NoneLit)):
        return True
    if isinstance(expr, A.Str):
        return literal(expr) is not None
    if isinstance(expr, A.ListLit):
        return all(literal_value(e) for e in expr.items)
    if isinstance(expr, A.MapLit):
        return all(literal_value(k) and literal_value(v) for k, v in expr.pairs)
    return False


class Analysis:
    def __init__(self, file, program, linker):
        self.units = []
        self.modules = {}
        self.linker = linker
        self.rules = (policy.declared(program) or (0, []))[1]
        self.programs = {file: program, **{m.file: m.program for m in linker.modules.values()}}
        for name, p in self.programs.items():
            self.modules[name] = len(self.units)
            self.units.append(Unit(name, '<module>', errors.Span(name, 1, 1, 1), p.body))
        self.lines = {name: errors.SOURCES.get(name, "").split("\n") for name in self.programs}
        for index in sorted(self.modules.values(), key=lambda i: (i != self.modules[file], self.units[i].file)):
            self.prepare(index, {})

    def fact(self, unit, span, operation, capability='', target='', reason=''):
        unit.facts.add(Fact(span.file if span else unit.file, span.line if span else 1,
                            span.col if span else 1, span.end_col if span else 1,
                            operation, capability or '', target or '', reason))

    def prepare(self, index, inherited):
        unit = self.units[index]
        env = ChainMap({}, inherited)
        declarations = {}
        direct = {id(st) for st in unit.body}
        # A binding anywhere in this conservative scope shadows a builtin.
        for n in shallow(unit.body):
            if isinstance(n, A.FnDef):
                declarations.setdefault(n.name, []).append(n)
            elif isinstance(n, A.Assign) and isinstance(n.target, A.Name):
                declarations.setdefault(n.target.name, []).append(None)
            elif isinstance(n, (A.VarDecl, A.SharedDecl, A.TypeDef, A.TraitDef)):
                declarations.setdefault(n.name, []).append(None)
            elif isinstance(n, A.For):
                declarations.setdefault(n.var, []).append(None)
            elif isinstance(n, A.Use):
                if is_file_module(n):
                    key = self.linker.use_keys.get(id(n))
                    info = self.linker.modules.get(key)
                    env[bound_name(n)] = ('module', self.modules[info.file]) if info else ('unknown', None)
                elif n.lang:
                    env[bound_name(n)] = ('unknown', None)
        pending = []
        for name, decls in sorted(declarations.items()):
            env[name] = ('unknown', None)
            for fn in (d for d in decls if d is not None):
                if len(self.units) >= MAX_UNITS:
                    self.fact(unit, unit.span, 'unknown', reason='analysis_limit')
                    continue
                child = len(self.units)
                label = fn.name if unit.name == '<module>' else unit.name + '.' + fn.name
                self.units.append(Unit(unit.file, label, fn.name_span, fn.body,
                                       defaults=[p.default for p in fn.params if p.default is not None]))
                if fn.annotations:
                    self.fact(self.units[child], fn.span, 'unknown', reason='annotated_function')
                pending.append((child, fn))
                if (len(decls) == 1 and id(fn) in direct and name not in CATALOG["builtin_roots"]
                        and not name.startswith("__")):
                    env[name] = ('function', child)
        unit.env = env
        # Loop/pattern/lambda bindings are conservative even outside their scope.
        todo = list(unit.body)
        while todo:
            n = todo.pop()
            if isinstance(n, (A.FnDef, A.TypeDef)):
                continue
            if isinstance(n, (A.Lambda, A.Comprehension, A.ParallelFor)):
                unit.env[n.param if isinstance(n, A.Lambda) else n.var] = ('unknown', None)
            if isinstance(n, A.SelectArm) and n.var:
                unit.env[n.var] = ("unknown", None)
            if isinstance(n, (A.Handler, A.PName)):
                unit.env[n.var if isinstance(n, A.Handler) else n.name] = ('unknown', None)
            todo.extend(children(n))

        for child, fn in pending:
            outer = ChainMap({p.name: ("unknown", None) for p in fn.params}, env)
            self.prepare(child, outer)

    def identity(self, expr, env):
        if isinstance(expr, A.Name):
            return env.get(expr.name, ('builtin', expr.name))
        if isinstance(expr, A.Attr):
            kind, name = self.identity(expr.obj, env)
            if kind == 'module':
                return self.units[name].env.get(expr.name, ('unknown', None))
            if kind == 'builtin':
                return kind, name + '.' + expr.name
        return 'unknown', None

    def call(self, unit, n):
        kind, name = self.identity(n.func, unit.env)
        if kind == 'function':
            unit.calls.add(name)
        elif kind != 'builtin':
            self.fact(unit, n.span, 'unknown', reason='dynamic_call')
        elif name in CATALOG['pure']:
            if any(not literal_value(a.value) for a in n.args):
                self.fact(unit, n.span, 'unknown', reason='dynamic_value')
        elif name in CATALOG['calls']:
            if any(not literal_value(a.value) for a in n.args):
                self.fact(unit, n.span, 'unknown', reason='dynamic_value')
            if name == 'run':
                self.fact(unit, n.span, 'unknown', reason='native_process')
            positional = [a.value for a in n.args if a.name is None]
            for spec in CATALOG['calls'][name]:
                value = next((a.value for a in n.args if a.name == spec['keyword']), None)
                arg = spec['argument']
                if value is None and arg is not None and arg < len(positional):
                    value = positional[arg]
                target = literal(value)
                mode, cap, op = spec['mode'], spec['capability'], spec['operation']
                reason = ''
                if mode == 'any':
                    target = ''
                elif mode == 'host':
                    # Only a conservative ASCII URL subset is compared. No DNS.
                    target = host(target)
                elif mode == 'database':
                    if target == ':memory:':
                        self.fact(unit, n.span, op)
                        continue
                    if target is not None and '://' not in target:
                        self.fact(unit, n.span, op, 'read', target)
                        self.fact(unit, n.span, op, 'write', target)
                        continue
                    # Server connections can include auth/discovery and further IO.
                    cap, target = ('net' if target is not None else None), None
                if target is None or mode == 'dynamic':
                    target, reason = '', 'dynamic_target'
                self.fact(unit, n.span, op, cap, target, reason)
        else:
            self.fact(unit, n.span, 'unknown', reason='unmodeled_call')
        # Evaluating a callee expression may itself call an effectful function.
        self.scan(unit, n.func)
        for arg in n.args:
            self.scan(unit, arg.value)

    def scan(self, unit, n):
        if isinstance(n, A.Call):
            self.call(unit, n)
            return
        if isinstance(n, A.FnDef):
            for p in n.params:
                self.scan(unit, p.default)
            for a in n.annotations:
                self.scan(unit, a.args)
            return
        if isinstance(n, A.Use):
            if is_file_module(n):
                info = self.linker.modules.get(self.linker.use_keys.get(id(n)))
                if info:
                    unit.calls.add(self.modules[info.file])
            elif n.lang in ('c', 'python'):
                self.fact(unit, n.span, 'native.load', n.lang, n.name)
                self.fact(unit, n.span, 'unknown', reason='native_integration')
            return
        if isinstance(n, (A.TypeDef, A.TraitDef)):
            self.fact(unit, n.span, 'unknown', reason='type_definition')
            if isinstance(n, A.TypeDef):
                for f in n.fields:
                    self.scan(unit, f.default)
            return
        if isinstance(n, (A.Lambda, A.Pipeline, A.EventStmt, A.ConfigBlock, A.Emit,
                          A.Comprehension, A.ParallelFor, A.For, A.Select, A.Index, A.Match)):
            self.fact(unit, n.span, 'unknown', reason='deferred_or_unmodeled_construct')
        if isinstance(n, A.Attr):
            kind, name = self.identity(n, unit.env)
            if kind == 'unknown':
                self.fact(unit, n.span, 'unknown', reason='dynamic_member')
            elif kind == 'builtin' and name not in CATALOG['pure'] and name not in CATALOG['calls']:
                self.fact(unit, n.span, 'unknown', reason='unmodeled_member')
        if isinstance(n, A.Str) and literal(n) is None:
            self.fact(unit, n.span, 'unknown', reason='dynamic_value')
        for child in children(n):
            self.scan(unit, child)

    def run(self):
        for unit in self.units:
            self.scan(unit, unit.body)
            self.scan(unit, unit.defaults)
        reverse = [set() for _ in self.units]
        for i, unit in enumerate(self.units):
            for callee in unit.calls:
                reverse[callee].add(i)
        queue = deque(i for i, u in enumerate(self.units) if u.facts)
        queued = set(queue)
        total = sum(len(u.facts) for u in self.units)
        limited = total > MAX_FACTS or any(f.reason == 'analysis_limit' for u in self.units for f in u.facts)
        while queue and not limited:
            child = queue.popleft()
            queued.remove(child)
            for parent in sorted(reverse[child]):
                new = self.units[child].facts - self.units[parent].facts
                total += len(new)
                if total > MAX_FACTS:
                    limited = True
                    break
                if new:
                    self.units[parent].facts.update(new)
                    if parent not in queued:
                        queue.append(parent)
                        queued.add(parent)
        if limited:
            for unit in self.units:
                unit.facts.clear()
                self.fact(unit, unit.span, 'unknown', reason='analysis_limit')
        out = []
        for unit in sorted(self.units, key=lambda u: (u.file, u.span.line, u.name)):
            effects = []
            for f in sorted(unit.facts):
                status = 'unknown' if f.reason else 'not_applicable' if not f.capability else (
                    'allowed' if permits(self.rules, f.capability, f.target) else 'denied')
                span = errors.Span(f.file, f.line, f.col, f.end_col)
                effects.append(dict(operation=f.operation, capability=f.capability or None,
                                    target=f.target if f.capability and not f.reason else None,
                                    status=status, reason=f.reason or None, file=f.file,
                                    range=span_range(self.lines.get(f.file, ['']), span)))
            complete = all(e['status'] != 'unknown' for e in effects)
            denied = any(e['status'] == 'denied' for e in effects)
            out.append(dict(file=unit.file, range=span_range(self.lines[unit.file], unit.span),
                            severity=1 if denied else 2 if not complete else 3, source='nexo',
                            code='effects_summary', message=errors.NexoError('effects_summary').text(),
                            data=dict(fixes=[], effects_version=1, function=unit.name, complete=complete, pure=complete and not effects,
                                      policy_scope='root', effects=effects)))
        return out


def host(url):
    # Same intentionally narrow subset in Rust: unusual URL forms remain unknown.
    if url is None or not url.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == '\\' for c in url):
        return None
    scheme, sep, rest = url.partition('://')
    if not sep or scheme not in ('http', 'https'):
        return None
    authority = rest.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
    if '@' in authority or '[' in authority or authority.count(':') > 1:
        return None
    name, colon, port = authority.partition(':')
    if not name or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in name):
        return None
    if colon and (len(port) > 5 or not port.isdigit() or not 0 < int(port) <= 65535):
        return None
    return name.lower()


def analyze(source, file):
    _, program, linker = front_end_modules(source, file)
    return Analysis(file, program, linker).run()
