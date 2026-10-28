"""An Elasticsearch client for nexo.db (D172): its REST API over HTTP(S),
the query DSL, and the operations a document store of nexo.db needs
(docdb.py). A table is an index (named `<prefix>-<table>` when the
address has a path), a row a document whose id is its key. Elasticsearch
has no unique keys: a `@unique` value is a document of the index
`nexo_unique`, created only when it does not exist. Every write is
refreshed at once, so that a program reads what it wrote."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from .pg import local_host

RANGE = {"lt": "lt", "gt": "gt", "le": "lte", "ge": "gte"}
PAGE = 1000


class ElasticError(Exception):
    """An error the server reported or a failure to reach it; `duplicate`
    is the column of a broken unique key."""

    def __init__(self, message: str, duplicate: str | None = None):
        super().__init__(message)
        self.message, self.duplicate = message, duplicate


def parse_url(url: str) -> dict:
    """elasticsearch://user@host:port,host2:port/prefix (D172)."""
    rest = url.split("://", 1)[1]
    auth, _, where = rest.rpartition("@")
    if ":" in auth:
        raise ElasticError("the address holds a password; give it with key: instead")
    hosts_text, _, prefix = where.partition("/")
    hosts = []
    for part in hosts_text.split(","):
        host, port = part, 9200
        if part.startswith("["):
            host, _, after = part[1:].partition("]")
            if after.startswith(":"):
                port = int(after[1:])
        elif ":" in part:
            host, p = part.rsplit(":", 1)
            port = int(p)
        hosts.append((host or "localhost", port))
    return {"user": urllib.parse.unquote(auth), "hosts": hosts, "prefix": prefix.strip("/").lower()}


def compact(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def wildcard_text(text: str) -> str:
    """A text as a wildcard pattern that matches it anywhere."""
    return "*" + "".join("\\" + c if c in "\\*?" else c for c in text) + "*"


def query_of(cond):
    """A condition of docdb in the query DSL."""
    if cond is None:
        return {"match_all": {}}
    kind = cond[0]
    if kind == "and":
        return {"bool": {"filter": [query_of(c) for c in cond[1]]}}
    if kind == "or":
        return {"bool": {"should": [query_of(c) for c in cond[1]], "minimum_should_match": 1}}
    field = cond[1]
    exists = {"exists": {"field": field}}
    if kind == "cmp":
        op, v = cond[2], cond[3]
        if op == "eq":
            return {"term": {field: v}}
        if op == "ne":
            return {"bool": {"filter": [exists], "must_not": [{"term": {field: v}}]}}
        return {"range": {field: {RANGE[op]: v}}}
    if kind == "contains":
        pattern = {"wildcard": {field: {"value": wildcard_text(cond[2])}}}
        return pattern if cond[3] else {"bool": {"filter": [exists], "must_not": [pattern]}}
    if kind == "null":
        return {"bool": {"must_not": [exists]}} if cond[2] else exists
    terms = {"terms": {field: list(cond[2])}}
    return terms if kind == "in" else {"bool": {"must_not": [terms]}}


class Store:
    """The operations of docdb on Elasticsearch."""
    name = "elastic"

    def __init__(self, url: str, password: str | None):
        info = parse_url(url)
        self.hosts = info["hosts"]
        self.prefix = info["prefix"]
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if password is not None:
            if info["user"]:
                pair = f"{info['user']}:{password}".encode()
                self.headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
            else:
                self.headers["Authorization"] = "ApiKey " + password
        self.uniques: dict = {}
        self.log = bool(os.environ.get("NEXO_SQL_LOG"))
        # the address answers, and the password is right
        self.call("GET", "/")

    def index(self, table: str) -> str:
        return f"{self.prefix}-{table}" if self.prefix else table

    def call(self, method: str, path: str, body=None, ok=()) -> tuple[int, dict]:
        """(status, answer) of a request; an error for a status of 400 or
        more not in `ok`."""
        data = None if body is None else compact(body).encode("utf-8")
        if self.log:
            sys.stderr.write(f"elastic: {method} {path}" + ("" if body is None else " " + compact(body)) + "\n")
        last = None
        for host, port in self.hosts:
            # a server on another machine is reached with TLS only (D172)
            scheme = "http" if local_host(host) else "https"
            shown = f"[{host}]" if ":" in host else host
            req = urllib.request.Request(f"{scheme}://{shown}:{port}{path}", data=data, method=method)
            for k, v in self.headers.items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    answer = json.loads(raw or b"{}")
                except ValueError:
                    answer = {"error": {"reason": raw.decode("utf-8", "replace")}}
                if e.code in ok:
                    return e.code, answer
                raise ElasticError(self.reason(e.code, answer)) from None
            except (urllib.error.URLError, OSError) as e:
                from .http import network_reason
                last = ElasticError(f"cannot reach {host}:{port}: {network_reason(e)}")
        raise last

    @staticmethod
    def reason(status: int, answer: dict) -> str:
        err = answer.get("error")
        if isinstance(err, dict):
            text = err.get("reason") or err.get("type") or "error"
        else:
            text = str(err or answer.get("message") or "error")
        if status == 401:
            return f"the server refuses the password (401): {text}"
        return f"{status}: {text}"

    @staticmethod
    def document(row: dict) -> dict:
        return {k: v for k, v in row.items() if v is not None}

    # --------------------------------------------------------- tables
    def ensure(self, table: str, key: str, fields: list, uniques: list) -> None:
        properties = {name: {"type": kind} for name, kind in fields}
        status, answer = self.call("PUT", "/" + self.index(table),
                                   {"mappings": {"dynamic": "strict", "properties": properties}}, ok=(400,))
        if status == 400:
            if answer.get("error", {}).get("type") != "resource_already_exists_exception":
                raise ElasticError(self.reason(status, answer))
            _, have = self.call("GET", f"/{self.index(table)}/_mapping")
            known = next(iter(have.values()), {}).get("mappings", {}).get("properties", {})
            missing = {k: v for k, v in properties.items() if k not in known}
            if missing:
                self.call("PUT", f"/{self.index(table)}/_mapping", {"properties": missing})
        self.uniques[table] = list(uniques)
        if uniques:
            self.call("PUT", "/" + self.index("nexo_unique"), {"mappings": {"dynamic": "strict", "properties": {
                "table": {"type": "keyword"}, "column": {"type": "keyword"}}}}, ok=(400,))

    def next_id(self, table: str) -> int:
        _, answer = self.call("POST", f"/{self.index('nexo_counters')}/_update/{urllib.parse.quote(table, safe='')}"
                                      "?refresh=true&retry_on_conflict=10",
                              {"script": {"source": "ctx._source.n += 1", "lang": "painless"},
                               "upsert": {"n": 1}, "_source": True})
        return int(answer["get"]["_source"]["n"])

    # ------------------------------------------------------ unique keys
    def unique_id(self, table: str, column: str, value) -> str:
        return hashlib.sha256(f"{table}\0{column}\0{compact(value)}".encode("utf-8")).hexdigest()

    def claim(self, table: str, row: dict, old: dict | None) -> list:
        """Create the unique keys of a row's new values; the ids created. A
        value another row holds is an error that names its column."""
        made = []
        for column in self.uniques.get(table, []):
            v = row.get(column)
            if v is None or (old is not None and old.get(column) == v):
                continue
            uid = self.unique_id(table, column, v)
            status, _ = self.call("PUT", f"/{self.index('nexo_unique')}/_create/{uid}?refresh=true",
                                  {"table": table, "column": column}, ok=(409,))
            if status == 409:
                self.release(made)
                raise ElasticError(f"the value of {column} is already in {table}", duplicate=column)
            made.append(uid)
        return made

    def release(self, ids: list) -> None:
        for uid in ids:
            self.call("DELETE", f"/{self.index('nexo_unique')}/_doc/{uid}?refresh=true", ok=(404,))

    def freed(self, table: str, old: dict, row: dict | None) -> list:
        """The unique keys of an old row's values a new row no longer has."""
        out = []
        for column in self.uniques.get(table, []):
            v = old.get(column)
            if v is not None and (row is None or row.get(column) != v):
                out.append(self.unique_id(table, column, v))
        return out

    # ---------------------------------------------------------- writes
    def doc_path(self, table: str, value) -> str:
        return f"/{self.index(table)}/_doc/{urllib.parse.quote(str(value), safe='')}"

    def get(self, table: str, value) -> dict | None:
        status, answer = self.call("GET", self.doc_path(table, value), ok=(404,))
        return answer.get("_source") if status == 200 and answer.get("found") else None

    def insert(self, table: str, key: str, row: dict) -> None:
        made = self.claim(table, row, None)
        path = f"/{self.index(table)}/_create/{urllib.parse.quote(str(row[key]), safe='')}?refresh=true"
        status, _ = self.call("PUT", path, self.document(row), ok=(409,))
        if status == 409:
            self.release(made)
            raise ElasticError(f"the value of {key} is already in {table}", duplicate=key)

    def replace(self, table: str, key: str, row: dict, upsert: bool = False) -> None:
        old = self.get(table, row[key])
        if old is None and not upsert:
            return
        made = self.claim(table, row, old)
        try:
            self.call("PUT", self.doc_path(table, row[key]) + "?refresh=true", self.document(row))
        except ElasticError:
            self.release(made)
            raise
        if old is not None:
            self.release(self.freed(table, old, row))

    def delete(self, table: str, key: str, value) -> None:
        old = self.get(table, value)
        if old is None:
            return
        self.call("DELETE", self.doc_path(table, value) + "?refresh=true", ok=(404,))
        self.release(self.freed(table, old, None))

    # --------------------------------------------------------- queries
    def find(self, table: str, key: str, cond, sort: list, limit: int | None, fields: list | None) -> list:
        if limit == 0:
            return []
        order, seen = [], set()
        for name, desc in sort:
            if name not in seen:
                seen.add(name)
                order.append({name: {"order": "desc" if desc else "asc", "missing": "_last" if desc else "_first"}})
        if key not in seen:
            order.append({key: {"order": "asc"}})
        body = {"query": query_of(cond), "sort": order, "track_total_hits": False}
        if fields is not None:
            body["_source"] = list(dict.fromkeys(list(fields) + [key]))
        rows: list = []
        after = None
        while True:
            size = PAGE if limit is None else min(PAGE, limit - len(rows))
            page = dict(body, size=size)
            if after is not None:
                page["search_after"] = after
            _, answer = self.call("POST", f"/{self.index(table)}/_search", page)
            hits = answer.get("hits", {}).get("hits", [])
            rows += [h.get("_source", {}) for h in hits]
            if len(hits) < size or (limit is not None and len(rows) >= limit):
                return rows
            after = hits[-1]["sort"]

    def count(self, table: str, key: str, cond) -> int:
        _, answer = self.call("POST", f"/{self.index(table)}/_count", {"query": query_of(cond)})
        return int(answer.get("count", 0))
