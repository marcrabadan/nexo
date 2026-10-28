# Shield targeted adversarial review — 2026-10-27

This is a local engineering review with reproducible attacks and regression tests.
It is not an independent audit, penetration-test certification, proof of memory
safety, or authorization to deploy hostile multi-tenant workloads.

## Scope and threat model

The reviewed boundaries are untrusted JSON request bodies and signed JWT claims,
operator manifest parsing, broker IPC framing/control JSON, selected-route ingress
ceilings, and existing process/resource supervision tests. The attacker may send
local loopback requests or supply application input; malformed operator files are
also tested as robustness inputs, although those files are trusted authority.
Fixtures use synthetic credentials and local peers. No external services were
attacked and no real credentials were read.

The review inventoried runtime filesystem/process/environment sinks and examined
selected dispatch, policy, broker and lifecycle paths. This is not exhaustive
coverage of every native library or standard-library side effect. No privilege
escape was demonstrated in the examined scope; that does not establish absence
of escapes elsewhere.

## Findings and fixes

| Finding | Evidence before fix | Resolution |
| --- | --- | --- |
| Unbounded recursive JSON parsing permits denial of service | A 12,000-level body sent to a loopback JSON handler closed the connection without an HTTP response. Both native JSON parser implementations lacked a general nesting bound. | Iterative, string/escape-aware preflight limits containers to 128 levels before recursive parsing/conversion. Excessive request bodies produce HTTP 400; a subsequent health request succeeds. Applies to both language runtimes, JWT claims and operator manifests. |
| Malformed UTF-16 surrogate pair can panic the configuration parser | The previous parser, isolated from the checked-in source and compiled with overflow checks, panics on `"\ud800\u0000"` due to unsigned subtraction. Release overflow behavior must not be relied upon as validation. | Validate the low-surrogate range before arithmetic. All 65,536 possible low words are exercised. Lone/invalid surrogate pairs are rejected by the control JSON parser. |
| Control JSON grammar was permissive and ambiguous | The previous parser accepted `01`, `1.`, `1e9999` and unescaped newlines inside strings. No permission bypass was demonstrated from these forms. | Enforce JSON number grammar, finite control numbers and escaped control characters. Broker control frames now reject duplicate fields rather than silently taking the last value. |

The first finding affects availability wherever an application exposes JSON
parsing. The present listener is local and already time-bounded, which limits
exposure; it remains a blocker for stronger persistent/public-service claims.
The second finding is primarily operator-input robustness in existing manifests,
not demonstrated remote execution or an authority escalation.

The 128-level limit is now an intentional compatibility boundary of JSON parsing.
Brackets inside quoted/escaped strings do not count. The preflight does not replace
syntax validation; malformed ordinary JSON retains the parser's usual diagnostics.
Existing stricter manifest/broker depth limits, API body/frame byte limits and process deadlines still apply. This does
not bound general application allocations, arbitrary computation or every JSON
serialization path.

## Regression and mutation coverage

- HTTP: over-depth bodies through both CLI entrypoints, followed by a successful
  request on the same server process.
- In-process dispatch: 128 accepted, 129/12,000 rejected in Python and native runtimes.
- JWT: correctly signed, over-depth claims rejected as unauthorized in both runtimes.
- Dependency manifests: hostile depth rejected with a normal error before payload.
- Rust: 20,000 seeded malformed JSON inputs through both parser entrypoints;
  exhaustive 65,536 low-surrogate words; 10,000 seeded oversized/truncated IPC frames;
  duplicate/invalid control JSON and consecutive frame boundaries.
- Python: 1,000 seeded structural inputs with independent known nesting depth.
- Existing tests recheck route permission intersection and restoration, denied
  effects, fatal failures, cumulative budgets, slow clients, hard deadlines,
  launcher death and socket cleanup.

These are deterministic mutation campaigns committed as tests, not a continuous
coverage-guided fuzzer or sanitizer campaign. They do not demonstrate resistance
to all resource exhaustion or native memory-corruption attacks.

## Kernel observations and remaining deployment blockers

The current probe reports Landlock ABI **0**, `landlock_enforced=false`, and
`supported=false`. It reports seccomp, openat2, pidfd and supervisor support.
Online additionally reports `proxies_unconfigured=false`; the managed environment
requires its configured proxy. Neither proxy settings nor network routes were
changed. Linux isolated execution must continue to fail closed here. Positive
Landlock enforcement and positive production broker egress cannot be established
on this host; they require a compatible, reviewed environment.

Before accepting hostile multi-tenant workloads:

1. Have an independent reviewer assess the full permission boundary and native
   unsafe/FFI surfaces, with explicit escape and cross-request authority tests.
2. Run the isolated profile on a compatible Landlock host, requiring successful
   real enforcement rather than treating skipped positive tests as a pass.
3. Add sustained coverage-guided fuzzing and appropriate native sanitizers for
   parsers, IPC, value ownership and concurrent lifecycle code.
4. Validate process-level memory/CPU/descriptor limits and filesystem race
   protection appropriate to the intended persistent deployment. The bounded
   loopback profile now installs fixed kernel limits; these are not aggregate
   job/cgroup limits or filesystem race protection. Current API
   permission checks do not pin inodes or eliminate TOCTOU.
5. Review information flow: read/env plus an allowed network destination can
   disclose data. Capability enforcement is not confidentiality tracking.

TLS, persistent server lifecycle and individual request cancellation remain
separate missing features. Adding them is not a substitute for these checks.

Recorded local results and reproducible checks are in the [validation snapshot](shield-validation.md). Local scratch logs are not part of the proposed change.
