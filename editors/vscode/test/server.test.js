// Talks to `nexo lsp` with vscode-jsonrpc, the protocol library of the VS
// Code client. NEXO picks the command; without it, the compiler's debug
// build, or phase 0 in .venv, whichever exists.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const rpc = require("vscode-jsonrpc/node");

const root = path.resolve(__dirname, "..", "..", "..");
const candidates = [
  path.join(root, "compiler", "target", "debug", "nexo"),
  path.join(root, ".venv", "bin", "nexo"),
];
const command = process.env.NEXO || candidates.find((c) => fs.existsSync(c));
const args = process.env.NEXO_ARGS ? process.env.NEXO_ARGS.split(" ") : [];

if (!command) {
  console.log("server: skipped, no nexo found (cargo build, or set NEXO)");
  process.exit(0);
}

(async () => {
  const child = spawn(command, [...args, "lsp"]);
  child.on("error", (e) => {
    console.error(`server: cannot start ${command}: ${e.message}`);
    process.exit(1);
  });
  const connection = rpc.createMessageConnection(
    new rpc.StreamMessageReader(child.stdout),
    new rpc.StreamMessageWriter(child.stdin)
  );
  const diagnostics = new Promise((resolve) => {
    connection.onNotification("textDocument/publishDiagnostics", (p) => resolve(p));
  });
  connection.listen();
  const init = await connection.sendRequest("initialize", { processId: null, rootUri: null, capabilities: {} });
  assert.ok(init.capabilities.hoverProvider);
  connection.sendNotification("initialized", {});
  const uri = "file:///tmp/app.nexo";
  const text = "fn double(x):\n    return x*2\nvar n = double(4)\nprint(n)\n";
  connection.sendNotification("textDocument/didOpen", { textDocument: { uri, languageId: "nexo", version: 1, text } });
  const published = await diagnostics;
  assert.strictEqual(published.uri, uri);
  assert.deepStrictEqual(published.diagnostics.map((d) => d.code), ["var_never_changed"]);
  const edits = await connection.sendRequest("textDocument/formatting", { textDocument: { uri }, options: { tabSize: 4, insertSpaces: true } });
  assert.strictEqual(edits[0].newText, "fn double(x):\n    return x * 2\nvar n = double(4)\nprint(n)\n");
  const hover = await connection.sendRequest("textDocument/hover", { textDocument: { uri }, position: { line: 2, character: 9 } });
  assert.strictEqual(hover.contents.value, "```nexo\nfn double(x)\n```");
  await connection.sendRequest("shutdown");
  connection.sendNotification("exit");
  const code = await new Promise((resolve) => child.on("exit", resolve));
  assert.strictEqual(code, 0);
  connection.dispose();
  console.log(`server: ${command} answers over vscode-jsonrpc`);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
