# syntax=docker/dockerfile:1
#
# Multi-arch: python:3.12-slim-bookworm is published for linux/amd64, linux/arm64 and linux/arm/v7.
# Wheels are built in a throw-away stage so that platforms without prebuilt wheels
# (e.g. 32-bit Raspberry Pi OS) work too, without shipping a compiler in the final image.

FROM python:3.12-slim-bookworm AS builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    DATABASE_PATH=/data/bot.sqlite3 \
    HEARTBEAT_FILE=/tmp/heartbeat

COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/* && rm -rf /wheels

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin bot \
 && mkdir -p /data && chown bot:bot /data
WORKDIR /app
COPY src/ ./src/
USER bot
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
  CMD ["python", "-m", "statustw_bot.healthcheck"]

STOPSIGNAL SIGTERM
CMD ["python", "-m", "statustw_bot"]
