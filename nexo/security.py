"""Shield CLI: strict application API enforcement, not OS confinement."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import cli
from .errors import NexoError
from .parser import parse
from .stdlib import policy


def _run_phase1(args: list[str]) -> int:
    if len(args) != 2 or args[0] not in ("check", "run", "trace", "audit") or args[1].startswith("-"):
        print("Usage: nexo security check|run|trace|audit file.nexo", file=sys.stderr)
        return 2
    command, file = args
    try:
        program = parse(Path(file).read_text(encoding="utf-8"), file)
        if policy.declared(program) is None:
            raise NexoError("policy_missing")
    except OSError:
        print(f"Error: Cannot read {file}.", file=sys.stderr)
        return 1
    except NexoError as exc:
        cli.report(exc)
        return 1
    if command in ("check", "audit"):
        status = cli.run_check([file])
        if status:
            return status
        print(policy.show_text(file, program), end="")
        # Reuse the unsafe inventory; this is not a vulnerability scanner.
        if command == "audit":
            return cli.run_audit([file])
        return 0
    previous = {k: os.environ.get(k) for k in ("NEXO_SECURITY_ENFORCE", "NEXO_SECURITY_TRACE")}
    try:
        os.environ["NEXO_SECURITY_ENFORCE"] = "1"
        if command == "trace":
            os.environ["NEXO_SECURITY_TRACE"] = "1"
        return cli.run_file(file)
    finally:
        for k, value in previous.items():
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value


def run_security(args: list[str]) -> int:
    import subprocess
    compiler = Path(os.environ.get("NEXO_COMPILER", str(Path(__file__).resolve().parents[1] / "compiler/target/debug/nexo")))
    if args[:1] in (["probe"], ["worker"]) or "--sandbox=linux" in args:
        if not compiler.is_file() or not os.access(compiler, os.X_OK):
            print("Error: Native compiler unavailable; build compiler/ or set NEXO_COMPILER to an executable compiler path.", file=sys.stderr)
            return 125
        return subprocess.run([str(compiler), "security", *args]).returncode
    if "--dependencies" not in args:
        return _run_phase1(args)
    index = args.index("--dependencies")
    if index + 1 >= len(args):
        print("Error: Missing dependency permission file.", file=sys.stderr)
        return 2
    rest = args[:index] + args[index+2:]
    if len(rest) != 2:
        print("Usage: nexo security check|run|trace|audit --dependencies file.json app.nexo", file=sys.stderr)
        return 2
    try:
        from . import security_context
        path = Path(args[index+1]).resolve(strict=True)
        security_context.load(path, rest[1])
    except (OSError, ValueError, TypeError):
        cli.report(NexoError("security_context_config"))
        return 1
    previous = os.environ.get("NEXO_SECURITY_DEPENDENCIES")
    try:
        os.environ["NEXO_SECURITY_DEPENDENCIES"] = str(path)
        return _run_phase1(rest)
    finally:
        if previous is None:
            os.environ.pop("NEXO_SECURITY_DEPENDENCIES", None)
        else:
            os.environ["NEXO_SECURITY_DEPENDENCIES"] = previous
