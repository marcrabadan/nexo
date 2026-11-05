const assert = require('assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');
const { validate, generate, geometry, setGeometry, transform, extractComponent } = require('../builder/model');
const { importSource } = require('../builder/import');
const { applySource } = require('../builder/sync');
const source = `use nexo.web

pub component Counter:
    state count: int = 0
    fn increment():
        count += 1
    view:
        main:
            h1("Hello")
            p("Count: {count}")
            button("Add", on_click: increment)
            Widget()
    style:
        main { padding: 24px; }
        button:hover { color: #112233; }

component Widget:
    view:
        p("Keep me")
`;
const imported = importSource(source).model;
imported.elements[0].text = 'Edited title';
setGeometry(imported.elements[0], 'tablet', { width: 300, x: 10 });
setGeometry(imported.elements[0], 'mobile', { width: 180 });
assert.strictEqual(geometry(imported.elements[0], 'desktop').width, 400);
assert.strictEqual(geometry(imported.elements[0], 'mobile').x, 10);
assert.strictEqual(geometry(imported.elements[0], 'mobile').width, 180);
const updated = applySource(source, imported);
for (const text of ['state count: int = 0', 'count += 1', 'on_click: increment', 'p("Count: {count}"', 'Widget()', 'button:hover { color: #112233; }', 'p("Keep me")', '@media (max-width: 768px)', '@media (max-width: 480px)']) assert(updated.includes(text), text);
assert.deepStrictEqual(importSource(updated).model, imported);
assert.deepStrictEqual(importSource(updated.replace('count += 1', 'count += 2')).model, imported);
const editedAgain = applySource(updated, imported);
assert.strictEqual((editedAgain.match(/\/\* nexo-builder:start \*\//g) || []).length, 1);
assert.deepStrictEqual(importSource(editedAgain).model, imported);
const unsafe = JSON.parse(JSON.stringify(imported)); unsafe.elements[1].text = 'Overwrite dynamic';
assert.throws(() => applySource(source, unsafe), /Dynamic text/);
const item = (id, x, y, width = 20) => ({ id, tag: 'p', text: id, x, y, width, height: 20, color: '#112233', background: '#ffffff' });
const arrangement = { version: 1, elements: [item('n1', 0, 0), item('n2', 40, 30), item('n3', 160, 70)] };
const ids = arrangement.elements.map(e => e.id);
assert.deepStrictEqual(transform(arrangement, ids, 'left').elements.map(e => e.x), [0, 0, 0]);
assert.deepStrictEqual(transform(arrangement, ids, 'bottom').elements.map(e => e.y), [70, 70, 70]);
assert.deepStrictEqual(transform(arrangement, ids, 'distribute-x').elements.map(e => e.x), [0, 80, 160]);
const alignedMobile = transform(arrangement, ids, 'left', 'mobile');
assert.strictEqual(alignedMobile.elements[1].x, 40);
assert.strictEqual(geometry(alignedMobile.elements[1], 'mobile').x, 0);
const grouped = transform(arrangement, ids, 'group');
assert(grouped.elements.every(e => e.group === grouped.elements[0].group));
assert(transform(grouped, ids, 'ungroup').elements.every(e => !e.group));
assert.throws(() => transform(arrangement, ids.slice(0, 2), 'distribute-x'), /three/);
const reusable = extractComponent(arrangement, ids.slice(0, 2), 'BuilderCard');
assert.strictEqual(reusable.components.length, 1);
assert.strictEqual(reusable.elements.length, 2);
const instance = reusable.elements.find(e => e.component);
instance.props.n1 = 'Custom instance title';
assert(generate(reusable).includes('component BuilderCard(text_n1: str = "n1"'));
assert(generate(reusable).includes('text_n1: "Custom instance title"'));
assert.throws(() => extractComponent(arrangement, ids, 'Bad name'), /Component names/);
const staticSource = 'use nexo.web\npub component App:\n    view:\n        h1("Title")\n        p("Description")\n';
const componentModel = extractComponent(importSource(staticSource).model, ['n1', 'n2'], 'BuilderSummary');
const componentText = applySource(staticSource, componentModel);
assert.deepStrictEqual(importSource(componentText).model, componentModel);
assert(applySource(componentText, componentModel).includes('BuilderSummary('));
const empty = { version: 1, elements: [] };
assert.deepStrictEqual(importSource(applySource(staticSource, empty)).model, empty);
const invalid = JSON.parse(JSON.stringify(arrangement)); invalid.elements[0].layouts = { mobile: { width: -10 } };
assert.throws(() => validate(invalid), /geometry/);
// Review regressions: bare bindings must not disappear during extraction.
for (const element of ['p(query)', 'input(value: query)', 'button("Go", disabled: busy)']) {
  const boundSource = 'use nexo.web\npub component App:\n    state query: str = ""\n    state busy: bool = false\n    view:\n        ' + element + '\n';
  const boundModel = importSource(boundSource).model;
  const extracted = extractComponent(boundModel, [boundModel.elements[0].id], 'BuilderBound');
  assert.throws(() => applySource(boundSource, extracted), /runtime bindings/);
}
const insertedSource = updated.replace('            h1(', '            h2("New handwritten heading")\n            h1(');
const insertedModel = importSource(insertedSource).model;
assert.strictEqual(new Set(insertedModel.elements.map(e => e.id)).size, insertedModel.elements.length);
assert.strictEqual(insertedModel.elements.find(e => e.text === 'Edited title').id, imported.elements[0].id);
assert.notStrictEqual(insertedModel.elements[0].id, imported.elements[0].id);
const { allocateId } = require('../builder/model');
const reservedModel = { version: 1, elements: [item('n9', 0, 0)], nextElementId: 10 };
reservedModel.elements = [];
assert.strictEqual(allocateId(reservedModel), 'n10');
const replaceSource = 'use nexo.web\npub component App:\n    state count: int = 0\n    fn increment():\n        count += 1\n    view:\n        button("Add", on_click: increment)\n';
const replacement = importSource(replaceSource).model;
replacement.nextElementId = 2;
replacement.elements = [];
replacement.elements.push({ ...item(allocateId(replacement), 0, 0), tag: 'h1', text: 'Replacement heading' });
const replacementSource = applySource(replaceSource, replacement);
assert(replacementSource.includes('h1("Replacement heading"'));
assert(!replacementSource.includes('on_click: increment'));
assert(replacementSource.includes('fn increment():'));
// Compile the results with the actual Nexo compiler when the repository interpreter is installed.
const root = path.resolve(__dirname, '../../..');
const command = process.env.NEXO_TEST_COMMAND || path.join(root, '.venv/bin/nexo');
if (fs.existsSync(command)) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'nexo-builder-check-'));
  try {
    for (const [name, text] of Object.entries({ synced: updated, component: componentText, empty: applySource(staticSource, empty), emptyContainer: applySource('use nexo.web\npub component App:\n    view:\n        main:\n            h1("Title")\n', empty), responsive: generate({ ...arrangement, elements: alignedMobile.elements }), reusable: generate(reusable) })) {
      const file = path.join(directory, name + '.nexo'); fs.writeFileSync(file, text);
      const result = spawnSync(command, ['web', 'check', file], { encoding: 'utf8', cwd: root });
      assert.strictEqual(result.status, 0, result.stdout + result.stderr);
    }
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
} else console.log('builder compiler integration skipped: set NEXO_TEST_COMMAND to an installed nexo executable');
console.log('builder features: responsive inheritance, source preservation, repeat sync, components, alignment and grouping pass');
