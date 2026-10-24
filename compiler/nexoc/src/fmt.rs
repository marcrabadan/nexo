//! `nexo fmt` (section 14): the formatter. It has no options, so all Nexo
//! code looks the same (D85). The same rules as phase 0 (`nexo/fmt.py`):
//!
//! - it works on the lexer's tokens and keeps the line breaks the author
//!   chose; the formatted text has exactly the same tokens (checked);
//! - one space around binary operators, after `,` and `:`, none inside
//!   brackets, before the `(` / `[` of a call or index, around `.` and
//!   `..`, after unary `-` or before a postfix `?` / `!`;
//! - `|>` lines two spaces deeper than their statement, lines inside
//!   brackets four spaces deeper than the line that opened them;
//! - comments on their own line take the indentation of the code around
//!   them; trailing comments of consecutive lines are aligned;
//! - at most one blank line in a row, none at the start or right after a
//!   line that opens a block; one newline at the end.

use nexo_diag::Diagnostic;

use crate::lexer::{tokenize, StrPart, Tok, Token};

const OPENERS: &[&str] = &["(", "[", "{"];
const CLOSERS: &[&str] = &[")", "]", "}"];
const VALUE_WORDS: &[&str] = &["true", "false", "none", "self"];

/// A token as written in the source.
#[derive(Clone)]
struct Piece {
    tok: Tok,
    text: String,
    line: usize,
    end_line: usize,
    col: usize,
    end: usize,
}

/// A logical line of output, plus an optional trailing comment.
#[derive(Default)]
struct Row {
    pieces: Vec<Piece>,
    comment: Option<String>,
    indent: usize,
    blank_before: usize,
    /// inside the body of an infra, stage, pipeline or flags block (D114)
    cfg: bool,
}

fn is_synthetic(t: &Tok) -> bool {
    matches!(t, Tok::Newline | Tok::Indent | Tok::Dedent | Tok::Eof)
}

fn op_in(t: &Tok, ops: &[&str]) -> bool {
    matches!(t, Tok::Op(o) if ops.contains(o))
}

fn is_op(t: &Tok, op: &str) -> bool {
    matches!(t, Tok::Op(o) if *o == op)
}

/// The formatted text of `source`, or the diagnostic if it does not lex.
pub fn format_source(source: &str) -> Result<String, Diagnostic> {
    let source = source.replace("\r\n", "\n");
    let tokens = tokenize(&source)?;
    let lines: Vec<Vec<char>> = source.split('\n').map(|l| l.chars().collect()).collect();
    let rows = build_rows(&source, &lines, &tokens);
    let out = render_rows(&rows);
    let again = tokenize(&out).unwrap_or_else(|d| panic!("nexo fmt broke the file: {}", d.text()));
    let keys = |ts: &[Token]| ts.iter().map(|t| token_key(&t.tok)).collect::<Vec<_>>();
    assert!(keys(&tokens) == keys(&again), "nexo fmt changed the tokens");
    Ok(out)
}

fn token_key(t: &Tok) -> String {
    match t {
        Tok::Str(parts) => {
            let mut s = String::from("S");
            for p in parts {
                match p {
                    StrPart::Lit(l) => s.push_str(&format!("|L{l}")),
                    StrPart::Interp { source, .. } => s.push_str(&format!("|I{source}")),
                }
            }
            s
        }
        other => format!("{other:?}"),
    }
}

fn build_rows(source: &str, lines: &[Vec<char>], tokens: &[Token]) -> Vec<Row> {
    let all: Vec<char> = source.chars().collect();
    let mut offsets = vec![0usize];
    for line in lines {
        offsets.push(offsets.last().unwrap() + line.len() + 1);
    }
    let mut pieces = Vec::new();
    for t in tokens {
        if is_synthetic(&t.tok) {
            continue;
        }
        let line = t.span.line as usize;
        let col = t.span.col as usize - 1;
        let chars = &lines[line - 1];
        let triple = matches!(t.tok, Tok::Str(_)) && chars.len() >= col + 3 && chars[col..col + 3] == ['"', '"', '"'];
        let (text, end_line, end) = if triple {
            let start = offsets[line - 1] + col;
            let mut stop = start + 3;
            while !(all[stop] == '"' && all[stop + 1] == '"' && all[stop + 2] == '"') {
                stop += 1;
            }
            let text: String = all[start..stop + 3].iter().collect();
            let newlines = text.matches('\n').count();
            let end = match text.rfind('\n') {
                Some(_) => text.chars().rev().take_while(|&c| c != '\n').count(),
                None => col + text.chars().count(),
            };
            (text, line + newlines, end)
        } else {
            let end = (t.span.end_col as usize - 1).min(chars.len());
            (chars[col..end].iter().collect(), line, end)
        };
        pieces.push(Piece { tok: t.tok.clone(), text, line, end_line, col, end });
    }

    let mut rows = Vec::new();
    let mut covered = 0usize;
    let mut k = 0usize;
    for (idx, chars) in lines.iter().enumerate() {
        let n = idx + 1;
        if n <= covered {
            continue;
        }
        let mut row = Row { indent: chars.iter().take_while(|&&c| c == ' ').count(), ..Row::default() };
        let (mut cursor_line, mut cursor) = (n, 0usize);
        while k < pieces.len() && pieces[k].line == cursor_line {
            let p = pieces[k].clone();
            cursor_line = p.end_line;
            cursor = p.end;
            row.pieces.push(p);
            k += 1;
        }
        let rest: String = lines[cursor_line - 1][cursor.min(lines[cursor_line - 1].len())..].iter().collect();
        let rest = rest.trim();
        if rest.starts_with('#') {
            row.comment = Some(rest.trim_end().to_string());
        }
        covered = cursor_line;
        rows.push(row);
    }

    let mut result = Vec::new();
    let mut blanks = 0;
    for mut row in rows {
        if row.pieces.is_empty() && row.comment.is_none() {
            blanks += 1;
            continue;
        }
        row.blank_before = blanks;
        blanks = 0;
        result.push(row);
    }
    mark_cfg(&mut result);
    result
}

const CFG_BLOCKS: &[&str] = &["infra", "stage", "pipeline", "flags"];

/// Rows inside phase 5 blocks: there a space before `(` or `[` starts a
/// new word of the phrase, and `2027-03-01` is a date (D114).
fn mark_cfg(rows: &mut [Row]) {
    let mut inside = false;
    for row in rows.iter_mut() {
        if !row.pieces.is_empty() && row.indent == 0 {
            let first = &row.pieces[0].tok;
            let last = &row.pieces[row.pieces.len() - 1].tok;
            inside = matches!(first, Tok::Name(n) if CFG_BLOCKS.contains(&n.as_str())) && is_op(last, ":");
            continue;
        }
        row.cfg = inside && !row.pieces.is_empty();
    }
}

fn ends_operand(t: Option<&Tok>) -> bool {
    match t {
        Some(Tok::Name(_) | Tok::Int(_) | Tok::Float(_) | Tok::Unit { .. } | Tok::Str(_)) => true,
        Some(Tok::Keyword(k)) => VALUE_WORDS.contains(k),
        Some(t) => op_in(t, &[")", "]", "}", "?", "!"]),
        None => false,
    }
}

/// The space between two tokens of the same row; `prev` comes before `a`.
fn gap(prev: Option<&Piece>, a: &Piece, b: &Piece, cfg: bool) -> &'static str {
    let same_place = a.end_line == b.line && a.end == b.col;
    if cfg
        && same_place
        && ((matches!(a.tok, Tok::Int(_)) && is_op(&b.tok, "-"))
            || (is_op(&a.tok, "-") && matches!(b.tok, Tok::Int(_)) && prev.is_some_and(|p| matches!(p.tok, Tok::Int(_)))))
    {
        return ""; // a date: 2027-03-01
    }
    if cfg && op_in(&b.tok, &["(", "["]) && ends_operand(Some(&a.tok)) {
        return if same_place { "" } else { " " };
    }
    if matches!(a.tok, Tok::Keyword("test")) && is_op(&b.tok, "(") {
        return ""; // test() in a step of a pipeline
    }
    if op_in(&b.tok, &[")", "]", "}", ",", ".", ":", "?", "!", ".."]) {
        return "";
    }
    if op_in(&a.tok, &["(", "[", "{", ".", "@", ".."]) {
        return "";
    }
    if is_op(&a.tok, "-") && !ends_operand(prev.map(|p| &p.tok)) {
        return ""; // unary minus
    }
    if op_in(&b.tok, &["(", "["]) {
        if matches!(a.tok, Tok::Name(_)) && prev.is_some_and(|p| is_op(&p.tok, "|>")) {
            // a pipeline step: `|> save(...)` is a call, `|> map [..]` is not
            let same_place = a.end_line == b.line && a.end == b.col;
            return if same_place { "" } else { " " };
        }
        if ends_operand(Some(&a.tok)) {
            return "";
        }
    }
    " "
}

fn row_code(before: Option<&Piece>, row: &Row) -> String {
    let mut out = String::new();
    for (i, piece) in row.pieces.iter().enumerate() {
        if i > 0 {
            let prev = if i >= 2 { Some(&row.pieces[i - 2]) } else { before };
            out.push_str(gap(prev, &row.pieces[i - 1], piece, row.cfg));
        }
        out.push_str(&piece.text);
    }
    out
}

fn render_rows(rows: &[Row]) -> String {
    let indents = row_indents(rows);
    let mut texts: Vec<(String, Option<String>)> = Vec::new();
    let mut blank_before = Vec::new();
    let mut before: Option<&Piece> = None;
    let mut depth: i64 = 0;
    let mut opened_block = false;
    for (i, row) in rows.iter().enumerate() {
        blank_before.push(row.blank_before > 0 && !texts.is_empty() && !opened_block);
        let code = if row.pieces.is_empty() { String::new() } else { row_code(before, row) };
        texts.push((format!("{}{}", " ".repeat(indents[i]), code), row.comment.clone()));
        if row.pieces.is_empty() {
            opened_block = false;
            continue;
        }
        for p in &row.pieces {
            if op_in(&p.tok, OPENERS) {
                depth += 1;
            } else if op_in(&p.tok, CLOSERS) {
                depth -= 1;
            }
        }
        before = row.pieces.last();
        opened_block = depth == 0 && before.is_some_and(|p| is_op(&p.tok, ":"));
    }
    join_lines(&texts, &blank_before)
}

fn is_continuation_start(row: &Row) -> bool {
    row.pieces.first().is_some_and(|p| is_op(&p.tok, "|>"))
}

fn row_indents(rows: &[Row]) -> Vec<usize> {
    let mut indents = vec![0usize; rows.len()];
    let mut stack: Vec<usize> = Vec::new();
    let mut statement_indent = 0usize;
    let mut pending: Vec<usize> = Vec::new();
    for (i, row) in rows.iter().enumerate() {
        if row.pieces.is_empty() {
            pending.push(i);
            continue;
        }
        let first = &row.pieces[0].tok;
        let indent = if let Some(&top) = stack.last() {
            if op_in(first, CLOSERS) {
                top
            } else {
                top + 4
            }
        } else if is_op(first, "|>") {
            statement_indent + 2
        } else {
            row.indent
        };
        for &c in &pending {
            indents[c] = comment_indent(&rows[c], &stack, indent, statement_indent, Some(row));
        }
        pending.clear();
        if stack.is_empty() && !is_op(first, "|>") {
            statement_indent = indent;
        }
        indents[i] = indent;
        for p in &row.pieces {
            if op_in(&p.tok, OPENERS) {
                stack.push(indent);
            } else if op_in(&p.tok, CLOSERS) {
                stack.pop();
            }
        }
    }
    for &c in &pending {
        indents[c] = comment_indent(&rows[c], &stack, 0, statement_indent, None);
    }
    indents
}

/// Indentation of a comment on its own line.
fn comment_indent(comment: &Row, stack: &[usize], next_indent: usize, prev_indent: usize, next_row: Option<&Row>) -> usize {
    if let Some(&top) = stack.last() {
        return top + 4;
    }
    if next_row.is_some_and(is_continuation_start) {
        return next_indent;
    }
    let original = comment.indent;
    if original <= next_indent {
        return next_indent;
    }
    let deepest = prev_indent.max(next_indent);
    next_indent.max((original - original % 4).min(deepest))
}

fn last_width(code: &str) -> usize {
    match code.rfind('\n') {
        Some(i) => code[i + 1..].chars().count(),
        None => code.chars().count(),
    }
}

fn join_lines(rows: &[(String, Option<String>)], blank_before: &[bool]) -> String {
    let n = rows.len();
    let mut aligned: Vec<String> = Vec::new();
    let mut i = 0;
    while i < n {
        let (code, comment) = &rows[i];
        if let (Some(_), false) = (comment, code.trim().is_empty()) {
            let mut j = i;
            if !code.contains('\n') {
                while j + 1 < n
                    && rows[j + 1].1.is_some()
                    && !rows[j + 1].0.trim().is_empty()
                    && !blank_before[j + 1]
                    && !rows[j + 1].0.contains('\n')
                {
                    j += 1;
                }
            }
            let width = (i..=j).map(|k| last_width(&rows[k].0)).max().unwrap_or(0);
            for row in &rows[i..=j] {
                let (c, com) = row;
                aligned.push(format!("{c}{}{}", " ".repeat(width - last_width(c) + 2), com.as_deref().unwrap_or("")));
            }
            i = j + 1;
            continue;
        }
        match comment {
            Some(com) => aligned.push(format!("{code}{com}")),
            None if code.contains('\n') => aligned.push(code.clone()),
            None => aligned.push(code.trim_end().to_string()),
        }
        i += 1;
    }
    let mut out = String::new();
    for (k, text) in aligned.iter().enumerate() {
        if blank_before[k] {
            out.push('\n');
        }
        out.push_str(text);
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::format_source;

    #[test]
    fn spaces_and_continuations() {
        let src = "x=[ 1,2 ]\ny = x\n      |> map it*2\nprint( -y [0] )   # p\n\n\n";
        assert_eq!(format_source(src).unwrap(), "x = [1, 2]\ny = x\n  |> map it * 2\nprint(-y[0])  # p\n");
    }

    #[test]
    fn keeps_tokens_and_is_idempotent() {
        let src = "fn f(x):\n\n    # c\n    return x  # r\n";
        let once = format_source(src).unwrap();
        assert_eq!(once, "fn f(x):\n    # c\n    return x  # r\n");
        assert_eq!(format_source(&once).unwrap(), once);
    }
}
