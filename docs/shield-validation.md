# Shield runtime validation snapshot

This change is proposed for review, not as a production-security certification.

## Integration with main — 2026-10-28

Integrated `1a78277a78e661dd3f4e81fed1f3be061c0aee5a` into the Shield branch.
All merge conflicts are resolved. See [connector boundaries](shield-connectors.md)
for the reviewed transport/credential paths, deliberate fail-closed restrictions
and ordinary-execution compatibility.

- Locked workspace build passed; Rust workspace: 66 passed, one optional benchmark ignored.
- Integration matrix: 1,000 passed, four failed, 57 skipped. Three failures were
  linker errors from the environment's full `/tmp`; the fourth assumed three
  native workers despite a two-CPU cgroup quota. After making that test respect
  the scheduler's CPU bound and using workspace temporary storage, the complete
  policy/stream suites passed: 28 tests, including all four prior failures.
  Combined coverage is 1,004 distinct passing cases and 57 skipped cases;
  it is not a single clean full-suite invocation.
- All 54 added connector/telemetry/key-budget regressions passed in that matrix.
- Final conformance rerun: 147/147 passed. The first run had linker failures from
  exhausted `/tmp`; the final run used workspace temporary storage.
- Differential checks: 218/228 identical, 10 documented differences, zero unexpected differences.
- `git diff origin/main --check` passed; no unresolved merge entries remain.

The selected integration suites cover Shield, ingress, all streams/AI providers,
document databases, MySQL/PostgreSQL, SQLite, HTTP/web, policy and supporting
units. Skips include unavailable database server binaries and positive kernel
isolation cases. The full repository CI and positive Landlock validation remain
merge gates. No independent security audit is claimed.

## Earlier development-base checks

The historical checks below used development base
`7d1b640c90f7942f9051b68ca9de56c497b9de00`; the integration results above supersede
its pending-main-integration status.

### Recorded checks

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

Before merge: obtain green full CI and actual kernel isolation results on a
supported host, and complete review of the changed native/FFI boundary. Current
main has been integrated and the new connector entry points reviewed; unsupported
Shield paths remain explicitly rejected.
Before claiming readiness for hostile multi-tenant production: obtain independent
security review and address general allocation limits, filesystem TOCTOU and
information-flow risks. TLS, persistent listeners and request cancellation remain
separate unimplemented features.
