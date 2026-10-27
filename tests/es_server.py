"""An Elasticsearch for the tests of nexo.db (D172), written from its REST
API's and query DSL's documentation on its own (it shares no code with
nexo/stdlib/elastic.py): indices with strict mappings, documents (_doc,
_create, _update with the counter script and an upsert), _search with
bool, term, terms, range, exists, wildcard and match_all, sorts with
missing values first or last, search_after, _source filtering, _count,
and Basic or ApiKey authorization.

    server = start(users={"elastic": "pw"})
    server.port, server.indices["customer"]["docs"]   # {id: source}
"""
from __future__ import annotations

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

MISSING = object()


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def field(doc: dict, name: str):
    return doc.get(name, MISSING) if doc.get(name) is not None else MISSING


def matches(doc: dict, q: dict) -> bool:
    (kind, body), = q.items()
    if kind == "match_all":
        return True
    if kind == "bool":
        if not all(matches(doc, c) for c in body.get("filter", []) + body.get("must", [])):
            return False
        if any(matches(doc, c) for c in body.get("must_not", [])):
            return False
        should = body.get("should", [])
        if should:
            need = body.get("minimum_should_match", 1)
            if sum(1 for c in should if matches(doc, c)) < need:
                return False
        return True
    if kind == "exists":
        return field(doc, body["field"]) is not MISSING
    (name, spec), = body.items()
    v = field(doc, name)
    if v is MISSING:
        return False
    if kind == "term":
        x = spec["value"] if isinstance(spec, dict) else spec
        return v == x and isinstance(v, str) == isinstance(x, str)
    if kind == "terms":
        return any(v == x and isinstance(v, str) == isinstance(x, str) for x in spec)
    if kind == "range":
        for op, x in spec.items():
            if isinstance(v, str) != isinstance(x, str):
                return False
            if not {"lt": v < x, "lte": v <= x, "gt": v > x, "gte": v >= x}[op]:
                return False
        return True
    if kind == "wildcard":
        pattern = spec["value"] if isinstance(spec, dict) else spec
        # \* \? \\ are the characters themselves
        regex = ""
        i = 0
        while i < len(pattern):
            c = pattern[i]
            if c == "\\" and i + 1 < len(pattern):
                regex += re.escape(pattern[i + 1])
                i += 2
                continue
            regex += ".*" if c == "*" else "." if c == "?" else re.escape(c)
            i += 1
        return isinstance(v, str) and re.fullmatch(regex, v, re.S) is not None
    raise ValueError(f"query {kind}")


def sorted_hits(docs: list, sort: list) -> list:
    """(doc id, source, sort values) in the order of `sort`."""
    keys = [next(iter(s.items())) if isinstance(s, dict) else (s, {}) for s in sort]

    def value(src, name, spec):
        v = field(src, name)
        return None if v is MISSING else v

    rows = [(i, src, [value(src, n, s) for n, s in keys]) for i, src in docs]

    class K:
        def __init__(self, row):
            self.row = row

        def __lt__(self, other):
            for (name, spec), a, b in zip(keys, self.row[2], other.row[2]):
                if a == b:
                    continue
                desc = spec.get("order") == "desc"
                missing_first = spec.get("missing", "_last" if not desc else "_first") == "_first"
                if a is None or b is None:
                    return (a is None) == missing_first
                return (a > b) if desc else (a < b)
            return False
    return sorted(rows, key=K)


class Server:
    def __init__(self, users):
        self.users = users
        self.indices: dict = {}  # name -> {"mappings": {...}, "docs": {id: source}}
        self.lock = threading.Lock()
        self.requests: list = []

    def authorized(self, header: str | None) -> bool:
        if not self.users:
            return True
        if not header:
            return False
        kind, _, value = header.partition(" ")
        if kind == "Basic":
            user, _, pw = base64.b64decode(value).decode().partition(":")
            return self.users.get(user) == pw
        if kind == "ApiKey":
            return value in self.users.values()
        return False

    def check(self, index: str, src: dict):
        props = self.indices[index]["mappings"].get("properties", {})
        strict = self.indices[index]["mappings"].get("dynamic") == "strict"
        for k, v in src.items():
            if k not in props:
                if strict:
                    return {"error": {"type": "strict_dynamic_mapping_exception",
                                      "reason": f"mapping set to strict, dynamic introduction of [{k}] within [_doc] "
                                                "is not allowed"}, "status": 400}
                continue
            t = props[k]["type"]
            ok = (t == "keyword" and isinstance(v, str)) or (t == "long" and is_number(v) and v == int(v)) \
                or (t == "double" and is_number(v))
            if not ok:
                return {"error": {"type": "document_parsing_exception",
                                  "reason": f"failed to parse field [{k}] of type [{t}]"}, "status": 400}
        return None

    def handle(self, method: str, path: str, query: dict, body):
        parts = [unquote(p) for p in path.strip("/").split("/") if p]
        if not parts:
            return 200, {"name": "nexo-test", "version": {"number": "8.15.0"}, "tagline": "You Know, for Search"}
        index = parts[0]
        missing = (404, {"error": {"type": "index_not_found_exception", "reason": f"no such index [{index}]"},
                         "status": 404})
        if len(parts) == 1:
            if method == "PUT":
                if index in self.indices:
                    return 400, {"error": {"type": "resource_already_exists_exception",
                                           "reason": f"index [{index}] already exists"}, "status": 400}
                self.indices[index] = {"mappings": (body or {}).get("mappings", {}), "docs": {}}
                return 200, {"acknowledged": True, "index": index}
            return (200, {"acknowledged": True}) if index in self.indices else missing
        what = parts[1]
        if what == "_mapping":
            if index not in self.indices:
                return missing
            if method == "GET":
                return 200, {index: {"mappings": self.indices[index]["mappings"]}}
            self.indices[index]["mappings"].setdefault("properties", {}).update(body.get("properties", {}))
            return 200, {"acknowledged": True}
        if what in ("_doc", "_create") and len(parts) == 3:
            doc_id = parts[2]
            if method == "GET":
                if index not in self.indices:
                    return missing
                src = self.indices[index]["docs"].get(doc_id)
                if src is None:
                    return 404, {"_index": index, "_id": doc_id, "found": False}
                return 200, {"_index": index, "_id": doc_id, "found": True, "_source": src}
            if method == "DELETE":
                if index not in self.indices or doc_id not in self.indices[index]["docs"]:
                    return 404, {"_index": index, "_id": doc_id, "result": "not_found"}
                del self.indices[index]["docs"][doc_id]
                return 200, {"_index": index, "_id": doc_id, "result": "deleted"}
            if index not in self.indices:
                self.indices[index] = {"mappings": {}, "docs": {}}
            docs = self.indices[index]["docs"]
            if (what == "_create" or query.get("op_type") == "create") and doc_id in docs:
                return 409, {"error": {"type": "version_conflict_engine_exception",
                                       "reason": f"[{doc_id}]: version conflict, document already exists"},
                             "status": 409}
            bad = self.check(index, body)
            if bad:
                return 400, bad
            created = doc_id not in docs
            docs[doc_id] = body
            return (201 if created else 200), {"_index": index, "_id": doc_id,
                                               "result": "created" if created else "updated"}
        if what == "_update":
            doc_id = parts[2]
            if index not in self.indices:
                self.indices[index] = {"mappings": {}, "docs": {}}
            docs = self.indices[index]["docs"]
            if doc_id not in docs:
                docs[doc_id] = dict(body["upsert"])
            else:
                script = body["script"]["source"]
                m = re.fullmatch(r"ctx\._source\.(\w+) \+= (\d+)", script)
                assert m, script
                docs[doc_id][m.group(1)] += int(m.group(2))
            answer = {"_index": index, "_id": doc_id, "result": "updated"}
            if body.get("_source"):
                answer["get"] = {"found": True, "_source": dict(docs[doc_id])}
            return 200, answer
        if what in ("_search", "_count"):
            if index not in self.indices:
                return missing
            q = (body or {}).get("query", {"match_all": {}})
            docs = [(i, s) for i, s in self.indices[index]["docs"].items() if matches(s, q)]
            if what == "_count":
                return 200, {"count": len(docs)}
            rows = sorted_hits(docs, body.get("sort", []))
            after = body.get("search_after")
            if after is not None:
                rows = [r for r in rows if self.after(r[2], after, body.get("sort", []))]
            rows = rows[:body.get("size", 10)]
            fields = body.get("_source")
            hits = []
            for i, src, sort_values in rows:
                if isinstance(fields, list):
                    src = {k: v for k, v in src.items() if k in fields}
                hits.append({"_index": index, "_id": i, "_source": src, "sort": sort_values})
            return 200, {"took": 1, "timed_out": False, "hits": {"hits": hits}}
        return 400, {"error": {"type": "illegal_argument_exception", "reason": f"unknown request {method} {path}"},
                     "status": 400}

    @staticmethod
    def after(values: list, pivot: list, sort: list) -> bool:
        """Whether a hit comes after the one whose sort values are `pivot`."""
        keys = [next(iter(s.items())) if isinstance(s, dict) else (s, {}) for s in sort]
        for (name, spec), a, b in zip(keys, values, pivot):
            if a == b:
                continue
            desc = spec.get("order") == "desc"
            missing_first = spec.get("missing", "_last" if not desc else "_first") == "_first"
            if a is None or b is None:
                return (b is None) == missing_first
            return (a < b) if desc else (a > b)
        return False


def start(users=None) -> Server:
    state = Server(users or {})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def serve(self, method):
            u = urlsplit(self.path)
            query = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            state.requests.append((method, u.path))
            if not state.authorized(self.headers.get("Authorization")):
                status, answer = 401, {"error": {"type": "security_exception",
                                                 "reason": "unable to authenticate user"}, "status": 401}
            else:
                with state.lock:
                    status, answer = state.handle(method, u.path, query, body)
            data = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.serve("GET")

        def do_PUT(self):
            self.serve("PUT")

        def do_POST(self):
            self.serve("POST")

        def do_DELETE(self):
            self.serve("DELETE")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.port = server.server_address[1]
    state.server = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return state

