# Local production-example qualification — 2026-11-02

Scope: the experimental M5 static example on Linux, without an application
backend, cloud account or public domain. This extends the original
[M5 validation](validation-m5.md); it does not start M6 or certify production
readiness. [Machine-readable evidence summary](qualification-report.json).

## Executed checks

`tests/test_web_production.py tests/test_web_qualification.py`: **24 passed,
zero skipped, in the initial 24.74-second run**, with browser/Vite/Docker requirements enabled.
After D195 maintenance the complete focused suite passed **154 tests** in 88.78
seconds, including these same 24 production/qualification cases. The JSON summary
records the current per-case durations and scanned-artifact/source fingerprints.
Environment: Python 3.12.14, Node 24.19.0, Playwright 1.56.0, system Chromium,
Docker 28.4.0. Original release, integrity, accessibility, keyboard, scoped CSS,
API fixture, header and container-hardening regressions remain included.

The eleven release/maintenance browser cases selected by
`-k 'real_browser and not nginx and not component_hmr'`
also passed in **WebKit 26.0**, zero skips, in 15.47 seconds. They cover keyboard/
axe/style scope, opt-in observation, SRI tampering, routes/context, mutation/CSRF/
invalidation fixtures, light/dark public documentation, keyed row/focus/disposal
and typed event/escaping checks, plus static preformatted/nesting preservation. This Linux Playwright
engine result does not establish Safari/iOS device support. Locally extracted
libraries were supplied to the browser bundle; host preflight validation was
skipped because these libraries were outside the system linker cache, while the
actual browser tests executed normally. The CI job installs system dependencies.

Additional regressions: M1–M4 frontend suites **92 passed**; post-M5 maintenance
**14 passed**; AI Guide Python suites **23 passed**; Node guide runner and **48
browser variants** passed. Lexer/parser/check JSON/core LSP selection: **209
passed, 16 native parity cases deselected**. Website freshness, measured AI Guide
freshness (**87 generated files**), YAML/shell syntax and git diff checks pass.
Optional Web guidance and a separately retrievable keyed/input project teach D195;
unrelated tasks do not need either payload. The current wheel includes the keyed
runtime and dev HMR resource and successfully builds the project after extraction.

The new local edge test uses two non-root/read-only Nginx containers on an owned
Docker network and a disposable one-day certificate with localhost/IP SANs.
The client explicitly trusts that certificate, verifies the hostname and proves
that default trust rejects it. TLS 1.2 and 1.3, HTTP 308 redirect, HSTS and
forwarded CSP passed. This is a test edge, not a supplied public-domain deployment
configuration. No TLS verification bypass or system trust modification is used.

The load test sends 160 GETs with 16 workers, consumes and checks response bodies,
distinguishes 200 from intentional 429 responses and checks no-store on errors.
The latest recorded run accepted 41 requests and limited 119. It then verifies recovery
after the configured burst drains, restarts the same immutable container and
checks readiness and unchanged HTML. Published loopback latency samples cover
all responses, including 429s: they are not business throughput, capacity or SLOs.
No timing threshold gates CI. This is a short burst, not a sustained load/soak test.

## Image security findings and remediation

Checksum-verified **Trivy 0.76.1** scanned the actual saved image archive using a
database updated 2026-11-02. The original upstream digest reported 1 CRITICAL,
2 HIGH, 3 MEDIUM, 44 LOW and 2 UNKNOWN package findings. Repeated package findings
are not counts of unique CVEs. Notable blockers:

| Package | Finding | Pinned correction |
| --- | --- | --- |
| brotli-libs | CVE-2026-111497, CRITICAL | 1.2.1-r0 |
| curl / libcurl | CVE-2026-92392, HIGH in each package | 8.23.0-r0 |
| musl / musl-utils | CVE-2026-107891, UNKNOWN | 1.2.6-r3 |
| zlib | CVE-2026-85091, MEDIUM | 1.3.2-r1 |

The stable upstream tag still resolved to the previously pinned digest. The
Dockerfile retains that digest and explicitly installs these signed Alpine fixes.
The rebuilt image's full rescan reported **zero findings at all severities** with
that database; the separate HIGH/CRITICAL exit-code gate passed. This is dated
scanner evidence, not proof of vulnerability freedom. Package pins require
deliberate maintenance if upstream repositories remove versions.

The optional BuildKit proxy-CA secret is read only during package installation;
it is not copied into layers. No credentials or generated certificate keys are
published. The exact local image ID, scanner checksum, database metadata, report
hash and validation source fingerprints are recorded in the JSON summary.

## Reproduction and CI

See the [example operations guide](production/README.md#reproduce-local-qualification)
for the commands and required flags. The Web production workflow now runs these
tests, scans the built image and uploads asset/load/scan JSON reports. Scanner or
database failures and HIGH/CRITICAL findings fail the gate. The workflow also
configures isolated Firefox/WebKit regression jobs. These new workflow changes
are locally prepared; no passing remote execution of them is claimed here.

Local Firefox 142 launch attempts failed in this managed container with read-only
user-namespace mappings and compositor startup errors. It also predates native
@scope support (Firefox 146). CI therefore pins Playwright 1.62.0 / Firefox 153;
that job remains unverified until actually executed. WebKit initially lacked
system libraries; after supplying them, the eleven browser cases above passed. Browser coverage must
be reported from successful executions, not downloaded binaries or configured jobs.

## Outside this local qualification

- A real backend: authentication, logout/session rotation, secure HttpOnly/SameSite
  cookies, CSRF, tenant/resource authorization, input validation and rate limits.
- Public CA/domain renewal, HTTPS edge failover, production secrets and external
  monitoring/alerting with retention and privacy rules.
- Workload-specific SLOs, sustained/soak load, capacity and failure-domain analysis,
  multi-instance rollout and complete release rollback.
- Actual device/browser support matrix, manual assistive-technology review and an
  application-specific OWASP ASVS 5.0 security assessment.

Shield's bounded loopback ingress is still not a persistent production server.
No cloud deployment, security certification or WCAG conformance is implied.
