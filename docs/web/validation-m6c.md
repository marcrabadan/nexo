# M6c request-time rendering validation — 2026-11-02

Scope: RFC-0004's experimental scalar WSGI renderer and local reference runner.
This does not qualify hydration, async server data, production serving or multiple
browser engines. Earlier M6a/M6b reports retain their historical milestone bounds.

## Compiler, isolation and HTTP checks

```sh
NEXO_WEB_REQUIRE_BROWSER=1 .venv/bin/python -m pytest \
  tests/test_web_ssr.py tests/test_web_static.py tests/test_web_ssg.py \
  tests/test_web_frontend.py tests/test_web_tooling.py \
  tests/test_web_composition.py -q
```

**128 passed, 2 skipped** in 56.48 seconds. The two skips require optional Vite
integration dependencies, which are unavailable in this environment. The SSR suite
contains 25 tests. Browser execution used Chromium 154.0.8037.57 with Playwright
1.56.0 over loopback HTTP, including application JavaScript disabled.

Verified behavior:

- On-demand typed routes without finite enumeration, dynamic local links, fresh
  scalar initial state, layout composition and escaped title/description metadata.
- Eight concurrent workers rendering 80 distinct routes through one compiled
  application, with isolated page content/metadata and deterministic repeat output.
- Source changes after startup do not affect the compiled instance. Patched file
  reads and socket creation fail if attempted during direct HTML/CSS responses;
  those responses pass without performing either operation.
- GET/HEAD byte lengths and empty HEAD bodies, canonical redirects, external CSS,
  405/Allow semantics, typed 404s and generic malformed-path 400s without reflection.
- WSGI status/header/body integration and preservation of the single-decode path
  contract, including rejection of percent literals and nonempty query strings.
- Safe 500 pages after request-time integer overflow, generic 500 after a failing
  error page, and successful subsequent requests through the same application.
- Startup rejection of unsupported actions/context, component-only programs and
  unsupported literal portions of dynamic route patterns.
- Real HTTP method/status behavior, loopback Host/Origin rejection and CLI startup
  with an automatically selected port. Tests stop their owned servers afterward.
- Chromium deep links to unenumerated member URLs, native next/back navigation,
  scoped CSS, metadata and 404 documents, with no script resource requests.

Static/SSG and ordinary browser rendering suites remain part of the passing run.
Python compilation, `git diff --check`, website generation/freshness and the final
website/AI canonical-source alignment check pass. The measured M5 release module
and demo artifacts remain unchanged. No public listener or deployment was started.

## Documentation and AI guidance

The public overview/deployment guide and optional Web topic now describe bounded
request-time rendering. A complete SSR project payload accompanies the source,
local command and WSGI integration example. Previous versioned guide snapshots are
retained; a new snapshot records the current source revision.

- AI example verification rerun and recorded against current Python sources.
- AI Guide measured generation/freshness: **90 files checked**.
- Python AI Guide suites: **23 passed**.
- Node AI Guide runner: **8 passed**.
- Chromium AI Guide QA: **48 variants passed**, including version selection,
  copy fallback, responsive viewports, failed fetches and no-JavaScript fallback.
- Maximum compact offline prompt: **1987 measured tokens**, below its 2000-token
  budget, using tiktoken 0.12.0 / cl100k_base and the existing task-local verified
  vocabulary cache.

## Remaining limits

Scalar, origin-root pages with portable ASCII paths only. No actions/events,
requests/mutations, context, structured state, async server data or hydration.
The synchronous WSGI adapter consumes no bodies, cookies or identity headers.
The local runner is single-request-at-a-time, loopback-only and has no hot reload.
An integrating host must provide production HTTP service, limits, authentication,
trusted forwarding, TLS and lifecycle management separately. Shield ingress and
backend authorization behavior are unchanged. No production or cross-engine
qualification is implied by these functional results.
