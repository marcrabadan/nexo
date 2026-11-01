"""Optional Vite dev runner, offline scaffolding and bounded artifact preview."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import os
import signal
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from .web_frontend import artifacts,fail
from .modules import project_root

TOOLS=Path(__file__).with_name('web_tools')
CSP="default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def scaffold(directory):
    path=Path(directory)
    if path.exists():fail(1,'Project destination already exists; choose a new directory.','NW4002')
    path.mkdir(parents=True)
    for name in ('app.nexo','package.json','package-lock.json'):
        (path/name).write_bytes((TOOLS/name).read_bytes())
    (path/'nexo.toml').write_text('[project]\nname = "nexo-web-app"\n')
    (path/'.gitignore').write_text('node_modules/\ndist/\n')
    print(f'Created {path}. Run npm ci --prefix {path}, then python -m nexo web dev {path}/app.nexo.')


def preview_server(directory,port=4173):
    directory=Path(directory).resolve()
    from .web_release import MANIFEST,read_release
    release=(directory/MANIFEST).exists()
    if release:allowed=set(read_release(directory))
    else:
        required=('index.html','main.js','app.js','app.css')
        allowed={name for name in (*required,'app.js.map') if (directory/name).is_file() and not (directory/name).is_symlink()}
        if not set(required)<=allowed:fail(1,'Preview requires a valid release or built index.html/main.js/app.js/app.css files.','NW4002')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            host=self.headers.get('Host','');origin=self.headers.get('Origin')
            permitted=(f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
            if host not in permitted or (origin and origin!='http://'+host):self.send_error(403);return
            path=urlsplit(self.path).path
            name='index.html' if path=='/' else path[1:]
            if name not in allowed:self.send_error(404);return
            target=directory/name
            if target.is_symlink() or not target.resolve().is_relative_to(directory):self.send_error(403);return
            try:body=target.read_bytes()
            except OSError:self.send_error(404);return
            kind={'html':'text/html','js':'text/javascript','css':'text/css','map':'application/json','json':'application/json'}.get(name.rsplit('.',1)[-1],'application/octet-stream')
            self.send_response(200);self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy',CSP);self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer');self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def command(args):
    if args.command=='new':scaffold(args.directory);return 0
    if not 0<=args.port<=65535:fail(1,'Use a loopback port from 0 through 65535.','NW4002')
    if args.command=='preview':
        server=preview_server(args.directory,args.port)
        print(f'Nexo Web preview http://127.0.0.1:{server.server_port}/ (local only)',flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()
        return 0
    entry=Path(args.file).resolve();artifacts(entry,True)
    node=shutil.which('node')
    if node is None:fail(1,'Node.js is required for dev. Install Node 22.12+ or 24+.','NW4001')
    root=project_root(str(entry))
    process=subprocess.Popen([node,str(TOOLS/'dev.mjs'),'--python',sys.executable,'--file',str(entry),'--root',str(root),'--port',str(args.port)],start_new_session=os.name=='posix')
    def interrupted(*_):raise KeyboardInterrupt
    previous=signal.signal(signal.SIGTERM,interrupted)
    try:return process.wait()
    except KeyboardInterrupt:return 0
    finally:
        signal.signal(signal.SIGTERM,previous)
        def stop(sig):
            try:
                if os.name=='posix':os.killpg(process.pid,sig)
                elif process.poll() is None:process.terminate()
            except ProcessLookupError:pass
        stop(signal.SIGTERM)
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:stop(signal.SIGKILL);process.wait()
