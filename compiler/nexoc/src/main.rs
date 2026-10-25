//! `nexo`, the compiler driver (sections 7 and 14):
//!
//!     nexo                               the REPL (D88)
//!     nexo file.nexo                     compile in development mode and run
//!     nexo build [--release] file.nexo   compile to a native program
//!         --trust-ensure                 (with --release) leave out unproven ensure checks
//!     nexo test [paths...]               run the `test` blocks
//!     nexo fmt [paths...]                format files in place (D85)
//!     nexo lint [paths...]               warn about questionable code (D86)
//!     nexo fix [paths...]                apply the safe fixes
//!     nexo check [paths...]              the compile checks, without running anything
//!     nexo lsp                           language server on standard input and output (D87)
//!     nexo db diff|migrate file.nexo     migrations of nexo.db (D101): --db path, --allow-drop

mod ai_cmd;
mod audit;
mod ast;
mod cheader;
mod checker;
mod codegen;
mod docs;
mod fmt;
mod intent;
mod json;
mod lexer;
mod lint;
mod lsp;
mod modules;
mod narrow;
mod parser;
mod phase5;
mod prove;
mod resolve;
mod sbom;
mod split;
mod suggest;
mod typecheck;

use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};

use nexo_diag::{msg, render, Diagnostic};

pub fn usage() -> &'static str {
    "Usage: nexo [file.nexo | build [--release [--trust-ensure]] [-o out] file.nexo | test|fmt|lint|fix|check|audit [paths...] | test --suggest file.nexo | check --intent [paths...] | docs --for-ai [file.nexo] | lsp | db diff|migrate file.nexo [--db path] [--allow-drop] | ai setup|usage | plan|apply|destroy|drift|cost [file.nexo] [--stage stage] [--yes] | import cloud resource id [file.nexo] | ci run [pipeline] [file.nexo] [--step step | --after success|failure] [--yes] | ci export github|gitlab|azure|jenkins [pipeline] [file.nexo] | flags set name value [file.nexo] [--stage stage] | privacy report [file.nexo]]"
}

/// A file that failed to compile, already rendered in the section 14 format.
struct CompileError(String);

/// Compile Nexo source to LLVM IR.
fn to_ir(source: &str, name: &str, test_mode: bool, trust_ensure: bool) -> Result<String, CompileError> {
    to_ir_program(source, name, test_mode, trust_ensure).map(|(ir, _)| ir)
}

/// The IR and the program it was made from, with its modules linked in.
fn to_ir_program(source: &str, name: &str, test_mode: bool, trust_ensure: bool) -> Result<(String, ast::Program), CompileError> {
    let fail = |d: Diagnostic| CompileError(render(name, source, d.span, &d.text()));
    let (mut program, next_id) = parser::parse_file(source, 0, 0).map_err(fail)?;
    // `use c`: the headers are read now (D110)
    let folder = Path::new(name).parent().map(|p| if p.as_os_str().is_empty() { Path::new(".") } else { p }).unwrap_or(Path::new("."));
    cheader::embed(&mut program, folder);
    checker::check(&program).map_err(fail)?;
    // the modules of the program's files (D124)
    let mut linker = modules::Linker::new(name, next_id);
    linker.link(&mut program).map_err(fail)?;
    program.files = linker.files;
    let res = resolve::resolve(&program).map_err(fail)?;
    typecheck::check(&program, &res).map_err(fail)?;
    let opts = codegen::Options {
        file: name.to_string(),
        test_mode,
        trust_ensure,
        repl_line: 0,
        shown_source: None,
        db_command: false,
    };
    let ir = codegen::Gen::new(source, &res, &opts).program(&program).map_err(fail)?;
    Ok((ir, program))
}

/// `nexo db diff|migrate file.nexo [--db path] [--allow-drop]` (D101): the
/// types of the program are compiled alone, and the runtime compares them
/// with the database.
fn run_db(args: &[String]) -> u8 {
    let command = args.first().cloned().unwrap_or_default();
    let (mut file, mut db_path, mut allow_drop) = (None::<String>, None::<String>, false);
    let mut i = 1;
    let mut bad = false;
    while i < args.len() {
        let a = args[i].as_str();
        if a == "--db" && i + 1 < args.len() {
            db_path = Some(args[i + 1].clone());
            i += 2;
            continue;
        }
        if a == "--allow-drop" && command == "migrate" {
            allow_drop = true;
        } else if file.is_none() && !a.starts_with('-') {
            file = Some(a.to_string());
        } else {
            bad = true;
        }
        i += 1;
    }
    let (Some(file), false) = (file, bad || !(command == "diff" || command == "migrate")) else {
        eprintln!("{}", usage());
        return 2;
    };
    let source = match read_source(&file) {
        Ok(s) => s,
        Err(e) => {
            eprint!("{e}");
            return 1;
        }
    };
    let fail = |d: Diagnostic| render(&file, &source, d.span, &d.text());
    let compiled = (|| {
        let mut program = linked(&file, &source).map_err(fail)?;
        resolve::resolve(&program).map_err(fail)?;
        if db_path.is_none() {
            db_path = connect_literal(&program);
        }
        retain_tree(&mut program.body, &|s| match &s.kind {
            ast::StmtKind::Type(_) | ast::StmtKind::Trait { .. } | ast::StmtKind::Use { .. } => true,
            ast::StmtKind::Expr(ast::Expr { kind: ast::ExprKind::Call { func, .. }, .. }) => {
                matches!(&func.kind, ast::ExprKind::Name(n) if n == "__db_table")
            }
            _ => false,
        });
        let res = resolve::resolve(&program).map_err(fail)?;
        typecheck::check(&program, &res).map_err(fail)?;
        let opts = codegen::Options {
            file: file.clone(),
            test_mode: false,
            trust_ensure: false,
            repl_line: 0,
            shown_source: None,
            db_command: true,
        };
        codegen::Gen::new(&source, &res, &opts).program(&program).map_err(fail)
    })();
    let ir = match compiled {
        Ok(ir) => ir,
        Err(text) => {
            eprint!("{text}");
            return 1;
        }
    };
    let dir = work_dir();
    let exe = dir.join("schema");
    if let Err(e) = link(&ir, &exe, false) {
        eprintln!("Error: {e}");
        return 1;
    }
    let mut cmd = Command::new(&exe);
    cmd.env("NEXO_DB_COMMAND", &command).env("NEXO_DB_FILE", &file);
    if let Some(p) = &db_path {
        cmd.env("NEXO_DB_PATH", p);
    }
    if allow_drop {
        cmd.env("NEXO_DB_ALLOW_DROP", "1");
    }
    let status = cmd.status();
    let _ = std::fs::remove_dir_all(&dir);
    match status {
        Ok(s) => exit_code(s),
        Err(e) => {
            eprintln!("Error: {e}");
            1
        }
    }
}

/// A program parsed and checked, with the modules it uses (D124).
fn linked(file: &str, source: &str) -> Result<ast::Program, Diagnostic> {
    let (mut program, next_id) = parser::parse_file(source, 0, 0)?;
    checker::check(&program)?;
    let mut linker = modules::Linker::new(file, next_id);
    linker.link(&mut program)?;
    program.files = linker.files;
    Ok(program)
}

/// Keep the statements of a program, and of the modules it uses, that
/// `keep` accepts.
fn retain_tree(body: &mut Vec<ast::Stmt>, keep: &dyn Fn(&ast::Stmt) -> bool) {
    body.retain(|s| matches!(s.kind, ast::StmtKind::Module { .. }) || keep(s));
    for s in body.iter_mut() {
        if let ast::StmtKind::Module { body: Some(b), .. } = &mut s.kind {
            retain_tree(b, keep);
        }
    }
}

/// Compile only the types of a program and the `keep` calls (`__db_table`
/// and the like), run it with `env` set, and return its exit code: the
/// runtime carries out the command of a module (D101, D116).
pub fn module_command(file: &str, env: &[(&str, String)], keep: &[&str], literals: bool, fns: bool) -> u8 {
    let source = match read_source(file) {
        Ok(s) => s,
        Err(e) => {
            eprint!("{e}");
            return 1;
        }
    };
    let fail = |d: Diagnostic| render(file, &source, d.span, &d.text());
    let mut db_path = None;
    let compiled = (|| {
        let mut program = linked(file, &source).map_err(fail)?;
        resolve::resolve(&program).map_err(fail)?;
        db_path = connect_literal(&program);
        retain_tree(&mut program.body, &|s| match &s.kind {
            ast::StmtKind::Type(_) | ast::StmtKind::Trait { .. } | ast::StmtKind::Use { .. } => true,
            ast::StmtKind::Fn(_) => fns,
            ast::StmtKind::Expr(ast::Expr { kind: ast::ExprKind::Call { func, .. }, .. }) => {
                matches!(&func.kind, ast::ExprKind::Name(n) if keep.contains(&n.as_str()))
            }
            ast::StmtKind::Assign { target: ast::Expr { kind: ast::ExprKind::Name(_), .. }, op: "=", value, .. } => {
                literals && phase5::literal(value)
            }
            _ => false,
        });
        let res = resolve::resolve(&program).map_err(fail)?;
        typecheck::check(&program, &res).map_err(fail)?;
        let opts = codegen::Options {
            file: file.to_string(),
            test_mode: false,
            trust_ensure: false,
            repl_line: 0,
            shown_source: None,
            db_command: true,
        };
        codegen::Gen::new(&source, &res, &opts).program(&program).map_err(fail)
    })();
    let ir = match compiled {
        Ok(ir) => ir,
        Err(text) => {
            eprint!("{text}");
            return 1;
        }
    };
    let dir = work_dir();
    let exe = dir.join("module");
    if let Err(e) = link(&ir, &exe, false) {
        eprintln!("Error: {e}");
        return 1;
    }
    let mut cmd = Command::new(&exe);
    for (k, v) in env {
        cmd.env(k, v);
    }
    if let Some(p) = &db_path {
        cmd.env("NEXO_DB_PATH", p);
    }
    let status = cmd.status();
    let _ = std::fs::remove_dir_all(&dir);
    match status {
        Ok(s) => exit_code(s),
        Err(e) => {
            eprintln!("Error: {e}");
            1
        }
    }
}

/// The database of a program: the first connect("...") it writes.
fn connect_literal(program: &ast::Program) -> Option<String> {
    use ast::{ExprKind, StrPiece};
    fn find(e: &ast::Expr) -> Option<String> {
        if let ExprKind::Call { func, args } = &e.kind {
            if matches!(&func.kind, ExprKind::Name(n) if n == "connect") {
                if let Some(ExprKind::Str(parts)) = args.first().map(|a| &a.value.kind) {
                    if let [StrPiece::Lit(s)] = parts.as_slice() {
                        return Some(s.clone());
                    }
                    if parts.is_empty() {
                        return Some(String::new());
                    }
                }
            }
            if let Some(r) = find(func) {
                return Some(r);
            }
            for a in args {
                if let Some(r) = find(&a.value) {
                    return Some(r);
                }
            }
            return None;
        }
        checker::children(e).into_iter().find_map(find)
    }
    let mut found = None;
    checker::walk_stmts(&program.body, true, &mut |s| {
        if found.is_some() {
            return;
        }
        let exprs: Vec<&ast::Expr> = match &s.kind {
            ast::StmtKind::Expr(e) | ast::StmtKind::Return(Some(e)) => vec![e],
            ast::StmtKind::Assign { target, value, .. } => vec![value, target],
            ast::StmtKind::VarDecl { value, .. } => vec![value],
            _ => vec![],
        };
        found = exprs.into_iter().find_map(find);
    });
    found
}

/// One line of the standard input, newline included, read byte by byte.
fn unbuffered_line() -> Option<Vec<u8>> {
    use std::io::Read;
    use std::os::fd::FromRawFd;
    let mut stdin = std::mem::ManuallyDrop::new(unsafe { std::fs::File::from_raw_fd(0) });
    let mut line = Vec::new();
    let mut byte = [0u8; 1];
    loop {
        match stdin.read(&mut byte) {
            Ok(1) => {
                line.push(byte[0]);
                if byte[0] == b'\n' {
                    return Some(line);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            _ => return if line.is_empty() { None } else { Some(line) },
        }
    }
}

/// `nexo` with no arguments: the REPL (D88). The session is a program;
/// each input is added to it and the program runs again, the earlier
/// inputs replaying from a journal (see runtime/src/repl.rs).
fn repl() -> u8 {
    use std::io::Write;
    let banner = "Nexo {v} (compiler). Type code and press Enter; Ctrl-D to exit.";
    println!("{}", banner.replace("{v}", env!("CARGO_PKG_VERSION")));
    let dir = work_dir();
    let journal = dir.join("journal");
    let _ = std::fs::write(&journal, "");
    let exe = dir.join("session");
    // stdin is read one byte at a time: a program that calls input() reads
    // the lines that follow, as in phase 0
    let prompt = |text: &str| -> Option<String> {
        print!("{text}");
        let _ = std::io::stdout().flush();
        let mut line = String::from_utf8_lossy(&unbuffered_line()?).into_owned();
        while line.ends_with('\n') || line.ends_with('\r') {
            line.pop();
        }
        Some(line)
    };
    let mut session = String::new();
    let mut session_lines = 0u32;
    loop {
        let Some(line) = prompt(">>> ") else {
            println!();
            break;
        };
        if line.trim().is_empty() {
            continue;
        }
        let mut cell = vec![line.clone()];
        let trimmed = line.trim_end();
        if trimmed.ends_with(':') || trimmed.ends_with(',') {
            while let Some(more) = prompt("... ") {
                if more.trim().is_empty() {
                    break;
                }
                cell.push(more);
            }
        }
        let cell = cell.join("\n") + "\n";
        let first = session_lines + 1;
        let candidate = format!("{session}{cell}");
        let opts = codegen::Options {
            file: "<repl>".into(),
            test_mode: false,
            trust_ensure: false,
            repl_line: first,
            shown_source: Some(cell.clone()),
            db_command: false,
        };
        let compiled = (|| {
            let (mut program, next_id) = parser::parse_file(&candidate, 0, 0)?;
            cheader::embed(&mut program, Path::new("."));
            checker::check(&program)?;
            let mut linker = modules::Linker::new("<repl>", next_id);
            linker.link(&mut program)?;
            program.files = linker.files;
            let res = resolve::resolve(&program)?;
            typecheck::check(&program, &res)?;
            codegen::Gen::new(&candidate, &res, &opts).program(&program)
        })();
        let ir = match compiled {
            Ok(ir) => ir,
            Err(mut d) => {
                // errors point into the new input, counted from its first line
                let shown = match d.span {
                    Some(sp) if sp.line >= first && sp.file == 0 => {
                        d.span = Some(nexo_diag::Span::new(sp.line - first + 1, sp.col, sp.end_col));
                        render("<repl>", &cell, d.span, &d.text())
                    }
                    _ => render("<repl>", &candidate, d.span, &d.text()),
                };
                let _ = std::io::stdout().flush();
                eprint!("{shown}");
                continue;
            }
        };
        if let Err(e) = link_at(&ir, &exe, "-O0") {
            eprintln!("Error: {e}");
            continue;
        }
        let before = std::fs::metadata(&journal).map(|m| m.len()).unwrap_or(0);
        let status = Command::new(&exe).env("NEXO_REPL_JOURNAL", &journal).status();
        match status {
            Ok(s) if s.success() => {
                session = candidate;
                session_lines += cell.matches('\n').count() as u32;
            }
            Ok(_) => {
                // a failed input is left out of the session, with what it did
                if let Ok(f) = std::fs::OpenOptions::new().write(true).open(&journal) {
                    let _ = f.set_len(before);
                }
            }
            Err(e) => eprintln!("Error: {e}"),
        }
    }
    let _ = std::fs::remove_dir_all(&dir);
    0
}

/// The checks of a compilation that happen before code generation; the
/// program is returned as written (without the modules it uses).
fn front_end(source: &str, file: &str) -> Result<ast::Program, Diagnostic> {
    let (program, next_id) = parser::parse_file(source, 0, 0)?;
    checker::check(&program)?;
    let mut linked = program.clone();
    modules::Linker::new(file, next_id).link(&mut linked)?;
    let res = resolve::resolve(&linked)?;
    typecheck::check(&linked, &res)?;
    Ok(program)
}

/// The warnings of a file, or the compile error that stops it (D86).
fn lint_source(source: &str, file: &str) -> Result<Vec<lint::Finding>, Diagnostic> {
    let program = front_end(source, file)?;
    let tokens = lexer::tokenize(source)?;
    Ok(lint::Linter::new(source, &tokens, &program).run())
}

/// Apply every safe fix, again and again until none is left.
fn fix_source(mut source: String, file: &str) -> String {
    for _ in 0..100 {
        match lint_source(&source, file) {
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

/// The first line of `clang --version` (the compiler named in the SBOM).
fn cc_version() -> String {
    let cc = std::env::var("NEXO_CC").unwrap_or_else(|_| "clang".into());
    Command::new(&cc)
        .arg("--version")
        .output()
        .ok()
        .and_then(|o| String::from_utf8_lossy(&o.stdout).lines().next().map(str::to_string))
        .unwrap_or(cc)
}

/// Compile IR and link it with the runtime into a native executable.
/// Development mode does not optimise: it compiles several times faster
/// (D127).
fn link(ir: &str, out: &Path, release: bool) -> Result<(), String> {
    link_at(ir, out, if release { "-O2" } else { "-O0" })
}

/// The same with a given optimisation level (the REPL uses -O0: it
/// compiles the session again for every input).
fn link_at(ir: &str, out: &Path, opt: &str) -> Result<(), String> {
    // the same name for every program: it is written into the executable,
    // and two builds of the same code give the same bytes (D128)
    static BUILDS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let dir = work_dir().join(BUILDS.fetch_add(1, std::sync::atomic::Ordering::Relaxed).to_string());
    let _ = std::fs::create_dir_all(&dir);
    let ll = dir.join("program.ll");
    let cc = std::env::var("NEXO_CC").unwrap_or_else(|_| "clang".into());
    let libs = ["-lgcc_s", "-lutil", "-lrt", "-lpthread", "-lm", "-ldl", "-lc"];
    let fail = |output: std::process::Output| String::from_utf8_lossy(&output.stderr).into_owned();
    // development mode calls the reference-count helpers instead of copying
    // them into every use: clang compiles half the code (D135)
    let pat = "attributes #0 = { alwaysinline }";
    let dev = opt != "-O2";
    let mut parts = 1;
    if dev {
        // a large program is compiled in parts, at the same time (D135)
        let cores = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1);
        parts = (ir.len() / (2 << 20)).clamp(1, cores.min(8));
    }
    if parts == 1 {
        use std::io::Write;
        let mut f = std::io::BufWriter::new(std::fs::File::create(&ll).map_err(|e| e.to_string())?);
        let head = &ir[..ir.len().min(4096)];
        let w = match head.find(pat) {
            Some(i) if dev => f
                .write_all(ir[..i].as_bytes())
                .and_then(|_| f.write_all(b"attributes #0 = { noinline }"))
                .and_then(|_| f.write_all(ir[i + pat.len()..].as_bytes())),
            _ => f.write_all(ir.as_bytes()),
        };
        w.and_then(|_| f.flush()).map_err(|e| e.to_string())?;
        let output = Command::new(&cc)
            .arg(opt)
            .arg("-Wno-override-module")
            .arg(&ll)
            .arg(runtime_lib())
            .args(["-o"])
            .arg(out)
            .args(libs)
            .stdin(Stdio::null())
            .output()
            .map_err(|e| format!("{cc}: {e}"))?;
        if !output.status.success() {
            return Err(fail(output));
        }
    } else {
        let text = ir.replacen(pat, "attributes #0 = { noinline }", 1);
        let modules = split::split(&text, parts);
        drop(text);
        let mut objects = Vec::new();
        for (k, m) in modules.iter().enumerate() {
            let part = dir.join(format!("program.{k}.ll"));
            std::fs::write(&part, m).map_err(|e| e.to_string())?;
            objects.push((part, dir.join(format!("program.{k}.o"))));
        }
        drop(modules);
        let results: Vec<Result<std::process::Output, String>> = std::thread::scope(|sc| {
            let handles: Vec<_> = objects
                .iter()
                .map(|(src, obj)| {
                    let cc = cc.clone();
                    sc.spawn(move || {
                        Command::new(&cc)
                            .arg(opt)
                            .arg("-Wno-override-module")
                            .arg("-c")
                            .arg(src)
                            .arg("-o")
                            .arg(obj)
                            .stdin(Stdio::null())
                            .output()
                            .map_err(|e| format!("{cc}: {e}"))
                    })
                })
                .collect();
            handles.into_iter().map(|h| h.join().unwrap_or_else(|_| Err("clang".into()))).collect()
        });
        for r in results {
            let output = r?;
            if !output.status.success() {
                return Err(fail(output));
            }
        }
        let output = Command::new(&cc)
            .args(objects.iter().map(|(_, o)| o))
            .arg(runtime_lib())
            .args(["-o"])
            .arg(out)
            .args(libs)
            .stdin(Stdio::null())
            .output()
            .map_err(|e| format!("{cc}: {e}"))?;
        if !output.status.success() {
            return Err(fail(output));
        }
        if std::env::var("NEXO_KEEP_IR").is_ok() {
            let _ = std::fs::write(&ll, ir);
        }
    }
    // NEXO_KEEP_IR=1 keeps the IR next to the program (out.ll)
    if std::env::var("NEXO_KEEP_IR").is_ok() {
        let _ = std::fs::copy(&ll, out.with_extension("ll"));
    }
    let _ = std::fs::remove_dir_all(&dir);
    Ok(())
}

pub fn read_source(path: &str) -> Result<String, String> {
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
    let (ir, program) = match to_ir_program(&source, &file, false, trust_ensure) {
        Ok(x) => x,
        Err(CompileError(text)) => {
            eprint!("{text}");
            return 1;
        }
    };
    if release {
        // personal data without keep or purpose (section 23, rule 1)
        if let Ok(program) = parser::parse(&source) {
            if let Some((span, tname, missing)) = phase5::privacy_gaps(&program.body).into_iter().next() {
                let d = Diagnostic::new("privacy_release", span).arg("type", tname).arg("missing", missing.join(", "));
                eprint!("{}", render(&file, &source, d.span, &d.text()));
                return 1;
            }
        }
    }
    let out = out.unwrap_or_else(|| Path::new(&file).with_extension("").to_string_lossy().into_owned());
    if let Err(e) = link(&ir, Path::new(&out), release) {
        eprintln!("Error: {e}");
        return 1;
    }
    let _ = std::fs::remove_dir_all(work_dir());
    if release {
        // the bill of materials, next to the program (D128)
        let mut files = vec![(file.clone(), source.clone())];
        for id in &program.files {
            if let Some((n, text)) = nexo_diag::file_of(*id) {
                files.push((n.to_string(), text.to_string()));
            }
        }
        let doc = sbom::document(Path::new(&out), &files, &program.body, &cc_version());
        let sbom_path = format!("{out}.sbom.json");
        if let Err(e) = std::fs::write(&sbom_path, doc) {
            eprintln!("Error: {sbom_path}: {e}");
            return 1;
        }
    }
    0
}

fn summary(total: u32, passed: u32, failed: u32) -> String {
    format!("{total} {}: {passed} passed, {failed} failed", if total == 1 { "test" } else { "tests" })
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
            // shown as phase 0 shows them: `nexo lint .` names `a.nexo`, not `./a.nexo`
            found.extend(files.into_iter().map(|f| f.strip_prefix(".").map(|r| r.to_path_buf()).unwrap_or(f)));
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
        if nums.len() == 2 {
            total += nums[0];
            passed += nums[1];
        }
        if !ok || nums.len() != 2 {
            broken += 1;
        }
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
        match lint_source(&source, &display) {
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

/// `nexo audit [paths]`: every use of unsafe code, then a summary (D129).
fn run_audit(paths: &[String]) -> u8 {
    let (mut total, mut files, mut status) = (0usize, 0usize, 0u8);
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
        match audit::audit_source(&source) {
            Ok(found) => {
                for (line, _, text) in &found {
                    println!("{name}:{line}: {text}");
                }
                total += found.len();
                files += usize::from(!found.is_empty());
            }
            Err(d) => {
                eprint!("{}", render(&name, &source, d.span, &d.text()));
                status = 1;
            }
        }
    }
    if total > 0 {
        println!("{}", msg("info.audit_summary", &[("n", total.to_string()), ("files", files.to_string())]));
    } else if status == 0 {
        let shown = if paths.is_empty() { ".".to_string() } else { paths.join(", ") };
        println!("{}", msg("info.audit_none", &[("path", shown)]));
    }
    status
}

/// `nexo test --suggest file.nexo` (D131): the probe is compiled as a
/// file next to the program (its modules are found from there) and run
/// for at most a minute.
fn run_suggest(args: &[String]) -> u8 {
    let [path] = args else {
        eprintln!("{}", usage());
        return 2;
    };
    let source = match read_source(path) {
        Ok(s) => s,
        Err(e) => {
            eprint!("{e}");
            return 1;
        }
    };
    let program = match parser::parse(&source) {
        Ok(p) => p,
        Err(d) => {
            eprint!("{}", render(path, &source, d.span, &d.text()));
            return 1;
        }
    };
    let run = |text: &str| run_probe(path, "suggest", text, 60);
    print!("{}", suggest::suggest(&program, &source, path, &run));
    let _ = std::fs::remove_dir_all(work_dir());
    0
}

/// Compile a probe program as a file next to `path` (its modules are found
/// from there) and run it for at most `secs` seconds: its standard output
/// (D131, D132).
fn run_probe(path: &str, what: &str, text: &str, secs: u64) -> String {
    let folder = Path::new(path).parent().unwrap_or(Path::new(""));
    let probe = folder.join(format!(".nexo-{what}-{}.nexo", std::process::id())).to_string_lossy().into_owned();
    let Ok(ir) = to_ir(text, &probe, false, false) else { return String::new() };
    let exe = work_dir().join(format!("{what}-probe"));
    if link(&ir, &exe, false).is_err() {
        return String::new();
    }
    let Ok(mut child) = Command::new(&exe).stdout(Stdio::piped()).stderr(Stdio::null()).stdin(Stdio::null()).spawn() else {
        return String::new();
    };
    let mut out = child.stdout.take().unwrap();
    let reader = std::thread::spawn(move || {
        let mut buf = String::new();
        let _ = std::io::Read::read_to_string(&mut out, &mut buf);
        buf
    });
    let start = std::time::Instant::now();
    while child.try_wait().ok().flatten().is_none() {
        if start.elapsed() > std::time::Duration::from_secs(secs) {
            let _ = child.kill();
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    let _ = child.wait();
    reader.join().unwrap_or_default()
}

/// `nexo check --intent [paths]` (D132).
fn run_intent(paths: &[String]) -> u8 {
    let (mut total, mut status) = (0usize, 0u8);
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
        let program = match parser::parse(&source) {
            Ok(p) => p,
            Err(d) => {
                eprint!("{}", render(&name, &source, d.span, &d.text()));
                status = 1;
                continue;
            }
        };
        let run = |text: &str| run_probe(&display, "intent", text, 600);
        let (warnings, error) = intent::check_intent(&program, &source, &name, &run);
        for w in &warnings {
            print!("{w}");
        }
        total += warnings.len();
        if let Some(e) = error {
            eprintln!("Error: {e}");
            let _ = std::fs::remove_dir_all(work_dir());
            return 1;
        }
    }
    let _ = std::fs::remove_dir_all(work_dir());
    if total > 0 {
        println!("{}", msg("info.intent_summary", &[("n", total.to_string())]));
        return 1;
    }
    if status == 0 {
        let shown = if paths.is_empty() { ".".to_string() } else { paths.join(", ") };
        println!("{}", msg("info.intent_ok", &[("path", shown)]));
    }
    status
}

/// `nexo docs --for-ai [file]` (D130).
fn run_docs(args: &[String]) -> u8 {
    if args.first().map(String::as_str) != Some("--for-ai") || args.len() > 2 {
        eprintln!("{}", usage());
        return 2;
    }
    match args.get(1) {
        None => print!("{}", docs::for_ai(None)),
        Some(path) => match read_source(path) {
            Ok(source) => print!("{}", docs::for_ai(Some((path, &source)))),
            Err(e) => {
                eprint!("{e}");
                return 1;
            }
        },
    }
    0
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
        let fixed = fix_source(source.clone(), &display);
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
        if let Err(d) = front_end(&source, &display) {
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
    let rest: Vec<String> = std::env::args().skip(1).collect();
    let code = if rest.is_empty() {
        repl()
    } else {
        match rest[0].as_str() {
            "test" if rest.get(1).map(String::as_str) == Some("--suggest") => run_suggest(&rest[2..]),
            "test" => run_tests(&rest[1..]),
            "build" => build(&rest[1..]),
            "fmt" => run_fmt(&rest[1..]),
            "lint" => run_lint(&rest[1..]),
            "fix" => run_fix(&rest[1..]),
            "check" if rest.get(1).map(String::as_str) == Some("--intent") => run_intent(&rest[2..]),
            "check" => run_check(&rest[1..]),
            "audit" => run_audit(&rest[1..]),
            "docs" => run_docs(&rest[1..]),
            "db" => run_db(&rest[1..]),
            "flags" => phase5::run_flags(&rest[1..]),
            "privacy" => phase5::privacy_report(&rest[1..]),
            "plan" | "apply" | "destroy" | "drift" | "cost" | "import" => phase5::run_infra(&rest[0], &rest[1..]),
            "ci" => phase5::run_ci(&rest[1..]),
            "ai" if rest.len() == 2 && rest[1] == "setup" => ai_cmd::setup(),
            "ai" if rest.len() == 2 && rest[1] == "usage" => ai_cmd::usage(),
            "lsp" if rest.len() == 1 => lsp::serve(),
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
