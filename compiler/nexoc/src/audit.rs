//! `nexo audit [paths]`: every use of code that Nexo does not check
//! (section "Goals and guarantees", D129). In this edition that is C: a
//! `use c` library and each call to one of its functions count as implicit
//! `unsafe` (section 18); `unsafe:` blocks belong to nexo.perf, which is
//! not part of it, so they are a compile error and never reach the audit.

use nexo_diag::{msg, Diagnostic};

use crate::ast::*;
use crate::lint::{walk_body, Node};

fn line_text(key: &str, args: &[(&str, String)]) -> String {
    format!("{} {}", msg(key, args), msg(&format!("fix.{key}"), &[]))
}

/// (line, column, text) of each unsafe use, in the order of the file; the
/// text ends with its fix.
pub fn audit_source(source: &str) -> Result<Vec<(u32, u32, String)>, Diagnostic> {
    let program = crate::parser::parse(source)?;
    let mut found = Vec::new();
    let mut bound: Vec<String> = Vec::new();
    walk_body(&program.body, &mut |n| {
        if let Node::S(s) = n {
            if let StmtKind::Use { lang: Some(lang), name, alias, .. } = &s.kind {
                if lang == "c" {
                    let mut shown = format!("use c \"{name}\"");
                    if let Some(a) = alias {
                        shown.push_str(&format!(" as {a}"));
                    }
                    found.push((s.span.line, s.span.col, line_text("audit.use_c", &[("use", shown)])));
                    bound.push(alias.clone().unwrap_or_else(|| name.clone()));
                }
            }
        }
    });
    walk_body(&program.body, &mut |n| {
        if let Node::E(e) = n {
            if let ExprKind::Call { func, .. } = &e.kind {
                if let ExprKind::Attr { obj, name, .. } = &func.kind {
                    if let ExprKind::Name(o) = &obj.kind {
                        if bound.contains(o) {
                            let call = format!("{o}.{name}");
                            found.push((e.span.line, e.span.col, line_text("audit.call_c", &[("call", call)])));
                        }
                    }
                }
            }
        }
    });
    found.sort();
    Ok(found)
}
