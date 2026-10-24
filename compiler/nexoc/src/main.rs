//! `nexo`, the compiler driver (sections 7 and 14):
//!
//!     nexo file.nexo                     compile in development mode and run
//!     nexo build [--release] file.nexo   compile to a native program
//!         --trust-ensure                 (with --release) leave out unproven ensure checks
//!     nexo test [paths...]               run the `test` blocks
//!     nexo fmt [paths...]                format files in place (D85)
//!     nexo lint [paths...]               warn about questionable code (D86)
//!     nexo fix [paths...]                apply the safe fixes
//!     nexo check [paths...]              the compile checks, without running anything
//!     nexo lsp                           language server on standard input and output (D87)
//!     --lang es|en                       language of messages (Spanish by default)

mod ast;
mod checker;
mod codegen;
mod fmt;
mod json;
mod lexer;
mod lint;
mod lsp;
mod narrow;
mod parser;
mod prove;
mod resolve;
mod typecheck;

use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};

use nexo_diag::{msg, render, Diagnostic, Lang};

fn usage() -> &'static str {
    if nexo_diag::lang() == Lang::En {
        "Usage: nexo [--lang es|en] [file.nexo | build [--release [--trust-ensure]] [-o out] file.nexo | test [paths...]]"
    } else {
        "Uso: nexo [--lang es|en] [archivo.nexo | build [--release [--trust-ensure]] [-o salida] archivo.nexo | test [rutas...]]"
    }
}

/// A file that failed to compile, already rendered in the section 14 format.
struct CompileError(String);

/// Compile Nexo source to LLVM IR.
fn to_ir(source: &str, name: &str, test_mode: bool, trust_ensure: bool) -> Result<String, CompileError> {
    let fail = |d: Diagnostic| CompileError(render(name, source, d.span, &d.text()));
    let program = parser::parse(source).map_err(fail)?;
    checker::check(&program).map_err(fail)?;
    let res = resolve::resolve(&program).map_err(fail)?;
    typecheck::check(&program, &res).map_err(fail)?;
    let lang = if nexo_diag::lang() == Lang::En { 1 } else { 0 };
    let opts = codegen::Options { file: name.to_string(), lang, test_mode, trust_ensure };
    codegen::Gen::new(source, &res, &opts).program(&program).map_err(fail)
}

/// The checks of a compilation that happen before code generation.
fn front_end(source: &str) -> Result<ast::Program, Diagnostic> {
    let program = parser::parse(source)?;
    checker::check(&program)?;
    let res = resolve::resolve(&program)?;
    typecheck::check(&program, &res)?;
    Ok(program)
}

/// The warnings of a file, or the compile error that stops it (D86).
fn lint_source(source: &str) -> Result<Vec<lint::Finding>, Diagnostic> {
    let program = front_end(source)?;
    let tokens = lexer::tokenize(source)?;
    Ok(lint::Linter::new(source, &tokens, &program).run())
}

/// Apply every safe fix, again and again until none is left.
fn fix_source(mut source: String) -> String {
    for _ in 0..100 {
        match lint_source(&source) {
            Err(d) => match lint::error_fix(&d, &source) {
                Some(fix) if fix.safe => source = lint::apply_edits(&source, &fix.edits),
                _ => break,
            },
            Ok(findings) => {
                let safe: Vec<&lint::Fix> = findings.iter().filter_map(|f| f.fix.as_ref()).filter(|f| f.safe).collect();
                if safe.is_empty() {
                    break;
                }
                // one fix per round when they could overlap
                let mut chosen: Vec<&lint::Fix> = Vec::new();
                let mut taken: Vec<(u32, u32)> = Vec::new();
                for fix in safe {
                    let (lo, hi) = (fix.edits[0].line, fix.edits.last().unwrap().end_line);
                    if taken.iter().all(|&(a, b)| hi < a || lo > b) {
                        chosen.push(fix);
                        taken.push((lo, hi));
                    }
                }
                let edits: Vec<lint::Edit> = chosen.iter().flat_map(|f| f.edits.iter().cloned()).collect();
                source = lint::apply_edits(&source, &edits);
            }
        }
    }
    source
}

/// The runtime library: next to this executable, or NEXO_RUNTIME.
fn runtime_lib() -> PathBuf {
    if let Ok(p) = std::env::var("NEXO_RUNTIME") {
        return PathBuf::from(p);
    }
    let exe = std::env::current_exe().unwrap_or_default();
    exe.parent().map(|d| d.join("libnexo_runtime.a")).unwrap_or_default()
}

fn work_dir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!("nexo-{}", std::process::id()));
    let _ = std::fs::create_dir_all(&dir);
    dir
}

/// Compile IR and link it with the runtime into a native executable.
fn link(ir: &str, out: &Path, release: bool) -> Result<(), String> {
    let dir = work_dir();
    let ll = dir.join(format!("{}.ll", out.file_name().unwrap_or_default().to_string_lossy()));
    std::fs::write(&ll, ir).map_err(|e| e.to_string())?;
    let cc = std::env::var("NEXO_CC").unwrap_or_else(|_| "clang".into());
    let opt = if release { "-O2" } else { "-O1" };
    let output = Command::new(&cc)
        .arg(opt)
        .arg("-Wno-override-module")
        .arg(&ll)
        .arg(runtime_lib())
        .args(["-o"])
        .arg(out)
        .args(["-lgcc_s", "-lutil", "-lrt", "-lpthread", "-lm", "-ldl", "-lc"])
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("{cc}: {e}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).into_owned());
    }
    // NEXO_KEEP_IR=1 keeps the IR next to the program (out.ll)
    if std::env::var("NEXO_KEEP_IR").is_ok() {
        let _ = std::fs::copy(&ll, out.with_extension("ll"));
    }
    let _ = std::fs::remove_file(&ll);
    Ok(())
}

fn read_source(path: &str) -> Result<String, String> {
    std::fs::read_to_string(path).map_err(|_| format!("Error: {}.\n", msg("err.NotFound", &[("path", path.to_string())])))
}

fn exit_code(status: std::process::ExitStatus) -> u8 {
    status.code().map(|c| c as u8).unwrap_or(1)
}

/// `nexo file.nexo`: compile in development mode and run.
fn run_file(path: &str) -> u8 {
    let source = match read_source(path) {
        Ok(s) => s,
        Err(e) => {
            eprint!("{e}");
            return 1;
        }
    };
    let ir = match to_ir(&source, path, false, false) {
        Ok(ir) => ir,
        Err(CompileError(text)) => {
            eprint!("{text}");
            return 1;
        }
    };
    let dir = work_dir();
    let exe = dir.join("program");
    if let Err(e) = link(&ir, &exe, false) {
        eprintln!("Error: {e}");
        return 1;
    }
    let status = Command::new(&exe).status();
    let _ = std::fs::remove_dir_all(&dir);
    match status {
        Ok(s) => exit_code(s),
        Err(e) => {
            eprintln!("Error: {e}");
            1
        }
    }
}

/// `nexo build [--release] [-o out] file.nexo`
fn build(args: &[String]) -> u8 {
    let mut release = false;
    let mut trust_ensure = false;
    let mut out: Option<String> = None;
    let mut file: Option<String> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--release" => release = true,
            // unproven `ensure` checks are left out (Goals and guarantees)
            "--trust-ensure" => trust_ensure = true,
            "-o" if i + 1 < args.len() => {
                out = Some(args[i + 1].clone());
                i += 1;
            }
            a if file.is_none() && !a.starts_with('-') => file = Some(a.to_string()),
            _ => {
                eprintln!("{}", usage());
                return 2;
            }
        }
        i += 1;
    }
    let Some(file) = file else {
        eprintln!("{}", usage());
        return 2;
    };
    if trust_ensure && !release {
        eprintln!("{}", usage());
        return 2;
    }
    let source = match read_source(&file) {
        Ok(s) => s,
        Err(e) => {
            eprint!("{e}");
            return 1;
        }
    };
    let ir = match to_ir(&source, &file, false, trust_ensure) {
        Ok(ir) => ir,
        Err(CompileError(text)) => {
            eprint!("{text}");
            return 1;
        }
    };
    let out = out.unwrap_or_else(|| Path::new(&file).with_extension("").to_string_lossy().into_owned());
    if let Err(e) = link(&ir, Path::new(&out), release) {
        eprintln!("Error: {e}");
        return 1;
    }
    let _ = std::fs::remove_dir_all(work_dir());
    0
}

fn summary(total: u32, passed: u32, failed: u32) -> String {
    if nexo_diag::lang() == Lang::Es {
        format!(
            "{total} {}: {passed} {}, {failed} {}",
            if total == 1 { "prueba" } else { "pruebas" },
            if passed == 1 { "correcta" } else { "correctas" },
            if failed == 1 { "fallida" } else { "fallidas" }
        )
    } else {
        format!("{total} {}: {passed} passed, {failed} failed", if total == 1 { "test" } else { "tests" })
    }
}

fn test_files(paths: &[String]) -> Vec<PathBuf> {
    fn walk(dir: &Path, base: &Path, out: &mut Vec<PathBuf>) {
        let Ok(entries) = std::fs::read_dir(dir) else { return };
        let mut entries: Vec<PathBuf> = entries.filter_map(|e| e.ok().map(|e| e.path())).collect();
        entries.sort();
        for p in entries {
            let hidden = p.strip_prefix(base).ok().is_some_and(|r| {
                r.components().any(|c| c.as_os_str().to_string_lossy().starts_with('.'))
            });
            if hidden {
                continue;
            }
            if p.is_dir() {
                walk(&p, base, out);
            } else if p.extension().is_some_and(|e| e == "nexo") {
                out.push(p);
            }
        }
    }
    let mut found = Vec::new();
    let defaults = vec![".".to_string()];
    for p in if paths.is_empty() { &defaults } else { paths } {
        let path = PathBuf::from(p);
        if path.is_dir() {
            let mut files = Vec::new();
            walk(&path, &path, &mut files);
            files.sort();
            found.extend(files);
        } else {
            found.push(path);
        }
    }
    found
}

/// `nexo test [paths]`: run the top level of each file, then its tests (D54).
fn run_tests(paths: &[String]) -> u8 {
    let files = test_files(paths);
    let (mut total, mut passed, mut broken) = (0u32, 0u32, 0u32);
    for f in &files {
        let display = f.to_string_lossy().into_owned();
        let source = match read_source(&display) {
            Ok(s) => s,
            Err(e) => {
                eprint!("{e}");
                broken += 1;
                continue;
            }
        };
        if !source.contains("test") {
            continue;
        }
        let name = if paths.is_empty() {
            display.strip_prefix("./").unwrap_or(&display).to_string()
        } else {
            display.clone()
        };
        let ir = match to_ir(&source, &name, true, false) {
            Ok(ir) => ir,
            Err(CompileError(text)) => {
                eprint!("{text}");
                broken += 1;
                continue;
            }
        };
        let dir = work_dir();
        let exe = dir.join("tests");
        if let Err(e) = link(&ir, &exe, false) {
            eprintln!("Error: {e}");
            broken += 1;
            continue;
        }
        let report = dir.join("report");
        let _ = std::fs::remove_file(&report);
        let mut cmd = Command::new(&exe);
        cmd.env("NEXO_TEST_REPORT", &report);
        if files.len() > 1 {
            cmd.env("NEXO_TEST_HEADER", &name);
        }
        let ok = cmd.status().map(|s| s.success()).unwrap_or(false);
        let counts = std::fs::read_to_string(&report).unwrap_or_default();
        let nums: Vec<u32> = counts.split_whitespace().filter_map(|n| n.parse().ok()).collect();
        if !ok || nums.len() != 2 {
            broken += 1;
            continue;
        }
        total += nums[0];
        passed += nums[1];
    }
    let _ = std::fs::remove_dir_all(work_dir());
    if total == 0 && broken == 0 {
        let shown = if paths.is_empty() { ".".to_string() } else { paths.join(", ") };
        println!("{}", msg("no_tests", &[("path", shown)]));
        return 0;
    }
    println!("{}", summary(total, passed, total - passed));
    if passed == total && broken == 0 {
        0
    } else {
        1
    }
}

/// `nexo fmt [paths]`: format the files in place and print the ones it changed.
fn run_fmt(paths: &[String]) -> u8 {
    let mut status = 0;
    for f in test_files(paths) {
        let display = f.to_string_lossy().into_owned();
        let source = match read_source(&display) {
            Ok(s) => s,
            Err(e) => {
                eprint!("{e}");
                status = 1;
                continue;
            }
        };
        let name = if paths.is_empty() { display.strip_prefix("./").unwrap_or(&display).to_string() } else { display.clone() };
        match fmt::format_source(&source) {
            Ok(formatted) => {
                if formatted != source {
                    if let Err(e) = std::fs::write(&f, &formatted) {
                        eprintln!("Error: {e}");
                        status = 1;
                        continue;
                    }
                    println!("{name}");
                }
            }
            Err(d) => {
                eprint!("{}", render(&name, &source, d.span, &d.text()));
                status = 1;
            }
        }
    }
    status
}

/// `nexo lint [paths]`: every warning with its fix, then a summary.
fn run_lint(paths: &[String]) -> u8 {
    let (mut total, mut safe, mut status) = (0usize, 0usize, 0u8);
    for f in test_files(paths) {
        let display = f.to_string_lossy().into_owned();
        let source = match read_source(&display) {
            Ok(s) => s,
            Err(e) => {
                eprint!("{e}");
                status = 1;
                continue;
            }
        };
        let name = if paths.is_empty() { display.strip_prefix("./").unwrap_or(&display).to_string() } else { display.clone() };
        match lint_source(&source) {
            Ok(findings) => {
                for finding in &findings {
                    print!("{}", nexo_diag::render_warning(&name, &source, finding.span, &finding.text()));
                    total += 1;
                    safe += finding.fix.as_ref().is_some_and(|f| f.safe) as usize;
                }
            }
            Err(d) => {
                eprint!("{}", render(&name, &source, d.span, &d.text()));
                status = 1;
            }
        }
    }
    if total > 0 {
        println!("{}", msg("lint_summary", &[("n", total.to_string()), ("safe", safe.to_string())]));
        return 1;
    }
    if status == 0 {
        let shown = if paths.is_empty() { ".".to_string() } else { paths.join(", ") };
        println!("{}", msg("lint_none", &[("path", shown)]));
    }
    status
}

/// `nexo fix [paths]`: apply the safe fixes in place and print the files changed.
fn run_fix(paths: &[String]) -> u8 {
    let mut status = 0;
    for f in test_files(paths) {
        let display = f.to_string_lossy().into_owned();
        let source = match read_source(&display) {
            Ok(s) => s,
            Err(e) => {
                eprint!("{e}");
                status = 1;
                continue;
            }
        };
        let name = if paths.is_empty() { display.strip_prefix("./").unwrap_or(&display).to_string() } else { display.clone() };
        let fixed = fix_source(source.clone());
        if let Err(d) = lexer::tokenize(&fixed) {
            eprint!("{}", render(&name, &fixed, d.span, &d.text()));
            status = 1;
            continue;
        }
        if fixed != source {
            if let Err(e) = std::fs::write(&f, &fixed) {
                eprintln!("Error: {e}");
                status = 1;
                continue;
            }
            println!("{name}");
        }
    }
    status
}

/// `nexo check [paths]`: report the compile error of each file, if any.
fn run_check(paths: &[String]) -> u8 {
    let mut status = 0;
    for f in test_files(paths) {
        let display = f.to_string_lossy().into_owned();
        let source = match read_source(&display) {
            Ok(s) => s,
            Err(e) => {
                eprint!("{e}");
                status = 1;
                continue;
            }
        };
        let name = if paths.is_empty() { display.strip_prefix("./").unwrap_or(&display).to_string() } else { display.clone() };
        if let Err(d) = front_end(&source) {
            eprint!("{}", render(&name, &source, d.span, &d.text()));
            status = 1;
        }
    }
    if status == 0 {
        let shown = if paths.is_empty() { ".".to_string() } else { paths.join(", ") };
        println!("{}", msg("check_ok", &[("path", shown)]));
    }
    status
}

/// Print the LLVM IR of a program (for the compiler's own tests).
fn emit_ir(path: &str) -> u8 {
    let source = match read_source(path) {
        Ok(s) => s,
        Err(e) => {
            eprint!("{e}");
            return 1;
        }
    };
    match to_ir(&source, path, false, false) {
        Ok(ir) => {
            print!("{ir}");
            0
        }
        Err(CompileError(text)) => {
            eprint!("{text}");
            1
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut lang = "es".to_string();
    let mut lang_given = false;
    let mut rest = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if a == "--lang" && i + 1 < args.len() {
            lang = args[i + 1].clone();
            lang_given = true;
            i += 2;
            continue;
        }
        if let Some(l) = a.strip_prefix("--lang=") {
            lang = l.to_string();
            lang_given = true;
            i += 1;
            continue;
        }
        rest.push(a.clone());
        i += 1;
    }
    let parsed = nexo_diag::parse_lang(&lang);
    nexo_diag::set_lang(parsed.unwrap_or(Lang::Es));
    let code = if parsed.is_none() || rest.is_empty() {
        eprintln!("{}", usage());
        2
    } else {
        match rest[0].as_str() {
            "test" => run_tests(&rest[1..]),
            "build" => build(&rest[1..]),
            "fmt" => run_fmt(&rest[1..]),
            "lint" => run_lint(&rest[1..]),
            "fix" => run_fix(&rest[1..]),
            "check" => run_check(&rest[1..]),
            "lsp" if rest.len() == 1 => lsp::serve(lang_given),
            "--emit-llvm" if rest.len() == 2 => emit_ir(&rest[1]),
            a if a.starts_with('-') || rest.len() > 1 => {
                eprintln!("{}", usage());
                2
            }
            _ => run_file(&rest[0]),
        }
    };
    ExitCode::from(code)
}
