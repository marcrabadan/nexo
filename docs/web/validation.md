# Nexo Web M0–M1 delivery — updated 2026-10-31

For the subsequent implemented M2 milestone, see [M2 delivery](validation-m2.md).
This document preserves the historical M1 validation record.

Experimental M1 implementation and its matching website/AI Guide are ready for
review. Confirmed validation and remaining execution limits are recorded below. No
M2–M6 implementation, production certification or public deployment is claimed.

1. **Architecture.** RFC-0001 and D190 isolate `nexo web` from the core interpreter,
   existing server `nexo.web`, synchronous HTTP and D175 WASI. The Python frontend
   reuses Nexo expression parsing/AST, checks names/types/browser capabilities,
   derives binding dependencies and emits direct DOM JavaScript plus HTML/CSS.
   No JSX, React wrapper, bundler or additional production dependency. The Rust
   core was rebuilt; a Rust browser frontend driver is not implemented.

2. **Files.** New compiler/runtime: `nexo/web_frontend.py`, `nexo/web_runtime.js`;
   Python CLI dispatch and package data: `nexo/cli.py`, `pyproject.toml`. Acceptance
   source: `examples/web/counter.nexo`; tests: `tests/test_web_frontend.py`.
   Design/specification: `docs/rfc/0001-nexo-web.md`, `docs/decisions.md` (D190),
   `docs/especificacion-nexo.md`. Website canonical sources: `docs/web/page.tmpl`,
   `docs/web/build.py`, `docs/assets/web.css`; generated page/demo: `docs/web.html`,
   `docs/web-demo/`. Relevant existing navigation/templates and shared theme/menu/
   highlighting assets were updated. Reference/library pages were regenerated;
   their reference generator now respects NEXO_CC/NEXO_RUNTIME and linker
   availability. `nexo/stdlib/api.json` now lists the already implemented Cancelled
   error type, correcting an exactness regression. The playground ZIP was rebuilt
   through its existing generator after Python/API changes. AI Guide sources and
   outputs are listed below. Its CI workflow now checks fresh Web artifacts and
   runs `tests/test_web_frontend.py` with mandatory browser availability.

3. **Implemented semantics.** `use nexo.web`; `[pub] component Name:`; scalar
   int/str/bool state; parameterless assignment actions; element calls/blocks in
   view; style blocks. Safe text/attribute bindings and on_click/on_input/on_change.
   Each mount owns state, bindings and listeners. Actions batch updates in source
   order; unchanged rendered values produce no DOM write. Destruction is
   idempotent and removes listeners/subscriptions/root. Int arithmetic checks the
   signed 64-bit range. CSS uses native @scope and supports custom properties and
   conditional queries. Reserved core words cannot name state/actions. The CSS
   boundary rejects resource functions including url, image-set, image and src.
   See the RFC for exact grammar, diagnostics, lifecycle and unsupported features.

4. **Working application.** From an installed checkout:

   ```sh
   python -m nexo web check --json examples/web/counter.nexo
   python -m nexo web build examples/web/counter.nexo -o /tmp/nexo-counter
   python -m http.server 8765 --bind 127.0.0.1 --directory /tmp/nexo-counter
   ```

   Open http://127.0.0.1:8765/. Public demo assets mount two independent Counters.
   `web new/dev/preview/audit`, --release, HMR and source maps are unavailable.

5. **Website.** `docs/web.html` explains M1 syntax, architecture, lifecycle, commands,
   scoped CSS, security and proposed milestones. Its semantic HTML/CSS illustration
   needs no new dependency. The real compiled demo is `docs/web-demo/index.html`.
   Intended URLs after normal Pages publication:
   https://marcrabadan.github.io/nexo/web.html and
   https://marcrabadan.github.io/nexo/web-demo/. No publication was performed.
   Existing navigation, typography, theme, focus, copy and mobile menu behavior
   are reused. Web browser checks cover mobile/tablet/desktop and light/dark.
   A wider review reproduced an existing reference-page overflow at 360 px with
   HEAD HTML/CSS (430 px document width); that unrelated baseline issue remains.

6. **AI Guide.** Canonical `source/nexo-web.md` is a conditional detailed topic.
   Compact syntax supplies discovery instead of embedding the entire RFC.
   `source/catalog.json` provides experimental implementation/test evidence;
   `source/examples.json` registers frontend_examples separately from the five
   interpreter fixtures. Generation compiles the Counter and records source/JS/
   CSS hashes; it never infers browser execution from compilation. README/template
   and generator were updated; interpreter verification was refreshed. All 48
   profile/experience/context/access variants, content hashing, snapshots, manifests,
   llms aliases and token measurement remain supported. Current source revision:
   `bb4af04aa8978df30670270b1d3e872f723bcb089c19f3ed0a707bc450d9666f`.
   Topic: `docs/ai-guide/versions/v1-bb4af04aa8978df30670/nexo-web.txt`.
   Earlier snapshots remain intact.

7. **Executed tests and limits.**

   - Latest focused Python run: **51 passed**: Web (27), AI Guide suites (23),
     libraries page (1). Chromium was mandatory, not simulated or skipped.
     Tests cover batching, stable nodes, independent state, escaping, CSS scope,
     keyboard events, cleanup, typed attributes and unchanged-value suppression.
     They also test CSS resource-function and reserved-name diagnostics.
   - AI Guide generation: **81 generated files fresh**, including actual
     `--measure --check` with the pinned tokenizer. Web generator --check and
     git diff --check passed. Five interpreter examples checked/executed and
     matched their recorded outputs.
   - Node command `node --test tests/ai-guide.test.cjs` passed. Direct execution
     also showed **8 passed** individually; this environment's Node 24 reporter
     aggregates the --test invocation as one passing file.
   - Rust `cargo build --offline`: passed using the discovered workspace Rust
     1.99.1 toolchain. Cargo tests by package: diagnostics **4 passed**, compiler
     **32 passed**, runtime **34 passed, 1 ignored**. An initial runtime run had
     two socket-denied failures; both pass with local-network permission.
   - Deadline/MCP follow-up against the rebuilt binary: **7 passed**. The shared
     API catalog is now embedded consistently in the Rust executable.
   - DB/interop/stream/cloud-stream follow-up: **30 passed** in 82.11 seconds.
   - Earlier native conformance: **150/150 passed**; differential: **218/228
     identical, 10 known differences, 0 unexpected**. Those runs used the earlier
     binary before rebuilding its embedded API catalog; no Rust source changed.
   - Earlier native security run: **381 passed, 13 skipped**. Existing AI Guide
     browser QA passed all 48 variants, archived selection, filtering, readiness,
     ADR/cost/copy behavior, six widths, themes, fetch failure and no-JS fallback.
     Pyodide examples/bundle checks previously gave **3 passed, 2 skipped**.
   - Initial broad run: **1604 passed, 280 skipped, 60 failed**; toolchain/cache/
     artifact issues were investigated. A later parallel run gave **2360 passed,
     243 skipped, 16 failed** before stopping: disk exhaustion prevented linking,
     the old binary contained the old API catalog, and a 30ms timing assertion
     failed under load. All affected files were subsequently retested as above.
     Neither historical broad run is presented as passing.
   - Final broad run: **2504 passed, 75 skipped, 0 failed** in 928.50 seconds.
     Verified JUnit: 2579 cases, zero failures/errors. This run used the rebuilt
     compiler, all available toolchains, workspace-backed pytest temporary files
     and monitored cleanup of dead compiler scratch directories. Evidence:
     `/workspace/nexo-validation-20261031/full.xml` and `full.log`.
   - **90 agent-lifecycle cases were excluded from that parallel run** because
     a 30ms timing assertion failed under earlier concurrent load. The complete
     sequential follow-up did not start: its additional-network execution requests
     were interrupted before execution. That final 90-case batch is **unverified**,
     not passed or skipped. The specific deadline test (both engines) and MCP
     follow-up did pass earlier (7 cases above). The combined 2669-case suite
     is therefore not claimed as fully passing.
   - Existing compiler warnings are not new Web changes. End-to-end WASI execution
     remains **unverified** because its runtime assets are absent. Live GitHub
     Actions, public hosting, external providers and deployment are unverified.

8. **Token measurements.** tiktoken 0.12.0 / cl100k_base / encode_ordinary on exact
   generated text. Developer compact offline **1878 → 1955**, compact URL
   **555 → 557**, developer full offline **17597 → 17750**, full evidence pack
   **17300 → 17453**. Conditional Web topic: **870**. Largest compact offline:
   **1989**, within the existing <2000 check. Counts exclude user tasks, framing
   and documents retrieved later. The official vocabulary download was denied by
   the proxy; the published npm ranks reconstructed byte-identically to the pinned
   SHA-256 `223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`.
   Python tiktoken used that verified cache; its counting method was not replaced.
   Validation tools/caches live outside the repository under /workspace.

9. **Security.** Browser compilation rejects arbitrary calls, server imports,
   secret lookup, raw HTML, script/URL/inline-style sinks and inline event code.
   Text nodes render strings safely; generated assets are external and standalone
   pages have restrictive CSP. All bundle source is public: keep secrets and
   resource authorization on supported backends. Production needs HTTPS, header
   CSP and validated session/CSRF/authorization/rate controls. Shield bounded
   loopback ingress is not persistent production hosting. No ASVS 5.0 assessment,
   external audit, penetration test or production certification was performed.

10. **Performance.** No startup/throughput/comparative benchmark. Acceptance app.js
    is **2841 bytes**, app.css **92 bytes** before compression/minification. These
    are asset sizes, not speed measurements.

11. **Known gaps.** Experimental Python browser driver; constrained expressions
    and HTML subset; no event payload/two-way input. Native @scope-capable browser
    required. No complete CSS parser/import/minification pipeline, static template
    extraction, props, conditions/lists, routing/context, browser APIs, Vite/HMR,
    source maps, frontend formatter/LSP, automatic a11y audit or SSR. Binding
    selection scans the binding table. Runtime overflow has no automatic error
    page or rollback. No production readiness claim is made.

12. **Next milestone.** Review the bounded M1 and its evidence first. M2 composition,
    typed props, imports and routing require a separate change. SSR/hydration/SSG
    require later RFCs. None of M2–M6 was automatically started.

13. **Commit/PR.** No commit, PR or deployment created. No SHA/URL is claimed;
    the shared working tree contains the implementation for review.
