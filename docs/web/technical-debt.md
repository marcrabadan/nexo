# Post-M5 compiler/tooling maintenance (D195)

The five frontend gaps recorded in the original M5 report now have compiler,
runtime/tooling, documentation and executable regression coverage. This is
RFC-0001 edition 6 maintenance, not M6 or a production/security certification.

| Original gap | Implemented behavior | Evidence |
| --- | --- | --- |
| Snapshot-only lists | Explicit item-dependent int/str keys; same-key DOM/owners/state retained, typed immutable record/list state, duplicate-key rejection and removal cleanup | `examples/web/keyed/app.nexo`, `tests/test_web_debt.py` |
| No event data | Frozen `WebEvent.value:str` / `checked:bool`, explicit assignment handlers and native input properties; no raw DOM/event API | Keyed/input browser tests and typed rejection tests |
| No static markup extraction | Maximal constant native subtrees become compiler-escaped cached templates cloned per instance; dynamic input remains text | Static literal/runtime XSS browser checks, independent instances |
| No CSS maps | Opt-in v3 line maps for inline/imported CSS, composed release maps, distinct mapped assets and stale-map removal | Nested CSS import/map/release tests and existing integrity tests |
| Whole-app logic HMR | Compatible changed component boundaries preserve their own local state; unaffected branches retain identity; shared dev runtime, per-app registry and cleanup | Actual Vite/Chromium sibling/state/listener tests, existing recovery/CSP tests |

Full semantics and fallback rules: [RFC amendment](../rfc/0001-nexo-web.md#post-m5-maintenance-d195).
Runnable project: [list/input demo](../web-keyed-demo/) and
[complete source](../web-keyed-demo/source/app.nexo). The same source is registered
in the existing AI Guide catalog as separately retrievable `web-keyed`; no
alternative React/TypeScript syntax or new prompt system is introduced.

## Build and validate

```sh
python -m nexo web check --json examples/web/keyed/app.nexo
python -m nexo web build examples/web/keyed/app.nexo --sourcemap -o /tmp/nexo-keyed
python -m nexo web preview /tmp/nexo-keyed
# Install optional pinned Vite/Playwright before browser/tooling tests.
NEXO_WEB_REQUIRE_BROWSER=1 NEXO_WEB_REQUIRE_VITE=1 \
  python -m pytest tests/test_web_debt.py tests/test_web_tooling.py -q
```

The maintenance suite checks typed failures, legacy strings containing `key`,
independent style namespaces, reordered row DOM/state, duplicate keys, focus,
removed-listener cleanup, escaped static/dynamic markup, typed input/checkbox
updates, original CSS import lines and compatible component refresh. Required
browser flags turn unavailable dependencies into failures rather than simulated
success. Production CI also includes it; new remote results must be checked after
the changes are committed/pushed.

Keyed runtime helpers are emitted only when the program needs them. Parameterless
handlers do not allocate event snapshots. Fully static subtrees have no reactive
subscriptions, and release optimization retains the existing tree-shaking/asset
budgets. The updated [fixture measurements](quality-report.json) are dated local
evidence; no universal speedup, HMR latency or capacity guarantee is inferred.

## Boundaries that remain

- Keys do not retain rows across removal of their surrounding conditional/page.
  Legacy unkeyed loops keep their prior whole-list replacement semantics.
- Compatible HMR retains only the refreshed boundary's own typed state. Its
  descendants/providers/request resources are recreated; ownership/type/context/
  routing changes safely reset the application. No arbitrary state migration.
- JSON state is immutable and assigned as a whole. Props/routes stay scalar;
  event snapshots have only two fields. No DOM methods, collection mutation,
  general browser async functions or automatic two-way binding is exposed.
- Maps embed public source and are line-level. Exact expression columns, frontend
  formatting/code actions and the Rust browser driver remain outside this work.
- Real backend authorization/session/CSRF, cloud deployment, workload SLOs/manual
  accessibility qualification and the local Firefox launch limitation remain in
  the [production qualification report](production-qualification.md).
- SSR, hydration, SSG, PWA, WASM interoperability and realtime subscriptions are
  implemented experimentally by the subsequent RFC-0002–0011 M6 work; this historical maintenance report does not qualify those modes. See [M6 validation](validation-m6.md).

## Recorded validation — 2026-11-02

- Full focused frontend/production/qualification/maintenance/AI Guide/library suite:
  **154 passed, zero skipped, 88.78 seconds**, with required browser/Vite/Docker flags.
- Lexer/parser/check JSON/core LSP selection: **209 passed, 16 native parity cases
  deselected**; no fresh full Rust/WASI run is claimed.
- Linux WebKit release/maintenance regression: **11 passed, 15.47 seconds**.
- AI Guide Node checks and actual Chromium QA of **48 variants** passed; all
  **87 generated files** pass measured freshness checks. Compact profiles remain
  below the existing 2,000-token budget; Web detail and project packs are optional.
- Rebuilt current production image: **zero Trivy findings** with the recorded
  database; scanned assets match the current compiler input fingerprint.
- Current wheel includes keyed/HMR resources, excludes node_modules and builds the
  keyed project successfully after extraction outside the repository.

Native @scope browser compatibility is a feature prerequisite, not a device
certification: [MDN support data](https://github.com/mdn/browser-compat-data/blob/main/css/at-rules/scope.json).
CI pins Playwright 1.62.0 / Firefox 153 because 1.56.0 bundles unsupported Firefox
142; Chromium/WebKit local evidence retains Playwright 1.56.0. The new Firefox
job and all new workflow edits still need a remote run after commit/push.
