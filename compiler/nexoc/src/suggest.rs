//! `nexo test --suggest file.nexo`: test blocks with edge cases taken from
//! the types, the contracts and the unions of each function (section
//! "Goals and guarantees", D131). The same as nexo/suggest.py: the values
//! of each parameter, a probe program that runs every case, and an
//! `assert` (or a check of the error kind) for each result.

use nexo_diag::msg;

use crate::ast::*;

const INT_EDGES: &[&str] = &["0", "1", "-1", "9223372036854775807"];
const MARK: &str = "__nexo_case";

fn literal_expr(e: &Expr) -> bool {
    match &e.kind {
        ExprKind::Num { .. } | ExprKind::Unit { .. } | ExprKind::Bool(_) | ExprKind::None => true,
        ExprKind::Str(parts) => parts.iter().all(|p| matches!(p, StrPiece::Lit(_))),
        ExprKind::Unary { op, operand } => *op == "-" && matches!(operand.kind, ExprKind::Num { .. } | ExprKind::Unit { .. }),
        ExprKind::List(items) => items.iter().all(literal_expr),
        ExprKind::Map(pairs) => pairs.iter().all(|(k, v)| literal_expr(k) && literal_expr(v)),
        _ => false,
    }
}

/// A top-level statement the probe keeps: a definition or a constant.
fn kept(s: &Stmt) -> bool {
    match &s.kind {
        StmtKind::Fn(_) | StmtKind::Type(_) | StmtKind::Trait { .. } | StmtKind::Use { .. } => true,
        StmtKind::Assign { target, op, value, .. } => matches!(target.kind, ExprKind::Name(_)) && *op == "=" && literal_expr(value),
        StmtKind::VarDecl { value, .. } => literal_expr(value),
        _ => false,
    }
}

fn int_of(e: &Expr) -> Option<i64> {
    match &e.kind {
        ExprKind::Num { text, is_float: false } => text.replace('_', "").parse().ok(),
        ExprKind::Unary { op: "-", operand } => match &operand.kind {
            ExprKind::Num { text, is_float: false } => text.replace('_', "").parse::<i64>().ok().map(|v| -v),
            _ => None,
        },
        _ => None,
    }
}

/// (op, value) of each `expect param OP int` (written either way).
fn bounds(f: &FnDef, param: &str) -> Vec<(&'static str, i64)> {
    let flip = |op: &'static str| -> Option<&'static str> {
        Some(match op {
            "<" => ">",
            ">" => "<",
            "<=" => ">=",
            ">=" => "<=",
            "==" => "==",
            "!=" => "!=",
            _ => return None,
        })
    };
    let mut out = Vec::new();
    for s in &f.body {
        let StmtKind::Expect(e) = &s.kind else { continue };
        let ExprKind::Compare { ops, operands } = &e.kind else { continue };
        if ops.len() != 1 || flip(ops[0]).is_none() {
            continue;
        }
        let (left, right) = (&operands[0], &operands[1]);
        if matches!(&left.kind, ExprKind::Name(n) if n == param) {
            if let Some(v) = int_of(right) {
                out.push((ops[0], v));
            }
        } else if matches!(&right.kind, ExprKind::Name(n) if n == param) {
            if let Some(v) = int_of(left) {
                out.push((flip(ops[0]).unwrap(), v));
            }
        }
    }
    out
}

fn holds(value: i64, op: &str, bound: i64) -> bool {
    match op {
        "<" => value < bound,
        ">" => value > bound,
        "<=" => value <= bound,
        ">=" => value >= bound,
        "==" => value == bound,
        _ => value != bound,
    }
}

fn is_int_text(t: &str) -> bool {
    let d = t.strip_prefix('-').unwrap_or(t);
    !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit())
}

fn is_float_text(t: &str) -> bool {
    match t.split_once('.') {
        Some((a, b)) => is_int_text(a) && !b.is_empty() && b.bytes().all(|c| c.is_ascii_digit()),
        None => false,
    }
}

fn slice(lines: &[&str], span: nexo_diag::Span) -> String {
    let chars: Vec<char> = lines[span.line as usize - 1].chars().collect();
    chars[span.col as usize - 1..span.end_col as usize - 1].iter().collect()
}

fn push_new(v: &mut Vec<String>, x: String) {
    if !v.contains(&x) {
        v.push(x);
    }
}

fn candidates(t: &TypeExpr, lines: &[&str], enums: &[(String, Vec<String>)]) -> Option<Vec<String>> {
    match t {
        TypeExpr::Range { low, high, .. } => {
            let mut v = vec![low.to_string()];
            push_new(&mut v, high.to_string());
            Some(v)
        }
        TypeExpr::Union { literals, .. } => Some(literals.iter().map(|x| slice(lines, x.span)).collect()),
        TypeExpr::Name(tn) => {
            if tn.fallible || tn.weak || tn.rel.is_some() {
                return None;
            }
            let strs = |xs: &[&str]| xs.iter().map(|s| s.to_string()).collect::<Vec<String>>();
            let mut base = if tn.params.is_empty() {
                match tn.name.as_str() {
                    "int" => strs(INT_EDGES),
                    "float" => strs(&["0.0", "-1.5", "1.5"]),
                    "str" => strs(&["\"\"", "\"a b\""]),
                    "bool" => strs(&["true", "false"]),
                    "list" => strs(&["[]", "[0]"]),
                    n => enums.iter().find(|(e, _)| e == n)?.1.clone(),
                }
            } else if tn.name == "list" && tn.params.len() == 1 {
                let inner = candidates(&tn.params[0], lines, enums)?;
                vec!["[]".to_string(), format!("[{}]", inner.first()?)]
            } else {
                return None;
            };
            if tn.optional {
                base.push("none".to_string());
            }
            Some(base)
        }
    }
}

/// (function, the calls of its cases) for each function it can test.
pub fn calls_of(program: &Program, source: &str) -> Vec<(String, Vec<String>)> {
    let lines: Vec<&str> = source.split('\n').collect();
    let mut enums: Vec<(String, Vec<String>)> = program
        .body
        .iter()
        .filter_map(|s| match &s.kind {
            StmtKind::Type(t) if !t.variants.is_empty() && t.variants.iter().all(|v| v.params.is_empty()) => {
                Some((t.name.clone(), t.variants.iter().map(|v| v.name.clone()).collect()))
            }
            _ => None,
        })
        .collect();
    // a named union or range (`type Level = "low" | "high"`) gives its values
    for s in &program.body {
        if let StmtKind::Type(t) = &s.kind {
            if let Some(alias @ (TypeExpr::Union { .. } | TypeExpr::Range { .. })) = &t.alias {
                if let Some(values) = candidates(alias, &lines, &enums) {
                    enums.retain(|(n, _)| n != &t.name);
                    enums.push((t.name.clone(), values));
                }
            }
        }
    }
    let mut out = Vec::new();
    'fns: for s in &program.body {
        let StmtKind::Fn(f) = &s.kind else { continue };
        if f.params.is_empty() {
            continue;
        }
        let mut cands: Vec<Vec<String>> = Vec::new();
        let mut base: Vec<String> = Vec::new();
        for p in &f.params {
            let Some(t) = &p.ty else { continue 'fns };
            let Some(mut c) = candidates(t, &lines, &enums) else { continue 'fns };
            if c.is_empty() {
                continue 'fns;
            }
            let bs = if matches!(t, TypeExpr::Name(tn) if tn.name == "int") { bounds(f, &p.name) } else { Vec::new() };
            for &(_, b) in &bs {
                for v in [b.checked_sub(1), Some(b), b.checked_add(1)].into_iter().flatten() {
                    push_new(&mut c, v.to_string());
                }
            }
            let good = c.iter().find(|x| !is_int_text(x) || bs.iter().all(|&(op, b)| x.parse::<i64>().is_ok_and(|v| holds(v, op, b))));
            base.push(good.unwrap_or(&c[0]).clone());
            cands.push(c);
        }
        let mut calls = vec![base.clone()];
        for (i, c) in cands.iter().enumerate() {
            for x in c {
                if *x != base[i] {
                    let mut a = base.clone();
                    a[i] = x.clone();
                    calls.push(a);
                }
            }
        }
        let mut texts: Vec<String> = Vec::new();
        for a in calls {
            push_new(&mut texts, format!("{}({})", f.name, a.join(", ")));
        }
        out.push((f.name.clone(), texts));
    }
    out
}

pub fn probe_source(program: &Program, source: &str, calls: &[String]) -> String {
    let lines: Vec<&str> = source.split('\n').collect();
    let mut starts: Vec<u32> = program.body.iter().map(|s| s.span.line).collect();
    starts.sort();
    starts.dedup();
    let mut out: Vec<String> = Vec::new();
    for s in &program.body {
        if !kept(s) {
            continue;
        }
        let i = starts.iter().position(|&l| l == s.span.line).unwrap();
        let end = if i + 1 < starts.len() { starts[i + 1] as usize - 1 } else { lines.len() };
        let chunk = lines[s.span.line as usize - 1..end].join("\n");
        push_new(&mut out, chunk);
    }
    for (k, call) in calls.iter().enumerate() {
        out.push("try:".to_string());
        out.push(format!("    __s{k} = {call}"));
        out.push(format!("    print(\"\\n{MARK} {k} ok \" + convert(__s{k}, to: json))"));
        out.push("else e:".to_string());
        out.push(format!("    print(\"\\n{MARK} {k} err \" + e.kind)"));
    }
    out.join("\n") + "\n"
}

fn results(output: &str) -> Vec<(usize, String, String)> {
    let mut found = Vec::new();
    for line in output.split('\n') {
        let Some(rest) = line.strip_prefix(MARK).and_then(|r| r.strip_prefix(' ')) else { continue };
        let parts: Vec<&str> = rest.splitn(3, ' ').collect();
        if parts.len() == 3 && !parts[0].is_empty() && parts[0].bytes().all(|b| b.is_ascii_digit()) {
            if let Ok(k) = parts[0].parse() {
                found.push((k, parts[1].to_string(), parts[2].to_string()));
            }
        }
    }
    found
}

/// The Nexo literal of a JSON result, when it has a plain one.
fn nexo_literal(text: &str) -> Option<String> {
    if is_int_text(text) || is_float_text(text) || text == "true" || text == "false" {
        return Some(text.to_string());
    }
    if text == "null" {
        return Some("none".to_string());
    }
    if text.starts_with('"') {
        let crate::json::Json::Str(value) = crate::json::parse(text)? else { return None };
        if value.chars().any(|c| (c as u32) < 32 && !"\n\t\r".contains(c)) {
            return None;
        }
        let esc = value
            .replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('\n', "\\n")
            .replace('\t', "\\t")
            .replace('\r', "\\r")
            .replace('{', "{{");
        return Some(format!("\"{esc}\""));
    }
    None
}

fn render(name: &str, calls: &[String], res: &[(usize, String, String)], first: usize) -> Vec<String> {
    let mut body = vec![format!("test \"{name}: {}\":", msg("info.suggest_name", &[]))];
    let mut declared = false;
    for (k, call) in calls.iter().enumerate() {
        // the last result of a case wins, as in a map
        match res.iter().rev().find(|(i, _, _)| *i == first + k) {
            None => body.push(format!("    # {call}: {}", msg("info.suggest_no_result", &[]))),
            Some((_, kind, value)) if kind == "ok" => match nexo_literal(value) {
                Some(lit) => body.push(format!("    assert {call} == {lit}")),
                None => body.push(format!("    # {call}: {}", msg("info.suggest_result", &[("json", value.clone())]))),
            },
            Some((_, _, value)) => {
                body.push(if declared { "    kind = \"\"".to_string() } else { "    var kind = \"\"".to_string() });
                declared = true;
                body.push("    try:".to_string());
                body.push(format!("        {call}"));
                body.push("    else e:".to_string());
                body.push("        kind = e.kind".to_string());
                body.push(format!("    assert kind == \"{value}\""));
            }
        }
    }
    body
}

/// The suggested tests for a file; `run(probe_text)` runs a probe and
/// returns its output.
pub fn suggest(program: &Program, source: &str, path: &str, run: &dyn Fn(&str) -> String) -> String {
    let fns = calls_of(program, source);
    if fns.is_empty() {
        return msg("info.suggest_none", &[("path", path.to_string())]) + "\n";
    }
    let calls: Vec<String> = fns.iter().flat_map(|(_, cs)| cs.iter().cloned()).collect();
    let res = results(&run(&probe_source(program, source, &calls)));
    let mut out = vec![msg("info.suggest_header", &[]), String::new()];
    let mut first = 0;
    for (name, cs) in &fns {
        out.extend(render(name, cs, &res, first));
        out.push(String::new());
        first += cs.len();
    }
    out.pop();
    out.join("\n") + "\n"
}
