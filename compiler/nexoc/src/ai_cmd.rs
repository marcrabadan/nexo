//! `nexo ai setup` and `nexo ai usage` (section 8, D106-D107), as phase 0's
//! ai.py writes them.

use std::collections::BTreeMap;
use std::io::{BufRead, Write};
use std::path::PathBuf;

use nexo_diag::{msg, render, suggest, Diagnostic};

const PROVIDERS: &[&str] = &["anthropic", "openai", "ollama"];

fn config_dir() -> PathBuf {
    match std::env::var_os("XDG_CONFIG_HOME").filter(|v| !v.is_empty()) {
        Some(base) => PathBuf::from(base).join("nexo"),
        None => PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".config").join("nexo"),
    }
}

fn quote(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn ask_line(key: &str) -> String {
    print!("{}", msg(key, &[]));
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    let _ = std::io::stdin().lock().read_line(&mut line);
    line.trim().to_string()
}

fn fail(d: Diagnostic) -> u8 {
    eprint!("{}", render("", "", None, &d.text()));
    1
}

/// The user configuration, asked for on the terminal and saved in ai.toml
/// (the key is the name of an environment variable, never the key itself).
pub fn setup() -> u8 {
    let provider = ask_line("info.ai_setup_provider");
    if !PROVIDERS.contains(&provider.as_str()) {
        return fail(match suggest(&provider, PROVIDERS.iter().copied()) {
            Some(s) => Diagnostic::new_spanless("ai_provider").fix("suggest").arg("provider", provider).arg("suggestion", s),
            None => Diagnostic::new_spanless("ai_provider").arg("provider", provider),
        });
    }
    let model = ask_line("info.ai_setup_model");
    if model.is_empty() || model.contains(':') {
        return fail(Diagnostic::new_spanless("ai_model").arg("model", model));
    }
    let mut lines = vec!["[ai]".to_string(), format!("provider = {}", quote(&provider)), format!("model = {}", quote(&model))];
    if provider != "ollama" {
        let var = ask_line("info.ai_setup_key");
        let ok = var.chars().next().is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
            && var.chars().all(|c| c.is_ascii_alphanumeric() || c == '_');
        if !ok {
            return fail(Diagnostic::new_spanless("ai_setup_var").arg("name", var));
        }
        lines.push(format!("key = {}", quote(&var)));
    }
    let url = ask_line("info.ai_setup_url");
    if !url.is_empty() {
        lines.push(format!("url = {}", quote(&url)));
    }
    let path = config_dir().join("ai.toml");
    let written = std::fs::create_dir_all(config_dir()).and_then(|_| std::fs::write(&path, lines.join("\n") + "\n"));
    if let Err(e) = written {
        let text = e.to_string();
        let reason = text.split(" (os error").next().unwrap_or(&text).to_string();
        eprintln!("Error: {}.", msg("err.IOError", &[("path", path.display().to_string()), ("reason", reason)]));
        return 1;
    }
    println!("{}", msg("info.ai_saved", &[("path", path.display().to_string())]));
    0
}

/// Calls and tokens per day and model.
pub fn usage() -> u8 {
    let text = std::fs::read_to_string(config_dir().join("usage.csv")).unwrap_or_default();
    let mut totals: BTreeMap<(String, String), [u64; 3]> = BTreeMap::new();
    for line in text.lines() {
        let parts: Vec<&str> = line.split(',').collect();
        let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
        if parts.len() != 4 || !digits(parts[2]) || !digits(parts[3]) {
            continue;
        }
        let t = totals.entry((parts[0].to_string(), parts[1].to_string())).or_insert([0; 3]);
        t[0] += 1;
        t[1] += parts[2].parse::<u64>().unwrap_or(0);
        t[2] += parts[3].parse::<u64>().unwrap_or(0);
    }
    if totals.is_empty() {
        println!("{}", msg("info.ai_usage_none", &[]));
        return 0;
    }
    for ((day, model), [calls, input, output]) in totals {
        // the spend, when the price of the model is known (D165)
        let spent = match nexo_runtime::ai::usage_spent(&model, input as i64, output as i64) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("Error: {e}");
                return 1;
            }
        };
        let args = [
            ("day", day),
            ("model", model),
            ("calls", calls.to_string()),
            ("input", input.to_string()),
            ("output", output.to_string()),
        ];
        let mut row = msg("info.ai_usage_row", &args);
        if let Some(s) = spent {
            row.push_str(&msg("info.ai_usage_spent", &[("spent", s)]));
        }
        println!("{row}");
    }
    0
}
