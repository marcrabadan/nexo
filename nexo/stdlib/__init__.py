"""Standard library available without `use` (section 15): io, data, and
the core builtins. Official modules (nexo.ai, nexo.db...) are not part of
phase 0."""
from __future__ import annotations

import inspect
import keyword
from typing import Any

from ..errors import NexoError, Span, suggest
from ..types import At, Builtin, Lazy, Native, NexoSet, plain, type_name
from . import clock, core, data, io, text  # noqa: E402


class Namespace:
    """A builtin namespace such as `json` or `csv`."""

    def __init__(self, name: str, attrs: dict[str, Any]):
        self.name = name
        self.attrs = attrs

    def __repr__(self) -> str:
        return self.name


def install(interp) -> None:
    from . import ai, cfg, ci, clib, crypto, db, flags, http, infra, privacy, pylib, stream, web
    web.reset()
    ai.reset()
    flags.reset()
    privacy.reset()
    stream.reset()
    infra.reset()
    ci.reset()
    env = interp.builtins
    for module in (core, io, data, clock, text, crypto, http, db, web, ai, clib, pylib, cfg, flags, privacy, stream, infra, ci):
        for name, value in module.exports().items():
            env.define(name, value)
    env.define("json", Namespace("json", data.json_exports()))
    env.define("csv", Namespace("csv", data.csv_exports()))


def members(interp, obj: Any, name: str, span: Span) -> Any:
    """Methods of builtin values: list.sum(), text.upper(), ..."""
    if isinstance(obj, Native):
        return obj.member(At(interp, span), name)
    if isinstance(obj, Namespace):
        if name in obj.attrs:
            return obj.attrs[name]
        return _missing(obj.name, name, span, list(obj.attrs))
    if type(obj).__name__ == "Channel":
        from . import sync
        table = sync.METHODS
    else:
        table = core.METHODS.get(_kind(obj))
    if table is None or name not in table:
        return _missing(type_name(obj), name, span, list(table or {}))
    fn = table[name]
    return Builtin(name, lambda call, *a, **k: fn(call, obj, *a, **k))


def _kind(obj: Any) -> str | None:
    if isinstance(obj, (list, Lazy)):
        return "list"
    if isinstance(obj, str):
        return "str"
    if isinstance(obj, dict):
        return "map"
    if isinstance(obj, NexoSet):
        return "set"
    return None


def _missing(vtype: str, name: str, span: Span, candidates: list[str]):
    s = suggest(name, candidates)
    if s:
        raise NexoError("no_member", span, fix="suggest", vtype=vtype, name=name, suggestion=s)
    raise NexoError("no_member", span, vtype=vtype, name=name)


_SIGNATURES: dict = {}


def check_arguments(fn: Builtin, positional: list, named: dict, span: Span) -> dict:
    """Validate a builtin call against its Python signature and return the
    named arguments renamed for Python (`as` -> `as_`)."""
    named = {(k + "_" if keyword.iskeyword(k) else k): v for k, v in named.items()}
    sig = _SIGNATURES.get(fn.fn)
    if sig is None:
        sig = _SIGNATURES[fn.fn] = inspect.signature(fn.fn)
    try:
        sig.bind(None, *positional, **named)
        return named
    except TypeError:
        pass
    params = list(sig.parameters.values())[1:]
    names = [p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    shown = [n.rstrip("_") for n in names]
    if not any(p.kind == p.VAR_KEYWORD for p in params):
        for key in named:
            if key not in names:
                s = suggest(key.rstrip("_"), shown)
                if s:
                    raise NexoError("arg_unknown", span, fix="suggest", fn=fn.name, param=key.rstrip("_"), suggestion=s)
                raise NexoError("arg_unknown", span, fn=fn.name, param=key.rstrip("_"), params=", ".join(shown) or "-")
    if not any(p.kind == p.VAR_POSITIONAL for p in params):
        limit = len([p for p in params if p.kind == p.POSITIONAL_OR_KEYWORD])
        if len(positional) > limit:
            raise NexoError("arg_too_many", span, fn=fn.name, n=limit, m=len(positional))
    for i, p in enumerate(params):
        if p.default is p.empty and p.kind == p.POSITIONAL_OR_KEYWORD:
            if i >= len(positional) and p.name not in named:
                raise NexoError("arg_missing", span, param=p.name.rstrip("_"), fn=fn.name)
    raise NexoError("arg_too_many", span, fn=fn.name, n=len(params), m=len(positional) + len(named))
