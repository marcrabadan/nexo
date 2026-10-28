//! Public HTTP(S) broker. The confined payload retains the offline syscall
//! filter; only this separately supervised process has network authority.
use crate::json::Json;
use super::{Capability, Effect, Engine, Kind, Rule};
use std::io::{self, Read, Write};
use std::net::{IpAddr, SocketAddr, ToSocketAddrs};
use std::time::Duration;

pub const PROFILE: &str = "linux-online-v1";
pub const MAX_REQUEST: usize = 1024 * 1024;
pub const MAX_RESPONSE: usize = 8 * 1024 * 1024;

#[derive(Clone, Debug, Default)]
pub struct Policy { pub allow: Vec<String>, pub deny: Vec<String> }
impl Policy {
    fn engine(&self) -> Engine {
        Engine::new([(Effect::Allow, &self.allow), (Effect::Deny, &self.deny)].into_iter()
            .filter(|(_, targets)| !targets.is_empty())
            .map(|(effect, targets)| Rule { effect, kind: Kind::Net, targets: targets.clone() }).collect())
    }
}

fn invalid() -> io::Error { io::Error::other("Shield online request rejected") }

/// Refuse proxies rather than delegating DNS or routing decisions to one.
/// This check runs in the launcher before the helper environment is cleared.
pub fn reject_proxies() -> Result<(), String> {
    if ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
        .iter().any(|k| std::env::var_os(k).is_some_and(|v| !v.is_empty())) {
        return Err("linux-online-v1 does not support configured proxies; use an execution host with reviewed direct egress.".into());
    }
    Ok(())
}

pub fn probe() -> String {
    let mut result = crate::json::parse(&super::sandbox::probe()).unwrap_or(Json::Obj(vec![]));
    let direct = reject_proxies().is_ok();
    #[cfg(unix)]
    let ipc = std::os::unix::net::UnixStream::pair().is_ok();
    #[cfg(not(unix))]
    let ipc = false;
    let supported = result.get("supported").bool() == Some(true) && direct && ipc;
    result.set("profile",Json::Str(PROFILE.into()));
    result.set("proxies_unconfigured",Json::Bool(direct));
    result.set("broker_ipc",Json::Bool(ipc));
    result.set("supported",Json::Bool(supported));
    result.compact()
}

/// Conservative public unicast set. Special, transition, mapped, multicast,
/// documentation and internal ranges are denied, even if the hostname is allowed.
pub fn public_address(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            let n = u32::from(ip);
            ![(0x00000000,8), (0x0a000000,8), (0x64400000,10), (0x7f000000,8),
              (0xa9fe0000,16), (0xac100000,12), (0xc0000000,24), (0xc0000200,24),
              (0xc0586300,24), (0xc0a80000,16), (0xc6120000,15), (0xc6336400,24),
              (0xcb007100,24), (0xe0000000,3)]
                .iter().any(|&(base,bits)| n >> (32-bits) == base >> (32-bits))
        }
        IpAddr::V6(ip) => {
            let n = u128::from(ip);
            n >> 125 == 1 // 2000::/3 only
                && n >> 105 != u128::from(0x2001u16) << 7 // 2001::/23 special assignments
                && n >> 96 != 0x20010db8 // documentation
                && n >> 112 != 0x2002 // 6to4
                && n >> 108 != 0x3fff0 // documentation 3fff::/20
        }
    }
}

fn target(text: &str, policy: &Policy) -> io::Result<url::Url> {
    if text.bytes().any(|c| c == b'\\' || c.is_ascii_control()) { return Err(invalid()); }
    let url = url::Url::parse(text).map_err(|_| invalid())?;
    if !matches!(url.scheme(), "http" | "https") || !url.username().is_empty() || url.password().is_some()
        || url.fragment().is_some() || url.port_or_known_default() != Some(if url.scheme() == "https" {443} else {80}) {
        return Err(invalid());
    }
    let host = url.host_str().ok_or_else(invalid)?;
    if policy.engine().evaluate(Capability::NetworkConnect(host), true, true).denied() { return Err(invalid()); }
    Ok(url)
}

fn pinned(addresses: Vec<SocketAddr>) -> io::Result<Vec<SocketAddr>> {
    if addresses.is_empty() || addresses.len() > 64 || addresses.iter().any(|a| !public_address(a.ip())) {
        return Err(invalid());
    }
    Ok(addresses)
}

/// Length-delimited frames avoid buffering past the initial environment frame.
pub fn read_frame(r: &mut impl Read, max: usize) -> io::Result<Vec<u8>> {
    let mut len = [0;4]; r.read_exact(&mut len)?;
    let len = u32::from_be_bytes(len) as usize;
    if len > max { return Err(invalid()); }
    let mut bytes = vec![0;len]; r.read_exact(&mut bytes)?; Ok(bytes)
}
pub fn write_frame(w: &mut impl Write, bytes: &[u8], max: usize) -> io::Result<()> {
    if bytes.len() > max { return Err(invalid()); }
    w.write_all(&(bytes.len() as u32).to_be_bytes())?; w.write_all(bytes)?; w.flush()
}

fn parse(bytes: &[u8]) -> io::Result<Json> {
    // Bound nesting before entering the runtime's recursive JSON parser.
    let text = std::str::from_utf8(bytes).map_err(|_| invalid())?;
    let (mut depth, mut quoted, mut escaped) = (0i32, false, false);
    for c in text.bytes() {
        if quoted {
            if escaped { escaped = false; } else if c == b'\\' { escaped = true; } else if c == b'"' { quoted = false; }
        } else if c == b'"' { quoted = true; }
        else if c == b'[' || c == b'{' { depth += 1; if depth > 8 { return Err(invalid()); } }
        else if c == b']' || c == b'}' { depth -= 1; if depth < 0 { return Err(invalid()); } }
    }
    crate::json::parse_unique(text).ok_or_else(invalid)
}

#[cfg(unix)]
fn channel() -> io::Result<std::fs::File> {
    use std::os::fd::BorrowedFd;
    // SAFETY: stdin is supplied by the trusted supervisor and remains open.
    // Clone ownership; never close the process's original standard descriptor.
    Ok(unsafe { BorrowedFd::borrow_raw(0) }.try_clone_to_owned()?.into())
}

fn headers(json: &Json) -> io::Result<Vec<(String,String)>> {
    let Json::Arr(items) = json else { return Err(invalid()) };
    if items.len() > 128 { return Err(invalid()); }
    items.iter().map(|item| {
        let Json::Arr(pair) = item else { return Err(invalid()) };
        if pair.len() != 2 { return Err(invalid()); }
        Ok((pair[0].str().ok_or_else(invalid)?.into(), pair[1].str().ok_or_else(invalid)?.into()))
    }).collect()
}
fn header_json(h: &[(String,String)]) -> Json {
    Json::Arr(h.iter().map(|(k,v)| Json::Arr(vec![Json::Str(k.clone()),Json::Str(v.clone())])).collect())
}

fn exchange(request: &Json, body: &[u8], policy: &Policy) -> io::Result<(Json, Vec<u8>)> {
    let url = target(request.get("url").str().ok_or_else(invalid)?, policy)?;
    let method = request.get("method").str().ok_or_else(invalid)?;
    if !["GET","HEAD","POST","PUT","PATCH","DELETE","OPTIONS"].contains(&method) { return Err(invalid()); }
    let headers = headers(request.get("headers"))?;
    for (name,value) in &headers {
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b))
            || value.bytes().any(|b| b.is_ascii_control())
            || ["host","connection","content-length","transfer-encoding","proxy-authorization","proxy-connection","upgrade","te","trailer"]
                .contains(&name.to_ascii_lowercase().as_str()) { return Err(invalid()); }
    }
    // Resolve once, validate the entire answer set and give the transport only
    // those addresses. The URL hostname remains intact for Host, TLS SNI and
    // certificate verification. No connection pooling or automatic redirects.
    let host = url.host_str().ok_or_else(invalid)?.trim_matches(['[',']']).to_string();
    let port = url.port_or_known_default().ok_or_else(invalid)?;
    let addresses = pinned((host.as_str(),port).to_socket_addrs()?.collect())?;
    transport(request, body, &url, addresses)
}

// Only exchange can reach this transport in production, after target and full
// DNS answer validation. Unit tests use a local peer here without weakening
// the production address validator or adding a runtime test override.
fn transport(request: &Json, body: &[u8], url: &url::Url, addresses: Vec<SocketAddr>) -> io::Result<(Json, Vec<u8>)> {
    let method = request.get("method").str().ok_or_else(invalid)?;
    let headers = headers(request.get("headers"))?;
    let expected = format!("{}:{}", url.host_str().ok_or_else(invalid)?, url.port_or_known_default().ok_or_else(invalid)?);
    let seconds = request.get("timeout_ms").int().ok_or_else(invalid)?;
    if !(1..=30000).contains(&seconds) { return Err(invalid()); }
    let agent = ureq::AgentBuilder::new().redirects(0).try_proxy_from_env(false)
        .timeout(Duration::from_millis(seconds as u64)).user_agent("nexo-shield/0.1")
        .resolver(move |name: &str| {
            if name != expected { return Err(invalid()); }
            Ok(addresses.clone())
        }).build();
    let mut req = agent.request(method, url.as_str());
    for (k,v) in headers { req = req.set(&k,&v); }
    let response = if request.get("body").bool() == Some(true) { req.send_bytes(body) } else { req.call() };
    let response = match response { Ok(r) | Err(ureq::Error::Status(_,r)) => r, Err(_) => return Err(invalid()) };
    let status = response.status() as i64;
    let mut hs = Vec::new();
    for name in response.headers_names() {
        if hs.len() >= 128 { return Err(invalid()); }
        hs.push((name.clone(),response.all(&name).join(", ")));
    }
    let mut data = Vec::new();
    response.into_reader().take(MAX_RESPONSE as u64 + 1).read_to_end(&mut data)?;
    if data.len() > MAX_RESPONSE { return Err(invalid()); }
    Ok((Json::Obj(vec![("status".into(),Json::Int(status)),("headers".into(),header_json(&hs))]),data))
}

/// Internal helper command; its PID is owned, stopped and reaped by supervise.
#[cfg(unix)]
pub fn serve(policy: &Policy) -> io::Result<()> {
    let mut io = channel()?;
    loop {
        let bytes = read_frame(&mut io,MAX_REQUEST)?;
        let body = read_frame(&mut io,MAX_REQUEST)?;
        let result = parse(&bytes).and_then(|r| exchange(&r,&body,policy));
        let (meta,body) = result.unwrap_or_else(|_| (Json::Obj(vec![("error".into(),Json::Bool(true))]),Vec::new()));
        write_frame(&mut io,meta.compact().as_bytes(),MAX_REQUEST)?;
        write_frame(&mut io,&body,MAX_RESPONSE)?;
    }
}

/// Runtime adapter. Policy/dependency checks remain in http::once, before IPC.
pub fn request(method: &str, url: &str, body: &Option<(Vec<u8>, &str)>, hs: &[(String,String)], seconds: f64)
    -> io::Result<crate::http::Response> {
    #[cfg(unix)] {
        static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        let _guard = LOCK.lock().map_err(|_| invalid())?;
        let mut io = channel()?;
        let mut hs = hs.to_vec();
        if let Some((_,kind)) = body { hs.insert(0,("Content-Type".into(),kind.to_string())); }
        let meta = Json::Obj(vec![("method".into(),Json::Str(method.into())),("url".into(),Json::Str(url.into())),
            ("headers".into(),header_json(&hs)),("body".into(),Json::Bool(body.is_some())),
            ("timeout_ms".into(),Json::Int((seconds * 1000.0).clamp(1.0,30000.0) as i64))]);
        write_frame(&mut io,meta.compact().as_bytes(),MAX_REQUEST)?;
        write_frame(&mut io,body.as_ref().map(|(v,_)| v.as_slice()).unwrap_or_default(),MAX_REQUEST)?;
        let meta = parse(&read_frame(&mut io,MAX_REQUEST)?)?;
        let body = read_frame(&mut io,MAX_RESPONSE)?;
        if meta.get("error").bool() == Some(true) { return Err(invalid()); }
        Ok(crate::http::Response { status: meta.get("status").int().ok_or_else(invalid)?, body, headers: headers(meta.get("headers"))? })
    }
    #[cfg(not(unix))] { let _ = (method,url,body,hs,seconds); Err(invalid()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn policy() -> Policy { Policy { allow: vec!["*.example.com".into(),"example.com".into()], deny: vec!["blocked.example.com".into()] } }
    fn request_json(url: &str) -> Json {
        Json::Obj(vec![("url".into(),Json::Str(url.into())),("method".into(),Json::Str("GET".into())),
            ("headers".into(),Json::Arr(vec![])),("timeout_ms".into(),Json::Int(1000)),("body".into(),Json::Bool(false))])
    }
    #[test]
    fn blocks_special_ipv4_and_ipv6_ranges() {
        for address in ["0.0.0.0","10.0.0.1","100.64.0.1","127.0.0.1","169.254.169.254","172.16.0.1",
            "192.0.0.9","192.0.2.1","192.88.99.1","192.168.1.1","198.18.0.1","198.51.100.1","203.0.113.1",
            "224.0.0.1","240.0.0.1","255.255.255.255","::","::1","::ffff:8.8.8.8","64:ff9b::808:808",
            "100::1","2001::1","2001:20::1","2001:db8::1","2002:808:808::1","3fff::1","fc00::1","fe80::1","ff02::1"] {
            assert!(!public_address(address.parse().unwrap()),"{address}");
        }
        for address in ["8.8.8.8","1.1.1.1","93.184.216.34","2606:4700:4700::1111","2001:4860:4860::8888"] {
            assert!(public_address(address.parse().unwrap()),"{address}");
        }
    }
    #[test]
    fn mixed_or_empty_dns_answers_fail_closed() {
        let public: SocketAddr = "8.8.8.8:443".parse().unwrap();
        assert_eq!(pinned(vec![public]).unwrap(),vec![public]);
        assert!(pinned(vec![]).is_err());
        assert!(pinned(vec![public,"127.0.0.1:443".parse().unwrap()]).is_err());
        assert!(pinned(vec![public,"[::ffff:127.0.0.1]:443".parse().unwrap()]).is_err());
        assert!(pinned(vec![public;65]).is_err());
    }
    #[test]
    fn canonical_urls_and_deny_rules_are_checked_before_dns() {
        let p = policy();
        for url in ["https://blocked.example.com/","https://evil.test/","http://example.com:8080/",
            "ftp://example.com/","https://user@example.com/","https://example.com/#secret",
            "http://127.0.0.1\\@example.com/","https://example.com/\n","http://2130706433/",
            "http://0x7f000001/","http://0177.0.0.1/","http://[::ffff:127.0.0.1]/"] {
            assert!(target(url,&p).is_err(),"{url}");
        }
        assert_eq!(target("https://API.EXAMPLE.COM/path",&p).unwrap().host_str(),Some("api.example.com"));
        assert!(target("https://example.com/",&Policy::default()).is_err());
    }
    #[test]
    fn local_destinations_and_header_tunnelling_never_connect() {
        let p = Policy { allow: vec!["127.0.0.1".into(),"example.com".into()], deny: vec![] };
        assert!(exchange(&request_json("http://127.0.0.1/"),&[],&p).is_err());
        for header in ["Host","Proxy-Authorization","Connection","Transfer-Encoding","Content-Length","Upgrade"] {
            let mut r = request_json("https://example.com/");
            r.set("headers", header_json(&[(header.into(),"evil.test".into())]));
            assert!(exchange(&r,&[],&p).is_err());
        }
    }
    #[test]
    fn frames_preserve_boundaries_and_reject_oversize_or_truncation() {
        let mut bytes = Vec::new();
        write_frame(&mut bytes,b"first",8).unwrap(); write_frame(&mut bytes,b"second",8).unwrap();
        let mut input = bytes.as_slice();
        assert_eq!(read_frame(&mut input,8).unwrap(),b"first");
        assert_eq!(read_frame(&mut input,8).unwrap(),b"second");
        assert!(read_frame(&mut [0,0,0,9].as_slice(),8).is_err());
        assert!(read_frame(&mut [0,0,0,2,1].as_slice(),8).is_err());
        assert!(parse("[".repeat(10000).as_bytes()).is_err());
        assert!(parse(br#"{"headers":[["x","[escaped]\\\""]]}"#).is_ok());
    }
    #[test]
    fn pinned_transport_preserves_host_and_does_not_follow_redirects() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut conn,_) = listener.accept().unwrap();
            conn.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut bytes = Vec::new(); let mut b = [0];
            while !bytes.ends_with(b"\r\n\r\n") { conn.read_exact(&mut b).unwrap(); bytes.push(b[0]); }
            assert!(String::from_utf8(bytes).unwrap().to_lowercase().contains("host: example.com\r\n"));
            conn.write_all(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/secret\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK").unwrap();
        });
        let url = url::Url::parse("http://example.com/data").unwrap();
        let (meta,body) = transport(&request_json(url.as_str()),&[],&url,vec![address]).unwrap();
        assert_eq!(meta.get("status").int(),Some(302)); assert_eq!(body,b"OK");
        server.join().unwrap();
        // A redirect is a new authorization/resolve cycle, never an unchecked
        // automatic transport hop. Local targets remain forbidden.
        assert!(exchange(&request_json("http://127.0.0.1/secret"),&[],&policy()).is_err());
    }
    #[test]
    fn pinned_tls_transport_rejects_untrusted_certificates() {
        // Public test fixture only: this private key protects no deployed asset.
        let certs = rustls_pemfile::certs(&mut include_bytes!("../../testdata/online-test-cert.txt").as_slice())
            .collect::<Result<Vec<_>,_>>().unwrap();
        let key = rustls_pemfile::private_key(&mut include_bytes!("../../testdata/online-test-key.txt").as_slice()).unwrap().unwrap();
        let provider = std::sync::Arc::new(rustls::crypto::ring::default_provider());
        let config = rustls::ServerConfig::builder_with_provider(provider).with_safe_default_protocol_versions().unwrap()
            .with_no_client_auth().with_single_cert(certs,key).unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (conn,_) = listener.accept().unwrap();
            conn.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            conn.set_write_timeout(Some(Duration::from_secs(2))).unwrap();
            let tls = rustls::ServerConnection::new(std::sync::Arc::new(config)).unwrap();
            let mut tls = rustls::StreamOwned::new(tls,conn);
            assert!(tls.read(&mut [0;1]).is_err());
            assert_eq!(tls.conn.server_name(),Some("example.com"));
        });
        let url = url::Url::parse("https://example.com/").unwrap();
        assert!(transport(&request_json(url.as_str()),&[],&url,vec![address]).is_err());
        server.join().unwrap();
    }
}

#[cfg(test)]
mod adversarial_ipc_tests {
    use super::*;
    #[test]
    fn malformed_and_duplicate_control_frames_fail_closed() {
        for bytes in [br#"{"url":"a","url":"b"}"#.as_slice(), br#"{"n":01}"#, br#"{"n":1e9999}"#, br#"{"s":"\ud800\u0000"}"#] {
            assert!(parse(bytes).is_err());
        }
        assert!(parse(b"[[[[[[[[[0]]]]]]]]]").is_err());
        assert!(parse(br#"{"ok":"[\\\"{}]"}"#).is_ok());
    }
    #[test]
    fn seeded_truncated_and_oversized_frames_are_bounded() {
        let mut seed = 0x4e45584fu64;
        for _ in 0..10000 {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            let length = (seed >> 32) as u32;
            let mut wire = length.to_be_bytes().to_vec();
            wire.extend_from_slice(b"payload");
            let result = read_frame(&mut std::io::Cursor::new(wire), 128);
            if length <= 7 { assert_eq!(result.unwrap().len(), length as usize); }
            else { assert!(result.is_err()); }
        }
        let mut wire = Vec::new();
        write_frame(&mut wire, b"first", 128).unwrap();
        write_frame(&mut wire, b"second", 128).unwrap();
        let mut reader = std::io::Cursor::new(wire);
        assert_eq!(read_frame(&mut reader, 128).unwrap(), b"first");
        assert_eq!(read_frame(&mut reader, 128).unwrap(), b"second");
        assert!(read_frame(&mut reader, 128).is_err());
    }
}
