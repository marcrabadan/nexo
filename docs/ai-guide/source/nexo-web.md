# Nexo Web — RFC-0001 / M1–M3 experimental

For browser UI, use the Python CLI: `python -m nexo web check --json app.nexo`;
`python -m nexo web build app.nexo -o dist`; serve dist over local HTTP.
The Rust browser driver is unavailable. `build --target web` remains D175 WASI.
No React/JSX/hooks, independently imported view fragments or server modules.

M1 remains supported: `component Name:`, scalar `state name: int|str|bool = value`,
parameterless `fn action():`, `view:`, `style:`. Four-space indentation, uppercase
UI/context names, no core reserved words as local names. Expressions: scalar
literals/reads/interpolation, checked int + - *, unary minus, str +, bool not.
Int64 uses BigInt; overflow throws without rolling back earlier assignments.
Events on_click/on_input/on_change name a local or injected context action.
No event payload or two-way input. M3 adds conditions and snapshot lists; no keyed reconciliation.

## M2 canonical composition

`component Badge(label: str, start: int = 0):` declares read-only typed scalar
props. Call `Badge(label: title)`; only named props, no unknown/missing required
props or implicit coercion. Defaults are constant. Local state may initialize
from props once; later prop updates preserve local state. The CLI auto-mounts the
first public component when no pages exist; that entry needs defaults for all
props. Reusable imported components may have required props.

`Card(heading: title):` with indented children projects content into its one
`slot()`. Slots retain caller lexical bindings/context/CSS and have a receiver-owned
lifetime. Nested components/slots/listeners/subscriptions are destroyed together.
Use `use ui.widgets` then `widgets.Card(...)`, or an explicit `as` alias. D124
project-root lookup, qualified access, pub privacy, once-per-file loading and
cycle rejection apply. M2 supports project files, not package/server imports.

`context Visits:` owns scalar state and assignment actions. `provide Visits`
creates a fresh provider on a component/layout/page. `visits = inject(Visits)`
resolves the nearest provider. Read `visits.count`; call `visits.increment()` in
an action, or use `on_click: visits.increment`. Consumers cannot assign shared
fields. No process-global singleton. Each root/router has isolated providers;
future SSR must also allocate fresh request-owned context.

## M2 pages and layouts

`page User(id: int):` with `path = "/users/{id}"` defines a screen. Path braces
are typed parameter placeholders, not runtime interpolation. Parameters must
match exactly; int/str/bool conversion is strict. `layout = Shell` selects a
layout; a layout can select a parent with the same spelling. Every layout has
one slot, no props. Shared layout prefixes and their providers persist; leaving
pages/layouts are destroyed. Changing route parameters remounts the page.

Routing uses URL fragments (`#/users/7`) for static hosting. Native anchors
`a("User", href: "/users/7")` and action `navigate("/users/7")` accept only local
route strings. No external URLs, query API, wildcard routes or history-server
rewrite requirement. Static routes precede parameters; equally specific overlaps
are compile errors. Title/description metadata can interpolate page parameters.
`not_found = true` declares a parameterless fallback page; `error = true` declares
a parameterless, layout-free mount/navigation error page. Event-action errors
are not caught by that page. Navigation focuses the new main/heading.

Bindings batch across local/context owners, run deterministically and skip equal
rendered values. Text uses text nodes. CSS uses native @scope, standard selectors,
custom properties, media/container/supports queries. Local `@import "./card.css";`
inside style is expanded and scoped; imports stay inside the project root and
cycles are rejected. No remote resources (url/image-set/image/src), global
at-rules, escapes or minifier. Native @scope browser support is required.

## M3 typed data

Declare field-only `type Product:` records with required int/str/bool, nested
record/list fields; export with pub and import through existing project modules.
No optional/union/float/default/recursive contracts. JSON fields match exactly;
wire int range is ±9007199254740991 (BigInt internally). Use str for larger IDs.

`load products = api.get("/api/products", as: list[Product])` starts on mount and
reacts to its scalar URL inputs. `mutation create = api.post("/api/products",
body: NewProduct(name: title), as: Product, invalidate: [products])` runs only
through create.run (event reference) or create.run() in an action. PUT/PATCH use
the same typed body; DELETE body is optional. No general await or server http.

Read products.data only in `if/elif products.ready:`. Render loading with
role: "status"; after `elif products.error != none:` read error.kind/message/status
and offer products.reload. Mutations expose loading/success/error; success also
narrows data. `if/elif/else` conditions require bool; `for product in products.data:`
iterates an immutable snapshot. Replacing a list destroys child state/listeners;
no keyed diff. Conditions and regions preserve semantic DOM parentage. Compound
conditions do not narrow data. Keep request error handling explicit; core T! and
page error boundaries do not catch these resource values.

Options: timeout_ms 1..120000 (default 10000), GET retries 0..3 (default 0),
GET cache_ms 0..60000 (default 0), GET dedupe bool (default true). Bounds are
literals. GET retries network/429/5xx with bounded delays, never validation/4xx.
Writes never retry/cache; repeated run during a write is ignored. Requests accept
one positional URL, named options and required as: type. Bodies are validated
scalars/record constructors/nonempty homogeneous lists. No arbitrary headers.

products.reload() bypasses cache; products.cancel()/create.cancel() cancel one
consumer. Cancellation cannot undo a committed write. Last-consumer disposal
aborts transport; generation checks suppress stale results. Each mounted root
owns deduplication/cache (64 entries max), never global/persistent. Mutation
success clears that cache and reloads only named local loads. Destroy/remount
on session/user changes; use cache_ms: 0 for sensitive data. Timeouts include
retries/body reads; request limit 64 KiB, response 1 MiB, nesting 32/list 10000.
Kinds: Http, Network, Timeout, Cancelled, Validation, Limit, Security.

Use same-origin absolute paths only; HTTPS in production, loopback HTTP for dev.
Credentials are same-origin, redirects disabled, generated CSP connect-src 'self'.
Writes require server-provided meta nexo-csrf-token; server must verify Origin and
X-CSRF-Token, secure cookies, authentication and resource permissions. Browser
checks never authorize. No token is generated or persisted by the compiler.
Run `python -m nexo web build examples/web/data/app.nexo -o dist` then
`python examples/web/data/server.py --directory dist`; loopback demo only, not a
production/Shield ingress. The public static demo supports reads only.
Run tests/test_web_data.py with real Chromium. NW3001 contracts; NW3002
requests/readiness; NW3003 URL boundary. No invented backend or browser passes.

## Workflow and security

Understand requirements; ask essential missing questions; choose page/layout/
component/context boundaries; define typed backend contracts; implement supported
state/view/style; apply security/a11y; compile; test; inspect JSON diagnostics;
correct and verify. Keep SOLID/simple boundaries. Use kind/k3d only when justified,
and choose hosting by budget/reliability/operations. Never fabricate results.
NW2001 modules; NW2002 props/composition; NW2003 routes; NW2004 context;
NW2005 slot ownership. Missing providers and cycles fail before browser execution.

Bundles are public. Keep secrets, authentication, resource authorization, CSRF,
sessions and rate controls on supported backends; browser visibility grants no
permission. Assess actual Shield profiles: bounded loopback ingress is not a
persistent production server. Production needs HTTPS/header CSP. No ASVS 5.0
certification is claimed. Native controls still need meaningful labels/contrast.

Proposed only: keyed reconciliation, Vite/HMR, source maps, SSR/hydration/SSG. Do not generate those as runnable code. Retrieve only the
relevant project from @@WEB_EXAMPLES@@; it includes every required source file,
compiler hashes and associated test commands. The entries below are generated
from real compiler-checked sources; actual browser results come from their tests.
