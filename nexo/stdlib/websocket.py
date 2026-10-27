"""A WebSocket client for nexo.stream (D169): `websocket("wss://host/path")`
connects (TLS for wss://; ws:// only to this machine), each text or binary
message the server sends is a message of the stream, and `send` writes a
text message on the same connection. A server keeps no messages for a
consumer that is not connected, so there is nothing to acknowledge."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import ssl
import struct
import time as _time
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WsError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def local_host(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


class Socket:
    def __init__(self, url: str, timeout: float = 30.0):
        u = urllib.parse.urlsplit(url)
        host = u.hostname or "localhost"
        tls = u.scheme == "wss"
        if not tls and not local_host(host):
            raise WsError(f"{host} is reached with wss:// only")
        port = u.port or (443 if tls else 80)
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise WsError(f"cannot reach {host}:{port}: {exc.strerror or 'timed out'}") from None
        if tls:
            try:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError:
                raise WsError(f"TLS with {host}: the certificate is not trusted") from None
            except (ssl.SSLError, OSError):
                raise WsError(f"TLS with {host}: the TLS handshake failed") from None
        self.sock, self.buf, self.parts = sock, b"", []
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        hostport = host if u.port is None else f"{host}:{u.port}"
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: nexo/0.1\r\n\r\n")
                          .encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.recv()
            if not chunk:
                raise WsError("the server closed the connection")
            head += chunk
        head, _, self.buf = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ")[1] if len(lines[0].split(" ")) > 1 else "?"
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if status != "101" or headers.get("sec-websocket-accept") != accept:
            raise WsError(f"the server does not open a WebSocket (it answers {status})")

    def recv(self) -> bytes:
        try:
            return self.sock.recv(65536)
        except (socket.timeout, TimeoutError):
            raise
        except OSError as exc:
            raise WsError(f"connection lost: {exc}") from None

    def send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(head + mask + masked)
        except OSError as exc:
            raise WsError(f"connection lost: {exc}") from None

    def send(self, text: str) -> None:
        self.send_frame(1, text.encode("utf-8"))

    def frame(self):
        """(fin, opcode, payload) of a whole frame in the buffer, or None."""
        b = self.buf
        if len(b) < 2:
            return None
        n, at = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, at = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, at = struct.unpack(">Q", b[2:10])[0], 10
        mask = b[at:at + 4] if b[1] & 0x80 else None
        at += 4 if mask else 0
        if len(b) < at + n:
            return None
        payload = b[at:at + n]
        if mask:
            payload = bytes(x ^ mask[i % 4] for i, x in enumerate(payload))
        self.buf = b[at + n:]
        return bool(b[0] & 0x80), b[0] & 0x0F, payload

    def messages(self, wait: float) -> list[str]:
        """The messages that arrive within `wait` seconds."""
        out = []
        end = _time.monotonic() + wait
        while True:
            f = self.frame()
            while f is not None:
                fin, opcode, payload = f
                if opcode == 9:
                    self.send_frame(10, payload)
                elif opcode == 8:
                    self.send_frame(8, payload[:2])
                    raise WsError("the server closed the connection")
                elif opcode in (0, 1, 2):
                    self.parts.append(payload)
                    if fin:
                        out.append(b"".join(self.parts).decode("utf-8", "replace"))
                        self.parts = []
                f = self.frame()
            left = end - _time.monotonic()
            if left <= 0:
                return out
            self.sock.settimeout(left)
            try:
                chunk = self.recv()
            except (socket.timeout, TimeoutError):
                return out
            finally:
                self.sock.settimeout(30.0)
            if not chunk:
                raise WsError("the server closed the connection")
            self.buf += chunk

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def dead_name(url: str) -> str:
    """The local stream of a WebSocket's dead letters: its address, with
    what a file name cannot have as `_`, then `.dead`."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[-1]) + ".dead"


class WebSocket:
    """A WebSocket server as a source: one partition, nothing to save."""
    log = False

    def __init__(self):
        self.socks: dict = {}
        self.counts: dict = {}

    def host(self, stream) -> str:
        return urllib.parse.urlsplit(stream.where).hostname or "localhost"

    def run(self, call, stream, what):
        from .stream_net import broker_failure
        try:
            if stream.ident() not in self.socks:
                self.socks[stream.ident()] = Socket(stream.where)
            return what(self.socks[stream.ident()])
        except WsError as exc:
            old = self.socks.pop(stream.ident(), None)
            if old is not None:
                old.close()
            raise broker_failure(call, stream, exc.reason)

    def partitions(self, call, stream) -> int:
        self.run(call, stream, lambda s: None)
        return 1

    def start(self, call, stream, partition):
        return None

    def send(self, call, stream, data: str, ms: int, key, name: str | None = None):
        from .stream import Message
        self.run(call, stream, lambda s: s.send(data))
        return Message(stream, None, ms, data, key)

    def poll(self, call, stream, partition, position) -> list:
        from .stream import Message, now_ms
        out = []
        for text in self.run(call, stream, lambda s: s.messages(0.2)):
            n = self.counts.get(stream.ident(), 0) + 1
            self.counts[stream.ident()] = n
            out.append(Message(stream, n, now_ms(), text))
        return out

    def done(self, call, stream, partition, messages, position) -> None:
        return None

    def dead(self, call, message) -> str:
        from .stream import Stream, append
        target = dead_name(message.stream.where)
        append(Stream("websocket", target), message.data, message.ms, message.key)
        return target


WEBSOCKET = WebSocket()
