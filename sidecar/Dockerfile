ARG PYTHON_IMAGE=python:3.13-alpine

FROM ${PYTHON_IMAGE}

RUN apk add --no-cache ffmpeg tzdata tini
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev && mkdir -p /cache
COPY sidecar.py test_sidecar.py ./

RUN addgroup -g 1000 -S sidecar && adduser -u 1000 -S -D -G sidecar sidecar && \
    chown -R sidecar:sidecar /app /cache

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 CACHE_DIR=/cache PORT=8090
USER sidecar
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -T 4 -O /dev/null "http://127.0.0.1:${PORT:-8090}/healthz"

ENTRYPOINT ["/sbin/tini", "-s", "--"]
CMD ["python", "sidecar.py"]

LABEL org.opencontainers.image.title="espaudiobook sidecar" \
      org.opencontainers.image.description="BookOrbit proxy + ffmpeg 48kHz mono CBR MP3 cache for the ESP32-S3 player" \
      org.opencontainers.image.source="https://github.com/ajurna/espaudiobook"
