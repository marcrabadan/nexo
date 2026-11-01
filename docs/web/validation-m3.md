# Nexo Web M3 delivery — 2026-11-01

For the next milestone see [M4 delivery](validation-m4.md). This report preserves
the M3 validation record.

M3 is implemented as an experimental Python browser frontend, with matching
website and versioned AI Guide. This extends [M2](validation-m2.md). No production
certification, public deployment, full native-suite rerun or M4 implementation is
claimed.

## Architecture and files

RFC-0001 edition 3 and D192 define owned asynchronous data resources. The compiler
reuses the core Nexo expression/type AST; JSON contracts and request semantics
live in `nexo/web_data.py`, while `nexo/web_data.js` implements bounded fetch,
validation, request leases, memory cache and resource state transitions. Neither
the server HTTP implementation nor Rust/WASI syntax or execution was changed.
No new production dependency was added.

Updated compiler layers are `nexo/web_model.py` (contextual records/requests and
view branches/loops), `nexo/web_frontend.py` (typed expressions and same-origin
connect CSP), `nexo/web_codegen.py` (contracts, readiness checks, structural DOM
regions), `nexo/web_runtime.js` (owned regions and parent-first settling), and
`pyproject.toml` (runtime asset packaging). Tests are `tests/test_web_data.py` plus
the preserved M1/M2 suites; the obsolete M1 rejection expectation now recognizes
the missing M3 contract diagnostic.

The complete application is `examples/web/data/{app.nexo,nexo.toml,server.py}`.
Website canonical sources are `docs/web/{page.tmpl,build.py}` and shared syntax
highlighting in `docs/assets/site.js`. Generated outputs are `docs/web.html`, the
refreshed M1/M2 demos and new `docs/web-m3-demo/` including source downloads.
RFC, decisions and `docs/especificacion-nexo.md` record the language extension.

AI Guide canonical inputs changed in `source/nexo-web.md`, `source/syntax.md`,
`source/catalog.json`, `source/examples.json`, `page.tmpl`, `README.md` and
`build.py`. It hashes the new runtime and explicit example support files. The
existing pipeline emits 82 fresh current artifacts, versioned snapshots, frontend
example projects/evidence, `docs/ai-guide.html`, `docs/llms.txt`,
`docs/llms-full.txt` and measurements. The interpreter playground ZIP was rebuilt.
CI now runs M3 browser tests alongside M1/M2.

## Implemented language and lifecycle

- Field-only JSON `type` records with int/str/bool, nested records and lists;
  qualified `pub` project imports, exact fields and typed constructors.
- `load name = api.get(url, as: T, ...)`: starts on mount and reacts to URL inputs.
- `mutation name = api.post/put/patch/delete(...)`: explicit user-triggered writes.
- loading/ready/success/error/data; reload/cancel/run operations; explicit local
  read invalidation after successful writes. Errors expose kind/message/status.
- `if/elif/else` and immutable snapshot `for` views. Data reads require a ready
  or mutation-success branch; error fields require an error-not-none branch.
- Every structural region owns descendant effects, listeners, state and requests.
  Replacement disposes them before rendering new content. Comment anchors retain
  semantic HTML list parentage. Replacing a list resets child local state.
- Nonblocking fetch, owner cancellation, stale-generation rejection, independent
  consumer leases, same-root deduplication, optional bounded cache and GET retries.

The full signatures, bounds, errors and troubleshooting are in
[RFC-0001 M3](../rfc/0001-nexo-web.md#m3). Unsupported syntax is not advertised as
available: no keyed reconciliation, arbitrary async functions, optional/union/
float JSON contracts, event payloads, record component props or cross-origin APIs.

## Run the working application

```sh
python -m nexo web check --json examples/web/data/app.nexo
python -m nexo web build examples/web/data/app.nexo -o dist
python examples/web/data/server.py --directory dist
```

Open `http://127.0.0.1:8765/`. The loopback development API serves independent
cookie sessions, injects a CSRF token, validates Origin/token and typed product
input, and supports the example GET/POST. All five compiler API verbs are tested
against the browser HTTP fixture. The development API is not a production backend
or a new Shield execution profile.

The public-directory demo fetches a real static JSON file; writes are disabled.
After publication, intended URLs are:
`https://marcrabadan.github.io/nexo/web.html#data` and
`https://marcrabadan.github.io/nexo/web-m3-demo/`. Both were tested from local
`docs/` hosting; this delivery has not published them remotely.

## Executed checks

- `pytest tests/test_web_frontend.py tests/test_web_composition.py tests/test_web_data.py tests/test_ai_guide.py tests/test_ai_guide_advanced.py tests/test_libraries_page.py -q`: **106 passed**, browser execution required. M3 contributes 36 tests, including 20 real Chromium cases; M1/M2 add seven browser cases. One unrelated third-party pytest deprecation warning remains.
- `pytest tests/unit/test_lexer.py tests/unit/test_parser.py tests/test_check_json.py -q`: **203 passed**.
- Existing HTTP/server/modules/playground regression subset: initially **9 passed, 1 skipped, 2 deselected** with interpreter-only tool availability. Follow-up with the existing native binary/toolchain: **17 passed, 2 deselected**, covering both implementations and D175 missing-runtime/unsupported-target diagnostics. The two deselected tests require Pyodide or a complete WASI browser runtime.
- Existing AI Guide backend verifier: **5 examples checked and executed**, exact expected outputs.
- `node --test tests/ai-guide.test.cjs`: passed; direct execution reports **8 passed**.
- `python docs/web/build.py --check`: passed.
- `python docs/ai-guide/build.py --measure --check`: **82 files fresh**.
- Existing AI Guide Chromium QA: passed all 48 variants, version selection, example filtering, readiness, copy/fallback, keyboard navigation, fetch failure and no-JavaScript cases.
- Web page review at 360/768/1440 pixels in light/dark themes: no horizontal overflow. Generated M2 and M3 demos run without page errors; static M3 reads/reloads JSON and disables creation. Source downloads and reconstructed versioned M3 project match compiler hashes, including the local API support file.
- `git diff --check`: passed.

Coverage includes all five verbs; load/mutation states; invalidation; response and
request validation; malformed types/extra fields/unsafe integers; non-JSON/large
responses; request size limit; strict same-origin URLs; redirect rejection;
missing CSRF rejection before transport; backend Origin/token rejection;
timeout/retry/cancel; last-consumer abort; shared-consumer independence; root
isolation/cache reuse; stale response suppression; HTML escaping; special JSON
field names; region cleanup; imported contract privacy and recursion rejection.

An early browser failure exposed child effects observing invalid data before a
parent branch was removed. Parent-first scheduling fixes it; that transition is
covered by the passing timeout/cancellation test. Another early assertion used an
unreliably short 30 ms transport timeout; the fixture now allows 200 ms against a
600 ms delayed endpoint. CSP remained enabled. Stale generated-artifact failures
were resolved through the generators, not by relaxing freshness assertions.

## Tokens, security, performance and limits

Current guide revision:
`38e671ccbcfdd43ed34036e86fe61ccac51bca21fe50b73a97140fe99885c64b`.
Pinned-tokenizer counts compared with M2: developer compact offline **1,955**
(previously 1,954); maximum compact offline **1,989** (1,988); full evidence pack
**17,447** (17,458); optional Web detail **2,249** (1,447); separate frontend
projects **3,587** (1,611). The larger Web topic/projects remain selective rather
than being injected into unrelated compact tasks. Existing profiles/URL/offline
variants remain usable and versioned.

M3 generated JS is **20,478 bytes**, CSS **273 bytes**, uncompressed. No runtime
speed, memory, throughput or cross-browser benchmark was measured. Conditional
markup uses direct DOM ownership, not a virtual DOM. CSS still needs @scope.

Production requests require HTTPS; loopback HTTP is allowed for local work.
Transport uses same-origin credentials, no redirects, no HTTP cache and a restrictive
connect-src. Backend bodies are not reflected in resource errors. POST/PUT/PATCH/
DELETE require server-provided CSRF metadata. These controls complement backend
authentication, resource permissions, Secure/HttpOnly/SameSite cookies, rate limits
and input validation; client visibility/validation never grants authorization.
Relevant [OWASP ASVS 5.0](https://github.com/OWASP/ASVS/tree/v5.0.0) validation,
browser/session/access-control and secure-communication areas inform the design;
no certification or complete ASVS audit is claimed.

JSON integer transport is intentionally restricted to the safe integer range;
use str for larger IDs. Limits are 64 KiB request/1 MiB response, 32 nesting levels,
10,000 list items, 64 cached entries, timeout <=120 s, TTL <=60 s and <=3 GET
retries. Writes never retry and cancellation cannot undo a committed mutation.
Cache/deduplication are root-local, not persistent or shared across users; destroy
and remount on identity changes. Only explicitly listed reads reload after a
mutation; other rendered reads can remain stale. Compound guards do not narrow
types yet. Layout slots must remain unconditional. The development server has no
production TLS, auth, operational controls or deployment assurance.

A full native compiler/WASI execution suite and formal accessibility/cross-browser
security audit were not rerun for M3. D175 code is unchanged. Next is M4 development
experience/security tooling, starting with a separate scoped plan. M4–M6 have not
been started. No new commit, push, PR or public deployment was created for M3.
