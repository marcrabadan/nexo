//! The patterns of section 10 that have their own word (D143-D145):
//! `single name:` (one shared value with fields), `states name:` (a state
//! machine whose transitions are checked) and `protect(...)` (a circuit
//! breaker). The parser rewrites the three into calls of the builtins here.

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
