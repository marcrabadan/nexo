//! What keeps the compiler fast on a large program (D180): the settings
//! read from the environment once, and a hasher for the maps of names and
//! nodes that is much cheaper than the default one (whose protection
//! against chosen keys a compiler does not need). Maps built with it
//! iterate in the same order on every run, which no output depends on.

use std::hash::{BuildHasherDefault, Hasher};
use std::sync::OnceLock;

/// Shield's dependency ceilings are on (NEXO_SECURITY_DEPENDENCIES).
pub fn security_dependencies() -> bool {
    static ON: OnceLock<bool> = OnceLock::new();
    *ON.get_or_init(|| std::env::var_os("NEXO_SECURITY_DEPENDENCIES").is_some())
}

/// The program runs in Shield's offline sandbox.
pub fn security_sandbox() -> bool {
    static ON: OnceLock<bool> = OnceLock::new();
    *ON.get_or_init(|| std::env::var("NEXO_SECURITY_SANDBOX").is_ok_and(|s| s == "linux-offline-v1"))
}

/// The hash of rustc's own maps (FxHash): a multiply and a rotate per word.
#[derive(Default, Clone, Copy)]
pub struct FxHasher {
    hash: u64,
}

const SEED: u64 = 0x51_7c_c1_b7_27_22_0a_95;

impl FxHasher {
    #[inline]
    fn add(&mut self, word: u64) {
        self.hash = (self.hash.rotate_left(5) ^ word).wrapping_mul(SEED);
    }
}

impl Hasher for FxHasher {
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        let mut chunks = bytes.chunks_exact(8);
        for c in &mut chunks {
            self.add(u64::from_le_bytes(c.try_into().unwrap()));
        }
        let rest = chunks.remainder();
        if !rest.is_empty() {
            let mut w = [0u8; 8];
            w[..rest.len()].copy_from_slice(rest);
            self.add(u64::from_le_bytes(w));
        }
    }

    #[inline]
    fn write_u8(&mut self, i: u8) {
        self.add(i as u64);
    }

    #[inline]
    fn write_u32(&mut self, i: u32) {
        self.add(i as u64);
    }

    #[inline]
    fn write_u64(&mut self, i: u64) {
        self.add(i);
    }

    #[inline]
    fn write_usize(&mut self, i: usize) {
        self.add(i as u64);
    }

    #[inline]
    fn finish(&self) -> u64 {
        self.hash
    }
}

pub type HashMap<K, V> = std::collections::HashMap<K, V, BuildHasherDefault<FxHasher>>;
pub type HashSet<K> = std::collections::HashSet<K, BuildHasherDefault<FxHasher>>;
