"""Arithmetic and comparison rules (section 17).

1. A number with a decimal point is a float unless the other operand is
   decimal or money (then the literal is read exactly as a decimal).
2. Mixing float and decimal is an error; convert with decimal(x) or float(x).
3. Adding money in different currencies is an error.
4. `/` yields float between integers and decimal between decimals; `//` is
   integer division.
int is 64-bit and overflow is an error; it never wraps.
"""
from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from .errors import NexoError, Span
from .types import LitFloat, Money, Quantity, check_int, failure, plain, type_name
from .stdlib.clock import Date, Time, date_time_op

NUMERIC = (int, float, Decimal)


def is_num(v: Any) -> bool:
    return isinstance(v, NUMERIC) and not isinstance(v, bool)


def exact_literals(a: Any, b: Any) -> tuple[Any, Any]:
    """Section 17, rule 1: a float literal next to a decimal, money or unit
    quantity is read as an exact decimal."""
    exact = (Decimal, Money, Quantity)
    if type(a) is LitFloat and isinstance(b, exact):
        a = Decimal(a.text)
    if type(b) is LitFloat and isinstance(a, exact):
        b = Decimal(b.text)
    return plain(a), plain(b)


def bad(op: str, a: Any, b: Any, span: Span) -> NexoError:
    return NexoError("bad_operands", span, op=op, a=type_name(a), b=type_name(b))


def mix(a: Any, b: Any, span: Span) -> NexoError:
    return NexoError("mix_numbers", span, a=type_name(a), b=type_name(b))


def arith(op: str, a: Any, b: Any, span: Span, right_span: Span | None = None) -> Any:
    a, b = exact_literals(a, b)
    if isinstance(a, bool) or isinstance(b, bool):
        raise bad(op, a, b, span)
    if op == "+" and isinstance(a, str) and isinstance(b, str):
        return a + b
    if op == "+" and isinstance(a, list) and isinstance(b, list):
        return a + b
    if isinstance(a, (Date, Time)) or isinstance(b, (Date, Time)):
        return date_time_op(op, a, b, span)
    if isinstance(a, Money) or isinstance(b, Money):
        return money_op(op, a, b, span)
    if isinstance(a, Quantity) or isinstance(b, Quantity):
        return quantity_op(op, a, b, span)
    if not (is_num(a) and is_num(b)):
        raise bad(op, a, b, span)
    if isinstance(a, int) and isinstance(b, int):
        return int_op(op, a, b, span)
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        if isinstance(a, float) or isinstance(b, float):
            raise mix(a, b, span)
        return decimal_op(op, Decimal(a), Decimal(b), span)
    return float_op(op, float(a), float(b), span)


def int_op(op: str, a: int, b: int, span: Span) -> Any:
    if op == "+":
        return check_int(a + b, span)
    if op == "-":
        return check_int(a - b, span)
    if op == "*":
        return check_int(a * b, span)
    if op in ("/", "//", "%") and b == 0:
        raise failure("DivisionByZero", span)
    if op == "/":
        return a / b
    if op == "//":
        return a // b
    if op == "%":
        return a % b
    if op == "**":
        if b < 0:
            if a == 0:
                raise failure("DivisionByZero", span)
            return float(a) ** b
        if abs(a) > 1 and b > 64:
            raise failure("Overflow", span)
        return check_int(a**b, span)
    raise bad(op, a, b, span)


def float_op(op: str, a: float, b: float, span: Span) -> float:
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op in ("/", "//", "%") and b == 0:
            raise failure("DivisionByZero", span)
        if op == "/":
            return a / b
        if op == "//":
            return float(math.floor(a / b))
        if op == "%":
            return a % b
        if op == "**":
            if a == 0 and b < 0:
                raise failure("DivisionByZero", span)
            result = a**b
            if isinstance(result, complex):
                raise failure("InvalidValue", span, detail=f"{a} ** {b}")
            return result
    except OverflowError:
        raise failure("Overflow", span)
    raise bad(op, a, b, span)


def decimal_op(op: str, a: Decimal, b: Decimal, span: Span) -> Decimal:
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op in ("/", "//", "%") and b == 0:
            raise failure("DivisionByZero", span)
        if op == "/":
            return a / b
        if op == "//":
            return (a / b).to_integral_value(rounding=ROUND_FLOOR)
        if op == "%":
            return a - b * (a / b).to_integral_value(rounding=ROUND_FLOOR)
        if op == "**":
            return a**b
    except InvalidOperation:
        raise failure("InvalidValue", span, detail=f"{a} {op} {b}")
    raise bad(op, a, b, span)


def as_factor(v: Any, other: Any, span: Span, op: str) -> Decimal:
    """A plain number used to scale money: int or decimal (never float)."""
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, Quantity) and v.unit == "%":
        return v.fraction()
    if isinstance(v, float):
        raise mix(other, v, span)
    raise bad(op, other, v, span)


def money_op(op: str, a: Any, b: Any, span: Span) -> Any:
    if isinstance(a, Money) and isinstance(b, Money):
        if a.currency != b.currency:
            if op in ("+", "-", "/"):
                raise NexoError("currency_mismatch", span, a=a.currency, b=b.currency)
            raise bad(op, a, b, span)
        if op == "+":
            return Money.of(a.amount + b.amount, a.currency)
        if op == "-":
            return Money.of(a.amount - b.amount, a.currency)
        if op == "/":
            if b.amount == 0:
                raise failure("DivisionByZero", span)
            return a.amount / b.amount
        raise bad(op, a, b, span)
    if op == "*":
        money, other = (a, b) if isinstance(a, Money) else (b, a)
        return Money.of(money.amount * as_factor(other, money, span, op), money.currency)
    if op == "/" and isinstance(a, Money):
        factor = as_factor(b, a, span, op)
        if factor == 0:
            raise failure("DivisionByZero", span)
        return Money.of(a.amount / factor, a.currency)
    raise bad(op, a, b, span)


def scalar(v: Any) -> Decimal | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, Decimal)):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(repr(v))
    return None


def quantity_op(op: str, a: Any, b: Any, span: Span) -> Any:
    if isinstance(a, Quantity) and isinstance(b, Quantity):
        if a.dimension != b.dimension:
            if op in ("+", "-"):
                raise NexoError("unit_mismatch", span, a=a.unit, b=b.unit)
            if op == "*" and "percent" in (a.dimension, b.dimension):
                q, p = (a, b) if b.unit == "%" else (b, a)
                return Quantity(q.value * p.fraction(), q.unit)
            raise bad(op, a, b, span)
        if op in ("+", "-"):
            small = a if a.factor <= b.factor else b
            total = a.base() + b.base() if op == "+" else a.base() - b.base()
            return Quantity(normalize(total / small.factor), small.unit)
        if op == "/":
            if b.value == 0:
                raise failure("DivisionByZero", span)
            return a.base() / b.base()
        raise bad(op, a, b, span)
    q, other = (a, b) if isinstance(a, Quantity) else (b, a)
    if q.unit == "%" and op == "*":
        if isinstance(other, float):
            return other * float(q.fraction())
        s = scalar(other)
        if s is None:
            raise bad(op, a, b, span)
        return normalize(s * q.fraction())
    s = scalar(other)
    if s is None:
        raise bad(op, a, b, span)
    if op == "*":
        return Quantity(normalize(q.value * s), q.unit)
    if op == "/" and isinstance(a, Quantity):
        if s == 0:
            raise failure("DivisionByZero", span)
        return Quantity(normalize(a.value / s), a.unit)
    raise bad(op, a, b, span)


def normalize(d: Decimal) -> Decimal:
    if d == d.to_integral_value():
        return d.quantize(Decimal(1))
    return d.normalize()


# ---------------------------------------------------------- comparisons

def equals(a: Any, b: Any, span: Span) -> bool:
    a, b = exact_literals(a, b)
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if is_num(a) and is_num(b):
        if (isinstance(a, Decimal) and isinstance(b, float)) or (isinstance(a, float) and isinstance(b, Decimal)):
            raise mix(a, b, span)
        return a == b
    if isinstance(a, Quantity) and isinstance(b, Quantity):
        return a.dimension == b.dimension and a.base() == b.base()
    if type(a) is not type(b) and not (isinstance(a, list) and isinstance(b, list)):
        return False
    return a == b


def order(op: str, a: Any, b: Any, span: Span) -> bool:
    a, b = exact_literals(a, b)
    if is_num(a) and is_num(b):
        if (isinstance(a, Decimal) and isinstance(b, float)) or (isinstance(a, float) and isinstance(b, Decimal)):
            raise mix(a, b, span)
        x, y = a, b
    elif isinstance(a, str) and isinstance(b, str):
        x, y = a, b
    elif isinstance(a, Money) and isinstance(b, Money):
        if a.currency != b.currency:
            raise NexoError("currency_mismatch", span, a=a.currency, b=b.currency)
        x, y = a.amount, b.amount
    elif isinstance(a, Quantity) and isinstance(b, Quantity) and a.dimension == b.dimension:
        x, y = a.base(), b.base()
    elif isinstance(a, Date) and isinstance(b, Date):
        x, y = a.key(), b.key()
    elif isinstance(a, Time) and isinstance(b, Time):
        x, y = a.ms, b.ms
    else:
        raise NexoError("compare", span, a=type_name(a), b=type_name(b))
    if op == "<":
        return x < y
    if op == ">":
        return x > y
    if op == "<=":
        return x <= y
    return x >= y
