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
    if args[:1] in (["probe"], ["worker"]) or any(a in ("--sandbox=linux", "--sandbox=linux-online") or a.startswith("--web=loopback:") for a in args):
        if not compiler.is_file() or not os.access(compiler, os.X_OK):
            print("Error: Native compiler unavailable; build compiler/ or set NEXO_COMPILER to an executable compiler path.", file=sys.stderr)
            return 125
        env = None
        if sys.platform == "linux" and any(a.startswith("--web=loopback:") for a in args):
            env = dict(os.environ, NEXO_SECURITY_WEB_FRONTEND=str(os.getpid()))
        return subprocess.run([str(compiler), "security", *args], env=env).returncode
    rest, bindings = [], {}
    i = 0
    while i < len(args):
        flag = args[i]
        if flag.startswith("--web="):
            if flag != "--web=memory" or "NEXO_SECURITY_WEB" in bindings:
                print("Error: Choose --web=memory or --web=loopback:PORT exactly once.", file=sys.stderr)
                return 2
            bindings["NEXO_SECURITY_WEB"] = "memory-v1"
            i += 1
            continue
        if flag.startswith("--streams="):
            if flag != "--streams=memory" or "NEXO_SECURITY_STREAMS" in bindings:
                print("Error: Choose --streams=memory exactly once.", file=sys.stderr)
                return 2
            bindings["NEXO_SECURITY_STREAMS"] = "memory-v1"
            i += 1
            continue
        if flag == "--ingress":
            print("Error: --ingress requires --web=loopback:PORT.", file=sys.stderr)
            return 2
        if flag not in ("--dependencies", "--authorization"):
            rest.append(flag)
            i += 1
            continue
        key = "NEXO_SECURITY_DEPENDENCIES" if flag == "--dependencies" else "NEXO_SECURITY_AUTHORIZATION"
        if i + 1 >= len(args) or key in bindings:
            print("Error: Missing or duplicate security configuration option.", file=sys.stderr)
            return 2
        bindings[key] = args[i+1]
        i += 2
    if not bindings:
        return _run_phase1(args)
    if len(rest) != 2 or rest[0] not in ("check", "run", "trace", "audit"):
        print("Usage: nexo security check|run|trace|audit [--dependencies file.json] [--authorization file.json] [--streams=memory] [--web=memory|loopback:PORT] [--ingress file.json] app.nexo", file=sys.stderr)
        return 2
    for key, value in bindings.items():
        if key in ("NEXO_SECURITY_STREAMS", "NEXO_SECURITY_WEB"):
            continue
        try:
            path = Path(value).resolve(strict=True)
            if key == "NEXO_SECURITY_DEPENDENCIES":
                from . import security_context
                security_context.load(path, rest[1])
            else:
                from . import authorization
                authorization.load(path)
            bindings[key] = str(path)
        except (OSError, ValueError, TypeError, RecursionError):
            cli.report(NexoError("security_context_config" if key == "NEXO_SECURITY_DEPENDENCIES" else "security_authorization"))
            return 1
    previous = {key: os.environ.get(key) for key in bindings}
    try:
        os.environ.update(bindings)
        return _run_phase1(rest)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
