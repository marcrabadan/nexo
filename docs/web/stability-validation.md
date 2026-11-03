# Web stabilization: first phase — 2026-11-03

Status: experimental; these results begin stabilization and do not remove that label. [Acceptance checklist](stability.md). [Machine-readable local evidence](stability-evidence.json).

## Changes and evidence

The new strict runner (`scripts/web/stability.py`) executes all six M6 suites. A nonzero pytest result, failed/error/skipped case, empty/malformed/missing JUnit report or source change during execution blocks success. Reports contain exact source fingerprints, actual browser versions from JUnit, Python/Playwright versions, counts, duration and dependency-precheck bypass status. Seven gate unit cases cover successful evidence and rejection of skips, empty runs, errors and malformed reports.

`.github/workflows/web-stability.yml` adds independent Chromium/Firefox/WebKit jobs with Playwright 1.62.0 and installed system dependencies. It archives evidence even when tests fail. It has been added locally; no hosted workflow execution or branch protection change is claimed.

The PWA test now requires a controlling worker and stops/closes the actual HTTP server before navigating a cached route and an unknown path. Cached content remains available, and the unknown path returns the cached 404 with status 404. Private API paths remain absent from the cache. Both engines pass this real origin-outage test. The earlier WebKit internal error happened with Playwright's offline emulation; it did not establish a product defect. Browser automation offline emulation, Safari/iOS device behavior and real origin availability are distinct qualifications.

Hydration teardown now attempts every host cleanup and every owned teardown even when a callback throws. It clears ownership and the root registry before reporting collected failures as AggregateError. The new browser case verifies that callbacks on both sides of a throwing callback run, old listeners become inert, the disposed handle refuses state access, repeated destroy is harmless and unchanged markup can be hydrated with a fresh owner. The shared M1–M5 runtime is unchanged.

## Executed results

| Check | Result |
| --- | --- |
| Strict M6, Chromium 154.0.8037.57 / Playwright 1.56.0 | **122 passed, zero skipped**, 133.62 s |
| Strict M6, WebKit 26.5 / Playwright 1.62.0 | **122 passed, zero skipped**, 125.17 s |
| Gate evidence unit tests | **7 passed** |
| Required Vite/browser tooling, release and maintenance selection | **45 passed, 1 deselected**, zero skipped |
| Required Docker/browser/Vite Nginx selection | **3 passed, 21 deselected**, zero skipped |
| npm audit of pinned Web tooling | **0 vulnerabilities reported** |
| AI Guide Python / Node / browser | **23 / 8 / 48 passed** |
| Generated AI Guide and website | **93 guide artifacts checked**, website fresh |
| Built wheel, extracted outside checkout | resource equality and SSR/async/hydration/adapters/PWA smoke passed |

The container selection covers the existing static deployment fixture: read-only non-root Nginx, a trusted disposable local TLS certificate, TLS versions/header behavior, bounded burst/rate-limit recovery and restart. It is not production SSR/async/SSE hosting qualification. These runs make previously optional local release/container checks concrete; they do not replace a fresh image-vulnerability scan or sustained workload test.

Wheel SHA256: `571dbea6a0e52f71828b195d35cc810a379cad0aa5846b0b6f75ae394ebd9211`. New hydration resources matched final source bytes. No production deployment was performed.

WebKit uses locally extracted Debian runtime libraries; its host dependency precheck was bypassed because those libraries are outside the system linker cache. Tests actually executed, and the bypass is recorded in machine evidence. Application CSP/TLS were not weakened. Hosted CI installs system dependencies normally.

Firefox 153 was retried with the normal browser sandbox. Launch fails before tests: `/proc/self/uid_map` is read-only and the software compositor cannot map its framebuffer. The required test reports an error rather than skipping. Firefox remains unqualified locally; no sandbox weakening was used to manufacture a passing result.

## Remaining release blockers

The acceptance checklist still requires the hosted three-engine matrix, reviewed stable API/versioning/support contracts, PWA update/failed-install regressions, independent boundary/security review, M6 manual accessibility assessment, production SSR hosting limits/disconnect/shutdown validation, sustained load/resource evidence and operational upgrade/rollback qualification. Workload-specific budgets must be selected for the intended hosting model. Repository administrators also need to make the CI checks required if branch protection is desired.
