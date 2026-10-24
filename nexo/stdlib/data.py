"""data: JSON and CSV (section 15).

A table is a list of rows; a row is a map from column name to value. CSV
cells that look like integers or decimal numbers are read as int or float,
and empty cells as none.
"""
from __future__ import annotations

import csv
import io
import json
import re
from decimal import Decimal
from typing import Any

from .. import ast as A
from .. import errors
from ..errors import NexoError
from ..types import (
    INT_MAX, INT_MIN, Builtin, BuiltinType, Money, Quantity, Record, RecordType, VariantValue, WeakRef,
    failure, repr_value, show, type_name,
)

INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


# ------------------------------------------------------------------ csv

def column_kind(cells: list[str]) -> str:
    """A column is int if every non-empty cell is an integer, float if every
    one is a number, and text otherwise."""
    values = [c for c in cells if c != ""]
    if values and all(INT_RE.match(c) and INT_MIN <= int(c) <= INT_MAX for c in values):
        return "int"
    if values and all(FLOAT_RE.match(c) for c in values):
        return "float"
    return "str"


def parse_csv(text: str) -> list[dict]:
    rows = [r for r in csv.reader(io.StringIO(text))]
    if not rows:
        return []
    header, body = rows[0], [r for r in rows[1:] if r]
    columns = [[r[i] if i < len(r) else "" for r in body] for i in range(len(header))]
    kinds = [column_kind(c) for c in columns]
    convert = {"int": int, "float": float, "str": str}
    table = []
    for raw in body:
        row = {}
        for i, name in enumerate(header):
            text = raw[i] if i < len(raw) else ""
            row[name] = None if text == "" else convert[kinds[i]](text)
        table.append(row)
    return table


def to_csv(call, value: Any) -> str:
    if not isinstance(value, list):
        raise NexoError("csv_value", call.span, vtype=type_name(value))
    rows = []
    columns: list[str] = []
    for item in value:
        if isinstance(item, Record):
            row = {k: (None if type(v) is WeakRef else v) for k, v in item.fields.items()}
        elif isinstance(item, dict):
            row = item
        else:
            raise NexoError("csv_value", call.span, vtype=type_name(item))
        for key in row:
            k = show(key)
            if k not in columns:
                columns.append(k)
        rows.append({show(k): v for k, v in row.items()})
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(c) is None else show(row.get(c)) for c in columns])
    return out.getvalue()


def b_csv_parse(call, text):
    if not isinstance(text, str):
        from .core import expects
        raise expects(call, "csv.parse", "str", text)
    return parse_csv(text)


# ----------------------------------------------------------------- json

def from_json(value: Any, span) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise failure("Overflow", span)
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, list):
        return [from_json(v, span) for v in value]
    if isinstance(value, dict):
        return {k: from_json(v, span) for k, v in value.items()}
    return value


def invalid(call, key: str, **args):
    return failure("InvalidData", call.span, detail=errors.msg(key, **args))


def as_type(call, value: Any, t: Any, type_expr: A.TypeExpr | None = None) -> Any:
    """Build a value of type `t` (a RecordType, BuiltinType...) from JSON data."""
    interp = call.interp
    if isinstance(t, RecordType):
        if not isinstance(value, dict):
            raise invalid(call, "detail.expected", type=t.name, vtype=type_name(value))
        fields = {}
        for f in t.fields:
            if f.name in value:
                raw = value[f.name]
                inner = field_type(interp, f.type, t.env)
                if inner is not None and raw is not None:
                    raw = as_type(call, raw, inner, f.type)
                try:
                    fields[f.name] = interp.field_store(f, interp.conform(raw, f.type, t.env, f.name, call.span))
                except NexoError:
                    raise invalid(call, "detail.field_type", field=f.name,
                                  type=interp.type_text(f.type), vtype=type_name(raw))
            elif f.default is not None:
                fields[f.name] = interp.bind_value(interp.eval(f.default, t.env))
            elif getattr(f.type, "optional", False):
                fields[f.name] = None
            else:
                raise invalid(call, "detail.missing_field", field=f.name, type=t.name)
        return Record(t, fields, interp.stamp)
    if isinstance(t, BuiltinType) and type_expr is not None and t.name == "list" and type_expr.params:
        if not isinstance(value, list):
            raise invalid(call, "detail.expected", type="list", vtype=type_name(value))
        inner = field_type(interp, type_expr.params[0], interp.globals)
        if inner is None:
            return value
        return [as_type(call, v, inner, type_expr.params[0]) for v in value]
    if isinstance(t, BuiltinType) and t.name in ("date", "time") and isinstance(value, str):
        from .clock import parse_date, parse_time
        parsed = parse_date(value) if t.name == "date" else parse_time(value)
        if parsed is None:
            raise invalid(call, "detail.expected", type=t.name, vtype="str")
        return parsed
    if isinstance(t, BuiltinType):
        if t.name == "float" and isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        if t.name == "decimal" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return Decimal(repr(value))
        if not interp.is_type(value, t, call.span):
            raise invalid(call, "detail.expected", type=t.name, vtype=type_name(value))
        return value
    return value


def field_type(interp, t: A.TypeExpr, env) -> Any:
    """The runtime type behind a field type when JSON needs converting."""
    if not isinstance(t, A.TypeName):
        return None
    if t.name == "list" and t.params:
        return BuiltinType("list")
    if t.name in ("float", "decimal", "date", "time"):
        return BuiltinType(t.name)
    kind = env.lookup(t.name) if env is not None else None
    if isinstance(kind, RecordType):
        return kind
    return None


def b_json_parse(call, text, as_=None):
    if not isinstance(text, str):
        from .core import expects
        raise expects(call, "json.parse", "str", text)
    try:
        value = from_json(json.loads(text), call.span)
    except json.JSONDecodeError as exc:
        raise invalid(call, "detail.json", line=exc.lineno, col=exc.colno)
    if as_ is None:
        return value
    if isinstance(as_, BuiltinType) and as_.name == "list":
        return as_type(call, value, as_)
    return as_type(call, value, as_)


def to_json(value: Any, level: int = 0) -> str:
    """JSON text of a value, indented with 2 spaces. Decimals keep all
    their digits; money and quantities are written as text ("19.99 EUR")."""
    pad = "  " * (level + 1)
    close = "  " * level
    if isinstance(value, Record):
        value = {k: (None if type(v) is WeakRef else v) for k, v in value.fields.items()}
    elif isinstance(value, VariantValue):
        value = {"variant": value.variant.name, **value.fields}
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f"{pad}{json.dumps(show(k), ensure_ascii=False)}: {to_json(v, level + 1)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + close + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[\n" + ",\n".join(pad + to_json(v, level + 1) for v in value) + "\n" + close + "]"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr_value(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(repr_value(value), ensure_ascii=False)


def json_exports() -> dict:
    return {"parse": Builtin("json.parse", b_json_parse, fallible=True)}


def csv_exports() -> dict:
    return {"parse": Builtin("csv.parse", b_csv_parse, fallible=True)}


def exports() -> dict:
    return {}
