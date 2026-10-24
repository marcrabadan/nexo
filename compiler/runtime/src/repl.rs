//! The compiled REPL (D88). `nexo` with no arguments keeps the session as a
//! program: each accepted input is added to it, and the whole program is
//! compiled and run again. The inputs before the new one replay: until the
//! compiled code calls `nx_repl_live` (at the first statement of the new
//! input) nothing is written to the output, and `input()`, reading a file
//! and writing one give the answers they gave the first time, from a
//! journal (`NEXO_REPL_JOURNAL`). From then on they really happen and are
//! added to the journal.
//!
//! Without `NEXO_REPL_JOURNAL` none of this is active.

use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};

struct Event {
    kind: String,
    ok: bool,
    data: Vec<u8>,
}

struct Journal {
    events: Vec<Event>,
    next: usize,
    file: Option<File>,
}

static JOURNAL: OnceLock<Option<Mutex<Journal>>> = OnceLock::new();
static LIVE: AtomicBool = AtomicBool::new(false);

fn parse(bytes: &[u8]) -> Vec<Event> {
    let mut events = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        let Some(nl) = bytes[i..].iter().position(|&b| b == b'\n') else { break };
        let head = String::from_utf8_lossy(&bytes[i..i + nl]).into_owned();
        let parts: Vec<&str> = head.split(' ').collect();
        if parts.len() != 3 {
            break;
        }
        let Ok(len) = parts[2].parse::<usize>() else { break };
        let start = i + nl + 1;
        if start + len > bytes.len() {
            break;
        }
        events.push(Event { kind: parts[0].to_string(), ok: parts[1] == "1", data: bytes[start..start + len].to_vec() });
        i = start + len + 1;
    }
    events
}

fn journal() -> Option<&'static Mutex<Journal>> {
    JOURNAL
        .get_or_init(|| {
            let path = std::env::var("NEXO_REPL_JOURNAL").ok()?;
            let mut bytes = Vec::new();
            if let Ok(mut f) = File::open(&path) {
                let _ = f.read_to_end(&mut bytes);
            }
            let file = OpenOptions::new().create(true).append(true).open(&path).ok();
            Some(Mutex::new(Journal { events: parse(&bytes), next: 0, file }))
        })
        .as_ref()
}

/// The program runs inside the REPL.
pub fn active() -> bool {
    journal().is_some()
}

/// The inputs before the new one are running again.
pub fn replaying() -> bool {
    !LIVE.load(Ordering::Relaxed) && active()
}

/// Called at the first statement of the new input.
#[no_mangle]
pub extern "C" fn nx_repl_live() {
    LIVE.store(true, Ordering::Relaxed);
}

/// Run an operation through the journal: replayed while replaying (when
/// the journal has the same operation next), done and recorded otherwise.
fn through(kind: &str, real: impl FnOnce() -> (bool, Vec<u8>)) -> (bool, Vec<u8>) {
    let Some(j) = journal() else { return real() };
    if replaying() {
        let mut j = j.lock().unwrap();
        let next = j.next;
        let found = j.events.get(next).filter(|e| e.kind == kind).map(|e| (e.ok, e.data.clone()));
        if let Some(answer) = found {
            j.next += 1;
            return answer;
        }
    }
    let (ok, data) = real();
    if !replaying() {
        let mut j = j.lock().unwrap();
        if let Some(f) = j.file.as_mut() {
            let _ = write!(f, "{kind} {} {}\n", if ok { 1 } else { 0 }, data.len());
            let _ = f.write_all(&data);
            let _ = f.write_all(b"\n");
            let _ = f.flush();
        }
    }
    (ok, data)
}

fn encode_error(e: &std::io::Error) -> Vec<u8> {
    let kind = if e.kind() == std::io::ErrorKind::NotFound { "NotFound" } else { "Other" };
    let text = e.to_string();
    let reason = match text.find(" (os error") {
        Some(i) => &text[..i],
        None => &text,
    };
    format!("{kind}\t{reason}").into_bytes()
}

fn decode_error(data: &[u8]) -> std::io::Error {
    let text = String::from_utf8_lossy(data);
    let (kind, reason) = text.split_once('\t').unwrap_or(("Other", &text));
    let kind = if kind == "NotFound" { std::io::ErrorKind::NotFound } else { std::io::ErrorKind::Other };
    std::io::Error::new(kind, reason.to_string())
}

/// A line of the standard input. Inside the REPL it is read one byte at a
/// time, so the rest of the input stays for the REPL itself.
pub fn input_line(real: impl FnOnce() -> String) -> String {
    if !active() {
        return real();
    }
    let (_, data) = through("in", || {
        let mut line = unbuffered_line().unwrap_or_default();
        while line.last() == Some(&b'\n') || line.last() == Some(&b'\r') {
            line.pop();
        }
        (true, line)
    });
    String::from_utf8_lossy(&data).into_owned()
}

/// One line of the standard input, newline included, reading byte by
/// byte; None at the end of the input.
pub fn unbuffered_line() -> Option<Vec<u8>> {
    use std::os::fd::FromRawFd;
    let mut stdin = std::mem::ManuallyDrop::new(unsafe { File::from_raw_fd(0) });
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
            Ok(_) => return if line.is_empty() { None } else { Some(line) },
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return if line.is_empty() { None } else { Some(line) },
        }
    }
}

/// The bytes of a file.
pub fn read_file(real: impl FnOnce() -> std::io::Result<Vec<u8>>) -> std::io::Result<Vec<u8>> {
    let (ok, data) = through("rd", || match real() {
        Ok(bytes) => (true, bytes),
        Err(e) => (false, encode_error(&e)),
    });
    if ok {
        Ok(data)
    } else {
        Err(decode_error(&data))
    }
}

/// Write a file.
pub fn write_file(real: impl FnOnce() -> std::io::Result<()>) -> std::io::Result<()> {
    let (ok, data) = through("wr", || match real() {
        Ok(()) => (true, Vec::new()),
        Err(e) => (false, encode_error(&e)),
    });
    if ok {
        Ok(())
    } else {
        Err(decode_error(&data))
    }
}

/// The value of an expression typed at the prompt, as phase 0 shows it.
#[no_mangle]
pub extern "C" fn nx_repl_show(tag: u64, bits: u64) {
    let v = crate::value::V { tag, bits };
    if v.is_none() {
        return;
    }
    let mut text = crate::fmt::repr(v);
    text.push('\n');
    crate::state::write_out(&text);
}
