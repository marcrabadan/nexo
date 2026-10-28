//! Ingress grants without filesystem writes/process delegation, frozen by the launcher and initialized
//! before application code. The runtime never reloads the operator file.
use std::path::Path;
use std::sync::{Arc, OnceLock};
use super::{Effect, Engine, Kind, Rule};
use crate::json::Json;

pub const SNAPSHOT: &str = "NEXO_SECURITY_INGRESS_SNAPSHOT";
const MAX: usize = 65536;
struct Configuration { server: Option<String>, engine: Arc<Engine>, routes: Option<Vec<(String, String, Arc<Engine>)>> }
static CONFIG: OnceLock<Configuration> = OnceLock::new();

fn document(text: &str) -> Result<Json, String> {
    if text.len() > MAX { return Err("Ingress configuration exceeds 64 KiB".into()); }
    let (mut depth, mut quote, mut escape) = (0i32, false, false);
    for b in text.bytes() {
        if quote { if escape { escape = false; } else if b == b'\\' { escape = true; } else if b == b'"' { quote = false; } }
        else if b == b'"' { quote = true; }
        else if b == b'[' || b == b'{' { depth += 1; if depth > 16 { return Err("Ingress configuration is too deeply nested".into()); } }
        else if b == b']' || b == b'}' { depth -= 1; if depth < 0 { return Err("Invalid ingress configuration".into()); } }
    }
    let doc = crate::json::parse_unique(text).ok_or("Invalid ingress JSON")?;
    let Json::Obj(fields) = &doc else { return Err("Expected ingress object".into()); };
    let version = doc.get("version").int();
    let keys: &[&str] = match version { Some(1) => &["version", "application", "server", "port", "permissions"], Some(2) => &["version", "application", "server", "port", "permissions", "routes"], _ => return Err("Unsupported ingress version".into()) };
    if fields.len() != keys.len() || fields.iter().any(|(k, _)| !keys.contains(&k.as_str())) {
        return Err("Unknown or missing ingress fields/version".into());
    }
    let Some(server) = doc.get("server").str() else { return Err("Invalid server identity".into()); };
    if !text_value(server, 256) { return Err("Invalid server identity".into()); }
    if !doc.get("port").int().is_some_and(|p| (0..=65535).contains(&p)) { return Err("Invalid ingress port".into()); }
    let Some(app) = doc.get("application").str() else { return Err("Invalid application identity".into()); };
    if !text_value(app, 4096) { return Err("Invalid application identity".into()); }
    Ok(doc)
}
fn text_value(text: &str, max: usize) -> bool {
    !text.is_empty() && text.len() <= max && !text.chars().any(|c| c < ' ' || c == '\u{7f}')
}
fn rules(doc: &mut Json, directory: Option<&Path>) -> Result<Vec<Rule>, String> {
    let Json::Arr(entries) = doc.get_mut("permissions").ok_or("Missing permissions")? else { return Err("Expected ingress permissions array".into()); };
    if entries.len() > 128 { return Err("Too many ingress rules".into()); }
    let mut rules = Vec::new();
    for entry in entries {
        let Json::Arr(words) = entry else { return Err("Expected permission rule array".into()); };
        if words.len() < 3 || words.len() > 66 { return Err("Invalid permission rule length".into()); }
        let effect = match words[0].str() { Some("allow") => Effect::Allow, Some("deny") => Effect::Deny, _ => return Err("Invalid permission effect".into()) };
        let kind = match words[1].str() { Some("read") => Kind::Read, Some("env") => Kind::Env, Some("net") => Kind::Net, _ => return Err("Ingress permits only read, env and net rules".into()) };
        let mut targets = Vec::new();
        for word in &mut words[2..] {
            let Some(raw) = word.str() else { return Err("Permission targets must be text".into()); };
            if !text_value(raw, 4096) { return Err("Invalid permission target".into()); }
            let target = match kind {
                Kind::Read => {
                    if let Some(directory) = directory {
                        directory.join(raw).canonicalize().map_err(|_| "Ingress read targets must exist")?.to_str().ok_or("Invalid ingress path encoding")?.to_string()
                    } else {
                        if !Path::new(raw).is_absolute() { return Err("Ingress snapshot read targets must be absolute".into()); }
                        raw.to_string()
                    }
                }
                Kind::Env => {
                    if !raw.bytes().enumerate().all(|(i,b)| b == b'_' || b.is_ascii_alphabetic() || (i > 0 && b.is_ascii_digit())) { return Err("Ingress env targets must be literal names".into()); }
                    raw.to_string()
                }
                Kind::Net => {
                    let host = raw.strip_prefix("*.").unwrap_or(raw);
                    if host.is_empty() || host.len() > 253 || !host.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
                        || host.split('.').any(|label| label.is_empty() || label.len() > 63 || label.starts_with('-') || label.ends_with('-')) {
                        return Err("Ingress net targets must be DNS names or IPv4 addresses, optionally *.domain".into());
                    }
                    raw.to_ascii_lowercase()
                }
                _ => unreachable!(),
            };
            *word = Json::Str(target.clone());
            targets.push(target);
        }
        rules.push(Rule { effect, kind, targets });
    }
    Ok(rules)
}
/// Match exact registered templates, never a request-controlled path or query.
fn route_rules(doc: &mut Json, directory: Option<&Path>) -> Result<Option<Vec<(String, String, Arc<Engine>)>>, String> {
    if doc.get("version").int() == Some(1) { return Ok(None); }
    let Json::Arr(routes) = doc.get_mut("routes").ok_or("Missing ingress routes")? else { return Err("Expected ingress routes array".into()); };
    if routes.len() > 128 { return Err("Too many ingress routes".into()); }
    let mut result: Vec<(String, String, Arc<Engine>)> = Vec::new();
    for route in routes {
        let Json::Obj(fields) = route else { return Err("Expected route object".into()); };
        if fields.len() != 3 || fields.iter().any(|(k,_)| !["method", "path", "permissions"].contains(&k.as_str())) { return Err("Unknown or missing route fields".into()); }
        let method = route.get("method").str().ok_or("Invalid route method")?.to_string();
        let path = route.get("path").str().ok_or("Invalid route path")?.to_string();
        if !["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].contains(&method.as_str())
            || !text_value(&path, 8192) || !path.starts_with('/') || path.contains(['?', '#']) {
            return Err("Invalid route identity".into());
        }
        if result.iter().any(|(m,p,_)| m == &method && p == &path) { return Err("Duplicate ingress route".into()); }
        let engine = Arc::new(Engine::with_resolved_paths(rules(route, directory)?));
        result.push((method, path, engine));
    }
    Ok(Some(result))
}
/// Freeze identities and read roots before application code can mutate files.
pub fn prepare(path: &Path, application: &Path, port: u16) -> Result<String, String> {
    use std::io::Read;
    let path = path.canonicalize().map_err(|_| "Cannot resolve ingress configuration")?;
    if !path.is_file() { return Err("Ingress configuration must be a regular file".into()); }
    let mut text = String::new();
    std::fs::File::open(&path).map_err(|_| "Cannot read ingress configuration")?.take((MAX+1) as u64)
        .read_to_string(&mut text).map_err(|_| "Cannot read ingress configuration")?;
    let mut doc = document(&text)?;
    let directory = path.parent().ok_or("Invalid ingress directory")?;
    let declared = directory.join(doc.get("application").str().unwrap()).canonicalize().map_err(|_| "Cannot resolve ingress application")?;
    let actual = application.canonicalize().map_err(|_| "Cannot resolve application")?;
    if declared != actual || !actual.is_file() || doc.get("port").int() != Some(i64::from(port)) { return Err("Ingress identity or port mismatch".into()); }
    doc.set("application", Json::Str(actual.to_str().ok_or("Invalid application encoding")?.to_string()));
    rules(&mut doc, Some(directory))?;
    route_rules(&mut doc, Some(directory))?;
    let wire = doc.compact();
    if wire.len() > MAX { return Err("Frozen ingress configuration exceeds 64 KiB".into()); }
    let mut frozen = document(&wire)?;
    rules(&mut frozen, None)?;
    route_rules(&mut frozen, None)?;
    Ok(wire)
}
pub fn initialize() -> Result<(), String> {
    let config = if let Some(text) = std::env::var_os(SNAPSHOT) {
        let text = text.to_str().ok_or("Invalid ingress snapshot encoding")?;
        let mut doc = document(text)?;
        if !Path::new(doc.get("application").str().unwrap()).is_absolute() || doc.get("port").int() != super::ingress::port().map(i64::from) {
            return Err("Invalid ingress snapshot identity or port".into());
        }
        let engine = Arc::new(Engine::with_resolved_paths(rules(&mut doc, None)?));
        let routes = route_rules(&mut doc, None)?;
        Configuration { server: Some(doc.get("server").str().unwrap().to_string()), engine, routes }
    } else { Configuration { server: None, engine: Arc::new(Engine::new(Vec::new())), routes: None } };
    CONFIG.set(config).map_err(|_| "Ingress configuration already initialized".into())
}
pub fn server_matches(server: &str) -> bool {
    CONFIG.get().is_some_and(|c| c.server.as_ref().is_none_or(|name| name == server))
}
pub fn engine() -> Arc<Engine> {
    CONFIG.get().map(|c| c.engine.clone()).unwrap_or_else(|| Arc::new(Engine::new(Vec::new())))
}

/// Version 1 keeps the server ceiling; version 2 denies capabilities for unmatched routes.
pub fn route_engine(method: &str, path: &str) -> Option<Arc<Engine>> {
    let routes = CONFIG.get()?.routes.as_ref()?;
    Some(routes.iter().find(|(m,p,_)| m == method && p == path).map(|(_,_,e)| e.clone())
        .unwrap_or_else(|| Arc::new(Engine::new(Vec::new()))))
}
