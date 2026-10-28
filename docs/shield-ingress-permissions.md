# Operator-owned ingress permissions

`--ingress file.json` configures the request capability ceiling for the bounded
native `--web=loopback:PORT` profile. Without it, requests keep the existing empty
ceiling. The Python CLI delegates this profile/configuration to the native driver.

```json
{
  "version": 1,
  "application": "main.nexo",
  "server": "api",
  "port": 0,
  "permissions": [
    ["allow", "read", "public"],
    ["deny", "read", "public/private.json"],
    ["allow", "env", "SERVICE_JWT_KEY"],
    ["allow", "net", "api.example.com"]
  ]
}
```

All read targets in this example, including the denied path, must exist. The
manifest's application and read targets resolve relative to the manifest's own
directory. For example:

```sh
nexo security run --web=loopback:0 --ingress ingress.json main.nexo
```

## Binding and freezing

Version 1 requires exactly the fields shown above. Version 2 also requires `routes`, as described below. It resolves the
application to its canonical file identity and requires it to match the launched
application; it checks the manifest port against the explicit CLI port. The
runtime verifies the single registered server's name before binding. Port `0`
means the explicitly granted ephemeral port, rather than its eventual number.

The regular UTF-8 manifest file is read and validated before application code
executes. Its canonical read roots, application identity and permission rules
are serialized into a bounded launcher-owned child snapshot and initialized
before the first application statement. The application does not receive extra
permission merely to read the manifest. The runtime never reloads its file.
Changing it during startup or after readiness cannot change the running grant.
An inherited snapshot environment variable is removed unless supplied by the
current CLI manifest preparation.

Read grant targets are canonicalized once and are not resolved again as grant
roots after startup. Requests still resolve their accessed paths. Redirecting a
symlink or replacing a granted directory with a link cannot widen the frozen
root. These remain API path checks, not kernel inode pinning, a file-content
snapshot or a guarantee against filesystem races/TOCTOU by other processes.

## Attenuation

The request ceiling intersects with all existing application, dependency,
server-construction, route-registration and agent/tool ceilings. It cannot
replace them. A grant in the ingress file is necessary for its operation, and is
not sufficient without every other applicable grant. Deny rules win regardless
of order. Omitted capabilities/targets are denied.

The same immutable engine remains active during JWT secret lookup, typed
argument/default conversion, handler work, response binding and serialization.
Nested handlers, synchronous events, memory streams, spawned tasks and parallel
work retain the existing snapshot rules. Configuring read/env/net does not remove
the original process, connection, HTTP size or deadline budgets. Security failures
remain fatal and cannot become recoverable ordinary language failures.

Version 1 accepts only:

| Kind | Target semantics |
| --- | --- |
| `read` | Existing canonical filesystem paths; directory grants include descendants |
| `env` | Literal POSIX-style environment variable names; no wildcard |
| `net` | DNS/IPv4 host names, optionally `*.domain`; no URL, port, path or bare `*` |

Network grants use the existing application host policy. They are not address
pinning, per-port/method rules, proxy isolation or kernel network confinement.
A network grant permits the supported HTTP methods on that host, including
operations that mutate upstream state; it is not a read-only remote API grant. The root
policy must also allow that host. File contents and environmental plaintext
exposure remain the trusted application's responsibility; this is not automatic
confidentiality/taint tracking. A granted JWT environment name permits lazy key
lookup during authentication; unrelated environment names stay denied.

Filesystem writes, shell execution, C and Python delegation are rejected in these ingress
permission files. Root startup can still perform its separately authorized file
writes, while requests cannot modify configuration/source files through a write
grant. Write grants require separate control-file protections and remain future
work. The loopback profile's existing rejection of root shell/C/Python grants and
interactive confirmations remains in effect.

## Validation and bounds

The manifest is limited to 64 KiB both before parsing and after freezing. JSON
nesting is checked before recursive parsing (maximum 16); duplicate keys,
unknown/missing fields, non-integer version/port, malformed identities, invalid
rules and unsupported capabilities fail closed. Permission arrays contain at
most 128 rules, with 1..64 nonempty text targets each. Each target is at most
4096 UTF-8 bytes, with no ASCII control characters. Server names are nonempty
text up to 256 bytes. Read targets must exist when the launcher freezes them.

`--ingress` requires a loopback run/trace invocation and cannot be duplicated.
Malformed schema/identity/port grants fail before payload startup; a mismatched
registered server fails before listener binding. The six-second payload timer
starts after manifest validation and compilation, as in the base loopback profile.

Tests exercise actual requests through both CLI entrypoints: allowed/denied file,
environment and network use, JWT environment keys, root/dependency attenuation,
deny ordering, snapshot freezing, symlink/directory redirection, manifest-relative
paths, application/server/port identity binding, inherited snapshot rejection,
strict schemas, duplicate/deep/oversized JSON and CLI compatibility.

## Version 2: route ceilings

Use `"version": 2` and add a required `routes` array to the same manifest:

```json
"routes": [
  {"method": "GET", "path": "/items/{id}", "permissions": [["allow", "read", "public"]]},
  {"method": "POST", "path": "/submit", "permissions": []}
]
```

Each route ceiling intersects with the top-level `permissions`; it cannot widen
that server ceiling. Missing route entries and empty route permissions deny all
capabilities. Pure computation remains available. Version 1 retains its existing
server-wide behavior.

Identity is the uppercase method and exact registered route template, including
parameter names. Request parameter values, URL encoding and query strings never
select a grant. The selected template's ceiling is installed before JWT key lookup,
argument conversion, handlers and serialization, and is restored after the request.
JWT environment grants must appear in both the server and relevant route rules.
Routes are capability limits, not authentication or authorization rules for users.

At most 128 unique method/template pairs are allowed. Each entry has exactly
`method`, `path`, and `permissions`. Methods are GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS;
paths start with `/`, contain no query, fragment or ASCII controls, and are at
most 8192 UTF-8 bytes. Rule limits and canonical freezing apply independently to
each route, within the shared 64 KiB document limit. Unused template entries grant
nothing; they do not create routes or require a registered endpoint to exist.
