"""`use python module as name` (section 18, D111).

Phase 0 runs inside Python, so the module is imported into the same
interpreter; the compiler embeds the Python whose libpython it found when
it compiled (runtime/pylib.rs). Data is converted at the boundary: none,
bool, int, float, str, decimal, lists (and tuples), maps and sets become
the values of the other side, a record goes to Python as a dict, and any
other Python value stays a `python` value whose attributes and calls go
through the same conversion.
"""
from __future__ import annotations

import decimal
import importlib
from typing import Any

from ..errors import NexoError
from ..types import Builtin, Native, NexoSet, Record, failure, type_name

INT_MIN, INT_MAX = -(2 ** 63), 2 ** 63 - 1


class PyObject(Native):
    type_name = "python"
    __slots__ = ("obj",)

    def __init__(self, obj):
        self.obj = obj

    def repr(self) -> str:
        try:
            return repr(self.obj)
        except Exception:  # noqa: BLE001 - a broken __repr__
            return "python"

    def member(self, call, name: str):
        try:
            value = getattr(self.obj, name)
        except AttributeError:
            return self.missing(call, name, public_names(self.obj))
        except Exception as exc:  # noqa: BLE001 - a property that fails
            raise python_error(call, exc)
        return to_nexo(call, value)

    def call_value(self, call, args: list):
        positional = []
        named = {}
        for n, v in args:
            if n is None:
                positional.append(to_python(call, v))
            else:
                if n in named:
                    raise NexoError("arg_twice", call.span, param=n)
                named[n] = to_python(call, v)
        try:
            result = self.obj(*positional, **named)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 - any error of Python code
            raise python_error(call, exc)
        return to_nexo(call, result)


def public_names(obj) -> list[str]:
    try:
        return [n for n in dir(obj) if not n.startswith("_")]
    except Exception:  # noqa: BLE001
        return []


def python_error(call, exc: BaseException):
    return failure("Error", call.span, key="Error.python", type=type(exc).__name__, message=str(exc))


def to_python(call, value: Any) -> Any:
    value = call.interp.bind_value(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return decimal.Decimal(str(value))
    if isinstance(value, PyObject):
        return value.obj
    if isinstance(value, list):
        return [to_python(call, v) for v in value]
    if isinstance(value, dict):
        return {to_python(call, k): to_python(call, v) for k, v in value.items()}
    if isinstance(value, NexoSet):
        return {to_python(call, v) for v in value}
    if isinstance(value, Record):
        return {k: to_python(call, v) for k, v in value.fields.items()}
    raise NexoError("python_value", call.span, vtype=type_name(value))


def to_nexo(call, value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise failure("Overflow", call.span)
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, decimal.Decimal):
        return decimal.Decimal(str(value))
    # only the plain containers: a subclass (Counter...) keeps its methods
    if type(value) in (list, tuple):
        return [to_nexo(call, v) for v in value]
    if type(value) is dict:
        return {to_nexo(call, k): to_nexo(call, v) for k, v in value.items()}
    if type(value) in (set, frozenset):
        from .core import hashable
        out = NexoSet()
        for v in value:
            out.add(hashable(to_nexo(call, v), call.span))
        return out
    return PyObject(value)


def b_python_import(call, name):
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - ImportError or an error of the module
        raise NexoError("python_module", call.span, name=name, reason=f"{type(exc).__name__}: {exc}")
    return PyObject(module)


def exports() -> dict:
    return {"__python_import": Builtin("__python_import", b_python_import)}

