//! Development mode compiles a large program in parallel (D135): its IR
//! is cut into parts that clang compiles at the same time, then they are
//! linked together. Every part has the shared header (types, declarations
//! of the runtime, the small helpers marked `#0`); constants and globals
//! are defined once, in the first part, and declared in the others, so
//! each keeps a single address; functions are spread over the parts, and
//! each part declares the functions the others define. A release build is
//! never cut: LLVM optimises the whole program at once.

/// The type at the start of `s` (`%V`, `ptr`, `[3 x { i64, ptr }]`...) and
/// the text after it.
fn split_type(s: &str) -> (&str, &str) {
    let b = s.as_bytes();
    if b.first() == Some(&b'[') || b.first() == Some(&b'{') {
        let mut depth = 0i32;
        for (i, c) in b.iter().enumerate() {
            match c {
                b'[' | b'{' => depth += 1,
                b']' | b'}' => {
                    depth -= 1;
                    if depth == 0 {
                        return (&s[..=i], &s[i + 1..]);
                    }
                }
                _ => {}
            }
        }
        return (s, "");
    }
    match s.find(' ') {
        Some(i) => (&s[..i], &s[i..]),
        None => (s, ""),
    }
}

/// `@name = private [unnamed_addr] constant TYPE ...` or `@name = internal
/// global TYPE ...`: (name, the definition for the first part, the
/// declaration for the others).
fn data_line(line: &str) -> Option<(String, String)> {
    let (name, rest) = line.split_once(" = ")?;
    let (kind, after) = if let Some(r) = rest.strip_prefix("private unnamed_addr constant ") {
        ("constant", r)
    } else if let Some(r) = rest.strip_prefix("private constant ") {
        ("constant", r)
    } else if let Some(r) = rest.strip_prefix("internal global ") {
        ("global", r)
    } else {
        return None;
    };
    let (ty, init) = split_type(after);
    Some((format!("{name} = hidden {kind} {ty}{init}"), format!("{name} = external hidden {kind} {ty}")))
}

/// `define [internal] RET @name(T a, U b) {` as a declaration.
fn declaration(define: &str) -> Option<(String, String)> {
    let rest = define.strip_prefix("define ")?;
    let rest = rest.strip_prefix("internal ").unwrap_or(rest);
    let at = rest.find(" @")?;
    let ret = &rest[..at];
    let after = &rest[at + 1..];
    let open = after.find('(')?;
    let name = &after[..open];
    let close = after.rfind(')')?;
    let params: Vec<&str> = after[open + 1..close]
        .split(", ")
        .filter(|p| !p.is_empty())
        .map(|p| p.rsplit_once(' ').map(|(t, _)| t).unwrap_or(p))
        .collect();
    let hidden = if define.starts_with("define internal ") { "hidden " } else { "" };
    let header = define.replacen("define internal ", "define hidden ", 1);
    Some((header, format!("declare {hidden}{ret} {name}({})", params.join(", "))))
}

/// The IR cut into `n` modules (at least one).
pub fn split(ir: &str, n: usize) -> Vec<String> {
    let mut header = String::new();
    let mut defs = String::new();
    let mut decls = String::new();
    // (header line, body, declaration)
    let mut funcs: Vec<(String, String, String)> = Vec::new();
    let mut lines = ir.lines();
    while let Some(line) = lines.next() {
        if line.starts_with("define ") && !line.contains(" #0 {") {
            let (head, decl) = declaration(line).unwrap_or_else(|| (line.to_string(), String::new()));
            let mut body = String::new();
            for l in lines.by_ref() {
                body.push_str(l);
                body.push('\n');
                if l == "}" {
                    break;
                }
            }
            funcs.push((head, body, decl));
        } else if let Some((def, decl)) = line.starts_with('@').then(|| data_line(line)).flatten() {
            defs.push_str(&def);
            defs.push('\n');
            decls.push_str(&decl);
            decls.push('\n');
        } else {
            header.push_str(line);
            header.push('\n');
        }
    }
    let n = n.clamp(1, funcs.len().max(1));
    // the largest functions first, each to the part with the least work:
    // the first part also reads the program's data, and clang takes longer
    // per line on a very large function (the top level of a big program)
    let cost = |len: usize| len + len / 1_000 * len / 40_000;
    let mut order: Vec<usize> = (0..funcs.len()).collect();
    order.sort_by_key(|&i| std::cmp::Reverse(funcs[i].1.len()));
    let mut owner = vec![0usize; funcs.len()];
    let mut sizes = vec![0usize; n];
    sizes[0] = defs.len();
    for i in order {
        let p = (0..n).min_by_key(|&p| sizes[p]).unwrap();
        owner[i] = p;
        sizes[p] += cost(funcs[i].1.len());
    }
    (0..n)
        .map(|p| {
            let mut out = header.clone();
            out.push_str(if p == 0 { &defs } else { &decls });
            for (i, (head, body, decl)) in funcs.iter().enumerate() {
                if owner[i] == p {
                    out.push_str(head);
                    out.push('\n');
                    out.push_str(body);
                } else if !decl.is_empty() {
                    out.push_str(decl);
                    out.push('\n');
                }
            }
            out
        })
        .collect()
}
