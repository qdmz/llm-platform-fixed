#!/bin/bash
set -euo pipefail
APP_DIR=/opt/llm-platform
PORT=${PORT:-5088}
MODEL_NAME=${MODEL_NAME:-qwen2.5-coder-14b-ms:latest}
PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-https://newapi.ypvps.com}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-admin123}
mkdir -p "$APP_DIR"/{gateway,data,logs}
if [ -f "$APP_DIR/data/platform.db" ]; then
  cp "$APP_DIR/data/platform.db" "$APP_DIR/data/platform.db.$(date +%F-%H%M%S).bak"
fi
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -U pip wheel
"$APP_DIR/venv/bin/pip" install -r /tmp/llm-platform-fixed/requirements.txt
cp /tmp/llm-platform-fixed/gateway.py "$APP_DIR/gateway/gateway.py"
cat > "$APP_DIR/gateway/__init__.py" <<'EOF'
from .gateway import app, init_db
init_db()
EOF
if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<EOF
LLM_PLATFORM_HOME=$APP_DIR
PORT=$PORT
FLASK_SECRET_KEY=$(openssl rand -hex 32)
ADMIN_PASSWORD=$ADMIN_PASSWORD
MODEL_NAME=$MODEL_NAME
OLLAMA_BASE_URL=http://127.0.0.1:11434
DOMAIN=newapi.ypvps.com
PUBLIC_BASE_URL=$PUBLIC_BASE_URL
EPAY_API_URL=
EPAY_PID=
EPAY_KEY=
EOF
else
  grep -q '^PORT=' "$APP_DIR/.env" || echo "PORT=$PORT" >> "$APP_DIR/.env"
  grep -q '^PUBLIC_BASE_URL=' "$APP_DIR/.env" || echo "PUBLIC_BASE_URL=$PUBLIC_BASE_URL" >> "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"
cat > /etc/systemd/system/llm-platform-demo.service <<EOF
[Unit]
Description=Commercial LLM Platform Demo Safe Port
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/gunicorn -w 2 --timeout 300 -b 0.0.0.0:$PORT gateway.__init__:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable llm-platform-demo >/dev/null
systemctl restart llm-platform-demo
sleep 2
systemctl --no-pager --full status llm-platform-demo | sed -n '1,18p'
