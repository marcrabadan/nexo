//! Dedicated worker/supervisor. Standard streams are the only delegated IPC.
use nexo_runtime::security::sandbox::{self, Profile};
use std::path::PathBuf;
use std::process::ExitCode;
fn run(args: &[String]) -> Result<u8, String> {
    #[cfg(unix)]
    if args.first().is_some_and(|s| s == "__broker") {
        let mut policy = nexo_runtime::security::online::Policy::default();
        let mut limits = sandbox::Limits::default();
        if (args.len()-1) % 2 != 0 { return Err("Invalid broker configuration".into()); }
        for pair in args[1..].chunks_exact(2) {
            match pair[0].as_str() {
                "--allow-host" => policy.allow.push(pair[1].clone()),
                "--deny-host" => policy.deny.push(pair[1].clone()),
                "--cpu-seconds" => limits.cpu_seconds = pair[1].parse().map_err(|_| "Invalid broker limit")?,
                "--memory-mib" => limits.memory_mib = pair[1].parse().map_err(|_| "Invalid broker limit")?,
                _ => return Err("Invalid broker configuration".into()),
            }
        }
        sandbox::bound_broker(&limits)?;
        nexo_runtime::security::online::serve(&policy).map_err(|_| "Broker channel closed")?;
        return Ok(0);
    }
    if args == ["probe"] { println!("{}", sandbox::probe()); return Ok(0); }
    if args == ["probe-online"] { println!("{}", nexo_runtime::security::online::probe()); return Ok(0); }
    if args == ["__probe_landlock"] { return Ok(if sandbox::probe_landlock_child() { 0 } else { 125 }); }
    if args == ["__probe_seccomp"] { return Ok(if sandbox::probe_child() { 0 } else { 125 }); }
    let enter = args.first().is_some_and(|a| a == "__enter");
    let mut p = Profile::default();
    let mut i = usize::from(enter);
    let mut env = Vec::new();
    while i < args.len() && args[i] != "--" {
        let name = args[i].as_str();
        let value = args.get(i+1).ok_or("Missing worker option value")?;
        match name {
            "--online" if enter && value == "1" => p.network = Some(Default::default()),
            "--read" => p.read.push(PathBuf::from(value)), "--write" => p.write.push(PathBuf::from(value)),
            "--cpu-seconds" => p.limits.cpu_seconds = value.parse().map_err(|_| "Invalid CPU limit")?,
            "--memory-mib" => p.limits.memory_mib = value.parse().map_err(|_| "Invalid memory limit")?,
            "--file-size-mib" => p.limits.file_size_mib = value.parse().map_err(|_| "Invalid file-size limit")?,
            "--wall-seconds" => p.limits.wall_seconds = value.parse().map_err(|_| "Invalid wall limit")?,
            "--env" if !enter => {
                if value.is_empty() || value.contains('=') || value.starts_with("LD_") || value.starts_with("DYLD_") {
                    return Err("Invalid or unsafe worker environment binding".into());
                }
                let v = std::env::var(value).map_err(|_| "Worker environment binding is missing")?;
                env.push((value.clone(),v));
            }
            _ => return Err("Unknown worker option; use explicit --read/--write, resource limits and -- executable arguments.".into()),
        }
        i += 2;
    }
    if args.get(i).map(String::as_str) != Some("--") { return Err("Worker requires -- executable arguments".into()); }
    let program = PathBuf::from(args.get(i+1).ok_or("Worker executable is missing")?);
    if !program.is_absolute() { return Err("Worker executable must be an absolute path".into()); }
    p.limits.validate()?;
    if enter {
        use std::io::Read;
        let mut payload = String::new();
        if p.network.is_some() {
            let bytes = nexo_runtime::security::online::read_frame(&mut std::io::stdin(),524288).map_err(|_| "Invalid worker environment channel")?;
            payload = String::from_utf8(bytes).map_err(|_| "Invalid worker environment channel")?;
        } else {
            std::io::stdin().take(524289).read_to_string(&mut payload).map_err(|_| "Invalid worker environment channel")?;
        }
        if payload.len() > 524288 { return Err("Worker environment is too large".into()); }
        let Some(nexo_runtime::json::Json::Obj(bindings)) = nexo_runtime::json::parse(&payload) else { return Err("Invalid worker environment payload".into()); };
        let mut env = Vec::new();
        for (k,v) in bindings {
            if k.is_empty() || k.contains('=') || k.contains('\0') { return Err("Invalid worker binding name".into()); }
            env.push((k,v.str().ok_or("Invalid worker binding")?.to_string()));
        }
        sandbox::enter(&p, &program, &args[i+2..], &env)?; Ok(125)
    }
    else { sandbox::supervise(&std::env::current_exe().map_err(|_| "Cannot locate worker")?, &p, &program, &args[i+2..], &env) }
}
fn main() -> ExitCode {
    match run(&std::env::args().skip(1).collect::<Vec<_>>()) {
        Ok(code) => ExitCode::from(code),
        Err(e) => { eprintln!("Error: {e}"); ExitCode::from(125) }
    }
}
