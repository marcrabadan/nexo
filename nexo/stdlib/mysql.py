"""A MySQL client for nexo.db (D170): the client/server protocol over a
socket, TLS, the caching_sha2_password and mysql_native_password
logins, and prepared statements with binary parameters and rows. Rows
come back typed as SQLite gives them (int, float, str, none; booleans as
1 and 0), so the rest of nexo.db reads them the same way."""
from __future__ import annotations

import hashlib
import socket
import ssl
import struct
import threading

from .pg import local_host

# capability flags
LONG_PASSWORD, CONNECT_WITH_DB, PROTOCOL_41, SSL = 0x1, 0x8, 0x200, 0x800
TRANSACTIONS, SECURE_CONNECTION, MULTI_RESULTS = 0x2000, 0x8000, 0x20000
PLUGIN_AUTH, LENENC_DATA, DEPRECATE_EOF = 0x80000, 0x200000, 0x1000000
WANTED = (LONG_PASSWORD | CONNECT_WITH_DB | PROTOCOL_41 | TRANSACTIONS | SECURE_CONNECTION | MULTI_RESULTS
          | PLUGIN_AUTH | LENENC_DATA | DEPRECATE_EOF)
UTF8MB4 = 255  # utf8mb4_0900_ai_ci: the connection's character set
MAX_PACKET = 0xFFFFFF
# the session of every connection: "name" quotes a name, as in SQLite and
# PostgreSQL; || joins texts; times are UTC (D170)
SESSION = ("SET SESSION sql_mode = 'ANSI_QUOTES,PIPES_AS_CONCAT,STRICT_ALL_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
           "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY', time_zone = '+00:00'")
# statements sent as text: they take no parameters and give no rows
CONTROL = ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "SET ")

T_DECIMAL, T_TINY, T_SHORT, T_LONG, T_FLOAT, T_DOUBLE, T_NULL = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
T_TIMESTAMP, T_LONGLONG, T_INT24, T_DATE, T_TIME, T_DATETIME, T_YEAR = 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D
T_VAR_STRING = 0xFD
UNSIGNED = 0x20


class MysqlError(Exception):
    """An error the server reported (its number in `code`) or a failure to
    reach it."""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.message, self.code = message, code


def parse_url(url: str) -> dict:
    """mysql://user@host:port/database (D170)."""
    rest = url.split("://", 1)[1]
    auth, _, where = rest.rpartition("@")
    if ":" in auth:
        raise MysqlError("the address holds a password; give it with key: instead")
    hostport, _, database = where.partition("/")
    host, port = hostport, 3306
    if hostport.startswith("["):
        host, _, after = hostport[1:].partition("]")
        if after.startswith(":"):
            port = int(after[1:])
    elif ":" in hostport:
        host, p = hostport.rsplit(":", 1)
        port = int(p)
    if not database:
        raise MysqlError("the address names no database: mysql://user@host/database")
    return {"user": auth or "root", "host": host or "localhost", "port": port, "database": database}


# ------------------------------------------------------------ encoding

def lenenc_int(n: int) -> bytes:
    if n < 251:
        return bytes([n])
    if n < 1 << 16:
        return b"\xfc" + struct.pack("<H", n)
    if n < 1 << 24:
        return b"\xfd" + struct.pack("<I", n)[:3]
    return b"\xfe" + struct.pack("<Q", n)


class Reader:
    def __init__(self, data: bytes, at: int = 0):
        self.d, self.p = data, at

    def take(self, n: int) -> bytes:
        out = self.d[self.p:self.p + n]
        self.p += n
        return out

    def int(self, n: int) -> int:
        return int.from_bytes(self.take(n), "little")

    def lenenc(self) -> int | None:
        first = self.int(1)
        if first < 251:
            return first
        if first == 0xFB:
            return None
        return self.int({0xFC: 2, 0xFD: 3, 0xFE: 8}[first])

    def lenenc_bytes(self) -> bytes | None:
        n = self.lenenc()
        return None if n is None else self.take(n)

    def nul(self) -> bytes:
        end = self.d.index(b"\0", self.p)
        out = self.d[self.p:end]
        self.p = end + 1
        return out


def number(text: str):
    """A DECIMAL as SQLite gives a number: an int when it is whole."""
    try:
        return int(text)
    except ValueError:
        return float(text)


def binary_value(r: Reader, kind: int, flags: int, charset: int):
    if kind == T_TINY:
        return r.int(1) if flags & UNSIGNED else struct.unpack("<b", r.take(1))[0]
    if kind in (T_SHORT, T_YEAR):
        return struct.unpack("<H" if flags & UNSIGNED else "<h", r.take(2))[0]
    if kind in (T_LONG, T_INT24):
        return struct.unpack("<I" if flags & UNSIGNED else "<i", r.take(4))[0]
    if kind == T_LONGLONG:
        return struct.unpack("<Q" if flags & UNSIGNED else "<q", r.take(8))[0]
    if kind == T_FLOAT:
        return struct.unpack("<f", r.take(4))[0]
    if kind == T_DOUBLE:
        return struct.unpack("<d", r.take(8))[0]
    if kind in (T_DATE, T_DATETIME, T_TIMESTAMP):
        n = r.int(1)
        y, mo, d, h, mi, s, us = 0, 0, 0, 0, 0, 0, 0
        if n >= 4:
            y, mo, d = r.int(2), r.int(1), r.int(1)
        if n >= 7:
            h, mi, s = r.int(1), r.int(1), r.int(1)
        if n >= 11:
            us = r.int(4)
        text = f"{y:04d}-{mo:02d}-{d:02d}"
        if kind != T_DATE:
            text += f" {h:02d}:{mi:02d}:{s:02d}" + (f".{us:06d}" if us else "")
        return text
    if kind == T_TIME:
        n = r.int(1)
        neg, days, h, mi, s, us = 0, 0, 0, 0, 0, 0
        if n >= 8:
            neg, days, h, mi, s = r.int(1), r.int(4), r.int(1), r.int(1), r.int(1)
        if n >= 12:
            us = r.int(4)
        return f"{'-' if neg else ''}{days * 24 + h:02d}:{mi:02d}:{s:02d}" + (f".{us:06d}" if us else "")
    raw = r.lenenc_bytes()
    text = raw.decode("utf-8", "replace")
    if kind in (T_DECIMAL, 0xF6):
        return number(text)
    return text


class Cursor:
    def __init__(self, names: list, rows: list, lastrowid=None):
        self.description = [(n,) for n in names] if names else None
        self.rows = rows
        self.lastrowid = lastrowid

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


def native_password(password: bytes, nonce: bytes) -> bytes:
    one = hashlib.sha1(password).digest()
    two = hashlib.sha1(nonce + hashlib.sha1(one).digest()).digest()
    return bytes(a ^ b for a, b in zip(one, two))


def sha2_password(password: bytes, nonce: bytes) -> bytes:
    one = hashlib.sha256(password).digest()
    two = hashlib.sha256(hashlib.sha256(one).digest() + nonce).digest()
    return bytes(a ^ b for a, b in zip(one, two))


def scramble(plugin: str, password: str | None, nonce: bytes) -> bytes:
    if not password:
        return b""
    if plugin == "mysql_native_password":
        return native_password(password.encode("utf-8"), nonce)
    if plugin == "caching_sha2_password":
        return sha2_password(password.encode("utf-8"), nonce)
    raise MysqlError(f"the server asks for an unsupported login ({plugin})")


class Connection:
    def __init__(self, url: str, password: str | None, timeout: float = 30.0):
        self.info = parse_url(url)
        self.lock = threading.Lock()
        host, port = self.info["host"], self.info["port"]
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise MysqlError(f"cannot reach {host}:{port}: {exc.strerror or 'timed out'}") from None
        self.buf = b""
        self.seq = 0
        self.tls = False
        self.login(password)
        self.query(SESSION)

    # ------------------------------------------------------------ wire
    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except OSError as exc:
                raise MysqlError(f"connection lost: {exc}") from None
            if not chunk:
                raise MysqlError("the server closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def packet(self) -> bytes:
        out = b""
        while True:
            head = self.read(4)
            size = int.from_bytes(head[:3], "little")
            self.seq = (head[3] + 1) & 0xFF
            out += self.read(size)
            if size < MAX_PACKET:
                return out

    def send(self, payload: bytes) -> None:
        out = b""
        while True:
            part, payload = payload[:MAX_PACKET], payload[MAX_PACKET:]
            out += len(part).to_bytes(3, "little") + bytes([self.seq]) + part
            self.seq = (self.seq + 1) & 0xFF
            if len(part) < MAX_PACKET:
                break
        try:
            self.sock.sendall(out)
        except OSError as exc:
            raise MysqlError(f"connection lost: {exc}") from None

    def command(self, payload: bytes) -> None:
        self.seq = 0
        self.send(payload)

    @staticmethod
    def error(p: bytes) -> MysqlError:
        r = Reader(p, 1)
        code = r.int(2)
        if r.d[r.p:r.p + 1] == b"#":
            r.take(6)
        return MysqlError(r.d[r.p:].decode("utf-8", "replace"), code)

    @staticmethod
    def ok(p: bytes) -> int:
        """The last insert id of an OK packet."""
        r = Reader(p, 1)
        r.lenenc()
        return r.lenenc() or 0

    # ----------------------------------------------------------- login
    def login(self, password: str | None) -> None:
        host = self.info["host"]
        p = self.packet()
        if p[:1] == b"\xff":
            raise self.error(p)
        r = Reader(p, 1)
        r.nul()  # server version
        r.take(4)
        nonce = r.take(8)
        r.take(1)
        caps = r.int(2)
        r.take(3)
        caps |= r.int(2) << 16
        size = r.int(1)
        r.take(10)
        if caps & SECURE_CONNECTION:
            nonce += r.take(max(13, size - 8))[:max(12, size - 9)]
        plugin = r.nul().decode() if caps & PLUGIN_AUTH else "mysql_native_password"
        if not caps & PROTOCOL_41:
            raise MysqlError("the server is too old (it does not speak protocol 4.1)")
        mine = WANTED & caps
        self.caps = mine
        # TLS is asked for always; a server on this machine may do without
        # it (D170)
        if caps & SSL:
            mine |= SSL
            self.send(struct.pack("<IIB", mine, MAX_PACKET, UTF8MB4) + bytes(23))
            context = ssl.create_default_context()
            try:
                self.sock = context.wrap_socket(self.sock, server_hostname=host)
            except ssl.SSLCertVerificationError:
                raise MysqlError(f"TLS with {host}: the certificate is not trusted") from None
            except (ssl.SSLError, OSError):
                raise MysqlError(f"TLS with {host}: the TLS handshake failed") from None
            self.tls = True
        elif not local_host(host):
            raise MysqlError(f"{host} does not offer TLS")
        auth = scramble(plugin, password, nonce)
        body = struct.pack("<IIB", mine, MAX_PACKET, UTF8MB4) + bytes(23)
        body += self.info["user"].encode() + b"\0"
        body += (lenenc_int(len(auth)) if mine & LENENC_DATA else bytes([len(auth)])) + auth
        body += self.info["database"].encode() + b"\0" + plugin.encode() + b"\0"
        self.send(body)
        while True:
            p = self.packet()
            head = p[:1]
            if head == b"\x00":
                return
            if head == b"\xff":
                raise self.error(p)
            if head == b"\xfe":
                # another login: its plugin and nonce
                r = Reader(p, 1)
                plugin = r.nul().decode()
                nonce = r.d[r.p:].rstrip(b"\0")
                self.send(scramble(plugin, password, nonce))
            elif head == b"\x01" and plugin == "caching_sha2_password":
                if p[1:2] == b"\x03":
                    continue  # the fast login worked: an OK packet follows
                if p[1:2] == b"\x04":
                    # the full login sends the password itself: over TLS only
                    if not self.tls:
                        raise MysqlError("the server asks for the password over TLS, which it does not offer")
                    self.send((password or "").encode("utf-8") + b"\0")
            else:
                raise MysqlError("the server's login answer is not valid")

    # -------------------------------------------------------- results
    def columns(self, count: int) -> list:
        """(name, type, flags, charset) of each column of a result."""
        out = []
        for _ in range(count):
            r = Reader(self.packet())
            for _ in range(4):
                r.lenenc_bytes()
            name = r.lenenc_bytes().decode("utf-8", "replace")
            r.lenenc_bytes()
            r.lenenc()
            charset = r.int(2)
            r.take(4)
            out.append((name, r.int(1), r.int(2), charset))
        return out

    def eof(self) -> None:
        if not self.caps & DEPRECATE_EOF:
            self.packet()

    def result(self, binary: bool) -> Cursor:
        p = self.packet()
        if p[:1] == b"\xff":
            raise self.error(p)
        if p[:1] == b"\x00":
            return Cursor([], [], self.ok(p))
        cols = self.columns(Reader(p).lenenc())
        self.eof()
        rows = []
        while True:
            p = self.packet()
            if p[:1] == b"\xfe" and len(p) < 9:
                break
            if p[:1] == b"\xff":
                raise self.error(p)
            if binary:
                n = len(cols)
                nulls = p[1:1 + (n + 9) // 8]
                r = Reader(p, 1 + (n + 9) // 8)
                row = []
                for i, (_, kind, flags, charset) in enumerate(cols):
                    if nulls[(i + 2) // 8] & (1 << ((i + 2) % 8)):
                        row.append(None)
                    else:
                        row.append(binary_value(r, kind, flags, charset))
            else:
                r = Reader(p)
                row = [None if (v := r.lenenc_bytes()) is None else v.decode("utf-8", "replace") for _ in cols]
            rows.append(tuple(row))
        return Cursor([c[0] for c in cols], rows)

    # -------------------------------------------------------- queries
    def query(self, sql: str) -> Cursor:
        self.command(b"\x03" + sql.encode("utf-8"))
        return self.result(False)

    def execute(self, sql: str, params: list) -> Cursor:
        with self.lock:
            if not params and sql.lstrip().upper().startswith(CONTROL):
                return self.query(sql)
            self.command(b"\x16" + sql.encode("utf-8"))
            p = self.packet()
            if p[:1] == b"\xff":
                raise self.error(p)
            r = Reader(p, 1)
            stmt, ncols, nparams = r.int(4), r.int(2), r.int(2)
            if nparams:
                self.columns(nparams)
                self.eof()
            if ncols:
                self.columns(ncols)
                self.eof()
            try:
                if nparams != len(params):
                    raise MysqlError(f"the statement takes {nparams} values, not {len(params)}")
                body = b"\x17" + struct.pack("<IBI", stmt, 0, 1)
                if params:
                    nulls = bytearray((len(params) + 7) // 8)
                    types, values = b"", b""
                    for i, v in enumerate(params):
                        if v is None:
                            nulls[i // 8] |= 1 << (i % 8)
                            types += bytes([T_NULL, 0])
                        elif isinstance(v, bool) or isinstance(v, int):
                            types += bytes([T_LONGLONG, 0])
                            values += struct.pack("<q", int(v))
                        elif isinstance(v, float):
                            types += bytes([T_DOUBLE, 0])
                            values += struct.pack("<d", v)
                        else:
                            data = str(v).encode("utf-8")
                            types += bytes([T_VAR_STRING, 0])
                            values += lenenc_int(len(data)) + data
                    body += bytes(nulls) + b"\x01" + types + values
                self.command(body)
                return self.result(True)
            finally:
                self.command(b"\x19" + struct.pack("<I", stmt))

    def close(self) -> None:
        try:
            self.command(b"\x01")
            self.sock.close()
        except (OSError, MysqlError):
            pass
