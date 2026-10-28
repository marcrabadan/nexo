# Shield process-local memory streams

`--streams=memory` enables a bounded, in-process stream broker with synchronous
message delivery. It supports `when` handlers, key functions, normal application
error retries and in-memory dead-letter history. The `kafka`, `rabbit`, `sqs`,
`kinesis`, `servicebus`, `eventhub` and `pubsub` names identify local sources;
this profile does not connect to any of those external services.

```sh
nexo security run --streams=memory main.nexo
nexo security run --streams=memory --dependencies permissions.json main.nexo
nexo security trace --streams=memory --authorization authorization.json main.nexo
nexo security run --sandbox=linux --streams=memory main.nexo
```

```nexo
policy:
    deny shell
use nexo.stream
when kafka("orders") as msg, key: msg.text():
    print(msg.text())
kafka("orders").send("hello")
```

Both CLIs accept exactly one `--streams=memory` option; other stream profile
names are rejected before execution. `check` and `audit` can inspect programs
under this profile without executing them. The trusted launcher setting is
`NEXO_SECURITY_STREAMS=memory-v1`; applications and models cannot set or replace
it through a policy grant. Linux workers explicitly receive that setting.

## Capability contexts

A handler registration captures the active immutable permission stack. The same
captured ceiling applies to its key callback. Each message also captures its
publisher's current context. Delivery intersects those snapshots with the current
caller and any lexical module ceilings, rather than replacing them. A privileged
root handler cannot widen a restricted publisher; a root publisher cannot widen
a handler registered by a dependency, tool or spawned task.

Every handler has its own context guard. Completion, deduplication and handled
application errors restore the previous context. Nested sends and concurrent
publishers preserve the existing restrictions. Handler lists are copied before
dispatch, and registry locks are released before invoking user code.

Normal fallible application errors retain the stream's existing behavior: four
attempts total, with backoff, followed by dead-letter history if all fail. Security
diagnostics are different: they propagate immediately, are not retried, cannot
be caught by Nexo `try`, and are not converted into dead-letter messages. The
same rule applies to key functions and broker budget failures.

Sequential successful keys are remembered in memory. This is not a distributed
exactly-once protocol, and concurrent publishes of the same key still require
application idempotency. Registration captures, message contexts and key state
are process-local and are never deserialized from an untrusted log.

## Data and retained history

Payloads and key results must be scalar data or materialized data containers.
The existing depth/node limits apply. Callable, deferred, record, secret-wrapper
and other opaque authority-bearing values are rejected before delivery. Regular
collection pipelines can be used on decoded payload data inside a handler.

Memory source names must be 1..256 UTF-8 bytes of letters, digits, dots,
underscores or hyphens. They are identifiers, not filesystem paths. Remote broker
options and WebSocket sources remain unsupported.

Protected message history has fixed limits:

| Bound | Limit |
| --- | --- |
| Serialized UTF-8 payload of one message | 1 MiB |
| Retained payload history across all streams | 8 MiB |
| Retained messages across all streams | 4096 |

Dead-letter payloads count toward the same history limits. A publish that would
exceed a history limit is rejected before append or handler delivery. These are
retained-message limits. [Process resource budgets](shield-budgets.md) separately
limit registrations and deduplication admissions/bytes; neither set measures total
process RSS, CPU or arbitrary application allocations. History is retained for the job lifetime and discarded at exit.

## Persistence and unsupported lifecycles

Shield rejects persistent stream construction and send paths without the
explicit memory profile, even for a bare `kafka(...).send(...)` call. Previously,
that path could create `.nexo/streams/...` files without application write grants;
the runtime now rejects it before accessing broker storage in both engines.

In the memory profile there are no broker logs, offsets, key files, persistence
or cross-process replay. Delivery finishes inside `send`, and the program exits
normally instead of starting the ordinary infinite broker listener. The profile
works inside compatible single-thread Linux workers without enabling threads or
network access; unsupported kernels still fail before payload execution.

## Deferred pipelines and windows

For example, run this with `nexo security run --streams=memory app.nexo`:

```nexo
fn show(n):
    print(n)
kafka("orders")
  |> window 1m
  |> count
  |> each show
kafka("orders").send({"id": 1})
```

The final flush prints `1` without creating broker files.

The memory profile supports stream pipelines, including `window`, `sliding`
and `session`. Each pipeline step captures its registration context. Execution
intersects **all step contexts and every contributing message's publisher
context**, plus the current caller. The resulting ceiling applies to decoding,
mapping, grouping, aggregation and terminal `each` callbacks. A later publisher
or a flush at application exit cannot discard an earlier publisher's restrictions.
Security diagnostics propagate to the job without retry or dead-letter handling.
Ordinary callback failures retain the existing pipeline reporting behavior.

`sliding` runs synchronously for each message; tumbling and session windows close
on the next message crossing their boundary. After joined tasks finish, normal
memory-profile execution takes one snapshot of registered queries and flushes
open tumbling/session windows once, then joins tasks started by those callbacks.
This is a single final flush, not an event loop: new queries/windows created by
flush callbacks are not repeatedly drained to quiescence. No wall-clock timers,
background listeners or persistent state are enabled. Query/window mutation is
synchronized; callbacks execute outside locks and may publish recursively.
Concurrent callbacks have no ordering guarantee. Durations must be at least one
millisecond. Retained history limits above also bound the source messages; they
do not alone bound query count or duplicate window references. Pipeline builders
also consume the cumulative process registration budget.

General `when` events, timers, lifecycle hooks and servers remain outside this
stream profile. Outside Shield, the ordinary file broker and stream/window test
behavior retain their existing contract.

This is capability attenuation, not automatic data confidentiality or taint
tracking. Topic names share the application namespace; there is no per-topic
subscription ACL. Trusted code controls which plaintext it publishes. Signed
publisher provenance, external broker credentials, subscription policies and
sensitivity-label propagation remain separate work.

## Validation

```sh
cd compiler
cargo build --locked --workspace
cargo test --locked --workspace
cd ..
pytest -q tests/test_security_streams.py tests/test_security_dispatch.py \
    tests/test_stream.py tests/test_security_events.py tests/test_security_spawn.py
```

Tests reproduce the former ungranted file-write path and verify no broker files
are created. They cover publisher/registration/key ceilings, nested and concurrent
sends, spawned registrations, agent/tool boundaries, ordinary retries, fatal
security diagnostics, name validation, data-only boundaries, budgets and Linux
profile configuration. Ordinary persistent-broker interoperability tests and
language conformance check compatibility independently of the new memory mode.
