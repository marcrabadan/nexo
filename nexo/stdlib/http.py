"""http: a client with retries (section 15, D98).

`http.get(url)` and the other methods return a response, or fail with a
NetworkError (no answer) or an HTTPError (an answer with status 400 or
more). Redirects of a GET are followed (up to 5); idempotent methods are
retried on network errors and on 502, 503 and 504.
"""
from __future__ import annotations

import socket
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import errors
from ..types import Builtin, Native, Quantity, failure

USER_AGENT = "nexo/0.1"
RETRY_STATUS = (502, 503, 504)
MAX_REDIRECTS = 5


class Response(Native):
    type_name = "response"
    __slots__ = ("status", "body", "headers", "url")

    def __init__(self, status: int, body: bytes, headers: dict, url: str):
        self.status, self.body, self.headers, self.url = status, body, headers, url

    def repr(self) -> str:
        return f"response({self.status})"

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def member(self, call, name: str):
        if name == "status":
            return self.status
        if name == "text":
            return self.text()
        if name == "headers":
            return dict(self.headers)
        if name == "json":
            return Builtin("json", self.json)
        return self.missing(call, name, ["status", "text", "headers", "json"])

    def json(self, call, as_=None):
        from .data import b_json_parse
        return b_json_parse(call, self.text(), as_=as_)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def encode_body(call, body: Any) -> tuple[bytes | None, str | None]:
    from .data import to_json
    if body is None:
        return None, None
    if isinstance(body, str):
        return body.encode("utf-8"), "text/plain; charset=utf-8"
    return to_json(call.interp.bind_value(body), compact=True).encode("utf-8"), "application/json"


def header_values(call, headers: Any) -> dict:
    from .core import expects
    from .crypto import text_or_secret
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise expects(call, "headers", "map", headers)
    out = {}
    for k, v in headers.items():
        if not isinstance(k, str):
            raise expects(call, "headers", "str", k)
        out[k] = text_or_secret(call, "headers", v)
    return out


def seconds(call, value: Any, name: str) -> float:
    from .core import expects
    if isinstance(value, Quantity) and value.dimension == "time":
        return float(value.base())
    raise expects(call, name, "duration", value)


def request(call, method: str, url, body=None, headers=None, timeout=None, retries=None):
    from .core import expects
    if not isinstance(url, str):
        raise expects(call, f"http.{method.lower()}", "str", url)
    if not url.startswith(("http://", "https://")):
        raise failure("NetworkError", call.span, url=url, reason=errors.msg("detail.bad_url"))
    limit = 30.0 if timeout is None else seconds(call, timeout, "timeout")
    idempotent = method in ("GET", "PUT", "DELETE")
    if retries is None:
        retries = 2 if idempotent else 0
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise expects(call, "retries", "int", retries)
    data, content_type = encode_body(call, body)
    extra = header_values(call, headers)
    return send(call, method, url, data, content_type, extra, limit, retries)


def send(call, method, url, data, content_type, extra, limit, retries, stream=False) -> Response:
    """The request, retried `retries` times after a network error or a 502,
    503 or 504 (also used by `ask`). With `stream`, an answer that is not
    an error comes back unread, as a Streaming (D164)."""
    attempt = 0
    while True:
        try:
            return once(call, method, url, data, content_type, extra, limit, stream)
        except _Retry as r:
            if attempt >= retries:
                raise r.failure
            _time.sleep(0.25 * 2 ** attempt)
            attempt += 1


class _Retry(Exception):
    def __init__(self, failure):
        self.failure = failure


class Streaming:
    """An answer read line by line as it arrives (`ask(..., stream: true)`)."""

    def __init__(self, call, resp, url: str):
        self.call, self.resp, self.url = call, resp, url

    def lines(self):
        try:
            for raw in self.resp:
                yield raw.decode("utf-8", "replace")
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise failure("NetworkError", self.call.span, url=self.url, reason=network_reason(exc))
        finally:
            self.resp.close()

    def raw(self):
        """The bytes as they arrive (a binary stream, such as AWS's)."""
        try:
            while True:
                chunk = self.resp.read1(65536)
                if not chunk:
                    return
                yield chunk
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise failure("NetworkError", self.call.span, url=self.url, reason=network_reason(exc))
        finally:
            self.resp.close()


def once(call, method, url, data, content_type, extra, limit, stream=False) -> Response:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        # every hop, so a redirect cannot lead outside the policy (D142)
        from .policy import check_url
        check_url(call, current)
        req = urllib.request.Request(current, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        if content_type:
            req.add_header("Content-Type", content_type)
        for k, v in extra.items():
            req.add_header(k, v)
        try:
            resp = _OPENER.open(req, timeout=limit)
            if stream:
                return Streaming(call, resp, current)
            with resp:
                return Response(resp.status, resp.read(), lower(resp.headers), current)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read() if exc.fp is not None else b""
            if status in (301, 302, 303, 307, 308) and method == "GET" and exc.headers.get("Location"):
                target = urllib.parse.urljoin(current, exc.headers["Location"])
                if origin(target) != origin(current):
                    # credentials are for the origin they were given to
                    extra = {k: v for k, v in extra.items() if k.lower() not in SENSITIVE}
                current = target
                continue
            if status < 400:
                return Response(status, body, lower(exc.headers), current)
            f = failure("HTTPError", call.span, method=method, url=current, status=f"{status} {reason(status)}".strip())
            if status in RETRY_STATUS:
                raise _Retry(f)
            raise f
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise _Retry(failure("NetworkError", call.span, url=current, reason=network_reason(exc)))
    raise failure("NetworkError", call.span, url=url, reason=errors.msg("detail.redirects"))


SENSITIVE = ("authorization", "cookie", "proxy-authorization")


def origin(url: str) -> tuple:
    """(scheme, host, port) of an address."""
    u = urllib.parse.urlsplit(url)
    scheme = u.scheme.lower()
    try:
        port = u.port
    except ValueError:
        port = None
    return scheme, (u.hostname or "").lower(), port or (443 if scheme == "https" else 80)


def lower(headers) -> dict:
    out = {}
    for k, v in headers.items():
        key = k.lower()
        out[key] = v if key not in out else out[key] + ", " + v
    return out


REASONS = {
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    409: "Conflict", 413: "Payload Too Large", 415: "Unsupported Media Type", 422: "Unprocessable Content",
    429: "Too Many Requests", 500: "Internal Server Error", 501: "Not Implemented", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
}


def reason(status: int) -> str:
    return REASONS.get(status, "")


def network_reason(exc: Exception) -> str:
    inner = getattr(exc, "reason", exc)
    if isinstance(inner, (socket.timeout, TimeoutError)):
        return errors.msg("detail.timeout")
    if isinstance(inner, ConnectionRefusedError):
        return errors.msg("detail.refused")
    if isinstance(inner, socket.gaierror):
        return errors.msg("detail.unknown_host")
    return errors.msg("detail.no_connection")


def method(name: str, with_body: bool):
    upper = name.upper()
    if with_body:
        def fn(call, url, body=None, headers=None, timeout=None, retries=None):
            return request(call, upper, url, body, headers, timeout, retries)
    else:
        def fn(call, url, headers=None, timeout=None, retries=None):
            return request(call, upper, url, None, headers, timeout, retries)
    return Builtin(f"http.{name}", fn, fallible=True)


def b_fake(call, name, value):
    """`fake name returns value` inside a test (section 14, D99)."""
    call.interp.fakes[name] = call.interp.store_value(value)
    return None


def exports() -> dict:
    from . import Namespace
    return {"__fake": Builtin("__fake", b_fake), "http": Namespace("http", {
        "get": method("get", False),
        "post": method("post", True),
        "put": method("put", True),
        "patch": method("patch", True),
        "delete": method("delete", False),
    })}

