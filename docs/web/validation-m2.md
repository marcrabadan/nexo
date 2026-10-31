# Nexo Web M2 delivery — 2026-10-31

M2 is implemented as an experimental browser frontend. This report extends the
[historical M0–M1 report](validation.md); it does not claim production readiness,
a public deployment, or a fresh run of the entire native compiler test suite.

## Architecture and implementation

RFC-0001 edition 2 and decision D191 define composition independently of the
existing server interpreter and D175 WASI target. The Python browser compiler
reuses Nexo expression parsing, resolves project modules under D124, checks typed
frontend declarations and emits direct DOM operations and external scoped CSS.
No new production dependencies or virtual DOM were introduced.

Implemented declarations are `component`, `page`, `layout` and `context`.
Components/pages accept typed `int`, `str` and `bool` properties. Component calls
compose views; `slot()` projects caller-owned bindings, context and CSS with
receiver-owned cleanup. `provide` creates isolated contexts; `inject()` resolves
the nearest lexical provider. Context actions batch reactive updates across
owners. Child property updates preserve child local state.

Pages declare explicit fragment paths, typed parameters and title/description.
Nested layouts preserve their common prefix during navigation. Leaving pages
and layouts releases subscriptions/listeners. Not-found pages and layout-free
mount/navigation error pages are supported. Navigation updates document metadata
and moves focus to the new main landmark or heading. Project component imports
respect aliases and `pub`; local CSS imports are expanded inside component scope.

New implementation files: `nexo/web_model.py`, `nexo/web_codegen.py`,
`nexo/web_router.js`, `tests/test_web_composition.py`, and the complete
`examples/web/composition/` project. Updated shared compiler/runtime files:
`nexo/web_frontend.py`, `nexo/web_runtime.js`, `pyproject.toml`, and the existing
M1 tests. New actionable diagnostic groups are NW2001–NW2005 for modules,
properties/composition, routes, context and slots.

## Working application

From the repository with Nexo installed:

```sh
python -m nexo web check --json examples/web/composition/app.nexo
python -m nexo web build examples/web/composition/app.nexo -o /tmp/nexo-composition
python -m http.server 8765 --bind 127.0.0.1 --directory /tmp/nexo-composition
```

Open `http://127.0.0.1:8765/`. The project demonstrates reactive properties,
projected content, independent counters, shared context, typed user routes and
persistent nested layouts. Keep its `nexo.toml`, modules and CSS together.

## Website and AI Guide

Website canonical sources are `docs/web/page.tmpl` and `docs/web/build.py`;
`docs/web.html`, `docs/web-demo/` and `docs/web-m2-demo/` are generated. The M2 demo
includes downloadable project dependencies. After publication the intended URLs
are `https://marcrabadan.github.io/nexo/web.html` and
`https://marcrabadan.github.io/nexo/web-m2-demo/`. They have been tested locally
from the published directory; remote publication has not been performed.

AI Guide canonical changes are in `source/nexo-web.md`, `source/catalog.json`,
`source/examples.json`, `source/syntax.md`, `build.py`, `page.tmpl` and `README.md`.
The generator produces versioned frontend project payloads (`web-examples.json`),
compiler evidence, dependency hashes, topic guidance, profile prompts,
`docs/ai-guide.html` and `docs/llms*.txt`. The playground ZIP was regenerated.
The CI browser step now includes M2 tests. RFC, language specification, decisions
and syntax highlighting also reflect the implemented milestone.

Current content revision:
`edd1e5a152d5561b670629139ffdd3df92b99909978605fe326a7b609d5a0ad9`.
All 82 generated guide files are fresh. The pinned tokenizer measures developer
compact offline at 1,954 tokens (M1: 1,955), maximum compact offline at 1,988
(M1: 1,989), full evidence at 17,458 (M1: 17,453), conditional Web detail at
1,447 (M1: 870), and the separately retrieved Web examples at 1,611.
Web detail is optional for unrelated tasks; existing profiles and prompt variants
remain available.

## Executed validation

- `pytest tests/test_web_frontend.py tests/test_web_composition.py tests/test_ai_guide.py tests/test_ai_guide_advanced.py tests/test_libraries_page.py -q`: **70 passed** with browser execution required. This includes seven real Chromium browser tests, independent state, reactive composition, slots, cleanup, routing, contexts, scoped CSS, safe rendering and AI project reconstruction.
- `pytest tests/unit/test_lexer.py tests/unit/test_parser.py tests/test_check_json.py -q`: **203 passed**.
- Relevant playground bundle/link checks: **2 passed**, 3 deselected.
- Existing AI Guide backend example verifier: **5 examples executed and verified**.
- `node --test tests/ai-guide.test.cjs`: passed; direct execution of the same file reports **8 passed**.
- `python docs/web/build.py --check`: passed.
- `python docs/ai-guide/build.py --measure --check`: **82 files fresh**.
- Existing AI Guide browser QA: passed all 48 prompt variants, version selection, examples, readiness, copy/fallback, themes, navigation and failure cases.
- Additional Chromium review of the generated Web page: no horizontal overflow at 360, 768 and 1440 pixels in light/dark mode. Published-directory M2 demo: shared state survives navigation, typed route/title and main focus correct, no page errors.
- `git diff --check`: passed.

Browser tests retained CSP restrictions. One test initially used an evaluation
helper incompatible with that policy; it was corrected to use Playwright's title
assertion rather than relaxing CSP. No full accessibility audit or cross-browser
certification is claimed. Earlier native/full-suite results and deferred checks
remain recorded separately in the M1 report.

## Security, performance and remaining work

Rendering uses safe DOM text operations. Route destinations are local fragments;
parameters are decoded and validated. Module/CSS paths cannot escape the project
root, including through symlinks; cycles and remote CSS imports are rejected.
No server secrets or backend authorization are implemented in this browser
milestone. Shield's existing execution profiles and server HTTP semantics remain
separate. Frontend visibility never grants backend permissions.

The generated M2 JavaScript is 22,448 bytes and CSS 676 bytes, uncompressed;
these are asset sizes, not speed benchmarks. No runtime performance benchmark
has been conducted. CSS relies on modern browser `@scope` support.

Current limits: scalar properties/context only; zero-argument actions; one slot
per component and exactly one per layout; no layout properties or package
imports; no conditional/keyed-list rendering, event payloads or callback props.
Routes use fragments without query/wildcard/optional parameters. Error pages
cover mount/navigation failures, not arbitrary action exceptions. Use one router
per document for metadata ownership. The browser driver is Python; the Rust
browser frontend, HMR, typed browser APIs, SSR and hydration are not implemented.

The recommended next milestone is M3 typed asynchronous API reads/mutations and
their cancellation/error contracts. M3–M6 have not been started. No commit, PR or
public deployment was created for this delivery.
