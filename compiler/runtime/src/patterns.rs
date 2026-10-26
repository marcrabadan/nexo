//! The patterns of section 10 that have their own word (D143-D145,
//! D150-D151): `single name:` (one shared value with fields), `states
//! name:` (a state machine whose transitions are checked), `protect(...)`
//! (a circuit breaker), `action name(...):` with `undo()`, and `workflow
//! name(...):` (durable steps). The parser rewrites them into calls of the
//! builtins here.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::state::{diag, failure, failure_as, R};
use crate::stdlib::{expects, missing_member};
use crate::value::*;
use nexo_diag::Diagnostic;

fn texts(names: &[String]) -> String {
    names.iter().map(|n| format!("\"{n}\"")).collect::<Vec<_>>().join(", ")
}

/// `single config:` (D143): its fields are read and changed with
/// `config.port`; it has no others.
struct Single {
    name: String,
    fields: Mutex<Vec<(String, Val)>>,
}

impl Single {
    fn missing(&self, name: &str, span: u32) -> crate::state::Fail {
        let fields = self.fields.lock().unwrap();
        let names: Vec<&str> = fields.iter().map(|(n, _)| n.as_str()).collect();
        missing_member(&self.name, name, &names, span)
    }
}

impl Native for Single {
    fn type_name(&self) -> &str {
        "single"
    }
    fn repr(&self) -> String {
        let fields = self.fields.lock().unwrap();
        let inner: Vec<String> = fields.iter().map(|(n, v)| format!("{n}: {}", crate::fmt::repr(v.v()))).collect();
        format!("{}({})", self.name, inner.join(", "))
    }
    fn member(&self, _me: V, name: &str, span: u32) -> R<Val> {
        if let Some((_, v)) = self.fields.lock().unwrap().iter().find(|(n, _)| n == name) {
            return Ok(v.clone());
        }
        Err(self.missing(name, span))
    }
    fn set_member(&self, name: &str, value: V, span: u32) -> Option<R<()>> {
        let v = match crate::pipe::bind(value) {
            Ok(v) => v,
            Err(e) => return Some(Err(e)),
        };
        let mut fields = self.fields.lock().unwrap();
        if let Some(slot) = fields.iter_mut().find(|(n, _)| n == name) {
            let old = std::mem::replace(&mut slot.1, v);
            drop(fields);
            drop(old);
            return Some(Ok(()));
        }
        drop(fields);
        Some(Err(self.missing(name, span)))
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// `states order:` (D144): `order.state` is the current state,
/// `order.go("paid")` moves to another one and `order.can("paid")` says
/// whether it may.
struct States {
    name: String,
    names: Vec<String>,
    next: HashMap<String, Vec<String>>,
    current: Mutex<String>,
}

impl States {
    fn known(&self, args: &[V], names: &[Option<&str>], method: &str, span: u32) -> R<String> {
        let a = crate::stdlib::method_args(method, &["state"], 1, args, names, span)?;
        let Some(to) = a[0].as_str() else { return Err(expects(method, "str", a[0], span)) };
        if self.next.contains_key(to) {
            return Ok(to.to_string());
        }
        let d = match nexo_diag::suggest(to, self.names.iter().map(|s| s.as_str())) {
            Some(s) => Diagnostic::new_spanless("state_unknown")
                .fix("suggest")
                .arg("state", to)
                .arg("machine", &self.name)
                .arg("suggestion", s),
            None => Diagnostic::new_spanless("state_unknown")
                .arg("state", to)
                .arg("machine", &self.name)
                .arg("states", texts(&self.names)),
        };
        Err(diag(d, span))
    }
}

impl Native for States {
    fn type_name(&self) -> &str {
        "states"
    }
    fn repr(&self) -> String {
        format!("states({})", self.name)
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        Ok(match name {
            "state" => Val::str(self.current.lock().unwrap().clone()),
            "go" => Val::new(Obj::BuiltinMethod(Val::from(me), "go")),
            "can" => Val::new(Obj::BuiltinMethod(Val::from(me), "can")),
            _ => return Err(missing_member("states", name, &["state", "go", "can"], span)),
        })
    }
    fn call_method(&self, _me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        let to = self.known(args, names, name, span)?;
        let mut current = self.current.lock().unwrap();
        let next = &self.next[current.as_str()];
        let allowed = next.contains(&to);
        if name == "can" {
            return Ok(Val::bool(allowed));
        }
        if !allowed {
            let after = if next.is_empty() { "none".to_string() } else { texts(next) };
            let origin = current.clone();
            drop(current);
            return Err(failure_as(
                "InvalidValue",
                "InvalidValue.state",
                span,
                &[("machine", self.name.clone()), ("origin", origin), ("to", to), ("next", after)],
            ));
        }
        *current = to;
        Ok(Val::none())
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// `__single("config", port: 8080, ...)`
pub fn single(args: &[V], names: &[Option<&str>], _span: u32) -> R<Val> {
    let name = args[0].as_str().unwrap_or("").to_string();
    let mut fields = Vec::new();
    for (a, n) in args.iter().zip(names).skip(1) {
        fields.push((n.unwrap_or("").to_string(), crate::pipe::bind(*a)?));
    }
    Ok(native(Single { name, fields: Mutex::new(fields) }))
}

/// `__states("order", [["new", "paid"], ...])`
pub fn states(name: V, pairs: V) -> R<Val> {
    let mut names: Vec<String> = Vec::new();
    let mut next: HashMap<String, Vec<String>> = HashMap::new();
    for pair in crate::cfg::items(pairs) {
        let p = crate::cfg::items(pair.v());
        let a = p[0].v().as_str().unwrap_or("").to_string();
        let b = p[1].v().as_str().unwrap_or("").to_string();
        for n in [&a, &b] {
            if !names.contains(n) {
                names.push(n.clone());
                next.insert(n.clone(), Vec::new());
            }
        }
        let after = next.get_mut(&a).unwrap();
        if !after.contains(&b) {
            after.push(b);
        }
    }
    let current = Mutex::new(names.first().cloned().unwrap_or_default());
    Ok(native(States { name: name.as_str().unwrap_or("").to_string(), names, next, current }))
}

/// The state of one protected value: failures in a row and, while it is
/// open, the moment it may be tried again.
#[derive(Default)]
struct Circuit {
    fails: i64,
    until: Option<Instant>,
}

static CIRCUITS: Mutex<Option<HashMap<String, Circuit>>> = Mutex::new(None);

/// What `__protect` gives the end of the block.
struct Guard {
    key: String,
    fails: i64,
    wait: f64,
}

impl Native for Guard {
    fn type_name(&self) -> &str {
        "protect"
    }
    fn repr(&self) -> String {
        "protect".into()
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// `__protect(target, "target", fails: n, wait: d)`: fails with CircuitOpen
/// while the circuit of `target` is open.
pub fn protect(target: V, name: V, fails: V, wait: V, span: u32) -> R<Val> {
    let n = match fails.as_int() {
        Some(n) if n >= 1 && fails.as_bool().is_none() => n,
        _ => return Err(expects("fails", "int", fails, span)),
    };
    let limit = crate::http::seconds(wait, "wait", span)?;
    let key = crate::fmt::repr(target);
    let mut all = CIRCUITS.lock().unwrap();
    let c = all.get_or_insert_with(HashMap::new).entry(key.clone()).or_default();
    if matches!(c.until, Some(t) if Instant::now() < t) {
        let args = [
            ("name", name.as_str().unwrap_or("").to_string()),
            ("fails", c.fails.to_string()),
            ("wait", crate::fmt::show(wait)),
        ];
        drop(all);
        return Err(failure("CircuitOpen", span, &args));
    }
    Ok(native(Guard { key, fails: n, wait: limit }))
}

/// `__protect_end(guard, ok)`
pub fn protect_end(guard: V, ok: V) -> R<Val> {
    let Some(g) = guard.native::<Guard>() else { return Ok(Val::none()) };
    let mut all = CIRCUITS.lock().unwrap();
    let c = all.get_or_insert_with(HashMap::new).entry(g.key.clone()).or_default();
    if ok.as_bool() == Some(true) {
        *c = Circuit::default();
    } else {
        c.fails += 1;
        if c.fails >= g.fails {
            c.until = Some(Instant::now() + Duration::from_secs_f64(g.wait.max(0.0)));
        }
    }
    Ok(Val::none())
}

// -- actions (D150) -------------------------------------------------------

/// The actions done and not undone, last at the end: (undo function, arguments).
static DONE: Mutex<Vec<(Val, Vec<Val>)>> = Mutex::new(Vec::new());

/// `__action_done("name", __undo_name, [arguments])`
pub fn action_done(undo: V, args: V) -> R<Val> {
    let args = crate::cfg::items(args);
    DONE.lock().unwrap().push((Val::from(undo), args));
    Ok(Val::none())
}

/// `undo()`: undoes the last action done; false when there is none. An
/// undo that fails stays the last action, so it can be tried again.
pub fn undo(span: u32) -> R<Val> {
    let Some(entry) = DONE.lock().unwrap().pop() else { return Ok(Val::bool(false)) };
    let args: Vec<V> = entry.1.iter().map(|a| a.v()).collect();
    match crate::call::call_value(entry.0.v(), &args, span) {
        Ok(_) => {
            drop(entry);
            Ok(Val::bool(true))
        }
        Err(e) => {
            DONE.lock().unwrap().push(entry);
            Err(e)
        }
    }
}

/// End of the program (NEXO_LEAKCHECK): the actions still held.
pub fn release_all() {
    let old = std::mem::take(&mut *DONE.lock().unwrap());
    drop(old);
}

// -- workflows (D151) -----------------------------------------------------

const WORKFLOW_DIR: &str = ".nexo/workflows";

#[derive(Default)]
struct Run {
    done: i64,
    until: Option<f64>,
    /// kind, message and fix key of the error that stopped it
    error: Option<(String, String, Option<String>)>,
}

/// One run of a workflow: the steps done, the end of the wait under way
/// and, while its steps are undone, the error that stopped it. It is saved
/// in .nexo/workflows/ after every change.
struct Workflow {
    name: String,
    key: String,
    path: std::path::PathBuf,
    run: Mutex<Run>,
}

impl Native for Workflow {
    fn type_name(&self) -> &str {
        "workflow"
    }
    fn repr(&self) -> String {
        format!("workflow({})", self.name)
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

impl Workflow {
    fn save(&self, run: &Run, span: u32) -> R<()> {
        use crate::json::{obj, s, Json};
        let error = match &run.error {
            Some((kind, message, fix)) => obj(vec![
                ("kind", s(kind.as_str())),
                ("message", s(message.as_str())),
                ("fix", fix.as_ref().map(|f| s(f.as_str())).unwrap_or(Json::Null)),
            ]),
            None => Json::Null,
        };
        let data = obj(vec![
            ("workflow", s(self.name.as_str())),
            ("args", s(self.key.as_str())),
            ("done", Json::Int(run.done)),
            ("until", run.until.map(Json::Num).unwrap_or(Json::Null)),
            ("error", error),
        ]);
        // a new file put in place of the old one, so a stop half way through
        // a save keeps the last state saved
        let fresh = self.path.with_extension("tmp");
        let written = std::fs::create_dir_all(WORKFLOW_DIR)
            .and_then(|_| std::fs::write(&fresh, data.compact() + "\n"))
            .and_then(|_| std::fs::rename(&fresh, &self.path));
        written.map_err(|e| crate::data::os_failure(&self.path.display().to_string(), &e, span))
    }
}

fn workflow_of(wf: V) -> &'static Workflow {
    wf.native::<Workflow>().expect("a workflow")
}

/// `__workflow("name", [arguments])`: the saved run, or a new one.
pub fn workflow(name: V, args: V) -> R<Val> {
    use sha2::{Digest, Sha256};
    let name = name.as_str().unwrap_or("").to_string();
    let key = crate::fmt::repr(args);
    let digest = Sha256::digest(format!("{name}{key}").as_bytes());
    let hex: String = digest.iter().take(8).map(|b| format!("{b:02x}")).collect();
    let path = std::path::Path::new(WORKFLOW_DIR).join(format!("{name}-{hex}.json"));
    let mut run = Run::default();
    if let Some(data) = std::fs::read_to_string(&path).ok().and_then(|t| crate::json::parse(&t)) {
        use crate::json::Json;
        run.done = data.get("done").int().unwrap_or(0);
        run.until = match data.get("until") {
            Json::Num(f) => Some(*f),
            Json::Int(i) => Some(*i as f64),
            _ => None,
        };
        let e = data.get("error");
        if e.is_obj() {
            let text = |k: &str| e.get(k).str().unwrap_or("").to_string();
            run.error = Some((text("kind"), text("message"), e.get("fix").str().map(|f| f.to_string())));
        }
    }
    Ok(native(Workflow { name, key, path, run: Mutex::new(run) }))
}

/// A run whose undos stopped half way goes on with them.
pub fn workflow_resume(wf: V, span: u32) -> R<Val> {
    let run = workflow_of(wf).run.lock().unwrap();
    if let Some((kind, message, fix)) = &run.error {
        let (kind, message, fix) = (kind.clone(), message.clone(), fix.clone().unwrap_or_default());
        drop(run);
        return Err(crate::state::raise_error_with(&kind, message, Val::none(), span, &fix));
    }
    Ok(Val::none())
}

pub fn workflow_step(wf: V, n: V) -> R<Val> {
    Ok(Val::bool(workflow_of(wf).run.lock().unwrap().done < n.as_int().unwrap_or(0)))
}

pub fn workflow_done(wf: V, n: V, span: u32) -> R<Val> {
    let w = workflow_of(wf);
    let mut run = w.run.lock().unwrap();
    run.done = n.as_int().unwrap_or(0);
    run.until = None;
    w.save(&run, span)?;
    Ok(Val::none())
}

fn epoch() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// `__workflow_wait(wf, "cond", max)`: the condition is still false.
pub fn workflow_wait(wf: V, cond: V, limit: V, span: u32) -> R<Val> {
    let seconds = crate::http::seconds(limit, "max", span)?;
    let w = workflow_of(wf);
    let mut run = w.run.lock().unwrap();
    let now = epoch();
    let until = match run.until {
        Some(t) => t,
        None => {
            run.until = Some(now + seconds);
            w.save(&run, span)?;
            now + seconds
        }
    };
    drop(run);
    if now >= until {
        let args = [
            ("cond", cond.as_str().unwrap_or("").to_string()),
            ("workflow", w.name.clone()),
            ("limit", crate::fmt::show(limit)),
        ];
        return Err(failure_as("Timeout", "Timeout.wait", span, &args));
    }
    std::thread::sleep(Duration::from_secs_f64((until - now).min(1.0)));
    Ok(Val::none())
}

pub fn workflow_failed(wf: V, e: V, span: u32) -> R<Val> {
    let w = workflow_of(wf);
    let mut run = w.run.lock().unwrap();
    if run.error.is_none() {
        if let Some(Obj::Error(err)) = e.get() {
            run.error = Some((err.kind.clone(), err.message.clone(), err.fix.map(|f| f.to_string())));
            w.save(&run, span)?;
        }
    }
    Ok(Val::none())
}

pub fn workflow_undo(wf: V, n: V) -> R<Val> {
    Ok(Val::bool(workflow_of(wf).run.lock().unwrap().done >= n.as_int().unwrap_or(0)))
}

pub fn workflow_undone(wf: V, n: V, span: u32) -> R<Val> {
    let w = workflow_of(wf);
    let mut run = w.run.lock().unwrap();
    run.done = n.as_int().unwrap_or(0) - 1;
    w.save(&run, span)?;
    Ok(Val::none())
}

/// The run is over: its file goes (a file left would skip every step of
/// the next run), and the folders if they are empty.
pub fn workflow_end(wf: V, span: u32) -> R<Val> {
    let path = &workflow_of(wf).path;
    if let Err(e) = std::fs::remove_file(path) {
        if e.kind() != std::io::ErrorKind::NotFound {
            return Err(crate::data::os_failure(&path.display().to_string(), &e, span));
        }
    }
    if std::fs::remove_dir(WORKFLOW_DIR).is_ok() {
        let _ = std::fs::remove_dir(".nexo");
    }
    Ok(Val::none())
}
