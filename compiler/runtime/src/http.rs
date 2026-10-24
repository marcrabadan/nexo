//! http: a client with retries (section 15, D98). `http.get(url)` and the
//! other methods return a response, or fail with a NetworkError (no
//! answer) or an HTTPError (an answer with status 400 or more). Redirects
//! of a GET are followed (up to 5); idempotent methods are retried on
//! network errors and on 502, 503 and 504.

use nexo_diag::msg;

use crate::state::{failure, R};
use crate::stdlib::{expects, method_args, missing_member};
use crate::value::*;

const USER_AGENT: &str = "nexo/0.1";
const MAX_REDIRECTS: usize = 5;

pub struct Response {
    pub status: i64,
    pub body: Vec<u8>,
    /// lowercase names, in the order received
    pub headers: Vec<(String, String)>,
}

impl Response {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }
}

const MEMBERS: &[&str] = &["status", "text", "headers", "json"];

impl Native for Response {
    fn type_name(&self) -> &str {
        "response"
    }
    fn repr(&self) -> String {
        format!("response({})", self.status)
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        Ok(match name {
            "status" => Val::int(self.status),
            "text" => Val::str(self.text()),
            "headers" => {
                crate::coll::map_from(self.headers.iter().map(|(k, v)| (Val::str(k.clone()), Val::str(v.clone()))).collect())
            }
            "json" => Val::new(Obj::BuiltinMethod(Val::from(me), "json")),
            _ => return Err(missing_member("response", name, MEMBERS, span)),
        })
    }
    fn call_method(&self, _me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        let a = method_args(name, &["as"], 0, args, names, span)?;
        let as_ = if a[0].tag == T_MISSING { None } else { Some(a[0]) };
        crate::data::json_parse(&self.text(), as_, span)
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

fn reason(status: i64) -> &'static str {
    match status {
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        413 => "Payload Too Large",
        415 => "Unsupported Media Type",
        422 => "Unprocessable Content",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        _ => "",
    }
}

pub fn status_text(status: i64) -> String {
    format!("{status} {}", reason(status)).trim().to_string()
}

fn network(url: &str, reason_key: &str, span: u32) -> crate::state::Fail {
    failure("NetworkError", span, &[("url", url.to_string()), ("reason", msg(reason_key, &[]))])
}

/// The proxy of the environment for `url`, unless NO_PROXY names its host.
fn proxy_for(url: &str) -> Option<ureq::Proxy> {
    let https = url.starts_with("https://");
    let var = |names: &[&str]| names.iter().find_map(|n| std::env::var(n).ok().filter(|v| !v.is_empty()));
    let proxy = if https { var(&["https_proxy", "HTTPS_PROXY"]) } else { var(&["http_proxy", "HTTP_PROXY"]) }?;
    let rest = url.split_once("://").map(|x| x.1).unwrap_or(url);
    let hostport = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host = hostport.rsplit_once(':').map(|x| x.0).unwrap_or(hostport).trim_matches(['[', ']']).to_lowercase();
    if let Some(no) = var(&["no_proxy", "NO_PROXY"]) {
        for entry in no.split(',').map(|e| e.trim().to_lowercase()).filter(|e| !e.is_empty()) {
            if entry == "*" {
                return None;
            }
            let e = entry.trim_start_matches('.');
            let e = e.split_once(':').map(|x| x.0).unwrap_or(e);
            if host == e || host.ends_with(&format!(".{e}")) {
                return None;
            }
        }
    }
    ureq::Proxy::new(proxy).ok()
}

enum Outcome {
    Done(Response),
    /// a failure worth another attempt
    Retry(crate::state::Fail),
}

fn once(method: &str, url: &str, body: &Option<(Vec<u8>, &str)>, headers: &[(String, String)], limit: f64, span: u32) -> R<Outcome> {
    let mut current = url.to_string();
    let mut headers: Vec<(String, String)> = headers.to_vec();
    for _ in 0..=MAX_REDIRECTS {
        let mut b = ureq::AgentBuilder::new()
            .redirects(0)
            .timeout(std::time::Duration::from_secs_f64(limit.max(0.001)))
            .user_agent(USER_AGENT);
        if let Some(p) = proxy_for(&current) {
            b = b.proxy(p);
        }
        let agent = b.build();
        let mut req = agent.request(method, &current);
        if let Some((_, ctype)) = body {
            req = req.set("Content-Type", ctype);
        }
        for (k, v) in &headers {
            req = req.set(k, v);
        }
        let result = match body {
            Some((data, _)) => req.send_bytes(data),
            None => req.call(),
        };
        let resp = match result {
            Ok(r) => r,
            Err(ureq::Error::Status(_, r)) => r,
            Err(ureq::Error::Transport(t)) => {
                let key = match t.kind() {
                    ureq::ErrorKind::Dns => "detail.unknown_host",
                    ureq::ErrorKind::ConnectionFailed => {
                        let text = t.to_string();
                        if text.contains("refused") {
                            "detail.refused"
                        } else if text.contains("timed out") {
                            "detail.timeout"
                        } else {
                            "detail.no_connection"
                        }
                    }
                    ureq::ErrorKind::Io => {
                        if t.to_string().contains("timed out") {
                            "detail.timeout"
                        } else {
                            "detail.no_connection"
                        }
                    }
                    ureq::ErrorKind::InvalidUrl | ureq::ErrorKind::UnknownScheme => "detail.bad_url",
                    _ => "detail.no_connection",
                };
                return Ok(Outcome::Retry(network(&current, key, span)));
            }
        };
        let status = resp.status() as i64;
        let mut hs: Vec<(String, String)> = Vec::new();
        for name in resp.headers_names() {
            let all: Vec<&str> = resp.all(&name);
            let key = name.to_lowercase();
            if hs.iter().any(|(k, _)| *k == key) {
                continue;
            }
            hs.push((key, all.join(", ")));
        }
        let location = resp.header("location").map(|s| s.to_string());
        let mut data = Vec::new();
        use std::io::Read;
        if resp.into_reader().read_to_end(&mut data).is_err() {
            return Ok(Outcome::Retry(network(&current, "detail.no_connection", span)));
        }
        if matches!(status, 301 | 302 | 303 | 307 | 308) && method == "GET" {
            if let Some(loc) = location {
                let target = join_url(&current, &loc);
                if origin(&target) != origin(&current) {
                    // credentials are for the origin they were given to
                    headers.retain(|(k, _)| !SENSITIVE.contains(&k.to_lowercase().as_str()));
                }
                current = target;
                continue;
            }
        }
        if status >= 400 {
            let f = failure(
                "HTTPError",
                span,
                &[("method", method.to_string()), ("url", current.clone()), ("status", status_text(status))],
            );
            return Ok(if matches!(status, 502..=504) { Outcome::Retry(f) } else { Err(f)? });
        }
        return Ok(Outcome::Done(Response { status, body: data, headers: hs }));
    }
    Err(network(url, "detail.redirects", span))
}

const SENSITIVE: &[&str] = &["authorization", "cookie", "proxy-authorization"];

/// (scheme, host, port) of an address, as phase 0 reads it.
fn origin(url: &str) -> (String, String, u16) {
    let (scheme, rest) = url.split_once("://").unwrap_or(("", url));
    let scheme = scheme.to_lowercase();
    let netloc = rest.split(['/', '?', '#']).next().unwrap_or("");
    let hostport = netloc.rsplit_once('@').map(|x| x.1).unwrap_or(netloc);
    let (host, port) = if let Some(r) = hostport.strip_prefix('[') {
        let (h, after) = r.split_once(']').unwrap_or((r, ""));
        (h.to_string(), after.strip_prefix(':').and_then(|p| p.parse().ok()))
    } else {
        match hostport.rsplit_once(':') {
            Some((h, p)) => (h.to_string(), p.parse().ok()),
            None => (hostport.to_string(), None),
        }
    };
    let default = if scheme == "https" { 443 } else { 80 };
    (scheme, host.to_lowercase(), port.filter(|p| *p != 0).unwrap_or(default))
}

/// A Location relative to the address it came from.
fn join_url(base: &str, loc: &str) -> String {
    if loc.starts_with("http://") || loc.starts_with("https://") {
        return loc.to_string();
    }
    let (scheme, rest) = base.split_once("://").unwrap_or(("http", base));
    let host = rest.split('/').next().unwrap_or("");
    if loc.starts_with('/') {
        return format!("{scheme}://{host}{loc}");
    }
    let path = &rest[host.len()..];
    let dir = match path.rfind('/') {
        Some(i) => &path[..=i],
        None => "/",
    };
    format!("{scheme}://{host}{dir}{loc}")
}

pub fn seconds(v: V, name: &str, span: u32) -> R<f64> {
    if let Some(Obj::Quantity(d, u)) = v.get() {
        let (dim, f) = crate::num::unit_dimension(u);
        if dim == "time" {
            return Ok(d.mul(&f).to_f64());
        }
    }
    Err(expects(name, "duration", v, span))
}

/// `http.get(url, headers:, timeout:, retries:)` and the others.
pub fn request(method: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    let with_body = matches!(method, "POST" | "PUT" | "PATCH");
    let fname = format!("http.{}", method.to_lowercase());
    let params: &[&str] = if with_body { &["url", "body", "headers", "timeout", "retries"] } else { &["url", "headers", "timeout", "retries"] };
    let a = method_args(&fname, params, 1, args, names, span)?;
    let get = |n: &str| {
        let i = params.iter().position(|p| *p == n)?;
        Some(a[i]).filter(|v| v.tag != T_MISSING && !v.is_none())
    };
    let url = a[0].as_str().ok_or_else(|| expects(&fname, "str", a[0], span))?.to_string();
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err(network(&url, "detail.bad_url", span));
    }
    let limit = match get("timeout") {
        Some(t) => seconds(t, "timeout", span)?,
        None => 30.0,
    };
    let idempotent = matches!(method, "GET" | "PUT" | "DELETE");
    let retries = match get("retries") {
        Some(r) => match r.as_int() {
            Some(n) if n >= 0 => n as u32,
            _ => return Err(expects("retries", "int", r, span)),
        },
        None => {
            if idempotent {
                2
            } else {
                0
            }
        }
    };
    let body = match get("body") {
        None => None,
        Some(b) => {
            let b = crate::pipe::bind(b)?;
            match b.as_str() {
                Some(s) => Some((s.as_bytes().to_vec(), "text/plain; charset=utf-8")),
                None => {
                    crate::fmt::show_checked(b.v(), span)?;
                    Some((crate::data::to_json_compact(b.v()).into_bytes(), "application/json"))
                }
            }
        }
    };
    let mut headers = Vec::new();
    if let Some(h) = get("headers") {
        let Some(Obj::Map(m)) = h.get() else { return Err(expects("headers", "map", h, span)) };
        for (k, v) in m.borrow().entries.iter() {
            let key = k.as_str().ok_or_else(|| expects("headers", "str", k.v(), span))?.to_string();
            headers.push((key, crate::crypto::text_or_secret("headers", v.v(), span)?));
        }
    }
    Ok(native(send(method, &url, &body, &headers, limit, retries, span)?))
}

/// The request, retried `retries` times after a network error or a 502,
/// 503 or 504 (also used by `ask`).
pub fn send(
    method: &str,
    url: &str,
    body: &Option<(Vec<u8>, &str)>,
    headers: &[(String, String)],
    limit: f64,
    retries: u32,
    span: u32,
) -> R<Response> {
    let mut attempt = 0;
    loop {
        match once(method, url, body, headers, limit, span)? {
            Outcome::Done(r) => return Ok(r),
            Outcome::Retry(f) => {
                if attempt >= retries {
                    return Err(f);
                }
                crate::state::clear();
                std::thread::sleep(std::time::Duration::from_secs_f64(0.25 * 2f64.powi(attempt as i32)));
                attempt += 1;
            }
        }
    }
}
