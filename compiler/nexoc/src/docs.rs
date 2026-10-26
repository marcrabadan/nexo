//! `nexo docs --for-ai [file]`: the exact list of what a program can use,
//! for a language model (section "Goals and guarantees", D130). The list
//! is nexo/stdlib/api.json, shared with phase 0; with a file, the
//! program's own functions and types follow, with their `##` comments.

use crate::json::{parse, Json};

const API: &str = include_str!("../../../nexo/stdlib/api.json");

const DEFS: &[&str] = &["fn ", "pub fn ", "type ", "pub type ", "trait ", "pub trait ", "action ", "workflow "];
const BLOCKS: &[&str] = &["type ", "pub type ", "trait ", "pub trait "];

fn strip_colon(t: &str) -> &str {
    t.strip_suffix(':').unwrap_or(t)
}

/// The definitions at the top of a file: their first line without the
/// colon, the `##` comments above them, and the fields and methods of a
/// type or trait.
pub fn outline(source: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut docs: Vec<String> = Vec::new();
    let mut inside = false;
    for line in source.lines() {
        let text = line.trim_end();
        if text.starts_with("##") {
            docs.push(text.to_string());
            inside = false;
            continue;
        }
        if DEFS.iter().any(|d| text.starts_with(d)) {
            out.push(strip_colon(text).to_string());
            out.extend(docs.drain(..).map(|d| format!("    {d}")));
            inside = BLOCKS.iter().any(|d| text.starts_with(d)) && text.ends_with(':');
            continue;
        }
        if inside && text.starts_with("    ") && !text.starts_with("     ") {
            let member = &text[4..];
            if member.starts_with("fn ") || member.starts_with("pub fn ") {
                out.push(format!("    {}", strip_colon(member)));
            } else if member.contains(':') && !member.starts_with('#') && !member.starts_with('@') {
                out.push(format!("    {member}"));
            }
            continue;
        }
        if !text.is_empty() && !text.starts_with(' ') {
            inside = false;
        }
        docs.clear();
    }
    out
}

fn text(j: &Json) -> String {
    j.str().unwrap_or("").to_string()
}

pub fn for_ai(file: Option<(&str, &str)>) -> String {
    let data = parse(API).expect("api.json is valid JSON");
    let mut lines = vec![
        format!("# Nexo {}: what a program can use", text(data.get("edition"))),
        String::new(),
        "Only these functions, values, types and methods exist; any other name is defined by the program.".to_string(),
        String::new(),
    ];
    for section in data.get("sections").arr() {
        lines.push(format!("## {}", text(section.get("title"))));
        for e in section.get("entries").arr() {
            lines.push(format!("{}  # {}", text(&e.arr()[0]), text(&e.arr()[1])));
        }
        lines.push(String::new());
    }
    lines.push("## Values".to_string());
    for e in data.get("values").arr() {
        lines.push(format!("{}  # {}", text(&e.arr()[0]), text(&e.arr()[1])));
    }
    lines.push(String::new());
    lines.push("## Types".to_string());
    lines.push(data.get("types").arr().iter().map(text).collect::<Vec<_>>().join(", "));
    lines.push(String::new());
    lines.push("## Methods".to_string());
    for e in data.get("methods").arr() {
        lines.push(format!("{}: {}", text(&e.arr()[0]), text(&e.arr()[1])));
    }
    lines.push(String::new());
    lines.push("## Not available in this edition".to_string());
    lines.push(data.get("unavailable").arr().iter().map(text).collect::<Vec<_>>().join(", "));
    if let Some((path, source)) = file {
        lines.push(String::new());
        lines.push(format!("## This program ({path})"));
        lines.extend(outline(source));
    }
    lines.join("\n") + "\n"
}

/// The declarations of a file as Markdown, as in phase 0: a heading for
/// each one, its `##` comments as a paragraph and its fields and methods as
/// a list; None when the file declares nothing (D147).
pub fn markdown(path: &str, source: &str) -> Option<String> {
    let mut lines = vec![format!("# {path}")];
    let last_is_item = |lines: &Vec<String>| lines.last().is_some_and(|l| l.starts_with("- "));
    for item in outline(source) {
        if !item.starts_with("    ") {
            lines.extend([String::new(), format!("## `{item}`"), String::new()]);
        } else if let Some(doc) = item.strip_prefix("    ##") {
            if last_is_item(&lines) {
                lines.push(String::new());
            }
            lines.push(doc.strip_prefix(' ').unwrap_or(doc).to_string());
        } else {
            if lines.last().is_some_and(|l| !l.is_empty()) && !last_is_item(&lines) {
                lines.push(String::new());
            }
            lines.push(format!("- `{}`", &item[4..]));
        }
    }
    if lines.len() == 1 {
        return None;
    }
    while lines.last().is_some_and(|l| l.is_empty()) {
        lines.pop();
    }
    Some(lines.join("\n") + "\n")
}
