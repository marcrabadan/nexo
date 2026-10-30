"""The patterns of section 10 that have their own word (D143-D145,
D150-D151): `single name:` (one shared value with fields), `states name:`
(a state machine whose transitions are checked), `protect(...)` (a circuit
breaker), `action name(...):` with `undo()`, and `workflow name(...):`
(durable steps). The parser rewrites them into calls of the builtins here."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..errors import NexoError, suggest
from ..types import Builtin, ErrorValue, Native, NexoFailure, failure, repr_value, show


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
    DONE.clear()


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


# -- actions (D150) ------------------------------------------------------

# the actions done and not undone, last at the end: (undo function, arguments)
DONE: list = []
_done_lock = threading.Lock()


def b_action_done(call, name, undo, args):
    with _done_lock:
        DONE.append((undo, [call.interp.store_value(a) for a in args]))
    return None


def b_undo(call):
    """Undoes the last action done; false when there is none. An undo
    that fails stays the last action, so it can be tried again."""
    with _done_lock:
        if not DONE:
            return False
        entry = DONE.pop()
    undo, args = entry
    try:
        call.interp.call(undo, [(None, a) for a in args], call.node)
    except NexoFailure:
        with _done_lock:
            DONE.append(entry)
        raise
    return True


# -- workflows (D151) ----------------------------------------------------

WORKFLOW_DIR = Path(".nexo") / "workflows"


class Workflow(Native):
    """One run of a workflow: the steps done, the end of the wait under
    way and, while its steps are undone, the error that stopped it. It is
    saved in .nexo/workflows/ after every change."""

    type_name = "workflow"
    __slots__ = ("name", "key", "path", "done", "until", "error")

    def __init__(self, name: str, key: str):
        self.name, self.key = name, key
        digest = hashlib.sha256(f"{name}{key}".encode()).hexdigest()[:16]
        self.path = WORKFLOW_DIR / f"{name}-{digest}.json"
        self.done, self.until, self.error = 0, None, None
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.done, self.until, self.error = data["done"], data["until"], data["error"]

    def repr(self) -> str:
        return f"workflow({self.name})"

    def save(self, call) -> None:
        data = {"workflow": self.name, "args": self.key, "done": self.done, "until": self.until, "error": self.error}
        try:
            WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
            # a new file put in place of the old one, so a stop half way
            # through a save keeps the last state saved
            fresh = self.path.with_suffix(".tmp")
            from ..checkpoints import commit
            commit(lambda: fresh.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"),
                   lambda: os.replace(fresh, self.path), lambda: None)
        except OSError as exc:
            from .io import os_failure
            raise os_failure(call, str(self.path), exc)


def b_workflow(call, name, args):
    from .policy import enforcing
    if enforcing():
        raise NexoError("security_workflow", call.span)
    return Workflow(name, repr_value(args))


def b_workflow_resume(call, wf):
    """A run whose undos stopped half way goes on with them."""
    if wf.error is not None:
        e = wf.error
        raise NexoFailure(ErrorValue(e["kind"], e["message"], None, call.span, e["fix"]))
    return None


def b_workflow_step(call, wf, n):
    return wf.done < n


def b_workflow_done(call, wf, n):
    wf.done, wf.until = n, None
    wf.save(call)
    return None


def b_workflow_wait(call, wf, cond, limit):
    from .http import seconds as duration
    seconds = duration(call, limit, "max")
    now = time.time()
    if wf.until is None:
        wf.until = now + seconds
        wf.save(call)
    if now >= wf.until:
        raise failure("Timeout", call.span, key="Timeout.wait", cond=cond, workflow=wf.name, limit=show(limit))
    time.sleep(min(1.0, wf.until - now))
    return None


def b_workflow_failed(call, wf, e):
    if wf.error is None:
        wf.error = {"kind": e.kind, "message": e.message, "fix": e.fix}
        wf.save(call)
    return None


def b_workflow_undo(call, wf, n):
    return wf.done >= n


def b_workflow_undone(call, wf, n):
    wf.done = n - 1
    wf.save(call)
    return None


def b_workflow_end(call, wf):
    """The run is over: its file goes (a file left would skip every step
    of the next run), and the folders if they are empty."""
    try:
        wf.path.unlink(missing_ok=True)
    except OSError as exc:
        from .io import os_failure
        raise os_failure(call, str(wf.path), exc)
    try:
        WORKFLOW_DIR.rmdir()
        WORKFLOW_DIR.parent.rmdir()
    except OSError:
        pass
    return None


def exports() -> dict:
    return {
        "__single": Builtin("__single", b_single),
        "__states": Builtin("__states", b_states),
        "__protect": Builtin("__protect", b_protect),
        "__protect_end": Builtin("__protect_end", b_protect_end),
        "__action_done": Builtin("__action_done", b_action_done),
        "undo": Builtin("undo", b_undo),
        "__workflow": Builtin("__workflow", b_workflow),
        "__workflow_resume": Builtin("__workflow_resume", b_workflow_resume),
        "__workflow_step": Builtin("__workflow_step", b_workflow_step),
        "__workflow_done": Builtin("__workflow_done", b_workflow_done),
        "__workflow_wait": Builtin("__workflow_wait", b_workflow_wait),
        "__workflow_failed": Builtin("__workflow_failed", b_workflow_failed),
        "__workflow_undo": Builtin("__workflow_undo", b_workflow_undo),
        "__workflow_undone": Builtin("__workflow_undone", b_workflow_undone),
        "__workflow_end": Builtin("__workflow_end", b_workflow_end),
    }
