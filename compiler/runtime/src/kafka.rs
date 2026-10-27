//! A Kafka client for nexo.stream (D169), as phase 0's kafka.py: the
//! binary protocol over a socket, TLS, SASL (PLAIN, or OAUTHBEARER for
//! Azure Event Hubs), record batches (version 2, uncompressed) and
//! Metadata v4, Produce v3, Fetch v4, ListOffsets v1, FindCoordinator v1,
//! OffsetFetch v1 and OffsetCommit v2.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

/// A broker's error code, or a failure to reach it (code None).
#[derive(Debug, Clone)]
pub struct KafkaError {
    pub reason: String,
    pub code: Option<i16>,
}

fn lost(reason: impl Into<String>) -> KafkaError {
    KafkaError { reason: reason.into(), code: None }
}

fn coded(code: i16) -> KafkaError {
    KafkaError { reason: error_text(code), code: Some(code) }
}

pub fn error_text(code: i16) -> String {
    match code {
        1 => "the offset is out of range",
        2 => "a record is corrupt",
        3 => "the topic or partition does not exist",
        5 => "the partition has no leader",
        6 => "the broker is not the leader of the partition",
        7 => "the request timed out",
        10 => "the message is too large",
        14 => "the coordinator is loading",
        15 => "the coordinator is not available",
        16 => "the broker is not the coordinator",
        25 => "the consumer group has members",
        29 => "the topic is not allowed",
        30 => "the group is not allowed",
        31 => "the cluster is not allowed",
        33 => "the SASL mechanism is not supported",
        58 => "the SASL authentication failed",
        _ => return format!("error {code}"),
    }
    .to_string()
}

// ------------------------------------------------------------ encoding

#[derive(Default)]
struct Out(Vec<u8>);

impl Out {
    fn i8(mut self, n: i8) -> Self {
        self.0.push(n as u8);
        self
    }
    fn i16(mut self, n: i16) -> Self {
        self.0.extend_from_slice(&n.to_be_bytes());
        self
    }
    fn i32(mut self, n: i32) -> Self {
        self.0.extend_from_slice(&n.to_be_bytes());
        self
    }
    fn i64(mut self, n: i64) -> Self {
        self.0.extend_from_slice(&n.to_be_bytes());
        self
    }
    fn string(self, s: Option<&str>) -> Self {
        match s {
            None => self.i16(-1),
            Some(s) => {
                let mut o = self.i16(s.len() as i16);
                o.0.extend_from_slice(s.as_bytes());
                o
            }
        }
    }
    fn blob(self, b: &[u8]) -> Self {
        let mut o = self.i32(b.len() as i32);
        o.0.extend_from_slice(b);
        o
    }
    fn raw(mut self, b: &[u8]) -> Self {
        self.0.extend_from_slice(b);
        self
    }
}

fn varint(n: i64, out: &mut Vec<u8>) {
    let mut z = ((n << 1) ^ (n >> 63)) as u64;
    loop {
        let b = (z & 0x7F) as u8;
        z >>= 7;
        if z != 0 {
            out.push(b | 0x80);
        } else {
            out.push(b);
            return;
        }
    }
}

struct Reader<'a> {
    d: &'a [u8],
    at: usize,
}

impl<'a> Reader<'a> {
    fn new(d: &'a [u8]) -> Self {
        Reader { d, at: 0 }
    }
    fn take(&mut self, n: usize) -> Result<&'a [u8], KafkaError> {
        if self.at + n > self.d.len() {
            return Err(lost("the answer of the broker is cut short"));
        }
        let out = &self.d[self.at..self.at + n];
        self.at += n;
        Ok(out)
    }
    fn i8(&mut self) -> Result<i8, KafkaError> {
        Ok(self.take(1)?[0] as i8)
    }
    fn i16(&mut self) -> Result<i16, KafkaError> {
        let b = self.take(2)?;
        Ok(i16::from_be_bytes([b[0], b[1]]))
    }
    fn i32(&mut self) -> Result<i32, KafkaError> {
        let b = self.take(4)?;
        Ok(i32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    }
    fn u32(&mut self) -> Result<u32, KafkaError> {
        Ok(self.i32()? as u32)
    }
    fn i64(&mut self) -> Result<i64, KafkaError> {
        let b = self.take(8)?;
        Ok(i64::from_be_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]]))
    }
    fn string(&mut self) -> Result<Option<String>, KafkaError> {
        let n = self.i16()?;
        if n < 0 {
            return Ok(None);
        }
        Ok(Some(String::from_utf8_lossy(self.take(n as usize)?).into_owned()))
    }
    fn blob(&mut self) -> Result<Option<&'a [u8]>, KafkaError> {
        let n = self.i32()?;
        if n < 0 {
            return Ok(None);
        }
        Ok(Some(self.take(n as usize)?))
    }
    fn varint(&mut self) -> Result<i64, KafkaError> {
        let (mut z, mut shift) = (0u64, 0);
        loop {
            let b = self.take(1)?[0];
            z |= ((b & 0x7F) as u64) << shift;
            if b & 0x80 == 0 {
                break;
            }
            shift += 7;
        }
        Ok(((z >> 1) as i64) ^ -((z & 1) as i64))
    }
    fn left(&self) -> usize {
        self.d.len() - self.at
    }
}

fn crc_table() -> [u32; 256] {
    let mut t = [0u32; 256];
    for (n, slot) in t.iter_mut().enumerate() {
        let mut c = n as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 { (c >> 1) ^ 0x82F6_3B78 } else { c >> 1 };
        }
        *slot = c;
    }
    t
}

pub fn crc32c(data: &[u8]) -> u32 {
    static TABLE: std::sync::OnceLock<[u32; 256]> = std::sync::OnceLock::new();
    let t = TABLE.get_or_init(crc_table);
    let mut c = 0xFFFF_FFFFu32;
    for b in data {
        c = t[((c ^ *b as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    c ^ 0xFFFF_FFFF
}

/// A record (key, value, timestamp ms).
pub type Record = (Option<Vec<u8>>, Vec<u8>, i64);

/// A RecordBatch (magic 2) of records.
pub fn record_batch(records: &[Record]) -> Vec<u8> {
    let base = records[0].2;
    let mut body = Vec::new();
    for (i, (key, value, ts)) in records.iter().enumerate() {
        let mut r = vec![0u8];
        varint(ts - base, &mut r);
        varint(i as i64, &mut r);
        match key {
            None => varint(-1, &mut r),
            Some(k) => {
                varint(k.len() as i64, &mut r);
                r.extend_from_slice(k);
            }
        }
        varint(value.len() as i64, &mut r);
        r.extend_from_slice(value);
        varint(0, &mut r);
        varint(r.len() as i64, &mut body);
        body.extend_from_slice(&r);
    }
    let max_ts = records.iter().map(|r| r.2).max().unwrap_or(base);
    let after = Out::default()
        .i16(0)
        .i32(records.len() as i32 - 1)
        .i64(base)
        .i64(max_ts)
        .i64(-1)
        .i16(-1)
        .i32(-1)
        .i32(records.len() as i32)
        .raw(&body)
        .0;
    let batch = Out::default().i32(0).i8(2).raw(&crc32c(&after).to_be_bytes()).raw(&after).0;
    Out::default().i64(0).i32(batch.len() as i32).raw(&batch).0
}

/// A fetched record: (offset, timestamp ms, key, value).
pub type Fetched = (i64, i64, Option<Vec<u8>>, Vec<u8>);

/// The records of the batches of a fetched partition; a batch cut at the
/// end is left for later.
pub fn read_batches(data: &[u8]) -> Result<Vec<Fetched>, KafkaError> {
    let mut out = Vec::new();
    let mut r = Reader::new(data);
    while r.left() >= 12 {
        let base_offset = r.i64()?;
        let size = r.i32()? as usize;
        if r.left() < size {
            break;
        }
        let mut b = Reader::new(r.take(size)?);
        b.i32()?;
        if b.i8()? != 2 {
            return Err(lost("a record batch is of an old format"));
        }
        let crc = b.u32()?;
        if crc32c(&b.d[b.at..]) != crc {
            return Err(lost("a record batch is corrupt"));
        }
        let attributes = b.i16()?;
        b.i32()?;
        let base_ts = b.i64()?;
        b.i64()?;
        b.i64()?;
        b.i16()?;
        b.i32()?;
        let count = b.i32()?;
        if attributes & 0x20 != 0 {
            continue;
        }
        if attributes & 0x07 != 0 {
            let codec = match attributes & 0x07 {
                1 => "gzip",
                2 => "snappy",
                3 => "lz4",
                4 => "zstd",
                _ => "?",
            };
            return Err(lost(format!("a record batch is compressed with {codec}, which is not read")));
        }
        for _ in 0..count {
            b.varint()?;
            b.i8()?;
            let ts = base_ts + b.varint()?;
            let offset = base_offset + b.varint()?;
            let n = b.varint()?;
            let key = if n < 0 { None } else { Some(b.take(n as usize)?.to_vec()) };
            let n = b.varint()?;
            let value = if n < 0 { Vec::new() } else { b.take(n as usize)?.to_vec() };
            for _ in 0..b.varint()? {
                let k = b.varint()?;
                b.take(k.max(0) as usize)?;
                let v = b.varint()?;
                if v > 0 {
                    b.take(v as usize)?;
                }
            }
            out.push((offset, ts, key, value));
        }
    }
    Ok(out)
}

// --------------------------------------------------------- connections

pub fn local_host(host: &str) -> bool {
    host == "localhost" || host == "::1" || host.starts_with("127.")
}

/// A socket, with TLS or without.
pub enum Stream {
    Plain(TcpStream),
    Tls(Box<rustls::StreamOwned<rustls::ClientConnection, TcpStream>>),
}

impl Stream {
    pub fn write_all(&mut self, data: &[u8]) -> std::io::Result<()> {
        match self {
            Stream::Plain(s) => s.write_all(data),
            Stream::Tls(s) => s.write_all(data).and_then(|_| s.flush()),
        }
    }
    pub fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        match self {
            Stream::Plain(s) => s.read(buf),
            Stream::Tls(s) => s.read(buf),
        }
    }
    pub fn set_timeout(&self, t: Option<Duration>) {
        let sock = match self {
            Stream::Plain(s) => s,
            Stream::Tls(s) => &s.sock,
        };
        let _ = sock.set_read_timeout(t);
    }
}

/// A socket to host:port, with TLS when `tls`: the words of a failure are
/// those of phase 0 (pg.rs).
pub fn open(host: &str, port: u16, tls: bool) -> Result<Stream, String> {
    let addrs: Vec<std::net::SocketAddr> = std::net::ToSocketAddrs::to_socket_addrs(&(host, port))
        .map_err(|e| format!("cannot reach {host}:{port}: {}", crate::pg::os_text(&e)))?
        .collect();
    let mut last = String::from("no address");
    let mut sock = None;
    for a in addrs {
        match TcpStream::connect_timeout(&a, Duration::from_secs(30)) {
            Ok(s) => {
                sock = Some(s);
                break;
            }
            Err(e) => last = crate::pg::os_text(&e),
        }
    }
    let Some(sock) = sock else { return Err(format!("cannot reach {host}:{port}: {last}")) };
    let _ = sock.set_read_timeout(Some(Duration::from_secs(30)));
    if !tls {
        return Ok(Stream::Plain(sock));
    }
    let name = rustls::pki_types::ServerName::try_from(host.to_string()).map_err(|_| format!("TLS with {host}: the TLS handshake failed"))?;
    let conn = rustls::ClientConnection::new(crate::pg::tls_config(), name).map_err(|_| format!("TLS with {host}: the TLS handshake failed"))?;
    let mut s = rustls::StreamOwned::new(conn, sock);
    while s.conn.is_handshaking() {
        s.conn.complete_io(&mut s.sock).map_err(|e| crate::pg::tls_text(host, &e).message)?;
    }
    Ok(Stream::Tls(Box::new(s)))
}

/// One connection to a broker; requests one at a time.
pub struct Conn {
    stream: Stream,
    correlation: i32,
}

impl Conn {
    fn new(host: &str, port: u16, sasl: &Option<(String, Vec<u8>)>) -> Result<Conn, KafkaError> {
        // a broker on another machine is reached with TLS only (D169)
        let stream = open(host, port, !local_host(host)).map_err(lost)?;
        let mut c = Conn { stream, correlation: 0 };
        if let Some((mechanism, payload)) = sasl {
            let body = c.request(17, 1, &Out::default().string(Some(mechanism)).0)?;
            let code = Reader::new(&body).i16()?;
            if code != 0 {
                return Err(coded(code));
            }
            let body = c.request(36, 0, &Out::default().blob(payload).0)?;
            let mut r = Reader::new(&body);
            let code = r.i16()?;
            let message = r.string()?;
            if code != 0 {
                return Err(KafkaError { reason: message.unwrap_or_else(|| error_text(code)), code: Some(code) });
            }
        }
        Ok(c)
    }

    fn read(&mut self, n: usize) -> Result<Vec<u8>, KafkaError> {
        let mut out = vec![0u8; n];
        let mut got = 0;
        while got < n {
            match self.stream.read(&mut out[got..]) {
                Ok(0) => return Err(lost("the broker closed the connection")),
                Ok(k) => got += k,
                Err(e) => return Err(lost(format!("connection lost: {}", crate::pg::os_text(&e)))),
            }
        }
        Ok(out)
    }

    fn request(&mut self, api: i16, version: i16, body: &[u8]) -> Result<Vec<u8>, KafkaError> {
        self.correlation += 1;
        let message = Out::default().i16(api).i16(version).i32(self.correlation).string(Some("nexo")).raw(body).0;
        let framed = Out::default().i32(message.len() as i32).raw(&message).0;
        self.stream.write_all(&framed).map_err(|e| lost(format!("connection lost: {}", crate::pg::os_text(&e))))?;
        let size = self.read(4)?;
        let size = i32::from_be_bytes([size[0], size[1], size[2], size[3]]) as usize;
        let data = self.read(size)?;
        if data.len() < 4 || i32::from_be_bytes([data[0], data[1], data[2], data[3]]) != self.correlation {
            return Err(lost("the broker answers another request"));
        }
        Ok(data[4..].to_vec())
    }
}

/// The brokers of a cluster, found from its bootstrap servers.
pub struct Client {
    servers: Vec<(String, u16)>,
    sasl: Option<(String, Vec<u8>)>,
    conns: HashMap<i32, Conn>,
    brokers: HashMap<i32, (String, u16)>,
    leaders: HashMap<(String, i32), i32>,
    pub counts: HashMap<String, i64>,
    bootstrap: Option<Conn>,
    /// called with each host before a connection to it: the program's
    /// policy (D142), for the bootstrap servers and the brokers found;
    /// false stops the request
    pub check: Option<Box<dyn Fn(&str) -> bool + Send>>,
}

impl Client {
    pub fn new(servers: Vec<(String, u16)>, sasl: Option<(String, Vec<u8>)>) -> Client {
        Client { servers, sasl, conns: HashMap::new(), brokers: HashMap::new(), leaders: HashMap::new(), counts: HashMap::new(), bootstrap: None, check: None }
    }

    fn allow(&self, host: &str) -> Result<(), KafkaError> {
        match &self.check {
            Some(check) if !check(host) => Err(KafkaError { reason: format!("the policy does not allow {host}"), code: Some(-1) }),
            _ => Ok(()),
        }
    }

    fn any(&mut self) -> Result<&mut Conn, KafkaError> {
        if self.bootstrap.is_none() {
            let mut last = lost("no server");
            for (host, port) in self.servers.clone() {
                self.allow(&host)?;
                match Conn::new(&host, port, &self.sasl) {
                    Ok(c) => {
                        self.bootstrap = Some(c);
                        break;
                    }
                    Err(e) if e.code.is_some() => return Err(e),
                    Err(e) => last = e,
                }
            }
            if self.bootstrap.is_none() {
                return Err(last);
            }
        }
        Ok(self.bootstrap.as_mut().unwrap())
    }

    fn node(&mut self, n: i32) -> Result<&mut Conn, KafkaError> {
        if !self.conns.contains_key(&n) {
            let (host, port) = self.brokers.get(&n).cloned().ok_or_else(|| lost("the broker is not known"))?;
            self.allow(&host)?;
            let c = Conn::new(&host, port, &self.sasl)?;
            self.conns.insert(n, c);
        }
        Ok(self.conns.get_mut(&n).unwrap())
    }

    /// The partitions of a topic; the leader of each is kept.
    pub fn metadata(&mut self, topic: &str) -> Result<i64, KafkaError> {
        let body = Out::default().i32(1).string(Some(topic)).i8(0).0;
        let data = self.any()?.request(3, 4, &body)?;
        let mut r = Reader::new(&data);
        r.i32()?;
        for _ in 0..r.i32()? {
            let (node, host, port) = (r.i32()?, r.string()?.unwrap_or_default(), r.i32()?);
            r.string()?;
            self.brokers.insert(node, (host, port as u16));
        }
        r.string()?;
        r.i32()?;
        for _ in 0..r.i32()? {
            let (code, name) = (r.i16()?, r.string()?.unwrap_or_default());
            r.i8()?;
            let mut parts = Vec::new();
            for _ in 0..r.i32()? {
                r.i16()?;
                let (index, leader) = (r.i32()?, r.i32()?);
                for _ in 0..2 {
                    for _ in 0..r.i32()?.max(0) {
                        r.i32()?;
                    }
                }
                parts.push((index, leader));
            }
            if name == topic {
                if code != 0 {
                    return Err(coded(code));
                }
                for (index, leader) in &parts {
                    self.leaders.insert((topic.to_string(), *index), *leader);
                }
                self.counts.insert(topic.to_string(), parts.len() as i64);
            }
        }
        Ok(*self.counts.get(topic).unwrap_or(&0))
    }

    fn leader(&mut self, topic: &str, partition: i32) -> Result<&mut Conn, KafkaError> {
        if !self.leaders.contains_key(&(topic.to_string(), partition)) {
            self.metadata(topic)?;
        }
        let n = *self.leaders.get(&(topic.to_string(), partition)).ok_or_else(|| coded(3))?;
        self.node(n)
    }

    pub fn produce(&mut self, topic: &str, partition: i32, records: &[Record]) -> Result<i64, KafkaError> {
        let body = Out::default()
            .string(None)
            .i16(-1)
            .i32(30000)
            .i32(1)
            .string(Some(topic))
            .i32(1)
            .i32(partition)
            .blob(&record_batch(records))
            .0;
        let data = self.leader(topic, partition)?.request(0, 3, &body)?;
        let mut r = Reader::new(&data);
        let mut offset = -1;
        for _ in 0..r.i32()? {
            r.string()?;
            for _ in 0..r.i32()? {
                r.i32()?;
                let code = r.i16()?;
                offset = r.i64()?;
                r.i64()?;
                if code != 0 {
                    self.leaders.remove(&(topic.to_string(), partition));
                    return Err(coded(code));
                }
            }
        }
        Ok(offset)
    }

    pub fn fetch(&mut self, topic: &str, partition: i32, offset: i64, wait_ms: i32) -> Result<Vec<Fetched>, KafkaError> {
        let body = Out::default()
            .i32(-1)
            .i32(wait_ms)
            .i32(1)
            .i32(1 << 20)
            .i8(0)
            .i32(1)
            .string(Some(topic))
            .i32(1)
            .i32(partition)
            .i64(offset)
            .i32(1 << 20)
            .0;
        let data = self.leader(topic, partition)?.request(1, 4, &body)?;
        let mut r = Reader::new(&data);
        r.i32()?;
        let mut out = Vec::new();
        for _ in 0..r.i32()? {
            r.string()?;
            for _ in 0..r.i32()? {
                r.i32()?;
                let code = r.i16()?;
                r.i64()?;
                r.i64()?;
                let aborted = r.i32()?;
                for _ in 0..aborted.max(0) {
                    r.i64()?;
                    r.i64()?;
                }
                let records = r.blob()?;
                if code != 0 {
                    self.leaders.remove(&(topic.to_string(), partition));
                    return Err(coded(code));
                }
                out.extend(read_batches(records.unwrap_or(&[]))?.into_iter().filter(|x| x.0 >= offset));
            }
        }
        Ok(out)
    }

    pub fn earliest(&mut self, topic: &str, partition: i32) -> Result<i64, KafkaError> {
        let body = Out::default().i32(-1).i32(1).string(Some(topic)).i32(1).i32(partition).i64(-2).0;
        let data = self.leader(topic, partition)?.request(2, 1, &body)?;
        let mut r = Reader::new(&data);
        for _ in 0..r.i32()? {
            r.string()?;
            for _ in 0..r.i32()? {
                r.i32()?;
                let code = r.i16()?;
                r.i64()?;
                let offset = r.i64()?;
                if code != 0 {
                    return Err(coded(code));
                }
                return Ok(offset);
            }
        }
        Ok(0)
    }

    fn coordinator(&mut self, group: &str) -> Result<&mut Conn, KafkaError> {
        let data = self.any()?.request(10, 1, &Out::default().string(Some(group)).i8(0).0)?;
        let mut r = Reader::new(&data);
        r.i32()?;
        let code = r.i16()?;
        r.string()?;
        let (node, host, port) = (r.i32()?, r.string()?.unwrap_or_default(), r.i32()?);
        if code != 0 {
            return Err(coded(code));
        }
        self.brokers.insert(node, (host, port as u16));
        self.node(node)
    }

    pub fn committed(&mut self, group: &str, topic: &str, partition: i32) -> Result<i64, KafkaError> {
        let body = Out::default().string(Some(group)).i32(1).string(Some(topic)).i32(1).i32(partition).0;
        let data = self.coordinator(group)?.request(9, 1, &body)?;
        let mut r = Reader::new(&data);
        let mut out = -1;
        for _ in 0..r.i32()? {
            r.string()?;
            for _ in 0..r.i32()? {
                let (p, offset) = (r.i32()?, r.i64()?);
                r.string()?;
                let code = r.i16()?;
                if code != 0 {
                    return Err(coded(code));
                }
                if p == partition {
                    out = offset;
                }
            }
        }
        Ok(out)
    }

    pub fn commit(&mut self, group: &str, topic: &str, partition: i32, offset: i64) -> Result<(), KafkaError> {
        let body = Out::default()
            .string(Some(group))
            .i32(-1)
            .string(Some(""))
            .i64(-1)
            .i32(1)
            .string(Some(topic))
            .i32(1)
            .i32(partition)
            .i64(offset)
            .string(None)
            .0;
        let data = self.coordinator(group)?.request(8, 2, &body)?;
        let mut r = Reader::new(&data);
        for _ in 0..r.i32()? {
            r.string()?;
            for _ in 0..r.i32()? {
                r.i32()?;
                let code = r.i16()?;
                if code != 0 {
                    return Err(coded(code));
                }
            }
        }
        Ok(())
    }
}
