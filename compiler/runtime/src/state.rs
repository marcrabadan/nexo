//! State of a running program.
//!
//! Shared by all tasks: the source (for error snippets), the standard
//! output, and the registries of user types, event handlers and tests.
//! Each thread (the main program, a task of `parallel` or of `spawn`) has
//! its own pending error and diagnostic, call depth and output buffer.
//!
//! Errors are values (section 3). A failing operation records the error in
//! the thread's state, sets `failing` and returns; the compiled code checks
//! the flag after every call that can fail and goes to the nearest `try`,
//! or returns to its caller. Diagnostics use the same path with a different
//! flag value, but `try` never catches them.

use std::cell::{RefCell, UnsafeCell};
use std::collections::HashMap;
use std::io::{BufWriter, Write};
use std::sync::{Mutex, OnceLock, RwLock};
use std::time::Instant;

use nexo_diag::{render, unhandled_text, Diagnostic, Span};

use crate::types::TypeRef;
use crate::value::{ErrorValue, Obj, Val, V};

/// The part of a thread's state the compiled code reads directly: every
/// compiled function receives a pointer to it.
#[repr(C)]
pub struct Tls {
    /// 0: running normally; 1: an error value is on its way to a `try`;
    /// 2: a diagnostic is on its way to the end of the program (or test).
    pub failing: u8,
    /// 1 while a `try within` runs: statements call nx_check_deadline.
    pub deadline_active: u8,
    pub pad: [u8; 6],
    /// calls in progress (D73)
    pub depth: u64,
    /// when this task started (see `cow::check_write`)
    pub stamp: u64,
}

thread_local! {
    static TLS: UnsafeCell<Tls> = const {
        UnsafeCell::new(Tls { failing: 0, deadline_active: 0, pad: [0; 6], depth: 0, stamp: 0 })
    };
    static TASK: RefCell<TaskState> = RefCell::new(TaskState::default());
}

#[inline]
pub fn tls() -> *mut Tls {
    TLS.with(|t| t.get())
}

#[inline]
fn t() -> &'static mut Tls {
    unsafe { &mut *tls() }
}

pub const MAX_DEPTH: u64 = 10_000;

pub const ERROR: u8 = 1;
pub const DIAG: u8 = 2;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct SpanRec {
    pub line: u32,
    pub col: u32,
    pub end_col: u32,
}

pub struct Program {
    pub file: &'static str,
    pub source: &'static str,
    pub spans: &'static [SpanRec],
}

/// What belongs to one thread.
#[derive(Default)]
pub struct TaskState {
    pub error: Option<Val>,
    pub diag: Option<(Diagnostic, u32)>,
    pub deadlines: Vec<(Instant, Val, u32)>,
    /// output of a `parallel` task, written in order when the block ends
    pub buffer: Option<String>,
}

static PROGRAM: OnceLock<Program> = OnceLock::new();
static OUT: Mutex<Option<BufWriter<std::io::Stdout>>> = Mutex::new(None);

pub struct Registry {
    pub types: HashMap<u32, TypeRef>,
    /// every type ever defined (to release methods and defaults at the end)
    pub all_types: Vec<TypeRef>,
    pub handlers: HashMap<String, Vec<Val>>,
}

static REGISTRY: OnceLock<RwLock<Registry>> = OnceLock::new();

pub fn registry() -> &'static RwLock<Registry> {
    REGISTRY.get_or_init(|| RwLock::new(Registry { types: HashMap::new(), all_types: Vec::new(), handlers: HashMap::new() }))
}

pub fn init(file: &'static str, source: &'static str, spans: &'static [SpanRec]) {
    let _ = PROGRAM.set(Program { file, source, spans });
    *OUT.lock().unwrap() = Some(BufWriter::with_capacity(1 << 16, std::io::stdout()));
    registry();
}

pub fn program() -> &'static Program {
    PROGRAM.get().expect("runtime not initialised")
}

pub fn with_task<T>(f: impl FnOnce(&mut TaskState) -> T) -> T {
    TASK.with(|t| f(&mut t.borrow_mut()))
}

/// Marker: the operation failed and the failure is recorded in the state.
#[derive(Debug, Clone, Copy)]
pub struct Fail;

pub type R<T> = Result<T, Fail>;

#[inline]
pub fn failing() -> u8 {
    t().failing
}

pub fn set_failing(f: u8) {
    t().failing = f;
}

#[inline]
pub fn check() -> R<()> {
    if failing() != 0 {
        Err(Fail)
    } else {
        Ok(())
    }
}

pub fn depth() -> u64 {
    t().depth
}

pub fn set_depth(d: u64) {
    t().depth = d;
}

pub fn stamp() -> u64 {
    t().stamp
}

pub fn set_stamp(s: u64) {
    t().stamp = s;
}

pub fn set_deadline_active(on: bool) {
    t().deadline_active = on as u8;
}

pub fn span(id: u32) -> Option<Span> {
    if id == 0 {
        return None;
    }
    program().spans.get(id as usize - 1).map(|s| Span::new(s.line, s.col, s.end_col))
}

/// Raise an error value of a system kind (NotFound, Overflow...).
pub fn failure(kind: &str, span_id: u32, args: &[(&str, String)]) -> Fail {
    let message = nexo_diag::msg(&format!("err.{kind}"), args);
    raise_error(kind, message, Val::none(), span_id)
}

pub fn raise_error(kind: &str, message: String, cause: Val, span_id: u32) -> Fail {
    let fix_key = format!("fix.{kind}");
    let fix = if nexo_diag::has(&fix_key) { Some(crate::num::intern(&fix_key)) } else { None };
    let line = span(span_id).map(|s| s.line);
    let err = Val::new(Obj::Error(ErrorValue {
        kind: kind.to_string(),
        message,
        cause,
        line,
        span: if span_id == 0 { None } else { Some(span_id) },
        fix,
    }));
    raise_value(err)
}

/// Raise an error value (`fail(e)`, an unmatched handler).
pub fn raise_value(err: Val) -> Fail {
    let old = with_task(|s| s.error.replace(err));
    drop(old);
    set_failing(ERROR);
    Fail
}

/// Record a diagnostic. It ends the program (or the current test).
pub fn diag(d: Diagnostic, span_id: u32) -> Fail {
    let old = with_task(|s| {
        if s.diag.is_none() {
            s.diag = Some((d, span_id));
        }
        s.error.take()
    });
    drop(old);
    set_failing(DIAG);
    Fail
}

pub fn take_error() -> Val {
    set_failing(0);
    with_task(|s| s.error.take()).unwrap_or(Val::none())
}

pub fn clear() {
    let old = with_task(|s| {
        s.diag = None;
        s.error.take()
    });
    drop(old);
    set_failing(0);
}

/// The pending failure of this thread, to pass it to another one.
pub struct Pending {
    pub kind: u8,
    pub error: Option<Val>,
    pub diag: Option<(Diagnostic, u32)>,
}

pub fn take_pending() -> Pending {
    let kind = failing();
    set_failing(0);
    with_task(|s| Pending { kind, error: s.error.take(), diag: s.diag.take() })
}

pub fn restore_pending(p: Pending) {
    let old = with_task(|s| {
        s.diag = p.diag;
        std::mem::replace(&mut s.error, p.error)
    });
    drop(old);
    set_failing(p.kind);
}

pub fn flush() {
    if let Some(out) = OUT.lock().unwrap().as_mut() {
        let _ = out.flush();
    }
}

pub fn write_out(text: &str) {
    let buffered = with_task(|s| match s.buffer.as_mut() {
        Some(b) => {
            b.push_str(text);
            true
        }
        None => false,
    });
    if buffered {
        return;
    }
    let mut guard = OUT.lock().unwrap();
    if let Some(out) = guard.as_mut() {
        if out.write_all(text.as_bytes()).is_err() {
            drop(guard);
            broken_pipe();
        }
    }
}

pub fn broken_pipe() -> ! {
    // a closed output pipe (`nexo app.nexo | head`) ends the program quietly
    std::process::exit(0)
}

/// Text of the pending failure in the section 14 format.
pub fn pending_text() -> String {
    let p = program();
    let (d, err) = with_task(|s| (s.diag.take(), s.error.take()));
    if let Some((d, span_id)) = d {
        let mut d = d;
        if d.span.is_none() {
            d.span = span(span_id);
        }
        return render(p.file, p.source, d.span, &d.text());
    }
    if let Some(err) = err {
        if let Some(Obj::Error(e)) = err.get() {
            let text = unhandled_text(&e.kind, &e.message, e.fix);
            return render(p.file, p.source, e.span.and_then(span), &text);
        }
    }
    String::new()
}

/// Show the pending failure on stderr (after flushing stdout).
pub fn report_pending() {
    flush();
    let text = pending_text();
    set_failing(0);
    let mut err = std::io::stderr();
    let _ = err.write_all(text.as_bytes());
    let _ = err.flush();
}

pub fn borrowed_error(v: V) -> Option<&'static ErrorValue> {
    match v.get() {
        Some(Obj::Error(e)) => Some(e),
        _ => None,
    }
}
