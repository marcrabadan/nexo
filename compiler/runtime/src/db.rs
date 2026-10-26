//! nexo.db: databases through `connect` and `@table` types (section 6,
//! D100). A database is an SQLite file; each `@table` type is a table
//! named after the type in lowercase, created (and given the columns of
//! new fields) the first time the program uses it. `db.T` is a query;
//! pipelines on a query run in SQL while their steps allow it (`where` on
//! columns, `sort by` a column, `take`, `count`), and in memory from the
//! first step that does not, with the same result as phase 0.

use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::os::raw::c_int;
use std::sync::{Arc, Mutex, OnceLock};

use libsqlite3_sys as ffi;
use nexo_diag::{msg, Diagnostic};

use crate::fmt::{repr, type_name};
use crate::state::{diag, failure, failure_as, R};
use crate::stdlib::{expects, missing_member};
use crate::types::{lookup, RecordType, TypeDesc, TypeRef};
use crate::value::*;

const KINDS: &[&str] = &["int", "float", "bool", "str", "decimal", "money", "date", "time"];

#[derive(Clone)]
pub struct Column {
    pub field: String,
    pub name: String,
    pub kind: &'static str,
    pub optional: bool,
    pub unique: bool,
    /// the type a `ref` column points to
    pub target: Option<String>,
    /// the enum of a "variant" column
    pub variants: Option<&'static crate::types::EnumType>,
    /// @private: encrypted at rest (D116)
    pub private: bool,
}

pub struct TableInfo {
    pub rtype: &'static RecordType,
    pub name: String,
    pub key: Option<String>,
    /// many fields: field -> target type name
    pub many: Vec<(String, String)>,
    pub columns: Vec<Column>,
}

impl TableInfo {
    fn column(&self, field: &str) -> Option<&Column> {
        self.columns.iter().find(|c| c.field == field)
    }

    fn key_index(&self) -> Option<usize> {
        let k = self.key.as_ref()?;
        self.rtype.fields.iter().position(|f| &f.name == k)
    }
}

#[derive(Default)]
struct Registry {
    tables: Vec<(String, Arc<TableInfo>)>,
    /// the database each type was last used with (for `many` fields)
    db_of_type: HashMap<String, Val>,
}

fn registry() -> &'static Mutex<Registry> {
    static R: OnceLock<Mutex<Registry>> = OnceLock::new();
    R.get_or_init(|| Mutex::new(Registry::default()))
}

fn table(name: &str) -> Option<Arc<TableInfo>> {
    registry().lock().unwrap().tables.iter().find(|(n, _)| n == name).map(|(_, t)| t.clone())
}

fn table_of(rt: &RecordType) -> Option<Arc<TableInfo>> {
    registry().lock().unwrap().tables.iter().find(|(_, t)| std::ptr::eq(t.rtype, rt)).map(|(_, t)| t.clone())
}

pub fn release_all() {
    let old = std::mem::take(&mut registry().lock().unwrap().db_of_type);
    drop(old);
}

fn literal_kind(options: &[V]) -> Option<&'static str> {
    let kinds: Vec<String> = options.iter().map(|o| type_name(*o)).collect();
    let first = kinds.first()?.clone();
    if kinds.iter().all(|k| *k == first) && ["int", "str", "float", "bool"].contains(&first.as_str()) {
        return Some(crate::num::intern(&first));
    }
    None
}

type Kind = (&'static str, Option<String>, Option<&'static crate::types::EnumType>);

fn column_kind(rt: &RecordType, f: &crate::types::FieldInfo, marks: &[String], span: u32) -> R<Kind> {
    let t = f.ty.unwrap();
    let bad = || {
        diag(
            Diagnostic::new_spanless("table_field")
                .arg("field", f.name.clone())
                .arg("type", rt.name.clone())
                .arg("ftype", t.display().trim_end_matches('?').to_string()),
            span,
        )
    };
    match t {
        TypeDesc::Range { .. } => Ok(("int", None, None)),
        TypeDesc::Union { options, .. } => literal_kind(options).map(|k| (k, None, None)).ok_or_else(bad),
        TypeDesc::Named { name, params, .. } => {
            if !params.is_empty() || !KINDS.contains(&name.as_str()) {
                return Err(bad());
            }
            Ok((crate::num::intern(name), None, None))
        }
        TypeDesc::User { id, name, .. } => match lookup(*id) {
            Some(TypeRef::Alias(a)) => match a.target {
                TypeDesc::Union { options, .. } => literal_kind(options).map(|k| (k, None, None)).ok_or_else(bad),
                TypeDesc::Named { name, params, .. } if params.is_empty() && KINDS.contains(&name.as_str()) => {
                    Ok((crate::num::intern(name), None, None))
                }
                _ => Err(bad()),
            },
            Some(TypeRef::Enum(e)) if e.variants.iter().all(|v| v.params.is_empty()) => Ok(("variant", None, Some(e))),
            Some(TypeRef::Record(_)) => {
                if !marks.iter().any(|m| m == "ref") {
                    return Err(diag(
                        Diagnostic::new_spanless("table_needs_ref")
                            .arg("field", f.name.clone())
                            .arg("type", rt.name.clone())
                            .arg("target", name.clone()),
                        span,
                    ));
                }
                Ok(("ref", Some(name.clone()), None))
            }
            _ => Err(bad()),
        },
        TypeDesc::Error { .. } => Err(bad()),
    }
}

fn map_pairs(v: V) -> Vec<(String, V)> {
    match v.get() {
        Some(Obj::Map(m)) => m.borrow().entries.iter().map(|(k, v)| (k.as_str().unwrap_or("").to_string(), v.v())).collect(),
        _ => Vec::new(),
    }
}

/// `__db_table(T, "id", {"orders": "Order"}, {"email": ["unique"]})`
pub fn register(t: V, key: V, many: V, marks: V, span: u32) -> R<Val> {
    let Some(Obj::Type(TypeRef::Record(rt))) = t.get() else { return Err(expects("__db_table", "type", t, span)) };
    let key = key.as_str().map(|s| s.to_string());
    let marks: Vec<(String, Vec<String>)> = map_pairs(marks)
        .into_iter()
        .map(|(k, v)| {
            let items = match v.get() {
                Some(Obj::List(items)) => items.borrow().iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect(),
                _ => Vec::new(),
            };
            (k, items)
        })
        .collect();
    let mut columns = Vec::new();
    for f in &rt.fields {
        let fmarks = marks.iter().find(|(k, _)| *k == f.name).map(|(_, m)| m.clone()).unwrap_or_default();
        let (kind, target, variants) = column_kind(rt, f, &fmarks, span)?;
        let private = fmarks.iter().any(|m| m == "private");
        if private && kind == "ref" {
            return Err(diag(Diagnostic::new_spanless("privacy_ref").arg("field", f.name.clone()).arg("type", rt.name.clone()), span));
        }
        let name = if kind == "ref" { format!("{}_id", f.name) } else { f.name.clone() };
        columns.push(Column {
            field: f.name.clone(),
            name,
            kind,
            optional: f.ty.unwrap().optional() || key.as_deref() == Some(&f.name),
            unique: fmarks.iter().any(|m| m == "unique"),
            target,
            variants,
            private,
        });
    }
    let many = map_pairs(many).into_iter().map(|(k, v)| (k, v.as_str().unwrap_or("").to_string())).collect();
    let info = Arc::new(TableInfo { rtype: rt, name: rt.name.to_lowercase(), key, many, columns });
    let mut r = registry().lock().unwrap();
    r.tables.retain(|(n, _)| *n != rt.name);
    r.tables.push((rt.name.clone(), info));
    Ok(Val::none())
}

// --------------------------------------------------------------- sqlite

struct Conn(*mut ffi::sqlite3);

unsafe impl Send for Conn {}
unsafe impl Sync for Conn {}

impl Drop for Conn {
    fn drop(&mut self) {
        unsafe { ffi::sqlite3_close(self.0) };
    }
}

#[derive(Clone, Debug)]
pub enum Sql {
    Null,
    Int(i64),
    Real(f64),
    Text(String),
}

pub struct Database {
    path: String,
    conn: Mutex<Conn>,
    synced: Mutex<Vec<String>>,
    depth: Mutex<usize>,
    /// encrypts @private columns (D116)
    key: Option<[u8; 32]>,
    /// when the keep period of each table was last applied (seconds)
    purged: Mutex<HashMap<String, f64>>,
}

fn now_secs() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// A table whose type has @personal or @private fields.
fn personal_table(info: &TableInfo) -> Option<crate::privacy::Info> {
    crate::privacy::info_of(&info.rtype.name).filter(|p| !p.marks.is_empty() && info.key.is_some())
}

/// Rows restricted or erased are left out of every query.
fn hidden_where(info: &TableInfo) -> String {
    format!(
        "{} NOT IN (SELECT \"row\" FROM \"{}\" WHERE \"tbl\" = '{}')",
        quote(info.key.as_deref().unwrap_or("id")),
        crate::privacy::TABLE,
        info.name
    )
}

struct Rows {
    names: Vec<String>,
    rows: Vec<Vec<Sql>>,
    last_id: i64,
}

fn errmsg(db: *mut ffi::sqlite3) -> String {
    unsafe { CStr::from_ptr(ffi::sqlite3_errmsg(db)).to_string_lossy().into_owned() }
}

impl Database {
    fn run(&self, sql: &str, params: &[Sql]) -> Result<Rows, String> {
        if std::env::var_os("NEXO_SQL_LOG").is_some() {
            let shown: Vec<String> = params.iter().map(py_repr).collect();
            crate::state::flush();
            eprintln!("sql: {sql} [{}]", shown.join(", "));
        }
        let conn = self.conn.lock().unwrap();
        let db = conn.0;
        let c = CString::new(sql).map_err(|_| "the query has a NUL character".to_string())?;
        let mut stmt: *mut ffi::sqlite3_stmt = std::ptr::null_mut();
        let rc = unsafe { ffi::sqlite3_prepare_v2(db, c.as_ptr(), -1, &mut stmt, std::ptr::null_mut()) };
        if rc != ffi::SQLITE_OK {
            return Err(errmsg(db));
        }
        struct Stmt(*mut ffi::sqlite3_stmt);
        impl Drop for Stmt {
            fn drop(&mut self) {
                unsafe { ffi::sqlite3_finalize(self.0) };
            }
        }
        let stmt = Stmt(stmt);
        if stmt.0.is_null() {
            return Ok(Rows { names: Vec::new(), rows: Vec::new(), last_id: 0 });
        }
        for (i, p) in params.iter().enumerate() {
            let i = (i + 1) as c_int;
            let rc = unsafe {
                match p {
                    Sql::Null => ffi::sqlite3_bind_null(stmt.0, i),
                    Sql::Int(x) => ffi::sqlite3_bind_int64(stmt.0, i, *x),
                    Sql::Real(x) => ffi::sqlite3_bind_double(stmt.0, i, *x),
                    Sql::Text(s) => ffi::sqlite3_bind_text(
                        stmt.0,
                        i,
                        s.as_ptr() as *const std::os::raw::c_char,
                        s.len() as c_int,
                        ffi::SQLITE_TRANSIENT(),
                    ),
                }
            };
            if rc != ffi::SQLITE_OK {
                return Err(errmsg(db));
            }
        }
        let n = unsafe { ffi::sqlite3_column_count(stmt.0) };
        let names: Vec<String> = (0..n)
            .map(|i| unsafe { CStr::from_ptr(ffi::sqlite3_column_name(stmt.0, i)).to_string_lossy().into_owned() })
            .collect();
        let mut rows = Vec::new();
        loop {
            let rc = unsafe { ffi::sqlite3_step(stmt.0) };
            if rc == ffi::SQLITE_DONE {
                break;
            }
            if rc != ffi::SQLITE_ROW {
                return Err(errmsg(db));
            }
            let mut row = Vec::with_capacity(n as usize);
            for i in 0..n {
                let v = unsafe {
                    match ffi::sqlite3_column_type(stmt.0, i) {
                        ffi::SQLITE_INTEGER => Sql::Int(ffi::sqlite3_column_int64(stmt.0, i)),
                        ffi::SQLITE_FLOAT => Sql::Real(ffi::sqlite3_column_double(stmt.0, i)),
                        ffi::SQLITE_NULL => Sql::Null,
                        _ => {
                            let p = ffi::sqlite3_column_text(stmt.0, i);
                            let len = ffi::sqlite3_column_bytes(stmt.0, i) as usize;
                            if p.is_null() {
                                Sql::Text(String::new())
                            } else {
                                Sql::Text(String::from_utf8_lossy(std::slice::from_raw_parts(p, len)).into_owned())
                            }
                        }
                    }
                };
                row.push(v);
            }
            rows.push(row);
        }
        let last_id = unsafe { ffi::sqlite3_last_insert_rowid(db) };
        Ok(Rows { names, rows, last_id })
    }

    fn fail(&self, text: String, info: Option<&TableInfo>, values: Option<&[(String, Val)]>, span: u32) -> crate::state::Fail {
        if let (Some(rest), Some(info)) = (text.strip_prefix("UNIQUE constraint failed: "), info) {
            let col = rest.split_once('.').map(|x| x.1).unwrap_or(rest);
            let field = info.columns.iter().find(|c| c.name == col).map(|c| c.field.clone()).unwrap_or(col.to_string());
            let value = values.and_then(|vs| vs.iter().find(|(n, _)| *n == field).map(|(_, v)| repr(v.v()))).unwrap_or("none".into());
            return failure_as(
                "DatabaseError",
                "DatabaseError.unique",
                span,
                &[("type", info.rtype.name.clone()), ("field", field), ("value", value)],
            );
        }
        failure("DatabaseError", span, &[("db", self.path.clone()), ("reason", text)])
    }

    fn exec(&self, sql: &str, params: &[Sql], span: u32) -> R<Rows> {
        self.run(sql, params).map_err(|e| self.fail(e, None, None, span))
    }

    /// A value as stored in a column: @private ones encrypted.
    fn cell(&self, v: V, c: &Column, span: u32) -> R<Sql> {
        let raw = to_sql(v, span)?;
        if !c.private {
            return Ok(raw);
        }
        let text = match &raw {
            Sql::Null => return Ok(Sql::Null),
            Sql::Int(i) => i.to_string(),
            Sql::Real(f) => crate::fmt::format_float(*f),
            Sql::Text(t) => t.clone(),
        };
        Ok(Sql::Text(crate::privacy::seal(self.key.as_ref().unwrap(), &text)))
    }

    /// The rows past their keep period are erased (when due) and the
    /// restricted or erased ones left out (D116).
    fn guard(&self, me: V, info: &TableInfo, span: u32) -> R<Vec<String>> {
        let Some(p) = personal_table(info) else { return Ok(Vec::new()) };
        let due = self.purged.lock().unwrap().get(&info.name).is_none_or(|t| now_secs() - t >= 86400.0);
        if due {
            self.purge(me, info, &p, span)?;
        }
        Ok(vec![hidden_where(info)])
    }

    /// Erase the rows past their keep period (section 23, rule 2).
    fn purge(&self, me: V, info: &TableInfo, p: &crate::privacy::Info, span: u32) -> R<()> {
        if let (Some(_), Some(after), Some(key)) = (p.keep, &p.after, &info.key) {
            let col = info.column(after).unwrap();
            let cutoff = crate::privacy::cutoff(p, col.kind);
            let sql = format!(
                "SELECT {k} FROM {t} WHERE {c} < ? AND {k} NOT IN (SELECT \"row\" FROM \"{pt}\" WHERE \"tbl\" = ? AND \"state\" = 'erased') ORDER BY {k}",
                k = quote(key),
                t = quote(&info.name),
                c = quote(&col.name),
                pt = crate::privacy::TABLE
            );
            let rows = self.exec(&sql, &[Sql::Text(cutoff), Sql::Text(info.name.clone())], span)?;
            for row in rows.rows {
                if let Some(Sql::Int(k)) = row.first() {
                    self.erase_row(me, info, *k, span)?;
                }
            }
        }
        self.purged.lock().unwrap().insert(info.name.clone(), now_secs());
        Ok(())
    }

    /// Set the personal fields of the rows `where` picks to blank values.
    fn anonymise(&self, info: &TableInfo, wheres: &str, params: &[Sql], span: u32) -> R<()> {
        let Some(p) = crate::privacy::info_of(&info.rtype.name) else { return Ok(()) };
        let cols: Vec<&Column> = p.marks.iter().filter_map(|(f, _)| info.column(f)).collect();
        if cols.is_empty() {
            return Ok(());
        }
        // a @unique field is made from the row's id, so that several erased
        // rows can keep it unique
        let values = |row: Option<i64>| -> Vec<Sql> {
            let mut out = Vec::new();
            for c in &cols {
                let v = if c.optional {
                    Sql::Null
                } else {
                    match (c.kind, row.filter(|_| c.unique)) {
                        ("str", Some(r)) => Sql::Text(format!("erased-{r}")),
                        ("int", Some(r)) => Sql::Int(-r),
                        ("float", Some(r)) => Sql::Real(-(r as f64)),
                        ("int" | "bool", _) => Sql::Int(0),
                        ("float", _) => Sql::Real(0.0),
                        ("date", _) => Sql::Text("0001-01-01".into()),
                        ("time", _) => Sql::Text("1970-01-01T00:00:00.000Z".into()),
                        _ => Sql::Text(String::new()),
                    }
                };
                let v = match (&v, c.private, &self.key) {
                    (Sql::Null, _, _) | (_, false, _) | (_, _, None) => v,
                    (Sql::Int(i), true, Some(k)) => Sql::Text(crate::privacy::seal(k, &i.to_string())),
                    (Sql::Real(f), true, Some(k)) => Sql::Text(crate::privacy::seal(k, &crate::fmt::format_float(*f))),
                    (Sql::Text(t), true, Some(k)) => Sql::Text(crate::privacy::seal(k, t)),
                };
                out.push(v);
            }
            out
        };
        let sets: Vec<String> = cols.iter().map(|c| format!("{} = ?", quote(&c.name))).collect();
        if !cols.iter().any(|c| c.unique && !c.optional) {
            let mut vs = values(None);
            vs.extend(params.iter().cloned());
            self.exec(&format!("UPDATE {} SET {} WHERE {wheres}", quote(&info.name), sets.join(", ")), &vs, span)?;
            return Ok(());
        }
        let key = quote(info.key.as_deref().unwrap_or("id"));
        let found = self.exec(&format!("SELECT {key} FROM {} WHERE {wheres}", quote(&info.name)), params, span)?;
        for r in found.rows {
            let Some(Sql::Int(row)) = r.into_iter().next() else { continue };
            let mut vs = values(Some(row));
            vs.push(Sql::Int(row));
            self.exec(&format!("UPDATE {} SET {} WHERE {key} = ?", quote(&info.name), sets.join(", ")), &vs, span)?;
        }
        Ok(())
    }

    /// Delete the row, or anonymise it when other rows point to it; the
    /// personal fields of the rows that point to it are anonymised too.
    fn erase_row(&self, me: V, info: &TableInfo, key: i64, span: u32) -> R<()> {
        let tables: Vec<Arc<TableInfo>> = registry().lock().unwrap().tables.iter().map(|(_, t)| t.clone()).collect();
        let mut referenced = false;
        for other in &tables {
            for c in &other.columns {
                if c.kind == "ref" && c.target.as_deref() == Some(&info.rtype.name) {
                    self.sync(me, other, span)?;
                    self.anonymise(other, &format!("{} = ?", quote(&c.name)), &[Sql::Int(key)], span)?;
                    let found = self.exec(&format!("SELECT 1 FROM {} WHERE {} = ? LIMIT 1", quote(&other.name), quote(&c.name)), &[Sql::Int(key)], span)?;
                    if !found.rows.is_empty() {
                        referenced = true;
                    }
                }
            }
        }
        let k = quote(info.key.as_deref().unwrap_or("id"));
        if referenced {
            self.anonymise(info, &format!("{k} = ?"), &[Sql::Int(key)], span)?;
            self.exec(
                &format!("INSERT OR REPLACE INTO \"{}\" (\"tbl\", \"row\", \"state\") VALUES (?, ?, ?)", crate::privacy::TABLE),
                &[Sql::Text(info.name.clone()), Sql::Int(key), Sql::Text("erased".into())],
                span,
            )?;
        } else {
            self.exec(&format!("DELETE FROM {} WHERE {k} = ?", quote(&info.name)), &[Sql::Int(key)], span)?;
            self.exec(
                &format!("DELETE FROM \"{}\" WHERE \"tbl\" = ? AND \"row\" = ?", crate::privacy::TABLE),
                &[Sql::Text(info.name.clone()), Sql::Int(key)],
                span,
            )?;
        }
        Ok(())
    }

    fn sync(&self, me: V, info: &TableInfo, span: u32) -> R<()> {
        registry().lock().unwrap().db_of_type.insert(info.rtype.name.clone(), Val::from(me));
        if self.synced.lock().unwrap().contains(&info.name) {
            return Ok(());
        }
        if self.key.is_none() && info.columns.iter().any(|c| c.private) {
            return Err(diag(
                Diagnostic::new_spanless("privacy_key").arg("table", info.name.clone()).arg("db", self.path.clone()),
                span,
            ));
        }
        let exists = self.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", &[Sql::Text(info.name.clone())], span)?;
        if exists.rows.is_empty() {
            self.exec(&create_sql(info), &[], span)?;
        } else {
            let have: Vec<String> = self
                .exec(&format!("PRAGMA table_info({})", quote(&info.name)), &[], span)?
                .rows
                .into_iter()
                .filter_map(|r| match &r[1] {
                    Sql::Text(s) => Some(s.clone()),
                    _ => None,
                })
                .collect();
            for c in &info.columns {
                if !have.contains(&c.name) {
                    self.exec(&format!("ALTER TABLE {} ADD COLUMN {}", quote(&info.name), column_sql(info, c, true)), &[], span)?;
                }
            }
        }
        self.synced.lock().unwrap().push(info.name.clone());
        if personal_table(info).is_some() {
            self.exec(
                &format!("CREATE TABLE IF NOT EXISTS \"{}\" (\"tbl\" TEXT NOT NULL, \"row\" INTEGER NOT NULL, \"state\" TEXT NOT NULL, PRIMARY KEY (\"tbl\", \"row\"))", crate::privacy::TABLE),
                &[],
                span,
            )?;
        }
        Ok(())
    }
}

/// A parameter as Python's repr writes it (for NEXO_SQL_LOG).
fn py_repr(s: &Sql) -> String {
    match s {
        Sql::Null => "None".into(),
        Sql::Int(i) => i.to_string(),
        Sql::Real(f) => crate::fmt::format_float(*f),
        Sql::Text(t) => {
            let q = if t.contains('\'') && !t.contains('"') { '"' } else { '\'' };
            let mut out = String::new();
            out.push(q);
            for c in t.chars() {
                match c {
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    c if c == q => {
                        out.push('\\');
                        out.push(c);
                    }
                    c => out.push(c),
                }
            }
            out.push(q);
            out
        }
    }
}

pub fn quote(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn sql_type(kind: &str) -> &'static str {
    match kind {
        "int" | "bool" | "ref" => "INTEGER",
        "float" => "REAL",
        _ => "TEXT",
    }
}

fn column_sql(info: &TableInfo, c: &Column, adding: bool) -> String {
    if info.key.as_deref() == Some(&c.field) {
        return format!("{} INTEGER PRIMARY KEY AUTOINCREMENT", quote(&c.name));
    }
    let mut s = format!("{} {}", quote(&c.name), if c.private { "TEXT" } else { sql_type(c.kind) });
    if !c.optional && !adding {
        s.push_str(" NOT NULL");
    }
    if c.unique && !adding {
        s.push_str(" UNIQUE");
    }
    if let Some(t) = &c.target {
        s.push_str(&format!(" REFERENCES {}", quote(&t.to_lowercase())));
    }
    s
}

pub fn create_sql(info: &TableInfo) -> String {
    let cols: Vec<String> = info.columns.iter().map(|c| column_sql(info, c, false)).collect();
    format!("CREATE TABLE {} ({})", quote(&info.name), cols.join(", "))
}

// ---------------------------------------------------------- conversions

fn to_sql(v: V, span: u32) -> R<Sql> {
    let v = v.plain();
    Ok(match v.tag {
        T_NONE => Sql::Null,
        T_BOOL => Sql::Int((v.bits != 0) as i64),
        T_INT => Sql::Int(v.bits as i64),
        T_FLOAT => Sql::Real(f64::from_bits(v.bits)),
        T_SSTR => Sql::Text(v.as_str().unwrap().to_string()),
        _ => match v.get() {
            Some(Obj::Str(s)) => Sql::Text(s.to_string()),
            Some(Obj::Decimal(d)) => Sql::Text(d.fixed()),
            Some(Obj::Money(d, c)) => Sql::Text(format!("{} {c}", d.py_str())),
            Some(Obj::Variant(var)) => Sql::Text(var.name.clone()),
            Some(Obj::Record(r)) => {
                let key = table_of(r.ty).and_then(|t| t.key_index()).map(|i| r.fields.borrow()[i].v());
                match key.and_then(|k| k.as_int()) {
                    Some(id) => Sql::Int(id),
                    None => return Err(diag(Diagnostic::new_spanless("db_unsaved").arg("type", r.ty.name.clone()), span)),
                }
            }
            _ => {
                if let Some(d) = v.native::<crate::time::Date>() {
                    Sql::Text(d.repr())
                } else if let Some(t) = v.native::<crate::time::Time>() {
                    Sql::Text(t.iso_utc())
                } else {
                    return Err(diag(Diagnostic::new_spanless("db_value").arg("vtype", type_name(v)), span));
                }
            }
        },
    })
}

fn sql_value(s: &Sql) -> Val {
    match s {
        Sql::Null => Val::none(),
        Sql::Int(i) => Val::int(*i),
        Sql::Real(f) => Val::float(*f),
        Sql::Text(t) => Val::str(t.clone()),
    }
}

type Cache = HashMap<(String, i64), Val>;

fn from_sql(db: &Database, me: V, raw: &Sql, c: &Column, cache: &mut Cache, span: u32) -> R<Val> {
    if matches!(raw, Sql::Null) {
        return Ok(Val::none());
    }
    if c.private {
        let text = match raw {
            Sql::Text(t) => t.clone(),
            _ => String::new(),
        };
        let table = table_of_column(c);
        let plain = crate::privacy::unseal(db.key.as_ref().unwrap(), &text, &table, span)?;
        let raw = match c.kind {
            "int" | "bool" | "ref" => plain.parse::<i64>().map(Sql::Int).unwrap_or(Sql::Text(plain)),
            "float" => plain.parse::<f64>().map(Sql::Real).unwrap_or(Sql::Text(plain)),
            _ => Sql::Text(plain),
        };
        let open = Column { private: false, ..c.clone() };
        return from_sql(db, me, &raw, &open, cache, span);
    }
    let text = || match raw {
        Sql::Text(t) => t.clone(),
        Sql::Int(i) => i.to_string(),
        Sql::Real(f) => crate::fmt::format_float(*f),
        Sql::Null => String::new(),
    };
    Ok(match c.kind {
        "int" => match raw {
            Sql::Int(i) => Val::int(*i),
            Sql::Real(f) => Val::int(*f as i64),
            _ => Val::int(text().parse().unwrap_or(0)),
        },
        "float" => match raw {
            Sql::Int(i) => Val::float(*i as f64),
            Sql::Real(f) => Val::float(*f),
            _ => Val::float(text().parse().unwrap_or(f64::NAN)),
        },
        "bool" => Val::bool(matches!(raw, Sql::Int(i) if *i != 0)),
        "str" => Val::str(text()),
        "decimal" => match crate::dec::Dec::parse(&text()) {
            Some(d) => decimal(d),
            None => Val::none(),
        },
        "money" => {
            let t = text();
            let (amount, cur) = t.split_once(' ').unwrap_or((&t, "EUR"));
            match crate::dec::Dec::parse(amount) {
                Some(d) => crate::num::money_of(d, crate::num::intern(cur), span)?,
                None => Val::none(),
            }
        }
        "date" => crate::time::parse_date(&text()).unwrap_or(Val::none()),
        "time" => crate::time::parse_time(&text()).unwrap_or(Val::none()),
        "variant" => {
            let t = text();
            match c.variants.and_then(|e| e.variants.iter().find(|v| v.name == t)) {
                Some(v) => Val::from(v.value.get()),
                None => Val::none(),
            }
        }
        "ref" => {
            let target = c.target.clone().unwrap_or_default();
            let Sql::Int(id) = raw else { return Ok(Val::none()) };
            let k = (target.clone(), *id);
            if let Some(v) = cache.get(&k) {
                return Ok(v.clone());
            }
            let Some(info) = table(&target) else { return Ok(Val::none()) };
            let key = quote(info.key.as_deref().unwrap_or("id"));
            let rows = select(db, me, &info, &[format!("{key} = ?")], &[Sql::Int(*id)], &[], None, cache, span)?;
            let v = rows.into_iter().next().unwrap_or(Val::none());
            cache.insert(k, v.clone());
            v
        }
        _ => sql_value(raw),
    })
}

#[allow(clippy::too_many_arguments)]
fn select(db: &Database, me: V, info: &TableInfo, wheres: &[String], params: &[Sql], order: &[String], limit: Option<i64>, cache: &mut Cache, span: u32) -> R<Vec<Val>> {
    db.sync(me, info, span)?;
    let mut all = db.guard(me, info, span)?;
    all.extend(wheres.iter().cloned());
    let wheres = &all;
    let cols: Vec<String> = info.columns.iter().map(|c| quote(&c.name)).collect();
    let mut sql = format!("SELECT {} FROM {}", cols.join(", "), quote(&info.name));
    if !wheres.is_empty() {
        let w: Vec<String> = wheres.iter().map(|w| format!("({w})")).collect();
        sql.push_str(&format!(" WHERE {}", w.join(" AND ")));
    }
    let mut keys: Vec<String> = order.to_vec();
    if let Some(k) = &info.key {
        keys.push(quote(k));
    }
    if !keys.is_empty() {
        sql.push_str(&format!(" ORDER BY {}", keys.join(", ")));
    }
    if let Some(n) = limit {
        sql.push_str(&format!(" LIMIT {n}"));
    }
    let rows = db.exec(&sql, params, span)?;
    let mut out = Vec::with_capacity(rows.rows.len());
    for row in &rows.rows {
        let mut vals = Vec::with_capacity(row.len());
        for (c, raw) in info.columns.iter().zip(row) {
            vals.push(from_sql(db, me, raw, c, cache, span)?);
        }
        out.push(record(info, vals));
    }
    Ok(out)
}

/// A record of a table type from the values of its columns.
fn record(info: &TableInfo, vals: Vec<Val>) -> Val {
    Val::new(Obj::Record(Record {
        ty: info.rtype,
        fields: Mut::new(vals.into()),
        birth: std::cell::Cell::new(crate::state::stamp()),
    }))
}

fn count(db: &Database, me: V, info: &TableInfo, wheres: &[String], params: &[Sql], span: u32) -> R<i64> {
    db.sync(me, info, span)?;
    let mut all = db.guard(me, info, span)?;
    all.extend(wheres.iter().cloned());
    let wheres = &all;
    let mut sql = format!("SELECT COUNT(*) FROM {}", quote(&info.name));
    if !wheres.is_empty() {
        let w: Vec<String> = wheres.iter().map(|w| format!("({w})")).collect();
        sql.push_str(&format!(" WHERE {}", w.join(" AND ")));
    }
    let rows = db.exec(&sql, params, span)?;
    Ok(match rows.rows.first().and_then(|r| r.first()) {
        Some(Sql::Int(n)) => *n,
        _ => 0,
    })
}

// --------------------------------------------------------------- values

const DB_MEMBERS: &[&str] = &["sql"];

impl Native for Database {
    fn type_name(&self) -> &str {
        "database"
    }
    fn repr(&self) -> String {
        format!("database({})", self.path)
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        if let Some(info) = table(name) {
            registry().lock().unwrap().db_of_type.insert(name.to_string(), Val::from(me));
            return Ok(query(me, info, Vec::new(), Vec::new(), Vec::new(), None));
        }
        if name == "sql" {
            return Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "sql")));
        }
        let mut names: Vec<String> = registry().lock().unwrap().tables.iter().map(|(n, _)| n.clone()).collect();
        names.extend(DB_MEMBERS.iter().map(|s| s.to_string()));
        let refs: Vec<&str> = names.iter().map(|s| s.as_str()).collect();
        Err(missing_member("database", name, &refs, span))
    }
    fn call_method(&self, me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        let a = crate::stdlib::method_args(name, &["query"], 1, args, names, span)?;
        let Some(text) = a[0].native::<SqlText>() else {
            return Err(diag(Diagnostic::new_spanless("sql_literal"), span));
        };
        let tables: Vec<Arc<TableInfo>> = registry().lock().unwrap().tables.iter().map(|(_, t)| t.clone()).collect();
        for info in &tables {
            let owner = registry().lock().unwrap().db_of_type.get(&info.rtype.name).map(|v| v.v());
            if owner.is_none_or(|o| o.same(me)) {
                self.sync(me, info, span)?;
            }
        }
        let names: Vec<String> = tables.iter().map(|t| t.name.clone()).collect();
        let sql: Vec<String> = text.parts.iter().map(|p| quote_tables(p, &names)).collect();
        let mut params = Vec::new();
        for v in &text.values {
            params.push(to_sql(v.v(), span)?);
        }
        let rows = self.exec(&sql.join("?"), &params, span)?;
        let out: Vec<Val> = rows
            .rows
            .iter()
            .map(|r| crate::coll::map_from(rows.names.iter().zip(r).map(|(n, v)| (Val::str(n.clone()), sql_value(v))).collect()))
            .collect();
        Ok(list(out))
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// Table names in raw SQL are quoted after FROM, JOIN, INTO, UPDATE and
/// TABLE, and before a dot, so `FROM order` works although order is an
/// SQL word (D100).
fn quote_tables(sql: &str, tables: &[String]) -> String {
    let chars: Vec<char> = sql.chars().collect();
    let mut out = String::with_capacity(sql.len());
    let mut prev = String::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '\'' || c == '"' {
            let start = i;
            i += 1;
            while i < chars.len() {
                if chars[i] == c {
                    if chars.get(i + 1) == Some(&c) {
                        i += 2;
                        continue;
                    }
                    break;
                }
                i += 1;
            }
            i = (i + 1).min(chars.len());
            out.extend(&chars[start..i]);
            continue;
        }
        if c.is_ascii_alphabetic() || c == '_' {
            let start = i;
            while i < chars.len() && (chars[i].is_ascii_alphanumeric() || chars[i] == '_') {
                i += 1;
            }
            let word: String = chars[start..i].iter().collect();
            let lower = word.to_lowercase();
            let after_dot = chars.get(i) == Some(&'.');
            let kw = ["FROM", "JOIN", "INTO", "UPDATE", "TABLE"].contains(&prev.to_uppercase().as_str());
            if tables.contains(&lower) && (kw || after_dot) {
                out.push_str(&quote(&lower));
            } else {
                out.push_str(&word);
            }
            prev = word;
            continue;
        }
        out.push(c);
        i += 1;
    }
    out
}

/// The text of `db.sql("...")` with its interpolated values apart.
pub struct SqlText {
    parts: Vec<String>,
    values: Vec<Val>,
}

impl SqlText {
    fn text(&self) -> String {
        let mut out = self.parts[0].clone();
        for (v, p) in self.values.iter().zip(&self.parts[1..]) {
            out.push_str(&crate::fmt::show(v.v()));
            out.push_str(p);
        }
        out
    }
}

impl Native for SqlText {
    fn type_name(&self) -> &str {
        "str"
    }
    fn repr(&self) -> String {
        crate::fmt::quote(&self.text())
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

pub fn sql_text(parts: V, values: V, span: u32) -> R<Val> {
    let parts = crate::pipe::collect_vec(parts, span)?.iter().map(|p| p.as_str().unwrap_or("").to_string()).collect();
    let mut vals = Vec::new();
    for v in crate::pipe::collect_vec(values, span)? {
        vals.push(crate::pipe::bind(v.v())?);
    }
    Ok(native(SqlText { parts, values: vals }))
}

// ------------------------------------------------------------- queries

/// `db.T` and the steps of a pipeline translated to SQL so far.
pub struct Query {
    db: Val,
    info: Arc<TableInfo>,
    wheres: Vec<String>,
    params: Vec<Sql>,
    order: Vec<String>,
    limit: Option<i64>,
}

fn query(db: V, info: Arc<TableInfo>, wheres: Vec<String>, params: Vec<Sql>, order: Vec<String>, limit: Option<i64>) -> Val {
    let q = Arc::new(Query { db: Val::from(db), info, wheres, params, order, limit });
    let rows = q.clone();
    let handle = native(QueryRef(q));
    crate::pipe::lazy_query(
        move || {
            let items = rows.rows(0)?;
            Ok(Box::new(items.into_iter().map(Ok)) as crate::pipe::Items)
        },
        handle,
    )
}

impl Query {
    fn database(&self) -> &Database {
        self.db.v().native::<Database>().unwrap()
    }

    fn rows(&self, span: u32) -> R<Vec<Val>> {
        let mut cache = Cache::new();
        select(self.database(), self.db.v(), &self.info, &self.wheres, &self.params, &self.order, self.limit, &mut cache, span)
    }

    fn is_table(&self) -> bool {
        self.wheres.is_empty() && self.order.is_empty() && self.limit.is_none()
    }

    pub fn count(&self, span: u32) -> R<i64> {
        if self.limit.is_some() {
            return Ok(self.rows(span)?.len() as i64);
        }
        count(self.database(), self.db.v(), &self.info, &self.wheres, &self.params, span)
    }
}

/// The handle a lazy query carries (pipe::Lazy::query).
pub struct QueryRef(pub Arc<Query>);

impl Native for QueryRef {
    fn type_name(&self) -> &str {
        "list"
    }
    fn repr(&self) -> String {
        "query".into()
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// The query behind a value, if it is one.
pub fn query_of(v: V) -> Option<Arc<Query>> {
    match v.get() {
        Some(Obj::Lazy(l)) => l.query.as_ref().and_then(|q| q.v().native::<QueryRef>()).map(|q| q.0.clone()),
        _ => None,
    }
}

fn no_field(info: &TableInfo, field: &str, span: u32) -> crate::state::Fail {
    let names: Vec<&str> = info.columns.iter().map(|c| c.field.as_str()).collect();
    missing_member(&info.rtype.name, field, &names, span)
}

/// The members of `db.T` (and of a query): find, add, update, remove, all,
/// count.
pub fn query_member(q: &Query, me: V, name: &str, span: u32) -> R<Val> {
    let names: &[&str] = if q.is_table() { &["find", "add", "update", "remove", "all", "count"] } else { &["count", "sum"] };
    if names.contains(&name) {
        return Ok(Val::new(Obj::BuiltinMethod(Val::from(me), crate::num::intern(name))));
    }
    Err(missing_member("list", name, names, span))
}

pub fn query_method(q: &Query, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    let info = &q.info;
    let db = q.database();
    let me = q.db.v();
    let tname = info.rtype.name.clone();
    match name {
        "count" => {
            crate::stdlib::method_args("count", &[], 0, args, names, span)?;
            Ok(Val::int(q.count(span)?))
        }
        "sum" => {
            crate::stdlib::method_args("sum", &[], 0, args, names, span)?;
            crate::pipe::total(&q.rows(span)?, span)
        }
        "all" => {
            crate::stdlib::method_args("all", &[], 0, args, names, span)?;
            Ok(list(q.rows(span)?))
        }
        "find" => {
            if args.iter().zip(names).any(|(_, n)| n.is_none()) || args.is_empty() {
                if args.is_empty() {
                    return Err(crate::stdlib::bad_arg(&format!("{tname}.find"), msg("detail.find_needs", &[]), span));
                }
            }
            let mut wheres = Vec::new();
            let mut params = Vec::new();
            let mut shown = Vec::new();
            for (v, n) in args.iter().zip(names) {
                let Some(n) = n else {
                    return Err(diag(
                        Diagnostic::new_spanless("arg_too_many").arg("fn", "find").arg("n", "0").arg("m", "1"),
                        span,
                    ));
                };
                let Some(col) = info.column(n) else { return Err(no_field(info, n, span)) };
                if v.is_none() {
                    wheres.push(format!("{} IS NULL", quote(&col.name)));
                } else {
                    wheres.push(format!("{} = ?", quote(&col.name)));
                    params.push(db.cell(*v, col, span)?);
                }
                shown.push(format!("{} = {}", col.field, repr(v.plain())));
            }
            let rows = select(db, me, info, &wheres, &params, &[], Some(1), &mut Cache::new(), span)?;
            match rows.into_iter().next() {
                Some(r) => Ok(r),
                None => Err(failure_as("NotFound", "NotFound.row", span, &[("type", tname), ("conditions", shown.join(" and "))])),
            }
        }
        "add" => {
            let positional: Vec<V> = args.iter().zip(names).filter(|(_, n)| n.is_none()).map(|(v, _)| *v).collect();
            let rec = if !positional.is_empty() {
                let ok = positional.len() == 1
                    && args.len() == 1
                    && matches!(positional[0].get(), Some(Obj::Record(r)) if std::ptr::eq(r.ty, info.rtype));
                if !ok {
                    return Err(expects(&format!("{tname}.add"), &tname, positional[0], span));
                }
                Val::from(positional[0])
            } else {
                crate::call::construct(info.rtype, args, names, span)?
            };
            let Some(Obj::Record(r)) = rec.get() else { unreachable!() };
            db.sync(me, info, span)?;
            db.guard(me, info, span)?;
            let key_i = info.key_index();
            let fields: Vec<Val> = r.fields.borrow().to_vec();
            let mut cols = Vec::new();
            let mut vals = Vec::new();
            let mut shown = Vec::new();
            for (i, c) in info.columns.iter().enumerate() {
                shown.push((c.field.clone(), fields[i].clone()));
                if Some(i) == key_i && fields[i].is_none() {
                    continue;
                }
                cols.push(quote(&c.name));
                vals.push(db.cell(fields[i].v(), c, span)?);
            }
            let marks: Vec<&str> = cols.iter().map(|_| "?").collect();
            let sql = format!("INSERT INTO {} ({}) VALUES ({})", quote(&info.name), cols.join(", "), marks.join(", "));
            let rows = db.run(&sql, &vals).map_err(|e| db.fail(e, Some(info), Some(&shown), span))?;
            if let Some(k) = key_i {
                if fields[k].is_none() {
                    r.fields.borrow_mut()[k] = Val::int(rows.last_id);
                }
            }
            Ok(rec)
        }
        "update" => {
            let positional: Vec<V> = args.iter().zip(names).filter(|(_, n)| n.is_none()).map(|(v, _)| *v).collect();
            if positional.len() != 1 {
                return Err(diag(Diagnostic::new_spanless("arg_missing").arg("param", "record").arg("fn", "update"), span));
            }
            let (key, rec) = key_of(q, positional[0], "update", span)?;
            let rec = match rec {
                Some(r) => r,
                None => query_method(q, "find", &[V::int(key)], &[info.key.as_deref()], span)?,
            };
            for (v, n) in args.iter().zip(names) {
                if let Some(n) = n {
                    crate::call::set_attr(rec.v(), n, *v, span, span)?;
                }
            }
            let Some(Obj::Record(r)) = rec.get() else { unreachable!() };
            db.sync(me, info, span)?;
            let key_i = info.key_index();
            let fields: Vec<Val> = r.fields.borrow().to_vec();
            let mut sets = Vec::new();
            let mut vals = Vec::new();
            let mut shown = Vec::new();
            for (i, c) in info.columns.iter().enumerate() {
                shown.push((c.field.clone(), fields[i].clone()));
                if Some(i) == key_i {
                    continue;
                }
                sets.push(format!("{} = ?", quote(&c.name)));
                vals.push(db.cell(fields[i].v(), c, span)?);
            }
            vals.push(Sql::Int(key));
            let sql = format!("UPDATE {} SET {} WHERE {} = ?", quote(&info.name), sets.join(", "), quote(info.key.as_deref().unwrap()));
            db.run(&sql, &vals).map_err(|e| db.fail(e, Some(info), Some(&shown), span))?;
            Ok(rec)
        }
        "remove" => {
            let a = crate::stdlib::method_args("remove", &["record"], 1, args, names, span)?;
            let (key, _) = key_of(q, a[0], "remove", span)?;
            db.sync(me, info, span)?;
            let sql = format!("DELETE FROM {} WHERE {} = ?", quote(&info.name), quote(info.key.as_deref().unwrap()));
            db.exec(&sql, &[Sql::Int(key)], span)?;
            Ok(Val::none())
        }
        _ => Err(missing_member("list", name, &[], span)),
    }
}

fn key_of(q: &Query, target: V, fname: &str, span: u32) -> R<(i64, Option<Val>)> {
    let info = &q.info;
    let Some(ki) = info.key_index() else {
        return Err(diag(Diagnostic::new_spanless("table_no_id").arg("type", info.rtype.name.clone()), span));
    };
    if let Some(Obj::Record(r)) = target.get() {
        if std::ptr::eq(r.ty, info.rtype) {
            return match r.fields.borrow()[ki].as_int() {
                Some(k) => Ok((k, Some(Val::from(target)))),
                None => Err(diag(Diagnostic::new_spanless("db_unsaved").arg("type", info.rtype.name.clone()), span)),
            };
        }
    }
    if let Some(k) = target.as_int() {
        return Ok((k, None));
    }
    Err(expects(&format!("{}.{fname}", info.rtype.name), &info.rtype.name, target, span))
}

/// `customer.orders` for a `many Order` field: a query (D100).
pub fn relation(rec: V, name: &str, span: u32) -> Option<R<Val>> {
    let Some(Obj::Record(r)) = rec.get() else { return None };
    let info = table_of(r.ty)?;
    let (_, target_name) = info.many.iter().find(|(f, _)| f == name)?.clone();
    Some((|| {
        let Some(target) = table(&target_name) else {
            return Err(diag(Diagnostic::new_spanless("unknown_type").arg("name", target_name.clone()), span));
        };
        let Some(col) = target.columns.iter().find(|c| c.kind == "ref" && c.target.as_deref() == Some(&r.ty.name)).cloned() else {
            return Err(diag(
                Diagnostic::new_spanless("table_many")
                    .arg("type", r.ty.name.clone())
                    .arg("field", name)
                    .arg("target", target.rtype.name.clone()),
                span,
            ));
        };
        let db = {
            let reg = registry().lock().unwrap();
            reg.db_of_type.get(&target.rtype.name).or_else(|| reg.db_of_type.get(&r.ty.name)).cloned()
        };
        let key = info.key_index().and_then(|k| r.fields.borrow()[k].as_int());
        let (Some(db), Some(key)) = (db, key) else {
            return Err(diag(Diagnostic::new_spanless("db_unsaved").arg("type", r.ty.name.clone()), span));
        };
        Ok(query(db.v(), target, vec![format!("{} = ?", quote(&col.name))], vec![Sql::Int(key)], Vec::new(), None))
    })())
}

// ------------------------------------------------- pipelines in SQL (D100)

/// A where plan, as the compiler writes it: `&(a,b)`, `|(a,b)`, `!(a)`,
/// `=(i,j)` (with `=`, `<>`, `<`, `>`, `<=`, `>=`, `in`), `?(i)` (a bare
/// boolean column); leaves are numbered. `names[i]` is the name of a leaf
/// that is a bare name, and `mentions[i]` the names a leaf uses.
pub struct Plan {
    pub tree: Node,
    pub names: Vec<Option<String>>,
    pub mentions: Vec<Vec<String>>,
}

pub enum Node {
    And(Box<Node>, Box<Node>),
    Or(Box<Node>, Box<Node>),
    Not(Box<Node>),
    Cmp(String, usize, usize),
    Truth(usize),
}

pub fn parse_plan(text: &str) -> Plan {
    let (tree, rest) = text.split_once('\u{1}').unwrap_or((text, ""));
    let mut names = Vec::new();
    let mut mentions = Vec::new();
    for leaf in rest.split('\u{2}').filter(|l| !l.is_empty()) {
        let (n, m) = leaf.split_once('\u{3}').unwrap_or((leaf, ""));
        names.push(if n.is_empty() { None } else { Some(n.to_string()) });
        mentions.push(m.split(',').filter(|x| !x.is_empty()).map(|x| x.to_string()).collect());
    }
    let b: Vec<char> = tree.chars().collect();
    let mut i = 0;
    fn node(b: &[char], i: &mut usize) -> Node {
        let mut op = String::new();
        while b[*i] != '(' {
            op.push(b[*i]);
            *i += 1;
        }
        *i += 1;
        let num = |b: &[char], i: &mut usize| -> usize {
            let mut n = 0;
            while b[*i].is_ascii_digit() {
                n = n * 10 + b[*i].to_digit(10).unwrap() as usize;
                *i += 1;
            }
            n
        };
        let r = match op.as_str() {
            "&" | "|" => {
                let a = node(b, i);
                *i += 1;
                let c = node(b, i);
                if op == "&" {
                    Node::And(Box::new(a), Box::new(c))
                } else {
                    Node::Or(Box::new(a), Box::new(c))
                }
            }
            "!" => Node::Not(Box::new(node(b, i))),
            "?" => Node::Truth(num(b, i)),
            _ => {
                let x = num(b, i);
                *i += 1;
                let y = num(b, i);
                Node::Cmp(op, x, y)
            }
        };
        *i += 1;
        r
    }
    Plan { tree: node(&b, &mut i), names, mentions }
}

/// The SQL of a where plan for a query, or None to run it in memory.
/// `value(i)` evaluates leaf i outside the row.
fn sql_where(q: &Query, plan: &Plan, value: &mut dyn FnMut(usize) -> R<Val>, span: u32) -> R<Option<(String, Vec<Sql>)>> {
    let info = &q.info;
    // an encrypted column cannot be compared in SQL (D116)
    let column = |i: usize| plan.names[i].as_ref().and_then(|n| info.column(n)).filter(|c| !c.private);
    let usable = |i: usize| {
        if column(i).is_some() {
            return true;
        }
        !plan.mentions[i].iter().any(|n| n == "it" || info.column(n).is_some())
    };
    let mut params = Vec::new();
    let mut values: HashMap<usize, Val> = HashMap::new();
    fn fits(col: &Column, v: V) -> bool {
        if v.is_none() {
            return true;
        }
        match col.kind {
            "int" => v.tag == T_INT,
            "float" => v.tag == T_INT || v.tag == T_FLOAT,
            "bool" => v.tag == T_BOOL,
            "str" => v.as_str().is_some(),
            "date" | "time" => type_name(v) == col.kind,
            "variant" => matches!(v.get(), Some(Obj::Variant(_))),
            "ref" => matches!(v.get(), Some(Obj::Record(r)) if Some(&r.ty.name) == col.target.as_ref()),
            _ => false,
        }
    }
    #[allow(clippy::too_many_arguments)]
    fn build<'c>(
        n: &Node,
        q: &Query,
        column: &dyn Fn(usize) -> Option<&'c Column>,
        usable: &dyn Fn(usize) -> bool,
        value: &mut dyn FnMut(usize) -> R<Val>,
        values: &mut HashMap<usize, Val>,
        params: &mut Vec<Sql>,
        span: u32,
    ) -> R<Option<String>> {
        let _ = q;
        Ok(match n {
            Node::And(a, b) | Node::Or(a, b) => {
                let x = build(a, q, column, usable, value, values, params, span)?;
                let y = build(b, q, column, usable, value, values, params, span)?;
                match (x, y) {
                    (Some(x), Some(y)) => Some(format!("({x} {} {y})", if matches!(n, Node::And(..)) { "AND" } else { "OR" })),
                    _ => None,
                }
            }
            Node::Not(a) => build(a, q, column, usable, value, values, params, span)?.map(|x| format!("(NOT {x})")),
            Node::Truth(i) => match column(*i) {
                Some(c) if c.kind == "bool" => Some(format!("{} = 1", quote(&c.name))),
                _ => None,
            },
            Node::Cmp(op, li, ri) => {
                if !usable(*li) || !usable(*ri) {
                    return Ok(None);
                }
                let (lc, rc) = (column(*li), column(*ri));
                let mut get = |i: usize| -> R<Val> {
                    if let Some(v) = values.get(&i) {
                        return Ok(v.clone());
                    }
                    let v = value(i)?.plain();
                    values.insert(i, v.clone());
                    Ok(v)
                };
                if op == "in" {
                    let (None, Some(c)) = (lc, rc) else { return Ok(None) };
                    if c.kind != "str" {
                        return Ok(None);
                    }
                    let v = get(*li)?;
                    let Some(s) = v.as_str() else { return Ok(None) };
                    params.push(Sql::Text(s.to_string()));
                    return Ok(Some(format!("instr({}, ?) > 0", quote(&c.name))));
                }
                if let (Some(a), Some(b)) = (lc, rc) {
                    if a.kind != b.kind || ["decimal", "money", "variant", "ref"].contains(&a.kind) || (a.kind == "bool" && op != "=" && op != "<>") {
                        return Ok(None);
                    }
                    return Ok(Some(format!("{} {op} {}", quote(&a.name), quote(&b.name))));
                }
                let (col, other, flipped) = match (lc, rc) {
                    (Some(c), _) => (c, *ri, false),
                    (_, Some(c)) => (c, *li, true),
                    _ => return Ok(None),
                };
                if col.kind == "decimal" || col.kind == "money" {
                    return Ok(None);
                }
                let v = get(other)?;
                if !fits(col, v.v()) {
                    return Ok(None);
                }
                if v.is_none() {
                    return Ok(match op.as_str() {
                        "=" => Some(format!("{} IS NULL", quote(&col.name))),
                        "<>" => Some(format!("{} IS NOT NULL", quote(&col.name))),
                        _ => None,
                    });
                }
                if ["bool", "variant", "ref"].contains(&col.kind) && op != "=" && op != "<>" {
                    return Ok(None);
                }
                params.push(to_sql(v.v(), span)?);
                let sym = if flipped {
                    match op.as_str() {
                        "<" => ">",
                        ">" => "<",
                        "<=" => ">=",
                        ">=" => "<=",
                        o => o,
                    }
                } else {
                    op.as_str()
                };
                Some(format!("{} {sym} ?", quote(&col.name)))
            }
        })
    }
    let sql = build(&plan.tree, q, &column, &usable, value, &mut values, &mut params, span)?;
    Ok(sql.map(|s| (s, params)))
}

/// `where` on a query: one more condition in SQL, or None (in memory).
pub fn pipe_where(src: V, plan: &Plan, value: &mut dyn FnMut(usize) -> R<Val>, span: u32) -> R<Option<Val>> {
    let Some(q) = query_of(src) else { return Ok(None) };
    if !q.order.is_empty() || q.limit.is_some() {
        return Ok(None);
    }
    let Some((sql, params)) = sql_where(&q, plan, value, span)? else { return Ok(None) };
    let mut wheres = q.wheres.clone();
    wheres.push(sql);
    let mut all = q.params.clone();
    all.extend(params);
    Ok(Some(query(q.db.v(), q.info.clone(), wheres, all, q.order.clone(), q.limit)))
}

/// `sort by column [desc]` on a query.
pub fn pipe_sort(src: V, column: &str, desc: bool) -> Option<Val> {
    let q = query_of(src)?;
    if q.limit.is_some() {
        return None;
    }
    let col = q.info.column(column)?;
    if ["decimal", "money", "ref", "variant"].contains(&col.kind) || col.private {
        return None;
    }
    let key = format!("{} {}", quote(&col.name), if desc { "DESC" } else { "ASC" });
    Some(query(q.db.v(), q.info.clone(), q.wheres.clone(), q.params.clone(), vec![key], None))
}

/// `take n` on a query.
pub fn pipe_take(src: V, n: i64) -> Option<Val> {
    let q = query_of(src)?;
    let limit = q.limit.map_or(n, |l| l.min(n));
    Some(query(q.db.v(), q.info.clone(), q.wheres.clone(), q.params.clone(), q.order.clone(), Some(limit)))
}

// ---------------------------------------------------------- connect

pub fn connect(url: V, key: Option<V>, span: u32) -> R<Val> {
    let path = url.as_str().ok_or_else(|| expects("connect", "str", url, span))?.to_string();
    if let Some(scheme) = path.split_once("://").map(|x| x.0) {
        let what = format!("\"connect({})\"", crate::fmt::quote(&format!("{scheme}://...")));
        return Err(diag(Diagnostic::new_spanless("not_supported").arg("what", what), span));
    }
    if path != ":memory:" {
        crate::policy::check("read", &path, span)?;
        crate::policy::check("write", &path, span)?;
    }
    let mut data_key = None;
    if let Some(k) = key.filter(|k| !k.is_none()) {
        data_key = Some(crate::privacy::data_key(&crate::crypto::text_or_secret("connect", k, span)?));
    }
    let c = CString::new(path.clone()).unwrap_or_default();
    let mut db: *mut ffi::sqlite3 = std::ptr::null_mut();
    let flags = ffi::SQLITE_OPEN_READWRITE | ffi::SQLITE_OPEN_CREATE | ffi::SQLITE_OPEN_FULLMUTEX;
    let rc = unsafe { ffi::sqlite3_open_v2(c.as_ptr(), &mut db, flags, std::ptr::null()) };
    if rc != ffi::SQLITE_OK {
        let reason = if db.is_null() { "out of memory".to_string() } else { errmsg(db) };
        unsafe { ffi::sqlite3_close(db) };
        return Err(failure("DatabaseError", span, &[("db", path), ("reason", reason)]));
    }
    let d = Database {
        path,
        conn: Mutex::new(Conn(db)),
        synced: Mutex::new(Vec::new()),
        depth: Mutex::new(0),
        key: data_key,
        purged: Mutex::new(HashMap::new()),
    };
    d.exec("PRAGMA foreign_keys = ON", &[], span)?;
    Ok(native(d))
}

// --------------------------------------------------------- transactions

pub struct Transaction {
    db: Val,
    depth: usize,
    done: std::sync::atomic::AtomicBool,
    /// the tables in sync when it began, for a rollback
    synced: Vec<String>,
}

impl Native for Transaction {
    fn type_name(&self) -> &str {
        "transaction"
    }
    fn repr(&self) -> String {
        "transaction".into()
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

pub fn begin(db: V, span: u32) -> R<Val> {
    let Some(d) = db.native::<Database>() else { return Err(expects("transaction", "database", db, span)) };
    let mut depth = d.depth.lock().unwrap();
    let sql = if *depth == 0 { "BEGIN".to_string() } else { format!("SAVEPOINT nexo_{}", *depth) };
    d.exec(&sql, &[], span)?;
    *depth += 1;
    let synced = d.synced.lock().unwrap().clone();
    Ok(native(Transaction { db: Val::from(db), depth: *depth, done: std::sync::atomic::AtomicBool::new(false), synced }))
}

pub fn end(tx: V, commit: bool, span: u32) -> R<Val> {
    let Some(t) = tx.native::<Transaction>() else { return Ok(Val::none()) };
    if t.done.swap(true, std::sync::atomic::Ordering::SeqCst) {
        return Ok(Val::none());
    }
    let d = t.db.v().native::<Database>().unwrap();
    let level = t.depth - 1;
    *d.depth.lock().unwrap() = level;
    if !commit {
        // a table created inside the transaction is gone again
        *d.synced.lock().unwrap() = t.synced.clone();
    }
    if level == 0 {
        d.exec(if commit { "COMMIT" } else { "ROLLBACK" }, &[], span)?;
    } else if commit {
        d.exec(&format!("RELEASE nexo_{level}"), &[], span)?;
    } else {
        d.exec(&format!("ROLLBACK TO nexo_{level}"), &[], span)?;
        d.exec(&format!("RELEASE nexo_{level}"), &[], span)?;
    }
    Ok(Val::none())
}


// ------------------------------------------------- migrations (D101)

fn diff(db: &Database, infos: &[Arc<TableInfo>]) -> Result<(Vec<String>, Vec<String>, Vec<String>), String> {
    let text = |s: &Sql| match s {
        Sql::Text(t) => t.clone(),
        _ => String::new(),
    };
    let existing: Vec<String> = db
        .run(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name <> 'nexo_migrations' ORDER BY name",
            &[],
        )?
        .rows
        .iter()
        .map(|r| text(&r[0]))
        .collect();
    let (mut changes, mut notes, mut drops) = (Vec::new(), Vec::new(), Vec::new());
    for info in infos {
        if !existing.contains(&info.name) {
            changes.push(format!("{};", create_sql(info)));
            continue;
        }
        let have: Vec<(String, String)> = db
            .run(&format!("PRAGMA table_info({})", quote(&info.name)), &[])?
            .rows
            .iter()
            .map(|r| (text(&r[1]), text(&r[2])))
            .collect();
        let names: Vec<&str> = info.columns.iter().map(|c| c.name.as_str()).collect();
        let old: Vec<&str> = have.iter().map(|(n, _)| n.as_str()).collect();
        for c in &info.columns {
            if let Some((_, t)) = have.iter().find(|(n, _)| *n == c.name) {
                let is_key = info.key.as_deref() == Some(&c.field) && t.to_uppercase() == "INTEGER";
                if t.to_uppercase() != sql_type(c.kind) && !is_key {
                    notes.push(msg(
                        "info.db_type_changed",
                        &[
                            ("table", info.name.clone()),
                            ("column", c.name.clone()),
                            ("was", t.to_uppercase()),
                            ("now", sql_type(c.kind).to_string()),
                        ],
                    ));
                }
            }
        }
        if old.iter().any(|n| !names.contains(n)) {
            // SQLite drops columns by building the table again
            let new = quote(&format!("{}_new", info.name));
            let cols: Vec<String> = info.columns.iter().map(|c| column_sql(info, c, !old.contains(&c.name.as_str()))).collect();
            let kept: Vec<String> = names.iter().filter(|n| old.contains(n)).map(|n| quote(n)).collect();
            let kept = kept.join(", ");
            drops.push(format!("CREATE TABLE {new} ({});", cols.join(", ")));
            drops.push(format!("INSERT INTO {new} ({kept}) SELECT {kept} FROM {};", quote(&info.name)));
            drops.push(format!("DROP TABLE {};", quote(&info.name)));
            drops.push(format!("ALTER TABLE {new} RENAME TO {};", quote(&info.name)));
            continue;
        }
        for c in &info.columns {
            if !old.contains(&c.name.as_str()) {
                changes.push(format!("ALTER TABLE {} ADD COLUMN {};", quote(&info.name), column_sql(info, c, true)));
            }
        }
    }
    for t in &existing {
        if !infos.iter().any(|i| &i.name == t) {
            drops.push(format!("DROP TABLE {};", quote(t)));
        }
    }
    Ok((changes, notes, drops))
}

fn migration_files(folder: &std::path::Path) -> Vec<String> {
    let mut out: Vec<String> = std::fs::read_dir(folder)
        .map(|d| {
            d.filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().into_owned()))
                .filter(|n| n.len() == 8 && n.ends_with(".sql") && n[..4].bytes().all(|b| b.is_ascii_digit()))
                .collect()
        })
        .unwrap_or_default();
    out.sort();
    out
}

/// `nexo db diff` and `nexo db migrate` once the types are registered.
pub fn run_command() -> i32 {
    crate::state::flush();
    let command = std::env::var("NEXO_DB_COMMAND").unwrap_or_default();
    let file = std::env::var("NEXO_DB_FILE").unwrap_or_default();
    let allow_drop = std::env::var_os("NEXO_DB_ALLOW_DROP").is_some();
    let Ok(db_path) = std::env::var("NEXO_DB_PATH") else {
        eprintln!("Error: {} {}", msg("db_no_target", &[("file", file.clone())]), msg("fix.db_no_target", &[]));
        return 1;
    };
    let infos: Vec<Arc<TableInfo>> = registry().lock().unwrap().tables.iter().map(|(_, t)| t.clone()).collect();
    let parent = std::path::Path::new(&file).parent().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default();
    let folder = std::path::Path::new(if parent.is_empty() { "." } else { &parent }).join("migrations");
    let c = CString::new(db_path.clone()).unwrap_or_default();
    let mut raw: *mut ffi::sqlite3 = std::ptr::null_mut();
    let flags = ffi::SQLITE_OPEN_READWRITE | ffi::SQLITE_OPEN_CREATE;
    if unsafe { ffi::sqlite3_open_v2(c.as_ptr(), &mut raw, flags, std::ptr::null()) } != ffi::SQLITE_OK {
        eprintln!("Error: {}", errmsg(raw));
        return 1;
    }
    let db = Database {
        path: db_path.clone(),
        conn: Mutex::new(Conn(raw)),
        synced: Mutex::new(Vec::new()),
        depth: Mutex::new(0),
        key: None,
        purged: Mutex::new(HashMap::new()),
    };
    let fail = |e: String| {
        eprintln!(
            "Error: {}. {}",
            msg("err.DatabaseError", &[("db", db_path.clone()), ("reason", e)]),
            msg("fix.DatabaseError", &[])
        );
        1
    };
    if command == "diff" {
        let (changes, notes, drops) = match diff(&db, &infos) {
            Ok(x) => x,
            Err(e) => return fail(e),
        };
        if changes.is_empty() && drops.is_empty() && notes.is_empty() {
            println!("{}", msg("info.db_diff_none", &[("db", db_path.clone()), ("file", file.clone())]));
            return 0;
        }
        let files = migration_files(&folder);
        let number = files.last().map(|f| f[..4].parse::<u32>().unwrap_or(0) + 1).unwrap_or(1);
        let path = folder.join(format!("{number:04}.sql")).to_string_lossy().into_owned();
        let mut lines = vec![format!(
            "-- {}",
            msg("info.db_migration_header", &[("n", format!("{number:04}")), ("db", db_path.clone()), ("file", file.clone())])
        )];
        lines.extend(changes);
        lines.extend(notes.iter().map(|n| format!("-- {n}")));
        if !drops.is_empty() {
            lines.push(format!("-- {}", msg("info.db_migration_drops", &[])));
            lines.extend(drops);
        }
        let text = lines.join("\n") + "\n";
        let _ = std::fs::create_dir_all(&folder);
        if let Err(e) = std::fs::write(&path, &text) {
            eprintln!("Error: {e}");
            return 1;
        }
        println!("{}", msg("info.db_diff_written", &[("path", path)]));
        print!("{text}");
        return 0;
    }
    if let Err(e) = db.run("CREATE TABLE IF NOT EXISTS nexo_migrations (name TEXT PRIMARY KEY)", &[]) {
        return fail(e);
    }
    let done: Vec<String> = match db.run("SELECT name FROM nexo_migrations", &[]) {
        Ok(r) => r
            .rows
            .iter()
            .map(|x| match &x[0] {
                Sql::Text(t) => t.clone(),
                _ => String::new(),
            })
            .collect(),
        Err(e) => return fail(e),
    };
    let mut applied = 0;
    for name in migration_files(&folder) {
        if done.contains(&name) {
            continue;
        }
        let path = folder.join(&name).to_string_lossy().into_owned();
        let text = std::fs::read_to_string(&path).unwrap_or_default();
        let body: Vec<&str> = text.lines().filter(|l| !l.starts_with("--")).collect();
        let statements: Vec<String> = body.join("\n").split(';').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();
        let drops = statements.iter().any(|s| s.to_uppercase().starts_with("DROP ") || s.to_uppercase().contains(" DROP COLUMN "));
        if drops && !allow_drop {
            eprintln!("Error: {} {}", msg("db_needs_drop", &[("path", path.clone())]), msg("fix.db_needs_drop", &[]));
            return 1;
        }
        let result = (|| {
            db.run("BEGIN", &[])?;
            for s in &statements {
                db.run(s, &[])?;
            }
            db.run("INSERT INTO nexo_migrations (name) VALUES (?)", &[Sql::Text(name.clone())])?;
            db.run("COMMIT", &[])
        })();
        if let Err(e) = result {
            let _ = db.run("ROLLBACK", &[]);
            return fail(e);
        }
        println!("{}", msg("info.db_migrated", &[("path", path)]));
        applied += 1;
    }
    if applied == 0 {
        println!("{}", msg("info.db_up_to_date", &[("db", db_path.clone())]));
    }
    0
}

/// The table of a column (for messages).
fn table_of_column(c: &Column) -> String {
    registry()
        .lock()
        .unwrap()
        .tables
        .iter()
        .find(|(_, t)| t.columns.iter().any(|x| std::ptr::eq(x, c) || (x.name == c.name && x.field == c.field)))
        .map(|(_, t)| t.name.clone())
        .unwrap_or_default()
}

/// (type, table) of every @table type, for `nexo privacy report`.
pub fn table_names() -> Vec<(String, String)> {
    registry().lock().unwrap().tables.iter().map(|(n, t)| (n.clone(), t.name.clone())).collect()
}

// ------------------------------------------------- privacy.export/erase

/// (database, table info, key) of a record saved in a table.
fn saved_row(fname: &str, rec: V, span: u32) -> R<(Val, Arc<TableInfo>, i64)> {
    let Some(Obj::Record(r)) = rec.get() else { return Err(expects(&format!("privacy.{fname}"), "@table", rec, span)) };
    let Some(info) = table_of(r.ty) else { return Err(expects(&format!("privacy.{fname}"), "@table", rec, span)) };
    let Some(ki) = info.key_index() else { return Err(expects(&format!("privacy.{fname}"), "@table", rec, span)) };
    let key = r.fields.borrow()[ki].as_int();
    let db = registry().lock().unwrap().db_of_type.get(&r.ty.name).cloned();
    match (key, db) {
        (Some(k), Some(db)) => Ok((db, info, k)),
        _ => Err(diag(Diagnostic::new_spanless("db_unsaved").arg("type", r.ty.name.clone()), span)),
    }
}

/// A row as a map whose `ref` fields are the ids they point to.
fn plain_row(rec: V) -> Val {
    let Some(Obj::Record(r)) = rec.get() else { return Val::from(rec) };
    let fields = r.fields.borrow();
    let mut pairs = Vec::new();
    for (i, f) in r.ty.fields.iter().enumerate() {
        let v = fields[i].v();
        let v = match v.get() {
            Some(Obj::Record(t)) => match table_of(t.ty).and_then(|ti| ti.key_index()) {
                Some(k) => Val::from(t.fields.borrow()[k].v()),
                None => Val::from(v),
            },
            _ => Val::from(v),
        };
        pairs.push((Val::str(f.name.clone()), v));
    }
    crate::coll::map_from(pairs)
}

/// privacy.export(person): a JSON file with all their data, including the
/// rows of its `many` fields; it returns the file's path.
pub fn privacy_export(rec: V, span: u32) -> R<Val> {
    let (db, info, key) = saved_row("export", rec, span)?;
    let d = db.v().native::<Database>().unwrap();
    let k = quote(info.key.as_deref().unwrap_or("id"));
    let rows = select(d, db.v(), &info, &[format!("{k} = ?")], &[Sql::Int(key)], &[], Some(1), &mut Cache::new(), span)?;
    let Some(row) = rows.into_iter().next() else {
        return Err(failure_as(
            "NotFound",
            "NotFound.row",
            span,
            &[("type", info.rtype.name.clone()), ("conditions", format!("{} = {key}", info.key.clone().unwrap_or_default()))],
        ));
    };
    let data = plain_row(row.v());
    for (field, target) in &info.many {
        let Some(t) = table(target) else { continue };
        let Some(col) = t.columns.iter().find(|c| c.kind == "ref" && c.target.as_deref() == Some(&info.rtype.name)) else { continue };
        let related = select(d, db.v(), &t, &[format!("{} = ?", quote(&col.name))], &[Sql::Int(key)], &[], None, &mut Cache::new(), span)?;
        let items: Vec<Val> = related.iter().map(|r| plain_row(r.v())).collect();
        if let Some(Obj::Map(m)) = data.get() {
            m.borrow_mut().insert(Key::Str(field.clone()), Val::str(field.clone()), list(items));
        }
    }
    let path = format!("{}-{key}.json", info.name);
    crate::policy::check("write", &path, span)?;
    crate::data::write_text(&path, &(crate::data::to_json(data.v(), 0) + "\n"), span)?;
    Ok(Val::str(path))
}

pub fn privacy_erase(rec: V, span: u32) -> R<Val> {
    let (db, info, key) = saved_row("erase", rec, span)?;
    let d = db.v().native::<Database>().unwrap();
    d.sync(db.v(), &info, span)?;
    d.erase_row(db.v(), &info, key, span)?;
    Ok(Val::none())
}

pub fn privacy_restrict(rec: V, span: u32) -> R<Val> {
    let (db, info, key) = saved_row("restrict", rec, span)?;
    let d = db.v().native::<Database>().unwrap();
    d.sync(db.v(), &info, span)?;
    d.exec(
        &format!("INSERT OR REPLACE INTO \"{}\" (\"tbl\", \"row\", \"state\") VALUES (?, ?, ?)", crate::privacy::TABLE),
        &[Sql::Text(info.name.clone()), Sql::Int(key), Sql::Text("restricted".into())],
        span,
    )?;
    Ok(Val::none())
}
