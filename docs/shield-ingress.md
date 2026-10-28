# Bounded native loopback ingress

`--web=loopback:PORT` grants one native HTTP listener on **127.0.0.1**, with the
exact operator-selected port. It requires Linux x86_64 process supervision and enforceable hard resource limits. The Python
CLI delegates this profile to the native compiler/runtime; it does not start a
Python HTTP server. Port `0` explicitly authorizes an ephemeral loopback port.

```nexo
policy:
    deny shell
server api:
    port = 0
    get "/hello/{name}"(name: str):
        return "hello {name}"
```

```sh
nexo security run --web=loopback:0 app.nexo
# stderr: Shield loopback http://127.0.0.1:ACTUAL_PORT/
# GET /hello/Ana with Host: 127.0.0.1:ACTUAL_PORT
```

The payload must register exactly one server with its port matching the CLI
grant. Multiple servers, a mismatched port, duplicate/conflicting profiles,
malformed ports or an unavailable bind fail closed. `PORT`, `NEXO_WEB_LOCAL` and
application-supplied hosts do not override the grant. Ordinary `listen()` and
test/background server paths stay forbidden in Shield. `--web=memory` remains a
separate profile; `server.dispatch` is memory-only.

## Request authority

Each external request receives an **empty capability ceiling by default**, or
the frozen read/env/net ceiling from an explicit [operator ingress manifest](shield-ingress-permissions.md), intersected
with the existing server-construction and route-registration contexts. This
applies before authentication, typed conversion/defaults, handler execution and
response binding/serialization. Root application permissions cannot widen the
request ceiling. Spawned/nested work captures that ceiling as usual. Security
diagnostics end the job with status 1 and a generic HTTP 500 when a reply is
possible; they are not ordinary catchable language failures.

This first ingress profile supports pure processing of request data and already
initialized application data. Without an ingress manifest it does not grant request-time file, environment,
outbound network, shell or foreign-library capabilities. The optional manifest
can attenuate explicit read/env/net grants within the root policy. A lazy JWT secret
needs a matching env grant in both policies at request time; alternatively,
initialize authentication key material during trusted startup. Interactive tool confirmations
are unavailable in this profile.

This is application API attenuation, not a new kernel sandbox, automatic
confidentiality tracking or revocation of pre-existing handles. Trusted code
controls which preloaded plaintext it exposes. Loopback is not per-user
authentication: other local processes can connect. Write permissions and production authentication/TLS integration remain future
work. Root policies delegating shell, C or Python are rejected before compilation
and payload startup; subprocess/foreign-code delegation is not supported here.

## Listener and process lifetime

| Limit | Behavior |
| --- | --- |
| Bind address | 127.0.0.1 only; exact CLI port or explicit ephemeral 0 |
| Accepted connections | At most 8; invalid, stalled and refused HTTP requests also count |
| Accept window | 3 seconds from readiness; current work can extend beyond it |
| Request reads | One absolute 750 ms deadline across headers and body |
| Response writes | 750 ms socket timeout per write; still inside the hard job deadline |
| Hard payload lifetime | 6 seconds from child spawn, including startup, handlers and task joining |
| Response body | 8 MiB serialized text/data |

Each connection handles at most one request and then closes. Idle completion and
connection-budget exhaustion close the listener and return 0 after joined tasks
finish. A busy handler or blocked descendant task cannot defeat the launcher:
it kills and reaps its owned runtime at the hard deadline and returns 124. The
compilation/link step precedes that timer. Listener ports close on normal exit,
security failure and timeout. Compiled payload artifacts are unlinked immediately
after successful exec, so killing a launcher does not retain its executable in
the compiler temporary directory. Linux parent-death signals tie the runtime to its
launcher and tie the delegated native launcher to the Python CLI, preventing a
listener from surviving a killed owner. These are whole-process lifetime bounds,
not individual task cancellation or a persistent server supervisor.

The socket-denied `--sandbox=linux` and `--sandbox=linux-online` profiles are
incompatible and are rejected before application execution. This profile does
not weaken their syscall filters or claim their Landlock/seccomp guarantees.
Unsupported platforms and non-run/trace commands reject the listener profile.

## Strict HTTP envelope

The listener accepts HTTP/1.1 and known method tokens with relative targets up
to 8192 bytes. Headers are bounded to 64 KiB and 128 entries, with ASCII token
names, no control values and no duplicate names (case insensitive). Transfer
encoding is unsupported. Content length must be decimal and at most 1 MiB;
already-buffered bytes beyond the declared body are rejected. Connections are
never forwarded or reused for pipelined requests.

Host must equal the actual `127.0.0.1:PORT`. A supplied Origin must equal
`http://127.0.0.1:PORT`. Supplied `Sec-Fetch-Site` must be `same-origin` or `none`.
Forwarded and X-Forwarded-* headers are rejected; this profile is not a reverse
proxy deployment mode. Invalid local-origin metadata is rejected before route
processing. Standard route matching, ordinary HTTP error mapping, JWT/roles/rate
handling and secure response headers reuse the existing dispatcher. The plain
response-data boundary and [process admission budgets](shield-budgets.md) also
apply.

Validation includes both CLI entrypoints, actual loopback requests, forbidden
file/write operations despite root grants, malformed HTTP/Host/Origin/framing,
read stalls, normal/idle closure, infinite handler termination, owner death,
port mismatch/multiple servers, incompatible modes and native-capability grant
rejection. This bounded local profile is not a persistent production listener.

## Hard payload limits

Before the first application statement, the dedicated loopback payload installs
kernel resource limits: 256 MiB address space, 6 seconds soft CPU / 7 seconds hard
CPU, 16 MiB per output file, 64 open descriptors and zero core-dump size. The
existing owning launcher's 6-second wall deadline still applies. Setup failure
returns 125 before application code. Limits cover startup and requests and are
shared by threads in that process; they do not limit the trusted compiler or
launcher. The address-space limit is not a resident-memory/cgroup budget, and
file-size limits are per file rather than a cumulative disk quota.

These limits do not add syscall or filesystem confinement to loopback. For the
socket-denying syscall filter, Landlock filesystem isolation and DNS-pinned
public HTTP broker, use the separate `--sandbox=linux` or
`--sandbox=linux-online` profiles on a supported host. Those profiles remain
incompatible with a loopback listener and fail closed on unsupported hosts.
