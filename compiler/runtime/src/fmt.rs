//! Text of values (D30): `show` is what `print` and interpolation write
//! (texts without quotes), `repr` is how a value appears inside a
//! collection (texts quoted).

use crate::types::TypeRef;
use crate::value::*;

pub fn type_name(v: V) -> String {
    match v.tag {
        T_NONE => "none".into(),
        T_BOOL => "bool".into(),
        T_INT => "int".into(),
        T_FLOAT | T_LITFLOAT => "float".into(),
        T_OBJ => match v.get().unwrap() {
            Obj::Str(_) => "str".into(),
            Obj::Decimal(_) => "decimal".into(),
            Obj::Money(..) => "money".into(),
            Obj::Quantity(..) => "quantity".into(),
            Obj::List(_) | Obj::Lazy(_) => "list".into(),
            Obj::Map(_) => "map".into(),
            Obj::Set(_) => "set".into(),
            Obj::Range(..) => "range".into(),
            Obj::Grouped(_) => "Grouped".into(),
            Obj::Record(r) => r.ty.name.clone(),
            Obj::VariantValue(vv) => vv.variant.enum_type().name.clone(),
            Obj::Variant(var) => var.enum_type().name.clone(),
            Obj::Error(e) => e.kind.clone(),
            Obj::Func(_) | Obj::Bound(..) | Obj::BuiltinMethod(..) | Obj::Builtin(_) => "fn".into(),
            Obj::Type(_) => "type".into(),
            Obj::Currency(_) => "currency".into(),
            Obj::Symbol(_) => "Symbol".into(),
            Obj::Namespace(_) => "Namespace".into(),
            Obj::Channel(_) | Obj::ChannelType => "channel".into(),
        },
        _ => "none".into(),
    }
}

/// Python's `repr(float)`: the shortest text that reads back the same.
pub fn format_float(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    let sci = format!("{:e}", x);
    let (mant, exp) = sci.split_once('e').unwrap();
    let exp: i64 = exp.parse().unwrap();
    let (neg, mant) = match mant.strip_prefix('-') {
        Some(m) => (true, m),
        None => (false, mant),
    };
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let decpt = exp + 1;
    let body = if -4 < decpt && decpt <= 16 {
        let n = digits.len() as i64;
        if decpt <= 0 {
            format!("0.{}{}", "0".repeat((-decpt) as usize), digits)
        } else if decpt >= n {
            format!("{}{}.0", digits, "0".repeat((decpt - n) as usize))
        } else {
            format!("{}.{}", &digits[..decpt as usize], &digits[decpt as usize..])
        }
    } else {
        let m = if digits.len() > 1 { format!("{}.{}", &digits[..1], &digits[1..]) } else { digits.clone() };
        let e = decpt - 1;
        format!("{}e{}{:02}", m, if e < 0 { '-' } else { '+' }, e.abs())
    };
    if neg {
        format!("-{body}")
    } else {
        body
    }
}

pub fn unit_attached(unit: &str) -> bool {
    matches!(unit, "%" | "s" | "ms" | "m" | "h")
}

pub fn format_quantity(value: &crate::dec::Dec, unit: &str) -> String {
    let v = value.fixed();
    if unit_attached(unit) {
        format!("{v}{unit}")
    } else {
        format!("{v} {unit}")
    }
}

pub fn quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            _ => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Text of a value as `print` and interpolation show it.
pub fn show(v: V) -> String {
    if let Some(s) = v.as_str() {
        return s.to_string();
    }
    repr(v)
}

/// Text of a value inside a collection (texts are quoted).
pub fn repr(v: V) -> String {
    let mut out = String::new();
    repr_into(v, &mut out);
    out
}

fn join(items: &[Val], out: &mut String) {
    for (i, item) in items.iter().enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        repr_into(item.v(), out);
    }
}

fn fields_into(names: &[String], values: &[Val], out: &mut String) {
    for (i, (n, v)) in names.iter().zip(values).enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        out.push_str(n);
        out.push_str(": ");
        if v.tag == T_WEAK {
            // a weak field points back (to a parent): only its type is shown
            let target = upgrade(v.v());
            match target.get() {
                Some(Obj::Record(r)) => {
                    out.push_str(&r.ty.name);
                    out.push_str("(...)");
                }
                _ => out.push_str("none"),
            }
            continue;
        }
        repr_into(v.v(), out);
    }
}

pub fn repr_into(v: V, out: &mut String) {
    match v.tag {
        T_NONE => out.push_str("none"),
        T_BOOL => out.push_str(if v.bits != 0 { "true" } else { "false" }),
        T_INT => out.push_str(&(v.bits as i64).to_string()),
        T_FLOAT => out.push_str(&format_float(f64::from_bits(v.bits))),
        T_LITFLOAT => out.push_str(&format_float(v.litfloat().value)),
        T_OBJ => match v.get().unwrap() {
            Obj::Str(s) => out.push_str(&quote(s)),
            Obj::Decimal(d) => out.push_str(&d.fixed()),
            Obj::Money(d, c) => {
                out.push_str(&d.py_str());
                out.push(' ');
                out.push_str(c);
            }
            Obj::Quantity(d, u) => out.push_str(&format_quantity(d, u)),
            Obj::List(items) => {
                out.push('[');
                join(&items.borrow(), out);
                out.push(']');
            }
            Obj::Lazy(_) => {
                let items = crate::pipe::collect_quiet(v);
                out.push('[');
                join(&items, out);
                out.push(']');
            }
            Obj::Map(m) => {
                out.push('{');
                for (i, (k, val)) in m.borrow().entries.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    repr_into(k.v(), out);
                    out.push_str(": ");
                    repr_into(val.v(), out);
                }
                out.push('}');
            }
            Obj::Set(s) => {
                out.push_str("set(");
                join(&s.borrow().items, out);
                out.push(')');
            }
            Obj::Range(a, b) => out.push_str(&format!("{a}..{b}")),
            Obj::Grouped(_) => out.push_str("Grouped"),
            Obj::Record(r) => {
                out.push_str(&r.ty.name);
                out.push('(');
                let names: Vec<String> = r.ty.fields.iter().map(|f| f.name.clone()).collect();
                fields_into(&names, &r.fields.borrow(), out);
                out.push(')');
            }
            Obj::VariantValue(vv) => {
                out.push_str(&vv.variant.name);
                out.push('(');
                let names: Vec<String> = vv.variant.params.iter().map(|f| f.name.clone()).collect();
                fields_into(&names, &vv.fields, out);
                out.push(')');
            }
            Obj::Variant(var) => out.push_str(&var.name),
            Obj::Error(e) => {
                out.push_str(&e.kind);
                out.push('(');
                out.push_str(&quote(&e.message));
                out.push(')');
            }
            Obj::Func(f) => {
                out.push_str("fn ");
                out.push_str(f.info.name());
            }
            Obj::Bound(_, m) => repr_into(m.v(), out),
            Obj::BuiltinMethod(_, name) => {
                out.push_str("fn ");
                out.push_str(name);
            }
            Obj::Builtin(id) => {
                out.push_str("fn ");
                out.push_str(crate::stdlib::builtin_name(*id));
            }
            Obj::Type(t) => out.push_str(&type_ref_name(t)),
            Obj::Currency(c) => out.push_str(c),
            Obj::Symbol(s) => out.push_str(s),
            Obj::Namespace(n) => out.push_str(n),
            Obj::Channel(_) => out.push_str("channel"),
            Obj::ChannelType => out.push_str("channel"),
        },
        _ => out.push_str("none"),
    }
}

pub fn type_ref_name(t: &TypeRef) -> String {
    match t {
        TypeRef::Builtin(n) => n.to_string(),
        TypeRef::Record(r) => r.name.clone(),
        TypeRef::Enum(e) => e.name.clone(),
        TypeRef::Alias(a) => a.name.clone(),
        TypeRef::Trait(t) => t.name.clone(),
        TypeRef::ErrorKind(k) => k.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::format_float;

    #[test]
    fn floats_print_like_python() {
        assert_eq!(format_float(0.1 + 0.2), "0.30000000000000004");
        assert_eq!(format_float(4.0), "4.0");
        assert_eq!(format_float(1e16), "1e+16");
        assert_eq!(format_float(1e15), "1000000000000000.0");
        assert_eq!(format_float(1e-5), "1e-05");
        assert_eq!(format_float(0.0001), "0.0001");
        assert_eq!(format_float(1.5e-7), "1.5e-07");
        assert_eq!(format_float(-2.5), "-2.5");
        assert_eq!(format_float(12.566370614359172), "12.566370614359172");
        assert_eq!(format_float(1.2345678901234568e17), "1.2345678901234568e+17");
        assert_eq!(format_float(1e22), "1e+22");
    }
}
