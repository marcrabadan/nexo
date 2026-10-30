# Phase 3 first slice: validation record

Historical D184 increment record. Later acceptance and current limits are in
[the Phase 3 completion record](ai-native-phase3-completion.md).

Phase 3 was in progress at this increment. D184 implements typed final agent responses, stricter
Nexo tool-call metadata/parameters, approval before conversion in every profile,
and cooperative per-invocation deadlines. It does not complete cancellation,
concurrent budget reservations, durable state or tracing acceptance.
See [the precise contract](agent-lifecycle.md).

## Checks and environment

The shared `tests/test_agent_lifecycle.py` suite uses local mocked Ollama,
OpenAI-shaped, Anthropic-shaped and Gemini-shaped endpoints. No external
provider is called. It verifies typed records/scalars, invalid final output
without replay, invalid options before requests, malformed arguments/metadata,
duplicate IDs, extra parameters, delayed dispatch denial and delayed/refused
approval before effectful record defaults. Existing provider/Shield dispatch
regressions are also retained.

Local Rust compilation checks pass. Native program linking on this macOS host
hits the repository's existing unconditional Linux linker flags (`-lgcc_s`),
so native execution is validated separately in the Linux AMD64 image under
Rosetta. This is runtime/API evidence, not positive kernel-isolation validation.
The initial host run's 15 native failures were linker failures; its apparent
invalid-option passes were not counted as native evidence. The tests now require
an option diagnostic as well as a failing exit code to exclude that false pass.

## Results

- Final lifecycle suite in Linux: **76 passed** across the interpreter and native
  compiler (38 scenarios per backend), including finite-number rejection,
  replay prevention after a tool effect, admission closure between tool calls,
  early network failure and preservation of fatal policy denials.
- Final local interpreter lifecycle plus generated playground check: **39
  passed**, with 38 native cases deselected because host linking is unavailable.
- Static effects, completeness, JSON diagnostics and catalog regressions:
  **77 passed** locally.
- Rust: **63 passed, 1 ignored** after the native fixes.
- Corrected-runtime provider/MCP/approval/Shield dispatch regressions:
  **128 passed, 2 deselected**. These repeat the directly affected compatibility
  cases after the native fixes, with the same documented profile exclusion.
- The broader initial Linux compatibility run passed **275 tests**, with two
  cases of the existing loopback-profile approval test deliberately deselected.
  That profile requires kernel facilities unavailable under Rosetta. The host
  interpreter compatibility run had **95 passed** and the corresponding
  Linux-only profile case failed with exit 125; it is not counted as a product
  regression or positive isolation evidence.

The first lifecycle run on Linux exposed five native failures: the runtime's
first-error rule preserved a transport `NetworkError` after deadline expiry,
and the general JSON parser accepted its existing nonfinite numeric extensions.
The fixes replace only an expired recoverable transport error (never a fatal
Shield diagnostic), and validate finite JSON privately at agent boundaries.
The general JSON API is unchanged. All five failing scenarios are retained in
the passing final suite. A controlled immediate provider disconnect verifies
that an early network error is not mislabeled as a timeout.

## Artifact provenance and reproduction

The production runtime was built with the standard Shield Dockerfile and locked
release workspace build. For the corrective increment, that exact cached build
stage was recovered without changing sources or dependencies, then only the
updated runtime source was rebuilt with the same Rust toolchain and
`cargo build --offline --locked --release --workspace -j 2` flags. No validation
uses a compiler or runtime archive mounted from the host.

Corrected runtime image:
`sha256:e073ca30857d2128936870523f88f76ad3f7eeb1cfe9383939c8adc0f983ab3f`.
Final validation image:
`sha256:3a16ee419a75eb9cd3f25fb02ec77e129033c0af3c541a0cc3623fde04285847`.
The latter reuses the disposable Python validation environment, replaces all
production compiler/worker/runtime artifacts and copies the current filtered
checkout/tests. Its inherited Linux Rust test executables were not used as
final-source evidence; the Rust results above are the local final-source run.
Containers run as UID 10001 with no external network or host mounts, three CPUs,
three GiB and a 256-process ceiling. Mock-provider loopback remains available.

A fresh equivalent validation image can be built directly from the checkout:

```sh
docker build --platform linux/amd64 -f docker/shield/Dockerfile \
  --target validation -t nexo-shield:phase3-validation .
docker run --rm --platform linux/amd64 --network none --cpus 3 \
  --memory 3g --pids-limit 256 --entrypoint /opt/nexo-venv/bin/python \
  nexo-shield:phase3-validation -m pytest -q -o cache_dir=/tmp/pytest-cache \
  tests/test_agent_lifecycle.py
```

Session logs are `/tmp/nexo-phase3-*.log`; they are local artifacts, not committed.


## Limits retained

No test or permission is weakened to accommodate emulation. Shield persistent
workflows remain rejected. Deadlines cannot preempt an admitted operation or a
blocking approval read. No live-provider benchmark, durable replay protection,
tracing exporter or full Phase 3 completion is claimed.
