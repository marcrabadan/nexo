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
`source.balance` through an immutable parameter. Records are shared by
reference; collections are values from phase 2 on (D78).

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

**D52. `parallel` runs sequentially in phase 0** (phase 1 runs it on
threads from phase 2 on, D80), as the prompt allows, keeping the same
observable semantics: `parallel for` returns the results
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

**D65. Unavailable features fail loudly.** `use` (except `use nexo.sync`
from phase 2 on, D82), module blocks (`ai:`,
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
keeps its REPL. (Phase 3 adds it to the compiler, D88.)

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

**D78. Collections are values; records are shared.** Section 7 says
collections are "borrowed by default" and copied when "modified after
being shared", and that small `type` values "are copied". Copying is only
invisible for values nobody changes, and the `transfer` example (Goals and
guarantees) changes the records it receives, and rule 4 speaks of
reference cycles between `type` values. So: lists, maps and sets behave as
values. `b = a`, passing `a` to a function, storing it in a field or a
list, all give a value that changing `b` does not affect; the copy is made
only when one of them is changed while the other still holds it. Records
are shared: `q = p; q.x = 2` changes `p` (D27); a list of records copied
on write still holds the same records. Texts never change. A collection
is changed through a place: a variable, a field, an element
(`xs.add(x)`, `m[k] = v`, `order.items.add(x)`, `grid[0][1] = 5`); the
change goes to that place only. A loop over a collection sees the value it
started with, even if the body changes the variable. `xs.add(xs)` adds the
list as it was. Changing something that is not a place (`f().add(1)`)
changes a copy nobody sees. Phase 0 keeps the same rules by counting the
places that hold a collection (one, or more than one).

**D79. `weak` fields and reference cycles.** Rule 4 makes "a reference
cycle between `type` values" a compile error unless a field is `weak`,
but its own example (`Node` with `children: list[Node]`) refers to itself
and must compile. The compiler cannot know which references point "back",
so the rule is about types: a type that refers to itself through its
fields (a chain, a tree) is allowed; two or more types that refer to each
other through fields that are not `weak` are an error, reported at the
field that points back to the type defined first, with the fix
`field: weak T?`. `weak` is only allowed at the top of a field's type
(`parent: weak Node?`), the target must be a record type, and the field
must be optional: once the target is freed the field reads `none`. A
weak field does not keep its target alive; it is printed as `Node(...)`
(only its type, so a parent-child pair does not print forever) and saved
to JSON and CSV as none. Copies of records (`old()`) keep the weak
reference. A cycle made at run time through a type that refers to itself
(`a.next = a`) is not detected and is freed only when the program ends.

**D80. `parallel` on threads.** In phase 1 the tasks of `parallel for` (one
per element) and of `parallel:` (one per statement) run on up to one
thread per processor, or `max:`. A program sees the same as running them
one after the other in order (D52): results keep the input order; what a
task prints is written when the block ends, in task order; the error of
the first failing task (in order) propagates and the output and results of
later tasks are dropped; tasks not started yet when one fails are not
run. A task that is already running when an earlier one fails finishes
(it is not interrupted), so only its side effects outside the program
(writing files) can differ from phase 0. A `try within` limit applies
inside the tasks too.

**D81. What tasks can share.** Section 5 promises "no data races": the
compiler rejects mutable data shared between tasks. Tasks read anything,
and change only what is theirs:
- A `parallel for` task cannot change (assign, or change a collection
  through) a variable of outside the task; the compiler reports it when
  the task's own code does it, and the program stops with the same message
  when a function called by the task does it.
- A task cannot change a record created before it started (it may be
  read by the other tasks); records it creates are its own, and they can
  be changed freely once the block ends.
- Each statement of `parallel:` is a task that computes the names it
  assigns (they are visible after the block, D25). Another statement of the
  same block cannot use or assign those names (compile errors), and
  `return` is not allowed inside the block.
Phase 0 applies the same rules (its tasks run one after the other), so
both give the same messages; a few are found by phase 0 only when the line
runs.

**D82. `nexo.sync`.** Section 5 shows `spawn`, `channel`, `select` and
`shared` without defining them; the smallest consistent set is:
- `use nexo.sync` makes them available: `spawn`, `select` and `shared`
  start statements and `channel` is a name. Without it they are ordinary
  names (they are contextual words, section 27).
- `channel(size: n)` or `channel[T](size: n)` makes a channel; `size` is
  how many values can wait without a receiver, 0 by default (each send
  waits until a task takes the value). `c.send(x)` (x must be a T),
  `c.receive()` (none once the channel is closed and empty), `c.close()`,
  and `for x in c:` (until it is closed and empty). Sending on a closed
  channel is an error.
- `spawn f(args)` runs the call in a new task. A spawned task works on
  copies: its arguments and every value that travels through a channel
  are copied, records included, so two tasks never share a record. It
  cannot read variables of the program that others may change (a `var`,
  or a list, map, set or record), only constants with values nobody can
  change, functions, types and channels. An error nobody handles in a
  spawned task ends the program, as at the top level.
- The program ends when every spawned task has ended. When every task
  waits on a channel (or the lock of `shared`) nobody can go on: the
  program ends with a deadlock error, shown where the main program waits
  (or else where the first spawned task does). Tasks of `parallel` that
  wait on a channel are not counted.
- `select:` has arms `name from channel:` and `after limit:`. It takes the
  first channel (in order) that has a value; `after` runs when nothing
  arrives within the limit, or at once if every channel is closed; without
  `after`, a select whose channels are all closed does nothing.
- `shared name = value` declares a variable every task can read and
  change. Every statement that uses it holds one lock for all shared
  variables (it can be taken again by the same task), values are copied in
  and out (records included), and changes through it (`cache[k] = v`)
  happen under the lock.
A spawned task that reads a top-level constant at the moment the main
program first assigns it is not detected; define constants before
starting tasks.

**D83. Contracts the compiler proves.** "A contract the compiler can
prove costs nothing at runtime". Phase 2 proves the common case of the
examples: a function whose body is straight-line integer arithmetic on its
parameters and on the int fields of its record parameters (assignments
and `+=`, `-=`, `*=` by a constant; a final `return`). It runs the body
with symbolic values and proves an `ensure` that holds for every value on
entry; since two record parameters of the same type may be the same
record, it must hold in every case (the `transfer` example with ints is
proven; `a.x += 1; b.x += 1` with `ensure a.x == old(a.x) + 1` is not).
An `expect` is proven only when its condition is a constant. Proven
contracts, and the `old()` copies only they use, are not compiled; the
others are checked when the function runs (D53). Floats are never proven
(their arithmetic is not exact). `nexo build --release --trust-ensure`
also leaves out the unproven `ensure` checks and their `old()` copies;
every `expect` stays. A proven `ensure` whose own evaluation would
overflow an int is not evaluated, so that Overflow is not raised.
A function whose `expect` or `ensure` may change something (it calls a
function other than `old()`, or uses a pipeline or a lambda) has none of
its contracts proven: the change would make the symbolic run wrong. As
with any statement, a contract that reads a `shared` variable (D82),
and the `old()` value taken for it, holds the lock while it is read.

**D84. Fast paths of the compiled code.** The exit criterion of phase 2
("benchmarks under 2× Rust") asks for code close to what the types allow;
none of these changes what a program prints.
- Values that are certainly ints (int literals, arithmetic on them,
  `len`, variables assigned only ints, parameters declared `int`, calls to
  a function defined once that returns `int`) skip the type checks of
  arithmetic and comparisons.
- A function defined once whose parameters (none optional or with a
  default) and result are all `int` gets a second entry that takes the
  numbers in registers; a direct call with certainly-int arguments uses it,
  and the usual entry (calls through a value, other arguments) checks the
  arguments and then calls it. It keeps its call depth (D73) in a
  register and writes it to the thread state only before calling other
  code; its caller puts its own depth back. The limit is the same.
- The time limit of `try within` (D58) is still checked between
  statements, but first through a count of the threads that have one, so
  code with no limit anywhere only reads that count.
- A call to a function defined once, from its own body (or a function
  inside it), does not check that the function is defined (D29): it is.
- `P(a, b)` with a record type defined once and one positional argument
  per field checks the fields in the compiled code and builds the record
  in one call; field accesses and method calls remember, at each place in
  the code, where the last record type they saw keeps that member.
- Map keys use a fast multiplicative hash instead of SipHash: maps keep
  their insertion order, so the hash is never visible.
- The Rust version of `bench/records` keeps its `Box` with
  `std::hint::black_box`: rustc removes the allocation otherwise, and a
  Nexo record is always a heap value (section 7), so without it the
  comparison would measure nothing.

## Phase 3: tooling (section 25)

**D85. `nexo fmt`.** Section 14 says the formatter "has no options, so all
Nexo code looks the same", without the rules. The simplest formatter that
cannot change a program works on the lexer's tokens: the formatted text
must have exactly the same tokens as the original (it is checked before
anything is written), and the line breaks are the author's. The rules:
one space around binary operators, `=`, `->`, `|>` and keywords; one
after `,` and `:`; none inside brackets, before the `(` or `[` of a call
or an index, around `.` and `..`, after a unary `-`, before a postfix `?`
or `!`. After `|>`, a step word keeps its space before `(` or `[` (`|>
map [..]` is not a call, `|> save(...)` is, D13). Indentation of
statements is already fixed by the lexer (D9); a continuation line that
starts with `|>` goes two spaces deeper than its statement, as in every
example of section 6, and a line inside brackets four spaces deeper than
the line that opened the bracket (a line that starts by closing it, at
that line's indentation). A comment on its own line takes the
indentation of the code it comes before (or of the block it closes); the
trailing comments of consecutive lines are aligned two spaces after the
longest of them. Blank lines: at most one in a row, none at the start of
the file or right after a line that opens a block; no trailing spaces;
one newline at the end. `nexo fmt [paths]` rewrites the files in place
and prints the ones it changed; a file that does not lex is reported and
left alone. Two parts of section 14 and 16 wait for their modules: the
"implicit `use` lines" belong to `nexo.ai`, `nexo.web`, `nexo.infra` and
`nexo.ci`, none of which exists yet, so there is nothing to add; and
`--view es` needs the official translation of every keyword (section 16,
rule 1), which the specification does not give beyond three examples.
Both phases format alike (`tests/test_fmt.py`).

**D86. `nexo lint` and `nexo fix`.** Section 14 says `lint` "warns about
questionable code with a proposed fix" and `fix` "applies the linter's
safe fixes", without a list of warnings. Lint first runs the checks a
compilation makes before the program starts (phase 0: lexer, parser and
checker; phase 1 also names and types, D67, D68): a file with an error is
reported like `nexo check` would, with its fix. Otherwise each warning is
written like an error (section 14 format, "Warning in ..."), with what is
questionable and how to fix it, and the exit code is 1 when there are
warnings. The warnings are the ones that follow from the rules of the
specification:
- a name assigned and never read (`unused`), and a name a pattern binds
  and never uses (`unused_binding`; fix: `_`);
- `var` on a name that is never changed, neither assigned nor changed
  through a place or a method (`var_never_changed`; fix: drop `var`,
  only offered when no outer name has the same spelling, since `x = v`
  could then change it, D26);
- `for i in 1..n` or `range(n)` whose variable the body never reads
  (`loop_var`; fix: `repeat n`, "one way to do each thing");
- comparing with `true` or `false` (`compare_bool`), `not not x`
  (`double_not`), `x = x` (`self_assign`);
- code after `return` or `fail()` in the same block (`unreachable`);
- `if`, `elif` or `while` on the literal `true`/`false` (`while true` is
  allowed: there is no `break`, D36);
- `use nexo.sync` when nothing of it is used (`unused_use`);
- an expression statement that only computes a value (`useless_expr`),
  and a pipeline statement whose last step is lazy, so it never runs
  (`lazy_pipeline`, D50);
- a key written twice in a map literal (`duplicate_key`).
An edit is safe when it does not change what a correct program does:
removing `var`, `?` on a call that cannot fail, unreachable code, an
unused `use` or a self-assignment, writing `_`, `repeat n`, dropping `not
not`, and adding the `!` a function that fails needs, or the `e` of
`else:`. Replacing `x == true` by `x` is not (it differs when `x` is not
a bool), nor a "Did you mean" suggestion or adding `?`: these are
offered by the editor (D87) and left to the person. `nexo fix` applies
the safe edits, checking the file again after each round, and prints the
files it changed. Both phases warn alike on every program the compiler
accepts (`tests/test_lint.py`). The warnings that depend on modules
(expired flags, section 22; `@personal` without `keep`, section 23) wait
for them.

**D87. `nexo lsp` and `nexo check`.** `nexo lsp` is a language server
(section 14) over standard input and output, for any editor. It
publishes the compile error of each open file, or its warnings (D86),
with their edits; offers them as quick fixes and "apply the safe fixes"
(`nexo fix`); formats with `nexo fmt`; and gives go to definition, hover
(the definition line and its `##` comments), the outline of the file and
completion (names of the file, builtins and keywords; fields and methods
after `.`). Messages use `--lang` or, without it, the language of the
editor when it is Spanish or English (section 16), Spanish otherwise
(D55). Definitions are found by scope the way D25 describes them,
without types: `p.norm` goes to the first field or method of that name.
`nexo check [paths]` runs the same checks without running anything
(section "Goals and guarantees" names it; `--intent` needs `nexo.ai`).
The VS Code extension (`editors/vscode`) starts `nexo lsp` of either
phase; publishing it needs the publisher's account and token, so the
repository only builds, tests and packages it
(`.github/workflows/vscode-extension.yml` publishes on a `vscode-v*` tag).

**D88. The compiler's REPL.** A compiled program cannot take new code
while it runs, so `nexo` with no arguments keeps the session as a
program: each input is added to it and the whole program is compiled and
run again. The inputs before the new one replay without being seen: until
the first statement of the new input nothing is written to the output,
and `input()`, reading a file and writing one give the answers they gave
the first time, from a journal, instead of happening again. The new input
then runs for real; the value of each of its top-level expressions is
shown as in phase 0 (D57). An input with a compile error is reported
(counting its own lines, as phase 0 does) and not added. One that fails
when it runs is reported and left out of the session, together with what
it did: phase 0 keeps the names its statements assigned before the
error. Standard input is read one byte at a time, so a program that calls
`input()` reads the lines that follow it. Concurrent tasks that read or
write files in a different order on each run, and timing (`select` with
`after`), may replay differently; the REPL is for trying code, and a
program file has none of these limits.

## Phase 4: library and modules (section 25)

**D89. What phase 4 covers.** Section 25 lists "standard library,
`nexo.ai`, `nexo.web`, `nexo.db`, C and Python interoperability" with the
exit criterion "a web API with a database and `ask` is built with Nexo
alone". Both implementations get, with the same output: the standard
library of section 15 (`io`, `text`, `data`, `time`, `units`, `http`,
`crypto`, `test` with `fake`), `nexo.ai` (`ask`, `ai` blocks, `@tool`,
`agent`), `nexo.web` (`server` blocks, routes, `jwt`, `@role`, `@limit`),
`nexo.db` (SQLite through `connect`, `@table`, pipelines translated to SQL,
transactions, `db.sql`, `nexo db diff` and `migrate`), `use c` and
`use python`. What the specification ties to later phases stays out and
is reported as unavailable (D65): `infra`, `pipeline`, `policy`, `flags`,
`stream`, `ml`, `ui`, `notify`, `when`, `every`, `lifecycle`, the other
patterns of section 10, and the parts of these modules that need them
(secrets from a vault or a cloud, `budget` in `ai`, `mcp(...)` tools,
PostgreSQL, see the decisions below). `unsupported` messages now say
"phase 4".

**D90. Module blocks and annotations are rewritten into core code.** The
parser turns each module construct into ordinary statements that call
builtins whose names start with `__`, so the checker, the interpreter,
the compiler's passes and the tools see only the core:
- `ai:` and `ai profile "p":` become `__ai_config(none | "p", key: value...)`;
  `ai models:` becomes `__ai_models(alias: "provider:model"...)`;
- `agent a:` becomes `a = __ai_agent("a", key: value...)`;
- `server api:` becomes `api = __web_server("api", key: value...)`, and each
  route a function named after it (`GET /users/{id}`, a name no program can
  write) followed by `__web_route(api, "GET", "/users/{id}", that function,
  [the parameter types], role:, limit:, public:)`;
- `@table type T` becomes the type (its `many` fields removed, its `id`
  field an `int?`) followed by `__db_table(T, "id", {many fields}, {marks})`;
- `@tool "d" fn f` becomes the function followed by `__ai_tool(f, "d", [types])`;
- `use python m as x` and `use c "lib"` become `x = __python_import("m")`
  and `lib = __c_library("lib")` after the `use`;
- `fake f returns v` (only inside `test`) becomes `__fake("f", v)`;
- `db.transaction:` becomes `__txL_C = __db_begin(db)` and a `try` whose
  body ends with `__db_commit` and whose handler calls `__db_rollback` and
  passes the error on with `fail(e)`; `return` inside the block is a
  compile error, since it would leave the transaction open.
The values of a block are evaluated where the block is written, like any
statement (D29): an `ai` block takes effect from its line on.

**D91. Syntax the core grammar lacks.** The module examples use forms the
EBNF of section 24 does not have; they are added in the simplest way that
accepts the examples:
- `use c "libz"`: a text after `c` (`use_stmt` only allows a dotted name).
  The library is used through the name given with `as`, or the text
  itself when it is a valid name.
- `@tool "Looks up an order"`: an annotation may take one text without
  parentheses.
- `per` builds a rate in annotation arguments and block values:
  `10 per minute per ip`, `5 EUR per day`.
- In the fields of a type, `ref T` and `many T` (only with `@table`);
  anywhere a type is written, `1..5` is an int between both bounds
  (`score: 1..5`, section 8).
- `ai profile "work":` has two words before its name.
- In a `server` block, a route is `{annotation} get|post|put|patch|delete
  "path" [(params)] block`; the `{name}` parts of the path are its path
  parameters, not interpolations.
- `words` and `tokens` are units (for `max:` of `ask`), written after a
  space (`200 words`).
- `X.transaction:` followed by a block is a statement.

**D92. Values of the library.** Dates, times, secrets, keys, regular
expressions, HTTP responses, databases, servers and the other values the
library adds are values like any other: `x is date` checks their type,
they are compared, used as map keys (dates and times) and passed around.
They print as they are written in data files (a date as `2026-10-24`, a
time in ISO 8601), without quotes even inside a collection, like money
(D30); values that are a handle to something (a database, a server)
print as their type and name.

**D93. Rates.** `10 per minute per ip` (section 15) and `5 EUR per day`
(section 8) have no rule in the grammar; `per` is read as a contextual
word only in annotation arguments and the values of module blocks, where
it builds a rate: an amount, the unit of time (`second`, `minute`,
`hour`, `day`) and what it is counted per (`ip`, `user`).

**D94. `time`.** Section 15 names `now`, `today`, durations and time
zones. `today` is a `date` and `now` a `time`; section 6 writes
`date: date = today` without parentheses, so both are names whose value
is read from the clock each time they are used (not functions). A `date`
is a day of the calendar (years 1 to 9999); `date(2026, 10, 24)` builds
one and `date(t)` takes the day of a time. A `time` is an instant, with
the zone it is shown in; `time(2026, 10, 24, 9, 30, zone: "Europe/Madrid")`
builds one (hour, minute and second default to 0 and the zone to the
machine's: `TZ`, then `/etc/localtime`, then UTC). Zones are the IANA
names of the system's time zone files, `"UTC"` or a fixed offset
(`"+05:30"`); `t.to_zone(z)` shows the same instant elsewhere (`in` is a
reserved word). A wall clock time that a change of offset skips or
repeats takes the offset in effect before the change. Members: `year`,
`month`, `day`, `weekday` (1 is Monday) and, for times, `hour`, `minute`,
`second`, `date`, `zone`. Arithmetic uses the durations of D39: a date
moves by whole days (`d + 3h` is an error), a time by milliseconds;
`date - date` is a number of `days` and `time - time` of `s`. Times are
kept to the millisecond and print as `2026-10-24T09:30:00+02:00`
(`Z` for UTC, `.250` when there are milliseconds); `validate(x, as: date)`
and `as: time` read that form (a time needs its offset), and JSON and CSV
write it. `1 year` is 365 days (D2).

**D95. `text`.** Section 15 lists "interpolation, search, regular
expressions, templates" without functions. The smallest set:
- `s.find(sub)` is the position of the first `sub` in `s` (counted in
  characters, from 0) or none.
- `regex(pattern)` builds a regular expression; `r.matches(t)` (the whole
  text), `r.find(t)` (the first match or none), `r.find_all(t)`,
  `r.replace(t, with)` (every match, by a literal text), `r.split(t)` and
  `r.groups(t)` (the groups of the first match, none for one that did not
  take part, or none without a match). The syntax is the one both
  implementations can run identically and in linear time: Rust's regex
  crate without what Python lacks (`\p{..}`, `[[:alpha:]]`, `(?U)`) and
  without what it lacks (lookarounds, backreferences, possessive
  repetitions); `$` is the end of the text unless `(?m)` is given. Any
  other pattern is an error when `regex` is called. Braces in a pattern
  are written `{{` as in any text (`"\\d{{4}"`).
- `render(path, name: value...)` reads a template file and replaces each
  `{name}` with the text of the value of that name (`{{` is a brace); in a
  `.html` file the values are escaped (section 9: "in web templates,
  interpolation is escaped automatically"). A placeholder without a value
  is an `InvalidData` error. Like `read`, it can fail.

**D96. `move_file`.** Section 10 calls `move_file(path, to: "trash/")`:
the file moves into the folder `to` (created when missing) keeping its
name, and the call returns its new path. It can fail like `read`
(`NotFound`, `IOError`).

**D97. `crypto`.** Section 9 fixes the algorithms per edition; edition
0.1 uses Argon2id (19 MiB, 2 passes, 1 lane, a 16-byte salt, written as a
PHC string, so a hash made by one implementation is checked by the
other), AES-256-GCM (a random 12-byte nonce, then the ciphertext and its
tag) and Ed25519. Nexo has no bytes type, so keys, ciphertexts and
signatures are base64 text; files are encrypted in place
(`encrypt_file(path, k)`, `decrypt_file(path, k)?`).
- `secret(name)` reads the environment variable `name` when the value is
  used, not when it is created, so a missing one fails where it is needed
  (`NotFound`: 'The secret "DB_PASS" is not set'). The system keychain and
  `from: vault | aws | azure | gcp` belong to `nexo.infra` (phase 5) and
  are reported as unavailable.
- A secret, a key and a key pair cannot be shown: `print`, interpolation,
  `str()`, `write`, `save` and `fail` stop with a compile-style
  diagnostic (the compiler cannot always know a value is a secret, so
  both phases check it when the value is shown). A `key:` argument (or a
  `key =` of a module block) written as a text literal is a compile error
  ("writing a literal key in the code is a compile error").
- `password.hash(p)`, `password.check(p, stored)` (false for anything
  that is not an Argon2id hash) and `password.generate(length: 20)`
  (letters and digits, from the system's random source).
- `key.new()`, `key.load(text)` and `k.export()`; `keypair.new()`,
  `keypair.load(private)`, `pair.public` (text), `pair.private` and its
  `export()`. Exporting is the visible, explicit way to store a key
  (section 1, rule 2). `encrypt(text, k)`, `decrypt(text, k)?` (an
  `InvalidData` error for another key or changed data), `sign(text,
  private)` and `verify(text, signature, public)`.

**D98. `http`.** Section 15 lists "`http.get`, `http.post`, client with
retries"; section 10 calls `http.get(EXCHANGE_API).json().usd`. The client
has `http.get(url)`, `http.delete(url)`, `http.post(url, body)`,
`http.put` and `http.patch`, with `headers:` (a map; a value can be a
secret), `timeout:` (a duration, 30s by default) and `retries:`. A text
body is sent as `text/plain`, anything else as JSON (on one line). The
answer is a `response` with `status`, `text`, `headers` (lowercase names)
and `json()` (or `json(as: T)`), which can fail like `json.parse`. Every
call can fail (`T!`): `NetworkError` when nothing answers (refused,
unknown host, no answer in time) and `HTTPError` for a status of 400 or
more, so `http.get(url)?.json()?` never reads an error page as data. GET,
PUT and DELETE are retried twice by default (POST and PATCH never are,
since repeating them may repeat their effect) after a network error or a
502, 503 or 504, waiting 0.25 s, then 0.5 s...; a GET follows up to 5
redirects; other 3xx answers are returned as they are. The proxy of the
environment (`HTTPS_PROXY`, `NO_PROXY`) is used. `.usd` on the parsed
JSON is not allowed (a map is read with `["usd"]`, D44); `json(as: T)`
gives a record. `validate(x, as: email)` (section 9) accepts a text with
one `@`, something before it and a domain with a dot after it.

**D99. `fake`.** Section 14 writes `fake ask returns "positive"` and
`fake payments_api returns {ok: true}` inside a test. `fake name returns
value` (a dotted name, `fake http.get returns ...`, works too) makes every
call written as `name(...)` return the value until the test ends; the
function and the arguments are still evaluated. It is only a statement
inside `test` blocks; `fake` stays an ordinary name elsewhere.

**D100. `nexo.db`.** Section 6 shows one way to connect and the `@table`
types as the source of truth. Phase 4 has SQLite, which needs no server:
- `use nexo.db` makes `connect` and `@table` available (section 15:
  "`use nexo.db`"; without it they are errors that say so).
  `connect("data.db")` opens (or creates) an SQLite file and can fail
  (`DatabaseError`); `connect("postgres://...")` and other servers belong
  with the cloud resources of phase 5 and are reported as unavailable.
  `key:` is checked to be readable and not used by SQLite.
- A `@table` type is a table named after the type in lowercase. Its
  fields are columns of type `int`, `float`, `bool`, `str`, `decimal`,
  `money`, `date` or `time`, an int range, a union of literals, or an enum
  without data; `ref T` is a column `<field>_id` that holds the id of a
  row of `T` (reading it loads that row); `many T` is not a column but a
  query of the rows of `T` whose `ref` field points to this one. A field
  of another type, or a record without `ref`, is an error. The field of
  type `id` is the automatic primary key (an `int?` that is none until the
  row is saved). `@unique` is a UNIQUE column; `@private` and `@personal`
  belong to privacy (phase 5) and change nothing yet. A record field
  typed with another table type is shared (D78), so `ref` and `many` make
  no reference cycle: `many` fields are left out of the cycle check.
- Tables are created, and the columns of new fields added (empty for
  existing rows), the first time the program uses a type through a
  database. Anything that removes or changes data is done by
  `nexo db migrate` (D101).
- `db.T` is a query of the whole table: `find(field: value, ...)` (a
  `NotFound` error when no row matches, so `?` passes it on; section 15's
  `find(...) ?? 404` is written `find(...)?`, which a route answers with
  404, D104), `add(field: value, ...)` (or `add(record)`; it returns the
  record with its id), `update(record, field: value, ...)`,
  `remove(record_or_id)`, `all()` and `count()`. A repeated `@unique` value
  is a `DatabaseError` that names the field and the value. `ana.orders`
  of a `many` field reads from the database the type was last used with.
- Pipelines on a query are translated to SQL while they can be (section
  6): `where` whose condition compares columns with values (`==`, `!=`,
  `<`, `>`, `<=`, `>=`, `text in column`, a bare bool column, joined with
  `and`, `or`, `not`), `sort by column [desc]` and `take n`, in that
  order, then `count`. A bare name in a step is a column first (D44), so
  `where date >= start` compares the column `date`. A value is computed
  once, outside the rows; columns of `decimal` and `money` are not
  compared in SQL (their text does not order as numbers do), and a value
  of the wrong type runs in memory (where it gives its usual error). From
  the first step that cannot be translated, the rows are read and the
  rest runs in memory with the same result; comparing a missing value is
  false in SQL. Rows come in the order of the primary key unless sorted
  (ties of `sort by` too, as the stable sort in memory keeps them).
  `NEXO_SQL_LOG=1` writes every statement to the standard error output;
  the tests check that both phases send the same SQL.
- `db.transaction:` (D90) runs its block in a transaction: an error rolls
  it back and passes on; inside another one it is a savepoint.
- `db.sql("SELECT ... {minimum}")` sends the text with each `{value}` as a
  parameter, never inside the SQL ("raw SQL, always parameterised"), so
  its argument must be a literal text (the parser keeps its parts apart);
  a text built elsewhere is an error. A table name after FROM, JOIN, INTO,
  UPDATE or TABLE, or before a dot, is quoted, so section 6's
  `FROM order` works although `order` is an SQL word. The result is a
  list of maps (empty for statements that return no rows).
- Times are stored in UTC with milliseconds (they come back in UTC),
  money as `"19.99 EUR"`, decimals as text.

**D101. Migrations.** Section 6 has `nexo db diff` write "a readable
migration file" and `nexo db migrate` apply it in a transaction; the
format is left open for edition 1.0 (section 27). `nexo db diff app.nexo`
runs only the types of the program (nothing else) and compares them with
the database of its first `connect("...")` (or `--db path`): it writes
`migrations/NNNN.sql` next to the program, numbered in order, with the
SQL to create tables and add columns, then, under a comment, what drops
data (removed tables, and removed columns, which SQLite drops by building
the table again). A column whose type changed gets a comment to change
it by hand. `nexo db migrate app.nexo` applies the files not applied yet
(recorded in the table `nexo_migrations`), each in a transaction; a file
that drops data needs `--allow-drop`.
