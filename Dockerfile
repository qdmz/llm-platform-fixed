# LLM Platform Fixed - production image
# Build:  docker build -t llm-platform-fixed .
# Run:    docker compose up -d

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LLM_PLATFORM_HOME=/opt/llm-platform \
    PORT=5088 \
    GUNICORN_WORKERS=2 \
    GUNICORN_TIMEOUT=300

WORKDIR /opt/llm-platform

# System deps (curl for healthcheck; ca-certificates for HTTPS upstreams)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip wheel \
    && pip install -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# Application files
COPY gateway.py wsgi.py ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /opt/llm-platform/data /opt/llm-platform/logs \
    && useradd --create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /opt/llm-platform

EXPOSE 5088

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null || exit 1

# Start as root so entrypoint can chown mounted volumes, then drop to app user.
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["gunicorn"]
