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
    if (model.designSystem !== undefined) validateDesignSystem(model.designSystem);
    const ids = new Set();
    for (const item of model.elements) {
      if (!item || !/^n[0-9]+$/.test(item.id) || ids.has(item.id) || !tags.includes(item.tag)) throw Error('Invalid or duplicate element.');
      ids.add(item.id);
      if (item.useTheme !== undefined && typeof item.useTheme !== 'boolean') throw Error('Invalid theme binding.');
      if (typeof item.text !== 'string' || item.text.length > 10000 || /[{}]/.test(item.text)) throw Error('Text must be under 10,000 characters and cannot contain interpolation braces yet.');
      for (const key of ['x', 'y', 'width', 'height']) if (!Number.isFinite(item[key]) || item[key] < 0 || item[key] > 10000) throw Error('Element dimensions must be between 0 and 10,000.');
      for (const key of ['color', 'background']) if (!/^#[0-9a-f]{6}$/i.test(item[key])) throw Error('Colors must use six digit hex values.');
    }
    return model;
  }
  function generate(model) {
    validate(model);
    const lines = ['use nexo.web', '', 'pub component App:', '    view:', '        main(class: "nexo-canvas"):' ];
    if (!model.elements.length) lines.push('            div()');
    for (const item of model.elements) {
      const args = item.tag === 'input' ? `placeholder: ${JSON.stringify(item.text)}, ` : `${JSON.stringify(item.text)}, `;
      lines.push(`            ${item.tag}(${args}class: "${item.id}")`);
    }
    const ds = designSystem(model), colors = ds.themes[ds.mode];
    lines.push('', '    style:', `        .nexo-canvas { position: relative; width: 100%; min-height: 900px; background: ${model.designSystem ? colors.background : '#ffffff'}; }`);
    for (const e of model.elements) {
      const style = elementStyle(model, e);
      const themeCSS = e.useTheme ? ` border-radius: ${style.borderRadius}px; padding: ${style.padding}px; border: ${style.border};` : '';
      lines.push(`        .${e.id} { position: absolute; box-sizing: border-box; left: ${e.x}px; top: ${e.y}px; width: ${e.width}px; height: ${e.height}px; color: ${style.color}; background: ${style.background}; margin: 0; overflow: hidden; font-family: ${style.fontFamily};${e.useTheme ? ` font-size: ${style.fontSize}px;` : ''}${themeCSS} }`);
    }
    return lines.join('\n') + '\n';
  }
  const api = { tags, fonts, validate, generate, defaultDesignSystem, validateDesignSystem, designSystem, elementStyle };
  if (typeof module !== 'undefined') module.exports = api;
  else root.NexoBuilder = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
