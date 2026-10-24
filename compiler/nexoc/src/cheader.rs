//! `use c "library"` (section 18, D110): the library is found and its
//! header read when the program is compiled, as phase 0's clib.py does when
//! it runs, and the result is passed to `__c_library` as a third argument:
//! "ok\n<path>\n<signature lines>" or "err\n<message key>\n<arg>=<value>...".

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use crate::ast::*;

/// well-known libraries whose header is not named after them
const HEADERS: &[(&str, &[&str])] = &[
    ("m", &["math.h"]),
    ("c", &["stdlib.h", "string.h"]),
    ("z", &["zlib.h"]),
    ("ssl", &["openssl/ssl.h"]),
    ("crypto", &["openssl/crypto.h"]),
    ("sqlite3", &["sqlite3.h"]),
    ("curl", &["curl/curl.h"]),
    ("pthread", &["pthread.h"]),
];

const DROP: &[&str] = &[
    "__extension__", "__restrict", "__restrict__", "restrict", "__inline", "__inline__", "inline", "extern", "_Noreturn",
    "__const", "volatile", "__volatile__", "register", "_Nullable", "_Nonnull", "__wur",
];
const SKIP_CALLS: &[&str] =
    &["__attribute__", "__attribute", "__asm__", "__asm", "asm", "__declspec", "_Alignas", "__typeof__", "_Static_assert"];
const INT_WORDS: &[&str] = &["char", "short", "int", "long", "signed", "unsigned", "_Bool", "bool", "__int128"];
const OTHER_TYPE_WORDS: &[&str] = &["void", "float", "double", "const", "struct", "union", "enum", "_Complex"];

fn type_word(t: &str) -> bool {
    INT_WORDS.contains(&t) || OTHER_TYPE_WORDS.contains(&t)
}

fn is_ident(t: &str) -> bool {
    let mut c = t.chars();
    c.next().is_some_and(|f| f.is_ascii_alphabetic() || f == '_') && c.all(|x| x.is_ascii_alphanumeric() || x == '_')
}

// ------------------------------------------------------------- finding

fn lib_dirs() -> Vec<String> {
    let arch = match std::env::consts::ARCH {
        "x86_64" => "x86_64",
        "aarch64" => "aarch64",
        other => other,
    };
    let triplet = format!("{arch}-linux-gnu");
    let mut out: Vec<String> =
        std::env::var("LD_LIBRARY_PATH").unwrap_or_default().split(':').filter(|d| !d.is_empty()).map(String::from).collect();
    out.extend(["/usr/local/lib".to_string(), format!("/usr/lib/{triplet}"), "/usr/lib".into(), format!("/lib/{triplet}"), "/lib".into()]);
    out
}

fn base_name(name: &str) -> String {
    let mut base = Path::new(name).file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default();
    if let Some(i) = base.find(".so") {
        let rest = &base[i + 3..];
        if rest.is_empty() || (rest.starts_with('.') && rest.chars().all(|c| c == '.' || c.is_ascii_digit())) {
            base.truncate(i);
        }
    } else if let Some(b) = base.strip_suffix(".dylib") {
        base = b.to_string();
    }
    match base.strip_prefix("lib") {
        Some(b) if !b.is_empty() => b.to_string(),
        _ => base,
    }
}

/// A shared library (libc.so is often a text script for the linker).
fn is_elf(path: &Path) -> bool {
    use std::io::Read;
    let mut magic = [0u8; 4];
    std::fs::File::open(path).and_then(|mut f| f.read_exact(&mut magic)).is_ok() && &magic == b"\x7fELF"
}

fn find_library(name: &str, folder: &Path) -> Option<PathBuf> {
    if name.contains('/') {
        let path = if name.starts_with('/') { PathBuf::from(name) } else { folder.join(name) };
        return is_elf(&path).then_some(path);
    }
    let base = base_name(name);
    for d in lib_dirs() {
        let Ok(entries) = std::fs::read_dir(&d) else { continue };
        let prefix = format!("lib{base}.so.");
        let mut versions: Vec<String> =
            entries.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).filter(|n| n.starts_with(&prefix)).collect();
        versions.sort();
        let mut candidates = vec![format!("lib{base}.so")];
        candidates.extend(versions);
        for c in candidates {
            let p = Path::new(&d).join(&c);
            if is_elf(&p) {
                return Some(p);
            }
        }
    }
    None
}

fn header_names(name: &str) -> (bool, Vec<String>) {
    let base = base_name(name);
    match HEADERS.iter().find(|(b, _)| *b == base) {
        Some((_, hs)) => (true, hs.iter().map(|h| h.to_string()).collect()),
        None => (false, vec![format!("{base}.h"), format!("lib{base}.h"), format!("{base}lib.h")]),
    }
}

fn which(cmd: &str) -> Option<PathBuf> {
    if cmd.contains('/') {
        return Path::new(cmd).is_file().then(|| PathBuf::from(cmd));
    }
    std::env::var_os("PATH").and_then(|paths| std::env::split_paths(&paths).map(|d| d.join(cmd)).find(|p| p.is_file()))
}

fn compiler() -> Option<PathBuf> {
    let cc = std::env::var("CC").ok().filter(|c| !c.is_empty()).unwrap_or_else(|| "cc".into());
    which(&cc).or_else(|| which("clang")).or_else(|| which("gcc"))
}

fn preprocess(cc: &Path, headers: &[String], folders: &[PathBuf]) -> Option<String> {
    use std::io::Write;
    let source: String = headers.iter().map(|h| format!("#include <{h}>\n")).collect();
    let mut cmd = Command::new(cc);
    cmd.args(["-E", "-P", "-x", "c"]);
    for f in folders {
        cmd.arg(format!("-I{}", f.display()));
    }
    cmd.arg("-").stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
    let mut child = cmd.spawn().ok()?;
    child.stdin.take()?.write_all(source.as_bytes()).ok()?;
    let out = child.wait_with_output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

/// The text passed to `__c_library` for one library.
pub fn analyse(name: &str, folder: &Path) -> String {
    let err = |key: &str, args: &[(&str, String)]| {
        let mut out = format!("err\n{key}");
        for (k, v) in args {
            out.push_str(&format!("\n{k}={v}"));
        }
        out
    };
    let Some(path) = find_library(name, folder) else { return err("c_library", &[("name", name.to_string())]) };
    let Some(cc) = compiler() else { return err("c_compiler", &[]) };
    let mut folders = vec![folder.to_path_buf()];
    if name.contains('/') {
        if let Some(p) = path.parent() {
            folders.insert(0, p.to_path_buf());
        }
    }
    let (known, names) = header_names(name);
    let groups: Vec<Vec<String>> = if known { vec![names.clone()] } else { names.iter().map(|h| vec![h.clone()]).collect() };
    let text = groups.iter().find_map(|g| preprocess(&cc, g, &folders));
    let Some(text) = text else { return err("c_header", &[("name", name.to_string()), ("tried", names.join(", "))]) };
    let mut out = format!("ok\n{}", path.display());
    for line in signatures(&text) {
        out.push('\n');
        out.push_str(&line);
    }
    out
}

// -------------------------------------------------------------- headers

fn tokens(text: &str) -> Vec<String> {
    let c: Vec<char> = text.chars().collect();
    let mut raw = Vec::new();
    let mut i = 0;
    while i < c.len() {
        let ch = c[i];
        if ch.is_whitespace() {
            i += 1;
            continue;
        }
        let start = i;
        if ch == '"' || ch == '\'' {
            i += 1;
            while i < c.len() && c[i] != ch {
                if c[i] == '\\' {
                    i += 1;
                }
                i += 1;
            }
            if i >= c.len() {
                // an unterminated quote is a single character, as the
                // regular expression of phase 0 reads it
                i = start + 1;
            } else {
                i += 1;
            }
        } else if ch.is_ascii_alphabetic() || ch == '_' {
            while i < c.len() && (c[i].is_alphanumeric() || c[i] == '_') {
                i += 1;
            }
        } else if ch.is_ascii_digit() {
            while i < c.len() && (c[i].is_alphanumeric() || c[i] == '_' || c[i] == '.') {
                i += 1;
            }
        } else if ch == '.' && c.get(i + 1) == Some(&'.') && c.get(i + 2) == Some(&'.') {
            i += 3;
        } else {
            i += 1;
        }
        raw.push(c[start..i].iter().collect::<String>());
    }
    let mut out = Vec::new();
    let mut i = 0;
    while i < raw.len() {
        let t = raw[i].as_str();
        if SKIP_CALLS.contains(&t) && raw.get(i + 1).map(|s| s.as_str()) == Some("(") {
            let mut depth = 0;
            i += 1;
            while i < raw.len() {
                if raw[i] == "(" {
                    depth += 1;
                } else if raw[i] == ")" {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                }
                i += 1;
            }
            i += 1;
            continue;
        }
        if !DROP.contains(&t) {
            out.push(raw[i].clone());
        }
        i += 1;
    }
    out
}

/// The top-level declarations (`...;`), without the bodies of functions
/// and the members of structs.
fn declarations(toks: &[String]) -> Vec<Vec<String>> {
    let mut out = Vec::new();
    let mut cur: Vec<String> = Vec::new();
    let mut i = 0;
    while i < toks.len() {
        let t = toks[i].as_str();
        if t == "{" {
            let mut depth = 0;
            while i < toks.len() {
                if toks[i] == "{" {
                    depth += 1;
                } else if toks[i] == "}" {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                }
                i += 1;
            }
            if cur.last().map(|s| s.as_str()) == Some(")") {
                // a function with its body: not a declaration
                cur.clear();
            } else {
                cur.push("{}".into());
            }
            i += 1;
            continue;
        }
        if t == ";" {
            if !cur.is_empty() {
                out.push(std::mem::take(&mut cur));
            }
        } else {
            cur.push(toks[i].clone());
        }
        i += 1;
    }
    out
}

fn split_params(toks: &[String]) -> Vec<Vec<String>> {
    let mut params = Vec::new();
    let mut cur = Vec::new();
    let mut depth = 0;
    for t in toks {
        if t == "(" {
            depth += 1;
        } else if t == ")" {
            depth -= 1;
        }
        if t == "," && depth == 0 {
            params.push(std::mem::take(&mut cur));
        } else {
            cur.push(t.clone());
        }
    }
    if !cur.is_empty() {
        params.push(cur);
    }
    params
}

/// A parameter without its name (`const char *s` -> `const char *`).
fn strip_name(toks: &[String]) -> Vec<String> {
    let mut toks = toks.to_vec();
    let mut arrays = 0;
    while toks.len() >= 2 && toks.last().map(|s| s.as_str()) == Some("]") {
        let mut j = toks.len() as isize - 1;
        while j >= 0 && toks[j as usize] != "[" {
            j -= 1;
        }
        toks.truncate(j.max(0) as usize);
        arrays += 1;
    }
    if toks.iter().any(|t| t == "(") {
        return vec!["(*)".into()];
    }
    if toks.len() >= 2 {
        let last = toks.last().unwrap();
        let tagged = toks.len() == 2 && matches!(toks[0].as_str(), "struct" | "union" | "enum");
        if is_ident(last) && !type_word(last) && !tagged {
            toks.pop();
        }
    }
    toks.extend(std::iter::repeat("*".to_string()).take(arrays));
    toks
}

type Typedefs = std::collections::HashMap<String, Vec<String>>;

/// The code of a C type: v, b, i8..u64, f32, f64, s, or x:<type>. A text
/// parameter must be `const char *` (a `char *` is a buffer C writes into).
fn classify(toks: &[String], typedefs: &Typedefs, param: bool, depth: usize) -> String {
    let constant = toks.iter().any(|t| t == "const");
    let shown = format!("x:{}", toks.join(" "));
    let toks: Vec<&String> = toks.iter().filter(|t| *t != "const").collect();
    let stars = toks.iter().filter(|t| **t == "*").count();
    let base: Vec<&str> = toks.iter().filter(|t| **t != "*").map(|t| t.as_str()).collect();
    if base.len() == 1 && depth < 20 {
        if let Some(def) = typedefs.get(base[0]) {
            let mut expanded = def.clone();
            if constant {
                expanded.push("const".into());
            }
            expanded.extend(std::iter::repeat("*".to_string()).take(stars));
            let inner = classify(&expanded, typedefs, param, depth + 1);
            return if inner.starts_with("x:") { shown } else { inner };
        }
    }
    if base.contains(&"(*)") {
        return shown;
    }
    if stars > 0 {
        let chars = !base.is_empty() && base.iter().all(|t| matches!(*t, "char" | "signed" | "unsigned")) && base.contains(&"char");
        if stars == 1 && chars && (constant || !param) {
            return "s".into();
        }
        return shown;
    }
    match base.as_slice() {
        ["void"] => return "v".into(),
        ["_Bool"] | ["bool"] => return "b".into(),
        ["float"] => return "f32".into(),
        ["double"] => return "f64".into(),
        _ => {}
    }
    if base.first() == Some(&"enum") {
        return "i32".into();
    }
    if base.is_empty() || !base.iter().all(|t| INT_WORDS.contains(t)) || base.contains(&"__int128") {
        return shown;
    }
    let unsigned = base.contains(&"unsigned");
    let bits = if base.contains(&"char") {
        8
    } else if base.contains(&"short") {
        16
    } else if base.contains(&"long") {
        64
    } else {
        32
    };
    format!("{}{bits}", if unsigned { 'u' } else { 'i' })
}

/// `name ret arg...` lines (with `...` at the end of a variadic function),
/// in the order of the header; the first declaration wins.
pub fn signatures(text: &str) -> Vec<String> {
    let mut typedefs = Typedefs::new();
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    for d in declarations(&tokens(text)) {
        if d[0] == "typedef" {
            let body = &d[1..];
            if let Some(open) = body.iter().position(|t| t == "(") {
                let names: Vec<&String> = body.iter().filter(|t| is_ident(t) && !type_word(t)).collect();
                let n = body[open + 1..].iter().find(|t| is_ident(t)).or(names.last().copied());
                if let Some(n) = n {
                    typedefs.insert(n.clone(), vec!["(*)".into()]);
                }
            } else if let Some(last) = body.last().filter(|t| is_ident(t)) {
                let def: Vec<String> = body[..body.len() - 1].iter().filter(|t| *t != "{}").cloned().collect();
                typedefs.insert(last.clone(), if def.is_empty() { vec!["int".into()] } else { def });
            }
            continue;
        }
        let Some(i) = d.iter().position(|t| t == "(") else { continue };
        if d.last().map(|s| s.as_str()) != Some(")") {
            continue;
        }
        if i == 0 || !is_ident(&d[i - 1]) || type_word(&d[i - 1]) {
            continue;
        }
        let name = d[i - 1].clone();
        let ret = &d[..i - 1];
        if ret.is_empty() || ret.iter().any(|t| t == "{}") || seen.contains(&name) {
            continue;
        }
        if ret.iter().any(|t| t == "(" || t == ")") {
            continue;
        }
        seen.insert(name.clone());
        let mut params = split_params(&d[i + 1..d.len() - 1]);
        let variadic = params.last().is_some_and(|p| p.len() == 1 && p[0] == "...");
        if variadic {
            params.pop();
        }
        if params.len() == 1 && params[0].len() == 1 && params[0][0] == "void" {
            params.clear();
        }
        let mut codes = vec![classify(ret, &typedefs, false, 0)];
        codes.extend(params.iter().map(|p| classify(&strip_name(p), &typedefs, true, 0)));
        let mut line = name;
        for c in codes {
            line.push(' ');
            line.push_str(&c.replace(' ', "~"));
        }
        if variadic {
            line.push_str(" ...");
        }
        out.push(line);
    }
    out
}

// ----------------------------------------------------------- the program

/// Give every `__c_library(name, bound)` of the program its header, and
/// every `__python_import(name)` the Python to load (D111).
pub fn embed(program: &mut Program, folder: &Path) {
    let mut python = None;
    visit(&mut program.body, folder, &mut python);
}

/// "ok\n<libpython>\n<sys.path...>" of $NEXO_PYTHON (or python3), or
/// "err\npython_missing".
fn python_info() -> String {
    const SCRIPT: &str = "import os, sys, sysconfig\n\
        d = sysconfig.get_config_var('LIBDIR') or ''\n\
        names = [sysconfig.get_config_var('LDLIBRARY') or '', sysconfig.get_config_var('INSTSONAME') or '']\n\
        lib = next((os.path.join(d, n) for n in names if '.so' in n and os.path.exists(os.path.join(d, n))), '')\n\
        print(lib)\n\
        print('\\n'.join(p for p in sys.path if p))\n";
    let python = std::env::var("NEXO_PYTHON").ok().filter(|p| !p.is_empty()).unwrap_or_else(|| "python3".into());
    let out = Command::new(python).args(["-c", SCRIPT]).stderr(Stdio::null()).output();
    match out {
        Ok(o) if o.status.success() => {
            let text = String::from_utf8_lossy(&o.stdout).into_owned();
            match text.lines().next() {
                Some(lib) if !lib.is_empty() => format!("ok\n{}", text.trim_end()),
                _ => "err\npython_missing".into(),
            }
        }
        _ => "err\npython_missing".into(),
    }
}

fn visit(body: &mut [Stmt], folder: &Path, python: &mut Option<String>) {
    for s in body {
        match &mut s.kind {
            StmtKind::Assign { value, .. } => {
                if let ExprKind::Call { func, args } = &mut value.kind {
                    if matches!(&func.kind, ExprKind::Name(n) if n == "__python_import") && args.len() == 1 {
                        let info = python.get_or_insert_with(python_info).clone();
                        let mut arg = args[0].clone();
                        arg.value.kind = ExprKind::Str(vec![StrPiece::Lit(info)]);
                        args.push(arg);
                        continue;
                    }
                    if matches!(&func.kind, ExprKind::Name(n) if n == "__c_library") && args.len() == 2 {
                        let name = match &args[0].value.kind {
                            ExprKind::Str(p) => p
                                .iter()
                                .map(|x| match x {
                                    StrPiece::Lit(t) => t.as_str(),
                                    _ => "",
                                })
                                .collect::<String>(),
                            _ => continue,
                        };
                        let info = analyse(&name, folder);
                        let mut arg = args[1].clone();
                        arg.value.kind = ExprKind::Str(vec![StrPiece::Lit(info)]);
                        args.push(arg);
                    }
                }
            }
            StmtKind::Fn(f) => visit(&mut f.body, folder, python),
            StmtKind::If { branches, orelse } => {
                for (_, b) in branches {
                    visit(b, folder, python);
                }
                if let Some(b) = orelse {
                    visit(b, folder, python);
                }
            }
            StmtKind::For { body, .. } | StmtKind::While { body, .. } | StmtKind::Repeat { body, .. } => visit(body, folder, python),
            StmtKind::Try { body, handlers, .. } => {
                visit(body, folder, python);
                for h in handlers {
                    visit(&mut h.body, folder, python);
                }
            }
            StmtKind::Parallel(b) | StmtKind::Test { body: b, .. } | StmtKind::Event { body: b, .. } => visit(b, folder, python),
            StmtKind::Match { arms, .. } => {
                for a in arms {
                    visit(&mut a.body, folder, python);
                }
            }
            _ => {}
        }
    }
}
