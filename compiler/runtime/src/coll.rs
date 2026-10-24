//! Collections: map keys and set elements (D35), indexing (D64),
//! membership (`in`) and iteration.

use nexo_diag::Diagnostic;

use crate::dec::exact_from_f64;
use crate::fmt::type_name;
use crate::num::equals;
use crate::state::{diag, failure, R};
use crate::value::*;

/// Map keys and set elements: texts, numbers, bools, money, quantities,
/// none and enum variants without data.
pub fn key_of(v: V) -> Option<Key> {
    let v = v.plain();
    Some(match v.tag {
        T_NONE => Key::None,
        T_BOOL => Key::Bool(v.bits != 0),
        T_INT => Key::Int(v.bits as i64),
        T_FLOAT => float_key(f64::from_bits(v.bits)),
        T_OBJ => match v.get()? {
            Obj::Str(s) => Key::Str(s.clone()),
            Obj::Decimal(d) => {
                if d.is_integral() {
                    if let Some(i) = d.to_i64() {
                        return Some(Key::Int(i));
                    }
                }
                Key::Dec(d.key())
            }
            Obj::Money(d, c) => Key::Money(d.key(), c),
            Obj::Quantity(d, u) => Key::Qty(d.key(), u),
            Obj::Variant(var) => Key::Variant(*var as *const _ as usize),
            Obj::Native(n) => Key::Native(n.key()?),
            _ => return None,
        },
        _ => return None,
    })
}

fn float_key(f: f64) -> Key {
    if f.fract() == 0.0 && f.abs() < 9.2e18 {
        return Key::Int(f as i64);
    }
    match exact_from_f64(f) {
        Some(d) => Key::Dec(d.key()),
        None => Key::Float(f.to_bits()),
    }
}

pub fn hashable(v: V, span: u32) -> R<Key> {
    key_of(v).ok_or_else(|| diag(Diagnostic::new_spanless("unhashable").arg("vtype", type_name(v)), span))
}

impl NMap {
    pub fn get(&self, k: &Key) -> Option<V> {
        self.index.get(k).map(|i| self.entries[*i].1.v())
    }

    pub fn insert(&mut self, key: Key, kv: Val, value: Val) {
        match self.index.get(&key) {
            Some(i) => self.entries[*i].1 = value,
            None => {
                self.index.insert(key, self.entries.len());
                self.entries.push((kv.plain(), value));
            }
        }
    }
}

impl NSet {
    pub fn insert(&mut self, key: Key, v: Val) {
        if !self.index.contains_key(&key) {
            self.index.insert(key, self.items.len());
            self.items.push(v.plain());
        }
    }
}

pub fn new_map() -> Val {
    Val::new(Obj::Map(Mut::new(NMap::default())))
}

pub fn new_set(items: &[V], span: u32) -> R<Val> {
    let mut s = NSet::default();
    for i in items {
        let k = hashable(*i, span)?;
        s.insert(k, Val::from(*i));
    }
    Ok(Val::new(Obj::Set(Mut::new(s))))
}

pub fn map_from(pairs: Vec<(Val, Val)>) -> Val {
    let mut m = NMap::default();
    for (k, v) in pairs {
        let key = key_of(k.v()).expect("simple key");
        m.insert(key, k, v);
    }
    Val::new(Obj::Map(Mut::new(m)))
}

pub fn str_len(s: &str) -> usize {
    s.chars().count()
}

/// `x[i]`: lists and texts by position (from 0), maps by key (a missing
/// key gives none).
pub fn get_index(obj: V, index: V, span: u32) -> R<Val> {
    let forced;
    let obj = if matches!(obj.get(), Some(Obj::Lazy(_))) {
        forced = crate::pipe::force(obj)?;
        forced.v()
    } else {
        obj
    };
    match obj.get() {
        Some(Obj::Map(m)) => {
            let k = hashable(index, span)?;
            Ok(Val::from(m.borrow().get(&k).unwrap_or(NONE)))
        }
        Some(Obj::List(items)) => {
            let Some(i) = index.as_int() else {
                return Err(bad_index(obj, index, span));
            };
            let items = items.borrow();
            if i < 0 || i as usize >= items.len() {
                return Err(out_of_range(i, items.len(), span));
            }
            Ok(items[i as usize].clone())
        }
        Some(Obj::Str(s)) => {
            let Some(i) = index.as_int() else {
                return Err(bad_index(obj, index, span));
            };
            let n = str_len(s);
            if i < 0 || i as usize >= n {
                return Err(out_of_range(i, n, span));
            }
            Ok(Val::str(s.chars().nth(i as usize).unwrap().to_string()))
        }
        // `channel[Order]`: channels that carry values of that type
        Some(Obj::ChannelType(None)) => Ok(Val::new(Obj::ChannelType(Some(Val::from(index))))),
        _ if obj.is_none() => Err(diag(Diagnostic::new_spanless("none_member").arg("name", "[]"), span)),
        _ => Err(diag(Diagnostic::new_spanless("not_indexable").arg("vtype", type_name(obj)), span)),
    }
}

fn bad_index(obj: V, index: V, span: u32) -> crate::state::Fail {
    diag(Diagnostic::new_spanless("bad_index").arg("vtype", type_name(obj)).arg("itype", type_name(index)), span)
}

fn out_of_range(i: i64, size: usize, span: u32) -> crate::state::Fail {
    failure("OutOfRange", span, &[("index", i.to_string()), ("size", size.to_string())])
}

/// A value to store in `container`: the container itself is stored as a
/// copy of what it is now (`xs.add(xs)`), so it never contains itself.
pub fn stored_in(container: V, value: Val) -> Val {
    if value.tag == T_OBJ && value.bits == container.bits {
        if let Some(c) = crate::cow::shallow_copy(value.v()) {
            return c;
        }
    }
    value
}

/// `obj[index] = value`. The caller made `obj` unique (copy-on-write).
pub fn set_index(obj: V, index: V, value: V, span: u32) -> R<()> {
    let value = stored_in(obj, crate::pipe::bind(value)?);
    match obj.get() {
        Some(Obj::Map(m)) => {
            let k = hashable(index, span)?;
            m.borrow_mut().insert(k, Val::from(index), value);
            Ok(())
        }
        Some(Obj::List(items)) => {
            let Some(i) = index.as_int() else {
                return Err(diag(
                    Diagnostic::new_spanless("bad_index").arg("vtype", "list").arg("itype", type_name(index)),
                    span,
                ));
            };
            let items = items.borrow_mut();
            if i < 0 || i as usize >= items.len() {
                return Err(out_of_range(i, items.len(), span));
            }
            items[i as usize] = value;
            Ok(())
        }
        _ => Err(diag(Diagnostic::new_spanless("not_indexable").arg("vtype", type_name(obj)), span)),
    }
}

/// Pointer to the element `container[index]` for a change through it
/// (`xs[0].add(1)`, `m[k][i] = v`): the container is made unique first,
/// then the element. None when the element is not stored in a container
/// (a missing map key, a character of a text): `scratch` then holds it.
pub fn element_slot(container: V, index: V, scratch: &mut Val, span: u32) -> R<Option<*mut V>> {
    match container.get() {
        Some(Obj::Map(m)) => {
            let k = hashable(index, span)?;
            let m = m.borrow_mut();
            match m.index.get(&k) {
                Some(i) => {
                    let slot = &mut m.entries[*i].1;
                    crate::cow::make_unique_val(slot);
                    Ok(Some(slot as *mut Val as *mut V))
                }
                None => {
                    *scratch = Val::none();
                    Ok(None)
                }
            }
        }
        Some(Obj::List(items)) => {
            let Some(i) = index.as_int() else {
                return Err(bad_index(container, index, span));
            };
            let items = items.borrow_mut();
            if i < 0 || i as usize >= items.len() {
                return Err(out_of_range(i, items.len(), span));
            }
            let slot = &mut items[i as usize];
            crate::cow::make_unique_val(slot);
            Ok(Some(slot as *mut Val as *mut V))
        }
        _ => {
            *scratch = get_index(container, index, span)?;
            Ok(None)
        }
    }
}

/// `item in container`
pub fn contains(container: V, item: V, span: u32) -> R<bool> {
    let item = item.plain();
    match container.get() {
        Some(Obj::Str(s)) => match item.as_str() {
            Some(x) => Ok(s.contains(x)),
            None => Err(diag(
                Diagnostic::new_spanless("bad_operands").arg("op", "in").arg("a", type_name(item)).arg("b", "str"),
                span,
            )),
        },
        Some(Obj::Map(m)) => Ok(key_of(item).is_some_and(|k| m.borrow().index.contains_key(&k))),
        Some(Obj::Set(s)) => Ok(key_of(item).is_some_and(|k| s.borrow().index.contains_key(&k))),
        Some(Obj::Range(a, b)) => Ok(in_range(item, *a, *b)),
        Some(Obj::List(_) | Obj::Lazy(_)) => {
            let mut it = crate::pipe::iterate(container, span)?;
            while let Some(x) = it.next() {
                if equals(item, x?.v(), span)? {
                    return Ok(true);
                }
            }
            Ok(false)
        }
        _ => Err(diag(Diagnostic::new_spanless("not_iterable").arg("vtype", type_name(container)), span)),
    }
}

pub fn in_range(item: V, a: i64, b: i64) -> bool {
    let lo = V::int(a);
    let hi = V::int(b);
    let num = item.tag == T_INT || item.tag == T_FLOAT || matches!(item.get(), Some(Obj::Decimal(_)));
    num && crate::num::order("<=", lo, item, 0).unwrap_or(false) && crate::num::order("<=", item, hi, 0).unwrap_or(false)
}

/// Make a deep copy (for `old()`): collections and records are copied.
pub fn deep_copy(v: V) -> Val {
    match v.get() {
        Some(Obj::List(items)) => list(items.borrow().iter().map(|x| deep_copy(x.v())).collect()),
        Some(Obj::Map(m)) => {
            let m = m.borrow();
            let mut out = NMap::default();
            for (k, val) in &m.entries {
                out.insert(key_of(k.v()).unwrap(), k.clone(), deep_copy(val.v()));
            }
            Val::new(Obj::Map(Mut::new(out)))
        }
        Some(Obj::Set(s)) => Val::new(Obj::Set(Mut::new(s.borrow().clone()))),
        Some(Obj::Record(r)) => Val::new(Obj::Record(Record {
            ty: r.ty,
            fields: Mut::new(
                r.fields.borrow().iter().map(|x| if x.tag == T_WEAK { x.clone() } else { deep_copy(x.v()) }).collect(),
            ),
            birth: std::cell::Cell::new(crate::state::stamp()),
        })),
        Some(Obj::VariantValue(vv)) => Val::new(Obj::VariantValue(VariantValue {
            variant: vv.variant,
            fields: vv.fields.iter().map(|x| deep_copy(x.v())).collect(),
        })),
        _ => Val::from(v),
    }
}
