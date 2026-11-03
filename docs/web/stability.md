# Nexo Web: acceptance criteria for a stable release

Status: stabilization in progress; Nexo Web remains experimental. This is a release checklist, not a statement of completed qualification.

The implemented M6 feature set is described by RFC-0002–0011. Removing the experimental label requires evidence for the supported interfaces and deployment model, a compatibility commitment, and a release decision. Adding features is not required to start stabilization.

## Release blockers and evidence

| Area | Acceptance criterion | Current evidence / remaining work |
| --- | --- | --- |
| Feature contracts | Review syntax, typing, loader/hydration lifecycle, error handling, PWA update policy, WASM and SSE limits; publish supported and excluded behavior | RFC-0002–0011 exist; compatibility review and stable API approval pending |
| Browser support | Required CI succeeds without skips in Chromium, Firefox and WebKit; record actual tested versions and native CSS @scope requirement | Strict M6 matrix added; local Firefox launch blocked by sandbox; hosted CI execution pending |
| Public offline PWA | Generated pages and 404 work after the real origin is stopped; installation integrity failures and version updates preserve the last good site | Real-origin outage, update/failure, same-version reinstall, rollback and recovery regressions pass in Chromium/WebKit; Firefox/hosted CI still required |
| Ownership and cancellation | Every owner releases listeners, context subscriptions, SSE and Workers, including failed setup/teardown; late delivery cannot mutate disposed UI | Existing ownership tests; teardown exception hardening is part of this phase |
| State and isolation | Exact nested state types, int64, request isolation, malformed wire payloads and failed transitions covered; no identity/data leakage between requests | Existing SSR/hydration/completion suites; independent boundary review pending |
| HTTP hosting | A documented production WSGI deployment defines request/body/header/time/concurrency limits, disconnect handling, trusted forwarding and graceful shutdown | Current runner is a loopback reference, not production hosting; deployment fixture pending |
| Security | Review CSP, integrity, public asset/cache paths, untrusted data and subscription authorization; track findings to resolution | Compiler/runtime defenses and tests exist; independent security review pending |
| Accessibility | Automated checks plus keyboard, focus, screen-reader and contrast assessment on representative SSR/hydrated screens | Earlier client fixture checks exist; M6 manual accessibility review pending |
| Operations | Agreed workload and latency/error/resource budgets; sustained load, cancellation/disconnect, memory retention, restart and rollback checks | Earlier static fixture burst test is insufficient; workload and hosting budgets must be selected |
| Packaging and regression | Install built wheel outside checkout, verify bundled runtimes, run core/Web and optional release/container gates; archive source hashes and results | Wheel smoke and earlier regression evidence exist; recurring M6 distribution gate pending |
| Compatibility and support | Publish versioning/deprecation/migration policy, supported Python/browser/hosting matrix and issue/security reporting path | Stable policy and release-owner approval pending |

A browser engine test on Linux is not Safari/iOS device certification. CI cannot approve production application authentication, authorization or business accessibility by itself. These application/operator responsibilities must be clearly separated from supported framework behavior.

## Strict executable M6 gate

```sh
python -m pip install -e '.[dev]' playwright==1.62.0
python -m playwright install --with-deps chromium
python scripts/web/stability.py --browser chromium --output /tmp/nexo-web-stability/chromium
```

Repeat installation and command for firefox and webkit. Firefox must support native CSS @scope (146 or newer); Playwright 1.62.0 supplies a suitable test engine. The runner uses required browser mode and runs all static, SSG, SSR, hydration, conditional, M6 completion and PWA lifecycle suites. It fails for any failed/error/skipped case, empty or malformed report, or nonzero pytest status. Inherited PYTEST_ADDOPTS selections are cleared and previous report/JUnit files are removed before a run, so filters or interrupted reruns cannot preserve stale green evidence. JUnit XML, raw log and a machine-readable report are retained, including Python/Playwright versions and source fingerprints. Local bypass of the browser dependency precheck is recorded and is not equivalent to a stock CI host.

`.github/workflows/web-stability.yml` installs browser system dependencies and runs this gate independently for all three engines. The existing `web-production.yml` continues to cover Vite release/container/quality checks. Repository administrators must configure these checks as required in branch protection; adding workflow files does not establish that remote policy or prove a hosted run passed.

## Offline evidence correction

The earlier WebKit failure was observed with Playwright's `context.set_offline(True)`. A service worker must instead be tested against a real origin outage as well: activation alone is insufficient, so the test verifies an active controlling worker and then stops/closes the HTTP listener. This exercises actual cache-backed document navigation and network-failure fallback without relying on browser automation's offline emulation. This is not an application CSP relaxation or a fallback that silently goes online.

Local results and remaining environment limitations are recorded separately in `stability-validation.md`. Historical M6 completion results remain in `validation-m6.md`.

## PWA stabilization follow-up

The second phase closes public-PWA lifecycle qualification for the two locally available engines and fixes deletion of a live cache during a failed same-version worker reinstall. [PWA validation and remaining blockers](stability-pwa-validation.md). This does not complete the third-engine matrix or qualify production SSR operations.

## Deployed Kubernetes E2E

`tests/e2e/web/README.md` supplies a URL-driven Playwright runner, a local-cluster public Gunicorn fixture and an artifact-collecting Job helper. These exercise an existing M6 deployment without starting an in-runner server. Local container verification is separate from an actual Kubernetes/Ingress run; the latter requires an available cluster, reachable hostname and trusted HTTPS. This does not satisfy production SSR hosting/operations blockers by itself.
