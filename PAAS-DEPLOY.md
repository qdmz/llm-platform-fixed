# PaaS / 沙箱部署说明（PandaStack、Zeabur、Sealos、Koyeb 等）

## 为什么之前会部署失败

平台日志：

```
==> detected framework: python (package manager: npm)
$ install          # pip install -r requirements.txt  ✅ 成功
==> starting: uvicorn main:app --host 0.0.0.0 --port $PORT
$ start
started pid 969
sh: 1: Syntax error: Unterminated quoted string
!! deploy failed: app never responded on port 3000
```

三个独立问题叠加：

1. **入口不存在**：本项目是单文件 Flask（WSGI）应用 `gateway.py`，只有给 gunicorn 用的 `wsgi.py`。
   平台自动选用的默认启动命令是 `uvicorn main:app`，但仓库里没有 `main.py`，
   而且 uvicorn 也无法直接托管 WSGI 应用（Flask 对象不是 ASGI 应用）。
2. **依赖缺失**：`requirements.txt` 里没有 `uvicorn`，平台自己启动的 uvicorn 没有被安装。
3. **启动命令被引号截断**：平台把启动命令拼进 shell 字符串时出现引号不闭合
   （`Unterminated quoted string`），进程立刻退出，于是 3000 端口永远没人监听。
   另外 `LLM_PLATFORM_HOME` 默认 `/opt/llm-platform`，在沙箱里可能不可写，
   而 `gateway.py` 在 **import 时** 就会调用 `init_db()` 建库，不可写会直接崩。

## 修复内容（本仓库新增）

| 文件 | 作用 |
| --- | --- |
| `main.py` | **新增**。补齐平台默认入口 `main:app`：自动挑选可写运行目录、持久化 `FLASK_SECRET_KEY`、把 Flask 包成 ASGI 应用；同时暴露 `wsgi_app` / `application` 供 gunicorn 使用。 |
| `start.sh` | **新增**。一条不含嵌套引号的启动命令：解析可写运行目录 → 绑定 `0.0.0.0:$PORT`（`$PORT` 缺省 3000）→ 优先 gunicorn，没有 gunicorn 时回退 `python main.py`。 |
| `Procfile` | **新增**。`web: sh start.sh`，供读取 Procfile 的平台使用。 |
| `requirements.txt` | 增加 `uvicorn`、`a2wsgi`（WSGI→ASGI 桥，纯 Python，无编译依赖）。 |
| `.gitignore` | 忽略 `runtime/`、`secret.key`。 |

`main.py` 的 ASGI 桥按 `a2wsgi → asgiref → 内置 stdlib 线程桥` 顺序自动选择，
可用 `ASGI_BRIDGE=builtin` 强制使用内置实现（零额外依赖，SSE 流式仍然可用）。

原有的 Docker / systemd / `deploy-safe.sh` 部署方式**完全不受影响**（新增文件是纯增量的）。

## 部署步骤

### 方案一（推荐）：在平台里指定启动命令

平台的应用设置里，把 Build / Start 命令改成：

```
Build: pip install -r requirements.txt
Start: sh start.sh
```

端口填 `3000`（或平台注入的 `$PORT`，`start.sh` 会自动使用）。
注意不要把命令写成带引号的形式，例如 `uvicorn main:app --host 0.0.0.0 --port "$PORT"`，
部分平台在拼接壳层命令时会因此报 `Unterminated quoted string`。

### 方案二（零配置）：什么都不改，直接重新部署

`main.py` 已经存在，平台默认的 `uvicorn main:app --host 0.0.0.0 --port $PORT`
现在可以正常启动应用。只要确认 `pip install -r requirements.txt` 成功即可。

### 需要配置的环境变量

| 变量 | 说明 |
| --- | --- |
| `PORT` | 平台一般会自动注入；未注入时默认 3000 |
| `LLM_PLATFORM_HOME` | 数据目录。沙箱建议留空，`main.py`/`start.sh` 会自动回退到仓库内 `runtime/`（可写） |
| `FLASK_SECRET_KEY` | 不设时自动生成并持久化到 `$LLM_PLATFORM_HOME/secret.key`，会话不会因重启失效 |
| `ADMIN_PASSWORD` | 必须改 |
| `PUBLIC_BASE_URL` / `DOMAIN` | 平台分配的访问域名，用于邮件激活链接与支付回调 |
| `DEMO_FALLBACK` | 设为 `1` 时，无可用上游模型也会返回演示回复，方便先验证部署 |

## 部署后的自检

```bash
curl https://<你的域名>/health
# {"ok":true,"db":true,...,"model_providers":[...]}

curl -o /dev/null -w '%{http_code}\n' https://<你的域名>/       # 200
curl https://<你的域名>/v1/models
```

`/health` 返回 `ok:true` 即表示进程已监听、SQLite 已建库。

## 注意事项

### 数据持久化（重要）

PandaStack App Hosting（及大多数沙箱 PaaS）**目前不支持给 App 挂载持久卷**，
文件系统是临时的：每次重新部署都会清空 `runtime/platform.db`（用户 / API Key / 订单全部丢失）。
运行期间不会丢，只有重新部署才丢。

本仓库已内置 **Litestream 方案**：启动时自动下载 litestream，
把 SQLite 实时复制到任意 S3 兼容对象存储（推荐 Cloudflare R2，免费 10GB），
下次启动自动从副本恢复，数据跨重新部署保留。

在平台环境变量里增加（建议勾选 secret）：

| 变量 | 说明 |
| --- | --- |
| `LITESTREAM_ENDPOINT` | R2 为 `https://<账户ID>.r2.cloudflarestorage.com` |
| `LITESTREAM_BUCKET` | 桶名 |
| `LITESTREAM_ACCESS_KEY_ID` | R2 API Token 的 Access Key ID |
| `LITESTREAM_SECRET_ACCESS_KEY` | R2 API Token 的 Secret Access Key |
| `LITESTREAM_REGION` | 可选，R2 用 `auto`（默认） |
| `LITESTREAM_REPLICA_PATH` | 可选，桶内对象路径，默认 `llm-platform/platform.db` |

不设置这些变量时完全走本地 SQLite（现状，重新部署丢数据）。
R2 操作：Dashboard → R2 → Create bucket → API Tokens（Object Read & Write）。

### 其它

* 沙箱内没有本地 Ollama，`/health` 的 `ollama` 会是 `false`。
  请在 `/admin` 里配置一个 OpenAI 兼容的第三方上游，或设 `DEMO_FALLBACK=1` 先跑通流程。
* 建议把 `ADMIN_PASSWORD`、`FLASK_SECRET_KEY`、`PUBLIC_BASE_URL` 放在平台的环境变量里，
  不要提交 `.env`。
