//! `use c "library"` (section 18, D110). The compiler read the header
//! (nexoc/cheader.rs) and passes its functions; a call puts the arguments
//! in the registers of the C calling convention, as phase 0's clib.py does
//! with ctypes: integers (and texts, as pointers) in the integer registers,
//! floats in the vector registers, each in order.

use std::ffi::{c_char, c_void, CStr, CString};
use std::sync::Mutex;

use nexo_diag::Diagnostic;

use crate::state::{diag, failure, R};
use crate::stdlib::expects;
use crate::value::*;

#[cfg(target_arch = "aarch64")]
const INT_REGS: usize = 8;
#[cfg(not(target_arch = "aarch64"))]
const INT_REGS: usize = 6;
const FLOAT_REGS: usize = 8;
const RTLD_NOW: i32 = 2;

extern "C" {
    fn dlopen(file: *const c_char, mode: i32) -> *mut c_void;
    fn dlsym(handle: *mut c_void, name: *const c_char) -> *mut c_void;
}

pub struct CLibrary {
    name: String,
    bound: String,
    path: String,
    /// name -> [result, parameters...] (the codes of cheader.rs)
    sigs: Vec<(String, Vec<String>)>,
    handle: Mutex<usize>,
}

impl Native for CLibrary {
    fn type_name(&self) -> &str {
        "c_library"
    }
    fn repr(&self) -> String {
        format!("c_library(\"{}\")", self.name)
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        if !self.sigs.iter().any(|(n, _)| n == name) {
            let names: Vec<&str> = self.sigs.iter().map(|(n, _)| n.as_str()).collect();
            return Err(crate::stdlib::missing_member("c_library", name, &names, span));
        }
        Ok(Val::new(Obj::BuiltinMethod(Val::from(me), crate::num::intern(name))))
    }
    fn call_method(&self, _me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        self.call_c(name, args, names, span)
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// `__c_library(name, bound, info)`: the library, or the error the
/// compiler found reading its header.
pub fn library(name: V, bound: V, info: V, span: u32) -> R<Val> {
    let name = name.as_str().unwrap_or("").to_string();
    let bound = bound.as_str().unwrap_or("").to_string();
    let info = info.as_str().unwrap_or("err\nc_compiler");
    let mut lines = info.lines();
    if lines.next() != Some("ok") {
        let key = lines.next().unwrap_or("c_compiler");
        let mut d = Diagnostic::new_spanless(crate::num::intern(key));
        for l in lines {
            if let Some((k, v)) = l.split_once('=') {
                d = d.arg(crate::num::intern(k), v.to_string());
            }
        }
        return Err(diag(d, span));
    }
    let path = lines.next().unwrap_or("").to_string();
    let sigs = lines
        .map(|l| {
            let mut parts = l.split(' ').map(|p| p.to_string());
            let n = parts.next().unwrap_or_default();
            (n, parts.collect())
        })
        .collect();
    Ok(native(CLibrary { name, bound, path, sigs, handle: Mutex::new(0) }))
}

type IntFn = extern "C" fn(i64, i64, i64, i64, i64, i64, i64, i64, f64, f64, f64, f64, f64, f64, f64, f64) -> i64;
type FloatFn = extern "C" fn(i64, i64, i64, i64, i64, i64, i64, i64, f64, f64, f64, f64, f64, f64, f64, f64) -> f64;

impl CLibrary {
    fn symbol(&self, name: &str, span: u32) -> R<*mut c_void> {
        let mut handle = self.handle.lock().unwrap();
        if *handle == 0 {
            let path = CString::new(self.path.clone()).unwrap_or_default();
            *handle = unsafe { dlopen(path.as_ptr(), RTLD_NOW) } as usize;
        }
        let sym = if *handle == 0 {
            std::ptr::null_mut()
        } else {
            let n = CString::new(name).unwrap_or_default();
            unsafe { dlsym(*handle as *mut c_void, n.as_ptr()) }
        };
        if sym.is_null() {
            let d = Diagnostic::new_spanless("c_symbol").arg("name", name).arg("lib", self.name.clone());
            return Err(diag(d, span));
        }
        Ok(sym)
    }

    fn call_c(&self, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        let codes = &self.sigs.iter().find(|(n, _)| n == name).unwrap().1;
        let (ret, params) = (codes[0].as_str(), &codes[1..]);
        let fname = format!("{}.{name}", self.bound);
        if params.last().map(|p| p.as_str()) == Some("...") {
            return Err(diag(Diagnostic::new_spanless("c_variadic").arg("fn", fname), span));
        }
        for c in std::iter::once(&codes[0]).chain(params) {
            if let Some(t) = c.strip_prefix("x:") {
                return Err(diag(Diagnostic::new_spanless("c_type").arg("fn", fname).arg("type", t.replace('~', " ")), span));
            }
        }
        let floats = params.iter().filter(|c| *c == "f32" || *c == "f64").count();
        if params.len() - floats > INT_REGS || floats > FLOAT_REGS {
            return Err(diag(Diagnostic::new_spanless("c_args").arg("fn", fname), span));
        }
        if let Some(n) = names.iter().flatten().next() {
            let d = Diagnostic::new_spanless("arg_unknown").arg("fn", fname).arg("param", *n).arg("params", "-");
            return Err(diag(d, span));
        }
        if args.len() > params.len() {
            let d = Diagnostic::new_spanless("arg_too_many")
                .arg("fn", fname)
                .arg("n", params.len().to_string())
                .arg("m", args.len().to_string());
            return Err(diag(d, span));
        }
        if args.len() < params.len() {
            let d = Diagnostic::new_spanless("arg_missing").arg("param", (args.len() + 1).to_string()).arg("fn", fname);
            return Err(diag(d, span));
        }
        let sym = self.symbol(name, span)?;
        let mut ints = [0i64; 8];
        let mut fl = [0f64; 8];
        let (mut ni, mut nf) = (0, 0);
        // the texts live until the call returns
        let mut texts: Vec<CString> = Vec::new();
        for (c, a) in params.iter().zip(args) {
            let owned = crate::pipe::bind(*a)?;
            let v = owned.v();
            match c.as_str() {
                "s" => {
                    let Some(s) = v.as_str() else { return Err(expects(&fname, "str", v, span)) };
                    let text = CString::new(s.split('\0').next().unwrap_or("")).unwrap_or_default();
                    ints[ni] = text.as_ptr() as i64;
                    texts.push(text);
                    ni += 1;
                }
                "f32" | "f64" => {
                    let x = match v.tag {
                        T_INT => v.bits as i64 as f64,
                        T_FLOAT => f64::from_bits(v.bits),
                        T_LITFLOAT => v.litfloat().value,
                        _ => return Err(expects(&fname, "float", v, span)),
                    };
                    fl[nf] = if c == "f32" { f64::from_bits((x as f32).to_bits() as u64) } else { x };
                    nf += 1;
                }
                "b" => {
                    let Some(b) = v.as_bool() else { return Err(expects(&fname, "bool", v, span)) };
                    ints[ni] = b as i64;
                    ni += 1;
                }
                _ => {
                    if v.tag != T_INT {
                        return Err(expects(&fname, "int", v, span));
                    }
                    ints[ni] = v.bits as i64;
                    ni += 1;
                }
            }
        }
        let [a0, a1, a2, a3, a4, a5, a6, a7] = ints;
        let [f0, f1, f2, f3, f4, f5, f6, f7] = fl;
        if ret == "f32" || ret == "f64" {
            let f: FloatFn = unsafe { std::mem::transmute(sym) };
            let r = f(a0, a1, a2, a3, a4, a5, a6, a7, f0, f1, f2, f3, f4, f5, f6, f7);
            drop(texts);
            let x = if ret == "f32" { f32::from_bits(r.to_bits() as u32) as f64 } else { r };
            return Ok(Val::float(x));
        }
        let f: IntFn = unsafe { std::mem::transmute(sym) };
        let r = f(a0, a1, a2, a3, a4, a5, a6, a7, f0, f1, f2, f3, f4, f5, f6, f7);
        drop(texts);
        Ok(match ret {
            "v" => Val::none(),
            "b" => Val::bool(r as u8 != 0),
            "s" => {
                if r == 0 {
                    Val::none()
                } else {
                    let text = unsafe { CStr::from_ptr(r as *const c_char) };
                    Val::str(text.to_string_lossy().into_owned())
                }
            }
            "i8" => Val::int(r as i8 as i64),
            "u8" => Val::int(r as u8 as i64),
            "i16" => Val::int(r as i16 as i64),
            "u16" => Val::int(r as u16 as i64),
            "i32" => Val::int(r as i32 as i64),
            "u32" => Val::int(r as u32 as i64),
            "u64" if r < 0 => return Err(failure("Overflow", span, &[])),
            _ => Val::int(r),
        })
    }
}
