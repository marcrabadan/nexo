"""Local-only support walkthrough: static UI, session bridge and model stub."""
import argparse
import base64
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent


def signed_token(key):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
    body = encode({'alg': 'HS256', 'typ': 'JWT'}) + b'.' + encode({
        'sub': 'local-demo', 'role': 'support', 'exp': int(time.time()) + 3600})
    return (body + b'.' + base64.urlsafe_b64encode(hmac.digest(key.encode(), body, hashlib.sha256)).rstrip(b'=')).decode()


def create_gateway(directory, key, backend_port=8081, port=8765):
    session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, *args):
            pass

        def trusted_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def session(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except Exception:
                return False
            supplied = cookie['support_demo'].value if 'support_demo' in cookie else ''
            return secrets.compare_digest(supplied, session_id)

        def reply(self, value, status=200):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def read_json(self):
            if self.headers.get('Content-Type') != 'application/json':
                raise ValueError('JSON required')
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('Body must be between 1 and 65536 bytes')
            return json.loads(self.rfile.read(size))

        def forward(self, body=None):
            request = urllib.request.Request(
                f'http://127.0.0.1:{backend_port}/tickets', data=body,
                headers={'Authorization': 'Bearer ' + signed_token(key), 'Content-Type': 'application/json'},
                method='POST' if body is not None else 'GET')
            # Fixed loopback destination; credentials and arbitrary URLs cannot come from the browser.
            try:
                response = urllib.request.urlopen(request, timeout=15)
            except urllib.error.HTTPError as error:
                response = error
            except (OSError, TimeoutError):
                return self.reply({'error': 'Backend unavailable'}, 502)
            with response:
                self.reply(json.loads(response.read(1048576)), response.status)

        def do_GET(self):
            if not self.trusted_host():
                return self.reply({'error': 'Host denied'}, 403)
            if self.path == '/api/tickets':
                if not self.session():
                    return self.reply({'error': 'Open the application first'}, 401)
                return self.forward()
            if self.path in ('/', '/index.html'):
                data = (directory / 'index.html').read_text().replace(
                    '<title>', f'<meta name="nexo-csrf-token" content="{csrf}"><title>').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Set-Cookie', f'support_demo={session_id}; HttpOnly; SameSite=Strict; Path=/')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path not in ('/main.js', '/app.js', '/app.css'):
                return self.reply({'error': 'Not found'}, 404)
            return super().do_GET()

        def do_POST(self):
            # A deterministic local provider fixture; it never calls a model service.
            if not self.trusted_host():
                return self.reply({'error': 'Host denied'}, 403)
            if self.path == '/v1/chat/completions':
                expected = 'Bearer ' + os.environ['SUPPORT_MODEL_KEY']
                if not secrets.compare_digest(self.headers.get('Authorization', ''), expected):
                    return self.reply({'error': 'Provider credential denied'}, 401)
                try:
                    self.read_json()
                except (ValueError, UnicodeError):
                    return self.reply({'error': 'Invalid JSON'}, 400)
                return self.reply({'choices': [{'message': {'role': 'assistant',
                    'content': 'A customer reports a duplicate order charge.'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 40, 'completion_tokens': 12, 'total_tokens': 52}})
            if self.path != '/api/tickets':
                return self.reply({'error': 'Not found'}, 404)
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if (not self.session() or self.headers.get('Origin') != origin
                    or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf)):
                return self.reply({'error': 'CSRF denied'}, 403)
            try:
                value = self.read_json()
                if (not isinstance(value, dict) or set(value) != {'text'}
                        or not isinstance(value['text'], str) or not 1 <= len(value['text']) <= 1000):
                    raise ValueError('Invalid ticket')
            except (ValueError, UnicodeError):
                return self.reply({'error': 'Use a JSON ticket with 1–1000 text characters'}, 400)
            return self.forward(json.dumps(value).encode())

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True, help='Dedicated local directory for assets and SQLite state')
    args = parser.parse_args()
    state = args.state.resolve()
    state.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'nexo', 'web', 'build', str(HERE / 'app.nexo'), '-o', str(state / 'dist')], check=True)
    os.environ['SUPPORT_MODEL_KEY'] = secrets.token_urlsafe(32)
    key = secrets.token_urlsafe(32)
    gateway = create_gateway(state / 'dist', key)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    backend = None
    try:
        backend = subprocess.Popen([sys.executable, '-m', 'nexo', str(HERE / 'backend.nexo')],
            cwd=state, env={**os.environ, 'SUPPORT_JWT_KEY': key, 'NEXO_WEB_LOCAL': '1'})
        for _ in range(100):
            if backend.poll() is not None:
                raise RuntimeError('Backend stopped; check diagnostics above')
            try:
                with urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=0.2) as response:
                    if response.read() == b'ready':
                        break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError('Backend did not become ready')
        print('Local support demo: http://127.0.0.1:8765/ (fixed model stub; Ctrl+C to stop)', flush=True)
        backend.wait()
    except KeyboardInterrupt:
        pass
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join()
        if backend is not None and backend.poll() is None:
            backend.terminate()
            try:
                backend.wait(timeout=5)
            except subprocess.TimeoutExpired:
                backend.kill()
                backend.wait()


if __name__ == '__main__':
    main()
