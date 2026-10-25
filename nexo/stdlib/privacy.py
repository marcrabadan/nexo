"""Privacy and GDPR (section 23, D116): `@personal` and `@private` fields,
`keep` and `purpose` in a type, `privacy.export`, `erase` and `restrict`,
and `nexo privacy report`.

`@private` columns of a table are encrypted at rest with the key the
database was opened with (`connect(path, key: ...)`); rows past their
`keep` period are erased when the table is first used and again every 24
hours; restricted and erased rows are left out of every query."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time as _time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .. import errors
from ..errors import NexoError
from ..types import Builtin, Quantity, Record, RecordType
from . import Namespace

TABLE = "nexo_privacy"
DAY = 86400


class Info:
    """What a type says about the personal data it holds."""

    def __init__(self, rtype: RecordType, keep, after, keep_text, purpose, marks: dict):
        self.rtype = rtype
        self.keep = keep  # seconds (Decimal) or None
        self.after = after  # field the period counts from
        self.keep_text = keep_text
        self.purpose = purpose
        self.marks = marks  # field -> "personal" | "private"

    def personal(self) -> list[str]:
        return [f for f, m in self.marks.items()]


PRIVACY: dict[str, Info] = {}


def reset() -> None:
    PRIVACY.clear()


def field_type(rtype: RecordType, name: str):
    for f in rtype.fields:
        if f.name == name:
            return f.type
    return None


def b_privacy_type(call, rtype, settings, marks):
    """__privacy_type(T, [keep = ..., purpose = ...], {"email": ["personal"]})"""
    from .cfg import atom_text, phrase_text
    keep = after = keep_text = purpose = None
    kinds = {}
    for name, ms in marks.items():
        kinds[name] = "private" if "private" in ms else "personal"
    for entry in settings:
        key = entry.key()
        if key == "keep":
            words = entry.values[0] if len(entry.values) == 1 else None
            ok = (words is not None and len(words) == 3 and isinstance(words[0], Quantity)
                  and words[0].dimension == "time" and words[1] == "after" and isinstance(words[2], str))
            t = field_type(rtype, words[2]) if ok else None
            if not ok or t is None or getattr(t, "name", None) not in ("date", "time") or kinds.get(words[2]) == "private":
                raise entry.error("keep_form")
            keep, after, keep_text = Decimal(words[0].base()), words[2], phrase_text(words)
        elif key == "purpose":
            v = entry.value()
            if not isinstance(v, str) or len(entry.values) != 1 or entry.options:
                raise entry.error("purpose_text")
            purpose = v
        else:
            s = errors.suggest(key or "", ["keep", "purpose"])
            shown = key if key is not None else atom_text(entry.words()[0]) if entry.words() else "?"
            if s:
                raise NexoError("type_setting", entry.span, fix="suggest", name=shown, suggestion=s)
            raise NexoError("type_setting", entry.span, name=shown)
    PRIVACY[rtype.name] = Info(rtype, keep, after, keep_text, purpose, kinds)
    rtype.privacy = PRIVACY[rtype.name]
    return None


def info_of(rtype) -> Info | None:
    return getattr(rtype, "privacy", None)


# --------------------------------------------------- encryption at rest

def data_key(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def seal(key: bytes, text: str) -> str:
    """Deterministic AES-256-GCM: the same text gives the same value, so
    `find(field: value)` still works on an encrypted column."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    data = text.encode("utf-8")
    nonce = hmac.new(key, b"nexo-private:" + data, hashlib.sha256).digest()[:12]
    return "nexo:" + base64.b64encode(nonce + AESGCM(key).encrypt(nonce, data, None)).decode("ascii")


def unseal(call, key: bytes, text: str, table: str) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        if not text.startswith("nexo:"):
            raise ValueError
        blob = base64.b64decode(text[5:], validate=True)
        return AESGCM(key).decrypt(blob[:12], blob[12:], None).decode("utf-8")
    except (ValueError, InvalidTag, UnicodeDecodeError):
        raise NexoError("privacy_wrong_key", call.span, table=table)


# ------------------------------------------------ the database's side

def ensure_table(db, call) -> None:
    db.execute(call, f'CREATE TABLE IF NOT EXISTS "{TABLE}" ("tbl" TEXT NOT NULL, "row" INTEGER NOT NULL, '
               '"state" TEXT NOT NULL, PRIMARY KEY ("tbl", "row"))', [])


def hidden_where(info) -> str:
    """Rows restricted or erased are left out of every query."""
    return f'{quote(info.key)} NOT IN (SELECT "row" FROM "{TABLE}" WHERE "tbl" = \'{info.name}\')'


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def now_ms() -> int:
    return int(_time.time() * 1000)


def purge(db, call, info) -> None:
    """Erase the rows past their keep period (section 23, rule 2)."""
    p = info_of(info.rtype)
    if p is None or p.keep is None or info.key is None:
        return
    from .clock import Date, Time
    col = info.column(p.after)
    seconds = int(p.keep)
    if col.kind == "date":
        today = Time(now_ms(), "UTC").date()
        cutoff = Date.from_ordinal(today.ordinal() - seconds // DAY).repr()
    else:
        cutoff = Time(now_ms() - seconds * 1000, "UTC").iso_utc()
    rows = db.execute(call, f"SELECT {quote(info.key)} FROM {quote(info.name)} WHERE {quote(col.name)} < ? AND "
                      f'{quote(info.key)} NOT IN (SELECT "row" FROM "{TABLE}" WHERE "tbl" = ? AND "state" = \'erased\') '
                      f"ORDER BY {quote(info.key)}", [cutoff, info.name]).fetchall()
    for (key,) in rows:
        erase_row(db, call, info, key)
    db.purged[info.name] = _time.time()


def due(db, info) -> bool:
    last = db.purged.get(info.name)
    return last is None or _time.time() - last >= DAY


def blank(col, row=None):
    """The value an anonymised column takes; a @unique one is made from the
    row's id, so that several erased rows can keep it unique."""
    if col.optional:
        return None
    if col.unique and row is not None:
        if col.kind == "str":
            return f"erased-{row}"
        if col.kind == "int":
            return -row
        if col.kind == "float":
            return float(-row)
    return {"str": "", "int": 0, "float": 0.0, "bool": 0, "date": "0001-01-01",
            "time": "1970-01-01T00:00:00.000Z"}.get(col.kind, "")


def anonymise(db, call, info, where: str, params: list) -> None:
    p = info_of(info.rtype)
    if p is None:
        return
    cols = [info.column(f) for f in p.personal() if info.column(f) is not None]
    if not cols:
        return
    sets = ", ".join(f"{quote(c.name)} = ?" for c in cols)

    def values(row=None):
        out = []
        for c in cols:
            v = blank(c, row)
            out.append(seal(db.key, str(v)) if c.private and v is not None and db.key is not None else v)
        return out
    if not any(c.unique and not c.optional for c in cols):
        db.execute(call, f"UPDATE {quote(info.name)} SET {sets} WHERE {where}", values() + params)
        return
    key = quote(info.key or "id")
    rows = db.execute(call, f"SELECT {key} FROM {quote(info.name)} WHERE {where}", params).fetchall()
    for (row,) in rows:
        db.execute(call, f"UPDATE {quote(info.name)} SET {sets} WHERE {key} = ?", values(row) + [row])


def erase_row(db, call, info, key) -> None:
    """Delete the row, or anonymise it when other rows point to it; the
    personal fields of the rows that point to it are anonymised too."""
    from .db import interp_tables
    referenced = False
    for other in interp_tables(call.interp).values():
        for c in other.columns:
            if c.kind == "ref" and c.target == info.rtype.name:
                db.sync(call, other)
                anonymise(db, call, other, f"{quote(c.name)} = ?", [key])
                if db.execute(call, f"SELECT 1 FROM {quote(other.name)} WHERE {quote(c.name)} = ? LIMIT 1",
                              [key]).fetchone() is not None:
                    referenced = True
    if referenced:
        anonymise(db, call, info, f"{quote(info.key)} = ?", [key])
        db.execute(call, f'INSERT OR REPLACE INTO "{TABLE}" ("tbl", "row", "state") VALUES (?, ?, ?)',
                   [info.name, key, "erased"])
    else:
        db.execute(call, f"DELETE FROM {quote(info.name)} WHERE {quote(info.key)} = ?", [key])
        db.execute(call, f'DELETE FROM "{TABLE}" WHERE "tbl" = ? AND "row" = ?', [info.name, key])


# ------------------------------------------------- privacy.export/erase

def saved_row(call, fn: str, record):
    """(database, table info, key) of a record saved in a table."""
    info = getattr(getattr(record, "type", None), "table", None)
    if not isinstance(record, Record) or info is None or info.key is None:
        from .core import expects
        raise expects(call, f"privacy.{fn}", "@table", record)
    key = record.fields.get(info.key)
    db = getattr(call.interp, "db_of_type", {}).get(record.type.name)
    if key is None or db is None:
        raise NexoError("db_unsaved", call.span, type=record.type.name)
    return db, info, key


def b_export(call, record):
    """privacy.export(person): a JSON file with all their data, including
    the rows of its `many` fields; it returns the file's path."""
    from .data import to_json
    from .db import Query, interp_tables
    db, info, key = saved_row(call, "export", record)
    row = Query(db, info, call).m_find(call, **{info.key: key})

    def plain_row(r: Record) -> dict:
        out = {}
        for name, v in r.fields.items():
            out[name] = v.fields.get(v.type.table.key) if isinstance(v, Record) and getattr(v.type, "table", None) else v
        return out

    data = plain_row(row)
    for name, target in info.many.items():
        tinfo = interp_tables(call.interp).get(target)
        col = next((c for c in tinfo.columns if c.kind == "ref" and c.target == info.rtype.name), None) if tinfo else None
        if col is None:
            continue
        rows = Query(db, tinfo, call, [f"{quote(col.name)} = ?"], [key]).m_all(call)
        data[name] = [plain_row(r) for r in rows]
    path = f"{info.name}-{key}.json"
    try:
        Path(path).write_text(to_json(data) + "\n", encoding="utf-8")
    except OSError as exc:
        from .io import os_failure
        raise os_failure(call, path, exc)
    return path


def b_erase(call, record):
    db, info, key = saved_row(call, "erase", record)
    db.sync(call, info)
    erase_row(db, call, info, key)
    return None


def b_restrict(call, record):
    db, info, key = saved_row(call, "restrict", record)
    db.sync(call, info)
    db.execute(call, f'INSERT OR REPLACE INTO "{TABLE}" ("tbl", "row", "state") VALUES (?, ?, ?)',
               [info.name, key, "restricted"])
    return None


# --------------------------------------------------- nexo privacy report

def readers_of_data(program) -> list[str]:
    """The identities of infra blocks that can read a database:
    `can read database customers` (section 11)."""
    from .. import ast as A

    def words(phrase):
        return ["".join(p for p in a.parts if isinstance(p, str)) if isinstance(a, A.Str) else None
                for a in phrase.items]

    out = []
    for s in program.body:
        if not (isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call) and isinstance(s.expr.func, A.Name)
                and s.expr.func.name == "__infra"):
            continue
        infra = "".join(s.expr.args[0].value.parts)
        for entry in s.expr.args[1].value.items:
            head = words(entry.args[0].value.items[0])
            body = entry.args[3].value
            if len(head) == 2 and head[0] == "identity" and isinstance(body, A.ListLit):
                for line in body.items:
                    w = words(line.args[0].value.items[0])
                    if w[:3] == ["can", "read", "database"] and len(w) == 4:
                        out.append(errors.msg("info.privacy_reader", identity=head[1], infra=infra, database=w[3]))
    return out


def report_lines(program_file: str, db_path: str | None, readers: list[str]) -> list[str]:
    """The record of processing activities (section 23, rule 4)."""
    m = errors.msg
    out = [m("info.privacy_title", file=program_file)]
    if not PRIVACY:
        out.append(m("info.privacy_none"))
    for name in sorted(PRIVACY):
        p = PRIVACY[name]
        table = getattr(p.rtype, "table", None)
        if table is not None:
            where = m("info.privacy_table", table=table.name, db=db_path or "?")
        else:
            where = m("info.privacy_memory")
        out.append("")
        out.append(f"{name}: {where}")
        width = max(len(f) for f in p.marks) if p.marks else 0
        for f, kind in p.marks.items():
            out.append(f"  {f.ljust(width)}  {m('info.privacy_' + kind)}")
        out.append("  " + m("info.privacy_keep", keep=p.keep_text or m("info.privacy_missing")))
        out.append("  " + m("info.privacy_purpose", purpose=p.purpose or m("info.privacy_missing")))
        who = [m("info.privacy_program")] + (readers if table is not None else [])
        out.append("  " + m("info.privacy_readers", readers=", ".join(who)))
    return out


def exports() -> dict:
    return {
        "__privacy_type": Builtin("__privacy_type", b_privacy_type),
        "privacy": Namespace("privacy", {
            "export": Builtin("privacy.export", b_export, fallible=True),
            "erase": Builtin("privacy.erase", b_erase),
            "restrict": Builtin("privacy.restrict", b_restrict),
        }),
    }

