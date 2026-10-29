# Phase 2 first slice: implementation and validation

Date: 2026-10-29. Phase 1 was committed and pushed to `origin/main` as
`d71ffca` on explicit user request. The user then authorized Phase 2.
This record covers the initial effect-reporting slice, implemented locally
for review. It does not claim completion of the entire effect system.

## Result and decisions

Both implementations support `nexo check --effects [--json] [paths...]`.
Function and imported-module requirements propagate through direct calls and
recursive graphs. Known literal requirements are compared with the root policy;
missing grants and explicit denies are errors. Dynamic targets, callback/alias
calls, conditional/builtin shadowing and unmodeled behavior remain explicit
unknowns. Analysis limits also produce unknowns rather than empty summaries.

D182 records the report-only design. JSON extends Phase 1's existing envelope,
LSP ranges, catalog diagnostics and extensible metadata, with `effects_version: 1`.
Shared immutable scopes avoid copying all visible globals into every function.
The native pass validates builtin identity against existing name resolution;
the Python pass uses conservative lexical binding information. Runtime Shield
checks, authorization, approval and unproven contracts remain intact. The
existing policy matcher/engine is reused; no second authorization engine,
new dependency or grammar change is introduced.

The root-policy comparison is deliberately narrower than runtime authorization.
Tests show that a matching static root grant still cannot override an imported
module's runtime ceiling or the active caller's denial. Nonliteral values are
conservative because operations such as `len` can force lazy values and trigger
hidden effects. See [the precise contract and limits](effects.md).

## Changed files

| Files | Purpose |
| --- | --- |
| `nexo/effects.py`, `compiler/nexoc/src/effects.rs` | Lexical units, direct-call graph, origin facts, fixed-point propagation, explicit unknowns, bounded reporting. |
| `nexo/stdlib/effects.json` | Shared versioned builtin-effect models and public builtin-name shadow coverage. |
| `nexo/cli.py`, `nexo/diagnostics.py`, native `main.rs` / `diagnostics.rs` | Opt-in human/JSON command dispatch and existing envelope/error handling. |
| `nexo/lint.py`, native `lsp.rs` / `modules.rs` | Reuse linked module identities and cached LSP coordinate conversion. |
| `nexo/modules.py` | Invalid UTF-8 module input becomes an existing structured module diagnostic, matching Rust, instead of an uncaught decode failure. |
| `nexo/security_context.py` | Extract the existing pure permission predicate for shared runtime/static matching; retain caller/module intersection semantics. |
| `nexo/errors.py`, `compiler/diag/src/catalog.rs` | Matched effect-summary diagnostic and repair text. |
| `tests/test_effects.py` | 23 focused cases, including backend parity and runtime denial invariants. |
| `compiler/bench/effects.py`, `compiler/bench/effects-baseline.json` | Reproducible offline chain, recursive and imported-graph measurements. |
| `docs/effects.md`, specification, decisions, diagnostics guide and roadmap | Contract, design, current coverage and pending increments. |
| Phase 1/2 validation records | Record Phase 1 publication and Phase 2 evidence/limitations. |
| `docs/playground/nexo.zip` | Regenerate the existing deterministic interpreter bundle. |

Pre-existing `README.md`, `editors/vscode/README.md` and untracked `Makefile`
changes remain separate. No merge or deployment was performed.

## Tests

- Focused final run: **390 passed** (`test_effects.py`, Phase 1 JSON CLI tests,
  LSP, Python unit tests, `docs --for-ai` and generated playground bundle).
- Interpreter Shield regression run: **31 passed**, 38 compiler cases
  deselected (`tests/test_security.py -k interpreter`). This run used local
  test servers outside the workspace sandbox.
- Final `cargo test --locked`: **63 passed**, 1 existing benchmark ignored;
  local socket tests ran outside the sandbox. No Rust test failed.
- `git diff --check` and `rustfmt --check` on both new/edited diagnostics/effects
  modules passed. Repository-wide formatting remains a pre-existing limitation
  documented in the Phase 1 record; unrelated files were not reformatted.
- Full native execution conformance/differential and the full mixed-backend
  Python suite were not repeated: the earlier Phase 1 runs establish that this
  macOS host cannot link native programs with the existing Linux linker flags
  (`gcc_s`, `--gc-sections`). Those flags are unchanged. Full execution/isolation
  validation still requires supported Linux CI. This slice's compiler checks
  and benchmarks compile the compiler binary but do not link or execute generated
  Nexo programs.

Coverage includes recursive and nested calls, imported initializer/function
origins, literal grants and deny precedence, absent grants, dynamic paths,
callbacks, aliases, builtin shadowing, conditional definitions, environment/
database/network/process/AI classification, invalid module encoding, sensitive
URL/command handling, oversized URL ports, lazy-value uncertainty, analysis
limits, human/JSON options and no target execution. Runtime regressions verify
that root reports do not defeat dependency or caller ceilings.

## Benchmarks

The checked-in JSON contains all **54 measurements**: 3 repeats × 3 corpus
sizes × 3 graph shapes × 2 backends, with corpus hashes, host/toolchain,
compiler hash, output bytes, median/p95 wall time and fresh-child peak RSS.
Measurements include parsing, linking/checking and JSON serialization. Commands:

```sh
python compiler/bench/effects.py --shape chain --repeat 3
python compiler/bench/effects.py --shape recursive --repeat 3
python compiler/bench/effects.py --shape imported --repeat 3
```

Representative chain medians on this local debug build:

| Lines | Python seconds | Native checker seconds | Analysis |
| --- | ---: | ---: | --- |
| 1,000 | 0.157 | 0.080 | Complete for this modeled corpus |
| 10,000 | 1.005 | 0.745 | Complete for this modeled corpus |
| 100,000 | 4.857 | 2.043 | Explicit 10,000-unit cutoff; incomplete |

At 100k lines the chain checker peak RSS was approximately 335 MiB for Python
and 308 MiB for native. The recursive and imported corpora were also measured
at all three sizes. Automated assertions verified complete/no-limit summaries
at 1k/10k and that **every retained summary** reports `analysis_limit` and is
incomplete at 100k. These are baseline observations, not performance targets
or proof of complete analysis of 100k lines. No generated program or provider
was executed, and no network or telemetry was used by the benchmark.

## Remaining Phase 2 work

- Statically compare trusted dependency, caller/task and tool ceilings; current
  `policy_scope: root` reports explicitly exclude them.
- Refine parameter targets, aliases, callbacks and value types without treating
  unresolved calls as pure or granting capabilities.
- Model methods, implicit/deferred calls, connectors and AI provider configuration
  more precisely. Type/trait/method behavior remains conservative/unmodeled.
- Extend parity/conformance tests as models expand, with dedicated supported
  Linux execution/isolation validation. Existing interpreter/compiler static
  coverage differences remain visible as ordinary front-end diagnostics.

Unknown warnings do not fail report mode by themselves. A successful exit
status must never be consumed as proof of complete effects, runtime permission
or OS isolation. Phases 3–5 remain outside this implementation.
