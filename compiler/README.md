# Nexo compiler (phases 1, 2 and 3)

`nexo` compiles the core of Nexo to native code: the source goes through
the lexer and parser (section 24), the compile errors of section 3, name
resolution (scopes, closures, D25-D29), gradual type checking (D68) and
code generation to LLVM IR, which clang links with the runtime library.

    cargo build --release
    target/release/nexo app.nexo                     # compile and run
    target/release/nexo build --release app.nexo -o app
    target/release/nexo test [paths]                 # run the test blocks
    target/release/nexo --lang en app.nexo
    target/release/nexo                              # the REPL
    target/release/nexo fmt|lint|fix|check [paths]   # the tools of phase 3
    target/release/nexo lsp                          # language server

Tests: `cargo test` (unit tests), `python3 scripts/conformance.py`
(conformance suite and examples) and `scripts/differential.py` (the same
output as the phase 0 interpreter on ~200 programs).

Phase 2 adds reference counting with a leak checker, copy-on-write
collections, `weak` fields, `parallel` on threads with the race rules
(D80, D81), `nexo.sync` (D82) and contracts the compiler proves (D83;
`nexo build --release --trust-ensure` leaves out the unproven `ensure`
checks). `bench/compare.py` compares each benchmark with the same
program in Rust (D84).

Phase 3 adds the tools, with the same output as phase 0: `fmt` (`nexoc/src/fmt.rs`,
D85), `lint` and `fix` (`lint.rs`, D86), `check` and the language server
(`lsp.rs` with a small `json.rs`, D87), and the REPL (D88): the session
is a program compiled and run again for every input, the earlier inputs
replaying from the journal in `runtime/src/repl.rs`
(`NEXO_REPL_JOURNAL`).

Environment: `NEXO_CC` (default `clang`), `NEXO_RUNTIME` (path of
`libnexo_runtime.a`, default next to the executable), `NEXO_LEAKCHECK=1`
(report objects not freed when the program ends), `NEXO_KEEP_IR=1` (keep
the LLVM IR next to the built program).
