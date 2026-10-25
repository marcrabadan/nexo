"""Feature flags (section 22, D115): the `flags:` block declares them and
`flag(name, user)` reads them. A value set with `nexo flags set` lives in
`.nexo/flags.json` (the store when there is no infrastructure) and is read
again whenever the file changes, so the program sees it at once."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from .. import errors
from ..errors import NexoError
from ..types import Builtin, Quantity, Record, repr_value, show
from .cfg import Cfg, Cond, holds

STORE = Path(".nexo") / "flags.json"
AUDIT = Path(".nexo") / "audit.log"


class Flag:
    __slots__ = ("name", "kind", "value", "expires", "span")

    def __init__(self, name, kind, value, expires, span):
        # kind: "on" | "off" | "percent" (value: Decimal) | "audience" (value: Cond)
        self.name, self.kind, self.value, self.expires, self.span = name, kind, value, expires, span


FLAGS: dict[str, Flag] = {}
_store: dict = {"mtime": None, "data": {}}


def reset() -> None:
    FLAGS.clear()
    _store["mtime"] = None
    _store["data"] = {}


def parse_value(entry: Cfg, name: str):
    """(kind, value) of the value of a flag."""
    v = entry.value()
    if v in ("on", "off"):
        return v, None
    if isinstance(v, Quantity) and v.unit == "%" and 0 <= v.value <= 100:
        return "percent", Decimal(v.value)
    # users where condition
    if isinstance(v, list) and len(v) == 3 and v[0] == "users" and v[1] == "where" and isinstance(v[2], Cond):
        return "audience", v[2]
    raise entry.error("flag_value", name=name)


def b_flags(call, entries):
    for entry in entries:
        name = entry.key()
        if name is None or entry.body is not None or len(entry.values) != 1:
            raise entry.error("flag_entry")
        if name in FLAGS:
            raise NexoError("duplicate_name", entry.span, name=name)
        kind, value = parse_value(entry, name)
        expires = None
        for option, v in entry.options.items():
            if option in ("watch", "rollback"):
                raise NexoError("not_supported", entry.span, what=f'"{option}"')
            if option != "expires":
                raise NexoError("flag_option", entry.span, option=option)
            from .clock import Date
            if not isinstance(v, Date):
                from .core import expects
                raise expects(entry, "expires", "date", v)
            expires = v
        FLAGS[name] = Flag(name, kind, value, expires, entry.span)
    return None


def stage() -> str:
    return os.environ.get("NEXO_STAGE") or "default"


def stored() -> dict:
    """The values set with `nexo flags set` for this stage."""
    try:
        mtime = STORE.stat().st_mtime_ns
    except OSError:
        return {}
    if mtime != _store["mtime"]:
        try:
            data = json.loads(STORE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _store["mtime"], _store["data"] = mtime, data if isinstance(data, dict) else {}
    values = _store["data"].get(stage(), {})
    return values if isinstance(values, dict) else {}


def text_value(text: str):
    """on, off or a percentage written on the command line (or in the store)."""
    if text in ("on", "off"):
        return text, None
    if text.endswith("%"):
        try:
            d = Decimal(text[:-1])
        except ArithmeticError:
            return None
        if d.is_finite() and 0 <= d <= 100 and text[:-1].replace(".", "", 1).isdigit():
            return "percent", d
    return None


def user_key(user: Any) -> str:
    if isinstance(user, Record) and "id" in user.fields:
        return show(user.fields["id"])
    if isinstance(user, (str, int)) and not isinstance(user, bool):
        return show(user)
    return repr_value(user)


def bucket(name: str, key: str) -> int:
    """0..9999, the same for the same flag and user on every machine."""
    digest = hashlib.sha256(f"{name}:{key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 10000


def b_flag(call, name, user=None):
    from .core import expects
    if not isinstance(name, str):
        raise expects(call, "name", "str", name)
    f = FLAGS.get(name)
    if f is None:
        s = errors.suggest(name, list(FLAGS))
        if s:
            raise NexoError("flag_unknown", call.span, fix="suggest", name=name, suggestion=s)
        raise NexoError("flag_unknown", call.span, name=name)
    kind, value = f.kind, f.value
    live = stored().get(name)
    if isinstance(live, str) and text_value(live) is not None:
        kind, value = text_value(live)
    if kind in ("on", "off"):
        return kind == "on"
    if user is None:
        raise NexoError("flag_needs_user", call.span, name=name)
    if kind == "percent":
        return bucket(name, user_key(user)) < int(value * 100)
    return holds(call.interp, value, user, call.span)


# ------------------------------------------------------------ nexo flags set

def declared(program) -> list[str]:
    """The names the flags blocks of a program declare (only the parser
    is needed)."""
    from .. import ast as A
    out = []
    for s in program.body:
        if (isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call) and isinstance(s.expr.func, A.Name)
                and s.expr.func.name == "__flags"):
            for entry in s.expr.args[0].value.items:
                head = entry.args[0].value.items[0].items
                if entry.args[1].value is not None and len(head) == 1 and isinstance(head[0], A.Str):
                    out.append("".join(p for p in head[0].parts if isinstance(p, str)))
    return out


def now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(action: dict) -> None:
    """Append a line to .nexo/audit.log; each line holds the hash of the
    previous one, so a changed or removed line shows."""
    prev = ""
    try:
        lines = AUDIT.read_text(encoding="utf-8").splitlines()
        if lines:
            prev = hashlib.sha256(lines[-1].encode("utf-8")).hexdigest()
    except OSError:
        pass
    entry = {"time": now_text(), "by": os.environ.get("USER") or "unknown", **action, "prev": prev}
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def set_command(names: list[str], name: str, value: str, stage_name: str | None) -> int:
    import sys
    if name not in names:
        s = errors.suggest(name, names)
        e = NexoError("flag_unknown", None, fix="suggest" if s else None, name=name, suggestion=s)
        sys.stderr.write(errors.render(None, e.text()))
        return 1
    if text_value(value) is None:
        sys.stderr.write(errors.render(None, NexoError("flag_set_value", None, value=value).text()))
        return 1
    st = stage_name or "default"
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data.setdefault(st, {})[name] = value
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    audit({"action": "flags set", "flag": name, "value": value, "stage": st})
    print(errors.msg("info.flags_set", name=name, value=value, stage=st))
    return 0


def exports() -> dict:
    return {
        "__flags": Builtin("__flags", b_flags),
        "flag": Builtin("flag", b_flag),
    }
