//! Operator-owned encrypted checkpoint store. No application path is authority.
use crate::json::Json;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
pub fn digest(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
#[derive(Clone, PartialEq)]
pub struct Grant {
    pub directory: PathBuf,
    pub device: u64,
    pub inode: u64,
    pub key: String,
    pub max_bytes: usize,
    pub budget: Json,
}
pub fn parse(value: &Json, base: &Path) -> Result<Grant, String> {
    let bad = || "Invalid operator state grant".to_string();
    let Json::Obj(fields) = value else {
        return Err(bad());
    };
    if fields.len() != 4
        || ["directory", "key", "max_bytes", "budget"]
            .iter()
            .any(|k| !value.has(k))
    {
        return Err(bad());
    }
    let key = value.get("key").str().ok_or_else(bad)?;
    if key.is_empty() || !key.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_') {
        return Err(bad());
    }
    let size = value
        .get("max_bytes")
        .int()
        .filter(|n| (4096..=16777216).contains(n))
        .ok_or_else(bad)? as usize;
    let path = base.join(value.get("directory").str().ok_or_else(bad)?);
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let info = std::fs::symlink_metadata(&path).map_err(|_| bad())?;
        extern "C" {
            fn geteuid() -> u32;
        }
        // Static manifest loading and runtime opening enforce the same owner.
        if !info.is_dir() || info.mode() & 0o7777 != 0o700 || info.uid() != unsafe { geteuid() } {
            return Err(bad());
        }
        let mut budget = value.get("budget").clone();
        fn canonical(j: &mut Json) {
            match j {
                Json::Obj(v) => {
                    v.sort_by(|a, b| a.0.cmp(&b.0));
                    for (_, j) in v {
                        canonical(j)
                    }
                }
                Json::Arr(v) => {
                    for j in v {
                        canonical(j)
                    }
                }
                _ => (),
            }
        }
        canonical(&mut budget);
        if budget != Json::Null {
            let Json::Obj(fields) = &budget else {
                return Err(bad());
            };
            if fields.len() != 4
                || ["limit", "currency", "period", "prices"]
                    .iter()
                    .any(|k| !budget.has(k))
            {
                return Err(bad());
            }
            let amount = |v: &Json, positive: bool| -> Result<(), String> {
                let t = v.str().ok_or_else(bad)?;
                if t.is_empty()
                    || t.len() > 32
                    || !t.bytes().all(|c| c.is_ascii_digit() || c == b'.')
                    || t.bytes().filter(|c| *c == b'.').count() > 1
                    || t.split_once('.').is_some_and(|(_, f)| f.len() > 6)
                {
                    return Err(bad());
                }
                let n = crate::dec::Dec::parse(t).ok_or_else(bad)?;
                if n.cmp(&crate::dec::Dec::zero()) == std::cmp::Ordering::Less
                    || (positive && n.is_zero())
                    || n.cmp(&crate::dec::Dec::parse("1000000000000").unwrap())
                        == std::cmp::Ordering::Greater
                {
                    return Err(bad());
                }
                Ok(())
            };
            amount(budget.get("limit"), true)?;
            if !matches!(budget.get("period").str(), Some("day" | "month")) {
                return Err(bad());
            }
            let c = budget.get("currency").str().ok_or_else(bad)?;
            if c.len() != 3 || !c.bytes().all(|c| c.is_ascii_uppercase()) {
                return Err(bad());
            }
            let Json::Obj(prices) = budget.get("prices") else {
                return Err(bad());
            };
            if prices.is_empty() {
                return Err(bad());
            }
            for (model, prices) in prices {
                let Json::Arr(p) = prices else {
                    return Err(bad());
                };
                if !model.contains(':') || p.len() != 2 {
                    return Err(bad());
                }
                amount(&p[0], false)?;
                amount(&p[1], false)?;
            }
        }
        return Ok(Grant {
            directory: path.canonicalize().map_err(|_| bad())?,
            device: info.dev(),
            inode: info.ino(),
            key: key.into(),
            max_bytes: size,
            budget,
        });
    }
    #[cfg(not(unix))]
    {
        let _ = (path, key, size);
        Err("Confined agent state is unsupported on this host".into())
    }
}
#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
mod host {
    use super::*;
    use aes_gcm::{
        aead::{Aead, KeyInit, Payload},
        Aes256Gcm, Nonce,
    };
    use std::{
        ffi::CString,
        fs::File,
        io::{Read, Write},
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::fs::MetadataExt,
        },
    };
    pub struct Store {
        root: File,
        cipher: Aes256Gcm,
        grant: Grant,
    }
    pub struct Lock {
        _file: File,
    }
    fn bad() -> std::io::Error {
        std::io::Error::other("Unsafe agent checkpoint")
    }
    fn c(s: &str) -> std::io::Result<CString> {
        CString::new(s).map_err(|_| bad())
    }
    impl Store {
        pub fn directory(&self) -> &Path {
            &self.grant.directory
        }
        pub fn budget(&self) -> Option<&Json> {
            if self.grant.budget == Json::Null {
                None
            } else {
                Some(&self.grant.budget)
            }
        }
        pub fn open(grant: &Grant) -> std::io::Result<Self> {
            let key = std::env::var(&grant.key).map_err(|_| bad())?;
            if key.len() != 64 || !key.bytes().all(|c| c.is_ascii_hexdigit()) {
                return Err(bad());
            }
            let bytes = (0..32)
                .map(|i| u8::from_str_radix(&key[i * 2..i * 2 + 2], 16).map_err(|_| bad()))
                .collect::<std::io::Result<Vec<_>>>()?;
            let root = unsafe {
                libc::open(
                    c(grant.directory.to_str().ok_or_else(bad)?)?.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            };
            if root < 0 {
                return Err(std::io::Error::last_os_error());
            }
            let root = unsafe { File::from_raw_fd(root) };
            let info = root.metadata()?;
            if info.dev() != grant.device
                || info.ino() != grant.inode
                || info.uid() != unsafe { libc::geteuid() }
                || info.mode() & 0o7777 != 0o700
            {
                return Err(bad());
            }
            Ok(Self {
                root,
                cipher: Aes256Gcm::new_from_slice(&bytes).map_err(|_| bad())?,
                grant: grant.clone(),
            })
        }
        fn at(&self, name: &str, flags: i32) -> std::io::Result<File> {
            let fd = unsafe {
                libc::openat(
                    self.root.as_raw_fd(),
                    c(name)?.as_ptr(),
                    flags | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    0o600,
                )
            };
            if fd < 0 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(unsafe { File::from_raw_fd(fd) })
            }
        }
        fn valid(&self, file: &File) -> std::io::Result<()> {
            let i = file.metadata()?;
            if !i.is_file()
                || i.nlink() != 1
                || i.uid() != unsafe { libc::geteuid() }
                || i.mode() & 0o7777 != 0o600
                || i.len() > self.grant.max_bytes as u64
            {
                return Err(bad());
            }
            Ok(())
        }
        pub fn lock(&self, name: &str) -> std::io::Result<Lock> {
            let f = self.at(&format!("{name}.lock"), libc::O_RDWR | libc::O_CREAT)?;
            self.valid(&f)?;
            if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(Lock { _file: f })
        }
        pub fn read(&self, name: &str) -> std::io::Result<Option<Json>> {
            let f = match self.at(&format!("{name}.state"), libc::O_RDONLY | libc::O_NONBLOCK) {
                Ok(f) => f,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
                Err(e) => return Err(e),
            };
            self.valid(&f)?;
            let mut data = Vec::new();
            f.take(self.grant.max_bytes as u64 + 1)
                .read_to_end(&mut data)?;
            if data.len() < 28 || data.len() > self.grant.max_bytes {
                return Err(bad());
            }
            let text = self
                .cipher
                .decrypt(
                    Nonce::from_slice(&data[..12]),
                    Payload {
                        msg: &data[12..],
                        aad: name.as_bytes(),
                    },
                )
                .map_err(|_| bad())?;
            let text = std::str::from_utf8(&text).map_err(|_| bad())?;
            if !crate::json::nesting_within(text, 128) {
                return Err(bad());
            }
            Ok(Some(crate::json::parse_unique(text).ok_or_else(bad)?))
        }
        pub fn write(&self, name: &str, value: &Json) -> std::io::Result<()> {
            let text = value.compact();
            if text.len() + 28 > self.grant.max_bytes {
                return Err(bad());
            }
            let mut nonce = [0; 12];
            getrandom::getrandom(&mut nonce).map_err(|_| bad())?;
            let encrypted = self
                .cipher
                .encrypt(
                    Nonce::from_slice(&nonce),
                    Payload {
                        msg: text.as_bytes(),
                        aad: name.as_bytes(),
                    },
                )
                .map_err(|_| bad())?;
            let mut random = [0; 16];
            getrandom::getrandom(&mut random).map_err(|_| bad())?;
            let fresh = format!(
                "{}.tmp",
                random
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>()
            );
            let mut f = self.at(&fresh, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
            let result = crate::checkpoints::commit(
                || {
                    f.write_all(&nonce)?;
                    f.write_all(&encrypted)?;
                    f.sync_all()
                },
                || {
                    if unsafe {
                        libc::renameat(
                            self.root.as_raw_fd(),
                            c(&fresh)?.as_ptr(),
                            self.root.as_raw_fd(),
                            c(&format!("{name}.state"))?.as_ptr(),
                        )
                    } != 0
                    {
                        Err(std::io::Error::last_os_error())
                    } else {
                        Ok(())
                    }
                },
                || self.root.sync_all(),
            );
            unsafe { libc::unlinkat(self.root.as_raw_fd(), c(&fresh)?.as_ptr(), 0) };
            result
        }
    }
}
#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
pub use host::{Lock, Store};
#[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
pub struct Store;
#[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
pub struct Lock;
#[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
impl Store {
    pub fn directory(&self) -> &Path {
        unreachable!()
    }
    pub fn budget(&self) -> Option<&Json> {
        None
    }
    pub fn open(_: &Grant) -> std::io::Result<Self> {
        Err(std::io::Error::other("Unsupported agent state host"))
    }
    pub fn lock(&self, _: &str) -> std::io::Result<Lock> {
        unreachable!()
    }
    pub fn read(&self, _: &str) -> std::io::Result<Option<Json>> {
        unreachable!()
    }
    pub fn write(&self, _: &str, _: &Json) -> std::io::Result<()> {
        unreachable!()
    }
}
