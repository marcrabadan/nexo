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
freedom. Scan/update images and dependencies before deployment. No cloud account,
public deployment, end-to-end TLS or production load qualification is implied by
a local container smoke test.

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
