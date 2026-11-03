# Deployed M6 E2E runner — 2026-11-03

[Instructions and local Kubernetes commands](../../tests/e2e/web/README.md). [Machine-readable evidence](kubernetes-e2e-evidence.json).

The new runner accepts an existing root origin URL. It starts no application server and performs no Kubernetes deployment mutations. Its five independent cases target the integrated public M6 demo: SSR without JavaScript, hydration/keyed lists/local state/context/conditional ownership, actual numeric Worker WASM execution, changing live SSE snapshots, and HTTP CSP/no-store/nosniff/404 behavior. It is a demo integration contract, not a generic test suite for arbitrary applications.

## Delivered artifacts

- `tests/e2e/web/run.py`: required checks, no skips, verified HTTPS policy, JSON/JUnit, failure-only screenshots and traces, bounded artifact collection window and acknowledged exit.
- `tests/e2e/web/Dockerfile`: Python/Playwright 1.62.0, one selected browser/system dependencies, non-root execution; Chromium uses its headless shell to reduce image size.
- `tests/e2e/web/job.yaml` and `run-job.sh`: uniquely named bounded Job, no retries/token/privilege escalation, read-only root, tmp/results/shm volumes, resource limits, Pod-creation/readiness handling, artifact copying and propagated test status. No existing Jobs/deployments are deleted.
- `examples/web/m6/k8s/`: Gunicorn 26.2.0 public fixture image plus Deployment/Service and configurable TLS Ingress. One worker/eight threads and in-process public snapshots remain an example, not distributed or production hosting qualification.

## Executed validation

Both images built successfully. Permissions were normalized so source files copied from a restrictive host umask remain readable by the non-root runtime. Download proxy CA certificates were mounted only for build steps; application certificate verification was not disabled.

The host runner (system Chromium 154.0.8037.57 / Playwright 1.56.0) passed **all five cases** against the running Gunicorn application image over a published loopback port.

The actual runner image (Chromium 151.0.7922.34 / Playwright 1.62.0) then passed **all five cases**, using the Job's environment-variable configuration and a separate application container. Both ran non-root with read-only roots, dropped capabilities and no-new-privileges. The runner had bounded memory/CPU and writable tmp/results/shm only. Containers shared a Docker network namespace for an HTTP loopback secure context; this was not Kubernetes Service/Ingress networking.

Results were extracted using exec/tar from the live results tmpfs, matching the mechanism used by kubectl cp. The acknowledgement marker released the bounded collection wait and the runner exited 0. Docker daemon-side archive copying is not used to qualify mounted tmpfs artifact retrieval.

**Nine runner tests passed**, including rejection of credential/query/path-prefix targets and an intentionally wrong deployment. The failure test verifies exit status 1, five failed cases, JUnit/JSON and five screenshots/traces. The Job, Deployment, Service and Ingress YAML parsed, their selector/TLS/security relationships were checked, and the helper passed `bash -n`.

The environment's small disk and Docker vfs driver caused the original all-browser base image build to run out of space. Unreferenced build layers were reclaimed and the runner was changed to install only the selected browser. The final images and runtime validation succeeded. Temporary browser/pip caches were retained on temporary storage through their original paths; no source or test evidence was discarded.

## Not executed / remaining requirements

No Kubernetes client/cluster is available here. There is no claimed Job scheduling, Kubernetes API schema validation, cluster DNS, Ingress, HTTPS certificate or NetworkPolicy result. The supplied manifests/helper still need to run in the user's local cluster.

For a full in-cluster run, provide a DNS-reachable HTTPS origin whose certificate the browser already trusts. HTTP Service DNS is not a secure context for the required WASM integrity API. Local port forwarding to loopback is an alternative for testing the deployed app, but does not qualify Ingress TLS. No insecure browser certificate override is exposed.

Only the Chromium runner image was built/executed. Firefox/WebKit are selectable by building their own image; runtime sandbox compatibility remains to be established. Public PWA lifecycle tests stay in the strict local compiler gate because they deliberately stop their owned origin; this remote suite does not stop or upgrade the user's deployment.
