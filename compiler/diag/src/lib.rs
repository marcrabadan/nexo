//! Error messages of Nexo (section 14): the message catalog (in English,
//! D125), the renderer and the "Did you mean...?" suggestions.
//!
//! Every message is two sentences: what went wrong and how to fix it. They
//! are rendered as
//!
//! ```text
//! Error in app.nexo, line 4:
//!   print(nmae)
//!         ^^^^
//! "nmae" does not exist. Did you mean "name"?
//! ```
//!
//! Both the compiler and the runtime library use this crate, so a message
//! reads the same whether it is found before or while the program runs.

mod catalog;

/// A position in the source: one line, columns counted in characters,
/// 1-based, `end_col` exclusive.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub struct Span {
    pub line: u32,
    pub col: u32,
    pub end_col: u32,
    /// 0 for the file being compiled, else a module of the program (D124)
    /// registered with `add_file`
    pub file: u32,
}

impl Span {
    pub const fn new(line: u32, col: u32, end_col: u32) -> Span {
        Span { line, col, end_col, file: 0 }
    }

    pub const fn in_file(file: u32, line: u32, col: u32, end_col: u32) -> Span {
        Span { line, col, end_col, file }
    }

    /// From the start of `self` to the end of `other`; cut at the end of the
    /// line when they are on different lines.
    pub fn to(self, other: Span) -> Span {
        if other.line != self.line {
            return Span::in_file(self.file, self.line, self.col, 1_000_000_000);
        }
        Span::in_file(self.file, self.line, self.col, self.end_col.max(other.end_col))
    }
}

/// The files of the modules a program uses (D124): id -> (name, source).
static FILES: std::sync::Mutex<Vec<(u32, &'static str, &'static str)>> = std::sync::Mutex::new(Vec::new());

/// Register a module file; its spans carry the id returned.
pub fn add_file(name: &str, source: &str) -> u32 {
    let mut files = FILES.lock().unwrap();
    let id = files.iter().map(|f| f.0).max().unwrap_or(0) + 1;
    files.push((id, Box::leak(name.to_string().into_boxed_str()), Box::leak(source.to_string().into_boxed_str())));
    id
}

/// Register a module file under a known id (the runtime of a compiled
/// program, with the ids the compiler gave).
pub fn set_file(id: u32, name: &'static str, source: &'static str) {
    FILES.lock().unwrap().push((id, name, source));
}

/// (name, source) of a registered module file.
pub fn file_of(id: u32) -> Option<(&'static str, &'static str)> {
    FILES.lock().unwrap().iter().find(|f| f.0 == id).map(|f| (f.1, f.2))
}

fn lookup(table: &'static [(&'static str, &'static str)], key: &str) -> Option<&'static str> {
    table.iter().find(|(k, _)| *k == key).map(|(_, text)| *text)
}

pub fn has(key: &str) -> bool {
    lookup(catalog::MESSAGES, key).is_some()
}

/// The text of a catalog entry with its `{placeholders}` filled in.
pub fn msg(key: &str, args: &[(&str, String)]) -> String {
    let template = lookup(catalog::MESSAGES, key).unwrap_or_else(|| panic!("unknown message {key}"));
    format(template, args)
}

/// A word of the parser's "Unexpected X here. Expected Y." messages.
pub fn word(key: &str) -> String {
    lookup(catalog::WORDS, key).unwrap_or(key).to_string()
}

/// `str.format` semantics: `{name}` is replaced, `{{` and `}}` are braces.
pub fn format(template: &str, args: &[(&str, String)]) -> String {
    let mut out = String::with_capacity(template.len() + 16);
    let mut chars = template.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '{' if chars.peek() == Some(&'{') => {
                chars.next();
                out.push('{');
            }
            '}' if chars.peek() == Some(&'}') => {
                chars.next();
                out.push('}');
            }
            '{' => {
                let mut name = String::new();
                for c in chars.by_ref() {
                    if c == '}' {
                        break;
                    }
                    name.push(c);
                }
                match args.iter().find(|(k, _)| *k == name) {
                    Some((_, v)) => out.push_str(v),
                    None => panic!("missing argument {name} in {template}"),
                }
            }
            _ => out.push(c),
        }
    }
    out
}

/// A diagnostic: a compile error, or a runtime mistake that is not an error
/// value. It always ends the program and is never caught by `try`.
#[derive(Clone, Debug)]
pub struct Diagnostic {
    pub key: &'static str,
    pub span: Option<Span>,
    /// Key of the fix sentence without the "fix." prefix; defaults to `key`.
    pub fix: Option<&'static str>,
    pub args: Vec<(&'static str, String)>,
}

impl Diagnostic {
    pub fn new(key: &'static str, span: Span) -> Diagnostic {
        Diagnostic { key, span: Some(span), fix: None, args: Vec::new() }
    }

    /// A diagnostic whose span is filled in later (the runtime knows the
    /// span only as an index into the program's span table).
    pub fn new_spanless(key: &'static str) -> Diagnostic {
        Diagnostic { key, span: None, fix: None, args: Vec::new() }
    }

    pub fn arg(mut self, name: &'static str, value: impl Into<String>) -> Diagnostic {
        self.args.push((name, value.into()));
        self
    }

    pub fn fix(mut self, fix: &'static str) -> Diagnostic {
        self.fix = Some(fix);
        self
    }

    /// The two sentences: what went wrong and how to fix it.
    pub fn text(&self) -> String {
        let what = msg(self.key, &self.args);
        let fix_key = format!("fix.{}", self.fix.unwrap_or(self.key));
        if has(&fix_key) {
            format!("{} {}", what, msg(&fix_key, &self.args))
        } else {
            what
        }
    }

    pub fn arg_value(&self, name: &str) -> Option<&str> {
        self.args.iter().find(|(k, _)| *k == name).map(|(_, v)| v.as_str())
    }
}

/// Render a message in the section 14 format.
pub fn render(file: &str, source: &str, span: Option<Span>, text: &str) -> String {
    render_with("header", file, source, span, text)
}

/// The same format with the header of a `nexo lint` warning.
pub fn render_warning(file: &str, source: &str, span: Span, text: &str) -> String {
    render_with("header_warning", file, source, Some(span), text)
}

fn render_with(header: &str, file: &str, source: &str, span: Option<Span>, text: &str) -> String {
    let Some(span) = span else {
        return format!("Error: {text}\n");
    };
    let (file, source) = match file_of(span.file) {
        Some((f, s)) if span.file != 0 => (f, s),
        _ => (file, source),
    };
    let mut out = vec![msg(header, &[("file", file.to_string()), ("line", span.line.to_string())])];
    let lines: Vec<&str> = source.split('\n').map(|l| l.strip_suffix('\r').unwrap_or(l)).collect();
    let count = if source.ends_with('\n') { lines.len() - 1 } else { lines.len() };
    if span.line >= 1 && (span.line as usize) <= count {
        let line: Vec<char> = lines[span.line as usize - 1].chars().collect();
        let cut = line.iter().take_while(|c| **c == ' ' || **c == '\t').count();
        let mut stripped: Vec<char> = line[cut..].to_vec();
        while stripped.last().is_some_and(|c| c.is_whitespace()) {
            stripped.pop();
        }
        let start = (span.col as i64 - 1 - cut as i64).max(0) as usize;
        let mut end = (span.end_col as i64 - 1 - cut as i64).min(stripped.len() as i64).max(0) as usize;
        if end <= start {
            end = start + 1;
        }
        out.push(format!("  {}", stripped.iter().collect::<String>()));
        out.push(format!("  {}{}", " ".repeat(start), "^".repeat(end - start)));
    }
    out.push(text.to_string());
    out.join("\n") + "\n"
}

/// The text of the source between the columns of a span ("" if outside).
pub fn source_text(source: &str, span: Span) -> String {
    let source = match file_of(span.file) {
        Some((_, s)) if span.file != 0 => s,
        _ => source,
    };
    let Some(line) = source.split('\n').nth(span.line as usize - 1) else {
        return String::new();
    };
    let line = line.strip_suffix('\r').unwrap_or(line);
    line.chars()
        .skip(span.col as usize - 1)
        .take((span.end_col.saturating_sub(span.col)) as usize)
        .collect()
}

/// Closest candidate by Damerau-Levenshtein distance, or None.
pub fn suggest<'a, I: IntoIterator<Item = &'a str>>(name: &str, candidates: I) -> Option<String> {
    let cands: Vec<&str> = candidates.into_iter().collect();
    best_match(name, cands.iter().copied()).map(|(_, i)| cands[i].to_string())
}

fn limit_for(name: &str) -> usize {
    if name.chars().count() <= 4 {
        1
    } else {
        2
    }
}

/// The closest candidate within the limit as (distance, position); the
/// first one wins between equally close candidates.
pub fn best_match<'a, I: IntoIterator<Item = &'a str>>(name: &str, candidates: I) -> Option<(usize, usize)> {
    let mut best: Option<(usize, usize)> = None;
    let limit = limit_for(name);
    for (i, cand) in candidates.into_iter().enumerate() {
        if cand == name || cand.starts_with("__") {
            continue;
        }
        let d = distance(name, cand);
        if d <= limit && best.is_none_or(|(bd, _)| d < bd) {
            best = Some((d, i));
        }
    }
    best
}

/// The strings left after removing up to `k` characters.
fn deletions(s: &str, k: usize, out: &mut std::collections::HashSet<String>) {
    let chars: Vec<char> = s.chars().collect();
    let mut frontier = vec![chars];
    out.insert(s.to_string());
    for _ in 0..k {
        let mut next = Vec::new();
        for w in &frontier {
            for i in 0..w.len() {
                let mut v = w.clone();
                v.remove(i);
                if out.insert(v.iter().collect()) {
                    next.push(v);
                }
            }
        }
        frontier = next;
    }
}

/// A large set of candidates indexed by the strings left after deleting up
/// to two characters (D127): two names within distance 2 always share one,
/// so only those candidates are compared. Gives what `best_match` gives.
pub struct SuggestIndex {
    names: Vec<String>,
    by_deletion: std::collections::HashMap<String, Vec<u32>>,
}

impl SuggestIndex {
    pub fn new<'a, I: IntoIterator<Item = &'a str>>(candidates: I) -> SuggestIndex {
        let mut index = SuggestIndex { names: Vec::new(), by_deletion: std::collections::HashMap::new() };
        for n in candidates {
            index.push(n);
        }
        index
    }

    /// Add a candidate after the others.
    pub fn push(&mut self, name: &str) {
        let i = self.names.len() as u32;
        let mut set = std::collections::HashSet::new();
        deletions(name, 2, &mut set);
        for d in set {
            self.by_deletion.entry(d).or_default().push(i);
        }
        self.names.push(name.to_string());
    }

    pub fn len(&self) -> usize {
        self.names.len()
    }

    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }

    pub fn best_match(&self, name: &str) -> Option<(usize, usize)> {
        let limit = limit_for(name);
        let mut set = std::collections::HashSet::new();
        deletions(name, limit, &mut set);
        let mut seen: Vec<u32> = set.iter().filter_map(|d| self.by_deletion.get(d)).flatten().copied().collect();
        seen.sort_unstable();
        seen.dedup();
        let mut best: Option<(usize, usize)> = None;
        for i in seen {
            let cand = &self.names[i as usize];
            if cand == name || cand.starts_with("__") {
                continue;
            }
            let d = distance(name, cand);
            if d <= limit && best.is_none_or(|(bd, bi)| d < bd || (d == bd && (i as usize) < bi)) {
                best = Some((d, i as usize));
            }
        }
        best
    }

    pub fn name(&self, i: usize) -> &str {
        &self.names[i]
    }
}

fn distance(a: &str, b: &str) -> usize {
    if (a.len() as i64 - b.len() as i64).abs() > 8 {
        // more than two characters apart (a character is at most 4 bytes)
        return 99;
    }
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    if (a.len() as i64 - b.len() as i64).abs() > 2 {
        return 99;
    }
    let mut prev2: Option<Vec<usize>> = None;
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    for i in 1..=a.len() {
        let mut cur = vec![0; b.len() + 1];
        cur[0] = i;
        for j in 1..=b.len() {
            let cost = if a[i - 1] == b[j - 1] { 0 } else { 1 };
            cur[j] = (prev[j] + 1).min(cur[j - 1] + 1).min(prev[j - 1] + cost);
            if let Some(p2) = &prev2 {
                if i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1] {
                    cur[j] = cur[j].min(p2[j - 2] + 1);
                }
            }
        }
        prev2 = Some(prev);
        prev = cur;
    }
    prev[b.len()]
}

/// Text of an unhandled error value: "Unhandled error (Kind): message. Fix."
pub fn unhandled_text(kind: &str, message: &str, fix_key: Option<&str>) -> String {
    let kind = if kind == "Error" { String::new() } else { format!(" ({kind})") };
    let mut what = msg("unhandled", &[("kind", kind), ("message", message.to_string())]);
    if !(what.ends_with('.') || what.ends_with('!') || what.ends_with('?')) {
        what.push('.');
    }
    let fix = match fix_key {
        Some(k) if has(k) => msg(k, &[]),
        _ => msg("fix.unhandled", &[]),
    };
    format!("{what} {fix}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_the_section_14_example() {
        let d = Diagnostic::new("name_not_found", Span::new(4, 7, 11))
            .fix("suggest")
            .arg("name", "nmae")
            .arg("suggestion", "name");
        let src = "name = \"Ana\"\nage = 30\ngreeting = \"Hello\"\nprint(nmae)\n";
        let text = render("app.nexo", src, d.span, &d.text());
        assert_eq!(
            text,
            "Error in app.nexo, line 4:\n  print(nmae)\n        ^^^^\n\"nmae\" does not exist. Did you mean \"name\"?\n"
        );
    }

    #[test]
    fn format_keeps_literal_braces() {
        assert_eq!(format("The interpolation {{}} is empty.", &[]), "The interpolation {} is empty.");
    }

    #[test]
    fn suggestions_use_edit_distance() {
        assert_eq!(suggest("nmae", ["name", "age"]), Some("name".into()));
        assert_eq!(suggest("zzzzzz", ["name"]), None);
        assert_eq!(suggest("lenght", ["length"]), Some("length".into()));
    }

    #[test]
    fn source_text_cuts_by_characters() {
        assert_eq!(source_text("x = \"ñandú\" + y\n", Span::new(1, 5, 12)), "\"ñandú\"");
    }
}
