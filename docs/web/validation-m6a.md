# M6a static component HTML validation — 2026-11-02

Scope: RFC-0002's experimental `web build --static`, not complete M6 or production
qualification. Python CLI only; no Rust browser-driver parity is claimed.

`NEXO_WEB_REQUIRE_BROWSER=1 .venv/bin/python -m pytest tests/test_web_static.py -q`:
**10 passed**, zero skipped. Chromium 154.0.8037.57 via Playwright 1.56.0.
The browser case serves generated artifacts over loopback HTTP with application
JavaScript disabled, checks text, full int64 digits, checkbox state and scoped
CSS, and confirms no script element or script resource request.

Compiler tests cover escaping, deterministic output, imports/props/lexical slots,
conditionals and short-circuiting, minimum int64 and overflow, unsupported feature
rejection, HTML parser nesting boundaries, preformatted leading newlines,
incompatible build options, CLI invocation, build-mode switching and unrelated
output preservation. The supplied static example builds successfully. Python
compilation and `git diff --check` pass.

The wider frontend/composition/data/debt/tooling run completed with **97 passed,
16 skipped and 2 failures**. One failure was the initial static browser test using
file navigation, which the environment blocks; the corrected test uses HTTP and
passes in the complete 10-case run above. The existing data test
`test_real_browser_last_consumer_disposal_aborts_fetch` timed out waiting for a
requestfailed event; its isolated rerun passed (1 passed). The skipped checks
require optional tooling. This is not a claim of a completely passing full suite
or of the existing M5 production qualification applying to static builds.
