const assert = require('assert');
const { validate, generate, tags } = require('../builder/model');
const model = { version: 1, elements: tags.map((tag, i) => ({ id: 'n' + i, tag, text: 'Hello "Nexo"\nNext line', x: 20 + i, y: 30, width: 200, height: 48, color: '#172033', background: '#ffffff' })) };
assert.strictEqual(validate(model), model);
const source = generate(model);
assert(source.includes('pub component App:'));
assert(source.includes('input(placeholder: "Hello \\"Nexo\\"\\nNext line", class: "n4")'));
assert(source.includes('left: 20px; top: 30px;'));
assert(generate({ version: 1, elements: [] }).includes('div()'));
for (const change of [{ id: 'bad' }, { tag: 'script' }, { text: '{secret}' }, { x: Infinity }, { width: -1 }, { color: 'red; }' }]) {
  const bad = JSON.parse(JSON.stringify(model)); Object.assign(bad.elements[0], change);
  assert.throws(() => generate(bad));
}
const duplicate = JSON.parse(JSON.stringify(model)); duplicate.elements[1].id = duplicate.elements[0].id;
assert.throws(() => validate(duplicate));
assert.throws(() => validate({ version: 2, elements: [] }));
assert.throws(() => validate({ version: 1, elements: Array(501).fill(model.elements[0]) }));
console.log('builder: generation, escaping, and invalid design rejection pass');

const { defaultDesignSystem, validateDesignSystem, elementStyle } = require('../builder/model');
const themed = JSON.parse(JSON.stringify(model)); themed.designSystem = defaultDesignSystem();
for (const element of themed.elements) element.useTheme = true;
const button = themed.elements.find(e => e.tag === 'button');
assert.strictEqual(elementStyle(themed, button).background, '#635bff');
themed.designSystem.themes.light.primary = '#cc3300';
assert(generate(themed).includes('background: #cc3300;'));
themed.designSystem.mode = 'dark';
assert.strictEqual(elementStyle(themed, button).background, '#a59eff');
assert(generate(themed).includes('background: #10131c;'));
button.useTheme = false;
assert.strictEqual(elementStyle(themed, button).background, button.background);
assert.strictEqual(JSON.parse(JSON.stringify(themed)).designSystem.mode, 'dark');
for (const change of [{ mode: 'other' }, { fontFamily: 'x; }' }, { radius: -1 }, { bodySize: 0 }, { spacing: Infinity }]) {
  assert.throws(() => validateDesignSystem({ ...defaultDesignSystem(), ...change }));
}
const invalidColors = defaultDesignSystem(); invalidColors.themes.dark.primary = 'red; }';
assert.throws(() => validateDesignSystem(invalidColors));
assert.strictEqual(defaultDesignSystem().themes.light.primary, '#635bff');
console.log('design system: theme propagation, mode switching, overrides, serialization and validation pass');
