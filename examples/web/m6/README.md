# Experimental M6 integration

From the repository root:

```sh
python examples/web/m6/server.py
```

Open http://127.0.0.1:4174/. The trusted async backend supplies the initial title and products; hydration reuses that markup. Increment a row, reorder it and verify its local count survives while the shared context increments. Hiding/remounting resets row state. A verified import-free WASM Worker computes 42. Public SSE snapshots update the notice. Closing the page disposes owned resources. Stop with Ctrl-C.

Public static offline demonstration (separate from request-time SSR):

```sh
python -m nexo web build examples/web/ssg/app.nexo --static --routes examples/web/ssg/routes.json --pwa -o /tmp/nexo-m6-pwa
python -m nexo web preview /tmp/nexo-m6-pwa --port 4173
```

Visit once online, wait for service-worker activation and reload; generated routes then work offline. Use HTTPS or loopback and root hosting. The cache contains only explicitly generated public files; never use this build for personalized state.

These are experimental interfaces, not production hosting or authentication. Reference contracts: RFC-0007 through RFC-0011; verification: docs/web/validation-m6.md. Browser source load/mutation resources are not executed by SSR; server data comes from the explicit trusted Python loader.

## Kubernetes E2E

A public Gunicorn Deployment/Service example and a Playwright Job are available in `examples/web/m6/k8s/` and `tests/e2e/web/`. See `tests/e2e/web/README.md` for image builds, port forwarding, HTTPS Ingress requirements and artifact collection. The runner targets the integrated demo contract; it does not start a local server or mutate a deployment.
