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
  state is in docs/guarantees.md (D126-D136); the program `policy` of
  section 9 with `nexo policy show|suggest` (D142); the patterns of
  section 10 with their own word, `single`, `states`, `protect`,
  `action` with `undo()` and `workflow` (D143-D145, D150-D151); `nexo mcp` (D146), `nexo docs` (D147) and `nexo new` (D148).

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
                  and of the packages in .nexo/packages (D174)
  packages.py     `nexo add` / `nexo publish`, nexo.lock, the registry (D174)
  numbers.py      section 17 arithmetic: int overflow, float/decimal, money, units
  types.py        runtime values: decimal/money/units, records, enums, errors
  errors.py       error type + section 14 renderer + message catalog (English, D125)
  stdlib/         builtins, io, data (JSON/CSV), sync (nexo.sync); phase 4:
                  clock, text, crypto, http, db, pg (PostgreSQL, D163),
                  mysql (D170), docdb (document stores: mongo D171,
                  elastic D172),
                  web, ai, clib (use c), pylib (use python); phase 5: cfg (the blocks of D114),
                  flags, privacy, stream, stream_cloud (SQS, Kinesis,
                  Service Bus, Pub/Sub, D168), stream_net (D169: kafka,
                  amqp, websocket), infra (model and plan),
                  infra_cloud (the clients and commands),
                  infra_providers.json (shared by both phases), ci,
                  ci_export; phase 6: api.json (what a program can use,
                  shared by both phases), policy (the program policy, D142),
                  patterns (single, states, protect, action and
                  workflow, D143-D145, D150-D151)
  audit.py        `nexo audit`: the uses of C (implicit unsafe, D129)
  docs.py         `nexo docs --for-ai`: stdlib/api.json + the program (D130)
  suggest.py      `nexo test --suggest`: edge cases run by a probe (D131)
  cover.py        `nexo test --coverage`: the branches the tests ran (D155)
  intent.py       `nexo check --intent`: comments against code with ask (D132)
  fmt.py          `nexo fmt`: token-preserving formatter (D85)
  lint.py         `nexo lint` / `nexo fix`: warnings and safe fixes (D86)
  lsp.py          `nexo lsp`: language server (D87)
  mcp.py          `nexo mcp`: Model Context Protocol server for assistants (D146)
  cli.py          `nexo file.nexo`, `nexo` (REPL), `nexo test|fmt|lint|fix|check|audit|lsp`,
                  `nexo test --suggest|--coverage`, `nexo check --intent`, `nexo docs --for-ai`,
                  `nexo db diff|migrate`, `nexo ai setup|usage`; phase 5:
                  `nexo plan|apply|destroy|drift|cost|import`, `nexo ci run|export`,
                  `nexo flags set`, `nexo privacy report`; `nexo policy show|suggest`;
                  `nexo new`, `nexo docs`, `nexo mcp`, `nexo add|publish`
tests/
  conformance/    .nexo + .expected pairs (stdout, or the full error text);
                  a .nexo without .expected is a module the cases use
  infra/          NAME.nexo + NAME.expected (nexo plan; tests/test_infra.py)
  ci/             pipelines and the files nexo ci export writes
  cloud_emulator.py  local stand-in for the AWS, Azure, GCP and k8s APIs
                  (and the data planes of the cloud streams, D168)
  kafka_broker.py, amqp_broker.py, ws_server.py  brokers for the tests of
                  D169, each written from its protocol's specification
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
                  records of known types, D126), sbom, split (parallel
                  development builds, D135), scalar (values as two words
                  in development mode, D141), audit, docs, suggest, intent,
                  cover (`nexo test --coverage`, D155), policy (`nexo policy`, D142), mcp (`nexo mcp`, D146),
                  packages (`nexo add|publish`, D174)
  runtime/        static library linked into every compiled program
                  (repl.rs: the journal the REPL replays; library.rs: the
                  builtins of phase 4, ids from 100; time, text, crypto,
                  http, db, pg, mysql, docdb, mongo, elastic, web, ai, clib,
                  pylib; phase 5: cfg, flags,
                  privacy, stream, stream_cloud, stream_net, kafka, amqp,
                  websocket, infra, infra_cloud, ci, ci_export;
                  policy.rs: the checks of the program policy, D142;
                  patterns.rs: single, states, protect, action and
                  workflow, D143-D145, D150-D151)
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
  events/         streams and flags, run with `nexo test` (tests/test_examples.py)
editors/vscode/   the VS Code extension (npm install && npm test)
docs/decisions.md every ambiguity found and the choice made
docs/guarantees.md the state of each guarantee of the specification (phase 6)
docs/index.html, reference.html  the GitHub Pages site; reference.html is built from
                  docs/reference/ (page.tmpl + the programs it runs) by
                  docs/reference/build.py
docs/playground.html  the playground (D176): phase 0 on Pyodide in a worker
                  (assets/playground*.js); docs/playground/build.py writes
                  its nexo.zip and examples.json
```

## Running

```
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                 # all tests
.venv/bin/nexo examples/x.nexo   # run a program
.venv/bin/nexo                   # REPL
.venv/bin/nexo test [path]       # run `test` blocks
.venv/bin/nexo test --coverage [path]   # and the branches they ran
.venv/bin/nexo fmt|lint|fix|check [path]
.venv/bin/nexo lsp               # language server on stdio
.venv/bin/nexo mcp               # MCP server on stdio, for AI assistants
.venv/bin/nexo new shop          # a project: shop/nexo.toml, shop/src/main.nexo
.venv/bin/nexo docs [path]       # Markdown from the ## comments
.venv/bin/nexo add money-format  # a package, signed, into nexo.toml and nexo.lock
.venv/bin/nexo publish           # the project, signed, into the registry folder of NEXO_REGISTRY
.venv/bin/nexo plan|apply|drift|destroy [--stage prod] [file]   # nexo.infra
.venv/bin/nexo ci run [--yes] | ci export github [file]         # nexo.ci
python3 tests/cloud_emulator.py 8765   # the cloud APIs, locally (prints the env)
```

Phase 1 (needs cargo and clang):

```
cd compiler && cargo build && cargo test
compiler/target/debug/nexo file.nexo          # compile and run
compiler/target/debug/nexo build --release file.nexo -o app   # + app.sbom.json
compiler/scripts/web_runtime.sh                    # the runtime for --target web (target/web)
compiler/target/debug/nexo build --target web file.nexo -o app # app.wasm, app.js, app.html
python3 compiler/scripts/web_conformance.py        # the suite as WebAssembly, under Node
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
(D100-D101), `pick`, `group by` and relations in SQL (D161-D162), and
PostgreSQL (D163) and MySQL (D170) through their own protocols, and the
document stores MongoDB (D171) and Elasticsearch (D172); `nexo.web` servers
(D102-D104); `nexo.ai`: `ask`, blocks, tools and agents (D106-D109),
`stream: true` as the answer arrives (D164), budgets (D165),
`mcp("name")` tools (D166) and the providers mistral, gemini, azure and
bedrock (D173, tests/test_ai_providers.py); `use c` and `use python` (D110-D111).
Tests that need a network use local servers: tests/test_http.py,
test_web.py, test_db.py, test_postgres.py (a local PostgreSQL started
by the test, skipped without initdb), test_mysql.py (likewise, without
mysqld), test_docdb.py (on tests/mongo_server.py and es_server.py),
test_ai.py, test_interop.py,
test_exit_criterion.py.

Phase 5: the blocks of D114 (`infra`, `stage`, `pipeline`, `flags`) are
read by both parsers into `__cfg` entries. `nexo.infra` (D118-D121): the
portable resources of section 11 and `native` on AWS, Azure, GCP and
k8s from one provider table, plan/apply/destroy/drift/cost/import
through each cloud's own API, state in `.nexo/state` or S3, references
between infra blocks. `nexo.ci` (D122-D123): pipelines, `nexo ci run`,
`nexo ci export github|gitlab|azure|jenkins`. `nexo.stream` on a local
broker (D117) with partitions handled in parallel (D167), on the clouds'
queues (D168) and on brokers on the network: Kafka, Event Hubs, RabbitMQ
and WebSocket (D169); flags (D115), privacy (D116). Tests that talk to
clouds use tests/cloud_emulator.py: tests/test_infra.py, test_ci.py,
test_stream_cloud.py, test_exit_criterion_5.py; tests/test_stream_net.py
uses the test brokers.

Phase 6: the fast paths of D126 (records of known types, fields inside
the object, counted range loops, counts without atomics until a second
thread starts) and D134 (fused `where`/`map`/`sum` pipelines, lists read
in place, int-key map index, a per-thread pool of heap objects); short
texts inside their object (D136); linear name resolution and `-O0` in
development mode (D127); development builds that call the count helpers,
compile large programs in parallel parts (compiler/nexoc/src/split.rs)
and skip checks for names certainly defined (D135); release builds that are the same byte for byte, with
`<program>.sbom.json` (D128); `nexo audit` (D129), `nexo docs --for-ai`
from nexo/stdlib/api.json (D130), `nexo test --suggest` (D131), `nexo test
--coverage` (branch coverage, D155: nexo/cover.py and
compiler/nexoc/src/cover.rs add a `__cover(n)` call to each branch) and
`nexo check --intent` through `ask` (D132), in both phases with the same
output (tests/test_phase6.py). External audits, user studies, the package
registry are not code; docs/guarantees.md says what each guarantee
needs (D133).

The program policy (D142): `policy:` with `allow`/`deny` of `read`,
`write`, `net`, `env`, `shell`, `c` and `python`, read by both parsers
into `__policy(...)` as the first statement. Every access of the
standard library calls `policy.check` (nexo/stdlib/policy.py,
compiler/runtime/src/policy.rs) before it happens. A release build
stops what is not allowed and needs a policy; development mode warns
once, except shell commands, which always need `allow shell` (so a
pipeline with `run` needs a policy). `nexo policy show|suggest`. The
sandbox escape tests are in tests/test_policy.py.

The patterns of section 10 (D143-D145): `single name:` and `states
name:` are module blocks read into `__single(...)` and `__states(...)`;
`protect(value, fails:, wait:):` is rewritten like `db.transaction`
into `__protect` before a `try` (a new error kind, `CircuitOpen`).
`action name(params):` with `do:` and `undo:` becomes two functions,
the action and `__undo_name`, and `undo()` reverses the last one (D150);
`workflow name(params):` with `step` lines becomes a function whose
steps run inside a `try`, with their undos, last first, in its `else`,
and whose state is saved in `.nexo/workflows/` after every step (D151;
the resume after a stop is in tests/test_phase6.py).
`nexo mcp` (D146) offers `check`, `run_tests`, `docs_for_ai`, `plan` and
`format` over stdio by running the same binary's commands
(tests/test_mcp.py); `nexo new` and `nexo docs` are in tests/test_project.py.

Packages (D174): `nexo add [name] [--accept-risk]` and `nexo publish`
in both phases, with the same files byte for byte. A registry is static
files (a folder, or HTTPS) with Ed25519-signed archives; requirements
are `^MAJOR.MINOR`; one version of each package, the highest every
requirement accepts; nexo.lock pins the version, sha256 and key;
`[permissions]` are checked against the project's policies and critical
advisories stop the install. `use pkg` loads .nexo/packages/pkg/lib.nexo.
Tests in tests/test_packages.py.

WebAssembly (D175, D176): `nexo build --target web` compiles for
wasm32-wasip1 and writes the program, its WASI code for a page
(compiler/nexoc/src/web.js) and a page; what compiled code shares with the
runtime is u64 on every target. The playground runs phase 0 on Pyodide.
tests/test_playground.py checks both (skipped without node, Pyodide or
target/web).

Excluded (do not implement yet): ml, ui, notify, the registry service, every/lifecycle, canary and blue/green releases, chaos,
vault and cloud secret managers, use rust/wasm, YAML/XML/Parquet (D89,
D112, D113).

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
