const vscode = acquireVsCodeApi();
const $ = id => document.getElementById(id);
let model = vscode.getState() || { version: 1, elements: [] }, selected = null, history = [], future = [];
let zoom = 1, selectedIds = new Set(), previewActive = false, previewTimer, lastPreviewModel;
function breakpoint() { return $('viewport').value === '375' ? 'mobile' : $('viewport').value === '768' ? 'tablet' : 'desktop'; }
function selection() { return selected ? model.elements.filter(e => selectedIds.has(e.id) || e.id === selected) : []; }
function choose(item, multiple = false) {
  const ids = item.group ? model.elements.filter(e => e.group === item.group).map(e => e.id) : [item.id];
  if (!multiple) selectedIds = new Set(ids);
  else if (ids.every(id => selectedIds.has(id))) ids.forEach(id => selectedIds.delete(id));
  else ids.forEach(id => selectedIds.add(id));
  selected = selectedIds.has(item.id) ? item.id : [...selectedIds].at(-1) || null;
}
function schedulePreview() {
  if (!previewActive) return;
  const snapshot = JSON.stringify(model);
  if (snapshot === lastPreviewModel) return;
  clearTimeout(previewTimer); lastPreviewModel = snapshot;
  previewTimer = setTimeout(() => vscode.postMessage({ type: 'preview-update', model }), 700);
}
function nextId() { return NexoBuilder.allocateId(model); }
function position(value) { return Math.min(10000, Math.max(0, Math.round(value / ($('snap').checked ? 8 : 1)) * ($('snap').checked ? 8 : 1))); }
function checkpoint(notify = true) { if (notify) vscode.postMessage({ type: 'design-changed' }); history.push(JSON.stringify(model)); if (history.length > 100) history.shift(); future = []; }
function current() { return model.elements.find(e => e.id === selected); }
function update(candidate, input, formId) {
  try { NexoBuilder.validate(candidate); } catch (error) { $('status').textContent = error.message; return; }
  checkpoint(); model = candidate;
  const start = input.type === 'text' ? input.selectionStart : null;
  const end = input.type === 'text' ? input.selectionEnd : null;
  render();
  const replacement = $(formId).querySelector(`[name="${input.name}"]`);
  if (replacement) { replacement.focus(); if (input.type === 'text') replacement.setSelectionRange(start, end); }
  $('status').textContent = 'Design updated. Export to save the generated component.';
}
function themeManager() {
  const form = $('theme-properties'); form.replaceChildren();
  const ds = NexoBuilder.designSystem(model);
  const fields = [
    ['mode', 'Theme', ['light', 'dark']], ['fontFamily', 'Font family', NexoBuilder.fonts],
    ...Object.keys(ds.themes[ds.mode]).map(key => [key, key, 'color']),
    ['bodySize', 'Body size', 'number'], ['headingSize', 'Heading size', 'number'],
    ['spacing', 'Padding', 'number'], ['radius', 'Corner radius', 'number'],
  ];
  for (const [key, title, type] of fields) {
    const label = document.createElement('label'); label.textContent = title;
    const input = document.createElement(Array.isArray(type) ? 'select' : 'input'); input.name = key;
    if (Array.isArray(type)) for (const value of type) { const option = document.createElement('option'); option.value = value; option.textContent = value; input.append(option); }
    else { input.type = type; if (type === 'number') { input.min = key.endsWith('Size') ? 8 : 0; input.max = 128; } }
    input.value = type === 'color' ? ds.themes[ds.mode][key] : ds[key];
    input.oninput = () => {
      const candidate = JSON.parse(JSON.stringify(model));
      candidate.designSystem = JSON.parse(JSON.stringify(NexoBuilder.designSystem(model)));
      if (type === 'color') candidate.designSystem.themes[ds.mode][key] = input.value;
      else candidate.designSystem[key] = type === 'number' ? Number(input.value) : input.value;
      update(candidate, input, 'theme-properties');
    };
    label.append(input); form.append(label);
  }
}
function resizeControls(item) {
  const overlay = document.createElement('div'); overlay.className = 'resize-overlay';
  const sync = () => { const box = NexoBuilder.geometry(item, breakpoint()); Object.assign(overlay.style, { left: box.x + 'px', top: box.y + 'px', width: box.width + 'px', height: box.height + 'px' }); };
  sync();
  for (const direction of ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']) {
    const handle = document.createElement('button'); handle.className = 'resize-handle resize-' + direction;
    handle.type = 'button'; handle.setAttribute('aria-label', 'Resize ' + direction.toUpperCase());
    handle.title = 'Drag to resize; use Width and Height in Properties for precise sizes';
    handle.onpointerdown = event => {
      event.preventDefault(); event.stopPropagation(); checkpoint();
      const start = NexoBuilder.geometry(item, breakpoint()), box = { ...start }, sx = event.clientX, sy = event.clientY;
      handle.setPointerCapture(event.pointerId);
      handle.onpointermove = move => {
        const dx = (move.clientX - sx) / zoom, dy = (move.clientY - sy) / zoom;
        if (direction.includes('e')) box.width = Math.max(8, position(start.width + dx));
        if (direction.includes('s')) box.height = Math.max(8, position(start.height + dy));
        if (direction.includes('w')) {
          const right = start.x + start.width;
          box.width = Math.min(right, Math.max(8, right - 10000, position(start.width - dx)));
          box.x = right - box.width;
        }
        if (direction.includes('n')) {
          const bottom = start.y + start.height;
          box.height = Math.min(bottom, Math.max(8, bottom - 10000, position(start.height - dy)));
          box.y = bottom - box.height;
        }
        NexoBuilder.setGeometry(item, breakpoint(), box);
        const element = $('canvas').querySelector('.selected');
        Object.assign(element.style, { left: box.x + 'px', top: box.y + 'px', width: box.width + 'px', height: box.height + 'px' });
        sync(); $('code').textContent = NexoBuilder.generate(model);
        $('status').textContent = `${item.id}: ${box.width} × ${box.height}px`;
      };
      const finish = () => { handle.onpointermove = null; handle.onpointerup = null; handle.onpointercancel = null; render(); };
      handle.onpointerup = finish; handle.onpointercancel = finish;
    };
    overlay.append(handle);
  }
  $('canvas').append(overlay);
}
function render() {
  model.nextElementId = Math.max(model.nextElementId || 1, ...model.elements.map(e => Number(e.id.slice(1)) + 1));
  selectedIds = new Set([...selectedIds].filter(id => model.elements.some(e => e.id === id)));
  if (!model.elements.some(e => e.id === selected)) selected = null;
  if (selected && !selectedIds.has(selected)) selectedIds = new Set([selected]);
  vscode.setState(model);
  schedulePreview();
  $('apply-source').disabled = !model.sourceToken;
  $('reset-breakpoint').disabled = breakpoint() === 'desktop';
  renderLibrary();
  themeManager();
  const ds = NexoBuilder.designSystem(model);
  $('canvas').style.background = model.designSystem ? ds.themes[ds.mode].background : '#ffffff';
  $('canvas').replaceChildren(); $('layers').replaceChildren(); $('properties').replaceChildren();
  for (const item of model.elements) {
    const element = document.createElement(item.tag);
    if (item.tag === 'input') { element.placeholder = item.text; element.readOnly = true; } else element.textContent = item.text;
    element.dataset.id = item.id;
    const box = NexoBuilder.geometry(item, breakpoint());
    element.className = 'element' + (selection().some(e => e.id === item.id) ? ' selected' : '');
    Object.assign(element.style, { left: box.x + 'px', top: box.y + 'px', width: box.width + 'px', height: box.height + 'px', ...NexoBuilder.elementStyle(model, item) });
    const resolved = NexoBuilder.elementStyle(model, item);
    for (const key of ['fontSize', 'padding', 'borderRadius']) if (resolved[key] !== undefined) element.style[key] = resolved[key] + 'px';
    if (item.component) {
      element.style.background = model.designSystem ? ds.themes[ds.mode].background : '#ffffff';
      const definition = model.components.find(c => c.name === item.component);
      element.replaceChildren();
      for (const child of definition.elements) {
        const node = document.createElement(child.tag);
        if (child.tag === 'input') { node.placeholder = item.props[child.id] ?? child.text; node.readOnly = true; }
        else node.textContent = item.props[child.id] ?? child.text;
        const style = NexoBuilder.elementStyle(model, child);
        Object.assign(node.style, { position: 'absolute', pointerEvents: 'none', boxSizing: 'border-box', margin: '0', overflow: 'hidden', left: child.x + 'px', top: child.y + 'px', width: child.width + 'px', height: child.height + 'px', ...style });
        for (const key of ['fontSize', 'padding', 'borderRadius']) if (style[key] !== undefined) node.style[key] = style[key] + 'px';
        element.append(node);
      }
    }
    element.onclick = event => event.preventDefault();
    element.onpointerdown = event => {
      event.preventDefault();
      if (event.shiftKey || !selection().some(e => e.id === item.id)) choose(item, !!event.shiftKey);
      else selected = item.id;
      checkpoint();
      const sx = event.clientX, sy = event.clientY, originals = selection().map(e => ({ item: e, box: NexoBuilder.geometry(e, breakpoint()) }));
      element.setPointerCapture(event.pointerId);
      for (const child of $('canvas').querySelectorAll('.element')) child.classList.toggle('selected', originals.some(e => e.item.id === child.dataset.id));
      element.onpointermove = move => {
        if (!originals.length) return;
        const anchor = originals.find(e => e.item.id === item.id).box;
        const dx = Math.max(-Math.min(...originals.map(e => e.box.x)), Math.min(10000 - Math.max(...originals.map(e => e.box.x)), position(anchor.x + (move.clientX - sx) / zoom) - anchor.x));
        const dy = Math.max(-Math.min(...originals.map(e => e.box.y)), Math.min(10000 - Math.max(...originals.map(e => e.box.y)), position(anchor.y + (move.clientY - sy) / zoom) - anchor.y));
        for (const entry of originals) {
          const values = { x: entry.box.x + dx, y: entry.box.y + dy };
          NexoBuilder.setGeometry(entry.item, breakpoint(), values);
          const node = $('canvas').querySelector(`[data-id="${entry.item.id}"]`);
          node.style.left = values.x + 'px'; node.style.top = values.y + 'px';
        }
        $('canvas').querySelector('.resize-overlay')?.remove();
        $('code').textContent = NexoBuilder.generate(model);
      };
      const finish = () => { element.onpointermove = null; render(); };
      element.onpointerup = finish; element.onpointercancel = finish;
    };
    $('canvas').append(element);
    const layer = document.createElement('button'); layer.textContent = `${item.component || item.tag} · ${item.id}${item.group ? ' · ' + item.group : ''}`;
    layer.setAttribute('aria-pressed', String(selection().some(e => e.id === item.id)));
    layer.onclick = event => { choose(item, !!event.shiftKey); render(); }; $('layers').append(layer);
  }
  const item = current();
  if (item && selection().length === 1) resizeControls(item);
  if (item) {
    const label = document.createElement('label'); label.textContent = 'Use design system';
    const input = document.createElement('input'); input.type = 'checkbox'; input.name = 'useTheme'; input.checked = !!item.useTheme;
    input.onchange = () => {
      const candidate = JSON.parse(JSON.stringify(model));
      const target = candidate.elements.find(e => e.id === selected);
      if (!input.checked) { const style = NexoBuilder.elementStyle(model, item); target.color = style.color; target.background = style.background; }
      target.useTheme = input.checked;
      if (input.checked && !candidate.designSystem) candidate.designSystem = NexoBuilder.defaultDesignSystem();
      update(candidate, input, 'properties');
    };
    label.append(input); $('properties').append(label);
  }
  if (item) for (const key of ['text', 'x', 'y', 'width', 'height', 'color', 'background']) {
    if (key === 'text' && item.component) continue;
    const label = document.createElement('label'); label.textContent = key;
    const input = document.createElement('input'); input.name = key;
    input.type = ['color', 'background'].includes(key) ? 'color' : key === 'text' ? 'text' : 'number';
    input.value = ['color', 'background'].includes(key) ? NexoBuilder.elementStyle(model, item)[key] : ['x', 'y', 'width', 'height'].includes(key) ? NexoBuilder.geometry(item, breakpoint())[key] : item[key]; if (input.type === 'number') { input.min = 0; input.max = 10000; }
    input.oninput = () => {
      const candidate = JSON.parse(JSON.stringify(model)); const value = input.type === 'number' ? Number(input.value) : input.value;
      if (['x', 'y', 'width', 'height'].includes(key)) NexoBuilder.setGeometry(candidate.elements.find(e => e.id === selected), breakpoint(), { [key]: value });
      else candidate.elements.find(e => e.id === selected)[key] = value;
      const target = candidate.elements.find(e => e.id === selected);
      if (['color', 'background'].includes(key)) {
        const resolved = NexoBuilder.elementStyle(model, item);
        target.color = key === 'color' ? input.value : resolved.color;
        target.background = key === 'background' ? input.value : resolved.background;
        target.useTheme = false;
      }
      update(candidate, input, 'properties');
    };
    label.append(input); $('properties').append(label);
  }
  if (item?.component) {
    const definition = model.components.find(c => c.name === item.component);
    for (const child of definition.elements) {
      const label = document.createElement('label'); label.textContent = 'Text · ' + child.id;
      const input = document.createElement('input'); input.name = 'prop_' + child.id; input.type = 'text'; input.value = item.props[child.id] ?? child.text;
      input.oninput = () => { const candidate = JSON.parse(JSON.stringify(model)); candidate.elements.find(e => e.id === item.id).props[child.id] = input.value; update(candidate, input, 'properties'); };
      label.append(input); $('properties').append(label);
    }
  }
  for (const button of $('arrange').querySelectorAll('button')) button.disabled = selection().length < (button.dataset.action.startsWith('distribute') ? 3 : 2);
  $('extract-component').disabled = !selection().length || selection().some(e => e.component);
  const index = model.elements.findIndex(e => e.id === selected);
  $('duplicate').disabled = !item || model.elements.length >= 500;
  $('backward').disabled = index <= 0;
  $('forward').disabled = index < 0 || index === model.elements.length - 1;
  $('canvas-info').textContent = `${$('viewport').value}px viewport · ${model.elements.length} elements${item ? ' · Selected ' + selection().length : ''}`;
  $('code').textContent = NexoBuilder.generate(model);
  $('undo').disabled = !history.length; $('redo').disabled = !future.length; $('delete').disabled = !item;
}
function renderLibrary() {
  const list = $('components'); list.replaceChildren();
  for (const definition of model.components || []) {
    const button = document.createElement('button'); button.textContent = '+ ' + definition.name;
    button.onclick = () => {
      if (model.elements.length >= 500) return;
      checkpoint();
      const width = Math.max(...definition.elements.map(e => e.x + e.width)), height = Math.max(...definition.elements.map(e => e.y + e.height));
      const item = { id: nextId(), tag: 'div', text: '', x: 40, y: 40, width, height, color: '#172033', background: '#ffffff', component: definition.name, props: Object.fromEntries(definition.elements.map(e => [e.id, e.text])) };
      model.elements.push(item); selected = item.id; selectedIds = new Set([item.id]); render();
    };
    list.append(button);
    const details = document.createElement('details'), title = document.createElement('summary'); title.textContent = 'Edit defaults · ' + definition.name; details.append(title);
    for (const child of definition.elements) {
      const label = document.createElement('label'); label.textContent = child.id;
      const input = document.createElement('input'); input.type = 'text'; input.value = child.text;
      input.onchange = () => {
        try {
          const candidate = JSON.parse(JSON.stringify(model));
          candidate.components.find(c => c.name === definition.name).elements.find(e => e.id === child.id).text = input.value;
          for (const item of candidate.elements.filter(e => e.component === definition.name && e.props[child.id] === child.text)) item.props[child.id] = input.value;
          NexoBuilder.validate(candidate); checkpoint(); model = candidate; render();
        } catch (error) { $('status').textContent = error.message; }
      };
      label.append(input); details.append(label);
    }
    list.append(details);
  }
}
$('extract-component').onclick = () => {
  try {
    const candidate = NexoBuilder.extractComponent(model, selection().map(e => e.id), $('component-name').value.trim());
    checkpoint(); model = candidate; selected = model.elements.find(e => e.component === $('component-name').value.trim()).id; selectedIds = new Set([selected]); render();
  } catch (error) { $('status').textContent = error.message; }
};
for (const button of $('arrange').querySelectorAll('button')) button.onclick = () => {
  try { const candidate = NexoBuilder.transform(model, selection().map(e => e.id), button.dataset.action, breakpoint()); checkpoint(); model = candidate; render(); }
  catch (error) { $('status').textContent = error.message; }
};
$('reset-breakpoint').onclick = () => {
  if (breakpoint() === 'desktop') return; checkpoint();
  const items = selection().length ? selection() : model.elements;
  for (const item of items) if (item.layouts) delete item.layouts[breakpoint()]; render();
};
$('apply-source').onclick = () => vscode.postMessage({ type: 'apply-source', model });
$('reload-source').onclick = () => vscode.postMessage({ type: 'reload-source' });
$('preview').onclick = () => { lastPreviewModel = JSON.stringify(model); vscode.postMessage({ type: 'preview', model }); };
$('stop-preview').onclick = () => { previewActive = false; clearTimeout(previewTimer); vscode.postMessage({ type: 'stop-preview' }); };
for (const tag of NexoBuilder.tags) {
  const button = document.createElement('button'); button.textContent = '+ ' + tag;
  button.onclick = () => {
    if (model.elements.length >= 500) { $('status').textContent = 'Designs support up to 500 elements.'; return; }
    checkpoint(); if (!model.designSystem) model.designSystem = NexoBuilder.defaultDesignSystem(); const id = nextId();
    model.elements.push({ id, tag, useTheme: true, text: tag === 'div' ? '' : tag === 'h1' ? 'Your next idea' : tag === 'button' ? 'Get started' : tag === 'input' ? 'Enter text' : 'Write something here', x: 40, y: Math.min(9900, Math.max(24, ...model.elements.map(e => e.y + e.height)) + 16), width: tag === 'h1' ? 400 : 240, height: tag === 'h1' ? 80 : tag === 'h2' ? 72 : tag === 'div' ? 120 : tag === 'p' ? 64 : 48, color: '#172033', background: '#ffffff' }); selected = id; render();
  }; $('palette').append(button);
}
$('viewport').onchange = () => { $('canvas').style.width = $('viewport').value + 'px'; render(); $('status').textContent = 'Editing ' + breakpoint() + ' layout. Position and size changes generate responsive CSS.'; };
$('zoom').onchange = () => { zoom = Number($('zoom').value); $('canvas').style.zoom = String(zoom); };
$('snap').onchange = () => $('workspace').classList.toggle('snap-grid', $('snap').checked);
$('duplicate').onclick = () => {
  const item = current(); if (!item || model.elements.length >= 500) return;
  checkpoint(); const copy = { ...JSON.parse(JSON.stringify(item)), id: nextId(), x: Math.min(10000, item.x + 16), y: Math.min(10000, item.y + 16) };
  model.elements.push(copy); selected = copy.id; selectedIds = new Set([copy.id]); render();
};
for (const [id, delta] of [['backward', -1], ['forward', 1]]) $(id).onclick = () => {
  const index = model.elements.findIndex(e => e.id === selected), target = index + delta;
  if (index < 0 || target < 0 || target >= model.elements.length) return;
  checkpoint(); [model.elements[index], model.elements[target]] = [model.elements[target], model.elements[index]]; render();
};
window.addEventListener('keydown', event => {
  if (event.target.closest?.('input, select, textarea, [contenteditable="true"]')) return;
  const modifier = event.ctrlKey || event.metaKey, key = event.key.toLowerCase();
  if (modifier && ['z', 'y', 'd'].includes(key)) {
    event.preventDefault(); $(key === 'd' ? 'duplicate' : key === 'y' || event.shiftKey ? 'redo' : 'undo').click(); return;
  }
  const item = current(); if (!item) return;
  if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); $('delete').click(); return; }
  const directions = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  const direction = directions[event.key]; if (!direction) return;
  event.preventDefault(); checkpoint(); const step = event.shiftKey ? 10 : 1;
  const items = selection(), boxes = items.map(e => NexoBuilder.geometry(e, breakpoint()));
  const dx = Math.max(-Math.min(...boxes.map(e => e.x)), Math.min(10000 - Math.max(...boxes.map(e => e.x)), direction[0] * step));
  const dy = Math.max(-Math.min(...boxes.map(e => e.y)), Math.min(10000 - Math.max(...boxes.map(e => e.y)), direction[1] * step));
  items.forEach((e, index) => NexoBuilder.setGeometry(e, breakpoint(), { x: boxes[index].x + dx, y: boxes[index].y + dy })); render();
});
$('theme-properties').onsubmit = e => e.preventDefault();
$('apply-theme').onclick = () => {
  checkpoint(); if (!model.designSystem) model.designSystem = NexoBuilder.defaultDesignSystem();
  for (const item of model.elements) item.useTheme = true;
  render(); $('status').textContent = 'Design system applied to all elements. Undo to restore overrides.';
};
$('properties').onsubmit = e => e.preventDefault();
$('delete').onclick = () => { if (!current()) return; checkpoint(); const ids = selection().map(e => e.id); model.elements = model.elements.filter(e => !ids.includes(e.id)); selected = null; selectedIds.clear(); render(); };
$('undo').onclick = () => { if (!history.length) return; vscode.postMessage({ type: 'design-changed' }); future.push(JSON.stringify(model)); model = JSON.parse(history.pop()); render(); };
$('redo').onclick = () => { if (!future.length) return; vscode.postMessage({ type: 'design-changed' }); history.push(JSON.stringify(model)); model = JSON.parse(future.pop()); render(); };
$('load-theme').onclick = () => vscode.postMessage({ type: 'load-theme' });
$('save-theme').onclick = () => vscode.postMessage({ type: 'save-theme', designSystem: NexoBuilder.designSystem(model) });
for (const type of ['save', 'export', 'load']) $(type).onclick = () => vscode.postMessage({ type, model });
window.addEventListener('message', ({ data }) => {
  if (data.type === 'load-source') {
    try { NexoBuilder.validate(data.model); checkpoint(false); model = data.model; selected = null; selectedIds.clear(); render(); $('status').textContent = 'Loaded ' + data.name + (data.warnings.length ? ' · Partial import: ' + data.warnings.join(' ') : ' · Source file unchanged.'); }
    catch (error) { $('status').textContent = error.message; }
  }
  if (data.type === 'load') { try { NexoBuilder.validate(data.model); checkpoint(); model = data.model; delete model.sourceToken; selected = null; selectedIds.clear(); render(); } catch (error) { $('status').textContent = error.message; } }
  if (data.type === 'theme') {
    try { NexoBuilder.validateDesignSystem(data.designSystem); checkpoint(); model.designSystem = data.designSystem; render(); $('status').textContent = 'Design system imported. Use Apply theme to bind custom elements.'; }
    catch (error) { $('status').textContent = error.message; }
  }
  if (data.type === 'preview-state') { previewActive = data.active; $('stop-preview').disabled = !data.active; $('status').textContent = data.text; }
  if (data.type === 'status') $('status').textContent = data.text;
});
render();
vscode.postMessage({ type: 'ready' });
