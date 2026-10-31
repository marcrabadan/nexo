# Nexo AI Guide

The GitHub Pages [AI Guide](../ai-guide.html) provides four profiles, Compact
(default) / Full Context, and offline / URL-based prompts. It needs no backend,
account or API key. The default self-contained prompt is rendered in HTML;
JavaScript fetches only static same-origin data to switch profiles. Clipboard
failure selects the text for manual copying and announces the fallback.

The behavioral foundation in `source/engineering.md` comes from the requirements
of this implementation request, not an additional external engineering guide.
Compact prompts preserve its essential behavior; Full Context includes it in full.
Profile selection changes focus, never permissions or feature availability.

## Rebuild and validate

From the repository root, with Python 3.12+:

```sh
python -m pip install -e '.[dev,ai-guide]'
python docs/ai-guide/build.py --measure
python docs/ai-guide/build.py --measure --check
python -m pytest tests/test_ai_guide.py tests/test_ai_guide_advanced.py
node --test tests/ai-guide.test.cjs
```

`--measure` uses **tiktoken 0.12.0**, **cl100k_base**, `encode_ordinary`, on the
exact UTF-8 prompt text. The first run may download the tokenizer vocabulary
through tiktoken's hash-verified cache. See the [tokenizer implementation](https://github.com/openai/tiktoken).
Counts exclude the user's task, conversation, provider framing and documents
retrieved later by URL prompts. They do not promise exact token parity across
providers. `token-counts.json` records payload hashes and actual measured counts.
Without `--measure`, generation and freshness tests use those recorded counts
only after matching every payload hash. The CI job independently remeasures.
No tokenizer or browser dependency is added to Nexo's production runtime.

Optional real browser QA (tested with Playwright 1.56.0 and Chromium):

```sh
python -m pip install playwright==1.56.0
python -m playwright install chromium
python -m http.server 8765 --bind 127.0.0.1 --directory docs
# In another shell:
python tests/ai_guide_browser.py
```

The browser check exercises all 48 variants, copy success/denial, keyboard manual
copy, six widths, theme/menu, missing context fetch and no-JavaScript fallback.
It writes screenshots to `/tmp`, never to the published site.

## Provenance and availability

`source/catalog.json` curates claims and evidence markers. A missing file or
marker fails generation. Availability is resolved conservatively from implemented
code, tests and decisions; a specification or roadmap statement alone cannot
establish availability. Each claim is classified:

- **Implemented:** bounded code/test evidence; inspect the stated platform limits.
- **Experimental:** present, but limited and unsuitable for broad parity claims.
- **Planned:** roadmap/design work, not an available feature.
- **Unverified:** an outcome or integration needing separate validation.

Review the catalog when implementation changes. Automated evidence markers
establish traceability and freshness, not semantic proof that every claim is
correct. Historical documentation in Full Context can describe broader designs;
the explicit availability boundaries govern those passages. Regression tests
keep the known WASI, registry, recovery, budget, Shield and HA limits visible.

The generator hashes selected source paths and bytes, including guide rules,
templates, scripts, styles, component version files, documentation, compiler/
runtime evidence and tests. This SHA-256 **content revision** is independent of
unrelated Git commits. Python package, Rust workspace and API edition versions
are reported separately. It is not a Git commit SHA or a promise of a stable 1.0.
`manifest.json` lists exact source hashes, claim evidence and measurements.

Latest aliases live at `docs/llms.txt`, `docs/llms-full.txt` and
`docs/nexo-syntax.txt`. Prompts point to pinned snapshots under
`docs/ai-guide/versions/v1-<revision-prefix>/`; retain old snapshot folders when
regenerating. The generator refuses to modify an existing snapshot with different
bytes. All linked evidence extracts are plain `.txt`, avoiding Markdown renderer
changes. Updates become live only after the repository's normal GitHub Pages
publication. The [llms.txt proposal](https://llmstxt.org/) does not guarantee
that models automatically discover or follow this context.

## Limits

Full Context is a curated pack, not a complete repository dump or all compiler
internals. Compact deliberately omits most API signatures; when needed, supply
`nexo docs --for-ai file.nexo` or the relevant reference rather than guessing.
The offline option supplies its context without browsing but cannot make an LLM
reliable by itself. The URL option requires actual document retrieval and cannot
verify that a model read the links. No live provider, cloud deployment, Kubernetes
cluster, security audit, HA/SLO, or independent model evaluation is performed by
this site. Token counts do not establish model quality or context-window fit.

## Shield integration

Every profile uses the single shared `source/shield-brief.md` rule: architecture
and readiness reviews assess Shield as the preferred security foundation and
report recommendation, capabilities, profile, unsupported operations, isolation
limits, validation commands and remaining risks. Profile-specific instructions
remain focused on their roles rather than duplicating security checklists.

`source/shield-security.md` provides the detailed assessment, minimal policy,
commands, agent/tool authorization, denial testing, Kubernetes controls and
profile compatibility matrix. The generator emits it as a version-pinned
`shield-security.txt` linked from the page, index and full pack. The five Shield
repository documents are separate snapshots tagged `when: security` in the
catalog. They participate in the revision hash and link tests but are not
embedded in any prompt or Full Context. Assistants load them only for relevant
security work; offline assistants ask for their text when the compact guidance
is insufficient. The full pack remains curated, with conditional security
references rather than a security-document dump.

Shield guidance does not introduce new runtime capabilities. Standard Shield is
API enforcement. Linux offline/online workers require supported hosts; the online
profile is initial and bounded to brokered public HTTP(S). Required interactive
approval is incompatible with the worker IPC channel and fails closed. The
Docker helper remains offline. Kernel availability, denied side effects and
production configuration must be validated on the actual execution host; no
weaker fallback is recommended.

## Advanced engineering workflow

Four profiles (including Beginner-friendly), three experience levels, two context
modes and two access modes generate **48 canonical prompts**. Developer experience
keeps the original prompt filenames; beginner/expert variants use suffixes. The
shared `advanced-brief.md` adds at most five essential questions per round and
facts/assumptions/recommendations/unknowns. `engineering-intelligence.md` supplies
architecture, Proposed ADRs, reproducible engineering, local kind/k3d, observability,
hosting trade-offs and the production readiness rubric. No LLM runs in the browser.
The browser's five-answer brief is structured user input, not automated inference
of requirements, compliance or production approval. Consequential gaps stay open.

The page includes searchable verified examples, project readiness and ADR drafts,
evidence-based production review, optional token/cost arithmetic, and archived
prompt selection. Selecting an archived prompt changes its text, count and download;
the catalog and review forms remain at the current page revision, explicitly labeled.
All user-entered text stays in memory in the browser; it is not transmitted or
saved by the guide. Copy review/ADR text manually. These additions do not change
canonical prompt token counts. No-JavaScript users retain the default prompt,
static examples and versioned downloads; interactive assessments need JavaScript
or the local CLI below.

### Verified examples

`source/examples.json` selects actual repository programs in five domains: CLI,
REST API, database, event worker and AI agent. `examples-verification.json` records
source hashes, runtime revision, verifier hash, exact stdout/stderr and actual
interpreter `check` plus test/run results. `build.py` refuses stale evidence.
Versioned `examples.json` adds source code, component versions, dependencies,
features/domains, expected output, associated tests and maturity/scope. Maturity
values distinguish implemented/tested, experimental, documented-only and unknown;
only the first is asserted for these five locally verified cases. Native compilation,
public deployment, external Kafka and live provider access remain unverified.

```sh
python docs/ai-guide/local.py verify-examples          # execute and compare
python docs/ai-guide/local.py verify-examples --record # review changed output before committing
python docs/ai-guide/build.py --measure
```

Verification uses isolated temporary working directories and loopback HTTP; it
is not an untrusted-code sandbox. CI executes it before freshness checks. The
SQLite example is checked through an output regression (it has no inline tests).

### Local profiler, readiness and ADR tools

Install `.[ai-guide]` for the pinned tokenizer; these commands make no model/API
requests and need no API key:

```sh
python docs/ai-guide/local.py profile docs/ai-guide/samples/profile.json
python docs/ai-guide/local.py readiness docs/ai-guide/samples/readiness.json
python docs/ai-guide/local.py adr docs/ai-guide/samples/adr.json --output /tmp/proposed-adr.md
```

The profiler accepts `iterations: [{input: "full input text", output: "response"}]`.
Include full resent context on every iteration; correction count is turns minus
one. Optionally add `pricing: {currency: "EUR", input_per_million: 2,
output_per_million: 4}`; these numbers illustrate configuration, **not actual
provider prices**. Missing prices give a null cost. Nonfinite/negative prices are
rejected. `compilation` and `tests` are optional user-reported Passed/Failed/Unknown
fields; the profiler never executes code or verifies those claims. cl100k_base
text counts are reproducible; target-provider tokens/costs are estimates and
exclude framing, caches, tool charges and extra context. Shorter characters do
not necessarily mean fewer tokens.

Readiness input is an object keyed by the eight IDs in `source/readiness.json`.
Each entry has `status`, `evidence`, `risks`, `remediation` and optional additional
`critical: true`. Empty evidence downgrades Passed/N/A to Unknown. Equal weights;
Failed/Unknown earn zero, evidenced N/A is excluded, all N/A yields null. Critical
Failed/Unknown items block regardless of score. Review the evidence independently;
these tools validate fields, not their truth. ADRs always start Proposed and
require user approval before storing approved records in the project's `docs/adr/`
or existing ADR location. Neither the website nor CLI approves decisions.

### Reproducible paired evaluation

The harness evaluates supplied **trusted local candidate files**, never sends
code to a provider and never generates model responses. The five task purposes,
feature scopes and expected-output oracles come from `source/examples.json` and
its verified results. Both arms require the same task catalog hash, pinned guide
revision, matching model configurations (include model/version, sampling, seed,
limits and tools), and matching price configurations per pair. Capture transcripts
and actual generation time externally; do not relabel estimates as measurements.

A fixture-only harness smoke run, using the exact same repository code in both
arms, is reproducible without a model:

```sh
python docs/ai-guide/evaluation.py /tmp/nexo-evaluation.json
python docs/ai-guide/local.py benchmark /tmp/nexo-evaluation.json --execute \
  --output /tmp/nexo-evaluation-results.json
# Optional actual native compilation, on a supported built checkout:
python docs/ai-guide/local.py benchmark /tmp/nexo-evaluation.json --execute \
  --native-compiler compiler/target/debug/nexo --output /tmp/nexo-native-results.json
```

`--execute` is required because candidates run as local code, with the user's
normal process permissions. Review candidates first; temporary directories are
not confinement. Use an externally configured supported isolation profile for
hostile inputs. The helper invokes no shell command supplied by the dataset.

Replace fixture records with actual paired model transcripts/candidates to assess
AI coding. A record contains `task`, `arm` (`guide`/`without-guide`), `candidate`
(path relative to input JSON), `model_configuration`, `iterations`, optional
`pricing` and actual `completion_seconds`. Keep all settings and task requirements
identical except the presence of guide context. Retain failed attempts and full
correction transcripts. The harness measures semantic checks, declared test/run
commands, exact-output regression, local tool wall times and optional native
builds. It reports tokens, estimated cost/success, correction turns, test success,
compilation rate (null unless native configured), static policy checks when a
policy exists, and runtime security validation Unknown. Static checks do not
prove isolation. Output regression is not exhaustive functional validation and
can be gamed; use held-out domain/security tests before drawing quality claims.

Record environment, model provenance and repeated trials when comparing results.
The report captures platform/Python, verifier/runtime hashes, source hashes,
revision and configuration. Tool timing and model output are not deterministic;
prompt artifacts are. Fixture-only success establishes harness operation, **not
AI performance or guide superiority**. No paid/live model comparison is bundled.

### Generic agent guidance and remaining scope

`agent-instructions.txt` is generated from the same canonical modules and links
to language/tooling, examples, architecture/testing, Shield/reliability, deployment
and agent guidance. It is generic text, not a claimed Codex/Claude installation
format. Tool-specific adapters are optional and deferred until documented formats
can be validated; no undocumented agent config files are emitted. Instructions
remain guidance, never proof of compiler behavior.

Run both Python suites and Node checks; the optional browser script now exercises
48 variants, archived revisions, example filtering, readiness evidence, Proposed
ADRs and local cost comparison. CI additionally runs browser and fixture-only
benchmark regressions. No cloud/Kubernetes deployment or production certification
is performed. Verify hosting prices and platform support before real deployment.

Executed checks, reference token counts and fixture-only results for this change
are recorded in [validation.md](validation.md). They are local evidence, not a
claim that the GitHub Actions job or live model comparisons have already run.

## Nexo Web (RFC-0001, experimental M1)

`source/nexo-web.md` is a conditional frontend topic, emitted as versioned
`nexo-web.txt`. Compact syntax carries a discovery pointer; unrelated tasks do
not include the complete RFC. All 48 existing profiles/modes are preserved.
`source/examples.json` registers the browser Counter separately in
`frontend_examples`, because the interpreter verifier cannot parse UI. Generation
compiles it with the browser target and records source/JS/CSS hashes; real-browser
execution is checked separately by `tests/test_web_frontend.py` and must never be
inferred from compilation. The topic embeds the exact canonical example.

Rebuild the public page/demo with `python docs/web/build.py`; validate with
`python docs/web/build.py --check` and `python -m pytest tests/test_web_frontend.py`.
The Rust driver and D175 WASI compiler are unchanged. Future frontend features
are explicitly proposed. Frontend topic tokens use the existing pinned tokenizer.

### M2 composition and routing

The same conditional Nexo Web topic now covers experimental M1–M2. Its separate
versioned `web-examples.json` carries every module/CSS/config file needed by the
composition project, avoiding unrelated backend examples in targeted retrieval.
The generator resolves project dependencies with the actual browser loader,
hashes them and compiles each entry with its source path. It never promotes a
compile result into a browser pass. Props, slots, fragment routes, layout/context
ownership and cleanup are tested by `tests/test_web_composition.py`; the existing
Counter and four profiles remain intact. `docs/web/build.py` builds both live
demos from canonical Nexo sources. Browser API loading/mutation remains M3 design.
