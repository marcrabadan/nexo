# Shield process-scoped spawned tasks

`spawn` and `use nexo.sync` now work with dependency ceilings and operator
agent/tool authorization outside the single-thread Linux worker profiles.
Both runtimes capture the caller's immutable permission stack at launch and
install it on the new thread before executing Nexo code. Nested spawns inherit
that complete stack too. A task cannot regain root permissions by waiting until
its launching function, tool or agent has returned.

```sh
nexo security run --dependencies permissions.json main.nexo
nexo security run --authorization authorization.json main.nexo
```

The existing spawn lifetime contract remains: a call launches asynchronously,
and the program waits for every spawned task, including descendants, before
normal shutdown. Task state and deadlines retain their existing language
semantics. A failed thread start restores task accounting and reports a failure;
unhandled task errors terminate the program. Context guards restore the thread's
previous state before normal completion is published. Runtime worker unwinds
cannot leave the program waiting for a task that no longer exists.

There is no new task handle or per-task cancellation API.
[Process resource budgets](shield-budgets.md) bound cumulative task submissions
and live runtime threads across spawn and native parallel work.
These are application-level capability checks, not OS confinement or protection
against CPU/RAM exhaustion. The offline and online Linux worker profiles continue
to reject `spawn`, `parallel` and sync imports before executing the payload,
because their syscall filters prohibit threads. [Synchronous events](shield-events.md)
retain both handler and emitter contexts, including on spawned threads. [Memory streams](shield-streams.md) also preserve publisher and handler contexts.
Persistent stream listeners, servers and pipelines remain outside the supported
lifecycle.

## Data crossing task boundaries

When either security manifest is configured, channel messages and shared values
must contain scalar data or materialized lists, maps and sets of data. Checks are
bounded to 100,000 visited nodes and depth 128. Callable values, deferred
computations, records, constructors, channels-as-message-values and other opaque
handles are rejected before publication. Shared reads are checked as well, so a
receiver cannot consume an unsealed value through an alternate access path.
Checks at compiled shared read/store boundaries immediately propagate a fatal
diagnostic before the next Nexo operation.

Normal scalar/container messages, channel closure, `select`, shared data and
function/channel arguments to `spawn` remain usable. Spawn arguments are not a
separate authority delegation: the child executes under the captured caller
ceiling, plus the existing lexical module checks. Ordinary execution without
either manifest retains its previous record/callback transfer behavior.

This is not taint tracking or automatic authority propagation for arbitrary
application-defined queues. A privileged pre-existing worker that interprets
untrusted data as instructions must authorize that dispatch independently.
Plain data does not itself grant permission, and sending a message does not
attenuate the receiver's own root policy. General sealed callable/native RPC
and data-label propagation remain separate work.

## Agents and human approval

A tool may launch a worker under its existing operator grant. The worker keeps
that grant after the tool response and agent invocation complete. Nested agents
still intersect all captured and active permissions. Approvals are serialized
across threads: one trusted input response applies to the one currently shown
call, and confirmation never expands the agent/tool ceiling. Linux control/broker
stdin remains unavailable for human approval.

## Validation

```sh
cd compiler
cargo build --locked --workspace
cargo test --locked --workspace
cd ..
pytest -q tests/test_security_spawn.py tests/test_security_dispatch.py \
    tests/test_security_isolation.py tests/unit/test_sync.py tests/test_ai.py
```

Tests coordinate real threads with channels rather than sleeps. They cover a
restricted launcher returning before its root callback executes, nested spawns,
process shutdown waiting for descendants, forbidden effects remaining absent,
uncatchable task violations, data-only transfers, shared mutations, thread-start
failure accounting and concurrent agent approvals. Ordinary sync/parallel tests
and language conformance verify compatibility. Docker validation includes these
tests without relaxing its network or syscall configuration.
