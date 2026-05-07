# AgentFlow 云服务器部署指南

本文档面向单台云服务器部署，适合阿里云、腾讯云、华为云或任意 Ubuntu 服务器。

## 1. 服务器准备

建议规格：

- Ubuntu 22.04/24.04
- 2 vCPU / 4 GB RAM 起步
- 40 GB 系统盘
- 安全组仅开放 `80`、`443`（不要对公网开放 `5432`、`6379`、`8000`、`8501`）

安装 Docker：

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
docker compose version
```

## 2. 上传代码与配置

```bash
git clone <your-repo-url> agentflow-platform
cd agentflow-platform
cp .env.example .env
```

生产环境至少修改这些变量（`compose.prod.yaml` 已注入 `APP_ENV=production`，启动时会校验密钥/CORS/Redis 密码等）：

```bash
DEEPSEEK_API_KEY=replace-with-real-key
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
JWT_SECRET=replace-with-a-long-random-secret

POSTGRES_USER=agentflow
POSTGRES_PASSWORD=replace-with-strong-password
POSTGRES_DB=agentflow_db
REDIS_PASSWORD=replace-with-strong-redis-password

# 必须与浏览器访问域名一致，且为 HTTPS；不要用 localhost
PUBLIC_APP_ORIGIN=https://agentflow.example.com
PUBLIC_API_BASE_URL=https://api.agentflow.example.com

UVICORN_RELOAD=false
```

可以用下面命令生成 `JWT_SECRET`：

```bash
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

## 3. 启动服务

```bash
docker compose -f compose.prod.yaml up -d --build
docker compose -f compose.prod.yaml ps
```

检查健康状态（生产 Compose 将 API/前端绑定到本机回环地址，仅配合本机 Nginx 暴露外网）：

```bash
bash scripts/check-deploy.sh
curl -sS http://127.0.0.1:8000/healthz
curl -sS http://127.0.0.1:8000/readyz
```

如需临时从另一台机器直连调试 API，可短期把 `compose.prod.yaml` 中的端口改为 `"8000:8000"`，调试结束后务必改回 `127.0.0.1` 并在安全组关闭 `8000`。

## 4. 域名与反向代理

推荐用 Nginx 或云厂商负载均衡做 HTTPS。

示例 Nginx 已放在 `docs/nginx-agentflow.conf`。复制后替换域名：

```bash
sudo cp docs/nginx-agentflow.conf /etc/nginx/sites-available/agentflow
sudo ln -sf /etc/nginx/sites-available/agentflow /etc/nginx/sites-enabled/agentflow
sudo nginx -t
sudo systemctl reload nginx
```

上线 HTTPS 可使用 Certbot：

```bash
sudo apt-get update
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d agentflow.example.com -d api.agentflow.example.com
```

## 5. 管理员账号

第一个注册的用户会自动成为管理员。登录后打开 Streamlit 侧边栏中的 Admin 页面，或访问 Streamlit 多页面路由中的 `admin` 页面，即可看到调用量、路由分布、Token 和费用估算。

## 6. 运维命令

```bash
docker compose -f compose.prod.yaml logs -f api
docker compose -f compose.prod.yaml logs -f frontend
docker compose -f compose.prod.yaml pull
docker compose -f compose.prod.yaml up -d --build
docker compose -f compose.prod.yaml down
```

备份数据库：

```bash
bash scripts/backup-postgres.sh
```

数据库卷 `pgdata` 和 Redis 卷 `redisdata` 会持久化在 Docker volume 中。升级前建议先做云盘快照或数据库备份。

## 6.1 容器日志轮转

`compose.prod.yaml` 已为各服务配置 Docker `json-file` 日志驱动（单文件约 10MB、保留多份），避免占满磁盘。

## 6.2 Nginx 日志轮转

示例（按需创建 `/etc/logrotate.d/nginx-agentflow`）：

```
/var/log/nginx/agentflow-*.log {
    weekly
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    sharedscripts
    postrotate
        [ -f /var/run/nginx.pid ] && kill -USR1 $(cat /var/run/nginx.pid)
    endscript
}
```

## 7. 上线检查清单

- `.env` 中 `JWT_SECRET`、`POSTGRES_PASSWORD`、`REDIS_PASSWORD` 已换成强随机值。
- `PUBLIC_APP_ORIGIN` 和 `PUBLIC_API_BASE_URL` 已换成真实 HTTPS 域名。
- 云服务器安全组只开放 `80`、`443`，不要对公网开放 `5432` 和 `6379`。
- `compose.prod.yaml` 中 API/前端端口为 `127.0.0.1:8000` / `127.0.0.1:8501`，由 Nginx 对外提供服务。
- 第一个管理员账号已注册，弱密码已更换。
- `curl https://api.agentflow.example.com/readyz` 返回 ready。
- 手机浏览器可以访问 `https://agentflow.example.com`。
