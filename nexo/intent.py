"""`nexo check --intent [paths]`: does each function do what its `##`
comments and its contracts say? (section "Goals and guarantees", D132)

A language model of `nexo.ai` reads the text of every top-level function
that has `##` comments above it or an `expect` or `ensure`, and answers
with a typed value: whether they agree, and why not. A probe program with
the file's `ai` blocks asks the questions, so the configuration, the
providers and `fake` are those of `ask`. Each disagreement is a warning on
the first comment line (or the definition)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import ast as A
from . import errors
from .errors import Span
from .lexer import tokenize
from .parser import Parser

MARK = "__nexo_intent"
INSTRUCTION = ("You check one function of the Nexo language. Its ## comments and its expect and ensure "
               "contracts say what it should do. Answer agrees: true when the code does what they say, or "
               "false when it does not; reason says in one sentence what disagrees.")


def nexo_string(text: str) -> str:
    esc = (text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
           .replace("\r", "\\r").replace("{", "{{"))
    return f'"{esc}"'


def _chunks(program: A.Program, lines: list[str]):
    """(statement, its lines) for each top-level statement."""
    starts = sorted({s.span.line for s in program.body})
    for s in program.body:
        i = starts.index(s.span.line)
        end = starts[i + 1] - 1 if i + 1 < len(starts) else len(lines)
        yield s, lines[s.span.line - 1:end]


def _ai_block(s: A.Stmt) -> bool:
    return (isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call) and isinstance(s.expr.func, A.Name)
            and s.expr.func.name in ("__ai_config", "__ai_models"))


def functions(program: A.Program, source: str) -> list[tuple[str, int, str]]:
    """(name, line to report, text) of each function with something to check."""
    lines = source.split("\n")
    out = []
    for s, chunk in _chunks(program, lines):
        if not isinstance(s, A.FnDef):
            continue
        first = s.span.line
        while first > 1 and lines[first - 2].startswith("##"):
            first -= 1
        contracts = any(isinstance(b, (A.Expect, A.Ensure)) for b in s.body)
        if first == s.span.line and not contracts:
            continue
        while chunk and not chunk[-1].strip():
            chunk = chunk[:-1]
        text = "\n".join(lines[first - 1:s.span.line - 1] + chunk)
        out.append((s.name, first, text))
    return out


def probe_source(program: A.Program, source: str, fns: list[tuple[str, int, str]]) -> str:
    lines = source.split("\n")
    out = ["\n".join(chunk) for s, chunk in _chunks(program, lines) if _ai_block(s)]
    out += ["type IntentCheck:", "    agrees: bool", "    reason: str"]
    for k, (_, _, text) in enumerate(fns):
        out += ["try:", f"    __i{k}: IntentCheck = ask({nexo_string(INSTRUCTION)}, {nexo_string(text)})",
                f'    print("\\n{MARK} {k} " + convert(__i{k}.agrees, to: json) + " " + __i{k}.reason.replace("\\n", " "))',
                "else e:", f'    print("\\n{MARK} {k} error " + e.message.replace("\\n", " "))']
    return "\n".join(out) + "\n"


def results(output: str) -> dict[int, tuple[str, str]]:
    found = {}
    for line in output.split("\n"):
        if line.startswith(MARK + " "):
            parts = line.split(" ", 3)
            if len(parts) >= 3 and parts[1].isdigit():
                found[int(parts[1])] = (parts[2], parts[3] if len(parts) == 4 else "")
    return found


def check_intent(source: str, path: str, run) -> tuple[list[str], str | None]:
    """The warnings of a file and the error that stopped the questions."""
    program = Parser(list(tokenize(source, path)), path).program()
    fns = functions(program, source)
    if not fns:
        return [], None
    res = results(run(probe_source(program, source, fns)))
    lines = source.split("\n")
    warnings, error = [], None
    for k, (name, line, _) in enumerate(fns):
        verdict, reason = res.get(k, ("error", errors.msg("info.intent_no_answer")))
        if verdict == "error":
            error = error or reason
        elif verdict == "false":
            reason = reason.rstrip(".")
            text = f"{errors.msg('intent.mismatch', name=name, reason=reason)} {errors.msg('fix.intent.mismatch')}"
            span = Span(path, line, 1, len(lines[line - 1]) + 1)
            warnings.append(errors.render(span, text, header="header_warning"))
    return warnings, error


def run_probe(path: str):
    def run(text: str) -> str:
        probe = Path(path).parent / f".nexo-intent-{os.getpid()}.nexo"
        probe.write_text(text, encoding="utf-8")
        try:
            p = subprocess.run([sys.executable, "-m", "nexo", str(probe)], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=600, text=True)
            return p.stdout
        except subprocess.TimeoutExpired:
            return ""
        finally:
            probe.unlink(missing_ok=True)
    return run
