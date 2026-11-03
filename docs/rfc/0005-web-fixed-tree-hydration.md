# RFC-0005: M6d — experimental fixed-tree scalar hydration

Status: implemented experimental fourth M6 slice. Opt-in hydration attaches
existing scalar client behavior to SSR HTML. It does not add structural hydration,
server data loaders, arbitrary serialized state or production qualification.

## Opt-in interface

```sh
python -m nexo web serve examples/web/hydration/app.nexo --hydrate --port 4174
```

```python
from nexo.web_ssr import SSRApplication
application = SSRApplication.from_file("app.nexo", hydrate=True)
```

The default SSR/WSGI mode remains noninteractive and rejects actions/events.
Hydration is not a `build --static` or optimized-release option in this slice.
It uses the same parsed/analyzed program, scalar evaluator, scoped HTML renderer,
HTTP rules and request isolation as RFC-0004. Existing client codegen/runtime
outputs are unchanged; a separate hydration emitter reuses expression/action
lowering and the existing owner/batching/listener primitives.

## Supported tree and state

Public pages, nested layouts, project components, typed scalar props/defaults,
scalar state, lexical slots and native safe elements are supported. Hydration
accepts assignment-only synchronous actions, parameterless event handlers and
existing frozen WebEvent value/checked snapshots for input/change actions.
Every loaded declaration/branch is validated at startup. Conditions and loops
are rejected, including unused ones, because this first slice has a fixed tree.
Context, resources/mutations, structured state, navigation actions, callback props
and arbitrary function calls remain unsupported.

The server still executes no source action. It renders scalar initial state from
props exactly as before, omitting event-handler attributes from HTML. An external,
same-origin ES module attaches client actions after validation. Both CSP headers
and the meta policy allow same-origin scripts only in this explicit mode; no
inline script, eval, inline event code or inline JSON script is introduced.

State transfer is deliberately narrow: the host `#app` carries escaped data
attributes containing the bundle build identity, page declaration identity and a
JSON object of typed route props. Int64 props use decimal strings and are decoded
as checked BigInt; booleans and strings retain their types. Keys and values are
validated against the page schema, including prototype-named scalar properties.
Client state initializers are recomputed from those props and source constants.
No arbitrary server state, request headers, cookies or secrets are serialized.
Any future async/server-derived state needs a separate transfer contract.

## Verification before attachment

The client claims existing component ownership roots, layout outlets, projected
caller roots, native elements and text nodes in source order. It verifies namespace,
tag, exact attributes, initial text, scope identities and complete child sequences.
No selector-by-index guessing, global DOM API interception, virtual DOM or detached
replacement tree is used. The full tree must validate before any listener or
binding is installed. Ownership wrappers preserve caller/receiver CSS boundaries.

The initial binding flush is registered without rewriting the DOM. Existing
nonempty text nodes/elements retain identity and focus. HTML does not preserve an
empty text node; a missing compiled empty text node is inserted only after the
whole tree validates. Initial input value/checked attributes are verified, while
the live properties are left alone, preserving typing, autofill and checkbox edits
made before hydration. These edits are not automatically adopted into Nexo state:
the first subsequent input/change action performs the explicit assignment.
Pre-hydration clicks/events are not replayed.

A wrong build/page identity, malformed/ill-typed prop payload, text/attribute
mismatch, missing node or extra node fails closed with `NW6005`. Planned owners
and subscriptions are disposed, no listeners attach and the owned SSR tree is left
intact. There is no automatic remount, retry, node deletion or state migration.
The external module's auto-boot sets only the host metadata attribute
`data-nexo-hydration` to `ready` or `mismatch`; it sends no event/telemetry or request.
Mismatched controls remain inert while ordinary native links keep working.

The build identity hashes the generated module template, with its self-reference
normalized to a placeholder. This covers the owner runtime, verifier, boot code,
declarations, routes and source-derived CSS scopes. The module
URL includes that identity. This coordinates matching server/client compilation;
it is not a signature, authorization check or SRI guarantee against a compromised
same-origin server. Source bundles and transferred props are public.

## Client lifecycle

The generated module exports `hydrate(target)`. Repeated calls for an actively
hydrated target return the same handle and do not duplicate listeners. Its
idempotent `destroy()` removes owners, listeners and subscriptions and leaves DOM
in place; the host owns eventual DOM removal. A destroyed old handle cannot remove
a newer root's registration. Rehydrating a disposed root works only if its markup
still matches the original initial state. Updated client DOM is not a new server
snapshot and may fail validation, deliberately.

After attachment, ordinary scalar bindings, child prop updates, slot subscriptions,
checked integer arithmetic and synchronous batching use existing owner semantics.
User-controlled text updates use text nodes/property/attribute writes, never HTML
parsing. Actions retain existing error/partial-assignment behavior; SSR error pages
handle server render failures, not client event exceptions.

Navigation remains normal full-document HTML navigation. Each response receives
fresh server state and a fresh client hydration plan; no SPA routing or persistent
layout state across document navigation is added. Without JavaScript, SSR content,
styles and native links still work, while action buttons are inert.

## Assets and bounds

The compile-once SSR application caches `/_nexo/hydrate-<build>.js` alongside its CSS.
GET/HEAD serve the exact cached module with explicit JavaScript content type and
byte length. Rendering and asset serving perform no per-request file/network I/O.
The package includes the hydration runtime resource. Source changes require a new
application/restart, as in RFC-0004; there is no hot reload or hydration optimizer.

Origin-root hosting, portable ASCII paths, loopback reference-runner limits,
HTTP status rules, backend identity exclusions and WSGI integration responsibilities
remain those of RFC-0004. No production host, backend authorization/session system,
streaming, cross-engine qualification or a11y certification is added.

`tests/test_web_hydration.py` covers opt-in behavior, deterministic assets, precision,
startup rejections, actual existing-node/focus preservation, pre-hydration input
and checkbox edits, reactive updates, typed events, child props/slots, mismatch
rejection, disposal/retry, prototype keys, no request I/O and JavaScript-disabled
fallback. Existing SSR/static/SSG/frontend/tooling/composition suites remain
regressions. Structural hydration, arbitrary state transfer, async server data,
PWA, WASM interop and subscriptions remain future RFC work.

RFC-0006 now extends this protocol with conditional regions and explicit branch/
slot session ownership. Earlier fixed-tree/no-condition statements record this
RFC's original milestone; list hydration and arbitrary state transfer remain future.
