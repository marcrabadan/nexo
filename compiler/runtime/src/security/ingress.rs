//! Opt-in Linux loopback ingress. The launcher owns a hard-deadline child;
//! external requests receive an empty capability ceiling, never root authority.
use std::process::{Command, ExitStatus};
use std::time::{Duration, Instant};

pub const PREFIX: &str = "loopback-v1:";
pub fn port() -> Option<u16> {
    let value = std::env::var("NEXO_SECURITY_WEB").ok()?;
    let value = value.strip_prefix(PREFIX)?;
    if value.is_empty() || !value.bytes().all(|b| b.is_ascii_digit()) { return None; }
    value.parse().ok()
}
pub fn supervised() -> bool {
    static VALID: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *VALID.get_or_init(verify_supervisor)
}
fn verify_supervisor() -> bool {
    #[cfg(target_os = "linux")]
    {
        let expected = std::env::var("NEXO_SECURITY_WEB_SUPERVISOR").ok().and_then(|v| v.parse::<i32>().ok());
        let mut signal = 0;
        unsafe { expected == Some(libc::getppid()) && libc::getppid() > 0
            && libc::prctl(libc::PR_GET_PDEATHSIG, &mut signal) == 0 && signal == libc::SIGKILL }
    }
    #[cfg(not(target_os = "linux"))]
    { false }
}

/// Attach the delegated native CLI itself after exec, avoiding Python pre_exec
/// callbacks (which are unsafe when the calling Python process has threads).
pub fn bind_frontend() -> Result<(), String> {
    let Some(expected) = std::env::var_os("NEXO_SECURITY_WEB_FRONTEND") else { return Ok(()); };
    #[cfg(target_os = "linux")]
    {
        let expected = expected.to_str().and_then(|s| s.parse::<i32>().ok())
            .filter(|pid| *pid > 0).ok_or("Invalid loopback frontend owner")?;
        if unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 || libc::getppid() != expected } {
            return Err("Loopback frontend owner is unavailable".into());
        }
        Ok(())
    }
    #[cfg(not(target_os = "linux"))]
    { let _ = expected; Err("Loopback frontend supervision requires Linux".into()) }
}

pub fn supervise(command: &mut Command, compiled_dir: &std::path::Path) -> Result<Option<ExitStatus>, String> {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        let parent = unsafe { libc::getpid() };
        command.env("NEXO_SECURITY_WEB_SUPERVISOR", parent.to_string());
        unsafe { command.pre_exec(move || {
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 || libc::getppid() != parent {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        }); }
        let until = Instant::now() + Duration::from_secs(6);
        let executable = command.get_program().to_owned();
        let mut child = command.spawn().map_err(|_| "Cannot start loopback payload".to_string())?;
        // exec succeeded: Linux keeps the executable mappings alive after unlink.
        // Remove compiler artifacts now, including when the owner is later killed.
        let _ = std::fs::remove_file(executable);
        let _ = std::fs::remove_dir_all(compiled_dir);
        loop {
            match child.try_wait() {
                Ok(Some(status)) => return Ok(Some(status)),
                Ok(None) => {},
                Err(_) => { let _ = child.kill(); let _ = child.wait(); return Err("Cannot supervise loopback payload".into()); }
            }
            if Instant::now() >= until {
                let _ = child.kill();
                let _ = child.wait();
                return Ok(None);
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    #[cfg(not(target_os = "linux"))]
    { let _ = (command, compiled_dir); Err("Loopback ingress requires Linux process supervision".into()) }
}

/// Installed in the dedicated payload before application code, never in its
/// launcher. These bound the process, including spawned native threads.
pub fn install_limits() -> Result<(), String> {
    super::sandbox::bound_process(&super::sandbox::Limits {
        cpu_seconds: 6,
        memory_mib: 256,
        file_size_mib: 16,
        wall_seconds: 6,
    })
}
