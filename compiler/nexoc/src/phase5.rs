//! Commands of phase 5 (D113): `nexo flags set` (D115); the commands of
//! nexo.infra, nexo.ci and privacy follow the same steps as `nexo db`.

use std::path::Path;

use nexo_diag::{suggest, Diagnostic};

use crate::ast::*;

/// (positional, options) or None when an option is unknown.
pub fn split_options(args: &[String], flags: &[&str], values: &[&str]) -> Option<(Vec<String>, Vec<(String, String)>)> {
    let (mut pos, mut opts) = (Vec::new(), Vec::new());
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        if values.contains(&a) && i + 1 < args.len() {
            opts.push((a.to_string(), args[i + 1].clone()));
            i += 2;
            continue;
        }
        if flags.contains(&a) {
            opts.push((a.to_string(), String::new()));
        } else if a.starts_with("--") {
            return None;
        } else {
            pos.push(a.to_string());
        }
        i += 1;
    }
    Some((pos, opts))
}

pub fn option<'a>(opts: &'a [(String, String)], name: &str) -> Option<&'a str> {
    opts.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
}

fn error_line(d: Diagnostic) -> String {
    format!("Error: {}\n", d.text())
}

/// The file a phase 5 command reads: the one given, or the only .nexo file
/// of the current directory that has a `word` block (D113).
pub fn program_file(word: &str, given: Option<&str>) -> Option<String> {
    if let Some(g) = given {
        return Some(g.to_string());
    }
    let mut found = Vec::new();
    if let Ok(dir) = std::fs::read_dir(".") {
        let mut names: Vec<String> =
            dir.filter_map(|e| e.ok()).map(|e| e.file_name().to_string_lossy().into_owned()).filter(|n| n.ends_with(".nexo")).collect();
        names.sort();
        for n in names {
            let Ok(text) = std::fs::read_to_string(Path::new(&n)) else { continue };
            let starts = text.lines().any(|l| {
                l.strip_prefix(word).is_some_and(|rest| !rest.starts_with(|c: char| c.is_alphanumeric() || c == '_'))
            });
            if starts {
                found.push(n);
            }
        }
    }
    if found.len() == 1 {
        return found.pop();
    }
    eprint!("{}", error_line(Diagnostic::new_spanless("which_file").arg("word", word)));
    None
}

/// The text of a literal Str node.
pub fn lit(e: &Expr) -> Option<String> {
    match &e.kind {
        ExprKind::Str(parts) => {
            let mut out = String::new();
            for p in parts {
                match p {
                    StrPiece::Lit(s) => out.push_str(s),
                    _ => return None,
                }
            }
            Some(out)
        }
        _ => None,
    }
}

/// The names the flags blocks of a program declare (only the parser is
/// needed).
pub fn declared_flags(body: &[Stmt]) -> Vec<String> {
    let mut out = Vec::new();
    for s in body {
        let StmtKind::Expr(Expr { kind: ExprKind::Call { func, args }, .. }) = &s.kind else { continue };
        if !matches!(&func.kind, ExprKind::Name(n) if n == "__flags") {
            continue;
        }
        let Some(ExprKind::List(entries)) = args.first().map(|a| &a.value.kind) else { continue };
        for entry in entries {
            let ExprKind::Call { args: eargs, .. } = &entry.kind else { continue };
            if eargs.len() < 2 || matches!(eargs[1].value.kind, ExprKind::None) {
                continue;
            }
            let ExprKind::List(phrases) = &eargs[0].value.kind else { continue };
            let Some(ExprKind::List(atoms)) = phrases.first().map(|p| &p.kind) else { continue };
            if atoms.len() == 1 {
                if let ExprKind::Str(parts) = &atoms[0].kind {
                    out.push(parts.iter().map(|p| match p {
                        StrPiece::Lit(s) => s.as_str(),
                        _ => "",
                    }).collect());
                }
            }
        }
    }
    out
}

/// Using a flag no `flags:` block declares is a compile error (section 22,
/// rule 2), when its name is a literal text.
pub fn check_flags(body: &[Stmt]) -> Result<(), Diagnostic> {
    let names = declared_flags(body);
    let mut uses: Vec<(String, nexo_diag::Span)> = Vec::new();
    fn visit(e: &Expr, uses: &mut Vec<(String, nexo_diag::Span)>) {
        if let ExprKind::Call { func, args } = &e.kind {
            if matches!(&func.kind, ExprKind::Name(n) if n == "flag") {
                if let Some(a) = args.first().filter(|a| a.name.is_none()) {
                    if let Some(t) = lit(&a.value) {
                        uses.push((t, a.value.span));
                    }
                }
            }
        }
        crate::parser::each_child(e, &mut |c| visit(c, uses));
    }
    crate::checker::walk_stmts(body, true, &mut |s| crate::parser::each_stmt_expr(s, &mut |e| visit(e, &mut uses)));
    for (name, span) in uses {
        if !names.contains(&name) {
            let d = match suggest(&name, names.iter().map(|s| s.as_str())) {
                Some(s) => Diagnostic::new("flag_unknown", span).fix("suggest").arg("name", name).arg("suggestion", s),
                None => Diagnostic::new("flag_unknown", span).arg("name", name),
            };
            return Err(d);
        }
    }
    Ok(())
}

/// `nexo flags set NAME VALUE [file.nexo] [--stage S]` (section 22).
pub fn run_flags(args: &[String]) -> u8 {
    let parsed = split_options(args.get(1..).unwrap_or(&[]), &[], &["--stage"]);
    let Some((pos, opts)) = parsed.filter(|(p, _)| args.first().map(|a| a.as_str()) == Some("set") && (p.len() == 2 || p.len() == 3)) else {
        eprintln!("{}", crate::usage());
        return 2;
    };
    let Some(path) = program_file("flags", pos.get(2).map(|s| s.as_str())) else { return 1 };
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
    let names = declared_flags(&program.body);
    let (name, value) = (&pos[0], &pos[1]);
    if !names.contains(name) {
        let d = match suggest(name, names.iter().map(|s| s.as_str())) {
            Some(s) => Diagnostic::new_spanless("flag_unknown").fix("suggest").arg("name", name.clone()).arg("suggestion", s),
            None => Diagnostic::new_spanless("flag_unknown").arg("name", name.clone()),
        };
        eprint!("{}", error_line(d));
        return 1;
    }
    if !nexo_runtime::flags::valid_text(value) {
        eprint!("{}", error_line(Diagnostic::new_spanless("flag_set_value").arg("value", value.clone())));
        return 1;
    }
    let stage = option(&opts, "--stage").unwrap_or("default").to_string();
    nexo_runtime::flags::set_store(name, value, &stage);
    println!("{}", nexo_diag::msg("info.flags_set", &[("name", name.clone()), ("value", value.clone()), ("stage", stage)]));
    0
}

/// Types with @personal or @private fields and the settings they lack
/// (section 23, rule 1): (span of the type's name, type, missing keys).
pub fn privacy_gaps(body: &[Stmt]) -> Vec<(nexo_diag::Span, String, Vec<&'static str>)> {
    let mut out = Vec::new();
    for s in body {
        let StmtKind::Expr(Expr { kind: ExprKind::Call { func, args }, .. }) = &s.kind else { continue };
        if !matches!(&func.kind, ExprKind::Name(n) if n == "__privacy_type") || args.len() != 3 {
            continue;
        }
        let ExprKind::Name(tname) = &args[0].value.kind else { continue };
        if matches!(&args[2].value.kind, ExprKind::Map(p) if p.is_empty()) {
            continue;
        }
        let mut keys = Vec::new();
        if let ExprKind::List(entries) = &args[1].value.kind {
            for entry in entries {
                let ExprKind::Call { args: eargs, .. } = &entry.kind else { continue };
                let ExprKind::List(phrases) = &eargs[0].value.kind else { continue };
                if let Some(ExprKind::List(atoms)) = phrases.first().map(|p| &p.kind) {
                    if let [a] = atoms.as_slice() {
                        if let Some(t) = lit(a) {
                            keys.push(t);
                        }
                    }
                }
            }
        }
        let missing: Vec<&'static str> = ["keep", "purpose"].into_iter().filter(|k| !keys.iter().any(|x| x == k)).collect();
        if !missing.is_empty() {
            out.push((args[0].value.span, tname.clone(), missing));
        }
    }
    out
}

/// `nexo privacy report [file.nexo]` (section 23, rule 4): the types of the
/// program are compiled alone, as for `nexo db diff`, and the runtime
/// writes the report.
pub fn privacy_report(args: &[String]) -> u8 {
    if args.first().map(|a| a.as_str()) != Some("report") || args.len() > 2 || args.iter().any(|a| a.starts_with('-')) {
        eprintln!("{}", crate::usage());
        return 2;
    }
    let Some(path) = program_file_matching("@personal", args.get(1).map(|s| s.as_str()), &["@personal", "@private"]) else { return 1 };
    let readers = match std::fs::read_to_string(&path).ok().and_then(|src| crate::parser::parse(&src).ok()) {
        Some(p) => readers_of_data(&p.body),
        None => Vec::new(),
    };
    crate::module_command(&path, &[("NEXO_PRIVACY_FILE", path.clone()), ("NEXO_PRIVACY_READERS", readers.join("\n"))], &["__db_table", "__privacy_type"])
}

/// The identities of infra blocks that can read a database: `can read
/// database customers` (section 11).
pub fn readers_of_data(body: &[Stmt]) -> Vec<String> {
    fn words(phrase: &Expr) -> Vec<Option<String>> {
        match &phrase.kind {
            ExprKind::List(items) => items.iter().map(lit).collect(),
            _ => Vec::new(),
        }
    }
    fn head(entry: &Expr) -> (Vec<Option<String>>, Option<&Expr>) {
        let ExprKind::Call { args, .. } = &entry.kind else { return (Vec::new(), None) };
        let w = match &args[0].value.kind {
            ExprKind::List(ph) => ph.first().map(words).unwrap_or_default(),
            _ => Vec::new(),
        };
        (w, args.get(3).map(|a| &a.value))
    }
    let mut out = Vec::new();
    for s in body {
        let StmtKind::Expr(Expr { kind: ExprKind::Call { func, args }, .. }) = &s.kind else { continue };
        if !matches!(&func.kind, ExprKind::Name(n) if n == "__infra") || args.len() != 2 {
            continue;
        }
        let infra = lit(&args[0].value).unwrap_or_default();
        let ExprKind::List(entries) = &args[1].value.kind else { continue };
        for entry in entries {
            let (h, body) = head(entry);
            let Some(Expr { kind: ExprKind::List(lines), .. }) = body else { continue };
            if h.len() != 2 || h[0].as_deref() != Some("identity") {
                continue;
            }
            for line in lines {
                let (w, _) = head(line);
                if w.len() == 4 && w[0].as_deref() == Some("can") && w[1].as_deref() == Some("read") && w[2].as_deref() == Some("database") {
                    out.push(nexo_diag::msg(
                        "info.privacy_reader",
                        &[
                            ("identity", h[1].clone().unwrap_or_default()),
                            ("infra", infra.clone()),
                            ("database", w[3].clone().unwrap_or_default()),
                        ],
                    ));
                }
            }
        }
    }
    out
}

/// Like `program_file`, for a file that holds any of `needles`.
pub fn program_file_matching(word: &str, given: Option<&str>, needles: &[&str]) -> Option<String> {
    if let Some(g) = given {
        return Some(g.to_string());
    }
    let mut found = Vec::new();
    if let Ok(dir) = std::fs::read_dir(".") {
        let mut names: Vec<String> =
            dir.filter_map(|e| e.ok()).map(|e| e.file_name().to_string_lossy().into_owned()).filter(|n| n.ends_with(".nexo")).collect();
        names.sort();
        for n in names {
            let Ok(text) = std::fs::read_to_string(&n) else { continue };
            let hit = needles.iter().any(|w| {
                text.match_indices(w).any(|(i, _)| !text[i + w.len()..].starts_with(|c: char| c.is_alphanumeric() || c == '_'))
            });
            if hit {
                found.push(n);
            }
        }
    }
    if found.len() == 1 {
        return found.pop();
    }
    eprint!("{}", error_line(Diagnostic::new_spanless("which_file").arg("word", word)));
    None
}
