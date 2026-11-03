# RFC-0001: Nexo Web

Status: M0 accepted design; M1–M6 implemented experimentally. Edition: 12.
M1–M5 are experimental implementations. [RFC-0002](0002-web-static-html.md) starts
M6a with experimental static component HTML. [RFC-0003](0003-web-routed-ssg.md) adds
M6b routed static generation. [RFC-0004](0004-web-request-time-ssr.md) adds M6c
scalar request-time WSGI rendering. [RFC-0005](0005-web-fixed-tree-hydration.md)
adds opt-in scalar hydration, extended to conditional regions by
[RFC-0006](0006-web-conditional-hydration.md). RFC-0007 through RFC-0011 complete the experimental M6 scope; see the completion checklist below.
The [post-M5 amendment](#post-m5-maintenance-d195) supersedes earlier limitations
on keyed lists, event payloads, static extraction, CSS maps and compatible component HMR.

## Decision and compatibility

Nexo Web is a native Nexo browser language extension, not JSX, React or a
TypeScript DSL. Readability, one canonical spelling, typed boundaries and
compile-time work take priority. `nexo web` selects a separate browser frontend.
`nexo build --target web` retains D175's wasm32-wasip1/WASI behavior unchanged.
The existing `use nexo.web` server library and synchronous `http` remain intact.
The command determines the capability domain; browser files cannot import server
modules, execute top-level statements or gain filesystem/network/process access.

The first frontend compiler lives in `nexo/web_frontend.py` and reuses the core
Nexo expression parser and AST. It builds a typed component/element AST, resolves
state/action names, derives binding dependencies, and lowers to deterministic
JavaScript DOM instructions plus external HTML/CSS. No bundler/runtime dependency
is required. Rust driver support is **not implemented**; use the Python CLI.
This isolates browser grammar without expanding the stable core's contextual
keywords or breaking existing parsers. A Rust port and shared IR serialization
are future engineering work, not parity claims.

D124 remains authoritative for future component imports: qualified `use` names,
project-root resolution, once-only module initialization and `pub` exports.
The original M1 subset accepted only `use nexo.web`. The M2 amendment adds
qualified project imports and aliases without changing core/server modules. `pub component`
marks browser mount exports; it does not change core function/type exports.
No independently importable `view` fragments are introduced.

## Vocabulary and milestones

| Concept | Canonical syntax | Status |
| --- | --- | --- |
| Reusable UI | `component Name:` | M1 |
| Instance-local state | `state value: Type = initial` | M1 |
| Synchronous action | `fn action():` | M1 bounded body |
| DOM description | `view:` | M1 |
| Component CSS | `style:` | M1 bounded CSS |
| Routable screen | `page Name:` with `path` | M2 (amendment) |
| Shared structure | `layout Name:` and `slot()` | M2 (amendment) |
| Shared state | `context Name:`, `provide`, `inject()` | M2 (amendment) |
| Reactive read | `load result = api.get(...)` | Experimental M3 |
| Explicit write | `mutation` with `api.post/put/patch/delete` | Experimental M3 |

Future vocabulary is reserved by this RFC, **not accepted by the M1 compiler**.
Properties, module imports, conditionals, keyed lists, routing and composition
are rejected in M1. Core `if`, `for`, `type`, explicit fallible `T!`/`?` semantics
must be reused when those constructs are implemented, rather than replaced by
hooks or competing state stores.

## Implemented M1 grammar

```ebnf
browser_file = "use nexo.web", NEWLINE, component+ ;
component = ["pub "], "component ", UpperName, ":", NEWLINE, INDENT,
            member+, DEDENT ;
member = state | action | view | style ;
state = "state ", name, ": ", ("int" | "str" | "bool"), " = ", constant ;
action = "fn ", name, "():", NEWLINE, INDENT, assignment+, DEDENT ;
assignment = state_name, ("=" | "+=" | "-=" | "*="), expression ;
view = "view:", NEWLINE, INDENT, element+, DEDENT ;
element = html_tag, ":", NEWLINE, INDENT, element+, DEDENT
        | html_tag, "(", [expression], {", ", attribute, ": ", expression}, ")" ;
style = "style:", NEWLINE, INDENT, css, DEDENT ;
```

Four spaces per level; tabs are rejected. Blank lines and Nexo line comments are
allowed outside CSS. Component names start with an uppercase ASCII letter;
other names use ASCII letters, digits and underscore, starting with a letter or
underscore. Core reserved words cannot name state or actions. Declare state before the actions/view that reference it. All actions
are resolved before event binding, so view handlers may reference later actions.
Exactly one nonempty view, at most one style, unique state/action names, and at
least one public component are required. There are no props or parameters yet.

Expressions reuse Nexo strings (including `{state}` interpolation and `{{`),
booleans and integer literals; state reads; checked integer `+`, `-`, `*` and
unary minus; and string `+`. Other expression forms fail `NW1002`. Initializers
are constant expressions with no state reads. Values must match their declared
state type. Compound assignments require int. Int is signed 64-bit, represented
by JavaScript BigInt to preserve precision; overflow raises a runtime RangeError.
Runtime error-page UI and fallible action bodies are deferred. An action commits
assignments in order; if a later statement overflows, earlier assignments remain
and their bindings flush in `finally` (no implicit transaction rollback).

Element calls take at most one text value followed by named attributes.
Text always becomes a DOM text node, never HTML. Blocks nest HTML elements.
HTML tags are an explicit safe subset: main, section, article, header, footer,
nav, div, span, h1–h6, p, button, label, input, ul, ol, li, strong, em, small,
form, fieldset, legend, output, pre, code, br and hr. Void tags have no children.
Attributes: class, id, title, role, type, name, placeholder, disabled, aria_label,
aria_live, aria_describedby, aria_hidden and tabindex. Underscores map to hyphens.
`disabled` requires bool; other attributes require str/int. URL, raw HTML, inline
style and raw event-handler attributes are rejected. `button` defaults to
`type="button"`. Native semantics support keyboard activation; authors still
supply meaningful names, landmarks and appropriate ARIA. No automatic full
accessibility certification is claimed.

Events `on_click`, `on_input`, `on_change` name parameterless declared actions.
Browser events are not passed into Nexo yet. Event closures retain their own
instance state and never mutate another instance.

## Reactive graph and lifecycle

The compiler computes the exact set of state names used by each dynamic text or
attribute expression. Static text/attributes produce initialization instructions
without subscriptions. M1 emits direct DOM creation; static template extraction
and hoisting are specified for the optimizer but **not implemented**.

A mount creates fresh state, bindings, listeners and a `data-nexo` ownership root.
The generated ES module exports `Counter(target: Element)` for a public Counter;
this JavaScript host adapter is not a second Nexo authoring syntax. The default
HTML mounts the first public component. Host code can mount it multiple times.
The mount returns `{ destroy() }`; destruction is synchronous/idempotent, removes
listeners, clears subscriptions/dirty state and removes the owned root. Consumers
must call destroy before discarding an instance; arbitrary external DOM removal
does not automatically dispose the instance.

An event batches synchronous assignments. Equal values produce no notification.
After the action, each binding depending on a changed state runs once, in source
order. A binding caches its rendered value and skips identical DOM writes, including
when a batch returns state to its initial value. Only its text node/attribute
changes; existing elements and unrelated
bindings retain identity. There is no VDOM, scheduler microtask, effect API or
user-defined reactive side effect. M1 scans the binding table to find affected
bindings; an adjacency index is a future optimization. Temporary intermediate
state is visible to later action statements, but not rendered between them.

## CSS

CSS is emitted as an external asset wrapped in native
`@scope ([data-nexo="stable-hash"]) to ([data-nexo])`. Descendant ownership roots
are boundaries. The hash depends on component name and source bytes, with no
absolute path, time or random data. Standard selectors, declarations, CSS custom
properties/design tokens, `@media`, `@container` and `@supports` are retained.
Native @scope support is required; older browsers are unsupported.
External CSS imports/resources (including url/image-set/image/src functions), global at-rules (`@font-face`, `@keyframes`,
`@property`, etc.) and escapes are outside M1. The compiler bounds braces and
at-rules; browser CSS parsing still determines declaration validity. This is not
a full CSS linter/minifier. Asset minification and external CSS imports are planned.

## Milestone boundaries

Pages, layouts, typed scalar props, project imports and owned context are now
implemented by the [M2 amendment](#m2). Its concrete grammar, lifecycle, routing,
security limits and tests supersede the original M2 design-only roadmap. The M1
grammar above records the preserved subset, not a separate compiler mode.

M3 asynchronous typed requests are implemented by the [M3 amendment](#m3).
It supersedes the original API design sketch; no server HTTP behavior changes.

M4 uses an optional pinned Vite integration with conservative root HMR,
line-level JS source maps, diagnostics, source audit and an explicit browser LSP.
See the [M4 amendment](#m4). M5 adds release optimization below; full security/a11y certification remains
workload-specific.
M5 adds release optimization, measured quality gates and a verified static deployment
example; see the M5 amendment. Broader production qualification remains workload-specific. [RFC-0002](0002-web-static-html.md) starts M6a with bounded static component HTML.
[RFC-0003](0003-web-routed-ssg.md) adds finite routed SSG and
[RFC-0004](0004-web-request-time-ssr.md) adds scalar request-time rendering.
[RFC-0005](0005-web-fixed-tree-hydration.md) adds fixed-tree scalar hydration.
[RFC-0006](0006-web-conditional-hydration.md) extends it with conditional regions.
RFC-0007–0011 add list/context hydration, async state, PWA, WASM interop and subscriptions. Historical M1–M5 amendments below retain their original
milestone boundaries; RFC-0002 through RFC-0011 supersede their blanket M6-future statements.

## Security

No server module imports, secrets access, arbitrary calls, script tags, URLs,
HTML sinks or network API exist in the M1 browser subset. All bundle source is
public: never place secrets in browser files. Generated pages use external
scripts/styles and a restrictive CSP, without unsafe-inline/eval. Deploy over
HTTPS with response-header CSP (including frame-ancestors), correct MIME types
and other deployment controls. The development static server is not production
hosting. Browser visibility never grants backend authorization.

Shield is a server-side foundation, not a browser sandbox or production listener.
Existing policy/JWT/@role/@public/@limit and CSRF behavior remains unchanged.
Actual Shield profiles include in-memory dispatch and separately bounded loopback
ingress; neither establishes a persistent public production service. Authentication,
resource-level authorization, input validation, rate limits, secure HttpOnly/SameSite
session cookies and CSRF protection belong at supported backend boundaries.
Map future reviews to OWASP ASVS 5.0 validation, encoding, browser security,
authentication, session, authorization and secure-communication controls. This RFC
is not an ASVS assessment or certification. See ../shield-web.md and
../shield-ingress.md for supported execution bounds.

## Commands, diagnostics and validation

Available through the installed Python CLI (or `python -m nexo`):

```sh
python -m nexo web check --json examples/web/counter.nexo
python -m nexo web build examples/web/counter.nexo -o /tmp/nexo-counter
python -m http.server 8765 --bind 127.0.0.1 --directory /tmp/nexo-counter
# Open http://127.0.0.1:8765/
python -m pytest tests/test_web_frontend.py
```

Build emits index.html, main.js, app.js, app.css with deterministic UTF-8 bytes.
`--json` returns schema_version 1 and diagnostics with code/file/line/severity/
message. Exit status is zero on success, one on compile/I/O failure; CLI argument
errors use argparse's status two. NW1000: I/O; NW1001: grammar/unsupported feature;
NW1002: name/type/expression; NW1003: CSS boundary; NW1004: unsafe DOM construct.
Compile diagnostics carry a fix sentence. Locations refer to the original source
line for expressions and declarations, with M4 statement columns/snippets and opt-in line-level JS source maps.
These codes are browser-specific; core catalogs/diagnostics remain untouched.

Troubleshooting: use the Python entry point, serve over loopback HTTP instead of
file URLs, check DevTools for CSP/module errors, use an @scope-capable browser,
call destroy on discarded mounts, and keep unsupported syntax out of M1 files.
Missing Playwright/Chromium must be reported as skipped/unverified browser QA.

M1 tests cover deterministic output, type/capability rejections, JSON diagnostics,
actual browser state/DOM updates, batching, unchanged node identity, independent
instances, text escaping, scoped styles, keyboard activation and disposal.
The website generator embeds the exact acceptance source and builds its runnable
demo; AI Guide generation emits the same verified source as a conditional topic.
The full core suite remains the regression oracle. No performance or production
readiness claim follows from these functional tests.

<a id="m2"></a>

## M2 amendment — implemented experimental composition and routing

D191 extends the isolated Python browser frontend, preserving M1 source programs,
core/server execution, D124 lookup/privacy and D175 WASI. The parser/dependency
loader is in nexo/web_model.py; semantic analysis and deterministic lowering are
in nexo/web_codegen.py; owner scheduling is in web_runtime.js and the fragment
router in web_router.js. There is no bundler or new production dependency.
Rust browser-driver parity remains unimplemented.

### Canonical M2 grammar

```nexo
use nexo.web
use ui.widgets

pub context Visits:
    state count: int = 0
    fn increment():
        count += 1

pub layout Shell:
    provide Visits
    visits = inject(Visits)
    view:
        header:
            p("Visits: {visits.count}")
            a("Home", href: "/")
        slot()

pub page Home:
    path = "/"
    layout = Shell
    title = "Home"
    visits = inject(Visits)
    view:
        main:
            widgets.Card(heading: "Welcome"):
                p("{visits.count}")
                button("+", on_click: visits.increment)
```

The example requires a pub Card component in ui/widgets.nexo. The complete,
compiler-checked project is examples/web/composition/app.nexo and its dependency
files. The M1 Counter is still the canonical standalone acceptance example.

- Components declare typed scalar props with ordinary Nexo parameter syntax:
  `component Badge(label: str, start: int = 0):`. Empty property parentheses are
  rejected; omit parentheses when there are no properties. Calls use named props.
  Types are int/str/bool; defaults are constant expressions of the declared type.
  Unknown/missing/incorrectly typed props fail compilation. Host JS adapters also
  validate values (int uses BigInt). Props are read-only in Nexo actions. Host
  controls expose update(props), replacing the complete prop set; defaults apply
  again, local state is retained. Initial state may read props once, not other
  state or context. A default auto-mounted entry needs defaults for all its props;
  wrap reusable required-prop components in an application component.
- Component blocks project nested content to one `slot()`. No positional child
  props, independent view fragments, named slots, fallback slot syntax, recursive
  composition or callback props in M2. Slot code retains caller lexical values,
  context and CSS; its own owner subscribes to caller dependencies and is disposed
  by the receiving owner. Children of a component without a slot are an error.
- `layout Name:` requires exactly one slot, has no props, and can select a parent
  through `layout = Parent`. Cycles fail compilation. Layouts belong to routing;
  reusable ordinary widgets remain components. Layout state/providers persist
  through the common prefix of route layout chains; departing descendants are
  destroyed from inner to outer. Parameter changes remount pages but retain the
  common layout chain. Navigating away and returning creates fresh page state.
- `context Name:` contains typed scalar state and parameterless assignment actions.
  `provide Name` creates a fresh instance at an owner; `binding = inject(Name)`
  resolves its nearest ancestor/self provider. Consumers read fields and invoke
  declared actions, never assign fields. Missing providers on reachable entry/
  route composition paths fail compilation; runtime injection also fails closed.
  Contexts cannot inject/provide other contexts in M2. Providers are disposed after
  descendants. No global context singleton or SSR implementation is introduced.
- `page Name(param: Type):` requires `path = "/users/{param}"`. A path is a static
  pattern: braces declare typed parameter names instead of interpolating values;
  all other strings keep ordinary Nexo interpolation. Placeholders occupy entire
  segments, match declared non-defaulted parameters exactly, and use strict int64,
  str or lowercase true/false conversion. No wildcard/query/optional-parameter API.
  Invalid conversion or encoded slash/control input selects not-found. Static
  segments outrank placeholders. Equal-specificity intersecting patterns are
  rejected instead of relying on registration order.
- Public pages in explicitly loaded project modules form the route registry. A
  `not_found = true` page has no path/parameters and may select a layout. An
  `error = true` page has no path/parameters/layout, so layout failures can recover.
  Mount/navigation failures clean partial owners and render that page, or a safe
  generic message. Event-action exceptions retain their existing behavior and are
  not swallowed by navigation error handling.
- `title` and `description` accept strings with route-prop interpolation, not live
  mutable state. Navigation sets text/attributes safely and focuses the new main/
  heading. Router destruction restores previous metadata and removes its hash
  listener. Use one router per document for document-level navigation/metadata;
  each router root nevertheless owns an independent context tree.
- Canonical URLs use fragments (`#/users/7`). Anchors `a("User", href: "/users/7")`
  and action `navigate("/users/7")` accept local route strings only; no external
  schemes, protocol-relative links, backslashes, controls or query/fragment input.
  This works on GitHub Pages and a plain static server without rewrite rules.
  Programmatic host controls offer navigate(path) and idempotent destroy().

### Modules, CSS, scheduling and diagnostics

`use ui.widgets` resolves from the nearest nexo.toml project root, or the entry's
folder when no project file exists; access is qualified as widgets.Card. `as`
aliases work. Files load once by resolved identity. Only pub declarations cross
module boundaries. Import/module/composition/layout/CSS cycles are errors. Module
aliases cannot be rebound by local members. M2 supports project modules, not
package resolution or server/native imports. No source/module initializer runs
on the server or in the interpreter during browser compilation.

`@import "./card.css";` within style expands local CSS relative to the declaring
file. Nested local imports resolve recursively; all resolved paths must remain
inside the project root and use .css. Network/absolute/escaped imports and cycles
are rejected. Expanded rules remain within the importing component's native
@scope boundary. Resource functions and global rules retain M1 restrictions.
CSS syntax checking still belongs to the browser; this is not a full CSS parser.
Dependencies, including project modules/CSS/config, participate in AI evidence
hashing. Output bytes never depend on absolute paths or timestamps.

Batches now span local owners, context actions and projected content. Owners flush
in deterministic creation order per wave, bindings in source order, with child
prop updates settled in subsequent waves before the outer batch returns. Equality
caches skip redundant DOM writes. Owner destruction removes pending work, links,
effects and listeners, then removes DOM. Partial mount failures dispose already
created descendants/providers. Context ownership is per root; future SSR must
allocate a fresh root per request and never reuse a client/root provider globally.

Diagnostics retain schema_version 1 and NW1000–NW1004. Added: NW2001 project
modules/imports; NW2002 typed props/composition; NW2003 route/metadata/layout
analysis; NW2004 provider/context access; NW2005 slot ownership. All provide fix
sentences. CLI commands remain web check [--json] and web build [-o directory].
A routed build auto-starts mountApp; a component-only build mounts its first
public component. Run tests/test_web_frontend.py and tests/test_web_composition.py.

In the historical M2 subset, APIs/load/mutation and records/lists/conditions were
not yet supported; the M3 amendment below implements them. Vite/HMR, source maps,
SSR/hydration/SSG, package imports and production certification remain outside
this amendment. It supersedes the corresponding M2 design-only statements above;
the M1 subset and historical roadmap remain recorded for provenance.


<a id="m3"></a>
## M3 amendment — asynchronous typed data (edition 3)

This experimental Python browser implementation extends M1–M2. There is no Rust
browser driver. D175 WASI and the synchronous server HTTP library are unchanged.

### Grammar and types

```nexo
use nexo.web

type Product:
    id: int
    name: str

type NewProduct:
    name: str

pub component Products:
    load products = api.get("/api/products", as: list[Product])
    mutation create = api.post(
        "/api/products",
        body: NewProduct(name: "Guide"),
        as: Product,
        invalidate: [products]
    )
    view:
        main:
            if products.loading:
                p("Loading...", role: "status")
            elif products.error != none:
                p(products.error.message, role: "alert")
                button("Retry", on_click: products.reload)
            elif products.ready:
                for product in products.data:
                    p(product.name)
            button("Create", on_click: create.run, disabled: create.loading)
```

Contracts use existing `type` field syntax and `pub`/qualified project imports.
Allowed types: int, str, bool, nonempty field-only records and nested lists of
these. No defaults, annotations, methods, recursion, union/optional/float types,
unknown or missing fields. Record constructors require every named field exactly
once; list literals are nonempty and homogeneous. JSON contracts are immutable
snapshots, not local writable state or new component property types. Runtime
validation checks both body and response recursively (maximum depth 32, maximum
10,000 items per list). JSON wire integers must be exact safe integers within
±9,007,199,254,740,991 and become Nexo BigInt internally. Larger wire integers are
rejected rather than rounded; represent identifiers outside this range as str.
Only JSON media types and UTF-8 are accepted; empty/204/non-JSON responses are
validation failures. Response errors never expose raw backend bodies.

```text
load name = api.get(url: str, as: T,
    timeout_ms: int-literal = 10000, retries: int-literal = 0,
    cache_ms: int-literal = 0, dedupe: bool-literal = true)
mutation name = api.post|put|patch(url: str, body: TypedValue, as: T,
    timeout_ms: int-literal = 10000, invalidate: [localLoadNames] = [])
mutation name = api.delete(url: str, as: T, body: TypedValue (optional),
    timeout_ms: int-literal = 10000, invalidate: [localLoadNames] = [])
```

The URL is one positional argument; options are named and unique. Requests are
UI members, not context members or general await expressions. timeout_ms is
1..120000, retries 0..3 and cache_ms 0..60000. GET has no body. Mutation retry/cache
must stay zero; disabling deduplication is a GET option. Only GET coalesces calls.
Mutation `body` may be a scalar, immutable list or record constructor with matching
static types. Context fields, local state and props can supply URL/body values;
requests cannot depend on other request results. GET reacts to changes in its
scalar URL inputs; mutation inputs are evaluated once when explicitly run.

### Resource states, errors and rendering

Reads start on mount; mutations start idle. Both expose loading, ready, success,
error and data. Start clears old data/error and sets loading. Success sets ready,
validated data, loading=false, error=none; only a mutation sets success=true.
Failure/cancel clears data/ready/success and sets a typed error. No stale-data
rendering is implied. Read data only under `if/elif resource.ready:`; mutation
`success` also narrows data. Error message/kind/status are available after
`if/elif resource.error != none:`. Compound conditions do not narrow yet.

Error fields: kind:str, message:str, status:int (0 except HTTP errors).
Kinds: Http, Network, Timeout, Cancelled, Validation, Limit, Security. Runtime
resource errors are explicit values to render/retry, not thrown into page error
boundaries. They do not introduce implicit `?` propagation or change core T!.
`load.reload()` bypasses cache, `load.cancel()` cancels that consumer;
`mutation.run()` explicitly starts a write, `mutation.cancel()` cancels it.
Use method references directly as event handlers or zero-argument calls inside
an action. Repeated mutation.run during an active write is ignored. Cancelling a
write cannot undo a server commit and does not automatically invalidate reads.

Views now accept if/elif/else and `for item in typedList:`. Conditions require
bool; scalar equality/order and bool and/or/not are supported. Each branch/list
snapshot owns descendant state, subscriptions/listeners and requests. Changing
branch or replacing a list destroys those owners before rendering replacements.
Items are immutable and loop bindings cannot shadow existing names. There is no
keyed diff: replacing a list resets descendant local state. Structural updates
settle parent owners before descendant effects, preventing reads of invalidated
data. Slot content retains its caller's narrowed/item bindings and receiver
lifetime. Regions use comment anchors and document fragments, preserving semantic ul/li
parentage without extra wrapper elements. Inspect application accessibility.

### Transport, ownership and security

fetch is asynchronous. Timeout covers retries and streaming body reads. GET
retries opt in to bounded 100/200/400 ms delays, only on network, 429 and 5xx;
validation/security/other 4xx and writes do not retry. Streaming response maximum
is 1 MiB; serialized request maximum 64 KiB. No progress/upload/binary/streaming
API, user headers, cross-origin access or redirects are exposed.

Each mounted logical application root owns a request scope inherited by its
components/slots/layout descendants. Deduplication keys include canonical URL,
response schema and transport options; each consumer holds an independent lease.
Last-consumer release aborts fetch. Generation tokens block completions after
reload/cancel/destruction. Root destruction clears cache and subscriptions.
Opt-in memory cache is bounded to 64 entries, TTL <=60 seconds; reload bypasses
it. It is never process-global, browser persistent or shared across root mounts.
Mutation success clears the root cache, advances its epoch and reloads only the
explicitly named local reads. The epoch prevents coalescing with pre-mutation
requests. Other already-rendered resources are not implicitly reloaded. Change
user/session by destroying and remounting the root; no login/logout API or token
storage is implemented.

Only same-origin absolute paths are accepted, including queries; no fragment,
backslash, control/space or protocol-relative URL. Production fetch requires
HTTPS; plain HTTP is permitted on localhost/127.0.0.1/::1. Credentials are fixed
to same-origin, HTTP cache disabled, redirect:error, referrerPolicy:same-origin.
Generated CSP adds connect-src 'self'. Writes require a server-injected
`<meta name="nexo-csrf-token" content="...">`, sent as X-CSRF-Token. The compiler
never generates a secret/token. A backend must verify token AND Origin, enforce
authentication/resource authorization, secure HttpOnly/SameSite cookies (Secure
in production), validate input and apply rate controls. CSRF and client validation
are layers, not proof of server authorization. A missing token fails before
network. Keep cache TTL zero for sensitive data and remount on identity change.

This aligns with relevant OWASP ASVS 5.0 areas for validation, browser security,
session management, access control and secure communications; it is not an ASVS
certification. Shield's bounded loopback ingress is not promoted to a persistent
production server. The bundled Python API is loopback development only.

### Commands, diagnostics and verification

```sh
python -m nexo web check --json examples/web/data/app.nexo
python -m nexo web build examples/web/data/app.nexo -o dist
python examples/web/data/server.py --directory dist
python -m pytest tests/test_web_data.py
```

NW3001: JSON contracts/fields; NW3002: resource declaration/options/readiness;
NW3003: API origin/URL boundary. Runtime failures appear through resource.error.
For a Security error check same-origin URL, HTTPS and server CSRF metadata. For
Validation check exact JSON fields, Content-Type and safe integer range. For
Timeout adjust bounded timeout and backend latency; explicit Retry is optional.
Server.py serves session-isolated in-memory products and verifies Host, Origin,
CSRF and request schema; it deliberately has no production TLS/auth/observability.
M4 development tools are specified below. Production security certification and M6 SSR remain future work.


<a id="m4"></a>
## M4 amendment — development and security tooling (edition 4)

M4 adds tools without new application syntax. Python remains the only browser
compiler; Rust/browser support and the existing D175 WASI target are unchanged.

### Canonical development workflow

```sh
python -m nexo web new my-app
cd my-app
npm ci
python -m nexo web dev app.nexo
# Open the printed http://127.0.0.1:5173/ URL.
python -m nexo web check --json app.nexo
python -m nexo web audit --security --json app.nexo
python -m nexo web build app.nexo --sourcemap -o dist
python -m nexo web preview dist
# Editor server: python -m nexo web lsp
```

new creates an offline Nexo Counter application, nexo.toml, package.json, exact
package-lock.json and .gitignore in a new directory; existing destinations are
never overwritten. npm ci is explicit, never automatically invoked. Node 22.12
or supported 24+ and pinned Vite 8.3.5 are optional development requirements.
For repository examples install with `npm ci --prefix nexo/web_tools`, then run
`python -m nexo web dev examples/web/counter.nexo`. The runner resolves Vite from
the project's installation, falling back to the installed Nexo tooling directory;
an incompatible Vite version fails instead of silently selecting a global tool.
Build/check/audit/LSP/preview do not require Node or Vite. No React/TypeScript DSL
or new production dependency is introduced.

Available ports: dev defaults 5173, preview 4173; --port accepts 0..65535, with 0
selecting an ephemeral loopback port. Ports in use fail; no automatic public
binding or port fallback. SIGINT/SIGTERM close the development listener and owned
compiler subprocess group on the tested Linux platform. No automatic open-browser,
remote bind, backend proxy or deployment command is supplied. The M3 full API
demo continues to use its separate loopback server; Vite preview/dev do not
invent backend endpoints or CSRF/session metadata.

### HMR and trust boundary

Vite is a development transport and watcher; a compiler-only JSON bridge invokes
Python without a shell, with a 30-second compilation timeout and bounded stdout.
Source changes under the project root trigger compilation; imported modules/CSS
and nexo.toml are included. node_modules/.git/dist are excluded. Changes serialize
through a short debounce. There is no frontend bundler/compiler implemented in
Vite and no automatic execution of application server code.

When emitted JS and line mappings are unchanged, CSS updates replace an external
stylesheet without remounting components. Scope identities depend on Nexo logic
and declaration identity, excluding style text. Logic edits remount the complete
root: destroy old routes/providers/effects/listeners/requests before creating the
new app. Local/context state resets. This conservative module HMR does not claim
state migration, per-component replacement, request replay or zero lost state.
Compile failures retain the last good UI and display an accessible text diagnostic;
a corrected edit clears it. Runtime mount failure displays an error, with cleanup
performed by the generated ownership boundaries. Initial compilation must succeed
before the dev listener starts. Manual reload may be required after a server
restart; CSP can block Vite's optional blob-worker reconnection fallback.

The listener is 127.0.0.1 only. HTTP Host and Origin and WebSocket upgrade origin
are checked. Vite runs against an empty temporary root with no public directory,
no project config and no .env loading. Filesystem allowlisting permits only that
root and Vite's own installed resources. Project source is deliberately available
through compiler maps, not arbitrary HTTP file paths. There is no CORS or API
proxy. Development CSP keeps external scripts/styles and an explicit same-origin
WebSocket endpoint, without unsafe-inline or unsafe-eval. Overlays use text nodes;
no CSS injection dependency is needed. Loopback is not per-user authentication:
other local processes can still access the dev site. Dev/preview are not
production hosting, OS isolation or Shield profiles.

### Maps, diagnostics and editor tools

build --sourcemap adds app.js.map and sourceMappingURL. Without the flag, builds
emit no map and remove a stale app.js.map in the output directory. Map v3 uses
project-relative nexo://project names, embedded sourcesContent and line-level
statement/action/view mappings; runtime/helper code is unmapped. It is deterministic
across relocated copies of a project. Columns default to the source line start;
expression-level stepping, CSS maps, minified-bundle composition and mapped runtime
error diagnostics are not implemented. Maps disclose source: publish them only
when intended. Vite consumes the same compiler map rather than inventing one.

check --json retains schema_version 1 and adds column, end_column and snippet to
code/file/line/severity/message. These spans identify a source statement, not a
precise token range. Imported-module errors refer to the actual dependency.
NW4001 reports missing/incompatible development tools; NW4002 invalid tooling
boundaries, paths or ports. CLI argument errors remain status 2.

web lsp is a separate stdio JSON-RPC server reusing the protocol infrastructure.
Point a frontend editor configuration at `python -m nexo web lsp`; the core
`nexo lsp` and Rust LSP preserve existing behavior. Browser LSP supports compile
diagnostics for unsaved entry text, UTF-16 positions, document outline, local
hover/definition and browser-only keyword/member completions. Saved project
imports are checked; imported diagnostics carry relatedInformation. Unsaved
import overlays, cross-file navigation, frontend formatting/code actions and
automatic file-mode selection are deferred. No misleading core formatter is
applied to style/view syntax.

### Frontend audit and Shield

web audit --security [--json] file first applies browser syntax/type/capability
checks, then flags literal credential-like names/arguments/URL queries (NW4101)
and opt-in cache requiring sensitivity/identity review (NW4102). It returns 1
for findings and 0 for none. Audit findings redact snippets and never repeat a
credential value. These are bounded heuristics with false positives/negatives,
not entropy scanning, dataflow analysis, dependency auditing or a proof that no
secret exists. The report explicitly marks backend authorization as Not assessed.

The machine-readable boundary report links actual Shield profiles: memory is
backend in-process dispatch; loopback has at most 8 connections, a 3-second accept
window and a 6-second hard payload lifetime. Neither is a browser execution
sandbox or persistent production listener. Use the existing `nexo security`
workflow separately for supported backend workloads. Browser audit never runs
backend code, grants permissions or claims end-to-end authorization/CSRF/rate
validation. ASVS 5.0 browser/session/authorization/validation guidance remains
relevant; production assessment and deployment are M5 work.

### Validation and remaining scope

`tests/test_web_tooling.py` runs compiler maps/scaffolding/audit/LSP tests and real
Vite/Chromium HMR and boundary checks. Set NEXO_WEB_REQUIRE_VITE=1 and
NEXO_WEB_REQUIRE_BROWSER=1 in CI so unavailable tools fail rather than silently
skip. Existing M1–M3, website and AI Guide checks remain required. The locked
optional tooling and sources participate in the AI Guide's content hash.

M5 adds release optimization, measured fixture quality and a local deployment example
below. Accessibility/security certification and production qualification remain unimplemented. M6 advanced rendering requires
separate RFCs. No SSR, hydration or production-ready claim follows from HMR.

<a id="m5"></a>
## M5 amendment — release artifacts and measured quality (edition 5)

This experimental milestone qualifies the included fixtures and static deployment
example, not every possible application or production environment. M6 is unchanged.

### Optimized application builds

```sh
npm ci --prefix nexo/web_tools
python -m nexo web audit --security --accessibility --json examples/web/production/app.nexo
python -m nexo web build examples/web/production/app.nexo --release -o dist
python -m nexo web preview dist
```

--release uses the same optional pinned Vite 8.3.5 toolchain with Rolldown and
Lightning CSS. Node 22.12/24+ is required for release/dev; ordinary build, check,
audit, LSP and preview remain Node-free. No project config, env or public directory
is loaded. The compiled application entry is minified/tree-shaken; CSS is minified
with native @scope preserved. HTML references hashed assets using relative URLs
and SHA-384 SRI. This is client rendering on static hosting, not SSG/SSR, and the
optimized entry is an application bundle rather than reusable library exports.
The existing WASI build --target web and Rust/compiler behavior are unchanged.

nexo-manifest.json schema_version 1 records format=nexo-web-release, entry,
compiler/Vite versions, source-map/observation flags, compiler-input/optimizer
fingerprints and per-file SHA-256/SHA-384 integrity, UTF-8 size and estimated gzip
size (mtime=0). It excludes timestamps and absolute build locations. Assets are
deterministic for a fixed compiler/toolchain across relocated project copies.
This manifest detects modification/staleness, not an authenticated signature or
proof that the build machine/dependencies are trustworthy.

Build into a dedicated directory. A complete staging directory is prepared before
replacing an existing owned build; unrelated files, symlink outputs or modified
release manifests fail closed. Rebuilding release drops old owned assets/maps;
switching to an ordinary build also removes the old release tree/manifest. A
failed optimization leaves prior output intact. Deployment must retain previous
hashed assets during a rolling switch or roll back the complete build/image;
local replacement is not an atomic multi-file CDN deployment protocol.

--sourcemap composes original Nexo line mappings through optimization and embeds
sourcesContent. Source identity is retained through Vite normalization, and mapped
entry filenames have a separate fingerprint prefix to prevent immutable-cache/SRI
collisions with unmapped assets. Runtime helpers remain partially/unmapped. Maps
disclose source and are opt-in; no CSS maps or expression-level stepping is implied.
Preview validates the manifest and serves only its listed assets on loopback.
NW5001 identifies optimizer/output/manifest failures; tooling availability retains
NW4001. Argument errors still return status 2.

### Accessibility and quality gates

web audit --accessibility [--security] [--json] file uses the compiler's semantic
model to flag missing statically available control names (NW5101), positive
tabindex (NW5102) and aria-hidden interactive controls (NW5103). Labels can use
text, aria_label, associated label/for, or aria_labelledby. Dynamic/component
output cannot be proven by these bounded checks. Findings return 1, none 0;
absence is never a WCAG certification. Contrast, rendered IDs, focus order,
conditional states and assistive technology still require browser/manual review.

Pinned axe-core 4.14.0 is a development/test tool only. Tests load it as a same-origin
external script while CSP stays enabled. The production example is checked under
WCAG 2 A/AA, 2.1 AA, 2.2 AA and best-practice tags, plus explicit keyboard/status/
focus/scoped-style tests. Scan scope and incomplete results are recorded rather
than claiming all states/devices are covered. The shipped site/app does not load axe.

```sh
python scripts/web/measure.py --output docs/web/quality-report.json --check
python docs/web/build.py --release
python docs/web/build.py --check
```

The measurement script builds development/release fixtures, records raw/per-file
gzip sizes, and runs a Counter in Chromium with one warmup and five fresh-context
samples per mode. It measures NavigationTiming DCL, synchronous 100-click duration,
request counts, text mutations and node identity. HTTP is loopback/uncompressed;
OS cache and shared-host load are uncontrolled, with no added CPU throttling.
Timing is local observational evidence, not a universal SLO or framework comparison.
Versioned byte/DOM/axe budgets in docs/web/quality/budgets.json fail CI; no timing
threshold is imposed on shared runners. Published reports retain input/optimizer/
measurement fingerprints; ordinary docs checks validate freshness without silently
rerunning nondeterministic timings. Explicit remeasurement is required after drift.

The Web production quality workflow installs locked tools, enforces source audits,
runs real release/browser/accessibility/container tests, evaluates budgets and
uploads a machine-readable report. A local pass does not claim a GitHub Actions
run or required branch protection has been configured remotely.

### Deployment and operations

examples/web/production supplies Nexo source, digest-pinned Dockerfile, Nginx
config and instructions. The tested static container runs UID 101, read-only root,
no capabilities/no-new-privileges and writable bounded tmpfs. Headers include CSP,
nosniff, referrer/permissions policy and HTML-no-cache/hashed-asset-immutable cache
rules plus gzip/Vary negotiation. It rejects dotfiles and writes, applies a sample rate limit, and exposes
/healthz for static-server liveness. JSON access logs contain only status, bytes,
duration and method; critical native diagnostics still need protected/redacted
retention. This is not a backend health/business-flow probe or production load test.

The internal listener is HTTP on 8080. Production needs a trusted TLS edge,
HTTPS redirects/HSTS, image/dependency scanning/updates, application-specific SLOs,
log retention, rollback tests and a separately verified backend for APIs, sessions,
CSRF and resource permissions. No cloud deployment or end-to-end TLS qualification
is claimed. Static sites usually do not justify Kubernetes; choose topology from
budget/reliability/service needs. Shield profiles remain backend-only and bounded;
they are not promoted to persistent production ingress by this example.

### Optional local observation

build --observe (ordinary or release) opts into `nexo:web` CustomEvents on the
browser global. Default builds emit none. The fixed detail allowlist is:
schema_version=1; kind=mount/request/navigation; phase=started/loading/ready/error/
cancelled/disposed; request operation=load/mutation; sanitized error code/status;
rounded finite duration_ms when available. Unknown codes become Unknown. No URLs,
route params, component/user names, payloads, messages, stack traces or tokens are
included. There is no external destination, network upload, persistence or global
unhandled-error capture. Consumers own their listener lifecycle and must explicitly
add any authorized monitoring integration. Client events can be forged and are
not security/billing evidence. Monitoring failures cannot change application flow.

M5 measured evidence and remaining limits are in docs/web/validation-m5.md.
M6 SSR/hydration/SSG/PWA/WASM interoperability/realtime still require separate RFCs.

## Post-M5 maintenance (D195)

These changes close compiler/tooling debt, not M6. Existing unkeyed loops,
parameterless actions, scalar state, CLI commands and D175 WASI remain compatible.

### Typed immutable state and keyed lists

State also accepts the existing finite JSON record/list contracts, including
`state products: list[Product] = [Product(id: 1, name: "One")]` and typed empty
lists. Constructors/assignments are checked; records/lists are frozen and replaced
as a whole. There are no in-place collection/field writes or general browser async
functions. Props/routes remain scalar. Record types are declared/imported normally.

```nexo
for product in products key product.id:
    li:
        ProductRow(label: product.name)
```

`key` is contextual to a view loop, not a new core keyword. The key expression
must depend on that item alone and have type int or str. Keys are unique within
that region; duplicate keys throw before changing the last rendered list.
Same-key rows retain DOM ranges, owners, listeners, descendant local state and
focus while immutable item values update their bindings/props. Order follows the
new list. Removed rows destroy descendants/subscriptions/listeners; re-added keys
create fresh state. No index-key inference. An unkeyed legacy loop still replaces
its whole region. Request loading/error branches retain their existing semantics:
if a branch disappears, its rows are destroyed even when the loop has keys.

### Typed events

```nexo
state query: str = ""
fn edit(event: WebEvent):
    query = event.value
view:
    input(value: query, on_input: edit, aria_label: "Filter")
```

UI event actions accept zero arguments or exactly one `name: WebEvent` parameter;
context actions remain parameterless. `WebEvent` is a reserved built-in browser
type with read-only `value: str` and `checked: bool`. Each invocation gets a frozen
snapshot of the native listener's currentTarget, captured synchronously before
batching. Non-value/checkbox controls yield empty string/false. No raw DOM target,
HTML, JS methods, native event retention or automatic two-way binding is exposed.
Use explicit assignment actions. `value` writes the native value property;
`checked` and `disabled` write boolean properties. Input is untrusted; text remains
escaped, and backend validation/authorization is still required.

### Static markup and maps

The compiler extracts maximal fully constant native-element subtrees, including
escaped text/attributes, into cached templates cloned per instance. Only compiled
escaped constants reach the template parser; dynamic values use text nodes and
typed attributes. Components, slots, structural regions and event-bearing nodes
are not treated as static fragments. There is no independently importable view,
raw HTML API, runtime evaluation or virtual DOM.

`--sourcemap` emits JS and CSS v3 line maps with original source content. CSS maps
include inline style lines and recursively expanded local CSS imports; generated
scope wrappers are unmapped. Multiple original fragments on one generated line
use its first mapped fragment; exact columns/expressions are not promised. Release
CSS maps compose through pinned Lightning CSS and mapped assets have distinct
names/SRI. Normal builds omit/remove both maps. Embedded sources are public when
maps are published; no server files or secrets belong in browser source.

### Compatible component HMR

Dev uses a shared runtime module and a per-application instance registry. Scope
IDs use module/declaration identity within a deterministic application namespace.
Dev retains its initial namespace across edits; separate builds derive theirs
from project sources, preventing unrelated bundle styles from sharing IDs. CSS-only updates
preserve DOM/state. A compatible component logic/view edit replaces only mounted
instances of changed outermost component boundaries and preserves each boundary's
own typed local state; unaffected components, inputs and layouts retain identity.
Descendants, providers and requests owned by a replaced boundary are disposed and
recreated; old listeners are inert. This is not arbitrary state migration.

Changes to state/prop/provider/injection shape, declarations, entry, context logic,
pages, layout or routing metadata safely remount the application and reset state.
Compile errors retain the last good UI. HMR registry/factories/state capture are
dev-only and absent from ordinary/release assets. Runtime ownership/batching is
shared across hot module versions; applications never share local state registries.

Examples/tests: `examples/web/keyed/app.nexo`, `tests/test_web_debt.py` and existing
M1–M5 suites. Browser LSP recognizes keys/events; frontend formatting, exact source
columns, Rust browser compilation and M6 features remain outside this amendment.


## M6 experimental completion (D201–D205)

The following checklist supersedes historical M6-future limitations in earlier amendments. Every M6 capability has an implemented bounded experimental interface and an executable browser/reference example. This does not certify arbitrary applications for production.

| Capability | Contract | Evidence |
| --- | --- | --- |
| Static HTML and routed SSG | RFC-0002/0003 | test_web_static.py, test_web_ssg.py |
| Request-time SSR | RFC-0004/0008 | test_web_ssr.py, async isolation/timeout tests |
| Hydration, conditions, lists and context | RFC-0005/0006/0007 | hydration, conditional and completion suites |
| Async server state transfer | RFC-0008 | typed loader and wire tests |
| Public offline PWA | RFC-0009 | real service-worker offline route/404 test |
| WASM interoperability | RFC-0010 | actual Worker calls, integrity/import rejection, timeout |
| Realtime subscriptions | RFC-0011 | SSE replay, typed updates and teardown |

Run `python examples/web/m6/server.py` for the integrated SSR/async/context/list/WASM/SSE example. See its README for the separate public static PWA build. `docs/web/validation-m6.md` records verification and environment limits. Rust browser compilation, production authentication/TLS hosting, streaming SSR, WASI imports and durable message delivery remain outside these contracts.
