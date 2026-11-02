# Static release example (experimental M5)

Install Nexo, Node 22.12/24+ and the pinned tooling. From the repository:

```sh
npm ci --prefix nexo/web_tools
python -m nexo web audit --security --accessibility --json examples/web/production/app.nexo
python -m nexo web build examples/web/production/app.nexo --release -o examples/web/production/dist
python -m nexo web preview examples/web/production/dist
```

`--release` minifies/tree-shakes with Vite and emits hashed JS/CSS, SRI HTML and
nexo-manifest.json. A clean/dedicated output directory is required; unrelated
files are never deleted. A subsequent build replaces all prior owned artifacts.
`--sourcemap` composes original Nexo line mappings and embeds source; do not
publish maps unless intended. Check and unoptimized builds still work without Node.
The preview verifies the manifest and serves only its listed assets on loopback;
it is not production hosting. Fragment routes work without an SPA rewrite rule.

## Local deployment test

The Dockerfile expects this directory's generated dist and nginx.conf:

```sh
docker build -t nexo-static-example examples/web/production
docker run --rm --read-only --user 101:101 --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777 \
  -p 127.0.0.1:8080:8080 nexo-static-example
# GET http://127.0.0.1:8080/healthz
```

This example is static-only. No API proxy, sessions, auth or production TLS is
provided. Deploy behind a trusted HTTPS edge, redirect HTTP and set HSTS there.
Keep HTML no-cache, hashed assets immutable, MIME/nosniff/CSP headers intact.
The sample enables gzip with Vary: Accept-Encoding; edge compression is also
valid; error responses remain no-store. Do not rewrite already-built asset bodies because SRI verifies them.
Upload assets before switching HTML, retain previous hashed assets during a
rolling rollout, and roll back the complete immutable build/image. A single
static site usually does not need Kubernetes; choose managed hosting/CDN based
on budget, reliability and operations. Use Kubernetes only when the application's
service architecture justifies it. Backend /api endpoints need separately verified
authorization, CSRF, secure HttpOnly/SameSite cookies, rate limits and input validation.
Shield's bounded loopback ingress is not a replacement production server.

The image digest is pinned for reproducibility, not a promise of vulnerability
freedom. The Dockerfile also pins signed Alpine package fixes for Brotli,
curl/libcurl, musl and zlib following the 2026-11-02 scan. If a pinned package is
removed from the repository, deliberately update/revalidate the pin; do not silently
fall back to unpinned packages. Scan/update images and dependencies before deployment. No cloud account,
public deployment, end-to-end TLS or production load qualification is implied by
a local container smoke test.

## Reproduce local qualification

```sh
python -m pip install -e '.[dev]' playwright==1.56.0
python -m playwright install --with-deps chromium
npm ci --prefix nexo/web_tools
NEXO_WEB_REQUIRE_BROWSER=1 NEXO_WEB_REQUIRE_VITE=1 NEXO_WEB_REQUIRE_DOCKER=1 \
  NEXO_WEB_LOAD_REPORT=/tmp/nexo-web-load.json \
  python -m pytest tests/test_web_production.py tests/test_web_qualification.py -q
```

The additional tests use a disposable verified local certificate and two hardened
Nginx containers to check TLS 1.2/1.3, HTTP redirect, HSTS and retained CSP. They
also exercise 160 loopback requests at concurrency 16, distinguish accepted and
rate-limited responses, check burst recovery and restart the same immutable image.
The rate-limit fixture is intentionally small; aggregate responses/s include 429s
and are not application throughput or an SLO. No public CA/domain renewal or
business flow is tested. Certificates and private keys are temporary test fixtures.

For another engine install `firefox` or `webkit` with Playwright, set
`NEXO_WEB_BROWSER=firefox` (or `webkit`) and run the production suite with
`-k 'real_browser and not nginx'` and required browser/Vite flags. Missing engines
must fail required checks. Browser jobs are configured in CI; consult their actual
results rather than inferring support from a configured job.

The Firefox job pins Playwright 1.62.0 (Firefox 153); Chromium/WebKit qualification
uses 1.56.0. Its older Firefox 142 lacks the native `@scope` used by the compiler.
MDN lists Firefox support from 146 and Chromium from 118; Safari 26 has known
scoped input/textarea limitations, corrected in 26.4. These are CSS feature
boundaries, not claims of complete framework/device qualification.

The Web production quality workflow downloads checksum-verified Trivy 0.76.1,
builds this image, scans its saved archive and fails on HIGH/CRITICAL findings.
It uploads the JSON scan and load/asset reports even when a later gate fails.
For local scanning use the same pinned Trivy release and verified release checksum:

```sh
docker save -o /tmp/nexo-web-image.tar nexo-static-example
trivy image --input /tmp/nexo-web-image.tar --scanners vuln \
  --severity HIGH,CRITICAL --exit-code 1 --format json \
  --output /tmp/nexo-web-image-scan.json --timeout 5m
```

Database download/scan errors are failures, never clean results. A zero-finding
scan is dated evidence, not proof of vulnerability freedom. For managed builds
behind a trusted TLS-intercepting proxy, pass its CA with
`docker build --secret id=proxy_ca,src=/path/to/ca.pem ...`; it is mounted only for
the package-install step, with certificate verification enabled, and is not copied
into the image. Registry credentials remain managed by Docker.

## Operations and optional browser events

Nginx provides /healthz (static-server liveness only), rate limiting, and JSON
access logs with status/bytes/duration/method. URLs, queries, cookies, IPs and
authorization headers are omitted from access logs. Native critical diagnostics
on stderr may still contain context; restrict/redact them before external retention. Collect aggregate error rates and latency at
your edge/log platform, set workload-specific SLOs, test rollbacks and retain logs
according to the application's privacy requirements. Healthz does not prove the
backend or application business flow works.

`web build --release --observe ...` opts in to local `nexo:web` CustomEvents.
There is no external collector, network upload or persistence. Details contain
schema_version/kind plus phase, request operation, sanitized error code/status
and rounded duration_ms when available; no URLs, route parameters, component/user
names, payloads, messages, stack traces or tokens. A trusted same-origin operations
script may subscribe before main.js runs. Consumers own listener cleanup and must
not treat client events as authoritative security or billing records. Default
builds emit no such events. No unhandled-error capture or automatic analytics is
implemented. Test a real application and obtain appropriate consent before wiring
any external monitoring destination.
