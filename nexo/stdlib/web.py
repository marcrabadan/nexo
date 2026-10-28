"""nexo.web: `server` blocks, routes, `jwt`, `@role`, `@limit` (section 15,
D102-D104).

A server answers HTTP/1.1 one request at a time (each connection is
closed after its answer). It starts when the top level of the program has
run (`nexo file.nexo`), or, under `nexo test`, on a free port before the
tests run. The same parser and answers are written by hand in both phases.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import time as _time
import urllib.parse
from typing import Any

from .. import errors
from ..errors import NexoError
from ..types import (
    Builtin, BuiltinType, Function, Native, NexoFailure, Quantity, RecordType, failure, plain, show,
)

MAX_HEAD = 64 * 1024
MAX_BODY = 1024 * 1024
REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 301: "Moved Permanently", 302: "Found", 304: "Not Modified",
    308: "Permanent Redirect", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 411: "Length Required", 413: "Payload Too Large",
    415: "Unsupported Media Type", 422: "Unprocessable Content", 429: "Too Many Requests",
    431: "Request Header Fields Too Large", 500: "Internal Server Error", 501: "Not Implemented",
    503: "Service Unavailable",
}
# secure headers on every answer (section 15: on by default)
SECURE = [
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    ("Referrer-Policy", "no-referrer"),
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Cache-Control", "no-store"),
]
WINDOWS = {"second": 1, "seconds": 1, "minute": 60, "minutes": 60, "hour": 3600, "hours": 3600,
           "day": 86400, "days": 86400, "month": 2592000, "months": 2592000}
PER = ("ip", "user")


def memory_profile():
    return os.environ.get("NEXO_SECURITY_WEB") == "memory-v1"


def require_mode(span=None):
    from .policy import enforcing
    if enforcing() and not memory_profile():
        raise NexoError("security_web_mode", span)


def forbid_listener():
    from .policy import enforcing
    if enforcing():
        raise NexoError("security_web_mode")


def reason(status: int) -> str:
    return REASONS.get(status, "")


# ----------------------------------------------------------------- rates

class Rate(Native):
    """`10 per minute per ip` (D93)."""
    type_name = "rate"
    __slots__ = ("amount", "unit", "per")

    def __init__(self, amount, unit, per):
        self.amount, self.unit, self.per = amount, unit, per

    def repr(self) -> str:
        from ..types import repr_value
        text = f"{repr_value(self.amount)} per {self.unit}"
        return text + (f" per {self.per}" if self.per else "")


def b_rate(call, amount, *words):
    units = list(words)
    if not units or units[0] not in WINDOWS:
        raise NexoError("rate_unit", call.span, unit=units[0] if units else "")
    if len(units) > 2 or (len(units) == 2 and units[1] not in PER):
        raise NexoError("rate_per", call.span, per=units[-1])
    return Rate(plain(amount), units[0], units[1] if len(units) == 2 else None)


# ------------------------------------------------------------------- jwt

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        return None


class Jwt(Native):
    """Tokens signed with HS256 (D103)."""
    type_name = "jwt"
    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key

    def repr(self) -> str:
        return "jwt"

    def secret(self, call) -> bytes:
        from .crypto import text_or_secret
        return text_or_secret(call, "jwt", self.key).encode("utf-8")

    def member(self, call, name: str):
        if name == "sign":
            return Builtin("sign", self.sign)
        if name == "verify":
            return Builtin("verify", self.verify, fallible=True)
        return self.missing(call, name, ["sign", "verify"])

    def sign(self, call, claims, expires=None):
        from .core import expects
        from .data import to_json
        if not isinstance(claims, dict):
            raise expects(call, "sign", "map", claims)
        body = dict(claims)
        if expires is not None:
            if not (isinstance(expires, Quantity) and expires.dimension == "time"):
                raise expects(call, "expires", "duration", expires)
            body["exp"] = int(_time.time()) + int(expires.base())
        head = b64url(b'{"alg":"HS256","typ":"JWT"}')
        payload = b64url(to_json(call.interp.bind_value(body), compact=True).encode("utf-8"))
        mac = hmac.new(self.secret(call), f"{head}.{payload}".encode("ascii"), hashlib.sha256).digest()
        return f"{head}.{payload}.{b64url(mac)}"

    def claims(self, call, token) -> dict | None:
        """The claims of a valid token, or None."""
        # a token is base64url text: anything else is not valid (and would
        # not encode as ASCII)
        if not isinstance(token, str) or token.count(".") != 2 or not token.isascii():
            return None
        head, payload, sig = token.split(".")
        mac = hmac.new(self.secret(call), f"{head}.{payload}".encode("ascii"), hashlib.sha256).digest()
        given = unb64url(sig)
        if given is None or not hmac.compare_digest(mac, given):
            return None
        try:
            from ..json_limits import nesting_within
            head_text = (unb64url(head) or b"").decode("utf-8")
            payload_text = (unb64url(payload) or b"").decode("utf-8")
            if not nesting_within(head_text) or not nesting_within(payload_text):
                return None
            h = json.loads(head_text)
            data = json.loads(payload_text)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(h, dict) or h.get("alg") != "HS256" or not isinstance(data, dict):
            return None
        exp = data.get("exp")
        if exp is not None and (not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp < _time.time()):
            return None
        from .data import from_json
        return from_json(data, call.span)

    def verify(self, call, token):
        claims = self.claims(call, token)
        if claims is None:
            raise failure("InvalidValue", call.span, detail=errors.msg("detail.bad_token"))
        return claims


def b_jwt(call, key):
    from .crypto import Secret
    if not isinstance(key, (str, Secret)):
        from .core import expects
        raise expects(call, "jwt", "str, secret", key)
    return Jwt(key)


# ---------------------------------------------------------------- server

class Route:
    def __init__(self, method, path, handler, types, role, limit, public):
        self.method, self.path, self.handler, self.types = method, path, handler, types
        self.role, self.limit, self.public = role, limit, public
        self.segments = path.strip("/").split("/") if path.strip("/") else []
        self.hits: dict = {}
        self.lock = threading.RLock()
        from .. import security_context
        self.security = security_context.capture()

    def match(self, parts: list[str]) -> dict | None:
        if len(parts) != len(self.segments):
            return None
        params = {}
        for seg, part in zip(self.segments, parts):
            if seg.startswith("{") and seg.endswith("}"):
                params[seg[1:-1]] = part
            elif seg != part:
                return None
        return params


SERVERS: list = []
# diagnostics shown by handlers under `nexo test`
MISTAKES: list = []


class Server(Native):
    type_name = "server"

    def __init__(self, name, port, auth):
        self.name, self.port, self.auth = name, port, auth
        self.routes: list[Route] = []
        self.lock = threading.RLock()
        from .. import security_context
        self.security = security_context.capture()
        self.sock = None

    def repr(self) -> str:
        return f"server({self.name})"

    def member(self, call, name: str):
        if name == "url":
            return f"memory://{self.name}" if memory_profile() else f"http://127.0.0.1:{self.port}"
        if name == "dispatch":
            return Builtin("dispatch", self.dispatch)
        if name == "port":
            return self.port
        if name == "name":
            return self.name
        return self.missing(call, name, ["url", "port", "name"])


    def dispatch(self, call, method, target, headers=None, body=""):
        if not memory_profile():
            raise NexoError("security_web_mode", call.span)
        from ..security_context import check_transfer
        check_transfer(headers, call.span)
        check_transfer(body, call.span)
        if not isinstance(method, str) or method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise NexoError("security_web_request", call.span)
        if (not isinstance(target, str) or not target.startswith("/") or target.startswith("//")
                or len(target) > 8192 or "#" in target or len(target.encode("utf-8")) > 8192
                or any(ord(c) < 33 or ord(c) == 127 for c in target)):
            raise NexoError("security_web_request", call.span)
        if not isinstance(body, str) or len(body) > MAX_BODY or len(body.encode("utf-8")) > MAX_BODY:
            raise NexoError("security_web_request", call.span)
        if headers is None:
            headers = {}
        if not isinstance(headers, dict) or len(headers) > 128:
            raise NexoError("security_web_request", call.span)
        normalized = {"host": "memory.nexo"}
        size = 0
        for key, value in headers.items():
            if (not isinstance(key, str) or not key or len(key) > MAX_HEAD or not key.isascii()
                    or not all(c.isalnum() or c in "!#$%&'*+-.^_`|~" for c in key)
                    or not isinstance(value, str) or len(value) > MAX_HEAD or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise NexoError("security_web_request", call.span)
            name = key.lower()
            if name in normalized or name in {"content-length", "transfer-encoding", "connection"}:
                raise NexoError("security_web_request", call.span)
            size += len(key.encode("utf-8")) + len(value.encode("utf-8")) + 4
            if size > MAX_HEAD:
                raise NexoError("security_web_request", call.span)
            normalized[name] = value
        from ..security_budget import charge
        charge([("web_requests", 1)], call.span)
        answer = handle(call.interp, self, method, target, normalized, body.encode("utf-8"), "127.0.0.1")
        if len(answer.body) > 8388608:
            raise NexoError("security_web_request", call.span)
        response_headers = dict(SECURE)
        if answer.ctype:
            response_headers["Content-Type"] = answer.ctype
        response_headers.update(answer.extra)
        return {"status": answer.status, "body": answer.body.decode("utf-8", errors="replace"), "headers": response_headers}


def b_web_server(call, name, port=8080, auth=None):
    require_mode(call.span)
    if memory_profile() and (not isinstance(name, str) or not name or len(name) > 256 or len(name.encode("utf-8")) > 256):
        raise NexoError("security_web_request", call.span)
    from .core import expects
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise expects(call, "port", "int", port)
    if auth is not None and not isinstance(auth, Jwt):
        raise expects(call, "auth", "jwt", auth)
    from ..security_budget import charge
    charge([("web_servers", 1), ("registrations", 1)], call.span)
    server = Server(name, port, auth)
    SERVERS.append(server)
    return server


def b_web_route(call, server, method, path, handler, types, role=None, limit=None, public=False):
    require_mode(call.span)
    from .core import expects
    if memory_profile() and (not isinstance(server, Server) or not isinstance(handler, Function) or not isinstance(types, list) or len(types) > 128):
        raise NexoError("security_web_request", call.span)
    if memory_profile() and (not isinstance(method, str) or method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
            or not isinstance(path, str) or not path.startswith("/") or len(path) > 8192 or len(path.encode("utf-8")) > 8192
            or any(ord(c) <= 32 or ord(c) == 127 for c in path) or not isinstance(public, bool)):
        raise NexoError("security_web_request", call.span)
    if role is not None and not isinstance(role, str):
        raise expects(call, "@role", "str", role)
    if role is not None and server.auth is None:
        raise NexoError("role_needs_auth", call.span, server=server.name)
    if limit is not None and not isinstance(limit, Rate):
        raise expects(call, "@limit", "rate", limit)
    if limit is not None and (not isinstance(limit.amount, int) or isinstance(limit.amount, bool) or limit.amount < 1):
        raise expects(call, "@limit", "int", limit.amount)
    with server.lock:
        if any(r.method == method and r.path == path for r in server.routes):
            raise NexoError("route_twice", call.span, method=method, path=path)
        from ..security_budget import charge
        charge([("registrations", 1)], call.span)
        route = Route(method, path, handler, list(types), role, limit, public)
        route.span = call.span
        server.routes.append(route)
    return None


def reset() -> None:
    SERVERS.clear()


# ---------------------------------------------------------------- requests

class Request(Native):
    type_name = "request"

    def __init__(self, method, path, headers, query, body, ip, user):
        self.method, self.path, self.headers, self.query = method, path, headers, query
        self.body, self.ip, self.user = body, ip, user

    def repr(self) -> str:
        return f"request({self.method} {self.path})"

    def member(self, call, name: str):
        if name in ("method", "path", "ip", "user"):
            return getattr(self, name)
        if name in ("headers", "query"):
            return dict(getattr(self, name))
        if name == "body":
            return self.body.decode("utf-8", errors="replace")
        if name == "json":
            from .data import b_json_parse
            return Builtin("json", lambda c, as_=None: b_json_parse(c, self.body.decode("utf-8", errors="replace"),
                                                                    as_=as_), fallible=True)
        return self.missing(call, name, ["method", "path", "headers", "query", "body", "json", "user", "ip"])


class Answer:
    def __init__(self, status: int, body: bytes = b"", ctype: str | None = None, extra=()):
        self.status, self.body, self.ctype, self.extra = status, body, ctype, list(extra)


def error_answer(status: int, message: str | None = None, extra=()) -> Answer:
    text = json.dumps({"error": message or reason(status)}, ensure_ascii=False, separators=(",", ":"))
    return Answer(status, text.encode("utf-8"), "application/json", extra)


def read_request(conn: socket.socket):
    """(method, target, headers, body) or an Answer for a bad request."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        data += chunk
        if len(data) > MAX_HEAD and b"\r\n\r\n" not in data[:MAX_HEAD + 4]:
            return error_answer(431)
    head, _, rest = data.partition(b"\r\n\r\n")
    try:
        lines = head.decode("iso-8859-1").split("\r\n")
    except UnicodeDecodeError:
        return error_answer(400)
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return error_answer(400)
    method, target, _version = parts
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            return error_answer(400)
        key = name.lower()
        value = value.strip()
        headers[key] = value if key not in headers else headers[key] + ", " + value
    if "transfer-encoding" in headers:
        return error_answer(501)
    length = headers.get("content-length", "0")
    if not length.isdigit():
        return error_answer(400)
    n = int(length)
    if n > MAX_BODY:
        return error_answer(413)
    body = rest
    while len(body) < n:
        chunk = conn.recv(min(65536, n - len(body)))
        if not chunk:
            return error_answer(400)
        body += chunk
    return method, target, headers, body[:n]


def write_answer(conn: socket.socket, a: Answer) -> None:
    lines = [f"HTTP/1.1 {a.status} {reason(a.status)}".rstrip()]
    if a.ctype:
        lines.append(f"Content-Type: {a.ctype}")
    lines.append(f"Content-Length: {len(a.body)}")
    for k, v in a.extra:
        lines.append(f"{k}: {v}")
    for k, v in SECURE:
        lines.append(f"{k}: {v}")
    lines.append("Connection: close")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
    try:
        conn.sendall(head + a.body)
    except OSError:
        pass


def convert(call, text: str, t: Any, name: str):
    """A path or query text as the declared type of its parameter."""
    from .core import b_validate
    if t is None or (isinstance(t, BuiltinType) and t.name in ("str", "any")):
        return text
    return b_validate(call, text, as_=t)


def body_param(t: Any) -> bool:
    return isinstance(t, RecordType) or (isinstance(t, BuiltinType) and t.name in ("map", "list"))


class Ctx:
    """What a handler call receives as `call`: the interpreter and the
    span of the route."""

    def __init__(self, interp, node, span):
        self.interp, self.node, self.span = interp, node, span


def handle(interp, server: Server, method, target, headers, body, ip) -> Answer:
    require_mode()
    from .. import security_context
    with security_context.intersect(server.security):
        return handle_server(interp, server, method, target, headers, body, ip)


def handle_server(interp, server, method, target, headers, body, ip):
    url = urllib.parse.urlsplit(target)
    if headers.get("x-forwarded-proto", "").lower() == "http":
        host = headers.get("host", "")
        return Answer(308, b"", None, [("Location", f"https://{host}{target}")])
    raw_parts = [p for p in url.path.split("/") if p] if url.path.strip("/") else []
    parts = [urllib.parse.unquote(p) for p in raw_parts]
    found = None
    allowed = []
    with server.lock:
        routes = tuple(server.routes)
    for r in routes:
        params = r.match(parts)
        if params is None:
            continue
        if r.method == method:
            found = (r, params)
            break
        allowed.append(r.method)
    if found is None:
        if allowed:
            return error_answer(405, extra=[("Allow", ", ".join(allowed))])
        return error_answer(404)
    route, path_params = found
    from .. import security_context
    with security_context.intersect(route.security):
        return handle_route(interp, server, route, path_params, url, method, target, headers, body, ip)


def handle_route(interp, server, route, path_params, url, method, target, headers, body, ip):
    if method in ("POST", "PUT", "PATCH", "DELETE") and "origin" in headers:
        origin = urllib.parse.urlsplit(headers["origin"]).netloc
        if origin != headers.get("host", ""):
            return error_answer(403, errors.msg("detail.csrf"))
    ctx = Ctx(interp, route.handler.decl, route.span)
    claims = None
    if server.auth is not None:
        auth = headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else None
        claims = server.auth.claims(ctx, token) if token else None
        if claims is None and not route.public:
            return error_answer(401, errors.msg("detail.need_token"), [("WWW-Authenticate", "Bearer")])
        if route.role is not None:
            roles = claims.get("roles") if claims else None
            ok = claims is not None and (claims.get("role") == route.role or
                                         (isinstance(roles, list) and route.role in roles))
            if not ok:
                return error_answer(403, errors.msg("detail.need_role", role=route.role))
    if route.limit is not None:
        who = ip
        if route.limit.per == "user" and claims and isinstance(claims.get("sub"), str):
            who = "user:" + claims["sub"]
        window = WINDOWS[route.limit.unit]
        now = int(_time.time())
        start = now - now % window
        key = (who, start)
        with route.lock:
            route.hits = {k: v for k, v in route.hits.items() if k[1] == start}
            count = route.hits.get(key, 0) + 1
            route.hits[key] = count
        if count > route.limit.amount:
            return error_answer(429, extra=[("Retry-After", str(start + window - now))])
    query: dict[str, str] = {}
    for k, v in urllib.parse.parse_qsl(url.query, keep_blank_values=True):
        query.setdefault(k, v)
    fn: Function = route.handler
    params = fn.decl.params
    args = []
    try:
        for p, t in zip(params, route.types + [None] * (len(params) - len(route.types))):
            optional = p.default is not None or bool(getattr(p.type, "optional", False))
            if p.name == "request":
                args.append((p.name, Request(method, url.path, headers, query, body, ip, claims)))
            elif p.name in path_params:
                args.append((p.name, convert(ctx, path_params[p.name], t, p.name)))
            elif body_param(t):
                from .data import b_json_parse
                text = body.decode("utf-8", errors="replace")
                if not text.strip():
                    if optional:
                        continue
                    return error_answer(400, errors.msg("detail.need_body"))
                args.append((p.name, b_json_parse(ctx, text, as_=t)))
            elif p.name in query:
                args.append((p.name, convert(ctx, query[p.name], t, p.name)))
            elif not optional:
                return error_answer(400, errors.msg("detail.need_param", name=p.name))
    except NexoFailure as f:
        return error_answer(400, f.error.message)
    try:
        result = interp.call(fn, args, fn.decl)
    except NexoFailure as f:
        return failure_answer(f)
    return answer_of(ctx, result, method)


def failure_answer(f: NexoFailure) -> Answer:
    kind = f.error.kind
    if kind == "NotFound":
        return error_answer(404, f.error.message)
    if kind in ("InvalidValue", "InvalidData"):
        return error_answer(400, f.error.message)
    if f.error.fix == "fix.DatabaseError.unique":
        return error_answer(409, f.error.message)
    from ..cli import report
    report(f)
    return error_answer(500)


def answer_of(ctx, result, method) -> Answer:
    from .data import to_json
    value = ctx.interp.bind_value(result)
    if memory_profile():
        from ..security_context import check_transfer
        check_transfer(value, ctx.span)
    ok = 201 if method == "POST" else 200
    if value is None:
        return Answer(204)
    if isinstance(value, int) and not isinstance(value, bool):
        if not 100 <= value <= 599:
            raise NexoError("web_status", ctx.span, status=value)
        if value >= 400:
            return error_answer(value)
        return Answer(value)
    if isinstance(value, str):
        return Answer(ok, value.encode("utf-8"), "text/plain; charset=utf-8")
    show(value)  # a secret cannot be sent
    return Answer(ok, to_json(value, compact=True).encode("utf-8"), "application/json")


# ---------------------------------------------------------------- serving

def listen(server: Server, port: int) -> None:
    forbid_listener()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1" if os.environ.get("NEXO_WEB_LOCAL") else "0.0.0.0", port))
    except OSError as exc:
        sock.close()
        raise NexoError("web_port", None, server=server.name, port=port, reason=exc.strerror or str(exc))
    sock.listen(64)
    server.sock = sock
    server.port = sock.getsockname()[1]


def serve_one(interp, server: Server) -> bool:
    """Answer one connection; False when a handler stopped with a
    diagnostic (already shown)."""
    conn, addr = server.sock.accept()
    try:
        conn.settimeout(10)
        try:
            req = read_request(conn)
        except (socket.timeout, OSError):
            req = None
        if req is None:
            return True
        if isinstance(req, Answer):
            write_answer(conn, req)
            return True
        method, target, headers, body = req
        try:
            answer = handle(interp, server, method, target, headers, body, addr[0])
        except NexoError as exc:
            # a mistake in the handler: the program shows the diagnostic,
            # then the client gets 500
            from ..cli import report
            report(exc)
            MISTAKES.append(exc)
            write_answer(conn, error_answer(500))
            return False
        write_answer(conn, answer)
        return True
    finally:
        conn.close()


def serve_forever(interp) -> bool:
    """`nexo file.nexo`: the servers answer until the program is stopped.
    False when a handler stopped it with a diagnostic (already shown)."""
    if not SERVERS or memory_profile():
        return True
    forbid_listener()
    port_env = os.environ.get("PORT")
    for server in SERVERS:
        port = int(port_env) if port_env and port_env.isdigit() and len(SERVERS) == 1 else server.port
        listen(server, port)
        sys.stderr.write(errors.msg("info.serving", server=server.name, port=server.port) + "\n")
        sys.stderr.flush()
    import selectors
    sel = selectors.DefaultSelector()
    for server in SERVERS:
        sel.register(server.sock, selectors.EVENT_READ, server)
    try:
        while True:
            for key, _ in sel.select():
                if not serve_one(interp, key.data):
                    return False
    except KeyboardInterrupt:
        pass
    return True


def serve_in_background(interp) -> None:
    """`nexo test`: every server on a free port, answering on its own thread."""
    if memory_profile():
        return
    if SERVERS:
        forbid_listener()
    for server in SERVERS:
        listen(server, 0)

        def loop(s=server):
            while True:
                # a mistake in a handler fails the request (and shows
                # itself), not the tests that are still running
                try:
                    serve_one(interp, s)
                except OSError:
                    return
        threading.Thread(target=loop, daemon=True).start()


def exports() -> dict:
    return {
        "jwt": Builtin("jwt", b_jwt),
        "__rate": Builtin("__rate", b_rate),
        "__web_server": Builtin("__web_server", b_web_server),
        "__web_route": Builtin("__web_route", b_web_route),
    }
