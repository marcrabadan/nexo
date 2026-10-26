//! text: regular expressions and templates (section 15, D95), and
//! `move_file` (io).

use nexo_diag::{msg, Diagnostic};

use crate::fmt::quote;
use crate::state::{diag, failure, R};
use crate::stdlib::{expects, method_args, missing_member, str_arg};
use crate::value::*;

/// Patterns that use what only one of the two engines has are rejected
/// (the same checks as phase 0's translation to Python's re).
fn shared_syntax(p: &str) -> bool {
    if ["(?=", "(?!", "(?<=", "(?<!", "(?>", "(?P=", "(?#"].iter().any(|u| p.contains(u)) {
        return false;
    }
    let b: Vec<char> = p.chars().collect();
    let mut i = 0;
    let mut in_class = false;
    while i < b.len() {
        let c = b[i];
        if c == '\\' {
            let Some(&n) = b.get(i + 1) else { return false };
            if n.is_ascii_digit() && n != '0' && !in_class {
                return false;
            }
            if "ZpPG".contains(n) {
                return false;
            }
            i += 2;
            continue;
        }
        if in_class {
            if c == ']' {
                in_class = false;
            } else if c == '[' && b.get(i + 1) == Some(&':') {
                return false;
            }
            i += 1;
            continue;
        }
        if c == '[' {
            in_class = true;
            i += 1;
            if b.get(i) == Some(&'^') {
                i += 1;
            }
            if b.get(i) == Some(&']') {
                i += 1;
            }
            continue;
        }
        if c == '(' && b.get(i + 1) == Some(&'?') {
            let mut j = i + 2;
            while j < b.len() && "imsxU-".contains(b[j]) {
                j += 1;
            }
            if j > i + 2 && matches!(b.get(j), Some(':') | Some(')')) && b[i + 2..j].contains(&'U') {
                return false;
            }
        }
        if "*+?".contains(c) && b.get(i + 1) == Some(&'+') {
            return false;
        }
        i += 1;
    }
    true
}

pub struct Regex {
    pattern: String,
    re: regex::Regex,
    /// the pattern anchored at both ends, for `matches`
    full: regex::Regex,
}

const METHODS: &[&str] = &["matches", "find", "find_all", "replace", "split", "groups"];

impl Native for Regex {
    fn type_name(&self) -> &str {
        "regex"
    }
    fn repr(&self) -> String {
        format!("regex({})", quote(&self.pattern))
    }
    fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
        match METHODS.iter().find(|m| **m == name) {
            Some(m) => Ok(Val::new(Obj::BuiltinMethod(Val::from(me), m))),
            None => Err(missing_member("regex", name, METHODS, span)),
        }
    }
    fn call_method(&self, _me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
        let params: &[&str] = if name == "replace" { &["text", "with"] } else { &["text"] };
        let a = method_args(name, params, params.len(), args, names, span)?;
        let text = a[0].as_str().ok_or_else(|| expects(name, "str", a[0], span))?;
        Ok(match name {
            "matches" => Val::bool(self.full.is_match(text)),
            "find" => self.re.find(text).map(|m| Val::str(m.as_str())).unwrap_or(Val::none()),
            "find_all" => list(self.re.find_iter(text).map(|m| Val::str(m.as_str())).collect()),
            "replace" => {
                let with = a[1].as_str().ok_or_else(|| expects(name, "str", a[1], span))?;
                Val::str(self.re.replace_all(text, regex::NoExpand(with)).into_owned())
            }
            "split" => list(self.re.split(text).map(Val::str).collect()),
            "groups" => match self.re.captures(text) {
                Some(c) => list(c.iter().skip(1).map(|g| g.map(|m| Val::str(m.as_str())).unwrap_or(Val::none())).collect()),
                None => Val::none(),
            },
            _ => return Err(missing_member("regex", name, METHODS, span)),
        })
    }
    fn equals(&self, other: &dyn Native) -> bool {
        other.as_any().downcast_ref::<Regex>().is_some_and(|o| o.pattern == self.pattern)
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

pub fn regex(pattern: V, span: u32) -> R<Val> {
    let p = str_arg("regex", &pattern, span)?;
    let re = if shared_syntax(p) { regex::Regex::new(p).ok() } else { None };
    let full = re.as_ref().and_then(|_| regex::Regex::new(&format!(r"\A(?:{p})\z")).ok());
    match (re, full) {
        (Some(re), Some(full)) => Ok(native(Regex { pattern: p.to_string(), re, full })),
        _ => Err(diag(Diagnostic::new_spanless("bad_regex").arg("pattern", p), span)),
    }
}

fn html_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            _ => out.push(c),
        }
    }
    out
}

/// `render(path, name: value...)`: a template file with {name}
/// placeholders; in .html files the values are escaped (section 9).
pub fn render(args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    let positional: Vec<V> = args.iter().zip(names).filter(|(_, n)| n.is_none()).map(|(v, _)| *v).collect();
    if positional.len() > 1 {
        return Err(diag(
            Diagnostic::new_spanless("arg_too_many").arg("fn", "render").arg("n", "1").arg("m", positional.len().to_string()),
            span,
        ));
    }
    let mut seen: Vec<&str> = Vec::new();
    for n in names.iter().flatten() {
        if seen.contains(n) {
            return Err(diag(Diagnostic::new_spanless("arg_twice").arg("param", *n), span));
        }
        seen.push(n);
    }
    let Some(path) = positional.first().copied().or_else(|| args.iter().zip(names).find(|(_, n)| **n == Some("path")).map(|(v, _)| *v)) else {
        return Err(diag(Diagnostic::new_spanless("arg_missing").arg("param", "path").arg("fn", "render"), span));
    };
    let path = str_arg("render", &path, span)?;
    let template = crate::data::read_text(path, span)?;
    let escape = {
        let l = path.to_lowercase();
        l.ends_with(".html") || l.ends_with(".htm")
    };
    let chars: Vec<char> = template.chars().collect();
    let mut out = String::with_capacity(template.len());
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '{' && chars.get(i + 1) == Some(&'{') {
            out.push('{');
            i += 2;
            continue;
        }
        if c == '{' && chars.get(i + 1).is_some_and(|c| c.is_alphabetic() || *c == '_') {
            let mut j = i + 1;
            while j < chars.len() && (chars[j].is_alphanumeric() || chars[j] == '_') {
                j += 1;
            }
            if chars.get(j) == Some(&'}') {
                let name: String = chars[i + 1..j].iter().collect();
                let Some(v) = args.iter().zip(names).find(|(_, n)| **n == Some(name.as_str())).map(|(v, _)| *v) else {
                    let detail = msg("detail.template", &[("path", path.to_string()), ("name", name)]);
                    return Err(failure("InvalidData", span, &[("detail", detail)]));
                };
                let text = crate::fmt::show_checked(crate::pipe::bind(v)?.v(), span)?;
                out.push_str(&if escape { html_escape(&text) } else { text });
                i = j + 1;
                continue;
            }
        }
        out.push(c);
        i += 1;
    }
    Ok(Val::str(out))
}

/// `move_file(path, to: folder)`: into the folder (created if needed);
/// returns the new path.
pub fn move_file(path: V, to: V, span: u32) -> R<Val> {
    let path = str_arg("move_file", &path, span)?;
    let folder = str_arg("move_file", &to, span)?;
    let meta = std::fs::metadata(path);
    match &meta {
        Err(e) => return Err(crate::data::os_failure(path, e, span)),
        Ok(m) if !m.is_file() => {
            let message = msg("err.IOError", &[("path", path.to_string()), ("reason", msg("detail.not_a_file", &[]))]);
            return Err(crate::state::raise_error("IOError", message, Val::none(), span));
        }
        _ => {}
    }
    let file = std::path::Path::new(path).file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    let target = std::path::Path::new(folder).join(&file).to_string_lossy().to_string();
    let r = crate::repl::write_file(|| {
        std::fs::create_dir_all(folder)?;
        match std::fs::rename(path, &target) {
            Ok(()) => Ok(()),
            Err(_) => {
                std::fs::copy(path, &target)?;
                std::fs::remove_file(path)
            }
        }
    });
    if let Err(e) = r {
        return Err(crate::data::os_failure(&target, &e, span));
    }
    Ok(Val::str(target))
}
