//! `use python module as name` (section 18, D111): the Python the compiler
//! found (its libpython and its `sys.path`) is loaded when the program first
//! imports a module, and used through its C API, as phase 0's pylib.py
//! uses the Python it runs in. Data is converted at the boundary in the
//! same way.

use std::ffi::{c_char, c_void, CString};
use std::sync::OnceLock;

use nexo_diag::Diagnostic;

use crate::fmt::type_name;
use crate::state::{diag, failure, failure_as, R};
use crate::value::*;

type P = *mut c_void;

const RTLD_NOW: i32 = 2;
const RTLD_GLOBAL: i32 = 0x100;

extern "C" {
    fn dlopen(file: *const c_char, mode: i32) -> *mut c_void;
    fn dlsym(handle: *mut c_void, name: *const c_char) -> *mut c_void;
}

/// The functions and objects of the C API that are used.
struct Api {
    gil_ensure: extern "C" fn() -> i32,
    gil_release: extern "C" fn(i32),
    import: extern "C" fn(*const c_char) -> P,
    getattr: extern "C" fn(P, *const c_char) -> P,
    call: extern "C" fn(P, P, P) -> P,
    repr: extern "C" fn(P) -> P,
    str_: extern "C" fn(P) -> P,
    dir: extern "C" fn(P) -> P,
    err_fetch: extern "C" fn(*mut P, *mut P, *mut P),
    err_normalize: extern "C" fn(*mut P, *mut P, *mut P),
    err_clear: extern "C" fn(),
    exc_matches: extern "C" fn(P) -> i32,
    incref: extern "C" fn(P),
    decref: extern "C" fn(P),
    long_from: extern "C" fn(i64) -> P,
    long_as: extern "C" fn(P, *mut i32) -> i64,
    float_from: extern "C" fn(f64) -> P,
    float_as: extern "C" fn(P) -> f64,
    bool_from: extern "C" fn(i64) -> P,
    unicode_from: extern "C" fn(*const c_char, isize) -> P,
    unicode_as: extern "C" fn(P, *mut isize) -> *const c_char,
    list_new: extern "C" fn(isize) -> P,
    list_set: extern "C" fn(P, isize, P) -> i32,
    list_size: extern "C" fn(P) -> isize,
    list_get: extern "C" fn(P, isize) -> P,
    tuple_new: extern "C" fn(isize) -> P,
    tuple_set: extern "C" fn(P, isize, P) -> i32,
    tuple_size: extern "C" fn(P) -> isize,
    tuple_get: extern "C" fn(P, isize) -> P,
    dict_new: extern "C" fn() -> P,
    dict_set: extern "C" fn(P, P, P) -> i32,
    dict_next: extern "C" fn(P, *mut isize, *mut P, *mut P) -> i32,
    set_new: extern "C" fn(P) -> P,
    set_add: extern "C" fn(P, P) -> i32,
    isinstance: extern "C" fn(P, P) -> i32,
    object_type: extern "C" fn(P) -> P,
    get_iter: extern "C" fn(P) -> P,
    iter_next: extern "C" fn(P) -> P,
    none: usize,
    t_bool: usize,
    t_long: usize,
    t_float: usize,
    t_unicode: usize,
    t_list: usize,
    t_tuple: usize,
    t_dict: usize,
    t_set: usize,
    t_frozenset: usize,
    t_decimal: usize,
    attribute_error: usize,
}

static API: OnceLock<Option<Api>> = OnceLock::new();

fn json_text(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Load libpython, start it with the `sys.path` of the compiler's Python
/// and release its lock for the threads that use it.
fn load(lib: &str, paths: &[&str]) -> Option<Api> {
    unsafe {
        let c = CString::new(lib).ok()?;
        let h = dlopen(c.as_ptr(), RTLD_NOW | RTLD_GLOBAL);
        if h.is_null() {
            return None;
        }
        let sym = |name: &str| -> Option<*mut c_void> {
            let n = CString::new(name).ok()?;
            let p = dlsym(h, n.as_ptr());
            (!p.is_null()).then_some(p)
        };
        macro_rules! f {
            ($name:expr) => {
                std::mem::transmute(sym($name)?)
            };
        }
        let initialize: extern "C" fn(i32) = f!("Py_InitializeEx");
        let is_initialized: extern "C" fn() -> i32 = f!("Py_IsInitialized");
        let run: extern "C" fn(*const c_char) -> i32 = f!("PyRun_SimpleString");
        let save_thread: extern "C" fn() -> P = f!("PyEval_SaveThread");
        if is_initialized() == 0 {
            initialize(0);
        }
        let list: Vec<String> = paths.iter().filter(|p| !p.is_empty()).map(|p| json_text(p)).collect();
        let code = CString::new(format!("import sys\nsys.path[:] = [{}]\nimport decimal\n", list.join(", "))).ok()?;
        run(code.as_ptr());
        let import: extern "C" fn(*const c_char) -> P = f!("PyImport_ImportModule");
        let getattr: extern "C" fn(P, *const c_char) -> P = f!("PyObject_GetAttrString");
        let decimal_mod = import(c"decimal".as_ptr());
        let t_decimal = if decimal_mod.is_null() { std::ptr::null_mut() } else { getattr(decimal_mod, c"Decimal".as_ptr()) };
        let attribute_error = *(sym("PyExc_AttributeError")? as *const P);
        let api = Api {
            gil_ensure: f!("PyGILState_Ensure"),
            gil_release: f!("PyGILState_Release"),
            import,
            getattr,
            call: f!("PyObject_Call"),
            repr: f!("PyObject_Repr"),
            str_: f!("PyObject_Str"),
            dir: f!("PyObject_Dir"),
            err_fetch: f!("PyErr_Fetch"),
            err_normalize: f!("PyErr_NormalizeException"),
            err_clear: f!("PyErr_Clear"),
            exc_matches: f!("PyErr_ExceptionMatches"),
            incref: f!("Py_IncRef"),
            decref: f!("Py_DecRef"),
            long_from: f!("PyLong_FromLongLong"),
            long_as: f!("PyLong_AsLongLongAndOverflow"),
            float_from: f!("PyFloat_FromDouble"),
            float_as: f!("PyFloat_AsDouble"),
            bool_from: f!("PyBool_FromLong"),
            unicode_from: f!("PyUnicode_FromStringAndSize"),
            unicode_as: f!("PyUnicode_AsUTF8AndSize"),
            list_new: f!("PyList_New"),
            list_set: f!("PyList_SetItem"),
            list_size: f!("PyList_Size"),
            list_get: f!("PyList_GetItem"),
            tuple_new: f!("PyTuple_New"),
            tuple_set: f!("PyTuple_SetItem"),
            tuple_size: f!("PyTuple_Size"),
            tuple_get: f!("PyTuple_GetItem"),
            dict_new: f!("PyDict_New"),
            dict_set: f!("PyDict_SetItem"),
            dict_next: f!("PyDict_Next"),
            set_new: f!("PySet_New"),
            set_add: f!("PySet_Add"),
            isinstance: f!("PyObject_IsInstance"),
            object_type: f!("PyObject_Type"),
            get_iter: f!("PyObject_GetIter"),
            iter_next: f!("PyIter_Next"),
            none: sym("_Py_NoneStruct")? as usize,
            t_bool: sym("PyBool_Type")? as usize,
            t_long: sym("PyLong_Type")? as usize,
            t_float: sym("PyFloat_Type")? as usize,
            t_unicode: sym("PyUnicode_Type")? as usize,
            t_list: sym("PyList_Type")? as usize,
            t_tuple: sym("PyTuple_Type")? as usize,
            t_dict: sym("PyDict_Type")? as usize,
            t_set: sym("PySet_Type")? as usize,
            t_frozenset: sym("PyFrozenSet_Type")? as usize,
            t_decimal: t_decimal as usize,
            attribute_error: attribute_error as usize,
        };
        save_thread();
        Some(api)
    }
}

fn api() -> &'static Api {
    API.get().and_then(|a| a.as_ref()).expect("python is loaded by __python_import")
}

/// The lock of the Python interpreter, held while it lives.
struct Gil(i32);

impl Gil {
    fn take() -> Gil {
        Gil((api().gil_ensure)())
    }
}

impl Drop for Gil {
    fn drop(&mut self) {
        (api().gil_release)(self.0)
    }
}

/// A Python value (a reference the program holds).
pub struct PyObj(usize);

impl Drop for PyObj {
    fn drop(&mut self) {
        let _g = Gil::take();
        (api().decref)(self.0 as P);
    }
}

fn text_of(p: P) -> Option<String> {
    let a = api();
    let mut n: isize = 0;
    let s = (a.unicode_as)(p, &mut n);
    if s.is_null() {
        (a.err_clear)();
        return None;
    }
    let bytes = unsafe { std::slice::from_raw_parts(s as *const u8, n as usize) };
    Some(String::from_utf8_lossy(bytes).into_owned())
}

/// str() of an object (a new reference is taken and given back).
fn str_of(p: P, repr: bool) -> Option<String> {
    let a = api();
    let s = if repr { (a.repr)(p) } else { (a.str_)(p) };
    if s.is_null() {
        (a.err_clear)();
        return None;
    }
    let t = text_of(s);
    (a.decref)(s);
    t
}

/// The pending Python exception as an error value.
fn python_error(span: u32) -> crate::state::Fail {
    let a = api();
    let (mut t, mut v, mut tb): (P, P, P) = (std::ptr::null_mut(), std::ptr::null_mut(), std::ptr::null_mut());
    (a.err_fetch)(&mut t, &mut v, &mut tb);
    (a.err_normalize)(&mut t, &mut v, &mut tb);
    let mut kind = String::from("Exception");
    if !t.is_null() {
        let n = (a.getattr)(t, c"__name__".as_ptr());
        if !n.is_null() {
            kind = text_of(n).unwrap_or(kind);
            (a.decref)(n);
        }
    }
    let message = if v.is_null() { String::new() } else { str_of(v, false).unwrap_or_default() };
    for p in [t, v, tb] {
        if !p.is_null() {
            (a.decref)(p);
        }
    }
    (a.err_clear)();
    failure_as("Error", "Error.python", span, &[("type", kind), ("message", message)])
}

fn is(p: P, t: usize) -> bool {
    t != 0 && (api().isinstance)(p, t as P) == 1
}

fn exact(p: P, types: &[usize]) -> bool {
    let a = api();
    let t = (a.object_type)(p);
    let found = types.contains(&(t as usize));
    (a.decref)(t);
    found
}

/// A Nexo value as a new Python reference.
fn to_python(v: V, span: u32) -> R<P> {
    let a = api();
    let owned = crate::pipe::bind(v)?;
    let v = owned.v();
    let p = match v.tag {
        T_NONE => {
            (a.incref)(a.none as P);
            a.none as P
        }
        T_BOOL => (a.bool_from)((v.bits != 0) as i64),
        T_INT => (a.long_from)(v.bits as i64),
        T_FLOAT => (a.float_from)(f64::from_bits(v.bits)),
        T_LITFLOAT => (a.float_from)(v.litfloat().value),
        T_SSTR => {
            let s = v.as_str().unwrap();
            (a.unicode_from)(s.as_ptr() as *const c_char, s.len() as isize)
        }
        _ => match v.get() {
            Some(Obj::Str(s)) => (a.unicode_from)(s.as_ptr() as *const c_char, s.len() as isize),
            Some(Obj::Decimal(d)) => {
                let text = d.fixed();
                let s = (a.unicode_from)(text.as_ptr() as *const c_char, text.len() as isize);
                let args = (a.tuple_new)(1);
                (a.tuple_set)(args, 0, s);
                let r = (a.call)(a.t_decimal as P, args, std::ptr::null_mut());
                (a.decref)(args);
                if r.is_null() {
                    return Err(python_error(span));
                }
                r
            }
            Some(Obj::List(items)) => {
                let items = items.borrow().clone();
                let l = (a.list_new)(items.len() as isize);
                for (i, item) in items.iter().enumerate() {
                    match to_python(item.v(), span) {
                        Ok(x) => {
                            (a.list_set)(l, i as isize, x);
                        }
                        Err(e) => {
                            (a.decref)(l);
                            return Err(e);
                        }
                    }
                }
                l
            }
            Some(Obj::Map(m)) => {
                let entries = m.borrow().entries.clone();
                let d = (a.dict_new)();
                for (k, val) in &entries {
                    let pair = to_python(k.v(), span).and_then(|kp| match to_python(val.v(), span) {
                        Ok(vp) => Ok((kp, vp)),
                        Err(e) => {
                            (a.decref)(kp);
                            Err(e)
                        }
                    });
                    let (kp, vp) = match pair {
                        Ok(x) => x,
                        Err(e) => {
                            (a.decref)(d);
                            return Err(e);
                        }
                    };
                    (a.dict_set)(d, kp, vp);
                    (a.decref)(kp);
                    (a.decref)(vp);
                }
                d
            }
            Some(Obj::Set(s)) => {
                let items = s.borrow().items.clone();
                let out = (a.set_new)(std::ptr::null_mut());
                for item in &items {
                    let x = match to_python(item.v(), span) {
                        Ok(x) => x,
                        Err(e) => {
                            (a.decref)(out);
                            return Err(e);
                        }
                    };
                    (a.set_add)(out, x);
                    (a.decref)(x);
                }
                out
            }
            Some(Obj::Record(r)) => {
                let values = r.fields.borrow().clone();
                let d = (a.dict_new)();
                for (f, val) in r.ty.fields.iter().zip(values.iter()) {
                    let vp = match to_python(val.v(), span) {
                        Ok(x) => x,
                        Err(e) => {
                            (a.decref)(d);
                            return Err(e);
                        }
                    };
                    let kp = (a.unicode_from)(f.name.as_ptr() as *const c_char, f.name.len() as isize);
                    (a.dict_set)(d, kp, vp);
                    (a.decref)(kp);
                    (a.decref)(vp);
                }
                d
            }
            Some(Obj::Native(n)) if n.as_any().downcast_ref::<PyObj>().is_some() => {
                let p = n.as_any().downcast_ref::<PyObj>().unwrap().0 as P;
                (a.incref)(p);
                p
            }
            _ => return Err(diag(Diagnostic::new_spanless("python_value").arg("vtype", type_name(v)), span)),
        },
    };
    Ok(p)
}

/// A Python value (borrowed) as a Nexo value.
fn to_nexo(p: P, span: u32) -> R<Val> {
    let a = api();
    if p as usize == a.none {
        return Ok(Val::none());
    }
    if is(p, a.t_bool) {
        let mut overflow = 0;
        return Ok(Val::bool((a.long_as)(p, &mut overflow) != 0));
    }
    if is(p, a.t_unicode) {
        return Ok(Val::str(text_of(p).unwrap_or_default()));
    }
    if is(p, a.t_long) {
        let mut overflow = 0;
        let n = (a.long_as)(p, &mut overflow);
        if overflow != 0 {
            (a.err_clear)();
            return Err(failure("Overflow", span, &[]));
        }
        return Ok(Val::int(n));
    }
    if is(p, a.t_float) {
        return Ok(Val::float((a.float_as)(p)));
    }
    if is(p, a.t_decimal) {
        if let Some(d) = str_of(p, false).and_then(|t| crate::dec::Dec::parse(&t)) {
            return Ok(decimal(d));
        }
    }
    // only the plain containers: a subclass (Counter...) keeps its methods
    if exact(p, &[a.t_list, a.t_tuple]) {
        let list = exact(p, &[a.t_list]);
        let n = if list { (a.list_size)(p) } else { (a.tuple_size)(p) };
        let mut items = Vec::new();
        for i in 0..n {
            let x = if list { (a.list_get)(p, i) } else { (a.tuple_get)(p, i) };
            items.push(to_nexo(x, span)?);
        }
        return Ok(list_value(items));
    }
    if exact(p, &[a.t_dict]) {
        let mut pos: isize = 0;
        let (mut k, mut v): (P, P) = (std::ptr::null_mut(), std::ptr::null_mut());
        let mut pairs = Vec::new();
        while (a.dict_next)(p, &mut pos, &mut k, &mut v) != 0 {
            pairs.push((to_nexo(k, span)?, to_nexo(v, span)?));
        }
        return Ok(crate::coll::map_from(pairs));
    }
    if exact(p, &[a.t_set, a.t_frozenset]) {
        let it = (a.get_iter)(p);
        let mut items = Vec::new();
        loop {
            let x = (a.iter_next)(it);
            if x.is_null() {
                break;
            }
            let v = to_nexo(x, span);
            (a.decref)(x);
            items.push(v?);
        }
        (a.decref)(it);
        let raw: Vec<V> = items.iter().map(|v| v.v()).collect();
        return crate::coll::new_set(&raw, span);
    }
    (a.incref)(p);
    Ok(native(PyObj(p as usize)))
}

fn list_value(items: Vec<Val>) -> Val {
    list(items)
}

impl Native for PyObj {
    fn type_name(&self) -> &str {
        "python"
    }
    fn repr(&self) -> String {
        let _g = Gil::take();
        str_of(self.0 as P, true).unwrap_or_else(|| "python".into())
    }
    fn member(&self, _me: V, name: &str, span: u32) -> R<Val> {
        let _g = Gil::take();
        let a = api();
        let n = CString::new(name).unwrap_or_default();
        let r = (a.getattr)(self.0 as P, n.as_ptr());
        if r.is_null() {
            if (a.exc_matches)(a.attribute_error as P) != 0 {
                (a.err_clear)();
                let names = public_names(self.0 as P);
                let names: Vec<&str> = names.iter().map(|s| s.as_str()).collect();
                return Err(crate::stdlib::missing_member("python", name, &names, span));
            }
            return Err(python_error(span));
        }
        let v = to_nexo(r, span);
        (a.decref)(r);
        v
    }
    fn call(&self, _me: V, args: &[V], names: &[Option<&str>], span: u32) -> Option<R<Val>> {
        Some(self.call_python(args, names, span))
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

fn public_names(p: P) -> Vec<String> {
    let a = api();
    let d = (a.dir)(p);
    if d.is_null() {
        (a.err_clear)();
        return Vec::new();
    }
    let mut out = Vec::new();
    for i in 0..(a.list_size)(d) {
        if let Some(t) = text_of((a.list_get)(d, i)) {
            if !t.starts_with('_') {
                out.push(t);
            }
        }
    }
    (a.decref)(d);
    out
}

impl PyObj {
    fn call_python(&self, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        let mut seen: Vec<&str> = Vec::new();
        for n in names.iter().flatten() {
            if seen.contains(n) {
                return Err(diag(Diagnostic::new_spanless("arg_twice").arg("param", *n), span));
            }
            seen.push(n);
        }
        let _g = Gil::take();
        let a = api();
        let positional: Vec<V> = args.iter().zip(names).filter(|(_, n)| n.is_none()).map(|(v, _)| *v).collect();
        let tuple = (a.tuple_new)(positional.len() as isize);
        let mut kwargs: P = std::ptr::null_mut();
        let cleanup = |tuple: P, kwargs: P| {
            (a.decref)(tuple);
            if !kwargs.is_null() {
                (a.decref)(kwargs);
            }
        };
        for (i, v) in positional.iter().enumerate() {
            match to_python(*v, span) {
                Ok(p) => {
                    (a.tuple_set)(tuple, i as isize, p);
                }
                Err(e) => {
                    cleanup(tuple, kwargs);
                    return Err(e);
                }
            }
        }
        for (v, n) in args.iter().zip(names) {
            let Some(n) = n else { continue };
            if kwargs.is_null() {
                kwargs = (a.dict_new)();
            }
            let p = match to_python(*v, span) {
                Ok(p) => p,
                Err(e) => {
                    cleanup(tuple, kwargs);
                    return Err(e);
                }
            };
            let k = (a.unicode_from)(n.as_ptr() as *const c_char, n.len() as isize);
            (a.dict_set)(kwargs, k, p);
            (a.decref)(k);
            (a.decref)(p);
        }
        let r = (a.call)(self.0 as P, tuple, kwargs);
        cleanup(tuple, kwargs);
        if r.is_null() {
            return Err(python_error(span));
        }
        let v = to_nexo(r, span);
        (a.decref)(r);
        v
    }
}

/// `__python_import(name, info)`: the module (info is what the compiler
/// found: "ok\n<libpython>\n<sys.path...>" or "err\npython_missing").
pub fn import(name: V, info: V, span: u32) -> R<Val> {
    let name = name.as_str().unwrap_or("").to_string();
    let info = info.as_str().unwrap_or("err\npython_missing").to_string();
    let loaded = API.get_or_init(|| {
        let mut lines = info.lines();
        if lines.next() != Some("ok") {
            return None;
        }
        let lib = lines.next().unwrap_or("");
        let paths: Vec<&str> = lines.collect();
        load(lib, &paths)
    });
    if loaded.is_none() {
        return Err(diag(Diagnostic::new_spanless("python_missing"), span));
    }
    let _g = Gil::take();
    let a = api();
    let n = CString::new(name.clone()).unwrap_or_default();
    let m = (a.import)(n.as_ptr());
    if m.is_null() {
        let (mut t, mut v, mut tb): (P, P, P) = (std::ptr::null_mut(), std::ptr::null_mut(), std::ptr::null_mut());
        (a.err_fetch)(&mut t, &mut v, &mut tb);
        (a.err_normalize)(&mut t, &mut v, &mut tb);
        let mut kind = String::from("ImportError");
        if !t.is_null() {
            let tn = (a.getattr)(t, c"__name__".as_ptr());
            if !tn.is_null() {
                kind = text_of(tn).unwrap_or(kind);
                (a.decref)(tn);
            }
        }
        let message = if v.is_null() { String::new() } else { str_of(v, false).unwrap_or_default() };
        for p in [t, v, tb] {
            if !p.is_null() {
                (a.decref)(p);
            }
        }
        (a.err_clear)();
        let d = Diagnostic::new_spanless("python_module").arg("name", name).arg("reason", format!("{kind}: {message}"));
        return Err(diag(d, span));
    }
    Ok(native(PyObj(m as usize)))
}
