"""A RabbitMQ stand-in for the tests of nexo.stream (D169), written from
AMQP 0-9-1 on its own (it shares no code with nexo/stdlib/amqp.py): the
handshake with PLAIN, one channel, Queue.Declare, Confirm.Select,
Basic.Publish with its content, Basic.Get and Basic.Ack. Messages taken
and not acknowledged go back to their queue when the connection closes.

    broker = start(users={"shop": "pw"})
    broker.port, broker.queues["orders"]  # [(headers, body)]
"""
from __future__ import annotations

import socketserver
import struct
import threading


def short(s: str) -> bytes:
    return bytes([len(s.encode())]) + s.encode()


def read_short(d: bytes, p: int) -> tuple[str, int]:
    n = d[p]
    return d[p + 1:p + 1 + n].decode(), p + 1 + n


def read_table(d: bytes, p: int) -> tuple[dict, int]:
    size = struct.unpack(">I", d[p:p + 4])[0]
    p += 4
    end, out = p + size, {}
    while p < end:
        key, p = read_short(d, p)
        kind = chr(d[p])
        p += 1
        if kind == "S":
            n = struct.unpack(">I", d[p:p + 4])[0]
            out[key] = d[p + 4:p + 4 + n].decode()
            p += 4 + n
        elif kind == "l":
            out[key] = struct.unpack(">q", d[p:p + 8])[0]
            p += 8
        elif kind == "t":
            out[key] = bool(d[p])
            p += 1
        elif kind == "F":
            out[key], p = read_table(d, p)
        else:
            raise ValueError(f"field type {kind}")
    return out, p


def write_table(t: dict) -> bytes:
    body = b""
    for k, v in t.items():
        if isinstance(v, int):
            body += short(k) + b"l" + struct.pack(">q", v)
        else:
            body += short(k) + b"S" + struct.pack(">I", len(v.encode())) + v.encode()
    return struct.pack(">I", len(body)) + body


class Broker:
    def __init__(self, users):
        self.users = users
        self.queues: dict = {}  # name -> [(headers, body)]
        self.lock = threading.Lock()


def start(users=None) -> Broker:
    broker = Broker(users or {"guest": "guest"})

    class Handler(socketserver.BaseRequestHandler):
        def recv(self, n):
            out = b""
            while len(out) < n:
                chunk = self.request.recv(n - len(out))
                if not chunk:
                    raise ConnectionError
                out += chunk
            return out

        def frame(self):
            kind, channel, size = struct.unpack(">BHI", self.recv(7))
            payload = self.recv(size)
            assert self.recv(1) == b"\xce"
            return kind, channel, payload

        def send(self, kind, channel, payload):
            self.request.sendall(struct.pack(">BHI", kind, channel, len(payload)) + payload + b"\xce")

        def method(self, channel, cls, mid, args=b""):
            self.send(1, channel, struct.pack(">HH", cls, mid) + args)

        def handle(self):
            taken: dict = {}  # tag -> (queue, message)
            try:
                assert self.recv(8) == b"AMQP\x00\x00\x09\x01"
                self.method(0, 10, 10, b"\x00\x09" + write_table({"product": "stand-in"})
                            + struct.pack(">I", 5) + b"PLAIN" + struct.pack(">I", 5) + b"en_US")
                _, _, p = self.frame()
                _, q = read_table(p, 4)
                mech, q = read_short(p, q)
                n = struct.unpack(">I", p[q:q + 4])[0]
                _, user, password = p[q + 4:q + 4 + n].split(b"\0")
                if mech != "PLAIN" or broker.users.get(user.decode()) != password.decode():
                    text = "ACCESS_REFUSED - Login was refused"
                    self.method(0, 10, 50, struct.pack(">H", 403) + short(text) + struct.pack(">HH", 0, 0))
                    return
                self.method(0, 10, 30, struct.pack(">HIH", 2047, 131072, 60))
                self.frame()  # TuneOk
                _, _, p = self.frame()  # Open
                self.method(0, 10, 41, short(""))
                tag = 0
                pending = None  # (queue, headers, size, body) of a publish
                while True:
                    kind, channel, p = self.frame()
                    if kind == 8:
                        continue
                    if kind == 2:
                        size = struct.unpack(">Q", p[4:12])[0]
                        flags = struct.unpack(">H", p[12:14])[0]
                        headers = read_table(p, 14)[0] if flags & (1 << 13) else {}
                        pending = (pending[0], headers, size, b"")
                        if size == 0:
                            kind, p = 3, b""
                        else:
                            continue
                    if kind == 3:
                        queue, headers, size, body = pending
                        body += p
                        pending = (queue, headers, size, body)
                        if len(body) >= size:
                            with broker.lock:
                                broker.queues.setdefault(queue, []).append((headers, body))
                            tag += 1
                            self.method(channel, 60, 80, struct.pack(">Q", tag) + b"\x00")
                        continue
                    cls, mid = struct.unpack(">HH", p[:4])
                    if (cls, mid) == (20, 10):
                        self.method(channel, 20, 11, struct.pack(">I", 0))
                    elif (cls, mid) == (85, 10):
                        self.method(channel, 85, 11)
                    elif (cls, mid) == (50, 10):
                        queue, _ = read_short(p, 6)
                        with broker.lock:
                            broker.queues.setdefault(queue, [])
                            count = len(broker.queues[queue])
                        self.method(channel, 50, 11, short(queue) + struct.pack(">II", count, 0))
                    elif (cls, mid) == (60, 40):
                        _, q = read_short(p, 6)
                        queue, _ = read_short(p, q)
                        pending = (queue, {}, 0, b"")
                    elif (cls, mid) == (60, 70):
                        queue, _ = read_short(p, 6)
                        with broker.lock:
                            items = broker.queues.get(queue)
                            if not items:
                                self.method(channel, 60, 72, short(""))
                                continue
                            message = items.pop(0)
                        tag += 1
                        taken[tag] = (queue, message)
                        self.method(channel, 60, 71, struct.pack(">Q", tag) + b"\x00" + short("") + short(queue)
                                    + struct.pack(">I", len(items)))
                        headers, body = message
                        props = write_table(headers)
                        self.send(2, channel, struct.pack(">HHQH", 60, 0, len(body), 1 << 13) + props)
                        self.send(3, channel, body)
                    elif (cls, mid) == (60, 80):
                        taken.pop(struct.unpack(">Q", p[4:12])[0], None)
                    elif (cls, mid) == (10, 50):
                        self.method(0, 10, 51)
                        return
                    else:
                        raise ValueError(f"method {cls}.{mid}")
            except (ConnectionError, AssertionError, ValueError, OSError):
                pass
            finally:
                with broker.lock:
                    for queue, message in taken.values():
                        broker.queues.setdefault(queue, []).insert(0, message)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    broker.port = server.server_address[1]
    broker.server = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return broker
