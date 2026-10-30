# Advanced AI Guide validation — 2026-10-30

Executed locally:

- `python3 -m pytest tests/test_ai_guide.py tests/test_ai_guide_advanced.py tests/test_check_json.py -q`: **38 passed**, including the real tiktoken test and browser/local scoring parity cases.
- `node --test tests/ai-guide.test.cjs`: **8 passed**.
- `python docs/ai-guide/local.py verify-examples`: **5 examples checked and executed**, matching recorded output. Uses the Python interpreter and local loopback HTTP; no provider call or external database/broker.
- `python docs/ai-guide/build.py --measure --check`: **80 generated files fresh**, with tiktoken 0.12.0 / cl100k_base / encode_ordinary.
- `python tests/ai_guide_browser.py`: **passed** for 48 prompt variants, experience-adaptive questions, archived version selection, example search, evidence/Unknown scoring, Proposed ADRs, cost calculation, clipboard success/denial, keyboard fallback, six viewport widths, themes/menu, fetch/revision failure and no-JavaScript default.
- `git diff --check`: passed.

The Developer Compact prompt at this revision measures **1,878 reference tokens
offline / 555 URL-based**; the curated full pack measures **17,300**. Prompt counts
exclude the user's task, chat framing, dynamically entered readiness/ADR text and
retrieved documents. Other providers may tokenize differently. Canonical inputs,
not model responses, are deterministic.

## Fixture-only evaluation

[Measured local report](evaluation-fixture-results.json) records an actual run of:

```sh
python docs/ai-guide/evaluation.py /tmp/nexo-guide-paired-fixtures.json
python docs/ai-guide/local.py benchmark /tmp/nexo-guide-paired-fixtures.json \
  --execute --output docs/ai-guide/evaluation-fixture-results.json
```

The same known repository code was supplied in both arms for five tasks (CLI,
REST API, database, event worker, fake AI agent). All **10 local candidate runs**
passed semantic checks, declared execution and exact-output regression. The
report contains actual tool wall times and reference transcript token counts.
No model was invoked: correction counts reflect fixture transcripts, generation
time is unknown, prices/costs are null, native compilation and runtime security
validation are Unknown. This establishes harness operation, **not an AI coding
success rate or an improvement due to the guide**. Do not compare fixture token
costs as evidence of real model efficiency. Model provenance and generation
measurements need independently recorded paired trials.

## Limits and remaining work

No full compiler/runtime suite, native example builds, paid/live AI benchmarks,
cloud writes, local Kubernetes cluster, production rollout, independent security
audit or model quality certification was performed. Existing diagnostic regression
tests ran; compiler/runtime sources were not changed. The benchmark's output
oracle is bounded, not an exhaustive functional/security test suite. Its local
executor is not a sandbox for hostile code.

Production readiness evidence is entered by users and must be reviewed; a score
cannot approve deployment. Generic agent guidance is available. Tool-specific
Codex/Claude adapters remain optional future work pending documented formats and
validation. Host probes, native build evidence, held-out acceptance/security tests
and real model trials remain necessary for stronger claims.
