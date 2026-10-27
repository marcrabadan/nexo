//! An Elasticsearch client for nexo.db (D172), as phase 0's elastic.py:
//! its REST API over HTTP(S), the query DSL, and the operations of a
//! document store (docdb.rs). A table is an index (`<prefix>-<table>` when
//! the address has a path), a row a document whose id is its key; a
//! `@unique` value is a document of `nexo_unique`, created only when it
//! does not exist; every write is refreshed at once.

use sha2::{Digest, Sha256};

use crate::db::Sql;
use crate::docdb::{get, Cond, DocError, Row};
use crate::json::{self, Json};
use crate::kafka::local_host;

const PAGE: i64 = 1000;

pub struct Target {
    pub user: String,
    pub hosts: Vec<(String, u16)>,
    pub prefix: String,
}

/// elasticsearch://user@host:port,host2:port/prefix (D172).
pub fn parse_url(url: &str) -> Result<Target, DocError> {
    let rest = url.split_once("://").map(|x| x.1).unwrap_or(url);
    let (auth, place) = match rest.rfind('@') {
        Some(i) => (&rest[..i], &rest[i + 1..]),
        None => ("", rest),
    };
    if auth.contains(':') {
        return Err(DocError::new("the address holds a password; give it with key: instead"));
    }
    let (hosts_text, prefix) = place.split_once('/').unwrap_or((place, ""));
    let mut hosts = Vec::new();
    for part in hosts_text.split(',') {
        let bad = || DocError::new(format!("invalid port in {url}"));
        let (host, port) = if let Some(inner) = part.strip_prefix('[') {
            let (h, after) = inner.split_once(']').unwrap_or((inner, ""));
            (h.to_string(), match after.strip_prefix(':') {
                Some(p) => p.parse().map_err(|_| bad())?,
                None => 9200,
            })
        } else if let Some((h, p)) = part.rsplit_once(':') {
            (h.to_string(), p.parse().map_err(|_| bad())?)
        } else {
            (part.to_string(), 9200)
        };
        hosts.push((if host.is_empty() { "localhost".into() } else { host }, port));
    }
    Ok(Target { user: crate::docdb::unquote(auth), hosts, prefix: prefix.trim_matches('/').to_lowercase() })
}

fn sql_json(v: &Sql) -> Json {
    match v {
        Sql::Null => Json::Null,
        Sql::Int(i) => Json::Int(*i),
        Sql::Real(f) => Json::Num(*f),
        Sql::Text(t) => Json::Str(t.clone()),
    }
}

fn json_sql(v: &Json) -> Sql {
    match v {
        Json::Null => Sql::Null,
        Json::Int(i) => Sql::Int(*i),
        Json::Num(f) => Sql::Real(*f),
        Json::Bool(b) => Sql::Int(*b as i64),
        Json::Str(s) => Sql::Text(s.clone()),
        other => Sql::Text(other.compact()),
    }
}

fn obj(pairs: Vec<(&str, Json)>) -> Json {
    json::obj(pairs)
}

fn one(key: &str, v: Json) -> Json {
    Json::Obj(vec![(key.to_string(), v)])
}

/// A text as a wildcard pattern that matches it anywhere.
fn wildcard_text(text: &str) -> String {
    let mut out = String::from("*");
    for c in text.chars() {
        if "\\*?".contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out.push('*');
    out
}

/// A condition of docdb in the query DSL.
pub fn query_of(cond: Option<&Cond>) -> Json {
    let Some(cond) = cond else { return one("match_all", Json::Obj(Vec::new())) };
    let exists = |f: &str| one("exists", one("field", json::s(f)));
    match cond {
        Cond::And(items) => one("bool", one("filter", Json::Arr(items.iter().map(|c| query_of(Some(c))).collect()))),
        Cond::Or(items) => one(
            "bool",
            obj(vec![("should", Json::Arr(items.iter().map(|c| query_of(Some(c))).collect())), ("minimum_should_match", Json::Int(1))]),
        ),
        Cond::Cmp(f, op, v) => {
            let v = sql_json(v);
            match *op {
                "eq" => one("term", one(f, v)),
                "ne" => one("bool", obj(vec![("filter", Json::Arr(vec![exists(f)])), ("must_not", Json::Arr(vec![one("term", one(f, v))]))])),
                op => {
                    let name = match op {
                        "lt" => "lt",
                        "gt" => "gt",
                        "le" => "lte",
                        _ => "gte",
                    };
                    one("range", one(f, one(name, v)))
                }
            }
        }
        Cond::Contains(f, text, positive) => {
            let pattern = one("wildcard", one(f, one("value", json::s(wildcard_text(text)))));
            if *positive {
                pattern
            } else {
                one("bool", obj(vec![("filter", Json::Arr(vec![exists(f)])), ("must_not", Json::Arr(vec![pattern]))]))
            }
        }
        Cond::Null(f, is_null) => {
            if *is_null {
                one("bool", one("must_not", Json::Arr(vec![exists(f)])))
            } else {
                exists(f)
            }
        }
        Cond::In(f, vs) => one("terms", one(f, Json::Arr(vs.iter().map(sql_json).collect()))),
        Cond::Nin(f, vs) => one("bool", one("must_not", Json::Arr(vec![one("terms", one(f, Json::Arr(vs.iter().map(sql_json).collect())))]))),
    }
}

/// A path segment as Python's quote(..., safe='') writes it.
fn quote(text: &str) -> String {
    let mut out = String::new();
    for b in text.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn shown(v: &Sql) -> String {
    match v {
        Sql::Text(t) => t.clone(),
        Sql::Int(i) => i.to_string(),
        Sql::Real(f) => crate::fmt::format_float(*f),
        Sql::Null => "None".into(),
    }
}

pub struct Store {
    hosts: Vec<(String, u16)>,
    prefix: String,
    auth: Option<String>,
    uniques: std::sync::Mutex<Vec<(String, Vec<String>)>>,
    log: bool,
}

impl Store {
    pub fn open(url: &str, password: Option<&str>) -> Result<Store, DocError> {
        let t = parse_url(url)?;
        let auth = password.map(|pw| {
            if t.user.is_empty() {
                format!("ApiKey {pw}")
            } else {
                format!("Basic {}", crate::crypto::b64(format!("{}:{pw}", t.user).as_bytes()))
            }
        });
        let store = Store { hosts: t.hosts, prefix: t.prefix, auth, uniques: std::sync::Mutex::new(Vec::new()), log: std::env::var_os("NEXO_SQL_LOG").is_some() };
        // the address answers, and the password is right
        store.call("GET", "/", None, &[])?;
        Ok(store)
    }

    fn index(&self, table: &str) -> String {
        if self.prefix.is_empty() {
            table.to_string()
        } else {
            format!("{}-{table}", self.prefix)
        }
    }

    fn reason(status: u16, answer: &Json) -> String {
        let err = answer.get("error");
        let text = match err {
            Json::Obj(_) => err.get("reason").str().filter(|s| !s.is_empty()).or_else(|| err.get("type").str()).unwrap_or("error").to_string(),
            Json::Str(s) if !s.is_empty() => s.clone(),
            _ => answer.get("message").str().unwrap_or("error").to_string(),
        };
        if status == 401 {
            return format!("the server refuses the password (401): {text}");
        }
        format!("{status}: {text}")
    }

    /// (status, answer) of a request; an error for a status of 400 or more
    /// not in `ok`.
    fn call(&self, method: &str, path: &str, body: Option<&Json>, ok: &[u16]) -> Result<(u16, Json), DocError> {
        let data = body.map(|b| b.compact());
        if self.log {
            crate::state::flush();
            match &data {
                Some(d) => eprintln!("elastic: {method} {path} {d}"),
                None => eprintln!("elastic: {method} {path}"),
            }
        }
        let mut last = None;
        for (host, port) in &self.hosts {
            // a server on another machine is reached with TLS only (D172)
            let scheme = if local_host(host) { "http" } else { "https" };
            let h = if host.contains(':') { format!("[{host}]") } else { host.clone() };
            let url = format!("{scheme}://{h}:{port}{path}");
            let mut b = ureq::AgentBuilder::new().redirects(0).timeout(std::time::Duration::from_secs(60)).user_agent("nexo/0.1");
            if let Some(p) = crate::http::proxy_for(&url) {
                b = b.proxy(p);
            }
            let mut req = b.build().request(method, &url).set("Content-Type", "application/json").set("Accept", "application/json");
            if let Some(a) = &self.auth {
                req = req.set("Authorization", a);
            }
            let result = match &data {
                Some(d) => req.send_bytes(d.as_bytes()),
                None => req.call(),
            };
            let (status, resp) = match result {
                Ok(r) => (r.status(), r),
                Err(ureq::Error::Status(code, r)) => (code, r),
                Err(ureq::Error::Transport(t)) => {
                    let reason = nexo_diag::msg(crate::http::transport_reason(&t), &[]);
                    last = Some(DocError::new(format!("cannot reach {host}:{port}: {reason}")));
                    continue;
                }
            };
            let text = resp.into_string().unwrap_or_default();
            let answer = if text.trim().is_empty() {
                Json::Obj(Vec::new())
            } else {
                json::parse(&text).unwrap_or_else(|| one("error", one("reason", json::s(text.clone()))))
            };
            if status >= 400 && !ok.contains(&status) {
                return Err(DocError::new(Self::reason(status, &answer)));
            }
            return Ok((status, answer));
        }
        Err(last.unwrap_or_else(|| DocError::new("the address names no host")))
    }

    fn document(row: &Row) -> Json {
        Json::Obj(row.iter().filter(|(_, v)| !matches!(v, Sql::Null)).map(|(k, v)| (k.clone(), sql_json(v))).collect())
    }

    fn uniques_of(&self, table: &str) -> Vec<String> {
        self.uniques.lock().unwrap().iter().find(|(t, _)| t == table).map(|(_, u)| u.clone()).unwrap_or_default()
    }

    fn unique_id(table: &str, column: &str, value: &Sql) -> String {
        let text = format!("{table}\0{column}\0{}", sql_json(value).compact());
        Sha256::digest(text.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
    }

    /// Create the unique keys of a row's new values; the ids created.
    fn claim(&self, table: &str, row: &Row, old: Option<&Row>) -> Result<Vec<String>, DocError> {
        let mut made = Vec::new();
        for column in self.uniques_of(table) {
            let v = get(row, &column);
            if matches!(v, Sql::Null) || old.is_some_and(|o| same(&get(o, &column), &v)) {
                continue;
            }
            let uid = Self::unique_id(table, &column, &v);
            let body = obj(vec![("table", json::s(table)), ("column", json::s(column.clone()))]);
            let (status, _) = self.call("PUT", &format!("/{}/_create/{uid}?refresh=true", self.index("nexo_unique")), Some(&body), &[409])?;
            if status == 409 {
                self.release(&made)?;
                return Err(DocError { message: format!("the value of {column} is already in {table}"), code: 0, duplicate: Some(column) });
            }
            made.push(uid);
        }
        Ok(made)
    }

    fn release(&self, ids: &[String]) -> Result<(), DocError> {
        for uid in ids {
            self.call("DELETE", &format!("/{}/_doc/{uid}?refresh=true", self.index("nexo_unique")), None, &[404])?;
        }
        Ok(())
    }

    /// The unique keys of an old row's values a new row no longer has.
    fn freed(&self, table: &str, old: &Row, row: Option<&Row>) -> Vec<String> {
        let mut out = Vec::new();
        for column in self.uniques_of(table) {
            let v = get(old, &column);
            if !matches!(v, Sql::Null) && row.is_none_or(|r| !same(&get(r, &column), &v)) {
                out.push(Self::unique_id(table, &column, &v));
            }
        }
        out
    }

    fn doc_path(&self, table: &str, value: &Sql) -> String {
        format!("/{}/_doc/{}", self.index(table), quote(&shown(value)))
    }

    fn get_doc(&self, table: &str, value: &Sql) -> Result<Option<Row>, DocError> {
        let (status, answer) = self.call("GET", &self.doc_path(table, value), None, &[404])?;
        if status == 200 && answer.get("found") == &Json::Bool(true) {
            if let Json::Obj(pairs) = answer.get("_source") {
                return Ok(Some(pairs.iter().map(|(k, v)| (k.clone(), json_sql(v))).collect()));
            }
        }
        Ok(None)
    }
}

/// Python's == between two stored values.
fn same(a: &Sql, b: &Sql) -> bool {
    match (a, b) {
        (Sql::Int(x), Sql::Real(y)) | (Sql::Real(y), Sql::Int(x)) => *x as f64 == *y,
        _ => a == b,
    }
}

impl crate::docdb::DocStore for Store {
    fn name(&self) -> &'static str {
        "elastic"
    }

    fn ensure(&self, table: &str, _key: &str, fields: &[(String, &'static str)], uniques: &[String]) -> Result<(), DocError> {
        let properties = Json::Obj(fields.iter().map(|(n, k)| (n.clone(), one("type", json::s(*k)))).collect());
        let body = one("mappings", obj(vec![("dynamic", json::s("strict")), ("properties", properties.clone())]));
        let index = self.index(table);
        let (status, answer) = self.call("PUT", &format!("/{index}"), Some(&body), &[400])?;
        if status == 400 {
            if answer.get("error").get("type").str() != Some("resource_already_exists_exception") {
                return Err(DocError::new(Self::reason(status, &answer)));
            }
            let (_, have) = self.call("GET", &format!("/{index}/_mapping"), None, &[])?;
            let known = match &have {
                Json::Obj(pairs) => pairs.first().map(|(_, v)| v.get("mappings").get("properties").clone()).unwrap_or(Json::Null),
                _ => Json::Null,
            };
            let Json::Obj(props) = &properties else { unreachable!() };
            let missing: Vec<(String, Json)> = props.iter().filter(|(k, _)| !known.has(k)).cloned().collect();
            if !missing.is_empty() {
                self.call("PUT", &format!("/{index}/_mapping"), Some(&one("properties", Json::Obj(missing))), &[])?;
            }
        }
        {
            let mut all = self.uniques.lock().unwrap();
            all.retain(|(t, _)| t != table);
            all.push((table.to_string(), uniques.to_vec()));
        }
        if !uniques.is_empty() {
            let props = obj(vec![("table", one("type", json::s("keyword"))), ("column", one("type", json::s("keyword")))]);
            let body = one("mappings", obj(vec![("dynamic", json::s("strict")), ("properties", props)]));
            self.call("PUT", &format!("/{}", self.index("nexo_unique")), Some(&body), &[400])?;
        }
        Ok(())
    }

    fn next_id(&self, table: &str) -> Result<i64, DocError> {
        let body = obj(vec![
            ("script", obj(vec![("source", json::s("ctx._source.n += 1")), ("lang", json::s("painless"))])),
            ("upsert", one("n", Json::Int(1))),
            ("_source", Json::Bool(true)),
        ]);
        let path = format!("/{}/_update/{}?refresh=true&retry_on_conflict=10", self.index("nexo_counters"), quote(table));
        let (_, answer) = self.call("POST", &path, Some(&body), &[])?;
        answer.get("get").get("_source").get("n").int().ok_or_else(|| DocError::new("the server's answer is not valid"))
    }

    fn insert(&self, table: &str, key: &str, row: &Row) -> Result<(), DocError> {
        let made = self.claim(table, row, None)?;
        let path = format!("/{}/_create/{}?refresh=true", self.index(table), quote(&shown(&get(row, key))));
        let (status, _) = self.call("PUT", &path, Some(&Self::document(row)), &[409])?;
        if status == 409 {
            self.release(&made)?;
            return Err(DocError { message: format!("the value of {key} is already in {table}"), code: 0, duplicate: Some(key.to_string()) });
        }
        Ok(())
    }

    fn replace(&self, table: &str, key: &str, row: &Row, upsert: bool) -> Result<(), DocError> {
        let value = get(row, key);
        let old = self.get_doc(table, &value)?;
        if old.is_none() && !upsert {
            return Ok(());
        }
        let made = self.claim(table, row, old.as_ref())?;
        if let Err(e) = self.call("PUT", &format!("{}?refresh=true", self.doc_path(table, &value)), Some(&Self::document(row)), &[]) {
            self.release(&made)?;
            return Err(e);
        }
        if let Some(old) = old {
            self.release(&self.freed(table, &old, Some(row)))?;
        }
        Ok(())
    }

    fn delete(&self, table: &str, _key: &str, value: &Sql) -> Result<(), DocError> {
        let Some(old) = self.get_doc(table, value)? else { return Ok(()) };
        self.call("DELETE", &format!("{}?refresh=true", self.doc_path(table, value)), None, &[404])?;
        self.release(&self.freed(table, &old, None))
    }

    fn find(&self, table: &str, key: &str, cond: Option<&Cond>, sort: &[(String, bool)], limit: Option<i64>, fields: Option<&[String]>) -> Result<Vec<Row>, DocError> {
        if limit == Some(0) {
            return Ok(Vec::new());
        }
        let mut order = Vec::new();
        let mut seen: Vec<&str> = Vec::new();
        for (name, desc) in sort {
            if !seen.contains(&name.as_str()) {
                seen.push(name);
                let spec = obj(vec![
                    ("order", json::s(if *desc { "desc" } else { "asc" })),
                    ("missing", json::s(if *desc { "_last" } else { "_first" })),
                ]);
                order.push(one(name, spec));
            }
        }
        if !seen.contains(&key) {
            order.push(one(key, one("order", json::s("asc"))));
        }
        let mut body = obj(vec![("query", query_of(cond)), ("sort", Json::Arr(order)), ("track_total_hits", Json::Bool(false))]);
        if let Some(fields) = fields {
            let mut names: Vec<String> = Vec::new();
            for f in fields.iter().map(|s| s.as_str()).chain(std::iter::once(key)) {
                if !names.iter().any(|n| n == f) {
                    names.push(f.to_string());
                }
            }
            body.set("_source", Json::Arr(names.into_iter().map(Json::Str).collect()));
        }
        let mut rows: Vec<Row> = Vec::new();
        let mut after: Option<Json> = None;
        loop {
            let size = match limit {
                None => PAGE,
                Some(n) => PAGE.min(n - rows.len() as i64),
            };
            let mut page = body.clone();
            page.set("size", Json::Int(size));
            if let Some(a) = &after {
                page.set("search_after", a.clone());
            }
            let (_, answer) = self.call("POST", &format!("/{}/_search", self.index(table)), Some(&page), &[])?;
            let hits = match answer.get("hits").get("hits") {
                Json::Arr(h) => h.clone(),
                _ => Vec::new(),
            };
            for h in &hits {
                if let Json::Obj(pairs) = h.get("_source") {
                    rows.push(pairs.iter().map(|(k, v)| (k.clone(), json_sql(v))).collect());
                } else {
                    rows.push(Vec::new());
                }
            }
            if (hits.len() as i64) < size || limit.is_some_and(|n| rows.len() as i64 >= n) {
                return Ok(rows);
            }
            after = hits.last().map(|h| h.get("sort").clone());
        }
    }

    fn count(&self, table: &str, _key: &str, cond: Option<&Cond>) -> Result<i64, DocError> {
        let (_, answer) = self.call("POST", &format!("/{}/_count", self.index(table)), Some(&one("query", query_of(cond))), &[])?;
        Ok(answer.get("count").int().unwrap_or(0))
    }
}
