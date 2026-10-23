//! User types (records, enums with data, unions of values, traits) and the
//! checks of declared types (D33): a value stored in a typed variable,
//! parameter, field or return value must fit its type; an int is accepted
//! where a float or decimal is declared, and a float literal becomes an
//! exact decimal where a decimal is expected (section 17, rule 1).

use std::cell::RefCell;
use std::collections::HashMap;

use nexo_diag::Diagnostic;

use crate::dec::Dec;
use crate::fmt::{repr, type_name};
use crate::num::{equals, to_decimal};
use crate::state::{diag, st, R};
use crate::value::*;

#[derive(Clone, Copy)]
pub enum TypeRef {
    Builtin(&'static str),
    Record(&'static RecordType),
    Enum(&'static EnumType),
    Alias(&'static AliasType),
    Trait(&'static TraitType),
    ErrorKind(&'static str),
}

pub struct FieldInfo {
    pub name: String,
    /// None for an untyped variant parameter
    pub ty: Option<&'static TypeDesc>,
    pub optional: bool,
    /// a function without parameters that computes the default, or UNDEF
    pub default: V,
}

pub struct RecordType {
    pub id: u32,
    pub name: String,
    pub fields: Vec<FieldInfo>,
    pub methods: RefCell<Vec<(String, V)>>,
    pub traits: RefCell<Vec<&'static TraitType>>,
}

pub struct VariantInfo {
    pub id: u32,
    pub name: String,
    pub params: Vec<FieldInfo>,
    pub enum_ptr: std::cell::Cell<*const EnumType>,
    /// the variant as a value (or constructor)
    pub value: std::cell::Cell<V>,
}

impl VariantInfo {
    pub fn enum_type(&self) -> &'static EnumType {
        unsafe { &*self.enum_ptr.get() }
    }
}

pub struct EnumType {
    pub id: u32,
    pub name: String,
    pub variants: Vec<&'static VariantInfo>,
    pub methods: RefCell<Vec<(String, V)>>,
    pub traits: RefCell<Vec<&'static TraitType>>,
}

pub struct AliasType {
    pub id: u32,
    pub name: String,
    pub target: &'static TypeDesc,
}

pub struct TraitType {
    pub id: u32,
    pub name: String,
    pub methods: Vec<String>,
}

pub const ERROR_KINDS: &[&str] = &[
    "Error",
    "NotFound",
    "IOError",
    "InvalidData",
    "Overflow",
    "DivisionByZero",
    "OutOfRange",
    "ContractBroken",
    "Timeout",
    "InvalidValue",
];

/// A declared type, decoded from the descriptor the compiler emits.
pub enum TypeDesc {
    /// builtin types: int, float, decimal, money, str, bool, list, map, set, any
    Named { name: String, params: Vec<&'static TypeDesc>, optional: bool, display: String },
    /// a user type (record, enum, variant, alias or trait) by its id
    User { id: u32, name: String, optional: bool, display: String },
    /// NotFound, ContractBroken...
    Error { kind: String, optional: bool, display: String },
    /// "active" | "closed"
    Union { options: Vec<V>, display: String },
}

impl TypeDesc {
    pub fn optional(&self) -> bool {
        match self {
            TypeDesc::Named { optional, .. } | TypeDesc::User { optional, .. } | TypeDesc::Error { optional, .. } => {
                *optional
            }
            TypeDesc::Union { .. } => false,
        }
    }

    /// The type as written (`list[int]?`), as in the messages of phase 0.
    pub fn display(&self) -> &str {
        match self {
            TypeDesc::Named { display, .. }
            | TypeDesc::User { display, .. }
            | TypeDesc::Error { display, .. }
            | TypeDesc::Union { display, .. } => display,
        }
    }
}

// ------------------------------------------------------------ descriptors
//
//  T     := 'n' name ';' params opt | 'u' id ':' name ';' opt | 'e' kind ';' opt
//         | 'l' count ';' lit*
//  params:= '[' T { ',' T } ']' | ''
//  opt   := '?' | ''
//  lit   := 's' len ':' bytes | 'i' digits ';' | 'f' text ';' | 'b0' | 'b1' | 'z'
//         | 'q' len ':' text len ':' unit
//  desc  := len ':' display T

struct Reader<'a> {
    s: &'a [u8],
    i: usize,
}

impl<'a> Reader<'a> {
    fn byte(&mut self) -> u8 {
        let b = self.s[self.i];
        self.i += 1;
        b
    }

    fn peek(&self) -> u8 {
        *self.s.get(self.i).unwrap_or(&0)
    }

    fn until(&mut self, end: u8) -> &'a str {
        let start = self.i;
        while self.s[self.i] != end {
            self.i += 1;
        }
        let t = std::str::from_utf8(&self.s[start..self.i]).unwrap();
        self.i += 1;
        t
    }

    fn sized(&mut self) -> &'a str {
        let len: usize = self.until(b':').parse().unwrap();
        let t = std::str::from_utf8(&self.s[self.i..self.i + len]).unwrap();
        self.i += len;
        t
    }

    fn opt(&mut self) -> bool {
        if self.peek() == b'?' {
            self.i += 1;
            true
        } else {
            false
        }
    }

    fn ty(&mut self, display: String) -> TypeDesc {
        match self.byte() {
            b'n' => {
                let name = self.until(b';').to_string();
                let mut params = Vec::new();
                if self.peek() == b'[' {
                    self.i += 1;
                    loop {
                        let d = self.desc();
                        params.push(&*Box::leak(Box::new(d)));
                        if self.byte() == b']' {
                            break;
                        }
                    }
                }
                let optional = self.opt();
                TypeDesc::Named { name, params, optional, display }
            }
            b'u' => {
                let id: u32 = self.until(b':').parse().unwrap();
                let name = self.until(b';').to_string();
                let optional = self.opt();
                TypeDesc::User { id, name, optional, display }
            }
            b'e' => {
                let kind = self.until(b';').to_string();
                let optional = self.opt();
                TypeDesc::Error { kind, optional, display }
            }
            b'l' => {
                let n: usize = self.until(b';').parse().unwrap();
                let options = (0..n).map(|_| self.lit()).collect();
                TypeDesc::Union { options, display }
            }
            other => panic!("bad type descriptor {}", other as char),
        }
    }

    fn desc(&mut self) -> TypeDesc {
        let display = self.sized().to_string();
        self.ty(display)
    }

    fn lit(&mut self) -> V {
        match self.byte() {
            b's' => V::str(self.sized()),
            b'i' => V::int(self.until(b';').parse().unwrap()),
            b'f' => V::float(self.until(b';').parse().unwrap()),
            b'b' => V::bool(self.byte() == b'1'),
            b'z' => NONE,
            b'q' => {
                let text = self.sized().to_string();
                let unit = self.sized().to_string();
                crate::num::unit_literal(&text, &unit)
            }
            other => panic!("bad literal {}", other as char),
        }
    }
}

thread_local! {
    static DESCS: RefCell<HashMap<usize, &'static TypeDesc>> = RefCell::new(HashMap::new());
}

/// Decode (once) the type descriptor at `ptr`.
pub fn desc(ptr: *const u8, len: usize) -> &'static TypeDesc {
    let key = ptr as usize;
    if let Some(d) = DESCS.with(|m| m.borrow().get(&key).copied()) {
        return d;
    }
    let bytes = unsafe { std::slice::from_raw_parts(ptr, len) };
    let d: &'static TypeDesc = Box::leak(Box::new(Reader { s: bytes, i: 0 }.desc()));
    DESCS.with(|m| m.borrow_mut().insert(key, d));
    d
}

pub fn lookup(id: u32) -> Option<TypeRef> {
    st().types.get(&id).copied()
}

pub fn register(id: u32, t: TypeRef) {
    st().types.insert(id, t);
}

// ------------------------------------------------------------ conformance

fn same_kind(a: V, b: V) -> bool {
    match (a.tag, b.tag) {
        (T_OBJ, T_OBJ) => std::mem::discriminant(a.get().unwrap()) == std::mem::discriminant(b.get().unwrap()),
        (x, y) => x == y,
    }
}

fn union_values(options: &[V]) -> String {
    options.iter().map(|o| repr(*o)).collect::<Vec<_>>().join(", ")
}

/// Check `value` against a declared type and apply the conversions of
/// section 17. Returns the value to store.
pub fn conform(value: V, t: &'static TypeDesc, name: &str, span: u32) -> R<V> {
    let value = if value.tag == T_OBJ && matches!(value.get(), Some(Obj::Lazy(_))) {
        crate::pipe::collect(value)?
    } else {
        value
    };
    if let TypeDesc::Union { options, display } = t {
        let plain = value.plain();
        for o in options {
            if same_kind(*o, plain) && equals(*o, plain, span)? {
                return Ok(plain);
            }
        }
        return Err(diag(
            Diagnostic::new_spanless("type_mismatch")
                .fix("type_mismatch_union")
                .arg("name", name)
                .arg("type", display.clone())
                .arg("value", repr(plain))
                .arg("vtype", type_name(value))
                .arg("values", union_values(options)),
            span,
        ));
    }
    if value.is_none() {
        if t.optional() {
            return Ok(NONE);
        }
        return Err(diag(
            Diagnostic::new_spanless("none_not_allowed").arg("name", name).arg("type", t.display()),
            span,
        ));
    }
    if let Some(v) = fits(value, t, span)? {
        return Ok(v);
    }
    let shown = t.display().trim_end_matches('?').to_string();
    if let TypeDesc::User { id, .. } = t {
        if let Some(TypeRef::Alias(a)) = lookup(*id) {
            if let TypeDesc::Union { options, .. } = a.target {
                return Err(diag(
                    Diagnostic::new_spanless("type_mismatch")
                        .fix("type_mismatch_union")
                        .arg("name", name)
                        .arg("type", shown)
                        .arg("value", repr(value.plain()))
                        .arg("vtype", type_name(value))
                        .arg("values", union_values(options)),
                    span,
                ));
            }
        }
    }
    Err(diag(
        Diagnostic::new_spanless("type_mismatch")
            .arg("name", name)
            .arg("type", shown)
            .arg("value", repr(value.plain()))
            .arg("vtype", type_name(value)),
        span,
    ))
}

pub fn builtin_is(v: V, name: &str) -> bool {
    match name {
        "int" => v.tag == T_INT,
        "float" => v.tag == T_FLOAT || v.tag == T_LITFLOAT,
        "bool" => v.tag == T_BOOL,
        _ => match (name, v.get()) {
            ("decimal", Some(Obj::Decimal(_))) => true,
            ("money", Some(Obj::Money(..))) => true,
            ("str", Some(Obj::Str(_))) => true,
            ("list", Some(Obj::List(_) | Obj::Lazy(_))) => true,
            ("map", Some(Obj::Map(_))) => true,
            ("set", Some(Obj::Set(_))) => true,
            _ => false,
        },
    }
}

fn fits_or_none(v: V, t: &'static TypeDesc, span: u32) -> R<bool> {
    if v.is_none() {
        return Ok(t.optional());
    }
    if let TypeDesc::Union { .. } = t {
        return match conform(v, t, "x", span) {
            Ok(_) => Ok(true),
            Err(_) => {
                crate::state::clear();
                Ok(false)
            }
        };
    }
    Ok(fits(v, t, span)?.is_some())
}

/// Some(converted value) when `v` fits the type.
pub fn fits(v: V, t: &'static TypeDesc, span: u32) -> R<Option<V>> {
    match t {
        TypeDesc::Named { name, params, .. } => {
            let name = name.as_str();
            match name {
                "any" => Ok(Some(v.plain())),
                "int" => Ok((v.tag == T_INT).then_some(v)),
                "float" => Ok(match v.tag {
                    T_FLOAT | T_LITFLOAT => Some(v.plain()),
                    T_INT => Some(V::float(v.bits as i64 as f64)),
                    _ => None,
                }),
                "decimal" => Ok(match v.tag {
                    T_LITFLOAT | T_INT => Some(decimal(to_decimal(v).unwrap())),
                    T_OBJ if matches!(v.get(), Some(Obj::Decimal(_))) => Some(v),
                    _ => None,
                }),
                "str" | "bool" | "money" | "map" | "set" => {
                    if !builtin_is(v, name) {
                        return Ok(None);
                    }
                    if name == "map" && params.len() == 2 {
                        if let Some(Obj::Map(m)) = v.get() {
                            let entries: Vec<(V, V)> = m.borrow().entries.clone();
                            for (k, val) in entries {
                                if !fits_or_none(k, params[0], span)? || !fits_or_none(val, params[1], span)? {
                                    return Ok(None);
                                }
                            }
                        }
                    }
                    if name == "set" && !params.is_empty() {
                        if let Some(Obj::Set(s)) = v.get() {
                            let items: Vec<V> = s.borrow().items.clone();
                            for item in items {
                                if !fits_or_none(item, params[0], span)? {
                                    return Ok(None);
                                }
                            }
                        }
                    }
                    Ok(Some(v))
                }
                "list" => {
                    let v = if matches!(v.get(), Some(Obj::Lazy(_))) { crate::pipe::collect(v)? } else { v };
                    let Some(Obj::List(items)) = v.get() else { return Ok(None) };
                    if params.is_empty() {
                        return Ok(Some(v));
                    }
                    let p = params[0];
                    let items: Vec<V> = items.borrow().clone();
                    let mut out = Vec::with_capacity(items.len());
                    for item in items {
                        if item.is_none() {
                            if !p.optional() {
                                return Ok(None);
                            }
                            out.push(NONE);
                            continue;
                        }
                        if let TypeDesc::Union { .. } = p {
                            out.push(item);
                            continue;
                        }
                        match fits(item, p, span)? {
                            Some(c) => out.push(c),
                            None => return Ok(None),
                        }
                    }
                    Ok(Some(list(out)))
                }
                _ => Ok(None),
            }
        }
        TypeDesc::User { id, name, .. } => {
            let Some(kind) = lookup(*id) else {
                return Err(diag(Diagnostic::new_spanless("unknown_type").arg("name", name.clone()), span));
            };
            if let TypeRef::Alias(a) = kind {
                if let TypeDesc::Union { .. } = a.target {
                    return match conform(v, a.target, "x", span) {
                        Ok(c) => Ok(Some(c)),
                        Err(_) => {
                            crate::state::clear();
                            Ok(None)
                        }
                    };
                }
                return fits(v, a.target, span);
            }
            Ok(is_type(v, kind, span)?.then_some(v))
        }
        TypeDesc::Error { kind, .. } => Ok(is_error_kind(v, kind).then_some(v)),
        TypeDesc::Union { .. } => match conform(v, t, "x", span) {
            Ok(c) => Ok(Some(c)),
            Err(_) => {
                crate::state::clear();
                Ok(None)
            }
        },
    }
}

pub fn is_error_kind(v: V, kind: &str) -> bool {
    matches!(v.get(), Some(Obj::Error(e)) if kind == "Error" || e.kind == kind)
}

fn traits_of(v: V) -> Option<Vec<&'static TraitType>> {
    match v.get()? {
        Obj::Record(r) => Some(r.ty.traits.borrow().clone()),
        Obj::VariantValue(vv) => Some(vv.variant.enum_type().traits.borrow().clone()),
        _ => None,
    }
}

/// `value is T` for a resolved type.
pub fn is_type(v: V, t: TypeRef, span: u32) -> R<bool> {
    Ok(match t {
        TypeRef::Builtin(name) => builtin_is(v, name),
        TypeRef::Record(rt) => matches!(v.get(), Some(Obj::Record(r)) if std::ptr::eq(r.ty, rt)),
        TypeRef::Enum(et) => match v.get() {
            Some(Obj::VariantValue(vv)) => std::ptr::eq(vv.variant.enum_type(), et),
            Some(Obj::Variant(var)) => std::ptr::eq(var.enum_type(), et),
            _ => false,
        },
        TypeRef::Trait(tt) => traits_of(v).is_some_and(|ts| ts.iter().any(|x| std::ptr::eq(*x, tt))),
        TypeRef::ErrorKind(k) => is_error_kind(v, k),
        TypeRef::Alias(a) => match conform(v, a.target, "x", span) {
            Ok(_) => true,
            Err(_) => {
                crate::state::clear();
                false
            }
        },
    })
}

/// `value is X` where X is any value: a type, a variant or none.
pub fn is_value(v: V, t: V, span: u32) -> R<bool> {
    if t.is_none() {
        return Ok(v.is_none());
    }
    match t.get() {
        Some(Obj::Type(tr)) => is_type(v, *tr, span),
        Some(Obj::Variant(var)) => Ok(match v.get() {
            Some(Obj::Variant(x)) => std::ptr::eq(*x, *var),
            Some(Obj::VariantValue(vv)) => std::ptr::eq(vv.variant, *var),
            _ => false,
        }),
        _ => Err(diag(Diagnostic::new_spanless("not_a_type_value").arg("vtype", type_name(t)), span)),
    }
}

/// The value of an int or a float literal as a decimal (for `decimal:`).
pub fn exact(v: V) -> Option<Dec> {
    to_decimal(v)
}
