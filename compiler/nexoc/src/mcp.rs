//! `nexo mcp` (section 26, D146): a Model Context Protocol server on
//! standard input and output, as in phase 0 (nexo/mcp.py). It offers an
//! assistant the commands of the project that only read or format it:
//! `check`, `run_tests`, `docs_for_ai`, `plan` and `format`. Each tool runs
//! this same `nexo` on a file of the project (the folder the server was
//! started in) and answers with what the command printed; `apply`,
//! deployments and secrets are never offered.

use std::io::{BufRead, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};

use crate::json::{obj, parse, s, Json};
use nexo_diag::msg;

const VERSION: &str = "2025-06-18";

const PATH: &str = "A .nexo file or folder of the project, relative to it (default: the project).";
const FILE: &str = "A .nexo file of the project, relative to it.";

/// name, description, arguments (name, description) and the required ones
const TOOLS: &[(&str, &str, &[(&str, &str)], &[&str])] = &[
    ("check", "Compile without running: every error with its fix, or that the code is correct.", &[("path", PATH)], &[]),
    ("run_tests", "Run the test blocks and report each one.", &[("path", PATH)], &[]),
    ("docs_for_ai", "The exact list of what a program can use, and the declarations of a file.", &[("path", FILE)], &[]),
    (
        "plan",
        "What nexo apply would change in the infrastructure, without applying it.",
        &[("path", FILE), ("stage", "The stage to plan (default: the first one).")],
        &[],
    ),
    ("format", "Format a file with nexo fmt (the same tokens, the one layout).", &[("path", FILE)], &["path"]),
];

fn problem(key: &str, args: &[(&str, String)]) -> String {
    format!("{} {}", msg(key, args), msg(&format!("fix.{key}"), args))
}

fn tool_list() -> Json {
    let tools = TOOLS
        .iter()
        .map(|(name, description, props, required)| {
            let props = Json::Obj(
                props
                    .iter()
                    .map(|(p, d)| (p.to_string(), obj(vec![("type", s("string")), ("description", s(*d))])))
                    .collect(),
            );
            let schema = obj(vec![
                ("type", s("object")),
                ("properties", props),
                ("required", Json::Arr(required.iter().map(|r| s(*r)).collect())),
                ("additionalProperties", Json::Bool(false)),
            ]);
            obj(vec![("name", s(*name)), ("description", s(*description)), ("inputSchema", schema)])
        })
        .collect();
    Json::Arr(tools)
}

/// The path with `.` and `..` taken away, without touching the disk.
fn normal(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in path.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            c => out.push(c),
        }
    }
    out
}

/// The path with its links followed as far as it exists (like Python's
/// `Path.resolve()`).
fn resolve(path: &Path) -> PathBuf {
    let mut existing = normal(path);
    let mut rest = Vec::new();
    loop {
        if let Ok(real) = existing.canonicalize() {
            let mut out = real;
            for part in rest.iter().rev() {
                out.push(part);
            }
            return normal(&out);
        }
        match (existing.file_name().map(|f| f.to_os_string()), existing.parent()) {
            (Some(name), Some(parent)) => {
                rest.push(name);
                existing = parent.to_path_buf();
            }
            _ => return normal(path),
        }
    }
}

/// A path of the project: relative, and not out of it through .. or a link.
fn inside(root: &Path, path: &str) -> bool {
    if path.is_empty() || Path::new(path).is_absolute() {
        return false;
    }
    let mut joined = root.to_path_buf();
    for c in Path::new(path).components() {
        joined.push(c);
        // a link is followed where it is, as the system does
        if joined.is_symlink() {
            joined = resolve(&joined);
        } else {
            joined = normal(&joined);
        }
    }
    resolve(&joined).starts_with(resolve(root))
}

/// The arguments of `nexo` for a tool, or the message of what is wrong.
fn command(name: &str, args: &[(String, Json)]) -> Result<Vec<String>, String> {
    let (_, _, props, required) = TOOLS.iter().find(|t| t.0 == name).unwrap();
    for (key, value) in args {
        if !props.iter().any(|(p, _)| p == key) {
            return Err(problem("mcp_argument", &[("tool", name.to_string()), ("name", key.clone())]));
        }
        if value.str().is_none() {
            return Err(problem("mcp_text", &[("name", key.clone())]));
        }
    }
    let get = |k: &str| args.iter().find(|(n, _)| n == k).and_then(|(_, v)| v.str()).map(str::to_string);
    for key in required.iter() {
        if get(key).is_none() {
            return Err(problem("mcp_missing", &[("tool", name.to_string()), ("name", key.to_string())]));
        }
    }
    let path = get("path");
    let root = std::env::current_dir().unwrap_or_default();
    if let Some(p) = &path {
        if !inside(&root, p) {
            return Err(problem("mcp_outside", &[("path", p.clone())]));
        }
    }
    let paths: Vec<String> = path.into_iter().collect();
    let mut out: Vec<String> = match name {
        "check" => vec!["check".into()],
        "run_tests" => vec!["test".into()],
        "docs_for_ai" => vec!["docs".into(), "--for-ai".into()],
        "plan" => {
            let mut v = vec!["plan".to_string()];
            if let Some(stage) = get("stage") {
                v.push("--stage".into());
                v.push(stage);
            }
            v
        }
        _ => vec!["fmt".into()],
    };
    out.extend(paths);
    Ok(out)
}

fn text_result(text: String, is_error: bool) -> Json {
    obj(vec![
        ("content", Json::Arr(vec![obj(vec![("type", s("text")), ("text", s(text))])])),
        ("isError", Json::Bool(is_error)),
    ])
}

fn call_tool(name: &str, args: &[(String, Json)]) -> Json {
    let argv = match command(name, args) {
        Ok(a) => a,
        Err(m) => return text_result(m, true),
    };
    let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("nexo"));
    // stdout and stderr in the order they are written, as one text
    let (reader, writer) = match std::io::pipe() {
        Ok(p) => p,
        Err(e) => return text_result(e.to_string(), true),
    };
    let child = Command::new(exe)
        .args(&argv)
        .stdin(Stdio::null())
        .stdout(writer.try_clone().map(Stdio::from).unwrap_or(Stdio::null()))
        .stderr(Stdio::from(writer))
        .spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => return text_result(e.to_string(), true),
    };
    let mut bytes = Vec::new();
    let mut reader = reader;
    let _ = std::io::Read::read_to_end(&mut reader, &mut bytes);
    let ok = child.wait().map(|s| s.success()).unwrap_or(false);
    text_result(String::from_utf8_lossy(&bytes).into_owned(), !ok)
}

fn error(id: Json, code: i32, message: String) -> Json {
    obj(vec![
        ("jsonrpc", s("2.0")),
        ("id", id),
        ("error", obj(vec![("code", Json::Num(code as f64)), ("message", s(message))])),
    ])
}

/// The answer to one message, or None for a notification.
fn answer(request: &Json) -> Option<Json> {
    let id = request.get("id").clone();
    if id.is_null() {
        return None;
    }
    let method = request.get("method").str().unwrap_or("");
    let params = request.get("params");
    let result = match method {
        "initialize" => obj(vec![
            ("protocolVersion", s(VERSION)),
            ("capabilities", obj(vec![("tools", obj(vec![]))])),
            ("serverInfo", obj(vec![("name", s("nexo")), ("version", s("0.1"))])),
        ]),
        "ping" => obj(vec![]),
        "tools/list" => obj(vec![("tools", tool_list())]),
        "tools/call" => {
            let name = params.get("name");
            let shown = match name {
                Json::Str(n) => n.clone(),
                Json::Null => "None".to_string(),
                other => other.text(),
            };
            let args = match params.get("arguments") {
                Json::Obj(pairs) => Some(pairs.clone()),
                Json::Null => Some(Vec::new()),
                _ => None,
            };
            match (name.str().filter(|n| TOOLS.iter().any(|t| t.0 == *n)), args) {
                (Some(n), Some(args)) => call_tool(n, &args),
                _ => return Some(error(id, -32602, problem("mcp_tool", &[("name", shown)]))),
            }
        }
        _ => {
            let shown = request.get("method").str().map(str::to_string).unwrap_or_else(|| "None".into());
            return Some(error(id, -32601, problem("mcp_method", &[("name", shown)])));
        }
    };
    Some(obj(vec![("jsonrpc", s("2.0")), ("id", id), ("result", result)]))
}

/// One JSON-RPC message per line, until standard input ends.
pub fn serve() -> u8 {
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    for line in stdin.lock().split(b'\n') {
        let Ok(line) = line else { break };
        let text = String::from_utf8_lossy(&line);
        if text.trim().is_empty() {
            continue;
        }
        let reply = match parse(text.trim()) {
            None => Some(error(Json::Null, -32700, problem("mcp_json", &[]))),
            Some(request @ Json::Obj(_)) => answer(&request),
            Some(_) => Some(error(Json::Null, -32600, problem("mcp_json", &[]))),
        };
        if let Some(r) = reply {
            let _ = writeln!(out, "{}", r.text());
            let _ = out.flush();
        }
    }
    0
}
