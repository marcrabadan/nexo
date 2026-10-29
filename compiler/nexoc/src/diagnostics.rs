//! Version 1 check diagnostics, sharing the LSP representation (D181).
use crate::json::{n, obj, s, Json};

fn problem(code: &str, message: &str, file: Json) -> Json {
    obj(vec![
        ("file", file),
        ("range", Json::Null),
        ("severity", n(1)),
        ("source", s("nexo")),
        ("code", s(code)),
        ("message", s(message)),
        ("data", obj(vec![("fixes", Json::Arr(vec![]))])),
    ])
}

pub(crate) fn run_check_json(args: &[String]) -> u8 {
    let effects = args.iter().any(|a| a == "--effects");
    let json_mode = args.iter().any(|a| a == "--json");
    let strict = args.iter().any(|a| a == "--require-complete");
    let mut paths = vec![];
    let mut options = std::collections::BTreeMap::new();
    let mut invalid = false;
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        if a == "--dependencies" || a == "--authorization" {
            if !effects
                || options.contains_key(a)
                || i + 1 == args.len()
                || args[i + 1].starts_with('-')
            {
                invalid = true;
                break;
            }
            options.insert(a, args[i + 1].as_str());
            i += 2;
            continue;
        }
        if !["--effects", "--json", "--require-complete"].contains(&a) {
            paths.push(args[i].clone());
        }
        i += 1;
    }
    invalid |= args.iter().filter(|a| *a == "--require-complete").count() > 1 || strict && !effects;
    let mut out = Vec::new();
    let mut status = 0;
    if invalid
        || paths.iter().any(|a| a.starts_with('-'))
        || args.iter().filter(|a| *a == "--json").count() > 1
        || args.iter().filter(|a| *a == "--effects").count() > 1
        || !(effects || json_mode)
    {
        out.push(problem(
            "check_usage",
            "Usage: nexo check --json [paths...]",
            Json::Null,
        ));
        status = 2;
    } else {
        for f in crate::test_files(&paths) {
            // Match Python Path's lexical display normalization, without
            // resolving symlinks or changing the path used for reading.
            let shown: std::path::PathBuf = f
                .components()
                .filter(|c| *c != std::path::Component::CurDir)
                .collect();
            let name = shown.to_string_lossy().into_owned();
            let source = match std::fs::read_to_string(&f) {
                Ok(source) => source,
                Err(_) => {
                    out.push(problem(
                        "check_io",
                        "Cannot read source as UTF-8.",
                        s(&name),
                    ));
                    status = 1;
                    continue;
                }
            };
            let checked = if effects {
                crate::effects::analyze(
                    &source,
                    &name,
                    options.get("--dependencies").copied(),
                    options.get("--authorization").copied(),
                )
            } else {
                crate::front_end(&source, &name).map(|_| vec![])
            };
            if let Ok(findings) = &checked {
                if findings.iter().any(|d| {
                    d.get("severity").num() == Some(1.0)
                        || strict && d.get("data").get("complete") == &Json::Bool(false)
                }) {
                    status = 1;
                }
                out.extend(findings.iter().cloned());
            }
            if let Err(d) = checked {
                let (file, text) = d
                    .span
                    .and_then(|sp| nexo_diag::file_of(sp.file))
                    .unwrap_or((&name, &source));
                let mut value = crate::lsp::error_diagnostic(&d, text);
                if let Json::Obj(ref mut fields) = value {
                    fields.push(("file".into(), s(file)));
                    if d.span.is_none() {
                        if let Some((_, range)) = fields.iter_mut().find(|(key, _)| key == "range")
                        {
                            *range = Json::Null;
                        }
                    }
                }
                out.push(value);
                status = 1;
            }
        }
    }
    if json_mode {
        println!(
            "{}",
            obj(vec![
                ("schema_version", n(1)),
                ("position_encoding", s("utf-16")),
                ("success", Json::Bool(status == 0)),
                ("diagnostics", Json::Arr(out)),
            ])
            .text()
        );
    } else {
        for d in out {
            println!(
                "{}: {}: {} {}",
                d.get("file").str().unwrap_or("<command>"),
                d.get("code").str().unwrap_or(""),
                d.get("data").get("function").str().unwrap_or(""),
                d.get("message").str().unwrap_or("")
            );
            if d.get("data").get("approval_required") == &Json::Bool(true) {
                println!("  human approval required for configured tool dispatch");
            }
            for e in d.get("data").get("effects").arr() {
                println!(
                    "  {}: {} ({})",
                    e.get("operation").str().unwrap_or(""),
                    e.get("status").str().unwrap_or(""),
                    e.get("reason")
                        .str()
                        .or_else(|| e.get("capability").str())
                        .unwrap_or("no policy capability")
                );
                for check in e.get("checks").arr() {
                    if check.get("status").str() == Some("denied") {
                        println!(
                            "    denied by {}: {}",
                            check.get("scope").str().unwrap_or(""),
                            check.get("identity").str().unwrap_or("<application>")
                        );
                    }
                }
            }
        }
    }
    status
}
