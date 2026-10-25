"""The entries of phase 5 blocks (D114): `infra`, `stage`, `pipeline` and
`flags` are read by both parsers into lists of `__cfg(head, values,
options, body)` values, which the modules then read. A bare name is a
word (a text), a dotted name a reference (`__ref`) and a comparison a
condition kept as data (`__cond`)."""
from __future__ import annotations

from typing import Any

from .. import errors
from ..errors import NexoError
from ..types import Native, repr_value


class Cfg(Native):
    """One entry: `head` and `values` are lists of phrases (lists of
    atoms), `options` the `name: value` items, `body` the entries of its
    block (or the function of a pipeline step)."""

    type_name = "config"
    __slots__ = ("head", "values", "options", "body", "span")

    def __init__(self, head, values, options, body, span):
        self.head, self.values, self.options, self.body, self.span = head, values, options, body, span

    def repr(self) -> str:
        return show_entry(self)

    # helpers for the modules
    def words(self) -> list:
        """The atoms of the first phrase of the head."""
        return list(self.head[0]) if self.head else []

    def key(self) -> str | None:
        """The key of a `key = value` entry, or None."""
        if self.values is None:
            return None
        w = self.words()
        return w[0] if len(w) == 1 and isinstance(w[0], str) else None

    def value(self) -> Any:
        """The single atom of `key = value`, or the list of atoms when the
        first phrase has several."""
        if not self.values:
            return None
        first = self.values[0]
        return first[0] if len(first) == 1 else list(first)

    def entries(self) -> list:
        return list(self.body) if isinstance(self.body, list) else []

    def error(self, key: str, **args) -> NexoError:
        return NexoError(key, self.span, **args)


class Ref(Native):
    """`customers.url`: what a resource exposes (D114)."""

    type_name = "reference"
    __slots__ = ("parts",)

    def __init__(self, parts):
        self.parts = list(parts)

    def repr(self) -> str:
        return ".".join(self.parts)

    def __eq__(self, other):
        return isinstance(other, Ref) and other.parts == self.parts

    def __hash__(self):
        return hash(tuple(self.parts))


class Cond(Native):
    """`errors < 1%`, `country == "ES" and age > 18`: a condition."""

    type_name = "condition"
    __slots__ = ("op", "args")

    def __init__(self, op, args):
        self.op, self.args = op, list(args)

    def repr(self) -> str:
        if self.op == "not":
            return f"not {atom_text(self.args[0])}"
        return f"{atom_text(self.args[0])} {self.op} {atom_text(self.args[1])}"


def atom_text(a: Any) -> str:
    if isinstance(a, str):
        return a
    if isinstance(a, list):
        return "[" + ", ".join(atom_text(x) for x in a) + "]"
    return repr_value(a)


def phrase_text(p) -> str:
    return " ".join(atom_text(a) for a in p)


def show_entry(c: Cfg) -> str:
    text = ", ".join(phrase_text(p) for p in c.head)
    if c.values is not None:
        text += " = " + ", ".join(phrase_text(p) for p in c.values)
    for k, v in c.options.items():
        text += f", {k}: {atom_text(v)}"
    return text


def b_cfg(call, head, values, options, body):
    return Cfg([list(p) for p in head], None if values is None else [list(p) for p in values],
               dict(options), body if not isinstance(body, list) else list(body), call.span)


def b_ref(call, *parts):
    return Ref(parts)


def b_cond(call, op, *args):
    return Cond(op, args)


def holds(interp, cond: Any, row: Any, span) -> bool:
    """Evaluate a condition against a record or map: its words are fields
    (flags audiences, section 22)."""
    from ..types import Record

    def get(a):
        if isinstance(a, Cond):
            return holds(interp, a, row, span)
        if isinstance(a, str) and isinstance(row, Record) and a in row.fields:
            return row.fields[a]
        if isinstance(a, str) and isinstance(row, dict) and a in row:
            return row[a]
        if isinstance(a, Ref):
            v = row
            for p in a.parts:
                v = v.fields.get(p) if isinstance(v, Record) else v.get(p) if isinstance(v, dict) else None
            return v
        return a

    if not isinstance(cond, Cond):
        v = get(cond)
        return v if isinstance(v, bool) else v is not None
    op = cond.op
    if op == "and":
        return holds(interp, cond.args[0], row, span) and holds(interp, cond.args[1], row, span)
    if op == "or":
        return holds(interp, cond.args[0], row, span) or holds(interp, cond.args[1], row, span)
    if op == "not":
        return not holds(interp, cond.args[0], row, span)
    if op == "is":
        raise NexoError("not_supported", span, what='"is"')
    a, b = get(cond.args[0]), get(cond.args[1])
    if op not in ("==", "!=", "in") and (a is None or b is None):
        return False
    return interp.compare(op, a, b, span)


def exports() -> dict:
    from ..types import Builtin
    return {
        "__cfg": Builtin("__cfg", b_cfg),
        "__ref": Builtin("__ref", b_ref),
        "__cond": Builtin("__cond", b_cond),
    }


def lang() -> str:
    return errors.LANG
