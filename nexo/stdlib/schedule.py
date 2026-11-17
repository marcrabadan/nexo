"""Scheduled tasks (section 4, D208): `every 6h:` and `every monday at
08:00:`. Both parsers turn the block into a function and a call of
`__every(period, it, at: "08:00")` (D90), which keeps the task. After the
top level a program with tasks runs them when their time comes, one at a
time, next to its servers and its streams, until it is stopped. Under
`nexo test` no time passes: the tasks are kept and none runs."""
from __future__ import annotations

import os
import sys
import threading
import time as _time

from .. import errors
from ..errors import NexoError

from ..types import Builtin, NexoFailure, Quantity

DAYS = ("day", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TASKS: list = []  # (period ms or None, day or None, minutes after midnight, fn, security)
_lock = threading.Lock()


def reset() -> None:
    with _lock:
        TASKS.clear()


def clock_of(call, at) -> int:
    """The minutes after midnight of "08:00"."""
    ok = isinstance(at, str) and len(at) == 5 and at[2] == ":" and (at[:2] + at[3:]).isdigit()
    if not ok or int(at[:2]) > 23 or int(at[3:]) > 59:
        raise NexoError("every_at", call.span)
    return int(at[:2]) * 60 + int(at[3:])


def b_every(call, period, fn, at=None):
    from .. import security_context
    from .stream import require_mode
    require_mode(call.span)
    if isinstance(period, str) and period in DAYS:
        if at is None:
            raise NexoError("every_day", call.span, day=period)
        task = (None, period, clock_of(call, at), fn, security_context.capture())
    elif isinstance(period, Quantity) and period.dimension == "time":
        ms = int(period.base() * 1000)
        if ms <= 0 or at is not None:
            raise NexoError("every_period", call.span)
        task = (ms, None, 0, fn, security_context.capture())
    else:
        raise NexoError("every_period", call.span)
    with _lock:
        TASKS.append(task)
    return None


def scheduled() -> bool:
    with _lock:
        return bool(TASKS)


def next_at(now_ms: int, day: str, minutes: int, zone: str) -> int:
    """The next time the clock of a zone shows a time of day, on any day or
    on a day of the week: the first one after now."""
    import datetime as dt
    from .clock import Time, wall_to_ms
    today = Time(now_ms, zone).date()
    first = dt.date(today.year, today.month, today.day)
    for ahead in range(9):
        date = first + dt.timedelta(days=ahead)
        if day != "day" and DAYS[1 + date.weekday()] != day:
            continue
        at = wall_to_ms(date.year, date.month, date.day, minutes // 60, minutes % 60, 0, 0, zone)
        if at > now_ms:
            return at
    return now_ms + 86_400_000


def now_ms() -> int:
    return int(_time.time() * 1000)


def run_forever(interp, stop_on_mistake=True) -> bool:
    """Run each task when its time comes until the program is stopped.
    False when a task stopped it with a diagnostic (already shown)."""
    from ..types import At
    from ..cli import report
    from .clock import local_zone
    from .. import security_context
    with _lock:
        tasks = list(TASKS)
    sys.stdout.flush()
    sys.stderr.write(errors.msg("info.every_running", count=len(tasks)) + "\n")
    sys.stderr.flush()
    call = At(interp, None)
    zone = local_zone()
    start = now_ms()

    def due(task, after: int) -> int:
        period, day, minutes, _, _ = task
        if period is None:
            return next_at(after, day, minutes, zone)
        # counted from the start of the program; turns that passed while
        # a task ran are not made up for
        return start + ((after - start) // period + 1) * period

    turns = [due(t, start) for t in tasks]
    try:
        while True:
            n = min(range(len(tasks)), key=lambda i: turns[i])
            wait = turns[n] - now_ms()
            if wait > 0:
                _time.sleep(min(wait, 500) / 1000)
                continue
            try:
                with security_context.intersect(tasks[n][4]):
                    interp.call(tasks[n][3], [], call.node)
            except NexoFailure as exc:
                # an error that leaves a task is shown; the next turn comes
                report(exc)
            except NexoError as exc:
                report(exc)
                return False
            sys.stdout.flush()
            turns[n] = due(tasks[n], now_ms())
    except KeyboardInterrupt:
        return True


def run_in_background(interp) -> None:
    """Next to servers or streams: on its own thread; a mistake in a task
    stops the program, as anywhere else (D17)."""
    def loop():
        if not run_forever(interp):
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(1)
    threading.Thread(target=loop, daemon=True).start()


def exports() -> dict:
    return {"__every": Builtin("__every", b_every)}
