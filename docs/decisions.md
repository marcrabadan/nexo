# Decisions

Ambiguities and contradictions found in `docs/especificacion-nexo.md`
while building the phase 0 interpreter, and the option chosen. The guiding
rule is section 1: one way to do each thing, the simplest consistent option.

## Process

**D1. Language of the specification file.** The prompt names
`docs/especificacion-nexo.md` (in Spanish), but the only specification
supplied was in English. It was committed unchanged under that name and
used as the source of truth.

## Lexical rules (section 24)

**D2. Which units exist.** Section 24 says "a number followed by a unit or
currency forms a unit literal" but gives no list of units. Phase 0 recognises
only the units used somewhere in the specification: time `ms s m h day days
year years`, sizes `B KB MB GB TB` (decimal: 1 KB = 1000 B), distance `km`,
percent `%`, and ISO 4217 currency codes (EUR, USD, GBP, JPY and a few more).
`m` means minutes, as in `1m` and `10m` (sections 10, 12 and 19); metres are
not a unit in phase 0.

**D3. When a space is allowed between the number and the unit.** The spec
writes both `30s` and `5 km`. Single-letter units and `%` must be attached
(`30s`, `5m`, `10%`); longer units and currencies may also follow after a
space (`5 km`, `2 days`, `19.99 EUR`). Otherwise `take 5 m` or `x = 5 s`
would silently become unit literals.

**D4. `%`.** Section 24: "`%` right after a number is a unit (percentage);
between two expressions it is the remainder operator". So `10%` is a
percentage and `10 % 3` (with a space) is the remainder.

**D5. An unknown attached unit is an error.** `3xyz` is rejected with
"not a known unit" rather than read as `3` followed by a name.

**D6. Escapes in text.** The spec defines `{expr}` and `{{` but no escapes.
Without them a text cannot contain a `"` on a single line, so phase 0
accepts `\n`, `\t`, `\r`, `\\` and `\"`. `}` needs no escape; only `{{` is
special, as the spec says.

**D7. Multi-line text.** The spec shows `"""` text whose lines are indented
inside the source. A newline right after the opening `"""` and a
whitespace-only last line are dropped, and the common indentation is
removed, so the example in section 2 is `"Several lines\nwith Ana"`.

**D8. Tabs.** Section 24 says "a tab is an error" in the indentation rule.
Phase 0 rejects tabs anywhere outside text and comments, since there is no
other use for them.

**D9. Indentation steps.** "Indentation is exactly 4 spaces": every new
level must be exactly 4 spaces deeper than the enclosing one; 2 or 8 spaces
are errors, and a dedent must land on an open level.

## Grammar (section 24)

**D10. `assert` is missing from the grammar.** `assert` is a reserved word
and section 14 uses `assert expr` and `assert expr fails`, but the EBNF has
no rule for it. Phase 0 adds `assert_stmt = "assert" expr [ "fails" ]` as a
`simple_stmt`; `fails` is contextual, like `within` and `desc`.

**D11. Chained comparisons.** `comparison = coalesce { op coalesce }`
allows `1 < x <= 10`. It means `1 < x and x <= 10`, as in mathematics
(rule 3: understandable by someone with no experience).

**D12. No trailing commas.** The EBNF for lists, maps, arguments and
parameters has no trailing comma, so `[1, 2,]` is a syntax error.

**D13. Steps of a pipeline.** `pipe_step = IDENT { expr | IDENT } | call`
is ambiguous. Each step word has a fixed shape: `where expr`, `map expr`,
`pick col, col`, `group by expr`, `sum [expr]`, `count`, `avg [expr]`,
`sort by expr [desc]`, `take expr`, `each expr`. Any other step is a
function (a name, attribute or call) that receives the value on the left
as its first argument. A step word directly followed by `(` or `.` is an
ordinary call.

**D14. `sum amount` without `group by`.** Section 6 removed "`sum amount`
without a preceding grouping word". This is checked when parsing:
`sum col` and `avg col` must follow `group by` in the same pipeline, and
only `sum`, `count` or `avg` may follow `group by`. Without a grouping,
`sum`, `avg` and `count` take no column and aggregate the elements
themselves (`numbers |> sum`, `amounts |> avg`, `rows |> count`). The
error suggests `map amount |> sum` for the total of a column.

**D15. Module blocks.** `config_block` covers `ai`, `infra`, `pipeline`,
`policy`... which are all outside phase 0. They are parsed (so the file is
still valid) and reported as "not available in phase 0" when run.

**D16. Annotations.** Annotations (`@cache`, `@table`, `@tool`...) all
belong to modules outside phase 0 and are reported as unavailable, instead
of being silently ignored, which would change behaviour. Inline field
annotations (`@private`, `@unique`) are parsed and ignored, since they do
not change how core code runs.

## Errors (section 3)

**D17. Ignoring an error at the top level.** Rule 2 says ignoring a possible
error is a compile error, but section 6 calls `read(...)` at the top level
with neither `?` nor `try`, and rule 6 describes "a program that ends with
an unhandled error". Resolution: inside a function (or lambda), a call
that can fail must be followed by `?` or be inside a `try`; otherwise it is
a compile error. At the top level of the program and inside `test` blocks
there is no caller, so an unhandled error ends the program (or the test)
in plain language with exit code 1. `?` at the top level is an error
(rule 3).

**D18. Which calls can fail.** The checker runs before the program and
knows: user functions (a function can fail when its return type ends in
`!` or its body has `fail(...)` or `?` outside a `try`), methods (by name,
across all types), and the standard library calls `read`, `write`,
`read_lines`, `save`, `validate`, `json.parse` and `csv.parse`. A call
through a variable or a lambda is not known statically and is not
checked.

**D19. Errors at runtime.** Error values travel to the nearest `try`; they
are never stored as `T!` values, because the checker forbids every way to
hold one. `?` therefore does nothing at runtime: it only marks, for the
checker, that the error is passed on.

**D20. What `try` catches.** `try` handles every error value: those made
with `fail()` and the system ones (`NotFound`, `IOError`, `InvalidData`,
`Overflow`, `DivisionByZero`, `OutOfRange`, `ContractBroken`, `Timeout`,
`InvalidValue`). Mistakes that a compiler would reject (unknown names,
wrong types, changing an immutable value, mixing float and decimal...) are
diagnostics: they stop the program and cannot be caught, like compile
errors.

**D21. Error values.** An error has `message`, `cause`, `line` (rule 5) and
`kind`. `fail("...")` makes an error of kind `Error`; `else e is Error`
matches any error; `fail(e)` passes an existing error on. Messages of
system errors are written in the language selected for the run.

**D22. Division by zero.** The spec does not say. `/`, `//` and `%` by zero
raise a `DivisionByZero` error for int, float and decimal alike, instead of
producing `inf` for floats, so the rule is the same for all numbers.

**D23. `??` and `?` are checked statically.** `??` applied directly to a
call that can fail, `?` applied to something that is not a call, and `?`
on a function that cannot fail are compile errors, following "missing
values and errors never mix".

**D24. Where unhandled errors point.** The snippet shows the line where the
error was created (the `fail(...)` call or the failing `read(...)`), since
rule 5 says errors carry their line.

## Variables and scopes

**D25. Scopes.** The spec does not define them. Functions and each
iteration of a loop (`for`, `while`, `repeat`, comprehensions, `parallel
for`) have their own scope. `if`, `try`, `match` and `parallel:` blocks do
not: a name first assigned inside them is visible afterwards, which the
section 3 example (`data` assigned in `try` and `else`) and the section 5
example (`sales` assigned inside `parallel:`) need. Only pattern variables
of a `match` arm and the `e` of `else e` are local to their block.

**D26. Assignment.** `x = value` changes a `var` wherever it was declared,
including from inside a function. Assigning to an immutable name declared
in the same function (or at the top level, from the top level) is an
error. Assigning inside a function to an immutable name of an outer scope
creates a new local name instead. Builtin names (`list`, `print`...) can be
shadowed: section 2 uses `list` as a variable.

**D27. What immutable means.** `x = 1` makes the name immutable, not the
value: the `transfer` example in "Goals and guarantees" changes
`source.balance` through an immutable parameter. Records and collections
are shared by reference in phase 0; the memory model of section 7
(copy-on-write) belongs to phase 2.

**D28. Conditions must be bool.** `if 1:` is an error. There is no
"truthiness" (rule 3: understandable without experience).

**D29. Functions are not hoisted.** A function exists from the line where it
is defined. Calls resolve names when they run, so functions can still call
each other recursively.

## Types

**D30. Printing.** The spec only shows a few printed values. Texts print
without quotes, but inside collections they are quoted; lists `[1, 2]`, maps
`{"a": 1}`, sets `set("a", "b")` (the way they are built), ranges `1..10`,
records `User(name: "Ana", age: 30, email: none)` (the way they are
built), variants `Click(x: 1, y: 2)` and `Quit`, `true`, `false`, `none`.
Floats use the shortest exact form (`0.30000000000000004`, `4.0`).

**D31. Records and variants are built with named or positional
arguments**, like functions. An optional field (`email: str?`) with no
default is `none`. Variants are bare names (`Click(...)`, `Quit`), as in
section 2; there is no `Event.Click`.

**D32. Patterns.** A bare name in a pattern matches a variant (or a type) of
that name if one exists, and otherwise binds a new name. Arms are tried in
order; if no arm matches, it is an error at runtime (phase 0 has no
exhaustiveness check).

**D33. Types are checked at runtime.** Phase 0 has no static type checker:
declared types of variables, parameters, fields and return values are
checked when a value is stored or returned. An int is accepted where a
float or a decimal is declared (it converts exactly).

**D34. Equality.** Values of different types are not equal (`1 == true` is
false), except int and float, which compare by value. Comparing a float
with a decimal is an error (section 17, rule 2).

**D35. Map keys and set elements** must be texts, numbers, bools, money,
quantities, `none` or variants without data, since records and
collections are mutable.

**D36. Lambdas have one parameter** (`lambda = IDENT "->" expr`), and there
is no `break` or `continue`: none of these appear in the specification.

## Numbers (section 17)

**D37. Money precision.** An amount of money always has the currency's
minor units (2 decimals; 0 for JPY and CLP). Every result is rounded half
to even (rule 5), so `120.00 EUR * 21%` is `25.20 EUR`. Money can be
multiplied or divided by int, decimal or a percentage; by a float it is an
error (rule 2). Money divided by money in the same currency is a decimal.

**D38. Percentages.** `21%` is a quantity that multiplies as 0.21: exactly
with int, decimal and money (`200 * 10%` is the decimal `20`), and as a
float with a float.

**D39. Units.** Quantities of the same dimension can be added, subtracted
and compared; the result uses the smaller unit (`30s + 1m` is `90s`).
Mixing dimensions is an error.

**D40. Rounding.** `round(x)` keeps the type (`round(2.5)` is `2.0`). On a
float it works on the binary value, so `round(2.675, 2)` is `2.67`; use
`decimal` for exact rounding. `mode: half_up` uses the builtin names
`half_up` and `half_even`.

**D41. Conversions.** `decimal(x)`, `float(x)`, `int(x)` (truncates toward
zero) and `str(x)` convert between values. They do not parse text: section
9 says external text goes through `validate(x, as: int)?`, which phase 0
implements for int, float, decimal, bool, str and record types, with an
optional `range:`.

**D42. Names the examples assume.** Section 2 uses `PI` without defining
it, so it is a builtin constant. Currency codes (`USD`, `EUR`...) are
builtin names so that `convert(x, to: USD, rate: ...)` works. `close()`,
`log.error`, `Config` and `http` are not standard library in the spec; the
conformance tests define the first three and leave out `http`.

## Data and pipelines (section 6)

**D43. `read`.** Section 3 reads a JSON file as text and then parses it,
while section 6 sends a data file straight into a pipeline. `read` returns
a table for `.csv` files and text for anything else. The `sales.parquet`
example uses `sales.csv` instead, because Parquet is outside phase 0.

**D44. Tables.** A table is a list of rows; a row is a map from column name
to value, read with `row["amount"]` (one way: no `row.amount`). Inside a
pipeline step, a bare name is first a column of the current row (or a
field of the current record), then a variable; `it` is the current
element.

**D45. CSV values.** A column whose non-empty cells are all integers is
int; if they are all numbers, float; otherwise text. Empty cells are
`none`.

**D46. `save`.** `save(path)` writes CSV for `.csv`, JSON for `.json`, and
text otherwise (one line per element of a list). In JSON, decimals keep
all their digits and money is written as text (`"19.99 EUR"`).

**D47. `json.parse(text, as: T)`** builds records recursively, applies field
defaults, and reports a missing field or a wrong type as an `InvalidData`
error.

**D48. Grouping.** After `group by expr`, the key column is named after the
expression (`region`), and the aggregate column after its argument
(`sum amount` gives `amount`, `count` gives `count`).

**D49. Empty and plain aggregates.** `sum` of nothing is `0`; `avg` of
nothing is `none`; `avg` of integers is a float, like `/`.

**D50. Laziness.** Steps run only when the result is stored, passed to a
function, iterated, printed, or reaches a final step (`each`, `save`,
`count`, `sum`, `avg`). A pipeline statement with no final step does
nothing (rule 4). `read_lines` reads the file line by line, so `take 100`
stops reading early.

**D51. Functions in steps.** In `map` and `each`, a step that evaluates to a
function is called with the element (`each print`, `map double`).

## Concurrency (section 5)

**D52. `parallel` runs sequentially in phase 0**, as the prompt allows,
keeping the same observable semantics: `parallel for` returns the results
in input order, and the first failing task stops the remaining ones and
its error propagates. `max:` is validated (an int of at least 1) but has
no effect. Inside a `parallel for` body, `return` gives the value of that
task.

## Contracts and tests

**D53. Contracts.** `expect` is checked where it is written (at the start
of the function, by convention). `ensure` is checked every time the
function returns. Each `old(expr)` is evaluated when the function is
entered, after its parameters are bound, and deep-copied. A broken
contract is a `ContractBroken` error whose message lists the names, fields
and `old()` values involved. The spec gives no name for the return value
inside `ensure`, so phase 0 has none.

**D54. `nexo test`.** It runs the file's top level first (so definitions and
constants exist), then each `test` block in its own scope, and prints ✓ or
✗ per test plus a summary; any failure gives exit code 1. With a directory
(or nothing, meaning `.`) it runs every `.nexo` file below it. `nexo
file.nexo` skips `test` blocks. `fake` (section 14) is not included: it
only exists to replace `ask` and external services, which are outside
phase 0.

## Tooling (section 14)

**D55. Language of messages.** Section 16 says messages use the system
language; the prompt fixes Spanish by default and English with `--lang
en`, and the prompt wins.

**D56. Format details.** The snippet is the source line without its leading
indentation; the carets cover the whole expression involved (cut at the
end of the line); the "what" and "how to fix it" sentences go on one line,
as in the section 14 example. Unhandled errors read "Unhandled error
(Kind): message. How to fix it."

**D57. REPL.** `nexo` with no arguments reads one statement per line; a line
ending in `:` continues until an empty line. A non-`none` expression value
is printed as it would appear in a collection.

**D58. `try within`.** Phase 0 checks the time limit between statements of
the `try` body and raises a `Timeout` error once it has passed; it cannot
interrupt a single long call.

## Standard library (section 15)

**D59. Minimal methods.** The spec only shows `prices.sum()` and
`orders.count()`. Phase 0 adds the smallest set needed to write real
programs: lists `sum count add`, sets `add count`, maps `keys values
count`, text `upper lower trim split replace starts_with ends_with`.
Collections are also changed with `m[key] = value` and `xs[i] = value`
(the `target` rule allows it).

**D60. `range` and `..`.** `1..10` is inclusive (section 2). The prompt asks
for `range` as well; it counts like most languages: `range(3)` is
`[0, 1, 2]`, `range(a, b, step)` stops before `b`.

**D61. `set(...)`** takes its elements as arguments, as in section 2
(`set("new", "sale")`).

**D62. `input(prompt)`** shows the prompt, returns the line without its
newline, and returns `""` at the end of the input.

**D63. `print`** separates its arguments with a space and accepts `end:`
(section 8 uses `print(chunk, end: "")`).

**D64. Indexes start at 0.** A list or text index outside the bounds
(negative ones included) is an `OutOfRange` error; a missing map key gives
`none`, so `rates[country] ?? world_rate` (section 10) works.

## Outside phase 0

**D65. Unavailable features fail loudly.** `use`, module blocks (`ai:`,
`infra`...), annotations, `key = value` settings inside a type, `when`,
`every` and `lifecycle` are parsed and then reported as "not available in
phase 0" instead of being silently ignored.

**D66. `on` and `emit`.** They are core reserved words with a clear
meaning (section 4), so phase 0 implements them: `emit` calls each `on`
handler at once, in the order they were declared.

## Phase 1: the compiler (section 25)

**D67. Unknown names are compile errors.** A name that no scope declares and
that is not a builtin is reported before the program runs, with the same
"Did you mean...?" message. Phase 0 found it only when the line ran. A name
that exists but is not assigned yet when it is read (a function called
before its `fn` line, D29) is still an error at run time.

**D68. Static type checking is gradual.** Types are optional and inferred
(section 2), so the compiler infers a type only when it is certain:
literals, declared types, functions defined once, records, variants and
operators on known types. A mistake that would certainly happen when the
line runs (mixing float and decimal, money in two currencies, a wrong
argument, a missing field, a condition that is not bool, a literal of the
wrong type...) is reported before the program runs with the runtime's
message. When the wrong value is not a literal its text is unknown, so the
messages `type_mismatch_static` and `return_mismatch_static` say "a value
of type T" instead. Everything else is checked when the value is stored
(D33).

**D69. How Nexo is compiled.** The compiler (Rust, `compiler/nexoc`) emits
LLVM IR as text and clang compiles it and links it with the runtime
library (`compiler/runtime`, a Rust static library). Values are two words
(tag and bits); ints, comparisons and direct calls to functions defined
once are native code, everything else calls the runtime. Errors travel as
in phase 0: after each call that can fail the code checks a flag and jumps
to the nearest `try`. `nexo file.nexo` builds with `-O1` and runs;
`nexo build --release` uses `-O2`.

**D70. Memory in phase 1.** Heap values are never freed. The memory model
of section 7 (ownership, copy-on-write, reference counting) is phase 2
(D76).

**D71. Module features are compile errors.** `use`, module blocks,
annotations, type settings, `when`, `every` and `lifecycle` (D65) cannot be
compiled, so the compiler rejects them instead of failing when the line
runs. The message now says "phase 1".

**D72. No REPL in the compiler.** The REPL is part of phase 3 (tooling); in
phase 1 `nexo` with no arguments shows the usage. The phase 0 interpreter
keeps its REPL.

**D73. Nested calls.** A compiled program allows 10,000 nested calls (the
limit phase 0 set for Python) and runs on a 1 GB stack; beyond that it
ends with "too many nested calls", as phase 0 did. Phase 0 actually failed
much earlier (about 1,000 Nexo calls) because of Python's own frames.

**D74. Checked when compiling.** An unnamed argument after a named one,
an unknown type name, a trait that does not exist or a missing trait
method, and `else e is X` with an unknown X are compile errors (phase 0
found them when the line ran).

**D75. Language of messages.** `--lang` is fixed when the program is
compiled: a native program reports its errors in the language it was
compiled with.

## Phase 2: memory and concurrency (section 25)

**D76. Reference counting everywhere.** Section 7 allows reference counting
"wherever the compiler cannot prove a single owner". Phase 2 counts the
references of every heap value (texts, collections, records, functions,
captured variables) and frees a value when its last owner lets it go; the
count is atomic so values can be read by several tasks at once (D80). Each
function owns the values of its variables and releases them when it ends,
also when it ends with an error. Proving single owners (rule 3) is an
optimisation left for later; it does not change what a program does.
Constants of the program (literal texts and units, types, variants) are
never freed. `NEXO_LEAKCHECK=1` makes a compiled program release
everything at the end and report the values that were not freed; the
conformance suite, the examples and the differential programs run with it
and must leave nothing ("no leaks in the test suite", section 25).

**D77. A function that calls itself does not capture itself.** A nested
function that calls itself by name would hold a reference to the cell
that holds it: a cycle that is never freed. A call of a function defined
once to its own name uses the function itself instead. Two nested
functions that call each other still form a cycle and are freed only when
the program ends.
