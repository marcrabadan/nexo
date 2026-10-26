//! Modules of the program's own files (section 13, D124). Phase 0's
//! modules.py: `use shop.payments` loads `shop/payments.nexo` from the root
//! of the project (the folder of the nearest nexo.toml above the program,
//! or else the program's own folder), once. Its `pub` functions and types
//! (and the variants of a `pub` enum) are used qualified, `payments.add`.
//!
//! Every file is parsed and checked on its own; then each `use` becomes a
//! `Module` statement that holds the module's code at its first use, and
//! each `payments.add` a name `"payments.add"` that the resolver looks up
//! in the module's scope.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use nexo_diag::{suggest, Diagnostic, Span};

use crate::ast::*;

type LResult<T> = Result<T, Diagnostic>;

pub fn is_file_module(lang: &Option<String>, name: &str) -> bool {
    lang.is_none() && name != "nexo" && !name.starts_with("nexo.")
}

fn bound_name(name: &str, alias: &Option<String>) -> String {
    alias.clone().unwrap_or_else(|| name.rsplit('.').next().unwrap().to_string())
}

fn project_root(file: &str) -> PathBuf {
    let path = Path::new(file);
    let start = path
        .canonicalize()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_default());
    for d in start.ancestors() {
        if d.join("nexo.toml").is_file() {
            return d.to_path_buf();
        }
    }
    start
}

/// The path of a module file as messages show it: relative to the current
/// folder when it is inside it.
fn shown_path(path: &Path) -> String {
    let cwd = std::env::current_dir().ok().and_then(|c| c.canonicalize().ok());
    match cwd.as_deref().and_then(|c| path.strip_prefix(c).ok()) {
        Some(rel) => rel.to_string_lossy().into_owned(),
        None => path.to_string_lossy().into_owned(),
    }
}

struct Info {
    name: String,
    file: String,
    exports: HashSet<String>,
    private: HashSet<String>,
    /// its code, until the first use takes it
    body: Option<Vec<Stmt>>,
}

/// (pub names, other top-level names) of a module.
fn exported(program: &Program) -> (HashSet<String>, HashSet<String>) {
    let mut exports = HashSet::new();
    let mut private = HashSet::new();
    for s in &program.body {
        match &s.kind {
            StmtKind::Fn(f) => {
                if f.is_pub { &mut exports } else { &mut private }.insert(f.name.clone());
            }
            StmtKind::Type(t) => {
                let set = if t.is_pub { &mut exports } else { &mut private };
                set.insert(t.name.clone());
                set.extend(t.variants.iter().map(|v| v.name.clone()));
            }
            StmtKind::Trait { name, .. } => {
                private.insert(name.clone());
            }
            StmtKind::Assign { target: Expr { kind: ExprKind::Name(n), .. }, .. } => {
                private.insert(n.clone());
            }
            StmtKind::VarDecl { name, .. } | StmtKind::Shared { name, .. } => {
                private.insert(name.clone());
            }
            _ => {}
        }
    }
    let private = private.difference(&exports).cloned().collect();
    (exports, private)
}

pub struct Linker {
    root: PathBuf,
    modules: HashMap<String, Info>,
    /// the modules being loaded, outermost first (the program is the first)
    stack: Vec<String>,
    next_id: u32,
    /// ids of the module files, in the order they were loaded
    pub files: Vec<u32>,
}

impl Linker {
    pub fn new(main_file: &str, next_id: u32) -> Linker {
        let root = project_root(main_file);
        let main = Path::new(main_file).canonicalize().unwrap_or_else(|_| PathBuf::from(main_file));
        let first = match main.with_extension("").strip_prefix(&root) {
            Ok(rel) => rel.iter().map(|p| p.to_string_lossy().into_owned()).collect::<Vec<_>>().join("."),
            Err(_) => main.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default(),
        };
        Linker { root, modules: HashMap::new(), stack: vec![first], next_id, files: Vec::new() }
    }

    /// Load the modules a file uses, check the names it takes from them
    /// and turn its `use` lines into `Module` statements.
    pub fn link(&mut self, program: &mut Program) -> LResult<()> {
        let top: HashSet<u32> = program.body.iter().map(|s| s.id).collect();
        let mut nested = None;
        crate::checker::walk_stmts(&program.body, true, &mut |s| {
            if let StmtKind::Use { lang, name, .. } = &s.kind {
                if is_file_module(lang, name) && !top.contains(&s.id) && nested.is_none() {
                    nested = Some(Diagnostic::new("use_top_level", s.span).arg("name", name.clone()));
                }
            }
        });
        if let Some(d) = nested {
            return Err(d);
        }
        let mut aliases: HashMap<String, (String, String)> = HashMap::new();
        for s in program.body.iter_mut() {
            let StmtKind::Use { lang, name, alias, .. } = &s.kind else { continue };
            if !is_file_module(lang, name) {
                continue;
            }
            let (name, alias) = (name.clone(), bound_name(name, alias));
            self.load(&name, s.span)?;
            if let Some((other, _)) = aliases.get(&alias) {
                return Err(Diagnostic::new("module_name_taken", s.span).arg("name", alias).arg("module", other.clone()));
            }
            let info = self.modules.get_mut(&name).unwrap();
            aliases.insert(alias.clone(), (name.clone(), info.file.clone()));
            s.kind = StmtKind::Module { name, alias, body: info.body.take() };
        }
        if !aliases.is_empty() {
            let refs = References { aliases: &aliases, modules: &self.modules };
            for s in program.body.iter_mut() {
                refs.stmt(s)?;
            }
        }
        Ok(())
    }

    fn load(&mut self, name: &str, span: Span) -> LResult<()> {
        if let Some(i) = self.stack.iter().position(|m| m == name) {
            let mut chain = self.stack[i..].to_vec();
            chain.push(name.to_string());
            return Err(Diagnostic::new("module_cycle", span).arg("chain", chain.join(" -> ")));
        }
        if self.modules.contains_key(name) {
            return Ok(());
        }
        let mut path = self.root.clone();
        for part in name.split('.') {
            path.push(part);
        }
        path.set_extension("nexo");
        let file = shown_path(&path);
        let Ok(source) = std::fs::read_to_string(&path) else {
            return Err(Diagnostic::new("module_not_found", span).arg("name", name.to_string()).arg("path", file));
        };
        let id = nexo_diag::add_file(&file, &source);
        self.files.push(id);
        let (mut program, next) = crate::parser::parse_file(&source, id, self.next_id)?;
        self.next_id = next;
        if let Some(s) = program.body.iter().find(|s| crate::parser::is_policy(s)) {
            return Err(Diagnostic::new("policy_module", s.span).arg("module", name.to_string()));
        }
        let folder = path.parent().map(Path::to_path_buf).unwrap_or_default();
        crate::cheader::embed(&mut program, &folder);
        crate::checker::check(&program)?;
        self.stack.push(name.to_string());
        self.link(&mut program)?;
        self.stack.pop();
        let (exports, private) = exported(&program);
        // its tests only run when it is the file tested
        let body = program.body.into_iter().filter(|s| !matches!(s.kind, StmtKind::Test { .. })).collect();
        self.modules.insert(name.to_string(), Info { name: name.to_string(), file, exports, private, body: Some(body) });
        Ok(())
    }
}

/// Checks every use of a module's name in one file, and turns
/// `payments.add` into the name `"payments.add"`.
struct References<'a> {
    /// alias -> (module, file)
    aliases: &'a HashMap<String, (String, String)>,
    modules: &'a HashMap<String, Info>,
}

impl References<'_> {
    fn member(&self, alias: &str, name: &str, span: Span) -> LResult<()> {
        let info = &self.modules[&self.aliases[alias].0];
        if info.exports.contains(name) {
            return Ok(());
        }
        if info.private.contains(name) {
            return Err(Diagnostic::new("module_private", span)
                .arg("name", name.to_string())
                .arg("module", info.name.clone())
                .arg("path", info.file.clone()));
        }
        let mut names: Vec<&str> = info.exports.iter().map(String::as_str).collect();
        names.sort();
        let d = Diagnostic::new("module_no_member", span).arg("name", name.to_string()).arg("module", info.name.clone());
        Err(match suggest(name, names) {
            Some(s) => d.fix("suggest").arg("suggestion", format!("{alias}.{s}")),
            None => d,
        })
    }

    /// A type or pattern name: `payments.Order`.
    fn dotted(&self, name: &str, span: Span) -> LResult<()> {
        match name.split_once('.') {
            Some((alias, member)) if self.aliases.contains_key(alias) => {
                self.member(alias, member.split('.').next().unwrap(), span)
            }
            Some(_) => Ok(()),
            None => self.bind(name, span),
        }
    }

    fn bind(&self, name: &str, span: Span) -> LResult<()> {
        match self.aliases.get(name) {
            Some((module, _)) => {
                Err(Diagnostic::new("module_name_taken", span).arg("name", name.to_string()).arg("module", module.clone()))
            }
            None => Ok(()),
        }
    }

    fn ty(&self, t: &mut TypeExpr) -> LResult<()> {
        match t {
            TypeExpr::Name(n) => {
                if n.name.contains('.') {
                    self.dotted(&n.name, n.span)?;
                }
                for p in n.params.iter_mut() {
                    self.ty(p)?;
                }
            }
            TypeExpr::Union { literals, .. } => {
                for l in literals.iter_mut() {
                    self.expr(l)?;
                }
            }
            TypeExpr::Range { .. } => {}
        }
        Ok(())
    }

    fn pattern(&self, p: &mut Pattern) -> LResult<()> {
        match p {
            Pattern::Name(span, n) => self.dotted(n, *span),
            Pattern::Ctor(span, n, args) => {
                if n.contains('.') {
                    self.dotted(n, *span)?;
                }
                for a in args.iter_mut() {
                    self.pattern(a)?;
                }
                Ok(())
            }
            Pattern::Lit(_, e) => self.expr(e),
            Pattern::Wild(_) => Ok(()),
        }
    }

    fn block(&self, body: &mut [Stmt]) -> LResult<()> {
        for s in body.iter_mut() {
            self.stmt(s)?;
        }
        Ok(())
    }

    fn args(&self, args: &mut [Arg]) -> LResult<()> {
        for a in args.iter_mut() {
            self.expr(&mut a.value)?;
        }
        Ok(())
    }

    fn function(&self, f: &mut FnDef) -> LResult<()> {
        self.bind(&f.name, f.name_span)?;
        for a in f.annotations.iter_mut() {
            self.args(&mut a.args)?;
        }
        for p in f.params.iter_mut() {
            self.bind(&p.name, p.span)?;
            if let Some(t) = p.ty.as_mut() {
                self.ty(t)?;
            }
            if let Some(d) = p.default.as_mut() {
                self.expr(d)?;
            }
        }
        if let Some(t) = f.ret.as_mut() {
            self.ty(t)?;
        }
        self.block(&mut f.body)
    }

    fn stmt(&self, s: &mut Stmt) -> LResult<()> {
        let span = s.span;
        match &mut s.kind {
            StmtKind::Expr(e) | StmtKind::Expect(e) | StmtKind::Ensure(e) | StmtKind::Assert { expr: e, .. } => {
                self.expr(e)?
            }
            StmtKind::Spawn { call } => self.expr(call)?,
            StmtKind::Return(e) => {
                if let Some(e) = e {
                    self.expr(e)?;
                }
            }
            StmtKind::Assign { target, value, ty, .. } => {
                if let ExprKind::Attr { obj, name, .. } = &target.kind {
                    if let ExprKind::Name(a) = &obj.kind {
                        if let Some((module, file)) = self.aliases.get(a) {
                            self.member(a, name, target.span)?;
                            return Err(Diagnostic::new("module_assign", target.span)
                                .arg("name", format!("{a}.{name}"))
                                .arg("module", module.clone())
                                .arg("path", file.clone()));
                        }
                    }
                }
                match &target.kind {
                    ExprKind::Name(n) if self.aliases.contains_key(n) => self.bind(n, target.span)?,
                    _ => self.expr(target)?,
                }
                if let Some(t) = ty.as_mut() {
                    self.ty(t)?;
                }
                self.expr(value)?;
            }
            StmtKind::VarDecl { name, name_span, ty, value } => {
                self.bind(name, *name_span)?;
                if let Some(t) = ty.as_mut() {
                    self.ty(t)?;
                }
                self.expr(value)?;
            }
            StmtKind::Shared { name, name_span, value } => {
                self.bind(name, *name_span)?;
                self.expr(value)?;
            }
            StmtKind::Select { arms } => {
                for a in arms.iter_mut() {
                    if let Some(v) = &a.var {
                        self.bind(v, a.span)?;
                    }
                    for e in [a.channel.as_mut(), a.after.as_mut()].into_iter().flatten() {
                        self.expr(e)?;
                    }
                    self.block(&mut a.body)?;
                }
            }
            StmtKind::Emit { args, .. } => self.args(args)?,
            StmtKind::Fn(f) => self.function(f)?,
            StmtKind::Type(t) => {
                self.bind(&t.name, t.name_span)?;
                if let Some(tr) = &t.trait_name {
                    if tr.contains('.') {
                        self.dotted(tr, t.name_span)?;
                    }
                }
                for a in t.annotations.iter_mut() {
                    self.args(&mut a.args)?;
                }
                if let Some(al) = t.alias.as_mut() {
                    self.ty(al)?;
                }
                for f in t.fields.iter_mut() {
                    self.ty(&mut f.ty)?;
                    if let Some(d) = f.default.as_mut() {
                        self.expr(d)?;
                    }
                }
                for v in t.variants.iter_mut() {
                    self.bind(&v.name, v.span)?;
                    for p in v.params.iter_mut() {
                        self.bind(&p.name, p.span)?;
                        if let Some(ty) = p.ty.as_mut() {
                            self.ty(ty)?;
                        }
                        if let Some(d) = p.default.as_mut() {
                            self.expr(d)?;
                        }
                    }
                }
                for m in t.methods.iter_mut() {
                    self.function(m)?;
                }
                for (_, e) in t.settings.iter_mut() {
                    self.expr(e)?;
                }
            }
            StmtKind::Trait { name, .. } => self.bind(name, span)?,
            StmtKind::If { branches, orelse } => {
                for (c, b) in branches.iter_mut() {
                    self.expr(c)?;
                    self.block(b)?;
                }
                if let Some(b) = orelse {
                    self.block(b)?;
                }
            }
            StmtKind::For { var, iterable, body } => {
                self.bind(var, span)?;
                self.expr(iterable)?;
                self.block(body)?;
            }
            StmtKind::While { cond: e, body } | StmtKind::Repeat { count: e, body } => {
                self.expr(e)?;
                self.block(body)?;
            }
            StmtKind::Match { subject, arms } => {
                self.expr(subject)?;
                for a in arms.iter_mut() {
                    for p in a.patterns.iter_mut() {
                        self.pattern(p)?;
                    }
                    if let Some(g) = a.guard.as_mut() {
                        self.expr(g)?;
                    }
                    self.block(&mut a.body)?;
                }
            }
            StmtKind::Try { within, body, handlers } => {
                if let Some(w) = within {
                    self.expr(w)?;
                }
                self.block(body)?;
                for h in handlers.iter_mut() {
                    self.bind(&h.var, h.span)?;
                    self.block(&mut h.body)?;
                }
            }
            StmtKind::Parallel(body) | StmtKind::Test { body, .. } => self.block(body)?,
            StmtKind::Event { params, body, .. } => {
                self.args(params)?;
                self.block(body)?;
            }
            StmtKind::Pass | StmtKind::Use { .. } | StmtKind::Module { .. } | StmtKind::Config { .. } => {}
        }
        Ok(())
    }

    fn expr(&self, e: &mut Expr) -> LResult<()> {
        let span = e.span;
        match &mut e.kind {
            ExprKind::Attr { obj, name, .. } => {
                if let ExprKind::Name(a) = &obj.kind {
                    if self.aliases.contains_key(a) {
                        self.member(a, name, span)?;
                        e.kind = ExprKind::Name(format!("{a}.{name}"));
                        return Ok(());
                    }
                }
                self.expr(obj)?;
            }
            ExprKind::Name(n) => {
                if self.aliases.contains_key(n) {
                    return Err(Diagnostic::new("module_value", span).arg("name", n.clone()));
                }
            }
            ExprKind::Str(parts) => {
                for p in parts.iter_mut() {
                    if let StrPiece::Expr(x) = p {
                        self.expr(x)?;
                    }
                }
            }
            ExprKind::List(items) => {
                for x in items.iter_mut() {
                    self.expr(x)?;
                }
            }
            ExprKind::Map(pairs) => {
                for (k, v) in pairs.iter_mut() {
                    self.expr(k)?;
                    self.expr(v)?;
                }
            }
            ExprKind::Comprehension { element, var, iterable, condition } => {
                self.bind(var, span)?;
                self.expr(element)?;
                self.expr(iterable)?;
                if let Some(c) = condition {
                    self.expr(c)?;
                }
            }
            ExprKind::Index { obj, index } => {
                self.expr(obj)?;
                self.expr(index)?;
            }
            ExprKind::Call { func, args } => {
                self.expr(func)?;
                self.args(args)?;
            }
            ExprKind::Propagate(x) | ExprKind::Unary { operand: x, .. } => self.expr(x)?,
            ExprKind::BinOp { left, right, .. }
            | ExprKind::BoolOp { left, right, .. }
            | ExprKind::Coalesce { left, right }
            | ExprKind::Range { start: left, end: right } => {
                self.expr(left)?;
                self.expr(right)?;
            }
            ExprKind::Compare { operands, .. } => {
                for x in operands.iter_mut() {
                    self.expr(x)?;
                }
            }
            ExprKind::Lambda { param, body } => {
                self.bind(param, span)?;
                self.expr(body)?;
            }
            ExprKind::Pipeline { source, steps } => {
                self.expr(source)?;
                for st in steps.iter_mut() {
                    if let Some(x) = st.expr.as_mut() {
                        self.expr(x)?;
                    }
                    for c in st.columns.iter_mut() {
                        self.expr(c)?;
                    }
                }
            }
            ExprKind::ParallelFor { var, iterable, args, body } => {
                self.bind(var, span)?;
                self.expr(iterable)?;
                self.args(args)?;
                self.block(body)?;
            }
            ExprKind::Num { .. } | ExprKind::Unit { .. } | ExprKind::Bool(_) | ExprKind::None => {}
        }
        Ok(())
    }
}
