# AI-native Phase 3 completion record

Date: 2026-10-30. D184–D189 implement the bounded AI/agent runtime increment in
the interpreter and Rust/LLVM runtime. It reuses providers, typed JSON conversion,
sealed Shield dispatch, task contexts, approval and D151 checkpoint commit hooks.
No grammar or dependency was added. Phases 4–5 remain future work.

## Delivered contracts

| Requirement | Implementation and evidence |
| --- | --- |
| Typed inputs/outputs | `input_type` checks existing values; `as:` validates final JSON. Private input fields stay hidden. Invalid output never replays a conversation. |
| Tools and authorization | Malformed calls/extra parameters are rejected; confirmation precedes typed conversion/defaults. Root/caller/module/agent/tool ceilings remain intersected. |
| Time and cancellation | Irreversible controls and monotonic deadlines follow captured tasks/nested work and gate protected operations. Already-admitted operations can finish. |
| Token budgets | `max_tokens` covers reported input plus output through inherited AI calls, repairs and streams. Atomic reservations prevent concurrent reuse. Durable write-ahead counters preserve late child receipts across restart. |
| Monetary budgets | Legacy process-local CSV reservations plus explicit encrypted cross-process operator accounts. Unknown charges are never refunded merely because a request failed or a process died. |
| Recovery | Explicit private state grants; bounded AES-256-GCM state, pinned directories, no-follow opens, private files, locks, fsync and atomic replacement. Identity includes source, input, target and actual execution permission contexts. |
| Approval/retry safety | Completed calls/results are not replayed. Pending approvals need a fresh decision. Uncertain provider/tool dispatch, failed runs and step exhaustion refuse recovery. Durable transport retries are disabled. |
| Tracing | `NEXO_AGENT_TRACE=stderr` enables a closed OTLP JSON profile; default off. Runtime spans validate against the checked-in schema and exclude application data/identifiers. |

The shared tests use only local Ollama/OpenAI/Anthropic/Gemini-shaped mocks and
fake credentials. They cover malformed/finite JSON, invalid options and types,
privacy, denial, cancellation from another task, deadlines inside tools, nested
and spawned control, concurrent budget admission, strict receipts, crash around
dispatch/approval, stale checkpoints after child receipts, state tampering,
changed source/input/target/grants, and cached results under narrower callers.

## Validation

- **313 acceptance checks verified** across both Linux backends and profile/account/bundle tests. The full run passed 311; two tests stopped at an accidentally interpolated `"{}"` literal. That fixture was changed to `"tampered"`, and both cases then passed with the same permission/error/unchanged-file assertions. Runtime sources were unchanged by this correction.
- **370 compatibility tests passed**, covering providers, MCP/approval, Shield task/event/stream contexts, resource budgets and D151 recovery. The same **2** existing kernel-profile cases remain deselected under Rosetta.
- **150/150 compiler conformance/example checks passed**.
- **228 differential cases:** 218 identical, 10 previously documented differences, **0 unexpected**. No difference was added or relaxed.
- **84 static/effect/JSON/catalog/schema/account-identity tests passed** locally.
- **66 Rust workspace tests passed, 1 existing test ignored**, using `cargo test --locked --workspace`.
- Local interpreter acceptance passed; the foreign-owned private-directory case is skipped on this macOS host and covered in Linux. **40 LSP/task-context/bundle checks passed** locally.

No live-provider certification or performance improvement is claimed. The
historical D184 and D185 records remain linked separately; the results here
cover their final integrated implementation as well as D186–D189.

Runtime image:
`sha256:3278e942ffc2e7aa78be211bfa953433d2a24030ceeae926a65e7d61ea9f5b5a`.
Final test image:
`sha256:40d4645970677551199063033b435ff26c4832e8c58c41c64a8c413e395af97e`.
The working tree starts from `b9b0e41` on `main`. These images use the pinned
cached Shield build toolchain and an offline locked release workspace build;
all final compiler/worker/runtime artifacts replace the earlier validation
image's artifacts. No host compiler/archive is mounted. Inherited old Rust test
executables are not used as final-source evidence; Rust results above come from
the current local sources. Completion documentation was finalized after the
runtime/test image was built.

Session logs are local `/tmp/nexo-phase3-*.log` artifacts, including
`linux-final`, `manifest-final`, `linux-regressions`, `conformance`,
`differential`, `static`, `rust-tests` and `tools`.

## Architecture and files

- `nexo/stdlib/ai.py` and `compiler/runtime/src/ai.rs`: typed agent boundaries,
  shared request reservation/receipt paths and bounded recovery integration.
- New mirrored `agent_control`, `agent_tokens`, `agent_finance`, `agent_state`,
  `agent_journal`, `agent_trace`, `ai_budget` and `checkpoints` modules: inherited
  admission, accounting, confined storage, protocol, telemetry and commit hooks.
- Authorization/policy/security-context modules: explicit version 2 state grants,
  immutable authority inputs and captured controls/accounts. Existing permission
  and generic workflow guards remain in force.
- Shared error kinds/catalogs and LSP completion lists: `Cancelled` and control
  API parity. The playground interpreter bundle is regenerated.
- `tests/test_agent_lifecycle.py`, `test_agent_tokens.py`, `test_agent_recovery.py`,
  `test_ai_budget_reservations.py`, profile/account unit tests and Shield's
  validation entrypoint carry the acceptance cases.

See [lifecycle](agent-lifecycle.md), [token accounting](agent-token-budgets.md),
[monetary reservations](ai-budget-reservations.md), [operator recovery](agent-recovery.md)
and [tracing](agent-tracing.md) for exact API and operational semantics.

## Reproduction and limits

Build the normal locked Shield validation target, then run the acceptance files:

```sh
docker build --platform linux/amd64 -f docker/shield/Dockerfile \
  --target validation -t nexo-shield:phase3-validation .
docker run --rm --platform linux/amd64 --network none --cpus 3 \
  --memory 3g --pids-limit 256 --entrypoint /opt/nexo-venv/bin/python \
  nexo-shield:phase3-validation -m pytest -q -o cache_dir=/tmp/pytest-cache \
  tests/test_agent_lifecycle.py tests/test_agent_tokens.py \
  tests/test_agent_recovery.py tests/test_ai_budget_reservations.py \
  tests/unit/test_agent_trace_schema.py tests/unit/test_agent_account_identity.py \
  tests/test_playground.py::test_the_bundle_is_built_from_the_interpreter
```

This session uses macOS plus Linux AMD64 containers under Rosetta. Existing host
Linux linker flags prevent native Nexo program execution on macOS; Linux
production compiler/runtime artifacts are used for native program tests.
Container runs are UID 10001, without external network or host mounts. This is
API/runtime evidence, not positive Landlock/seccomp/kernel-isolation evidence.
The existing loopback-profile approval test remains excluded in compatibility
runs because the required kernel facilities are unavailable under emulation.
No guard or assertion is relaxed to accommodate that limitation.

Cancellation is cooperative, not syscall/CPU/terminal preemption. Native durable
state currently requires Linux AMD64; Python requires Unix primitives. OS
profiles do not gain mounts or key access from a state grant. Durable input is
text/none; result defaults, non-record user types and durable MCP tools are
rejected. Generic Shield workflows remain rejected. No exactly-once external
effects, global provider-invoice ceiling, arbitrary external-client metering or
protection against a hostile operator/same-UID host process is claimed.
