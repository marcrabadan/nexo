"""Core builtins: print, input, len, range, set, round, split, convert,
the conversions decimal(), float(), int() and str(), fail(), validate(),
and the names of types, error kinds, currencies and constants."""
from __future__ import annotations

import decimal
import math
import re
import sys
from decimal import Decimal
from typing import Any

from .. import errors
from ..errors import NexoError
from ..lexer import CURRENCIES
from ..types import (
    ERROR_KINDS, Builtin, BuiltinType, Currency, ErrorKind, ErrorValue, Lazy, LitFloat, Money,
    NexoFailure, NexoSet, Quantity, Range, RecordType, Symbol, copy_collection, failure, hashable, plain, show,
    to_decimal, type_name,
)

HALF_UP = Symbol("half_up")
HALF_EVEN = Symbol("half_even")


def bad_arg(call, fn: str, key: str, **args) -> NexoError:
    return NexoError("bad_argument", call.span, fn=fn, detail=errors.msg(key, **args))


def expects(call, fn: str, what: str, value: Any) -> NexoError:
    return bad_arg(call, fn, "detail.expects", what=what, vtype=type_name(value))


# ------------------------------------------------------------------- io

def b_print(call, *values, end="\n"):
    interp = call.interp
    text = " ".join(show(interp.bind_value(v)) for v in values)
    interp.write(text + show(end))
    return None


def b_input(call, prompt=""):
    interp = call.interp
    interp.write(show(prompt))
    try:
        interp.out.flush()
    except Exception:
        pass
    line = sys.stdin.readline()
    return line.rstrip("\r\n")


# ---------------------------------------------------------- collections

def b_len(call, value):
    value = call.interp.bind_value(value) if isinstance(value, Lazy) else value
    if isinstance(value, (str, list, dict, NexoSet, Range)):
        return len(value)
    raise expects(call, "len", "str, list, map, set", value)


def b_range(call, start, end=None, step=1):
    for v in (start, end, step):
        if v is not None and (not isinstance(v, int) or isinstance(v, bool)):
            raise expects(call, "range", "int", v)
    if end is None:
        start, end = 0, start
    if step == 0:
        raise bad_arg(call, "range", "detail.step_zero")
    return Lazy(lambda: iter(range(start, end, step)))


def b_set(call, *items):
    return NexoSet(hashable(i, call.span) for i in items)


# -------------------------------------------------------------- numbers

def b_round(call, value, digits=0, mode=HALF_EVEN):
    if mode not in (HALF_EVEN, HALF_UP):
        raise bad_arg(call, "round", "detail.mode")
    if not isinstance(digits, int) or isinstance(digits, bool):
        raise expects(call, "round", "int", digits)
    rounding = decimal.ROUND_HALF_UP if mode is HALF_UP else decimal.ROUND_HALF_EVEN
    quantum = Decimal(1).scaleb(-digits)
    if isinstance(value, bool):
        raise expects(call, "round", "int, float, decimal, money", value)
    if isinstance(value, int):
        return value
    if type(value) is LitFloat:
        value = float(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return value
        if mode is HALF_UP:
            return float(Decimal(repr(value)).quantize(quantum, rounding=rounding))
        return float(round(value, digits))
    if isinstance(value, Decimal):
        return value.quantize(quantum, rounding=rounding)
    if isinstance(value, Money):
        return Money.of(value.amount.quantize(quantum, rounding=rounding), value.currency)
    raise expects(call, "round", "int, float, decimal, money", value)


def b_split(call, amount, parts):
    """Split money without losing cents: the remainder goes to the first parts."""
    if not isinstance(amount, Money):
        raise expects(call, "split", "money", amount)
    if not isinstance(parts, int) or isinstance(parts, bool):
        raise expects(call, "split", "int", parts)
    if parts <= 0:
        raise bad_arg(call, "split", "detail.positive")
    places = CURRENCIES.get(amount.currency, 2)
    units = int(amount.amount.scaleb(places))
    base, extra = divmod(units, parts)
    out = []
    for i in range(parts):
        share = Decimal(base + (1 if i < extra else 0)).scaleb(-places)
        out.append(Money.of(share, amount.currency))
    return out


def b_convert(call, value, to, rate):
    if not isinstance(value, Money):
        raise expects(call, "convert", "money", value)
    if not isinstance(to, Currency):
        raise expects(call, "convert", "currency", to)
    try:
        factor = to_decimal(rate)
    except TypeError:
        raise expects(call, "convert", "decimal", rate)
    return Money.of(value.amount * factor, to.code)


def conv_decimal(call, value):
    if isinstance(value, Money):
        return value.amount
    if isinstance(value, str):
        raise bad_arg(call, "decimal", "detail.use_validate", type="decimal")
    try:
        return to_decimal(value)
    except TypeError:
        raise expects(call, "decimal", "int, float", value)


def conv_float(call, value):
    if isinstance(value, bool) or isinstance(value, str):
        if isinstance(value, str):
            raise bad_arg(call, "float", "detail.use_validate", type="float")
        raise expects(call, "float", "int, decimal", value)
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, Money):
        return float(value.amount)
    raise expects(call, "float", "int, decimal", value)


def conv_int(call, value):
    if isinstance(value, str):
        raise bad_arg(call, "int", "detail.use_validate", type="int")
    if isinstance(value, bool):
        raise expects(call, "int", "float, decimal", value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise failure("InvalidValue", call.span, detail=repr(value))
        return int(value)
    if isinstance(value, Decimal):
        return int(value)
    raise expects(call, "int", "float, decimal", value)


def conv_str(call, value):
    return show(call.interp.bind_value(value))


# --------------------------------------------------------------- errors

def b_fail(call, message):
    """fail(...) creates a custom error (section 3, rule 5)."""
    if isinstance(message, ErrorValue):
        raise NexoFailure(message)
    raise NexoFailure(ErrorValue("Error", show(call.interp.bind_value(message)), None, call.span))


INT_RE = re.compile(r"^[+-]?\d+$")
# an address with one @, a name before it and a domain with a dot after it
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def b_validate(call, value, as_, range=None):
    """validate(value, as: type, range: a..b)? turns external text into a
    checked value (section 9)."""
    interp = call.interp
    value = interp.bind_value(value)
    target = as_
    name = getattr(target, "name", type_name(target))

    def invalid():
        return failure("InvalidValue", call.span, detail=errors.msg("detail.not_a", value=show(value), type=name))

    result = value
    if isinstance(target, BuiltinType) and isinstance(value, str):
        text = value.strip()
        if target.name == "int":
            if not INT_RE.match(text):
                raise invalid()
            result = int(text)
            if not -(2**63) <= result <= 2**63 - 1:
                raise failure("Overflow", call.span)
        elif target.name == "float":
            if not FLOAT_RE.match(text):
                raise invalid()
            result = float(text)
        elif target.name == "decimal":
            if not FLOAT_RE.match(text):
                raise invalid()
            result = Decimal(text)
        elif target.name == "bool":
            if text not in ("true", "false"):
                raise invalid()
            result = text == "true"
        elif target.name == "str":
            result = value
        elif target.name == "email":
            if not EMAIL_RE.match(text):
                raise invalid()
            result = text
        elif target.name in ("date", "time"):
            from .clock import parse_date, parse_time
            result = parse_date(text) if target.name == "date" else parse_time(text)
            if result is None:
                raise invalid()
        else:
            raise invalid()
    elif isinstance(target, (BuiltinType, RecordType)) or hasattr(target, "name"):
        if not interp.is_type(value, target, call.span):
            raise invalid()
    else:
        raise expects(call, "validate", "type", target)
    if range is not None:
        if not isinstance(range, Range):
            raise expects(call, "validate", "range", range)
        if not (isinstance(result, (int, float, Decimal)) and not isinstance(result, bool)) or result not in range:
            raise failure("InvalidValue", call.span,
                          detail=errors.msg("detail.out_of_range", value=show(result), range=f"{range.start}..{range.end}"))
    return result


def exports() -> dict:
    names = {
        "print": Builtin("print", b_print),
        "input": Builtin("input", b_input),
        "len": Builtin("len", b_len),
        "range": Builtin("range", b_range),
        "set": Builtin("set", b_set),
        "round": Builtin("round", b_round),
        "split": Builtin("split", b_split),
        "convert": Builtin("convert", b_convert),
        "fail": Builtin("fail", b_fail),
        "validate": Builtin("validate", b_validate, fallible=True),
        "__convert_decimal": Builtin("decimal", conv_decimal),
        "__convert_float": Builtin("float", conv_float),
        "__convert_int": Builtin("int", conv_int),
        "__convert_str": Builtin("str", conv_str),
        "PI": math.pi,
        "half_up": HALF_UP,
        "half_even": HALF_EVEN,
    }
    for t in ("int", "float", "decimal", "money", "str", "bool", "list", "map"):
        names[t] = BuiltinType(t)
    names["email"] = BuiltinType("email")
    for kind in ERROR_KINDS:
        names[kind] = ErrorKind(kind)
    for code in CURRENCIES:
        names[code] = Currency(code)
    return names


def _text():
    from . import text
    return text


# ---------------------------------------------------- methods of values

def m_sum(call, obj):
    return call.interp.total([plain(v) for v in obj], call.span)


def m_count(call, obj):
    return len(list(obj))


def m_add(call, obj, item):
    if isinstance(obj, Lazy):
        raise expects(call, "add", "list", obj)
    if isinstance(obj, NexoSet):
        obj.add(hashable(item, call.span))
    else:
        value = call.interp.store_value(item)
        # a list stored in itself is stored as it was before
        obj.append(copy_collection(value) if value is obj else value)
    return None


def text_method(name, fn, arity):
    def method(call, obj, *args):
        for a in args:
            if not isinstance(a, str):
                raise expects(call, name, "str", a)
        return fn(obj, *args)
    return method


def m_split(call, obj, sep=None):
    if sep is not None and (not isinstance(sep, str) or sep == ""):
        raise expects(call, "split", "str", sep)
    return obj.split(sep) if sep is not None else obj.split()


METHODS = {
    "list": {"sum": m_sum, "count": m_count, "add": m_add},
    "set": {"add": m_add, "count": m_count},
    "map": {
        "keys": lambda call, obj: list(obj.keys()),
        "values": lambda call, obj: list(obj.values()),
        "count": m_count,
    },
    "str": {
        "upper": lambda call, obj: obj.upper(),
        "lower": lambda call, obj: obj.lower(),
        "trim": lambda call, obj: obj.strip(),
        "split": m_split,
        "replace": text_method("replace", lambda s, a, b: s.replace(a, b), 2),
        "starts_with": text_method("starts_with", lambda s, p: s.startswith(p), 1),
        "ends_with": text_method("ends_with", lambda s, p: s.endswith(p), 1),
        "find": lambda call, obj, text: _text().m_find(call, obj, text),
    },
}
