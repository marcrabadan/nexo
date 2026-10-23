"""Command line (section 14):

    nexo                 interactive REPL
    nexo file.nexo       run a program
    nexo test [paths]    run the `test` blocks
    --lang es|en         language of messages (Spanish by default)
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import __version__, errors
from . import ast as A
from .errors import NexoError
from .interpreter import Env, Interpreter
from .types import NexoFailure, repr_value

USAGE = {
    "es": "Uso: nexo [--lang es|en] [archivo.nexo | test [rutas...]]",
    "en": "Usage: nexo [--lang es|en] [file.nexo | test [paths...]]",
}
BANNER = {
    "es": "Nexo {v} (fase 0). Escribe código y pulsa Intro; Ctrl-D para salir.",
    "en": "Nexo {v} (phase 0). Type code and press Enter; Ctrl-D to exit.",
}


def failure_text(error) -> str:
    kind = "" if error.kind == "Error" else f" ({error.kind})"
    message = error.message
    what = errors.msg("unhandled", kind=kind, message=message)
    if not what.endswith((".", "!", "?")):
        what += "."
    fix = errors.msg(error.fix) if error.fix and errors.has(error.fix) else errors.msg("fix.unhandled")
    return f"{what} {fix}"


def report(exc: BaseException) -> None:
    """Show an error in the section 14 format on stderr."""
    sys.stdout.flush()
    if isinstance(exc, NexoError):
        text = errors.render(exc.span, exc.text())
    elif isinstance(exc, NexoFailure):
        text = errors.render(exc.error.span, failure_text(exc.error))
    else:
        raise exc
    sys.stderr.write(text)
    sys.stderr.flush()


def run_file(path: str) -> int:
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError:
        sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=path)}.\n")
        return 1
    interp = Interpreter()
    try:
        interp.run(source, path)
        # the program ends when every spawned task has ended
        from .stdlib.sync import wait_all
        wait_all()
    except (NexoError, NexoFailure) as exc:
        report(exc)
        return 1
    except RecursionError:
        sys.stdout.flush()
        sys.stderr.write("Error: " + ("demasiadas llamadas anidadas (recursión infinita)."
                                      if errors.LANG == "es" else "too many nested calls (infinite recursion).") + "\n")
        return 1
    finally:
        sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- tests

def summary(total: int, passed: int, failed: int) -> str:
    if errors.LANG == "es":
        return (f"{total} {'prueba' if total == 1 else 'pruebas'}: "
                f"{passed} {'correcta' if passed == 1 else 'correctas'}, "
                f"{failed} {'fallida' if failed == 1 else 'fallidas'}")
    return f"{total} {'test' if total == 1 else 'tests'}: {passed} passed, {failed} failed"


def test_files(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for p in paths or ["."]:
        path = Path(p)
        if path.is_dir():
            for f in sorted(path.rglob("*.nexo")):
                if not any(part.startswith(".") for part in f.relative_to(path).parts):
                    found.append(f)
        else:
            found.append(path)
    return found


def run_tests(paths: list[str]) -> int:
    files = test_files(paths)
    total = passed = 0
    broken_files = 0
    for f in files:
        try:
            source = f.read_text(encoding="utf-8")
        except OSError:
            sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=str(f))}.\n")
            broken_files += 1
            continue
        if "test" not in source:
            continue
        name = str(f) if paths else str(f.relative_to("."))
        interp = Interpreter()
        try:
            interp.run(source, name)
        except (NexoError, NexoFailure) as exc:
            report(exc)
            broken_files += 1
            continue
        if not interp.tests:
            continue
        if len(files) > 1:
            print(name)
            sys.stdout.flush()
        for test in interp.tests:
            total += 1
            try:
                interp.exec_block(test.body, Env(interp.globals))
            except (NexoError, NexoFailure) as exc:
                print(f"✗ {test.name}")
                report(exc)
                continue
            passed += 1
            print(f"✓ {test.name}")
            sys.stdout.flush()
    if total == 0 and not broken_files:
        print(errors.msg("no_tests", path=", ".join(paths) or "."))
        return 0
    print(summary(total, passed, total - passed))
    sys.stdout.flush()
    return 0 if passed == total and not broken_files else 1


# ----------------------------------------------------------------- REPL

def repl() -> int:
    interp = Interpreter()
    print(BANNER[errors.LANG].format(v=__version__))
    count = 0
    while True:
        try:
            line = input(">>> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line.strip():
            continue
        lines = [line]
        if line.rstrip().endswith(":") or line.rstrip().endswith(","):
            while True:
                try:
                    more = input("... ")
                except (EOFError, KeyboardInterrupt):
                    break
                if not more.strip():
                    break
                lines.append(more)
        count += 1
        name = "<repl>"
        source = "\n".join(lines) + "\n"
        try:
            program = interp.load(source, name)
            for stmt in program.body:
                if isinstance(stmt, A.ExprStmt):
                    value = interp.bind_value(interp.eval(stmt.expr, interp.globals))
                    if value is not None:
                        print(repr_value(value))
                else:
                    interp.exec(stmt, interp.globals)
        except (NexoError, NexoFailure) as exc:
            report(exc)
        except RecursionError:
            print("Error: recursion")
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    lang = "es"
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--lang" and i + 1 < len(args):
            lang = args[i + 1]
            i += 2
            continue
        if a.startswith("--lang="):
            lang = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    errors.set_lang(lang)
    if lang not in ("es", "en") or any(a.startswith("-") for a in rest):
        print(USAGE[errors.LANG], file=sys.stderr)
        return 2
    sys.setrecursionlimit(10000)
    if not rest:
        return repl()
    if rest[0] == "test":
        return run_tests(rest[1:])
    if len(rest) > 1:
        print(USAGE[errors.LANG], file=sys.stderr)
        return 2
    return run_file(rest[0])


def entry() -> int:
    """Console entry point: like main(), but a closed output pipe (for
    example `nexo app.nexo | head`) ends the program quietly."""
    import os
    try:
        return main()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(entry())
