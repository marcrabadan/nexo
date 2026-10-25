//! nexo.infra (section 11, D118-D121): `infra` and `stage` blocks and the
//! commands `nexo plan`, `apply`, `destroy`, `drift`, `cost` and `import`.
//! Follows phase 0's infra.py line by line; both read the same table of
//! providers (nexo/stdlib/infra_providers.json). The clients of each
//! cloud's API are in infra_cloud.rs.

use std::collections::HashSet;
use std::sync::{Mutex, OnceLock};

use nexo_diag::{msg, suggest, Diagnostic};
use sha2::{Digest, Sha256};

use crate::cfg::{as_cfg, atom_text, Cfg, Cond, Ref};
use crate::dec::Dec;
use crate::json::{self, Json};
use crate::state::{diag, R};
use crate::value::*;

static PROVIDERS_TEXT: &str = include_str!("../../../nexo/stdlib/infra_providers.json");

pub fn providers() -> &'static Json {
    static P: OnceLock<Json> = OnceLock::new();
    P.get_or_init(|| json::parse(PROVIDERS_TEXT).expect("infra_providers.json"))
}

pub const CLOUDS: &[&str] = &["aws", "azure", "gcp", "k8s"];
const INFRA_KEYS: &[&str] = &["cloud", "region", "state", "budget", "data_region", "tags", "project", "namespace", "cluster", "subscription"];
const UNSUPPORTED_KEYS: &[&str] =
    &["spread", "min_available", "regions", "traffic", "failover", "flags_store", "backup", "restore_test", "read_replicas", "replicas_read"];
pub const UNKNOWN: &str = "\u{0}unknown";

fn kinds() -> Vec<&'static str> {
    providers().get("kinds").pairs().iter().map(|(k, _)| k.as_str()).collect()
}

fn kind_spec(kind: &str) -> &'static Json {
    providers().get("kinds").get(kind)
}

pub fn m(key: &str, args: &[(&str, String)]) -> String {
    msg(key, args)
}

pub fn slug(text: &str, keep: char) -> String {
    let mut out = String::new();
    let mut gap = false;
    for c in text.to_lowercase().chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            if gap && !out.is_empty() {
                out.push(keep);
            }
            gap = false;
            out.push(c);
        } else {
            gap = true;
        }
    }
    if out.is_empty() {
        "nexo".into()
    } else {
        out
    }
}

fn err(key: &'static str, span: u32) -> crate::state::Fail {
    diag(Diagnostic::new_spanless(key), span)
}

fn errd(d: Diagnostic, span: u32) -> crate::state::Fail {
    diag(d, span)
}

// ------------------------------------------------------------- the model

pub struct Resource {
    pub kind: String,
    pub name: String,
    pub span: u32,
    /// key -> (value, entry)
    pub values: Vec<(String, Val, &'static Cfg)>,
    pub env: Vec<(String, Val, &'static Cfg)>,
    pub scale: Option<(i64, i64, i64)>,
    pub lines: Vec<&'static Cfg>,
    pub stages: Vec<(String, Vec<&'static Cfg>)>,
    pub target: String,
    pub https: bool,
    pub native_type: String,
}

impl Resource {
    fn value(&self, key: &str) -> Option<&(String, Val, &'static Cfg)> {
        self.values.iter().find(|(k, _, _)| k == key)
    }
}

pub struct Output {
    pub name: String,
    pub parts: Vec<String>,
    pub secret: bool,
    pub span: u32,
}

pub struct Infra {
    pub name: String,
    pub span: u32,
    pub values: Vec<(String, Val, &'static Cfg)>,
    pub policy: Vec<&'static Cfg>,
    pub resources: Vec<Resource>,
    pub outputs: Vec<Output>,
    /// values of other infra blocks: (reference, span) (D121)
    pub peer_refs: Vec<(Vec<String>, u32)>,
}

impl Infra {
    pub fn resource(&self, name: &str) -> Option<&Resource> {
        self.resources.iter().find(|r| r.name == name)
    }
    fn value(&self, key: &str) -> Option<&(String, Val, &'static Cfg)> {
        self.values.iter().find(|(k, _, _)| k == key)
    }
}

/// The entries the program evaluated stay alive for the whole command.
static INFRAS: Mutex<Vec<&'static Infra>> = Mutex::new(Vec::new());
static STAGES: Mutex<Vec<(String, Vec<&'static Cfg>)>> = Mutex::new(Vec::new());
static KEEP: Mutex<Vec<Val>> = Mutex::new(Vec::new());

pub fn infras() -> Vec<&'static Infra> {
    INFRAS.lock().unwrap().clone()
}

fn stage_entries(stage: &str) -> Vec<&'static Cfg> {
    STAGES.lock().unwrap().iter().find(|(s, _)| s == stage).map(|(_, e)| e.clone()).unwrap_or_default()
}

pub fn release_all() {
    let infras = std::mem::take(&mut *INFRAS.lock().unwrap());
    for i in infras {
        // SAFETY: every entry is a box leaked by infra_block, and the
        // command that borrowed it has ended
        drop(unsafe { Box::from_raw(i as *const Infra as *mut Infra) });
    }
    STAGES.lock().unwrap().clear();
    let old = std::mem::take(&mut *KEEP.lock().unwrap());
    drop(old);
}

fn cfg_list(v: V) -> Vec<&'static Cfg> {
    let items = crate::cfg::items(v);
    let mut out = Vec::new();
    for i in &items {
        if let Some(c) = as_cfg(i.v()) {
            out.push(c);
        }
    }
    KEEP.lock().unwrap().extend(items);
    out
}

fn key_error(entry: &Cfg, key: &str, choices: &[String], kind: &str) -> crate::state::Fail {
    let d = match suggest(key, choices.iter().map(|s| s.as_str())) {
        Some(s) => Diagnostic::new_spanless("infra_key").fix("suggest").arg("key", key).arg("kind", kind).arg("suggestion", s),
        None => Diagnostic::new_spanless("infra_key").arg("key", key).arg("kind", kind),
    };
    errd(d, entry.span)
}

fn infra_choices() -> Vec<String> {
    let mut v: Vec<String> = INFRA_KEYS.iter().map(|s| s.to_string()).collect();
    v.extend(kinds().iter().map(|s| s.to_string()));
    v.push("policy".into());
    v.push("output".into());
    v
}

pub fn infra_block(name: V, entries: V, span: u32) -> R<Val> {
    let name = name.as_str().unwrap_or("").to_string();
    if INFRAS.lock().unwrap().iter().any(|i| i.name == name) {
        return Err(errd(Diagnostic::new_spanless("duplicate_name").arg("name", name), span));
    }
    let mut infra = Infra { name: name.clone(), span, values: Vec::new(), policy: Vec::new(), resources: Vec::new(), outputs: Vec::new(), peer_refs: Vec::new() };
    for entry in cfg_list(entries) {
        read_infra_entry(&mut infra, entry)?;
    }
    if infra.value("cloud").is_none() {
        return Err(errd(Diagnostic::new_spanless("infra_cloud").arg("infra", name), span));
    }
    check_refs(&mut infra)?;
    INFRAS.lock().unwrap().push(Box::leak(Box::new(infra)));
    Ok(Val::none())
}

pub fn stage_block(name: V, entries: V, _span: u32) -> R<Val> {
    let name = name.as_str().unwrap_or("").to_string();
    let entries = cfg_list(entries);
    for e in &entries {
        if e.key().is_none() || !matches!(e.body, crate::cfg::Body::None) {
            return Err(err("stage_entry", e.span));
        }
    }
    let mut st = STAGES.lock().unwrap();
    match st.iter_mut().find(|(s, _)| *s == name) {
        Some((_, v)) => v.extend(entries),
        None => st.push((name, entries)),
    }
    Ok(Val::none())
}

fn word_of(v: &Val) -> Option<&'static str> {
    v.v().as_str()
}

fn show_phrase(entry: &Cfg) -> String {
    entry.repr()
}

/// The value of `key = value`: one atom, or the atoms of a phrase (a list
/// of the phrases when there are several or options).
fn entry_value(entry: &Cfg) -> Val {
    let values = entry.values.as_ref().unwrap();
    if values.len() > 1 || !entry.options.is_empty() {
        let v = list(values.iter().map(|p| list(p.clone())).collect());
        KEEP.lock().unwrap().push(v.clone());
        return v;
    }
    let v = entry.value().unwrap();
    KEEP.lock().unwrap().push(v.clone());
    v
}

fn read_infra_entry(infra: &mut Infra, entry: &'static Cfg) -> R<()> {
    if let Some(key) = entry.key() {
        if UNSUPPORTED_KEYS.contains(&key) {
            return Err(errd(Diagnostic::new_spanless("not_supported").arg("what", format!("\"{key} =\"")), entry.span));
        }
        if !INFRA_KEYS.contains(&key) {
            return Err(key_error(entry, key, &infra_choices(), "infra"));
        }
        if infra.value(key).is_some() {
            return Err(errd(Diagnostic::new_spanless("duplicate_name").arg("name", key), entry.span));
        }
        infra.values.push((key.to_string(), entry_value(entry), entry));
        return Ok(());
    }
    let words = entry.words();
    let head = words.first().and_then(word_of);
    if head == Some("policy") && words.len() == 1 {
        if let crate::cfg::Body::Entries(_) = &entry.body {
            for line in entry.entries() {
                if line.values.is_some() || !matches!(line.body, crate::cfg::Body::None) {
                    return Err(errd(Diagnostic::new_spanless("policy_rule").arg("rule", show_phrase(line)), line.span));
                }
                infra.policy.push(line);
            }
            return Ok(());
        }
    }
    if head == Some("output") {
        let bad = || err("output_form", entry.span);
        if words.len() != 2 || word_of(&words[1]).is_none() || entry.values.is_none() {
            return Err(bad());
        }
        let values = entry.values.as_ref().unwrap();
        let r = if values[0].len() == 1 { values[0][0].v().native::<Ref>() } else { None };
        let secret = values.len() == 2 && values[1].len() == 1 && values[1][0].v().as_str() == Some("secret");
        let Some(r) = r.filter(|r| r.parts.len() == 2 || r.parts.len() == 3) else { return Err(bad()) };
        if values.len() > 1 && !secret {
            return Err(bad());
        }
        infra.outputs.push(Output { name: word_of(&words[1]).unwrap().to_string(), parts: r.parts.clone(), secret, span: entry.span });
        return Ok(());
    }
    match head {
        Some(h) if kinds().contains(&h) => read_resource(infra, h, entry),
        _ => {
            let shown = head.map(|s| s.to_string()).or_else(|| words.first().map(|w| atom_text(w.v()))).unwrap_or_else(|| "?".into());
            Err(key_error(entry, &shown, &infra_choices(), "infra"))
        }
    }
}

fn new_resource(kind: &str, name: &str, span: u32) -> Resource {
    Resource {
        kind: kind.to_string(),
        name: name.to_string(),
        span,
        values: Vec::new(),
        env: Vec::new(),
        scale: None,
        lines: Vec::new(),
        stages: Vec::new(),
        target: String::new(),
        https: false,
        native_type: String::new(),
    }
}

fn read_resource(infra: &mut Infra, kind: &str, entry: &'static Cfg) -> R<()> {
    let words = entry.words();
    let spec = kind_spec(kind);
    let mut r = if kind == "dns" {
        let ok = words.len() == 4 && word_of(&words[1]).is_some() && word_of(&words[2]) == Some("->") && word_of(&words[3]).is_some();
        if !ok {
            return Err(err("dns_form", entry.span));
        }
        let mut r = new_resource(kind, word_of(&words[1]).unwrap(), entry.span);
        r.target = word_of(&words[3]).unwrap().to_string();
        let extra = &entry.head[1..];
        let https = extra.len() == 1 && extra[0].len() == 1 && extra[0][0].v().as_str() == Some("https");
        if !(extra.is_empty() || https) || !entry.options.is_empty() || !matches!(entry.body, crate::cfg::Body::None) {
            return Err(err("dns_form", entry.span));
        }
        r.https = https;
        r
    } else if kind == "native" {
        if words.len() != 3 || word_of(&words[1]).is_none() || word_of(&words[2]).is_none() {
            return Err(err("native_form", entry.span));
        }
        let mut r = new_resource(kind, word_of(&words[2]).unwrap(), entry.span);
        r.native_type = word_of(&words[1]).unwrap().to_string();
        r
    } else {
        if words.len() != 2 || word_of(&words[1]).is_none() || entry.values.is_some() || entry.head.len() > 1 {
            return Err(errd(Diagnostic::new_spanless("resource_form").arg("kind", kind), entry.span));
        }
        new_resource(kind, word_of(&words[1]).unwrap(), entry.span)
    };
    if infra.resource(&r.name).is_some() {
        return Err(errd(Diagnostic::new_spanless("duplicate_name").arg("name", r.name.clone()), entry.span));
    }
    for sub in entry.entries() {
        read_resource_entry(&mut r, spec, sub)?;
    }
    infra.resources.push(r);
    Ok(())
}

fn names_of(j: &Json) -> Vec<String> {
    match j {
        Json::Obj(p) => p.iter().map(|(k, _)| k.clone()).collect(),
        Json::Arr(a) => a.iter().filter_map(|x| x.str().map(String::from)).collect(),
        _ => Vec::new(),
    }
}

fn read_resource_entry(r: &mut Resource, spec: &Json, entry: &'static Cfg) -> R<()> {
    let keys = names_of(spec.get("keys"));
    let blocks = names_of(spec.get("blocks"));
    let lines = names_of(spec.get("lines"));
    let mut choices = keys.clone();
    choices.extend(blocks.clone());
    choices.extend(lines.clone());
    if let Some(key) = entry.key() {
        if r.kind == "native" {
            r.values.push((key.to_string(), entry_value(entry), entry));
            return Ok(());
        }
        if UNSUPPORTED_KEYS.contains(&key) {
            return Err(errd(Diagnostic::new_spanless("not_supported").arg("what", format!("\"{key} =\"")), entry.span));
        }
        if !keys.iter().any(|k| k == key) {
            return Err(key_error(entry, key, &choices, &r.kind));
        }
        if r.value(key).is_some() {
            return Err(errd(Diagnostic::new_spanless("duplicate_name").arg("name", key), entry.span));
        }
        r.values.push((key.to_string(), entry_value(entry), entry));
        return Ok(());
    }
    let words = entry.words();
    let head = words.first().and_then(word_of);
    let has = |list: &Vec<String>, w: &str| list.iter().any(|x| x == w);
    let is_block = matches!(entry.body, crate::cfg::Body::Entries(_));
    if let Some(h) = head {
        if has(&lines, h) && matches!(entry.body, crate::cfg::Body::None) {
            r.lines.push(entry);
            return Ok(());
        }
        if h == "env" && has(&blocks, "env") && words.len() == 1 && is_block {
            for e in entry.entries() {
                let Some(k) = e.key() else { return Err(err("env_entry", e.span)) };
                if !matches!(e.body, crate::cfg::Body::None) || e.values.as_ref().unwrap().len() != 1 || !e.options.is_empty() {
                    return Err(err("env_entry", e.span));
                }
                let v = e.value().unwrap();
                KEEP.lock().unwrap().push(v.clone());
                r.env.push((k.to_string(), v, e));
            }
            return Ok(());
        }
        if h == "scale" && has(&blocks, "scale") {
            let rng = if words.len() == 2 {
                match words[1].v().get() {
                    Some(Obj::Range(a, b)) => Some((*a, *b)),
                    _ => None,
                }
            } else {
                None
            };
            let Some((lo, hi)) = rng.filter(|_| entry.values.is_none()) else {
                if words.len() >= 2 && word_of(&words[1]) == Some("at") {
                    return Err(errd(Diagnostic::new_spanless("not_supported").arg("what", "\"scale at\""), entry.span));
                }
                return Err(err("scale_form", entry.span));
            };
            let mut target = 70;
            for c in entry.entries() {
                let w = c.words();
                let ok = w.len() == 1 && c.values.is_none() && c.options.is_empty() && {
                    match w[0].v().native::<Cond>() {
                        Some(cond) if cond.op == ">" && cond.args[0].v().as_str() == Some("cpu") => match cond.args[1].v().get() {
                            Some(Obj::Quantity(d, "%")) => {
                                target = d.to_integral(crate::dec::Rounding::Down).to_i64().unwrap_or(70);
                                true
                            }
                            _ => false,
                        },
                        _ => false,
                    }
                };
                if !ok {
                    return Err(errd(Diagnostic::new_spanless("not_supported").arg("what", format!("\"{}\"", show_phrase(c))), c.span));
                }
            }
            r.scale = Some((lo, hi, target));
            return Ok(());
        }
        if h == "release" && has(&blocks, "release") {
            let w = if words.len() == 2 { word_of(&words[1]) } else { None };
            let Some(w) = w.filter(|w| ["rolling", "canary", "blue_green"].contains(w)) else {
                return Err(err("release_form", entry.span));
            };
            if w != "rolling" {
                return Err(errd(Diagnostic::new_spanless("not_supported").arg("what", format!("\"release {w}\"")), entry.span));
            }
            if let Some(first) = entry.entries().first() {
                return Err(errd(Diagnostic::new_spanless("not_supported").arg("what", format!("\"{}\"", show_phrase(first))), first.span));
            }
            return Ok(());
        }
        if h == "stage" && has(&blocks, "stage") && words.len() == 2 && is_block {
            let name = atom_text(words[1].v());
            let subs = entry.entries();
            for e in &subs {
                let Some(k) = e.key().filter(|_| matches!(e.body, crate::cfg::Body::None)) else { return Err(err("stage_entry", e.span)) };
                if !keys.iter().any(|x| x == k) {
                    return Err(key_error(e, k, &keys, &r.kind));
                }
            }
            match r.stages.iter_mut().find(|(s, _)| *s == name) {
                Some((_, v)) => v.extend(subs),
                None => r.stages.push((name, subs)),
            }
            return Ok(());
        }
        if h == "calls" {
            return Err(errd(Diagnostic::new_spanless("not_supported").arg("what", "\"calls\""), entry.span));
        }
    }
    let shown = head.map(|s| s.to_string()).or_else(|| words.first().map(|w| atom_text(w.v()))).unwrap_or_else(|| "?".into());
    Err(key_error(entry, &shown, &choices, &r.kind))
}

fn check_refs(infra: &mut Infra) -> R<()> {
    let mut deferred = Vec::new();
    for r in &infra.resources {
        let spec = kind_spec(&r.kind);
        for (key, value, entry) in &r.values {
            let k = spec.get("keys").get(key);
            if k.get("type").str() == Some("ref") {
                let want = k.get("kind").str().unwrap_or("");
                let ok = value.v().as_str().and_then(|n| infra.resource(n)).is_some_and(|t| t.kind == want);
                if !ok {
                    return Err(errd(
                        Diagnostic::new_spanless("infra_ref").arg("key", key.clone()).arg("kind", want).arg("name", atom_text(value.v())),
                        entry.span,
                    ));
                }
            }
        }
        if r.kind == "dns" && !infra.resource(&r.target).is_some_and(|t| t.kind == "app") {
            return Err(errd(Diagnostic::new_spanless("infra_ref").arg("key", "->").arg("kind", "app").arg("name", r.target.clone()), r.span));
        }
        for (_, value, e) in &r.env {
            if let Some(rf) = value.v().native::<Ref>() {
                if check_attr(infra, &rf.parts, e.span, None)? {
                    deferred.push((rf.parts.clone(), e.span));
                }
            }
        }
    }
    for o in &infra.outputs {
        if check_attr(infra, &o.parts, o.span, None)? {
            deferred.push((o.parts.clone(), o.span));
        }
    }
    infra.peer_refs.extend(deferred);
    Ok(())
}

/// Ok(true): a value of another infra block, maybe declared later, which
/// infra_order checks.
fn check_attr(infra: &Infra, parts: &[String], span: u32, shown: Option<&str>) -> R<bool> {
    if parts.len() == 3 && shown.is_none() && infra.resource(&parts[0]).is_none() {
        return Ok(true);
    }
    let shown = shown.map(String::from).unwrap_or_else(|| parts.join("."));
    if parts.len() != 2 {
        return Err(errd(Diagnostic::new_spanless("infra_attr").arg("ref", shown), span));
    }
    let Some(target) = infra.resource(&parts[0]) else {
        return Err(errd(Diagnostic::new_spanless("infra_attr").arg("ref", shown), span));
    };
    let attrs = names_of(kind_spec(&target.kind).get("attrs"));
    if !attrs.contains(&parts[1]) {
        let d = match suggest(&parts[1], attrs.iter().map(|s| s.as_str())) {
            Some(s) => Diagnostic::new_spanless("infra_attr_name").fix("suggest").arg("ref", shown).arg("kind", target.kind.clone()).arg("suggestion", s),
            None => Diagnostic::new_spanless("infra_attr_name").arg("ref", shown).arg("kind", target.kind.clone()),
        };
        return Err(errd(d, span));
    }
    Ok(false)
}

/// The infra blocks in the order they are applied: each one after the
/// blocks whose values it uses (D121).
pub fn infra_order(infras: &[&'static Infra]) -> R<Vec<&'static Infra>> {
    let all = self::infras();
    let by_name = |n: &str| all.iter().find(|i| i.name == n).copied();
    let mut needs: Vec<(String, Vec<String>)> = Vec::new();
    for infra in infras {
        let mut mine: Vec<String> = Vec::new();
        for (parts, span) in &infra.peer_refs {
            let shown = parts.join(".");
            let peer = by_name(&parts[0]).filter(|p| p.name != infra.name);
            let Some(peer) = peer else {
                return Err(errd(Diagnostic::new_spanless("infra_attr").arg("ref", shown), *span));
            };
            check_attr(peer, &parts[1..], *span, Some(&shown))?;
            if !mine.contains(&peer.name) {
                mine.push(peer.name.clone());
            }
        }
        needs.push((infra.name.clone(), mine));
    }
    fn visit(
        infra: &'static Infra,
        wanted: &[&'static Infra],
        all: &[&'static Infra],
        needs: &[(String, Vec<String>)],
        done: &mut Vec<String>,
        visiting: &mut Vec<String>,
        out: &mut Vec<&'static Infra>,
    ) -> R<()> {
        if done.contains(&infra.name) {
            return Ok(());
        }
        if let Some(at) = visiting.iter().position(|n| *n == infra.name) {
            let names = visiting[at..].to_vec();
            let first = all.iter().find(|i| i.name == names[0]).unwrap();
            return Err(errd(Diagnostic::new_spanless("infra_cycle").arg("names", names.join(", ")), first.span));
        }
        visiting.push(infra.name.clone());
        if let Some((_, ns)) = needs.iter().find(|(n, _)| *n == infra.name) {
            for n in ns {
                let peer = all.iter().find(|i| i.name == *n).copied().unwrap();
                visit(peer, wanted, all, needs, done, visiting, out)?;
            }
        }
        visiting.pop();
        done.push(infra.name.clone());
        if wanted.iter().any(|w| std::ptr::eq(*w, infra)) {
            out.push(infra);
        }
        Ok(())
    }
    let (mut done, mut visiting, mut out) = (Vec::new(), Vec::new(), Vec::new());
    for infra in infras {
        visit(infra, infras, &all, &needs, &mut done, &mut visiting, &mut out)?;
    }
    Ok(out)
}

// ----------------------------------------------------------------- values

fn expects(entry: &Cfg, key: &str, what: &str) -> crate::state::Fail {
    errd(Diagnostic::new_spanless("infra_value").arg("key", key).arg("what", m(&format!("detail.infra_{what}"), &[])), entry.span)
}

fn is_int(v: V) -> Option<i64> {
    if v.tag == T_INT {
        Some(v.bits as i64)
    } else {
        None
    }
}

fn is_cidr(s: &str) -> bool {
    let Some((ip, p)) = s.split_once('/') else { return false };
    let parts: Vec<&str> = ip.split('.').collect();
    parts.len() == 4
        && parts.iter().all(|x| !x.is_empty() && x.len() <= 3 && x.bytes().all(|b| b.is_ascii_digit()))
        && !p.is_empty()
        && p.len() <= 2
        && p.bytes().all(|b| b.is_ascii_digit())
}

/// A setting checked against the table of its kind, as plain data.
fn setting(entry: &Cfg, key: &str, spec: &Json, value: V) -> R<Json> {
    let t = spec.get("type").str().unwrap_or("");
    match t {
        "text" | "cidr" => {
            let Some(s) = value.as_str() else { return Err(expects(entry, key, "text")) };
            if t == "cidr" && !is_cidr(s) {
                return Err(expects(entry, key, "cidr"));
            }
            Ok(json::s(s))
        }
        "int" => match is_int(value) {
            Some(i) if i >= 0 => Ok(Json::Int(i)),
            _ => Err(expects(entry, key, "int")),
        },
        "number" => {
            let f = match value.tag {
                T_INT => Some(value.bits as i64 as f64),
                T_FLOAT => Some(f64::from_bits(value.bits)),
                _ => match value.get() {
                    Some(Obj::Decimal(d)) => Some(d.to_f64()),
                    _ => None,
                },
            };
            match f {
                Some(f) if f > 0.0 => Ok(Json::Num(f)),
                _ => Err(expects(entry, key, "number")),
            }
        }
        "bool" => match value.as_bool() {
            Some(b) if value.tag == T_BOOL => Ok(Json::Bool(b)),
            _ => Err(expects(entry, key, "bool")),
        },
        "size" => {
            if let Some(Obj::Quantity(d, unit)) = value.get() {
                let (dim, factor) = crate::num::unit_dimension(unit);
                let bytes = d.mul(&factor);
                if dim == "size" && bytes.cmp(&Dec::zero()).is_gt() {
                    let mb = bytes.div(&Dec::from_i64(1_000_000)).to_integral(crate::dec::Rounding::Down).to_i64().unwrap_or(0);
                    return Ok(Json::Int(mb));
                }
            }
            Err(expects(entry, key, "size"))
        }
        "count" => {
            if let Some(Obj::Range(a, b)) = value.get() {
                if 0 <= *a && a <= b {
                    return Ok(json::obj(vec![("min", Json::Int(*a)), ("max", Json::Int(*b))]));
                }
            }
            match is_int(value) {
                Some(i) if i >= 0 => Ok(json::obj(vec![("min", Json::Int(i)), ("max", Json::Int(i))])),
                _ => Err(expects(entry, key, "count")),
            }
        }
        "word" => {
            let words = names_of(spec.get("words"));
            match value.as_str() {
                Some(w) if words.iter().any(|x| x == w) => Ok(json::s(w)),
                _ => Err(errd(Diagnostic::new_spanless("infra_word").arg("key", key).arg("words", words.join(", ")), entry.span)),
            }
        }
        "ref" => Ok(value.as_str().map(json::s).unwrap_or(Json::Null)),
        "tags" => {
            let Some(Obj::Map(mp)) = value.get() else { return Err(expects(entry, key, "tags")) };
            let mut out = Vec::new();
            for (k, v) in mp.borrow().entries.iter() {
                match (k.as_str(), v.v().as_str()) {
                    (Some(k), Some(v)) => out.push((k.to_string(), json::s(v))),
                    _ => return Err(expects(entry, key, "tags")),
                }
            }
            Ok(Json::Obj(out))
        }
        _ => Ok(native_value(value)),
    }
}

/// Defaults, then the resource's values, then the stage's (D119).
fn effective(r: &Resource, stage: Option<&str>) -> R<Json> {
    let spec = kind_spec(&r.kind);
    let mut out = Json::Obj(Vec::new());
    for (key, k) in spec.get("keys").pairs() {
        if k.has("default") {
            let d = k.get("default").clone();
            let v = if k.get("type").str() == Some("count") { json::obj(vec![("min", d.clone()), ("max", d)]) } else { d };
            out.set(key, v);
        }
    }
    let mut entries: Vec<(String, Val, &'static Cfg)> = r.values.clone();
    let put = |entries: &mut Vec<(String, Val, &'static Cfg)>, k: &str, v: Val, e: &'static Cfg| {
        match entries.iter_mut().find(|(x, _, _)| x == k) {
            Some(slot) => *slot = (k.to_string(), v, e),
            None => entries.push((k.to_string(), v, e)),
        }
    };
    if let Some(stage) = stage {
        for e in stage_entries(stage) {
            let k = e.key().unwrap_or("");
            if spec.get("keys").has(k) {
                put(&mut entries, k, entry_value(e), e);
            }
        }
        if let Some((_, subs)) = r.stages.iter().find(|(s, _)| s == stage) {
            for e in subs {
                put(&mut entries, e.key().unwrap_or(""), entry_value(e), e);
            }
        }
    }
    for (key, v, e) in &entries {
        let value = if r.kind == "native" { native_value(v.v()) } else { setting(e, key, spec.get("keys").get(key), v.v())? };
        out.set(key, value);
    }
    for (key, k) in spec.get("keys").pairs() {
        if k.get("required").bool() == Some(true) && !out.has(key) {
            return Err(errd(
                Diagnostic::new_spanless("infra_required").arg("key", key.clone()).arg("kind", r.kind.clone()).arg("name", r.name.clone()),
                r.span,
            ));
        }
    }
    if r.kind == "app" {
        if let Some(image) = IMAGE.lock().unwrap().clone() {
            if out.get("image").str().is_some_and(|i| image_name(i) == image_name(&image)) {
                out.set("image", json::s(image));
            }
        }
    }
    Ok(out)
}

/// apply(image:) in a pipeline step (D122).
pub static IMAGE: Mutex<Option<String>> = Mutex::new(None);

/// `registry:5000/shop-api:2.4` -> `registry:5000/shop-api`.
fn image_name(image: &str) -> &str {
    match image.rsplit_once(':') {
        Some((head, tag)) if !tag.contains('/') => head,
        _ => image,
    }
}

/// The settings of the infra block itself, with its stage's.
fn infra_settings(infra: &Infra, stage: Option<&str>) -> R<Vec<(String, Val, &'static Cfg)>> {
    let mut values: Vec<(String, Val, &'static Cfg)> = infra.values.clone();
    if let Some(stage) = stage {
        for e in stage_entries(stage) {
            let k = e.key().unwrap_or("");
            if INFRA_KEYS.contains(&k) {
                let v = entry_value(e);
                match values.iter_mut().find(|(x, _, _)| x == k) {
                    Some(slot) => *slot = (k.to_string(), v, e),
                    None => values.push((k.to_string(), v, e)),
                }
            }
        }
    }
    let cloud = values.iter().find(|(k, _, _)| k == "cloud").unwrap();
    if !cloud.1.v().as_str().is_some_and(|c| CLOUDS.contains(&c)) {
        let span = infra.value("cloud").unwrap().2.span;
        return Err(errd(Diagnostic::new_spanless("infra_word").arg("key", "cloud").arg("words", CLOUDS.join(", ")), span));
    }
    for key in ["region", "data_region", "project", "namespace", "cluster", "subscription"] {
        if let Some((_, v, e)) = values.iter().find(|(k, _, _)| k == key) {
            if v.v().as_str().is_none() {
                return Err(expects(e, key, "text"));
            }
        }
    }
    if let Some((_, v, e)) = values.iter().find(|(k, _, _)| k == "tags") {
        setting(e, "tags", &json::obj(vec![("type", json::s("tags"))]), v.v())?;
    }
    Ok(values)
}

pub fn text_value(values: &[(String, Val, &'static Cfg)], key: &str) -> Option<String> {
    values.iter().find(|(k, _, _)| k == key).and_then(|(_, v, _)| v.v().as_str().map(String::from))
}

// -------------------------------------------------------------- templates

pub fn lookup<'a>(path: &str, root: &'a Json) -> Option<&'a Json> {
    let mut v = root;
    for part in path.split('.') {
        v = match v {
            Json::Obj(_) => {
                if !v.has(part) {
                    return None;
                }
                v.get(part)
            }
            Json::Arr(a) if !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()) => a.get(part.parse::<usize>().ok()?)?,
            _ => return None,
        };
    }
    Some(v)
}

pub fn text_of(v: &Json) -> String {
    match v {
        Json::Bool(b) => if *b { "true" } else { "false" }.into(),
        Json::Num(f) if f.fract() == 0.0 && f.is_finite() => format!("{}", *f as i64),
        Json::Num(f) => json::py_float(*f),
        Json::Int(i) => i.to_string(),
        Json::Str(s) => s.clone(),
        Json::Null => "None".into(),
        _ => v.compact(),
    }
}

pub fn is_unknown(v: &Json) -> bool {
    matches!(v, Json::Str(s) if s.starts_with(UNKNOWN))
}

/// The innermost `${...}`: (start, end, content).
fn places(s: &str) -> Vec<(usize, usize, String)> {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i + 1 < b.len() {
        if b[i] == b'$' && b[i + 1] == b'{' {
            let mut j = i + 2;
            let mut ok = true;
            while j < b.len() && b[j] != b'}' {
                if b[j] == b'$' || b[j] == b'{' {
                    ok = false;
                    break;
                }
                j += 1;
            }
            if ok && j < b.len() {
                out.push((i, j + 1, s[i + 2..j].to_string()));
                i = j + 1;
                continue;
            }
        }
        i += 1;
    }
    out
}

pub type Resolve<'r> = dyn FnMut(&str) -> R<Option<Json>> + 'r;

/// `${path}` placeholders, innermost first; a text that is only one
/// placeholder keeps the value's type.
fn expand_text(s: &str, resolve: &mut Resolve) -> R<Option<Json>> {
    let mut s = s.to_string();
    loop {
        let found = places(&s);
        if found.is_empty() {
            return Ok(Some(Json::Str(s)));
        }
        if found.len() == 1 && found[0].0 == 0 && found[0].1 == s.len() {
            let v = resolve(&found[0].2)?;
            return match v {
                Some(v @ (Json::Str(_) | Json::Arr(_) | Json::Obj(_))) if v.compact().contains("${") => expand(&v, resolve),
                other => Ok(other),
            };
        }
        let mut out = String::new();
        let mut last = 0;
        for (a, b, path) in &found {
            out.push_str(&s[last..*a]);
            let v = resolve(path)?;
            if let Some(v) = &v {
                if is_unknown(v) {
                    return Ok(Some(Json::Str(format!("{UNKNOWN}:{path}"))));
                }
                out.push_str(&text_of(v));
            }
            last = *b;
        }
        out.push_str(&s[last..]);
        s = out;
    }
}

pub fn truthy(v: &Option<Json>) -> bool {
    match v {
        None => false,
        Some(v) => is_unknown(v) || v.truthy(),
    }
}

/// A template with its placeholders filled in (None: omitted).
pub fn expand(t: &Json, resolve: &mut Resolve) -> R<Option<Json>> {
    match t {
        Json::Str(s) => expand_text(s, resolve),
        Json::Arr(items) => {
            let mut out = Vec::new();
            for x in items {
                if x.has("$each") {
                    let seq = resolve(x.get("$each").str().unwrap_or(""))?;
                    let seq: Vec<Json> = seq.map(|s| s.arr().to_vec()).unwrap_or_default();
                    for item in seq {
                        let mut inner = |path: &str| -> R<Option<Json>> {
                            if path == "item" {
                                return Ok(Some(item.clone()));
                            }
                            if let Some(rest) = path.strip_prefix("item.") {
                                return Ok(lookup(rest, &item).cloned());
                            }
                            resolve(path)
                        };
                        if let Some(v) = expand_or_omit(x.get("do"), &mut inner)? {
                            out.push(v.unwrap_or(Json::Null));
                        }
                    }
                    continue;
                }
                if let Some(v) = expand_or_omit(x, resolve)? {
                    out.push(v.unwrap_or(Json::Null));
                }
            }
            Ok(Some(Json::Arr(out)))
        }
        Json::Obj(pairs) => {
            if t.has("$if") {
                let cond = resolve(t.get("$if").str().unwrap_or(""))?;
                let branch = if truthy(&cond) { Some(t.get("then")) } else if t.has("else") { Some(t.get("else")) } else { None };
                return match branch {
                    None => Ok(None),
                    Some(b) => expand(b, resolve),
                };
            }
            let mut out = Vec::new();
            for (k, v) in pairs {
                let key = match expand_text(k, resolve)? {
                    Some(Json::Str(s)) => s,
                    Some(other) => text_of(&other),
                    None => String::new(),
                };
                if let Some(Some(val)) = expand_or_omit(v, resolve)? {
                    if !val.is_null() {
                        out.push((key, val));
                    }
                }
            }
            Ok(Some(Json::Obj(out)))
        }
        other => Ok(Some(other.clone())),
    }
}

/// Some(value) for an item of a list (None value = null), None when an
/// `$if` leaves it out.
fn expand_or_omit(t: &Json, resolve: &mut Resolve) -> R<Option<Option<Json>>> {
    if t.has("$if") {
        let cond = resolve(t.get("$if").str().unwrap_or(""))?;
        let branch = if truthy(&cond) { Some(t.get("then")) } else if t.has("else") { Some(t.get("else")) } else { None };
        return match branch {
            None => Ok(None),
            Some(b) => Ok(Some(expand(b, resolve)?)),
        };
    }
    Ok(Some(expand(t, resolve)?))
}

pub fn native_value(v: V) -> Json {
    if let Some(s) = v.native::<crate::crypto::Secret>() {
        return json::s(format!("${{secret.{}}}", s.name));
    }
    if let Some(r) = v.native::<Ref>() {
        return json::s(format!("${{attr.{}}}", r.parts.join(".")));
    }
    match v.tag {
        T_NONE => return Json::Null,
        T_BOOL => return Json::Bool(v.bits != 0),
        T_INT => return Json::Int(v.bits as i64),
        T_FLOAT => return Json::Num(f64::from_bits(v.bits)),
        _ => {}
    }
    match v.get() {
        Some(Obj::Str(s)) => json::s(s.to_string()),
        Some(Obj::Decimal(d)) => Json::Num(d.to_f64()),
        Some(Obj::List(items)) => Json::Arr(items.borrow().iter().map(|x| native_value(x.v())).collect()),
        Some(Obj::Map(mp)) => {
            Json::Obj(mp.borrow().entries.iter().map(|(k, x)| (crate::fmt::show(k.v()), native_value(x.v()))).collect())
        }
        _ => json::s(crate::fmt::show(v)),
    }
}

pub fn sha_hex(text: &str) -> String {
    crate::flags::hex(&Sha256::digest(text.as_bytes()))
}

pub fn hash_set<T: std::hash::Hash + Eq>() -> HashSet<T> {
    HashSet::new()
}

// ---------------------------------------------------------------- context

fn cidr_parts(text: &str) -> (u64, u32) {
    let (ip, prefix) = text.split_once('/').unwrap_or((text, "32"));
    let mut n: u64 = 0;
    for part in ip.split('.') {
        n = n * 256 + part.parse::<u64>().unwrap_or(0);
    }
    (n, prefix.parse().unwrap_or(32))
}

fn cidr_text(n: u64, prefix: u32) -> String {
    format!("{}.{}.{}.{}/{prefix}", (n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255)
}

fn guid(text: &str) -> String {
    let h = sha_hex(text);
    format!("{}-{}-{}-{}-{}", &h[0..8], &h[8..12], &h[12..16], &h[16..20], &h[20..32])
}

const AWS_WORDS: &[(&str, &str)] = &[
    ("acl", "ACL"), ("api", "API"), ("arn", "ARN"), ("dns", "DNS"), ("ec2", "EC2"), ("ecs", "ECS"), ("ecr", "ECR"),
    ("eks", "EKS"), ("efs", "EFS"), ("elb", "ELB"), ("iam", "IAM"), ("id", "Id"), ("ip", "IP"), ("kms", "KMS"),
    ("rds", "RDS"), ("s3", "S3"), ("sns", "SNS"), ("sqs", "SQS"), ("ssm", "SSM"), ("vpc", "VPC"), ("waf", "WAF"),
    ("wafv2", "WAFv2"), ("elasticloadbalancingv2", "ElasticLoadBalancingV2"), ("apigateway", "ApiGateway"),
    ("cloudfront", "CloudFront"), ("cloudwatch", "CloudWatch"), ("dynamodb", "DynamoDB"), ("route53", "Route53"),
    ("secretsmanager", "SecretsManager"), ("stepfunctions", "StepFunctions"), ("elasticache", "ElastiCache"),
    ("opensearchservice", "OpenSearchService"), ("eventbridge", "EventBridge"), ("lambda", "Lambda"),
];

fn pascal(word: &str) -> String {
    if let Some((_, w)) = AWS_WORDS.iter().find(|(k, _)| *k == word) {
        return w.to_string();
    }
    let mut c = word.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

/// `aws_wafv2_web_acl` -> `AWS::WAFv2::WebACL` (D120).
pub fn aws_type(name: &str) -> String {
    if name.contains("::") {
        return name.to_string();
    }
    let parts: Vec<&str> = name.split('_').collect();
    if parts[0] != "aws" || parts.len() < 3 {
        return name.to_string();
    }
    format!("AWS::{}::{}", pascal(parts[1]), parts[2..].iter().map(|p| pascal(p)).collect::<String>())
}

fn ident_of(name: &str) -> String {
    let s: String = name.to_lowercase().chars().filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit()).collect();
    if s.is_empty() {
        "nexo".into()
    } else {
        s
    }
}

fn strip_dash(s: &str) -> String {
    s.trim_matches('-').to_string()
}

fn head_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// ram (MB) / 1024 as the shortest decimal text.
fn gi_text(ram: i64) -> String {
    let n = ram as i128 * 9_765_625; // ram * 5^10 / 10^10 = ram / 1024
    let int = n / 10_000_000_000;
    let frac = n % 10_000_000_000;
    if frac == 0 {
        return int.to_string();
    }
    let f = format!("{frac:010}");
    format!("{int}.{}", f.trim_end_matches('0'))
}

fn cpu_of(st: &Json) -> f64 {
    match st.get("cpu") {
        Json::Num(f) => *f,
        Json::Int(i) => *i as f64,
        _ => 0.0,
    }
}

fn int_of(j: &Json) -> i64 {
    match j {
        Json::Int(i) => *i,
        Json::Num(f) => *f as i64,
        _ => 0,
    }
}

fn py_round(x: f64) -> i64 {
    // Python's round(): halves go to the even neighbour
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 {
        let t = x.trunc();
        return if (t as i64) % 2 == 0 { t as i64 } else { r as i64 };
    }
    r as i64
}

pub struct Plan {
    pub infra: &'static Infra,
    pub stage: Option<String>,
    pub values: Vec<(String, Val, &'static Cfg)>,
    pub cloud: String,
    pub region: String,
    pub project: String,
    pub subscription: String,
    pub namespace: String,
    /// resource -> settings and context
    pub settings: Vec<(String, Json)>,
    pub ctx: Vec<(String, Json)>,
    pub state: Option<State>,
    pub fresh: Vec<(String, Json)>,
    pub reveal: bool,
    /// the other infra blocks this one reads values of (D121)
    pub peers: std::cell::RefCell<Vec<(String, Box<Plan>)>>,
}

fn env_var(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.is_empty())
}

impl Plan {
    pub fn new(infra: &'static Infra, stage: Option<&str>) -> R<Plan> {
        let values = infra_settings(infra, stage)?;
        let cloud = text_value(&values, "cloud").unwrap();
        let region = text_value(&values, "region").unwrap_or_default();
        if cloud != "k8s" && region.is_empty() {
            return Err(errd(Diagnostic::new_spanless("infra_region").arg("infra", infra.name.clone()).arg("cloud", cloud.clone()), infra.span));
        }
        let project = text_value(&values, "project").filter(|s| !s.is_empty()).or_else(|| env_var("GOOGLE_CLOUD_PROJECT")).unwrap_or_else(|| "?".into());
        let subscription =
            text_value(&values, "subscription").filter(|s| !s.is_empty()).or_else(|| env_var("AZURE_SUBSCRIPTION_ID")).unwrap_or_else(|| "?".into());
        let namespace = text_value(&values, "namespace").filter(|s| !s.is_empty()).unwrap_or_else(|| slug(&infra.name, '-'));
        let mut plan = Plan {
            infra,
            stage: stage.map(String::from),
            values,
            cloud: cloud.clone(),
            region,
            project,
            subscription,
            namespace,
            settings: Vec::new(),
            ctx: Vec::new(),
            state: None,
            fresh: Vec::new(),
            reveal: false,
            peers: std::cell::RefCell::new(Vec::new()),
        };
        for r in &infra.resources {
            if cloud == "k8s" && (r.kind == "server" || r.kind == "cluster") {
                return Err(errd(Diagnostic::new_spanless("infra_target").arg("kind", r.kind.clone()).arg("cloud", cloud.clone()), r.span));
            }
            let mut st = effective(r, stage)?;
            if ["app", "database", "server", "cluster", "cache"].contains(&r.kind.as_str()) && cloud == "aws" && !st.has("network") {
                let nets: Vec<&Resource> = infra.resources.iter().filter(|x| x.kind == "network").collect();
                if nets.len() != 1 {
                    return Err(errd(Diagnostic::new_spanless("infra_needs_network").arg("kind", r.kind.clone()).arg("name", r.name.clone()), r.span));
                }
                st.set("network", json::s(nets[0].name.clone()));
            }
            if cloud == "k8s" && r.kind == "database" && stage == Some("prod") {
                return Err(errd(Diagnostic::new_spanless("infra_dev_only").arg("name", r.name.clone()), r.span));
            }
            if r.kind == "app" && st.has("cluster") && cloud != "k8s" {
                let span = r.value("cluster").map(|x| x.2.span).unwrap_or(r.span);
                return Err(errd(Diagnostic::new_spanless("not_supported").arg("what", "\"app ... cluster =\""), span));
            }
            plan.settings.push((r.name.clone(), st));
        }
        for r in &infra.resources {
            let c = build_ctx(&plan, r)?;
            plan.ctx.push((r.name.clone(), c));
        }
        Ok(plan)
    }

    pub fn set(&self, name: &str) -> &Json {
        self.settings.iter().find(|(n, _)| n == name).map(|(_, s)| s).unwrap()
    }

    pub fn ctx(&self, name: &str) -> &Json {
        self.ctx.iter().find(|(n, _)| n == name).map(|(_, s)| s).unwrap()
    }

    pub fn value(&self, key: &str) -> Option<V> {
        self.values.iter().find(|(k, _, _)| k == key).map(|(_, v, _)| v.v())
    }

    pub fn tags(&self, r: Option<&Resource>) -> Vec<(String, String)> {
        let mut out = vec![("nexo".to_string(), self.infra.name.clone())];
        let mut put = |k: String, v: String| match out.iter_mut().find(|(x, _)| *x == k) {
            Some(slot) => slot.1 = v,
            None => out.push((k, v)),
        };
        if let Some(Obj::Map(mp)) = self.value("tags").and_then(|v| v.get()) {
            for (k, v) in mp.borrow().entries.iter() {
                put(crate::fmt::show(k.v()), crate::fmt::show(v.v()));
            }
        }
        if let Some(r) = r {
            for (k, v) in self.set(&r.name).get("tags").pairs() {
                put(k.clone(), v.str().unwrap_or("").to_string());
            }
        }
        out
    }

    fn output(&self, nid: &str, path: &str, deps: &mut Vec<String>) -> Option<Json> {
        if !deps.iter().any(|d| d == nid) {
            deps.push(nid.to_string());
        }
        let read = self.fresh.iter().find(|(i, _)| i == nid).map(|(_, r)| r).or_else(|| {
            self.state.as_ref().and_then(|s| s.get(nid)).map(|e| e.get("read")).filter(|r| !r.is_null())
        });
        let unknown = || Some(json::s(format!("{UNKNOWN}:{nid}.{path}")));
        let Some(read) = read else { return unknown() };
        let v = if path.is_empty() { Some(read) } else { lookup(path, read) };
        match v {
            Some(v) if !v.is_null() => Some(v.clone()),
            _ => unknown(),
        }
    }

    /// What the templates of one resource read (None: of the infra).
    pub fn resolve(&self, r: Option<&Resource>, path: &str, deps: &mut Vec<String>) -> R<Option<Json>> {
        let (head, rest) = path.split_once('.').unwrap_or((path, ""));
        let tags = self.tags(r);
        Ok(match head {
            "name" => Some(json::s(r.map(|r| r.name.clone()).unwrap_or_else(|| self.infra.name.clone()))),
            "infra" => Some(json::s(self.infra.name.clone())),
            "region" => Some(json::s(self.region.clone())),
            "project" => Some(json::s(self.project.clone())),
            "subscription" => Some(json::s(self.subscription.clone())),
            "namespace" => Some(json::s(self.namespace.clone())),
            "stage" => Some(json::s(self.stage.clone().unwrap_or_else(|| "default".into()))),
            "tags" => Some(Json::Obj(tags.into_iter().map(|(k, v)| (k, json::s(v))).collect())),
            "aws_tags" | "aws_tags_named" => {
                let mut items = tags;
                if head == "aws_tags_named" {
                    if let Some(r) = r {
                        let base = self.ctx(&r.name).get("base").str().unwrap_or("").to_string();
                        match items.iter_mut().find(|(k, _)| k == "Name") {
                            Some(slot) => slot.1 = base,
                            None => items.push(("Name".into(), base)),
                        }
                    }
                }
                items.sort_by(|a, b| a.0.cmp(&b.0));
                Some(Json::Arr(items.into_iter().map(|(k, v)| json::obj(vec![("Key", json::s(k)), ("Value", json::s(v))])).collect()))
            }
            "labels" => {
                let mut items = tags;
                items.sort_by(|a, b| a.0.cmp(&b.0));
                Some(Json::Obj(items.into_iter().map(|(k, v)| (slug(&k, '_'), json::s(slug(&v, '_')))).collect()))
            }
            "k8s_labels" => Some(json::obj(vec![
                ("app.kubernetes.io/managed-by", json::s("nexo")),
                ("nexo.dev/infra", json::s(slug(&self.infra.name, '-'))),
            ])),
            "set" => r.and_then(|r| lookup(rest, self.set(&r.name)).cloned()),
            "ctx" => r.and_then(|r| lookup(rest, self.ctx(&r.name)).cloned()),
            "own" => {
                let Some(r) = r else { return Ok(None) };
                let (nid, p) = rest.split_once('.').unwrap_or((rest, ""));
                self.output(&format!("{}.{nid}", r.name), p, deps)
            }
            "link" => {
                let Some(r) = r else { return Ok(None) };
                let (key, more) = rest.split_once('.').unwrap_or((rest, ""));
                let Some(target) = self.set(&r.name).get(key).str() else { return Ok(None) };
                let (nid, p) = more.split_once('.').unwrap_or((more, ""));
                self.output(&format!("{target}.{nid}"), p, deps)
            }
            "res" => {
                let (name, more) = rest.split_once('.').unwrap_or((rest, ""));
                let (nid, p) = more.split_once('.').unwrap_or((more, ""));
                self.output(&format!("{name}.{nid}"), p, deps)
            }
            "attr" => {
                let (name, attr) = rest.split_once('.').unwrap_or((rest, ""));
                self.attr(name, attr, deps)?
            }
            "peer" => {
                let parts: Vec<String> = rest.split('.').map(String::from).collect();
                self.peer_attr(&parts)?
            }
            "secret" => {
                if self.reveal {
                    match std::env::var(rest) {
                        Ok(v) => Some(json::s(v)),
                        Err(_) => return Err(errd(Diagnostic::new_spanless("infra_secret").arg("name", rest), self.infra.span)),
                    }
                } else {
                    Some(json::s(secret_hash(rest)))
                }
            }
            _ => None,
        })
    }

    /// A value of another infra block, read from its state (D121).
    pub fn peer_attr(&self, parts: &[String]) -> R<Option<Json>> {
        let known = self.peers.borrow().iter().position(|(n, _)| *n == parts[0]);
        let at = match known {
            Some(i) => i,
            None => {
                let infra = infras().into_iter().find(|i| i.name == parts[0]).unwrap();
                let mut peer = Plan::new(infra, self.stage.as_deref())?;
                let store = store_of(&peer)?;
                peer.state = Some(State::new(store.load()?));
                let mut peers = self.peers.borrow_mut();
                peers.push((parts[0].clone(), Box::new(peer)));
                peers.len() - 1
            }
        };
        let peers = self.peers.borrow();
        peers[at].1.attr(&parts[1], &parts[2], &mut Vec::new())
    }

    pub fn attr(&self, name: &str, attr: &str, deps: &mut Vec<String>) -> R<Option<Json>> {
        let Some(r) = self.infra.resource(name) else { return Ok(None) };
        let t = providers().get("resources").get(&self.cloud).get(&r.kind).get("attrs").get(attr);
        if t.is_null() {
            return Ok(None);
        }
        let mut f = |p: &str| self.resolve(Some(r), p, deps);
        expand(t, &mut f)
    }

    // -- the native resources -------------------------------------------
    pub fn natives(&self) -> R<Vec<NativeRes>> {
        let mut out: Vec<NativeRes> = Vec::new();
        for t in providers().get("infra").get(&self.cloud).arr() {
            let mut deps = Vec::new();
            let desc = {
                let mut f = |p: &str| self.resolve(None, p, &mut deps);
                expand(t, &mut f)?.unwrap_or(Json::Null)
            };
            let id = desc.get("id").str().unwrap_or("").to_string();
            out.push(NativeRes { id, resource: String::new(), kind: "infra".into(), desc, deps });
        }
        let top: Vec<String> = out.iter().map(|n| n.id.clone()).collect();
        for r in &self.infra.resources {
            for t in providers().get("resources").get(&self.cloud).get(&r.kind).get("natives").arr() {
                for (desc, mut deps) in self.expand_native(r, t)? {
                    for d in &top {
                        if !deps.contains(d) {
                            deps.push(d.clone());
                        }
                    }
                    let id = format!("{}.{}", r.name, desc.get("id").str().unwrap_or(""));
                    out.push(NativeRes { id, resource: r.name.clone(), kind: r.kind.clone(), desc, deps });
                }
            }
        }
        Ok(order(out))
    }

    fn expand_native(&self, r: &Resource, t: &Json) -> R<Vec<(Json, Vec<String>)>> {
        let mut out = Vec::new();
        if t.has("$each") {
            let mut scratch = Vec::new();
            let seq = self.resolve(Some(r), t.get("$each").str().unwrap_or(""), &mut scratch)?;
            for item in seq.map(|s| s.arr().to_vec()).unwrap_or_default() {
                let mut deps = Vec::new();
                let desc = {
                    let mut f = |p: &str| -> R<Option<Json>> {
                        if p == "item" {
                            return Ok(Some(item.clone()));
                        }
                        if let Some(rest) = p.strip_prefix("item.") {
                            return Ok(lookup(rest, &item).cloned());
                        }
                        self.resolve(Some(r), p, &mut deps)
                    };
                    expand(t.get("do"), &mut f)?.unwrap_or(Json::Null)
                };
                out.push((desc, deps));
            }
            return Ok(out);
        }
        if t.has("$if") {
            let mut deps = Vec::new();
            let c = self.resolve(Some(r), t.get("$if").str().unwrap_or(""), &mut deps)?;
            if truthy(&c) {
                return self.expand_native(r, t.get("then"));
            }
            return Ok(out);
        }
        let mut deps = Vec::new();
        let desc = {
            let mut f = |p: &str| self.resolve(Some(r), p, &mut deps);
            expand(t, &mut f)?.unwrap_or(Json::Null)
        };
        out.push((desc, deps));
        Ok(out)
    }
}

pub fn secret_hash(name: &str) -> String {
    match std::env::var(name) {
        Ok(v) => format!("sha256:{}", sha_hex(&v)),
        Err(_) => format!("{UNKNOWN}:secret.{name}"),
    }
}

pub struct NativeRes {
    pub id: String,
    pub resource: String,
    pub kind: String,
    pub desc: Json,
    pub deps: Vec<String>,
}

impl NativeRes {
    pub fn typ(&self) -> String {
        self.desc.get("type").str().unwrap_or("").to_string()
    }
    pub fn name(&self) -> String {
        self.desc.get("name").str().unwrap_or("").to_string()
    }
}

/// Each native after the ones it reads from, otherwise in written order.
fn order(natives: Vec<NativeRes>) -> Vec<NativeRes> {
    let ids: HashSet<String> = natives.iter().map(|n| n.id.clone()).collect();
    let mut done: HashSet<String> = HashSet::new();
    let mut out = Vec::new();
    let mut pending = natives;
    while !pending.is_empty() {
        let pos = pending.iter().position(|n| n.deps.iter().all(|d| done.contains(d) || !ids.contains(d) || *d == n.id));
        match pos {
            Some(i) => {
                let n = pending.remove(i);
                done.insert(n.id.clone());
                out.push(n);
            }
            None => {
                out.append(&mut pending);
                break;
            }
        }
    }
    out
}

fn build_ctx(plan: &Plan, r: &Resource) -> R<Json> {
    let cloud = plan.cloud.as_str();
    let infra = plan.infra;
    let st = plan.set(&r.name);
    let sizes = providers().get("sizes").get(cloud);
    let base = slug(&format!("{}-{}", infra.name, r.name), '-');
    let mut ctx = Json::Obj(Vec::new());
    let put = |ctx: &mut Json, k: &str, v: Json| ctx.set(k, v);
    put(&mut ctx, "base", json::s(base.clone()));
    put(&mut ctx, "ident", json::s(ident_of(&r.name)));
    put(&mut ctx, "short", json::s(strip_dash(&head_chars(&base, 32))));
    put(&mut ctx, "bucket", json::s(head_chars(&base, 63)));
    let alnum: String = base.chars().filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit()).collect();
    put(&mut ctx, "account", json::s(head_chars(&alnum, 24)));
    let aid = strip_dash(&head_chars(&base, 30));
    put(&mut ctx, "account_id", json::s(if aid.chars().count() >= 6 { aid } else { head_chars(&format!("{aid}-nexo"), 30) }));
    let public = st.get("public").bool().unwrap_or(false);
    put(&mut ctx, "private", Json::Bool(!public));
    put(&mut ctx, "public_access", json::s(if public { "Enabled" } else { "Disabled" }));
    put(&mut ctx, "blob_access", json::s(if public { "Blob" } else { "None" }));
    put(&mut ctx, "access_prevention", json::s(if public { "inherited" } else { "enforced" }));
    put(&mut ctx, "s3_encryption", json::s("AES256"));
    put(&mut ctx, "scheme", json::s(if public { "internet-facing" } else { "internal" }));
    put(&mut ctx, "k8s_service_type", json::s(if public { "LoadBalancer" } else { "ClusterIP" }));
    put(&mut ctx, "run_ingress", json::s(if public { "INGRESS_TRAFFIC_ALL" } else { "INGRESS_TRAFFIC_INTERNAL_ONLY" }));
    if let Some(size) = st.get("size").str() {
        for (table, values) in sizes.pairs() {
            if values.has(size) {
                put(&mut ctx, table, values.get(size).clone());
            }
        }
    }
    if st.has("storage") {
        let mb = int_of(st.get("storage"));
        let gb = (-((-mb).div_euclid(1024))).max(1);
        put(&mut ctx, "storage_gb", Json::Int(gb));
        put(&mut ctx, "storage_gb_text", json::s(gb.to_string()));
        put(&mut ctx, "storage_gi", json::s(format!("{gb}Gi")));
    }
    if r.kind == "storage" {
        let mb = int_of(st.get("capacity"));
        put(&mut ctx, "size_gi", json::s(format!("{}Gi", (-((-mb).div_euclid(1024))).max(1))));
    }
    if r.kind == "database" {
        let pg = st.get("type").str() == Some("postgres");
        put(&mut ctx, "az_db_type", json::s(if pg { "Microsoft.DBforPostgreSQL/flexibleServers" } else { "Microsoft.DBforMySQL/flexibleServers" }));
        put(&mut ctx, "az_db_version", json::s(if pg { "16" } else { "8.0.21" }));
        put(&mut ctx, "gcp_db_version", json::s(if pg { "POSTGRES_16" } else { "MYSQL_8_0" }));
        put(&mut ctx, "k8s_db_image", json::s(if pg { "postgres:16" } else { "mysql:8.4" }));
        put(&mut ctx, "db_port", Json::Int(if pg { 5432 } else { 3306 }));
        put(&mut ctx, "k8s_db_name_var", json::s(if pg { "POSTGRES_DB" } else { "MYSQL_DATABASE" }));
        put(&mut ctx, "k8s_db_trust_var", json::s(if pg { "POSTGRES_HOST_AUTH_METHOD" } else { "MYSQL_ALLOW_EMPTY_PASSWORD" }));
        put(&mut ctx, "k8s_db_trust", json::s(if pg { "trust" } else { "yes" }));
    }
    for key in ["replicas", "nodes"] {
        if st.has(key) {
            let (mut lo, mut hi) = (int_of(st.get(key).get("min")), int_of(st.get(key).get("max")));
            if let (Some((a, b, _)), "replicas") = (r.scale, key) {
                lo = a;
                hi = b;
            }
            put(&mut ctx, &format!("{key}_min"), Json::Int(lo));
            put(&mut ctx, &format!("{key}_max"), Json::Int(hi));
        }
    }
    if r.kind == "app" {
        let lo = int_of(ctx.get("replicas_min"));
        let hi = int_of(ctx.get("replicas_max"));
        put(&mut ctx, "autoscale", Json::Bool(hi > lo));
        let target = r.scale.map(|s| s.2).unwrap_or(70);
        put(&mut ctx, "cpu_target", Json::Int(target));
        put(&mut ctx, "cpu_target_text", json::s(target.to_string()));
        let cpu = cpu_of(st);
        let ram = int_of(st.get("ram"));
        put(&mut ctx, "ecs_cpu", json::s(py_round(cpu * 1024.0).to_string()));
        put(&mut ctx, "ram_text", json::s(ram.to_string()));
        put(&mut ctx, "ram_mi", json::s(format!("{ram}Mi")));
        put(&mut ctx, "ram_gi", json::s(format!("{}Gi", gi_text(ram))));
        put(&mut ctx, "run_cpu", json::s(if cpu <= 1.0 { "1".to_string() } else { (cpu.ceil() as i64).to_string() }));
        put(&mut ctx, "k8s_cpu", json::s(format!("{}m", py_round(cpu * 1000.0))));
        // the program knows its stage (D115)
        let mut env = vec![json::obj(vec![("name", json::s("NEXO_STAGE")), ("value", json::s(plan.stage.clone().unwrap_or_else(|| "default".into())))])];
        let mut secrets = Vec::new();
        for (name, value, e) in &r.env {
            let v = value.v();
            if let Some(s) = v.native::<crate::crypto::Secret>() {
                secrets.push(json::obj(vec![
                    ("key", json::s(slug(name, '-'))),
                    ("name", json::s(name.clone())),
                    ("value", json::s(format!("${{secret.{}}}", s.name))),
                ]));
            } else if let Some(rf) = v.native::<Ref>() {
                env.push(json::obj(vec![("name", json::s(name.clone())), ("value", json::s(format!("${{{}.{}}}", if rf.parts.len() == 3 { "peer" } else { "attr" }, rf.parts.join("."))))]));
            } else if v.as_str().is_some() || matches!(v.tag, T_INT | T_FLOAT | T_BOOL) || matches!(v.get(), Some(Obj::Decimal(_))) {
                let text = match v.get() {
                    Some(Obj::Decimal(d)) => d.py_str(),
                    _ => text_of(&native_value(v)),
                };
                env.push(json::obj(vec![("name", json::s(name.clone())), ("value", json::s(text))]));
            } else {
                return Err(errd(Diagnostic::new_spanless("env_value").arg("name", name.clone()), e.span));
            }
        }
        let data: Vec<(String, Json)> = secrets.iter().map(|s| (s.get("name").str().unwrap().to_string(), s.get("value").clone())).collect();
        put(&mut ctx, "env", Json::Arr(env));
        put(&mut ctx, "secrets", Json::Arr(secrets));
        put(&mut ctx, "secret_data", Json::Obj(data));
    }
    if r.kind == "network" {
        let range = st.get("range").str().unwrap_or("").to_string();
        let (mut n, prefix) = cidr_parts(&range);
        if prefix > 19 {
            let span = r.value("range").map(|x| x.2.span).unwrap_or(r.span);
            return Err(errd(Diagnostic::new_spanless("infra_range").arg("range", range), span));
        }
        n &= !((1u64 << (32 - prefix)) - 1) & 0xFFFF_FFFF;
        put(&mut ctx, "subnet_a", json::s(cidr_text(n + 256, 24)));
        put(&mut ctx, "subnet_b", json::s(cidr_text(n + 512, 24)));
        put(&mut ctx, "subnet_wide", json::s(cidr_text(n + 4096, 20)));
        put(&mut ctx, "public_net", Json::Bool(!st.get("private").bool().unwrap_or(false)));
        let mut ingress = Vec::new();
        for (i, line) in r.lines.iter().enumerate() {
            let w = line.words();
            let port = line.option("port").and_then(is_int);
            let ws: Vec<Option<&str>> = w.iter().map(|x| x.v().as_str()).collect();
            let ok = w.len() == 5
                && ws[0] == Some("allow")
                && ws[1] == Some("from")
                && ws[3] == Some("to")
                && ws[2].is_some()
                && port.is_some()
                && line.options.len() == 1
                && line.head.len() == 1;
            if !ok {
                return Err(err("allow_form", line.span));
            }
            let src = ws[2].unwrap();
            let (cidr, source) = if src == "internet" {
                ("0.0.0.0/0".to_string(), "Internet".to_string())
            } else if is_cidr(src) {
                (src.to_string(), src.to_string())
            } else if infra.resource(src).is_some() {
                (range.clone(), range.clone())
            } else {
                return Err(errd(Diagnostic::new_spanless("infra_ref").arg("key", "from").arg("kind", "network").arg("name", src), line.span));
            };
            let dst = ws[4].unwrap_or("");
            if infra.resource(dst).is_none() {
                return Err(errd(Diagnostic::new_spanless("infra_ref").arg("key", "to").arg("kind", "app").arg("name", atom_text(w[4].v())), line.span));
            }
            let port = port.unwrap();
            ingress.push(json::obj(vec![
                ("port", Json::Int(port)),
                ("port_text", json::s(port.to_string())),
                ("cidr", json::s(cidr)),
                ("source", json::s(source)),
                ("priority", Json::Int(100 + 10 * i as i64)),
            ]));
        }
        put(&mut ctx, "ingress", Json::Arr(ingress));
    }
    if r.kind == "identity" {
        let g = grants(plan, r)?;
        put(&mut ctx, "grants", Json::Arr(g));
    }
    if r.kind == "dns" {
        let domain = r.name.clone();
        let (host, zone) = domain.split_once('.').map(|(a, b)| (a.to_string(), b.to_string())).unwrap_or((domain.clone(), String::new()));
        let zone = if zone.is_empty() { domain.clone() } else { zone };
        put(&mut ctx, "domain", json::s(domain));
        put(&mut ctx, "host", json::s(host));
        put(&mut ctx, "zone_id", json::s(slug(&zone, '-')));
        put(&mut ctx, "zone", json::s(zone));
        put(&mut ctx, "target", json::s(r.target.clone()));
        put(&mut ctx, "target_base", json::s(slug(&format!("{}-{}", infra.name, r.target), '-')));
        put(&mut ctx, "https", Json::Bool(r.https));
        put(&mut ctx, "scheme_dns", json::s(if r.https { "https" } else { "http" }));
        put(&mut ctx, "cert_mode", json::s(if r.https { "AUTOMATIC" } else { "NONE" }));
    }
    if r.kind == "server" && cloud == "azure" {
        let image = st.get("image").str().unwrap_or("").to_string();
        let parts: Vec<&str> = image.split(':').collect();
        if parts.len() != 4 {
            let span = r.value("image").map(|x| x.2.span).unwrap_or(r.span);
            return Err(errd(Diagnostic::new_spanless("server_image").arg("image", image), span));
        }
        put(
            &mut ctx,
            "az_image",
            json::obj(vec![("publisher", json::s(parts[0])), ("offer", json::s(parts[1])), ("sku", json::s(parts[2])), ("version", json::s(parts[3]))]),
        );
    }
    if r.kind == "native" {
        let mut values = st.clone();
        if cloud == "aws" {
            put(&mut ctx, "native_type", json::s(aws_type(&r.native_type)));
            let body: Vec<(String, Json)> =
                values.pairs().iter().map(|(k, v)| (k.split('_').map(pascal).collect::<String>(), v.clone())).collect();
            put(&mut ctx, "native_body", Json::Obj(body));
        } else {
            put(&mut ctx, "native_type", json::s(r.native_type.clone()));
            for key in ["api", "plural", "url"] {
                let v = values.remove(key).unwrap_or(json::s(""));
                put(&mut ctx, &format!("native_{key}"), v);
            }
            put(&mut ctx, "native_body", values);
        }
    }
    Ok(ctx)
}

fn grants(plan: &Plan, r: &Resource) -> R<Vec<Json>> {
    let mut out = Vec::new();
    let cloud = plan.cloud.as_str();
    let roles = providers().get("roles").get(cloud);
    for (i, line) in r.lines.iter().enumerate() {
        let w = line.words();
        let ws: Vec<Option<&str>> = w.iter().map(|x| x.v().as_str()).collect();
        let ok = w.len() == 4
            && ws[0] == Some("can")
            && matches!(ws[1], Some("read") | Some("write"))
            && ws[2].is_some_and(|k| roles.has(k))
            && ws[3].is_some()
            && line.options.is_empty()
            && line.head.len() == 1;
        if !ok {
            return Err(err("can_form", line.span));
        }
        let (verb, kind, name) = (ws[1].unwrap(), ws[2].unwrap(), ws[3].unwrap());
        if !plan.infra.resource(name).is_some_and(|t| t.kind == kind) {
            return Err(errd(Diagnostic::new_spanless("infra_ref").arg("key", "can").arg("kind", kind).arg("name", name), line.span));
        }
        let tbase = slug(&format!("{}-{name}", plan.infra.name), '-');
        let role = roles.get(kind).get(verb).clone();
        let mut g = json::obj(vec![("index", Json::Int(i as i64))]);
        match cloud {
            "aws" => {
                let b = head_chars(&tbase, 63);
                let res: Vec<String> = match kind {
                    "storage" => vec![format!("arn:aws:s3:::{b}"), format!("arn:aws:s3:::{b}/*")],
                    "queue" => vec![format!("${{res.{name}.queue.Arn}}")],
                    "database" => vec![format!("${{res.{name}.db.DBInstanceArn}}")],
                    _ => vec![format!("${{res.{name}.cache.ARN}}")],
                };
                g.set("actions", role);
                g.set("resources", Json::Arr(res.into_iter().map(json::s).collect()));
            }
            "azure" => {
                let main = match kind {
                    "storage" => "account",
                    "queue" => "namespace",
                    "database" => "server",
                    _ => "cache",
                };
                g.set("role", role);
                g.set("scope", json::s(format!("${{res.{name}.{main}.id}}")));
                g.set("guid", json::s(guid(&format!("{}/{}/{i}/{kind}/{name}", plan.infra.name, r.name))));
            }
            "gcp" => {
                let p = &plan.project;
                let res = match kind {
                    "storage" => format!("https://storage.googleapis.com/storage/v1/b/{}/iam", head_chars(&tbase, 63)),
                    "queue" => format!("https://pubsub.googleapis.com/v1/projects/{p}/topics/{tbase}"),
                    _ => format!("https://cloudresourcemanager.googleapis.com/v1/projects/{p}"),
                };
                g.set("role", role);
                g.set("resource", json::s(res));
            }
            _ => {
                let (group, plural) = if kind == "storage" { ("", "persistentvolumeclaims") } else { ("apps", "statefulsets") };
                g.set("group", json::s(group));
                g.set("plural", json::s(plural));
                g.set("target", json::s(tbase));
                g.set("verbs", role);
            }
        }
        out.push(g);
    }
    Ok(out)
}

// ------------------------------------------------------------------- diff

fn has_unknown(v: &Json) -> bool {
    match v {
        Json::Str(s) => s.starts_with(UNKNOWN),
        Json::Arr(a) => a.iter().any(has_unknown),
        Json::Obj(o) => o.iter().any(|(_, x)| has_unknown(x)),
        _ => false,
    }
}

/// Python's ==: 1 == 1.0.
pub fn same(a: &Json, b: &Json) -> bool {
    match (a, b) {
        (Json::Int(x), Json::Num(y)) | (Json::Num(y), Json::Int(x)) => *x as f64 == *y,
        (Json::Arr(x), Json::Arr(y)) => x.len() == y.len() && x.iter().zip(y).all(|(p, q)| same(p, q)),
        (Json::Obj(x), Json::Obj(y)) => x.len() == y.len() && x.iter().all(|(k, v)| y.iter().any(|(k2, v2)| k == k2 && same(v, v2))),
        _ => a == b,
    }
}

fn changed_keys(old: &Json, new: &Json, prefix: &str, out: &mut Vec<String>) {
    if let (Json::Obj(o), Json::Obj(n)) = (old, new) {
        let mut keys: Vec<&String> = n.iter().map(|(k, _)| k).collect();
        keys.extend(o.iter().map(|(k, _)| k).filter(|k| !n.iter().any(|(x, _)| x == *k)));
        for k in keys {
            let p = if prefix.is_empty() { k.clone() } else { format!("{prefix}.{k}") };
            changed_keys(old.get(k), new.get(k), &p, out);
        }
        return;
    }
    if has_unknown(new) || !same(old, new) {
        out.push(if prefix.is_empty() { "(body)".into() } else { prefix.into() });
    }
}

pub struct Action {
    pub op: &'static str,
    pub native: Option<NativeRes>,
    pub keys: Vec<String>,
    pub entry: Option<Json>,
}

pub fn diff(plan: &Plan) -> R<Vec<Action>> {
    let natives = plan.natives()?;
    let state = plan.state.as_ref().unwrap();
    let wanted: Vec<String> = natives.iter().map(|n| n.id.clone()).collect();
    let mut actions = Vec::new();
    for n in natives {
        let Some(entry) = state.get(&n.id).cloned() else {
            actions.push(Action { op: "create", native: Some(n), keys: Vec::new(), entry: None });
            continue;
        };
        let mut keys = Vec::new();
        changed_keys(entry.get("desc").get("body"), n.desc.get("body"), "", &mut keys);
        // a value kept beside the body (a GCP secret version: its hash)
        if entry.get("desc").get("value") != n.desc.get("value") {
            keys.push("value".into());
        }
        let old_type = entry.get("desc").get("type").clone();
        let old_name = entry.get("desc").get("name").clone();
        if old_type != *n.desc.get("type") || old_name != *n.desc.get("name") {
            let k = if old_type != *n.desc.get("type") { "type" } else { "name" };
            actions.push(Action { op: "replace", native: Some(n), keys: vec![k.into()], entry: Some(entry) });
        } else if !keys.is_empty() {
            actions.push(Action { op: "update", native: Some(n), keys, entry: Some(entry) });
        } else {
            actions.push(Action { op: "keep", native: Some(n), keys: Vec::new(), entry: Some(entry) });
        }
    }
    for (nid, entry) in state.entries.iter().rev() {
        if !wanted.contains(nid) {
            actions.push(Action { op: "delete", native: None, keys: Vec::new(), entry: Some(entry.clone()) });
        }
    }
    Ok(actions)
}

// ------------------------------------------------------- checks and costs

fn policy_errors(plan: &Plan) -> R<()> {
    let infra = plan.infra;
    let mut allowed: Vec<String> = Vec::new();
    for line in &infra.policy {
        let w = line.words();
        let ws: Vec<Option<&str>> = w.iter().map(|x| x.v().as_str()).collect();
        let ks = kinds();
        if w.len() == 3 && ws[0] == Some("deny") && ws[1] == Some("public") && ws[2].is_some_and(|k| ks.contains(&k)) && line.options.is_empty() {
            for r in &infra.resources {
                if r.kind == ws[2].unwrap() && plan.set(&r.name).get("public").bool() == Some(true) {
                    return Err(errd(
                        Diagnostic::new_spanless("policy_public").arg("infra", infra.name.clone()).arg("kind", r.kind.clone()).arg("name", r.name.clone()),
                        r.span,
                    ));
                }
            }
        } else if w.len() == 2 && ws[0] == Some("require") && ws[1] == Some("encryption") && line.options.is_empty() {
            for r in &infra.resources {
                if plan.set(&r.name).get("encryption").bool() == Some(false) {
                    return Err(errd(
                        Diagnostic::new_spanless("policy_encryption")
                            .arg("infra", infra.name.clone())
                            .arg("kind", r.kind.clone())
                            .arg("name", r.name.clone()),
                        r.span,
                    ));
                }
            }
        } else if w.len() == 3
            && ws[0] == Some("require")
            && ws[1] == Some("tags")
            && matches!(w[2].v().get(), Some(Obj::List(items)) if items.borrow().iter().all(|t| t.v().as_str().is_some()))
        {
            let wanted: Vec<String> = crate::cfg::items(w[2].v()).iter().map(|t| t.v().as_str().unwrap().to_string()).collect();
            for r in &infra.resources {
                let tags = plan.tags(Some(r));
                let missing: Vec<String> = wanted.iter().filter(|t| !tags.iter().any(|(k, _)| k == *t)).cloned().collect();
                if !missing.is_empty() {
                    return Err(errd(
                        Diagnostic::new_spanless("policy_tags")
                            .arg("infra", infra.name.clone())
                            .arg("kind", r.kind.clone())
                            .arg("name", r.name.clone())
                            .arg("tags", missing.join(", ")),
                        r.span,
                    ));
                }
            }
        } else if w.len() == 5 && ws[..4] == [Some("allow"), Some("personal"), Some("data"), Some("to")] && ws[4].is_some() {
            allowed.push(ws[4].unwrap().to_string());
        } else {
            return Err(errd(Diagnostic::new_spanless("policy_rule").arg("rule", show_phrase(line)), line.span));
        }
    }
    // section 23, rule 3
    if let Some(want) = text_value(&plan.values, "data_region").filter(|s| !s.is_empty()) {
        if crate::privacy::has_personal() && plan.cloud != "k8s" {
            let area = area_of(&plan.cloud, &plan.region);
            if area != want && !allowed.contains(&area) {
                let span = infra.value("region").or(infra.value("cloud")).unwrap().2.span;
                return Err(errd(
                    Diagnostic::new_spanless("data_region").arg("infra", infra.name.clone()).arg("region", plan.region.clone()).arg("area", want),
                    span,
                ));
            }
        }
    }
    Ok(())
}

fn area_of(cloud: &str, region: &str) -> String {
    let a = providers().get("areas").get(cloud);
    if a.has("names") {
        return a.get("names").get(region).str().unwrap_or(region).to_string();
    }
    let head = region.split('-').next().unwrap_or("");
    a.get("prefix").get(head).str().unwrap_or(head).to_string()
}

fn price(j: &Json) -> f64 {
    match j {
        Json::Num(f) => *f,
        Json::Int(i) => *i as f64,
        _ => 0.0,
    }
}

fn cents(x: f64) -> i64 {
    py_round(x * 100.0)
}

/// Estimated cents of EUR per month.
fn resource_cost(plan: &Plan, r: &Resource) -> i64 {
    let c = providers().get("cost").get(&plan.cloud);
    let st = plan.set(&r.name);
    let ctx = plan.ctx(&r.name);
    match r.kind.as_str() {
        "app" => {
            let n = int_of(ctx.get("replicas_min")).max(1);
            let per = py_round(cpu_of(st) * 1000.0) * cents(price(c.get("app_vcpu"))) / 1000
                + int_of(st.get("ram")) * cents(price(c.get("app_gb"))) / 1024;
            (per + cents(price(c.get("app_fixed"))) / n) * n
        }
        "server" | "database" | "cache" => {
            let mut p = cents(price(c.get(&r.kind).get(st.get("size").str().unwrap_or(""))));
            if r.kind == "database" {
                p += int_of(ctx.get("storage_gb")) * cents(price(c.get("database_gb")));
            }
            p
        }
        "cluster" => {
            cents(price(c.get("cluster")))
                + int_of(ctx.get("nodes_min")) * cents(price(c.get("cluster_node").get(st.get("size").str().unwrap_or(""))))
        }
        k => match c.get(k) {
            Json::Num(_) | Json::Int(_) => cents(price(c.get(k))),
            _ => 0,
        },
    }
}

fn availability(plan: &Plan) -> f64 {
    let sla = providers().get("sla").get(&plan.cloud);
    let mut a = 1.0;
    for r in &plan.infra.resources {
        if ["app", "server", "database", "storage", "cache", "queue", "cluster", "dns"].contains(&r.kind.as_str()) {
            a *= price(sla.get(&r.kind)) / 100.0;
        }
    }
    a * 100.0
}

fn money_text(c: i64) -> String {
    format!("{}.{:02}", c.div_euclid(100), c.rem_euclid(100))
}

fn budget_cents(plan: &Plan) -> R<Option<i64>> {
    let Some(v) = plan.value("budget") else { return Ok(None) };
    let entry = plan.infra.value("budget").unwrap().2;
    let atoms = crate::cfg::items(v);
    let ok = atoms.len() == 3
        && matches!(atoms[0].v().get(), Some(Obj::Money(_, "EUR")))
        && atoms[1].v().as_str() == Some("per")
        && atoms[2].v().as_str() == Some("month");
    if !ok {
        return Err(err("budget_form", entry.span));
    }
    let Some(Obj::Money(d, _)) = atoms[0].v().get() else { unreachable!() };
    Ok(Some(d.mul(&Dec::from_i64(100)).to_integral(crate::dec::Rounding::Down).to_i64().unwrap_or(0)))
}

/// The mistakes that stop a plan: policy, data region, budget.
pub fn check_plan(plan: &Plan) -> R<()> {
    policy_errors(plan)?;
    let budget = budget_cents(plan)?;
    let total: i64 = plan.infra.resources.iter().map(|r| resource_cost(plan, r)).sum();
    if let Some(b) = budget {
        if total > b {
            let span = plan.infra.value("budget").unwrap().2.span;
            return Err(errd(
                Diagnostic::new_spanless("over_budget")
                    .arg("infra", plan.infra.name.clone())
                    .arg("cost", money_text(total))
                    .arg("budget", money_text(b)),
                span,
            ));
        }
    }
    Ok(())
}

// ----------------------------------------------------------------- output

fn shown(v: &Json) -> String {
    match v {
        Json::Str(s) if s.contains(UNKNOWN) => m("info.unknown_value", &[]),
        Json::Str(s) => s.clone(),
        other => text_of(other),
    }
}

fn symbol(op: &str) -> &'static str {
    match op {
        "create" => "+",
        "update" => "~",
        "replace" => "-/+",
        "delete" => "-",
        _ => "=",
    }
}

pub fn plan_lines(plan: &Plan, actions: &[Action]) -> R<Vec<String>> {
    let where_ = if plan.region.is_empty() { String::new() } else { format!(", {}", plan.region) };
    let mut out = vec![m(
        "info.plan_header",
        &[
            ("infra", plan.infra.name.clone()),
            ("cloud", plan.cloud.clone()),
            ("region", where_),
            ("stage", plan.stage.clone().unwrap_or_else(|| "default".into())),
        ],
    )];
    let mut groups: Vec<(String, String, Vec<&Action>)> = Vec::new();
    for a in actions {
        let (rid, kind) = match &a.native {
            Some(n) => (n.resource.clone(), n.kind.clone()),
            None => {
                let e = a.entry.as_ref().unwrap();
                (e.get("resource").str().unwrap_or("").to_string(), e.get("kind").str().unwrap_or("").to_string())
            }
        };
        match groups.iter_mut().find(|(r, _, _)| *r == rid) {
            Some(g) => g.2.push(a),
            None => groups.push((rid, kind, vec![a])),
        }
    }
    let mut names: Vec<String> = vec![String::new()];
    names.extend(plan.infra.resources.iter().map(|r| r.name.clone()));
    let mut ordered: Vec<String> = names.clone();
    for (g, _, _) in &groups {
        if !names.contains(g) {
            ordered.push(g.clone());
        }
    }
    for rid in ordered {
        let Some((_, kind, acts)) = groups.iter().find(|(r, _, _)| *r == rid) else { continue };
        let ops: HashSet<&str> = acts.iter().map(|a| a.op).collect();
        let sym = if ops.len() == 1 && ops.contains("keep") {
            "="
        } else if ops.len() == 1 && ops.contains("create") {
            "+"
        } else if ops.len() == 1 && ops.contains("delete") {
            "-"
        } else {
            "~"
        };
        let title = if rid.is_empty() { format!("infra {}", plan.infra.name) } else { format!("{kind} {rid}") };
        out.push(format!("  {sym} {title}"));
        if sym == "=" {
            continue;
        }
        for a in acts {
            if a.op == "keep" {
                continue;
            }
            let d = match &a.native {
                Some(n) => &n.desc,
                None => a.entry.as_ref().unwrap().get("desc"),
            };
            let keys = if (a.op == "update" || a.op == "replace") && !a.keys.is_empty() { format!(": {}", a.keys.join(", ")) } else { String::new() };
            out.push(format!("      {} {} {}{keys}", symbol(a.op), shown(d.get("type")), shown(d.get("name"))));
        }
    }
    let count = |ops: &[&str]| actions.iter().filter(|a| ops.contains(&a.op)).count().to_string();
    if count(&["create", "update", "replace", "delete"]) == "0" {
        out.push(m("info.plan_none", &[]));
    } else {
        out.push(m(
            "info.plan_summary",
            &[("create", count(&["create"])), ("change", count(&["update", "replace"])), ("delete", count(&["delete"]))],
        ));
    }
    let total: i64 = plan.infra.resources.iter().map(|r| resource_cost(plan, r)).sum();
    match budget_cents(plan)? {
        None => out.push(m("info.plan_cost", &[("cost", money_text(total))])),
        Some(b) => out.push(m("info.plan_cost_budget", &[("cost", money_text(total)), ("budget", money_text(b))])),
    }
    out.push(m("info.plan_availability", &[("pct", format!("{:.2}", availability(plan)))]));
    Ok(out)
}

pub fn cost_lines(plan: &Plan) -> Vec<String> {
    let mut out = vec![m(
        "info.cost_header",
        &[("infra", plan.infra.name.clone()), ("cloud", plan.cloud.clone()), ("stage", plan.stage.clone().unwrap_or_else(|| "default".into()))],
    )];
    let width = plan.infra.resources.iter().map(|r| format!("{} {}", r.kind, r.name).chars().count()).max().unwrap_or(0);
    let mut total = 0;
    for r in &plan.infra.resources {
        let c = resource_cost(plan, r);
        total += c;
        let label = format!("{} {}", r.kind, r.name);
        let pad = " ".repeat(width - label.chars().count());
        out.push(format!("  {label}{pad}  {:>9} EUR", money_text(c)));
    }
    out.push(m("info.cost_total", &[("cost", money_text(total))]));
    out
}

// ------------------------------------------------------------------ state

/// What was created, native by native, in the order it was created.
pub struct State {
    pub serial: i64,
    pub entries: Vec<(String, Json)>,
    pub outputs: Vec<(String, Json)>,
}

impl State {
    pub fn new(data: Option<Json>) -> State {
        let data = data.unwrap_or(Json::Obj(Vec::new()));
        State {
            serial: data.get("serial").int().unwrap_or(0),
            entries: data.get("resources").arr().iter().map(|e| (e.get("id").str().unwrap_or("").to_string(), e.clone())).collect(),
            outputs: data.get("outputs").pairs().to_vec(),
        }
    }

    pub fn get(&self, nid: &str) -> Option<&Json> {
        self.entries.iter().find(|(i, _)| i == nid).map(|(_, e)| e)
    }

    pub fn put(&mut self, nid: &str, entry: Json) {
        match self.entries.iter_mut().find(|(i, _)| i == nid) {
            Some(slot) => slot.1 = entry,
            None => self.entries.push((nid.to_string(), entry)),
        }
    }

    pub fn remove(&mut self, nid: &str) {
        self.entries.retain(|(i, _)| i != nid);
    }

    pub fn data(&self, plan: &Plan) -> Json {
        json::obj(vec![
            ("infra", json::s(plan.infra.name.clone())),
            ("cloud", json::s(plan.cloud.clone())),
            ("stage", json::s(plan.stage.clone().unwrap_or_else(|| "default".into()))),
            ("serial", Json::Int(self.serial)),
            ("resources", Json::Arr(self.entries.iter().map(|(_, e)| e.clone()).collect())),
            ("outputs", Json::Obj(self.outputs.clone())),
        ])
    }
}

pub fn state_name(plan: &Plan) -> String {
    format!("{}-{}.json", slug(&plan.infra.name, '-'), slug(plan.stage.as_deref().unwrap_or("default"), '-'))
}

pub trait Store {
    fn where_(&self) -> String;
    fn load(&self) -> R<Option<Json>>;
    fn save(&self, data: &Json) -> R<()>;
    fn lock(&self) -> R<bool>;
    fn unlock(&self);
}

pub struct LocalStore {
    path: String,
}

impl Store for LocalStore {
    fn where_(&self) -> String {
        self.path.clone()
    }
    fn load(&self) -> R<Option<Json>> {
        Ok(std::fs::read_to_string(&self.path).ok().and_then(|t| json::parse(&t)))
    }
    fn save(&self, data: &Json) -> R<()> {
        let _ = std::fs::create_dir_all(".nexo/state");
        let tmp = self.path.trim_end_matches(".json").to_string() + ".tmp";
        let _ = std::fs::write(&tmp, data.pretty(false) + "\n");
        let _ = std::fs::rename(&tmp, &self.path);
        Ok(())
    }
    fn lock(&self) -> R<bool> {
        let _ = std::fs::create_dir_all(".nexo/state");
        let lock = self.path.trim_end_matches(".json").to_string() + ".lock";
        Ok(std::fs::OpenOptions::new().write(true).create_new(true).open(lock).is_ok())
    }
    fn unlock(&self) {
        let _ = std::fs::remove_file(self.path.trim_end_matches(".json").to_string() + ".lock");
    }
}

/// `s3("bucket")`: where the state of an infra block lives.
pub struct StateTarget {
    pub kind: String,
    pub name: String,
}

impl Native for StateTarget {
    fn type_name(&self) -> &str {
        "state"
    }
    fn repr(&self) -> String {
        format!("{}(\"{}\")", self.kind, self.name)
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

pub fn state_target(kind: &str, name: V, span: u32) -> R<Val> {
    let Some(n) = name.as_str() else { return Err(crate::stdlib::expects(kind, "str", name, span)) };
    if kind != "s3" {
        return Err(errd(Diagnostic::new_spanless("not_supported").arg("what", format!("\"state = {kind}(...)\"")), span));
    }
    Ok(native(StateTarget { kind: kind.to_string(), name: n.to_string() }))
}

pub fn store_of(plan: &Plan) -> R<Box<dyn Store>> {
    let Some(v) = plan.value("state") else {
        return Ok(Box::new(LocalStore { path: format!(".nexo/state/{}", state_name(plan)) }));
    };
    if let Some(t) = v.native::<StateTarget>() {
        if t.kind == "s3" {
            return Ok(Box::new(crate::infra_cloud::S3Store::new(plan, &t.name)));
        }
    }
    Err(err("state_form", plan.infra.value("state").unwrap().2.span))
}
