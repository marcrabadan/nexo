# M6 E2E against a local Kubernetes deployment

This runner visits an existing HTTP(S) origin. It starts no application server and does not modify deployments, stop services or publish data. It checks the **integrated M6 demo contract**, not arbitrary application selectors. Use `examples/web/m6/app.nexo` with its trusted backend/boot module (or an application preserving that exact test contract).

Five independent browser checks cover server-rendered data with JavaScript disabled; hydration plus keyed identity/local state/context/conditional ownership; actual Worker WASM result 42; changing live SSE snapshots; and HTTP CSP/no-store/nosniff/404 behavior. A missing browser, bad certificate or failed check returns a nonzero exit code. There are no optional skips.

The public PWA install/update/offline tests remain in `scripts/web/stability.py`: they own and stop their test origin. This remote runner deliberately does not stop your Kubernetes workload or attempt an update/rollback of your application.

## Build and load local images

Run from the repository root:

```sh
docker build -f examples/web/m6/k8s/Dockerfile -t nexo-m6-e2e-app:local .
docker build -f tests/e2e/web/Dockerfile --build-arg BROWSER=chromium -t nexo-web-e2e:local .
```

The runner image contains Playwright 1.62.0 and the selected browser plus its system dependencies. Chromium uses its headless shell to reduce image size. To test Firefox or WebKit, build a separate image with `--build-arg BROWSER=firefox` or `webkit`, give it a distinct tag, and set both `E2E_IMAGE` and `BROWSER` when launching the Job. Each engine must be supported by your cluster's container sandbox; unavailable engines fail, rather than being relabeled as passed.

If your build network needs an additional proxy CA, pass its public certificate through `--secret id=proxy_ca,src=/path/to/proxy-ca.pem`. The Dockerfiles use it only during dependency downloads; it is not installed in the resulting image. TLS verification stays enabled.

Load the images into your cluster, for example:

```sh
kind load docker-image nexo-m6-e2e-app:local nexo-web-e2e:local --name YOUR_CLUSTER
```

For k3d use `k3d image import ... -c YOUR_CLUSTER`; for Minikube use `minikube image load IMAGE` for each image. The manifests use `IfNotPresent`. When rebuilding an image, use a fresh tag (and update the Deployment/Job) to avoid stale node images.

## Deploy the public fixture

Verify the current kubectl context before applying. These manifests create a public demo, not authentication or production hosting.

```sh
kubectl config current-context
kubectl create namespace nexo-e2e --dry-run=client -o yaml | kubectl apply -f -
kubectl -n nexo-e2e apply -f examples/web/m6/k8s/deployment.yaml
kubectl -n nexo-e2e rollout status deployment/nexo-m6 --timeout=120s
```

The example runs Gunicorn 26.2.0 with one worker/eight threads and a public in-process SSE publisher. Deployment and Job use non-root identities, read-only filesystems, dropped capabilities, no service-account token and bounded resources. This does not qualify authentication, distributed subscriptions, sustained capacity or graceful streaming shutdown.

### Fastest local check: port forwarding

Run in one terminal:

```sh
kubectl -n nexo-e2e port-forward service/nexo-m6 8080:8080
```

In another terminal with Playwright installed:

```sh
python -m pip install playwright==1.62.0
python -m playwright install --with-deps chromium
python tests/e2e/web/run.py --base-url http://127.0.0.1:8080 --results ./web-e2e-local
```

This reaches the deployed application through Kubernetes port forwarding. It does **not** qualify cluster DNS, Ingress routing or Ingress TLS. Loopback is a browser secure context, so WASM integrity works without an HTTP certificate here.

### Job against your HTTPS Ingress

Configure `examples/web/m6/k8s/ingress.yaml` with your ingress class, real hostname and TLS Secret, then apply it. The hostname must resolve to the Ingress **from the runner Pod**, and its certificate must already be trusted by the browser image. Publicly trusted certificates work with the normal image trust store. For a private local CA, provision browser/system trust correctly in your image; this runner has no `ignore_https_errors` switch. A mounted CA file alone is not automatically a browser trust-store installation.

The example annotations disable response buffering for ingress-nginx SSE. For another controller configure its equivalent; the changing-notice check will fail if events are buffered beyond the test timeout. Use root hosting: path-prefix rewrites are not supported by this demo.

```sh
kubectl -n nexo-e2e apply -f examples/web/m6/k8s/ingress.yaml
BASE_URL=https://YOUR_REAL_HOST NAMESPACE=nexo-e2e \
  RESULTS_DIR=./web-e2e-results bash tests/e2e/web/run-job.sh
```

Optional variables: `BROWSER=webkit`, `E2E_IMAGE=your-registry/runner:webkit`, `NAMESPACE`, `RESULTS_DIR`. The namespace must exist and the image must be loaded/pullable. For special local DNS routing you can edit the Job template's `hostAliases`; keep the URL hostname matching the certificate. NetworkPolicies must allow DNS and traffic to the target.

An HTTP Service URL such as `http://nexo-m6:8080` can reach SSR/SSE but is **not** a secure context. The required WASM integrity check then fails. Use trusted HTTPS for a full in-cluster run, or the loopback port-forward method locally; do not suppress the failing check with insecure browser flags.

## Results and Job lifecycle

The helper creates a uniquely named Job, waits for readiness/results, copies artifacts while the container is alive, acknowledges collection and returns 0 only for five passing checks. The runner waits up to 300 seconds for collection; the Job has a 600-second deadline and no retries. If no helper collects results, the runner eventually exits with its real test result. Jobs are retained for an hour by the TTL controller. The helper does not delete existing Jobs or deployments.

Artifacts are stored in `RESULTS_DIR/JOB_NAME/`: `report.json`, `junit.xml`, `runner.log`, the Job manifest, and failure-only screenshots (`.png`) and Playwright traces (`.zip`). Reports record the target, engine/version, runner hash and TLS verification policy. Readiness failures retain Pod diagnostics. Treat traces/screenshots as application data; the supplied target is deliberately public.

Open a failed trace locally with:

```sh
python -m playwright show-trace web-e2e-results/JOB_NAME/wasm_worker.zip
```

A failed Job is useful evidence. Inspect `runner_error`, failed cases, Pod events and Ingress logs before changing limits or trust. Do not mark an unavailable browser or untrusted certificate as a successful E2E run.
