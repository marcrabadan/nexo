//! Scheduled tasks (section 4, D208): `every 6h:` and `every monday at
//! 08:00:`. The parser turns the block into a function and a call of
//! `__every(period, it, at: "08:00")` (D90), which keeps the task. After
//! the top level a program with tasks runs them when their time comes,
//! one at a time, next to its servers and its streams, until it is
//! stopped. Under `nexo test` no time passes: none runs.
use std::sync::Mutex;

use nexo_diag::{msg, Diagnostic};

use crate::call::call_value;
use crate::state::{diag, R};
use crate::value::*;

const DAYS: &[&str] = &["day", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

struct Task {
    /// milliseconds between two runs, or none for a time of day
    period: Option<i64>,
    /// its place in DAYS, for a time of day
    day: usize,
    /// minutes after midnight
    minutes: i64,
    func: Val,
    security: crate::security::context::Snapshot,
}

static TASKS: Mutex<Vec<Task>> = Mutex::new(Vec::new());

pub fn release_all() {
    let old = std::mem::take(&mut *TASKS.lock().unwrap());
    drop(old);
}

/// The minutes after midnight of "08:00".
fn clock_of(at: V, span: u32) -> R<i64> {
    let bad = || diag(Diagnostic::new_spanless("every_at"), span);
    let Some(t) = at.as_str() else { return Err(bad()) };
    let b = t.as_bytes();
    if b.len() != 5 || b[2] != b':' || ![0, 1, 3, 4].iter().all(|&i| b[i].is_ascii_digit()) {
        return Err(bad());
    }
    let (h, m) = (t[..2].parse::<i64>().unwrap_or(99), t[3..].parse::<i64>().unwrap_or(99));
    if h > 23 || m > 59 {
        return Err(bad());
    }
    Ok(h * 60 + m)
}

/// `__every(period, fn, at:)`
pub fn every(period: V, func: V, at: Option<V>, span: u32) -> R<Val> {
    crate::stream::require_mode(span)?;
    let at = at.filter(|a| !a.is_none() && a.tag != T_MISSING);
    let bad = || diag(Diagnostic::new_spanless("every_period"), span);
    let security = crate::security::context::capture();
    let task = if let Some(day) = period.as_str().and_then(|d| DAYS.iter().position(|x| *x == d)) {
        let Some(at) = at else {
            return Err(diag(Diagnostic::new_spanless("every_day").arg("day", DAYS[day].to_string()), span));
        };
        Task { period: None, day, minutes: clock_of(at, span)?, func: Val::from(func), security }
    } else {
        let ms = crate::time::duration_ms(period).filter(|ms| *ms > 0 && at.is_none()).ok_or_else(bad)?;
        Task { period: Some(ms), day: 0, minutes: 0, func: Val::from(func), security }
    };
    TASKS.lock().unwrap().push(task);
    Ok(Val::none())
}

pub fn scheduled() -> bool {
    !TASKS.lock().unwrap().is_empty()
}

/// When a task runs next, after a moment: a period is counted from the
/// start of the program, and turns that passed while a task ran are not
/// made up for.
fn due(t: &Task, start: i64, after: i64, zone: &str) -> i64 {
    match t.period {
        Some(p) => start + ((after - start).div_euclid(p) + 1) * p,
        None => crate::time::next_at(after, t.day as i64, t.minutes, zone),
    }
}

/// Runs each task when its time comes until the program is stopped.
/// False when a task stopped it with a diagnostic (already shown).
pub fn run_forever() -> bool {
    let count = TASKS.lock().unwrap().len();
    crate::state::flush();
    eprintln!("{}", msg("info.every_running", &[("count", count.to_string())]));
    let zone = crate::time::local_zone();
    let start = crate::time::now_ms();
    let mut turns: Vec<i64> = TASKS.lock().unwrap().iter().map(|t| due(t, start, start, &zone)).collect();
    loop {
        let n = (0..count).min_by_key(|i| turns[*i]).unwrap();
        let wait = turns[n] - crate::time::now_ms();
        if wait > 0 {
            std::thread::sleep(std::time::Duration::from_millis(wait.min(500) as u64));
            continue;
        }
        let (func, security) = {
            let tasks = TASKS.lock().unwrap();
            (tasks[n].func.clone(), tasks[n].security.clone())
        };
        let failed = {
            let _registration = crate::security::context::intersect(&security);
            call_value(func.v(), &[], 0).is_err()
        };
        if failed {
            // an error that leaves a task is shown and the next turn
            // comes; a mistake stops the program (D17)
            let mistake = crate::state::failing() == crate::state::DIAG;
            crate::state::report_pending();
            if mistake {
                return false;
            }
        }
        crate::state::flush();
        turns[n] = due(&TASKS.lock().unwrap()[n], start, crate::time::now_ms(), &zone);
    }
}
