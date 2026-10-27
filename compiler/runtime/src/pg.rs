//! A PostgreSQL client for nexo.db (D163): the version 3 protocol over a
//! socket, TLS, SCRAM-SHA-256 or cleartext passwords, and the extended
//! query protocol with text parameters. Rows come back typed as SQLite
//! gives them (int, float, text, null; booleans as 1 and 0), as in
//! nexo/stdlib/pg.py.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::Arc;
use std::time::Duration;

use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

use crate::db::Sql;

/// An error the server reported, or a failure to reach it.
#[derive(Debug, Clone, Default)]
pub struct PgError {
    pub message: String,
    pub code: String,
    pub detail: String,
}

impl PgError {
    fn new(message: impl Into<String>) -> PgError {
        PgError { message: message.into(), ..Default::default() }
    }
}

pub struct Target {
    pub user: String,
    pub host: String,
    pub port: u16,
    pub database: String,
}

/// An error of the system as Python's strerror writes it.
fn os_text(e: &std::io::Error) -> String {
    if e.kind() == std::io::ErrorKind::TimedOut || e.kind() == std::io::ErrorKind::WouldBlock {
        return "timed out".into();
    }
    let t = e.to_string();
    let t = t.strip_prefix("failed to lookup address information: ").unwrap_or(&t);
    match t.find(" (os error") {
        Some(i) => t[..i].to_string(),
        None => t.to_string(),
    }
}

/// A TLS failure, in the words of both phases.
fn tls_text(host: &str, e: &std::io::Error) -> PgError {
    let untrusted = e.get_ref().and_then(|x| x.downcast_ref::<rustls::Error>()).is_some_and(|x| matches!(x, rustls::Error::InvalidCertificate(_)));
    let what = if untrusted { "the certificate is not trusted" } else { "the TLS handshake failed" };
    PgError::new(format!("TLS with {host}: {what}"))
}

pub fn local_host(host: &str) -> bool {
    host == "localhost" || host == "::1" || host.starts_with("127.")
}

/// postgres://user@host:port/database (D163).
pub fn parse_url(url: &str) -> Result<Target, PgError> {
    let rest = url.split_once("://").map(|x| x.1).unwrap_or(url);
    let (auth, place) = match rest.rfind('@') {
        Some(i) => (&rest[..i], &rest[i + 1..]),
        None => ("", rest),
    };
    if auth.contains(':') {
        return Err(PgError::new("the address holds a password; give it with key: instead"));
    }
    let (hostport, database) = place.split_once('/').unwrap_or((place, ""));
    let bad = || PgError::new(format!("invalid port in {url}"));
    let (host, port) = if let Some(inner) = hostport.strip_prefix('[') {
        let (h, after) = inner.split_once(']').unwrap_or((inner, ""));
        let port = match after.strip_prefix(':') {
            Some(p) => p.parse().map_err(|_| bad())?,
            None => 5432,
        };
        (h.to_string(), port)
    } else if let Some((h, p)) = hostport.rsplit_once(':') {
        (h.to_string(), p.parse().map_err(|_| bad())?)
    } else {
        (hostport.to_string(), 5432)
    };
    let user = if auth.is_empty() { "postgres".to_string() } else { auth.to_string() };
    let database = if !database.is_empty() { database.to_string() } else { user.clone() };
    Ok(Target { user, host: if host.is_empty() { "localhost".into() } else { host }, port, database })
}

enum Stream {
    Plain(TcpStream),
    Tls(Box<rustls::StreamOwned<rustls::ClientConnection, TcpStream>>),
}

impl Stream {
    fn write_all(&mut self, data: &[u8]) -> std::io::Result<()> {
        match self {
            Stream::Plain(s) => s.write_all(data),
            Stream::Tls(s) => s.write_all(data).and_then(|_| s.flush()),
        }
    }
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        match self {
            Stream::Plain(s) => s.read(buf),
            Stream::Tls(s) => s.read(buf),
        }
    }
}

pub struct Connection {
    stream: Stream,
    buf: Vec<u8>,
}

pub struct Answer {
    pub names: Vec<String>,
    pub rows: Vec<Vec<Sql>>,
}

fn tls_config() -> Arc<rustls::ClientConfig> {
    let mut roots = rustls::RootCertStore::empty();
    for cert in rustls_native_certs::load_native_certs().into_iter().flatten() {
        let _ = roots.add(cert);
    }
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .expect("TLS versions")
        .with_root_certificates(roots)
        .with_no_client_auth();
    Arc::new(config)
}

fn convert(oid: i32, raw: Option<&[u8]>) -> Sql {
    let Some(raw) = raw else { return Sql::Null };
    let text = String::from_utf8_lossy(raw).into_owned();
    match oid {
        20 | 21 | 23 | 26 => text.parse().map(Sql::Int).unwrap_or(Sql::Text(text)),
        700 | 701 => text.parse().map(Sql::Real).unwrap_or(Sql::Text(text)),
        16 => Sql::Int((text == "t") as i64),
        1700 => match text.parse::<i64>() {
            Ok(i) => Sql::Int(i),
            // an integral numeric too large for an int: kept as text
            Err(_) if !text.contains('.') => Sql::Text(text),
            Err(_) => text.parse().map(Sql::Real).unwrap_or(Sql::Text(text)),
        },
        _ => Sql::Text(text),
    }
}

/// A parameter as text: floats as Python's repr writes them.
fn param(p: &Sql) -> Option<Vec<u8>> {
    match p {
        Sql::Null => None,
        Sql::Int(i) => Some(i.to_string().into_bytes()),
        Sql::Real(f) => Some(crate::fmt::format_float(*f).into_bytes()),
        Sql::Text(t) => Some(t.clone().into_bytes()),
    }
}

fn i32_at(b: &[u8], at: usize) -> i32 {
    i32::from_be_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}

fn i16_at(b: &[u8], at: usize) -> i16 {
    i16::from_be_bytes([b[at], b[at + 1]])
}

impl Connection {
    pub fn open(url: &str, password: Option<&str>) -> Result<Connection, PgError> {
        let t = parse_url(url)?;
        let addrs: Vec<std::net::SocketAddr> = std::net::ToSocketAddrs::to_socket_addrs(&(t.host.as_str(), t.port))
            .map_err(|e| PgError::new(format!("cannot reach {}:{}: {}", t.host, t.port, os_text(&e))))?
            .collect();
        let mut sock = None;
        let mut last = String::from("no address");
        for a in addrs {
            match TcpStream::connect_timeout(&a, Duration::from_secs(30)) {
                Ok(s) => {
                    sock = Some(s);
                    break;
                }
                Err(e) => last = os_text(&e),
            }
        }
        let Some(mut sock) = sock else {
            return Err(PgError::new(format!("cannot reach {}:{}: {last}", t.host, t.port)));
        };
        // TLS is asked for always; a server on this machine may answer
        // without it (D163)
        let lost = |e: std::io::Error| PgError::new(format!("connection lost: {e}"));
        sock.write_all(&[0, 0, 0, 8, 4, 210, 22, 47]).map_err(lost)?;
        let mut answer = [0u8; 1];
        sock.read_exact(&mut answer).map_err(lost)?;
        let stream = if answer[0] == b'S' {
            let name = rustls::pki_types::ServerName::try_from(t.host.clone())
                .map_err(|_| PgError::new(format!("TLS with {}: invalid name", t.host)))?;
            let conn = rustls::ClientConnection::new(tls_config(), name)
                .map_err(|_| PgError::new(format!("TLS with {}: the TLS handshake failed", t.host)))?;
            let mut s = rustls::StreamOwned::new(conn, sock);
            while s.conn.is_handshaking() {
                s.conn.complete_io(&mut s.sock).map_err(|e| tls_text(&t.host, &e))?;
            }
            Stream::Tls(Box::new(s))
        } else if local_host(&t.host) {
            Stream::Plain(sock)
        } else {
            return Err(PgError::new(format!("{} does not offer TLS", t.host)));
        };
        let mut c = Connection { stream, buf: Vec::new() };
        c.startup(&t, password)?;
        Ok(c)
    }

    fn send(&mut self, kind: u8, body: &[u8]) -> Result<(), PgError> {
        let mut out = vec![kind];
        out.extend_from_slice(&((body.len() + 4) as i32).to_be_bytes());
        out.extend_from_slice(body);
        self.stream.write_all(&out).map_err(|e| PgError::new(format!("connection lost: {e}")))
    }

    fn take(&mut self, n: usize) -> Result<Vec<u8>, PgError> {
        let mut chunk = [0u8; 65536];
        while self.buf.len() < n {
            let got = self.stream.read(&mut chunk).map_err(|e| PgError::new(format!("connection lost: {e}")))?;
            if got == 0 {
                return Err(PgError::new("the server closed the connection"));
            }
            self.buf.extend_from_slice(&chunk[..got]);
        }
        let rest = self.buf.split_off(n);
        Ok(std::mem::replace(&mut self.buf, rest))
    }

    fn message(&mut self) -> Result<(u8, Vec<u8>), PgError> {
        let head = self.take(5)?;
        let size = i32_at(&head, 1) as usize;
        Ok((head[0], self.take(size - 4)?))
    }

    fn error(body: &[u8]) -> PgError {
        let mut e = PgError::new("error");
        for part in body.split(|b| *b == 0).filter(|p| !p.is_empty()) {
            let text = String::from_utf8_lossy(&part[1..]).into_owned();
            match part[0] {
                b'M' => e.message = text,
                b'C' => e.code = text,
                b'D' => e.detail = text,
                _ => {}
            }
        }
        e
    }

    fn startup(&mut self, t: &Target, password: Option<&str>) -> Result<(), PgError> {
        let mut body = 196608i32.to_be_bytes().to_vec();
        for (k, v) in [
            ("user", t.user.as_str()),
            ("database", t.database.as_str()),
            ("client_encoding", "UTF8"),
            ("extra_float_digits", "3"),
        ] {
            body.extend_from_slice(k.as_bytes());
            body.push(0);
            body.extend_from_slice(v.as_bytes());
            body.push(0);
        }
        body.push(0);
        let mut out = ((body.len() + 4) as i32).to_be_bytes().to_vec();
        out.extend_from_slice(&body);
        self.stream.write_all(&out).map_err(|e| PgError::new(format!("connection lost: {e}")))?;
        let mut scram: Option<Scram> = None;
        loop {
            let (kind, body) = self.message()?;
            match kind {
                b'E' => return Err(Self::error(&body)),
                b'R' => {
                    let code = i32_at(&body, 0);
                    if code == 0 {
                        continue;
                    }
                    let Some(pw) = password else {
                        return Err(PgError::new("the server asks for a password; give it with key:"));
                    };
                    match code {
                        3 => {
                            let mut b = pw.as_bytes().to_vec();
                            b.push(0);
                            self.send(b'p', &b)?;
                        }
                        10 => {
                            if !body[4..].split(|b| *b == 0).any(|m| m == b"SCRAM-SHA-256") {
                                return Err(PgError::new("the server asks for an unsupported authentication"));
                            }
                            let s = Scram::new(pw);
                            let first = s.first();
                            let mut b = b"SCRAM-SHA-256\0".to_vec();
                            b.extend_from_slice(&(first.len() as i32).to_be_bytes());
                            b.extend_from_slice(first.as_bytes());
                            self.send(b'p', &b)?;
                            scram = Some(s);
                        }
                        11 => {
                            let s = scram.as_mut().ok_or_else(|| PgError::new("unexpected SCRAM message"))?;
                            let fin = s.finish(&String::from_utf8_lossy(&body[4..]))?;
                            self.send(b'p', fin.as_bytes())?;
                        }
                        12 => {
                            let s = scram.as_ref().ok_or_else(|| PgError::new("unexpected SCRAM message"))?;
                            s.verify(&String::from_utf8_lossy(&body[4..]))?;
                        }
                        _ => return Err(PgError::new("the server asks for an unsupported authentication")),
                    }
                }
                b'Z' => return Ok(()),
                _ => {}
            }
        }
    }

    /// One statement with its parameters, as text.
    pub fn execute(&mut self, sql: &str, params: &[Sql]) -> Result<Answer, PgError> {
        let mut out = Vec::new();
        let msg = |kind: u8, body: &[u8], out: &mut Vec<u8>| {
            out.push(kind);
            out.extend_from_slice(&((body.len() + 4) as i32).to_be_bytes());
            out.extend_from_slice(body);
        };
        let mut parse = vec![0u8];
        parse.extend_from_slice(sql.as_bytes());
        parse.extend_from_slice(&[0, 0, 0]);
        msg(b'P', &parse, &mut out);
        let mut bind = vec![0u8, 0, 0, 0];
        bind.extend_from_slice(&(params.len() as i16).to_be_bytes());
        for p in params {
            match param(p) {
                None => bind.extend_from_slice(&(-1i32).to_be_bytes()),
                Some(v) => {
                    bind.extend_from_slice(&(v.len() as i32).to_be_bytes());
                    bind.extend_from_slice(&v);
                }
            }
        }
        bind.extend_from_slice(&[0, 0]);
        msg(b'B', &bind, &mut out);
        msg(b'D', b"P\0", &mut out);
        msg(b'E', &[0, 0, 0, 0, 0], &mut out);
        msg(b'S', &[], &mut out);
        self.stream.write_all(&out).map_err(|e| PgError::new(format!("connection lost: {e}")))?;
        let (mut names, mut types, mut rows, mut failed) = (Vec::new(), Vec::new(), Vec::new(), None);
        loop {
            let (kind, body) = self.message()?;
            match kind {
                b'T' => {
                    let count = i16_at(&body, 0);
                    let mut at = 2;
                    for _ in 0..count {
                        let end = at + body[at..].iter().position(|b| *b == 0).unwrap_or(0);
                        names.push(String::from_utf8_lossy(&body[at..end]).into_owned());
                        types.push(i32_at(&body, end + 7));
                        at = end + 19;
                    }
                }
                b'D' => {
                    let count = i16_at(&body, 0) as usize;
                    let mut at = 2;
                    let mut row = Vec::with_capacity(count);
                    for i in 0..count {
                        let size = i32_at(&body, at);
                        at += 4;
                        let raw = if size < 0 { None } else { Some(&body[at..at + size as usize]) };
                        at += size.max(0) as usize;
                        row.push(convert(types.get(i).copied().unwrap_or(25), raw));
                    }
                    rows.push(row);
                }
                b'E' => failed = Some(Self::error(&body)),
                b'Z' => break,
                _ => {}
            }
        }
        match failed {
            Some(e) => Err(e),
            None => Ok(Answer { names, rows }),
        }
    }
}

impl Drop for Connection {
    fn drop(&mut self) {
        let _ = self.send(b'X', &[]);
    }
}

type HmacSha = Hmac<Sha256>;

fn hmac(key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut m = <HmacSha as Mac>::new_from_slice(key).expect("hmac key");
    m.update(data);
    m.finalize().into_bytes().to_vec()
}

/// SCRAM-SHA-256 (RFC 5802, RFC 7677) without channel binding.
struct Scram {
    password: Vec<u8>,
    nonce: String,
    bare: String,
    auth: Vec<u8>,
    server_key: Vec<u8>,
}

impl Scram {
    fn new(password: &str) -> Scram {
        let mut raw = [0u8; 18];
        let _ = getrandom::getrandom(&mut raw);
        Scram { password: password.as_bytes().to_vec(), nonce: crate::crypto::b64(&raw), bare: String::new(), auth: Vec::new(), server_key: Vec::new() }
    }

    fn first(&self) -> String {
        format!("n,,n=,r={}", self.nonce)
    }

    fn finish(&mut self, server_first: &str) -> Result<String, PgError> {
        self.bare = format!("n=,r={}", self.nonce);
        let bad = || PgError::new("the server's SCRAM answer is not valid");
        let mut nonce = "";
        let mut salt = Vec::new();
        let mut rounds = 0u32;
        for part in server_first.split(',') {
            match part.split_once('=') {
                Some(("r", v)) => nonce = v,
                Some(("s", v)) => salt = crate::crypto::unb64(v).ok_or_else(bad)?,
                Some(("i", v)) => rounds = v.parse().map_err(|_| bad())?,
                _ => {}
            }
        }
        if !nonce.starts_with(&self.nonce) || rounds == 0 {
            return Err(bad());
        }
        // PBKDF2-HMAC-SHA256 with one block of 32 bytes
        let mut s1 = salt.clone();
        s1.extend_from_slice(&1u32.to_be_bytes());
        let mut u = hmac(&self.password, &s1);
        let mut salted = u.clone();
        for _ in 1..rounds {
            u = hmac(&self.password, &u);
            for (a, b) in salted.iter_mut().zip(&u) {
                *a ^= b;
            }
        }
        let client_key = hmac(&salted, b"Client Key");
        let stored = Sha256::digest(&client_key);
        let without = format!("c=biws,r={nonce}");
        self.auth = format!("{},{server_first},{without}", self.bare).into_bytes();
        let signature = hmac(&stored, &self.auth);
        let proof: Vec<u8> = client_key.iter().zip(&signature).map(|(a, b)| a ^ b).collect();
        self.server_key = hmac(&salted, b"Server Key");
        Ok(format!("{without},p={}", crate::crypto::b64(&proof)))
    }

    fn verify(&self, server_final: &str) -> Result<(), PgError> {
        let expected = format!("v={}", crate::crypto::b64(&hmac(&self.server_key, &self.auth)));
        if server_final.split(',').next() != Some(expected.as_str()) {
            return Err(PgError::new("the server's SCRAM signature is not valid"));
        }
        Ok(())
    }
}
