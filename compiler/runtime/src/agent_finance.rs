//! Cross-process debit/reservation ledger in the operator checkpoint store.
use crate::{
    agent_state::{digest, Store},
    dec::Dec,
    json::{obj, s, Json},
    state::R,
};
use std::sync::Arc;
fn bad(span: u32) -> crate::state::Fail {
    crate::security::authorization::state_error(span)
}
fn policy(b: &Json) -> String {
    let Json::Obj(prices) = b.get("prices") else {
        unreachable!()
    };
    let mut prices = prices.iter().collect::<Vec<_>>();
    prices.sort_by_key(|(model, _)| model.as_str());
    let prices = Json::Arr(
        prices
            .into_iter()
            .map(|(model, value)| Json::Arr(vec![s(model.clone()), value.clone()]))
            .collect(),
    );
    digest(
        &Json::Arr(vec![
            b.get("currency").clone(),
            b.get("limit").clone(),
            b.get("period").clone(),
            prices,
        ])
        .compact(),
    )
}

fn load(store: &Store, identity: &str, span: u32) -> R<Json> {
    let j = store.read("budget").map_err(|_| bad(span))?;
    let Some(j) = j else {
        return Ok(obj(vec![
            ("version", Json::Int(1)),
            ("policy", s(identity)),
            ("entries", Json::Arr(vec![])),
        ]));
    };
    let Json::Obj(fields) = &j else {
        return Err(bad(span));
    };
    if fields.len() != 3
        || j.get("version").int() != Some(1)
        || j.get("policy").str() != Some(identity)
    {
        return Err(bad(span));
    }
    let Json::Arr(entries) = j.get("entries") else {
        return Err(bad(span));
    };
    let mut ids = std::collections::HashSet::new();
    for entry in entries {
        let Json::Obj(fields) = entry else {
            return Err(bad(span));
        };
        let (Some(id), Some(day), Some(amount)) = (
            entry.get("id").str(),
            entry.get("day").str(),
            entry.get("amount").str(),
        ) else {
            return Err(bad(span));
        };
        if fields.len() != 4
            || id.len() != 32
            || !id
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
            || amount.len() > 100
            || amount.is_empty()
            || !amount.bytes().all(|c| c.is_ascii_digit() || c == b'.')
            || amount.bytes().filter(|c| *c == b'.').count() > 1
            || !ids.insert(id)
            || entry.get("pending").bool().is_none()
            || crate::time::parse_date(day).is_none()
            || day.starts_with("0000")
            || Dec::parse(amount).is_none_or(|n| n.cmp(&Dec::zero()) == std::cmp::Ordering::Less)
        {
            return Err(bad(span));
        }
    }
    Ok(j)
}
pub struct Ticket {
    store: Arc<Store>,
    identity: String,
    id: String,
    pub day: String,
    finished: bool,
}
pub fn reserve(store: Arc<Store>, day: &str, span: u32) -> R<Ticket> {
    let b = store.budget().ok_or_else(|| bad(span))?;
    let identity = policy(b);
    let _lock = store.lock("budget").map_err(|_| bad(span))?;
    let mut j = load(&store, &identity, span)?;
    let Json::Arr(entries) = j.get("entries") else {
        unreachable!()
    };
    let mut used = Dec::zero();
    for e in entries {
        let booked = e.get("day").str().unwrap();
        let same = if b.get("period").str() == Some("day") {
            booked == day
        } else {
            booked.get(..7) == day.get(..7)
        };
        if same {
            used = used.add(&Dec::parse(e.get("amount").str().unwrap()).unwrap());
        }
    }
    let limit = Dec::parse(b.get("limit").str().unwrap()).unwrap();
    let free = limit.sub(&used);
    if free.cmp(&Dec::zero()) != std::cmp::Ordering::Greater {
        return Err(crate::state::failure_as(
            "AIError",
            "AIError.budget_pending",
            span,
            &[
                (
                    "budget",
                    format!("{} {}", limit.py_str(), b.get("currency").str().unwrap()),
                ),
                ("period", b.get("period").str().unwrap().into()),
                ("spent", used.py_str()),
            ],
        ));
    }
    let mut bytes = [0; 16];
    getrandom::getrandom(&mut bytes).map_err(|_| bad(span))?;
    let id = bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
    let Some(Json::Arr(entries)) = j.get_mut("entries") else {
        unreachable!()
    };
    entries.push(obj(vec![
        ("id", s(id.clone())),
        ("day", s(day)),
        ("amount", s(free.fixed())),
        ("pending", Json::Bool(true)),
    ]));
    store.write("budget", &j).map_err(|_| bad(span))?;
    Ok(Ticket {
        store,
        identity,
        id,
        day: day.into(),
        finished: false,
    })
}
impl Ticket {
    pub fn settle(&mut self, actual: &Dec, span: u32) -> R<()> {
        if self.finished {
            return Ok(());
        }
        let _lock = self.store.lock("budget").map_err(|_| bad(span))?;
        let mut j = load(&self.store, &self.identity, span)?;
        let Some(Json::Arr(entries)) = j.get_mut("entries") else {
            unreachable!()
        };
        let entry = entries
            .iter_mut()
            .find(|e| e.get("id").str() == Some(&self.id))
            .ok_or_else(|| bad(span))?;
        if entry.get("pending").bool() != Some(true) {
            return Err(bad(span));
        }
        entry.set("amount", s(actual.fixed()));
        entry.set("pending", Json::Bool(false));
        self.store.write("budget", &j).map_err(|_| bad(span))?;
        self.finished = true;
        Ok(())
    }
}

#[derive(Clone, Default)]
pub struct Snapshot(pub Vec<Arc<Store>>);
thread_local! {static CURRENT:std::cell::RefCell<Snapshot>=std::cell::RefCell::new(Snapshot::default());}
pub fn capture() -> Snapshot {
    CURRENT.with(|v| v.borrow().clone())
}
pub fn combine(snapshots: impl IntoIterator<Item = Snapshot>) -> Snapshot {
    let mut out = Snapshot::default();
    for snapshot in snapshots {
        for store in snapshot.0 {
            if !out.0.iter().any(|s| s.directory() == store.directory()) {
                out.0.push(store);
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
pub fn inherited(span: u32) -> R<Option<Arc<Store>>> {
    let value = capture();
    if value.0.len() > 1 {
        return Err(bad(span));
    }
    Ok(value.0.first().cloned())
}
