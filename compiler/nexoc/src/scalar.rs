//! Development mode rewrites the values of the IR (`%V`, two words) into
//! pairs of i64 before clang compiles it (D141): clang's fast instruction
//! selector, the one used without optimisation, does not take a `%V`
//! loaded, stored, built or passed by value, and the slow one it falls
//! back to took most of the compile time. Loads and stores become two,
//! `insertvalue` and `extractvalue` name the words they mean, `select`
//! chooses each word, and the helpers that take a `%V` (rc_inc, rc_dec,
//! nx_plainv) take its two words. A value returned by a call is split
//! right after it; `ret` builds the value again. A function with a form
//! this pass does not know is kept as it was.

use std::collections::{HashMap, HashSet};
use std::fmt::Write as _;

pub fn scalarize(ir: &str) -> String {
    // the functions defined with a `%V` parameter
    let mut pair_fns: HashSet<&str> = HashSet::new();
    for line in ir.lines() {
        if let Some(rest) = line.strip_prefix("define ") {
            if let Some((name, params)) = callee(rest) {
                if params.split(", ").any(|p| p.starts_with("%V ")) {
                    pair_fns.insert(name);
                }
            }
        }
    }
    let mut out = String::with_capacity(ir.len() + ir.len() / 4);
    let mut lines = ir.lines();
    let mut f = Func::default();
    while let Some(line) = lines.next() {
        if !line.starts_with("define ") {
            out.push_str(line);
            out.push('\n');
            continue;
        }
        let start = out.len();
        let mut body = vec![line];
        for l in lines.by_ref() {
            if l == "}" {
                break;
            }
            body.push(l);
        }
        f.clear();
        if f.function(&body, &pair_fns, &mut out).is_none() {
            out.truncate(start);
            for l in &body {
                out.push_str(l);
                out.push('\n');
            }
        }
        out.push_str("}\n");
    }
    out
}

/// The name of the function called or defined in `s` and the text of its
/// arguments (which may hold constant expressions in parentheses).
fn callee(s: &str) -> Option<(&str, &str)> {
    let at = s.find('@')?;
    let open = at + s[at..].find('(')?;
    let close = open + closing(&s[open..])?;
    Some((&s[at..open], &s[open + 1..close]))
}

/// Where the parenthesis that opens `s` is closed.
fn closing(s: &str) -> Option<usize> {
    let mut depth = 0;
    for (i, c) in s.bytes().enumerate() {
        match c {
            b'(' => depth += 1,
            b')' => {
                depth -= 1;
                if depth == 0 {
                    return Some(i);
                }
            }
            _ => {}
        }
    }
    None
}

/// A single operand: a register, a global or a number.
fn token(s: &str) -> Option<&str> {
    let ok = !s.is_empty() && s.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'%' | b'@' | b'.' | b'_' | b'-'));
    ok.then_some(s)
}

#[derive(Default)]
struct Func<'a> {
    /// the two words of each `%V` register
    pair: HashMap<&'a str, (String, String)>,
    /// registers that are one word of a `%V`
    alias: HashMap<&'a str, String>,
    temps: usize,
}

impl<'a> Func<'a> {
    fn clear(&mut self) {
        self.pair.clear();
        self.alias.clear();
        self.temps = 0;
    }

    fn temp(&mut self) -> String {
        self.temps += 1;
        format!("%z.{}", self.temps)
    }

    /// `s` with each register that names a word replaced by that word.
    fn sub(&self, s: &str, out: &mut String) {
        if self.alias.is_empty() {
            out.push_str(s);
            return;
        }
        let b = s.as_bytes();
        let mut i = 0;
        let mut last = 0;
        while i < b.len() {
            if b[i] == b'%' {
                let mut j = i + 1;
                while j < b.len() && (b[j].is_ascii_alphanumeric() || b[j] == b'.' || b[j] == b'_') {
                    j += 1;
                }
                if let Some(w) = self.alias.get(&s[i..j]) {
                    out.push_str(&s[last..i]);
                    out.push_str(w);
                    last = j;
                }
                i = j;
            } else {
                i += 1;
            }
        }
        out.push_str(&s[last..]);
    }

    fn word(&self, s: &str) -> String {
        let mut o = String::new();
        self.sub(s, &mut o);
        o
    }

    /// The two words of a `%V` operand.
    fn val(&self, s: &str) -> Option<(String, String)> {
        let s = s.trim();
        if s == "zeroinitializer" || s == "undef" || s == "poison" {
            return Some(("0".into(), "0".into()));
        }
        if let Some(inner) = s.strip_prefix("{ i64 ").and_then(|r| r.strip_suffix(" }")) {
            let (t, b) = inner.split_once(", i64 ")?;
            return Some((token(t)?.to_string(), token(b)?.to_string()));
        }
        let (t, b) = self.pair.get(s)?;
        Some((self.word(t), self.word(b)))
    }

    fn function(&mut self, body: &[&'a str], pair_fns: &HashSet<&str>, out: &mut String) -> Option<()> {
        // the parameters that are values become two words
        let head = body[0];
        let (_, params) = callee(head)?;
        let open = head.find('(')?;
        let close = open + closing(&head[open..])?;
        out.push_str(&head[..open + 1]);
        for (k, p) in params.split(", ").filter(|p| !p.is_empty()).enumerate() {
            if k > 0 {
                out.push_str(", ");
            }
            match p.strip_prefix("%V ") {
                Some(r) => {
                    let _ = write!(out, "i64 {r}.t, i64 {r}.b");
                    self.pair.insert(r, (format!("{r}.t"), format!("{r}.b")));
                }
                None => out.push_str(p),
            }
        }
        out.push_str(&head[close..]);
        out.push('\n');
        for &line in &body[1..] {
            let s = line.trim();
            if s.ends_with(':') || !s.contains("%V") {
                if s.ends_with(':') {
                    out.push_str(line);
                } else {
                    out.push_str("  ");
                    self.sub(s, out);
                }
                out.push('\n');
                continue;
            }
            self.line(s, pair_fns, out)?;
        }
        Some(())
    }

    fn line(&mut self, s: &'a str, pair_fns: &HashSet<&str>, out: &mut String) -> Option<()> {
        if let Some(rest) = s.strip_prefix("store %V ") {
            let i = rest.rfind(", ptr ")?;
            let p = self.word(token(&rest[i + 6..])?);
            let (t, b) = self.val(&rest[..i])?;
            let q = self.temp();
            let _ = writeln!(out, "  store i64 {t}, ptr {p}\n  {q} = getelementptr inbounds i8, ptr {p}, i64 8\n  store i64 {b}, ptr {q}");
            return Some(());
        }
        if let Some(rest) = s.strip_prefix("ret %V ") {
            let (t, b) = self.val(rest)?;
            let r0 = self.temp();
            let r1 = self.temp();
            let _ = writeln!(out, "  {r0} = insertvalue %V undef, i64 {t}, 0\n  {r1} = insertvalue %V {r0}, i64 {b}, 1\n  ret %V {r1}");
            return Some(());
        }
        if s.starts_with("call ") {
            return self.call(None, &s[5..], pair_fns, out);
        }
        if s.contains("alloca ") || s.contains("getelementptr ") {
            out.push_str("  ");
            self.sub(s, out);
            out.push('\n');
            return Some(());
        }
        let (x, rhs) = s.split_once(" = ")?;
        let x = token(x)?;
        if let Some(p) = rhs.strip_prefix("load %V, ptr ") {
            let p = self.word(token(p)?);
            let _ = writeln!(out, "  {x}.t = load i64, ptr {p}\n  {x}.q = getelementptr inbounds i8, ptr {p}, i64 8\n  {x}.b = load i64, ptr {x}.q");
            self.pair.insert(x, (format!("{x}.t"), format!("{x}.b")));
            return Some(());
        }
        if let Some(rest) = rhs.strip_prefix("insertvalue %V ") {
            let (rest, idx) = rest.rsplit_once(", ")?;
            let (agg, y) = rest.rsplit_once(", i64 ")?;
            let y = self.word(token(y)?);
            let (t, b) = self.val(agg)?;
            let p = match idx {
                "0" => (y, b),
                "1" => (t, y),
                _ => return None,
            };
            self.pair.insert(x, p);
            return Some(());
        }
        if let Some(rest) = rhs.strip_prefix("extractvalue %V ") {
            let (y, idx) = rest.rsplit_once(", ")?;
            let (t, b) = self.val(token(y)?)?;
            let w = match idx {
                "0" => t,
                "1" => b,
                _ => return None,
            };
            self.alias.insert(x, w);
            return Some(());
        }
        if let Some(rest) = rhs.strip_prefix("select i1 ") {
            let (c, rest) = rest.split_once(", %V ")?;
            let (a, b) = rest.split_once(", %V ")?;
            let c = self.word(token(c)?);
            let (at, ab) = self.val(a)?;
            let (bt, bb) = self.val(b)?;
            let _ = writeln!(out, "  {x}.t = select i1 {c}, i64 {at}, i64 {bt}\n  {x}.b = select i1 {c}, i64 {ab}, i64 {bb}");
            self.pair.insert(x, (format!("{x}.t"), format!("{x}.b")));
            return Some(());
        }
        if let Some(rest) = rhs.strip_prefix("call ") {
            return self.call(Some(x), rest, pair_fns, out);
        }
        None
    }

    /// A call (`rest` is the text after `call `): the values passed to a
    /// helper become two words, a value returned is split.
    fn call(&mut self, x: Option<&'a str>, rest: &str, pair_fns: &HashSet<&str>, out: &mut String) -> Option<()> {
        let (ret, after) = rest.split_once(' ')?;
        let (name, args) = callee(after)?;
        let tail = &after[after.find('(')? + args.len() + 2..];
        if let Some(x) = x {
            let _ = write!(out, "  {x} = ");
        } else {
            out.push_str("  ");
        }
        let _ = write!(out, "call {ret} {name}(");
        if pair_fns.contains(name) {
            for (k, a) in args.split(", ").filter(|a| !a.is_empty()).enumerate() {
                if k > 0 {
                    out.push_str(", ");
                }
                match a.strip_prefix("%V ") {
                    Some(v) => {
                        let (t, b) = self.val(v)?;
                        let _ = write!(out, "i64 {t}, i64 {b}");
                    }
                    None => self.sub(a, out),
                }
            }
        } else {
            if args.contains("%V") {
                return None;
            }
            self.sub(args, out);
        }
        out.push(')');
        out.push_str(tail);
        out.push('\n');
        match (x, ret) {
            (Some(x), "%V") => {
                let _ = writeln!(out, "  {x}.t = extractvalue %V {x}, 0\n  {x}.b = extractvalue %V {x}, 1");
                self.pair.insert(x, (format!("{x}.t"), format!("{x}.b")));
            }
            (_, "%V") => {}
            _ if ret.contains("%V") => return None,
            _ => {}
        }
        Some(())
    }
}

#[cfg(test)]
mod tests {
    use super::scalarize;

    #[test]
    fn values_become_words() {
        let ir = "define internal void @rc_inc(%V %v) #0 {\nentry:\n  %t = extractvalue %V %v, 0\n  ret void\n}\n\
                  define internal %V @f(ptr %tls) {\nentry:\n  %a = alloca %V\n  store %V { i64 2, i64 5 }, ptr %a\n  \
                  %r = load %V, ptr %a\n  call void @rc_inc(%V %r)\n  %s = insertvalue %V { i64 2, i64 0 }, i64 7, 1\n  \
                  %b = extractvalue %V %s, 1\n  %c = add i64 %b, 1\n  ret %V %r\n}\n";
        let out = scalarize(ir);
        assert!(out.contains("define internal void @rc_inc(i64 %v.t, i64 %v.b) #0 {"), "{out}");
        assert!(out.contains("store i64 2, ptr %a"), "{out}");
        assert!(out.contains("%r.t = load i64, ptr %a"), "{out}");
        assert!(out.contains("call void @rc_inc(i64 %r.t, i64 %r.b)"), "{out}");
        assert!(out.contains("%c = add i64 7, 1"), "{out}");
        assert!(out.contains("ret %V %z."), "{out}");
    }

    #[test]
    fn constant_expressions_in_arguments() {
        let ir = "define internal %V @f(ptr %tls) {\nentry:\n  %a = call %V @g()\n  %t = extractvalue %V %a, 0\n  \
                  %r = call %V @nx_binop(i32 2, i64 5, i64 ptrtoint (ptr @lf.1 to i64), i64 %t, i32 2)\n  ret %V %r\n}\n";
        let out = scalarize(ir);
        assert!(out.contains("@nx_binop(i32 2, i64 5, i64 ptrtoint (ptr @lf.1 to i64), i64 %a.t, i32 2)"), "{out}");
    }

    #[test]
    fn unknown_forms_are_kept() {
        let ir = "define internal %V @g() {\nentry:\n  %p = phi %V [ %x, %a ]\n  ret %V %p\n}\n";
        assert_eq!(scalarize(ir), ir);
    }
}
