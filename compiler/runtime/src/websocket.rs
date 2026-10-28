//! A WebSocket client for nexo.stream (D169), as phase 0's websocket.py:
//! `websocket("wss://host/path")` connects (TLS for wss://; ws:// only to
//! this machine), each text or binary message the server sends is a
//! message of the stream, and `send` writes a text message on the same
//! connection.

use std::time::{Duration, Instant};

use crate::kafka::{local_host, open, Stream};

const GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/// SHA-1, for the handshake's Sec-WebSocket-Accept only.
pub fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [0x6745_2301, 0xEFCD_AB89, 0x98BA_DCFE, 0x1032_5476, 0xC3D2_E1F0];
    let mut msg = data.to_vec();
    let bits = (data.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bits.to_be_bytes());
    for block in msg.chunks(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([block[i * 4], block[i * 4 + 1], block[i * 4 + 2], block[i * 4 + 3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let [mut a, mut b, mut c, mut d, mut e] = h;
        for (i, wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | (!b & d), 0x5A82_7999),
                20..=39 => (b ^ c ^ d, 0x6ED9_EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1B_BCDC),
                _ => (b ^ c ^ d, 0xCA62_C1D6),
            };
            let t = a.rotate_left(5).wrapping_add(f).wrapping_add(e).wrapping_add(k).wrapping_add(*wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = t;
        }
        for (x, y) in h.iter_mut().zip([a, b, c, d, e]) {
            *x = x.wrapping_add(y);
        }
    }
    let mut out = [0u8; 20];
    for (i, x) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&x.to_be_bytes());
    }
    out
}

pub struct Socket {
    stream: Stream,
    buf: Vec<u8>,
    parts: Vec<u8>,
}

fn random<const N: usize>() -> [u8; N] {
    let mut b = [0u8; N];
    let _ = getrandom::getrandom(&mut b);
    b
}

impl Socket {
    pub fn open(url: &str) -> Result<Socket, String> {
        let (scheme, rest) = url.split_once("://").unwrap_or(("ws", url));
        let (hostport, path) = match rest.find(['/', '?']) {
            Some(i) => (&rest[..i], &rest[i..]),
            None => (rest, "/"),
        };
        let path = if path.starts_with('?') { format!("/{path}") } else { path.to_string() };
        let (host, port) = match hostport.rsplit_once(':').filter(|(_, p)| p.parse::<u16>().is_ok()) {
            Some((h, p)) => (h.trim_matches(['[', ']']).to_lowercase(), p.parse::<u16>().ok()),
            None => (hostport.trim_matches(['[', ']']).to_lowercase(), None),
        };
        let tls = scheme == "wss";
        if !tls && !local_host(&host) {
            return Err(format!("{host} is reached with wss:// only"));
        }
        let mut stream = open(&host, port.unwrap_or(if tls { 443 } else { 80 }), tls)?;
        let key = crate::crypto::b64(&random::<16>());
        let request = format!(
            "GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: nexo/0.1\r\n\r\n"
        );
        stream.write_all(request.as_bytes()).map_err(|e| format!("connection lost: {}", crate::pg::os_text(&e)))?;
        let mut head = Vec::new();
        let mut chunk = [0u8; 4096];
        let end = loop {
            if let Some(i) = head.windows(4).position(|w| w == b"\r\n\r\n") {
                break i;
            }
            match stream.read(&mut chunk) {
                Ok(0) => return Err("the server closed the connection".into()),
                Ok(k) => head.extend_from_slice(&chunk[..k]),
                Err(e) => return Err(format!("connection lost: {}", crate::pg::os_text(&e))),
            }
        };
        let buf = head[end + 4..].to_vec();
        let text = String::from_utf8_lossy(&head[..end]).into_owned();
        let mut lines = text.split("\r\n");
        let status = lines.next().and_then(|l| l.split(' ').nth(1)).unwrap_or("?").to_string();
        let mut accept = None;
        for line in lines {
            if let Some((k, v)) = line.split_once(':') {
                if k.trim().eq_ignore_ascii_case("sec-websocket-accept") {
                    accept = Some(v.trim().to_string());
                }
            }
        }
        let want = crate::crypto::b64(&sha1(format!("{key}{GUID}").as_bytes()));
        if status != "101" || accept.as_deref() != Some(want.as_str()) {
            return Err(format!("the server does not open a WebSocket (it answers {status})"));
        }
        Ok(Socket { stream, buf, parts: Vec::new() })
    }

    fn send_frame(&mut self, opcode: u8, payload: &[u8]) -> Result<(), String> {
        let mask = random::<4>();
        let n = payload.len();
        let mut out = vec![0x80 | opcode];
        if n < 126 {
            out.push(0x80 | n as u8);
        } else if n < 65536 {
            out.push(0x80 | 126);
            out.extend_from_slice(&(n as u16).to_be_bytes());
        } else {
            out.push(0x80 | 127);
            out.extend_from_slice(&(n as u64).to_be_bytes());
        }
        out.extend_from_slice(&mask);
        out.extend(payload.iter().enumerate().map(|(i, b)| b ^ mask[i % 4]));
        self.stream.write_all(&out).map_err(|e| format!("connection lost: {}", crate::pg::os_text(&e)))
    }

    pub fn send(&mut self, text: &str) -> Result<(), String> {
        self.send_frame(1, text.as_bytes())
    }

    /// (fin, opcode, payload) of a whole frame in the buffer, or None.
    fn frame(&mut self) -> Option<(bool, u8, Vec<u8>)> {
        let b = &self.buf;
        if b.len() < 2 {
            return None;
        }
        let (mut n, mut at) = ((b[1] & 0x7F) as usize, 2);
        if n == 126 {
            if b.len() < 4 {
                return None;
            }
            n = u16::from_be_bytes([b[2], b[3]]) as usize;
            at = 4;
        } else if n == 127 {
            if b.len() < 10 {
                return None;
            }
            n = u64::from_be_bytes([b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9]]) as usize;
            at = 10;
        }
        let masked = b[1] & 0x80 != 0;
        let mask = if masked { b.get(at..at + 4).map(|m| [m[0], m[1], m[2], m[3]]) } else { None };
        if masked {
            at += 4;
        }
        if b.len() < at + n {
            return None;
        }
        let mut payload = b[at..at + n].to_vec();
        if let Some(m) = mask {
            for (i, x) in payload.iter_mut().enumerate() {
                *x ^= m[i % 4];
            }
        }
        let (fin, opcode) = (b[0] & 0x80 != 0, b[0] & 0x0F);
        self.buf.drain(..at + n);
        Some((fin, opcode, payload))
    }

    /// The messages that arrive within `wait`.
    pub fn messages(&mut self, wait: Duration) -> Result<Vec<String>, String> {
        let mut out = Vec::new();
        let end = Instant::now() + wait;
        loop {
            while let Some((fin, opcode, payload)) = self.frame() {
                match opcode {
                    9 => self.send_frame(10, &payload)?,
                    8 => {
                        let _ = self.send_frame(8, &payload[..payload.len().min(2)]);
                        return Err("the server closed the connection".into());
                    }
                    0..=2 => {
                        self.parts.extend(payload);
                        if fin {
                            out.push(String::from_utf8_lossy(&std::mem::take(&mut self.parts)).into_owned());
                        }
                    }
                    _ => {}
                }
            }
            let left = end.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Ok(out);
            }
            self.stream.set_timeout(Some(left));
            let mut chunk = [0u8; 65536];
            let got = self.stream.read(&mut chunk);
            self.stream.set_timeout(Some(Duration::from_secs(30)));
            match got {
                Ok(0) => return Err("the server closed the connection".into()),
                Ok(k) => self.buf.extend_from_slice(&chunk[..k]),
                Err(e) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) => return Ok(out),
                Err(e) => return Err(format!("connection lost: {}", crate::pg::os_text(&e))),
            }
        }
    }
}

/// The local stream of a WebSocket's dead letters: its address, with what
/// a file name cannot have as `_`, then `.dead`.
pub fn dead_name(url: &str) -> String {
    let rest = url.split_once("://").map(|x| x.1).unwrap_or(url);
    let clean: String = rest.chars().map(|c| if c.is_ascii_alphanumeric() || "._-".contains(c) { c } else { '_' }).collect();
    format!("{clean}.dead")
}
