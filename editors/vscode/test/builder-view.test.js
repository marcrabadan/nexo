const assert = require('assert');
const fs = require('fs');
const Module = require('module');
const { JSDOM } = require('jsdom');
let panel = { webview: { cspSource: 'webview:', asWebviewUri: uri => uri.fsPath, onDidReceiveMessage: () => ({ dispose() {} }) }, onDidDispose() {} };
const load = Module._load;
Module._load = function (name, ...args) {
  if (name === 'vscode') return {
    Uri: { joinPath: (root, ...parts) => ({ fsPath: root.fsPath + '/' + parts.join('/') }) },
    ViewColumn: { One: 1 }, window: { createWebviewPanel: () => panel },
  };
  return load.call(this, name, ...args);
};
require('../builder/host').openBuilder({ extensionUri: { fsPath: '/extension' }, subscriptions: [] });
Module._load = load;
const dom = new JSDOM(panel.webview.html, { runScripts: 'outside-only' });
const { window } = dom, doc = window.document;
let state; const messages = [];
window.acquireVsCodeApi = () => ({ getState: () => undefined, setState: value => { state = JSON.parse(JSON.stringify(value)); }, postMessage: message => messages.push(message) });
for (const file of ['model.js', 'view.js']) window.eval(fs.readFileSync(require.resolve('../builder/' + file), 'utf8'));
const click = id => doc.getElementById(id).click();
function edit(form, name, value) {
  const input = doc.querySelector(`#${form} [name="${name}"]`);
  input.value = value; input.dispatchEvent(new window.Event('input', { bubbles: true }));
}
function receive(data) { window.dispatchEvent(new window.MessageEvent('message', { data })); }
doc.querySelectorAll('#palette button')[3].click();
assert.strictEqual(state.elements[0].useTheme, true);
edit('theme-properties', 'primary', '#cc3300');
assert.strictEqual(doc.querySelector('.element').style.background, 'rgb(204, 51, 0)');
assert(doc.getElementById('code').textContent.includes('background: #cc3300;'));
edit('theme-properties', 'radius', '20');
assert.strictEqual(doc.querySelector('.element').style.borderRadius, '20px');
edit('theme-properties', 'fontFamily', 'serif');
assert.strictEqual(doc.querySelector('.element').style.fontFamily, 'serif');
edit('theme-properties', 'mode', 'dark');
assert.strictEqual(doc.querySelector('.element').style.background, 'rgb(165, 158, 255)');
edit('properties', 'background', '#123456');
assert.strictEqual(state.elements[0].useTheme, false);
edit('theme-properties', 'primary', '#ff0000');
assert.strictEqual(doc.querySelector('.element').style.background, 'rgb(18, 52, 86)');
click('apply-theme'); assert.strictEqual(doc.querySelector('.element').style.background, 'rgb(255, 0, 0)');
click('undo'); assert.strictEqual(state.elements[0].useTheme, false);
click('redo'); assert.strictEqual(state.elements[0].useTheme, true);
click('save'); assert.strictEqual(messages.at(-1).model.designSystem.mode, 'dark');
click('save-theme'); assert.strictEqual(messages.at(-1).designSystem.mode, 'dark');
const imported = window.NexoBuilder.defaultDesignSystem(); imported.themes.light.primary = '#008800';
receive({ type: 'theme', designSystem: imported });
assert.strictEqual(doc.querySelector('.element').style.background, 'rgb(0, 136, 0)');
const legacy = { version: 1, elements: [{ id: 'n1', tag: 'button', text: 'Legacy', x: 0, y: 0, width: 200, height: 48, color: '#000000', background: '#ffffff' }] };
receive({ type: 'load', model: legacy });
assert.strictEqual(doc.querySelector('.element').style.background, 'rgb(255, 255, 255)');
click('apply-theme'); assert.strictEqual(state.elements[0].useTheme, true);
click('undo'); assert.strictEqual(state.designSystem, undefined);
console.log('builder view: live theme edits, custom overrides, apply, undo/redo, persistence, import and legacy designs pass');
window.close();
