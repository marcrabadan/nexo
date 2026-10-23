# Nexo compiler (phase 1)

`nexo` compiles the core of Nexo to native code: the source goes through
the lexer and parser (section 24), the compile errors of section 3, name
resolution (scopes, closures, D25-D29), gradual type checking (D68) and
code generation to LLVM IR, which clang links with the runtime library.

    cargo build --release
    target/release/nexo app.nexo                     # compile and run
    target/release/nexo build --release app.nexo -o app
    target/release/nexo test [paths]                 # run the test blocks
    target/release/nexo --lang en app.nexo

Tests: `cargo test` (unit tests), `python3 scripts/conformance.py`
(conformance suite and examples) and `scripts/differential.py` (the same
output as the phase 0 interpreter on ~200 programs).

Environment: `NEXO_CC` (default `clang`), `NEXO_RUNTIME` (path of
`libnexo_runtime.a`, default next to the executable).
