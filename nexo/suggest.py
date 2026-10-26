"""`nexo test --suggest file.nexo`: test blocks with edge cases taken from
the types, the contracts and the unions of each function (section "Goals
and guarantees", D131).

For every top-level function whose parameters all have a type it knows,
it picks values for each parameter: the edges of its type (0, 1, -1 and
the largest int; an empty text; each literal of a union; the bounds of a
range; each variant of an enum of the file; none for `T?`), and the
values on each side of a bound in an `expect` (`expect n > 0` gives -1, 0
and 1). A first case uses a value of each parameter that keeps the
`expect`s; the others change one parameter at a time. A probe program
(the file's definitions and literal constants, without the rest of its
top level) runs every case, and each result becomes an `assert` of the
value the function gives now, or a check of the kind of error it fails
with. The developer reads them and keeps the ones that are right."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import ast as A
from . import errors
from .lexer import tokenize
from .parser import Parser

INT_EDGES = ["0", "1", "-1", "9223372036854775807"]
MARK = "__nexo_case"
INT_MAX = 2**63 - 1
INT_MIN = -(2**63)


def _literal_expr(e) -> bool:
    if isinstance(e, (A.Num, A.UnitLit, A.Bool, A.NoneLit)):
        return True
    if isinstance(e, A.Str):
        return all(isinstance(p, str) for p in e.parts)
    if isinstance(e, A.Unary):
        return e.op == "-" and isinstance(e.operand, (A.Num, A.UnitLit))
    if isinstance(e, A.ListLit):
        return all(_literal_expr(x) for x in e.items)
    if isinstance(e, A.MapLit):
        return all(_literal_expr(k) and _literal_expr(v) for k, v in e.pairs)
    return False


def _kept(s: A.Stmt) -> bool:
    """A top-level statement the probe keeps: a definition or a constant."""
    if isinstance(s, (A.FnDef, A.TypeDef, A.TraitDef, A.Use)):
        return True
    if isinstance(s, A.Assign):
        return isinstance(s.target, A.Name) and s.op == "=" and _literal_expr(s.value)
    if isinstance(s, A.VarDecl):
        return _literal_expr(s.value)
    return False


def _int_of(e) -> int | None:
    """An int literal (that fits in an int) or its negation."""
    if isinstance(e, A.Num) and not e.is_float:
        v = int(e.text.replace("_", ""))
        return v if v <= INT_MAX else None
    if isinstance(e, A.Unary) and e.op == "-" and isinstance(e.operand, A.Num) and not e.operand.is_float:
        v = int(e.operand.text.replace("_", ""))
        return -v if v <= INT_MAX else None
    return None


def _bounds(fn: A.FnDef, param: str) -> list[tuple[str, int]]:
    """(op, value) of each `expect param OP int` (written either way)."""
    flip = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "!=": "!="}
    out = []
    for s in fn.body:
        if not isinstance(s, A.Expect) or not isinstance(s.expr, A.Compare) or len(s.expr.ops) != 1:
            continue
        op = s.expr.ops[0]
        if op not in flip:
            continue
        left, right = s.expr.operands
        if isinstance(left, A.Name) and left.name == param and _int_of(right) is not None:
            out.append((op, _int_of(right)))
        elif isinstance(right, A.Name) and right.name == param and _int_of(left) is not None:
            out.append((flip[op], _int_of(left)))
    return out


def _holds(value: int, op: str, bound: int) -> bool:
    return {"<": value < bound, ">": value > bound, "<=": value <= bound, ">=": value >= bound,
            "==": value == bound, "!=": value != bound}[op]


def _slice(lines: list[str], span) -> str:
    return lines[span.line - 1][span.col - 1:span.end_col - 1]


def _candidates(t, lines: list[str], enums: dict[str, list[str]]) -> list[str] | None:
    if isinstance(t, A.RangeType):
        return list(dict.fromkeys([str(t.low), str(t.high)]))
    if isinstance(t, A.LiteralUnion):
        return [_slice(lines, x.span) for x in t.literals]
    if not isinstance(t, A.TypeName) or t.fallible or t.weak or t.rel:
        return None
    base: list[str] | None = None
    if not t.params:
        base = {"int": INT_EDGES[:], "float": ["0.0", "-1.5", "1.5"], "str": ['""', '"a b"'],
                "bool": ["true", "false"], "list": ["[]", "[0]"]}.get(t.name)
        if base is None and t.name in enums:
            base = enums[t.name][:]
    elif t.name == "list" and len(t.params) == 1:
        inner = _candidates(t.params[0], lines, enums)
        if inner:
            base = ["[]", f"[{inner[0]}]"]
    if base is None:
        return None
    if t.optional:
        base.append("none")
    return base


def calls_of(program: A.Program, source: str) -> list[tuple[str, list[str]]]:
    """(function, the calls of its cases) for each function it can test."""
    lines = source.split("\n")
    enums = {s.name: [v.name for v in s.variants] for s in program.body
             if isinstance(s, A.TypeDef) and s.variants and all(not v.params for v in s.variants)}
    out = []
    for fn in program.body:
        if not isinstance(fn, A.FnDef) or not fn.params:
            continue
        cands, base = [], []
        for p in fn.params:
            c = _candidates(p.type, lines, enums) if p.type is not None else None
            if not c:
                break
            bounds = _bounds(fn, p.name) if isinstance(p.type, A.TypeName) and p.type.name == "int" else []
            for _, b in bounds:
                for v in (b - 1, b, b + 1):
                    if INT_MIN <= v <= INT_MAX and str(v) not in c:
                        c.append(str(v))
            good = [x for x in c if re.fullmatch(r"-?\d+", x) is None or all(_holds(int(x), op, b) for op, b in bounds)]
            cands.append(c)
            base.append(good[0] if good else c[0])
        else:
            calls = [base[:]]
            for i, c in enumerate(cands):
                for x in c:
                    if x != base[i]:
                        calls.append(base[:i] + [x] + base[i + 1:])
            texts = list(dict.fromkeys(f"{fn.name}({', '.join(a)})" for a in calls))
            out.append((fn.name, texts))
    return out


def probe_source(program: A.Program, source: str, calls: list[str]) -> str:
    lines = source.split("\n")
    starts = sorted({s.span.line for s in program.body})
    kept: list[str] = []
    for s in program.body:
        if not _kept(s):
            continue
        i = starts.index(s.span.line)
        end = starts[i + 1] - 1 if i + 1 < len(starts) else len(lines)
        chunk = "\n".join(lines[s.span.line - 1:end])
        if chunk not in kept:
            kept.append(chunk)
    out = kept[:]
    for k, call in enumerate(calls):
        out += ["try:", f"    __s{k} = {call}", f'    print("\\n{MARK} {k} ok " + convert(__s{k}, to: json))',
                "else e:", f'    print("\\n{MARK} {k} err " + e.kind)']
    return "\n".join(out) + "\n"


def results(output: str) -> dict[int, tuple[str, str]]:
    found = {}
    for line in output.split("\n"):
        if line.startswith(MARK + " "):
            parts = line.split(" ", 3)
            if len(parts) == 4 and parts[1].isdigit():
                found[int(parts[1])] = (parts[2], parts[3])
    return found


def nexo_literal(text: str) -> str | None:
    """The Nexo literal of a JSON result, when it has a plain one."""
    if re.fullmatch(r"-?\d+", text) or re.fullmatch(r"-?\d+\.\d+", text):
        return text
    if text in ("true", "false"):
        return text
    if text == "null":
        return "none"
    if text.startswith('"'):
        value = json.loads(text)
        if any(ord(c) < 32 and c not in "\n\t\r" for c in value):
            return None
        esc = (value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
               .replace("\r", "\\r").replace("{", "{{"))
        return f'"{esc}"'
    return None


def render(name: str, calls: list[str], res: dict[int, tuple[str, str]], first: int) -> list[str]:
    body: list[str] = []
    declared = False
    for k, call in enumerate(calls):
        r = res.get(first + k)
        if r is None:
            body.append(f"    # {call}: {errors.msg('info.suggest_no_result')}")
        elif r[0] == "ok":
            lit = nexo_literal(r[1])
            if lit is None:
                body.append(f"    # {call}: {errors.msg('info.suggest_result', json=r[1])}")
            else:
                body.append(f"    assert {call} == {lit}")
        else:
            body.append("    var kind = \"\"" if not declared else "    kind = \"\"")
            declared = True
            body += ["    try:", f"        {call}", "    else e:", "        kind = e.kind", f'    assert kind == "{r[1]}"']
    return [f'test "{name}: {errors.msg("info.suggest_name")}":'] + body


def suggest(source: str, path: str, run) -> str:
    """The suggested tests for a file; `run(probe_text)` runs a probe and
    returns its output."""
    program = Parser(list(tokenize(source, path)), path).program()
    fns = calls_of(program, source)
    if not fns:
        return errors.msg("info.suggest_none", path=path) + "\n"
    calls = [c for _, cs in fns for c in cs]
    res = results(run(probe_source(program, source, calls)))
    out = [errors.msg("info.suggest_header"), ""]
    first = 0
    for name, cs in fns:
        out += render(name, cs, res, first)
        out.append("")
        first += len(cs)
    return "\n".join(out[:-1]) + "\n"


def run_probe(path: str):
    """Run a probe with this interpreter, next to the file (its modules)."""
    def run(text: str) -> str:
        probe = Path(path).parent / f".nexo-suggest-{os.getpid()}.nexo"
        probe.write_text(text, encoding="utf-8")
        try:
            p = subprocess.run([sys.executable, "-m", "nexo", str(probe)], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=60, text=True)
            return p.stdout
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or b""
            return out.decode() if isinstance(out, bytes) else out
        finally:
            probe.unlink(missing_ok=True)
    return run
