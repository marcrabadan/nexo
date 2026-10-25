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

function activate(context) {
  context.subscriptions.push(
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
  return startClient(context);
}

function deactivate() {
  return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };
