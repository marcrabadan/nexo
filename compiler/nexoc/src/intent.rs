//! `nexo check --intent [paths]`: does each function do what its `##`
//! comments and its contracts say? (section "Goals and guarantees", D132)
//! The same as nexo/intent.py: a probe program with the file's `ai` blocks
//! asks a language model about each function, with a typed answer.

use nexo_diag::{msg, Span};

use crate::ast::*;

const MARK: &str = "__nexo_intent";
const INSTRUCTION: &str = "You check one function of the Nexo language. Its ## comments and its expect and ensure \
contracts say what it should do. Answer agrees: true when the code does what they say, or false when it does not; \
reason says in one sentence what disagrees.";

pub fn nexo_string(text: &str) -> String {
    let esc = text
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\t', "\\t")
        .replace('\r', "\\r")
        .replace('{', "{{");
    format!("\"{esc}\"")
}

/// (statement, its lines) for each top-level statement.
fn chunks<'a>(program: &'a Program, lines: &[&'a str]) -> Vec<(&'a Stmt, Vec<&'a str>)> {
    let mut starts: Vec<u32> = program.body.iter().map(|s| s.span.line).collect();
    starts.sort();
    starts.dedup();
    program
        .body
        .iter()
        .map(|s| {
            let i = starts.iter().position(|&l| l == s.span.line).unwrap();
            let end = if i + 1 < starts.len() { starts[i + 1] as usize - 1 } else { lines.len() };
            (s, lines[s.span.line as usize - 1..end].to_vec())
        })
        .collect()
}

fn ai_block(s: &Stmt) -> bool {
    let StmtKind::Expr(e) = &s.kind else { return false };
    let ExprKind::Call { func, .. } = &e.kind else { return false };
    matches!(&func.kind, ExprKind::Name(n) if n == "__ai_config" || n == "__ai_models")
}

/// (name, line to report, text) of each function with something to check.
pub fn functions(program: &Program, source: &str) -> Vec<(String, u32, String)> {
    let lines: Vec<&str> = source.split('\n').collect();
    let mut out = Vec::new();
    for (s, mut chunk) in chunks(program, &lines) {
        let StmtKind::Fn(f) = &s.kind else { continue };
        let mut first = s.span.line;
        while first > 1 && lines[first as usize - 2].starts_with("##") {
            first -= 1;
        }
        let contracts = f.body.iter().any(|b| matches!(b.kind, StmtKind::Expect(_) | StmtKind::Ensure(_)));
        if first == s.span.line && !contracts {
            continue;
        }
        while chunk.last().is_some_and(|l| l.trim().is_empty()) {
            chunk.pop();
        }
        let mut all: Vec<&str> = lines[first as usize - 1..s.span.line as usize - 1].to_vec();
        all.extend(chunk);
        out.push((f.name.clone(), first, all.join("\n")));
    }
    out
}

pub fn probe_source(program: &Program, source: &str, fns: &[(String, u32, String)]) -> String {
    let lines: Vec<&str> = source.split('\n').collect();
    let mut out: Vec<String> = chunks(program, &lines).into_iter().filter(|(s, _)| ai_block(s)).map(|(_, c)| c.join("\n")).collect();
    out.extend(["type IntentCheck:".to_string(), "    agrees: bool".to_string(), "    reason: str".to_string()]);
    for (k, (_, _, text)) in fns.iter().enumerate() {
        out.push("try:".to_string());
        out.push(format!("    __i{k}: IntentCheck = ask({}, {})", nexo_string(INSTRUCTION), nexo_string(text)));
        out.push(format!(
            "    print(\"\\n{MARK} {k} \" + convert(__i{k}.agrees, to: json) + \" \" + __i{k}.reason.replace(\"\\n\", \" \"))"
        ));
        out.push("else e:".to_string());
        out.push(format!("    print(\"\\n{MARK} {k} error \" + e.message.replace(\"\\n\", \" \"))"));
    }
    out.join("\n") + "\n"
}

fn results(output: &str) -> Vec<(usize, String, String)> {
    let mut found = Vec::new();
    for line in output.split('\n') {
        let Some(rest) = line.strip_prefix(MARK).and_then(|r| r.strip_prefix(' ')) else { continue };
        let parts: Vec<&str> = rest.splitn(3, ' ').collect();
        if parts.len() >= 2 && !parts[0].is_empty() && parts[0].bytes().all(|b| b.is_ascii_digit()) {
            if let Ok(k) = parts[0].parse() {
                found.push((k, parts[1].to_string(), parts.get(2).map(|s| s.to_string()).unwrap_or_default()));
            }
        }
    }
    found
}

/// The warnings of a file and the error that stopped the questions.
pub fn check_intent(program: &Program, source: &str, path: &str, run: &dyn Fn(&str) -> String) -> (Vec<String>, Option<String>) {
    let fns = functions(program, source);
    if fns.is_empty() {
        return (Vec::new(), None);
    }
    let res = results(&run(&probe_source(program, source, &fns)));
    let lines: Vec<&str> = source.split('\n').collect();
    let (mut warnings, mut error) = (Vec::new(), None);
    for (k, (name, line, _)) in fns.iter().enumerate() {
        let (verdict, reason) = match res.iter().rev().find(|(i, _, _)| *i == k) {
            Some((_, v, r)) => (v.clone(), r.clone()),
            None => ("error".to_string(), msg("info.intent_no_answer", &[])),
        };
        if verdict == "error" {
            error.get_or_insert(reason);
        } else if verdict == "false" {
            let reason = reason.trim_end_matches('.').to_string();
            let text = format!(
                "{} {}",
                msg("intent.mismatch", &[("name", name.clone()), ("reason", reason)]),
                msg("fix.intent.mismatch", &[])
            );
            let len = lines[*line as usize - 1].chars().count() as u32;
            let span = Span { line: *line, col: 1, end_col: len + 1, file: 0 };
            warnings.push(nexo_diag::render_warning(path, source, span, &text));
        }
    }
    (warnings, error)
}
