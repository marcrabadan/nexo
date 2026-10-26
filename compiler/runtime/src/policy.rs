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
    rules: Option<Vec<(String, String, Vec<String>)>>,
    release: bool,
    reported: Vec<(String, String)>,
}

static POLICY: Mutex<Policy> = Mutex::new(Policy { rules: None, release: false, reported: Vec::new() });

pub fn release_all() {
    let mut p = POLICY.lock().unwrap();
    p.rules = None;
    p.reported.clear();
}

pub fn policy(entries: V, release: Option<V>, _span: u32) -> R<Val> {
    let mut rules = Vec::new();
    for e in crate::cfg::items(entries) {
        let words: Vec<String> = crate::cfg::items(e.v()).iter().map(|w| w.v().as_str().unwrap_or("").to_string()).collect();
        if words.len() >= 2 {
            rules.push((words[0].clone(), words[1].clone(), words[2..].to_vec()));
        }
    }
    let mut p = POLICY.lock().unwrap();
    p.rules = Some(rules);
    p.release = release.and_then(|r| r.as_bool()).unwrap_or(false);
    Ok(Val::none())
}

// ------------------------------------------------------------- matching

/// The components of a path with `.` and `..` taken away.
fn parts(path: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for p in path.split('/') {
        match p {
            "" | "." => {}
            ".." => {
                if out.last().is_some_and(|l| l != "..") {
                    out.pop();
                } else if !path.starts_with('/') {
                    out.push(p.to_string());
                }
            }
            _ => out.push(p.to_string()),
        }
    }
    out
}

/// A path as the policy writes it: `data/../x.csv` is `x.csv`.
pub fn shown(path: &str) -> String {
    let p = parts(path).join("/");
    if path.starts_with('/') {
        format!("/{p}")
    } else if p.is_empty() {
        ".".into()
    } else {
        p
    }
}

/// The absolute path the system opens: each component in turn, a
/// symbolic link replaced by its target before the `..` after it, so
/// neither a link nor `link/..` can lead out of an allowed folder.
fn resolve(path: &str) -> String {
    let full = if path.starts_with('/') {
        path.to_string()
    } else {
        format!("{}/{path}", std::env::current_dir().map(|d| d.to_string_lossy().to_string()).unwrap_or_default())
    };
    let split = |p: &str| -> Vec<String> { p.split('/').filter(|c| !c.is_empty() && *c != ".").map(String::from).collect() };
    let mut todo: Vec<String> = split(&full);
    todo.reverse();
    let mut out: Vec<String> = Vec::new();
    let mut links = 0;
    while let Some(c) = todo.pop() {
        if c == ".." {
            out.pop();
            continue;
        }
        let here = format!("/{}", out.iter().chain(std::iter::once(&c)).cloned().collect::<Vec<_>>().join("/"));
        let is_link = std::fs::symlink_metadata(&here).is_ok_and(|m| m.file_type().is_symlink());
        if links < 40 && is_link {
            if let Ok(target) = std::fs::read_link(&here) {
                links += 1;
                let target = target.to_string_lossy().to_string();
                if target.starts_with('/') {
                    out.clear();
                }
                todo.extend(split(&target).into_iter().rev());
                continue;
            }
        }
        out.push(c);
    }
    format!("/{}", out.join("/"))
}

fn path_matches(path: &str, target: &str) -> bool {
    let (p, t) = (resolve(path), resolve(target));
    p == t || t == "/" || p.starts_with(&format!("{t}/"))
}

fn host_matches(host: &str, target: &str) -> bool {
    let t = target.to_lowercase();
    match t.strip_prefix('*') {
        Some(rest) if rest.starts_with('.') => host.ends_with(rest),
        _ => host == t,
    }
}

fn matches(kind: &str, value: &str, target: &str) -> bool {
    match kind {
        "read" | "write" => path_matches(value, target),
        "net" => host_matches(value, target),
        _ => value == target,
    }
}

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
    if let Some(record) = std::env::var_os("NEXO_POLICY_RECORD").filter(|r| !r.is_empty()) {
        use std::io::Write;
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(record) {
            let _ = writeln!(f, "{kind}\t{}", if kind == "shell" { "" } else { &target });
        }
        return Ok(());
    }
    let what = msg(&format!("detail.policy_{kind}"), &[("target", target.clone())]);
    let allow_line = rule_text("allow", kind, if kind == "shell" { None } else { Some(&target) });
    let mut p = POLICY.lock().unwrap();
    let Some(rules) = &p.rules else {
        if kind == "shell" {
            return Err(diag(Diagnostic::new_spanless("policy_shell").arg("what", what), span));
        }
        return Ok(());
    };
    let mut allowed = false;
    for (rule, k, targets) in rules {
        if k != kind {
            continue;
        }
        let hit = targets.is_empty() || targets.iter().any(|t| matches(kind, value, t));
        if hit && rule == "deny" {
            let denied = targets.iter().find(|t| matches(kind, value, t)).map(|s| s.as_str());
            let d = Diagnostic::new_spanless("policy_deny").arg("what", what).arg("rule", rule_text("deny", kind, denied));
            return Err(diag(d, span));
        }
        if hit {
            allowed = true;
        }
    }
    if allowed {
        return Ok(());
    }
    if p.release || kind == "shell" {
        return Err(diag(Diagnostic::new_spanless("policy_denied").arg("what", what).arg("line", allow_line), span));
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
    check("net", &crate::http::url_host(url), span)
}
