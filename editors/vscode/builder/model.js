/* Shared by the extension host and its isolated webview. */
(function (root) {
  const tags = ['h1', 'h2', 'p', 'button', 'input', 'div'];
  const palettes = {
    light: { primary: '#635bff', onPrimary: '#ffffff', background: '#f5f6fa', surface: '#ffffff', text: '#172033', border: '#cbd0dc' },
    dark: { primary: '#a59eff', onPrimary: '#17132e', background: '#10131c', surface: '#202635', text: '#f1f3fa', border: '#485267' },
  };
  const fonts = ['system-ui', 'sans-serif', 'serif', 'monospace'];
  function defaultDesignSystem() {
    return { mode: 'light', themes: JSON.parse(JSON.stringify(palettes)), fontFamily: 'system-ui', bodySize: 16, headingSize: 32, radius: 8, spacing: 12 };
  }
  function validateDesignSystem(ds) {
    if (!ds || !['light', 'dark'].includes(ds.mode) || !fonts.includes(ds.fontFamily)) throw Error('Invalid design system theme or font.');
    for (const mode of ['light', 'dark']) for (const key of Object.keys(palettes.light)) {
      if (!ds.themes || !ds.themes[mode] || !/^#[0-9a-f]{6}$/i.test(ds.themes[mode][key])) throw Error('Design system colors must use six digit hex values.');
    }
    for (const key of ['bodySize', 'headingSize', 'radius', 'spacing']) {
      if (!Number.isFinite(ds[key]) || ds[key] < (key.endsWith('Size') ? 8 : 0) || ds[key] > 128) throw Error('Invalid design system size (maximum 128px).');
    }
    return ds;
  }
  function designSystem(model) { return model.designSystem || defaultDesignSystem(); }
  function elementStyle(model, item) {
    const ds = designSystem(model), colors = ds.themes[ds.mode];
    if (!item.useTheme) return { color: item.color, background: item.background, fontFamily: 'system-ui', fontSize: item.tag === 'h1' ? 32 : item.tag === 'h2' ? 24 : 16 };
    return {
      color: item.tag === 'button' ? colors.onPrimary : colors.text,
      background: item.tag === 'button' ? colors.primary : colors.surface,
      fontFamily: ds.fontFamily,
      fontSize: item.tag === 'h1' ? ds.headingSize : item.tag === 'h2' ? Math.round(ds.headingSize * 0.75) : ds.bodySize,
      borderRadius: ds.radius, padding: ds.spacing,
      border: `1px solid ${colors.border}`,
    };
  }
  function validate(model) {
    if (!model || model.version !== 1 || !Array.isArray(model.elements) || model.elements.length > 500) throw Error('Invalid builder design (expected version 1, at most 500 elements).');
    if (model.nextElementId !== undefined && (!Number.isSafeInteger(model.nextElementId) || model.nextElementId < 1)) throw Error('Invalid element ID counter.');
    if (model.designSystem !== undefined) validateDesignSystem(model.designSystem);
    if (model.components !== undefined) {
      if (!Array.isArray(model.components) || model.components.length > 100) throw Error('At most 100 reusable components are supported.');
      const names = new Set();
      for (const component of model.components) {
        if (!component || !/^Builder[A-Z][A-Za-z0-9]*$/.test(component.name) || names.has(component.name)) throw Error('Component names must be unique and start with Builder followed by a capital letter.');
        names.add(component.name);
        validate({ version: 1, elements: component.elements });
        if (!component.elements.length || component.elements.some(e => e.component)) throw Error('Reusable components require elements and cannot contain other component instances.');
      }
    }
    const ids = new Set();
    for (const item of model.elements) {
      if (!item || !/^n[0-9]+$/.test(item.id) || ids.has(item.id) || !tags.includes(item.tag)) throw Error('Invalid or duplicate element.');
      ids.add(item.id);
      if (item.component !== undefined) {
        const definition = (model.components || []).find(c => c.name === item.component);
        if (!definition || !item.props || Object.keys(item.props).some(key => !definition.elements.some(e => e.id === key))) throw Error('Invalid component instance properties.');
        for (const value of Object.values(item.props)) if (typeof value !== 'string' || value.length > 10000 || /[{}]/.test(value)) throw Error('Invalid component text property.');
      }
      if (item.group !== undefined && !/^g[0-9]+$/.test(item.group)) throw Error('Invalid group.');
      if (item.layouts !== undefined) {
        if (!item.layouts || typeof item.layouts !== 'object' || Array.isArray(item.layouts)) throw Error('Invalid responsive layouts.');
        for (const [breakpoint, layout] of Object.entries(item.layouts)) {
          if (!['tablet', 'mobile'].includes(breakpoint) || !layout || typeof layout !== 'object') throw Error('Invalid breakpoint.');
          for (const [key, value] of Object.entries(layout)) if (!['x', 'y', 'width', 'height'].includes(key) || !Number.isFinite(value) || value < 0 || value > 10000) throw Error('Invalid responsive geometry.');
        }
      }
      if (item.useTheme !== undefined && typeof item.useTheme !== 'boolean') throw Error('Invalid theme binding.');
      if (typeof item.text !== 'string' || item.text.length > 10000 || /[{}]/.test(item.text)) throw Error('Text must be under 10,000 characters and cannot contain interpolation braces yet.');
      for (const key of ['x', 'y', 'width', 'height']) if (!Number.isFinite(item[key]) || item[key] < 0 || item[key] > 10000) throw Error('Element dimensions must be between 0 and 10,000.');
      for (const key of ['color', 'background']) if (!/^#[0-9a-f]{6}$/i.test(item[key])) throw Error('Colors must use six digit hex values.');
    }
    return model;
  }
  function allocateId(model) {
    const next = Math.max(model.nextElementId || 1, ...model.elements.map(e => Number(e.id.slice(1)) + 1));
    model.nextElementId = next + 1;
    return 'n' + next;
  }
  function geometry(item, breakpoint = 'desktop') {
    return { x: item.x, y: item.y, width: item.width, height: item.height, ...(breakpoint !== 'desktop' ? item.layouts?.tablet : {}), ...(breakpoint === 'mobile' ? item.layouts?.mobile : {}) };
  }
  function setGeometry(item, breakpoint, values) {
    if (breakpoint === 'desktop') Object.assign(item, values);
    else { item.layouts = item.layouts || {}; item.layouts[breakpoint] = { ...item.layouts[breakpoint], ...values }; }
  }
  function responsiveCSS(elements, indent = '        ', selector = e => '.' + e.id) {
    const lines = [];
    for (const [name, width] of [['tablet', 768], ['mobile', 480]]) {
      const items = elements.filter(e => e.layouts?.[name]);
      if (!items.length) continue;
      lines.push(`${indent}@media (max-width: ${width}px) {`);
      for (const e of items) {
        const fields = Object.entries(e.layouts[name]).map(([key, value]) => `${{x:'left',y:'top',width:'width',height:'height'}[key]}: ${value}px;`).join(' ');
        lines.push(`${indent}    ${selector(e)} { ${fields} }`);
      }
      lines.push(indent + '}');
    }
    return lines;
  }
  function componentCall(item) {
    return `${item.component}(${Object.entries(item.props).map(([id, text]) => 'text_' + id + ': ' + JSON.stringify(text)).join(', ')})`;
  }
  function componentSource(definition, model) {
    const local = { ...model, components: [], elements: definition.elements };
    const generated = generate(local).split('\n');
    const params = definition.elements.map(e => `text_${e.id}: str = ${JSON.stringify(e.text)}`).join(', ');
    generated.splice(0, 3, `component ${definition.name}(${params}):`);
    for (let i = 0; i < generated.length; i++) for (const e of definition.elements) {
      const literal = e.tag === 'input' ? `placeholder: ${JSON.stringify(e.text)}` : JSON.stringify(e.text);
      const replacement = e.tag === 'input' ? `placeholder: text_${e.id}` : `text_${e.id}`;
      if (generated[i].trimStart().startsWith(e.tag + '(') && generated[i].includes(`class: "${e.id}"`)) generated[i] = generated[i].replace(literal, replacement);
    }
    // The root fills the instance wrapper instead of imposing the app canvas height.
    return generated.join('\n').replace('        main(class: \"nexo-canvas\"):', '        div(class: \"nexo-canvas\"):').replace('min-height: 900px;', 'min-height: 0; height: 100%;');
  }
  function generate(model) {
    validate(model);
    const lines = ['use nexo.web', '', 'pub component App:', '    view:', '        main(class: "nexo-canvas"):' ];
    if (!model.elements.length) lines.push('            div()');
    for (const item of model.elements) {
      if (item.component) {
        lines.push(`            div(class: "${item.id}"):`);
        lines.push('                ' + componentCall(item));
        continue;
      }
      const args = item.tag === 'input' ? `placeholder: ${JSON.stringify(item.text)}, ` : `${JSON.stringify(item.text)}, `;
      lines.push(`            ${item.tag}(${args}class: "${item.id}")`);
    }
    const ds = designSystem(model), colors = ds.themes[ds.mode];
    lines.push('', '    style:', `        .nexo-canvas { position: relative; width: 100%; min-height: 900px; background: ${model.designSystem ? colors.background : '#ffffff'}; }`);
    for (const e of model.elements) {
      const style = elementStyle(model, e);
      const themeCSS = e.useTheme ? ` border-radius: ${style.borderRadius}px; padding: ${style.padding}px; border: ${style.border};` : '';
      lines.push(`        .${e.id} { position: absolute; box-sizing: border-box; left: ${e.x}px; top: ${e.y}px; width: ${e.width}px; height: ${e.height}px; color: ${style.color}; background: ${style.background}; z-index: ${model.elements.indexOf(e)}; margin: 0; overflow: hidden; font-family: ${style.fontFamily};${e.useTheme ? ` font-size: ${style.fontSize}px;` : ''}${themeCSS} }`);
    }
    lines.push(...responsiveCSS(model.elements));
    for (const definition of model.components || []) lines.push('', componentSource(definition, model));
    return lines.join('\n') + '\n';
  }
  function transform(model, ids, action, breakpoint = 'desktop') {
    validate(model);
    const next = JSON.parse(JSON.stringify(model)), items = next.elements.filter(e => ids.includes(e.id));
    if (!items.length) return next;
    const boxes = items.map(e => geometry(e, breakpoint));
    const left = Math.min(...boxes.map(e => e.x)), top = Math.min(...boxes.map(e => e.y));
    const right = Math.max(...boxes.map(e => e.x + e.width)), bottom = Math.max(...boxes.map(e => e.y + e.height));
    if (action === 'group') {
      const group = 'g' + (Math.max(0, ...next.elements.map(e => Number(e.group?.slice(1)) || 0)) + 1);
      items.forEach(e => e.group = group);
    } else if (action === 'ungroup') items.forEach(e => delete e.group);
    else if (action.startsWith('distribute-')) {
      if (items.length < 3) throw Error('Select at least three elements to distribute spacing.');
      const horizontal = action === 'distribute-x', pos = horizontal ? 'x' : 'y', size = horizontal ? 'width' : 'height';
      items.sort((a, b) => geometry(a, breakpoint)[pos] - geometry(b, breakpoint)[pos]);
      const first = geometry(items[0], breakpoint), last = geometry(items.at(-1), breakpoint);
      const gap = (last[pos] + last[size] - first[pos] - items.reduce((sum, e) => sum + geometry(e, breakpoint)[size], 0)) / (items.length - 1);
      if (gap < 0) throw Error('Spread elements farther apart before distributing spacing.');
      let cursor = first[pos];
      for (const e of items) { setGeometry(e, breakpoint, { [pos]: Math.round(cursor) }); cursor += geometry(e, breakpoint)[size] + gap; }
    } else {
      for (const e of items) {
        const box = geometry(e, breakpoint), values = {};
        if (action === 'left') values.x = left;
        else if (action === 'right') values.x = right - box.width;
        else if (action === 'center') values.x = Math.round((left + right - box.width) / 2);
        else if (action === 'top') values.y = top;
        else if (action === 'bottom') values.y = bottom - box.height;
        else if (action === 'middle') values.y = Math.round((top + bottom - box.height) / 2);
        else throw Error('Unknown alignment operation.');
        setGeometry(e, breakpoint, values);
      }
    }
    return validate(next);
  }
  function extractComponent(model, ids, name) {
    validate(model);
    const next = JSON.parse(JSON.stringify(model)), items = next.elements.filter(e => ids.includes(e.id));
    if (!items.length || items.some(e => e.component)) throw Error('Select ordinary elements to create a reusable component.');
    if ((next.components || []).some(c => c.name === name)) throw Error('Choose a unique component name.');
    const x = Math.min(...items.map(e => e.x)), y = Math.min(...items.map(e => e.y));
    const width = Math.max(...items.map(e => e.x + e.width)) - x, height = Math.max(...items.map(e => e.y + e.height)) - y;
    const children = items.map(e => { const child = { ...e, x: e.x - x, y: e.y - y }; delete child.group; delete child.layouts; return child; });
    if (items.some(e => e.layouts && Object.keys(e.layouts).length)) throw Error('Extract components before adding breakpoint overrides to their children.');
    next.components = [...(next.components || []), { name, elements: children }];
    const instance = { id: allocateId(next), tag: 'div', text: '', x, y, width, height, color: '#172033', background: '#ffffff', component: name, props: Object.fromEntries(children.map(e => [e.id, e.text])) };
    const first = next.elements.findIndex(e => ids.includes(e.id));
    next.elements = next.elements.filter(e => !ids.includes(e.id)); next.elements.splice(first, 0, instance);
    return validate(next);
  }
  const api = { allocateId, transform, extractComponent, geometry, setGeometry, responsiveCSS, componentCall, componentSource, tags, fonts, validate, generate, defaultDesignSystem, validateDesignSystem, designSystem, elementStyle };
  if (typeof module !== 'undefined') module.exports = api;
  else root.NexoBuilder = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
