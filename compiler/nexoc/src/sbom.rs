//! The software bill of materials that `nexo build --release` writes next
//! to the program (section "Goals and guarantees", D128): a CycloneDX 1.5
//! document listing the program, its source files, the runtime and the
//! crates it is built from, the packages of nexo.lock, the C libraries and Python modules the program
//! uses, and the compiler that turned it into machine code. It has no
//! dates or random serial number, so two builds of the same code write the
//! same file.

use std::path::Path;

use sha2::{Digest, Sha256};

use crate::ast::{Stmt, StmtKind};
use crate::json::{obj, s, Json};

/// The lock file of the compiler: the exact crates linked into the runtime.
const CARGO_LOCK: &str = include_str!("../../Cargo.lock");

pub fn sha256(bytes: &[u8]) -> String {
    Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect()
}

/// (name, version, dependencies) of each package of the lock file.
fn lock_packages() -> Vec<(String, String, Vec<String>)> {
    let mut out = Vec::new();
    for block in CARGO_LOCK.split("[[package]]").skip(1) {
        let mut name = String::new();
        let mut version = String::new();
        let mut deps = Vec::new();
        let mut in_deps = false;
        for line in block.lines() {
            let line = line.trim();
            if in_deps {
                if line.starts_with(']') {
                    in_deps = false;
                } else {
                    // "name" or "name version"
                    let d = line.trim_matches(|c| c == '"' || c == ',');
                    deps.push(d.split(' ').next().unwrap_or("").to_string());
                }
                continue;
            }
            if let Some(v) = line.strip_prefix("name = ") {
                name = v.trim_matches('"').to_string();
            } else if let Some(v) = line.strip_prefix("version = ") {
                version = v.trim_matches('"').to_string();
            } else if line.starts_with("dependencies = [") {
                in_deps = !line.ends_with(']');
            }
        }
        out.push((name, version, deps));
    }
    out
}

/// The crates the runtime is built from (every program links them).
fn runtime_crates() -> Vec<(String, String)> {
    let packages = lock_packages();
    let mut seen: Vec<String> = Vec::new();
    let mut todo = vec!["nexo-runtime".to_string()];
    while let Some(n) = todo.pop() {
        if seen.contains(&n) {
            continue;
        }
        if let Some((_, _, deps)) = packages.iter().find(|(p, _, _)| *p == n) {
            todo.extend(deps.iter().cloned());
        }
        seen.push(n);
    }
    let mut out: Vec<(String, String)> = packages
        .iter()
        .filter(|(n, _, _)| seen.contains(n) && n != "nexo-runtime" && n != "nexo-diag")
        .map(|(n, v, _)| (n.clone(), v.clone()))
        .collect();
    out.sort();
    out
}

fn uses(body: &[Stmt], out: &mut Vec<(String, String)>) {
    crate::checker::walk_stmts(body, true, &mut |st| {
        if let StmtKind::Use { name, lang: Some(lang), .. } = &st.kind {
            let item = (lang.clone(), name.clone());
            if !out.contains(&item) {
                out.push(item);
            }
        }
    });
}

fn hash(value: &str) -> Json {
    Json::Arr(vec![obj(vec![("alg", s("SHA-256")), ("content", s(value))])])
}

/// The document for the program built at `out` from `files` (the main
/// file first, then its modules) whose code is `body`.
pub fn document(out: &Path, files: &[(String, String)], body: &[Stmt], cc_version: &str) -> String {
    let binary = std::fs::read(out).unwrap_or_default();
    let name = out.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let version = env!("CARGO_PKG_VERSION");
    let mut components = Vec::new();
    for (path, text) in files {
        components.push(obj(vec![
            ("type", s("file")),
            ("bom-ref", s(format!("file:{path}"))),
            ("name", s(path.clone())),
            ("hashes", hash(&sha256(text.as_bytes()))),
        ]));
    }
    components.push(obj(vec![
        ("type", s("library")),
        ("bom-ref", s("nexo-runtime")),
        ("name", s("nexo-runtime")),
        ("version", s(version)),
        ("licenses", Json::Arr(vec![obj(vec![("expression", s("Apache-2.0 WITH LLVM-exception"))])])),
    ]));
    for (n, v) in runtime_crates() {
        components.push(obj(vec![
            ("type", s("library")),
            ("bom-ref", s(format!("pkg:cargo/{n}@{v}"))),
            ("name", s(n.clone())),
            ("version", s(v.clone())),
            ("purl", s(format!("pkg:cargo/{n}@{v}"))),
        ]));
    }
    // the packages of nexo.lock (D174), with the sha256 of each archive
    if let Some((main, _)) = files.first() {
        let root = crate::modules::project_root(main);
        for (n, entry) in crate::packages::read_lock(&root) {
            let v = entry.get("version").cloned().unwrap_or_default();
            components.push(obj(vec![
                ("type", s("library")),
                ("bom-ref", s(format!("pkg:nexo/{n}@{v}"))),
                ("name", s(n.clone())),
                ("version", s(v.clone())),
                ("hashes", hash(entry.get("sha256").map(String::as_str).unwrap_or(""))),
                ("purl", s(format!("pkg:nexo/{n}@{v}"))),
            ]));
        }
    }
    let mut foreign = Vec::new();
    uses(body, &mut foreign);
    for (lang, what) in foreign {
        let kind = if lang == "c" { "C library" } else { "Python module" };
        components.push(obj(vec![
            ("type", s("library")),
            ("bom-ref", s(format!("{lang}:{what}"))),
            ("name", s(what.clone())),
            ("description", s(kind)),
        ]));
    }
    let doc = obj(vec![
        ("bomFormat", s("CycloneDX")),
        ("specVersion", s("1.5")),
        ("version", Json::Num(1.0)),
        (
            "metadata",
            obj(vec![
                (
                    "tools",
                    Json::Arr(vec![
                        obj(vec![("name", s("nexo")), ("version", s(version))]),
                        obj(vec![("name", s(cc_version))]),
                    ]),
                ),
                (
                    "component",
                    obj(vec![
                        ("type", s("application")),
                        ("bom-ref", s(name.clone())),
                        ("name", s(name)),
                        ("hashes", hash(&sha256(&binary))),
                    ]),
                ),
            ]),
        ),
        ("components", Json::Arr(components)),
    ]);
    let mut text = pretty(&doc, 0);
    text.push('\n');
    text
}

/// Indented JSON (the language server's printer writes one line).
fn pretty(v: &Json, depth: usize) -> String {
    let pad = "  ".repeat(depth + 1);
    let end = "  ".repeat(depth);
    match v {
        Json::Arr(items) if !items.is_empty() => {
            let inner: Vec<String> = items.iter().map(|x| format!("{pad}{}", pretty(x, depth + 1))).collect();
            format!("[\n{}\n{end}]", inner.join(",\n"))
        }
        Json::Obj(pairs) if !pairs.is_empty() => {
            let inner: Vec<String> =
                pairs.iter().map(|(k, x)| format!("{pad}{}: {}", s(k.clone()).text(), pretty(x, depth + 1))).collect();
            format!("{{\n{}\n{end}}}", inner.join(",\n"))
        }
        Json::Num(n) if n.fract() == 0.0 => format!("{}", *n as i64),
        other => other.text(),
    }
}
