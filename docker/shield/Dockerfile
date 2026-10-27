# syntax=docker/dockerfile:1
# Builds the selected checkout; never clones a mutable remote branch.
ARG RUST_IMAGE=rust:1.99.1-trixie@sha256:931c2c0531a8b143e044f0f3914dfe802f15bb3d1955f0b657e9359327ac2ffc
ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
FROM ${RUST_IMAGE} AS toolchain
FROM toolchain AS build
WORKDIR /src
COPY compiler/ compiler/
COPY nexo/stdlib/api.json nexo/stdlib/infra_providers.json nexo/stdlib/
RUN --mount=type=secret,id=build_ca,mode=0444 \
    if [ -s /run/secrets/build_ca ]; then export CARGO_HTTP_CAINFO=/run/secrets/build_ca; fi; \
    cargo build --manifest-path compiler/Cargo.toml --locked --release --workspace -j 2

FROM ${DEBIAN_IMAGE} AS runtime
# Source compilation happens in the container; the worker then applies its own
# limits/profile when explicitly requested. No Rust toolchain in this image.
COPY --from=toolchain --chmod=0444 /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN --mount=type=secret,id=build_ca,mode=0444 \
    set -eu; ca=/etc/ssl/certs/ca-certificates.crt; \
    if [ -s /run/secrets/build_ca ]; then ca=/run/secrets/build_ca; fi; \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    apt-get -o Acquire::https::CaInfo="$ca" -o APT::Update::Error-Mode=any update \
 && apt-get -o Acquire::https::CaInfo="$ca" install -y --no-install-recommends clang gcc libc6-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 shield \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin shield \
 && mkdir -p /out /usr/local/lib/nexo \
 && chown 10001:10001 /out
COPY --from=build --chmod=0555 /src/compiler/target/release/nexo /src/compiler/target/release/nexo-shield-worker /usr/local/bin/
COPY --from=build --chmod=0444 /src/compiler/target/release/libnexo_runtime.a /usr/local/lib/nexo/libnexo_runtime.a
COPY --chmod=0555 docker/shield/entrypoint.sh /usr/local/bin/shield
COPY --chmod=0555 docker/shield/example/ /app/
ENV NEXO_RUNTIME=/usr/local/lib/nexo/libnexo_runtime.a HOME=/nonexistent TMPDIR=/tmp LANG=C.UTF-8
LABEL org.opencontainers.image.title="Nexo Shield" \
      org.opencontainers.image.source="https://github.com/marcrabadan/nexo"
WORKDIR /app
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/shield"]
CMD ["run", "/app/main.nexo"]


# Export only freshly built test harnesses, not another Rust development image.
FROM build AS test-binaries
RUN cargo test --manifest-path compiler/Cargo.toml --offline --locked --release --workspace --no-run -j 2 2>/tmp/test-executables.log \
 && mkdir /test-executables \
 && sed -n 's/^  Executable .* (\(.*\))$/\1/p' /tmp/test-executables.log > /tmp/test-executables.list \
 && test -s /tmp/test-executables.list \
 && while IFS= read -r binary; do cp "$binary" /test-executables/; done < /tmp/test-executables.list

FROM scratch AS validation-source
COPY compiler/ /compiler/
COPY nexo/ /nexo/
COPY tests/ /tests/
COPY pyproject.toml /pyproject.toml

# This disposable test image uses the same production compiler and worker.
# No Docker socket, Rust installation, mutable source tree or cargo cache.
FROM runtime AS validation
USER root
RUN --mount=type=secret,id=build_ca,mode=0444 \
    set -eu; ca=/etc/ssl/certs/ca-certificates.crt; \
    if [ -s /run/secrets/build_ca ]; then ca=/run/secrets/build_ca; fi; \
    apt-get -o Acquire::https::CaInfo="$ca" -o APT::Update::Error-Mode=any update \
 && apt-get -o Acquire::https::CaInfo="$ca" install -y --no-install-recommends python3 python3-venv python3-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY --from=validation-source --chown=10001:10001 / /workspace/
RUN --mount=type=secret,id=build_ca,mode=0444 \
    set -eu; if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    python3 -m venv /opt/nexo-venv \
 && /opt/nexo-venv/bin/python -m pip install --no-cache-dir -e '.[dev]'
COPY --from=test-binaries --chmod=0555 /test-executables/ /opt/nexo-tests/
COPY --chmod=0555 docker/shield/validate.sh /usr/local/bin/shield-validate
ENV PATH=/opt/nexo-venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    NEXO_PYTHON=/opt/nexo-venv/bin/python PYTHONDONTWRITEBYTECODE=1 \
    NEXO_COMPILER=/usr/local/bin/nexo NEXO_TEST_COMPILER=/usr/local/bin/nexo
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/shield-validate"]
CMD []
