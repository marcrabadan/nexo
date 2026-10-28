"""A one-node MongoDB for the tests of nexo.db (D171), written from the
wire protocol's and the commands' documentation on its own (it shares no
code with nexo/stdlib/mongo.py): OP_MSG, BSON, SCRAM-SHA-256, and hello,
insert, update, delete, find with getMore, count, findAndModify and
createIndexes, with filters of $and $or $eq $ne $in $nin $lt $lte $gt $gte
$exists $regex $not, sorts in MongoDB's order of types, and unique indexes
with a partial filter.

    server = start(users={"shop": "pw"})
    server.port, server.data["shop"]["customer"]   # [document]
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import socketserver
import struct
import threading

MISSING = object()


# ------------------------------------------------------------------ BSON

def to_bson(doc: dict) -> bytes:
    body = b""
    for name, v in doc.items():
        key = name.encode() + b"\0"
        if v is None:
            body += b"\x0a" + key
        elif isinstance(v, bool):
            body += b"\x08" + key + bytes([int(v)])
        elif isinstance(v, int):
            # counts and codes as int32, as MongoDB sends them
            small = -2**31 <= v < 2**31 and name in ("n", "code", "conversationId", "maxWireVersion")
            body += (b"\x10" + key + struct.pack("<i", v)) if small else (b"\x12" + key + struct.pack("<q", v))
        elif isinstance(v, float):
            body += b"\x01" + key + struct.pack("<d", v)
        elif isinstance(v, str):
            raw = v.encode()
            body += b"\x02" + key + struct.pack("<i", len(raw) + 1) + raw + b"\0"
        elif isinstance(v, bytes):
            body += b"\x05" + key + struct.pack("<i", len(v)) + b"\0" + v
        elif isinstance(v, dict):
            body += b"\x03" + key + to_bson(v)
        elif isinstance(v, list):
            body += b"\x04" + key + to_bson({str(i): x for i, x in enumerate(v)})
        else:
            raise TypeError(type(v))
    return struct.pack("<i", len(body) + 5) + body + b"\0"


def from_bson(data: bytes, p: int = 0, array: bool = False):
    size = struct.unpack_from("<i", data, p)[0]
    end, p = p + size - 1, p + 4
    out = {}
    while p < end:
        t = data[p]
        z = data.index(b"\0", p + 1)
        name = data[p + 1:z].decode()
        p = z + 1
        if t == 0x01:
            out[name] = struct.unpack_from("<d", data, p)[0]
            p += 8
        elif t == 0x02:
            n = struct.unpack_from("<i", data, p)[0]
            out[name] = data[p + 4:p + 3 + n].decode()
            p += 4 + n
        elif t in (0x03, 0x04):
            n = struct.unpack_from("<i", data, p)[0]
            out[name] = from_bson(data, p, t == 0x04)
            p += n
        elif t == 0x05:
            n = struct.unpack_from("<i", data, p)[0]
            out[name] = data[p + 5:p + 5 + n]
            p += 5 + n
        elif t == 0x08:
            out[name] = data[p] == 1
            p += 1
        elif t == 0x0A:
            out[name] = None
        elif t == 0x10:
            out[name] = struct.unpack_from("<i", data, p)[0]
            p += 4
        elif t == 0x12:
            out[name] = struct.unpack_from("<q", data, p)[0]
            p += 8
        else:
            raise ValueError(f"BSON type {t}")
    return list(out.values()) if array else out


# ------------------------------------------------------------- queries

def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def rank(v) -> int:
    """MongoDB's order of types: null (and missing), numbers, strings."""
    if v is MISSING or v is None:
        return 0
    if is_number(v):
        return 1
    if isinstance(v, str):
        return 2
    return 3


def same(a, b) -> bool:
    if a is MISSING:
        a = None
    return rank(a) == rank(b) and a == b


def compare(v, op, x) -> bool:
    if v is MISSING or v is None or rank(v) != rank(x):
        return False
    return {"$lt": v < x, "$lte": v <= x, "$gt": v > x, "$gte": v >= x}[op]


def field_matches(v, cond) -> bool:
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, x in cond.items():
            if op == "$eq" and not same(v, x):
                return False
            if op == "$ne" and same(v, x):
                return False
            if op == "$in" and not any(same(v, y) for y in x):
                return False
            if op == "$nin" and any(same(v, y) for y in x):
                return False
            if op in ("$lt", "$lte", "$gt", "$gte") and not compare(v, op, x):
                return False
            if op == "$exists" and (v is not MISSING) != bool(x):
                return False
            if op == "$regex" and not (isinstance(v, str) and re.search(x, v)):
                return False
            if op == "$not" and field_matches(v, x):
                return False
        return True
    return same(v, cond)


def matches(doc: dict, query: dict) -> bool:
    for key, cond in query.items():
        if key == "$and":
            if not all(matches(doc, q) for q in cond):
                return False
        elif key == "$or":
            if not any(matches(doc, q) for q in cond):
                return False
        elif not field_matches(doc.get(key, MISSING), cond):
            return False
    return True


def sort_key(doc: dict, order: dict):
    class Key:
        def __init__(self, d):
            self.d = d

        def __lt__(self, other):
            for f, direction in order.items():
                a, b = self.d.get(f, MISSING), other.d.get(f, MISSING)
                ka = (rank(a), None if rank(a) == 0 else a)
                kb = (rank(b), None if rank(b) == 0 else b)
                if ka == kb:
                    continue
                less = ka[0] < kb[0] or (ka[0] == kb[0] and ka[1] < kb[1])
                return less if direction > 0 else not less
            return False
    return Key(doc)


# ------------------------------------------------------------------ SCRAM

def scram_keys(password: str, salt: bytes, rounds: int = 4096):
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    return hashlib.sha256(client_key).digest(), hmac.new(salted, b"Server Key", hashlib.sha256).digest()


class Server:
    def __init__(self, users):
        self.users = users
        self.data: dict = {}  # database -> collection -> [document]
        self.indexes: dict = {}  # (database, collection) -> [(name, field)]
        self.lock = threading.Lock()
        self.cursors: dict = {}
        self.next_cursor = 1000
        self.commands: list = []

    def coll(self, db: str, name: str) -> list:
        return self.data.setdefault(db, {}).setdefault(name, [])

    def duplicate(self, db: str, name: str, doc: dict, ignore=None):
        """The error of a document that breaks a unique index, or None."""
        indexes = [("_id_", "_id")] + self.indexes.get((db, name), [])
        for index, field in indexes:
            if field not in doc:
                continue
            for other in self.coll(db, name):
                if other is not ignore and field in other and same(other[field], doc[field]):
                    return {"index": 0, "code": 11000,
                            "errmsg": f"E11000 duplicate key error collection: {db}.{name} index: {index} "
                                      f"dup key: {{ {field}: {doc[field]!r} }}"}
        return None

    def run(self, cmd: dict, conn: dict) -> dict:
        name = next(iter(cmd))
        db = cmd.get("$db", "test")
        self.commands.append(name)
        if name in ("hello", "isMaster", "ismaster"):
            return {"isWritablePrimary": True, "ismaster": True, "maxWireVersion": 17, "ok": 1.0}
        if name == "saslStart":
            user_part = bytes(cmd["payload"]).decode()
            bare = user_part.split(",", 2)[2]
            fields = dict(p.split("=", 1) for p in bare.split(","))
            user = fields["n"].replace("=2C", ",").replace("=3D", "=")
            salt = b"nexo-salt-" + user.encode()
            snonce = fields["r"] + base64.b64encode(os.urandom(12)).decode()
            first = f"r={snonce},s={base64.b64encode(salt).decode()},i=4096"
            conn["sasl"] = (user, bare, first, snonce)
            return {"conversationId": 1, "done": False, "payload": first.encode(), "ok": 1.0}
        if name == "saslContinue":
            if "sasl" not in conn:
                return {"ok": 0.0, "errmsg": "No SASL session state found", "code": 17}
            if conn.get("authed"):
                return {"conversationId": 1, "done": True, "payload": b"", "ok": 1.0}
            user, bare, first, snonce = conn["sasl"]
            final = bytes(cmd["payload"]).decode()
            without, _, proof = final.rpartition(",p=")
            password = self.users.get(user)
            if password is None or f"r={snonce}" not in without:
                return {"ok": 0.0, "errmsg": "Authentication failed.", "code": 18}
            stored, server_key = scram_keys(password, b"nexo-salt-" + user.encode())
            auth = f"{bare},{first},{without}".encode()
            signature = hmac.new(stored, auth, hashlib.sha256).digest()
            client_key = bytes(a ^ b for a, b in zip(base64.b64decode(proof), signature))
            if hashlib.sha256(client_key).digest() != stored:
                return {"ok": 0.0, "errmsg": "Authentication failed.", "code": 18}
            conn["authed"] = True
            verifier = base64.b64encode(hmac.new(server_key, auth, hashlib.sha256).digest()).decode()
            return {"conversationId": 1, "done": True, "payload": f"v={verifier}".encode(), "ok": 1.0}
        if self.users and not conn.get("authed"):
            return {"ok": 0.0, "errmsg": f"command {name} requires authentication", "code": 13}
        with self.lock:
            return self.data_command(name, cmd, db)

    def data_command(self, name: str, cmd: dict, db: str) -> dict:
        if name == "insert":
            coll = self.coll(db, cmd["insert"])
            for i, doc in enumerate(cmd["documents"]):
                err = self.duplicate(db, cmd["insert"], doc)
                if err:
                    err["index"] = i
                    return {"n": i, "writeErrors": [err], "ok": 1.0}
                coll.append(dict(doc))
            return {"n": len(cmd["documents"]), "ok": 1.0}
        if name == "update":
            coll = self.coll(db, cmd["update"])
            n = 0
            for u in cmd["updates"]:
                found = next((d for d in coll if matches(d, u["q"])), None)
                new = dict(u["u"])
                if found is None:
                    if u.get("upsert"):
                        err = self.duplicate(db, cmd["update"], new)
                        if err:
                            return {"n": n, "writeErrors": [err], "ok": 1.0}
                        coll.append(new)
                        n += 1
                    continue
                new.setdefault("_id", found["_id"])
                err = self.duplicate(db, cmd["update"], new, ignore=found)
                if err:
                    return {"n": n, "writeErrors": [err], "ok": 1.0}
                coll[coll.index(found)] = new
                n += 1
            return {"n": n, "nModified": n, "ok": 1.0}
        if name == "delete":
            coll = self.coll(db, cmd["delete"])
            n = 0
            for d in cmd["deletes"]:
                for doc in list(coll):
                    if matches(doc, d["q"]):
                        coll.remove(doc)
                        n += 1
                        if d.get("limit"):
                            break
            return {"n": n, "ok": 1.0}
        if name == "find":
            docs = [d for d in self.coll(db, cmd["find"]) if matches(d, cmd.get("filter", {}))]
            order = cmd.get("sort") or {"_id": 1}
            docs.sort(key=lambda d: sort_key(d, order))
            if cmd.get("limit"):
                docs = docs[:cmd["limit"]]
            if cmd.get("projection"):
                keep = set(cmd["projection"]) | {"_id"}
                docs = [{k: v for k, v in d.items() if k in keep} for d in docs]
            # a small first batch, so that the client asks for the rest
            first, rest = docs[:2], docs[2:]
            cursor = 0
            if rest:
                self.next_cursor += 1
                cursor = self.next_cursor
                self.cursors[cursor] = rest
            return {"cursor": {"firstBatch": first, "id": cursor, "ns": f"{db}.{cmd['find']}"}, "ok": 1.0}
        if name == "getMore":
            rest = self.cursors.pop(cmd["getMore"], [])
            batch, rest = rest[:3], rest[3:]
            cursor = 0
            if rest:
                cursor = cmd["getMore"]
                self.cursors[cursor] = rest
            return {"cursor": {"nextBatch": batch, "id": cursor, "ns": f"{db}.{cmd['collection']}"}, "ok": 1.0}
        if name == "count":
            return {"n": sum(1 for d in self.coll(db, cmd["count"]) if matches(d, cmd.get("query", {}))), "ok": 1.0}
        if name == "findAndModify":
            coll = self.coll(db, cmd["findAndModify"])
            found = next((d for d in coll if matches(d, cmd["query"])), None)
            if found is None:
                found = dict(cmd["query"])
                coll.append(found)
            for f, by in cmd["update"].get("$inc", {}).items():
                found[f] = found.get(f, 0) + by
            return {"lastErrorObject": {"n": 1}, "value": dict(found), "ok": 1.0}
        if name == "createIndexes":
            have = self.indexes.setdefault((db, cmd["createIndexes"]), [])
            for ix in cmd["indexes"]:
                field = next(iter(ix["key"]))
                if ix.get("unique") and (ix["name"], field) not in have:
                    have.append((ix["name"], field))
            return {"ok": 1.0}
        return {"ok": 0.0, "errmsg": f"no such command: '{name}'", "code": 59}


def start(users=None) -> Server:
    state = Server(users or {})

    class Handler(socketserver.BaseRequestHandler):
        def recv(self, n):
            out = b""
            while len(out) < n:
                chunk = self.request.recv(n - len(out))
                if not chunk:
                    raise ConnectionError
                out += chunk
            return out

        def handle(self):
            conn: dict = {}
            try:
                while True:
                    size, request, _, op = struct.unpack("<iiii", self.recv(16))
                    body = self.recv(size - 16)
                    assert op == 2013, op
                    assert body[4] == 0
                    cmd = from_bson(body, 5)
                    answer = to_bson(state.run(cmd, conn))
                    reply = struct.pack("<I", 0) + b"\0" + answer
                    self.request.sendall(struct.pack("<iiii", 16 + len(reply), request + 1000000, request, 2013) + reply)
            except (ConnectionError, OSError):
                return

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.port = server.server_address[1]
    state.server = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return state
