"""nexo.stream (section 19, D117): streams are sources for `when` and `|>`.

Phase 5 has a local broker: each stream is an append-only file of
messages under `.nexo/streams/<kind>/<name>.log` (one per partition when
it has several, D167), so programs on one machine talk through it; under
`nexo test` the broker lives in memory and `send` delivers at once to the
program's own handlers. A message is acknowledged (its offset saved) only
when its handler ends without error; a failing one is tried 3 more times
with exponential backoff and then goes to `<name>.dead`. The partitions
of a stream are handled as parallel tasks, each in order."""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # not on Windows
    fcntl = None
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
# the option that puts a source in its cloud (D168)
CLOUD = {"sqs": "region", "kinesis": "region", "servicebus": "namespace", "pubsub": "project"}
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


def murmur2(data: bytes) -> int:
    """Kafka's hash of a key, so a key goes to the partition Kafka's own
    clients choose (D167)."""
    length = len(data)
    m = 0x5BD1E995
    h = (0x9747B28C ^ length) & 0xFFFFFFFF
    for i in range(length // 4):
        k = int.from_bytes(data[i * 4:i * 4 + 4], "little")
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> 24
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
    tail, rest = length & ~3, length % 4
    if rest == 3:
        h ^= data[tail + 2] << 16
    if rest >= 2:
        h ^= data[tail + 1] << 8
    if rest >= 1:
        h ^= data[tail]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h


def partition_of(key: str | None, data: str, count: int) -> int:
    """The partition of a message: by its key, or by its data without one."""
    if count <= 1:
        return 0
    return (murmur2((key if key is not None else data).encode("utf-8")) & 0x7FFFFFFF) % count


class Message(Native):
    """One message: `msg.json()`, `msg.text()`, `msg.id`, `msg.time`,
    `msg.key` and `msg.partition` (D167)."""

    type_name = "message"
    __slots__ = ("stream", "id", "ms", "data", "key", "partition", "before", "receipt")

    def __init__(self, stream, id_, ms, data, key=None, partition=0, before=None, receipt=None):
        self.stream, self.id, self.ms, self.data = stream, id_, ms, data
        self.key, self.partition = key, partition
        # where a consumer starts to read it again (a log), or what
        # acknowledges it (a queue)
        self.before, self.receipt = before, receipt

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
        if name == "key":
            return self.key
        if name == "partition":
            return self.partition
        return self.missing(call, name, ["id", "time", "json", "text", "key", "partition"])


class Stream(Native):
    """A source: `kafka("orders")` and the others (section 19)."""

    type_name = "stream"
    __slots__ = ("kind", "name", "wanted", "where")

    def __init__(self, kind: str, name: str, wanted: int | None = None, where: str | None = None):
        self.kind, self.name = kind, name
        # partitions: n of the source, if given (D167)
        self.wanted = wanted
        # the region, namespace or project of a source in its cloud (D168)
        self.where = where

    def ident(self) -> str:
        return f"{self.kind}:{self.name}" + (f"@{self.where}" if self.where else "")

    def backend(self):
        """The broker of the stream: the local one, or its cloud's."""
        if self.where is None:
            return LOCAL
        from .stream_cloud import backend
        return backend(self)

    def repr(self) -> str:
        return f'{self.kind}("{self.name}")'

    def __eq__(self, other):
        return isinstance(other, Stream) and other.ident() == self.ident()

    def __hash__(self):
        return hash(self.ident())

    def path(self, partition: int = 0) -> Path:
        """The log of a partition: `<name>.log` when the stream has one,
        `<name>.<p>.log` when it has several."""
        if self.partitions() == 1:
            return ROOT / self.kind / f"{self.name}.log"
        return ROOT / self.kind / f"{self.name}.{partition}.log"

    def partitions(self, call=None) -> int:
        """How many partitions the stream has: fixed when it is created (the
        file `<name>.partitions`), 1 for a stream created without them."""
        if TEST_MODE:
            return self.wanted or 1
        meta = ROOT / self.kind / f"{self.name}.partitions"
        try:
            have = int(meta.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            # a stream that already has its one log has one partition
            have = 1 if (ROOT / self.kind / f"{self.name}.log").exists() else 0
        if have == 0:
            have = self.wanted or 1
            if have > 1:
                meta.parent.mkdir(parents=True, exist_ok=True)
                meta.write_text(f"{have}\n", encoding="utf-8")
        if self.wanted is not None and self.wanted != have and call is not None:
            raise NexoError("stream_partitions", call.span, stream=self.repr(), have=have, want=self.wanted)
        return have

    def member(self, call, name: str):
        if name == "send":
            return Builtin("send", self.m_send)
        return self.missing(call, name, ["send"])

    def m_send(self, call, value, at=None, key=None):
        from .clock import Time
        if at is not None and not isinstance(at, Time):
            from .core import expects
            raise expects(call, "send", "time", at)
        ms = at.ms if at is not None else now_ms()
        key_text = None if key is None else payload_text(call.interp.bind_value(key))
        data = payload_text(call.interp.bind_value(value))
        if TEST_MODE or self.where is None:
            message = append(self, data, ms, key_text, call)
        else:
            from .policy import check
            check(call, "net", self.backend().host(self))
            message = self.backend().send(call, self, data, ms, key_text)
        if TEST_MODE:
            deliver(call, message)
        return None


def append(stream: Stream, data: str, ms: int, key: str | None = None, call=None) -> Message:
    with _lock:
        p = partition_of(key, data, stream.partitions(call))
        if TEST_MODE:
            log = MEMORY.setdefault(f"{stream.ident()}#{p}", [])
            message = Message(stream, len(log) + 1, ms, data, key, p)
            log.append(message)
            return message
        path = stream.path(p)
        path.parent.mkdir(parents=True, exist_ok=True)
        # the lock of the file itself: producers in other processes count
        # and append one at a time, so ids stay unique
        with path.open("a+b") as f:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            count = sum(1 for _ in f)
            message = Message(stream, count + 1, ms, data, key, p)
            line = {"id": message.id, "time": ms, "data": data}
            if key is not None:
                line["key"] = key
            f.write((json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        return message


def make_source(kind: str):
    def b_source(call, name, **options):
        from .core import expects
        if not isinstance(name, str):
            raise expects(call, kind, "str", name)
        wanted = where = None
        for k, v in options.items():
            if k == "partitions":
                if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                    raise expects(call, "partitions", "int", v)
                wanted = v
                continue
            if CLOUD.get(kind) == k:
                if not isinstance(v, str) or not v:
                    raise expects(call, k, "str", v)
                where = v
                continue
            if k in REMOTE:
                raise NexoError("not_supported", call.span, what=f'"{kind}(..., {k}: ...)"')
            known = "partitions" + (f", {CLOUD[kind]}" if kind in CLOUD else "")
            raise NexoError("arg_unknown", call.span, fn=kind, param=k, params=known)
        if kind == "websocket":
            raise NexoError("not_supported", call.span, what='"websocket(...)"')
        if where is not None and wanted is not None:
            # the cloud says how many partitions (shards) a stream has
            raise NexoError("arg_unknown", call.span, fn=kind, param="partitions", params=CLOUD[kind])
        return Stream(kind, name, wanted, where)
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
    if TEST_MODE or stream.where is None:
        append(Stream(stream.kind, stream.name + ".dead"), message.data, message.ms, message.key)
        target = stream.name + ".dead"
    else:
        target = stream.backend().dead(call, message)
    text = error.text() if isinstance(error, NexoError) else failure_text(error.error)
    sys.stdout.flush()
    sys.stderr.write(errors.msg("info.stream_dead", stream=stream.name, id=message.id, dead=target, error=text) + "\n")
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
    handlers_of(call, message)
    queries_of(call, message)


def handlers_of(call, message: Message) -> None:
    n = 0
    for source, fn, key, span in list(HANDLERS):
        if source == message.stream:
            n += 1
            handle(call, fn, key, message, n)


def queries_of(call, message: Message) -> None:
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


def offset_path(stream: Stream, partition: int = 0) -> Path:
    if stream.partitions() == 1:
        return ROOT / stream.kind / f"{stream.name}.{group()}.offset"
    return ROOT / stream.kind / f"{stream.name}.{partition}.{group()}.offset"


def read_offset(stream: Stream, partition: int = 0) -> int:
    try:
        return int(offset_path(stream, partition).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def save_offset(stream: Stream, partition: int, n) -> None:
    path = offset_path(stream, partition)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{n}\n", encoding="utf-8")


def new_messages(stream: Stream, partition: int, after: int) -> list[Message]:
    out = []
    try:
        with stream.path(partition).open(encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                if i <= after:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                n = d.get("id", i)
                out.append(Message(stream, n, d.get("time", 0), d.get("data", ""), d.get("key"), partition, n - 1))
    except OSError:
        pass
    return out


def kept(stream: Stream, partition: int, position):
    """The position a consumer may save: `position`, but not past the
    messages of a window still open, so that after a stop they are read
    again (at least once, D117)."""
    for q in QUERIES:
        if q.stream == stream and q.open and q.window is not None and q.window[0] != "sliding":
            first = next((m for m in q.open if m.partition == partition), None)
            if first is not None and (first.before is None or position is None or int(first.before) < int(position)):
                position = first.before
    return position


class Local:
    """The local broker of D117: the logs of .nexo/streams, an offset per
    partition and consumer group."""
    log = True  # a consumer saves where it is (a queue deletes what it read)

    def host(self, stream):
        return None

    def partitions(self, call, stream: Stream) -> int:
        return stream.partitions(call)

    def start(self, call, stream: Stream, partition: int):
        return read_offset(stream, partition)

    def poll(self, call, stream: Stream, partition: int, position) -> list[Message]:
        return new_messages(stream, partition, position)

    def done(self, call, stream: Stream, partition: int, messages: list, position) -> None:
        save_offset(stream, partition, kept(stream, partition, position))


LOCAL = Local()


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
    # what was delivered, per stream and partition
    try:
        from .policy import check
        for s in streams:
            if s.backend().host(s) is not None:
                check(call, "net", s.backend().host(s))
        counts = {s: s.backend().partitions(call, s) for s in streams}
        read = {(s, p): s.backend().start(call, s, p) for s in streams for p in range(counts[s])}
    except (NexoFailure, NexoError) as exc:
        from ..cli import report
        report(exc)
        raise SystemExit(1)
    try:
        while True:
            idle = True
            for s in streams:
                b = s.backend()
                try:
                    batches = [(p, b.poll(call, s, p, read[(s, p)])) for p in range(counts[s])]
                except (NexoFailure, NexoError) as exc:
                    # a broker that cannot be reached now: tried again
                    from ..cli import report
                    report(exc)
                    _time.sleep(1)
                    continue
                batches = [(p, ms) for p, ms in batches if ms]
                if not batches:
                    continue
                idle = False
                if counts[s] == 1:
                    p, messages = batches[0]
                    for message in messages:
                        deliver(call, message)
                        sys.stdout.flush()
                        read[(s, p)] = message.id if b.log else None
                        b.done(call, s, p, [message], read[(s, p)])
                    continue
                # one task per partition, each in order (D167); phase 0 runs
                # the tasks one after the other, as `parallel for` does
                for p, messages in batches:
                    interp.run_task(lambda ms=messages: [handlers_of(call, m) for m in ms], [], None)
                    sys.stdout.flush()
                for p, messages in batches:
                    for m in messages:
                        queries_of(call, m)
                    read[(s, p)] = messages[-1].id if b.log else None
                    b.done(call, s, p, messages, read[(s, p)])
            for q in QUERIES:
                if q.due():
                    q.flush(call)
                    b = q.stream.backend()
                    for p in range(counts[q.stream]):
                        b.done(call, q.stream, p, [], read[(q.stream, p)])
            if idle:
                _time.sleep(0.2)
    except KeyboardInterrupt:
        drain(interp)


def exports() -> dict:
    out = {kind: Builtin(kind, make_source(kind)) for kind in KINDS}
    out["__stream_when"] = Builtin("__stream_when", b_stream_when)
    return out

