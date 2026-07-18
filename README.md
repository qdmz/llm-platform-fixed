# LLM Platform Fixed

一个单文件 Flask 实现的商业化 LLM 网关 / NewAPI 风格平台，已集成用户系统、套餐额度、API Key、OpenAI 兼容接口、Ollama 本地模型、第三方模型中转、易支付、工单、发票、SMTP 邮箱激活等能力。

线上示例域名：`https://newapi.ypvps.com`

## 功能特性

- OpenAI 兼容接口：
  - `GET /v1/models`
  - `POST /v1/chat/completions`
- 用户系统：注册、登录、控制台、API Key 管理。
- 邮箱激活：注册必须填写邮箱，发送激活邮件，点击 `/activate?token=...` 后激活账号。
- SMTP 后台配置：支持 Host、端口、TLS/SSL、账号、授权码、发件邮箱、测试邮件。
- 第三方模型中转：支持 OpenAI-compatible Base URL + API Key。
- 自动读取模型：后台可通过供应商 `/v1/models` 自动批量导入模型 ID。
- 自动故障切换：`model` 为空或填写 `auto` 时，按后台优先级自动尝试可用模型；指定模型失败时也会继续 fallback 到其它启用模型。
- 本地 Ollama：支持本地 `qwen2.5-coder-14b-ms:latest` 等模型。
- 商业化能力：套餐、余额、订单、易支付回调、发票、工单。
- SQLite 持久化，部署简单。

## 项目结构

```text
.
├── gateway.py              # Flask 主程序，包含页面、API、数据库初始化和业务逻辑
├── requirements.txt        # Python 依赖
├── deploy-safe.sh          # 生产部署脚本，部署到 /opt/llm-platform
├── Dockerfile              # Docker 生产镜像
├── docker-compose.yml      # Docker Compose 部署
├── docker-compose.ollama.yml # 可选 Ollama sidecar
├── DOCKER.md               # Docker 部署说明
├── pull-modelscope.sh      # 本地模型拉取辅助脚本
├── .env.example            # 环境变量示例，禁止提交真实 .env
├── DEPLOY.md               # 部署说明
└── README.md               # 项目说明
```

## 快速本地运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
export LLM_PLATFORM_HOME="$PWD/.runtime"
export FLASK_SECRET_KEY="dev-secret"
export ADMIN_PASSWORD="admin123"
python gateway.py
```

默认监听：`http://127.0.0.1:5088`

默认管理员：

- 用户名：`admin`
- 密码：由 `ADMIN_PASSWORD` 指定

## Docker 快速运行

```bash
cp .env.docker.example .env
# 编辑 DOMAIN / PUBLIC_BASE_URL / ADMIN_PASSWORD / FLASK_SECRET_KEY
docker compose up -d --build
curl http://127.0.0.1:5088/health
```

更多说明见 [DOCKER.md](./DOCKER.md)。

## 生产部署

详见 [DEPLOY.md](./DEPLOY.md)。

核心命令：

```bash
mkdir -p /tmp/llm-platform-fixed
cp gateway.py requirements.txt deploy-safe.sh /tmp/llm-platform-fixed/
PUBLIC_BASE_URL=https://newapi.example.com \
ADMIN_PASSWORD='change-me' \
bash /tmp/llm-platform-fixed/deploy-safe.sh
```

部署后服务名：`llm-platform-demo`

常用检查：

```bash
systemctl status llm-platform-demo --no-pager
curl http://127.0.0.1:5088/health
curl http://127.0.0.1:5088/v1/models
```

## OpenAI 兼容调用示例

```bash
curl https://newapi.example.com/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen2.5-coder-14b-ms:latest",
    "messages": [{"role":"user","content":"Say OK"}]
  }'
```

## 管理后台

访问：`/admin`

后台可配置：

- 易支付：`/payment/notify`、`/payment/success`
- SMTP 邮件
- 第三方 OpenAI 兼容模型
- 本地 Ollama 模型
- 套餐、用户、工单、发票

## 安全注意事项

- 不要提交真实 `.env`、数据库、日志、模型文件。
- 首次部署必须修改 `ADMIN_PASSWORD` 和 `FLASK_SECRET_KEY`。
- SMTP 授权码、第三方 API Key、易支付密钥只在后台或服务器 `.env` 配置。
- QQ 邮箱 `smtp.qq.com:465` 使用 SSL；程序已对 465 端口自动按 SSL 处理。
