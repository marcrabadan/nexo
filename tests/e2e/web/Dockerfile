# syntax=docker/dockerfile:1
FROM python:3.12-slim
ARG BROWSER=chromium
WORKDIR /runner
COPY tests/e2e/web/run.py /runner/run.py
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 XDG_CACHE_HOME=/tmp/cache
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; printf 'Acquire::https::CaInfo "/run/secrets/proxy_ca";\n' > /tmp/nexo-apt.conf; export APT_CONFIG=/tmp/nexo-apt.conf; fi; \
    python -m pip install --no-cache-dir playwright==1.62.0 && \
    python -m playwright install --with-deps --only-shell "$BROWSER" && \
    chmod -R a+rX /ms-playwright && \
    chmod 0444 /runner/run.py && \
    rm -rf /var/lib/apt/lists/* /tmp/nexo-apt.conf
USER 1000:1000
ENTRYPOINT ["python", "/runner/run.py"]
