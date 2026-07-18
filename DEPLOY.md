# 部署说明

本文档以 Ubuntu/Debian 系统为例，部署目录为 `/opt/llm-platform`，systemd 服务名为 `llm-platform-demo`，应用端口默认为 `5088`。

## 1. 服务器依赖

```bash
apt update
apt install -y python3 python3-venv python3-pip curl sqlite3 nginx
```

如需本地模型，安装并启动 Ollama：

```bash
curl -fsSL https://ollama.com/install.sh | sh
systemctl enable --now ollama
curl http://127.0.0.1:11434/api/tags
```

## 2. 上传代码

```bash
mkdir -p /tmp/llm-platform-fixed
cp gateway.py requirements.txt deploy-safe.sh /tmp/llm-platform-fixed/
```

如果从 GitHub 拉取：

```bash
git clone <YOUR_REPO_URL> /tmp/llm-platform-fixed
cd /tmp/llm-platform-fixed
```

## 3. 执行部署

```bash
cd /tmp/llm-platform-fixed
PUBLIC_BASE_URL=https://newapi.example.com \
DOMAIN=newapi.example.com \
PORT=5088 \
ADMIN_PASSWORD='change-me' \
MODEL_NAME='qwen2.5-coder-14b-ms:latest' \
bash deploy-safe.sh
```

脚本会：

- 创建 `/opt/llm-platform/{gateway,data,logs}`
- 创建 Python venv
- 安装 `requirements.txt`
- 复制 `gateway.py`
- 首次部署时生成 `/opt/llm-platform/.env`
- 创建并重启 systemd 服务 `llm-platform-demo`

## 4. 配置环境变量

生产配置文件：

```bash
/opt/llm-platform/.env
```

示例：

```env
LLM_PLATFORM_HOME=/opt/llm-platform
PORT=5088
FLASK_SECRET_KEY=change-me-to-a-long-random-secret
ADMIN_PASSWORD=change-me
MODEL_NAME=qwen2.5-coder-14b-ms:latest
OLLAMA_BASE_URL=http://127.0.0.1:11434
DOMAIN=newapi.example.com
PUBLIC_BASE_URL=https://newapi.example.com
EPAY_API_URL=
EPAY_PID=
EPAY_KEY=
```

修改后重启：

```bash
systemctl restart llm-platform-demo
```

## 5. Nginx 反向代理

示例配置：

```nginx
server {
    listen 80;
    server_name newapi.example.com;

    location / {
        proxy_pass http://127.0.0.1:5088;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
```

启用：

```bash
nginx -t
systemctl reload nginx
```

建议使用 acme.sh 或 certbot 配置 HTTPS。

## 6. 初始化后台

访问：

```text
https://newapi.example.com/admin
```

默认管理员：

- 用户名：`admin`
- 密码：部署时的 `ADMIN_PASSWORD`

后台建议依次配置：

1. `PUBLIC_BASE_URL` / 域名
2. SMTP 邮件
3. 第三方模型供应商
4. 本地 Ollama 模型
5. 易支付参数
6. 套餐和用户额度

## 7. SMTP 配置

以 QQ 邮箱为例：

- SMTP Host：`smtp.qq.com`
- 端口：`465`
- 加密：`SSL`
- 账号：完整邮箱
- 密码：邮箱授权码，不是登录密码
- 发件邮箱：完整邮箱

说明：程序对端口 `465` 自动使用 SSL，即使误选 TLS 也会按 SSL 处理。

配置后点击“发送测试邮件”。返回成功后，用户注册激活邮件即可正常发送。

## 8. 第三方模型自动导入

后台“模型配置管理”中填写：

- 供应商名称
- Base URL，例如：`https://api.example.com/v1`
- API Key

点击：

```text
通过 /v1/models 自动读取并批量导入
```

程序会请求：

```text
GET {Base URL}/models
Authorization: Bearer {API Key}
```

兼容 OpenAI 风格返回：

```json
{
  "data": [
    {"id": "model-a"},
    {"id": "model-b"}
  ]
}
```

## 9. 本地 14B 模型

当前线上验证可用的 Ollama 模型 ID：

```text
qwen2.5-coder-14b-ms:latest
```

检查 Ollama 模型：

```bash
curl http://127.0.0.1:11434/api/tags
```

注意：CPU 跑 14B 首次推理会很慢，可能几十秒到数分钟。生产默认建议使用第三方模型或较小本地模型，14B 作为低频备用。

## 10. API 验证

健康检查：

```bash
curl http://127.0.0.1:5088/health
```

模型列表：

```bash
curl http://127.0.0.1:5088/v1/models
```

聊天接口：

```bash
curl http://127.0.0.1:5088/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen2.5-coder-14b-ms:latest",
    "messages": [{"role":"user","content":"Say OK"}]
  }'
```

支付回调地址：

```text
/payment/notify
/payment/success
```

## 11. 常用运维命令

```bash
systemctl status llm-platform-demo --no-pager
journalctl -u llm-platform-demo -n 200 --no-pager
systemctl restart llm-platform-demo
sqlite3 /opt/llm-platform/data/platform.db '.tables'
```

## 12. 升级部署

```bash
cd /tmp/llm-platform-fixed
git pull
bash deploy-safe.sh
```

脚本不会覆盖已有 `/opt/llm-platform/.env` 和 `/opt/llm-platform/data/platform.db`。

升级前建议备份数据库：

```bash
cp /opt/llm-platform/data/platform.db /opt/llm-platform/data/platform.db.$(date +%F-%H%M%S).bak
```
