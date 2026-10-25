//! Static type checking (D68).
//!
//! Types are optional and inferred (section 2), so the checker is gradual:
//! it infers the type of an expression when it is certain (literals,
//! declared types, known functions and records, operators on known
//! types) and `Unknown` otherwise. A mistake that would certainly happen
//! when the line runs is reported before the program runs, with the same
//! message the runtime gives; everything else is still checked when the
//! value is stored (D33).

use std::collections::HashMap;

use nexo_diag::{suggest, Diagnostic, Span};

use crate::ast::*;
use crate::checker::{type_text, walk_stmts};
use crate::resolve::*;

#[derive(Clone, Debug, PartialEq)]
pub enum Ty {
    Unknown,
    None,
    Bool,
    Int,
    Float,
    /// a float literal: a float, or an exact decimal next to a decimal
    FloatLit,
    Decimal,
    /// money, with its currency when it is known
    Money(Option<String>),
    Quantity,
    Str,
    List(Box<Ty>),
    Map,
    Set,
    Range,
    Record(u32),
    Enum(u32),
    Error,
    /// a known function or constructor
    Callable(Callee),
    /// a type used as a value (int, User...)
    TypeValue,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Callee {
    /// a user function, by the id of its definition
    Fn(u32),
    Record(u32),
    Variant(u32),
    Builtin,
}

type TResult<T> = Result<T, Diagnostic>;

struct TypeInfo<'a> {
    def: &'a TypeDef,
}

pub struct TypeChecker<'a> {
    res: &'a Resolution,
    fns: HashMap<u32, &'a FnDef>,
    types: HashMap<u32, TypeInfo<'a>>,
    /// variant id -> (enum id, definition)
    variants: HashMap<u32, (u32, &'a VariantDef)>,
    binding_fn: HashMap<BindingId, u32>,
    bty: Vec<Ty>,
}

fn is_float_lit(e: &Expr) -> bool {
    match &e.kind {
        ExprKind::Num { is_float: true, .. } => true,
        ExprKind::Unary { op: "-", operand } => is_float_lit(operand),
        _ => false,
    }
}

/// Python's repr(float), as the runtime prints it.
pub fn format_float(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    let sci = format!("{:e}", x);
    let (mant, exp) = sci.split_once('e').unwrap();
    let exp: i64 = exp.parse().unwrap();
    let (neg, mant) = match mant.strip_prefix('-') {
        Some(m) => (true, m),
        None => (false, mant),
    };
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let decpt = exp + 1;
    let body = if -4 < decpt && decpt <= 16 {
        let n = digits.len() as i64;
        if decpt <= 0 {
            format!("0.{}{}", "0".repeat((-decpt) as usize), digits)
        } else if decpt >= n {
            format!("{}{}.0", digits, "0".repeat((decpt - n) as usize))
        } else {
            format!("{}.{}", &digits[..decpt as usize], &digits[decpt as usize..])
        }
    } else {
        let m = if digits.len() > 1 { format!("{}.{}", &digits[..1], &digits[1..]) } else { digits.clone() };
        let e = decpt - 1;
        format!("{}e{}{:02}", m, if e < 0 { '-' } else { '+' }, e.abs())
    };
    if neg {
        format!("-{body}")
    } else {
        body
    }
}

fn quote(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            _ => out.push(c),
        }
    }
    out.push('"');
    out
}

/// A literal value, as the runtime would show it, with its kind.
#[derive(Clone, PartialEq, Debug)]
enum Lit {
    Int(i64),
    Float(String),
    Str(String),
    Bool(bool),
    None,
}

fn literal(e: &Expr) -> Option<Lit> {
    match &e.kind {
        ExprKind::Num { text, is_float: false } => text.parse().ok().map(Lit::Int),
        ExprKind::Num { text, is_float: true } => text.parse::<f64>().ok().map(|f| Lit::Float(format_float(f))),
        ExprKind::Unary { op: "-", operand } => match literal(operand)? {
            Lit::Int(i) => i.checked_neg().map(Lit::Int),
            Lit::Float(f) => {
                let v: f64 = f.parse().ok()?;
                Some(Lit::Float(format_float(-v)))
            }
            _ => None,
        },
        ExprKind::Str(parts) if parts.iter().all(|p| matches!(p, StrPiece::Lit(_))) => Some(Lit::Str(
            parts
                .iter()
                .map(|p| match p {
                    StrPiece::Lit(s) => s.as_str(),
                    _ => "",
                })
                .collect(),
        )),
        ExprKind::Bool(b) => Some(Lit::Bool(*b)),
        ExprKind::None => Some(Lit::None),
        _ => None,
    }
}

impl Lit {
    fn repr(&self) -> String {
        match self {
            Lit::Int(i) => i.to_string(),
            Lit::Float(f) => f.clone(),
            Lit::Str(s) => quote(s),
            Lit::Bool(b) => b.to_string(),
            Lit::None => "none".into(),
        }
    }

    fn type_name(&self) -> &'static str {
        match self {
            Lit::Int(_) => "int",
            Lit::Float(_) => "float",
            Lit::Str(_) => "str",
            Lit::Bool(_) => "bool",
            Lit::None => "none",
        }
    }

    /// Same kind and equal (a union of values, D30).
    fn matches(&self, other: &Lit) -> bool {
        match (self, other) {
            (Lit::Float(a), Lit::Float(b)) => a.parse::<f64>().ok() == b.parse::<f64>().ok(),
            _ => self == other,
        }
    }
}

fn join(a: &Ty, b: &Ty) -> Ty {
    if a == b {
        return a.clone();
    }
    match (a, b) {
        (Ty::Money(_), Ty::Money(_)) => Ty::Money(None),
        (Ty::List(_), Ty::List(_)) => Ty::List(Box::new(Ty::Unknown)),
        _ => Ty::Unknown,
    }
}

impl<'a> TypeChecker<'a> {
    pub fn new(program: &'a Program, res: &'a Resolution) -> TypeChecker<'a> {
        let mut tc = TypeChecker {
            res,
            fns: HashMap::new(),
            types: HashMap::new(),
            variants: HashMap::new(),
            binding_fn: HashMap::new(),
            bty: vec![Ty::Unknown; res.bindings.len()],
        };
        // how many times each binding is defined (functions known by
        // their single definition)
        let mut defs: HashMap<BindingId, usize> = HashMap::new();
        walk_stmts(&program.body, true, &mut |s| match &s.kind {
            StmtKind::Fn(f) => {
                tc.fns.insert(f.id, f);
                if let Some(b) = res.decl.get(&Node::Stmt(s.id)) {
                    *defs.entry(*b).or_default() += 1;
                    tc.binding_fn.insert(*b, f.id);
                }
            }
            StmtKind::Type(t) => {
                let id = res.type_ids[&s.id];
                tc.types.insert(id, TypeInfo { def: t });
                for (i, v) in t.variants.iter().enumerate() {
                    tc.variants.insert(id + 1 + i as u32, (id, v));
                }
                for m in &t.methods {
                    tc.fns.insert(m.id, m);
                }
            }
            StmtKind::Assign { .. } | StmtKind::VarDecl { .. } => {
                if let Some(a) = res.assigns.get(&s.id) {
                    *defs.entry(a.target).or_default() += 1;
                }
            }
            _ => {}
        });
        tc.binding_fn.retain(|b, _| defs.get(b) == Some(&1));
        tc
    }

    // ------------------------------------------------------- declared types

    fn decl_ty(&self, t: &TypeExpr) -> Ty {
        match t {
            TypeExpr::Union { .. } | TypeExpr::Range { .. } => Ty::Unknown,
            TypeExpr::Name(tn) => {
                if tn.optional {
                    return Ty::Unknown;
                }
                match self.res.types.get(&(tn as *const TypeName as usize)) {
                    Some(TypeRes::Builtin(n)) => match n.as_str() {
                        "int" => Ty::Int,
                        "float" => Ty::Float,
                        "decimal" => Ty::Decimal,
                        "money" => Ty::Money(None),
                        "str" => Ty::Str,
                        "bool" => Ty::Bool,
                        "list" => {
                            Ty::List(Box::new(tn.params.first().map(|p| self.decl_ty(p)).unwrap_or(Ty::Unknown)))
                        }
                        "map" => Ty::Map,
                        "set" => Ty::Set,
                        _ => Ty::Unknown,
                    },
                    Some(TypeRes::User(id, _)) => match self.types.get(id) {
                        Some(info) if info.def.alias.is_some() => self.decl_ty(info.def.alias.as_ref().unwrap()),
                        Some(info) if !info.def.variants.is_empty() => Ty::Enum(*id),
                        Some(_) => Ty::Record(*id),
                        None => match self.variants.get(id) {
                            Some((e, _)) => Ty::Enum(*e),
                            None => Ty::Unknown,
                        },
                    },
                    _ => Ty::Unknown,
                }
            }
        }
    }

    fn type_name(&self, t: &Ty) -> Option<String> {
        Some(match t {
            Ty::None => "none".into(),
            Ty::Bool => "bool".into(),
            Ty::Int => "int".into(),
            Ty::Float | Ty::FloatLit => "float".into(),
            Ty::Decimal => "decimal".into(),
            Ty::Money(_) => "money".into(),
            Ty::Quantity => "quantity".into(),
            Ty::Str => "str".into(),
            Ty::List(_) => "list".into(),
            Ty::Map => "map".into(),
            Ty::Set => "set".into(),
            Ty::Range => "range".into(),
            Ty::Record(id) | Ty::Enum(id) => self.types.get(id)?.def.name.clone(),
            Ty::Callable(_) => "fn".into(),
            Ty::TypeValue => "type".into(),
            Ty::Unknown | Ty::Error => return None,
        })
    }

    /// Can a value of type `v` be stored where `t` is declared?
    fn compatible(v: &Ty, t: &Ty) -> bool {
        match (v, t) {
            (Ty::Unknown, _) | (_, Ty::Unknown) => true,
            (Ty::Int, Ty::Float | Ty::Decimal) => true,
            (Ty::FloatLit, Ty::Float | Ty::Decimal) => true,
            (Ty::Money(_), Ty::Money(_)) => true,
            (Ty::List(_), Ty::List(_)) => true,
            _ => v == t,
        }
    }

    /// The union of values behind a declared type, if it is one.
    fn union_of(&self, t: &'a TypeExpr) -> Option<&'a [Expr]> {
        match t {
            TypeExpr::Union { literals, .. } => Some(literals),
            TypeExpr::Name(tn) if !tn.optional => match self.res.types.get(&(tn as *const TypeName as usize)) {
                Some(TypeRes::User(id, _)) => match self.types.get(id).and_then(|i| i.def.alias.as_ref()) {
                    Some(TypeExpr::Union { literals, .. }) => Some(literals),
                    _ => None,
                },
                _ => None,
            },
            _ => None,
        }
    }

    /// Check a value against a declared type (what the runtime's conform
    /// would certainly reject).
    fn conform(&self, value: &Expr, vt: &Ty, t: &'a TypeExpr, name: &str, span: Span) -> TResult<()> {
        let lit = literal(value);
        if let Some(options) = self.union_of(t) {
            let Some(l) = &lit else { return Ok(()) };
            let opts: Vec<Lit> = options.iter().filter_map(literal).collect();
            if opts.len() != options.len() || opts.iter().any(|o| o.matches(l)) {
                return Ok(());
            }
            let shown = type_text(t).trim_end_matches('?').to_string();
            let values = opts.iter().map(|o| o.repr()).collect::<Vec<_>>().join(", ");
            return Err(Diagnostic::new("type_mismatch", span)
                .fix("type_mismatch_union")
                .arg("name", name)
                .arg("type", shown)
                .arg("value", l.repr())
                .arg("vtype", l.type_name())
                .arg("values", values));
        }
        if *vt == Ty::None {
            if t.optional() {
                return Ok(());
            }
            return Err(Diagnostic::new("none_not_allowed", span).arg("name", name).arg("type", type_text(t)));
        }
        let target = self.decl_ty(t);
        if Self::compatible(vt, &target) {
            return Ok(());
        }
        let Some(vtype) = self.type_name(vt) else { return Ok(()) };
        let shown = type_text(t).trim_end_matches('?').to_string();
        Err(match lit {
            Some(l) => Diagnostic::new("type_mismatch", span)
                .arg("name", name)
                .arg("type", shown)
                .arg("value", l.repr())
                .arg("vtype", vtype),
            None => Diagnostic::new("type_mismatch_static", span).arg("name", name).arg("type", shown).arg("vtype", vtype),
        })
    }

    // ------------------------------------------------------ binding types

    /// Infer the type of every binding from the values assigned to it.
    pub fn infer(&mut self, program: &'a Program) {
        for _ in 0..6 {
            let mut facts: Vec<Option<Ty>> = vec![None; self.res.bindings.len()];
            let mut add = |b: BindingId, t: Ty| {
                let t = if t == Ty::FloatLit { Ty::Float } else { t };
                facts[b] = Some(match &facts[b] {
                    None => t,
                    Some(old) => join(old, &t),
                });
            };
            let mut stmts: Vec<&'a Stmt> = Vec::new();
            walk_stmts(&program.body, true, &mut |s| stmts.push(s));
            for s in &stmts {
                match &s.kind {
                    StmtKind::Assign { target: Expr { kind: ExprKind::Name(_), id, .. }, op, value, ty } => {
                        let a = &self.res.assigns[&s.id];
                        let vt = match ty {
                            Some(t) => self.decl_ty(t),
                            None if *op == "=" => self.ty_quiet(value),
                            None => {
                                let cur = self.name_ty(*id);
                                let v = self.ty_quiet(value);
                                self.arith_ty(&op[..1], &cur, &v)
                            }
                        };
                        if ty.is_none() {
                            for (b, _) in &a.cands {
                                if self.res.bindings[*b].kind == BKind::Var {
                                    let bt = match &self.res.bindings[*b].ty {
                                        Some(t) => self.decl_ty(t),
                                        None => vt.clone(),
                                    };
                                    add(*b, bt);
                                }
                            }
                        }
                        add(a.target, vt);
                    }
                    StmtKind::VarDecl { ty, value, .. } => {
                        let a = &self.res.assigns[&s.id];
                        let vt = match ty {
                            Some(t) => self.decl_ty(t),
                            None => self.ty_quiet(value),
                        };
                        add(a.target, vt);
                    }
                    StmtKind::For { iterable, .. } => {
                        let b = self.res.decl[&Node::Stmt(s.id)];
                        let it = self.ty_quiet(iterable);
                        add(b, Self::element(&it));
                    }
                    _ => {}
                }
            }
            let mut changed = false;
            for (b, f) in facts.into_iter().enumerate() {
                let binding = &self.res.bindings[b];
                let t = match binding.origin {
                    Origin::Fn => match self.binding_fn.get(&b) {
                        Some(id) => Ty::Callable(Callee::Fn(*id)),
                        None => Ty::Unknown,
                    },
                    Origin::Type(id) => match self.types.get(&id) {
                        Some(info) if info.def.alias.is_none() && info.def.variants.is_empty() => {
                            Ty::Callable(Callee::Record(id))
                        }
                        _ => Ty::TypeValue,
                    },
                    Origin::Variant(id, true) => Ty::Callable(Callee::Variant(id)),
                    Origin::Variant(id, false) => Ty::Enum(self.variants.get(&id).map(|v| v.0).unwrap_or(0)),
                    Origin::Trait(_) => Ty::TypeValue,
                    Origin::Param => Ty::Unknown,
                    Origin::Plain => {
                        if binding.kind == BKind::Var && binding.ty.is_some() {
                            self.decl_ty(binding.ty.as_ref().unwrap())
                        } else {
                            f.unwrap_or(Ty::Unknown)
                        }
                    }
                };
                if self.bty[b] != t {
                    self.bty[b] = t;
                    changed = true;
                }
            }
            self.param_types(program);
            if !changed {
                break;
            }
        }
    }

    fn param_types(&mut self, program: &'a Program) {
        let fns: Vec<&'a FnDef> = self.fns.values().copied().collect();
        let _ = program;
        for f in fns {
            let Some(frame) = self.res.frame_of.get(&Node::Fn(f.id)) else { continue };
            let params = self.res.frames[*frame].params.clone();
            for (p, b) in f.params.iter().zip(params) {
                self.bty[b] = match &p.ty {
                    Some(t) => self.decl_ty(t),
                    None => Ty::Unknown,
                };
            }
        }
    }

    fn element(t: &Ty) -> Ty {
        match t {
            Ty::List(e) => (**e).clone(),
            Ty::Range => Ty::Int,
            Ty::Str => Ty::Str,
            _ => Ty::Unknown,
        }
    }

    fn name_ty(&self, id: u32) -> Ty {
        match self.res.names.get(&id) {
            Some(NameRes::Bindings { cands, row: None, .. }) if cands.len() == 1 => self.bty[cands[0]].clone(),
            Some(NameRes::Builtin(n)) => match n.as_str() {
                "PI" => Ty::Float,
                "print" | "input" | "len" | "range" | "set" | "round" | "split" | "convert" | "fail" | "validate"
                | "read" | "write" | "read_lines" | "save" => Ty::Callable(Callee::Builtin),
                "json" | "csv" => Ty::Unknown,
                "half_up" | "half_even" => Ty::Unknown,
                _ if crate::resolve::LIBRARY_FUNCTIONS.contains(&n.as_str()) => Ty::Unknown,
                _ if crate::lexer::is_currency(n) => Ty::Unknown,
                _ => Ty::TypeValue,
            },
            _ => Ty::Unknown,
        }
    }

    /// The type of an expression without reporting anything.
    fn ty_quiet(&self, e: &'a Expr) -> Ty {
        self.ty(e).unwrap_or(Ty::Unknown)
    }

    fn arith_ty(&self, op: &str, a: &Ty, b: &Ty) -> Ty {
        self.arith(op, a, b, Span::default()).unwrap_or(Ty::Unknown)
    }

    // --------------------------------------------------------- operators

    fn bad(&self, op: &str, a: &Ty, b: &Ty, span: Span) -> TResult<Ty> {
        match (self.type_name(a), self.type_name(b)) {
            (Some(x), Some(y)) => Err(Diagnostic::new("bad_operands", span).arg("op", op).arg("a", x).arg("b", y)),
            _ => Ok(Ty::Unknown),
        }
    }

    fn mix(&self, a: &Ty, b: &Ty, span: Span) -> TResult<Ty> {
        Err(Diagnostic::new("mix_numbers", span)
            .arg("a", self.type_name(a).unwrap_or_default())
            .arg("b", self.type_name(b).unwrap_or_default()))
    }

    /// Section 17 rule 1 for static types: a float literal next to a
    /// decimal or money is a decimal; otherwise it is a float.
    fn exact(a: &Ty, b: &Ty) -> (Ty, Ty) {
        let exact = |t: &Ty| matches!(t, Ty::Decimal | Ty::Money(_) | Ty::Quantity);
        let a2 = match a {
            Ty::FloatLit if exact(b) => Ty::Decimal,
            Ty::FloatLit => Ty::Float,
            t => t.clone(),
        };
        let b2 = match b {
            Ty::FloatLit if exact(a) => Ty::Decimal,
            Ty::FloatLit => Ty::Float,
            t => t.clone(),
        };
        (a2, b2)
    }

    /// The result of `a op b`, or the error the runtime would give.
    fn arith(&self, op: &str, a: &Ty, b: &Ty, span: Span) -> TResult<Ty> {
        let (a, b) = Self::exact(a, b);
        if a == Ty::Unknown || b == Ty::Unknown || a == Ty::Error || b == Ty::Error {
            return Ok(Ty::Unknown);
        }
        if a == Ty::Bool || b == Ty::Bool {
            return self.bad(op, &a, &b, span);
        }
        if op == "+" {
            if a == Ty::Str && b == Ty::Str {
                return Ok(Ty::Str);
            }
            if let (Ty::List(_), Ty::List(_)) = (&a, &b) {
                return Ok(Ty::List(Box::new(Ty::Unknown)));
            }
        }
        let money = |t: &Ty| matches!(t, Ty::Money(_));
        if money(&a) || money(&b) {
            if let (Ty::Money(ca), Ty::Money(cb)) = (&a, &b) {
                if let (Some(x), Some(y)) = (ca, cb) {
                    if x != y {
                        if matches!(op, "+" | "-" | "/") {
                            return Err(Diagnostic::new("currency_mismatch", span).arg("a", x.clone()).arg("b", y.clone()));
                        }
                        return self.bad(op, &a, &b, span);
                    }
                }
                return match op {
                    "+" | "-" => Ok(Ty::Money(ca.clone().or(cb.clone()))),
                    "/" => Ok(Ty::Decimal),
                    _ => self.bad(op, &a, &b, span),
                };
            }
            let factor_ok = |t: &Ty| matches!(t, Ty::Int | Ty::Decimal | Ty::Quantity);
            if op == "*" {
                let (m, other) = if money(&a) { (&a, &b) } else { (&b, &a) };
                if *other == Ty::Float {
                    return self.mix(m, other, span);
                }
                if factor_ok(other) {
                    return Ok(m.clone());
                }
                return self.bad(op, m, other, span);
            }
            if op == "/" && money(&a) {
                if b == Ty::Float {
                    return self.mix(&a, &b, span);
                }
                if factor_ok(&b) {
                    return Ok(a.clone());
                }
                return self.bad(op, &a, &b, span);
            }
            return self.bad(op, &a, &b, span);
        }
        if a == Ty::Quantity || b == Ty::Quantity {
            return Ok(Ty::Unknown);
        }
        let num = |t: &Ty| matches!(t, Ty::Int | Ty::Float | Ty::Decimal);
        if !(num(&a) && num(&b)) {
            return self.bad(op, &a, &b, span);
        }
        if a == Ty::Int && b == Ty::Int {
            return Ok(match op {
                "/" => Ty::Float,
                "**" => Ty::Unknown,
                _ => Ty::Int,
            });
        }
        if a == Ty::Decimal || b == Ty::Decimal {
            if a == Ty::Float || b == Ty::Float {
                return self.mix(&a, &b, span);
            }
            return Ok(Ty::Decimal);
        }
        Ok(Ty::Float)
    }

    fn compare(&self, op: &str, a: &Ty, b: &Ty, span: Span) -> TResult<()> {
        let (a, b) = Self::exact(a, b);
        if matches!(a, Ty::Unknown | Ty::Error) || matches!(b, Ty::Unknown | Ty::Error) {
            return Ok(());
        }
        let num = |t: &Ty| matches!(t, Ty::Int | Ty::Float | Ty::Decimal);
        let mixed = (a == Ty::Decimal && b == Ty::Float) || (a == Ty::Float && b == Ty::Decimal);
        match op {
            "==" | "!=" => {
                if num(&a) && num(&b) && mixed {
                    self.mix(&a, &b, span)?;
                }
                Ok(())
            }
            "<" | ">" | "<=" | ">=" => {
                if num(&a) && num(&b) {
                    if mixed {
                        self.mix(&a, &b, span)?;
                    }
                    return Ok(());
                }
                if a == Ty::Str && b == Ty::Str {
                    return Ok(());
                }
                if let (Ty::Money(x), Ty::Money(y)) = (&a, &b) {
                    if let (Some(x), Some(y)) = (x, y) {
                        if x != y {
                            return Err(Diagnostic::new("currency_mismatch", span).arg("a", x.clone()).arg("b", y.clone()));
                        }
                    }
                    return Ok(());
                }
                if a == Ty::Quantity && b == Ty::Quantity {
                    return Ok(());
                }
                match (self.type_name(&a), self.type_name(&b)) {
                    (Some(x), Some(y)) => Err(Diagnostic::new("compare", span).arg("a", x).arg("b", y)),
                    _ => Ok(()),
                }
            }
            "in" => {
                if b == Ty::Str && !matches!(a, Ty::Str) {
                    if let Some(x) = self.type_name(&a) {
                        return Err(Diagnostic::new("bad_operands", span).arg("op", "in").arg("a", x).arg("b", "str"));
                    }
                }
                if matches!(b, Ty::Int | Ty::Float | Ty::Decimal | Ty::Bool | Ty::None | Ty::Money(_) | Ty::Record(_)) {
                    if let Some(x) = self.type_name(&b) {
                        return Err(Diagnostic::new("not_iterable", span).arg("vtype", x));
                    }
                }
                Ok(())
            }
            _ => {
                // `is` needs a type on its right
                if matches!(b, Ty::Int | Ty::Float | Ty::Decimal | Ty::Str | Ty::Bool | Ty::List(_) | Ty::Map | Ty::Money(_) | Ty::Record(_)) {
                    if let Some(x) = self.type_name(&b) {
                        return Err(Diagnostic::new("not_a_type_value", span).arg("vtype", x));
                    }
                }
                Ok(())
            }
        }
    }

    fn truth(&self, t: &Ty, span: Span, what: &str) -> TResult<()> {
        if matches!(t, Ty::Bool | Ty::Unknown | Ty::Error) {
            return Ok(());
        }
        match self.type_name(t) {
            Some(v) => Err(Diagnostic::new("not_bool", span).arg("what", what).arg("vtype", v)),
            None => Ok(()),
        }
    }

    // ------------------------------------------------------------- calls

    /// Bind arguments to parameters as the runtime does; returns, for each
    /// parameter, the argument given to it.
    fn bind_args<'e>(
        &self,
        fn_name: &str,
        params: &[(&str, bool)],
        args: &'e [Arg],
        skip_first: bool,
        span: Span,
    ) -> TResult<Vec<Option<&'e Expr>>> {
        let first = usize::from(skip_first);
        let mut bound: Vec<Option<&'e Expr>> = vec![None; params.len()];
        let positional: Vec<&Arg> = args.iter().filter(|a| a.name.is_none()).collect();
        let n_pos = positional.len() + first;
        if n_pos > params.len() {
            return Err(Diagnostic::new("arg_too_many", span)
                .arg("fn", fn_name)
                .arg("n", params.len().to_string())
                .arg("m", n_pos.to_string()));
        }
        for (i, a) in positional.iter().enumerate() {
            bound[i + first] = Some(&a.value);
        }
        for a in args {
            let Some(n) = &a.name else { continue };
            let Some(i) = params.iter().position(|(p, _)| p == n) else {
                let names: Vec<&str> = params.iter().map(|(p, _)| *p).collect();
                return Err(match suggest(n, names.iter().copied()) {
                    Some(s) => Diagnostic::new("arg_unknown", span)
                        .fix("suggest")
                        .arg("fn", fn_name)
                        .arg("param", n.clone())
                        .arg("suggestion", s),
                    None => Diagnostic::new("arg_unknown", span).arg("fn", fn_name).arg("param", n.clone()).arg(
                        "params",
                        if names.is_empty() { "-".to_string() } else { names.join(", ") },
                    ),
                });
            };
            if bound[i].is_some() || (i < first) {
                return Err(Diagnostic::new("arg_twice", span).arg("param", n.clone()));
            }
            bound[i] = Some(&a.value);
        }
        for (i, (p, optional)) in params.iter().enumerate() {
            if i >= first && bound[i].is_none() && !optional {
                return Err(Diagnostic::new("arg_missing", span).arg("param", *p).arg("fn", fn_name));
            }
        }
        Ok(bound)
    }

    /// Check a call to a known user function.
    fn call_fn(&self, f: &'a FnDef, args: &'a [Arg], method: bool, first: bool, span: Span) -> TResult<Ty> {
        let mut params: Vec<(&str, bool)> = f
            .params
            .iter()
            .map(|p| (p.name.as_str(), p.default.is_some() || p.ty.as_ref().is_some_and(|t| t.optional())))
            .collect();
        let mut decl_params: Vec<&Param> = f.params.iter().collect();
        if method && params.first().is_some_and(|(n, _)| *n == "self") {
            params.remove(0);
            decl_params.remove(0);
        }
        let bound = self.bind_args(&f.name, &params, args, first, span)?;
        for (p, arg) in decl_params.iter().zip(bound) {
            if let (Some(t), Some(arg)) = (&p.ty, arg) {
                let at = self.ty(arg)?;
                self.conform(arg, &at, t, &p.name, span)?;
            }
        }
        Ok(match &f.ret {
            // a `T!` function gives a T when it does not fail
            Some(t) => self.decl_ty(t),
            None => Ty::Unknown,
        })
    }

    fn call_record(&self, id: u32, args: &'a [Arg], first: bool, span: Span) -> TResult<Ty> {
        let def = self.types[&id].def;
        let params: Vec<(&str, bool)> =
            def.fields.iter().map(|f| (f.name.as_str(), f.default.is_some() || f.ty.optional())).collect();
        let bound = self.bind_args(&def.name, &params, args, first, span)?;
        for (f, arg) in def.fields.iter().zip(bound) {
            if let Some(arg) = arg {
                let at = self.ty(arg)?;
                self.conform(arg, &at, &f.ty, &f.name, span)?;
            }
        }
        Ok(Ty::Record(id))
    }

    fn call_variant(&self, id: u32, args: &'a [Arg], first: bool, span: Span) -> TResult<Ty> {
        let (enum_id, v) = self.variants[&id];
        let params: Vec<(&str, bool)> = v.params.iter().map(|p| (p.name.as_str(), false)).collect();
        let bound = self.bind_args(&v.name, &params, args, first, span)?;
        for (p, arg) in v.params.iter().zip(bound) {
            if let (Some(t), Some(arg)) = (&p.ty, arg) {
                let at = self.ty(arg)?;
                self.conform(arg, &at, t, &p.name, span)?;
            }
        }
        Ok(Ty::Enum(enum_id))
    }

    fn method_of(&self, type_id: u32, name: &str) -> Option<&'a FnDef> {
        self.types.get(&type_id)?.def.methods.iter().find(|m| m.name == name)
    }

    fn builtin_call_ty(name: &str) -> Ty {
        match name {
            "len" | "int" => Ty::Int,
            "str" | "input" => Ty::Str,
            "float" => Ty::Float,
            "decimal" => Ty::Decimal,
            "print" | "write" | "save" => Ty::None,
            "range" => Ty::List(Box::new(Ty::Int)),
            "read_lines" => Ty::List(Box::new(Ty::Str)),
            "set" => Ty::Set,
            "split" => Ty::List(Box::new(Ty::Money(None))),
            "convert" => Ty::Money(None),
            _ => Ty::Unknown,
        }
    }

    fn method_ty(obj: &Ty, name: &str) -> Ty {
        match (obj, name) {
            (Ty::Str, "upper" | "lower" | "trim" | "replace") => Ty::Str,
            (Ty::Str, "split") => Ty::List(Box::new(Ty::Str)),
            (Ty::Str, "starts_with" | "ends_with") => Ty::Bool,
            (Ty::List(_) | Ty::Set | Ty::Map, "count") => Ty::Int,
            (Ty::List(_) | Ty::Set, "add") => Ty::None,
            (Ty::Map, "keys" | "values") => Ty::List(Box::new(Ty::Unknown)),
            _ => Ty::Unknown,
        }
    }

    fn builtin_members(obj: &Ty) -> Option<&'static [&'static str]> {
        Some(match obj {
            Ty::List(_) => &["sum", "count", "add"],
            Ty::Set => &["add", "count"],
            Ty::Map => &["keys", "values", "count"],
            Ty::Str => &["upper", "lower", "trim", "split", "replace", "starts_with", "ends_with", "find"],
            _ => return None,
        })
    }

    fn member_error(&self, vtype: String, name: &str, candidates: &[String], span: Span) -> Diagnostic {
        match suggest(name, candidates.iter().map(|s| s.as_str())) {
            Some(s) => Diagnostic::new("no_member", span).fix("suggest").arg("vtype", vtype).arg("name", name).arg("suggestion", s),
            None => Diagnostic::new("no_member", span).arg("vtype", vtype).arg("name", name),
        }
    }

    /// `obj.name` on a value of a known type.
    fn attr(&self, obj: &Ty, name: &str, name_span: Span) -> TResult<Ty> {
        match obj {
            Ty::Record(id) => {
                let def = self.types[id].def;
                if let Some(f) = def.fields.iter().find(|f| f.name == name) {
                    return Ok(self.decl_ty(&f.ty));
                }
                if def.methods.iter().any(|m| m.name == name) {
                    return Ok(Ty::Unknown);
                }
                let mut c: Vec<String> = def.fields.iter().map(|f| f.name.clone()).collect();
                c.extend(def.methods.iter().map(|m| m.name.clone()));
                Err(self.member_error(def.name.clone(), name, &c, name_span))
            }
            Ty::Error => Ok(match name {
                "message" | "kind" => Ty::Str,
                _ => Ty::Unknown,
            }),
            Ty::None => Err(Diagnostic::new("none_member", name_span).arg("name", name)),
            Ty::Str | Ty::List(_) | Ty::Map | Ty::Set => {
                let names = Self::builtin_members(obj).unwrap();
                if names.contains(&name) {
                    return Ok(Ty::Unknown);
                }
                let c: Vec<String> = names.iter().map(|s| s.to_string()).collect();
                Err(self.member_error(self.type_name(obj).unwrap(), name, &c, name_span))
            }
            Ty::Int | Ty::Float | Ty::FloatLit | Ty::Bool | Ty::Decimal | Ty::Range => {
                Err(self.member_error(self.type_name(obj).unwrap(), name, &[], name_span))
            }
            _ => Ok(Ty::Unknown),
        }
    }

    // --------------------------------------------------------- expressions

    /// The static type of an expression; reports certain mistakes.
    pub fn ty(&self, e: &'a Expr) -> TResult<Ty> {
        Ok(match &e.kind {
            ExprKind::Num { is_float: false, .. } => Ty::Int,
            ExprKind::Num { is_float: true, .. } => Ty::FloatLit,
            ExprKind::Unit { unit, .. } => {
                if crate::lexer::is_currency(unit) {
                    Ty::Money(Some(unit.clone()))
                } else {
                    Ty::Quantity
                }
            }
            ExprKind::Str(parts) => {
                for p in parts {
                    if let StrPiece::Expr(x) = p {
                        self.ty(x)?;
                    }
                }
                Ty::Str
            }
            ExprKind::Bool(_) => Ty::Bool,
            ExprKind::None => Ty::None,
            ExprKind::Name(_) => self.name_ty(e.id),
            ExprKind::List(items) => {
                let mut t: Option<Ty> = None;
                for i in items {
                    let it = self.ty(i)?;
                    let it = if it == Ty::FloatLit { Ty::Float } else { it };
                    t = Some(match t {
                        None => it,
                        Some(old) => join(&old, &it),
                    });
                }
                Ty::List(Box::new(t.unwrap_or(Ty::Unknown)))
            }
            ExprKind::Map(pairs) => {
                for (k, v) in pairs {
                    let kt = self.ty(k)?;
                    if matches!(kt, Ty::List(_) | Ty::Map | Ty::Set | Ty::Record(_)) {
                        return Err(Diagnostic::new("unhashable", k.span).arg("vtype", self.type_name(&kt).unwrap()));
                    }
                    self.ty(v)?;
                }
                Ty::Map
            }
            ExprKind::Comprehension { element, iterable, condition, .. } => {
                let it = self.ty(iterable)?;
                self.iterable(&it, iterable.span)?;
                if let Some(c) = condition {
                    let ct = self.ty(c)?;
                    self.truth(&ct, c.span, "if")?;
                }
                let el = self.ty(element)?;
                Ty::List(Box::new(if el == Ty::FloatLit { Ty::Float } else { el }))
            }
            ExprKind::Attr { obj, name, name_span } => {
                let ot = self.ty(obj)?;
                self.attr(&ot, name, *name_span)?
            }
            ExprKind::Index { obj, index } => {
                let ot = self.ty(obj)?;
                let it = self.ty(index)?;
                match &ot {
                    Ty::List(el) => {
                        if !matches!(it, Ty::Int | Ty::Unknown) {
                            if let Some(v) = self.type_name(&it) {
                                return Err(Diagnostic::new("bad_index", e.span).arg("vtype", "list").arg("itype", v));
                            }
                        }
                        (**el).clone()
                    }
                    Ty::Str => {
                        if !matches!(it, Ty::Int | Ty::Unknown) {
                            if let Some(v) = self.type_name(&it) {
                                return Err(Diagnostic::new("bad_index", e.span).arg("vtype", "str").arg("itype", v));
                            }
                        }
                        Ty::Str
                    }
                    Ty::Map => {
                        if matches!(it, Ty::List(_) | Ty::Map | Ty::Set | Ty::Record(_)) {
                            return Err(Diagnostic::new("unhashable", e.span).arg("vtype", self.type_name(&it).unwrap()));
                        }
                        Ty::Unknown
                    }
                    Ty::None => return Err(Diagnostic::new("none_member", e.span).arg("name", "[]")),
                    Ty::Int | Ty::Float | Ty::FloatLit | Ty::Bool | Ty::Decimal | Ty::Money(_) | Ty::Set | Ty::Record(_) => {
                        return Err(Diagnostic::new("not_indexable", e.span).arg("vtype", self.type_name(&ot).unwrap()))
                    }
                    _ => Ty::Unknown,
                }
            }
            ExprKind::Call { func, args } => self.call(e, func, args, false)?,
            ExprKind::Propagate(x) => self.ty(x)?,
            ExprKind::Unary { op: "-", operand } => {
                let t = self.ty(operand)?;
                match t {
                    Ty::Int | Ty::Float | Ty::FloatLit | Ty::Decimal | Ty::Money(_) | Ty::Quantity => t,
                    Ty::Unknown | Ty::Error => Ty::Unknown,
                    _ => match self.type_name(&t) {
                        Some(v) => return Err(Diagnostic::new("bad_operand", e.span).arg("op", "-").arg("a", v)),
                        None => Ty::Unknown,
                    },
                }
            }
            ExprKind::Unary { operand, .. } => {
                let t = self.ty(operand)?;
                self.truth(&t, operand.span, "not")?;
                Ty::Bool
            }
            ExprKind::BinOp { op, left, right } => {
                let l = self.ty(left)?;
                let r = self.ty(right)?;
                self.arith(op, &l, &r, e.span)?
            }
            ExprKind::BoolOp { op, left, right } => {
                let l = self.ty(left)?;
                self.truth(&l, left.span, op)?;
                let r = self.ty(right)?;
                self.truth(&r, right.span, op)?;
                Ty::Bool
            }
            ExprKind::Compare { ops, operands } => {
                let mut left = self.ty(&operands[0])?;
                for (op, o) in ops.iter().zip(&operands[1..]) {
                    let right = self.ty(o)?;
                    self.compare(op, &left, &right, e.span)?;
                    left = right;
                }
                Ty::Bool
            }
            ExprKind::Coalesce { left, right } => {
                let l = self.ty(left)?;
                let r = self.ty(right)?;
                if l == Ty::None {
                    r
                } else {
                    Ty::Unknown
                }
            }
            ExprKind::Range { start, end } => {
                let a = self.ty(start)?;
                let b = self.ty(end)?;
                for t in [&a, &b] {
                    if !matches!(t, Ty::Int | Ty::Unknown) {
                        if let (Some(x), Some(y)) = (self.type_name(&a), self.type_name(&b)) {
                            return Err(Diagnostic::new("bad_operands", e.span).arg("op", "..").arg("a", x).arg("b", y));
                        }
                    }
                }
                Ty::Range
            }
            ExprKind::Lambda { body, .. } => {
                self.ty(body)?;
                Ty::Unknown
            }
            ExprKind::Pipeline { source, steps } => {
                self.ty(source)?;
                let mut t = Ty::Unknown;
                for s in steps {
                    if let Some(x) = &s.expr {
                        match (&s.kind, &x.kind) {
                            (StepKind::Call, ExprKind::Call { func, args }) => {
                                t = self.call(x, func, args, true)?;
                                continue;
                            }
                            (StepKind::Where, _) => {
                                let ct = self.ty(x)?;
                                self.truth(&ct, x.span, "where")?;
                            }
                            (StepKind::Take, _) => {
                                let nt = self.ty(x)?;
                                if let Some(l) = literal(x) {
                                    if !matches!(l, Lit::Int(i) if i >= 0) {
                                        return Err(Diagnostic::new("type_mismatch", x.span)
                                            .arg("name", "take")
                                            .arg("type", "int")
                                            .arg("value", l.repr())
                                            .arg("vtype", l.type_name()));
                                    }
                                }
                                let _ = nt;
                            }
                            _ => {
                                self.ty(x)?;
                            }
                        }
                    }
                    for c in &s.columns {
                        self.ty(c)?;
                    }
                    t = match s.kind {
                        StepKind::Count => Ty::Int,
                        StepKind::Each => Ty::None,
                        StepKind::Where | StepKind::Map | StepKind::Take | StepKind::Sort | StepKind::Pick => {
                            Ty::List(Box::new(Ty::Unknown))
                        }
                        _ => Ty::Unknown,
                    };
                }
                t
            }
            ExprKind::ParallelFor { iterable, args, body, .. } => {
                let it = self.ty(iterable)?;
                self.iterable(&it, iterable.span)?;
                for a in args {
                    self.ty(&a.value)?;
                }
                self.block(body, None)?;
                Ty::List(Box::new(Ty::Unknown))
            }
        })
    }

    fn iterable(&self, t: &Ty, span: Span) -> TResult<()> {
        if matches!(t, Ty::Int | Ty::Float | Ty::FloatLit | Ty::Decimal | Ty::Bool | Ty::None | Ty::Money(_) | Ty::Record(_)) {
            return Err(Diagnostic::new("not_iterable", span).arg("vtype", self.type_name(t).unwrap()));
        }
        Ok(())
    }

    /// A call: `first` when a pipeline passes the value on the left.
    fn call(&self, e: &'a Expr, func: &'a Expr, args: &'a [Arg], first: bool) -> TResult<Ty> {
        let mut named = false;
        for a in args {
            if a.name.is_none() && named {
                return Err(Diagnostic::new("positional_after_named", a.span));
            }
            named |= a.name.is_some();
        }
        if matches!(&func.kind, ExprKind::Name(n) if n == "old") {
            for a in args {
                self.ty(&a.value)?;
            }
            return Ok(Ty::Unknown);
        }
        let ft = match &func.kind {
            ExprKind::Attr { obj, name, name_span } => {
                let ot = self.ty(obj)?;
                for a in args {
                    self.ty(&a.value)?;
                }
                if let Ty::Record(id) = &ot {
                    let def = self.types[id].def;
                    if !def.fields.iter().any(|f| &f.name == name) {
                        match self.method_of(*id, name) {
                            Some(m) => return self.call_fn(m, args, true, first, e.span),
                            None => {
                                self.attr(&ot, name, *name_span)?;
                            }
                        }
                    }
                    return Ok(Ty::Unknown);
                }
                if Self::builtin_members(&ot).is_some() || matches!(ot, Ty::None | Ty::Int | Ty::Float | Ty::Bool | Ty::Decimal) {
                    self.attr(&ot, name, *name_span)?;
                    return Ok(Self::method_ty(&ot, name));
                }
                return Ok(Ty::Unknown);
            }
            _ => self.ty(func)?,
        };
        let builtin_name = match (&func.kind, self.res.names.get(&func.id)) {
            (ExprKind::Name(_), Some(NameRes::Builtin(n))) => Some(n.clone()),
            _ => None,
        };
        let result = match &ft {
            Ty::Callable(Callee::Fn(id)) => {
                let f = self.fns[id];
                self.call_fn(f, args, false, first, e.span)?
            }
            Ty::Callable(Callee::Record(id)) => self.call_record(*id, args, first, e.span)?,
            Ty::Callable(Callee::Variant(id)) => self.call_variant(*id, args, first, e.span)?,
            Ty::Int | Ty::Float | Ty::FloatLit | Ty::Str | Ty::Bool | Ty::None | Ty::Decimal | Ty::Money(_) | Ty::List(_) | Ty::Map
            | Ty::Range | Ty::Record(_) => {
                return Err(Diagnostic::new("not_callable", e.span).arg("vtype", self.type_name(&ft).unwrap()));
            }
            _ => Ty::Unknown,
        };
        for a in args {
            self.ty(&a.value)?;
        }
        if let Some(n) = builtin_name {
            return Ok(Self::builtin_call_ty(&n));
        }
        Ok(result)
    }

    // -------------------------------------------------------- statements

    pub fn check(&self, program: &'a Program) -> TResult<()> {
        self.block(&program.body, None)
    }

    fn block(&self, body: &'a [Stmt], func: Option<&'a FnDef>) -> TResult<()> {
        for s in body {
            self.stmt(s, func)?;
        }
        Ok(())
    }

    fn stmt(&self, s: &'a Stmt, func: Option<&'a FnDef>) -> TResult<()> {
        match &s.kind {
            StmtKind::Module { body: Some(b), .. } => self.block(b, None)?,
            StmtKind::Expr(e) | StmtKind::Ensure(e) => {
                self.ty(e)?;
            }
            StmtKind::Assign { target, op, value, ty } => {
                let vt = self.ty(value)?;
                match &target.kind {
                    ExprKind::Name(name) => {
                        if let Some(t) = ty {
                            self.conform(value, &vt, t, name, value.span)?;
                        } else if *op == "=" {
                            let a = &self.res.assigns[&s.id];
                            // a typed var being changed
                            if let Some((b, _)) = a.cands.first() {
                                let binding = &self.res.bindings[*b];
                                if binding.kind == BKind::Var {
                                    if let Some(t) = &binding.ty {
                                        self.conform(value, &vt, t, name, value.span)?;
                                    }
                                }
                            }
                        } else {
                            let cur = self.name_ty(target.id);
                            self.arith(&op[..1], &cur, &vt, s.span.to(value.span))?;
                        }
                    }
                    ExprKind::Attr { obj, name, name_span } => {
                        let ot = self.ty(obj)?;
                        if let Ty::Record(id) = &ot {
                            let def = self.types[id].def;
                            match def.fields.iter().find(|f| &f.name == name) {
                                Some(f) => {
                                    if *op == "=" {
                                        self.conform(value, &vt, &f.ty, name, value.span)?;
                                    } else {
                                        let cur = self.decl_ty(&f.ty);
                                        self.arith(&op[..1], &cur, &vt, s.span.to(value.span))?;
                                    }
                                }
                                None => {
                                    let c: Vec<String> = def.fields.iter().map(|f| f.name.clone()).collect();
                                    return Err(self.member_error(def.name.clone(), name, &c, *name_span));
                                }
                            }
                        } else if ot == Ty::None {
                            return Err(Diagnostic::new("none_member", *name_span).arg("name", name.clone()));
                        }
                    }
                    ExprKind::Index { obj, index } => {
                        let ot = self.ty(obj)?;
                        self.ty(index)?;
                        if matches!(ot, Ty::Int | Ty::Float | Ty::Str | Ty::Bool | Ty::Decimal | Ty::Set | Ty::Record(_)) {
                            return Err(Diagnostic::new("not_indexable", target.span).arg("vtype", self.type_name(&ot).unwrap()));
                        }
                    }
                    _ => {}
                }
            }
            StmtKind::VarDecl { name, ty, value, .. } => {
                let vt = self.ty(value)?;
                if let Some(t) = ty {
                    self.conform(value, &vt, t, name, value.span)?;
                }
            }
            StmtKind::Return(v) => {
                if let Some(v) = v {
                    let vt = self.ty(v)?;
                    if let Some(f) = func {
                        if let Some(ret) = &f.ret {
                            let vt = if vt == Ty::FloatLit { Ty::Float } else { vt };
                            self.check_return(f, ret, v, &vt)?;
                        }
                    }
                }
            }
            StmtKind::Emit { args, .. } => {
                for a in args {
                    self.ty(&a.value)?;
                }
            }
            StmtKind::Expect(e) => {
                let t = self.ty(e)?;
                self.truth(&t, e.span, "expect")?;
            }
            StmtKind::Assert { expr, fails } => {
                let t = self.ty(expr)?;
                if !*fails && !matches!(expr.kind, ExprKind::Compare { .. }) {
                    self.truth(&t, expr.span, "assert")?;
                }
            }
            StmtKind::Fn(f) => self.function(f)?,
            StmtKind::Type(t) => {
                for f in &t.fields {
                    if let Some(d) = &f.default {
                        let dt = self.ty(d)?;
                        let _ = dt;
                    }
                }
                for m in &t.methods {
                    self.function(m)?;
                }
            }
            StmtKind::If { branches, orelse } => {
                for (c, b) in branches {
                    let t = self.ty(c)?;
                    self.truth(&t, c.span, "if")?;
                    self.block(b, func)?;
                }
                if let Some(b) = orelse {
                    self.block(b, func)?;
                }
            }
            StmtKind::For { iterable, body, .. } => {
                let t = self.ty(iterable)?;
                self.iterable(&t, iterable.span)?;
                self.block(body, func)?;
            }
            StmtKind::While { cond, body } => {
                let t = self.ty(cond)?;
                self.truth(&t, cond.span, "while")?;
                self.block(body, func)?;
            }
            StmtKind::Repeat { count, body } => {
                self.ty(count)?;
                if let Some(l) = literal(count) {
                    if !matches!(l, Lit::Int(_)) {
                        return Err(Diagnostic::new("type_mismatch", count.span)
                            .arg("name", "repeat")
                            .arg("type", "int")
                            .arg("value", l.repr())
                            .arg("vtype", l.type_name()));
                    }
                }
                self.block(body, func)?;
            }
            StmtKind::Match { subject, arms } => {
                self.ty(subject)?;
                for arm in arms {
                    if let Some(g) = &arm.guard {
                        let t = self.ty(g)?;
                        self.truth(&t, g.span, "if")?;
                    }
                    self.block(&arm.body, func)?;
                }
            }
            StmtKind::Try { within, body, handlers } => {
                if let Some(w) = within {
                    self.ty(w)?;
                }
                self.block(body, func)?;
                for h in handlers {
                    self.block(&h.body, func)?;
                }
            }
            StmtKind::Parallel(body) => self.block(body, func)?,
            StmtKind::Spawn { call } => {
                self.ty(call)?;
            }
            StmtKind::Shared { value, .. } => {
                self.ty(value)?;
            }
            StmtKind::Select { arms } => {
                for a in arms {
                    if let Some(c) = a.channel.as_ref().or(a.after.as_ref()) {
                        self.ty(c)?;
                    }
                    self.block(&a.body, func)?;
                }
            }
            StmtKind::Test { body, .. } => self.block(body, None)?,
            StmtKind::Event { body, .. } => self.block(body, None)?,
            _ => {}
        }
        Ok(())
    }

    fn function(&self, f: &'a FnDef) -> TResult<()> {
        for p in &f.params {
            if let Some(d) = &p.default {
                self.ty(d)?;
            }
        }
        self.block(&f.body, Some(f))
    }

    fn check_return(&self, f: &'a FnDef, ret: &'a TypeExpr, value: &'a Expr, vt: &Ty) -> TResult<()> {
        let span = f.name_span;
        match self.conform(value, vt, ret, &f.name, span) {
            Ok(()) => Ok(()),
            Err(d) => Err(match d.key {
                "type_mismatch" | "none_not_allowed" => {
                    let l = literal(value);
                    let vtype = self.type_name(vt).unwrap_or_default();
                    let shown = type_text(ret);
                    match l {
                        Some(l) => Diagnostic::new("return_mismatch", span)
                            .arg("fn", f.name.clone())
                            .arg("type", shown)
                            .arg("value", l.repr())
                            .arg("vtype", l.type_name()),
                        None => Diagnostic::new("return_mismatch_static", span)
                            .arg("fn", f.name.clone())
                            .arg("type", shown)
                            .arg("vtype", vtype),
                    }
                }
                "type_mismatch_static" => Diagnostic::new("return_mismatch_static", span)
                    .arg("fn", f.name.clone())
                    .arg("type", type_text(ret))
                    .arg("vtype", self.type_name(vt).unwrap_or_default()),
                _ => d,
            }),
        }
    }
}

pub fn check(program: &Program, res: &Resolution) -> TResult<()> {
    let mut tc = TypeChecker::new(program, res);
    tc.infer(program);
    tc.check(program)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parser::parse;

    fn key(src: &str) -> Option<&'static str> {
        let p = parse(src).unwrap();
        let r = resolve(&p).unwrap();
        check(&p, &r).err().map(|d| d.key)
    }

    #[test]
    fn mistakes_found_before_running() {
        assert_eq!(key("x: decimal = 0.1\ny = 0.5\nprint(x + y)\n"), Some("mix_numbers"));
        assert_eq!(key("a = 10.00 EUR\nb = 5.00 USD\nprint(a + b)\n"), Some("currency_mismatch"));
        assert_eq!(key("age: int = none\n"), Some("none_not_allowed"));
        assert_eq!(key("type S = \"a\" | \"b\"\ns: S = \"c\"\n"), Some("type_mismatch"));
        assert_eq!(key("print(1 + \"a\")\n"), Some("bad_operands"));
        assert_eq!(key("if 1:\n    pass\n"), Some("not_bool"));
        assert_eq!(key("fn f(a, b = 1):\n    return a\nf()\n"), Some("arg_missing"));
        assert_eq!(key("fn f(a):\n    return a\nf(1, 2)\n"), Some("arg_too_many"));
        assert_eq!(key("type P:\n    x: int\np = P(1)\nprint(p.y)\n"), Some("no_member"));
        assert_eq!(key("type P:\n    x: int\nP(x: \"a\")\n"), Some("type_mismatch"));
        assert_eq!(key("fn f() -> int:\n    return \"x\"\n"), Some("return_mismatch"));
        assert_eq!(key("n = 5\nn(1)\n"), Some("not_callable"));
        assert_eq!(key("for x in 5:\n    pass\n"), Some("not_iterable"));
        assert_eq!(key("print(\"a\".nope())\n"), Some("no_member"));
    }

    #[test]
    fn gradual_types_stay_quiet() {
        assert_eq!(key("x: decimal = 0.1\nprint(x + 0.2)\n"), None);
        assert_eq!(key("fn f(a):\n    return a + 1\nprint(f(\"s\"))\n"), None);
        assert_eq!(key("var x = 1\nx = \"a\"\nprint(x + 1)\n"), None);
        assert_eq!(key("x: float = 1\ny: decimal = 2\nprint(1.5 EUR * 2)\n"), None);
        assert_eq!(key("if true:\n    v = 1\nelse:\n    v = \"a\"\nprint(v + 1)\n"), None);
        assert_eq!(key("email: str? = none\nprint(email ?? \"-\")\n"), None);
    }
}
