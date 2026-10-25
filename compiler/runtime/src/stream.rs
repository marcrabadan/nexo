//! nexo.stream (section 19, D117): streams are sources for `when` and `|>`.
//! Follows phase 0's stream.py: a local broker of append-only files under
//! `.nexo/streams/<kind>/<name>.log` (in memory under `nexo test`, where
//! `send` delivers at once), at-least-once handlers with 3 retries and a
//! dead-letter stream, keys that are processed once, and windows.

use std::io::Write;
use std::sync::{Arc, Mutex};

use nexo_diag::{msg, Diagnostic};

use crate::call::call_value;
use crate::fmt::repr;
use crate::json::{self, Json};
use crate::state::{diag, R};
use crate::value::*;

pub const KINDS: &[&str] = &["kafka", "rabbit", "sqs", "kinesis", "servicebus", "eventhub", "pubsub", "websocket"];
const REMOTE: &[&str] = &["servers", "url", "region", "namespace", "project", "endpoint"];
const ROOT: &str = ".nexo/streams";
const BACKOFF: [u64; 3] = [100, 200, 400];

static TEST_MODE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

struct Handler {
    stream: (String, String),
    func: Val,
    key: Option<Val>,
}

#[derive(Default)]
struct Broker {
    handlers: Vec<Handler>,
    queries: Vec<Arc<Mutex<Query>>>,
    /// test mode: stream ident -> messages (id, ms, data)
    memory: Vec<(String, Vec<(i64, i64, String)>)>,
    keys: Vec<(String, Vec<String>)>,
    group: String,
}

static BROKER: Mutex<Option<Broker>> = Mutex::new(None);

fn with<T>(f: impl FnOnce(&mut Broker) -> T) -> T {
    let mut b = BROKER.lock().unwrap();
    f(b.get_or_insert_with(Broker::default))
}

pub fn set_test_mode() {
    TEST_MODE.store(true, std::sync::atomic::Ordering::SeqCst);
}

pub fn in_test_mode() -> bool {
    test_mode()
}

fn test_mode() -> bool {
    TEST_MODE.load(std::sync::atomic::Ordering::SeqCst)
}

pub fn release_all() {
    let old = BROKER.lock().unwrap().take();
    drop(old);
}

/// Before each test: the in-memory broker starts empty.
pub fn reset_memory() {
    with(|b| {
        b.memory.clear();
        b.keys.clear();
    });
}

fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

// ---------------------------------------------------------------- values

/// A source: `kafka("orders")` and the others (section 19).
pub struct Stream {
    pub kind: String,
    pub name: String,
}

impl Stream {
    fn ident(&self) -> String {
        format!("{}:{}", self.kind, self.name)
    }
    fn pair(&self) -> (String, String) {
        (self.kind.clone(), self.name.clone())
    }
}

impl Native for Stream {
    fn type_name(&self) -> &str {
        "stream"
    }
    fn repr(&self) -> String {
        format!("{}(\"{}\")", self.kind, self.name)
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        if name == "send" {
            return Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "send")));
        }
        Err(crate::stdlib::missing_member("stream", name, &["send"], span))
    }
    fn call_method(&self, _me: V, _name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        let a = crate::stdlib::method_args("send", &["value", "at"], 1, args, names, span)?;
        let ms = if a[1].is_none() || a[1].tag == T_MISSING {
            now_ms()
        } else {
            match a[1].native::<crate::time::Time>() {
                Some(t) => t.ms,
                None => return Err(crate::stdlib::expects("send", "time", a[1], span)),
            }
        };
        let value = crate::pipe::bind(a[0])?;
        let data = match value.v().as_str() {
            Some(s) => s.to_string(),
            None => crate::data::to_json_compact(value.v()),
        };
        let message = append(&self.pair(), data, ms);
        if test_mode() {
            deliver(&message, span);
        }
        Ok(Val::none())
    }
    fn equals(&self, other: &dyn Native) -> bool {
        other.as_any().downcast_ref::<Stream>().is_some_and(|o| o.ident() == self.ident())
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// One message: `msg.json()`, `msg.text()`, `msg.id`, `msg.time`.
#[derive(Clone)]
pub struct Message {
    pub stream: (String, String),
    pub id: i64,
    pub ms: i64,
    pub data: String,
}

impl Message {
    /// The decoded payload (pipelines work on it): JSON, or the text.
    fn value(&self, span: u32) -> Val {
        match crate::data::json_parse(&self.data, None, span) {
            Ok(v) => v,
            Err(_) => {
                crate::state::clear();
                Val::str(self.data.clone())
            }
        }
    }
}

impl Native for Message {
    fn type_name(&self) -> &str {
        "message"
    }
    fn repr(&self) -> String {
        format!("message({}:{} {})", self.stream.0, self.stream.1, self.id)
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        match name {
            "id" => Ok(Val::int(self.id)),
            "time" => Ok(native(crate::time::Time { ms: self.ms, zone: crate::time::local_zone() })),
            "json" => Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "json"))),
            "text" => Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "text"))),
            _ => Err(crate::stdlib::missing_member("message", name, &["id", "time", "json", "text"], span)),
        }
    }
    fn call_method(&self, _me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        crate::stdlib::method_args(name, &[], 0, args, names, span)?;
        if name == "json" {
            return crate::data::json_parse(&self.data, None, span);
        }
        Ok(Val::str(self.data.clone()))
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

pub fn source(kind: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    for n in names.iter().flatten() {
        if REMOTE.contains(n) {
            return Err(diag(Diagnostic::new_spanless("not_supported").arg("what", format!("\"{kind}(..., {n}: ...)\"")), span));
        }
        return Err(diag(Diagnostic::new_spanless("arg_unknown").arg("fn", kind.to_string()).arg("param", *n).arg("params", "-"), span));
    }
    let a = crate::stdlib::method_args(kind, &["name"], 1, args, names, span)?;
    let Some(name) = a[0].as_str() else { return Err(crate::stdlib::expects(kind, "str", a[0], span)) };
    if kind == "websocket" {
        return Err(diag(Diagnostic::new_spanless("not_supported").arg("what", "\"websocket(...)\""), span));
    }
    Ok(native(Stream { kind: kind.to_string(), name: name.to_string() }))
}

fn log_path(s: &(String, String)) -> String {
    format!("{ROOT}/{}/{}.log", s.0, s.1)
}

fn append(s: &(String, String), data: String, ms: i64) -> Message {
    if test_mode() {
        return with(|b| {
            let ident = format!("{}:{}", s.0, s.1);
            let pos = match b.memory.iter().position(|(k, _)| *k == ident) {
                Some(p) => p,
                None => {
                    b.memory.push((ident, Vec::new()));
                    b.memory.len() - 1
                }
            };
            let log = &mut b.memory[pos].1;
            let id = log.len() as i64 + 1;
            log.push((id, ms, data.clone()));
            Message { stream: s.clone(), id, ms, data }
        });
    }
    let _guard = BROKER.lock().unwrap();
    let path = log_path(s);
    let _ = std::fs::create_dir_all(format!("{ROOT}/{}", s.0));
    let count = std::fs::read_to_string(&path).map(|t| t.lines().count()).unwrap_or(0) as i64;
    let message = Message { stream: s.clone(), id: count + 1, ms, data };
    let line = json::obj(vec![("id", Json::Int(message.id)), ("time", Json::Int(ms)), ("data", json::s(message.data.clone()))]).compact();
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(f, "{line}");
    }
    message
}

// -------------------------------------------------------------- handlers

pub fn when(src: V, func: V, key: Option<V>, span: u32) -> R<Val> {
    let Some(s) = src.native::<Stream>() else { return Err(crate::stdlib::expects("when", "stream", src, span)) };
    let key = key.filter(|k| !k.is_none()).map(Val::from);
    with(|b| b.handlers.push(Handler { stream: s.pair(), func: Val::from(func), key }));
    Ok(Val::none())
}

pub fn listening() -> bool {
    with(|b| !b.handlers.is_empty() || !b.queries.is_empty())
}

/// The text of the pending failure, without the place (for the dead
/// letter line).
fn pending_message() -> String {
    let text = crate::state::pending_plain();
    crate::state::set_failing(0);
    text
}

fn group() -> String {
    with(|b| if b.group.is_empty() { "nexo".to_string() } else { b.group.clone() })
}

/// The keys each handler (the n-th `when` of its stream) has processed: its
/// own, so that two handlers of one stream each process a message once.
fn keys_path(s: &(String, String), n: usize) -> String {
    format!("{ROOT}/{}/{}.{}.{n}.keys", s.0, s.1, group())
}

fn seen_key(s: &(String, String), n: usize, k: &str) -> bool {
    if test_mode() {
        let ident = format!("{}:{}#{n}", s.0, s.1);
        return with(|b| b.keys.iter().any(|(i, ks)| *i == ident && ks.iter().any(|x| x == k)));
    }
    std::fs::read_to_string(keys_path(s, n)).map(|t| t.lines().any(|l| l == k)).unwrap_or(false)
}

fn remember_key(s: &(String, String), n: usize, k: String) {
    if test_mode() {
        let ident = format!("{}:{}#{n}", s.0, s.1);
        with(|b| match b.keys.iter_mut().find(|(i, _)| *i == ident) {
            Some((_, ks)) => ks.push(k),
            None => b.keys.push((ident, vec![k])),
        });
        return;
    }
    let _ = std::fs::create_dir_all(format!("{ROOT}/{}", s.0));
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(keys_path(s, n)) {
        let _ = writeln!(f, "{k}");
    }
}

/// Run a handler (the n-th `when` of its stream) with retries; then the
/// dead-letter stream.
fn handle(func: &Val, key: &Option<Val>, m: &Message, n: usize, span: u32) {
    let mv = native(m.clone());
    let mut k = None;
    if let Some(kf) = key {
        match call_value(kf.v(), &[mv.v()], span).and_then(|v| crate::pipe::bind(v.v())) {
            Ok(v) => {
                let text = repr(v.v());
                if seen_key(&m.stream, n, &text) {
                    return;
                }
                k = Some(text);
            }
            Err(_) => {
                let e = pending_message();
                dead(m, &e);
                return;
            }
        }
    }
    let mut error = None;
    for attempt in 0..4 {
        match call_value(func.v(), &[mv.v()], span) {
            Ok(_) => {
                error = None;
                break;
            }
            Err(_) => {
                error = Some(pending_message());
                if attempt < 3 {
                    std::thread::sleep(std::time::Duration::from_millis(BACKOFF[attempt]));
                }
            }
        }
    }
    if let Some(e) = error {
        dead(m, &e);
        return;
    }
    if let Some(k) = k {
        remember_key(&m.stream, n, k);
    }
}

fn dead(m: &Message, error: &str) {
    let target = (m.stream.0.clone(), format!("{}.dead", m.stream.1));
    append(&target, m.data.clone(), m.ms);
    crate::state::flush();
    eprintln!("{}", msg("info.stream_dead", &[("stream", m.stream.1.clone()), ("id", m.id.to_string()), ("error", error.to_string())]));
}

/// A message reaches every handler and query of its stream.
fn deliver(m: &Message, span: u32) {
    let handlers: Vec<(Val, Option<Val>)> =
        with(|b| b.handlers.iter().filter(|h| h.stream == m.stream).map(|h| (h.func.clone(), h.key.clone())).collect());
    for (i, (f, k)) in handlers.iter().enumerate() {
        handle(f, k, m, i + 1, span);
    }
    let queries: Vec<Arc<Mutex<Query>>> = with(|b| b.queries.clone());
    for q in queries {
        let mine = q.lock().unwrap().stream == m.stream;
        if mine {
            add(&q, m.clone(), span);
        }
    }
}

// --------------------------------------------------------------- windows

type Op = Arc<dyn Fn(V) -> R<Val> + Send + Sync>;

/// A pipeline whose source is a stream: its steps are kept and run on the
/// messages of each window; `each` at the end starts it.
pub struct Query {
    stream: (String, String),
    /// "window" | "sliding" | "session" and its size in ms
    window: Option<(&'static str, i64)>,
    ops: Vec<Op>,
    open: Vec<Message>,
}

pub struct QueryVal(Arc<Mutex<Query>>);

impl Native for QueryVal {
    fn type_name(&self) -> &str {
        "stream"
    }
    fn repr(&self) -> String {
        let q = self.0.lock().unwrap();
        format!("{}(\"{}\")", q.stream.0, q.stream.1)
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// The query behind a pipeline value whose source is a stream, if any.
pub fn query_of(v: V) -> Option<(Arc<Mutex<Query>>, bool)> {
    if let Some(s) = v.native::<Stream>() {
        return Some((Arc::new(Mutex::new(Query { stream: s.pair(), window: None, ops: Vec::new(), open: Vec::new() })), true));
    }
    v.native::<QueryVal>().map(|q| (q.0.clone(), false))
}

/// One more step of a stream pipeline (D117).
pub fn push(src: V, op: Op, each: bool) -> Option<Val> {
    let (q, _) = query_of(src)?;
    let (stream, window, mut ops) = {
        let q = q.lock().unwrap();
        (q.stream.clone(), q.window, q.ops.clone())
    };
    ops.push(op);
    let next = Arc::new(Mutex::new(Query { stream, window, ops, open: Vec::new() }));
    if each {
        with(|b| b.queries.push(next));
        return Some(Val::none());
    }
    Some(native(QueryVal(next)))
}

pub fn window(src: V, size: V, kind: &'static str, step_span: u32, size_span: u32) -> R<Val> {
    let Some((q, _)) = query_of(src) else {
        return Err(diag(Diagnostic::new_spanless("stream_window_source").arg("step", kind), step_span));
    };
    let (stream, has) = {
        let q = q.lock().unwrap();
        (q.stream.clone(), q.window.is_some() || !q.ops.is_empty())
    };
    if has {
        return Err(diag(Diagnostic::new_spanless("stream_window_first").arg("step", kind), step_span));
    }
    let ms = match size.get() {
        Some(Obj::Quantity(d, unit)) => {
            let (dim, factor) = crate::num::unit_dimension(unit);
            let secs = d.mul(&factor);
            if dim == "time" && secs.cmp(&crate::dec::Dec::zero()).is_gt() {
                secs.mul(&crate::dec::Dec::from_i64(1000)).to_integral(crate::dec::Rounding::Down).to_i64()
            } else {
                None
            }
        }
        _ => None,
    };
    let Some(ms) = ms else {
        return Err(diag(Diagnostic::new_spanless("stream_window").arg("step", kind), size_span));
    };
    Ok(native(QueryVal(Arc::new(Mutex::new(Query { stream, window: Some((kind, ms)), ops: Vec::new(), open: Vec::new() })))))
}

fn run(q: &Arc<Mutex<Query>>, messages: Vec<Message>, span: u32) {
    let ops = q.lock().unwrap().ops.clone();
    let items: Vec<Val> = messages.iter().map(|m| m.value(span)).collect();
    let mut value = list(items);
    for op in ops {
        match op(value.v()) {
            Ok(v) => value = v,
            Err(_) => {
                crate::state::report_pending();
                return;
            }
        }
    }
    drop(value);
}

fn add(q: &Arc<Mutex<Query>>, m: Message, span: u32) {
    let window = q.lock().unwrap().window;
    let Some((kind, size)) = window else {
        run(q, vec![m], span);
        return;
    };
    if kind == "sliding" {
        let open = {
            let mut g = q.lock().unwrap();
            g.open.push(m.clone());
            g.open.retain(|x| x.ms > m.ms - size);
            g.open.clone()
        };
        run(q, open, span);
        return;
    }
    let closed = {
        let mut g = q.lock().unwrap();
        let close = match (g.open.first(), g.open.last()) {
            (Some(first), Some(last)) => {
                (kind == "window" && m.ms.div_euclid(size) != first.ms.div_euclid(size)) || (kind == "session" && m.ms - last.ms > size)
            }
            _ => false,
        };
        let out = if close { std::mem::take(&mut g.open) } else { Vec::new() };
        g.open.push(m);
        out
    };
    if !closed.is_empty() {
        run(q, closed, span);
    }
}

fn due(q: &Query) -> bool {
    let Some((kind, size)) = q.window else { return false };
    if q.open.is_empty() || kind == "sliding" {
        return false;
    }
    if kind == "window" {
        return now_ms() >= (q.open[0].ms.div_euclid(size) + 1) * size;
    }
    now_ms() - q.open[q.open.len() - 1].ms > size
}

fn flush_query(q: &Arc<Mutex<Query>>) {
    let messages = {
        let mut g = q.lock().unwrap();
        if g.window.is_none() || g.window.map(|w| w.0) == Some("sliding") {
            return;
        }
        std::mem::take(&mut g.open)
    };
    if !messages.is_empty() {
        run(q, messages, 0);
    }
}

/// Close every open window (end of a test, end of the program).
pub fn drain() {
    let queries: Vec<Arc<Mutex<Query>>> = with(|b| b.queries.clone());
    for q in queries {
        flush_query(&q);
    }
}

// --------------------------------------------------------------- run mode

fn offset_path(s: &(String, String)) -> String {
    format!("{ROOT}/{}/{}.{}.offset", s.0, s.1, group())
}

/// Save the offset up to `read`, but not past the messages of a window
/// still open: after a stop they are read again (at least once, D117).
fn acknowledge(s: &(String, String), read: i64) {
    let mut done = read;
    let queries: Vec<Arc<Mutex<Query>>> = with(|b| b.queries.clone());
    for q in queries {
        let g = q.lock().unwrap();
        let windowed = g.window.is_some_and(|w| w.0 != "sliding");
        if g.stream == *s && windowed && !g.open.is_empty() {
            done = done.min(g.open[0].id - 1);
        }
    }
    let _ = std::fs::write(offset_path(s), format!("{done}\n"));
}

/// `nexo file.nexo` with `when` handlers or stream pipelines: read the new
/// messages of every stream until the program is stopped.
pub fn listen_forever(program: &str) {
    let stem = std::path::Path::new(program).file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    with(|b| b.group = if stem.is_empty() { "nexo".into() } else { stem });
    let mut streams: Vec<(String, String)> = Vec::new();
    let all: Vec<(String, String)> = with(|b| {
        let mut v: Vec<(String, String)> = b.handlers.iter().map(|h| h.stream.clone()).collect();
        v.extend(b.queries.iter().map(|q| q.lock().unwrap().stream.clone()));
        v
    });
    for s in all {
        if !streams.contains(&s) {
            streams.push(s);
        }
    }
    let shown: Vec<String> = streams.iter().map(|s| format!("{}(\"{}\")", s.0, s.1)).collect();
    crate::state::flush();
    eprintln!("{}", msg("info.stream_listening", &[("streams", shown.join(", "))]));
    // what was delivered, per stream
    let mut read: Vec<i64> =
        streams.iter().map(|s| std::fs::read_to_string(offset_path(s)).ok().and_then(|t| t.trim().parse().ok()).unwrap_or(0)).collect();
    loop {
        let mut idle = true;
        for (si, s) in streams.iter().enumerate() {
            let mut done = read[si];
            let text = std::fs::read_to_string(log_path(s)).unwrap_or_default();
            for (i, line) in text.lines().enumerate() {
                let n = i as i64 + 1;
                if n <= done {
                    continue;
                }
                let Some(d) = json::parse(line) else { continue };
                let m = Message {
                    stream: s.clone(),
                    id: d.get("id").int().unwrap_or(n),
                    ms: d.get("time").int().unwrap_or(0),
                    data: d.get("data").str().unwrap_or("").to_string(),
                };
                idle = false;
                deliver(&m, 0);
                crate::state::flush();
                done = m.id;
                read[si] = done;
                acknowledge(s, done);
            }
        }
        let queries: Vec<Arc<Mutex<Query>>> = with(|b| b.queries.clone());
        for q in queries {
            let d = due(&q.lock().unwrap());
            if d {
                flush_query(&q);
                let stream = q.lock().unwrap().stream.clone();
                if let Some(si) = streams.iter().position(|s| *s == stream) {
                    acknowledge(&stream, read[si]);
                }
            }
        }
        if idle {
            std::thread::sleep(std::time::Duration::from_millis(200));
        }
    }
}
