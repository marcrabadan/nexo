# RFC-0004: M6c — experimental scalar request-time rendering

Status: implemented experimental third M6 slice. Extends RFC-0002's renderer with
a compile-once Python WSGI application and on-demand typed page routes. Hydration,
source-executed server loaders, async data and backend authentication are excluded.

## Interface and ownership

```python
from nexo.web_ssr import SSRApplication

application = SSRApplication.from_file("examples/web/ssr/app.nexo")
response = application.response("/members/123/", method="GET")
assert response.status == 200
```

`SSRApplication(source, source_path=None)` accepts trusted Nexo browser source.
The file factory reads source/imports/CSS and runs the ordinary Python browser
compiler exactly once at construction. It validates every loaded declaration and
branch against the scalar server-rendering subset before accepting requests.
Changing a source file afterward does not change the compiled instance; create a
new application explicitly to reload. No new core syntax, interpreter execution,
server module import or Rust browser-driver parity is introduced.

Each request creates fresh page, layout, component and slot render locals. The
shared analyzed program and CSS are read-only during rendering. No request state,
provider singleton, cache of rendered HTML or mutable current-route field is
stored on the application. Concurrent `response()` calls do not exchange state.
Source paths, exception details and compiler diagnostics are never put into HTTP
error bodies. Rendering performs no file reads or network requests after startup.

At least one public ordinary page is required. Unlike finite SSG, neither a root
page nor a routes JSON file is required. All typed public page patterns can match
on demand. Scalar initial state, named/default props, imports, lexical slots,
conditions, nested layouts and escaped title/description metadata reuse the shared
renderer. Actions/events, requests/mutations, context, structured state and loops
remain rejected. Browser bundle and standalone static behavior stay unchanged.

## Routing and HTTP responses

`response(path, method)` accepts a raw absolute local URL path and returns a frozen
`Response(status, headers, body)`, with immutable header tuples and UTF-8 bytes.
It uses RFC-0003's bounded portable ASCII slug paths, single percent decoding,
reserved asset prefix and typed matching: int64 range, strict integer spelling,
lowercase booleans and static-route specificity. Fixed literal portions of dynamic
patterns are also validated at startup; unsupported patterns cannot silently load.
Rendered local links must match a typed public page but need no enumeration.
Origin-root paths only; query strings, fragments, external URLs, dot segments,
encoded separators and Unicode/punctuation-bearing segments remain unsupported.

- GET returns rendered HTML with status 200. Matched noncanonical paths redirect
  with 308 to their trailing-slash directory URL. Redirect locations are validated
  local paths, never request headers or external authorities.
- HEAD performs the same rendering/status selection, returns the GET headers and
  content length, and sends an empty body. Rendering errors still produce 500.
- `/_nexo/app.css` serves the compiled stylesheet as text/css for GET or HEAD.
- Unknown or mistyped routes return 404, with a public `not_found = true` page if
  declared. Invalid/unsupported URL syntax returns a generic 400 without echoing
  the input. Unsupported methods return 405 with `Allow: GET, HEAD`.
- Runtime overflow, bad generated links, invalid HTML nesting, metadata errors or
  a failed not-found render return 500. A public, layout-free `error = true` page
  can supply that response. If it also fails, a generic 500 document is returned.
  One failed request cannot retain partial state for the next request.

All responses set explicit UTF-8 content type and byte length, no-store caching,
no-referrer, nosniff and a response-header CSP disabling scripts and connections
and forbidding framing. HTML/CSS remain external. There is no JavaScript runtime,
hydration marker, serialized state, cookie creation, redirect based on Host or
reflection of invalid request input. Status/header semantics do not constitute a
complete application security or production assessment.

## WSGI integration and local runner

The application implements the synchronous WSGI callable protocol. It reads only
`PATH_INFO`, `QUERY_STRING` and `REQUEST_METHOD`; it does not consume request bodies,
cookies, Authorization headers, sessions or arbitrary environ values. WSGI already
decodes PATH_INFO; the adapter re-escapes it before the single raw-URL decode, so
literal percent escapes cannot become an unintended second decode. Nonempty query
strings are rejected in this slice. Deployment middleware owns host validation,
authentication, timeouts, limits, TLS, trusted forwarding and server lifecycle.
No ASGI or production WSGI-server implementation is supplied.

```sh
python -m nexo web serve examples/web/ssr/app.nexo --port 4174
```

The CLI compiles once, then starts a single-request-at-a-time WSGI reference runner
bound to 127.0.0.1 only. Port 0 selects an available port; the printed URL gives the
actual port. Host and optional Origin must match the loopback address/port (or
localhost alias). Request logging is disabled so raw parameter URLs are not logged.
This runner is a local development tool with no hot reload or production listener
contract. It does not extend Shield's existing ingress profiles or enforce backend
policy/session/resource authorization. Stop it with Ctrl-C.

## Validation and remaining work

`tests/test_web_ssr.py` checks on-demand routes, request isolation under concurrent
calls, no per-request I/O, typed 404s, invalid path handling, redirects, CSS,
HEAD/405, WSGI decoding, safe 500 fallback, startup rejections, actual HTTP Host/
Origin/status handling, CLI startup and Chromium navigation without JavaScript.
Static/SSG and existing frontend/composition/tooling suites remain regressions.

Hydration needs a separate ownership/state/mismatch RFC. Async server data,
backend identity integration, Unicode/base-path URL support, production serving,
streaming, PWA, WASM interop and subscriptions remain future work. The existing
M5 production qualification does not qualify this SSR mode.

RFC-0005 now adds opt-in fixed-tree scalar hydration to this adapter. The default
mode and this RFC's scalar/noninteractive guarantees remain unchanged; structural
hydration and arbitrary server-state transfer are still outside the implementation.
