const assert = require('assert');
const Module = require('module');
let receive, saved = '', applied = false, dirty = false;
const errors = [], posted = [];
let fileContent = '{"version":1,"elements":[]}';
const uri = { fsPath: '/design/app.nexo' };
const panel = {
  webview: { cspSource: 'vscode-webview:', asWebviewUri: u => u.fsPath,
    onDidReceiveMessage: callback => { receive = callback; return { dispose() {} }; },
    postMessage: async message => posted.push(message) },
  onDidDispose() {}, dispose() {},
};
const document = { get isDirty() { return dirty; }, getText: () => 'old', positionAt: n => n, save: async () => true };
const vscode = {
  Uri: { joinPath: (root, ...parts) => ({ fsPath: root.fsPath + '/' + parts.join('/') }) },
  ViewColumn: { One: 1, Beside: 2 },
  Range: class { constructor(start, end) { this.start = start; this.end = end; } },
  WorkspaceEdit: class { replace(target, range, text) { saved = text; } },
  window: { createWebviewPanel: () => panel, showSaveDialog: async () => uri, showOpenDialog: async () => [uri], showTextDocument: async () => {}, showErrorMessage: text => errors.push(text) },
  workspace: { openTextDocument: async () => document, applyEdit: async () => { applied = true; return true; }, fs: { readFile: async () => Buffer.from(fileContent) } },
};
const load = Module._load;
Module._load = function (name, ...rest) { return name === 'vscode' ? vscode : load.call(this, name, ...rest); };
const { openBuilder } = require('../builder/host');
Module._load = load;
(async () => {
  openBuilder({ extensionUri: { fsPath: '/extension' }, subscriptions: [] });
  assert(panel.webview.html.includes("default-src 'none'"));
  const model = { version: 1, elements: [] };
  await receive({ type: 'export', model });
  assert(applied); assert(saved.includes('pub component App:')); assert(posted.at(-1).text.includes('Saved'));
  await receive({ type: 'load' }); assert.deepStrictEqual(posted.at(-1), { type: 'load', model });
  applied = false; dirty = true;
  await receive({ type: 'export', model }); assert(!applied); assert(errors.at(-1).includes('unsaved'));
  dirty = false; await receive({ type: 'save', model: { version: 100 } }); assert(!applied); assert(errors.at(-1).includes('Invalid builder design'));
  const designSystem = require('../builder/model').defaultDesignSystem();
  await receive({ type: 'save-theme', designSystem });
  assert.deepStrictEqual(JSON.parse(saved), designSystem);
  fileContent = JSON.stringify(designSystem);
  await receive({ type: 'load-theme' }); assert.deepStrictEqual(posted.at(-1), { type: 'theme', designSystem });
  fileContent = '{"mode":"invalid"}';
  const count = posted.length; await receive({ type: 'load-theme' }); assert.strictEqual(posted.length, count);
  assert(errors.at(-1).includes('Invalid design system'));
  console.log('builder host: export, load, CSP, dirty-buffer protection, and message validation pass');
})().catch(error => { console.error(error); process.exitCode = 1; });
