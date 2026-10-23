//! Global state of a running program: the source (for error snippets), the
//! pending error or diagnostic, buffered output and the registries of
//! user types and event handlers.
//!
//! Errors are values (section 3). A failing operation records the error in
//! the state, sets `nx_failing` and returns; the compiled code checks the
//! flag after every call that can fail and goes to the nearest `try`, or
//! returns to its caller. Diagnostics use the same path with a different
//! flag value, but `try` never catches them.

use std::cell::UnsafeCell;
use std::collections::HashMap;
use std::io::{BufWriter, Write};
use std::time::Instant;

use nexo_diag::{render, unhandled_text, Diagnostic, Span};

use crate::types::TypeRef;
use crate::value::{ErrorValue, Obj, V};

/// 0: running normally; 1: an error value is on its way to a `try`;
/// 2: a diagnostic is on its way to the end of the program (or the test).
#[no_mangle]
pub static mut nx_failing: u8 = 0;

/// 1 while a `try within` block is running, so statements check the clock.
#[no_mangle]
pub static mut nx_deadline_active: u8 = 0;

pub const ERROR: u8 = 1;
pub const DIAG: u8 = 2;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct SpanRec {
    pub line: u32,
    pub col: u32,
    pub end_col: u32,
}

pub struct State {
    pub file: &'static str,
    pub source: &'static str,
    pub spans: &'static [SpanRec],
    pub error: Option<V>,
    pub diag: Option<(Diagnostic, u32)>,
    pub out: BufWriter<std::io::Stdout>,
    pub types: HashMap<u32, TypeRef>,
    pub handlers: HashMap<String, Vec<V>>,
    pub deadlines: Vec<(Instant, V, u32)>,
    pub tests_run: u32,
    pub tests_passed: u32,
}

struct Global(UnsafeCell<Option<State>>);
unsafe impl Sync for Global {}

static STATE: Global = Global(UnsafeCell::new(None));

pub fn init(file: &'static str, source: &'static str, spans: &'static [SpanRec]) {
    unsafe {
        *STATE.0.get() = Some(State {
            file,
            source,
            spans,
            error: None,
            diag: None,
            out: BufWriter::with_capacity(1 << 16, std::io::stdout()),
            types: HashMap::new(),
            handlers: HashMap::new(),
            deadlines: Vec::new(),
            tests_run: 0,
            tests_passed: 0,
        });
    }
}

#[allow(clippy::mut_from_ref)]
pub fn st() -> &'static mut State {
    unsafe { (*STATE.0.get()).as_mut().expect("runtime not initialised") }
}

/// Marker: the operation failed and the failure is recorded in the state.
#[derive(Debug, Clone, Copy)]
pub struct Fail;

pub type R<T> = Result<T, Fail>;

pub fn failing() -> u8 {
    unsafe { nx_failing }
}

pub fn check() -> R<()> {
    if failing() != 0 {
        Err(Fail)
    } else {
        Ok(())
    }
}

pub fn span(id: u32) -> Option<Span> {
    if id == 0 {
        return None;
    }
    st().spans.get(id as usize - 1).map(|s| Span::new(s.line, s.col, s.end_col))
}

/// Raise an error value of a system kind (NotFound, Overflow...).
pub fn failure(kind: &str, span_id: u32, args: &[(&str, String)]) -> Fail {
    let message = nexo_diag::msg(&format!("err.{kind}"), args);
    raise_error(kind, message, V { tag: 0, bits: 0 }, span_id)
}

pub fn raise_error(kind: &str, message: String, cause: V, span_id: u32) -> Fail {
    let fix_key = format!("fix.{kind}");
    let fix = if nexo_diag::has(&fix_key) { Some(&*Box::leak(fix_key.into_boxed_str())) } else { None };
    let line = span(span_id).map(|s| s.line);
    let err = V::obj(Obj::Error(ErrorValue {
        kind: kind.to_string(),
        message,
        cause,
        line,
        span: if span_id == 0 { None } else { Some(span_id) },
        fix,
    }));
    raise_value(err)
}

/// Raise an existing error value again (`fail(e)`, an unmatched handler).
pub fn raise_value(err: V) -> Fail {
    let s = st();
    s.error = Some(err);
    unsafe { nx_failing = ERROR };
    Fail
}

/// Record a diagnostic. It ends the program (or the current test).
pub fn diag(d: Diagnostic, span_id: u32) -> Fail {
    let s = st();
    if s.diag.is_none() {
        s.diag = Some((d, span_id));
    }
    s.error = None;
    unsafe { nx_failing = DIAG };
    Fail
}

pub fn take_error() -> V {
    let s = st();
    unsafe { nx_failing = 0 };
    s.error.take().unwrap_or(V { tag: 0, bits: 0 })
}

pub fn clear() {
    let s = st();
    s.error = None;
    s.diag = None;
    unsafe { nx_failing = 0 };
}

pub fn flush() {
    let _ = st().out.flush();
}

pub fn write_out(text: &str) {
    if st().out.write_all(text.as_bytes()).is_err() {
        broken_pipe();
    }
}

pub fn broken_pipe() -> ! {
    // a closed output pipe (`nexo app.nexo | head`) ends the program quietly
    std::process::exit(0)
}

/// Text of the pending failure in the section 14 format.
pub fn pending_text() -> String {
    let s = st();
    if let Some((d, span_id)) = s.diag.take() {
        let mut d = d;
        if d.span.is_none() {
            d.span = span(span_id);
        }
        return render(s.file, s.source, d.span, &d.text());
    }
    if let Some(err) = s.error.take() {
        if let Some(Obj::Error(e)) = err.get() {
            let text = unhandled_text(&e.kind, &e.message, e.fix);
            return render(s.file, s.source, e.span.and_then(span), &text);
        }
    }
    String::new()
}

/// Show the pending failure on stderr (after flushing stdout).
pub fn report_pending() {
    flush();
    let text = pending_text();
    unsafe { nx_failing = 0 };
    let mut err = std::io::stderr();
    let _ = err.write_all(text.as_bytes());
    let _ = err.flush();
}
