"""The patterns of section 10 that have their own word (D143-D145):
`single name:` (one shared value with fields), `states name:` (a state
machine whose transitions are checked) and `protect(...)` (a circuit
breaker). The parser rewrites the three into calls of the builtins here."""
from __future__ import annotations

import threading
import time
from typing import Any

from ..errors import NexoError, suggest
from ..types import Builtin, Native, failure, repr_value, show


def texts(names) -> str:
    return ", ".join(f'"{n}"' for n in names)


class Single(Native):
    """`single config:` (D143): its fields are read and changed with
    `config.port`; it has no others."""

    type_name = "single"
    __slots__ = ("name", "fields", "lock")

    def __init__(self, name: str, fields: dict):
        self.name, self.fields, self.lock = name, fields, threading.Lock()

    def repr(self) -> str:
        with self.lock:
            inner = ", ".join(f"{k}: {repr_value(v)}" for k, v in self.fields.items())
        return f"{self.name}({inner})"

    def member(self, call, name: str):
        with self.lock:
            if name in self.fields:
                return self.fields[name]
        return self.missing(call, name, list(self.fields))

    def missing(self, call, name, candidates):
        s = suggest(name, candidates)
        if s:
            raise NexoError("no_member", call.span, fix="suggest", vtype=self.name, name=name, suggestion=s)
        raise NexoError("no_member", call.span, vtype=self.name, name=name)

    def set_member(self, call, name: str, value: Any) -> None:
        with self.lock:
            if name in self.fields:
                self.fields[name] = call.interp.store_value(value)
                return
        self.missing(call, name, list(self.fields))


class States(Native):
    """`states order:` (D144): `order.state` is the current state,
    `order.go("paid")` moves to another one and `order.can("paid")` says
    whether it may."""

    type_name = "states"
    __slots__ = ("name", "names", "next", "current", "lock")

    def __init__(self, name: str, pairs: list):
        self.name = name
        self.names: list[str] = []
        self.next: dict[str, list[str]] = {}
        for a, b in pairs:
            for n in (a, b):
                if n not in self.names:
                    self.names.append(n)
                    self.next[n] = []
            if b not in self.next[a]:
                self.next[a].append(b)
        self.current = self.names[0] if self.names else None
        self.lock = threading.Lock()

    def repr(self) -> str:
        return f"states({self.name})"

    def member(self, call, name: str):
        if name == "state":
            with self.lock:
                return self.current
        if name == "go":
            return Builtin("go", self.go)
        if name == "can":
            return Builtin("can", self.can)
        return self.missing(call, name, ["state", "go", "can"])

    def known(self, call, to: Any, method: str) -> str:
        if not isinstance(to, str):
            from .core import expects
            raise expects(call, method, "str", to)
        if to not in self.next:
            s = suggest(to, self.names)
            if s:
                raise NexoError("state_unknown", call.span, fix="suggest", state=to, machine=self.name, suggestion=s)
            raise NexoError("state_unknown", call.span, state=to, machine=self.name, states=texts(self.names))
        return to

    def can(self, call, to):
        to = self.known(call, to, "can")
        with self.lock:
            return to in self.next[self.current]

    def go(self, call, to):
        to = self.known(call, to, "go")
        with self.lock:
            here = self.current
            if to not in self.next[here]:
                after = texts(self.next[here]) or "none"
                raise failure("InvalidValue", call.span, key="InvalidValue.state",
                              machine=self.name, origin=here, to=to, next=after)
            self.current = to
        return None


class Circuit:
    """The state of one protected value: failures in a row and, while it
    is open, the moment it may be tried again."""

    __slots__ = ("fails", "until")

    def __init__(self):
        self.fails, self.until = 0, None


CIRCUITS: dict[str, Circuit] = {}
_lock = threading.Lock()


def reset() -> None:
    CIRCUITS.clear()


class Guard(Native):
    """What `__protect` gives the end of the block."""

    type_name = "protect"
    __slots__ = ("key", "fails", "wait")

    def __init__(self, key, fails, wait):
        self.key, self.fails, self.wait = key, fails, wait


def b_single(call, name, **fields):
    return Single(name, {k: call.interp.store_value(v) for k, v in fields.items()})


def b_states(call, name, pairs):
    return States(name, [tuple(p) for p in pairs])


def b_protect(call, target, name, fails=None, wait=None):
    """Fails with CircuitOpen while the circuit of `target` is open."""
    from .core import expects
    from .http import seconds
    if not isinstance(fails, int) or isinstance(fails, bool) or fails < 1:
        raise expects(call, "fails", "int", fails)
    limit = seconds(call, wait, "wait")
    key = repr_value(target)
    with _lock:
        c = CIRCUITS.setdefault(key, Circuit())
        if c.until is not None and time.monotonic() < c.until:
            raise failure("CircuitOpen", call.span, name=name, fails=c.fails, wait=show(wait))
    return Guard(key, fails, limit)


def b_protect_end(call, guard, ok):
    with _lock:
        c = CIRCUITS.setdefault(guard.key, Circuit())
        if ok:
            c.fails, c.until = 0, None
        else:
            c.fails += 1
            if c.fails >= guard.fails:
                c.until = time.monotonic() + guard.wait
    return None


def exports() -> dict:
    return {
        "__single": Builtin("__single", b_single),
        "__states": Builtin("__states", b_states),
        "__protect": Builtin("__protect", b_protect),
        "__protect_end": Builtin("__protect_end", b_protect_end),
    }
