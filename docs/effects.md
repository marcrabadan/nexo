# Static effects and capability requirements (Phase 2, first slice)

`nexo check --effects [--json] [paths...]` reports conservative requirements
without executing the program. Both backends use their existing front ends,
module linkers and diagnostics. Rust additionally uses its resolved builtin
identities; Python uses conservative lexical binding information. Existing
`check`, compilation and Shield execution remain unchanged. No grammar or
dependency changes are required. D182 records the design.

```nexo
policy:
    allow read "data/"

fn load() -> str!:
    return read("data/log")?

fn report() -> str!:
    return load()?
```

Both `load` and `report` require `read "data/log"`; their facts identify the
original call in `load`. The policy comparison reports that this root grant
matches. A deny rule overrides an allow, regardless of their order. A known
requirement with no matching grant is reported as denied, including when no
policy is declared. Console I/O has no D142 capability and is reported explicitly
as `console.read` or `console.write`, rather than classified as pure.

## Interpretation and exit status

This is an **opt-in report of all functions**, including unreachable functions,
and module initialization. It over-approximates branches, defaults, tests and
some deferred bodies. It does not specialize a callee's parameter from each
caller's argument. Known literal targets can be compared; a path parameter,
interpolation or unresolved provider configuration remains unknown.

- Exit 0: front-end checks succeeded and no known root-policy denial was found.
  Unknowns are warnings and can still be present. Check `complete`, not only the
  exit status, when deciding whether further analysis is needed.
- Exit 1: a front-end/read failure or a known root-policy denial.
- Exit 2: invalid or duplicate flags. `--intent` cannot be combined with this
  deterministic analysis. The program, model, tool and target operation never run.

`allowed` means **matched by the declared root policy at analysis time**. It
is not a runtime authorization decision. `policy_scope` is always `root`.
Dependency, task, caller, agent and tool ceilings are not statically compared
in this slice. Runtime checks still intersect every active ceiling, require
operator-controlled approval and fail closed. Reports cannot create grants.
Tests demonstrate that dependency denials still stop execution even after a
successful static root-policy report. Symlink/filesystem matching reuses the
existing matcher and may inspect path metadata; it is a snapshot, not a proof
about filesystem mutation after the check or OS-enforced isolation.

## Versioned metadata

JSON mode uses the existing [v1 diagnostics envelope](diagnostics.md) and schema;
new information is in the existing extensible `data` object. Each module/function
has a catalog diagnostic `effects_summary` with severity 1 if any known root
requirement is denied, otherwise 2 if incomplete, otherwise 3. Its source path
and UTF-16 range identify the definition. Compile errors keep their existing
codes, messages, ranges and fixes instead of being replaced by summaries.

| `data` field | Meaning |
| --- | --- |
| `effects_version` | `1`; consumers must reject unknown effect metadata versions. |
| `function` | `<module>`, a function name or a dotted lexical function name. Combine with diagnostic `file` and `range` for identity. |
| `complete` | No unknown facts remain within the supported conservative analysis/model. It is not proof of all runtime authorization or behavior. |
| `pure` | `complete` and the modeled effect set is empty. Never true for incomplete analysis. This is modeled purity, not a guarantee about arbitrary native values or code. |
| `policy_scope` | `root`; comparison excludes runtime ceilings and approvals. |
| `effects` | Stable, sorted and deduplicated transitive facts, retaining origin file/range. |
| `fixes` | Empty; the analyzer never proposes or applies an automatic permission grant. |

Each fact contains `operation`, nullable `capability`, nullable `target`,
`status`, nullable `reason`, `file` and the existing LSP `range` shape. `status`
is `allowed`, `denied`, `unknown` or `not_applicable` (no policy capability).
Unknown targets are null, not an implicit wildcard. Origin ranges identify
where a requirement originated, not the caller receiving it. New reason codes
and operations can be added within metadata v1; changing field meanings or
coordinate conventions requires a new version.

## Analysis implementation

Both implementations build lexical function/module units and direct-call edges
from the linked AST. Immutable parent scopes are shared rather than copied per
function. Unambiguous local function calls and qualified imported calls contribute
edges; recursive components converge using a reverse-edge worklist and union of
origin facts. No recursive call-graph traversal is required. Facts are sorted
by source identity/position and operation, making output deterministic.

Shadowed builtin names, reassignment, aliases, callbacks and conditional function
definitions are conservative unknown calls. Function declarations sharing a
builtin name are treated conservatively because Nexo's runtime lookup can fall
back to the builtin before a local binding exists. A missing function summary
never becomes an empty/pure one. Module initializers propagate through imports;
function facts keep the defining module's source identity.

The shared `nexo/stdlib/effects.json` catalog is a compiler model, not a second
API inventory. `nexo docs --for-ai` and `api.json` remain the public API source.
Catalog coverage is checked against public builtin names so an API addition
cannot silently bypass shadow analysis. Unmodeled operations remain unknown.

Currently modeled:

- `read`, `read_lines`, `write`, `save`: existing read/write capabilities,
  including named path arguments and `save`'s second positional argument.
- `secret`: conservative environment requirement by key name, even if a created
  secret is never consumed. No environment value is read or included.
- `http` methods: net requirements for a deliberately narrow ASCII HTTP(S) URL
  subset. Only the lowercase host is retained; no DNS request is made.
- `connect`: local database read/write requirements; `:memory:` has no file
  requirement. Server URLs and dynamic connections remain unknown because
  authentication, discovery and further operations are not fully modeled.
- `ask`: explicit AI/net effect with unknown target/configuration. Prompts and
  provider credentials are never attached.
- `run`, C and Python imports: explicit native/process capability requirements
  plus unknown arbitrary native effects. Shell command text is not attached.
- Console I/O and a small explicit set of pure core/JSON/CSV operations.

Builtin calls with non-literal arguments and interpolated text also remain
unknown: core conversions and `len` can force lazy/native values and execute
effects beyond the visible call. This deliberately trades precision for explicit
uncertainty until value/target refinement is implemented.

Method dispatch, record/trait behavior, annotations, indexing, lazy iteration,
pipelines, events, dynamic calls, unmodeled standard-library APIs and higher-order
flows are not fully modeled. Their supported encounters are reported as unknown;
this slice does not claim a sound general-purpose effect type system for all
Nexo constructs or arbitrary native values. Type/method declarations are flagged
as unmodeled; their methods do not yet have separate callable summaries.

Analysis is capped at 10,000 module/function units and 200,000 propagated fact
memberships per input graph. Exceeding a limit discards partial summaries and
reports `analysis_limit` for each retained unit, always with `complete: false`
and `pure: false`. No unlimited inference or authorization claim is made.
Parsing/linking/type checking and direct AST collection still have their existing
resource behavior; these caps are not process memory or CPU limits.

## Validation and next increments

`tests/test_effects.py` covers backend parity, recursion, imports, nested calls,
known grants/denials, deny precedence, dynamic targets/callbacks, shadowing,
conditional definitions, defaults, URL redaction/limits, invalid module encoding,
analysis limits, no execution, runtime dependency denial and caller intersections.
Existing Phase 1, LSP, unit and Shield regressions validate shared infrastructure.

`compiler/bench/effects.py` generates fixed 1k/10k/100k-line chain, recursive and
imported corpora, recording corpus hashes, median/p95 times, output bytes and
fresh-child peak RSS. Its generated Nexo programs are checked but never executed.
Measurements include front-end cost and JSON serialization. The checked-in
baseline records the host/toolchain and debug compiler hash; it is not a cross-host
performance guarantee. See [the Phase 2 validation record](ai-native-phase2-validation.md).

Next Phase 2 increments: static dependency/tool ceiling comparison with trusted
configuration identity, parameter/alias/callback target refinement, method and
implicit/deferred-call coverage, and explicit diagnostic reasons for provider/
connector-specific requirements. Preserve runtime denial, approval and existing
OS-isolation boundaries throughout. Phases 3–5 have not started.
