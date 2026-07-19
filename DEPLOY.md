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
- 如已有 `/opt/llm-platform/data/platform.db`，先自动备份为 `platform.db.时间.bak`
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

脚本不会覆盖已有 `/opt/llm-platform/.env` 和 `/opt/llm-platform/data/platform.db`，并会在升级前自动备份现有 SQLite 数据库。

升级前建议备份数据库：

```bash
cp /opt/llm-platform/data/platform.db /opt/llm-platform/data/platform.db.$(date +%F-%H%M%S).bak
```

## 13. 自动故障切换模式

`/v1/chat/completions` 支持自动模式：

```json
{
  "model": "auto",
  "messages": [{"role":"user","content":"Say OK"}]
}
```

行为规则：

- `model` 为空或 `auto`：按“第三方优先、本地 Ollama 最后兜底”的顺序依次尝试。
- 指定具体模型：先尝试指定模型，失败后继续尝试其它启用模型。
- 自动模式优先级：第三方/OpenAI 兼容供应商优先，本地 Ollama 最后兜底；每组内部按 `is_default DESC, sort_order ASC, id ASC`。
- 故障切换触发条件：连接错误、请求超时、上游非 2xx、Ollama `not found`、JSON/网关异常等。
- `stream=true` 不做多模型重试，因为流式响应一旦开始发送，就无法安全切换到另一个模型。
- 所有模型都失败时返回：

```json
{
  "error": {
    "message": "All model providers failed",
    "type": "gateway_error",
    "fallback_errors": []
  }
}
```

建议后台将稳定的第三方模型设为默认；本地 Ollama 无论排序如何都会作为最后兜底，避免慢速本地模型抢先响应。


## 14. 多协议 / 多模态模型配置

本版本支持把不同上游协议统一中转为 OpenAI 兼容出口。已开放：

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`

后台“模型配置管理”新增字段：

- 协议类型：
  - `OpenAI Chat`：上游路径 `{Base URL}/chat/completions`
  - `OpenAI Responses`：上游路径 `{Base URL}/responses`
  - `Anthropic Messages`：上游路径 `{Base URL}/messages`
- 能力：文本 / 图片 / 视频 / 音频
- `stream` 支持
- `tools` 支持
- 输入/输出 token 上限
- `extra_config`：JSON 扩展配置，例如 Anthropic 版本号

配置示例：

```json
{"anthropic_version":"2023-06-01"}
```

中转行为：

- 调用 `/v1/chat/completions` 时，如果选中的上游是 `responses`，会自动把 `messages` 转为 `input`，再把上游 Responses 结果包装成 Chat Completions 响应。
- 调用 `/v1/responses` 时，如果选中的上游是 Chat Completions，会自动把 `input` 转为 `messages`，再把上游 Chat 结果包装成 Responses 响应。
- 调用 `/v1/messages` 时，只选择 `Anthropic Messages` 类型模型。
- 请求包含图片/视频/音频/tools/stream 时，会先按后台能力配置过滤模型；不支持则提前返回 `unsupported_modality` / capability error，不盲目转发。
- `stream=true` 仍只使用第一个匹配模型，不做中途故障切换。

图片请求示例：

```bash
curl https://newapi.example.com/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "auto",
    "messages": [{
      "role": "user",
      "content": [
        {"type":"text","text":"描述这张图片"},
        {"type":"image_url","image_url":{"url":"https://example.com/a.jpg"}}
      ]
    }]
  }'
```

Responses 请求示例：

```bash
curl https://newapi.example.com/v1/responses \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","input":"Say OK"}'
```

Anthropic Messages 请求示例：

```bash
curl https://newapi.example.com/v1/messages \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-3-5-sonnet","messages":[{"role":"user","content":"Say OK"}],"max_tokens":256}'
```




### 后台说明与模型能力识别

部署后进入管理后台，可以在“后台使用说明 / OpenAI 兼容接口”区域查看：

- OpenAI 兼容 Base URL
- `/v1/models`
- `/v1/chat/completions`
- `/v1/responses`
- `/v1/messages`
- 文本和图片请求 curl 示例

在“模型配置管理”区域可以点击“一键重新识别全部模型多模态能力”。该操作不会发起对话请求、不消耗模型额度，只根据 `/v1/models` 元数据和模型名规则推断图片/视频/音频能力。识别后建议人工抽查重点模型，尤其是第三方聚合商自定义命名的模型。


## Docker 部署

项目已提供 Docker 版本：

```bash
cp .env.docker.example .env
docker compose up -d --build
curl http://127.0.0.1:5088/health
```

如需 Docker 同时启动 Ollama：

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d --build
```

完整说明见 [DOCKER.md](./DOCKER.md)。
