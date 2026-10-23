//! Calls through values: functions, closures, bound methods, builtins,
//! record and variant constructors. Arguments are bound to parameters by
//! position and by name (section 2); omitted parameters with a default
//! arrive as MISSING and the callee evaluates the default.

use std::cell::RefCell;

use nexo_diag::{suggest, Diagnostic};

use crate::fmt::type_name;
use crate::pipe::{bind, force};
use crate::state::{check, diag, R};
use crate::types::{conform, TypeRef};
use crate::value::*;

pub fn is_callable(v: V) -> bool {
    matches!(v.get(), Some(Obj::Func(_) | Obj::Builtin(_) | Obj::Bound(..) | Obj::BuiltinMethod(..)))
}

/// Bind arguments to parameters. `names[i]` is the name of argument i
/// (None when positional). Returns the values in parameter order, with
/// MISSING for omitted parameters that have a default.
pub fn bind_args(
    fn_name: &str,
    params: &[(&str, bool)],
    args: &[V],
    names: &[Option<&str>],
    span: u32,
) -> R<Vec<V>> {
    let mut bound: Vec<Option<V>> = vec![None; params.len()];
    let positional: Vec<V> = args.iter().zip(names).filter(|(_, n)| n.is_none()).map(|(v, _)| *v).collect();
    if positional.len() > params.len() {
        return Err(diag(
            Diagnostic::new_spanless("arg_too_many")
                .arg("fn", fn_name)
                .arg("n", params.len().to_string())
                .arg("m", positional.len().to_string()),
            span,
        ));
    }
    for (i, v) in positional.iter().enumerate() {
        bound[i] = Some(*v);
    }
    for (v, n) in args.iter().zip(names) {
        let Some(n) = n else { continue };
        let Some(i) = params.iter().position(|(p, _)| p == n) else {
            let candidates: Vec<&str> = params.iter().map(|(p, _)| *p).collect();
            let d = match suggest(n, candidates.iter().copied()) {
                Some(s) => Diagnostic::new_spanless("arg_unknown")
                    .fix("suggest")
                    .arg("fn", fn_name)
                    .arg("param", *n)
                    .arg("suggestion", s),
                None => {
                    let list = if candidates.is_empty() { "-".to_string() } else { candidates.join(", ") };
                    Diagnostic::new_spanless("arg_unknown").arg("fn", fn_name).arg("param", *n).arg("params", list)
                }
            };
            return Err(diag(d, span));
        };
        if bound[i].is_some() {
            return Err(diag(Diagnostic::new_spanless("arg_twice").arg("param", *n), span));
        }
        bound[i] = Some(*v);
    }
    let mut out = Vec::with_capacity(params.len());
    for (i, (p, optional)) in params.iter().enumerate() {
        match bound[i] {
            Some(v) => out.push(v),
            None if *optional => out.push(MISSING),
            None => {
                return Err(diag(Diagnostic::new_spanless("arg_missing").arg("param", *p).arg("fn", fn_name), span))
            }
        }
    }
    Ok(out)
}

fn fn_params(info: &'static FnInfo) -> Vec<(&'static str, bool)> {
    info.params().iter().map(|p| (p.name.get().unwrap_or(""), p.optional != 0)).collect()
}

pub fn invoke(f: &'static Func, args: &[V], span: u32) -> R<V> {
    let r = unsafe { (f.code)(f as *const Func, args.as_ptr(), span) };
    check()?;
    Ok(r)
}

/// Call a value with positional arguments only.
pub fn call_value(f: V, args: &[V], span: u32) -> R<V> {
    let names: Vec<Option<&str>> = vec![None; args.len()];
    call(f, args, &names, span)
}

/// Call any value.
pub fn call(f: V, args: &[V], names: &[Option<&str>], span: u32) -> R<V> {
    match f.get() {
        Some(Obj::Func(func)) => {
            let mut bound = bind_args(func.info.name(), &fn_params(func.info), args, names, span)?;
            // lazy values are read; float literals stay literals until the
            // callee checks them against the parameter types
            for v in bound.iter_mut() {
                if v.tag != T_MISSING {
                    *v = force(*v)?;
                }
            }
            invoke(func, &bound, span)
        }
        Some(Obj::Bound(receiver, method)) => call_method_value(*receiver, *method, args, names, span),
        Some(Obj::BuiltinMethod(obj, name)) => crate::stdlib::call_method(*obj, name, args, names, span),
        Some(Obj::Builtin(id)) => crate::stdlib::call(*id, args, names, span),
        Some(Obj::Type(TypeRef::Record(rt))) => construct(rt, args, names, span),
        Some(Obj::Variant(var)) if !var.params.is_empty() => construct_variant(var, args, names, span),
        Some(Obj::Type(TypeRef::Builtin(name))) => match *name {
            "decimal" => crate::stdlib::call(crate::stdlib::B_DECIMAL, args, names, span),
            "float" => crate::stdlib::call(crate::stdlib::B_FLOAT, args, names, span),
            "int" => crate::stdlib::call(crate::stdlib::B_INT, args, names, span),
            "str" => crate::stdlib::call(crate::stdlib::B_STR, args, names, span),
            _ => Err(not_callable(f, span)),
        },
        _ => Err(not_callable(f, span)),
    }
}

fn not_callable(f: V, span: u32) -> crate::state::Fail {
    diag(Diagnostic::new_spanless("not_callable").arg("vtype", type_name(f)), span)
}

/// Call a method of a user type with its receiver.
pub fn call_method_value(receiver: V, method: V, args: &[V], names: &[Option<&str>], span: u32) -> R<V> {
    let Some(Obj::Func(func)) = method.get() else {
        return call(method, args, names, span);
    };
    let params = fn_params(func.info);
    let takes_self = params.first().is_some_and(|(n, _)| *n == "self");
    let rest = if takes_self { &params[1..] } else { &params[..] };
    let mut bound = bind_args(func.info.name(), rest, args, names, span)?;
    for v in bound.iter_mut() {
        if v.tag != T_MISSING {
            *v = force(*v)?;
        }
    }
    if takes_self {
        bound.insert(0, receiver);
    }
    invoke(func, &bound, span)
}

/// Build a record: missing fields take their default, or none when the
/// field is optional (D31).
pub fn construct(rt: &'static crate::types::RecordType, args: &[V], names: &[Option<&str>], span: u32) -> R<V> {
    let params: Vec<(&str, bool)> =
        rt.fields.iter().map(|f| (f.name.as_str(), f.default.tag != T_UNDEF || f.optional)).collect();
    let bound = bind_args(&rt.name, &params, args, names, span)?;
    let mut values = Vec::with_capacity(bound.len());
    for (f, v) in rt.fields.iter().zip(bound) {
        let v = if v.tag == T_MISSING {
            if f.default.tag != T_UNDEF {
                call_value(f.default, &[], span)?
            } else {
                NONE
            }
        } else {
            v
        };
        let v = conform(v, f.ty.expect("record fields are typed"), &f.name, span)?;
        values.push(bind(v)?);
    }
    Ok(V::obj(Obj::Record(Record { ty: rt, fields: RefCell::new(values) })))
}

pub fn construct_variant(
    var: &'static crate::types::VariantInfo,
    args: &[V],
    names: &[Option<&str>],
    span: u32,
) -> R<V> {
    let params: Vec<(&str, bool)> = var.params.iter().map(|p| (p.name.as_str(), false)).collect();
    let bound = bind_args(&var.name, &params, args, names, span)?;
    let mut values = Vec::with_capacity(bound.len());
    for (p, v) in var.params.iter().zip(bound) {
        let v = match p.ty {
            Some(t) => conform(v, t, &p.name, span)?,
            None => v,
        };
        values.push(bind(v)?);
    }
    Ok(V::obj(Obj::VariantValue(VariantValue { variant: var, fields: values })))
}

fn methods_of(v: V) -> Option<&'static RefCell<Vec<(String, V)>>> {
    match v.get()? {
        Obj::Record(r) => Some(&r.ty.methods),
        Obj::VariantValue(vv) => Some(&vv.variant.enum_type().methods),
        Obj::Variant(var) => Some(&var.enum_type().methods),
        _ => None,
    }
}

fn find_method(v: V, name: &str) -> Option<V> {
    let methods = methods_of(v)?;
    let m = methods.borrow();
    m.iter().find(|(n, _)| n == name).map(|(_, f)| *f)
}

fn member_error(obj: V, name: &str, span: u32, candidates: Vec<String>) -> crate::state::Fail {
    let d = match suggest(name, candidates.iter().map(|s| s.as_str())) {
        Some(s) => Diagnostic::new_spanless("no_member")
            .fix("suggest")
            .arg("vtype", type_name(obj))
            .arg("name", name)
            .arg("suggestion", s),
        None => Diagnostic::new_spanless("no_member").arg("vtype", type_name(obj)).arg("name", name),
    };
    diag(d, span)
}

fn method_names(v: V) -> Vec<String> {
    methods_of(v).map(|m| m.borrow().iter().map(|(n, _)| n.clone()).collect()).unwrap_or_default()
}

/// `obj.name`
pub fn get_attr(obj: V, name: &str, span: u32) -> R<V> {
    match obj.get() {
        Some(Obj::Record(r)) => {
            if let Some(i) = r.ty.fields.iter().position(|f| f.name == name) {
                return Ok(r.fields.borrow()[i]);
            }
            if let Some(m) = find_method(obj, name) {
                return Ok(V::obj(Obj::Bound(obj, m)));
            }
            let mut c: Vec<String> = r.ty.fields.iter().map(|f| f.name.clone()).collect();
            c.extend(method_names(obj));
            Err(member_error(obj, name, span, c))
        }
        Some(Obj::VariantValue(vv)) => {
            if let Some(i) = vv.variant.params.iter().position(|f| f.name == name) {
                return Ok(vv.fields[i]);
            }
            if let Some(m) = find_method(obj, name) {
                return Ok(V::obj(Obj::Bound(obj, m)));
            }
            let mut c: Vec<String> = vv.variant.params.iter().map(|f| f.name.clone()).collect();
            c.extend(method_names(obj));
            Err(member_error(obj, name, span, c))
        }
        Some(Obj::Variant(_)) => {
            if let Some(m) = find_method(obj, name) {
                return Ok(V::obj(Obj::Bound(obj, m)));
            }
            Err(member_error(obj, name, span, method_names(obj)))
        }
        Some(Obj::Error(e)) => match name {
            "message" => Ok(V::str(e.message.clone())),
            "cause" => Ok(e.cause),
            "line" => Ok(e.line.map(|l| V::int(l as i64)).unwrap_or(NONE)),
            "kind" => Ok(V::str(e.kind.clone())),
            _ => Err(member_error(
                obj,
                name,
                span,
                ["message", "cause", "line", "kind"].iter().map(|s| s.to_string()).collect(),
            )),
        },
        _ if obj.is_none() => Err(diag(Diagnostic::new_spanless("none_member").arg("name", name), span)),
        _ => crate::stdlib::member(obj, name, span),
    }
}

/// `obj.name(args)` without building a bound method.
pub fn call_method(obj: V, name: &str, args: &[V], names: &[Option<&str>], span: u32, name_span: u32) -> R<V> {
    match obj.get() {
        Some(Obj::Record(r)) if r.ty.fields.iter().any(|f| f.name == name) => {
            let f = get_attr(obj, name, name_span)?;
            call(f, args, names, span)
        }
        Some(Obj::VariantValue(vv)) if vv.variant.params.iter().any(|f| f.name == name) => {
            let f = get_attr(obj, name, name_span)?;
            call(f, args, names, span)
        }
        Some(Obj::Record(_) | Obj::VariantValue(_) | Obj::Variant(_)) => match find_method(obj, name) {
            Some(m) => call_method_value(obj, m, args, names, span),
            None => {
                get_attr(obj, name, name_span)?;
                unreachable!()
            }
        },
        Some(Obj::Str(_) | Obj::List(_) | Obj::Lazy(_) | Obj::Map(_) | Obj::Set(_)) => {
            crate::stdlib::member(obj, name, name_span)?;
            crate::stdlib::call_method(obj, crate::num::intern(name), args, names, span)
        }
        _ => {
            let f = get_attr(obj, name, name_span)?;
            call(f, args, names, span)
        }
    }
}

/// `obj.name = value`
pub fn set_attr(obj: V, name: &str, value: V, name_span: u32, value_span: u32) -> R<()> {
    match obj.get() {
        Some(Obj::Record(r)) => {
            let Some(i) = r.ty.fields.iter().position(|f| f.name == name) else {
                let c: Vec<String> = r.ty.fields.iter().map(|f| f.name.clone()).collect();
                return Err(member_error(obj, name, name_span, c));
            };
            let f = &r.ty.fields[i];
            let v = conform(value, f.ty.expect("record fields are typed"), name, value_span)?;
            let v = bind(v)?;
            r.fields.borrow_mut()[i] = v;
            Ok(())
        }
        _ if obj.is_none() => Err(diag(Diagnostic::new_spanless("none_member").arg("name", name), name_span)),
        _ => Err(diag(
            Diagnostic::new_spanless("readonly_member").arg("name", name).arg("vtype", type_name(obj)),
            name_span,
        )),
    }
}
