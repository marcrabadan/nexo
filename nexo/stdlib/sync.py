"""`nexo.sync` (section 5): channels, `spawn`, `select` and `shared`.

Spawned tasks run on threads and work on copies: their arguments and the
values that travel through a channel or come out of a `shared` variable are
copied (records included), so no two tasks change the same value (D82). All
channels and the `shared` lock use one condition variable; when every task
waits for a channel (or the lock), the program ends with a deadlock error.
"""
from __future__ import annotations

import itertools
import os
import threading
import time
from collections import deque
from decimal import Decimal
from typing import Any

from ..errors import NexoError
from ..types import (
    BuiltinType, Currency, EnumType, ErrorKind, ErrorValue, Function, LambdaFn, Builtin, Money, NexoSet,
    Quantity, Range, Record, RecordType, Symbol, TraitType, Variant, VariantValue, WeakRef, AliasType,
    repr_value, type_name, NList, NMap,
)

COND = threading.Condition()


class _State:
    def __init__(self):
        self.alive = 1  # the main program and the spawned tasks still running
        # tasks waiting since the last change: any change wakes them all, and
        # those that still cannot go on count themselves again
        self.blocked = 0
        self.epoch = 0
        self.waits: list[tuple[int, Any]] = []
        self.next_task = itertools.count(1)
        self.owner = None  # thread holding the shared lock
        self.depth = 0


STATE = _State()
LOCAL = threading.local()


def reset() -> None:
    global STATE
    STATE = _State()


def task_id() -> int:
    return getattr(LOCAL, "task", 0)


def wait(span, until: float | None = None, in_task: bool = False) -> None:
    """Wait (holding COND) until something changes."""
    if until is not None:
        COND.wait(max(0.0, until - time.monotonic()))
        return
    if in_task:
        # a task of `parallel` waits for the task that started the block
        COND.wait()
        return
    me = task_id()
    STATE.blocked += 1
    STATE.waits.append((me, span))
    epoch = STATE.epoch
    deadlock_check()
    COND.wait()
    if STATE.epoch == epoch:
        # woken without a change: no longer waiting
        STATE.blocked -= 1
        for i, w in enumerate(STATE.waits):
            if w[0] == me:
                del STATE.waits[i]
                break


def changed() -> None:
    """Something changed (holding COND): every waiting task looks again."""
    STATE.epoch += 1
    STATE.blocked = 0
    STATE.waits.clear()
    COND.notify_all()


def deadlock_check() -> None:
    if STATE.blocked < STATE.alive:
        return
    waits = sorted(STATE.waits, key=lambda w: w[0])
    span = next((w[1] for w in waits if w[1] is not None), None)
    from ..cli import report
    report(NexoError("deadlock", span))
    os._exit(1)


# ------------------------------------------------------------- channels

class ChannelMaker:
    """`channel` and `channel[T]`: called with `size:` it makes a channel."""

    def __init__(self, elem: Any = None):
        self.elem = elem

    def __getitem__(self, t: Any) -> "ChannelMaker":
        return ChannelMaker(t)


class Channel:
    def __init__(self, elem: Any, size: int):
        self.elem = elem
        self.size = size
        self.queue: deque = deque()
        self.closed = False
        self.sent = 0
        self.taken = 0


def copy_records(v: Any, birth: int | None = None) -> Any:
    """A copy in which every record is new."""
    if birth is None:
        birth = LOCAL_STAMP()
    if isinstance(v, Record):
        fields = {k: (None if type(x) is WeakRef else copy_records(x, birth)) for k, x in v.fields.items()}
        return Record(v.type, fields, birth)
    if isinstance(v, list):
        if not any(has_records(x) for x in v):
            return v
        return NList(copy_records(x, birth) for x in v)
    if isinstance(v, dict):
        if not any(has_records(x) for x in v.values()):
            return v
        return NMap((k, copy_records(x, birth)) for k, x in v.items())
    if isinstance(v, VariantValue):
        if not any(has_records(x) for x in v.fields.values()):
            return v
        return VariantValue(v.variant, {k: copy_records(x, birth) for k, x in v.fields.items()})
    if isinstance(v, ErrorValue) and has_records(v.cause):
        return ErrorValue(v.kind, v.message, copy_records(v.cause, birth), v.span, v.fix)
    return v


def has_records(v: Any) -> bool:
    if isinstance(v, Record):
        return True
    if isinstance(v, list):
        return any(has_records(x) for x in v)
    if isinstance(v, dict):
        return any(has_records(x) for x in v.values())
    if isinstance(v, VariantValue):
        return any(has_records(x) for x in v.fields.values())
    if isinstance(v, ErrorValue):
        return has_records(v.cause)
    return False


def restamp(v: Any, birth: int) -> None:
    """The value a task receives is its own."""
    if isinstance(v, Record):
        v.birth = birth
        for x in v.fields.values():
            if type(x) is not WeakRef:
                restamp(x, birth)
    elif isinstance(v, list):
        for x in v:
            restamp(x, birth)
    elif isinstance(v, dict):
        for x in v.values():
            restamp(x, birth)
    elif isinstance(v, VariantValue):
        for x in v.fields.values():
            restamp(x, birth)


def LOCAL_STAMP() -> int:
    from ..interpreter import _TASK
    return _TASK.stamp


def in_parallel_task() -> bool:
    from ..interpreter import _TASK
    return _TASK.in_task > 0


def new_channel(call, maker: ChannelMaker, size=None) -> Channel:
    if size is None:
        size = 0
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise NexoError("type_mismatch", call.span, name="size", type="int", value=repr_value(size),
                        vtype=type_name(size))
    return Channel(maker.elem, size)


def send(call, ch: Channel, value) -> None:
    interp = call.interp
    if ch.elem is not None and not interp.is_type(value, ch.elem, call.span):
        raise NexoError("type_mismatch", call.span, name="send", type=repr_value(ch.elem),
                        value=repr_value(value), vtype=type_name(value))
    value = copy_records(interp.bind_value(value))
    in_task = in_parallel_task()
    with COND:
        if ch.closed:
            raise NexoError("channel_closed", call.span)
        while ch.size > 0 and len(ch.queue) >= ch.size and not ch.closed:
            wait(call.span, None, in_task)
        if ch.closed:
            raise NexoError("channel_closed", call.span)
        ch.queue.append(value)
        ticket = ch.sent
        ch.sent += 1
        changed()
        if ch.size == 0:
            while ch.taken <= ticket:
                wait(call.span, None, in_task)


def receive(ch: Channel, span) -> tuple[bool, Any]:
    in_task = in_parallel_task()
    with COND:
        while True:
            if ch.queue:
                v = ch.queue.popleft()
                ch.taken += 1
                changed()
                break
            if ch.closed:
                return False, None
            wait(span, None, in_task)
    restamp(v, LOCAL_STAMP())
    return True, v


def close(ch: Channel) -> None:
    with COND:
        ch.closed = True
        changed()


def iterate(ch: Channel, span):
    while True:
        ok, v = receive(ch, span)
        if not ok:
            return
        yield v


def m_send(call, ch, value):
    send(call, ch, value)
    return None


def m_receive(call, ch):
    return receive(ch, call.span)[1]


def m_close(call, ch):
    close(ch)
    return None


METHODS = {"send": m_send, "receive": m_receive, "close": m_close}


def select(interp, channels: list, limit, span) -> tuple[int, Any]:
    """The arm that runs: the first channel with a value, len(channels)
    when the time limit passes (or every channel is closed), -1 when there
    is nothing to wait for."""
    for c in channels:
        if not isinstance(c, Channel):
            raise NexoError("not_a_channel", span, vtype=type_name(c))
    until = None
    if limit is not None:
        if not isinstance(limit, Quantity) or limit.dimension != "time":
            raise NexoError("type_mismatch", span, name="after", type="duration", value=repr_value(limit),
                            vtype=type_name(limit))
        until = time.monotonic() + float(limit.base())
    in_task = in_parallel_task()
    with COND:
        while True:
            open_ = 0
            for k, c in enumerate(channels):
                if c.queue:
                    v = c.queue.popleft()
                    c.taken += 1
                    changed()
                    restamp(v, LOCAL_STAMP())
                    return k, v
                if not c.closed:
                    open_ += 1
            if open_ == 0:
                return (len(channels) if until is not None else -1), None
            if until is not None and time.monotonic() >= until:
                return len(channels), None
            wait(span, until, in_task)


# ---------------------------------------------------------------- spawn

def spawn(interp, func, args: list, node) -> None:
    from ..cli import report
    from ..interpreter import _TASK
    copies = [(n, copy_records(interp.bind_value(v))) for n, v in args]
    with COND:
        STATE.alive += 1
        ident = next(STATE.next_task)
    deadlines = list(_TASK.deadlines)

    def run():
        LOCAL.task = ident
        _TASK.stamp = interp.new_stamp()
        _TASK.isolated = True
        _TASK.deadlines = deadlines
        for _, v in copies:
            restamp(v, _TASK.stamp)
        try:
            interp.call_at(func, copies, node)
        except Exception as exc:  # an error nobody handles ends the program
            from ..errors import NexoError as E
            from ..types import NexoFailure
            if isinstance(exc, (E, NexoFailure)):
                report(exc)
                os._exit(1)
            raise
        finally:
            finished()

    threading.stack_size(256 << 20)
    threading.Thread(target=run, daemon=True).start()


def finished() -> None:
    with COND:
        STATE.alive -= 1
        changed()
        deadlock_check()


def wait_all() -> None:
    """The end of the main program: wait for the spawned tasks."""
    with COND:
        while STATE.alive > 1:
            wait(None)


# ---------------------------------------------------------------- shared

class SharedLock:
    """The lock of `shared` variables: a statement that uses one holds it."""

    def __enter__(self):
        me = threading.get_ident()
        with COND:
            while STATE.owner is not None and STATE.owner != me:
                wait(None, None, in_parallel_task())
            STATE.owner = me
            STATE.depth += 1

    def __exit__(self, *exc):
        with COND:
            STATE.depth -= 1
            if STATE.depth == 0:
                STATE.owner = None
                changed()
        return False


def shared_read(v: Any) -> Any:
    return copy_records(v)


def shared_store(v: Any) -> Any:
    """A value stored in a shared variable: only code holding the lock
    reaches it, so any task may change its records."""
    c = copy_records(v)
    restamp(c, float("inf"))
    return c


IMMUTABLE = (str, int, float, Decimal, Money, Quantity, Range, Variant, Function, LambdaFn, Builtin,
             BuiltinType, RecordType, EnumType, AliasType, TraitType, ErrorKind, Currency, Symbol, Channel,
             ChannelMaker, type(None))


def isolated_read(b, name: str, span) -> None:
    """A spawned task reads a variable of the program: only values nobody
    can change (numbers, texts, functions, types, channels)."""
    v = b.value
    if b.mutable or not isinstance(v, IMMUTABLE):
        raise NexoError("spawn_read", span, name=name)
