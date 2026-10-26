"""io: read, write, read_lines, save (section 15). All of them can fail
(T!): a missing file is a NotFound error, anything else an IOError."""
from __future__ import annotations

import os
from typing import Any

from .. import errors
from ..types import Builtin, Lazy, failure, show
from . import data


def check_path(call, path: Any, fn: str) -> str:
    if not isinstance(path, str):
        from .core import expects
        raise expects(call, fn, "str", path)
    return path


def os_failure(call, path: str, exc: OSError):
    if isinstance(exc, FileNotFoundError):
        return failure("NotFound", call.span, cause=exc.strerror, path=path)
    return failure("IOError", call.span, cause=exc.strerror, path=path, reason=exc.strerror or str(exc))


def read_text(call, path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        raise failure("InvalidData", call.span, detail=errors.msg("detail.encoding", path=path))
    except OSError as exc:
        raise os_failure(call, path, exc)


def b_read(call, path):
    """Text of a file; a .csv file is read as a table (a list of rows)."""
    path = check_path(call, path, "read")
    text = read_text(call, path)
    if path.lower().endswith(".csv"):
        return data.parse_csv(text)
    return text


def b_write(call, path, content):
    path = check_path(call, path, "write")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(show(call.interp.bind_value(content)))
    except OSError as exc:
        raise os_failure(call, path, exc)
    return None


def b_read_lines(call, path):
    """The lines of a file, read lazily one by one."""
    path = check_path(call, path, "read_lines")
    try:
        f = open(path, encoding="utf-8")
    except OSError as exc:
        raise os_failure(call, path, exc)
    f.close()

    def lines():
        with open(path, encoding="utf-8") as fh:
            try:
                for line in fh:
                    yield line.rstrip("\r\n")
            except UnicodeDecodeError:
                raise failure("InvalidData", call.span, detail=errors.msg("detail.encoding", path=path))
    return Lazy(lines)


def b_save(call, value, path):
    """Final pipeline step: .csv writes a table, .json writes JSON, any
    other extension writes text (one line per element of a list)."""
    path = check_path(call, path, "save")
    interp = call.interp
    value = interp.bind_value(value)
    lower = path.lower()
    if lower.endswith(".csv"):
        text = data.to_csv(call, value)
    elif lower.endswith(".json"):
        text = data.to_json(value) + "\n"
    elif isinstance(value, list):
        text = "".join(show(v) + "\n" for v in value)
    else:
        text = show(value)
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError as exc:
        raise os_failure(call, path, exc)
    return None


def b_move_file(call, path, to):
    """Move a file into a folder (created if needed); returns its new path."""
    import shutil
    path = check_path(call, path, "move_file")
    folder = check_path(call, to, "move_file")
    if not os.path.isfile(path):
        if not os.path.exists(path):
            raise failure("NotFound", call.span, path=path)
        raise failure("IOError", call.span, path=path, reason=errors.msg("detail.not_a_file"))
    target = os.path.join(folder, os.path.basename(path))
    try:
        os.makedirs(folder, exist_ok=True)
        shutil.move(path, target)
    except OSError as exc:
        raise os_failure(call, target, exc)
    return target


def exports() -> dict:
    return {
        "read": Builtin("read", b_read, fallible=True),
        "write": Builtin("write", b_write, fallible=True),
        "read_lines": Builtin("read_lines", b_read_lines, fallible=True),
        "save": Builtin("save", b_save, fallible=True),
        "move_file": Builtin("move_file", b_move_file, fallible=True),
    }
