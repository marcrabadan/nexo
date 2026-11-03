# M6d fixed-tree hydration validation — 2026-11-02

Scope: RFC-0005's opt-in scalar fixed-tree hydration through the SSR/WSGI adapter.
This report does not qualify structural hydration, arbitrary server-state transfer,
async data, production hosting or multiple browser engines.

## Compiler and browser regressions

```sh
NEXO_WEB_REQUIRE_BROWSER=1 .venv/bin/python -m pytest \
  tests/test_web_hydration.py tests/test_web_ssr.py tests/test_web_static.py \
  tests/test_web_ssg.py tests/test_web_frontend.py tests/test_web_tooling.py \
  tests/test_web_composition.py -q
```

**147 passed, 2 skipped** in 146.33 seconds. The two skips require optional Vite
integration dependencies, which are not installed in this environment. The
hydration suite contains **19 tests**. Browser execution used Chromium
154.0.8037.57 with Playwright 1.56.0 over owned loopback HTTP servers.

Verified hydration behavior:

- Opt-in HTML, escaped typed route props, exact int64 decimal transfer, build-
  specific JavaScript assets and GET/HEAD asset content type/length semantics.
- Complete element identity and focused-control preservation, with no child/text
  mutations during successful attachment to the nonempty reference tree.
- Pre-hydration text/checkbox property edits survive attachment. Subsequent typed
  WebEvent input/change actions update Nexo state and safe text bindings.
- Scalar actions, child prop updates and lexical slot subscriptions remain reactive.
  Missing empty text nodes are supplied only after validation succeeds.
- Modified text, extra nodes, unexpected attributes, old build identity, malformed
  props, out-of-range int64 props and inherited page names fail closed. No handlers
  attach and the owned SSR markup remains unchanged.
- Repeated hydration returns one handle without duplicate listeners. Destroy is
  idempotent, removes behavior and preserves DOM. An old disposed handle cannot
  unregister a newer active root. Pristine disposed DOM can hydrate again.
- Prototype-named route props work without object-prototype side effects.
- Automatic boot, full-document navigation and readable/native-link fallback with
  application JavaScript disabled work in the real browser.
- Unsupported conditions, navigation actions and request resources fail at startup.
  Ordinary SSR still rejects actions without the explicit hydration option.
- Patched file reads and socket creation are not invoked by hydrated HTML or cached
  module responses after startup. Host hydration metadata is added only to the outer
  app host, without rewriting a nested source element that also uses id="app".

The existing SSR/static/SSG/frontend/tooling/composition cases remain in the passing
run. Node syntax checking of the emitted module, Python compilation,
`git diff --check`, website generation/freshness and canonical-source alignment
checks pass. The ordinary client emitter/runtime, M5 release optimizer and measured
demo artifacts remain unchanged.

## Package check

A wheel was built from a task-local copy of the final package sources. Its
`nexo/web_hydrate.js` and `nexo/web_hydrate.py` bytes match the source files exactly.
The extracted wheel, imported outside the repository, successfully constructs a
hydration-enabled SSRApplication, renders a counter response and returns the cached
module through response(). No browser/package parity claim beyond these checks is
implied. No generated wheel was published or installed into a production service.

## Documentation and AI guidance

Public overview/deployment pages and the optional Web topic describe the explicit
hydration mode, state recomputation, input preservation, mismatch and lifecycle
bounds. A complete hydration project payload is generated. Previous snapshots
remain intact; the new snapshot records the final source revision.

- AI example verification rerun and recorded against current Python sources.
- AI Guide measured generation/freshness: **91 files checked**.
- Python AI Guide suites: **23 passed**.
- Node AI Guide runner: **8 passed**.
- Chromium AI Guide browser QA: **48 variants passed**, including copy fallback,
  version selection, responsive widths, failed fetches and no-JavaScript fallback.
- Maximum compact offline prompt: **1978 measured tokens**, within the 2000-token
  budget, using tiktoken 0.12.0 / cl100k_base and the verified task-local cache.

## Remaining bounds

Fixed scalar trees only: no conditions/lists, context, structured state, resource
requests, navigation actions, arbitrary server-state snapshots or async loaders.
Input edits are preserved as DOM properties until explicit assignment; earlier
clicks are not replayed. Mismatch has no automatic remount or state migration.
Native links perform full document navigation. The mode is not a static-build or
optimized-release option. Origin-root paths, WSGI/local-runner limitations and
backend identity exclusions remain those of RFC-0004. No public deployment,
production qualification, cross-engine support or accessibility certification is
claimed by this functional evidence.
