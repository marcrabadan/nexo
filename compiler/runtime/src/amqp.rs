//! A RabbitMQ client for nexo.stream (D169), as phase 0's amqp.py: AMQP
//! 0-9-1 over a socket (TLS for amqps://), PLAIN sign-in, durable queues,
//! publisher confirms, and Basic.Get with acknowledgements.

use crate::kafka::{local_host, open, Stream};

const FRAME_END: u8 = 0xCE;

#[derive(Debug, Clone)]
pub struct AmqpError {
    pub reason: String,
    /// the connection is gone: it is made again
    pub lost: bool,
}

fn fail(reason: impl Into<String>, lost: bool) -> AmqpError {
    AmqpError { reason: reason.into(), lost }
}

/// A value of a field table (the ones nexo reads and writes).
#[derive(Clone, Debug)]
pub enum Field {
    Int(i64),
    Text(String),
    Other,
}

fn shortstr(s: &str) -> Vec<u8> {
    let mut out = vec![s.len() as u8];
    out.extend_from_slice(s.as_bytes());
    out
}

fn longstr(b: &[u8]) -> Vec<u8> {
    let mut out = (b.len() as u32).to_be_bytes().to_vec();
    out.extend_from_slice(b);
    out
}

fn table(pairs: &[(&str, Field)]) -> Vec<u8> {
    let mut body = Vec::new();
    for (k, v) in pairs {
        body.extend(shortstr(k));
        match v {
            Field::Int(n) => {
                body.push(b'l');
                body.extend_from_slice(&n.to_be_bytes());
            }
            Field::Text(t) => {
                body.push(b'S');
                body.extend(longstr(t.as_bytes()));
            }
            Field::Other => {
                body.push(b'V');
            }
        }
    }
    let mut out = (body.len() as u32).to_be_bytes().to_vec();
    out.extend(body);
    out
}

struct Reader<'a> {
    d: &'a [u8],
    p: usize,
}

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], AmqpError> {
        if self.p + n > self.d.len() {
            return Err(fail("a frame of the broker is cut short", true));
        }
        let out = &self.d[self.p..self.p + n];
        self.p += n;
        Ok(out)
    }
    fn u8(&mut self) -> Result<u8, AmqpError> {
        Ok(self.take(1)?[0])
    }
    fn u16(&mut self) -> Result<u16, AmqpError> {
        let b = self.take(2)?;
        Ok(u16::from_be_bytes([b[0], b[1]]))
    }
    fn u32(&mut self) -> Result<u32, AmqpError> {
        let b = self.take(4)?;
        Ok(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    }
    fn u64(&mut self) -> Result<u64, AmqpError> {
        let b = self.take(8)?;
        Ok(u64::from_be_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]]))
    }
    fn shortstr(&mut self) -> Result<String, AmqpError> {
        let n = self.u8()? as usize;
        Ok(String::from_utf8_lossy(self.take(n)?).into_owned())
    }
    fn longstr(&mut self) -> Result<&'a [u8], AmqpError> {
        let n = self.u32()? as usize;
        self.take(n)
    }
    fn table(&mut self) -> Result<Vec<(String, Field)>, AmqpError> {
        let end = self.u32()? as usize + self.p;
        let mut out = Vec::new();
        while self.p < end {
            let k = self.shortstr()?;
            out.push((k, self.value()?));
        }
        Ok(out)
    }
    fn value(&mut self) -> Result<Field, AmqpError> {
        let t = self.u8()?;
        Ok(match t {
            b't' | b'b' | b'B' => {
                self.take(1)?;
                Field::Other
            }
            b's' | b'u' => {
                self.take(2)?;
                Field::Other
            }
            b'I' => Field::Int(self.u32()? as i32 as i64),
            b'i' => Field::Int(self.u32()? as i64),
            b'l' => Field::Int(self.u64()? as i64),
            b'f' => {
                self.take(4)?;
                Field::Other
            }
            b'd' | b'T' => {
                self.take(8)?;
                Field::Other
            }
            b'D' => {
                self.take(5)?;
                Field::Other
            }
            b'S' => Field::Text(String::from_utf8_lossy(self.longstr()?).into_owned()),
            b'x' => {
                self.longstr()?;
                Field::Other
            }
            b'F' => {
                self.table()?;
                Field::Other
            }
            b'A' => {
                let end = self.u32()? as usize + self.p;
                while self.p < end {
                    self.value()?;
                }
                Field::Other
            }
            b'V' => Field::Other,
            _ => return Err(fail(format!("a field of type {} is not read", t as char), true)),
        })
    }
}

fn method(cls: u16, mid: u16, args: &[u8]) -> Vec<u8> {
    let mut out = cls.to_be_bytes().to_vec();
    out.extend_from_slice(&mid.to_be_bytes());
    out.extend_from_slice(args);
    out
}

/// A message got from a queue: (delivery tag, headers, body).
pub type Got = (u64, Vec<(String, Field)>, Vec<u8>);

/// One connection with one channel.
pub struct Connection {
    stream: Stream,
    buf: Vec<u8>,
    frame_max: u32,
    declared: Vec<String>,
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// (tls, user, host, port, vhost) of amqp(s)://user@host:port/vhost.
pub fn parse_url(url: &str) -> (bool, Option<String>, String, Option<u16>, String) {
    let (scheme, rest) = url.split_once("://").unwrap_or(("amqp", url));
    let (hostpart, path) = rest.split_once('/').map_or((rest, ""), |(a, b)| (a, b));
    let (user, hostport) = match hostpart.rsplit_once('@') {
        Some((u, h)) => (Some(percent_decode(u.split(':').next().unwrap_or(u))), h),
        None => (None, hostpart),
    };
    let (host, port) = if let Some(inner) = hostport.strip_prefix('[') {
        let (h, after) = inner.split_once(']').unwrap_or((inner, ""));
        (h.to_string(), after.strip_prefix(':').and_then(|p| p.parse().ok()))
    } else {
        match hostport.rsplit_once(':') {
            Some((h, p)) => (h.to_string(), p.parse().ok()),
            None => (hostport.to_string(), None),
        }
    };
    let vhost = if path.is_empty() { "/".to_string() } else { percent_decode(path.split('?').next().unwrap_or(path)) };
    (scheme == "amqps", user, if host.is_empty() { "localhost".into() } else { host.to_lowercase() }, port, vhost)
}

impl Connection {
    pub fn open(url: &str, password: Option<&str>) -> Result<Connection, AmqpError> {
        let (tls, user, host, port, vhost) = parse_url(url);
        if !tls && !local_host(&host) {
            return Err(fail(format!("{host} is reached with amqps:// only"), false));
        }
        let port = port.unwrap_or(if tls { 5671 } else { 5672 });
        let stream = open(&host, port, tls).map_err(|e| fail(e, true))?;
        let mut c = Connection { stream, buf: Vec::new(), frame_max: 131072, declared: Vec::new() };
        c.write(b"AMQP\x00\x00\x09\x01")?;
        c.expect(0, 10, 10)?;
        let props = table(&[("product", Field::Text("nexo".into()))]);
        let mut args = props;
        args.extend(shortstr("PLAIN"));
        args.extend(longstr(format!("\0{}\0{}", user.unwrap_or_else(|| "guest".into()), password.unwrap_or("")).as_bytes()));
        args.extend(shortstr("en_US"));
        c.send(0, &method(10, 11, &args), 1)?;
        let tune = c.expect(0, 10, 30)?;
        let mut r = Reader { d: &tune, p: 0 };
        let (channel_max, frame_max, heartbeat) = (r.u16()?, r.u32()?, r.u16()?);
        c.frame_max = if frame_max == 0 { 131072 } else { frame_max };
        let mut ok = channel_max.to_be_bytes().to_vec();
        ok.extend_from_slice(&c.frame_max.to_be_bytes());
        ok.extend_from_slice(&heartbeat.to_be_bytes());
        c.send(0, &method(10, 31, &ok), 1)?;
        let mut open_args = shortstr(&vhost);
        open_args.extend(shortstr(""));
        open_args.push(0);
        c.send(0, &method(10, 40, &open_args), 1)?;
        c.expect(0, 10, 41)?;
        c.send(1, &method(20, 10, &shortstr("")), 1)?;
        c.expect(1, 20, 11)?;
        c.send(1, &method(85, 10, &[0]), 1)?;
        c.expect(1, 85, 11)?;
        Ok(c)
    }

    fn write(&mut self, data: &[u8]) -> Result<(), AmqpError> {
        self.stream.write_all(data).map_err(|e| fail(format!("connection lost: {}", crate::pg::os_text(&e)), true))
    }

    fn send(&mut self, channel: u16, payload: &[u8], kind: u8) -> Result<(), AmqpError> {
        let mut out = vec![kind];
        out.extend_from_slice(&channel.to_be_bytes());
        out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        out.extend_from_slice(payload);
        out.push(FRAME_END);
        self.write(&out)
    }

    fn read(&mut self, n: usize) -> Result<Vec<u8>, AmqpError> {
        let mut chunk = [0u8; 65536];
        while self.buf.len() < n {
            match self.stream.read(&mut chunk) {
                Ok(0) => return Err(fail("the broker closed the connection", true)),
                Ok(k) => self.buf.extend_from_slice(&chunk[..k]),
                Err(e) => return Err(fail(format!("connection lost: {}", crate::pg::os_text(&e)), true)),
            }
        }
        let rest = self.buf.split_off(n);
        Ok(std::mem::replace(&mut self.buf, rest))
    }

    fn frame(&mut self) -> Result<(u8, Vec<u8>), AmqpError> {
        loop {
            let head = self.read(7)?;
            let kind = head[0];
            let size = u32::from_be_bytes([head[3], head[4], head[5], head[6]]) as usize;
            let payload = self.read(size)?;
            if self.read(1)?[0] != FRAME_END {
                return Err(fail("a frame of the broker is not valid", true));
            }
            if kind == 8 {
                self.send(0, &[], 8)?;
                continue;
            }
            return Ok((kind, payload));
        }
    }

    /// The next method: (class, method, its arguments).
    fn method_frame(&mut self) -> Result<(u16, u16, Vec<u8>), AmqpError> {
        let (kind, payload) = self.frame()?;
        if kind != 1 {
            return Err(fail("the broker sends content out of order", true));
        }
        let mut r = Reader { d: &payload, p: 0 };
        let (cls, mid) = (r.u16()?, r.u16()?);
        if (cls, mid) == (10, 50) || (cls, mid) == (20, 40) {
            let (code, text) = (r.u16()?, r.shortstr()?);
            if cls == 10 {
                self.send(0, &method(10, 51, &[]), 1)?;
            } else {
                self.send(1, &method(20, 41, &[]), 1)?;
            }
            return Err(fail(format!("{code} {text}"), true));
        }
        Ok((cls, mid, payload[4..].to_vec()))
    }

    fn expect(&mut self, _channel: u16, cls: u16, mid: u16) -> Result<Vec<u8>, AmqpError> {
        let (c, m, args) = self.method_frame()?;
        if (c, m) != (cls, mid) {
            return Err(fail(format!("the broker answers {c}.{m} where {cls}.{mid} was expected"), true));
        }
        Ok(args)
    }

    fn content(&mut self) -> Result<(Vec<(String, Field)>, Vec<u8>), AmqpError> {
        let (_, payload) = self.frame()?;
        let mut r = Reader { d: &payload, p: 0 };
        r.u16()?;
        r.u16()?;
        let size = r.u64()? as usize;
        let flags = r.u16()?;
        let mut headers = Vec::new();
        // the properties in their order; only the headers are kept
        for i in 0..14 {
            if flags & (1 << (15 - i)) == 0 {
                continue;
            }
            match i {
                2 => headers = r.table()?,
                3 | 4 => {
                    r.u8()?;
                }
                9 => {
                    r.u64()?;
                }
                _ => {
                    r.shortstr()?;
                }
            }
        }
        let mut body = Vec::new();
        while body.len() < size {
            let (_, chunk) = self.frame()?;
            body.extend(chunk);
        }
        Ok((headers, body))
    }

    pub fn declare(&mut self, queue: &str) -> Result<(), AmqpError> {
        if self.declared.iter().any(|q| q == queue) {
            return Ok(());
        }
        let mut args = 0u16.to_be_bytes().to_vec();
        args.extend(shortstr(queue));
        args.push(0x02);
        args.extend(table(&[]));
        self.send(1, &method(50, 10, &args), 1)?;
        self.expect(1, 50, 11)?;
        self.declared.push(queue.to_string());
        Ok(())
    }

    pub fn publish(&mut self, queue: &str, body: &[u8], headers: &[(&str, Field)]) -> Result<(), AmqpError> {
        self.declare(queue)?;
        let mut args = 0u16.to_be_bytes().to_vec();
        args.extend(shortstr(""));
        args.extend(shortstr(queue));
        args.push(0);
        self.send(1, &method(60, 40, &args), 1)?;
        let mut head = 60u16.to_be_bytes().to_vec();
        head.extend_from_slice(&0u16.to_be_bytes());
        head.extend_from_slice(&(body.len() as u64).to_be_bytes());
        head.extend_from_slice(&((1u16 << 13) | (1u16 << 12)).to_be_bytes());
        head.extend(table(headers));
        head.push(2);
        self.send(1, &head, 2)?;
        let step = (self.frame_max - 8) as usize;
        for chunk in body.chunks(step.max(1)) {
            self.send(1, chunk, 3)?;
        }
        let (cls, mid, _) = self.method_frame()?;
        if (cls, mid) == (60, 120) {
            return Err(fail("the broker did not keep the message", false));
        }
        if (cls, mid) != (60, 80) {
            return Err(fail("the broker does not confirm the message", true));
        }
        Ok(())
    }

    pub fn get(&mut self, queue: &str) -> Result<Option<Got>, AmqpError> {
        self.declare(queue)?;
        let mut args = 0u16.to_be_bytes().to_vec();
        args.extend(shortstr(queue));
        args.push(0);
        self.send(1, &method(60, 70, &args), 1)?;
        let (cls, mid, args) = self.method_frame()?;
        if (cls, mid) == (60, 72) {
            return Ok(None);
        }
        if (cls, mid) != (60, 71) {
            return Err(fail("the broker answers Basic.Get out of order", true));
        }
        let tag = Reader { d: &args, p: 0 }.u64()?;
        let (headers, body) = self.content()?;
        Ok(Some((tag, headers, body)))
    }

    pub fn ack(&mut self, tag: u64) -> Result<(), AmqpError> {
        let mut args = tag.to_be_bytes().to_vec();
        args.push(0);
        self.send(1, &method(60, 80, &args), 1)
    }
}
