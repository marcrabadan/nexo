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

/// Freed heap objects kept for the next ones, per thread (D134): every
/// object has the same size, so a list of free blocks serves them all
/// without asking malloc. Returned to malloc when the thread ends.
struct Pool(UnsafeCell<Vec<*mut Heap>>);

const POOL_MAX: usize = 4096;

impl Drop for Pool {
    fn drop(&mut self) {
        for h in self.0.get_mut().drain(..) {
            unsafe { std::alloc::dealloc(h as *mut u8, std::alloc::Layout::new::<Heap>()) };
        }
    }
}

thread_local! {
    static POOL: Pool = const { Pool(UnsafeCell::new(Vec::new())) };
}

fn alloc(o: Obj, rc: u64) -> *mut Heap {
    let heap = Heap { rc: AtomicU64::new(rc), weak: AtomicU64::new(1), obj: ManuallyDrop::new(o) };
    let block = POOL.try_with(|p| unsafe { (*p.0.get()).pop() }).ok().flatten();
    match block {
        Some(h) => {
            unsafe { std::ptr::write(h, heap) };
            h
        }
        None => Box::into_raw(Box::new(heap)),
    }
}

/// The memory of an object whose contents are gone.
fn free_block(h: *mut Heap) {
    let kept = POOL
        .try_with(|p| {
            let v = unsafe { &mut *p.0.get() };
            if v.len() < POOL_MAX {
                v.push(h);
                true
            } else {
                false
            }
        })
        .unwrap_or(false);
    if !kept {
        unsafe { std::alloc::dealloc(h as *mut u8, std::alloc::Layout::new::<Heap>()) };
    }
}

#[inline]
fn heap(v: V) -> &'static Heap {
    unsafe { &*(v.bits as *const Heap) }
}

/// Whether a second thread may touch values (D126). Until the program
/// starts one (`parallel`, `spawn`, a server, a stream), counts change
/// without atomic instructions; the flag is set before the first such
/// thread starts and is never cleared, so every later change is atomic.
#[no_mangle]
pub static nx_multi: AtomicBool = AtomicBool::new(false);

/// Called before starting a thread that may touch values.
pub fn going_multi() {
    nx_multi.store(true, Ordering::SeqCst);
}

#[inline]
fn single() -> bool {
    !nx_multi.load(Ordering::Relaxed)
}

/// Take one more reference.
#[inline]
pub fn retain(v: V) {
    match v.tag {
        T_OBJ => {
            let rc = &heap(v).rc;
            if single() {
                rc.store(rc.load(Ordering::Relaxed) + 1, Ordering::Relaxed);
            } else {
                rc.fetch_add(1, Ordering::Relaxed);
            }
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
            let rc = &heap(v).rc;
            if single() {
                let n = rc.load(Ordering::Relaxed);
                rc.store(n - 1, Ordering::Relaxed);
                if n == 1 {
                    destroy_later(v.bits as *mut Heap);
                }
            } else if rc.fetch_sub(1, Ordering::Release) == 1 {
                fence(Ordering::Acquire);
                destroy_later(v.bits as *mut Heap);
            }
        }
        T_WEAK => release_weak(v.bits as *mut Heap),
        _ => {}
    }
}

fn release_weak(h: *mut Heap) {
    // with no weak references there is nobody to race with: a weak
    // reference is only made from a strong one, and there is none left
    let weak = &unsafe { &*h }.weak;
    if weak.load(Ordering::Acquire) == 1 || weak.fetch_sub(1, Ordering::Release) == 1 {
        fence(Ordering::Acquire);
        free_block(h);
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
    // a small record of plain values owns nothing else: its memory goes
    // back at once (D134)
    if let Obj::Record(r) = unsafe { &*(*h).obj } {
        if let Fields::Inline(n, vals) = r.fields.borrow() {
            if vals[..*n as usize].iter().all(|v| v.tag != T_OBJ && v.tag != T_WEAK) {
                count(-1);
                release_weak(h);
                return;
            }
        }
    }
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

/// Hash of an int key: one multiplication spreads it over the high bits
/// that the table looks at first.
#[derive(Default)]
pub struct IntHasher(u64);

impl std::hash::Hasher for IntHasher {
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        for b in bytes {
            self.0 = (self.0 ^ *b as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15);
        }
    }
    #[inline]
    fn write_i64(&mut self, i: i64) {
        self.0 = (i as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    }
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
}

/// Where each key of a map or set is in its list of entries. Int keys,
/// the most common, have a table of their own with a cheaper hash (D134);
/// the hash is never visible (maps keep their insertion order).
#[derive(Default, Clone)]
pub struct KeyIndex {
    ints: HashMap<i64, usize, std::hash::BuildHasherDefault<IntHasher>>,
    other: HashMap<Key, usize, std::hash::BuildHasherDefault<KeyHasher>>,
}

impl KeyIndex {
    #[inline]
    pub fn get(&self, k: &Key) -> Option<&usize> {
        match k {
            Key::Int(i) => self.ints.get(i),
            _ => self.other.get(k),
        }
    }

    #[inline]
    pub fn get_int(&self, i: i64) -> Option<&usize> {
        self.ints.get(&i)
    }

    pub fn contains_key(&self, k: &Key) -> bool {
        self.get(k).is_some()
    }

    pub fn insert(&mut self, k: Key, i: usize) {
        match k {
            Key::Int(n) => {
                self.ints.insert(n, i);
            }
            _ => {
                self.other.insert(k, i);
            }
        }
    }

    /// Every key and its position (in no particular order).
    pub fn iter(&self) -> impl Iterator<Item = (Key, usize)> + '_ {
        self.ints.iter().map(|(k, i)| (Key::Int(*k), *i)).chain(self.other.iter().map(|(k, i)| (k.clone(), *i)))
    }
}

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
    Native(String),
}

pub struct Record {
    pub ty: &'static RecordType,
    pub fields: Mut<Fields>,
    /// the task that created it may change it (see `cow::check_write`)
    pub birth: std::cell::Cell<u64>,
}

/// Fields kept inside a record (D126).
const INLINE_FIELDS: usize = 4;

/// The values of a record's fields. Up to four live inside the object, so
/// building a small record is one allocation; the number of fields of a
/// record never changes.
pub enum Fields {
    Inline(u8, [V; INLINE_FIELDS]),
    Heap(Vec<Val>),
}

impl Fields {
    pub fn with(n: usize, mut f: impl FnMut(usize) -> Val) -> Fields {
        if n <= INLINE_FIELDS {
            let mut vals = [NONE; INLINE_FIELDS];
            for (i, slot) in vals.iter_mut().enumerate().take(n) {
                *slot = f(i).into_raw();
            }
            Fields::Inline(n as u8, vals)
        } else {
            Fields::Heap((0..n).map(f).collect())
        }
    }
}

impl From<Vec<Val>> for Fields {
    fn from(v: Vec<Val>) -> Fields {
        if v.len() <= INLINE_FIELDS {
            let n = v.len();
            let mut it = v.into_iter();
            Fields::with(n, |_| it.next().unwrap())
        } else {
            Fields::Heap(v)
        }
    }
}

impl FromIterator<Val> for Fields {
    fn from_iter<I: IntoIterator<Item = Val>>(iter: I) -> Fields {
        Fields::from(iter.into_iter().collect::<Vec<Val>>())
    }
}

impl std::ops::Deref for Fields {
    type Target = [Val];
    #[inline]
    fn deref(&self) -> &[Val] {
        match self {
            // Val is a transparent V that owns its reference
            Fields::Inline(n, vals) => unsafe { std::slice::from_raw_parts(vals.as_ptr() as *const Val, *n as usize) },
            Fields::Heap(v) => v,
        }
    }
}

impl std::ops::DerefMut for Fields {
    #[inline]
    fn deref_mut(&mut self) -> &mut [Val] {
        match self {
            Fields::Inline(n, vals) => unsafe { std::slice::from_raw_parts_mut(vals.as_mut_ptr() as *mut Val, *n as usize) },
            Fields::Heap(v) => v,
        }
    }
}

impl Drop for Fields {
    fn drop(&mut self) {
        if let Fields::Inline(n, vals) = self {
            for v in &vals[..*n as usize] {
                release(*v);
            }
        }
    }
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
    /// a value of the standard library or a module (date, time, secret,
    /// regex, database...), see `Native`
    Native(Box<dyn Native>),
}

/// A value of the standard library or of a module: it names its type,
/// shows itself and answers its members. Methods are returned as
/// `BuiltinMethod(value, name)` and run by `call_method`.
pub trait Native: Send + Sync + 'static {
    fn type_name(&self) -> &str;
    fn repr(&self) -> String;
    /// a field, or a method as `BuiltinMethod`
    fn member(&self, me: V, name: &str, span: u32) -> crate::state::R<Val> {
        let _ = me;
        Err(crate::stdlib::missing_member(self.type_name(), name, &[], span))
    }
    fn call_method(&self, me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> crate::state::R<Val> {
        let _ = (me, args, names);
        Err(crate::stdlib::missing_member(self.type_name(), name, &[], span))
    }
    /// called like a function (`log(x)`)
    fn call(&self, me: V, args: &[V], names: &[Option<&str>], span: u32) -> Option<crate::state::R<Val>> {
        let _ = (me, args, names, span);
        None
    }
    /// identity as a map key or set element
    fn key(&self) -> Option<String> {
        None
    }
    fn equals(&self, other: &dyn Native) -> bool {
        let _ = other;
        false
    }
    fn order(&self, other: &dyn Native) -> Option<std::cmp::Ordering> {
        let _ = other;
        None
    }
    /// a secret: it cannot be shown or turned into text (section 9)
    fn is_secret(&self) -> bool {
        false
    }
    fn as_any(&self) -> &dyn std::any::Any;
}

pub fn native(n: impl Native) -> Val {
    Val::new(Obj::Native(Box::new(n)))
}

impl V {
    /// The library value of type T behind this value.
    pub fn native<T: 'static>(self) -> Option<&'static T> {
        match self.get() {
            Some(Obj::Native(n)) => n.as_any().downcast_ref::<T>(),
            _ => None,
        }
    }
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
