//! Privacy and GDPR (section 23, D116): `@personal` and `@private` fields,
//! `keep` and `purpose` in a type, `privacy.export`, `erase` and
//! `restrict`, and `nexo privacy report`. Follows phase 0's privacy.py.
//!
//! `@private` columns of a table are encrypted at rest with the key the
//! database was opened with; rows past their `keep` period are erased when
//! the table is first used and again every 24 hours; restricted and erased
//! rows are left out of every query.

use std::sync::Mutex;

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Nonce};
use hmac::{Hmac, Mac};
use nexo_diag::{msg, suggest, Diagnostic};
use sha2::{Digest, Sha256};

use crate::cfg::{as_cfg, atom_text, phrase_text};
use crate::state::{diag, R};
use crate::types::{TypeDesc, TypeRef};
use crate::value::*;

pub const TABLE: &str = "nexo_privacy";
const DAY: i64 = 86400;

/// What a type says about the personal data it holds.
#[derive(Clone)]
pub struct Info {
    pub name: String,
    /// seconds
    pub keep: Option<i64>,
    pub after: Option<String>,
    pub keep_text: Option<String>,
    pub purpose: Option<String>,
    /// field -> "personal" | "private", in the order of the type
    pub marks: Vec<(String, &'static str)>,
}

static PRIVACY: Mutex<Vec<Info>> = Mutex::new(Vec::new());

/// Does the program declare personal data (section 23, rule 3)?
pub fn has_personal() -> bool {
    !PRIVACY.lock().unwrap().is_empty()
}

pub fn info_of(type_name: &str) -> Option<Info> {
    PRIVACY.lock().unwrap().iter().find(|i| i.name == type_name).cloned()
}

pub fn release_all() {
    PRIVACY.lock().unwrap().clear();
}

/// `__privacy_type(T, [keep = ..., purpose = ...], {"email": ["personal"]})`
pub fn privacy_type(t: V, settings: V, marks: V, _span: u32) -> R<Val> {
    let rt = match t.get() {
        Some(Obj::Type(TypeRef::Record(rt))) => Some(*rt),
        _ => None,
    };
    let name = match t.get() {
        Some(Obj::Type(tr)) => crate::fmt::type_ref_name(tr),
        _ => String::new(),
    };
    let mut kinds: Vec<(String, &'static str)> = Vec::new();
    if let Some(Obj::Map(m)) = marks.get() {
        for (k, v) in m.borrow().entries.iter() {
            let ms = crate::cfg::items(v.v());
            let private = ms.iter().any(|x| x.v().as_str() == Some("private"));
            kinds.push((k.as_str().unwrap_or("").to_string(), if private { "private" } else { "personal" }));
        }
    }
    let field_type = |f: &str| -> Option<String> {
        let rt = rt?;
        let fi = rt.fields.iter().find(|x| x.name == f)?;
        match fi.ty? {
            TypeDesc::Named { name, .. } => Some(name.clone()),
            _ => Some(String::new()),
        }
    };
    let mut info = Info { name: name.clone(), keep: None, after: None, keep_text: None, purpose: None, marks: kinds.clone() };
    for e in crate::cfg::items(settings) {
        let Some(entry) = as_cfg(e.v()) else { continue };
        let sp = entry.span;
        match entry.key() {
            Some("keep") => {
                let words = entry.values.as_ref().filter(|v| v.len() == 1).map(|v| v[0].clone());
                let parsed = words.as_ref().and_then(|w| {
                    if w.len() != 3 || w[1].v().as_str() != Some("after") {
                        return None;
                    }
                    let field = w[2].v().as_static_str()?;
                    let Some(Obj::Quantity(d, unit)) = w[0].v().get() else { return None };
                    let (dim, factor) = crate::num::unit_dimension(unit);
                    if dim != "time" {
                        return None;
                    }
                    Some((d.mul(&factor), field.to_string()))
                });
                let Some((secs, field)) = parsed else { return Err(diag(Diagnostic::new_spanless("keep_form"), sp)) };
                let ft = field_type(&field);
                if !matches!(ft.as_deref(), Some("date") | Some("time")) || kinds.iter().any(|(f, k)| *f == field && *k == "private") {
                    return Err(diag(Diagnostic::new_spanless("keep_form"), sp));
                }
                info.keep = Some(secs.to_integral(crate::dec::Rounding::Floor).to_i64().unwrap_or(i64::MAX / 4));
                info.after = Some(field);
                info.keep_text = Some(phrase_text(&words.unwrap()));
            }
            Some("purpose") => {
                let v = entry.value();
                let ok = entry.values.as_ref().is_some_and(|v| v.len() == 1) && entry.options.is_empty();
                match v.as_ref().and_then(|v| v.v().as_static_str()) {
                    Some(text) if ok => info.purpose = Some(text.to_string()),
                    _ => return Err(diag(Diagnostic::new_spanless("purpose_text"), sp)),
                }
            }
            key => {
                let shown = match key {
                    Some(k) => k.to_string(),
                    None => entry.words().first().map(|w| atom_text(w.v())).unwrap_or_else(|| "?".into()),
                };
                let d = match suggest(key.unwrap_or(""), ["keep", "purpose"]) {
                    Some(s) => Diagnostic::new_spanless("type_setting").fix("suggest").arg("name", shown).arg("suggestion", s),
                    None => Diagnostic::new_spanless("type_setting").arg("name", shown),
                };
                return Err(diag(d, sp));
            }
        }
    }
    let mut all = PRIVACY.lock().unwrap();
    all.retain(|i| i.name != name);
    all.push(info);
    Ok(Val::none())
}

// --------------------------------------------------- encryption at rest

pub fn data_key(text: &str) -> [u8; 32] {
    Sha256::digest(text.as_bytes()).into()
}

/// Deterministic AES-256-GCM: the same text gives the same value, so
/// `find(field: value)` still works on an encrypted column.
pub fn seal(key: &[u8; 32], text: &str) -> String {
    let mut m = <Hmac<Sha256> as Mac>::new_from_slice(key).unwrap();
    m.update(b"nexo-private:");
    m.update(text.as_bytes());
    let tag = m.finalize().into_bytes();
    let nonce = &tag[..12];
    let cipher = Aes256Gcm::new_from_slice(key).unwrap();
    let ct = cipher.encrypt(Nonce::from_slice(nonce), text.as_bytes()).unwrap_or_default();
    let mut blob = nonce.to_vec();
    blob.extend(ct);
    format!("nexo:{}", crate::crypto::b64(&blob))
}

pub fn unseal(key: &[u8; 32], text: &str, table: &str, span: u32) -> R<String> {
    let bad = || diag(Diagnostic::new_spanless("privacy_wrong_key").arg("table", table.to_string()), span);
    let blob = text.strip_prefix("nexo:").and_then(crate::crypto::unb64).ok_or_else(bad)?;
    if blob.len() < 12 {
        return Err(bad());
    }
    let cipher = Aes256Gcm::new_from_slice(key).unwrap();
    let plain = cipher.decrypt(Nonce::from_slice(&blob[..12]), &blob[12..]).map_err(|_| bad())?;
    String::from_utf8(plain).map_err(|_| bad())
}

pub fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// The cutoff of a keep period: rows whose field is before it are erased.
pub fn cutoff(info: &Info, kind: &str) -> String {
    let secs = info.keep.unwrap_or(0);
    if kind == "date" {
        let today = now_ms().div_euclid(86_400_000);
        let (y, m, d) = crate::time::civil_from_days(today - secs / DAY);
        format!("{y:04}-{m:02}-{d:02}")
    } else {
        crate::time::Time { ms: now_ms() - secs * 1000, zone: "UTC".into() }.iso_utc()
    }
}

// --------------------------------------------------- nexo privacy report

/// The record of processing activities (section 23, rule 4). `tables`
/// names the table of each @table type.
pub fn report_lines(file: &str, db: Option<&str>, readers: &[String], tables: &[(String, String)]) -> Vec<String> {
    let m = |k: &str, a: &[(&str, String)]| msg(k, a);
    let mut out = vec![m("info.privacy_title", &[("file", file.to_string())])];
    let mut all = PRIVACY.lock().unwrap().clone();
    if all.is_empty() {
        out.push(m("info.privacy_none", &[]));
    }
    all.sort_by(|a, b| a.name.cmp(&b.name));
    for p in all {
        let table = tables.iter().find(|(t, _)| *t == p.name).map(|(_, n)| n.clone());
        let place = match &table {
            Some(t) => m("info.privacy_table", &[("table", t.clone()), ("db", db.unwrap_or("?").to_string())]),
            None => m("info.privacy_memory", &[]),
        };
        out.push(String::new());
        out.push(format!("{}: {place}", p.name));
        let width = p.marks.iter().map(|(f, _)| f.chars().count()).max().unwrap_or(0);
        for (f, kind) in &p.marks {
            let pad = " ".repeat(width - f.chars().count());
            out.push(format!("  {f}{pad}  {}", m(&format!("info.privacy_{kind}"), &[])));
        }
        let missing = m("info.privacy_missing", &[]);
        out.push(format!("  {}", m("info.privacy_keep", &[("keep", p.keep_text.clone().unwrap_or(missing.clone()))])));
        out.push(format!("  {}", m("info.privacy_purpose", &[("purpose", p.purpose.clone().unwrap_or(missing))])));
        let mut who = vec![m("info.privacy_program", &[])];
        if table.is_some() {
            who.extend(readers.iter().cloned());
        }
        out.push(format!("  {}", m("info.privacy_readers", &[("readers", who.join(", "))])));
    }
    out
}

/// `nexo privacy report`: the types of the program have been registered;
/// the driver passes the file, the database and the readers.
pub fn run_command() -> i32 {
    crate::state::flush();
    let file = std::env::var("NEXO_PRIVACY_FILE").unwrap_or_default();
    let db = std::env::var("NEXO_DB_PATH").ok();
    let readers: Vec<String> =
        std::env::var("NEXO_PRIVACY_READERS").unwrap_or_default().split('\n').filter(|s| !s.is_empty()).map(String::from).collect();
    let tables = crate::db::table_names();
    for line in report_lines(&file, db.as_deref(), &readers, &tables) {
        println!("{line}");
    }
    0
}

