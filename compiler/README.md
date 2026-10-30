# Nexo compiler (phases 1 to 4)

`nexo` compiles the core of Nexo to native code: the source goes through
the lexer and parser (section 24), the compile errors of section 3, name
resolution (scopes, closures, D25-D29), gradual type checking (D68) and
code generation to LLVM IR, which clang links with the runtime library.

Needs `cargo` (Rust 1.89 or newer: the runtime locks files) and `clang`.

    cargo build --release
    target/release/nexo app.nexo                     # compile and run
    target/release/nexo build --release app.nexo -o app
    target/release/nexo test [paths]                 # run the test blocks
    target/release/nexo                              # the REPL
    target/release/nexo fmt|lint|fix|check [paths]   # the tools of phase 3
    target/release/nexo lsp                          # language server
    target/release/nexo db diff|migrate app.nexo     # migrations (nexo.db)
    target/release/nexo ai setup|usage               # nexo.ai configuration

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

Phase 4 adds the library in `runtime/src`: `library.rs` (the builtins of
phase 4, with ids from 100) and `time.rs`, `text.rs`, `crypto.rs`,
`http.rs`, `db.rs` (SQLite, pipelines translated to SQL), `web.rs` (the
server), `ai.rs` (`ask`, tools, agents), `clib.rs` (`use c`, calls
through the registers) and `pylib.rs` (`use python`, libpython loaded
with `dlopen`). The parser rewrites module blocks into calls of `__`
builtins (D90). The driver reads C headers when it compiles
(`nexoc/src/cheader.rs`), records which Python to load, and runs the
servers after the top level (D102).

Environment: `NEXO_CC` (default `clang`), `NEXO_RUNTIME` (path of
`libnexo_runtime.a`, default next to the executable), `NEXO_LEAKCHECK=1`
(report objects not freed when the program ends), `NEXO_KEEP_IR=1` (keep
the LLVM IR next to the built program), `NEXO_PYTHON` (the Python whose
libpython `use python` loads, default `python3`), `CC` (the preprocessor
for C headers), `NEXO_WEB_LOCAL=1` (servers listen on 127.0.0.1 only),
`PORT`, `NEXO_SQL_LOG=1`.

The AI-native runtime increment (D184–D189) keeps parity with the interpreter for
typed agent input/output, inherited control, conservative token/money admission,
confined checkpoints and opt-in tracing. See the [lifecycle](../docs/agent-lifecycle.md)
and [completion record](../docs/ai-native-phase3-completion.md). Native durable
state currently requires Linux AMD64; unsupported hosts/profiles fail before
dispatch. Generic Shield workflows retain their existing rejection.
