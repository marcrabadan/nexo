//! Invocation token admission, inherited by provider work in tools/tasks.
use crate::{
    json::Json,
    state::{failure_as, R},
};
use std::{
    cell::RefCell,
    sync::{Arc, Mutex},
};
struct State {
    remaining: i64,
    pending: bool,
    backend: Option<Backend>,
}
pub struct Meter {
    limit: i64,
    state: Mutex<State>,
}
impl Meter {
    pub fn new(limit: i64) -> Arc<Self> {
        Arc::new(Self {
            limit,
            state: Mutex::new(State {
                remaining: limit,
                pending: false,
                backend: None,
            }),
        })
    }
}
#[derive(Clone)]
struct Backend {
    store: Arc<crate::agent_state::Store>,
    name: String,
    identity: String,
}
fn bad(span: u32) -> crate::state::Fail {
    crate::security::authorization::state_error(span)
}
fn load(s: &mut State, limit: i64, span: u32) -> R<()> {
    let Some(b) = s.backend.clone() else {
        return Ok(());
    };
    let j = b
        .store
        .read(&b.name)
        .map_err(|_| bad(span))?
        .ok_or_else(|| bad(span))?;
    let Json::Obj(fields) = &j else {
        return Err(bad(span));
    };
    let remaining = j
        .get("remaining")
        .int()
        .filter(|n| *n >= 0 && *n <= limit)
        .ok_or_else(|| bad(span))?;
    let pending = j.get("pending").bool().ok_or_else(|| bad(span))?;
    if fields.len() != 5
        || j.get("version").int() != Some(1)
        || j.get("identity").str() != Some(&b.identity)
        || j.get("limit").int() != Some(limit)
    {
        return Err(bad(span));
    }
    s.remaining = s.remaining.min(remaining);
    s.pending |= pending;
    Ok(())
}
fn save(s: &State, limit: i64, span: u32) -> R<()> {
    let Some(b) = &s.backend else { return Ok(()) };
    let j = crate::json::obj(vec![
        ("version", Json::Int(1)),
        ("identity", crate::json::s(b.identity.clone())),
        ("limit", Json::Int(limit)),
        ("remaining", Json::Int(s.remaining)),
        ("pending", Json::Bool(s.pending)),
    ]);
    b.store.write(&b.name, &j).map_err(|_| bad(span))
}
fn transaction<T>(meter: &Meter, span: u32, f: impl FnOnce(&mut State) -> R<T>) -> R<T> {
    let mut s = meter.state.lock().unwrap();
    let b = s.backend.clone();
    let _lock = b
        .as_ref()
        .map(|b| b.store.lock(&b.name).map_err(|_| bad(span)))
        .transpose()?;
    load(&mut s, meter.limit, span)?;
    f(&mut s)
}
pub fn attach(
    store: Arc<crate::agent_state::Store>,
    run: &str,
    identity: &str,
    allow_new: bool,
    span: u32,
) -> R<()> {
    for (index, meter) in capture().0.iter().enumerate() {
        let mut s = meter.state.lock().unwrap();
        if let Some(b) = s.backend.clone() {
            let _lock = b.store.lock(&b.name).map_err(|_| bad(span))?;
            load(&mut s, meter.limit, span)?;
            if s.pending {
                return Err(bad(span));
            }
            save(&s, meter.limit, span)?;
            continue;
        }
        if s.pending {
            return Err(bad(span));
        }
        let name = crate::agent_state::digest(&format!("{run}\ntokens\n{index}"));
        let _lock = store.lock(&name).map_err(|_| bad(span))?;
        let existed = store.read(&name).map_err(|_| bad(span))?.is_some();
        if !existed && !allow_new {
            return Err(bad(span));
        }
        s.backend = Some(Backend {
            store: store.clone(),
            name,
            identity: identity.into(),
        });
        if existed {
            load(&mut s, meter.limit, span)?;
        }
        if s.pending {
            return Err(bad(span));
        }
        save(&s, meter.limit, span)?;
    }
    Ok(())
}

#[derive(Clone, Default)]
pub struct Snapshot(pub Vec<Arc<Meter>>);
thread_local! {static CURRENT:RefCell<Snapshot>=RefCell::new(Snapshot::default());}
pub fn capture() -> Snapshot {
    CURRENT.with(|v| v.borrow().clone())
}
pub fn combine(snapshots: impl IntoIterator<Item = Snapshot>) -> Snapshot {
    let mut out = Snapshot::default();
    for snap in snapshots {
        for meter in snap.0 {
            if !out.0.iter().any(|m| Arc::ptr_eq(m, &meter)) {
                out.0.push(meter)
            }
        }
    }
    out
}
pub struct Scope(Snapshot);
impl Drop for Scope {
    fn drop(&mut self) {
        CURRENT.with(|v| *v.borrow_mut() = self.0.clone())
    }
}
pub fn enter(snapshot: &Snapshot, replace: bool) -> Scope {
    let old = capture();
    let next = if replace {
        snapshot.clone()
    } else {
        combine([old.clone(), snapshot.clone()])
    };
    CURRENT.with(|v| *v.borrow_mut() = next);
    Scope(old)
}
pub fn states() -> Json {
    Json::Arr(
        capture()
            .0
            .iter()
            .map(|m| {
                let v = m.state.lock().unwrap();
                Json::Arr(vec![
                    Json::Int(m.limit),
                    Json::Int(v.remaining),
                    Json::Bool(v.pending),
                ])
            })
            .collect(),
    )
}
pub fn restore(saved: &Json, span: u32) -> R<()> {
    let meters = capture();
    let bad = || crate::security::authorization::state_error(span);
    let Json::Arr(values) = saved else {
        return Err(bad());
    };
    if values.len() != meters.0.len() {
        return Err(bad());
    }
    let mut remaining = vec![];
    for (value, meter) in values.iter().zip(&meters.0) {
        let Json::Arr(v) = value else {
            return Err(bad());
        };
        if v.len() != 3 || v[0].int() != Some(meter.limit) || v[2].bool().is_none() {
            return Err(bad());
        }
        remaining.push(
            v[1].int()
                .filter(|n| *n >= 0 && *n <= meter.limit)
                .ok_or_else(bad)?,
        );
    }
    for (value, meter) in remaining.iter().zip(&meters.0) {
        let mut s = meter.state.lock().unwrap();
        s.remaining = s.remaining.min(*value);
    }
    Ok(())
}
pub struct Lease {
    meters: Vec<Arc<Meter>>,
    pub cap: i64,
    finished: bool,
}
impl Lease {
    pub fn abort(&mut self, span: u32) -> R<()> {
        if !self.finished {
            for m in &self.meters {
                transaction(m, span, |s| {
                    s.pending = false;
                    save(s, m.limit, span)
                })?;
            }
            self.finished = true;
        }
        Ok(())
    }
    pub fn finish(&mut self, usage: Option<[i64; 2]>, span: u32) -> R<()> {
        if self.finished {
            return Ok(());
        }
        let Some(usage) = usage else {
            return Err(failure_as("AIError", "AIError.budget_usage", span, &[]));
        };
        let total = usage[0] as u128 + usage[1] as u128;
        let mut exceeded = false;
        for m in &self.meters {
            transaction(m, span, |s| {
                exceeded |= total > s.remaining as u128;
                s.remaining = (s.remaining as u128).saturating_sub(total) as i64;
                s.pending = false;
                save(s, m.limit, span)
            })?;
        }
        self.finished = true;
        if exceeded {
            Err(failure_as("AIError", "AIError.tokens", span, &[]))
        } else {
            Ok(())
        }
    }
}

pub fn reserve(span: u32) -> R<Option<Lease>> {
    let meters = capture().0;
    if meters.is_empty() {
        return Ok(None);
    }
    let mut lease = Lease {
        meters: vec![],
        cap: i64::MAX,
        finished: false,
    };
    for meter in meters {
        let result = transaction(&meter, span, |s| {
            if s.pending || s.remaining <= 0 {
                return Err(failure_as("AIError", "AIError.tokens", span, &[]));
            }
            s.pending = true;
            let remaining = s.remaining;
            save(s, meter.limit, span)?;
            Ok(remaining)
        });
        match result {
            Ok(remaining) => {
                lease.cap = lease.cap.min(remaining);
                lease.meters.push(meter)
            }
            Err(error) => {
                lease.abort(span)?;
                return Err(error);
            }
        }
    }
    Ok(Some(lease))
}
