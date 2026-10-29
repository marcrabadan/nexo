# AI-native Phase 1 implementation and validation

Implemented on 2026-10-29, subsequently committed and pushed as `d71ffca`.
Phase 2 was then explicitly authorized; its first slice and remaining scope
are documented in [the roadmap](ai-native-roadmap.md).

## Architecture decisions

D181 extends the existing check and LSP infrastructure. Both CLIs serialize
catalog diagnostics and existing LSP edits inside a versioned envelope, with
zero-based UTF-16 positions. Imported errors use their actual source file.
Reported usage and UTF-8/read errors are JSON too. Existing human checks and
backend-specific static analysis coverage remain unchanged. No dependency,
grammar, policy, Shield enforcement or native code generation change is made.
`docs --for-ai` remains the exact API source; [the consumer guide](diagnostics.md)
explains how coding agents combine it with diagnostics without executing code
or granting permissions.

## Changed files

| File | Change |
| --- | --- |
| `nexo/cli.py` | Route `check --json`, preserve human dispatch, advertise option. |
| `nexo/diagnostics.py` | Python v1 envelope, source/usage/I/O handling. |
| `nexo/lsp.py` | Extract the existing compile-error serializer for reuse. |
| `compiler/nexoc/src/main.rs` | Native CLI JSON dispatch and help. |
| `compiler/nexoc/src/diagnostics.rs` | Native v1 envelope and error handling using the existing JSON module. |
| `compiler/nexoc/src/lsp.rs` | Extract the existing compile-error serializer for reuse. |
| `tests/test_check_json.py` | CLI, schema, parity, imported fixes, UTF-16, no-execution and sensitive-output regressions. |
| `docs/diagnostics-v1.schema.json` | Normative versioned JSON Schema. |
| `docs/diagnostics.md` | Contract, limitations and external coding-agent workflow. |
| `docs/decisions.md` | D181. |
| `docs/especificacion-nexo.md` | Check/JSON commands and normative contract link. |
| `docs/ai-native-roadmap.md` | Repository audit, architecture, dependencies, invariants, phases and acceptance/benchmark strategy. |
| `docs/ai-native-phase1-validation.md` | This implementation/validation record. |
| `docs/playground/nexo.zip` | Regenerate the deterministic interpreter bundle after Python source changes. |

Pre-existing edits to `README.md`, `editors/vscode/README.md` and the untracked
`Makefile` were preserved; they are outside this change. Phase 1 was pushed
on explicit user request. Nothing was merged or deployed.

## Validation

- `cargo test --locked` in `compiler/`: passed after permitting local test
  sockets outside the workspace sandbox; one existing benchmark remains ignored.
  The first sandboxed attempt failed on two loopback-socket tests.
- `python -m pytest -q tests/test_check_json.py tests/test_lsp.py tests/unit`:
  364 passed, including the final sensitive-output and explicit-dot-path regressions.
- The focused run includes all 15 JSON diagnostics cases, with both backend
  invocations and schema checks without a new dependency.
- `python -m pytest -q tests/test_phase6.py::test_docs_for_ai`: 2 passed,
  confirming the existing API-inventory path in both implementations.
- `git diff --check`: passed. `rustfmt --check` on the new Rust diagnostics
  module: passed. Repository-wide `cargo fmt --all -- --check` fails on broad
  pre-existing formatting differences, beginning in untouched `diag/src/catalog.rs`;
  unrelated files were not reformatted.
- Compiler conformance: 45/150 passed. Differential: 63/228 identical,
  11 known differences, 154 unexpected differences. Native execution is blocked
  on this macOS host by the existing Linux linker library list in
  `link_at` (`-lgcc_s`, `-lrt`, etc.); the reported error is
  `ld: library 'gcc_s' not found`. The compiler binary builds and the new check
  path does not invoke the native linker. These runs do not establish full
  native execution conformance on this host.
- `python -m pytest -q tests/test_playground.py::test_the_bundle_is_built_from_the_interpreter`:
  passed after regenerating the checked-in archive with the existing build script.
- Full sandboxed Python suite: 1303 passed, 440 failed, 309 skipped and 188
  errors. Local server fixtures were denied sockets, alongside native linker
  failures.
- Full Python suite outside the sandbox: 1440 passed, 493 failed, 309 skipped
  in 422.29 seconds. Many failures show the native linker issue above (release
  builds also encounter unsupported `--gc-sections`). Other host/platform
  and cross-backend failures remain and were not all individually triaged.
  This full run caught the stale generated playground archive, which was
  regenerated and its dedicated regression rerun successfully afterward.
  The full suite was not repeated after that artifact-only repair; the focused
  final tests above validate the implementation. A supported Linux CI run is
  still required before claiming full-suite compatibility.

Raw logs for this session are under `/tmp/nexo-phase1-*.log`; they are local
validation artifacts, not committed files.

## Remaining limitations

The Python checker still defers some unresolved-name/type errors until runtime
(D67/D68); Rust diagnoses them during check. Both use the same schema but do
not claim identical static coverage. Check emits the existing first error per
input and does not add lint findings. Existing discovery behavior for hidden
files, unreadable directories and symlinks is unchanged. Diagnostic prose may
contain user-authored identifiers/literals; no source snippets, environment
values or credential configuration are attached. Outputs are not automatically
sent to providers. Successful checking is not authorization or isolation.

Linux worker/container enforcement was not positively validated on this macOS
host. No new effect system, durable agent integration, protocol compatibility
upgrade, standalone WASI backend or Sentinel implementation is claimed.
