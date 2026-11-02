# Nexo Web M4 delivery — 2026-11-01

For the next milestone see [M5 delivery](validation-m5.md). This report preserves
the M4 validation record. Publication status updated 2026-11-02: M4 and M5
were committed together in [8c0c7b4](https://github.com/marcrabadan/nexo/commit/8c0c7b42901aed6b6857843cf2af3f3a3103945b)
and pushed to `main`. The [M5 remote validation record](validation-m5.md#remote-validation-and-publication--2026-11-02)
links the successful CI and GitHub Pages runs for the subsequent CI fixes.

M4 is an experimental development/security tooling milestone on top of
[M3](validation-m3.md). It does not claim production readiness, browser Shield
execution, release optimization or advanced rendering.

## Architecture and files

RFC-0001 edition 4 and D193 retain Python as the browser compiler and add an
optional pinned **Vite 8.3.5** development transport. Vite supplies watching,
module serving and WebSocket HMR; a compiler-only subprocess bridge supplies
Nexo assets, diagnostics, dependencies and maps. No new bundler, React wrapper,
TypeScript application DSL or Python production dependency was introduced.
Rust/browser compilation and D175 WASI remain unchanged.

New files:

- `nexo/web_tooling.py`: offline scaffolding, Vite process ownership and bounded loopback artifact preview.
- `nexo/web_bridge.py`: versioned JSON compiler subprocess protocol.
- `nexo/web_maps.py`: deterministic Source Map v3 line mappings.
- `nexo/web_diagnostics.py`: shared statement location/snippet rendering.
- `nexo/web_audit.py`: redacted heuristic source findings and honest Shield/backend boundaries.
- `nexo/web_lsp.py`: explicit Python browser LSP, reusing protocol infrastructure.
- `nexo/web_tools/{dev.mjs,plugin.mjs,package.json,package-lock.json,app.nexo}`: trusted runner/plugin, exact optional dependency lock and packaged Counter scaffold.
- `tests/test_web_tooling.py`: ten compiler/tooling/editor/security cases, including two real Vite/Chromium HMR cases.

Updated `nexo/web_frontend.py`, `nexo/web_codegen.py`, `nexo/web_model.py` add CLI
commands, maps, source diagnostics and style-independent scope identity.
`pyproject.toml` packages resources and excludes node_modules; `.gitignore` excludes
installed/generated tooling. No core lexer/parser, server HTTP or Rust source
was changed. The existing core/Python/Rust LSP behavior is preserved; browser
support is selected explicitly with `web lsp`.

## Working workflow and semantics

```sh
python -m nexo web new my-app
cd my-app
npm ci
python -m nexo web dev app.nexo
python -m nexo web check --json app.nexo
python -m nexo web audit --security --json app.nexo
python -m nexo web build app.nexo --sourcemap -o dist
python -m nexo web preview dist
```

Install Nexo first. Node 22.12 or 24+ and locked Vite are needed only for dev;
installation is explicit, never automatic. Build/check/audit/LSP/preview work
without Node. For existing repository examples use `npm ci --prefix
nexo/web_tools`, then `python -m nexo web dev examples/web/counter.nexo`.
Configure an editor command as `python -m nexo web lsp`.

Dev defaults to `http://127.0.0.1:5173/`, preview to port 4173; --port 0 chooses an
ephemeral port. Existing ports fail rather than falling back. New destinations
must not already exist. The scaffold is the actual canonical Counter source and
ships a package lock usable with npm ci.

CSS-only HMR preserves the root/state when emitted JS and mapped positions stay
unchanged. Logic edits destroy and remount the entire application, resetting
state/context/requests. Old listeners become inert. Failed compilation keeps the
last good UI and shows text diagnostics; fixing the entry or creating a missing
import recovers. Initial compilation must succeed. Imported CSS/module changes
are watched; source watching excludes node_modules/.git/dist. Graceful SIGINT/SIGTERM
closes the listener and owned compiler subprocess group on tested Linux.

build --sourcemap emits app.js.map with embedded Nexo sources and line mappings;
default builds omit maps and remove a stale output map. Static output includes
no Vite HMR client. Maps cover declarations/statements/actions/views, not exact
expression columns or CSS. Runtime helpers are unmapped. check JSON retains
schema_version 1 and adds statement column/end_column/snippet. Audit diagnostics
redact snippets even when compilation fails.

Browser LSP offers unsaved-entry diagnostics, saved dependency checking with
related locations, UTF-16 positions, outline, local hover/definitions and canonical
completions. Symbol ranges contain their members. Browser formatting/code actions,
cross-file navigation, unsaved imported buffers and automatic mode selection are
not implemented. NW4001 reports tooling availability; NW4002 tooling boundaries;
NW4101 possible credential literals; NW4102 cache requiring identity review.

## Website and AI Guide

Canonical website source `docs/web/page.tmpl` now documents M4 at `#development`:
setup, explicit Node/Vite requirement, HMR reset semantics, commands, maps, editor
tools and security limits. Existing visual identity, themes, code copying and
navigation remain. `docs/web/build.py` regenerated `docs/web.html` and the M1–M3
demos with the updated compiler. No development dependency was added to the public
website itself. Published documentation:
[development workflow](https://marcrabadan.github.io/nexo/web.html#development).

AI Guide source updates: `source/nexo-web.md`, `source/catalog.json`,
`source/examples.json`, `source/syntax.md`, `page.tmpl`, `README.md` and `build.py`.
Tooling sources, lock and scaffold participate in existing content hashing.
The Counter is reused rather than duplicated in a new prompt system. All 48
profiles/modes and offline/URL variants remain. Generated outputs include
versioned context/evidence/project payloads, `docs/ai-guide.html`, `docs/llms*.txt`
and measurements. The playground ZIP was regenerated; CI installs pinned Vite
and makes missing Vite/Chromium a failure in frontend tests.

Historical M4 guide revision (superseded by the revision in the [M5 report](validation-m5.md)):
`5b23dc17ba641a958056cd1c8d9a67db1266c33f1b0ceb9eaa2b81af56509b04`.
**82 generated artifacts were fresh at M4 validation.** Pinned-tokenizer counts compared with M3:

| Context | M3 | M4 |
| --- | ---: | ---: |
| Developer compact offline | 1,955 | 1,964 |
| Maximum compact offline | 1,989 | 1,998 |
| Full evidence | 17,447 | 17,461 |
| Optional Web detail | 2,249 | 2,850 |
| Separately retrieved Web projects | 3,587 | 3,618 |

The Web topic stays conditional; unrelated tasks do not need its complete text.

## Executed validation

- Focused compiler/browser/website/AI Guide command:
  `pytest tests/test_web_frontend.py tests/test_web_composition.py tests/test_web_data.py tests/test_web_tooling.py tests/test_ai_guide.py tests/test_ai_guide_advanced.py tests/test_libraries_page.py -q`: **116 passed**, with NEXO_WEB_REQUIRE_BROWSER=1 and NEXO_WEB_REQUIRE_VITE=1. The final run took 61.27 seconds; one unrelated pytest plugin deprecation warning remains.
- `pytest tests/unit/test_lexer.py tests/unit/test_parser.py tests/test_check_json.py tests/test_lsp.py -q -k 'not compiler_answers'`: **209 passed, 16 deselected**. The deselected cases are native LSP parity cases; no claim of a fresh native parity run is made.
- Existing backend AI Guide verifier: **5 examples checked and executed** with exact expected output.
- `python docs/web/build.py --check`: passed.
- `python docs/ai-guide/build.py --measure --check`: **82 current files fresh**.
- `node --test tests/ai-guide.test.cjs`: passed; direct execution reports **8 passed**.
- Existing AI Guide Chromium QA: **48 variants passed**, including versions, filtering, readiness, copy success/failure, keyboard fallback, responsive/theme/menu, fetch failure and no JavaScript. Web page passes overflow checks at 360/768/1440 pixels in light/dark themes. M4 documentation and generated M2/M3 demos work from the local published directory without page errors.
- Real Vite/Chromium tests verify CSS-state retention, logic reset, old-listener cleanup, compile-error recovery, new imported-module recovery, external CSS updates, updated embedded source maps, Host/Origin denial, and rejection of .env/arbitrary project source serving. Graceful CLI termination leaves no development listener.
- Locked optional dependency audit: `npm audit --prefix nexo/web_tools --json` returned **0 reported vulnerabilities** on 2026-11-01. This is the registry advisory result at that time, not a production security certification.
- Offline scaffold/npm lock check: **npm ci succeeded** against the populated local cache (15 installed packages).
- Python wheel built successfully; archive inspection confirms tooling/plugin/lock/template assets and no node_modules. An extracted-package check/build outside the repository succeeds on a generated project.
- `git diff --check`: passed.


Tests caught and corrected an exported-alias bootstrap error and a mismatch
between virtual module URLs and Vite's HMR boundary identifiers. HMR replacement
now uses the accepting entry boundary and the compiler's actual bootstrap symbol.
The checks keep CSP enabled and verify state retention/reset, disposal, import
recovery, updated maps and private-file denial rather than simulating HMR.

## Security, performance and remaining work

Dev binds only 127.0.0.1; HTTP Host/Origin and WebSocket upgrade origins are
checked. Vite uses an empty temporary root, no project config, no public directory
and no .env loading. Filesystem serving permits only that root and Vite resources;
project sources are intentionally visible through developer maps, not arbitrary
file paths. Styles use external links; no unsafe-inline/eval is added. Preview
serves only regular built asset names and rejects other paths/hosts/origins.
Loopback is not per-user authentication and these servers are not production
hosting or OS isolation.

Audit checks are bounded heuristics, not secret/dataflow/dependency analysis or
proof of authorization. Cache warnings require review. The report marks backend
authorization as Not assessed and accurately describes Shield memory dispatch
and bounded loopback (8 connections, 3-second accept window, 6-second hard payload
lifetime). There is no browser Shield sandbox, automatic backend execution or
persistent production listener. Use existing supported backend security workflows
separately. Production still needs backend auth/resource permissions, sessions,
CSRF, rate limits, TLS and deployment/header controls; no ASVS certification is
claimed. Maps embed source and must only be published intentionally.

No runtime speed, HMR latency, throughput, memory or cross-browser benchmark was
conducted. Current uncompressed generated JS sizes: Counter 6,392 bytes, M2 demo
23,453 bytes, M3 demo 20,478 bytes. These are asset sizes, not performance results.
The optional Vite dependency is development-only and node_modules is excluded
from the wheel and repository.

Known limits: root-level logic remount rather than state migration/component
patching; line-only JS maps, no CSS maps; limited browser LSP as above; no API
proxy or automatic CSRF/session bootstrap in Vite; M3's full API example keeps its
separate loopback server. Manual reload may be needed after server restart because
strict CSP blocks Vite's optional blob-worker reconnection fallback. Node 24 and
Chromium/Linux were tested; other environments remain unverified. No full native
compiler/WASI parity suite, formal accessibility audit or production penetration
test was rerun for M4.

The subsequent [M5 delivery](validation-m5.md) adds measured optimization,
accessibility/security review, performance/CI gates and deployment/observability
evidence, and is now published on `main` with M4. The local results above remain
the original M4 evidence; later native/compiler and remote CI validation is
recorded separately in M5. M6 has not been started. No PR was created for this
delivery; documentation publication does not establish production application
readiness.
