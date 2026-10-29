# Nexo for VS Code

Support for [Nexo](https://github.com/marcrabadan/nexo), the language with
one way to do each thing.

- Highlighting of the whole core: keywords, pipeline steps after `|>`,
  text with `{interpolation}`, numbers with units (`30s`, `10%`,
  `19.99 EUR`), `##` documentation comments.
- Errors as you type, in the same words as the compiler: what went wrong
  and how to fix it. Warnings of `nexo lint` too.
- Quick fixes for errors and warnings, and **Fix all** (the safe fixes of
  `nexo fix`).
- **Format Document** with `nexo fmt` (it has no options).
- Go to definition, hover with the signature and the `##` comments, the
  outline of the file and completion.
- Commands **Nexo: Run file**, **Nexo: Run tests of file** and **Nexo:
  Restart language server**.

## Installing Nexo

The extension carries no language server of its own. It runs `nexo lsp`,
the server built into the `nexo` command (D87), and the two run commands
run that same command in a terminal. **Nothing works until `nexo` is
installed and `nexo.path` points at it.**

Nexo is not packaged yet, so it is installed from a clone of the
repository. Either implementation serves: both answer the same requests
with the same words.

```sh
git clone https://github.com/marcrabadan/nexo.git
cd nexo
```

### Phase 0, the interpreter

Needs Python 3.12 or newer.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

The command is then `<clone>/.venv/bin/nexo`.

### Phase 1, the compiler

Needs a Rust toolchain (`cargo`, 1.89 or newer) and `clang`.

```sh
cd compiler
cargo build --release
```

The command is then `<clone>/compiler/target/release/nexo`.

### Pointing the extension at it

Open Settings (`Cmd`/`Ctrl` `,`), search for `nexo.path` and write the
absolute path of the command, or put its directory on your `PATH` and
leave the default `nexo`. In `settings.json`:

```json
{
  "nexo.path": "/Users/you/nexo/compiler/target/release/nexo"
}
```

The value is run as it is written: VS Code does not expand
`${workspaceFolder}`, `~` or environment variables inside it, so write
the path in full. Saving the setting restarts the server, so there is
nothing else to do; **Nexo: Restart language server** restarts it after
rebuilding the compiler.

If the path is wrong, the extension says so once, naming the command it
tried: *Nexo: cannot start "... lsp". Set nexo.path to the nexo
command.* The server's own log is in the **Output** panel, channel
**Nexo**.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `nexo.path` | `nexo` | the `nexo` command to run: a name on the `PATH`, or the absolute path of `compiler/target/release/nexo` or `.venv/bin/nexo` |