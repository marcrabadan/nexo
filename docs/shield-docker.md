# Shield in Docker

Build and run the current checkout on Linux x86_64 with Docker Engine and cgroup v2:

```sh
docker/shield/build.sh runtime
docker/shield/run.sh
docker/shield/run.sh probe
```

The default demo reads `/app/input.txt` and writes `/out/result.txt`. Production
uses the compiled Nexo CLI and worker, without Python or a Rust toolchain.
Base images are pinned by digest; Cargo uses the lockfile. Debian packages are
resolved at build time for security updates, so builds are not byte-identical.
Builds need network access; execution has no external network.

For an existing project and an existing, separate output directory:

```sh
docker/shield/run.sh --project "$PWD/my-project" --output "$PWD/results" run /app/main.nexo
```

The helper maps the current non-root UID/GID (10001 when invoked as root), mounts
sources read-only, and never changes directory ownership. Project and output
paths must not overlap. Without `--output`, results live in a disposable 16 MiB
tmpfs. Bind-mounted output needs its own host disk quota and mount restrictions.
Do not mount secrets or the Docker socket into the container.

The wrapper requires a read-only root, a non-root user, all capabilities dropped,
no-new-privileges, seccomp, network isolation, and finite CPU/memory/process limits
with swap disabled. It refuses execution if these prerequisites are absent.
The default limits are 512 MiB RAM, one CPU and 64 processes. `/tmp` is an
executable 128 MiB tmpfs because Nexo compiles temporary programs there. `/out`
is a non-executable tmpfs by default. The bundled seccomp profile and attribution
are in `docker/shield/SECCOMP.md`.

Docker isolation surrounds Shield's API policy checks. Additional worker-level
Landlock isolation is explicit:

```sh
docker/shield/run.sh run-linux /app/main.nexo
docker/shield/run.sh probe --require-linux
```

This requires the Linux offline profile's kernel facilities, including Landlock
ABI 3 or newer. Unsupported hosts exit 125 before target execution; there is no
fallback. Docker networking allows container loopback, whereas the Linux worker
profile denies sockets. Container filesystem isolation does not itself implement
per-policy filesystem grants. See [the isolation contract](shield-isolation.md).

Compose provides the same hardened demo configuration:

```sh
docker compose -f docker/shield/compose.yaml up --abort-on-container-exit
```

Validation uses the production compiler and worker, prebuilt Rust test harnesses,
and the Python security/policy suites. It needs no Docker socket or mutable Rust
cache. Source stays read-only and temporary test files use tmpfs:

```sh
docker/shield/build.sh validation
docker compose -f docker/shield/compose.yaml run --rm validation
docker compose -f docker/shield/compose.yaml run --rm validation --require-linux
NEXO_SHIELD_DOCKER_IMAGE=nexo-shield:local python -m pytest -q tests/test_shield_docker.py
```

The strict validation command must succeed before claiming the Linux worker is
validated on a deployment host. This development host reports Landlock ABI 0,
including inside Docker: positive Landlock tests remain blocked here. CI requires
actual kernel support and fails when it is unavailable. Current remote results
are tracked in [Shield PR #18](https://github.com/marcrabadan/nexo/pull/18).

For builds behind a trusted TLS proxy, `build.sh` passes standard proxy build
arguments and optionally mounts the public CA bundle named by `SSL_CERT_FILE`
as a temporary BuildKit secret. TLS verification remains enabled. Private keys,
local virtual environments, Cargo targets and environment files are excluded
from the build context.
