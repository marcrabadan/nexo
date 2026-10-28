//! The only unsafe boundary added by Shield: audited Linux kernel ABI calls.
//! All pointers refer to live owned buffers; installation happens before exec
//! in a single-threaded helper, never inside the multithreaded language runtime.
use super::{Profile, Limits};
use std::ffi::CString;
use std::fs::File;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

const READ: u64 = (1 << 2) | (1 << 3);
const WRITE: u64 = (1 << 1) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 8) | (1 << 12) | (1 << 13) | (1 << 14);
const EXECUTE: u64 = 1;
#[repr(C)] struct Ruleset { handled_access_fs: u64 }
#[repr(C, packed)] struct Beneath { allowed_access: u64, parent_fd: i32 }
#[repr(C)] struct OpenHow { flags: u64, mode: u64, resolve: u64 }

fn error(what: &str) -> String { format!("Sandbox {what}: {}", std::io::Error::last_os_error()) }
fn abi() -> i64 {
    // SAFETY: VERSION query requires null attr and size zero; no memory is read.
    unsafe { libc::syscall(libc::SYS_landlock_create_ruleset, std::ptr::null::<Ruleset>(), 0usize, 1u32) as i64 }
}
fn pin(path: &Path, read: bool, protected: &[std::path::PathBuf]) -> Result<OwnedFd, String> {
    let path = std::fs::canonicalize(path).map_err(|_| "Sandbox grant or executable does not exist; grant an existing directory for new files.".to_string())?;
    if protected.iter().any(|p| p.starts_with(&path) || path.starts_with(p)) {
        return Err("Sandbox write grants cannot include trusted worker, runtime libraries or dependency configuration".into());
    }
    let name = CString::new(path.as_os_str().as_bytes()).map_err(|_| "Invalid sandbox path".to_string())?;
    let how = OpenHow { flags: ((if read { libc::O_RDONLY } else { libc::O_PATH }) | libc::O_CLOEXEC) as u64,
        mode: 0, resolve: 0x02 | 0x04 }; // NO_MAGICLINKS | NO_SYMLINKS, including every ancestor.
    // SAFETY: name/how are valid for their full declared lengths. A successful
    // syscall returns a newly-owned descriptor, closed by OwnedFd.
    let fd = unsafe { libc::syscall(libc::SYS_openat2, libc::AT_FDCWD, name.as_ptr(), &how, std::mem::size_of::<OpenHow>()) };
    if fd < 0 { Err(error("cannot pin path with openat2")) } else { Ok(unsafe { OwnedFd::from_raw_fd(fd as i32) }) }
}
fn add(ruleset: &OwnedFd, path: &Path, access: u64, protected: &[std::path::PathBuf]) -> Result<(), String> {
    let fd = pin(path, false, protected)?;
    let file = File::from(fd);
    let directory = file.metadata().map_err(|_| error("cannot inspect pinned grant"))?.is_dir();
    let access = if directory { access } else { access & (EXECUTE | (1 << 1) | (1 << 2) | (1 << 14)) };
    let rule = Beneath { allowed_access: access, parent_fd: file.as_raw_fd() };
    // SAFETY: packed structure matches landlock_path_beneath_attr (12 bytes).
    if unsafe { libc::syscall(libc::SYS_landlock_add_rule, ruleset.as_raw_fd(), 1u32, &rule, 0u32) } < 0 {
        return Err(error("cannot add filesystem grant"));
    }
    Ok(())
}

pub(super) fn apply_limits(l: &Limits) -> Result<(), String> {
    l.validate()?;
    for (resource, soft, hard) in [
        (libc::RLIMIT_CPU, l.cpu_seconds, l.cpu_seconds + 1),
        (libc::RLIMIT_AS, l.memory_mib * 1024 * 1024, l.memory_mib * 1024 * 1024),
        (libc::RLIMIT_FSIZE, l.file_size_mib * 1024 * 1024, l.file_size_mib * 1024 * 1024),
        (libc::RLIMIT_NOFILE, 64, 64), (libc::RLIMIT_CORE, 0, 0),
    ] {
        let limit = libc::rlimit { rlim_cur: soft, rlim_max: hard };
        // SAFETY: setrlimit copies this initialized structure. Limits are only
        // lowered in the dedicated child; the supervisor remains unaffected.
        if unsafe { libc::setrlimit(resource, &limit) } != 0 { return Err(error("cannot set hard resource limit")); }
    }
    Ok(())
}

pub(super) fn broker_parent_lifetime() -> Result<(), String> {
    // SAFETY: scalar prctl arguments set this dedicated helper's parent-death
    // signal only. Check both sides of installation to reject reparenting races.
    unsafe {
        let parent = libc::getppid();
        if parent <= 1 || libc::prctl(libc::PR_SET_PDEATHSIG,libc::SIGKILL,0,0,0) != 0 || libc::getppid() != parent {
            return Err("Cannot bind broker lifetime to its supervisor".into());
        }
    }
    Ok(())
}
fn statement(code: u16, k: u32) -> libc::sock_filter { libc::sock_filter { code, jt: 0, jf: 0, k } }
fn jump(k: u32, jt: u8, jf: u8) -> libc::sock_filter { libc::sock_filter { code: 0x15, jt, jf, k } }
fn install(mut filter: Vec<libc::sock_filter>) -> Result<(), String> {
    let program = libc::sock_fprog { len: filter.len() as u16, filter: filter.as_mut_ptr() };
    // SAFETY: these prctl/seccomp operations copy the live BPF buffer. The
    // kernel validates all jumps; failure is fatal, never an optional fallback.
    unsafe {
        if libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 { return Err(error("cannot drop exec privileges")); }
        if libc::syscall(libc::SYS_seccomp, 1u32, 0u32, &program) != 0 { return Err(error("cannot install seccomp")); }
    }
    Ok(())
}
pub(super) fn verify_supervisor() -> Result<(), String> {
    if std::fs::read_dir("/proc/self/task").map_err(|_| "Cannot verify supervisor threads")?.count() != 1 {
        return Err("Sandbox supervision requires a dedicated single-threaded launcher".into());
    }
    // SAFETY: sigaction only reads the initialized kernel disposition here.
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        if libc::sigaction(libc::SIGCHLD,std::ptr::null(),&mut action) != 0 { return Err(error("cannot inspect child ownership")); }
        if action.sa_sigaction != libc::SIG_DFL || action.sa_flags & libc::SA_NOCLDWAIT != 0 {
            return Err("Sandbox supervision requires exclusive child reaping and default SIGCHLD".into());
        }
    }
    Ok(())
}
pub(super) fn process_handle(pid: u32) -> Result<OwnedFd,String> {
    // SAFETY: pidfd_open returns an owned handle; the caller owns this child
    // with default SIGCHLD, so even an exited child retains its PID as a zombie.
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open,pid,0u32) };
    if fd < 0 { Err(error("cannot pin worker process")) } else { Ok(unsafe { OwnedFd::from_raw_fd(fd as i32) }) }
}
pub(super) fn terminate(handle: &OwnedFd) {
    // SAFETY: pidfd_send_signal targets the pinned process, never a recycled PID.
    unsafe { libc::syscall(libc::SYS_pidfd_send_signal,handle.as_raw_fd(),libc::SIGKILL,std::ptr::null::<libc::siginfo_t>(),0u32); }
}

fn architecture() -> Vec<libc::sock_filter> {
    vec![statement(0x20, 4), jump(0xc000003e, 1, 0), statement(0x06, 0x80000000), statement(0x20, 0)]
}
pub(super) fn seccomp(exec_fd: i32) -> Result<(), String> {
    const ALLOW: u32 = 0x7fff0000;
    const DENY: u32 = 0x00050000 | libc::EPERM as u32;
    let mut f = architecture();
    // execveat is allowed only through the pinned descriptor. Landlock also
    // requires execute permission on its inode; the FD closes on initial exec.
    f.extend([jump(libc::SYS_execveat as u32, 0, 4), statement(0x20, 16),
        jump(exec_fd as u32, 0, 1), statement(0x06, ALLOW), statement(0x06, DENY)]);
    // No sockets, clone/clone3/fork/vfork, ptrace, io_uring, bpf, mounts,
    // namespaces, capability changes, process_vm_*, signals to other processes,
    // chmod/chown/xattrs, prctl/seccomp, or general execve.
    let allowed = [libc::SYS_read, libc::SYS_write, libc::SYS_close, libc::SYS_stat, libc::SYS_fstat, libc::SYS_lstat,
        libc::SYS_poll, libc::SYS_lseek, libc::SYS_mmap, libc::SYS_mprotect, libc::SYS_munmap, libc::SYS_brk,
        libc::SYS_rt_sigaction, libc::SYS_rt_sigprocmask, libc::SYS_rt_sigreturn, libc::SYS_pread64, libc::SYS_pwrite64,
        libc::SYS_readv, libc::SYS_writev, libc::SYS_access, libc::SYS_sched_yield, libc::SYS_mremap, libc::SYS_msync,
        libc::SYS_mincore, libc::SYS_madvise, libc::SYS_getpid, libc::SYS_gettid, libc::SYS_getuid, libc::SYS_geteuid,
        libc::SYS_getgid, libc::SYS_getegid, libc::SYS_getppid, libc::SYS_getpgrp, libc::SYS_getpgid, libc::SYS_getsid,
        libc::SYS_arch_prctl, libc::SYS_set_tid_address, libc::SYS_set_robust_list, libc::SYS_rseq, libc::SYS_futex,
        libc::SYS_clock_gettime, libc::SYS_clock_getres, libc::SYS_nanosleep, libc::SYS_clock_nanosleep,
        libc::SYS_gettimeofday, libc::SYS_time, libc::SYS_getrandom, libc::SYS_uname, libc::SYS_getrlimit,
        libc::SYS_getrusage, libc::SYS_open, libc::SYS_openat, libc::SYS_openat2, libc::SYS_readlink, libc::SYS_readlinkat,
        libc::SYS_newfstatat, libc::SYS_statx, libc::SYS_getdents, libc::SYS_getdents64, libc::SYS_getcwd, libc::SYS_chdir,
        libc::SYS_fchdir, libc::SYS_fcntl, libc::SYS_dup, libc::SYS_dup2, libc::SYS_dup3, libc::SYS_close_range,
        libc::SYS_truncate, libc::SYS_ftruncate, libc::SYS_unlink, libc::SYS_unlinkat, libc::SYS_rename,
        libc::SYS_renameat, libc::SYS_renameat2, libc::SYS_mkdir, libc::SYS_mkdirat, libc::SYS_rmdir,
        libc::SYS_link, libc::SYS_linkat, libc::SYS_symlink, libc::SYS_symlinkat, libc::SYS_fsync, libc::SYS_fdatasync,
        libc::SYS_flock, libc::SYS_umask, libc::SYS_restart_syscall, libc::SYS_exit, libc::SYS_exit_group];
    for n in allowed { f.extend([jump(n as u32, 0, 1), statement(0x06, ALLOW)]); }
    // glibc queries its own resource limits using prlimit64(pid=0,...). Hard
    // limits cannot be raised; targeting another process is not allowed.
    f.extend([jump(libc::SYS_prlimit64 as u32, 0, 4), statement(0x20, 16), jump(0, 0, 1),
        statement(0x06, ALLOW), statement(0x06, DENY), statement(0x06, DENY)]);
    install(f)
}

fn seccomp_probe() -> bool {
    let mut f = architecture();
    f.extend([jump(libc::SYS_getppid as u32, 0, 1), statement(0x06, 0x00050000 | libc::EACCES as u32), statement(0x06, 0x7fff0000)]);
    if install(f).is_err() { return false; }
    // SAFETY: no pointers; verifies this child's new filter, not just kernel support.
    unsafe { libc::syscall(libc::SYS_getppid) == -1 && *libc::__errno_location() == libc::EACCES }
}
pub(super) fn probe() -> String {
    // Avoid applying irreversible filters to the caller: re-exec the trusted helper.
    let seccomp = std::env::current_exe().ok().and_then(|exe| std::process::Command::new(exe).arg("__probe_seccomp")
        .env_clear().status().ok()).is_some_and(|s| s.success());
    let landlock = abi();
    let openat2 = pin(Path::new("/"), false, &[]).is_ok();
    let enforced = landlock >= 3 && std::env::current_exe().ok().and_then(|exe| std::process::Command::new(exe)
        .arg("__probe_landlock").env_clear().status().ok()).is_some_and(|s| s.success());
    let unprivileged = unsafe { libc::geteuid() } != 0;
    let pidfd = process_handle(std::process::id()).is_ok();
    let supervisor = verify_supervisor().is_ok();
    format!(r#"{{"profile":"linux-offline-v1","supported":{},"landlock_abi":{},"seccomp":{},"openat2":{},"unprivileged":{},"landlock_enforced":{},"pidfd":{},"supervisor":{}}}"#,
        landlock >= 3 && seccomp && openat2 && unprivileged && enforced && pidfd && supervisor, landlock.max(0), seccomp, openat2, unprivileged, enforced, pidfd, supervisor)
}
pub fn probe_child() -> bool { seccomp_probe() }
pub fn probe_landlock_child() -> bool {
    let version = abi();
    if version < 3 { return false; }
    let attr = Ruleset { handled_access_fs: (1u64 << if version >= 5 { 16 } else { 15 }) - 1 };
    let own = match std::env::current_exe() { Ok(p) => p, Err(_) => return false };
    let own_c = match CString::new(own.as_os_str().as_bytes()) { Ok(p) => p, Err(_) => return false };
    // SAFETY: same ruleset ABI/ownership as enter; probe executes in its own
    // short-lived process and never changes the calling host's Landlock domain.
    let fd = unsafe { libc::syscall(libc::SYS_landlock_create_ruleset,&attr,std::mem::size_of::<Ruleset>(),0u32) };
    if fd < 0 { return false; }
    let fd = unsafe { OwnedFd::from_raw_fd(fd as i32) };
    if add(&fd,&own,READ,&[]).is_err() { return false; }
    unsafe {
        if libc::prctl(libc::PR_SET_NO_NEW_PRIVS,1,0,0,0) != 0 { return false; }
        if libc::syscall(libc::SYS_landlock_restrict_self,fd.as_raw_fd(),0u32) != 0 { return false; }
        let allowed = libc::open(own_c.as_ptr(),libc::O_RDONLY|libc::O_CLOEXEC);
        if allowed < 0 { return false; }
        libc::close(allowed);
        let denied = libc::open(c"/etc/passwd".as_ptr(),libc::O_RDONLY|libc::O_CLOEXEC);
        if denied >= 0 { libc::close(denied); return false; }
        *libc::__errno_location() == libc::EACCES
    }
}

pub(super) fn enter(profile: &Profile, program: &Path, args: &[String], payload_env: &[(String, String)]) -> Result<(), String> {
    if std::fs::read_dir("/proc/self/task").map_err(|_| "Cannot verify single-threaded worker")?.count() != 1 {
        return Err("Sandbox installation requires a dedicated single-threaded worker".into());
    }
    // Never pretend no_new_privs drops an already privileged UID.
    if unsafe { libc::geteuid() } == 0 { return Err("Sandbox workers must run as a non-root user".into()); }
    #[repr(C)] struct CapHeader { version: u32, pid: i32 }
    #[repr(C)] struct CapData { effective: u32, permitted: u32, inheritable: u32 }
    let header = CapHeader { version: 0x20080522, pid: 0 };
    let caps = [CapData { effective: 0, permitted: 0, inheritable: 0 }, CapData { effective: 0, permitted: 0, inheritable: 0 }];
    // SAFETY: capset copies the two initialized v3 capability records.
    if unsafe { libc::syscall(libc::SYS_capset, &header, caps.as_ptr()) } != 0 { return Err(error("cannot drop capabilities")); }
    if unsafe { libc::prctl(47, 4, 0, 0, 0) } != 0 { return Err(error("cannot clear ambient capabilities")); }
    let version = abi();
    if version < 3 { return Err("Linux sandbox unavailable: Landlock ABI >= 3 is required; no weaker fallback was executed.".into()); }
    let control = std::env::current_exe().map_err(|_| "Cannot identify trusted worker")?.canonicalize().map_err(|_| "Cannot pin trusted worker identity")?;
    let library = Path::new("/usr/lib").canonicalize().map_err(|_| "Cannot resolve runtime library directory")?;
    let mut protected = vec![control,library];
    for key in ["NEXO_SECURITY_DEPENDENCIES","NEXO_SECURITY_AUTHORIZATION"] {
        if let Some(config) = payload_env.iter().find(|(k,_)| k == key).and_then(|(_,v)| Path::new(v).canonicalize().ok()) { protected.push(config); }
    }
    let executable = pin(program, true, &[])?;
    let handled = (1u64 << if version >= 5 { 16 } else { 15 }) - 1;
    let attr = Ruleset { handled_access_fs: handled };
    // SAFETY: kernel copies the initialized attr and returns an owned FD.
    let fd = unsafe { libc::syscall(libc::SYS_landlock_create_ruleset, &attr, std::mem::size_of::<Ruleset>(), 0u32) };
    if fd < 0 { return Err(error("cannot create Landlock ruleset")); }
    let ruleset = unsafe { OwnedFd::from_raw_fd(fd as i32) };
    for path in &profile.read { add(&ruleset, path, READ, &[])?; }
    for path in &profile.write { add(&ruleset, path, WRITE, &protected)?; }
    // Pin the same inode used for exec rather than reopening a potentially raced path.
    let rule = Beneath { allowed_access: EXECUTE | (1 << 2), parent_fd: executable.as_raw_fd() };
    if unsafe { libc::syscall(libc::SYS_landlock_add_rule, ruleset.as_raw_fd(), 1u32, &rule, 0u32) } < 0 { return Err(error("cannot grant executable")); }
    let loader = Path::new("/lib64/ld-linux-x86-64.so.2");
    if loader.exists() { add(&ruleset, loader, EXECUTE | (1 << 2), &[])?; }
    apply_limits(&profile.limits)?;
    let argv: Vec<CString> = std::iter::once(program.as_os_str().as_bytes()).chain(args.iter().map(|a| a.as_bytes()))
        .map(|a| CString::new(a).map_err(|_| "Invalid worker argument".to_string())).collect::<Result<_,_>>()?;
    let env: Vec<CString> = payload_env.iter().map(|(k,v)| {
        let mut bytes = k.as_bytes().to_vec(); bytes.push(b'='); bytes.extend(v.as_bytes());
        CString::new(bytes).map_err(|_| "Invalid worker environment".to_string())
    }).collect::<Result<_,_>>()?;
    let argv_ptrs: Vec<_> = argv.iter().map(|a| a.as_ptr()).chain(std::iter::once(std::ptr::null())).collect();
    let env_ptrs: Vec<_> = env.iter().map(|a| a.as_ptr()).chain(std::iter::once(std::ptr::null())).collect();
    // SAFETY: descriptor is owned, arg buffers remain live through execveat.
    // close_range marks all incidental inherited FDs close-on-exec, including
    // the pinned executable. Only explicitly delegated standard output/error survive.
    unsafe {
        if libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 { return Err(error("cannot set no_new_privs")); }
        if libc::syscall(libc::SYS_close_range, 3u32, u32::MAX, 4u32) != 0 { return Err(error("cannot seal inherited descriptors")); }
        if libc::syscall(libc::SYS_landlock_restrict_self, ruleset.as_raw_fd(), 0u32) < 0 { return Err(error("cannot enforce Landlock")); }
    }
    seccomp(executable.as_raw_fd())?;
    let empty = b"\0";
    let r = unsafe { libc::syscall(libc::SYS_execveat, executable.as_raw_fd(), empty.as_ptr(), argv_ptrs.as_ptr(), env_ptrs.as_ptr(), libc::AT_EMPTY_PATH) };
    let _ = r;
    Err(error("cannot exec pinned worker executable"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn limits_are_checked() {
        let mut l = Limits::default(); l.cpu_seconds = 0; assert!(l.validate().is_err());
        l.cpu_seconds = 1; l.memory_mib = 15; assert!(l.validate().is_err());
    }
    #[test]
    fn pidfd_signals_only_its_owned_process() {
        struct Child(std::process::Child);
        impl Drop for Child {
            fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); }
        }
        use std::os::unix::process::ExitStatusExt;
        let mut first = Child(std::process::Command::new("/usr/bin/sleep").arg("60").spawn().unwrap());
        let mut second = Child(std::process::Command::new("/usr/bin/sleep").arg("60").spawn().unwrap());
        let handle = process_handle(first.0.id()).unwrap();
        terminate(&handle);
        let status = first.0.wait().unwrap();
        let untouched = second.0.try_wait().unwrap().is_none();
        assert_eq!(status.signal(),Some(9));
        assert!(untouched);
    }

    #[test]
    fn resource_limits_are_enforced() {
        const CHILD: &str = "NEXO_SANDBOX_RESOURCE_TEST";
        if let Ok(mode) = std::env::var(CHILD) {
            let l = Limits { cpu_seconds: 1, memory_mib: 64, file_size_mib: 0, wall_seconds: 3 };
            let path = std::env::temp_dir().join(format!("nexo-limit-{}",std::process::id()));
            apply_limits(&l).unwrap();
            match mode.as_str() {
                "cpu" => loop { std::hint::black_box(1 + 1); },
                "memory" => {
                    // SAFETY: failed allocation returns null; a successful
                    // allocation would be freed before the child exits.
                    unsafe {
                        // Keep the allocation observable in optimized test binaries.
                        let ptr = std::hint::black_box(libc::malloc(256 * 1024 * 1024));
                        if !ptr.is_null() { libc::free(ptr); libc::_exit(1); }
                        libc::_exit(0);
                    }
                }
                "file" => {
                    use std::io::Write;
                    let mut f = File::create(path).unwrap();
                    let _ = f.write_all(b"must not fit");
                    std::process::exit(1);
                }
                _ => panic!("unknown test mode"),
            }
        }
        use std::os::unix::process::ExitStatusExt;
        for (mode, signal) in [("cpu",Some(24)),("file",Some(25)),("memory",None)] {
            let status = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact","security::sandbox::linux::tests::resource_limits_are_enforced","--nocapture"])
                .env(CHILD,mode).status().unwrap();
            if let Some(signal) = signal { assert_eq!(status.signal(),Some(signal)); }
            else { assert!(status.success()); }
        }
    }

    #[test]
    fn seccomp_and_limits_really_prevent_syscalls() {
        const CHILD: &str = "NEXO_SANDBOX_UNIT_CHILD";
        if std::env::var_os(CHILD).is_some() {
            apply_limits(&Limits::default()).unwrap();
            seccomp(-1).unwrap();
            // SAFETY: socket/fork have scalar args; filters must return EPERM.
            unsafe {
                assert_eq!(libc::socket(libc::AF_INET, libc::SOCK_STREAM, 0), -1);
                assert_eq!(*libc::__errno_location(), libc::EPERM);
                assert_eq!(libc::fork(), -1);
                assert_eq!(*libc::__errno_location(), libc::EPERM);
                let mut l: libc::rlimit = std::mem::zeroed();
                assert_eq!(libc::getrlimit(libc::RLIMIT_AS, &mut l), 0);
                assert_eq!(l.rlim_max, 256 * 1024 * 1024);
            }
            return;
        }
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "security::sandbox::linux::tests::seccomp_and_limits_really_prevent_syscalls", "--nocapture"])
            .env(CHILD, "1").status().unwrap();
        assert!(status.success());
    }
}
