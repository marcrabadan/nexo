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
> real `parallel`, `nexo.sync` and contracts the compiler proves. Phase 3
> adds the tools: `nexo fmt`, `lint`, `fix`, `check`, `lsp`, a REPL in the
> compiler and a VS Code extension. Phase 4 adds the standard library and
> the modules `nexo.ai`, `nexo.web` and `nexo.db`, with C and Python
> interoperability. Phase 5 adds cloud and operations: `nexo.infra` for
> AWS, Azure, GCP and Kubernetes, `nexo.ci` pipelines, `nexo.stream`,
> feature flags and privacy. The modules of later phases are described
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
  highlighted code, what went wrong and how to fix it.

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

`make setup` does the rest: the virtual environment of phase 0, the
compiler of phase 1 and the VS Code extension's packages, each one when
its tools are there. `make` lists every target and `make tools` says what
is installed and what is missing. The sections below are the same steps
by hand.

### Interpreter (phase 0)

Needs Python 3.12 or newer.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/nexo examples/invoice.nexo
```

### Compiler (phase 1)

Needs a Rust toolchain (`cargo`, 1.89 or newer) and `clang`. The compiler emits LLVM IR
and uses clang to build it and link it with the Nexo runtime library.

```sh
cd compiler
cargo build --release
target/release/nexo ../examples/invoice.nexo               # compile and run
target/release/nexo build --release ../examples/invoice.nexo -o invoice  # native binary
./invoice
```

Two environment variables adjust the build: `NEXO_CC` picks the C compiler
(default `clang`) and `NEXO_RUNTIME` points to `libnexo_runtime.a`
(default: next to the `nexo` executable).

### Nexo Shield

`nexo security check|run|trace|audit app.nexo` adds strict policy execution and
structured security decisions in both implementations. Shield rejects native
C, Python and shell integrations until they can be isolated. It enforces Nexo
API permissions; it is not an OS sandbox. The policy alone does not prevent DNS
rebinding, mediate direct native syscalls or impose process resource limits.
Capability-based API permissions do not imply confinement of arbitrary native code.
See [the security model](docs/shield.md)
for trust boundaries and tests. [Phase 2](docs/shield-isolation.md) adds
`security probe`, opt-in Linux offline workers, hard limits and synchronous
dependency ceilings with `--dependencies permissions.json`. OS execution fails
closed on hosts without the required Landlock/seccomp support.
[Docker deployment](docs/shield-docker.md) packages the native runtime with
read-only sources, no external network and enforced container resource limits.

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
| `nexo` | open the REPL |
| `nexo fmt [paths]` | format the files in place (no options) |
| `nexo lint [paths]` | warn about questionable code, each warning with its fix |
| `nexo fix [paths]` | apply the safe fixes of warnings and errors |
| `nexo check [paths]` | report compile errors without running anything |
| `nexo lsp` | language server for any editor (the VS Code extension uses it) |
| `nexo policy show [file]` | what the program's `policy` lets it read, write, reach and run |
| `nexo policy suggest [file]` | the smallest `policy` that lets its tests run |

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

### Modules

A module is a file of the program, named by its path from the folder of
`nexo.toml` (or of the program). Only what is `pub` is exported, and it is
used through the module's name:

```nexo
# shop/payments.nexo
pub type Status:
    Paid(amount: int)
    Pending

pub fn charge(amount: int) -> Status:
    if amount == 0:
        return Pending
    return Paid(amount)
```

```nexo
# main.nexo
use shop.payments

match payments.charge(10):
    payments.Paid(n): print("paid {n}")
    payments.Pending: print("nothing to pay")
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
nexo test examples/bank.nexo
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
| [`shop_api/shop.nexo`](examples/shop_api/shop.nexo) | a web API with SQLite and `ask`: products, reviews the AI analyses, JWT login, tests with `fake ask` |

Run any of them from the `examples/` directory so relative paths to
`data/` resolve:

```sh
cd examples
../.venv/bin/nexo sales_report.nexo
```

## What is implemented

This repository covers phases 0 to 5 of the plan in section 25 of
the specification.

**Included:** modules of the program's own files (`use shop.payments`
loads `shop/payments.nexo`; its `pub` functions and types are used as
`payments.charge(...)`); constants and `var`; `type` (records, unions, enums with
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

**Phase 3: tooling.** Every tool exists in both implementations and gives
the same output in both:

- **`nexo fmt`** formats with no options (D85). It works on tokens, so a
  formatted file always means the same as before, and it keeps your line
  breaks. `|>` lines sit two spaces under their statement, as in the
  specification.
- **`nexo lint`** reports unused names, `var` that never changes,
  `for i in 1..n` that should be `repeat n`, unreachable code, a pipeline
  that never runs, `== true`, duplicate map keys and more (D86). Each
  warning says how to fix it. **`nexo fix`** applies the fixes that cannot
  change what a correct program does, including the safe fixes of compile
  errors (a missing `!`, a superfluous `?`, `else:` without a name).
- **Every error has a proposed fix:** each message in the catalog has a
  second sentence that says how to correct it, and the tests check it.
- **`nexo lsp`** is a language server (D87): errors and warnings as you
  type, quick fixes, "apply the safe fixes", formatting, go to definition,
  hover with the `##` comments, the outline and completion.
- **[VS Code extension](editors/vscode):** highlighting, the language
  server and commands to run a file and its tests.
- **A REPL in the compiler** (D88): each input recompiles and reruns the
  session. Earlier inputs replay silently from a journal, so their output,
  `input()` answers and file writes do not happen again.

```
$ nexo lint app.nexo
Warning in app.nexo, line 3:
  var total = 0
  ^^^
"total" is declared with var but never changes. Remove var: a name that does not change is immutable.
Warnings: 1. Safe fixes with nexo fix: 1.
```

**Phase 4: library and modules.** Both implementations again give the
same output; the decisions are D89 to D112.

- **Standard library (section 15):** `now` and `today`, dates, times,
  durations and IANA time zones; `find`, `regex` and `render` templates;
  `move_file`; `convert(x, to: json | csv)`; an `http` client with
  retries; `fake` in tests; `log`, which hides `@private` fields.
- **Security (section 9):** `secret("NAME")` values that can never be
  printed, `password.hash`/`check`, `encrypt`/`decrypt` (AES-256-GCM),
  `sign`/`verify` (Ed25519) and `validate(x, as: email)`. A key written in
  the code is a compile error.
- **`nexo.db`:** SQLite through `connect`; `@table` types are the schema,
  with `ref` and `many` relations. Pipelines on tables become
  parameterised SQL, and there are transactions, `db.sql` and
  `nexo db diff` / `nexo db migrate`.
- **`nexo.web`:** `server` blocks with typed routes, JWT `auth`,
  `@role`, `@limit`, `@public`, secure headers, CSRF and HTTPS
  redirects. Errors become status codes, and `nexo test` serves the API
  on a free port.
- **`nexo.ai`:** `ai`, `ai models` and `ai profile` blocks, plus
  nexo.toml and `nexo ai setup`. `ask` works with Anthropic, OpenAI and
  Ollama; it validates typed answers with retries and has `fallback`,
  `stream`, `guard` and `redact`. There are also `@tool` functions,
  `agent` blocks with `confirm`, and `nexo ai usage`. The
  [AI-native lifecycle](docs/agent-lifecycle.md) adds typed input/output guards,
  inherited cancellation/deadlines, token and monetary reservations,
  operator-granted recovery and explicit opt-in OTLP tracing.
- **Interoperability (section 18):** `use c "libz"` reads the header
  through the C preprocessor and calls with int, float, bool and text
  arguments. `use python statistics as stats` converts data at the
  boundary; the compiler loads libpython at run time.

```nexo
o: Opinion = ask("Analyse this review", review)?   # validated, up to 3 retries

server shop:
    auth = jwt(key: secret("JWT_KEY"))

    @role("admin")
    post "/products"(product: NewProduct):
        return db.Product.add(name: product.name, price: product.price)
```

**Phase 5: cloud and operations.** Both implementations give the same
output and read and write the same state; the decisions are D113 to
D123. The exit criterion, an app that deploys to AWS, Azure, GCP and
k8s from the same file, is [`examples/deploy/shop.nexo`](examples/deploy/shop.nexo).

- **`nexo.infra` (section 11):** `infra` and `stage` blocks with the ten
  portable resources, `native`, `output`, `policy`, `budget` and
  `data_region`. `nexo plan`, `apply`, `destroy`, `drift`, `cost` and
  `import` talk to each cloud's own API (AWS Cloud Control with SigV4,
  Azure Resource Manager, the GCP REST APIs, Kubernetes server-side
  apply), with the state in `.nexo/state` or in S3, secrets that never
  reach it, and values passed between infra blocks.
- **`nexo.ci` (section 12):** `pipeline` blocks whose steps are ordinary
  code, `version = auto` from git tags and commits, `run`, `test()`,
  `build_image`, `apply`, `plan`, `approve` and `comment`; `nexo ci run`
  runs them here and `nexo ci export github|gitlab|azure|jenkins`
  writes the file of each service.
- **`nexo.stream` (section 19):** `when SOURCE as msg` with retries,
  dead letters and keys, `send`, and the `window`, `sliding` and
  `session` steps, on a local broker.
- **Flags (section 22) and privacy (section 23):** `flags:` with
  percentages and audiences, `nexo flags set`; `@personal`, `@private`
  (encrypted at rest), `keep`, `purpose`, `privacy.export`, `erase` and
  `restrict`, and `nexo privacy report`.
- **The program `policy` (section 9, D142):** `allow` and `deny` of
  `read`, `write`, `net`, `env`, `shell`, `c` and `python`. A release
  build stops at anything the policy does not allow, and does not build
  without one; development mode allows it with a warning that gives the
  rule to copy, except shell commands. Paths are compared after `..` and
  symbolic links, every redirect is checked, and modules follow the
  program's policy. `nexo policy show` and `nexo policy suggest`.

```nexo
policy:
    allow shell

infra shop_k8s:
    cloud = "k8s"
    app api:
        image = "shop:1.0"
        env:
            DB = shop_gcp.orders.url

pipeline main:
    trigger push to main
    version = auto

    step build:
        image = build_image("shop")

    step deploy:
        approve(by: ["ops@shop.example"])
        apply(stage: "prod", image: image)
```

**Containers.** `build_image("shop")` runs `docker build` (or the tool in
`NEXO_CONTAINER_TOOL`) with the project's own `Dockerfile`, and does not
push: a step pushes with `run("docker push {image}")` to a registry the
clouds can read. [`examples/deploy/Dockerfile`](examples/deploy/Dockerfile)
compiles the app with `nexo build --release` and copies the binary, which
needs only glibc, into `debian:bookworm-slim`; secrets are passed with
`-e` when the container starts.

**Not yet:** `ml`, `ui`, `notify`, `every` and
`lifecycle`, canary and blue/green releases, `nexo chaos`,
MCP tools, PostgreSQL, brokers on the network, `use rust`/`use wasm`,
YAML/XML/Parquet (D89, D113). Using any of them gives a clear "not
available in this phase" error; nothing is silently ignored.

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

`compiler/bench/languages.py` runs the same benchmarks in C, Rust, Go,
Java, JavaScript and Python as well, checks that every one prints the
same result, and writes `languages.json`; `compiler/bench/site.py` puts
the results on the benchmark page (`docs/benchmarks.html`).

**Interpreter and compiler differences.** The two implementations print
the same output for every program. The compiler catches some mistakes
earlier (unknown names and certain type errors are reported before the
program runs; `nexo lint`, `check` and the language server report them
too). Its
REPL drops an input that fails when it runs, while phase 0 keeps what
that input assigned before the error (D88). Each difference is recorded
in [`docs/decisions.md`](docs/decisions.md) (D67 to D75, D88).

## Repository layout

```
nexo/                 phase 0: Python interpreter
  lexer.py parser.py  tokens and recursive-descent parser (section 24)
  checker.py          static pre-pass for compile errors
  modules.py          use shop.payments: the program's own modules
  interpreter.py      tree-walking evaluator
  numbers.py types.py arithmetic, money, units and runtime values
  errors.py           error renderer and es/en messages
  stdlib/             builtins, io, JSON/CSV, time, text, crypto, http,
                      db, web, ai, clib (use c), pylib (use python); phase 5:
                      cfg, flags, privacy, stream, infra (+ infra_cloud and
                      infra_providers.json), ci (+ ci_export)
  fmt.py lint.py      nexo fmt, nexo lint and nexo fix
  lsp.py              nexo lsp, the language server
  cli.py              the `nexo` command
compiler/             phase 1: Rust compiler
  nexoc/              driver, lexer, parser, modules, resolver, type checker, LLVM codegen,
                      fmt, lint, lsp (with json), the REPL, cheader (use c),
                      ai_cmd (nexo ai setup|usage)
  runtime/            static library linked into every program (repl.rs: the REPL journal;
                      library.rs and its modules: the phase 4 and 5 library)
  diag/               es/en message catalog
  scripts/            conformance and differential test runners
  bench/              benchmarks
tests/
  conformance/        .nexo programs with their exact .expected output
  fmt/ lint/          formatter and linter cases (.formatted, .expected, .fixed)
  unit/               pytest unit tests per layer
editors/vscode/       the VS Code extension
examples/             complete programs with expected output
  shop_api/           the exit criterion of phase 4: a web API with a database and ask
  deploy/             the exit criterion of phase 5: one file deployed to four clouds
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
3. **Every message has its fix.** New error messages go in
   `nexo/errors.py` with the same text in `compiler/diag/src/catalog.rs`
   (messages are in English, D125), and follow the section 14 format:
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
python3 compiler/bench/languages.py                  # benchmarks: Nexo vs C, Rust, Go, Java, JS, Python
```

To add a conformance test, put `NAME.nexo` and its exact output
`NAME.expected` in `tests/conformance/`. Extra CLI arguments, such as
`test`, go in `NAME.args`.
