//! nexo.web: `server` blocks, routes, `jwt`, `@role`, `@limit` (section 15,
//! D102-D104). A server answers HTTP/1.1 one request at a time (each
//! connection is closed after its answer). It starts when the top level of
//! the program has run, or, under `nexo test`, on a free port before the
//! tests. Parsing and answers follow phase 0's web.py line by line.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Mutex;

use hmac::{Hmac, Mac};
use nexo_diag::{msg, Diagnostic};
use sha2::Sha256;

use crate::fmt::repr;
use crate::state::{diag, failure, R};
use crate::stdlib::{expects, method_args, missing_member};
use crate::types::TypeRef;
use crate::value::*;

const MAX_HEAD: usize = 64 * 1024;
const MAX_BODY: usize = 1024 * 1024;

pub fn reason(status: i64) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        204 => "No Content",
        301 => "Moved Permanently",
        302 => "Found",
        304 => "Not Modified",
        308 => "Permanent Redirect",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        411 => "Length Required",
        413 => "Payload Too Large",
        415 => "Unsupported Media Type",
        422 => "Unprocessable Content",
        429 => "Too Many Requests",
        431 => "Request Header Fields Too Large",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        503 => "Service Unavailable",
        _ => "",
    }
}

/// secure headers on every answer (section 15: on by default)
const SECURE: &[(&str, &str)] = &[
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    ("Referrer-Policy", "no-referrer"),
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Cache-Control", "no-store"),
];

fn window(unit: &str) -> Option<i64> {
    Some(match unit {
        "second" | "seconds" => 1,
        "minute" | "minutes" => 60,
        "hour" | "hours" => 3600,
        "day" | "days" => 86400,
        "month" | "months" => 2592000,
        _ => return None,
    })
}

fn now_secs() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

// ------------------------------------------------------------------ rates

/// `10 per minute per ip` (D93).
pub struct Rate {
    pub(crate) amount: Val,
    pub(crate) unit: String,
    pub(crate) per: Option<String>,
}

impl Native for Rate {
    fn type_name(&self) -> &str {
        "rate"
    }
    fn repr(&self) -> String {
        let mut t = format!("{} per {}", repr(self.amount.v()), self.unit);
        if let Some(p) = &self.per {
            t.push_str(&format!(" per {p}"));
        }
        t
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

pub fn rate(args: &[V], span: u32) -> R<Val> {
    let words: Vec<String> = args[1..].iter().map(|w| w.as_str().unwrap_or("").to_string()).collect();
    if words.is_empty() || window(&words[0]).is_none() {
        return Err(diag(Diagnostic::new_spanless("rate_unit").arg("unit", words.first().cloned().unwrap_or_default()), span));
    }
    if words.len() > 2 || (words.len() == 2 && words[1] != "ip" && words[1] != "user") {
        return Err(diag(Diagnostic::new_spanless("rate_per").arg("per", words.last().unwrap().clone()), span));
    }
    Ok(native(Rate { amount: Val::from(args[0].plain()), unit: words[0].clone(), per: words.get(1).cloned() }))
}

// -------------------------------------------------------------------- jwt

fn b64url(data: &[u8]) -> String {
    crate::crypto::b64(data).trim_end_matches('=').replace('+', "-").replace('/', "_")
}

fn unb64url(text: &str) -> Option<Vec<u8>> {
    let mut t = text.replace('-', "+").replace('_', "/");
    while t.len() % 4 != 0 {
        t.push('=');
    }
    crate::crypto::unb64(&t)
}

/// Tokens signed with HS256 (D103).
pub struct Jwt {
    key: Val,
}

impl Jwt {
    fn secret(&self, span: u32) -> R<Vec<u8>> {
        Ok(crate::crypto::text_or_secret("jwt", self.key.v(), span)?.into_bytes())
    }

    fn mac(&self, text: &str, span: u32) -> R<Vec<u8>> {
        let mut m = Hmac::<Sha256>::new_from_slice(&self.secret(span)?).unwrap();
        m.update(text.as_bytes());
        Ok(m.finalize().into_bytes().to_vec())
    }

    /// The claims of a valid token, or None.
    pub fn claims(&self, token: &str, span: u32) -> R<Option<Val>> {
        let parts: Vec<&str> = token.split('.').collect();
        if parts.len() != 3 || !token.is_ascii() {
            return Ok(None);
        }
        let mac = self.mac(&format!("{}.{}", parts[0], parts[1]), span)?;
        match unb64url(parts[2]) {
            // compared in constant time, as phase 0's hmac.compare_digest
            Some(g) if g.len() == mac.len() && g.iter().zip(&mac).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0 => {}
            _ => return Ok(None),
        }
        let decode = |p: &str| unb64url(p).and_then(|b| String::from_utf8(b).ok()).and_then(|t| crate::data::parse_json(&t).ok());
        let (Some(head), Some(body)) = (decode(parts[0]), decode(parts[1])) else { return Ok(None) };
        let head = crate::data::from_json(head, span)?;
        let body = crate::data::from_json(body, span)?;
        let Some(Obj::Map(h)) = head.get() else { return Ok(None) };
        if h.borrow().get(&Key::Str("alg".into())).and_then(|v| v.as_static_str()) != Some("HS256") {
            return Ok(None);
        }
        let Some(Obj::Map(b)) = body.get() else { return Ok(None) };
        if let Some(exp) = b.borrow().get(&Key::Str("exp".into())) {
            let n = match exp.tag {
                T_INT => exp.bits as i64 as f64,
                T_FLOAT => f64::from_bits(exp.bits),
                _ => return Ok(None),
            };
            if n < now_secs() {
                return Ok(None);
            }
        }
        Ok(Some(body))
    }
}

impl Native for Jwt {
    fn type_name(&self) -> &str {
        "jwt"
    }
    fn repr(&self) -> String {
        "jwt".into()
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        match name {
            "sign" => Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "sign"))),
            "verify" => Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "verify"))),
            _ => Err(missing_member("jwt", name, &["sign", "verify"], span)),
        }
    }
    fn call_method(&self, _me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        if name == "verify" {
            let a = method_args("verify", &["token"], 1, args, names, span)?;
            let claims = match a[0].as_str() {
                Some(t) => self.claims(t, span)?,
                None => None,
            };
            return claims.ok_or_else(|| failure("InvalidValue", span, &[("detail", msg("detail.bad_token", &[]))]));
        }
        let a = method_args("sign", &["claims", "expires"], 1, args, names, span)?;
        let Some(Obj::Map(m)) = a[0].get() else { return Err(expects("sign", "map", a[0], span)) };
        let mut entries: Vec<(Val, Val)> = m.borrow().entries.clone();
        if a[1].tag != T_MISSING && !a[1].is_none() {
            let secs = match a[1].get() {
                Some(Obj::Quantity(d, u)) if crate::num::unit_dimension(u).0 == "time" => d.mul(&crate::num::unit_dimension(u).1),
                _ => return Err(expects("expires", "duration", a[1], span)),
            };
            let exp = now_secs() as i64 + secs.to_i64().unwrap_or(0);
            entries.retain(|(k, _)| k.as_str() != Some("exp"));
            entries.push((Val::str("exp"), Val::int(exp)));
        }
        let body = crate::coll::map_from(entries);
        let bound = crate::pipe::bind(body.v())?;
        let head = b64url(b"{\"alg\":\"HS256\",\"typ\":\"JWT\"}");
        let payload = b64url(crate::data::to_json_compact(bound.v()).as_bytes());
        let mac = self.mac(&format!("{head}.{payload}"), span)?;
        Ok(Val::str(format!("{head}.{payload}.{}", b64url(&mac))))
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

pub fn jwt(key: V, span: u32) -> R<Val> {
    if key.as_str().is_none() && key.native::<crate::crypto::Secret>().is_none() {
        return Err(expects("jwt", "str, secret", key, span));
    }
    Ok(native(Jwt { key: Val::from(key) }))
}

// ----------------------------------------------------------------- server

struct Route {
    method: String,
    path: String,
    handler: Val,
    types: Vec<Val>,
    role: Option<String>,
    limit: Option<(i64, i64, Option<String>)>,
    public: bool,
    segments: Vec<String>,
    hits: Mutex<HashMap<(String, i64), i64>>,
    span: u32,
}

impl Route {
    fn matches(&self, parts: &[String]) -> Option<Vec<(String, String)>> {
        if parts.len() != self.segments.len() {
            return None;
        }
        let mut params = Vec::new();
        for (seg, part) in self.segments.iter().zip(parts) {
            if seg.starts_with('{') && seg.ends_with('}') && seg.len() >= 2 {
                params.push((seg[1..seg.len() - 1].to_string(), part.clone()));
            } else if seg != part {
                return None;
            }
        }
        Some(params)
    }
}

pub struct Server {
    name: String,
    port: Mutex<i64>,
    auth: Option<Val>,
    routes: Mutex<Vec<std::sync::Arc<Route>>>,
}

static SERVERS: Mutex<Vec<Val>> = Mutex::new(Vec::new());

pub fn release_all() {
    let old = std::mem::take(&mut *SERVERS.lock().unwrap());
    drop(old);
}

impl Native for Server {
    fn type_name(&self) -> &str {
        "server"
    }
    fn repr(&self) -> String {
        format!("server({})", self.name)
    }
    fn member(&self, _me: V, name: &str, span: u32) -> R<Val> {
        let port = *self.port.lock().unwrap();
        Ok(match name {
            "url" => Val::str(format!("http://127.0.0.1:{port}")),
            "port" => Val::int(port),
            "name" => Val::str(self.name.clone()),
            _ => return Err(missing_member("server", name, &["url", "port", "name"], span)),
        })
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

pub fn server(name: V, port: Option<V>, auth: Option<V>, span: u32) -> R<Val> {
    let p = match port {
        None => 8080,
        Some(v) => match v.as_int() {
            Some(p) if (0..=65535).contains(&p) => p,
            _ => return Err(expects("port", "int", v, span)),
        },
    };
    let auth = auth.filter(|a| !a.is_none());
    if let Some(a) = auth {
        if a.native::<Jwt>().is_none() {
            return Err(expects("auth", "jwt", a, span));
        }
    }
    let s = native(Server {
        name: name.as_str().unwrap_or("").to_string(),
        port: Mutex::new(p),
        auth: auth.map(Val::from),
        routes: Mutex::new(Vec::new()),
    });
    SERVERS.lock().unwrap().push(s.clone());
    Ok(s)
}

#[allow(clippy::too_many_arguments)]
pub fn route(server: V, method: V, path: V, handler: V, types: V, role: Option<V>, limit: Option<V>, public: Option<V>, span: u32) -> R<Val> {
    let s = server.native::<Server>().unwrap();
    let role = role.filter(|r| !r.is_none());
    let role = match role {
        None => None,
        Some(r) => match r.as_str() {
            Some(t) => Some(t.to_string()),
            None => return Err(expects("@role", "str", r, span)),
        },
    };
    if role.is_some() && s.auth.is_none() {
        return Err(diag(Diagnostic::new_spanless("role_needs_auth").arg("server", s.name.clone()), span));
    }
    let limit = match limit.filter(|l| !l.is_none()) {
        None => None,
        Some(l) => {
            let Some(r) = l.native::<Rate>() else { return Err(expects("@limit", "rate", l, span)) };
            let n = match r.amount.as_int() {
                Some(n) if n >= 1 => n,
                _ => return Err(expects("@limit", "int", r.amount.v(), span)),
            };
            Some((n, window(&r.unit).unwrap(), r.per.clone()))
        }
    };
    let method = method.as_str().unwrap_or("").to_string();
    let path = path.as_str().unwrap_or("").to_string();
    let mut routes = s.routes.lock().unwrap();
    if routes.iter().any(|r| r.method == method && r.path == path) {
        return Err(diag(Diagnostic::new_spanless("route_twice").arg("method", method).arg("path", path), span));
    }
    let trimmed = path.trim_matches('/');
    let segments = if trimmed.is_empty() { Vec::new() } else { trimmed.split('/').map(|x| x.to_string()).collect() };
    routes.push(std::sync::Arc::new(Route {
        method,
        path,
        handler: Val::from(handler),
        types: crate::pipe::collect_vec(types, span)?,
        role,
        limit,
        public: public.and_then(|p| p.as_bool()).unwrap_or(false),
        segments,
        hits: Mutex::new(HashMap::new()),
        span,
    }));
    Ok(Val::none())
}

// --------------------------------------------------------------- requests

pub struct Request {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    query: Vec<(String, String)>,
    body: Vec<u8>,
    ip: String,
    user: Val,
}

const REQUEST_MEMBERS: &[&str] = &["method", "path", "headers", "query", "body", "json", "user", "ip"];

fn pairs_map(p: &[(String, String)]) -> Val {
    crate::coll::map_from(p.iter().map(|(k, v)| (Val::str(k.clone()), Val::str(v.clone()))).collect())
}

impl Native for Request {
    fn type_name(&self) -> &str {
        "request"
    }
    fn repr(&self) -> String {
        format!("request({} {})", self.method, self.path)
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        Ok(match name {
            "method" => Val::str(self.method.clone()),
            "path" => Val::str(self.path.clone()),
            "ip" => Val::str(self.ip.clone()),
            "user" => self.user.clone(),
            "headers" => pairs_map(&self.headers),
            "query" => pairs_map(&self.query),
            "body" => Val::str(String::from_utf8_lossy(&self.body).into_owned()),
            "json" => Val::new(Obj::BuiltinMethod(Val::from(me), "json")),
            _ => return Err(missing_member("request", name, REQUEST_MEMBERS, span)),
        })
    }
    fn call_method(&self, _me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        let a = method_args(name, &["as"], 0, args, names, span)?;
        let as_ = if a[0].tag == T_MISSING { None } else { Some(a[0]) };
        crate::data::json_parse(&String::from_utf8_lossy(&self.body), as_, span)
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

struct Answer {
    status: i64,
    body: Vec<u8>,
    ctype: Option<&'static str>,
    extra: Vec<(String, String)>,
}

impl Answer {
    fn new(status: i64) -> Answer {
        Answer { status, body: Vec::new(), ctype: None, extra: Vec::new() }
    }
}

fn error_answer(status: i64, message: Option<String>, extra: Vec<(String, String)>) -> Answer {
    let text = format!("{{\"error\":{}}}", crate::data::json_string(&message.unwrap_or_else(|| reason(status).to_string())));
    Answer { status, body: text.into_bytes(), ctype: Some("application/json"), extra }
}

enum Read_ {
    Closed,
    Bad(Answer),
    Ok(String, String, Vec<(String, String)>, Vec<u8>),
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

fn read_request(conn: &mut TcpStream) -> Read_ {
    let mut data = Vec::new();
    let mut buf = vec![0u8; 65536];
    let end = loop {
        if let Some(i) = find(&data, b"\r\n\r\n") {
            break i;
        }
        match conn.read(&mut buf) {
            Ok(0) | Err(_) => return Read_::Closed,
            Ok(n) => data.extend_from_slice(&buf[..n]),
        }
        if data.len() > MAX_HEAD && find(&data[..(MAX_HEAD + 4).min(data.len())], b"\r\n\r\n").is_none() {
            return Read_::Bad(error_answer(431, None, Vec::new()));
        }
    };
    let head: String = data[..end].iter().map(|b| *b as char).collect();
    let rest = data[end + 4..].to_vec();
    let lines: Vec<&str> = head.split("\r\n").collect();
    let parts: Vec<&str> = lines[0].split(' ').collect();
    if parts.len() != 3 || !parts[2].starts_with("HTTP/1.") {
        return Read_::Bad(error_answer(400, None, Vec::new()));
    }
    let mut headers: Vec<(String, String)> = Vec::new();
    for line in &lines[1..] {
        let Some((name, value)) = line.split_once(':') else { return Read_::Bad(error_answer(400, None, Vec::new())) };
        if name.is_empty() || name != name.trim() {
            return Read_::Bad(error_answer(400, None, Vec::new()));
        }
        let key = name.to_lowercase();
        let value = value.trim().to_string();
        match headers.iter_mut().find(|(k, _)| *k == key) {
            Some((_, v)) => {
                v.push_str(", ");
                v.push_str(&value);
            }
            None => headers.push((key, value)),
        }
    }
    let get = |k: &str| headers.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
    if get("transfer-encoding").is_some() {
        return Read_::Bad(error_answer(501, None, Vec::new()));
    }
    let length = get("content-length").unwrap_or_else(|| "0".into());
    if length.is_empty() || !length.bytes().all(|b| b.is_ascii_digit()) {
        return Read_::Bad(error_answer(400, None, Vec::new()));
    }
    let n: usize = length.parse().unwrap_or(usize::MAX);
    if n > MAX_BODY {
        return Read_::Bad(error_answer(413, None, Vec::new()));
    }
    let mut body = rest;
    while body.len() < n {
        match conn.read(&mut buf[..(n - body.len()).min(65536)]) {
            Ok(0) | Err(_) => return Read_::Bad(error_answer(400, None, Vec::new())),
            Ok(k) => body.extend_from_slice(&buf[..k]),
        }
    }
    body.truncate(n);
    Read_::Ok(parts[0].to_string(), parts[1].to_string(), headers, body)
}

fn write_answer(conn: &mut TcpStream, a: &Answer) {
    let mut lines = vec![format!("HTTP/1.1 {} {}", a.status, reason(a.status)).trim_end().to_string()];
    if let Some(c) = a.ctype {
        lines.push(format!("Content-Type: {c}"));
    }
    lines.push(format!("Content-Length: {}", a.body.len()));
    for (k, v) in &a.extra {
        lines.push(format!("{k}: {v}"));
    }
    for (k, v) in SECURE {
        lines.push(format!("{k}: {v}"));
    }
    lines.push("Connection: close".into());
    let mut out = (lines.join("\r\n") + "\r\n\r\n").into_bytes();
    out.extend_from_slice(&a.body);
    let _ = conn.write_all(&out);
}

/// Percent-decoding (UTF-8, invalid sequences replaced); `plus` turns `+`
/// into a space, as in a query.
fn unquote(s: &str, plus: bool) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        let hex = |c: u8| (c as char).to_digit(16);
        if b[i] == b'%' && i + 2 < b.len() {
            if let (Some(x), Some(y)) = (hex(b[i + 1]), hex(b[i + 2])) {
                out.push((x * 16 + y) as u8);
                i += 3;
                continue;
            }
        }
        out.push(if plus && b[i] == b'+' { b' ' } else { b[i] });
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// The netloc of a URL (as Python's urlsplit gives it).
fn netloc(url: &str) -> String {
    match url.find("//") {
        Some(i) => {
            let rest = &url[i + 2..];
            rest.split(['/', '?', '#']).next().unwrap_or("").to_string()
        }
        None => String::new(),
    }
}

fn convert(text: &str, t: V, span: u32) -> R<Val> {
    match t.get() {
        None => Ok(Val::str(text)),
        Some(Obj::Type(TypeRef::Builtin(n))) if *n == "str" || *n == "any" => Ok(Val::str(text)),
        _ => crate::stdlib::validate(Val::str(text).v(), t, None, span),
    }
}

fn body_param(t: V) -> bool {
    matches!(t.get(), Some(Obj::Type(TypeRef::Record(_))))
        || matches!(t.get(), Some(Obj::Type(TypeRef::Builtin(n))) if *n == "map" || *n == "list")
}

/// The pending failure, taken: (kind, message, fix) of an error value, or
/// None for a diagnostic (left pending).
fn take_failure() -> Option<(String, String, Option<&'static str>)> {
    if crate::state::failing() == crate::state::DIAG {
        return None;
    }
    let e = crate::state::with_task(|s| s.error.clone());
    let r = match e.as_ref().and_then(|e| e.get()) {
        Some(Obj::Error(err)) => Some((err.kind.clone(), err.message.clone(), err.fix)),
        _ => Some(("Error".into(), String::new(), None)),
    };
    r
}

enum Outcome {
    Answer(Answer),
    /// a diagnostic in a handler (pending in the state)
    Diag,
}

fn handle(server: &Server, method: &str, target: &str, headers: &[(String, String)], body: Vec<u8>, ip: &str) -> Outcome {
    let get = |k: &str| headers.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
    let (path, query) = {
        let no_frag = target.split('#').next().unwrap_or("");
        match no_frag.split_once('?') {
            Some((p, q)) => (p.to_string(), q.to_string()),
            None => (no_frag.to_string(), String::new()),
        }
    };
    if get("x-forwarded-proto").map(|p| p.to_lowercase()) == Some("http".into()) {
        let host = get("host").unwrap_or_default();
        let mut a = Answer::new(308);
        a.extra.push(("Location".into(), format!("https://{host}{target}")));
        return Outcome::Answer(a);
    }
    let parts: Vec<String> = if path.trim_matches('/').is_empty() {
        Vec::new()
    } else {
        path.split('/').filter(|p| !p.is_empty()).map(|p| unquote(p, false)).collect()
    };
    let routes: Vec<std::sync::Arc<Route>> = server.routes.lock().unwrap().clone();
    let mut found = None;
    let mut allowed = Vec::new();
    for r in &routes {
        let Some(params) = r.matches(&parts) else { continue };
        if r.method == method {
            found = Some((r.clone(), params));
            break;
        }
        allowed.push(r.method.clone());
    }
    let Some((route, path_params)) = found else {
        if !allowed.is_empty() {
            return Outcome::Answer(error_answer(405, None, vec![("Allow".into(), allowed.join(", "))]));
        }
        return Outcome::Answer(error_answer(404, None, Vec::new()));
    };
    let span = route.span;
    if matches!(method, "POST" | "PUT" | "PATCH" | "DELETE") {
        if let Some(origin) = get("origin") {
            if netloc(&origin) != get("host").unwrap_or_default() {
                return Outcome::Answer(error_answer(403, Some(msg("detail.csrf", &[])), Vec::new()));
            }
        }
    }
    let mut claims = Val::none();
    if let Some(auth) = &server.auth {
        let jwt = auth.v().native::<Jwt>().unwrap();
        let header = get("authorization").unwrap_or_default();
        if let Some(token) = header.strip_prefix("Bearer ").filter(|t| !t.is_empty()) {
            match jwt.claims(token, span) {
                Ok(Some(c)) => claims = c,
                Ok(None) => {}
                Err(_) => return Outcome::Diag,
            }
        }
        if claims.is_none() && !route.public {
            return Outcome::Answer(error_answer(
                401,
                Some(msg("detail.need_token", &[])),
                vec![("WWW-Authenticate".into(), "Bearer".into())],
            ));
        }
        if let Some(role) = &route.role {
            let ok = match claims.get() {
                Some(Obj::Map(m)) => {
                    let m = m.borrow();
                    m.get(&Key::Str("role".into())).and_then(|r| r.as_static_str()) == Some(role.as_str())
                        || matches!(m.get(&Key::Str("roles".into())).and_then(|r| r.get()), Some(Obj::List(items))
                            if items.borrow().iter().any(|x| x.as_str() == Some(role.as_str())))
                }
                _ => false,
            };
            if !ok {
                return Outcome::Answer(error_answer(403, Some(msg("detail.need_role", &[("role", role.clone())])), Vec::new()));
            }
        }
    }
    if let Some((amount, win, per)) = &route.limit {
        let mut who = ip.to_string();
        if per.as_deref() == Some("user") {
            if let Some(Obj::Map(m)) = claims.get() {
                if let Some(sub) = m.borrow().get(&Key::Str("sub".into())).and_then(|s| s.as_static_str()) {
                    who = format!("user:{sub}");
                }
            }
        }
        let now = now_secs() as i64;
        let start = now - now % win;
        let mut hits = route.hits.lock().unwrap();
        hits.retain(|k, _| k.1 == start);
        let count = hits.entry((who, start)).or_insert(0);
        *count += 1;
        if *count > *amount {
            return Outcome::Answer(error_answer(429, None, vec![("Retry-After".into(), (start + win - now).to_string())]));
        }
    }
    let mut q: Vec<(String, String)> = Vec::new();
    for part in query.split('&').filter(|p| !p.is_empty()) {
        let (k, v) = part.split_once('=').unwrap_or((part, ""));
        let (k, v) = (unquote(k, true), unquote(v, true));
        if !q.iter().any(|(n, _)| *n == k) {
            q.push((k, v));
        }
    }
    let Some(Obj::Func(f)) = route.handler.get() else { return Outcome::Answer(error_answer(500, None, Vec::new())) };
    let params = f.info.params();
    let mut vals: Vec<Val> = Vec::new();
    let mut names: Vec<Option<&str>> = Vec::new();
    let bad = |_: crate::state::Fail| -> Outcome {
        match take_failure() {
            None => Outcome::Diag,
            Some((_, m, _)) => {
                crate::state::clear();
                Outcome::Answer(error_answer(400, Some(m), Vec::new()))
            }
        }
    };
    for (i, p) in params.iter().enumerate() {
        let name = p.name.get().unwrap_or("");
        let t = route.types.get(i).map(|t| t.v()).unwrap_or(NONE);
        let optional = p.optional != 0;
        let value = if name == "request" {
            native(Request {
                method: method.to_string(),
                path: path.clone(),
                headers: headers.to_vec(),
                query: q.clone(),
                body: body.clone(),
                ip: ip.to_string(),
                user: claims.clone(),
            })
        } else if let Some((_, text)) = path_params.iter().find(|(n, _)| n == name) {
            match convert(text, t, span) {
                Ok(v) => v,
                Err(e) => return bad(e),
            }
        } else if body_param(t) {
            let text = String::from_utf8_lossy(&body).into_owned();
            if text.trim().is_empty() {
                if optional {
                    continue;
                }
                return Outcome::Answer(error_answer(400, Some(msg("detail.need_body", &[])), Vec::new()));
            }
            match crate::data::json_parse(&text, Some(t), span) {
                Ok(v) => v,
                Err(e) => return bad(e),
            }
        } else if let Some((_, text)) = q.iter().find(|(n, _)| n == name) {
            match convert(text, t, span) {
                Ok(v) => v,
                Err(e) => return bad(e),
            }
        } else if !optional {
            return Outcome::Answer(error_answer(400, Some(msg("detail.need_param", &[("name", name.to_string())])), Vec::new()));
        } else {
            continue;
        };
        vals.push(value);
        names.push(Some(name));
    }
    let raw: Vec<V> = vals.iter().map(|v| v.v()).collect();
    let result = match crate::call::call(route.handler.v(), &raw, &names, span) {
        Ok(v) => v,
        Err(_) => {
            return match take_failure() {
                None => Outcome::Diag,
                Some((kind, m, fix)) => {
                    if kind == "NotFound" {
                        crate::state::clear();
                        return Outcome::Answer(error_answer(404, Some(m), Vec::new()));
                    }
                    if kind == "InvalidValue" || kind == "InvalidData" {
                        crate::state::clear();
                        return Outcome::Answer(error_answer(400, Some(m), Vec::new()));
                    }
                    if fix == Some("fix.DatabaseError.unique") {
                        crate::state::clear();
                        return Outcome::Answer(error_answer(409, Some(m), Vec::new()));
                    }
                    crate::state::report_pending();
                    crate::state::clear();
                    Outcome::Answer(error_answer(500, None, Vec::new()))
                }
            };
        }
    };
    match answer_of(result, method, span) {
        Ok(a) => Outcome::Answer(a),
        Err(_) => Outcome::Diag,
    }
}

fn answer_of(result: Val, method: &str, span: u32) -> R<Answer> {
    let value = crate::pipe::bind(result.v())?;
    let ok = if method == "POST" { 201 } else { 200 };
    if value.is_none() {
        return Ok(Answer::new(204));
    }
    if let Some(n) = value.as_int() {
        if !(100..=599).contains(&n) {
            return Err(diag(Diagnostic::new_spanless("web_status").arg("status", n.to_string()), span));
        }
        if n >= 400 {
            return Ok(error_answer(n, None, Vec::new()));
        }
        return Ok(Answer::new(n));
    }
    if let Some(s) = value.as_str() {
        return Ok(Answer { status: ok, body: s.as_bytes().to_vec(), ctype: Some("text/plain; charset=utf-8"), extra: Vec::new() });
    }
    crate::fmt::show_checked(value.v(), span)?;
    Ok(Answer { status: ok, body: crate::data::to_json_compact(value.v()).into_bytes(), ctype: Some("application/json"), extra: Vec::new() })
}

// ---------------------------------------------------------------- serving

fn listen(s: &Server, port: i64) -> Result<TcpListener, String> {
    let host = if std::env::var_os("NEXO_WEB_LOCAL").is_some() { "127.0.0.1" } else { "0.0.0.0" };
    let l = TcpListener::bind((host, port as u16)).map_err(|e| {
        let text = e.to_string();
        text.split(" (os error").next().unwrap_or(&text).to_string()
    })?;
    *s.port.lock().unwrap() = l.local_addr().map(|a| a.port() as i64).unwrap_or(port);
    Ok(l)
}

/// Answer one connection; false when a handler stopped with a diagnostic
/// (already shown).
fn serve_one(s: &Server, mut conn: TcpStream) -> bool {
    let _ = conn.set_read_timeout(Some(std::time::Duration::from_secs(10)));
    let ip = conn.peer_addr().map(|a| a.ip().to_string()).unwrap_or_default();
    match read_request(&mut conn) {
        Read_::Closed => true,
        Read_::Bad(a) => {
            write_answer(&mut conn, &a);
            true
        }
        Read_::Ok(method, target, headers, body) => match handle(s, &method, &target, &headers, body, &ip) {
            Outcome::Answer(a) => {
                write_answer(&mut conn, &a);
                true
            }
            Outcome::Diag => {
                // a mistake in the handler: the program shows the
                // diagnostic, then the client gets 500
                crate::state::report_pending();
                crate::state::clear();
                MISTAKE.store(true, std::sync::atomic::Ordering::SeqCst);
                write_answer(&mut conn, &error_answer(500, None, Vec::new()));
                false
            }
        },
    }
}

fn port_error(s: &Server, port: i64, reason: String) -> crate::state::Fail {
    diag(
        Diagnostic::new_spanless("web_port").arg("server", s.name.clone()).arg("port", port.to_string()).arg("reason", reason),
        0,
    )
}

/// `nexo file.nexo`: the servers answer until the program is stopped.
/// Returns false when it stops with a diagnostic (already shown).
pub fn has_servers() -> bool {
    !SERVERS.lock().unwrap().is_empty()
}

pub fn serve_forever() -> bool {
    let servers: Vec<Val> = SERVERS.lock().unwrap().clone();
    if servers.is_empty() || TESTING.load(std::sync::atomic::Ordering::SeqCst) {
        return true;
    }
    let env_port = std::env::var("PORT").ok().filter(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()));
    let mut listeners = Vec::new();
    for v in &servers {
        let s = v.v().native::<Server>().unwrap();
        let configured = *s.port.lock().unwrap();
        let port = match (&env_port, servers.len()) {
            (Some(p), 1) => p.parse().unwrap_or(configured),
            _ => configured,
        };
        match listen(s, port) {
            Ok(l) => listeners.push(l),
            Err(reason) => {
                port_error(s, port, reason);
                crate::state::report_pending();
                return false;
            }
        }
        crate::state::flush();
        eprintln!("{}", msg("info.serving", &[("server", s.name.clone()), ("port", s.port.lock().unwrap().to_string())]));
    }
    if listeners.len() == 1 {
        let s = servers[0].v().native::<Server>().unwrap();
        for conn in listeners[0].incoming().flatten() {
            if !serve_one(s, conn) {
                return false;
            }
            crate::state::flush();
        }
        return true;
    }
    for l in &listeners {
        let _ = l.set_nonblocking(true);
    }
    loop {
        let mut idle = true;
        for (l, v) in listeners.iter().zip(&servers) {
            if let Ok((conn, _)) = l.accept() {
                idle = false;
                let _ = conn.set_nonblocking(false);
                if !serve_one(v.v().native::<Server>().unwrap(), conn) {
                    return false;
                }
                crate::state::flush();
            }
        }
        if idle {
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    }
}

/// `nexo test`: every server on a free port, answering on its own thread.
/// A handler stopped with a diagnostic (`nexo test` then fails, D102).
pub static MISTAKE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

static TESTING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

pub fn serve_in_background() -> bool {
    TESTING.store(true, std::sync::atomic::Ordering::SeqCst);
    let servers: Vec<Val> = SERVERS.lock().unwrap().clone();
    for (i, v) in servers.into_iter().enumerate() {
        let s = v.v().native::<Server>().unwrap();
        let l = match listen(s, 0) {
            Ok(l) => l,
            Err(reason) => {
                port_error(s, 0, reason);
                return false;
            }
        };
        // the thread looks its server up on every connection, so it owns
        // nothing when the program releases the library at its end
        crate::value::going_multi();
        std::thread::spawn(move || {
            for conn in l.incoming().flatten() {
                let Some(owner) = SERVERS.lock().unwrap().get(i).cloned() else { return };
                // a mistake in a handler fails the request (and shows
                // itself), not the tests that are still running
                serve_one(owner.v().native::<Server>().unwrap(), conn);
                drop(owner);
                crate::state::flush();
            }
        });
    }
    true
}
