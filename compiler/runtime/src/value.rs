//! Runtime values.
//!
//! A value is two machine words, `{tag, bits}`, passed in registers by the
//! compiled code. Small values (none, bool, int, float) live in the words;
//! everything else is a pointer to a heap object. Phase 1 never frees heap
//! objects: the memory model of section 7 (ownership, copy-on-write and
//! reference counting) is phase 2.

use std::cell::RefCell;
use std::collections::HashMap;

use crate::dec::Dec;
use crate::types::{EnumType, RecordType, TraitType, TypeRef, VariantInfo};

pub const T_NONE: u64 = 0;
pub const T_BOOL: u64 = 1;
pub const T_INT: u64 = 2;
pub const T_FLOAT: u64 = 3;
pub const T_OBJ: u64 = 4;
/// A float literal that has not been bound yet (section 17, rule 1): next
/// to a decimal it is read exactly from its source text.
pub const T_LITFLOAT: u64 = 5;
/// A variable that has not been assigned yet.
pub const T_UNDEF: u64 = 6;
/// An argument that was not passed (its default is evaluated by the callee).
pub const T_MISSING: u64 = 7;

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct V {
    pub tag: u64,
    pub bits: u64,
}

/// What a float literal points to: its value and its source text.
#[repr(C)]
pub struct LitFloat {
    pub value: f64,
    pub text: *const u8,
    pub len: usize,
}

impl LitFloat {
    pub fn text(&self) -> &str {
        unsafe { std::str::from_utf8_unchecked(std::slice::from_raw_parts(self.text, self.len)) }
    }
}

pub const NONE: V = V { tag: T_NONE, bits: 0 };
pub const UNDEF: V = V { tag: T_UNDEF, bits: 0 };
pub const MISSING: V = V { tag: T_MISSING, bits: 0 };

impl V {
    pub fn int(i: i64) -> V {
        V { tag: T_INT, bits: i as u64 }
    }

    pub fn float(f: f64) -> V {
        V { tag: T_FLOAT, bits: f.to_bits() }
    }

    pub fn bool(b: bool) -> V {
        V { tag: T_BOOL, bits: b as u64 }
    }

    pub fn obj(o: Obj) -> V {
        let p: &'static Obj = Box::leak(Box::new(o));
        V { tag: T_OBJ, bits: p as *const Obj as u64 }
    }

    pub fn from_static(o: &'static Obj) -> V {
        V { tag: T_OBJ, bits: o as *const Obj as u64 }
    }

    pub fn str(s: impl Into<String>) -> V {
        V::obj(Obj::Str(s.into()))
    }

    pub fn is_none(self) -> bool {
        self.tag == T_NONE
    }

    pub fn as_int(self) -> Option<i64> {
        (self.tag == T_INT).then_some(self.bits as i64)
    }

    pub fn as_float(self) -> Option<f64> {
        match self.tag {
            T_FLOAT => Some(f64::from_bits(self.bits)),
            T_LITFLOAT => Some(self.litfloat().value),
            _ => None,
        }
    }

    pub fn as_bool(self) -> Option<bool> {
        (self.tag == T_BOOL).then_some(self.bits != 0)
    }

    pub fn litfloat(self) -> &'static LitFloat {
        unsafe { &*(self.bits as *const LitFloat) }
    }

    pub fn get(self) -> Option<&'static Obj> {
        if self.tag == T_OBJ {
            Some(unsafe { &*(self.bits as *const Obj) })
        } else {
            None
        }
    }

    pub fn as_str(self) -> Option<&'static str> {
        match self.get() {
            Some(Obj::Str(s)) => Some(s.as_str()),
            _ => None,
        }
    }

    pub fn same(self, other: V) -> bool {
        self.tag == other.tag && self.bits == other.bits
    }

    /// Drop the literal tag: the value is now an ordinary float.
    pub fn plain(self) -> V {
        if self.tag == T_LITFLOAT {
            V::float(self.litfloat().value)
        } else {
            self
        }
    }
}

/// A builtin function of the standard library (see stdlib.rs).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BuiltinId(pub u32);

/// A captured variable: a heap cell shared by a function and its closures.
pub type Cell = *mut V;

/// Signature of compiled functions: the closure and the arguments in
/// parameter order (MISSING where a default applies), plus the call span.
pub type Code = unsafe extern "C" fn(*const Func, *const V, u32) -> V;

#[repr(C)]
pub struct NameRef {
    pub ptr: *const u8,
    pub len: usize,
}

impl NameRef {
    pub fn get(&self) -> Option<&'static str> {
        if self.ptr.is_null() {
            None
        } else {
            Some(unsafe { std::str::from_utf8_unchecked(std::slice::from_raw_parts(self.ptr, self.len)) })
        }
    }
}

#[repr(C)]
pub struct ParamInfo {
    pub name: NameRef,
    /// 1 when the parameter has a default or an optional type
    pub optional: u64,
}

/// Static description of a compiled function, emitted by the compiler.
#[repr(C)]
pub struct FnInfo {
    pub name: NameRef,
    pub nparams: u64,
    pub params: *const ParamInfo,
}

impl FnInfo {
    pub fn name(&self) -> &'static str {
        self.name.get().unwrap_or("lambda")
    }

    pub fn params(&self) -> &'static [ParamInfo] {
        if self.nparams == 0 {
            return &[];
        }
        unsafe { std::slice::from_raw_parts(self.params, self.nparams as usize) }
    }
}

/// A function value: compiled code, its description and the cells of the
/// variables it captured.
#[repr(C)]
pub struct Func {
    pub code: Code,
    pub info: &'static FnInfo,
    pub env: *const Cell,
    pub nenv: u64,
}

pub struct ErrorValue {
    pub kind: String,
    pub message: String,
    pub cause: V,
    pub line: Option<u32>,
    pub span: Option<u32>,
    /// catalog key of the fix sentence
    pub fix: Option<&'static str>,
}

/// An insertion-ordered map. Keys are simple values (D35).
#[derive(Default)]
pub struct NMap {
    pub entries: Vec<(V, V)>,
    pub index: HashMap<Key, usize>,
}

/// An insertion-ordered set.
#[derive(Default)]
pub struct NSet {
    pub items: Vec<V>,
    pub index: HashMap<Key, usize>,
}

/// Identity of a map key or set element: values that are equal as keys
/// have the same Key (1 and 1.0 are the same key; true and 1 are not).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Key {
    None,
    Bool(bool),
    Int(i64),
    Float(u64),
    Str(String),
    Dec(String),
    Money(String, &'static str),
    Qty(String, &'static str),
    Variant(usize),
}

pub struct Record {
    pub ty: &'static RecordType,
    pub fields: RefCell<Vec<V>>,
}

pub struct VariantValue {
    pub variant: &'static VariantInfo,
    pub fields: Vec<V>,
}

pub enum Obj {
    Str(String),
    Decimal(Dec),
    Money(Dec, &'static str),
    /// a number with a unit: 30s, 5 km, 512 MB, 10%
    Quantity(Dec, &'static str),
    List(RefCell<Vec<V>>),
    Map(RefCell<NMap>),
    Set(RefCell<NSet>),
    /// inclusive range
    Range(i64, i64),
    /// a lazy sequence: a pipeline or read_lines (section 6, rule 4)
    Lazy(crate::pipe::Lazy),
    /// result of `group by`, waiting for sum, count or avg
    Grouped(crate::pipe::Grouped),
    Record(Record),
    VariantValue(VariantValue),
    /// a variant of an enum: a value when it has no fields, a constructor
    /// when it has some
    Variant(&'static VariantInfo),
    Error(ErrorValue),
    Func(Func),
    /// a method bound to its receiver, or a builtin method of a value
    Bound(V, V),
    BuiltinMethod(V, &'static str),
    Builtin(BuiltinId),
    Type(TypeRef),
    Currency(&'static str),
    /// half_up, half_even
    Symbol(&'static str),
    /// json, csv
    Namespace(&'static str),
}

pub fn list(items: Vec<V>) -> V {
    V::obj(Obj::List(RefCell::new(items)))
}

pub fn decimal(d: Dec) -> V {
    V::obj(Obj::Decimal(d))
}

pub fn record_type_name(r: &RecordType) -> &str {
    &r.name
}

pub fn enum_of(v: &VariantInfo) -> &'static EnumType {
    v.enum_type()
}

pub fn trait_name(t: &TraitType) -> &str {
    &t.name
}
