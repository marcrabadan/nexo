//! Shield CLI; only application API enforcement is available in this phase.
use nexo_diag::Diagnostic;
use std::path::Path;
use std::process::Command;

fn run_phase1(args: &[String], sandbox: Option<nexo_runtime::security::sandbox::Profile>) -> u8 {
    if args.len() != 2 || !matches!(args[0].as_str(), "check" | "run" | "trace" | "audit") || args[1].starts_with('-') {
        eprintln!("Usage: nexo security check|run|trace|audit file.nexo");
        return 2;
    }
    let (command, file) = (args[0].as_str(), &args[1]);
    let source = match crate::read_source(file) {
        Ok(s) => s, Err(e) => { eprint!("{e}"); return 1; }
    };
    let program = match crate::parser::parse(&source) {
        Ok(p) => p,
        Err(d) => { eprint!("{}", nexo_diag::render(file, &source, d.span, &d.text())); return 1; }
    };
    if crate::policy::declared(&program).is_none() {
        eprintln!("Error: {}", Diagnostic::new_spanless("policy_missing").text());
        return 1;
    }
    // The driver is single-threaded here. Also suppress native discovery during
    // compilation: denied imports must not start a Python/header probe first.
    std::env::set_var("NEXO_SECURITY_ENFORCE", "1");
    if matches!(command, "check" | "audit") {
        let status = crate::run_check(std::slice::from_ref(file));
        if status != 0 { return status; }
        let status = crate::policy::run(&["show".into(), file.clone()]);
        if status != 0 { return status; }
        return if command == "audit" { crate::run_audit(std::slice::from_ref(file)) } else { 0 };
    }
    // Mark policy strict without enabling release optimisations or trusting contracts.
    let ir = match crate::to_ir_program(&source, file, false, false, true) {
        Ok((ir, _)) => ir, Err(crate::CompileError(text)) => { eprint!("{text}"); return 1; }
    };
    let dir = crate::work_dir();
    let exe = dir.join("program");
    if let Err(e) = crate::link(&ir, Path::new(&exe), false) {
        eprintln!("Error: {e}");
        let _ = std::fs::remove_dir_all(&dir);
        return 1;
    }
    let mut child = Command::new(&exe);
    child.env("NEXO_SECURITY_ENFORCE", "1");
    if command == "trace" { child.env("NEXO_SECURITY_TRACE", "1"); }
    if let Some(mut profile) = sandbox {
        let Some((_,rules)) = crate::policy::declared(&program) else { return 1 };
        for (effect,kind,targets) in &rules {
            if (kind == "read" || kind == "write") && effect == "deny" {
                eprintln!("Error: Linux filesystem grants cannot represent deny exceptions; use disjoint positive grants.");
                let _ = std::fs::remove_dir_all(&dir); return 125;
            }
            if effect == "allow" && kind == "net" {
                eprintln!("Error: linux-offline-v1 does not support network grants.");
                let _ = std::fs::remove_dir_all(&dir); return 125;
            }
            if effect == "allow" && kind == "read" { profile.read.extend(targets.iter().map(std::path::PathBuf::from)); }
            if effect == "allow" && kind == "write" { profile.write.extend(targets.iter().map(std::path::PathBuf::from)); }
        }
        // Trusted boot files are readable, never writable. The payload is still
        // offline and single-process; native Nexo integrations remain denied.
        profile.read.push("/usr/lib".into());
        if Path::new("/etc/ld.so.cache").exists() { profile.read.push("/etc/ld.so.cache".into()); }
        let mut env = vec![("NEXO_SECURITY_ENFORCE".into(),"1".into()), ("NEXO_SECURITY_SANDBOX".into(),"linux-offline-v1".into())];
        if command == "trace" { env.push(("NEXO_SECURITY_TRACE".into(),"1".into())); }
        if let Some(path) = std::env::var_os("NEXO_SECURITY_DEPENDENCIES") {
            profile.read.push(path.clone().into());
            env.push(("NEXO_SECURITY_DEPENDENCIES".into(),path.to_string_lossy().into()));
        }
        let names: std::collections::BTreeSet<&String> = rules.iter()
            .filter(|(r,k,_)| r == "allow" && k == "env").flat_map(|(_,_,t)| t.iter()).collect();
        for name in names {
            if name.starts_with("LD_") || name.starts_with("DYLD_") || name == "GLIBC_TUNABLES" {
                eprintln!("Error: Loader-control bindings cannot be delegated to a Nexo sandbox.");
                let _ = std::fs::remove_dir_all(&dir); return 125;
            }
            if ["NEXO_SECURITY_ENFORCE","NEXO_SECURITY_TRACE","NEXO_SECURITY_SANDBOX",
                "NEXO_SECURITY_DEPENDENCIES","NEXO_POLICY_RECORD"].contains(&name.as_str()) { continue; }
            if rules.iter().any(|(r,k,t)| r == "deny" && k == "env" && t.contains(name)) { continue; }
            // Read only explicitly delegated bindings; never enumerate credential values.
            if let Ok(value) = std::env::var(name) { env.push((name.clone(),value)); }
        }
        let code = nexo_runtime::security::sandbox::supervise(&worker(), &profile, &exe, &[], &env)
            .unwrap_or_else(|e| { eprintln!("Error: {e}"); 125 });
        let _ = std::fs::remove_dir_all(&dir);
        return code;
    }
    let status = child.status();
    let _ = std::fs::remove_dir_all(&dir);
    match status { Ok(s) => crate::exit_code(s), Err(e) => { eprintln!("Error: {e}"); 1 } }
}


fn worker() -> std::path::PathBuf {
    std::env::current_exe().unwrap_or_default().with_file_name("nexo-shield-worker")
}

pub fn run(args: &[String]) -> u8 {
    if args.first().is_some_and(|s| s == "probe" || s == "worker") {
        let rest: Vec<String> = if args[0] == "probe" {
            if args.len() != 1 { eprintln!("Usage: nexo security probe"); return 2; }
            vec!["probe".into()]
        } else { args[1..].to_vec() };
        return Command::new(worker()).args(rest).status().map(crate::exit_code).unwrap_or_else(|_| {
            eprintln!("Error: Build nexo-shield-worker with cargo build --workspace."); 125
        });
    }
    let Some(command) = args.first() else { eprintln!("Usage: nexo security check|run|trace|audit [options] file.nexo"); return 2 };
    let mut rest = vec![command.clone()];
    let mut profile = nexo_runtime::security::sandbox::Profile::default();
    let mut isolated = false;
    let mut limits = false;
    let mut dependency = None;
    let mut i = 1;
    while i < args.len() {
        if args[i] == "--sandbox=linux" { isolated = true; i += 1; continue; }
        if args[i].starts_with('-') {
            let Some(value) = args.get(i+1) else { eprintln!("Error: Missing security option value."); return 2 };
            if args[i] == "--dependencies" { dependency = Some(value.clone()); }
            else {
                let Ok(n) = value.parse::<u64>() else { eprintln!("Error: Security limits must be non-negative integers."); return 2 };
                match args[i].as_str() {
                    "--cpu-seconds" => profile.limits.cpu_seconds = n,
                    "--memory-mib" => profile.limits.memory_mib = n,
                    "--file-size-mib" => profile.limits.file_size_mib = n,
                    "--wall-seconds" => profile.limits.wall_seconds = n,
                    _ => { eprintln!("Error: Unsupported security option."); return 2; }
                }
                limits = true;
            }
            i += 2;
        } else { rest.push(args[i].clone()); i += 1; }
    }
    if rest.len() != 2 || !matches!(command.as_str(),"check"|"run"|"trace"|"audit")
        || (limits && !isolated) || (isolated && !matches!(command.as_str(),"run"|"trace")) {
        eprintln!("Error: Use security run|trace --sandbox=linux [limits] file.nexo, or --dependencies file.json with any security command."); return 2;
    }
    if let Err(e) = profile.limits.validate() { eprintln!("Error: {e}"); return 2; }
    if isolated { std::env::set_var("NEXO_SECURITY_SANDBOX","linux-offline-v1"); }
    if let Some(path) = dependency {
        let path = match Path::new(&path).canonicalize() { Ok(p) => p, Err(_) => { eprintln!("Error: Cannot read dependency permissions."); return 1; } };
        if nexo_runtime::security::context::Configuration::load(&path, Path::new(&rest[1])).is_err() {
            eprintln!("Error: {}",Diagnostic::new_spanless("security_context_config").text()); return 1;
        }
        std::env::set_var("NEXO_SECURITY_DEPENDENCIES",path);
    }
    run_phase1(&rest, if isolated { Some(profile) } else { None })
}
