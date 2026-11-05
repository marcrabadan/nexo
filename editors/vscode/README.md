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

The canvas edits absolutely positioned elements, with desktop, tablet and mobile
layouts. Change size in Properties or drag resize handles. Shift+click layers or
elements to arrange several at once; extract static selections into reusable
components. The canvas is a design surface. Use **Live preview** to execute source
state, input handlers and navigation in a browser. Freeform auto layout, images
and authoring runtime bindings in the inspector are not supported. Dynamic text
must be edited in source and is preserved when applying visual changes.
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

### Canvas editing tools

Preview desktop (960px), tablet (768px) and mobile (375px) canvas widths and
zoom from 50% to 125%. The selected viewport is also the layout being edited:
desktop supplies base geometry, tablet overrides apply at widths up to 768px,
and mobile overrides apply at widths up to 480px.
Enable **Snap to 8px grid** for pointer dragging. Drag distances respect zoom.

Duplicate the selected element or change its stacking order with **Send backward**
and **Bring forward**. Layer order is preserved in saved designs and exported
components. The selected layer is highlighted. Use arrow keys to move by 1px,
Shift+arrow for 10px, Delete/Backspace to remove, Ctrl/Cmd+D to duplicate,
Ctrl/Cmd+Z to undo, and Ctrl/Cmd+Shift+Z or Ctrl/Cmd+Y to redo.
Shortcuts leave property text fields and other inputs alone. Editing operations
support undo/redo; merely selecting a viewport or changing zoom/grid controls
does not change the design.

Select an element to reveal eight edge and corner resize handles. Drag a handle
to change its dimensions; resizing respects zoom and optional grid snapping.
The opposite edge stays fixed when resizing from the top or left. Dimensions
are bounded by the canvas coordinate limits (up to 10,000px). Each drag is one
undo step. Width and Height in Properties remain available for exact sizes.

### Open automatically for web files

Opening or switching to a `.nexo` file that references `nexo.web` opens Web Builder
beside the source editor without moving keyboard focus. Adding the reference
while editing also opens it. Comments and quoted strings do not trigger it.
The extension reuses the existing builder and opens automatically only once per
file until that file is closed, so dismissing the builder does not keep reopening it.
Set `nexo.builder.autoOpen` to `false` to use only **Nexo: Open Web Builder**.

Automatic opening loads the current editor buffer, including unsaved changes,
into the builder. Switching to another web file loads that file; manual
**Nexo: Open Web Builder** loads the active web file. **Reload source** explicitly
reimports it, discarding current canvas changes (which remain available in Undo). Import waits until the
webview is ready. Source edits reload automatically while the canvas has no edits.
If both source and canvas change, the builder keeps the canvas and reports a
conflict instead of replacing either side. Undo restores the design from before an import.

The importer reads the first public component/page and supported static elements
(h1, h2, p, button, input, div). It supports literal text and basic pixel geometry
and six-digit hex colors from simple tag/class CSS selectors. Other layout is
approximated as a vertical wireframe. Runtime state, event handlers, interpolation,
custom components, control flow and advanced CSS are not reproduced; a partial
import notice lists limitations. Import never runs source code. Files without
supported elements show an error and keep the current design.

Import leaves the original file unchanged. **Apply to source** explicitly updates
the linked editor buffer; normal VS Code Save and Undo remain available.
**Export .nexo** creates a standalone static component; use Apply to source to
preserve the original file's runtime logic.

### Apply canvas changes to source

**Apply to source** updates the imported elements' literal text, visual styling,
responsive geometry and stacking order. It also adds new elements and removes
deleted elements. Existing state, functions, event handlers, dynamic text,
unsupported view nodes and handwritten CSS remain in the file. Builder CSS is
kept in a marked block rather than replacing handwritten styles. The file includes
a visual metadata comment so component instances, groups, themes and breakpoint
overrides can be restored. Runtime-only edits retain this metadata; manual
view/style changes trigger a fresh static import.

Updates target the exact source revision that was loaded. If the editor has
changed meanwhile, use **Reload source** before applying. Save the design as JSON
first if you want to keep conflicting canvas edits. Undoing an import from another
file cannot apply an old design to the newly linked file. Dynamic text/class
expressions and removal of nested source containers are deliberately rejected
when they cannot be patched while preserving their contents.

### Responsive layout editing

Choose Desktop, Tablet or Mobile, then drag, resize, align, or edit X/Y/Width/Height.
Tablet inherits desktop values; mobile inherits tablet and desktop values unless
overridden. Generated Nexo includes scoped CSS media queries. Text and theme
changes are shared across breakpoints. **Reset breakpoint overrides** resets the
selection, or all elements if none are selected. Zoom does not affect exported size.

### Reusable components

Shift+click a static selection, name it (for example `BuilderCard`), and choose
**Create from selection**. The library stores a reusable component; its canvas
instance exposes each child's text as an editable property in the inspector.
Use the library's add button to insert more instances. **Edit defaults** changes
text defaults and instances still using those defaults; custom instance values
stay independent. Export and Apply generate real Nexo component declarations with
string properties. Definitions and instances are included in JSON design files.

Extract components before adding breakpoint overrides to their children; the
instance wrapper can then have its own responsive geometry. Nested reusable
instances and extracting source elements with runtime bindings are rejected.
Those bindings continue to work in their original source component and preview.

### Alignment, distribution and groups

Shift+click elements or layers to select several. Align left/center/right or
top/middle/bottom. Distribution requires at least three elements and preserves
the outer edges while equalizing horizontal or vertical gaps. **Group** makes a
selection move together when dragging or nudging; **Ungroup** separates it. Groups
are editing metadata, not extra DOM containers. Delete removes the selection.
These operations are undoable and apply to the active breakpoint.

### Interactive preview

Choose **Live preview** to compile the canvas together with the linked source's
runtime logic and open a local browser preview. Buttons, inputs, state and routes
use the actual Nexo web runtime. Canvas edits rebuild after a short delay; the
browser reloads after a successful build. Compiler failures keep the last working
preview and show diagnostics in the builder. Clean source edits also update the
preview. **Stop preview** or closing the builder closes its local server and
removes temporary build files. Reloading a build resets application state.

Preview requires a trusted workspace and a working `nexo.path` that supports
`web build`; it does not require Vite or npm dependencies in the user's app. Imports
resolve from a temporary entry beside the linked source. The server binds only
127.0.0.1 and serves compiled preview assets. Application backend APIs still need
their own server; this preview is not a backend proxy or deployment.
