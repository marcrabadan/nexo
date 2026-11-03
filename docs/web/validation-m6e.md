# M6e conditional hydration validation — 2026-11-03

Scope: RFC-0006's scalar conditional hydration, branch ownership and live range
protection. This does not qualify lists/keyed hydration, arbitrary server state,
async loaders, production serving or additional browser engines.

## Compiler and browser regressions

```sh
NEXO_WEB_REQUIRE_BROWSER=1 .venv/bin/python -m pytest \
  tests/test_web_conditional_hydration.py tests/test_web_hydration.py \
  tests/test_web_ssr.py tests/test_web_static.py tests/test_web_ssg.py \
  tests/test_web_frontend.py tests/test_web_tooling.py \
  tests/test_web_composition.py -q
```

**164 passed, 2 skipped** in 96.72 seconds. The two skips require optional Vite
integration dependencies, which are unavailable here. The conditional suite
contains **17 tests**. Browser checks ran in Chromium 154.0.8037.57 through
Playwright 1.56.0 using owned loopback HTTP servers.

Verified behavior:

- Compiler-owned conditional markers appear only in hydration responses. Default
  noninteractive SSR renders the selected content without these new markers.
- Initial conditional hydration retains element identity without child/text DOM
  mutations. Existing stateful children survive parent changes while the selected
  branch index is unchanged, including reactive prop and projected-slot updates.
- Branch switching disposes old handlers/subscriptions, resets newly mounted local
  component state and preserves parent/unrelated DOM. Retained detached nodes stop
  responding both to old clicks and parent-state changes.
- Repeated toggles do not duplicate visible behavior. Root destroy removes behavior
  from the current nested branch hierarchy while preserving its DOM.
- if/elif/else and no-match empty regions transition both into and out of visible
  content. Unrelated input values and focus remain intact.
- Direct nested regions sharing the same DOM parent stay within their own marker
  limits. Inner replacement preserves outer siblings; outer replacement cleans
  old inner subscriptions and reconstructs current content from current props.
- Altered/missing markers, changed selected indices and extra comments fail initial
  hydration atomically, with no listeners installed or owned content replaced.
- Runtime marker deletion/reordering/corruption or an extended range containing an
  unrelated trailing sibling refuses replacement. Outside DOM and old branch
  behavior remain intact rather than being removed by an unbounded range scan.
- A new branch whose state initializer overflows is cleaned up while still detached.
  The old branch remains mounted and usable. The source action is not rolled back,
  and client errors retain existing runtime semantics.
- Conditional SSR remains readable with application JavaScript disabled.

The existing fixed-tree, SSR/static/SSG and ordinary frontend/tooling/composition
suites remain in the passing run. Python compilation, emitted-module syntax,
`git diff --check`, website generation/freshness and canonical-source alignment
checks pass. Ordinary client codegen/runtime, the M5 optimizer and its measured
demo artifacts remain unchanged.

## Package check

A wheel was built from a task-local copy of the final sources. The hydration JS,
emitter and shared renderer bytes in the wheel match the source files exactly.
An extracted-wheel import outside the repository successfully constructs an
SSRApplication, renders the conditional example with both expected marker pairs
and returns the cached owned-region module through response(). The wheel was not
published or deployed. No cross-platform/package/browser parity is claimed beyond
these explicit checks.

## Documentation and AI guidance

Public overview/deployment pages describe conditional hydration and the remaining
list/data limits. The optional Web topic explains comment preservation, state
retention/reset, explicit slot sessions, failed transitions and live ownership
guards. A complete conditional example payload is generated; existing examples and
historical guide snapshots remain available.

- AI example verification rerun and recorded against current Python sources.
- AI Guide measured generation/freshness: **92 files checked**.
- Python AI Guide suites: **23 passed**.
- Node AI Guide runner: **8 passed**.
- Chromium AI Guide QA: **48 variants passed**, including version selection,
  copy fallback, responsive widths, failed fetches and no-JavaScript fallback.
- Maximum compact offline prompt: **1985 measured tokens**, within the 2000-token
  progressive-disclosure budget, using tiktoken 0.12.0 / cl100k_base and the
  existing verified task-local vocabulary cache.

## Remaining bounds

Scalar if/elif/else regions only. Lists/keyed hydration, context, resource requests,
server data loaders, arbitrary state snapshots and navigation actions remain
unsupported. Keep comment boundaries in deployed HTML. No automatic mismatch
remount, event replay, state migration or source-state rollback is added. Destroy
leaves current DOM for its host to manage. Native links still navigate full
HTML documents. WSGI/loopback-runner and backend identity limits remain those of
RFC-0004. No public deployment, production qualification, cross-engine support or
accessibility certification is implied.
