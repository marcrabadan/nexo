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
system errors are written in English (D125).

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
en`, and the prompt wins. Superseded by D125: messages are in English
only and there is no `--lang`.

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
compiled with. Superseded by D125: there is one language, English.

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
after `.`). Messages are in English whatever the editor's locale
(D125). Definitions are found by scope the way D25 describes them,
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
`hour`, `day`, `month`) and what it is counted per (`ip`, `user`).

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
redirects (a redirect to another origin, scheme, host or port, drops the
`Authorization`, `Cookie` and `Proxy-Authorization` headers, which belong
to the origin they were given to); other 3xx answers are returned as they
are. The proxy of the
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
  it back and passes on; inside another one it is a savepoint. A table
  first created inside a transaction that rolls back is created again
  when it is next used.
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

## nexo.web (section 15)

**D102. Servers and routes.** Section 15 shows one `server` block and
says HTTPS, secure headers and CSRF protection are on and cannot be
turned off; how a server starts is not written. A `server NAME:` block
(D90) holds `port` (default 8080) and `auth` (a `jwt`, optional), then
routes: `get|post|put|patch|delete "/path/{param}"(params):` with a
block. The servers start when the top level of the program has run (and
its spawned tasks have ended), and answer until the program is stopped;
the line `api escucha en http://127.0.0.1:8080` goes to the standard
error output. `PORT` in the environment replaces the port when there is
one server. Under `nexo test` every server listens on a free port before
the tests, and `api.url` (also `api.port`, `api.name`) is its address,
so a test calls it with `http`. `NEXO_WEB_LOCAL=1` listens on 127.0.0.1
only; otherwise on every address.
- A server answers HTTP/1.1 one request at a time and closes each
  connection after its answer (`Connection: close`); heads are limited to
  64 KB and bodies to 1 MB.
- Parameters: a `{name}` of the path, then a parameter whose type is a
  record, list or map, read from the JSON body (`json.parse(as: T)`),
  then the query (`?limit=5`), converted to the parameter's type as
  `validate` does; a parameter named `request` receives the request
  (`method`, `path`, `headers`, `query`, `body`, `json()`, `user`, `ip`).
  A parameter that is missing and has no default answers 400.
- HTTPS: Nexo does not terminate TLS itself in this phase (a certificate
  needs a host name). Every answer carries `Strict-Transport-Security`,
  and a request that a proxy marks `X-Forwarded-Proto: http` is
  redirected with 308 to `https://`. The other secure headers are
  `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`,
  `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY` and `Cache-Control: no-store`.
- CSRF: a POST, PUT, PATCH or DELETE whose `Origin` is not the host it
  was sent to answers 403. Tokens travel in `Authorization`, never in
  cookies, so a page of another site cannot use them.
- A path that no route has answers 404; one that another method has
  answers 405 with `Allow`. A route defined twice, or `@role` on a server
  without `auth`, is an error when the program starts.
- `@limit(10 per minute per ip)` (D93; the units are `second`, `minute`,
  `hour`, `day` and `month`) counts requests per route in windows of the
  clock, per address (`per ip`, the default) or per token subject
  (`per user`); one more answers 429 with `Retry-After`.
- A mistake in a handler (a diagnostic) answers 500 and stops the program
  with its message, as everywhere else (D17); under `nexo test` it fails
  that request and is shown, the tests go on, and `nexo test` ends with
  status 1.

**D103. Authentication.** Section 15 writes `auth = jwt(key: ...)` and
`@role("admin")` and nothing else. `jwt(key:)` takes a secret or a text
and signs tokens with HS256 (the one algorithm, so there is nothing to
choose and the `none` algorithm is never accepted): `tokens.sign(claims,
expires: 1h)` adds `exp`; `tokens.verify(token)` gives the claims or
fails with InvalidValue. With `auth`, every route needs
`Authorization: Bearer <token>` (401 otherwise) unless it has `@public`;
`@role("admin")` needs the claim `role` to be that text, or the claim
`roles` to contain it (403 otherwise). `request.user` holds the claims
(`none` on a public route without a token). A token that is not
base64url text (three ASCII parts) is simply not valid.

**D104. Answers.** Section 15 returns a record, `204` and `?? 404`. The
value a handler returns is the answer: `none` is 204; an int is the
status (100 to 599, another number is an error), with a JSON body
`{"error": "Not Found"}` from 400 up; a text is `text/plain`; anything
else is compact JSON (D58), with status 201 for POST and 200 otherwise.
An error that leaves the handler is an answer too: NotFound is 404,
InvalidValue and InvalidData are 400, a `@unique` that is repeated is
409, each with `{"error": message}`; any other error is 500, and the
program shows it on the standard error output as an unhandled error
without stopping. Request parameters that cannot be converted answer 400
with the message of the conversion.

**D105. `log` and `@private`.** Section 9 shows `log(customer)` printing
`{name: "Ana", national_id: "***", card: "***"}` and says `@private`
fields are hidden in logs, traces and AI calls; where a log goes is not
written. `log(values...)` writes one line to the standard error output,
the values shown as `print` shows them and separated by a space, with the
value of every `@private` field of a record replaced by `"***"` (the
record keeps its own form, `Customer(name: "Ana", national_id: "***")`,
D30). A secret cannot be logged (D97). Outside logs and `ask` (D108), the
field is an ordinary field: the program reads and prints it.

## nexo.ai (section 8)

**D106. Configuration of `ask`.** Section 8 lists five places, "the first
one that exists wins", and three providers by name (anthropic, openai,
ollama); the format of nexo.toml and of the user configuration is not
written.
- The places are read key by key: a call with `model:` still takes the
  key from the `ai` block. An `ai:` block replaces the file's previous
  one from its line on (D90); `ai profile "p":` is used by
  `ask(..., profile: "p")`, and a profile that does not exist is an error.
- The keys of a block are `provider`, `model`, `key`, `url` and
  `fallback`; another key is an error (with "Did you mean"). `budget` and
  `when_over_budget` need the cost controls of a later phase (D89). `url`
  is the address of the provider (for ollama on another machine, or a
  server that speaks the same API); the defaults are
  https://api.anthropic.com, https://api.openai.com and
  http://localhost:11434.
- A model is `"provider:model"` or a name of `ai models:`; a model without
  a provider uses the configured `provider`. `key` and `url` belong to a
  provider: they come from the first place that configures that
  provider, so a fallback to another provider does not send it the first
  one's key.
- nexo.toml (in the program's folder or the nearest folder above) and the
  user configuration (`$XDG_CONFIG_HOME/nexo/ai.toml`, by default
  `~/.config/nexo/ai.toml`) have an `[ai]` table with the same keys and
  an `[ai.models]` table with the aliases. Values are texts or lists of
  texts in double quotes; `key` there is the name of the environment
  variable that holds the key, since a key is never written in a file
  (section 9). `nexo ai setup` asks for the provider, the model, that name
  and the address, and writes the file.
- Without a provider anywhere, `ask` fails with AIError and the message of
  section 8; a provider without its key (ollama needs none) fails with
  AIError too.

**D107. Calls.** `ask(instruction, input?, options...)` is a function that
can fail (`?` passes its error on). It sends one message to the provider
(Anthropic's Messages API, OpenAI's Chat Completions, Ollama's
`/api/chat`) with the http client and returns the text of the answer.
- `creativity`, `guard` and `redact` take a bare word (`creativity: low`),
  read as text by the parser like the model aliases. `creativity` is the
  temperature: low 0.2, medium 0.7, high 1.0. `max: 200 tokens` limits the
  answer; `max: 200 words` asks for that many words and limits the
  answer to about 4/3 tokens per word. `timeout` is 60s by default.
- `retries` (3 with a type, 1 without) is the number of new attempts after
  a network error or a 502, 503 or 504, and, with a type, after an answer
  that is not a valid value. When a model cannot answer (NetworkError,
  HTTPError or AIError), the next one of `fallback` is tried; the error of
  the last one passes on.
- `stream: true` asks the provider for the answer in pieces and returns
  the list of pieces in order, so `for chunk in ask(..., stream: true)`
  works; the loop starts when the answer is complete, since a value that
  arrives while the program runs needs the streams of a later phase. A
  typed answer cannot be streamed.
- Every answer adds a line to `usage.csv` next to ai.toml (day, model,
  input and output tokens). `nexo ai usage` shows the calls and tokens per
  day and model; spend needs the prices of each provider, which change
  and are not in the specification, so it is not shown.
- `fake ask returns value` (D99) replaces every call in a test.

**D108. Typed answers, `guard` and `redact`.** `o: Opinion = ask(...)`
passes the type of the variable to the call (`as: Opinion`, which can
also be written). The message asks for JSON of the form of the type,
written with the types as Nexo writes them (`{"tone": "positive" |
"negative" | "neutral", "score": 1..5}`); the answer (also inside ```
fences) is read as `json.parse(as: T)` does. An answer that is not a
valid value is sent back with the reason and asked again, up to
`retries` times; then the InvalidData error passes on.
- Every value is treated as external (phase 4 does not mark where values
  come from), so `guard: injection` is the default whenever there is an
  input: the input goes between `<input>` and `</input>` and the system
  message says that text there is data, never instructions. With
  `guard: none` the input follows the instruction after a blank line.
- `redact: private` (the default) shows the input with its `@private`
  fields as `"***"`, as `log` does (D105); `redact: none` sends them. A
  secret is never sent (D97).
- The system message and these instructions are in English, like every
  message (D125), so the same program sends the same request.

**D109. Tools and agents.** Section 8 shows `@tool "description"`, an
`agent` block with `model`, `tools`, `goal`, `max_steps` and `confirm`,
and `support.handle(mail)`; how the agent talks to the model is not
written.
- A `@tool` function is described to the model by its name, its
  description and a JSON schema of its parameters built from their types
  (`int` is an integer, `decimal` a number, a union of texts an `enum`,
  `1..5` an integer with bounds, a record an object with its fields); a
  parameter with a default or an optional type is not required. Only
  `@tool` functions can be tools, and `confirm` can only name tools of
  the agent. Other keys are errors; `mcp("name")` belongs with the MCP
  servers of a later phase (D89).
- `agent.handle(input)` sends the goal (and the guard of D108) as the
  system message and the input between `<input>` and `</input>`, with its
  `@private` fields hidden, using the provider's own way of calling tools
  (Anthropic's `tool_use` blocks, the `tool_calls` of OpenAI and Ollama).
  Each call the model asks for runs the function with its arguments read
  as `json.parse(as: T)` reads them; the value goes back as text (shown as
  `log` shows it), and an error of the function goes back as
  `Error: message`, so the model can recover. A tool in `confirm` is
  shown on the terminal (`¿Usar send_email(to: "ana@mail.com")? (s/n)`)
  and runs only after `s` or `y`; otherwise the model is told the user
  did not allow it. The answer is the text of the first reply that asks
  for no tool; after `max_steps` (10 by default) requests the call fails
  with AIError. The agent uses its model (or the configured one) without
  fallback, with creativity medium.

## Interoperability (section 18)

**D110. `use c "library"`.** Section 18 promises "direct call with headers
read automatically"; how the library and its header are found, and which
C types cross, is not written.
- The library is `lib<name>.so` (or its first versioned file) in the
  folders of `LD_LIBRARY_PATH`, `/usr/local/lib`, `/usr/lib/<arch>-linux-gnu`,
  `/usr/lib`, `/lib/<arch>-linux-gnu` and `/lib`, the first that is a real
  shared library (`libc.so` is often a script for the linker); a name with
  a `/` is a path from the program's folder (`use c "./libgreet.so" as
  greet`). It is used through the name after `as`, or the text itself.
- The header of a few common libraries is known (`libm` is `math.h`,
  `libz` is `zlib.h`, `libc` is `stdlib.h` and `string.h`, and `libssl`,
  `libcrypto`, `libsqlite3`, `libcurl`, `libpthread`); for the others it
  is `<name>.h`, `lib<name>.h` or `<name>lib.h`, looked for next to the
  program (and the library) and in the system's folders. It is read
  through the C preprocessor (`$CC`, `cc`, `clang` or `gcc`): phase 0 when
  the `use` runs, the compiler when it compiles (so the program it builds
  needs neither the compiler nor the header). Not finding the library,
  the header or a C compiler is an error at the `use`.
- A call converts `int` (every integer type and enum of C; a value that
  does not fit in a narrower type is cut to its bits, as C does), `float`
  (`float` and `double`; an int is accepted), `bool` and texts: a `const
  char *` parameter takes a text, and a `char *` result is copied into a
  text (`none` for NULL). A function that uses another type (other
  pointers, which C may write through, structs, `long double`), takes a
  variable number of arguments, or more arguments than the registers of
  the calling convention hold (6 integers and 8 floats on x86-64) is an
  error when it is called, with the type named. An unsigned 64-bit result
  above the largest int is an Overflow. Arguments are positional.
- Section 18's `C functions ... return T! if the header indicates an
  error code` has no way to be read from a header, so results are plain
  values; `nexo audit` belongs with the security tools of a later phase.
- `use rust` and `use wasm` need a later phase (D89).

**D111. `use python module as name`.** Section 18: "embedded Python
interpreter; data is converted at the boundary". Phase 0 runs in Python and
imports the module into itself. The compiler asks the Python of
`$NEXO_PYTHON` (or `python3`) for its shared library and its `sys.path`
when it compiles; the program loads that library when it first imports a
module (a Python without its shared library is an error at the `use`).
- To Python go `none`, `bool`, `int`, `float`, `str`, `decimal` (as
  `decimal.Decimal`), lists, maps, sets and records (as a dict of their
  fields); another value is an error. From Python come the same types
  (tuples become lists; an int that does not fit is an Overflow); a
  subclass of a container (`Counter`) and every other object stay a
  `python` value, shown with Python's `repr`, whose attributes are read
  and that is called with positional and named arguments, both converted
  the same way. A missing attribute says "Did you mean" from `dir()`.
- An exception of Python code is an error value (`Error`, message
  `ValueError en Python: math domain error`) that `try` handles; a module
  that cannot be imported is an error at the `use`.
- "Unannotated Python types arrive as `any`": the values are dynamic, and
  a typed variable checks them as usual (D33).

**D112. `convert(x, to: format)` and the formats of `data`.** Section 10
names `convert(x, to: format)` as the adapter and section 15 lists JSON,
CSV, Parquet, XML, YAML and columnar tables in `data`. `convert(x, to:
json)` gives the JSON text of a value, as `save` writes it (indented with
two spaces), and `convert(rows, to: csv)` the CSV text of a list of
records or maps; `convert(money, to: USD, rate: r)` stays as it was, and
`rate` belongs only to it. Parquet, XML, YAML and columnar tables need
their own readers and writers and are not part of phase 4's exit
criterion; they stay for a later phase, and `yaml`, `xml` and `parquet`
are ordinary names until then.

## Phase 5: cloud and operations (section 25)

**D113. What phase 5 covers.** Section 25 lists "`nexo.infra`, `nexo.ci`,
`nexo.stream`, flags, privacy" with the exit criterion "an app deploys to
AWS, Azure, GCP and k8s from the same file". Both implementations get,
with the same output: `infra` and `stage` blocks with the portable
resources of section 11, `native` resources, `output`, the `policy` of an
infra block, `budget`, and the commands `nexo plan`, `apply`, `destroy`,
`drift`, `cost` and `import`, which talk to each cloud's own API (D118-D121);
`pipeline` blocks with `nexo ci run` and `nexo ci export github|gitlab|
azure|jenkins` (D122, D123); `when` on a stream source, `send` and the
window steps of section 19 on a local broker (D117); `flags` and `flag()`
with `nexo flags set` (D115); the markers, rules and functions of section
23 with `nexo privacy report` (D116). What needs a running controller or
measured metrics stays out and is reported as unavailable (D65): `release
canary` and `blue_green` (`rolling` is the default and the only strategy),
`watch` and `rollback` in flags, high availability across regions, the
`calls` block, `scale at`, `nexo chaos` and `nexo release`; also what
other phases or modules own: the program `policy` of section 9, `notify`
(`slack`), `audit`, `scan`, `every`, `lifecycle`, `when` on things that are
not streams (email, files, webhooks), secrets from a vault or a cloud
manager, brokers reached over the network (Kafka, RabbitMQ, SQS, Kinesis,
Service Bus, Event Hubs, Pub/Sub, WebSocket: D117), `flags_store`,
PostgreSQL. `unsupported` messages now say "phase 5".

**D114. The body of phase 5 blocks.** Section 4 says configuration blocks
are `word name?:` followed by `key = value` pairs, but the examples of
sections 11, 12, 22 and 23 also write lines of words (`deny public
database`, `can read storage photos`, `trigger push to main`, `queue
orders`), nested blocks (`network net:`, `env:`, `scale 3..50:`), options
after a comma (`retry = 3, backoff: exp`, `output db = customers.url,
secret`) and conditions (`cpu > 70%`, `users where country == "ES"`). Both
parsers read the body of `infra`, `stage`, `pipeline` and `flags` with one
rule:

    entry  = phrase [ "=" items ] [ ":" NEWLINE INDENT { entry } DEDENT ] NEWLINE
           | phrase { "," item } ...
    items  = item { "," item }
    item   = IDENT ":" atom | phrase
    phrase = atom { atom }

An atom is an expression without `->`, `|>` or lambdas (so a phrase can
hold several), or a keyword that cannot start one (`every`, `at`, `type`),
or `->`. Inside atoms a bare name is a word (the text of the name: `size =
medium` is `size = "medium"`), a dotted name is a reference to what a
resource exposes (`customers.url`), a comparison or `and`/`or`/`not` is a
condition kept as data (`errors < 1%`), and `2027-03-01` is a date;
calls (`secret("API_KEY")`, `s3("bucket")`) and their arguments are
ordinary code. Each entry becomes a value made by `__cfg(head, values,
options, body)` (D90) that the module reads, so every mistake is reported
at the line of its entry. The bodies of `step` and `after` in a `pipeline`
are ordinary code (section 12). A value of a program constant is written
inside a text (`image = "{IMAGE}"`), since a bare name is a word.
`nexo fmt` keeps, inside these blocks, the space a phrase needs before
`(` or `[` (`require tags ["team", "cost"]` has two words, `tags[0]`
would be an index) and the date `2027-03-01` together. `test` is a
keyword, so `test()` in a step (section 12) is the call of nexo.ci's
`test`: the keyword followed by `(` is a name.

**D115. Flags.** Section 22 declares flags in a `flags:` block of `name =
value` lines and reads them with `flag(name, user)`. A value is `on`,
`off`, a percentage from 0% to 100%, or `users where condition`, whose
words are fields of the user (`country == "ES"`); `expires: date` is the
only option (`watch` and `rollback` need metrics, D113). `flag(name)`
needs the user only for a percentage or an audience. The same user always
gets the same result (rule 1): the first 8 bytes of SHA-256 of
`name:key`, modulo 10,000, below the percentage times 100, where the key
is the `id` field of a record, or the text of a text or an int. Rule 2: a
literal name no block declares is a compile error; a name built at run
time is checked when it is used. Rule 3: `nexo lint` warns about a flag
whose `expires` day (in UTC) has passed. Rule 4: `nexo flags set name
value [file.nexo] [--stage s]` writes on, off or a percentage to
`.nexo/flags.json` (the local store of the last paragraph, under the stage
or `default`), which `flag()` reads again whenever the file changes, so
running programs see it at once; the program's stage is the environment
variable `NEXO_STAGE` (set by `nexo apply` on every app, D120). The change
is recorded as a line of `.nexo/audit.log` with the time, `$USER`, what
changed and the SHA-256 of the previous line, so a changed or removed line
shows (section 9 calls the audit log signed and append-only; `audit()`
itself is not part of phase 5). The commands of phase 5 that read a
program take its file, or else the only `.nexo` file of the current
directory with that block.

**D116. Privacy.** Section 23 marks fields `@personal` or `@private` and
adds `keep` and `purpose` to a type. A type with either marks or settings
is followed by `__privacy_type(T, [settings], {marks})` (D90); its
settings are read with the rule of D114. `keep` is a duration `after` a
`date` or `time` field that is not `@private` (`keep = 5 years after
last_activity`; a year is 365 days, D2), and `purpose` a text; any other
setting is an error. The rules:
1. `nexo lint` warns once for each of `keep` and `purpose` that a type
   with personal fields lacks; `nexo build --release` stops with an error.
2. In a table, rows whose field is older than the period are erased the
   first time the table is used through a connection and again whenever
   it is used 24 hours later: the program that uses the data is the
   daily task, since `every` is outside phase 5. Dates compare in UTC.
3. `data_region` is checked by `nexo plan` (D121).
4. `nexo privacy report [file.nexo]` runs only the types of the program
   (as `nexo db diff` does) and lists, type by type, its fields with their
   mark, where the rows live (the table and the database of the first
   `connect`, or "not stored in a database"), the period, the purpose and
   who reads it: the program and every `identity` of an infra block with
   `can read database`.
Encryption at rest: the `@private` columns of a table are stored as
`nexo:` and the base64 of a nonce and AES-256-GCM, with the SHA-256 of the
`key:` given to `connect` (D100) as the key; the nonce is the HMAC-SHA256
of the text with that key, so equal texts give equal values and
`find(national_id: x)` still finds the row, while `where`, `sort by` and
`in` on such a column run in memory. A table with `@private` fields
opened without a key is an error, as is a wrong key when a value is read.
`privacy.export(record)` writes `<table>-<id>.json` (as `save` writes
JSON) with the row and the rows of its `many` fields, whose `ref` fields
are written as ids, and returns the path; it can fail like any write.
`privacy.erase(record)` deletes the row; when other rows point to it, it
is anonymised instead (its personal fields become none, `""`, 0 or the
first day; a `@unique` one that is not optional becomes `erased-<id>`, or
minus the id for numbers, so that several erased rows keep it unique) and
recorded as erased, and the personal fields of the rows
that point to it are anonymised as well. `privacy.restrict(record)`
records it as restricted. The table `nexo_privacy (tbl, row, state)`
holds both, and every query of a table with personal fields leaves its
rows out; `db.sql` reads them, for legal obligations. `@private` makes a
`ref` field an error (the data are in the other table).

**D117. nexo.stream.** Section 19 makes a stream "just another source for
`when` and `|>`". The sources (`kafka`, `rabbit`, `sqs`, `kinesis`,
`servicebus`, `eventhub`, `pubsub`) take the name of the stream; phase 5
has one broker, local, so a source with the address of a broker on the
network (`servers:`, `url:`...) and `websocket(...)` are reported as
unavailable (D113). Each stream is an append-only file
`.nexo/streams/<kind>/<name>.log`, one JSON line per message (its number,
the time in milliseconds and the data), shared by the programs of the
machine, which lock the file while they number and append a message, so
producers in several processes never share a number; under `nexo test` the broker lives in memory, starts empty for
each test, and `send` delivers the message at once to the program's own
handlers and pipelines. `use nexo.stream` (like `nexo.infra` and
`nexo.ci`) is accepted and changes nothing: the module is loaded
implicitly (section 1).
- `stream.send(value, at: time)` writes a text as it is and any other
  value as compact JSON; `at:` sets the time the message happened (for
  windows; the default is now).
- `when SOURCE as msg[, key: expression]: block` becomes a function and
  `__stream_when(SOURCE, it, key: (msg) -> expression)` (D90); `when` on
  anything but a source (email, files, webhooks) stays unavailable. A
  message has `msg.id` (its number in the stream), `msg.time`,
  `msg.text()` and `msg.json()`, which fails on data that is not JSON;
  `validate(map, as: Record)` builds the record as JSON data is read
  (D47), so section 19's `validate(msg.json(), as: Order)?` works.
- After the top level (and the spawned tasks), a program with handlers or
  stream pipelines reads its streams until it is stopped, next to its
  servers if it has any, as consumer group named after the file: the
  offset of the last acknowledged message is saved in
  `<name>.<group>.offset`, and a new group starts at the first message.
  A message is acknowledged when its block ends without error (at least
  once). A failing block (an error value or a mistake) is tried 3 more
  times, 0.1 s, 0.2 s and 0.4 s apart, and then the message goes to the
  stream `<name>.dead` with a line on the standard error output saying
  why. With `key:`, the key of each processed message (as `repr` writes
  it) is kept in `<name>.<group>.<n>.keys` and a message with a key already
  processed is acknowledged without running the block. Each handler has
  its own keys (n is its place among the `when` of that stream), so two
  handlers of one stream each process a message once. There is one
  partition per stream, so order is kept and handlers run one at a time.
- A pipeline whose source is a stream keeps its steps and runs them over
  the messages of each window, whose rows are the decoded data (JSON, or
  the text); it starts when it reaches `each`, which gets each row, or
  once the value of a final `count`, `sum` or `avg`. Call steps are not
  allowed on a stream. `window D` is a tumbling window aligned to
  multiples of D since 1970 (UTC); `session D` ends after D without
  messages; both close when a message of a later window arrives, when
  the time passes (while the program runs) or at the end of a test.
  `sliding D` runs on every message over the messages of the last D.
  The offset saved never passes the first message of a window or session
  still open, so a consumer stopped before it closes reads those messages
  again (and the handlers of that stream see them again: at least once).
  These three are contextual steps after `|>`, must come right after the
  source, and are errors on anything else.

**D118. The model of nexo.infra.** Section 11 gives the resources, the
keys of its examples and the commands, not the exact list of settings
nor what a command runs. Both phases share one table,
`nexo/stdlib/infra_providers.json` (the compiler embeds it), and read
the blocks with it:
- `infra NAME:` takes `cloud` (`"aws"`, `"azure"`, `"gcp"`, `"k8s"`,
  required), `region` (required except on k8s), `state`, `budget`,
  `data_region`, `tags`, `project` (GCP), `subscription` (Azure),
  `namespace` and `cluster` (k8s), `policy:`, `output` and the
  resources. Each resource kind has its keys with a type (text, number,
  size, count `1..4`, bool, a reference to another resource of a given
  kind, tags) and a default; an unknown key is an error with "Did you
  mean...?". The storage key is `capacity`, not `size`, because a stage's
  `size = large` means the size of machines (`small`, `medium`, `large`,
  which the table turns into each cloud's classes).
- A resource body may hold the lines of section 11 (`allow from internet
  to api, port: 443`, `can read storage photos`), `env:` (texts, numbers,
  `secret("NAME")` and references `customers.url`), `scale MIN..MAX:`
  with `cpu > N%`. What a resource gives (`url`, `host`, `id`...) is
  listed per kind; a reference to anything else is an error at once.
- `stage NAME:` holds `key = value` lines that override, for that stage,
  the value of every resource that has the key (`size`, `replicas`...).
  Values apply in the order: the default, the resource's value, the
  stage's (D119). Without `--stage` the stage is `default`; the app gets
  `NEXO_STAGE` (D115).
- A portable resource with no equivalent on a target (`server` and
  `cluster` on k8s) and a `database` on k8s in the stage `prod` (section
  11 says "dev only") are plan errors, as the section requires.
- `nexo plan|apply|destroy|drift|cost [file] [--stage S]` (and `--yes` for
  apply) run, like `nexo db` (D101), only the types, the constants written
  as literals and the infra and stage blocks of the program, never its
  other code; without a file they take the only `.nexo` file of the
  directory with an `infra` block. A program without one is an error.

**D119. Plan, cost and policy.** Section 11 asks `nexo plan` to show the
changes, "the estimated cost and the estimated availability", and
section 22's `budget` to stop a plan, without saying how they are
computed. Both phases print one block per infra: the portable resources
with `+` (create), `~` (change, naming the changed fields), `-/+`
(replace, for the fields a cloud cannot change), `=` (keep) and `-`
(delete, what the state has and the program no longer does), each with
its native resources, then the totals. The cost is the sum, in EUR per
month, of the on-demand list prices in the table for each resource at
its size, replicas, cpu and ram, in whole cents; it is an estimate and
says so. The availability multiplies the published SLA of each service
the infra uses. `budget = N EUR per month` is the only form; an
estimate above it is an error that names both. The rules of `policy:`
are `deny public KIND`, `require encryption` and `require tags [...]`
(the tags of the infra count for every resource), plus `allow personal
data to "AREA"`; `data_region = "eu"` makes a region outside that area an
error when the program has `@personal` data (section 23, rule 3), with
the areas of each cloud's regions in the table. Anything else in
`policy:` is an error.

**D120. How the commands talk to each cloud.** Section 11 does not say
how the plan is applied. Both phases talk to each cloud's own API over
HTTPS, with no SDK nor external tool: AWS through Cloud Control
(`CreateResource`, `UpdateResource` with a JSON Patch, `DeleteResource`,
`GetResource`, `GetResourceRequestStatus`) signed with SigV4, which
covers every CloudFormation type, so `native "aws_wafv2_web_acl"` is
`AWS::WAFv2::WebACL`; Azure through Resource Manager (`PUT`/`DELETE` of
each resource id, polling `Azure-AsyncOperation`) with a client-secret
token; GCP through each service's REST API with its long-running
operations and IAM policies; Kubernetes with server-side apply
(`fieldManager=nexo`) and a bearer token. `native` takes the
resource's own type and properties as they are. Credentials come from
the usual variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`; `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
`AZURE_CLIENT_SECRET`, `AZURE_SUBSCRIPTION_ID`;
`GOOGLE_OAUTH_ACCESS_TOKEN`, `GOOGLE_CLOUD_PROJECT`; `NEXO_K8S_SERVER`
and `NEXO_K8S_TOKEN`, with `NEXO_K8S_CA` for the certificate authority of a
cluster outside it, or the service account of a pod), and the
addresses can be changed (`AWS_ENDPOINT_URL`, `AZURE_AUTHORITY_HOST`,
`NEXO_AZURE_ENDPOINT`, `NEXO_GCP_ENDPOINT`), which is how the tests use
`tests/cloud_emulator.py`, a local stand-in for those APIs that checks
the signatures and tokens. Native resources are created in the order
their references need, and deleted in reverse; a failure stops at once,
saving what was done.

**D121. State, secrets, several infra blocks and import.** The state of
each infra and stage is a JSON file: `.nexo/state/<infra>-<stage>.json`,
or, with `state = s3("bucket")`, the object `nexo/<infra>-<stage>.json`
of that bucket, written with server-side encryption and locked with a
conditional write of a lock object (section 11: "encrypted and
locked"); `gcs(...)` and `blob(...)` are reported as unavailable. Both
phases read and write the same state. A secret is sent to the cloud
when applying, read from the environment, and is a hash
(`sha256:...`) in the state, the plan and the output; values the cloud
echoes back are scrubbed, and `output ..., secret` prints `(secret)`.
`apply` asks for confirmation unless `--yes` (with no answer, as in CI
without `--yes`, nothing changes); `destroy` asks to type the infra's name, block by block.
`drift` compares what each API returns with the state, field by field.
The state is locked only when the changes start: if another command
changed it since it was read (while one asked for confirmation), the
command stops without changing anything and asks to run it again. A
secret whose value changed is a change (its hash differs), also for a
GCP secret, whose version is kept beside its body.
A value of another infra block is written `infra.resource.value`; the
blocks are applied in the order those references need (a cycle is an
error) and each one reads the other's values from its state, so a plan
before the first apply shows them as known after applying; destroy goes
in the opposite order. `nexo import CLOUD RESOURCE ID [file]` (section
11's `nexo import aws app i-0abc123`) adopts the existing object with
that id as the native resource of the program's resource named
RESOURCE (`RESOURCE.native` when it has several), reading it into the
state; the next plan then shows it as kept or changed.

**D122. nexo.ci: pipelines and `nexo ci run`.** Section 12 shows a
`pipeline` with `trigger`, `secrets`, `version = auto`, steps of ordinary
code and `after success|failure`, and says `nexo ci run` runs it
locally; it does not say what the step functions do, how values pass
between steps nor how the version is computed. Both phases:
- The entries are `trigger push`, `trigger push to BRANCH`, `trigger
  pull_request`, `secrets = ["NAME", ...]` (environment variables the
  run needs; `from vault` is unavailable, D113), `version = auto` or
  `version = "X.Y.Z"` (without it the version is `dev`), `step NAME:` and
  `after success:` / `after failure:`; anything else is an error. A step
  body is a function of the run (D114): the names it assigns are kept and
  the later steps read them; `version` and `step` (the current step, or
  the failed one in `after failure`) are always there. What is kept must
  be text, numbers, bools, lists or maps, because in a CI it travels
  between jobs as JSON in `.nexo/ci/<pipeline>.json`. `return` in a step
  is an error.
- `nexo ci run [PIPELINE] [file] [--step S | --after success|failure]
  [--yes]` runs, like the infra commands (D118), only the program's
  declarations (types, functions, literal constants) and blocks, never its
  other code; without a name it takes the only pipeline. It prints `==
  paso NAME` before each step, stops at the first step that fails (an
  error value or a mistake, shown as usual), runs `after failure`, and
  ends with code 1; otherwise it runs `after success` and prints the
  count and the version. `--step` runs one step with what the earlier
  ones kept, which is how exported jobs run (D123).
- `version = auto` reads git: the highest tag `vX.Y.Z` reachable from
  HEAD (0.0.0 without one) raised by the commits after it, following the
  comment of section 12: a `fix:` raises the patch, a `feat:` the minor,
  and `type!:` or a `BREAKING CHANGE:` line the major (Conventional
  Commits); outside a repository it is an error.
- The functions of section 12: `run(command)` runs it with the shell,
  shows what it writes (stdout and stderr together) and returns it; a
  non-zero exit is an error value that stops the step. It is the only
  one usable outside a pipeline. `test()` runs `nexo test` in the
  directory; `build_image(name)` runs `docker build -t name:VERSION .`
  (`NEXO_CONTAINER_TOOL` chooses another tool, such as podman) and returns
  the tag; `apply(stage:, image:)` applies the infra blocks of the file
  without asking, with `image` replacing the image of every app whose
  image has the same name; `plan(stage:, image:)` returns the plan as
  text; `approve(by: [...])` asks on the terminal unless `--yes`;
  `comment(text)` shows the text and appends it to `.nexo/ci/comment.md`
  (the exported GitHub job can post it). `audit()`, `scan()` and
  `slack()` are unavailable (D113). Inside `parallel:` they run as any
  call does.

**D123. `nexo ci export`.** Section 12 says `nexo ci export github` (also
`gitlab`, `azure`, `jenkins`) "generates the native file". So that a CI
runs exactly what `nexo ci run` runs, both phases write one job (stage in
Jenkins) per step, in order, that installs Nexo (`pip install nexo`) and
runs `nexo ci run PIPELINE FILE --step NAME --yes`; `.nexo/ci` travels
between jobs as an artifact (in Jenkins the workspace is shared). The
files are `.github/workflows/<pipeline>.yml`, `.gitlab-ci.yml`,
`azure-pipelines.yml` and `Jenkinsfile`. Triggers become each service's
own (`on: push/pull_request`, workflow rules, `trigger`/`pr`, `when`
conditions); secrets become its secret variables. A step whose code calls
`approve(by: [...])` with a literal list waits for the service's approval
before running (a GitHub environment named after the step, a manual
GitLab job, an Azure `ManualValidation` task notifying those people, a
Jenkins `input` with them as submitters), which is why the job passes
`--yes`. `after success` and `after failure` become jobs that run when
the steps pass or fail.

## Modules of the program's own files (section 13)

**D124. `use` of a file of the program.** Section 13 says "a module is a
file; its name is its path (`shop/payments.nexo` is `shop.payments`)" and
"only what is marked `pub` is exported", and the grammar has `use_stmt`,
but neither says where paths start, how imported names are written or when
a module's code runs. Both phases (nexo/modules.py, compiler/nexoc/src/
modules.rs) do this:

- `use shop.payments` (any name that does not start with `nexo.`) loads
  `shop/payments.nexo` from the root of the project: the folder of the
  nearest nexo.toml above the program, or the program's own folder when
  there is none. So a module has one name wherever it is used from, and
  `nexo test shop/payments.nexo` finds the modules it uses in a project
  with a nexo.toml. Messages show the module's path relative to the
  current folder.
- Its names are used qualified, with the last part of its name
  (`payments.charge(...)`, `payments.Order`, `payments.Paid(x)` in a
  pattern) or with the name given by `as`. There is no unqualified import:
  one way to name each thing (section 1). Patterns accept a dotted name for
  the variants of another module; nothing else changes in the grammar.
- The grammar only allows `pub` on `fn` and `type`, so a module exports
  its `pub` functions, its `pub` types and the variants of a `pub` enum.
  Constants, `var`s and traits are not exported; a `pub` function can
  return a constant.
- A module's code runs once, at its first `use`, in its own top scope,
  which sees only the builtins and the modules it uses itself. Its `test`
  blocks only run when it is the file given to `nexo test`.
- `use` of a module is only allowed at the top level of a file.
- The commands that run only part of a program (`nexo db`, `nexo privacy
  report`, the commands of nexo.infra and nexo.ci, D101, D116, D121,
  D122) run the same part of each module it uses.
- Everything about modules is a compile error, checked before the program
  runs: a missing file, a cycle (the chain is shown), a name the module does
  not have ("Did you mean...?") or that is not `pub`, the module's name used
  as a value, bound to anything else in the file (a variable, a parameter,
  another module) or assigned through (`payments.charge = ...`).
- Every file is parsed and checked on its own (the `T!` and `?` rules of
  D17-D24 look only at the file's own functions, as in phase 0). An error
  inside a module shows the module's file and line in both phases: the
  compiler's spans carry the file, and a compiled program registers the
  source of each module for its messages. `nexo check`, `nexo lint` and the
  language server read the modules too; the language server takes the
  folder it was started in as the program's folder.
- In the compiler each module is resolved in its own top scope and its
  names are static like the program's; `payments.charge` is a plain name
  for the later passes, so calls to it take the same fast paths. Two files
  may define types with the same name; the contract prover (D83) then does
  not prove `ensure`s that depend on the int fields of either.

## Messages in one language

**D125. English only.** Section 14 and section 16 describe messages in the
system language, and D55 made Spanish the default with English behind
`--lang en`. The owner of the project decided that Nexo has one language
for its messages: English. This is the simplest option consistent with
section 1 ("one way to do each thing"): one catalog, one expected output
per program, nothing to choose.

- `--lang` no longer exists in either phase; `nexo --lang en file.nexo`
  is a usage error (exit code 2), like any other unknown option. The
  language server ignores the editor's locale, and the VS Code extension
  has no `nexo.lang` setting.
- The catalogs (`nexo/errors.py`, `compiler/diag/src/catalog.rs`) hold
  one text per key, and both phases hold the same text for every key
  (tests/unit/test_errors.py). The rule that every message has its fix
  sentence (section 14) is unchanged.
- The summary of `nexo test` ("2 tests: 2 passed, 0 failed"), the usage
  line, the REPL banner and every word of the parser's messages are in
  English. Questions that wait for a confirmation (`nexo apply`, an
  approval in `nexo ci run`, an agent's `confirm`) accept `yes` and `y`
  (and `n` to refuse a tool, D109) only.
- A compiled program no longer receives a language when it starts: the
  runtime's `nx_init` lost its language argument.
- The conformance cases that only existed to show the English messages
  (`tests/conformance/en/`, `s14_es`) were removed; `s14_en` became `s14`.
  Every `.expected` now holds the English text.
- Keywords were already in English (section 16); the editor views of
  keywords in other languages (`nexo fmt --view es`, `nexo config lang`)
  were never implemented and are not planned.

## Phase 6: optimisation and 1.0 (section 25)

**D126. Faster compiled code.** Section 25 asks phase 6 for the
performance goals of "Goals and guarantees" (under 1.5× Rust's time and
1.3× its memory). None of these changes what a program prints.
- A value is certainly a record of a type defined once (D84) when it is a
  call to that type, `self` in one of its methods (methods are only
  reached through a value of their type, and `self` is never assigned),
  or a variable whose every assignment (with `=` and no declared type) is
  one. `x.field` on such a value reads the field by its index, without
  looking up the name; `x.method(args)` calls the method's function
  directly, with the receiver as `self`, when the call has one positional
  argument per parameter after `self`. A field declared `int` (not weak)
  read from such a record is certainly an int: storing a field checks its
  type.
- A record keeps up to four fields inside its object (one allocation; the
  object does not grow, other values are larger), and the compiled code
  reads them at an offset the runtime finds when it starts.
- `for i in a..b` with both ends certainly ints is a counted loop: no
  range object and no iterator.
- Reference counts change without atomic instructions until the program
  starts a second thread that may touch values (`parallel`, `spawn`, a
  server, a stream that listens). The flag is set before that thread
  starts and is never cleared, so every later change is atomic; a program
  with no threads never pays for them.
- mimalloc as the allocator and link-time optimisation of the runtime
  were tried and measured slower; they are not used.

**D127. Compiling large programs.** The goal is 100,000 lines in under
10 s in development mode. Resolving names was quadratic: each name was
looked up in a list of the names of its scope, and each read computed its
"Did you mean...?" suggestion against every visible name. Scopes now keep
an index of their names, and suggestions among a large scope (more than
32 names) or the builtins use an index of the strings left after
deleting up to two characters (two names within distance 2 always share
one); they give exactly the suggestion the linear search gave, the first
name in scope order among the closest. Development mode (`nexo
file.nexo`, `nexo build` without `--release`) no longer optimises the
generated code (`-O0` instead of `-O1`): it compiled several times
slower than it runs faster, and `--release` is the mode for speed.

**D128. Reproducible release builds and the bill of materials.** "Goals
and guarantees" asks that two builds of the same code produce the same
binary byte for byte and that `nexo build --release` generate an SBOM.
The only byte that changed between two builds was the name of the
temporary IR file, which the object file records: it is now always
`program.ll`, in a directory of its own. Builds of the same file give the
same bytes from any directory and at any time. Next to the program,
`nexo build --release` writes `<program>.sbom.json`, a CycloneDX 1.5
document: the program (with the SHA-256 of the binary), each source file
(main file and modules, with their SHA-256), the runtime and the crates
it is built from (from the compiler's `Cargo.lock`, as `pkg:cargo` URLs),
the C libraries and Python modules the program uses, and the C compiler.
It has no date and no random serial number, so it is reproducible too.
A development build writes none. Signed packages need the registry of
section 13, which does not exist yet.

**D129. `nexo audit`.** "Goals and guarantees" says `nexo audit` lists
every use of `unsafe`, and section 18 that calls to C count as implicit
`unsafe`. `unsafe:` blocks belong to `nexo.perf`, outside this edition
(D89), so they are a compile error and never reach the audit. `nexo audit
[paths]` reads every file (like `nexo lint`) and prints, in the order of
the file, each `use c` library and each call to a function of one, as
`file:line: text fix`, then a summary; with none, "No unsafe code in
...". It does not fail (exit code 0) unless a file cannot be read or
parsed. Calls to Python are not unsafe: data is converted at the
boundary (section 18).

**D130. `nexo docs --for-ai`.** "Goals and guarantees" says it gives any
model "the exact list of available functions". The list is written once,
in `nexo/stdlib/api.json` (read by both phases, like
`infra_providers.json`): each function of the core, the standard library
and the modules of this edition with its signature and one line on what
it does, the values (`PI`, `now`...), the types, the methods of the
builtin types, and the names that exist but are not available in this
edition (brokers on the network, `mcp`, `audit`...). A test checks that
it names every builtin of both phases and nothing else. `nexo docs
--for-ai file.nexo` adds the program's own top-level functions, types
and traits: their first line, their `##` comments and the fields and
methods of types. `nexo docs` without `--for-ai` is a usage error: the
other documentation is the site.

**D131. `nexo test --suggest`.** "Goals and guarantees" says it
"generates edge cases from types, contracts and unions". It prints test
blocks for every top-level function whose parameters all have a type it
can make values of: for `int` 0, 1, -1 and the largest int, plus the
values on each side of the bound of an `expect param OP literal` (either
way round); for `float` 0.0, -1.5 and 1.5; for `str` "" and "a b"; for
`bool` both; each literal of a union; the two bounds of a range (also when the
union or range is a named type of the file, `type Level = "low" |
"high"`); each variant of an enum of the file (variants without data); `[]` and a list
of one value; `none` for `T?`. A first case uses, for each parameter, its
first value that keeps the `expect`s; every other case changes one
parameter. A function with a parameter of another type (a record, money,
a map...) is left out. The expected values are what the function gives
now: a probe program (the file's definitions and literal constants,
without the rest of its top level, next to the file so its modules are
found, run for at most a minute) calls every case and prints the result
as JSON, and each result becomes `assert call == value`, or, when the
call fails, a check of the error's kind. A result with no plain literal
(a record, a list) is a comment with its JSON. The header says to check
each value before keeping it: the suggestion records behaviour, it does
not know what is right. Both phases run the probe with their own
implementation and print the same text.

**D132. `nexo check --intent`.** "Goals and guarantees" says it
"compares `##` comments and contracts with what the code does"; D87 left
it to `nexo.ai`, since reading the intent of a comment needs a language
model. For every top-level function with `##` comments right above it or
an `expect` or `ensure`, the text of the function (comments included) is
sent to the model with `ask` and a typed answer (`agrees: bool`, `reason:
str`). A probe program with the file's `ai` blocks asks the questions, so
the configuration (D106), the providers, the retries and `fake` are those
of `ask`. Each function the model says disagrees is a warning on its
first comment line, "The comments of "f" and its code disagree:
reason.", then a summary, with exit code 1; otherwise "The comments and
the code agree in ...". When the model cannot answer (no provider, a
network error) the error of `ask` is printed and the exit code is 1. The
public set of cases with known mismatches that verifies this guarantee
needs a real model and is left to the project (D133).

**D133. What phase 6 covers and how its exit criterion is reported.**
Section 25 gives phase 6 "performance goals, external audit, user
studies" and the exit criterion "the guarantees in 'Goals and guarantees'
are met or removed". The code side is done here: the optimisations of
D126-D127, the tools the guarantees name (D128-D132), differential
fuzzing (`compiler/scripts/fuzz.py`: random programs that are correct by
construction run in both phases, with the same output, no crash and no
leak; nightly in `.github/workflows/fuzz.yml`), the percentage of errors
with a fix (`compiler/scripts/fix_rate.py`) and the measure of the
performance goals (`compiler/bench/goals.py`, written to `goals.json`).
`docs/guarantees.md` gives the state of every guarantee. Removing a
guarantee from the specification is the owner's decision, not an
ambiguity to settle here: the ones not met (time and memory against
Rust, compiling 100,000 lines in 10 s, capability permissions) and the
ones that need people (external audits, user studies, the registry,
comparisons against models) stay in the specification and are listed as
not met, so phase 6 stays open.

**D134. More fast paths of the compiled code.** A second round on the
performance goals; none of these changes what a program prints.
- A pipeline of `where` and `map` steps that ends in `sum` or `count` is
  one loop that calls the functions of its steps directly (at `--release`
  LLVM can inline them), with no lazy values in between. A step whose
  value is a function still calls it with the element (D51). `sum` keeps
  an exact int total while every element is an int and nothing
  overflows; otherwise the remaining elements are kept and added at the
  end, after every element has been read, as `sum` always did, so an
  error of a step still comes before an overflow. A database query, a
  stream or a grouped value takes the usual steps, which translate them.
- A list being iterated (by `for`, a comprehension or a fused pipeline)
  is read in place: the iterator holds the list, so a change through a
  variable copies it and the elements the loop reads do not move. A
  range is counted without a boxed iterator.
- `xs.add(x)` on a list its place alone owns, with a value that is not
  lazy, is pushed directly; `m[k]` and `m[k] = v` with an int `k` and
  `xs[i]` with an int `i` skip the general index code. Int keys of maps
  and sets have a table of their own with a cheaper hash.
- `len(x)` and `print` with positional arguments skip the general
  binding of arguments.
- Freed heap objects go to a list per thread and are reused by the next
  ones (every object has the same size); a small record of plain values
  is freed without the general work list.
- Fixed on the way: `fits` released a value it had not taken when a heap
  value was checked against `int` (a field of the wrong type in
  `json.parse(..., as: T)`), which could free an object still in use.
  The atomic counts had hidden it; the counts of D126 made it visible.

**D135. Faster development builds.** Compiling 100,000 lines took 208 s,
nearly all in clang. Development mode now (1) calls the reference-count
helpers instead of copying them into every use (`noinline` instead of
`alwaysinline`; release builds keep them inline), which halves the code
clang compiles; (2) cuts a large program's IR (over 2 MB) into as many
parts as there are cores, up to eight, compiled at the same time and
linked together (constants and globals are defined once and declared in
the other parts, so each keeps one address); (3) leaves out the check
that a function or type is defined when it certainly is: it is defined
once by a statement of the top level, and the code that uses it is a
later statement of the top level or runs inside a function (or method)
that a later statement defines, which can only run after its own
definition; and (4) looks for the "Did you mean...?" of a name only when
it writes an error that may show it, among the names that were visible
at the lookup (the resolver keeps how many names each scope had).
Release builds are never cut: LLVM optimises the whole program at once,
and two of them still give the same bytes (D128).

**D136. Short texts inside their object.** A text was an object (112
bytes, like every heap value) plus a separate buffer. A text of up to 70
bytes now lives inside the object: one allocation instead of two, and less
memory. Longer texts keep their own buffer. Nothing a program prints
changes.

**D137. Lists of ints keep the numbers.** A list element was a value of
two words (16 bytes): twice the memory of Rust's `Vec<i64>`. A list whose
elements are all ints now keeps the numbers themselves, eight bytes each;
the first element that is not an int turns it into a list of values, once.
Reading a list never changes how it is kept (several tasks may read one
list at once); only a change by the list's single owner (`add`, `xs[i] =`,
a change through an element) does. The compiled code reads the numbers of
a list of ints in place and `xs.add(n)` pushes an int directly. What a
program prints does not change.

**D138. Short texts inside the value.** A text of up to seven bytes (a
word, a key, a digit, one character of `split`) is kept in the value
itself, two words with a tag of its own, with no object and no
allocation; a longer text is an object (D136). Every text of up to seven
bytes is kept this way, so two texts are equal exactly when both are short
with the same bytes or both are objects with the same text. Everything
that reads a text (methods, `len`, indexing, `in`, iteration, keys of
maps, JSON, SQL, Python, C) takes it from the value. What a program prints
does not change.

**D139. The time limit in functions on ints.** A function the compiler
runs on ints only (D84) checks the time limit of `try within` (D58) when
it is entered and on each pass of a loop inside it, not between each two
of its statements: its straight-line code is short, so the limit is still
seen after a bounded amount of work, and every call checks once instead
of once per statement. Every other function, and phase 0, check between
statements. What a program prints does not change.

**D140. Names certainly defined, and names never defined.** D135 skips
the check that a name is defined for a function or type the top level
defines earlier. The compiler also skips it for a `var` declared by an
earlier statement of an enclosing block of the same function, and for
parameters and loop variables (bound before their block runs). A name
that only `name = value` statements bind is never defined when every one
of those statements has, in the same function, a `var` of that name
declared by an earlier statement of an enclosing block: the `var` takes
each assignment (D26). Such a name is left out when the compiler looks a
name up, so `total += x` inside a loop over `var total` reads and writes
`total` directly. A statement the compiler does not look into (a module,
`select`) counts as one that may define its name. A `var` that is the
only statement binding its name is not checked for being declared twice:
its scope starts again each time it is entered, so the name is not
defined yet when the `var` runs. What a program prints does not change.

**D141. Compiling in development mode.** Clang without optimisation has
a fast instruction selector that does not take a value of two words
passed, loaded or built as one: a function with such a value fell back
to the slow selector, which took most of the compile time. Development
mode now rewrites the program's IR before clang reads it
(compiler/nexoc/src/scalar.rs): values are loaded, stored and passed to
the helpers as two words; a function with a form the rewrite does not
know is left as it was. The parts of a large program are rewritten by
one thread each and reach clang through pipes, not files. The compiler
also emits less code wherever it knows more, in both modes:

- a variable that only ever holds ints is not released;
- a register known to hold an int or a bool needs no count and no check
  of its type, and a single comparison gives the condition of an `if` or
  `while` directly;
- `%` and `//` by a positive constant need no check of the divisor;
- a method declared `-> int` of a record of a known type returns an int
  (its frame checks the value), and a function whose every `return`
  gives an int it knows needs no check of the value it returns;
- the statements of the top level never run inside `try within`, so the
  time limit (D58) is not checked between them.

Release builds are compiled as before otherwise. What a program prints
does not change.

## The program policy (section 9)

**D142. Capability-based permissions: the program `policy`.** Section 9
gives the block, the rule that a release build does nothing the policy
does not allow, that development mode allows everything except `shell`
and reports what is undeclared, and `nexo policy show` and `suggest`; it
names `read`, `write`, `net`, `env` and `shell`. Both phases implement it
with the same output (tests/test_policy.py, the `s09_policy*` and
`modules_policy*` conformance cases):

- The block is `policy:` at the top level of the program's file, with one
  rule per line: `allow` or `deny`, a kind, and the texts it applies to,
  separated by commas (`allow net "api.company.com", "*.openai.com"`);
  `shell` takes no text. Section 18 says code from another language is
  "subject to the policy", and in-process native code cannot be confined,
  so a program with a policy also has to list it: `allow c "libz"` and
  `allow python "numpy"`, with the name `use` gives. Texts are literals
  (no `{...}`), so the policy is known without running the program. The
  parser reads the block into `__policy([[rule, kind, target...], ...])`
  and puts it first, so it holds from the first statement on, wherever it
  is written. A second block, a block inside another one, and a block in
  a module (D124) are errors: modules follow the program's policy, which
  is how "dependencies inherit the policy".
- A path rule is the file or folder it names and everything inside it
  (`"data"` and `"data/"` are the same). Both the rule and the path used
  are made absolute and read one component at a time, as the system
  does: a symbolic link is replaced by its target before the `..` that
  follows it, so neither `data/../x`, a link in `data/`, nor
  `data/link/../x` reaches outside. A `net` rule is a host name, or `*.domain` for
  any name below it; ports are not part of it. An `env`, `c` or `python`
  rule is the exact name.
- What is checked, before it happens: `read`, `read_lines` and `render`
  (read); `write`, `save` and `privacy.export` (write); `move_file` (write,
  on the file, the folder and the file it becomes); `encrypt_file`, `decrypt_file` and
  `connect` to a database file (read and write); every request of `http`
  and of `ask`, at every redirect (net); the value of a `secret` when it
  is used (env); `run` and `build_image` of nexo.ci (shell); `use c` and
  `use python`. What the runtime itself keeps (`.nexo/` files, the local
  broker of nexo.stream, the AI configuration, the variables Nexo reads
  such as `PORT` for a server) and what a server receives are not
  accesses of the program. `fake` replaces the call, so it needs no rule.
- A `deny` rule always stops the program; a `deny` wins over an `allow`.
  Without a rule, a release build (`nexo build --release`, phase 1) stops
  the program, and development mode (`nexo app.nexo`, `nexo test`, and
  every program of phase 0) lets it happen and warns once per kind and
  target, in the section 14 format with the `Warning` header, with the
  rule to copy (`Add allow read "x.csv" to the policy block.`). A shell
  command needs `allow shell` in every mode. Being stopped is a
  diagnostic (D17-D24), not an error value: `try` cannot catch it, so a
  program cannot probe what it may do.
- Without a policy, development mode allows everything except shell
  commands, and warns about nothing: there is nothing to copy into (`nexo
  policy suggest` writes the whole block). A pipeline that runs commands
  therefore needs `policy:` with `allow shell`. `nexo build --release`
  without a policy fails.
- `nexo policy show [file]` lists, for each kind, what the policy allows
  and denies, read from the file without running it. `nexo policy suggest
  [file]` runs `nexo test` on the file with every access allowed and
  recorded (`NEXO_POLICY_RECORD`), and prints the smallest block that
  allows exactly the files, hosts and names used, sorted, one line per
  kind; a run that uses nothing gives `deny shell` (a block needs a rule).
  It needs `test` blocks that pass. Both read the file given, or the only
  `.nexo` file of the directory with that block.
- Per-function permissions, `require` rules and package permissions
  checked by `nexo add` need the package registry, which does not exist
  (D133); a function has the permissions of its program.

**D143. `single`.** Section 10 shows `single config:` with `key = value`
lines and nothing else; section 21 calls what it holds "state". It is
read like the other module blocks (D90) into `config = __single("config",
port: 8080, ...)`. The value has exactly the fields written: `config.port`
reads one and `config.port = 9090` changes it everywhere, since there is
only one; a field it does not have is an error with "Did you mean...?",
as for a record. The values are evaluated where the block is written
(D29). It prints like a record, `config(port: 9090, mode: "production")`.
A field keeps whatever value is given to it (there is no type to conform
to), and it can be changed from any task: each read and change is whole,
under a lock, so it is not a data race (D80). Only the top level of a
file may hold one (`single_place`): inside a function it would be made
again on each call, which is not a single value.

**D144. `states`.** Section 10 writes the transitions as chains, `new ->
paid -> shipped -> delivered`, one or more per line, and `order.go("paid")`.
- The block becomes `order = __states("order", [["new", "paid"], ...])`,
  one pair for each arrow. The first state written is where it starts.
  A line with a single state and no arrow is a syntax error.
- `order.state` is the current state (a text), `order.go("x")` moves to
  it, and `order.can("x")` says whether `go` would. A name that is not a
  state is a diagnostic with "Did you mean...?" (it is a mistake in the
  code); a transition the block does not declare is an error value,
  `InvalidValue`, whose message names the states that can follow, so a
  program can handle it with `try`. Staying in the same state is a
  transition like any other: it must be declared.
- The value is shared, like a database (D92): `go` changes it for every
  name it is bound to and every task, under a lock. It prints as
  `states(order)`.
- `on enter`/`on leave` actions and persisted states are not in the
  specification and are not added.

**D145. `protect`.** Section 10 writes `protect(payments_api, fails: 5,
wait: 1m):` and a block; section 12 names it the circuit breaker.
- It takes the value it protects and both `fails:` (an int from 1) and
  `wait:` (a duration); anything else is `protect_args`. Like
  `db.transaction` (D90) it is rewritten into a call before a `try`: the
  block runs normally, an error that leaves it counts as a failure and
  goes on to the caller unchanged, and the end of the block without an
  error closes the circuit. `return` inside the block is a compile error
  (`protect_return`), as in a transaction.
- After `fails` failures in a row the circuit is open for `wait`: the
  block does not run and the statement fails with a new error kind,
  `CircuitOpen` (`"payments_api" is not called for 1m: it failed 5 times
  in a row`). After `wait` the block runs once more: success closes the
  circuit, a failure opens it for another `wait`.
- The circuit belongs to the value protected, compared by the text it
  prints as, so every `protect` of the same value shares it, in every
  function and task; the name in the message is the expression as written.
- `fallback` and `calls` blocks of section 12 belong to services and
  deployments (excluded with the rest of resilience, D113).

**D146. `nexo mcp`.** Section 26 lists the tools `check`, `run_tests`,
`explain`, `docs_for_ai`, `plan` and `format`, and says the server never
exposes `apply`, `deploy` or secrets.
- It speaks the Model Context Protocol on standard input and output: one
  JSON-RPC 2.0 message per line, with `initialize`, `ping`, `tools/list`
  and `tools/call`; notifications get no answer and anything else is the
  JSON-RPC error for it, with the section 14 sentence on how to fix it.
- Each tool runs the binary's own command on a path of the project (the
  folder `nexo mcp` started in): `check` is `nexo check`, `run_tests` is
  `nexo test`, `docs_for_ai` is `nexo docs --for-ai`, `plan` is `nexo
  plan [--stage s]`, and `format` is `nexo fmt` on one file (it rewrites
  it, keeping its tokens, D85). The answer is the text the command
  printed, standard output and errors together, and `isError` when it
  ended with an error. So the tools say exactly what the commands say,
  in both phases.
- A path must be relative and stay inside the project once `..` and
  links are followed; arguments are texts, and an unknown one is an
  error, so an assistant cannot pass options to the commands.
- `explain` is left out: `nexo explain` (section 16) does not exist in
  this edition, and an assistant explains code on its own from
  `docs_for_ai`. `apply`, `destroy`, `ci run` and secrets are not tools.

**D147. `nexo docs`.** Section 14 says it "generates documentation from
`##` comments" and gives no format. `nexo docs [paths]` prints Markdown
(it can be saved with `> API.md`): for each file, in the order of `nexo
test`, a `# path` title and, for each function, type and trait at its top
level, a heading with its first line, the `##` lines above it as a
paragraph and the fields and methods of a type as a list. These are the
same declarations `nexo docs --for-ai` lists (D130). A file that declares
nothing is left out; when no file declares anything it says so. A site
in HTML, the documentation of the standard library and links between
pages are not added.

**D148. `nexo new`.** Section 14: "creates a project with `nexo.toml`, a
`src/` folder and a test". `nexo new name` makes the folder `name` with
`nexo.toml` (the `[project]` table of section 13: the name, version
`0.1.0` and edition `0.1`) and `src/main.nexo`, a documented function, a
`test` of it and a line that uses it, formatted and lint-clean. The name
is letters, digits, `_` and `-`, starting with a letter, so it is a
folder of the current one and a package name of section 13; a folder that
exists already is never written into. There is no `nexo.lock` until there
are dependencies, and no dependencies until the registry (D133).

**D149. Release builds leave out what the program does not reach.** The
runtime is one static library with every module of the library, and a
program uses a few of them. `nexo build --release` links with
`--gc-sections`, so the parts no code reaches are not in the file: the
program of one `print` goes from 19.5 MB to 15.4 MB and starts about
0.4 ms sooner (2.8 to 2.4 ms on the machine of goals.json, a noisy run),
and two builds are still the same byte for byte (D128). Development
builds do not, since linking would take longer and they are run once.

**D150. `action` and `undo()`.** Section 10 shows `action trash(path):`
with a `do:` line and an `undo:` line, then `trash("photo.jpg")` and
`undo()`, and gives no more.
- An action has its parameters, as a function, and exactly `do:` and
  then `undo:`; each is a statement on its line or an indented block
  (`action_parts` otherwise). It goes at the top level of a file
  (`action_place`), like `single`, and `return` inside it is a compile
  error (`action_return`): an action gives nothing back, so `undo` has
  nothing to answer for.
- It is read into two functions: `name(params)`, which runs `do` and
  then records the action with its arguments, and `__undo_name(params)`
  with the `undo` part. Each part runs inside a `try` that passes its
  error on, as a workflow step: the calls in it need no `?` (the example
  of section 10 writes `move_file` without one), and so an action and
  `undo()` can always fail (`T!`). An action whose `do` failed is not
  recorded.
- `undo()` reverses the last action done and not undone, with the
  arguments it was called with, and gives `true`; with none left it
  gives `false` (nothing to undo is not a mistake). An undo that fails
  stays the last action, so `undo()` can try it again. The history is one
  for the program, under a lock, and has no limit. Redo is not in the
  specification and is not added; nor is `pub action`.

**D151. `workflow`.** Section 10: "A `workflow` saves its state after every
step and resumes after a restart or a failure. If a step fails
permanently, the `undo` of every completed step runs in reverse order",
with `step` lines, `undo` on the line after a step, and `step wait
email.confirmed(email), max: 3 days`.
- A workflow has parameters and is called like a function; it goes at
  the top level of a file (`workflow_place`). A step is an expression,
  run inside a `try` (no `?`, as in D150), and its value is not kept. A
  step may have one `undo` expression, indented on the next line.
  `step wait cond, max: d` needs both (`workflow_wait`): a wait with no
  end would hold a run forever. The condition is checked at once and
  then every second until it is true; after `max` the step fails with
  `Timeout` (`The step "wait confirmed(email)" of "customer_signup" was
  not true within 3 days`).
- "Fails permanently" is an error value of a step: there is no retry of
  its own, since `@retry` (section 10) already retries a function. Then
  the `undo` of each step done runs, last first, and the call fails with
  the step's error (a workflow is `T!`).
- The state of a run is the number of steps done, the end of the wait
  under way and, while its steps are undone, the error that stopped it.
  It is saved after every change in `.nexo/workflows/<name>-<hash>.json`
  (written to a new file put in place of the old one, so a stop during a
  save keeps the last state)
  under the folder the program runs in (like the state of `nexo.infra`
  and `nexo.ci`), where the hash is of the name and the arguments as they
  print, so a run is one workflow called with the same arguments. It is
  written by the runtime, not by the program, so the policy (D142) does
  not apply to it, and both phases read each other's files.
- "Resumes after a restart": a call whose file exists starts after the
  last step saved; the end of a wait is kept, so the 3 days count from
  the first try. A step that was running when the program stopped runs
  again (at least once). An `undo` that fails stops the undoing with its
  own error and keeps the file, so the next call goes on undoing from it
  and then fails with the step's error. When the run ends, in success or
  failure, its file is removed; a file that cannot be removed is an
  error, since the next call would skip every step.
- Step results passed to later steps, signals, parallel steps and a
  list of runs are not in the specification and are not added.

**D152. Nexo Shield phase 1: application enforcement and honest isolation.**
Shield reuses D142 policy syntax, matching and diagnostics through a typed Rust
engine and a matching Python adapter. `security check|run|trace|audit file.nexo`
requires a policy; check/audit reuse the current static checks, policy display
and unsafe inventory. Run/trace enforce production decisions in development
builds. Native C, Python and shell integrations are rejected in Shield execution:
there is no OS confinement and granting permission cannot provide it. Ordinary
D142 development/release behavior remains compatible. Internal `__policy` is
not a user-callable API, and policy initialization is one-shot. The development
recorder cannot override release/Shield decisions (fixing two escalation paths).
Trace emits local JSON with all targets redacted; existing diagnostics can
contain source snippets and are not sanitized audit events. No OS sandbox,
resource limits, IP/DNS confinement, per-package attenuation or complete taint
tracking is claimed. `docs/shield.md` records the trust boundaries and roadmap.

Shield also rejects literal backslashes and ASCII control characters in HTTP
URLs before authorization in both backends. A verified parser differential
allowed a Rust request to reach `127.0.0.1` via
`http://127.0.0.1:PORT\@allowed.example/` under a grant for `allowed.example`.
Rejecting these ambiguous URLs, including redirect destinations, fixes the
bypass without changing hostname rule syntax; encode such data explicitly.


**D153. Shield phase 2: explicit offline workers and synchronous ceilings.**
`security probe` checks actual kernel facilities. `security run|trace
--sandbox=linux` requires Linux x86_64, Landlock ABI >=3, openat2, seccomp and a
non-root dedicated worker; unavailable guarantees stop before target execution.
The kernel profile is offline and single-process, with pinned filesystem grants,
capability dropping, descriptor sealing and hard CPU/address-space/file/FD limits
plus a supervising wall deadline. Network grants and filesystem deny exceptions
that the profile cannot faithfully represent are rejected. The native `security
worker` interface executes an independent constrained program; in-process FFI
remains disallowed and there is no implicit typed RPC bridge.
`--dependencies file.json` adds immutable version-1 file-module ceilings that
intersect lexical and dynamic caller permissions. Scope guards are emitted only
for context-enabled compilation and restored on returns and failures; an
uninstrumented binary refuses a requested context. Async constructs are rejected
under this profile until contexts can follow queued execution. Dependency returns
are limited to bounded plain data; callable, deferred and opaque handles are
rejected rather than exported without sealed contexts. There is no new
policy syntax or package-registry claim. `docs/shield-isolation.md` specifies the
trust boundary, unsafe kernel ABI module, exact limits and unsupported features.

**D154. Shield Docker execution and validation.** Production packages the native
compiler, static runtime and worker in a digest-pinned Debian image. A mandatory
entrypoint checks the actual non-root, read-only, capability, seccomp, network
and cgroup posture before execution. The host helper mounts disjoint source and
output directories with explicit permissions; no Docker socket is exposed.
Docker isolation complements API authorization; the explicit Linux worker mode
still requires Landlock and never falls back. A separate validation image runs
release Rust harnesses and both backends' security suites with the production
binaries. CI requires positive kernel support, not merely skipped kernel tests.
See `docs/shield-docker.md` for commands and boundaries.

Shield also rejects persistent workflows before state loading or step execution.
D151 exempts runtime workflow state from ordinary application policy; accepting
it under Shield would bypass API filesystem authorization. This restriction
also applies when write grants are present, until those state effects can be
authorized throughout the active dependency context. Ordinary workflows retain
their existing behavior.

**D155. `nexo test --coverage`.** "Goals and guarantees" measures "Tests
proposed automatically" by "branch coverage before and after", and no
section names a coverage tool, so it is an option of `nexo test`, as
`--suggest` is (D131): `nexo test --coverage [paths]` runs the same
tests and, after their summary, prints `Branch coverage: 6 of 10 (60%).`
(rounded down), each branch that never ran as `file:line: what.`, by
file and line, and a sentence on how to cover them. The exit code is
that of `nexo test`: coverage is a measure, not a rule.
- A branch is a block that runs on some paths only, as the developer
  wrote it: the body of each function and method (`the function
  "Shape.area" never ran`, on the line of its name), each branch of an
  `if`, `elif` and `else`, the path an `if` without `else` takes when no
  condition holds (on the line of the `if`), each arm of a `match`, each
  loop body (`for`, `while`, `repeat`) and each `else` of a `try`; the
  `undo` part of an action is its own branch. The others are on the line
  of their first statement.
- Not counted: `test` blocks (they are what runs the code), lambdas,
  `parallel for` and `on`/`when` blocks (code inside expressions and
  events), and the code the parser writes for module blocks and patterns
  (functions whose names start with `__`, the `try` of `protect`,
  transactions, actions and workflows, the steps of a workflow).
  Function bodies count, so a function with no `if` that no test calls
  is still found.
- Both parsers' programs get a call `__cover(n)` first in each branch
  (after the `expect`s and `ensure`s that open it) before they run or
  are compiled, so both phases count the same branches; the modules a
  file uses are counted too. A file whose tests do not run (it does not
  load, or has no test) adds nothing. With several files, a branch
  counts once and is covered if any file's tests ran it.
- Statement and line coverage, reports in other formats and a minimum
  percentage are not in the specification and are not added.

**D156. Programs that start sooner and calls that check less.** Two
changes to compiled programs, neither of which shows in their output:
- They are linked without position-independent code (`-fno-pic`,
  `-no-pie`): the runtime's tables need no relocation when the program
  starts (20,000 of them), and the code reads the runtime's globals
  directly. A program that prints one line starts in about 1.7 ms instead
  of 2.5 ms. The program still runs on a thread with a large stack (D73):
  running it on the main thread, with a stack of its own, started 0.25 ms
  sooner but made programs that create many objects about 15% slower.
- An int-only function (D84) checked the depth of calls and, when some
  thread had a `try within`, the clock, at every call. The thread state
  now holds a limit that is the largest depth, or 0 while a `try within`
  runs, so one comparison does both, and the slow path tells them apart.
  fib(32) runs about 20% faster.

**D157. Records that never leave their function are kept as their
fields.** Section 7 says the compiler moves a value when it proves a
single owner; the same proof allows more for small records. A variable
whose type is defined once at the top level with only `int` fields,
which is only given values by `name = T(a, b)` (arguments certainly
ints that do not read it), only read as `name.field` and used to call
methods that read `self` only through its fields, take ints and return
an int, and that no other function can see, is replaced by one int per
field; each of those methods becomes a top-level function that takes the
fields (compiler/nexoc/src/unbox.rs). Every argument is evaluated before
any field changes, as the record is built before it is assigned, so a
failure in a later argument leaves the fields as they were; a method
call that a test fakes (`fake p.norm returns 99`) keeps its record,
since fakes are found by the text of the call. Such a record can never be
printed, compared, stored or shared, so nothing can tell the difference,
and every expression keeps its span, so errors read the same. Any other
record is an object as before. The `records` benchmark builds such a
record in a loop: it went from 2.5× Rust's time to about 0.4× (the Rust
program boxes its value, as a record that escapes would need).

**D158. Compiling 100,000 lines in under 10 s.** Three changes to how a
development build is made, none of which changes the program it gives:
- Registers, labels and stack slots are numbered again from 1 in each
  function, so their names stay short: the IR of the program of
  `compiler/bench/goals.py` went from 121 MB to 102 MB, and both the
  compiler (which writes it) and clang (which reads it) take less time.
- The parts compiled at the same time (D135) are balanced by an
  estimate of clang's work rather than by bytes: the first part also
  reads the program's data, and a very large function (the top level of
  a big program) costs clang more per line than many small ones.
- The parts are linked with lld when it is installed (`ld.lld` on the
  PATH); otherwise with the system's linker, as before. Linking went
  from about 1.1 s to 0.3 s. Release builds keep the system's linker, so
  they stay the same byte for byte (D128).
The program of goals.py now compiles in about 9.5 s on the 4 cores of
goals.json (it took 13.9 s).

**D159. An int-only function returns its int.** The body of a function
whose parameters and result are all `int` (D84) returned a value of two
words, and its caller checked the tag of the first to know whether it
had failed. It now returns the int itself, or 0 when it fails, and the
caller looks at the failure flag of the thread state, which the failure
already set: one comparison with memory after each call, where there
were six instructions. fib(32) went from 1.9× to about 1.55× Rust's
time. The usual entry of such a function (called through a value, with
named arguments...) gives back the two words as before.

**D160. Fused pipelines over ints.** In a fused `where`/`map`/`sum` loop
(D134), each step was a function called with the element as a value of
two words, whose code checked its type at every operation even when the
list holds ints (D137). A step that captures nothing is now also
compiled to take the element as an int, like an int-only function
(D84): the loop calls it when the element is an int and the usual one
otherwise, and LLVM folds the checks away. A step that is an operator or
a literal no longer goes through the runtime to see whether it named a
function (D51), and `sum` adds ints in the loop itself while the total
is an int (an overflow and any other value go to the runtime as
before). The `lists` benchmark went from 2.1× to about 1.6× Rust's time.

**D161. More pipeline steps in SQL.** Section 6 says queries "use the
same pipeline; they are translated to SQL", and D100 translated `where`,
`sort by`, `take` and `count`. Two more steps now run in the database
when the result cannot differ from memory:
- `pick a, b` of columns of the table reads only those columns; the rows
  are maps, as in memory. After it only `take` and `count` stay in SQL
  (a `where` or `sort by` on the picked rows runs in memory).
- `group by column` on a query whose steps so far are `where` only, of a
  table with an `id`, by a column of type int, str, bool, date, time or
  an enum without data (not `@private`), followed by `count`, `sum
  column` or `avg column`, is one `SELECT key, COUNT(*) ... GROUP BY key
  ORDER BY MIN(id)`: groups come in the order their first row comes, as
  in memory. `sum` and `avg` are of a required `int` column; the
  statement also asks for its `MIN` and `MAX`, and when a group's count
  times the largest absolute value does not fit in an int (so some order
  of adding them could overflow) the rows are read and added in memory,
  which gives the Overflow error of memory. `avg` is the sum divided by
  the count as `/` divides them. Float sums stay in memory, since SQLite
  adds reals with compensated summation and the last digit could
  differ, and so do decimal and money (stored as text).
- `group by` with nothing after it reads nothing; any other step after
  it reads the rows and groups them in memory, as before.

**D162. Relations in SQL.** Section 6 has `ref` and `many` fields and
queries "translated to SQL"; D100 read the row of each `ref` field with
its own statement, and ran a `where` that reads a field through a `ref`
in memory.
- The rows a query reads are read first; then the rows their `ref`
  columns point to are read with one `SELECT ... WHERE id IN (...)` per
  column (500 ids at most per statement, in the order they first
  appear), and those rows' own refs the same way. A ref whose row is
  missing or hidden (D116) is none, as before. A ref to a row the same
  statement reads is that row's record, so rows that point to each other
  (`boss: ref Person?`) are read once, where before reading them never
  ended; the records then form a cycle through a type that refers to
  itself, which is freed when the program ends (D79).
- In a `where` on a query, `ref.field` (a bare ref column, then a field)
  is a column of the table the ref points to: a comparison with a value,
  `text in ref.field` or a bare bool `ref.field` becomes `ref_id IN
  (SELECT id FROM target WHERE ...)`, with the rules of D100 for the
  field. Only for a required ref (`ref T`, not `ref T?`, whose none
  would be an error in memory) to a table with an `id` and without
  personal data (whose hidden rows would be an error in memory too);
  otherwise, and when comparing two columns, it runs in memory.
  The table the ref points to is made (synchronised) before the query
  runs, so a query on a new database finds nothing instead of failing.
- `many` fields were already a query (`customer_id = ?`), so
  `ana.orders.count()` and the pipelines on it run in SQL as any query.
  `group by` a ref stays an error, as in memory (a record is not a key).

**D163. PostgreSQL.** Section 6 connects to a server with
`connect("postgres://prod/shop", key: secret("DB_PASS"))`; D100 left
servers for later. Both phases now speak PostgreSQL's protocol (version
3) themselves, with no driver library (nexo/stdlib/pg.py,
compiler/runtime/src/pg.rs):
- The address is `postgres://user@host:port/database` (or
  `postgresql://`); the user is `postgres` when there is none, the port
  5432 and the database the user's name. A password inside the address is
  a DatabaseError: it goes in `key:` (a text or a secret), as section 6
  writes it, so it is never in the code (section 9). The same key encrypts
  the `@private` columns (D116), as the key of an SQLite file does.
  Authentication is SCRAM-SHA-256 (the default of PostgreSQL since 14)
  or a cleartext password; MD5 is refused as unsupported. The program
  policy checks `net` to the host (D142).
- TLS is always asked for, and its certificate is checked against the
  system's authorities (`SSL_CERT_FILE` adds one, in both phases) and the
  host name. A server that does not offer TLS is accepted only on this
  machine (localhost, 127.x, ::1). Failures read the same in both phases:
  "the certificate is not trusted", "the TLS handshake failed", "cannot
  reach host:port: Connection refused".
- The tables and queries are those of SQLite, with PostgreSQL's words
  where they differ: `id` is `BIGINT GENERATED BY DEFAULT AS IDENTITY`
  and `add` reads it with `RETURNING`; ints are `BIGINT`, floats `DOUBLE
  PRECISION`, bools `BOOLEAN` (`= TRUE` for a bare bool column), the
  other kinds `TEXT COLLATE "C"`, so texts compare and sort byte by byte
  as in SQLite and in memory; `text in column` uses `strpos`; `sort by`
  puts none first (`NULLS FIRST`, and `NULLS LAST` with `desc`) as SQLite
  does; parameters are `$1, $2...` (the SQL log shows them so); a
  repeated `@unique` value is read from the server's detail; tables and
  columns are found in `information_schema`; the rows marked by privacy
  are kept with `ON CONFLICT ... DO UPDATE`. Floats travel as the
  shortest text that reads back the same (`extra_float_digits` 3).
- Inside `db.transaction:` each statement runs in a savepoint of its own,
  so a statement that fails (and is handled with `try`) leaves the
  transaction as it was, as in SQLite; PostgreSQL would otherwise refuse
  every later statement of the transaction.
- `group by` sums (D161) need no overflow check from the server, which
  adds bigints as numeric; the check of D161 still decides.
- `nexo db diff|migrate` take the server from the program's first
  `connect("postgres://...", key: secret("NAME"))` and the password from
  the variable NAME; removed columns are `ALTER TABLE ... DROP COLUMN`.
- The tests start a PostgreSQL server with TLS and a certificate
  authority of their own (tests/test_postgres.py, skipped when PostgreSQL
  is not installed), run the db conformance programs on it with the
  output of SQLite, and compare the SQL both phases send.

**D164. `ask(..., stream: true)` as the answer arrives.** D107 returned
the list of pieces once the answer was complete. Now:
- `ask` reads until the first piece of text has arrived and returns a
  lazy list of the pieces, read from the open connection as the program
  asks for them: `for chunk in ask(..., stream: true):` runs its block for
  each piece when it arrives. Before waiting for the next piece, what the
  program has written is flushed, so `print(chunk, end: "")` shows at
  once. Like any lazy value (section 6, rule 4), an assignment reads the
  whole answer first; looping over the same answer again gives the
  pieces already read, without asking again.
- What happens before the first piece is as before: retries, the next
  model of `fallback`, an answer with no text (AIError). After it, a
  failure ends the loop with its error: NetworkError when the connection
  is lost, AIError for a line that is not valid. The line of usage.csv is
  written when the answer ends.

**D165. AI budgets.** Section 8 shows `budget = 5 EUR per day` and
`when_over_budget = local` in the `ai` block, and `nexo ai usage`
showing "spend per model and per day"; the prices of the models are not
written, and D107 left them out because they change.
- Prices are configuration, never built in: an `[ai.prices]` table in
  nexo.toml or the user's ai.toml gives each model (as `"provider:model"`)
  the price of a million input tokens and of a million output tokens,
  `"openai:gpt-mini" = ["0.15 EUR", "0.60 EUR"]`, both in one currency.
  ollama's models run on this machine and cost nothing.
- `budget` is an amount of money `per day` or `per month` (in a toml
  file, the text `"5 EUR per day"`); it is read key by key like the other
  keys (D106), so a profile can have its own. The spend of the period is
  that of the lines of usage.csv (D107) of this day or calendar month, at
  the prices now known; usage.csv is the user's, so the budget counts
  every program of the user.
- With a budget, every model a call may use (its model, the fallback and
  `when_over_budget`) needs a price in the budget's currency: a model
  without one, or with prices in another currency (section 17, rule 3:
  currencies are not added), is an AIError that says how to add it.
- Before each `ask` and when an agent's `handle` starts, a spend that has
  reached the budget sends the call to `when_over_budget` (an alias or
  `provider:model`, without fallback), or, when there is none, fails
  with AIError "The AI budget of 5.00 EUR per day is spent (5.02 EUR so
  far)". A call that starts under the budget is not stopped halfway.
- `nexo ai usage` adds the spend of each day and model whose price is
  known, in whole cents (half to even).

**D166. MCP servers as tools of an agent.** Section 8 says "any MCP
server is connected with `tools = [mcp("name")]`"; where the server is
and how it is reached is not written.
- A server is declared in nexo.toml (or the user's ai.toml, D106) as a
  table `[mcp.NAME]` with `command = ["program", "argument", ...]`; it
  speaks the Model Context Protocol on its standard input and output
  (one JSON-RPC message per line, as `nexo mcp` does, D146). Servers over
  the network are not part of this edition. `mcp("NAME")` is a value
  that names it; the same name is the same server in the whole program.
- The server starts the first time an agent's `handle` needs it, in the
  program's folder, with its errors discarded: `initialize`, then
  `tools/list` (every page). It stays for the rest of the program.
  Starting it is running a command, so the program policy checks `shell`
  with the command (D142): a program that uses one needs `allow shell`.
- Its tools join the agent's @tool functions, described to the model
  with the server's own name, description and input schema. A call goes
  to the server as `tools/call`; the model gets the text of the answer,
  marked as an error when the server says so. Two tools with one name
  are an AIError. `confirm = [mcp("NAME")]` asks before each call of a
  tool of that server, showing its arguments.
- A server that cannot be started, closes its output, answers with an
  error or with something that is not an answer, or does not answer in
  60 seconds is an AIError that names it; a name without a table is an
  AIError that says how to add it. An agent's tools inherit the program's
  policy inside the program; a server is another program, with its own.

**D167. Partitions of a stream.** Section 19 says `when` on a stream
"scales with implicit `parallel` per partition, preserving order within
each one"; how a stream gets partitions and which one a message goes to
is not written, and D117 had one partition per stream.
- A source can say how many partitions its stream has:
  `kafka("orders", partitions: 4)`. The number is fixed when the stream is
  created (in the local broker, the file `<name>.partitions`; a stream
  that exists with one log has one); a source that asks for another
  number is an error that names both. Without it a stream has the
  partitions it was created with, or one. The local broker keeps one log
  per partition (`<name>.<p>.log`) and one offset per partition and
  consumer group; a stream with one partition keeps the files of D117.
- `send(value, key: k)` sends a message with a key (a text as it is,
  another value as compact JSON); the partition is Kafka's own:
  murmur2 of the key, as positive, modulo the partitions, so a key keeps
  its partition and its order, and a Kafka client elsewhere would choose
  the same one. Without a key the data is hashed instead. A message has
  `msg.key` (none without one) and `msg.partition`; `msg.id` is its
  position in its partition. The `key:` of `send`, like any `key:`, is
  never a text written in the code (section 9): it comes from the data.
- A consumer reads the new messages of each partition and, for a stream
  with several, handles them as `parallel for` handles its elements (D52,
  D80): one task per partition, each in order, on threads in phase 1 and
  one after the other in phase 0; what the tasks write (dead-letter lines
  too) comes out in partition order, and the race rules of the tasks
  apply to the handlers. Then the pipelines of the stream get the same
  messages, partition by partition, and the offsets are saved. A stream
  with one partition is handled as before, message by message.
- With `key:` on `when`, a key being handled in one partition makes the
  same key wait in another, so it is still processed once.
- Under `nexo test` the partitions are those the source asks for, and
  `send` delivers at once, as before.

**D168. Streams on the clouds' services.** Section 19 lists `sqs`,
`kinesis`, `servicebus`, `eventhub` and `pubsub`; D117 ran every source
on the local broker. A source is now on its cloud when it has that
cloud's option, and on the local broker otherwise: `sqs("orders",
region: "eu-west-1")` and `kinesis(..., region:)` on AWS,
`servicebus("orders", namespace: "shop")` on Azure, `pubsub("orders",
project: "shop")` on Google Cloud. `eventhub` reads only through AMQP or
Kafka's protocol, so it comes with the brokers on the network (D169).
- Both phases talk to each service's own HTTP API with the credentials
  and addresses of nexo.infra (D120): SQS's JSON protocol and Kinesis
  signed with SigV4 (AWS_ENDPOINT_URL points both elsewhere), Service Bus
  through REST with an Azure AD token (NEXO_AZURE_ENDPOINT puts its data
  plane at ENDPOINT/HOST/...), Pub/Sub through REST
  (PUBSUB_EMULATOR_HOST, or NEXO_GCP_ENDPOINT as for infra). The queues,
  streams and topics are not created: an infra block creates them (its
  `queue`), and a missing one is an HTTPError that names the source and
  what the cloud says.
- A queue (SQS, Service Bus, Pub/Sub) has one partition. A consumer
  leases what it reads (SQS's visibility, Service Bus's peek-lock,
  Pub/Sub's deadline, 60 seconds) and deletes each message once its
  handlers ended, or once it went to the dead letters. Pub/Sub is read
  through the subscription `<topic>.<group>` of the consumer group, made
  when it is missing (it gets what is published after it exists). A
  message that a window holds is deleted too: a queue keeps no position
  to read it again after a stop.
- A Kinesis stream's shards are its partitions, handled as D167 says;
  the consumer group saves the sequence number it reached in each one in
  .nexo/streams/kinesis, kept before the messages of an open window as
  D117 does for the local broker.
- `send(value, key:, at:)` keeps the time and the key in the message's
  attributes (`nexo-time`, `nexo-key`) where the service has them; a
  Kinesis record has its partition key (the key, or a hash of the data)
  and the time it arrived. A FIFO SQS queue (`.fifo`) gets the key as
  its message group. `msg.id` is the service's id: a text for SQS,
  Kinesis and Pub/Sub, Service Bus's sequence number.
- The dead letters go to `<name>.dead` on the same service, except on
  SQS, whose names cannot have a dot: `<name>-dead`. The line on the
  error output names it. A dead letter that cannot be sent stops the
  consumer with its error, leaving the message to come back.
- The policy checks `net` to the service's host when a program sends
  and when a consumer starts (D142). Under `nexo test` every source is
  on the in-memory broker, as before.
- tests/cloud_emulator.py answers these data planes, and
  tests/test_stream_cloud.py runs producers and consumers of each in
  both phases.

**D169. Streams on brokers on the network.** D113 left out brokers on
the network; section 19 writes `kafka("orders", servers: ...)` and lists
`rabbit`, `eventhub` and `websocket`. A source is now on a broker on the
network when it has its option, and on the local broker otherwise:
`kafka("orders", servers: "user@host:9092,host2:9092")`, `rabbit("orders",
url: "amqp://user@host/")`, `eventhub("clicks", namespace: "shop")`, and
`websocket("wss://host/path")`, whose source is the address itself.
- Both phases speak each protocol themselves, with no library: Kafka's
  binary protocol (the requests of the versions that Kafka 1.0 to 4.x
  answer, record batches of format 2), AMQP 0-9-1 with publisher
  confirms, WebSocket (RFC 6455). Compressed Kafka batches are not read:
  one is a NetworkError that names its codec. The specification asks for
  no codec, and the producers of Nexo do not compress.
- A broker on another machine is reached with TLS only (`amqps://`,
  `wss://`, Kafka on any host but this machine), checked against the
  system's certificates; plain sockets only to this machine.
- `key:` is the broker's password, a text or a `secret` (Kafka's SASL
  PLAIN with the user of `servers:`, RabbitMQ's PLAIN with the user of
  the url); `key:` without the broker's option is an unknown parameter.
  Event Hubs is read through its Kafka endpoint
  (`<namespace>.servicebus.windows.net:9093`, or NEXO_EVENTHUB_SERVERS)
  with a token of Azure AD (OAUTHBEARER), the credentials of D120.
- A broker says how many partitions a topic has, so `partitions:` goes
  with the local broker only. Kafka's partitions are handled as D167
  says; `send` picks the partition with Kafka's own hash of the key. The
  consumer group (the program's file name) commits the offset after the
  last message handled (before an open window, as D117), and a new group
  starts from the first message. One consumer of a group reads every
  partition: Nexo does not join Kafka's group protocol, so two consumers
  of one group would each read everything.
- RabbitMQ: a queue has one partition. A consumer takes messages with
  Basic.Get and acknowledges each once its handlers ended or once it went
  to the dead letters; a message not acknowledged comes back when the
  connection closes (at least once). The time and the key are the headers
  `nexo-time` and `nexo-key`, as D168's attributes. A connection the
  broker closed while it was idle is made again once.
- WebSocket: each text or binary message the server sends is a message
  (`msg.id` counts them from 1); `send` writes a text message on the same
  connection. A WebSocket keeps nothing, so a consumer saves no position
  and its dead letters go to the local broker, to the stream named by
  the address (`host_port_path.dead`).
- The dead letters of Kafka, Event Hubs and RabbitMQ go to `<name>.dead`
  on the same broker. A failure of a broker is a NetworkError that names
  the source and the reason (`err.NetworkError.broker`); a consumer that
  cannot reach its broker tries again every second.
- The policy checks `net` to every host a client connects to (D142):
  each bootstrap server it tries, and the brokers Kafka's metadata names
  (leaders, coordinators). Under `nexo test`
  every source is on the in-memory broker, as before.
- tests/kafka_broker.py, tests/amqp_broker.py and tests/ws_server.py are
  written from each protocol's specification, sharing no code with the
  clients; tests/test_stream_net.py runs producers and consumers on them
  in both phases. A check against a real Kafka is not part of the suite.

**D170. MySQL.** Section 6 has "one way to connect, whether to a local
file or a server"; D163 added PostgreSQL. `connect("mysql://user@host/
shop", key: secret("DB_PASS"))` now opens a MySQL server, with the same
tables, queries, transactions, privacy and migrations. Both phases speak
MySQL's client/server protocol themselves (nexo/stdlib/mysql.py,
compiler/runtime/src/mysql.rs):
- The address is `mysql://user@host:port/database`; the user is `root`
  when there is none, the port 3306, and the database must be named.
  A password in the address is refused, as in D163: it goes in `key:`.
- TLS is asked for whenever the server offers it, and its certificate is
  checked against the host name; a server that does not offer it is
  accepted only on this machine. Logins are caching_sha2_password (MySQL
  8's own; its full login sends the password, so it needs TLS) and
  mysql_native_password.
- Every statement with values is a prepared statement with binary
  parameters, so values never become SQL text. Rows come back as SQLite
  gives them: whole DECIMALs (the sums of `group by`) as ints, which do
  not overflow, as on PostgreSQL.
- Each connection sets `sql_mode` with ANSI_QUOTES, so `"name"` quotes a
  name in the SQL of nexo.db and in `db.sql`, as in SQLite and
  PostgreSQL; PIPES_AS_CONCAT makes `||` join texts; strict mode makes a
  value that does not fit an error; the time zone is UTC.
- Types: int and ref BIGINT, float DOUBLE, bool BOOLEAN, texts LONGTEXT
  with the collation utf8mb4_0900_bin, so that they compare and sort byte
  by byte as in SQLite (MySQL 8.0 or later; MariaDB has no such
  collation). InnoDB indexes 3072 bytes of a column at most, so a
  `@unique` text is a VARCHAR(768): a longer one is a DatabaseError.
  MySQL sorts a text by its first 1024 bytes (max_sort_length). The id
  is `BIGINT AUTO_INCREMENT`.
- MySQL ignores a REFERENCES written on a column: a `ref` is a table
  constraint `fk_<table>_<column>`, and the tables it points to are
  created first. `nexo db diff` drops that constraint before its column.
- MySQL commits a transaction when a table is created or changed, so
  `db.transaction` creates the program's tables before it begins; a
  table so created stays after a rollback (empty). A migration's
  statements are not undone when one fails halfway, for the same reason.
  A failed statement leaves a transaction as it was, as in SQLite, so no
  savepoint per statement is needed (D163 needs one on PostgreSQL).
  Savepoints are released with `RELEASE SAVEPOINT`.
- `null`s sort first ascending and last descending, as in SQLite; `in`
  on a text is `INSTR`, as in SQLite.
- tests/test_mysql.py starts a MySQL server with TLS (skipped when it is
  not installed) and runs the db conformance cases, privacy,
  transactions, migrations and the TLS checks in both phases, which send
  the same SQL.

**D171. Document stores, and MongoDB.** Section 6 has one `connect` for
"a local file or a server" and queries "translated to SQL"; it names no
document store. `connect("mongodb://user@host/shop", key:
secret("DB_PASS"))` now opens MongoDB with the same `@table` types,
queries, relations, transactions and privacy, and the same results as on
SQLite (nexo/stdlib/docdb.py and mongo.py, compiler/runtime/src/docdb.rs
and mongo.rs, with no driver library):
- A table is a collection, a row a document whose `_id` is its key; a
  field that is none is left out of the document. Ids are ints, as on SQL
  databases, from a counter per table in `nexo_counters` (an id a failed
  insert took is not given again). A table without `id: id` gets a
  hidden key `nexo_row`.
- The conditions of a `where` are those SQL runs (where_tree in db.py
  and db.rs, which also renders SQL), in negation normal form, each leaf
  leaving out the rows whose column is none, as SQL's three-valued logic
  does; `sort by`, `take`, `count` and `pick` run in the store too. A
  condition on the table a ref points to, two columns compared, and
  `group by` run in memory, with the same result. `in` on a text is a
  regular expression of the text, its signs escaped.
- `@unique` is a unique index with a partial filter (`$exists`), so
  several rows can have none, as in SQL.
- `db.transaction` keeps a journal of what it wrote and writes it back
  when it fails, nested ones too: a failed statement leaves the
  transaction as it was, as in SQLite. MongoDB's own transactions need a
  replica set and end at the first failed statement, so they are not
  used: other programs can see a transaction's writes before it ends,
  and a program that stops halfway keeps them. This is weaker than an
  SQL transaction, and said here so.
- The address is `mongodb://user@host:port,host2:port/database`
  (27017 by default); with several hosts the client uses the primary.
  `key:` is the password, checked with SCRAM-SHA-256 against the users of
  the `admin` database. A server on another machine is reached with TLS
  only, checked against the system's certificates; `mongodb+srv://` (a
  DNS lookup) is not read: write the hosts.
- `db.sql` is an error on a document store (`db_no_sql`); `nexo db diff`
  and `migrate` say that it keeps no schema (`info.db_no_schema`): its
  collections and unique indexes are made when a program first uses a
  table.
- NEXO_SQL_LOG writes every command (`mongo: {...}`); both phases send
  the same ones. tests/mongo_server.py is a MongoDB written from the wire
  protocol's documentation for the tests (MongoDB is not packaged by the
  system), and tests/test_docdb.py runs the db conformance cases,
  transactions, privacy and errors on it in both phases.

**D172. Elasticsearch.** As D171, `connect("elasticsearch://user@host:9200/
shop", key: secret("ES_PASS"))` keeps the tables of a program in
Elasticsearch, through its REST API (nexo/stdlib/elastic.py,
compiler/runtime/src/elastic.rs):
- A table is an index named `<path>-<table>` (`shop-customer`), or the
  table's name when the address has no path; a row is a document whose
  id is its key. Each index has a strict mapping: int, ref and bool are
  `long`, float `double`, the rest `keyword`, so texts compare and sort
  byte by byte, as in SQLite (a text holds at most 32766 bytes there). A
  field is added to the mapping when its type gets it; a type that
  changes is not.
- `where` is the query DSL (term, range, exists, wildcard, bool), none
  values sort first ascending and last descending as in SQLite, and the
  rows are read in pages of 1000 with search_after.
- Elasticsearch has no unique keys: a `@unique` value is a document of
  `nexo_unique` whose id is a hash of the table, column and value,
  created only when it does not exist, before the row; ids come from a
  counter document updated by a script. Every write asks for a refresh,
  so that a program reads what it wrote, which costs time on a large
  index.
- `key:` is the password of the user in the address (Basic), or, with
  no user, an API key. HTTPS for any host but this machine.
- Transactions, `db.sql`, `nexo db` and the logs (`elastic: METHOD path
  body`) are those of D171; tests/es_server.py is an Elasticsearch
  written from its documentation for the tests.

**D173. More AI providers.** Section 8 names anthropic, openai and ollama
in its examples ("provider:model"); D106 took those three. Four more are
providers now, in both phases (nexo/stdlib/ai.py, compiler/runtime/src/
ai.rs), each through its own API, with `ask`, `stream: true`, typed
answers, agents and their tools, usage and budgets as the others:
- `mistral`: Mistral's chat completions, which are OpenAI's
  (https://api.mistral.ai, a bearer key); it sends the usage of a
  streamed answer in its last event, so no `stream_options` is sent.
- `azure`: Azure OpenAI, `"azure:<deployment>"`. Its address is the
  resource's, so `url = "https://NAME.openai.azure.com"` is needed
  (AIError.url without it); the API version is 2024-10-21. `key:` is the
  resource's api-key; without one, a token of Azure AD from the
  credentials of D120 (AZURE_TENANT_ID, AZURE_CLIENT_ID,
  AZURE_CLIENT_SECRET, AZURE_AUTHORITY_HOST) is sent as a bearer token.
- `gemini`: Google's generateContent and streamGenerateContent (server-
  sent events), the key in `x-goog-api-key`; the system text is
  `systemInstruction`, tools are `functionDeclarations`, and the results
  of tools go back as `functionResponse` parts.
- `bedrock`: AWS's Converse API (`/model/<id>/converse` and
  `converse-stream`, whose answer is AWS's binary event stream, read with
  its CRCs). `key:` is a Bedrock API key, sent as a bearer token; without
  one the request is signed with SigV4 and the AWS credentials of D120,
  in AWS_REGION (or AWS_DEFAULT_REGION, else us-east-1). A Bedrock model
  id holds colons (`anthropic.claude-3-haiku-20240307-v1:0`), so a model
  is read as "provider:name" only when the part before its first colon is
  a provider, or when no provider is configured; `nexo ai setup` accepts
  colons in a Bedrock model.
- Bedrock and Azure without a key and without those variables fail with
  AIError.cloud_key, which names them.
- A server that speaks OpenAI's API (Groq, DeepSeek, OpenRouter,
  Together, vLLM, LM Studio) needs no provider of its own: `provider =
  "openai"` with its `url` (D106).
- tests/test_ai_providers.py runs each against a local server that
  answers as the provider's documentation says, and checks Bedrock's
  signature with its own implementation of SigV4.

**D174. Packages: `nexo add`, `nexo publish`, nexo.lock.** Section 13
gives the rules of packages (a `nexo.toml` and a `nexo.lock`, SemVer,
deterministic resolution, one version of each package, signed packages,
`nexo audit` on install, declared permissions against the `policy`) and
one example, `stripe-payments = "^2.1"` used as `use stripe_payments`.
It does not say how a registry is laid out, how a package is signed or
named, nor how a package is made. In both phases (nexo/packages.py,
compiler/nexoc/src/packages.rs), with the same files byte for byte:
- A registry is static files, a folder or an HTTPS address (HTTP only
  for this machine, as D172): `NAME/index.json` lists the versions, each
  with the sha256 of its archive, the Ed25519 key that signed it, the
  signature of the archive's bytes and the registry's `advisories`;
  `NAME/NAME-VERSION.json` is the archive, `{"files": {...}}`, JSON with
  sorted keys and no spaces. The official registry is
  `https://registry.nexo-lang.org` (provisional, as the playground of
  section 26); `NEXO_REGISTRY` names another, for mirrors and tests.
- A package name is lowercase letters and digits in words joined by `-`;
  `use` writes it with `_`, as section 13 does. The `-`/`_` pair is the
  only mapping, so `_` is not part of a package name.
- A requirement is `^MAJOR.MINOR` or `^MAJOR.MINOR.PATCH`, Cargo's caret:
  `^1.2` is 1.2.0 up to 2.0.0, `^0.3` stays in 0.3. It is the only form
  (section 1); `nexo add name` writes `^` and the newest version's
  major and minor.
- Resolution: each package takes the highest version that every
  requirement on it in the graph accepts, or its version in nexo.lock
  while that still does, so a new compatible version never moves a
  locked project. No version for both requirements is
  `package_conflict`, which names who asks for what (rule 5).
- Signatures: the first `nexo add` of a package records its key in
  nexo.lock; a later version signed by another key is `package_key`,
  an archive whose bytes are not those signed is `package_signature`,
  and the locked version published again with other bytes is
  `package_hash`. Nothing is written when any check fails. Who may
  publish under a name, and the review of its keys, is the registry's
  (D133).
- nexo.lock holds name, version, sha256, key and dependencies of each
  package, sorted by name. `nexo add` without a name installs exactly
  what it says, into `.nexo/packages/NAME/` (a clone of the project).
- `use pkg` loads the package's `lib.nexo` and `use pkg.a.b` its
  `a/b.nexo`. A file can use the packages its own nexo.toml lists, not
  the dependencies of those (as Cargo); inside a package, its own
  modules are named after it (`use money_format.round`), and any other
  name is `package_use`. Packages follow the program's policy at run
  time, as modules do (D142).
- Permissions: `[permissions]` in a package's nexo.toml uses the kinds of
  D142, `net = ["api.stripe.com"]`, `shell = true`. `nexo add` prints
  them and rejects the package (`package_permissions`, with the `allow`
  line that would let it) when a program of the project with a policy
  does not allow one.
- Audit on install: an advisory of severity `critical` stops `nexo add`
  unless `--accept-risk`; others are printed as warnings. Advisories are
  written by the registry, not signed by the author, so they can only
  stop an install.
- `nexo publish` signs the project with the user's key (made on first use
  in the configuration folder of D106, `publish.key`, mode 0600) and
  writes it to a registry folder; it takes nexo.toml, README.md and the
  `.nexo` files, never a hidden folder. A published version never
  changes (`publish_exists`). Publishing to the official registry is
  done through it, so an address is `publish_registry`.
- `nexo build --release` lists each package of nexo.lock in the SBOM of
  D128 (`pkg:nexo/NAME@VERSION` with its sha256).
- D148's "no dependencies until the registry" no longer holds; the
  registry service itself, its review of `unsafe` and its signing keys,
  still needs people (D133).
