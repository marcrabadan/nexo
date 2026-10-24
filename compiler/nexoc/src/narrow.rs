//! Values that are certainly ints, so the code for them needs no type
//! checks at run time (the slow paths of `+`, `<`... are left out, and
//! LLVM keeps the numbers in registers).
//!
//! The analysis is deliberately strict: an expression is certainly an int
//! when it is an int literal, `+ - * // %` or unary `-` on certainly-int
//! operands (an overflow is an error, never another type), `len(...)`, a
//! call to a function defined once whose declared return type is `int`
//! (the return value is checked), or a variable whose every assignment is
//! certainly an int (or that is declared `int`, which is checked when it is
//! stored). The variables are found by assuming them all ints and removing
//! those with an assignment that is not, until nothing changes.

use std::collections::{HashMap, HashSet};

use crate::ast::*;
use crate::resolve::*;

pub struct Ints<'a> {
    res: &'a Resolution,
    int_bindings: HashSet<BindingId>,
    /// functions defined once whose declared return type is int
    int_fns: HashSet<BindingId>,
}

/// How a binding gets its values.
enum Def<'a> {
    /// assigned this expression
    Value(&'a Expr),
    /// checked against `int` when stored
    Int,
    /// anything else (a pattern, an element of a list...)
    Other,
}

pub fn is_int_type(t: &TypeExpr) -> bool {
    matches!(t, TypeExpr::Name(tn) if tn.name == "int" && tn.params.is_empty() && !tn.optional && !tn.fallible)
}

impl<'a> Ints<'a> {
    pub fn new(program: &'a Program, res: &'a Resolution, known_fns: &HashMap<BindingId, &'a FnDef>) -> Ints<'a> {
        let int_fns: HashSet<BindingId> = known_fns
            .iter()
            .filter(|(_, f)| f.ret.as_ref().is_some_and(is_int_type))
            .map(|(b, _)| *b)
            .collect();
        let mut defs: Vec<Vec<Def<'a>>> = (0..res.bindings.len()).map(|_| Vec::new()).collect();
        let mut stmts: Vec<&'a Stmt> = Vec::new();
        collect_stmts(&program.body, &mut stmts);
        for s in &stmts {
            match &s.kind {
                StmtKind::Assign { target: Expr { kind: ExprKind::Name(_), .. }, op, value, ty } => {
                    let Some(a) = res.assigns.get(&s.id) else { continue };
                    let mut targets: Vec<BindingId> = a.cands.iter().map(|(b, _)| *b).collect();
                    targets.push(a.target);
                    for b in targets {
                        defs[b].push(match ty {
                            Some(t) if is_int_type(t) => Def::Int,
                            Some(_) => Def::Other,
                            // `x += v` is `x = x + v`: an int when both are
                            None if *op == "=" || *op == "+=" || *op == "-=" || *op == "*=" => Def::Value(value),
                            None => Def::Other,
                        });
                    }
                }
                StmtKind::VarDecl { ty, value, .. } => {
                    let Some(a) = res.assigns.get(&s.id) else { continue };
                    defs[a.target].push(match ty {
                        Some(t) if is_int_type(t) => Def::Int,
                        Some(_) => Def::Other,
                        None => Def::Value(value),
                    });
                }
                StmtKind::Shared { .. } => {
                    if let Some(a) = res.assigns.get(&s.id) {
                        defs[a.target].push(Def::Other);
                    }
                }
                StmtKind::For { iterable, .. } => {
                    if let Some(b) = res.decl.get(&Node::Stmt(s.id)) {
                        defs[*b].push(if matches!(iterable.kind, ExprKind::Range { .. }) {
                            Def::Value(iterable)
                        } else {
                            Def::Other
                        });
                    }
                }
                _ => {}
            }
        }
        let mut ints = Ints { res, int_bindings: HashSet::new(), int_fns };
        // parameters declared int; plain variables assigned only ints
        let mut candidates: HashSet<BindingId> = HashSet::new();
        for (b, binding) in res.bindings.iter().enumerate() {
            if binding.shared || binding.origin != Origin::Plain || defs[b].is_empty() {
                continue;
            }
            // a binding given its value before its scope runs (a pattern, a
            // handler, a loop variable) is an int only as a loop over a range
            let from_range = defs[b].iter().any(|d| matches!(d, Def::Value(Expr { kind: ExprKind::Range { .. }, .. })));
            if binding.entry && !from_range {
                continue;
            }
            candidates.insert(b);
        }
        // parameters: from their declaration
        let mut params: HashSet<BindingId> = HashSet::new();
        let mut fns: Vec<&'a FnDef> = Vec::new();
        crate::checker::walk_stmts(&program.body, true, &mut |s| match &s.kind {
            StmtKind::Fn(f) => fns.push(f),
            StmtKind::Type(t) => fns.extend(t.methods.iter()),
            _ => {}
        });
        for f in fns {
            let Some(frame) = res.frame_of.get(&Node::Fn(f.id)) else { continue };
            for (p, b) in f.params.iter().zip(&res.frames[*frame].params) {
                if p.ty.as_ref().is_some_and(is_int_type) {
                    params.insert(*b);
                }
            }
        }
        ints.int_bindings = candidates.union(&params).copied().collect();
        loop {
            let mut removed = Vec::new();
            for b in &candidates {
                if !ints.int_bindings.contains(b) {
                    continue;
                }
                let ok = defs[*b].iter().all(|d| match d {
                    Def::Int => true,
                    Def::Other => false,
                    Def::Value(e) => match &e.kind {
                        ExprKind::Range { start, end } => ints.certain(start) && ints.certain(end),
                        _ => ints.certain(e),
                    },
                });
                if !ok {
                    removed.push(*b);
                }
            }
            if removed.is_empty() {
                break;
            }
            for b in removed {
                ints.int_bindings.remove(&b);
            }
        }
        ints
    }

    /// Is the expression certainly an int?
    pub fn certain(&self, e: &Expr) -> bool {
        match &e.kind {
            ExprKind::Num { text, is_float: false } => text.parse::<i64>().is_ok(),
            ExprKind::BinOp { op, left, right } => {
                matches!(*op, "+" | "-" | "*" | "//" | "%") && self.certain(left) && self.certain(right)
            }
            ExprKind::Unary { op: "-", operand } => self.certain(operand),
            ExprKind::Name(_) => self.int_name(e),
            ExprKind::Call { func, .. } => match &func.kind {
                ExprKind::Name(_) => match self.res.names.get(&func.id) {
                    Some(NameRes::Builtin(n)) => n == "len",
                    Some(NameRes::Bindings { cands, row: None, .. }) if cands.len() == 1 => self.int_fns.contains(&cands[0]),
                    _ => false,
                },
                _ => false,
            },
            _ => false,
        }
    }

    fn int_name(&self, e: &Expr) -> bool {
        match self.res.names.get(&e.id) {
            Some(NameRes::Bindings { cands, row: None, .. }) if !cands.is_empty() => {
                cands.iter().all(|b| self.int_bindings.contains(b))
            }
            _ => false,
        }
    }
}

/// Every statement, including those of `parallel for` bodies.
fn collect_stmts<'a>(body: &'a [Stmt], out: &mut Vec<&'a Stmt>) {
    crate::checker::walk_stmts(body, true, &mut |s| {
        out.push(s);
    });
    // statements inside expressions (parallel for)
    let mut inner: Vec<&'a [Stmt]> = Vec::new();
    for s in out.iter() {
        stmt_exprs(s, &mut |e| find_bodies(e, &mut inner));
    }
    for b in inner {
        let mut more = Vec::new();
        collect_stmts(b, &mut more);
        out.extend(more);
    }
}

fn stmt_exprs<'a>(s: &'a Stmt, f: &mut dyn FnMut(&'a Expr)) {
    match &s.kind {
        StmtKind::Expr(e) | StmtKind::Expect(e) | StmtKind::Ensure(e) | StmtKind::Return(Some(e)) => f(e),
        StmtKind::Assign { target, value, .. } => {
            f(target);
            f(value);
        }
        StmtKind::VarDecl { value, .. } | StmtKind::Shared { value, .. } => f(value),
        StmtKind::If { branches, .. } => branches.iter().for_each(|(c, _)| f(c)),
        StmtKind::For { iterable: e, .. } | StmtKind::While { cond: e, .. } | StmtKind::Repeat { count: e, .. } => f(e),
        StmtKind::Match { subject, .. } => f(subject),
        StmtKind::Spawn { call } => f(call),
        _ => {}
    }
}

fn find_bodies<'a>(e: &'a Expr, out: &mut Vec<&'a [Stmt]>) {
    match &e.kind {
        ExprKind::ParallelFor { iterable, body, .. } => {
            find_bodies(iterable, out);
            out.push(body);
        }
        ExprKind::Call { func, args } => {
            find_bodies(func, out);
            for a in args {
                find_bodies(&a.value, out);
            }
        }
        ExprKind::Pipeline { source, .. } => find_bodies(source, out),
        ExprKind::Coalesce { left, right } => {
            find_bodies(left, out);
            find_bodies(right, out);
        }
        ExprKind::Propagate(x) => find_bodies(x, out),
        _ => {
            for c in crate::checker::children(e) {
                find_bodies(c, out);
            }
        }
    }
}
