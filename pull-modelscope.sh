#!/bin/bash
set -euo pipefail
pkill -f "ollama pull qwen2.5-coder:1.5b" 2>/dev/null || true
pkill -f "llm-platform-pull-model.sh" 2>/dev/null || true
mkdir -p /opt/llm-platform/models /opt/llm-platform/logs
cat > /usr/local/bin/llm-platform-pull-modelscope.sh <<'SH'
#!/bin/bash
set -euo pipefail
LOG=/opt/llm-platform/logs/modelscope-pull.log
MODEL_FILE=/opt/llm-platform/models/Qwen2.5-Coder-1.5B-Instruct-Q4_K_M.gguf
MODEL_NAME=qwen2.5-coder-1.5b-ms
URL="https://modelscope.cn/models/unsloth/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/master/Qwen2.5-Coder-1.5B-Instruct-Q4_K_M.gguf"
{
  echo "==== $(date) start download from ModelScope ===="
  curl -L -C - --retry 20 --retry-delay 5 --connect-timeout 20 -o "$MODEL_FILE" "$URL"
  echo "==== $(date) create ollama model ===="
  cat > /opt/llm-platform/models/Modelfile.qwen25coder15b <<EOF
FROM $MODEL_FILE
PARAMETER num_ctx 4096
PARAMETER num_thread 8
EOF
  systemctl enable ollama >/dev/null 2>&1 || true
  systemctl restart ollama || true
  sleep 3
  ollama create "$MODEL_NAME" -f /opt/llm-platform/models/Modelfile.qwen25coder15b
  if grep -q "^MODEL_NAME=" /opt/llm-platform/.env; then
    sed -i "s/^MODEL_NAME=.*/MODEL_NAME=$MODEL_NAME/" /opt/llm-platform/.env
  else
    echo "MODEL_NAME=$MODEL_NAME" >> /opt/llm-platform/.env
  fi
  systemctl restart llm-platform-demo
  echo "==== $(date) done: $MODEL_NAME ===="
} >> "$LOG" 2>&1
SH
chmod +x /usr/local/bin/llm-platform-pull-modelscope.sh
: > /opt/llm-platform/logs/modelscope-pull.log
nohup /usr/local/bin/llm-platform-pull-modelscope.sh >/opt/llm-platform/logs/modelscope-pull.nohup 2>&1 & echo $! > /opt/llm-platform/logs/modelscope-pull.pid
sleep 3
echo PID=$(cat /opt/llm-platform/logs/modelscope-pull.pid)
pgrep -af "modelscope-pull|curl -L -C" || true
tail -n 20 /opt/llm-platform/logs/modelscope-pull.log || true
