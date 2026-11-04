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
## Publishing from GitHub Actions

Store the publisher's Azure DevOps personal access token as the repository
**Actions secret** `VSCE_PAT` (not a repository variable or Dependabot secret).
The token needs Marketplace Manage scope and access to publisher `nexo-lang`.
An organization secret must explicitly permit this repository. Environment
secrets are not available unless the job selects that environment.

Push a `vscode-vX.Y.Z` tag to publish its package, or open **Actions → VS Code
extension → Run workflow**, select `main` and enable **publish**. A manual run
without that option only tests and packages the extension. The workflow checks
that the credential is present before compiling; it never prints the token.

If publishing reports `TF400813`, check whether `VSCE_PAT` was available to the
run, then verify token expiry, scope and publisher membership. After correcting
the secret, rerun the failed release job or use a manual publish from `main`.
Do not republish a version already accepted by Marketplace; increase the package
and lockfile version for a new release.

## Visual Web Builder (experimental)

Run **Nexo: Open Web Builder** from the Command Palette. Add headings,
paragraphs, buttons, inputs, or rectangles (`div`) from the palette, then
select a layer or drag it on the canvas. The property inspector edits text,
position, size, foreground and background color. The Nexo source pane updates
immediately during dragging and property changes. Undo and redo keep up to
100 design edits.

**Save design** writes an editable versioned JSON document; **Open design**
restores it. **Export .nexo** saves the current generated `App` component and
opens it beside the builder. Existing files use the normal save dialog's
replacement confirmation; save existing dirty editor buffers before replacing
them. Code updates live in the builder; exporting to disk is explicit.
The builder itself does not require a running Nexo language server.

This first version is a fixed 960 × 900 wireframe canvas with independent,
absolutely positioned elements. Change width and height in Properties to resize.
It does not yet support responsive auto layout, nesting, interaction bindings,
images, or importing changes from handwritten Nexo code. Text containing
interpolation braces is rejected, since text bindings are not implemented.
Buttons and inputs are design elements; they do not execute application actions.
The generated component can be checked with `nexo web check app.nexo` and
built with `nexo web build app.nexo --output dist`.

To install a locally packaged build, use **Extensions → … → Install from VSIX…**
and select `nexo.vsix`, then run **Nexo: Open Web Builder**.

To try the extension from this repository:

1. Open `editors/vscode` as your VS Code workspace.
2. Run `npm ci` in that directory.
3. Press **F5** using the included **Run Nexo Extension** launch configuration.
4. In the Extension Development Host, run **Nexo: Open Web Builder**.

Run `node test/builder.test.js` for dependency-free generator and validation
checks, or `npm test` for the complete extension suite after installing packages.

### Design System Manager

The inspector includes a **Design System Manager**. Choose **light** or **dark**
and edit the active palette: primary, on-primary, canvas background, surface,
text and border colors. Font family, body and heading sizes, padding and corner
radius are shared between both modes. Changes update the canvas and generated
Nexo CSS immediately; exporting produces the currently selected theme.

New elements use the design system. Buttons use primary/on-primary colors;
other elements use surface/text colors. Use **Apply theme to all elements** to
bind an existing design to the system. Existing JSON designs retain their
original colors until you apply the theme. Theme edits and applying a theme
support undo/redo.

For an individual element, disable **Use design system** to use its custom
colors, or edit its color/background directly to switch to custom styling.
Enable the checkbox to restore the shared styles. Applying a theme to all
elements replaces custom styling bindings; Undo restores them.

Both palettes, shared settings and element bindings are saved with the design.
**Export design system** writes the settings to a separate JSON file for reuse;
**Import design system** loads them into another design. Imported settings affect
theme-bound elements; custom elements keep their styling until you apply the theme.
This is a design-time theme choice; runtime theme switching is not generated yet.
