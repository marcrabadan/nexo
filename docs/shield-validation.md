# Shield runtime validation snapshot

This change is proposed for review, not as a production-security certification.
Results below were obtained on the original development base
`7d1b640c90f7942f9051b68ca9de56c497b9de00`; integration with subsequent main-branch
changes requires a new complete validation run and review of added API boundaries.

## Recorded checks

- Locked Rust workspace build passed.
- Rust workspace: 66 passed, one optional benchmark ignored.
- Compiler conformance after parser hardening: 142/142 passed.
- Adversarial/security matrix: 885 passed, 33 failed, 16 skipped initially.
  All 33 failures were interpreter dispatch tests caused by the new JSON depth
  preflight receiving byte manifests. After correcting UTF-8 byte handling and
  adding a BOM regression, the complete dispatch suite and JSON limit units
  passed: 105 tests. Combined coverage is 919 distinct passing cases and
  16 skipped cases, not a single clean full-suite invocation.
- Subsequent loopback-limit matrix: 296 passed and 16 skipped; two new tests
  initially failed because this host does not expose `/proc/PID/task/PID/children`.
  Both passed after using process stat ancestry and `prlimit` to inspect actual
  live payload limits. Combined coverage: 298 passing cases and 16 skipped cases.
- `git diff --check` passed.

The local matrices selected Shield ingress, routes, permissions, hostile JSON,
isolation, broker, budgets, streams, events, spawn, dispatch, HTTP/AI, ordinary
web and supporting unit suites. They are not a claim that the entire repository's
Python suite ran cleanly on the final commit. CI must run that suite, conformance,
differential checks and the Docker/kernel jobs before merge.

## Reproducible checks

```sh
cargo build --locked --workspace --manifest-path compiler/Cargo.toml
cargo test --locked --workspace --manifest-path compiler/Cargo.toml
NEXO_TEST_COMPILER="$PWD/compiler/target/debug/nexo" python -m pytest -q
python compiler/scripts/conformance.py
python compiler/scripts/differential.py
nexo security probe
nexo security probe --sandbox=linux-online
```

Run the CLI probes with the built native compiler available. Local-network tests
require loopback socket access. Docker validation uses the existing security
workflow and `docker/shield/validate.sh --require-linux`; unsupported kernel
isolation must not be counted as a positive isolation result.

## Adversarial coverage

The added deterministic tests exercise 20,000 malformed JSON inputs through both
native parsers, every possible low-surrogate word (65,536), 10,000 malformed IPC
frames and 1,000 structural Python inputs. Integration tests cover over-depth
request bodies, survival of a subsequent request, signed over-depth JWT claims,
manifest rejection before payload, static route identity, authority restoration,
real kernel limits, slow clients and parent/worker termination.

These are seeded mutation tests, not a continuous coverage-guided or sanitizer
campaign. See [the targeted review](shield-security-review.md) for findings and
remaining risks.

## Environment limits and merge gates

The local probe reports `landlock_abi=0`, `landlock_enforced=false` and
`supported=false`; seccomp, openat2, pidfd and supervisor support are present.
The online probe additionally reports `proxies_unconfigured=false`. The managed
proxy was preserved. Full positive Landlock isolation and production broker
public egress remain unvalidated on this host.

Before merge: integrate current main without weakening guards, review all new
connectors for checked side effects, obtain green full CI and actual kernel
isolation results on a supported host, and review the changed native/FFI boundary.
Before claiming readiness for hostile multi-tenant production: obtain independent
security review and address general allocation limits, filesystem TOCTOU and
information-flow risks. TLS, persistent listeners and request cancellation remain
separate unimplemented features.
