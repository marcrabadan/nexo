//! Code generation: the checked AST becomes LLVM IR (text), which clang
//! compiles and links with the runtime library.
//!
//! Every value is `%V = { i64 tag, i64 bits }`. Each function, lambda,
//! pipeline step, field default, test and event handler becomes an LLVM
//! function `%V (ptr tls, ptr closure, ptr args, i32 call_span)`, where
//! `tls` is the state of the running thread. Errors are values (section
//! 3): after every call that can fail the code checks the thread's
//! `failing` flag and jumps to the nearest `try`, or returns to its caller.
//!
//! Memory (section 7): heap values are reference counted. Runtime
//! functions borrow their arguments and return owned values. Every frame
//! owns the values in its slots and releases them when it ends; the owned
//! temporaries of the expression being computed are kept in `live`, and a
//! failure releases them on its way to the handler.

use std::collections::HashMap;
use std::fmt::Write as _;

use nexo_diag::{source_text, Diagnostic, Span};

use crate::ast::*;
use crate::checker::type_text;
use crate::resolve::*;

pub struct Options {
    pub file: String,
    pub lang: u32,
    pub test_mode: bool,
    /// `--trust-ensure`: unproven `ensure` checks are not compiled
    pub trust_ensure: bool,
}

pub type GResult<T> = Result<T, Diagnostic>;

const T_NONE: u64 = 0;
const T_BOOL: u64 = 1;
const T_INT: u64 = 2;
const T_FLOAT: u64 = 3;
const T_OBJ: u64 = 4;
const T_LITFLOAT: u64 = 5;
const T_UNDEF: u64 = 6;
const T_MISSING: u64 = 7;

/// Nested calls allowed before "too many nested calls" (D73).
const MAX_DEPTH: u64 = 10_000;

#[derive(Clone, Debug)]
pub enum Val {
    /// a register of type %V
    Reg(String),
    /// a constant: tag and the operand of its bits
    Lit(u64, String),
}

fn lit(tag: u64, bits: impl Into<String>) -> Val {
    Val::Lit(tag, bits.into())
}

fn none() -> Val {
    lit(T_NONE, "0")
}

/// A place that is changed (`xs[i] = v`, `order.items.add(x)`): its
/// root variable, record values in slots and indexes in slots.
enum Place<'e> {
    Var(&'e Expr, Vec<BindingId>),
    Field(String, String, Span),
    Index(Box<Place<'e>>, String, Span),
    Value(Val),
}

#[derive(Clone)]
enum Slot {
    /// an alloca holding the value
    Value(String),
    /// an alloca holding a pointer to a heap cell (captured variable)
    Cell(String),
}

enum RetCtx {
    Function,
    /// `return` inside a `parallel for` gives the value of the task
    Task { slot: String, cont: String, live: usize },
}

/// An owned temporary that must be released if the code fails before it
/// is used.
#[derive(Clone, Debug, PartialEq)]
enum Live {
    /// a register of type %V
    Reg(String),
    /// an alloca holding an owned %V
    Slot(String),
    /// an iterator (nx_iter)
    Iter(String),
    /// the lock of `shared` variables
    Lock,
}

struct FnState {
    frame: FrameId,
    allocas: String,
    body: String,
    terminated: bool,
    /// where a failure goes, and how many live temporaries it leaves alone
    unwind: Vec<(String, usize)>,
    live: Vec<Live>,
    /// allocas holding owned values released when the frame ends
    owned_slots: Vec<String>,
    /// blocks that release temporaries on the way to a handler
    pads: String,
    returns: Vec<RetCtx>,
    slots: HashMap<BindingId, Slot>,
    env: Option<String>,
    free_index: HashMap<BindingId, usize>,
    old_slots: HashMap<u32, String>,
    ret_slot: String,
    ret_label: String,
}

/// What a frame compiles from.
#[derive(Clone, Copy)]
enum FrameSrc<'a> {
    Fn(&'a FnDef),
    Lambda(&'a Expr),
    Step(&'a Expr),
    Default(&'a Expr),
    Test(&'a [Stmt]),
    Handler(&'a [Stmt]),
    /// the body of a `parallel for` (one call per element)
    Task(&'a [Stmt]),
    /// a statement of a `parallel:` block
    TaskStmt(&'a Stmt),
}

pub struct Gen<'a> {
    src: &'a str,
    res: &'a Resolution,
    opts: &'a Options,
    consts: String,
    globals: String,
    funcs: String,
    init: String,
    cstr: HashMap<Vec<u8>, String>,
    counter: usize,
    str_slots: HashMap<String, String>,
    unit_slots: HashMap<(String, String), String>,
    named_slots: HashMap<String, String>,
    lit_floats: HashMap<String, String>,
    descs: HashMap<String, String>,
    spans: Vec<Span>,
    span_ids: HashMap<(u32, u32, u32), u32>,
    frame_fns: HashMap<FrameId, (String, String)>,
    fs: Vec<FnState>,
    /// functions defined exactly once (called directly, without binding
    /// their arguments at run time)
    known_fns: HashMap<BindingId, &'a FnDef>,
    /// what the contract prover knows, and the type each method belongs to
    prover: Option<crate::prove::Types>,
    method_of: HashMap<u32, String>,
}

fn op_code(op: &str) -> u32 {
    match op {
        "+" => 0,
        "-" => 1,
        "*" => 2,
        "/" => 3,
        "//" => 4,
        "%" => 5,
        _ => 6,
    }
}

fn may_be_lazy(e: &Expr) -> bool {
    match &e.kind {
        ExprKind::Pipeline { .. } | ExprKind::Call { .. } => true,
        ExprKind::Coalesce { left, right } => may_be_lazy(left) || may_be_lazy(right),
        ExprKind::Propagate(x) => may_be_lazy(x),
        _ => false,
    }
}

fn builtin_id(name: &str) -> Option<u32> {
    Some(match name {
        "print" => 0,
        "input" => 1,
        "len" => 2,
        "range" => 3,
        "set" => 4,
        "round" => 5,
        "split" => 6,
        "convert" => 7,
        "fail" => 8,
        "validate" => 9,
        "decimal" => 10,
        "float" => 11,
        "int" => 12,
        "str" => 13,
        "read" => 14,
        "write" => 15,
        "read_lines" => 16,
        "save" => 17,
        _ => return None,
    })
}

/// Functions (not types) among the builtins, called directly.
const DIRECT_BUILTINS: &[&str] = &[
    "print", "input", "len", "range", "set", "round", "split", "convert", "fail", "validate", "read", "write",
    "read_lines", "save",
];

fn not_supported(what: String, span: Span) -> Diagnostic {
    Diagnostic::new("not_supported", span).arg("what", what)
}

/// The names, fields and old(...) values shown when a contract fails.
fn contract_operands(e: &Expr) -> Vec<&Expr> {
    match &e.kind {
        ExprKind::Name(_) => vec![e],
        ExprKind::Attr { obj, .. } => {
            let mut inner = obj.as_ref();
            while let ExprKind::Attr { obj, .. } = &inner.kind {
                inner = obj;
            }
            if matches!(inner.kind, ExprKind::Name(_)) {
                vec![e]
            } else {
                contract_operands(obj)
            }
        }
        ExprKind::Call { func, args } => {
            if matches!(&func.kind, ExprKind::Name(n) if n == "old") {
                return vec![e];
            }
            args.iter().flat_map(|a| contract_operands(&a.value)).collect()
        }
        _ => crate::checker::children(e).into_iter().flat_map(contract_operands).collect(),
    }
}

fn find_old_calls(e: &Expr) -> Vec<&Expr> {
    match &e.kind {
        ExprKind::Call { func, args } => {
            if matches!(&func.kind, ExprKind::Name(n) if n == "old") {
                return vec![e];
            }
            let mut out: Vec<&Expr> = args.iter().flat_map(|a| find_old_calls(&a.value)).collect();
            out.extend(find_old_calls(func));
            out
        }
        _ => crate::checker::children(e).into_iter().flat_map(find_old_calls).collect(),
    }
}

fn escape_bytes(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len());
    for b in bytes {
        if (0x20..0x7f).contains(b) && *b != b'"' && *b != b'\\' {
            out.push(*b as char);
        } else {
            let _ = write!(out, "\\{:02X}", b);
        }
    }
    out
}

/// Float constant as LLVM writes it (the exact bits, in hex).
fn float_bits(f: f64) -> String {
    format!("{}", f.to_bits() as i64)
}

impl<'a> Gen<'a> {
    pub fn new(src: &'a str, res: &'a Resolution, opts: &'a Options) -> Gen<'a> {
        Gen {
            src,
            res,
            opts,
            consts: String::new(),
            globals: String::new(),
            funcs: String::new(),
            init: String::new(),
            cstr: HashMap::new(),
            counter: 0,
            str_slots: HashMap::new(),
            unit_slots: HashMap::new(),
            named_slots: HashMap::new(),
            lit_floats: HashMap::new(),
            descs: HashMap::new(),
            spans: Vec::new(),
            span_ids: HashMap::new(),
            frame_fns: HashMap::new(),
            fs: Vec::new(),
            known_fns: HashMap::new(),
            prover: None,
            method_of: HashMap::new(),
        }
    }

    fn find_known_fns(&mut self, program: &'a Program) {
        let mut defs: HashMap<BindingId, usize> = HashMap::new();
        let mut fns: HashMap<BindingId, &'a FnDef> = HashMap::new();
        let res = self.res;
        crate::checker::walk_stmts(&program.body, true, &mut |s| match &s.kind {
            StmtKind::Fn(f) => {
                if let Some(b) = res.decl.get(&Node::Stmt(s.id)) {
                    *defs.entry(*b).or_default() += 1;
                    fns.insert(*b, f);
                }
            }
            StmtKind::Assign { .. } | StmtKind::VarDecl { .. } => {
                if let Some(a) = res.assigns.get(&s.id) {
                    *defs.entry(a.target).or_default() += 1;
                }
            }
            _ => {}
        });
        fns.retain(|b, f| defs.get(b) == Some(&1) && f.annotations.is_empty());
        self.known_fns = fns;
    }

    fn next(&mut self) -> usize {
        self.counter += 1;
        self.counter
    }

    fn f(&mut self) -> &mut FnState {
        self.fs.last_mut().unwrap()
    }

    // ------------------------------------------------------- constants

    /// A byte string constant; returns its global name.
    fn cstr(&mut self, s: &str) -> String {
        let bytes = s.as_bytes().to_vec();
        if let Some(n) = self.cstr.get(&bytes) {
            return n.clone();
        }
        let name = format!("@c.{}", self.next());
        let _ = writeln!(
            self.consts,
            "{name} = private unnamed_addr constant [{} x i8] c\"{}\"",
            bytes.len().max(1),
            if bytes.is_empty() { "\\00".to_string() } else { escape_bytes(&bytes) }
        );
        self.cstr.insert(bytes, name.clone());
        name
    }

    /// `ptr @c.N, i64 len`
    fn strarg(&mut self, s: &str) -> String {
        let c = self.cstr(s);
        format!("ptr {c}, i64 {}", s.len())
    }

    fn span_id(&mut self, span: Span) -> u32 {
        let key = (span.line, span.col, span.end_col);
        if let Some(id) = self.span_ids.get(&key) {
            return *id;
        }
        self.spans.push(span);
        let id = self.spans.len() as u32;
        self.span_ids.insert(key, id);
        id
    }

    /// A string value created once when the program starts.
    fn str_value(&mut self, s: &str) -> Val {
        let slot = match self.str_slots.get(s) {
            Some(n) => n.clone(),
            None => {
                let name = format!("@sv.{}", self.next());
                let _ = writeln!(self.globals, "{name} = internal global %V zeroinitializer");
                let arg = self.strarg(s);
                let r = format!("%i{}", self.next());
                let _ = writeln!(self.init, "  {r} = call %V @nx_str({arg})\n  store %V {r}, ptr {name}");
                self.str_slots.insert(s.to_string(), name.clone());
                name
            }
        };
        self.load_global(&slot)
    }

    fn unit_value(&mut self, text: &str, unit: &str) -> Val {
        let key = (text.to_string(), unit.to_string());
        let slot = match self.unit_slots.get(&key) {
            Some(n) => n.clone(),
            None => {
                let name = format!("@uv.{}", self.next());
                let _ = writeln!(self.globals, "{name} = internal global %V zeroinitializer");
                let a = self.strarg(text);
                let b = self.strarg(unit);
                let r = format!("%i{}", self.next());
                let _ = writeln!(self.init, "  {r} = call %V @nx_unit({a}, {b})\n  store %V {r}, ptr {name}");
                self.unit_slots.insert(key, name.clone());
                name
            }
        };
        self.load_global(&slot)
    }

    /// Builtin names that are values: types, error kinds, currencies...
    fn named_value(&mut self, name: &str) -> Val {
        let slot = match self.named_slots.get(name) {
            Some(n) => n.clone(),
            None => {
                let g = format!("@nv.{}", self.next());
                let _ = writeln!(self.globals, "{g} = internal global %V zeroinitializer");
                let a = self.strarg(name);
                let r = format!("%i{}", self.next());
                let _ = writeln!(self.init, "  {r} = call %V @nx_named({a})\n  store %V {r}, ptr {g}");
                self.named_slots.insert(name.to_string(), g.clone());
                g
            }
        };
        self.load_global(&slot)
    }

    /// A float literal that keeps its source text (section 17, rule 1).
    fn lit_float(&mut self, text: &str) -> Val {
        let name = match self.lit_floats.get(text) {
            Some(n) => n.clone(),
            None => {
                let value: f64 = text.parse().unwrap_or(f64::NAN);
                let c = self.cstr(text);
                let name = format!("@lf.{}", self.next());
                let _ = writeln!(
                    self.consts,
                    "{name} = private constant {{ i64, ptr, i64 }} {{ i64 {}, ptr {c}, i64 {} }}",
                    float_bits(value),
                    text.len()
                );
                self.lit_floats.insert(text.to_string(), name.clone());
                name
            }
        };
        lit(T_LITFLOAT, format!("ptrtoint (ptr {name} to i64)"))
    }

    /// The descriptor of a declared type (see runtime/src/types.rs).
    fn desc_text(&self, t: &TypeExpr) -> String {
        let display = type_text(t);
        let body = match t {
            TypeExpr::Union { literals, .. } => {
                let mut s = format!("l{};", literals.len());
                for l in literals {
                    match &l.kind {
                        ExprKind::Str(parts) => {
                            let text: String = parts
                                .iter()
                                .map(|p| match p {
                                    StrPiece::Lit(s) => s.as_str(),
                                    _ => "",
                                })
                                .collect();
                            let _ = write!(s, "s{}:{}", text.len(), text);
                        }
                        ExprKind::Num { text, is_float: false } => {
                            let _ = write!(s, "i{};", text);
                        }
                        ExprKind::Num { text, is_float: true } => {
                            let _ = write!(s, "f{};", text);
                        }
                        ExprKind::Bool(b) => s.push_str(if *b { "b1" } else { "b0" }),
                        ExprKind::Unit { text, unit, .. } => {
                            let _ = write!(s, "q{}:{}{}:{}", text.len(), text, unit.len(), unit);
                        }
                        _ => s.push('z'),
                    }
                }
                s
            }
            TypeExpr::Name(tn) => {
                let r = self.res.types.get(&(tn as *const TypeName as usize));
                let mut s = match r {
                    Some(TypeRes::User(id, name)) => format!("u{id}:{name};"),
                    Some(TypeRes::ErrorKind(k)) => format!("e{k};"),
                    Some(TypeRes::Builtin(n)) => format!("n{n};"),
                    None => format!("n{};", tn.name),
                };
                if matches!(r, Some(TypeRes::Builtin(_)) | None) && !tn.params.is_empty() {
                    s.push('[');
                    let parts: Vec<String> = tn.params.iter().map(|p| self.desc_text(p)).collect();
                    s.push_str(&parts.join(","));
                    s.push(']');
                }
                if tn.optional {
                    s.push('?');
                }
                s
            }
        };
        format!("{}:{}{}", display.len(), display, body)
    }

    /// `ptr @d.N, i64 len` for a declared type.
    fn desc_arg(&mut self, t: &TypeExpr) -> String {
        let text = self.desc_text(t);
        let name = match self.descs.get(&text) {
            Some(n) => n.clone(),
            None => {
                let c = self.cstr(&text);
                self.descs.insert(text.clone(), c.clone());
                c
            }
        };
        format!("ptr {name}, i64 {}", text.len())
    }

    // ------------------------------------------------------ instructions

    fn emit(&mut self, line: &str) {
        if self.f().terminated {
            let l = format!("D{}", self.next());
            let f = self.f();
            let _ = writeln!(f.body, "{l}:");
            f.terminated = false;
        }
        let f = self.f();
        f.body.push_str("  ");
        f.body.push_str(line);
        f.body.push('\n');
    }

    fn label(&mut self) -> String {
        format!("L{}", self.next())
    }

    fn place(&mut self, label: &str) {
        if !self.f().terminated {
            let f = self.f();
            let _ = writeln!(f.body, "  br label %{label}");
        }
        let f = self.f();
        let _ = writeln!(f.body, "{label}:");
        f.terminated = false;
    }

    fn br(&mut self, label: &str) {
        self.emit(&format!("br label %{label}"));
        self.f().terminated = true;
    }

    fn cond_br(&mut self, cond: &str, yes: &str, no: &str) {
        self.emit(&format!("br i1 {cond}, label %{yes}, label %{no}"));
        self.f().terminated = true;
    }

    fn reg(&mut self) -> String {
        format!("%r{}", self.next())
    }

    fn alloca(&mut self, ty: &str) -> String {
        let name = format!("%a{}", self.next());
        let f = self.f();
        let _ = writeln!(f.allocas, "  {name} = alloca {ty}");
        name
    }

    fn agg(&mut self, v: &Val) -> String {
        match v {
            Val::Reg(r) => r.clone(),
            Val::Lit(t, b) => format!("{{ i64 {t}, i64 {} }}", if b.is_empty() { "0" } else { b }),
        }
    }

    /// `i64 tag, i64 bits` of a value.
    fn tb(&mut self, v: &Val) -> String {
        match v {
            Val::Lit(t, b) => format!("i64 {t}, i64 {}", if b.is_empty() { "0" } else { b }),
            Val::Reg(r) => {
                let t = self.reg();
                let b = self.reg();
                self.emit(&format!("{t} = extractvalue %V {r}, 0"));
                self.emit(&format!("{b} = extractvalue %V {r}, 1"));
                format!("i64 {t}, i64 {b}")
            }
        }
    }

    fn tag_of(&mut self, v: &Val) -> String {
        match v {
            Val::Lit(t, _) => t.to_string(),
            Val::Reg(r) => {
                let t = self.reg();
                self.emit(&format!("{t} = extractvalue %V {r}, 0"));
                t
            }
        }
    }

    fn bits_of(&mut self, v: &Val) -> String {
        match v {
            Val::Lit(_, b) => if b.is_empty() { "0".into() } else { b.clone() },
            Val::Reg(r) => {
                let b = self.reg();
                self.emit(&format!("{b} = extractvalue %V {r}, 1"));
                b
            }
        }
    }

    fn call_v(&mut self, f: &str, args: &str) -> Val {
        let r = self.reg();
        self.emit(&format!("{r} = call %V @{f}({args})"));
        Val::Reg(r)
    }

    fn call_void(&mut self, f: &str, args: &str) {
        self.emit(&format!("call void @{f}({args})"));
    }

    /// Call returning i8; gives an i1 (`!= 0`).
    fn call_flag(&mut self, f: &str, args: &str) -> String {
        let r = self.reg();
        self.emit(&format!("{r} = call i8 @{f}({args})"));
        let c = self.reg();
        self.emit(&format!("{c} = icmp ne i8 {r}, 0"));
        c
    }

    /// The nearest handler, through a block that releases the live
    /// temporaries it does not keep.
    fn unwind_label(&mut self) -> String {
        let (label, keep) = self.fs.last().unwrap().unwind.last().unwrap().clone();
        let extra: Vec<Live> = self.f().live[keep..].to_vec();
        if extra.is_empty() {
            return label;
        }
        let pad = format!("PAD{}", self.next());
        let mut text = format!("{pad}:\n");
        for l in extra.iter().rev() {
            text.push_str(&self.release_text(l));
        }
        let _ = writeln!(text, "  br label %{label}");
        self.f().pads.push_str(&text);
        pad
    }

    /// IR that releases a live temporary (for blocks written apart).
    fn release_text(&mut self, l: &Live) -> String {
        match l {
            Live::Reg(r) => format!("  call void @rc_dec(%V {r})\n"),
            Live::Slot(a) => {
                let x = self.reg();
                format!("  {x} = load %V, ptr {a}\n  call void @rc_dec(%V {x})\n")
            }
            Live::Iter(i) => format!("  call void @nx_iter_free(ptr {i})\n"),
            Live::Lock => "  call void @nx_shared_unlock()\n".to_string(),
        }
    }

    fn failing_flag(&mut self) -> String {
        let f = self.reg();
        self.emit(&format!("{f} = load i8, ptr %tls"));
        f
    }

    /// After a call that can fail: go to the nearest handler.
    fn check(&mut self) {
        let f = self.failing_flag();
        let c = self.reg();
        self.emit(&format!("{c} = icmp ne i8 {f}, 0"));
        let ok = self.label();
        let unwind = self.unwind_label();
        self.cond_br(&c, &unwind, &ok);
        self.place(&ok);
    }

    /// The failure is recorded; go to the nearest handler.
    fn fail_here(&mut self) {
        let unwind = self.unwind_label();
        self.br(&unwind);
    }

    // ------------------------------------------------------- ownership

    fn is_live(&self, v: &Val) -> bool {
        match v {
            Val::Reg(r) => self.fs.last().unwrap().live.contains(&Live::Reg(r.clone())),
            Val::Lit(..) => false,
        }
    }

    fn unlive(&mut self, l: &Live) -> bool {
        let live = &mut self.f().live;
        match live.iter().rposition(|x| x == l) {
            Some(i) => {
                live.remove(i);
                true
            }
            None => false,
        }
    }

    /// A value the runtime returned: owned by this code until used.
    fn owned(&mut self, v: Val) -> Val {
        if let Val::Reg(r) = &v {
            self.f().live.push(Live::Reg(r.clone()));
        }
        v
    }

    fn rc_inc(&mut self, v: &Val) {
        if let Val::Lit(t, _) = v {
            if *t != T_OBJ {
                return;
            }
        }
        let x = self.agg(v);
        self.emit(&format!("call void @rc_inc(%V {x})"));
    }

    fn rc_dec(&mut self, v: &Val) {
        if let Val::Lit(t, _) = v {
            if *t != T_OBJ {
                return;
            }
        }
        let x = self.agg(v);
        self.emit(&format!("call void @rc_dec(%V {x})"));
    }

    /// Take ownership of a value to store it: an owned temporary is moved,
    /// a borrowed value gets a new reference.
    fn own(&mut self, v: &Val) {
        let moved = match v {
            Val::Reg(r) => self.unlive(&Live::Reg(r.clone())),
            Val::Lit(..) => false,
        };
        if !moved {
            self.rc_inc(v);
        }
    }

    /// The value is no longer needed.
    fn drop_val(&mut self, v: &Val) {
        if let Val::Reg(r) = v {
            if self.unlive(&Live::Reg(r.clone())) {
                self.rc_dec(v);
            }
        }
    }

    /// Keep a value in a stack slot across control flow. An owned value
    /// stays owned by the slot (released by `drop_slot`); a borrowed one
    /// stays borrowed.
    fn spill(&mut self, v: &Val) -> String {
        let a = self.alloca("%V");
        self.store(&a, v);
        if let Val::Reg(r) = v {
            if self.unlive(&Live::Reg(r.clone())) {
                self.f().live.push(Live::Slot(a.clone()));
            }
        }
        a
    }

    /// An owned value in a slot (taking a reference if it was borrowed).
    fn spill_owned(&mut self, v: &Val) -> String {
        self.own(v);
        let a = self.alloca("%V");
        self.store(&a, v);
        self.f().live.push(Live::Slot(a.clone()));
        a
    }

    /// Release what a slot made by `spill` owns.
    fn drop_slot(&mut self, a: &str) {
        if self.unlive(&Live::Slot(a.to_string())) {
            let x = self.load(a);
            self.rc_dec(&x);
        }
    }

    /// The owned value of a slot becomes an owned temporary again.
    fn unspill(&mut self, a: &str) -> Val {
        let x = self.load(a);
        if self.unlive(&Live::Slot(a.to_string())) {
            return self.owned(x);
        }
        x
    }

    /// Release every live temporary above `keep` on this path (a `return`),
    /// leaving the list as it is for the code that follows.
    fn release_live_from(&mut self, keep: usize) {
        let extra: Vec<Live> = self.f().live[keep..].to_vec();
        for l in extra.iter().rev() {
            let text = self.release_text(l);
            for line in text.lines() {
                self.emit(line.trim());
            }
        }
    }

    fn load_global(&mut self, g: &str) -> Val {
        let r = self.reg();
        self.emit(&format!("{r} = load %V, ptr {g}"));
        Val::Reg(r)
    }

    /// A temporary stack slot holding a value.
    fn temp(&mut self, v: &Val) -> String {
        let a = self.alloca("%V");
        let x = self.agg(v);
        self.emit(&format!("store %V {x}, ptr {a}"));
        a
    }

    fn load(&mut self, ptr: &str) -> Val {
        let r = self.reg();
        self.emit(&format!("{r} = load %V, ptr {ptr}"));
        Val::Reg(r)
    }

    fn store(&mut self, ptr: &str, v: &Val) {
        let x = self.agg(v);
        self.emit(&format!("store %V {x}, ptr {ptr}"));
    }

    /// An array of values on the stack; `ptr` operand (or null).
    fn values_array(&mut self, vals: &[Val]) -> String {
        if vals.is_empty() {
            return "null".into();
        }
        let a = self.alloca(&format!("[{} x %V]", vals.len()));
        for (i, v) in vals.iter().enumerate() {
            let p = self.reg();
            self.emit(&format!("{p} = getelementptr inbounds [{} x %V], ptr {a}, i64 0, i64 {i}", vals.len()));
            self.store(&p, v);
        }
        a
    }

    /// A constant array of names (null for positional arguments).
    fn names_array(&mut self, names: &[Option<&str>]) -> String {
        if names.iter().all(|n| n.is_none()) {
            return "null".into();
        }
        let mut items = Vec::new();
        for n in names {
            match n {
                Some(n) => {
                    let c = self.cstr(n);
                    items.push(format!("{{ ptr, i64 }} {{ ptr {c}, i64 {} }}", n.len()));
                }
                None => items.push("{ ptr, i64 } { ptr null, i64 0 }".into()),
            }
        }
        let name = format!("@nm.{}", self.next());
        let _ = writeln!(self.consts, "{name} = private constant [{} x {{ ptr, i64 }}] [{}]", names.len(), items.join(", "));
        name
    }

    fn is_tag(&mut self, v: &Val, tag: u64) -> String {
        let t = self.tag_of(v);
        let c = self.reg();
        self.emit(&format!("{c} = icmp eq i64 {t}, {tag}"));
        c
    }

    /// Conditions must be bool (D28): an i1 for a branch.
    fn truth(&mut self, v: &Val, span: Span, what: &str) -> String {
        if let Val::Lit(T_BOOL, b) = v {
            return if b == "0" { "false".into() } else { "true".into() };
        }
        let ok = self.label();
        let bad = self.label();
        let isb = self.is_tag(v, T_BOOL);
        self.cond_br(&isb, &ok, &bad);
        self.place(&bad);
        let tb = self.tb(v);
        let w = self.strarg(what);
        let sp = self.span_id(span);
        self.call_void("nx_not_bool", &format!("{tb}, {w}, i32 {sp}"));
        self.fail_here();
        self.place(&ok);
        let bits = self.bits_of(v);
        let c = self.reg();
        self.emit(&format!("{c} = icmp ne i64 {bits}, 0"));
        // the condition uses the value up
        self.drop_val(v);
        c
    }

    fn bool_val(&mut self, cond: &str) -> Val {
        let z = self.reg();
        self.emit(&format!("{z} = zext i1 {cond} to i64"));
        let r = self.reg();
        self.emit(&format!("{r} = insertvalue %V {{ i64 1, i64 0 }}, i64 {z}, 1"));
        Val::Reg(r)
    }

    /// Read lazy sequences and drop the literal tag of floats (a value
    /// that is stored somewhere).
    fn bind(&mut self, v: Val, e: &Expr) -> Val {
        if !may_be_lazy(e) {
            return self.plain(v);
        }
        let tb = self.tb(&v);
        let r = self.call_v("nx_bind", &tb);
        self.check();
        self.drop_val(&v);
        self.owned(r)
    }

    /// Drop the literal tag of a float (no heap value changes, so the
    /// result has the same owner as `v`).
    fn plain(&mut self, v: Val) -> Val {
        match &v {
            Val::Lit(..) => v,
            Val::Reg(r) => {
                let x = self.reg();
                self.emit(&format!("{x} = call %V @nx_plainv(%V {r})"));
                if self.unlive(&Live::Reg(r.clone())) {
                    self.f().live.push(Live::Reg(x.clone()));
                }
                Val::Reg(x)
            }
        }
    }

    // ---------------------------------------------------------- bindings

    fn slot_of(&self, b: BindingId) -> Option<Slot> {
        self.fs.last().unwrap().slots.get(&b).cloned()
    }

    /// Pointer to the storage of a binding (global, local or cell).
    fn binding_ptr(&mut self, b: BindingId) -> String {
        if self.res.is_static(b) {
            return format!("@g.{b}");
        }
        match self.slot_of(b) {
            Some(Slot::Value(a)) => a,
            Some(Slot::Cell(a)) => {
                let p = self.reg();
                self.emit(&format!("{p} = load ptr, ptr {a}"));
                p
            }
            None => {
                let idx = *self.fs.last().unwrap().free_index.get(&b).unwrap_or_else(|| {
                    panic!("binding {} ({}) is not reachable", b, self.res.bindings[b].name)
                });
                let env = self.fs.last().unwrap().env.clone().unwrap();
                let pp = self.reg();
                self.emit(&format!("{pp} = getelementptr inbounds ptr, ptr {env}, i64 {idx}"));
                let p = self.reg();
                self.emit(&format!("{p} = load ptr, ptr {pp}"));
                p
            }
        }
    }

    fn load_binding(&mut self, b: BindingId) -> Val {
        let p = self.binding_ptr(b);
        self.load(&p)
    }

    /// Store into a binding a value that is already owned for it.
    fn store_binding_raw(&mut self, b: BindingId, v: &Val) {
        let p = self.binding_ptr(b);
        let old = self.load(&p);
        self.store(&p, v);
        self.rc_dec(&old);
    }

    /// Store into a binding, which takes ownership of `v`; the old value
    /// is released.
    fn store_binding(&mut self, b: BindingId, v: &Val) {
        self.own(v);
        let p = self.binding_ptr(b);
        let old = self.load(&p);
        self.store(&p, v);
        self.rc_dec(&old);
    }

    /// Cell pointer of a captured binding, to build a closure.
    fn cell_ptr(&mut self, b: BindingId) -> String {
        match self.slot_of(b) {
            Some(Slot::Cell(a)) => {
                let p = self.reg();
                self.emit(&format!("{p} = load ptr, ptr {a}"));
                p
            }
            Some(Slot::Value(_)) => panic!("binding {b} is not captured"),
            None => {
                let idx = self.fs.last().unwrap().free_index[&b];
                let env = self.fs.last().unwrap().env.clone().unwrap();
                let pp = self.reg();
                self.emit(&format!("{pp} = getelementptr inbounds ptr, ptr {env}, i64 {idx}"));
                let p = self.reg();
                self.emit(&format!("{p} = load ptr, ptr {pp}"));
                p
            }
        }
    }

    /// Fresh storage for the bindings of a scope that starts again (a
    /// function, a loop iteration, a match arm).
    fn enter_scope(&mut self, scope: ScopeId) {
        let names: Vec<BindingId> = self.res.scopes[scope].names.iter().map(|(_, b)| *b).collect();
        for b in names {
            self.reset_binding(b);
        }
    }

    fn reset_binding(&mut self, b: BindingId) {
        if self.res.is_static(b) {
            return;
        }
        match self.slot_of(b) {
            Some(Slot::Cell(a)) => {
                let old = self.reg();
                self.emit(&format!("{old} = load ptr, ptr {a}"));
                let c = self.reg();
                self.emit(&format!("{c} = call ptr @nx_cell()"));
                self.emit(&format!("store ptr {c}, ptr {a}"));
                self.emit(&format!("call void @nx_cell_release(ptr {old})"));
            }
            Some(Slot::Value(a)) => {
                let old = self.load(&a);
                self.emit(&format!("store %V {{ i64 {T_UNDEF}, i64 0 }}, ptr {a}"));
                self.rc_dec(&old);
            }
            None => {}
        }
    }

    /// IR that releases every slot of the current frame (its end).
    fn frame_release_text(&mut self) -> String {
        let mut slots: Vec<(BindingId, Slot)> = self.f().slots.iter().map(|(b, s)| (*b, s.clone())).collect();
        slots.sort_by_key(|(b, _)| *b);
        let mut text = String::new();
        for (_, s) in slots {
            match s {
                Slot::Value(a) => text.push_str(&self.release_text(&Live::Slot(a))),
                Slot::Cell(a) => {
                    let c = self.reg();
                    let _ = writeln!(text, "  {c} = load ptr, ptr {a}\n  call void @nx_cell_release(ptr {c})");
                }
            }
        }
        let owned: Vec<String> = self.f().owned_slots.clone();
        for a in owned {
            text.push_str(&self.release_text(&Live::Slot(a)));
        }
        text
    }

    /// Every slot starts empty, so the end of the frame can release them
    /// all whatever path it took.
    fn init_frame_slots(&mut self) {
        let mut slots: Vec<(BindingId, Slot)> = self.f().slots.iter().map(|(b, s)| (*b, s.clone())).collect();
        slots.sort_by_key(|(b, _)| *b);
        for (_, s) in slots {
            match s {
                Slot::Value(a) => self.emit(&format!("store %V {{ i64 {T_UNDEF}, i64 0 }}, ptr {a}")),
                Slot::Cell(a) => self.emit(&format!("store ptr null, ptr {a}")),
            }
        }
    }

    /// An alloca owned by the frame (released when it ends).
    fn frame_owned_slot(&mut self) -> String {
        let a = self.alloca("%V");
        // initialised in the entry block, before any path can end the frame
        let f = self.f();
        let _ = writeln!(f.allocas, "  store %V {{ i64 0, i64 0 }}, ptr {a}");
        f.owned_slots.push(a.clone());
        a
    }

    /// Is the binding assigned? (an i1)
    fn defined(&mut self, b: BindingId) -> String {
        let v = self.load_binding(b);
        let t = self.tag_of(&v);
        let c = self.reg();
        self.emit(&format!("{c} = icmp ne i64 {t}, {T_UNDEF}"));
        c
    }

    // ------------------------------------------------------------ frames

    /// The LLVM function of a frame (compiled the first time it is needed)
    /// and its FnInfo constant.
    fn frame_fn(&mut self, frame: FrameId, src: FrameSrc<'a>) -> GResult<(String, String)> {
        if let Some(x) = self.frame_fns.get(&frame) {
            return Ok(x.clone());
        }
        let n = self.next();
        let name = format!("@fr.{n}");
        let info = format!("@fi.{n}");
        self.frame_fns.insert(frame, (name.clone(), info.clone()));
        // FnInfo: name and parameters
        let (fname, params): (String, Vec<(String, bool)>) = match src {
            FrameSrc::Fn(f) => (
                f.name.clone(),
                f.params
                    .iter()
                    .map(|p| (p.name.clone(), p.default.is_some() || p.ty.as_ref().is_some_and(|t| t.optional())))
                    .collect(),
            ),
            FrameSrc::Lambda(_) => {
                let p = &self.res.bindings[self.res.frames[frame].params[0]].name;
                ("lambda".into(), vec![(p.clone(), false)])
            }
            FrameSrc::Step(_) => ("lambda".into(), vec![("it".into(), false)]),
            FrameSrc::Default(_) => ("default".into(), vec![]),
            FrameSrc::Test(_) => ("test".into(), vec![]),
            FrameSrc::Handler(_) => {
                let names: Vec<(String, bool)> =
                    self.res.frames[frame].params.iter().map(|b| (self.res.bindings[*b].name.clone(), false)).collect();
                ("on".into(), names)
            }
            FrameSrc::Task(_) => {
                let p = &self.res.bindings[self.res.frames[frame].params[0]].name;
                ("task".into(), vec![(p.clone(), false)])
            }
            FrameSrc::TaskStmt(_) => ("task".into(), vec![]),
        };
        let fn_c = self.cstr(&fname);
        let mut pitems = Vec::new();
        for (p, opt) in &params {
            let c = self.cstr(p);
            pitems.push(format!("{{ ptr, i64, i64 }} {{ ptr {c}, i64 {}, i64 {} }}", p.len(), *opt as u8));
        }
        let pi = if params.is_empty() {
            "null".to_string()
        } else {
            let pn = format!("@pi.{n}");
            let _ = writeln!(self.consts, "{pn} = private constant [{} x {{ ptr, i64, i64 }}] [{}]", params.len(), pitems.join(", "));
            pn
        };
        let _ = writeln!(
            self.consts,
            "{info} = private constant {{ ptr, i64, i64, ptr }} {{ ptr {fn_c}, i64 {}, i64 {}, ptr {pi} }}",
            fname.len(),
            params.len()
        );
        self.compile_frame(frame, src, &name)?;
        Ok((name, info))
    }

    fn new_fn_state(&mut self, frame: FrameId) -> FnState {
        let n = self.next();
        FnState {
            frame,
            allocas: String::new(),
            body: String::new(),
            terminated: false,
            unwind: vec![(format!("UNWIND{n}"), 0)],
            live: Vec::new(),
            owned_slots: Vec::new(),
            pads: String::new(),
            returns: vec![RetCtx::Function],
            slots: HashMap::new(),
            env: None,
            free_index: HashMap::new(),
            old_slots: HashMap::new(),
            ret_slot: String::new(),
            ret_label: format!("RET{n}"),
        }
    }

    /// Allocate the slots of every non-static binding of a frame.
    fn frame_slots(&mut self, frame: FrameId) {
        let ids: Vec<BindingId> =
            (0..self.res.bindings.len()).filter(|b| self.res.bindings[*b].frame == frame && !self.res.is_static(*b)).collect();
        for b in ids {
            if self.res.bindings[b].captured {
                let a = self.alloca("ptr");
                self.f().slots.insert(b, Slot::Cell(a));
            } else {
                let a = self.alloca("%V");
                self.f().slots.insert(b, Slot::Value(a));
            }
        }
    }

    fn compile_frame(&mut self, frame: FrameId, src: FrameSrc<'a>, name: &str) -> GResult<()> {
        let st = self.new_fn_state(frame);
        self.fs.push(st);
        self.frame_slots(frame);
        self.init_frame_slots();
        let ret_slot = self.alloca("%V");
        self.emit(&format!("store %V {{ i64 0, i64 0 }}, ptr {ret_slot}"));
        self.f().ret_slot = ret_slot.clone();
        // count nested calls (D73)
        let dp = self.reg();
        self.emit(&format!("{dp} = getelementptr inbounds i8, ptr %tls, i64 8"));
        let d = self.reg();
        self.emit(&format!("{d} = load i64, ptr {dp}"));
        let d1 = self.reg();
        self.emit(&format!("{d1} = add i64 {d}, 1"));
        self.emit(&format!("store i64 {d1}, ptr {dp}"));
        let deep = self.reg();
        self.emit(&format!("{deep} = icmp ugt i64 {d1}, {MAX_DEPTH}"));
        let too = self.label();
        let fine = self.label();
        self.cond_br(&deep, &too, &fine);
        self.place(&too);
        self.call_void("nx_too_deep", "");
        self.fail_here();
        self.place(&fine);
        let free = self.res.frames[frame].free.clone();
        if !free.is_empty() {
            let p = self.reg();
            self.emit(&format!("{p} = getelementptr inbounds i8, ptr %clo, i64 16"));
            let env = self.reg();
            self.emit(&format!("{env} = load ptr, ptr {p}"));
            self.f().env = Some(env);
            for (i, b) in free.iter().enumerate() {
                self.f().free_index.insert(*b, i);
            }
        }
        let root = self.res.frames[frame].root;
        self.enter_scope(root);
        // parameters: the arguments are borrowed, the slots own them
        let params = self.res.frames[frame].params.clone();
        for (i, b) in params.iter().enumerate() {
            let p = self.reg();
            self.emit(&format!("{p} = getelementptr inbounds %V, ptr %args, i64 {i}"));
            let v = self.load(&p);
            let v = match src {
                FrameSrc::Fn(f) => self.param_value(f, i, v)?,
                _ => self.plain(v),
            };
            self.store_binding(*b, &v);
        }
        let mut ensures: Vec<&Expr> = Vec::new();
        match src {
            FrameSrc::Fn(f) => {
                // a contract the compiler proves costs nothing at run time;
                // `--trust-ensure` leaves out the others too
                let self_type = self.method_of.get(&f.id).cloned();
                let proven = crate::prove::ensures(f, self_type.as_deref(), self.prover.as_ref().unwrap());
                let mut k = 0;
                for s in &f.body {
                    if let StmtKind::Ensure(e) = &s.kind {
                        if !proven[k] {
                            ensures.push(e);
                        }
                        k += 1;
                    }
                }
                // old() values are taken when the function is entered
                if !self.opts.trust_ensure {
                    for e in &ensures {
                        for call in find_old_calls(e) {
                            let ExprKind::Call { args, .. } = &call.kind else { continue };
                            let Some(arg) = args.first() else { continue };
                            let v = self.expr(&arg.value, false)?;
                            let v = self.bind(v, &arg.value);
                            let tb = self.tb(&v);
                            let c = self.call_v("nx_deep_copy", &tb);
                            self.drop_val(&v);
                            let slot = self.frame_owned_slot();
                            self.store(&slot, &c);
                            self.f().old_slots.insert(call.id, slot);
                        }
                    }
                }
                self.block(&f.body)?;
            }
            FrameSrc::Lambda(body) => {
                let v = self.expr(body, false)?;
                let v = self.bind(v, body);
                self.own(&v);
                self.store(&ret_slot, &v);
            }
            FrameSrc::Step(e) | FrameSrc::Default(e) => {
                let v = self.expr(e, true)?;
                self.own(&v);
                self.store(&ret_slot, &v);
            }
            FrameSrc::Test(body) | FrameSrc::Handler(body) | FrameSrc::Task(body) => {
                self.block(body)?;
            }
            FrameSrc::TaskStmt(st) => {
                self.block(std::slice::from_ref(st))?;
            }
        }
        debug_assert!(self.f().live.is_empty(), "live temporaries at the end of a frame");
        let ret_label = self.f().ret_label.clone();
        self.br(&ret_label);
        self.place(&ret_label);
        let mut result = self.load(&ret_slot);
        if let FrameSrc::Fn(f) = src {
            if !self.opts.trust_ensure {
                for e in &ensures {
                    let v = self.expr(e, false)?;
                    let c = self.truth(&v, e.span, "ensure");
                    let ok = self.label();
                    let bad = self.label();
                    self.cond_br(&c, &ok, &bad);
                    self.place(&bad);
                    self.contract_broken("ensure", e)?;
                    self.place(&ok);
                }
            }
            if let Some(ret) = &f.ret {
                let tb = self.tb(&result);
                let d = self.desc_arg(ret);
                let n = self.strarg(&f.name);
                let sp = self.span_id(f.name_span);
                result = self.call_v("nx_conform_return", &format!("{tb}, {d}, {n}, i32 {sp}"));
                self.check();
                // the slot keeps the value before the check; the result is new
                let old = self.load(&ret_slot);
                self.rc_dec(&old);
            }
        }
        let r = self.agg(&result);
        // the slots are released; the result goes to the caller
        let mut text = String::new();
        let _ = writeln!(text, "define internal %V {name}(ptr %tls, ptr %clo, ptr %args, i32 %span) {{\nentry:");
        let release = self.frame_release_text();
        for line in release.lines() {
            self.emit(line.trim());
        }
        self.leave_depth();
        self.emit(&format!("ret %V {r}"));
        self.f().terminated = true;
        let unwinds: Vec<String> = self.f().unwind.iter().map(|(u, _)| u.clone()).collect();
        let mut tail = String::new();
        for u in &unwinds {
            let release = self.frame_release_text();
            let ret_release = self.release_text(&Live::Slot(ret_slot.clone()));
            let n = self.next();
            let _ = writeln!(
                tail,
                "{u}:\n{release}{ret_release}  %ud{n}p = getelementptr inbounds i8, ptr %tls, i64 8\n  %ud{n} = load i64, ptr %ud{n}p\n  %ud{n}x = sub i64 %ud{n}, 1\n  store i64 %ud{n}x, ptr %ud{n}p\n  ret %V {{ i64 0, i64 0 }}"
            );
        }
        let st = self.fs.pop().unwrap();
        text.push_str(&st.allocas);
        text.push_str(&st.body);
        text.push_str(&tail);
        text.push_str(&st.pads);
        text.push_str("}\n\n");
        self.funcs.push_str(&text);
        Ok(())
    }

    fn leave_depth(&mut self) {
        let dp = self.reg();
        self.emit(&format!("{dp} = getelementptr inbounds i8, ptr %tls, i64 8"));
        let d = self.reg();
        self.emit(&format!("{d} = load i64, ptr {dp}"));
        let d1 = self.reg();
        self.emit(&format!("{d1} = sub i64 {d}, 1"));
        self.emit(&format!("store i64 {d1}, ptr {dp}"));
    }

    /// The value of parameter i on entry (owned): its default when it was
    /// not passed, checked against its declared type.
    fn param_value(&mut self, f: &'a FnDef, i: usize, v: Val) -> GResult<Val> {
        let p = &f.params[i];
        let slot = self.alloca("%V");
        let missing = self.is_tag(&v, T_MISSING);
        let yes = self.label();
        let no = self.label();
        let done = self.label();
        self.cond_br(&missing, &yes, &no);
        self.place(&yes);
        let d = match &p.default {
            Some(d) => self.expr(d, true)?,
            None => none(),
        };
        self.own(&d);
        self.store(&slot, &d);
        self.br(&done);
        self.place(&no);
        self.rc_inc(&v);
        self.store(&slot, &v);
        self.br(&done);
        self.place(&done);
        self.f().live.push(Live::Slot(slot.clone()));
        let mut v = self.unspill(&slot);
        if let Some(t) = &p.ty {
            let tb = self.tb(&v);
            let d = self.desc_arg(t);
            let n = self.strarg(&p.name);
            let c = self.call_v("nx_conform", &format!("{tb}, {d}, {n}, i32 %span"));
            self.check();
            self.drop_val(&v);
            v = self.owned(c);
        }
        Ok(self.plain(v))
    }

    /// A closure value for a frame: its code and the cells it captures.
    fn closure(&mut self, frame: FrameId, src: FrameSrc<'a>) -> GResult<Val> {
        let (name, info) = self.frame_fn(frame, src)?;
        let free = self.res.frames[frame].free.clone();
        let cells = if free.is_empty() {
            "null".to_string()
        } else {
            let a = self.alloca(&format!("[{} x ptr]", free.len()));
            for (i, b) in free.iter().enumerate() {
                let c = self.cell_ptr(*b);
                let p = self.reg();
                self.emit(&format!("{p} = getelementptr inbounds [{} x ptr], ptr {a}, i64 0, i64 {i}", free.len()));
                self.emit(&format!("store ptr {c}, ptr {p}"));
            }
            a
        };
        let r = self.call_v("nx_closure", &format!("ptr {name}, ptr {info}, ptr {cells}, i64 {}", free.len()));
        Ok(self.owned(r))
    }

    // ------------------------------------------------------------ module

    pub fn program(mut self, program: &'a Program) -> GResult<String> {
        self.find_known_fns(program);
        self.prover = Some(crate::prove::Types::new(program));
        crate::checker::walk_stmts(&program.body, true, &mut |s| {
            if let StmtKind::Type(t) = &s.kind {
                for m in &t.methods {
                    self.method_of.insert(m.id, t.name.clone());
                }
            }
        });
        let mut st = self.new_fn_state(GLOBAL_FRAME);
        st.unwind = vec![("UNHANDLED".into(), 0)];
        self.fs.push(st);
        self.frame_slots(GLOBAL_FRAME);
        let src_c = self.cstr(self.src);
        let file_c = self.cstr(&self.opts.file.clone());
        let file_len = self.opts.file.len();
        let src_len = self.src.len();
        self.emit(&format!(
            "call void @nx_init(ptr {file_c}, i64 {file_len}, ptr {src_c}, i64 {src_len}, ptr @nx_spans, i64 SPANS_COUNT, i32 {}, ptr @nexo_cleanup)",
            self.opts.lang
        ));
        self.emit("call void @nx_module_init()");
        self.init_frame_slots();
        // every top-level name starts unassigned
        let mut cleanup = String::new();
        for b in 0..self.res.bindings.len() {
            if self.res.is_static(b) {
                let _ = writeln!(self.globals, "@g.{b} = internal global %V {{ i64 {T_UNDEF}, i64 0 }}");
                let _ = writeln!(
                    cleanup,
                    "  %c{b} = load %V, ptr @g.{b}\n  store %V {{ i64 {T_UNDEF}, i64 0 }}, ptr @g.{b}\n  call void @rc_dec(%V %c{b})"
                );
            }
        }
        let root = self.res.frames[GLOBAL_FRAME].root;
        self.enter_scope(root);
        self.block(&program.body)?;
        // the program's own loop variables and cells
        let release = self.frame_release_text();
        for line in release.lines() {
            self.emit(line.trim());
        }
        if self.opts.test_mode {
            self.emit("call void @nx_run_tests()");
        } else {
            self.emit("call void @nx_exit(i32 0)");
        }
        self.emit("unreachable");
        self.f().terminated = true;
        let st = self.fs.pop().unwrap();
        let mut main = String::new();
        main.push_str("define internal void @nexo_main(ptr %tls) {\nentry:\n");
        main.push_str(&st.allocas);
        main.push_str(&st.body);
        main.push_str(&st.pads);
        main.push_str("UNHANDLED:\n  call void @nx_unhandled()\n  unreachable\n}\n\n");
        main.push_str("define internal void @nexo_cleanup(ptr %tls) {\nentry:\n");
        main.push_str(&cleanup);
        main.push_str("  ret void\n}\n\n");
        main.push_str("define i32 @main(i32 %argc, ptr %argv) {\nentry:\n  call void @nx_start(ptr @nexo_main)\n  unreachable\n}\n\n");

        let mut out = String::new();
        out.push_str("; generated by the Nexo compiler\n%V = type { i64, i64 }\n\n");
        out.push_str(RUNTIME_DECLS);
        out.push('\n');
        let spans: Vec<String> = self
            .spans
            .iter()
            .map(|s| format!("[3 x i32] [i32 {}, i32 {}, i32 {}]", s.line, s.col, s.end_col.min(i32::MAX as u32)))
            .collect();
        let _ = writeln!(
            out,
            "@nx_spans = private constant [{} x [3 x i32]] [{}]",
            spans.len().max(1),
            if spans.is_empty() { "[3 x i32] zeroinitializer".to_string() } else { spans.join(", ") }
        );
        out.push_str(&self.consts);
        out.push('\n');
        out.push_str(&self.globals);
        out.push('\n');
        out.push_str("define internal void @nx_module_init() {\nentry:\n");
        out.push_str(&self.init);
        out.push_str("  ret void\n}\n\n");
        out.push_str(&self.funcs);
        out.push_str(&main.replace("SPANS_COUNT", &self.spans.len().to_string()));
        Ok(out)
    }

    // -------------------------------------------------------- statements

    fn block(&mut self, body: &'a [Stmt]) -> GResult<()> {
        // names assigned so far in this statement list (for the static
        // "immutable" check of straight-line code)
        let mut assigned: Vec<&str> = Vec::new();
        for s in body {
            self.static_assign_check(s, &mut assigned)?;
            self.deadline_tick();
            let before = self.f().live.len();
            // a statement that uses a shared variable holds its lock
            let locked = self.uses_shared(s);
            if locked {
                self.call_void("nx_shared_lock", "");
                self.f().live.push(Live::Lock);
            }
            self.stmt(s)?;
            if locked {
                self.unlive(&Live::Lock);
                self.call_void("nx_shared_unlock", "");
            }
            debug_assert_eq!(self.f().live.len(), before, "temporaries left by a statement at line {}", s.span.line);
        }
        Ok(())
    }

    fn static_assign_check(&mut self, s: &'a Stmt, assigned: &mut Vec<&'a str>) -> GResult<()> {
        match &s.kind {
            StmtKind::Assign { target: Expr { kind: ExprKind::Name(n), span, .. }, ty, .. } => {
                let a = &self.res.assigns[&s.id];
                let b = &self.res.bindings[a.target];
                if assigned.contains(&n.as_str()) && b.kind == BKind::Const && a.cands.first().map(|c| c.0) == Some(a.target) {
                    let _ = ty;
                    return Err(Diagnostic::new("immutable", *span).arg("name", n.clone()));
                }
                assigned.push(n);
            }
            StmtKind::VarDecl { name, name_span, .. } => {
                if assigned.contains(&name.as_str()) {
                    let a = &self.res.assigns[&s.id];
                    if self.res.bindings[a.target].kind == BKind::Var {
                        return Err(Diagnostic::new("already_declared", *name_span).arg("name", name.clone()));
                    }
                }
                assigned.push(name);
            }
            _ => {}
        }
        Ok(())
    }

    /// `try within` checks the clock between statements (D58).
    fn deadline_tick(&mut self) {
        let pa = self.reg();
        self.emit(&format!("{pa} = getelementptr inbounds i8, ptr %tls, i64 1"));
        let d = self.reg();
        self.emit(&format!("{d} = load i8, ptr {pa}"));
        let c = self.reg();
        self.emit(&format!("{c} = icmp ne i8 {d}, 0"));
        let yes = self.label();
        let done = self.label();
        self.cond_br(&c, &yes, &done);
        self.place(&yes);
        self.call_void("nx_check_deadline", "");
        self.check();
        self.br(&done);
        self.place(&done);
    }

    fn stmt(&mut self, s: &'a Stmt) -> GResult<()> {
        match &s.kind {
            StmtKind::Expr(e) => {
                let v = self.expr(e, false)?;
                self.drop_val(&v);
            }
            StmtKind::Pass | StmtKind::Ensure(_) => {}
            StmtKind::Assign { target, op, value, ty } => self.assign(s, target, op, value, ty.as_ref())?,
            StmtKind::VarDecl { name, name_span, ty, value } => {
                let v = self.expr(value, ty.is_some())?;
                let v = match ty {
                    Some(t) => self.conform(v, t, name, value.span)?,
                    None => v,
                };
                let v = self.bind(v, value);
                let target = self.res.assigns[&s.id].target;
                let def = self.defined(target);
                let bad = self.label();
                let ok = self.label();
                self.cond_br(&def, &bad, &ok);
                self.place(&bad);
                let n = self.strarg(name);
                let sp = self.span_id(*name_span);
                self.call_void("nx_already_declared", &format!("{n}, i32 {sp}"));
                self.fail_here();
                self.place(&ok);
                self.store_binding(target, &v);
            }
            StmtKind::Return(value) => {
                let v = match value {
                    Some(e) => {
                        let v = self.expr(e, false)?;
                        self.bind(v, e)
                    }
                    None => none(),
                };
                match self.fs.last().unwrap().returns.last().unwrap() {
                    RetCtx::Function => {
                        let slot = self.f().ret_slot.clone();
                        let l = self.f().ret_label.clone();
                        self.own(&v);
                        self.store(&slot, &v);
                        // a return leaves every temporary of the frame
                        self.release_live_from(0);
                        self.br(&l);
                        if let Val::Reg(r) = &v {
                            // the compile-time list stays as it was for the code after
                            let _ = r;
                        }
                    }
                    RetCtx::Task { slot, cont, live } => {
                        let (slot, cont, keep) = (slot.clone(), cont.clone(), *live);
                        self.own(&v);
                        let old = self.load(&slot);
                        self.store(&slot, &v);
                        self.rc_dec(&old);
                        self.release_live_from(keep);
                        self.br(&cont);
                    }
                }
            }
            StmtKind::Use { what } if what == "nexo.sync" => {}
            StmtKind::Use { what } => return Err(not_supported(format!("\"use {what}\""), s.span)),
            StmtKind::Spawn { call } => {
                let ExprKind::Call { func, args } = &call.kind else { unreachable!() };
                let f = self.expr(func, false)?;
                let mut vals = Vec::new();
                let mut names: Vec<Option<&str>> = Vec::new();
                for a in args {
                    vals.push(self.expr(&a.value, true)?);
                    names.push(a.name.as_deref());
                }
                let arr = self.values_array(&vals);
                let nm = self.names_array(&names);
                let tf = self.tb(&f);
                let sp = self.span_id(call.span);
                self.call_void("nx_spawn", &format!("{tf}, ptr {arr}, ptr {nm}, i64 {}, i32 {sp}", vals.len()));
                self.check();
                self.drop_all(&vals);
                self.drop_val(&f);
            }
            StmtKind::Shared { value, .. } => {
                let v = self.expr(value, false)?;
                let v = self.bind(v, value);
                let tv = self.tb(&v);
                let c = self.call_v("nx_shared_store", &tv);
                self.drop_val(&v);
                let c = self.owned(c);
                let target = self.res.assigns[&s.id].target;
                self.store_binding(target, &c);
            }
            StmtKind::Select { arms } => self.select_stmt(s, arms)?,
            StmtKind::Config { word } => return Err(not_supported(format!("\"{word}:\""), s.span)),
            StmtKind::Event { kind, name, body, .. } => {
                if *kind != "on" {
                    return Err(not_supported(format!("\"{kind}\""), s.span));
                }
                let frame = self.res.frame_of[&Node::Stmt(s.id)];
                let clo = self.closure(frame, FrameSrc::Handler(body))?;
                let n = self.strarg(name);
                let tb = self.tb(&clo);
                self.call_void("nx_on", &format!("{n}, {tb}"));
                self.drop_val(&clo);
            }
            StmtKind::Emit { name, args, call_span } => {
                let mut vals = Vec::new();
                for a in args {
                    let v = self.expr(&a.value, true)?;
                    vals.push(v);
                }
                let arr = self.values_array(&vals);
                let n = self.strarg(name);
                let sp = self.span_id(*call_span);
                self.call_void("nx_emit", &format!("{n}, ptr {arr}, i64 {}, i32 {sp}", vals.len()));
                self.check();
                for v in vals.iter().rev() {
                    self.drop_val(v);
                }
            }
            // an expect that always holds is not checked
            StmtKind::Expect(e) if crate::prove::expect_holds(e) => {}
            StmtKind::Expect(e) => {
                let v = self.expr(e, false)?;
                let c = self.truth(&v, e.span, "expect");
                let ok = self.label();
                let bad = self.label();
                self.cond_br(&c, &ok, &bad);
                self.place(&bad);
                self.contract_broken("expect", e)?;
                self.place(&ok);
            }
            StmtKind::Assert { expr, fails } => self.assert(expr, *fails)?,
            StmtKind::Fn(f) => {
                if let Some(a) = f.annotations.first() {
                    return Err(not_supported(format!("\"@{}\"", a.name), a.span));
                }
                let frame = self.res.frame_of[&Node::Fn(f.id)];
                let clo = self.closure(frame, FrameSrc::Fn(f))?;
                let b = self.res.decl[&Node::Stmt(s.id)];
                self.store_binding(b, &clo);
            }
            StmtKind::Type(t) => self.type_def(s, t)?,
            StmtKind::Trait { name, sigs } => {
                let id = self.res.type_ids[&s.id];
                let names: Vec<Option<&str>> = sigs.iter().map(|g| Some(g.name.as_str())).collect();
                let arr = self.names_array(&names);
                let n = self.strarg(name);
                let v = self.call_v("nx_define_trait", &format!("i32 {id}, {n}, i64 {}, ptr {arr}", sigs.len()));
                let b = self.res.decl[&Node::Stmt(s.id)];
                self.store_binding(b, &v);
            }
            StmtKind::If { branches, orelse } => {
                let end = self.label();
                for (cond, body) in branches {
                    let v = self.expr(cond, false)?;
                    let c = self.truth(&v, cond.span, "if");
                    let yes = self.label();
                    let no = self.label();
                    self.cond_br(&c, &yes, &no);
                    self.place(&yes);
                    self.block(body)?;
                    self.br(&end);
                    self.place(&no);
                }
                if let Some(b) = orelse {
                    self.block(b)?;
                }
                self.br(&end);
                self.place(&end);
            }
            StmtKind::For { iterable, body, .. } => {
                let scope = self.res.scope_of[&Node::Stmt(s.id)];
                let var = self.res.decl[&Node::Stmt(s.id)];
                let it = self.expr(iterable, false)?;
                self.for_each(&it, iterable.span, |g, item| {
                    g.enter_scope(scope);
                    g.store_binding(var, item);
                    g.block(body)
                })?;
            }
            StmtKind::While { cond, body } => {
                let scope = self.res.scope_of[&Node::Stmt(s.id)];
                let head = self.label();
                let yes = self.label();
                let end = self.label();
                self.br(&head);
                self.place(&head);
                let v = self.expr(cond, false)?;
                let c = self.truth(&v, cond.span, "while");
                self.cond_br(&c, &yes, &end);
                self.place(&yes);
                self.enter_scope(scope);
                self.block(body)?;
                self.br(&head);
                self.place(&end);
            }
            StmtKind::Repeat { count, body } => {
                let scope = self.res.scope_of[&Node::Stmt(s.id)];
                let v = self.expr(count, false)?;
                let tb = self.tb(&v);
                let sp = self.span_id(count.span);
                let n = self.reg();
                self.emit(&format!("{n} = call i64 @nx_repeat_count({tb}, i32 {sp})"));
                self.check();
                self.drop_val(&v);
                let i = self.alloca("i64");
                self.emit(&format!("store i64 0, ptr {i}"));
                let head = self.label();
                let yes = self.label();
                let end = self.label();
                self.br(&head);
                self.place(&head);
                let cur = self.reg();
                self.emit(&format!("{cur} = load i64, ptr {i}"));
                let c = self.reg();
                self.emit(&format!("{c} = icmp slt i64 {cur}, {n}"));
                self.cond_br(&c, &yes, &end);
                self.place(&yes);
                let nx = self.reg();
                self.emit(&format!("{nx} = add i64 {cur}, 1"));
                self.emit(&format!("store i64 {nx}, ptr {i}"));
                self.enter_scope(scope);
                self.block(body)?;
                self.br(&head);
                self.place(&end);
            }
            StmtKind::Match { subject, arms } => self.match_stmt(s, subject, arms)?,
            StmtKind::Try { within, body, handlers } => self.try_stmt(s, within.as_ref(), body, handlers)?,
            StmtKind::Parallel(body) => {
                // each statement is a task; they run at the same time
                let mut clos = Vec::new();
                for (i, st) in body.iter().enumerate() {
                    let frame = self.res.frame_of[&Node::Task(s.id, i)];
                    clos.push(self.closure(frame, FrameSrc::TaskStmt(st))?);
                }
                let arr = self.values_array(&clos);
                let sp = self.span_id(s.span);
                self.call_void("nx_parallel_block", &format!("ptr {arr}, i64 {}, i32 {sp}", clos.len()));
                self.check();
                self.drop_all(&clos);
            }
            StmtKind::Test { name, body } => {
                if self.opts.test_mode {
                    let frame = self.res.frame_of[&Node::Stmt(s.id)];
                    let clo = self.closure(frame, FrameSrc::Test(body))?;
                    let n = self.strarg(name);
                    let tb = self.tb(&clo);
                    self.call_void("nx_test_register", &format!("{n}, {tb}"));
                    self.drop_val(&clo);
                }
            }
        }
        Ok(())
    }

    /// Iterate over a value; `body` gets each element (owned: storing it
    /// moves it). The iterator holds its own reference to the value.
    fn for_each(
        &mut self,
        iterable: &Val,
        span: Span,
        mut body: impl FnMut(&mut Self, &Val) -> GResult<()>,
    ) -> GResult<()> {
        let tb = self.tb(iterable);
        let sp = self.span_id(span);
        let it = self.reg();
        self.emit(&format!("{it} = call ptr @nx_iter({tb}, i32 {sp})"));
        self.check();
        self.drop_val(iterable);
        self.f().live.push(Live::Iter(it.clone()));
        let item = self.alloca("%V");
        let head = self.label();
        let yes = self.label();
        let end = self.label();
        self.br(&head);
        self.place(&head);
        let ok = self.call_flag("nx_iter_next", &format!("ptr {it}, ptr {item}"));
        self.check();
        self.cond_br(&ok, &yes, &end);
        self.place(&yes);
        let v = self.load(&item);
        let v = self.owned(v);
        body(self, &v)?;
        self.drop_val(&v);
        self.br(&head);
        self.place(&end);
        self.unlive(&Live::Iter(it.clone()));
        self.emit(&format!("call void @nx_iter_free(ptr {it})"));
        Ok(())
    }

    /// Check a value against a declared type; the result is owned and the
    /// value is used up.
    fn conform(&mut self, v: Val, t: &TypeExpr, name: &str, span: Span) -> GResult<Val> {
        let tb = self.tb(&v);
        let d = self.desc_arg(t);
        let n = self.strarg(name);
        let sp = self.span_id(span);
        let r = self.call_v("nx_conform", &format!("{tb}, {d}, {n}, i32 {sp}"));
        self.check();
        self.drop_val(&v);
        Ok(self.owned(r))
    }

    fn name_error(&mut self, name: &str, suggestion: Option<&str>, span: Span) {
        let n = self.strarg(name);
        let s = match suggestion {
            Some(s) => self.strarg(s),
            None => "ptr null, i64 0".into(),
        };
        let sp = self.span_id(span);
        self.call_void("nx_name_error", &format!("{n}, {s}, i32 {sp}"));
        self.fail_here();
    }

    fn immutable_error(&mut self, name: &str, span: Span) {
        let n = self.strarg(name);
        let sp = self.span_id(span);
        self.call_void("nx_immutable", &format!("{n}, i32 {sp}"));
        self.fail_here();
    }

    fn assign(&mut self, s: &'a Stmt, target: &'a Expr, op: &str, value: &'a Expr, ty: Option<&TypeExpr>) -> GResult<()> {
        let aug = op != "=";
        match &target.kind {
            ExprKind::Name(name) => {
                let a = self.res.assigns[&s.id].clone();
                if let Some(t) = ty {
                    // `x: T = value` declares x in this scope
                    let v = self.expr(value, true)?;
                    let v = self.conform(v, t, name, value.span)?;
                    let v = self.bind(v, value);
                    let def = self.defined(a.target);
                    let bad = self.label();
                    let ok = self.label();
                    self.cond_br(&def, &bad, &ok);
                    self.place(&bad);
                    self.immutable_error(name, target.span);
                    self.place(&ok);
                    self.store_binding(a.target, &v);
                    return Ok(());
                }
                let typed_var = a.cands.iter().any(|(b, _)| {
                    let b = &self.res.bindings[*b];
                    b.kind == BKind::Var && b.ty.is_some()
                });
                let mut v = self.expr(value, typed_var)?;
                if aug {
                    let cur = self.name_read(target, name)?;
                    let r = self.binop(&op[..1], &cur, &v, s.span.to(value.span))?;
                    self.drop_val(&v);
                    self.drop_val(&cur);
                    v = r;
                }
                let mut v = self.bind(v, value);
                if a.cands.iter().any(|(b, _)| self.res.bindings[*b].shared) {
                    // a shared variable keeps a copy of its own
                    let tv = self.tb(&v);
                    let c = self.call_v("nx_shared_store", &tv);
                    self.drop_val(&v);
                    v = self.owned(c);
                }
                // the value waits in a slot; each path moves it out
                let slot = self.spill_owned(&v);
                let done = self.label();
                let define = self.label();
                for (b, crossed) in &a.cands {
                    let binding = &self.res.bindings[*b];
                    let kind = binding.kind;
                    let bty = binding.ty.clone();
                    let def = self.defined(*b);
                    let yes = self.label();
                    let next = self.label();
                    self.cond_br(&def, &yes, &next);
                    self.place(&yes);
                    if kind == BKind::Var {
                        self.task_write_check(*b, name, target.span);
                        let cur = self.load(&slot);
                        let v2 = match &bty {
                            Some(t) => {
                                let c = self.conform(cur, t, name, value.span)?;
                                // the value before the conversion stays in the slot
                                self.move_out(&slot, true);
                                c
                            }
                            None => {
                                self.move_out(&slot, false);
                                let c = self.plain(cur);
                                self.f().live.push(Live::Reg(match &c {
                                    Val::Reg(r) => r.clone(),
                                    Val::Lit(..) => unreachable!(),
                                }));
                                c
                            }
                        };
                        self.store_binding(*b, &v2);
                        self.br(&done);
                    } else if *crossed {
                        self.br(&define);
                    } else {
                        self.immutable_error(name, target.span);
                    }
                    self.place(&next);
                }
                self.br(&define);
                self.place(&define);
                let cur = self.load(&slot);
                self.move_out(&slot, false);
                let v = self.plain(cur);
                self.f().live.push(Live::Reg(match &v {
                    Val::Reg(r) => r.clone(),
                    Val::Lit(..) => unreachable!(),
                }));
                self.store_binding(a.target, &v);
                self.br(&done);
                self.place(&done);
                self.unlive(&Live::Slot(slot));
            }
            ExprKind::Attr { obj, name, name_span } => {
                let o = self.expr(obj, false)?;
                let mut v = self.expr(value, true)?;
                if aug {
                    let to = self.tb(&o);
                    let n = self.strarg(name);
                    let nsp = self.span_id(*name_span);
                    let cur = self.call_v("nx_attr", &format!("{to}, {n}, i32 {nsp}"));
                    self.check();
                    let cur = self.owned(cur);
                    let r = self.binop(&op[..1], &cur, &v, s.span.to(value.span))?;
                    self.drop_val(&cur);
                    self.drop_val(&v);
                    v = r;
                }
                let to = self.tb(&o);
                let tv = self.tb(&v);
                let n = self.strarg(name);
                let nsp = self.span_id(*name_span);
                let vsp = self.span_id(value.span);
                self.call_void("nx_set_attr", &format!("{to}, {n}, {tv}, i32 {nsp}, i32 {vsp}"));
                self.check();
                self.drop_val(&v);
                self.drop_val(&o);
            }
            ExprKind::Index { obj, index } => {
                // a change through a place (copy-on-write)
                let place = self.place_parts(obj)?;
                let i = self.expr(index, false)?;
                let v = self.expr(value, false)?;
                let v = self.into_shared(&place, v);
                let sp = self.span_id(target.span);
                let ptr = self.place_ptr(&place)?;
                let ti = self.tb(&i);
                let v = if aug {
                    let cur = self.call_v("nx_index_at", &format!("ptr {ptr}, {ti}, i32 {sp}"));
                    self.check();
                    let cur = self.owned(cur);
                    let r = self.binop(&op[..1], &cur, &v, s.span.to(value.span))?;
                    self.drop_val(&cur);
                    self.drop_val(&v);
                    r
                } else {
                    v
                };
                let ti = self.tb(&i);
                let tv = self.tb(&v);
                self.call_void("nx_set_index_at", &format!("ptr {ptr}, {ti}, {tv}, i32 {sp}"));
                self.check();
                self.drop_val(&v);
                self.drop_val(&i);
                self.drop_place(place);
            }
            _ => {}
        }
        Ok(())
    }

    /// The value of `slot` was moved out; `release` gives back the
    /// reference the slot held.
    fn move_out(&mut self, slot: &str, release: bool) {
        if release {
            let x = self.load(slot);
            self.rc_dec(&x);
        }
        self.emit(&format!("store %V {{ i64 0, i64 0 }}, ptr {slot}"));
    }

    /// The parts of a place that are evaluated before the change: the
    /// variable at its root, record values and indexes.
    fn place_parts(&mut self, e: &'a Expr) -> GResult<Place<'a>> {
        Ok(match &e.kind {
            ExprKind::Name(_) => match self.res.names.get(&e.id) {
                Some(NameRes::Bindings { cands, row: None, .. }) if !cands.is_empty() => {
                    Place::Var(e, cands.clone())
                }
                _ => Place::Value(self.expr(e, false)?),
            },
            ExprKind::Attr { obj, name, name_span } => {
                let o = self.expr(obj, false)?;
                let slot = self.spill(&o);
                Place::Field(slot, name.clone(), *name_span)
            }
            ExprKind::Index { obj, index } => {
                let inner = self.place_parts(obj)?;
                let i = self.expr(index, false)?;
                let slot = self.spill(&i);
                Place::Index(Box::new(inner), slot, e.span)
            }
            ExprKind::Propagate(x) => self.place_parts(x)?,
            _ => {
                let v = self.expr(e, false)?;
                Place::Value(v)
            }
        })
    }

    /// A pointer to the place, each step made unique.
    fn place_ptr(&mut self, p: &Place<'a>) -> GResult<String> {
        Ok(match p {
            Place::Var(e, cands) => {
                if let (ExprKind::Name(n), [b]) = (&e.kind, cands.as_slice()) {
                    self.task_write_check(*b, n, e.span);
                }
                let ptr = self.binding_ptr_any(e, cands)?;
                self.emit(&format!("call void @nx_unique_slot(ptr {ptr})"));
                ptr
            }
            Place::Field(slot, name, span) => {
                let o = self.load(slot);
                let to = self.tb(&o);
                let n = self.strarg(name);
                let sp = self.span_id(*span);
                let ptr = self.reg();
                self.emit(&format!("{ptr} = call ptr @nx_field_slot({to}, {n}, i32 {sp})"));
                self.check();
                ptr
            }
            Place::Index(inner, slot, span) => {
                let base = self.place_ptr(inner)?;
                let i = self.load(slot);
                let ti = self.tb(&i);
                let sp = self.span_id(*span);
                let ptr = self.reg();
                self.emit(&format!("{ptr} = call ptr @nx_index_slot(ptr {base}, {ti}, i32 {sp})"));
                self.check();
                ptr
            }
            Place::Value(v) => {
                // not a place: the change goes to a copy nobody sees
                let a = self.alloca("%V");
                self.store(&a, v);
                a
            }
        })
    }

    /// A value stored inside a shared variable is a copy of its own.
    fn into_shared(&mut self, p: &Place<'a>, v: Val) -> Val {
        let mut root = p;
        while let Place::Index(inner, ..) = root {
            root = inner;
        }
        let Place::Var(_, cands) = root else { return v };
        if !cands.iter().any(|b| self.res.bindings[*b].shared) {
            return v;
        }
        let tv = self.tb(&v);
        let c = self.call_v("nx_shared_store", &tv);
        self.drop_val(&v);
        self.owned(c)
    }

    fn drop_place(&mut self, p: Place<'a>) {
        match p {
            Place::Var(..) => {}
            Place::Field(slot, ..) => self.drop_slot(&slot),
            Place::Index(inner, slot, _) => {
                self.drop_slot(&slot);
                self.drop_place(*inner);
            }
            Place::Value(v) => self.drop_val(&v),
        }
    }

    /// The storage of the first candidate binding that is assigned (a name
    /// that is changed through a place).
    fn binding_ptr_any(&mut self, e: &'a Expr, cands: &[BindingId]) -> GResult<String> {
        if cands.len() == 1 && self.res.bindings[cands[0]].entry {
            return Ok(self.binding_ptr(cands[0]));
        }
        let out = self.alloca("ptr");
        let done = self.label();
        for b in cands {
            let p = self.binding_ptr(*b);
            self.emit(&format!("store ptr {p}, ptr {out}"));
            let v = self.load(&p);
            let t = self.tag_of(&v);
            let c = self.reg();
            self.emit(&format!("{c} = icmp ne i64 {t}, {T_UNDEF}"));
            let next = self.label();
            self.cond_br(&c, &done, &next);
            self.place(&next);
        }
        let ExprKind::Name(name) = &e.kind else { unreachable!() };
        let sugg = match self.res.names.get(&e.id) {
            Some(NameRes::Bindings { suggestion, .. }) => suggestion.clone(),
            _ => None,
        };
        self.name_error(name, sugg.as_deref(), e.span);
        self.place(&done);
        let r = self.reg();
        self.emit(&format!("{r} = load ptr, ptr {out}"));
        Ok(r)
    }

    fn type_def(&mut self, s: &'a Stmt, t: &'a TypeDef) -> GResult<()> {
        if let Some(a) = t.annotations.first() {
            return Err(not_supported(format!("\"@{}\"", a.name), a.span));
        }
        if let Some((name, value)) = t.settings.first() {
            return Err(not_supported(format!("\"{name} =\""), value.span));
        }
        let id = self.res.type_ids[&s.id];
        let b = self.res.decl[&Node::Stmt(s.id)];
        let name_arg = self.strarg(&t.name);
        if let Some(alias) = &t.alias {
            let d = self.desc_arg(alias);
            let v = self.call_v("nx_define_alias", &format!("i32 {id}, {name_arg}, {d}"));
            let v = self.owned(v);
            self.store_binding(b, &v);
            return Ok(());
        }
        // the trait it claims to be (checked now: a compile error)
        let mut trait_value = None;
        if let Some(tr) = &t.trait_name {
            let res = self.res.traits_of[&s.id].clone();
            let tb = match &res {
                NameRes::Bindings { cands, .. } if !cands.is_empty() => Some(cands[0]),
                _ => None,
            };
            let trait_id = tb.and_then(|b| match self.res.bindings[b].origin {
                Origin::Trait(id) => Some(id),
                _ => None,
            });
            let Some(trait_id) = trait_id else {
                return Err(Diagnostic::new("not_a_trait", t.name_span).arg("name", tr.clone()));
            };
            for m in &self.res.trait_methods[&trait_id] {
                if !t.methods.iter().any(|x| &x.name == m) {
                    return Err(Diagnostic::new("trait_missing", t.name_span)
                        .arg("type", t.name.clone())
                        .arg("trait", tr.clone())
                        .arg("method", m.clone()));
                }
            }
            trait_value = tb;
        }
        let v = if !t.variants.is_empty() {
            let mut items = Vec::new();
            for (i, var) in t.variants.iter().enumerate() {
                let fields = self.field_decls(var.params.iter().map(|p| (p.name.as_str(), p.ty.as_ref())).collect());
                let c = self.cstr(&var.name);
                items.push(format!(
                    "{{ i64, ptr, i64, i64, ptr }} {{ i64 {}, ptr {c}, i64 {}, i64 {}, ptr {fields} }}",
                    id + 1 + i as u32,
                    var.name.len(),
                    var.params.len()
                ));
            }
            let arr = format!("@vd.{}", self.next());
            let _ = writeln!(
                self.consts,
                "{arr} = private constant [{} x {{ i64, ptr, i64, i64, ptr }}] [{}]",
                items.len(),
                items.join(", ")
            );
            let v = self.call_v("nx_define_enum", &format!("i32 {id}, {name_arg}, i64 {}, ptr {arr}", t.variants.len()));
            let vbs = self.res.variant_bindings[&s.id].clone();
            for (i, vb) in vbs.iter().enumerate() {
                let tb = self.tb(&v);
                // variants are constants of the program (never freed)
                let var = self.call_v("nx_enum_variant", &format!("{tb}, i64 {i}"));
                self.store_binding(*vb, &var);
            }
            v
        } else {
            let fields = self.field_decls(t.fields.iter().map(|f| (f.name.as_str(), Some(&f.ty))).collect());
            let mut defaults = Vec::new();
            let mut any_default = false;
            for (i, f) in t.fields.iter().enumerate() {
                match &f.default {
                    Some(d) => {
                        any_default = true;
                        let frame = self.res.frame_of[&Node::Field(s.id, i)];
                        defaults.push(self.closure(frame, FrameSrc::Default(d))?);
                    }
                    None => defaults.push(lit(T_UNDEF, "0")),
                }
            }
            let darr = if any_default { self.values_array(&defaults) } else { "null".into() };
            let v = self.call_v(
                "nx_define_record",
                &format!("i32 {id}, {name_arg}, i64 {}, ptr {fields}, ptr {darr}", t.fields.len()),
            );
            for d in defaults.iter().rev() {
                self.drop_val(d);
            }
            v
        };
        // types are constants of the program (never freed)
        let tmp = self.temp(&v);
        for m in &t.methods {
            if let Some(a) = m.annotations.first() {
                return Err(not_supported(format!("\"@{}\"", a.name), a.span));
            }
            let frame = self.res.frame_of[&Node::Fn(m.id)];
            let clo = self.closure(frame, FrameSrc::Fn(m))?;
            let tv = self.load(&tmp);
            let tt = self.tb(&tv);
            let tc = self.tb(&clo);
            let n = self.strarg(&m.name);
            self.call_void("nx_add_method", &format!("{tt}, {n}, {tc}"));
            self.drop_val(&clo);
        }
        if let Some(tb) = trait_value {
            let trv = self.load_binding(tb);
            let tv = self.load(&tmp);
            let a = self.tb(&tv);
            let c = self.tb(&trv);
            self.call_void("nx_add_trait", &format!("{a}, {c}"));
        }
        let v = self.load(&tmp);
        self.store_binding(b, &v);
        Ok(())
    }

    fn field_decls(&mut self, fields: Vec<(&str, Option<&TypeExpr>)>) -> String {
        if fields.is_empty() {
            return "null".into();
        }
        let mut items = Vec::new();
        for (name, ty) in &fields {
            let c = self.cstr(name);
            let (d, dl, opt) = match ty {
                Some(t) => {
                    let text = self.desc_text(t);
                    let dc = self.cstr(&text);
                    // bit 1: optional; bit 2: a weak field
                    let weak = matches!(t, TypeExpr::Name(tn) if tn.weak);
                    (dc, text.len(), t.optional() as u8 | (weak as u8) << 1)
                }
                None => ("null".to_string(), 0, 0),
            };
            items.push(format!(
                "{{ ptr, i64, ptr, i64, i64 }} {{ ptr {c}, i64 {}, ptr {d}, i64 {dl}, i64 {opt} }}",
                name.len()
            ));
        }
        let name = format!("@fd.{}", self.next());
        let _ = writeln!(
            self.consts,
            "{name} = private constant [{} x {{ ptr, i64, ptr, i64, i64 }}] [{}]",
            items.len(),
            items.join(", ")
        );
        name
    }

    fn contract_broken(&mut self, kw: &str, e: &'a Expr) -> GResult<()> {
        let mut texts: Vec<String> = Vec::new();
        let mut slots = Vec::new();
        for op in contract_operands(e) {
            let text = source_text(self.src, op.span);
            if text.is_empty() || texts.contains(&text) {
                continue;
            }
            // operands that cannot be evaluated are left out
            let slot = self.alloca("%V");
            let skip = self.label();
            let done = self.label();
            let keep = self.f().live.len();
            self.f().unwind.push((skip.clone(), keep));
            let v = self.expr(op, false)?;
            self.f().unwind.pop();
            self.own(&v);
            self.store(&slot, &v);
            self.br(&done);
            self.place(&skip);
            self.call_void("nx_clear", "");
            self.emit(&format!("store %V {{ i64 {T_UNDEF}, i64 0 }}, ptr {slot}"));
            self.br(&done);
            self.place(&done);
            self.f().live.push(Live::Slot(slot.clone()));
            texts.push(text);
            slots.push(slot);
        }
        let vals: Vec<Val> = slots.iter().map(|s| self.load(s)).collect();
        let names: Vec<Option<&str>> = texts.iter().map(|t| Some(t.as_str())).collect();
        let narr = self.names_array(&names);
        let varr = self.values_array(&vals);
        let k = self.strarg(kw);
        let x = self.strarg(&source_text(self.src, e.span));
        let sp = self.span_id(e.span);
        self.call_void(
            "nx_contract_broken",
            &format!("{k}, {x}, ptr {narr}, ptr {varr}, i64 {}, i32 {sp}", vals.len()),
        );
        // the error is on its way: the values are released on the way out
        self.fail_here();
        for s in slots.iter().rev() {
            self.unlive(&Live::Slot(s.clone()));
        }
        Ok(())
    }

    fn assert(&mut self, e: &'a Expr, fails: bool) -> GResult<()> {
        let text = source_text(self.src, e.span);
        let sp = self.span_id(e.span);
        if fails {
            let caught = self.label();
            let keep = self.f().live.len();
            self.f().unwind.push((caught.clone(), keep));
            let v = self.expr(e, false)?;
            self.f().unwind.pop();
            self.drop_val(&v);
            let x = self.strarg(&text);
            self.call_void("nx_assert_not_failed", &format!("{x}, i32 {sp}"));
            self.fail_here();
            self.place(&caught);
            let f = self.failing_flag();
            let is_error = self.reg();
            self.emit(&format!("{is_error} = icmp eq i8 {f}, 1"));
            let ok = self.label();
            let unwind = self.unwind_label();
            self.cond_br(&is_error, &ok, &unwind);
            self.place(&ok);
            self.call_void("nx_clear", "");
            return Ok(());
        }
        if let ExprKind::Compare { ops, operands } = &e.kind {
            if ops.len() == 1 {
                let l = self.expr(&operands[0], true)?;
                let r = self.expr(&operands[1], true)?;
                let c = self.compare_op(ops[0], &l, &r, e.span)?;
                let ok = self.label();
                let bad = self.label();
                self.cond_br(&c, &ok, &bad);
                self.place(&bad);
                let x = self.strarg(&text);
                let o = self.strarg(ops[0]);
                let tl = self.tb(&l);
                let tr = self.tb(&r);
                self.call_void("nx_assert_cmp_failed", &format!("{x}, {o}, {tl}, {tr}, i32 {sp}"));
                self.fail_here();
                self.place(&ok);
                self.drop_val(&r);
                self.drop_val(&l);
                return Ok(());
            }
        }
        let v = self.expr(e, false)?;
        let c = self.truth(&v, e.span, "assert");
        let ok = self.label();
        let bad = self.label();
        self.cond_br(&c, &ok, &bad);
        self.place(&bad);
        let x = self.strarg(&text);
        self.call_void("nx_assert_failed", &format!("{x}, i32 {sp}"));
        self.fail_here();
        self.place(&ok);
        Ok(())
    }

    fn try_stmt(&mut self, s: &'a Stmt, within: Option<&'a Expr>, body: &'a [Stmt], handlers: &'a [Handler]) -> GResult<()> {
        if let Some(w) = within {
            let v = self.expr(w, false)?;
            let tb = self.tb(&v);
            let wsp = self.span_id(w.span);
            let tsp = self.span_id(s.span);
            self.call_void("nx_deadline_push", &format!("{tb}, i32 {wsp}, i32 {tsp}"));
            self.check();
            self.drop_val(&v);
        }
        let catch = self.label();
        let end = self.label();
        let keep = self.f().live.len();
        self.f().unwind.push((catch.clone(), keep));
        self.block(body)?;
        self.f().unwind.pop();
        if within.is_some() {
            self.call_void("nx_deadline_pop", "");
        }
        self.br(&end);
        self.place(&catch);
        if within.is_some() {
            self.call_void("nx_deadline_pop", "");
        }
        let f = self.failing_flag();
        let is_error = self.reg();
        self.emit(&format!("{is_error} = icmp eq i8 {f}, 1"));
        let handle = self.label();
        let outer = self.unwind_label();
        self.cond_br(&is_error, &handle, &outer);
        self.place(&handle);
        let err = self.call_v("nx_take_error", "");
        let err = self.owned(err);
        let err_slot = self.spill(&err);
        for (i, h) in handlers.iter().enumerate() {
            let next = self.label();
            if let Some(k) = &h.kind {
                let e = self.load(&err_slot);
                let tb = self.tb(&e);
                let ka = self.strarg(k);
                let c = self.call_flag("nx_error_is", &format!("{tb}, {ka}"));
                let yes = self.label();
                self.cond_br(&c, &yes, &next);
                self.place(&yes);
            }
            let scope = self.res.scope_of[&Node::Handler(s.id, i)];
            self.enter_scope(scope);
            let b = self.res.decl[&Node::Handler(s.id, i)];
            let e = self.load(&err_slot);
            self.store_binding(b, &e);
            self.block(&h.body)?;
            // handled: the error value is no longer needed here
            let e = self.load(&err_slot);
            self.rc_dec(&e);
            self.br(&end);
            self.place(&next);
        }
        let e = self.load(&err_slot);
        let tb = self.tb(&e);
        self.call_void("nx_reraise", &tb);
        self.fail_here();
        self.unlive(&Live::Slot(err_slot));
        self.place(&end);
        Ok(())
    }

    fn match_stmt(&mut self, s: &'a Stmt, subject: &'a Expr, arms: &'a [MatchArm]) -> GResult<()> {
        let v = self.expr(subject, false)?;
        let subj = self.spill(&v);
        let end = self.label();
        for (ai, arm) in arms.iter().enumerate() {
            let scope = self.res.scope_of[&Node::Arm(s.id, ai)];
            for (pi, p) in arm.patterns.iter().enumerate() {
                let next = self.label();
                self.enter_scope(scope);
                let value = self.load(&subj);
                self.pattern(s.id, ai, vec![pi], p, &value, &next)?;
                if let Some(g) = &arm.guard {
                    let gv = self.expr(g, false)?;
                    let c = self.truth(&gv, g.span, "if");
                    let yes = self.label();
                    self.cond_br(&c, &yes, &next);
                    self.place(&yes);
                }
                self.block(&arm.body)?;
                self.br(&end);
                self.place(&next);
            }
        }
        let value = self.load(&subj);
        let tb = self.tb(&value);
        let sp = self.span_id(subject.span);
        self.call_void("nx_no_match", &format!("{tb}, i32 {sp}"));
        self.fail_here();
        self.place(&end);
        self.drop_slot(&subj);
        self.call_void("nx_scratch_clear", "");
        Ok(())
    }

    /// Test a pattern; jumps to `fail` when it does not match, binding its
    /// names on the way.
    fn pattern(&mut self, stmt: u32, arm: usize, path: Vec<usize>, p: &'a Pattern, value: &Val, fail: &str) -> GResult<()> {
        match p {
            Pattern::Wild(_) => {}
            Pattern::Lit(span, l) => {
                let lv = self.expr(l, false)?;
                let c = self.eq(&lv, value, *span);
                self.drop_val(&lv);
                let yes = self.label();
                self.cond_br(&c, &yes, fail);
                self.place(&yes);
            }
            Pattern::Name(span, _) => {
                let key = (stmt, arm, path.clone());
                match self.res.pattern_tests.get(&key).cloned().flatten() {
                    Some(res) => {
                        let pname = match p {
                            Pattern::Name(_, n) => n.as_str(),
                            _ => "",
                        };
                        let known = self.resolved(&res, pname, *span)?;
                        let NameRes::Bindings { cands, .. } = &res else { unreachable!() };
                        let tv = self.tb(value);
                        let tk = self.tb(&known);
                        let c = match self.res.bindings[cands[0]].origin {
                            Origin::Variant(..) => self.call_flag("nx_match_variant", &format!("{tv}, {tk}")),
                            _ => {
                                let sp = self.span_id(*span);
                                let c = self.call_flag("nx_is", &format!("{tv}, {tk}, i32 {sp}"));
                                self.check();
                                c
                            }
                        };
                        self.drop_val(&known);
                        let yes = self.label();
                        self.cond_br(&c, &yes, fail);
                        self.place(&yes);
                    }
                    None => {
                        let b = self.res.pattern_binds[&key];
                        self.store_binding(b, value);
                    }
                }
            }
            Pattern::Ctor(span, name, args) => {
                let key = (stmt, arm, path.clone());
                let res = self.res.pattern_tests[&key].clone().unwrap();
                let ctor = self.resolved(&res, name, *span)?;
                let tv = self.tb(value);
                let tc = self.tb(&ctor);
                let n = self.strarg(name);
                let sp = self.span_id(*span);
                let c = self.call_flag("nx_match_ctor", &format!("{tv}, {tc}, i64 {}, {n}, i32 {sp}", args.len()));
                self.check();
                self.drop_val(&ctor);
                let yes = self.label();
                self.cond_br(&c, &yes, fail);
                self.place(&yes);
                let vslot = self.temp(value);
                for (i, a) in args.iter().enumerate() {
                    let v = self.load(&vslot);
                    let tv = self.tb(&v);
                    // borrowed from the value being matched
                    let field = self.call_v("nx_field_at", &format!("{tv}, i64 {i}"));
                    let mut sub = path.clone();
                    sub.push(i);
                    self.pattern(stmt, arm, sub, a, &field, fail)?;
                }
            }
        }
        Ok(())
    }
}


impl<'a> Gen<'a> {
    // ------------------------------------------------------- expressions

    /// Generate an expression. `lit_ok`: a float literal may stay a
    /// literal (it is an operand or an argument, where section 17 rule 1
    /// can read it as a decimal). The result is owned when it is in
    /// `live`, borrowed otherwise.
    pub(crate) fn expr(&mut self, e: &'a Expr, lit_ok: bool) -> GResult<Val> {
        Ok(match &e.kind {
            ExprKind::Num { text, is_float: false } => match text.parse::<i64>() {
                Ok(n) => lit(T_INT, n.to_string()),
                Err(_) => {
                    let sp = self.span_id(e.span);
                    self.call_void("nx_fail_overflow", &format!("i32 {sp}"));
                    self.fail_here();
                    none()
                }
            },
            ExprKind::Num { text, is_float: true } => {
                if lit_ok {
                    self.lit_float(text)
                } else {
                    lit(T_FLOAT, float_bits(text.parse().unwrap_or(f64::NAN)))
                }
            }
            ExprKind::Unit { text, unit, .. } => self.unit_value(text, unit),
            ExprKind::Str(parts) => {
                if parts.iter().all(|p| matches!(p, StrPiece::Lit(_))) {
                    let s: String = parts
                        .iter()
                        .map(|p| match p {
                            StrPiece::Lit(s) => s.as_str(),
                            _ => "",
                        })
                        .collect();
                    self.str_value(&s)
                } else {
                    let mut vals = Vec::new();
                    for p in parts {
                        match p {
                            StrPiece::Lit(s) => vals.push(self.str_value(s)),
                            StrPiece::Expr(x) => vals.push(self.expr(x, false)?),
                        }
                    }
                    let arr = self.values_array(&vals);
                    let r = self.call_v("nx_interp", &format!("ptr {arr}, i64 {}", vals.len()));
                    self.check();
                    self.drop_all(&vals);
                    self.owned(r)
                }
            }
            ExprKind::Bool(b) => lit(T_BOOL, if *b { "1" } else { "0" }),
            ExprKind::None => none(),
            ExprKind::Name(n) => self.name_read(e, n)?,
            ExprKind::List(items) => {
                let mut vals = Vec::new();
                for i in items {
                    vals.push(self.expr(i, false)?);
                }
                let arr = self.values_array(&vals);
                let r = self.call_v("nx_list", &format!("ptr {arr}, i64 {}", vals.len()));
                self.check();
                self.drop_all(&vals);
                self.owned(r)
            }
            ExprKind::Map(pairs) => {
                let mut keys = Vec::new();
                let mut vals = Vec::new();
                let mut spans = Vec::new();
                for (k, v) in pairs {
                    keys.push(self.expr(k, false)?);
                    vals.push(self.expr(v, false)?);
                    spans.push(self.span_id(k.span));
                }
                let ka = self.values_array(&keys);
                let va = self.values_array(&vals);
                let sa = if spans.is_empty() {
                    "null".to_string()
                } else {
                    let name = format!("@ms.{}", self.next());
                    let items: Vec<String> = spans.iter().map(|s| format!("i32 {s}")).collect();
                    let _ = writeln!(self.consts, "{name} = private constant [{} x i32] [{}]", spans.len(), items.join(", "));
                    name
                };
                let r = self.call_v("nx_map", &format!("ptr {ka}, ptr {va}, ptr {sa}, i64 {}", pairs.len()));
                self.check();
                self.drop_all(&vals);
                self.drop_all(&keys);
                self.owned(r)
            }
            ExprKind::Comprehension { element, iterable, condition, .. } => {
                let out = self.call_v("nx_list", "ptr null, i64 0");
                let out = self.owned(out);
                let slot = self.spill(&out);
                let scope = self.res.scope_of[&Node::Expr(e.id)];
                let var = self.res.decl[&Node::Expr(e.id)];
                let it = self.expr(iterable, false)?;
                self.for_each(&it, iterable.span, |g, item| {
                    g.enter_scope(scope);
                    g.store_binding(var, item);
                    let skip = g.label();
                    if let Some(c) = condition {
                        let cv = g.expr(c, false)?;
                        let cc = g.truth(&cv, c.span, "if");
                        let yes = g.label();
                        g.cond_br(&cc, &yes, &skip);
                        g.place(&yes);
                    }
                    let v = g.expr(element, false)?;
                    let l = g.load(&slot);
                    let tl = g.tb(&l);
                    let tv = g.tb(&v);
                    g.call_void("nx_list_push", &format!("{tl}, {tv}"));
                    g.check();
                    g.drop_val(&v);
                    g.br(&skip);
                    g.place(&skip);
                    Ok(())
                })?;
                self.unspill(&slot)
            }
            ExprKind::Attr { obj, name, name_span } => {
                let o = self.expr(obj, false)?;
                let to = self.tb(&o);
                let n = self.strarg(name);
                let sp = self.span_id(*name_span);
                let r = self.call_v("nx_attr", &format!("{to}, {n}, i32 {sp}"));
                self.check();
                self.drop_val(&o);
                self.owned(r)
            }
            ExprKind::Index { obj, index } => {
                let o = self.expr(obj, false)?;
                let i = self.expr(index, false)?;
                let to = self.tb(&o);
                let ti = self.tb(&i);
                let sp = self.span_id(e.span);
                let r = self.call_v("nx_index", &format!("{to}, {ti}, i32 {sp}"));
                self.check();
                self.drop_val(&i);
                self.drop_val(&o);
                self.owned(r)
            }
            ExprKind::Call { func, args } => self.call_expr(e, func, args, None, e.span)?,
            ExprKind::Propagate(x) => self.expr(x, lit_ok)?,
            ExprKind::Unary { op: "-", operand } => {
                if let ExprKind::Num { text, is_float: true } = &operand.kind {
                    let neg = format!("-{text}");
                    return Ok(if lit_ok { self.lit_float(&neg) } else { lit(T_FLOAT, float_bits(-text.parse::<f64>().unwrap_or(f64::NAN))) });
                }
                if let ExprKind::Num { text, is_float: false } = &operand.kind {
                    if let Ok(n) = text.parse::<i64>() {
                        return Ok(lit(T_INT, (-n).to_string()));
                    }
                }
                let v = self.expr(operand, true)?;
                let tv = self.tb(&v);
                let sp = self.span_id(e.span);
                let r = self.call_v("nx_neg", &format!("{tv}, i32 {sp}"));
                self.check();
                self.drop_val(&v);
                self.owned(r)
            }
            ExprKind::Unary { operand, .. } => {
                let v = self.expr(operand, false)?;
                let c = self.truth(&v, operand.span, "not");
                let n = self.reg();
                self.emit(&format!("{n} = xor i1 {c}, true"));
                self.bool_val(&n)
            }
            ExprKind::BinOp { op, left, right } => {
                let l = self.expr(left, true)?;
                let r = self.expr(right, true)?;
                let v = self.binop(op, &l, &r, e.span)?;
                self.drop_val(&r);
                self.drop_val(&l);
                v
            }
            ExprKind::BoolOp { op, left, right } => {
                let slot = self.alloca("%V");
                let l = self.expr(left, false)?;
                let cl = self.truth(&l, left.span, op);
                let short = self.label();
                let rest = self.label();
                let end = self.label();
                if *op == "and" {
                    self.cond_br(&cl, &rest, &short);
                } else {
                    self.cond_br(&cl, &short, &rest);
                }
                self.place(&short);
                let sv = lit(T_BOOL, if *op == "and" { "0" } else { "1" });
                self.store(&slot, &sv);
                self.br(&end);
                self.place(&rest);
                let r = self.expr(right, false)?;
                let cr = self.truth(&r, right.span, op);
                let rv = self.bool_val(&cr);
                self.store(&slot, &rv);
                self.br(&end);
                self.place(&end);
                self.load(&slot)
            }
            ExprKind::Compare { ops, operands } => {
                let slot = self.alloca("%V");
                let end = self.label();
                let fail = self.label();
                let mut left = self.expr(&operands[0], true)?;
                for (op, operand) in ops.iter().zip(&operands[1..]) {
                    let right = self.expr(operand, true)?;
                    let c = self.compare_op(op, &left, &right, e.span)?;
                    self.drop_val(&left);
                    let next = self.label();
                    if self.is_live(&right) {
                        // the right operand is released on the way out
                        let no = self.label();
                        self.cond_br(&c, &next, &no);
                        self.place(&no);
                        self.rc_dec(&right);
                        self.br(&fail);
                    } else {
                        self.cond_br(&c, &next, &fail);
                    }
                    self.place(&next);
                    left = right;
                }
                self.drop_val(&left);
                self.store(&slot, &lit(T_BOOL, "1"));
                self.br(&end);
                self.place(&fail);
                self.store(&slot, &lit(T_BOOL, "0"));
                self.br(&end);
                self.place(&end);
                self.load(&slot)
            }
            ExprKind::Coalesce { left, right } => {
                let l = self.expr(left, lit_ok)?;
                let slot = self.spill_owned(&l);
                let isn = self.is_tag(&l, T_NONE);
                let yes = self.label();
                let end = self.label();
                self.cond_br(&isn, &yes, &end);
                self.place(&yes);
                // the left value was none: nothing to release
                let r = self.expr(right, lit_ok)?;
                self.own(&r);
                self.store(&slot, &r);
                self.br(&end);
                self.place(&end);
                self.unspill(&slot)
            }
            ExprKind::Range { start, end } => {
                let a = self.expr(start, false)?;
                let b = self.expr(end, false)?;
                let ta = self.tb(&a);
                let tb = self.tb(&b);
                let sp = self.span_id(e.span);
                let r = self.call_v("nx_range", &format!("{ta}, {tb}, i32 {sp}"));
                self.check();
                self.drop_val(&b);
                self.drop_val(&a);
                self.owned(r)
            }
            ExprKind::Lambda { body, .. } => {
                let frame = self.res.frame_of[&Node::Expr(e.id)];
                self.closure(frame, FrameSrc::Lambda(body))?
            }
            ExprKind::Pipeline { source, steps } => self.pipeline(e, source, steps)?,
            ExprKind::ParallelFor { iterable, args, body, .. } => self.parallel_for(e, iterable, args, body)?,
        })
    }

    fn drop_all(&mut self, vals: &[Val]) {
        for v in vals.iter().rev() {
            self.drop_val(v);
        }
    }

    fn binop(&mut self, op: &str, l: &Val, r: &Val, span: Span) -> GResult<Val> {
        // ints are computed inline; anything else (or an overflow) goes to
        // the runtime, which applies the rules of section 17
        let fast = matches!(op, "+" | "-" | "*" | "//" | "%");
        if !fast || matches!(l, Val::Lit(t, _) if *t != T_INT) || matches!(r, Val::Lit(t, _) if *t != T_INT) {
            let v = self.binop_slow(op, l, r, span)?;
            return Ok(self.owned(v));
        }
        let slot = self.alloca("%V");
        let slow = self.label();
        let done = self.label();
        let both = self.both_tag(l, r, T_INT);
        let fast_l = self.label();
        self.cond_br(&both, &fast_l, &slow);
        self.place(&fast_l);
        let a = self.bits_of(l);
        let b = self.bits_of(r);
        let res = match op {
            "+" | "-" | "*" => {
                let f = match op {
                    "+" => "sadd",
                    "-" => "ssub",
                    _ => "smul",
                };
                let pair = self.reg();
                self.emit(&format!("{pair} = call {{ i64, i1 }} @llvm.{f}.with.overflow.i64(i64 {a}, i64 {b})"));
                let ovf = self.reg();
                self.emit(&format!("{ovf} = extractvalue {{ i64, i1 }} {pair}, 1"));
                let ok = self.label();
                self.cond_br(&ovf, &slow, &ok);
                self.place(&ok);
                let v = self.reg();
                self.emit(&format!("{v} = extractvalue {{ i64, i1 }} {pair}, 0"));
                v
            }
            _ => {
                // b == 0 and MIN // -1 are left to the runtime
                let zero = self.reg();
                self.emit(&format!("{zero} = icmp eq i64 {b}, 0"));
                let m1 = self.reg();
                self.emit(&format!("{m1} = icmp eq i64 {b}, -1"));
                let min = self.reg();
                self.emit(&format!("{min} = icmp eq i64 {a}, -9223372036854775808"));
                let both_bad = self.reg();
                self.emit(&format!("{both_bad} = and i1 {m1}, {min}"));
                let bad = self.reg();
                self.emit(&format!("{bad} = or i1 {zero}, {both_bad}"));
                let ok = self.label();
                self.cond_br(&bad, &slow, &ok);
                self.place(&ok);
                let q = self.reg();
                self.emit(&format!("{q} = sdiv i64 {a}, {b}"));
                let rem = self.reg();
                self.emit(&format!("{rem} = srem i64 {a}, {b}"));
                // floor division: adjust when the remainder and the divisor
                // have different signs
                let nz = self.reg();
                self.emit(&format!("{nz} = icmp ne i64 {rem}, 0"));
                let x = self.reg();
                self.emit(&format!("{x} = xor i64 {rem}, {b}"));
                let neg = self.reg();
                self.emit(&format!("{neg} = icmp slt i64 {x}, 0"));
                let adj = self.reg();
                self.emit(&format!("{adj} = and i1 {nz}, {neg}"));
                if op == "//" {
                    let q1 = self.reg();
                    self.emit(&format!("{q1} = sub i64 {q}, 1"));
                    let v = self.reg();
                    self.emit(&format!("{v} = select i1 {adj}, i64 {q1}, i64 {q}"));
                    v
                } else {
                    let r1 = self.reg();
                    self.emit(&format!("{r1} = add i64 {rem}, {b}"));
                    let v = self.reg();
                    self.emit(&format!("{v} = select i1 {adj}, i64 {r1}, i64 {rem}"));
                    v
                }
            }
        };
        let v = self.reg();
        self.emit(&format!("{v} = insertvalue %V {{ i64 {T_INT}, i64 0 }}, i64 {res}, 1"));
        self.emit(&format!("store %V {v}, ptr {slot}"));
        self.br(&done);
        self.place(&slow);
        let sv = self.binop_slow(op, l, r, span)?;
        self.store(&slot, &sv);
        self.br(&done);
        self.place(&done);
        // an int, or a value the runtime made: owned either way
        let v = self.load(&slot);
        Ok(self.owned(v))
    }

    /// Both values have this tag (an i1).
    fn both_tag(&mut self, l: &Val, r: &Val, tag: u64) -> String {
        let cl = match l {
            Val::Lit(t, _) => (if *t == tag { "true" } else { "false" }).to_string(),
            _ => self.is_tag(l, tag),
        };
        let cr = match r {
            Val::Lit(t, _) => (if *t == tag { "true" } else { "false" }).to_string(),
            _ => self.is_tag(r, tag),
        };
        let c = self.reg();
        self.emit(&format!("{c} = and i1 {cl}, {cr}"));
        c
    }

    fn binop_slow(&mut self, op: &str, l: &Val, r: &Val, span: Span) -> GResult<Val> {
        let tl = self.tb(l);
        let tr = self.tb(r);
        let sp = self.span_id(span);
        let v = self.call_v("nx_binop", &format!("i32 {}, {tl}, {tr}, i32 {sp}", op_code(op)));
        self.check();
        Ok(v)
    }

    fn eq(&mut self, l: &Val, r: &Val, span: Span) -> String {
        // two ints (or two bools) are equal when their bits are
        let slot = self.alloca("i1");
        let slow = self.label();
        let fast = self.label();
        let done = self.label();
        let ti = self.both_tag(l, r, T_INT);
        let tb_ = self.both_tag(l, r, T_BOOL);
        let simple = self.reg();
        self.emit(&format!("{simple} = or i1 {ti}, {tb_}"));
        self.cond_br(&simple, &fast, &slow);
        self.place(&fast);
        let a = self.bits_of(l);
        let b = self.bits_of(r);
        let c = self.reg();
        self.emit(&format!("{c} = icmp eq i64 {a}, {b}"));
        self.emit(&format!("store i1 {c}, ptr {slot}"));
        self.br(&done);
        self.place(&slow);
        let tl = self.tb(l);
        let tr = self.tb(r);
        let sp = self.span_id(span);
        let c2 = self.call_flag("nx_eq", &format!("{tl}, {tr}, i32 {sp}"));
        self.check();
        self.emit(&format!("store i1 {c2}, ptr {slot}"));
        self.br(&done);
        self.place(&done);
        let r = self.reg();
        self.emit(&format!("{r} = load i1, ptr {slot}"));
        r
    }

    /// One comparison of a chain (an i1).
    fn compare_op(&mut self, op: &str, l: &Val, r: &Val, span: Span) -> GResult<String> {
        Ok(match op {
            "==" => self.eq(l, r, span),
            "!=" => {
                let c = self.eq(l, r, span);
                let n = self.reg();
                self.emit(&format!("{n} = xor i1 {c}, true"));
                n
            }
            "in" | "is" => {
                let tl = self.tb(l);
                let tr = self.tb(r);
                let sp = self.span_id(span);
                let f = if op == "in" { "nx_in" } else { "nx_is" };
                let c = self.call_flag(f, &format!("{tl}, {tr}, i32 {sp}"));
                self.check();
                c
            }
            _ => {
                let (code, pred) = match op {
                    "<" => (0, "slt"),
                    ">" => (1, "sgt"),
                    "<=" => (2, "sle"),
                    _ => (3, "sge"),
                };
                let slot = self.alloca("i1");
                let slow = self.label();
                let fast = self.label();
                let done = self.label();
                let ti = self.both_tag(l, r, T_INT);
                self.cond_br(&ti, &fast, &slow);
                self.place(&fast);
                let a = self.bits_of(l);
                let b = self.bits_of(r);
                let c = self.reg();
                self.emit(&format!("{c} = icmp {pred} i64 {a}, {b}"));
                self.emit(&format!("store i1 {c}, ptr {slot}"));
                self.br(&done);
                self.place(&slow);
                let tl = self.tb(l);
                let tr = self.tb(r);
                let sp = self.span_id(span);
                let c2 = self.call_flag("nx_order", &format!("i32 {code}, {tl}, {tr}, i32 {sp}"));
                self.check();
                self.emit(&format!("store i1 {c2}, ptr {slot}"));
                self.br(&done);
                self.place(&done);
                let r = self.reg();
                self.emit(&format!("{r} = load i1, ptr {slot}"));
                r
            }
        })
    }

    fn name_read(&mut self, e: &'a Expr, name: &str) -> GResult<Val> {
        let res = self.res.names.get(&e.id).cloned().unwrap_or_else(|| panic!("unresolved name {name}"));
        let v = self.resolved(&res, name, e.span)?;
        let NameRes::Bindings { cands, row: None, .. } = &res else { return Ok(v) };
        if cands.iter().any(|b| self.res.bindings[*b].shared) {
            // a shared variable is read as a copy (nexo.sync)
            let tv = self.tb(&v);
            let c = self.call_v("nx_shared_read", &tv);
            self.drop_val(&v);
            return Ok(self.owned(c));
        }
        if let [b] = cands.as_slice() {
            self.isolated_check(*b, &v, name, e.span);
        }
        Ok(v)
    }

    /// A spawned task cannot read a variable of the program that others
    /// may change (a `var`, or a list, map, set or record).
    fn isolated_check(&mut self, b: BindingId, v: &Val, name: &str, span: Span) {
        let binding = &self.res.bindings[b];
        if !matches!(binding.origin, Origin::Plain | Origin::Param) {
            return;
        }
        let is_var = binding.kind == BKind::Var;
        let cell_birth = if self.res.is_static(b) {
            None
        } else {
            match self.slot_of(b) {
                Some(Slot::Value(_)) => return,
                _ => Some(self.cell_ptr(b)),
            }
        };
        let ip = self.reg();
        self.emit(&format!("{ip} = getelementptr inbounds i8, ptr %tls, i64 24"));
        let iso = self.reg();
        self.emit(&format!("{iso} = load i64, ptr {ip}"));
        let mut c = self.reg();
        self.emit(&format!("{c} = icmp ne i64 {iso}, 0"));
        if let Some(cell) = cell_birth {
            // a cell made by the task itself is its own
            let bp = self.reg();
            self.emit(&format!("{bp} = getelementptr inbounds i8, ptr {cell}, i64 24"));
            let birth = self.reg();
            self.emit(&format!("{birth} = load i64, ptr {bp}"));
            let sp_ptr = self.reg();
            self.emit(&format!("{sp_ptr} = getelementptr inbounds i8, ptr %tls, i64 16"));
            let stamp = self.reg();
            self.emit(&format!("{stamp} = load i64, ptr {sp_ptr}"));
            let old = self.reg();
            self.emit(&format!("{old} = icmp ult i64 {birth}, {stamp}"));
            let both = self.reg();
            self.emit(&format!("{both} = and i1 {c}, {old}"));
            c = both;
        }
        let bad = self.label();
        let ok = self.label();
        self.cond_br(&c, &bad, &ok);
        self.place(&bad);
        let tv = self.tb(v);
        let n = self.strarg(name);
        let sp = self.span_id(span);
        self.call_void("nx_isolated_read", &format!("{tv}, {n}, i32 {}, i32 {sp}", is_var as u32));
        self.check();
        self.br(&ok);
        self.place(&ok);
    }

    /// Does a statement use a shared variable (in its own expressions, not
    /// in the statements of its blocks)?
    fn uses_shared(&self, s: &Stmt) -> bool {
        if !self.res.sync {
            return false;
        }
        let mut exprs: Vec<&Expr> = Vec::new();
        match &s.kind {
            StmtKind::Expr(e) | StmtKind::Expect(e) | StmtKind::Return(Some(e)) => exprs.push(e),
            StmtKind::Assert { expr, .. } => exprs.push(expr),
            StmtKind::Assign { target, value, .. } => {
                exprs.push(target);
                exprs.push(value);
            }
            StmtKind::VarDecl { value, .. } | StmtKind::Shared { value, .. } => exprs.push(value),
            StmtKind::If { branches, .. } => exprs.extend(branches.iter().map(|(c, _)| c)),
            StmtKind::For { iterable: e, .. } | StmtKind::While { cond: e, .. } | StmtKind::Repeat { count: e, .. } => {
                exprs.push(e)
            }
            StmtKind::Match { subject, .. } => exprs.push(subject),
            StmtKind::Spawn { call } => exprs.push(call),
            StmtKind::Emit { args, .. } => exprs.extend(args.iter().map(|a| &a.value)),
            StmtKind::Select { arms } => exprs.extend(arms.iter().filter_map(|a| a.channel.as_ref().or(a.after.as_ref()))),
            _ => {}
        }
        if let StmtKind::Assign { .. } = &s.kind {
            if let Some(a) = self.res.assigns.get(&s.id) {
                if a.cands.iter().any(|(b, _)| self.res.bindings[*b].shared) {
                    return true;
                }
            }
        }
        exprs.iter().any(|e| self.expr_uses_shared(e))
    }

    fn expr_uses_shared(&self, e: &Expr) -> bool {
        if let ExprKind::Name(_) = &e.kind {
            if let Some(NameRes::Bindings { cands, .. }) = self.res.names.get(&e.id) {
                return cands.iter().any(|b| self.res.bindings[*b].shared);
            }
            return false;
        }
        let mut found = false;
        let mut visit = |x: &Expr| found |= self.expr_uses_shared(x);
        match &e.kind {
            ExprKind::Call { func, args } => {
                visit(func);
                for a in args {
                    visit(&a.value);
                }
            }
            ExprKind::Propagate(x) => visit(x),
            ExprKind::Coalesce { left, right } => {
                visit(left);
                visit(right);
            }
            ExprKind::Pipeline { source, steps } => {
                visit(source);
                for st in steps {
                    for x in st.expr.iter().chain(st.columns.iter()) {
                        visit(x);
                    }
                }
            }
            ExprKind::ParallelFor { iterable, .. } => visit(iterable),
            ExprKind::Lambda { .. } => {}
            _ => {
                for c in crate::checker::children(e) {
                    visit(c);
                }
            }
        }
        found
    }

    fn select_stmt(&mut self, s: &'a Stmt, arms: &'a [SelectArm]) -> GResult<()> {
        let mut chans = Vec::new();
        let mut limit = none();
        for a in arms {
            if let Some(c) = &a.channel {
                chans.push(self.expr(c, false)?);
            } else if let Some(x) = &a.after {
                limit = self.expr(x, false)?;
            }
        }
        let arr = self.values_array(&chans);
        let out = self.alloca("%V");
        let tl = self.tb(&limit);
        let sp = self.span_id(s.span);
        let idx = self.reg();
        self.emit(&format!("{idx} = call i64 @nx_select(ptr {arr}, i64 {}, {tl}, ptr {out}, i32 {sp})", chans.len()));
        self.check();
        self.drop_all(&chans);
        self.drop_val(&limit);
        let value = self.load(&out);
        let value = self.owned(value);
        let end = self.label();
        let mut k = 0;
        for (i, a) in arms.iter().enumerate() {
            let want = if a.channel.is_some() {
                k += 1;
                k - 1
            } else {
                chans.len()
            };
            let c = self.reg();
            self.emit(&format!("{c} = icmp eq i64 {idx}, {want}"));
            let yes = self.label();
            let next = self.label();
            self.cond_br(&c, &yes, &next);
            self.place(&yes);
            let scope = self.res.scope_of[&Node::SelectArm(s.id, i)];
            self.enter_scope(scope);
            if a.var.is_some() {
                let b = self.res.decl[&Node::SelectArm(s.id, i)];
                self.rc_inc(&value);
                self.store_binding_raw(b, &value);
            }
            self.block(&a.body)?;
            self.br(&end);
            self.place(&next);
        }
        self.br(&end);
        self.place(&end);
        self.drop_val(&value);
        Ok(())
    }

    fn builtin_value(&mut self, name: &str) -> Val {
        if name == "PI" {
            return lit(T_FLOAT, float_bits(std::f64::consts::PI));
        }
        if DIRECT_BUILTINS.contains(&name) {
            let key = format!("builtin {name}");
            let slot = match self.named_slots.get(&key) {
                Some(n) => n.clone(),
                None => {
                    let g = format!("@bv.{}", self.next());
                    let _ = writeln!(self.globals, "{g} = internal global %V zeroinitializer");
                    let r = format!("%i{}", self.next());
                    let _ = writeln!(self.init, "  {r} = call %V @nx_builtin(i32 {})\n  store %V {r}, ptr {g}", builtin_id(name).unwrap());
                    self.named_slots.insert(key, g.clone());
                    g
                }
            };
            return self.load_global(&slot);
        }
        self.named_value(name)
    }

    /// The value of a resolved name: the first binding that is assigned.
    /// A plain local is borrowed; anything else (a top-level variable or a
    /// captured one, which a call could change) is read with a reference.
    fn resolved(&mut self, res: &NameRes, name: &str, span: Span) -> GResult<Val> {
        let (cands, row, builtin, suggestion) = match res {
            NameRes::Builtin(n) => return Ok(self.builtin_value(n)),
            NameRes::Bindings { cands, row, builtin, suggestion } => (cands.clone(), *row, *builtin, suggestion.clone()),
        };
        if row.is_none() && cands.len() == 1 && self.res.bindings[cands[0]].entry {
            let b = cands[0];
            let v = self.load_binding(b);
            if self.res.is_static(b) || matches!(self.slot_of(b), Some(Slot::Cell(_)) | None) {
                self.rc_inc(&v);
                return Ok(self.owned(v));
            }
            return Ok(v);
        }
        let slot = self.alloca("%V");
        let done = self.label();
        let mut item_slot = None;
        if let Some(it) = row {
            let item = self.load_binding(it);
            let islot = self.temp(&item);
            item_slot = Some(islot);
            let found = self.alloca("i8");
            let ti = self.tb(&item);
            let n = self.strarg(name);
            let v = self.call_v("nx_row_get", &format!("{ti}, {n}, ptr {found}"));
            self.store(&slot, &v);
            let f = self.reg();
            self.emit(&format!("{f} = load i8, ptr {found}"));
            let c = self.reg();
            self.emit(&format!("{c} = icmp ne i8 {f}, 0"));
            let next = self.label();
            self.cond_br(&c, &done, &next);
            self.place(&next);
        }
        for b in &cands {
            let v = self.load_binding(*b);
            self.store(&slot, &v);
            let t = self.tag_of(&v);
            let c = self.reg();
            self.emit(&format!("{c} = icmp ne i64 {t}, {T_UNDEF}"));
            let next = self.label();
            let yes = self.label();
            self.cond_br(&c, &yes, &next);
            self.place(&yes);
            self.rc_inc(&v);
            self.br(&done);
            self.place(&next);
        }
        match item_slot {
            Some(_) if builtin => {
                let v = self.builtin_value(name);
                self.store(&slot, &v);
                self.br(&done);
            }
            Some(islot) => {
                let item = self.load(&islot);
                let ti = self.tb(&item);
                let n = self.strarg(name);
                let s = match &suggestion {
                    Some(s) => self.strarg(s),
                    None => "ptr null, i64 0".into(),
                };
                let sp = self.span_id(span);
                self.call_void("nx_row_missing", &format!("{ti}, {n}, {s}, i32 {sp}"));
                self.fail_here();
            }
            None => self.name_error(name, suggestion.as_deref(), span),
        }
        self.place(&done);
        // owned on every path (builtin values are constants)
        let v = self.load(&slot);
        Ok(self.owned(v))
    }

    /// A call. `first` is the value a pipeline passes as first argument
    /// (the caller keeps ownership of it).
    fn call_expr(&mut self, e: &'a Expr, func: &'a Expr, args: &'a [Arg], first: Option<Val>, span: Span) -> GResult<Val> {
        if let ExprKind::Name(n) = &func.kind {
            if n == "old" {
                if let Some(slot) = self.fs.last().unwrap().old_slots.get(&e.id).cloned() {
                    return Ok(self.load(&slot));
                }
            }
        }
        let mut seen_named = false;
        for a in args {
            if a.name.is_none() && seen_named {
                return Err(Diagnostic::new("positional_after_named", a.span));
            }
            seen_named |= a.name.is_some();
        }
        let sp = self.span_id(span);
        // the function itself is evaluated before the arguments
        enum Target<'p> {
            Builtin(u32),
            Method(String, String, Span),
            /// a method that changes its receiver: `xs.add(x)`
            Place(Place<'p>, String, Span),
            Value(String),
        }
        if let Some(v) = self.direct_call(func, args, &first, span)? {
            return Ok(v);
        }
        let target = match &func.kind {
            ExprKind::Name(n) => match self.res.names.get(&func.id) {
                Some(NameRes::Builtin(b)) if DIRECT_BUILTINS.contains(&b.as_str()) || matches!(b.as_str(), "decimal" | "float" | "int" | "str") => {
                    Target::Builtin(builtin_id(b).unwrap())
                }
                _ => {
                    let v = self.name_read(func, n)?;
                    Target::Value(self.spill(&v))
                }
            },
            ExprKind::Attr { obj, name, name_span }
                if name == "add" && matches!(obj.kind, ExprKind::Name(_) | ExprKind::Attr { .. } | ExprKind::Index { .. }) =>
            {
                let p = self.place_parts(obj)?;
                Target::Place(p, name.clone(), *name_span)
            }
            ExprKind::Attr { obj, name, name_span } => {
                let o = self.expr(obj, false)?;
                let slot = self.spill(&o);
                Target::Method(slot, name.clone(), *name_span)
            }
            _ => {
                let v = self.expr(func, false)?;
                Target::Value(self.spill(&v))
            }
        };
        let mut vals = Vec::new();
        let mut names: Vec<Option<&str>> = Vec::new();
        if let Some(f) = &first {
            vals.push(f.clone());
            names.push(None);
        }
        let mut own_args = Vec::new();
        for a in args {
            let mut v = self.expr(&a.value, true)?;
            if let Target::Place(p, ..) = &target {
                v = self.into_shared(p, v);
            }
            vals.push(v.clone());
            own_args.push(v);
            names.push(a.name.as_deref());
        }
        let n = vals.len();
        let r = match target {
            Target::Builtin(id) => {
                let arr = self.values_array(&vals);
                let nm = self.names_array(&names);
                let r = self.call_v("nx_builtin_call", &format!("i32 {id}, ptr {arr}, ptr {nm}, i64 {n}, i32 {sp}"));
                self.check();
                self.drop_all(&own_args);
                r
            }
            Target::Method(slot, name, name_span) => {
                let arr = self.values_array(&vals);
                let nm = self.names_array(&names);
                let o = self.load(&slot);
                let to = self.tb(&o);
                let na = self.strarg(&name);
                let nsp = self.span_id(name_span);
                let r = self.call_v("nx_call_method", &format!("{to}, {na}, ptr {arr}, ptr {nm}, i64 {n}, i32 {sp}, i32 {nsp}"));
                self.check();
                self.drop_all(&own_args);
                self.drop_slot(&slot);
                r
            }
            Target::Place(place, name, name_span) => {
                // the arguments are evaluated first; then the place is made
                // unique and changed (copy-on-write)
                let ptr = self.place_ptr(&place)?;
                let arr = self.values_array(&vals);
                let nm = self.names_array(&names);
                let na = self.strarg(&name);
                let nsp = self.span_id(name_span);
                let r = self.call_v("nx_call_method_at", &format!("ptr {ptr}, {na}, ptr {arr}, ptr {nm}, i64 {n}, i32 {sp}, i32 {nsp}"));
                self.check();
                self.drop_all(&own_args);
                self.drop_place(place);
                r
            }
            Target::Value(slot) => {
                let arr = self.values_array(&vals);
                let nm = self.names_array(&names);
                let f = self.load(&slot);
                let tf = self.tb(&f);
                let r = self.call_v("nx_call", &format!("{tf}, ptr {arr}, ptr {nm}, i64 {n}, i32 {sp}"));
                self.check();
                self.drop_all(&own_args);
                self.drop_slot(&slot);
                r
            }
        };
        Ok(self.owned(r))
    }

    /// A call to a function defined once, with its arguments bound when
    /// compiling: the LLVM function is called directly.
    fn direct_call(&mut self, func: &'a Expr, args: &'a [Arg], first: &Option<Val>, span: Span) -> GResult<Option<Val>> {
        let ExprKind::Name(name) = &func.kind else { return Ok(None) };
        let Some(NameRes::Bindings { cands, row: None, .. }) = self.res.names.get(&func.id) else { return Ok(None) };
        if cands.len() != 1 {
            return Ok(None);
        }
        let b = cands[0];
        let Some(f) = self.known_fns.get(&b).copied() else { return Ok(None) };
        // bind the arguments to the parameters now
        let n = f.params.len();
        let mut slots: Vec<Option<usize>> = vec![None; n];
        let offset = usize::from(first.is_some());
        let positional = args.iter().filter(|a| a.name.is_none()).count() + offset;
        if positional > n {
            return Ok(None);
        }
        let mut pi = offset;
        for (ai, a) in args.iter().enumerate() {
            match &a.name {
                None => {
                    slots[pi] = Some(ai);
                    pi += 1;
                }
                Some(nm) => match f.params.iter().position(|p| &p.name == nm) {
                    Some(i) if slots[i].is_none() && i >= offset => slots[i] = Some(ai),
                    _ => return Ok(None),
                },
            }
        }
        for (i, p) in f.params.iter().enumerate() {
            let optional = p.default.is_some() || p.ty.as_ref().is_some_and(|t| t.optional());
            if i >= offset && slots[i].is_none() && !optional {
                return Ok(None);
            }
        }
        let own_call = self.res.self_calls.contains(&func.id);
        // the function must have been defined when the call runs (D29);
        // a function defined once is never replaced, so it is only looked at
        // (a function calling itself uses its own closure)
        let fv = if own_call { lit(T_OBJ, "0") } else { self.load_binding(b) };
        let fslot = self.temp(&fv);
        let t = self.tag_of(&fv);
        let undef = self.reg();
        self.emit(&format!("{undef} = icmp eq i64 {t}, {T_UNDEF}"));
        let bad = self.label();
        let ok = self.label();
        self.cond_br(&undef, &bad, &ok);
        self.place(&bad);
        let sugg = match self.res.names.get(&func.id) {
            Some(NameRes::Bindings { suggestion, .. }) => suggestion.clone(),
            _ => None,
        };
        self.name_error(name, sugg.as_deref(), func.span);
        self.place(&ok);
        let mut vals: Vec<Val> = vec![lit(T_MISSING, "0"); n];
        if let Some(fst) = first {
            vals[0] = fst.clone();
        }
        // arguments are evaluated in the order they are written
        let mut evaluated: Vec<(usize, Val)> = Vec::new();
        for (ai, a) in args.iter().enumerate() {
            let mut v = self.expr(&a.value, true)?;
            if may_be_lazy(&a.value) {
                let tb = self.tb(&v);
                let forced = self.call_v("nx_force", &tb);
                self.check();
                self.drop_val(&v);
                v = self.owned(forced);
            }
            evaluated.push((ai, v));
        }
        for (i, s) in slots.iter().enumerate() {
            if let Some(ai) = s {
                vals[i] = evaluated.iter().find(|(x, _)| x == ai).unwrap().1.clone();
            }
        }
        let frame = self.res.frame_of[&Node::Fn(f.id)];
        let (fname, _) = self.frame_fn(frame, FrameSrc::Fn(f))?;
        let clo = if self.res.frames[frame].free.is_empty() {
            "null".to_string()
        } else if own_call {
            "%clo".to_string()
        } else {
            let fv = self.load(&fslot);
            let tb = self.tb(&fv);
            let p = self.reg();
            self.emit(&format!("{p} = call ptr @nx_func_ptr({tb})"));
            p
        };
        let arr = self.values_array(&vals);
        let sp = self.span_id(span);
        let r = self.reg();
        self.emit(&format!("{r} = call %V {fname}(ptr %tls, ptr {clo}, ptr {arr}, i32 {sp})"));
        self.check();
        for (_, v) in evaluated.iter().rev() {
            self.drop_val(v);
        }
        Ok(Some(self.owned(Val::Reg(r))))
    }

    fn step_closure(&mut self, node: Node, e: &'a Expr) -> GResult<Val> {
        let frame = self.res.frame_of[&node];
        self.closure(frame, FrameSrc::Step(e))
    }

    fn pipeline(&mut self, e: &'a Expr, source: &'a Expr, steps: &'a [PipeStep]) -> GResult<Val> {
        let mut v = self.expr(source, false)?;
        let mut grouped = false;
        for (i, step) in steps.iter().enumerate() {
            let ssp = self.span_id(step.span);
            let node = Node::Step(e.id, i);
            let slot = self.spill(&v);
            let r = match step.kind {
                StepKind::Call => {
                    let x = step.expr.as_ref().unwrap();
                    let cur = self.load(&slot);
                    match &x.kind {
                        ExprKind::Call { func, args } => self.call_expr(x, func, args, Some(cur), step.span)?,
                        _ => {
                            let f = self.expr(x, false)?;
                            let cur = self.load(&slot);
                            let arr = self.values_array(&[cur]);
                            let tf = self.tb(&f);
                            let r = self.call_v("nx_call", &format!("{tf}, ptr {arr}, ptr null, i64 1, i32 {ssp}"));
                            self.check();
                            self.drop_val(&f);
                            self.owned(r)
                        }
                    }
                }
                StepKind::Take => {
                    let x = step.expr.as_ref().unwrap();
                    let n = self.expr(x, false)?;
                    let cur = self.load(&slot);
                    let ts = self.tb(&cur);
                    let tn = self.tb(&n);
                    let nsp = self.span_id(x.span);
                    let r = self.call_v("nx_pipe_take", &format!("{ts}, {tn}, i32 {ssp}, i32 {nsp}"));
                    self.check();
                    self.drop_val(&n);
                    self.owned(r)
                }
                StepKind::Pick => {
                    let mut fns = Vec::new();
                    let mut cols = Vec::new();
                    for (ci, c) in step.columns.iter().enumerate() {
                        let ExprKind::Name(n) = &c.kind else {
                            let what = if nexo_diag::lang() == nexo_diag::Lang::En { "a column name" } else { "un nombre de columna" };
                            return Err(Diagnostic::new("pipe_step_arg", c.span)
                                .arg("step", "pick")
                                .arg("what", what)
                                .arg("example", "pick name, email"));
                        };
                        fns.push(self.step_closure(Node::Column(e.id, i, ci), c)?);
                        cols.push(n.as_str());
                    }
                    let farr = self.values_array(&fns);
                    let names: Vec<Option<&str>> = cols.iter().map(|c| Some(*c)).collect();
                    let narr = self.names_array(&names);
                    let cur = self.load(&slot);
                    let ts = self.tb(&cur);
                    let esp = self.span_id(step.columns[0].span);
                    let r = self.call_v("nx_pipe_pick", &format!("{ts}, ptr {farr}, ptr {narr}, i64 {}, i32 {ssp}, i32 {esp}", fns.len()));
                    self.drop_all(&fns);
                    self.owned(r)
                }
                StepKind::Count | StepKind::Sum | StepKind::Avg if grouped => {
                    grouped = false;
                    let kind = match step.kind {
                        StepKind::Sum => 0,
                        StepKind::Count => 1,
                        _ => 2,
                    };
                    let (f, col, esp) = match &step.expr {
                        Some(x) => {
                            let f = self.step_closure(node, x)?;
                            (f, source_text(self.src, x.span), self.span_id(x.span))
                        }
                        None => (none(), "count".to_string(), ssp),
                    };
                    let cur = self.load(&slot);
                    let ts = self.tb(&cur);
                    let tf = self.tb(&f);
                    let c = self.strarg(&col);
                    let r = self.call_v("nx_pipe_agg", &format!("{ts}, i32 {kind}, {tf}, {c}, i32 {ssp}, i32 {esp}"));
                    self.check();
                    self.drop_val(&f);
                    self.owned(r)
                }
                StepKind::Count | StepKind::Sum | StepKind::Avg => {
                    let f = match step.kind {
                        StepKind::Count => "nx_pipe_count",
                        StepKind::Sum => "nx_pipe_sum",
                        _ => "nx_pipe_avg",
                    };
                    let cur = self.load(&slot);
                    let ts = self.tb(&cur);
                    let r = self.call_v(f, &format!("{ts}, i32 {ssp}"));
                    self.check();
                    self.owned(r)
                }
                StepKind::Where | StepKind::Map | StepKind::Sort | StepKind::Group | StepKind::Each => {
                    let x = step.expr.as_ref().unwrap();
                    let f = self.step_closure(node, x)?;
                    let cur = self.load(&slot);
                    let ts = self.tb(&cur);
                    let tf = self.tb(&f);
                    let esp = self.span_id(x.span);
                    let r = match step.kind {
                        StepKind::Where => self.call_v("nx_pipe_where", &format!("{ts}, {tf}, i32 {ssp}, i32 {esp}")),
                        StepKind::Map => self.call_v("nx_pipe_map", &format!("{ts}, {tf}, i32 {ssp}, i32 {esp}")),
                        StepKind::Each => self.call_v("nx_pipe_each", &format!("{ts}, {tf}, i32 {ssp}, i32 {esp}")),
                        StepKind::Sort => {
                            self.call_v("nx_pipe_sort", &format!("{ts}, {tf}, i32 {}, i32 {ssp}, i32 {esp}", step.desc as u8))
                        }
                        _ => {
                            grouped = true;
                            let mut key = source_text(self.src, x.span);
                            if key.is_empty() {
                                key = "key".into();
                            }
                            let k = self.strarg(&key);
                            self.call_v("nx_pipe_group", &format!("{ts}, {tf}, {k}, i32 {ssp}, i32 {esp}"))
                        }
                    };
                    self.check();
                    self.drop_val(&f);
                    self.owned(r)
                }
            };
            self.drop_slot(&slot);
            v = r;
        }
        Ok(v)
    }

    fn parallel_for(&mut self, e: &'a Expr, iterable: &'a Expr, args: &'a [Arg], body: &'a [Stmt]) -> GResult<Val> {
        let mut max = none();
        for a in args {
            if a.name.as_deref() != Some("max") {
                return Err(Diagnostic::new("arg_unknown", a.span)
                    .arg("fn", "parallel for")
                    .arg("param", a.name.clone().unwrap_or_else(|| "?".into()))
                    .arg("params", "max"));
            }
            let v = self.expr(&a.value, false)?;
            let tv = self.tb(&v);
            let sp = self.span_id(a.value.span);
            self.call_void("nx_parallel_max", &format!("{tv}, i32 {sp}"));
            self.check();
            max = v;
        }
        let it = self.expr(iterable, false)?;
        // the body is a task: a function called for each element, on as
        // many threads as there are processors (or `max`)
        let frame = self.res.frame_of[&Node::Expr(e.id)];
        let clo = self.closure(frame, FrameSrc::Task(body))?;
        let ti = self.tb(&it);
        let tc = self.tb(&clo);
        let tm = self.tb(&max);
        let sp = self.span_id(iterable.span);
        let r = self.call_v("nx_parallel_for", &format!("{ti}, {tc}, {tm}, i32 {sp}"));
        self.check();
        self.drop_val(&clo);
        self.drop_val(&it);
        self.drop_val(&max);
        Ok(self.owned(r))
    }

    /// A task cannot change a variable that existed before it started
    /// (section 5): checked when the variable is a top-level one or a
    /// captured one, unless the task is the `parallel:` statement that
    /// computes it.
    fn task_write_check(&mut self, b: BindingId, name: &str, span: Span) {
        if self.res.bindings[b].shared {
            return;
        }
        let frame = self.fs.last().unwrap().frame;
        if self.res.task_outputs.get(&frame).is_some_and(|o| o.contains(&b)) {
            return;
        }
        let birth = if self.res.is_static(b) {
            None
        } else {
            match self.slot_of(b) {
                Some(Slot::Value(_)) => return,
                _ => {
                    let c = self.cell_ptr(b);
                    let p = self.reg();
                    self.emit(&format!("{p} = getelementptr inbounds i8, ptr {c}, i64 24"));
                    let x = self.reg();
                    self.emit(&format!("{x} = load i64, ptr {p}"));
                    Some(x)
                }
            }
        };
        let sp_ptr = self.reg();
        self.emit(&format!("{sp_ptr} = getelementptr inbounds i8, ptr %tls, i64 16"));
        let stamp = self.reg();
        self.emit(&format!("{stamp} = load i64, ptr {sp_ptr}"));
        let c = self.reg();
        match birth {
            None => self.emit(&format!("{c} = icmp ne i64 {stamp}, 0")),
            Some(x) => self.emit(&format!("{c} = icmp ult i64 {x}, {stamp}")),
        }
        let bad = self.label();
        let ok = self.label();
        self.cond_br(&c, &bad, &ok);
        self.place(&bad);
        let n = self.strarg(name);
        let sp = self.span_id(span);
        self.call_void("nx_task_write", &format!("{n}, i32 {sp}"));
        self.fail_here();
        self.place(&ok);
    }
}

pub const RUNTIME_DECLS: &str = r#"attributes #0 = { alwaysinline }

; reference counts (section 7): the count is the first word of a heap value
define internal void @rc_inc(%V %v) #0 {
  %t = extractvalue %V %v, 0
  %o = icmp eq i64 %t, 4
  br i1 %o, label %yes, label %no
yes:
  %b = extractvalue %V %v, 1
  %p = inttoptr i64 %b to ptr
  %x = atomicrmw add ptr %p, i64 1 monotonic
  ret void
no:
  ret void
}

define internal void @rc_dec(%V %v) #0 {
  %t = extractvalue %V %v, 0
  %o = icmp eq i64 %t, 4
  br i1 %o, label %yes, label %no
yes:
  %b = extractvalue %V %v, 1
  %p = inttoptr i64 %b to ptr
  %x = atomicrmw sub ptr %p, i64 1 release
  %last = icmp eq i64 %x, 1
  br i1 %last, label %free, label %no
free:
  fence acquire
  call void @nx_destroy(ptr %p)
  ret void
no:
  ret void
}

; a float literal becomes an ordinary float
define internal %V @nx_plainv(%V %v) #0 {
  %t = extractvalue %V %v, 0
  %l = icmp eq i64 %t, 5
  br i1 %l, label %yes, label %no
yes:
  %b = extractvalue %V %v, 1
  %p = inttoptr i64 %b to ptr
  %f = load i64, ptr %p
  %r = insertvalue %V { i64 3, i64 0 }, i64 %f, 1
  ret %V %r
no:
  ret %V %v
}

declare void @nx_destroy(ptr)
declare %V @nx_parallel_for(i64, i64, i64, i64, i64, i64, i32)
declare void @nx_parallel_block(ptr, i64, i32)
declare void @nx_task_write(ptr, i64, i32)
declare void @nx_spawn(i64, i64, ptr, ptr, i64, i32)
declare i64 @nx_select(ptr, i64, i64, i64, ptr, i32)
declare void @nx_shared_lock()
declare void @nx_shared_unlock()
declare %V @nx_shared_read(i64, i64)
declare %V @nx_shared_store(i64, i64)
declare void @nx_isolated_read(i64, i64, ptr, i64, i32, i32)
declare void @nx_cell_release(ptr)
declare void @nx_iter_free(ptr)
declare void @nx_scratch_clear()
declare void @nx_unique_slot(ptr)
declare ptr @nx_field_slot(i64, i64, ptr, i64, i32)
declare ptr @nx_index_slot(ptr, i64, i64, i32)
declare void @nx_set_index_at(ptr, i64, i64, i64, i64, i32)
declare %V @nx_index_at(ptr, i64, i64, i32)
declare %V @nx_call_method_at(ptr, ptr, i64, ptr, ptr, i64, i32, i32)
declare { i64, i1 } @llvm.sadd.with.overflow.i64(i64, i64)
declare { i64, i1 } @llvm.ssub.with.overflow.i64(i64, i64)
declare { i64, i1 } @llvm.smul.with.overflow.i64(i64, i64)
declare void @nx_start(ptr) noreturn
declare void @nx_too_deep()
declare void @nx_init(ptr, i64, ptr, i64, ptr, i64, i32, ptr)
declare void @nx_exit(i32) noreturn
declare void @nx_unhandled() noreturn
declare void @nx_run_tests() noreturn
declare void @nx_report()
declare %V @nx_take_error()
declare void @nx_reraise(i64, i64)
declare i8 @nx_error_is(i64, i64, ptr, i64)
declare void @nx_clear()
declare void @nx_name_error(ptr, i64, ptr, i64, i32)
declare void @nx_immutable(ptr, i64, i32)
declare void @nx_already_declared(ptr, i64, i32)
declare void @nx_not_bool(i64, i64, ptr, i64, i32)
declare %V @nx_str(ptr, i64)
declare %V @nx_unit(ptr, i64, ptr, i64)
declare %V @nx_builtin(i32)
declare %V @nx_named(ptr, i64)
declare %V @nx_list(ptr, i64)
declare void @nx_list_push(i64, i64, i64, i64)
declare %V @nx_map(ptr, ptr, ptr, i64)
declare %V @nx_range(i64, i64, i64, i64, i32)
declare %V @nx_index(i64, i64, i64, i64, i32)
declare void @nx_set_index(i64, i64, i64, i64, i64, i64, i32)
declare %V @nx_attr(i64, i64, ptr, i64, i32)
declare void @nx_set_attr(i64, i64, ptr, i64, i64, i64, i32, i32)
declare %V @nx_binop(i32, i64, i64, i64, i64, i32)
declare %V @nx_neg(i64, i64, i32)
declare i8 @nx_eq(i64, i64, i64, i64, i32)
declare i8 @nx_order(i32, i64, i64, i64, i64, i32)
declare i8 @nx_in(i64, i64, i64, i64, i32)
declare i8 @nx_is(i64, i64, i64, i64, i32)
declare %V @nx_interp(ptr, i64)
declare %V @nx_bind(i64, i64)
declare %V @nx_force(i64, i64)
declare ptr @nx_func_ptr(i64, i64)
declare %V @nx_conform(i64, i64, ptr, i64, ptr, i64, i32)
declare %V @nx_conform_return(i64, i64, ptr, i64, ptr, i64, i32)
declare %V @nx_call(i64, i64, ptr, ptr, i64, i32)
declare %V @nx_call_method(i64, i64, ptr, i64, ptr, ptr, i64, i32, i32)
declare %V @nx_builtin_call(i32, ptr, ptr, i64, i32)
declare %V @nx_closure(ptr, ptr, ptr, i64)
declare ptr @nx_cell()
declare %V @nx_define_record(i32, ptr, i64, i64, ptr, ptr)
declare %V @nx_define_enum(i32, ptr, i64, i64, ptr)
declare %V @nx_enum_variant(i64, i64, i64)
declare %V @nx_define_alias(i32, ptr, i64, ptr, i64)
declare %V @nx_define_trait(i32, ptr, i64, i64, ptr)
declare void @nx_add_method(i64, i64, ptr, i64, i64, i64)
declare void @nx_add_trait(i64, i64, i64, i64)
declare i8 @nx_match_variant(i64, i64, i64, i64)
declare i8 @nx_match_ctor(i64, i64, i64, i64, i64, ptr, i64, i32)
declare %V @nx_field_at(i64, i64, i64)
declare void @nx_no_match(i64, i64, i32)
declare %V @nx_pipe_where(i64, i64, i64, i64, i32, i32)
declare %V @nx_pipe_map(i64, i64, i64, i64, i32, i32)
declare %V @nx_pipe_pick(i64, i64, ptr, ptr, i64, i32, i32)
declare %V @nx_pipe_take(i64, i64, i64, i64, i32, i32)
declare %V @nx_pipe_sort(i64, i64, i64, i64, i32, i32, i32)
declare %V @nx_pipe_group(i64, i64, i64, i64, ptr, i64, i32, i32)
declare %V @nx_pipe_each(i64, i64, i64, i64, i32, i32)
declare %V @nx_pipe_count(i64, i64, i32)
declare %V @nx_pipe_sum(i64, i64, i32)
declare %V @nx_pipe_avg(i64, i64, i32)
declare %V @nx_pipe_agg(i64, i64, i32, i64, i64, ptr, i64, i32, i32)
declare %V @nx_row_get(i64, i64, ptr, i64, ptr)
declare void @nx_row_missing(i64, i64, ptr, i64, ptr, i64, i32)
declare ptr @nx_iter(i64, i64, i32)
declare i8 @nx_iter_next(ptr, ptr)
declare i64 @nx_repeat_count(i64, i64, i32)
declare void @nx_parallel_max(i64, i64, i32)
declare %V @nx_deep_copy(i64, i64)
declare void @nx_contract_broken(ptr, i64, ptr, i64, ptr, ptr, i64, i32)
declare void @nx_assert_failed(ptr, i64, i32)
declare void @nx_assert_cmp_failed(ptr, i64, ptr, i64, i64, i64, i64, i64, i32)
declare void @nx_assert_not_failed(ptr, i64, i32)
declare void @nx_on(ptr, i64, i64, i64)
declare void @nx_emit(ptr, i64, ptr, i64, i32)
declare void @nx_deadline_push(i64, i64, i32, i32)
declare void @nx_deadline_pop()
declare void @nx_check_deadline()
declare void @nx_fail_overflow(i32)
declare void @nx_test_register(ptr, i64, i64, i64)
"#;
