//! Runtime values and the memory model of section 7.
//!
//! A value is two machine words, `{tag, bits}`, passed in registers by the
//! compiled code. Small values (none, bool, int, float) live in the words;
//! everything else is a pointer to a heap object with a reference count
//! (rule 4). Counts are atomic, so values can travel between the tasks of
//! a `parallel` block. Collections are values: they are copied only when
//! they are changed while shared (rule 2, copy-on-write, see `cow.rs`).
//!
//! `V` is a borrowed value (Copy); `Val` owns one reference and releases it
//! when dropped. Containers hold `Val`s. The compiled code follows the same
//! convention: runtime functions borrow their arguments and return owned
//! values.

use std::cell::{Cell as StdCell, RefCell, UnsafeCell};
use std::collections::HashMap;
use std::mem::ManuallyDrop;
use std::sync::atomic::{fence, AtomicBool, AtomicI64, AtomicU64, Ordering};

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
/// A `weak` field of a record: a reference that does not keep its target
/// alive. It never leaves the runtime (reading the field upgrades it).
pub const T_WEAK: u64 = 8;

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

// ------------------------------------------------------------ heap objects

/// Every heap object starts with its counts. `rc` counts owners; `weak`
/// counts weak references plus one held by all the owners together (as
/// Rust's `Arc`): the object is destroyed when `rc` reaches 0 and its
/// memory is returned when `weak` does.
#[repr(C)]
pub struct Heap {
    pub rc: AtomicU64,
    pub weak: AtomicU64,
    pub obj: ManuallyDrop<Obj>,
}

/// Objects created once for the whole program (constants of the compiled
/// code, types, variants) are never freed.
pub const IMMORTAL: u64 = 1 << 62;

static LEAKCHECK: AtomicBool = AtomicBool::new(false);
static LIVE: AtomicI64 = AtomicI64::new(0);

/// Count live objects (NEXO_LEAKCHECK=1): at the end of the program every
/// object must have been freed.
pub fn enable_leakcheck() {
    LEAKCHECK.store(true, Ordering::Relaxed);
}

pub fn leakcheck_enabled() -> bool {
    LEAKCHECK.load(Ordering::Relaxed)
}

pub fn live_objects() -> i64 {
    LIVE.load(Ordering::SeqCst)
}

fn count(delta: i64) {
    if LEAKCHECK.load(Ordering::Relaxed) {
        LIVE.fetch_add(delta, Ordering::Relaxed);
    }
}

fn alloc(o: Obj, rc: u64) -> *mut Heap {
    Box::into_raw(Box::new(Heap { rc: AtomicU64::new(rc), weak: AtomicU64::new(1), obj: ManuallyDrop::new(o) }))
}

#[inline]
fn heap(v: V) -> &'static Heap {
    unsafe { &*(v.bits as *const Heap) }
}

/// Take one more reference.
#[inline]
pub fn retain(v: V) {
    match v.tag {
        T_OBJ => {
            heap(v).rc.fetch_add(1, Ordering::Relaxed);
        }
        T_WEAK => {
            heap(v).weak.fetch_add(1, Ordering::Relaxed);
        }
        _ => {}
    }
}

/// Give a reference back; the last one destroys the object.
#[inline]
pub fn release(v: V) {
    match v.tag {
        T_OBJ => {
            if heap(v).rc.fetch_sub(1, Ordering::Release) == 1 {
                fence(Ordering::Acquire);
                destroy_later(v.bits as *mut Heap);
            }
        }
        T_WEAK => release_weak(v.bits as *mut Heap),
        _ => {}
    }
}

fn release_weak(h: *mut Heap) {
    if unsafe { &*h }.weak.fetch_sub(1, Ordering::Release) == 1 {
        fence(Ordering::Acquire);
        unsafe { drop(Box::from_raw(h)) };
    }
}

thread_local! {
    static FREEING: StdCell<bool> = const { StdCell::new(false) };
    static PENDING: RefCell<Vec<*mut Heap>> = const { RefCell::new(Vec::new()) };
}

/// Destroy an object whose count reached 0. Its contents are released
/// with a work list rather than recursively, so a long chain of records
/// does not exhaust the stack.
pub fn destroy_later(h: *mut Heap) {
    if FREEING.with(|f| f.get()) {
        PENDING.with(|p| p.borrow_mut().push(h));
        return;
    }
    FREEING.with(|f| f.set(true));
    destroy(h);
    while let Some(next) = PENDING.with(|p| p.borrow_mut().pop()) {
        destroy(next);
    }
    FREEING.with(|f| f.set(false));
}

fn destroy(h: *mut Heap) {
    count(-1);
    unsafe { ManuallyDrop::drop(&mut (*h).obj) };
    release_weak(h);
}

/// A weak reference to a record (the value of a `weak` field).
pub fn downgrade(v: V) -> V {
    if v.tag != T_OBJ {
        return v;
    }
    heap(v).weak.fetch_add(1, Ordering::Relaxed);
    V { tag: T_WEAK, bits: v.bits }
}

/// The target of a weak reference, or none when it was freed.
pub fn upgrade(w: V) -> Val {
    if w.tag != T_WEAK {
        return Val::from(w);
    }
    let rc = &heap(w).rc;
    let mut n = rc.load(Ordering::Relaxed);
    loop {
        if n == 0 {
            return Val::none();
        }
        match rc.compare_exchange_weak(n, n + 1, Ordering::Acquire, Ordering::Relaxed) {
            Ok(_) => return Val(V { tag: T_OBJ, bits: w.bits }),
            Err(x) => n = x,
        }
    }
}

/// Number of owners of a heap value (1: nobody else can see it).
#[inline]
pub fn owners(v: V) -> u64 {
    if v.tag == T_OBJ {
        heap(v).rc.load(Ordering::Acquire)
    } else {
        0
    }
}

/// Never free this object (a constant of the program).
pub fn make_immortal(v: V) {
    if v.tag == T_OBJ && heap(v).rc.load(Ordering::Relaxed) < IMMORTAL {
        heap(v).rc.store(IMMORTAL, Ordering::Relaxed);
        count(-1);
    }
}

// ------------------------------------------------------------- owned values

/// An owned reference to a value.
#[repr(transparent)]
#[derive(Debug)]
pub struct Val(V);

unsafe impl Send for Val {}
unsafe impl Sync for Val {}

impl Val {
    pub fn new(o: Obj) -> Val {
        count(1);
        Val(V { tag: T_OBJ, bits: alloc(o, 1) as u64 })
    }

    /// An object that is never freed.
    pub fn immortal(o: Obj) -> Val {
        Val(V { tag: T_OBJ, bits: alloc(o, IMMORTAL) as u64 })
    }

    /// Own a value that is already owned (no new reference).
    ///
    /// # Safety
    /// The caller gives its reference away.
    pub unsafe fn adopt(v: V) -> Val {
        Val(v)
    }

    /// A new reference to a borrowed value.
    pub fn from(v: V) -> Val {
        retain(v);
        Val(v)
    }

    #[inline]
    pub fn v(&self) -> V {
        self.0
    }

    /// Give the reference to the caller (the compiled code).
    pub fn into_raw(self) -> V {
        let v = self.0;
        std::mem::forget(self);
        v
    }

    pub const fn simple(v: V) -> Val {
        Val(v)
    }

    pub const fn none() -> Val {
        Val(NONE)
    }

    pub fn int(i: i64) -> Val {
        Val(V::int(i))
    }

    pub fn float(f: f64) -> Val {
        Val(V::float(f))
    }

    pub fn bool(b: bool) -> Val {
        Val(V::bool(b))
    }

    pub fn str(s: impl Into<String>) -> Val {
        Val::new(Obj::Str(s.into()))
    }

    /// Drop the literal tag: the value is now an ordinary float.
    pub fn plain(self) -> Val {
        if self.0.tag == T_LITFLOAT {
            Val(V::float(self.0.litfloat().value))
        } else {
            self
        }
    }
}

impl Clone for Val {
    fn clone(&self) -> Val {
        Val::from(self.0)
    }
}

impl Drop for Val {
    #[inline]
    fn drop(&mut self) {
        release(self.0);
    }
}

impl std::ops::Deref for Val {
    type Target = V;
    fn deref(&self) -> &V {
        &self.0
    }
}

/// Own every value of a slice.
pub fn own_all(vs: &[V]) -> Vec<Val> {
    vs.iter().map(|v| Val::from(*v)).collect()
}

/// Borrow every value of a slice.
pub fn raw_all(vs: &[Val]) -> Vec<V> {
    vs.iter().map(|v| v.v()).collect()
}

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
            Some(&heap(self).obj)
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

/// Interior mutability without run-time checks. A value is only changed
/// when its owner holds the only reference (copy-on-write) or, for
/// records, by the task that created it (see `cow::check_write`), so no
/// two threads touch the same contents at once.
pub struct Mut<T>(UnsafeCell<T>);

unsafe impl<T> Sync for Mut<T> {}
unsafe impl<T> Send for Mut<T> {}

impl<T> Mut<T> {
    pub fn new(t: T) -> Mut<T> {
        Mut(UnsafeCell::new(t))
    }

    #[inline]
    pub fn borrow(&self) -> &T {
        unsafe { &*self.0.get() }
    }

    #[allow(clippy::mut_from_ref)]
    #[inline]
    pub fn borrow_mut(&self) -> &mut T {
        unsafe { &mut *self.0.get() }
    }
}

impl<T: Default> Default for Mut<T> {
    fn default() -> Mut<T> {
        Mut::new(T::default())
    }
}

/// A builtin function of the standard library (see stdlib.rs).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BuiltinId(pub u32);

/// A captured variable: a heap cell shared by a function and its closures.
/// The compiled code reads and writes `value` through the cell pointer.
#[repr(C)]
pub struct CellBox {
    pub value: V,
    pub rc: AtomicU64,
    /// the task that created it may change it (read by the compiled code
    /// at offset 24, see cow.rs)
    pub birth: u64,
}

pub type Cell = *mut CellBox;

pub fn new_cell() -> Cell {
    count(1);
    Box::into_raw(Box::new(CellBox { value: UNDEF, rc: AtomicU64::new(1), birth: crate::state::stamp() }))
}

pub fn retain_cell(c: Cell) {
    if !c.is_null() {
        unsafe { &*c }.rc.fetch_add(1, Ordering::Relaxed);
    }
}

pub fn release_cell(c: Cell) {
    if c.is_null() {
        return;
    }
    if unsafe { &*c }.rc.fetch_sub(1, Ordering::Release) == 1 {
        fence(Ordering::Acquire);
        count(-1);
        let b = unsafe { Box::from_raw(c) };
        release(b.value);
    }
}

/// Signature of compiled functions: the thread's state, the closure and
/// the arguments in parameter order (MISSING where a default applies),
/// plus the call span. Arguments are borrowed; the result is owned.
pub type Code = unsafe extern "C" fn(*mut crate::state::Tls, *const Func, *const V, u32) -> V;

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

unsafe impl Sync for FnInfo {}

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
/// variables it captured. The compiled code reads `env` at offset 16.
#[repr(C)]
pub struct Func {
    pub code: Code,
    pub info: &'static FnInfo,
    pub env: *const Cell,
    pub nenv: u64,
}

impl Drop for Func {
    fn drop(&mut self) {
        if self.nenv == 0 {
            return;
        }
        let env = unsafe { Box::from_raw(std::ptr::slice_from_raw_parts_mut(self.env as *mut Cell, self.nenv as usize)) };
        for c in env.iter() {
            release_cell(*c);
        }
    }
}

pub struct ErrorValue {
    pub kind: String,
    pub message: String,
    pub cause: Val,
    pub line: Option<u32>,
    pub span: Option<u32>,
    /// catalog key of the fix sentence
    pub fix: Option<&'static str>,
}

/// The hash of map keys: the multiply-and-rotate hash of rustc (keys are
/// short and the tables are the program's own, so speed matters more than
/// resistance to chosen collisions).
#[derive(Default, Clone, Copy)]
pub struct KeyHasher(u64);

impl KeyHasher {
    #[inline]
    fn add(&mut self, x: u64) {
        self.0 = (self.0.rotate_left(5) ^ x).wrapping_mul(0x51_7c_c1_b7_27_22_0a_95);
    }
}

impl std::hash::Hasher for KeyHasher {
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        let mut chunks = bytes.chunks_exact(8);
        for c in &mut chunks {
            self.add(u64::from_le_bytes(c.try_into().unwrap()));
        }
        let rest = chunks.remainder();
        if !rest.is_empty() {
            let mut b = [0u8; 8];
            b[..rest.len()].copy_from_slice(rest);
            self.add(u64::from_le_bytes(b));
        }
    }
    #[inline]
    fn write_u8(&mut self, i: u8) {
        self.add(i as u64);
    }
    #[inline]
    fn write_u64(&mut self, i: u64) {
        self.add(i);
    }
    #[inline]
    fn write_i64(&mut self, i: i64) {
        self.add(i as u64);
    }
    #[inline]
    fn write_usize(&mut self, i: usize) {
        self.add(i as u64);
    }
    #[inline]
    fn write_isize(&mut self, i: isize) {
        self.add(i as u64);
    }
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
}

pub type KeyIndex = HashMap<Key, usize, std::hash::BuildHasherDefault<KeyHasher>>;

/// An insertion-ordered map. Keys are simple values (D35).
#[derive(Default, Clone)]
pub struct NMap {
    pub entries: Vec<(Val, Val)>,
    pub index: KeyIndex,
}

/// An insertion-ordered set.
#[derive(Default, Clone)]
pub struct NSet {
    pub items: Vec<Val>,
    pub index: KeyIndex,
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
    pub fields: Mut<Vec<Val>>,
    /// the task that created it may change it (see `cow::check_write`)
    pub birth: std::cell::Cell<u64>,
}

pub struct VariantValue {
    pub variant: &'static VariantInfo,
    pub fields: Vec<Val>,
}

pub enum Obj {
    Str(String),
    Decimal(Dec),
    Money(Dec, &'static str),
    /// a number with a unit: 30s, 5 km, 512 MB, 10%
    Quantity(Dec, &'static str),
    List(Mut<Vec<Val>>),
    Map(Mut<NMap>),
    Set(Mut<NSet>),
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
    Bound(Val, Val),
    BuiltinMethod(Val, &'static str),
    Builtin(BuiltinId),
    Type(TypeRef),
    Currency(&'static str),
    /// half_up, half_even
    Symbol(&'static str),
    /// json, csv
    Namespace(&'static str),
    /// a channel of `nexo.sync`
    Channel(crate::sync::Channel),
    /// `channel` and `channel[T]`: make channels
    ChannelType(Option<Val>),
}

pub fn list(items: Vec<Val>) -> Val {
    Val::new(Obj::List(Mut::new(items)))
}

pub fn decimal(d: Dec) -> Val {
    Val::new(Obj::Decimal(d))
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
