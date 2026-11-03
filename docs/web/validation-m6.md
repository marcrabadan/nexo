# Experimental M6 completion validation — 2026-11-03

Scope: RFC-0002 through RFC-0011. All M6 capability buckets have bounded experimental implementations: static HTML, routed SSG, request-time SSR, typed structural hydration, async initial server data, public offline PWA, import-free numeric WASM and typed SSE subscriptions. This report supersedes earlier slices' exclusions for these features. It does not qualify arbitrary production applications.

## Reproducible integrated example

```sh
python examples/web/m6/server.py
```

Open http://127.0.0.1:4174/. The executable test checks trusted async initial state, DOM attachment, keyed row local-state retention, shared context, conditional disposal/remounting, actual WASM result 42 and actual SSE updates together. The example README supplies a separate static PWA build with finite routes. SSR is never placed in that public offline cache.

## Regression command

```sh
NEXO_WEB_REQUIRE_BROWSER=1 .venv/bin/python -m pytest \
 tests/test_web_m6_completion.py tests/test_web_conditional_hydration.py \
 tests/test_web_hydration.py tests/test_web_ssr.py tests/test_web_static.py \
 tests/test_web_ssg.py tests/test_web_frontend.py tests/test_web_tooling.py \
 tests/test_web_composition.py tests/test_web_data.py tests/test_web_debt.py \
 tests/test_web_production.py tests/test_web_qualification.py -q
```

**211 passed, 40 skipped in 190.07 seconds.** Chromium 154.0.8037.57 with Playwright 1.56.0 runs the required browser checks. Optional Vite/Docker/production qualification environments are unavailable; their skips are not successful qualification.

The completion suite has 11 tests. Verified behavior includes:

- Keyed row identity/local-state preservation, data updates, removal cleanup, duplicate-key rejection and empty-list transitions. Unkeyed lists recreate row owners; nested keyed markers claim correctly.
- Shared layout/page/child/projected-slot context with fresh server state and cleanup after conditional removal.
- Concurrent async loader request isolation, signed int64 transfer, exact nested type validation, generic failures and timeouts. Invalid typed host patches leave previous state intact.
- Actual import-free WASM numeric execution in a Worker, SHA256 rejection, imported-module rejection, infinite-loop termination and root teardown.
- Typed bounded SSE publication/replay, live browser updates, stale/closed lifetime handling and root-owned cleanup.
- Explicit public PWA precache; actual offline known-route navigation and offline 404; private API paths remain uncached. Plain static builds omit worker registration.
- Real integrated trusted host with async initial state, keyed/context interaction, WASM and SSE.

A timing-dependent test-helper failure was traced to Playwright wait_for_function evaluating a string under the application's restrictive CSP. Assertions now use locator waits. No unsafe-eval permission was added to fix tests.

## Additional browser coverage and limits

Playwright 1.62.0 was isolated in a qualification environment. Firefox 153 failed to launch: this sandbox prevents writing /proc/self/uid_map and the software compositor failed framebuffer mapping. Firefox is unverified.

WebKit 26.5 ran with locally extracted Debian runtime libraries, without changing system libraries or application security policy. The distribution's host-library precheck was bypassed after checking/loading those local dependencies. Its non-offline selection passed **10 tests, 1 deselected**, including integrated async/list/context/WASM/SSE behavior. The offline PWA test failed on navigation with “WebKit encountered an internal error.” Consequently offline PWA is verified in Chromium only; WebKit offline compatibility remains unqualified. This is an explicit platform limitation, not a successful cross-engine claim.

## Documentation, guide and distribution

RFC-0007–0011 define ownership, typed transfer, loader, PWA, WASM and SSE bounds separately. RFC-0001 edition 12 contains the completed milestone checklist; decisions D201–D205 record the contracts. Public website templates and generated pages describe the final interfaces. Historical reports/RFC slice boundaries remain historical.

AI guidance includes a complete compiler-checked M6 project with its required trusted host and boot module. Python guide tests: **23 passed**; direct Node interface tests: **8 passed**; browser guide QA: **48 variants passed**; generated guide check: **93 files checked**. The developer compact offline prompt measures **1,954 cl100k tokens**. Example verification, generated-artifact checks, tokenizer measurements, Python guide tests, Node interface tests and browser guide QA are run independently. Compact prompts stay below 2,000 measured cl100k tokens. Original M1–M5 codegen/runtime/release implementation files are unchanged.

A fresh wheel was built from a scratch source copy: `nexo-0.0.1-py3-none-any.whl`, SHA256 `253f919bfc6ce38f2affcae45b586fe28ad0ddfd620b088e6a7d4c0a0d4ef28c`. All new Python/JS resources matched source bytes. Outside the repository, the extracted wheel successfully rendered typed async SSR/hydration with WASM/subscription assets and built public static PWA output. No deployment, publication or production credential configuration is implied.

Production authentication/authorization, HTTPS/session operations, workload limits, accessibility assessment, durable distributed delivery, streaming SSR and Rust browser compilation are outside these experimental contracts. Browser load/mutation resource declarations belong to the existing client-rendered mode; SSR uses an explicit trusted Python loader.

## Subsequent stabilization evidence

The first stabilization phase supersedes the WebKit offline qualification limitation above: the corrected test verifies a controlling service worker, stops the real HTTP origin and navigates cached pages/404 successfully. Chromium and WebKit each pass 122 strict M6 cases without skips, including teardown-error hardening. The earlier offline-emulation failure remains historical evidence, not a demonstrated application defect. See [stabilization validation](stability-validation.md) and [release acceptance criteria](stability.md). Firefox and hosted CI remain pending; the release is still experimental.
