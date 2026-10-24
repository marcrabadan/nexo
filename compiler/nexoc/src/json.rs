//! A small JSON value for the language server (D87): parsing and printing.

use std::fmt::Write;

#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub fn get(&self, key: &str) -> &Json {
        static NULL: Json = Json::Null;
        match self {
            Json::Obj(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v).unwrap_or(&NULL),
            _ => &NULL,
        }
    }

    pub fn str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn num(&self) -> Option<f64> {
        match self {
            Json::Num(n) => Some(*n),
            _ => None,
        }
    }

    pub fn arr(&self) -> &[Json] {
        match self {
            Json::Arr(items) => items,
            _ => &[],
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Json::Null)
    }

    pub fn write(&self, out: &mut String) {
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Num(n) => {
                if n.fract() == 0.0 && n.abs() < 1e15 {
                    let _ = write!(out, "{}", *n as i64);
                } else {
                    let _ = write!(out, "{n}");
                }
            }
            Json::Str(s) => write_str(s, out),
            Json::Arr(items) => {
                out.push('[');
                for (i, v) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    v.write(out);
                }
                out.push(']');
            }
            Json::Obj(pairs) => {
                out.push('{');
                for (i, (k, v)) in pairs.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_str(k, out);
                    out.push(':');
                    v.write(out);
                }
                out.push('}');
            }
        }
    }

    pub fn text(&self) -> String {
        let mut out = String::new();
        self.write(&mut out);
        out
    }
}

fn write_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Shorthands to build values.
pub fn obj(pairs: Vec<(&str, Json)>) -> Json {
    Json::Obj(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

pub fn s(text: impl Into<String>) -> Json {
    Json::Str(text.into())
}

pub fn n(value: impl Into<f64>) -> Json {
    Json::Num(value.into())
}

pub fn parse(text: &str) -> Option<Json> {
    let chars: Vec<char> = text.chars().collect();
    let mut p = Parser { c: &chars, i: 0 };
    let v = p.value()?;
    p.ws();
    (p.i == chars.len()).then_some(v)
}

struct Parser<'a> {
    c: &'a [char],
    i: usize,
}

impl Parser<'_> {
    fn ws(&mut self) {
        while self.i < self.c.len() && self.c[self.i].is_whitespace() {
            self.i += 1;
        }
    }

    fn eat(&mut self, word: &str) -> bool {
        let w: Vec<char> = word.chars().collect();
        if self.c[self.i..].starts_with(&w) {
            self.i += w.len();
            true
        } else {
            false
        }
    }

    fn value(&mut self) -> Option<Json> {
        self.ws();
        let c = *self.c.get(self.i)?;
        match c {
            'n' => self.eat("null").then_some(Json::Null),
            't' => self.eat("true").then_some(Json::Bool(true)),
            'f' => self.eat("false").then_some(Json::Bool(false)),
            '"' => self.string().map(Json::Str),
            '[' => {
                self.i += 1;
                let mut items = Vec::new();
                self.ws();
                if self.c.get(self.i) == Some(&']') {
                    self.i += 1;
                    return Some(Json::Arr(items));
                }
                loop {
                    items.push(self.value()?);
                    self.ws();
                    match self.c.get(self.i)? {
                        ',' => self.i += 1,
                        ']' => {
                            self.i += 1;
                            return Some(Json::Arr(items));
                        }
                        _ => return None,
                    }
                }
            }
            '{' => {
                self.i += 1;
                let mut pairs = Vec::new();
                self.ws();
                if self.c.get(self.i) == Some(&'}') {
                    self.i += 1;
                    return Some(Json::Obj(pairs));
                }
                loop {
                    self.ws();
                    if self.c.get(self.i) != Some(&'"') {
                        return None;
                    }
                    let k = self.string()?;
                    self.ws();
                    if self.c.get(self.i) != Some(&':') {
                        return None;
                    }
                    self.i += 1;
                    let v = self.value()?;
                    pairs.push((k, v));
                    self.ws();
                    match self.c.get(self.i)? {
                        ',' => self.i += 1,
                        '}' => {
                            self.i += 1;
                            return Some(Json::Obj(pairs));
                        }
                        _ => return None,
                    }
                }
            }
            _ => {
                let start = self.i;
                while self.i < self.c.len() && (self.c[self.i].is_ascii_digit() || "+-.eE".contains(self.c[self.i])) {
                    self.i += 1;
                }
                let t: String = self.c[start..self.i].iter().collect();
                t.parse().ok().map(Json::Num)
            }
        }
    }

    fn hex4(&mut self) -> Option<u32> {
        let t: String = self.c.get(self.i..self.i + 4)?.iter().collect();
        self.i += 4;
        u32::from_str_radix(&t, 16).ok()
    }

    fn string(&mut self) -> Option<String> {
        self.i += 1;
        let mut out = String::new();
        loop {
            let c = *self.c.get(self.i)?;
            self.i += 1;
            match c {
                '"' => return Some(out),
                '\\' => {
                    let e = *self.c.get(self.i)?;
                    self.i += 1;
                    match e {
                        'n' => out.push('\n'),
                        't' => out.push('\t'),
                        'r' => out.push('\r'),
                        'b' => out.push('\u{8}'),
                        'f' => out.push('\u{c}'),
                        'u' => {
                            let mut code = self.hex4()?;
                            if (0xD800..0xDC00).contains(&code) && self.eat("\\u") {
                                let low = self.hex4()?;
                                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                            }
                            out.push(char::from_u32(code).unwrap_or('\u{fffd}'));
                        }
                        other => out.push(other),
                    }
                }
                c => out.push(c),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let text = r#"{"a":[1,2.5,true,null],"b":"x\"y\nñ😀","c":{}}"#;
        let v = parse(text).unwrap();
        assert_eq!(v.get("b").str(), Some("x\"y\nñ😀"));
        assert_eq!(parse(&v.text()).unwrap(), v);
        assert_eq!(v.get("a").arr().len(), 4);
    }
}
