# Nexo Web M5 delivery — 2026-11-01

M5 adds an experimental release/quality/operations pipeline on top of
[M4](validation-m4.md). Included fixtures and the static container were verified
locally, with subsequent remote CI validation recorded below. M4 and M5 are
published on `main`; the documentation and static demo are deployed to GitHub
Pages. This is not a blanket production-readiness, WCAG/ASVS certification or
production application TLS/cloud qualification. M6 was not started.

## Architecture and files

RFC-0001 edition 5 and D194 keep Nexo parsing/types/DOM semantics in the Python
compiler and reuse pinned Vite 8.3.5/Rolldown/Lightning CSS for release optimization.
There is no React wrapper, TypeScript application DSL, new bundler or additional
Python production dependency. Core Rust/WASI and server HTTP/Shield behavior were
not changed.

New implementation: `nexo/web_release.py` (optimizer protocol, manifest/integrity,
safe output replacement), `nexo/web_accessibility.py` (bounded source checks),
`nexo/web_observe.js` (opt-in allowlisted local events),
`nexo/web_tools/release.mjs` (trusted Vite build), and `nexo/web_tools/vite.mjs`
(shared pinned resolver). Updated frontend/codegen, data/router observation hooks,
audit CLI, preview and package data integrate them. Optional axe-core 4.14.0 is
locked as a development dependency; it is not shipped in application bundles.

New quality/deployment sources: `tests/test_web_production.py`,
`scripts/web/measure.py`, `docs/web/quality/budgets.json`,
`examples/web/production/{app.nexo,nexo.toml,Dockerfile,nginx.conf,README.md}`,
and `.github/workflows/web-production.yml`.
The playground generator now excludes optional web_tools/node_modules content;
its ZIP was rebuilt. The wheel includes required runtime/tooling files and excludes
node_modules. M4 and M5 were committed together in
[8c0c7b4](https://github.com/marcrabadan/nexo/commit/8c0c7b42901aed6b6857843cf2af3f3a3103945b)
and pushed to `main`.

## Implemented commands and behavior

```sh
npm ci --prefix nexo/web_tools
python -m nexo web audit --security --accessibility --json examples/web/production/app.nexo
python -m nexo web build examples/web/production/app.nexo --release -o dist
python -m nexo web preview dist
python scripts/web/measure.py --output /tmp/nexo-web-quality.json --check
```

Open the preview's loopback URL. --release requires optional pinned Node/Vite;
ordinary builds/check/audit/LSP/preview remain Node-free. It minifies/tree-shakes
the application entry, minifies scoped CSS, emits hashed assets/SRI HTML and
nexo-manifest.json. This is client-rendered static hosting, not SSG/SSR or a
reusable optimized library target.

The manifest records schema/toolchain/input/optimizer fingerprints, flags and
per-file SHA-256/SHA-384, byte and estimated gzip sizes. Relocated builds produce
identical assets for the fixed toolchain. Preview validates the manifest before
serving only listed files. Dedicated staging/replacement refuses unrelated or
modified output, removes old owned assets/maps, and supports release-to-normal
build conversion without retaining a source-bearing release tree. A failed
optimizer leaves prior output intact. The manifest is not an authenticated
signature or guarantee that dependencies/build hosts are trustworthy.

--sourcemap composes original Nexo line mappings through optimization, retaining
source identities/content and using a separate mapped-entry fingerprint to avoid
immutable-cache/SRI conflicts. It embeds source; publish deliberately. CSS maps,
precise expression stepping and full runtime-error translation remain unavailable.
NW5001 reports optimizer/manifest/output failures; NW4001 tooling failures.

--accessibility adds source findings for control names (NW5101), positive tabindex
(NW5102) and hidden interactive controls (NW5103). It supports label/for and
aria_label/aria_labelledby. Heuristics can miss dynamic states and produce warnings
that need review; no clean-source-audit security/accessibility proof is implied.

--observe opts into local nexo:web events for mount/request/navigation phases,
request operation, sanitized error code/status and rounded finite duration.
The detail allowlist excludes URLs, route params, component/user names, messages,
stacks, payloads and tokens. There is no collector, persistence, external upload
or unhandled-error capture; default builds emit none. Listener lifecycle and any
explicit authorized monitoring integration belong to the operator. Events are
forgeable and unsuitable as authorization/billing evidence.

## Website and AI Guide

Website canonical source `docs/web/page.tmpl` now has M5 workflow, measured-size
table, accessibility/deployment/operations limits and links to the real optimized
example. `docs/web/build.py --release` generated `docs/web-release-demo/` and
source downloads under `docs/web/production/`; ordinary generation/check verifies
stored measured/release artifacts without invoking Node. `docs/web.html` and
M1–M3 demos were regenerated through that pipeline.

Published URLs:
[production workflow](https://marcrabadan.github.io/nexo/web.html#production) and
[optimized static demo](https://marcrabadan.github.io/nexo/web-release-demo/).
The successful GitHub Pages deployment is linked in the remote validation record.
No axe/Vite runtime dependency was added to the public documentation page.

AI Guide canonical sources/catalog/examples/template/README now teach M1–M5.
web-production is compiler-checked with its Docker/Nginx/operations support files.
The combined frontend payload remains compatible; individual versioned
web-examples/<id>.json payloads enable targeted retrieval and are token-measured
with the existing pinned tokenizer. All 48 prompt variants/profiles and
compact/full/offline/URL modes remain. Current generation emits **86 fresh files**,
including versioned evidence/context, project payloads, docs/ai-guide.html,
docs/llms.txt and docs/llms-full.txt. No hand-edited generated context is used.

Current revision:
`f7a070ec6dbd4b04b4db1b221038e10dc97b5c047ef15f7923c78b9f17620a00`.

| Token context | M4 | M5 |
| --- | ---: | ---: |
| Developer compact offline | 1,964 | 1,956 |
| Maximum compact offline | 1,998 | 1,990 |
| Full evidence | 17,461 | 17,465 |
| Optional Web topic | 2,850 | 3,530 |
| Combined Web projects | 3,618 | 5,934 |

Individual payload counts: Counter 460, composition 1,223, data 2,035,
production/deployment 2,389. Retrieve only the relevant project; unrelated tasks
do not include the complete Web topic or combined projects.

## Measured performance and accessibility

Canonical results: [quality-report.json](quality-report.json). They identify
compiler input, optimizer and measurement source, environment, method, samples
and budgets. CI gates fixture JS gzip <=16 KiB, CSS gzip <=4 KiB, at most 100
mutations for 100 Counter clicks, preserved node identity and zero axe violations.
These are fixture regression gates, not limits imposed on every user application.

| Fixture | Ordinary JS bytes | Release JS bytes | Ordinary estimated gzip | Release estimated gzip |
| --- | ---: | ---: | ---: | ---: |
| Counter | 6,469 | 2,670 | 2,409 | 1,259 |
| Composition | 23,703 | 13,403 | 5,382 | 3,953 |
| Data | 21,050 | 12,658 | 6,582 | 4,550 |
| Production example | 6,533 | 2,718 | 2,443 | 1,290 |

Sizes sum application JS assets; gzip is estimated per-file compression, not a
claim about benchmark transfer encoding. Counter estimated gzip falls about
47.7%. For both modes, five samples recorded 100 text mutations for 100 clicks
with the same text/heading nodes. JS/CSS request count changes from three to two.
The production Nginx test separately verifies actual gzip/Vary negotiation and
decoded content size/SRI behavior.

Local Counter samples: ordinary DCL median 80.9 ms (p95 95.1), release 34.4 ms
(p95 75.1); synchronous 100-click medians 1.2/1.1 ms. The report contains every
sample. There is one warmup per mode, fresh browser contexts, loopback HTTP
without compression, uncontrolled OS cache/shared-host load and no added CPU
throttle. These are noisy local observations, not a universal speedup, SLO,
load/capacity qualification or framework comparison. No timing threshold gates CI.

Pinned axe-core 4.14.0 found **zero violations, zero incomplete results and 22
applicable passing rules** in the initial rendered production fixture under the
configured WCAG A/AA/2.1/2.2/best-practice tags. Separate tests cover keyboard
activation/focus/status updates and style scoping. Web and AI Guide pages also
pass the same automated rules in light/dark initial states. This does not cover
all application states, assistive technologies, devices or browsers and is not a
WCAG conformance certification.

## Executed validation

The following results are the original local M5 validation record. Later remote
CI results are recorded separately below.

- Focused compiler/browser/website/AI Guide suite including tests/test_web_frontend.py, test_web_composition.py, test_web_data.py, test_web_tooling.py, test_web_production.py, test_ai_guide.py, test_ai_guide_advanced.py and test_libraries_page.py: **138 passed**, with browser/Vite/Docker requirements enabled. Final run: 80.64 seconds, one unrelated third-party pytest deprecation warning.
- Lexer/parser/check JSON/core LSP regression selection: **209 passed, 16 deselected**. Native LSP parity cases were deliberately excluded; no fresh full native/WASI parity result is claimed.
- Existing AI Guide backend verifier: **5 examples checked and executed**, exact output.
- Node guide tests: **8 passed** directly; node --test passed its file-level check.
- docs/web/build.py --check: passed. AI Guide --measure --check: **86 files fresh**.
- Existing AI Guide Chromium QA: **48 variants passed**, including version selection, filtering, readiness, clipboard success/fallback, keyboard, theme/menu, fetch failure and no-JavaScript cases.
- Public-directory review: responsive overflow checks at 360/768/1440 in light/dark themes, existing M2/M3 demos and the actual optimized demo pass without page errors.
- Release tests cover relocated deterministic outputs, maps/original sources, SRI tampering, source/manifest integrity, safe replacement, stale-map removal, CSS scope preservation, typed mutations/CSRF/invalidation, routing/context/focus, opt-in observation privacy and unknown-code sanitization.
- Docker/Nginx: image built from the pinned digest; nginx -t passes. Real Chromium loads and updates the release. Tests inspect UID 101/read-only root, actual NoNewPrivs=1 and CapEff=0, health, gzip/Vary, MIME/CSP/cache headers, denied writes/dotfiles, no-store missing assets and access-log field minimization. Owned containers/images are cleaned up.
- npm audit: **0 reported vulnerabilities** in the locked optional tooling graph on 2026-11-01. No container-image vulnerability scan/certification is claimed.
- Wheel built and resource inclusion verified; no node_modules. Playground ZIP excludes web_tools/node_modules. Playground bundle/link checks: **2 passed, 3 deselected**.
- git diff --check: passed.

Initial deployment tests exposed read-only Buildx state and restrictive source
file modes. Buildx state now uses an owned temporary path without changing Docker
credentials; the Dockerfile explicitly normalizes read permissions before UID 101
startup. These were fixed and the real deployment checks rerun. A source-map
composition issue was fixed by preserving original source identity through Vite's
normalization and stripping internal tags from final maps.

## Remote validation and publication — 2026-11-02

GitHub Actions reports all four workflows successful for
[ca66956](https://github.com/marcrabadan/nexo/commit/ca66956cf867c5c46cbdf0786f4ea92542620926),
which includes the M4/M5 delivery and subsequent CI corrections:

- [Shield security regressions](https://github.com/marcrabadan/nexo/actions/runs/41400327442): four isolated Python shards, Rust build/tests, compiler conformance, interpreter/compiler differential checks and Docker security/isolation checks passed.
- [Web production quality](https://github.com/marcrabadan/nexo/actions/runs/41400325181): source audits, release/browser/accessibility/deployment tests and measured asset/DOM budgets passed.
- [AI Guide](https://github.com/marcrabadan/nexo/actions/runs/41400325361): example verification, measured generation freshness, Python/Node guide tests, frontend tests and browser regression passed.
- [GitHub Pages](https://github.com/marcrabadan/nexo/actions/runs/41400324800): documentation/static-site build and deployment passed.

The original serial Shield job exhausted its shared 45-minute budget after a
successful Python suite. [d6f1628](https://github.com/marcrabadan/nexo/commit/d6f1628)
separated the gates; [6c773a3](https://github.com/marcrabadan/nexo/commit/6c773a3)
partitioned Python tests across four isolated runners without dropping coverage.
The sharded run exposed a timing-sensitive agent deadline test.
[ca66956](https://github.com/marcrabadan/nexo/commit/ca66956)
replaced its fixed provider delay with a response-release event and allowed a
one-second request setup window, retaining timeout, single-request and no-tool-effect
assertions. Local validation passed all 90 lifecycle tests and three further
repetitions of the corrected test on both backends before the successful remote run.

These native CI results supplement the earlier deliberately limited local native
selection. They do not establish a fresh WASI suite result, cross-browser support,
production load capacity or security/accessibility certification. No PR was
created; the commits were pushed directly to `main`.

## Deployment, security, remaining work and next step

For subsequent local TLS, burst/recovery and patched-image scan evidence see
[production-example qualification, 2026-11-02](production-qualification.md).
The historical checks above are preserved; that report records the later scope
and remaining application-specific gaps separately.

The static container pins
`nginxinc/nginx-unprivileged@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03`.
It runs non-root/read-only with bounded tmpfs, no capabilities and no-new-privileges.
Sample rate limits are not a measured capacity guarantee. Health is static-server
liveness only. Access logs omit IP/URL/query/cookie/auth data; critical native
stderr still needs protected/redacted retention. Use workload-specific SLOs,
monitoring, rollback and privacy policies before real deployment.

Internal HTTP on 8080 must sit behind a trusted production TLS edge with HTTPS
redirect/HSTS. Beyond the published documentation/static demo on GitHub Pages,
no production application deployment, end-to-end TLS qualification, cloud topology, real backend
auth/session/resource-permission integration, production load test or
full security audit was performed. Existing Shield memory and bounded loopback
profiles retain their actual limits and are not production listeners or browser
sandboxes. Source audits, SRI and hashes do not authorize backend requests or make
a compromised host trustworthy. Relevant OWASP ASVS 5.0 controls remain a review
framework, not a certification result.

The original M5 gaps in keyed reconciliation, event snapshots, static extraction,
CSS maps and granular HMR are addressed by the subsequent
[D195 maintenance](technical-debt.md), with immutable JSON state and explicit
compatibility/cleanup semantics. Props/routes remain scalar; general browser
async functions and the Rust browser driver are still unavailable. Broader
device/accessibility coverage and real-application qualification remain pending.

Stop at M5. Before M6, choose a concrete workload and qualification needs; SSR,
hydration, SSG, PWA, WASM interop and realtime subscriptions require separate RFCs.
No M6 work was started. M4/M5 and the CI corrections are committed and pushed to
`main`, with successful remote workflows and documentation deployment as linked
above. Production application qualification remains pending.
