//! Feature flags (section 22, D115): the `flags:` block declares them and
//! `flag(name, user)` reads them. A value set with `nexo flags set` lives
//! in `.nexo/flags.json` and is read again whenever the file changes.
//! Follows phase 0's flags.py.

use std::sync::Mutex;

use nexo_diag::{suggest, Diagnostic};
use sha2::{Digest, Sha256};

use crate::cfg::{as_cfg, Cond};
use crate::dec::Dec;
use crate::json::{self, Json};
use crate::state::{diag, R};
use crate::value::*;

pub const STORE: &str = ".nexo/flags.json";
pub const AUDIT: &str = ".nexo/audit.log";

enum Kind {
    On,
    Off,
    Percent(Dec),
    Audience(Val),
}

struct Flag {
    name: String,
    kind: Kind,
}

static FLAGS: Mutex<Vec<Flag>> = Mutex::new(Vec::new());
static STORED: Mutex<(Option<std::time::SystemTime>, Option<Json>)> = Mutex::new((None, None));

pub fn release_all() {
    FLAGS.lock().unwrap().clear();
}

fn percent(v: V) -> Option<Dec> {
    match v.get() {
        Some(Obj::Quantity(d, "%")) if d.cmp(&Dec::zero()).is_ge() && d.cmp(&Dec::from_i64(100)).is_le() => Some(d.clone()),
        _ => None,
    }
}

pub fn flags(entries: V, _span: u32) -> R<Val> {
    for e in crate::cfg::items(entries) {
        let Some(entry) = as_cfg(e.v()) else { continue };
        let sp = entry.span;
        let name = match entry.key() {
            Some(n) if matches!(entry.body, crate::cfg::Body::None) && entry.values.as_ref().is_some_and(|v| v.len() == 1) => n,
            _ => return Err(diag(Diagnostic::new_spanless("flag_entry"), sp)),
        };
        if FLAGS.lock().unwrap().iter().any(|f| f.name == name) {
            return Err(diag(Diagnostic::new_spanless("duplicate_name").arg("name", name), sp));
        }
        let v = entry.value().unwrap();
        let kind = match v.v().as_str() {
            Some("on") => Kind::On,
            Some("off") => Kind::Off,
            _ => {
                if let Some(d) = percent(v.v()) {
                    Kind::Percent(d)
                } else {
                    let atoms = crate::cfg::items(v.v());
                    match atoms.as_slice() {
                        [u, w, c]
                            if u.v().as_str() == Some("users")
                                && w.v().as_str() == Some("where")
                                && c.v().native::<Cond>().is_some() =>
                        {
                            Kind::Audience(c.clone())
                        }
                        _ => return Err(diag(Diagnostic::new_spanless("flag_value").arg("name", name), sp)),
                    }
                }
            }
        };
        for (option, v) in &entry.options {
            if option == "watch" || option == "rollback" {
                return Err(diag(Diagnostic::new_spanless("not_supported").arg("what", format!("\"{option}\"")), sp));
            }
            if option != "expires" {
                return Err(diag(Diagnostic::new_spanless("flag_option").arg("option", option.clone()), sp));
            }
            if v.v().native::<crate::time::Date>().is_none() {
                return Err(crate::stdlib::expects("expires", "date", v.v(), sp));
            }
        }
        FLAGS.lock().unwrap().push(Flag { name: name.to_string(), kind });
    }
    Ok(Val::none())
}

fn stage() -> String {
    std::env::var("NEXO_STAGE").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "default".into())
}

/// The value `nexo flags set` gave a flag for this stage, if any.
fn stored(name: &str) -> Option<String> {
    let mtime = std::fs::metadata(STORE).and_then(|m| m.modified()).ok()?;
    let mut st = STORED.lock().unwrap();
    if st.0 != Some(mtime) {
        let data = std::fs::read_to_string(STORE).ok().and_then(|t| json::parse(&t)).filter(|j| j.is_obj());
        *st = (Some(mtime), data);
    }
    st.1.as_ref()?.get(&stage()).get(name).str().map(String::from)
}

/// on, off or a percentage written on the command line (or in the store).
fn text_value(text: &str) -> Option<Kind> {
    match text {
        "on" => return Some(Kind::On),
        "off" => return Some(Kind::Off),
        _ => {}
    }
    let num = text.strip_suffix('%')?;
    let digits = num.replacen('.', "", 1);
    if digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let d = Dec::parse(num)?;
    if d.cmp(&Dec::from_i64(100)).is_gt() {
        return None;
    }
    Some(Kind::Percent(d))
}

pub fn valid_text(text: &str) -> bool {
    text_value(text).is_some()
}

fn user_key(user: V) -> String {
    if let Some(Obj::Record(_)) = user.get() {
        if let Some(id) = crate::pipe::row_get(user, "id") {
            return crate::fmt::show(id.v());
        }
    }
    if user.as_str().is_some() || (user.as_int().is_some() && user.as_bool().is_none()) {
        return crate::fmt::show(user);
    }
    crate::fmt::repr(user)
}

/// 0..9999, the same for the same flag and user on every machine.
fn bucket(name: &str, key: &str) -> i64 {
    let digest = Sha256::digest(format!("{name}:{key}").as_bytes());
    let mut n: u64 = 0;
    for b in &digest[..8] {
        n = (n << 8) | *b as u64;
    }
    (n % 10000) as i64
}

pub fn flag(name: V, user: Option<V>, span: u32) -> R<Val> {
    let Some(name) = name.as_str() else {
        return Err(crate::stdlib::expects("name", "str", name, span));
    };
    let (kind, live) = {
        let flags = FLAGS.lock().unwrap();
        let Some(f) = flags.iter().find(|f| f.name == name) else {
            let names: Vec<&str> = flags.iter().map(|f| f.name.as_str()).collect();
            let d = match suggest(name, names) {
                Some(s) => Diagnostic::new_spanless("flag_unknown").fix("suggest").arg("name", name).arg("suggestion", s),
                None => Diagnostic::new_spanless("flag_unknown").arg("name", name),
            };
            return Err(diag(d, span));
        };
        let kind = match &f.kind {
            Kind::On => Kind::On,
            Kind::Off => Kind::Off,
            Kind::Percent(d) => Kind::Percent(d.clone()),
            Kind::Audience(c) => Kind::Audience(c.clone()),
        };
        (kind, stored(name).and_then(|t| text_value(&t)))
    };
    let kind = live.unwrap_or(kind);
    match kind {
        Kind::On => return Ok(Val::bool(true)),
        Kind::Off => return Ok(Val::bool(false)),
        _ => {}
    }
    let Some(user) = user.filter(|u| !u.is_none()) else {
        return Err(diag(Diagnostic::new_spanless("flag_needs_user").arg("name", name), span));
    };
    match kind {
        Kind::Percent(d) => {
            let limit = d.mul(&Dec::from_i64(100)).to_integral(crate::dec::Rounding::Down).to_i64().unwrap_or(0);
            Ok(Val::bool(bucket(name, &user_key(user)) < limit))
        }
        Kind::Audience(c) => Ok(Val::bool(crate::cfg::holds(c.v(), user, span)?)),
        _ => unreachable!(),
    }
}

// ------------------------------------------------------------ nexo flags set

fn now_text() -> String {
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let (y, m, d) = crate::time::civil_from_days(secs.div_euclid(86400));
    let s = secs.rem_euclid(86400);
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", s / 3600, s / 60 % 60, s % 60)
}

/// Append a line to .nexo/audit.log; each line holds the hash of the
/// previous one, so a changed or removed line shows.
pub fn audit(action: Vec<(&str, Json)>) {
    let prev = std::fs::read_to_string(AUDIT)
        .ok()
        .and_then(|t| t.lines().last().map(|l| hex(&Sha256::digest(l.as_bytes()))))
        .unwrap_or_default();
    let by = std::env::var("USER").ok().filter(|u| !u.is_empty()).unwrap_or_else(|| "unknown".into());
    let mut pairs = vec![("time", json::s(now_text())), ("by", json::s(by))];
    pairs.extend(action);
    pairs.push(("prev", json::s(prev)));
    let line = json::obj(pairs).compact();
    let _ = std::fs::create_dir_all(".nexo");
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(AUDIT) {
        let _ = writeln!(f, "{line}");
    }
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Write a value of `nexo flags set` (the driver has checked the name and
/// the value).
pub fn set_store(name: &str, value: &str, stage: &str) {
    let mut data = std::fs::read_to_string(STORE).ok().and_then(|t| json::parse(&t)).filter(|j| j.is_obj()).unwrap_or(Json::Obj(Vec::new()));
    if !data.get(stage).is_obj() {
        data.set(stage, Json::Obj(Vec::new()));
    }
    data.get_mut(stage).unwrap().set(name, json::s(value));
    let _ = std::fs::create_dir_all(".nexo");
    let _ = std::fs::write(STORE, data.pretty(true) + "\n");
    audit(vec![("action", json::s("flags set")), ("flag", json::s(name)), ("value", json::s(value)), ("stage", json::s(stage))]);
}
