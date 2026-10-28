"""A Kafka client for nexo.stream (D169): the binary protocol over a
socket, TLS, SASL (PLAIN, or OAUTHBEARER for Azure Event Hubs), record
batches (version 2, uncompressed) and the requests a producer and a
consumer group need: Metadata v4, Produce v3, Fetch v4, ListOffsets v1,
FindCoordinator v1, OffsetFetch v1 and OffsetCommit v2, which every broker
from Kafka 1.0 to 4.x answers."""
from __future__ import annotations

import socket
import ssl
import struct
import threading


class KafkaError(Exception):
    """A broker's error code, or a failure to reach it (code None)."""

    def __init__(self, reason: str, code: int | None = None):
        super().__init__(reason)
        self.reason, self.code = reason, code


ERRORS = {
    1: "the offset is out of range", 2: "a record is corrupt", 3: "the topic or partition does not exist",
    5: "the partition has no leader", 6: "the broker is not the leader of the partition",
    7: "the request timed out", 10: "the message is too large", 14: "the coordinator is loading",
    15: "the coordinator is not available", 16: "the broker is not the coordinator",
    25: "the consumer group has members", 29: "the topic is not allowed", 30: "the group is not allowed",
    31: "the cluster is not allowed", 33: "the SASL mechanism is not supported",
    58: "the SASL authentication failed",
}


def error_text(code: int) -> str:
    return ERRORS.get(code, f"error {code}")


# ------------------------------------------------------------ encoding

def i8(n): return struct.pack(">b", n)
def i16(n): return struct.pack(">h", n)
def i32(n): return struct.pack(">i", n)
def i64(n): return struct.pack(">q", n)
def string(s): return i16(-1) if s is None else i16(len(s.encode())) + s.encode()
def blob(b): return i32(-1) if b is None else i32(len(b)) + b
def array(items): return i32(len(items)) + b"".join(items)


def varint(n: int) -> bytes:
    z = (n << 1) ^ (n >> 63)
    z &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        b = z & 0x7F
        z >>= 7
        if z:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class Reader:
    def __init__(self, data: bytes, at: int = 0):
        self.data, self.at = data, at

    def take(self, n: int) -> bytes:
        if self.at + n > len(self.data):
            raise KafkaError("the answer of the broker is cut short")
        out = self.data[self.at:self.at + n]
        self.at += n
        return out

    def i8(self): return struct.unpack(">b", self.take(1))[0]
    def i16(self): return struct.unpack(">h", self.take(2))[0]
    def i32(self): return struct.unpack(">i", self.take(4))[0]
    def u32(self): return struct.unpack(">I", self.take(4))[0]
    def i64(self): return struct.unpack(">q", self.take(8))[0]

    def string(self):
        n = self.i16()
        return None if n < 0 else self.take(n).decode("utf-8", "replace")

    def blob(self):
        n = self.i32()
        return None if n < 0 else self.take(n)

    def array(self, item):
        n = self.i32()
        return [] if n < 0 else [item() for _ in range(n)]

    def varint(self) -> int:
        shift = z = 0
        while True:
            b = self.take(1)[0]
            z |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        return (z >> 1) ^ -(z & 1)

    def done(self) -> bool:
        return self.at >= len(self.data)


def _crc_table():
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


CRC_TABLE = _crc_table()


def crc32c(data: bytes) -> int:
    c = 0xFFFFFFFF
    for b in data:
        c = CRC_TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def record_batch(records: list) -> bytes:
    """A RecordBatch (magic 2) of [(key or None, value, timestamp ms)]."""
    base = records[0][2]
    body = bytearray()
    for i, (key, value, ts) in enumerate(records):
        r = i8(0) + varint(ts - base) + varint(i)
        r += varint(-1) if key is None else varint(len(key)) + key
        r += varint(len(value)) + value + varint(0)
        body += varint(len(r)) + r
    after_crc = (i16(0) + i32(len(records) - 1) + i64(base) + i64(max(r[2] for r in records))
                 + i64(-1) + i16(-1) + i32(-1) + i32(len(records)) + bytes(body))
    batch = i32(0) + i8(2) + struct.pack(">I", crc32c(after_crc)) + after_crc
    return i64(0) + i32(len(batch)) + batch


def read_batches(data: bytes) -> list:
    """[(offset, timestamp ms, key or None, value)] of the record batches
    in a fetched partition; a batch cut at the end is left for later."""
    out = []
    r = Reader(data)
    while len(data) - r.at >= 12:
        base_offset, size = r.i64(), r.i32()
        if len(data) - r.at < size:
            break
        b = Reader(r.take(size))
        b.i32()  # partition leader epoch
        if b.i8() != 2:
            raise KafkaError("a record batch is of an old format")
        crc = b.u32()
        if crc32c(b.data[b.at:]) != crc:
            raise KafkaError("a record batch is corrupt")
        attributes = b.i16()
        b.i32()
        base_ts = b.i64()
        b.i64()
        b.i64(), b.i16(), b.i32()
        count = b.i32()
        if attributes & 0x20:  # a control batch of a transaction
            continue
        if attributes & 0x07:
            codec = {1: "gzip", 2: "snappy", 3: "lz4", 4: "zstd"}.get(attributes & 0x07, "?")
            raise KafkaError(f"a record batch is compressed with {codec}, which is not read")
        for _ in range(count):
            b.varint()
            b.i8()
            ts = base_ts + b.varint()
            offset = base_offset + b.varint()
            n = b.varint()
            key = None if n < 0 else b.take(n)
            n = b.varint()
            value = b"" if n < 0 else b.take(n)
            for _ in range(b.varint()):
                b.take(b.varint())
                n = b.varint()
                if n > 0:
                    b.take(n)
            out.append((offset, ts, key, value))
    return out


# --------------------------------------------------------- connections

def local_host(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


class Conn:
    """One connection to a broker; requests one at a time."""

    def __init__(self, host: str, port: int, sasl=None, timeout: float = 30.0):
        self.host, self.lock, self.correlation = host, threading.Lock(), 0
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise KafkaError(f"cannot reach {host}:{port}: {exc.strerror or 'timed out'}") from None
        if not local_host(host):
            # a broker on another machine is reached with TLS only (D169)
            try:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError:
                raise KafkaError(f"TLS with {host}: the certificate is not trusted") from None
            except (ssl.SSLError, OSError):
                raise KafkaError(f"TLS with {host}: the TLS handshake failed") from None
        self.sock = sock
        if sasl is not None:
            self.authenticate(*sasl)

    def authenticate(self, mechanism: str, payload: bytes) -> None:
        r = Reader(self.request(17, 1, string(mechanism)))
        code = r.i16()
        if code:
            raise KafkaError(error_text(code), code)
        r = Reader(self.request(36, 0, blob(payload)))
        code = r.i16()
        message = r.string()
        if code:
            raise KafkaError(message or error_text(code), code)

    def read(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            try:
                chunk = self.sock.recv(n - len(out))
            except OSError as exc:
                raise KafkaError(f"connection lost: {exc}") from None
            if not chunk:
                raise KafkaError("the broker closed the connection")
            out += chunk
        return out

    def request(self, api: int, version: int, body: bytes) -> bytes:
        with self.lock:
            self.correlation += 1
            head = i16(api) + i16(version) + i32(self.correlation) + string("nexo")
            message = head + body
            try:
                self.sock.sendall(i32(len(message)) + message)
            except OSError as exc:
                raise KafkaError(f"connection lost: {exc}") from None
            size = struct.unpack(">i", self.read(4))[0]
            data = self.read(size)
            if struct.unpack(">i", data[:4])[0] != self.correlation:
                raise KafkaError("the broker answers another request")
            return data[4:]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Client:
    """The brokers of a cluster, found from its bootstrap servers."""

    def __init__(self, servers: list, sasl=None):
        self.servers, self.sasl = servers, sasl
        self.conns: dict = {}  # node -> Conn
        self.brokers: dict = {}  # node -> (host, port)
        self.leaders: dict = {}  # (topic, partition) -> node
        self.counts: dict = {}  # topic -> partitions
        self.bootstrap = None
        # called with each host before a connection to it: the program's
        # policy (D142), for the bootstrap servers and the brokers found
        self.check = None

    def allow(self, host: str) -> None:
        if self.check is not None:
            self.check(host)

    def any(self) -> Conn:
        if self.bootstrap is None:
            last = None
            for host, port in self.servers:
                self.allow(host)
                try:
                    self.bootstrap = Conn(host, port, self.sasl)
                    break
                except KafkaError as exc:
                    if exc.code is not None:
                        raise
                    last = exc
            if self.bootstrap is None:
                raise last
        return self.bootstrap

    def node(self, n: int) -> Conn:
        if n not in self.conns:
            host, port = self.brokers[n]
            self.allow(host)
            self.conns[n] = Conn(host, port, self.sasl)
        return self.conns[n]

    def metadata(self, topic: str) -> int:
        """The partitions of a topic; the leader of each is kept."""
        r = Reader(self.any().request(3, 4, array([string(topic)]) + i8(0)))
        r.i32()
        for _ in range(r.i32()):
            node, host, port = r.i32(), r.string(), r.i32()
            r.string()
            self.brokers[node] = (host, port)
        r.string()
        r.i32()
        for _ in range(r.i32()):
            code, name = r.i16(), r.string()
            r.i8()
            parts = []
            for _ in range(r.i32()):
                pcode, index, leader = r.i16(), r.i32(), r.i32()
                r.array(r.i32)
                r.array(r.i32)
                parts.append((index, leader))
            if name == topic:
                if code:
                    raise KafkaError(error_text(code), code)
                for index, leader in parts:
                    self.leaders[(topic, index)] = leader
                self.counts[topic] = len(parts)
        return self.counts.get(topic, 0)

    def leader(self, topic: str, partition: int) -> Conn:
        if (topic, partition) not in self.leaders:
            self.metadata(topic)
        return self.node(self.leaders[(topic, partition)])

    def produce(self, topic: str, partition: int, records: list) -> int:
        body = (string(None) + i16(-1) + i32(30000)
                + array([string(topic) + array([i32(partition) + blob(record_batch(records))])]))
        r = Reader(self.leader(topic, partition).request(0, 3, body))
        offset = -1
        for _ in range(r.i32()):
            r.string()
            for _ in range(r.i32()):
                r.i32()
                code, offset = r.i16(), r.i64()
                r.i64()
                if code:
                    self.leaders.pop((topic, partition), None)
                    raise KafkaError(error_text(code), code)
        return offset

    def fetch(self, topic: str, partition: int, offset: int, wait_ms: int = 500) -> list:
        body = (i32(-1) + i32(wait_ms) + i32(1) + i32(1 << 20) + i8(0)
                + array([string(topic) + array([i32(partition) + i64(offset) + i32(1 << 20)])]))
        r = Reader(self.leader(topic, partition).request(1, 4, body))
        r.i32()
        out = []
        for _ in range(r.i32()):
            r.string()
            for _ in range(r.i32()):
                r.i32()
                code = r.i16()
                r.i64(), r.i64()
                r.array(lambda: (r.i64(), r.i64()))
                records = r.blob()
                if code:
                    self.leaders.pop((topic, partition), None)
                    raise KafkaError(error_text(code), code)
                out += [x for x in read_batches(records or b"") if x[0] >= offset]
        return out

    def earliest(self, topic: str, partition: int) -> int:
        body = i32(-1) + array([string(topic) + array([i32(partition) + i64(-2)])])
        r = Reader(self.leader(topic, partition).request(2, 1, body))
        for _ in range(r.i32()):
            r.string()
            for _ in range(r.i32()):
                r.i32()
                code = r.i16()
                r.i64()
                offset = r.i64()
                if code:
                    raise KafkaError(error_text(code), code)
                return offset
        return 0

    def coordinator(self, group: str) -> Conn:
        r = Reader(self.any().request(10, 1, string(group) + i8(0)))
        r.i32()
        code = r.i16()
        r.string()
        node, host, port = r.i32(), r.string(), r.i32()
        if code:
            raise KafkaError(error_text(code), code)
        self.brokers[node] = (host, port)
        return self.node(node)

    def committed(self, group: str, topic: str, partitions: list) -> dict:
        body = string(group) + array([string(topic) + array([i32(p) for p in partitions])])
        r = Reader(self.coordinator(group).request(9, 1, body))
        out = {}
        for _ in range(r.i32()):
            r.string()
            for _ in range(r.i32()):
                p, offset = r.i32(), r.i64()
                r.string()
                code = r.i16()
                if code:
                    raise KafkaError(error_text(code), code)
                out[p] = offset
        return out

    def commit(self, group: str, topic: str, offsets: dict) -> None:
        body = (string(group) + i32(-1) + string("") + i64(-1)
                + array([string(topic) + array([i32(p) + i64(o) + string(None) for p, o in offsets.items()])]))
        r = Reader(self.coordinator(group).request(8, 2, body))
        for _ in range(r.i32()):
            r.string()
            for _ in range(r.i32()):
                r.i32()
                code = r.i16()
                if code:
                    raise KafkaError(error_text(code), code)
