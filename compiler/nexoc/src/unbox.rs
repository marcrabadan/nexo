//! Records that never leave their function become plain ints (D154).
//!
//! A variable qualifies when:
//! - it holds a record of a type defined once at the top level of the
//!   program, whose fields are all declared `int`;
//! - every statement that gives it a value is `name = T(a, b...)` or
//!   `var name = T(a, b...)`, with one positional argument per field that
//!   is certainly an int (narrow.rs) and does not read the variable; a
//!   constant has one such statement;
//! - it is only used as `name.field` (read) and `name.method(args)`, where
//!   the method reads `self` only through its fields, takes ints and
//!   returns an int;
//! - no other function, lambda or task can see it, and it is certainly
//!   defined where it is used (its first assignment is earlier in the same
//!   block as every use).
//!
//! Such a record cannot be shared, printed or compared, so nothing can tell
//! it apart from its fields: the variable becomes one int variable per
//! field, and each method it calls becomes a top-level function that takes
//! the fields as ints. Those functions take the fast paths of int-only
//! functions (D84, D139), and LLVM can inline them. Every expression keeps
//! its span, so errors read exactly as before; the conditions above make
//! sure no message can name the new variables or functions.

use std::collections::{HashMap, HashSet};

use crate::ast::*;
use crate::narrow::{is_int_type, Ints};
use crate::resolve::*;
use nexo_diag::Span;

/// What the rewrite does, found before any change is made.
struct Plan {
    /// variable -> (type name, field names)
    vars: HashMap<BindingId, (String, Vec<String>)>,
    /// expression ids of `v.field` reads -> (variable, field)
    reads: HashMap<u32, (BindingId, String)>,
    /// expression ids of `v.method(...)` calls -> (variable, method)
    calls: HashMap<u32, (BindingId, String)>,
    /// statement ids that give a variable its value -> variable
    defs: HashMap<u32, BindingId>,
    /// (type name, method) of each function to write, with the method
    methods: Vec<(String, String)>,
}

fn field_var(b: BindingId, field: &str) -> String {
    format!("__ur{b}_{field}")
}

fn method_fn(t: &str, m: &str) -> String {
    format!("__ur_{t}_{m}")
}

fn self_field(field: &str) -> String {
    format!("__s_{field}")
}

/// The single binding a name expression reads, if it reads exactly one.
fn single(res: &Resolution, e: &Expr) -> Option<BindingId> {
    match res.names.get(&e.id) {
        Some(NameRes::Bindings { cands, row: None, .. }) if cands.len() == 1 => Some(cands[0]),
        _ => None,
    }
}

fn mentions(res: &Resolution, e: &Expr, b: BindingId) -> bool {
    let mut found = false;
    visit(e, &mut |x| {
        if let ExprKind::Name(_) = x.kind {
            if let Some(NameRes::Bindings { cands, .. }) = res.names.get(&x.id) {
                found |= cands.contains(&b);
            }
        }
    });
    found
}

/// Every expression inside `e`, `e` included.
fn visit<'a>(e: &'a Expr, f: &mut dyn FnMut(&'a Expr)) {
    f(e);
    crate::parser::each_child(e, &mut |c| visit(c, f));
}

/// Is this method body simple enough to copy (no nested scopes of its
/// own), and does it read `self` only through its int fields?
fn method_ok(res: &Resolution, ints: &Ints, t: &TypeDef, m: &FnDef) -> bool {
    if !m.annotations.is_empty() || m.params.first().map(|p| p.name.as_str()) != Some("self") {
        return false;
    }
    if t.methods.iter().filter(|x| x.name == m.name).count() != 1 || t.fields.iter().any(|f| f.name == m.name) {
        return false;
    }
    if !m.params[1..].iter().all(|p| p.default.is_none() && p.ty.as_ref().is_some_and(is_int_type)) {
        return false;
    }
    if !m.ret.as_ref().is_some_and(is_int_type) || !matches!(m.body.last().map(|s| &s.kind), Some(StmtKind::Return(Some(_)))) {
        return false;
    }
    let Some(frame) = res.frame_of.get(&Node::Fn(m.id)) else { return false };
    let sb = res.frames[*frame].params[0];
    if res.bindings[sb].captured {
        return false;
    }
    let mut ok = true;
    let mut allowed: HashSet<u32> = HashSet::new();
    crate::checker::walk_stmts(&m.body, true, &mut |s| {
        ok &= simple_stmt(s);
        match &s.kind {
            StmtKind::Return(Some(v)) => ok &= ints.certain(v),
            StmtKind::Return(None) => ok = false,
            StmtKind::Assign { target, .. } if !matches!(target.kind, ExprKind::Name(_)) => ok = false,
            _ => {}
        }
        crate::parser::each_stmt_expr(s, &mut |e| {
            visit(e, &mut |x| {
                ok &= simple_expr(x);
                if let ExprKind::Attr { obj, name, .. } = &x.kind {
                    if single(res, obj) == Some(sb) && t.fields.iter().any(|f| &f.name == name) {
                        allowed.insert(obj.id);
                    }
                }
            });
        });
    });
    if !ok {
        return false;
    }
    crate::checker::walk_stmts(&m.body, true, &mut |s| {
        crate::parser::each_stmt_expr(s, &mut |e| {
            visit(e, &mut |x| {
                if let (ExprKind::Name(_), Some(NameRes::Bindings { cands, .. })) = (&x.kind, res.names.get(&x.id)) {
                    if cands.contains(&sb) && !allowed.contains(&x.id) {
                        ok = false;
                    }
                }
            });
        });
    });
    ok
}

fn simple_stmt(s: &Stmt) -> bool {
    matches!(
        &s.kind,
        StmtKind::Expr(_)
            | StmtKind::Return(_)
            | StmtKind::Pass
            | StmtKind::If { .. }
            | StmtKind::While { .. }
            | StmtKind::For { .. }
            | StmtKind::Repeat { .. }
            | StmtKind::Expect(_)
            | StmtKind::Ensure(_)
            | StmtKind::Assert { .. }
            | StmtKind::Assign { ty: None, .. }
            | StmtKind::VarDecl { ty: None, .. }
    )
}

fn simple_expr(e: &Expr) -> bool {
    !matches!(
        &e.kind,
        ExprKind::Lambda { .. } | ExprKind::Comprehension { .. } | ExprKind::Pipeline { .. } | ExprKind::ParallelFor { .. }
    )
}

/// Where the walk is: the blocks it is in, and the statement of each.
struct Walk<'a> {
    res: &'a Resolution,
    /// (block id, statement index) from the outermost block in
    stack: Vec<(usize, usize)>,
    next_block: usize,
    /// variable -> the place of its first assignment (depth, block, index)
    first: HashMap<BindingId, (usize, usize, usize)>,
    /// variables seen used before or outside their first block
    bad: HashSet<BindingId>,
    in_module: bool,
    module_vars: HashSet<BindingId>,
}

impl<'a> Walk<'a> {
    fn block(&mut self, body: &'a [Stmt]) {
        let id = self.next_block;
        self.next_block += 1;
        for (i, s) in body.iter().enumerate() {
            self.stack.push((id, i));
            self.stmt(s);
            self.stack.pop();
        }
    }

    /// A use or assignment of `b` here.
    fn seen(&mut self, b: BindingId, defines: bool) {
        if self.in_module {
            self.module_vars.insert(b);
        }
        match self.first.get(&b) {
            Some(&(d, blk, i)) => {
                let fine = self.stack.get(d).is_some_and(|&(bb, j)| bb == blk && j >= i);
                if !fine {
                    self.bad.insert(b);
                }
            }
            None if defines => {
                let d = self.stack.len() - 1;
                let (blk, i) = self.stack[d];
                self.first.insert(b, (d, blk, i));
            }
            None => {
                self.bad.insert(b);
            }
        }
    }

    fn stmt(&mut self, s: &'a Stmt) {
        let res = self.res;
        match &s.kind {
            StmtKind::Assign { value, .. } | StmtKind::VarDecl { value, .. } => {
                self.exprs(value);
                if let Some(a) = res.assigns.get(&s.id) {
                    for (b, _) in &a.cands {
                        self.seen(*b, true);
                    }
                    self.seen(a.target, true);
                }
                if let StmtKind::Assign { target, .. } = &s.kind {
                    if !matches!(target.kind, ExprKind::Name(_)) {
                        self.exprs(target);
                    }
                }
            }
            _ => crate::parser::each_stmt_expr(s, &mut |e| self.exprs_dyn(e)),
        }
        let module = matches!(s.kind, StmtKind::Module { .. });
        let was = self.in_module;
        self.in_module |= module;
        match &s.kind {
            StmtKind::Fn(f) => self.block(&f.body),
            StmtKind::Type(t) => {
                for m in &t.methods {
                    self.block(&m.body);
                }
            }
            StmtKind::If { branches, orelse } => {
                for (_, b) in branches {
                    self.block(b);
                }
                if let Some(b) = orelse {
                    self.block(b);
                }
            }
            StmtKind::For { body, .. }
            | StmtKind::While { body, .. }
            | StmtKind::Repeat { body, .. }
            | StmtKind::Parallel(body)
            | StmtKind::Test { body, .. }
            | StmtKind::Event { body, .. } => self.block(body),
            StmtKind::Match { arms, .. } => {
                for a in arms {
                    self.block(&a.body);
                }
            }
            StmtKind::Try { body, handlers, .. } => {
                self.block(body);
                for h in handlers {
                    self.block(&h.body);
                }
            }
            StmtKind::Select { arms } => {
                for a in arms {
                    self.block(&a.body);
                }
            }
            StmtKind::Module { body: Some(body), .. } => self.block(body),
            _ => {}
        }
        self.in_module = was;
    }

    fn exprs_dyn(&mut self, e: &'a Expr) {
        self.exprs(e)
    }

    fn exprs(&mut self, e: &'a Expr) {
        let res = self.res;
        let mut names = Vec::new();
        visit(e, &mut |x| {
            if let (ExprKind::Name(_), Some(NameRes::Bindings { cands, .. })) = (&x.kind, res.names.get(&x.id)) {
                names.extend(cands.iter().copied());
            }
        });
        for b in names {
            self.seen(b, false);
        }
    }
}

fn plan(program: &Program, res: &Resolution) -> Option<Plan> {
    // functions and record types defined once (as codegen finds them)
    let mut count: HashMap<BindingId, usize> = HashMap::new();
    let mut fns: HashMap<BindingId, &FnDef> = HashMap::new();
    let mut types: HashMap<BindingId, &TypeDef> = HashMap::new();
    crate::checker::walk_stmts(&program.body, true, &mut |s| match &s.kind {
        StmtKind::Fn(f) => {
            if let Some(b) = res.decl.get(&Node::Stmt(s.id)) {
                *count.entry(*b).or_default() += 1;
                fns.insert(*b, f);
            }
        }
        StmtKind::Type(t) => {
            if let Some(b) = res.decl.get(&Node::Stmt(s.id)) {
                *count.entry(*b).or_default() += 1;
                let record = t.alias.is_none() && t.variants.is_empty() && !t.fields.is_empty();
                if record && t.settings.is_empty() && t.annotations.is_empty() {
                    types.insert(*b, t);
                }
            }
        }
        StmtKind::Assign { .. } | StmtKind::VarDecl { .. } => {
            if let Some(a) = res.assigns.get(&s.id) {
                *count.entry(a.target).or_default() += 1;
            }
        }
        _ => {}
    });
    fns.retain(|b, f| count.get(b) == Some(&1) && f.annotations.is_empty());
    types.retain(|b, _| count.get(b) == Some(&1));
    // only types written at the top level of the program, all-int fields,
    // no trait (a trait's methods may be called through any value)
    let eligible: HashMap<BindingId, &TypeDef> = program
        .body
        .iter()
        .filter_map(|s| match &s.kind {
            StmtKind::Type(_) => res.decl.get(&Node::Stmt(s.id)).copied(),
            _ => None,
        })
        .filter_map(|b| types.get(&b).map(|t| (b, *t)))
        .filter(|(b, t)| {
            t.trait_name.is_none() && t.fields.iter().all(|f| is_int_type(&f.ty)) && !res.bindings[*b].name.starts_with("__")
        })
        .collect();
    if eligible.is_empty() {
        return None;
    }
    // nothing to do unless some variable is given a value of such a type
    let mut any = false;
    crate::checker::walk_stmts(&program.body, true, &mut |s| {
        let value = match &s.kind {
            StmtKind::Assign { value, .. } | StmtKind::VarDecl { value, .. } => value,
            _ => return,
        };
        if let ExprKind::Call { func, .. } = &value.kind {
            any |= single(res, func).is_some_and(|tb| eligible.contains_key(&tb));
        }
    });
    if !any {
        return None;
    }
    let ints = Ints::new(program, res, &fns, &types);
    // the calls `fake` replaces in tests
    let mut fakes: HashSet<String> = HashSet::new();
    crate::checker::walk_stmts(&program.body, true, &mut |s| {
        if let StmtKind::Expr(Expr { kind: ExprKind::Call { func, args }, .. }) = &s.kind {
            if matches!(&func.kind, ExprKind::Name(n) if n == "__fake") {
                if let Some(Arg { value: Expr { kind: ExprKind::Str(parts), .. }, .. }) = args.first() {
                    if let [StrPiece::Lit(n)] = parts.as_slice() {
                        fakes.insert(n.clone());
                    }
                }
            }
        }
    });
    let methods_ok: HashMap<(BindingId, String), bool> = eligible
        .iter()
        .flat_map(|(tb, t)| t.methods.iter().map(move |m| ((*tb, m.name.clone()), m)))
        .map(|(k, m)| {
            let t = eligible[&k.0];
            let ok = method_ok(res, &ints, t, m);
            (k, ok)
        })
        .collect();

    // each assignment: the variable, if it is `name = T(args)` of an eligible type
    let mut var_type: HashMap<BindingId, BindingId> = HashMap::new();
    let mut defs_of: HashMap<BindingId, Vec<u32>> = HashMap::new();
    let mut bad: HashSet<BindingId> = HashSet::new();
    crate::checker::walk_stmts(&program.body, true, &mut |s| {
        let Some(a) = res.assigns.get(&s.id) else { return };
        let mut all: Vec<BindingId> = a.cands.iter().map(|(b, _)| *b).collect();
        all.push(a.target);
        all.sort();
        all.dedup();
        let (value, simple) = match &s.kind {
            StmtKind::Assign { target, op, value, ty } => {
                (value, matches!(target.kind, ExprKind::Name(_)) && *op == "=" && ty.is_none())
            }
            StmtKind::VarDecl { value, ty, .. } => (value, ty.is_none()),
            _ => return,
        };
        if all.len() != 1 || !simple {
            bad.extend(all);
            return;
        }
        let b = all[0];
        let ok = match &value.kind {
            ExprKind::Call { func, args } => match single(res, func) {
                Some(tb) if eligible.contains_key(&tb) => {
                    let t = eligible[&tb];
                    let shape = args.len() == t.fields.len()
                        && args.iter().all(|a| a.name.is_none() && ints.certain(&a.value) && !mentions(res, &a.value, b));
                    if shape && var_type.get(&b).is_none_or(|x| *x == tb) {
                        var_type.insert(b, tb);
                        true
                    } else {
                        false
                    }
                }
                _ => false,
            },
            _ => false,
        };
        if ok {
            defs_of.entry(b).or_default().push(s.id);
        } else {
            bad.insert(b);
        }
    });
    // the variables: locals no other frame sees, a constant assigned once
    let mut walk = Walk {
        res,
        stack: Vec::new(),
        next_block: 0,
        first: HashMap::new(),
        bad: HashSet::new(),
        in_module: false,
        module_vars: HashSet::new(),
    };
    walk.block(&program.body);
    var_type.retain(|b, _| {
        let binding = &res.bindings[*b];
        // names of the top level that a function uses are static; any
        // other name another frame uses is captured
        let local = !res.is_static(*b) && !binding.captured;
        let once = binding.kind == BKind::Var || defs_of.get(b).is_some_and(|d| d.len() == 1);
        local
            && once
            && !bad.contains(b)
            && !walk.bad.contains(b)
            && !walk.module_vars.contains(b)
            && !binding.shared
            && !binding.entry
            && binding.origin == Origin::Plain
    });
    // every use is a field read or a call of a method that qualifies
    let mut reads = HashMap::new();
    let mut calls = HashMap::new();
    let mut allowed: HashSet<u32> = HashSet::new();
    let mut writes: HashSet<BindingId> = HashSet::new();
    crate::checker::walk_stmts(&program.body, true, &mut |s| {
        if let StmtKind::Assign { target, .. } = &s.kind {
            if let ExprKind::Attr { obj, .. } | ExprKind::Index { obj, .. } = &target.kind {
                if let Some(b) = single(res, obj) {
                    writes.insert(b);
                }
            }
        }
        crate::parser::each_stmt_expr(s, &mut |e| {
            visit(e, &mut |x| match &x.kind {
                ExprKind::Attr { obj, name, .. } => {
                    if let Some(b) = single(res, obj).filter(|b| var_type.contains_key(b)) {
                        let t = eligible[&var_type[&b]];
                        if t.fields.iter().any(|f| &f.name == name) {
                            reads.insert(x.id, (b, name.clone()));
                            allowed.insert(obj.id);
                        }
                    }
                }
                ExprKind::Call { func, args } => {
                    if let ExprKind::Attr { obj, name, .. } = &func.kind {
                        if let Some(b) = single(res, obj).filter(|b| var_type.contains_key(b)) {
                            let tb = var_type[&b];
                            let t = eligible[&tb];
                            // a call a test fakes keeps its name (fakes are
                            // found by the text of the call)
                            let fits = !fakes.contains(&crate::checker::callee_name(func))
                                && methods_ok.get(&(tb, name.clone())) == Some(&true)
                                && t.methods.iter().find(|m| &m.name == name).is_some_and(|m| m.params.len() == args.len() + 1)
                                && args.iter().all(|a| a.name.is_none() && ints.certain(&a.value));
                            if fits {
                                calls.insert(x.id, (b, name.clone()));
                                allowed.insert(obj.id);
                            }
                        }
                    }
                }
                _ => {}
            });
        });
    });
    let mut used_wrong: HashSet<BindingId> = writes;
    crate::checker::walk_stmts(&program.body, true, &mut |s| {
        // the name an assignment gives a value to is checked with the
        // assignments above
        if let StmtKind::Assign { target, .. } = &s.kind {
            if matches!(target.kind, ExprKind::Name(_)) {
                allowed.insert(target.id);
            }
        }
    });
    crate::checker::walk_stmts(&program.body, true, &mut |s| {
        crate::parser::each_stmt_expr(s, &mut |e| {
            visit(e, &mut |x| {
                if let (ExprKind::Name(_), Some(NameRes::Bindings { cands, row, .. })) = (&x.kind, res.names.get(&x.id)) {
                    for b in cands {
                        if var_type.contains_key(b) && (cands.len() != 1 || row.is_some() || !allowed.contains(&x.id)) {
                            used_wrong.insert(*b);
                        }
                    }
                }
            });
        });
    });
    var_type.retain(|b, _| !used_wrong.contains(b));
    if var_type.is_empty() {
        return None;
    }
    reads.retain(|_, (b, _)| var_type.contains_key(b));
    calls.retain(|_, (b, _)| var_type.contains_key(b));
    let mut methods: Vec<(String, String)> = Vec::new();
    for (b, m) in calls.values() {
        let tname = eligible[&var_type[b]].name.clone();
        if !methods.contains(&(tname.clone(), m.clone())) {
            methods.push((tname, m.clone()));
        }
    }
    methods.sort();
    let mut defs = HashMap::new();
    for (b, ids) in &defs_of {
        if var_type.contains_key(b) {
            for id in ids {
                defs.insert(*id, *b);
            }
        }
    }
    let vars = var_type
        .iter()
        .map(|(b, tb)| {
            let t = eligible[tb];
            (*b, (t.name.clone(), t.fields.iter().map(|f| f.name.clone()).collect()))
        })
        .collect();
    Some(Plan { vars, reads, calls, defs, methods })
}

struct Ids(u32);

impl Ids {
    fn next(&mut self) -> u32 {
        self.0 += 1;
        self.0
    }
}

fn name(ids: &mut Ids, span: Span, n: &str) -> Expr {
    Expr { id: ids.next(), span, kind: ExprKind::Name(n.to_string()) }
}

/// Every expression of a block, children first, mutably.
fn each_expr_mut(body: &mut [Stmt], f: &mut dyn FnMut(&mut Expr)) {
    for s in body.iter_mut() {
        stmt_exprs_mut(s, f);
    }
}

fn stmt_exprs_mut(s: &mut Stmt, f: &mut dyn FnMut(&mut Expr)) {
    match &mut s.kind {
        StmtKind::Expr(e) | StmtKind::Expect(e) | StmtKind::Ensure(e) | StmtKind::Return(Some(e)) => expr_mut(e, f),
        StmtKind::Assign { target, value, .. } => {
            expr_mut(target, f);
            expr_mut(value, f);
        }
        StmtKind::VarDecl { value, .. } | StmtKind::Shared { value, .. } => expr_mut(value, f),
        StmtKind::Assert { expr, .. } => expr_mut(expr, f),
        StmtKind::Spawn { call } => expr_mut(call, f),
        StmtKind::Emit { args, .. } => args.iter_mut().for_each(|a| expr_mut(&mut a.value, f)),
        StmtKind::Fn(d) => each_expr_mut(&mut d.body, f),
        StmtKind::Type(t) => {
            for m in t.methods.iter_mut() {
                each_expr_mut(&mut m.body, f);
            }
        }
        StmtKind::If { branches, orelse } => {
            for (c, b) in branches.iter_mut() {
                expr_mut(c, f);
                each_expr_mut(b, f);
            }
            if let Some(b) = orelse {
                each_expr_mut(b, f);
            }
        }
        StmtKind::For { iterable: e, body, .. } | StmtKind::While { cond: e, body } | StmtKind::Repeat { count: e, body } => {
            expr_mut(e, f);
            each_expr_mut(body, f);
        }
        StmtKind::Match { subject, arms } => {
            expr_mut(subject, f);
            for a in arms.iter_mut() {
                if let Some(g) = &mut a.guard {
                    expr_mut(g, f);
                }
                each_expr_mut(&mut a.body, f);
            }
        }
        StmtKind::Try { within, body, handlers } => {
            if let Some(w) = within {
                expr_mut(w, f);
            }
            each_expr_mut(body, f);
            for h in handlers.iter_mut() {
                each_expr_mut(&mut h.body, f);
            }
        }
        StmtKind::Select { arms } => {
            for a in arms.iter_mut() {
                if let Some(c) = &mut a.channel {
                    expr_mut(c, f);
                }
                if let Some(x) = &mut a.after {
                    expr_mut(x, f);
                }
                each_expr_mut(&mut a.body, f);
            }
        }
        StmtKind::Parallel(body) | StmtKind::Test { body, .. } => each_expr_mut(body, f),
        StmtKind::Event { params, body, .. } => {
            params.iter_mut().for_each(|a| expr_mut(&mut a.value, f));
            each_expr_mut(body, f);
        }
        StmtKind::Module { body: Some(body), .. } => each_expr_mut(body, f),
        _ => {}
    }
}

fn expr_mut(e: &mut Expr, f: &mut dyn FnMut(&mut Expr)) {
    match &mut e.kind {
        ExprKind::Str(parts) => {
            for p in parts.iter_mut() {
                if let StrPiece::Expr(x) = p {
                    expr_mut(x, f);
                }
            }
        }
        ExprKind::List(items) => items.iter_mut().for_each(|x| expr_mut(x, f)),
        ExprKind::Map(pairs) => {
            for (k, v) in pairs.iter_mut() {
                expr_mut(k, f);
                expr_mut(v, f);
            }
        }
        ExprKind::Comprehension { element, iterable, condition, .. } => {
            expr_mut(iterable, f);
            expr_mut(element, f);
            if let Some(c) = condition {
                expr_mut(c, f);
            }
        }
        ExprKind::Attr { obj, .. } => expr_mut(obj, f),
        ExprKind::Index { obj, index } => {
            expr_mut(obj, f);
            expr_mut(index, f);
        }
        ExprKind::Call { func, args } => {
            expr_mut(func, f);
            args.iter_mut().for_each(|a| expr_mut(&mut a.value, f));
        }
        ExprKind::Propagate(x) | ExprKind::Unary { operand: x, .. } => expr_mut(x, f),
        ExprKind::BinOp { left, right, .. } | ExprKind::BoolOp { left, right, .. } | ExprKind::Coalesce { left, right } => {
            expr_mut(left, f);
            expr_mut(right, f);
        }
        ExprKind::Compare { operands, .. } => operands.iter_mut().for_each(|x| expr_mut(x, f)),
        ExprKind::Range { start, end } => {
            expr_mut(start, f);
            expr_mut(end, f);
        }
        ExprKind::Lambda { body, .. } => expr_mut(body, f),
        ExprKind::Pipeline { source, steps } => {
            expr_mut(source, f);
            for st in steps.iter_mut() {
                if let Some(x) = &mut st.expr {
                    expr_mut(x, f);
                }
                st.columns.iter_mut().for_each(|c| expr_mut(c, f));
            }
        }
        ExprKind::ParallelFor { iterable, args, body, .. } => {
            expr_mut(iterable, f);
            args.iter_mut().for_each(|a| expr_mut(&mut a.value, f));
            each_expr_mut(body, f);
        }
        _ => {}
    }
    f(e);
}

/// New ids for every node of a copied method body (the simple statements
/// and expressions `method_ok` allows).
fn renumber(body: &mut [Stmt], ids: &mut Ids) {
    for s in body.iter_mut() {
        s.id = ids.next();
        match &mut s.kind {
            StmtKind::If { branches, orelse } => {
                for (_, b) in branches.iter_mut() {
                    renumber(b, ids);
                }
                if let Some(b) = orelse {
                    renumber(b, ids);
                }
            }
            StmtKind::For { body, .. } | StmtKind::While { body, .. } | StmtKind::Repeat { body, .. } => renumber(body, ids),
            _ => {}
        }
    }
    each_expr_mut(body, &mut |e| e.id = ids.next());
}

/// The block with each variable's assignments written per field, and its
/// reads and method calls on the fields.
fn rewrite_block(body: &mut Vec<Stmt>, plan: &Plan, ids: &mut Ids) {
    let old = std::mem::take(body);
    for mut s in old {
        if let Some(b) = plan.defs.get(&s.id) {
            let (_, fields) = &plan.vars[b];
            let (args, var) = match s.kind {
                StmtKind::Assign { value: Expr { kind: ExprKind::Call { args, .. }, .. }, .. } => (args, false),
                StmtKind::VarDecl { value: Expr { kind: ExprKind::Call { args, .. }, .. }, .. } => (args, true),
                _ => unreachable!(),
            };
            // every argument is evaluated before any field changes, as the
            // record would be built before it is assigned: a failure in a
            // later argument leaves the fields as they were
            let mut values = Vec::new();
            for (f, a) in fields.iter().zip(args) {
                let value = if fields.len() > 1 {
                    let tmp = format!("__urt{}_{f}", s.id);
                    let kind = StmtKind::Assign { target: name(ids, a.span, &tmp), op: "=", value: a.value, ty: None };
                    let mut st = Stmt { id: ids.next(), span: s.span, kind };
                    rewrite_exprs(&mut st, plan, ids);
                    body.push(st);
                    name(ids, a.span, &tmp)
                } else {
                    a.value
                };
                values.push((f, a.span, value));
            }
            for (f, span, value) in values {
                let n = field_var(*b, f);
                let kind = if var {
                    StmtKind::VarDecl { name: n, name_span: span, ty: None, value }
                } else {
                    StmtKind::Assign { target: name(ids, span, &n), op: "=", value, ty: None }
                };
                let mut st = Stmt { id: ids.next(), span: s.span, kind };
                rewrite_exprs(&mut st, plan, ids);
                body.push(st);
            }
            continue;
        }
        rewrite_nested(&mut s, plan, ids);
        rewrite_exprs(&mut s, plan, ids);
        body.push(s);
    }
}

fn rewrite_nested(s: &mut Stmt, plan: &Plan, ids: &mut Ids) {
    match &mut s.kind {
        StmtKind::Fn(d) => rewrite_block(&mut d.body, plan, ids),
        StmtKind::Type(t) => {
            for m in t.methods.iter_mut() {
                rewrite_block(&mut m.body, plan, ids);
            }
        }
        StmtKind::If { branches, orelse } => {
            for (_, b) in branches.iter_mut() {
                rewrite_block(b, plan, ids);
            }
            if let Some(b) = orelse {
                rewrite_block(b, plan, ids);
            }
        }
        StmtKind::For { body, .. }
        | StmtKind::While { body, .. }
        | StmtKind::Repeat { body, .. }
        | StmtKind::Parallel(body)
        | StmtKind::Test { body, .. }
        | StmtKind::Event { body, .. } => rewrite_block(body, plan, ids),
        StmtKind::Match { arms, .. } => {
            for a in arms.iter_mut() {
                rewrite_block(&mut a.body, plan, ids);
            }
        }
        StmtKind::Try { body, handlers, .. } => {
            rewrite_block(body, plan, ids);
            for h in handlers.iter_mut() {
                rewrite_block(&mut h.body, plan, ids);
            }
        }
        StmtKind::Select { arms } => {
            for a in arms.iter_mut() {
                rewrite_block(&mut a.body, plan, ids);
            }
        }
        StmtKind::Module { body: Some(body), .. } => rewrite_block(body, plan, ids),
        _ => {}
    }
}

/// The reads and calls in the statement's own expressions (its blocks are
/// done by `rewrite_nested`).
fn rewrite_exprs(s: &mut Stmt, plan: &Plan, ids: &mut Ids) {
    let mut f = |e: &mut Expr| {
        if let Some((b, field)) = plan.reads.get(&e.id) {
            *e = name(ids, e.span, &field_var(*b, field));
        } else if let Some((b, m)) = plan.calls.get(&e.id) {
            let ExprKind::Call { func, args } = &mut e.kind else { return };
            let (tname, fields) = &plan.vars[b];
            let mut all: Vec<Arg> = fields.iter().map(|f| Arg { span: func.span, name: None, value: name(ids, func.span, &field_var(*b, f)) }).collect();
            all.append(args);
            let func = name(ids, func.span, &method_fn(tname, m));
            *e = Expr { id: ids.next(), span: e.span, kind: ExprKind::Call { func: Box::new(func), args: all } };
        }
    };
    match &mut s.kind {
        // the blocks of these are rewritten on their own
        StmtKind::If { branches, .. } => branches.iter_mut().for_each(|(c, _)| expr_mut(c, &mut f)),
        StmtKind::For { iterable: e, .. } | StmtKind::While { cond: e, .. } | StmtKind::Repeat { count: e, .. } => expr_mut(e, &mut f),
        StmtKind::Match { subject, arms } => {
            expr_mut(subject, &mut f);
            for a in arms.iter_mut() {
                if let Some(g) = &mut a.guard {
                    expr_mut(g, &mut f);
                }
            }
        }
        StmtKind::Try { within: Some(w), .. } => expr_mut(w, &mut f),
        StmtKind::Fn(_) | StmtKind::Type(_) | StmtKind::Parallel(_) | StmtKind::Test { .. } | StmtKind::Module { .. } | StmtKind::Try { .. } => {}
        StmtKind::Select { arms } => {
            for a in arms.iter_mut() {
                if let Some(c) = &mut a.channel {
                    expr_mut(c, &mut f);
                }
                if let Some(x) = &mut a.after {
                    expr_mut(x, &mut f);
                }
            }
        }
        StmtKind::Event { params, .. } => params.iter_mut().for_each(|a| expr_mut(&mut a.value, &mut f)),
        _ => stmt_exprs_mut(s, &mut f),
    }
}

/// The function a method becomes: the fields first, as ints.
fn method_function(t: &TypeDef, m: &FnDef, ids: &mut Ids) -> Stmt {
    let mut body = m.body.clone();
    let fields: Vec<String> = t.fields.iter().map(|f| f.name.clone()).collect();
    each_expr_mut(&mut body, &mut |e| {
        if let ExprKind::Attr { obj, name: field, .. } = &e.kind {
            if matches!(&obj.kind, ExprKind::Name(n) if n == "self") && fields.contains(field) {
                e.kind = ExprKind::Name(self_field(field));
            }
        }
    });
    renumber(&mut body, ids);
    let mut params: Vec<Param> = t
        .fields
        .iter()
        .map(|f| Param { span: f.span, name: self_field(&f.name), ty: Some(f.ty.clone()), default: None })
        .collect();
    params.extend(m.params[1..].iter().cloned());
    let f = FnDef {
        id: ids.next(),
        span: m.span,
        name: method_fn(&t.name, &m.name),
        name_span: m.name_span,
        params,
        ret: m.ret.clone(),
        body,
        is_pub: false,
        annotations: Vec::new(),
    };
    Stmt { id: ids.next(), span: m.span, kind: StmtKind::Fn(f) }
}

/// Rewrite the program; true when anything changed (it must then be
/// resolved again). `next_id` is the first free node id.
pub fn unbox(program: &mut Program, res: &Resolution, next_id: &mut u32) -> bool {
    let Some(plan) = plan(program, res) else { return false };
    let mut ids = Ids(*next_id);
    // the functions of the methods, right after their type
    let mut out = Vec::new();
    for s in std::mem::take(&mut program.body) {
        let extra: Vec<Stmt> = match &s.kind {
            StmtKind::Type(t) => plan
                .methods
                .iter()
                .filter(|(tn, _)| *tn == t.name)
                .filter_map(|(_, m)| t.methods.iter().find(|x| &x.name == m))
                .map(|m| method_function(t, m, &mut ids))
                .collect(),
            _ => Vec::new(),
        };
        out.push(s);
        out.extend(extra);
    }
    program.body = out;
    rewrite_block(&mut program.body, &plan, &mut ids);
    *next_id = ids.0 + 1;
    true
}
