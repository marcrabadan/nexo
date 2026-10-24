//! Name resolution (D25-D29), done before code generation.
//!
//! Functions and each iteration of a loop have their own scope; `if`,
//! `try`, `match` and `parallel:` blocks do not (a name first assigned in
//! them belongs to the enclosing scope), except for the pattern variables
//! of a match arm and the `e` of `else e`.
//!
//! Every place where a name can be bound is a *binding*. A name read
//! resolves to the bindings that could hold it, innermost first; at run time
//! the first one that has been assigned wins, which is the lookup of the
//! phase 0 interpreter. Bindings of a function that a nested function,
//! lambda or pipeline step uses are *captured*: they live in heap cells
//! shared with the closures.

use std::collections::HashMap;

use nexo_diag::{suggest, Diagnostic};

use crate::ast::*;

pub type BindingId = usize;
pub type ScopeId = usize;
pub type FrameId = usize;

pub const GLOBAL_FRAME: FrameId = 0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BKind {
    Var,
    Const,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Origin {
    Plain,
    Param,
    Fn,
    /// a record, enum or alias type
    Type(u32),
    Trait(u32),
    /// a variant of an enum (its id, and whether it has fields)
    Variant(u32, bool),
}

#[derive(Debug)]
pub struct Binding {
    pub name: String,
    pub kind: BKind,
    /// declared type of a `var x: T` (used when it is assigned again)
    pub ty: Option<TypeExpr>,
    pub origin: Origin,
    pub scope: ScopeId,
    pub frame: FrameId,
    pub captured: bool,
    /// bound before its scope runs (parameters, loop and pattern variables)
    pub entry: bool,
    /// a `shared` variable (nexo.sync): used under a lock, read as a copy
    pub shared: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SKind {
    /// a scope that receives new names
    Normal,
    /// only its own pattern/handler variables; new names go outward
    Transparent,
    /// a pipeline step: `it` and the columns of the current row
    Row,
}

#[derive(Debug)]
pub struct Scope {
    pub parent: Option<ScopeId>,
    pub kind: SKind,
    pub frame: FrameId,
    /// true for the outermost scope of a function (crossing it is crossing
    /// a function boundary, D26)
    pub is_fn: bool,
    pub names: Vec<(String, BindingId)>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FrameKind {
    Global,
    Function,
    Lambda,
    Step,
    Default,
    Test,
    Handler,
    /// a task of `parallel for` or `parallel:` (run on another thread)
    Task,
}

#[derive(Debug)]
pub struct Frame {
    pub parent: Option<FrameId>,
    pub kind: FrameKind,
    pub root: ScopeId,
    /// captured bindings of outer functions this frame uses, in order
    pub free: Vec<BindingId>,
    pub params: Vec<BindingId>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Node {
    Stmt(u32),
    Expr(u32),
    Fn(u32),
    Arm(u32, usize),
    Handler(u32, usize),
    Step(u32, usize),
    Column(u32, usize, usize),
    Field(u32, usize),
    /// statement i of a `parallel:` block
    Task(u32, usize),
    /// arm i of a `select:`
    SelectArm(u32, usize),
}

#[derive(Clone, Debug)]
pub enum NameRes {
    /// candidate bindings, innermost first; `row` is the `it` binding of a
    /// pipeline step whose row columns are looked up first
    Bindings { cands: Vec<BindingId>, row: Option<BindingId>, builtin: bool, suggestion: Option<String> },
    Builtin(String),
}

#[derive(Clone, Debug)]
pub struct AssignRes {
    /// candidate bindings innermost first, with "a function boundary was
    /// crossed before reaching it"
    pub cands: Vec<(BindingId, bool)>,
    /// where a new name is defined
    pub target: BindingId,
}

#[derive(Debug, Clone)]
pub enum TypeRes {
    Builtin(String),
    User(u32, String),
    ErrorKind(String),
}

pub struct Resolution {
    pub bindings: Vec<Binding>,
    pub scopes: Vec<Scope>,
    pub frames: Vec<Frame>,
    pub names: HashMap<u32, NameRes>,
    pub assigns: HashMap<u32, AssignRes>,
    pub scope_of: HashMap<Node, ScopeId>,
    pub frame_of: HashMap<Node, FrameId>,
    /// binding declared by a statement (fn, type, var...) or pattern/loop
    pub decl: HashMap<Node, BindingId>,
    /// variant bindings of an enum type statement, in order
    pub variant_bindings: HashMap<u32, Vec<BindingId>>,
    /// pattern names: Some(binding) when the name is a variant or a type to
    /// compare with, None when it binds a new variable
    pub pattern_tests: HashMap<(u32, usize, Vec<usize>), Option<NameRes>>,
    /// the binding a pattern name binds
    pub pattern_binds: HashMap<(u32, usize, Vec<usize>), BindingId>,
    /// `type X is T`: the trait T
    pub traits_of: HashMap<u32, NameRes>,
    /// methods each trait requires, by trait type id
    pub trait_methods: HashMap<u32, Vec<String>>,
    pub type_ids: HashMap<u32, u32>,
    pub types: HashMap<usize, TypeRes>,
    pub next_type_id: u32,
    /// calls of a function to itself by name: they use the function's own
    /// closure, so it does not capture itself (a reference cycle)
    pub self_calls: std::collections::HashSet<u32>,
    /// the names each task of a `parallel:` block computes (it may assign
    /// them although they belong outside the task)
    pub task_outputs: HashMap<FrameId, Vec<BindingId>>,
    /// the program has `use nexo.sync`
    pub sync: bool,
    /// top-level names no function, lambda, task or test uses: they are
    /// locals of the main program (kept in registers) instead of statics
    pub main_local: std::collections::HashSet<BindingId>,
}

pub const BUILTIN_FUNCTIONS: &[&str] = &[
    "print", "input", "len", "range", "set", "round", "split", "convert", "fail", "validate", "decimal", "float",
    "int", "str", "read", "write", "read_lines", "save",
];

pub const ERROR_KINDS: &[&str] = &[
    "Error",
    "NotFound",
    "IOError",
    "InvalidData",
    "Overflow",
    "DivisionByZero",
    "OutOfRange",
    "ContractBroken",
    "Timeout",
    "InvalidValue",
];

/// Builtin names in the order the phase 0 interpreter defines them (this
/// order decides between equally close suggestions).
pub fn builtin_names() -> Vec<&'static str> {
    let mut v = vec![
        "print", "input", "len", "range", "set", "round", "split", "convert", "fail", "validate", "PI", "half_up",
        "half_even", "int", "float", "decimal", "money", "str", "bool", "list", "map",
    ];
    v.extend(ERROR_KINDS);
    v.extend(crate::lexer::CURRENCIES.iter().map(|(c, _)| *c));
    v.extend(["read", "write", "read_lines", "save", "json", "csv"]);
    v
}

pub fn is_builtin(name: &str) -> bool {
    builtin_names().contains(&name)
}

pub const BUILTIN_TYPES: &[&str] =
    &["int", "float", "decimal", "money", "str", "bool", "list", "map", "set", "any", "channel"];

type RResult<T> = Result<T, Diagnostic>;

struct Resolver {
    r: Resolution,
    /// the function being resolved and the binding of its name
    self_fn: Vec<(FrameId, Option<BindingId>)>,
    self_fn_binding: Option<BindingId>,
    /// calls to the function's own name: (call expr, scope, binding)
    pending_self: Vec<(u32, ScopeId, BindingId)>,
    /// the tasks being resolved: (frame, true for `parallel for`)
    tasks: Vec<(FrameId, bool)>,
    /// top-level names used from another frame
    used_outside: std::collections::HashSet<BindingId>,
}

impl Resolution {
    pub fn binding(&self, id: BindingId) -> &Binding {
        &self.bindings[id]
    }

    /// Names of the top level of the program that a function uses live in
    /// static storage; every other binding belongs to the frame (function)
    /// that runs its scope.
    pub fn is_static(&self, b: BindingId) -> bool {
        self.bindings[b].scope == self.frames[GLOBAL_FRAME].root && !self.main_local.contains(&b)
    }
}

pub fn resolve(program: &Program) -> RResult<Resolution> {
    let mut res = Resolver {
        r: Resolution {
            bindings: Vec::new(),
            scopes: Vec::new(),
            frames: Vec::new(),
            names: HashMap::new(),
            assigns: HashMap::new(),
            scope_of: HashMap::new(),
            frame_of: HashMap::new(),
            decl: HashMap::new(),
            variant_bindings: HashMap::new(),
            pattern_tests: HashMap::new(),
            pattern_binds: HashMap::new(),
            traits_of: HashMap::new(),
            trait_methods: HashMap::new(),
            type_ids: HashMap::new(),
            types: HashMap::new(),
            next_type_id: 1,
            self_calls: std::collections::HashSet::new(),
            task_outputs: HashMap::new(),
            sync: program.body.iter().any(|s| matches!(&s.kind, StmtKind::Use { what, .. } if what == "nexo.sync")),
            main_local: std::collections::HashSet::new(),
        },
        used_outside: std::collections::HashSet::new(),
        tasks: Vec::new(),
        self_fn: Vec::new(),
        self_fn_binding: None,
        pending_self: Vec::new(),
    };
    let root = res.new_scope(None, SKind::Normal, GLOBAL_FRAME, true);
    res.r.frames.push(Frame { parent: None, kind: FrameKind::Global, root, free: Vec::new(), params: Vec::new() });
    res.assign_type_ids(&program.body);
    res.declare_block(root, &program.body);
    res.block(root, &program.body)?;
    res.finish_self_calls(program);
    let global_root = res.r.frames[GLOBAL_FRAME].root;
    let main_local = (0..res.r.bindings.len())
        .filter(|b| res.r.bindings[*b].scope == global_root && !res.used_outside.contains(b))
        .collect();
    res.r.main_local = main_local;
    Ok(res.r)
}

impl Resolver {
    fn new_scope(&mut self, parent: Option<ScopeId>, kind: SKind, frame: FrameId, is_fn: bool) -> ScopeId {
        self.r.scopes.push(Scope { parent, kind, frame, is_fn, names: Vec::new() });
        self.r.scopes.len() - 1
    }

    fn new_frame(&mut self, parent_scope: ScopeId, kind: FrameKind, node: Node) -> (FrameId, ScopeId) {
        let parent_frame = self.r.scopes[parent_scope].frame;
        let id = self.r.frames.len();
        let root = self.new_scope(Some(parent_scope), SKind::Normal, id, true);
        self.r.frames.push(Frame { parent: Some(parent_frame), kind, root, free: Vec::new(), params: Vec::new() });
        self.r.frame_of.insert(node, id);
        self.r.scope_of.insert(node, root);
        (id, root)
    }

    /// A task frame. The root of a `parallel:` task is transparent: the
    /// names it assigns belong to the enclosing scope (D25).
    fn new_task_frame(&mut self, parent_scope: ScopeId, node: Node, transparent: bool) -> (FrameId, ScopeId) {
        let parent_frame = self.r.scopes[parent_scope].frame;
        let id = self.r.frames.len();
        let kind = if transparent { SKind::Transparent } else { SKind::Normal };
        let root = self.new_scope(Some(parent_scope), kind, id, true);
        self.r.frames.push(Frame { parent: Some(parent_frame), kind: FrameKind::Task, root, free: Vec::new(), params: Vec::new() });
        self.r.frame_of.insert(node, id);
        self.r.scope_of.insert(node, root);
        (id, root)
    }

    /// Inside a task of `parallel for`, a variable of outside the task
    /// cannot be changed: the tasks run at the same time (section 5).
    fn check_task_write(&self, scope: ScopeId, cands: &[BindingId], name: &str, span: nexo_diag::Span, any: bool) -> RResult<()> {
        let Some((frame, true)) = self.tasks.last().copied() else { return Ok(()) };
        if self.r.scopes[scope].frame != frame {
            return Ok(());
        }
        let outside = cands.iter().any(|b| {
            let binding = &self.r.bindings[*b];
            binding.frame != frame && (any || binding.kind == BKind::Var)
        });
        if outside {
            return Err(Diagnostic::new("task_write_var", span).arg("name", name));
        }
        Ok(())
    }

    /// The variable a place starts from (`xs` in `xs[0].add(1)`).
    fn place_root(e: &Expr) -> Option<&Expr> {
        match &e.kind {
            ExprKind::Name(_) => Some(e),
            ExprKind::Index { obj, .. } | ExprKind::Attr { obj, .. } | ExprKind::Propagate(obj) => Self::place_root(obj),
            _ => None,
        }
    }

    /// `place.add(x)` or `place[i] = x` inside a `parallel for` task.
    fn check_task_change(&mut self, scope: ScopeId, place: &Expr) -> RResult<()> {
        let Some(root) = Self::place_root(place) else { return Ok(()) };
        let ExprKind::Name(n) = &root.kind else { return Ok(()) };
        // records are shared: changing their fields is checked when it runs
        if !matches!(place.kind, ExprKind::Name(_) | ExprKind::Index { .. }) {
            return Ok(());
        }
        let mut cands = Vec::new();
        let mut s = Some(scope);
        while let Some(id) = s {
            if let Some(b) = self.lookup_local(id, n) {
                cands.push(b);
            }
            s = self.r.scopes[id].parent;
        }
        self.check_task_write(scope, &cands, n, root.span, true)
    }

    fn lookup_local(&self, scope: ScopeId, name: &str) -> Option<BindingId> {
        self.r.scopes[scope].names.iter().find(|(n, _)| n == name).map(|(_, b)| *b)
    }

    /// Add (or update) the binding of `name` in `scope`.
    fn declare(&mut self, scope: ScopeId, name: &str, kind: BKind, ty: Option<TypeExpr>, origin: Origin) -> BindingId {
        if let Some(b) = self.lookup_local(scope, name) {
            let binding = &mut self.r.bindings[b];
            if kind == BKind::Var {
                binding.kind = BKind::Var;
                if binding.ty.is_none() {
                    binding.ty = ty;
                }
            }
            if binding.origin == Origin::Plain && origin != Origin::Plain {
                binding.origin = origin;
            }
            return b;
        }
        let frame = self.r.scopes[scope].frame;
        let entry = origin == Origin::Param;
        self.r.bindings.push(Binding {
            name: name.to_string(),
            kind,
            ty,
            origin,
            scope,
            frame,
            captured: false,
            entry,
            shared: false,
        });
        let id = self.r.bindings.len() - 1;
        self.r.scopes[scope].names.push((name.to_string(), id));
        id
    }

    /// The scope that receives new names: the nearest non-transparent one.
    fn target(&self, mut scope: ScopeId) -> ScopeId {
        while self.r.scopes[scope].kind == SKind::Transparent {
            scope = self.r.scopes[scope].parent.unwrap();
        }
        scope
    }

    fn assign_type_ids(&mut self, body: &[Stmt]) {
        crate::checker::walk_stmts(body, true, &mut |s| {
            if let StmtKind::Type(t) = &s.kind {
                let id = self.r.next_type_id;
                self.r.next_type_id += 1 + t.variants.len() as u32;
                self.r.type_ids.insert(s.id, id);
            } else if let StmtKind::Trait { sigs, .. } = &s.kind {
                let id = self.r.next_type_id;
                self.r.next_type_id += 1;
                self.r.type_ids.insert(s.id, id);
                self.r.trait_methods.insert(id, sigs.iter().map(|s| s.name.clone()).collect());
            }
        });
    }

    /// Declare the names a statement list binds in `scope` (descending into
    /// the blocks that do not have their own scope).
    fn declare_block(&mut self, scope: ScopeId, body: &[Stmt]) {
        for s in body {
            match &s.kind {
                StmtKind::Assign { target: Expr { kind: ExprKind::Name(n), .. }, .. } => {
                    self.declare(scope, n, BKind::Const, None, Origin::Plain);
                }
                StmtKind::VarDecl { name, ty, .. } => {
                    self.declare(scope, name, BKind::Var, ty.clone(), Origin::Plain);
                }
                StmtKind::Fn(f) => {
                    let b = self.declare(scope, &f.name, BKind::Const, None, Origin::Fn);
                    self.r.decl.insert(Node::Stmt(s.id), b);
                }
                StmtKind::Type(t) => {
                    let id = self.r.type_ids[&s.id];
                    let b = self.declare(scope, &t.name, BKind::Const, None, Origin::Type(id));
                    self.r.decl.insert(Node::Stmt(s.id), b);
                    let mut vbs = Vec::new();
                    for (i, v) in t.variants.iter().enumerate() {
                        let vb = self.declare(
                            scope,
                            &v.name,
                            BKind::Const,
                            None,
                            Origin::Variant(id + 1 + i as u32, !v.params.is_empty()),
                        );
                        vbs.push(vb);
                    }
                    self.r.variant_bindings.insert(s.id, vbs);
                }
                StmtKind::Trait { name, .. } => {
                    let id = self.r.type_ids[&s.id];
                    let b = self.declare(scope, name, BKind::Const, None, Origin::Trait(id));
                    self.r.decl.insert(Node::Stmt(s.id), b);
                }
                StmtKind::If { branches, orelse } => {
                    for (_, b) in branches {
                        self.declare_block(scope, b);
                    }
                    if let Some(b) = orelse {
                        self.declare_block(scope, b);
                    }
                }
                StmtKind::Try { body, handlers, .. } => {
                    self.declare_block(scope, body);
                    for h in handlers {
                        self.declare_block(scope, &h.body);
                    }
                }
                StmtKind::Match { arms, .. } => {
                    for a in arms {
                        self.declare_block(scope, &a.body);
                    }
                }
                StmtKind::Parallel(body) => self.declare_block(scope, body),
                StmtKind::Shared { name, .. } => {
                    let b = self.declare(scope, name, BKind::Var, None, Origin::Plain);
                    self.r.bindings[b].shared = true;
                }
                StmtKind::Select { arms } => {
                    for a in arms {
                        self.declare_block(scope, &a.body);
                    }
                }
                _ => {}
            }
        }
    }

    /// Names visible from `scope`, innermost first, then the builtins.
    fn visible(&self, mut scope: ScopeId) -> Vec<String> {
        let mut out = Vec::new();
        loop {
            for (n, _) in &self.r.scopes[scope].names {
                out.push(n.clone());
            }
            match self.r.scopes[scope].parent {
                Some(p) => scope = p,
                None => break,
            }
        }
        out.extend(builtin_names().into_iter().map(String::from));
        if self.r.sync {
            out.push("channel".into());
        }
        out
    }

    /// Mark a binding used from `scope` as captured when it belongs to an
    /// outer function (globals are never captured).
    fn note_use(&mut self, scope: ScopeId, b: BindingId) {
        let bf = self.r.bindings[b].frame;
        let mut f = self.r.scopes[scope].frame;
        if self.r.is_static(b) && f != GLOBAL_FRAME {
            self.used_outside.insert(b);
        }
        if bf == f || self.r.is_static(b) {
            return;
        }
        self.r.bindings[b].captured = true;
        while f != bf {
            if !self.r.frames[f].free.contains(&b) {
                self.r.frames[f].free.push(b);
            }
            match self.r.frames[f].parent {
                Some(p) => f = p,
                None => break,
            }
        }
    }

    fn lookup(&mut self, scope: ScopeId, name: &str) -> NameRes {
        let mut cands = Vec::new();
        let mut row = None;
        let mut s = Some(scope);
        while let Some(id) = s {
            if let Some(b) = self.lookup_local(id, name) {
                cands.push(b);
            }
            if self.r.scopes[id].kind == SKind::Row && cands.is_empty() && row.is_none() && name != "it" {
                row = self.lookup_local(id, "it");
            }
            s = self.r.scopes[id].parent;
        }
        if cands.is_empty() && row.is_none() && (is_builtin(name) || (self.r.sync && name == "channel")) {
            return NameRes::Builtin(name.to_string());
        }
        for b in &cands {
            self.note_use(scope, *b);
        }
        if let Some(r) = row {
            self.note_use(scope, r);
        }
        let visible = self.visible(scope);
        let suggestion = suggest(name, visible.iter().map(|s| s.as_str()));
        let builtin = cands.is_empty() && is_builtin(name);
        NameRes::Bindings { cands, row, builtin, suggestion }
    }

    fn name_error(&self, scope: ScopeId, name: &str, span: nexo_diag::Span) -> Diagnostic {
        let visible = self.visible(scope);
        match suggest(name, visible.iter().map(|s| s.as_str())) {
            Some(s) => Diagnostic::new("name_not_found", span).fix("suggest").arg("name", name).arg("suggestion", s),
            None => Diagnostic::new("name_not_found", span).arg("name", name),
        }
    }

    fn read(&mut self, scope: ScopeId, e: &Expr, name: &str) -> RResult<()> {
        let res = self.lookup(scope, name);
        if let NameRes::Bindings { cands, row: None, .. } = &res {
            if cands.is_empty() {
                return Err(self.name_error(scope, name, e.span));
            }
        }
        self.r.names.insert(e.id, res);
        Ok(())
    }

    fn assign_res(&mut self, scope: ScopeId, name: &str) -> AssignRes {
        let target_scope = self.target(scope);
        let target = self.lookup_local(target_scope, name).expect("declared");
        let mut cands = Vec::new();
        let mut crossed = false;
        let mut s = Some(scope);
        while let Some(id) = s {
            if let Some(b) = self.lookup_local(id, name) {
                cands.push((b, crossed));
                self.note_use(scope, b);
            }
            let fk = self.r.frames[self.r.scopes[id].frame].kind;
            if self.r.scopes[id].is_fn && matches!(fk, FrameKind::Function | FrameKind::Lambda | FrameKind::Handler) {
                crossed = true;
            }
            s = self.r.scopes[id].parent;
        }
        AssignRes { cands, target }
    }

    /// `f(...)` inside `fn f`, when the name is the function itself: no
    /// capture yet (decided in finish_self_calls).
    fn self_call(&mut self, scope: ScopeId, func: &Expr) -> bool {
        let ExprKind::Name(n) = &func.kind else { return false };
        let Some((frame, Some(own))) = self.self_fn.last().copied() else { return false };
        if self.r.scopes[scope].frame != frame {
            return false;
        }
        let mut s = Some(scope);
        let mut found = None;
        while let Some(id) = s {
            if let Some(b) = self.lookup_local(id, n) {
                found = Some(b);
                break;
            }
            if self.r.scopes[id].kind == SKind::Row {
                return false;
            }
            s = self.r.scopes[id].parent;
        }
        if found != Some(own) || self.r.is_static(own) {
            return false;
        }
        let visible = self.visible(scope);
        let suggestion = suggest(n, visible.iter().map(|s| s.as_str()));
        self.r.names.insert(func.id, NameRes::Bindings { cands: vec![own], row: None, builtin: false, suggestion });
        self.pending_self.push((func.id, scope, own));
        true
    }

    /// A function defined once calls itself through its own closure; any
    /// other name use is an ordinary capture.
    fn finish_self_calls(&mut self, program: &Program) {
        let mut defs: HashMap<BindingId, usize> = HashMap::new();
        let r = &self.r;
        crate::checker::walk_stmts(&program.body, true, &mut |s| match &s.kind {
            StmtKind::Fn(_) => {
                if let Some(b) = r.decl.get(&Node::Stmt(s.id)) {
                    *defs.entry(*b).or_default() += 1;
                }
            }
            StmtKind::Assign { .. } | StmtKind::VarDecl { .. } => {
                if let Some(a) = r.assigns.get(&s.id) {
                    *defs.entry(a.target).or_default() += 1;
                }
            }
            _ => {}
        });
        for (id, scope, b) in std::mem::take(&mut self.pending_self) {
            if defs.get(&b) == Some(&1) {
                self.r.self_calls.insert(id);
            } else {
                self.note_use(scope, b);
            }
        }
    }

    // ------------------------------------------------------------ walking

    fn block(&mut self, scope: ScopeId, body: &[Stmt]) -> RResult<()> {
        for s in body {
            self.stmt(scope, s)?;
        }
        Ok(())
    }

    fn new_block_scope(&mut self, parent: ScopeId, node: Node, body: &[Stmt]) -> ScopeId {
        let frame = self.r.scopes[parent].frame;
        let s = self.new_scope(Some(parent), SKind::Normal, frame, false);
        self.r.scope_of.insert(node, s);
        self.declare_block(s, body);
        s
    }

    fn function(&mut self, scope: ScopeId, f: &FnDef, kind: FrameKind) -> RResult<()> {
        let own = self.self_fn_binding.take();
        let (frame, root) = self.new_frame(scope, kind, Node::Fn(f.id));
        self.self_fn.push((frame, own));
        let r = self.function_body(scope, root, frame, f);
        self.self_fn.pop();
        r
    }

    fn function_body(&mut self, scope: ScopeId, root: ScopeId, frame: FrameId, f: &FnDef) -> RResult<()> {
        for p in &f.params {
            let b = self.declare(root, &p.name, BKind::Const, None, Origin::Param);
            self.r.frames[frame].params.push(b);
        }
        self.declare_block(root, &f.body);
        for p in &f.params {
            if let Some(d) = &p.default {
                self.expr(root, d)?;
            }
            if let Some(t) = &p.ty {
                self.type_expr(scope, t)?;
            }
        }
        if let Some(t) = &f.ret {
            self.type_expr(scope, t)?;
        }
        self.block(root, &f.body)
    }

    fn stmt(&mut self, scope: ScopeId, s: &Stmt) -> RResult<()> {
        match &s.kind {
            StmtKind::Expr(e) => self.expr(scope, e),
            StmtKind::Assign { target, value, ty, .. } => {
                self.expr(scope, value)?;
                if let Some(t) = ty {
                    self.type_expr(scope, t)?;
                }
                match &target.kind {
                    ExprKind::Name(n) => {
                        // `x += 1` reads x first
                        let res = self.lookup(scope, n);
                        self.r.names.insert(target.id, res);
                        let a = self.assign_res(scope, n);
                        let cands: Vec<BindingId> = a.cands.iter().map(|(b, _)| *b).collect();
                        self.r.assigns.insert(s.id, a);
                        self.check_task_write(scope, &cands, n, target.span, false)
                    }
                    ExprKind::Attr { obj, .. } => self.expr(scope, obj),
                    ExprKind::Index { obj, index } => {
                        self.check_task_change(scope, obj)?;
                        self.expr(scope, obj)?;
                        self.expr(scope, index)
                    }
                    _ => Ok(()),
                }
            }
            StmtKind::VarDecl { name, value, ty, .. } => {
                self.expr(scope, value)?;
                if let Some(t) = ty {
                    self.type_expr(scope, t)?;
                }
                let a = self.assign_res(scope, name);
                self.r.assigns.insert(s.id, a);
                Ok(())
            }
            StmtKind::Return(Some(e)) | StmtKind::Expect(e) | StmtKind::Ensure(e) => self.expr(scope, e),
            StmtKind::Assert { expr, .. } => self.expr(scope, expr),
            StmtKind::Emit { args, .. } => {
                for a in args {
                    self.expr(scope, &a.value)?;
                }
                Ok(())
            }
            StmtKind::Fn(f) => {
                let b = self.r.decl.get(&Node::Stmt(s.id)).copied();
                self.self_fn_binding = b;
                self.function(scope, f, FrameKind::Function)
            }
            StmtKind::Type(t) => {
                if let Some(tr) = &t.trait_name {
                    let res = self.lookup(scope, tr);
                    self.r.traits_of.insert(s.id, res);
                }
                if let Some(a) = &t.alias {
                    self.type_expr(scope, a)?;
                }
                for f in &t.fields {
                    self.type_expr(scope, &f.ty)?;
                }
                for v in &t.variants {
                    for p in &v.params {
                        if let Some(ty) = &p.ty {
                            self.type_expr(scope, ty)?;
                        }
                    }
                }
                for (i, f) in t.fields.iter().enumerate() {
                    if let Some(d) = &f.default {
                        let (_, root) = self.new_frame(scope, FrameKind::Default, Node::Field(s.id, i));
                        self.expr(root, d)?;
                    }
                }
                for m in &t.methods {
                    self.function(scope, m, FrameKind::Function)?;
                }
                Ok(())
            }
            StmtKind::If { branches, orelse } => {
                for (c, b) in branches {
                    self.expr(scope, c)?;
                    self.block(scope, b)?;
                }
                if let Some(b) = orelse {
                    self.block(scope, b)?;
                }
                Ok(())
            }
            StmtKind::For { var, iterable, body } => {
                self.expr(scope, iterable)?;
                let inner = self.new_block_scope(scope, Node::Stmt(s.id), body);
                let b = self.declare(inner, var, BKind::Const, None, Origin::Plain);
                self.r.bindings[b].entry = true;
                self.r.decl.insert(Node::Stmt(s.id), b);
                self.block(inner, body)
            }
            StmtKind::While { cond, body } => {
                self.expr(scope, cond)?;
                let inner = self.new_block_scope(scope, Node::Stmt(s.id), body);
                self.block(inner, body)
            }
            StmtKind::Repeat { count, body } => {
                self.expr(scope, count)?;
                let inner = self.new_block_scope(scope, Node::Stmt(s.id), body);
                self.block(inner, body)
            }
            StmtKind::Match { subject, arms } => {
                self.expr(scope, subject)?;
                for (i, arm) in arms.iter().enumerate() {
                    let frame = self.r.scopes[scope].frame;
                    let arm_scope = self.new_scope(Some(scope), SKind::Transparent, frame, false);
                    self.r.scope_of.insert(Node::Arm(s.id, i), arm_scope);
                    for (pi, p) in arm.patterns.iter().enumerate() {
                        self.pattern(scope, arm_scope, s.id, i, vec![pi], p)?;
                    }
                    if let Some(g) = &arm.guard {
                        self.expr(arm_scope, g)?;
                    }
                    self.block(arm_scope, &arm.body)?;
                }
                Ok(())
            }
            StmtKind::Try { within, body, handlers } => {
                if let Some(w) = within {
                    self.expr(scope, w)?;
                }
                self.block(scope, body)?;
                for (i, h) in handlers.iter().enumerate() {
                    if let Some(k) = &h.kind {
                        let t = self.type_name(scope, k);
                        match t {
                            Some(TypeRes::ErrorKind(_)) | Some(TypeRes::User(..)) => {}
                            _ => return Err(Diagnostic::new("unknown_type", h.span).arg("name", k.clone())),
                        }
                    }
                    let frame = self.r.scopes[scope].frame;
                    let hs = self.new_scope(Some(scope), SKind::Transparent, frame, false);
                    self.r.scope_of.insert(Node::Handler(s.id, i), hs);
                    let b = self.declare(hs, &h.var, BKind::Const, None, Origin::Plain);
                    self.r.bindings[b].entry = true;
                    self.r.decl.insert(Node::Handler(s.id, i), b);
                    self.block(hs, &h.body)?;
                }
                Ok(())
            }
            StmtKind::Spawn { call } => self.expr(scope, call),
            StmtKind::Shared { name, value, .. } => {
                self.expr(scope, value)?;
                let a = self.assign_res(scope, name);
                self.r.assigns.insert(s.id, a);
                Ok(())
            }
            StmtKind::Select { arms } => {
                for (i, arm) in arms.iter().enumerate() {
                    if let Some(c) = arm.channel.as_ref().or(arm.after.as_ref()) {
                        self.expr(scope, c)?;
                    }
                    let frame = self.r.scopes[scope].frame;
                    let arm_scope = self.new_scope(Some(scope), SKind::Transparent, frame, false);
                    self.r.scope_of.insert(Node::SelectArm(s.id, i), arm_scope);
                    if let Some(v) = &arm.var {
                        let b = self.declare(arm_scope, v, BKind::Const, None, Origin::Plain);
                        self.r.bindings[b].entry = true;
                        self.r.decl.insert(Node::SelectArm(s.id, i), b);
                    }
                    self.block(arm_scope, &arm.body)?;
                }
                Ok(())
            }
            StmtKind::Parallel(body) => {
                // each statement is a task (section 5)
                for (i, st) in body.iter().enumerate() {
                    let (frame, root) = self.new_task_frame(scope, Node::Task(s.id, i), true);
                    self.tasks.push((frame, false));
                    let r = self.stmt(root, st);
                    self.tasks.pop();
                    r?;
                    let mut outputs = Vec::new();
                    for name in crate::checker::assigned_names(std::slice::from_ref(st)) {
                        if let Some(b) = self.lookup_local(self.target(root), &name) {
                            outputs.push(b);
                        }
                    }
                    self.r.task_outputs.insert(frame, outputs);
                }
                Ok(())
            }
            StmtKind::Test { body, .. } => {
                let global_root = self.r.frames[GLOBAL_FRAME].root;
                let (_, root) = self.new_frame(global_root, FrameKind::Test, Node::Stmt(s.id));
                self.declare_block(root, body);
                self.block(root, body)
            }
            StmtKind::Event { kind: "on", params, body, .. } => {
                let (frame, root) = self.new_frame(scope, FrameKind::Handler, Node::Stmt(s.id));
                for a in params {
                    let ExprKind::Name(n) = &a.value.kind else {
                        let got = format!("\"{}\"", self.arg_text(a));
                        return Err(Diagnostic::new("unexpected", a.span)
                            .fix("expected")
                            .arg("got", got)
                            .arg("expected", nexo_diag::word("name")));
                    };
                    let b = self.declare(root, n, BKind::Const, None, Origin::Param);
                    self.r.frames[frame].params.push(b);
                }
                self.declare_block(root, body);
                self.block(root, body)
            }
            _ => Ok(()),
        }
    }

    fn arg_text(&self, a: &Arg) -> String {
        match &a.value.kind {
            ExprKind::Num { text, .. } => text.clone(),
            _ => String::new(),
        }
    }

    fn pattern(
        &mut self,
        scope: ScopeId,
        arm_scope: ScopeId,
        stmt: u32,
        arm: usize,
        path: Vec<usize>,
        p: &Pattern,
    ) -> RResult<()> {
        match p {
            Pattern::Wild(_) => Ok(()),
            Pattern::Lit(_, lit) => self.expr(scope, lit),
            Pattern::Name(_, n) => {
                let res = self.lookup(scope, n);
                let is_test = match &res {
                    NameRes::Bindings { cands, .. } if !cands.is_empty() => {
                        matches!(self.r.bindings[cands[0]].origin, Origin::Variant(..) | Origin::Type(_))
                    }
                    _ => false,
                };
                if is_test {
                    self.r.pattern_tests.insert((stmt, arm, path), Some(res));
                } else {
                    let b = self.declare(arm_scope, n, BKind::Const, None, Origin::Plain);
                    self.r.pattern_binds.insert((stmt, arm, path.clone()), b);
                    self.r.pattern_tests.insert((stmt, arm, path), None);
                }
                Ok(())
            }
            Pattern::Ctor(span, n, args) => {
                let res = self.lookup(scope, n);
                if let NameRes::Bindings { cands, .. } = &res {
                    if cands.is_empty() {
                        return Err(self.name_error(scope, n, *span));
                    }
                }
                self.r.pattern_tests.insert((stmt, arm, path.clone()), Some(res));
                for (i, a) in args.iter().enumerate() {
                    let mut sub = path.clone();
                    sub.push(i);
                    self.pattern(scope, arm_scope, stmt, arm, sub, a)?;
                }
                Ok(())
            }
        }
    }

    /// A type name used in a type: a user type, a builtin type or an error kind.
    fn type_name(&mut self, scope: ScopeId, name: &str) -> Option<TypeRes> {
        let mut s = Some(scope);
        while let Some(id) = s {
            if let Some(b) = self.lookup_local(id, name) {
                match self.r.bindings[b].origin {
                    Origin::Type(t) | Origin::Trait(t) | Origin::Variant(t, _) => {
                        return Some(TypeRes::User(t, name.to_string()))
                    }
                    _ => {}
                }
            }
            s = self.r.scopes[id].parent;
        }
        if BUILTIN_TYPES.contains(&name) {
            return Some(TypeRes::Builtin(name.to_string()));
        }
        if ERROR_KINDS.contains(&name) {
            return Some(TypeRes::ErrorKind(name.to_string()));
        }
        None
    }

    fn type_expr(&mut self, scope: ScopeId, t: &TypeExpr) -> RResult<()> {
        match t {
            TypeExpr::Union { .. } | TypeExpr::Range { .. } => Ok(()),
            TypeExpr::Name(tn) => {
                match self.type_name(scope, &tn.name) {
                    Some(r) => {
                        self.r.types.insert(tn as *const TypeName as usize, r);
                    }
                    None => return Err(Diagnostic::new("unknown_type", tn.span).arg("name", tn.name.clone())),
                }
                for p in &tn.params {
                    self.type_expr(scope, p)?;
                }
                Ok(())
            }
        }
    }

    fn step_frame(&mut self, scope: ScopeId, node: Node, e: &Expr) -> RResult<()> {
        let (frame, root) = self.new_frame(scope, FrameKind::Step, node);
        self.r.scopes[root].kind = SKind::Row;
        let it = self.declare(root, "it", BKind::Const, None, Origin::Param);
        self.r.frames[frame].params.push(it);
        self.expr(root, e)
    }

    fn expr(&mut self, scope: ScopeId, e: &Expr) -> RResult<()> {
        match &e.kind {
            ExprKind::Name(n) => self.read(scope, e, n),
            ExprKind::Str(parts) => {
                for p in parts {
                    if let StrPiece::Expr(x) = p {
                        self.expr(scope, x)?;
                    }
                }
                Ok(())
            }
            ExprKind::List(items) => {
                for i in items {
                    self.expr(scope, i)?;
                }
                Ok(())
            }
            ExprKind::Map(pairs) => {
                for (k, v) in pairs {
                    self.expr(scope, k)?;
                    self.expr(scope, v)?;
                }
                Ok(())
            }
            ExprKind::Comprehension { element, var, iterable, condition } => {
                self.expr(scope, iterable)?;
                let frame = self.r.scopes[scope].frame;
                let inner = self.new_scope(Some(scope), SKind::Normal, frame, false);
                self.r.scope_of.insert(Node::Expr(e.id), inner);
                let b = self.declare(inner, var, BKind::Const, None, Origin::Plain);
                self.r.bindings[b].entry = true;
                self.r.decl.insert(Node::Expr(e.id), b);
                if let Some(c) = condition {
                    self.expr(inner, c)?;
                }
                self.expr(inner, element)
            }
            ExprKind::Attr { obj, .. } => self.expr(scope, obj),
            ExprKind::Index { obj, index } => {
                self.expr(scope, obj)?;
                self.expr(scope, index)
            }
            ExprKind::Call { func, args } => {
                if let ExprKind::Attr { obj, name, .. } = &func.kind {
                    if name == "add" {
                        self.check_task_change(scope, obj)?;
                    }
                }
                // old(...) inside ensure is not a name (D53)
                if !matches!(&func.kind, ExprKind::Name(n) if n == "old") && !self.self_call(scope, func) {
                    self.expr(scope, func)?;
                }
                for a in args {
                    self.expr(scope, &a.value)?;
                }
                Ok(())
            }
            ExprKind::Propagate(x) | ExprKind::Unary { operand: x, .. } => self.expr(scope, x),
            ExprKind::BinOp { left, right, .. }
            | ExprKind::BoolOp { left, right, .. }
            | ExprKind::Coalesce { left, right } => {
                self.expr(scope, left)?;
                self.expr(scope, right)
            }
            ExprKind::Range { start, end } => {
                self.expr(scope, start)?;
                self.expr(scope, end)
            }
            ExprKind::Compare { operands, .. } => {
                for o in operands {
                    self.expr(scope, o)?;
                }
                Ok(())
            }
            ExprKind::Lambda { param, body } => {
                let (frame, root) = self.new_frame(scope, FrameKind::Lambda, Node::Expr(e.id));
                let b = self.declare(root, param, BKind::Const, None, Origin::Param);
                self.r.frames[frame].params.push(b);
                self.expr(root, body)
            }
            ExprKind::Pipeline { source, steps } => {
                self.expr(scope, source)?;
                for (i, step) in steps.iter().enumerate() {
                    let node = Node::Step(e.id, i);
                    match step.kind {
                        StepKind::Call | StepKind::Take => {
                            if let Some(x) = &step.expr {
                                self.expr(scope, x)?;
                            }
                        }
                        StepKind::Pick => {
                            for (ci, c) in step.columns.iter().enumerate() {
                                self.step_frame(scope, Node::Column(e.id, i, ci), c)?;
                            }
                        }
                        _ => {
                            if let Some(x) = &step.expr {
                                self.step_frame(scope, node, x)?;
                            }
                        }
                    }
                }
                Ok(())
            }
            ExprKind::ParallelFor { var, iterable, args, body } => {
                self.expr(scope, iterable)?;
                for a in args {
                    self.expr(scope, &a.value)?;
                }
                // the body is a task: a frame of its own, run for each element
                let (frame, root) = self.new_task_frame(scope, Node::Expr(e.id), false);
                self.declare_block(root, body);
                let b = self.declare(root, var, BKind::Const, None, Origin::Param);
                self.r.bindings[b].entry = true;
                self.r.frames[frame].params.push(b);
                self.r.decl.insert(Node::Expr(e.id), b);
                self.tasks.push((frame, true));
                let r = self.block(root, body);
                self.tasks.pop();
                r
            }
            _ => Ok(()),
        }
    }
}
