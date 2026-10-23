//! `decimal`: exact decimal numbers with 28 significant digits (section 17),
//! following the General Decimal Arithmetic rules that the phase 0
//! interpreter got from Python's `decimal` module (ROUND_HALF_EVEN, ideal
//! exponents, trailing zeros kept: 0.10 * 10 is 1.00).

use std::cmp::Ordering;

use num_bigint::BigUint;
use num_integer::Integer;
use num_traits::{One, ToPrimitive, Zero};

pub const PREC: usize = 28;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rounding {
    HalfEven,
    HalfUp,
    /// toward negative infinity
    Floor,
    /// toward zero
    Down,
}

#[derive(Clone, Debug)]
pub struct Dec {
    pub neg: bool,
    pub coef: BigUint,
    pub exp: i64,
}

fn pow10(n: u64) -> BigUint {
    BigUint::from(10u32).pow(n as u32)
}

pub fn ndigits(c: &BigUint) -> usize {
    if c.is_zero() {
        1
    } else {
        c.to_str_radix(10).len()
    }
}

impl Dec {
    pub fn zero() -> Dec {
        Dec { neg: false, coef: BigUint::zero(), exp: 0 }
    }

    pub fn from_i64(v: i64) -> Dec {
        Dec { neg: v < 0, coef: BigUint::from(v.unsigned_abs()), exp: 0 }
    }

    pub fn from_parts(neg: bool, coef: BigUint, exp: i64) -> Dec {
        Dec { neg, coef, exp }
    }

    /// `Decimal(text)`: `[sign] digits [. digits] [e [sign] digits]`.
    pub fn parse(text: &str) -> Option<Dec> {
        let t = text.trim();
        let (neg, t) = match t.strip_prefix('-') {
            Some(r) => (true, r),
            None => (false, t.strip_prefix('+').unwrap_or(t)),
        };
        let (mant, e) = match t.find(['e', 'E']) {
            Some(i) => {
                let e: i64 = t[i + 1..].parse().ok()?;
                (&t[..i], e)
            }
            None => (t, 0),
        };
        let (int, frac) = match mant.find('.') {
            Some(i) => (&mant[..i], &mant[i + 1..]),
            None => (mant, ""),
        };
        if int.is_empty() && frac.is_empty() {
            return None;
        }
        if !int.chars().chain(frac.chars()).all(|c| c.is_ascii_digit() || c == '_') {
            return None;
        }
        let digits: String = int.chars().chain(frac.chars()).filter(|c| *c != '_').collect();
        let coef = BigUint::parse_bytes(digits.as_bytes(), 10)?;
        Some(Dec { neg, coef, exp: e - frac.chars().filter(|c| *c != '_').count() as i64 })
    }

    pub fn is_zero(&self) -> bool {
        self.coef.is_zero()
    }

    fn digits(&self) -> usize {
        ndigits(&self.coef)
    }

    pub fn adjusted(&self) -> i64 {
        self.exp + self.digits() as i64 - 1
    }

    /// Change the exponent, rounding if digits are dropped.
    pub fn rescale(&self, exp: i64, mode: Rounding) -> Dec {
        if self.is_zero() {
            return Dec { neg: self.neg, coef: BigUint::zero(), exp };
        }
        if exp <= self.exp {
            let coef = &self.coef * pow10((self.exp - exp) as u64);
            return Dec { neg: self.neg, coef, exp };
        }
        let drop = (exp - self.exp) as u64;
        let coef = round_div(&self.coef, drop, mode, self.neg);
        Dec { neg: self.neg, coef, exp }
    }

    /// Round to the context precision (`_fix`).
    pub fn fix(mut self) -> Dec {
        let n = self.digits();
        if n > PREC {
            let drop = (n - PREC) as u64;
            self.coef = round_div(&self.coef, drop, Rounding::HalfEven, self.neg);
            self.exp += drop as i64;
            if ndigits(&self.coef) > PREC {
                self.coef /= BigUint::from(10u32);
                self.exp += 1;
            }
        }
        self
    }

    pub fn neg(&self) -> Dec {
        if self.is_zero() {
            return Dec { neg: false, coef: BigUint::zero(), exp: self.exp };
        }
        Dec { neg: !self.neg, coef: self.coef.clone(), exp: self.exp }.fix()
    }

    pub fn abs(&self) -> Dec {
        Dec { neg: false, coef: self.coef.clone(), exp: self.exp }
    }

    pub fn add(&self, other: &Dec) -> Dec {
        let exp = self.exp.min(other.exp);
        if self.is_zero() && other.is_zero() {
            return Dec { neg: self.neg && other.neg, coef: BigUint::zero(), exp }.fix();
        }
        if self.is_zero() {
            let e = exp.max(other.exp - PREC as i64 - 1);
            return other.rescale(e, Rounding::HalfEven).fix();
        }
        if other.is_zero() {
            let e = exp.max(self.exp - PREC as i64 - 1);
            return self.rescale(e, Rounding::HalfEven).fix();
        }
        let (a, b) = normalize(self, other);
        let (neg, coef) = if a.neg == b.neg {
            (a.neg, &a.coef + &b.coef)
        } else {
            match a.coef.cmp(&b.coef) {
                Ordering::Equal => return Dec { neg: false, coef: BigUint::zero(), exp: a.exp }.fix(),
                Ordering::Greater => (a.neg, &a.coef - &b.coef),
                Ordering::Less => (b.neg, &b.coef - &a.coef),
            }
        };
        Dec { neg, coef, exp: a.exp }.fix()
    }

    pub fn sub(&self, other: &Dec) -> Dec {
        let mut o = other.clone();
        o.neg = !o.neg;
        self.add(&o)
    }

    pub fn mul(&self, other: &Dec) -> Dec {
        Dec { neg: self.neg != other.neg, coef: &self.coef * &other.coef, exp: self.exp + other.exp }.fix()
    }

    /// Division; the caller checks for a zero divisor.
    pub fn div(&self, other: &Dec) -> Dec {
        let sign = self.neg != other.neg;
        if self.is_zero() {
            return Dec { neg: sign, coef: BigUint::zero(), exp: self.exp - other.exp }.fix();
        }
        let shift = other.digits() as i64 - self.digits() as i64 + PREC as i64 + 1;
        let mut exp = self.exp - other.exp - shift;
        let (mut coef, rem) = if shift >= 0 {
            (&self.coef * pow10(shift as u64)).div_rem(&other.coef)
        } else {
            self.coef.div_rem(&(&other.coef * pow10((-shift) as u64)))
        };
        if !rem.is_zero() {
            // not exact: make sure the rounding sees a non-zero remainder
            if (&coef % BigUint::from(5u32)).is_zero() {
                coef += BigUint::one();
            }
        } else {
            let ideal = self.exp - other.exp;
            let ten = BigUint::from(10u32);
            while exp < ideal && (&coef % &ten).is_zero() {
                coef /= &ten;
                exp += 1;
            }
        }
        Dec { neg: sign, coef, exp }.fix()
    }

    /// `x ** n` for an integer n.
    pub fn powi(&self, n: i64) -> Dec {
        if n >= 0 {
            let coef = self.coef.pow(n as u32);
            let neg = self.neg && n % 2 == 1;
            return Dec { neg, coef, exp: self.exp * n }.fix();
        }
        let positive = self.powi(-n);
        Dec::from_i64(1).div(&positive)
    }

    pub fn cmp(&self, other: &Dec) -> Ordering {
        if self.is_zero() && other.is_zero() {
            return Ordering::Equal;
        }
        let sa = if self.is_zero() { 0 } else if self.neg { -1 } else { 1 };
        let sb = if other.is_zero() { 0 } else if other.neg { -1 } else { 1 };
        if sa != sb {
            return sa.cmp(&sb);
        }
        let e = self.exp.min(other.exp);
        let a = &self.coef * pow10((self.exp - e) as u64);
        let b = &other.coef * pow10((other.exp - e) as u64);
        let ord = a.cmp(&b);
        if sa < 0 {
            ord.reverse()
        } else {
            ord
        }
    }

    pub fn eq(&self, other: &Dec) -> bool {
        self.cmp(other) == Ordering::Equal
    }

    pub fn is_integral(&self) -> bool {
        if self.exp >= 0 || self.is_zero() {
            return true;
        }
        (&self.coef % pow10((-self.exp) as u64)).is_zero()
    }

    pub fn to_integral(&self, mode: Rounding) -> Dec {
        if self.exp >= 0 {
            return self.clone();
        }
        self.rescale(0, mode)
    }

    /// `quantize`: None when the result needs more than 28 digits.
    pub fn quantize(&self, exp: i64, mode: Rounding) -> Option<Dec> {
        if self.is_zero() {
            return Some(Dec { neg: self.neg, coef: BigUint::zero(), exp }.fix());
        }
        if self.adjusted() - exp + 1 > PREC as i64 {
            return None;
        }
        let ans = self.rescale(exp, mode);
        if ans.digits() > PREC {
            return None;
        }
        Some(ans)
    }

    /// Strip trailing zeros (`normalize`).
    pub fn normalize(&self) -> Dec {
        let d = self.clone().fix();
        if d.is_zero() {
            return Dec { neg: d.neg, coef: BigUint::zero(), exp: 0 };
        }
        let ten = BigUint::from(10u32);
        let mut coef = d.coef;
        let mut exp = d.exp;
        while (&coef % &ten).is_zero() {
            coef /= &ten;
            exp += 1;
        }
        Dec { neg: d.neg, coef, exp }
    }

    /// Integer part truncated toward zero, if it fits in an i64.
    pub fn to_i64(&self) -> Option<i64> {
        let t = self.to_integral(Rounding::Down);
        let mag = &t.coef * pow10(t.exp.max(0) as u64);
        let v = mag.to_i128()?;
        let v = if t.neg { -v } else { v };
        i64::try_from(v).ok()
    }

    pub fn to_f64(&self) -> f64 {
        self.py_str().parse::<f64>().unwrap_or(f64::NAN)
    }

    /// Python's `str(Decimal)`: scientific notation for large exponents.
    pub fn py_str(&self) -> String {
        let digits = self.coef.to_str_radix(10);
        let leftdigits = self.exp + digits.len() as i64;
        let dotplace = if self.exp <= 0 && leftdigits > -6 { leftdigits } else { 1 };
        let mut out = layout(&digits, dotplace);
        if leftdigits != dotplace {
            out.push_str(&format!("E{:+}", leftdigits - dotplace));
        }
        if self.neg {
            format!("-{out}")
        } else {
            out
        }
    }

    /// Fixed-point text, never an exponent (`format(d, "f")`).
    pub fn fixed(&self) -> String {
        let digits = self.coef.to_str_radix(10);
        let leftdigits = self.exp + digits.len() as i64;
        let out = layout(&digits, leftdigits);
        if self.neg {
            format!("-{out}")
        } else {
            out
        }
    }

    /// Canonical text used to compare decimals as map keys.
    pub fn key(&self) -> String {
        let n = self.normalize();
        if n.is_zero() {
            return "0".into();
        }
        format!("{}{}e{}", if n.neg { "-" } else { "" }, n.coef, n.exp)
    }

    /// Exact comparison with a binary float (as Python does).
    pub fn cmp_f64(&self, f: f64) -> Option<Ordering> {
        let other = exact_from_f64(f)?;
        Some(self.cmp(&other))
    }
}

/// The exact value of a finite float as a decimal.
pub fn exact_from_f64(f: f64) -> Option<Dec> {
    if !f.is_finite() {
        return None;
    }
    if f == 0.0 {
        return Some(Dec::zero());
    }
    let bits = f.to_bits();
    let neg = bits >> 63 == 1;
    let e = ((bits >> 52) & 0x7ff) as i64;
    let m = bits & 0x000f_ffff_ffff_ffff;
    let (mant, e2) = if e == 0 { (m, -1074) } else { (m | (1 << 52), e - 1075) };
    if e2 >= 0 {
        let coef = BigUint::from(mant) << (e2 as usize);
        return Some(Dec { neg, coef, exp: 0 });
    }
    // mant / 2^k = mant * 5^k / 10^k
    let k = (-e2) as u32;
    let coef = BigUint::from(mant) * BigUint::from(5u32).pow(k);
    Some(Dec { neg, coef, exp: -(k as i64) })
}

fn layout(digits: &str, dotplace: i64) -> String {
    let n = digits.len() as i64;
    if dotplace <= 0 {
        format!("0.{}{}", "0".repeat((-dotplace) as usize), digits)
    } else if dotplace >= n {
        format!("{}{}", digits, "0".repeat((dotplace - n) as usize))
    } else {
        format!("{}.{}", &digits[..dotplace as usize], &digits[dotplace as usize..])
    }
}

/// coef / 10^drop with the given rounding (neg tells the sign for Floor).
fn round_div(coef: &BigUint, drop: u64, mode: Rounding, neg: bool) -> BigUint {
    let div = pow10(drop);
    let (q, r) = coef.div_rem(&div);
    if r.is_zero() {
        return q;
    }
    let twice = &r * BigUint::from(2u32);
    let up = match mode {
        Rounding::Down => false,
        Rounding::Floor => neg,
        Rounding::HalfUp => twice >= div,
        Rounding::HalfEven => match twice.cmp(&div) {
            Ordering::Greater => true,
            Ordering::Less => false,
            Ordering::Equal => q.is_odd(),
        },
    };
    if up {
        q + BigUint::one()
    } else {
        q
    }
}

/// Bring both operands to the same exponent (`_normalize` in Python): when
/// one is far smaller than the other's precision it is replaced by a tiny
/// stand-in that rounds the same way.
fn normalize(a: &Dec, b: &Dec) -> (Dec, Dec) {
    let (mut tmp, mut other, swapped) = if a.exp < b.exp { (b.clone(), a.clone(), true) } else { (a.clone(), b.clone(), false) };
    let tmp_len = tmp.digits() as i64;
    let other_len = other.digits() as i64;
    let exp = tmp.exp + (-1i64).min(tmp_len - PREC as i64 - 2);
    if other_len + other.exp - 1 < exp {
        other.coef = BigUint::one();
        other.exp = exp;
    }
    tmp.coef *= pow10((tmp.exp - other.exp) as u64);
    tmp.exp = other.exp;
    if swapped {
        (other, tmp)
    } else {
        (tmp, other)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> Dec {
        Dec::parse(s).unwrap()
    }

    #[test]
    fn arithmetic_keeps_trailing_zeros() {
        assert_eq!(d("19.99").mul(&Dec::from_i64(3)).py_str(), "59.97");
        assert_eq!(d("0.10").mul(&Dec::from_i64(10)).py_str(), "1.00");
        assert_eq!(d("0.1").add(&d("0.2")).py_str(), "0.3");
        assert_eq!(d("0.00").add(&d("5")).py_str(), "5.00");
        assert_eq!(d("1.10").sub(&d("1.10")).py_str(), "0.00");
    }

    #[test]
    fn division_uses_28_digits_and_ideal_exponents() {
        assert_eq!(Dec::from_i64(1).div(&Dec::from_i64(3)).py_str(), "0.3333333333333333333333333333");
        assert_eq!(Dec::from_i64(10).div(&Dec::from_i64(4)).py_str(), "2.5");
        assert_eq!(d("1.00").div(&Dec::from_i64(2)).py_str(), "0.50");
        assert_eq!(Dec::from_i64(2).div(&Dec::from_i64(3)).py_str(), "0.6666666666666666666666666667");
        assert_eq!(d("21").div(&Dec::from_i64(100)).py_str(), "0.21");
        assert_eq!(Dec::from_i64(100).div(&Dec::from_i64(4)).py_str(), "25");
    }

    #[test]
    fn quantize_and_rounding() {
        assert_eq!(d("2.675").quantize(-2, Rounding::HalfEven).unwrap().py_str(), "2.68");
        assert_eq!(d("2.665").quantize(-2, Rounding::HalfEven).unwrap().py_str(), "2.66");
        assert_eq!(d("2.665").quantize(-2, Rounding::HalfUp).unwrap().py_str(), "2.67");
        assert_eq!(d("25.2000").quantize(-2, Rounding::HalfEven).unwrap().py_str(), "25.20");
        assert_eq!(d("-2.5").to_integral(Rounding::Floor).py_str(), "-3");
        assert_eq!(d("2.5").to_integral(Rounding::HalfEven).py_str(), "2");
    }

    #[test]
    fn text_forms() {
        assert_eq!(d("1e-3").py_str(), "0.001");
        assert_eq!(d("1E+3").py_str(), "1E+3");
        assert_eq!(d("1E+3").fixed(), "1000");
        assert_eq!(d("0.0000001").py_str(), "1E-7");
        assert_eq!(d("0.0000001").fixed(), "0.0000001");
        assert_eq!(d("-0").neg().py_str(), "0");
        assert_eq!(d("20.0").normalize().py_str(), "2E+1");
    }

    #[test]
    fn comparisons() {
        assert!(d("1.0").eq(&Dec::from_i64(1)));
        assert_eq!(d("0.1").cmp_f64(0.1), Some(Ordering::Less));
        assert_eq!(d("0.5").cmp_f64(0.5), Some(Ordering::Equal));
        assert_eq!(d("-3").cmp(&d("2")), Ordering::Less);
        assert_eq!(d("2.675").to_f64(), 2.675);
    }
}
