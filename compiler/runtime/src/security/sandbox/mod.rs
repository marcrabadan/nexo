//! A single-process Linux worker, optionally with a separately supervised HTTP
//! broker. The payload's syscall confinement is identical in both profiles.
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
mod linux;

#[derive(Clone, Debug)]
pub struct Limits {
    pub cpu_seconds: u64,
    pub memory_mib: u64,
    pub file_size_mib: u64,
    pub wall_seconds: u64,
}
impl Default for Limits {
    fn default() -> Self { Self { cpu_seconds: 2, memory_mib: 256, file_size_mib: 16, wall_seconds: 5 } }
}
impl Limits {
    pub fn validate(&self) -> Result<(), String> {
        if self.cpu_seconds == 0 || self.cpu_seconds > 86400 || self.wall_seconds == 0 || self.wall_seconds > 86400
            || !(16..=1_048_576).contains(&self.memory_mib) || self.file_size_mib > 1_048_576 {
            return Err("Invalid worker limits: CPU/wall must be 1..86400 seconds, memory 16..1048576 MiB and file size 0..1048576 MiB.".into());
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Default)]
pub struct Profile {
    pub read: Vec<PathBuf>,
    pub write: Vec<PathBuf>,
    pub limits: Limits,
    pub network: Option<super::online::Policy>,
}

pub fn probe() -> String {
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    { linux::probe() }
    #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
    { r#"{"profile":"linux-offline-v1","supported":false,"reason":"unsupported_platform"}"#.into() }
}

/// Install irreversible hard limits in a dedicated payload process.
/// Its supervisor remains responsible for the wall deadline.
pub fn bound_process(limits: &Limits) -> Result<(), String> {
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    { linux::apply_limits(limits) }
    #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
    { let _ = limits; Err("Hard process limits require Linux x86_64".into()) }
}

pub fn bound_broker(limits: &Limits) -> Result<(), String> {
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    { linux::broker_parent_lifetime()?; linux::apply_limits(limits) }
    #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
    { let _ = limits; Err("Online isolation requires Linux x86_64".into()) }
}

/// Apply only in a dedicated, single-threaded worker immediately before exec.
/// This process is irreversibly confined; callers must never reuse it as a host.
pub fn enter(profile: &Profile, program: &std::path::Path, args: &[String], env: &[(String, String)]) -> Result<(), String> {
    profile.limits.validate()?;
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    { linux::enter(profile, program, args, env) }
    #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
    { let _ = (program, args, env); Err("The linux-offline-v1 sandbox requires Linux x86_64, Landlock ABI >= 3, openat2 and seccomp.".into()) }
}

/// Monitor one process; the profile prohibits clone/fork, so killing this PID
/// terminates the complete untrusted computation. No pre_exec allocation/fork hooks.
pub fn supervise(worker: &std::path::Path, profile: &Profile, program: &std::path::Path, args: &[String], env: &[(String, String)]) -> Result<u8, String> {
    profile.limits.validate()?;
    if profile.network.is_some() { super::online::reject_proxies()?; }
    #[cfg(not(unix))]
    if profile.network.is_some() { return Err("Online isolation requires Linux".into()); }
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    linux::verify_supervisor()?;
    let mut command = Command::new(worker);
    command.arg("__enter");
    if profile.network.is_some() { command.args(["--online", "1"]); }
    for path in &profile.read { command.arg("--read").arg(path); }
    for path in &profile.write { command.arg("--write").arg(path); }
    for (name, value) in [("--cpu-seconds", profile.limits.cpu_seconds), ("--memory-mib", profile.limits.memory_mib),
        ("--file-size-mib", profile.limits.file_size_mib), ("--wall-seconds", profile.limits.wall_seconds)] {
        command.arg(name).arg(value.to_string());
    }
    command.arg("--").arg(program).args(args);
    // Payload bindings cross an anonymous pipe, never the helper's startup
    // environment: LD_PRELOAD must not run before sandbox installation.
    let payload = crate::json::Json::Obj(env.iter().map(|(k,v)| (k.clone(), crate::json::Json::Str(v.clone()))).collect()).compact();
    if payload.len() > 524288 { return Err("Worker environment is too large".into()); }
    command.env_clear().env("LANG", "C.UTF-8");
    command.stdin(Stdio::piped());
    #[cfg(unix)]
    let mut broker_channel = if profile.network.is_some() {
        let (child_end, host_end) = std::os::unix::net::UnixStream::pair().map_err(|_| "Cannot create broker channel")?;
        use std::os::fd::OwnedFd;
        command.stdin(Stdio::from(OwnedFd::from(child_end)));
        Some(host_end)
    } else { None };
    let mut child = command.spawn().map_err(|e| format!("Cannot start sandbox worker: {e}"))?;
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    let handle = match linux::process_handle(child.id()) {
        Ok(fd) => fd,
        Err(e) => {
            // No wait has occurred and default SIGCHLD retains an exited child
            // as our zombie, so this initial fallback cannot hit a reused PID.
            let _ = child.kill(); let _ = child.wait(); return Err(e);
        }
    };
    let stop = |child: &mut std::process::Child| {
        #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
        linux::terminate(&handle);
        #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
        let _ = child.kill();
        let _ = child.wait();
    };
    // Every exit path drops this guard, kills and reaps the broker too. A DNS
    // lookup or blocked peer cannot outlive the supervised job's wall deadline.
    struct Broker(std::process::Child);
    impl Drop for Broker {
        fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); }
    }
    let mut _broker: Option<Broker> = None;
    #[cfg(unix)]
    if let Some(mut channel) = broker_channel.take() {
        if super::online::write_frame(&mut channel,payload.as_bytes(),524288).is_err() {
            stop(&mut child); return Err("Cannot deliver worker environment".into());
        }
        let policy = profile.network.as_ref().unwrap();
        let mut broker = Command::new(worker);
        broker.arg("__broker");
        broker.arg("--cpu-seconds").arg(profile.limits.cpu_seconds.to_string());
        broker.arg("--memory-mib").arg(profile.limits.memory_mib.to_string());
        for host in &policy.allow { broker.arg("--allow-host").arg(host); }
        for host in &policy.deny { broker.arg("--deny-host").arg(host); }
        use std::os::fd::OwnedFd;
        broker.env_clear().env("LANG","C.UTF-8").stdin(Stdio::from(OwnedFd::from(channel)))
            .stdout(Stdio::null()).stderr(Stdio::null());
        _broker = Some(Broker(match broker.spawn() {
            Ok(b) => b, Err(_) => { stop(&mut child); return Err("Cannot start HTTP broker".into()); }
        }));
    }
    if profile.network.is_none() {
        use std::io::Write;
        if child.stdin.take().unwrap().write_all(payload.as_bytes()).is_err() {
            stop(&mut child);
            return Err("Cannot deliver worker environment".into());
        }
    }
    let deadline = Instant::now() + Duration::from_secs(profile.limits.wall_seconds);
    loop {
        let status = match child.try_wait() {
            Ok(status) => status,
            Err(e) => { stop(&mut child); return Err(format!("Cannot monitor sandbox worker: {e}")); }
        };
        if let Some(status) = status {
            #[cfg(unix)]
            {
                use std::os::unix::process::ExitStatusExt;
                if let Some(signal) = status.signal() {
                    let reason = match signal { 24 => "cpu_limit", 25 => "file_size_limit", _ => "worker_signal" };
                    eprintln!(r#"{{"event":"security.worker.stopped","reason":"{reason}","signal":{signal}}}"#);
                    return Ok((128 + signal).min(255) as u8);
                }
            }
            return Ok(status.code().unwrap_or(1).clamp(0,255) as u8);
        }
        if Instant::now() >= deadline {
            stop(&mut child);
            eprintln!(r#"{{"event":"security.resource.exceeded","reason":"wall_limit"}}"#);
            return Ok(124);
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

pub fn probe_child() -> bool {
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    { linux::probe_child() }
    #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
    { false }
}


pub fn probe_landlock_child() -> bool {
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    { linux::probe_landlock_child() }
    #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
    { false }
}
