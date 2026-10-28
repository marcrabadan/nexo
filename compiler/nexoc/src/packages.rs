//! Packages (section 13, D174): `nexo add`, `nexo publish`, nexo.lock and
//! the packages a `use` loads. Follows phase 0's packages.py, and writes
//! the same archives, indexes and lock files byte for byte.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use ed25519_dalek::{Signer, Verifier};
use nexo_diag::{msg, Diagnostic};
use sha2::{Digest, Sha256};

use crate::json::{self, Json};

pub const REGISTRY: &str = "https://registry.nexo-lang.org";
const LOCK_HEADER: &str = "# nexo.lock: the exact packages of this project, written by nexo add. Do not edit.\n";

type V3 = (u64, u64, u64);
type PResult<T> = Result<T, Diagnostic>;

fn fail(key: &'static str) -> Diagnostic {
    Diagnostic::new_spanless(key)
}

/// The name a `use` gives a package: `money-format` is `money_format`.
pub fn import_name(package: &str) -> String {
    package.replace('-', "_")
}

pub fn valid_name(name: &str) -> bool {
    let mut words = name.split('-');
    let first = words.next().unwrap_or("");
    first.chars().next().is_some_and(|c| c.is_ascii_lowercase())
        && name.split('-').all(|w| !w.is_empty() && w.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit()))
}

fn number(text: &str) -> Option<u64> {
    let ok = !text.is_empty() && text.chars().all(|c| c.is_ascii_digit()) && (text == "0" || !text.starts_with('0'));
    if ok { text.parse().ok() } else { None }
}

// ---------------------------------------------------------------- versions

pub fn version(text: &str) -> Option<V3> {
    let p: Vec<&str> = text.split('.').collect();
    if p.len() != 3 {
        return None;
    }
    Some((number(p[0])?, number(p[1])?, number(p[2])?))
}

/// [low, high) of `^1.2`: 1.2.0 up to 2.0.0; `^0.3` stays in 0.3.
fn requirement(text: &str) -> PResult<(V3, V3)> {
    let bad = || fail("package_requirement").arg("requirement", text.to_string());
    let rest = text.strip_prefix('^').ok_or_else(bad)?;
    let p: Vec<&str> = rest.split('.').collect();
    if p.len() < 2 || p.len() > 3 {
        return Err(bad());
    }
    let major = number(p[0]).ok_or_else(bad)?;
    let minor = number(p[1]).ok_or_else(bad)?;
    let patch = match p.get(2) {
        Some(t) => Some(number(t).ok_or_else(bad)?),
        None => None,
    };
    let low = (major, minor, patch.unwrap_or(0));
    Ok(if major > 0 {
        (low, (major + 1, 0, 0))
    } else if minor > 0 || patch.is_none() {
        (low, (0, minor + 1, 0))
    } else {
        (low, (0, 0, low.2 + 1))
    })
}

fn accepts(req: &str, v: V3) -> bool {
    requirement(req).is_ok_and(|(low, high)| low <= v && v < high)
}

fn shown_version(v: V3) -> String {
    format!("{}.{}.{}", v.0, v.1, v.2)
}

// -------------------------------------------------------------------- toml

#[derive(Clone, Debug, PartialEq)]
pub enum TomlValue {
    Bool(bool),
    Str(String),
    List(Vec<String>),
}

fn toml_value(text: &str) -> Option<TomlValue> {
    let t = text.trim();
    if t == "true" || t == "false" {
        return Some(TomlValue::Bool(t == "true"));
    }
    if t.len() >= 2 && t.starts_with('"') && t.ends_with('"') {
        return Some(TomlValue::Str(t[1..t.len() - 1].to_string()));
    }
    if t.starts_with('[') && t.ends_with(']') {
        let mut items = Vec::new();
        for p in t[1..t.len() - 1].split(',').filter(|p| !p.trim().is_empty()) {
            match toml_value(p)? {
                TomlValue::Str(s) => items.push(s),
                _ => return None,
            }
        }
        return Some(TomlValue::List(items));
    }
    None
}

fn without_comment(line: &str) -> &str {
    let mut quoted = false;
    for (i, c) in line.char_indices() {
        if c == '"' {
            quoted = !quoted;
        } else if c == '#' && !quoted {
            return &line[..i];
        }
    }
    line
}

/// Each table with its keys, in the order of the file.
pub type Manifest = Vec<(String, Vec<(String, Option<TomlValue>)>)>;

pub fn read_manifest(text: &str, path: &str) -> PResult<Manifest> {
    let mut tables: Manifest = vec![(String::new(), Vec::new())];
    for (n, raw) in text.lines().enumerate() {
        let line = without_comment(raw).trim();
        if line.is_empty() {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            let name = line.trim_matches(['[', ']']).trim().to_string();
            if !tables.iter().any(|t| t.0 == name) {
                tables.push((name.clone(), Vec::new()));
            }
            let i = tables.iter().position(|t| t.0 == name).unwrap();
            let t = tables.remove(i);
            tables.push(t);
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            return Err(fail("package_manifest_line").arg("path", path.to_string()).arg("line", (n + 1).to_string()));
        };
        let key = key.trim().trim_matches('"').to_string();
        let table = &mut tables.last_mut().unwrap().1;
        let value = toml_value(value);
        match table.iter_mut().find(|(k, _)| *k == key) {
            Some(entry) => entry.1 = value,
            None => table.push((key, value)),
        }
    }
    Ok(tables)
}

fn table<'a>(m: &'a Manifest, name: &str) -> &'a [(String, Option<TomlValue>)] {
    m.iter().find(|t| t.0 == name).map(|t| t.1.as_slice()).unwrap_or(&[])
}

fn table_str(m: &Manifest, name: &str, key: &str) -> Option<String> {
    match table(m, name).iter().find(|(k, _)| k == key) {
        Some((_, Some(TomlValue::Str(s)))) => Some(s.clone()),
        _ => None,
    }
}

fn dependencies(m: &Manifest, path: &str) -> PResult<BTreeMap<String, String>> {
    let mut out = BTreeMap::new();
    for (name, value) in table(m, "dependencies") {
        let Some(TomlValue::Str(req)) = value else {
            return Err(fail("package_manifest_line").arg("path", path.to_string()).arg("line", String::from("0")));
        };
        requirement(req)?;
        out.insert(name.clone(), req.clone());
    }
    Ok(out)
}

/// (kind, target) pairs of `[permissions]`; `shell = true` has no target.
fn permissions(m: &Manifest) -> Vec<(String, Option<String>)> {
    let mut out = Vec::new();
    for (kind, value) in table(m, "permissions") {
        if !crate::parser::POLICY_KINDS.contains(&kind.as_str()) {
            continue;
        }
        match value {
            Some(TomlValue::Bool(true)) if kind == "shell" => out.push((kind.clone(), None)),
            Some(TomlValue::List(items)) if kind != "shell" => {
                out.extend(items.iter().map(|t| (kind.clone(), Some(t.clone()))));
            }
            _ => {}
        }
    }
    out
}

/// Write `name = "req"` in the [dependencies] table of nexo.toml.
fn set_dependency(path: &Path, name: &str, req: &str) -> std::io::Result<()> {
    let text = std::fs::read_to_string(path)?;
    let mut lines: Vec<String> = text.lines().map(String::from).collect();
    let entry = format!("{name} = \"{req}\"");
    match lines.iter().position(|l| l.trim() == "[dependencies]") {
        None => {
            while lines.last().is_some_and(|l| l.trim().is_empty()) {
                lines.pop();
            }
            lines.extend([String::from(""), String::from("[dependencies]"), entry]);
        }
        Some(start) => {
            let mut end = start + 1;
            while end < lines.len() && !lines[end].trim().starts_with('[') {
                end += 1;
            }
            let found = (start + 1..end).find(|&i| lines[i].split('=').next().unwrap().trim().trim_matches('"') == name);
            match found {
                Some(i) => lines[i] = entry,
                None => {
                    let mut last = end;
                    while last > start + 1 && lines[last - 1].trim().is_empty() {
                        last -= 1;
                    }
                    lines.insert(last, entry);
                }
            }
        }
    }
    std::fs::write(path, lines.join("\n") + "\n")
}

// -------------------------------------------------------------------- json

/// The JSON both phases write byte for byte: keys sorted, no spaces.
fn canonical(v: &Json) -> String {
    match v {
        Json::Obj(pairs) => {
            let mut sorted: Vec<&(String, Json)> = pairs.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            let items: Vec<String> = sorted.iter().map(|(k, v)| format!("{}:{}", json::s(k.clone()).text(), canonical(v))).collect();
            format!("{{{}}}", items.join(","))
        }
        Json::Arr(items) => format!("[{}]", items.iter().map(canonical).collect::<Vec<_>>().join(",")),
        other => other.text(),
    }
}

// ---------------------------------------------------------------- registry

pub fn registry() -> String {
    std::env::var("NEXO_REGISTRY").ok().filter(|r| !r.is_empty()).unwrap_or_else(|| REGISTRY.to_string())
}

fn is_url(place: &str) -> bool {
    place.starts_with("https://") || place.starts_with("http://")
}

fn registry_error(detail: String) -> Diagnostic {
    fail("package_registry").arg("registry", registry()).arg("detail", detail)
}

fn quote_path(text: &str) -> String {
    let mut out = String::new();
    for b in text.bytes() {
        if b.is_ascii_alphanumeric() || b"/_.-~".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// A file of the registry, or None when it has no such file.
fn fetch(name: &str, file: &str) -> PResult<Option<Vec<u8>>> {
    let place = registry();
    if !is_url(&place) {
        return match std::fs::read(Path::new(&place).join(name).join(file)) {
            Ok(data) => Ok(Some(data)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(registry_error(io_reason(&e))),
        };
    }
    let host = nexo_runtime::http::url_host(&place);
    if place.starts_with("http://") && !["localhost", "127.0.0.1", "::1"].contains(&host.as_str()) {
        return Err(registry_error(String::from("HTTPS is needed for any host but this machine")));
    }
    let url = format!("{}/{}", place.trim_end_matches('/'), quote_path(&format!("{name}/{file}")));
    let mut b = ureq::AgentBuilder::new().timeout(std::time::Duration::from_secs(30)).user_agent("nexo/0.1");
    if let Some(p) = nexo_runtime::http::proxy_for(&url) {
        b = b.proxy(p);
    }
    match b.build().get(&url).call() {
        Ok(r) => {
            let mut data = Vec::new();
            std::io::Read::read_to_end(&mut r.into_reader(), &mut data).map_err(|e| registry_error(io_reason(&e)))?;
            Ok(Some(data))
        }
        Err(ureq::Error::Status(404, _)) => Ok(None),
        Err(ureq::Error::Status(code, _)) => Err(registry_error(format!("HTTP {code}"))),
        Err(ureq::Error::Transport(t)) => Err(registry_error(t.to_string())),
    }
}

/// An I/O error as Python's strerror writes it.
fn io_reason(e: &std::io::Error) -> String {
    let text = e.to_string();
    text.split(" (os error").next().unwrap_or(&text).to_string()
}

fn index(name: &str) -> PResult<Vec<Json>> {
    let Some(data) = fetch(name, "index.json")? else {
        return Err(fail("package_unknown").arg("name", name.to_string()).arg("registry", registry()));
    };
    let doc = String::from_utf8(data).ok().and_then(|t| json::parse(&t));
    match doc {
        Some(d @ Json::Obj(_)) if matches!(d.get("versions"), Json::Arr(_)) => Ok(d
            .get("versions")
            .arr()
            .iter()
            .filter(|e| e.get("version").str().and_then(version).is_some())
            .cloned()
            .collect()),
        _ => Err(registry_error(format!("{name}/index.json is not an index"))),
    }
}

fn verify(data: &[u8], key: &str, signature: &str) -> bool {
    use nexo_runtime::crypto::unb64;
    let (Some(k), Some(s)) = (unb64(key), unb64(signature)) else { return false };
    let (Ok(k), Ok(s)) = (<[u8; 32]>::try_from(k.as_slice()), <[u8; 64]>::try_from(s.as_slice())) else { return false };
    let Ok(public) = ed25519_dalek::VerifyingKey::from_bytes(&k) else { return false };
    public.verify(data, &ed25519_dalek::Signature::from_bytes(&s)).is_ok()
}

fn sha256_hex(data: &[u8]) -> String {
    Sha256::digest(data).iter().map(|b| format!("{b:02x}")).collect()
}

fn valid_file(path: &str) -> bool {
    if path == "nexo.toml" || path == "README.md" {
        return true;
    }
    let ok_part = |p: &str| {
        p.chars().next().is_some_and(|c| c.is_ascii_alphanumeric() || c == '_')
            && p.chars().all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c))
    };
    path.ends_with(".nexo") && path.split('/').all(|p| ok_part(p) && p != "..")
}

/// One version of a package, downloaded and checked.
pub struct Package {
    pub name: String,
    pub version: String,
    sha256: String,
    key: String,
    advisories: Vec<Json>,
    files: BTreeMap<String, String>,
    requires: BTreeMap<String, String>,
    permissions: Vec<(String, Option<String>)>,
}

type Locked = BTreeMap<String, BTreeMap<String, String>>;

fn download(name: &str, entry: &Json, locked: Option<&BTreeMap<String, String>>) -> PResult<Package> {
    let ver = entry.get("version").str().unwrap_or("").to_string();
    let bad = |detail: &str| {
        fail("package_bad").arg("name", name.to_string()).arg("version", ver.clone()).arg("detail", detail.to_string())
    };
    let Some(data) = fetch(name, &format!("{name}-{ver}.json"))? else {
        return Err(bad("the registry has no archive of it"));
    };
    let digest = sha256_hex(&data);
    let named = |key: &'static str| fail(key).arg("name", name.to_string()).arg("version", ver.clone());
    if let Some(l) = locked {
        if l.get("version") == Some(&ver) && l.get("sha256") != Some(&digest) {
            return Err(named("package_hash"));
        }
    }
    let key = entry.get("key").str().unwrap_or("").to_string();
    if entry.get("sha256").str() != Some(digest.as_str()) || !verify(&data, &key, entry.get("signature").str().unwrap_or("")) {
        return Err(named("package_signature"));
    }
    if locked.is_some_and(|l| l.get("key") != Some(&key)) {
        return Err(named("package_key"));
    }
    let doc = String::from_utf8(data).ok().and_then(|t| json::parse(&t));
    let mut files = BTreeMap::new();
    let ok = match doc.as_ref().map(|d| d.get("files")) {
        Some(Json::Obj(pairs)) => pairs.iter().all(|(p, t)| match t {
            Json::Str(text) if valid_file(p) => {
                files.insert(p.clone(), text.clone());
                true
            }
            _ => false,
        }),
        _ => false,
    };
    if !ok {
        return Err(bad("its archive is not a list of package files"));
    }
    let path = format!("{name} {ver}/nexo.toml");
    let manifest = read_manifest(files.get("nexo.toml").map(String::as_str).unwrap_or(""), &path)?;
    if table_str(&manifest, "project", "name").as_deref() != Some(name)
        || table_str(&manifest, "project", "version").as_deref() != Some(ver.as_str())
    {
        return Err(bad("its nexo.toml names another package or version"));
    }
    Ok(Package {
        name: name.to_string(),
        version: ver.clone(),
        sha256: digest,
        key,
        advisories: entry.get("advisories").arr().to_vec(),
        requires: dependencies(&manifest, &path)?,
        permissions: permissions(&manifest),
        files,
    })
}

// -------------------------------------------------------------- resolution

/// One version of each package: the highest that every requirement on it
/// accepts, or the locked one while it still does.
fn resolve(wanted: &BTreeMap<String, String>, locked: &Locked) -> PResult<BTreeMap<String, Package>> {
    let mut chosen: BTreeMap<String, Package> = BTreeMap::new();
    let mut indexes: BTreeMap<String, Vec<Json>> = BTreeMap::new();
    for _ in 0..100 {
        let mut reqs: BTreeMap<String, Vec<(String, String)>> = BTreeMap::new();
        for (name, req) in wanted {
            reqs.entry(name.clone()).or_default().push((req.clone(), String::from("nexo.toml")));
        }
        for p in chosen.values() {
            for (name, req) in &p.requires {
                reqs.entry(name.clone()).or_default().push((req.clone(), format!("{} {}", p.name, p.version)));
            }
        }
        let mut changed = false;
        for (name, rs) in &reqs {
            if !indexes.contains_key(name) {
                indexes.insert(name.clone(), index(name)?);
            }
            let mut versions: Vec<(V3, &Json)> =
                indexes[name].iter().map(|e| (version(e.get("version").str().unwrap()).unwrap(), e)).collect();
            versions.sort_by_key(|v| v.0);
            let fits: Vec<&(V3, &Json)> = versions.iter().filter(|(v, _)| rs.iter().all(|(r, _)| accepts(r, *v))).collect();
            if fits.is_empty() {
                for (r, _) in rs {
                    requirement(r)?;
                }
                return Err(no_version(name, rs, &versions.iter().map(|v| v.0).collect::<Vec<_>>()));
            }
            let lock = locked.get(name);
            let pick = fits
                .iter()
                .find(|f| lock.is_some_and(|l| l.get("version") == Some(&shown_version(f.0))))
                .unwrap_or(fits.last().unwrap());
            let ver = pick.1.get("version").str().unwrap();
            if chosen.get(name).map(|p| p.version.as_str()) != Some(ver) {
                let p = download(name, pick.1, lock)?;
                chosen.insert(name.clone(), p);
                changed = true;
            }
        }
        let gone: Vec<String> = chosen.keys().filter(|n| !reqs.contains_key(*n)).cloned().collect();
        for n in gone {
            chosen.remove(&n);
            changed = true;
        }
        if !changed {
            return Ok(chosen);
        }
    }
    Err(fail("package_conflict_loop"))
}

fn no_version(name: &str, reqs: &[(String, String)], versions: &[V3]) -> Diagnostic {
    for (i, (a, who_a)) in reqs.iter().enumerate() {
        for (b, who_b) in &reqs[i + 1..] {
            if !versions.iter().any(|v| accepts(a, *v) && accepts(b, *v)) {
                return fail("package_conflict")
                    .arg("name", name.to_string())
                    .arg("first", who_a.clone())
                    .arg("a", a.clone())
                    .arg("second", who_b.clone())
                    .arg("b", b.clone());
            }
        }
    }
    let shown: Vec<String> = versions.iter().map(|v| shown_version(*v)).collect();
    fail("package_no_version")
        .arg("name", name.to_string())
        .arg("requirement", reqs[0].0.clone())
        .arg("versions", if shown.is_empty() { String::from("none") } else { shown.join(", ") })
}

// -------------------------------------------------------------------- lock

pub fn read_lock(root: &Path) -> Locked {
    let mut out = Locked::new();
    let Ok(text) = std::fs::read_to_string(root.join("nexo.lock")) else { return out };
    let mut entry: Option<BTreeMap<String, String>> = None;
    let mut flush = |e: Option<BTreeMap<String, String>>, out: &mut Locked| {
        if let Some(e) = e {
            if let Some(n) = e.get("name") {
                out.insert(n.clone(), e);
            }
        }
    };
    for line in text.lines() {
        let line = line.trim();
        if line == "[[package]]" {
            flush(entry.take(), &mut out);
            entry = Some(BTreeMap::new());
            continue;
        }
        if let (Some(e), Some((k, v))) = (entry.as_mut(), line.split_once('=')) {
            if let Some(TomlValue::Str(s)) = toml_value(v) {
                e.insert(k.trim().to_string(), s);
            }
        }
    }
    flush(entry, &mut out);
    out
}

fn write_lock(root: &Path, chosen: &BTreeMap<String, Package>) -> std::io::Result<()> {
    let mut text = LOCK_HEADER.to_string();
    for (name, p) in chosen {
        let deps: Vec<String> = p.requires.iter().map(|(d, r)| format!("\"{d} {r}\"")).collect();
        text.push_str(&format!(
            "\n[[package]]\nname = \"{name}\"\nversion = \"{}\"\nsha256 = \"{}\"\nkey = \"{}\"\ndependencies = [{}]\n",
            p.version,
            p.sha256,
            p.key,
            deps.join(", ")
        ));
    }
    std::fs::write(root.join("nexo.lock"), text)
}

pub fn installed_dir(root: &Path, name: &str) -> PathBuf {
    root.join(".nexo").join("packages").join(name)
}

fn install(root: &Path, chosen: &BTreeMap<String, Package>) -> std::io::Result<()> {
    let base = root.join(".nexo").join("packages");
    if let Ok(entries) = std::fs::read_dir(&base) {
        for old in entries.flatten() {
            if !chosen.contains_key(&*old.file_name().to_string_lossy()) {
                let _ = std::fs::remove_dir_all(old.path());
            }
        }
    }
    for (name, p) in chosen {
        let target = installed_dir(root, name);
        let _ = std::fs::remove_dir_all(&target);
        for (rel, text) in &p.files {
            let path = target.join(rel);
            std::fs::create_dir_all(path.parent().unwrap())?;
            std::fs::write(path, text)?;
        }
    }
    Ok(())
}

// ------------------------------------------------------- permissions, audit

fn nexo_files(dir: &Path, root: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    for e in entries.flatten() {
        let path = e.path();
        if e.file_name().to_string_lossy().starts_with('.') {
            continue;
        }
        if path.is_dir() {
            nexo_files(&path, root, out);
        } else if path.extension().is_some_and(|x| x == "nexo") {
            out.push(path);
        }
    }
}

type Rules = Vec<(String, String, Vec<String>)>;

/// (file, rules) of every program of the project that has a policy.
fn program_policies(root: &Path) -> Vec<(String, Rules)> {
    let mut files = Vec::new();
    nexo_files(root, root, &mut files);
    let mut rels: Vec<String> =
        files.iter().filter_map(|f| f.strip_prefix(root).ok()).map(|r| r.to_string_lossy().into_owned()).collect();
    rels.sort();
    let mut out = Vec::new();
    for rel in rels {
        let Ok(source) = std::fs::read_to_string(root.join(&rel)) else { continue };
        let Ok(program) = crate::parser::parse(&source) else { continue };
        if let Some((_, rules)) = crate::policy::declared(&program) {
            out.push((rel, rules));
        }
    }
    out
}

fn allowed(rules: &Rules, kind: &str, target: Option<&str>) -> bool {
    use nexo_runtime::security::matching::matches;
    let mut ok = false;
    for (rule, k, targets) in rules {
        if k != kind {
            continue;
        }
        let hit = targets.is_empty() || target.is_none_or(|t| targets.iter().any(|x| matches(kind, t, x)));
        if hit && rule == "deny" {
            return false;
        }
        ok = ok || hit;
    }
    ok
}

fn permission_text(kind: &str, target: Option<&str>) -> String {
    match target {
        None => msg("detail.package_shell", &[]),
        Some(t) => msg(&format!("detail.policy_{kind}"), &[("target", t.to_string())]),
    }
}

fn check_permissions(root: &Path, chosen: &BTreeMap<String, Package>) -> PResult<()> {
    let policies = program_policies(root);
    for (name, p) in chosen {
        for (kind, target) in &p.permissions {
            for (file, rules) in &policies {
                if !allowed(rules, kind, target.as_deref()) {
                    let line = match target {
                        None => format!("allow {kind}"),
                        Some(t) => format!("allow {kind} \"{t}\""),
                    };
                    return Err(fail("package_permissions")
                        .arg("name", name.clone())
                        .arg("file", file.clone())
                        .arg("what", permission_text(kind, target.as_deref()))
                        .arg("line", line));
                }
            }
        }
    }
    Ok(())
}

fn check_advisories(chosen: &BTreeMap<String, Package>, accept_risk: bool) -> PResult<Vec<String>> {
    let mut warnings = Vec::new();
    for (name, p) in chosen {
        for a in &p.advisories {
            let field = |k: &str| a.get(k).str().unwrap_or("").to_string();
            let severity = field("severity").to_lowercase();
            if severity == "critical" && !accept_risk {
                return Err(fail("package_advisory")
                    .arg("name", name.clone())
                    .arg("version", p.version.clone())
                    .arg("id", field("id"))
                    .arg("summary", field("summary")));
            }
            warnings.push(msg(
                "info.package_advisory",
                &[
                    ("name", name.clone()),
                    ("version", p.version.clone()),
                    ("id", field("id")),
                    ("severity", severity),
                    ("summary", field("summary")),
                ],
            ));
        }
    }
    Ok(warnings)
}

// ------------------------------------------------------------------ add

fn project() -> PResult<(PathBuf, Manifest)> {
    let cwd = std::env::current_dir().unwrap_or_default();
    let root = crate::modules::project_root(&cwd.join("x.nexo").to_string_lossy());
    let Ok(text) = std::fs::read_to_string(root.join("nexo.toml")) else { return Err(fail("package_project")) };
    Ok((root, read_manifest(&text, "nexo.toml")?))
}

fn io_fail(e: std::io::Error) -> Diagnostic {
    registry_error(io_reason(&e))
}

/// `nexo add [name] [--accept-risk]`: the lines it prints.
pub fn add(name: Option<&str>, accept_risk: bool) -> PResult<Vec<String>> {
    let (root, manifest) = project()?;
    let mut wanted = dependencies(&manifest, "nexo.toml")?;
    let locked = read_lock(&root);
    if let Some(name) = name {
        if !valid_name(name) {
            return Err(fail("package_name").arg("name", name.to_string()));
        }
        if !wanted.contains_key(name) {
            let entries = index(name)?;
            let Some(newest) = entries.iter().filter_map(|e| version(e.get("version").str()?)).max() else {
                return Err(fail("package_no_version")
                    .arg("name", name.to_string())
                    .arg("requirement", String::from("^0.0"))
                    .arg("versions", String::from("none")));
            };
            wanted.insert(name.to_string(), format!("^{}.{}", newest.0, newest.1));
        }
    }
    let chosen = resolve(&wanted, &locked)?;
    check_permissions(&root, &chosen)?;
    let mut lines = check_advisories(&chosen, accept_risk)?;
    if let Some(name) = name {
        set_dependency(&root.join("nexo.toml"), name, &wanted[name]).map_err(io_fail)?;
    }
    install(&root, &chosen).map_err(io_fail)?;
    write_lock(&root, &chosen).map_err(io_fail)?;
    for (n, p) in &chosen {
        lines.push(msg("info.package_line", &[("name", n.clone()), ("version", p.version.clone())]));
        for (kind, target) in &p.permissions {
            lines.push(msg("info.package_permission", &[("what", permission_text(kind, target.as_deref()))]));
        }
    }
    lines.push(match name {
        Some(name) => msg("info.package_added", &[("name", name.to_string()), ("version", chosen[name].version.clone())]),
        None => msg("info.package_installed", &[("count", chosen.len().to_string())]),
    });
    Ok(lines)
}

// ------------------------------------------------------------------ publish

fn config_dir() -> PathBuf {
    match std::env::var_os("XDG_CONFIG_HOME").filter(|v| !v.is_empty()) {
        Some(base) => PathBuf::from(base).join("nexo"),
        None => PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".config").join("nexo"),
    }
}

/// The private key of this user (made on the first publish).
fn publish_key() -> std::io::Result<ed25519_dalek::SigningKey> {
    use nexo_runtime::crypto::{b64, unb64};
    let path = config_dir().join("publish.key");
    if let Ok(text) = std::fs::read_to_string(&path) {
        if let Some(seed) = unb64(text.trim()).and_then(|s| <[u8; 32]>::try_from(s.as_slice()).ok()) {
            return Ok(ed25519_dalek::SigningKey::from_bytes(&seed));
        }
    }
    let mut seed = [0u8; 32];
    getrandom::getrandom(&mut seed).map_err(|e| std::io::Error::other(e.to_string()))?;
    std::fs::create_dir_all(path.parent().unwrap())?;
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut opts, 0o600);
    std::io::Write::write_all(&mut opts.open(&path)?, format!("{}\n", b64(&seed)).as_bytes())?;
    Ok(ed25519_dalek::SigningKey::from_bytes(&seed))
}

fn package_files(dir: &Path, root: &Path, out: &mut BTreeMap<String, String>) -> std::io::Result<()> {
    let mut entries: Vec<_> = std::fs::read_dir(dir)?.flatten().collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let path = e.path();
        if e.file_name().to_string_lossy().starts_with('.') {
            continue;
        }
        let rel = path.strip_prefix(root).unwrap().to_string_lossy().replace('\\', "/");
        if path.is_dir() {
            package_files(&path, root, out)?;
        } else if valid_file(&rel) {
            out.insert(rel, std::fs::read_to_string(&path)?);
        }
    }
    Ok(())
}

pub fn publish() -> PResult<String> {
    use nexo_runtime::crypto::b64;
    let (root, manifest) = project()?;
    let place = registry();
    if is_url(&place) {
        return Err(fail("publish_registry").arg("registry", place));
    }
    let name = table_str(&manifest, "project", "name").filter(|n| valid_name(n));
    let Some(name) = name else {
        return Err(fail("publish_manifest").arg("field", String::from("name = \"letters-and-digits\"")));
    };
    let ver = table_str(&manifest, "project", "version").filter(|v| version(v).is_some());
    let Some(ver) = ver else {
        return Err(fail("publish_manifest").arg("field", String::from("version = \"MAJOR.MINOR.PATCH\"")));
    };
    dependencies(&manifest, "nexo.toml")?;
    let folder = Path::new(&place).join(&name);
    let index_path = folder.join("index.json");
    let mut entries: Vec<Json> = std::fs::read_to_string(&index_path)
        .ok()
        .and_then(|t| json::parse(&t))
        .map(|d| d.get("versions").arr().to_vec())
        .unwrap_or_default();
    if entries.iter().any(|e| e.get("version").str() == Some(ver.as_str())) {
        return Err(fail("publish_exists").arg("name", name).arg("version", ver));
    }
    let mut files = BTreeMap::new();
    package_files(&root, &root, &mut files).map_err(io_fail)?;
    let files = Json::Obj(files.into_iter().map(|(k, v)| (k, Json::Str(v))).collect());
    let data = canonical(&json::obj(vec![("files", files)])).into_bytes();
    let signing = publish_key().map_err(io_fail)?;
    let key = b64(signing.verifying_key().as_bytes());
    let signature = b64(&signing.sign(&data).to_bytes());
    std::fs::create_dir_all(&folder).map_err(io_fail)?;
    std::fs::write(folder.join(format!("{name}-{ver}.json")), &data).map_err(io_fail)?;
    entries.push(json::obj(vec![
        ("version", json::s(ver.clone())),
        ("sha256", json::s(sha256_hex(&data))),
        ("key", json::s(key.clone())),
        ("signature", json::s(signature)),
    ]));
    entries.sort_by_key(|e| e.get("version").str().and_then(version).unwrap_or((0, 0, 0)));
    let doc = json::obj(vec![("name", json::s(name.clone())), ("versions", Json::Arr(entries))]);
    std::fs::write(&index_path, canonical(&doc) + "\n").map_err(io_fail)?;
    Ok(msg("info.package_published", &[("name", name), ("version", ver), ("registry", place), ("public", key)]))
}

// ------------------------------------------------------------- for `use`

/// {import name: package} of the dependencies a nexo.toml lists.
pub fn scope(manifest_path: &Path) -> BTreeMap<String, String> {
    let Ok(text) = std::fs::read_to_string(manifest_path) else { return BTreeMap::new() };
    let Ok(m) = read_manifest(&text, &manifest_path.to_string_lossy()) else { return BTreeMap::new() };
    table(&m, "dependencies").iter().map(|(n, _)| (import_name(n), n.clone())).collect()
}
