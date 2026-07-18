#!/bin/sh
set -eu

HOME_DIR="${LLM_PLATFORM_HOME:-/opt/llm-platform}"
PORT_VALUE="${PORT:-5088}"
WORKERS="${GUNICORN_WORKERS:-2}"
TIMEOUT="${GUNICORN_TIMEOUT:-300}"

mkdir -p "$HOME_DIR/data" "$HOME_DIR/logs"

# Best-effort: if container is started as root (compose override), fix ownership.
if [ "$(id -u)" = "0" ]; then
  chown -R app:app "$HOME_DIR/data" "$HOME_DIR/logs" 2>/dev/null || true
  if [ "$#" -eq 0 ] || [ "$1" = "gunicorn" ]; then
    exec su -s /bin/sh app -c "cd '$HOME_DIR' && exec gunicorn -w '$WORKERS' --timeout '$TIMEOUT' -b '0.0.0.0:$PORT_VALUE' wsgi:app"
  fi
  exec "$@"
fi

if [ "$#" -eq 0 ] || [ "$1" = "gunicorn" ]; then
  cd "$HOME_DIR"
  exec gunicorn -w "$WORKERS" --timeout "$TIMEOUT" -b "0.0.0.0:$PORT_VALUE" wsgi:app
fi

exec "$@"
