"""Command line (section 14):

    nexo                 interactive REPL
    nexo file.nexo       run a program
    nexo test [paths]    run the `test` blocks
    nexo fmt [paths]     format files in place (D85)
    nexo lint [paths]    warn about questionable code (D86)
    nexo fix [paths]     apply the safe fixes
    nexo check [paths]   the compile checks, without running anything
    nexo lsp             language server on standard input and output (D87)
    nexo db diff|migrate file.nexo [--db path] [--allow-drop]   migrations (D101)
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import __version__, errors
from . import ast as A
from .errors import NexoError
from .interpreter import Env, Interpreter
from .types import NexoFailure, repr_value

USAGE = ("Usage: nexo [file.nexo | test|fmt|lint|fix|check|audit [paths...] | test --suggest file.nexo | check --intent [paths...] | docs --for-ai [file.nexo] | lsp"
         " | db diff|migrate file.nexo [--db path] [--allow-drop] | ai setup|usage"
         " | plan|apply|destroy|drift|cost [file.nexo] [--stage stage] [--yes] | import cloud resource id [file.nexo] | ci run [pipeline] [file.nexo] [--step step | --after success|failure] [--yes] | ci export github|gitlab|azure|jenkins [pipeline] [file.nexo] | flags set name value [file.nexo] [--stage stage] | privacy report [file.nexo]]")
BANNER = "Nexo {v} (phase 0). Type code and press Enter; Ctrl-D to exit."


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
        # then its servers answer and its streams are read until it is
        # stopped (D102, D117)
        from .stdlib import stream
        from .stdlib.web import SERVERS, serve_forever
        if stream.listening():
            if not SERVERS:
                stream.listen_forever(interp, path)
                return 0
            import threading
            threading.Thread(target=stream.listen_forever, args=(interp, path), daemon=True).start()
        if not serve_forever(interp):
            return 1
    except (NexoError, NexoFailure) as exc:
        report(exc)
        return 1
    except RecursionError:
        sys.stdout.flush()
        sys.stderr.write(errors.render(None, NexoError("too_deep").text()))
        return 1
    finally:
        sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- tests

def summary(total: int, passed: int, failed: int) -> str:
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
        from .stdlib import stream
        stream.TEST_MODE = True
        try:
            interp.run(source, name)
        except (NexoError, NexoFailure) as exc:
            report(exc)
            broken_files += 1
            continue
        if not interp.tests:
            continue
        from .stdlib.web import serve_in_background
        try:
            serve_in_background(interp)
        except NexoError as exc:
            report(exc)
            broken_files += 1
            continue
        if len(files) > 1:
            print(name)
            sys.stdout.flush()
        for test in interp.tests:
            total += 1
            interp.fakes = {}
            stream.MEMORY.clear()
            stream.SEEN_KEYS.clear()
            try:
                interp.exec_block(test.body, Env(interp.globals))
                stream.drain(interp)
            except (NexoError, NexoFailure) as exc:
                print(f"✗ {test.name}")
                report(exc)
                continue
            passed += 1
            print(f"✓ {test.name}")
            sys.stdout.flush()
        # a mistake in a handler of a server is a broken program (D102)
        from .stdlib import web
        if web.MISTAKES:
            broken_files += 1
            web.MISTAKES.clear()
    if total == 0 and not broken_files:
        print(errors.msg("no_tests", path=", ".join(paths) or "."))
        return 0
    print(summary(total, passed, total - passed))
    sys.stdout.flush()
    return 0 if passed == total and not broken_files else 1


# ------------------------------------------------------------------ fmt

def run_fmt(paths: list[str]) -> int:
    """`nexo fmt`: format the files in place and print the ones it changed."""
    from .fmt import format_source
    status = 0
    for f in test_files(paths):
        try:
            source = f.read_text(encoding="utf-8")
        except OSError:
            sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=str(f))}.\n")
            status = 1
            continue
        name = str(f) if paths else str(f.relative_to("."))
        try:
            formatted = format_source(source, name)
        except NexoError as exc:
            report(exc)
            status = 1
            continue
        if formatted != source:
            f.write_text(formatted, encoding="utf-8")
            print(name)
    return status


# ----------------------------------------------------------- lint and fix

def run_lint(paths: list[str]) -> int:
    """`nexo lint`: every warning with its fix, then a summary."""
    from .lint import lint_source, render_finding
    total = safe = 0
    status = 0
    for f in test_files(paths):
        try:
            source = f.read_text(encoding="utf-8")
        except OSError:
            sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=str(f))}.\n")
            status = 1
            continue
        name = str(f) if paths else str(f.relative_to("."))
        try:
            findings = lint_source(source, name)
        except NexoError as exc:
            report(exc)
            status = 1
            continue
        for finding in findings:
            sys.stdout.write(render_finding(finding))
            total += 1
            safe += finding.fix is not None and finding.fix.safe
    if total:
        print(errors.msg("lint_summary", n=total, safe=safe))
        return 1
    if status == 0:
        print(errors.msg("lint_none", path=", ".join(paths) or "."))
    return status


def run_fix(paths: list[str]) -> int:
    """`nexo fix`: apply the safe fixes in place and print the files changed."""
    from .lint import fix_source
    status = 0
    for f in test_files(paths):
        try:
            source = f.read_text(encoding="utf-8")
        except OSError:
            sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=str(f))}.\n")
            status = 1
            continue
        name = str(f) if paths else str(f.relative_to("."))
        try:
            fixed, _count = fix_source(source, name)
        except NexoError as exc:
            report(exc)
            status = 1
            continue
        if fixed != source:
            f.write_text(fixed, encoding="utf-8")
            print(name)
    return status


def run_check(paths: list[str]) -> int:
    """`nexo check`: report the compile error of each file, if any."""
    from .lint import front_end
    status = 0
    for f in test_files(paths):
        try:
            source = f.read_text(encoding="utf-8")
        except OSError:
            sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=str(f))}.\n")
            status = 1
            continue
        name = str(f) if paths else str(f.relative_to("."))
        try:
            front_end(source, name)
        except NexoError as exc:
            report(exc)
            status = 1
    if status == 0:
        print(errors.msg("check_ok", path=", ".join(paths) or "."))
    return status


def run_intent(paths: list[str]) -> int:
    """`nexo check --intent`: comments and contracts against the code (D132)."""
    from .intent import check_intent, run_probe
    total = status = 0
    for f in test_files(paths):
        try:
            source = f.read_text(encoding="utf-8")
        except OSError:
            sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=str(f))}.\n")
            status = 1
            continue
        name = str(f) if paths else str(f.relative_to("."))
        try:
            warnings, error = check_intent(source, name, run_probe(str(f)))
        except NexoError as exc:
            report(exc)
            status = 1
            continue
        for w in warnings:
            sys.stdout.write(w)
        total += len(warnings)
        if error is not None:
            sys.stderr.write(f"Error: {error}\n")
            return 1
    if total:
        print(errors.msg("info.intent_summary", n=total))
        return 1
    if status == 0:
        print(errors.msg("info.intent_ok", path=", ".join(paths) or "."))
    return status


# ------------------------------------------------------------- nexo.db

def run_db(args: list[str]) -> int:
    """`nexo db diff|migrate file.nexo [--db path] [--allow-drop]` (D101)."""
    from .parser import parse
    from .stdlib import db
    command = args[0] if args else ""
    file = db_path = None
    allow_drop = False
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--db" and i + 1 < len(args):
            db_path = args[i + 1]
            i += 2
            continue
        if a == "--allow-drop" and command == "migrate":
            allow_drop = True
        elif file is None and not a.startswith("-"):
            file = a
        else:
            file = None
            break
        i += 1
    if command not in ("diff", "migrate") or file is None:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        source = Path(file).read_text(encoding="utf-8")
    except OSError:
        sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=file)}.\n")
        return 1
    interp = Interpreter()
    try:
        program = interp.load(source, file)
        if db_path is None:
            db_path = db.connect_literal(program)
        interp.module_filter = db.schema_program
        for stmt in db.schema_program(program):
            interp.exec(stmt, interp.globals)
    except (NexoError, NexoFailure) as exc:
        report(exc)
        return 1
    return db.run_command(interp, command, file, db_path, allow_drop)


# ------------------------------------------------------------- phase 5

def split_options(args: list[str], flags: tuple = (), values: tuple = ()):
    """(positional, {option: value or True}) or None when an option is unknown."""
    pos, opts = [], {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in values and i + 1 < len(args):
            opts[a] = args[i + 1]
            i += 2
            continue
        if a in flags:
            opts[a] = True
        elif a.startswith("--"):
            return None
        else:
            pos.append(a)
        i += 1
    return pos, opts


def program_file(word: str, given: str | None, pattern: str | None = None) -> str | None:
    """The file a phase 5 command reads: the one given, or the only .nexo
    file of the current directory that has a `word` block (D113)."""
    if given is not None:
        return given
    import re
    pattern = re.compile(pattern or rf"^{word}\b", re.M)
    found = []
    for f in sorted(Path(".").glob("*.nexo")):
        try:
            if pattern.search(f.read_text(encoding="utf-8")):
                found.append(str(f))
        except OSError:
            continue
    if len(found) == 1:
        return found[0]
    sys.stderr.write(errors.render(None, NexoError("which_file", None, word=word).text()))
    return None


def read_program(path: str):
    """(source, program) parsed, or None after reporting the error."""
    from .parser import parse
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError:
        sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=path)}.\n")
        return None
    errors.SOURCES[path] = source
    try:
        return source, parse(source, path)
    except NexoError as exc:
        report(exc)
        return None


def run_flags(args: list[str]) -> int:
    """`nexo flags set NAME VALUE [file.nexo] [--stage S]` (section 22)."""
    from .stdlib import flags
    parsed = split_options(args[1:], values=("--stage",))
    if args[:1] != ["set"] or parsed is None or len(parsed[0]) not in (2, 3):
        print(USAGE, file=sys.stderr)
        return 2
    pos, opts = parsed
    path = program_file("flags", pos[2] if len(pos) == 3 else None)
    if path is None:
        return 1
    read = read_program(path)
    if read is None:
        return 1
    return flags.set_command(flags.declared(read[1]), pos[0], pos[1], opts.get("--stage"))


def run_privacy(args: list[str]) -> int:
    """`nexo privacy report [file.nexo]` (section 23, rule 4)."""
    from .stdlib import db, privacy
    if args[:1] != ["report"] or len(args) > 2 or any(a.startswith("-") for a in args):
        print(USAGE, file=sys.stderr)
        return 2
    path = program_file("@personal", args[1] if len(args) == 2 else None, r"@personal\b|@private\b")
    if path is None:
        return 1
    interp = Interpreter()
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError:
        sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=path)}.\n")
        return 1
    try:
        program = interp.load(source, path)
        interp.module_filter = db.schema_program
        for stmt in db.schema_program(program):
            interp.exec(stmt, interp.globals)
    except (NexoError, NexoFailure) as exc:
        report(exc)
        return 1
    for line in privacy.report_lines(path, db.connect_literal(program), privacy.readers_of_data(program)):
        print(line)
    return 0


INFRA_COMMANDS = ("plan", "apply", "destroy", "drift", "cost", "import")


def infra_program(program) -> list:
    """What the commands of nexo.infra run: the types (for privacy, D121),
    constants written as literals, and the infra and stage blocks."""
    keep = []
    for s in program.body:
        if isinstance(s, (A.TypeDef, A.TraitDef, A.Use)):
            keep.append(s)
        elif (isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call) and isinstance(s.expr.func, A.Name)
              and s.expr.func.name in ("__db_table", "__privacy_type", "__infra", "__stage")):
            keep.append(s)
        elif isinstance(s, A.Assign) and s.op == "=" and isinstance(s.target, A.Name) and literal(s.value):
            keep.append(s)
    return keep


def literal(e) -> bool:
    if isinstance(e, (A.Num, A.UnitLit, A.Bool, A.NoneLit)):
        return True
    if isinstance(e, A.Str):
        return all(isinstance(p, str) for p in e.parts)
    if isinstance(e, A.ListLit):
        return all(literal(i) for i in e.items)
    return False


def run_infra(command: str, args: list[str]) -> int:
    """`nexo plan|apply|destroy|drift|cost [file.nexo] [--stage s] [--yes]`
    and `nexo import CLOUD RESOURCE ID [file.nexo] [--stage s]` (section 11)."""
    from .stdlib import infra, infra_cloud
    parsed = split_options(args, flags=("--yes",) if command == "apply" else (), values=("--stage",))
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    pos, opts = parsed
    extra = 3 if command == "import" else 0
    if len(pos) not in (extra, extra + 1):
        print(USAGE, file=sys.stderr)
        return 2
    path = program_file("infra", pos[extra] if len(pos) > extra else None)
    if path is None:
        return 1
    interp = Interpreter()
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError:
        sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=path)}.\n")
        return 1
    try:
        program = interp.load(source, path)
        interp.module_filter = infra_program
        for stmt in infra_program(program):
            interp.exec(stmt, interp.globals)
        if not infra.INFRAS:
            raise NexoError("infra_none", None, file=path)
        return infra_cloud.command(interp, command, pos[:extra], opts.get("--stage"), "--yes" in opts)
    except (NexoError, NexoFailure) as exc:
        report(exc)
        return 1


def ci_program(program) -> list:
    """What nexo ci runs: the declarations of the program (types,
    functions, literal constants) and its blocks, not its other code (D122)."""
    keep = []
    for s in program.body:
        if isinstance(s, (A.TypeDef, A.TraitDef, A.Use, A.FnDef)):
            keep.append(s)
        elif (isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call) and isinstance(s.expr.func, A.Name)
              and s.expr.func.name in ("__db_table", "__privacy_type", "__infra", "__stage", "__flags", "__pipeline")):
            keep.append(s)
        elif isinstance(s, A.Assign) and s.op == "=" and isinstance(s.target, A.Name) and literal(s.value):
            keep.append(s)
    return keep


def approvals(program, pipeline: str) -> dict:
    """step -> the people of its approve(by: [...]) call (D123)."""
    from .checker import walk_stmts
    from .parser import stmt_exprs, sub_exprs
    out = {}
    for s in program.body:
        if not (isinstance(s, A.FnDef) and s.name.startswith(f"{pipeline} step ")):
            continue
        step = s.name[len(pipeline) + 6:]

        def visit(e):
            if (isinstance(e, A.Call) and isinstance(e.func, A.Name) and e.func.name == "approve"
                    and step not in out):
                for a in e.args:
                    if isinstance(a.value, A.ListLit):
                        out[step] = [i.parts[0] for i in a.value.items
                                     if isinstance(i, A.Str) and len(i.parts) == 1 and isinstance(i.parts[0], str)]
            for c in sub_exprs(e):
                visit(c)
        for st in walk_stmts(s.body):
            for e in stmt_exprs(st):
                visit(e)
    return out


def run_ci(args: list[str]) -> int:
    """`nexo ci run [PIPELINE] [file.nexo] [--step S | --after success|failure] [--yes]`
    and `nexo ci export github|gitlab|azure|jenkins [PIPELINE] [file.nexo]` (section 12)."""
    from .stdlib import ci, ci_export
    sub = args[0] if args else None
    if sub == "run":
        parsed = split_options(args[1:], flags=("--yes",), values=("--step", "--after"))
    elif sub == "export":
        parsed = split_options(args[1:], flags=(), values=())
    else:
        parsed = None
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    pos, opts = parsed
    target = None
    if sub == "export":
        if not pos:
            print(USAGE, file=sys.stderr)
            return 2
        target, pos = pos[0], pos[1:]
    files = [a for a in pos if a.endswith(".nexo")]
    names = [a for a in pos if not a.endswith(".nexo")]
    after = opts.get("--after")
    if (len(files) > 1 or len(names) > 1 or ("--step" in opts and after is not None)
            or after not in (None, "success", "failure")):
        print(USAGE, file=sys.stderr)
        return 2
    path = program_file("pipeline", files[0] if files else None)
    if path is None:
        return 1
    loaded = read_program(path)
    if loaded is None:
        return 1
    try:
        if target is not None and target not in ci_export.TARGETS:
            raise NexoError("ci_target", None, target=target)
        interp = Interpreter()
        program = interp.load(loaded[0], path)
        interp.module_filter = ci_program
        for stmt in ci_program(program):
            interp.exec(stmt, interp.globals)
        ci.FILE[0] = path
        p = ci.pick(names[0] if names else None, path)
        if target is None:
            return ci.run_pipeline(interp, p, opts.get("--step"), after, "--yes" in opts, report)
        out, text = ci_export.export(target, p, path, approvals(program, p.name))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
        print(errors.msg("info.ci_exported", path=out))
        return 0
    except (NexoError, NexoFailure) as exc:
        report(exc)
        return 1


# ----------------------------------------------------------------- REPL

def repl() -> int:
    interp = Interpreter()
    print(BANNER.format(v=__version__))
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
            sys.stdout.flush()
            sys.stderr.write(errors.render(None, NexoError("too_deep").text()))
        sys.stdout.flush()


def run_audit(paths: list[str]) -> int:
    """`nexo audit`: every use of unsafe code, then a summary (D129)."""
    from .audit import audit_source
    total = files = 0
    status = 0
    for f in test_files(paths):
        try:
            source = f.read_text(encoding="utf-8")
        except OSError:
            sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=str(f))}.\n")
            status = 1
            continue
        name = str(f) if paths else str(f.relative_to("."))
        try:
            found = audit_source(source, name)
        except NexoError as exc:
            report(exc)
            status = 1
            continue
        for line, _, text in found:
            print(f"{name}:{line}: {text}")
        total += len(found)
        files += bool(found)
    if total:
        print(errors.msg("info.audit_summary", n=total, files=files))
    elif status == 0:
        print(errors.msg("info.audit_none", path=", ".join(paths) or "."))
    return status


def run_suggest(args: list[str]) -> int:
    """`nexo test --suggest file.nexo` (D131)."""
    from .suggest import run_probe, suggest
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        source = Path(args[0]).read_text(encoding="utf-8")
    except OSError:
        sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=args[0])}.\n")
        return 1
    try:
        sys.stdout.write(suggest(source, args[0], run_probe(args[0])))
    except NexoError as exc:
        report(exc)
        return 1
    return 0


def run_docs(args: list[str]) -> int:
    """`nexo docs --for-ai [file]` (D130)."""
    from .docs import for_ai
    if args[:1] != ["--for-ai"] or len(args) > 2:
        print(USAGE, file=sys.stderr)
        return 2
    if len(args) == 1:
        sys.stdout.write(for_ai())
        return 0
    try:
        source = Path(args[1]).read_text(encoding="utf-8")
    except OSError:
        sys.stderr.write(f"Error: {errors.msg('err.NotFound', path=args[1])}.\n")
        return 1
    sys.stdout.write(for_ai(args[1], source))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    rest = args
    if rest[:1] == ["docs"]:
        return run_docs(rest[1:])
    if rest[:2] == ["test", "--suggest"]:
        return run_suggest(rest[2:])
    if rest[:2] == ["check", "--intent"]:
        return run_intent(rest[2:])
    if rest[:1] == ["db"]:
        return run_db(rest[1:])
    if rest[:1] == ["flags"]:
        return run_flags(rest[1:])
    if rest[:1] and rest[0] in INFRA_COMMANDS:
        return run_infra(rest[0], rest[1:])
    if rest[:1] == ["ci"]:
        return run_ci(rest[1:])
    if rest[:1] == ["privacy"]:
        return run_privacy(rest[1:])
    if rest in (["ai", "setup"], ["ai", "usage"]):
        from .stdlib import ai
        return ai.setup_command() if rest[1] == "setup" else ai.usage_command()
    if any(a.startswith("-") for a in rest):
        print(USAGE, file=sys.stderr)
        return 2
    sys.setrecursionlimit(10000)
    if not rest:
        return repl()
    if rest[0] == "test":
        return run_tests(rest[1:])
    if rest[0] == "fmt":
        return run_fmt(rest[1:])
    if rest[0] == "lint":
        return run_lint(rest[1:])
    if rest[0] == "fix":
        return run_fix(rest[1:])
    if rest[0] == "check":
        return run_check(rest[1:])
    if rest[0] == "audit":
        return run_audit(rest[1:])
    if rest == ["lsp"]:
        from .lsp import serve
        return serve()
    if len(rest) > 1:
        print(USAGE, file=sys.stderr)
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
