const { importSource } = require('./import');
const { validate, generate, componentCall, componentSource } = require('./model');
const { withMetadata } = require('./metadata');
function applySource(source, model) {
  validate(model);
  const parsed = importSource(source), baseline = parsed.model;
  const lines = source.split(/\r?\n/), replacements = [];
  const byId = new Map(model.elements.map(e => [e.id, e]));
  const tracked = new Set();
  for (const binding of parsed.bindings) {
    const item = byId.get(binding.id), old = baseline.elements.find(e => e.id === binding.id);
    if (!old) continue;
    tracked.add(binding.id);
    if (!item) {
      const extracted = (model.components || []).some(c => c.elements.some(e => e.id === binding.id));
      if (extracted && binding.args.some(arg => /^on_\w+:/.test(arg) || /[{}]/.test(arg))) throw Error('Extract only static source elements; runtime bindings must remain in their original component.');
      if (binding.endLine > binding.line + 1 && !old.component) throw Error('Delete nested containers in source; their children cannot safely be removed by the builder.');
      replacements.push({ start: binding.line, end: binding.endLine, lines: [] }); continue;
    }
    const args = binding.args.slice();
    if (item.component) {
      replacements.push({ start: binding.line, end: binding.endLine, lines: [' '.repeat(binding.indentation) + `div(class: "nexo-builder-${item.id}"):`, ' '.repeat(binding.indentation + 4) + componentCall(item)] }); continue;
    }
    if (item.text !== old.text) {
      const index = args.findIndex(arg => item.tag === 'input' ? /^placeholder:/.test(arg) : arg && !/^\w+:/.test(arg));
      if (index >= 0) {
        const value = args[index].replace(/^placeholder:\s*/, '');
        let literal; try { literal = JSON.parse(value); } catch { throw Error('Dynamic text must be edited in source; its binding was preserved.'); }
        if (typeof literal !== 'string' || /[{}]/.test(literal)) throw Error('Dynamic text must be edited in source; its binding was preserved.');
        args[index] = (item.tag === 'input' ? 'placeholder: ' : '') + JSON.stringify(item.text);
      } else args.unshift((item.tag === 'input' ? 'placeholder: ' : '') + JSON.stringify(item.text));
    }
    const classIndex = args.findIndex(arg => /^class:/.test(arg));
    const classes = [...binding.classes.filter(name => !/^nexo-builder-n\d+$/.test(name)), 'nexo-builder-' + item.id];
    if (classIndex >= 0) {
      try { if (typeof JSON.parse(args[classIndex].replace(/^class:\s*/, '')) !== 'string') throw Error(); }
      catch { throw Error('Dynamic CSS classes must be edited in source.'); }
      args[classIndex] = 'class: ' + JSON.stringify(classes.join(' '));
    } else args.push('class: ' + JSON.stringify(classes.join(' ')));
    replacements.push({ start: binding.line, end: binding.line + 1, lines: [' '.repeat(binding.indentation) + binding.tag + '(' + args.filter(Boolean).join(', ') + ')' + (lines[binding.line].trim().endsWith(':') ? ':' : '')] });
  }
  // New nodes are siblings at the top of the existing view, never inserted into a conditional.
  let viewEnd = parsed.declaration + 1;
  while (viewEnd < parsed.declarationEnd && !/^    view:/.test(lines[viewEnd])) viewEnd++;
  viewEnd++;
  while (viewEnd < parsed.declarationEnd && (!lines[viewEnd].trim() || /^ {8}/.test(lines[viewEnd]))) viewEnd++;
  const added = [];
  for (const item of model.elements.filter(e => !tracked.has(e.id))) {
    if (item.component) added.push(`        div(class: "nexo-builder-${item.id}"):`, '            ' + componentCall(item));
    else added.push(`        ${item.tag}(${item.tag === 'input' ? 'placeholder: ' : ''}${JSON.stringify(item.text)}, class: "nexo-builder-${item.id}")`);
  }
  if (added.length) replacements.push({ start: viewEnd, end: viewEnd, lines: added });
  // Keep all non-builder CSS and all declaration members intact.
  let styleStart = lines.findIndex((line, index) => index > parsed.declaration && index < parsed.declarationEnd && /^    style:\s*$/.test(line));
  let styleEnd = styleStart + 1;
  if (styleStart >= 0) while (styleEnd < parsed.declarationEnd && (!lines[styleEnd].trim() || /^ {8}/.test(lines[styleEnd]))) styleEnd++;
  const generated = generate(model).split('\n');
  const css = generated.slice(generated.indexOf('    style:') + 1, generated.findIndex(line => line.startsWith('component ')) < 0 ? undefined : generated.findIndex(line => line.startsWith('component '))).filter(Boolean).filter(line => !line.includes('.nexo-canvas')).map(line => line.replace(/\.(n\d+)\b/g, '.nexo-builder-$1'));
  const managed = ['        /* nexo-builder:start */', ...css, '        /* nexo-builder:end */'];
  if (styleStart < 0) replacements.push({ start: parsed.declarationEnd, end: parsed.declarationEnd, lines: ['    style:', ...managed, ''] });
  else {
    const original = lines.slice(styleStart + 1, styleEnd).join('\n').replace(/ *\/\* nexo-builder:start \*\/[\s\S]*?\/\* nexo-builder:end \*\/\n?/g, '');
    replacements.push({ start: styleStart + 1, end: styleEnd, lines: [...original.split('\n').filter(Boolean), ...managed] });
  }
  // Apply edits backwards so bindings refer to the unmodified source throughout.
  replacements.sort((a, b) => b.start - a.start || b.end - a.end || (a.lines[0] === '    style:' ? -1 : b.lines[0] === '    style:' ? 1 : 0));
  for (const change of replacements) lines.splice(change.start, change.end - change.start, ...change.lines);
  // A deletion must not leave an empty view/container/control-flow block.
  const firstView = lines.findIndex((line, index) => index > parsed.declaration && /^    view:\s*$/.test(line));
  if (firstView >= 0) {
    let lastView = firstView + 1;
    while (lastView < lines.length && (!lines[lastView].trim() || /^ {8}/.test(lines[lastView]))) lastView++;
    for (let index = lastView - 1; index >= firstView; index--) {
      if (!lines[index].trim().endsWith(':')) continue;
      const indent = lines[index].length - lines[index].trimStart().length;
      let next = index + 1; while (next < lines.length && !lines[next].trim()) next++;
      if (next === lines.length || lines[next].length - lines[next].trimStart().length <= indent) lines.splice(index + 1, 0, ' '.repeat(indent + 4) + 'div()');
    }
  }
  let result = lines.join('\n').replace(/\n# nexo-builder-components:start\n[\s\S]*?# nexo-builder-components:end\n?/g, '\n');
  const definitions = model.components || [];
  for (const definition of definitions) {
    if (new RegExp('^(?:pub )?component ' + definition.name + '\\b', 'm').test(result)) throw Error('Component name already exists in source: ' + definition.name);
  }
  if (definitions.length) result += '\n# nexo-builder-components:start\n' + definitions.map(definition => componentSource(definition, model)).join('\n') + '\n# nexo-builder-components:end\n';
  return withMetadata(result, model);
}
module.exports = { applySource };
