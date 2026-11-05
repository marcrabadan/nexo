// The Nexo extension for VS Code: starts `nexo lsp` (D87) and adds the
// commands to run a file and its tests.
const vscode = require("vscode");
const { LanguageClient } = require("vscode-languageclient/node");

let client;

function settings() {
  const config = vscode.workspace.getConfiguration("nexo");
  const command = config.get("path") || "nexo";
  return { command };
}

function startClient(context) {
  const { command } = settings();
  const server = { command, args: ["lsp"] };
  client = new LanguageClient(
    "nexo",
    "Nexo",
    { run: server, debug: server },
    { documentSelector: [{ language: "nexo" }] }
  );
  context.subscriptions.push(client);
  return client.start().catch((error) => {
    vscode.window.showErrorMessage(
      `Nexo: cannot start "${command} lsp" (${error.message}). Set nexo.path to the nexo command.`
    );
  });
}

function runInTerminal(subcommand) {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.languageId !== "nexo") {
    return;
  }
  const { command } = settings();
  const file = editor.document.fileName;
  editor.document.save().then(() => {
    const terminal = vscode.window.terminals.find((t) => t.name === "Nexo") || vscode.window.createTerminal("Nexo");
    terminal.show(true);
    const quote = (s) => `"${s.replace(/"/g, '\\"')}"`;
    terminal.sendText([quote(command), ...subcommand, quote(file)].join(" "));
  });
}

// Ignore comments and quoted text so prose examples do not open the builder.
function referencesWeb(document) {
  if (!document || document.languageId !== 'nexo' || !document.fileName.endsWith('.nexo')) return false;
  const source = document.getText().replace(/"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|#[^\n]*/g, ' ');
  return /\bnexo\.web\b/.test(source);
}

function activate(context) {
  const opened = new Set();
  const autoOpenBuilder = (editor, editing = false) => {
    const document = editor && editor.document;
    if (vscode.workspace.getConfiguration('nexo').get('builder.autoOpen') === false || !referencesWeb(document)) return;
    const key = document.uri.toString();
    if (opened.has(key)) { if (!editing) require('./builder/host').loadDocument(document); return; }
    opened.add(key);
    require('./builder/host').openBuilder(context, { automatic: true, document });
  };
  context.subscriptions.push(
    vscode.commands.registerCommand("nexo.builder", () => require("./builder/host").openBuilder(context, { document: referencesWeb(vscode.window.activeTextEditor?.document) ? vscode.window.activeTextEditor.document : undefined })),
    vscode.commands.registerCommand("nexo.run", () => runInTerminal([])),
    vscode.commands.registerCommand("nexo.test", () => runInTerminal(["test"])),
    vscode.commands.registerCommand("nexo.restart", async () => {
      if (client) {
        await client.stop();
      }
      await startClient(context);
    }),
    vscode.workspace.onDidChangeConfiguration(async (event) => {
      if (event.affectsConfiguration("nexo") && client) {
        await client.stop();
        await startClient(context);
      }
    })
  );
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor(autoOpenBuilder),
    vscode.workspace.onDidChangeTextDocument(event => {
      const editor = vscode.window.activeTextEditor;
      if (editor && editor.document === event.document) autoOpenBuilder(editor, true);
    }),
    vscode.workspace.onDidCloseTextDocument(document => opened.delete(document.uri.toString()))
  );
  autoOpenBuilder(vscode.window.activeTextEditor);
  return startClient(context);
}

function deactivate() {
  return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };
