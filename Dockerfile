# syntax=docker/dockerfile:1
# Slim Python image. No compilers, no package index left in the layers that
# matter, and the process runs as uid 65532.

FROM python:3.13.18-slim-bookworm

ARG VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CAPLOG_VERSION=${VERSION} \
    CAPLOG_LISTEN=:8080 \
    CAPLOG_CHECKPOINTS=/checkpoints/checkpoints.jsonl \
    HOME=/tmp

RUN groupadd --gid 65532 caplog \
    && useradd --uid 65532 --gid 65532 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin caplog \
    && mkdir -p /checkpoints /certs /backups \
    && chown 65532:65532 /checkpoints /certs /backups

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /root/.cache
COPY caplog /app/caplog
COPY static /app/static
RUN printf '%s\n' '#!/bin/sh' 'exec python -m caplog "$@"' > /usr/local/bin/caplog \
    && chmod 755 /usr/local/bin/caplog

USER 65532:65532
EXPOSE 8080 8443
VOLUME ["/checkpoints", "/certs", "/backups"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["caplog", "healthcheck"]
ENTRYPOINT ["caplog"]
CMD ["serve"]
