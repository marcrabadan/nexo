# M6b routed static generation validation — 2026-11-02

Scope: RFC-0003's experimental finite routed SSG and the shared RFC-0002 renderer.
This report records the current checks following the earlier M6a validation.
It does not qualify request-time SSR, hydration, cross-engine support or production.

## Compiler and browser regressions

```sh
NEXO_WEB_REQUIRE_BROWSER=1 .venv/bin/python -m pytest \
  tests/test_web_static.py tests/test_web_ssg.py tests/test_web_frontend.py \
  tests/test_web_tooling.py tests/test_web_composition.py -q
```

**103 passed, 2 skipped** in 35.02 seconds. The two skips require optional Vite
integration dependencies, which are not installed in this environment. There are
49 tests in the static/SSG suites, including Chromium execution. Browser evidence:
Chromium 154.0.8037.57 with Playwright 1.56.0, served through loopback HTTP.

Static browser checks explicitly disable application JavaScript. They verify
standalone rendering, scalar int64 precision, checkbox state, scoped CSS, deep
links, title/description metadata, normal link navigation and back navigation.
A separate paired browser test compares nested-layout element trees and scope
ownership against the normal client build, including caller/receiver CSS isolation
for projected slots. The latter corrects an M6a slot-wrapper omission.

Compiler/output checks cover literal and enumerated routes, typed int/bool/string
matching, static precedence, missing/invalid/duplicate path rejection, portable
filesystem boundaries, case collisions, metadata escaping, ungenerated links,
relocatable deterministic output, integrity validation, removal of obsolete pages,
build-mode switching, preservation of modified/unrelated/symlink files and rollback
after an injected final rename failure. The checked-in M5 release demo is used to
verify transitions from and back to real optimized artifacts without an optimizer.
Preview checks include directory redirects, nested assets and fallback HTTP 404.

Additional data/debt compiler regression selection:
`tests/test_web_data.py tests/test_web_debt.py -k 'not real_browser'`:
**16 passed, 10 skipped, 24 deselected**. The skips require optional tooling. No
additional browser data-request or HMR qualification is implied by that selection.

Both supplied examples build successfully through the CLI. Python compilation,
`git diff --check`, public website generation/freshness and the existing website/
AI canonical-source alignment check pass. The M5 optimizer implementation and its
measured demo artifacts remain unchanged.

## Documentation and AI guidance

Public overview/deployment templates and their generated pages now describe static
generation. The AI Web topic and optional project payloads include standalone and
routed static examples with the required routes JSON. Existing versioned snapshots
are retained; one new snapshot is generated for the final source revision.

- AI example verification rerun and recorded against the current Python sources.
- AI Guide generation and measured freshness: **89 generated files checked**.
- `tests/test_ai_guide.py tests/test_ai_guide_advanced.py`: **23 passed**.
- Node AI Guide runner: **8 passed**.
- Real Chromium AI Guide QA: **48 variants passed**, including copy fallback,
  responsive viewports, version selection, failed fetches and no-JavaScript mode.
- Maximum compact offline prompt: **1983 measured tokens**, within the 2000-token
  progressive-disclosure budget, using tiktoken 0.12.0 / cl100k_base.

The tokenizer vocabulary was supplied from a public tiktoken-rs copy and verified
against tiktoken's pinned SHA-256 before populating a task-local cache. No token
counts were estimated and no TLS verification was disabled.

## Remaining bounds

Origin-root hosting with portable ASCII slug segments only. Actions/events,
requests/mutations, context, structured state and loops remain unsupported in
static mode. The static build cannot use release optimization, observation or
source maps. Production hosts must configure directory indexes and the generated
404 document/status. No deployment was performed. Request-time SSR, hydration,
async server data, PWA, WASM interop and subscriptions remain future work.
