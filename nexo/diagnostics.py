"""Version 1 check diagnostics; reuse the front end and LSP edits (D181)."""
from __future__ import annotations

import json

from . import errors
from .errors import NexoError
from .lint import front_end
from .lsp import error_diagnostic


def problem(code: str, message: str, file: str | None) -> dict:
    return {"file": file, "range": None, "severity": 1, "source": "nexo",
            "code": code, "message": message, "data": {"fixes": []}}


def run_check_json(args: list[str]) -> int:
    from .cli import test_files
    effects = "--effects" in args
    json_mode = "--json" in args
    paths, options = [], {}
    invalid = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('--dependencies', '--authorization'):
            if not effects or a in options or i + 1 == len(args) or args[i + 1].startswith('-'):
                invalid = True
                break
            options[a] = args[i + 1]
            i += 2
            continue
        if a not in ('--json', '--effects', '--require-complete'):
            paths.append(a)
        i += 1
    strict = '--require-complete' in args
    invalid |= args.count('--require-complete') > 1 or strict and not effects
    out = []
    status = 0
    if (invalid or any(a.startswith("-") for a in paths) or args.count("--json") > 1
            or args.count("--effects") > 1 or not (effects or json_mode)):
        out.append(problem("check_usage", "Usage: nexo check --json [paths...]", None))
        status = 2
    else:
        for f in test_files(paths):
            name = str(f)
            try:
                source = f.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                out.append(problem("check_io", "Cannot read source as UTF-8.", name))
                status = 1
                continue
            try:
                if effects:
                    from .effects import analyze
                    findings = analyze(source, name, options.get("--dependencies"), options.get("--authorization"))
                    out.extend(findings)
                    status = max(status, int(any(d["severity"] == 1 or strict and not d["data"]["complete"] for d in findings)))
                else:
                    front_end(source, name)
            except NexoError as err:
                file = err.span.file if err.span else name
                text = errors.SOURCES.get(file, source) if file != name else source
                d = error_diagnostic(err, text)
                d["file"] = file
                if err.span is None:
                    d["range"] = None
                out.append(d)
                status = 1
    if json_mode:
        print(json.dumps({"schema_version": 1, "position_encoding": "utf-16",
                          "success": status == 0, "diagnostics": out}, ensure_ascii=True))
    else:
        for d in out:
            print(f"{d['file'] or '<command>'}: {d['code']}: {d['data'].get('function', '')} {d['message']}")
            if d['data'].get('approval_required'):
                print('  human approval required for configured tool dispatch')
            for e in d['data'].get('effects', []):
                print(f"  {e['operation']}: {e['status']} ({e['reason'] or e['capability'] or 'no policy capability'})")
                for check in e.get('checks', []):
                    if check['status'] == 'denied':
                        print(f"    denied by {check['scope']}: {check['identity'] or '<application>'}")
    return status
