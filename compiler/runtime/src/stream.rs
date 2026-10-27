//! nexo.stream (section 19, D117): streams are sources for `when` and `|>`.
//! Follows phase 0's stream.py: a local broker of append-only files under
//! `.nexo/streams/<kind>/<name>.log` (one per partition when a stream has
//! several, D167; in memory under `nexo test`, where `send` delivers at
//! once), at-least-once handlers with 3 retries and a dead-letter stream,
//! keys that are processed once, windows, and the partitions of a stream
//! handled as parallel tasks, each in order.

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

/// The option that puts a source in its cloud (D168).
pub fn cloud_option(kind: &str) -> Option<&'static str> {
    match kind {
        "sqs" | "kinesis" => Some("region"),
        "servicebus" => Some("namespace"),
        "pubsub" => Some("project"),
        _ => None,
    }
}
const ROOT: &str = ".nexo/streams";
const BACKOFF: [u64; 3] = [100, 200, 400];

static TEST_MODE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

struct Handler {
    stream: (String, String),
    /// the region, namespace or project of a source in its cloud (D168)
    place: Option<String>,
    /// partitions: n of its source, if given (D167)
    wanted: Option<i64>,
    func: Val,
    key: Option<Val>,
}

#[derive(Default)]
struct Broker {
    handlers: Vec<Handler>,
    queries: Vec<Arc<Mutex<Query>>>,
    /// test mode: stream ident#partition -> messages (id, ms, data)
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
    /// partitions: n, if given (D167)
    pub wanted: Option<i64>,
    /// the region, namespace or project of a source in its cloud (D168)
    pub place: Option<String>,
}

impl Stream {
    fn ident(&self) -> String {
        let at = self.place.as_ref().map(|p| format!("@{p}")).unwrap_or_default();
        format!("{}:{}{at}", self.kind, self.name)
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
        let a = crate::stdlib::method_args("send", &["value", "at", "key"], 1, args, names, span)?;
        let ms = if a[1].is_none() || a[1].tag == T_MISSING {
            now_ms()
        } else {
            match a[1].native::<crate::time::Time>() {
                Some(t) => t.ms,
                None => return Err(crate::stdlib::expects("send", "time", a[1], span)),
            }
        };
        let text = |v: V| -> R<String> {
            let value = crate::pipe::bind(v)?;
            Ok(match value.v().as_str() {
                Some(s) => s.to_string(),
                None => crate::data::to_json_compact(value.v()),
            })
        };
        let data = text(a[0])?;
        let key = if a[2].is_none() || a[2].tag == T_MISSING { None } else { Some(text(a[2])?) };
        let message = match &self.place {
            Some(place) if !test_mode() => {
                crate::policy::check("net", &crate::stream_cloud::host(&self.kind, place), span)?;
                crate::stream_cloud::send(&self.pair(), place, &data, ms, key.as_deref(), None, span)?
            }
            _ => append(&self.pair(), data, ms, key, self.wanted, Some(span))?,
        };
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

/// The id of a message: its number in the local broker, the id the cloud
/// gives it (D168), or none.
#[derive(Clone, PartialEq)]
pub enum Id {
    Int(i64),
    Text(String),
    None,
}

impl Id {
    fn val(&self) -> Val {
        match self {
            Id::Int(n) => Val::int(*n),
            Id::Text(t) => Val::str(t.clone()),
            Id::None => Val::none(),
        }
    }
    /// As Python's str() writes it (the dead-letter line).
    fn shown(&self) -> String {
        match self {
            Id::Int(n) => n.to_string(),
            Id::Text(t) => t.clone(),
            Id::None => "None".into(),
        }
    }
}

/// One message: `msg.json()`, `msg.text()`, `msg.id`, `msg.time`,
/// `msg.key` and `msg.partition` (D167).
#[derive(Clone)]
pub struct Message {
    pub stream: (String, String),
    pub place: Option<String>,
    pub id: Id,
    pub ms: i64,
    pub data: String,
    pub key: Option<String>,
    pub partition: i64,
    /// where a consumer starts to read it again (a log), or what
    /// acknowledges it (a queue)
    pub before: Option<String>,
    pub receipt: Option<String>,
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
        format!("message({}:{} {})", self.stream.0, self.stream.1, self.id.shown())
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        match name {
            "id" => Ok(self.id.val()),
            "time" => Ok(native(crate::time::Time { ms: self.ms, zone: crate::time::local_zone() })),
            "json" => Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "json"))),
            "text" => Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "text"))),
            "key" => Ok(self.key.clone().map(Val::str).unwrap_or_else(Val::none)),
            "partition" => Ok(Val::int(self.partition)),
            _ => Err(crate::stdlib::missing_member("message", name, &["id", "time", "json", "text", "key", "partition"], span)),
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
    let mut wanted = None;
    let mut place = None;
    for (i, n) in names.iter().enumerate() {
        let Some(n) = n else { continue };
        if *n == "partitions" {
            match args[i].as_int() {
                Some(c) if c >= 1 && args[i].tag == T_INT => wanted = Some(c),
                _ => return Err(crate::stdlib::expects("partitions", "int", args[i], span)),
            }
            continue;
        }
        if cloud_option(kind) == Some(*n) {
            match args[i].as_str() {
                Some(p) if !p.is_empty() => place = Some(p.to_string()),
                _ => return Err(crate::stdlib::expects(n, "str", args[i], span)),
            }
            continue;
        }
        if REMOTE.contains(n) {
            return Err(diag(Diagnostic::new_spanless("not_supported").arg("what", format!("\"{kind}(..., {n}: ...)\"")), span));
        }
        let known = match cloud_option(kind) {
            Some(o) => format!("partitions, {o}"),
            None => "partitions".into(),
        };
        return Err(diag(Diagnostic::new_spanless("arg_unknown").arg("fn", kind.to_string()).arg("param", *n).arg("params", known), span));
    }
    let positional: Vec<V> = args.iter().zip(names).filter(|(_, n)| n.is_none()).map(|(a, _)| *a).collect();
    let nones: Vec<Option<&str>> = positional.iter().map(|_| None).collect();
    let a = crate::stdlib::method_args(kind, &["name"], 1, &positional, &nones, span)?;
    let Some(name) = a[0].as_str() else { return Err(crate::stdlib::expects(kind, "str", a[0], span)) };
    if kind == "websocket" {
        return Err(diag(Diagnostic::new_spanless("not_supported").arg("what", "\"websocket(...)\""), span));
    }
    if let (Some(_), Some(_)) = (&place, wanted) {
        // the cloud says how many partitions (shards) a stream has
        let o = cloud_option(kind).unwrap_or("");
        return Err(diag(Diagnostic::new_spanless("arg_unknown").arg("fn", kind.to_string()).arg("param", "partitions").arg("params", o), span));
    }
    Ok(native(Stream { kind: kind.to_string(), name: name.to_string(), wanted, place }))
}

/// Kafka's hash of a key, so a key goes to the partition Kafka's own
/// clients choose (D167).
pub fn murmur2(data: &[u8]) -> u32 {
    let m: u32 = 0x5BD1_E995;
    let mut h: u32 = 0x9747_B28C ^ data.len() as u32;
    let chunks = data.chunks_exact(4);
    let rest = chunks.remainder();
    for c in chunks {
        let mut k = u32::from_le_bytes([c[0], c[1], c[2], c[3]]);
        k = k.wrapping_mul(m);
        k ^= k >> 24;
        k = k.wrapping_mul(m);
        h = h.wrapping_mul(m);
        h ^= k;
    }
    if rest.len() == 3 {
        h ^= (rest[2] as u32) << 16;
    }
    if rest.len() >= 2 {
        h ^= (rest[1] as u32) << 8;
    }
    if !rest.is_empty() {
        h ^= rest[0] as u32;
        h = h.wrapping_mul(m);
    }
    h ^= h >> 13;
    h = h.wrapping_mul(m);
    h ^= h >> 15;
    h
}

/// The partition of a message: by its key, or by its data without one.
pub fn partition_of(key: Option<&str>, data: &str, count: i64) -> i64 {
    if count <= 1 {
        return 0;
    }
    ((murmur2(key.unwrap_or(data).as_bytes()) & 0x7FFF_FFFF) as i64) % count
}

/// How many partitions a stream has: fixed when it is created (the file
/// `<name>.partitions`), 1 for a stream created without them; an error
/// when a source asks for another number.
fn partitions(s: &(String, String), wanted: Option<i64>, span: Option<u32>) -> R<i64> {
    if test_mode() {
        return Ok(wanted.unwrap_or(1));
    }
    let meta = format!("{ROOT}/{}/{}.partitions", s.0, s.1);
    let mut have = std::fs::read_to_string(&meta).ok().and_then(|t| t.trim().parse::<i64>().ok()).unwrap_or(0);
    if have == 0 && std::path::Path::new(&format!("{ROOT}/{}/{}.log", s.0, s.1)).exists() {
        // a stream that already has its one log has one partition
        have = 1;
    }
    if have == 0 {
        have = wanted.unwrap_or(1);
        if have > 1 {
            let _ = std::fs::create_dir_all(format!("{ROOT}/{}", s.0));
            let _ = std::fs::write(&meta, format!("{have}\n"));
        }
    }
    if let (Some(w), Some(span)) = (wanted, span) {
        if w != have {
            let shown = format!("{}(\"{}\")", s.0, s.1);
            let d = Diagnostic::new_spanless("stream_partitions").arg("stream", shown).arg("have", have.to_string()).arg("want", w.to_string());
            return Err(diag(d, span));
        }
    }
    Ok(have)
}

/// The log of a partition: `<name>.log` when the stream has one,
/// `<name>.<p>.log` when it has several.
fn log_path(s: &(String, String), partition: i64, count: i64) -> String {
    if count == 1 {
        return format!("{ROOT}/{}/{}.log", s.0, s.1);
    }
    format!("{ROOT}/{}/{}.{partition}.log", s.0, s.1)
}

fn append(s: &(String, String), data: String, ms: i64, key: Option<String>, wanted: Option<i64>, span: Option<u32>) -> R<Message> {
    let count = partitions(s, wanted, span)?;
    let p = partition_of(key.as_deref(), &data, count);
    if test_mode() {
        return Ok(with(|b| {
            let ident = format!("{}:{}#{p}", s.0, s.1);
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
            Message { stream: s.clone(), place: None, id: Id::Int(id), ms, data, key, partition: p, before: Some((id - 1).to_string()), receipt: None }
        }));
    }
    let _guard = BROKER.lock().unwrap();
    let path = log_path(s, p, count);
    let _ = std::fs::create_dir_all(format!("{ROOT}/{}", s.0));
    // the lock of the file itself: producers in other processes count and
    // append one at a time, so ids stay unique
    let file = std::fs::OpenOptions::new().create(true).read(true).append(true).open(&path);
    let mut count: i64 = 0;
    if let Ok(f) = &file {
        let _ = f.lock();
        let mut text = String::new();
        let _ = std::io::Read::read_to_string(&mut &*f, &mut text);
        count = text.lines().count() as i64;
    }
    let message = Message {
        stream: s.clone(),
        place: None,
        id: Id::Int(count + 1),
        ms,
        data,
        key,
        partition: p,
        before: Some(count.to_string()),
        receipt: None,
    };
    let mut fields = vec![("id", Json::Int(count + 1)), ("time", Json::Int(ms)), ("data", json::s(message.data.clone()))];
    if let Some(k) = &message.key {
        fields.push(("key", json::s(k.clone())));
    }
    let line = json::obj(fields).compact();
    if let Ok(mut f) = file {
        let _ = writeln!(f, "{line}");
        let _ = f.unlock();
    }
    Ok(message)
}

// -------------------------------------------------------------- handlers

pub fn when(src: V, func: V, key: Option<V>, span: u32) -> R<Val> {
    let Some(s) = src.native::<Stream>() else { return Err(crate::stdlib::expects("when", "stream", src, span)) };
    let key = key.filter(|k| !k.is_none()).map(Val::from);
    with(|b| b.handlers.push(Handler { stream: s.pair(), place: s.place.clone(), wanted: s.wanted, func: Val::from(func), key }));
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

/// The consumer group: the name of the program's file.
pub fn group_name() -> String {
    group()
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
    let Some(kf) = key else { return finish(func, &mv, m, n, None, span) };
    match call_value(kf.v(), &[mv.v()], span).and_then(|v| crate::pipe::bind(v.v())) {
        Ok(v) => {
            let text = repr(v.v());
            // partitions run at once: a key another one is handling waits
            // for it, then counts as seen
            let _held = Busy::hold(format!("{}:{}#{n}#{text}", m.stream.0, m.stream.1));
            if !seen_key(&m.stream, n, &text) {
                finish(func, &mv, m, n, Some(text), span);
            }
        }
        Err(_) => {
            let e = pending_message();
            dead(m, &e);
        }
    }
}

/// The keys being handled now, by stream, handler and key.
static BUSY: Mutex<Vec<String>> = Mutex::new(Vec::new());
static BUSY_FREE: std::sync::Condvar = std::sync::Condvar::new();

struct Busy(String);

impl Busy {
    fn hold(slot: String) -> Busy {
        let mut busy = BUSY.lock().unwrap();
        while busy.contains(&slot) {
            busy = BUSY_FREE.wait(busy).unwrap();
        }
        busy.push(slot.clone());
        Busy(slot)
    }
}

impl Drop for Busy {
    fn drop(&mut self) {
        BUSY.lock().unwrap().retain(|s| *s != self.0);
        BUSY_FREE.notify_all();
    }
}

/// The handler with its retries; then the dead-letter stream.
fn finish(func: &Val, mv: &Val, m: &Message, n: usize, k: Option<String>, span: u32) {
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
    let target = match &m.place {
        Some(place) if !test_mode() => match crate::stream_cloud::dead(m, place) {
            Ok(t) => t,
            Err(_) => {
                // the dead letter cannot be kept: the consumer stops, and
                // the message, not acknowledged, comes back (at least once)
                crate::state::report_pending();
                std::process::exit(1);
            }
        },
        _ => {
            let t = (m.stream.0.clone(), format!("{}.dead", m.stream.1));
            let _ = append(&t, m.data.clone(), m.ms, m.key.clone(), None, None);
            t.1
        }
    };
    let line = msg(
        "info.stream_dead",
        &[("stream", m.stream.1.clone()), ("id", m.id.shown()), ("dead", target), ("error", error.to_string())],
    );
    // inside a partition's task the line waits with what the task wrote
    let captured = CAPTURE.with(|c| {
        let mut c = c.borrow_mut();
        let Some(events) = c.as_mut() else { return false };
        if let Some(out) = crate::state::with_task(|s| s.buffer.as_mut().map(std::mem::take)).filter(|o| !o.is_empty()) {
            events.push((false, out));
        }
        events.push((true, line.clone()));
        true
    });
    if !captured {
        crate::state::flush();
        eprintln!("{line}");
    }
}

thread_local! {
    /// what a partition's task writes, in order: (to the error output, text)
    static CAPTURE: std::cell::RefCell<Option<Vec<(bool, String)>>> = const { std::cell::RefCell::new(None) };
}

/// A message reaches every handler and query of its stream.
fn deliver(m: &Message, span: u32) {
    handlers_of(m, span);
    queries_of(m, span);
}

fn handlers_of(m: &Message, span: u32) {
    let handlers: Vec<(Val, Option<Val>)> = with(|b| {
        b.handlers.iter().filter(|h| h.stream == m.stream && h.place == m.place).map(|h| (h.func.clone(), h.key.clone())).collect()
    });
    for (i, (f, k)) in handlers.iter().enumerate() {
        handle(f, k, m, i + 1, span);
    }
}

fn queries_of(m: &Message, span: u32) {
    let queries: Vec<Arc<Mutex<Query>>> = with(|b| b.queries.clone());
    for q in queries {
        let mine = {
            let q = q.lock().unwrap();
            q.stream == m.stream && q.place == m.place
        };
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
    place: Option<String>,
    /// partitions: n of its source, if given (D167)
    wanted: Option<i64>,
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
        let q = Query { stream: s.pair(), place: s.place.clone(), wanted: s.wanted, window: None, ops: Vec::new(), open: Vec::new() };
        return Some((Arc::new(Mutex::new(q)), true));
    }
    v.native::<QueryVal>().map(|q| (q.0.clone(), false))
}

/// One more step of a stream pipeline (D117).
pub fn push(src: V, op: Op, each: bool) -> Option<Val> {
    let (q, _) = query_of(src)?;
    let (stream, place, wanted, window, mut ops) = {
        let q = q.lock().unwrap();
        (q.stream.clone(), q.place.clone(), q.wanted, q.window, q.ops.clone())
    };
    ops.push(op);
    let next = Arc::new(Mutex::new(Query { stream, place, wanted, window, ops, open: Vec::new() }));
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
    let (stream, place, wanted, has) = {
        let q = q.lock().unwrap();
        (q.stream.clone(), q.place.clone(), q.wanted, q.window.is_some() || !q.ops.is_empty())
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
    let q = Query { stream, place, wanted, window: Some((kind, ms)), ops: Vec::new(), open: Vec::new() };
    Ok(native(QueryVal(Arc::new(Mutex::new(q)))))
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

fn offset_path(s: &(String, String), partition: i64, count: i64) -> String {
    if count == 1 {
        return format!("{ROOT}/{}/{}.{}.offset", s.0, s.1, group());
    }
    format!("{ROOT}/{}/{}.{partition}.{}.offset", s.0, s.1, group())
}

/// The position a consumer may save: `position`, but not past the
/// messages of a window still open, so that after a stop they are read
/// again (at least once, D117).
pub fn kept(s: &(String, String), place: &Option<String>, partition: i64, mut position: Option<String>) -> Option<String> {
    let num = |p: &str| p.parse::<i128>().unwrap_or(0);
    let queries: Vec<Arc<Mutex<Query>>> = with(|b| b.queries.clone());
    for q in queries {
        let g = q.lock().unwrap();
        let windowed = g.window.is_some_and(|w| w.0 != "sliding");
        if g.stream == *s && g.place == *place && windowed {
            if let Some(first) = g.open.iter().find(|m| m.partition == partition) {
                let earlier = match (&first.before, &position) {
                    (None, _) | (_, None) => true,
                    (Some(b), Some(p)) => num(b) < num(p),
                };
                if earlier {
                    position = first.before.clone();
                }
            }
        }
    }
    position
}

/// The messages of a partition of the local broker after `after`.
fn new_messages(s: &(String, String), partition: i64, count: i64, after: i64) -> Vec<Message> {
    let text = std::fs::read_to_string(log_path(s, partition, count)).unwrap_or_default();
    let mut out = Vec::new();
    for (i, line) in text.lines().enumerate() {
        let n = i as i64 + 1;
        if n <= after {
            continue;
        }
        let Some(d) = json::parse(line) else { continue };
        let id = d.get("id").int().unwrap_or(n);
        out.push(Message {
            stream: s.clone(),
            place: None,
            id: Id::Int(id),
            ms: d.get("time").int().unwrap_or(0),
            data: d.get("data").str().unwrap_or("").to_string(),
            key: d.get("key").str().map(|k| k.to_string()),
            partition,
            before: Some((id - 1).to_string()),
            receipt: None,
        });
    }
    out
}

/// The broker of a stream: the local one of D117, or its cloud's (D168).
enum Backend {
    Local,
    Cloud(String),
}

impl Backend {
    fn of(place: &Option<String>) -> Backend {
        match place {
            Some(p) => Backend::Cloud(p.clone()),
            None => Backend::Local,
        }
    }

    /// Whether a consumer saves where it is (a log) or deletes what it
    /// read (a queue).
    fn log(&self, s: &(String, String)) -> bool {
        match self {
            Backend::Local => true,
            Backend::Cloud(_) => s.0 == "kinesis",
        }
    }

    fn partitions(&self, s: &(String, String), wanted: Option<i64>) -> R<i64> {
        match self {
            Backend::Local => partitions(s, wanted, Some(0)),
            Backend::Cloud(place) => crate::stream_cloud::partitions(s, place, 0),
        }
    }

    fn start(&self, s: &(String, String), p: i64, count: i64) -> Option<String> {
        match self {
            Backend::Local => {
                let n: i64 = std::fs::read_to_string(offset_path(s, p, count)).ok().and_then(|t| t.trim().parse().ok()).unwrap_or(0);
                Some(n.to_string())
            }
            Backend::Cloud(_) => crate::stream_cloud::start(s, p),
        }
    }

    fn poll(&self, s: &(String, String), p: i64, count: i64, position: &Option<String>) -> R<Vec<Message>> {
        match self {
            Backend::Local => {
                let after = position.as_deref().and_then(|t| t.parse().ok()).unwrap_or(0);
                Ok(new_messages(s, p, count, after))
            }
            Backend::Cloud(place) => crate::stream_cloud::poll(s, place, p, position, 0),
        }
    }

    fn done(&self, s: &(String, String), p: i64, count: i64, messages: &[Message], position: &Option<String>) -> R<()> {
        match self {
            Backend::Local => {
                let at = kept(s, &None, p, position.clone()).unwrap_or_else(|| "0".into());
                let _ = std::fs::write(offset_path(s, p, count), format!("{at}\n"));
                Ok(())
            }
            Backend::Cloud(place) => crate::stream_cloud::done(s, place, p, messages, position, 0),
        }
    }
}

/// The work of one round: the new messages of each partition of a stream,
/// handled by one task per partition (D167).
struct Round {
    batches: Vec<Vec<Message>>,
    /// what each task wrote, in order: (to the error output, text)
    events: Arc<Mutex<Vec<Vec<(bool, String)>>>>,
}

impl Native for Round {
    fn type_name(&self) -> &str {
        "task"
    }
    fn repr(&self) -> String {
        "task".into()
    }
    fn call_method(&self, _me: V, _name: &str, args: &[V], _names: &[Option<&str>], _span: u32) -> R<Val> {
        let i = args.first().and_then(|a| a.as_int()).unwrap_or(0) as usize;
        CAPTURE.with(|c| *c.borrow_mut() = Some(Vec::new()));
        for m in &self.batches[i] {
            handlers_of(m, 0);
        }
        let mut events = CAPTURE.with(|c| c.borrow_mut().take()).unwrap_or_default();
        if let Some(out) = crate::state::with_task(|s| s.buffer.as_mut().map(std::mem::take)).filter(|o| !o.is_empty()) {
            events.push((false, out));
        }
        self.events.lock().unwrap()[i] = events;
        Ok(Val::none())
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// A stream a consumer reads.
struct Source {
    stream: (String, String),
    place: Option<String>,
    backend: Backend,
    count: i64,
    /// what was delivered in each partition
    read: Vec<Option<String>>,
}

/// `nexo file.nexo` with `when` handlers or stream pipelines: read the new
/// messages of every stream until the program is stopped.
pub fn listen_forever(program: &str) {
    let stem = std::path::Path::new(program).file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    with(|b| b.group = if stem.is_empty() { "nexo".into() } else { stem });
    type Wanted = ((String, String), Option<String>, Option<i64>);
    let mut found: Vec<Wanted> = Vec::new();
    let all: Vec<Wanted> = with(|b| {
        let mut v: Vec<Wanted> = b.handlers.iter().map(|h| (h.stream.clone(), h.place.clone(), h.wanted)).collect();
        v.extend(b.queries.iter().map(|q| {
            let q = q.lock().unwrap();
            (q.stream.clone(), q.place.clone(), q.wanted)
        }));
        v
    });
    for s in all {
        if !found.iter().any(|(x, p, _)| *x == s.0 && *p == s.1) {
            found.push(s);
        }
    }
    let shown: Vec<String> = found.iter().map(|(s, _, _)| format!("{}(\"{}\")", s.0, s.1)).collect();
    crate::state::flush();
    eprintln!("{}", msg("info.stream_listening", &[("streams", shown.join(", "))]));
    let stop = || -> ! {
        crate::state::report_pending();
        std::process::exit(1);
    };
    let mut sources = Vec::new();
    for (stream, place, wanted) in found {
        let backend = Backend::of(&place);
        if let Some(p) = &place {
            if crate::policy::check("net", &crate::stream_cloud::host(&stream.0, p), 0).is_err() {
                stop();
            }
        }
        let count = match backend.partitions(&stream, wanted) {
            Ok(c) => c,
            Err(_) => stop(),
        };
        let read = (0..count).map(|p| backend.start(&stream, p, count)).collect();
        sources.push(Source { stream, place, backend, count, read });
    }
    loop {
        let mut idle = true;
        for src in sources.iter_mut() {
            let (s, count) = (&src.stream, src.count);
            let mut batches: Vec<(i64, Vec<Message>)> = Vec::new();
            let mut unreachable = false;
            for p in 0..count {
                match src.backend.poll(s, p, count, &src.read[p as usize]) {
                    Ok(ms) if !ms.is_empty() => batches.push((p, ms)),
                    Ok(_) => {}
                    Err(_) => {
                        // a broker that cannot be reached now: tried again
                        crate::state::report_pending();
                        unreachable = true;
                        break;
                    }
                }
            }
            if unreachable {
                std::thread::sleep(std::time::Duration::from_secs(1));
                continue;
            }
            if batches.is_empty() {
                continue;
            }
            idle = false;
            let log = src.backend.log(s);
            if count == 1 {
                for m in &batches[0].1 {
                    deliver(m, 0);
                    crate::state::flush();
                    src.read[0] = if log { Some(m.id.shown()) } else { None };
                    if src.backend.done(s, 0, 1, std::slice::from_ref(m), &src.read[0]).is_err() {
                        stop();
                    }
                }
                continue;
            }
            // one task per partition, each in order (D167): what they write
            // comes out in partition order, as `parallel for` does
            let events = Arc::new(Mutex::new(vec![Vec::new(); batches.len()]));
            let round = native(Round { batches: batches.iter().map(|(_, ms)| ms.clone()).collect(), events: events.clone() });
            let task = Val::new(Obj::BuiltinMethod(round, "run"));
            let items = list((0..batches.len() as i64).map(Val::int).collect());
            if crate::par::parallel_for(items.v(), task.v(), NONE, 0).is_err() {
                stop();
            }
            for events in events.lock().unwrap().iter() {
                for (err, text) in events {
                    if *err {
                        crate::state::flush();
                        eprintln!("{text}");
                    } else {
                        crate::state::write_out(text);
                    }
                }
            }
            crate::state::flush();
            for (p, ms) in &batches {
                for m in ms {
                    queries_of(m, 0);
                }
                src.read[*p as usize] = if log { Some(ms[ms.len() - 1].id.shown()) } else { None };
                if src.backend.done(s, *p, count, ms, &src.read[*p as usize]).is_err() {
                    stop();
                }
            }
        }
        let queries: Vec<Arc<Mutex<Query>>> = with(|b| b.queries.clone());
        for q in queries {
            let d = due(&q.lock().unwrap());
            if d {
                flush_query(&q);
                let (stream, place) = {
                    let q = q.lock().unwrap();
                    (q.stream.clone(), q.place.clone())
                };
                if let Some(src) = sources.iter().find(|x| x.stream == stream && x.place == place) {
                    for p in 0..src.count {
                        if src.backend.done(&src.stream, p, src.count, &[], &src.read[p as usize]).is_err() {
                            stop();
                        }
                    }
                }
            }
        }
        if idle {
            std::thread::sleep(std::time::Duration::from_millis(200));
        }
    }
}
