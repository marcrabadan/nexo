//! Conservative opt-in effects over linked ASTs. Reports never grant authority.
use crate::ast::*;
use crate::json::{n, obj, s, Json};
use nexo_diag::{Diagnostic, Span};
use nexo_runtime::security::{Capability, Effect, Engine, Rule};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::rc::Rc;

const MAX_UNITS: usize = 10_000;
const MAX_FACTS: usize = 200_000;

#[derive(Clone)]
enum Binding {
    Function(usize),
    Module(usize),
    Unknown,
    Builtin(String),
}
#[derive(Default)]
struct Env {
    local: BTreeMap<String, Binding>,
    parent: Option<Rc<Env>>,
}
impl Env {
    fn get(&self, name: &str) -> Option<&Binding> {
        self.local
            .get(name)
            .or_else(|| self.parent.as_ref().and_then(|p| p.get(name)))
    }
}
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
struct Fact {
    file: String,
    line: u32,
    col: u32,
    end_col: u32,
    operation: String,
    capability: String,
    target: String,
    reason: String,
}
struct Unit<'a> {
    file: String,
    name: String,
    span: Span,
    body: &'a [Stmt],
    env: Rc<Env>,
    facts: BTreeSet<Fact>,
    calls: BTreeSet<usize>,
    defaults: Vec<&'a Expr>,
}
impl<'a> Unit<'a> {
    fn new(file: String, name: String, span: Span, body: &'a [Stmt]) -> Self {
        Self {
            file,
            name,
            span,
            body,
            env: Rc::new(Env::default()),
            facts: BTreeSet::new(),
            calls: BTreeSet::new(),
            defaults: vec![],
        }
    }
    fn fact(&mut self, span: Span, operation: &str, capability: &str, target: &str, reason: &str) {
        let file = nexo_diag::file_of(span.file)
            .map(|f| f.0.to_string())
            .unwrap_or_else(|| self.file.clone());
        self.facts.insert(Fact {
            file,
            line: span.line,
            col: span.col,
            end_col: span.end_col,
            operation: operation.into(),
            capability: capability.into(),
            target: target.into(),
            reason: reason.into(),
        });
    }
}

/// Statements/expressions evaluated in the current conservative unit.
fn parts(s: &Stmt) -> (Vec<&Expr>, Vec<&[Stmt]>) {
    let mut es = vec![];
    let mut bs = vec![];
    match &s.kind {
        StmtKind::Expr(e)
        | StmtKind::Expect(e)
        | StmtKind::Ensure(e)
        | StmtKind::Assert { expr: e, .. } => es.push(e),
        StmtKind::Assign { target, value, .. } => es.extend([target, value]),
        StmtKind::VarDecl { value, .. } | StmtKind::Shared { value, .. } => es.push(value),
        StmtKind::Return(e) => es.extend(e),
        StmtKind::Spawn { call } => es.push(call),
        StmtKind::Emit { args, .. } => es.extend(args.iter().map(|a| &a.value)),
        StmtKind::Fn(f) => {
            es.extend(f.params.iter().filter_map(|p| p.default.as_ref()));
            es.extend(
                f.annotations
                    .iter()
                    .flat_map(|a| a.args.iter().map(|a| &a.value)),
            );
        }
        StmtKind::Type(t) => es.extend(t.fields.iter().filter_map(|f| f.default.as_ref())),
        StmtKind::If { branches, orelse } => {
            es.extend(branches.iter().map(|(e, _)| e));
            bs.extend(branches.iter().map(|(_, b)| b.as_slice()));
            bs.extend(orelse.iter().map(Vec::as_slice));
        }
        StmtKind::For { iterable, body, .. } => {
            es.push(iterable);
            bs.push(body);
        }
        StmtKind::While { cond, body } => {
            es.push(cond);
            bs.push(body);
        }
        StmtKind::Repeat { count, body } => {
            es.push(count);
            bs.push(body);
        }
        StmtKind::Parallel(b) | StmtKind::Test { body: b, .. } => bs.push(b),
        StmtKind::Event { params, body, .. } => {
            es.extend(params.iter().map(|a| &a.value));
            bs.push(body);
        }
        StmtKind::Match { subject, arms } => {
            fn literals<'a>(p: &'a Pattern, out: &mut Vec<&'a Expr>) {
                match p {
                    Pattern::Lit(_, e) => out.push(e),
                    Pattern::Ctor(_, _, ps) => {
                        for p in ps {
                            literals(p, out);
                        }
                    }
                    _ => {}
                }
            }
            es.push(subject);
            for a in arms {
                for p in &a.patterns {
                    literals(p, &mut es);
                }
                es.extend(a.guard.as_ref());
                bs.push(&a.body);
            }
        }
        StmtKind::Try {
            within,
            body,
            handlers,
        } => {
            es.extend(within);
            bs.push(body);
            bs.extend(handlers.iter().map(|h| h.body.as_slice()));
        }
        StmtKind::Select { arms } => {
            for a in arms {
                es.extend(a.channel.as_ref());
                es.extend(a.after.as_ref());
                bs.push(&a.body);
            }
        }
        StmtKind::Trait { .. } => {}
        StmtKind::Pass
        | StmtKind::Use { .. }
        | StmtKind::Module { .. }
        | StmtKind::Config { .. } => {}
    }
    (es, bs)
}
fn shallow(body: &[Stmt]) -> Vec<&Stmt> {
    let mut out = vec![];
    let mut todo: Vec<&Stmt> = body.iter().rev().collect();
    while let Some(s) = todo.pop() {
        out.push(s);
        for b in parts(s).1.into_iter().rev() {
            todo.extend(b.iter().rev());
        }
    }
    out
}
fn literal(e: &Expr) -> Option<String> {
    if let ExprKind::Str(pieces) = &e.kind {
        let mut text = String::new();
        for p in pieces {
            if let StrPiece::Lit(s) = p {
                text.push_str(s);
            } else {
                return None;
            }
        }
        Some(text)
    } else {
        None
    }
}
fn literal_value(e: &Expr) -> bool {
    match &e.kind {
        ExprKind::Num { .. } | ExprKind::Unit { .. } | ExprKind::Bool(_) | ExprKind::None => true,
        ExprKind::Str(_) => literal(e).is_some(),
        ExprKind::List(es) => es.iter().all(literal_value),
        ExprKind::Map(es) => es.iter().all(|(k, v)| literal_value(k) && literal_value(v)),
        _ => false,
    }
}
/// Deliberately narrow ASCII URL subset, shared with Python. Never performs DNS.
fn host(url: &str) -> Option<String> {
    if !url.is_ascii()
        || url
            .chars()
            .any(|c| c.is_ascii_whitespace() || c.is_control() || c == '\\')
    {
        return None;
    }
    let (scheme, rest) = url.split_once("://")?;
    if !["http", "https"].contains(&scheme) {
        return None;
    }
    let authority = rest.split(['/', '?', '#']).next()?;
    if authority.contains(['@', '[']) || authority.matches(':').count() > 1 {
        return None;
    }
    let (name, port) = authority
        .split_once(':')
        .map(|(a, b)| (a, Some(b)))
        .unwrap_or((authority, None));
    if name.is_empty()
        || !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-')
    {
        return None;
    }
    if let Some(port) = port {
        if port.is_empty()
            || port.len() > 5
            || !port.chars().all(|c| c.is_ascii_digit())
            || port.parse::<u16>().ok()? == 0
        {
            return None;
        }
    }
    Some(name.to_ascii_lowercase())
}
struct Analysis<'a> {
    units: Vec<Unit<'a>>,
    modules: BTreeMap<String, usize>,
    catalog: Json,
    engine: Engine,
    sources: BTreeMap<String, Vec<Vec<char>>>,
    resolution: &'a crate::resolve::Resolution,
}
impl<'a> Analysis<'a> {
    fn modules(&mut self, body: &'a [Stmt]) {
        for st in shallow(body) {
            if let StmtKind::Module {
                name,
                body: Some(body),
                ..
            } = &st.kind
            {
                if !self.modules.contains_key(name) {
                    let file = crate::modules::shown_path(std::path::Path::new(name));
                    let id = self.units.len();
                    self.modules.insert(name.clone(), id);
                    let text = body
                        .first()
                        .and_then(|st| nexo_diag::file_of(st.span.file))
                        .map(|(_, s)| s)
                        .unwrap_or("");
                    self.sources
                        .insert(file.clone(), crate::lsp::lines_of(text));
                    self.units
                        .push(Unit::new(file, "<module>".into(), Span::new(1, 1, 1), body));
                    self.modules(body);
                }
            }
        }
    }
    fn prepare(&mut self, index: usize, inherited: Option<Rc<Env>>) {
        let mut env = BTreeMap::new();
        let mut declarations: BTreeMap<String, Vec<Option<&'a FnDef>>> = BTreeMap::new();
        let body = self.units[index].body;
        let direct: BTreeSet<u32> = body
            .iter()
            .filter_map(|st| {
                if let StmtKind::Fn(f) = &st.kind {
                    Some(f.id)
                } else {
                    None
                }
            })
            .collect();
        for st in shallow(body) {
            match &st.kind {
                StmtKind::Fn(f) => declarations
                    .entry(f.name.clone())
                    .or_default()
                    .push(Some(f)),
                StmtKind::Assign {
                    target:
                        Expr {
                            kind: ExprKind::Name(name),
                            ..
                        },
                    ..
                }
                | StmtKind::VarDecl { name, .. }
                | StmtKind::Shared { name, .. }
                | StmtKind::For { var: name, .. } => {
                    declarations.entry(name.clone()).or_default().push(None)
                }
                StmtKind::Type(t) => declarations.entry(t.name.clone()).or_default().push(None),
                StmtKind::Trait { name, .. } => {
                    declarations.entry(name.clone()).or_default().push(None)
                }
                StmtKind::Module { name, alias, .. } => {
                    env.insert(
                        alias.clone(),
                        self.modules
                            .get(name)
                            .map(|i| Binding::Module(*i))
                            .unwrap_or(Binding::Unknown),
                    );
                }
                StmtKind::Use {
                    lang: Some(_),
                    name,
                    alias,
                    ..
                } => {
                    env.insert(
                        alias
                            .clone()
                            .unwrap_or_else(|| name.rsplit('.').next().unwrap_or(name).into()),
                        Binding::Unknown,
                    );
                }
                _ => {}
            }
        }
        let mut pending = vec![];
        for (name, decls) in declarations {
            env.insert(name.clone(), Binding::Unknown);
            for f in decls.iter().flatten() {
                if self.units.len() >= MAX_UNITS {
                    let span = self.units[index].span;
                    self.units[index].fact(span, "unknown", "", "", "analysis_limit");
                    continue;
                }
                let label = if self.units[index].name == "<module>" {
                    f.name.clone()
                } else {
                    format!("{}.{}", self.units[index].name, f.name)
                };
                let mut u = Unit::new(self.units[index].file.clone(), label, f.name_span, &f.body);
                u.defaults = f.params.iter().filter_map(|p| p.default.as_ref()).collect();
                let child = self.units.len();
                if !f.annotations.is_empty() {
                    u.fact(f.span, "unknown", "", "", "annotated_function");
                }
                self.units.push(u);
                pending.push((child, *f));
                if decls.len() == 1
                    && direct.contains(&f.id)
                    && !name.starts_with("__")
                    && !self
                        .catalog
                        .get("builtin_roots")
                        .arr()
                        .iter()
                        .any(|v| v.str() == Some(&name))
                    && !crate::resolve::is_builtin(&name)
                    && !crate::resolve::LIBRARY_FUNCTIONS.contains(&name.as_str())
                {
                    env.insert(name.clone(), Binding::Function(child));
                }
            }
        }
        // Conservative shadowing of loop, handler, pattern and row bindings.
        for st in shallow(body) {
            match &st.kind {
                StmtKind::Try { handlers, .. } => {
                    for h in handlers {
                        env.insert(h.var.clone(), Binding::Unknown);
                    }
                }
                StmtKind::Select { arms } => {
                    for a in arms {
                        if let Some(v) = &a.var {
                            env.insert(v.clone(), Binding::Unknown);
                        }
                    }
                }
                StmtKind::Match { arms, .. } => {
                    fn bind(p: &Pattern, env: &mut BTreeMap<String, Binding>) {
                        match p {
                            Pattern::Name(_, n) => {
                                env.insert(n.clone(), Binding::Unknown);
                            }
                            Pattern::Ctor(_, _, ps) => {
                                for p in ps {
                                    bind(p, env);
                                }
                            }
                            _ => {}
                        }
                    }
                    for a in arms {
                        for p in &a.patterns {
                            bind(p, &mut env);
                        }
                    }
                }
                _ => {}
            }
            let mut exprs = parts(st).0;
            while let Some(e) = exprs.pop() {
                match &e.kind {
                    ExprKind::Lambda { param: var, .. }
                    | ExprKind::Comprehension { var, .. }
                    | ExprKind::ParallelFor { var, .. } => {
                        env.insert(var.clone(), Binding::Unknown);
                    }
                    _ => {}
                }
                exprs.extend(expr_children(e));
            }
        }
        self.units[index].env = Rc::new(Env {
            local: env,
            parent: inherited,
        });
        for (child, f) in pending {
            let mut outer = BTreeMap::new();
            for p in &f.params {
                outer.insert(p.name.clone(), Binding::Unknown);
            }
            let outer = Rc::new(Env {
                local: outer,
                parent: Some(self.units[index].env.clone()),
            });
            self.prepare(child, Some(outer));
        }
    }
    fn identity(&self, e: &Expr, env: &Env) -> Binding {
        match &e.kind {
            ExprKind::Name(name) => {
                if let Some((base, tail)) = name.split_once('.') {
                    return self.member(
                        env.get(base)
                            .cloned()
                            .unwrap_or(Binding::Builtin(base.into())),
                        tail,
                    );
                }
                env.get(name)
                    .cloned()
                    .unwrap_or_else(|| match self.resolution.names.get(&e.id) {
                        Some(crate::resolve::NameRes::Builtin(canonical)) => {
                            Binding::Builtin(canonical.clone())
                        }
                        _ => Binding::Unknown,
                    })
            }
            ExprKind::Attr { obj, name, .. } => self.member(self.identity(obj, env), name),
            _ => Binding::Unknown,
        }
    }
    fn member(&self, binding: Binding, member: &str) -> Binding {
        match binding {
            Binding::Builtin(name) => Binding::Builtin(format!("{name}.{member}")),
            Binding::Module(i) => self.units[i]
                .env
                .get(member)
                .cloned()
                .unwrap_or(Binding::Unknown),
            _ => Binding::Unknown,
        }
    }
    fn call(&mut self, index: usize, e: &Expr, func: &Expr, args: &[Arg]) {
        match self.identity(func, &self.units[index].env) {
            Binding::Function(i) => {
                self.units[index].calls.insert(i);
            }
            Binding::Builtin(name) => {
                let pure = self
                    .catalog
                    .get("pure")
                    .arr()
                    .iter()
                    .any(|v| v.str() == Some(&name));
                let specs = self.catalog.get("calls").get(&name).arr().to_vec();
                if (pure || !specs.is_empty()) && args.iter().any(|a| !literal_value(&a.value)) {
                    self.units[index].fact(e.span, "unknown", "", "", "dynamic_value");
                }
                if !pure && specs.is_empty() {
                    self.units[index].fact(e.span, "unknown", "", "", "unmodeled_call");
                }
                if name == "run" {
                    self.units[index].fact(e.span, "unknown", "", "", "native_process");
                }
                for spec in specs {
                    let arg = spec.get("argument").num().map(|n| n as usize);
                    let keyword = spec.get("keyword").str();
                    let value = args
                        .iter()
                        .find(|a| a.name.as_deref().is_some() && a.name.as_deref() == keyword)
                        .map(|a| &a.value)
                        .or_else(|| {
                            arg.and_then(|i| {
                                args.iter()
                                    .filter(|a| a.name.is_none())
                                    .nth(i)
                                    .map(|a| &a.value)
                            })
                        });
                    let mut target = value.and_then(literal);
                    let mut cap = spec.get("capability").str().unwrap_or("");
                    let op = spec.get("operation").str().unwrap();
                    let mode = spec.get("mode").str().unwrap();
                    if mode == "any" {
                        target = Some(String::new());
                    }
                    if mode == "host" {
                        target = target.as_deref().and_then(host);
                    }
                    if mode == "database" {
                        if target.as_deref() == Some(":memory:") {
                            self.units[index].fact(e.span, op, "", "", "");
                            continue;
                        }
                        if let Some(path) = target.as_deref().filter(|t| !t.contains("://")) {
                            self.units[index].fact(e.span, op, "read", path, "");
                            self.units[index].fact(e.span, op, "write", path, "");
                            continue;
                        }
                        cap = if target.is_some() { "net" } else { "" };
                        target = None;
                    }
                    let reason = if target.is_none() || mode == "dynamic" {
                        "dynamic_target"
                    } else {
                        ""
                    };
                    self.units[index].fact(
                        e.span,
                        op,
                        cap,
                        target.as_deref().unwrap_or(""),
                        reason,
                    );
                }
            }
            _ => self.units[index].fact(e.span, "unknown", "", "", "dynamic_call"),
        }
        self.expr(index, func);
        for a in args {
            self.expr(index, &a.value);
        }
    }
    fn expr(&mut self, index: usize, e: &Expr) {
        match &e.kind {
            ExprKind::Call { func, args } => {
                self.call(index, e, func, args);
                return;
            }
            ExprKind::Lambda { .. }
            | ExprKind::Pipeline { .. }
            | ExprKind::Index { .. }
            | ExprKind::Comprehension { .. }
            | ExprKind::ParallelFor { .. } => {
                self.units[index].fact(e.span, "unknown", "", "", "deferred_or_unmodeled_construct")
            }
            ExprKind::Attr { .. } => match self.identity(e, &self.units[index].env) {
                Binding::Unknown => {
                    self.units[index].fact(e.span, "unknown", "", "", "dynamic_member")
                }
                Binding::Builtin(name)
                    if !self
                        .catalog
                        .get("pure")
                        .arr()
                        .iter()
                        .any(|p| p.str() == Some(&name))
                        && self.catalog.get("calls").get(&name).is_null() =>
                {
                    self.units[index].fact(e.span, "unknown", "", "", "unmodeled_member")
                }
                _ => {}
            },
            _ => {}
        }
        if matches!(&e.kind, ExprKind::Str(_)) && literal(e).is_none() {
            self.units[index].fact(e.span, "unknown", "", "", "dynamic_value");
        }
        if let ExprKind::ParallelFor { body, .. } = &e.kind {
            self.body(index, body);
        }
        for child in expr_children(e) {
            self.expr(index, child);
        }
    }
    fn body(&mut self, index: usize, body: &[Stmt]) {
        for st in shallow(body) {
            match &st.kind {
                StmtKind::Module { name, .. } => {
                    if let Some(id) = self.modules.get(name) {
                        self.units[index].calls.insert(*id);
                    }
                }
                StmtKind::Use {
                    lang: Some(lang),
                    name,
                    ..
                } if ["c", "python"].contains(&lang.as_str()) => {
                    self.units[index].fact(st.span, "native.load", lang, name, "");
                    self.units[index].fact(st.span, "unknown", "", "", "native_integration");
                }
                StmtKind::Type(_) | StmtKind::Trait { .. } => {
                    self.units[index].fact(st.span, "unknown", "", "", "type_definition")
                }
                StmtKind::Event { .. }
                | StmtKind::Config { .. }
                | StmtKind::Emit { .. }
                | StmtKind::For { .. }
                | StmtKind::Select { .. }
                | StmtKind::Match { .. } => self.units[index].fact(
                    st.span,
                    "unknown",
                    "",
                    "",
                    "deferred_or_unmodeled_construct",
                ),
                _ => {}
            }
            for e in parts(st).0 {
                self.expr(index, e);
            }
        }
    }
    fn run(mut self) -> Vec<Json> {
        for i in 0..self.units.len() {
            let body = self.units[i].body;
            self.body(i, body);
            for e in self.units[i].defaults.clone() {
                self.expr(i, e);
            }
        }
        let mut reverse = vec![BTreeSet::new(); self.units.len()];
        for (i, u) in self.units.iter().enumerate() {
            for c in &u.calls {
                reverse[*c].insert(i);
            }
        }
        let mut queue: VecDeque<usize> = self
            .units
            .iter()
            .enumerate()
            .filter(|(_, u)| !u.facts.is_empty())
            .map(|(i, _)| i)
            .collect();
        let mut queued: BTreeSet<usize> = queue.iter().copied().collect();
        let mut total: usize = self.units.iter().map(|u| u.facts.len()).sum();
        let mut limited = total > MAX_FACTS
            || self
                .units
                .iter()
                .any(|u| u.facts.iter().any(|f| f.reason == "analysis_limit"));
        while !queue.is_empty() && !limited {
            let child = queue.pop_front().unwrap();
            queued.remove(&child);
            for parent in &reverse[child] {
                let new: Vec<Fact> = self.units[child]
                    .facts
                    .difference(&self.units[*parent].facts)
                    .cloned()
                    .collect();
                total += new.len();
                if total > MAX_FACTS {
                    limited = true;
                    break;
                }
                if !new.is_empty() {
                    self.units[*parent].facts.extend(new);
                    if queued.insert(*parent) {
                        queue.push_back(*parent);
                    }
                }
            }
        }
        if limited {
            for u in &mut self.units {
                u.facts.clear();
                u.fact(u.span, "unknown", "", "", "analysis_limit");
            }
        }
        self.units
            .sort_by(|a, b| (&a.file, a.span.line, &a.name).cmp(&(&b.file, b.span.line, &b.name)));
        self.units
            .iter()
            .map(|u| {
                let mut complete = true;
                let mut denied = false;
                let effects = u
                    .facts
                    .iter()
                    .map(|f| {
                        let status = if !f.reason.is_empty() {
                            complete = false;
                            "unknown"
                        } else if f.capability.is_empty() {
                            "not_applicable"
                        } else if Capability::from_boundary(&f.capability, &f.target)
                            .is_some_and(|cap| !self.engine.evaluate(cap, true, false).denied())
                        {
                            "allowed"
                        } else {
                            denied = true;
                            "denied"
                        };
                        obj(vec![
                            ("operation", s(&f.operation)),
                            ("capability", nullable(&f.capability)),
                            (
                                "target",
                                if !f.capability.is_empty() && f.reason.is_empty() {
                                    s(&f.target)
                                } else {
                                    Json::Null
                                },
                            ),
                            ("status", s(status)),
                            ("reason", nullable(&f.reason)),
                            ("file", s(&f.file)),
                            (
                                "range",
                                crate::lsp::span_range(
                                    self.sources.get(&f.file).unwrap(),
                                    Some(Span::new(f.line, f.col, f.end_col)),
                                ),
                            ),
                        ])
                    })
                    .collect::<Vec<_>>();
                obj(vec![
                    ("file", s(&u.file)),
                    (
                        "range",
                        crate::lsp::span_range(self.sources.get(&u.file).unwrap(), Some(u.span)),
                    ),
                    (
                        "severity",
                        n(if denied {
                            1
                        } else if !complete {
                            2
                        } else {
                            3
                        }),
                    ),
                    ("source", s("nexo")),
                    ("code", s("effects_summary")),
                    (
                        "message",
                        s(Diagnostic::new_spanless("effects_summary").text()),
                    ),
                    (
                        "data",
                        obj(vec![
                            ("fixes", Json::Arr(vec![])),
                            ("effects_version", n(1)),
                            ("function", s(&u.name)),
                            ("complete", Json::Bool(complete)),
                            ("pure", Json::Bool(complete && effects.is_empty())),
                            ("policy_scope", s("root")),
                            ("effects", Json::Arr(effects)),
                        ]),
                    ),
                ])
            })
            .collect()
    }
}
fn nullable(value: &str) -> Json {
    if value.is_empty() {
        Json::Null
    } else {
        s(value)
    }
}
fn expr_children(e: &Expr) -> Vec<&Expr> {
    match &e.kind {
        ExprKind::Call { func, args } => std::iter::once(&**func)
            .chain(args.iter().map(|a| &a.value))
            .collect(),
        ExprKind::Propagate(e) => vec![e],
        ExprKind::Lambda { body, .. } => vec![body],
        ExprKind::Coalesce { left, right } => vec![left, right],
        ExprKind::Pipeline { source, steps } => std::iter::once(&**source)
            .chain(
                steps
                    .iter()
                    .flat_map(|s| s.expr.iter().chain(s.columns.iter())),
            )
            .collect(),
        ExprKind::ParallelFor { iterable, args, .. } => std::iter::once(&**iterable)
            .chain(args.iter().map(|a| &a.value))
            .collect(),
        _ => crate::checker::children(e),
    }
}
pub(crate) fn analyze(source: &str, file: &str) -> Result<Vec<Json>, Diagnostic> {
    let program = crate::linked(file, source)?;
    let res = crate::resolve::resolve(&program)?;
    crate::typecheck::check(&program, &res)?;
    let rules = crate::policy::declared(&program)
        .map(|(_, rules)| rules)
        .unwrap_or_default()
        .into_iter()
        .filter_map(|(effect, kind, targets)| {
            Some(Rule {
                effect: if effect == "allow" {
                    Effect::Allow
                } else {
                    Effect::Deny
                },
                kind: Capability::from_boundary(&kind, "")?.kind(),
                targets,
            })
        })
        .collect();
    let mut analysis = Analysis {
        units: vec![Unit::new(
            file.into(),
            "<module>".into(),
            Span::new(1, 1, 1),
            &program.body,
        )],
        modules: BTreeMap::new(),
        catalog: crate::json::parse(include_str!("../../../nexo/stdlib/effects.json"))
            .expect("checked-in effect catalog"),
        engine: Engine::new(rules),
        resolution: &res,
        sources: BTreeMap::from([(file.to_string(), crate::lsp::lines_of(source))]),
    };
    analysis.modules(&program.body);
    let count = analysis.units.len();
    let mut indices: Vec<usize> = (0..count).collect();
    indices.sort_by_key(|i| (*i != 0, analysis.units[*i].file.clone()));
    for i in indices {
        analysis.prepare(i, None);
    }
    Ok(analysis.run())
}
