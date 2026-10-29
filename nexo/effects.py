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
    contexts: dict = field(default_factory=dict)


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
    def __init__(self, file, program, linker, configuration):
        self.configuration = configuration
        self.units = []
        self.modules = {}
        self.linker = linker
        self.rules = (policy.declared(program) or (0, []))[1]
        self.programs = {file: program, **{m.file: m.program for m in linker.modules.values()}}
        ordered = sorted(self.programs, key=lambda name: (name != file, name))
        truncated = len(ordered) > MAX_UNITS
        for name in ordered[:MAX_UNITS]:
            p = self.programs[name]
            self.modules[name] = len(self.units)
            self.units.append(Unit(name, '<module>', errors.Span(name, 1, 1, 1), p.body))
        if truncated:
            self.fact(self.units[0], self.units[0].span, 'unknown', reason='analysis_limit')
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
                    env[bound_name(n)] = ('module', self.modules[info.file]) if info and info.file in self.modules else ('unknown', None)
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
        for node in unit.body:
            if not isinstance(node, A.TypeDef):
                continue
            methods = {}
            for fn in node.methods:
                if len(self.units) >= MAX_UNITS:
                    self.fact(unit, unit.span, 'unknown', reason='analysis_limit')
                    break
                child = len(self.units)
                self.units.append(Unit(unit.file, node.name + '.' + fn.name, fn.name_span, fn.body,
                    defaults=[p.default for p in fn.params if p.default is not None]))
                if fn.annotations:
                    self.fact(self.units[child], fn.span, 'unknown', reason='annotated_function')
                methods[fn.name] = child
                pending.append((child, fn))
            methods = {name: idx for name, idx in methods.items() if name not in {f.name for f in node.fields}}
            if (len(declarations.get(node.name, [])) == 1 and node.fields and node.name not in inherited
                    and node.name not in CATALOG['builtin_roots'] and not node.name.startswith('__')):
                env[node.name] = ('record', methods)
        for node in unit.body:
            if (isinstance(node, A.Assign) and node.op == '=' and isinstance(node.target, A.Name)
                    and len(declarations[node.target.name]) == 1 and node.target.name not in inherited
                    and node.target.name not in CATALOG['builtin_roots'] and not node.target.name.startswith('__')
                    and isinstance(node.value, (A.Name, A.Attr, A.Str, A.Call))):
                env[node.target.name] = ('alias', (index, node.value))
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
            parameters = {p.name: ("unknown", None) for p in fn.params}
            if '.' in self.units[child].name and fn.params and fn.params[0].name == 'self':
                class_name = self.units[child].name.rsplit('.', 1)[0]
                binding = env.get(class_name)
                if binding and binding[0] == 'record':
                    parameters['self'] = ('instance', binding[1])
            outer = ChainMap(parameters, env)
            self.prepare(child, outer)

    def identity(self, expr, env, depth=0):
        if depth > 32:
            return 'unknown', None
        binding = ('unknown', None)
        if isinstance(expr, A.Name):
            binding = env.get(expr.name, ('builtin', expr.name))
        elif isinstance(expr, A.Str) and literal(expr) is not None:
            binding = ('literal', literal(expr))
        elif isinstance(expr, A.Call):
            kind, methods = self.identity(expr.func, env, depth + 1)
            if kind == 'record':
                binding = ('instance', methods)
        elif isinstance(expr, A.Attr):
            kind, name = self.identity(expr.obj, env, depth + 1)
            if kind == 'module':
                binding = self.units[name].env.get(expr.name, ('unknown', None))
            elif kind == 'builtin':
                binding = kind, name + '.' + expr.name
            elif kind == 'instance' and expr.name in name:
                binding = 'function', name[expr.name]
        if binding[0] == 'alias':
            owner, value = binding[1]
            return self.identity(value, self.units[owner].env, depth + 1)
        return binding

    def value(self, expr, env):
        kind, value = self.identity(expr, env)
        return value if kind == 'literal' else literal(expr)

    def known_value(self, expr, env):
        return literal_value(expr) or self.identity(expr, env)[0] == 'literal'

    def call(self, unit, n):
        kind, name = self.identity(n.func, unit.env)
        if kind == 'function':
            unit.calls.add(name)
        elif kind != 'builtin':
            self.fact(unit, n.span, 'unknown', reason='dynamic_call')
        elif name in CATALOG['pure']:
            if any(not self.known_value(a.value, unit.env) for a in n.args):
                self.fact(unit, n.span, 'unknown', reason='dynamic_value')
        elif name in CATALOG['calls']:
            if any(not self.known_value(a.value, unit.env) for a in n.args):
                self.fact(unit, n.span, 'unknown', reason='dynamic_value')
            if name == 'run':
                self.fact(unit, n.span, 'unknown', reason='native_process')
            positional = [a.value for a in n.args if a.name is None]
            for spec in CATALOG['calls'][name]:
                value = next((a.value for a in n.args if a.name == spec['keyword']), None)
                arg = spec['argument']
                if value is None and arg is not None and arg < len(positional):
                    value = positional[arg]
                target = self.value(value, unit.env)
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
                if info and info.file in self.modules:
                    unit.calls.add(self.modules[info.file])
                else:
                    self.fact(unit, n.span, 'unknown', reason='analysis_limit')
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
        local_contexts = [self.configuration.contexts(u.file, u.name.rsplit('.', 1)[-1]) for u in self.units]
        registration_limited = False
        if self.configuration.has_authorization:
            modules = {i for i,u in enumerate(self.units) if u.name == '<module>'}
            known_registrations = set()
            for module in modules:
                unit = self.units[module]
                file = Path(unit.file).resolve()
                for node in unit.body:
                    call = node.expr if isinstance(node, A.ExprStmt) else node.value if isinstance(node, A.Assign) else None
                    if not isinstance(call, A.Call) or not call.args:
                        continue
                    kind, name = self.identity(call.func, unit.env)
                    if kind == 'builtin' and name == '__ai_tool':
                        target_kind, target = self.identity(call.args[0].value, unit.env)
                        if target_kind == 'function' and '.' not in self.units[target].name:
                            known_registrations.add(('tool', file, self.units[target].name))
                    if (kind == 'builtin' and name == '__ai_agent' and isinstance(node, A.Assign)
                            and isinstance(node.target, A.Name) and literal(call.args[0].value) == node.target.name):
                        known_registrations.add(('agent', file, node.target.name))

            registered = {i: set(local_contexts[i]) for i in modules}
            queue = deque(sorted(modules))
            queued = set(queue)
            size = sum(len(c) for c in registered.values())
            while queue and not registration_limited:
                parent = queue.popleft()
                queued.remove(parent)
                for child in sorted(self.units[parent].calls & modules):
                    extra = registered[parent] - registered[child]
                    size += len(extra)
                    if size > MAX_FACTS:
                        registration_limited = True
                        break
                    if extra:
                        registered[child].update(extra)
                        if child not in queued:
                            queue.append(child)
                            queued.add(child)
            by_file = {Path(self.units[i].file).resolve(): contexts for i,contexts in registered.items()}
            for i,unit in enumerate(self.units):
                if not any(symbol and symbol != '#registration' for _,symbol in local_contexts[i]):
                    continue
                required = self.configuration.registrations(unit.file, unit.name.rsplit('.', 1)[-1])
                if '.' in unit.name or not required <= known_registrations:
                    self.fact(unit, unit.span, 'unknown', reason='dynamic_registration_context')
                for file,_ in list(local_contexts[i]):
                    captured = by_file.get(Path(file).resolve())
                    if captured is None:
                        self.fact(unit, unit.span, 'unknown', reason='dynamic_registration_context')
                    else:
                        local_contexts[i].update(captured)
        for i,unit in enumerate(self.units):
            unit.contexts = {f: set(local_contexts[i]) for f in unit.facts}
        reverse = [set() for _ in self.units]
        for i, unit in enumerate(self.units):
            for callee in unit.calls:
                reverse[callee].add(i)
        queue = deque(i for i, u in enumerate(self.units) if u.facts)
        queued = set(queue)
        total = sum(len(u.facts) + sum(len(c) for c in u.contexts.values()) for u in self.units)
        limited = registration_limited or total > MAX_FACTS or any(f.reason == 'analysis_limit' for u in self.units for f in u.facts)
        while queue and not limited:
            child = queue.popleft()
            queued.remove(child)
            for parent in sorted(reverse[child]):
                changed = False
                for fact in self.units[child].facts:
                    if fact not in self.units[parent].facts:
                        self.units[parent].facts.add(fact)
                        total += 1
                        changed = True
                    contexts = self.units[parent].contexts.setdefault(fact, set())
                    extra = (self.units[child].contexts[fact] | local_contexts[parent]) - contexts
                    total += len(extra)
                    contexts.update(extra)
                    changed |= bool(extra)
                    if total > MAX_FACTS:
                        limited = True
                        break
                if limited:
                    break
                if changed and parent not in queued:
                    queue.append(parent)
                    queued.add(parent)
        if limited:
            for unit in self.units:
                unit.facts.clear()
                unit.contexts.clear()
                self.fact(unit, unit.span, 'unknown', reason='analysis_limit')
        out = []
        for unit in sorted(self.units, key=lambda u: (u.file, u.span.line, u.name)):
            effects = []
            for f in sorted(unit.facts):
                checks = []
                if not f.reason and f.capability:
                    checks.append(dict(scope='root', identity=None,
                        status='allowed' if permits(self.rules, f.capability, f.target) else 'denied', approval_required=False))
                    checks.extend(self.configuration.checks(unit.contexts.get(f, set()), f.capability, f.target))
                    checks.sort(key=lambda c: (c['scope'], c['identity'] or ''))
                status = 'unknown' if f.reason else 'not_applicable' if not f.capability else (
                    'denied' if any(c['status'] == 'denied' for c in checks) else 'allowed')
                span = errors.Span(f.file, f.line, f.col, f.end_col)
                effects.append(dict(operation=f.operation, capability=f.capability or None,
                                    target=f.target if f.capability and not f.reason else None,
                                    status=status, reason=f.reason or None, checks=checks, file=f.file,
                                    range=span_range(self.lines.get(f.file, ['']), span)))
            complete = all(e['status'] != 'unknown' for e in effects)
            denied = any(e['status'] == 'denied' for e in effects)
            out.append(dict(file=unit.file, range=span_range(self.lines[unit.file], unit.span),
                            severity=1 if denied else 2 if not complete else 3, source='nexo',
                            code='effects_summary', message=errors.NexoError('effects_summary').text(),
                            data=dict(fixes=[], effects_version=2, approval_required=self.configuration.approval(unit.file, unit.name.rsplit('.', 1)[-1]), function=unit.name, complete=complete, pure=complete and not effects,
                                      policy_scope='configured' if self.configuration.active else 'root', effects=effects)))
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


def analyze(source, file, dependencies=None, authorization=None):
    from .effect_policy import Configuration
    configuration = Configuration(file, dependencies, authorization)
    _, program, linker = front_end_modules(source, file)
    return Analysis(file, program, linker, configuration).run()
