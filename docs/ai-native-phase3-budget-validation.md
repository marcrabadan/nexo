# Phase 3 budget reservation validation — D185

Historical D185 increment record. Later acceptance and current limits are in
[the Phase 3 completion record](ai-native-phase3-completion.md).

Implementation date: 2026-10-30. Phase 3 remains in progress.
See [the contract](ai-budget-reservations.md) for scope and limitations.

The shared reservation suite uses local mocked OpenAI, Anthropic, Gemini and
Bedrock endpoints and real Nexo `spawn` calls. A held provider response proves
that a competing request is refused before the first receipt can be recorded.
The suite covers both `ask` and agents; per-turn admission, typed repairs,
stream completion/replay/abandonment, missing or invalid usage, duplicate receipt
fields, actual-cost overruns, failed writes, invalid history, explicit zero
usage, and continued Shield persistence rejection.

No external provider, cloud deployment or billing service is called. A reservation
is a process-local accounting hold, not an invoice guarantee. Persistent workflows
and budget persistence under Shield remain rejected. No permission, assertion or
kernel limit is weakened.

## Results

- Final Linux run: **143 passed** — 66 shared reservation cases, 76 existing
  lifecycle cases and the generated interpreter bundle check.
- Final provider, serial budget/fallback, MCP and Shield dispatch regressions:
  **128 passed, 2 deselected**. The two cases belong to the existing loopback
  profile approval test requiring kernel facilities unavailable under Rosetta;
  they are not treated as positive isolation evidence.
- Final local interpreter reservation/lifecycle/bundle run: **72 passed**.
- Static effects, completeness, JSON diagnostics and message catalogs:
  **77 passed** locally.
- `cargo test --locked`: **66 passed, 1 ignored**, including three new native
  ledger tests for competing admissions, failed receipt writes/idempotence and
  day/month overlap. Formatting of the new Rust module and `git diff --check`
  also pass.

The initial Linux reservation run passed 62 cases before duplicate-key receipt
and missing-stream-usage regressions were added; all are included in the final
corpus. No failing product assertion was suppressed to obtain these results.
The macOS compiler/runtime build succeeds, but executable linking still hits the
pre-existing Linux linker flags documented in the first lifecycle record;
shared native execution therefore uses Linux AMD64 under Rosetta.

## Artifacts and reproduction

The production artifacts were rebuilt with the pinned Rust toolchain and
`cargo build --offline --locked --release --workspace -j 2`, reusing the verified
build-stage dependency cache. Only changed source files replace cached checkout
files; compiler, worker and runtime archive all come from that same build.
The validation image copies the current filtered checkout and these artifacts
into the disposable Python test environment. No host compiler, runtime archive,
source mount, Docker socket or credentials are supplied to test containers.

Production image:
`sha256:f8f46c64d34181cb5aa686c3b471fb2ca5270471501213d939f97f5104bc6536`.
Final validation image:
`sha256:4e7f9980a080cfa5264a097b2f767aa71a0d1063ffb995a0abc8ec021f926088`.
The inherited Linux Rust test executables are not final-source evidence; Rust
results above come from the local source run. Containers use UID 10001, no
external network or host mounts, three CPUs, three GiB and a 256-process limit.
Local mock-provider loopback traffic is available inside that namespace.

A fresh validation image can be built directly with the standard Dockerfile:

```sh
docker build --platform linux/amd64 -f docker/shield/Dockerfile \
  --target validation -t nexo-shield:phase3-budget-validation .
docker run --rm --platform linux/amd64 --network none --cpus 3 \
  --memory 3g --pids-limit 256 --entrypoint /opt/nexo-venv/bin/python \
  nexo-shield:phase3-budget-validation -m pytest -q \
  -o cache_dir=/tmp/pytest-cache tests/test_ai_budget_reservations.py \
  tests/test_agent_lifecycle.py
```

Session logs are `/tmp/nexo-phase3-budget-*.log`; these are local artifacts, not
committed evidence files. No live-provider certification, actual-invoice ceiling,
cross-process safety or durable pending-reservation recovery is claimed.
