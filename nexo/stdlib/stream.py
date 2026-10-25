"""nexo.stream (section 19, D117): streams are sources for `when` and `|>`.

Phase 5 has a local broker: each stream is an append-only file of
messages under `.nexo/streams/<kind>/<name>.log`, so programs on one
machine talk through it; under `nexo test` the broker lives in memory and
`send` delivers at once to the program's own handlers. A message is
acknowledged (its offset saved) only when its handler ends without error;
a failing one is tried 3 more times with exponential backoff and then goes
to `<name>.dead`."""
from __future__ import annotations

import json
import sys
import threading
import time as _time
from pathlib import Path
from typing import Any

from .. import errors
from ..errors import NexoError
from ..types import Builtin, Native, NexoFailure, Quantity, failure

KINDS = ("kafka", "rabbit", "sqs", "kinesis", "servicebus", "eventhub", "pubsub", "websocket")
# options that name a broker on the network: not available in phase 5 (D113)
REMOTE = ("servers", "url", "region", "namespace", "project", "endpoint")
ROOT = Path(".nexo") / "streams"
BACKOFF = (0.1, 0.2, 0.4)

TEST_MODE = False
HANDLERS: list = []  # (stream, fn, key_fn)
QUERIES: list = []  # StreamQuery waiting for messages
MEMORY: dict = {}  # test mode: stream id -> list of messages
SEEN_KEYS: dict = {}  # test mode: stream id -> set of keys
_lock = threading.RLock()


def reset() -> None:
    HANDLERS.clear()
    QUERIES.clear()
    MEMORY.clear()
    SEEN_KEYS.clear()


def now_ms() -> int:
    return int(_time.time() * 1000)


def payload_text(value: Any) -> str:
    """What `send` writes: a text as it is, anything else as compact JSON."""
    from .data import to_json
    if isinstance(value, str):
        return value
    return to_json(value, compact=True)


class Message(Native):
    """One message: `msg.json()`, `msg.text()`, `msg.id`, `msg.time`."""

    type_name = "message"
    __slots__ = ("stream", "id", "ms", "data")

    def __init__(self, stream, id_, ms, data):
        self.stream, self.id, self.ms, self.data = stream, id_, ms, data

    def repr(self) -> str:
        return f"message({self.stream.ident()} {self.id})"

    def value(self, call):
        """The decoded payload (pipelines work on it): JSON, or the text."""
        try:
            from .data import b_json_parse
            return b_json_parse(call, self.data)
        except NexoFailure:
            return self.data

    def member(self, call, name: str):
        if name == "id":
            return self.id
        if name == "time":
            from .clock import Time, local_zone
            return Time(self.ms, local_zone())
        if name == "json":
            from .data import b_json_parse
            return Builtin("json", lambda c: b_json_parse(c, self.data))
        if name == "text":
            return Builtin("text", lambda c: self.data)
        return self.missing(call, name, ["id", "time", "json", "text"])


class Stream(Native):
    """A source: `kafka("orders")` and the others (section 19)."""

    type_name = "stream"
    __slots__ = ("kind", "name")

    def __init__(self, kind: str, name: str):
        self.kind, self.name = kind, name

    def ident(self) -> str:
        return f"{self.kind}:{self.name}"

    def repr(self) -> str:
        return f'{self.kind}("{self.name}")'

    def __eq__(self, other):
        return isinstance(other, Stream) and other.ident() == self.ident()

    def __hash__(self):
        return hash(self.ident())

    def path(self) -> Path:
        return ROOT / self.kind / f"{self.name}.log"

    def member(self, call, name: str):
        if name == "send":
            return Builtin("send", self.m_send)
        return self.missing(call, name, ["send"])

    def m_send(self, call, value, at=None):
        from .clock import Time
        if at is not None and not isinstance(at, Time):
            from .core import expects
            raise expects(call, "send", "time", at)
        ms = at.ms if at is not None else now_ms()
        message = append(self, payload_text(call.interp.bind_value(value)), ms)
        if TEST_MODE:
            deliver(call, message)
        return None


def append(stream: Stream, data: str, ms: int) -> Message:
    with _lock:
        if TEST_MODE:
            log = MEMORY.setdefault(stream.ident(), [])
            message = Message(stream, len(log) + 1, ms, data)
            log.append(message)
            return message
        path = stream.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        if path.exists():
            with path.open("rb") as f:
                count = sum(1 for _ in f)
        message = Message(stream, count + 1, ms, data)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": message.id, "time": ms, "data": data}, ensure_ascii=False,
                               separators=(",", ":")) + "\n")
        return message


def make_source(kind: str):
    def b_source(call, name, **options):
        from .core import expects
        if not isinstance(name, str):
            raise expects(call, kind, "str", name)
        for k in options:
            if k in REMOTE:
                raise NexoError("not_supported", call.span, what=f'"{kind}(..., {k}: ...)"')
            raise NexoError("arg_unknown", call.span, fn=kind, param=k, params="-")
        if kind == "websocket":
            raise NexoError("not_supported", call.span, what='"websocket(...)"')
        return Stream(kind, name)
    return b_source


# ------------------------------------------------------------- handlers

def b_stream_when(call, source, fn, key=None):
    if not isinstance(source, Stream):
        from .core import expects
        raise expects(call, "when", "stream", source)
    HANDLERS.append((source, fn, key, call.span))
    return None


def handle(call, fn, key, message: Message, n: int = 1) -> None:
    """Run a handler (the n-th `when` of its stream) with retries; then the
    dead-letter stream."""
    interp = call.interp
    ident = message.stream.ident()
    if key is not None:
        from ..types import repr_value
        try:
            k = repr_value(interp.bind_value(interp.call(key, [(None, message)], call.node)))
        except (NexoFailure, NexoError) as exc:
            dead(call, message, exc)
            return
        if k in keys_of(message.stream, n):
            return
    error = None
    for attempt in range(4):
        try:
            interp.call(fn, [(None, message)], call.node)
            error = None
            break
        except (NexoFailure, NexoError) as exc:
            error = exc
            if attempt < 3:
                _time.sleep(BACKOFF[attempt])
    if error is not None:
        dead(call, message, error)
        return
    if key is not None:
        remember_key(message.stream, n, k)


def dead(call, message: Message, error) -> None:
    from ..cli import failure_text
    stream = message.stream
    target = Stream(stream.kind, stream.name + ".dead")
    append(target, message.data, message.ms)
    text = error.text() if isinstance(error, NexoError) else failure_text(error.error)
    sys.stdout.flush()
    sys.stderr.write(errors.msg("info.stream_dead", stream=stream.name, id=message.id, error=text) + "\n")
    sys.stderr.flush()


def keys_path(stream: Stream, n: int) -> Path:
    """The keys each handler has processed: its own, so that two handlers of
    one stream each process a message once."""
    return ROOT / stream.kind / f"{stream.name}.{group()}.{n}.keys"


def keys_of(stream: Stream, n: int) -> set:
    if TEST_MODE:
        return SEEN_KEYS.setdefault(f"{stream.ident()}#{n}", set())
    path = keys_path(stream, n)
    try:
        return set(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return set()


def remember_key(stream: Stream, n: int, k: str) -> None:
    if TEST_MODE:
        SEEN_KEYS.setdefault(f"{stream.ident()}#{n}", set()).add(k)
        return
    path = keys_path(stream, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(k + "\n")


GROUP = {"name": "nexo"}


def group() -> str:
    return GROUP["name"]


def deliver(call, message: Message) -> None:
    """A message reaches every handler and query of its stream."""
    n = 0
    for source, fn, key, span in list(HANDLERS):
        if source == message.stream:
            n += 1
            handle(call, fn, key, message, n)
    for q in list(QUERIES):
        if q.stream == message.stream:
            q.add(call, message)


# ---------------------------------------------------------- windows

class StreamQuery(Native):
    """A pipeline whose source is a stream: its steps are kept and run on
    the messages of each window; `each` at the end starts it."""

    type_name = "stream"
    __slots__ = ("stream", "window", "steps", "open", "last")

    def __init__(self, stream, window=None, steps=()):
        self.stream = stream
        self.window = window  # None | ("window"|"sliding"|"session", ms)
        self.steps = list(steps)  # (step, env)
        self.open: list = []  # messages of the open window
        self.last = None

    def repr(self) -> str:
        return self.stream.repr()

    def with_step(self, step, env) -> "StreamQuery":
        return StreamQuery(self.stream, self.window, self.steps + [(step, env)])

    def add(self, call, message: Message) -> None:
        kind = self.window[0] if self.window else None
        size = self.window[1] if self.window else 0
        if kind is None:
            self.run(call, [message])
            return
        if kind == "sliding":
            self.open.append(message)
            self.open = [m for m in self.open if m.ms > message.ms - size]
            self.run(call, list(self.open))
            return
        if self.open:
            first = self.open[0]
            if kind == "window" and message.ms // size != first.ms // size:
                self.flush(call)
            elif kind == "session" and message.ms - self.open[-1].ms > size:
                self.flush(call)
        self.open.append(message)

    def due(self) -> bool:
        """A window or session whose time has passed (run mode)."""
        if not self.open or self.window is None or self.window[0] == "sliding":
            return False
        kind, size = self.window
        if kind == "window":
            return now_ms() >= (self.open[0].ms // size + 1) * size
        return now_ms() - self.open[-1].ms > size

    def flush(self, call) -> None:
        if self.open and self.window is not None and self.window[0] != "sliding":
            messages, self.open = self.open, []
            self.run(call, messages)

    def run(self, call, messages: list) -> None:
        interp = call.interp
        value: Any = [m.value(call) for m in messages]
        try:
            for i, (step, env) in enumerate(self.steps):
                if step.kind == "each" and not is_rows(value):
                    # count, sum or avg give one value: each gets it once
                    interp.apply_expr(step.expr, value, env)
                    continue
                value = interp.step(step, value, env)
        except (NexoFailure, NexoError) as exc:
            from ..cli import report
            report(exc)


def is_rows(value) -> bool:
    from ..types import Lazy
    return isinstance(value, (list, Lazy))


def window_ms(call, step_kind: str, size) -> int:
    if not isinstance(size, Quantity) or size.dimension != "time" or size.base() <= 0:
        raise NexoError("stream_window", call.span, step=step_kind)
    return int(size.base() * 1000)


def stream_step(interp, step, value, env):
    """A step of a pipeline whose source is a stream, or None."""
    if isinstance(value, Stream):
        value = StreamQuery(value)
    if not isinstance(value, StreamQuery):
        if step.kind in ("window", "sliding", "session"):
            raise NexoError("stream_window_source", step.span, step=step.kind)
        return None
    if step.kind in ("window", "sliding", "session"):
        if value.window is not None or value.steps:
            raise NexoError("stream_window_first", step.span, step=step.kind)
        from ..types import At
        size = interp.eval(step.expr, env)
        return StreamQuery(value.stream, (step.kind, window_ms(At(interp, step.span), step.kind, size)))
    if step.kind == "call":
        raise NexoError("stream_call", step.span)
    q = value.with_step(step, env)
    if step.kind == "each":
        QUERIES.append(q)
        return None
    return q


def drain(interp) -> None:
    """Close every open window (end of a test, end of the program)."""
    from ..types import At
    call = At(interp, None)
    for q in list(QUERIES):
        q.flush(call)


# ------------------------------------------------------------- run mode

def listening() -> bool:
    return bool(HANDLERS or QUERIES)


def offset_path(stream: Stream) -> Path:
    return ROOT / stream.kind / f"{stream.name}.{group()}.offset"


def read_offset(stream: Stream) -> int:
    try:
        return int(offset_path(stream).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def save_offset(stream: Stream, n: int) -> None:
    path = offset_path(stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{n}\n", encoding="utf-8")


def new_messages(stream: Stream, after: int) -> list[Message]:
    out = []
    try:
        with stream.path().open(encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                if i <= after:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                out.append(Message(stream, d.get("id", i), d.get("time", 0), d.get("data", "")))
    except OSError:
        pass
    return out


def acknowledge(stream: Stream, read: int) -> None:
    """Save the offset up to `read`, but not past the messages of a window
    still open: after a stop they are read again (at least once, D117)."""
    done = read
    for q in QUERIES:
        if q.stream == stream and q.open and q.window is not None and q.window[0] != "sliding":
            first = q.open[0].id
            if isinstance(first, int):
                done = min(done, first - 1)
    save_offset(stream, done)


def listen_forever(interp, program: str) -> None:
    """`nexo file.nexo` with `when` handlers or stream pipelines: read the
    new messages of every stream until the program is stopped."""
    from ..types import At
    GROUP["name"] = Path(program).stem or "nexo"
    call = At(interp, None)
    streams = []
    for s in [h[0] for h in HANDLERS] + [q.stream for q in QUERIES]:
        if s not in streams:
            streams.append(s)
    sys.stderr.write(errors.msg("info.stream_listening", streams=", ".join(s.repr() for s in streams)) + "\n")
    sys.stderr.flush()
    read = {s: read_offset(s) for s in streams}  # what was delivered
    try:
        while True:
            idle = True
            for s in streams:
                for message in new_messages(s, read[s]):
                    idle = False
                    deliver(call, message)
                    sys.stdout.flush()
                    read[s] = message.id if isinstance(message.id, int) else read[s] + 1
                    acknowledge(s, read[s])
            for q in QUERIES:
                if q.due():
                    q.flush(call)
                    acknowledge(q.stream, read[q.stream])
            if idle:
                _time.sleep(0.2)
    except KeyboardInterrupt:
        drain(interp)


def exports() -> dict:
    out = {kind: Builtin(kind, make_source(kind)) for kind in KINDS}
    out["__stream_when"] = Builtin("__stream_when", b_stream_when)
    return out

