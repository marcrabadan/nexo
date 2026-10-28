# Shield process resource budgets

Shield API enforcement now applies fixed process-wide admission budgets in both
engines. They are enabled by `security run`, dependency/authorization enforcement,
or the explicit memory-stream profile. No new flag or writable configuration is
required. Ordinary execution keeps its existing behavior.

| Resource | Process limit | Charging point |
| --- | ---: | --- |
| Task submissions | 4096 | Each `spawn`, parallel statement or parallel-for input item |
| Additional runtime threads | 32 live | Shared by spawned tasks and native parallel workers |
| Registrations | 1024 cumulative | Each event/stream handler, web server/route, deferred pipeline step or window constructor |
| Web server admissions | 16 cumulative | Before server registry insertion |
| HTTP memory dispatch admissions | 4096 cumulative | After validation, before route processing |
| Channels | 256 cumulative | Successful channel admission |
| Declared buffered channel capacity | 65536 slots cumulative | Sum of admitted channel sizes |
| New deduplication-key admissions | 4096 cumulative | After computing an unseen key, before its handler |
| Deduplication key text | 1 MiB cumulative | UTF-8 bytes of the rendered admitted keys, including representation punctuation |

These are one set of counters for the entire process, shared across all modules,
agents, tools, spawned tasks, nested parallel blocks and registries. Completing a
task, discarding a channel/query or resetting stream/test state does not replenish
cumulative budgets. Admission failures produce the fatal `security_job_budget`
diagnostic; they cannot be handled as ordinary language failures or converted to
stream retries/dead-letter processing. Atomic admission checks cannot partially
charge a multi-resource request. Successfully charged admission attempts are not
refunded if later argument binding, callback processing or OS thread creation fails.

Thread permits are the exception to cumulative charging: a permit is held for a
worker's lifetime and released on completion or thread-start failure. A refused
`spawn` fails immediately. Native `parallel` uses available permits and runs the
remaining work on its calling thread, preserving task results/output ordering.
The interpreter still executes parallel tasks sequentially. The main thread is
not included in the 32 additional-thread limit. HTTP broker subprocesses and
library-owned threads are outside this runtime-thread counter.

Parallel-for inputs are collected incrementally with submission admission before
retaining each input. Oversized inputs stop before executing their task bodies,
including lazy/range inputs; input expressions and iterators can themselves have
effects during evaluation. Parallel blocks reserve all their statement admissions
before any of their task bodies run. Nested work uses the remaining process budget.

Event/stream handlers and pipeline builders use the same registration budget.
Even discarded, unregistered query stages consume admissions. Channel capacity
is charged at construction rather than only when filled; unbuffered channels
consume a channel admission and zero declared buffered slots. Queued payload
bytes and arbitrary application allocations are not measured by these counters.

Duplicate already-processed stream keys do not consume new admissions. An unseen
key reserves count and bytes before its processing handler runs; computing the
key itself can run user code first. Reservations remain charged after ordinary
processing failure. Concurrent attempts using the same not-yet-committed key may
consume separate admissions and execute more than once; applications still need
idempotent handlers. This does not add exactly-once concurrent delivery.

The existing [memory-stream history limits](shield-streams.md) remain process-wide:
4096 messages, 8 MiB retained payload text, 1 MiB per message. History and dedup
budgets are independent and include all topics/handlers.

These controls bound admission to specific runtime resources. Total RSS, CPU,
wall-clock execution, recursion, general data-structure size and library thread
usage need their own limits. Existing Linux worker rlimits and container limits
remain separate. Per-task cancellation, configurable operator budgets and a
persistent server/event-loop lifecycle are still pending.

Validation covers exact boundaries, oversized lazy/range task input, nested and
mixed task modes, live thread rejection and parallel fallback, shared registries,
dependency registrations, channel totals, key count/bytes before handler effects,
duplicate keys, atomic concurrent accounting, start-failure cleanup and ordinary
execution compatibility. The Docker validation matrix includes the budget tests.

[In-memory HTTP routes](shield-web.md) reuse these process counters without enabling listeners.
