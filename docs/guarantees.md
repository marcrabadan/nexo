# The guarantees of Nexo, measured

The exit criterion of phase 6 (specification, section 25) is that "the
guarantees in 'Goals and guarantees' are met or removed". This page says,
for each one, how it is checked in this repository, what it gives today,
and what is still missing. It is updated with each release; the numbers
come from the scripts named, on the machine in `compiler/bench/goals.json`
(4 cores, Linux; 2026-10-25). Decisions D126-D136 explain the choices.

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
| Capability-based permissions | Met for the program; per-package permissions need the registry | The `policy` of section 9 in both phases (D142): a release build stops at every read, write, connection, environment variable, shell command and C or Python library the policy does not allow, and does not build without a policy; modules follow the program's policy. `nexo policy show` lists what a program can do and `nexo policy suggest` writes the smallest policy from a test run. `tests/test_policy.py` holds the sandbox escape tests: `..`, absolute paths, symbolic links to a file and to a folder, a module, a redirect to another host, writes, `move_file`, `connect`, secrets, shell commands and `use python`, each stopped in a release build. Permissions declared by each package and checked by `nexo add` need the package registry. |
| Hardened supply chain | Met for builds; signing needs people | Two `nexo build --release` of the same code give the same binary byte for byte, from any directory, and the same CycloneDX SBOM (D128, `tests/test_phase6.py`). Signed packages need the registry. |
| Verifiable contracts | Met | `expect`, `ensure` and `old()` in both phases; the compiler proves what it can and checks the rest (D83). |
| Cryptography without dangerous choices | Algorithms fixed; the audit needs people | Argon2id, AES-256-GCM and Ed25519 are fixed for edition 0.1 (D97). No external audit of `crypto` has been made. |

Shield phase 1 adds strict API execution and structured decisions in both
backends; see [the threat model and limitations](shield.md). The capability
checks above are not OS-enforced isolation: concurrent filesystem mutation,
DNS rebinding, direct native syscalls and resource limits are outside them.
`nexo security run` rejects C, Python and shell integrations until isolated
workers exist; ordinary explicit D142 native grants remain trusted code.

Shield phase 2 adds an explicit [Linux offline worker and dependency
ceilings](shield-isolation.md). OS enforcement requires a supported kernel;
this cloud host lacks Landlock, so positive validation requires a compatible
host. [Remote Docker validation](https://github.com/marcrabadan/nexo/actions/runs/40401630179/job/121848682972)
passed with Landlock ABI 7, including all 13 positive kernel cases.
[Shield PR #18](https://github.com/marcrabadan/nexo/pull/18) tracks full-suite CI.
The implementation fails closed rather than treating API checks as OS isolation.
[Docker deployment](shield-docker.md) adds a non-root, read-only, offline container
with capability dropping, seccomp and cgroup limits. This layer works independently
of Landlock; requesting the Linux worker still requires its kernel facilities.

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
| Performance on typical code | under 1.5× Rust's time | strings 1.11×, parallel 1.30×, maps 1.42×, loop 1.60×, hello 1.77×, fib 2.04×, lists 2.15×, records 2.53× | Not met |
| Memory | under 1.3× Rust's RAM | 1.0× or less on seven benchmarks (strings 0.75×); lists 1.10× | Met |
| Compilation | 100,000 lines in under 10 s in development mode | 13.9 s (about 3 s in the compiler, the rest in clang on four cores) | Not met |
| Program start-up | under 20 ms | 2.3 ms | Met |
| First useful program | under 1 hour for someone with no experience | not measured | Needs people (user studies) |
| From idea to production | one project for code, infrastructure and deployment | `examples/deploy` holds an app, its tests, its infrastructure on four clouds and its pipeline in one file (phase 5) | Met |

What phase 6 changed (D126, D127, D134-D136), against the start of the
phase: records went from 8.0× to 2.4× Rust, lists from 6.3× to 3.3×, maps
from 6.2× to 1.7×, strings from 3.8× to 2.4×, and the worst ratio from
8.0× to 3.3×. The main changes: fields of records whose type is known are
read by their index and their methods called directly; a small record is
one allocation and freed at once; loops over ranges are counted and lists
are read in place; `where`/`map` pipelines that end in `sum` or `count`
are one loop with their steps inlined; maps have a table of their own
for int keys; short texts live inside their object; freed objects are
reused from a list per thread; and reference counts change without
atomic instructions until a second thread starts. Compiling 100,000 lines
went from 208 s to 13.9 s: name resolution is linear, suggestions are only
looked for where an error may show them, development mode does not
optimise, calls the reference-count helpers instead of copying them,
compiles a large program in parts on every core, and hands clang values
as two words so its fast instruction selector takes them (D141); the
compiler also leaves out checks it proves are not needed (D140, D141),
which made the IR half its size.

What is left is the shape of the compiled code: every value is two words
with a tag (a list of ints is kept as plain ints, D137), every heap
value is an object of the same size (112 bytes), and an operation on a
value whose type is not known calls the runtime. Times are measured on
programs of 10 to 60 ms, so a few milliseconds of start-up count. Meeting
1.5× on records, lists and calls needs values specialised by type (records
on the stack when they do not escape, unboxed lists of ints, calls that
pass ints in registers everywhere), and meeting 10 s for 100,000 lines
needs smaller code still, or generating it on several threads. Both are
compiler work for the next releases, or the goals change.

## What is needed to finish phase 6

- The performance and compilation goals above: more compiler work, or new
  values in the specification.
- An external security audit of the compiler and of `crypto`, studies
  with real users, the package registry with signed packages and
  reviewed `unsafe`, and the model comparisons: these need people and
  money, not code.

Until then the transparency commitment of "Goals and guarantees" holds:
a guarantee that is not met is either fixed or removed before edition 1.0.
