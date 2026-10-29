# Static effects and capability requirements — Phase 2

`nexo check --effects [--json] [--require-complete] [--dependencies file.json]
[--authorization file.json] [paths...]` checks a program without executing it.
Both backends reuse their front ends, linked modules, LSP coordinates and Shield
policy engines. The language grammar and ordinary compilation are unchanged.
D182 introduced the report; D183 completes configured ceiling analysis and the
version 2 metadata contract.

```nexo
policy:
    allow read "data/"

path = "data/log"
reader = read

fn load() -> str!:
    return reader(path)?

fn report() -> str!:
    return load()?
```

Both functions require `read "data/log"`. Facts retain the original call's file
and range. An immutable, unambiguous alias can resolve to a builtin, function,
imported function or literal target. Conditional definitions, mutable bindings,
shadowed builtins and unresolved callbacks remain conservative unknowns.

## Operator configuration and caller boundaries

Use the **same manifests as Shield**, supplied explicitly or through the existing
`NEXO_SECURITY_DEPENDENCIES` / `NEXO_SECURITY_AUTHORIZATION` environment variables:

```sh
nexo check --effects --json --require-complete \
  --dependencies permissions.json --authorization grants.json app.nexo
```

Explicit options override their corresponding environment setting. The existing
strict loaders validate size, depth, duplicate fields, canonical file identities,
versions, rules and allowlists. Invalid configuration produces the existing
`security_context_config` or `security_authorization` diagnostic. No permissive
fallback is used. Loading and querying configurations never initializes or
mutates active runtime authorization, context stacks or approval state.

Known capability requirements are compared with:

- The declared root policy; absent grants deny, and explicit deny wins.
- The defining dependency's ceiling and every statically reachable caller
  ceiling along a call path. A root function cannot remove an imported caller's
  restriction. All possible paths are joined conservatively: a denied path is
  reported even if another path allows the same operation.
- Configured tool ceilings and the permissions of every configured agent whose
  allowlist includes that tool. A tool with no permitted agent has a denied
  `dispatch` check. These are possible **configured dispatch scenarios**, not a
  claim that a model will select a tool or that a function has been registered.
- Dependency ceilings captured at top-level tool registration. Import-graph
  propagation retains importer restrictions when a tool is defined in a nested
  import. Known imported agent identities contribute their dependency contexts
  too. Nested, unresolved or opaque registration contexts remain explicit
  `dynamic_registration_context` unknowns.

The report never supplies approval. `approval_required` records the operator's
confirmation requirement, including for a pure configured tool. Native/process
requirements under configured ceilings use Shield's strict comparison and do
not imply confinement of arbitrary native code.

`allowed` means the **statically identified configured comparisons** matched at
analysis time. It is not a runtime authorization token. Runtime still intersects
actual caller/task/module/agent/tool contexts, verifies seals and allowlists,
requires approval and checks every protected operation. Dynamic task/handler
registration and opaque dispatch remain unknown when their contexts cannot be
identified. Static results never widen policy or bypass Shield.

Filesystem matching reuses Shield's matcher and can inspect symlink/path
metadata. It is a snapshot, not a proof about subsequent filesystem mutation,
network endpoints or OS isolation. Configuration paths and source identities
are canonically resolved; no target file contents, provider or tool is executed
by this analysis.

## Report and exit semantics

The pass reports **all functions and module initializers**, including unreachable
code. Branches, defaults, tests and supported deferred bodies are over-approximated.
It does not assume a closed set of arguments for externally callable functions.

- Exit **0**: front-end checks passed and no known configured denial was found.
  Advisory mode can still contain unknowns.
- Exit **1**: source/configuration/read failure, a known denial, or an incomplete
  summary when `--require-complete` is requested.
- Exit **2**: malformed or duplicate options. Configuration and completeness
  options require `--effects`. `--intent` is incompatible with deterministic
  analysis and never invokes a model through this command.

Unknowns remain severity-2 warnings even when `--require-complete` makes the
command fail. In either mode, successful checking is not proof of correctness,
runtime approval, all runtime permissions or OS-enforced isolation.

## Versioned JSON metadata

The [diagnostics v1 envelope](diagnostics.md) and schema remain unchanged.
`effects_summary` diagnostics now carry **`effects_version: 2`** in `data`;
consumers of effect metadata must reject unsupported versions. The normative
metadata shape is [effects-v2.schema.json](effects-v2.schema.json), which reuses
the existing diagnostic range schema. Version 1 meant root-only comparison;
version 2 adds configured comparisons, registration contexts and approval
requirements. This changes effect-metadata interpretation, not envelope version.

| Summary field | Meaning |
| --- | --- |
| `function` | `<module>`, function or dotted method/lexical function name. Combine diagnostic `file` and `range` for identity. |
| `effects_version` | `2`. |
| `complete` | No unknown facts within the supported conservative model. This is not a general proof about arbitrary runtime/native values. |
| `pure` | `complete` and the modeled effect set is empty; never true for an unknown summary. |
| `policy_scope` | `root` without operator manifests; `configured` when either manifest is supplied. |
| `approval_required` | Whether this configured tool identity requires human confirmation; no approval is granted. |
| `effects` | Sorted, deduplicated transitive requirements retaining origin file/range. |
| `fixes` | Empty; permission grants are never applied or proposed automatically. |

Each fact contains `operation`, nullable `capability`, nullable `target`,
`status`, nullable `reason`, origin `file`/`range` and a `checks` array. Status
is `allowed`, `denied`, `unknown` or `not_applicable` (no policy capability).
Unknown targets are null, not wildcards. Origin positions use zero-based UTF-16
code units and end-exclusive ranges, exactly as the LSP.

Each known comparison has `scope` (`root`, `dependency`, `agent`, `tool` or
`dispatch`), `identity` (null for root, otherwise file or `file::symbol`),
`status` (`allowed`/`denied`) and `approval_required`. Comparisons are sorted;
any denial makes that requirement denied. No list of matching comparisons is
emitted for an unknown requirement. A known denial elsewhere remains an error.

Summary severity is 1 for a known denial, otherwise 2 if incomplete, otherwise
3. Compile errors retain the existing catalog codes, messages and fixes instead
of being replaced by summaries. Future reason/operation codes and additive
metadata can appear; consumers must tolerate them. Breaking interpretation
changes require a new effect metadata version.

## Inference and deliberate unknowns

The linked AST forms lexical function/module units and direct-call edges.
Record methods have independent summaries; calls on statically identified
record constructions/immutable receivers and `self` resolve when unambiguous.
Field/method collisions, dynamic receivers, trait dispatch and opaque values
remain unknown. Constructors/default behavior is conservative rather than
assumed pure. Shared parent scopes avoid quadratic copies of visible globals.

A reverse-edge worklist propagates effects and relevant caller identities to a
fixed point, including recursive components. A separate import-graph closure
tracks captured registration ceilings. Missing callable summaries cannot become
empty/pure summaries. Alias expansion is bounded at 32 links; unresolved/cyclic
aliases remain unknown rather than assuming a target. Function parameters,
callbacks, mutable/conditional aliases and paths that cannot be uniquely resolved
stay unknown. There is no inference-based grant or implicit permission escalation.

The shared `nexo/stdlib/effects.json` describes effects; `api.json` and
`nexo docs --for-ai` remain the exact public API inventory. Currently modeled:

- `read`, `read_lines`, `write`, `save`: read/write requirements, positional and
  named path arguments and immutable literal aliases.
- `secret`: conservative env requirement by key name, without reading a value.
- HTTP methods: net requirements for a narrow ASCII HTTP(S) URL subset. Only
  the lowercase host is kept; no DNS request or credential-bearing URL is emitted.
- `connect`: read/write for a literal local database; `:memory:` has no file
  requirement. Server authentication/discovery and dynamic connections stay unknown.
- `ask`: explicit AI/net effect with unresolved provider configuration. No
  prompt, response or provider credential is attached.
- `run`, C and Python integrations: capability requirements plus explicit
  unknown arbitrary native effects. Shell command text is omitted.
- Console I/O and a small explicit set of pure core/JSON/CSV operations.

Builtin calls receiving unknown values and interpolated text stay conservative:
`len`, conversion or formatting can force lazy/native values and trigger hidden
effects. Unresolved methods, callbacks, indexing, lazy iteration, pipelines,
events, annotations, provider/connector behavior and opaque/deferred calls are
reported as unknown. This is the specified unknown/dynamic-effect behavior of
Phase 2, not a claim of a complete symbolic evaluator or unrestricted effect
polymorphism. More precision can be added without weakening the runtime boundary.

Inference is limited to 10,000 retained units and 200,000 fact/context
memberships per entry graph. Registration closure has the same membership
budget. Exceeding a limit discards partial summaries and reports `analysis_limit`
for every retained unit, with `complete: false` and `pure: false`. Parsing,
linking and direct AST collection keep their existing resource behavior; these
are analysis budgets, not process memory/CPU confinement.

## Validation

The Phase 2 tests cover parity, recursion/imports, caller and captured-registration
ceilings, operator allowlists, approval metadata, malformed/oversized manifests,
canonical identities, aliases and methods, symlink denials, explicit unknowns,
strict completeness, unchanged runtime denial and no execution/state mutation.
The existing formatter, unit, LSP, JSON and Shield tests remain the compatibility
baseline. [The completion record](ai-native-phase2-completion.md) records results
and Linux evidence.

`compiler/bench/effects.py` generates reproducible chain, recursive and imported
1k/10k/100k-line corpora. It records corpus hashes, debug compiler/toolchain,
wall-time median/p95, output bytes and fresh-child peak RSS. Generated programs
are checked but never executed. These are baseline measurements; the 100k
corpora deliberately hit the explicit unit limit and are not called complete.
