//! `nexo lsp` (section 14, D87): a language server for any editor, over
//! standard input and output. The same answers as phase 0 (`nexo/lsp.py`):
//! diagnostics (the compile error or the warnings of `nexo lint`) with
//! their quick fixes, "apply the safe fixes", formatting, go to
//! definition, hover, the outline of the file and completion.

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, Read, Write};

use nexo_diag::{has, msg, Span};

use crate::ast::*;
use crate::json::{n, obj, s, Json};
use crate::lexer::{Tok, Token, KEYWORDS};
use crate::lint::Edit;

const BUILTINS: &[&str] = &[
    "print", "input", "len", "range", "set", "round", "split", "convert", "fail", "validate", "PI", "half_up", "half_even",
    "int", "float", "decimal", "money", "str", "bool", "list", "map", "Error", "NotFound", "IOError", "InvalidData", "Overflow",
    "DivisionByZero", "OutOfRange", "ContractBroken", "Timeout", "InvalidValue", "read", "write", "read_lines", "save",
    "json", "csv",
    // the library of phase 4 (D89)
    "move_file", "now", "today", "date", "time", "regex", "render", "secret", "password", "key", "keypair", "encrypt",
    "decrypt", "encrypt_file", "decrypt_file", "sign", "verify", "log", "http", "jwt", "ask", "email", "NetworkError",
    "HTTPError", "AIError", "DatabaseError",
];
/// The functions of phase 4 among BUILTINS (the others are values).
const LIBRARY_FUNCTIONS: &[&str] = &[
    "move_file", "date", "time", "regex", "render", "secret", "encrypt", "decrypt", "encrypt_file", "decrypt_file", "sign",
    "verify", "log", "jwt", "ask",
];
const MEMBERS: &[&str] = &[
    "add", "cause", "close", "count", "ends_with", "keys", "kind", "line", "lower", "message", "parse", "receive", "replace",
    "send", "split", "starts_with", "sum", "trim", "upper", "values",
    // members of the values of phase 4
    "all", "find", "handle", "headers", "remove", "status", "text", "url",
];

fn symbol_kind(kind: &str) -> u32 {
    match kind {
        "function" => 12,
        "method" => 6,
        "struct" => 23,
        "enum" => 10,
        "field" => 8,
        "member" => 22,
        "interface" => 11,
        "constant" => 14,
        _ => 13,
    }
}

fn completion_kind(kind: &str) -> u32 {
    match kind {
        "function" => 3,
        "method" => 2,
        "field" => 5,
        "constant" => 21,
        "type" => 7,
        "member" => 20,
        "interface" => 8,
        "keyword" => 14,
        _ => 6,
    }
}

// ------------------------------------------------------------ positions

fn lines_of(text: &str) -> Vec<Vec<char>> {
    text.split('\n').map(|l| l.chars().collect()).collect()
}

/// LSP character offset (UTF-16) of the 1-based column `col` of a line.
fn utf16_col(line: &[char], col: usize) -> usize {
    line[..(col - 1).min(line.len())].iter().map(|c| c.len_utf16()).sum()
}

/// 1-based column of an LSP character offset in a line.
fn from_utf16(line: &[char], character: usize) -> usize {
    let mut units = 0;
    for (i, c) in line.iter().enumerate() {
        if units >= character {
            return i + 1;
        }
        units += c.len_utf16();
    }
    line.len() + 1
}

fn lsp_range(lines: &[Vec<char>], line: usize, col: usize, end_line: usize, end_col: usize) -> Json {
    let pos = |l: usize, c: usize| -> Json {
        if l < 1 {
            return obj(vec![("line", n(0)), ("character", n(0))]);
        }
        if l > lines.len() {
            let last = lines.last().unwrap();
            return obj(vec![("line", n((lines.len() - 1) as u32)), ("character", n(utf16_col(last, last.len() + 1) as u32))]);
        }
        let text = &lines[l - 1];
        let c = c.clamp(1, text.len() + 1);
        obj(vec![("line", n((l - 1) as u32)), ("character", n(utf16_col(text, c) as u32))])
    };
    obj(vec![("start", pos(line, col)), ("end", pos(end_line, end_col))])
}

fn span_range(lines: &[Vec<char>], span: Option<Span>) -> Json {
    match span {
        None => lsp_range(lines, 1, 1, 1, 1),
        Some(sp) => lsp_range(lines, sp.line as usize, sp.col as usize, sp.line as usize, sp.end_col as usize),
    }
}

fn edit_json(lines: &[Vec<char>], e: &Edit) -> Json {
    obj(vec![
        ("range", lsp_range(lines, e.line as usize, e.col as usize, e.end_line as usize, e.end_col as usize)),
        ("newText", s(e.text.clone())),
    ])
}

fn fix_json(lines: &[Vec<char>], title: String, fix: &crate::lint::Fix) -> Json {
    obj(vec![
        ("title", s(title)),
        ("safe", Json::Bool(fix.safe)),
        ("edits", Json::Arr(fix.edits.iter().map(|e| edit_json(lines, e)).collect())),
    ])
}

// ----------------------------------------------------------- diagnostics

/// The file name of a document, as phase 0's lsp.py gives it.
fn name_of(uri: &str) -> &str {
    uri.rsplit('/').next().unwrap_or(uri)
}

fn diagnostics(text: &str, name: &str) -> Vec<Json> {
    let lines = lines_of(text);
    let diag = |range: Json, severity: u32, code: &str, message: String, fixes: Vec<Json>| {
        obj(vec![
            ("range", range),
            ("severity", n(severity)),
            ("source", s("nexo")),
            ("code", s(code)),
            ("message", s(message)),
            ("data", obj(vec![("fixes", Json::Arr(fixes))])),
        ])
    };
    match crate::lint_source(text, name) {
        Err(d) => {
            let mut fixes = Vec::new();
            if let Some(fix) = crate::lint::error_fix(&d, text) {
                let key = format!("fix.{}", d.fix.unwrap_or(d.key));
                let title = if has(&key) { msg(&key, &d.args) } else { d.text() };
                fixes.push(fix_json(&lines, title, &fix));
            }
            vec![diag(span_range(&lines, d.span), 1, d.key, d.text(), fixes)]
        }
        Ok(findings) => findings
            .iter()
            .map(|f| {
                let mut fixes = Vec::new();
                if let Some(fix) = &f.fix {
                    fixes.push(fix_json(&lines, msg(&format!("fix.lint.{}", f.key), &f.args), fix));
                }
                diag(span_range(&lines, Some(f.span)), 2, f.key, f.text(), fixes)
            })
            .collect(),
    }
}

fn whole_document(text: &str, new_text: String) -> Json {
    let lines = lines_of(text);
    let last = lines.last().map(|l| l.len()).unwrap_or(0);
    obj(vec![("range", lsp_range(&lines, 1, 1, lines.len(), last + 1)), ("newText", s(new_text))])
}

fn formatting(text: &str) -> Json {
    match crate::fmt::format_source(text) {
        Err(_) => Json::Null,
        Ok(f) if f == text => Json::Arr(vec![]),
        Ok(f) => Json::Arr(vec![whole_document(text, f)]),
    }
}

fn code_actions(text: &str, uri: &str, params: &Json) -> Json {
    let context = params.get("context");
    let only: Vec<&str> = context.get("only").arr().iter().filter_map(|k| k.str()).collect();
    let mut actions = Vec::new();
    if only.is_empty() || only.iter().any(|k| k.starts_with("quickfix")) {
        for d in context.get("diagnostics").arr() {
            for fix in d.get("data").get("fixes").arr() {
                let safe = matches!(fix.get("safe"), Json::Bool(true));
                actions.push(obj(vec![
                    ("title", fix.get("title").clone()),
                    ("kind", s("quickfix")),
                    ("diagnostics", Json::Arr(vec![d.clone()])),
                    ("isPreferred", Json::Bool(safe)),
                    ("edit", obj(vec![("changes", obj(vec![(uri, fix.get("edits").clone())]))])),
                ]));
            }
        }
    }
    if only.is_empty() || only.iter().any(|k| k.starts_with("source")) {
        let parses = crate::parser::parse(text).is_ok();
        if parses {
            let fixed = crate::fix_source(text.to_string(), name_of(uri));
            if fixed != text {
                actions.push(obj(vec![
                    ("title", s(msg("lsp_fix_all", &[]))),
                    ("kind", s("source.fixAll.nexo")),
                    ("edit", obj(vec![("changes", obj(vec![(uri, Json::Arr(vec![whole_document(text, fixed)]))]))])),
                ]));
            }
        }
    }
    Json::Arr(actions)
}

// ---------------------------------------------------------------- index

#[derive(Clone)]
struct Decl {
    name: String,
    kind: &'static str,
    line: usize,
    col: usize,
    end_col: usize,
    end_line: usize,
    scope: Option<(usize, usize)>,
    detail: String,
    doc: String,
    children: Vec<usize>,
    member: bool,
}

/// Declarations of a file, for definition, hover, outline and completion.
struct Index {
    lines: Vec<Vec<char>>,
    tokens: Vec<Token>,
    decls: Vec<Decl>,
    top: Vec<usize>,
    seen: HashSet<(String, Option<(usize, usize)>, bool)>,
}

fn is_ident(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

fn param_type_text(t: &TypeExpr) -> String {
    let mut out = crate::checker::type_text(t);
    if t.fallible() {
        out.push('!');
    }
    out
}

impl Index {
    fn new(text: &str) -> Option<Index> {
        let tokens = crate::lexer::tokenize(text).ok()?;
        let program = crate::parser::parse(text).ok()?;
        let mut idx = Index { lines: lines_of(text), tokens, decls: Vec::new(), top: Vec::new(), seen: HashSet::new() };
        let mut top = Vec::new();
        idx.body(&program.body, None, Some(&mut top));
        idx.top = top;
        Some(idx)
    }

    fn real(t: &Token) -> bool {
        !matches!(t.tok, Tok::Newline | Tok::Indent | Tok::Dedent | Tok::Eof)
    }

    fn line_tokens(&self, line: usize) -> Vec<&Token> {
        self.tokens.iter().filter(|t| t.span.line as usize == line && Self::real(t)).collect()
    }

    fn slice(&self, line: usize, a: usize, b: usize) -> String {
        let text = &self.lines[line - 1];
        let a = (a - 1).min(text.len());
        let b = (b - 1).min(text.len()).max(a);
        text[a..b].iter().collect::<String>().trim_end().to_string()
    }

    /// The code of a definition line, up to the `:` that opens its block.
    fn header(&self, line: usize) -> String {
        let toks = self.line_tokens(line);
        let (Some(first), Some(last)) = (toks.first(), toks.last()) else { return String::new() };
        if last.is_op(":") {
            return self.slice(line, first.span.col as usize, last.span.col as usize);
        }
        self.slice(line, first.span.col as usize, last.span.end_col as usize)
    }

    fn code(&self, line: usize) -> String {
        let toks = self.line_tokens(line);
        let (Some(first), Some(last)) = (toks.first(), toks.last()) else { return String::new() };
        self.slice(line, first.span.col as usize, last.span.end_col as usize)
    }

    fn doc(&self, line: usize) -> String {
        let mut found = Vec::new();
        let mut k = line as i64 - 1;
        while k >= 1 {
            let text: String = self.lines[k as usize - 1].iter().collect();
            let t = text.trim();
            if !t.starts_with("##") {
                break;
            }
            found.push(t[2..].trim().to_string());
            k -= 1;
        }
        found.reverse();
        found.join("\n")
    }

    /// Last line of the statement at (line, col), with its block.
    fn block_end(&self, line: usize, col: usize) -> usize {
        let toks = &self.tokens;
        let mut k = 0;
        while k < toks.len()
            && ((toks[k].span.line as usize, toks[k].span.col as usize) < (line, col)
                || matches!(toks[k].tok, Tok::Indent | Tok::Dedent | Tok::Newline))
        {
            k += 1;
        }
        let mut depth = 0i32;
        let mut last = line;
        while k < toks.len() {
            match toks[k].tok {
                Tok::Indent => depth += 1,
                Tok::Dedent => {
                    depth -= 1;
                    if depth <= 0 {
                        break;
                    }
                }
                Tok::Eof => break,
                Tok::Newline => {
                    if depth == 0 && !(k + 1 < toks.len() && matches!(toks[k + 1].tok, Tok::Indent)) {
                        break;
                    }
                }
                _ => last = toks[k].span.line as usize,
            }
            k += 1;
        }
        last
    }

    fn name_after(&self, line: usize, col: usize) -> Option<Span> {
        self.tokens
            .iter()
            .find(|t| (t.span.line as usize, t.span.col as usize) > (line, col) && matches!(t.tok, Tok::Name(_)))
            .map(|t| t.span)
    }

    #[allow(clippy::too_many_arguments)]
    fn decl(&self, name: &str, kind: &'static str, span: Span, end_col: usize, end_line: usize, scope: Option<(usize, usize)>, detail: String, doc: String) -> Decl {
        Decl {
            name: name.to_string(),
            kind,
            line: span.line as usize,
            col: span.col as usize,
            end_col,
            end_line,
            scope,
            detail,
            doc,
            children: Vec::new(),
            member: false,
        }
    }

    fn add(&mut self, d: Decl, into: Option<&mut Vec<usize>>) -> Option<usize> {
        let var = d.kind == "variable" || d.kind == "constant";
        let key = (d.name.clone(), d.scope, var);
        if var && self.seen.contains(&key) {
            return None;
        }
        self.seen.insert(key);
        self.decls.push(d);
        let i = self.decls.len() - 1;
        if let Some(v) = into {
            v.push(i);
        }
        Some(i)
    }

    fn func(&mut self, f: &FnDef, scope: Option<(usize, usize)>, into: Option<&mut Vec<usize>>, kind: &'static str) {
        let line = f.span.line as usize;
        let end = self.block_end(line, f.span.col as usize);
        let mut d = self.decl(&f.name, kind, f.name_span, f.name_span.end_col as usize, end, scope, self.header(line), self.doc(line));
        d.member = kind == "method";
        self.add(d, into);
        let inner = Some((line, end));
        for p in &f.params {
            let detail = match &p.ty {
                Some(t) => format!("{}: {}", p.name, param_type_text(t)),
                None => p.name.clone(),
            };
            let d = self.decl(&p.name, "param", p.span, p.span.end_col as usize, p.span.line as usize, inner, detail, String::new());
            self.add(d, None);
        }
        self.body(&f.body, inner, None);
    }

    fn body(&mut self, body: &[Stmt], scope: Option<(usize, usize)>, mut into: Option<&mut Vec<usize>>) {
        for st in body {
            self.stmt(st, scope, into.as_deref_mut());
        }
    }

    fn stmt(&mut self, st: &Stmt, scope: Option<(usize, usize)>, into: Option<&mut Vec<usize>>) {
        let (line, col) = (st.span.line as usize, st.span.col as usize);
        match &st.kind {
            StmtKind::Fn(f) => self.func(f, scope, into, "function"),
            StmtKind::Type(t) => {
                let tl = t.span.line as usize;
                let end = self.block_end(tl, t.span.col as usize);
                let kind = if t.fields.is_empty() { "enum" } else { "struct" };
                let d = self.decl(&t.name, kind, t.name_span, t.name_span.end_col as usize, end, scope, self.header(tl), self.doc(tl));
                let Some(i) = self.add(d, into) else { return };
                let mut children = Vec::new();
                for f in &t.fields {
                    let fl = f.span.line as usize;
                    let mut d = self.decl(&f.name, "field", f.span, f.span.end_col as usize, fl, scope, self.code(fl), self.doc(fl));
                    d.member = true;
                    self.add(d, Some(&mut children));
                }
                for v in &t.variants {
                    let vl = v.span.line as usize;
                    let end_col = v.span.col as usize + v.name.chars().count();
                    let d = self.decl(&v.name, "member", v.span, end_col, vl, scope, self.code(vl), self.doc(vl));
                    self.add(d, Some(&mut children));
                }
                for m in &t.methods {
                    self.func(m, scope, Some(&mut children), "method");
                }
                self.decls[i].children = children;
            }
            StmtKind::Trait { name, .. } => {
                if let Some(sp) = self.name_after(line, col) {
                    let end = self.block_end(line, col);
                    let d = self.decl(name, "interface", sp, sp.end_col as usize, end, scope, self.header(line), self.doc(line));
                    self.add(d, into);
                }
            }
            StmtKind::Test { body, .. } => {
                let end = self.block_end(line, col);
                self.body(body, Some((line, end)), None);
            }
            StmtKind::VarDecl { name, name_span, .. } => {
                let d = self.decl(name, "variable", *name_span, name_span.end_col as usize, name_span.line as usize, scope, self.code(line), self.doc(line));
                self.add(d, into);
            }
            StmtKind::Shared { name, name_span, .. } => {
                let nl = name_span.line as usize;
                let d = self.decl(name, "variable", *name_span, name_span.end_col as usize, nl, scope, self.code(nl), self.doc(nl));
                self.add(d, into);
            }
            StmtKind::Assign { target, op: "=", .. } => {
                if let ExprKind::Name(name) = &target.kind {
                    let sp = target.span;
                    let tl = sp.line as usize;
                    let d = self.decl(name, "constant", sp, sp.end_col as usize, tl, scope, self.code(tl), self.doc(tl));
                    self.add(d, into);
                }
            }
            StmtKind::For { var, body, .. } => {
                let end = self.block_end(line, col);
                let inner = Some((line, end));
                if let Some(sp) = self.name_after(line, col) {
                    let d = self.decl(var, "variable", sp, sp.end_col as usize, sp.line as usize, inner, format!("for {var}"), String::new());
                    self.add(d, None);
                }
                self.body(body, inner, None);
            }
            StmtKind::If { branches, orelse } => {
                let mut into = into;
                for (_, b) in branches {
                    self.body(b, scope, into.as_deref_mut());
                }
                if let Some(b) = orelse {
                    self.body(b, scope, into);
                }
            }
            StmtKind::While { body, .. } | StmtKind::Repeat { body, .. } => {
                let end = self.block_end(line, col);
                self.body(body, Some((line, end)), None);
            }
            StmtKind::Match { arms, .. } => {
                let mut into = into;
                for a in arms {
                    self.body(&a.body, scope, into.as_deref_mut());
                }
            }
            StmtKind::Try { body, handlers, .. } => {
                let mut into = into;
                self.body(body, scope, into.as_deref_mut());
                for h in handlers {
                    self.body(&h.body, scope, into.as_deref_mut());
                }
            }
            StmtKind::Parallel(body) => self.body(body, scope, into),
            _ => {}
        }
    }

    /// The identifier at a 1-based position: (name, start col, end col).
    fn word_at(&self, line: usize, col: usize) -> Option<(String, usize, usize)> {
        if line < 1 || line > self.lines.len() {
            return None;
        }
        let text = &self.lines[line - 1];
        let mut i = col as i64 - 1;
        let ok = |i: i64| i >= 0 && (i as usize) < text.len() && is_ident(text[i as usize]);
        if !ok(i) && i > 0 && ok(i - 1) {
            i -= 1;
        }
        if !ok(i) {
            return None;
        }
        let mut a = i as usize;
        while a > 0 && is_ident(text[a - 1]) {
            a -= 1;
        }
        let mut b = i as usize;
        while b < text.len() && is_ident(text[b]) {
            b += 1;
        }
        if text[a].is_ascii_digit() || text[a].is_numeric() {
            return None;
        }
        Some((text[a..b].iter().collect(), a + 1, b + 1))
    }

    fn lookup(&self, line: usize, col: usize) -> Option<&Decl> {
        let (word, start, _) = self.word_at(line, col)?;
        let text = &self.lines[line - 1];
        let member = start > 1 && text[start - 2] == '.';
        let candidates: Vec<&Decl> = self.decls.iter().filter(|d| d.name == word).collect();
        if member {
            return candidates.into_iter().find(|d| d.member);
        }
        let visible: Vec<&Decl> = candidates
            .into_iter()
            .filter(|d| !d.member && d.scope.is_none_or(|(a, b)| a <= line && line <= b))
            .collect();
        let key = |d: &Decl| d.scope.map(|(a, _)| a as i64).unwrap_or(-1);
        let mut inner = *visible.first()?;
        for d in &visible {
            if key(d) > key(inner) {
                inner = d;
            }
        }
        let same: Vec<&Decl> = visible.iter().copied().filter(|d| d.scope == inner.scope).collect();
        let before: Vec<&Decl> = same.iter().copied().filter(|d| (d.line, d.col) <= (line, col)).collect();
        before.last().copied().or(same.first().copied())
    }
}

fn definition(idx: &Index, uri: &str, line: usize, col: usize) -> Json {
    match idx.lookup(line, col) {
        None => Json::Null,
        Some(d) => obj(vec![("uri", s(uri)), ("range", lsp_range(&idx.lines, d.line, d.col, d.line, d.end_col))]),
    }
}

fn hover(idx: &Index, line: usize, col: usize) -> Json {
    let Some(d) = idx.lookup(line, col) else { return Json::Null };
    let mut value = format!("```nexo\n{}\n```", d.detail);
    if !d.doc.is_empty() {
        value.push_str("\n\n");
        value.push_str(&d.doc);
    }
    let (_, a, b) = idx.word_at(line, col).unwrap();
    obj(vec![
        ("contents", obj(vec![("kind", s("markdown")), ("value", s(value))])),
        ("range", lsp_range(&idx.lines, line, a, line, b)),
    ])
}

fn symbols(idx: &Index) -> Json {
    fn one(idx: &Index, i: usize) -> Json {
        let d = &idx.decls[i];
        let end_len = idx.lines[d.end_line - 1].len();
        obj(vec![
            ("name", s(d.name.clone())),
            ("detail", s(d.detail.clone())),
            ("kind", n(symbol_kind(d.kind))),
            ("range", lsp_range(&idx.lines, d.line, 1, d.end_line, end_len + 1)),
            ("selectionRange", lsp_range(&idx.lines, d.line, d.col, d.line, d.end_col)),
            ("children", Json::Arr(d.children.iter().map(|&c| one(idx, c)).collect())),
        ])
    }
    Json::Arr(idx.top.iter().map(|&i| one(idx, i)).collect())
}

fn completion(idx: Option<&Index>, text: &str, line: usize, col: usize) -> Json {
    let lines = lines_of(text);
    let empty = Vec::new();
    let current = if line >= 1 && line <= lines.len() { &lines[line - 1] } else { &empty };
    let mut i = (col - 1).min(current.len());
    while i > 0 && is_ident(current[i - 1]) {
        i -= 1;
    }
    let mut items: Vec<(String, u32, String)> = Vec::new();
    let mut put = |label: &str, kind: &str, detail: &str| {
        if !items.iter().any(|(l, _, _)| l == label) {
            items.push((label.to_string(), completion_kind(kind), detail.to_string()));
        }
    };
    if i > 0 && current[i - 1] == '.' {
        if let Some(idx) = idx {
            for d in idx.decls.iter().filter(|d| d.member) {
                put(&d.name, if d.kind == "method" { "method" } else { "field" }, &d.detail);
            }
        }
        for m in MEMBERS {
            put(m, "method", "");
        }
    } else {
        if let Some(idx) = idx {
            for d in &idx.decls {
                if d.member || d.scope.is_some_and(|(a, b)| !(a <= line && line <= b)) {
                    continue;
                }
                let kind = match d.kind {
                    "struct" | "enum" => "type",
                    k => k,
                };
                put(&d.name, kind, &d.detail);
            }
        }
        for b in BUILTINS {
            let function = crate::resolve::BUILTIN_FUNCTIONS.contains(b) || LIBRARY_FUNCTIONS.contains(b);
            let kind = if function { "function" } else { "constant" };
            put(b, kind, "");
        }
        let mut kws: Vec<&str> = KEYWORDS.to_vec();
        kws.sort();
        for k in kws {
            put(k, "keyword", "");
        }
    }
    items.sort_by(|a, b| a.0.cmp(&b.0));
    Json::Arr(
        items
            .into_iter()
            .map(|(label, kind, detail)| {
                let mut pairs = vec![("label", s(label)), ("kind", n(kind))];
                if !detail.is_empty() {
                    pairs.push(("detail", s(detail)));
                }
                obj(pairs)
            })
            .collect(),
    )
}

// --------------------------------------------------------------- server

struct Server {
    docs: HashMap<String, String>,
    indexes: HashMap<String, Index>,
    shutdown: bool,
    lang_given: bool,
}

fn send(out: &mut impl Write, message: Json) {
    let body = message.text();
    let _ = write!(out, "Content-Length: {}\r\n\r\n{}", body.len(), body);
    let _ = out.flush();
}

fn read(inp: &mut impl BufRead) -> Option<Json> {
    let mut length = None;
    loop {
        let mut line = String::new();
        if inp.read_line(&mut line).ok()? == 0 {
            return None;
        }
        let line = line.trim();
        if line.is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                length = value.trim().parse::<usize>().ok();
            }
        }
    }
    let mut body = vec![0u8; length?];
    inp.read_exact(&mut body).ok()?;
    crate::json::parse(&String::from_utf8_lossy(&body))
}

impl Server {
    fn index(&mut self, uri: &str) -> Option<&Index> {
        let text = self.docs.get(uri)?;
        if let Some(idx) = Index::new(text) {
            self.indexes.insert(uri.to_string(), idx);
        }
        self.indexes.get(uri)
    }

    fn publish(&self, out: &mut impl Write, uri: &str) {
        let diags = self.docs.get(uri).map(|t| diagnostics(t, name_of(uri))).unwrap_or_default();
        send(
            out,
            obj(vec![
                ("jsonrpc", s("2.0")),
                ("method", s("textDocument/publishDiagnostics")),
                ("params", obj(vec![("uri", s(uri)), ("diagnostics", Json::Arr(diags))])),
            ]),
        );
    }

    fn position(&self, uri: &str, params: &Json) -> (usize, usize) {
        let pos = params.get("position");
        let line = pos.get("line").num().unwrap_or(0.0) as usize + 1;
        let character = pos.get("character").num().unwrap_or(0.0) as usize;
        let lines = lines_of(self.docs.get(uri).map(|s| s.as_str()).unwrap_or(""));
        let col = if line <= lines.len() { from_utf16(&lines[line - 1], character) } else { 1 };
        (line, col)
    }

    /// Some(result) for a request it knows; None for an unknown method.
    fn handle(&mut self, out: &mut impl Write, method: &str, params: &Json) -> Option<Json> {
        let uri = params.get("textDocument").get("uri").str().unwrap_or("").to_string();
        Some(match method {
            "initialize" => {
                let locale: String = params.get("locale").str().unwrap_or("").chars().take(2).collect();
                if !self.lang_given {
                    if let Some(l) = nexo_diag::parse_lang(&locale) {
                        nexo_diag::set_lang(l);
                    }
                }
                obj(vec![
                    (
                        "capabilities",
                        obj(vec![
                            ("positionEncoding", s("utf-16")),
                            ("textDocumentSync", obj(vec![("openClose", Json::Bool(true)), ("change", n(1))])),
                            ("documentFormattingProvider", Json::Bool(true)),
                            ("codeActionProvider", obj(vec![("codeActionKinds", Json::Arr(vec![s("quickfix"), s("source.fixAll.nexo")]))])),
                            ("definitionProvider", Json::Bool(true)),
                            ("hoverProvider", Json::Bool(true)),
                            ("documentSymbolProvider", Json::Bool(true)),
                            ("completionProvider", obj(vec![("triggerCharacters", Json::Arr(vec![s(".")]))])),
                        ]),
                    ),
                    ("serverInfo", obj(vec![("name", s("nexo")), ("version", s(env!("CARGO_PKG_VERSION")))])),
                ])
            }
            "shutdown" => {
                self.shutdown = true;
                Json::Null
            }
            "textDocument/didOpen" => {
                let doc = params.get("textDocument");
                let uri = doc.get("uri").str().unwrap_or("").to_string();
                self.docs.insert(uri.clone(), doc.get("text").str().unwrap_or("").to_string());
                self.publish(out, &uri);
                Json::Null
            }
            "textDocument/didChange" => {
                if let Some(last) = params.get("contentChanges").arr().last() {
                    self.docs.insert(uri.clone(), last.get("text").str().unwrap_or("").to_string());
                }
                self.publish(out, &uri);
                Json::Null
            }
            "textDocument/didClose" => {
                self.docs.remove(&uri);
                self.indexes.remove(&uri);
                send(
                    out,
                    obj(vec![
                        ("jsonrpc", s("2.0")),
                        ("method", s("textDocument/publishDiagnostics")),
                        ("params", obj(vec![("uri", s(uri.clone())), ("diagnostics", Json::Arr(vec![]))])),
                    ]),
                );
                Json::Null
            }
            "initialized" | "textDocument/didSave" | "$/cancelRequest" | "$/setTrace" | "workspace/didChangeConfiguration" => Json::Null,
            "textDocument/formatting" => match self.docs.get(&uri) {
                Some(t) => formatting(t),
                None => Json::Null,
            },
            "textDocument/codeAction" => match self.docs.get(&uri) {
                Some(t) => code_actions(t, &uri, params),
                None => Json::Arr(vec![]),
            },
            "textDocument/definition" => {
                let (l, c) = self.position(&uri, params);
                match self.index(&uri) {
                    Some(idx) => definition(idx, &uri, l, c),
                    None => Json::Null,
                }
            }
            "textDocument/hover" => {
                let (l, c) = self.position(&uri, params);
                match self.index(&uri) {
                    Some(idx) => hover(idx, l, c),
                    None => Json::Null,
                }
            }
            "textDocument/documentSymbol" => match self.index(&uri) {
                Some(idx) => symbols(idx),
                None => Json::Arr(vec![]),
            },
            "textDocument/completion" => {
                let (l, c) = self.position(&uri, params);
                let Some(text) = self.docs.get(&uri).cloned() else { return Some(Json::Arr(vec![])) };
                let idx = self.index(&uri);
                completion(idx, &text, l, c)
            }
            m if m.starts_with("$/") => Json::Null,
            _ => return None,
        })
    }
}

/// Serve until `exit`: 0 after a `shutdown`, 1 otherwise.
pub fn serve(lang_given: bool) -> u8 {
    let stdin = std::io::stdin();
    let mut inp = stdin.lock();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    let mut server = Server { docs: HashMap::new(), indexes: HashMap::new(), shutdown: false, lang_given };
    loop {
        let Some(message) = read(&mut inp) else { return if server.shutdown { 0 } else { 1 } };
        let method = message.get("method").str().unwrap_or("").to_string();
        if method == "exit" {
            return if server.shutdown { 0 } else { 1 };
        }
        let result = server.handle(&mut out, &method, message.get("params"));
        let id = message.get("id");
        if id.is_null() {
            continue;
        }
        let reply = match result {
            Some(r) => obj(vec![("jsonrpc", s("2.0")), ("id", id.clone()), ("result", r)]),
            None => obj(vec![
                ("jsonrpc", s("2.0")),
                ("id", id.clone()),
                ("error", obj(vec![("code", n(-32601)), ("message", s(format!("unknown method {method}")))])),
            ]),
        };
        send(&mut out, reply);
    }
}
