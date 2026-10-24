//! Pipelines (section 6). Every pipeline is lazy: `where`, `map`, `pick`,
//! `sort by` and `take` read nothing until the result is stored, iterated,
//! printed or reaches a final step (D50). `read_lines` reads line by line,
//! so `take 100` stops reading early.

use std::collections::HashMap;

use nexo_diag::Diagnostic;

use crate::call::{call_value, is_callable};
use crate::coll::{hashable, map_from};
use crate::fmt::{repr, type_name};
use crate::num::{arith, sort_less, Op};
use crate::state::{diag, R};
use crate::value::*;

pub type Items = Box<dyn Iterator<Item = R<Val>> + Send>;

/// A re-readable lazy sequence: each iteration calls `make` again.
pub struct Lazy {
    pub make: Box<dyn Fn() -> R<Items> + Send + Sync>,
    /// a database query (nexo.db): its steps may still run in SQL
    pub query: Option<Val>,
}

/// Result of `group by`, waiting for sum, count or avg.
pub struct Grouped {
    pub key_name: String,
    pub groups: Vec<(Val, Vec<Val>)>,
}

struct FnIter<F: FnMut() -> Option<R<Val>>>(F, bool);

impl<F: FnMut() -> Option<R<Val>>> Iterator for FnIter<F> {
    type Item = R<Val>;
    fn next(&mut self) -> Option<R<Val>> {
        if self.1 {
            return None;
        }
        let r = (self.0)();
        if matches!(r, Some(Err(_))) {
            self.1 = true;
        }
        r
    }
}

fn boxed<F: FnMut() -> Option<R<Val>> + Send + 'static>(f: F) -> Items {
    Box::new(FnIter(f, false))
}

pub fn lazy(make: impl Fn() -> R<Items> + Send + Sync + 'static) -> Val {
    Val::new(Obj::Lazy(Lazy { make: Box::new(make), query: None }))
}

/// A lazy sequence that is a database query.
pub fn lazy_query(make: impl Fn() -> R<Items> + Send + Sync + 'static, query: Val) -> Val {
    Val::new(Obj::Lazy(Lazy { make: Box::new(make), query: Some(query) }))
}

/// The elements of an iterable value. The iterator holds its own
/// reference to the collection: changing the variable during the loop
/// copies it (copy-on-write), so the loop sees the value it started with.
pub fn iterate(v: V, span: u32) -> R<Items> {
    match v.get() {
        Some(Obj::List(_)) => {
            let owner = Val::from(v);
            let mut i = 0;
            Ok(boxed(move || {
                let Some(Obj::List(items)) = owner.get() else { return None };
                let r = items.borrow().get(i).cloned();
                i += 1;
                r.map(Ok)
            }))
        }
        Some(Obj::Lazy(l)) => (l.make)(),
        Some(Obj::Set(s)) => {
            let items: Vec<Val> = s.borrow().items.clone();
            Ok(Box::new(items.into_iter().map(Ok)))
        }
        Some(Obj::Map(m)) => {
            let keys: Vec<Val> = m.borrow().entries.iter().map(|(k, _)| k.clone()).collect();
            Ok(Box::new(keys.into_iter().map(Ok)))
        }
        Some(Obj::Range(a, b)) => {
            let (a, b) = (*a, *b);
            Ok(Box::new((a..=b).map(|i| Ok(Val::int(i)))))
        }
        Some(Obj::Str(s)) => {
            let chars: Vec<Val> = s.chars().map(|c| Val::str(c.to_string())).collect();
            Ok(Box::new(chars.into_iter().map(Ok)))
        }
        Some(Obj::Channel(c)) => Ok(crate::sync::iterate(c, v, span)),
        _ => Err(diag(Diagnostic::new_spanless("not_iterable").arg("vtype", type_name(v)), span)),
    }
}

pub fn collect_vec(v: V, span: u32) -> R<Vec<Val>> {
    let mut out = Vec::new();
    for item in iterate(v, span)? {
        out.push(item?.plain());
    }
    Ok(out)
}

/// A lazy sequence read into a list.
pub fn collect(v: V) -> R<Val> {
    Ok(list(collect_vec(v, 0)?))
}

/// For `repr` of a lazy value that was never bound (errors are ignored).
pub fn collect_quiet(v: V) -> Vec<Val> {
    match collect_vec(v, 0) {
        Ok(items) => items,
        Err(_) => {
            crate::state::clear();
            Vec::new()
        }
    }
}

/// Read lazy sequences (equality, `+`); other values are returned as is.
pub fn force(v: V) -> R<Val> {
    if matches!(v.get(), Some(Obj::Lazy(_))) {
        collect(v)
    } else {
        Ok(Val::from(v))
    }
}

/// A value that is stored somewhere: lazy pipelines are read and float
/// literals become ordinary floats.
pub fn bind(v: V) -> R<Val> {
    match v.get() {
        Some(Obj::Lazy(_)) => collect(v),
        Some(Obj::Grouped(_)) => Err(diag(Diagnostic::new_spanless("pipe_group_next"), 0)),
        _ => Ok(Val::from(v.plain())),
    }
}

fn truth(v: V, span: u32, what: &str) -> R<bool> {
    match v.as_bool() {
        Some(b) => Ok(b),
        None => Err(diag(Diagnostic::new_spanless("not_bool").arg("what", what).arg("vtype", type_name(v)), span)),
    }
}

/// Evaluate a step expression for one element. If it names a function
/// (`each print`, `map double`), call it with the element (D51).
pub fn apply(f: V, item: V, span: u32) -> R<Val> {
    let value = call_value(f, &[item], span)?;
    if is_callable(value.v()) {
        return call_value(value.v(), &[item], span);
    }
    Ok(value)
}

pub fn where_(src: V, pred: V, step_span: u32, expr_span: u32) -> Val {
    let (src, pred) = (Val::from(src), Val::from(pred));
    lazy(move || {
        let mut it = iterate(src.v(), step_span)?;
        let pred = pred.clone();
        Ok(boxed(move || loop {
            let item = match it.next()? {
                Ok(x) => x,
                Err(e) => return Some(Err(e)),
            };
            let keep = call_value(pred.v(), &[item.v()], expr_span).and_then(|v| truth(v.v(), expr_span, "where"));
            match keep {
                Ok(true) => return Some(Ok(item)),
                Ok(false) => continue,
                Err(e) => return Some(Err(e)),
            }
        }))
    })
}

pub fn map(src: V, f: V, step_span: u32, expr_span: u32) -> Val {
    let (src, f) = (Val::from(src), Val::from(f));
    lazy(move || {
        let mut it = iterate(src.v(), step_span)?;
        let f = f.clone();
        Ok(boxed(move || {
            let item = match it.next()? {
                Ok(x) => x,
                Err(e) => return Some(Err(e)),
            };
            Some(apply(f.v(), item.v(), expr_span).and_then(|v| bind(v.v())))
        }))
    })
}

pub fn pick(src: V, fns: Vec<Val>, names: Vec<&'static str>, step_span: u32, expr_span: u32) -> Val {
    let src = Val::from(src);
    lazy(move || {
        let mut it = iterate(src.v(), step_span)?;
        let fns = fns.clone();
        let names = names.clone();
        Ok(boxed(move || {
            let item = match it.next()? {
                Ok(x) => x,
                Err(e) => return Some(Err(e)),
            };
            let mut pairs = Vec::new();
            for (f, n) in fns.iter().zip(&names) {
                match call_value(f.v(), &[item.v()], expr_span) {
                    Ok(v) => pairs.push((Val::str(*n), v)),
                    Err(e) => return Some(Err(e)),
                }
            }
            Some(Ok(map_from(pairs)))
        }))
    })
}

pub fn take(src: V, n: V, step_span: u32, n_span: u32) -> R<Val> {
    let count = match n.as_int() {
        Some(c) if c >= 0 => c as usize,
        _ => {
            return Err(diag(
                Diagnostic::new_spanless("type_mismatch")
                    .arg("name", "take")
                    .arg("type", "int")
                    .arg("value", repr(n.plain()))
                    .arg("vtype", type_name(n)),
                n_span,
            ))
        }
    };
    if let Some(q) = crate::db::pipe_take(src, count as i64) {
        return Ok(q);
    }
    let src = Val::from(src);
    Ok(lazy(move || Ok(Box::new(iterate(src.v(), step_span)?.take(count)))))
}

/// Stable merge sort with a comparison that can fail.
fn merge_sort(items: &mut Vec<(Val, Val)>, less: &dyn Fn(V, V) -> R<bool>) -> R<()> {
    let n = items.len();
    if n < 2 {
        return Ok(());
    }
    let mut right = items.split_off(n / 2);
    merge_sort(items, less)?;
    merge_sort(&mut right, less)?;
    let left = std::mem::take(items);
    let mut left = left.into_iter().peekable();
    let mut right = right.into_iter().peekable();
    while let (Some(l), Some(r)) = (left.peek(), right.peek()) {
        // take from the right only when it is strictly smaller (stability)
        if less(r.0.v(), l.0.v())? {
            items.push(right.next().unwrap());
        } else {
            items.push(left.next().unwrap());
        }
    }
    items.extend(left);
    items.extend(right);
    Ok(())
}

pub fn sort(src: V, key: V, desc: bool, step_span: u32, expr_span: u32) -> Val {
    let (src, key) = (Val::from(src), Val::from(key));
    lazy(move || {
        let items = collect_vec(src.v(), step_span)?;
        let mut keyed = Vec::with_capacity(items.len());
        for item in items {
            let k = call_value(key.v(), &[item.v()], expr_span)?;
            keyed.push((k, item));
        }
        if desc {
            merge_sort(&mut keyed, &|a, b| sort_less(b, a, expr_span))?;
        } else {
            merge_sort(&mut keyed, &|a, b| sort_less(a, b, expr_span))?;
        }
        Ok(Box::new(keyed.into_iter().map(|(_, v)| Ok(v))))
    })
}

pub fn group(src: V, key: V, key_name: &str, step_span: u32, expr_span: u32) -> R<Val> {
    let mut groups: Vec<(Val, Vec<Val>)> = Vec::new();
    let mut index: HashMap<Key, usize> = HashMap::new();
    for item in iterate(src, step_span)? {
        let item = item?;
        let k = call_value(key, &[item.v()], expr_span)?;
        let hk = hashable(k.v(), expr_span)?;
        match index.get(&hk) {
            Some(i) => groups[*i].1.push(item),
            None => {
                index.insert(hk, groups.len());
                groups.push((k.plain(), vec![item]));
            }
        }
    }
    Ok(Val::new(Obj::Grouped(Grouped { key_name: key_name.to_string(), groups })))
}

pub fn each(src: V, f: V, step_span: u32, expr_span: u32) -> R<Val> {
    for item in iterate(src, step_span)? {
        apply(f, item?.v(), expr_span)?;
    }
    Ok(Val::none())
}

pub fn count(src: V, span: u32) -> R<Val> {
    if let Some(q) = crate::db::query_of(src) {
        return Ok(Val::int(q.count(span)?));
    }
    let mut n = 0i64;
    for item in iterate(src, span)? {
        item?;
        n += 1;
    }
    Ok(Val::int(n))
}

pub fn total(items: &[Val], span: u32) -> R<Val> {
    let Some(first) = items.first() else { return Ok(Val::int(0)) };
    let mut acc = first.clone().plain();
    for item in &items[1..] {
        acc = arith(Op::Add, acc.v(), item.v().plain(), span)?;
    }
    Ok(acc)
}

pub fn sum(src: V, span: u32) -> R<Val> {
    let items = collect_vec(src, span)?;
    total(&items, span)
}

pub fn avg(src: V, span: u32) -> R<Val> {
    let items = collect_vec(src, span)?;
    if items.is_empty() {
        return Ok(Val::none());
    }
    let t = total(&items, span)?;
    arith(Op::Div, t.v(), V::int(items.len() as i64), span)
}

/// `sum col`, `count` or `avg col` right after `group by`.
pub fn aggregate(grouped: V, kind: &str, f: V, col: &str, span: u32, expr_span: u32) -> R<Val> {
    let Some(Obj::Grouped(g)) = grouped.get() else {
        return Err(diag(Diagnostic::new_spanless("pipe_group_next"), span));
    };
    let mut rows = Vec::with_capacity(g.groups.len());
    for (key, items) in &g.groups {
        let result = if kind == "count" {
            Val::int(items.len() as i64)
        } else {
            let mut values = Vec::with_capacity(items.len());
            for item in items {
                values.push(call_value(f, &[item.v()], expr_span)?.plain());
            }
            let t = total(&values, span)?;
            if kind == "avg" {
                arith(Op::Div, t.v(), V::int(values.len() as i64), span)?
            } else {
                t
            }
        };
        rows.push(map_from(vec![(Val::str(g.key_name.clone()), key.clone()), (Val::str(col), result)]));
    }
    Ok(list(rows))
}

/// The value of a bare name inside a step: a column of the current row,
/// or a field of the current record (D44).
pub fn row_get(item: V, name: &str) -> Option<Val> {
    match item.get()? {
        Obj::Map(m) => m.borrow().get(&Key::Str(name.to_string())).map(Val::from),
        Obj::Record(r) => {
            r.ty.fields.iter().position(|f| f.name == name).map(|i| upgrade(r.fields.borrow()[i].v()))
        }
        Obj::VariantValue(vv) => vv.variant.params.iter().position(|f| f.name == name).map(|i| vv.fields[i].clone()),
        _ => None,
    }
}

/// Names a row offers, for "Did you mean...?".
pub fn row_names(item: V) -> Vec<String> {
    match item.get() {
        Some(Obj::Map(m)) => m.borrow().entries.iter().filter_map(|(k, _)| k.as_str().map(String::from)).collect(),
        Some(Obj::Record(r)) => r.ty.fields.iter().map(|f| f.name.clone()).collect(),
        _ => Vec::new(),
    }
}
