const assert = require('assert');
const fs = require('fs/promises');
const syncFs = require('fs');
const os = require('os');
const path = require('path');
const { Preview } = require('../builder/preview');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '../../..');
const command = process.env.NEXO_TEST_COMMAND || path.join(root, '.venv/bin/nexo');
const source = `use nexo.web
pub component App:
    state count: int = 0
    state query: str = ""
    fn increment():
        count += 1
    fn edit(event: WebEvent):
        query = event.value
    fn go():
        navigate("/next")
    view:
        main:
            p("Count: {count}", id: "count")
            input(value: query, on_input: edit)
            p(query, id: "query")
            button("Increment", on_click: increment)
            button("Navigate", on_click: go)
`;
(async () => {
  if (!syncFs.existsSync(command)) { console.log('builder preview skipped: set NEXO_TEST_COMMAND to an installed nexo executable'); return; }
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'nexo-preview-project-'));
  const preview = new Preview({ command, directory });
  let dom;
  try {
    const url = await preview.update(source);
    const html = await (await fetch(url)).text();
    assert(html.includes('/__nexo_builder_reload.js'));
    assert.strictEqual(await (await fetch(url + '__nexo_builder_revision')).text(), '1');
    assert.strictEqual((await fetch(url + '.env')).status, 404);
    assert.strictEqual((await fetch(url, { method: 'POST' })).status, 403);
    assert.strictEqual((await fetch(url, { headers: { Origin: 'https://example.com' } })).status, 403);
    const javascript = await (await fetch(url + 'app.js')).text();
    dom = new JSDOM('<div id="app"></div>', { url });
    global.document = dom.window.document; global.window = dom.window;
    const app = await import('data:text/javascript;base64,' + Buffer.from(javascript).toString('base64'));
    const mounted = app.App(document.querySelector('#app'));
    document.querySelector('button').click();
    assert.strictEqual(document.querySelector('#count').textContent, 'Count: 1');
    const input = document.querySelector('input'); input.value = 'Typed preview text'; input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    assert.strictEqual(document.querySelector('#query').textContent, 'Typed preview text');
    document.querySelectorAll('button')[1].click();
    assert.strictEqual(dom.window.location.hash, '#/next');
    mounted.destroy();
    await preview.update(source.replace('Count: {count}', 'Clicks: {count}'));
    assert.strictEqual(await (await fetch(url + '__nexo_builder_revision')).text(), '2');
    assert((await (await fetch(url + 'app.js')).text()).includes('Clicks: '));
    await assert.rejects(preview.update('not valid nexo'), /NW|Invalid|public|browser|declaration/i);
    assert.strictEqual((await fetch(url)).status, 200); // Keep the last working build.
    assert.deepStrictEqual(await fs.readdir(directory), []); // Temporary source is removed.
  } finally { await preview.dispose(); if (dom) dom.window.close(); delete global.document; delete global.window; await fs.rm(directory, { recursive: true, force: true }); }
  assert.strictEqual(syncFs.existsSync(preview.root), false);
  console.log('builder preview: actual compiler, state, input, navigation, rebuild, failed-build recovery and cleanup pass');
})().catch(error => { console.error(error); process.exitCode = 1; });
