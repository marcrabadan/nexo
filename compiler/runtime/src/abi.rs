//! The interface the compiled code calls. Values travel as two words
//! (`tag`, `bits`); names as (pointer, length); spans as indexes into the
//! program's span table (0: no span).
//!
//! Every function that can fail records the failure in the runtime state
//! and returns a dummy value; the caller checks `nx_failing`.

#![allow(clippy::missing_safety_doc, clippy::too_many_arguments)]

use std::cell::RefCell;

use nexo_diag::{suggest, Diagnostic, Lang};

use crate::call;
use crate::coll;
use crate::fmt::{repr, show, type_name};
use crate::num::{self, Op};
use crate::pipe::{self, Items};
use crate::state::{self, diag, failure, st, SpanRec, R};
use crate::stdlib;
use crate::types::{self, AliasType, EnumType, FieldInfo, RecordType, TraitType, TypeRef, VariantInfo};
use crate::value::*;

#[inline]
fn v(tag: u64, bits: u64) -> V {
    V { tag, bits }
}

#[inline]
fn ret(r: R<V>) -> V {
    r.unwrap_or(NONE)
}

unsafe fn text(ptr: *const u8, len: usize) -> &'static str {
    if ptr.is_null() {
        return "";
    }
    std::str::from_utf8_unchecked(std::slice::from_raw_parts(ptr, len))
}

unsafe fn values(ptr: *const V, n: u64) -> &'static [V] {
    if n == 0 {
        return &[];
    }
    std::slice::from_raw_parts(ptr, n as usize)
}

unsafe fn names(ptr: *const NameRef, n: u64) -> Vec<Option<&'static str>> {
    if ptr.is_null() || n == 0 {
        return vec![None; n as usize];
    }
    std::slice::from_raw_parts(ptr, n as usize).iter().map(|r| r.get()).collect()
}

// ------------------------------------------------------------- lifecycle

#[no_mangle]
pub unsafe extern "C" fn nx_init(
    file: *const u8,
    file_len: usize,
    src: *const u8,
    src_len: usize,
    spans: *const SpanRec,
    nspans: u64,
    lang: u32,
) {
    nexo_diag::set_lang(if lang == 1 { Lang::En } else { Lang::Es });
    let spans: &'static [SpanRec] = if nspans == 0 { &[] } else { std::slice::from_raw_parts(spans, nspans as usize) };
    state::init(text(file, file_len), text(src, src_len), spans);
}

/// Run the program on a thread with a large stack, so that deep recursion
/// reaches the call limit before the end of the stack.
#[no_mangle]
pub extern "C" fn nx_start(entry: extern "C" fn()) -> ! {
    let handle = std::thread::Builder::new().stack_size(1 << 30).spawn(move || entry());
    match handle {
        Ok(h) => {
            let _ = h.join();
        }
        Err(_) => entry(),
    }
    nx_exit(0)
}

/// More than MAX_DEPTH nested calls.
#[no_mangle]
pub extern "C" fn nx_too_deep() {
    diag(Diagnostic::new_spanless("too_deep"), 0);
}

#[no_mangle]
pub extern "C" fn nx_exit(code: i32) -> ! {
    state::flush();
    std::process::exit(code)
}

/// The program ended with an unhandled error or a diagnostic (rule 6).
#[no_mangle]
pub extern "C" fn nx_unhandled() -> ! {
    state::report_pending();
    std::process::exit(1)
}

#[no_mangle]
pub extern "C" fn nx_report() {
    state::report_pending();
}

// ---------------------------------------------------------------- errors

#[no_mangle]
pub extern "C" fn nx_take_error() -> V {
    state::take_error()
}

#[no_mangle]
pub extern "C" fn nx_reraise(t: u64, b: u64) {
    state::raise_value(v(t, b));
}

/// `else e is Kind`: Error matches every error.
#[no_mangle]
pub unsafe extern "C" fn nx_error_is(t: u64, b: u64, kind: *const u8, len: usize) -> u8 {
    types::is_error_kind(v(t, b), text(kind, len)) as u8
}

#[no_mangle]
pub extern "C" fn nx_clear() {
    state::clear();
}

#[no_mangle]
pub unsafe extern "C" fn nx_name_error(name: *const u8, len: usize, sug: *const u8, sug_len: usize, span: u32) {
    let name = text(name, len);
    let d = if sug.is_null() {
        Diagnostic::new_spanless("name_not_found").arg("name", name)
    } else {
        Diagnostic::new_spanless("name_not_found").fix("suggest").arg("name", name).arg("suggestion", text(sug, sug_len))
    };
    diag(d, span);
}

#[no_mangle]
pub unsafe extern "C" fn nx_immutable(name: *const u8, len: usize, span: u32) {
    diag(Diagnostic::new_spanless("immutable").arg("name", text(name, len)), span);
}

#[no_mangle]
pub unsafe extern "C" fn nx_already_declared(name: *const u8, len: usize, span: u32) {
    diag(Diagnostic::new_spanless("already_declared").arg("name", text(name, len)), span);
}

#[no_mangle]
pub unsafe extern "C" fn nx_not_bool(t: u64, b: u64, what: *const u8, len: usize, span: u32) {
    diag(Diagnostic::new_spanless("not_bool").arg("what", text(what, len)).arg("vtype", type_name(v(t, b))), span);
}

#[no_mangle]
pub unsafe extern "C" fn nx_return_outside(span: u32) {
    diag(Diagnostic::new_spanless("return_outside_fn"), span);
}

// ---------------------------------------------------------------- values

#[no_mangle]
pub unsafe extern "C" fn nx_str(ptr: *const u8, len: usize) -> V {
    V::str(text(ptr, len))
}

#[no_mangle]
pub unsafe extern "C" fn nx_unit(t: *const u8, tl: usize, u: *const u8, ul: usize) -> V {
    num::unit_literal(text(t, tl), text(u, ul))
}

#[no_mangle]
pub extern "C" fn nx_builtin(id: u32) -> V {
    V::obj(Obj::Builtin(BuiltinId(id)))
}

/// Builtin types, error kinds, currencies, half_up and the json/csv namespaces.
#[no_mangle]
pub unsafe extern "C" fn nx_named(ptr: *const u8, len: usize) -> V {
    stdlib::named_value(text(ptr, len)).unwrap_or(NONE)
}

#[no_mangle]
pub unsafe extern "C" fn nx_list(items: *const V, n: u64) -> V {
    let mut out = Vec::with_capacity(n as usize);
    for x in values(items, n) {
        match pipe::bind(*x) {
            Ok(b) => out.push(b),
            Err(_) => return NONE,
        }
    }
    list(out)
}

#[no_mangle]
pub extern "C" fn nx_list_push(lt: u64, lb: u64, t: u64, b: u64) {
    let Some(Obj::List(items)) = v(lt, lb).get() else { return };
    if let Ok(x) = pipe::bind(v(t, b)) {
        items.borrow_mut().push(x);
    }
}

#[no_mangle]
pub unsafe extern "C" fn nx_map(keys: *const V, vals: *const V, spans: *const u32, n: u64) -> V {
    let keys = values(keys, n);
    let vals = values(vals, n);
    let spans: &[u32] = if n == 0 { &[] } else { std::slice::from_raw_parts(spans, n as usize) };
    let mut m = NMap::default();
    for i in 0..n as usize {
        let Ok(k) = coll::hashable(keys[i], spans[i]) else { return NONE };
        let Ok(val) = pipe::bind(vals[i]) else { return NONE };
        m.insert(k, keys[i], val);
    }
    V::obj(Obj::Map(RefCell::new(m)))
}

#[no_mangle]
pub extern "C" fn nx_range(at: u64, ab: u64, bt: u64, bb: u64, span: u32) -> V {
    let (a, b) = (v(at, ab), v(bt, bb));
    match (a.as_int(), b.as_int()) {
        (Some(x), Some(y)) => V::obj(Obj::Range(x, y)),
        _ => {
            diag(
                Diagnostic::new_spanless("bad_operands").arg("op", "..").arg("a", type_name(a)).arg("b", type_name(b)),
                span,
            );
            NONE
        }
    }
}

#[no_mangle]
pub extern "C" fn nx_index(ot: u64, ob: u64, it: u64, ib: u64, span: u32) -> V {
    ret(coll::get_index(v(ot, ob), v(it, ib), span))
}

#[no_mangle]
pub extern "C" fn nx_set_index(ot: u64, ob: u64, it: u64, ib: u64, t: u64, b: u64, span: u32) {
    let _ = coll::set_index(v(ot, ob), v(it, ib), v(t, b), span);
}

#[no_mangle]
pub unsafe extern "C" fn nx_attr(t: u64, b: u64, name: *const u8, len: usize, span: u32) -> V {
    ret(call::get_attr(v(t, b), text(name, len), span))
}

#[no_mangle]
pub unsafe extern "C" fn nx_set_attr(
    ot: u64,
    ob: u64,
    name: *const u8,
    len: usize,
    t: u64,
    b: u64,
    name_span: u32,
    value_span: u32,
) {
    let _ = call::set_attr(v(ot, ob), text(name, len), v(t, b), name_span, value_span);
}

// ------------------------------------------------------------ operators

#[no_mangle]
pub extern "C" fn nx_binop(op: u32, at: u64, ab: u64, bt: u64, bb: u64, span: u32) -> V {
    ret(num::arith(Op::from_code(op), v(at, ab), v(bt, bb), span))
}

#[no_mangle]
pub extern "C" fn nx_neg(t: u64, b: u64, span: u32) -> V {
    ret(num::negate(v(t, b), span))
}

#[no_mangle]
pub extern "C" fn nx_eq(at: u64, ab: u64, bt: u64, bb: u64, span: u32) -> u8 {
    num::equals(v(at, ab), v(bt, bb), span).unwrap_or(false) as u8
}

#[no_mangle]
pub extern "C" fn nx_order(op: u32, at: u64, ab: u64, bt: u64, bb: u64, span: u32) -> u8 {
    let op = match op {
        0 => "<",
        1 => ">",
        2 => "<=",
        _ => ">=",
    };
    num::order(op, v(at, ab), v(bt, bb), span).unwrap_or(false) as u8
}

#[no_mangle]
pub extern "C" fn nx_in(it: u64, ib: u64, ct: u64, cb: u64, span: u32) -> u8 {
    coll::contains(v(ct, cb), v(it, ib), span).unwrap_or(false) as u8
}

#[no_mangle]
pub extern "C" fn nx_is(at: u64, ab: u64, tt: u64, tb: u64, span: u32) -> u8 {
    types::is_value(v(at, ab), v(tt, tb), span).unwrap_or(false) as u8
}

#[no_mangle]
pub unsafe extern "C" fn nx_interp(parts: *const V, n: u64) -> V {
    let mut out = String::new();
    for p in values(parts, n) {
        match pipe::bind(*p) {
            Ok(b) => out.push_str(&show(b)),
            Err(_) => return NONE,
        }
    }
    V::str(out)
}

#[no_mangle]
pub extern "C" fn nx_bind(t: u64, b: u64) -> V {
    ret(pipe::bind(v(t, b)))
}

/// Read a lazy value (an argument of a direct call); keep float literals.
#[no_mangle]
pub extern "C" fn nx_force(t: u64, b: u64) -> V {
    ret(pipe::force(v(t, b)))
}

/// The closure behind a function value (for direct calls).
#[no_mangle]
pub extern "C" fn nx_func_ptr(t: u64, b: u64) -> *const Func {
    match v(t, b).get() {
        Some(Obj::Func(f)) => f as *const Func,
        _ => std::ptr::null(),
    }
}

#[no_mangle]
pub extern "C" fn nx_plain(t: u64, b: u64) -> V {
    v(t, b).plain()
}

#[no_mangle]
pub unsafe extern "C" fn nx_conform(
    t: u64,
    b: u64,
    desc: *const u8,
    desc_len: usize,
    name: *const u8,
    name_len: usize,
    span: u32,
) -> V {
    let d = types::desc(desc, desc_len);
    ret(types::conform(v(t, b), d, text(name, name_len), span))
}

/// Check a returned value against the declared return type.
#[no_mangle]
pub unsafe extern "C" fn nx_conform_return(
    t: u64,
    b: u64,
    desc: *const u8,
    desc_len: usize,
    name: *const u8,
    name_len: usize,
    span: u32,
) -> V {
    let d = types::desc(desc, desc_len);
    let value = v(t, b);
    let fname = text(name, name_len);
    match types::conform(value, d, fname, span) {
        Ok(x) => x,
        Err(_) => {
            let key = st().diag.as_ref().map(|(d, _)| d.key);
            if matches!(key, Some("type_mismatch" | "none_not_allowed")) {
                state::clear();
                diag(
                    Diagnostic::new_spanless("return_mismatch")
                        .arg("fn", fname)
                        .arg("type", d.display().to_string())
                        .arg("value", repr(value.plain()))
                        .arg("vtype", type_name(value)),
                    span,
                );
            }
            NONE
        }
    }
}

// ----------------------------------------------------------------- calls

#[no_mangle]
pub unsafe extern "C" fn nx_call(t: u64, b: u64, args: *const V, nm: *const NameRef, n: u64, span: u32) -> V {
    let names = names(nm, n);
    ret(call::call(v(t, b), values(args, n), &names, span))
}

#[no_mangle]
pub unsafe extern "C" fn nx_call_method(
    t: u64,
    b: u64,
    name: *const u8,
    len: usize,
    args: *const V,
    nm: *const NameRef,
    n: u64,
    span: u32,
    name_span: u32,
) -> V {
    let names = names(nm, n);
    ret(call::call_method(v(t, b), text(name, len), values(args, n), &names, span, name_span))
}

#[no_mangle]
pub unsafe extern "C" fn nx_builtin_call(id: u32, args: *const V, nm: *const NameRef, n: u64, span: u32) -> V {
    let names = names(nm, n);
    ret(stdlib::call(BuiltinId(id), values(args, n), &names, span))
}

#[no_mangle]
pub unsafe extern "C" fn nx_closure(code: Code, info: *const FnInfo, cells: *const Cell, n: u64) -> V {
    let env: Vec<Cell> = if n == 0 { Vec::new() } else { std::slice::from_raw_parts(cells, n as usize).to_vec() };
    let env = Box::leak(env.into_boxed_slice());
    V::obj(Obj::Func(Func { code, info: &*info, env: env.as_ptr(), nenv: n }))
}

/// A new captured-variable cell, not assigned yet.
#[no_mangle]
pub extern "C" fn nx_cell() -> *mut V {
    Box::leak(Box::new(UNDEF))
}

// ----------------------------------------------------------------- types

#[repr(C)]
pub struct FieldDecl {
    pub name: NameRef,
    /// type descriptor (null for an untyped variant parameter)
    pub desc: NameRef,
    pub optional: u64,
}

#[repr(C)]
pub struct VariantDecl {
    pub id: u64,
    pub name: NameRef,
    pub nparams: u64,
    pub params: *const FieldDecl,
}

unsafe fn fields(decls: *const FieldDecl, n: u64, defaults: *const V) -> Vec<FieldInfo> {
    if n == 0 {
        return Vec::new();
    }
    let decls = std::slice::from_raw_parts(decls, n as usize);
    decls
        .iter()
        .enumerate()
        .map(|(i, f)| FieldInfo {
            name: f.name.get().unwrap_or("").to_string(),
            ty: if f.desc.ptr.is_null() { None } else { Some(types::desc(f.desc.ptr, f.desc.len)) },
            optional: f.optional != 0,
            default: if defaults.is_null() { UNDEF } else { *defaults.add(i) },
        })
        .collect()
}

#[no_mangle]
pub unsafe extern "C" fn nx_define_record(
    id: u32,
    name: *const u8,
    len: usize,
    n: u64,
    decls: *const FieldDecl,
    defaults: *const V,
) -> V {
    let rt: &'static RecordType = Box::leak(Box::new(RecordType {
        id,
        name: text(name, len).to_string(),
        fields: fields(decls, n, defaults),
        methods: RefCell::new(Vec::new()),
        traits: RefCell::new(Vec::new()),
    }));
    types::register(id, TypeRef::Record(rt));
    V::obj(Obj::Type(TypeRef::Record(rt)))
}

#[no_mangle]
pub unsafe extern "C" fn nx_define_enum(id: u32, name: *const u8, len: usize, n: u64, decls: *const VariantDecl) -> V {
    let decls: &[VariantDecl] = if n == 0 { &[] } else { std::slice::from_raw_parts(decls, n as usize) };
    let variants: Vec<&'static VariantInfo> = decls
        .iter()
        .map(|d| {
            let info: &'static VariantInfo = Box::leak(Box::new(VariantInfo {
                id: d.id as u32,
                name: d.name.get().unwrap_or("").to_string(),
                params: fields(d.params, d.nparams, std::ptr::null()),
                enum_ptr: std::cell::Cell::new(std::ptr::null()),
                value: std::cell::Cell::new(NONE),
            }));
            info.value.set(V::obj(Obj::Variant(info)));
            info
        })
        .collect();
    let et: &'static EnumType = Box::leak(Box::new(EnumType {
        id,
        name: text(name, len).to_string(),
        variants,
        methods: RefCell::new(Vec::new()),
        traits: RefCell::new(Vec::new()),
    }));
    for var in &et.variants {
        var.enum_ptr.set(et);
    }
    types::register(id, TypeRef::Enum(et));
    V::obj(Obj::Type(TypeRef::Enum(et)))
}

#[no_mangle]
pub extern "C" fn nx_enum_variant(t: u64, b: u64, index: u64) -> V {
    match v(t, b).get() {
        Some(Obj::Type(TypeRef::Enum(e))) => e.variants[index as usize].value.get(),
        _ => NONE,
    }
}

#[no_mangle]
pub unsafe extern "C" fn nx_define_alias(id: u32, name: *const u8, len: usize, desc: *const u8, dlen: usize) -> V {
    let a: &'static AliasType =
        Box::leak(Box::new(AliasType { id, name: text(name, len).to_string(), target: types::desc(desc, dlen) }));
    types::register(id, TypeRef::Alias(a));
    V::obj(Obj::Type(TypeRef::Alias(a)))
}

#[no_mangle]
pub unsafe extern "C" fn nx_define_trait(id: u32, name: *const u8, len: usize, n: u64, methods: *const NameRef) -> V {
    let methods: Vec<String> = names(methods, n).into_iter().map(|m| m.unwrap_or("").to_string()).collect();
    let tt: &'static TraitType = Box::leak(Box::new(TraitType { id, name: text(name, len).to_string(), methods }));
    types::register(id, TypeRef::Trait(tt));
    V::obj(Obj::Type(TypeRef::Trait(tt)))
}

#[no_mangle]
pub unsafe extern "C" fn nx_add_method(tt: u64, tb: u64, name: *const u8, len: usize, ft: u64, fb: u64) {
    let name = text(name, len).to_string();
    match v(tt, tb).get() {
        Some(Obj::Type(TypeRef::Record(r))) => r.methods.borrow_mut().push((name, v(ft, fb))),
        Some(Obj::Type(TypeRef::Enum(e))) => e.methods.borrow_mut().push((name, v(ft, fb))),
        _ => {}
    }
}

#[no_mangle]
pub extern "C" fn nx_add_trait(tt: u64, tb: u64, rt: u64, rb: u64) {
    let Some(Obj::Type(TypeRef::Trait(tr))) = v(rt, rb).get() else { return };
    match v(tt, tb).get() {
        Some(Obj::Type(TypeRef::Record(r))) => r.traits.borrow_mut().push(tr),
        Some(Obj::Type(TypeRef::Enum(e))) => e.traits.borrow_mut().push(tr),
        _ => {}
    }
}

// -------------------------------------------------------------- patterns

/// A bare name in a pattern that names a variant.
#[no_mangle]
pub extern "C" fn nx_match_variant(t: u64, b: u64, vt: u64, vb: u64) -> u8 {
    let (value, var) = (v(t, b), v(vt, vb));
    let Some(Obj::Variant(target)) = var.get() else { return 0 };
    (match value.get() {
        Some(Obj::Variant(x)) => std::ptr::eq(*x, *target),
        Some(Obj::VariantValue(vv)) => std::ptr::eq(vv.variant, *target),
        _ => false,
    }) as u8
}

/// `Ctor(p1, p2)`: 1 when the value was built by the constructor; then its
/// fields are read with nx_field_at.
#[no_mangle]
pub unsafe extern "C" fn nx_match_ctor(
    t: u64,
    b: u64,
    ct: u64,
    cb: u64,
    nargs: u64,
    name: *const u8,
    len: usize,
    span: u32,
) -> u8 {
    let (value, ctor) = (v(t, b), v(ct, cb));
    let count = match (ctor.get(), value.get()) {
        (Some(Obj::Variant(var)), Some(Obj::VariantValue(vv))) if std::ptr::eq(*var, vv.variant) => vv.fields.len(),
        (Some(Obj::Variant(var)), Some(Obj::Variant(x))) if std::ptr::eq(*var, *x) => 0,
        (Some(Obj::Variant(_)), _) => return 0,
        (Some(Obj::Type(TypeRef::Record(rt))), Some(Obj::Record(r))) if std::ptr::eq(*rt, r.ty) => r.ty.fields.len(),
        (Some(Obj::Type(TypeRef::Record(_))), _) => return 0,
        _ => {
            diag(Diagnostic::new_spanless("not_callable").arg("vtype", type_name(ctor)), span);
            return 0;
        }
    };
    if count as u64 != nargs {
        diag(
            Diagnostic::new_spanless("variant_fields")
                .arg("name", text(name, len))
                .arg("n", count.to_string())
                .arg("m", nargs.to_string()),
            span,
        );
        return 0;
    }
    1
}

#[no_mangle]
pub extern "C" fn nx_field_at(t: u64, b: u64, i: u64) -> V {
    match v(t, b).get() {
        Some(Obj::VariantValue(vv)) => vv.fields[i as usize],
        Some(Obj::Record(r)) => r.fields.borrow()[i as usize],
        _ => NONE,
    }
}

#[no_mangle]
pub extern "C" fn nx_no_match(t: u64, b: u64, span: u32) {
    diag(Diagnostic::new_spanless("no_match").arg("value", repr(v(t, b))), span);
}

// ------------------------------------------------------------- pipelines

#[no_mangle]
pub extern "C" fn nx_pipe_where(st_: u64, sb: u64, ft: u64, fb: u64, step: u32, expr: u32) -> V {
    pipe::where_(v(st_, sb), v(ft, fb), step, expr)
}

#[no_mangle]
pub extern "C" fn nx_pipe_map(st_: u64, sb: u64, ft: u64, fb: u64, step: u32, expr: u32) -> V {
    pipe::map(v(st_, sb), v(ft, fb), step, expr)
}

#[no_mangle]
pub unsafe extern "C" fn nx_pipe_pick(st_: u64, sb: u64, fns: *const V, nm: *const NameRef, n: u64, step: u32, expr: u32) -> V {
    let fns = values(fns, n).to_vec();
    let cols: Vec<&'static str> = names(nm, n).into_iter().map(|x| x.unwrap_or("")).collect();
    pipe::pick(v(st_, sb), fns, cols, step, expr)
}

#[no_mangle]
pub extern "C" fn nx_pipe_take(st_: u64, sb: u64, nt: u64, nb: u64, step: u32, nspan: u32) -> V {
    ret(pipe::take(v(st_, sb), v(nt, nb), step, nspan))
}

#[no_mangle]
pub extern "C" fn nx_pipe_sort(st_: u64, sb: u64, ft: u64, fb: u64, desc: u32, step: u32, expr: u32) -> V {
    pipe::sort(v(st_, sb), v(ft, fb), desc != 0, step, expr)
}

#[no_mangle]
pub unsafe extern "C" fn nx_pipe_group(
    st_: u64,
    sb: u64,
    ft: u64,
    fb: u64,
    key: *const u8,
    len: usize,
    step: u32,
    expr: u32,
) -> V {
    ret(pipe::group(v(st_, sb), v(ft, fb), text(key, len), step, expr))
}

#[no_mangle]
pub extern "C" fn nx_pipe_each(st_: u64, sb: u64, ft: u64, fb: u64, step: u32, expr: u32) -> V {
    ret(pipe::each(v(st_, sb), v(ft, fb), step, expr))
}

#[no_mangle]
pub extern "C" fn nx_pipe_count(t: u64, b: u64, span: u32) -> V {
    ret(pipe::count(v(t, b), span))
}

#[no_mangle]
pub extern "C" fn nx_pipe_sum(t: u64, b: u64, span: u32) -> V {
    ret(pipe::sum(v(t, b), span))
}

#[no_mangle]
pub extern "C" fn nx_pipe_avg(t: u64, b: u64, span: u32) -> V {
    ret(pipe::avg(v(t, b), span))
}

#[no_mangle]
pub unsafe extern "C" fn nx_pipe_agg(
    gt: u64,
    gb: u64,
    kind: u32,
    ft: u64,
    fb: u64,
    col: *const u8,
    len: usize,
    span: u32,
    expr: u32,
) -> V {
    let kind = match kind {
        0 => "sum",
        1 => "count",
        _ => "avg",
    };
    ret(pipe::aggregate(v(gt, gb), kind, v(ft, fb), text(col, len), span, expr))
}

/// A bare name inside a pipeline step: the column of the current row.
#[no_mangle]
pub unsafe extern "C" fn nx_row_get(t: u64, b: u64, name: *const u8, len: usize, found: *mut u8) -> V {
    match pipe::row_get(v(t, b), text(name, len)) {
        Some(x) => {
            *found = 1;
            x
        }
        None => {
            *found = 0;
            NONE
        }
    }
}

/// A bare name in a step that is neither a column nor a variable.
#[no_mangle]
pub unsafe extern "C" fn nx_row_missing(t: u64, b: u64, name: *const u8, len: usize, sug: *const u8, sl: usize, span: u32) {
    let name = text(name, len);
    let mut candidates = pipe::row_names(v(t, b));
    if !sug.is_null() {
        candidates.push(text(sug, sl).to_string());
    }
    let d = match suggest(name, candidates.iter().map(|s| s.as_str())) {
        Some(s) => Diagnostic::new_spanless("name_not_found").fix("suggest").arg("name", name).arg("suggestion", s),
        None => Diagnostic::new_spanless("name_not_found").arg("name", name),
    };
    diag(d, span);
}

// ------------------------------------------------------------- iteration

pub struct Iter(Items);

#[no_mangle]
pub extern "C" fn nx_iter(t: u64, b: u64, span: u32) -> *mut Iter {
    match pipe::iterate(v(t, b), span) {
        Ok(items) => Box::leak(Box::new(Iter(items))),
        Err(_) => std::ptr::null_mut(),
    }
}

/// 1 and the next element in `out`, or 0 at the end (or on a failure).
#[no_mangle]
pub unsafe extern "C" fn nx_iter_next(it: *mut Iter, out: *mut V) -> u8 {
    match (*it).0.next() {
        Some(Ok(x)) => {
            *out = x.plain();
            1
        }
        _ => 0,
    }
}

#[no_mangle]
pub unsafe extern "C" fn nx_iter_free(it: *mut Iter) {
    if !it.is_null() {
        drop(Box::from_raw(it));
    }
}

/// `repeat n`: n must be an int.
#[no_mangle]
pub extern "C" fn nx_repeat_count(t: u64, b: u64, span: u32) -> i64 {
    let x = v(t, b);
    match x.as_int() {
        Some(n) => n,
        None => {
            diag(
                Diagnostic::new_spanless("type_mismatch")
                    .arg("name", "repeat")
                    .arg("type", "int")
                    .arg("value", repr(x.plain()))
                    .arg("vtype", type_name(x)),
                span,
            );
            0
        }
    }
}

/// `parallel for ..., max: n`: n must be an int of at least 1 (D52).
#[no_mangle]
pub extern "C" fn nx_parallel_max(t: u64, b: u64, span: u32) {
    let x = v(t, b);
    if !matches!(x.as_int(), Some(n) if n >= 1) {
        diag(
            Diagnostic::new_spanless("type_mismatch")
                .arg("name", "max")
                .arg("type", "int")
                .arg("value", repr(x.plain()))
                .arg("vtype", type_name(x)),
            span,
        );
    }
}

// --------------------------------------------------------- contracts, tests

#[no_mangle]
pub extern "C" fn nx_deep_copy(t: u64, b: u64) -> V {
    coll::deep_copy(v(t, b))
}

/// A broken `expect` or `ensure`: a ContractBroken error with the values
/// of the names, fields and old() values involved (D53).
#[no_mangle]
pub unsafe extern "C" fn nx_contract_broken(
    kw: *const u8,
    kl: usize,
    expr: *const u8,
    el: usize,
    texts: *const NameRef,
    vals: *const V,
    n: u64,
    span: u32,
) {
    let texts = names(texts, n);
    let vals = values(vals, n);
    let mut parts = Vec::new();
    for (t, x) in texts.iter().zip(vals) {
        if x.tag == T_UNDEF || call::is_callable(*x) || matches!(x.get(), Some(Obj::Type(_))) {
            continue;
        }
        parts.push(format!("{} = {}", t.unwrap_or(""), repr(*x)));
    }
    let values_text = if parts.is_empty() { String::new() } else { format!(" ({})", parts.join(", ")) };
    failure(
        "ContractBroken",
        span,
        &[("kw", text(kw, kl).to_string()), ("expr", text(expr, el).to_string()), ("values", values_text)],
    );
}

#[no_mangle]
pub unsafe extern "C" fn nx_assert_failed(expr: *const u8, len: usize, span: u32) {
    diag(Diagnostic::new_spanless("assert_failed").arg("expr", text(expr, len)).arg("detail", ""), span);
}

#[no_mangle]
pub unsafe extern "C" fn nx_assert_cmp_failed(
    expr: *const u8,
    len: usize,
    op: *const u8,
    ol: usize,
    lt: u64,
    lb: u64,
    rt: u64,
    rb: u64,
    span: u32,
) {
    let detail = format!(" ({} {} {})", repr(v(lt, lb)), text(op, ol), repr(v(rt, rb)));
    diag(Diagnostic::new_spanless("assert_failed").arg("expr", text(expr, len)).arg("detail", detail), span);
}

#[no_mangle]
pub unsafe extern "C" fn nx_assert_not_failed(expr: *const u8, len: usize, span: u32) {
    diag(Diagnostic::new_spanless("assert_not_failed").arg("expr", text(expr, len)), span);
}

/// Print the name of the file before its tests (`nexo test` with several files).
#[no_mangle]
pub extern "C" fn nx_test_header() {
    if let Ok(name) = std::env::var("NEXO_TEST_HEADER") {
        state::write_out(&format!("{name}\n"));
        state::flush();
    }
}

/// After a test block: ✓ or ✗ and the failure.
#[no_mangle]
pub unsafe extern "C" fn nx_test_result(name: *const u8, len: usize) {
    let s = st();
    s.tests_run += 1;
    if state::failing() != 0 {
        state::write_out(&format!("✗ {}\n", text(name, len)));
        state::report_pending();
        state::clear();
    } else {
        s.tests_passed += 1;
        state::write_out(&format!("✓ {}\n", text(name, len)));
        state::flush();
    }
}

/// End of `nexo test` for this file: tell the driver how many tests ran.
#[no_mangle]
pub extern "C" fn nx_test_finish() -> ! {
    let s = st();
    if let Ok(path) = std::env::var("NEXO_TEST_REPORT") {
        let _ = std::fs::write(path, format!("{} {}\n", s.tests_run, s.tests_passed));
    }
    nx_exit(0)
}

// ------------------------------------------------------------ events, time

#[no_mangle]
pub unsafe extern "C" fn nx_on(name: *const u8, len: usize, t: u64, b: u64) {
    stdlib::on(text(name, len), v(t, b));
}

#[no_mangle]
pub unsafe extern "C" fn nx_emit(name: *const u8, len: usize, args: *const V, n: u64, span: u32) {
    let _ = stdlib::emit(text(name, len), values(args, n), span);
}

/// Start of `try within limit:`.
#[no_mangle]
pub extern "C" fn nx_deadline_push(t: u64, b: u64, within_span: u32, try_span: u32) {
    let limit = v(t, b);
    let seconds = match limit.get() {
        Some(Obj::Quantity(d, u)) if num::unit_dimension(u).0 == "time" => num::unit_dimension(u).1.mul(d).to_f64(),
        _ => {
            diag(
                Diagnostic::new_spanless("type_mismatch")
                    .arg("name", "within")
                    .arg("type", "duration")
                    .arg("value", repr(limit.plain()))
                    .arg("vtype", type_name(limit)),
                within_span,
            );
            return;
        }
    };
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs_f64(seconds.max(0.0));
    st().deadlines.push((deadline, limit, try_span));
    unsafe { state::nx_deadline_active = 1 };
}

#[no_mangle]
pub extern "C" fn nx_deadline_pop() {
    let s = st();
    s.deadlines.pop();
    if s.deadlines.is_empty() {
        unsafe { state::nx_deadline_active = 0 };
    }
}

/// Between statements while a `try within` runs (D58).
#[no_mangle]
pub extern "C" fn nx_check_deadline() {
    let now = std::time::Instant::now();
    let expired = st().deadlines.iter().find(|(d, _, _)| now > *d).map(|(_, l, s)| (*l, *s));
    if let Some((limit, span)) = expired {
        failure("Timeout", span, &[("limit", repr(limit))]);
    }
}

// ------------------------------------------------------------ odds and ends

/// An int literal that does not fit in 64 bits.
#[no_mangle]
pub extern "C" fn nx_fail_overflow(span: u32) {
    failure("Overflow", span, &[]);
}

thread_local! {
    static TESTS: RefCell<Vec<(String, V)>> = const { RefCell::new(Vec::new()) };
}

/// A `test` block reached while running the top level of the file.
#[no_mangle]
pub unsafe extern "C" fn nx_test_register(name: *const u8, len: usize, t: u64, b: u64) {
    let name = text(name, len).to_string();
    TESTS.with(|tests| tests.borrow_mut().push((name, v(t, b))));
}

/// Run every registered test, each in its own scope (D54).
#[no_mangle]
pub extern "C" fn nx_run_tests() -> ! {
    let tests: Vec<(String, V)> = TESTS.with(|t| t.borrow().clone());
    if !tests.is_empty() {
        nx_test_header();
    }
    for (name, f) in tests {
        let _ = call::call_value(f, &[], 0);
        unsafe { nx_test_result(name.as_ptr(), name.len()) };
    }
    nx_test_finish()
}
