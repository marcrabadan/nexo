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

