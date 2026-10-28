# Shield synchronous events

`on` and `emit` now work with dependency ceilings and operator agent/tool grants.
Events keep their existing synchronous execution model: `emit` invokes the
registered handlers on the emitter's thread, in registration order. It does not
start a listener, create a worker or install an asynchronous event loop.

```nexo
policy:
    allow read "data/public"
on changed(value):
    print(value["id"])
emit changed({"id": 7})
```

```sh
nexo security run --dependencies permissions.json main.nexo
nexo security run --authorization authorization.json main.nexo
```

No manifest schema changes or new permission kinds are required. The existing
root policy remains the outer ceiling, and event names retain their process-wide
namespace. There is no new event-name subscription allowlist or publisher
identity protocol.

## Registration and dispatch contexts

A registration retains the handler closure and an immutable snapshot of the
active permission stack. If a restricted dependency or tool calls a root
function that registers a handler, that restriction remains attached to the
registration after the dependency, tool or agent invocation returns. The same
holds when registration occurs in a spawned task.

Dispatch intersects the captured registration context with the emitter's current
context. The handler's lexical module ceiling also applies when dependency
contexts are enabled. A root handler cannot discard the emitter's restrictions,
and a root emitter cannot discard a restricted registration context. Nested
emits, parallel tasks and spawned emitters preserve those intersections.

Each handler has its own context guard. Completion or a handled failure restores
the previous context; one restricted handler cannot reduce or widen the grant of
the next unrelated handler. Security violations remain fatal diagnostics that
Nexo `try` cannot catch.

The registry is locked only while registering or copying the current handler
list. The lock is released before invoking user code. Every emission uses a
fixed snapshot, so a handler registered during that emission first participates
in a subsequent emission. This also applies to concurrent registration from
another thread. Rust and Python now implement the same behavior.

## Payloads and trust boundary

When either security manifest is configured, event payloads follow the same
plain-data boundary as channels and shared state: scalar values and materialized
lists, maps and sets of data. Callable, deferred, record, constructor and opaque
values are rejected before delivery. Validation has the existing depth and node
bounds. An invalid payload does not invoke any handler, including for an event
with no listeners. Ordinary execution retains its existing event value semantics.

This protects capability contexts, not payload confidentiality or complete
information flow. Event names are shared inside the application. Trusted code
must decide which data it publishes and which modules may receive it. A text
payload does not grant a handler new capabilities, but this feature does not
assign sensitivity labels to that text. Label propagation, event subscription
policy and cross-process event protocols remain separate work.

## Linux and remaining event loops

Synchronous events require no additional syscalls and can run inside the Linux
offline/online worker profiles. They remain subject to the worker's filesystem,
network and resource contract. A handler cannot introduce `spawn`, `parallel` or
sync imports into those single-thread profiles. Hosts without the required
kernel mechanisms still fail before target execution.

`when` stream handlers are available only through the explicit
[process-local memory stream profile](shield-streams.md). Other `when`, `every`
and `lifecycle` constructs, network servers and persistent pipelines remain rejected
under dependency or authorization configuration. This implementation does not add unsubscribe,
per-event subscription ACLs, event queues, individual handler cancellation or listener
lifetimes.

## Validation

```sh
cd compiler
cargo build --locked --workspace
cargo test --locked --workspace
cd ..
pytest -q tests/test_security_events.py tests/test_security_dispatch.py \
    tests/test_security_spawn.py tests/test_security_isolation.py \
    tests/test_security.py tests/test_ai.py
```

Integration tests use real files, a local fake model and channel-coordinated
threads. They verify registration-time ceilings, emitter ceilings, nested events,
restoration after success/failure, fixed dispatch snapshots, concurrent
registration, data-only payloads, agent/tool scopes and spawned registrations.
Kernel tests execute inline events on a supported host or verify rejection before
execution when kernel support is missing. This development host cannot establish
positive Landlock execution; strict validation remains required on the deployment
host. Docker validation includes the event suite.

Event registrations share the cumulative [process registration budget](shield-budgets.md) with stream handlers and deferred pipeline builders.

[In-memory HTTP dispatch](shield-web.md) is available with its separate explicit mode.
