"""nexo.db on document stores (D171-D172): MongoDB (mongo.py) and
Elasticsearch (elastic.py). A table is a collection (an index), a row a
document; the same `@table` types, queries, relations, transactions and
privacy as on SQL databases, with the same results.

The conditions of a pipeline are where_tree's (db.py), put in negation
normal form so that each leaf leaves out the rows whose column is none,
as SQL's three-valued logic does: a store needs no NULL of its own. Its
leaves:
    ("cmp", column, op, value)   op: eq ne lt gt le ge (none excluded)
    ("contains", column, text, positive)
    ("null", column, is_null)
    ("in", column, values) / ("nin", column, values)
and ("and", [...]) / ("or", [...]).

A transaction keeps a journal of what it wrote and, when it fails, writes
it back (D171): other programs see its writes before it ends."""
from __future__ import annotations

import time as _time

from ..errors import NexoError
from ..types import Record, failure, repr_value
from .db import Column, Database, TableInfo, Transaction, interp_tables, personal_table, where_tree

PRIVACY = "nexo_privacy"
INVERSE = {"eq": "ne", "ne": "eq", "lt": "ge", "ge": "lt", "gt": "le", "le": "gt"}
SQL_OPS = {"=": "eq", "<>": "ne", "<": "lt", ">": "gt", "<=": "le", ">=": "ge"}
# the key of a table without `id: id` (D171)
HIDDEN_KEY = "nexo_row"


def nnf(node, neg: bool = False):
    """A where tree in negation normal form, or None when a store cannot
    run it (a column of the table a ref points to, two columns compared)."""
    kind = node[0]
    if kind == "not":
        return nnf(node[1], not neg)
    if kind in ("and", "or"):
        a, b = nnf(node[1], neg), nnf(node[2], neg)
        if a is None or b is None:
            return None
        joined = {"and": "or", "or": "and"}[kind] if neg else kind
        items = []
        for x in (a, b):
            items += x[1] if x[0] == joined else [x]
        return (joined, items)
    if kind == "colcol" or node[-1] is not None:
        return None
    if kind == "truth":
        return ("cmp", node[1].name, "ne" if neg else "eq", 1)
    if kind == "contains":
        return ("contains", node[1].name, node[2], not neg)
    if kind == "null":
        return ("null", node[1].name, node[2] != neg)
    op = SQL_OPS[node[2]]
    return ("cmp", node[1].name, INVERSE[op] if neg else op, node[3])


def store_type(info: TableInfo, c: Column) -> str:
    """The type of a field in a store that asks for one (Elasticsearch)."""
    if c.field == info.key:
        return "long"
    if c.private:
        return "keyword"
    return {"int": "long", "ref": "long", "bool": "long", "float": "double"}.get(c.kind, "keyword")


def all_of(conds: list):
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else ("and", conds)


class DocDatabase(Database):
    document = True

    def __init__(self, path: str, store, error):
        super().__init__(path, None, store.name)
        self.store = store
        self.error = error
        # (kind, table, key, value or row) to write back on a rollback
        self.journal: list = []
        self.marks: list = []

    @staticmethod
    def key_of(info: TableInfo) -> str:
        return info.key or HIDDEN_KEY

    def fail(self, call, exc, info: TableInfo | None = None, values: dict | None = None):
        dup = getattr(exc, "duplicate", None)
        if dup and info is not None:
            col = next((c for c in info.columns if c.name == dup), None)
            field = col.field if col else dup
            return failure("DatabaseError", call.span, key="DatabaseError.unique", type=info.rtype.name, field=field,
                           value=repr_value((values or {}).get(field)))
        return failure("DatabaseError", call.span, db=self.path, reason=str(exc))

    def call(self, call, what, info: TableInfo | None = None, values: dict | None = None):
        try:
            return what()
        except self.error as exc:
            raise self.fail(call, exc, info, values) from None

    # ------------------------------------------------------- tables
    def sync(self, call, info: TableInfo) -> None:
        call.interp.db_of_type[info.rtype.name] = self
        if info.name in self.synced:
            return
        if self.key is None and any(c.private for c in info.columns):
            raise NexoError("privacy_key", call.span, table=info.name, db=self.path)
        fields = [(c.name, store_type(info, c)) for c in info.columns]
        if info.key is None:
            fields.append((HIDDEN_KEY, "long"))
        uniques = [c.name for c in info.columns if c.unique]
        self.call(call, lambda: self.store.ensure(info.name, self.key_of(info), fields, uniques))
        self.synced.add(info.name)
        if personal_table(info) and PRIVACY not in self.synced:
            self.call(call, lambda: self.store.ensure(PRIVACY, "id", [("id", "keyword"), ("tbl", "keyword"),
                                                                       ("row", "long"), ("state", "keyword")], []))
            self.synced.add(PRIVACY)

    def states(self, call, table: str) -> dict:
        """row -> state of the restricted and erased rows of a table."""
        rows = self.call(call, lambda: self.store.find(PRIVACY, "id", ("cmp", "tbl", "eq", table), [], None, None))
        return {r["row"]: r["state"] for r in rows}

    def guard(self, call, info: TableInfo) -> list:
        if not personal_table(info):
            return []
        from .privacy import due
        if due(self, info):
            self.purge(call, info)
        hidden = sorted(self.states(call, info.name))
        return [("nin", info.key, hidden)] if hidden else []

    # ------------------------------------------------------- queries
    def fetch(self, call, info, wheres, params, order, limit, count, columns):
        cond = all_of(self.guard(call, info) + list(wheres))
        key = self.key_of(info)
        if count:
            return self.call(call, lambda: self.store.count(info.name, key, cond))
        fields = None if columns is info.columns else [c.name for c in columns]
        sort = list(order or []) + [(key, False)]
        docs = self.call(call, lambda: self.store.find(info.name, key, cond, sort, limit, fields))
        return [tuple(d.get(c.name) for c in columns) for d in docs]

    def where(self, query, plan, leaves, value_of):
        tree = where_tree(query, plan, leaves, value_of)
        cond = None if tree is None else nnf(tree)
        return None if cond is None else (cond, [])

    def order_key(self, col: Column, desc: bool):
        return (col.name, desc)

    def equal(self, col: Column, raw) -> tuple[list, list]:
        if raw is None:
            return [("null", col.name, True)], []
        return [("cmp", col.name, "eq", raw)], []

    def keys_in(self, info: TableInfo, ids: list) -> tuple[list, list]:
        return [("in", info.key, list(ids))], []

    def sql(self, call, query):
        raise NexoError("db_no_sql", call.span, db=self.path)

    # -------------------------------------------------------- writes
    def get(self, call, table: str, key: str, value):
        rows = self.call(call, lambda: self.store.find(table, key, ("cmp", key, "eq", value), [], 1, None))
        return rows[0] if rows else None

    def write(self, call, kind: str, table: str, key: str, row: dict, info=None, values=None) -> None:
        """Insert, replace or delete a document, with what undoes it in the
        journal of the transaction."""
        old = None
        if self.depth > 0 and kind != "insert":
            old = self.get(call, table, key, row[key])
        if kind == "insert":
            self.call(call, lambda: self.store.insert(table, key, row), info, values)
        elif kind == "replace":
            self.call(call, lambda: self.store.replace(table, key, row), info, values)
        elif kind == "put":
            self.call(call, lambda: self.store.replace(table, key, row, upsert=True), info, values)
        else:
            self.call(call, lambda: self.store.delete(table, key, row[key]))
        if self.depth > 0:
            if kind == "insert" or (kind == "put" and old is None):
                self.journal.append(("delete", table, key, row))
            elif old is not None:
                self.journal.append(("insert" if kind == "delete" else "replace", table, key, old))

    def insert(self, call, info: TableInfo, record: Record) -> None:
        self.guard(call, info)
        key = self.key_of(info)
        row = {c.name: self.cell(call, record.fields[c.field], c) for c in info.columns}
        if row.get(key) is None:
            row[key] = self.call(call, lambda: self.store.next_id(info.name))
        self.write(call, "insert", info.name, key, row, info, record.fields)
        if info.key is not None and record.fields.get(info.key) is None:
            record.fields[info.key] = row[key]

    def replace(self, call, info: TableInfo, key, record: Record) -> None:
        row = {c.name: self.cell(call, record.fields[c.field], c) for c in info.columns}
        row[info.key] = key
        if self.get(call, info.name, info.key, key) is not None:
            self.write(call, "replace", info.name, info.key, row, info, record.fields)

    def delete(self, call, info: TableInfo, key) -> None:
        self.write(call, "delete", info.name, info.key, {info.key: key})

    # --------------------------------------------------- transactions
    def begin(self, call):
        self.marks.append(len(self.journal))
        self.depth += 1
        return Transaction(self, self.depth, set(self.synced))

    def end(self, call, tx, commit: bool):
        if tx.done:
            return
        tx.done = True
        self.depth = tx.depth - 1
        mark = self.marks.pop()
        if not commit:
            undo = self.journal[mark:]
            del self.journal[mark:]
            for kind, table, key, row in reversed(undo):
                if kind == "delete":
                    self.call(call, lambda: self.store.delete(table, key, row[key]))
                elif kind == "insert":
                    self.call(call, lambda: self.store.insert(table, key, row))
                else:
                    self.call(call, lambda: self.store.replace(table, key, row))
        if self.depth == 0:
            self.journal.clear()

    # -------------------------------------------------------- privacy
    def set_state(self, call, table: str, key, state: str) -> None:
        self.write(call, "put", PRIVACY, "id", {"id": f"{table}:{key}", "tbl": table, "row": key, "state": state})

    def purge(self, call, info: TableInfo) -> None:
        """Erase the rows past their keep period (section 23, rule 2)."""
        from .privacy import cutoff_of, info_of
        p = info_of(info.rtype)
        self.purged[info.name] = _time.time()
        if p is None or p.keep is None or info.key is None:
            return
        col = info.column(p.after)
        erased = sorted(r for r, s in self.states(call, info.name).items() if s == "erased")
        conds = [("cmp", col.name, "lt", cutoff_of(p, col))]
        if erased:
            conds.append(("nin", info.key, erased))
        rows = self.call(call, lambda: self.store.find(info.name, info.key, all_of(conds), [(info.key, False)],
                                                        None, [info.key]))
        for r in rows:
            self.erase_row(call, info, r[info.key])

    def anonymise(self, call, info: TableInfo, cond) -> None:
        from .privacy import blank, info_of, seal
        p = info_of(info.rtype)
        if p is None:
            return
        cols = [info.column(f) for f in p.personal() if info.column(f) is not None]
        if not cols:
            return
        key = self.key_of(info)
        rows = self.call(call, lambda: self.store.find(info.name, key, cond, [(key, False)], None, None))
        for r in rows:
            row = dict(r)
            for c in cols:
                v = blank(c, r[key])
                row[c.name] = seal(self.key, str(v)) if c.private and v is not None and self.key is not None else v
            self.write(call, "replace", info.name, key, row, info)

    def erase_row(self, call, info: TableInfo, key) -> None:
        """Delete the row, or anonymise it when other rows point to it; the
        personal fields of the rows that point to it are anonymised too."""
        referenced = False
        for other in interp_tables(call.interp).values():
            for c in other.columns:
                if c.kind == "ref" and c.target == info.rtype.name:
                    self.sync(call, other)
                    cond = ("cmp", c.name, "eq", key)
                    self.anonymise(call, other, cond)
                    if self.call(call, lambda: self.store.count(other.name, self.key_of(other), cond)):
                        referenced = True
        if referenced:
            self.anonymise(call, info, ("cmp", info.key, "eq", key))
            self.set_state(call, info.name, key, "erased")
        else:
            self.delete(call, info, key)
            if self.get(call, PRIVACY, "id", f"{info.name}:{key}") is not None:
                self.write(call, "delete", PRIVACY, "id", {"id": f"{info.name}:{key}"})


def connect(call, url: str, key):
    """connect("mongodb://...") (D171) or connect("elasticsearch://...")
    (D172)."""
    from .policy import check
    if url.startswith("mongodb://"):
        from . import mongo as client
        Error = client.MongoError
    else:
        from . import elastic as client
        Error = client.ElasticError
    try:
        hosts = client.parse_url(url)["hosts"]
    except (Error, ValueError) as exc:
        raise failure("DatabaseError", call.span, db=url, reason=str(exc))
    for host, _ in hosts:
        check(call, "net", host)
    password = None
    if key is not None:
        from .crypto import text_or_secret
        password = text_or_secret(call, "connect", key)
    try:
        store = client.Store(url, password)
    except Error as exc:
        raise failure("DatabaseError", call.span, db=url, reason=str(exc))
    db = DocDatabase(url, store, Error)
    if password is not None:
        from .privacy import data_key
        db.key = data_key(password)
    return db
