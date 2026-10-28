//! The program policy (section 9, D142): what a program may read, write,
//! reach on the network, read from the environment, run as a shell
//! command, and load from C or Python. The parser turns `policy:` into
//! `__policy([[rule, kind, target...], ...])`, the first statement of the
//! program, and every access of the runtime goes through `check`.
//! Follows phase 0's policy.py.

use std::sync::Mutex;

use nexo_diag::{msg, Diagnostic};

use crate::state::{diag, R};
use crate::value::*;

struct Policy {
    engine: Option<crate::security::Engine>,
    release: bool,
    reported: Vec<(String, String)>,
}

static POLICY: Mutex<Policy> = Mutex::new(Policy { engine: None, release: false, reported: Vec::new() });

pub fn release_all() {
    let mut p = POLICY.lock().unwrap();
    p.engine = None;
    p.release = false;
    p.reported.clear();
}

pub fn policy(entries: V, release: Option<V>, span: u32) -> R<Val> {
    let mut rules = Vec::new();
    for e in crate::cfg::items(entries) {
        let words: Vec<String> = crate::cfg::items(e.v()).iter().map(|w| w.v().as_str().unwrap_or("").to_string()).collect();
        if words.len() >= 2 {
            let effect = match words[0].as_str() {
                "allow" => crate::security::Effect::Allow,
                "deny" => crate::security::Effect::Deny,
                _ => return Err(diag(Diagnostic::new_spanless("policy_internal"), span)),
            };
            let Some(capability) = crate::security::Capability::from_boundary(&words[1], "") else {
                return Err(diag(Diagnostic::new_spanless("policy_internal"), span));
            };
            rules.push(crate::security::Rule { effect, kind: capability.kind(), targets: words[2..].to_vec() });
        }
    }
    if crate::security::context::initialize(crate::state::program().file).is_err() {
        return Err(diag(Diagnostic::new_spanless("security_context_config"), span));
    }
    if crate::security::authorization::initialize(crate::state::program().file).is_err() {
        return Err(diag(Diagnostic::new_spanless("security_authorization"),span));
    }
    let mut p = POLICY.lock().unwrap();
    if p.engine.is_some() {
        return Err(diag(Diagnostic::new_spanless("policy_internal"), span));
    }
    p.engine = Some(crate::security::Engine::new(rules));
    p.release = release.and_then(|r| r.as_bool()).unwrap_or(false) || crate::security::enforcing();
    Ok(Val::none())
}

// ------------------------------------------------------------- matching

pub use crate::security::matching::shown;

// --------------------------------------------------------------- checks

fn rule_text(rule: &str, kind: &str, target: Option<&str>) -> String {
    match target {
        None => format!("{rule} {kind}"),
        Some(t) => format!("{rule} {kind} \"{t}\""),
    }
}

/// Stop the program, warn or record before an access (D142).
pub fn check(kind: &str, value: &str, span: u32) -> R<()> {
    let target = if kind == "read" || kind == "write" { shown(value) } else { value.to_string() };
    let what = msg(&format!("detail.policy_{kind}"), &[("target", target.clone())]);
    let allow_line = rule_text("allow", kind, if kind == "shell" { None } else { Some(&target) });
    let mut p = POLICY.lock().unwrap();
    let capability = crate::security::Capability::from_boundary(kind, value)
        .expect("runtime policy boundary must have a typed capability");
    if !p.release && !crate::security::enforcing() {
        if let Some(record) = std::env::var_os("NEXO_POLICY_RECORD").filter(|r| !r.is_empty()) {
            use std::io::Write;
            if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(record) {
                let _ = writeln!(f, "{kind}\t{}", if kind == "shell" { "" } else { &target });
            }
            return Ok(());
        }
    }
    let Some(engine) = &p.engine else {
        if crate::security::enforcing() {
            return Err(diag(Diagnostic::new_spanless("policy_missing"), span));
        }
        if kind == "shell" {
            return Err(diag(Diagnostic::new_spanless("policy_shell").arg("what", what), span));
        }
        return Ok(());
    };
    let decision = engine.evaluate(capability, p.release, crate::security::enforcing());
    if !matches!(decision, crate::security::Decision::Allow) { crate::security::audit::emit(capability, &decision); }
    match decision {
        crate::security::Decision::Allow => {
            let file = crate::state::span(span).map(|s| s.file).unwrap_or(0);
            if !crate::security::context::allows(capability, file) {
                crate::security::audit::emit(capability, &crate::security::Decision::DenyMissing);
                return Err(diag(Diagnostic::new_spanless("security_dependency"), span));
            }
            crate::security::audit::emit(capability, &decision);
            return Ok(());
        }
        crate::security::Decision::InvalidTarget => {
            return Err(diag(Diagnostic::new_spanless("security_url"), span));
        }
        crate::security::Decision::IsolationUnavailable => {
            return Err(diag(Diagnostic::new_spanless("security_isolation"), span));
        }
        crate::security::Decision::DenyRule(target) => {
            let d = Diagnostic::new_spanless("policy_deny").arg("what", what)
                .arg("rule", rule_text("deny", kind, target));
            return Err(diag(d, span));
        }
        crate::security::Decision::DenyMissing => {
            return Err(diag(Diagnostic::new_spanless("policy_denied").arg("what", what).arg("line", allow_line), span));
        }
        crate::security::Decision::Warn => {}
    }
    let key = (kind.to_string(), target);
    if p.reported.contains(&key) {
        return Ok(());
    }
    p.reported.push(key);
    drop(p);
    crate::state::flush();
    let d = Diagnostic::new_spanless("policy_undeclared").arg("what", what).arg("line", allow_line);
    let prog = crate::state::program();
    let text = match crate::state::span(span) {
        Some(s) => nexo_diag::render_warning(prog.file, prog.source, s, &d.text()),
        None => nexo_diag::render(prog.file, prog.source, None, &d.text()),
    };
    eprint!("{text}");
    Ok(())
}

pub fn check_url(url: &str, span: u32) -> R<()> {
    // The transport's WHATWG parser and lexical hostname extraction disagree
    // on backslashes and controls. Reject before authorization or any request.
    if url.chars().any(|c| c == '\\' || c.is_ascii_control()) {
        crate::security::audit::emit(crate::security::Capability::NetworkConnect(""), &crate::security::Decision::InvalidTarget);
        return Err(diag(Diagnostic::new_spanless("security_url"), span));
    }
    check("net", &crate::http::url_host(url), span)
}
