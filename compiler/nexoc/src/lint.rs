//! `nexo lint` and `nexo fix` (section 14, D86): the same rules as phase 0
//! (`nexo/lint.py`). Each warning says what is wrong and how to fix it;
//! many carry an edit, and `nexo fix` applies the safe ones (also the safe
//! fixes of compile errors).

use std::collections::{HashMap, HashSet};

use nexo_diag::{msg, Diagnostic, Span};

use crate::ast::*;
use crate::lexer::{Tok, Token};

const SYNC_WORDS: &[&str] = &["spawn", "channel", "select", "shared"];
const LAZY_STEPS: &[StepKind] = &[StepKind::Where, StepKind::Map, StepKind::Pick, StepKind::Group, StepKind::Sort, StepKind::Take];
const FAR: u32 = 100_000_000;

/// Replace the text from (line, col) to (end_line, end_col) (1-based
/// columns in characters, end exclusive) with `text`.
#[derive(Clone, Debug)]
pub struct Edit {
    pub line: u32,
    pub col: u32,
    pub end_line: u32,
    pub end_col: u32,
    pub text: String,
}

#[derive(Clone, Debug)]
pub struct Fix {
    pub edits: Vec<Edit>,
    pub safe: bool,
}

#[derive(Clone, Debug)]
pub struct Finding {
    /// catalog key without "lint."
    pub key: &'static str,
    pub span: Span,
    pub args: Vec<(&'static str, String)>,
    pub fix: Option<Fix>,
}

impl Finding {
    pub fn text(&self) -> String {
        format!("{} {}", msg(&format!("lint.{}", self.key), &self.args), msg(&format!("fix.lint.{}", self.key), &self.args))
    }
}

fn one_line(span: Span) -> bool {
    span.end_col < FAR
}

fn edit_span(span: Span, text: String) -> Edit {
    Edit { line: span.line, col: span.col, end_line: span.line, end_col: span.end_col, text }
}

// ----------------------------------------------------------- walking

#[derive(Clone, Copy)]
enum Node<'a> {
    E(&'a Expr),
    S(&'a Stmt),
}

fn walk_args<'a>(args: &'a [Arg], f: &mut dyn FnMut(Node<'a>)) {
    for a in args {
        walk_expr(&a.value, f);
    }
}

fn walk_fn<'a>(d: &'a FnDef, f: &mut dyn FnMut(Node<'a>)) {
    for p in &d.params {
        if let Some(e) = &p.default {
            walk_expr(e, f);
        }
    }
    walk_body(&d.body, f);
}

fn walk_body<'a>(body: &'a [Stmt], f: &mut dyn FnMut(Node<'a>)) {
    for s in body {
        walk_stmt(s, f);
    }
}

fn walk_pattern<'a>(p: &'a Pattern, f: &mut dyn FnMut(Node<'a>)) {
    match p {
        Pattern::Lit(_, e) => walk_expr(e, f),
        Pattern::Ctor(_, _, args) => args.iter().for_each(|a| walk_pattern(a, f)),
        _ => {}
    }
}

fn walk_arm<'a>(arm: &'a MatchArm, f: &mut dyn FnMut(Node<'a>)) {
    for p in &arm.patterns {
        walk_pattern(p, f);
    }
    if let Some(g) = &arm.guard {
        walk_expr(g, f);
    }
    walk_body(&arm.body, f);
}

fn walk_select<'a>(arm: &'a SelectArm, f: &mut dyn FnMut(Node<'a>)) {
    for e in [&arm.channel, &arm.after].into_iter().flatten() {
        walk_expr(e, f);
    }
    walk_body(&arm.body, f);
}

fn walk_stmt<'a>(s: &'a Stmt, f: &mut dyn FnMut(Node<'a>)) {
    f(Node::S(s));
    match &s.kind {
        StmtKind::Expr(e) | StmtKind::Expect(e) | StmtKind::Ensure(e) => walk_expr(e, f),
        StmtKind::Assert { expr, .. } => walk_expr(expr, f),
        StmtKind::Assign { target, value, .. } => {
            walk_expr(target, f);
            walk_expr(value, f);
        }
        StmtKind::VarDecl { value, .. } | StmtKind::Shared { value, .. } => walk_expr(value, f),
        StmtKind::Return(Some(e)) => walk_expr(e, f),
        StmtKind::Spawn { call } => walk_expr(call, f),
        StmtKind::Select { arms } => arms.iter().for_each(|a| walk_select(a, f)),
        StmtKind::Emit { args, .. } => walk_args(args, f),
        StmtKind::Fn(d) => walk_fn(d, f),
        StmtKind::Type(t) => {
            for fd in &t.fields {
                if let Some(e) = &fd.default {
                    walk_expr(e, f);
                }
            }
            for v in &t.variants {
                for p in &v.params {
                    if let Some(e) = &p.default {
                        walk_expr(e, f);
                    }
                }
            }
            for m in &t.methods {
                walk_fn(m, f);
            }
            for (_, e) in &t.settings {
                walk_expr(e, f);
            }
        }
        StmtKind::If { branches, orelse } => {
            for (c, b) in branches {
                walk_expr(c, f);
                walk_body(b, f);
            }
            if let Some(b) = orelse {
                walk_body(b, f);
            }
        }
        StmtKind::For { iterable, body, .. } => {
            walk_expr(iterable, f);
            walk_body(body, f);
        }
        StmtKind::While { cond: e, body } | StmtKind::Repeat { count: e, body } => {
            walk_expr(e, f);
            walk_body(body, f);
        }
        StmtKind::Match { subject, arms } => {
            walk_expr(subject, f);
            arms.iter().for_each(|a| walk_arm(a, f));
        }
        StmtKind::Try { within, body, handlers } => {
            if let Some(e) = within {
                walk_expr(e, f);
            }
            walk_body(body, f);
            for h in handlers {
                walk_body(&h.body, f);
            }
        }
        StmtKind::Parallel(body) | StmtKind::Test { body, .. } => walk_body(body, f),
        StmtKind::Event { params, body, .. } => {
            walk_args(params, f);
            walk_body(body, f);
        }
        _ => {}
    }
}

fn walk_expr<'a>(e: &'a Expr, f: &mut dyn FnMut(Node<'a>)) {
    f(Node::E(e));
    match &e.kind {
        ExprKind::Str(parts) => {
            for p in parts {
                if let StrPiece::Expr(x) = p {
                    walk_expr(x, f);
                }
            }
        }
        ExprKind::List(items) => items.iter().for_each(|x| walk_expr(x, f)),
        ExprKind::Map(pairs) => {
            for (k, v) in pairs {
                walk_expr(k, f);
                walk_expr(v, f);
            }
        }
        ExprKind::Comprehension { element, iterable, condition, .. } => {
            walk_expr(element, f);
            walk_expr(iterable, f);
            if let Some(c) = condition {
                walk_expr(c, f);
            }
        }
        ExprKind::Attr { obj, .. } => walk_expr(obj, f),
        ExprKind::Index { obj, index } => {
            walk_expr(obj, f);
            walk_expr(index, f);
        }
        ExprKind::Call { func, args } => {
            walk_expr(func, f);
            walk_args(args, f);
        }
        ExprKind::Propagate(x) => walk_expr(x, f),
        ExprKind::Unary { operand, .. } => walk_expr(operand, f),
        ExprKind::BinOp { left, right, .. } | ExprKind::BoolOp { left, right, .. } | ExprKind::Coalesce { left, right } => {
            walk_expr(left, f);
            walk_expr(right, f);
        }
        ExprKind::Compare { operands, .. } => operands.iter().for_each(|x| walk_expr(x, f)),
        ExprKind::Range { start, end } => {
            walk_expr(start, f);
            walk_expr(end, f);
        }
        ExprKind::Lambda { body, .. } => walk_expr(body, f),
        ExprKind::Pipeline { source, steps } => {
            walk_expr(source, f);
            for st in steps {
                if let Some(x) = &st.expr {
                    walk_expr(x, f);
                }
                st.columns.iter().for_each(|x| walk_expr(x, f));
            }
        }
        ExprKind::ParallelFor { iterable, args, body, .. } => {
            walk_expr(iterable, f);
            walk_args(args, f);
            walk_body(body, f);
        }
        _ => {}
    }
}

/// A part of the program whose names are searched.
#[derive(Clone, Copy)]
enum Area<'a> {
    Body(&'a [Stmt]),
    Fn(&'a FnDef),
    Stmt(&'a Stmt),
    Arm(&'a MatchArm),
    Handler(&'a Handler),
    Select(&'a SelectArm),
}

impl<'a> Area<'a> {
    fn key(self) -> usize {
        match self {
            Area::Body(b) => b.as_ptr() as usize,
            Area::Fn(d) => d as *const FnDef as usize,
            Area::Stmt(s) => s as *const Stmt as usize,
            Area::Arm(a) => a as *const MatchArm as usize,
            Area::Handler(h) => h as *const Handler as usize,
            Area::Select(a) => a as *const SelectArm as usize,
        }
    }

    fn walk(self, f: &mut dyn FnMut(Node<'a>)) {
        match self {
            Area::Body(b) => walk_body(b, f),
            Area::Fn(d) => walk_fn(d, f),
            Area::Stmt(s) => walk_stmt(s, f),
            Area::Arm(a) => walk_arm(a, f),
            Area::Handler(h) => walk_body(&h.body, f),
            Area::Select(a) => walk_select(a, f),
        }
    }
}

fn declared_names(body: &[Stmt]) -> HashSet<String> {
    fn pattern(p: &Pattern, found: &mut HashSet<String>) {
        match p {
            Pattern::Name(_, n) => {
                found.insert(n.clone());
            }
            Pattern::Ctor(_, _, args) => args.iter().for_each(|a| pattern(a, found)),
            _ => {}
        }
    }
    let mut found = HashSet::new();
    let mut fn_def = |d: &FnDef, found: &mut HashSet<String>| {
        found.insert(d.name.clone());
        for p in &d.params {
            found.insert(p.name.clone());
        }
    };
    walk_body(body, &mut |n| match n {
        Node::S(s) => match &s.kind {
            StmtKind::Fn(d) => fn_def(d, &mut found),
            StmtKind::Type(t) => {
                found.insert(t.name.clone());
                for m in &t.methods {
                    fn_def(m, &mut found);
                }
                for v in &t.variants {
                    for p in &v.params {
                        found.insert(p.name.clone());
                    }
                }
            }
            StmtKind::Trait { name, .. } => {
                found.insert(name.clone());
            }
            StmtKind::VarDecl { name, .. } | StmtKind::Shared { name, .. } | StmtKind::For { var: name, .. } => {
                found.insert(name.clone());
            }
            StmtKind::Assign { target, .. } => {
                if let ExprKind::Name(name) = &target.kind {
                    found.insert(name.clone());
                }
            }
            StmtKind::Match { arms, .. } => {
                for a in arms {
                    for p in &a.patterns {
                        pattern(p, &mut found);
                    }
                }
            }
            StmtKind::Try { handlers, .. } => {
                for h in handlers {
                    found.insert(h.var.clone());
                }
            }
            StmtKind::Select { arms } => {
                for a in arms {
                    if let Some(v) = &a.var {
                        found.insert(v.clone());
                    }
                }
            }
            _ => {}
        },
        Node::E(e) => match &e.kind {
            ExprKind::Comprehension { var, .. } | ExprKind::ParallelFor { var, .. } | ExprKind::Lambda { param: var, .. } => {
                found.insert(var.clone());
            }
            _ => {}
        },
    });
    found
}

fn root_name(e: &Expr) -> Option<&str> {
    let mut e = e;
    loop {
        match &e.kind {
            ExprKind::Attr { obj, .. } | ExprKind::Index { obj, .. } => e = obj,
            ExprKind::Name(n) => return Some(n),
            _ => return None,
        }
    }
}

fn is_fail(s: &Stmt) -> bool {
    matches!(&s.kind, StmtKind::Expr(Expr { kind: ExprKind::Call { func, .. }, .. })
        if matches!(&func.kind, ExprKind::Name(n) if n == "fail"))
}

fn has_effect(e: &Expr) -> bool {
    matches!(e.kind, ExprKind::Call { .. } | ExprKind::Pipeline { .. } | ExprKind::ParallelFor { .. } | ExprKind::Propagate(_))
}

/// An expression whose value is all it does.
fn pure(e: &Expr) -> bool {
    if has_effect(e) || matches!(e.kind, ExprKind::Lambda { .. }) {
        return false;
    }
    if let ExprKind::Str(parts) = &e.kind {
        return parts.iter().all(|p| match p {
            StrPiece::Lit(_) => true,
            StrPiece::Expr(x) => pure(x),
        });
    }
    let mut found = false;
    walk_expr(e, &mut |n| {
        if let Node::E(x) = n {
            if has_effect(x) {
                found = true;
            }
        }
    });
    !found
}

fn literal_key(e: &Expr) -> Option<String> {
    match &e.kind {
        ExprKind::Str(parts) => {
            let mut s = String::from("s:");
            for p in parts {
                match p {
                    StrPiece::Lit(l) => s.push_str(l),
                    StrPiece::Expr(_) => return None,
                }
            }
            Some(s)
        }
        ExprKind::Num { text, .. } => Some(format!("n:{text}")),
        ExprKind::Bool(b) => Some(format!("b:{b}")),
        _ => None,
    }
}

// ------------------------------------------------------------ scopes

struct Scope<'a> {
    area: Area<'a>,
    parent: Option<usize>,
    is_fn: bool,
    /// name -> declared with var
    names: HashMap<String, bool>,
    /// the body of a match arm, `else e` or select arm: new names go to
    /// this enclosing scope (D25)
    outer: Option<usize>,
}

pub struct Linter<'a> {
    lines: Vec<Vec<char>>,
    source: &'a str,
    tokens: &'a [Token],
    program: &'a Program,
    findings: Vec<Finding>,
    type_names: HashSet<String>,
    /// every name the program defines
    declared: HashSet<String>,
    scopes: Vec<Scope<'a>>,
    reads_cache: HashMap<usize, HashSet<String>>,
    writes_cache: HashMap<usize, HashSet<String>>,
}

impl<'a> Linter<'a> {
    pub fn new(source: &'a str, tokens: &'a [Token], program: &'a Program) -> Linter<'a> {
        let mut type_names = HashSet::new();
        walk_body(&program.body, &mut |n| {
            if let Node::S(Stmt { kind: StmtKind::Type(t), .. }) = n {
                type_names.insert(t.name.clone());
                for v in &t.variants {
                    type_names.insert(v.name.clone());
                }
            }
        });
        let declared = declared_names(&program.body);
        Linter {
            declared,
            lines: source.split('\n').map(|l| l.chars().collect()).collect(),
            source,
            tokens,
            program,
            findings: Vec::new(),
            type_names,
            scopes: Vec::new(),
            reads_cache: HashMap::new(),
            writes_cache: HashMap::new(),
        }
    }

    pub fn run(mut self) -> Vec<Finding> {
        let top = self.new_scope(Area::Body(&self.program.body), None, true, None);
        self.block(&self.program.body, top);
        self.use_lines();
        self.expired_flags();
        for (span, tname, missing) in crate::phase5::privacy_gaps(&self.program.body) {
            for key in missing {
                let rule = if key == "keep" { "privacy_keep" } else { "privacy_purpose" };
                self.add(rule, span, None, vec![("type", tname.clone())]);
            }
        }
        self.findings.sort_by(|a, b| (a.span.line, a.span.col, a.key).cmp(&(b.span.line, b.span.col, b.key)));
        self.findings
    }

    /// A flag whose `expires` date has passed (section 22, rule 3); the day
    /// is the one in UTC.
    fn expired_flags(&mut self) {
        let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
        let today = nexo_runtime::time::civil_from_days(secs.div_euclid(86400));
        let mut found = Vec::new();
        for s in &self.program.body {
            let StmtKind::Expr(Expr { kind: ExprKind::Call { func, args }, .. }) = &s.kind else { continue };
            if !matches!(&func.kind, ExprKind::Name(n) if n == "__flags") {
                continue;
            }
            let Some(ExprKind::List(entries)) = args.first().map(|a| &a.value.kind) else { continue };
            for entry in entries {
                let ExprKind::Call { args: eargs, .. } = &entry.kind else { continue };
                let ExprKind::List(phrases) = &eargs[0].value.kind else { continue };
                let Some(ExprKind::List(atoms)) = phrases.first().map(|p| &p.kind) else { continue };
                let [Expr { kind: ExprKind::Str(name), span: name_span, .. }] = atoms.as_slice() else { continue };
                let ExprKind::Map(options) = &eargs[2].value.kind else { continue };
                for (k, v) in options {
                    let is_expires = matches!(&k.kind, ExprKind::Str(p) if matches!(p.as_slice(), [StrPiece::Lit(t)] if t == "expires"));
                    let ExprKind::Call { func: df, args: dargs } = &v.kind else { continue };
                    if !is_expires || !matches!(&df.kind, ExprKind::Name(n) if n == "date") || dargs.len() != 3 {
                        continue;
                    }
                    let nums: Vec<i64> = dargs
                        .iter()
                        .filter_map(|a| match &a.value.kind {
                            ExprKind::Num { text, is_float: false } => text.parse().ok(),
                            _ => None,
                        })
                        .collect();
                    if nums.len() != 3 || !nexo_runtime::time::valid_date(nums[0], nums[1], nums[2]) {
                        continue;
                    }
                    if (nums[0], nums[1], nums[2]) < today {
                        let text: String = name.iter().map(|p| match p {
                            StrPiece::Lit(t) => t.as_str(),
                            _ => "",
                        }).collect();
                        found.push((*name_span, text, format!("{:04}-{:02}-{:02}", nums[0], nums[1], nums[2])));
                    }
                }
            }
        }
        for (span, name, date) in found {
            self.add("flag_expired", span, None, vec![("name", name), ("date", date)]);
        }
    }

    fn add(&mut self, key: &'static str, span: Span, fix: Option<Fix>, args: Vec<(&'static str, String)>) {
        self.findings.push(Finding { key, span, args, fix });
    }

    fn new_scope(&mut self, area: Area<'a>, parent: Option<usize>, is_fn: bool, outer: Option<usize>) -> usize {
        self.scopes.push(Scope { area, parent, is_fn, names: HashMap::new(), outer });
        self.scopes.len() - 1
    }

    fn target(&self, scope: usize) -> usize {
        match self.scopes[scope].outer {
            Some(o) => self.target(o),
            None => scope,
        }
    }

    fn declare(&mut self, scope: usize, name: &str, var: bool) {
        self.scopes[scope].names.insert(name.to_string(), var);
    }

    /// (is var, crossed a function) for a visible name.
    fn find(&self, scope: usize, name: &str) -> Option<(bool, bool)> {
        let mut s = Some(scope);
        let mut crossed = false;
        while let Some(i) = s {
            if let Some(&var) = self.scopes[i].names.get(name) {
                return Some((var, crossed));
            }
            if self.scopes[i].is_fn {
                crossed = true;
            }
            s = self.scopes[i].parent;
        }
        None
    }

    fn reads(&mut self, area: Area<'a>) -> &HashSet<String> {
        let key = area.key();
        if !self.reads_cache.contains_key(&key) {
            let mut targets: HashSet<*const Expr> = HashSet::new();
            area.walk(&mut |n| {
                if let Node::S(Stmt { kind: StmtKind::Assign { target, .. }, .. }) = n {
                    if matches!(target.kind, ExprKind::Name(_)) {
                        targets.insert(target as *const Expr);
                    }
                }
            });
            let mut found = HashSet::new();
            area.walk(&mut |n| match n {
                Node::E(e) => {
                    if let ExprKind::Name(name) = &e.kind {
                        if !targets.contains(&(e as *const Expr)) {
                            found.insert(name.clone());
                        }
                    }
                }
                Node::S(Stmt { kind: StmtKind::Assign { target, op, .. }, .. }) => {
                    if let ExprKind::Name(name) = &target.kind {
                        if *op != "=" {
                            found.insert(name.clone());
                        }
                    }
                }
                Node::S(Stmt { kind: StmtKind::Emit { name, .. }, .. }) => {
                    found.insert(name.clone());
                }
                Node::S(Stmt { kind: StmtKind::Event { name, .. }, .. }) => {
                    found.insert(name.clone());
                }
                _ => {}
            });
            self.reads_cache.insert(key, found);
        }
        &self.reads_cache[&key]
    }

    fn is_read(&mut self, area: Area<'a>, name: &str) -> bool {
        self.reads(area).contains(name)
    }

    /// Names assigned or changed through a place below `area`.
    fn is_written(&mut self, area: Area<'a>, name: &str) -> bool {
        let key = area.key();
        if !self.writes_cache.contains_key(&key) {
            let mut found = HashSet::new();
            area.walk(&mut |n| {
                let root = match n {
                    Node::S(Stmt { kind: StmtKind::Assign { target, .. }, .. }) => root_name(target),
                    Node::E(Expr { kind: ExprKind::Call { func, .. }, .. }) => match &func.kind {
                        ExprKind::Attr { obj, .. } => root_name(obj),
                        _ => None,
                    },
                    _ => None,
                };
                if let Some(r) = root {
                    found.insert(r.to_string());
                }
            });
            self.writes_cache.insert(key, found);
        }
        self.writes_cache[&key].contains(name)
    }

    // -- statements -------------------------------------------------------
    fn block(&mut self, body: &'a [Stmt], scope: usize) {
        let mut ended = false;
        for (i, s) in body.iter().enumerate() {
            if ended {
                self.unreachable(&body[i]);
                break;
            }
            self.stmt(s, scope);
            ended = matches!(s.kind, StmtKind::Return(_)) || is_fail(s);
        }
    }

    fn func(&mut self, d: &'a FnDef, scope: usize) {
        for p in &d.params {
            if let Some(e) = &p.default {
                self.expr(e);
            }
        }
        let inner = self.new_scope(Area::Fn(d), Some(scope), true, None);
        for p in &d.params {
            self.declare(inner, &p.name, false);
        }
        self.block(&d.body, inner);
    }

    fn stmt(&mut self, s: &'a Stmt, scope: usize) {
        match &s.kind {
            StmtKind::Fn(d) => {
                self.declare(scope, &d.name, false);
                self.func(d, scope);
            }
            StmtKind::Type(t) => {
                self.declare(scope, &t.name, false);
                for fd in &t.fields {
                    if let Some(e) = &fd.default {
                        self.expr(e);
                    }
                }
                for m in &t.methods {
                    self.func(m, scope);
                }
            }
            StmtKind::Test { body, .. } => {
                let inner = self.new_scope(Area::Stmt(s), Some(scope), true, None);
                self.block(body, inner);
            }
            StmtKind::Event { params, body, .. } => {
                for a in params {
                    self.expr(&a.value);
                }
                let inner = self.new_scope(Area::Stmt(s), Some(scope), true, None);
                self.block(body, inner);
            }
            StmtKind::Expr(e) => {
                self.expr(e);
                self.useless(e);
            }
            StmtKind::VarDecl { name, name_span, value, .. } => {
                self.expr(value);
                self.var_decl(s, name, *name_span, scope);
            }
            StmtKind::Shared { name, value, .. } => {
                self.expr(value);
                self.declare(scope, name, true);
            }
            StmtKind::Assign { target, op, value, ty } => self.assign(s, target, op, value, ty.is_some(), scope),
            StmtKind::Return(Some(e)) | StmtKind::Expect(e) | StmtKind::Ensure(e) => self.expr(e),
            StmtKind::Assert { expr, .. } => self.expr(expr),
            StmtKind::Emit { args, .. } => {
                for a in args {
                    self.expr(&a.value);
                }
            }
            StmtKind::Spawn { call } => self.expr(call),
            StmtKind::If { branches, orelse } => {
                for (n, (cond, body)) in branches.iter().enumerate() {
                    self.expr(cond);
                    if let ExprKind::Bool(b) = cond.kind {
                        let word = if n > 0 { "elif" } else { "if" };
                        self.add("constant_condition", cond.span, None, vec![("word", word.into()), ("value", b.to_string())]);
                    }
                    self.block(body, scope);
                }
                if let Some(b) = orelse {
                    self.block(b, scope);
                }
            }
            StmtKind::For { var, iterable, body } => {
                self.expr(iterable);
                let inner = self.new_scope(Area::Stmt(s), Some(scope), false, None);
                self.declare(inner, var, false);
                self.block(body, inner);
                self.loop_var(s, var, iterable);
            }
            StmtKind::While { cond, body } => {
                self.expr(cond);
                if let ExprKind::Bool(false) = cond.kind {
                    self.add("constant_condition", cond.span, None, vec![("word", "while".into()), ("value", "false".into())]);
                }
                let inner = self.new_scope(Area::Stmt(s), Some(scope), false, None);
                self.block(body, inner);
            }
            StmtKind::Repeat { count, body } => {
                self.expr(count);
                let inner = self.new_scope(Area::Stmt(s), Some(scope), false, None);
                self.block(body, inner);
            }
            StmtKind::Match { subject, arms } => {
                self.expr(subject);
                for arm in arms {
                    let inner = self.new_scope(Area::Arm(arm), Some(scope), false, None);
                    for p in &arm.patterns {
                        let mut found = Vec::new();
                        self.bindings(p, &mut found);
                        for (name, span) in found {
                            self.declare(inner, &name, false);
                            if !self.is_read(Area::Arm(arm), &name) {
                                let fix = Fix { edits: vec![edit_span(span, "_".into())], safe: true };
                                self.add("unused_binding", span, Some(fix), vec![("name", name)]);
                            }
                        }
                    }
                    if let Some(g) = &arm.guard {
                        self.expr(g);
                    }
                    let body = self.new_scope(Area::Arm(arm), Some(inner), false, Some(scope));
                    self.block(&arm.body, body);
                }
            }
            StmtKind::Try { within, body, handlers } => {
                if let Some(e) = within {
                    self.expr(e);
                }
                self.block(body, scope);
                for h in handlers {
                    let inner = self.new_scope(Area::Handler(h), Some(scope), false, None);
                    self.declare(inner, &h.var, false);
                    let b = self.new_scope(Area::Handler(h), Some(inner), false, Some(scope));
                    self.block(&h.body, b);
                }
            }
            StmtKind::Parallel(body) => self.block(body, scope),
            StmtKind::Select { arms } => {
                for arm in arms {
                    for e in [&arm.channel, &arm.after].into_iter().flatten() {
                        self.expr(e);
                    }
                    let inner = self.new_scope(Area::Select(arm), Some(scope), false, None);
                    if let Some(v) = &arm.var {
                        self.declare(inner, v, false);
                    }
                    let b = self.new_scope(Area::Select(arm), Some(inner), false, Some(scope));
                    self.block(&arm.body, b);
                }
            }
            _ => {}
        }
    }

    fn var_decl(&mut self, s: &'a Stmt, name: &str, name_span: Span, scope: usize) {
        let target = self.target(scope);
        let outer = self.find(scope, name);
        self.declare(target, name, true);
        let area = self.scopes[target].area;
        if !self.is_read(area, name) {
            self.add("unused", name_span, None, vec![("name", name.to_string())]);
            return;
        }
        if outer.is_none() && !self.is_written(area, name) {
            let remove = Edit { line: s.span.line, col: s.span.col, end_line: name_span.line, end_col: name_span.col, text: String::new() };
            let span = Span::new(s.span.line, s.span.col, s.span.col + 3);
            self.add("var_never_changed", span, Some(Fix { edits: vec![remove], safe: true }), vec![("name", name.to_string())]);
        }
    }

    fn assign(&mut self, s: &'a Stmt, target: &'a Expr, op: &str, value: &'a Expr, typed: bool, scope: usize) {
        self.expr(value);
        let ExprKind::Name(name) = &target.kind else {
            self.expr(target);
            return;
        };
        if op != "=" {
            return;
        }
        let found = self.find(scope, name);
        // `x = x` changes nothing only when it assigns the same binding: an
        // immutable name of an enclosing function gets a new local instead (D26)
        let same = matches!(found, Some((true, _)) | Some((_, false)));
        if matches!(&value.kind, ExprKind::Name(v) if v == name) && !typed && same {
            let fix = self.remove_statement(s).map(|e| Fix { edits: vec![e], safe: true });
            self.add("self_assign", target.span, fix, vec![("name", name.clone())]);
            return;
        }
        let declares = typed || found.is_none() || matches!(found, Some((false, true)));
        if !declares {
            return;
        }
        let home = self.target(scope);
        self.declare(home, name, false);
        let area = self.scopes[home].area;
        // names the parser makes for module code (D90) are not the author's
        if !self.is_read(area, name) && !name.starts_with("__") {
            self.add("unused", target.span, None, vec![("name", name.clone())]);
        }
    }

    // -- rules ------------------------------------------------------------
    fn loop_var(&mut self, s: &'a Stmt, var: &str, it: &'a Expr) {
        if self.is_read(Area::Stmt(s), var) {
            return;
        }
        let count = match &it.kind {
            ExprKind::Range { start, end } if matches!(&start.kind, ExprKind::Num { text, .. } if text == "1") => Some(&**end),
            ExprKind::Call { func, args }
                if matches!(&func.kind, ExprKind::Name(n) if n == "range")
                    && args.len() == 1
                    && args[0].name.is_none()
                    && !self.declared.contains("range") =>
            {
                Some(&args[0].value)
            }
            _ => None,
        };
        let Some(count) = count else { return };
        let mut fix = None;
        if it.span.line == s.span.line && count.span.line == s.span.line && it.span.end_col < FAR {
            let text = self.text(count.span);
            let edit = Edit { line: s.span.line, col: s.span.col, end_line: s.span.line, end_col: it.span.end_col, text: format!("repeat {text}") };
            fix = Some(Fix { edits: vec![edit], safe: true });
        }
        let shown = if one_line(count.span) { self.text(count.span) } else { "n".to_string() };
        self.add("loop_var", s.span, fix, vec![("name", var.to_string()), ("count", shown)]);
    }

    fn useless(&mut self, e: &'a Expr) {
        if let ExprKind::Pipeline { steps, .. } = &e.kind {
            let last = steps.last().unwrap();
            if LAZY_STEPS.contains(&last.kind) {
                self.add("lazy_pipeline", last.span, None, vec![("step", last.kind.word().into())]);
            }
            return;
        }
        if pure(e) {
            self.add("useless_expr", e.span, None, vec![]);
        }
    }

    fn unreachable(&mut self, first: &Stmt) {
        let end = self.block_end(first.span);
        let edit = Edit { line: first.span.line, col: 1, end_line: end + 1, end_col: 1, text: String::new() };
        self.add("unreachable", first.span, Some(Fix { edits: vec![edit], safe: true }), vec![]);
    }

    fn use_lines(&mut self) {
        let uses: Vec<&'a Stmt> =
            self.program.body.iter().filter(|s| matches!(&s.kind, StmtKind::Use { what, .. } if what == "nexo.sync")).collect();
        if uses.is_empty() {
            return;
        }
        let mut used = false;
        walk_body(&self.program.body, &mut |n| match n {
            Node::S(Stmt { kind: StmtKind::Spawn { .. } | StmtKind::Select { .. } | StmtKind::Shared { .. }, .. }) => used = true,
            Node::E(Expr { kind: ExprKind::Name(n), .. }) if SYNC_WORDS.contains(&n.as_str()) => used = true,
            _ => {}
        });
        if used {
            return;
        }
        for u in uses {
            let fix = self.remove_statement(u).map(|e| Fix { edits: vec![e], safe: true });
            self.add("unused_use", u.span, fix, vec![("name", "nexo.sync".into())]);
        }
    }

    fn expr(&mut self, e: &'a Expr) {
        let mut nodes = Vec::new();
        walk_expr(e, &mut |n| {
            if let Node::E(x) = n {
                nodes.push(x);
            }
        });
        for n in nodes {
            match &n.kind {
                ExprKind::Compare { ops, operands } if ops.len() == 1 && (ops[0] == "==" || ops[0] == "!=") => {
                    self.compare_bool(n, ops[0], &operands[0], &operands[1])
                }
                ExprKind::Unary { op: "not", operand } => {
                    if let ExprKind::Unary { op: "not", operand: inner } = &operand.kind {
                        let mut fix = None;
                        if one_line(n.span) && one_line(inner.span) {
                            fix = Some(Fix { edits: vec![edit_span(n.span, self.text(inner.span))], safe: true });
                        }
                        self.add("double_not", n.span, fix, vec![]);
                    }
                }
                ExprKind::Map(pairs) => {
                    let mut seen = HashSet::new();
                    for (k, _) in pairs {
                        let Some(key) = literal_key(k) else { continue };
                        if seen.contains(&key) {
                            self.add("duplicate_key", k.span, None, vec![("text", self.text(k.span))]);
                        }
                        seen.insert(key);
                    }
                }
                _ => {}
            }
        }
    }

    fn compare_bool(&mut self, n: &Expr, op: &str, left: &Expr, right: &Expr) {
        let (other, value) = match (&left.kind, &right.kind) {
            (_, ExprKind::Bool(v)) => (left, *v),
            (ExprKind::Bool(v), _) => (right, *v),
            _ => return,
        };
        if matches!(other.kind, ExprKind::Bool(_)) {
            return;
        }
        let positive = value == (op == "==");
        let mut fix = None;
        if one_line(n.span) && one_line(other.span) {
            let mut text = self.text(other.span);
            if !positive {
                text = format!("not {text}");
                let whole = self.text(n.span);
                if whole.starts_with('(') && whole.ends_with(')') {
                    text = format!("({text})");
                }
            }
            fix = Some(Fix { edits: vec![edit_span(n.span, text)], safe: false });
        }
        let shown = if one_line(other.span) { self.text(other.span) } else { "x".to_string() };
        let suggestion = if positive { shown } else { format!("not {shown}") };
        self.add("compare_bool", n.span, fix, vec![("suggestion", suggestion)]);
    }

    fn bindings(&self, p: &Pattern, out: &mut Vec<(String, Span)>) {
        match p {
            Pattern::Name(span, name) if !self.type_names.contains(name) => out.push((name.clone(), *span)),
            Pattern::Ctor(_, _, args) => args.iter().for_each(|a| self.bindings(a, out)),
            _ => {}
        }
    }

    // -- source helpers ---------------------------------------------------
    fn text(&self, span: Span) -> String {
        let Some(line) = self.lines.get(span.line as usize - 1) else { return String::new() };
        let a = (span.col as usize - 1).min(line.len());
        let b = (span.end_col as usize - 1).min(line.len()).max(a);
        line[a..b].iter().collect()
    }

    /// The last line of the block that contains the statement at `span`.
    fn block_end(&self, span: Span) -> u32 {
        let mut depth = 0i32;
        let mut last = span.line;
        let mut started = false;
        for t in self.tokens {
            if !started {
                if (t.span.line, t.span.col) < (span.line, span.col) {
                    continue;
                }
                started = true;
            }
            match &t.tok {
                Tok::Indent => depth += 1,
                Tok::Dedent => {
                    depth -= 1;
                    if depth < 0 {
                        break;
                    }
                }
                Tok::Eof => break,
                Tok::Newline => {}
                other => {
                    last = t.span.line;
                    if matches!(other, Tok::Str(_)) {
                        let line = &self.lines[last as usize - 1];
                        let col = t.span.col as usize - 1;
                        if line.len() >= col + 3 && line[col..col + 3] == ['"', '"', '"'] {
                            let chars: Vec<char> = self.source.chars().collect();
                            let start: usize = self.lines[..last as usize - 1].iter().map(|l| l.len() + 1).sum::<usize>() + col;
                            let mut stop = start + 3;
                            while !(chars[stop] == '"' && chars[stop + 1] == '"' && chars[stop + 2] == '"') {
                                stop += 1;
                            }
                            last += chars[start..stop].iter().filter(|&&c| c == '\n').count() as u32;
                        }
                    }
                }
            }
        }
        last
    }

    /// Remove a one-line statement; `pass` keeps a block that would be
    /// empty. None when the statement goes on to other lines.
    fn remove_statement(&self, s: &Stmt) -> Option<Edit> {
        let line = s.span.line;
        let end = self.tokens.iter().find(|t| matches!(t.tok, Tok::Newline) && (t.span.line, t.span.col) >= (line, s.span.col))?;
        if end.span.line != line {
            return None;
        }
        let text = &self.lines[line as usize - 1];
        let indent = text.iter().take_while(|&&c| c == ' ').count() as u32;
        if alone_in_block(&self.program.body, s, true) == Some(true) {
            return Some(Edit { line, col: indent + 1, end_line: line, end_col: text.len() as u32 + 1, text: "pass".into() });
        }
        Some(Edit { line, col: 1, end_line: line + 1, end_col: 1, text: String::new() })
    }
}

/// Some(alone) when `body` (or a block inside it) holds `s`.
fn alone_in_block(body: &[Stmt], s: &Stmt, top: bool) -> Option<bool> {
    if body.iter().any(|x| std::ptr::eq(x, s)) {
        return Some(body.len() == 1 && !top);
    }
    for x in body {
        let mut blocks: Vec<&[Stmt]> = Vec::new();
        match &x.kind {
            StmtKind::Fn(d) => blocks.push(&d.body),
            StmtKind::Type(t) => blocks.extend(t.methods.iter().map(|m| m.body.as_slice())),
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
            StmtKind::Select { arms } => blocks.extend(arms.iter().map(|a| a.body.as_slice())),
            _ => {}
        }
        for b in blocks {
            if let Some(r) = alone_in_block(b, s, false) {
                return Some(r);
            }
        }
    }
    None
}

// ------------------------------------------------- fixes of compile errors

/// The edit that fixes a compile error, when there is one.
pub fn error_fix(d: &Diagnostic, source: &str) -> Option<Fix> {
    let span = d.span?;
    let lines: Vec<Vec<char>> = source.split('\n').map(|l| l.chars().collect()).collect();
    if !one_line(span) || span.line as usize > lines.len() {
        return None;
    }
    let line = &lines[span.line as usize - 1];
    let at = |l: u32, c: u32, text: &str| Fix { edits: vec![Edit { line: l, col: c, end_line: l, end_col: c, text: text.into() }], safe: true };
    if d.fix == Some("suggest") {
        if let Some(s) = d.arg_value("suggestion") {
            return Some(Fix { edits: vec![edit_span(span, s.to_string())], safe: false });
        }
    }
    match d.key {
        "question_cannot_fail" if span.end_col >= 2 && line.get(span.end_col as usize - 2) == Some(&'?') => {
            Some(Fix { edits: vec![Edit { line: span.line, col: span.end_col - 1, end_line: span.line, end_col: span.end_col, text: String::new() }], safe: true })
        }
        "else_needs_var" => Some(at(span.line, span.end_col, " e")),
        "ignored_error" => Some(Fix { safe: false, ..at(span.line, span.end_col, "?") }),
        "fallible_needs_bang" => {
            let (l, c) = d.arg_value("ret_end")?.split_once(':')?;
            Some(at(l.parse().ok()?, c.parse().ok()?, "!"))
        }
        _ => None,
    }
}

// --------------------------------------------------------------- editing

pub fn apply_edits(source: &str, edits: &[Edit]) -> String {
    let mut chars: Vec<char> = source.chars().collect();
    let lines: Vec<usize> = source.split('\n').map(|l| l.chars().count()).collect();
    let mut starts = vec![0usize];
    for l in &lines {
        starts.push(starts.last().unwrap() + l + 1);
    }
    let offset = |line: u32, col: u32, len: usize| -> usize {
        if line as usize > lines.len() {
            return len;
        }
        (starts[line as usize - 1] + col as usize - 1).min(len)
    };
    let mut sorted: Vec<&Edit> = edits.iter().collect();
    sorted.sort_by(|a, b| (b.line, b.col).cmp(&(a.line, a.col)));
    for e in sorted {
        let a = offset(e.line, e.col, chars.len());
        let b = offset(e.end_line, e.end_col, chars.len());
        chars.splice(a..b, e.text.chars());
    }
    chars.into_iter().collect()
}
