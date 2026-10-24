"""`nexo lint` and `nexo fix` (section 14, D86).

`nexo lint` first runs the checks of a normal run that happen before the
program starts (lexer, parser, checker); a file with an error is reported
like `nexo file.nexo` would. Otherwise it reports questionable code: each
warning says what is wrong and how to fix it, and many carry an edit. An
edit marked safe does not change what a correct program does, and
`nexo fix` applies those (also the safe fixes of compile errors).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ast as A
from . import errors
from .checker import Checker
from .errors import NexoError, Span
from .lexer import Token, tokenize
from .parser import Parser

SYNC_WORDS = {"spawn", "channel", "select", "shared"}
LAZY_STEPS = {"where", "map", "pick", "group", "sort", "take"}


@dataclass
class Edit:
    """Replace the text from (line, col) to (end_line, end_col) (1-based
    columns, end exclusive) with `text`."""
    line: int
    col: int
    end_line: int
    end_col: int
    text: str


@dataclass
class Fix:
    edits: list[Edit]
    safe: bool


@dataclass
class Finding:
    key: str  # catalog key without "lint."
    span: Span
    args: dict = field(default_factory=dict)
    fix: Fix | None = None

    def text(self) -> str:
        return f"{errors.msg('lint.' + self.key, **self.args)} {errors.msg('fix.lint.' + self.key, **self.args)}"


# ------------------------------------------------------------- running

def front_end(source: str, file: str) -> tuple[list[Token], A.Program]:
    """The checks of a run that happen before the program starts."""
    tokens = tokenize(source, file)
    program = Parser(list(tokens), file).program()
    Checker().check(program)
    return tokens, program


def lint_source(source: str, file: str) -> list[Finding]:
    """The warnings of a file; raises NexoError if it does not compile."""
    tokens, program = front_end(source, file)
    return Linter(source, tokens, program).run()


def render_finding(f: Finding) -> str:
    return errors.render(f.span, f.text(), header="header_warning")


# ------------------------------------------------------------ the rules

class Scope:
    def __init__(self, node, parent: "Scope | None", is_fn: bool):
        self.node = node  # what the names are searched in
        self.parent = parent
        self.is_fn = is_fn
        self.names: dict[str, dict] = {}  # name -> {"var": bool, "report": bool}

    def target(self) -> "Scope":
        """The scope where a new name is stored."""
        return self


class Linter:
    def __init__(self, source: str, tokens: list[Token], program: A.Program):
        self.source = source
        self.lines = source.split("\n")
        self.tokens = tokens
        self.program = program
        self.findings: list[Finding] = []
        self.type_names: set[str] = set()
        for s in walk(program.body):
            if isinstance(s, A.TypeDef):
                self.type_names.add(s.name)
                self.type_names.update(v.name for v in s.variants)
        self.declared = declared_names(program.body)  # every name the program defines
        self.reads_cache: dict[int, set[str]] = {}
        self.writes_cache: dict[int, set[str]] = {}

    def run(self) -> list[Finding]:
        top = Scope(self.program.body, None, True)
        self.block(self.program.body, top)
        self.use_lines()
        self.findings.sort(key=lambda f: (f.span.line, f.span.col, f.key))
        return self.findings

    def add(self, rule: str, span: Span, fix: Fix | None = None, **args) -> None:
        self.findings.append(Finding(rule, span, args, fix))

    # -- names read and written below a node -----------------------------
    def reads(self, node) -> set[str]:
        key = id(node)
        if key not in self.reads_cache:
            found: set[str] = set()
            targets = {id(n.target) for n in nodes(node) if isinstance(n, A.Assign) and isinstance(n.target, A.Name)}
            for n in nodes(node):
                if isinstance(n, A.Name) and id(n) not in targets:
                    found.add(n.name)
                elif isinstance(n, A.Assign) and isinstance(n.target, A.Name) and n.op != "=":
                    found.add(n.target.name)
            self.reads_cache[key] = found
        return self.reads_cache[key]

    def writes(self, node) -> set[str]:
        """Names assigned or changed through a place (`x = v`, `x += v`,
        `x[i] = v`, `x.f = v`, `x.add(v)`, any method call on x) below node."""
        key = id(node)
        if key not in self.writes_cache:
            found: set[str] = set()
            for n in nodes(node):
                if isinstance(n, A.Assign):
                    root = root_name(n.target)
                elif isinstance(n, A.Call) and isinstance(n.func, A.Attr):
                    root = root_name(n.func.obj)
                else:
                    continue
                if root is not None:
                    found.add(root)
            self.writes_cache[key] = found
        return self.writes_cache[key]

    # -- statements -------------------------------------------------------
    def block(self, body: list[A.Stmt], scope: Scope) -> None:
        ended = False
        for i, s in enumerate(body):
            if ended:
                self.unreachable(body, i)
                break
            self.stmt(s, scope)
            ended = isinstance(s, A.Return) or is_fail(s)

    def declare(self, scope: Scope, name: str, report: bool = False, var: bool = False) -> None:
        scope.names[name] = {"var": var, "report": report}

    def find(self, scope: Scope, name: str):
        """(scope, info, crossed a function) for a visible name, or None."""
        s, crossed = scope, False
        while s is not None:
            if name in s.names:
                return s, s.names[name], crossed
            if s.is_fn:
                crossed = True
            s = s.parent
        return None

    def new_scope(self, node, parent: Scope, is_fn: bool) -> Scope:
        return Scope(node, parent, is_fn)

    def fn(self, f: A.FnDef, scope: Scope) -> None:
        for p in f.params:
            if p.default is not None:
                self.expr(p.default)
        inner = self.new_scope(f, scope, True)
        for p in f.params:
            self.declare(inner, p.name)
        self.block(f.body, inner)

    def stmt(self, s: A.Stmt, scope: Scope) -> None:
        if isinstance(s, A.FnDef):
            self.declare(scope, s.name)
            self.fn(s, scope)
        elif isinstance(s, A.TypeDef):
            self.declare(scope, s.name)
            for f in s.fields:
                if f.default is not None:
                    self.expr(f.default)
            for m in s.methods:
                self.fn(m, scope)
        elif isinstance(s, A.TestDef):
            self.block(s.body, self.new_scope(s, scope, True))
        elif isinstance(s, A.EventStmt):
            if s.trigger is not None:
                self.expr(s.trigger)
            self.block(s.body, self.new_scope(s, scope, True))
        elif isinstance(s, A.ExprStmt):
            self.expr(s.expr)
            self.useless(s)
        elif isinstance(s, A.VarDecl):
            self.expr(s.value)
            self.var_decl(s, scope)
        elif isinstance(s, A.SharedDecl):
            self.expr(s.value)
            self.declare(scope, s.name, var=True)
        elif isinstance(s, A.Assign):
            self.assign(s, scope)
        elif isinstance(s, (A.Return,)):
            if s.value is not None:
                self.expr(s.value)
        elif isinstance(s, (A.Expect, A.Ensure, A.Assert)):
            self.expr(s.expr)
        elif isinstance(s, (A.Emit, A.Spawn)):
            self.expr(s.call)
        elif isinstance(s, A.If):
            for n, (cond, body) in enumerate(s.branches):
                self.expr(cond)
                if isinstance(cond, A.Bool):
                    self.add("constant_condition", cond.span, word="elif" if n else "if",
                             value="true" if cond.value else "false")
                self.block(body, scope)
            if s.orelse:
                self.block(s.orelse, scope)
        elif isinstance(s, A.For):
            self.expr(s.iterable)
            inner = self.new_scope(s, scope, False)
            self.declare(inner, s.var)
            self.block(s.body, inner)
            self.loop_var(s)
        elif isinstance(s, A.While):
            self.expr(s.cond)
            if isinstance(s.cond, A.Bool) and not s.cond.value:
                self.add("constant_condition", s.cond.span, word="while", value="false")
            self.block(s.body, self.new_scope(s, scope, False))
        elif isinstance(s, A.Repeat):
            self.expr(s.count)
            self.block(s.body, self.new_scope(s, scope, False))
        elif isinstance(s, A.Match):
            self.expr(s.subject)
            for arm in s.arms:
                inner = self.new_scope(arm, scope, False)
                for p in arm.patterns:
                    for name, span in self.bindings(p):
                        self.declare(inner, name)
                        if name not in self.reads(arm):
                            self.add("unused_binding", span, Fix([edit_span(span, "_")], True), name=name)
                if arm.guard is not None:
                    self.expr(arm.guard)
                # pattern names are local to the arm; the rest is the
                # enclosing scope (D25)
                self.block(arm.body, Transparent(inner, scope))
        elif isinstance(s, A.TryStmt):
            if s.within is not None:
                self.expr(s.within)
            self.block(s.body, scope)
            for h in s.handlers:
                inner = self.new_scope(h, scope, False)
                self.declare(inner, h.var)
                self.block(h.body, Transparent(inner, scope))
        elif isinstance(s, A.ParallelBlock):
            self.block(s.body, scope)
        elif isinstance(s, A.Select):
            for arm in s.arms:
                for e in (arm.channel, arm.after):
                    if e is not None:
                        self.expr(e)
                inner = self.new_scope(arm, scope, False)
                if arm.var:
                    self.declare(inner, arm.var)
                self.block(arm.body, Transparent(inner, scope))

    def var_decl(self, s: A.VarDecl, scope: Scope) -> None:
        target = scope.target()
        outer = self.find(scope, s.name)
        self.declare(target, s.name, var=True)
        if s.name not in self.reads(target.node):
            self.add("unused", s.name_span, name=s.name)
            return
        if outer is None and s.name not in self.writes(target.node):
            remove = Edit(s.span.line, s.span.col, s.name_span.line, s.name_span.col, "")
            self.add("var_never_changed", Span(s.span.file, s.span.line, s.span.col, s.span.col + 3),
                     Fix([remove], True), name=s.name)

    def assign(self, s: A.Assign, scope: Scope) -> None:
        self.expr(s.value)
        target = s.target
        if not isinstance(target, A.Name):
            self.expr(target)
            return
        name = target.name
        if s.op != "=":
            return
        found = self.find(scope, name)
        # `x = x` changes nothing only when it assigns the same binding: an
        # immutable name of an enclosing function gets a new local instead (D26)
        same = found is not None and (found[1]["var"] or not found[2])
        if isinstance(s.value, A.Name) and s.value.name == name and s.type is None and same:
            edit = self.remove_statement(s)
            self.add("self_assign", target.span, Fix([edit], True) if edit else None, name=name)
            return
        declares = s.type is not None or found is None or (not found[1]["var"] and found[2])
        if not declares:
            return
        home = scope.target()
        self.declare(home, name)
        # names the parser makes for module code (D90) are not the author's
        if name not in self.reads(home.node) and not name.startswith("__"):
            self.add("unused", target.span, name=name)

    # -- rules ------------------------------------------------------------
    def loop_var(self, s: A.For) -> None:
        if s.var in self.reads(s):
            return
        it = s.iterable
        count = None
        if isinstance(it, A.RangeExpr) and isinstance(it.start, A.Num) and it.start.text == "1":
            count = it.end
        elif (isinstance(it, A.Call) and isinstance(it.func, A.Name) and it.func.name == "range"
              and len(it.args) == 1 and it.args[0].name is None and "range" not in self.declared):
            count = it.args[0].value
        if count is None:
            return
        fix = None
        if it.span.line == s.span.line and count.span.line == s.span.line and it.span.end_col < 10**8:
            text = self.text(count.span)
            fix = Fix([Edit(s.span.line, s.span.col, s.span.line, it.span.end_col, f"repeat {text}")], True)
        shown = self.text(count.span) if one_line(count.span) else "n"
        self.add("loop_var", s.span, fix, name=s.var, count=shown)

    def useless(self, s: A.ExprStmt) -> None:
        e = s.expr
        if isinstance(e, A.Pipeline):
            last = e.steps[-1]
            if last.kind in LAZY_STEPS:
                self.add("lazy_pipeline", last.span, step=last.kind)
            return
        if pure(e):
            self.add("useless_expr", e.span)

    def unreachable(self, body: list[A.Stmt], i: int) -> None:
        first = body[i]
        end = self.block_end(first.span)
        edit = Edit(first.span.line, 1, end + 1, 1, "")
        self.add("unreachable", first.span, Fix([edit], True))

    def use_lines(self) -> None:
        uses = [s for s in self.program.body if isinstance(s, A.Use) and s.name == "nexo.sync"]
        if not uses:
            return
        words = set()
        for n in nodes(self.program.body):
            if isinstance(n, (A.Spawn, A.Select, A.SharedDecl)):
                words.add("statement")
            elif isinstance(n, A.Name) and n.name in SYNC_WORDS:
                words.add(n.name)
        if words:
            return
        for u in uses:
            edit = self.remove_statement(u)
            self.add("unused_use", u.span, Fix([edit], True) if edit else None, name=u.name)

    def expr(self, e: A.Expr | None) -> None:
        if e is None:
            return
        for n in nodes(e):
            if isinstance(n, A.Compare) and len(n.ops) == 1 and n.ops[0] in ("==", "!="):
                self.compare_bool(n)
            elif isinstance(n, A.Unary) and n.op == "not" and isinstance(n.operand, A.Unary) and n.operand.op == "not":
                inner = n.operand.operand
                fix = None
                if one_line(n.span) and one_line(inner.span):
                    fix = Fix([edit_span(n.span, self.text(inner.span))], True)
                self.add("double_not", n.span, fix)
            elif isinstance(n, A.MapLit):
                seen: set = set()
                for key, _ in n.pairs:
                    k = literal_key(key)
                    if k is None:
                        continue
                    if k in seen:
                        self.add("duplicate_key", key.span, text=self.text(key.span))
                    seen.add(k)

    def compare_bool(self, n: A.Compare) -> None:
        left, right = n.operands
        if isinstance(right, A.Bool):
            other, value = left, right.value
        elif isinstance(left, A.Bool):
            other, value = right, left.value
        else:
            return
        if isinstance(other, A.Bool):
            return
        positive = value == (n.ops[0] == "==")
        fix = None
        if one_line(n.span) and one_line(other.span):
            text = self.text(other.span)
            if not positive:
                text = "not " + text
                whole = self.text(n.span)
                if whole.startswith("(") and whole.endswith(")"):
                    text = f"({text})"
            fix = Fix([edit_span(n.span, text)], False)
        shown = self.text(other.span) if one_line(other.span) else "x"
        self.add("compare_bool", n.span, fix, suggestion=shown if positive else f"not {shown}")

    def bindings(self, p: A.Pattern):
        if isinstance(p, A.PName) and p.name not in self.type_names:
            yield p.name, p.span
        elif isinstance(p, A.PCtor):
            for sub in p.args:
                yield from self.bindings(sub)

    # -- source helpers ---------------------------------------------------
    def text(self, span: Span) -> str:
        return self.lines[span.line - 1][span.col - 1:span.end_col - 1]

    def block_end(self, span: Span) -> int:
        """The last line of the block that contains the statement at span."""
        depth = 0
        last = span.line
        started = False
        for tok in self.tokens:
            if not started:
                if (tok.span.line, tok.span.col) < (span.line, span.col):
                    continue
                started = True
            if tok.kind == "INDENT":
                depth += 1
            elif tok.kind == "DEDENT":
                depth -= 1
                if depth < 0:
                    break
            elif tok.kind == "EOF":
                break
            elif tok.kind != "NEWLINE":
                last = tok.span.line
                if tok.kind == "STRING" and self.lines[last - 1].startswith('"""', tok.span.col - 1):
                    start = sum(len(l) + 1 for l in self.lines[:last - 1]) + tok.span.col - 1
                    stop = self.source.index('"""', start + 3)
                    last += self.source.count("\n", start, stop)
        return last

    def remove_statement(self, s: A.Stmt) -> Edit | None:
        """Remove a one-line statement; `pass` keeps a block that would be
        empty. None when the statement goes on to other lines."""
        line = s.span.line
        end = next((t for t in self.tokens if t.kind == "NEWLINE"
                    and (t.span.line, t.span.col) >= (line, s.span.col)), None)
        if end is None or end.span.line != line:
            return None
        text = self.lines[line - 1]
        indent = len(text) - len(text.lstrip(" "))
        if self.alone_in_block(s):
            return Edit(line, indent + 1, line, len(text) + 1, "pass")
        return Edit(line, 1, line + 1, 1, "")

    def alone_in_block(self, s: A.Stmt) -> bool:
        for body in blocks(self.program):
            if any(x is s for x in body):
                return len(body) == 1 and body is not self.program.body
        return False


class Transparent(Scope):
    """The body of a match arm, an `else e` or a select arm: its own names
    are local, everything else is assigned in the enclosing scope (D25)."""

    def __init__(self, inner: Scope, outer: Scope):
        super().__init__(inner.node, inner, False)
        self.outer = outer

    def target(self) -> Scope:
        return self.outer.target()


# ------------------------------------------------------------- walking

def nodes(node):
    """Every AST node below `node` (statements and expressions)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            stack.extend(reversed(n))
            continue
        if isinstance(n, tuple):
            stack.extend(reversed(n))
            continue
        if not isinstance(n, A.Node):
            continue
        yield n
        for name in n.__dataclass_fields__:
            if name == "span":
                continue
            value = getattr(n, name)
            if isinstance(value, (A.Node, list, tuple)):
                stack.append(value)


def walk(body: list[A.Stmt]):
    for n in nodes(body):
        if isinstance(n, A.Stmt):
            yield n


def blocks(program: A.Program):
    """Every list of statements in the program."""
    yield program.body
    for n in nodes(program.body):
        for name in n.__dataclass_fields__:
            value = getattr(n, name)
            if isinstance(value, list) and value and isinstance(value[0], A.Stmt):
                yield value
            elif name == "branches":
                for _, b in value:
                    yield b


def declared_names(body: list[A.Stmt]) -> set[str]:
    found: set[str] = set()
    for n in nodes(body):
        if isinstance(n, (A.FnDef, A.TypeDef, A.TraitDef, A.VarDecl, A.SharedDecl, A.Param, A.Handler, A.PName)):
            found.add(n.var if isinstance(n, A.Handler) else n.name)
        elif isinstance(n, (A.For, A.Comprehension, A.ParallelFor)):
            found.add(n.var)
        elif isinstance(n, A.Lambda):
            found.add(n.param)
        elif isinstance(n, A.SelectArm) and n.var:
            found.add(n.var)
        elif isinstance(n, A.Assign) and isinstance(n.target, A.Name):
            found.add(n.target.name)
    return found


def root_name(e: A.Expr) -> str | None:
    while isinstance(e, (A.Attr, A.Index)):
        e = e.obj
    return e.name if isinstance(e, A.Name) else None


def is_fail(s: A.Stmt) -> bool:
    return (isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call) and isinstance(s.expr.func, A.Name)
            and s.expr.func.name == "fail")


def pure(e) -> bool:
    """An expression whose value is all it does."""
    if isinstance(e, (A.Call, A.Pipeline, A.ParallelFor, A.Propagate, A.Lambda)):
        return False
    if isinstance(e, A.Str):
        return all(isinstance(p, str) or pure(p) for p in e.parts)
    for n in nodes(e):
        if isinstance(n, (A.Call, A.Pipeline, A.ParallelFor, A.Propagate)):
            return False
    return True


def literal_key(e: A.Expr):
    if isinstance(e, A.Str) and all(isinstance(p, str) for p in e.parts):
        return ("str", "".join(e.parts))
    if isinstance(e, A.Num):
        return ("num", e.text)
    if isinstance(e, A.Bool):
        return ("bool", e.value)
    return None


def one_line(span: Span) -> bool:
    return span.end_col < 10**8


def edit_span(span: Span, text: str) -> Edit:
    return Edit(span.line, span.col, span.line, span.end_col, text)


# ------------------------------------------------- fixes of compile errors

def error_fix(err: NexoError, source: str) -> Fix | None:
    """The edit that fixes a compile error, when there is one."""
    lines = source.split("\n")
    span = err.span
    if span is None or not one_line(span) or span.line > len(lines):
        return None
    line = lines[span.line - 1]
    if err.fix == "suggest" and err.args_.get("suggestion"):
        return Fix([edit_span(span, err.args_["suggestion"])], False)
    if err.key == "question_cannot_fail" and line[span.end_col - 2:span.end_col - 1] == "?":
        return Fix([Edit(span.line, span.end_col - 1, span.line, span.end_col, "")], True)
    if err.key == "else_needs_var":
        return Fix([Edit(span.line, span.end_col, span.line, span.end_col, " e")], True)
    if err.key == "ignored_error":
        return Fix([Edit(span.line, span.end_col, span.line, span.end_col, "?")], False)
    if err.key == "fallible_needs_bang":
        at = err.args_.get("ret_end")
        if at is not None:
            return Fix([Edit(at[0], at[1], at[0], at[1], "!")], True)
    return None


# --------------------------------------------------------------- editing

def apply_edits(source: str, edits: list[Edit]) -> str:
    lines = source.split("\n")
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)

    def offset(line: int, col: int) -> int:
        if line > len(lines):
            return len(source)
        return min(starts[line - 1] + col - 1, len(source))

    for e in sorted(edits, key=lambda e: (e.line, e.col), reverse=True):
        a, b = offset(e.line, e.col), offset(e.end_line, e.end_col)
        source = source[:a] + e.text + source[b:]
    return source


def fix_source(source: str, file: str) -> tuple[str, int]:
    """Apply every safe fix, again and again until none is left. Returns
    the new text and the number of fixes applied."""
    applied = 0
    for _ in range(100):
        try:
            findings = lint_source(source, file)
        except NexoError as err:
            fix = error_fix(err, source)
            if fix is None or not fix.safe:
                break
            source = apply_edits(source, fix.edits)
            applied += 1
            continue
        safe = [f.fix for f in findings if f.fix is not None and f.fix.safe]
        if not safe:
            break
        # one fix per round when they could overlap
        chosen = [safe[0]]
        taken = [(safe[0].edits[0].line, safe[0].edits[-1].end_line)]
        for fix in safe[1:]:
            lo, hi = fix.edits[0].line, fix.edits[-1].end_line
            if all(hi < a or lo > b for a, b in taken):
                chosen.append(fix)
                taken.append((lo, hi))
        source = apply_edits(source, [e for f in chosen for e in f.edits])
        applied += len(chosen)
    tokenize(source, file)
    return source, applied
