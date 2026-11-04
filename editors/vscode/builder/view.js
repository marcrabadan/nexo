const vscode = acquireVsCodeApi();
const $ = id => document.getElementById(id);
let model = vscode.getState() || { version: 1, elements: [] }, selected = null, history = [], future = [];
function checkpoint() { history.push(JSON.stringify(model)); if (history.length > 100) history.shift(); future = []; }
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
function render() {
  vscode.setState(model);
  themeManager();
  const ds = NexoBuilder.designSystem(model);
  $('canvas').style.background = model.designSystem ? ds.themes[ds.mode].background : '#ffffff';
  $('canvas').replaceChildren(); $('layers').replaceChildren(); $('properties').replaceChildren();
  for (const item of model.elements) {
    const element = document.createElement(item.tag);
    if (item.tag === 'input') { element.placeholder = item.text; element.readOnly = true; } else element.textContent = item.text;
    element.className = 'element' + (item.id === selected ? ' selected' : '');
    Object.assign(element.style, { left: item.x + 'px', top: item.y + 'px', width: item.width + 'px', height: item.height + 'px', ...NexoBuilder.elementStyle(model, item) });
    const resolved = NexoBuilder.elementStyle(model, item);
    for (const key of ['fontSize', 'padding', 'borderRadius']) if (resolved[key] !== undefined) element.style[key] = resolved[key] + 'px';
    element.onclick = event => event.preventDefault();
    element.onpointerdown = event => {
      event.preventDefault(); selected = item.id; checkpoint();
      const sx = event.clientX, sy = event.clientY, x = item.x, y = item.y;
      element.setPointerCapture(event.pointerId);
      for (const child of $('canvas').children) child.classList.remove('selected'); element.classList.add('selected');
      element.onpointermove = move => {
        item.x = Math.min(10000, Math.max(0, Math.round(x + move.clientX - sx)));
        item.y = Math.min(10000, Math.max(0, Math.round(y + move.clientY - sy)));
        element.style.left = item.x + 'px'; element.style.top = item.y + 'px';
        $('code').textContent = NexoBuilder.generate(model);
      };
      const finish = () => { element.onpointermove = null; render(); };
      element.onpointerup = finish; element.onpointercancel = finish;
    };
    $('canvas').append(element);
    const layer = document.createElement('button'); layer.textContent = `${item.tag} · ${item.id}`;
    layer.onclick = () => { selected = item.id; render(); }; $('layers').append(layer);
  }
  const item = current();
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
    const label = document.createElement('label'); label.textContent = key;
    const input = document.createElement('input'); input.name = key;
    input.type = ['color', 'background'].includes(key) ? 'color' : key === 'text' ? 'text' : 'number';
    input.value = ['color', 'background'].includes(key) ? NexoBuilder.elementStyle(model, item)[key] : item[key]; if (input.type === 'number') { input.min = 0; input.max = 10000; }
    input.oninput = () => {
      const candidate = JSON.parse(JSON.stringify(model)); candidate.elements.find(e => e.id === selected)[key] = input.type === 'number' ? Number(input.value) : input.value;
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
  $('code').textContent = NexoBuilder.generate(model);
  $('undo').disabled = !history.length; $('redo').disabled = !future.length; $('delete').disabled = !item;
}
for (const tag of NexoBuilder.tags) {
  const button = document.createElement('button'); button.textContent = '+ ' + tag;
  button.onclick = () => {
    checkpoint(); if (!model.designSystem) model.designSystem = NexoBuilder.defaultDesignSystem(); const id = 'n' + (Math.max(0, ...model.elements.map(e => Number(e.id.slice(1)))) + 1);
    model.elements.push({ id, tag, useTheme: true, text: tag === 'div' ? '' : tag === 'h1' ? 'Your next idea' : tag === 'button' ? 'Get started' : tag === 'input' ? 'Enter text' : 'Write something here', x: 40, y: Math.min(9900, Math.max(24, ...model.elements.map(e => e.y + e.height)) + 16), width: tag === 'h1' ? 400 : 240, height: tag === 'h1' ? 80 : tag === 'h2' ? 72 : tag === 'div' ? 120 : tag === 'p' ? 64 : 48, color: '#172033', background: '#ffffff' }); selected = id; render();
  }; $('palette').append(button);
}
$('theme-properties').onsubmit = e => e.preventDefault();
$('apply-theme').onclick = () => {
  checkpoint(); if (!model.designSystem) model.designSystem = NexoBuilder.defaultDesignSystem();
  for (const item of model.elements) item.useTheme = true;
  render(); $('status').textContent = 'Design system applied to all elements. Undo to restore overrides.';
};
$('properties').onsubmit = e => e.preventDefault();
$('delete').onclick = () => { if (!current()) return; checkpoint(); model.elements = model.elements.filter(e => e.id !== selected); selected = null; render(); };
$('undo').onclick = () => { if (!history.length) return; future.push(JSON.stringify(model)); model = JSON.parse(history.pop()); render(); };
$('redo').onclick = () => { if (!future.length) return; history.push(JSON.stringify(model)); model = JSON.parse(future.pop()); render(); };
$('load-theme').onclick = () => vscode.postMessage({ type: 'load-theme' });
$('save-theme').onclick = () => vscode.postMessage({ type: 'save-theme', designSystem: NexoBuilder.designSystem(model) });
for (const type of ['save', 'export', 'load']) $(type).onclick = () => vscode.postMessage({ type, model });
window.addEventListener('message', ({ data }) => {
  if (data.type === 'load') { try { NexoBuilder.validate(data.model); checkpoint(); model = data.model; selected = null; render(); } catch (error) { $('status').textContent = error.message; } }
  if (data.type === 'theme') {
    try { NexoBuilder.validateDesignSystem(data.designSystem); checkpoint(); model.designSystem = data.designSystem; render(); $('status').textContent = 'Design system imported. Use Apply theme to bind custom elements.'; }
    catch (error) { $('status').textContent = error.message; }
  }
  if (data.type === 'status') $('status').textContent = data.text;
});
render();
