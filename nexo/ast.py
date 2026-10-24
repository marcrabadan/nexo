"""Typed AST for the Nexo core. Every node carries the span of its source."""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Span


@dataclass
class Node:
    span: Span


# ---------------------------------------------------------------- types

@dataclass
class TypeName(Node):
    name: str  # dotted name, e.g. "list" or "nexo.Thing"
    params: list["TypeExpr"] = field(default_factory=list)
    optional: bool = False  # T?
    fallible: bool = False  # T!
    weak: bool = False
    rel: str | None = None  # `ref T` or `many T` in a @table type (nexo.db)


@dataclass
class LiteralUnion(Node):
    literals: list["Expr"]  # "active" | "closed"


@dataclass
class RangeType(Node):
    """`score: 1..5`: an int between the two bounds (section 8)."""
    low: int
    high: int


TypeExpr = TypeName | LiteralUnion | RangeType


# ---------------------------------------------------------- expressions

@dataclass
class Expr(Node):
    pass


@dataclass
class Num(Expr):
    text: str
    is_float: bool


@dataclass
class UnitLit(Expr):
    text: str
    is_float: bool
    unit: str


@dataclass
class Str(Expr):
    parts: list  # str | Expr


@dataclass
class Bool(Expr):
    value: bool


@dataclass
class NoneLit(Expr):
    pass


@dataclass
class Name(Expr):
    name: str


@dataclass
class ListLit(Expr):
    items: list[Expr]


@dataclass
class MapLit(Expr):
    pairs: list[tuple[Expr, Expr]]


@dataclass
class Comprehension(Expr):
    element: Expr
    var: str
    iterable: Expr
    condition: Expr | None


@dataclass
class Attr(Expr):
    obj: Expr
    name: str
    name_span: Span


@dataclass
class Index(Expr):
    obj: Expr
    index: Expr


@dataclass
class Arg(Node):
    name: str | None
    value: Expr


@dataclass
class Call(Expr):
    func: Expr
    args: list[Arg]


@dataclass
class Propagate(Expr):
    """Postfix `?`: pass the error to the caller."""
    expr: Expr


@dataclass
class Unary(Expr):
    op: str  # "-" or "not"
    operand: Expr


@dataclass
class BinOp(Expr):
    op: str  # + - * / // % **
    left: Expr
    right: Expr


@dataclass
class BoolOp(Expr):
    op: str  # and / or
    left: Expr
    right: Expr


@dataclass
class Compare(Expr):
    ops: list[str]  # == != < > <= >= in is
    operands: list[Expr]


@dataclass
class Coalesce(Expr):
    left: Expr
    right: Expr


@dataclass
class RangeExpr(Expr):
    start: Expr
    end: Expr


@dataclass
class Lambda(Expr):
    param: str
    body: Expr


@dataclass
class PipeStep(Node):
    """One step after `|>`.

    kind is one of: where map pick group sum count avg sort take each call.
    For "call", `expr` is the function (a name, attribute or call).
    """
    kind: str
    expr: Expr | None = None
    columns: list[Expr] = field(default_factory=list)  # pick
    desc: bool = False  # sort by ... desc


@dataclass
class Pipeline(Expr):
    source: Expr
    steps: list[PipeStep]


@dataclass
class ParallelFor(Expr):
    var: str
    iterable: Expr
    args: list[Arg]
    body: list["Stmt"]


# ----------------------------------------------------------- statements

@dataclass
class Stmt(Node):
    pass


@dataclass
class ExprStmt(Stmt):
    expr: Expr


@dataclass
class Assign(Stmt):
    target: Expr  # Name | Attr | Index
    op: str  # = += -= *= /=
    value: Expr
    type: TypeExpr | None = None


@dataclass
class VarDecl(Stmt):
    name: str
    name_span: Span
    type: TypeExpr | None
    value: Expr


@dataclass
class Return(Stmt):
    value: Expr | None


@dataclass
class Pass(Stmt):
    pass


@dataclass
class Use(Stmt):
    lang: str | None
    name: str
    alias: str | None


# nexo.sync (section 5)

@dataclass
class Spawn(Stmt):
    call: "Call"


@dataclass
class SelectArm(Node):
    var: str | None  # msg in `msg from input:`; None for `after`
    channel: "Expr | None"
    after: "Expr | None"
    body: list[Stmt]


@dataclass
class Select(Stmt):
    arms: list[SelectArm]


@dataclass
class SharedDecl(Stmt):
    name: str
    name_span: Span
    value: "Expr"


@dataclass
class Emit(Stmt):
    call: Call


@dataclass
class Expect(Stmt):
    expr: Expr


@dataclass
class Ensure(Stmt):
    expr: Expr


@dataclass
class Assert(Stmt):
    expr: Expr
    fails: bool


@dataclass
class Annotation(Node):
    name: str
    args: list[Arg]


@dataclass
class Param(Node):
    name: str
    type: TypeExpr | None
    default: Expr | None


@dataclass
class FnDef(Stmt):
    name: str
    name_span: Span
    params: list[Param]
    ret: TypeExpr | None
    body: list[Stmt]
    pub: bool = False
    annotations: list[Annotation] = field(default_factory=list)


@dataclass
class FieldDef(Node):
    name: str
    type: TypeExpr
    default: Expr | None
    marks: list[str] = field(default_factory=list)  # @unique, @private, @personal


@dataclass
class VariantDef(Node):
    name: str
    params: list[Param]


@dataclass
class TypeDef(Stmt):
    name: str
    name_span: Span
    alias: TypeExpr | None = None  # type Status = "a" | "b"
    trait: str | None = None  # type Circle is Shape
    fields: list[FieldDef] = field(default_factory=list)
    variants: list[VariantDef] = field(default_factory=list)
    methods: list[FnDef] = field(default_factory=list)
    settings: list[tuple[str, Expr]] = field(default_factory=list)
    pub: bool = False
    annotations: list[Annotation] = field(default_factory=list)


@dataclass
class FnSig(Node):
    name: str
    params: list[Param]
    ret: TypeExpr | None


@dataclass
class TraitDef(Stmt):
    name: str
    sigs: list[FnSig]


@dataclass
class If(Stmt):
    branches: list[tuple[Expr, list[Stmt]]]
    orelse: list[Stmt] | None


@dataclass
class For(Stmt):
    var: str
    iterable: Expr
    body: list[Stmt]


@dataclass
class While(Stmt):
    cond: Expr
    body: list[Stmt]


@dataclass
class Repeat(Stmt):
    count: Expr
    body: list[Stmt]


@dataclass
class Pattern(Node):
    pass


@dataclass
class PWild(Pattern):
    pass


@dataclass
class PLit(Pattern):
    value: Expr


@dataclass
class PName(Pattern):
    """A bare name: a variant/constant to compare with, or a new binding."""
    name: str


@dataclass
class PCtor(Pattern):
    name: str
    args: list[Pattern]


@dataclass
class MatchArm(Node):
    patterns: list[Pattern]
    guard: Expr | None
    body: list[Stmt]


@dataclass
class Match(Stmt):
    subject: Expr
    arms: list[MatchArm]


@dataclass
class Handler(Node):
    var: str
    kind: str | None  # else e is NotFound
    body: list[Stmt]


@dataclass
class TryStmt(Stmt):
    within: Expr | None
    body: list[Stmt]
    handlers: list[Handler]


@dataclass
class ParallelBlock(Stmt):
    body: list[Stmt]


@dataclass
class TestDef(Stmt):
    name: str
    body: list[Stmt]


@dataclass
class EventStmt(Stmt):
    kind: str  # on when every lifecycle
    trigger: Expr | None
    alias: str | None
    body: list[Stmt]


@dataclass
class ConfigBlock(Stmt):
    word: str
    name: str | None


@dataclass
class Program(Node):
    body: list[Stmt]
