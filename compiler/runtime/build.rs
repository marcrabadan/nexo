//! For a program built for the web (D175), the time zones of section 15
//! go inside the runtime: a page has no /usr/share/zoneinfo. The TZif
//! files of the build machine (or NEXO_TZDIR) are packed into one blob,
//! `name \0 length(u32, little-endian) bytes` each, read by time.rs.

use std::path::Path;

fn walk(dir: &Path, base: &Path, out: &mut Vec<(String, Vec<u8>)>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    for e in entries.flatten() {
        let path = e.path();
        if path.is_dir() {
            walk(&path, base, out);
        } else if let Ok(data) = std::fs::read(&path) {
            if data.starts_with(b"TZif") {
                let name = path.strip_prefix(base).unwrap().to_string_lossy().replace('\\', "/");
                out.push((name, data));
            }
        }
    }
}

fn main() {
    println!("cargo:rerun-if-env-changed=NEXO_TZDIR");
    let out = Path::new(&std::env::var("OUT_DIR").unwrap()).join("zones.bin");
    let mut blob = Vec::new();
    if std::env::var("CARGO_CFG_TARGET_FAMILY").is_ok_and(|f| f.split(',').any(|x| x == "wasm")) {
        let dir = std::env::var("NEXO_TZDIR").unwrap_or_else(|_| "/usr/share/zoneinfo".into());
        let mut zones = Vec::new();
        walk(Path::new(&dir), Path::new(&dir), &mut zones);
        zones.sort();
        for (name, data) in zones {
            blob.extend_from_slice(name.as_bytes());
            blob.push(0);
            blob.extend_from_slice(&(data.len() as u32).to_le_bytes());
            blob.extend_from_slice(&data);
        }
    }
    std::fs::write(out, blob).unwrap();
}
