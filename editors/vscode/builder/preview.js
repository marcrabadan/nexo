const fs = require('fs/promises');
const os = require('os');
const path = require('path');
const http = require('http');
const { execFile } = require('child_process');
const { promisify } = require('util');
const run = promisify(execFile);
// A real compiler preview: no interpretation of event handlers in the webview.
class Preview {
  constructor({ command, directory, compile = run }) { this.command = command; this.directory = directory; this.compile = compile; this.revision = 0; this.disposed = false; this.queue = Promise.resolve(); this.sequence = 0; }
  update(source) {
    const sequence = ++this.sequence;
    const task = this.queue.then(() => sequence !== this.sequence && this.url ? this.url : this.build(source));
    this.queue = task.catch(() => {}); return task;
  }
  async build(source) {
    if (this.disposed) throw Error('Preview is closed.');
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'nexo-preview-'));
    const file = path.join(this.directory || root, '.nexo-builder-' + path.basename(root) + '.nexo');
    try {
      await fs.writeFile(file, source, { flag: 'wx' });
      await this.compile(this.command, ['web', 'build', file, '-o', path.join(root, 'dist')], { cwd: this.directory || root, timeout: 30000, maxBuffer: 1024 * 1024, windowsHide: true });
      if (this.disposed) throw Error('Preview is closed.');
      const previous = this.root; this.root = root; this.revision++;
      if (!this.server) await this.listen();
      if (previous) await fs.rm(previous, { recursive: true, force: true });
      return this.url;
    } catch (error) {
      await fs.rm(root, { recursive: true, force: true });
      throw Error(String(error.stderr || error.message).slice(0, 8000));
    } finally { await fs.rm(file, { force: true }); }
  }
  async listen() {
    this.server = http.createServer(async (req, res) => {
      const host = '127.0.0.1:' + this.server.address().port;
      if (req.headers.host !== host || (req.headers.origin && req.headers.origin !== 'http://' + host) || !['GET', 'HEAD'].includes(req.method)) { res.writeHead(403); res.end(); return; }
      res.setHeader('Cache-Control', 'no-store'); res.setHeader('X-Content-Type-Options', 'nosniff');
      res.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
      try {
        const name = decodeURIComponent(new URL(req.url, 'http://' + host).pathname);
        if (name === '/__nexo_builder_revision') { res.setHeader('Content-Type', 'text/plain'); res.end(String(this.revision)); return; }
        if (name === '/__nexo_builder_reload.js') {
          res.setHeader('Content-Type', 'text/javascript');
          res.end(`let version=${this.revision};setInterval(async()=>{try{const value=await(await fetch('/__nexo_builder_revision')).text();if(value!==String(version))location.reload()}catch{}},1000);`); return;
        }
        if (!/^\/[\w./-]*$/.test(name) || name.includes('..') || name.split('/').some(part => part.startsWith('.'))) throw Error('Invalid path');
        const file = path.join(this.root, 'dist', name === '/' ? 'index.html' : name.slice(1));
        let body;
        try { body = await fs.readFile(file); }
        catch (error) { if (error.code !== 'ENOENT' || path.extname(name)) throw error; body = await fs.readFile(path.join(this.root, 'dist/index.html')); }
        const ext = path.extname(file) || '.html';
        const kind = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.wasm': 'application/wasm' }[ext];
        if (!kind) throw Error('Unsupported asset');
        if (kind === 'text/html') body = Buffer.from(body.toString().replace('</body>', '<script src="/__nexo_builder_reload.js"></script></body>'));
        res.setHeader('Content-Type', kind + (kind === 'application/wasm' ? '' : '; charset=utf-8'));
        res.end(req.method === 'HEAD' ? undefined : body);
      } catch { res.writeHead(404); res.end('Not found'); }
    });
    await new Promise((resolve, reject) => { this.server.once('error', reject); this.server.listen(0, '127.0.0.1', resolve); });
    this.url = 'http://127.0.0.1:' + this.server.address().port + '/';
  }
  async dispose() {
    this.disposed = true;
    if (this.server) { this.server.closeAllConnections?.(); await new Promise(resolve => this.server.close(resolve)); }
    await this.queue;
    if (this.root) await fs.rm(this.root, { recursive: true, force: true });
  }
}
module.exports = { Preview };
