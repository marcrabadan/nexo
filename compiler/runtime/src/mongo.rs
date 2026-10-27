//! A MongoDB client for nexo.db (D171), as phase 0's mongo.py: BSON and
//! OP_MSG over a socket, TLS for any host but this machine, SCRAM-SHA-256,
//! the primary of a replica set, and the commands of a document store
//! (docdb.rs). The key column of a table is the document's `_id`, and a
//! none field is left out of the document.

use std::sync::Mutex;

use crate::db::Sql;
use crate::docdb::{Cond, DocError, Row};
use crate::json::Json;
use crate::kafka::{local_host, open, Stream};

const OP_MSG: i32 = 2013;

/// A BSON value.
#[derive(Clone, Debug)]
pub enum Bson {
    Double(f64),
    Str(String),
    Doc(Vec<(String, Bson)>),
    Arr(Vec<Bson>),
    Bin(Vec<u8>),
    Bool(bool),
    Null,
    Int32(i32),
    Int64(i64),
    /// an ObjectId or a decimal, as hex text
    Other(String),
}

impl Bson {
    fn get(&self, key: &str) -> Option<&Bson> {
        match self {
            Bson::Doc(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    fn int(&self) -> Option<i64> {
        match self {
            Bson::Int32(i) => Some(*i as i64),
            Bson::Int64(i) => Some(*i),
            Bson::Double(f) => Some(*f as i64),
            _ => None,
        }
    }
    fn text(&self) -> String {
        match self {
            Bson::Str(s) => s.clone(),
            Bson::Bin(b) => String::from_utf8_lossy(b).into_owned(),
            other => other.json().compact(),
        }
    }
    fn truth(&self) -> bool {
        match self {
            Bson::Bool(b) => *b,
            other => other.int().is_some_and(|i| i != 0),
        }
    }

    /// As phase 0 logs it (json.dumps of the command).
    pub fn json(&self) -> Json {
        match self {
            Bson::Double(f) => Json::Num(*f),
            Bson::Str(s) | Bson::Other(s) => Json::Str(s.clone()),
            Bson::Doc(pairs) => Json::Obj(pairs.iter().map(|(k, v)| (k.clone(), v.json())).collect()),
            Bson::Arr(items) => Json::Arr(items.iter().map(|v| v.json()).collect()),
            Bson::Bin(b) => Json::Str(String::from_utf8_lossy(b).into_owned()),
            Bson::Bool(b) => Json::Bool(*b),
            Bson::Null => Json::Null,
            Bson::Int32(i) => Json::Int(*i as i64),
            Bson::Int64(i) => Json::Int(*i),
        }
    }

    fn of(v: &Sql) -> Bson {
        match v {
            Sql::Null => Bson::Null,
            Sql::Int(i) => Bson::Int64(*i),
            Sql::Real(f) => Bson::Double(*f),
            Sql::Text(t) => Bson::Str(t.clone()),
        }
    }

    fn sql(&self) -> Sql {
        match self {
            Bson::Null => Sql::Null,
            Bson::Int32(i) => Sql::Int(*i as i64),
            Bson::Int64(i) => Sql::Int(*i),
            Bson::Double(f) => Sql::Real(*f),
            Bson::Bool(b) => Sql::Int(*b as i64),
            other => Sql::Text(other.text()),
        }
    }
}

fn doc(pairs: Vec<(&str, Bson)>) -> Bson {
    Bson::Doc(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

fn s(text: &str) -> Bson {
    Bson::Str(text.to_string())
}

fn encode(pairs: &[(String, Bson)], out: &mut Vec<u8>) {
    let start = out.len();
    out.extend_from_slice(&[0, 0, 0, 0]);
    for (k, v) in pairs {
        let kind: u8 = match v {
            Bson::Double(_) => 0x01,
            Bson::Str(_) | Bson::Other(_) => 0x02,
            Bson::Doc(_) => 0x03,
            Bson::Arr(_) => 0x04,
            Bson::Bin(_) => 0x05,
            Bson::Bool(_) => 0x08,
            Bson::Null => 0x0A,
            Bson::Int32(_) => 0x10,
            Bson::Int64(_) => 0x12,
        };
        out.push(kind);
        out.extend_from_slice(k.as_bytes());
        out.push(0);
        match v {
            Bson::Double(f) => out.extend_from_slice(&f.to_le_bytes()),
            Bson::Str(t) | Bson::Other(t) => {
                out.extend_from_slice(&((t.len() + 1) as i32).to_le_bytes());
                out.extend_from_slice(t.as_bytes());
                out.push(0);
            }
            Bson::Doc(p) => encode(p, out),
            Bson::Arr(items) => {
                let p: Vec<(String, Bson)> = items.iter().enumerate().map(|(i, x)| (i.to_string(), x.clone())).collect();
                encode(&p, out)
            }
            Bson::Bin(b) => {
                out.extend_from_slice(&(b.len() as i32).to_le_bytes());
                out.push(0);
                out.extend_from_slice(b);
            }
            Bson::Bool(b) => out.push(*b as u8),
            Bson::Null => {}
            Bson::Int32(i) => out.extend_from_slice(&i.to_le_bytes()),
            Bson::Int64(i) => out.extend_from_slice(&i.to_le_bytes()),
        }
    }
    out.push(0);
    let size = (out.len() - start) as i32;
    out[start..start + 4].copy_from_slice(&size.to_le_bytes());
}

fn i32_at(d: &[u8], p: usize) -> Result<i32, DocError> {
    d.get(p..p + 4).map(|b| i32::from_le_bytes([b[0], b[1], b[2], b[3]])).ok_or_else(|| DocError::new("the server's answer is not valid"))
}

/// A document and the offset after it.
fn decode(d: &[u8], at: usize) -> Result<(Vec<(String, Bson)>, usize), DocError> {
    let bad = || DocError::new("the server's answer is not valid");
    let size = i32_at(d, at)? as usize;
    let end = at + size - 1;
    let mut p = at + 4;
    let mut out = Vec::new();
    while p < end {
        let kind = d[p];
        p += 1;
        let z = p + d[p..].iter().position(|b| *b == 0).ok_or_else(bad)?;
        let name = String::from_utf8_lossy(&d[p..z]).into_owned();
        p = z + 1;
        let eight = |p: usize| -> Result<[u8; 8], DocError> { d.get(p..p + 8).map(|b| b.try_into().unwrap()).ok_or_else(bad) };
        let v = match kind {
            0x01 => {
                let v = Bson::Double(f64::from_le_bytes(eight(p)?));
                p += 8;
                v
            }
            0x02 => {
                let n = i32_at(d, p)? as usize;
                let v = Bson::Str(String::from_utf8_lossy(d.get(p + 4..p + 3 + n).ok_or_else(bad)?).into_owned());
                p += 4 + n;
                v
            }
            0x03 | 0x04 => {
                let (pairs, next) = decode(d, p)?;
                p = next;
                if kind == 0x04 {
                    Bson::Arr(pairs.into_iter().map(|(_, v)| v).collect())
                } else {
                    Bson::Doc(pairs)
                }
            }
            0x05 => {
                let n = i32_at(d, p)? as usize;
                let v = Bson::Bin(d.get(p + 5..p + 5 + n).ok_or_else(bad)?.to_vec());
                p += 5 + n;
                v
            }
            0x07 => {
                let v = Bson::Other(d.get(p..p + 12).ok_or_else(bad)?.iter().map(|b| format!("{b:02x}")).collect());
                p += 12;
                v
            }
            0x08 => {
                let v = Bson::Bool(d[p] != 0);
                p += 1;
                v
            }
            0x09 | 0x12 => {
                let v = Bson::Int64(i64::from_le_bytes(eight(p)?));
                p += 8;
                v
            }
            0x0A => Bson::Null,
            0x10 => {
                let v = Bson::Int32(i32_at(d, p)?);
                p += 4;
                v
            }
            0x11 => {
                let v = Bson::Int64(u64::from_le_bytes(eight(p)?) as i64);
                p += 8;
                v
            }
            0x13 => {
                let v = Bson::Other(d.get(p..p + 16).ok_or_else(bad)?.iter().map(|b| format!("{b:02x}")).collect());
                p += 16;
                v
            }
            other => return Err(DocError::new(format!("the server sent a BSON value of type {other}"))),
        };
        out.push((name, v));
    }
    Ok((out, end + 1))
}

// ------------------------------------------------------------ conditions

/// A text as a regular expression that matches it anywhere.
fn regex_text(text: &str) -> String {
    let mut out = String::new();
    for c in text.chars() {
        if c.is_ascii() && !c.is_ascii_alphanumeric() && c != '_' {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

fn field_of(name: &str, key: &str) -> String {
    if name == key { "_id".to_string() } else { name.to_string() }
}

/// A condition of docdb as a filter of `find`.
pub fn filter_of(cond: Option<&Cond>, key: &str) -> Bson {
    let Some(cond) = cond else { return Bson::Doc(Vec::new()) };
    let one = |f: String, v: Bson| Bson::Doc(vec![(f, v)]);
    match cond {
        Cond::And(items) | Cond::Or(items) => {
            let items: Vec<Bson> = items.iter().map(|c| filter_of(Some(c), key)).collect();
            if items.is_empty() {
                return Bson::Doc(Vec::new());
            }
            let and = matches!(cond, Cond::And(_));
            if and && items.len() == 1 {
                return items.into_iter().next().unwrap();
            }
            one(if and { "$and" } else { "$or" }.into(), Bson::Arr(items))
        }
        Cond::Cmp(f, op, v) => {
            let f = field_of(f, key);
            let v = Bson::of(v);
            match *op {
                "eq" => one(f, v),
                "ne" => one(f, doc(vec![("$nin", Bson::Arr(vec![v, Bson::Null]))])),
                op => {
                    let name = match op {
                        "lt" => "$lt",
                        "gt" => "$gt",
                        "le" => "$lte",
                        _ => "$gte",
                    };
                    one(f, doc(vec![(name, v)]))
                }
            }
        }
        Cond::Contains(f, text, positive) => {
            let f = field_of(f, key);
            let r = doc(vec![("$regex", s(&regex_text(text)))]);
            if *positive {
                one(f, r)
            } else {
                one(f, doc(vec![("$not", r), ("$ne", Bson::Null)]))
            }
        }
        Cond::Null(f, is_null) => {
            let f = field_of(f, key);
            if *is_null {
                one(f, Bson::Null)
            } else {
                one(f, doc(vec![("$ne", Bson::Null)]))
            }
        }
        Cond::In(f, vs) => one(field_of(f, key), doc(vec![("$in", Bson::Arr(vs.iter().map(Bson::of).collect()))])),
        Cond::Nin(f, vs) => one(field_of(f, key), doc(vec![("$nin", Bson::Arr(vs.iter().map(Bson::of).collect()))])),
    }
}

// ------------------------------------------------------------ connection

pub struct Target {
    pub user: String,
    pub hosts: Vec<(String, u16)>,
    pub database: String,
}

/// mongodb://user@host:port,host2:port/database (D171).
pub fn parse_url(url: &str) -> Result<Target, DocError> {
    let rest = url.split_once("://").map(|x| x.1).unwrap_or(url);
    let rest = rest.split('?').next().unwrap_or("");
    let (auth, place) = match rest.rfind('@') {
        Some(i) => (&rest[..i], &rest[i + 1..]),
        None => ("", rest),
    };
    if auth.contains(':') {
        return Err(DocError::new("the address holds a password; give it with key: instead"));
    }
    let (hosts_text, database) = place.split_once('/').unwrap_or((place, ""));
    if database.is_empty() {
        return Err(DocError::new("the address names no database: mongodb://user@host/database"));
    }
    let mut hosts = Vec::new();
    for part in hosts_text.split(',') {
        let bad = || DocError::new(format!("invalid port in {url}"));
        let (host, port) = if let Some(inner) = part.strip_prefix('[') {
            let (h, after) = inner.split_once(']').unwrap_or((inner, ""));
            (h.to_string(), match after.strip_prefix(':') {
                Some(p) => p.parse().map_err(|_| bad())?,
                None => 27017,
            })
        } else if let Some((h, p)) = part.rsplit_once(':') {
            (h.to_string(), p.parse().map_err(|_| bad())?)
        } else {
            (part.to_string(), 27017)
        };
        hosts.push((if host.is_empty() { "localhost".into() } else { host }, port));
    }
    Ok(Target { user: crate::docdb::unquote(auth), hosts, database: database.to_string() })
}

struct Connection {
    stream: Stream,
    buf: Vec<u8>,
    request: i32,
}

impl Connection {
    fn read(&mut self, n: usize) -> Result<Vec<u8>, DocError> {
        let mut chunk = [0u8; 65536];
        while self.buf.len() < n {
            let got = self.stream.read(&mut chunk).map_err(|e| DocError::new(format!("connection lost: {e}")))?;
            if got == 0 {
                return Err(DocError::new("the server closed the connection"));
            }
            self.buf.extend_from_slice(&chunk[..got]);
        }
        let rest = self.buf.split_off(n);
        Ok(std::mem::replace(&mut self.buf, rest))
    }

    /// One command and its answer; an error when it is not ok.
    fn command(&mut self, cmd: &Bson) -> Result<Bson, DocError> {
        self.request += 1;
        let Bson::Doc(pairs) = cmd else { unreachable!() };
        let mut body = vec![0, 0, 0, 0, 0];
        encode(pairs, &mut body);
        let mut out = ((16 + body.len()) as i32).to_le_bytes().to_vec();
        out.extend_from_slice(&self.request.to_le_bytes());
        out.extend_from_slice(&0i32.to_le_bytes());
        out.extend_from_slice(&OP_MSG.to_le_bytes());
        out.extend_from_slice(&body);
        self.stream.write_all(&out).map_err(|e| DocError::new(format!("connection lost: {e}")))?;
        let head = self.read(16)?;
        let size = i32_at(&head, 0)? as usize;
        let op = i32_at(&head, 12)?;
        let data = self.read(size - 16)?;
        if op != OP_MSG {
            return Err(DocError::new("the server answers in an unknown format"));
        }
        if data.get(4) != Some(&0) {
            return Err(DocError::new("the server's answer is not valid"));
        }
        let (answer, _) = decode(&data, 5)?;
        let answer = Bson::Doc(answer);
        if answer.get("ok").and_then(|v| v.int()) != Some(1) {
            let code = answer.get("code").and_then(|c| c.int()).filter(|c| *c != 0).unwrap_or(-1);
            let message = answer.get("errmsg").map(|m| m.text()).unwrap_or("error".into());
            return Err(DocError { message, code, duplicate: None });
        }
        Ok(answer)
    }

    fn login(&mut self, user: &str, password: &str) -> Result<(), DocError> {
        let mut scram = crate::pg::Scram::with_user(password, user);
        let first = self.command(&doc(vec![
            ("saslStart", Bson::Int64(1)),
            ("mechanism", s("SCRAM-SHA-256")),
            ("payload", Bson::Bin(scram.first().into_bytes())),
            ("autoAuthorize", Bson::Int64(1)),
            ("options", doc(vec![("skipEmptyExchange", Bson::Bool(true))])),
            ("$db", s("admin")),
        ]))?;
        let conversation = first.get("conversationId").cloned().unwrap_or(Bson::Int32(1));
        let payload = first.get("payload").map(|p| p.text()).unwrap_or_default();
        let fin = scram.finish(&payload).map_err(|_| DocError::new("the server's SCRAM answer is not valid"))?;
        let mut answer = self.command(&doc(vec![
            ("saslContinue", Bson::Int64(1)),
            ("conversationId", conversation.clone()),
            ("payload", Bson::Bin(fin.into_bytes())),
            ("$db", s("admin")),
        ]))?;
        let last = answer.get("payload").map(|p| p.text()).unwrap_or_default();
        scram.verify(&last).map_err(|_| DocError::new("the server's SCRAM signature is not valid"))?;
        while !answer.get("done").is_some_and(|d| d.truth()) {
            answer = self.command(&doc(vec![
                ("saslContinue", Bson::Int64(1)),
                ("conversationId", conversation.clone()),
                ("payload", Bson::Bin(Vec::new())),
                ("$db", s("admin")),
            ]))?;
        }
        Ok(())
    }
}

/// The operations of docdb on MongoDB: a table is a collection.
pub struct Store {
    conn: Mutex<Connection>,
    database: String,
    log: bool,
}

impl Store {
    pub fn open(url: &str, password: Option<&str>) -> Result<Store, DocError> {
        let t = parse_url(url)?;
        if password.is_some() && t.user.is_empty() {
            return Err(DocError::new("the address names no user for the password of key:"));
        }
        let mut last = None;
        for (host, port) in &t.hosts {
            // a server on another machine is reached with TLS only (D171)
            let stream = match open(host, *port, !local_host(host)) {
                Ok(s) => s,
                Err(e) => {
                    last = Some(DocError::new(e));
                    continue;
                }
            };
            let mut c = Connection { stream, buf: Vec::new(), request: 0 };
            let hello = match c.command(&doc(vec![("hello", Bson::Int64(1)), ("$db", s("admin"))])) {
                Ok(h) => h,
                Err(e) if e.code == 0 => {
                    last = Some(e);
                    continue;
                }
                Err(e) => return Err(e),
            };
            let primary = hello.get("isWritablePrimary").or_else(|| hello.get("ismaster")).is_some_and(|v| v.truth());
            if !primary {
                last = Some(DocError::new(format!("{host}:{port} is not the primary")));
                continue;
            }
            if hello.get("maxWireVersion").and_then(|v| v.int()).unwrap_or(0) < 6 {
                return Err(DocError::new("the server is too old (MongoDB 3.6 or later speaks OP_MSG)"));
            }
            if let Some(pw) = password {
                c.login(&t.user, pw)?;
            }
            return Ok(Store { conn: Mutex::new(c), database: t.database, log: std::env::var_os("NEXO_SQL_LOG").is_some() });
        }
        Err(last.unwrap_or_else(|| DocError::new("the address names no host")))
    }

    fn run(&self, mut cmd: Vec<(String, Bson)>) -> Result<Bson, DocError> {
        cmd.push(("$db".into(), s(&self.database)));
        let cmd = Bson::Doc(cmd);
        if self.log {
            crate::state::flush();
            eprintln!("mongo: {}", cmd.json().compact());
        }
        let answer = self.conn.lock().unwrap().command(&cmd)?;
        if let Some(Bson::Arr(errors)) = answer.get("writeErrors") {
            if let Some(e) = errors.first() {
                let code = e.get("code").and_then(|c| c.int()).unwrap_or(0);
                let message = e.get("errmsg").map(|m| m.text()).unwrap_or("write error".into());
                let dup = if code == 11000 {
                    message.split_once("index: ").and_then(|(_, r)| r.split_once("_unique dup key")).map(|(c, _)| c.to_string()).filter(|c| !c.contains(' '))
                } else {
                    None
                };
                return Err(DocError { message, code, duplicate: dup });
            }
        }
        Ok(answer)
    }

    /// A row as a document: its key as _id, none fields left out.
    fn document(key: &str, row: &Row) -> Bson {
        let mut out = vec![("_id".to_string(), Bson::of(&crate::docdb::get(row, key)))];
        for (k, v) in row {
            if k != key && !matches!(v, Sql::Null) {
                out.push((k.clone(), Bson::of(v)));
            }
        }
        Bson::Doc(out)
    }

    fn row(key: &str, d: &Bson) -> Row {
        let Bson::Doc(pairs) = d else { return Vec::new() };
        let mut out: Row = pairs.iter().filter(|(k, _)| k != "_id").map(|(k, v)| (k.clone(), v.sql())).collect();
        out.push((key.to_string(), d.get("_id").map(|v| v.sql()).unwrap_or(Sql::Null)));
        out
    }
}

fn pairs(v: Vec<(&str, Bson)>) -> Vec<(String, Bson)> {
    v.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}

impl crate::docdb::DocStore for Store {
    fn name(&self) -> &'static str {
        "mongo"
    }

    fn ensure(&self, table: &str, _key: &str, _fields: &[(String, &'static str)], uniques: &[String]) -> Result<(), DocError> {
        if uniques.is_empty() {
            return Ok(());
        }
        let indexes = uniques
            .iter()
            .map(|c| {
                doc(vec![
                    ("key", Bson::Doc(vec![(c.clone(), Bson::Int64(1))])),
                    ("name", Bson::Str(format!("{c}_unique"))),
                    ("unique", Bson::Bool(true)),
                    ("partialFilterExpression", Bson::Doc(vec![(c.clone(), doc(vec![("$exists", Bson::Bool(true))]))])),
                ])
            })
            .collect();
        self.run(pairs(vec![("createIndexes", s(table)), ("indexes", Bson::Arr(indexes))]))?;
        Ok(())
    }

    fn next_id(&self, table: &str) -> Result<i64, DocError> {
        let answer = self.run(pairs(vec![
            ("findAndModify", s("nexo_counters")),
            ("query", doc(vec![("_id", s(table))])),
            ("update", doc(vec![("$inc", doc(vec![("n", Bson::Int64(1))]))])),
            ("upsert", Bson::Bool(true)),
            ("new", Bson::Bool(true)),
        ]))?;
        answer.get("value").and_then(|v| v.get("n")).and_then(|n| n.int()).ok_or_else(|| DocError::new("the server's answer is not valid"))
    }

    fn insert(&self, table: &str, key: &str, row: &Row) -> Result<(), DocError> {
        self.run(pairs(vec![("insert", s(table)), ("documents", Bson::Arr(vec![Self::document(key, row)]))]))?;
        Ok(())
    }

    fn replace(&self, table: &str, key: &str, row: &Row, upsert: bool) -> Result<(), DocError> {
        let mut update = pairs(vec![("q", doc(vec![("_id", Bson::of(&crate::docdb::get(row, key)))])), ("u", Self::document(key, row))]);
        if upsert {
            update.push(("upsert".into(), Bson::Bool(true)));
        }
        self.run(pairs(vec![("update", s(table)), ("updates", Bson::Arr(vec![Bson::Doc(update)]))]))?;
        Ok(())
    }

    fn delete(&self, table: &str, _key: &str, value: &Sql) -> Result<(), DocError> {
        let one = doc(vec![("q", doc(vec![("_id", Bson::of(value))])), ("limit", Bson::Int64(1))]);
        self.run(pairs(vec![("delete", s(table)), ("deletes", Bson::Arr(vec![one]))]))?;
        Ok(())
    }

    fn find(&self, table: &str, key: &str, cond: Option<&Cond>, sort: &[(String, bool)], limit: Option<i64>, fields: Option<&[String]>) -> Result<Vec<Row>, DocError> {
        if limit == Some(0) {
            return Ok(Vec::new());
        }
        let mut order: Vec<(String, Bson)> = Vec::new();
        for (name, desc) in sort {
            let f = field_of(name, key);
            if !order.iter().any(|(k, _)| *k == f) {
                order.push((f, Bson::Int64(if *desc { -1 } else { 1 })));
            }
        }
        if !order.iter().any(|(k, _)| k == "_id") {
            order.push(("_id".into(), Bson::Int64(1)));
        }
        let mut cmd = pairs(vec![("find", s(table)), ("filter", filter_of(cond, key)), ("sort", Bson::Doc(order))]);
        if let Some(fields) = fields {
            let mut proj: Vec<(String, Bson)> = Vec::new();
            for f in fields {
                let f = field_of(f, key);
                if !proj.iter().any(|(k, _)| *k == f) {
                    proj.push((f, Bson::Int64(1)));
                }
            }
            cmd.push(("projection".into(), Bson::Doc(proj)));
        }
        if let Some(n) = limit {
            cmd.push(("limit".into(), Bson::Int64(n)));
        }
        let answer = self.run(cmd)?;
        let mut cursor = answer.get("cursor").cloned().unwrap_or(Bson::Null);
        let mut docs: Vec<Bson> = match cursor.get("firstBatch") {
            Some(Bson::Arr(items)) => items.clone(),
            _ => Vec::new(),
        };
        while let Some(id) = cursor.get("id").and_then(|i| i.int()).filter(|i| *i != 0) {
            let more = self.run(pairs(vec![("getMore", Bson::Int64(id)), ("collection", s(table))]))?;
            cursor = more.get("cursor").cloned().unwrap_or(Bson::Null);
            if let Some(Bson::Arr(items)) = cursor.get("nextBatch") {
                docs.extend(items.iter().cloned());
            }
        }
        Ok(docs.iter().map(|d| Self::row(key, d)).collect())
    }

    fn count(&self, table: &str, key: &str, cond: Option<&Cond>) -> Result<i64, DocError> {
        let answer = self.run(pairs(vec![("count", s(table)), ("query", filter_of(cond, key))]))?;
        Ok(answer.get("n").and_then(|n| n.int()).unwrap_or(0))
    }
}
