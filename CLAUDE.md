# Nexo — phases 0 to 6

The core of Nexo, a new programming language, in two implementations
(section 25 of the specification):

- phase 0: a Python 3.12 tree-walking interpreter (`nexo/`);
- phase 1: a compiler written in Rust that generates native code with
  LLVM (`compiler/`). Both must give the same output on every program,
  except for the differences recorded in docs/decisions.md (D67-D75);
- phase 2, in both: the memory model of section 7 (reference counting,
  copy-on-write, `weak`), real `parallel`, `nexo.sync` and contracts the
  compiler proves (D76-D84);
- phase 3, in both: the tools of section 14 — `nexo fmt`, `lint`, `fix`,
  `check`, `lsp`, the compiler's REPL — and the VS Code extension in
  `editors/vscode` (D85-D88);
- phase 4, in both: the standard library of section 15, `nexo.ai`,
  `nexo.web`, `nexo.db`, `use c` and `use python` (D89-D112);
- phase 5, in both: `nexo.infra`, `nexo.ci`, `nexo.stream`, flags and
  privacy (D113-D123);
- phase 6, optimisation and 1.0: faster compiled code and compilation,
  reproducible release builds with an SBOM, and in both phases `nexo
  audit`, `nexo docs --for-ai`, `nexo test --suggest` and `nexo check
  --intent`; the guarantees of the specification are measured and their
  state is in docs/guarantees.md (D126-D133).

## Source of truth

`docs/especificacion-nexo.md` is the specification and the source of truth.
If something is not in it, do not invent it. When the specification is
ambiguous or contradictory, pick the simplest option consistent with
section 1 ("one way to do each thing") and record it in `docs/decisions.md`
with the reason.

## Layout

```
nexo/
  lexer.py        tokens, INDENT/DEDENT, unit literals, string interpolation
  parser.py       recursive descent, follows the EBNF of section 24
  ast.py          typed AST (dataclasses)
  interpreter.py  tree-walking evaluator (scopes, calls, pipelines, contracts)
  checker.py      static pre-pass ("compile errors": ignored T!, misplaced ?)
  modules.py      `use shop.payments`: the modules of the program's files (D124)
  numbers.py      section 17 arithmetic: int overflow, float/decimal, money, units
  types.py        runtime values: decimal/money/units, records, enums, errors
  errors.py       error type + section 14 renderer + message catalog (English, D125)
  stdlib/         builtins, io, data (JSON/CSV), sync (nexo.sync); phase 4:
                  clock, text, crypto, http, db, web, ai, clib (use c),
                  pylib (use python); phase 5: cfg (the blocks of D114),
                  flags, privacy, stream, infra (model and plan),
                  infra_cloud (the clients and commands),
                  infra_providers.json (shared by both phases), ci,
                  ci_export; phase 6: api.json (what a program can use,
                  shared by both phases)
  audit.py        `nexo audit`: the uses of C (implicit unsafe, D129)
  docs.py         `nexo docs --for-ai`: stdlib/api.json + the program (D130)
  suggest.py      `nexo test --suggest`: edge cases run by a probe (D131)
  intent.py       `nexo check --intent`: comments against code with ask (D132)
  fmt.py          `nexo fmt`: token-preserving formatter (D85)
  lint.py         `nexo lint` / `nexo fix`: warnings and safe fixes (D86)
  lsp.py          `nexo lsp`: language server (D87)
  cli.py          `nexo file.nexo`, `nexo` (REPL), `nexo test|fmt|lint|fix|check|audit|lsp`,
                  `nexo test --suggest`, `nexo check --intent`, `nexo docs --for-ai`,
                  `nexo db diff|migrate`, `nexo ai setup|usage`; phase 5:
                  `nexo plan|apply|destroy|drift|cost|import`, `nexo ci run|export`,
                  `nexo flags set`, `nexo privacy report`
tests/
  conformance/    .nexo + .expected pairs (stdout, or the full error text);
                  a .nexo without .expected is a module the cases use
  infra/          NAME.nexo + NAME.expected (nexo plan; tests/test_infra.py)
  ci/             pipelines and the files nexo ci export writes
  cloud_emulator.py  local stand-in for the AWS, Azure, GCP and k8s APIs
  fmt/            NAME.nexo + NAME.formatted (nexo fmt)
  lint/           NAME.nexo + NAME.expected (nexo lint) + NAME.fixed (nexo fix)
  unit/           pytest unit tests per layer
compiler/
  diag/           message catalog and the section 14 renderer
  nexoc/          the `nexo` driver: lexer, parser, checker, modules (D124), resolve
                  (scopes, closures), typecheck (gradual), codegen (LLVM IR),
                  fmt, lint, lsp (+ json), the REPL, cheader (C headers
                  read at compile time), ai_cmd, phase5 (the commands of
                  flags, privacy, infra and ci); phase 6: narrow (ints and
                  records of known types, D126), sbom, audit, docs,
                  suggest, intent
  runtime/        static library linked into every compiled program
                  (repl.rs: the journal the REPL replays; library.rs: the
                  builtins of phase 4, ids from 100; time, text, crypto,
                  http, db, web, ai, clib, pylib; phase 5: cfg, flags,
                  privacy, stream, infra, infra_cloud, ci, ci_export)
  tests/programs/ extra programs for the differential test
  scripts/        conformance.py, differential.py, fuzz.py (random programs
                  in both phases), fix_rate.py (errors with a fix)
  bench/          benchmarks (run.sh; compare.py against rust/*.rs; goals.py
                  measures the goals of the specification -> goals.json; languages.py
                  against c, rust, go, java, js, python -> languages.json, shown
                  on the site by site.py)
examples/         real programs; NAME.expected (and NAME.stdin) checked by
                  tests/test_examples.py; they are formatted and lint-clean;
  shop_api/       the exit criterion of phase 4 (tests/test_exit_criterion.py)
  deploy/         the exit criterion of phase 5 (tests/test_exit_criterion_5.py)
editors/vscode/   the VS Code extension (npm install && npm test)
docs/decisions.md every ambiguity found and the choice made
docs/guarantees.md the state of each guarantee of the specification (phase 6)
docs/index.html, reference.html  the GitHub Pages site; reference.html is built from
                  docs/reference/ (page.tmpl + the programs it runs) by
                  docs/reference/build.py
```

## Running

```
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                 # all tests
.venv/bin/nexo examples/x.nexo   # run a program
.venv/bin/nexo                   # REPL
.venv/bin/nexo test [path]       # run `test` blocks
.venv/bin/nexo fmt|lint|fix|check [path]
.venv/bin/nexo lsp               # language server on stdio
.venv/bin/nexo plan|apply|drift|destroy [--stage prod] [file]   # nexo.infra
.venv/bin/nexo ci run [--yes] | ci export github [file]         # nexo.ci
python3 tests/cloud_emulator.py 8765   # the cloud APIs, locally (prints the env)
```

Phase 1 (needs cargo and clang):

```
cd compiler && cargo build && cargo test
compiler/target/debug/nexo file.nexo          # compile and run
compiler/target/debug/nexo build --release file.nexo -o app   # + app.sbom.json
python3 compiler/scripts/conformance.py       # conformance + examples
.venv/bin/python compiler/scripts/differential.py  # same output as phase 0
.venv/bin/python compiler/scripts/fuzz.py --count=200 # random programs, both phases
python3 compiler/bench/goals.py                    # the performance goals (needs a release build)
```

`tests/test_compiler.py` runs the conformance suite and the examples with
the compiled binaries inside pytest (skipped without cargo/clang).

Conformance pairs: `tests/conformance/NAME.nexo` runs from the directory
`tests/conformance/` and its stdout (plus the rendered error, if the program
fails) must equal `NAME.expected` exactly. Messages are in English (the
only language, D125); a `NAME.args` file holds extra CLI arguments placed
before the file name, such as `test`.

## Scope (phases 0 to 6)

Included: lexer/parser per section 24; modules of the program's own
files (`use shop.payments`, `pub`, D124); constants and `var`; `type`
(records, unions, enums with data, methods); `trait`; `fn` with named and
default args; lambdas; if/elif/else, for, while, repeat, match with guards;
comprehensions; errors (`T!`, `?`, `try`/`else e`, `fail()`, ignoring a `T!`
is an error); `T?` and `??`; int (overflow detected), float, decimal, money;
pipelines (`where map pick group by sum count avg sort by take each save`,
`it`, bare column names); `expect`/`ensure`/`old()`; `test` and
`assert`; stdlib print, input, read, write, read_lines, len, range, and
JSON/CSV.

Phase 2: reference counting with a leak checker (`NEXO_LEAKCHECK=1`,
on in every suite); lists, maps and sets copied on write, records
shared; `weak T?` fields and the type-cycle error; `parallel for` /
`parallel:` on threads (results and output in task order, the race
rules of D80-D81); `use nexo.sync` with `spawn`, `channel`, `select`,
`shared` (D82); proven contracts and `--trust-ensure` (D83); the fast
paths of D84 (`compiler/bench/compare.py`).

Phase 3: `nexo fmt` (no options, same tokens, D85); `nexo lint` and
`nexo fix` (the warnings and safe fixes of D86, also safe fixes of
compile errors); `nexo check`; `nexo lsp` (D87); the compiler's REPL
(D88); the VS Code extension. Every message has its fix sentence (tests
in tests/unit/test_errors.py check both catalogs). Both phases format,
lint and answer LSP requests alike (tests/test_fmt.py, test_lint.py,
test_lsp.py, test_repl.py).

Phase 4: module blocks and annotations are rewritten by both parsers into
calls of `__` builtins (D90); library values are `Native` in both
runtimes (D92). Standard library: time, text (`find`, `regex`, `render`),
`move_file`, `log` (hides `@private`), `convert(to: json|csv)`, crypto
and secrets, the `http` client and `fake` (D92-D99, D105, D112).
`nexo.db` on SQLite with pipelines translated to SQL and migrations
(D100-D101); `nexo.web` servers (D102-D104); `nexo.ai`: `ask`, blocks,
tools and agents (D106-D109); `use c` and `use python` (D110-D111).
Tests that need a network use local servers: tests/test_http.py,
test_web.py, test_db.py, test_ai.py, test_interop.py,
test_exit_criterion.py.

Phase 5: the blocks of D114 (`infra`, `stage`, `pipeline`, `flags`) are
read by both parsers into `__cfg` entries. `nexo.infra` (D118-D121): the
portable resources of section 11 and `native` on AWS, Azure, GCP and
k8s from one provider table, plan/apply/destroy/drift/cost/import
through each cloud's own API, state in `.nexo/state` or S3, references
between infra blocks. `nexo.ci` (D122-D123): pipelines, `nexo ci run`,
`nexo ci export github|gitlab|azure|jenkins`. `nexo.stream` on a local
broker (D117), flags (D115), privacy (D116). Tests that talk to clouds
use tests/cloud_emulator.py: tests/test_infra.py, test_ci.py,
test_exit_criterion_5.py.

Phase 6: the fast paths of D126 (records of known types, fields inside
the object, counted range loops, counts without atomics until a second
thread starts); linear name resolution and `-O0` in development mode
(D127); release builds that are the same byte for byte, with
`<program>.sbom.json` (D128); `nexo audit` (D129), `nexo docs --for-ai`
from nexo/stdlib/api.json (D130), `nexo test --suggest` (D131) and `nexo
check --intent` through `ask` (D132), in both phases with the same output
(tests/test_phase6.py). External audits, user studies, the package
registry and the program `policy` are not code; docs/guarantees.md says
what each guarantee needs (D133).

Excluded (do not implement yet): ml, ui, notify, the program policy,
every/lifecycle, canary and blue/green releases, chaos, AI budgets and
MCP, vault and cloud secret managers, brokers on the network,
PostgreSQL, use rust/wasm, YAML/XML/Parquet (D89, D112, D113).

## Rules

- Phase 1: every change keeps the conformance suite at 100% and the
  differential test without unexpected differences; a new intended
  difference goes to compiler/tests/differences.txt and docs/decisions.md.
- Messages exist in nexo/errors.py and compiler/diag/src/catalog.rs; keep
  them in sync (the same English text). Every message that reports a problem has its `fix.` sentence.
- Phase 3: fmt, lint/fix and lsp give the same output in both phases; a
  change to one goes to the other in the same commit.
- Build in layers (lexer, parser, interpreter, stdlib); each layer has
  passing tests before moving on.
- Conformance tests come from the examples in the specification.
- Error messages follow the section 14 format exactly: file and line,
  highlighted snippet, what went wrong, how to fix it, "Did you mean...?".
  Messages are in English only (D125): no `--lang`, one text per key in
  `nexo/errors.py`, the same text in `compiler/diag/src/catalog.rs`.
- Unhandled errors and compile errors are two different things: error
  values (`NexoFailure`) are caught by `try`; diagnostics (`NexoError`)
  never are. See docs/decisions.md D17-D24.
- Record every new ambiguity in docs/decisions.md (next number in sequence).
- One small commit per working step. pytest must stay at 100%.
