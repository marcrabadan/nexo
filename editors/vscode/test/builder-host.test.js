const assert = require('assert');
const Module = require('module');
let receive, saved = '', applied = false, dirty = false, created = 0, disposed, revealed = 0;
let panelOptions, sourceChanged, editTarget;
let sourceText = 'use nexo.web\npub component App:\n    state count: int = 0\n    fn increment():\n        count += 1\n    view:\n        h1("Title")\n        button("Add", on_click: increment)\n';
const sourceUri = { fsPath: '/source/sync.nexo' };
const linkedDocument = { fileName: sourceUri.fsPath, uri: sourceUri, getText: () => sourceText, positionAt: n => n };
const errors = [], posted = [];
let fileContent = '{"version":1,"elements":[]}';
const uri = { fsPath: '/design/app.nexo' };
const panel = {
  webview: { cspSource: 'vscode-webview:', asWebviewUri: u => u.fsPath,
    onDidReceiveMessage: callback => { receive = callback; return { dispose() {} }; },
    postMessage: async message => posted.push(message) },
  onDidDispose(fn) { disposed = fn; }, dispose() {}, reveal() { revealed++; },
};
const document = { get isDirty() { return dirty; }, getText: () => 'old', positionAt: n => n, save: async () => true };
const vscode = {
  Uri: { joinPath: (root, ...parts) => ({ fsPath: root.fsPath + '/' + parts.join('/') }) },
  ViewColumn: { One: 1, Beside: 2 },
  Range: class { constructor(start, end) { this.start = start; this.end = end; } },
  WorkspaceEdit: class { replace(target, range, text) { saved = text; editTarget = target; } },
  window: { createWebviewPanel: (...args) => { created++; panelOptions = args[2]; return panel; }, showSaveDialog: async () => uri, showOpenDialog: async () => [uri], showTextDocument: async () => {}, showErrorMessage: text => errors.push(text) },
  workspace: { onDidChangeTextDocument: fn => (sourceChanged = fn, { dispose() {} }), openTextDocument: async () => document, applyEdit: async () => { applied = true; if (editTarget === sourceUri) sourceText = saved; return true; }, fs: { readFile: async () => Buffer.from(fileContent) } },
};
const load = Module._load;
Module._load = function (name, ...rest) { return name === 'vscode' ? vscode : load.call(this, name, ...rest); };
const { openBuilder } = require('../builder/host');
Module._load = load;
(async () => {
  openBuilder({ extensionUri: { fsPath: '/extension' }, subscriptions: [] });
  openBuilder({ subscriptions: [] }, { automatic: true });
  assert.strictEqual(created, 1); assert.strictEqual(revealed, 0);
  openBuilder({ subscriptions: [] }); assert.strictEqual(revealed, 1);
  disposed();
  openBuilder({ extensionUri: { fsPath: '/extension' }, subscriptions: [] }, { automatic: true });
  assert.strictEqual(created, 2);
  assert.deepStrictEqual(panelOptions, { viewColumn: 2, preserveFocus: true });
  assert(panel.webview.html.includes("default-src 'none'"));
  const sourceDocument = { fileName: '/source/app.nexo', getText: () => 'use nexo.web\npub component App:\n    view:\n        h1("Loaded title")' };
  openBuilder({ subscriptions: [] }, { automatic: true, document: sourceDocument });
  assert.strictEqual(posted.length, 0); // Wait for webview readiness.
  await receive({ type: 'ready' });
  assert.strictEqual(posted.at(-1).type, 'load-source');
  assert.strictEqual(posted.at(-1).model.elements[0].text, 'Loaded title');
  openBuilder({ subscriptions: [] }, { automatic: true, document: { ...sourceDocument, getText: () => 'use nexo.web' } });
  assert.strictEqual(posted.at(-1).type, 'status');
  assert(posted.at(-1).text.includes('Cannot import'));
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
  const count = posted.length; await receive({ type: 'load-theme' }); assert.strictEqual(posted.length, count + 1); assert.strictEqual(posted.at(-1).type, 'status');
  assert(errors.at(-1).includes('Invalid design system'));
  openBuilder({ subscriptions: [] }, { automatic: true, document: linkedDocument });
  const linkedModel = JSON.parse(JSON.stringify(posted.at(-1).model));
  assert(linkedModel.sourceToken);
  linkedModel.elements[0].text = 'Updated from canvas';
  await receive({ type: 'apply-source', model: linkedModel });
  assert(sourceText.includes('Updated from canvas'));
  assert(sourceText.includes('on_click: increment'));
  assert(sourceText.includes('count += 1'));
  assert(posted.at(-1).text.includes('applied to source'));
  await receive({ type: 'design-changed' });
  sourceText += '\n# concurrent editor change\n';
  sourceChanged({ document: linkedDocument });
  assert(posted.at(-1).text.includes('Source changed'));
  applied = false;
  await receive({ type: 'apply-source', model: linkedModel });
  assert.strictEqual(applied, false);
  assert(errors.at(-1).includes('Source changed'));
  await receive({ type: 'reload-source' });
  assert.strictEqual(posted.at(-1).type, 'load-source');
  assert.notStrictEqual(posted.at(-1).model.sourceToken, linkedModel.sourceToken);
  await receive({ type: 'apply-source', model: linkedModel });
  assert(errors.at(-1).includes('another source revision'));
  console.log('builder host: export, load, CSP, dirty-buffer protection, and message validation pass');
})().catch(error => { console.error(error); process.exitCode = 1; });
