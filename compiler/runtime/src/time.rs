//! time: `now`, `today`, dates, times, durations and time zones (section
//! 15, D94). A `date` is a day of the calendar; a `time` is an instant in
//! milliseconds since 1970-01-01 UTC with the zone it is shown in (an IANA
//! name read from the system's TZif files, "UTC" or a fixed offset).
//! Wall clock times skipped or repeated by a change of offset take the
//! offset in effect before the change (PEP 495, fold 0), as phase 0 does.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use nexo_diag::{msg, Diagnostic};

use crate::dec::Dec;
use crate::fmt::type_name;
use crate::num::{normalize, unit_dimension, Op};
use crate::state::{diag, failure, R};
use crate::value::*;

const MS_PER_DAY: i64 = 86_400_000;

// ------------------------------------------------------------ calendar

/// Days since 1970-01-01 of a civil date (proleptic Gregorian).
pub fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

pub fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn days_in_month(y: i64, m: i64) -> i64 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        _ => {
            if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 {
                29
            } else {
                28
            }
        }
    }
}

pub fn valid_date(y: i64, m: i64, d: i64) -> bool {
    (1..=9999).contains(&y) && (1..=12).contains(&m) && d >= 1 && d <= days_in_month(y, m)
}

/// 1 = Monday ... 7 = Sunday
fn weekday(days: i64) -> i64 {
    (days + 3).rem_euclid(7) + 1
}

// ---------------------------------------------------------------- zones

struct Rule {
    std: i64,
    dst: i64,
    /// (month, week, weekday, seconds after local midnight) or Julian day
    start: RuleDate,
    end: RuleDate,
}

#[derive(Clone, Copy)]
enum RuleDate {
    /// Mm.w.d/time
    Month(i64, i64, i64, i64),
    /// Jn/time: day 1..365, February 29 never counted
    Julian(i64, i64),
    /// n/time: day 0..365, February 29 counted
    Day(i64, i64),
}

struct TzData {
    /// transitions: (UTC seconds, offset after it)
    trans: Vec<(i64, i64)>,
    /// offset before the first transition
    first: i64,
    /// the POSIX rule after the last transition: fixed offset or DST rule
    tail: Option<Result<i64, Rule>>,
}

#[derive(Clone)]
enum Zone {
    Fixed(i64),
    Tz(Arc<TzData>),
}

const TZPATH: &[&str] = &["/usr/share/zoneinfo", "/usr/lib/zoneinfo", "/usr/share/lib/zoneinfo", "/etc/zoneinfo"];

fn zones() -> &'static Mutex<HashMap<String, Option<Zone>>> {
    static Z: OnceLock<Mutex<HashMap<String, Option<Zone>>>> = OnceLock::new();
    Z.get_or_init(|| Mutex::new(HashMap::new()))
}

fn parse_offset_name(z: &str) -> Option<i64> {
    let b = z.as_bytes();
    if b.len() != 6 || (b[0] != b'+' && b[0] != b'-') || b[3] != b':' {
        return None;
    }
    let h: i64 = z[1..3].parse().ok()?;
    let m: i64 = z[4..6].parse().ok()?;
    let s = (h * 60 + m) * 60;
    Some(if b[0] == b'-' { -s } else { s })
}

fn zone(name: &str) -> Option<Zone> {
    if name == "UTC" {
        return Some(Zone::Fixed(0));
    }
    if let Some(s) = parse_offset_name(name) {
        return Some(Zone::Fixed(s));
    }
    if name.is_empty() || name.starts_with('/') || name.contains("..") {
        return None;
    }
    let mut cache = zones().lock().unwrap();
    if let Some(z) = cache.get(name) {
        return z.clone();
    }
    let z = TZPATH.iter().find_map(|dir| std::fs::read(format!("{dir}/{name}")).ok()).and_then(|b| parse_tzif(&b)).map(|d| Zone::Tz(Arc::new(d)));
    cache.insert(name.to_string(), z.clone());
    z
}

pub fn valid_zone(name: &str) -> bool {
    zone(name).is_some()
}

fn be32(b: &[u8], i: usize) -> i64 {
    i32::from_be_bytes(b[i..i + 4].try_into().unwrap()) as i64
}

fn be64(b: &[u8], i: usize) -> i64 {
    i64::from_be_bytes(b[i..i + 8].try_into().unwrap())
}

fn parse_tzif(b: &[u8]) -> Option<TzData> {
    if b.len() < 44 || &b[0..4] != b"TZif" {
        return None;
    }
    let version = b[4];
    let counts = |at: usize| -> Option<[usize; 6]> {
        let mut c = [0usize; 6];
        for (k, item) in c.iter_mut().enumerate() {
            *item = u32::from_be_bytes(b.get(at + 20 + 4 * k..at + 24 + 4 * k)?.try_into().ok()?) as usize;
        }
        Some(c)
    };
    let c = counts(0)?;
    let (isut, isstd, leap, timecnt, typecnt, charcnt) = (c[0], c[1], c[2], c[3], c[4], c[5]);
    let v1_len = timecnt * 5 + typecnt * 6 + charcnt + leap * 8 + isstd + isut;
    let (start, tsize, c) = if version >= b'2' {
        let at = 44 + v1_len;
        (at + 44, 8, counts(at)?)
    } else {
        (44, 4, c)
    };
    let (isut, isstd, leap, timecnt, typecnt, charcnt) = (c[0], c[1], c[2], c[3], c[4], c[5]);
    let times_at = start;
    let idx_at = times_at + timecnt * tsize;
    let types_at = idx_at + timecnt;
    let chars_at = types_at + typecnt * 6;
    let end = chars_at + charcnt + leap * (tsize + 4) + isstd + isut;
    if b.len() < end || typecnt == 0 {
        return None;
    }
    let types: Vec<i64> = (0..typecnt).map(|k| be32(b, types_at + 6 * k)).collect();
    let mut trans = Vec::with_capacity(timecnt);
    for k in 0..timecnt {
        let t = if tsize == 8 { be64(b, times_at + 8 * k) } else { be32(b, times_at + 4 * k) };
        let ty = *types.get(b[idx_at + k] as usize)?;
        trans.push((t, ty));
    }
    let mut tail = None;
    if version >= b'2' && b.len() > end + 1 && b[end] == b'\n' {
        if let Some(stop) = b[end + 1..].iter().position(|c| *c == b'\n') {
            let footer = std::str::from_utf8(&b[end + 1..end + 1 + stop]).ok()?;
            if !footer.is_empty() {
                tail = parse_posix(footer);
            }
        }
    }
    Some(TzData { trans, first: types[0], tail })
}

/// A POSIX TZ string such as "CET-1CEST,M3.5.0,M10.5.0/3".
fn parse_posix(s: &str) -> Option<Result<i64, Rule>> {
    let b = s.as_bytes();
    let mut i = 0;
    let name = |i: &mut usize| -> Option<()> {
        if b.get(*i) == Some(&b'<') {
            while *i < b.len() && b[*i] != b'>' {
                *i += 1;
            }
            *i += 1;
        } else {
            let st = *i;
            while *i < b.len() && b[*i].is_ascii_alphabetic() {
                *i += 1;
            }
            if *i == st {
                return None;
            }
        }
        Some(())
    };
    // [+-]hh[:mm[:ss]] -> seconds
    let time = |i: &mut usize| -> Option<i64> {
        let mut sign = 1;
        if b.get(*i) == Some(&b'-') {
            sign = -1;
            *i += 1;
        } else if b.get(*i) == Some(&b'+') {
            *i += 1;
        }
        let mut parts = [0i64; 3];
        for (k, part) in parts.iter_mut().enumerate() {
            if k > 0 {
                if b.get(*i) != Some(&b':') {
                    break;
                }
                *i += 1;
            }
            let st = *i;
            while *i < b.len() && b[*i].is_ascii_digit() {
                *i += 1;
            }
            if *i == st {
                return None;
            }
            *part = s[st..*i].parse().ok()?;
        }
        Some(sign * (parts[0] * 3600 + parts[1] * 60 + parts[2]))
    };
    name(&mut i)?;
    let std = -time(&mut i)?;
    if i >= b.len() {
        return Some(Ok(std));
    }
    name(&mut i)?;
    let mut dst = std + 3600;
    if i < b.len() && b[i] != b',' {
        dst = -time(&mut i)?;
    }
    let date = |i: &mut usize| -> Option<RuleDate> {
        if b.get(*i) != Some(&b',') {
            return None;
        }
        *i += 1;
        let num = |i: &mut usize| -> Option<i64> {
            let st = *i;
            while *i < b.len() && b[*i].is_ascii_digit() {
                *i += 1;
            }
            s[st..*i].parse().ok()
        };
        let mut at = 7200;
        let d = match b.get(*i) {
            Some(b'M') => {
                *i += 1;
                let m = num(i)?;
                *i += 1;
                let w = num(i)?;
                *i += 1;
                let wd = num(i)?;
                if b.get(*i) == Some(&b'/') {
                    *i += 1;
                    at = time(i)?;
                }
                RuleDate::Month(m, w, wd, at)
            }
            Some(b'J') => {
                *i += 1;
                let n = num(i)?;
                if b.get(*i) == Some(&b'/') {
                    *i += 1;
                    at = time(i)?;
                }
                RuleDate::Julian(n, at)
            }
            _ => {
                let n = num(i)?;
                if b.get(*i) == Some(&b'/') {
                    *i += 1;
                    at = time(i)?;
                }
                RuleDate::Day(n, at)
            }
        };
        Some(d)
    };
    let start = date(&mut i)?;
    let end = date(&mut i)?;
    Some(Err(Rule { std, dst, start, end }))
}

impl RuleDate {
    /// Local seconds since 1970 of the change in `year` (in the offset in
    /// effect before it).
    fn local(self, year: i64) -> i64 {
        match self {
            RuleDate::Month(m, w, wd, at) => {
                let first = days_from_civil(year, m, 1);
                // weekday of the first day, 0 = Sunday
                let first_wd = (first + 4).rem_euclid(7);
                let mut day = 1 + (wd - first_wd).rem_euclid(7) + (w - 1) * 7;
                while day > days_in_month(year, m) {
                    day -= 7;
                }
                (first + day - 1) * 86400 + at
            }
            RuleDate::Julian(n, at) => {
                let mut d = days_from_civil(year, 1, 1) + n - 1;
                if days_in_month(year, 2) == 29 && n >= 60 {
                    d += 1;
                }
                d * 86400 + at
            }
            RuleDate::Day(n, at) => (days_from_civil(year, 1, 1) + n) * 86400 + at,
        }
    }
}

impl Rule {
    /// The changes of `year` as (UTC seconds, offset after it).
    fn transitions(&self, year: i64) -> [(i64, i64); 2] {
        let on = (self.start.local(year) - self.std, self.dst);
        let off = (self.end.local(year) - self.dst, self.std);
        if on.0 <= off.0 {
            [on, off]
        } else {
            [off, on]
        }
    }
}

impl Zone {
    /// The changes of offset around `secs` (UTC seconds): (time, offset
    /// after), sorted, and the offset before the first of them.
    fn around(&self, secs: i64) -> (i64, Vec<(i64, i64)>) {
        match self {
            Zone::Fixed(o) => (*o, Vec::new()),
            Zone::Tz(d) => {
                let mut list = d.trans.clone();
                if let Some(Err(rule)) = &d.tail {
                    let last = list.last().map(|t| t.0).unwrap_or(i64::MIN);
                    let year = civil_from_days(secs.div_euclid(86400)).0;
                    let from = civil_from_days(last.max(-(1i64 << 40)).div_euclid(86400)).0.max(year - 2);
                    for y in from..=year + 1 {
                        for t in rule.transitions(y) {
                            if t.0 > last {
                                list.push(t);
                            }
                        }
                    }
                } else if let Some(Ok(fixed)) = &d.tail {
                    if list.last().map(|t| t.1) != Some(*fixed) {
                        let last = list.last().map(|t| t.0).unwrap_or(i64::MIN);
                        list.push((last.saturating_add(1), *fixed));
                    }
                }
                (d.first, list)
            }
        }
    }

    fn offset_at(&self, utc: i64) -> i64 {
        let (first, list) = self.around(utc);
        let k = list.partition_point(|t| t.0 <= utc);
        if k == 0 {
            first
        } else {
            list[k - 1].1
        }
    }

    /// The UTC second a wall clock shows (fold 0: before a change, the
    /// earlier offset holds until the later of both wall times).
    fn utc_of(&self, wall: i64) -> i64 {
        let (first, list) = self.around(wall);
        let mut before = first;
        for (t, after) in &list {
            if wall < t + before.max(*after) {
                break;
            }
            before = *after;
        }
        wall - before
    }
}

pub fn local_zone() -> String {
    if let Ok(tz) = std::env::var("TZ") {
        let mut tz = tz.trim_start_matches(':').to_string();
        if !tz.is_empty() {
            if let Some(i) = tz.find("zoneinfo/") {
                tz = tz[i + 9..].to_string();
            }
            return if valid_zone(&tz) { tz } else { "UTC".into() };
        }
    }
    if let Ok(p) = std::fs::canonicalize("/etc/localtime") {
        let p = p.to_string_lossy().to_string();
        if let Some(i) = p.find("zoneinfo/") {
            let name = &p[i + 9..];
            if valid_zone(name) {
                return name.to_string();
            }
        }
    }
    "UTC".into()
}

// --------------------------------------------------------------- values

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Date {
    pub days: i64,
}

pub struct Time {
    pub ms: i64,
    pub zone: String,
}

impl Date {
    fn ymd(&self) -> (i64, i64, i64) {
        civil_from_days(self.days)
    }
}

fn date_text(days: i64) -> String {
    let (y, m, d) = civil_from_days(days);
    format!("{y:04}-{m:02}-{d:02}")
}

impl Time {
    fn offset(&self) -> i64 {
        zone(&self.zone).map(|z| z.offset_at(self.ms.div_euclid(1000))).unwrap_or(0)
    }

    /// local milliseconds since 1970 on the clock of its zone
    fn wall(&self) -> i64 {
        self.ms + self.offset() * 1000
    }

    fn parts(&self) -> (i64, i64, i64, i64, i64, i64) {
        let w = self.wall();
        let days = w.div_euclid(MS_PER_DAY);
        let rest = w.rem_euclid(MS_PER_DAY) / 1000;
        let (y, m, d) = civil_from_days(days);
        (y, m, d, rest / 3600, rest / 60 % 60, rest % 60)
    }

    fn date(&self) -> Date {
        Date { days: self.wall().div_euclid(MS_PER_DAY) }
    }
}

const DATE_MEMBERS: &[&str] = &["year", "month", "day", "weekday"];
const TIME_MEMBERS: &[&str] = &["year", "month", "day", "hour", "minute", "second", "date", "zone", "to_zone"];

impl Native for Date {
    fn type_name(&self) -> &str {
        "date"
    }
    fn repr(&self) -> String {
        date_text(self.days)
    }
    fn member(&self, _me: V, name: &str, span: u32) -> R<Val> {
        let (y, m, d) = self.ymd();
        Ok(match name {
            "year" => Val::int(y),
            "month" => Val::int(m),
            "day" => Val::int(d),
            "weekday" => Val::int(weekday(self.days)),
            _ => return Err(crate::stdlib::missing_member("date", name, DATE_MEMBERS, span)),
        })
    }
    fn key(&self) -> Option<String> {
        Some(format!("date:{}", self.days))
    }
    fn equals(&self, other: &dyn Native) -> bool {
        other.as_any().downcast_ref::<Date>().is_some_and(|o| o.days == self.days)
    }
    fn order(&self, other: &dyn Native) -> Option<std::cmp::Ordering> {
        other.as_any().downcast_ref::<Date>().map(|o| self.days.cmp(&o.days))
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

impl Native for Time {
    fn type_name(&self) -> &str {
        "time"
    }
    fn repr(&self) -> String {
        let (y, m, d, h, mi, s) = self.parts();
        let mut text = format!("{y:04}-{m:02}-{d:02}T{h:02}:{mi:02}:{s:02}");
        let ms = self.ms.rem_euclid(1000);
        if ms != 0 {
            text.push_str(&format!(".{ms:03}"));
        }
        let off = self.offset().div_euclid(60);
        if off == 0 {
            text.push('Z');
        } else {
            let sign = if off > 0 { '+' } else { '-' };
            let a = off.abs();
            text.push_str(&format!("{sign}{:02}:{:02}", a / 60, a % 60));
        }
        text
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        let (y, m, d, h, mi, s) = self.parts();
        Ok(match name {
            "year" => Val::int(y),
            "month" => Val::int(m),
            "day" => Val::int(d),
            "hour" => Val::int(h),
            "minute" => Val::int(mi),
            "second" => Val::int(s),
            "date" => native(self.date()),
            "zone" => Val::str(self.zone.clone()),
            "to_zone" => Val::new(Obj::BuiltinMethod(Val::from(me), "to_zone")),
            _ => return Err(crate::stdlib::missing_member("time", name, TIME_MEMBERS, span)),
        })
    }
    fn call_method(&self, _me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        let a = crate::stdlib::method_args(name, &["zone"], 1, args, names, span)?;
        let z = check_zone("to_zone", a[0], span)?;
        Ok(native(Time { ms: self.ms, zone: z }))
    }
    fn key(&self) -> Option<String> {
        Some(format!("time:{}", self.ms))
    }
    fn equals(&self, other: &dyn Native) -> bool {
        other.as_any().downcast_ref::<Time>().is_some_and(|o| o.ms == self.ms)
    }
    fn order(&self, other: &dyn Native) -> Option<std::cmp::Ordering> {
        other.as_any().downcast_ref::<Time>().map(|o| self.ms.cmp(&o.ms))
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

fn check_zone(fname: &str, z: V, span: u32) -> R<String> {
    let Some(s) = z.as_str() else { return Err(crate::stdlib::expects(fname, "str", z, span)) };
    if !valid_zone(s) {
        return Err(failure("InvalidValue", span, &[("detail", msg("detail.zone", &[("zone", s.to_string())]))]));
    }
    Ok(s.to_string())
}

fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// `now` and `today` (read each time they are used).
pub fn clock(name: &str) -> Val {
    let t = Time { ms: now_ms(), zone: local_zone() };
    if name == "today" {
        native(t.date())
    } else {
        native(t)
    }
}

fn wall_to_ms(y: i64, m: i64, d: i64, h: i64, mi: i64, s: i64, ms: i64, z: &str) -> Option<i64> {
    let zone = zone(z)?;
    let wall = days_from_civil(y, m, d) * 86400 + h * 3600 + mi * 60 + s;
    Some(zone.utc_of(wall) * 1000 + ms)
}

fn bad_date(text: String, span: u32) -> crate::state::Fail {
    failure("InvalidValue", span, &[("detail", msg("detail.bad_date", &[("value", text)]))])
}

/// `date(year, month, day)` or `date(t)`
pub fn make_date(args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    let a = crate::stdlib::method_args("date", &["year", "month", "day"], 1, args, names, span)?;
    if let Some(t) = a[0].native::<Time>() {
        if a[1].tag == T_MISSING && a[2].tag == T_MISSING {
            return Ok(native(t.date()));
        }
    }
    let mut n = [0i64; 3];
    for (k, v) in a.iter().enumerate() {
        if v.tag == T_MISSING {
            return Err(diag(
                Diagnostic::new_spanless("arg_missing").arg("param", ["year", "month", "day"][k]).arg("fn", "date"),
                span,
            ));
        }
        n[k] = v.as_int().ok_or_else(|| crate::stdlib::expects("date", "int", *v, span))?;
    }
    if !valid_date(n[0], n[1], n[2]) {
        return Err(bad_date(format!("{}-{:02}-{:02}", n[0], n[1], n[2]), span));
    }
    Ok(native(Date { days: days_from_civil(n[0], n[1], n[2]) }))
}

/// `time(year, month, day, hour, minute, second, zone:)`
pub fn make_time(args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    let params = ["year", "month", "day", "hour", "minute", "second", "zone"];
    let a = crate::stdlib::method_args("time", &params, 3, args, names, span)?;
    let mut n = [0i64; 6];
    for k in 0..6 {
        if a[k].tag == T_MISSING {
            continue;
        }
        n[k] = a[k].as_int().ok_or_else(|| crate::stdlib::expects("time", "int", a[k], span))?;
    }
    let z = if a[6].tag == T_MISSING || a[6].is_none() { local_zone() } else { check_zone("zone", a[6], span)? };
    let ok = valid_date(n[0], n[1], n[2]) && (0..24).contains(&n[3]) && (0..60).contains(&n[4]) && (0..60).contains(&n[5]);
    let ms = if ok { wall_to_ms(n[0], n[1], n[2], n[3], n[4], n[5], 0, &z) } else { None };
    match ms {
        Some(ms) => Ok(native(Time { ms, zone: z })),
        None => Err(bad_date(format!("{}-{:02}-{:02} {:02}:{:02}:{:02}", n[0], n[1], n[2], n[3], n[4], n[5]), span)),
    }
}

fn digits(s: &str) -> Option<i64> {
    if s.is_empty() || !s.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    s.parse().ok()
}

/// "2026-10-24"
pub fn parse_date(t: &str) -> Option<Val> {
    let b = t.as_bytes();
    if b.len() != 10 || b[4] != b'-' || b[7] != b'-' {
        return None;
    }
    let (y, m, d) = (digits(&t[0..4])?, digits(&t[5..7])?, digits(&t[8..10])?);
    valid_date(y, m, d).then(|| native(Date { days: days_from_civil(y, m, d) }))
}

/// "2026-10-24T09:30[:15[.250]](Z|+02:00)"
pub fn parse_time(t: &str) -> Option<Val> {
    let b = t.as_bytes();
    if b.len() < 17 || !matches!(b[10], b'T' | b' ') {
        return None;
    }
    let date = &t[..10];
    if b[4] != b'-' || b[7] != b'-' {
        return None;
    }
    let (y, m, d) = (digits(&date[0..4])?, digits(&date[5..7])?, digits(&date[8..10])?);
    let rest = &t[11..];
    let (clock, zone_text) = if let Some(c) = rest.strip_suffix('Z') {
        (c, "UTC".to_string())
    } else {
        if rest.len() < 6 {
            return None;
        }
        let (c, z) = rest.split_at(rest.len() - 6);
        parse_offset_name(z)?;
        (c, if z == "+00:00" || z == "-00:00" { "UTC".to_string() } else { z.to_string() })
    };
    let (main, frac) = match clock.split_once('.') {
        Some((a, f)) => (a, Some(f)),
        None => (clock, None),
    };
    let parts: Vec<&str> = main.split(':').collect();
    if !(2..=3).contains(&parts.len()) || parts.iter().any(|p| p.len() != 2) || (frac.is_some() && parts.len() != 3) {
        return None;
    }
    let h = digits(parts[0])?;
    let mi = digits(parts[1])?;
    let s = if parts.len() == 3 { digits(parts[2])? } else { 0 };
    let ms = match frac {
        Some(f) if (1..=3).contains(&f.len()) => digits(&format!("{f:0<3}"))?,
        Some(_) => return None,
        None => 0,
    };
    if !valid_date(y, m, d) || h > 23 || mi > 59 || s > 59 {
        return None;
    }
    let ms = wall_to_ms(y, m, d, h, mi, s, ms, &zone_text)?;
    Some(native(Time { ms, zone: zone_text }))
}

// ------------------------------------------------------------ arithmetic

fn duration(v: V) -> Option<Dec> {
    match v.get() {
        Some(Obj::Quantity(d, u)) => {
            let (dim, f) = unit_dimension(u);
            (dim == "time").then(|| d.mul(&f))
        }
        _ => None,
    }
}

fn bad(op: Op, a: V, b: V, span: u32) -> crate::state::Fail {
    diag(
        Diagnostic::new_spanless("bad_operands").arg("op", op.text()).arg("a", type_name(a)).arg("b", type_name(b)),
        span,
    )
}

/// date ± days, date - date, time ± duration, time - time.
pub fn arith(op: Op, a: V, b: V, span: u32) -> R<Val> {
    let add = op == Op::Add;
    if !(add || op == Op::Sub) {
        return Err(bad(op, a, b, span));
    }
    if let (Some(d), Some(secs)) = (a.native::<Date>(), duration(b)) {
        let day = Dec::from_i64(86400);
        let days = secs.div(&day);
        if !days.is_integral() {
            return Err(diag(Diagnostic::new_spanless("date_days").arg("value", crate::fmt::repr(b)), span));
        }
        let n = days.to_i64().unwrap_or(i64::MAX / 4);
        let to = if add { d.days.checked_add(n) } else { d.days.checked_sub(n) };
        return match to.filter(|t| (days_from_civil(1, 1, 1)..=days_from_civil(9999, 12, 31)).contains(t)) {
            Some(t) => Ok(native(Date { days: t })),
            None => Err(failure("InvalidValue", span, &[("detail", msg("detail.date_range", &[]))])),
        };
    }
    if add && duration(a).is_some() && (b.native::<Date>().is_some() || b.native::<Time>().is_some()) {
        return arith(op, b, a, span);
    }
    if let (Some(x), Some(y), false) = (a.native::<Date>(), b.native::<Date>(), add) {
        return Ok(Val::new(Obj::Quantity(Dec::from_i64(x.days - y.days), "days")));
    }
    if let (Some(t), Some(secs)) = (a.native::<Time>(), duration(b)) {
        let q = secs.mul(&Dec::from_i64(1000)).quantize(0, crate::dec::Rounding::HalfEven);
        let delta = q.and_then(|q| q.to_i64()).ok_or_else(|| failure("Overflow", span, &[]))?;
        let ms = if add { t.ms.checked_add(delta) } else { t.ms.checked_sub(delta) };
        let ms = ms.ok_or_else(|| failure("Overflow", span, &[]))?;
        return Ok(native(Time { ms, zone: t.zone.clone() }));
    }
    if let (Some(x), Some(y), false) = (a.native::<Time>(), b.native::<Time>(), add) {
        let d = Dec::from_i64(x.ms - y.ms).div(&Dec::from_i64(1000));
        return Ok(Val::new(Obj::Quantity(normalize(d), "s")));
    }
    Err(bad(op, a, b, span))
}
