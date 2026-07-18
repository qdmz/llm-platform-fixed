# Docker 部署说明

本文档说明如何用 Docker / Docker Compose 运行 `llm-platform-fixed`。

## 文件说明

```text
Dockerfile                  # 生产镜像，Python 3.12 + gunicorn
wsgi.py                     # gunicorn WSGI 入口，会自动 init_db()
docker/entrypoint.sh        # 容器启动脚本，初始化 data/logs 并启动 gunicorn
docker-compose.yml          # 标准部署：应用容器 + SQLite 数据卷
docker-compose.ollama.yml   # 可选：同时启动 Ollama sidecar
.env.docker.example         # Docker 环境变量示例
.dockerignore               # 镜像构建忽略规则
```

## 1. 准备环境变量

```bash
cp .env.docker.example .env
nano .env
```

生产环境至少修改：

```env
DOMAIN=newapi.ypvps.com
PUBLIC_BASE_URL=https://newapi.ypvps.com
ADMIN_PASSWORD=请改成强密码
FLASK_SECRET_KEY=请改成长随机字符串
```

如果本地 Ollama 跑在宿主机，保持：

```env
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

Linux Compose 已在 `docker-compose.yml` 中配置：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

## 2. 构建并启动

```bash
docker compose up -d --build
```

查看状态：

```bash
docker compose ps
docker compose logs -f llm-platform
curl http://127.0.0.1:5088/health
```

本仓库已验证：

- `docker build -t llm-platform-fixed:test .` 构建通过。
- 临时容器映射 `127.0.0.1:5099:5088` 后 `/health` 返回 `ok: true`。

默认访问：

```text
http://127.0.0.1:5088
```

如果 `.env` 里设置了 `HOST_PORT=8080`，则访问：

```text
http://127.0.0.1:8080
```

## 3. 数据持久化

Compose 使用命名卷保存 SQLite 数据库和日志：

```text
llm_platform_data -> /opt/llm-platform/data
llm_platform_logs -> /opt/llm-platform/logs
```

查看卷：

```bash
docker volume ls | grep llm_platform
```

备份数据库：

```bash
docker compose exec llm-platform sh -lc 'sqlite3 /opt/llm-platform/data/platform.db ".backup /opt/llm-platform/data/platform.backup.db"'
docker cp llm-platform:/opt/llm-platform/data/platform.backup.db ./platform.backup.db
```

## 4. 使用宿主机 Ollama

宿主机安装并启动 Ollama：

```bash
curl -fsSL https://ollama.com/install.sh | sh
systemctl enable --now ollama
ollama pull qwen2.5-coder-14b-ms:latest
```

确保 `.env`：

```env
OLLAMA_BASE_URL=http://host.docker.internal:11434
MODEL_NAME=qwen2.5-coder-14b-ms:latest
```

启动应用：

```bash
docker compose up -d --build
```

## 5. 使用 Ollama sidecar（可选）

如果希望 Docker 同时启动 Ollama：

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d --build
```

拉取模型：

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml exec ollama ollama pull qwen2.5-coder-14b-ms:latest
```

注意：14B 模型体积大、CPU 推理慢，需要足够磁盘和内存。生产建议优先使用第三方模型，本地 Ollama 作为最后兜底。

## 6. Nginx 反向代理

```nginx
server {
    listen 80;
    server_name newapi.ypvps.com;

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

HTTPS 建议用 acme.sh 或 certbot。

## 7. 更新版本

```bash
git pull
docker compose up -d --build
```

如果只改了环境变量：

```bash
docker compose up -d
```

## 8. 停止 / 重启

```bash
docker compose restart llm-platform
docker compose down
```

保留数据卷；如需彻底删除数据：

```bash
docker compose down -v
```

执行 `down -v` 会删除 SQLite 数据库，请先备份。
