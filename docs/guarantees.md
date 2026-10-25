# The guarantees of Nexo, measured

The exit criterion of phase 6 (specification, section 25) is that "the
guarantees in 'Goals and guarantees' are met or removed". This page says,
for each one, how it is checked in this repository, what it gives today,
and what is still missing. It is updated with each release; the numbers
come from the scripts named, on the machine in `compiler/bench/goals.json`
(4 cores, Linux; 2026-10-25). Decisions D126-D133 explain the choices.

| State | Meaning |
| --- | --- |
| Met | checked by the repository's tests or scripts |
| Not met | measured, and the number is outside the goal |
| Needs people | cannot be done by code: it needs an audit, a study or a registry |

## Safer

| Guarantee | State | Evidence |
| --- | --- | --- |
| No memory errors or data races | Met for the test suite | Every suite runs with the leak checker (`NEXO_LEAKCHECK=1`); the race rules of D80-D81 are compile errors. `compiler/scripts/fuzz.py` runs random programs in both phases and requires the same output with no crash or leak: 310 programs on three seeds, all identical, after it found and fixed one bug (an outer `var` assigned twice in a loop). `.github/workflows/fuzz.yml` runs 500 more every night. |
| `unsafe` under control | Met (the tool); the registry needs people | `nexo audit` lists every use of C, the only unsafe code of this edition (D129). The public registry of reviewed packages does not exist yet. |
| Capability-based permissions | Not implemented | The program `policy` of section 9 and `nexo policy show` were left out of every phase so far (D89, D113). Until they exist this row should not be claimed. |
| Hardened supply chain | Met for builds; signing needs people | Two `nexo build --release` of the same code give the same binary byte for byte, from any directory, and the same CycloneDX SBOM (D128, `tests/test_phase6.py`). Signed packages need the registry. |
| Verifiable contracts | Met | `expect`, `ensure` and `old()` in both phases; the compiler proves what it can and checks the rest (D83). |
| Cryptography without dangerous choices | Algorithms fixed; the audit needs people | Argon2id, AES-256-GCM and Ed25519 are fixed for edition 0.1 (D97). No external audit of `crypto` has been made. |

## Smarter

| Guarantee | State | Evidence |
| --- | --- | --- |
| The compiler understands intent | Met (the tool); the public set of cases needs a model | `nexo check --intent` asks the configured model whether each function does what its `##` comments and contracts say (D132), tested against a local model. The public set of known mismatches, run against a real model, is still to be written. |
| Tests proposed automatically | Met (the tool) | `nexo test --suggest` proposes edge cases from types, contracts and unions and records what the code gives now (D131). Branch coverage before and after is not measured yet: neither phase has a coverage tool. |
| Every error comes with its fix | Met | Every message that reports a problem has its fix sentence in both catalogs (`tests/unit/test_errors.py`). Of the 32 programs in the repository that do not compile (27 kinds of error), 32 say how to fix it (100%) and 6 (19%) have an edit that `nexo fix` or the editor applies (`compiler/scripts/fix_rate.py`). |
| Verifiable AI-generated code | Partly | One way to do each thing, types at boundaries and contracts, and `nexo check` validates code before it runs. The rate of LLM-generated programs that compile and pass their tests on the first try needs a benchmark run against models. |
| AI does not invent APIs | Met (the tool); the comparison needs a model | `nexo docs --for-ai` prints the exact list of what a program can use, from `nexo/stdlib/api.json`, which a test keeps equal to the builtins of both phases (D130). The comparison with and without it needs models. |

## More effective

Measured by `python3 compiler/bench/goals.py` (best of 10 runs; the Rust
programs are in `compiler/bench/rust/`, built with `rustc -O` and overflow
checks, since Nexo checks int overflow).

| Goal | Value | Today | State |
| --- | --- | --- | --- |
| Performance on typical code | under 1.5× Rust's time | loop 1.25×, parallel 1.48×, hello 1.69×, fib 2.19×, records 3.16×, strings 3.57×, maps 5.21×, lists 7.02× | Not met |
| Memory | under 1.3× Rust's RAM | 1.0× on six benchmarks; lists 2.23×, strings 3.18× | Not met |
| Compilation | 100,000 lines in under 10 s in development mode | 208 s (about 17 s in the compiler, the rest in clang) | Not met |
| Program start-up | under 20 ms | 2.3 ms | Met |
| First useful program | under 1 hour for someone with no experience | not measured | Needs people (user studies) |
| From idea to production | one project for code, infrastructure and deployment | `examples/deploy` holds an app, its tests, its infrastructure on four clouds and its pipeline in one file (phase 5) | Met |

What phase 6 changed (D126, D127): records went from 8.0× to 3.2× Rust,
maps from 6.2× to 5.2× and strings from 3.8× to 3.6×, mostly by reading the
fields of records whose type is known by their index, calling their
methods directly, keeping small records in one allocation, counting loops
over ranges without an iterator, and changing reference counts without
atomic instructions until a second thread starts. Compiling 8,000 lines
went from 2.4 s to 0.4 s in the compiler (name resolution was quadratic)
and the development mode no longer optimises, which made clang about six
times faster.

What is left is the shape of the compiled code: every value is two words
with a tag, every operation on a value whose type is not known calls the
runtime, and lists, maps and strings are reached through those calls.
Meeting 1.5× on lists, maps and strings needs code specialised by type
(unboxed lists of ints, maps with int or text keys known at compile
time), and meeting 10 s for 100,000 lines needs much smaller IR or
compiling the program in parallel parts. Both are compiler work for the
next releases, or the goals change.

## What is needed to finish phase 6

- The performance and compilation goals above: more compiler work, or new
  values in the specification.
- Capability-based permissions: the program `policy` of section 9.
- An external security audit of the compiler and of `crypto`, studies
  with real users, the package registry with signed packages and
  reviewed `unsafe`, and the model comparisons: these need people and
  money, not code.

Until then the transparency commitment of "Goals and guarantees" holds:
a guarantee that is not met is either fixed or removed before edition 1.0.
