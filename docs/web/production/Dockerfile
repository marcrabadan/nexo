# Pinned upstream image; update and scan deliberately before real deployment.
FROM nginxinc/nginx-unprivileged@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03
USER 0:0
# Signed Alpine fixes absent from the pinned upstream image (2026-11-02).
# Optional session CA is a BuildKit secret, never persisted in a layer.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    apk add --no-cache brotli-libs=1.2.1-r0 curl=8.23.0-r0 libcurl=8.23.0-r0 \
        musl=1.2.6-r3 musl-utils=1.2.6-r3 zlib=1.3.2-r1
COPY --chmod=0444 nginx.conf /etc/nginx/nginx.conf
COPY dist/ /srv/nexo/
RUN find /srv/nexo -type d -exec chmod 0555 {} +
RUN find /srv/nexo -type f -exec chmod 0444 {} +
USER 101:101
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=2s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
