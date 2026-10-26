//! `nexo.sync` (section 5): channels, `spawn`, `select` and `shared`.
//!
//! Every task started with `spawn` runs on its own thread and works on
//! copies: its arguments, and every value that travels through a channel
//! or comes out of a `shared` variable, are copied (records included), so
//! no two tasks change the same value (D82). A spawned task cannot read the
//! program's variables that hold values others may change.
//!
//! All channels and the `shared` lock use one mutex and one condition
//! variable: a task that waits is counted, and when every task waits for a
//! channel (or for the lock) nobody can continue: the program ends with a
//! deadlock error instead of hanging.

use std::collections::VecDeque;
use std::sync::{Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use nexo_diag::Diagnostic;

use crate::cow::copy_records;
use crate::fmt::{repr, type_name};
use crate::pipe::Items;
use crate::state::{self, diag, R};
use crate::value::*;

pub struct Channel {
    /// `channel[T]`: the type of the values it carries
    pub elem: Option<Val>,
    /// how many values wait without a receiver (0: each send waits for one)
    pub size: usize,
    inner: Mut<Inner>,
}

#[derive(Default)]
struct Inner {
    queue: VecDeque<Val>,
    closed: bool,
    sent: u64,
    taken: u64,
}

/// What every task shares: who waits and where.
struct Sync {
    /// the main program and the spawned tasks still running
    alive: usize,
    /// tasks waiting for a channel or the lock (without a time limit)
    /// since the last change: any change wakes them all, and those that
    /// still cannot go on count themselves again
    blocked: usize,
    epoch: u64,
    /// where each waiting task waits: (task number, span); 0 is the main program
    waits: Vec<(u64, u32)>,
    next_task: u64,
    /// the `shared` lock: owner thread and how many times it took it
    owner: Option<std::thread::ThreadId>,
    depth: usize,
}

static SYNC: Mutex<Sync> =
    Mutex::new(Sync { alive: 1, blocked: 0, epoch: 0, waits: Vec::new(), next_task: 1, owner: None, depth: 0 });
static CV: Condvar = Condvar::new();

thread_local! {
    /// the number of this task (0: the main program and its parallel tasks)
    static TASK: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

fn lock() -> MutexGuard<'static, Sync> {
    SYNC.lock().unwrap_or_else(|e| e.into_inner())
}

/// Wait until something changes. A wait with a time limit always ends,
/// so it is not counted for deadlocks.
fn wait(mut g: MutexGuard<'static, Sync>, span: u32, until: Option<Instant>) -> MutexGuard<'static, Sync> {
    if let Some(t) = until {
        let left = t.saturating_duration_since(Instant::now());
        return CV.wait_timeout(g, left).unwrap_or_else(|e| e.into_inner()).0;
    }
    // a task of `parallel` waits on behalf of the task that started the
    // block, which is not waiting for a channel: it is not counted
    if crate::par::in_task() {
        return CV.wait(g).unwrap_or_else(|e| e.into_inner());
    }
    let me = TASK.with(|t| t.get());
    g.blocked += 1;
    g.waits.push((me, span));
    let epoch = g.epoch;
    deadlock_check(&g);
    let mut g = CV.wait(g).unwrap_or_else(|e| e.into_inner());
    if g.epoch == epoch {
        // woken without a change: no longer waiting
        g.blocked -= 1;
        if let Some(i) = g.waits.iter().position(|w| w.0 == me) {
            g.waits.remove(i);
        }
    }
    g
}

/// Something changed: every waiting task looks again.
fn changed(g: &mut Sync) {
    g.epoch += 1;
    g.blocked = 0;
    g.waits.clear();
    CV.notify_all();
}

/// Every task waits: report it where the main program waits, or else
/// where the first spawned task does, and end the program.
fn deadlock_check(g: &Sync) {
    if g.blocked < g.alive {
        return;
    }
    let mut waits = g.waits.clone();
    waits.sort();
    let span = waits.iter().find(|w| w.1 != 0).map(|w| w.1).unwrap_or(0);
    state::flush();
    diag(Diagnostic::new_spanless("deadlock"), span);
    state::report_pending();
    std::process::exit(1);
}

fn inner(c: &Channel) -> &mut Inner {
    c.inner.borrow_mut()
}

pub fn new_channel(elem: Option<Val>, size: V, span: u32) -> R<Val> {
    let size = match size.tag {
        T_MISSING | T_NONE => 0,
        _ => match size.as_int() {
            Some(n) if n >= 0 => n as usize,
            _ => {
                return Err(diag(
                    Diagnostic::new_spanless("type_mismatch")
                        .arg("name", "size")
                        .arg("type", "int")
                        .arg("value", repr(size.plain()))
                        .arg("vtype", type_name(size)),
                    span,
                ))
            }
        },
    };
    Ok(Val::new(Obj::Channel(Channel { elem, size, inner: Mut::new(Inner::default()) })))
}

/// The value a task receives is its own: its records are given the
/// task's stamp (they were copied when sent).
fn adopt(v: Val) -> Val {
    restamp(v.v());
    v
}

fn restamp(v: V) {
    match v.get() {
        Some(Obj::Record(r)) => {
            r.birth.set(state::stamp());
            for f in r.fields.borrow().iter() {
                if f.tag != T_WEAK {
                    restamp(f.v());
                }
            }
        }
        Some(Obj::List(items)) => items.borrow().iter().for_each(|x| restamp(x.v())),
        Some(Obj::Map(m)) => m.borrow().entries.iter().for_each(|(_, x)| restamp(x.v())),
        Some(Obj::VariantValue(vv)) => vv.fields.iter().for_each(|x| restamp(x.v())),
        _ => {}
    }
}

pub fn send(c: &Channel, value: V, span: u32) -> R<()> {
    if let Some(t) = &c.elem {
        if !crate::types::is_value(value, t.v(), span)? {
            return Err(diag(
                Diagnostic::new_spanless("type_mismatch")
                    .arg("name", "send")
                    .arg("type", repr(t.v()))
                    .arg("value", repr(value.plain()))
                    .arg("vtype", type_name(value)),
                span,
            ));
        }
    }
    let value = copy_records(crate::pipe::bind(value)?.v());
    let mut g = lock();
    if inner(c).closed {
        return Err(diag(Diagnostic::new_spanless("channel_closed"), span));
    }
    while c.size > 0 && inner(c).queue.len() >= c.size && !inner(c).closed {
        g = wait(g, span, None);
    }
    if inner(c).closed {
        return Err(diag(Diagnostic::new_spanless("channel_closed"), span));
    }
    let i = inner(c);
    i.queue.push_back(value);
    let ticket = i.sent;
    i.sent += 1;
    changed(&mut g);
    if c.size == 0 {
        // no room: wait until a receiver takes it
        while inner(c).taken <= ticket {
            g = wait(g, span, None);
        }
    }
    drop(g);
    Ok(())
}

/// The next value, or None when the channel is closed and empty.
pub fn receive(c: &Channel, span: u32) -> Option<Val> {
    let mut g = lock();
    loop {
        let i = inner(c);
        if let Some(v) = i.queue.pop_front() {
            i.taken += 1;
            changed(&mut g);
            drop(g);
            return Some(adopt(v));
        }
        if i.closed {
            return None;
        }
        g = wait(g, span, None);
    }
}

pub fn close(c: &Channel) {
    let mut g = lock();
    inner(c).closed = true;
    changed(&mut g);
}

/// `for x in c`: until the channel is closed and empty.
pub fn iterate(c: &'static Channel, owner: V, span: u32) -> Items {
    let owner = Val::from(owner);
    Box::new(std::iter::from_fn(move || {
        let _keep = &owner;
        receive(c, span).map(Ok)
    }))
}

pub fn call_method(c: &'static Channel, _obj: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    let want = if name == "send" { 1 } else { 0 };
    let params: &[(&str, bool)] = if name == "send" { &[("value", false)] } else { &[] };
    let bound = crate::call::bind_args(name, params, args, names, span)?;
    debug_assert_eq!(bound.len(), want);
    match name {
        "send" => {
            send(c, bound[0], span)?;
            Ok(Val::none())
        }
        "receive" => Ok(receive(c, span).unwrap_or(Val::none())),
        _ => {
            close(c);
            Ok(Val::none())
        }
    }
}

/// `select:` arms: the first channel (in order) with a value, `n` when the
/// time limit passes (or every channel is closed), -1 when there is nothing
/// to wait for.
pub fn select(chans: &[V], limit: V, out: &mut Val, span: u32) -> R<i64> {
    let mut channels: Vec<&'static Channel> = Vec::with_capacity(chans.len());
    for c in chans {
        match c.get() {
            Some(Obj::Channel(ch)) => channels.push(ch),
            _ => return Err(diag(Diagnostic::new_spanless("not_a_channel").arg("vtype", type_name(*c)), span)),
        }
    }
    let until = if limit.is_none() {
        None
    } else {
        Some(Instant::now() + duration(limit, span)?)
    };
    let mut g = lock();
    loop {
        let mut open = 0;
        for (k, c) in channels.iter().enumerate() {
            let i = inner(c);
            if let Some(v) = i.queue.pop_front() {
                i.taken += 1;
                changed(&mut g);
                drop(g);
                *out = adopt(v);
                return Ok(k as i64);
            }
            if !i.closed {
                open += 1;
            }
        }
        if open == 0 {
            return Ok(if until.is_some() { chans.len() as i64 } else { -1 });
        }
        if let Some(t) = until {
            if Instant::now() >= t {
                return Ok(chans.len() as i64);
            }
        }
        g = wait(g, span, until);
    }
}

fn duration(limit: V, span: u32) -> R<Duration> {
    match limit.get() {
        Some(Obj::Quantity(d, u)) if crate::num::unit_dimension(u).0 == "time" => {
            let secs = crate::num::unit_dimension(u).1.mul(d).to_f64();
            Ok(Duration::from_secs_f64(secs.max(0.0)))
        }
        _ => Err(diag(
            Diagnostic::new_spanless("type_mismatch")
                .arg("name", "after")
                .arg("type", "duration")
                .arg("value", repr(limit.plain()))
                .arg("vtype", type_name(limit)),
            span,
        )),
    }
}

// ------------------------------------------------------------------ spawn

/// Stack of a spawned task (deep recursion reaches the call limit first).
const TASK_STACK: usize = 256 << 20;

/// `spawn f(args)`: run the call on a new thread, on copies of the
/// arguments. An error nobody handles ends the program.
pub fn spawn(f: V, args: &[V], names: &[Option<&str>], span: u32) -> R<()> {
    let f = Val::from(f);
    let mut copies = Vec::with_capacity(args.len());
    for a in args {
        copies.push(copy_records(crate::pipe::bind(*a)?.v()));
    }
    let names: Vec<Option<String>> = names.iter().map(|n| n.map(String::from)).collect();
    let id = {
        let mut g = lock();
        g.alive += 1;
        let id = g.next_task;
        g.next_task += 1;
        id
    };
    let deadlines = state::with_task(|s| s.deadlines.clone());
    crate::value::going_multi();
    let started = std::thread::Builder::new().stack_size(TASK_STACK).spawn(move || {
        TASK.with(|t| t.set(id));
        state::set_stamp(crate::par::new_stamp());
        state::set_isolated(true);
        state::with_task(|s| s.deadlines = deadlines);
        state::set_deadline_active(state::with_task(|s| !s.deadlines.is_empty()));
        let args = copies.iter().map(|c| restamped(c)).collect::<Vec<V>>();
        let names: Vec<Option<&str>> = names.iter().map(|n| n.as_deref()).collect();
        let r = crate::call::call(f.v(), &args, &names, span);
        if state::failing() != 0 {
            state::report_pending();
            std::process::exit(1);
        }
        drop(r);
        drop(copies);
        drop(f);
        state::with_task(|s| s.deadlines.clear());
        state::set_deadline_active(false);
        crate::value::flush_pool();
        finished();
    });
    if started.is_err() {
        finished();
        return Err(diag(Diagnostic::new_spanless("spawn_failed"), span));
    }
    Ok(())
}

fn restamped(v: &Val) -> V {
    restamp(v.v());
    v.v()
}

fn finished() {
    let mut g = lock();
    g.alive -= 1;
    changed(&mut g);
    deadlock_check(&g);
}

/// The end of the main program: wait for the spawned tasks.
pub fn wait_all() {
    let mut g = lock();
    while g.alive > 1 {
        g = wait(g, 0, None);
    }
}

// ----------------------------------------------------------------- shared

/// The lock of `shared` variables: a statement that uses one holds it.
pub fn shared_lock() {
    let me = std::thread::current().id();
    let mut g = lock();
    while g.owner.is_some() && g.owner != Some(me) {
        g = wait(g, 0, None);
    }
    g.owner = Some(me);
    g.depth += 1;
}

pub fn shared_unlock() {
    let mut g = lock();
    g.depth -= 1;
    if g.depth == 0 {
        g.owner = None;
        changed(&mut g);
    }
}

/// A value read from a shared variable: a copy the task owns.
pub fn shared_read(v: V) -> Val {
    copy_records(v)
}

/// A value stored in a shared variable: a copy that only code holding
/// the lock reaches, so any task may change its records.
pub fn shared_store(v: V) -> Val {
    let c = copy_records(v);
    mark_shared(c.v());
    c
}

fn mark_shared(v: V) {
    match v.get() {
        Some(Obj::Record(r)) => {
            r.birth.set(u64::MAX);
            for f in r.fields.borrow().iter() {
                if f.tag != T_WEAK {
                    mark_shared(f.v());
                }
            }
        }
        Some(Obj::List(items)) => items.borrow().iter().for_each(|x| mark_shared(x.v())),
        Some(Obj::Map(m)) => m.borrow().entries.iter().for_each(|(_, x)| mark_shared(x.v())),
        Some(Obj::VariantValue(vv)) => vv.fields.iter().for_each(|x| mark_shared(x.v())),
        _ => {}
    }
}

/// Reading a variable of the program from a spawned task: only values
/// nobody can change (numbers, texts, functions, types, channels).
pub fn isolated_read(v: V, name: &str, is_var: bool, span: u32) -> R<()> {
    let immutable = match v.tag {
        T_SSTR => true,
        T_OBJ => matches!(
            v.get(),
            Some(
                Obj::Str(_)
                    | Obj::Decimal(_)
                    | Obj::Money(..)
                    | Obj::Quantity(..)
                    | Obj::Range(..)
                    | Obj::Variant(_)
                    | Obj::Func(_)
                    | Obj::Builtin(_)
                    | Obj::Type(_)
                    | Obj::Currency(_)
                    | Obj::Symbol(_)
                    | Obj::Namespace(_)
                    | Obj::Channel(_)
                    | Obj::ChannelType(_)
            )
        ),
        _ => true,
    };
    if is_var || !immutable {
        return Err(diag(Diagnostic::new_spanless("spawn_read").arg("name", name), span));
    }
    Ok(())
}
