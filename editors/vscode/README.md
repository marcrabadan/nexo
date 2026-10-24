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

The extension runs `nexo lsp`, the language server built into the `nexo`
command. Both implementations have it: the compiler
(`compiler/target/release/nexo`) and the phase 0 interpreter
(`.venv/bin/nexo`).

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `nexo.path` | `nexo` | the `nexo` command to run |
| `nexo.lang` | (empty) | `es` or `en`; empty follows the language of VS Code |

## Building the extension

```sh
cd editors/vscode
npm install
npm test           # grammar and activation tests
npx vsce package   # nexo-0.3.0.vsix
code --install-extension nexo-0.3.0.vsix
```

Publishing to the Marketplace needs a publisher account and a token:
`npx vsce publish -p <token>` (see the main README).
