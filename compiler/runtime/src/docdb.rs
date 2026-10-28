//! nexo.db on document stores (D171-D172), as phase 0's docdb.py: MongoDB
//! (mongo.rs) and Elasticsearch (elastic.rs). A table is a collection (an
//! index), a row a document; the same `@table` types, queries, relations,
//! transactions and privacy as on SQL databases, with the same results.
//!
//! The conditions of a pipeline are where_tree's (db.rs) in negation normal
//! form, so that each leaf leaves out the rows whose column is none, as
//! SQL's three-valued logic does. A transaction keeps a journal of what it
//! wrote and, when it fails, writes it back (D171).

use std::sync::Mutex;

use nexo_diag::Diagnostic;

use crate::db::{Clause, Column, Database, Sort, Sql, TableInfo, Tree};
use crate::fmt::repr;
use crate::state::{diag, failure, failure_as, R};
use crate::value::*;

const PRIVACY: &str = "nexo_privacy";
/// the key of a table without `id: id`
const HIDDEN_KEY: &str = "nexo_row";

/// A condition of a document store, in negation normal form: each leaf
/// leaves out the rows whose column is none.
#[derive(Clone, Debug)]
pub enum Cond {
    And(Vec<Cond>),
    Or(Vec<Cond>),
    /// a column against a value; op: eq ne lt gt le ge
    Cmp(String, &'static str, Sql),
    /// a text column holds (or does not hold) a text
    Contains(String, String, bool),
    /// a column is (or is not) none
    Null(String, bool),
    In(String, Vec<Sql>),
    Nin(String, Vec<Sql>),
}

/// An error a store reported or a failure to reach it; `duplicate` is the
/// column of a broken unique key.
#[derive(Debug, Clone, Default)]
pub struct DocError {
    pub message: String,
    pub code: i64,
    pub duplicate: Option<String>,
}

impl DocError {
    pub fn new(message: impl Into<String>) -> DocError {
        DocError { message: message.into(), ..Default::default() }
    }
}

/// A row of a store: (column, value), in order.
pub type Row = Vec<(String, Sql)>;

pub fn get(row: &Row, name: &str) -> Sql {
    row.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone()).unwrap_or(Sql::Null)
}

fn set(row: &mut Row, name: &str, v: Sql) {
    match row.iter_mut().find(|(k, _)| k == name) {
        Some(slot) => slot.1 = v,
        None => row.push((name.to_string(), v)),
    }
}

/// Python's unquote: %XX as the byte it names.
pub fn unquote(text: &str) -> String {
    let b = text.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let Ok(v) = u8::from_str_radix(&text[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// The operations of a document store.
pub trait DocStore: Send + Sync {
    fn name(&self) -> &'static str;
    fn ensure(&self, table: &str, key: &str, fields: &[(String, &'static str)], uniques: &[String]) -> Result<(), DocError>;
    fn next_id(&self, table: &str) -> Result<i64, DocError>;
    fn insert(&self, table: &str, key: &str, row: &Row) -> Result<(), DocError>;
    fn replace(&self, table: &str, key: &str, row: &Row, upsert: bool) -> Result<(), DocError>;
    fn delete(&self, table: &str, key: &str, value: &Sql) -> Result<(), DocError>;
    fn find(&self, table: &str, key: &str, cond: Option<&Cond>, sort: &[(String, bool)], limit: Option<i64>, fields: Option<&[String]>) -> Result<Vec<Row>, DocError>;
    fn count(&self, table: &str, key: &str, cond: Option<&Cond>) -> Result<i64, DocError>;
}

/// The store of a database and the journal of its transactions.
pub struct Doc {
    pub store: Box<dyn DocStore>,
    /// (kind, table, key column, row) to write back on a rollback
    journal: Mutex<Vec<(&'static str, String, String, Row)>>,
    marks: Mutex<Vec<usize>>,
}

fn doc(db: &Database) -> &Doc {
    db.doc.as_ref().unwrap()
}

fn key_of(info: &TableInfo) -> String {
    info.key.clone().unwrap_or_else(|| HIDDEN_KEY.to_string())
}

fn all_of(mut conds: Vec<Cond>) -> Option<Cond> {
    match conds.len() {
        0 => None,
        1 => conds.pop(),
        _ => Some(Cond::And(conds)),
    }
}

/// A store's result, its error as nexo.db's DatabaseError.
fn call<T>(db: &Database, r: Result<T, DocError>, info: Option<&TableInfo>, shown: Option<&[(String, Val)]>, span: u32) -> R<T> {
    r.map_err(|e| {
        if let (Some(dup), Some(info)) = (e.duplicate.as_deref(), info) {
            let field = info.columns.iter().find(|c| c.name == dup).map(|c| c.field.clone()).unwrap_or(dup.to_string());
            let value = shown.and_then(|vs| vs.iter().find(|(n, _)| *n == field).map(|(_, v)| repr(v.v()))).unwrap_or("none".into());
            return failure_as("DatabaseError", "DatabaseError.unique", span, &[("type", info.rtype.name.clone()), ("field", field), ("value", value)]);
        }
        failure("DatabaseError", span, &[("db", db.path.clone()), ("reason", e.message)])
    })
}

/// A where tree in negation normal form, or None when a store cannot run
/// it (a column of the table a ref points to, two columns compared).
pub fn nnf(tree: &Tree, neg: bool) -> Option<Cond> {
    let inverse = |op: &'static str| match op {
        "eq" => "ne",
        "ne" => "eq",
        "lt" => "ge",
        "ge" => "lt",
        "gt" => "le",
        _ => "gt",
    };
    match tree {
        Tree::Not(a) => nnf(a, !neg),
        Tree::And(a, b) | Tree::Or(a, b) => {
            let (x, y) = (nnf(a, neg), nnf(b, neg));
            let (x, y) = (x?, y?);
            let and = matches!(tree, Tree::And(..)) != neg;
            let mut items = Vec::new();
            for c in [x, y] {
                match c {
                    Cond::And(inner) if and => items.extend(inner),
                    Cond::Or(inner) if !and => items.extend(inner),
                    c => items.push(c),
                }
            }
            Some(if and { Cond::And(items) } else { Cond::Or(items) })
        }
        Tree::ColCol(..) => None,
        Tree::Truth(c, None) => Some(Cond::Cmp(c.name.clone(), if neg { "ne" } else { "eq" }, Sql::Int(1))),
        Tree::Contains(c, text, None) => Some(Cond::Contains(c.name.clone(), text.clone(), !neg)),
        Tree::Null(c, is_null, None) => Some(Cond::Null(c.name.clone(), *is_null != neg)),
        Tree::Cmp(c, sym, raw, None) => {
            let op = match sym.as_str() {
                "=" => "eq",
                "<>" => "ne",
                "<" => "lt",
                ">" => "gt",
                "<=" => "le",
                _ => "ge",
            };
            Some(Cond::Cmp(c.name.clone(), if neg { inverse(op) } else { op }, raw.clone()))
        }
        _ => None,
    }
}

/// The type of a field in a store that asks for one (Elasticsearch).
fn store_type(info: &TableInfo, c: &Column) -> &'static str {
    if info.key.as_deref() == Some(&c.field) {
        return "long";
    }
    if c.private {
        return "keyword";
    }
    match c.kind {
        "int" | "ref" | "bool" => "long",
        "float" => "double",
        _ => "keyword",
    }
}

// ----------------------------------------------------------------- tables

/// A table's store side, the first time a program uses it.
pub fn ensure(db: &Database, info: &TableInfo, personal: bool, span: u32) -> R<()> {
    let mut fields: Vec<(String, &'static str)> = info.columns.iter().map(|c| (c.name.clone(), store_type(info, c))).collect();
    if info.key.is_none() {
        fields.push((HIDDEN_KEY.into(), "long"));
    }
    let uniques: Vec<String> = info.columns.iter().filter(|c| c.unique).map(|c| c.name.clone()).collect();
    call(db, doc(db).store.ensure(&info.name, &key_of(info), &fields, &uniques), None, None, span)?;
    db.synced.lock().unwrap().push(info.name.clone());
    if personal && !db.synced.lock().unwrap().iter().any(|n| n == PRIVACY) {
        let fields = vec![("id".to_string(), "keyword"), ("tbl".to_string(), "keyword"), ("row".to_string(), "long"), ("state".to_string(), "keyword")];
        call(db, doc(db).store.ensure(PRIVACY, "id", &fields, &[]), None, None, span)?;
        db.synced.lock().unwrap().push(PRIVACY.into());
    }
    Ok(())
}

/// row -> state of the restricted and erased rows of a table.
fn states(db: &Database, table: &str, span: u32) -> R<Vec<(i64, String)>> {
    let cond = Cond::Cmp("tbl".into(), "eq", Sql::Text(table.into()));
    let rows = call(db, doc(db).store.find(PRIVACY, "id", Some(&cond), &[], None, None), None, None, span)?;
    let mut out: Vec<(i64, String)> = Vec::new();
    for r in rows {
        if let (Sql::Int(row), Sql::Text(state)) = (get(&r, "row"), get(&r, "state")) {
            out.retain(|(k, _)| *k != row);
            out.push((row, state));
        }
    }
    Ok(out)
}

/// The restricted and erased rows are left out (D116); the rows past their
/// keep period are erased when due.
pub fn guard(db: &Database, me: V, info: &TableInfo, p: &crate::privacy::Info, due: bool, span: u32) -> R<Vec<Clause>> {
    if due {
        purge(db, me, info, p, span)?;
    }
    let mut hidden: Vec<i64> = states(db, &info.name, span)?.into_iter().map(|(k, _)| k).collect();
    hidden.sort();
    if hidden.is_empty() {
        return Ok(Vec::new());
    }
    Ok(vec![Clause::Doc(Cond::Nin(info.key.clone().unwrap_or_default(), hidden.into_iter().map(Sql::Int).collect()))])
}

// ---------------------------------------------------------------- queries

fn doc_conds(wheres: &[Clause]) -> Vec<Cond> {
    wheres.iter().filter_map(|w| if let Clause::Doc(c) = w { Some(c.clone()) } else { None }).collect()
}

/// The rows of a query: the raw values of `columns` in each.
#[allow(clippy::too_many_arguments)]
pub fn fetch(db: &Database, me: V, info: &TableInfo, wheres: &[Clause], order: &[Sort], limit: Option<i64>, columns: &[&Column], picked: bool, span: u32) -> R<Vec<Vec<Sql>>> {
    let mut conds = doc_conds(&db.guard(me, info, span)?);
    conds.extend(doc_conds(wheres));
    let cond = all_of(conds);
    let key = key_of(info);
    let mut sort: Vec<(String, bool)> = order.iter().filter_map(|o| if let Sort::Doc(c, d) = o { Some((c.clone(), *d)) } else { None }).collect();
    sort.push((key.clone(), false));
    let fields: Option<Vec<String>> = if picked { Some(columns.iter().map(|c| c.name.clone()).collect()) } else { None };
    let rows = call(db, doc(db).store.find(&info.name, &key, cond.as_ref(), &sort, limit, fields.as_deref()), None, None, span)?;
    Ok(rows.iter().map(|r| columns.iter().map(|c| get(r, &c.name)).collect()).collect())
}

pub fn count(db: &Database, me: V, info: &TableInfo, wheres: &[Clause], span: u32) -> R<i64> {
    let mut conds = doc_conds(&db.guard(me, info, span)?);
    conds.extend(doc_conds(wheres));
    let cond = all_of(conds);
    call(db, doc(db).store.count(&info.name, &key_of(info), cond.as_ref()), None, None, span)
}

// ----------------------------------------------------------------- writes

fn get_row(db: &Database, table: &str, key: &str, value: &Sql, span: u32) -> R<Option<Row>> {
    let cond = Cond::Cmp(key.into(), "eq", value.clone());
    let rows = call(db, doc(db).store.find(table, key, Some(&cond), &[], Some(1), None), None, None, span)?;
    Ok(rows.into_iter().next())
}

/// Insert, replace or delete a document, with what undoes it in the journal
/// of the transaction.
#[allow(clippy::too_many_arguments)]
fn write(db: &Database, kind: &'static str, table: &str, key: &str, row: &Row, info: Option<&TableInfo>, shown: Option<&[(String, Val)]>, span: u32) -> R<()> {
    let open = *db.depth.lock().unwrap() > 0;
    let old = if open && kind != "insert" { get_row(db, table, key, &get(row, key), span)? } else { None };
    let store = &doc(db).store;
    let r = match kind {
        "insert" => store.insert(table, key, row),
        "replace" => store.replace(table, key, row, false),
        "put" => store.replace(table, key, row, true),
        _ => store.delete(table, key, &get(row, key)),
    };
    call(db, r, info, shown, span)?;
    if open {
        let mut journal = doc(db).journal.lock().unwrap();
        if kind == "insert" || (kind == "put" && old.is_none()) {
            journal.push(("delete", table.into(), key.into(), row.clone()));
        } else if let Some(old) = old {
            journal.push((if kind == "delete" { "insert" } else { "replace" }, table.into(), key.into(), old));
        }
    }
    Ok(())
}

fn cells(db: &Database, info: &TableInfo, fields: &[Val], span: u32) -> R<Row> {
    let mut row = Vec::with_capacity(info.columns.len());
    for (i, c) in info.columns.iter().enumerate() {
        row.push((c.name.clone(), db.cell(fields[i].v(), c, span)?));
    }
    Ok(row)
}

/// `db.T.add(...)`: the key it gave the row, when it gave one.
pub fn insert(db: &Database, info: &TableInfo, fields: &[Val], shown: &[(String, Val)], span: u32) -> R<Option<i64>> {
    let key = key_of(info);
    let mut row = cells(db, info, fields, span)?;
    let mut given = None;
    if matches!(get(&row, &key), Sql::Null) {
        let id = call(db, doc(db).store.next_id(&info.name), None, None, span)?;
        set(&mut row, &key, Sql::Int(id));
        given = Some(id);
    }
    write(db, "insert", &info.name, &key, &row, Some(info), Some(shown), span)?;
    Ok(given.filter(|_| info.key.is_some()))
}

pub fn replace(db: &Database, info: &TableInfo, key: i64, fields: &[Val], shown: &[(String, Val)], span: u32) -> R<()> {
    let kc = info.key.clone().unwrap_or_default();
    let mut row = cells(db, info, fields, span)?;
    set(&mut row, &kc, Sql::Int(key));
    if get_row(db, &info.name, &kc, &Sql::Int(key), span)?.is_some() {
        write(db, "replace", &info.name, &kc, &row, Some(info), Some(shown), span)?;
    }
    Ok(())
}

pub fn delete(db: &Database, info: &TableInfo, key: i64, span: u32) -> R<()> {
    let kc = info.key.clone().unwrap_or_default();
    write(db, "delete", &info.name, &kc.clone(), &vec![(kc, Sql::Int(key))], None, None, span)
}

// ----------------------------------------------------------- transactions

pub fn begin(db: &Database) {
    let d = doc(db);
    let n = d.journal.lock().unwrap().len();
    d.marks.lock().unwrap().push(n);
}

/// The end of a transaction (the depth after it): a rollback writes back
/// what it wrote.
pub fn end(db: &Database, commit: bool, depth: usize, span: u32) -> R<()> {
    let d = doc(db);
    let mark = d.marks.lock().unwrap().pop().unwrap_or(0);
    if !commit {
        let undo: Vec<_> = {
            let mut j = d.journal.lock().unwrap();
            let at = mark.min(j.len());
            j.split_off(at)
        };
        for (kind, table, key, row) in undo.into_iter().rev() {
            let r = match kind {
                "delete" => d.store.delete(&table, &key, &get(&row, &key)),
                "insert" => d.store.insert(&table, &key, &row),
                _ => d.store.replace(&table, &key, &row, false),
            };
            call(db, r, None, None, span)?;
        }
    }
    if depth == 0 {
        d.journal.lock().unwrap().clear();
    }
    Ok(())
}

// ---------------------------------------------------------------- privacy

pub fn set_state(db: &Database, table: &str, key: i64, state: &str, span: u32) -> R<()> {
    let row = vec![
        ("id".to_string(), Sql::Text(format!("{table}:{key}"))),
        ("tbl".to_string(), Sql::Text(table.into())),
        ("row".to_string(), Sql::Int(key)),
        ("state".to_string(), Sql::Text(state.into())),
    ];
    write(db, "put", PRIVACY, "id", &row, None, None, span)
}

/// Erase the rows past their keep period (section 23, rule 2).
fn purge(db: &Database, me: V, info: &TableInfo, p: &crate::privacy::Info, span: u32) -> R<()> {
    db.purged.lock().unwrap().insert(info.name.clone(), crate::db::now_secs());
    let (Some(_), Some(after), Some(key)) = (p.keep, &p.after, &info.key) else { return Ok(()) };
    let Some(col) = info.column(after) else { return Ok(()) };
    let mut erased: Vec<i64> = states(db, &info.name, span)?.into_iter().filter(|(_, s)| s == "erased").map(|(k, _)| k).collect();
    erased.sort();
    let mut conds = vec![Cond::Cmp(col.name.clone(), "lt", Sql::Text(crate::privacy::cutoff(p, col.kind)))];
    if !erased.is_empty() {
        conds.push(Cond::Nin(key.clone(), erased.into_iter().map(Sql::Int).collect()));
    }
    let cond = all_of(conds);
    let fields = vec![key.clone()];
    let rows = call(db, doc(db).store.find(&info.name, key, cond.as_ref(), &[(key.clone(), false)], None, Some(&fields)), None, None, span)?;
    for r in rows {
        if let Sql::Int(k) = get(&r, key) {
            erase_row(db, me, info, k, span)?;
        }
    }
    Ok(())
}

/// Set the personal fields of the rows `cond` picks to blank values.
fn anonymise(db: &Database, info: &TableInfo, cond: &Cond, span: u32) -> R<()> {
    let Some(p) = crate::privacy::info_of(&info.rtype.name) else { return Ok(()) };
    let cols: Vec<&Column> = p.marks.iter().filter_map(|(f, _)| info.column(f)).collect();
    if cols.is_empty() {
        return Ok(());
    }
    let key = key_of(info);
    let rows = call(db, doc(db).store.find(&info.name, &key, Some(cond), &[(key.clone(), false)], None, None), None, None, span)?;
    for r in rows {
        let mut row = r.clone();
        let id = match get(&r, &key) {
            Sql::Int(i) => i,
            _ => 0,
        };
        for c in &cols {
            // a @unique field is made from the row's id, so that several
            // erased rows can keep it unique
            let v = if c.optional {
                Sql::Null
            } else {
                match (c.kind, c.unique) {
                    ("str", true) => Sql::Text(format!("erased-{id}")),
                    ("int", true) => Sql::Int(-id),
                    ("float", true) => Sql::Real(-(id as f64)),
                    ("int" | "bool", _) => Sql::Int(0),
                    ("float", _) => Sql::Real(0.0),
                    ("date", _) => Sql::Text("0001-01-01".into()),
                    ("time", _) => Sql::Text("1970-01-01T00:00:00.000Z".into()),
                    _ => Sql::Text(String::new()),
                }
            };
            let v = match (&v, c.private, &db.key) {
                (Sql::Null, _, _) | (_, false, _) | (_, _, None) => v,
                (Sql::Int(i), true, Some(k)) => Sql::Text(crate::privacy::seal(k, &i.to_string())),
                (Sql::Real(f), true, Some(k)) => Sql::Text(crate::privacy::seal(k, &crate::fmt::format_float(*f))),
                (Sql::Text(t), true, Some(k)) => Sql::Text(crate::privacy::seal(k, t)),
            };
            set(&mut row, &c.name, v);
        }
        write(db, "replace", &info.name, &key, &row, Some(info), None, span)?;
    }
    Ok(())
}

/// Delete the row, or anonymise it when other rows point to it; the
/// personal fields of the rows that point to it are anonymised too.
pub fn erase_row(db: &Database, me: V, info: &TableInfo, key: i64, span: u32) -> R<()> {
    let mut referenced = false;
    for other in crate::db::tables() {
        for c in &other.columns {
            if c.kind == "ref" && c.target.as_deref() == Some(&info.rtype.name) {
                db.sync(me, &other, span)?;
                let cond = Cond::Cmp(c.name.clone(), "eq", Sql::Int(key));
                anonymise(db, &other, &cond, span)?;
                if call(db, doc(db).store.count(&other.name, &key_of(&other), Some(&cond)), None, None, span)? > 0 {
                    referenced = true;
                }
            }
        }
    }
    let kc = info.key.clone().unwrap_or_default();
    if referenced {
        anonymise(db, info, &Cond::Cmp(kc, "eq", Sql::Int(key)), span)?;
        set_state(db, &info.name, key, "erased", span)?;
    } else {
        delete(db, info, key, span)?;
        let id = Sql::Text(format!("{}:{key}", info.name));
        if get_row(db, PRIVACY, "id", &id, span)?.is_some() {
            write(db, "delete", PRIVACY, "id", &vec![("id".to_string(), id)], None, None, span)?;
        }
    }
    Ok(())
}

// ---------------------------------------------------------------- connect

/// connect("mongodb://...") (D171) or connect("elasticsearch://...") (D172).
pub fn connect(path: String, key: Option<V>, span: u32) -> R<Val> {
    crate::security::check_connector(span)?;
    let fail = |e: DocError, path: &str| failure("DatabaseError", span, &[("db", path.to_string()), ("reason", e.message)]);
    let mongo = path.starts_with("mongodb://");
    let hosts = if mongo { crate::mongo::parse_url(&path).map(|t| t.hosts) } else { crate::elastic::parse_url(&path).map(|t| t.hosts) };
    let hosts = hosts.map_err(|e| fail(e, &path))?;
    for (host, _) in &hosts {
        crate::policy::check("net", host, span)?;
    }
    let mut password = None;
    if let Some(k) = key.filter(|k| !k.is_none()) {
        password = Some(crate::crypto::text_or_secret("connect", k, span)?);
    }
    let store: Box<dyn DocStore> = if mongo {
        Box::new(crate::mongo::Store::open(&path, password.as_deref()).map_err(|e| fail(e, &path))?)
    } else {
        Box::new(crate::elastic::Store::open(&path, password.as_deref()).map_err(|e| fail(e, &path))?)
    };
    let d = Doc { store, journal: Mutex::new(Vec::new()), marks: Mutex::new(Vec::new()) };
    Ok(crate::db::document_database(path, d, password.as_deref().map(crate::privacy::data_key)))
}

/// db.sql on a document store.
pub fn no_sql(db: &Database, span: u32) -> crate::state::Fail {
    diag(Diagnostic::new_spanless("db_no_sql").arg("db", db.path.clone()), span)
}
