# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY nexo ./nexo
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir . gunicorn==26.2.0
COPY examples/web/m6 ./examples/web/m6
RUN chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1000:1000
EXPOSE 8080
CMD ["gunicorn", "--chdir", "examples/web/m6/k8s", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "30", "--graceful-timeout", "5", "--worker-tmp-dir", "/tmp", "app:application"]
