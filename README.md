# Nexo

Nexo is a new programming language built around one idea: **one way to do
each thing**. It reads like Python, treats errors as values the compiler
won't let you ignore, does exact arithmetic for money, and has data
pipelines built into the core.

```nexo
type Line:
    product: str
    quantity: int
    price: money

lines = [Line("Keyboard", 2, 49.90 EUR), Line("Monitor", 1, 189.99 EUR)]

total = lines |> map it.price * it.quantity |> sum
print("Total with VAT: {total + total * 21%}")
for part in split(total, 3):
    print("  instalment: {part}")
```

```
Total with VAT: 350.65 EUR
  instalment: 96.60 EUR
  instalment: 96.60 EUR
  instalment: 96.59 EUR
```

> **Status: early.** This repository holds the core of the language in two
> implementations: a Python tree-walking interpreter (phase 0) and a Rust
> compiler that produces native code through LLVM (phase 1). Both run the
> same programs with the same output. Phase 2 adds the memory model,
> real `parallel`, `nexo.sync` and contracts the compiler proves. Modules
> such as `nexo.ai`, `nexo.web`, `nexo.db` and `nexo.infra` are described
> in the specification but not implemented yet.

## Contents

- [Why Nexo](#why-nexo)
- [Install](#install)
- [Quick start](#quick-start)
- [A tour of the language](#a-tour-of-the-language)
- [Examples](#examples)
- [What is implemented](#what-is-implemented)
- [Repository layout](#repository-layout)
- [Contributing](#contributing)

## Why Nexo

- **One form per construct.** `x = 1` is immutable and `var x = 1` is
  mutable; `type` covers records, unions and enums; blocks are a colon and
  four spaces. There is no second way to write any of it.
- **Errors you can't ignore.** A function that can fail returns `T!`.
  Calling it without `?` or `try` is a compile error, not a surprise at
  run time.
- **No `null` surprises.** A value that may be missing is `T?`, and `??`
  gives it a default. Missing values and errors never mix.
- **Numbers that don't lie.** `int` overflow is an error, never a silent
  wrap. `decimal` is exact, and `money` is a decimal with a currency, so
  adding EUR to USD is an error and `split` never loses a cent.
- **Pipelines in the core.** `where`, `map`, `group by`, `sum`,
  `sort by`, `take` and `save` work the same on lists and CSV files.
- **Contracts.** `expect`, `ensure` and `old()` state what a function
  needs and promises, and a broken contract reports the values involved.
- **Errors that teach.** Every message shows the file, the line, the
  highlighted code, what went wrong and how to fix it, in Spanish (the
  default) or English.

The full design lives in
[`docs/especificacion-nexo.md`](docs/especificacion-nexo.md), and every
ambiguity found while implementing it is recorded with its resolution in
[`docs/decisions.md`](docs/decisions.md).

## Install

Nexo is not packaged yet; you run it from a clone of this repository.

```sh
git clone https://github.com/marcrabadan/nexo.git
cd nexo
```

### Interpreter (phase 0)

Needs Python 3.12 or newer.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/nexo --lang en examples/invoice.nexo
```

### Compiler (phase 1)

Needs a Rust toolchain (`cargo`) and `clang`. The compiler emits LLVM IR
and uses clang to build it and link it with the Nexo runtime library.

```sh
cd compiler
cargo build --release
target/release/nexo --lang en ../examples/invoice.nexo               # compile and run
target/release/nexo build --release ../examples/invoice.nexo -o invoice  # native binary
./invoice
```

Two environment variables adjust the build: `NEXO_CC` picks the C compiler
(default `clang`) and `NEXO_RUNTIME` points to `libnexo_runtime.a`
(default: next to the `nexo` executable).

## Quick start

Save this as `hello.nexo`:

```nexo
name = "Ana"
var visits = 0
repeat 3:
    visits += 1
print("Hello {name}, visit number {visits}")

fn greet(person, greeting = "Hello"):
    return "{greeting}, {person}"

print(greet("Luis", greeting: "Hi"))

squares = [n * n for n in 1..5 if n % 2 == 1]
print(squares)
```

Run it with either implementation:

```sh
.venv/bin/nexo hello.nexo                     # interpreter
compiler/target/release/nexo hello.nexo       # compiler
```

```
Hello Ana, visit number 3
Hi, Luis
[1, 9, 25]
```

The command line:

| Command | What it does |
| --- | --- |
| `nexo file.nexo` | run a program (the compiler builds it first) |
| `nexo test [paths]` | run the `test` blocks in the given files |
| `nexo build --release file.nexo -o app` | build an optimised native binary (compiler only) |
| `nexo` | open the REPL (interpreter only) |
| `nexo --lang en ...` | show messages in English; Spanish is the default |

Make a mistake and Nexo tells you how to fix it:

```nexo
name = "Ana"
print(nmae)
```

```
Error in typo.nexo, line 2:
  print(nmae)
        ^^^^
"nmae" does not exist. Did you mean "name"?
```

## A tour of the language

### Types

`type` is the only word for user-defined data: records with methods,
enums with data, and unions of values. `trait` describes shared behaviour.

```nexo
type User:
    name: str
    age: int
    email: str?                     # may be missing

    fn is_adult(self) -> bool:
        return self.age >= 18

type Status = "active" | "closed"   # union of values

type Shape:                         # enum with data
    Circle(radius: float)
    Rect(w: float, h: float)

fn area(s: Shape) -> float:
    match s:
        Circle(r): return 3.14159 * r ** 2
        Rect(w, h): return w * h
```

### Control flow

```nexo
if age >= 18:
    print("Adult")
elif age >= 13:
    print("Teenager")
else:
    print("Child")

for i in 1..10:          # inclusive range
    print(i)

while counter < 5:
    counter += 1

match event:
    Click(x, y) if x > 100: print("right")
    Key("q") or Quit:       close()
    _:                      pass
```

### Errors

An error is a value. There are exactly two things you can do with it:
pass it up with `?`, or handle it with `try` and `else e`.

```nexo
fn parse_age(text: str) -> int!:
    return validate(text, as: int, range: 0..150)?

try:
    print(parse_age("42"))
    print(parse_age("abc"))
else e:
    print("Invalid: {e.message}")    # Invalid: "abc" is not a valid int
```

Forgetting to handle a call that can fail is caught before the program
runs:

```
Error in load.nexo, line 2:
  return read(p)
         ^^^^^^^
The call to "read" can fail and its error is ignored. Add ? at the end to pass it to the caller, or handle it with try and else e.
```

### Numbers and money

```nexo
print(0.1 + 0.2)                  # 0.30000000000000004 (float)

price: decimal = 19.99
print(price * 3)                  # 59.97, exact

invoice: money = 120.00 EUR
print(invoice * 21%)              # 25.20 EUR
print(split(100.00 EUR, 3))       # [33.34 EUR, 33.33 EUR, 33.33 EUR]
```

### Pipelines

`|>` passes the value on the left to the step on the right. With records
and CSV rows you name columns directly; with plain values the current
element is `it`.

```nexo
sales = read("data/sales.csv")

by_region = sales
  |> where amount > 0
  |> group by region
  |> sum amount
  |> sort by amount desc

by_region |> save("summary.csv")

positives = numbers |> where it > 0 |> map it * 2
```

### Contracts and tests

```nexo
fn transfer(source: Account, target: Account, amount: money):
    expect amount > 0.00 EUR
    expect source.balance >= amount
    ensure source.balance + target.balance == old(source.balance + target.balance)

    source.balance -= amount
    target.balance += amount

test "cannot transfer more than the balance":
    a = Account(owner: "A", balance: 1.00 EUR)
    b = Account(owner: "B", balance: 0.00 EUR)
    assert transfer(a, b, 2.00 EUR) fails
```

```sh
nexo --lang en test examples/bank.nexo
```

## Examples

Complete programs live in [`examples/`](examples). Each one has a
`.expected` file with its exact output, checked by the test suite.

| Program | Shows |
| --- | --- |
| [`bank.nexo`](examples/bank.nexo) | contracts with `expect`, `ensure` and `old()`; tests |
| [`config_loader.nexo`](examples/config_loader.nexo) | JSON into typed records, every failure handled with `try` |
| [`file_stats.nexo`](examples/file_stats.nexo) | line and word counts with `parallel for`, results kept in order |
| [`grades.nexo`](examples/grades.nexo) | records with methods, an enum, `match` with guards and a trait |
| [`inventory.nexo`](examples/inventory.nexo) | low-stock check on a CSV, exact restock prices, saved as JSON |
| [`invoice.nexo`](examples/invoice.nexo) | exact `money`, VAT and splitting into instalments |
| [`log_filter.nexo`](examples/log_filter.nexo) | lazy line-by-line log reading and an error summary |
| [`sales_report.nexo`](examples/sales_report.nexo) | `group by`, `sum`, `sort by` and `save` on a CSV |
| [`tip_calculator.nexo`](examples/tip_calculator.nexo) | reading and validating user input |
| [`todo.nexo`](examples/todo.nexo) | enums, `match`, events with `on`/`emit`, tests |
| [`word_count.nexo`](examples/word_count.nexo) | the five most common words in a text file |

Run any of them from the `examples/` directory so relative paths to
`data/` resolve:

```sh
cd examples
../.venv/bin/nexo --lang en sales_report.nexo
```

## What is implemented

This repository covers phases 0, 1 and 2 of the plan in section 25 of
the specification.

**Included:** constants and `var`; `type` (records, unions, enums with
data, methods); `trait`; functions with named and default arguments;
lambdas; `if`/`elif`/`else`, `for`, `while`, `repeat`, `match` with
guards; comprehensions; errors (`T!`, `?`, `try`/`else e`, `fail()`);
`T?` and `??`; `int` with overflow detection, `float`, `decimal` and
`money`; pipelines; `expect`/`ensure`/`old()`; `test` and `assert`; and
the standard library functions `print`, `input`, `read`, `write`,
`read_lines`, `len`, `range` and JSON/CSV.

**Phase 2:**

- **Memory model (section 7).** Compiled programs free memory by
  reference counting. `NEXO_LEAKCHECK=1` reports any object left at exit,
  and every test suite runs with it.
- **Values and references.** Lists, maps and sets are values, copied on
  write; records are shared references.
- **Weak fields.** A `weak T?` field does not keep its target alive, and a
  cycle of strong fields between types is a compile error.
- **Real `parallel`.** `parallel for` and `parallel:` run on threads.
  Output and results stay in task order. A task may not change a record
  or variable that another task can see.
- **`nexo.sync`.** `use nexo.sync` enables `spawn`, `channel`, `select`
  and `shared`. A deadlock is reported instead of hanging.
- **Contracts the compiler proves.** A proven `ensure` costs nothing at
  run time (D83), and `nexo build --release --trust-ensure` leaves out
  the checks it could not prove.

**Not yet:** the other official modules (`ai`, `web`, `db`, `infra`,
`ci`, `stream`, `ml`) and `when`/`every`/`lifecycle`. Using them gives a
clear "not available in this phase" error rather than being silently
ignored.

**Performance.** `compiler/bench/compare.py` times each benchmark against
the same program in Rust (`rustc -O` with overflow checks, since Nexo
checks overflow too). These are the latest ratios of Nexo time to Rust
time:

| Benchmark | Ratio |
| --- | --- |
| integer loop | 1.3× |
| `parallel for` | 1.3× |
| recursive `fib` | 2.0× |
| strings | 3.5× |
| lists and pipelines | 7× |
| maps | 7× |
| records | 8× |

Code on ints is under the phase 2 goal of 2× Rust. Code on
collections and records is not yet: every value is boxed and
reference-counted, and each element passes through the runtime. The
details are in D84.

**Interpreter and compiler differences.** The two implementations print
the same output for every program. The compiler catches some mistakes
earlier (unknown names and certain type errors are reported before the
program runs), has no REPL, and fixes the message language when the
program is compiled. Each difference is recorded in
[`docs/decisions.md`](docs/decisions.md) (D67 to D75).

## Repository layout

```
nexo/                 phase 0: Python interpreter
  lexer.py parser.py  tokens and recursive-descent parser (section 24)
  checker.py          static pre-pass for compile errors
  interpreter.py      tree-walking evaluator
  numbers.py types.py arithmetic, money, units and runtime values
  errors.py           error renderer and es/en messages
  stdlib/             builtins, io, JSON/CSV
  cli.py              the `nexo` command
compiler/             phase 1: Rust compiler
  nexoc/              driver, lexer, parser, resolver, type checker, LLVM codegen
  runtime/            static library linked into every program
  diag/               es/en message catalog
  scripts/            conformance and differential test runners
  bench/              benchmarks
tests/
  conformance/        .nexo programs with their exact .expected output
  unit/               pytest unit tests per layer
examples/             complete programs with expected output
docs/
  especificacion-nexo.md  the specification (source of truth)
  decisions.md            every ambiguity and the choice made
```

## Contributing

Contributions are welcome. A few rules keep the two implementations
honest:

1. **The specification is the source of truth.** If something isn't in
   [`docs/especificacion-nexo.md`](docs/especificacion-nexo.md), don't
   invent it. When the specification is ambiguous, pick the simplest
   option that keeps "one way to do each thing" and record it in
   [`docs/decisions.md`](docs/decisions.md) with the next number.
2. **Both implementations agree.** Every change keeps the conformance
   suite at 100% and the differential test free of unexpected
   differences. An intended difference goes in
   `compiler/tests/differences.txt` and `docs/decisions.md`.
3. **Every message has two languages.** New error messages need Spanish
   and English text in `nexo/errors.py`, mirrored in
   `compiler/diag/src/catalog.rs`, and follow the section 14 format:
   file and line, highlighted snippet, what went wrong, how to fix it.
4. **Small commits, green tests.** One small commit per working step, with
   the whole test suite passing.

Run the tests:

```sh
.venv/bin/pytest                                     # everything (compiled tests skip without cargo/clang)
cd compiler && cargo test && cd ..                   # compiler unit tests
python3 compiler/scripts/conformance.py              # conformance suite with the compiler
.venv/bin/python compiler/scripts/differential.py    # compiler output equals interpreter output
compiler/bench/run.sh                                # benchmarks: native vs interpreter
python3 compiler/bench/compare.py                    # benchmarks: native vs Rust
```

To add a conformance test, put `NAME.nexo` and its exact output
`NAME.expected` in `tests/conformance/` (messages in Spanish, the
default). Extra CLI arguments, such as `--lang en`, go in `NAME.args`.
