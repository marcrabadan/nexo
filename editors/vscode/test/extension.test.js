// Loads extension.js with a stand-in for the vscode API and checks that it
// starts `nexo lsp` and registers the commands package.json declares.
const assert = require("assert");
const Module = require("module");
const pkg = require("../package.json");

const registered = [];
const started = [];
let settings = { path: "/opt/nexo/bin/nexo" };

const vscode = {
  workspace: {
    getConfiguration: () => ({ get: (key) => settings[key] }),
    onDidChangeConfiguration: () => ({ dispose() {} }),
  },
  commands: { registerCommand: (name) => (registered.push(name), { dispose() {} }) },
  window: { showErrorMessage: (m) => { throw new Error(m); }, terminals: [], activeTextEditor: undefined },
};

class LanguageClient {
  constructor(id, name, server, options) {
    started.push({ id, server, options });
  }
  start() { return Promise.resolve(); }
  stop() { return Promise.resolve(); }
  dispose() {}
}

const load = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "vscode") return vscode;
  if (request === "vscode-languageclient/node") return { LanguageClient };
  return load.call(this, request, parent, isMain);
};

(async () => {
  const ext = require("../extension.js");
  const context = { subscriptions: [] };
  await ext.activate(context);
  assert.strictEqual(started.length, 1);
  assert.deepStrictEqual(started[0].server.run, { command: "/opt/nexo/bin/nexo", args: ["lsp"] });
  assert.deepStrictEqual(started[0].options.documentSelector, [{ language: "nexo" }]);
  const declared = pkg.contributes.commands.map((c) => c.command).sort();
  assert.deepStrictEqual(registered.slice().sort(), declared);
  settings = { path: "" };
  await ext.activate({ subscriptions: [] });
  assert.deepStrictEqual(started[1].server.run, { command: "nexo", args: ["lsp"] });
  assert.ok(pkg.contributes.languages[0].extensions.includes(".nexo"));
  console.log("extension: starts nexo lsp and registers", registered.length / 2, "commands");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
