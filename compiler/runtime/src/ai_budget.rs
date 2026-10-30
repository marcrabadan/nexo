//! Process-local AI reservations. Dropping a ticket never refunds unknown cost.
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};

use crate::dec::Dec;
use crate::state::R;

struct Hold {
    id: u64,
    scope: PathBuf,
    currency: String,
    day: String,
    amount: Dec,
}

#[derive(Default)]
pub(crate) struct Ledger {
    next: u64,
    holds: Vec<Hold>,
}

static LEDGER: OnceLock<Mutex<Ledger>> = OnceLock::new();

pub(crate) fn locked<T>(f: impl FnOnce(&mut Ledger) -> T) -> T {
    let mut ledger = LEDGER
        .get_or_init(|| Mutex::new(Ledger::default()))
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    f(&mut ledger)
}

impl Ledger {
    fn reserved(&self, scope: &PathBuf, currency: &str, day: &str, period: &str) -> Dec {
        self.holds
            .iter()
            .filter(|h| {
                h.scope == *scope
                    && h.currency == currency
                    && if period == "day" {
                        h.day == day
                    } else {
                        h.day.get(..7) == day.get(..7)
                    }
            })
            .fold(Dec::zero(), |total, h| total.add(&h.amount))
    }
}

pub(crate) fn inspect(
    scope: &PathBuf,
    currency: &str,
    day: &str,
    period: &str,
    history: impl FnOnce() -> R<Dec>,
) -> R<(Dec, Dec)> {
    locked(|ledger| Ok((history()?, ledger.reserved(scope, currency, day, period))))
}

pub(crate) enum Admission {
    Granted(Ticket),
    Full { spent: Dec, reserved: Dec },
}

pub(crate) fn reserve(
    scope: PathBuf,
    currency: &str,
    day: String,
    period: &str,
    limit: &Dec,
    history: impl FnOnce() -> R<Dec>,
) -> R<Admission> {
    locked(|ledger| {
        let spent = history()?;
        let reserved = ledger.reserved(&scope, currency, &day, period);
        let available = limit.sub(&spent).sub(&reserved);
        if available.cmp(&Dec::zero()) != std::cmp::Ordering::Greater || ledger.next == u64::MAX {
            return Ok(Admission::Full { spent, reserved });
        }
        ledger.next += 1;
        let id = ledger.next;
        ledger.holds.push(Hold {
            id,
            scope,
            currency: currency.into(),
            day: day.clone(),
            amount: available,
        });
        Ok(Admission::Granted(Ticket {
            id,
            day,
            finished: false,
        }))
    })
}

pub(crate) struct Ticket {
    id: u64,
    pub(crate) day: String,
    finished: bool,
}

impl Ticket {
    pub(crate) fn settle(&mut self, actual: Dec, write: impl FnOnce() -> bool) {
        locked(|ledger| {
            if self.finished {
                return;
            }
            if let Some(index) = ledger.holds.iter().position(|h| h.id == self.id) {
                if write() || actual.is_zero() {
                    ledger.holds.remove(index);
                } else {
                    ledger.holds[index].amount = actual;
                }
            }
            self.finished = true;
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_one_equal_budget_can_reserve_headroom() {
        let scope = PathBuf::from("/unit/ai-budget/concurrent");
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
        let threads: Vec<_> = (0..8)
            .map(|_| {
                let (scope, barrier) = (scope.clone(), barrier.clone());
                std::thread::spawn(move || {
                    barrier.wait();
                    matches!(
                        reserve(
                            scope,
                            "EUR",
                            "2026-10-30".into(),
                            "day",
                            &Dec::from_i64(1),
                            || Ok(Dec::zero())
                        )
                        .unwrap(),
                        Admission::Granted(_)
                    )
                })
            })
            .collect();
        assert_eq!(
            threads
                .into_iter()
                .map(|thread| thread.join().unwrap())
                .filter(|won| *won)
                .count(),
            1
        );
    }
    #[test]
    fn failed_receipt_write_keeps_actual_debit() {
        let path = PathBuf::from("/unit/ai-budget/write-failure");
        let Admission::Granted(mut ticket) = reserve(
            path.clone(),
            "EUR",
            "2026-10-30".into(),
            "day",
            &Dec::from_i64(10),
            || Ok(Dec::zero()),
        )
        .unwrap() else {
            panic!()
        };
        ticket.settle(Dec::from_i64(4), || false);
        let (_, held) = inspect(&path, "EUR", "2026-10-30", "day", || Ok(Dec::zero())).unwrap();
        assert_eq!(held.cmp(&Dec::from_i64(4)), std::cmp::Ordering::Equal);
        ticket.settle(Dec::zero(), || panic!("a second settlement must not write"));
        let (_, held) = inspect(&path, "EUR", "2026-10-30", "day", || Ok(Dec::zero())).unwrap();
        assert_eq!(held.cmp(&Dec::from_i64(4)), std::cmp::Ordering::Equal);
    }
    #[test]
    fn daily_and_monthly_policies_see_the_same_reservation() {
        let path = PathBuf::from("/unit/ai-budget/periods");
        let Admission::Granted(_ticket) = reserve(
            path.clone(),
            "EUR",
            "2026-10-30".into(),
            "day",
            &Dec::from_i64(1),
            || Ok(Dec::zero()),
        )
        .unwrap() else {
            panic!()
        };
        assert!(matches!(
            reserve(
                path.clone(),
                "EUR",
                "2026-10-30".into(),
                "month",
                &Dec::from_i64(1),
                || Ok(Dec::zero())
            )
            .unwrap(),
            Admission::Full { .. }
        ));
        assert!(matches!(
            reserve(
                path,
                "EUR",
                "2026-10-31".into(),
                "day",
                &Dec::from_i64(1),
                || Ok(Dec::zero())
            )
            .unwrap(),
            Admission::Granted(_)
        ));
    }
}
