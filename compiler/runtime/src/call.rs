//! Calls through values: functions, closures, bound methods, builtins,
//! record and variant constructors. Arguments are bound to parameters by
//! position and by name (section 2); omitted parameters with a default
//! arrive as MISSING and the callee evaluates the default.

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

pub fn invoke(f: &'static Func, args: &[V], span: u32) -> R<Val> {
    let r = unsafe { Val::adopt((f.code)(crate::state::tls(), f as *const Func, args.as_ptr(), span)) };
    check()?;
    Ok(r)
}

/// Call a value with positional arguments only.
pub fn call_value(f: V, args: &[V], span: u32) -> R<Val> {
    static POSITIONAL: [Option<&str>; 8] = [None; 8];
    if args.len() <= 8 {
        return call(f, args, &POSITIONAL[..args.len()], span);
    }
    let names: Vec<Option<&str>> = vec![None; args.len()];
    call(f, args, &names, span)
}

/// Call any value.
pub fn call(f: V, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    match f.get() {
        Some(Obj::Func(func)) => {
            if plain_args(func.info.params().len(), args, names) {
                return invoke(func, args, span);
            }
            let bound = bind_args(func.info.name(), &fn_params(func.info), args, names, span)?;
            // lazy values are read; float literals stay literals until the
            // callee checks them against the parameter types
            let forced = force_all(&bound)?;
            invoke(func, &raw_all(&forced), span)
        }
        Some(Obj::Bound(receiver, method)) => call_method_value(receiver.v(), method.v(), args, names, span),
        Some(Obj::BuiltinMethod(obj, name)) => crate::stdlib::call_method(obj.v(), name, args, names, span),
        Some(Obj::Builtin(id)) => crate::stdlib::call(*id, args, names, span),
        Some(Obj::Type(TypeRef::Record(rt))) => construct(rt, args, names, span),
        Some(Obj::ChannelType(elem)) => {
            let bound = bind_args("channel", &[("size", true)], args, names, span)?;
            crate::sync::new_channel(elem.clone(), bound[0], span)
        }
        Some(Obj::Variant(var)) if !var.params.is_empty() => construct_variant(var, args, names, span),
        Some(Obj::Type(TypeRef::Builtin(name))) => match *name {
            "decimal" => crate::stdlib::call(crate::stdlib::B_DECIMAL, args, names, span),
            "float" => crate::stdlib::call(crate::stdlib::B_FLOAT, args, names, span),
            "int" => crate::stdlib::call(crate::stdlib::B_INT, args, names, span),
            "str" => crate::stdlib::call(crate::stdlib::B_STR, args, names, span),
            "date" => crate::time::make_date(args, names, span),
            "time" => crate::time::make_time(args, names, span),
            _ => Err(not_callable(f, span)),
        },
        Some(Obj::Native(n)) => match n.call(f, args, names, span) {
            Some(r) => r,
            None => Err(not_callable(f, span)),
        },
        _ => Err(not_callable(f, span)),
    }
}

fn not_callable(f: V, span: u32) -> crate::state::Fail {
    diag(Diagnostic::new_spanless("not_callable").arg("vtype", type_name(f)), span)
}

/// Every parameter gets a positional argument that needs nothing done to
/// it: the arguments can be passed as they are.
fn plain_args(params: usize, args: &[V], names: &[Option<&str>]) -> bool {
    args.len() == params
        && names.iter().all(|n| n.is_none())
        && args.iter().all(|a| a.tag != T_OBJ || !matches!(a.get(), Some(Obj::Lazy(_))))
}

/// Arguments read (lazy pipelines become lists); MISSING stays.
fn force_all(bound: &[V]) -> R<Vec<Val>> {
    let mut out = Vec::with_capacity(bound.len());
    for v in bound {
        out.push(if v.tag == T_MISSING { Val::simple(*v) } else { force(*v)? });
    }
    Ok(out)
}

/// Call a method of a user type with its receiver.
pub fn call_method_value(receiver: V, method: V, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    let Some(Obj::Func(func)) = method.get() else {
        return call(method, args, names, span);
    };
    let infos = func.info.params();
    if infos.first().is_some_and(|p| p.name.get() == Some("self")) && plain_args(infos.len() - 1, args, names) {
        if args.len() < 8 {
            let mut all = [NONE; 8];
            all[0] = receiver;
            all[1..=args.len()].copy_from_slice(args);
            return invoke(func, &all[..=args.len()], span);
        }
    }
    let params = fn_params(func.info);
    let takes_self = params.first().is_some_and(|(n, _)| *n == "self");
    let rest = if takes_self { &params[1..] } else { &params[..] };
    let bound = bind_args(func.info.name(), rest, args, names, span)?;
    let mut forced = force_all(&bound)?;
    if takes_self {
        forced.insert(0, Val::from(receiver));
    }
    invoke(func, &raw_all(&forced), span)
}

/// Build a record: missing fields take their default, or none when the
/// field is optional (D31).
pub fn construct(rt: &'static crate::types::RecordType, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    if plain_args(rt.fields.len(), args, names) {
        let mut values = Vec::with_capacity(args.len());
        for (f, v) in rt.fields.iter().zip(args) {
            let v = conform(*v, f.ty.expect("record fields are typed"), &f.name, span)?;
            values.push(field_value(f, bind(v.v())?));
        }
        return Ok(Val::new(Obj::Record(Record {
            ty: rt,
            fields: Mut::new(values),
            birth: std::cell::Cell::new(crate::state::stamp()),
        })));
    }
    let params: Vec<(&str, bool)> =
        rt.fields.iter().map(|f| (f.name.as_str(), f.default.tag != T_UNDEF || f.optional)).collect();
    let bound = bind_args(&rt.name, &params, args, names, span)?;
    let mut values = Vec::with_capacity(bound.len());
    for (f, v) in rt.fields.iter().zip(bound) {
        let v = if v.tag == T_MISSING {
            if f.default.tag != T_UNDEF {
                call_value(f.default.v(), &[], span)?
            } else {
                Val::none()
            }
        } else {
            Val::from(v)
        };
        let v = conform(v.v(), f.ty.expect("record fields are typed"), &f.name, span)?;
        values.push(field_value(f, bind(v.v())?));
    }
    Ok(Val::new(Obj::Record(Record { ty: rt, fields: Mut::new(values), birth: std::cell::Cell::new(crate::state::stamp()) })))
}

pub fn construct_variant(
    var: &'static crate::types::VariantInfo,
    args: &[V],
    names: &[Option<&str>],
    span: u32,
) -> R<Val> {
    let params: Vec<(&str, bool)> = var.params.iter().map(|p| (p.name.as_str(), false)).collect();
    let bound = bind_args(&var.name, &params, args, names, span)?;
    let mut values = Vec::with_capacity(bound.len());
    for (p, v) in var.params.iter().zip(bound) {
        let v = match p.ty {
            Some(t) => conform(v, t, &p.name, span)?,
            None => Val::from(v),
        };
        values.push(bind(v.v())?);
    }
    Ok(Val::new(Obj::VariantValue(VariantValue { variant: var, fields: values })))
}

fn methods_of(v: V) -> Option<&'static Mut<Vec<(String, Val)>>> {
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
    m.iter().find(|(n, _)| n == name).map(|(_, f)| f.v())
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
pub fn get_attr(obj: V, name: &str, span: u32) -> R<Val> {
    match obj.get() {
        Some(Obj::Record(r)) => {
            if let Some(i) = r.ty.fields.iter().position(|f| f.name == name) {
                return Ok(upgrade(r.fields.borrow()[i].v()));
            }
            if let Some(m) = find_method(obj, name) {
                return Ok(bound(obj, m));
            }
            let mut c: Vec<String> = r.ty.fields.iter().map(|f| f.name.clone()).collect();
            c.extend(method_names(obj));
            Err(member_error(obj, name, span, c))
        }
        Some(Obj::VariantValue(vv)) => {
            if let Some(i) = vv.variant.params.iter().position(|f| f.name == name) {
                return Ok(vv.fields[i].clone());
            }
            if let Some(m) = find_method(obj, name) {
                return Ok(bound(obj, m));
            }
            let mut c: Vec<String> = vv.variant.params.iter().map(|f| f.name.clone()).collect();
            c.extend(method_names(obj));
            Err(member_error(obj, name, span, c))
        }
        Some(Obj::Variant(_)) => {
            if let Some(m) = find_method(obj, name) {
                return Ok(bound(obj, m));
            }
            Err(member_error(obj, name, span, method_names(obj)))
        }
        Some(Obj::Error(e)) => match name {
            "message" => Ok(Val::str(e.message.clone())),
            "cause" => Ok(e.cause.clone()),
            "line" => Ok(e.line.map(|l| Val::int(l as i64)).unwrap_or(Val::none())),
            "kind" => Ok(Val::str(e.kind.clone())),
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
pub fn call_method(obj: V, name: &str, args: &[V], names: &[Option<&str>], span: u32, name_span: u32) -> R<Val> {
    match obj.get() {
        Some(Obj::Record(r)) if r.ty.fields.iter().any(|f| f.name == name) => {
            let f = get_attr(obj, name, name_span)?;
            call(f.v(), args, names, span)
        }
        Some(Obj::VariantValue(vv)) if vv.variant.params.iter().any(|f| f.name == name) => {
            let f = get_attr(obj, name, name_span)?;
            call(f.v(), args, names, span)
        }
        Some(Obj::Record(_) | Obj::VariantValue(_) | Obj::Variant(_)) => match find_method(obj, name) {
            Some(m) => call_method_value(obj, m, args, names, span),
            None => {
                get_attr(obj, name, name_span)?;
                unreachable!()
            }
        },
        Some(Obj::Str(_) | Obj::List(_) | Obj::Lazy(_) | Obj::Map(_) | Obj::Set(_) | Obj::Channel(_)) => {
            let name = crate::stdlib::method_name(obj, name, name_span)?;
            crate::stdlib::call_method(obj, name, args, names, span)
        }
        _ => {
            let f = get_attr(obj, name, name_span)?;
            call(f.v(), args, names, span)
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
            crate::cow::check_write(r, name_span)?;
            let v = conform(value, f.ty.expect("record fields are typed"), name, value_span)?;
            let v = bind(v.v())?;
            r.fields.borrow_mut()[i] = field_value(f, v);
            Ok(())
        }
        _ if obj.is_none() => Err(diag(Diagnostic::new_spanless("none_member").arg("name", name), name_span)),
        _ => Err(diag(
            Diagnostic::new_spanless("readonly_member").arg("name", name).arg("vtype", type_name(obj)),
            name_span,
        )),
    }
}

fn bound(obj: V, method: V) -> Val {
    Val::new(Obj::Bound(Val::from(obj), Val::from(method)))
}

/// What a field stores: a `weak` field keeps a weak reference.
pub fn field_value(f: &crate::types::FieldInfo, v: Val) -> Val {
    if f.weak && v.tag == T_OBJ {
        unsafe { Val::adopt(downgrade(v.v())) }
    } else {
        v
    }
}

/// Pointer to the field `name` of a record, for a change through it
/// (`order.items.add(x)`): the record must be one this task may change,
/// and the collection in the field is made unique. None when `obj` is not
/// a record with that field: `scratch` then holds the attribute's value.
pub fn field_slot(obj: V, name: &str, scratch: &mut Val, span: u32) -> R<Option<*mut V>> {
    if let Some(Obj::Record(r)) = obj.get() {
        if let Some(i) = r.ty.fields.iter().position(|f| f.name == name) {
            if r.ty.fields[i].weak {
                *scratch = upgrade(r.fields.borrow()[i].v());
                return Ok(None);
            }
            crate::cow::check_write(r, span)?;
            let slot = &mut r.fields.borrow_mut()[i];
            crate::cow::make_unique_val(slot);
            return Ok(Some(slot as *mut Val as *mut V));
        }
    }
    *scratch = get_attr(obj, name, span)?;
    Ok(None)
}
