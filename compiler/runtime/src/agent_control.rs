//! Inherited, authority-reducing cancellation and deadline admission scopes.
use crate::state::{failure, failure_as, R};
use crate::value::*;
use std::cell::RefCell;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::Instant;

#[derive(Clone, Default)]
pub(crate) struct Snapshot(pub(crate) Vec<Arc<Control>>);
thread_local! { static CURRENT: RefCell<Snapshot> = RefCell::new(Snapshot::default()); }

pub(crate) struct Control {
    closed: AtomicBool,
    pub(crate) deadline: Option<Instant>,
}
impl Control {
    pub(crate) fn new(deadline: Option<Instant>) -> Arc<Self> {
        Arc::new(Self {
            closed: AtomicBool::new(false),
            deadline,
        })
    }
    pub(crate) fn check(&self, span: u32) -> R<()> {
        if self.closed.load(Ordering::SeqCst) {
            return Err(failure("Cancelled", span, &[]));
        }
        if self.deadline.is_some_and(|limit| Instant::now() >= limit) {
            return Err(failure_as("Timeout", "Timeout.agent", span, &[]));
        }
        Ok(())
    }
}
pub(crate) struct Token(pub(crate) Arc<Control>);
impl Native for Token {
    fn type_name(&self) -> &str {
        "agent_control"
    }
    fn repr(&self) -> String {
        "agent_control".into()
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        match name {
            "cancel" => Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "cancel"))),
            "cancelled" => Ok(Val::bool(self.0.closed.load(Ordering::SeqCst))),
            _ => Err(crate::stdlib::missing_member(
                "agent_control",
                name,
                &["cancel", "cancelled"],
                span,
            )),
        }
    }
    fn call_method(
        &self,
        _me: V,
        name: &str,
        args: &[V],
        names: &[Option<&str>],
        span: u32,
    ) -> R<Val> {
        crate::stdlib::method_args(name, &[], 0, args, names, span)?;
        self.0.closed.store(true, Ordering::SeqCst);
        Ok(Val::none())
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}
pub(crate) fn capture() -> Snapshot {
    CURRENT.with(|value| value.borrow().clone())
}
pub(crate) struct Scope(Snapshot);
impl Drop for Scope {
    fn drop(&mut self) {
        CURRENT.with(|value| *value.borrow_mut() = self.0.clone());
    }
}
pub(crate) fn enter(snapshot: &Snapshot, replace: bool) -> Scope {
    CURRENT.with(|value| {
        let old = value.borrow().clone();
        if replace {
            *value.borrow_mut() = snapshot.clone();
        } else {
            value.borrow_mut().0.extend(snapshot.0.iter().cloned());
        }
        Scope(old)
    })
}
pub(crate) fn check(span: u32) -> R<()> {
    let snapshot = capture();
    for control in snapshot.0 {
        control.check(span)?;
    }
    Ok(())
}
