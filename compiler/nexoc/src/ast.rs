//! Typed AST of the Nexo core. Every node carries the span of its source.
//! Expressions and statements get a unique `id` so later passes can attach
//! information to them.

use nexo_diag::Span;

#[derive(Clone, Debug)]
pub enum TypeExpr {
    Name(TypeName),
    /// `"active" | "closed"`
    Union { span: Span, literals: Vec<Expr> },
    /// `1..5`: an int between the two bounds (section 8)
    Range { span: Span, low: i64, high: i64 },
}

impl TypeExpr {
    pub fn span(&self) -> Span {
        match self {
            TypeExpr::Name(t) => t.span,
            TypeExpr::Union { span, .. } | TypeExpr::Range { span, .. } => *span,
        }
    }

    pub fn optional(&self) -> bool {
        matches!(self, TypeExpr::Name(t) if t.optional)
    }

    pub fn fallible(&self) -> bool {
        matches!(self, TypeExpr::Name(t) if t.fallible)
    }
}

#[derive(Clone, Debug)]
pub struct TypeName {
    pub span: Span,
    /// dotted name, e.g. "list" or "nexo.Thing"
    pub name: String,
    pub params: Vec<TypeExpr>,
    pub optional: bool,
    pub fallible: bool,
    pub weak: bool,
    /// `ref T` or `many T` in a @table type (nexo.db)
    pub rel: Option<&'static str>,
}

#[derive(Clone, Debug)]
pub enum StrPiece {
    Lit(String),
    Expr(Expr),
}

#[derive(Clone, Debug)]
pub struct Arg {
    pub span: Span,
    pub name: Option<String>,
    pub value: Expr,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StepKind {
    Where,
    Map,
    Pick,
    Group,
    Sum,
    Count,
    Avg,
    Sort,
    Take,
    Each,
    /// `window 1m`, `sliding 5m`, `session 30s` after a stream (section 19)
    Window,
    Sliding,
    Session,
    Call,
}

impl StepKind {
    pub fn word(self) -> &'static str {
        match self {
            StepKind::Where => "where",
            StepKind::Map => "map",
            StepKind::Pick => "pick",
            StepKind::Group => "group",
            StepKind::Sum => "sum",
            StepKind::Count => "count",
            StepKind::Avg => "avg",
            StepKind::Sort => "sort",
            StepKind::Take => "take",
            StepKind::Each => "each",
            StepKind::Window => "window",
            StepKind::Sliding => "sliding",
            StepKind::Session => "session",
            StepKind::Call => "call",
        }
    }

    pub fn from_word(w: &str) -> Option<StepKind> {
        Some(match w {
            "where" => StepKind::Where,
            "map" => StepKind::Map,
            "pick" => StepKind::Pick,
            "group" => StepKind::Group,
            "sum" => StepKind::Sum,
            "count" => StepKind::Count,
            "avg" => StepKind::Avg,
            "sort" => StepKind::Sort,
            "take" => StepKind::Take,
            "each" => StepKind::Each,
            "window" => StepKind::Window,
            "sliding" => StepKind::Sliding,
            "session" => StepKind::Session,
            _ => return None,
        })
    }
}

/// One step after `|>`. For `Call`, `expr` is the function (a name,
/// attribute, call or lambda).
#[derive(Clone, Debug)]
pub struct PipeStep {
    pub span: Span,
    pub kind: StepKind,
    pub expr: Option<Expr>,
    /// pick
    pub columns: Vec<Expr>,
    /// sort by ... desc
    pub desc: bool,
}

#[derive(Clone, Debug)]
pub struct Expr {
    pub id: u32,
    pub span: Span,
    pub kind: ExprKind,
}

#[derive(Clone, Debug)]
pub enum ExprKind {
    Num { text: String, is_float: bool },
    Unit { text: String, is_float: bool, unit: String },
    Str(Vec<StrPiece>),
    Bool(bool),
    None,
    Name(String),
    List(Vec<Expr>),
    Map(Vec<(Expr, Expr)>),
    Comprehension { element: Box<Expr>, var: String, iterable: Box<Expr>, condition: Option<Box<Expr>> },
    Attr { obj: Box<Expr>, name: String, name_span: Span },
    Index { obj: Box<Expr>, index: Box<Expr> },
    Call { func: Box<Expr>, args: Vec<Arg> },
    /// Postfix `?`: pass the error to the caller.
    Propagate(Box<Expr>),
    /// "-" or "not"
    Unary { op: &'static str, operand: Box<Expr> },
    /// + - * / // % **
    BinOp { op: &'static str, left: Box<Expr>, right: Box<Expr> },
    /// and / or
    BoolOp { op: &'static str, left: Box<Expr>, right: Box<Expr> },
    /// == != < > <= >= in is
    Compare { ops: Vec<&'static str>, operands: Vec<Expr> },
    Coalesce { left: Box<Expr>, right: Box<Expr> },
    Range { start: Box<Expr>, end: Box<Expr> },
    Lambda { param: String, body: Box<Expr> },
    Pipeline { source: Box<Expr>, steps: Vec<PipeStep> },
    ParallelFor { var: String, iterable: Box<Expr>, args: Vec<Arg>, body: Vec<Stmt> },
}

#[derive(Clone, Debug)]
pub struct Param {
    pub span: Span,
    pub name: String,
    pub ty: Option<TypeExpr>,
    pub default: Option<Expr>,
}

#[derive(Clone, Debug)]
pub struct Annotation {
    pub span: Span,
    pub name: String,
    pub args: Vec<Arg>,
}

#[derive(Clone, Debug)]
pub struct FnDef {
    pub id: u32,
    pub span: Span,
    pub name: String,
    pub name_span: Span,
    pub params: Vec<Param>,
    pub ret: Option<TypeExpr>,
    pub body: Vec<Stmt>,
    pub is_pub: bool,
    pub annotations: Vec<Annotation>,
}

#[derive(Clone, Debug)]
pub struct FieldDef {
    pub span: Span,
    pub name: String,
    pub ty: TypeExpr,
    pub default: Option<Expr>,
    /// @unique, @private, @personal
    pub marks: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct VariantDef {
    pub span: Span,
    pub name: String,
    pub params: Vec<Param>,
}

#[derive(Clone, Debug)]
pub struct TypeDef {
    pub span: Span,
    pub is_pub: bool,
    pub name: String,
    pub name_span: Span,
    /// type Status = "a" | "b"
    pub alias: Option<TypeExpr>,
    /// type Circle is Shape
    pub trait_name: Option<String>,
    pub fields: Vec<FieldDef>,
    pub variants: Vec<VariantDef>,
    pub methods: Vec<FnDef>,
    pub settings: Vec<(String, Expr)>,
    pub annotations: Vec<Annotation>,
}

#[derive(Clone, Debug)]
pub struct FnSig {
    pub span: Span,
    pub name: String,
}

#[derive(Clone, Debug)]
pub enum Pattern {
    Wild(Span),
    Lit(Span, Expr),
    /// A bare name: a variant/type to compare with, or a new binding.
    Name(Span, String),
    Ctor(Span, String, Vec<Pattern>),
}

impl Pattern {
    pub fn span(&self) -> Span {
        match self {
            Pattern::Wild(s) | Pattern::Lit(s, _) | Pattern::Name(s, _) | Pattern::Ctor(s, _, _) => *s,
        }
    }
}

#[derive(Clone, Debug)]
pub struct SelectArm {
    pub span: Span,
    /// the name of the received value; None for `after`
    pub var: Option<String>,
    pub channel: Option<Expr>,
    pub after: Option<Expr>,
    pub body: Vec<Stmt>,
}

#[derive(Clone, Debug)]
pub struct MatchArm {
    pub span: Span,
    pub patterns: Vec<Pattern>,
    pub guard: Option<Expr>,
    pub body: Vec<Stmt>,
}

#[derive(Clone, Debug)]
pub struct Handler {
    pub span: Span,
    pub var: String,
    /// else e is NotFound
    pub kind: Option<String>,
    pub body: Vec<Stmt>,
}

#[derive(Clone, Debug)]
pub struct Stmt {
    pub id: u32,
    pub span: Span,
    pub kind: StmtKind,
}

#[derive(Clone, Debug)]
pub enum StmtKind {
    Expr(Expr),
    Assign { target: Expr, op: &'static str, value: Expr, ty: Option<TypeExpr> },
    VarDecl { name: String, name_span: Span, ty: Option<TypeExpr>, value: Expr },
    Return(Option<Expr>),
    Pass,
    /// `use nexo.db`, `use python numpy as np`, `use c "libm"`
    Use { what: String, lang: Option<String>, name: String, alias: Option<String> },
    /// `use shop.payments` of a file of the program (D124), after loading:
    /// the module's code at its first use (None at the others); `alias` is
    /// the name it is used with
    Module { name: String, alias: String, body: Option<Vec<Stmt>> },
    /// `spawn f(x)` (nexo.sync)
    Spawn { call: Expr },
    /// `select:` with `msg from channel:` and `after limit:` arms (nexo.sync)
    Select { arms: Vec<SelectArm> },
    /// `shared name = value` (nexo.sync)
    Shared { name: String, name_span: Span, value: Expr },
    Emit { name: String, args: Vec<Arg>, call_span: Span },
    Expect(Expr),
    Ensure(Expr),
    Assert { expr: Expr, fails: bool },
    Fn(FnDef),
    Type(TypeDef),
    Trait { name: String, sigs: Vec<FnSig> },
    If { branches: Vec<(Expr, Vec<Stmt>)>, orelse: Option<Vec<Stmt>> },
    For { var: String, iterable: Expr, body: Vec<Stmt> },
    While { cond: Expr, body: Vec<Stmt> },
    Repeat { count: Expr, body: Vec<Stmt> },
    Match { subject: Expr, arms: Vec<MatchArm> },
    Try { within: Option<Expr>, body: Vec<Stmt>, handlers: Vec<Handler> },
    Parallel(Vec<Stmt>),
    Test { name: String, body: Vec<Stmt> },
    /// on / when / every / lifecycle
    Event { kind: &'static str, name: String, params: Vec<Arg>, body: Vec<Stmt> },
    Config { word: String },
}

#[derive(Clone, Debug)]
pub struct Program {
    pub body: Vec<Stmt>,
    /// ids of the module files merged into it (D124)
    pub files: Vec<u32>,
}
