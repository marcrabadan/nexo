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
    let paths: Vec<String> = args.iter().filter(|a| *a != "--json").cloned().collect();
    let mut out = Vec::new();
    let mut status = 0;
    if paths.iter().any(|a| a.starts_with('-'))
        || args.iter().filter(|a| *a == "--json").count() != 1
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
            if let Err(d) = crate::front_end(&source, &name) {
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
    status
}
