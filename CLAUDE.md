# Nexo — phase 0 prototype

A Python 3.12 tree-walking interpreter for the core of Nexo, a new
programming language. This is phase 0 of section 25 of the specification.

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
  stdlib/         builtins, io, data (JSON/CSV)
  cli.py          `nexo file.nexo`, `nexo` (REPL), `nexo test`
tests/
  conformance/    .nexo + .expected pairs (stdout, or the full error text)
  unit/           pytest unit tests per layer
examples/         real programs; NAME.expected (and NAME.stdin) checked by
                  tests/test_examples.py
docs/decisions.md every ambiguity found and the choice made
```

## Running

```
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                 # all tests
.venv/bin/nexo examples/x.nexo   # run a program
.venv/bin/nexo                   # REPL
.venv/bin/nexo test [path]       # run `test` blocks
.venv/bin/nexo --lang en file.nexo
```

Conformance pairs: `tests/conformance/NAME.nexo` runs from the directory
`tests/conformance/` and its stdout (plus the rendered error, if the program
fails) must equal `NAME.expected` exactly. Messages in `.expected` files are
in Spanish (the default) unless the file starts with `# lang: en`.

## Scope (phase 0)

Included: lexer/parser per section 24; constants and `var`; `type`
(records, unions, enums with data, methods); `trait`; `fn` with named and
default args; lambdas; if/elif/else, for, while, repeat, match with guards;
comprehensions; errors (`T!`, `?`, `try`/`else e`, `fail()`, ignoring a `T!`
is an error); `T?` and `??`; int (overflow detected), float, decimal, money;
pipelines (`where map pick group by sum count avg sort by take each save`,
`it`, bare column names); `parallel for` / `parallel:` (sequential, same
error semantics and result order); `expect`/`ensure`/`old()`; `test` and
`assert`; stdlib print, input, read, write, read_lines, len, range, and
JSON/CSV.

Excluded (do not implement yet): ask, infra, CI pipelines, server, db,
stream, ml, policy, native performance.

## Rules

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
