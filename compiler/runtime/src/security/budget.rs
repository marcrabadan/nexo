//! Process-wide Shield admission budgets. Counters are never task-local or reset
//! by test/stream resets. These bound runtime resources, not general heap/CPU.
use std::sync::Mutex;
use crate::state::{diag, R};
use nexo_diag::Diagnostic;

#[derive(Clone, Copy)]
pub enum Resource { Tasks, Registrations, Channels, ChannelSlots, Keys, KeyBytes, WebServers, WebRequests }
impl Resource {
    fn index(self) -> usize { self as usize }
    fn limit(self) -> usize { match self {
        Self::Tasks => 4096, Self::Registrations => 1024, Self::Channels => 256,
        Self::ChannelSlots => 65536, Self::Keys => 4096, Self::KeyBytes => 1048576, Self::WebServers => 16, Self::WebRequests => 4096,
    } }
}
#[derive(Default)]
struct Budget { used: [usize; 8], threads: usize }
impl Budget {
    fn reserve_threads(&mut self, want: usize) -> usize {
        let granted = want.min(32usize.saturating_sub(self.threads));
        self.threads += granted;
        granted
    }
    fn charge(&mut self, requests: &[(Resource, usize)]) -> bool {
        let mut next = self.used;
        for &(resource, amount) in requests {
            let i = resource.index();
            let Some(total) = next[i].checked_add(amount) else { return false; };
            if total > resource.limit() { return false; }
            next[i] = total;
        }
        self.used = next;
        true
    }
}
static BUDGET: Mutex<Budget> = Mutex::new(Budget { used: [0; 8], threads: 0 });
pub fn charge(requests: &[(Resource, usize)], span: u32) -> R<()> {
    if !super::enforcing() { return Ok(()); }
    if BUDGET.lock().unwrap().charge(requests) { return Ok(()); }
    Err(diag(Diagnostic::new_spanless("security_job_budget"), span))
}

pub struct Threads { counted: usize, granted: usize }
impl Threads {
    pub fn granted(&self) -> usize { self.granted }
    /// Return reservations for workers the OS could not start.
    pub fn release_unused(&mut self, count: usize) {
        let release = count.min(self.granted);
        let counted = release.min(self.counted);
        BUDGET.lock().unwrap().threads -= counted;
        self.counted -= counted;
        self.granted -= release;
    }
}
impl Drop for Threads {
    fn drop(&mut self) { BUDGET.lock().unwrap().threads -= self.counted; }
}
/// Parallel work falls back to its calling thread when slots are exhausted.
pub fn threads(want: usize) -> Threads {
    if !super::enforcing() { return Threads { counted: 0, granted: want }; }
    let mut budget = BUDGET.lock().unwrap();
    let granted = budget.reserve_threads(want);
    Threads { counted: granted, granted }
}
pub fn spawn_thread(span: u32) -> R<Threads> {
    let permit = threads(1);
    if permit.granted() == 1 { return Ok(permit); }
    Err(diag(Diagnostic::new_spanless("security_job_budget"), span))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parallel_reservation_falls_back_and_completed_threads_release_slots() {
        let mut b = Budget::default();
        assert_eq!(b.reserve_threads(31), 31);
        assert_eq!(b.reserve_threads(100), 1);
        assert_eq!(b.reserve_threads(1), 0);
        b.threads -= 32;
        assert_eq!(b.reserve_threads(32), 32);
    }
    #[test]
    fn unused_thread_reservations_return_once_and_do_not_underflow() {
        BUDGET.lock().unwrap().threads += 4;
        let mut permit = Threads { counted: 4, granted: 4 };
        permit.release_unused(3);
        assert_eq!(permit.counted, 1);
        assert_eq!(permit.granted(), 1);
        permit.release_unused(10);
        assert_eq!(permit.counted, 0);
        assert_eq!(permit.granted(), 0);
        drop(permit);
    }
    #[test]
    fn charges_are_atomic_cumulative_and_overflow_safe() {
        let mut b = Budget::default();
        assert!(b.charge(&[(Resource::Channels, 255), (Resource::ChannelSlots, 65535)]));
        assert!(!b.charge(&[(Resource::Channels, 1), (Resource::ChannelSlots, 2)]));
        assert_eq!(b.used[Resource::Channels.index()], 255);
        assert!(b.charge(&[(Resource::Channels, 1), (Resource::ChannelSlots, 1)]));
        assert!(!b.charge(&[(Resource::Tasks, usize::MAX)]));
        assert!(b.charge(&[(Resource::Tasks, 4096)]));
        assert!(!b.charge(&[(Resource::Tasks, 1)]));
        assert!(!b.charge(&[(Resource::Keys, 1), (Resource::KeyBytes, 1048577)]));
        assert_eq!(b.used[Resource::Keys.index()], 0);
    }
}
