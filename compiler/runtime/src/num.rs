//! Arithmetic and comparison rules (section 17).
//!
//! 1. A number with a decimal point is a float unless the other operand is
//!    decimal or money (then the literal is read exactly as a decimal).
//! 2. Mixing float and decimal is an error; convert with decimal(x) or float(x).
//! 3. Adding money in different currencies is an error.
//! 4. `/` yields float between integers and decimal between decimals; `//` is
//!    integer division.
//!
//! int is 64-bit and overflow is an error; it never wraps.

use std::cmp::Ordering;

use nexo_diag::Diagnostic;
use num_bigint::BigInt;
use num_traits::ToPrimitive;

use crate::dec::{Dec, Rounding};
use crate::fmt::{format_float, type_name};
use crate::state::{diag, failure, R};
use crate::value::*;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Op {
    Add,
    Sub,
    Mul,
    Div,
    IDiv,
    Mod,
    Pow,
}

impl Op {
    pub fn from_code(c: u32) -> Op {
        match c {
            0 => Op::Add,
            1 => Op::Sub,
            2 => Op::Mul,
            3 => Op::Div,
            4 => Op::IDiv,
            5 => Op::Mod,
            _ => Op::Pow,
        }
    }

    pub fn text(self) -> &'static str {
        match self {
            Op::Add => "+",
            Op::Sub => "-",
            Op::Mul => "*",
            Op::Div => "/",
            Op::IDiv => "//",
            Op::Mod => "%",
            Op::Pow => "**",
        }
    }
}

// ----------------------------------------------------------- unit literals

pub fn currency_places(code: &str) -> i64 {
    match code {
        "JPY" | "CLP" => 0,
        _ => 2,
    }
}

pub fn intern(s: &str) -> &'static str {
    use std::collections::HashSet;
    use std::sync::Mutex;
    static NAMES: Mutex<Option<HashSet<&'static str>>> = Mutex::new(None);
    let mut guard = NAMES.lock().unwrap();
    let n = guard.get_or_insert_with(HashSet::new);
    if let Some(s) = n.get(s) {
        return s;
    }
    let leaked: &'static str = Box::leak(s.to_string().into_boxed_str());
    n.insert(leaked);
    leaked
}

pub fn is_currency(code: &str) -> bool {
    matches!(
        code,
        "EUR" | "USD" | "GBP" | "JPY" | "CHF" | "CAD" | "AUD" | "CNY" | "MXN" | "ARS" | "BRL" | "CLP" | "COP"
            | "PEN" | "UYU" | "SEK" | "NOK" | "DKK" | "PLN" | "INR"
    )
}

/// Money keeps the minor units of its currency (D37).
pub fn money_of(amount: Dec, currency: &'static str, span: u32) -> R<Val> {
    match amount.quantize(-currency_places(currency), Rounding::HalfEven) {
        Some(d) => Ok(Val::new(Obj::Money(d, currency))),
        None => Err(failure("InvalidValue", span, &[("detail", amount.py_str())])),
    }
}

pub fn unit_literal(text: &str, unit: &str) -> Val {
    let d = Dec::parse(text).unwrap();
    let unit = intern(unit);
    if is_currency(unit) {
        let q = d.quantize(-currency_places(unit), Rounding::HalfEven).unwrap_or(d);
        Val::new(Obj::Money(q, unit))
    } else {
        Val::new(Obj::Quantity(d, unit))
    }
}

/// Dimension and factor to the base unit (seconds, bytes, km, percent).
pub fn unit_dimension(unit: &str) -> (&'static str, Dec) {
    let (dim, f) = match unit {
        "ms" => ("time", "0.001"),
        "s" => ("time", "1"),
        "m" => ("time", "60"),
        "h" => ("time", "3600"),
        "day" | "days" => ("time", "86400"),
        "year" | "years" => ("time", "31536000"),
        "B" => ("size", "1"),
        "KB" => ("size", "1000"),
        "MB" => ("size", "1000000"),
        "GB" => ("size", "1000000000"),
        "TB" => ("size", "1000000000000"),
        "km" => ("length", "1"),
        "%" => ("percent", "1"),
        _ => return (intern(unit), Dec::from_i64(1)),
    };
    (dim, Dec::parse(f).unwrap())
}

fn qty_base(value: &Dec, unit: &str) -> Dec {
    value.mul(&unit_dimension(unit).1)
}

fn fraction(value: &Dec) -> Dec {
    value.div(&Dec::from_i64(100))
}

/// Quantities are shown without needless trailing zeros.
pub fn normalize(d: Dec) -> Dec {
    if d.is_integral() {
        return d.quantize(0, Rounding::HalfEven).unwrap_or(d);
    }
    d.normalize()
}

// ------------------------------------------------------------- decimals

/// An exact decimal from an int, a float or a float literal.
pub fn to_decimal(v: V) -> Option<Dec> {
    match v.tag {
        T_LITFLOAT => Dec::parse(v.litfloat().text()),
        T_INT => Some(Dec::from_i64(v.bits as i64)),
        T_FLOAT => {
            let f = f64::from_bits(v.bits);
            if f.is_finite() {
                Dec::parse(&format_float(f))
            } else {
                None
            }
        }
        T_OBJ => match v.get() {
            Some(Obj::Decimal(d)) => Some(d.clone()),
            _ => None,
        },
        _ => None,
    }
}

fn dec_of(v: V) -> Option<&'static Dec> {
    match v.get() {
        Some(Obj::Decimal(d)) => Some(d),
        _ => None,
    }
}

fn is_exact_kind(v: V) -> bool {
    matches!(v.get(), Some(Obj::Decimal(_) | Obj::Money(..) | Obj::Quantity(..)))
}

/// Section 17, rule 1: a float literal next to a decimal, money or unit
/// quantity is read as an exact decimal.
pub fn exact_literals(a: V, b: V) -> (Val, Val) {
    let mut x = Val::from(a.plain());
    let mut y = Val::from(b.plain());
    if a.tag == T_LITFLOAT && is_exact_kind(b) {
        x = decimal(Dec::parse(a.litfloat().text()).unwrap());
    }
    if b.tag == T_LITFLOAT && is_exact_kind(a) {
        y = decimal(Dec::parse(b.litfloat().text()).unwrap());
    }
    (x, y)
}

fn is_num(v: V) -> bool {
    v.tag == T_INT || v.tag == T_FLOAT || dec_of(v).is_some()
}

fn bad(op: &str, a: V, b: V, span: u32) -> crate::state::Fail {
    diag(
        Diagnostic::new_spanless("bad_operands").arg("op", op).arg("a", type_name(a)).arg("b", type_name(b)),
        span,
    )
}

pub fn mix(a: V, b: V, span: u32) -> crate::state::Fail {
    diag(Diagnostic::new_spanless("mix_numbers").arg("a", type_name(a)).arg("b", type_name(b)), span)
}

fn overflow(span: u32) -> crate::state::Fail {
    failure("Overflow", span, &[])
}

fn div_zero(span: u32) -> crate::state::Fail {
    failure("DivisionByZero", span, &[])
}

// ------------------------------------------------------------ arithmetic

pub fn arith(op: Op, a: V, b: V, span: u32) -> R<Val> {
    let (oa, ob) = exact_literals(a, b);
    let (a, b) = (oa.v(), ob.v());
    if a.tag == T_BOOL || b.tag == T_BOOL {
        return Err(bad(op.text(), a, b, span));
    }
    if a.tag == T_INT && b.tag == T_INT {
        return int_op(op, a.bits as i64, b.bits as i64, span);
    }
    if a.tag == T_FLOAT && b.tag == T_FLOAT {
        return float_op(op, f64::from_bits(a.bits), f64::from_bits(b.bits), span);
    }
    if op == Op::Add {
        if let (Some(x), Some(y)) = (a.as_str(), b.as_str()) {
            let mut s = String::with_capacity(x.len() + y.len());
            s.push_str(x);
            s.push_str(y);
            return Ok(Val::str(s));
        }
        let fa = crate::pipe::force(a)?;
        let fb = crate::pipe::force(b)?;
        if let (Some(Obj::List(x)), Some(Obj::List(y))) = (fa.get(), fb.get()) {
            let mut items = x.borrow().clone();
            for v in y.borrow().iter() {
                items.push(Val::from(v));
            }
            return Ok(Val::new(Obj::List(Mut::new(items))));
        }
    }
    if matches!(a.get(), Some(Obj::Native(_))) || matches!(b.get(), Some(Obj::Native(_))) {
        return crate::time::arith(op, a, b, span);
    }
    if matches!(a.get(), Some(Obj::Money(..))) || matches!(b.get(), Some(Obj::Money(..))) {
        return money_op(op, a, b, span);
    }
    if matches!(a.get(), Some(Obj::Quantity(..))) || matches!(b.get(), Some(Obj::Quantity(..))) {
        return quantity_op(op, a, b, span);
    }
    if !(is_num(a) && is_num(b)) {
        return Err(bad(op.text(), a, b, span));
    }
    if dec_of(a).is_some() || dec_of(b).is_some() {
        if a.tag == T_FLOAT || b.tag == T_FLOAT {
            return Err(mix(a, b, span));
        }
        let x = to_decimal(a).unwrap();
        let y = to_decimal(b).unwrap();
        return decimal_op(op, &x, &y, span);
    }
    let x = a.as_float().unwrap_or(a.bits as i64 as f64);
    let y = b.as_float().unwrap_or(b.bits as i64 as f64);
    float_op(op, x, y, span)
}

pub fn int_op(op: Op, a: i64, b: i64, span: u32) -> R<Val> {
    let r = match op {
        Op::Add => a.checked_add(b),
        Op::Sub => a.checked_sub(b),
        Op::Mul => a.checked_mul(b),
        Op::Div => {
            if b == 0 {
                return Err(div_zero(span));
            }
            return Ok(Val::float(int_true_div(a, b)));
        }
        Op::IDiv => {
            if b == 0 {
                return Err(div_zero(span));
            }
            let (q, r) = (a.checked_div(b), a.checked_rem(b));
            match (q, r) {
                (Some(q), Some(r)) => Some(if r != 0 && ((r < 0) != (b < 0)) { q - 1 } else { q }),
                _ => None,
            }
        }
        Op::Mod => {
            if b == 0 {
                return Err(div_zero(span));
            }
            match a.checked_rem(b) {
                Some(r) => Some(if r != 0 && ((r < 0) != (b < 0)) { r + b } else { r }),
                None => Some(0),
            }
        }
        Op::Pow => {
            if b < 0 {
                if a == 0 {
                    return Err(div_zero(span));
                }
                return float_op(Op::Pow, a as f64, b as f64, span);
            }
            match a {
                0 => Some(if b == 0 { 1 } else { 0 }),
                1 => Some(1),
                -1 => Some(if b % 2 == 0 { 1 } else { -1 }),
                _ if b > 64 => None,
                _ => a.checked_pow(b as u32),
            }
        }
    };
    r.map(Val::int).ok_or_else(|| overflow(span))
}

/// Correctly rounded `a / b` for 64-bit integers.
fn int_true_div(a: i64, b: i64) -> f64 {
    const LIMIT: i64 = 1 << 53;
    if a.abs() <= LIMIT && b.abs() <= LIMIT {
        return a as f64 / b as f64;
    }
    let q = Dec::from_i64(a).div(&Dec::from_i64(b));
    q.to_f64()
}

fn py_float_mod(a: f64, b: f64) -> f64 {
    let m = a % b;
    if m != 0.0 {
        if (b < 0.0) != (m < 0.0) {
            m + b
        } else {
            m
        }
    } else {
        0.0f64.copysign(b)
    }
}

pub fn float_op(op: Op, a: f64, b: f64, span: u32) -> R<Val> {
    Ok(Val::float(match op {
        Op::Add => a + b,
        Op::Sub => a - b,
        Op::Mul => a * b,
        Op::Div | Op::IDiv | Op::Mod if b == 0.0 => return Err(div_zero(span)),
        Op::Div => a / b,
        Op::IDiv => {
            let q = (a / b).floor();
            if q.is_infinite() {
                return Err(overflow(span));
            }
            q
        }
        Op::Mod => py_float_mod(a, b),
        Op::Pow => {
            if a == 0.0 && b < 0.0 {
                return Err(div_zero(span));
            }
            if a < 0.0 && b.fract() != 0.0 && b.is_finite() {
                let detail = format!("{} ** {}", format_float(a), format_float(b));
                return Err(failure("InvalidValue", span, &[("detail", detail)]));
            }
            let r = a.powf(b);
            if r.is_infinite() && a.is_finite() && b.is_finite() {
                return Err(overflow(span));
            }
            r
        }
    }))
}

pub fn decimal_op(op: Op, a: &Dec, b: &Dec, span: u32) -> R<Val> {
    if matches!(op, Op::Div | Op::IDiv | Op::Mod) && b.is_zero() {
        return Err(div_zero(span));
    }
    Ok(decimal(match op {
        Op::Add => a.add(b),
        Op::Sub => a.sub(b),
        Op::Mul => a.mul(b),
        Op::Div => a.div(b),
        Op::IDiv => a.div(b).to_integral(Rounding::Floor),
        Op::Mod => a.sub(&b.mul(&a.div(b).to_integral(Rounding::Floor))),
        Op::Pow => {
            if b.is_integral() {
                let n = b.to_i64().unwrap_or(i64::MAX);
                if a.is_zero() && n < 0 {
                    return Err(failure("InvalidValue", span, &[("detail", format!("{} ** {}", a.py_str(), b.py_str()))]));
                }
                if n.unsigned_abs() > 10_000 {
                    return Err(overflow(span));
                }
                a.powi(n)
            } else {
                let r = a.to_f64().powf(b.to_f64());
                if !r.is_finite() {
                    return Err(failure("InvalidValue", span, &[("detail", format!("{} ** {}", a.py_str(), b.py_str()))]));
                }
                Dec::parse(&format_float(r)).unwrap().fix()
            }
        }
    }))
}

/// A plain number used to scale money: int or decimal (never float).
fn as_factor(v: V, other: V, span: u32, op: Op) -> R<Dec> {
    if let Some(i) = v.as_int() {
        return Ok(Dec::from_i64(i));
    }
    match v.get() {
        Some(Obj::Decimal(d)) => return Ok(d.clone()),
        Some(Obj::Quantity(d, "%")) => return Ok(fraction(d)),
        _ => {}
    }
    if v.tag == T_FLOAT {
        return Err(mix(other, v, span));
    }
    Err(bad(op.text(), other, v, span))
}

fn money_op(op: Op, a: V, b: V, span: u32) -> R<Val> {
    if let (Some(Obj::Money(x, cx)), Some(Obj::Money(y, cy))) = (a.get(), b.get()) {
        if cx != cy {
            if matches!(op, Op::Add | Op::Sub | Op::Div) {
                return Err(diag(Diagnostic::new_spanless("currency_mismatch").arg("a", *cx).arg("b", *cy), span));
            }
            return Err(bad(op.text(), a, b, span));
        }
        return match op {
            Op::Add => money_of(x.add(y), cx, span),
            Op::Sub => money_of(x.sub(y), cx, span),
            Op::Div => {
                if y.is_zero() {
                    return Err(div_zero(span));
                }
                Ok(decimal(x.div(y)))
            }
            _ => Err(bad(op.text(), a, b, span)),
        };
    }
    if op == Op::Mul {
        let (money, other) = if matches!(a.get(), Some(Obj::Money(..))) { (a, b) } else { (b, a) };
        let Some(Obj::Money(amount, cur)) = money.get() else { unreachable!() };
        let f = as_factor(other, money, span, op)?;
        return money_of(amount.mul(&f), cur, span);
    }
    if op == Op::Div {
        if let Some(Obj::Money(amount, cur)) = a.get() {
            let f = as_factor(b, a, span, op)?;
            if f.is_zero() {
                return Err(div_zero(span));
            }
            return money_of(amount.div(&f), cur, span);
        }
    }
    Err(bad(op.text(), a, b, span))
}

fn scalar(v: V) -> Option<Dec> {
    match v.tag {
        T_INT => Some(Dec::from_i64(v.bits as i64)),
        T_FLOAT => to_decimal(v),
        _ => dec_of(v).cloned(),
    }
}

fn quantity_op(op: Op, a: V, b: V, span: u32) -> R<Val> {
    if let (Some(Obj::Quantity(x, ux)), Some(Obj::Quantity(y, uy))) = (a.get(), b.get()) {
        let (dx, fx) = unit_dimension(ux);
        let (dy, fy) = unit_dimension(uy);
        if dx != dy {
            if matches!(op, Op::Add | Op::Sub) {
                return Err(diag(Diagnostic::new_spanless("unit_mismatch").arg("a", *ux).arg("b", *uy), span));
            }
            if op == Op::Mul && (dx == "percent" || dy == "percent") {
                let (q, qu, p) = if *uy == "%" { (x, ux, y) } else { (y, uy, x) };
                return Ok(Val::new(Obj::Quantity(q.mul(&fraction(p)), qu)));
            }
            return Err(bad(op.text(), a, b, span));
        }
        return match op {
            Op::Add | Op::Sub => {
                let (small_f, small_u) = if fx.cmp(&fy) != Ordering::Greater { (&fx, *ux) } else { (&fy, *uy) };
                let bx = x.mul(&fx);
                let by = y.mul(&fy);
                let total = if op == Op::Add { bx.add(&by) } else { bx.sub(&by) };
                Ok(Val::new(Obj::Quantity(normalize(total.div(small_f)), small_u)))
            }
            Op::Div => {
                if y.is_zero() {
                    return Err(div_zero(span));
                }
                Ok(decimal(x.mul(&fx).div(&y.mul(&fy))))
            }
            _ => Err(bad(op.text(), a, b, span)),
        };
    }
    let (q, other) = if matches!(a.get(), Some(Obj::Quantity(..))) { (a, b) } else { (b, a) };
    let Some(Obj::Quantity(qv, qu)) = q.get() else { unreachable!() };
    if *qu == "%" && op == Op::Mul {
        if other.tag == T_FLOAT {
            let frac = fraction(qv).to_f64();
            return Ok(Val::float(f64::from_bits(other.bits) * frac));
        }
        let Some(s) = scalar(other) else { return Err(bad(op.text(), a, b, span)) };
        return Ok(decimal(normalize(s.mul(&fraction(qv)))));
    }
    let Some(s) = scalar(other) else { return Err(bad(op.text(), a, b, span)) };
    if op == Op::Mul {
        return Ok(Val::new(Obj::Quantity(normalize(qv.mul(&s)), qu)));
    }
    if op == Op::Div && matches!(a.get(), Some(Obj::Quantity(..))) {
        if s.is_zero() {
            return Err(div_zero(span));
        }
        return Ok(Val::new(Obj::Quantity(normalize(qv.div(&s)), qu)));
    }
    Err(bad(op.text(), a, b, span))
}

pub fn negate(v: V, span: u32) -> R<Val> {
    match v.tag {
        T_LITFLOAT => Ok(Val::float(-v.litfloat().value)),
        T_INT => (v.bits as i64).checked_neg().map(Val::int).ok_or_else(|| overflow(span)),
        T_FLOAT => Ok(Val::float(-f64::from_bits(v.bits))),
        _ => match v.get() {
            Some(Obj::Decimal(d)) => Ok(decimal(d.neg())),
            Some(Obj::Money(d, c)) => Ok(Val::new(Obj::Money(d.neg(), c))),
            Some(Obj::Quantity(d, u)) => Ok(Val::new(Obj::Quantity(d.neg(), u))),
            _ => Err(diag(Diagnostic::new_spanless("bad_operand").arg("op", "-").arg("a", type_name(v)), span)),
        },
    }
}

// ----------------------------------------------------------- comparisons

fn num_cmp(a: V, b: V) -> Option<Ordering> {
    match (a.tag, b.tag) {
        (T_INT, T_INT) => Some((a.bits as i64).cmp(&(b.bits as i64))),
        (T_FLOAT, T_FLOAT) => f64::from_bits(a.bits).partial_cmp(&f64::from_bits(b.bits)),
        (T_INT, T_FLOAT) => int_float_cmp(a.bits as i64, f64::from_bits(b.bits)),
        (T_FLOAT, T_INT) => int_float_cmp(b.bits as i64, f64::from_bits(a.bits)).map(Ordering::reverse),
        _ => {
            let (da, db) = (dec_of(a), dec_of(b));
            match (da, db) {
                (Some(x), Some(y)) => Some(x.cmp(y)),
                (Some(x), None) if b.tag == T_INT => Some(x.cmp(&Dec::from_i64(b.bits as i64))),
                (None, Some(y)) if a.tag == T_INT => Some(Dec::from_i64(a.bits as i64).cmp(y)),
                (Some(x), None) if b.tag == T_FLOAT => x.cmp_f64(f64::from_bits(b.bits)),
                (None, Some(y)) if a.tag == T_FLOAT => y.cmp_f64(f64::from_bits(a.bits)).map(Ordering::reverse),
                _ => None,
            }
        }
    }
}

/// Exact comparison of an int with a float (as Python does).
fn int_float_cmp(i: i64, f: f64) -> Option<Ordering> {
    if f.is_nan() {
        return None;
    }
    if f.is_infinite() {
        return Some(if f > 0.0 { Ordering::Less } else { Ordering::Greater });
    }
    let fl = f.floor();
    let bi = BigInt::from(i);
    let bf = BigInt::from(fl.to_i128().unwrap_or(if fl > 0.0 { i128::MAX } else { i128::MIN }));
    match bi.cmp(&bf) {
        Ordering::Equal => Some(if f > fl { Ordering::Less } else { Ordering::Equal }),
        o => Some(o),
    }
}

fn is_dec_float_mix(a: V, b: V) -> bool {
    (dec_of(a).is_some() && b.tag == T_FLOAT) || (a.tag == T_FLOAT && dec_of(b).is_some())
}

/// `==` (D34): values of different types are not equal, except int and
/// float, which compare by value; float with decimal is an error.
pub fn equals(a: V, b: V, span: u32) -> R<bool> {
    let (oa, ob) = exact_literals(a, b);
    let (a, b) = (oa.v(), ob.v());
    if a.is_none() || b.is_none() {
        return Ok(a.is_none() && b.is_none());
    }
    if a.tag == T_BOOL || b.tag == T_BOOL {
        return Ok(a.tag == b.tag && a.bits == b.bits);
    }
    if is_num(a) && is_num(b) {
        if is_dec_float_mix(a, b) {
            return Err(mix(a, b, span));
        }
        return Ok(num_cmp(a, b) == Some(Ordering::Equal));
    }
    if let (Some(Obj::Quantity(x, ux)), Some(Obj::Quantity(y, uy))) = (a.get(), b.get()) {
        let (dx, _) = unit_dimension(ux);
        let (dy, _) = unit_dimension(uy);
        return Ok(dx == dy && qty_base(x, ux).eq(&qty_base(y, uy)));
    }
    let a = crate::pipe::force(a)?;
    let b = crate::pipe::force(b)?;
    Ok(py_eq(a.v(), b.v()))
}

fn natives(a: V, b: V) -> Option<(&'static dyn Native, &'static dyn Native)> {
    match (a.get(), b.get()) {
        (Some(Obj::Native(x)), Some(Obj::Native(y))) => Some((&**x, &**y)),
        _ => None,
    }
}

/// Structural equality used inside collections (Python `==` on values).
pub fn py_eq(a: V, b: V) -> bool {
    if a.tag != T_OBJ || b.tag != T_OBJ {
        if a.tag == T_BOOL || b.tag == T_BOOL {
            return a.tag == b.tag && a.bits == b.bits;
        }
        if a.is_none() || b.is_none() {
            return a.is_none() && b.is_none();
        }
        return num_cmp(a.plain(), b.plain()) == Some(Ordering::Equal);
    }
    match (a.get().unwrap(), b.get().unwrap()) {
        (Obj::Str(x), Obj::Str(y)) => x == y,
        (Obj::Decimal(x), Obj::Decimal(y)) => x.eq(y),
        (Obj::Money(x, cx), Obj::Money(y, cy)) => cx == cy && x.eq(y),
        (Obj::Quantity(x, ux), Obj::Quantity(y, uy)) => ux == uy && x.eq(y),
        (Obj::List(x), Obj::List(y)) => {
            let (x, y) = (x.borrow(), y.borrow());
            x.len() == y.len() && x.iter().zip(y.iter()).all(|(p, q)| py_eq(p.v(), q.v()))
        }
        (Obj::Map(x), Obj::Map(y)) => {
            let (x, y) = (x.borrow(), y.borrow());
            x.entries.len() == y.entries.len()
                && x.index.iter().all(|(k, i)| match y.index.get(&k) {
                    Some(j) => py_eq(x.entries[i].1.v(), y.entries[*j].1.v()),
                    None => false,
                })
        }
        (Obj::Set(x), Obj::Set(y)) => {
            let (x, y) = (x.borrow(), y.borrow());
            x.items.len() == y.items.len() && x.index.iter().all(|(k, _)| y.index.contains_key(&k))
        }
        (Obj::Range(a1, b1), Obj::Range(a2, b2)) => a1 == a2 && b1 == b2,
        (Obj::Record(x), Obj::Record(y)) => {
            std::ptr::eq(x.ty, y.ty) && {
                let (fx, fy) = (x.fields.borrow(), y.fields.borrow());
                fx.iter().zip(fy.iter()).all(|(p, q)| if p.tag == T_WEAK || q.tag == T_WEAK { p.same(q.v()) } else { py_eq(p.v(), q.v()) })
            }
        }
        (Obj::VariantValue(x), Obj::VariantValue(y)) => {
            std::ptr::eq(x.variant, y.variant) && x.fields.iter().zip(y.fields.iter()).all(|(p, q)| py_eq(p.v(), q.v()))
        }
        (Obj::Type(x), Obj::Type(y)) => crate::fmt::type_ref_name(x) == crate::fmt::type_ref_name(y),
        (Obj::Currency(x), Obj::Currency(y)) => x == y,
        (Obj::Symbol(x), Obj::Symbol(y)) => x == y,
        (Obj::Native(x), Obj::Native(y)) => x.type_name() == y.type_name() && x.equals(&**y),
        (Obj::Decimal(x), _) => b.tag == T_OBJ && matches!(b.get(), Some(Obj::Decimal(y)) if x.eq(y)),
        _ => a.bits == b.bits,
    }
}

/// `< > <= >=`
pub fn order(op: &str, a: V, b: V, span: u32) -> R<bool> {
    let (oa, ob) = exact_literals(a, b);
    let (a, b) = (oa.v(), ob.v());
    let ord = if is_num(a) && is_num(b) {
        if is_dec_float_mix(a, b) {
            return Err(mix(a, b, span));
        }
        num_cmp(a, b)
    } else if let (Some(x), Some(y)) = (a.as_str(), b.as_str()) {
        Some(x.cmp(y))
    } else if let (Some(Obj::Money(x, cx)), Some(Obj::Money(y, cy))) = (a.get(), b.get()) {
        if cx != cy {
            return Err(diag(Diagnostic::new_spanless("currency_mismatch").arg("a", *cx).arg("b", *cy), span));
        }
        Some(x.cmp(y))
    } else if let (Some(Obj::Quantity(x, ux)), Some(Obj::Quantity(y, uy))) = (a.get(), b.get()) {
        if unit_dimension(ux).0 != unit_dimension(uy).0 {
            return Err(compare_error(a, b, span));
        }
        Some(qty_base(x, ux).cmp(&qty_base(y, uy)))
    } else if let Some(o) = natives(a, b).filter(|(x, y)| x.type_name() == y.type_name()).and_then(|(x, y)| x.order(y)) {
        Some(o)
    } else {
        return Err(compare_error(a, b, span));
    };
    let Some(o) = ord else { return Ok(false) };
    Ok(match op {
        "<" => o == Ordering::Less,
        ">" => o == Ordering::Greater,
        "<=" => o != Ordering::Greater,
        _ => o != Ordering::Less,
    })
}

fn compare_error(a: V, b: V, span: u32) -> crate::state::Fail {
    diag(Diagnostic::new_spanless("compare").arg("a", type_name(a)).arg("b", type_name(b)), span)
}

/// Ordering used by `sort by` (none first); errors as `<` would give.
pub fn sort_less(a: V, b: V, span: u32) -> R<bool> {
    if a.is_none() || b.is_none() {
        return Ok(a.is_none() && !b.is_none());
    }
    order("<", a, b, span)
}
