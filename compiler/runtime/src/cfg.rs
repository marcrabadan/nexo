//! The entries of phase 5 blocks (D114): `infra`, `stage`, `pipeline` and
//! `flags` are read by both parsers into lists of `__cfg(head, values,
//! options, body)` values, which the modules then read. A bare name is a
//! word (a text), a dotted name a reference (`__ref`) and a comparison a
//! condition kept as data (`__cond`). Follows phase 0's cfg.py.

use crate::fmt::repr;
use crate::state::R;
use crate::value::*;

/// One entry: `head` and `values` are lists of phrases (lists of atoms),
/// `options` the `name: value` items, `body` the entries of its block (or
/// the function of a pipeline step).
pub struct Cfg {
    pub head: Vec<Vec<Val>>,
    pub values: Option<Vec<Vec<Val>>>,
    pub options: Vec<(String, Val)>,
    pub body: Body,
    pub span: u32,
}

pub enum Body {
    None,
    Entries(Vec<Val>),
    Code(Val),
}

impl Cfg {
    /// The atoms of the first phrase of the head.
    pub fn words(&self) -> &[Val] {
        self.head.first().map(|p| p.as_slice()).unwrap_or(&[])
    }

    /// The word at position i of the head, if it is a text.
    pub fn word(&self, i: usize) -> Option<&'static str> {
        self.words().get(i).and_then(|v| v.v().as_str())
    }

    /// The key of a `key = value` entry, or None.
    pub fn key(&self) -> Option<&'static str> {
        self.values.as_ref()?;
        if self.words().len() == 1 {
            self.word(0)
        } else {
            None
        }
    }

    /// The single atom of `key = value`, or the list of atoms when the
    /// first phrase has several.
    pub fn value(&self) -> Option<Val> {
        let first = self.values.as_ref()?.first()?;
        if first.len() == 1 {
            Some(first[0].clone())
        } else {
            Some(list(first.clone()))
        }
    }

    pub fn entries(&self) -> Vec<&'static Cfg> {
        match &self.body {
            Body::Entries(v) => v.iter().filter_map(|e| as_cfg(e.v())).collect(),
            _ => Vec::new(),
        }
    }

    pub fn option(&self, name: &str) -> Option<V> {
        self.options.iter().find(|(k, _)| k == name).map(|(_, v)| v.v())
    }
}

pub fn as_cfg(v: V) -> Option<&'static Cfg> {
    v.native::<Cfg>()
}

impl Native for Cfg {
    fn type_name(&self) -> &str {
        "config"
    }
    fn repr(&self) -> String {
        let mut text = self.head.iter().map(|p| phrase_text(p)).collect::<Vec<_>>().join(", ");
        if let Some(values) = &self.values {
            text.push_str(" = ");
            text.push_str(&values.iter().map(|p| phrase_text(p)).collect::<Vec<_>>().join(", "));
        }
        for (k, v) in &self.options {
            text.push_str(&format!(", {k}: {}", atom_text(v.v())));
        }
        text
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// `customers.url`: what a resource exposes (D114).
pub struct Ref {
    pub parts: Vec<String>,
}

impl Native for Ref {
    fn type_name(&self) -> &str {
        "reference"
    }
    fn repr(&self) -> String {
        self.parts.join(".")
    }
    fn equals(&self, other: &dyn Native) -> bool {
        other.as_any().downcast_ref::<Ref>().is_some_and(|o| o.parts == self.parts)
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// `errors < 1%`, `country == "ES" and age > 18`: a condition.
pub struct Cond {
    pub op: String,
    pub args: Vec<Val>,
}

impl Native for Cond {
    fn type_name(&self) -> &str {
        "condition"
    }
    fn repr(&self) -> String {
        if self.op == "not" {
            return format!("not {}", atom_text(self.args[0].v()));
        }
        format!("{} {} {}", atom_text(self.args[0].v()), self.op, atom_text(self.args[1].v()))
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

pub fn atom_text(a: V) -> String {
    if let Some(s) = a.as_str() {
        return s.to_string();
    }
    if let Some(Obj::List(items)) = a.get() {
        return format!("[{}]", items.borrow().iter().map(|x| atom_text(x.v())).collect::<Vec<_>>().join(", "));
    }
    repr(a)
}

pub fn phrase_text(p: &[Val]) -> String {
    p.iter().map(|a| atom_text(a.v())).collect::<Vec<_>>().join(" ")
}

pub fn items(v: V) -> Vec<Val> {
    match v.get() {
        Some(Obj::List(items)) => items.borrow().clone(),
        _ => Vec::new(),
    }
}

fn phrases(v: V) -> Vec<Vec<Val>> {
    items(v).iter().map(|p| items(p.v())).collect()
}

pub fn cfg(head: V, values: V, options: V, body: V, span: u32) -> R<Val> {
    let mut opts = Vec::new();
    if let Some(Obj::Map(m)) = options.get() {
        for (k, v) in m.borrow().entries.iter() {
            opts.push((k.as_str().unwrap_or("").to_string(), v.clone()));
        }
    }
    let body = if body.is_none() {
        Body::None
    } else if let Some(Obj::List(_)) = body.get() {
        Body::Entries(items(body))
    } else {
        Body::Code(Val::from(body))
    };
    let values = if values.is_none() { None } else { Some(phrases(values)) };
    Ok(native(Cfg { head: phrases(head), values, options: opts, body, span }))
}

pub fn reference(args: &[V]) -> R<Val> {
    Ok(native(Ref { parts: args.iter().map(|a| a.as_str().unwrap_or("").to_string()).collect() }))
}

pub fn cond(args: &[V]) -> R<Val> {
    let op = args.first().and_then(|a| a.as_str()).unwrap_or("").to_string();
    Ok(native(Cond { op, args: args[1..].iter().map(|a| Val::from(*a)).collect() }))
}

/// Evaluate a condition against a record or map: its words are fields
/// (flags audiences, section 22).
pub fn holds(c: V, row: V, span: u32) -> R<bool> {
    fn get(a: V, row: V, span: u32) -> R<Val> {
        if a.native::<Cond>().is_some() {
            return Ok(Val::bool(holds(a, row, span)?));
        }
        if let Some(s) = a.as_str() {
            if let Some(v) = crate::pipe::row_get(row, s) {
                return Ok(v);
            }
        }
        if let Some(r) = a.native::<Ref>() {
            let mut v = Val::from(row);
            for p in &r.parts {
                v = crate::pipe::row_get(v.v(), p).unwrap_or_else(Val::none);
            }
            return Ok(v);
        }
        Ok(Val::from(a))
    }
    let Some(c) = c.native::<Cond>() else {
        let v = get(c, row, span)?;
        return Ok(v.v().as_bool().unwrap_or(!v.v().is_none()));
    };
    match c.op.as_str() {
        "and" => Ok(holds(c.args[0].v(), row, span)? && holds(c.args[1].v(), row, span)?),
        "or" => Ok(holds(c.args[0].v(), row, span)? || holds(c.args[1].v(), row, span)?),
        "not" => Ok(!holds(c.args[0].v(), row, span)?),
        op => {
            let a = get(c.args[0].v(), row, span)?;
            let b = get(c.args[1].v(), row, span)?;
            match op {
                "==" => crate::num::equals(a.v(), b.v(), span),
                "!=" => Ok(!crate::num::equals(a.v(), b.v(), span)?),
                "in" => crate::coll::contains(b.v(), a.v(), span),
                "is" => Err(crate::state::diag(nexo_diag::Diagnostic::new_spanless("not_supported").arg("what", "\"is\""), span)),
                _ => {
                    if a.v().is_none() || b.v().is_none() {
                        return Ok(false);
                    }
                    crate::num::order(op, a.v(), b.v(), span)
                }
            }
        }
    }
}
