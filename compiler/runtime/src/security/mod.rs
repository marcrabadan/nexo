//! Shield's application API enforcement. This is not an OS sandbox.
pub(crate) mod matching;
pub mod audit;
pub mod sandbox;
pub mod context;
pub mod budget;
pub mod authorization;
pub mod online;
pub mod ingress;
pub mod ingress_permissions;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind { Read, Write, Net, Env, Shell, C, Python }
impl Kind {
    fn as_str(self) -> &'static str {
        match self { Self::Read => "read", Self::Write => "write", Self::Net => "net", Self::Env => "env",
            Self::Shell => "shell", Self::C => "c", Self::Python => "python" }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Capability<'a> {
    FileRead(&'a str),
    FileWrite(&'a str),
    NetworkConnect(&'a str), // D142 grants hosts, not ports or IP ranges.
    EnvironmentRead(&'a str),
    ProcessExecute(&'a str),
    NativeLibraryLoad(&'a str),
    PythonModuleLoad(&'a str),
}

impl<'a> Capability<'a> {
    pub fn from_boundary(kind: &str, value: &'a str) -> Option<Self> {
        Some(match kind {
            "read" => Self::FileRead(value), "write" => Self::FileWrite(value),
            "net" => Self::NetworkConnect(value), "env" => Self::EnvironmentRead(value),
            "shell" => Self::ProcessExecute(value), "c" => Self::NativeLibraryLoad(value),
            "python" => Self::PythonModuleLoad(value), _ => return None,
        })
    }
    pub fn kind(self) -> Kind {
        match self {
            Self::FileRead(_) => Kind::Read, Self::FileWrite(_) => Kind::Write, Self::NetworkConnect(_) => Kind::Net,
            Self::EnvironmentRead(_) => Kind::Env, Self::ProcessExecute(_) => Kind::Shell,
            Self::NativeLibraryLoad(_) => Kind::C, Self::PythonModuleLoad(_) => Kind::Python,
        }
    }
    fn target(self) -> &'a str {
        match self { Self::FileRead(v) | Self::FileWrite(v) | Self::NetworkConnect(v) |
            Self::EnvironmentRead(v) | Self::ProcessExecute(v) | Self::NativeLibraryLoad(v) |
            Self::PythonModuleLoad(v) => v }
    }
    pub fn operation(self) -> &'static str {
        match self { Self::FileRead(_) => "file.read", Self::FileWrite(_) => "file.write",
            Self::NetworkConnect(_) => "network.connect", Self::EnvironmentRead(_) => "environment.read",
            Self::ProcessExecute(_) => "process.execute", Self::NativeLibraryLoad(_) => "native.load",
            Self::PythonModuleLoad(_) => "python.load" }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Effect { Allow, Deny }
pub struct Rule { pub effect: Effect, pub kind: Kind, pub targets: Vec<String> }

#[derive(Debug, PartialEq, Eq)]
pub enum Decision<'a> { Allow, Warn, DenyMissing, DenyRule(Option<&'a str>), IsolationUnavailable, InvalidTarget }
impl Decision<'_> {
    pub fn denied(&self) -> bool { !matches!(self, Self::Allow | Self::Warn) }
    pub fn reason(&self) -> &'static str {
        match self { Self::Allow => "capability_granted", Self::Warn => "development_undeclared",
            Self::DenyMissing => "capability_missing", Self::DenyRule(_) => "explicit_deny",
            Self::IsolationUnavailable => "isolation_unavailable", Self::InvalidTarget => "invalid_target" }
    }
}

/// Immutable compiled rules; callers share the execution's engine across tasks.
pub struct Engine { rules: Vec<Rule>, resolved_paths: bool }
impl Engine {
    pub fn new(rules: Vec<Rule>) -> Self { Self { rules, resolved_paths: false } }
    pub(crate) fn with_resolved_paths(rules: Vec<Rule>) -> Self { Self { rules, resolved_paths: true } }
    pub fn evaluate(&self, capability: Capability<'_>, release: bool, shield: bool) -> Decision<'_> {
        let mut allowed = false;
        for rule in &self.rules {
            if rule.kind != capability.kind() { continue; }
            let target = rule.targets.iter().find(|t| if self.resolved_paths { matching::matches_resolved(rule.kind.as_str(), capability.target(), t) } else { matching::matches(rule.kind.as_str(), capability.target(), t) });
            if !rule.targets.is_empty() && target.is_none() { continue; }
            if rule.effect == Effect::Deny { return Decision::DenyRule(target.map(String::as_str)); }
            allowed = true;
        }
        if shield && matches!(capability, Capability::ProcessExecute(_) | Capability::NativeLibraryLoad(_) | Capability::PythonModuleLoad(_)) {
            return Decision::IsolationUnavailable;
        }
        if allowed { Decision::Allow }
        else if release || shield || matches!(capability, Capability::ProcessExecute(_)) { Decision::DenyMissing }
        else { Decision::Warn }
    }
}

/// Launcher configuration is trusted host input, never an authority granted by a model.
pub fn enforcing() -> bool { std::env::var_os("NEXO_SECURITY_ENFORCE").is_some_and(|v| v == "1") || std::env::var_os("NEXO_SECURITY_DEPENDENCIES").is_some() || std::env::var_os("NEXO_SECURITY_AUTHORIZATION").is_some() || std::env::var_os("NEXO_SECURITY_STREAMS").is_some() || std::env::var_os("NEXO_SECURITY_WEB").is_some() }

#[cfg(test)]
mod tests {
    use super::*;
    fn engine(entries: &[(&str, &str, &[&str])]) -> Engine {
        Engine::new(entries.iter().map(|(effect, kind, targets)| Rule {
            effect: if *effect == "allow" { Effect::Allow } else { Effect::Deny },
            kind: Capability::from_boundary(kind, "").unwrap().kind(),
            targets: targets.iter().map(|s| s.to_string()).collect(),
        }).collect())
    }
    #[test]
    fn deny_wins_in_both_orders_and_modes() {
        for entries in [vec![("allow", "net", &["*.example.com"][..]), ("deny", "net", &["private.example.com"][..])],
                        vec![("deny", "net", &["private.example.com"][..]), ("allow", "net", &["*.example.com"][..])]] {
            let e = engine(&entries);
            for release in [false, true] {
                assert!(matches!(e.evaluate(Capability::NetworkConnect("private.example.com"), release, false), Decision::DenyRule(_)));
                assert_eq!(e.evaluate(Capability::NetworkConnect("api.example.com"), release, false), Decision::Allow);
            }
            assert_eq!(e.evaluate(Capability::NetworkConnect("example.com"), true, false), Decision::DenyMissing);
            assert_eq!(e.evaluate(Capability::NetworkConnect("evil-example.com"), true, false), Decision::DenyMissing);
        }
    }
    #[test]
    fn development_compatibility_and_strict_default_deny() {
        let e = engine(&[]);
        for capability in [Capability::FileRead("x"), Capability::FileWrite("x"), Capability::NetworkConnect("x"), Capability::EnvironmentRead("x")] {
            assert_eq!(e.evaluate(capability, false, false), Decision::Warn);
            assert_eq!(e.evaluate(capability, true, false), Decision::DenyMissing);
            assert_eq!(e.evaluate(capability, false, true), Decision::DenyMissing);
        }
        assert_eq!(e.evaluate(Capability::ProcessExecute("x"), false, false), Decision::DenyMissing);
    }
    #[test]
    fn native_permissions_are_trust_not_confinement() {
        let e = engine(&[("allow", "c", &["libm"]), ("allow", "python", &["os"]), ("allow", "shell", &[])]);
        for cap in [Capability::NativeLibraryLoad("libm"), Capability::PythonModuleLoad("os"), Capability::ProcessExecute("anything")] {
            assert_eq!(e.evaluate(cap, true, false), Decision::Allow);
            assert_eq!(e.evaluate(cap, true, true), Decision::IsolationUnavailable);
        }
    }
    #[test]
    fn events_redact_every_untrusted_target() {
        for kind in ["read", "write", "net", "env", "shell", "c", "python"] {
            let cap = Capability::from_boundary(kind, "password\n\"sensitive-personal-data").unwrap();
            let text = audit::event_json(cap, &Decision::DenyMissing, 3);
            let parsed = crate::json::parse(&text).unwrap();
            assert_eq!(parsed.get("target").str(), Some("[redacted]"));
            assert_eq!(parsed.get("repeated").bool(), Some(true));
            assert!(!text.contains("password") && !text.contains("personal-data"));
        }
    }
    /// Local microbenchmark; no external network, no timing assertions.
    #[test]
    #[ignore]
    fn benchmark_decisions() {
        let e = engine(&[("allow", "net", &["api.example.com"])]);
        for host in ["api.example.com", "blocked.example.com"] {
            let now = std::time::Instant::now();
            for _ in 0..100_000 {
                std::hint::black_box(e.evaluate(Capability::NetworkConnect(std::hint::black_box(host)), true, false));
            }
            eprintln!("{host}: {:.1} ns/decision", now.elapsed().as_nanos() as f64 / 100_000.0);
        }
    }
}
