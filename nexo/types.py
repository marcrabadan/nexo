"""Runtime values of the Nexo core.

Mapping to Python:
    int      -> int (64-bit range checked by the interpreter)
    float    -> float (a literal is a LitFloat until it is bound)
    decimal  -> decimal.Decimal, 28 significant digits
    money    -> Money
    bool     -> bool, str -> str, none -> None
    list     -> list, map -> dict, set -> NexoSet
"""
from __future__ import annotations

import decimal
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from . import errors
from .errors import NexoError, Span
from .lexer import CURRENCIES, LENGTH_UNITS, SIZE_UNITS, TIME_UNITS

DECIMAL_CONTEXT = decimal.Context(prec=28, rounding=decimal.ROUND_HALF_EVEN)
decimal.setcontext(DECIMAL_CONTEXT)

INT_MIN, INT_MAX = -(2**63), 2**63 - 1


class LitFloat(float):
    """A float literal that has not been bound yet. Section 17 rule 1: it
    becomes a decimal when the variable, parameter or other operand is a
    decimal or money, so its exact source text is kept."""

    text: str

    def __new__(cls, text: str):
        obj = super().__new__(cls, float(text))
        obj.text = text
        return obj


def plain(value: Any) -> Any:
    """Drop the literal tag: the value is now an ordinary float."""
    if type(value) is LitFloat:
        return float(value)
    return value


def to_decimal(value: Any) -> Decimal:
    if type(value) is LitFloat:
        return Decimal(value.text)
    if isinstance(value, bool):
        raise TypeError
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    raise TypeError


# ------------------------------------------------------------ money/units

@dataclass(frozen=True)
class Currency:
    code: str

    @property
    def places(self) -> int:
        return CURRENCIES.get(self.code, 2)


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    @staticmethod
    def of(amount: Decimal, currency: str) -> "Money":
        places = CURRENCIES.get(currency, 2)
        q = Decimal(1).scaleb(-places)
        return Money(amount.quantize(q, rounding=decimal.ROUND_HALF_EVEN), currency)


def unit_dimension(unit: str) -> tuple[str, Any]:
    if unit in TIME_UNITS:
        return "time", Decimal(str(TIME_UNITS[unit]))
    if unit in SIZE_UNITS:
        return "size", Decimal(SIZE_UNITS[unit])
    if unit in LENGTH_UNITS:
        return "length", Decimal(LENGTH_UNITS[unit])
    if unit == "%":
        return "percent", Decimal(1)
    return unit, Decimal(1)


@dataclass(frozen=True)
class Quantity:
    """A number with a unit: 30s, 5 km, 512 MB, 10%."""
    value: Decimal
    unit: str

    @property
    def dimension(self) -> str:
        return unit_dimension(self.unit)[0]

    @property
    def factor(self) -> Decimal:
        return unit_dimension(self.unit)[1]

    def base(self) -> Decimal:
        return self.value * self.factor

    def fraction(self) -> Decimal:
        """The value of a percentage as a fraction: 21% -> 0.21."""
        return self.value / 100


# ----------------------------------------------------------- collections

class NexoSet:
    """An insertion-ordered set."""

    def __init__(self, items=()):
        self.items: dict = {}
        for item in items:
            self.items[item] = True

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __contains__(self, item):
        return item in self.items

    def __eq__(self, other):
        return isinstance(other, NexoSet) and set(self.items) == set(other.items)

    def add(self, item):
        self.items[item] = True


@dataclass
class Range:
    start: int
    end: int  # inclusive

    def __iter__(self):
        return iter(range(self.start, self.end + 1))

    def __len__(self):
        return max(0, self.end - self.start + 1)

    def __contains__(self, item):
        return isinstance(item, (int, float, Decimal)) and not isinstance(item, bool) and self.start <= item <= self.end


class Lazy:
    """A lazy sequence produced by a pipeline or by read_lines. It reads
    nothing until a final step needs it (section 6, rule 4)."""

    def __init__(self, factory: Callable[[], Any]):
        self.factory = factory

    def __iter__(self):
        return iter(self.factory())


# ------------------------------------------------------------- user types

@dataclass(eq=False)
class TraitType:
    name: str
    methods: list[str]


@dataclass(eq=False)
class RecordType:
    name: str
    fields: list  # list[ast.FieldDef]
    methods: dict[str, Any] = field(default_factory=dict)
    traits: list[TraitType] = field(default_factory=list)
    env: Any = None


@dataclass(eq=False)
class Record:
    type: RecordType
    fields: dict[str, Any]

    def __eq__(self, other):
        return isinstance(other, Record) and other.type is self.type and other.fields == self.fields

    def __hash__(self):
        return id(self)


@dataclass(eq=False)
class EnumType:
    name: str
    variants: dict[str, "Variant"] = field(default_factory=dict)
    methods: dict[str, Any] = field(default_factory=dict)
    traits: list[TraitType] = field(default_factory=list)


@dataclass(eq=False)
class Variant:
    """A variant of an enum. Without fields it is also its own value."""
    name: str
    params: list  # list[ast.Param]
    enum: EnumType


@dataclass(eq=False)
class VariantValue:
    variant: Variant
    fields: dict[str, Any]

    def __eq__(self, other):
        return isinstance(other, VariantValue) and other.variant is self.variant and other.fields == self.fields

    def __hash__(self):
        return hash((id(self.variant), tuple(self.fields.items())))


@dataclass(eq=False)
class AliasType:
    """type Status = "active" | "closed"  (or an alias of another type)."""
    name: str
    target: Any  # ast.TypeExpr
    env: Any = None


@dataclass(frozen=True)
class BuiltinType:
    name: str  # int float decimal money str bool list map set


@dataclass(frozen=True)
class ErrorKind:
    """NotFound, ContractBroken, ... used in `else e is NotFound`."""
    name: str


@dataclass(frozen=True)
class Symbol:
    """A named constant such as half_up."""
    name: str


# ----------------------------------------------------------------- errors

ERROR_KINDS = ["Error", "NotFound", "IOError", "InvalidData", "Overflow", "DivisionByZero",
               "OutOfRange", "ContractBroken", "Timeout", "InvalidValue"]


@dataclass(eq=False)
class ErrorValue:
    """An error is a value (section 3): it carries a message, a cause and a line."""
    kind: str
    message: str
    cause: Any = None
    span: Span | None = None
    fix: str | None = None  # catalog key for the fix sentence

    @property
    def line(self) -> int | None:
        return self.span.line if self.span else None


class NexoFailure(Exception):
    """An error value travelling to the nearest `try` (or to the end of the
    program, where it is shown in plain language and the exit code is 1)."""

    def __init__(self, error: ErrorValue):
        super().__init__(error.message)
        self.error = error


def failure(kind: str, span: Span | None = None, cause: Any = None, **args) -> NexoFailure:
    message = errors.msg("err." + kind, **args)
    fix = "fix." + kind if errors.has("fix." + kind) else None
    return NexoFailure(ErrorValue(kind, message, cause, span, fix))


# ------------------------------------------------------------- callables

@dataclass(eq=False)
class Function:
    decl: Any  # ast.FnDef
    env: Any
    self_value: Any = None  # bound method receiver

    @property
    def name(self) -> str:
        return self.decl.name

    def bind(self, receiver: Any) -> "Function":
        return Function(self.decl, self.env, receiver)


@dataclass(eq=False)
class LambdaFn:
    node: Any  # ast.Lambda
    env: Any
    name: str = "lambda"


@dataclass(eq=False)
class Builtin:
    name: str
    fn: Callable
    fallible: bool = False
    raw: bool = False  # receives (interp, args, kwargs, node)


# ------------------------------------------------------------- formatting

def type_name(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, Money):
        return "money"
    if isinstance(value, Quantity):
        return "quantity"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "map"
    if isinstance(value, NexoSet):
        return "set"
    if isinstance(value, Range):
        return "range"
    if isinstance(value, Lazy):
        return "list"
    if isinstance(value, Record):
        return value.type.name
    if isinstance(value, VariantValue):
        return value.variant.enum.name
    if isinstance(value, Variant):
        return value.enum.name
    if isinstance(value, ErrorValue):
        return value.kind
    if isinstance(value, (Function, LambdaFn, Builtin)):
        return "fn"
    if isinstance(value, (RecordType, EnumType, AliasType, BuiltinType, TraitType, ErrorKind)):
        return "type"
    if isinstance(value, Currency):
        return "currency"
    return type(value).__name__


def format_float(value: float) -> str:
    if value != value:
        return "nan"
    if value in (float("inf"), float("-inf")):
        return "inf" if value > 0 else "-inf"
    return repr(float(value))


def format_quantity(q: Quantity) -> str:
    value = format_decimal(q.value)
    if q.unit in ("%", "s", "ms", "m", "h") :
        return f"{value}{q.unit}"
    return f"{value} {q.unit}"


def format_decimal(d: Decimal) -> str:
    if d == d.to_integral_value() and d.as_tuple().exponent > 0:
        return str(d.quantize(Decimal(1)))
    text = str(d)
    if "E" in text:
        text = format(d, "f")
    return text


def show(value: Any) -> str:
    """Text of a value as `print` and interpolation show it."""
    if isinstance(value, str):
        return value
    return repr_value(value)


def repr_value(value: Any) -> str:
    """Text of a value inside a collection (texts are quoted)."""
    if value is None:
        return "none"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format_float(value)
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, Money):
        return f"{value.amount} {value.currency}"
    if isinstance(value, Quantity):
        return format_quantity(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(value, (list, Lazy)):
        return "[" + ", ".join(repr_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{repr_value(k)}: {repr_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, NexoSet):
        return "set(" + ", ".join(repr_value(v) for v in value) + ")"
    if isinstance(value, Range):
        return f"{value.start}..{value.end}"
    if isinstance(value, Record):
        inner = ", ".join(f"{k}: {repr_value(v)}" for k, v in value.fields.items())
        return f"{value.type.name}({inner})"
    if isinstance(value, VariantValue):
        inner = ", ".join(f"{k}: {repr_value(v)}" for k, v in value.fields.items())
        return f"{value.variant.name}({inner})"
    if isinstance(value, Variant):
        return value.name
    if isinstance(value, ErrorValue):
        return f"{value.kind}({repr_value(value.message)})"
    if isinstance(value, (Function, LambdaFn, Builtin)):
        return f"fn {value.name}"
    if isinstance(value, (RecordType, EnumType, AliasType, BuiltinType, TraitType, ErrorKind)):
        return value.name
    if isinstance(value, Currency):
        return value.code
    if isinstance(value, Symbol):
        return value.name
    return str(value)


def check_int(value: int, span: Span | None) -> int:
    if not INT_MIN <= value <= INT_MAX:
        raise failure("Overflow", span)
    return value


def hashable(value: Any, span: Span | None) -> Any:
    if isinstance(value, (list, dict, NexoSet, Lazy)):
        raise NexoError("unhashable", span, vtype=type_name(value))
    return plain(value)
