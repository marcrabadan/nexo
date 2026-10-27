"""A one-node Kafka broker for the tests of nexo.stream (D169), written
from the protocol's specification on its own (it shares no code with
nexo/stdlib/kafka.py): Metadata v4, Produce v3, Fetch v4, ListOffsets v1,
FindCoordinator v1, OffsetFetch v1, OffsetCommit v2, SaslHandshake v1 and
SaslAuthenticate v0 (PLAIN with a user and password, OAUTHBEARER with a
token), record batches of magic 2 with their CRC-32C checked.

    broker = start({"orders": 3}, users={"shop": "pw"})
    broker.port, broker.topics["orders"][0]  # [(offset, ts, key, value)]
"""
from __future__ import annotations

import socket
import socketserver
import struct
import threading


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


class In:
    def __init__(self, data: bytes):
        self.d, self.p = data, 0

    def raw(self, n):
        out = self.d[self.p:self.p + n]
        if len(out) != n:
            raise ValueError("short request")
        self.p += n
        return out

    def fmt(self, f):
        return struct.unpack(">" + f, self.raw(struct.calcsize(">" + f)))[0]

    def str_(self):
        n = self.fmt("h")
        return None if n == -1 else self.raw(n).decode()

    def bytes_(self):
        n = self.fmt("i")
        return None if n == -1 else self.raw(n)

    def arr(self, read):
        return [read() for _ in range(self.fmt("i"))]

    def var(self):
        value = shift = 0
        while True:
            b = self.raw(1)[0]
            value |= (b & 0x7F) << shift
            shift += 7
            if b < 0x80:
                return (value >> 1) ^ -(value & 1)


class Out:
    def __init__(self):
        self.b = bytearray()

    def fmt(self, f, v):
        self.b += struct.pack(">" + f, v)
        return self

    def str_(self, s):
        if s is None:
            return self.fmt("h", -1)
        e = s.encode()
        self.fmt("h", len(e))
        self.b += e
        return self

    def bytes_(self, b):
        if b is None:
            return self.fmt("i", -1)
        self.fmt("i", len(b))
        self.b += b
        return self

    def var(self, v):
        z = ((v << 1) ^ (v >> 63)) & (2 ** 64 - 1)
        while z >= 0x80:
            self.b.append((z & 0x7F) | 0x80)
            z >>= 7
        self.b.append(z)
        return self


def parse_batch(data: bytes) -> list:
    """[(offset delta, timestamp, key, value)] of the batches a producer sent."""
    out = []
    r = In(data)
    while r.p < len(data):
        r.fmt("q")
        length = r.fmt("i")
        body = In(r.raw(length))
        body.fmt("i")
        assert body.fmt("b") == 2, "magic"
        crc = body.fmt("I")
        assert crc32c(body.d[body.p:]) == crc, "crc"
        assert body.fmt("h") == 0, "attributes"
        body.fmt("i")
        first_ts = body.fmt("q")
        body.fmt("q"), body.fmt("q"), body.fmt("h"), body.fmt("i")
        for _ in range(body.fmt("i")):
            size = body.var()
            rec = In(body.raw(size))
            rec.fmt("b")
            ts = first_ts + rec.var()
            delta = rec.var()
            n = rec.var()
            key = None if n < 0 else rec.raw(n)
            n = rec.var()
            value = rec.raw(n)
            assert rec.var() == 0, "headers"
            out.append((delta, ts, key, value))
    return out


def make_batch(base: int, records: list) -> bytes:
    """One batch of stored [(offset, ts, key, value)]."""
    recs = Out()
    for off, ts, key, value in records:
        one = Out().fmt("b", 0).var(ts - records[0][1]).var(off - base)
        if key is None:
            one.var(-1)
        else:
            one.var(len(key))
            one.b += key
        one.var(len(value))
        one.b += value
        one.var(0)
        recs.var(len(one.b))
        recs.b += one.b
    tail = (Out().fmt("h", 0).fmt("i", len(records) - 1).fmt("q", records[0][1]).fmt("q", records[-1][1])
            .fmt("q", -1).fmt("h", -1).fmt("i", -1).fmt("i", len(records)))
    tail.b += recs.b
    head = Out().fmt("i", 0).fmt("b", 2).fmt("I", crc32c(bytes(tail.b)))
    batch = bytes(head.b) + bytes(tail.b)
    return bytes(Out().fmt("q", base).fmt("i", len(batch)).b) + batch


class Broker:
    def __init__(self, topics: dict, users=None, tokens=None):
        self.topics = {t: [[] for _ in range(n)] for t, n in topics.items()}
        self.offsets: dict = {}  # (group, topic, partition) -> offset
        self.users, self.tokens = users, tokens
        self.lock = threading.Lock()
        self.calls: list = []  # (api, version)

    def answer(self, api: int, version: int, r: In, conn: dict) -> Out:
        o = Out()
        self.calls.append((api, version))
        if api == 17:  # SaslHandshake v1
            mech = r.str_()
            conn["mech"] = mech
            ok = mech in ("PLAIN", "OAUTHBEARER")
            return o.fmt("h", 0 if ok else 33).fmt("i", 2).str_("PLAIN").str_("OAUTHBEARER")
        if api == 36:  # SaslAuthenticate v0
            auth = r.bytes_()
            good = False
            if conn.get("mech") == "PLAIN" and self.users is not None:
                _, user, password = auth.split(b"\0")
                good = self.users.get(user.decode()) == password.decode()
            elif conn.get("mech") == "OAUTHBEARER" and self.tokens is not None:
                good = any(f"auth=Bearer {t}".encode() in auth for t in self.tokens)
            conn["authed"] = good
            if not good:
                return o.fmt("h", 58).str_("Authentication failed: Invalid username or password").bytes_(b"")
            return o.fmt("h", 0).str_(None).bytes_(b"")
        if (self.users is not None or self.tokens is not None) and not conn.get("authed"):
            raise ConnectionError("not authenticated")
        if api == 3:  # Metadata v4
            names = r.arr(r.str_)
            r.fmt("b")
            o.fmt("i", 0).fmt("i", 1).fmt("i", 0).str_("localhost").fmt("i", self.port).str_(None)
            o.str_("nexo-test").fmt("i", 0).fmt("i", len(names))
            for n in names:
                parts = self.topics.get(n)
                o.fmt("h", 0 if parts is not None else 3).str_(n).fmt("b", 0).fmt("i", len(parts or []))
                for i in range(len(parts or [])):
                    o.fmt("h", 0).fmt("i", i).fmt("i", 0).fmt("i", 1).fmt("i", 0).fmt("i", 1).fmt("i", 0)
            return o
        if api == 0:  # Produce v3
            r.str_(), r.fmt("h"), r.fmt("i")
            topics = r.arr(lambda: (r.str_(), r.arr(lambda: (r.fmt("i"), r.bytes_()))))
            o.fmt("i", len(topics))
            for name, parts in topics:
                o.str_(name).fmt("i", len(parts))
                for index, data in parts:
                    log = self.topics[name][index]
                    base = len(log)
                    for delta, ts, key, value in parse_batch(data):
                        log.append((base + delta, ts, key, value))
                    o.fmt("i", index).fmt("h", 0).fmt("q", base).fmt("q", -1)
            return o.fmt("i", 0)
        if api == 1:  # Fetch v4
            r.fmt("i"), r.fmt("i"), r.fmt("i"), r.fmt("i"), r.fmt("b")
            topics = r.arr(lambda: (r.str_(), r.arr(lambda: (r.fmt("i"), r.fmt("q"), r.fmt("i")))))
            o.fmt("i", 0).fmt("i", len(topics))
            for name, parts in topics:
                o.str_(name).fmt("i", len(parts))
                for index, offset, _ in parts:
                    log = self.topics[name][index]
                    o.fmt("i", index)
                    if offset > len(log):
                        o.fmt("h", 1).fmt("q", len(log)).fmt("q", len(log)).fmt("i", -1).bytes_(None)
                        continue
                    # two batches, the first from before the offset asked
                    data = b""
                    start = max(0, offset - 1)
                    rest = log[start:]
                    if rest:
                        data += make_batch(rest[0][0], rest[:2])
                        if len(rest) > 2:
                            data += make_batch(rest[2][0], rest[2:])
                    o.fmt("h", 0).fmt("q", len(log)).fmt("q", len(log)).fmt("i", -1).bytes_(data)
            return o
        if api == 2:  # ListOffsets v1
            r.fmt("i")
            topics = r.arr(lambda: (r.str_(), r.arr(lambda: (r.fmt("i"), r.fmt("q")))))
            o.fmt("i", len(topics))
            for name, parts in topics:
                o.str_(name).fmt("i", len(parts))
                for index, ts in parts:
                    log = self.topics[name][index]
                    o.fmt("i", index).fmt("h", 0).fmt("q", -1).fmt("q", 0 if ts == -2 else len(log))
            return o
        if api == 10:  # FindCoordinator v1
            r.str_(), r.fmt("b")
            return o.fmt("i", 0).fmt("h", 0).str_(None).fmt("i", 0).str_("localhost").fmt("i", self.port)
        if api == 9:  # OffsetFetch v1
            group = r.str_()
            topics = r.arr(lambda: (r.str_(), r.arr(lambda: r.fmt("i"))))
            o.fmt("i", len(topics))
            for name, parts in topics:
                o.str_(name).fmt("i", len(parts))
                for p in parts:
                    o.fmt("i", p).fmt("q", self.offsets.get((group, name, p), -1)).str_("").fmt("h", 0)
            return o
        if api == 8:  # OffsetCommit v2
            group = r.str_()
            assert r.fmt("i") == -1 and r.str_() == ""
            r.fmt("q")
            topics = r.arr(lambda: (r.str_(), r.arr(lambda: (r.fmt("i"), r.fmt("q"), r.str_()))))
            o.fmt("i", len(topics))
            for name, parts in topics:
                o.str_(name).fmt("i", len(parts))
                for p, offset, _ in parts:
                    self.offsets[(group, name, p)] = offset
                    o.fmt("i", p).fmt("h", 0)
            return o
        raise ConnectionError(f"api {api} v{version} is not answered")


def start(topics: dict, users=None, tokens=None) -> Broker:
    broker = Broker(topics, users, tokens)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            conn: dict = {}
            sock = self.request
            while True:
                head = recv(sock, 4)
                if head is None:
                    return
                data = recv(sock, struct.unpack(">i", head)[0])
                r = In(data)
                api, version, correlation = r.fmt("h"), r.fmt("h"), r.fmt("i")
                r.str_()
                try:
                    with broker.lock:
                        body = broker.answer(api, version, r, conn)
                except (ConnectionError, ValueError, AssertionError, KeyError):
                    return
                out = struct.pack(">i", correlation) + bytes(body.b)
                sock.sendall(struct.pack(">i", len(out)) + out)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    broker.port = server.server_address[1]
    broker.server = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return broker


def recv(sock: socket.socket, n: int):
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            return None
        out += chunk
    return out
