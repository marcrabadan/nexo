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

pub type Items = Box<dyn Iterator<Item = R<V>>>;

/// A re-readable lazy sequence: each iteration calls `make` again.
pub struct Lazy {
    pub make: Box<dyn Fn() -> R<Items>>,
}

/// Result of `group by`, waiting for sum, count or avg.
pub struct Grouped {
    pub key_name: String,
    pub groups: Vec<(V, Vec<V>)>,
}

struct FnIter<F: FnMut() -> Option<R<V>>>(F, bool);

impl<F: FnMut() -> Option<R<V>>> Iterator for FnIter<F> {
    type Item = R<V>;
    fn next(&mut self) -> Option<R<V>> {
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

fn boxed<F: FnMut() -> Option<R<V>> + 'static>(f: F) -> Items {
    Box::new(FnIter(f, false))
}

pub fn lazy(make: impl Fn() -> R<Items> + 'static) -> V {
    V::obj(Obj::Lazy(Lazy { make: Box::new(make) }))
}

/// The elements of an iterable value.
pub fn iterate(v: V, span: u32) -> R<Items> {
    match v.get() {
        Some(Obj::List(items)) => {
            let mut i = 0;
            Ok(boxed(move || {
                let items = items.borrow();
                let r = items.get(i).copied();
                i += 1;
                r.map(Ok)
            }))
        }
        Some(Obj::Lazy(l)) => (l.make)(),
        Some(Obj::Set(s)) => {
            let items: Vec<V> = s.borrow().items.clone();
            Ok(Box::new(items.into_iter().map(Ok)))
        }
        Some(Obj::Map(m)) => {
            let keys: Vec<V> = m.borrow().entries.iter().map(|(k, _)| *k).collect();
            Ok(Box::new(keys.into_iter().map(Ok)))
        }
        Some(Obj::Range(a, b)) => {
            let (a, b) = (*a, *b);
            Ok(Box::new((a..=b).map(|i| Ok(V::int(i)))))
        }
        Some(Obj::Str(s)) => {
            let chars: Vec<V> = s.chars().map(|c| V::str(c.to_string())).collect();
            Ok(Box::new(chars.into_iter().map(Ok)))
        }
        _ => Err(diag(Diagnostic::new_spanless("not_iterable").arg("vtype", type_name(v)), span)),
    }
}

pub fn collect_vec(v: V, span: u32) -> R<Vec<V>> {
    let mut out = Vec::new();
    for item in iterate(v, span)? {
        out.push(item?.plain());
    }
    Ok(out)
}

/// A lazy sequence read into a list.
pub fn collect(v: V) -> R<V> {
    Ok(list(collect_vec(v, 0)?))
}

/// For `repr` of a lazy value that was never bound (errors are ignored).
pub fn collect_quiet(v: V) -> Vec<V> {
    match collect_vec(v, 0) {
        Ok(items) => items,
        Err(_) => {
            crate::state::clear();
            Vec::new()
        }
    }
}

/// Read lazy sequences (equality, `+`); other values are returned as is.
pub fn force(v: V) -> R<V> {
    if matches!(v.get(), Some(Obj::Lazy(_))) {
        collect(v)
    } else {
        Ok(v)
    }
}

/// A value that is stored somewhere: lazy pipelines are read and float
/// literals become ordinary floats.
pub fn bind(v: V) -> R<V> {
    match v.get() {
        Some(Obj::Lazy(_)) => collect(v),
        Some(Obj::Grouped(_)) => Err(diag(Diagnostic::new_spanless("pipe_group_next"), 0)),
        _ => Ok(v.plain()),
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
fn apply(f: V, item: V, span: u32) -> R<V> {
    let value = call_value(f, &[item], span)?;
    if is_callable(value) {
        return call_value(value, &[item], span);
    }
    Ok(value)
}

pub fn where_(src: V, pred: V, step_span: u32, expr_span: u32) -> V {
    lazy(move || {
        let mut it = iterate(src, step_span)?;
        Ok(boxed(move || loop {
            let item = match it.next()? {
                Ok(x) => x,
                Err(e) => return Some(Err(e)),
            };
            let keep = call_value(pred, &[item], expr_span).and_then(|v| truth(v, expr_span, "where"));
            match keep {
                Ok(true) => return Some(Ok(item)),
                Ok(false) => continue,
                Err(e) => return Some(Err(e)),
            }
        }))
    })
}

pub fn map(src: V, f: V, step_span: u32, expr_span: u32) -> V {
    lazy(move || {
        let mut it = iterate(src, step_span)?;
        Ok(boxed(move || {
            let item = match it.next()? {
                Ok(x) => x,
                Err(e) => return Some(Err(e)),
            };
            Some(apply(f, item, expr_span).and_then(bind))
        }))
    })
}

pub fn pick(src: V, fns: Vec<V>, names: Vec<&'static str>, step_span: u32, expr_span: u32) -> V {
    lazy(move || {
        let mut it = iterate(src, step_span)?;
        let fns = fns.clone();
        let names = names.clone();
        Ok(boxed(move || {
            let item = match it.next()? {
                Ok(x) => x,
                Err(e) => return Some(Err(e)),
            };
            let mut pairs = Vec::new();
            for (f, n) in fns.iter().zip(&names) {
                match call_value(*f, &[item], expr_span) {
                    Ok(v) => pairs.push((V::str(*n), v)),
                    Err(e) => return Some(Err(e)),
                }
            }
            Some(Ok(map_from(pairs)))
        }))
    })
}

pub fn take(src: V, n: V, step_span: u32, n_span: u32) -> R<V> {
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
    Ok(lazy(move || Ok(Box::new(iterate(src, step_span)?.take(count)))))
}

/// Stable merge sort with a comparison that can fail.
fn merge_sort(items: &mut Vec<(V, V)>, less: &dyn Fn(V, V) -> R<bool>) -> R<()> {
    let n = items.len();
    if n < 2 {
        return Ok(());
    }
    let mut right = items.split_off(n / 2);
    merge_sort(items, less)?;
    merge_sort(&mut right, less)?;
    let left = std::mem::take(items);
    let (mut i, mut j) = (0, 0);
    while i < left.len() && j < right.len() {
        // take from the right only when it is strictly smaller (stability)
        if less(right[j].0, left[i].0)? {
            items.push(right[j]);
            j += 1;
        } else {
            items.push(left[i]);
            i += 1;
        }
    }
    items.extend_from_slice(&left[i..]);
    items.extend_from_slice(&right[j..]);
    Ok(())
}

pub fn sort(src: V, key: V, desc: bool, step_span: u32, expr_span: u32) -> V {
    lazy(move || {
        let items = collect_vec(src, step_span)?;
        let mut keyed = Vec::with_capacity(items.len());
        for item in items {
            let k = call_value(key, &[item], expr_span)?;
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

pub fn group(src: V, key: V, key_name: &str, step_span: u32, expr_span: u32) -> R<V> {
    let mut groups: Vec<(V, Vec<V>)> = Vec::new();
    let mut index: HashMap<Key, usize> = HashMap::new();
    for item in iterate(src, step_span)? {
        let item = item?;
        let k = call_value(key, &[item], expr_span)?;
        let hk = hashable(k, expr_span)?;
        match index.get(&hk) {
            Some(i) => groups[*i].1.push(item),
            None => {
                index.insert(hk, groups.len());
                groups.push((k.plain(), vec![item]));
            }
        }
    }
    Ok(V::obj(Obj::Grouped(Grouped { key_name: key_name.to_string(), groups })))
}

pub fn each(src: V, f: V, step_span: u32, expr_span: u32) -> R<V> {
    for item in iterate(src, step_span)? {
        apply(f, item?, expr_span)?;
    }
    Ok(NONE)
}

pub fn count(src: V, span: u32) -> R<V> {
    let mut n = 0i64;
    for item in iterate(src, span)? {
        item?;
        n += 1;
    }
    Ok(V::int(n))
}

pub fn total(items: &[V], span: u32) -> R<V> {
    let Some(first) = items.first() else { return Ok(V::int(0)) };
    let mut acc = first.plain();
    for item in &items[1..] {
        acc = arith(Op::Add, acc, item.plain(), span)?;
    }
    Ok(acc)
}

pub fn sum(src: V, span: u32) -> R<V> {
    let items = collect_vec(src, span)?;
    total(&items, span)
}

pub fn avg(src: V, span: u32) -> R<V> {
    let items = collect_vec(src, span)?;
    if items.is_empty() {
        return Ok(NONE);
    }
    let t = total(&items, span)?;
    arith(Op::Div, t, V::int(items.len() as i64), span)
}

/// `sum col`, `count` or `avg col` right after `group by`.
pub fn aggregate(grouped: V, kind: &str, f: V, col: &str, span: u32, expr_span: u32) -> R<V> {
    let Some(Obj::Grouped(g)) = grouped.get() else {
        return Err(diag(Diagnostic::new_spanless("pipe_group_next"), span));
    };
    let mut rows = Vec::with_capacity(g.groups.len());
    for (key, items) in &g.groups {
        let result = if kind == "count" {
            V::int(items.len() as i64)
        } else {
            let mut values = Vec::with_capacity(items.len());
            for item in items {
                values.push(call_value(f, &[*item], expr_span)?.plain());
            }
            let t = total(&values, span)?;
            if kind == "avg" {
                arith(Op::Div, t, V::int(values.len() as i64), span)?
            } else {
                t
            }
        };
        rows.push(map_from(vec![(V::str(g.key_name.clone()), *key), (V::str(col), result)]));
    }
    Ok(list(rows))
}

/// The value of a bare name inside a step: a column of the current row,
/// or a field of the current record (D44).
pub fn row_get(item: V, name: &str) -> Option<V> {
    match item.get()? {
        Obj::Map(m) => m.borrow().get(&Key::Str(name.to_string())),
        Obj::Record(r) => r.ty.fields.iter().position(|f| f.name == name).map(|i| r.fields.borrow()[i]),
        Obj::VariantValue(vv) => vv.variant.params.iter().position(|f| f.name == name).map(|i| vv.fields[i]),
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
