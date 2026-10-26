"""`nexo test --coverage`: branch coverage of the code the tests run
(section "Goals and guarantees", D152).

Before the program runs, every branch the developer wrote gets a call to
`__cover(n)` as its first statement: the body of each function and method,
each branch of an `if` (and the branch an `if` without `else` takes when
every condition is false, as an added `else`), each arm of a `match`, each
loop body and each `else` of a `try`. Code inside `test` blocks, lambdas
and `parallel for` is not counted, nor the code the parser writes for
module blocks (functions and handlers whose names start with `__`). The
compiler does the same, so both phases count the same branches."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from . import ast as A
from . import errors


@dataclass
class Branch:
    file: str
    line: int
    kind: str  # fn, undo, if, elif, else, no_else, arm, loop, handler
    name: str = ""

    def key(self) -> tuple:
        return (self.file, self.line, self.kind, self.name)

    def text(self) -> str:
        return f"{self.file}:{self.line}: " + errors.msg("info.cover_" + self.kind, name=self.name) + "."


# the branches of the program being run with coverage (None: no coverage)
TABLE: list[Branch] | None = None
HITS: set[int] = set()
_lock = threading.Lock()


def hit(n: int) -> None:
    with _lock:
        HITS.add(n)


def _cover(span, n: int) -> A.Stmt:
    return A.ExprStmt(span, A.Call(span, A.Name(span, "__cover"), [A.Arg(span, None, A.Num(span, str(n), False))]))


def _generated_call(e) -> bool:
    return isinstance(e, A.Call) and isinstance(e.func, A.Name) and e.func.name.startswith("__")


class Instrument:
    def __init__(self, table: list[Branch]):
        self.table = table

    def add(self, file: str, line: int, kind: str, name: str = "") -> int:
        self.table.append(Branch(file, line, kind, name))
        return len(self.table) - 1

    def mark(self, body: list[A.Stmt], kind: str, name: str = "", at_span=None) -> None:
        """Put the call first in `body`, after its leading contracts. The
        branch is on the line of its first statement, or of `at_span`."""
        at = 0
        while at < len(body) and isinstance(body[at], (A.Expect, A.Ensure)):
            at += 1
        span = at_span or body[0].span
        n = self.add(span.file, span.line, kind, name)
        body.insert(at, _cover(span, n))

    def block(self, body: list[A.Stmt]) -> None:
        for s in list(body):
            self.stmt(s)

    def fn(self, f: A.FnDef, owner: str | None = None) -> None:
        self.block(f.body)
        if f.name.startswith("__undo_"):
            self.mark(f.body, "undo", f.name[len("__undo_"):], f.name_span)
        elif not f.name.startswith("__"):
            self.mark(f.body, "fn", f"{owner}.{f.name}" if owner else f.name, f.name_span)

    def stmt(self, s: A.Stmt) -> None:
        if isinstance(s, A.FnDef):
            self.fn(s)
        elif isinstance(s, A.TypeDef):
            for m in s.methods:
                self.fn(m, s.name)
        elif isinstance(s, A.If):
            for _, body in s.branches:
                self.block(body)
            if s.orelse:
                self.block(s.orelse)
            if _generated_call(s.branches[0][0]):
                return
            for i, (_, body) in enumerate(s.branches):
                self.mark(body, "if" if i == 0 else "elif")
            if s.orelse:
                self.mark(s.orelse, "else")
            else:
                n = self.add(s.span.file, s.span.line, "no_else")
                s.orelse = [_cover(s.span, n)]
        elif isinstance(s, (A.For, A.While, A.Repeat)):
            self.block(s.body)
            if len(s.body) == 1 and isinstance(s.body[0], A.ExprStmt) and _generated_call(s.body[0].expr):
                return
            self.mark(s.body, "loop")
        elif isinstance(s, A.Match):
            for arm in s.arms:
                self.block(arm.body)
                self.mark(arm.body, "arm")
        elif isinstance(s, A.TryStmt):
            self.block(s.body)
            for h in s.handlers:
                self.block(h.body)
                if not h.var.startswith("__"):
                    self.mark(h.body, "handler")
        elif isinstance(s, A.ParallelBlock):
            self.block(s.body)


def instrument(program: A.Program, table: list[Branch]) -> None:
    """Add the `__cover` calls to a parsed program; its branches go to `table`."""
    Instrument(table).block(program.body)


def report(branches: dict[tuple, Branch], covered: set[tuple]) -> str:
    """The summary and every branch that never ran, by file and line."""
    total = len(branches)
    if total == 0:
        return errors.msg("info.cover_none") + "\n"
    done = sum(1 for k in branches if k in covered)
    lines = [errors.msg("info.cover_summary", covered=done, total=total, percent=done * 100 // total)]
    missing = [b for k, b in branches.items() if k not in covered]
    missing.sort(key=lambda b: (b.file, b.line))
    lines.extend(b.text() for b in missing)
    if missing:
        lines.append(errors.msg("info.cover_fix"))
    return "\n".join(lines) + "\n"
