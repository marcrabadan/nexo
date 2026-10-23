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

