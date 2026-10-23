//! `parallel for` and `parallel:` (section 5).

/// This task was cancelled because an earlier one failed: it raises
/// nothing and its result is not used.
pub fn cancelled() -> bool {
    false
}
