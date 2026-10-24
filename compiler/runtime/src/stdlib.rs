//! Standard library available without `use` (section 15): io, data and the
//! core builtins, plus the methods of builtin values (D59).

use std::io::BufRead;

use nexo_diag::{msg, suggest, Diagnostic};

use crate::call::call_value;
use crate::coll::{hashable, new_set, str_len};
use crate::data;
use crate::dec::{exact_from_f64, Dec, Rounding};
use crate::fmt::{format_float, show, type_name};
use crate::num::{currency_places, money_of, to_decimal};
use crate::pipe::{bind, collect_vec, lazy, total};
use crate::state::{diag, failure, raise_error, raise_value, registry, write_out, R};
use crate::types::{is_value, TypeRef};
use crate::value::*;

pub const B_PRINT: BuiltinId = BuiltinId(0);
pub const B_INPUT: BuiltinId = BuiltinId(1);
pub const B_LEN: BuiltinId = BuiltinId(2);
pub const B_RANGE: BuiltinId = BuiltinId(3);
pub const B_SET: BuiltinId = BuiltinId(4);
pub const B_ROUND: BuiltinId = BuiltinId(5);
pub const B_SPLIT: BuiltinId = BuiltinId(6);
pub const B_CONVERT: BuiltinId = BuiltinId(7);
pub const B_FAIL: BuiltinId = BuiltinId(8);
pub const B_VALIDATE: BuiltinId = BuiltinId(9);
pub const B_DECIMAL: BuiltinId = BuiltinId(10);
pub const B_FLOAT: BuiltinId = BuiltinId(11);
pub const B_INT: BuiltinId = BuiltinId(12);
pub const B_STR: BuiltinId = BuiltinId(13);
pub const B_READ: BuiltinId = BuiltinId(14);
pub const B_WRITE: BuiltinId = BuiltinId(15);
pub const B_READ_LINES: BuiltinId = BuiltinId(16);
pub const B_SAVE: BuiltinId = BuiltinId(17);
pub const B_JSON_PARSE: BuiltinId = BuiltinId(18);
pub const B_CSV_PARSE: BuiltinId = BuiltinId(19);

#[derive(Clone, Copy, PartialEq)]
pub(crate) enum Kind {
    /// positional or named
    Pos,
    /// *values
    VarPos,
    /// named only
    KwOnly,
}

pub(crate) struct Param {
    name: &'static str,
    kind: Kind,
    required: bool,
}

pub(crate) const fn p(name: &'static str) -> Param {
    Param { name, kind: Kind::Pos, required: true }
}

pub(crate) const fn opt(name: &'static str) -> Param {
    Param { name, kind: Kind::Pos, required: false }
}

pub(crate) const fn var(name: &'static str) -> Param {
    Param { name, kind: Kind::VarPos, required: false }
}

pub(crate) const fn kw(name: &'static str) -> Param {
    Param { name, kind: Kind::KwOnly, required: false }
}

pub(crate) struct Sig {
    pub(crate) name: &'static str,
    pub(crate) params: &'static [Param],
}

static SIGS: &[Sig] = &[
    Sig { name: "print", params: &[var("values"), kw("end")] },
    Sig { name: "input", params: &[opt("prompt")] },
    Sig { name: "len", params: &[p("value")] },
    Sig { name: "range", params: &[p("start"), opt("end"), opt("step")] },
    Sig { name: "set", params: &[var("items")] },
    Sig { name: "round", params: &[p("value"), opt("digits"), opt("mode")] },
    Sig { name: "split", params: &[p("amount"), p("parts")] },
    Sig { name: "convert", params: &[p("value"), p("to"), p("rate")] },
    Sig { name: "fail", params: &[p("message")] },
    Sig { name: "validate", params: &[p("value"), p("as"), opt("range")] },
    Sig { name: "decimal", params: &[p("value")] },
    Sig { name: "float", params: &[p("value")] },
    Sig { name: "int", params: &[p("value")] },
    Sig { name: "str", params: &[p("value")] },
    Sig { name: "read", params: &[p("path")] },
    Sig { name: "write", params: &[p("path"), p("content")] },
    Sig { name: "read_lines", params: &[p("path")] },
    Sig { name: "save", params: &[p("value"), p("path")] },
    Sig { name: "json.parse", params: &[p("text"), opt("as")] },
    Sig { name: "csv.parse", params: &[p("text")] },
];

pub fn builtin_name(id: BuiltinId) -> &'static str {
    if id.0 >= crate::library::BASE {
        return crate::library::name(id);
    }
    SIGS[id.0 as usize].name
}

/// Arguments after checking them against the signature: one slot per
/// parameter (MISSING when absent) and the extra positional values.
pub(crate) struct Args {
    pub(crate) slots: Vec<V>,
    pub(crate) rest: Vec<V>,
}

impl Args {
    pub(crate) fn get(&self, i: usize) -> Option<V> {
        let v = self.slots[i];
        (v.tag != T_MISSING).then_some(v)
    }
}

pub(crate) fn check_args(sig: &Sig, args: &[V], names: &[Option<&str>], span: u32) -> R<Args> {
    let fname = sig.name;
    let mut seen: Vec<&str> = Vec::new();
    for n in names.iter().flatten() {
        if seen.contains(n) {
            return Err(diag(Diagnostic::new_spanless("arg_twice").arg("param", *n), span));
        }
        seen.push(n);
    }
    let positional: Vec<V> = args.iter().zip(names).filter(|(_, n)| n.is_none()).map(|(v, _)| *v).collect();
    let named: Vec<(&str, V)> = args.iter().zip(names).filter_map(|(v, n)| n.map(|n| (n, *v))).collect();
    let pos_params: Vec<&Param> = sig.params.iter().filter(|p| p.kind == Kind::Pos).collect();
    let has_var = sig.params.iter().any(|p| p.kind == Kind::VarPos);
    let named_ok = |n: &str| sig.params.iter().any(|p| p.kind != Kind::VarPos && p.name == n);
    let mut slots = vec![MISSING; sig.params.len()];
    let mut rest = Vec::new();
    let mut ok = true;
    // positional arguments
    let mut pi = 0;
    for v in &positional {
        while pi < sig.params.len() && sig.params[pi].kind == Kind::KwOnly {
            pi += 1;
        }
        if pi >= sig.params.len() {
            ok = false;
            break;
        }
        if sig.params[pi].kind == Kind::VarPos {
            rest.push(*v);
            continue;
        }
        slots[pi] = *v;
        pi += 1;
    }
    for (n, v) in &named {
        match sig.params.iter().position(|p| p.kind != Kind::VarPos && p.name == *n) {
            Some(i) if slots[i].tag == T_MISSING => slots[i] = *v,
            _ => ok = false,
        }
    }
    for (i, p) in sig.params.iter().enumerate() {
        if p.required && slots[i].tag == T_MISSING {
            ok = false;
        }
    }
    if ok {
        return Ok(Args { slots, rest });
    }
    // the same order of checks as the phase 0 interpreter
    for (n, _) in &named {
        if !named_ok(n) {
            let shown: Vec<&str> = sig.params.iter().filter(|p| p.kind != Kind::VarPos).map(|p| p.name).collect();
            let d = match suggest(n, shown.iter().copied()) {
                Some(s) => Diagnostic::new_spanless("arg_unknown")
                    .fix("suggest")
                    .arg("fn", fname)
                    .arg("param", *n)
                    .arg("suggestion", s),
                None => Diagnostic::new_spanless("arg_unknown").arg("fn", fname).arg("param", *n).arg(
                    "params",
                    if shown.is_empty() { "-".to_string() } else { shown.join(", ") },
                ),
            };
            return Err(diag(d, span));
        }
    }
    if !has_var && positional.len() > pos_params.len() {
        return Err(diag(
            Diagnostic::new_spanless("arg_too_many")
                .arg("fn", fname)
                .arg("n", pos_params.len().to_string())
                .arg("m", positional.len().to_string()),
            span,
        ));
    }
    for (i, p) in pos_params.iter().enumerate() {
        if p.required && i >= positional.len() && !named.iter().any(|(n, _)| *n == p.name) {
            return Err(diag(Diagnostic::new_spanless("arg_missing").arg("param", p.name).arg("fn", fname), span));
        }
    }
    Err(diag(
        Diagnostic::new_spanless("arg_too_many")
            .arg("fn", fname)
            .arg("n", sig.params.len().to_string())
            .arg("m", (positional.len() + named.len()).to_string()),
        span,
    ))
}

pub(crate) fn bad_arg(fname: &str, detail: String, span: u32) -> crate::state::Fail {
    diag(Diagnostic::new_spanless("bad_argument").arg("fn", fname).arg("detail", detail), span)
}

pub fn expects(fname: &str, what: &str, v: V, span: u32) -> crate::state::Fail {
    bad_arg(fname, msg("detail.expects", &[("what", what.to_string()), ("vtype", type_name(v))]), span)
}

pub(crate) fn str_arg(fname: &str, v: V, span: u32) -> R<&'static str> {
    v.as_str().ok_or_else(|| expects(fname, "str", v, span))
}

/// Call builtin `id` with arguments checked against its signature.
pub fn call(id: BuiltinId, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    if id.0 >= crate::library::BASE {
        return crate::library::call(id, args, names, span);
    }
    let sig = &SIGS[id.0 as usize];
    let a = check_args(sig, args, names, span)?;
    match id {
        B_PRINT => {
            let mut text = String::new();
            for (i, v) in a.rest.iter().enumerate() {
                if i > 0 {
                    text.push(' ');
                }
                text.push_str(&crate::fmt::show_checked(bind(*v)?.v(), span)?);
            }
            match a.get(1) {
                Some(end) => text.push_str(&crate::fmt::show_checked(end, span)?),
                None => text.push('\n'),
            }
            write_out(&text);
            Ok(Val::none())
        }
        B_INPUT => {
            if let Some(prompt) = a.get(0) {
                write_out(&show(prompt));
            }
            crate::state::flush();
            let line = crate::repl::input_line(|| {
                let mut line = String::new();
                let _ = std::io::stdin().lock().read_line(&mut line);
                while line.ends_with('\n') || line.ends_with('\r') {
                    line.pop();
                }
                line
            });
            Ok(Val::str(line))
        }
        B_LEN => {
            let owned = bind(a.get(0).unwrap())?;
            let v = owned.v();
            let n = match v.get() {
                Some(Obj::Str(s)) => str_len(s),
                Some(Obj::List(items)) => items.borrow().len(),
                Some(Obj::Map(m)) => m.borrow().entries.len(),
                Some(Obj::Set(s)) => s.borrow().items.len(),
                Some(Obj::Range(x, y)) => (y - x + 1).max(0) as usize,
                _ => return Err(expects("len", "str, list, map, set", v, span)),
            };
            Ok(Val::int(n as i64))
        }
        B_RANGE => {
            let start = a.get(0).unwrap();
            let end = a.get(1).filter(|v| !v.is_none());
            let step = a.get(2).unwrap_or(V::int(1));
            for v in [Some(start), end, Some(step)].into_iter().flatten() {
                if v.tag != T_INT {
                    return Err(expects("range", "int", v, span));
                }
            }
            let (s, e) = match end {
                Some(e) => (start.bits as i64, e.bits as i64),
                None => (0, start.bits as i64),
            };
            let st_ = step.bits as i64;
            if st_ == 0 {
                return Err(bad_arg("range", msg("detail.step_zero", &[]), span));
            }
            Ok(lazy(move || {
                let mut cur = s;
                Ok(Box::new(std::iter::from_fn(move || {
                    if (st_ > 0 && cur < e) || (st_ < 0 && cur > e) {
                        let v = cur;
                        cur = cur.saturating_add(st_);
                        Some(Ok(Val::int(v)))
                    } else {
                        None
                    }
                })))
            }))
        }
        B_SET => new_set(&a.rest, span),
        B_ROUND => round(a.get(0).unwrap(), a.get(1).unwrap_or(V::int(0)), a.get(2), span),
        B_SPLIT => split(a.get(0).unwrap(), a.get(1).unwrap(), span),
        B_CONVERT => {
            let value = a.get(0).unwrap();
            let Some(Obj::Money(amount, _)) = value.get() else {
                return Err(expects("convert", "money", value, span));
            };
            let to = a.get(1).unwrap();
            let Some(Obj::Currency(code)) = to.get() else {
                return Err(expects("convert", "currency", to, span));
            };
            let rate = a.get(2).unwrap();
            let Some(factor) = to_decimal(rate) else {
                return Err(expects("convert", "decimal", rate, span));
            };
            money_of(amount.mul(&factor), code, span)
        }
        B_FAIL => {
            let m = a.get(0).unwrap();
            if let Some(Obj::Error(_)) = m.get() {
                return Err(raise_value(Val::from(m)));
            }
            let text = crate::fmt::show_checked(bind(m)?.v(), span)?;
            Err(raise_error("Error", text, Val::none(), span))
        }
        B_VALIDATE => validate(a.get(0).unwrap(), a.get(1).unwrap(), a.get(2), span),
        B_DECIMAL => conv_decimal(a.get(0).unwrap(), span),
        B_FLOAT => conv_float(a.get(0).unwrap(), span),
        B_INT => conv_int(a.get(0).unwrap(), span),
        B_STR => Ok(Val::str(crate::fmt::show_checked(bind(a.get(0).unwrap())?.v(), span)?)),
        B_READ => {
            let path = str_arg("read", a.get(0).unwrap(), span)?;
            let text = data::read_text(path, span)?;
            if path.to_lowercase().ends_with(".csv") {
                return Ok(data::parse_csv(&text));
            }
            Ok(Val::str(text))
        }
        B_WRITE => {
            let path = str_arg("write", a.get(0).unwrap(), span)?;
            let content = crate::fmt::show_checked(bind(a.get(1).unwrap())?.v(), span)?;
            data::write_text(path, &content, span)?;
            Ok(Val::none())
        }
        B_READ_LINES => {
            let path = str_arg("read_lines", a.get(0).unwrap(), span)?;
            data::read_lines(path, span)
        }
        B_SAVE => {
            let value = bind(a.get(0).unwrap())?;
            let path = str_arg("save", a.get(1).unwrap(), span)?;
            crate::fmt::show_checked(value.v(), span)?;
            data::save(value.v(), path, span)?;
            Ok(Val::none())
        }
        B_JSON_PARSE => {
            let text = a.get(0).unwrap();
            let Some(t) = text.as_str() else { return Err(expects("json.parse", "str", text, span)) };
            data::json_parse(t, a.get(1), span)
        }
        B_CSV_PARSE => {
            let text = a.get(0).unwrap();
            let Some(t) = text.as_str() else { return Err(expects("csv.parse", "str", text, span)) };
            Ok(data::parse_csv(t))
        }
        _ => unreachable!(),
    }
}

fn round(value: V, digits: V, mode: Option<V>, span: u32) -> R<Val> {
    let rounding = match mode.map(|m| m.get()) {
        None => Rounding::HalfEven,
        Some(Some(Obj::Symbol("half_even"))) => Rounding::HalfEven,
        Some(Some(Obj::Symbol("half_up"))) => Rounding::HalfUp,
        _ => return Err(bad_arg("round", msg("detail.mode", &[]), span)),
    };
    let Some(n) = digits.as_int() else {
        return Err(expects("round", "int", digits, span));
    };
    match value.tag {
        T_BOOL => Err(expects("round", "int, float, decimal, money", value, span)),
        T_INT => Ok(Val::simple(value)),
        T_FLOAT | T_LITFLOAT => {
            let f = value.as_float().unwrap();
            if !f.is_finite() {
                return Ok(Val::float(f));
            }
            let d = if rounding == Rounding::HalfUp {
                Dec::parse(&format_float(f)).unwrap()
            } else {
                exact_from_f64(f).unwrap()
            };
            match d.quantize(-n, rounding) {
                Some(q) => Ok(Val::float(q.to_f64())),
                None => Ok(Val::float(f)),
            }
        }
        _ => match value.get() {
            Some(Obj::Decimal(d)) => match d.quantize(-n, rounding) {
                Some(q) => Ok(decimal(q)),
                None => Err(failure("InvalidValue", span, &[("detail", d.py_str())])),
            },
            Some(Obj::Money(d, c)) => match d.quantize(-n, rounding) {
                Some(q) => money_of(q, c, span),
                None => Err(failure("InvalidValue", span, &[("detail", d.py_str())])),
            },
            _ => Err(expects("round", "int, float, decimal, money", value, span)),
        },
    }
}

/// Split money without losing cents: the remainder goes to the first parts.
fn split(amount: V, parts: V, span: u32) -> R<Val> {
    let Some(Obj::Money(d, cur)) = amount.get() else {
        return Err(expects("split", "money", amount, span));
    };
    let Some(n) = parts.as_int() else {
        return Err(expects("split", "int", parts, span));
    };
    if n <= 0 {
        return Err(bad_arg("split", msg("detail.positive", &[]), span));
    }
    let places = currency_places(cur);
    let mut scaled = d.clone();
    scaled.exp += places;
    let units = scaled.to_i64().unwrap_or(0) as i128;
    let n128 = n as i128;
    let base = units.div_euclid(n128);
    let extra = units.rem_euclid(n128);
    let mut out = Vec::with_capacity(n as usize);
    for i in 0..n128 {
        let share = base + if i < extra { 1 } else { 0 };
        let mut sd = Dec::from_i64(share as i64);
        sd.exp -= places;
        out.push(money_of(sd, cur, span)?);
    }
    Ok(list(out))
}

fn conv_decimal(v: V, span: u32) -> R<Val> {
    if let Some(Obj::Money(d, _)) = v.get() {
        return Ok(decimal(d.clone()));
    }
    if v.as_str().is_some() {
        return Err(bad_arg("decimal", msg("detail.use_validate", &[("type", "decimal".into())]), span));
    }
    match to_decimal(v) {
        Some(d) => Ok(decimal(d)),
        None => Err(expects("decimal", "int, float", v, span)),
    }
}

fn conv_float(v: V, span: u32) -> R<Val> {
    if v.as_str().is_some() {
        return Err(bad_arg("float", msg("detail.use_validate", &[("type", "float".into())]), span));
    }
    match v.tag {
        T_INT => Ok(Val::float(v.bits as i64 as f64)),
        T_FLOAT | T_LITFLOAT => Ok(Val::simple(v.plain())),
        _ => match v.get() {
            Some(Obj::Decimal(d)) | Some(Obj::Money(d, _)) => Ok(Val::float(d.to_f64())),
            _ => Err(expects("float", "int, decimal", v, span)),
        },
    }
}

fn conv_int(v: V, span: u32) -> R<Val> {
    if v.as_str().is_some() {
        return Err(bad_arg("int", msg("detail.use_validate", &[("type", "int".into())]), span));
    }
    match v.tag {
        T_INT => Ok(Val::simple(v)),
        T_FLOAT | T_LITFLOAT => {
            let f = v.as_float().unwrap();
            if !f.is_finite() {
                return Err(failure("InvalidValue", span, &[("detail", format_float(f))]));
            }
            let t = f.trunc();
            if t < -9.223372036854776e18 || t >= 9.223372036854776e18 {
                return Err(failure("Overflow", span, &[]));
            }
            Ok(Val::int(t as i64))
        }
        _ => match v.get() {
            Some(Obj::Decimal(d)) => d.to_i64().map(Val::int).ok_or_else(|| failure("Overflow", span, &[])),
            _ => Err(expects("int", "float, decimal", v, span)),
        },
    }
}

/// An address with one @, a name before it and a domain with a dot after it.
fn is_email(t: &str) -> bool {
    let Some((user, domain)) = t.split_once('@') else { return false };
    let bad = |c: char| c == '@' || c.is_whitespace();
    !user.is_empty()
        && !user.chars().any(bad)
        && !domain.chars().any(bad)
        && domain.contains('.')
        && domain.split('.').all(|p| !p.is_empty())
}

fn is_int_text(t: &str) -> bool {
    let t = t.strip_prefix(['+', '-']).unwrap_or(t);
    !t.is_empty() && t.chars().all(|c| c.is_ascii_digit())
}

pub fn is_float_text(t: &str, allow_plus: bool) -> bool {
    let t = match t.strip_prefix('-') {
        Some(r) => r,
        None if allow_plus => t.strip_prefix('+').unwrap_or(t),
        None => t,
    };
    let (mant, exp) = match t.find(['e', 'E']) {
        Some(i) => (&t[..i], Some(&t[i + 1..])),
        None => (t, None),
    };
    if let Some(e) = exp {
        let e = e.strip_prefix(['+', '-']).unwrap_or(e);
        if e.is_empty() || !e.chars().all(|c| c.is_ascii_digit()) {
            return false;
        }
    }
    let (a, b) = match mant.find('.') {
        Some(i) => (&mant[..i], Some(&mant[i + 1..])),
        None => (mant, None),
    };
    let digits = |s: &str| s.chars().all(|c| c.is_ascii_digit());
    match b {
        None => !a.is_empty() && digits(a),
        Some(b) => digits(a) && digits(b) && (!a.is_empty() || !b.is_empty()),
    }
}

/// `validate(value, as: type, range: a..b)?` turns external text into a
/// checked value (section 9).
fn validate(value: V, target: V, range: Option<V>, span: u32) -> R<Val> {
    let owned = bind(value)?;
    let value = owned.v();
    let tname = match target.get() {
        Some(Obj::Type(t)) => crate::fmt::type_ref_name(t),
        Some(Obj::Variant(v)) => v.name.clone(),
        Some(Obj::Func(f)) => f.info.name().to_string(),
        Some(Obj::Builtin(id)) => builtin_name(*id).to_string(),
        Some(Obj::Symbol(s)) => s.to_string(),
        _ => type_name(target),
    };
    let invalid = || {
        failure("InvalidValue", span, &[("detail", msg("detail.not_a", &[("value", show(value)), ("type", tname.clone())]))])
    };
    let mut result = owned.clone();
    match (target.get(), value.as_str()) {
        (Some(Obj::Type(TypeRef::Builtin(b))), Some(text)) => {
            let text = text.trim();
            result = match *b {
                "int" => {
                    if !is_int_text(text) {
                        return Err(invalid());
                    }
                    match text.parse::<i64>() {
                        Ok(i) => Val::int(i),
                        Err(_) => return Err(failure("Overflow", span, &[])),
                    }
                }
                "float" => {
                    if !is_float_text(text, true) {
                        return Err(invalid());
                    }
                    Val::float(text.parse::<f64>().unwrap_or(f64::NAN))
                }
                "decimal" => {
                    if !is_float_text(text, true) {
                        return Err(invalid());
                    }
                    decimal(Dec::parse(text).unwrap())
                }
                "bool" => match text {
                    "true" => Val::bool(true),
                    "false" => Val::bool(false),
                    _ => return Err(invalid()),
                },
                "str" => owned.clone(),
                "email" => {
                    if !is_email(text) {
                        return Err(invalid());
                    }
                    Val::str(text)
                }
                "date" => crate::time::parse_date(text).ok_or_else(invalid)?,
                "time" => crate::time::parse_time(text).ok_or_else(invalid)?,
                _ => return Err(invalid()),
            };
        }
        (Some(Obj::Type(_) | Obj::Variant(_) | Obj::Func(_) | Obj::Builtin(_) | Obj::Symbol(_)), _) => {
            if !is_value(value, target, span)? {
                return Err(invalid());
            }
        }
        _ => return Err(expects("validate", "type", target, span)),
    }
    if let Some(r) = range.filter(|r| !r.is_none()) {
        let Some(Obj::Range(lo, hi)) = r.get() else {
            return Err(expects("validate", "range", r, span));
        };
        if !crate::coll::in_range(result.v(), *lo, *hi) {
            let detail = msg("detail.out_of_range", &[("value", show(result.v())), ("range", format!("{lo}..{hi}"))]);
            return Err(failure("InvalidValue", span, &[("detail", detail)]));
        }
    }
    Ok(result)
}

// ------------------------------------------------------ methods of values

fn table(v: V) -> Option<(&'static str, &'static [&'static str])> {
    Some(match v.get()? {
        Obj::List(_) | Obj::Lazy(_) => ("list", &["sum", "count", "add"]),
        Obj::Set(_) => ("set", &["add", "count"]),
        Obj::Map(_) => ("map", &["keys", "values", "count"]),
        Obj::Str(_) => ("str", &["upper", "lower", "trim", "split", "replace", "starts_with", "ends_with", "find"]),
        Obj::Channel(_) => ("channel", &["send", "receive", "close"]),
        _ => return None,
    })
}

pub fn missing_member(vtype: &str, name: &str, candidates: &[&str], span: u32) -> crate::state::Fail {
    missing(vtype, name, candidates, span)
}

fn missing(vtype: &str, name: &str, candidates: &[&str], span: u32) -> crate::state::Fail {
    let d = match suggest(name, candidates.iter().copied()) {
        Some(s) => {
            Diagnostic::new_spanless("no_member").fix("suggest").arg("vtype", vtype).arg("name", name).arg("suggestion", s)
        }
        None => Diagnostic::new_spanless("no_member").arg("vtype", vtype).arg("name", name),
    };
    diag(d, span)
}

/// Members of builtin values: `list.sum`, `text.upper`, `json.parse`...
/// The name of a method of a builtin type, or the error of `member`.
pub fn method_name(obj: V, name: &str, span: u32) -> R<&'static str> {
    if let Some(q) = crate::db::query_of(obj) {
        crate::db::query_member(&q, obj, name, span)?;
        return Ok(crate::num::intern(name));
    }
    match table(obj) {
        Some((_, names)) => match names.iter().find(|n| **n == name) {
            Some(n) => Ok(n),
            None => Err(missing(&type_name(obj), name, names, span)),
        },
        None => Err(missing(&type_name(obj), name, &[], span)),
    }
}

pub fn member(obj: V, name: &str, span: u32) -> R<Val> {
    if let Some(Obj::Native(n)) = obj.get() {
        return n.member(obj, name, span);
    }
    if let Some(q) = crate::db::query_of(obj) {
        return crate::db::query_member(&q, obj, name, span);
    }
    if let Some(Obj::Namespace(ns)) = obj.get() {
        if let Some(v) = crate::library::member(ns, name, span) {
            return v;
        }
        return match (*ns, name) {
            ("json", "parse") => Ok(Val::new(Obj::Builtin(B_JSON_PARSE))),
            ("csv", "parse") => Ok(Val::new(Obj::Builtin(B_CSV_PARSE))),
            _ => Err(missing(ns, name, &["parse"], span)),
        };
    }
    match table(obj) {
        Some((_, names)) if names.contains(&name) => {
            Ok(Val::new(Obj::BuiltinMethod(Val::from(obj), crate::num::intern(name))))
        }
        Some((_, names)) => Err(missing(&type_name(obj), name, names, span)),
        None => Err(missing(&type_name(obj), name, &[], span)),
    }
}

/// Arguments of a library function or method checked against its
/// parameters (the first `required` must be given): one slot per
/// parameter, MISSING when absent. The errors come in the order phase 0
/// checks them.
pub fn method_args(fname: &str, params: &[&str], required: usize, args: &[V], names: &[Option<&str>], span: u32) -> R<Vec<V>> {
    let mut slots = vec![MISSING; params.len()];
    let mut seen: Vec<&str> = Vec::new();
    for n in names.iter().flatten() {
        if seen.contains(n) {
            return Err(diag(Diagnostic::new_spanless("arg_twice").arg("param", *n), span));
        }
        seen.push(n);
    }
    for n in names.iter().flatten() {
        if !params.contains(n) {
            let d = match suggest(n, params.iter().copied()) {
                Some(s) => Diagnostic::new_spanless("arg_unknown")
                    .fix("suggest")
                    .arg("fn", fname)
                    .arg("param", *n)
                    .arg("suggestion", s),
                None => Diagnostic::new_spanless("arg_unknown").arg("fn", fname).arg("param", *n).arg(
                    "params",
                    if params.is_empty() { "-".to_string() } else { params.join(", ") },
                ),
            };
            return Err(diag(d, span));
        }
    }
    let positional: Vec<V> = args.iter().zip(names).filter(|(_, n)| n.is_none()).map(|(v, _)| *v).collect();
    if positional.len() > params.len() {
        return Err(diag(
            Diagnostic::new_spanless("arg_too_many")
                .arg("fn", fname)
                .arg("n", params.len().to_string())
                .arg("m", positional.len().to_string()),
            span,
        ));
    }
    slots[..positional.len()].copy_from_slice(&positional);
    for (v, n) in args.iter().zip(names) {
        if let Some(n) = n {
            let i = params.iter().position(|p| p == n).unwrap();
            if slots[i].tag != T_MISSING {
                return Err(diag(Diagnostic::new_spanless("arg_twice").arg("param", *n), span));
            }
            slots[i] = *v;
        }
    }
    for (i, p) in params.iter().enumerate().take(required) {
        if slots[i].tag == T_MISSING {
            return Err(diag(Diagnostic::new_spanless("arg_missing").arg("param", *p).arg("fn", fname), span));
        }
    }
    Ok(slots)
}

fn arity(name: &str, args: &[V], names: &[Option<&str>], n: usize, span: u32) -> R<()> {
    if let Some(Some(k)) = names.iter().find(|n| n.is_some()) {
        return Err(diag(
            Diagnostic::new_spanless("arg_unknown").arg("fn", name).arg("param", *k).arg("params", "-"),
            span,
        ));
    }
    if args.len() > n {
        return Err(diag(
            Diagnostic::new_spanless("arg_too_many")
                .arg("fn", name)
                .arg("n", n.to_string())
                .arg("m", args.len().to_string()),
            span,
        ));
    }
    Ok(())
}

pub fn call_method(obj: V, name: &'static str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    if let Some(Obj::Native(n)) = obj.get() {
        return n.call_method(obj, name, args, names, span);
    }
    if let Some(q) = crate::db::query_of(obj) {
        return crate::db::query_method(&q, name, args, names, span);
    }
    if let Some(Obj::Channel(c)) = obj.get() {
        return crate::sync::call_method(c, obj, name, args, names, span);
    }
    let max = match name {
        "add" | "starts_with" | "ends_with" | "split" | "find" => 1,
        "replace" => 2,
        _ => 0,
    };
    arity(name, args, names, max, span)?;
    let need = match name {
        "add" | "starts_with" | "ends_with" | "find" => 1,
        "replace" => 2,
        _ => 0,
    };
    if args.len() < need {
        let param = if name == "add" { "item" } else { "text" };
        return Err(diag(Diagnostic::new_spanless("arg_missing").arg("param", param).arg("fn", name), span));
    }
    match (obj.get().unwrap(), name) {
        (Obj::List(_) | Obj::Lazy(_), "sum") => {
            let items = collect_vec(obj, span)?;
            total(&items, span)
        }
        (Obj::List(_) | Obj::Lazy(_) | Obj::Set(_), "count") => Ok(Val::int(collect_vec(obj, span)?.len() as i64)),
        (Obj::Map(m), "count") => Ok(Val::int(m.borrow().entries.len() as i64)),
        (Obj::Lazy(_), "add") => Err(expects("add", "list", obj, span)),
        // the compiled code makes the collection unique before changing it
        // through its place; a value seen by other owners (a temporary
        // copy) is not changed
        (Obj::List(items), "add") => {
            let v = crate::coll::stored_in(obj, bind(args[0])?);
            if owners(obj) <= 1 {
                items.borrow_mut().push(v);
            }
            Ok(Val::none())
        }
        (Obj::Set(s), "add") => {
            let k = hashable(args[0], span)?;
            if owners(obj) <= 1 {
                s.borrow_mut().insert(k, Val::from(args[0]));
            }
            Ok(Val::none())
        }
        (Obj::Map(m), "keys") => Ok(list(m.borrow().entries.iter().map(|(k, _)| k.clone()).collect())),
        (Obj::Map(m), "values") => Ok(list(m.borrow().entries.iter().map(|(_, v)| v.clone()).collect())),
        (Obj::Str(s), "upper") => Ok(Val::str(s.to_uppercase())),
        (Obj::Str(s), "lower") => Ok(Val::str(s.to_lowercase())),
        (Obj::Str(s), "trim") => Ok(Val::str(s.trim())),
        (Obj::Str(s), "split") => {
            let parts: Vec<Val> = match args.first() {
                None => s.split_whitespace().map(Val::str).collect(),
                Some(sep) => match sep.as_str() {
                    Some(sep) if !sep.is_empty() => s.split(sep).map(Val::str).collect(),
                    _ => return Err(expects("split", "str", *sep, span)),
                },
            };
            Ok(list(parts))
        }
        (Obj::Str(s), "replace") => {
            let a = str_arg("replace", args[0], span)?;
            let b = str_arg("replace", args[1], span)?;
            Ok(Val::str(s.replace(a, b)))
        }
        (Obj::Str(s), "starts_with") => Ok(Val::bool(s.starts_with(str_arg("starts_with", args[0], span)?))),
        (Obj::Str(s), "ends_with") => Ok(Val::bool(s.ends_with(str_arg("ends_with", args[0], span)?))),
        (Obj::Str(s), "find") => {
            let sub = str_arg("find", args[0], span)?;
            Ok(match s.find(sub) {
                Some(i) => Val::int(s[..i].chars().count() as i64),
                None => Val::none(),
            })
        }
        _ => Err(missing(&type_name(obj), name, &[], span)),
    }
}

/// Builtin names (types, error kinds, currencies, constants) as values.
pub fn named_value(name: &str) -> Option<Val> {
    let v = match name {
        "int" | "float" | "decimal" | "money" | "str" | "bool" | "list" | "map" => {
            Obj::Type(TypeRef::Builtin(crate::num::intern(name)))
        }
        "date" | "time" | "email" => Obj::Type(TypeRef::Builtin(crate::num::intern(name))),
        "half_up" | "half_even" => Obj::Symbol(crate::num::intern(name)),
        "json" | "csv" => Obj::Namespace(crate::num::intern(name)),
        "channel" => Obj::ChannelType(None),
        _ if crate::types::ERROR_KINDS.contains(&name) => Obj::Type(TypeRef::ErrorKind(crate::num::intern(name))),
        _ if crate::num::is_currency(name) => Obj::Currency(crate::num::intern(name)),
        _ => return crate::library::value(name),
    };
    Some(Val::new(v))
}

pub fn emit(name: &str, args: &[V], span: u32) -> R<()> {
    let handlers = registry().read().unwrap().handlers.get(name).cloned().unwrap_or_default();
    let mut owned = Vec::with_capacity(args.len());
    for a in args {
        owned.push(bind(*a)?);
    }
    let bound = raw_all(&owned);
    for h in handlers {
        let Some(Obj::Func(f)) = h.get() else { continue };
        let n = f.info.params().len();
        if n != bound.len() {
            return Err(diag(
                Diagnostic::new_spanless("arg_too_many")
                    .arg("fn", name)
                    .arg("n", n.to_string())
                    .arg("m", bound.len().to_string()),
                span,
            ));
        }
        call_value(h.v(), &bound, span)?;
    }
    Ok(())
}

pub fn on(name: &str, handler: V) {
    registry().write().unwrap().handlers.entry(name.to_string()).or_default().push(Val::from(handler));
}
