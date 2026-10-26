//! Local JSON lines to stderr; never include user-controlled targets or payloads.
use std::sync::atomic::{AtomicU64, Ordering};
use super::{Capability, Decision};
static VIOLATIONS: AtomicU64 = AtomicU64::new(0);

pub fn emit(capability: Capability<'_>, decision: &Decision<'_>) {
    if std::env::var_os("NEXO_SECURITY_TRACE").is_none_or(|v| v != "1") { return; }
    let denied = decision.denied();
    let violations = if denied { VIOLATIONS.fetch_add(1, Ordering::Relaxed).saturating_add(1) }
        else { VIOLATIONS.load(Ordering::Relaxed) };
    eprintln!("{}", event_json(capability, decision, violations));
}

pub fn event_json(capability: Capability<'_>, decision: &Decision<'_>, violations: u64) -> String {
    let denied = decision.denied();
    format!(r#"{{"event":"security.policy.{}","severity":"{}","operation":"{}","target":"[redacted]","decision":"{}","reason":"{}","violations":{},"repeated":{}}}"#,
        if denied { "denied" } else { "decision" }, if denied { "high" } else { "info" },
        capability.operation(), if denied { "deny" } else if matches!(decision, Decision::Warn) { "warn" } else { "allow" },
        decision.reason(), violations, violations >= 3)
}
