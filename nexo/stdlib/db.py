"""nexo.db: databases through `connect` and `@table` types (section 6, D100).

A database is an SQLite file, a PostgreSQL server (D163, nexo/stdlib/pg.py)
or a MySQL server (D170, nexo/stdlib/mysql.py).
Each `@table` type is a table named after the
type in lowercase; its fields are columns (`ref T` fields are `<name>_id`
columns, `many T` fields are queries, not columns). Tables are created, and
new fields added, the first time a program uses them (development mode).

`db.T` is a query over the table. Pipelines on a query are translated to
SQL while their steps allow it (`where` on columns, `sort by` a column,
`take`, `count`, `pick` of columns, `group by` a column followed by
`count`, `sum` or `avg`, D161); from the first step that is not, the rows
are read and the rest runs in memory, with the same result.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import ast as A
from .. import errors
from ..errors import NexoError
from ..types import (
    AliasType, Builtin, EnumType, Lazy, Money, Native, Record, RecordType, Variant, failure, plain,
    repr_value, type_name,
)

KINDS = ("int", "float", "bool", "str", "decimal", "money", "date", "time")
# NEXO_SQL_LOG=1 writes every statement to stderr (the tests compare the
# SQL both phases send)
import os as _os


class _Sqlite:
    """sqlite3, imported the first time a program uses a database: a
    program without one starts without it (and the playground, D176,
    downloads it only for such a program)."""

    def __getattr__(self, name):
        import sqlite3 as module
        return getattr(module, name)


sqlite3 = _Sqlite()
SQL_LOG = bool(_os.environ.get("NEXO_SQL_LOG"))


class Column:
    __slots__ = ("field", "name", "kind", "optional", "unique", "target", "variants", "private", "table")

    def __init__(self, field, name, kind, optional, unique, target=None, variants=None, private=False):
        self.field, self.name, self.kind = field, name, kind
        self.optional, self.unique, self.target = optional, unique, target
        self.variants = variants
        self.private = private  # @private: encrypted at rest (D116)
        self.table = ""


class TableInfo:
    def __init__(self, rtype: RecordType, key: str | None, many: dict, marks: dict, columns: list[Column]):
        self.rtype = rtype
        self.name = rtype.name.lower()
        self.key = key
        self.many = many  # field -> target type name
        self.marks = marks
        self.columns = columns

    def column(self, field: str) -> Column | None:
        return next((c for c in self.columns if c.field == field), None)


def personal_table(info: "TableInfo") -> bool:
    from .privacy import info_of
    p = info_of(info.rtype)
    return p is not None and bool(p.marks) and info.key is not None


def interp_tables(interp) -> dict:
    if not hasattr(interp, "db_tables"):
        interp.db_tables = {}
        interp.db_of_type = {}
    return interp.db_tables


def column_kind(interp, call, rtype: RecordType, f: A.FieldDef, marks: list[str]):
    """(kind, target, variants) of a field, or a diagnostic."""
    t = f.type
    bad = NexoError("table_field", call.span, field=f.name, type=rtype.name, ftype=interp.type_text(t))
    if isinstance(t, A.RangeType):
        return "int", None, None
    if isinstance(t, A.LiteralUnion):
        kinds = {type_name(plain(interp.eval(l, rtype.env))) for l in t.literals}
        if len(kinds) == 1 and next(iter(kinds)) in ("int", "str", "float", "bool"):
            return next(iter(kinds)), None, None
        raise bad
    if t.params:
        raise bad
    if t.name in KINDS:
        return t.name, None, None
    kind = rtype.env.lookup(t.name)
    if isinstance(kind, AliasType):
        target = kind.target
        if isinstance(target, A.LiteralUnion):
            kinds = {type_name(plain(interp.eval(l, kind.env))) for l in target.literals}
            if len(kinds) == 1 and next(iter(kinds)) in ("int", "str", "float", "bool"):
                return next(iter(kinds)), None, None
        if isinstance(target, A.TypeName) and target.name in KINDS and not target.params:
            return target.name, None, None
        raise bad
    if isinstance(kind, EnumType) and all(not v.params for v in kind.variants.values()):
        return "variant", None, kind
    if isinstance(kind, RecordType):
        if "ref" not in marks:
            raise NexoError("table_needs_ref", call.span, field=f.name, type=rtype.name, target=t.name)
        return "ref", t.name, None
    raise bad


def b_db_table(call, rtype, key, many, marks):
    """__db_table(T, "id", {"orders": "Order"}, {"email": ["unique"]})"""
    interp = call.interp
    columns = []
    for f in rtype.fields:
        fmarks = list(marks.get(f.name, []))
        kind, target, variants = column_kind(interp, call, rtype, f, fmarks)
        name = f.name + "_id" if kind == "ref" else f.name
        optional = bool(getattr(f.type, "optional", False)) or f.name == key
        private = "private" in fmarks
        if private and kind == "ref":
            raise NexoError("privacy_ref", call.span, field=f.name, type=rtype.name)
        columns.append(Column(f.name, name, kind, optional, "unique" in fmarks, target, variants, private))
    info = TableInfo(rtype, key, dict(many), {k: list(v) for k, v in marks.items()}, columns)
    for c in columns:
        c.table = info.name
    rtype.table = info
    interp_tables(interp)[rtype.name] = info
    return None


# ----------------------------------------------------------- conversions

def to_sql(call, value: Any, col: Column | None):
    value = plain(value)
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, Decimal):
        from ..types import format_decimal
        return format_decimal(value)
    if isinstance(value, Money):
        return f"{value.amount} {value.currency}"
    from .clock import Date, Time
    if isinstance(value, Date):
        return value.repr()
    if isinstance(value, Time):
        return Time(value.ms, "UTC").iso_utc()
    if isinstance(value, Variant):
        return value.name
    if isinstance(value, Record):
        info = getattr(value.type, "table", None)
        if info is None or info.key is None or value.fields.get(info.key) is None:
            raise NexoError("db_unsaved", call.span, type=value.type.name)
        return value.fields[info.key]
    raise NexoError("db_value", call.span, vtype=type_name(value))


def from_sql(call, db: "Database", raw: Any, col: Column, cache: dict) -> Any:
    if raw is None:
        return None
    kind = col.kind
    if col.private:
        from .privacy import unseal
        raw = unseal(call, db.key, str(raw), col.table)
        if kind == "bool":
            raw = raw == "1"
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "bool":
        return bool(raw)
    if kind == "str":
        return str(raw)
    if kind == "decimal":
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            return None
    if kind == "money":
        amount, _, currency = str(raw).partition(" ")
        return Money.of(Decimal(amount), currency)
    if kind in ("date", "time"):
        from .clock import parse_date, parse_time
        return parse_date(str(raw)) if kind == "date" else parse_time(str(raw))
    if kind == "variant":
        return col.variants.variants.get(str(raw))
    if kind == "ref":
        info = interp_tables(call.interp).get(col.target)
        key = (col.target, raw)
        if key not in cache:
            wheres, params = db.equal(info.column(info.key), raw)
            rows = db.select(call, info, wheres, params, None, None, cache)
            cache[key] = rows[0] if rows else None
        return cache[key]
    return raw


# -------------------------------------------------------------- database

def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


SQL_TYPES = {"int": "INTEGER", "float": "REAL", "bool": "INTEGER", "str": "TEXT", "decimal": "TEXT",
             "money": "TEXT", "date": "TEXT", "time": "TEXT", "variant": "TEXT", "ref": "INTEGER"}
# PostgreSQL (D163): texts compare and sort byte by byte, as in SQLite
PG_TYPES = {"int": "BIGINT", "float": "DOUBLE PRECISION", "bool": "BOOLEAN", "str": 'TEXT COLLATE "C"',
            "decimal": 'TEXT COLLATE "C"', "money": 'TEXT COLLATE "C"', "date": 'TEXT COLLATE "C"',
            "time": 'TEXT COLLATE "C"', "variant": 'TEXT COLLATE "C"', "ref": "BIGINT"}
# MySQL (D170): texts compare byte by byte too; a unique text is a VARCHAR,
# since InnoDB indexes 3072 bytes of a column at most
MY_TEXT = "LONGTEXT COLLATE utf8mb4_0900_bin"
MY_TYPES = {"int": "BIGINT", "float": "DOUBLE", "bool": "BOOLEAN", "str": MY_TEXT, "decimal": MY_TEXT,
            "money": MY_TEXT, "date": MY_TEXT, "time": MY_TEXT, "variant": MY_TEXT, "ref": "BIGINT"}
MY_UNIQUE = "VARCHAR(768) COLLATE utf8mb4_0900_bin"
# the data_type information_schema gives each of them
PG_NAMES = {"BIGINT": "bigint", "DOUBLE PRECISION": "double precision", "BOOLEAN": "boolean",
            'TEXT COLLATE "C"': "text"}


def column_type(c: Column, dialect: str) -> str:
    kind = "str" if c.private else c.kind
    if dialect == "mysql":
        return MY_UNIQUE if c.unique and kind == "str" else MY_TYPES[kind]
    return (PG_TYPES if dialect == "pg" else SQL_TYPES)[kind]


def column_sql(info: TableInfo, c: Column, adding: bool = False, dialect: str = "sqlite") -> str:
    if c.field == info.key:
        if dialect == "pg":
            return f"{quote(c.name)} BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        if dialect == "mysql":
            return f"{quote(c.name)} BIGINT AUTO_INCREMENT PRIMARY KEY"
        return f"{quote(c.name)} INTEGER PRIMARY KEY AUTOINCREMENT"
    text = f"{quote(c.name)} {column_type(c, dialect)}"
    if not c.optional and not adding:
        text += " NOT NULL"
    if c.unique and not adding:
        text += " UNIQUE"
    if c.kind == "ref" and dialect != "mysql":
        text += f" REFERENCES {quote(c.target.lower())}"
    return text


def foreign_key(info: TableInfo, c: Column, keys: dict) -> str:
    """MySQL reads a REFERENCES written on a column and ignores it: its
    foreign keys are constraints of the table, named fk_<table>_<column>
    so that a migration can drop them (D170)."""
    return (f"CONSTRAINT {quote(f'fk_{info.name}_{c.name}')} FOREIGN KEY ({quote(c.name)}) "
            f"REFERENCES {quote(c.target.lower())} ({quote(keys.get(c.target) or 'id')})")


def create_sql(info: TableInfo, dialect: str = "sqlite", keys: dict | None = None) -> str:
    cols = [column_sql(info, c, dialect=dialect) for c in info.columns]
    if dialect == "mysql":
        cols += [foreign_key(info, c, keys or {}) for c in info.columns if c.kind == "ref"]
    return f"CREATE TABLE {quote(info.name)} ({', '.join(cols)})"


def add_column_sql(info: TableInfo, c: Column, dialect: str, keys: dict | None = None) -> str:
    text = f"ALTER TABLE {quote(info.name)} ADD COLUMN {column_sql(info, c, adding=True, dialect=dialect)}"
    if dialect == "mysql" and c.kind == "ref":
        text += f", ADD {foreign_key(info, c, keys or {})}"
    return text


def table_keys(tables) -> dict:
    """The key column of each table type, for MySQL's foreign keys."""
    return {info.rtype.name: info.key for info in tables}


def numbered(sql: str) -> str:
    """The `?` of a statement (outside quotes) as PostgreSQL's $1, $2..."""
    out, n, quote_char = [], 0, None
    for ch in sql:
        if quote_char:
            if ch == quote_char:
                quote_char = None
        elif ch in "'\"":
            quote_char = ch
        elif ch == "?":
            n += 1
            out.append(f"${n}")
            continue
        out.append(ch)
    return "".join(out)


CONTROL = ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE")


class Database(Native):
    type_name = "database"
    # a document store (D171-D172) rather than an SQL database
    document = False

    def __init__(self, path: str, conn, dialect: str = "sqlite"):
        self.path = path
        self.conn = conn
        # "sqlite", "pg" (D163) or "mysql" (D170): each server has its own
        # words in a few statements
        self.dialect = dialect
        self.pg = dialect == "pg"
        self.mysql = dialect == "mysql"
        self.synced: set[str] = set()
        self.depth = 0
        self.key: bytes | None = None  # encrypts @private columns (D116)
        self.purged: dict[str, float] = {}

    def cell(self, call, value, col: Column | None):
        """A value as stored in a column: @private ones encrypted."""
        raw = to_sql(call, value, col)
        if col is not None and col.private and raw is not None:
            from .privacy import seal
            return seal(self.key, str(raw))
        return raw

    def __eq__(self, other):
        return other is self

    __hash__ = object.__hash__

    def repr(self) -> str:
        return f"database({self.path})"

    def member(self, call, name: str):
        tables = interp_tables(call.interp)
        if name in tables:
            call.interp.db_of_type[name] = self
            return Query(self, tables[name], call)
        if name == "sql":
            return Builtin("sql", self.sql)
        return self.missing(call, name, list(tables) + ["sql"])

    def fail(self, call, exc: Exception, info: TableInfo | None = None, values: dict | None = None):
        text = str(exc)
        m = re.match(r"UNIQUE constraint failed: (\w+)\.(\w+)", text)
        if self.mysql:
            # ER_DUP_ENTRY: `Duplicate entry 'a' for key 'item.name'`, the key
            # of a UNIQUE column being named after it
            m = re.search(r"for key '(?:[^']*\.)?([^'.]+)'$", text) if getattr(exc, "code", 0) == 1062 else None
            m = (m, m.group(1)) if m else None
        elif self.pg:
            # unique_violation, with the detail `Key (email)=(...) already exists.`
            m = re.match(r"Key \((\w+)\)=", getattr(exc, "detail", "")) if getattr(exc, "code", "") == "23505" else None
            m = (m, m.group(1)) if m else None
        else:
            m = (m, m.group(2)) if m else None
        if m and info is not None:
            col = next((c for c in info.columns if c.name == m[1]), None)
            field = col.field if col else m[1]
            value = (values or {}).get(field)
            return failure("DatabaseError", call.span, key="DatabaseError.unique", type=info.rtype.name, field=field,
                           value=repr_value(value))
        return failure("DatabaseError", call.span, db=self.path, reason=text)

    def execute(self, call, sql: str, params: list, info: TableInfo | None = None, values: dict | None = None):
        if self.mysql:
            # a failed statement leaves MySQL's transaction as it was, as in
            # SQLite
            from .mysql import MysqlError
            try:
                return self.run(sql, params)
            except MysqlError as exc:
                raise self.fail(call, exc, info, values)
        if not self.pg:
            if SQL_LOG:
                import sys
                sys.stderr.write(f"sql: {sql} {params!r}\n")
            try:
                return self.conn.execute(sql, params)
            except sqlite3.Error as exc:
                raise self.fail(call, exc, info, values)
        from .pg import PgError
        sql = numbered(sql)
        # inside a transaction a failed statement leaves it as it was, as in
        # SQLite: PostgreSQL would refuse every later one (D163)
        guarded = self.depth > 0 and not sql.startswith(CONTROL)
        try:
            if guarded:
                self.run("SAVEPOINT nexo_statement", [])
            cur = self.run(sql, params)
            if guarded:
                self.run("RELEASE SAVEPOINT nexo_statement", [])
            return cur
        except PgError as exc:
            if guarded:
                self.run("ROLLBACK TO SAVEPOINT nexo_statement", [])
                self.run("RELEASE SAVEPOINT nexo_statement", [])
            raise self.fail(call, exc, info, values)

    def run(self, sql: str, params: list):
        if SQL_LOG:
            import sys
            sys.stderr.write(f"sql: {sql} {params!r}\n")
        return self.conn.execute(sql, params)

    def sync(self, call, info: TableInfo) -> None:
        """Create the table, or add the columns of new fields (D100)."""
        call.interp.db_of_type[info.rtype.name] = self
        if info.name in self.synced:
            return
        from .privacy import ensure_table, info_of
        if self.key is None and any(c.private for c in info.columns):
            raise NexoError("privacy_key", call.span, table=info.name, db=self.path)
        if self.mysql:
            # a server checks that a foreign key's table exists: those this
            # one points to are made first (D170)
            self.synced.add(info.name)
            tables = interp_tables(call.interp)
            for c in info.columns:
                target = tables.get(c.target) if c.kind == "ref" else None
                if target is not None and target.name not in self.synced:
                    self.sync(call, target)
            self.synced.discard(info.name)
        if self.pg or self.mysql:
            schema = "DATABASE()" if self.mysql else "current_schema()"
            have = {row[0] for row in self.execute(
                call, f"SELECT column_name FROM information_schema.columns WHERE table_schema = {schema} "
                      "AND table_name = ?", [info.name])}
            exists = have or None
        else:
            exists = self.execute(call, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                                  [info.name]).fetchone()
        keys = table_keys(interp_tables(call.interp).values())
        if exists is None:
            self.execute(call, create_sql(info, self.dialect, keys), [])
        else:
            if not self.pg and not self.mysql:
                have = {row[1] for row in self.execute(call, f"PRAGMA table_info({quote(info.name)})", [])}
            for c in info.columns:
                if c.name not in have:
                    self.execute(call, add_column_sql(info, c, self.dialect, keys), [])
        self.synced.add(info.name)
        if personal_table(info):
            ensure_table(self, call)

    def guard(self, call, info: TableInfo) -> list[str]:
        """The rows past their keep period are erased (when due) and the
        restricted or erased ones left out (D116)."""
        if not personal_table(info):
            return []
        from .privacy import due, hidden_where, purge
        if due(self, info):
            purge(self, call, info)
        return [hidden_where(info)]

    def fetch(self, call, info, wheres, params, order, limit, count, columns):
        """The rows (tuples of the columns' raw values) of a query, or their
        number."""
        wheres = self.guard(call, info) + list(wheres)
        what = "COUNT(*)" if count else ", ".join(quote(c.name) for c in columns)
        sql = f"SELECT {what} FROM {quote(info.name)}"
        if wheres:
            sql += " WHERE " + " AND ".join(f"({w})" for w in wheres)
        if not count:
            keys = list(order or [])
            if info.key is not None:
                keys.append(f"{quote(info.key)}")
            if keys:
                sql += " ORDER BY " + ", ".join(keys)
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        cur = self.execute(call, sql, list(params))
        if count:
            return cur.fetchone()[0]
        return cur.fetchall()

    # the words of a query (D171): a document store has its own
    def where(self, query, plan, leaves, value_of):
        """A where plan as one more condition and its values, or None."""
        return sql_where(query, plan, leaves, value_of)

    def order_key(self, col: Column, desc: bool):
        key = f"{quote(col.name)} {'DESC' if desc else 'ASC'}"
        if self.pg:
            # none first, as in SQLite (D163)
            key += " NULLS LAST" if desc else " NULLS FIRST"
        return key

    def equal(self, col: Column, raw) -> tuple[list, list]:
        """The condition `col = raw` (a stored value) and its values."""
        if raw is None:
            return [f"{quote(col.name)} IS NULL"], []
        return [f"{quote(col.name)} = ?"], [raw]

    def keys_in(self, info: TableInfo, ids: list) -> tuple[list, list]:
        marks = ", ".join("?" for _ in ids)
        return [f"{quote(info.key)} IN ({marks})"], list(ids)

    def select(self, call, info, wheres, params, order, limit, cache=None, count=False, pick=None):
        self.sync(call, info)
        columns = info.columns if pick is None else [c for _, c in pick]
        rows = self.fetch(call, info, wheres, params, order, limit, count, columns)
        if count:
            return rows
        cache = {} if cache is None else cache
        if pick is not None:
            self.prefetch(call, columns, rows, cache, set())
            return [{name: from_sql(call, self, raw, c, cache) for (name, c), raw in zip(pick, row)}
                    for row in rows]
        # the rows read here are the rows their refs to this same table
        # point to, when they are among them
        k = next((i for i, c in enumerate(columns) if c.field == info.key), None)
        own = {row[k] for row in rows} if k is not None else set()
        self.prefetch(call, columns, rows, cache, own, info.rtype.name)
        later = [i for i, c in enumerate(columns) if own and c.kind == "ref" and c.target == info.rtype.name]
        records = []
        for row in rows:
            fields = {c.field: None if i in later else from_sql(call, self, raw, c, cache)
                      for i, (c, raw) in enumerate(zip(columns, row))}
            rec = Record(info.rtype, fields, call.interp.stamp)
            if k is not None:
                cache[(info.rtype.name, row[k])] = rec
            records.append(rec)
        for rec, row in zip(records, rows):
            for i in later:
                rec.fields[columns[i].field] = from_sql(call, self, row[i], columns[i], cache)
        return records

    def prefetch(self, call, columns, rows, cache, own, name=None) -> None:
        """The rows the `ref` columns of these rows point to, read with one
        statement per column (and per 500 ids) instead of one per row (D162);
        `own` are the ids of these rows, of the table `name`."""
        tables = interp_tables(call.interp)
        for i, c in enumerate(columns):
            if c.kind != "ref" or c.private:
                continue
            target = tables.get(c.target)
            if target is None or target.key is None:
                continue
            ids = []
            for row in rows:
                raw = row[i]
                if raw is not None and (c.target, raw) not in cache and raw not in ids \
                        and not (c.target == name and raw in own):
                    ids.append(raw)
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                wheres, params = self.keys_in(target, chunk)
                found = self.select(call, target, wheres, params, None, None, cache)
                for rec in found:
                    cache[(c.target, rec.fields[target.key])] = rec
                for raw in chunk:
                    cache.setdefault((c.target, raw), None)

    def group(self, call, info, wheres, params, key: Column, total: Column | None):
        """The rows of `group by key` with COUNT(*) and, for sum and avg,
        SUM, MIN and MAX of total; None when SQLite reports an overflow."""
        self.sync(call, info)
        wheres = self.guard(call, info) + list(wheres)
        what = f"{quote(key.name)}, COUNT(*)"
        if total is not None:
            t = quote(total.name)
            what += f", SUM({t}), MIN({t}), MAX({t})"
        sql = f"SELECT {what} FROM {quote(info.name)}"
        if wheres:
            sql += " WHERE " + " AND ".join(f"({w})" for w in wheres)
        sql += f" GROUP BY {quote(key.name)} ORDER BY MIN({quote(info.key)})"
        if self.pg or self.mysql:
            # PostgreSQL and MySQL add bigints as decimals, which do not
            # overflow
            return self.execute(call, sql, list(params)).fetchall()
        if SQL_LOG:
            import sys
            sys.stderr.write(f"sql: {sql} {list(params)!r}\n")
        try:
            return self.conn.execute(sql, list(params)).fetchall()
        except sqlite3.Error as exc:
            if "integer overflow" in str(exc):
                return None
            raise self.fail(call, exc)

    def sql(self, call, query):
        """db.sql("SELECT ... {value}"): the values become parameters."""
        if not isinstance(query, SqlText):
            raise NexoError("sql_literal", call.span)
        tables = {info.name: info for info in interp_tables(call.interp).values()}
        for info in tables.values():
            if call.interp.db_of_type.get(info.rtype.name) in (None, self):
                self.sync(call, info)
        text = "?".join(quote_tables(p, tables) for p in query.parts)
        params = [to_sql(call, v, None) for v in query.values]
        cur = self.execute(call, text, params)
        if cur.description is None:
            return []
        names = [d[0] for d in cur.description]
        return [{n: v for n, v in zip(names, row)} for row in cur.fetchall()]

    # writes
    def insert(self, call, info: TableInfo, record: Record) -> None:
        cols = [c for c in info.columns if c.field != info.key or record.fields.get(c.field) is not None]
        self.guard(call, info)
        values = [self.cell(call, record.fields[c.field], c) for c in cols]
        sql = (f"INSERT INTO {quote(info.name)} ({', '.join(quote(c.name) for c in cols)}) "
               f"VALUES ({', '.join('?' for _ in cols)})")
        if self.pg and info.key is not None:
            sql += f" RETURNING {quote(info.key)}"
        cur = self.execute(call, sql, values, info, record.fields)
        if info.key is not None and record.fields.get(info.key) is None:
            record.fields[info.key] = cur.lastrowid

    def replace(self, call, info: TableInfo, key, record: Record) -> None:
        cols = [c for c in info.columns if c.field != info.key]
        values = [self.cell(call, record.fields[c.field], c) for c in cols]
        sets = ", ".join(f"{quote(c.name)} = ?" for c in cols)
        self.execute(call, f"UPDATE {quote(info.name)} SET {sets} WHERE {quote(info.key)} = ?", values + [key],
                     info, record.fields)

    def delete(self, call, info: TableInfo, key) -> None:
        self.execute(call, f"DELETE FROM {quote(info.name)} WHERE {quote(info.key)} = ?", [key])

    # transactions (D100)
    def begin(self, call):
        if self.mysql and self.depth == 0:
            # MySQL commits what a transaction did when a table is created or
            # changed: the tables are made before it begins (D170)
            for info in interp_tables(call.interp).values():
                if call.interp.db_of_type.get(info.rtype.name) in (None, self):
                    self.sync(call, info)
        self.execute(call, "BEGIN" if self.depth == 0 else f"SAVEPOINT nexo_{self.depth}", [])
        self.depth += 1
        return Transaction(self, self.depth, set(self.synced))

    def end(self, call, tx: "Transaction", commit: bool):
        if tx.done:
            return
        tx.done = True
        self.depth = tx.depth - 1
        if not commit:
            # a table created inside the transaction is gone again
            self.synced = tx.synced
        if self.depth == 0:
            self.execute(call, "COMMIT" if commit else "ROLLBACK", [])
        elif commit:
            self.execute(call, f"{self.release()} nexo_{self.depth}", [])
        else:
            self.execute(call, f"ROLLBACK TO nexo_{self.depth}", [])
            self.execute(call, f"{self.release()} nexo_{self.depth}", [])

    def release(self) -> str:
        return "RELEASE SAVEPOINT" if self.mysql else "RELEASE"


IDENT = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|[A-Za-z_][A-Za-z_0-9]*")


def quote_tables(sql: str, tables: dict) -> str:
    """Table names in raw SQL are quoted after FROM, JOIN, INTO, UPDATE and
    TABLE, and before a dot, so `FROM order` works although order is an
    SQL word (D100)."""
    out, last, prev = [], 0, ""
    for m in IDENT.finditer(sql):
        word = m.group(0)
        out.append(sql[last:m.start()])
        last = m.end()
        is_table = word.lower() in tables and word[0] not in "'\""
        after = sql[m.end():m.end() + 1]
        if is_table and (prev.upper() in ("FROM", "JOIN", "INTO", "UPDATE", "TABLE") or after == "."):
            out.append(quote(word.lower()))
        else:
            out.append(word)
        if word[0] not in "'\"":
            prev = word
    out.append(sql[last:])
    return "".join(out)


class Transaction(Native):
    type_name = "transaction"

    def __init__(self, db: Database, depth: int, synced: set):
        # the tables in sync when it began, for a rollback
        self.db, self.depth, self.done, self.synced = db, depth, False, synced


def b_db_begin(call, db):
    if not isinstance(db, Database):
        from .core import expects
        raise expects(call, "transaction", "database", db)
    return db.begin(call)


def b_db_commit(call, tx):
    tx.db.end(call, tx, True)
    return None


def b_db_rollback(call, tx):
    tx.db.end(call, tx, False)
    return None


class SqlText(Native):
    """The text of `db.sql("...")` with its interpolated values apart."""
    type_name = "str"

    def __init__(self, parts: list[str], values: list):
        self.parts, self.values = parts, values

    def repr(self) -> str:
        from ..types import show
        out = [self.parts[0]]
        for v, p in zip(self.values, self.parts[1:]):
            out.append(show(v))
            out.append(p)
        return repr_value("".join(out))

    def show(self) -> str:
        from ..types import show
        out = [self.parts[0]]
        for v, p in zip(self.values, self.parts[1:]):
            out.append(show(v))
            out.append(p)
        return "".join(out)


def b_sql_text(call, parts, values):
    return SqlText(list(parts), [call.interp.bind_value(v) for v in values])


# ----------------------------------------------------------------- queries

class Query(Lazy, Native):
    """`db.T` and the steps of a pipeline translated to SQL so far."""
    type_name = "list"

    def __init__(self, db: Database, info: TableInfo, call, wheres=(), params=(), order=(), limit=None, pick=None):
        self.db, self.info, self.call = db, info, call
        self.wheres, self.params, self.order, self.limit = list(wheres), list(params), list(order), limit
        # `pick` of columns: [(name, Column)], rows are maps (D161)
        self.pick = pick
        super().__init__(self.rows)

    def rows(self):
        return iter(self.db.select(self.call, self.info, self.wheres, self.params, self.order, self.limit,
                                   pick=self.pick))

    def derive(self, **changes) -> "Query":
        q = Query(self.db, self.info, self.call, self.wheres, self.params, self.order, self.limit, self.pick)
        for k, v in changes.items():
            setattr(q, k, v)
        return q

    def repr(self) -> str:
        return repr_value(list(self.rows()))

    def is_table(self) -> bool:
        return not self.wheres and not self.order and self.limit is None and self.pick is None

    def member(self, call, name: str):
        if self.is_table():
            methods = {"find": self.m_find, "add": self.m_add, "update": self.m_update, "remove": self.m_remove,
                       "all": self.m_all, "count": self.m_count}
        else:
            methods = {"count": self.m_count, "sum": self.m_sum}
        if name in methods:
            return Builtin(name, methods[name])
        return self.missing(call, name, list(methods))

    def m_sum(self, call):
        from .core import m_sum
        return m_sum(call, list(self.rows()))

    def m_count(self, call):
        return self.db.select(call, self.info, self.wheres, self.params, None, self.limit, count=True) \
            if self.limit is None else len(list(self.rows()))

    def m_all(self, call):
        return list(self.rows())

    def m_find(self, call, **conditions):
        info = self.info
        if not conditions:
            from .core import bad_arg
            raise bad_arg(call, f"{info.rtype.name}.find", "detail.find_needs")
        wheres, params, shown = [], [], []
        for field, value in conditions.items():
            col = info.column(field)
            if col is None:
                raise _no_field(call, info, field)
            w, p = self.db.equal(col, None if value is None else self.db.cell(call, value, col))
            wheres += w
            params += p
            shown.append(f"{col.field} = {repr_value(plain(value))}")
        rows = self.db.select(call, info, wheres, params, None, 1)
        if not rows:
            raise failure("NotFound", call.span, key="NotFound.row", type=info.rtype.name, conditions=" and ".join(shown))
        return rows[0]

    def m_add(self, call, *args, **fields):
        interp = call.interp
        info = self.info
        if args:
            if len(args) != 1 or fields or not (isinstance(args[0], Record) and args[0].type is info.rtype):
                from .core import expects
                raise expects(call, f"{info.rtype.name}.add", info.rtype.name, args[0] if args else None)
            record = args[0]
        else:
            record = interp.construct(info.rtype, list(fields.items()), call.node)
        self.db.sync(call, info)
        self.db.insert(call, info, record)
        return record

    def key_of(self, call, target) -> int:
        info = self.info
        if info.key is None:
            raise NexoError("table_no_id", call.span, type=info.rtype.name)
        if isinstance(target, Record) and target.type is info.rtype:
            key = target.fields.get(info.key)
            if key is None:
                raise NexoError("db_unsaved", call.span, type=info.rtype.name)
            return key
        if isinstance(target, int) and not isinstance(target, bool):
            return target
        from .core import expects
        raise expects(call, f"{info.rtype.name}.update", info.rtype.name, target)

    def m_update(self, call, record, **fields):
        interp = call.interp
        info = self.info
        key = self.key_of(call, record)
        if not isinstance(record, Record):
            record = self.m_find(call, **{info.key: key})
        for name, value in fields.items():
            interp.set_attr(record, name, value, call.span, call.span)
        self.db.sync(call, info)
        self.db.replace(call, info, key, record)
        return record

    def m_remove(self, call, record):
        info = self.info
        key = self.key_of(call, record)
        self.db.sync(call, info)
        self.db.delete(call, info, key)
        return None


def _no_field(call, info: TableInfo, field: str):
    from ..errors import suggest
    names = [c.field for c in info.columns]
    s = suggest(field, names)
    if s:
        return NexoError("no_member", call.span, fix="suggest", vtype=info.rtype.name, name=field, suggestion=s)
    return NexoError("no_member", call.span, vtype=info.rtype.name, name=field)


def relation(call, record: Record, name: str):
    """`customer.orders` for a `many Order` field: a query (D100)."""
    info = getattr(record.type, "table", None)
    if info is None or name not in info.many:
        return None
    interp = call.interp
    target = interp_tables(interp).get(info.many[name])
    if target is None:
        raise NexoError("unknown_type", call.span, name=info.many[name])
    col = next((c for c in target.columns if c.kind == "ref" and c.target == record.type.name), None)
    if col is None:
        raise NexoError("table_many", call.span, type=record.type.name, field=name, target=target.rtype.name)
    db = interp.db_of_type.get(target.rtype.name) or interp.db_of_type.get(record.type.name)
    if db is None:
        raise NexoError("db_unsaved", call.span, type=record.type.name)
    key = record.fields.get(info.key) if info.key else None
    if key is None:
        raise NexoError("db_unsaved", call.span, type=record.type.name)
    return Query(db, target, call, *db.equal(col, key))


# ------------------------------------------------- pipelines in SQL (D100)

OPS = {"==": "=", "!=": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">="}


def where_plan(e: A.Expr):
    """The shape of a `where` the database can run: comparisons between a
    column and a value, joined with and, or, not. Leaves are (name, expr)."""
    leaves: list = []

    def leaf(x):
        leaves.append(x)
        return len(leaves) - 1

    def cond(x):
        if isinstance(x, A.BoolOp):
            a, b = cond(x.left), cond(x.right)
            return None if a is None or b is None else (x.op, a, b)
        if isinstance(x, A.Unary) and x.op == "not":
            a = cond(x.operand)
            return None if a is None else ("not", a)
        if isinstance(x, A.Compare) and len(x.ops) == 1 and (x.ops[0] in OPS or x.ops[0] == "in"):
            return ("cmp", x.ops[0], leaf(x.operands[0]), leaf(x.operands[1]))
        if isinstance(x, A.Name) or (isinstance(x, A.Attr) and isinstance(x.obj, A.Name)):
            return ("truth", leaf(x))
        return None

    plan = cond(e)
    return None if plan is None else (plan, leaves)


def leaf_names(x: A.Expr) -> set:
    from ..checker import expr_names
    names: list = []
    expr_names(x, names, names)
    return {n for n, _ in names}


def where_tree(query: Query, plan, leaves, value_of):
    """The conditions of a where plan the database can run, as a tree
    (D171), or None to run it in memory. value_of(i) evaluates leaf i
    outside the row. Leaves:
      ("truth", col, via)            a bool column is true
      ("contains", col, text, via)   a text column holds a text
      ("colcol", lc, op, rc)         two columns compared
      ("null", col, is_null, via)    a column is (or is not) none
      ("cmp", col, op, raw, via)     a column against a value (op in SQL's
                                     words, the column on the left)
    and ("and"|"or", a, b), ("not", a). via is (ref column, table) when the
    column is of the table a ref points to (D162)."""
    info = query.info
    values: dict = {}
    # leaves `ref.field`: i -> (the ref column, the table it points to) (D162)
    via: dict = {}

    def column(i):
        x = leaves[i]
        if isinstance(x, A.Name):
            col = info.column(x.name)
            # an encrypted column cannot be compared in SQL (D116)
            return None if col is not None and col.private else col
        if isinstance(x, A.Attr) and isinstance(x.obj, A.Name):
            ref = info.column(x.obj.name)
            if ref is None or ref.kind != "ref" or ref.optional or ref.private:
                return None
            target = interp_tables(query.call.interp).get(ref.target)
            if target is None or target.key is None or personal_table(target):
                return None
            col = target.column(x.name)
            if col is None or col.private:
                return None
            if i not in via:
                # the subquery reads that table: it exists first
                query.db.sync(query.call, target)
            via[i] = (ref, target)
            return col
        return None

    def usable(i):
        x = leaves[i]
        if column(i) is not None:
            return True
        names = leaf_names(x)
        if "it" in names or any(info.column(n) is not None for n in names):
            return False
        return True

    def value(i):
        if i not in values:
            values[i] = plain(value_of(i))
        return values[i]

    def fits(col: Column, v) -> bool:
        if v is None:
            return True
        k = col.kind
        if k == "int":
            return isinstance(v, int) and not isinstance(v, bool)
        if k == "float":
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        if k == "bool":
            return isinstance(v, bool)
        if k == "str":
            return isinstance(v, str)
        if k in ("date", "time"):
            return type_name(v) == k
        if k == "variant":
            return isinstance(v, Variant)
        if k == "ref":
            return isinstance(v, Record) and v.type.name == col.target
        return False

    def build(node):
        kind = node[0]
        if kind in ("and", "or"):
            a, b = build(node[1]), build(node[2])
            return None if a is None or b is None else (kind, a, b)
        if kind == "not":
            a = build(node[1])
            return None if a is None else ("not", a)
        if kind == "truth":
            col = column(node[1])
            if col is None or col.kind != "bool":
                return None
            return ("truth", col, via.get(node[1]))
        _, op, li, ri = node
        if not usable(li) or not usable(ri):
            return None
        lc, rc = column(li), column(ri)
        if op == "in":
            if lc is not None or rc is None:
                return None
            if rc.kind == "str":
                v = value(li)
                if not isinstance(v, str):
                    return None
                return ("contains", rc, v, via.get(ri))
            return None
        if lc is not None and rc is not None:
            if li in via or ri in via:
                return None
            if lc.kind != rc.kind or lc.kind in ("decimal", "money", "variant", "ref") or (lc.kind == "bool" and op not in ("==", "!=")):
                return None
            return ("colcol", lc, OPS[op], rc)
        col, other, flipped = (lc, ri, False) if lc is not None else (rc, li, True)
        if col is None:
            return None
        at = li if lc is not None else ri
        if col.kind in ("decimal", "money"):
            return None
        v = value(other)
        if not fits(col, v):
            return None
        if v is None:
            if op in ("==", "!="):
                return ("null", col, op == "==", via.get(at))
            return None
        if col.kind in ("bool", "variant", "ref") and op not in ("==", "!="):
            return None
        sym = OPS[op]
        if flipped:
            sym = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(sym, sym)
        return ("cmp", col, sym, to_sql(query.call, v, col), via.get(at))

    return build(plan)


def render_sql(tree, db) -> tuple[str, list]:
    """The SQL of a where tree and its parameters, in order."""
    params: list = []

    def through(v, sql):
        """A condition on the table a ref points to, as one on the ref."""
        if v is None:
            return sql
        ref, target = v
        return (f"{quote(ref.name)} IN (SELECT {quote(target.key)} FROM {quote(target.name)} "
                f"WHERE {sql})")

    def render(node):
        kind = node[0]
        if kind in ("and", "or"):
            return f"({render(node[1])} {kind.upper()} {render(node[2])})"
        if kind == "not":
            return f"(NOT {render(node[1])})"
        if kind == "truth":
            return through(node[2], f"{quote(node[1].name)} = {'TRUE' if db.pg else 1}")
        if kind == "contains":
            params.append(node[2])
            return through(node[3], f"{'strpos' if db.pg else 'instr'}({quote(node[1].name)}, ?) > 0")
        if kind == "colcol":
            return f"{quote(node[1].name)} {node[2]} {quote(node[3].name)}"
        if kind == "null":
            return through(node[3], f"{quote(node[1].name)} IS {'' if node[2] else 'NOT '}NULL")
        params.append(node[3])
        return through(node[4], f"{quote(node[1].name)} {node[2]} ?")

    return render(tree), params


def sql_where(query: Query, plan, leaves, value_of) -> tuple[str, list] | None:
    """The SQL of a where plan for this table, or None to run it in memory."""
    tree = where_tree(query, plan, leaves, value_of)
    return None if tree is None else render_sql(tree, query.db)


def pipe_where(query: Query, plan, leaves, value_of):
    """A query with one more condition, or None (run in memory)."""
    if query.order or query.limit is not None or query.pick is not None:
        return None
    built = query.db.where(query, plan, leaves, value_of)
    if built is None:
        return None
    cond, params = built
    return query.derive(wheres=query.wheres + [cond], params=query.params + params)


def pipe_sort(query: Query, e: A.Expr, desc: bool):
    if query.limit is not None or query.pick is not None or not isinstance(e, A.Name):
        return None
    col = query.info.column(e.name)
    if col is None or col.kind in ("decimal", "money", "ref", "variant") or col.private:
        return None
    return query.derive(order=[query.db.order_key(col, desc)])


def pipe_take(query: Query, n: int):
    limit = n if query.limit is None else min(n, query.limit)
    return query.derive(limit=limit)


def pipe_pick(query: Query, names: list):
    """`pick a, b` of columns: only those are read (D161)."""
    if query.pick is not None:
        return None
    cols = []
    for n in names:
        col = query.info.column(n)
        if col is None:
            return None
        cols.append((n, col))
    return query.derive(pick=cols)


GROUP_KINDS = ("int", "str", "bool", "date", "time", "variant")
INT_MAX = (1 << 63) - 1


def group_column(query: Query, name: str) -> Column | None:
    """The column a query can be grouped by in SQL (D161), or None."""
    if query.order or query.limit is not None or query.pick is not None or query.info.key is None:
        return None
    if query.db.document:
        # a document store groups in memory (D171)
        return None
    col = query.info.column(name)
    if col is None or col.private or col.kind not in GROUP_KINDS:
        return None
    return col


def group_aggregate(call, query: Query, key: Column, key_name: str, kind: str, total: str | None, col_name: str):
    """`group by key |> count|sum|avg total` in SQL: the rows, or None to run
    in memory. Sums are of required int columns, and only when no order of
    adding a group's values can overflow, so the result (or the error) is
    the one of memory (D161)."""
    tc = None
    if kind != "count":
        tc = query.info.column(total) if total is not None else None
        if tc is None or tc.kind != "int" or tc.optional or tc.private:
            return None
    rows = query.db.group(call, query.info, query.wheres, query.params, key, tc)
    if rows is None:
        return None
    if tc is not None:
        for row in rows:
            if row[1] * max(abs(row[3]), abs(row[4])) > INT_MAX:
                return None
    out = []
    cache: dict = {}
    interp = call.interp
    for row in rows:
        k = from_sql(call, query.db, row[0], key, cache)
        if kind == "count":
            result = row[1]
        elif kind == "sum":
            result = row[2]
        else:
            result = interp.binop("/", row[2], row[1], call.span, call.span)
        out.append({key_name: k, col_name: result})
    return out


# ------------------------------------------------------------------ connect

def b_connect(call, url, key=None):
    from .core import expects
    if not isinstance(url, str):
        raise expects(call, "connect", "str", url)
    if url.startswith(SERVERS):
        return connect_server(call, url, key)
    if url.startswith(DOCUMENTS):
        from .docdb import connect
        return connect(call, url, key)
    if "://" in url:
        raise NexoError("not_supported", call.span, what=f'"connect({repr_value(url.split("://")[0] + "://...")})"')
    if url != ":memory:":
        from .policy import check
        check(call, "read", url)
        check(call, "write", url)
    secret_text = None
    if key is not None:
        from .crypto import text_or_secret
        secret_text = text_or_secret(call, "connect", key)
    try:
        conn = sqlite3.connect(url, isolation_level=None, check_same_thread=False)
    except sqlite3.Error as exc:
        raise failure("DatabaseError", call.span, db=url, reason=str(exc))
    db = Database(url, conn)
    if secret_text is not None:
        from .privacy import data_key
        db.key = data_key(secret_text)
    db.execute(call, "PRAGMA foreign_keys = ON", [])
    return db


SERVERS = ("postgres://", "postgresql://", "mysql://")
# the document stores (D171-D172)
DOCUMENTS = ("mongodb://", "elasticsearch://")


def dialect_of(url: str) -> str:
    return "mysql" if url.startswith("mysql://") else "pg" if url.startswith(SERVERS) else "sqlite"


def server_module(dialect: str):
    """The client of a server, and its error type."""
    if dialect == "mysql":
        from . import mysql
        return mysql, mysql.MysqlError
    from . import pg
    return pg, pg.PgError


def connect_server(call, url: str, key):
    from ..security_context import check_connector
    check_connector(call.span)
    """connect("postgres://user@host:port/database", key: password) (D163),
    or "mysql://..." (D170)."""
    dialect = dialect_of(url)
    client, Error = server_module(dialect)
    try:
        host = client.parse_url(url)["host"]
    except (Error, ValueError) as exc:
        raise failure("DatabaseError", call.span, db=url, reason=str(exc))
    from .policy import check
    check(call, "net", host)
    password = None
    if key is not None:
        from .crypto import text_or_secret
        password = text_or_secret(call, "connect", key)
    try:
        conn = client.Connection(url, password)
    except Error as exc:
        raise failure("DatabaseError", call.span, db=url, reason=str(exc))
    db = Database(url, conn, dialect)
    if password is not None:
        from .privacy import data_key
        db.key = data_key(password)
    return db


def exports() -> dict:
    return {
        "__db_table": Builtin("__db_table", b_db_table),
        "__db_begin": Builtin("__db_begin", b_db_begin),
        "__db_commit": Builtin("__db_commit", b_db_commit),
        "__db_rollback": Builtin("__db_rollback", b_db_rollback),
        "__sql_text": Builtin("__sql_text", b_sql_text),
    }


def db_exports() -> dict:
    """What `use nexo.db` makes available."""
    return {"connect": Builtin("connect", b_connect, fallible=True)}



# --------------------------------------------------- migrations (D101)

def schema_program(program: A.Program) -> list:
    """The statements `nexo db diff` and `nexo db migrate` run: the types
    and @table declarations of the program, and its `use` lines."""
    keep = []
    for s in program.body:
        if isinstance(s, (A.TypeDef, A.TraitDef, A.Use)):
            keep.append(s)
        elif (isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call) and isinstance(s.expr.func, A.Name)
              and s.expr.func.name in ("__db_table", "__privacy_type")):
            keep.append(s)
    return keep


def connect_literal(program: A.Program) -> str | None:
    """The database of the program: the first connect("...") it writes."""
    found = connect_call(program)
    return found[0] if found else None


def connect_key(program: A.Program) -> str | None:
    """The environment variable of that connect's `key: secret("NAME")`:
    the password `nexo db` gives a server (D163)."""
    found = connect_call(program)
    return found[1] if found else None


def connect_call(program: A.Program):
    """(address, secret name or None) of the first connect("...")."""
    from ..checker import walk_stmts
    from ..parser import stmt_exprs, sub_exprs

    def literal(x):
        if isinstance(x, A.Str) and all(isinstance(p, str) for p in x.parts):
            return "".join(x.parts)
        return None

    def find(e):
        if isinstance(e, A.Call) and isinstance(e.func, A.Name) and e.func.name == "connect" and e.args:
            url = literal(e.args[0].value)
            if url is not None:
                name = None
                for a in e.args[1:]:
                    v = a.value
                    if (a.name == "key" and isinstance(v, A.Call) and isinstance(v.func, A.Name)
                            and v.func.name == "secret" and v.args):
                        name = literal(v.args[0].value)
                return url, name
        for c in sub_exprs(e):
            r = find(c)
            if r is not None:
                return r
        return None

    for s in walk_stmts(program.body):
        for e in stmt_exprs(s):
            r = find(e)
            if r is not None:
                return r
    return None


class Migrations:
    """The connection of `nexo db diff|migrate`: an SQLite file or a server."""

    def __init__(self, target: str, password: str | None):
        self.dialect = dialect_of(target)
        self.pg = self.dialect == "pg"
        if self.dialect != "sqlite":
            self.conn = server_module(self.dialect)[0].Connection(target, password)
        else:
            self.conn = sqlite3.connect(target, isolation_level=None)

    def execute(self, sql: str, params=()):
        return self.conn.execute(numbered(sql) if self.pg else sql, list(params))

    def tables(self) -> list[str]:
        if self.dialect == "mysql":
            sql = ("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() "
                   "AND table_name <> 'nexo_migrations' ORDER BY table_name")
        elif self.pg:
            sql = ("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
                   "AND table_name <> 'nexo_migrations' ORDER BY table_name")
        else:
            sql = ("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                   "AND name <> 'nexo_migrations' ORDER BY name")
        return [r[0] for r in self.execute(sql)]

    def columns(self, table: str) -> list[tuple[str, str]]:
        """(name, type) of each column, the type as diff shows it."""
        if self.dialect == "mysql":
            return [(r[0], r[1].upper()) for r in self.execute(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = DATABASE() "
                "AND table_name = ? ORDER BY ordinal_position", [table])]
        if self.pg:
            return [(r[0], r[1].upper()) for r in self.execute(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = ? ORDER BY ordinal_position", [table])]
        return [(r[1], r[2].upper()) for r in self.execute(f"PRAGMA table_info({quote(table)})")]

    def foreign_keys(self, table: str) -> dict:
        """MySQL: the foreign key constraint of each column that has one."""
        if self.dialect != "mysql":
            return {}
        return {r[0]: r[1] for r in self.execute(
            "SELECT column_name, constraint_name FROM information_schema.key_column_usage WHERE table_schema = DATABASE() "
            "AND table_name = ? AND referenced_table_name IS NOT NULL", [table])}


def type_shown(c: Column, dialect: str) -> str:
    if dialect == "mysql":
        # information_schema names the type alone: BOOLEAN is a TINYINT
        shown = column_type(c, dialect).split(" ")[0].split("(")[0]
        return "TINYINT" if shown == "BOOLEAN" else shown
    kind = c.kind
    return PG_TYPES[kind].replace(' COLLATE "C"', "") if dialect == "pg" else SQL_TYPES[kind]


def diff(conn: Migrations, infos: list) -> tuple[list[str], list[str], list[str]]:
    """(changes, notes, drops) that bring the database to the types."""
    existing = conn.tables()
    dialect = conn.dialect
    server = dialect != "sqlite"
    keys = table_keys(infos)
    changes, notes, drops = [], [], []
    for info in infos:
        if info.name not in existing:
            changes.append(create_sql(info, dialect, keys) + ";")
            continue
        have = conn.columns(info.name)
        names = [c.name for c in info.columns]
        old_names = [n for n, _ in have]
        key_type = "BIGINT" if server else "INTEGER"
        for c in info.columns:
            found = next((t for n, t in have if n == c.name), None)
            if found is not None and found != type_shown(c, dialect) and not (c.field == info.key and found == key_type):
                notes.append(errors.msg("info.db_type_changed", table=info.name, column=c.name, was=found,
                                        now=type_shown(c, dialect)))
        if server and any(n not in names for n in old_names):
            fks = conn.foreign_keys(info.name)
            for n in old_names:
                if n not in names:
                    if n in fks:
                        drops.append(f"ALTER TABLE {quote(info.name)} DROP FOREIGN KEY {quote(fks[n])};")
                    drops.append(f"ALTER TABLE {quote(info.name)} DROP COLUMN {quote(n)};")
        elif any(n not in names for n in old_names):
            # SQLite drops columns by building the table again
            new = quote(info.name + "_new")
            cols = ", ".join(column_sql(info, c, adding=c.name not in old_names) for c in info.columns)
            kept = ", ".join(quote(n) for n in names if n in old_names)
            drops.append(f"CREATE TABLE {new} ({cols});")
            drops.append(f"INSERT INTO {new} ({kept}) SELECT {kept} FROM {quote(info.name)};")
            drops.append(f"DROP TABLE {quote(info.name)};")
            drops.append(f"ALTER TABLE {new} RENAME TO {quote(info.name)};")
            continue
        for c in info.columns:
            if c.name not in old_names:
                changes.append(add_column_sql(info, c, dialect, keys) + ";")
    wanted = {info.name for info in infos}
    for t in existing:
        if t not in wanted:
            drops.append(f"DROP TABLE {quote(t)};")
    return changes, notes, drops


def migration_files(folder) -> list:
    import os
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if re.fullmatch(r"\d{4}\.sql", f))


def run_command(interp, command: str, file: str, db_path: str | None, allow_drop: bool,
                key_name: str | None = None) -> int:
    """`nexo db diff` and `nexo db migrate` once the types are registered."""
    import os
    import sys
    infos = list(interp_tables(interp).values())
    if db_path is None:
        sys.stderr.write("Error: " + errors.msg("db_no_target", file=file) + " "
                         + errors.msg("fix.db_no_target") + "\n")
        return 1
    if db_path.startswith(DOCUMENTS):
        print(errors.msg("info.db_no_schema", db=db_path))
        return 0
    folder = os.path.join(os.path.dirname(file) or ".", "migrations")
    from .mysql import MysqlError
    from .pg import PgError
    try:
        conn = Migrations(db_path, os.environ.get(key_name) if key_name else None)
    except (PgError, MysqlError) as exc:
        sys.stderr.write("Error: " + errors.msg("err.DatabaseError", db=db_path, reason=str(exc)) + ". "
                         + errors.msg("fix.DatabaseError") + "\n")
        return 1
    if command == "diff":
        changes, notes, drops = diff(conn, infos)
        if not changes and not drops and not notes:
            print(errors.msg("info.db_diff_none", db=db_path, file=file))
            return 0
        files = migration_files(folder)
        number = int(files[-1][:4]) + 1 if files else 1
        path = os.path.join(folder, f"{number:04d}.sql")
        lines = ["-- " + errors.msg("info.db_migration_header", n=f"{number:04d}", db=db_path, file=file)]
        lines += changes
        lines += ["-- " + n for n in notes]
        if drops:
            lines.append("-- " + errors.msg("info.db_migration_drops"))
            lines += drops
        text = "\n".join(lines) + "\n"
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print(errors.msg("info.db_diff_written", path=path))
        sys.stdout.write(text)
        return 0
    # (MySQL keys a text of a known length; its DDL commits at once, so a
    # migration that fails halfway keeps what ran, D170)
    name_type = "VARCHAR(255)" if conn.dialect == "mysql" else "TEXT"
    conn.execute(f"CREATE TABLE IF NOT EXISTS nexo_migrations (name {name_type} PRIMARY KEY)")
    done = {r[0] for r in conn.execute("SELECT name FROM nexo_migrations")}
    applied = 0
    for name in migration_files(folder):
        if name in done:
            continue
        path = os.path.join(folder, name)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        statements = [s.strip() for s in "\n".join(
            line for line in text.splitlines() if not line.startswith("--")).split(";") if s.strip()]
        if not allow_drop and any(s.upper().startswith("DROP ") or " DROP COLUMN " in s.upper() for s in statements):
            sys.stderr.write("Error: " + errors.msg("db_needs_drop", path=path) + " "
                             + errors.msg("fix.db_needs_drop") + "\n")
            return 1
        try:
            conn.execute("BEGIN")
            for s in statements:
                conn.execute(s)
            conn.execute("INSERT INTO nexo_migrations (name) VALUES (?)", [name])
            conn.execute("COMMIT")
        except (sqlite3.Error, PgError, MysqlError) as exc:
            conn.execute("ROLLBACK")
            sys.stderr.write("Error: " + errors.msg("err.DatabaseError", db=db_path, reason=str(exc)) + ". "
                             + errors.msg("fix.DatabaseError") + "\n")
            return 1
        print(errors.msg("info.db_migrated", path=path))
        applied += 1
    if not applied:
        print(errors.msg("info.db_up_to_date", db=db_path))
    return 0
