"""A RabbitMQ client for nexo.stream (D169): AMQP 0-9-1 over a socket (TLS
for amqps://), PLAIN sign-in, durable queues, publisher confirms, and
Basic.Get with acknowledgements for a consumer."""
from __future__ import annotations

import socket
import ssl
import struct
import threading
import urllib.parse

FRAME_END = 0xCE


class AmqpError(Exception):
    def __init__(self, reason: str, lost: bool = False):
        super().__init__(reason)
        self.reason, self.lost = reason, lost


# ------------------------------------------------------------ encoding

def shortstr(s: str) -> bytes:
    b = s.encode()
    return bytes([len(b)]) + b


def longstr(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def table(pairs: dict) -> bytes:
    body = b""
    for k, v in pairs.items():
        body += shortstr(k)
        if isinstance(v, bool):
            body += b"t" + bytes([1 if v else 0])
        elif isinstance(v, int):
            body += b"l" + struct.pack(">q", v)
        elif isinstance(v, dict):
            body += b"F" + table(v)
        else:
            body += b"S" + longstr(str(v).encode())
    return struct.pack(">I", len(body)) + body


class Reader:
    def __init__(self, data: bytes):
        self.d, self.p = data, 0

    def take(self, n: int) -> bytes:
        out = self.d[self.p:self.p + n]
        self.p += n
        return out

    def fmt(self, f: str):
        return struct.unpack(">" + f, self.take(struct.calcsize(">" + f)))[0]

    def shortstr(self) -> str:
        return self.take(self.fmt("B")).decode("utf-8", "replace")

    def longstr(self) -> bytes:
        return self.take(self.fmt("I"))

    def table(self) -> dict:
        end = self.fmt("I") + self.p
        out = {}
        while self.p < end:
            k = self.shortstr()
            out[k] = self.value()
        return out

    def value(self):
        t = chr(self.take(1)[0])
        sizes = {"b": "b", "B": "B", "s": "h", "u": "H", "I": "i", "i": "I", "l": "q", "f": "f", "d": "d", "T": "Q"}
        if t == "t":
            return self.take(1)[0] != 0
        if t in sizes:
            return self.fmt(sizes[t])
        if t == "D":
            self.take(5)
            return None
        if t == "S":
            return self.longstr().decode("utf-8", "replace")
        if t == "x":
            return self.longstr()
        if t == "F":
            return self.table()
        if t == "A":
            end = self.fmt("I") + self.p
            items = []
            while self.p < end:
                items.append(self.value())
            return items
        if t == "V":
            return None
        raise AmqpError(f"a field of type {t} is not read")


def method(cls: int, mid: int, args: bytes = b"") -> bytes:
    return struct.pack(">HH", cls, mid) + args


def local_host(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


# --------------------------------------------------------- connection

class Connection:
    """One connection with one channel, used by one thread at a time."""

    def __init__(self, url: str, password: str | None, timeout: float = 30.0):
        u = urllib.parse.urlsplit(url)
        host = u.hostname or "localhost"
        tls = u.scheme == "amqps"
        if not tls and not local_host(host):
            raise AmqpError(f"{host} is reached with amqps:// only")
        port = u.port or (5671 if tls else 5672)
        self.vhost = urllib.parse.unquote(u.path[1:]) if len(u.path) > 1 else "/"
        user = urllib.parse.unquote(u.username) if u.username else "guest"
        self.lock = threading.Lock()
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise AmqpError(f"cannot reach {host}:{port}: {exc.strerror or 'timed out'}", True) from None
        if tls:
            try:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError:
                raise AmqpError(f"TLS with {host}: the certificate is not trusted") from None
            except (ssl.SSLError, OSError):
                raise AmqpError(f"TLS with {host}: the TLS handshake failed") from None
        self.sock, self.buf, self.tag = sock, b"", 0
        self.sock.sendall(b"AMQP\x00\x00\x09\x01")
        self.expect(0, 10, 10)  # Connection.Start
        props = {"product": "nexo", "capabilities": {"publisher_confirms": True}}
        self.send(0, method(10, 11, table(props) + shortstr("PLAIN")
                            + longstr(f"\0{user}\0{password or ''}".encode()) + shortstr("en_US")))
        tune = self.expect(0, 10, 30)
        channel_max, frame_max, heartbeat = tune.fmt("H"), tune.fmt("I"), tune.fmt("H")
        self.frame_max = frame_max or 131072
        self.send(0, method(10, 31, struct.pack(">HIH", channel_max, self.frame_max, heartbeat)))
        self.send(0, method(10, 40, shortstr(self.vhost) + shortstr("") + b"\x00"))
        self.expect(0, 10, 41)
        self.send(1, method(20, 10, shortstr("")))
        self.expect(1, 20, 11)
        self.send(1, method(85, 10, b"\x00"))  # Confirm.Select
        self.expect(1, 85, 11)
        self.declared: set = set()

    # frames
    def send(self, channel: int, payload: bytes, kind: int = 1) -> None:
        try:
            self.sock.sendall(struct.pack(">BHI", kind, channel, len(payload)) + payload + bytes([FRAME_END]))
        except OSError as exc:
            raise AmqpError(f"connection lost: {exc}", True) from None

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except OSError as exc:
                raise AmqpError(f"connection lost: {exc}", True) from None
            if not chunk:
                raise AmqpError("the broker closed the connection", True)
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frame(self) -> tuple[int, int, bytes]:
        while True:
            kind, channel, size = struct.unpack(">BHI", self.read(7))
            payload = self.read(size)
            if self.read(1)[0] != FRAME_END:
                raise AmqpError("a frame of the broker is not valid", True)
            if kind == 8:  # heartbeat
                self.send(0, b"", 8)
                continue
            return kind, channel, payload

    def method_frame(self) -> tuple[int, int, int, Reader]:
        kind, channel, payload = self.frame()
        if kind != 1:
            raise AmqpError("the broker sends content out of order", True)
        r = Reader(payload)
        cls, mid = r.fmt("H"), r.fmt("H")
        if (cls, mid) == (10, 50):  # Connection.Close
            code, text = r.fmt("H"), r.shortstr()
            self.send(0, method(10, 51))
            raise AmqpError(f"{code} {text}", True)
        if (cls, mid) == (20, 40):  # Channel.Close
            code, text = r.fmt("H"), r.shortstr()
            self.send(1, method(20, 41))
            # a closed channel: the connection is made again
            raise AmqpError(f"{code} {text}", True)
        return channel, cls, mid, r

    def expect(self, channel: int, cls: int, mid: int) -> Reader:
        _, c, m, r = self.method_frame()
        if (c, m) != (cls, mid):
            raise AmqpError(f"the broker answers {c}.{m} where {cls}.{mid} was expected", True)
        return r

    def content(self) -> tuple[dict, bytes]:
        _, _, payload = self.frame()
        r = Reader(payload)
        r.fmt("H"), r.fmt("H")
        size, flags = r.fmt("Q"), r.fmt("H")
        props = {}
        names = [("content_type", "s"), ("content_encoding", "s"), ("headers", "F"), ("delivery_mode", "B"),
                 ("priority", "B"), ("correlation_id", "s"), ("reply_to", "s"), ("expiration", "s"),
                 ("message_id", "s"), ("timestamp", "Q"), ("type", "s"), ("user_id", "s"), ("app_id", "s"),
                 ("cluster_id", "s")]
        for i, (name, kind) in enumerate(names):
            if flags & (1 << (15 - i)):
                props[name] = r.shortstr() if kind == "s" else r.table() if kind == "F" else r.fmt(kind)
        body = b""
        while len(body) < size:
            _, _, chunk = self.frame()
            body += chunk
        return props, body

    # operations
    def declare(self, queue: str) -> None:
        if queue in self.declared:
            return
        self.send(1, method(50, 10, struct.pack(">H", 0) + shortstr(queue) + b"\x02" + table({})))
        self.expect(1, 50, 11)
        self.declared.add(queue)

    def publish(self, queue: str, body: bytes, headers: dict) -> None:
        with self.lock:
            self.declare(queue)
            self.send(1, method(60, 40, struct.pack(">H", 0) + shortstr("") + shortstr(queue) + b"\x00"))
            flags = (1 << 13) | (1 << 12)  # headers, delivery mode
            props = table(headers) + bytes([2])
            self.send(1, struct.pack(">HHQH", 60, 0, len(body), flags) + props, 2)
            step = self.frame_max - 8
            for i in range(0, len(body), step):
                self.send(1, body[i:i + step], 3)
            self.tag += 1
            _, cls, mid, r = self.method_frame()
            if (cls, mid) == (60, 120):  # Basic.Nack
                raise AmqpError("the broker did not keep the message")
            if (cls, mid) != (60, 80):
                raise AmqpError("the broker does not confirm the message", True)

    def get(self, queue: str) -> tuple | None:
        """(delivery tag, headers, body) of the next message, or None."""
        with self.lock:
            self.declare(queue)
            self.send(1, method(60, 70, struct.pack(">H", 0) + shortstr(queue) + b"\x00"))
            _, cls, mid, r = self.method_frame()
            if (cls, mid) == (60, 72):  # GetEmpty
                return None
            if (cls, mid) != (60, 71):
                raise AmqpError("the broker answers Basic.Get out of order", True)
            tag = r.fmt("Q")
            props, body = self.content()
            return tag, props.get("headers") or {}, body

    def ack(self, tag: int) -> None:
        with self.lock:
            self.send(1, method(60, 80, struct.pack(">Q", tag) + b"\x00"))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Rabbit:
    """A RabbitMQ queue: one partition; a consumer gets each message with
    Basic.Get and acknowledges it once it is handled."""
    log = False

    def __init__(self):
        self.conns: dict = {}

    def host(self, stream) -> str:
        return urllib.parse.urlsplit(stream.where).hostname or "localhost"

    def run(self, call, stream, what, again: bool = True):
        from .stream_net import broker_failure
        try:
            if stream.ident() not in self.conns:
                password = None
                if stream.secret is not None:
                    from .crypto import text_or_secret
                    password = text_or_secret(call, "key", stream.secret)
                self.conns[stream.ident()] = Connection(stream.where, password)
            return what(self.conns[stream.ident()])
        except AmqpError as exc:
            if exc.lost:
                old = self.conns.pop(stream.ident(), None)
                if old is not None:
                    old.close()
                    # a connection the broker closed while it was idle is
                    # made again once (D169)
                    if again:
                        return self.run(call, stream, what, False)
            raise broker_failure(call, stream, exc.reason)

    def partitions(self, call, stream) -> int:
        self.run(call, stream, lambda c: c.declare(stream.name))
        return 1

    def start(self, call, stream, partition):
        return None

    def send(self, call, stream, data: str, ms: int, key, name: str | None = None):
        from .stream import KEY, TIME, Message
        headers = {TIME: ms}
        if key is not None:
            headers[KEY] = key
        self.run(call, stream, lambda c: c.publish(name or stream.name, data.encode(), headers))
        return Message(stream, None, ms, data, key)

    def poll(self, call, stream, partition, position) -> list:
        from .stream import KEY, TIME, Message, now_ms
        out = []
        for _ in range(100):
            got = self.run(call, stream, lambda c: c.get(stream.name))
            if got is None:
                break
            tag, headers, body = got
            ms = headers.get(TIME)
            out.append(Message(stream, tag, ms if isinstance(ms, int) else now_ms(), body.decode("utf-8", "replace"),
                               headers.get(KEY), 0, None, tag))
        return out

    def done(self, call, stream, partition, messages, position) -> None:
        for m in messages:
            self.run(call, stream, lambda c: c.ack(m.receipt))

    def dead(self, call, message) -> str:
        target = message.stream.name + ".dead"
        self.send(call, message.stream, message.data, message.ms, message.key, target)
        return target


RABBIT = Rabbit()
