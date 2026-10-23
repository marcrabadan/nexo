//! Lexer: source text -> tokens, following the lexical rules of section 24.
//!
//! - UTF-8; identifiers accept Unicode letters, digits and `_`.
//! - Indentation is exactly 4 spaces per level; a tab is an error. INDENT and
//!   DEDENT are emitted when the level changes.
//! - A newline ends a statement, except inside ( ) [ ] { } or when the next
//!   line starts with `|>`.
//! - `#` starts a comment; `##` is a documentation comment.
//! - Numbers: 1_000, 3.14, 1e-3. A number followed by a unit or currency
//!   (30s, 10%, 5 km, 19.99 EUR) is a unit literal.
//! - Text: "..." with {expr} interpolation, triple-quoted multi-line text,
//!   and {{ for a literal brace.

use nexo_diag::{Diagnostic, Span};

/// The 39 core reserved words (section 27).
pub const KEYWORDS: &[&str] = &[
    "if", "elif", "else", "for", "in", "while", "repeat", "match", "pass", "fn", "return", "type", "trait",
    "is", "pub", "use", "var", "self", "weak", "true", "false", "none", "and", "or", "not", "as", "try",
    "unsafe", "expect", "ensure", "parallel", "on", "emit", "when", "every", "at", "lifecycle", "test",
    "assert",
];

/// Time units in seconds (D2): `m` means minutes.
pub const TIME_UNITS: &[(&str, &str)] = &[
    ("ms", "0.001"),
    ("s", "1"),
    ("m", "60"),
    ("h", "3600"),
    ("day", "86400"),
    ("days", "86400"),
    ("year", "31536000"),
    ("years", "31536000"),
];
/// Sizes in bytes (decimal: 1 KB = 1000 B).
pub const SIZE_UNITS: &[(&str, &str)] =
    &[("B", "1"), ("KB", "1000"), ("MB", "1000000"), ("GB", "1000000000"), ("TB", "1000000000000")];
pub const LENGTH_UNITS: &[(&str, &str)] = &[("km", "1")];
/// ISO 4217 codes and their minor units.
pub const CURRENCIES: &[(&str, u32)] = &[
    ("EUR", 2),
    ("USD", 2),
    ("GBP", 2),
    ("JPY", 0),
    ("CHF", 2),
    ("CAD", 2),
    ("AUD", 2),
    ("CNY", 2),
    ("MXN", 2),
    ("ARS", 2),
    ("BRL", 2),
    ("CLP", 0),
    ("COP", 2),
    ("PEN", 2),
    ("UYU", 2),
    ("SEK", 2),
    ("NOK", 2),
    ("DKK", 2),
    ("PLN", 2),
    ("INR", 2),
];
/// Single-letter units and % must be attached to the number (D3).
const ATTACHED_ONLY: &[&str] = &["s", "m", "h", "B", "%"];

pub fn is_unit(u: &str) -> bool {
    u == "%"
        || TIME_UNITS.iter().any(|(n, _)| *n == u)
        || SIZE_UNITS.iter().any(|(n, _)| *n == u)
        || LENGTH_UNITS.iter().any(|(n, _)| *n == u)
}

pub fn is_currency(u: &str) -> bool {
    CURRENCIES.iter().any(|(n, _)| *n == u)
}

pub fn is_keyword(w: &str) -> bool {
    KEYWORDS.contains(&w)
}

const OPERATORS: &[&str] = &[
    "|>", "->", "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "**", "//", "..", "??", "+", "-", "*", "/",
    "%", "<", ">", "=", "(", ")", "[", "]", "{", "}", ",", ":", ".", "?", "!", "@", "|",
];

fn closer(open: &str) -> &'static str {
    match open {
        "(" => ")",
        "[" => "]",
        _ => "}",
    }
}

fn opener(close: &str) -> &'static str {
    match close {
        ")" => "(",
        "]" => "[",
        _ => "{",
    }
}

/// A piece of a text literal: literal characters or an `{expr}`.
#[derive(Clone, Debug, PartialEq)]
pub enum StrPart {
    Lit(String),
    Interp { source: String, line: u32, col: u32 },
}

#[derive(Clone, Debug, PartialEq)]
pub enum Tok {
    Name(String),
    Keyword(&'static str),
    Int(String),
    Float(String),
    Unit { text: String, is_float: bool, unit: String },
    Str(Vec<StrPart>),
    Op(&'static str),
    Newline,
    Indent,
    Dedent,
    Eof,
}

#[derive(Clone, Debug)]
pub struct Token {
    pub tok: Tok,
    pub span: Span,
}

impl Token {
    pub fn is_op(&self, op: &str) -> bool {
        matches!(&self.tok, Tok::Op(o) if *o == op)
    }

    pub fn is_kw(&self, kw: &str) -> bool {
        matches!(&self.tok, Tok::Keyword(k) if *k == kw)
    }
}

fn is_ident_start(c: char) -> bool {
    c.is_alphabetic() || c == '_'
}

fn is_ident_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

pub struct Lexer {
    src: Vec<char>,
    i: usize,
    line: u32,
    /// index of the first character of the current line (can be negative
    /// when lexing an interpolated expression that starts mid-line)
    line_start: i64,
    expr_mode: bool,
    tokens: Vec<Token>,
    indents: Vec<usize>,
    brackets: Vec<Token>,
}

type LexResult<T> = Result<T, Diagnostic>;

impl Lexer {
    pub fn new(source: &str, line: u32, col: u32, expr_mode: bool) -> Lexer {
        Lexer {
            src: source.chars().collect(),
            i: 0,
            line,
            line_start: -(col as i64 - 1),
            expr_mode,
            tokens: Vec::new(),
            indents: vec![0],
            brackets: Vec::new(),
        }
    }

    fn col(&self, i: usize) -> u32 {
        (i as i64 - self.line_start + 1) as u32
    }

    fn span(&self, start: usize, end: usize) -> Span {
        Span::new(self.line, self.col(start), self.col(end))
    }

    fn span_from(&self, start: usize) -> Span {
        self.span(start, self.i)
    }

    fn at(&self, j: usize) -> Option<char> {
        self.src.get(j).copied()
    }

    fn starts_with(&self, j: usize, s: &str) -> bool {
        let mut k = j;
        for c in s.chars() {
            if self.at(k) != Some(c) {
                return false;
            }
            k += 1;
        }
        true
    }

    fn add(&mut self, tok: Tok, span: Span) {
        self.tokens.push(Token { tok, span });
    }

    fn word_end(&self, mut j: usize) -> usize {
        while j < self.src.len() && is_ident_char(self.src[j]) {
            j += 1;
        }
        j
    }

    fn text(&self, a: usize, b: usize) -> String {
        self.src[a..b].iter().collect()
    }

    pub fn tokenize(mut self) -> LexResult<Vec<Token>> {
        let mut at_line_start = !self.expr_mode;
        while self.i < self.src.len() {
            if at_line_start {
                at_line_start = false;
                if self.brackets.is_empty() {
                    if self.indentation()? {
                        at_line_start = true;
                    }
                    continue;
                }
            }
            let c = self.src[self.i];
            if c == '\n' {
                if self.brackets.is_empty() && !self.expr_mode {
                    self.newline_token();
                }
                self.i += 1;
                self.line += 1;
                self.line_start = self.i as i64;
                at_line_start = true;
                continue;
            }
            if c == ' ' || c == '\r' {
                self.i += 1;
                continue;
            }
            if c == '\t' {
                return Err(Diagnostic::new("tab", self.span(self.i, self.i + 1)));
            }
            if c == '#' {
                while self.i < self.src.len() && self.src[self.i] != '\n' {
                    self.i += 1;
                }
                continue;
            }
            if c.is_ascii_digit() {
                self.number()?;
                continue;
            }
            if is_ident_start(c) {
                let start = self.i;
                self.i = self.word_end(self.i);
                let word = self.text(start, self.i);
                let span = self.span_from(start);
                match KEYWORDS.iter().find(|k| **k == word) {
                    Some(k) => self.add(Tok::Keyword(k), span),
                    None => self.add(Tok::Name(word), span),
                }
                continue;
            }
            if c == '"' {
                self.string()?;
                continue;
            }
            self.operator()?;
        }
        if let Some(tok) = self.brackets.last() {
            let Tok::Op(open) = tok.tok else { unreachable!() };
            return Err(Diagnostic::new("unclosed_bracket", tok.span).arg("open", open).arg("close", closer(open)));
        }
        if !self.expr_mode {
            self.newline_token();
            let end = self.span_from(self.i);
            while self.indents.len() > 1 {
                self.indents.pop();
                self.add(Tok::Dedent, end);
            }
        }
        let end = self.span_from(self.i);
        self.add(Tok::Eof, end);
        Ok(self.tokens)
    }

    fn newline_token(&mut self) {
        if let Some(last) = self.tokens.last() {
            if !matches!(last.tok, Tok::Newline | Tok::Indent | Tok::Dedent) {
                let span = self.span(self.i, self.i + 1);
                self.add(Tok::Newline, span);
            }
        }
    }

    /// Process the indentation of a line. Returns true if the line was blank
    /// or a comment (so the caller should start over on the next one).
    fn indentation(&mut self) -> LexResult<bool> {
        let n = self.src.len();
        let mut j = self.i;
        let mut tab = None;
        while j < n && matches!(self.src[j], ' ' | '\t' | '\r') {
            if self.src[j] == '\t' && tab.is_none() {
                tab = Some(j);
            }
            j += 1;
        }
        if j >= n || self.src[j] == '\n' || self.src[j] == '#' {
            while j < n && self.src[j] != '\n' {
                j += 1;
            }
            self.i = j;
            if j < n {
                self.i += 1;
                self.line += 1;
                self.line_start = self.i as i64;
            }
            return Ok(true);
        }
        let first = self.span(j, self.word_end(j).max(j + 1));
        if tab.is_some() {
            return Err(Diagnostic::new("indent_tab", first));
        }
        let count = j - self.i;
        self.i = j;
        if self.starts_with(j, "|>") {
            // continuation line: the statement goes on
            if matches!(self.tokens.last(), Some(t) if t.tok == Tok::Newline) {
                self.tokens.pop();
            }
            return Ok(false);
        }
        let current = *self.indents.last().unwrap();
        if count > current {
            if count != current + 4 {
                let len = self.tokens.len();
                let opens_block = len >= 2 && self.tokens[len - 1].tok == Tok::Newline && self.tokens[len - 2].is_op(":");
                let expected = if opens_block { current + 4 } else { current };
                return Err(Diagnostic::new("indent_size", first)
                    .arg("n", count.to_string())
                    .arg("expected", expected.to_string()));
            }
            self.indents.push(count);
            self.add(Tok::Indent, first);
        } else if count < current {
            while *self.indents.last().unwrap() > count {
                self.indents.pop();
                self.add(Tok::Dedent, first);
            }
            if *self.indents.last().unwrap() != count {
                return Err(Diagnostic::new("dedent_mismatch", first));
            }
        }
        Ok(false)
    }

    fn number(&mut self) -> LexResult<()> {
        let n = self.src.len();
        let start = self.i;
        let mut is_float = false;
        let mut j = self.i;
        let digit_or_us = |c: char| c.is_ascii_digit() || c == '_';
        while j < n && digit_or_us(self.src[j]) {
            j += 1;
        }
        if j + 1 < n && self.src[j] == '.' && self.src[j + 1].is_ascii_digit() {
            is_float = true;
            j += 1;
            while j < n && digit_or_us(self.src[j]) {
                j += 1;
            }
        }
        if j < n && (self.src[j] == 'e' || self.src[j] == 'E') {
            let mut k = j + 1;
            if k < n && (self.src[k] == '+' || self.src[k] == '-') {
                k += 1;
            }
            if k < n && self.src[k].is_ascii_digit() {
                is_float = true;
                j = k;
                while j < n && self.src[j].is_ascii_digit() {
                    j += 1;
                }
            }
        }
        let raw = self.text(start, j);
        if raw.ends_with('_') || raw.contains("__") || raw.contains("_.") || raw.contains("._") {
            return Err(Diagnostic::new("bad_number", self.span(start, j)).arg("text", raw));
        }
        let text = raw.replace('_', "");
        self.i = j;
        // unit attached to the number: 30s, 10%, 300ms, 5km
        if j < n && (self.src[j] == '%' || is_ident_start(self.src[j])) {
            let (unit, end) = if self.src[j] == '%' {
                ("%".to_string(), j + 1)
            } else {
                let end = self.word_end(j);
                (self.text(j, end), end)
            };
            if !is_unit(&unit) && !is_currency(&unit) {
                return Err(Diagnostic::new("unknown_unit", self.span(j, end)).arg("unit", unit));
            }
            self.i = end;
            let span = self.span_from(start);
            self.add(Tok::Unit { text, is_float, unit }, span);
            return Ok(());
        }
        // unit after a space: 5 km, 2 days, 19.99 EUR
        let mut k = j;
        while k < n && self.src[k] == ' ' {
            k += 1;
        }
        if k > j && k < n && is_ident_start(self.src[k]) {
            let end = self.word_end(k);
            let unit = self.text(k, end);
            if (is_unit(&unit) && !ATTACHED_ONLY.contains(&unit.as_str())) || is_currency(&unit) {
                self.i = end;
                let span = self.span_from(start);
                self.add(Tok::Unit { text, is_float, unit }, span);
                return Ok(());
            }
        }
        let span = self.span_from(start);
        self.add(if is_float { Tok::Float(text) } else { Tok::Int(text) }, span);
        Ok(())
    }

    fn find(&self, from: usize, s: &str) -> Option<usize> {
        (from..self.src.len()).find(|&j| self.starts_with(j, s))
    }

    fn string(&mut self) -> LexResult<()> {
        let n = self.src.len();
        let start = self.i;
        let start_line = self.line;
        let start_col = self.col(self.i);
        if self.starts_with(self.i, "\"\"\"") {
            let Some(end) = self.find(self.i + 3, "\"\"\"") else {
                return Err(Diagnostic::new("unterminated_string", self.span(start, start + 3)).arg("quote", "\"\"\""));
            };
            let raw = self.text(self.i + 3, end);
            let parts = self.multiline_parts(&raw, start_line, start_col + 3)?;
            let newlines = raw.matches('\n').count() as u32;
            self.i = end + 3;
            let span = Span::new(start_line, start_col, if newlines > 0 { start_col + 3 } else { self.col(self.i) });
            if newlines > 0 {
                self.line += newlines;
                let last_nl = (0..end).rev().find(|&j| self.src[j] == '\n').unwrap();
                self.line_start = last_nl as i64 + 1;
            }
            self.add(Tok::Str(parts), span);
            return Ok(());
        }
        let mut j = self.i + 1;
        loop {
            if j >= n || self.src[j] == '\n' {
                return Err(Diagnostic::new("unterminated_string", self.span(start, j)).arg("quote", "\""));
            }
            if self.src[j] == '\\' {
                j += 2;
                continue;
            }
            if self.starts_with(j, "{{") {
                j += 2;
                continue;
            }
            if self.src[j] == '{' {
                j = self.skip_interp(j, start)?;
                continue;
            }
            if self.src[j] == '"' {
                break;
            }
            j += 1;
        }
        let raw = self.text(self.i + 1, j);
        let parts = self.parts(&raw, start_line, start_col + 1)?;
        self.i = j + 1;
        let span = self.span_from(start);
        self.add(Tok::Str(parts), span);
        Ok(())
    }

    /// j is at '{'; return the index after the matching '}'.
    fn skip_interp(&self, mut j: usize, start: usize) -> LexResult<usize> {
        let n = self.src.len();
        let mut depth = 0;
        while j < n && self.src[j] != '\n' {
            let c = self.src[j];
            if c == '"' {
                j += 1;
                while j < n && self.src[j] != '"' && self.src[j] != '\n' {
                    j += if self.src[j] == '\\' { 2 } else { 1 };
                }
            } else if c == '{' {
                depth += 1;
            } else if c == '}' {
                depth -= 1;
                if depth == 0 {
                    return Ok(j + 1);
                }
            }
            j += 1;
        }
        Err(Diagnostic::new("unterminated_string", self.span(start, j)).arg("quote", "\""))
    }

    fn multiline_parts(&self, raw: &str, line: u32, col: u32) -> LexResult<Vec<StrPart>> {
        let mut lines: Vec<&str> = raw.split('\n').collect();
        let mut positions: Vec<(u32, u32)> =
            (0..lines.len()).map(|n| (line + n as u32, if n == 0 { col } else { 1 })).collect();
        if lines.len() > 1 && lines[0].trim().is_empty() {
            lines.remove(0);
            positions.remove(0);
        }
        if lines.len() > 1 && lines[lines.len() - 1].trim().is_empty() {
            lines.pop();
            positions.pop();
        }
        for l in &lines {
            let lead: String = l.chars().take_while(|c| c.is_whitespace()).collect();
            if lead.contains('\t') {
                return Err(Diagnostic::new("tab", Span::new(positions[0].0, 1, 2)));
            }
        }
        let widths: Vec<usize> = lines
            .iter()
            .filter(|l| !l.trim().is_empty())
            .map(|l| l.chars().take_while(|c| *c == ' ').count())
            .collect();
        let cut = widths.iter().copied().min().unwrap_or(0);
        let mut parts = Vec::new();
        for (n, text) in lines.iter().enumerate() {
            if n > 0 {
                parts.push(StrPart::Lit("\n".into()));
            }
            let (ln, c) = positions[n];
            let rest: String = text.chars().skip(cut).collect();
            parts.extend(self.parts(&rest, ln, c + cut as u32)?);
        }
        Ok(merge_parts(parts))
    }

    /// Split text content into literal strings and interpolated pieces.
    fn parts(&self, raw: &str, line: u32, col: u32) -> LexResult<Vec<StrPart>> {
        let raw: Vec<char> = raw.chars().collect();
        let mut parts = Vec::new();
        let mut buf = String::new();
        let mut j = 0;
        while j < raw.len() {
            let c = raw[j];
            if c == '\\' && j + 1 < raw.len() {
                let esc = match raw[j + 1] {
                    'n' => Some('\n'),
                    't' => Some('\t'),
                    '\\' => Some('\\'),
                    '"' => Some('"'),
                    'r' => Some('\r'),
                    _ => None,
                };
                if let Some(e) = esc {
                    buf.push(e);
                    j += 2;
                    continue;
                }
            }
            if c == '{' {
                if j + 1 < raw.len() && raw[j + 1] == '{' {
                    buf.push('{');
                    j += 2;
                    continue;
                }
                let mut depth = 0;
                let mut k = j;
                while k < raw.len() {
                    if raw[k] == '"' {
                        k += 1;
                        while k < raw.len() && raw[k] != '"' {
                            k += if raw[k] == '\\' { 2 } else { 1 };
                        }
                    } else if raw[k] == '{' {
                        depth += 1;
                    } else if raw[k] == '}' {
                        depth -= 1;
                        if depth == 0 {
                            break;
                        }
                    }
                    k += 1;
                }
                let at = col + j as u32;
                if k >= raw.len() {
                    return Err(Diagnostic::new("unclosed_bracket", Span::new(line, at, at + 1))
                        .arg("open", "{")
                        .arg("close", "}"));
                }
                let inner: String = raw[j + 1..k].iter().collect();
                if inner.trim().is_empty() {
                    return Err(Diagnostic::new("empty_interp", Span::new(line, at, col + k as u32 + 1)));
                }
                if !buf.is_empty() {
                    parts.push(StrPart::Lit(std::mem::take(&mut buf)));
                }
                parts.push(StrPart::Interp { source: inner, line, col: at + 1 });
                j = k + 1;
                continue;
            }
            buf.push(c);
            j += 1;
        }
        if !buf.is_empty() {
            parts.push(StrPart::Lit(buf));
        }
        Ok(parts)
    }

    fn operator(&mut self) -> LexResult<()> {
        for op in OPERATORS {
            if self.starts_with(self.i, op) {
                let start = self.i;
                self.i += op.chars().count();
                let span = self.span_from(start);
                if matches!(*op, "(" | "[" | "{") {
                    self.brackets.push(Token { tok: Tok::Op(op), span });
                } else if matches!(*op, ")" | "]" | "}") {
                    let matches = matches!(self.brackets.last(), Some(t) if t.tok == Tok::Op(opener(op)));
                    if !matches {
                        return Err(Diagnostic::new("unmatched_bracket", span).arg("ch", *op));
                    }
                    self.brackets.pop();
                }
                self.add(Tok::Op(op), span);
                return Ok(());
            }
        }
        let ch = self.src[self.i].to_string();
        Err(Diagnostic::new("bad_char", self.span(self.i, self.i + 1)).arg("ch", ch))
    }
}

fn merge_parts(parts: Vec<StrPart>) -> Vec<StrPart> {
    let mut out: Vec<StrPart> = Vec::new();
    for p in parts {
        if let (StrPart::Lit(s), Some(StrPart::Lit(prev))) = (&p, out.last_mut()) {
            prev.push_str(s);
            continue;
        }
        out.push(p);
    }
    out
}

pub fn tokenize(source: &str) -> LexResult<Vec<Token>> {
    Lexer::new(source, 1, 1, false).tokenize()
}

/// Tokens of an interpolated expression (no NEWLINE/INDENT handling).
pub fn tokenize_expr(source: &str, line: u32, col: u32) -> LexResult<Vec<Token>> {
    Lexer::new(source, line, col, true).tokenize()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kinds(src: &str) -> Vec<Tok> {
        tokenize(src).unwrap().into_iter().map(|t| t.tok).collect()
    }

    #[test]
    fn indentation_produces_indent_and_dedent() {
        let toks = kinds("if x:\n    y\nz\n");
        assert_eq!(
            toks,
            vec![
                Tok::Keyword("if"),
                Tok::Name("x".into()),
                Tok::Op(":"),
                Tok::Newline,
                Tok::Indent,
                Tok::Name("y".into()),
                Tok::Newline,
                Tok::Dedent,
                Tok::Name("z".into()),
                Tok::Newline,
                Tok::Eof
            ]
        );
    }

    #[test]
    fn unit_literals() {
        assert_eq!(kinds("30s")[0], Tok::Unit { text: "30".into(), is_float: false, unit: "s".into() });
        assert_eq!(kinds("19.99 EUR")[0], Tok::Unit { text: "19.99".into(), is_float: true, unit: "EUR".into() });
        assert_eq!(kinds("10%")[0], Tok::Unit { text: "10".into(), is_float: false, unit: "%".into() });
        assert_eq!(kinds("10 % 3")[1], Tok::Op("%"));
        assert_eq!(kinds("x = 5 s")[2], Tok::Int("5".into()));
        assert_eq!(tokenize("3xyz").unwrap_err().key, "unknown_unit");
    }

    #[test]
    fn numbers_with_underscores_and_exponents() {
        assert_eq!(kinds("1_000")[0], Tok::Int("1000".into()));
        assert_eq!(kinds("1e-3")[0], Tok::Float("1e-3".into()));
        assert_eq!(tokenize("1__0").unwrap_err().key, "bad_number");
    }

    #[test]
    fn strings_with_interpolation() {
        let toks = kinds("\"a{{b} {x + 1}\"");
        assert_eq!(
            toks[0],
            Tok::Str(vec![StrPart::Lit("a{b} ".into()), StrPart::Interp { source: "x + 1".into(), line: 1, col: 9 }])
        );
        assert_eq!(tokenize("\"{}\"").unwrap_err().key, "empty_interp");
    }

    #[test]
    fn multiline_text_drops_common_indentation() {
        let toks = kinds("t = \"\"\"\n    Several lines\n    with {name}\n\"\"\"\n");
        assert_eq!(
            toks[2],
            Tok::Str(vec![
                StrPart::Lit("Several lines\nwith ".into()),
                StrPart::Interp { source: "name".into(), line: 3, col: 11 }
            ])
        );
    }

    #[test]
    fn indentation_errors() {
        let e = tokenize("if true:\n  print(1)\n").unwrap_err();
        assert_eq!(e.key, "indent_size");
        assert_eq!(e.arg_value("expected"), Some("4"));
        assert_eq!(tokenize("if true:\n\tprint(1)\n").unwrap_err().key, "indent_tab");
        assert_eq!(tokenize("x = (1\n").unwrap_err().key, "unclosed_bracket");
        assert_eq!(tokenize("x = 1)\n").unwrap_err().key, "unmatched_bracket");
    }

    #[test]
    fn newlines_inside_brackets_and_pipelines_continue() {
        let toks = kinds("x = [1,\n    2]\ny = a\n  |> f\n");
        assert_eq!(toks.iter().filter(|t| **t == Tok::Newline).count(), 2);
        assert!(!toks.contains(&Tok::Indent));
    }

    #[test]
    fn unicode_identifiers() {
        assert_eq!(kinds("nombre_año")[0], Tok::Name("nombre_año".into()));
        let t = tokenize("x = \"ñ\" + y").unwrap();
        assert_eq!(t[4].span, Span::new(1, 11, 12));
    }
}
