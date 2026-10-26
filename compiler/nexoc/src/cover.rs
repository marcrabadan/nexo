//! `nexo test --coverage`: branch coverage of the code the tests run (D152).
//!
//! Before the program is compiled, every branch the developer wrote gets a
//! call to `__cover(n)` as its first statement: the body of each function
//! and method, each branch of an `if` (and the branch an `if` without
//! `else` takes when every condition is false, as an added `else`), each
//! arm of a `match`, each loop body and each `else` of a `try`. Code inside
//! `test` blocks, lambdas and `parallel for` is not counted, nor the code
//! the parser writes for module blocks (functions and handlers whose names
//! start with `__`). Phase 0 does the same (nexo/cover.py).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use crate::ast::*;
use nexo_diag::Span;

#[derive(Clone, Debug)]
pub struct Branch {
    pub file: String,
    pub line: u32,
    /// fn, undo, if, elif, else, no_else, arm, loop, handler
    pub kind: &'static str,
    pub name: String,
}

impl Branch {
    pub fn key(&self) -> (String, u32, &'static str, String) {
        (self.file.clone(), self.line, self.kind, self.name.clone())
    }

    pub fn text(&self) -> String {
        let what = nexo_diag::msg(&format!("info.cover_{}", self.kind), &[("name", self.name.clone())]);
        format!("{}:{}: {what}.", self.file, self.line)
    }
}

/// Set by `nexo test --coverage`: compile with the calls.
pub static ENABLED: AtomicBool = AtomicBool::new(false);
/// The branches of the last program compiled with coverage.
pub static TABLE: Mutex<Vec<Branch>> = Mutex::new(Vec::new());

pub fn enabled() -> bool {
    ENABLED.load(Ordering::Relaxed)
}

struct Instrument<'a> {
    main: &'a str,
    table: Vec<Branch>,
    next_id: u32,
}

fn generated_call(e: &Expr) -> bool {
    matches!(&e.kind, ExprKind::Call { func, .. } if matches!(&func.kind, ExprKind::Name(n) if n.starts_with("__")))
}

impl Instrument<'_> {
    fn id(&mut self) -> u32 {
        self.next_id += 1;
        self.next_id
    }

    fn file(&self, span: Span) -> String {
        if span.file == 0 {
            return self.main.to_string();
        }
        nexo_diag::file_of(span.file).map(|f| f.0.to_string()).unwrap_or_default()
    }

    fn add(&mut self, span: Span, kind: &'static str, name: &str) -> usize {
        let file = self.file(span);
        self.table.push(Branch { file, line: span.line, kind, name: name.to_string() });
        self.table.len() - 1
    }

    fn call(&mut self, span: Span, n: usize) -> Stmt {
        let func = Expr { id: self.id(), span, kind: ExprKind::Name("__cover".into()) };
        let arg = Expr { id: self.id(), span, kind: ExprKind::Num { text: n.to_string(), is_float: false } };
        let call = Expr { id: self.id(), span, kind: ExprKind::Call { func: Box::new(func), args: vec![Arg { span, name: None, value: arg }] } };
        Stmt { id: self.id(), span, kind: StmtKind::Expr(call) }
    }

    /// Put the call first in `body`, after its leading contracts. The branch
    /// is on the line of its first statement, or of `at`.
    fn mark(&mut self, body: &mut Vec<Stmt>, kind: &'static str, name: &str, at: Option<Span>) {
        let mut i = 0;
        while i < body.len() && matches!(body[i].kind, StmtKind::Expect(_) | StmtKind::Ensure(_)) {
            i += 1;
        }
        let span = at.unwrap_or(body[0].span);
        let n = self.add(span, kind, name);
        let call = self.call(span, n);
        body.insert(i, call);
    }

    fn block(&mut self, body: &mut [Stmt]) {
        for s in body.iter_mut() {
            self.stmt(s);
        }
    }

    fn func(&mut self, f: &mut FnDef, owner: Option<&str>) {
        self.block(&mut f.body);
        if let Some(action) = f.name.strip_prefix("__undo_") {
            let action = action.to_string();
            self.mark(&mut f.body, "undo", &action, Some(f.name_span));
        } else if !f.name.starts_with("__") {
            let name = match owner {
                Some(o) => format!("{o}.{}", f.name),
                None => f.name.clone(),
            };
            self.mark(&mut f.body, "fn", &name, Some(f.name_span));
        }
    }

    fn stmt(&mut self, s: &mut Stmt) {
        let span = s.span;
        match &mut s.kind {
            StmtKind::Fn(f) => self.func(f, None),
            StmtKind::Type(t) => {
                let owner = t.name.clone();
                for m in t.methods.iter_mut() {
                    self.func(m, Some(&owner));
                }
            }
            StmtKind::Module { body: Some(body), .. } => self.block(body),
            StmtKind::If { branches, orelse } => {
                for (_, body) in branches.iter_mut() {
                    self.block(body);
                }
                if let Some(body) = orelse.as_mut() {
                    self.block(body);
                }
                if generated_call(&branches[0].0) {
                    return;
                }
                for (i, (_, body)) in branches.iter_mut().enumerate() {
                    self.mark(body, if i == 0 { "if" } else { "elif" }, "", None);
                }
                match orelse.as_mut() {
                    Some(body) => self.mark(body, "else", "", None),
                    None => {
                        let n = self.add(span, "no_else", "");
                        *orelse = Some(vec![self.call(span, n)]);
                    }
                }
            }
            StmtKind::For { body, .. } | StmtKind::While { body, .. } | StmtKind::Repeat { body, .. } => {
                self.block(body);
                if body.len() == 1 && matches!(&body[0].kind, StmtKind::Expr(e) if generated_call(e)) {
                    return;
                }
                self.mark(body, "loop", "", None);
            }
            StmtKind::Match { arms, .. } => {
                for arm in arms.iter_mut() {
                    self.block(&mut arm.body);
                    self.mark(&mut arm.body, "arm", "", None);
                }
            }
            StmtKind::Try { body, handlers, .. } => {
                self.block(body);
                for h in handlers.iter_mut() {
                    self.block(&mut h.body);
                    if !h.var.starts_with("__") {
                        self.mark(&mut h.body, "handler", "", None);
                    }
                }
            }
            StmtKind::Parallel(body) => self.block(body),
            _ => {}
        }
    }
}

/// Add the `__cover` calls to a linked program (its modules inside it);
/// returns the next free node id. The branches go to TABLE.
pub fn instrument(program: &mut Program, main: &str, next_id: u32) -> u32 {
    let mut ins = Instrument { main, table: Vec::new(), next_id };
    ins.block(&mut program.body);
    *TABLE.lock().unwrap() = ins.table;
    ins.next_id
}

/// The summary and every branch that never ran, by file and line.
pub fn report(branches: &[Branch], covered: &std::collections::HashSet<(String, u32, &'static str, String)>) -> String {
    let total = branches.len();
    if total == 0 {
        return nexo_diag::msg("info.cover_none", &[]) + "\n";
    }
    let done = branches.iter().filter(|b| covered.contains(&b.key())).count();
    let mut lines = vec![nexo_diag::msg(
        "info.cover_summary",
        &[("covered", done.to_string()), ("total", total.to_string()), ("percent", (done * 100 / total).to_string())],
    )];
    let mut missing: Vec<&Branch> = branches.iter().filter(|b| !covered.contains(&b.key())).collect();
    missing.sort_by(|a, b| (&a.file, a.line).cmp(&(&b.file, b.line)));
    lines.extend(missing.iter().map(|b| b.text()));
    if !missing.is_empty() {
        lines.push(nexo_diag::msg("info.cover_fix", &[]));
    }
    lines.join("\n") + "\n"
}
