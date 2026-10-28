//! The commands of nexo.infra (D120, D121) and the clients of each target's
//! API: AWS Cloud Control (signed with SigV4), Azure Resource Manager, the
//! REST APIs of Google Cloud and the Kubernetes API. Follows phase 0's
//! infra_cloud.py; credentials come from the same environment variables.

use std::io::{Read, Write};
use std::sync::Mutex;

use hmac::{Hmac, Mac};
use nexo_diag::Diagnostic;
use sha2::{Digest, Sha256};

use crate::infra::{self as I, m, Plan, Store};
use crate::json::{self, Json};
use crate::state::{diag, R};

const POLL: [f64; 5] = [0.2, 0.5, 1.0, 2.0, 5.0];
const MAX_WAIT: f64 = 1800.0;

/// plan() in a pipeline step keeps the lines (D122).
pub static CAPTURE: Mutex<Vec<Vec<String>>> = Mutex::new(Vec::new());

fn say(text: &str) {
    if let Some(lines) = CAPTURE.lock().unwrap().last_mut() {
        lines.push(text.to_string());
        return;
    }
    crate::state::flush();
    println!("{text}");
    let _ = std::io::stdout().flush();
}

fn fail_d(d: Diagnostic) -> crate::state::Fail {
    diag(d, 0)
}

// ------------------------------------------------------------------- http

pub struct Answer {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Answer {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }
    fn json(&self) -> Json {
        let text = String::from_utf8_lossy(&self.body).to_string();
        json::parse(if text.trim().is_empty() { "{}" } else { &text }).unwrap_or(Json::Obj(Vec::new()))
    }
}

/// (status, headers, body); a network failure is a diagnostic.
pub fn http(method: &str, url: &str, headers: &[(String, String)], body: Option<&[u8]>) -> R<Answer> {
    http_with(method, url, headers, body, None)
}

/// The TLS settings that also trust the certificate authority in `ca`
/// (a PEM file): the one of a Kubernetes cluster.
fn tls_with(ca: &str) -> R<std::sync::Arc<rustls::ClientConfig>> {
    let bad = || fail_d(Diagnostic::new_spanless("infra_ca").arg("path", ca));
    let pem = std::fs::read(ca).map_err(|_| bad())?;
    let mut roots = rustls::RootCertStore::empty();
    #[cfg(not(target_family = "wasm"))]
    for cert in rustls_native_certs::load_native_certs().into_iter().flatten() {
        let _ = roots.add(cert);
    }
    let mut added = 0;
    for cert in rustls_pemfile::certs(&mut pem.as_slice()) {
        let cert = cert.map_err(|_| bad())?;
        roots.add(cert).map_err(|_| bad())?;
        added += 1;
    }
    if added == 0 {
        return Err(bad());
    }
    let provider = std::sync::Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|_| bad())?
        .with_root_certificates(roots)
        .with_no_client_auth();
    Ok(std::sync::Arc::new(config))
}

fn http_with(method: &str, url: &str, headers: &[(String, String)], body: Option<&[u8]>, ca: Option<&str>) -> R<Answer> {
    let mut b = ureq::AgentBuilder::new().redirects(0).timeout(std::time::Duration::from_secs(120)).user_agent("nexo/0.1");
    if let Some(p) = crate::http::proxy_for(url) {
        b = b.proxy(p);
    }
    if let Some(ca) = ca {
        b = b.tls_config(tls_with(ca)?);
    }
    let agent = b.build();
    let mut req = agent.request(method, url);
    for (k, v) in headers {
        req = req.set(k, v);
    }
    let result = match body {
        Some(data) => req.send_bytes(data),
        None => req.call(),
    };
    let resp = match result {
        Ok(r) => r,
        Err(ureq::Error::Status(_, r)) => r,
        Err(ureq::Error::Transport(t)) => {
            let reason = t.to_string();
            return Err(fail_d(Diagnostic::new_spanless("infra_network").arg("url", url).arg("reason", reason)));
        }
    };
    let status = resp.status();
    let mut hs = Vec::new();
    for name in resp.headers_names() {
        if let Some(v) = resp.header(&name) {
            hs.push((name.to_lowercase(), v.to_string()));
        }
    }
    let mut data = Vec::new();
    let _ = resp.into_reader().read_to_end(&mut data);
    Ok(Answer { status, headers: hs, body: data })
}

pub fn error_text(body: &[u8]) -> String {
    let text = String::from_utf8_lossy(body).to_string();
    let Some(d) = json::parse(&text) else { return text.trim().chars().take(300).collect() };
    if let Some(msg) = d.get("error").get("message").str() {
        return msg.to_string();
    }
    for k in ["Message", "message"] {
        if let Some(msg) = d.get(k).str() {
            return msg.to_string();
        }
    }
    d.compact().chars().take(300).collect()
}

fn api_fail(what: &str, status: impl ToString, reason: String) -> crate::state::Fail {
    fail_d(Diagnostic::new_spanless("infra_api").arg("what", what).arg("status", status.to_string()).arg("reason", reason))
}

fn check(a: &Answer, what: &str) -> R<()> {
    if a.status >= 400 {
        return Err(api_fail(what, a.status, error_text(&a.body)));
    }
    Ok(())
}

pub fn need_env(name: &str) -> R<String> {
    match std::env::var(name) {
        Ok(v) if !v.is_empty() => Ok(v),
        _ => Err(fail_d(Diagnostic::new_spanless("infra_credentials").arg("name", name))),
    }
}

fn sleep(secs: f64) {
    std::thread::sleep(std::time::Duration::from_secs_f64(secs));
}

fn hex(b: &[u8]) -> String {
    crate::flags::hex(b)
}

/// Percent-encoding of everything but unreserved characters (and `/`).
fn quote(s: &str, slash: bool) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        let c = b as char;
        if c.is_ascii_alphanumeric() || "-_.~".contains(c) || (slash && c == '/') {
            out.push(c);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn unquote(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() + 0 && i + 2 <= b.len() - 1 {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(if b[i] == b'+' { b'+' } else { b[i] });
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// scheme://host, path, query of an address.
fn split_url(url: &str) -> (String, String, String, String) {
    let (scheme, rest) = url.split_once("://").unwrap_or(("", url));
    let (netloc, path_q) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, ""),
    };
    let (path, query) = path_q.split_once('?').unwrap_or((path_q, ""));
    (scheme.to_string(), netloc.to_string(), path.to_string(), query.to_string())
}

fn hmac_sha(key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut m = <Hmac<Sha256> as Mac>::new_from_slice(key).unwrap();
    m.update(data);
    m.finalize().into_bytes().to_vec()
}

fn now_parts() -> (String, String) {
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let (y, mo, d) = crate::time::civil_from_days(secs.div_euclid(86400));
    let s = secs.rem_euclid(86400);
    (format!("{y:04}{mo:02}{d:02}T{:02}{:02}{:02}Z", s / 3600, s / 60 % 60, s % 60), format!("{y:04}{mo:02}{d:02}"))
}

/// The headers of a request signed with AWS Signature Version 4.
pub fn sigv4(method: &str, url: &str, headers: &[(&str, String)], body: &[u8], service: &str, region: &str) -> R<Vec<(String, String)>> {
    sigv4_path(method, url, headers, body, service, region, false)
}

/// SigV4; with `encoded_path` the path, as sent, is encoded once more, as
/// services other than S3 ask (Bedrock's model ids hold a colon, sent as
/// %3A, D173).
pub fn sigv4_path(method: &str, url: &str, headers: &[(&str, String)], body: &[u8], service: &str, region: &str, encoded_path: bool) -> R<Vec<(String, String)>> {
    let key_id = need_env("AWS_ACCESS_KEY_ID")?;
    let secret = need_env("AWS_SECRET_ACCESS_KEY")?;
    let token = std::env::var("AWS_SESSION_TOKEN").ok().filter(|t| !t.is_empty());
    let (amz_date, day) = now_parts();
    let (_, netloc, path, query) = split_url(url);
    let payload = hex(&Sha256::digest(body));
    let mut h: Vec<(String, String)> = headers.iter().map(|(k, v)| (k.to_lowercase(), v.clone())).collect();
    let set = |h: &mut Vec<(String, String)>, k: &str, v: String| match h.iter_mut().find(|(x, _)| x == k) {
        Some(slot) => slot.1 = v,
        None => h.push((k.to_string(), v)),
    };
    set(&mut h, "host", netloc);
    set(&mut h, "x-amz-date", amz_date.clone());
    set(&mut h, "x-amz-content-sha256", payload.clone());
    if let Some(t) = token {
        set(&mut h, "x-amz-security-token", t);
    }
    let path = if encoded_path { quote(&path, true) } else { quote(&unquote(if path.is_empty() { "/" } else { &path }), true) };
    let mut pairs: Vec<(String, String)> = query
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            (unquote(&k.replace('+', " ")), unquote(&v.replace('+', " ")))
        })
        .collect();
    pairs.sort();
    let q: Vec<String> = pairs.iter().map(|(k, v)| format!("{}={}", quote(k, false), quote(v, false))).collect();
    h.sort_by(|a, b| a.0.cmp(&b.0));
    let names: Vec<String> = h.iter().map(|(k, _)| k.clone()).collect();
    let canon_headers: String = h.iter().map(|(k, v)| format!("{k}:{}\n", v.trim())).collect();
    let canonical = [method.to_string(), path, q.join("&"), canon_headers, names.join(";"), payload].join("\n");
    let scope = format!("{day}/{region}/{service}/aws4_request");
    let to_sign = ["AWS4-HMAC-SHA256".to_string(), amz_date, scope.clone(), hex(&Sha256::digest(canonical.as_bytes()))].join("\n");
    let mut k = format!("AWS4{secret}").into_bytes();
    for part in [day.as_str(), region, service, "aws4_request"] {
        k = hmac_sha(&k, part.as_bytes());
    }
    let signature = hex(&hmac_sha(&k, to_sign.as_bytes()));
    h.push((
        "authorization".into(),
        format!("AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={}, Signature={signature}", names.join(";")),
    ));
    Ok(h)
}

fn token_of(desc: &Json, body: &Json) -> String {
    let text = format!("{}{}{}", desc.get("type").str().unwrap_or(""), desc.get("name").str().unwrap_or(""), body.compact());
    hex(&Sha256::digest(text.as_bytes()))[..36].to_string()
}

pub trait Client {
    fn read(&mut self, desc: &Json, ident: &str, what: &str) -> R<Option<Json>>;
    fn create(&mut self, desc: &Json, body: &Json, what: &str) -> R<(String, Json)>;
    fn update(&mut self, desc: &Json, ident: &str, old: &Json, body: &Json, what: &str) -> R<(String, Json)>;
    fn delete(&mut self, desc: &Json, ident: &str, what: &str) -> R<()>;
}

// -------------------------------------------------------------------- aws

pub struct Aws {
    region: String,
    endpoint: String,
}

impl Aws {
    fn new(plan: &Plan) -> Aws {
        let endpoint = std::env::var("AWS_ENDPOINT_URL")
            .ok()
            .filter(|e| !e.is_empty())
            .unwrap_or_else(|| format!("https://cloudcontrolapi.{}.amazonaws.com", plan.region));
        Aws { region: plan.region.clone(), endpoint }
    }

    fn call(&self, op: &str, payload: Json, what: &str) -> R<Json> {
        let body = payload.compact().into_bytes();
        let url = format!("{}/", self.endpoint.trim_end_matches('/'));
        let headers = sigv4(
            "POST",
            &url,
            &[("Content-Type", "application/x-amz-json-1.0".into()), ("X-Amz-Target", format!("CloudApiService.{op}"))],
            &body,
            "cloudcontrolapi",
            &self.region,
        )?;
        let a = http("POST", &url, &headers, Some(&body))?;
        check(&a, what)?;
        Ok(a.json())
    }

    fn wait(&self, mut event: Json, what: &str) -> R<Json> {
        let (mut waited, mut i) = (0.0, 0);
        while matches!(event.get("OperationStatus").str(), Some("PENDING") | Some("IN_PROGRESS") | Some("CANCEL_IN_PROGRESS")) {
            let pause = POLL[i.min(POLL.len() - 1)];
            sleep(pause);
            waited += pause;
            i += 1;
            if waited > MAX_WAIT {
                return Err(fail_d(Diagnostic::new_spanless("infra_timeout").arg("what", what)));
            }
            let token = event.get("RequestToken").clone();
            event = self.call("GetResourceRequestStatus", json::obj(vec![("RequestToken", token)]), what)?.get("ProgressEvent").clone();
        }
        if event.get("OperationStatus").str() != Some("SUCCESS") {
            return Err(api_fail(what, event.get("ErrorCode").str().unwrap_or("?"), event.get("StatusMessage").str().unwrap_or("").to_string()));
        }
        Ok(event)
    }
}

impl Client for Aws {
    fn read(&mut self, desc: &Json, ident: &str, what: &str) -> R<Option<Json>> {
        let payload = json::obj(vec![("TypeName", desc.get("type").clone()), ("Identifier", json::s(ident))]);
        let body = payload.compact().into_bytes();
        let url = format!("{}/", self.endpoint.trim_end_matches('/'));
        let headers = sigv4(
            "POST",
            &url,
            &[("Content-Type", "application/x-amz-json-1.0".into()), ("X-Amz-Target", "CloudApiService.GetResource".into())],
            &body,
            "cloudcontrolapi",
            &self.region,
        )?;
        let a = http("POST", &url, &headers, Some(&body))?;
        if a.status == 404 || (a.status == 400 && error_text(&a.body).contains("NotFound")) {
            return Ok(None);
        }
        check(&a, what)?;
        let props = a.json().get("ResourceDescription").get("Properties").str().unwrap_or("{}").to_string();
        Ok(json::parse(&props))
    }

    fn create(&mut self, desc: &Json, body: &Json, what: &str) -> R<(String, Json)> {
        let payload =
            json::obj(vec![("TypeName", desc.get("type").clone()), ("DesiredState", json::s(body.compact())), ("ClientToken", json::s(token_of(desc, body)))]);
        let e = self.call("CreateResource", payload, what)?.get("ProgressEvent").clone();
        let e = self.wait(e, what)?;
        let ident = e.get("Identifier").str().unwrap_or("").to_string();
        let read = self.read(desc, &ident, what)?.unwrap_or(Json::Null);
        Ok((ident, read))
    }

    fn update(&mut self, desc: &Json, ident: &str, old: &Json, body: &Json, what: &str) -> R<(String, Json)> {
        let mut patch = Vec::new();
        for (k, v) in body.pairs() {
            if !old.has(k) {
                patch.push(json::obj(vec![("op", json::s("add")), ("path", json::s(format!("/{k}"))), ("value", v.clone())]));
            } else if !I::same(old.get(k), v) {
                patch.push(json::obj(vec![("op", json::s("replace")), ("path", json::s(format!("/{k}"))), ("value", v.clone())]));
            }
        }
        for (k, _) in old.pairs() {
            if !body.has(k) {
                patch.push(json::obj(vec![("op", json::s("remove")), ("path", json::s(format!("/{k}")))]));
            }
        }
        let payload = json::obj(vec![
            ("TypeName", desc.get("type").clone()),
            ("Identifier", json::s(ident)),
            ("PatchDocument", json::s(Json::Arr(patch).compact())),
            ("ClientToken", json::s(token_of(desc, body))),
        ]);
        let e = self.call("UpdateResource", payload, what)?.get("ProgressEvent").clone();
        self.wait(e, what)?;
        let read = self.read(desc, ident, what)?.unwrap_or(Json::Null);
        Ok((ident.to_string(), read))
    }

    fn delete(&mut self, desc: &Json, ident: &str, what: &str) -> R<()> {
        let token = token_of(desc, &json::obj(vec![("delete", json::s(ident))]));
        let payload = json::obj(vec![("TypeName", desc.get("type").clone()), ("Identifier", json::s(ident)), ("ClientToken", json::s(token))]);
        let e = self.call("DeleteResource", payload, what)?.get("ProgressEvent").clone();
        self.wait(e, what)?;
        Ok(())
    }
}

/// The state as an S3 object, encrypted, locked with a conditional write.
pub struct S3Store {
    region: String,
    bucket: String,
    key: String,
    base: String,
}

impl S3Store {
    pub fn new(plan: &Plan, bucket: &str) -> S3Store {
        let key = format!("nexo/{}", I::state_name(plan));
        let base = match std::env::var("AWS_ENDPOINT_URL").ok().filter(|e| !e.is_empty()) {
            Some(e) => format!("{}/{bucket}", e.trim_end_matches('/')),
            None => format!("https://{bucket}.s3.{}.amazonaws.com", plan.region),
        };
        S3Store { region: plan.region.clone(), bucket: bucket.to_string(), key, base }
    }

    fn request(&self, method: &str, key: &str, body: &[u8], extra: &[(&str, String)]) -> R<Answer> {
        let url = format!("{}/{key}", self.base);
        let headers = sigv4(method, &url, extra, body, "s3", &self.region)?;
        http(method, &url, &headers, if method == "PUT" { Some(body) } else { None })
    }
}

impl Store for S3Store {
    fn where_(&self) -> String {
        format!("s3://{}/{}", self.bucket, self.key)
    }
    fn load(&self) -> R<Option<Json>> {
        let a = self.request("GET", &self.key, b"", &[])?;
        if a.status == 404 {
            return Ok(None);
        }
        check(&a, &self.where_())?;
        Ok(json::parse(&String::from_utf8_lossy(&a.body)))
    }
    fn save(&self, data: &Json) -> R<()> {
        let body = (data.pretty(false) + "\n").into_bytes();
        let a = self.request(
            "PUT",
            &self.key,
            &body,
            &[("Content-Type", "application/json".into()), ("x-amz-server-side-encryption", "AES256".into())],
        )?;
        check(&a, &self.where_())
    }
    fn lock(&self) -> R<bool> {
        let a = self.request("PUT", &format!("{}.lock", self.key), b"nexo", &[("If-None-Match", "*".into())])?;
        if a.status == 409 || a.status == 412 {
            return Ok(false);
        }
        check(&a, &self.where_())?;
        Ok(true)
    }
    fn unlock(&self) {
        let _ = self.request("DELETE", &format!("{}.lock", self.key), b"", &[]);
    }
}

// ------------------------------------------------------------------ azure

pub struct Azure {
    subscription: String,
    group: String,
    endpoint: String,
    token: Option<String>,
}

impl Azure {
    fn new(plan: &Plan) -> R<Azure> {
        if plan.subscription == "?" {
            need_env("AZURE_SUBSCRIPTION_ID")?;
        }
        let endpoint = std::env::var("NEXO_AZURE_ENDPOINT").ok().filter(|e| !e.is_empty()).unwrap_or_else(|| "https://management.azure.com".into());
        Ok(Azure {
            subscription: plan.subscription.clone(),
            group: plan.infra.name.clone(),
            endpoint: endpoint.trim_end_matches('/').to_string(),
            token: None,
        })
    }

    fn auth(&mut self) -> R<String> {
        if self.token.is_none() {
            let tenant = need_env("AZURE_TENANT_ID")?;
            let client = need_env("AZURE_CLIENT_ID")?;
            let secret = need_env("AZURE_CLIENT_SECRET")?;
            let host = std::env::var("AZURE_AUTHORITY_HOST").ok().filter(|e| !e.is_empty()).unwrap_or_else(|| "https://login.microsoftonline.com".into());
            let url = format!("{}/{tenant}/oauth2/v2.0/token", host.trim_end_matches('/'));
            let form = format!(
                "grant_type=client_credentials&client_id={}&client_secret={}&scope={}",
                quote(&client, false),
                quote(&secret, false),
                quote("https://management.azure.com/.default", false)
            );
            let a = http("POST", &url, &[("Content-Type".into(), "application/x-www-form-urlencoded".into())], Some(form.as_bytes()))?;
            check(&a, &url)?;
            self.token = a.json().get("access_token").str().map(String::from);
        }
        Ok(self.token.clone().unwrap_or_default())
    }

    fn path(&self, desc: &Json) -> String {
        if let Some(p) = desc.get("path").str().filter(|p| !p.is_empty()) {
            return p.to_string();
        }
        let scope = desc
            .get("scope")
            .str()
            .filter(|s| !s.is_empty())
            .map(String::from)
            .unwrap_or_else(|| format!("/subscriptions/{}/resourceGroups/{}", self.subscription, self.group));
        let types: Vec<&str> = desc.get("type").str().unwrap_or("").split('/').collect();
        let names: Vec<&str> = desc.get("name").str().unwrap_or("").split('/').collect();
        let pairs: Vec<String> = types[1..].iter().zip(names.iter()).map(|(t, n)| format!("{t}/{n}")).collect();
        format!("{scope}/providers/{}/{}", types[0], pairs.join("/"))
    }

    fn url(&self, desc: &Json, path: &str) -> String {
        format!("{}{path}?api-version={}", self.endpoint, desc.get("api").str().unwrap_or(""))
    }

    fn send(&mut self, method: &str, url: &str, body: Option<&Json>) -> R<Answer> {
        let mut headers = vec![("Authorization".to_string(), format!("Bearer {}", self.auth()?))];
        let data = body.map(|b| b.compact().into_bytes());
        if data.is_some() {
            headers.push(("Content-Type".into(), "application/json".into()));
        }
        http(method, url, &headers, data.as_deref())
    }

    fn wait(&mut self, first: &Answer, what: &str) -> R<()> {
        let async_op = first.header("azure-asyncoperation").map(String::from);
        let Some(url) = async_op.clone().or_else(|| first.header("location").map(String::from)) else { return Ok(()) };
        let (mut waited, mut i) = (0.0, 0);
        loop {
            let a = self.send("GET", &url, None)?;
            check(&a, what)?;
            if async_op.is_some() {
                let j = a.json();
                let state = j.get("status").str().unwrap_or("Succeeded").to_string();
                if state == "Succeeded" {
                    return Ok(());
                }
                if state == "Failed" || state == "Canceled" {
                    return Err(api_fail(what, state, error_text(&a.body)));
                }
            } else if a.status != 202 {
                return Ok(());
            }
            let pause = POLL[i.min(POLL.len() - 1)];
            sleep(pause);
            waited += pause;
            i += 1;
            if waited > MAX_WAIT {
                return Err(fail_d(Diagnostic::new_spanless("infra_timeout").arg("what", what)));
            }
        }
    }
}

impl Client for Azure {
    fn read(&mut self, desc: &Json, ident: &str, what: &str) -> R<Option<Json>> {
        let url = self.url(desc, ident);
        let a = self.send("GET", &url, None)?;
        if a.status == 404 {
            return Ok(None);
        }
        check(&a, what)?;
        Ok(Some(a.json()))
    }

    fn create(&mut self, desc: &Json, body: &Json, what: &str) -> R<(String, Json)> {
        let path = self.path(desc);
        let url = self.url(desc, &path);
        let a = self.send("PUT", &url, Some(body))?;
        check(&a, what)?;
        self.wait(&a, what)?;
        let read = self.read(desc, &path, what)?.unwrap_or(Json::Null);
        Ok((path, read))
    }

    fn update(&mut self, desc: &Json, _ident: &str, _old: &Json, body: &Json, what: &str) -> R<(String, Json)> {
        self.create(desc, body, what)
    }

    fn delete(&mut self, desc: &Json, ident: &str, what: &str) -> R<()> {
        let url = self.url(desc, ident);
        let a = self.send("DELETE", &url, None)?;
        if a.status >= 400 && a.status != 404 {
            check(&a, what)?;
        }
        self.wait(&a, what)
    }
}

// -------------------------------------------------------------------- gcp

pub struct Gcp {
    endpoint: String,
}

impl Gcp {
    fn new(plan: &Plan) -> R<Gcp> {
        if plan.project == "?" {
            need_env("GOOGLE_CLOUD_PROJECT")?;
        }
        Ok(Gcp { endpoint: std::env::var("NEXO_GCP_ENDPOINT").unwrap_or_default().trim_end_matches('/').to_string() })
    }

    fn url(&self, url: &str) -> String {
        if !self.endpoint.is_empty() {
            if let Some(rest) = url.strip_prefix("https://") {
                let (host, path) = rest.split_once('/').unwrap_or((rest, ""));
                if let Some(service) = host.strip_suffix(".googleapis.com") {
                    return format!("{}/{service}/{path}", self.endpoint);
                }
            }
        }
        url.to_string()
    }

    fn send(&self, method: &str, url: &str, body: Option<&Json>) -> R<Answer> {
        let token = need_env("GOOGLE_OAUTH_ACCESS_TOKEN")?;
        let mut headers = vec![("Authorization".to_string(), format!("Bearer {token}"))];
        let data = body.map(|b| b.compact().into_bytes());
        if data.is_some() {
            headers.push(("Content-Type".into(), "application/json".into()));
        }
        http(method, &self.url(url), &headers, data.as_deref())
    }

    fn wait(&self, create_url: &str, mut op: Json, what: &str) -> R<()> {
        let (mut waited, mut i) = (0.0, 0);
        loop {
            let poll = if op.get("done").bool() == Some(false) && op.get("name").str().is_some() {
                let (scheme, netloc, path, _) = split_url(create_url);
                let version = path.trim_matches('/').split('/').next().unwrap_or("").to_string();
                format!("{scheme}://{netloc}/{version}/{}", op.get("name").str().unwrap())
            } else if op.get("kind").str().is_some_and(|k| k.ends_with("#operation"))
                && !matches!(op.get("status"), Json::Null)
                && op.get("status").str() != Some("DONE")
                && op.get("selfLink").str().is_some()
            {
                op.get("selfLink").str().unwrap().to_string()
            } else {
                if op.get("error").truthy() {
                    return Err(api_fail(what, "error", op.get("error").compact()));
                }
                return Ok(());
            };
            let pause = POLL[i.min(POLL.len() - 1)];
            sleep(pause);
            waited += pause;
            i += 1;
            if waited > MAX_WAIT {
                return Err(fail_d(Diagnostic::new_spanless("infra_timeout").arg("what", what)));
            }
            let a = self.send("GET", &poll, None)?;
            check(&a, what)?;
            op = a.json();
        }
    }

    fn policy(&self, resource: &str, what: &str) -> R<Json> {
        let a = if resource.ends_with("/iam") {
            self.send("GET", resource, None)?
        } else {
            self.send("POST", &format!("{resource}:getIamPolicy"), Some(&Json::Obj(Vec::new())))?
        };
        check(&a, what)?;
        Ok(a.json())
    }

    fn set_policy(&self, resource: &str, policy: &Json, what: &str) -> R<()> {
        let a = if resource.ends_with("/iam") {
            self.send("PUT", resource, Some(policy))?
        } else {
            self.send("POST", &format!("{resource}:setIamPolicy"), Some(&json::obj(vec![("policy", policy.clone())])))?
        };
        check(&a, what)
    }

    fn member(&self, desc: &Json, body: &Json, add: bool, what: &str) -> R<()> {
        let resource = desc.get("self").str().unwrap_or("").to_string();
        let mut policy = self.policy(&resource, what)?;
        if !policy.get("bindings").is_null() && !matches!(policy.get("bindings"), Json::Arr(_)) {
            policy.set("bindings", Json::Arr(Vec::new()));
        }
        if !policy.has("bindings") {
            policy.set("bindings", Json::Arr(Vec::new()));
        }
        let role = body.get("role").clone();
        let member = body.get("member").clone();
        let Some(Json::Arr(bindings)) = policy.get_mut("bindings") else { unreachable!() };
        let pos = bindings.iter().position(|b| *b.get("role") == role);
        if add {
            let i = match pos {
                Some(i) => i,
                None => {
                    bindings.push(json::obj(vec![("role", role.clone()), ("members", Json::Arr(Vec::new()))]));
                    bindings.len() - 1
                }
            };
            if let Some(Json::Arr(members)) = bindings[i].get_mut("members") {
                if !members.contains(&member) {
                    members.push(member);
                }
            }
        } else if let Some(i) = pos {
            let mut empty = false;
            if let Some(Json::Arr(members)) = bindings[i].get_mut("members") {
                members.retain(|x| *x != member);
                empty = members.is_empty();
            }
            if empty {
                bindings.remove(i);
            }
        }
        self.set_policy(&resource, &policy, what)
    }

    fn add_version(&self, desc: &Json, what: &str) -> R<()> {
        let value = match desc.get("value") {
            Json::Str(s) => s.clone(),
            Json::Null => String::new(),
            other => I::text_of(other),
        };
        let data = crate::crypto::b64(value.as_bytes());
        let body = json::obj(vec![("payload", json::obj(vec![("data", json::s(data))]))]);
        let a = self.send("POST", &format!("{}:addVersion", desc.get("self").str().unwrap_or("")), Some(&body))?;
        check(&a, what)
    }

    fn create_url(desc: &Json) -> (String, String) {
        let c = desc.get("create").str().unwrap_or(" ");
        let (m, u) = c.split_once(' ').unwrap_or((c, ""));
        (m.to_string(), u.to_string())
    }
}

impl Client for Gcp {
    fn read(&mut self, desc: &Json, ident: &str, what: &str) -> R<Option<Json>> {
        if desc.get("op").str() == Some("iam") {
            let policy = self.policy(ident, what)?;
            let want = desc.get("body");
            for b in policy.get("bindings").arr() {
                if b.get("role") == want.get("role") && b.get("members").arr().contains(want.get("member")) {
                    return Ok(Some(want.clone()));
                }
            }
            return Ok(None);
        }
        let a = self.send("GET", ident, None)?;
        if a.status == 404 {
            return Ok(None);
        }
        check(&a, what)?;
        Ok(Some(a.json()))
    }

    fn create(&mut self, desc: &Json, body: &Json, what: &str) -> R<(String, Json)> {
        let me = desc.get("self").str().unwrap_or("").to_string();
        if desc.get("op").str() == Some("iam") {
            self.member(desc, body, true, what)?;
            return Ok((me, body.clone()));
        }
        let (method, url) = Gcp::create_url(desc);
        let a = self.send(&method, &url, Some(body))?;
        check(&a, what)?;
        self.wait(&url, a.json(), what)?;
        if desc.get("op").str() == Some("secret") {
            self.add_version(desc, what)?;
        }
        let read = self.read(desc, &me, what)?.unwrap_or(Json::Null);
        Ok((me, read))
    }

    fn update(&mut self, desc: &Json, ident: &str, _old: &Json, body: &Json, what: &str) -> R<(String, Json)> {
        if desc.get("op").str() == Some("iam") {
            return self.create(desc, body, what);
        }
        if desc.get("op").str() == Some("secret") {
            self.add_version(desc, what)?;
            let read = self.read(desc, ident, what)?.unwrap_or(Json::Null);
            return Ok((ident.to_string(), read));
        }
        let method = desc.get("update").str().unwrap_or("PATCH").to_string();
        let a = self.send(&method, ident, Some(body))?;
        check(&a, what)?;
        self.wait(&Gcp::create_url(desc).1, a.json(), what)?;
        let read = self.read(desc, ident, what)?.unwrap_or(Json::Null);
        Ok((ident.to_string(), read))
    }

    fn delete(&mut self, desc: &Json, ident: &str, what: &str) -> R<()> {
        if desc.get("op").str() == Some("iam") {
            return self.member(desc, desc.get("body"), false, what);
        }
        let a = self.send("DELETE", ident, None)?;
        if a.status >= 400 && a.status != 404 {
            check(&a, what)?;
        }
        if a.status < 300 {
            self.wait(&Gcp::create_url(desc).1, a.json(), what)?;
        }
        Ok(())
    }
}

// -------------------------------------------------------------------- k8s

pub struct K8s {
    server: String,
    token: Option<String>,
    namespace: String,
    ca: Option<String>,
}

impl K8s {
    fn new(plan: &Plan) -> R<K8s> {
        let mut server = std::env::var("NEXO_K8S_SERVER").ok().filter(|s| !s.is_empty());
        if server.is_none() {
            if let Ok(host) = std::env::var("KUBERNETES_SERVICE_HOST") {
                let port = std::env::var("KUBERNETES_SERVICE_PORT").unwrap_or_else(|_| "443".into());
                server = Some(format!("https://{host}:{port}"));
            }
        }
        let Some(server) = server else { return Err(fail_d(Diagnostic::new_spanless("infra_credentials").arg("name", "NEXO_K8S_SERVER"))) };
        let mut token = std::env::var("NEXO_K8S_TOKEN").ok().filter(|t| !t.is_empty());
        let mut ca = None;
        let sa = "/var/run/secrets/kubernetes.io/serviceaccount";
        if token.is_none() {
            token = std::fs::read_to_string(format!("{sa}/token")).ok().map(|t| t.trim().to_string());
            if token.is_some() && std::path::Path::new(&format!("{sa}/ca.crt")).exists() {
                ca = Some(format!("{sa}/ca.crt"));
            }
        }
        // the certificate authority of a cluster outside (kind, minikube...)
        if let Some(path) = std::env::var("NEXO_K8S_CA").ok().filter(|p| !p.is_empty()) {
            if !std::path::Path::new(&path).exists() {
                return Err(fail_d(Diagnostic::new_spanless("infra_ca").arg("path", path)));
            }
            ca = Some(path);
        }
        Ok(K8s { server: server.trim_end_matches('/').to_string(), token, namespace: plan.namespace.clone(), ca })
    }

    fn path(&self, desc: &Json) -> String {
        let api = desc.get("api").str().unwrap_or("");
        let base = if api == "v1" { "/api/v1".to_string() } else { format!("/apis/{api}") };
        let ns = if desc.get("cluster_scope").bool() == Some(true) { String::new() } else { format!("/namespaces/{}", self.namespace) };
        format!("{base}{ns}/{}/{}", desc.get("plural").str().unwrap_or(""), desc.get("name").str().unwrap_or(""))
    }

    fn send(&self, method: &str, path: &str, body: Option<&Json>, content: &str) -> R<Answer> {
        let mut headers = Vec::new();
        if let Some(t) = &self.token {
            headers.push(("Authorization".to_string(), format!("Bearer {t}")));
        }
        let data = body.map(|b| b.compact().into_bytes());
        if data.is_some() {
            headers.push(("Content-Type".into(), content.to_string()));
        }
        http_with(method, &format!("{}{path}", self.server), &headers, data.as_deref(), self.ca.as_deref())
    }
}

impl Client for K8s {
    fn read(&mut self, _desc: &Json, ident: &str, what: &str) -> R<Option<Json>> {
        let a = self.send("GET", ident, None, "application/json")?;
        if a.status == 404 {
            return Ok(None);
        }
        check(&a, what)?;
        Ok(Some(a.json()))
    }

    fn create(&mut self, desc: &Json, body: &Json, what: &str) -> R<(String, Json)> {
        let path = self.path(desc);
        let a = self.send("PATCH", &format!("{path}?fieldManager=nexo&force=true"), Some(body), "application/apply-patch+yaml")?;
        check(&a, what)?;
        Ok((path, a.json()))
    }

    fn update(&mut self, desc: &Json, _ident: &str, _old: &Json, body: &Json, what: &str) -> R<(String, Json)> {
        self.create(desc, body, what)
    }

    fn delete(&mut self, _desc: &Json, ident: &str, what: &str) -> R<()> {
        let a = self.send("DELETE", ident, None, "application/json")?;
        if a.status >= 400 && a.status != 404 {
            check(&a, what)?;
        }
        Ok(())
    }
}

fn client(plan: &Plan) -> R<Box<dyn Client>> {
    Ok(match plan.cloud.as_str() {
        "aws" => Box::new(Aws::new(plan)),
        "azure" => Box::new(Azure::new(plan)?),
        "gcp" => Box::new(Gcp::new(plan)?),
        _ => Box::new(K8s::new(plan)?),
    })
}

// --------------------------------------------------------------- commands

/// The value of each secret a program uses -> what the state keeps.
fn secret_values(plan: &Plan) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for r in &plan.infra.resources {
        for item in plan.ctx(&r.name).get("secrets").arr() {
            let v = item.get("value").str().unwrap_or("");
            let name = v.trim_start_matches("${secret.").trim_end_matches('}');
            if let Ok(value) = std::env::var(name) {
                if !value.is_empty() {
                    let h = I::secret_hash(name);
                    out.push((value.clone(), h.clone()));
                    out.push((crate::crypto::b64(value.as_bytes()), h));
                }
            }
        }
    }
    out
}

/// What the cloud answers, without the secrets it echoes.
fn scrub(v: &Json, secrets: &[(String, String)]) -> Json {
    match v {
        Json::Str(s) => match secrets.iter().find(|(k, _)| k == s) {
            Some((_, h)) => json::s(h.clone()),
            None => v.clone(),
        },
        Json::Arr(a) => Json::Arr(a.iter().map(|x| scrub(x, secrets)).collect()),
        Json::Obj(o) => Json::Obj(o.iter().map(|(k, x)| (k.clone(), scrub(x, secrets))).collect()),
        other => other.clone(),
    }
}

fn native_by_id(plan: &mut Plan, nid: &str, reveal: bool) -> R<Option<I::NativeRes>> {
    plan.reveal = reveal;
    let r = plan.natives();
    plan.reveal = false;
    Ok(r?.into_iter().find(|n| n.id == nid))
}

fn confirm(prompt: &str, answers: &[&str]) -> bool {
    crate::state::flush();
    print!("{prompt}");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    let _ = std::io::stdin().read_line(&mut line);
    answers.contains(&line.trim().to_lowercase().as_str())
}

fn outputs_lines(plan: &Plan) -> R<Vec<String>> {
    let mut out = Vec::new();
    for o in &plan.infra.outputs {
        if o.secret {
            out.push(format!("{} = {}", o.name, m("info.secret_output", &[])));
            continue;
        }
        let mut deps = Vec::new();
        let v = if o.parts.len() == 3 { plan.peer_attr(&o.parts)? } else { plan.attr(&o.parts[0], &o.parts[1], &mut deps)? };
        let text = match &v {
            Some(v) if !I::is_unknown(v) => I::text_of(v),
            _ => m("info.unknown_value", &[]),
        };
        out.push(format!("{} = {text}", o.name));
    }
    Ok(out)
}

fn desc_text(d: &Json) -> String {
    format!("{} {}", d.get("type").str().unwrap_or(""), d.get("name").str().unwrap_or(""))
}

fn entry_of(n: &I::NativeRes, desc: &Json, ident: &str, read: Json) -> Json {
    json::obj(vec![
        ("id", json::s(n.id.clone())),
        ("resource", json::s(n.resource.clone())),
        ("kind", json::s(n.kind.clone())),
        ("desc", desc.clone()),
        ("identifier", json::s(ident)),
        ("read", read),
    ])
}

fn save(plan: &Plan, store: &dyn Store) -> R<()> {
    store.save(&plan.state.as_ref().unwrap().data(plan))
}

fn apply(plan: &mut Plan, store: &dyn Store, yes: bool) -> R<i32> {
    I::check_plan(plan)?;
    let actions = I::diff(plan)?;
    for line in I::plan_lines(plan, &actions)? {
        say(&line);
    }
    if actions.iter().all(|a| a.op == "keep") {
        for line in outputs_lines(plan)? {
            say(&line);
        }
        return Ok(0);
    }
    if !yes {
        if std::env::var("CI").is_ok_and(|v| !v.is_empty()) {
            return Err(fail_d(Diagnostic::new_spanless("apply_needs_yes")));
        }
        if !confirm(&m("info.apply_confirm", &[]), &["yes", "y"]) {
            say(&m("info.apply_cancelled", &[]));
            return Ok(1);
        }
    }
    take_lock(plan, store)?;
    let result = (|| -> R<(usize, usize, usize)> {
        let mut c = client(plan)?;
        let secrets = secret_values(plan);
        let (mut created, mut changed, mut deleted) = (0, 0, 0);
        for a in &actions {
            if a.op == "keep" {
                continue;
            }
            if a.op == "delete" || a.op == "replace" {
                let entry = a.entry.as_ref().unwrap();
                let what = desc_text(entry.get("desc"));
                c.delete(entry.get("desc"), entry.get("identifier").str().unwrap_or(""), &what)?;
                let id = entry.get("id").str().unwrap_or("").to_string();
                plan.state.as_mut().unwrap().remove(&id);
                plan.fresh.retain(|(i, _)| *i != id);
                plan.state.as_mut().unwrap().serial += 1;
                save(plan, store)?;
                say(&format!("  - {what}"));
                deleted += 1;
                if a.op == "delete" {
                    continue;
                }
            }
            let nid = a.native.as_ref().unwrap().id.clone();
            let n = native_by_id(plan, &nid, true)?.unwrap();
            let shown = native_by_id(plan, &nid, false)?.unwrap();
            let what = format!("{} {}", n.typ(), n.name());
            let (ident, read, sym) = if a.op == "update" {
                let entry = a.entry.as_ref().unwrap();
                let (i, r) = c.update(&n.desc, entry.get("identifier").str().unwrap_or(""), entry.get("desc").get("body"), n.desc.get("body"), &what)?;
                changed += 1;
                (i, r, "~")
            } else {
                let (i, r) = c.create(&n.desc, n.desc.get("body"), &what)?;
                created += 1;
                (i, r, "+")
            };
            plan.fresh.retain(|(i, _)| *i != n.id);
            plan.fresh.push((n.id.clone(), read.clone()));
            let entry = entry_of(&n, &shown.desc, &ident, scrub(&read, &secrets));
            let st = plan.state.as_mut().unwrap();
            st.put(&n.id, entry);
            st.serial += 1;
            save(plan, store)?;
            say(&format!("  {sym} {what}"));
        }
        let mut outs = Vec::new();
        for line in outputs_lines(plan)? {
            let (name, value) = line.split_once(" = ").unwrap_or((&line, ""));
            outs.push((name.to_string(), json::s(value)));
        }
        plan.state.as_mut().unwrap().outputs = outs;
        save(plan, store)?;
        Ok((created, changed, deleted))
    })();
    store.unlock();
    let (created, changed, deleted) = result?;
    for line in outputs_lines(plan)? {
        say(&line);
    }
    say(&m("info.apply_done", &[("create", created.to_string()), ("change", changed.to_string()), ("delete", deleted.to_string())]));
    Ok(0)
}

/// Lock the state, and check that no other command changed it since this
/// one read it (while it asked for confirmation, for example).
fn take_lock(plan: &Plan, store: &dyn Store) -> R<()> {
    if !store.lock()? {
        return Err(fail_d(Diagnostic::new_spanless("state_locked").arg("where", store.where_())));
    }
    let now = match store.load() {
        Ok(d) => I::State::new(d).serial,
        Err(e) => {
            store.unlock();
            return Err(e);
        }
    };
    if now != plan.state.as_ref().map(|s| s.serial).unwrap_or(0) {
        store.unlock();
        return Err(fail_d(Diagnostic::new_spanless("state_changed").arg("where", store.where_())));
    }
    Ok(())
}

fn destroy(plan: &mut Plan, store: &dyn Store) -> R<i32> {
    let entries: Vec<(String, Json)> = plan.state.as_ref().unwrap().entries.clone();
    let infra = plan.infra.name.clone();
    if entries.is_empty() {
        say(&m("info.destroy_none", &[("infra", infra)]));
        return Ok(0);
    }
    say(&m("info.destroy_list", &[("infra", infra.clone()), ("n", entries.len().to_string())]));
    for (_, e) in entries.iter().rev() {
        say(&format!("  - {}", desc_text(e.get("desc"))));
    }
    if !confirm(&m("info.destroy_confirm", &[("infra", infra.clone())]), &[infra.to_lowercase().as_str()]) {
        say(&m("info.apply_cancelled", &[]));
        return Ok(1);
    }
    take_lock(plan, store)?;
    let result = (|| -> R<()> {
        let mut c = client(plan)?;
        for (nid, e) in entries.iter().rev() {
            let what = desc_text(e.get("desc"));
            c.delete(e.get("desc"), e.get("identifier").str().unwrap_or(""), &what)?;
            let st = plan.state.as_mut().unwrap();
            st.remove(nid);
            st.serial += 1;
            save(plan, store)?;
            say(&format!("  - {what}"));
        }
        plan.state.as_mut().unwrap().outputs = Vec::new();
        save(plan, store)
    })();
    store.unlock();
    result?;
    say(&m("info.destroy_done", &[("infra", infra), ("n", entries.len().to_string())]));
    Ok(0)
}

/// The paths whose value in the cloud is not the one Nexo set.
fn differences(want: &Json, have: Option<&Json>, prefix: &str, out: &mut Vec<(String, Json, Option<Json>)>) {
    let join = |k: &str| if prefix.is_empty() { k.to_string() } else { format!("{prefix}.{k}") };
    match (want, have) {
        (Json::Obj(w), Some(h @ Json::Obj(_))) => {
            for (k, v) in w {
                differences(v, if h.has(k) { Some(h.get(k)) } else { None }, &join(k), out);
            }
        }
        (Json::Obj(_), _) => out.push((prefix.to_string(), want.clone(), have.cloned())),
        (Json::Arr(w), Some(Json::Arr(h))) if w.len() == h.len() => {
            for (i, (v, x)) in w.iter().zip(h).enumerate() {
                differences(v, Some(x), &join(&i.to_string()), out);
            }
        }
        (Json::Str(s), _) if s.starts_with(I::UNKNOWN) || s.starts_with("sha256:") => {}
        _ => {
            if !have.is_some_and(|h| I::same(want, h)) {
                out.push((prefix.to_string(), want.clone(), have.cloned()));
            }
        }
    }
}

fn drift(plan: &Plan) -> R<i32> {
    let mut c = client(plan)?;
    let secrets = secret_values(plan);
    say(&m(
        "info.drift_header",
        &[("infra", plan.infra.name.clone()), ("cloud", plan.cloud.clone()), ("stage", plan.stage.clone().unwrap_or_else(|| "default".into()))],
    ));
    let mut found = 0;
    for (_, e) in &plan.state.as_ref().unwrap().entries {
        let desc = e.get("desc");
        let what = desc_text(desc);
        let have = c.read(desc, e.get("identifier").str().unwrap_or(""), &what)?;
        let Some(have) = have.map(|h| scrub(&h, &secrets)) else {
            say(&format!("  - {what}: {}", m("info.drift_gone", &[])));
            found += 1;
            continue;
        };
        let mut diffs = Vec::new();
        differences(desc.get("body"), Some(&have), "", &mut diffs);
        for (path, want, now) in diffs {
            let now = now.filter(|n| !n.is_null()).map(|n| I::text_of(&n)).unwrap_or_else(|| "-".into());
            say(&format!("  ~ {what}: {path} ({} -> {now})", I::text_of(&want)));
            found += 1;
        }
    }
    if found == 0 {
        say(&m("info.drift_none", &[]));
    } else {
        say(&m("info.drift_found", &[("n", found.to_string())]));
    }
    Ok(0)
}

fn import(plan: &mut Plan, store: &dyn Store, target: &str, ident: &str) -> R<i32> {
    let (name, nid) = target.split_once('.').unwrap_or((target, ""));
    if plan.infra.resource(name).is_none() {
        return Err(fail_d(Diagnostic::new_spanless("import_resource").arg("name", name).arg("infra", plan.infra.name.clone())));
    }
    let all = plan.natives()?;
    let mut natives: Vec<I::NativeRes> = Vec::new();
    let mut names = Vec::new();
    for n in all {
        if n.resource == name {
            names.push(n.id.clone());
            if nid.is_empty() || n.id == format!("{name}.{nid}") {
                natives.push(n);
            }
        }
    }
    if natives.len() != 1 {
        return Err(fail_d(Diagnostic::new_spanless("import_which").arg("name", name).arg("natives", names.join(", "))));
    }
    let n = natives.pop().unwrap();
    let what = format!("{} {}", n.typ(), n.name());
    let mut c = client(plan)?;
    let Some(read) = c.read(&n.desc, ident, &what)? else {
        return Err(fail_d(Diagnostic::new_spanless("import_missing").arg("what", what).arg("id", ident)));
    };
    take_lock(plan, store)?;
    let secrets = secret_values(plan);
    let entry = entry_of(&n, &n.desc, ident, scrub(&read, &secrets));
    let st = plan.state.as_mut().unwrap();
    st.put(&n.id, entry);
    st.serial += 1;
    let r = save(plan, store);
    store.unlock();
    r?;
    say(&m("info.imported", &[("what", what), ("id", ident.to_string())]));
    Ok(0)
}

pub fn command(name: &str, args: &[String], stage: Option<&str>, yes: bool) -> R<i32> {
    let mut infras = I::infras();
    if infras.is_empty() {
        let file = crate::state::program().file;
        return Err(fail_d(Diagnostic::new_spanless("infra_none").arg("file", file)));
    }
    if name == "import" {
        let cloud = args[0].clone();
        let mut matching = Vec::new();
        for x in &infras {
            if I::Plan::new(x, stage).map(|p| p.cloud == cloud).unwrap_or(false) {
                matching.push(*x);
            } else {
                crate::state::clear();
            }
        }
        if matching.is_empty() {
            return Err(fail_d(Diagnostic::new_spanless("import_cloud").arg("cloud", cloud)));
        }
        let res = args[1].split_once('.').map(|x| x.0).unwrap_or(&args[1]).to_string();
        let pick = matching.iter().find(|x| x.resource(&res).is_some()).copied().unwrap_or(matching[0]);
        infras = vec![pick];
    }
    // destroy: the blocks that use values of others go first
    let infras = if name == "destroy" {
        infras.reverse();
        let mut v = I::infra_order(&infras)?;
        v.reverse();
        v
    } else {
        I::infra_order(&infras)?
    };
    let mut status = 0;
    for infra in infras {
        let mut plan = Plan::new(infra, stage)?;
        let store = I::store_of(&plan)?;
        plan.state = Some(I::State::new(store.load()?));
        match name {
            "cost" => {
                for line in I::cost_lines(&plan) {
                    say(&line);
                }
            }
            "plan" => {
                I::check_plan(&plan)?;
                let actions = I::diff(&plan)?;
                for line in I::plan_lines(&plan, &actions)? {
                    say(&line);
                }
            }
            "apply" => status |= apply(&mut plan, store.as_ref(), yes)?,
            "destroy" => status |= destroy(&mut plan, store.as_ref())?,
            "drift" => status |= drift(&plan)?,
            _ => status |= import(&mut plan, store.as_ref(), &args[1], &args[2])?,
        }
    }
    Ok(status)
}

/// `nexo plan|apply|destroy|drift|cost|import`: the program's infra and
/// stage blocks have been registered; the driver passes the command.
pub fn run_command() -> i32 {
    crate::state::flush();
    let name = std::env::var("NEXO_INFRA_COMMAND").unwrap_or_default();
    let args: Vec<String> = std::env::var("NEXO_INFRA_ARGS").unwrap_or_default().split('\n').filter(|s| !s.is_empty()).map(String::from).collect();
    let stage = std::env::var("NEXO_INFRA_STAGE").ok().filter(|s| !s.is_empty());
    let yes = std::env::var_os("NEXO_INFRA_YES").is_some();
    match command(&name, &args, stage.as_deref(), yes) {
        Ok(code) => code,
        Err(_) => {
            crate::state::report_pending();
            1
        }
    }
}
