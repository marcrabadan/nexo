//! A MySQL client for nexo.db (D170), as nexo/stdlib/mysql.py: the
//! client/server protocol over a socket, TLS, the caching_sha2_password
//! and mysql_native_password logins, and prepared statements with binary
//! parameters and rows. Rows come back typed as SQLite gives them (int,
//! float, text, null; booleans as 1 and 0).

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use sha2::{Digest, Sha256};

use crate::db::Sql;
use crate::pg::{local_host, os_text, tls_config, PgError};

const LONG_PASSWORD: u32 = 0x1;
const CONNECT_WITH_DB: u32 = 0x8;
const PROTOCOL_41: u32 = 0x200;
const SSL: u32 = 0x800;
const TRANSACTIONS: u32 = 0x2000;
const SECURE_CONNECTION: u32 = 0x8000;
const MULTI_RESULTS: u32 = 0x20000;
const PLUGIN_AUTH: u32 = 0x80000;
const LENENC_DATA: u32 = 0x200000;
const DEPRECATE_EOF: u32 = 0x1000000;
const WANTED: u32 =
    LONG_PASSWORD | CONNECT_WITH_DB | PROTOCOL_41 | TRANSACTIONS | SECURE_CONNECTION | MULTI_RESULTS | PLUGIN_AUTH | LENENC_DATA | DEPRECATE_EOF;
const UTF8MB4: u8 = 255;
const MAX_PACKET: usize = 0xFF_FFFF;
/// The session of every connection: "name" quotes a name, as in SQLite and
/// PostgreSQL; || joins texts; times are UTC (D170).
const SESSION: &str = "SET SESSION sql_mode = 'ANSI_QUOTES,PIPES_AS_CONCAT,STRICT_ALL_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY', time_zone = '+00:00'";
/// Statements sent as text: they take no parameters and give no rows.
const CONTROL: &[&str] = &["BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "SET "];

const T_TINY: u8 = 0x01;
const T_SHORT: u8 = 0x02;
const T_LONG: u8 = 0x03;
const T_FLOAT: u8 = 0x04;
const T_DOUBLE: u8 = 0x05;
const T_NULL: u8 = 0x06;
const T_TIMESTAMP: u8 = 0x07;
const T_LONGLONG: u8 = 0x08;
const T_INT24: u8 = 0x09;
const T_DATE: u8 = 0x0A;
const T_TIME: u8 = 0x0B;
const T_DATETIME: u8 = 0x0C;
const T_YEAR: u8 = 0x0D;
const T_VAR_STRING: u8 = 0xFD;
const UNSIGNED: u16 = 0x20;

/// An error of the server (its number as the code) or a failure to reach
/// it, as the other servers' errors are.
fn err(message: impl Into<String>) -> PgError {
    PgError { message: message.into(), ..Default::default() }
}

pub struct Target {
    pub user: String,
    pub host: String,
    pub port: u16,
    pub database: String,
}

/// mysql://user@host:port/database (D170).
pub fn parse_url(url: &str) -> Result<Target, PgError> {
    let rest = url.split_once("://").map(|x| x.1).unwrap_or(url);
    let (auth, place) = match rest.rfind('@') {
        Some(i) => (&rest[..i], &rest[i + 1..]),
        None => ("", rest),
    };
    if auth.contains(':') {
        return Err(err("the address holds a password; give it with key: instead"));
    }
    let (hostport, database) = place.split_once('/').unwrap_or((place, ""));
    let bad = || err(format!("invalid port in {url}"));
    let (host, port) = if let Some(inner) = hostport.strip_prefix('[') {
        let (h, after) = inner.split_once(']').unwrap_or((inner, ""));
        let port = match after.strip_prefix(':') {
            Some(p) => p.parse().map_err(|_| bad())?,
            None => 3306,
        };
        (h.to_string(), port)
    } else if let Some((h, p)) = hostport.rsplit_once(':') {
        (h.to_string(), p.parse().map_err(|_| bad())?)
    } else {
        (hostport.to_string(), 3306)
    };
    if database.is_empty() {
        return Err(err("the address names no database: mysql://user@host/database"));
    }
    let user = if auth.is_empty() { "root".to_string() } else { auth.to_string() };
    Ok(Target { user, host: if host.is_empty() { "localhost".into() } else { host }, port, database: database.to_string() })
}

fn lenenc_int(n: usize, out: &mut Vec<u8>) {
    if n < 251 {
        out.push(n as u8);
    } else if n < 1 << 16 {
        out.push(0xFC);
        out.extend_from_slice(&(n as u16).to_le_bytes());
    } else if n < 1 << 24 {
        out.push(0xFD);
        out.extend_from_slice(&(n as u32).to_le_bytes()[..3]);
    } else {
        out.push(0xFE);
        out.extend_from_slice(&(n as u64).to_le_bytes());
    }
}

struct Reader<'a> {
    d: &'a [u8],
    p: usize,
}

impl<'a> Reader<'a> {
    fn new(d: &'a [u8], p: usize) -> Reader<'a> {
        Reader { d, p }
    }
    fn take(&mut self, n: usize) -> &'a [u8] {
        let end = (self.p + n).min(self.d.len());
        let out = &self.d[self.p.min(end)..end];
        self.p = end;
        out
    }
    fn int(&mut self, n: usize) -> u64 {
        self.take(n).iter().rev().fold(0u64, |acc, b| (acc << 8) | *b as u64)
    }
    fn lenenc(&mut self) -> Option<u64> {
        match self.int(1) {
            0xFB => None,
            0xFC => Some(self.int(2)),
            0xFD => Some(self.int(3)),
            0xFE => Some(self.int(8)),
            n => Some(n),
        }
    }
    fn lenenc_bytes(&mut self) -> Option<&'a [u8]> {
        let n = self.lenenc()?;
        Some(self.take(n as usize))
    }
    fn nul(&mut self) -> &'a [u8] {
        let end = self.d[self.p..].iter().position(|b| *b == 0).map(|i| self.p + i).unwrap_or(self.d.len());
        let out = &self.d[self.p..end];
        self.p = (end + 1).min(self.d.len());
        out
    }
    fn rest(&self) -> &'a [u8] {
        &self.d[self.p.min(self.d.len())..]
    }
}

/// A DECIMAL as SQLite gives a number: an int when it is whole.
fn number(text: String) -> Sql {
    match text.parse::<i64>() {
        Ok(i) => Sql::Int(i),
        // a whole decimal too large for an int: kept as text
        Err(_) if !text.contains('.') => Sql::Text(text),
        Err(_) => text.parse().map(Sql::Real).unwrap_or(Sql::Text(text)),
    }
}

fn binary_value(r: &mut Reader, kind: u8, flags: u16) -> Sql {
    let unsigned = flags & UNSIGNED != 0;
    match kind {
        T_TINY => {
            let v = r.int(1);
            Sql::Int(if unsigned { v as i64 } else { v as u8 as i8 as i64 })
        }
        T_SHORT | T_YEAR => {
            let v = r.int(2);
            Sql::Int(if unsigned { v as i64 } else { v as u16 as i16 as i64 })
        }
        T_LONG | T_INT24 => {
            let v = r.int(4);
            Sql::Int(if unsigned { v as i64 } else { v as u32 as i32 as i64 })
        }
        T_LONGLONG => {
            let v = r.int(8);
            if unsigned && v > i64::MAX as u64 {
                Sql::Text(v.to_string())
            } else {
                Sql::Int(v as i64)
            }
        }
        T_FLOAT => Sql::Real(f32::from_bits(r.int(4) as u32) as f64),
        T_DOUBLE => Sql::Real(f64::from_bits(r.int(8))),
        T_DATE | T_DATETIME | T_TIMESTAMP => {
            let n = r.int(1);
            let (mut y, mut mo, mut d, mut h, mut mi, mut s, mut us) = (0, 0, 0, 0, 0, 0, 0);
            if n >= 4 {
                y = r.int(2);
                mo = r.int(1);
                d = r.int(1);
            }
            if n >= 7 {
                h = r.int(1);
                mi = r.int(1);
                s = r.int(1);
            }
            if n >= 11 {
                us = r.int(4);
            }
            let mut text = format!("{y:04}-{mo:02}-{d:02}");
            if kind != T_DATE {
                text.push_str(&format!(" {h:02}:{mi:02}:{s:02}"));
                if us != 0 {
                    text.push_str(&format!(".{us:06}"));
                }
            }
            Sql::Text(text)
        }
        T_TIME => {
            let n = r.int(1);
            let (mut neg, mut days, mut h, mut mi, mut s, mut us) = (0, 0, 0, 0, 0, 0);
            if n >= 8 {
                neg = r.int(1);
                days = r.int(4);
                h = r.int(1);
                mi = r.int(1);
                s = r.int(1);
            }
            if n >= 12 {
                us = r.int(4);
            }
            let mut text = format!("{}{:02}:{mi:02}:{s:02}", if neg != 0 { "-" } else { "" }, days * 24 + h);
            if us != 0 {
                text.push_str(&format!(".{us:06}"));
            }
            Sql::Text(text)
        }
        _ => {
            let text = String::from_utf8_lossy(r.lenenc_bytes().unwrap_or(&[])).into_owned();
            if kind == 0x00 || kind == 0xF6 {
                number(text)
            } else {
                Sql::Text(text)
            }
        }
    }
}

fn xor(a: &[u8], b: &[u8]) -> Vec<u8> {
    a.iter().zip(b).map(|(x, y)| x ^ y).collect()
}

fn scramble(plugin: &str, password: Option<&str>, nonce: &[u8]) -> Result<Vec<u8>, PgError> {
    let Some(pw) = password.filter(|p| !p.is_empty()) else { return Ok(Vec::new()) };
    match plugin {
        "mysql_native_password" => {
            let one = crate::websocket::sha1(pw.as_bytes());
            let mut more = nonce.to_vec();
            more.extend_from_slice(&crate::websocket::sha1(&one));
            Ok(xor(&one, &crate::websocket::sha1(&more)))
        }
        "caching_sha2_password" => {
            let one = Sha256::digest(pw.as_bytes());
            let mut more = Sha256::digest(Sha256::digest(one)).to_vec();
            more.extend_from_slice(nonce);
            Ok(xor(&one, &Sha256::digest(&more)))
        }
        other => Err(err(format!("the server asks for an unsupported login ({other})"))),
    }
}

enum Stream {
    Plain(TcpStream),
    Tls(Box<rustls::StreamOwned<rustls::ClientConnection, TcpStream>>),
    Gone,
}

impl Stream {
    fn write_all(&mut self, data: &[u8]) -> std::io::Result<()> {
        match self {
            Stream::Plain(s) => s.write_all(data),
            Stream::Tls(s) => s.write_all(data).and_then(|_| s.flush()),
            Stream::Gone => Err(std::io::Error::other("closed")),
        }
    }
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        match self {
            Stream::Plain(s) => s.read(buf),
            Stream::Tls(s) => s.read(buf),
            Stream::Gone => Ok(0),
        }
    }
}

pub struct Answer {
    pub names: Vec<String>,
    pub rows: Vec<Vec<Sql>>,
    pub last_id: i64,
}

pub struct Connection {
    stream: Stream,
    buf: Vec<u8>,
    seq: u8,
    caps: u32,
    tls: bool,
}

impl Connection {
    pub fn open(url: &str, password: Option<&str>) -> Result<Connection, PgError> {
        let t = parse_url(url)?;
        let addrs: Vec<std::net::SocketAddr> = std::net::ToSocketAddrs::to_socket_addrs(&(t.host.as_str(), t.port))
            .map_err(|e| err(format!("cannot reach {}:{}: {}", t.host, t.port, os_text(&e))))?
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
        let Some(sock) = sock else {
            return Err(err(format!("cannot reach {}:{}: {last}", t.host, t.port)));
        };
        let mut c = Connection { stream: Stream::Plain(sock), buf: Vec::new(), seq: 0, caps: 0, tls: false };
        c.login(&t, password)?;
        c.query(SESSION)?;
        Ok(c)
    }

    fn read(&mut self, n: usize) -> Result<Vec<u8>, PgError> {
        let mut chunk = [0u8; 65536];
        while self.buf.len() < n {
            let got = self.stream.read(&mut chunk).map_err(|e| err(format!("connection lost: {e}")))?;
            if got == 0 {
                return Err(err("the server closed the connection"));
            }
            self.buf.extend_from_slice(&chunk[..got]);
        }
        let rest = self.buf.split_off(n);
        Ok(std::mem::replace(&mut self.buf, rest))
    }

    fn packet(&mut self) -> Result<Vec<u8>, PgError> {
        let mut out = Vec::new();
        loop {
            let head = self.read(4)?;
            let size = head[0] as usize | (head[1] as usize) << 8 | (head[2] as usize) << 16;
            self.seq = head[3].wrapping_add(1);
            out.extend(self.read(size)?);
            if size < MAX_PACKET {
                return Ok(out);
            }
        }
    }

    fn send(&mut self, mut payload: &[u8]) -> Result<(), PgError> {
        let mut out = Vec::with_capacity(payload.len() + 4);
        loop {
            let n = payload.len().min(MAX_PACKET);
            out.extend_from_slice(&(n as u32).to_le_bytes()[..3]);
            out.push(self.seq);
            out.extend_from_slice(&payload[..n]);
            self.seq = self.seq.wrapping_add(1);
            payload = &payload[n..];
            if n < MAX_PACKET {
                break;
            }
        }
        self.stream.write_all(&out).map_err(|e| err(format!("connection lost: {e}")))
    }

    fn command(&mut self, payload: &[u8]) -> Result<(), PgError> {
        self.seq = 0;
        self.send(payload)
    }

    fn error(p: &[u8]) -> PgError {
        let mut r = Reader::new(p, 1);
        let code = r.int(2);
        if r.rest().first() == Some(&b'#') {
            r.take(6);
        }
        PgError { message: String::from_utf8_lossy(r.rest()).into_owned(), code: code.to_string(), detail: String::new() }
    }

    /// The last insert id of an OK packet.
    fn ok(p: &[u8]) -> i64 {
        let mut r = Reader::new(p, 1);
        r.lenenc();
        r.lenenc().unwrap_or(0) as i64
    }

    fn login(&mut self, t: &Target, password: Option<&str>) -> Result<(), PgError> {
        let p = self.packet()?;
        if p.first() == Some(&0xFF) {
            return Err(Self::error(&p));
        }
        let mut r = Reader::new(&p, 1);
        r.nul();
        r.take(4);
        let mut nonce = r.take(8).to_vec();
        r.take(1);
        let mut caps = r.int(2) as u32;
        r.take(3);
        caps |= (r.int(2) as u32) << 16;
        let size = r.int(1) as usize;
        r.take(10);
        if caps & SECURE_CONNECTION != 0 {
            let part = r.take(13usize.max(size.saturating_sub(8)));
            nonce.extend_from_slice(&part[..part.len().min(12usize.max(size.saturating_sub(9)))]);
        }
        let mut plugin =
            if caps & PLUGIN_AUTH != 0 { String::from_utf8_lossy(r.nul()).into_owned() } else { "mysql_native_password".to_string() };
        if caps & PROTOCOL_41 == 0 {
            return Err(err("the server is too old (it does not speak protocol 4.1)"));
        }
        let mut mine = WANTED & caps;
        // TLS is asked for always; a server on this machine may do without
        // it (D170)
        if caps & SSL != 0 {
            mine |= SSL;
            let mut body = mine.to_le_bytes().to_vec();
            body.extend_from_slice(&(MAX_PACKET as u32).to_le_bytes());
            body.push(UTF8MB4);
            body.extend_from_slice(&[0u8; 23]);
            self.send(&body)?;
            let Stream::Plain(sock) = std::mem::replace(&mut self.stream, Stream::Gone) else { unreachable!() };
            let name = rustls::pki_types::ServerName::try_from(t.host.clone()).map_err(|_| err(format!("TLS with {}: invalid name", t.host)))?;
            let conn = rustls::ClientConnection::new(tls_config(), name).map_err(|_| err(format!("TLS with {}: the TLS handshake failed", t.host)))?;
            let mut s = rustls::StreamOwned::new(conn, sock);
            while s.conn.is_handshaking() {
                s.conn.complete_io(&mut s.sock).map_err(|e| crate::pg::tls_text(&t.host, &e))?;
            }
            self.stream = Stream::Tls(Box::new(s));
            self.tls = true;
        } else if !local_host(&t.host) {
            return Err(err(format!("{} does not offer TLS", t.host)));
        }
        self.caps = mine;
        let auth = scramble(&plugin, password, &nonce)?;
        let mut body = mine.to_le_bytes().to_vec();
        body.extend_from_slice(&(MAX_PACKET as u32).to_le_bytes());
        body.push(UTF8MB4);
        body.extend_from_slice(&[0u8; 23]);
        body.extend_from_slice(t.user.as_bytes());
        body.push(0);
        if mine & LENENC_DATA != 0 {
            lenenc_int(auth.len(), &mut body);
        } else {
            body.push(auth.len() as u8);
        }
        body.extend_from_slice(&auth);
        body.extend_from_slice(t.database.as_bytes());
        body.push(0);
        body.extend_from_slice(plugin.as_bytes());
        body.push(0);
        self.send(&body)?;
        loop {
            let p = self.packet()?;
            match p.first() {
                Some(0x00) => return Ok(()),
                Some(0xFF) => return Err(Self::error(&p)),
                Some(0xFE) => {
                    // another login: its plugin and nonce
                    let mut r = Reader::new(&p, 1);
                    plugin = String::from_utf8_lossy(r.nul()).into_owned();
                    let mut n = r.rest().to_vec();
                    while n.last() == Some(&0) {
                        n.pop();
                    }
                    let answer = scramble(&plugin, password, &n)?;
                    self.send(&answer)?;
                }
                Some(0x01) if plugin == "caching_sha2_password" => match p.get(1) {
                    // the fast login worked: an OK packet follows
                    Some(0x03) => continue,
                    Some(0x04) => {
                        // the full login sends the password itself: over TLS only
                        if !self.tls {
                            return Err(err("the server asks for the password over TLS, which it does not offer"));
                        }
                        let mut pw = password.unwrap_or("").as_bytes().to_vec();
                        pw.push(0);
                        self.send(&pw)?;
                    }
                    _ => return Err(err("the server's login answer is not valid")),
                },
                _ => return Err(err("the server's login answer is not valid")),
            }
        }
    }

    /// (name, type, flags) of each column of a result.
    fn columns(&mut self, count: u64) -> Result<Vec<(String, u8, u16)>, PgError> {
        let mut out = Vec::new();
        for _ in 0..count {
            let p = self.packet()?;
            let mut r = Reader::new(&p, 0);
            for _ in 0..4 {
                r.lenenc_bytes();
            }
            let name = String::from_utf8_lossy(r.lenenc_bytes().unwrap_or(&[])).into_owned();
            r.lenenc_bytes();
            r.lenenc();
            r.take(2);
            r.take(4);
            let kind = r.int(1) as u8;
            let flags = r.int(2) as u16;
            out.push((name, kind, flags));
        }
        Ok(out)
    }

    fn eof(&mut self) -> Result<(), PgError> {
        if self.caps & DEPRECATE_EOF == 0 {
            self.packet()?;
        }
        Ok(())
    }

    fn result(&mut self, binary: bool) -> Result<Answer, PgError> {
        let p = self.packet()?;
        match p.first() {
            Some(0xFF) => return Err(Self::error(&p)),
            Some(0x00) => return Ok(Answer { names: Vec::new(), rows: Vec::new(), last_id: Self::ok(&p) }),
            _ => {}
        }
        let count = Reader::new(&p, 0).lenenc().unwrap_or(0);
        let cols = self.columns(count)?;
        self.eof()?;
        let mut rows = Vec::new();
        loop {
            let p = self.packet()?;
            if p.first() == Some(&0xFE) && p.len() < 9 {
                break;
            }
            if p.first() == Some(&0xFF) {
                return Err(Self::error(&p));
            }
            let mut row = Vec::with_capacity(cols.len());
            if binary {
                let skip = (cols.len() + 9) / 8;
                let nulls = p[1..1 + skip].to_vec();
                let mut r = Reader::new(&p, 1 + skip);
                for (i, (_, kind, flags)) in cols.iter().enumerate() {
                    if nulls[(i + 2) / 8] & (1 << ((i + 2) % 8)) != 0 {
                        row.push(Sql::Null);
                    } else {
                        row.push(binary_value(&mut r, *kind, *flags));
                    }
                }
            } else {
                let mut r = Reader::new(&p, 0);
                for _ in &cols {
                    row.push(match r.lenenc_bytes() {
                        None => Sql::Null,
                        Some(b) => Sql::Text(String::from_utf8_lossy(b).into_owned()),
                    });
                }
            }
            rows.push(row);
        }
        Ok(Answer { names: cols.into_iter().map(|c| c.0).collect(), rows, last_id: 0 })
    }

    fn query(&mut self, sql: &str) -> Result<Answer, PgError> {
        let mut body = vec![0x03];
        body.extend_from_slice(sql.as_bytes());
        self.command(&body)?;
        self.result(false)
    }

    pub fn execute(&mut self, sql: &str, params: &[Sql]) -> Result<Answer, PgError> {
        if params.is_empty() && CONTROL.iter().any(|c| sql.trim_start().to_uppercase().starts_with(c)) {
            return self.query(sql);
        }
        let mut body = vec![0x16];
        body.extend_from_slice(sql.as_bytes());
        self.command(&body)?;
        let p = self.packet()?;
        if p.first() == Some(&0xFF) {
            return Err(Self::error(&p));
        }
        let mut r = Reader::new(&p, 1);
        let stmt = r.int(4) as u32;
        let ncols = r.int(2);
        let nparams = r.int(2);
        let out = (|| {
            if nparams > 0 {
                self.columns(nparams)?;
                self.eof()?;
            }
            if ncols > 0 {
                self.columns(ncols)?;
                self.eof()?;
            }
            if nparams as usize != params.len() {
                return Err(err(format!("the statement takes {nparams} values, not {}", params.len())));
            }
            let mut body = vec![0x17];
            body.extend_from_slice(&stmt.to_le_bytes());
            body.push(0);
            body.extend_from_slice(&1u32.to_le_bytes());
            if !params.is_empty() {
                let mut nulls = vec![0u8; params.len().div_ceil(8)];
                let (mut types, mut values) = (Vec::new(), Vec::new());
                for (i, v) in params.iter().enumerate() {
                    match v {
                        Sql::Null => {
                            nulls[i / 8] |= 1 << (i % 8);
                            types.extend_from_slice(&[T_NULL, 0]);
                        }
                        Sql::Int(n) => {
                            types.extend_from_slice(&[T_LONGLONG, 0]);
                            values.extend_from_slice(&n.to_le_bytes());
                        }
                        Sql::Real(f) => {
                            types.extend_from_slice(&[T_DOUBLE, 0]);
                            values.extend_from_slice(&f.to_le_bytes());
                        }
                        Sql::Text(s) => {
                            types.extend_from_slice(&[T_VAR_STRING, 0]);
                            lenenc_int(s.len(), &mut values);
                            values.extend_from_slice(s.as_bytes());
                        }
                    }
                }
                body.extend_from_slice(&nulls);
                body.push(1);
                body.extend_from_slice(&types);
                body.extend_from_slice(&values);
            }
            self.command(&body)?;
            self.result(true)
        })();
        let mut close = vec![0x19];
        close.extend_from_slice(&stmt.to_le_bytes());
        let _ = self.command(&close);
        out
    }
}

impl Drop for Connection {
    fn drop(&mut self) {
        let _ = self.command(&[0x01]);
    }
}
