"""`nexo audit [paths]`: every use of code that Nexo does not check
(section "Goals and guarantees", D129). In this edition that is C: a
`use c` library and each call to one of its functions count as implicit
`unsafe` (section 18); `unsafe:` blocks belong to nexo.perf, which is not
part of it, so they are a compile error and never reach the audit."""

from __future__ import annotations

from . import ast as A
from . import errors
from .lint import nodes
from .parser import Parser
from .lexer import tokenize


def line_text(key: str, **args) -> str:
    return f"{errors.msg(key, **args)} {errors.msg('fix.' + key)}"


def audit_source(source: str, name: str) -> list[tuple[int, int, str]]:
    """(line, column, text) of each unsafe use, in the order of the file;
    the text ends with its fix."""
    program = Parser(list(tokenize(source, name)), name).program()
    found: list[tuple[int, int, str]] = []
    bound: set[str] = set()
    for n in nodes(program.body):
        if isinstance(n, A.Use) and n.lang == "c":
            shown = f'use c "{n.name}"' + (f" as {n.alias}" if n.alias else "")
            found.append((n.span.line, n.span.col, line_text("audit.use_c", use=shown)))
            bound.add(n.alias or n.name)
    for n in nodes(program.body):
        if (isinstance(n, A.Call) and isinstance(n.func, A.Attr) and isinstance(n.func.obj, A.Name)
                and n.func.obj.name in bound):
            call = f"{n.func.obj.name}.{n.func.name}"
            found.append((n.span.line, n.span.col, line_text("audit.call_c", call=call)))
    return sorted(found)
