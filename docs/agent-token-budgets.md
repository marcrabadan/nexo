# Agent token budgets (D189)

Declare an invocation allowance on the existing agent block:

```nexo
agent reviewer:
    goal = "Review the supplied text"
    max_steps = 4
    max_tokens = 2000
```

`max_tokens` must be a positive integer. It counts the provider's reported
**input plus output** tokens across that invocation, including repeated
conversation history on later turns. Each new independent invocation gets its
own allowance. It does not meter arbitrary raw HTTP clients or opaque external processes.
This is separate from monetary accounts, Shield resource limits
and the existing `ask(max: N tokens)` output parameter.

The allowance follows provider work through Nexo’s AI APIs and the existing
captured security contexts into tools,
nested agents, `ask`, typed repairs, streams and spawned tasks. Nested limits
can only reduce the remaining allowance. A request reserves all remaining
headroom under a shared lock; a competitor receives `AIError` rather than
spending it simultaneously. The provider output parameter uses the actual
reservation, or an existing smaller output limit. HTTP transport retries are
disabled while a token reservation is active.

Strict nonnegative integer input/output counts reconcile each received reply.
Malformed/missing/ambiguous usage is not zero. Unknown requests, failed
transport and abandoned streams retain their holds. Valid counts are accounted
before returning a result, and an excess raises `AIError` before the agent
admits any returned tool call. Known monetary cost and ordinary usage accounting
are retained even if the token allowance is exceeded. An earlier admitted tool
or syscall can still have effects; token exhaustion is not rollback.

No provider-neutral input tokenizer is guessed. A provider may ingest input or
report actual usage above the allowance; the runtime rejects the reply and
future requests, but cannot undo that charge. This is cooperative admission and
reported-usage enforcement, not a guaranteed provider-invoice ceiling. Streams
can yield partial text before their final usage receipt; headroom stays held
until successful completion. Treat partial model text as untrusted data.

## Durable recovery

With a [state-granted run](agent-recovery.md), each token counter also has a
namespaced encrypted ledger. It pins the run/target identity and limit. Under a
file lock, the runtime reads the latest debit, saves a pending hold before HTTP,
and saves settlement before delivering a receipt. Checkpoints retain a snapshot
for structural validation, but never replace newer ledger debits. This prevents
an async child receipt arriving after an approval checkpoint from being erased
by a restart.

Unknown pending ledgers, malformed counters, changed limits and missing ledgers
for an existing run fail closed. There is no source reset/refund API. A cached
final result makes no provider request. Operator reconciliation is required for
uncertain dispatches, as with the monetary account; do not delete accounting
files to regain headroom. Generic Shield workflow persistence remains rejected.
