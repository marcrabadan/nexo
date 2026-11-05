const vscode = require('vscode');
const { randomBytes } = require('crypto');
const { validate, generate, validateDesignSystem } = require('./model');
const { importSource } = require('./import');
const { applySource } = require('./sync');
const { withMetadata } = require('./metadata');
const { Preview } = require('./preview');
const path = require('path');

let builderPanel, loadSource;
function loadDocument(document) { if (loadSource && document) loadSource(document); }
function openBuilder(context, { automatic = false, document } = {}) {
  if (builderPanel) {
    loadDocument(document);
    if (!automatic) builderPanel.reveal(vscode.ViewColumn.Beside);
    return builderPanel;
  }
  const root = vscode.Uri.joinPath(context.extensionUri, 'builder');
  const panel = vscode.window.createWebviewPanel('nexo.builder', 'Nexo Web Builder', { viewColumn: vscode.ViewColumn.Beside, preserveFocus: automatic }, {
    enableScripts: true, localResourceRoots: [root], retainContextWhenHidden: true,
  });
  builderPanel = panel;
  const nonce = randomBytes(16).toString('hex');
  const asset = name => panel.webview.asWebviewUri(vscode.Uri.joinPath(root, name));
  panel.webview.html = `<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${panel.webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';"><link rel="stylesheet" href="${asset('style.css')}"></head><body>
    <header><strong>NEXO / Web Builder</strong><span>Wireframe → Nexo</span><button id="apply-source" disabled>Apply to source</button><button id="reload-source">Reload source</button><button id="preview">Live preview</button><button id="stop-preview" disabled>Stop preview</button><button id="load">Open design</button><button id="save">Save design</button><button id="export">Export .nexo</button></header>
    <main><aside><h3>Elements</h3><div id="palette"></div><h3>Reusable components</h3><label>Name<input id="component-name" value="BuilderCard" aria-label="Component name"></label><button id="extract-component">Create from selection</button><div id="components"></div><h3>Arrange selection</h3><p>Shift+click to select multiple</p><div id="arrange"><button data-action="left">Align left</button><button data-action="center">Align center</button><button data-action="right">Align right</button><button data-action="top">Align top</button><button data-action="middle">Align middle</button><button data-action="bottom">Align bottom</button><button data-action="distribute-x">Distribute horizontally</button><button data-action="distribute-y">Distribute vertically</button><button data-action="group">Group</button><button data-action="ungroup">Ungroup</button></div><h3>Layers</h3><div id="layers"></div><button id="duplicate">Duplicate selected</button><button id="backward">Send backward</button><button id="forward">Bring forward</button><button id="delete">Delete selected</button><button id="undo">Undo</button><button id="redo">Redo</button></aside>
    <section id="workspace"><div id="canvas-tools"><label>Viewport<select id="viewport"><option value="960">Desktop · 960px</option><option value="768">Tablet · 768px</option><option value="375">Mobile · 375px</option></select></label><button id="reset-breakpoint">Reset breakpoint overrides</button><label>Zoom<select id="zoom"><option value="0.5">50%</option><option value="0.75">75%</option><option value="1" selected>100%</option><option value="1.25">125%</option></select></label><label><input id="snap" type="checkbox">Snap to 8px grid</label></div><p id="canvas-info" role="status"></p><div id="canvas" tabindex="0" aria-label="Design canvas"></div></section>
    <aside id="inspector"><details id="design-system" open><summary>Design System Manager</summary><form id="theme-properties"></form><button id="load-theme">Import design system</button><button id="save-theme">Export design system</button><button id="apply-theme">Apply theme to all elements</button></details><h3>Properties</h3><form id="properties"></form><h3>Live Nexo</h3><pre id="code"></pre></aside></main><footer id="status" role="status">Add an element to start designing.</footer>
    <script nonce="${nonce}" src="${asset('model.js')}"></script><script nonce="${nonce}" src="${asset('view.js')}"></script></body></html>`;
  let ready = false, pending, sourceSession, designDirty = false, applying = false, preview;
  const projectSource = model => {
    validate(model);
    if (!model.sourceToken) return generate(model);
    if (!sourceSession || model.sourceToken !== sourceSession.token) throw Error('This design belongs to another source revision. Reload the source before applying.');
    if (sourceSession.document.getText() !== sourceSession.text) throw Error('Source changed since import. Reload source before applying canvas changes.');
    return applySource(sourceSession.text, model);
  };
  const deliver = () => { if (ready && pending) { panel.webview.postMessage(pending); pending = undefined; } };
  loadSource = sourceDocument => {
    try {
      const text = sourceDocument.getText();
      if (sourceSession?.document === sourceDocument && sourceSession.text === text) return;
      const imported = importSource(text);
      const token = randomBytes(16).toString('hex'); imported.model.sourceToken = token;
      sourceSession = { document: sourceDocument, text, token }; designDirty = false;
      pending = { type: 'load-source', ...imported, name: sourceDocument.fileName };
    } catch (error) { pending = { type: 'status', text: 'Cannot import ' + sourceDocument.fileName + ': ' + error.message }; }
    deliver();
  };
  loadDocument(document);
  const listener = panel.webview.onDidReceiveMessage(async message => {
    try {
      if (message.type === 'design-changed') { designDirty = true; return; }
      if (message.type === 'reload-source') {
        if (!sourceSession) throw Error('Open a Nexo web source file first.');
        const document = sourceSession.document; sourceSession = undefined; loadSource(document); return;
      }
      if (message.type === 'apply-source') {
        if (!message.model?.sourceToken) throw Error('This design is not linked to a source file. Export it instead.');
        const text = projectSource(message.model), document = sourceSession.document;
        const edit = new vscode.WorkspaceEdit();
        edit.replace(document.uri, new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)), text);
        applying = true;
        try {
          if (!await vscode.workspace.applyEdit(edit)) throw Error('Could not apply source changes.');
          sourceSession.text = document.getText(); designDirty = false;
        } finally { applying = false; }
        await panel.webview.postMessage({ type: 'status', text: 'Canvas changes applied to source. Use normal editor Save and Undo.' });
        return;
      }
      if (message.type === 'preview' || message.type === 'preview-update') {
        if (vscode.workspace.isTrusted === false) throw Error('Trust the workspace before running its compiler preview.');
        if (message.type === 'preview-update' && !preview) return;
        const source = projectSource(message.model);
        if (!preview) preview = new Preview({ command: vscode.workspace.getConfiguration('nexo').get('path') || 'nexo', directory: sourceSession?.document.uri && path.isAbsolute(sourceSession.document.fileName) ? path.dirname(sourceSession.document.fileName) : undefined });
        preview.directory = message.model.sourceToken && sourceSession?.document.uri && path.isAbsolute(sourceSession.document.fileName) ? path.dirname(sourceSession.document.fileName) : undefined;
        const running = preview;
        try {
          const url = await running.update(source);
          if (preview !== running) return;
          if (message.type === 'preview') await vscode.env.openExternal(vscode.Uri.parse(url));
          await panel.webview.postMessage({ type: 'preview-state', active: true, text: 'Interactive preview running. Canvas changes rebuild automatically.' });
        } catch (error) {
          if (preview !== running) return;
          const active = !!running.url;
          await panel.webview.postMessage({ type: 'preview-state', active, text: 'Preview build failed: ' + error.message });
        }
        return;
      }
      if (message.type === 'stop-preview') {
        const previous = preview; preview = undefined; if (previous) await previous.dispose();
        await panel.webview.postMessage({ type: 'preview-state', active: false, text: 'Preview stopped.' }); return;
      }
      if (message.type === 'ready') { ready = true; deliver(); return; }
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
        const content = exporting ? withMetadata(generate(model), model) : JSON.stringify(model, null, 2) + '\n';
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
    } catch (error) { await panel.webview.postMessage({ type: 'status', text: error.message }); vscode.window.showErrorMessage('Nexo Builder: ' + error.message); }
  });
  const sourceChanges = vscode.workspace?.onDidChangeTextDocument?.(event => {
    if (applying || !sourceSession || event.document !== sourceSession.document) return;
    if (!designDirty) { loadSource(event.document); return; }
    panel.webview.postMessage({ type: 'status', text: 'Source changed. Reload source before applying canvas changes; save the design to keep your canvas edits.' });
  });
  panel.onDidDispose(() => { const previous = preview; preview = undefined; if (previous) previous.dispose().catch(() => {}); sourceChanges?.dispose(); builderPanel = undefined; loadSource = undefined; listener.dispose(); });
  context.subscriptions.push(panel, listener);
  return panel;
}
module.exports = { openBuilder, loadDocument };
