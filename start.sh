#!/bin/sh
# Single, quote-free start command for sandbox / PaaS hosts.
#
# Use it as the app's start command (or through the Procfile) when the host
# cannot be told "this is a Flask app served by gunicorn":
#
#     sh start.sh
#
# It resolves a writable runtime dir, binds 0.0.0.0 and honours $PORT
# (falling back to 3000, the port most sandboxes probe).

set -eu

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$SRC_DIR"

# 1) writable runtime home (the default /opt/llm-platform is read-only in sandboxes)
if [ -z "${LLM_PLATFORM_HOME:-}" ] || ! mkdir -p "${LLM_PLATFORM_HOME}/data" 2>/dev/null; then
  LLM_PLATFORM_HOME="$SRC_DIR/runtime"
fi
mkdir -p "$LLM_PLATFORM_HOME/data" "$LLM_PLATFORM_HOME/logs"
export LLM_PLATFORM_HOME

# 2) port: sandboxes inject PORT; 3000 is their usual probe target
if [ -z "${PORT:-}" ]; then
  PORT=3000
fi
export PORT

echo "[start.sh] LLM_PLATFORM_HOME=$LLM_PLATFORM_HOME PORT=$PORT"

if command -v gunicorn >/dev/null 2>&1; then
  exec gunicorn \
    --workers "${GUNICORN_WORKERS:-2}" \
    --timeout "${GUNICORN_TIMEOUT:-300}" \
    --bind "0.0.0.0:${PORT}" \
    --access-logfile - \
    wsgi:app
fi

exec python main.py
