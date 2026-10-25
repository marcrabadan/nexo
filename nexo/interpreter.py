"""Tree-walking interpreter for the Nexo core.

Errors (section 3) are values that travel as `NexoFailure` until a `try`
handles them; the static checker (checker.py) guarantees that no fallible
call is silently ignored. Diagnostics (`NexoError`) are never caught.
"""
from __future__ import annotations

import contextlib
import copy
import itertools
import sys
import threading
import time
from decimal import Decimal
from typing import Any

from . import ast as A
from . import errors
from .checker import Checker
from .modules import Linker, ModuleValue, bound_name, is_file_module
from .errors import NexoError, Span, suggest
from .parser import parse, source_text
from .types import (
    AliasType, Builtin, BuiltinType, Native, Currency, EnumType, ErrorKind, ErrorValue, Function,
    INT_MIN, Lazy, LambdaFn, LitFloat, Money, NexoFailure, NexoSet, NList, NMap, Quantity, Range, Record,
    RecordType, Symbol, TraitType, Variant, VariantValue, check_int, copy_collection, failure, hashable,
    is_collection, plain, share, unique, WeakRef, field_value,
    repr_value, show, to_decimal, type_name,
)

from .stdlib.clock import Clock

MISSING = object()

class _TaskState(threading.local):
    """What belongs to the running task (the program, a task of
    `parallel`, or a spawned task with its own thread)."""

    def __init__(self):
        # the stamp of the task (see Interpreter.check_write): bindings and
        # records created by the task carry it
        self.stamp = 0
        self.isolated = False  # a spawned task (nexo.sync)
        self.in_task = 0  # tasks of `parallel` running
        self.deadlines: list = []
        self.old_stack: list = []
        self.task_env = None
        self.task_outputs: list[str] = []


_TASK = _TaskState()


class Binding:
    __slots__ = ("value", "mutable", "type", "type_env", "birth", "shared")

    def __init__(self, value, mutable, type_=None, type_env=None):
        self.value = value
        self.mutable = mutable
        self.type = type_
        self.type_env = type_env
        self.birth = _TASK.stamp
        self.shared = False


class Env:
    """A scope. Functions and loop iterations get their own scope; `if`,
    `try`, `match` and `parallel:` blocks are transparent: new names they
    define land in the enclosing scope (only pattern names and the error
    variable of `else e` stay local)."""

    __slots__ = ("vars", "parent", "is_fn", "transparent", "builtins")

    def __init__(self, parent: "Env | None" = None, is_fn: bool = False, transparent: bool = False,
                 builtins: bool = False):
        self.vars: dict[str, Binding] = {}
        self.parent = parent
        self.is_fn = is_fn
        self.transparent = transparent
        self.builtins = builtins

    def lookup(self, name: str) -> Any:
        env = self
        while env is not None:
            b = env.vars.get(name)
            if b is not None:
                return b.value
            env = env.parent
        if "." in name:
            # a type or variant of another module: payments.Order (D124)
            alias, _, member = name.partition(".")
            module = self.lookup(alias)
            if isinstance(module, ModuleValue):
                return module.env.lookup(member)
        return MISSING

    def find(self, name: str) -> Binding | None:
        env = self
        while env is not None:
            b = env.vars.get(name)
            if b is not None:
                return b
            env = env.parent
        return None

    def names(self) -> list[str]:
        out, env = [], self
        while env is not None:
            out.extend(env.vars)
            env = env.parent
        return out

    def target(self) -> "Env":
        env = self
        while env.transparent:
            env = env.parent
        return env

    def define(self, name: str, value: Any, mutable: bool = False, type_=None, type_env=None) -> None:
        self.target().vars[name] = Binding(value, mutable, type_, type_env)


class RowEnv(Env):
    """Scope of a pipeline step: `it` is the current element, and the
    columns of a row (or the fields of a record) are bare names."""

    __slots__ = ("item",)

    def __init__(self, parent: Env, item: Any):
        super().__init__(parent)
        self.item = item
        self.vars["it"] = Binding(share(item), False)

    def lookup(self, name: str) -> Any:
        if name != "it":
            item = self.item
            if isinstance(item, dict) and name in item:
                return item[name]
            if isinstance(item, Record) and name in item.fields:
                return field_value(item.fields[name])
            if isinstance(item, VariantValue) and name in item.fields:
                return item.fields[name]
        return super().lookup(name)

    def names(self) -> list[str]:
        extra = list(self.item) if isinstance(self.item, dict) else []
        if isinstance(self.item, Record):
            extra = list(self.item.fields)
        return extra + super().names()


class ReturnSignal(Exception):
    def __init__(self, value: Any):
        self.value = value


class Grouped:
    """Result of `group by`, waiting for sum, count or avg."""

    def __init__(self, key_name: str, groups: dict):
        self.key_name = key_name
        self.groups = groups


class Interpreter:
    def __init__(self, out=None):
        from .stdlib import install
        self.out = out or sys.stdout
        self.builtins = Env(builtins=True)
        install(self)
        self.globals = Env(self.builtins)
        self.checker_fns: dict[str, bool] = {}
        self.checker_methods: dict[str, bool] = {}
        self.handlers: dict[str, list] = {}
        # `fake name returns value` of the running test (section 14)
        self.fakes: dict[str, Any] = {}
        self.tests: list[A.TestDef] = []
        self.contracts: dict[int, tuple[list, list]] = {}
        # tasks of `parallel` (sequential in phase 0, D52) and of spawn
        self.stamps = itertools.count(1)
        self.sync = False
        self.shared_names: set[str] = set()
        self.shared_uses: dict[int, bool] = {}
        # the modules of the program's files (D124): loaded, and run
        self.linker: Linker | None = None
        self.module_values: dict[str, ModuleValue] = {}
        # the commands that run part of a program run that part of its
        # modules too (nexo db, plan, ci: cli.py)
        self.module_filter = None
        _TASK.__init__()
        from .stdlib import sync
        sync.reset()

    # what belongs to the running task
    deadlines = property(lambda self: _TASK.deadlines, lambda self, v: setattr(_TASK, "deadlines", v))
    old_stack = property(lambda self: _TASK.old_stack, lambda self, v: setattr(_TASK, "old_stack", v))
    task_env = property(lambda self: _TASK.task_env, lambda self, v: setattr(_TASK, "task_env", v))
    task_outputs = property(lambda self: _TASK.task_outputs, lambda self, v: setattr(_TASK, "task_outputs", v))

    def new_stamp(self) -> int:
        return next(self.stamps)

    # ----------------------------------------------------------- running
    def load(self, source: str, file: str) -> A.Program:
        program = parse(source, file)
        checker = Checker(self.checker_fns, self.checker_methods)
        checker.check(program)
        self.checker_fns, self.checker_methods = checker.fns, checker.methods
        if self.linker is None:
            self.linker = Linker(file)
        self.linker.link(program)
        return program

    def run(self, source: str, file: str) -> None:
        program = self.load(source, file)
        self.exec_block(program.body, self.globals)

    def write(self, text: str) -> None:
        self.out.write(text)

    # -------------------------------------------------------- statements
    def exec_block(self, body: list[A.Stmt], env: Env) -> None:
        for stmt in body:
            self.exec(stmt, env)

    def exec(self, s: A.Stmt, env: Env) -> None:
        if self.deadlines:
            self.check_deadline()
        method = getattr(self, "exec_" + type(s).__name__)
        if self.shared_names and self.uses_shared(s):
            # a statement that uses a shared variable holds its lock
            from .stdlib.sync import SharedLock
            with SharedLock():
                method(s, env)
            return
        method(s, env)

    def uses_shared(self, s: A.Stmt) -> bool:
        """Does the statement use a shared variable in its own expressions
        (not in the statements of its blocks)?"""
        key = id(s)
        if key not in self.shared_uses:
            from .checker import expr_names
            exprs: list = []
            if isinstance(s, (A.ExprStmt, A.Expect, A.Assert)):
                exprs = [s.expr]
            elif isinstance(s, A.Return) and s.value is not None:
                exprs = [s.value]
            elif isinstance(s, A.Assign):
                exprs = [s.target, s.value]
            elif isinstance(s, (A.VarDecl, A.SharedDecl)):
                exprs = [s.value]
            elif isinstance(s, A.If):
                exprs = [c for c, _ in s.branches]
            elif isinstance(s, A.For):
                exprs = [s.iterable]
            elif isinstance(s, A.While):
                exprs = [s.cond]
            elif isinstance(s, A.Repeat):
                exprs = [s.count]
            elif isinstance(s, A.Match):
                exprs = [s.subject]
            elif isinstance(s, A.Spawn):
                exprs = [s.call]
            elif isinstance(s, A.Emit):
                exprs = [a.value for a in s.call.args]
            elif isinstance(s, A.Select):
                exprs = [a.channel or a.after for a in s.arms if (a.channel or a.after) is not None]
            names: list = []
            for e in exprs:
                expr_names(e, names, names)
            if isinstance(s, A.Assign) and isinstance(s.target, A.Name):
                names.append((s.target.name, None))
            self.shared_uses[key] = any(n in self.shared_names for n, _ in names)
        return self.shared_uses[key]

    def lock_for(self, e: A.Expr):
        """The lock of shared variables when the expression reads one."""
        if self.shared_names:
            from .checker import expr_names
            names: list = []
            expr_names(e, names, names)
            if any(n in self.shared_names for n, _ in names):
                from .stdlib.sync import SharedLock
                return SharedLock()
        return contextlib.nullcontext()

    def exec_ExprStmt(self, s: A.ExprStmt, env: Env) -> None:
        self.eval(s.expr, env)

    def exec_Pass(self, s, env) -> None:
        pass

    def exec_Assign(self, s: A.Assign, env: Env) -> None:
        target = s.target
        if isinstance(target, A.Name):
            name = target.name
            if s.op == "=":
                value = self.eval(s.value, env)
                if s.type is not None:
                    value = self.store_value(self.conform(value, s.type, env, name, s.value.span))
                    self.declare(env, name, value, target.span, s.type)
                else:
                    self.assign_name(env, name, value, target.span, s.value.span)
                return
            current = self.lookup(env, name, target.span)
            value = self.binop(s.op[0], current, self.eval(s.value, env), s.span.to(s.value.span), s.value.span)
            self.assign_name(env, name, value, target.span, s.value.span)
            return
        if isinstance(target, A.Index):
            # a change through a place (copy-on-write)
            obj = self.place_parts(target.obj, env)
        else:
            obj = self.eval(target.obj, env)
        if isinstance(target, A.Attr):
            value = self.eval(s.value, env)
            if s.op != "=":
                value = self.binop(s.op[0], self.get_attr(obj, target.name, target.name_span), value,
                                   s.span.to(s.value.span), s.value.span)
            self.set_attr(obj, target.name, value, target.name_span, s.value.span)
            return
        index = self.eval(target.index, env)
        value = self.into_shared(obj, self.eval(s.value, env))
        container = self.place_resolve(obj)
        if s.op != "=":
            value = self.binop(s.op[0], self.get_index(container, index, target.span), value,
                               s.span.to(s.value.span), s.value.span)
        self.set_index(container, index, value, target.span)

    def declare(self, env: Env, name: str, value: Any, span: Span, type_=None) -> None:
        target = env.target()
        if name in target.vars:
            raise NexoError("immutable", span, name=name)
        target.define(name, value, False, type_, env)

    def assign_name(self, env: Env, name: str, value: Any, span: Span, value_span: Span) -> None:
        e, crossed_fn = env, False
        while e is not None and not e.builtins:
            b = e.vars.get(name)
            if b is not None:
                if not b.mutable:
                    if crossed_fn:
                        break
                    raise NexoError("immutable", span, name=name)
                self.check_var_write(b, name, env, span)
                if b.type is not None:
                    value = self.conform(value, b.type, b.type_env, name, value_span)
                b.value = self.store_value(value)
                if b.shared:
                    from .stdlib.sync import shared_store
                    b.value = shared_store(b.value)
                return
            if e.is_fn:
                crossed_fn = True
            e = e.parent
        env.define(name, self.store_value(value))

    def exec_VarDecl(self, s: A.VarDecl, env: Env) -> None:
        value = self.eval(s.value, env)
        if s.type is not None:
            value = self.conform(value, s.type, env, s.name, s.value.span)
        value = self.store_value(value)
        target = env.target()
        if s.name in target.vars:
            raise NexoError("already_declared", s.name_span, name=s.name)
        target.define(s.name, value, True, s.type, env)

    def exec_Return(self, s: A.Return, env: Env) -> None:
        value = None if s.value is None else self.store_value(self.eval(s.value, env))
        raise ReturnSignal(value)

    def exec_FnDef(self, s: A.FnDef, env: Env) -> None:
        if s.annotations:
            a = s.annotations[0]
            raise NexoError("not_supported", a.span, what=f'"@{a.name}"')
        env.define(s.name, Function(s, env))

    def exec_TraitDef(self, s: A.TraitDef, env: Env) -> None:
        env.define(s.name, TraitType(s.name, [sig.name for sig in s.sigs]))

    def exec_TypeDef(self, s: A.TypeDef, env: Env) -> None:
        if s.annotations:
            a = s.annotations[0]
            raise NexoError("not_supported", a.span, what=f'"@{a.name}"')
        if s.settings:
            name, value = s.settings[0]
            raise NexoError("not_supported", value.span, what=f'"{name} ="')
        if s.alias is not None:
            env.define(s.name, AliasType(s.name, s.alias, env))
            return
        if s.variants:
            enum = EnumType(s.name)
            for v in s.variants:
                variant = Variant(v.name, v.params, enum)
                enum.variants[v.name] = variant
            kind = enum
        else:
            kind = RecordType(s.name, s.fields, env=env)
        for m in s.methods:
            kind.methods[m.name] = Function(m, env)
        env.define(s.name, kind)
        if s.variants:
            for name, variant in kind.variants.items():
                env.define(name, variant)
        if s.trait:
            trait = env.lookup(s.trait)
            if not isinstance(trait, TraitType):
                raise NexoError("not_a_trait", s.name_span, name=s.trait)
            for method in trait.methods:
                if method not in kind.methods:
                    raise NexoError("trait_missing", s.name_span, type=s.name, trait=trait.name, method=method)
            kind.traits.append(trait)

    def exec_If(self, s: A.If, env: Env) -> None:
        for cond, body in s.branches:
            if self.truth(self.eval(cond, env), cond.span, "if"):
                self.exec_block(body, Env(env, transparent=True))
                return
        if s.orelse is not None:
            self.exec_block(s.orelse, Env(env, transparent=True))

    def exec_For(self, s: A.For, env: Env) -> None:
        for item in self.iterate(self.eval(s.iterable, env), s.iterable.span):
            loop_env = Env(env)
            loop_env.vars[s.var] = Binding(self.store_value(item), False)
            self.exec_block(s.body, loop_env)

    def exec_While(self, s: A.While, env: Env) -> None:
        while self.truth(self.eval(s.cond, env), s.cond.span, "while"):
            self.exec_block(s.body, Env(env))

    def exec_Repeat(self, s: A.Repeat, env: Env) -> None:
        count = self.eval(s.count, env)
        if not isinstance(count, int) or isinstance(count, bool):
            raise NexoError("type_mismatch", s.count.span, name="repeat", type="int",
                            value=repr_value(count), vtype=type_name(count))
        for _ in range(count):
            self.exec_block(s.body, Env(env))

    def exec_Match(self, s: A.Match, env: Env) -> None:
        value = self.eval(s.subject, env)
        for arm in s.arms:
            for pattern in arm.patterns:
                bindings = self.match(pattern, value, env)
                if bindings is None:
                    continue
                arm_env = Env(env, transparent=True)
                for k, v in bindings.items():
                    arm_env.vars[k] = Binding(share(v), False)
                if arm.guard is not None and not self.truth(self.eval(arm.guard, arm_env), arm.guard.span, "if"):
                    continue
                self.exec_block(arm.body, arm_env)
                return
        raise NexoError("no_match", s.subject.span, value=repr_value(value))

    def match(self, p: A.Pattern, value: Any, env: Env) -> dict | None:
        if isinstance(p, A.PWild):
            return {}
        if isinstance(p, A.PLit):
            lit = self.eval(p.value, env)
            return {} if self.equals(plain(lit), value, p.span) else None
        if isinstance(p, A.PName):
            known = env.lookup(p.name)
            if isinstance(known, Variant):
                if value is known:
                    return {}
                if isinstance(value, VariantValue) and value.variant is known:
                    return {}
                return None
            if isinstance(known, (RecordType, EnumType)):
                return {} if self.is_type(value, known) else None
            return {p.name: value}
        if isinstance(p, A.PCtor):
            ctor = env.lookup(p.name)
            if isinstance(ctor, Variant):
                if not (isinstance(value, VariantValue) and value.variant is ctor):
                    return None
                fields = list(value.fields.values())
            elif isinstance(ctor, RecordType):
                if not (isinstance(value, Record) and value.type is ctor):
                    return None
                fields = [field_value(v) for v in value.fields.values()]
            elif ctor is MISSING:
                raise self.name_error(p.name, p.span, env)
            else:
                raise NexoError("not_callable", p.span, vtype=type_name(ctor))
            if len(fields) != len(p.args):
                raise NexoError("variant_fields", p.span, name=p.name, n=len(fields), m=len(p.args))
            result: dict = {}
            for sub, sub_value in zip(p.args, fields):
                b = self.match(sub, sub_value, env)
                if b is None:
                    return None
                result.update(b)
            return result
        return None

    def exec_TryStmt(self, s: A.TryStmt, env: Env) -> None:
        pushed = False
        if s.within is not None:
            limit = self.eval(s.within, env)
            if not isinstance(limit, Quantity) or limit.dimension != "time":
                raise NexoError("type_mismatch", s.within.span, name="within", type="duration",
                                value=repr_value(limit), vtype=type_name(limit))
            self.deadlines.append((time.monotonic() + float(limit.base()), limit, s.span))
            pushed = True
        try:
            try:
                self.exec_block(s.body, Env(env, transparent=True))
            finally:
                if pushed:
                    self.deadlines.pop()
        except NexoFailure as f:
            for h in s.handlers:
                if h.kind is not None:
                    kind = env.lookup(h.kind)
                    if not isinstance(kind, (ErrorKind, RecordType)):
                        raise NexoError("unknown_type", h.span, name=h.kind)
                    if kind.name != "Error" and f.error.kind != kind.name:
                        continue
                handler_env = Env(env, transparent=True)
                handler_env.vars[h.var] = Binding(f.error, False)
                self.exec_block(h.body, handler_env)
                return
            raise

    def check_deadline(self) -> None:
        now = time.monotonic()
        for deadline, limit, span in self.deadlines:
            if now > deadline:
                raise failure("Timeout", span, limit=repr_value(limit))

    def exec_ParallelBlock(self, s: A.ParallelBlock, env: Env) -> None:
        # Sequential in phase 0: same error semantics, the first failure
        # stops the remaining tasks and propagates. Each statement is a task.
        from .checker import assigned_names
        for stmt in s.body:
            task_env = Env(env, transparent=True)
            self.run_task(lambda: self.exec(stmt, task_env), assigned_names([stmt]), task_env)

    def exec_TestDef(self, s: A.TestDef, env: Env) -> None:
        self.tests.append(s)

    def exec_Expect(self, s: A.Expect, env: Env) -> None:
        if not self.truth(self.eval(s.expr, env), s.expr.span, "expect"):
            raise self.contract_broken("expect", s.expr, env)

    def exec_Ensure(self, s: A.Ensure, env: Env) -> None:
        pass  # checked when the function returns

    def exec_Assert(self, s: A.Assert, env: Env) -> None:
        text = source_text(s.expr.span)
        if s.fails:
            try:
                self.eval(s.expr, env)
            except NexoFailure:
                return
            raise NexoError("assert_not_failed", s.expr.span, expr=text)
        e = s.expr
        if isinstance(e, A.Compare) and len(e.ops) == 1:
            left = self.eval(e.operands[0], env)
            right = self.eval(e.operands[1], env)
            ok = self.compare(e.ops[0], left, right, e.span)
            detail = f" ({repr_value(left)} {e.ops[0]} {repr_value(right)})"
        else:
            value = self.eval(e, env)
            ok = self.truth(value, e.span, "assert")
            detail = ""
        if not ok:
            raise NexoError("assert_failed", e.span, expr=text, detail=detail)

    def exec_Use(self, s: A.Use, env: Env) -> None:
        if is_file_module(s):
            # the module's code runs once, at its first use; its tests only
            # run when it is the file tested
            value = self.module_values.get(s.name)
            if value is None:
                info = self.linker.modules[s.name]
                module_env = Env(self.builtins)
                value = self.module_values[s.name] = ModuleValue(s.name, module_env)
                body = self.module_filter(info.program) if self.module_filter else info.program.body
                self.exec_block([x for x in body if not isinstance(x, A.TestDef)], module_env)
            env.define(bound_name(s), value)
            return
        if s.lang is None and s.name == "nexo.sync":
            from .stdlib.sync import ChannelMaker
            self.sync = True
            self.builtins.define("channel", ChannelMaker())
            return
        if s.lang is None and s.name == "nexo.db":
            from .stdlib.db import db_exports
            for name, value in db_exports().items():
                self.builtins.define(name, value)
            return
        if s.lang in ("c", "python") or (s.lang is None and s.name in ("nexo.ai", "nexo.web", "nexo.stream", "nexo.infra", "nexo.ci")):
            return  # the statements that follow it bind the names (D90)
        name = f"{s.lang} {s.name}" if s.lang else s.name
        raise NexoError("not_supported", s.span, what=f'"use {name}"')

    # ------------------------------------------------------ nexo.sync

    def exec_Spawn(self, s: A.Spawn, env: Env) -> None:
        from .stdlib.sync import spawn
        func = self.eval(s.call.func, env)
        args = self.eval_args(s.call.args, env)
        spawn(self, func, args, s.call)

    def exec_SharedDecl(self, s: A.SharedDecl, env: Env) -> None:
        from .stdlib.sync import shared_store
        value = shared_store(self.store_value(self.eval(s.value, env)))
        target = env.target()
        if s.name in target.vars:
            raise NexoError("already_declared", s.name_span, name=s.name)
        target.define(s.name, value, True)
        target.vars[s.name].shared = True
        self.shared_names.add(s.name)

    def exec_Select(self, s: A.Select, env: Env) -> None:
        from .stdlib.sync import select
        channels = [self.eval(a.channel, env) for a in s.arms if a.channel is not None]
        limit = None
        for a in s.arms:
            if a.after is not None:
                limit = self.eval(a.after, env)
        k, value = select(self, channels, limit, s.span)
        i = 0
        for arm in s.arms:
            want = i if arm.channel is not None else len(channels)
            if arm.channel is not None:
                i += 1
            if k == want:
                arm_env = Env(env, transparent=True)
                if arm.var is not None:
                    arm_env.vars[arm.var] = Binding(self.store_value(value), False)
                self.exec_block(arm.body, arm_env)
                return

    def exec_ConfigBlock(self, s: A.ConfigBlock, env: Env) -> None:
        raise NexoError("not_supported", s.span, what=f'"{s.word}:"')

    def exec_EventStmt(self, s: A.EventStmt, env: Env) -> None:
        if s.kind != "on":
            raise NexoError("not_supported", s.span, what=f'"{s.kind}"')
        call = s.trigger
        name = getattr(call.func, "name", "")
        params = []
        for a in call.args:
            if not isinstance(a.value, A.Name):
                raise NexoError("unexpected", a.span, fix="expected", got=f'"{source_text(a.span)}"',
                                expected="a name")
            params.append(a.value.name)
        self.handlers.setdefault(name, []).append((params, s.body, env))

    def exec_Emit(self, s: A.Emit, env: Env) -> None:
        name = getattr(s.call.func, "name", "")
        args = [self.store_value(self.eval(a.value, env)) for a in s.call.args]
        for params, body, henv in self.handlers.get(name, []):
            if len(params) != len(args):
                raise NexoError("arg_too_many", s.call.span, fn=name, n=len(params), m=len(args))
            local = Env(henv, is_fn=True)
            for p, v in zip(params, args):
                local.vars[p] = Binding(share(v), False)
            try:
                self.exec_block(body, local)
            except ReturnSignal:
                pass

    # -------------------------------------------------------- expressions
    def eval(self, e: A.Expr, env: Env) -> Any:
        return getattr(self, "eval_" + type(e).__name__)(e, env)

    def eval_Num(self, e: A.Num, env: Env) -> Any:
        if e.is_float:
            return LitFloat(e.text)
        return check_int(int(e.text), e.span)

    def eval_UnitLit(self, e: A.UnitLit, env: Env) -> Any:
        from .lexer import CURRENCIES
        if e.unit in CURRENCIES:
            return Money.of(Decimal(e.text), e.unit)
        return Quantity(Decimal(e.text), e.unit)

    def eval_Str(self, e: A.Str, env: Env) -> str:
        out = []
        for part in e.parts:
            if isinstance(part, str):
                out.append(part)
                continue
            value = self.bind_value(self.eval(part, env))
            try:
                out.append(show(value))
            except NexoError as err:
                if err.span is None:
                    err.span = part.span
                raise
        return "".join(out)

    def eval_Bool(self, e: A.Bool, env: Env) -> bool:
        return e.value

    def eval_NoneLit(self, e, env) -> None:
        return None

    def eval_Name(self, e: A.Name, env: Env) -> Any:
        return self.lookup(env, e.name, e.span)

    def lookup(self, env: Env, name: str, span: Span) -> Any:
        value = env.lookup(name)
        if value is MISSING:
            raise self.name_error(name, span, env)
        if type(value) is Clock:
            return value.read()
        if self.sync:
            b = env.find(name)
            if b is not None and b.value is value:
                if b.shared:
                    from .stdlib.sync import shared_read
                    return shared_read(value)
                if _TASK.isolated and b.birth < _TASK.stamp:
                    from .stdlib.sync import isolated_read
                    isolated_read(b, name, span)
        return value

    def name_error(self, name: str, span: Span, env: Env) -> NexoError:
        if name == "connect":
            return NexoError("needs_use", span, what='"connect"', module="nexo.db")
        s = suggest(name, env.names())
        if s:
            return NexoError("name_not_found", span, fix="suggest", name=name, suggestion=s)
        return NexoError("name_not_found", span, name=name)

    def eval_ListLit(self, e: A.ListLit, env: Env) -> list:
        return NList(self.store_value(self.eval(i, env)) for i in e.items)

    def eval_MapLit(self, e: A.MapLit, env: Env) -> dict:
        out = NMap()
        for k, v in e.pairs:
            out[hashable(self.eval(k, env), k.span)] = self.store_value(self.eval(v, env))
        return out

    def eval_Comprehension(self, e: A.Comprehension, env: Env) -> list:
        out = NList()
        for item in self.iterate(self.eval(e.iterable, env), e.iterable.span):
            inner = Env(env)
            inner.vars[e.var] = Binding(self.store_value(item), False)
            if e.condition is not None and not self.truth(self.eval(e.condition, inner), e.condition.span, "if"):
                continue
            out.append(self.store_value(self.eval(e.element, inner)))
        return out

    def eval_Attr(self, e: A.Attr, env: Env) -> Any:
        return self.get_attr(self.eval(e.obj, env), e.name, e.name_span)

    def eval_Index(self, e: A.Index, env: Env) -> Any:
        return self.get_index(self.eval(e.obj, env), self.eval(e.index, env), e.span)

    def eval_Propagate(self, e: A.Propagate, env: Env) -> Any:
        # The error, if any, is already on its way to the caller.
        return self.eval(e.expr, env)

    def eval_Call(self, e: A.Call, env: Env) -> Any:
        if isinstance(e.func, A.Name) and e.func.name == "old" and self.old_stack:
            values = self.old_stack[-1]
            if id(e) in values:
                return values[id(e)]
        if isinstance(e.func, A.Attr) and e.func.name == "add" and isinstance(e.func.obj, (A.Name, A.Attr, A.Index)):
            # `place.add(x)` changes the collection in its place
            parts = self.place_parts(e.func.obj, env)
            args = [(n, self.into_shared(parts, v)) for n, v in self.eval_args(e.args, env)]
            obj = self.place_resolve(parts)
            func = self.get_attr(obj, "add", e.func.name_span)
            return self.call_at(func, args, e)
        func = self.eval(e.func, env)
        args = self.eval_args(e.args, env)
        if self.fakes:
            from .checker import callee_name
            name = callee_name(e.func)
            if name in self.fakes:
                return self.fakes[name]
        return self.call_at(func, args, e)

    # ------------------------------------------------------------ places
    # A collection changed through a place (`xs[i] = v`, `order.items.add(x)`)
    # is made unique first: if another place holds it, the place gets a copy
    # (section 7, rule 2). The parts of the place are evaluated before the
    # rest of the statement, then each step is made unique.

    def place_parts(self, e: A.Expr, env: Env) -> tuple:
        if isinstance(e, A.Name):
            b = env.find(e.name)
            if b is not None and env.lookup(e.name) is b.value:
                return ("var", b, e.name, env, e.span)
            return ("value", self.eval(e, env))
        if isinstance(e, A.Attr):
            return ("field", self.eval(e.obj, env), e.name, e.name_span)
        if isinstance(e, A.Index):
            return ("index", self.place_parts(e.obj, env), self.eval(e.index, env), e.span)
        if isinstance(e, A.Propagate):
            return self.place_parts(e.expr, env)
        return ("value", self.eval(e, env))

    def place_resolve(self, parts: tuple) -> Any:
        kind = parts[0]
        if kind == "var":
            b = parts[1]
            self.check_var_write(b, parts[2], parts[3], parts[4])
            u = unique(b.value)
            if u is not b.value:
                b.value = u
            return u
        if kind == "field":
            _, obj, name, span = parts
            if isinstance(obj, Record) and name in obj.fields and not self.is_weak_field(obj, name):
                self.check_write(obj, span)
                u = unique(obj.fields[name])
                if u is not obj.fields[name]:
                    obj.fields[name] = u
                return u
            return self.temp_copy(self.get_attr(obj, name, span))
        if kind == "index":
            _, inner, index, span = parts
            container = self.place_resolve(inner)
            if isinstance(container, Lazy):
                container = self.bind_value(container)
            if isinstance(container, dict):
                key = hashable(index, span)
                if key in container:
                    u = unique(container[key])
                    if u is not container[key]:
                        container[key] = u
                    return u
                return None
            if isinstance(container, list):
                if not isinstance(index, int) or isinstance(index, bool):
                    raise NexoError("bad_index", span, vtype=type_name(container), itype=type_name(index))
                if not 0 <= index < len(container):
                    raise failure("OutOfRange", span, index=index, size=len(container))
                u = unique(container[index])
                if u is not container[index]:
                    container[index] = u
                return u
            return self.temp_copy(self.get_index(container, index, span))
        return self.temp_copy(parts[1])

    def into_shared(self, parts: tuple, value: Any) -> Any:
        """A value stored inside a shared variable is a copy of its own."""
        while parts[0] == "index":
            parts = parts[1]
        if parts[0] == "var" and parts[1].shared:
            from .stdlib.sync import shared_store
            return shared_store(self.bind_value(value))
        return value

    def is_weak_field(self, obj: Record, name: str) -> bool:
        return type(obj.fields.get(name)) is WeakRef or any(
            f.name == name and getattr(f.type, "weak", False) for f in obj.type.fields)

    @staticmethod
    def field_store(f: A.FieldDef, value: Any) -> Any:
        """What a field stores: a weak field keeps a weak reference."""
        if getattr(f.type, "weak", False) and isinstance(value, Record):
            return WeakRef(value)
        return value

    @property
    def stamp(self) -> int:
        return _TASK.stamp

    def check_write(self, record: Record, span: Span) -> None:
        """A task may only change records created after it started: the
        others can be read by several tasks at once (section 5)."""
        if record.birth < _TASK.stamp:
            raise NexoError("record_shared", span, type=record.type.name)

    def check_var_write(self, b: Binding, name: str, env: Env, span: Span) -> None:
        """Nor variables that existed before it, except the names a
        `parallel:` task computes."""
        if b.birth >= _TASK.stamp or b.shared:
            return
        if name in self.task_outputs:
            e = env
            while e is not None and e is not self.task_env and not e.is_fn:
                e = e.parent
            if e is self.task_env:
                return
        raise NexoError("task_write_var", span, name=name)

    def run_task(self, run, outputs: list[str], env: Env | None):
        """Run one task with a new stamp."""
        saved = (_TASK.stamp, self.task_outputs, self.task_env)
        _TASK.stamp = self.new_stamp()
        _TASK.in_task += 1
        self.task_outputs, self.task_env = outputs, env
        try:
            return run()
        finally:
            _TASK.stamp, self.task_outputs, self.task_env = saved
            _TASK.in_task -= 1

    def temp_copy(self, value: Any) -> Any:
        """A value that is not in a place: a change to it is seen by nobody."""
        return copy_collection(value) if is_collection(value) else value

    def eval_args(self, args: list[A.Arg], env: Env) -> list[tuple[str | None, Any]]:
        out = []
        seen_named = False
        for a in args:
            if a.name is None and seen_named:
                raise NexoError("positional_after_named", a.span)
            seen_named = seen_named or a.name is not None
            out.append((a.name, self.eval(a.value, env)))
        return out

    def call_at(self, func: Any, args: list, node: A.Node) -> Any:
        try:
            return self.call(func, args, node)
        except NexoFailure as f:
            if f.error.span is None:
                f.error.span = node.span
            raise
        except NexoError as err:
            if err.span is None:
                err.span = node.span
            raise

    def eval_Unary(self, e: A.Unary, env: Env) -> Any:
        value = self.eval(e.operand, env)
        if e.op == "not":
            return not self.truth(value, e.operand.span, "not")
        if type(value) is LitFloat:
            return LitFloat(value.text[1:] if value.text.startswith("-") else "-" + value.text)
        if isinstance(value, bool):
            raise NexoError("bad_operand", e.span, op="-", a=type_name(value))
        if isinstance(value, int):
            return check_int(-value, e.span)
        if isinstance(value, (float, Decimal)):
            return -value
        if isinstance(value, Money):
            return Money(-value.amount, value.currency)
        if isinstance(value, Quantity):
            return Quantity(-value.value, value.unit)
        raise NexoError("bad_operand", e.span, op="-", a=type_name(value))

    def eval_BinOp(self, e: A.BinOp, env: Env) -> Any:
        left = self.eval(e.left, env)
        right = self.eval(e.right, env)
        return self.binop(e.op, left, right, e.span, e.right.span)

    def eval_BoolOp(self, e: A.BoolOp, env: Env) -> bool:
        left = self.truth(self.eval(e.left, env), e.left.span, e.op)
        if e.op == "and":
            return left and self.truth(self.eval(e.right, env), e.right.span, e.op)
        return left or self.truth(self.eval(e.right, env), e.right.span, e.op)

    def eval_Compare(self, e: A.Compare, env: Env) -> bool:
        left = self.eval(e.operands[0], env)
        for op, operand in zip(e.ops, e.operands[1:]):
            right = self.eval(operand, env)
            if not self.compare(op, left, right, e.span):
                return False
            left = right
        return True

    def eval_Coalesce(self, e: A.Coalesce, env: Env) -> Any:
        left = self.eval(e.left, env)
        return self.eval(e.right, env) if left is None else left

    def eval_RangeExpr(self, e: A.RangeExpr, env: Env) -> Range:
        start, end = self.eval(e.start, env), self.eval(e.end, env)
        for v in (start, end):
            if not isinstance(v, int) or isinstance(v, bool):
                raise NexoError("bad_operands", e.span, op="..", a=type_name(start), b=type_name(end))
        return Range(start, end)

    def eval_Lambda(self, e: A.Lambda, env: Env) -> LambdaFn:
        return LambdaFn(e, env)

    def eval_ParallelFor(self, e: A.ParallelFor, env: Env) -> list:
        # Sequential in phase 0: results keep the order of the input and the
        # first failing task stops the rest and propagates its error.
        for a in e.args:
            if a.name != "max":
                raise NexoError("arg_unknown", a.span, fn="parallel for", param=a.name or "?", params="max")
            limit = self.eval(a.value, env)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise NexoError("type_mismatch", a.value.span, name="max", type="int",
                                value=repr_value(limit), vtype=type_name(limit))
        results = NList()

        def task(item):
            task_env = Env(env)
            task_env.vars[e.var] = Binding(self.store_value(item), False)
            try:
                self.exec_block(e.body, task_env)
                return None
            except ReturnSignal as r:
                return r.value

        for item in list(self.iterate(self.eval(e.iterable, env), e.iterable.span)):
            results.append(self.run_task(lambda: task(item), [], None))
        return results

    def eval_Pipeline(self, e: A.Pipeline, env: Env) -> Any:
        value = self.eval(e.source, env)
        for step in e.steps:
            value = self.step(step, value, env)
        return value

    # ---------------------------------------------------------- pipelines
    def step(self, step: A.PipeStep, value: Any, env: Env) -> Any:
        kind = step.kind
        if kind in ("window", "sliding", "session") or type(value).__name__ in ("Stream", "StreamQuery"):
            from .stdlib.stream import stream_step
            return stream_step(self, step, value, env)
        if kind == "call":
            if isinstance(step.expr, A.Call):
                func = self.eval(step.expr.func, env)
                args = [(None, value)] + self.eval_args(step.expr.args, env)
                return self.call_at(func, args, step)
            return self.call_at(self.eval(step.expr, env), [(None, value)], step)
        if isinstance(value, Grouped):
            return self.aggregate(step, value, env)
        if type(value).__name__ == "Query":
            q = self.query_step(step, value, env)
            if q is not None:
                return q
        span = step.span
        source = value
        if kind == "where":
            def where():
                for item in self.iterate(source, span):
                    if self.truth(self.eval(step.expr, RowEnv(env, item)), step.expr.span, "where"):
                        yield item
            return Lazy(where)
        if kind == "map":
            def mapper():
                for item in self.iterate(source, span):
                    yield self.store_value(self.apply_expr(step.expr, item, env))
            return Lazy(mapper)
        if kind == "pick":
            names = []
            for c in step.columns:
                if not isinstance(c, A.Name):
                    raise NexoError("pipe_step_arg", c.span, step="pick", what=self.word_name(), example="pick name, email")
                names.append(c.name)

            def picker():
                for item in self.iterate(source, span):
                    row = {}
                    for c, n in zip(step.columns, names):
                        row[n] = self.eval(c, RowEnv(env, item))
                    yield row
            return Lazy(picker)
        if kind == "take":
            n = self.eval(step.expr, env)
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                raise NexoError("type_mismatch", step.expr.span, name="take", type="int",
                                value=repr_value(n), vtype=type_name(n))
            return Lazy(lambda: itertools.islice(self.iterate(source, span), n))
        if kind == "sort":
            def sorter():
                items = list(self.iterate(source, span))
                keys = [self.eval(step.expr, RowEnv(env, item)) for item in items]
                order = sorted(range(len(items)), key=lambda i: SortKey(self, keys[i], step.expr.span),
                               reverse=step.desc)
                return [items[i] for i in order]
            return Lazy(sorter)
        if kind == "group":
            groups: dict = {}
            for item in self.iterate(source, span):
                key = hashable(self.eval(step.expr, RowEnv(env, item)), step.expr.span)
                groups.setdefault(key, []).append(item)
            return Grouped(source_text(step.expr.span) or "key", groups)
        if kind == "each":
            for item in self.iterate(source, span):
                self.apply_expr(step.expr, item, env)
            return None
        if kind == "count":
            return sum(1 for _ in self.iterate(source, span))
        if kind == "sum":
            return self.total(list(self.iterate(source, span)), span)
        if kind == "avg":
            items = list(self.iterate(source, span))
            if not items:
                return None
            return self.binop("/", self.total(items, span), len(items), span, span)
        raise NexoError("not_supported", span, what=f'"{kind}"')

    def query_step(self, step: A.PipeStep, query, env: Env):
        """A step on a database query, in SQL when it can be (D100)."""
        from .stdlib import db
        kind = step.kind
        if kind == "where":
            plan = db.where_plan(step.expr)
            if plan is None:
                return None
            return db.pipe_where(query, plan[0], plan[1], lambda i: self.eval(plan[1][i], env))
        if kind == "sort":
            return db.pipe_sort(query, step.expr, step.desc)
        if kind == "take":
            n = self.eval(step.expr, env)
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                raise NexoError("type_mismatch", step.expr.span, name="take", type="int",
                                value=repr_value(n), vtype=type_name(n))
            return db.pipe_take(query, n)
        if kind == "count":
            from .types import At
            return query.m_count(At(self, step.span, step))
        return None

    def word_name(self) -> str:
        return "a column name"

    def apply_expr(self, expr: A.Expr, item: Any, env: Env) -> Any:
        """Evaluate a step expression for one element. If it names a
        function (`each print`, `map double`), call it with the element."""
        value = self.eval(expr, RowEnv(env, item))
        if isinstance(value, (Function, LambdaFn, Builtin)):
            return self.call_at(value, [(None, item)], expr)
        return value

    def total(self, items: list, span: Span) -> Any:
        if not items:
            return 0
        acc = plain(items[0])
        for item in items[1:]:
            acc = self.binop("+", acc, plain(item), span, span)
        return acc

    def aggregate(self, step: A.PipeStep, grouped: Grouped, env: Env) -> list:
        rows = []
        col = source_text(step.expr.span) if step.expr is not None else "count"
        for key, items in grouped.groups.items():
            if step.kind == "count":
                result = len(items)
            else:
                values = [plain(self.eval(step.expr, RowEnv(env, item))) for item in items]
                result = self.total(values, step.span)
                if step.kind == "avg":
                    result = self.binop("/", result, len(values), step.span, step.span)
            rows.append({grouped.key_name: key, col: result})
        return rows

    # ----------------------------------------------------------- calling
    def call(self, func: Any, args: list, node: A.Node) -> Any:
        if isinstance(func, Function):
            return self.call_function(func, args, node)
        if isinstance(func, Builtin):
            return self.call_builtin(func, args, node)
        if isinstance(func, LambdaFn):
            env = Env(func.env, is_fn=True)
            params = [func.node.param]
            bound = self.bind_args("lambda", params, args, node)
            env.vars[func.node.param] = Binding(self.store_value(bound[func.node.param]), False)
            return self.store_value(self.eval(func.node.body, env))
        if isinstance(func, RecordType):
            return self.construct(func, args, node)
        if isinstance(func, Variant) and func.params:
            return self.construct_variant(func, args, node)
        if type(func).__name__ == "ChannelMaker":
            from .stdlib.sync import new_channel
            bound = self.bind_args("channel", ["size"], args, node, lambda n: True)
            return new_channel(Call(self, node), func, bound.get("size"))
        if isinstance(func, BuiltinType):
            conv = self.builtins.lookup("__convert_" + func.name)
            if conv is not MISSING:
                return self.call_builtin(conv, args, node)
        if isinstance(func, Native) and hasattr(func, "call_value"):
            # a value of another language that can be called (D111)
            return func.call_value(Call(self, node), args)
        raise NexoError("not_callable", node.span, vtype=type_name(func))

    def bind_args(self, fn_name: str, params: list[str], args: list, node: A.Node,
                  has_default=lambda name: False) -> dict:
        bound: dict = {}
        positional = [v for n, v in args if n is None]
        if len(positional) > len(params):
            raise NexoError("arg_too_many", node.span, fn=fn_name, n=len(params), m=len(positional))
        for name, value in zip(params, positional):
            bound[name] = value
        for name, value in args:
            if name is None:
                continue
            if name not in params:
                s = suggest(name, params)
                if s:
                    raise NexoError("arg_unknown", node.span, fix="suggest", fn=fn_name, param=name, suggestion=s)
                raise NexoError("arg_unknown", node.span, fn=fn_name, param=name,
                                params=", ".join(params) or "-")
            if name in bound:
                raise NexoError("arg_twice", node.span, param=name)
            bound[name] = value
        for name in params:
            if name not in bound and not has_default(name):
                raise NexoError("arg_missing", node.span, param=name, fn=fn_name)
        return bound

    def call_function(self, fn: Function, args: list, node: A.Node) -> Any:
        decl = fn.decl
        env = Env(fn.env, is_fn=True)
        params = decl.params
        if fn.self_value is not None:
            if params and params[0].name == "self":
                env.vars["self"] = Binding(fn.self_value, False)
                params = params[1:]
        by_name = {p.name: p for p in params}
        bound = self.bind_args(decl.name, [p.name for p in params], args, node,
                               lambda n: by_name[n].default is not None or
                               (by_name[n].type is not None and getattr(by_name[n].type, "optional", False)))
        for p in params:
            if p.name in bound:
                value = bound[p.name]
            elif p.default is not None:
                value = self.eval(p.default, env)
            else:
                value = None
            if p.type is not None:
                value = self.conform(value, p.type, fn.env, p.name, node.span)
            env.vars[p.name] = Binding(self.store_value(value), False)
        ensures, olds = self.contracts_of(decl)
        old_values = {}
        for call_node in olds:
            if not call_node.args:
                continue
            with self.lock_for(call_node.args[0].value):
                old_values[id(call_node)] = copy.deepcopy(self.bind_value(self.eval(call_node.args[0].value, env)))
        try:
            self.exec_block(decl.body, env)
            result = None
        except ReturnSignal as r:
            result = r.value
        if ensures:
            self.old_stack.append(old_values)
            try:
                for ens in ensures:
                    # like a statement, a contract that reads a shared
                    # variable holds its lock
                    with self.lock_for(ens.expr):
                        if not self.truth(self.eval(ens.expr, env), ens.expr.span, "ensure"):
                            raise self.contract_broken("ensure", ens.expr, env)
            finally:
                self.old_stack.pop()
        if decl.ret is not None:
            result = self.conform_return(result, decl, fn.env, node)
        return result

    def contracts_of(self, decl: A.FnDef) -> tuple[list, list]:
        key = id(decl)
        if key not in self.contracts:
            ensures = [s for s in decl.body if isinstance(s, A.Ensure)]
            olds = []
            for ens in ensures:
                olds.extend(find_old_calls(ens.expr))
            self.contracts[key] = (ensures, olds)
        return self.contracts[key]

    def contract_broken(self, kw: str, expr: A.Expr, env: Env) -> NexoFailure:
        parts = []
        seen = set()
        for sub in contract_operands(expr):
            text = source_text(sub.span)
            if not text or text in seen:
                continue
            seen.add(text)
            try:
                value = self.eval(sub, env)
            except (NexoError, NexoFailure):
                continue
            if isinstance(value, (Function, LambdaFn, Builtin, RecordType, EnumType, BuiltinType)):
                continue
            parts.append(f"{text} = {repr_value(value)}")
        values = f" ({', '.join(parts)})" if parts else ""
        return failure("ContractBroken", expr.span, kw=kw, expr=source_text(expr.span), values=values)

    def conform_return(self, result: Any, decl: A.FnDef, env: Env, node: A.Node) -> Any:
        ret = decl.ret
        try:
            return self.conform(result, ret, env, decl.name, node.span)
        except NexoError as err:
            if err.key in ("type_mismatch", "none_not_allowed"):
                raise NexoError("return_mismatch", decl.name_span, fn=decl.name, type=self.type_text(ret),
                                value=repr_value(result), vtype=type_name(result))
            raise

    def call_builtin(self, fn: Builtin, args: list, node: A.Node) -> Any:
        from .stdlib import check_arguments
        positional = [v for n, v in args if n is None]
        named = {}
        for n, v in args:
            if n is not None:
                if n in named:
                    raise NexoError("arg_twice", node.span, param=n)
                named[n] = v
        named = check_arguments(fn, positional, named, node.span)
        return fn.fn(Call(self, node), *positional, **named)

    def construct(self, rtype: RecordType, args: list, node: A.Node) -> Record:
        fields = rtype.fields
        by_name = {f.name: f for f in fields}
        bound = self.bind_args(rtype.name, [f.name for f in fields], args, node,
                               lambda n: by_name[n].default is not None or getattr(by_name[n].type, "optional", False))
        values = {}
        for f in fields:
            if f.name in bound:
                value = bound[f.name]
            elif f.default is not None:
                value = self.eval(f.default, rtype.env)
            else:
                value = None
            values[f.name] = self.field_store(f, self.store_value(self.conform(value, f.type, rtype.env, f.name, node.span)))
        return Record(rtype, values, self.stamp)

    def construct_variant(self, variant: Variant, args: list, node: A.Node) -> VariantValue:
        params = variant.params
        bound = self.bind_args(variant.name, [p.name for p in params], args, node,
                               lambda n: False)
        values = {}
        for p in params:
            value = bound[p.name]
            if p.type is not None:
                value = self.conform(value, p.type, self.globals, p.name, node.span)
            values[p.name] = self.store_value(value)
        return VariantValue(variant, values)

    # ------------------------------------------------------- attributes
    def get_attr(self, obj: Any, name: str, span: Span) -> Any:
        if isinstance(obj, ModuleValue):
            return obj.env.lookup(name)
        if isinstance(obj, Record):
            if name in obj.fields:
                return field_value(obj.fields[name])
            method = obj.type.methods.get(name)
            if method is not None:
                return method.bind(obj)
            if getattr(obj.type, "table", None) is not None:
                from .stdlib.db import relation
                from .types import At
                q = relation(At(self, span), obj, name)
                if q is not None:
                    return q
            raise self.member_error(obj, name, span, list(obj.fields) + list(obj.type.methods))
        if isinstance(obj, VariantValue):
            if name in obj.fields:
                return obj.fields[name]
            method = obj.variant.enum.methods.get(name)
            if method is not None:
                return method.bind(obj)
            raise self.member_error(obj, name, span, list(obj.fields) + list(obj.variant.enum.methods))
        if isinstance(obj, Variant):
            method = obj.enum.methods.get(name)
            if method is not None:
                return method.bind(obj)
            raise self.member_error(obj, name, span, list(obj.enum.methods))
        if isinstance(obj, ErrorValue):
            if name == "message":
                return obj.message
            if name == "cause":
                return obj.cause
            if name == "line":
                return obj.line
            if name == "kind":
                return obj.kind
            raise self.member_error(obj, name, span, ["message", "cause", "line", "kind"])
        if obj is None:
            raise NexoError("none_member", span, name=name)
        from .stdlib import members
        return members(self, obj, name, span)

    def member_error(self, obj: Any, name: str, span: Span, candidates: list[str]) -> NexoError:
        s = suggest(name, candidates)
        if s:
            return NexoError("no_member", span, fix="suggest", vtype=type_name(obj), name=name, suggestion=s)
        return NexoError("no_member", span, vtype=type_name(obj), name=name)

    def set_attr(self, obj: Any, name: str, value: Any, span: Span, value_span: Span) -> None:
        if isinstance(obj, Record):
            field = next((f for f in obj.type.fields if f.name == name), None)
            if field is None:
                raise self.member_error(obj, name, span, list(obj.fields))
            self.check_write(obj, span)
            obj.fields[name] = self.field_store(field, self.store_value(self.conform(value, field.type, obj.type.env, name, value_span)))
            return
        if obj is None:
            raise NexoError("none_member", span, name=name)
        raise NexoError("readonly_member", span, name=name, vtype=type_name(obj))

    def get_index(self, obj: Any, index: Any, span: Span) -> Any:
        if isinstance(obj, Lazy):
            obj = list(obj)
        if type(obj).__name__ == "ChannelMaker" and obj.elem is None:
            return obj[index]
        if isinstance(obj, dict):
            return obj.get(hashable(index, span))
        if isinstance(obj, (list, str)):
            if not isinstance(index, int) or isinstance(index, bool):
                raise NexoError("bad_index", span, vtype=type_name(obj), itype=type_name(index))
            if not 0 <= index < len(obj):
                raise failure("OutOfRange", span, index=index, size=len(obj))
            return obj[index]
        if obj is None:
            raise NexoError("none_member", span, name="[]")
        raise NexoError("not_indexable", span, vtype=type_name(obj))

    def set_index(self, obj: Any, index: Any, value: Any, span: Span) -> None:
        value = self.store_value(value)
        if value is obj:
            value = copy_collection(value)
        if isinstance(obj, dict):
            obj[hashable(index, span)] = value
            return
        if isinstance(obj, list):
            if not isinstance(index, int) or isinstance(index, bool):
                raise NexoError("bad_index", span, vtype="list", itype=type_name(index))
            if not 0 <= index < len(obj):
                raise failure("OutOfRange", span, index=index, size=len(obj))
            obj[index] = value
            return
        raise NexoError("not_indexable", span, vtype=type_name(obj))

    # ------------------------------------------------------------ values
    def store_value(self, value: Any) -> Any:
        """A value that is stored in a place (section 7, rule 2): a
        collection gets one more owner, so changing it copies it."""
        return share(self.bind_value(value))

    def bind_value(self, value: Any) -> Any:
        """A value that is read whole: lazy pipelines are read and float
        literals become ordinary floats."""
        if isinstance(value, Lazy):
            return NList(plain(v) for v in value)
        if isinstance(value, Grouped):
            raise NexoError("pipe_group_next", None)
        return plain(value)

    def iterate(self, value: Any, span: Span):
        if isinstance(value, (list, NexoSet)):
            # the loop holds the collection: changing it copies it, and
            # the loop sees the value it started with
            if type(value) is list:
                return iter(list(value))
            share(value)
            return iter(value) if isinstance(value, list) else iter(list(value))
        if isinstance(value, (Lazy, Range)):
            return iter(value)
        if type(value).__name__ == "Channel":
            from .stdlib.sync import iterate
            return iterate(value, span)
        if isinstance(value, dict):
            return iter(list(value))
        if isinstance(value, str):
            return iter(value)
        raise NexoError("not_iterable", span, vtype=type_name(value))

    def truth(self, value: Any, span: Span, what: str) -> bool:
        if value is True or value is False:
            return value
        raise NexoError("not_bool", span, what=what, vtype=type_name(value))

    # -------------------------------------------------------- arithmetic
    def binop(self, op: str, a: Any, b: Any, span: Span, right_span: Span) -> Any:
        from .numbers import arith
        return arith(op, a, b, span, right_span)

    def compare(self, op: str, a: Any, b: Any, span: Span) -> bool:
        if op == "in":
            return self.contains(b, a, span)
        if op == "is":
            return self.is_type(a, b, span)
        if op == "==":
            return self.equals(a, b, span)
        if op == "!=":
            return not self.equals(a, b, span)
        from .numbers import order
        return order(op, a, b, span)

    def equals(self, a: Any, b: Any, span: Span) -> bool:
        from .numbers import equals
        return equals(a, b, span)

    def contains(self, container: Any, item: Any, span: Span) -> bool:
        item = plain(item)
        if isinstance(container, str):
            if not isinstance(item, str):
                raise NexoError("bad_operands", span, op="in", a=type_name(item), b="str")
            return item in container
        if isinstance(container, dict):
            return item in container
        if isinstance(container, (NexoSet, Range)):
            return item in container
        if isinstance(container, (list, Lazy)):
            return any(self.equals(item, x, span) for x in container)
        raise NexoError("not_iterable", span, vtype=type_name(container))

    def is_type(self, value: Any, t: Any, span: Span | None = None) -> bool:
        if t is None:
            return value is None
        if isinstance(t, BuiltinType):
            return builtin_is(value, t.name)
        if isinstance(t, RecordType):
            return isinstance(value, Record) and value.type is t
        if isinstance(t, EnumType):
            return (isinstance(value, VariantValue) and value.variant.enum is t) or \
                   (isinstance(value, Variant) and value.enum is t)
        if isinstance(t, Variant):
            return value is t or (isinstance(value, VariantValue) and value.variant is t)
        if isinstance(t, TraitType):
            kind = value.type if isinstance(value, Record) else value.variant.enum if isinstance(value, VariantValue) else None
            return kind is not None and t in kind.traits
        if isinstance(t, ErrorKind):
            return isinstance(value, ErrorValue) and (t.name == "Error" or value.kind == t.name)
        if isinstance(t, AliasType):
            try:
                self.conform(value, t.target, t.env, "x", span)
                return True
            except NexoError:
                return False
        raise NexoError("not_a_type_value", span, vtype=type_name(t))

    # ------------------------------------------------------------- types
    def type_text(self, t: A.TypeExpr) -> str:
        from .checker import type_text
        return type_text(t)

    def conform(self, value: Any, t: A.TypeExpr, env: Env, name: str, span: Span) -> Any:
        """Check `value` against a declared type and apply the conversions
        of section 17 (a float literal becomes decimal where a decimal is
        expected). Returns the value to store."""
        if isinstance(t, A.RangeType):
            v = plain(value)
            if isinstance(v, int) and not isinstance(v, bool) and t.low <= v <= t.high:
                return v
            if v is None:
                raise NexoError("none_not_allowed", span, name=name, type=self.type_text(t))
            raise NexoError("type_mismatch", span, name=name, type=self.type_text(t), value=repr_value(v),
                            vtype=type_name(v))
        if isinstance(t, A.LiteralUnion):
            options = [self.eval(l, env) for l in t.literals]
            if any(self.equals(plain(o), plain(value), span) for o in options if type(plain(o)) is type(plain(value))):
                return plain(value)
            raise NexoError("type_mismatch", span, fix="type_mismatch_union", name=name,
                            type=self.type_text(t), value=repr_value(plain(value)), vtype=type_name(value),
                            values=", ".join(repr_value(plain(o)) for o in options))
        if value is None:
            if t.optional:
                return None
            raise NexoError("none_not_allowed", span, name=name, type=self.type_text(t))
        ok, converted = self.fits(value, t, env, span)
        if ok:
            return converted
        shown = self.type_text(t).rstrip("?")
        alias = env.lookup(t.name)
        if isinstance(alias, AliasType) and isinstance(alias.target, A.LiteralUnion):
            options = [self.eval(l, alias.env) for l in alias.target.literals]
            raise NexoError("type_mismatch", span, fix="type_mismatch_union", name=name, type=shown,
                            value=repr_value(plain(value)), vtype=type_name(value),
                            values=", ".join(repr_value(plain(o)) for o in options))
        raise NexoError("type_mismatch", span, name=name, type=shown, value=repr_value(plain(value)),
                        vtype=type_name(value))

    def fits(self, value: Any, t: A.TypeName, env: Env, span: Span) -> tuple[bool, Any]:
        name = t.name
        if name == "any":
            return True, plain(value)
        if name == "int":
            return builtin_is(value, "int"), value
        if name == "float":
            if isinstance(value, float):
                return True, plain(value)
            if isinstance(value, int) and not isinstance(value, bool):
                return True, float(value)
            return False, value
        if name == "decimal":
            if isinstance(value, Decimal):
                return True, value
            if type(value) is LitFloat or (isinstance(value, int) and not isinstance(value, bool)):
                return True, to_decimal(value)
            return False, value
        if name in ("str", "bool", "money", "map", "set", "channel", "date", "time"):
            if not builtin_is(value, name):
                return False, value
            if name == "map" and len(t.params) == 2:
                for k, v in value.items():
                    if not self.fits_or_none(k, t.params[0], env, span) or not self.fits_or_none(v, t.params[1], env, span):
                        return False, value
            if name == "set" and t.params:
                if not all(self.fits_or_none(v, t.params[0], env, span) for v in value):
                    return False, value
            return True, value
        if name == "list":
            if isinstance(value, Lazy):
                value = [plain(v) for v in value]
            if not isinstance(value, list):
                return False, value
            if t.params:
                out = []
                for v in value:
                    if v is None and not getattr(t.params[0], "optional", False):
                        return False, value
                    if v is None:
                        out.append(None)
                        continue
                    ok, c = self.fits(v, t.params[0], env, span) if isinstance(t.params[0], A.TypeName) else (True, v)
                    if not ok:
                        return False, value
                    out.append(c)
                return True, out
            return True, value
        kind = env.lookup(name)
        if kind is MISSING:
            raise NexoError("unknown_type", t.span, name=name)
        if isinstance(kind, AliasType):
            target = kind.target
            if isinstance(target, A.TypeName):
                return self.fits(value, target, kind.env, span)
            try:
                return True, self.conform(value, target, kind.env, "x", span)
            except NexoError:
                return False, value
        if isinstance(kind, (RecordType, EnumType, TraitType, ErrorKind, Variant, BuiltinType)):
            return self.is_type(value, kind, span), value
        raise NexoError("unknown_type", t.span, name=name)

    def fits_or_none(self, value, t, env, span) -> bool:
        if value is None:
            return getattr(t, "optional", False)
        if isinstance(t, (A.LiteralUnion, A.RangeType)):
            try:
                self.conform(value, t, env, "x", span)
                return True
            except NexoError:
                return False
        return self.fits(value, t, env, span)[0]


class Call:
    """What a builtin receives: the interpreter and the call site."""

    __slots__ = ("interp", "node")

    def __init__(self, interp: Interpreter, node: A.Node):
        self.interp = interp
        self.node = node

    @property
    def span(self) -> Span:
        return self.node.span


class SortKey:
    __slots__ = ("interp", "value", "span")

    def __init__(self, interp, value, span):
        self.interp, self.value, self.span = interp, value, span

    def __lt__(self, other):
        a, b = self.value, other.value
        if a is None or b is None:
            return a is None and b is not None
        return self.interp.compare("<", a, b, self.span)


def builtin_is(value: Any, name: str) -> bool:
    if name == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "float":
        return isinstance(value, float)
    if name == "decimal":
        return isinstance(value, Decimal)
    if name == "money":
        return isinstance(value, Money)
    if name == "str":
        return isinstance(value, str)
    if name == "bool":
        return isinstance(value, bool)
    if name == "list":
        return isinstance(value, (list, Lazy))
    if name == "map":
        return isinstance(value, dict)
    if name == "set":
        return isinstance(value, NexoSet)
    if name == "channel":
        return type(value).__name__ == "Channel"
    if isinstance(value, Native):
        return value.type_name == name
    return False


def find_old_calls(e: A.Expr) -> list[A.Call]:
    from .checker import children
    found = []
    if isinstance(e, A.Call) and isinstance(e.func, A.Name) and e.func.name == "old":
        return [e]
    if isinstance(e, A.Call):
        for a in e.args:
            found.extend(find_old_calls(a.value))
        found.extend(find_old_calls(e.func))
        return found
    for c in children(e):
        found.extend(find_old_calls(c))
    return found


def contract_operands(e: A.Expr) -> list[A.Expr]:
    """The names, fields and old(...) values shown when a contract fails."""
    from .checker import children
    if isinstance(e, A.Name):
        return [e]
    if isinstance(e, A.Attr):
        inner = e.obj
        while isinstance(inner, A.Attr):
            inner = inner.obj
        if isinstance(inner, A.Name):
            return [e]
        return contract_operands(e.obj)
    if isinstance(e, A.Call):
        if isinstance(e.func, A.Name) and e.func.name == "old":
            return [e]
        out = []
        for a in e.args:
            out.extend(contract_operands(a.value))
        return out
    out = []
    for c in children(e):
        out.extend(contract_operands(c))
    return out
