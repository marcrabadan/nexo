# Nexo Shield: security foundation

Shield phase 1 centralizes the existing D142 application policy. It adds
strict execution, typed Rust decisions and opt-in structured decision events.
It is **application API enforcement, not an operating system sandbox**.

## Use

```
nexo security check app.nexo
nexo security run app.nexo
nexo security trace app.nexo
nexo security audit app.nexo
```

Both CLIs implement the same interface and require a `policy:` block.
`check` reuses `nexo check` and `policy show`; it validates the linked program
and displays the declared rules without running it. It is not a static proof
that every dynamic access is authorized. `audit` also reuses the existing
D129 C inventory; it is not a dependency vulnerability scanner or a complete
inventory of transitive native calls. `run` runs the program with production
policy decisions without enabling release optimizations or trusting contracts.
`trace` does the same and writes JSON lines to stderr before checked accesses.
Standard output remains program output. Capture stderr separately when
consuming events: ordinary compiler/runtime diagnostics can also appear there.

In Shield execution, undeclared accesses are fatal diagnostics and `deny`
overrides `allow`; `try` cannot catch them. C libraries, Python modules and
shell commands (including CI test subprocesses) are rejected even when explicitly allowed: their confinement
is unavailable in phase 1. Remove the integration or run it separately as
trusted code. Existing development and release execution retain their D142
semantics, including explicit permission to trust native integrations.
`security run` does not claim stronger OS isolation than a release binary.
Unsupported sandbox/resource configuration is rejected by the CLI, not ignored.

## Repository analysis and integration

The Python interpreter walks the linked AST. The Rust compiler resolves and
checks the linked program, generates LLVM IR and links the static Rust runtime.
The parsers lower `policy:` into an initial internal call before other program
statements. D142 already checks reads, writes, database file opens, environment
secret resolution, HTTP/AI requests and each redirect, shell commands and native
imports. Shield reuses these boundaries and the same path/host semantics.
No LLVM optimization paths or language policy syntax are replaced. Shield
compilation skips C-header/Python discovery, including in imported modules, so
an import rejected at runtime cannot first start a native analysis subprocess.

`compiler/runtime/src/security/` contains:

- `mod.rs`: typed `Capability`, `Kind`, `Effect`, immutable `Rule`/`Engine`,
  and `Decision`. String boundary identifiers are converted at the adapter;
  evaluation uses typed rules. The program owns one engine shared by its tasks.
- `matching.rs`: the original D142 component-wise symlink/path resolution and
  hostname matching, moved without changing matching semantics.
- `audit.rs`: deterministic JSON event formatting, local stderr emission and a
  violation counter. No remote services or new dependencies are required.

`policy.rs` adapts runtime values to rules and decisions to existing fatal
section-14 diagnostics and development warnings. The Python policy adapter
mirrors these decisions. Policy initialization is one-shot; user references to
`__policy` are rejected by both lexers, including declarations, aliases and interpolations.
The test recorder is honored only in ordinary development execution. An
inherited `NEXO_POLICY_RECORD` cannot bypass release or Shield decisions.
These were verified gaps in the pre-Shield implementation.

`NEXO_SECURITY_ENFORCE=1` is trusted launcher configuration for strict API
execution; it does not grant capabilities. `NEXO_SECURITY_TRACE=1` enables
local events, including for ordinary policy-bearing executions. Host operators
control these settings and the process; they are outside the threat boundary.
Trace collection is optional and never changes allow/deny decisions.

## Events and data protection

An event contains a constant event name, severity, operation, decision, reason,
a redacted target, cumulative observed violations and a repeated-violation flag
(threshold three). Decisions occur locally; no model or network service grants
permission. Only enabled tracing maintains the observed violation counter.
Repeated events are detection signals, not a new permission or rate limit.

Every target is `[redacted]`: even file names, environment variable names,
URLs and shell arguments can contain secrets or personal data. Events contain
no payload, source text, credentials, query strings or user-defined reason.
This intentionally limits attribution until a reviewed safe-label scheme exists.
JSON lines can be collected by an external observability adapter; OpenTelemetry,
Loki and SIEM exporters are not implemented. Ordinary diagnostics retain the
language's source snippets and requested targets; those are not sanitized audit
events and must be handled as potentially sensitive application output.

D97 secret/key printing and serialization guards, crypto algorithms and
`@private` log/AI redaction remain in place. `@personal` is existing privacy
metadata, not automatic end-to-end taint tracking. No new claim of information
flow protection is made.

## Threat model and limitations

Protected assets are files, network destinations, environment-backed secrets
and process/native execution exposed through supported Nexo APIs. An attacker
can supply Nexo application/module code and runtime arguments, try traversal,
symlinks, redirects, catch denied operations or forge policy initialization.
The main application's literal policy and host/compiler configuration are
trusted. Modules inherit that ceiling and cannot declare another policy or
reset it; importing another module grants no extra permissions. There is no
package registry, signed permission manifest or per-dependency attenuation yet.
An application author allowed to change the root policy can grant permissions;
this is not a sandbox for a hostile author who controls their own policy.

Filesystem checks resolve components and symlinks before opening a path. A
hostile process racing path checks or changing allowed directory symlinks can
still cause TOCTOU escapes. No race-free handle-relative `openat2`/Landlock
boundary is implemented. Runtime-maintained `.nexo/` files, configuration,
source/module loading, server listeners and compiler subprocesses retain D142's
trusted-runtime exceptions. The compiler, runtime, libraries and writable
configuration/state directories must be trusted. Memory-safety guarantees of
existing runtime unsafe/FFI code do not follow from these checks.

Network rules grant hostnames at all ports, not addresses or subnets.
Literal backslashes and ASCII controls are rejected before every HTTP request
and redirect in both engines. The original lexical hostname parser could
approve `http://127.0.0.1:PORT\@allowed.example/` as `allowed.example` while the
Rust HTTP transport connected to `127.0.0.1`; a local-server regression verifies
that the forbidden request no longer occurs. Percent-encode such data instead. Every
HTTP redirect is checked before the next request, but DNS rebinding, proxy
routing and private-address restrictions are **not** enforced. A permitted
hostname can resolve to private or changing addresses. Do not use a hostname
allowlist as an SSRF/IP sandbox. Certificate verification remains enabled.

There is no seccomp, Landlock, namespace, cgroup, privilege dropping, CPU/RAM/
file-size limit or worker isolation in this phase. Direct syscalls and hostile
native code are outside application API enforcement; Shield rejects the Nexo
entry points that would load/execute them. Ordinary `allow c`, `allow python`
and `allow shell` explicitly trust that code and cannot confine its actions.
The API policy runs on existing supported platforms; no platform offers an
OS-isolation guarantee here. This phase must not run mutually hostile tenants
inside one process or be used to execute arbitrary native plugins safely.

## Validation

```
cd compiler
cargo test --locked -j 4
cargo test --release -p nexo-runtime security::tests::benchmark_decisions -- --ignored --nocapture
cd ..
.venv/bin/pytest tests/test_security.py tests/test_policy.py tests/test_http.py tests/test_ci.py tests/unit/test_errors.py
.venv/bin/python compiler/scripts/conformance.py
.venv/bin/python compiler/scripts/differential.py
```

The benchmark measures cached-engine host allow/deny decisions only, with
tracing disabled; it does not measure syscall, path resolution, mutex or event
I/O overhead. Regression tests use real files and a local HTTP server, assert
forbidden side effects do not happen, and exercise interpreter/compiler parity.
Existing policy tests cover release requirements and development compatibility.

## Observed validation results

Cloud validation used Python 3.12.14, Rust 1.99.1 and clang 19.1.7 on a
five-vCPU AMD EPYC 9V74 environment:

- `pytest -q`: 1081 passed, 3 skipped (optional VS Code tests), 711 seconds.
  This full-run snapshot collected 1084 cases; ten further hardening cases were
  added afterward and covered by the focused final run below. Final collection
  contains 1094 cases; the entire suite was not rerun after those additions.
- Final `pytest -q tests/test_security.py tests/test_policy.py tests/test_http.py
  tests/unit/test_errors.py`: 95 passed, 124 seconds, including all 65 Shield
  cases and the URL/probe regressions. An earlier security/CI run passed all
  79 cases; the isolated process-integration matrix passed 8 cases.
- `cargo test --locked -j 4`: 45 passed; the one ignored microbenchmark was
  executed explicitly in release mode and passed separately.
- Final compiler conformance: 127/127 passed. The differential suite passed
  during implementation: 218/228 identical, 10 documented differences,
  zero unexpected differences.
- Final release microbenchmark: 34.7 ns per allowed hostname decision and
  28.5 ns per denied decision (100,000 iterations each). These single-run
  figures vary with load and exclude tracing, runtime adapter/lock overhead,
  filesystem resolution and I/O; they are not an end-to-end performance claim.

The crafted backslash URL test was run before its fix: the Rust program exited
successfully and the prohibited local server received the request. Both backend
regressions pass after the fix, with no server request. This is a reproduced
security defect, not just an audit finding. CI configuration was added in
`.github/workflows/security.yml`; the remote GitHub Actions job was not run
from this environment.

## Phase 2 implementation

[Execution isolation and dependency ceilings](shield-isolation.md) are now
implemented as explicit opt-in profiles. The phase-1 commands above retain their
API-only contract; the linked guide describes the Linux requirements, limits,
synchronous context restriction and current-host validation blocker.

## Original roadmap and remaining work

Phase 2 should establish an explicit OS-isolation contract first: probe Linux
Landlock ABI and seccomp availability; use handle-relative filesystem opens;
resolve and validate addresses on the actual connection, including redirects
and proxies; isolate native integrations in workers; bound workers with cgroups
v2/rlimits and drop privileges. Reject requested guarantees on unsupported hosts.
Dependency contexts should be immutable intersections of the application's
ceiling, caller context and dependency manifest. Contexts must follow callbacks,
threads and imports so re-exporting a helper cannot recover a larger grant.

Phase 3 should implement a tested data-label lattice with the total order
`public < personal < private < secret`. The join is the maximum label of all
inputs, including container members; identity, slicing and transformations
retain the input label. Example test vectors are `join(public, private) =
private`, `join(personal, secret) = secret`, and `join(private, private) =
private`. Functions return the join of their input labels and values read;
control-dependent outputs need the program-counter label too. Only an explicit,
policy-authorized declassification may lower a label. Logs reject `secret` and
redact `private`; external sinks require authorization for the resulting label
and personal-data region obligations. These rules are a proposed contract, not
an implemented propagation mechanism. Existing explicit cryptographic export
semantics need a separate reviewed declassification rule. Until propagation
through calls, serialization, FFI, logs, network and all other sinks is covered,
do not advertise complete taint tracking. Agent/tool invocations should receive
attenuated contexts outside the model, checked before dispatch, with approval
required for named privileged capabilities; model output and retrieved content
never grant capabilities. Existing tools inherit the application ceiling and
existing human confirmation remains; per-agent/per-tool grants, new approval
protocols and exported observability remain future work.
