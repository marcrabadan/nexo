# Agent lifecycle — Phase 3 (D184–D189)

The lifecycle combines typed final answers, strict tool boundaries, inherited
cooperative cancellation/deadlines, budget reservations, operator-granted
checkpoints and opt-in redacted tracing in both runtimes. It reuses the existing
providers, JSON/type conversion, tool registration, Shield seals, approval and
D151 checkpoint commit machinery. No grammar or dependency is added.

## Typed inputs

An agent can declare `input_type = Request` (a record or builtin type). Before
provider dispatch, `handle` checks the existing value using `value is Type`;
a mismatch raises `InvalidData`. This guard does not cast, deserialize JSON or
execute defaults. Construct/parse typed inputs explicitly using the existing
language APIs. Without this key, inputs retain their existing behavior.
Private record fields remain hidden from model input. Durable runs currently
accept text/none only.

## Typed final answers

```nexo
type Finding:
    answer: str
    score: 1..5

agent reviewer:
    goal = "Review the supplied input"
    max_steps = 3

finding = reviewer.handle("Review this text", as: Finding, timeout: 5s)
print(finding.answer, finding.score)
```

Configure a provider as before. `as:` accepts a record type or a builtin type
supported by the existing JSON converter. Record fields retain their existing
validation, defaults and optional-field semantics. `as: str`, an omitted `as:`,
and `as: none` keep the existing plain-text result. Other type values are
rejected before a provider request rather than treated as unchecked JSON.

For a typed answer, the provider receives the existing JSON-shape instruction;
the final text is extracted with the same JSON/fence handling as typed `ask`
and validated locally. Malformed JSON or nonconforming fields fail with `InvalidData`; existing
overflow or executable-default errors retain their normal error kinds. There is
no automatic retry of the agent conversation: tools may already have produced
side effects, so replaying the conversation is not a safe repair strategy.
A valid schema does not establish the truth or authority of model content.

## Cooperative deadlines

`timeout:` is optional. It must be a positive duration no greater than 24 hours;
invalid values fail before a provider request. The monotonic deadline starts
when the runtime enters `handle`, after ordinary argument evaluation. It applies
to this invocation and follows captured contexts into its tools, nested agents
and tasks. It is not a process deadline or a hard task termination mechanism.

The runtime checks admission before a provider turn, after its response, before
each tool dispatch, after approval and before typed conversion/function entry,
and before returning a final answer. Provider transport timeouts use the lesser
of 60 seconds and the earliest remaining inherited call time. Calls with an explicit deadline do
not automatically retry the provider HTTP request. Transport failures observed
after expiry become `Timeout`; earlier network failures retain their normal
error kind. Without a deadline, the existing provider timeout/retry behavior is
preserved.

This is **cooperative admission control, not preemptive cancellation**:

- Protected runtime API admission checks observe inherited cancellation and
  deadlines. A tool admitted earlier cannot begin another protected operation
  after closure. An operation whose admission check already succeeded may
  finish; cancellation does not roll back its effects or preempt a syscall.
- CPU work, approval-lock waits and blocking terminal input are not interrupted.
  Checks inside the lock prevent a new prompt after expiry; an answer read after
  expiry/cancellation cannot approve conversion or dispatch.
- MCP discovery/process lifecycle do not gain hard interruption guarantees.
  The interoperability audit remains Phase 4; durable MCP tools are rejected.

## Cancellation controls

```nexo
control = reviewer.control()
control.cancel()
try:
    reviewer.handle("Review this text", cancel: control)
else e is Cancelled:
    print("Review was cancelled.")
```

A control is independent of its creator and can be passed to another task or
agent. `cancel()` is irreversible; `cancelled` reports its state. A fresh control
is needed for deliberate new work. Cancellation before entry prevents provider
requests. A separate task can cancel an in-flight request; after it returns,
no tool is admitted. Captured security contexts carry controls/deadlines into
spawned work and nested agents without granting permissions. Scope restoration
keeps unrelated caller operations independent. Closure linearizes at the
cooperative admission check, not at physical completion of an earlier operation.

Handle `Timeout` explicitly and inspect any admitted effects before retrying:

```nexo
try:
    reviewer.handle("Review this text", as: Finding, timeout: 5s)
else e is Timeout:
    print("Review did not complete; inspect admitted work before retrying.")
```

## Tool-call and approval boundaries

Agent/provider JSON is checked against the existing nesting bound and rejects
nonfinite numeric values. This does not change the general JSON API’s existing
numeric extensions.

All supported provider adapters reject non-object tool arguments, malformed
call metadata and duplicate call IDs within one response before dispatching any
call from that malformed response. Absent arguments retain their existing empty
object default; provider-specific explicit-null behavior is not expanded into a
new grant. Unknown tools still produce the existing error result for the model.
Duplicate-ID checking is local to a response, not durable replay protection.

For a registered Nexo tool, unexpected parameter names produce a tool error
before defaults or the function body run. Existing declared-parameter type
conversion and missing-parameter handling remain in use. This does not introduce
a globally stricter JSON schema or change how unrelated JSON records are parsed.
MCP tools retain their separate schema/dispatch contract.

When confirmation is required, the prompt now always shows proposed raw
arguments **before** typed JSON conversion, in ordinary execution as well as
operator-configured execution. Record defaults can execute code, so refusal
must prevent their evaluation too. Approval remains serialized, applies only
to the displayed call and never widens policy. Expiry is checked after the
answer before conversion or dispatch. There are no new approval tokens.

Shield still intersects actual application/caller/module/agent/tool contexts.
Generic persistent workflows still fail with `security_workflow`. The separate
[operator-granted agent protocol](agent-recovery.md) uses confined checkpoints;
no generic persistence guard is removed. Effect analysis still reports opaque/dynamic agent behavior as
unknown: adding a result type or timeout is not a static authorization proof.

## Budget reservation increment

D185 adds [conservative process-local budget reservations](ai-budget-reservations.md)
for provider turns, `ask`, typed repairs and streams, using the existing budget
configuration. It does not add a durable billing coordinator or a guaranteed
bound on provider invoices. See [its validation record](ai-native-phase3-budget-validation.md).

## Token budgets, durable runs and tracing

[Invocation token budgets](agent-token-budgets.md) add `max_tokens = N` to the
agent block, covering reported input/output across inherited provider work.

[Operator-granted recovery](agent-recovery.md) adds `run:` to `handle`. It binds
identity and inputs, caches validated final results, saves remaining calls,
requests fresh approval after restart and refuses uncertain dispatch replay.
Its encrypted monetary account coordinates requests across processes and
retains uncertain charges after a crash. It is distinct from legacy CSV budgets.

[Opt-in OTLP JSON tracing](agent-tracing.md) requires `NEXO_AGENT_TRACE=stderr`.
Static run/provider/tool spans contain no application payloads or identifiers;
the exporter is disabled by default. Its closed profile is schema-tested.

See [the completion record](ai-native-phase3-completion.md) for acceptance,
reproduction and explicit limits. MCP/WASI interoperability remains Phase 4.
