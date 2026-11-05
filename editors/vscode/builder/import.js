// Import static view content without evaluating Nexo, JavaScript or CSS.
const { tags, validate } = require('./model');
const { readMetadata } = require('./metadata');
function importSource(source) {
  if (typeof source !== 'string' || source.length > 2000000) throw Error('Nexo source must be under 2 MB.');
  const warnings = new Set(), elements = [], bindings = [];
  const lines = source.split(/\r?\n/);
  const declaration = lines.findIndex(line => /^pub (component|page) \w+.*:$/.test(line));
  if (declaration < 0) throw Error('No public component or page found in this file.');
  let end = declaration + 1;
  while (end < lines.length && (!lines[end].trim() || /^\s/.test(lines[end]))) end++;
  const block = lines.slice(declaration + 1, end);
  const view = block.findIndex(line => /^    view:\s*$/.test(line));
  if (view < 0) throw Error('The first public component/page has no view to import.');
  const css = block.join('\n').split(/\n    style:\s*\n/)[1] || '';
  const rules = new Map();
  for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = match[1].trim();
    if (/^(?:\.[\w-]+|[a-z][\w-]*)$/.test(selector)) rules.set(selector, match[2]);
    else warnings.add('Complex CSS selectors are not imported.');
  }
  let y = 24;
  for (let i = view + 1; i < block.length; i++) {
    const line = block[i];
    if (line.trim() && !/^ {8}/.test(line)) break;
    const text = line.trim();
    if (!text || text.startsWith('#')) continue;
    if (/^(main|section|header|footer|article|nav)(?:\(.*\))?:$/.test(text)) continue;
    const call = /^(\w+)\((.*)\):?$/.exec(text);
    if (!call || !tags.includes(call[1])) { warnings.add('Some control flow, custom components or unsupported tags are omitted.'); continue; }
    // Split arguments only outside quoted strings and nested calls.
    const args = []; let start = 0, quote = false, escape = false, depth = 0;
    for (let j = 0; j < call[2].length; j++) {
      const ch = call[2][j];
      if (quote) { if (escape) escape = false; else if (ch === '\\') escape = true; else if (ch === '"') quote = false; }
      else if (ch === '"') quote = true;
      else if ('(['.includes(ch)) depth++;
      else if (')]'.includes(ch)) depth--;
      else if (ch === ',' && depth === 0) { args.push(call[2].slice(start, j).trim()); start = j + 1; }
    }
    args.push(call[2].slice(start).trim());
    let label = '', classes = [];
    for (const arg of args.filter(Boolean)) {
      const named = /^(\w+):\s*(.*)$/.exec(arg), key = named ? named[1] : 'text', value = named ? named[2] : arg;
      if (['text', 'placeholder', 'class'].includes(key)) {
        let literal;
        try { literal = JSON.parse(value); } catch { warnings.add('Dynamic text and bindings are omitted.'); continue; }
        if (typeof literal !== 'string' || /[{}]/.test(literal)) { warnings.add('Dynamic text and bindings are omitted.'); continue; }
        if (key === 'class') classes = literal.split(/\s+/); else label = literal;
      } else warnings.add('Runtime actions and other attributes are not imported.');
    }
    const managedId = classes.map(name => /^(?:nexo-builder-)?(n[0-9]+)$/.exec(name)?.[1]).find(Boolean);
    const item = { id: managedId || 'n' + (elements.length + 1), tag: call[1], text: label, x: 24, y, width: call[1] === 'h1' ? 400 : 240, height: call[1] === 'h1' ? 80 : 48, color: '#172033', background: '#ffffff' };
    const declarations = [rules.get(item.tag), ...classes.map(name => rules.get('.' + name))].filter(Boolean).join(';');
    for (const entry of declarations.split(';')) {
      const match = /^\s*([\w-]+)\s*:\s*(.*?)\s*$/.exec(entry); if (!match) continue;
      const [, property, value] = match;
      const dimension = { left: 'x', top: 'y', width: 'width', height: 'height' }[property];
      if (dimension && /^\d+(?:\.\d+)?px$/.test(value) && parseFloat(value) <= 10000) item[dimension] = parseFloat(value);
      else if (['color', 'background', 'background-color'].includes(property) && /^#[\da-f]{6}$/i.test(value)) item[property === 'color' ? 'color' : 'background'] = value;
      else if (!['position', 'box-sizing', 'margin', 'overflow'].includes(property)) warnings.add('Some CSS styling is outside the visual importer subset.');
    }
    let childEnd = i + 1;
    const indentation = line.length - line.trimStart().length;
    if (text.endsWith(':')) while (childEnd < block.length && (!block[childEnd].trim() || block[childEnd].length - block[childEnd].trimStart().length > indentation)) childEnd++;
    bindings.push({ id: item.id, line: declaration + 1 + i, endLine: declaration + 1 + childEnd, args, tag: item.tag, classes, indentation });
    elements.push(item); y = Math.min(9900, item.y + item.height + 16);
  }
  const snapshot = readMetadata(source);
  if (!elements.length && !snapshot) throw Error('No supported view elements found. The current design was kept.');
  if (block.slice(0, view).some(line => /\b(state|fn|load|provide)\b/.test(line))) warnings.add('State and runtime logic are not imported.');
  if (lines.slice(end).some(line => /^(pub )?(component|page|layout) /.test(line))) warnings.add('Only the first public component/page is imported.');
  return { model: snapshot || validate({ version: 1, elements }), warnings: snapshot ? [] : [...warnings], bindings, declaration, declarationEnd: end };
}
module.exports = { importSource };
