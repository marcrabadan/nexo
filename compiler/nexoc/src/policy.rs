//! `nexo policy show|suggest [file.nexo]` (section 9, D142), and what a
//! release build needs of the policy. Follows phase 0's stdlib/policy.py
//! and cli.py.

use nexo_diag::{msg, Diagnostic};

use crate::ast::*;
use crate::parser::{is_policy, POLICY_KINDS};

/// (line, rules) of the policy of a program, or None.
pub fn declared(program: &Program) -> Option<(u32, Vec<(String, String, Vec<String>)>)> {
    let s = program.body.iter().find(|s| is_policy(s))?;
    let StmtKind::Expr(Expr { kind: ExprKind::Call { args, .. }, .. }) = &s.kind else { return None };
    let ExprKind::List(entries) = &args.first()?.value.kind else { return None };
    let mut rules = Vec::new();
    for e in entries {
        let ExprKind::List(items) = &e.kind else { continue };
        let words: Vec<String> = items.iter().map(|i| crate::phase5::lit(i).unwrap_or_default()).collect();
        rules.push((words[0].clone(), words[1].clone(), words[2..].to_vec()));
    }
    Some((s.span.line, rules))
}

/// A release build passes `release = true` to the policy, so what it does
/// not allow stops the program (D142).
pub fn mark_release(program: &mut Program, id: u32) -> bool {
    let Some(s) = program.body.iter_mut().find(|s| is_policy(s)) else { return false };
    let StmtKind::Expr(Expr { kind: ExprKind::Call { args, .. }, .. }) = &mut s.kind else { return false };
    let span = args[0].span;
    args.push(Arg { span, name: None, value: Expr { id, span, kind: ExprKind::Bool(true) } });
    true
}

fn show_text(file: &str, program: &Program) -> String {
    let Some((line, rules)) = declared(program) else {
        return msg("info.policy_none", &[("file", file.to_string())]) + "\n";
    };
    let mut out = vec![msg("info.policy_header", &[("file", file.to_string()), ("line", line.to_string())])];
    for kind in POLICY_KINDS {
        let of = |rule: &str| -> Vec<String> {
            rules.iter().filter(|(r, k, _)| k == kind && r == rule).flat_map(|(_, _, ts)| ts.clone()).collect()
        };
        let value = if *kind == "shell" {
            let ok = rules.iter().any(|(r, k, _)| k == kind && r == "allow") && !rules.iter().any(|(r, k, _)| k == kind && r == "deny");
            msg(if ok { "info.policy_yes" } else { "info.policy_no" }, &[])
        } else {
            let (allow, deny) = (of("allow"), of("deny"));
            let mut v = if allow.is_empty() { msg("info.policy_nothing", &[]) } else { allow.join(", ") };
            if !deny.is_empty() {
                v = format!("{v} {}", msg("info.policy_except", &[("targets", deny.join(", "))]));
            }
            v
        };
        out.push(format!("  {kind:<8}{value}"));
    }
    out.push(msg("info.policy_footer", &[("file", file.to_string())]));
    out.join("\n") + "\n"
}

/// The minimal policy for the accesses a test run recorded.
fn suggest_text(lines: &str) -> String {
    let mut seen: Vec<(&str, Vec<String>)> = POLICY_KINDS.iter().map(|k| (*k, Vec::new())).collect();
    for line in lines.lines() {
        let (kind, target) = line.split_once('\t').unwrap_or((line, ""));
        if let Some((_, ts)) = seen.iter_mut().find(|(k, _)| *k == kind) {
            if !ts.iter().any(|t| t == target) {
                ts.push(target.to_string());
            }
        }
    }
    let mut out = vec!["policy:".to_string()];
    for (kind, mut targets) in seen {
        if targets.is_empty() {
            continue;
        }
        if kind == "shell" {
            out.push("    allow shell".into());
            continue;
        }
        targets.sort();
        let quoted: Vec<String> = targets.iter().map(|t| format!("\"{t}\"")).collect();
        out.push(format!("    allow {kind} {}", quoted.join(", ")));
    }
    if out.len() == 1 {
        out.push("    deny shell".into());
    }
    out.join("\n") + "\n"
}

pub fn run(args: &[String]) -> u8 {
    let command = args.first().map(String::as_str);
    if !matches!(command, Some("show") | Some("suggest")) || args.len() > 2 || args[1..].iter().any(|a| a.starts_with('-')) {
        eprintln!("{}", crate::usage());
        return 2;
    }
    let given = args.get(1).map(String::as_str);
    let show = command == Some("show");
    let Some(path) = crate::phase5::program_file(if show { "policy" } else { "test" }, given) else { return 1 };
    let source = match crate::read_source(&path) {
        Ok(s) => s,
        Err(e) => {
            eprint!("{e}");
            return 1;
        }
    };
    let program = match crate::parser::parse(&source) {
        Ok(p) => p,
        Err(d) => {
            eprint!("{}", nexo_diag::render(&path, &source, d.span, &d.text()));
            return 1;
        }
    };
    if show {
        print!("{}", show_text(&path, &program));
        return 0;
    }
    let error = |key: &'static str| {
        let _ = std::io::Write::flush(&mut std::io::stdout());
        eprint!("Error: {}\n", Diagnostic::new_spanless(key).arg("file", path.clone()).text());
        1
    };
    if !program.body.iter().any(|s| matches!(s.kind, StmtKind::Test { .. })) {
        return error("policy_no_tests");
    }
    let dir = std::env::temp_dir().join(format!("nexo-policy-{}", std::process::id()));
    let _ = std::fs::create_dir_all(&dir);
    let record = dir.join("accesses");
    let _ = std::fs::write(&record, "");
    let exe = std::env::current_exe().unwrap_or_else(|_| "nexo".into());
    let status = std::process::Command::new(exe)
        .args(["test", &path])
        .env("NEXO_POLICY_RECORD", &record)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status();
    let lines = std::fs::read_to_string(&record).unwrap_or_default();
    let _ = std::fs::remove_dir_all(&dir);
    if !status.is_ok_and(|s| s.success()) {
        return error("policy_tests_fail");
    }
    print!("{}", suggest_text(&lines));
    0
}
