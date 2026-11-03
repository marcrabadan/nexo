# RFC-0002: M6a — experimental static component HTML

Status: implemented experimental first M6 slice. This RFC authorizes only the
bounded static renderer described here. [RFC-0003](0003-web-routed-ssg.md) extends
it with finite routed SSG; remaining M6 features are future work.

## Decision

`python -m nexo web build examples/web/static/app.nexo --static -o dist`
produces `index.html` and `app.css`. The complete initial component tree is in
HTML and is readable with JavaScript disabled. No application JavaScript, browser
runtime, hydration markers, event listeners, network requests or inline scripts
are emitted. CSP disables scripts and connections. This is build-time rendering
of one standalone entry, not request-time SSR or routed multi-page generation.

The existing browser frontend parses and type-checks the complete program first.
The first public root component is the entry, with the same default-property
requirements as client builds. A separate allowlisted AST evaluator computes
scalar expressions with checked signed 64-bit arithmetic, lowercase boolean text
and ordinary Nexo interpolation. It cannot call functions, execute core modules,
read environment variables or access the network. Source/module/CSS loading
retains existing project-root restrictions. No Node or browser is needed to build.

Supported: scalar default props, scalar initial state (reading props), native
safe elements/attributes, project component imports, named scalar props, slots
with caller lexical scope, conditionals, and existing scoped external CSS.
Component ownership wrappers and scope hashes match the browser compiler. Text
and attributes are HTML escaped; booleans use native boolean attributes. A leading
newline in `pre` is preserved through HTML parsing. Expanded nesting that would
trigger paragraph, heading, list-item, button or form repair is rejected.

All loaded declarations and branches are checked, including unreachable ones.
Actions, event handlers, requests/mutations, context, pages/layouts, route links,
list/record state and loops fail `NW6001`; no interactive app is silently frozen.
Static arithmetic overflow fails `NW6002`. Unbound actions are also rejected so
adding interactivity cannot accidentally leave a static deployment unchanged.

`--static` cannot combine with `--release`, `--observe` or `--sourcemap` in this
slice. Ordinary client build behavior is unchanged. Static output uses the
existing staged build-directory writer: compilation finishes before replacement,
unrelated files are rejected, and switching from a client build removes its old
JavaScript/maps. Use a dedicated output directory. The static output is not a
hashed/SRI release bundle and does not establish production qualification.

## Validation and remaining work

`python -m pytest tests/test_web_static.py` covers deterministic output, imported
composition/slots, escaping, full int64 precision and overflow, unsupported feature
rejection before writes, CLI options, preservation of unrelated output, and build
mode switching. The real-browser case explicitly disables JavaScript; unavailable
Playwright/browser dependencies must be reported as skipped.

RFC-0003 specifies routed SSG, including enumeration, metadata, asset paths and
route URLs. Future RFCs must specify request-time SSR, hydration ownership and
mismatch recovery, async server data and serialization, PWA, WASM interop and
subscriptions. Rust browser-driver parity remains outside this slice. Existing
M1–M5 production qualification does not qualify this renderer.

RFC-0004 now provides bounded scalar request-time WSGI rendering; earlier
future-SSR statements here describe this RFC's original milestone boundary.
Hydration and async server data remain future work.
