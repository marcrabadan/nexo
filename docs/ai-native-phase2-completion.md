# Phase 2 completion: conservative effects and configured ceilings

Implemented on 2026-10-29, based on commit `384b1ed`. This completes the
Phase 2 conservative analysis requirements; it does not implement Phases 3–5.
The original first-slice measurements remain in
[the historical record](ai-native-phase2-validation.md).

## Acceptance evidence

| Requirement | Implementation and verification |
| --- | --- |
| Function effect propagation | Linked function/module units, fixed-point propagation through direct, recursive, imported and unambiguous record-method calls. Origin locations and relevant caller identities survive propagation. |
| Explicit permission requirements | Existing policy kinds are reported per effect. The same runtime matcher/engine compares known targets; absent grants and explicit denies are errors. No new permission syntax or automatic grant is introduced. |
| Imported module and caller ceilings | The existing strict dependency loader is reused. Callee, transitive caller and captured top-level registration contexts are intersected conservatively; tests cover a permitted leaf called through a denied importer. |
| Tool/agent boundaries | The existing authorization loader and immutable engines are queried without entering runtime scopes. Configured tool/allowed-agent comparisons and allowlist absence are explicit. Source registration roles are verified separately: a tool named like an agent cannot stand in for that agent's registration. |
| Human approval | Metadata identifies required approval, including pure configured tools; analysis never approves or prompts. Actual seals, allowlists, approval and runtime ceilings remain enforced by Shield. |
| Unknown/dynamic effects | Unresolved parameters/callbacks, mutable/conditional aliases, opaque methods/constructors, providers and deferred registration remain explicit unknowns. `--require-complete` rejects incomplete analysis. Unsupported flags cannot fall through to model-backed intent checking. |
| Compiler diagnostics | Both backends reuse the diagnostics v1 envelope, catalog messages and LSP UTF-16 ranges. Effect metadata is version 2, with a checked-in schema, scoped comparisons and approval requirements. |
| Bounded deterministic analysis | Shared scopes, sorted/deduplicated facts and bounded fixed points. Alias expansion stops at 32 links. Retention is capped across modules and functions; fact/context and registration closure exhaustion becomes `analysis_limit`, never purity or permission. |
| Allowed/denied regressions | Backend parity covers grants, deny precedence, missing grants/allowlists, canonical identities, imported/registration ceilings, immutable callback/literal aliases, record methods and symlink denials. Runtime denials/state restoration remain independently checked. |
| Compatibility | Ordinary check/build/execute behavior, grammar, dependencies, runtime contracts and enforcement are preserved. Unknowns remain advisory by default; strict completeness is opt-in. |

This is a bounded conservative analysis, not a claim of full symbolic evaluation,
effect polymorphism or unrestricted static proof for every Nexo construct.
Explicit unknowns are an intentional result required by this phase. Runtime
verification remains mandatory even for a complete static summary.

## Implementation changes

- `nexo/effects.py` / `compiler/nexoc/src/effects.rs`: immutable alias and literal
  resolution, method summaries, call-path provenance and captured registration
  closure, including explicit unresolved/ambiguous registration outcomes.
- `nexo/effect_policy.py` / native `effect_policy.rs`: read-only adapters over
  the existing Shield dependency and authorization loaders/engines.
- Runtime authorization/context modules: read-only inspection methods only;
  active state, scope transitions, dispatch enforcement and approval are unchanged.
- CLI/diagnostic drivers: explicit manifests, strict completeness, option
  validation before intent dispatch, human denial provenance and approval notices.
- `docs/effects-v2.schema.json`, effect guide, specification, decisions D183,
  diagnostics guide and roadmap: current contract and deliberate model limits.
- `tests/test_effects_completion.py` and shared JSON tests: complete-phase
  regressions, metadata validation and prebuilt-compiler parity in Linux images.
- Docker validation: include effect models in compiler builds; make public CA
  directories traversable while retaining TLS verification; include the complete
  filtered checkout and Git for repository/CI examples; provide expected artifact
  paths as aliases to the freshly built production binaries/runtime archive.
  No host source, Docker socket or credentials are mounted into validation runs.
- Regenerate the deterministic interpreter playground bundle.

No language/runtime dependency was added. Git is a validation-image tool required
by the existing CI tests, not a production runtime dependency. There is no new
network call, telemetry, provider invocation or untrusted code execution in the
analysis command. Docker downloads/builds are explicit validation setup.

## Validation results

The definitive local focused run passed **420 tests**: Phase 2 completion and
original effect tests, Phase 1 JSON CLI, LSP, Python units, `docs --for-ai` and
the generated playground bundle. Both available backends were exercised.

Local `cargo test --locked` passed **63 tests**, with one existing benchmark
ignored. `git diff --check` and formatting checks for the edited/new analysis
modules passed. Broad legacy formatting differences were not rewritten.

Linux evidence is recorded separately from kernel-isolation claims. Validation
uses a disposable non-root Linux AMD64 image under Rosetta, with no external
network, no host mounts, a four-CPU/four-GiB limit and a 512-process ceiling.
The kernel probe explicitly reports unavailable Landlock, seccomp, openat2 and
pidfd support; unsupported workers fail closed. Positive native-kernel isolation
is **not** claimed on this emulated host.

The first broad Linux run, before validation-layout repairs, produced:

- Python: **1860 passed, 128 failed, 230 skipped, 10 errors**. Missing examples
  and Git explained repository/CI fixture failures; worker/ingress failures
  reported Rosetta mapping errors under real limits and unavailable syscalls.
- Rust: **65 passed, 2 failed, 1 ignored**. The two failures were pidfd signaling
  and seccomp/limit enforcement under the emulated kernel. These checks were
  retained; neither protections nor assertions were disabled.
- Compiler conformance: **134/134 passed** for the cases present in that initial
  image. The image lacked the top-level examples, so this is not the full 150-case
  conformance result. An earlier isolated run had one transient web-output
  mismatch; the web subset and subsequent conformance run passed without a
  production code change.
- Differential: **218/228 identical, 10 known differences, 0 unexpected**.
- The final effect implementation's dedicated Linux tests passed **67 cases**
  before the final invalid-option/no-model regression was added. Subsequent
  corrected-image results are recorded below.

The final corrected image is
`sha256:4dda0486c1d3011124c3a1cb407e756cc7ca7bafa928362b35f4ed3910bb2f7b`.
The expanded corrected-image regression run finished **565 passed, 16 skipped,
1 failed** in 308.22 seconds. It includes effect/completeness/JSON tests, units,
LSP, documentation/playground, repository/CI examples and runtime security API
checks. The remaining failure is
`test_infra.py::test_a_cluster_with_its_own_certificate_authority[interpreter]`:
the generated test CA lacks a `keyUsage` extension required by the image's
Python TLS verifier. TLS verification remains enabled; neither production TLS
behavior nor this unrelated fixture was weakened to obtain a green result.

Its full conformance run reached **149/150**, with the same intermittent
`s15_web` output mismatch (exit 0). Repeating the complete web subset passed
**6/6** without a source change. The full run is therefore recorded as one
failure, not rewritten as a clean 150-case pass. The final-image differential
comparison again passed with **218/228 identical, 10 known differences and
0 unexpected differences**.

Raw session logs are `/tmp/nexo-phase2-*.log`. They are local artifacts and are
not committed. Full kernel-profile validation still requires a real supported
Linux x86-64 kernel; an emulation failure is not treated as sandbox success.

## Benchmark strategy and data

`compiler/bench/effects.py` measures chain, mutually recursive and imported graphs
at 1k/10k/100k lines, with three repeats per backend. It records corpus hashes,
compiler hash/toolchain, median/p95 elapsed time, fresh-child peak RSS, output
bytes and incomplete/limited summary counts. The completion baseline is stored
in `compiler/bench/effects-completion-baseline.json`; the first-slice baseline
remains historical evidence. All **54 final samples** passed the complete/limit
assertions. The final debug compiler SHA-256 is
`40a3235f5a6cb5ac064ecab4603eca1018b3ef5ead7746d7d133c5a72e5c53f6`.
Median seconds for this local debug build (three repeats; measurements shared
host resources with validation workloads):

| Graph | Lines | Interpreter | Compiler |
| --- | ---: | ---: | ---: |
| Chain | 1k / 10k / 100k | 0.174 / 1.080 / 5.611 | 0.136 / 0.947 / 2.381 |
| Recursive | 1k / 10k / 100k | 0.169 / 1.218 / 6.066 | 0.111 / 1.203 / 2.837 |
| Imported | 1k / 10k / 100k | 0.272 / 1.459 / 10.945 | 0.116 / 1.300 / 2.160 |

Measurements include front-end and JSON cost, not
execution of generated programs.

At 1k/10k all three modeled corpora must have complete, unbounded summaries. At
100k the retained-unit ceiling must be explicit for every retained summary;
partial results are discarded. No cross-host timing guarantee or production
performance improvement is claimed. Operator-loaded graphs additionally bound
caller/registration context memberships and preserve unknowns on exhaustion.

## Intentional limits and next phase

The analyzer does not specialize arbitrary external function arguments, solve
all higher-order flows, prove opaque native/trait/constructor behavior or resolve
every provider's runtime configuration. Those cases are explicitly unknown,
never silently pure or authorized. Filesystem/configuration comparisons are
snapshots and cannot authorize a later changed artifact or filesystem. A static
comparison is not an execution seal, approval token or OS-isolation guarantee.

Phase 3 can now build on this foundation for typed agent boundaries,
cancellation, coordinated budgets, secure durable workflows and opt-in tracing.
No Phase 3 runtime feature is included in this change. Changes remain local for
review; no commit, push, merge or deployment was performed for this task.
