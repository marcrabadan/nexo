# Nexo — phases 0 to 3

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
  `editors/vscode` (D85-D88).

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
  numbers.py      section 17 arithmetic: int overflow, float/decimal, money, units
  types.py        runtime values: decimal/money/units, records, enums, errors
  errors.py       error type + section 14 renderer + i18n (es default, en)
  stdlib/         builtins, io, data (JSON/CSV), sync (nexo.sync)
  fmt.py          `nexo fmt`: token-preserving formatter (D85)
  lint.py         `nexo lint` / `nexo fix`: warnings and safe fixes (D86)
  lsp.py          `nexo lsp`: language server (D87)
  cli.py          `nexo file.nexo`, `nexo` (REPL), `nexo test|fmt|lint|fix|check|lsp`
tests/
  conformance/    .nexo + .expected pairs (stdout, or the full error text)
  fmt/            NAME.nexo + NAME.formatted (nexo fmt)
  lint/           NAME.nexo + NAME.expected (nexo lint) + NAME.fixed (nexo fix)
  unit/           pytest unit tests per layer
compiler/
  diag/           es/en message catalog and the section 14 renderer
  nexoc/          the `nexo` driver: lexer, parser, checker, resolve
                  (scopes, closures), typecheck (gradual), codegen (LLVM IR),
                  fmt, lint, lsp (+ json), the REPL
  runtime/        static library linked into every compiled program
                  (repl.rs: the journal the REPL replays)
  tests/programs/ extra programs for the differential test
  scripts/        conformance.py, differential.py
  bench/          benchmarks (run.sh; compare.py against rust/*.rs)
examples/         real programs; NAME.expected (and NAME.stdin) checked by
                  tests/test_examples.py; they are formatted and lint-clean
editors/vscode/   the VS Code extension (npm install && npm test)
docs/decisions.md every ambiguity found and the choice made
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
.venv/bin/nexo --lang en file.nexo
```

Phase 1 (needs cargo and clang):

```
cd compiler && cargo build && cargo test
compiler/target/debug/nexo file.nexo          # compile and run
compiler/target/debug/nexo build --release file.nexo -o app
python3 compiler/scripts/conformance.py       # conformance + examples
.venv/bin/python compiler/scripts/differential.py  # same output as phase 0
```

`tests/test_compiler.py` runs the conformance suite and the examples with
the compiled binaries inside pytest (skipped without cargo/clang).

Conformance pairs: `tests/conformance/NAME.nexo` runs from the directory
`tests/conformance/` and its stdout (plus the rendered error, if the program
fails) must equal `NAME.expected` exactly. Messages in `.expected` files are
in Spanish (the default); a `NAME.args` file holds extra CLI arguments
placed before the file name, such as `--lang en` (see `tests/conformance/en/`)
or `test`.

## Scope (phases 0 to 3)

Included: lexer/parser per section 24; constants and `var`; `type`
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

Excluded (do not implement yet): ask, infra, CI pipelines, server, db,
stream, ml, policy.

## Rules

- Phase 1: every change keeps the conformance suite at 100% and the
  differential test without unexpected differences; a new intended
  difference goes to compiler/tests/differences.txt and docs/decisions.md.
- Messages exist in nexo/errors.py and compiler/diag/src/catalog.rs; keep
  them in sync. Every message that reports a problem has its `fix.` sentence.
- Phase 3: fmt, lint/fix and lsp give the same output in both phases; a
  change to one goes to the other in the same commit.
- Build in layers (lexer, parser, interpreter, stdlib); each layer has
  passing tests before moving on.
- Conformance tests come from the examples in the specification.
- Error messages follow the section 14 format exactly: file and line,
  highlighted snippet, what went wrong, how to fix it, "Did you mean...?".
  Spanish by default, English with `--lang en`. Every new message needs
  both translations in `nexo/errors.py`.
- Unhandled errors and compile errors are two different things: error
  values (`NexoFailure`) are caught by `try`; diagnostics (`NexoError`)
  never are. See docs/decisions.md D17-D24.
- Record every new ambiguity in docs/decisions.md (next number in sequence).
- One small commit per working step. pytest must stay at 100%.
