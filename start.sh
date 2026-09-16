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

# 3) optional: keep the SQLite database durable across redeploys.
#
#    PandaStack app hosting (and most sandbox PaaS) gives you an ephemeral
#    filesystem, so runtime/data/platform.db is wiped on every deploy.
#    Set these four variables and the DB is continuously replicated to any
#    S3-compatible object store (e.g. Cloudflare R2) and restored on boot:
#
#      LITESTREAM_ENDPOINT          e.g. https://<account>.r2.cloudflarestorage.com
#      LITESTREAM_BUCKET            bucket name
#      LITESTREAM_ACCESS_KEY_ID     R2 API token access key
#      LITESTREAM_SECRET_ACCESS_KEY R2 API token secret
#
#    Everything here is best-effort: if replication setup fails the app still
#    starts with a local database.
DB_FILE="$LLM_PLATFORM_HOME/data/platform.db"
if [ -n "${LITESTREAM_ENDPOINT:-}" ] && [ -n "${LITESTREAM_BUCKET:-}" ] \
   && [ -n "${LITESTREAM_ACCESS_KEY_ID:-}" ] && [ -n "${LITESTREAM_SECRET_ACCESS_KEY:-}" ]; then
  LS_DIR="$SRC_DIR/runtime/bin"
  LS_BIN="$LS_DIR/litestream"
  LS_OK=1
  if [ ! -x "$LS_BIN" ]; then
    case "$(uname -m)" in
      aarch64|arm64) LS_ARCH=arm64 ;;
      *) LS_ARCH=amd64 ;;
    esac
    LS_URL="${LITESTREAM_DOWNLOAD_URL:-https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-${LS_ARCH}.tar.gz}"
    echo "[start.sh] downloading litestream from $LS_URL"
    mkdir -p "$LS_DIR" /tmp/litestream-dl
    if curl --ssl-no-revoke -sSfL "$LS_URL" | tar -xz -C /tmp/litestream-dl \
       && mv /tmp/litestream-dl/litestream "$LS_BIN" \
       && chmod +x "$LS_BIN"; then
      echo "[start.sh] litestream installed at $LS_BIN"
    else
      echo "[start.sh] WARNING: litestream download failed, continuing WITHOUT replication"
      LS_OK=0
    fi
  fi

  if [ "$LS_OK" = "1" ]; then
    # Litestream requires the database to use WAL journal mode.
    PYBIN=python3
    command -v python3 >/dev/null 2>&1 || PYBIN=python
    "$PYBIN" - <<'PYEOF' || echo "[start.sh] WARNING: sqlite WAL setup failed, replication may be unreliable"
import os, sqlite3
path = os.path.join(os.environ["LLM_PLATFORM_HOME"], "data", "platform.db")
con = sqlite3.connect(path)
con.execute("PRAGMA journal_mode=WAL")
con.close()
print("[start.sh] sqlite journal_mode=WAL ok")
PYEOF

    LS_CFG="$LLM_PLATFORM_HOME/litestream.yml"
    cat > "$LS_CFG" <<YEOF
dbs:
  - path: $DB_FILE
    replicas:
      - type: s3
        bucket: $LITESTREAM_BUCKET
        path: ${LITESTREAM_REPLICA_PATH:-llm-platform/platform.db}
        endpoint: $LITESTREAM_ENDPOINT
        access-key-id: $LITESTREAM_ACCESS_KEY_ID
        secret-access-key: $LITESTREAM_SECRET_ACCESS_KEY
        force-path-style: true
        region: ${LITESTREAM_REGION:-auto}
YEOF

    "$LS_BIN" restore -config "$LS_CFG" -if-db-not-exists "$DB_FILE" \
      || echo "[start.sh] litestream restore skipped (no replica yet / first deploy)"
    "$LS_BIN" replicate -config "$LS_CFG" &
    echo "[start.sh] litestream replicate running in background"
  fi
else
  echo "[start.sh] litestream not configured - database is local to this sandbox"
fi

if command -v gunicorn >/dev/null 2>&1; then
  # 单进程 + 多线程：SQLite 对多进程写入很不友好（database is locked），
  # 而且每个 worker 都会重新初始化数据库 / 重新拉取上游模型列表。
  # 要恢复多进程请自行设置 GUNICORN_WORKERS，并确保上游数量少。
  exec gunicorn \
    --workers "${GUNICORN_WORKERS:-1}" \
    --worker-class "${GUNICORN_WORKER_CLASS:-gthread}" \
    --threads "${GUNICORN_THREADS:-8}" \
    --timeout "${GUNICORN_TIMEOUT:-300}" \
    --bind "0.0.0.0:${PORT}" \
    --access-logfile - \
    wsgi:app
fi

exec python main.py
