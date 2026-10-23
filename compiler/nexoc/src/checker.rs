//! Static checks that the specification calls compile errors.
//!
//! Section 3:
//!   1. A function that can fail returns T!; on pub functions it is written out.
//!   2. Ignoring a possible error is a compile error.
//!   3. `?` can only be used inside a function that also returns `!`.
//!   -  `??` is only for T? values; `?` and `try` only for T! values.
//!
//! Section 2: pub functions declare their types. Contracts: `ensure` and
//! `old()` only make sense inside a function.
//!
//! A function can fail when its declared return type ends in `!`, or when
//! its body contains `fail(...)` or `?` outside a `try`. Calls that can fail
//! must be followed by `?`, be inside a `try`, or be at the top level of the
//! program or of a `test` (where an unhandled error ends the program, D17).

use std::collections::HashMap;

use nexo_diag::{Diagnostic, Span};

use crate::ast::*;

const FALLIBLE_BUILTINS: &[&str] = &["read", "write", "read_lines", "save", "validate"];
const FALLIBLE_MEMBERS: &[(&str, &str)] = &[("json", "parse"), ("csv", "parse")];

#[derive(Clone, Copy)]
enum FnRef<'a> {
    Def(&'a FnDef),
    Lambda,
}

#[derive(Clone, Copy)]
struct Ctx<'a> {
    /// None: top level or test
    func: Option<FnRef<'a>>,
    in_try: bool,
    in_ensure: bool,
    in_parallel_for: bool,
}

impl<'a> Ctx<'a> {
    fn top() -> Ctx<'a> {
        Ctx { func: None, in_try: false, in_ensure: false, in_parallel_for: false }
    }
}

pub fn callee_name(func: &Expr) -> String {
    match &func.kind {
        ExprKind::Name(n) => n.clone(),
        ExprKind::Attr { obj, name, .. } => {
            let inner = callee_name(obj);
            if inner.is_empty() {
                name.clone()
            } else {
                format!("{inner}.{name}")
            }
        }
        _ => String::new(),
    }
}

/// The type as the messages show it: `list[int]?`, or `a | b` for a union.
pub fn type_text(t: &TypeExpr) -> String {
    match t {
        TypeExpr::Union { literals, .. } => literals
            .iter()
            .map(|l| match &l.kind {
                ExprKind::Num { text, .. } | ExprKind::Unit { text, .. } => text.clone(),
                ExprKind::Str(parts) => parts
                    .iter()
                    .map(|p| match p {
                        StrPiece::Lit(s) => s.as_str(),
                        _ => "",
                    })
                    .collect(),
                _ => String::new(),
            })
            .collect::<Vec<_>>()
            .join(" | "),
        TypeExpr::Name(t) => {
            let mut text = t.name.clone();
            if !t.params.is_empty() {
                text.push('[');
                text.push_str(&t.params.iter().map(type_text).collect::<Vec<_>>().join(", "));
                text.push(']');
            }
            if t.optional {
                text.push('?');
            }
            text
        }
    }
}

pub struct Checker {
    /// name -> can fail
    fns: HashMap<String, bool>,
    methods: HashMap<String, bool>,
}

type CResult = Result<(), Diagnostic>;

pub fn check(program: &Program) -> CResult {
    let mut c = Checker { fns: HashMap::new(), methods: HashMap::new() };
    c.collect(&program.body);
    c.stmts(&program.body, Ctx::top())
}

impl Checker {
    // -- pass 1: which functions can fail -------------------------------
    fn collect(&mut self, body: &[Stmt]) {
        walk_stmts(body, true, &mut |s| match &s.kind {
            StmtKind::Fn(f) => {
                let e = self.fns.entry(f.name.clone()).or_insert(false);
                *e = *e || can_fail(f);
            }
            StmtKind::Type(t) => {
                for m in &t.methods {
                    let e = self.methods.entry(m.name.clone()).or_insert(false);
                    *e = *e || can_fail(m);
                }
            }
            _ => {}
        });
    }

    /// Some(true/false) when known, None when unknown (e.g. a lambda).
    fn call_can_fail(&self, func: &Expr) -> Option<bool> {
        match &func.kind {
            ExprKind::Name(n) => {
                if let Some(f) = self.fns.get(n) {
                    return Some(*f);
                }
                if FALLIBLE_BUILTINS.contains(&n.as_str()) {
                    return Some(true);
                }
                None
            }
            ExprKind::Attr { obj, name, .. } => {
                if let ExprKind::Name(o) = &obj.kind {
                    if FALLIBLE_MEMBERS.contains(&(o.as_str(), name.as_str())) {
                        return Some(true);
                    }
                }
                self.methods.get(name).copied()
            }
            _ => None,
        }
    }

    // -- pass 2 ---------------------------------------------------------
    fn stmts<'a>(&self, body: &'a [Stmt], ctx: Ctx<'a>) -> CResult {
        for s in body {
            self.stmt(s, ctx)?;
        }
        Ok(())
    }

    fn stmt<'a>(&self, s: &'a Stmt, ctx: Ctx<'a>) -> CResult {
        match &s.kind {
            StmtKind::Fn(f) => self.fn_def(f),
            StmtKind::Type(t) => {
                for m in &t.methods {
                    self.fn_def(m)?;
                }
                for f in &t.fields {
                    if let Some(d) = &f.default {
                        self.expr(d, Ctx::top(), false)?;
                    }
                }
                Ok(())
            }
            StmtKind::Test { body, .. } => self.stmts(body, Ctx::top()),
            StmtKind::Expr(e) => self.expr(e, ctx, false),
            StmtKind::Assign { target, value, .. } => {
                self.expr(target, ctx, false)?;
                self.expr(value, ctx, false)
            }
            StmtKind::VarDecl { value, .. } => self.expr(value, ctx, false),
            StmtKind::Return(value) => {
                if ctx.func.is_none() && !ctx.in_parallel_for {
                    return Err(Diagnostic::new("return_outside_fn", s.span));
                }
                match value {
                    Some(v) => self.expr(v, ctx, false),
                    None => Ok(()),
                }
            }
            StmtKind::Expect(e) => {
                if !matches!(ctx.func, Some(FnRef::Def(_))) {
                    return Err(Diagnostic::new("ensure_outside_fn", s.span).arg("kw", "expect"));
                }
                self.expr(e, ctx, false)
            }
            StmtKind::Ensure(e) => {
                if !matches!(ctx.func, Some(FnRef::Def(_))) {
                    return Err(Diagnostic::new("ensure_outside_fn", s.span).arg("kw", "ensure"));
                }
                self.expr(e, Ctx { in_ensure: true, ..ctx }, false)
            }
            StmtKind::Assert { expr, .. } => self.expr(expr, ctx, false),
            StmtKind::Emit { args, .. } => {
                for a in args {
                    self.expr(&a.value, ctx, false)?;
                }
                Ok(())
            }
            StmtKind::If { branches, orelse } => {
                for (cond, body) in branches {
                    self.expr(cond, ctx, false)?;
                    self.stmts(body, ctx)?;
                }
                if let Some(b) = orelse {
                    self.stmts(b, ctx)?;
                }
                Ok(())
            }
            StmtKind::For { iterable: head, body, .. }
            | StmtKind::While { cond: head, body }
            | StmtKind::Repeat { count: head, body } => {
                self.expr(head, ctx, false)?;
                self.stmts(body, ctx)
            }
            StmtKind::Match { subject, arms } => {
                self.expr(subject, ctx, false)?;
                for arm in arms {
                    if let Some(g) = &arm.guard {
                        self.expr(g, ctx, false)?;
                    }
                    self.stmts(&arm.body, ctx)?;
                }
                Ok(())
            }
            StmtKind::Try { within, body, handlers } => {
                if let Some(w) = within {
                    self.expr(w, ctx, false)?;
                }
                self.stmts(body, Ctx { in_try: true, ..ctx })?;
                for h in handlers {
                    self.stmts(&h.body, ctx)?;
                }
                Ok(())
            }
            StmtKind::Parallel(body) => self.stmts(body, ctx),
            StmtKind::Event { body, .. } => self.stmts(body, Ctx::top()),
            _ => Ok(()),
        }
    }

    fn fn_def(&self, f: &FnDef) -> CResult {
        let fallible = can_fail(f);
        if f.is_pub {
            for p in &f.params {
                if p.ty.is_none() && p.name != "self" {
                    return Err(Diagnostic::new("pub_param_type", p.span).arg("param", p.name.clone()).arg("fn", f.name.clone()));
                }
            }
            if f.ret.is_none() {
                if fallible {
                    return Err(Diagnostic::new("pub_fallible_untyped", f.name_span).arg("fn", f.name.clone()));
                }
                let mut returns_value = false;
                walk_stmts(&f.body, false, &mut |s| {
                    if matches!(s.kind, StmtKind::Return(Some(_))) {
                        returns_value = true;
                    }
                });
                if returns_value {
                    return Err(Diagnostic::new("pub_return_type", f.name_span).arg("fn", f.name.clone()));
                }
            }
        }
        for p in &f.params {
            if let Some(d) = &p.default {
                self.expr(d, Ctx::top(), false)?;
            }
        }
        self.stmts(&f.body, Ctx { func: Some(FnRef::Def(f)), ..Ctx::top() })
    }

    fn bang_check(&self, ctx: Ctx, span: Span) -> CResult {
        if let Some(FnRef::Def(f)) = ctx.func {
            if let Some(ret) = &f.ret {
                if !ret.fallible() && !ctx.in_try {
                    return Err(Diagnostic::new("fallible_needs_bang", span)
                        .arg("fn", f.name.clone())
                        .arg("type", type_text(ret)));
                }
            }
        }
        Ok(())
    }

    fn expr<'a>(&self, e: &'a Expr, ctx: Ctx<'a>, propagated: bool) -> CResult {
        match &e.kind {
            ExprKind::Propagate(inner) => {
                if ctx.func.is_none() {
                    return Err(Diagnostic::new("question_outside_fn", e.span));
                }
                let ExprKind::Call { func, .. } = &inner.kind else {
                    return Err(Diagnostic::new("question_not_error", e.span));
                };
                if matches!(&func.kind, ExprKind::Name(n) if n == "fail") {
                    return Err(Diagnostic::new("question_not_error", e.span));
                }
                if self.call_can_fail(func) == Some(false) {
                    return Err(Diagnostic::new("question_cannot_fail", e.span).arg("name", callee_name(func)));
                }
                self.bang_check(ctx, e.span)?;
                self.expr(inner, ctx, true)
            }
            ExprKind::Call { func, args } => {
                let name = callee_name(func);
                if name == "fail" {
                    if ctx.func.is_some() {
                        self.bang_check(ctx, e.span)?;
                    }
                } else if name == "old" && !ctx.in_ensure {
                    return Err(Diagnostic::new("old_outside_ensure", e.span));
                } else if !propagated && ctx.func.is_some() && !ctx.in_try && self.call_can_fail(func) == Some(true) {
                    return Err(Diagnostic::new("ignored_error", e.span).arg("name", name));
                }
                self.expr(func, ctx, false)?;
                for a in args {
                    self.expr(&a.value, ctx, false)?;
                }
                Ok(())
            }
            ExprKind::Coalesce { left, right } => {
                if let ExprKind::Call { func, .. } = &left.kind {
                    if self.call_can_fail(func) == Some(true) {
                        return Err(Diagnostic::new("coalesce_on_error", left.span).arg("name", callee_name(func)));
                    }
                }
                self.expr(left, ctx, false)?;
                self.expr(right, ctx, false)
            }
            ExprKind::Lambda { body, .. } => self.expr(body, Ctx { func: Some(FnRef::Lambda), ..Ctx::top() }, false),
            ExprKind::ParallelFor { iterable, args, body, .. } => {
                self.expr(iterable, ctx, false)?;
                for a in args {
                    self.expr(&a.value, ctx, false)?;
                }
                self.stmts(body, Ctx { in_parallel_for: true, ..ctx })
            }
            ExprKind::Pipeline { source, steps } => {
                self.expr(source, ctx, false)?;
                for step in steps {
                    match (&step.kind, &step.expr) {
                        (StepKind::Call, Some(x)) if matches!(x.kind, ExprKind::Call { .. }) => {
                            // the value on the left becomes the first argument
                            self.expr(x, ctx, false)?;
                        }
                        (StepKind::Call, Some(Expr { kind: ExprKind::Name(n), .. })) if ctx.func.is_some() && !ctx.in_try => {
                            let known = self.fns.get(n).copied();
                            if known == Some(true) || (known.is_none() && FALLIBLE_BUILTINS.contains(&n.as_str())) {
                                return Err(Diagnostic::new("ignored_error", step.span).arg("name", n.clone()));
                            }
                        }
                        _ => {
                            if let Some(x) = &step.expr {
                                self.expr(x, ctx, false)?;
                            }
                            for c in &step.columns {
                                self.expr(c, ctx, false)?;
                            }
                        }
                    }
                }
                Ok(())
            }
            _ => {
                for c in children(e) {
                    self.expr(c, ctx, false)?;
                }
                Ok(())
            }
        }
    }
}

pub fn children(e: &Expr) -> Vec<&Expr> {
    match &e.kind {
        ExprKind::Str(parts) => parts
            .iter()
            .filter_map(|p| match p {
                StrPiece::Expr(x) => Some(x),
                _ => None,
            })
            .collect(),
        ExprKind::List(items) => items.iter().collect(),
        ExprKind::Map(pairs) => pairs.iter().flat_map(|(k, v)| [k, v]).collect(),
        ExprKind::Comprehension { element, iterable, condition, .. } => {
            let mut v: Vec<&Expr> = vec![iterable, element];
            if let Some(c) = condition {
                v.push(c);
            }
            v
        }
        ExprKind::Attr { obj, .. } => vec![obj],
        ExprKind::Index { obj, index } => vec![obj, index],
        ExprKind::Unary { operand, .. } => vec![operand],
        ExprKind::BinOp { left, right, .. } | ExprKind::BoolOp { left, right, .. } => vec![left, right],
        ExprKind::Compare { operands, .. } => operands.iter().collect(),
        ExprKind::Range { start, end } => vec![start, end],
        _ => Vec::new(),
    }
}

/// Visit every statement, nested blocks included.
pub fn walk_stmts<'a>(body: &'a [Stmt], into_fns: bool, f: &mut dyn FnMut(&'a Stmt)) {
    for s in body {
        f(s);
        let mut blocks: Vec<&'a [Stmt]> = Vec::new();
        match &s.kind {
            StmtKind::Fn(d) => {
                if into_fns {
                    blocks.push(&d.body);
                }
            }
            StmtKind::Type(t) => {
                if into_fns {
                    for m in &t.methods {
                        blocks.push(&m.body);
                    }
                }
            }
            StmtKind::If { branches, orelse } => {
                blocks.extend(branches.iter().map(|(_, b)| b.as_slice()));
                if let Some(b) = orelse {
                    blocks.push(b);
                }
            }
            StmtKind::For { body, .. }
            | StmtKind::While { body, .. }
            | StmtKind::Repeat { body, .. }
            | StmtKind::Parallel(body)
            | StmtKind::Test { body, .. }
            | StmtKind::Event { body, .. } => blocks.push(body),
            StmtKind::Match { arms, .. } => blocks.extend(arms.iter().map(|a| a.body.as_slice())),
            StmtKind::Try { body, handlers, .. } => {
                blocks.push(body);
                blocks.extend(handlers.iter().map(|h| h.body.as_slice()));
            }
            _ => {}
        }
        for b in blocks {
            walk_stmts(b, into_fns, f);
        }
    }
}

/// Inferred T! (section 3, rule 1).
pub fn can_fail(f: &FnDef) -> bool {
    if f.ret.as_ref().is_some_and(|r| r.fallible()) {
        return true;
    }
    body_fails(&f.body, false)
}

fn body_fails(body: &[Stmt], in_try: bool) -> bool {
    for s in body {
        let mut nested: Vec<&[Stmt]> = Vec::new();
        let mut exprs: Vec<&Expr> = Vec::new();
        match &s.kind {
            StmtKind::Fn(_) | StmtKind::Type(_) | StmtKind::Test { .. } | StmtKind::Event { .. } => continue,
            StmtKind::Try { body, handlers, .. } => {
                if body_fails(body, true) || handlers.iter().any(|h| body_fails(&h.body, in_try)) {
                    return true;
                }
                continue;
            }
            StmtKind::If { branches, orelse } => {
                exprs.extend(branches.iter().map(|(c, _)| c));
                nested.extend(branches.iter().map(|(_, b)| b.as_slice()));
                if let Some(b) = orelse {
                    nested.push(b);
                }
            }
            StmtKind::For { iterable, body, .. } => {
                exprs.push(iterable);
                nested.push(body);
            }
            StmtKind::While { cond, body } => {
                exprs.push(cond);
                nested.push(body);
            }
            StmtKind::Repeat { count, body } => {
                exprs.push(count);
                nested.push(body);
            }
            StmtKind::Match { subject, arms } => {
                exprs.push(subject);
                for a in arms {
                    if let Some(g) = &a.guard {
                        exprs.push(g);
                    }
                    nested.push(&a.body);
                }
            }
            StmtKind::Parallel(body) => nested.push(body),
            StmtKind::Expr(e) => exprs.push(e),
            StmtKind::Assign { target, value, .. } => {
                exprs.push(target);
                exprs.push(value);
            }
            StmtKind::VarDecl { value, .. } => exprs.push(value),
            StmtKind::Return(Some(v)) => exprs.push(v),
            StmtKind::Expect(e) | StmtKind::Assert { expr: e, .. } => exprs.push(e),
            _ => {}
        }
        if !in_try && exprs.iter().any(|e| expr_fails(e)) {
            return true;
        }
        if nested.iter().any(|b| body_fails(b, in_try)) {
            return true;
        }
    }
    false
}

fn expr_fails(e: &Expr) -> bool {
    match &e.kind {
        ExprKind::Propagate(_) => true,
        ExprKind::Call { func, args } => {
            if matches!(&func.kind, ExprKind::Name(n) if n == "fail") {
                return true;
            }
            expr_fails(func) || args.iter().any(|a| expr_fails(&a.value))
        }
        ExprKind::Lambda { .. } => false,
        ExprKind::ParallelFor { iterable, args, body, .. } => {
            expr_fails(iterable) || args.iter().any(|a| expr_fails(&a.value)) || body_fails(body, false)
        }
        ExprKind::Pipeline { source, steps } => {
            expr_fails(source)
                || steps.iter().any(|s| s.expr.iter().chain(s.columns.iter()).any(expr_fails))
        }
        _ => children(e).into_iter().any(expr_fails),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parser::parse;

    const DIVIDE: &str = "fn divide(a, b):\n    if b == 0:\n        fail(\"zero\")\n    return a / b\n";

    fn key(src: &str) -> Option<&'static str> {
        check(&parse(src).unwrap()).err().map(|d| d.key)
    }

    #[test]
    fn section_3_compile_errors() {
        assert_eq!(key(&format!("{DIVIDE}fn half(x):\n    return divide(x, 2)\nprint(half(4))\n")), Some("ignored_error"));
        assert_eq!(key("fn load(p):\n    return read(p)\n"), Some("ignored_error"));
        assert_eq!(key("fn load(p):\n    text = json.parse(p)\n    return text\n"), Some("ignored_error"));
        assert_eq!(key("x = read(\"a\")?\n"), Some("question_outside_fn"));
        assert_eq!(key("fn f(p) -> str:\n    return read(p)?\n"), Some("fallible_needs_bang"));
        assert_eq!(key("fn f(p) -> int:\n    fail(\"no\")\n"), Some("fallible_needs_bang"));
        assert_eq!(key("pub fn f(p: str):\n    return read(p)?\n"), Some("pub_fallible_untyped"));
        assert_eq!(key("pub fn f(p) -> int:\n    return 1\n"), Some("pub_param_type"));
        assert_eq!(key("pub fn f(p: int):\n    return p\n"), Some("pub_return_type"));
        assert_eq!(key("fn f(x: str?) -> str!:\n    return x?\n"), Some("question_not_error"));
        assert_eq!(key("fn g():\n    return 1\nfn f() -> int!:\n    return g()?\n"), Some("question_cannot_fail"));
        assert_eq!(key("x = read(\"a\") ?? \"\"\n"), Some("coalesce_on_error"));
        assert_eq!(key("ensure 1 == 1\n"), Some("ensure_outside_fn"));
        assert_eq!(key("x = old(1)\n"), Some("old_outside_ensure"));
        assert_eq!(key("return 1\n"), Some("return_outside_fn"));
    }

    #[test]
    fn handled_and_passed_errors_are_fine() {
        assert_eq!(key(&format!("{DIVIDE}fn half(x) -> float!:\n    return divide(x, 2)?\n")), None);
        assert_eq!(key(&format!("{DIVIDE}fn safe(a, b):\n    try:\n        return divide(a, b)\n    else e:\n        return 0\n")), None);
        assert_eq!(key(&format!("{DIVIDE}print(divide(1, 0))\n")), None);
    }
}
