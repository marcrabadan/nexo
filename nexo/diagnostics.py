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
    paths = [a for a in args if a != "--json"]
    out = []
    status = 0
    if any(a.startswith("-") for a in paths) or args.count("--json") != 1:
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
    print(json.dumps({"schema_version": 1, "position_encoding": "utf-16",
                      "success": status == 0, "diagnostics": out}, ensure_ascii=True))
    return status
