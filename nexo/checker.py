"""Static checks that the specification calls compile errors.

Section 3:
  1. A function that can fail returns T!; on pub functions it is written out.
  2. Ignoring a possible error is a compile error.
  3. `?` can only be used inside a function that also returns `!`.
  -  `??` is only for T? values; `?` and `try` only for T! values.
Section 2: pub functions declare their types.
Contracts: `ensure` and `old()` only make sense inside a function.

A function can fail when its declared return type ends in `!`, or when its
body contains `fail(...)` or `?` outside a `try`. Calls that can fail must
be followed by `?`, be inside a `try`, or be at the top level of the program
or of a `test` (where an unhandled error ends the program, rule 6).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import ast as A
from .errors import NexoError

FALLIBLE_BUILTINS = {"read", "write", "read_lines", "save", "validate"}
FALLIBLE_MEMBERS = {("json", "parse"), ("csv", "parse")}


@dataclass
class Ctx:
    fn: A.FnDef | A.Lambda | None  # None: top level or test
    in_try: bool = False
    in_ensure: bool = False
    in_parallel_for: bool = False


def callee_name(func: A.Expr) -> str:
    if isinstance(func, A.Name):
        return func.name
    if isinstance(func, A.Attr):
        inner = callee_name(func.obj)
        return f"{inner}.{func.name}" if inner else func.name
    return ""


def type_text(t: A.TypeExpr) -> str:
    if isinstance(t, A.LiteralUnion):
        return " | ".join(getattr(l, "text", "") or "".join(p for p in getattr(l, "parts", []) if isinstance(p, str)) for l in t.literals)
    text = t.name
    if t.params:
        text += "[" + ", ".join(type_text(p) for p in t.params) + "]"
    if t.optional:
        text += "?"
    return text


class Checker:
    def __init__(self, known_fns: dict[str, bool] | None = None, known_methods: dict[str, bool] | None = None):
        # name -> can fail
        self.fns: dict[str, bool] = dict(known_fns or {})
        self.methods: dict[str, bool] = dict(known_methods or {})

    # -- pass 1: which functions can fail -------------------------------
    def collect(self, body: list[A.Stmt]) -> None:
        for node in walk_stmts(body):
            if isinstance(node, A.FnDef):
                self.fns[node.name] = self.fns.get(node.name, False) or can_fail(node)
            elif isinstance(node, A.TypeDef):
                for m in node.methods:
                    self.methods[m.name] = self.methods.get(m.name, False) or can_fail(m)

    def call_can_fail(self, call: A.Call) -> bool | None:
        """True/False when known, None when unknown (e.g. a lambda)."""
        func = call.func
        if isinstance(func, A.Name):
            if func.name in self.fns:
                return self.fns[func.name]
            if func.name in FALLIBLE_BUILTINS:
                return True
            return None
        if isinstance(func, A.Attr):
            if isinstance(func.obj, A.Name) and (func.obj.name, func.name) in FALLIBLE_MEMBERS:
                return True
            if func.name in self.methods:
                return self.methods[func.name]
        return None

    # -- pass 2 ---------------------------------------------------------
    def check(self, program: A.Program) -> None:
        self.collect(program.body)
        self.stmts(program.body, Ctx(None))

    def stmts(self, body: list[A.Stmt], ctx: Ctx) -> None:
        for s in body:
            self.stmt(s, ctx)

    def stmt(self, s: A.Stmt, ctx: Ctx) -> None:
        if isinstance(s, A.FnDef):
            self.fn_def(s)
        elif isinstance(s, A.TypeDef):
            for m in s.methods:
                self.fn_def(m)
            for f in s.fields:
                if f.default is not None:
                    self.expr(f.default, Ctx(None))
        elif isinstance(s, A.TestDef):
            self.stmts(s.body, Ctx(None))
        elif isinstance(s, A.ExprStmt):
            self.expr(s.expr, ctx)
        elif isinstance(s, A.Assign):
            self.expr(s.target, ctx)
            self.expr(s.value, ctx)
        elif isinstance(s, A.VarDecl):
            self.expr(s.value, ctx)
        elif isinstance(s, A.Return):
            if ctx.fn is None and not ctx.in_parallel_for:
                raise NexoError("return_outside_fn", s.span)
            if s.value is not None:
                self.expr(s.value, ctx)
        elif isinstance(s, A.Expect):
            if ctx.fn is None or isinstance(ctx.fn, A.Lambda):
                raise NexoError("ensure_outside_fn", s.span, kw="expect")
            self.expr(s.expr, ctx)
        elif isinstance(s, A.Ensure):
            if ctx.fn is None or isinstance(ctx.fn, A.Lambda):
                raise NexoError("ensure_outside_fn", s.span, kw="ensure")
            self.expr(s.expr, Ctx(ctx.fn, ctx.in_try, True, ctx.in_parallel_for))
        elif isinstance(s, A.Assert):
            self.expr(s.expr, ctx)
        elif isinstance(s, A.Emit):
            self.expr(s.call, ctx)
        elif isinstance(s, A.If):
            for cond, body in s.branches:
                self.expr(cond, ctx)
                self.stmts(body, ctx)
            if s.orelse:
                self.stmts(s.orelse, ctx)
        elif isinstance(s, (A.For, A.While, A.Repeat)):
            head = s.iterable if isinstance(s, A.For) else s.cond if isinstance(s, A.While) else s.count
            self.expr(head, ctx)
            self.stmts(s.body, ctx)
        elif isinstance(s, A.Match):
            self.expr(s.subject, ctx)
            for arm in s.arms:
                if arm.guard is not None:
                    self.expr(arm.guard, ctx)
                self.stmts(arm.body, ctx)
        elif isinstance(s, A.TryStmt):
            if s.within is not None:
                self.expr(s.within, ctx)
            self.stmts(s.body, Ctx(ctx.fn, True, ctx.in_ensure, ctx.in_parallel_for))
            for h in s.handlers:
                self.stmts(h.body, ctx)
        elif isinstance(s, A.ParallelBlock):
            self.stmts(s.body, ctx)
        elif isinstance(s, A.EventStmt):
            self.stmts(s.body, Ctx(None))

    def fn_def(self, fn: A.FnDef) -> None:
        fallible = can_fail(fn)
        if fn.pub:
            for p in fn.params:
                if p.type is None and p.name != "self":
                    raise NexoError("pub_param_type", p.span, param=p.name, fn=fn.name)
            if fn.ret is None:
                if fallible:
                    raise NexoError("pub_fallible_untyped", fn.name_span, fn=fn.name)
                if any(isinstance(r, A.Return) and r.value is not None for r in walk_stmts(fn.body, into_fns=False)):
                    raise NexoError("pub_return_type", fn.name_span, fn=fn.name)
        for p in fn.params:
            if p.default is not None:
                self.expr(p.default, Ctx(None))
        self.stmts(fn.body, Ctx(fn))

    def bang_check(self, ctx: Ctx, span) -> None:
        fn = ctx.fn
        if isinstance(fn, A.FnDef) and fn.ret is not None and not getattr(fn.ret, "fallible", False) and not ctx.in_try:
            raise NexoError("fallible_needs_bang", span, fn=fn.name, type=type_text(fn.ret))

    def expr(self, e: A.Expr | None, ctx: Ctx, propagated: bool = False) -> None:
        if e is None:
            return
        if isinstance(e, A.Propagate):
            inner = e.expr
            if ctx.fn is None:
                raise NexoError("question_outside_fn", e.span)
            if not isinstance(inner, A.Call) or (isinstance(inner.func, A.Name) and inner.func.name == "fail"):
                raise NexoError("question_not_error", e.span)
            if self.call_can_fail(inner) is False:
                raise NexoError("question_cannot_fail", e.span, name=callee_name(inner.func))
            self.bang_check(ctx, e.span)
            self.expr(inner, ctx, propagated=True)
            return
        if isinstance(e, A.Call):
            name = callee_name(e.func)
            if name == "fail":
                if ctx.fn is not None:
                    self.bang_check(ctx, e.span)
            elif name == "old" and not ctx.in_ensure:
                raise NexoError("old_outside_ensure", e.span)
            elif (not propagated and ctx.fn is not None and not ctx.in_try
                  and self.call_can_fail(e)):
                raise NexoError("ignored_error", e.span, name=name)
            self.expr(e.func, ctx)
            for a in e.args:
                self.expr(a.value, ctx)
            return
        if isinstance(e, A.Coalesce):
            left = e.left
            if isinstance(left, A.Call) and self.call_can_fail(left):
                raise NexoError("coalesce_on_error", left.span, name=callee_name(left.func))
            self.expr(left, ctx)
            self.expr(e.right, ctx)
            return
        if isinstance(e, A.Lambda):
            self.expr(e.body, Ctx(e))
            return
        if isinstance(e, A.ParallelFor):
            self.expr(e.iterable, ctx)
            for a in e.args:
                self.expr(a.value, ctx)
            self.stmts(e.body, Ctx(ctx.fn, ctx.in_try, ctx.in_ensure, True))
            return
        if isinstance(e, A.Pipeline):
            self.expr(e.source, ctx)
            for step in e.steps:
                if step.kind == "call" and isinstance(step.expr, A.Call):
                    # the value on the left becomes the first argument
                    self.expr(step.expr, ctx)
                elif step.kind == "call" and isinstance(step.expr, A.Name) and ctx.fn is not None and not ctx.in_try:
                    if self.fns.get(step.expr.name) or (step.expr.name not in self.fns and step.expr.name in FALLIBLE_BUILTINS):
                        raise NexoError("ignored_error", step.span, name=step.expr.name)
                else:
                    self.expr(step.expr, ctx)
                    for c in step.columns:
                        self.expr(c, ctx)
            return
        for child in children(e):
            self.expr(child, ctx)


def children(e: A.Expr) -> list[A.Expr]:
    if isinstance(e, A.Str):
        return [p for p in e.parts if not isinstance(p, str)]
    if isinstance(e, A.ListLit):
        return list(e.items)
    if isinstance(e, A.MapLit):
        return [x for pair in e.pairs for x in pair]
    if isinstance(e, A.Comprehension):
        return [x for x in (e.iterable, e.element, e.condition) if x is not None]
    if isinstance(e, A.Attr):
        return [e.obj]
    if isinstance(e, A.Index):
        return [e.obj, e.index]
    if isinstance(e, A.Unary):
        return [e.operand]
    if isinstance(e, (A.BinOp, A.BoolOp)):
        return [e.left, e.right]
    if isinstance(e, A.Compare):
        return list(e.operands)
    if isinstance(e, A.RangeExpr):
        return [e.start, e.end]
    return []


def walk_stmts(body: list[A.Stmt], into_fns: bool = True):
    """Yield every statement, nested blocks included."""
    for s in body:
        yield s
        blocks: list[list[A.Stmt]] = []
        if isinstance(s, A.FnDef):
            if into_fns:
                blocks.append(s.body)
        elif isinstance(s, A.TypeDef):
            if into_fns:
                for m in s.methods:
                    yield m
                    blocks.append(m.body)
        elif isinstance(s, A.If):
            blocks.extend(b for _, b in s.branches)
            if s.orelse:
                blocks.append(s.orelse)
        elif isinstance(s, (A.For, A.While, A.Repeat, A.ParallelBlock, A.TestDef, A.EventStmt)):
            blocks.append(s.body)
        elif isinstance(s, A.Match):
            blocks.extend(a.body for a in s.arms)
        elif isinstance(s, A.TryStmt):
            blocks.append(s.body)
            blocks.extend(h.body for h in s.handlers)
        for b in blocks:
            yield from walk_stmts(b, into_fns)


def can_fail(fn: A.FnDef) -> bool:
    """Inferred T! (section 3, rule 1)."""
    if fn.ret is not None and getattr(fn.ret, "fallible", False):
        return True
    return _body_fails(fn.body, in_try=False)


def _body_fails(body: list[A.Stmt], in_try: bool) -> bool:
    for s in body:
        if isinstance(s, (A.FnDef, A.TypeDef, A.TestDef, A.EventStmt)):
            continue
        if isinstance(s, A.TryStmt):
            if _body_fails(s.body, True) or any(_body_fails(h.body, in_try) for h in s.handlers):
                return True
            continue
        nested: list[list[A.Stmt]] = []
        exprs: list[A.Expr] = []
        if isinstance(s, A.If):
            exprs.extend(c for c, _ in s.branches)
            nested.extend(b for _, b in s.branches)
            if s.orelse:
                nested.append(s.orelse)
        elif isinstance(s, A.For):
            exprs.append(s.iterable)
            nested.append(s.body)
        elif isinstance(s, A.While):
            exprs.append(s.cond)
            nested.append(s.body)
        elif isinstance(s, A.Repeat):
            exprs.append(s.count)
            nested.append(s.body)
        elif isinstance(s, A.Match):
            exprs.append(s.subject)
            for a in s.arms:
                if a.guard is not None:
                    exprs.append(a.guard)
                nested.append(a.body)
        elif isinstance(s, A.ParallelBlock):
            nested.append(s.body)
        elif isinstance(s, A.ExprStmt):
            exprs.append(s.expr)
        elif isinstance(s, A.Assign):
            exprs.extend([s.target, s.value])
        elif isinstance(s, A.VarDecl):
            exprs.append(s.value)
        elif isinstance(s, A.Return) and s.value is not None:
            exprs.append(s.value)
        elif isinstance(s, (A.Expect, A.Assert)):
            exprs.append(s.expr)
        if not in_try and any(_expr_fails(e) for e in exprs):
            return True
        if any(_body_fails(b, in_try) for b in nested):
            return True
    return False


def _expr_fails(e: A.Expr) -> bool:
    if isinstance(e, A.Propagate):
        return True
    if isinstance(e, A.Call) and isinstance(e.func, A.Name) and e.func.name == "fail":
        return True
    if isinstance(e, A.Lambda):
        return False
    if isinstance(e, A.ParallelFor):
        return any(_expr_fails(x) for x in [e.iterable, *[a.value for a in e.args]]) or _body_fails(e.body, False)
    if isinstance(e, A.Call):
        return _expr_fails(e.func) or any(_expr_fails(a.value) for a in e.args)
    if isinstance(e, A.Pipeline):
        parts = [e.source] + [x for s in e.steps for x in [s.expr, *s.columns] if x is not None]
        return any(_expr_fails(x) for x in parts)
    return any(_expr_fails(c) for c in children(e))
