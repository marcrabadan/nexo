"""Experimental typed SSR, async state and owned host adapters (RFC-0004–0011).

Source is compiled once. Requests receive fresh render locals, never an
interpreter, browser runtime, shared mutable state or access to request secrets.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import inspect
from types import MappingProxyType
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote

from . import web_frontend as F
from .web_codegen import compile_program
from .web_model import load_program
from .web_ssg import canonical, document, match
from .web_static import evaluate, render, text, validate


CSP = "default-src 'none'; script-src 'none'; connect-src 'none'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class RenderRequest:
    path: str
    page: str
    params: object
    page_id: str


_MISSING = object()


class SSRApplication:
    """A compile-once WSGI callable; response() accepts a raw local request URL."""

    def __init__(self, source, source_path=None, *, hydrate=False, loader=None, loader_timeout=10, wasm=False, subscriptions=False, channels=None, assets=None, client_boot=None):
        if loader is not None and not (inspect.iscoroutinefunction(loader) or inspect.iscoroutinefunction(getattr(loader, '__call__', None))):
            F.fail(1, 'Server loader must be an explicit async callable.', 'NW6008')
        if not isinstance(loader_timeout, (int, float)) or not 0 < loader_timeout <= 120:
            F.fail(1, 'Loader timeout must be within 0..120 seconds.', 'NW6008')
        if (wasm or subscriptions or channels) and not hydrate:
            F.fail(1, 'WASM/subscriptions require explicit hydration mode.', 'NW6011')
        self._wasm = bool(wasm)
        self._channels = dict(channels or {})
        self._subscriptions = bool(subscriptions or self._channels)
        self._assets = {}
        from .web_subscriptions import EventChannel
        for path, channel in self._channels.items():
            canonical(path)
            if path == '/' or not isinstance(channel, EventChannel):
                F.fail(1, 'Subscriptions require named local EventChannel endpoints.', 'NW6011')
        for path, spec in dict(assets or {}).items():
            if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or path.startswith('/_nexo/') or any(c in path for c in '?#\\') or any(ord(c) < 33 for c in path):
                F.fail(1, 'Invalid public asset path.', 'NW6011')
            mime, content = spec
            if not isinstance(mime, str) or '\n' in mime or '\r' in mime:
                F.fail(1, 'Invalid public asset MIME type.', 'NW6011')
            self._assets[path] = (mime, content if isinstance(content, str) else bytes(content))
        if set(self._assets) & set(self._channels):
            F.fail(1, 'Asset/subscription endpoints collide.', 'NW6011')
        self._client_boot = client_boot
        if client_boot is not None and (not hydrate or client_boot not in self._assets or self._assets[client_boot][0] not in ('text/javascript', 'application/javascript')):
            F.fail(1, 'Client boot must be an explicitly supplied local JavaScript asset in hydration mode.', 'NW6011')
        self._loader, self._loader_timeout = loader, loader_timeout
        self._program = load_program(source, source_path)
        _, self._css, _ = compile_program(self._program)
        self._hydrate = hydrate
        validate(self._program, routed=True, allow_error=True, interactive=hydrate)
        self._client_assets = {}
        self._build = None
        if hydrate:
            from .web_hydrate import compile_hydration
            self._build, js = compile_hydration(self._program, wasm=self._wasm, subscriptions=self._subscriptions)
            self._client_assets[f"/_nexo/hydrate-{self._build}.js"] = js
        if self._wasm:
            import hashlib
            worker = Path(__file__).with_name('web_wasm_worker.js').read_text(encoding='utf-8')
            self._client_assets['/_nexo/wasm-worker-' + hashlib.sha256(worker.encode()).hexdigest() + '.js'] = worker
        declarations = [d for unit in self._program.units for d in unit.declarations.values()]
        self._pages = tuple(d for d in declarations if d.kind == 'page' and d.public and 'fallback' not in d.route)
        if not self._pages:
            F.fail(1, 'Request-time rendering requires at least one public page.', 'NW6004')
        self._fallbacks = {d.route['fallback']: d for d in declarations
                           if d.kind == 'page' and d.public and 'fallback' in d.route}
        # Validate fixed output paths without requiring finite dynamic enumeration.
        for page in self._pages:
            sample = '/' + '/'.join(part.get('literal', 'parameter') for part in page.route['parts'])
            canonical(sample, page.line)

    @classmethod
    def from_file(cls, source_path, **options):
        path = Path(source_path)
        return cls(path.read_text(encoding='utf-8'), path, **options)

    def _link(self, path, line):
        url, segments = canonical(path, line)
        if match(self._pages, segments) is None:
            F.fail(line, 'SSR link does not match a typed public page.', 'NW6004')
        return url

    async def _load(self, path, page, props):
        from .web_state import patch
        request = RenderRequest(path, page.name, MappingProxyType(dict(props)), page.unit.key + '::' + page.name)
        result = await asyncio.wait_for(self._loader(request), timeout=self._loader_timeout)
        if result is None:
            raise ValueError('Loader must return a state mapping')
        return patch({name: p.type for name, p in page.states.items()}, result)[0]

    def _load_sync(self, path, page, props):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._load(path, page, props))
        raise RuntimeError('Use aresponse inside an async loop')

    async def aresponse(self, path, method='GET'):
        if self._loader is None:
            return self.response(path, method)
        state = {}
        if method in ('GET', 'HEAD') and path not in self._client_assets and path != '/_nexo/app.css':
            try:
                url, segments = canonical(path)
                found = match(self._pages, segments)
                if found and path == url:
                    state = await self._load(url, *found)
            except Exception:
                return self._failure(method)
        return self._respond(path, method, state)

    def _page(self, page, props, initial_state=None):
        from .web_state import patch
        state, state_wire = patch({name: p.type for name, p in page.states.items()}, {} if initial_state is None else initial_state)
        markup = render(self._program, [], entry=page, props=props, routed=True,
                        link=self._link, allow_error=True, interactive=self._hydrate, state=state)
        meta = {key: text(evaluate(expr, props, line)) for key, (expr, line) in page.metadata.items()
                if key in ('title', 'description')}
        content = document(markup, meta.get('title', 'Nexo Web'), meta.get('description'))
        if self._hydrate:
            import json
            from .web_static import escaped
            payload = {name: str(value) if page.props[name].type == 'int' else value for name, value in props.items()}
            attributes = (' data-nexo-build="' + self._build + '" data-nexo-page="' + escaped(page.symbol) +
                          '" data-nexo-props="' + escaped(json.dumps(payload, ensure_ascii=True, separators=(',', ':'))) + '" data-nexo-state="' + escaped(json.dumps(state_wire, ensure_ascii=True, separators=(',', ':'))) + '"')
            content = content.replace('<div id="app">', '<div id="app"' + attributes + '>', 1)
            content = content.replace("script-src 'none'", "script-src 'self' 'wasm-unsafe-eval'" if self._wasm else "script-src 'self'")
            if self._subscriptions or self._wasm:
                content = content.replace("connect-src 'none'", "connect-src 'self'")
            if self._wasm:
                content = content.replace("base-uri 'none'", "worker-src 'self'; base-uri 'none'")
            scripts = f'<script type="module" src="/_nexo/hydrate-{self._build}.js"></script>'
            if self._client_boot:
                scripts += '<script type="module" src="' + escaped(self._client_boot) + '"></script>'
            content = content.replace('</body>', scripts + '</body>')
        return content

    def _response(self, status, content, method, content_type='text/html', extra=()):
        body = content.encode('utf-8') if isinstance(content, str) else content
        csp = CSP.replace("script-src 'none'", "script-src 'self'") if self._hydrate else CSP
        if self._subscriptions or self._wasm:
            csp = csp.replace("connect-src 'none'", "connect-src 'self'")
        if self._wasm:
            csp = csp.replace("script-src 'self'", "script-src 'self' 'wasm-unsafe-eval'") + "; worker-src 'self'"
        headers = (('Content-Type', content_type + ('; charset=utf-8' if isinstance(content, str) else '')),
                   ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'),
                   ('Content-Security-Policy', csp), ('X-Content-Type-Options', 'nosniff'),
                   ('Referrer-Policy', 'no-referrer'), *extra)
        return Response(status, headers, b'' if method == 'HEAD' else body)

    def response(self, path, method='GET'):
        return self._respond(path, method)

    def _respond(self, path, method='GET', state=_MISSING):
        """Render an untrusted raw URL. Render failures never expose diagnostics."""
        if method not in ('GET', 'HEAD'):
            return self._response(405, document('<h1>Method not allowed</h1>', 'Method not allowed'),
                                  method, extra=(('Allow', 'GET, HEAD'),))
        if path in self._assets:
            mime, body = self._assets[path]
            return self._response(200, body, method, mime)
        if path in self._client_assets:
            return self._response(200, self._client_assets[path], method, 'text/javascript')
        if path == '/_nexo/app.css':
            return self._response(200, self._css, method, 'text/css')
        try:
            url, segments = canonical(path)
        except F.WebError:
            return self._response(400, document('<h1>Invalid request path</h1>', 'Bad request'), method)
        try:
            found = match(self._pages, segments)
            if found:
                if path != url:
                    return self._response(308, '', method, extra=(('Location', url),))
                page, props = found
                if state is _MISSING:
                    state = self._load_sync(url, page, props) if self._loader is not None else {}
                return self._response(200, self._page(page, props, state), method)
            fallback = self._fallbacks.get('not_found')
            content = self._page(fallback, {}) if fallback else document('<h1>Page not found</h1>', 'Page not found')
            return self._response(404, content, method)
        except Exception:
            return self._failure(method)

    def _failure(self, method):
        fallback = self._fallbacks.get('error')
        if fallback:
            try:
                return self._response(500, self._page(fallback, {}), method)
            except Exception:
                pass
        return self._response(500, document('<h1>Unable to render this page</h1>', 'Server error'), method)

    def __call__(self, environ, start_response):
        # WSGI PATH_INFO is decoded already. Re-escape it so response() performs
        # exactly one URL decode; percent literals cannot become double escapes.
        path = environ.get('PATH_INFO', '') or '/'
        if path in self._channels:
            method = environ.get('REQUEST_METHOD', 'GET')
            if method not in ('GET', 'HEAD') or environ.get('QUERY_STRING'):
                response = self._response(405 if method not in ('GET', 'HEAD') else 400, '', method, extra=(('Allow', 'GET, HEAD'),))
                start_response(f'{response.status} {HTTPStatus(response.status).phrase}', list(response.headers));return [response.body]
            identity = environ.get('HTTP_LAST_EVENT_ID', '')
            if len(identity) > 80:
                response = self._response(400, '', method);start_response('400 Bad Request', list(response.headers));return [response.body]
            headers = [(key,value) for key,value in self._response(200, '', method, 'text/event-stream').headers if key != 'Content-Length']
            start_response('200 OK', headers)
            return [] if method == 'HEAD' else self._channels[path].events(identity)
        path = quote(path, safe='/')
        if environ.get('QUERY_STRING'):
            path += '?' + environ['QUERY_STRING']
        response = self.response(path, environ.get('REQUEST_METHOD', 'GET'))
        start_response(f'{response.status} {HTTPStatus(response.status).phrase}', list(response.headers))
        return [response.body]


def load_loader(spec):
    import importlib
    import re
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*:[A-Za-z_][A-Za-z_0-9]*', spec):
        F.fail(1, 'Use --loader trusted.module:async_function.', 'NW6008')
    module, name = spec.split(':')
    try:return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):F.fail(1, 'Cannot import configured server loader.', 'NW6008')


def local_server(source_path, port=4174, **options):
    """Create a loopback-only WSGI runner with Host/Origin checks."""
    from wsgiref.simple_server import WSGIRequestHandler, make_server
    if not 0 <= port <= 65535:
        F.fail(1, 'Use a loopback port from 0 through 65535.', 'NW6004')
    application = SSRApplication.from_file(source_path, **options)
    def guarded(environ, start_response):
        host = environ.get('HTTP_HOST', '')
        origin = environ.get('HTTP_ORIGIN')
        permitted = (f'127.0.0.1:{server.server_port}', f'localhost:{server.server_port}')
        if host not in permitted or (origin and origin != 'http://' + host):
            response = application._response(403, document('<h1>Forbidden</h1>', 'Forbidden'),
                                             environ.get('REQUEST_METHOD', 'GET'))
            start_response('403 Forbidden', list(response.headers))
            return [response.body]
        return application(environ, start_response)
    class Handler(WSGIRequestHandler):
        def setup(self):
            super().setup()
            # Browsers may preconnect without sending a request. Do not let an
            # idle connection monopolize this single-request local server.
            self.connection.settimeout(5)
        def log_message(self, *args):
            pass
        def handle(self):
            from wsgiref.simple_server import ServerHandler
            try:
                self.raw_requestline = self.rfile.readline(65537)
            except TimeoutError:
                return
            if len(self.raw_requestline) > 65536:
                self.requestline = self.request_version = self.command = ''
                self.send_error(414)
                return
            if not self.parse_request():
                return
            handler = ServerHandler(self.rfile, self.wfile, self.get_stderr(), self.get_environ(), multithread=bool(application._channels))
            handler.request_handler = self
            handler.run(self.server.get_app())
    if application._channels:
        from socketserver import ThreadingMixIn
        from wsgiref.simple_server import WSGIServer
        import threading
        class BoundedServer(ThreadingMixIn, WSGIServer):
            daemon_threads = True
            def __init__(self, *args, **kwargs):
                self._slots = threading.BoundedSemaphore(8)
                super().__init__(*args, **kwargs)
            def process_request(self, request, address):
                if not self._slots.acquire(False):
                    try:request.sendall(b'HTTP/1.0 503 Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    finally:self.shutdown_request(request)
                    return
                try:super().process_request(request, address)
                except Exception:self._slots.release();raise
            def process_request_thread(self, request, address):
                try:super().process_request_thread(request, address)
                finally:self._slots.release()
        server = make_server('127.0.0.1', port, guarded, handler_class=Handler, server_class=BoundedServer)
    else:
        server = make_server('127.0.0.1', port, guarded, handler_class=Handler)
    return server


def serve(source_path, port=4174, **options):
    """Local single-request-at-a-time runner, not production hosting."""
    with local_server(source_path, port, **options) as server:
        print(f'Nexo Web SSR http://127.0.0.1:{server.server_port}/ (experimental, local only)', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
