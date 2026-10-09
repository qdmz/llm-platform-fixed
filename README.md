# LLM Platform Fixed

一个单文件 Flask 实现的商业化 LLM 网关 / NewAPI 风格平台，已集成用户系统、套餐额度、API Key、OpenAI 兼容接口、Ollama 本地模型、第三方模型中转、易支付、工单、发票、SMTP 邮箱激活等能力。

线上示例域名：`https://newapi.ypvps.com`

## 功能特性

- **多协议 OpenAI 兼容网关**：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages`（Anthropic），上游可以是 OpenAI Chat / OpenAI Responses / Anthropic Messages / Ollama，网关在协议之间自动转换请求和响应（含流式 SSE）。
- **auto 智能选路**：`model` 为空或 `auto` 时按实测速度选最快的可用模型，失败、超时、空回复自动换下一个候选；流式请求在首字节前同样可重试。
- **测速与自动启停**：后台一键测速，不可用的模型临时停用、测通后自动恢复。
- **模型调用权限**：用户 / Key 级白名单（`up:上游名`、`m:模型id`、通配 `*`）。
- **用户系统**：注册、邮箱激活、登录、控制台、API Key 创建 / 复制 / 停用 / 重新生成 / 删除。
- **商业化**：套餐、每日 token 额度、限速、订单、易支付回调、工单、发票。
- **管理后台**：概览仪表盘（用户、调用量、收入、热门模型）+ 分页签管理模型、测速、用户与 Key、套餐与订单、工单、系统设置。
- **响应式卡片界面**：桌面 / 平板 / 手机自适应，深色模式，代码块一键复制，危险操作二次确认。
- **安全加固**：CSRF 防护、登录失败限流、会话密钥持久化、支付回调金额校验与幂等。
- SQLite 持久化，单文件 Flask，部署简单；支持 systemd、Docker、各类 PaaS。

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
├── README.md               # 项目说明
├── CHANGELOG.md            # 版本变更记录
├── VERSION                 # 当前版本号
└── scripts/release.sh      # 自动递增版本号、打 tag 并推送
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

## 新增环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SESSION_COOKIE_SECURE` | `0` | 设为 `1` 时会话 Cookie 只走 HTTPS |
| `LOGIN_MAX_FAILS` | `8` | 同一 IP 10 分钟内允许的登录失败次数 |
| `FLASK_SECRET_KEY` | 自动生成并持久化 | 会话签名密钥 |

## 版本发布

```bash
# 自动把 VERSION 的补丁号 +1，更新 CHANGELOG 标题、提交、打 tag 并推送
bash scripts/release.sh            # 1.0.0 -> 1.0.1
bash scripts/release.sh minor      # 1.0.1 -> 1.1.0
bash scripts/release.sh major      # 1.1.0 -> 2.0.0
```

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

## 页面一览

| 路径 | 说明 |
| --- | --- |
| `/` | 首页：模型广场、套餐价格、快速接入 |
| `/register` `/login` | 注册 / 登录（登录失败 10 分钟内超过 `LOGIN_MAX_FAILS` 次会被临时限制） |
| `/dashboard` | 用户控制台：套餐与额度、近 7 天用量、最近调用、API Key、订单、工单 |
| `/playground` | 在线聊天测试，`Ctrl/⌘ + Enter` 发送 |
| `/admin` | 管理后台（概览 / 模型与上游 / 测速 / 用户与 Key / 套餐与订单 / 工单 / 系统设置 / 接入文档） |
| `/health` `/healthz` `/envz` | 健康检查与环境自检 |

## 管理后台

访问：`/admin`

- **概览**：用户数、启用模型、今日调用与 tokens、累计收入、近 14 天调用趋势、热门模型。
- **模型与上游**：按上游批量换 Key / Base URL / 启停 / 重命名；通过 `/v1/models` 自动导入；多模态能力识别。
- **测速 / 智能选路**：一键测速、查看最快模型和连续失败模型、恢复自动停用。
- **用户与 Key**：套餐、额度、限速、模型权限、IP 白名单。
- **套餐与订单**：套餐 CRUD、订单管理、易支付配置（回调 `/payment/notify`，跳转 `/payment/success`）。
- **工单**、**系统设置**（SMTP、项目、用量日志）、**接入文档**。

使用默认密码 `admin123` 登录时，后台顶部会显示安全提醒。

## 安全注意事项

- 不要提交真实 `.env`、数据库、日志、模型文件。
- 首次部署必须修改 `ADMIN_PASSWORD`；`FLASK_SECRET_KEY` 未设置时会自动生成并保存到 `data/.flask_secret_key`，重启和多 worker 下登录态保持不变。
- 所有页面表单带 CSRF 校验；`/v1/*` API 与支付回调不受影响。
- 全站 HTTPS 时建议设置 `SESSION_COOKIE_SECURE=1`。
- 易支付回调会校验签名（常量时间比较）和金额，重复回调幂等处理，不会重复延长套餐。
- SMTP 授权码、第三方 API Key、易支付密钥只在后台或服务器 `.env` 配置。
- QQ 邮箱 `smtp.qq.com:465` 使用 SSL；程序已对 465 端口自动按 SSL 处理。


## 多协议 / 多模态中转

本项目对外保持 OpenAI 兼容，当前支持：

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`

后台模型配置可为每个上游设置协议类型（OpenAI Chat / OpenAI Responses / Anthropic Messages）、模态能力（文本、图片、视频、音频）、stream/tools 支持以及 token 上限。网关会在调用前按能力过滤模型，并在不同协议之间做基础请求/响应转换。

详细部署和配置见 [DEPLOY.md](./DEPLOY.md)。


### 后台使用说明与模型能力识别

管理后台已内置“后台使用说明 / OpenAI 兼容接口”卡片，说明 Base URL、API Key、`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages` 以及多模态调用示例。

模型配置支持一键重新识别全部模型多模态能力：

- 优先使用 `/v1/models` 返回的模型元数据（如果上游提供）
- 再按常见模型命名规则识别：`vision`、`vl`、`vlm`、`image`、`video`、`audio`、`omni`、`fuyu`、`kosmos`、`neva`、`vila`、`gemini` 等
- 识别结果会写入 `modalities` / `supports_vision` / `supports_video`
- 为避免误路由，`tools` 不按名称批量自动开启，建议按上游真实能力手动勾选
