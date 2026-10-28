"""A MongoDB client for nexo.db (D171): BSON and OP_MSG over a socket, TLS
for any host but this machine, SCRAM-SHA-256, the primary of a replica
set, and the commands a document store of nexo.db needs (docdb.py).

Conditions come in docdb's form (its nnf); the key column of a table is
the document's `_id`, and a none field is left out of the document."""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import struct
import sys
import threading
from urllib.parse import unquote

from .pg import Scram, local_host

OP_MSG = 2013
OPS = {"lt": "$lt", "gt": "$gt", "le": "$lte", "ge": "$gte"}


class MongoError(Exception):
    """An error the server reported (its code) or a failure to reach it;
    `duplicate` is the column of a broken unique index."""

    def __init__(self, message: str, code: int = 0, duplicate: str | None = None):
        super().__init__(message)
        self.message, self.code, self.duplicate = message, code, duplicate


def parse_url(url: str) -> dict:
    """mongodb://user@host:port,host2:port/database (D171)."""
    rest = url.split("://", 1)[1]
    rest = rest.split("?", 1)[0]
    auth, _, where = rest.rpartition("@")
    if ":" in auth:
        raise MongoError("the address holds a password; give it with key: instead")
    hosts_text, _, database = where.partition("/")
    if not database:
        raise MongoError("the address names no database: mongodb://user@host/database")
    hosts = []
    for part in hosts_text.split(","):
        host, port = part, 27017
        if part.startswith("["):
            host, _, after = part[1:].partition("]")
            if after.startswith(":"):
                port = int(after[1:])
        elif ":" in part:
            host, p = part.rsplit(":", 1)
            port = int(p)
        hosts.append((host or "localhost", port))
    return {"user": unquote(auth), "hosts": hosts, "database": database}


# ------------------------------------------------------------------ BSON

def cstring(s: str) -> bytes:
    data = s.encode("utf-8")
    if b"\0" in data:
        raise MongoError("a name holds a NUL character")
    return data + b"\0"


def encode(doc: dict) -> bytes:
    out = b""
    for k, v in doc.items():
        out += element(k, v)
    return struct.pack("<i", len(out) + 5) + out + b"\0"


def element(name: str, v) -> bytes:
    key = cstring(name)
    if v is None:
        return b"\x0a" + key
    if isinstance(v, bool):
        return b"\x08" + key + (b"\x01" if v else b"\x00")
    if isinstance(v, int):
        return b"\x12" + key + struct.pack("<q", v)
    if isinstance(v, float):
        return b"\x01" + key + struct.pack("<d", v)
    if isinstance(v, str):
        data = v.encode("utf-8")
        return b"\x02" + key + struct.pack("<i", len(data) + 1) + data + b"\0"
    if isinstance(v, (bytes, bytearray)):
        return b"\x05" + key + struct.pack("<i", len(v)) + b"\x00" + bytes(v)
    if isinstance(v, dict):
        return b"\x03" + key + encode(v)
    if isinstance(v, list):
        return b"\x04" + key + encode({str(i): x for i, x in enumerate(v)})
    raise MongoError(f"a value of type {type(v).__name__} cannot be sent")


def decode(data: bytes, at: int = 0) -> tuple[dict, int]:
    """A document and the offset after it."""
    size = struct.unpack_from("<i", data, at)[0]
    end = at + size - 1
    p = at + 4
    out: dict = {}
    while p < end:
        kind = data[p]
        p += 1
        z = data.index(b"\0", p)
        name = data[p:z].decode("utf-8", "replace")
        p = z + 1
        if kind == 0x01:
            v = struct.unpack_from("<d", data, p)[0]
            p += 8
        elif kind == 0x02:
            n = struct.unpack_from("<i", data, p)[0]
            v = data[p + 4:p + 3 + n].decode("utf-8", "replace")
            p += 4 + n
        elif kind in (0x03, 0x04):
            v, p = decode(data, p)
            if kind == 0x04:
                v = list(v.values())
        elif kind == 0x05:
            n = struct.unpack_from("<i", data, p)[0]
            v = data[p + 5:p + 5 + n]
            p += 5 + n
        elif kind == 0x07:
            v = data[p:p + 12].hex()
            p += 12
        elif kind == 0x08:
            v = data[p] != 0
            p += 1
        elif kind in (0x09, 0x12):
            v = struct.unpack_from("<q", data, p)[0]
            p += 8
        elif kind == 0x0A:
            v = None
        elif kind == 0x10:
            v = struct.unpack_from("<i", data, p)[0]
            p += 4
        elif kind == 0x11:
            v = struct.unpack_from("<Q", data, p)[0]
            p += 8
        elif kind == 0x13:
            v = data[p:p + 16].hex()
            p += 16
        else:
            raise MongoError(f"the server sent a BSON value of type {kind}")
        out[name] = v
    return out, end + 1


# ------------------------------------------------------------ conditions

def regex_text(text: str) -> str:
    """A text as a regular expression that matches it anywhere."""
    return "".join("\\" + c if c.isascii() and not c.isalnum() and c != "_" else c for c in text)


def filter_of(cond, key: str):
    """A condition of docdb as a filter of `find`."""
    if cond is None:
        return {}
    kind = cond[0]
    if kind in ("and", "or"):
        items = [filter_of(c, key) for c in cond[1]]
        if not items:
            return {}
        if kind == "and" and len(items) == 1:
            return items[0]
        return {"$and" if kind == "and" else "$or": items}
    field = "_id" if cond[1] == key else cond[1]
    if kind == "cmp":
        op, v = cond[2], cond[3]
        if op == "eq":
            return {field: v}
        if op == "ne":
            return {field: {"$nin": [v, None]}}
        return {field: {OPS[op]: v}}
    if kind == "contains":
        if cond[3]:
            return {field: {"$regex": regex_text(cond[2])}}
        return {field: {"$not": {"$regex": regex_text(cond[2])}, "$ne": None}}
    if kind == "null":
        return {field: None} if cond[2] else {field: {"$ne": None}}
    if kind == "in":
        return {field: {"$in": list(cond[2])}}
    return {field: {"$nin": list(cond[2])}}


# ------------------------------------------------------------ connection

class Connection:
    def __init__(self, url: str, password: str | None, timeout: float = 30.0):
        self.info = parse_url(url)
        self.database = self.info["database"]
        self.lock = threading.Lock()
        self.request = 0
        user = self.info["user"]
        if password is not None and not user:
            raise MongoError("the address names no user for the password of key:")
        last = None
        for host, port in self.info["hosts"]:
            try:
                self.open(host, port, timeout)
                hello = self.command({"hello": 1, "$db": "admin"})
                if not hello.get("isWritablePrimary", hello.get("ismaster", False)):
                    self.close()
                    last = MongoError(f"{host}:{port} is not the primary")
                    continue
                if hello.get("maxWireVersion", 0) < 6:
                    raise MongoError("the server is too old (MongoDB 3.6 or later speaks OP_MSG)")
                if password is not None:
                    self.login(user, password)
                return
            except MongoError as exc:
                if exc.code:
                    raise
                last = exc
        raise last or MongoError("the address names no host")

    def open(self, host: str, port: int, timeout: float) -> None:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise MongoError(f"cannot reach {host}:{port}: {exc.strerror or 'timed out'}") from None
        # a server on another machine is reached with TLS only (D171)
        if not local_host(host):
            context = ssl.create_default_context()
            try:
                sock = context.wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError:
                raise MongoError(f"TLS with {host}: the certificate is not trusted") from None
            except (ssl.SSLError, OSError):
                raise MongoError(f"TLS with {host}: the TLS handshake failed") from None
        self.sock = sock
        self.buf = b""

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except OSError as exc:
                raise MongoError(f"connection lost: {exc}") from None
            if not chunk:
                raise MongoError("the server closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def command(self, doc: dict) -> dict:
        """One command and its answer; an error when it is not ok."""
        self.request += 1
        body = struct.pack("<I", 0) + b"\0" + encode(doc)
        header = struct.pack("<iiii", 16 + len(body), self.request, 0, OP_MSG)
        try:
            self.sock.sendall(header + body)
        except OSError as exc:
            raise MongoError(f"connection lost: {exc}") from None
        size, _, _, op = struct.unpack("<iiii", self.read(16))
        data = self.read(size - 16)
        if op != OP_MSG:
            raise MongoError("the server answers in an unknown format")
        if data[4] != 0:
            raise MongoError("the server's answer is not valid")
        answer, _ = decode(data, 5)
        if answer.get("ok") != 1:
            raise MongoError(str(answer.get("errmsg", "error")), int(answer.get("code", 0) or 0) or -1)
        return answer

    def login(self, user: str, password: str) -> None:
        scram = Scram(password, user)
        first = self.command({"saslStart": 1, "mechanism": "SCRAM-SHA-256", "payload": scram.first().encode(),
                              "autoAuthorize": 1, "options": {"skipEmptyExchange": True}, "$db": "admin"})
        try:
            final = scram.final(bytes(first["payload"]).decode())
        except Exception:
            raise MongoError("the server's SCRAM answer is not valid") from None
        answer = self.command({"saslContinue": 1, "conversationId": first["conversationId"],
                               "payload": final.encode(), "$db": "admin"})
        try:
            scram.verify(bytes(answer["payload"]).decode())
        except Exception:
            raise MongoError("the server's SCRAM signature is not valid") from None
        while not answer.get("done"):
            answer = self.command({"saslContinue": 1, "conversationId": first["conversationId"], "payload": b"",
                                   "$db": "admin"})

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Store:
    """The operations of docdb on MongoDB: a table is a collection."""
    name = "mongo"

    def __init__(self, url: str, password: str | None):
        self.conn = Connection(url, password)
        self.database = self.conn.database
        self.log = bool(os.environ.get("NEXO_SQL_LOG"))

    def run(self, doc: dict) -> dict:
        doc["$db"] = self.database
        if self.log:
            sys.stderr.write("mongo: " + json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n")
        with self.conn.lock:
            answer = self.conn.command(doc)
        errors = answer.get("writeErrors")
        if errors:
            e = errors[0]
            code = int(e.get("code", 0))
            message = str(e.get("errmsg", "write error"))
            m = re.search(r"index: (\S+)_unique dup key", message) if code == 11000 else None
            raise MongoError(message, code, m.group(1) if m else None)
        return answer

    @staticmethod
    def document(key: str, row: dict) -> dict:
        """A row as a document: its key as _id, none fields left out."""
        out = {"_id": row[key]}
        for k, v in row.items():
            if k != key and v is not None:
                out[k] = v
        return out

    @staticmethod
    def row(key: str, doc: dict) -> dict:
        out = {k: v for k, v in doc.items() if k != "_id"}
        out[key] = doc.get("_id")
        return out

    def ensure(self, table: str, key: str, columns: list, uniques: list) -> None:
        """A table's unique indexes: a none field is left out of a document,
        so the index holds only the documents that have it."""
        if uniques:
            self.run({"createIndexes": table, "indexes": [
                {"key": {c: 1}, "name": f"{c}_unique", "unique": True,
                 "partialFilterExpression": {c: {"$exists": True}}} for c in uniques]})

    def next_id(self, table: str) -> int:
        answer = self.run({"findAndModify": "nexo_counters", "query": {"_id": table}, "update": {"$inc": {"n": 1}},
                           "upsert": True, "new": True})
        return int(answer["value"]["n"])

    def insert(self, table: str, key: str, row: dict) -> None:
        self.run({"insert": table, "documents": [self.document(key, row)]})

    def replace(self, table: str, key: str, row: dict, upsert: bool = False) -> None:
        update = {"q": {"_id": row[key]}, "u": self.document(key, row)}
        if upsert:
            update["upsert"] = True
        self.run({"update": table, "updates": [update]})

    def delete(self, table: str, key: str, value) -> None:
        self.run({"delete": table, "deletes": [{"q": {"_id": value}, "limit": 1}]})

    def find(self, table: str, key: str, cond, sort: list, limit: int | None, fields: list | None) -> list:
        if limit == 0:
            return []
        order = {}
        for name, desc in sort:
            field = "_id" if name == key else name
            if field not in order:
                order[field] = -1 if desc else 1
        if "_id" not in order:
            order["_id"] = 1
        doc = {"find": table, "filter": filter_of(cond, key), "sort": order}
        if fields is not None:
            doc["projection"] = {("_id" if f == key else f): 1 for f in fields}
        if limit is not None:
            doc["limit"] = limit
        answer = self.run(doc)
        cursor = answer["cursor"]
        docs = list(cursor.get("firstBatch", []))
        while cursor.get("id"):
            cursor = self.run({"getMore": cursor["id"], "collection": table})["cursor"]
            docs += cursor.get("nextBatch", [])
        return [self.row(key, d) for d in docs]

    def count(self, table: str, key: str, cond) -> int:
        return int(self.run({"count": table, "query": filter_of(cond, key)})["n"])
