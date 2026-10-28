# Shield in-memory HTTP routes

`--web=memory` enables the existing `server`/route syntax for bounded,
synchronous dispatch inside the process. It does not open listening sockets or
start server threads. [Bounded native loopback ingress](shield-ingress.md) is available separately.
Persistent production listeners and TLS termination remain separate work.

```nexo
policy:
    deny shell
server api:
    get "/hello/{name}"(name: str):
        return "hello {name}"
response = api.dispatch("GET", "/hello/Ana")
print(response["status"], response["body"])
```

```sh
nexo security run --web=memory app.nexo
nexo security run --web=memory --dependencies permissions.json app.nexo
nexo security trace --web=memory --authorization authorization.json app.nexo
```

Both engines accept exactly one `--web=memory`; unknown modes and duplicate flags
fail before execution. The trusted launcher forwards the mode to Linux payloads.
Compatible single-thread Linux profiles can dispatch routes without network or
thread permissions; unsupported kernels still fail before application execution.

## Dispatch and authority

`server.dispatch(method, target, headers: {}, body: "")` returns a plain map:
`status` (integer), `body` (UTF-8 text), and `headers` (text map). `server.url` is a
`memory://name` display identifier; it is not a network URL. The configured port
is not used. The HTTP client does not implicitly route such URLs to a server.

Every server captures its construction context. Every route captures its
registration context. Dispatch intersects both with the current caller before
authentication, typed argument conversion, record/function defaults, handler
execution, deferred result binding and serialization. A route installed inside
a restricted tool/handler retains that ceiling after its installer returns.
Nested, parallel and spawned dispatch retain these contexts. Registry snapshots
and per-route rate accounting use locks; callbacks run outside registry locks.
New routes registered during dispatch participate in subsequent dispatches.

HTTP status/error mapping, query/path parameters, JSON request decoding, JWT
verification, `@public`, `@role`, `@limit`, CSRF checks and secure response headers
reuse the existing web dispatcher. Ordinary language failures retain existing
HTTP error mapping; security diagnostics abort the call/job, without being caught
as ordinary failures or changed into HTTP 500 responses. Scopes restore the
caller's context after success and ordinary failure.

Bodies supplied to dispatch must be text. Headers must be a map of HTTP-token
names and text values without control characters; names normalize to lowercase.
Case-colliding duplicates and framing headers (`Content-Length`,
`Transfer-Encoding`, `Connection`) are rejected. The virtual Host is fixed to
`memory.nexo` and cannot be overridden. The virtual peer IP is `127.0.0.1`.
This is local invocation: JWT tests do not establish authenticated external
network ingress, and caller-supplied headers are not trusted identity grants.

Response data must be plain materialized data (scalars and lists/maps/sets of
plain data). Callable, deferred and opaque values, records and secret wrappers
cannot cross the response boundary. For typed record input, return selected data
fields or a plain map. This is capability attenuation, not automatic information
flow tracking or a per-route caller ACL; trusted application code controls which
plaintext it returns.

## Bounds and runtime guards

| Resource | Limit |
| --- | ---: |
| Servers admitted per process | 16 cumulative |
| Valid dispatch admissions across all servers | 4096 cumulative |
| Server name UTF-8 text (nonempty) | 256 bytes |
| Target/route path UTF-8 text | 8192 bytes |
| Header entries | 128 |
| Route parameter type descriptors | 128 |
| Supplied header UTF-8 names/values plus separators | 64 KiB |
| Request body | 1 MiB |
| Serialized response body | 8 MiB |

Targets must be relative paths beginning with one `/`, with no fragment, ASCII whitespace
or ASCII control characters. Methods are GET, POST, PUT, PATCH, DELETE, HEAD or OPTIONS.
Servers and routes also consume the shared [registration budget](shield-budgets.md).
Budgets are process-wide and do not reset per server, caller, test or task.
Input validation precedes request admission; request count is charged before
route authentication/processing. Header/body/target errors and oversized responses
produce fatal `security_web_request` diagnostics. General allocations, CPU and
wall-clock cost before/within serialization are not measured by the body limit.

Server construction, route registration, dispatch and low-level listener paths
have runtime guards. Without the explicit memory profile, protected execution
rejects server construction/registration. Listener creation is forbidden even
inside the memory profile. This closes the default `security run` path which
previously could start a server without network authorization when no dependency
manifest was supplied. Ordinary non-Shield servers retain their existing socket
and test-server behavior.

Validation covers both engines, caller/registration ceilings, defaults during
conversion, nested/parallel/spawned calls, JWT and role handling, HTTP error
mapping/restoration, size/count bounds, data-only responses, strict mode flags,
agent/tool dispatch and guards before socket/thread creation. Full positive Linux
isolation deployment validation still requires a compatible host.
