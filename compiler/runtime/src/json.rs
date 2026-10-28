//! A JSON value for the modules of phase 5 (flags, infra, ci, stream):
//! parsing, and printing exactly as phase 0's `json.dumps` does, compact
//! (`separators=(",", ":")`) or indented by two spaces, with
//! `ensure_ascii=False`.

use std::fmt::Write;

#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

static NULL: Json = Json::Null;

impl Json {
    pub fn get(&self, key: &str) -> &Json {
        match self {
            Json::Obj(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v).unwrap_or(&NULL),
            _ => &NULL,
        }
    }

    pub fn has(&self, key: &str) -> bool {
        matches!(self, Json::Obj(pairs) if pairs.iter().any(|(k, _)| k == key))
    }

    pub fn get_mut(&mut self, key: &str) -> Option<&mut Json> {
        match self {
            Json::Obj(pairs) => pairs.iter_mut().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// Set a key of an object, keeping its place when it exists.
    pub fn set(&mut self, key: &str, value: Json) {
        if let Json::Obj(pairs) = self {
            if let Some(slot) = pairs.iter_mut().find(|(k, _)| k == key) {
                slot.1 = value;
            } else {
                pairs.push((key.to_string(), value));
            }
        }
    }

    pub fn remove(&mut self, key: &str) -> Option<Json> {
        if let Json::Obj(pairs) = self {
            if let Some(i) = pairs.iter().position(|(k, _)| k == key) {
                return Some(pairs.remove(i).1);
            }
        }
        None
    }

    pub fn str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn int(&self) -> Option<i64> {
        match self {
            Json::Int(n) => Some(*n),
            _ => None,
        }
    }

    pub fn bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn arr(&self) -> &[Json] {
        match self {
            Json::Arr(items) => items,
            _ => &[],
        }
    }

    pub fn pairs(&self) -> &[(String, Json)] {
        match self {
            Json::Obj(pairs) => pairs,
            _ => &[],
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Json::Null)
    }

    pub fn is_obj(&self) -> bool {
        matches!(self, Json::Obj(_))
    }

    /// Python's truth: null, false, 0, "", [] and {} are false.
    pub fn truthy(&self) -> bool {
        match self {
            Json::Null => false,
            Json::Bool(b) => *b,
            Json::Int(n) => *n != 0,
            Json::Num(n) => *n != 0.0,
            Json::Str(s) => !s.is_empty(),
            Json::Arr(a) => !a.is_empty(),
            Json::Obj(o) => !o.is_empty(),
        }
    }

    /// `json.dumps(x, ensure_ascii=False, separators=(",", ":"))`
    pub fn compact(&self) -> String {
        let mut out = String::new();
        self.write(&mut out, None, 0, false);
        out
    }

    /// `json.dumps(x, ensure_ascii=False, indent=2[, sort_keys=True])`
    pub fn pretty(&self, sort: bool) -> String {
        let mut out = String::new();
        self.write(&mut out, Some(2), 0, sort);
        out
    }

    fn write(&self, out: &mut String, indent: Option<usize>, level: usize, sort: bool) {
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Int(n) => {
                let _ = write!(out, "{n}");
            }
            Json::Num(n) => out.push_str(&py_float(*n)),
            Json::Str(s) => write_str(s, out),
            Json::Arr(items) => {
                if items.is_empty() {
                    out.push_str("[]");
                    return;
                }
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    if let Some(n) = indent {
                        out.push('\n');
                        out.push_str(&" ".repeat(n * (level + 1)));
                    }
                    item.write(out, indent, level + 1, sort);
                }
                if let Some(n) = indent {
                    out.push('\n');
                    out.push_str(&" ".repeat(n * level));
                }
                out.push(']');
            }
            Json::Obj(pairs) => {
                if pairs.is_empty() {
                    out.push_str("{}");
                    return;
                }
                let mut order: Vec<&(String, Json)> = pairs.iter().collect();
                if sort {
                    order.sort_by(|a, b| a.0.cmp(&b.0));
                }
                out.push('{');
                for (i, (k, v)) in order.into_iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    if let Some(n) = indent {
                        out.push('\n');
                        out.push_str(&" ".repeat(n * (level + 1)));
                    }
                    write_str(k, out);
                    out.push(':');
                    if indent.is_some() {
                        out.push(' ');
                    }
                    v.write(out, indent, level + 1, sort);
                }
                if let Some(n) = indent {
                    out.push('\n');
                    out.push_str(&" ".repeat(n * level));
                }
                out.push('}');
            }
        }
    }
}

/// Python's repr of a float.
pub fn py_float(n: f64) -> String {
    if n.is_nan() {
        return "NaN".into();
    }
    if n.is_infinite() {
        return if n > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    crate::fmt::format_float(n)
}

pub fn write_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

pub fn obj(pairs: Vec<(&str, Json)>) -> Json {
    Json::Obj(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

pub fn s(text: impl Into<String>) -> Json {
    Json::Str(text.into())
}

/// Iterative preflight before any recursive parse or destruction of a JSON tree.
/// Quotes and escapes are tracked so brackets inside strings do not count.
pub fn nesting_within(text: &str, max: usize) -> bool {
    let (mut depth, mut quoted, mut escaped) = (0usize, false, false);
    for b in text.bytes() {
        if quoted {
            if escaped { escaped = false; } else if b == b'\\' { escaped = true; } else if b == b'"' { quoted = false; }
        } else if b == b'"' { quoted = true; }
        else if b == b'[' || b == b'{' { depth += 1; if depth > max { return false; } }
        else if b == b']' || b == b'}' { depth = depth.saturating_sub(1); }
    }
    true
}

pub fn parse(text: &str) -> Option<Json> {
    parse_options(text, false)
}

/// Operator security manifests must not silently accept conflicting fields.
pub fn parse_unique(text: &str) -> Option<Json> {
    parse_options(text, true)
}

fn parse_options(text: &str, unique: bool) -> Option<Json> {
    if !nesting_within(text, 128) { return None; }
    let mut p = P { s: text.as_bytes(), i: 0, unique };
    p.ws();
    let v = p.value()?;
    p.ws();
    if p.i != p.s.len() {
        return None;
    }
    Some(v)
}

struct P<'a> {
    s: &'a [u8],
    i: usize,
    unique: bool,
}

impl P<'_> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn eat(&mut self, word: &str) -> bool {
        if self.s[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            true
        } else {
            false
        }
    }

    fn value(&mut self) -> Option<Json> {
        self.ws();
        let c = *self.s.get(self.i)?;
        match c {
            b'n' => self.eat("null").then_some(Json::Null),
            b't' => self.eat("true").then_some(Json::Bool(true)),
            b'f' => self.eat("false").then_some(Json::Bool(false)),
            b'"' => self.string().map(Json::Str),
            b'[' => {
                self.i += 1;
                let mut items = Vec::new();
                self.ws();
                if self.s.get(self.i) == Some(&b']') {
                    self.i += 1;
                    return Some(Json::Arr(items));
                }
                loop {
                    items.push(self.value()?);
                    self.ws();
                    match self.s.get(self.i)? {
                        b',' => self.i += 1,
                        b']' => {
                            self.i += 1;
                            return Some(Json::Arr(items));
                        }
                        _ => return None,
                    }
                }
            }
            b'{' => {
                self.i += 1;
                let mut pairs: Vec<(String, Json)> = Vec::new();
                self.ws();
                if self.s.get(self.i) == Some(&b'}') {
                    self.i += 1;
                    return Some(Json::Obj(pairs));
                }
                loop {
                    self.ws();
                    if self.s.get(self.i) != Some(&b'"') {
                        return None;
                    }
                    let k = self.string()?;
                    self.ws();
                    if self.s.get(self.i) != Some(&b':') {
                        return None;
                    }
                    self.i += 1;
                    let v = self.value()?;
                    // a repeated key keeps the last value, in the first place
                    if let Some(slot) = pairs.iter_mut().find(|(x, _)| *x == k) {
                        if self.unique { return None; }
                        slot.1 = v;
                    } else {
                        pairs.push((k, v));
                    }
                    self.ws();
                    match self.s.get(self.i)? {
                        b',' => self.i += 1,
                        b'}' => {
                            self.i += 1;
                            return Some(Json::Obj(pairs));
                        }
                        _ => return None,
                    }
                }
            }
            _ => {
                let start = self.i;
                if self.s[self.i] == b'-' {
                    self.i += 1;
                }
                match self.s.get(self.i) {
                    Some(b'0') => self.i += 1,
                    Some(b'1'..=b'9') => { while self.s.get(self.i).is_some_and(u8::is_ascii_digit) { self.i += 1; } }
                    _ => return None,
                }
                let mut float = false;
                if self.s.get(self.i) == Some(&b'.') {
                    float = true; self.i += 1;
                    let digits = self.i;
                    while self.s.get(self.i).is_some_and(u8::is_ascii_digit) { self.i += 1; }
                    if self.i == digits { return None; }
                }
                if matches!(self.s.get(self.i), Some(b'e' | b'E')) {
                    float = true; self.i += 1;
                    if matches!(self.s.get(self.i), Some(b'+' | b'-')) { self.i += 1; }
                    let digits = self.i;
                    while self.s.get(self.i).is_some_and(u8::is_ascii_digit) { self.i += 1; }
                    if self.i == digits { return None; }
                }
                let t = std::str::from_utf8(&self.s[start..self.i]).ok()?;
                if !float { if let Ok(n) = t.parse::<i64>() { return Some(Json::Int(n)); } }
                let n = t.parse::<f64>().ok()?;
                n.is_finite().then_some(Json::Num(n))
            }
        }
    }

    fn hex4(&mut self) -> Option<u32> {
        let t = std::str::from_utf8(self.s.get(self.i..self.i + 4)?).ok()?;
        self.i += 4;
        u32::from_str_radix(t, 16).ok()
    }

    fn string(&mut self) -> Option<String> {
        self.i += 1;
        let mut out: Vec<u8> = Vec::new();
        loop {
            let c = *self.s.get(self.i)?;
            self.i += 1;
            match c {
                b'"' => return String::from_utf8(out).ok(),
                b'\\' => {
                    let e = *self.s.get(self.i)?;
                    self.i += 1;
                    match e {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(8),
                        b'f' => out.push(12),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let mut cp = self.hex4()?;
                            if (0xD800..0xDC00).contains(&cp) && self.s.get(self.i..self.i + 2) == Some(b"\\u") {
                                self.i += 2;
                                let lo = self.hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&lo) { return None; }
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            }
                            let ch = char::from_u32(cp)?;
                            let mut buf = [0u8; 4];
                            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                        }
                        _ => return None,
                    }
                }
                0..=31 => return None,
                c => out.push(c),
            }
        }
    }
}

#[cfg(test)]
mod adversarial_tests {
    use super::*;
    #[test]
    fn rejects_hostile_depth_before_recursive_parsing() {
        let deep = format!("{}0{}", "[".repeat(12000), "]".repeat(12000));
        assert!(parse(&deep).is_none());
        assert!(parse_unique(&deep).is_none());
        assert!(crate::data::parse_json(&deep).is_err());
        let boundary = format!("{}0{}", "[".repeat(128), "]".repeat(128));
        assert!(parse(&boundary).is_some());
        assert!(crate::data::parse_json(&boundary).is_ok());
        assert!(parse(r#"{"text":"[\\\"{}]"}"#).is_some());
    }
    #[test]
    fn strict_configuration_grammar_and_unicode() {
        for bad in ["01", "-01", "1.", "1.e2", "1e", "1e+", "1e9999", "+1", "NaN", "Infinity", "\"a\nb\"",
            r#""\ud800\u0000""#, r#""\ud800\ud800""#, r#""\udc00""#] {
            assert!(parse_unique(bad).is_none(), "{bad:?}");
        }
        for good in ["0", "-0", "1.25", "-1e+2", r#""\ud83d\ude00""#, r#"{"v":1}"#] {
            assert!(parse_unique(good).is_some(), "{good:?}");
        }
        assert!(parse_unique(r#"{"v":1,"v":2}"#).is_none());
    }
    #[test]
    fn seeded_malformed_input_campaign_does_not_panic() {
        let mut seed = 0x4e45584fu64;
        let alphabet = b"[]{}\\\"0123456789eE+-.truefalsnul :\n\t";
        for _ in 0..20000 {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            let n = (seed >> 32) as usize % 256;
            let mut input = String::new();
            for _ in 0..n {
                seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
                input.push(alphabet[(seed >> 32) as usize % alphabet.len()] as char);
            }
            let _ = parse_unique(&input);
            let _ = crate::data::parse_json(&input);
        }
        // Exercise all low-surrogate alternatives, including malformed pairs.
        for low in 0..=65535 {
            let input = format!("\"\\ud800\\u{low:04x}\"");
            assert_eq!(parse_unique(&input).is_some(), (0xdc00..=0xdfff).contains(&low));
        }
    }
}
