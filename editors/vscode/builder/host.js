const vscode = require('vscode');
const { randomBytes } = require('crypto');
const { validate, generate, validateDesignSystem } = require('./model');

function openBuilder(context) {
  const root = vscode.Uri.joinPath(context.extensionUri, 'builder');
  const panel = vscode.window.createWebviewPanel('nexo.builder', 'Nexo Web Builder', vscode.ViewColumn.One, {
    enableScripts: true, localResourceRoots: [root], retainContextWhenHidden: true,
  });
  const nonce = randomBytes(16).toString('hex');
  const asset = name => panel.webview.asWebviewUri(vscode.Uri.joinPath(root, name));
  panel.webview.html = `<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${panel.webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';"><link rel="stylesheet" href="${asset('style.css')}"></head><body>
    <header><strong>NEXO / Web Builder</strong><span>Wireframe → Nexo</span><button id="load">Open design</button><button id="save">Save design</button><button id="export">Export .nexo</button></header>
    <main><aside><h3>Elements</h3><div id="palette"></div><h3>Layers</h3><div id="layers"></div><button id="delete">Delete selected</button><button id="undo">Undo</button><button id="redo">Redo</button></aside>
    <section id="workspace"><div id="canvas" aria-label="Design canvas"></div></section>
    <aside id="inspector"><details id="design-system" open><summary>Design System Manager</summary><form id="theme-properties"></form><button id="load-theme">Import design system</button><button id="save-theme">Export design system</button><button id="apply-theme">Apply theme to all elements</button></details><h3>Properties</h3><form id="properties"></form><h3>Live Nexo</h3><pre id="code"></pre></aside></main><footer id="status" role="status">Add an element to start designing.</footer>
    <script nonce="${nonce}" src="${asset('model.js')}"></script><script nonce="${nonce}" src="${asset('view.js')}"></script></body></html>`;
  const listener = panel.webview.onDidReceiveMessage(async message => {
    try {
      if (message.type === 'load' || message.type === 'load-theme') {
        const files = await vscode.window.showOpenDialog({ canSelectMany: false, filters: { 'Nexo design': ['json'] } });
        if (!files) return;
        const value = JSON.parse(Buffer.from(await vscode.workspace.fs.readFile(files[0])).toString('utf8'));
        if (message.type === 'load-theme') await panel.webview.postMessage({ type: 'theme', designSystem: validateDesignSystem(value) });
        else await panel.webview.postMessage({ type: 'load', model: validate(value) });
      } else if (message.type === 'save' || message.type === 'export' || message.type === 'save-theme') {
        const themeOnly = message.type === 'save-theme';
        const model = themeOnly ? validateDesignSystem(message.designSystem) : validate(message.model);
        const exporting = message.type === 'export';
        const uri = await vscode.window.showSaveDialog({ filters: exporting ? { Nexo: ['nexo'] } : { 'Nexo design': ['json'] }, saveLabel: exporting ? 'Export Nexo component' : themeOnly ? 'Export design system' : 'Save design' });
        if (!uri) return;
        const content = exporting ? generate(model) : JSON.stringify(model, null, 2) + '\n';
        // WorkspaceEdit respects dirty editor buffers and normal VS Code undo/save behavior.
        const document = await vscode.workspace.openTextDocument(uri).then(d => d, async error => {
          if (error.code !== 'FileNotFound' && error.code !== 'ENOENT') throw error;
          await vscode.workspace.fs.writeFile(uri, new Uint8Array());
          return vscode.workspace.openTextDocument(uri);
        });
        if (document.isDirty) throw Error('Save or close the existing unsaved file before exporting over it.');
        const edit = new vscode.WorkspaceEdit();
        edit.replace(uri, new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)), content);
        if (!await vscode.workspace.applyEdit(edit) || !await document.save()) throw Error('Could not save the design.');
        if (exporting) await vscode.window.showTextDocument(document, vscode.ViewColumn.Beside);
        await panel.webview.postMessage({ type: 'status', text: 'Saved ' + uri.fsPath });
      }
    } catch (error) { vscode.window.showErrorMessage('Nexo Builder: ' + error.message); }
  });
  panel.onDidDispose(() => listener.dispose());
  context.subscriptions.push(panel, listener);
}
module.exports = { openBuilder };
