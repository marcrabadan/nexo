//! io and data (section 15): files, JSON and CSV.
//!
//! A table is a list of rows; a row is a map from column name to value
//! (D44). CSV columns are typed as a whole (D45); `save` writes CSV, JSON
//! or text depending on the extension (D46).

use std::io::{BufRead, BufReader, Read};

use num_bigint::BigInt;
use num_traits::ToPrimitive;

use nexo_diag::msg;

use crate::call::call_value;
use crate::coll::map_from;
use crate::dec::Dec;
use crate::fmt::{format_float, repr, show, type_name};
use crate::pipe::lazy;
use crate::state::{diag, failure, raise_error, R};

use crate::types::{builtin_is, conform, lookup, TypeDesc, TypeRef};
use crate::value::*;

// ------------------------------------------------------------------- files

pub fn os_failure(path: &str, e: &std::io::Error, span: u32) -> crate::state::Fail {
    let reason = os_reason(e);
    let cause = Val::str(reason.clone());
    if e.kind() == std::io::ErrorKind::NotFound {
        let message = msg("err.NotFound", &[("path", path.to_string())]);
        return raise_error("NotFound", message, cause, span);
    }
    let message = msg("err.IOError", &[("path", path.to_string()), ("reason", reason)]);
    raise_error("IOError", message, cause, span)
}

/// The C library's text for an error (what Python's `strerror` shows).
fn os_reason(e: &std::io::Error) -> String {
    let text = e.to_string();
    match text.find(" (os error") {
        Some(i) => text[..i].to_string(),
        None => text,
    }
}

/// Text mode with universal newlines, as Python's `open()`.
fn universal(text: String) -> String {
    if text.contains('\r') {
        text.replace("\r\n", "\n").replace('\r', "\n")
    } else {
        text
    }
}

pub fn read_text(path: &str, span: u32) -> R<String> {
    let mut bytes = Vec::new();
    let read = crate::repl::read_file(|| {
        std::fs::File::open(path).and_then(|mut f| f.read_to_end(&mut bytes))?;
        Ok(std::mem::take(&mut bytes))
    });
    match read {
        Ok(b) => bytes = b,
        Err(e) => return Err(os_failure(path, &e, span)),
    }
    match String::from_utf8(bytes) {
        Ok(t) => Ok(universal(t)),
        Err(_) => Err(failure("InvalidData", span, &[("detail", msg("detail.encoding", &[("path", path.to_string())]))])),
    }
}

pub fn write_text(path: &str, text: &str, span: u32) -> R<()> {
    crate::repl::write_file(|| std::fs::write(path, text)).map_err(|e| os_failure(path, &e, span))
}

/// The lines of a file, read lazily one by one.
pub fn read_lines(path: &str, span: u32) -> R<Val> {
    if crate::repl::active() {
        return read_lines_journaled(path, span);
    }
    if let Err(e) = std::fs::File::open(path) {
        return Err(os_failure(path, &e, span));
    }
    let path = path.to_string();
    Ok(lazy(move || {
        let f = std::fs::File::open(&path).map_err(|e| os_failure(&path, &e, span))?;
        let mut reader = BufReader::new(f);
        let path = path.clone();
        Ok(Box::new(std::iter::from_fn(move || {
            let mut buf = Vec::new();
            match reader.read_until(b'\n', &mut buf) {
                Ok(0) => None,
                Ok(_) => {
                    while buf.last() == Some(&b'\n') || buf.last() == Some(&b'\r') {
                        buf.pop();
                    }
                    match String::from_utf8(buf) {
                        Ok(s) => Some(Ok(Val::str(s))),
                        Err(_) => Some(Err(failure(
                            "InvalidData",
                            span,
                            &[("detail", msg("detail.encoding", &[("path", path.clone())]))],
                        ))),
                    }
                }
                Err(e) => Some(Err(os_failure(&path, &e, span))),
            }
        })))
    }))
}

/// `read_lines` inside the REPL (D88): the file is read when the call runs,
/// through the journal, and its lines are taken from memory.
fn read_lines_journaled(path: &str, span: u32) -> R<Val> {
    let bytes = crate::repl::read_file(|| {
        let mut bytes = Vec::new();
        std::fs::File::open(path).and_then(|mut f| f.read_to_end(&mut bytes))?;
        Ok(bytes)
    })
    .map_err(|e| os_failure(path, &e, span))?;
    let path = path.to_string();
    Ok(lazy(move || {
        let mut lines: Vec<Vec<u8>> = bytes.split_inclusive(|&b| b == b'\n').map(|l| l.to_vec()).collect();
        lines.reverse();
        let path = path.clone();
        Ok(Box::new(std::iter::from_fn(move || {
            let mut buf = lines.pop()?;
            while buf.last() == Some(&b'\n') || buf.last() == Some(&b'\r') {
                buf.pop();
            }
            Some(match String::from_utf8(buf) {
                Ok(s) => Ok(Val::str(s)),
                Err(_) => Err(failure("InvalidData", span, &[("detail", msg("detail.encoding", &[("path", path.clone())]))])),
            })
        })))
    }))
}

/// Final pipeline step: .csv writes a table, .json writes JSON, any other
/// extension writes text (one line per element of a list).
pub fn save(value: V, path: &str, span: u32) -> R<()> {
    let lower = path.to_lowercase();
    let text = if lower.ends_with(".csv") {
        to_csv(value, span)?
    } else if lower.ends_with(".json") {
        to_json(value, 0) + "\n"
    } else if let Some(Obj::List(items)) = value.get() {
        items.borrow().iter().map(|v| show(v.v()) + "\n").collect()
    } else {
        show(value)
    };
    write_text(path, &text, span)
}

// --------------------------------------------------------------------- csv

fn is_int_cell(c: &str) -> bool {
    let t = c.strip_prefix('-').unwrap_or(c);
    !t.is_empty() && t.chars().all(|c| c.is_ascii_digit()) && c.parse::<i64>().is_ok()
}

fn csv_rows(text: &str) -> Vec<Vec<String>> {
    let chars: Vec<char> = text.chars().collect();
    let mut rows = Vec::new();
    let mut row: Vec<String> = Vec::new();
    let mut field = String::new();
    let mut i = 0;
    let mut in_quotes = false;
    let mut started = false;
    while i < chars.len() {
        let c = chars[i];
        if in_quotes {
            if c == '"' {
                if i + 1 < chars.len() && chars[i + 1] == '"' {
                    field.push('"');
                    i += 2;
                    continue;
                }
                in_quotes = false;
            } else {
                field.push(c);
            }
            i += 1;
            continue;
        }
        match c {
            '"' if field.is_empty() && !started => {
                in_quotes = true;
                started = true;
            }
            ',' => {
                row.push(std::mem::take(&mut field));
                started = false;
            }
            '\r' | '\n' => {
                if c == '\r' && i + 1 < chars.len() && chars[i + 1] == '\n' {
                    i += 1;
                }
                if !row.is_empty() || !field.is_empty() || started {
                    row.push(std::mem::take(&mut field));
                }
                rows.push(std::mem::take(&mut row));
                started = false;
            }
            _ => {
                field.push(c);
                started = true;
            }
        }
        i += 1;
    }
    if !row.is_empty() || !field.is_empty() || started {
        row.push(field);
        rows.push(row);
    }
    rows
}

#[derive(PartialEq)]
enum ColKind {
    Int,
    Float,
    Str,
}

/// A column is int if every non-empty cell is an integer, float if every
/// one is a number, and text otherwise.
fn column_kind(cells: &[&str]) -> ColKind {
    let values: Vec<&&str> = cells.iter().filter(|c| !c.is_empty()).collect();
    if !values.is_empty() && values.iter().all(|c| is_int_cell(c)) {
        return ColKind::Int;
    }
    if !values.is_empty() && values.iter().all(|c| crate::stdlib::is_float_text(c, false)) {
        return ColKind::Float;
    }
    ColKind::Str
}

pub fn parse_csv(text: &str) -> Val {
    let rows = csv_rows(text);
    if rows.is_empty() {
        return list(Vec::new());
    }
    let header = &rows[0];
    let body: Vec<&Vec<String>> = rows[1..].iter().filter(|r| !r.is_empty()).collect();
    let kinds: Vec<ColKind> = (0..header.len())
        .map(|i| {
            let cells: Vec<&str> = body.iter().map(|r| r.get(i).map(|s| s.as_str()).unwrap_or("")).collect();
            column_kind(&cells)
        })
        .collect();
    let mut table = Vec::with_capacity(body.len());
    for raw in body {
        let mut pairs = Vec::with_capacity(header.len());
        for (i, name) in header.iter().enumerate() {
            let cell = raw.get(i).map(|s| s.as_str()).unwrap_or("");
            let v = if cell.is_empty() {
                Val::none()
            } else {
                match kinds[i] {
                    ColKind::Int => Val::int(cell.parse().unwrap()),
                    ColKind::Float => Val::float(cell.parse().unwrap()),
                    ColKind::Str => Val::str(cell),
                }
            };
            pairs.push((Val::str(name.as_str()), v));
        }
        table.push(map_from(pairs));
    }
    list(table)
}

fn csv_field(s: &str, only: bool) -> String {
    if s.contains([',', '"', '\n', '\r']) || (only && s.is_empty()) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_string()
    }
}

fn csv_line(fields: &[String]) -> String {
    let only = fields.len() == 1;
    let parts: Vec<String> = fields.iter().map(|f| csv_field(f, only)).collect();
    parts.join(",") + "\n"
}

pub fn to_csv(value: V, span: u32) -> R<String> {
    let Some(Obj::List(items)) = value.get() else {
        return Err(diag(nexo_diag::Diagnostic::new_spanless("csv_value").arg("vtype", type_name(value)), span));
    };
    let mut columns: Vec<String> = Vec::new();
    let mut rows: Vec<Vec<(String, Val)>> = Vec::new();
    for item in items.borrow().iter() {
        let row: Vec<(String, Val)> = match item.get() {
            Some(Obj::Record(r)) => r.ty.fields.iter().map(|f| f.name.clone()).zip(r.fields.borrow().iter().map(|x| if x.tag == T_WEAK { Val::none() } else { x.clone() })).collect(),
            Some(Obj::Map(m)) => m.borrow().entries.iter().map(|(k, v)| (show(k.v()), v.clone())).collect(),
            _ => {
                return Err(diag(nexo_diag::Diagnostic::new_spanless("csv_value").arg("vtype", type_name(item.v())), span))
            }
        };
        for (k, _) in &row {
            if !columns.contains(k) {
                columns.push(k.clone());
            }
        }
        rows.push(row);
    }
    let mut out = csv_line(&columns);
    for row in rows {
        let cells: Vec<String> = columns
            .iter()
            .map(|c| match row.iter().rev().find(|(k, _)| k == c) {
                Some((_, v)) if !v.is_none() => show(v.v()),
                _ => String::new(),
            })
            .collect();
        out.push_str(&csv_line(&cells));
    }
    Ok(out)
}

// -------------------------------------------------------------------- json

pub(crate) enum Json {
    Null,
    Bool(bool),
    Int(BigInt),
    Float(f64),
    Str(String),
    List(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

struct JsonParser<'a> {
    s: &'a [char],
    i: usize,
}

type JResult<T> = Result<T, usize>;

impl<'a> JsonParser<'a> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], ' ' | '\t' | '\n' | '\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<char> {
        self.s.get(self.i).copied()
    }

    fn starts(&self, w: &str) -> bool {
        let w: Vec<char> = w.chars().collect();
        self.s.len() >= self.i + w.len() && self.s[self.i..self.i + w.len()] == w[..]
    }

    fn value(&mut self) -> JResult<Json> {
        let Some(c) = self.peek() else { return Err(self.i) };
        match c {
            '{' => self.object(),
            '[' => self.array(),
            '"' => Ok(Json::Str(self.string()?)),
            _ if self.starts("null") => {
                self.i += 4;
                Ok(Json::Null)
            }
            _ if self.starts("true") => {
                self.i += 4;
                Ok(Json::Bool(true))
            }
            _ if self.starts("false") => {
                self.i += 5;
                Ok(Json::Bool(false))
            }
            _ if self.starts("NaN") => {
                self.i += 3;
                Ok(Json::Float(f64::NAN))
            }
            _ if self.starts("Infinity") => {
                self.i += 8;
                Ok(Json::Float(f64::INFINITY))
            }
            _ if self.starts("-Infinity") => {
                self.i += 9;
                Ok(Json::Float(f64::NEG_INFINITY))
            }
            '-' | '0'..='9' => self.number(),
            _ => Err(self.i),
        }
    }

    fn number(&mut self) -> JResult<Json> {
        let start = self.i;
        if self.peek() == Some('-') {
            self.i += 1;
        }
        match self.peek() {
            Some('0') => self.i += 1,
            Some('1'..='9') => {
                while matches!(self.peek(), Some('0'..='9')) {
                    self.i += 1;
                }
            }
            _ => return Err(start),
        }
        let mut is_float = false;
        if self.peek() == Some('.') && matches!(self.s.get(self.i + 1), Some('0'..='9')) {
            is_float = true;
            self.i += 1;
            while matches!(self.peek(), Some('0'..='9')) {
                self.i += 1;
            }
        }
        if matches!(self.peek(), Some('e' | 'E')) {
            let save = self.i;
            self.i += 1;
            if matches!(self.peek(), Some('+' | '-')) {
                self.i += 1;
            }
            if matches!(self.peek(), Some('0'..='9')) {
                is_float = true;
                while matches!(self.peek(), Some('0'..='9')) {
                    self.i += 1;
                }
            } else {
                self.i = save;
            }
        }
        let text: String = self.s[start..self.i].iter().collect();
        if is_float {
            Ok(Json::Float(text.parse().unwrap()))
        } else {
            Ok(Json::Int(text.parse().unwrap()))
        }
    }

    fn string(&mut self) -> JResult<String> {
        let start = self.i;
        self.i += 1;
        let mut out = String::new();
        loop {
            let Some(c) = self.peek() else { return Err(start) };
            match c {
                '"' => {
                    self.i += 1;
                    return Ok(out);
                }
                '\\' => {
                    let esc_at = self.i;
                    self.i += 1;
                    let Some(e) = self.peek() else { return Err(start) };
                    self.i += 1;
                    match e {
                        '"' => out.push('"'),
                        '\\' => out.push('\\'),
                        '/' => out.push('/'),
                        'b' => out.push('\u{8}'),
                        'f' => out.push('\u{c}'),
                        'n' => out.push('\n'),
                        'r' => out.push('\r'),
                        't' => out.push('\t'),
                        'u' => {
                            let code = self.hex4().ok_or(esc_at)?;
                            if (0xd800..0xdc00).contains(&code) && self.starts("\\u") {
                                let save = self.i;
                                self.i += 2;
                                match self.hex4() {
                                    Some(low) if (0xdc00..0xe000).contains(&low) => {
                                        let c = 0x10000 + ((code - 0xd800) << 10) + (low - 0xdc00);
                                        out.push(char::from_u32(c).unwrap_or('\u{fffd}'));
                                        continue;
                                    }
                                    _ => self.i = save,
                                }
                            }
                            out.push(char::from_u32(code).unwrap_or('\u{fffd}'));
                        }
                        _ => return Err(esc_at),
                    }
                }
                c if (c as u32) < 0x20 => return Err(self.i),
                c => {
                    out.push(c);
                    self.i += 1;
                }
            }
        }
    }

    fn hex4(&mut self) -> Option<u32> {
        if self.i + 4 > self.s.len() {
            return None;
        }
        let t: String = self.s[self.i..self.i + 4].iter().collect();
        let v = u32::from_str_radix(&t, 16).ok()?;
        self.i += 4;
        Some(v)
    }

    fn object(&mut self) -> JResult<Json> {
        self.i += 1;
        let mut pairs = Vec::new();
        self.ws();
        if self.peek() == Some('}') {
            self.i += 1;
            return Ok(Json::Obj(pairs));
        }
        loop {
            self.ws();
            if self.peek() != Some('"') {
                return Err(self.i);
            }
            let key = self.string()?;
            self.ws();
            if self.peek() != Some(':') {
                return Err(self.i);
            }
            self.i += 1;
            self.ws();
            let v = self.value()?;
            pairs.push((key, v));
            self.ws();
            match self.peek() {
                Some(',') => self.i += 1,
                Some('}') => {
                    self.i += 1;
                    return Ok(Json::Obj(pairs));
                }
                _ => return Err(self.i),
            }
        }
    }

    fn array(&mut self) -> JResult<Json> {
        self.i += 1;
        let mut items = Vec::new();
        self.ws();
        if self.peek() == Some(']') {
            self.i += 1;
            return Ok(Json::List(items));
        }
        loop {
            self.ws();
            items.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(',') => self.i += 1,
                Some(']') => {
                    self.i += 1;
                    return Ok(Json::List(items));
                }
                _ => return Err(self.i),
            }
        }
    }
}

pub(crate) fn parse_json(text: &str) -> Result<Json, (usize, usize)> {
    if !crate::json::nesting_within(text, 128) { return Err((1, 1)); }
    let chars: Vec<char> = text.chars().collect();
    let mut p = JsonParser { s: &chars, i: 0 };
    p.ws();
    let result = p.value().and_then(|v| {
        p.ws();
        if p.i != chars.len() {
            Err(p.i)
        } else {
            Ok(v)
        }
    });
    result.map_err(|pos| {
        let line = chars[..pos].iter().filter(|c| **c == '\n').count() + 1;
        let col = match chars[..pos].iter().rposition(|c| *c == '\n') {
            Some(nl) => pos - nl,
            None => pos + 1,
        };
        (line, col)
    })
}

pub(crate) fn from_json(j: Json, span: u32) -> R<Val> {
    Ok(match j {
        Json::Null => Val::none(),
        Json::Bool(b) => Val::bool(b),
        Json::Int(i) => match i.to_i64() {
            Some(i) => Val::int(i),
            None => return Err(failure("Overflow", span, &[])),
        },
        Json::Float(f) => Val::float(f),
        Json::Str(s) => Val::str(s),
        Json::List(items) => {
            let mut out = Vec::with_capacity(items.len());
            for i in items {
                out.push(from_json(i, span)?);
            }
            list(out)
        }
        Json::Obj(pairs) => {
            let mut out = Vec::with_capacity(pairs.len());
            for (k, v) in pairs {
                out.push((Val::str(k), from_json(v, span)?));
            }
            map_from(out)
        }
    })
}

fn invalid(span: u32, key: &str, args: &[(&str, String)]) -> crate::state::Fail {
    failure("InvalidData", span, &[("detail", msg(key, args))])
}

/// The runtime type behind a field type when JSON needs converting.
#[derive(Clone, Copy)]
enum Target {
    Record(&'static crate::types::RecordType),
    List(Option<&'static TypeDesc>),
    Builtin(&'static str),
}

fn field_target(t: &'static TypeDesc) -> Option<Target> {
    match t {
        TypeDesc::Named { name, params, .. } => match name.as_str() {
            "list" if !params.is_empty() => Some(Target::List(Some(params[0]))),
            "float" | "decimal" | "date" | "time" => Some(Target::Builtin(crate::num::intern(name))),
            _ => None,
        },
        TypeDesc::User { id, .. } => match lookup(*id) {
            Some(TypeRef::Record(r)) => Some(Target::Record(r)),
            _ => None,
        },
        _ => None,
    }
}

/// Build a value of type `t` from JSON data (D47).
fn as_type(value: V, t: Target, span: u32) -> R<Val> {
    match t {
        Target::Record(rt) => {
            let Some(Obj::Map(m)) = value.get() else {
                return Err(invalid(span, "detail.expected", &[("type", rt.name.clone()), ("vtype", type_name(value))]));
            };
            let mut fields = Vec::with_capacity(rt.fields.len());
            for f in &rt.fields {
                let ty = f.ty.unwrap();
                let raw = m.borrow().get(&Key::Str(f.name.clone()));
                let present = m.borrow().index.contains_key(&Key::Str(f.name.clone()));
                if present {
                    let mut raw = Val::from(raw.unwrap());
                    if !raw.is_none() {
                        if let Some(inner) = field_target(ty) {
                            raw = as_type(raw.v(), inner, span)?;
                        }
                    }
                    match conform(raw.v(), ty, &f.name, span) {
                        Ok(v) => fields.push(crate::call::field_value(f, v)),
                        Err(_) => {
                            crate::state::clear();
                            return Err(invalid(
                                span,
                                "detail.field_type",
                                &[("field", f.name.clone()), ("type", ty.display().to_string()), ("vtype", type_name(raw.v()))],
                            ));
                        }
                    }
                } else if f.default.tag != T_UNDEF {
                    fields.push(crate::pipe::bind(call_value(f.default.v(), &[], span)?.v())?);
                } else if f.optional {
                    fields.push(Val::none());
                } else {
                    return Err(invalid(span, "detail.missing_field", &[("field", f.name.clone()), ("type", rt.name.clone())]));
                }
            }
            Ok(Val::new(Obj::Record(Record { ty: rt, fields: Mut::new(fields.into()), birth: std::cell::Cell::new(crate::state::stamp()) })))
        }
        Target::List(param) => {
            let Some(Obj::List(items)) = value.get() else {
                return Err(invalid(span, "detail.expected", &[("type", "list".into()), ("vtype", type_name(value))]));
            };
            let Some(inner) = param.and_then(field_target) else { return Ok(Val::from(value)) };
            let items: Vec<Val> = items.borrow().to_vec();
            let mut out = Vec::with_capacity(items.len());
            for v in items {
                out.push(as_type(v.v(), inner, span)?);
            }
            Ok(list(out))
        }
        Target::Builtin(name) => {
            if name == "any" {
                return Ok(Val::from(value));
            }
            if name == "float" && value.tag == T_INT {
                return Ok(Val::float(value.bits as i64 as f64));
            }
            if name == "date" || name == "time" {
                if let Some(t) = value.as_str() {
                    let parsed = if name == "date" { crate::time::parse_date(t) } else { crate::time::parse_time(t) };
                    return parsed.ok_or_else(|| {
                        invalid(span, "detail.expected", &[("type", name.to_string()), ("vtype", "str".into())])
                    });
                }
            }
            if name == "decimal" && (value.tag == T_INT || value.tag == T_FLOAT) {
                let text = if value.tag == T_INT { (value.bits as i64).to_string() } else { format_float(f64::from_bits(value.bits)) };
                return Ok(decimal(Dec::parse(&text).unwrap()));
            }
            if !builtin_is(value, name) {
                return Err(invalid(span, "detail.expected", &[("type", name.to_string()), ("vtype", type_name(value))]));
            }
            Ok(Val::from(value))
        }
    }
}

/// A record of type `rt` from a map, as JSON data is read (D47, D117).
pub fn record_from_map(value: V, rt: &'static crate::types::RecordType, span: u32) -> R<Val> {
    as_type(value, Target::Record(rt), span)
}

pub fn json_parse(text: &str, as_: Option<V>, span: u32) -> R<Val> {
    let j = match parse_json(text) {
        Ok(j) => j,
        Err((line, col)) => {
            return Err(invalid(span, "detail.json", &[("line", line.to_string()), ("col", col.to_string())]))
        }
    };
    let owned = from_json(j, span)?;
    let value = owned.v();
    let Some(t) = as_.filter(|t| !t.is_none()) else { return Ok(owned) };
    match t.get() {
        Some(Obj::Type(TypeRef::Record(rt))) => as_type(value, Target::Record(rt), span),
        Some(Obj::Type(TypeRef::Builtin(b))) => as_type(value, Target::Builtin(b), span),
        Some(Obj::Type(tr)) => {
            if !crate::types::is_type(value, *tr, span)? {
                return Err(invalid(
                    span,
                    "detail.expected",
                    &[("type", crate::fmt::type_ref_name(tr)), ("vtype", type_name(value))],
                ));
            }
            Ok(owned)
        }
        _ => Ok(owned),
    }
}

pub fn json_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
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
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// JSON on one line without spaces (for HTTP).
pub fn to_json_compact(v: V) -> String {
    to_json_in(v, 0, true)
}

/// JSON text of a value, indented with 2 spaces. Decimals keep all their
/// digits; money and quantities are written as text ("19.99 EUR").
pub fn to_json(v: V, level: usize) -> String {
    to_json_in(v, level, false)
}

fn to_json_in(v: V, level: usize, compact: bool) -> String {
    let pad = "  ".repeat(level + 1);
    let close = "  ".repeat(level);
    let pairs: Option<Vec<(String, Val)>> = match v.get() {
        Some(Obj::Record(r)) => {
            Some(r.ty.fields.iter().map(|f| f.name.clone()).zip(r.fields.borrow().iter().map(|x| if x.tag == T_WEAK { Val::none() } else { x.clone() })).collect())
        }
        Some(Obj::VariantValue(vv)) => {
            let mut p = vec![("variant".to_string(), Val::str(vv.variant.name.clone()))];
            p.extend(vv.variant.params.iter().map(|f| f.name.clone()).zip(vv.fields.iter().cloned()));
            Some(p)
        }
        Some(Obj::Map(m)) => Some(m.borrow().entries.iter().map(|(k, v)| (show(k.v()), v.clone())).collect()),
        _ => None,
    };
    if let Some(pairs) = pairs {
        if pairs.is_empty() {
            return "{}".into();
        }
        if compact {
            let items: Vec<String> = pairs.iter().map(|(k, x)| format!("{}:{}", json_string(k), to_json_in(x.v(), 0, true))).collect();
            return format!("{{{}}}", items.join(","));
        }
        let items: Vec<String> =
            pairs.iter().map(|(k, x)| format!("{pad}{}: {}", json_string(k), to_json_in(x.v(), level + 1, false))).collect();
        return format!("{{\n{}\n{close}}}", items.join(",\n"));
    }
    if let Some(Obj::List(items)) = v.get() {
        let items = items.borrow();
        if items.is_empty() {
            return "[]".into();
        }
        if compact {
            let parts: Vec<String> = items.iter().map(|x| to_json_in(x.v(), 0, true)).collect();
            return format!("[{}]", parts.join(","));
        }
        let parts: Vec<String> = items.iter().map(|x| format!("{pad}{}", to_json_in(x.v(), level + 1, false))).collect();
        return format!("[\n{}\n{close}]", parts.join(",\n"));
    }
    match v.tag {
        T_NONE => "null".into(),
        T_BOOL => if v.bits != 0 { "true" } else { "false" }.into(),
        T_INT | T_FLOAT | T_LITFLOAT => repr(v),
        T_SSTR => json_string(v.as_str().unwrap()),
        _ => match v.get() {
            Some(Obj::Decimal(d)) => d.py_str(),
            Some(Obj::Str(s)) => json_string(s),
            _ => json_string(&repr(v)),
        },
    }
}
