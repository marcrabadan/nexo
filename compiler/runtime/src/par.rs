//! `parallel for` and `parallel:` (section 5): the tasks run on several
//! threads, at most one per processor (or `max:`).
//!
//! What a program sees is the same as running the tasks one after the
//! other, in order (D52, D80): the results keep the order of the input;
//! what each task prints is written when the block ends, in task order; if
//! tasks fail, the error of the first one (in order) is the one that
//! propagates, and the output and results of the later tasks are dropped.
//! Tasks that have not started when one fails are not run.
//!
//! Each task starts with a new stamp: records and captured variables it
//! creates carry it, and a task may only change those (cow::check_write).

use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::Instant;

use crate::call::call_value;
use crate::state::{self, Fail, Pending, R};
use crate::value::*;

static STAMPS: AtomicU64 = AtomicU64::new(1);
/// threads working for `parallel` besides the ones that started the blocks
static EXTRA: AtomicUsize = AtomicUsize::new(0);

thread_local! {
    /// tasks of `parallel` running on this thread
    static IN_TASK: std::cell::Cell<u32> = const { std::cell::Cell::new(0) };
}

pub fn in_task() -> bool {
    IN_TASK.with(|t| t.get() > 0)
}

pub fn new_stamp() -> u64 {
    STAMPS.fetch_add(1, Ordering::Relaxed)
}

/// Stack of a task thread: deep recursion must reach the call limit (D73)
/// before the end of the stack.
const TASK_STACK: usize = 256 << 20;

fn processors() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

/// Take up to `want` extra threads from the budget.
fn reserve(want: usize) -> usize {
    let cap = processors().saturating_sub(1);
    let mut used = EXTRA.load(Ordering::Relaxed);
    loop {
        let take = want.min(cap.saturating_sub(used));
        if take == 0 {
            return 0;
        }
        match EXTRA.compare_exchange_weak(used, used + take, Ordering::AcqRel, Ordering::Relaxed) {
            Ok(_) => return take,
            Err(x) => used = x,
        }
    }
}

/// What a task inherits from the code that starts it.
struct Inherit {
    depth: u64,
    deadlines: Vec<(Instant, Val, u32)>,
}

struct Outcome {
    value: Val,
    output: String,
    failure: Option<Pending>,
}

/// Values that are only read while the tasks run (their counts are atomic).
struct Shared<T>(T);
unsafe impl<T> Send for Shared<T> {}
unsafe impl<T> Sync for Shared<T> {}

/// Run one task on this thread, keeping the thread's own state aside.
fn run_task(f: V, arg: Option<V>, from: &Inherit, span: u32) -> Outcome {
    let old_stamp = state::stamp();
    let old_depth = state::depth();
    state::set_stamp(new_stamp());
    state::set_depth(from.depth);
    let deadlines = from.deadlines.clone();
    let has_deadline = !deadlines.is_empty();
    let (old_buffer, old_deadlines) = state::with_task(|s| {
        (s.buffer.replace(String::new()), std::mem::replace(&mut s.deadlines, deadlines))
    });
    let was_active = has_deadline || !old_deadlines.is_empty();
    state::set_deadline_active(has_deadline);
    let args: Vec<V> = arg.into_iter().collect();
    IN_TASK.with(|t| t.set(t.get() + 1));
    let r = call_value(f, &args, span);
    IN_TASK.with(|t| t.set(t.get() - 1));
    let failure = if state::failing() != 0 { Some(state::take_pending()) } else { None };
    let value = r.unwrap_or(Val::none());
    let (output, mine) = state::with_task(|s| {
        (std::mem::replace(&mut s.buffer, old_buffer), std::mem::replace(&mut s.deadlines, old_deadlines))
    });
    drop(mine);
    state::set_deadline_active(was_active && state::with_task(|s| !s.deadlines.is_empty()));
    state::set_stamp(old_stamp);
    state::set_depth(old_depth);
    Outcome { value, output: output.unwrap_or_default(), failure }
}

/// Run every task; the results in order, or the first failure.
fn run(tasks: &[(V, Option<V>)], max: Option<usize>, span: u32) -> R<Vec<Val>> {
    let n = tasks.len();
    let from = Inherit {
        depth: state::depth(),
        deadlines: state::with_task(|s| s.deadlines.clone()),
    };
    let want = n.min(max.unwrap_or(usize::MAX)).min(processors());
    let extra = if want > 1 { reserve(want - 1) } else { 0 };
    let next = AtomicUsize::new(0);
    let first_failure = AtomicUsize::new(usize::MAX);
    let slots: Vec<Mutex<Option<Outcome>>> = (0..n).map(|_| Mutex::new(None)).collect();
    let shared = Shared((tasks, &from));
    let work = || loop {
        let i = next.fetch_add(1, Ordering::Relaxed);
        if i >= n || i > first_failure.load(Ordering::Acquire) {
            break;
        }
        let (tasks, from) = shared.0;
        let (f, arg) = tasks[i];
        let out = run_task(f, arg, from, span);
        if out.failure.is_some() {
            first_failure.fetch_min(i, Ordering::AcqRel);
        }
        *slots[i].lock().unwrap() = Some(out);
    };
    if extra == 0 {
        work();
    } else {
        crate::value::going_multi();
        std::thread::scope(|sc| {
            for _ in 0..extra {
                let w = &work;
                let spawned = std::thread::Builder::new().stack_size(TASK_STACK).spawn_scoped(sc, move || {
                    w();
                    crate::value::flush_pool();
                });
                if spawned.is_err() {
                    break;
                }
            }
            work();
        });
        EXTRA.fetch_sub(extra, Ordering::AcqRel);
    }
    // what the tasks did, in order
    let mut results = Vec::with_capacity(n);
    for slot in slots {
        let Some(out) = slot.into_inner().unwrap() else { break };
        if !out.output.is_empty() {
            state::write_out(&out.output);
        }
        if let Some(p) = out.failure {
            state::restore_pending(p);
            return Err(Fail);
        }
        results.push(out.value);
    }
    Ok(results)
}

/// `parallel for x in items, max: n:` with the body compiled as `task`.
pub fn parallel_for(items: V, task: V, max: V, span: u32) -> R<Val> {
    let items = crate::pipe::collect_vec(items, span)?;
    let tasks: Vec<(V, Option<V>)> = items.iter().map(|x| (task, Some(x.v()))).collect();
    let max = max.as_int().map(|m| m.max(1) as usize);
    Ok(list(run(&tasks, max, span)?))
}

/// `parallel:` with each statement compiled as a task.
pub fn parallel_block(tasks: &[V], span: u32) -> R<()> {
    let tasks: Vec<(V, Option<V>)> = tasks.iter().map(|t| (*t, None)).collect();
    run(&tasks, None, span)?;
    Ok(())
}

