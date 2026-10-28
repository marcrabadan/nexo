//! D142 path and host matching, shared by the typed engine.
/// The components of a path with `.` and `..` taken away.
fn parts(path: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for p in path.split('/') {
        match p {
            "" | "." => {}
            ".." => {
                if out.last().is_some_and(|l| l != "..") {
                    out.pop();
                } else if !path.starts_with('/') {
                    out.push(p.to_string());
                }
            }
            _ => out.push(p.to_string()),
        }
    }
    out
}

/// A path as the policy writes it: `data/../x.csv` is `x.csv`.
pub fn shown(path: &str) -> String {
    let p = parts(path).join("/");
    if path.starts_with('/') {
        format!("/{p}")
    } else if p.is_empty() {
        ".".into()
    } else {
        p
    }
}

/// The absolute path the system opens: each component in turn, a
/// symbolic link replaced by its target before the `..` after it, so
/// neither a link nor `link/..` can lead out of an allowed folder.
fn resolve(path: &str) -> String {
    let full = if path.starts_with('/') {
        path.to_string()
    } else {
        format!("{}/{path}", std::env::current_dir().map(|d| d.to_string_lossy().to_string()).unwrap_or_default())
    };
    let split = |p: &str| -> Vec<String> { p.split('/').filter(|c| !c.is_empty() && *c != ".").map(String::from).collect() };
    let mut todo: Vec<String> = split(&full);
    todo.reverse();
    let mut out: Vec<String> = Vec::new();
    let mut links = 0;
    while let Some(c) = todo.pop() {
        if c == ".." {
            out.pop();
            continue;
        }
        let here = format!("/{}", out.iter().chain(std::iter::once(&c)).cloned().collect::<Vec<_>>().join("/"));
        let is_link = std::fs::symlink_metadata(&here).is_ok_and(|m| m.file_type().is_symlink());
        if links < 40 && is_link {
            if let Ok(target) = std::fs::read_link(&here) {
                links += 1;
                let target = target.to_string_lossy().to_string();
                if target.starts_with('/') {
                    out.clear();
                }
                todo.extend(split(&target).into_iter().rev());
                continue;
            }
        }
        out.push(c);
    }
    format!("/{}", out.join("/"))
}

fn path_matches(path: &str, target: &str) -> bool {
    let (p, t) = (resolve(path), resolve(target));
    p == t || t == "/" || p.starts_with(&format!("{t}/"))
}

fn host_matches(host: &str, target: &str) -> bool {
    let t = target.to_lowercase();
    match t.strip_prefix('*') {
        Some(rest) if rest.starts_with('.') => host.ends_with(rest),
        _ => host == t,
    }
}

pub(super) fn matches(kind: &str, value: &str, target: &str) -> bool {
    match kind {
        "read" | "write" => path_matches(value, target),
        "net" => host_matches(value, target),
        _ => value == target,
    }
}


/// The launcher already canonicalized grant roots. Do not re-resolve them
/// after application code has run; a replaced symlink must not widen a grant.
pub(super) fn matches_resolved(kind: &str, value: &str, target: &str) -> bool {
    if kind == "read" || kind == "write" {
        let value = resolve(value);
        value == target || target == "/" || value.starts_with(&format!("{target}/"))
    } else { matches(kind, value, target) }
}
