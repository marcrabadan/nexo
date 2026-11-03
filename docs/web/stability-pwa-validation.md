# Web stabilization: PWA lifecycle — 2026-11-03

Status: experimental; second stabilization phase. This closes the public-PWA update/failure qualification item for the two locally available browser engines. It does not certify all browsers, Safari/iOS devices, storage eviction behavior or production SSR hosting. [Acceptance checklist](stability.md).

## Defect and resulting behavior

A failed installation used to delete its cache unconditionally. Service-worker code can change while its public asset version remains the same, so a new installing worker and the previous active worker can reference the same cache. An integrity error in that reinstall deleted the live cache, leaving the old worker active but unable to serve its previous offline pages.

The regression reproduced this in Chromium before the fix: three lifecycle cases passed, then the same-generation reinstall case failed because the active cache had disappeared. The installer now records whether its cache already existed before opening it. Failure removes a newly created candidate cache, but preserves a reused cache. Integrity checks still apply to every fetch; a reused public asset version has the same expected resource hashes. Successful activation continues to retire only obsolete caches belonging to this root application.

## Browser lifecycle coverage

`tests/test_web_pwa_lifecycle.py` adds six real browser cases:

- A successful public release update activates the new worker, removes the old owned cache, keeps an unrelated application's cache and serves the new route after the actual origin is stopped.
- A modified resource fails integrity verification; the candidate becomes redundant, its new cache is removed and the last good worker still serves the old offline site.
- A missing resource likewise fails installation without replacing or damaging the last good site.
- A failed same-generation worker reinstallation preserves the cache still used by the active worker.
- Rolling back to the prior public release reinstalls its cache, retires the newer one and serves the restored route offline.
- A failed first installation removes its candidate cache and exposes no controlling worker; correcting the deployment allows a subsequent install and offline navigation.

Tests use an owned loopback HTTP server and a real server shutdown, with generated integrity-bearing workers and normal browser service-worker transitions. They do not mock CacheStorage, worker lifecycle or network integrity. The installed worker's activated/redundant state is awaited explicitly. Application CSP, TLS verification and browser integrity checks remain enabled.

## Qualification gate hardening

The strict runner now includes the lifecycle suite. Before a new run it removes both old JUnit and old JSON reports, so an interrupted rerun cannot leave an old green result behind. It clears inherited PYTEST_ADDOPTS selections so local filters cannot silently reduce the CI scope. Additional gate tests verify removal of stale success evidence, rejection when fresh results are missing, selection isolation and blocking if source fingerprints change during a nominally passing test run.

Nine gate unit tests pass. The targeted WebKit lifecycle/gate selection passed 15 cases without skips. Chromium's five update/rollback cases and the initial-failure recovery case also passed; the full strict gate subsequently passed as follows.

| Engine | Result |
| --- | --- |
| Chromium 154.0.8037.57 / Playwright 1.56.0 | **128 passed, zero skipped**, 254.59 s |
| WebKit 26.5 / Playwright 1.62.0 | **128 passed, zero skipped**, 227.21 s |

[Machine-readable evidence](stability-pwa-evidence.json) records source fingerprints, unchanged-source verification, browser versions and exact commands. WebKit uses the same locally extracted runtime libraries and recorded host-precheck bypass as the first phase. Hosted CI installs system dependencies normally. Application CSP and integrity checks were not relaxed.

## Distribution and documentation

A fresh wheel was built from final sources, SHA256 `c98690e20e54ff9a37bc2ef09ff2f1f7b0c8276ee3d45952d64195047d36394e`. Extracted resources matched source bytes. Outside the checkout, the installed package generated a worker containing the cache-preservation fix and rendered async SSR/hydration with explicit WASM/subscription adapters.

RFC-0009 documents preservation of an existing cache on failed reinstall; the acceptance checklist and optional AI Web guidance describe the updated contract. Guide verification passed 23 Python tests, 8 Node tests and 48 browser interface variants. Generated guide checking covers 93 files; all compact offline prompt variants remain below 2,000 measured cl100k tokens (maximum 1,986).

## Remaining work

Firefox cannot launch in this sandbox, as recorded in the first-phase report; it was not reclassified as passing here. The hosted three-engine workflow still needs to execute. Stable API/versioning review, independent security review, M6 manual accessibility assessment, production SSR request/concurrency/disconnect/shutdown limits and sustained workload/rollout validation remain release blockers. Public PWA rollback evidence does not establish production SSR operational rollback.
