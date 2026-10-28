"""A PostgreSQL client for nexo.db (D163): the version 3 protocol over a
socket, TLS, SCRAM-SHA-256 or cleartext passwords, and the extended query
protocol with text parameters. Rows come back typed as SQLite gives them
(int, float, str, none; booleans as 1 and 0), so the rest of nexo.db reads
them the same way."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import ssl
import struct
import threading

INT_TYPES = (20, 21, 23, 26)  # int8, int2, int4, oid
FLOAT_TYPES = (700, 701)
BOOL = 16
NUMERIC = 1700


class PgError(Exception):
    """An error the server reported (fields: code, message, detail) or a
    failure to reach it."""

    def __init__(self, message: str, code: str = "", detail: str = ""):
        super().__init__(message)
        self.message, self.code, self.detail = message, code, detail


def local_host(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def parse_url(url: str) -> dict:
    """postgres://user@host:port/database (D163)."""
    rest = url.split("://", 1)[1]
    auth, _, where = rest.rpartition("@")
    if ":" in auth:
        raise PgError("the address holds a password; give it with key: instead")
    hostport, _, database = where.partition("/")
    host, port = hostport, 5432
    if hostport.startswith("["):
        host, _, after = hostport[1:].partition("]")
        if after.startswith(":"):
            port = int(after[1:])
    elif ":" in hostport:
        host, p = hostport.rsplit(":", 1)
        port = int(p)
    return {"user": auth or "postgres", "host": host or "localhost", "port": port,
            "database": database or auth or "postgres"}


def convert(oid: int, raw: bytes | None):
    if raw is None:
        return None
    text = raw.decode("utf-8")
    if oid in INT_TYPES:
        return int(text)
    if oid in FLOAT_TYPES:
        return float(text)
    if oid == BOOL:
        return 1 if text == "t" else 0
    if oid == NUMERIC:
        try:
            return int(text)
        except ValueError:
            return float(text)
    return text


def param(value) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, float):
        return repr(value).encode()
    return str(value).encode("utf-8")


class Cursor:
    def __init__(self, names: list, rows: list):
        self.description = [(n,) for n in names] if names else None
        self.rows = rows
        self.lastrowid = rows[0][0] if rows and rows[0] else None

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class Connection:
    def __init__(self, url: str, password: str | None, timeout: float = 30.0):
        self.info = parse_url(url)
        self.lock = threading.Lock()
        host, port = self.info["host"], self.info["port"]
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise PgError(f"cannot reach {host}:{port}: {exc.strerror or 'timed out'}") from None
        # TLS is asked for always; a server on this machine may answer
        # without it (D163)
        sock.sendall(struct.pack("!ii", 8, 80877103))
        answer = sock.recv(1)
        if answer == b"S":
            context = ssl.create_default_context()
            try:
                sock = context.wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError:
                raise PgError(f"TLS with {host}: the certificate is not trusted") from None
            except (ssl.SSLError, OSError):
                raise PgError(f"TLS with {host}: the TLS handshake failed") from None
        elif not local_host(host):
            sock.close()
            raise PgError(f"{host} does not offer TLS")
        self.sock = sock
        self.buf = b""
        self.startup(password)

    # ------------------------------------------------------------ wire
    def send(self, kind: bytes, body: bytes = b"") -> None:
        self.sock.sendall(kind + struct.pack("!i", len(body) + 4) + body)

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except OSError as exc:
                raise PgError(f"connection lost: {exc}") from None
            if not chunk:
                raise PgError("the server closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def message(self) -> tuple[bytes, bytes]:
        head = self.read(5)
        kind, size = head[:1], struct.unpack("!i", head[1:])[0]
        return kind, self.read(size - 4)

    @staticmethod
    def error(body: bytes) -> PgError:
        fields = {}
        for part in body.split(b"\0"):
            if part:
                fields[part[:1].decode()] = part[1:].decode("utf-8", "replace")
        return PgError(fields.get("M", "error"), fields.get("C", ""), fields.get("D", ""))

    # ------------------------------------------------------- startup
    def startup(self, password: str | None) -> None:
        params = {"user": self.info["user"], "database": self.info["database"], "client_encoding": "UTF8",
                  "extra_float_digits": "3"}
        body = struct.pack("!i", 196608) + b"".join(k.encode() + b"\0" + v.encode() + b"\0"
                                                     for k, v in params.items()) + b"\0"
        self.sock.sendall(struct.pack("!i", len(body) + 4) + body)
        scram = None
        while True:
            kind, body = self.message()
            if kind == b"E":
                raise self.error(body)
            if kind == b"R":
                code = struct.unpack("!i", body[:4])[0]
                if code == 0:
                    continue
                if password is None:
                    raise PgError("the server asks for a password; give it with key:")
                if code == 3:
                    self.send(b"p", password.encode() + b"\0")
                elif code == 10:
                    mechanisms = body[4:].split(b"\0")
                    if b"SCRAM-SHA-256" not in mechanisms:
                        raise PgError("the server asks for an unsupported authentication")
                    scram = Scram(password)
                    first = scram.first().encode()
                    self.send(b"p", b"SCRAM-SHA-256\0" + struct.pack("!i", len(first)) + first)
                elif code == 11:
                    self.send(b"p", scram.final(body[4:].decode()).encode())
                elif code == 12:
                    scram.verify(body[4:].decode())
                else:
                    raise PgError("the server asks for an unsupported authentication")
            elif kind == b"Z":
                return

    # -------------------------------------------------------- queries
    def execute(self, sql: str, params: list) -> Cursor:
        with self.lock:
            values = [param(p) for p in params]
            bind = b"\0\0" + struct.pack("!hh", 0, len(values))
            for v in values:
                bind += struct.pack("!i", -1) if v is None else struct.pack("!i", len(v)) + v
            bind += struct.pack("!h", 0)
            out = (b"P" + struct.pack("!i", 4 + 1 + len(sql.encode()) + 1 + 2) + b"\0" + sql.encode() + b"\0"
                   + struct.pack("!h", 0))
            out += b"B" + struct.pack("!i", len(bind) + 4) + bind
            out += b"D" + struct.pack("!i", 6) + b"P\0"
            out += b"E" + struct.pack("!i", 9) + b"\0" + struct.pack("!i", 0)
            out += b"S" + struct.pack("!i", 4)
            try:
                self.sock.sendall(out)
            except OSError as exc:
                raise PgError(f"connection lost: {exc}") from None
            names, types, rows, failed = [], [], [], None
            while True:
                kind, body = self.message()
                if kind == b"T":
                    count = struct.unpack("!h", body[:2])[0]
                    at = 2
                    for _ in range(count):
                        end = body.index(b"\0", at)
                        names.append(body[at:end].decode())
                        types.append(struct.unpack("!i", body[end + 7:end + 11])[0])
                        at = end + 19
                elif kind == b"D":
                    count = struct.unpack("!h", body[:2])[0]
                    at, row = 2, []
                    for i in range(count):
                        size = struct.unpack("!i", body[at:at + 4])[0]
                        at += 4
                        raw = None if size < 0 else body[at:at + size]
                        at += max(size, 0)
                        row.append(convert(types[i], raw))
                    rows.append(tuple(row))
                elif kind == b"E":
                    failed = self.error(body)
                elif kind == b"Z":
                    break
            if failed is not None:
                raise failed
            return Cursor(names, rows)

    def close(self) -> None:
        try:
            self.send(b"X")
            self.sock.close()
        except OSError:
            pass


class Scram:
    """SCRAM-SHA-256 (RFC 5802, RFC 7677) without channel binding."""

    def __init__(self, password: str, user: str = ""):
        # PostgreSQL takes the user from the startup message; MongoDB from
        # here (with = and , written as =3D and =2C)
        self.password = password.encode("utf-8")
        self.user = user.replace("=", "=3D").replace(",", "=2C")
        self.nonce = base64.b64encode(os.urandom(18)).decode()

    def first(self) -> str:
        self.bare = f"n={self.user},r={self.nonce}"
        return "n,," + self.bare

    def final(self, server_first: str) -> str:
        parts = dict(p.split("=", 1) for p in server_first.split(","))
        nonce, salt, rounds = parts["r"], base64.b64decode(parts["s"]), int(parts["i"])
        if not nonce.startswith(self.nonce):
            raise PgError("the server's SCRAM answer is not valid")
        salted = hashlib.pbkdf2_hmac("sha256", self.password, salt, rounds)
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        stored = hashlib.sha256(client_key).digest()
        without = f"c=biws,r={nonce}"
        self.auth = f"{self.bare},{server_first},{without}".encode()
        signature = hmac.new(stored, self.auth, hashlib.sha256).digest()
        proof = bytes(a ^ b for a, b in zip(client_key, signature))
        self.server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
        return f"{without},p={base64.b64encode(proof).decode()}"

    def verify(self, server_final: str) -> None:
        expected = hmac.new(self.server_key, self.auth, hashlib.sha256).digest()
        if server_final.split(",")[0] != "v=" + base64.b64encode(expected).decode():
            raise PgError("the server's SCRAM signature is not valid")
