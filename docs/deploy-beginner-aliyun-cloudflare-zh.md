# 新手向：阿里云轻量 + Cloudflare + Docker + Nginx + HTTPS 部署 AgentFlow

本文假设你已经具备：

- 一台 **阿里云轻量应用服务器**，Ubuntu 24.04，**公网 IP** 例如 `139.224.135.73`（以你控制台为准）。
- 域名 **`treehouserly.asia`** 的 DNS 在 **Cloudflare** 管理。
- Cloudflare 里已有两条 **A 记录**：
  - **`app`** → 你的轻量公网 IP，**小黄云（已代理）** 打开。
  - **`api`** → 同一公网 IP，**小黄云** 打开。
- 树洞/看板仍在 **Vercel**（`www`、`kanban` 等 **不要动**）。

最终效果：朋友用浏览器打开 **`https://app.treehouserly.asia`** 聊天；前端会通过 **`https://api.treehouserly.asia`** 调用接口。

---

## 第零部分：先搞懂三件小事（别怕）

1. **服务器**：像一台永远在网上的电脑，你只通过 **SSH** 在它的「黑框框」里打字装软件。
2. **Docker Compose**：像在服务器里一键装好「数据库 + Redis + 后端 + 网页」一整套，不用自己挨个装PostgreSQL。
3. **Nginx**：像「门卫」。外面的人访问 **443 端口**（HTTPS），Nginx 再根据域名 **`app`** 或 **`api`**，把访问转到本机隐藏的 **8501（网页）** 和 **8000（接口）**。

Cloudflare 小黄云：**外面的人看到的是 Cloudflare 的 IP**，真实流量最终会到你服务器。你只要保证服务器 **80、443 开着**，Nginx 配好就行。

---

## 第一部分：用密码登录服务器（Windows / Mac 通用）

### 1.1 确认你有「公网 IP」和 root 密码

在阿里云控制台 → 轻量应用服务器 → 你的实例，记下：

- **公网 IPv4**（下文用 `你的公网IP` 代替）
- 若还没设密码：点 **「设置密码」**，设一个**强密码**并保存。

### 1.2 在你自己电脑上打开终端

- **Windows 11**：开始菜单搜 **PowerShell**，打开。
- **Mac**：打开 **终端**。

### 1.3 第一次连接

在终端里输入（把 IP 换成你的）：

```bash
ssh root@你的公网IP
```

第一次会问 `Are you sure you want to continue connecting?`，输入 **`yes`** 回车。

然后输入 **root 密码**（输入时屏幕上**不会显示星号**，正常），回车。

看到类似 `root@xxx:~#` 就说明**已经进到服务器里了**。后面所有命令**都在这一台机器上**执行。

**连不上时常见原因：**

- 安全组/防火墙没放行 **22**。
- IP 填错、密码错。
- 部分网络禁止 22 端口（换手机热点试）。

---

## 第二部分：放行防火墙端口（必须）

在 **阿里云控制台** → 你的轻量实例 → **防火墙**（或安全组）：

添加规则（若已有可跳过）：

| 协议 | 端口 | 说明 |
|------|------|------|
| TCP | 22 | 你自己远程登录 |
| TCP | 80 | 申请证书、HTTP 跳转 |
| TCP | 443 | 正式 HTTPS 访问 |

**不要**对全网开放 **5432、6379、8000、8501**（数据库和内部端口不必暴露）。

---

## 第三部分：在服务器上安装基础工具 + Docker

以下命令**整段复制到 SSH 里执行**即可（一条执行完再贴下一条）。

### 3.1 更新软件列表并装 git、curl

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
```

### 3.2 安装 Docker 官方一键脚本

```bash
curl -fsSL https://get.docker.com | sudo sh
```

### 3.3 让 Docker 开机自启，并把当前用户加入 docker 组（可选）

```bash
sudo systemctl enable --now docker
```

你若是 **root** 登录，下面这句可省略；若是普通用户再执行：

```bash
sudo usermod -aG docker "$USER"
```

然后 **退出 SSH 再登录一次**，`docker` 命令才免 sudo。

### 3.4 验证 Docker

```bash
docker --version
docker compose version
```

能看到版本号即成功。

---

## 第四部分：把项目代码弄到服务器上

白话流程分两段：**先在「你自己的电脑」上，用 GitHub Desktop 把代码推到 GitHub**；**再在「阿里云服务器」上一条命令克隆下来**。  
你以后改代码也是在电脑上 Commit + Push，服务器上 `git pull` 就行。

---

### （一）在电脑上：用 GitHub Desktop 上传到 GitHub

**先安装** [GitHub Desktop](https://desktop.github.com/)，打开一次并用浏览器登录你的 GitHub 账号。

#### 情况 1：这个文件夹还从来没当过 Git 仓库

1. 打开 GitHub Desktop。  
2. 菜单 **File → Add local repository…（添加本地仓库）**。  
3. 点 **Choose…**，选你电脑上的 **`agentflow-platform`** 文件夹。  
4. 若提示不是 Git 仓库：点里面的 **create a repository**（在这创建仓库）；或 **File → New repository**：  
   - **Name**：例如写 `agentflow-platform`  
   - **Local path**：选到**上一级目录**，避免多层嵌套。  
5. 创建后左边会看到很多「未提交」文件。**重点**：列表里如果出现 **`.env`**，说明你本地密钥文件没被忽略——**绝对不要勾选提交**；应保留在本地，密钥只放在服务器 `.env`。正常项目里有 `.gitignore` 会自动忽略 `.env`。  
6. 左下角 **Summary** 写一句话，例如 `Initial commit`，点 **Commit to main**。  
7. 顶部点 **Publish repository（发布仓库）**：  
   - 可勾选 **Keep this code private**（私有，别人看不到代码）。  
   - 确定 **Publish**。  
8. 浏览器打开 GitHub，应能看到 `https://github.com/你的用户名/仓库名`。记下：**用户名** 和 **仓库名**。

#### 情况 2：已经是 Git 仓库（以前用过）

1. **File → Add local repository…**，选好 `agentflow-platform`。  
2. 有改动就 **Commit**。  
3. 若顶部是 **Publish repository**，点发布；若是 **Push origin**，有改动就 **Push**。  
同样确认 **不要推送 `.env`**。

---

### （二）在阿里云服务器：从 GitHub 克隆

SSH 登录服务器后（把网址里的用户名、仓库名换成你的）：

```bash
cd /root
git clone https://github.com/你的用户名/你的仓库名.git agentflow-platform
cd agentflow-platform
```

#### 私有仓库会问密码怎么办？

用 **HTTPS** 克隆私有仓库时：  
**Username**：你的 GitHub 用户名。**Password**：不是登录密码，要填 **Personal Access Token（PAT）**。

1. 浏览器：GitHub 头像 → **Settings** → **Developer settings** → **Personal access tokens** → 新建一个 token。  
2. 勾选至少 **能读仓库** 的权限。  
3. 生成后**复制一长串字符**保存好（只显示一次）。  
4. 服务器执行 `git clone` 问你密码时，**粘贴这串 Token**（光标不动是正常的）。

以后可在服务器配置 SSH Key，就不用每次 PAT（进阶，可稍后再弄）。

---

### 方式 B：不用 GitHub —— 打包上传（零基础也可用）

适合你暂时不想注册 GitHub 的情况。

在你**电脑**上，在项目**上一级文件夹**打开终端（或 PowerShell），执行（Windows 若没有 `tar`，可用 WinSCP 直接拖文件夹，略）：

```bash
tar czf agentflow-platform.tar.gz --exclude=venv --exclude=.git agentflow-platform
```

把生成的压缩包用 **WinSCP** 传到服务器 **`/root/`**，再在服务器：

```bash
cd /root
tar xzf agentflow-platform.tar.gz
cd agentflow-platform
```

---

## 第五部分：配置 `.env`（非常关键，逐项填）

### 5.1 复制模板

```bash
cd ~/agentflow-platform
cp .env.example .env
nano .env
```

`nano` 用法：**改完按 `Ctrl+O` 保存，回车，`Ctrl+X` 退出**。

### 5.2 你必须理解的几类变量

**（1）大模型密钥**

```env
DEEPSEEK_API_KEY=你在 DeepSeek 控制台复制的真实密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
```

没有密钥，Agent 没法回答。

**（2）JWT 与生产模式**

在生产环境必须用**长随机字符串**（至少 30+ 字符）：

```env
JWT_SECRET=这里填一长串随机字符不要用示例
JWT_REFRESH_EXPIRES_SECONDS=2592000
APP_ENV=production
UVICORN_RELOAD=false
```

本机生成随机串（可在服务器执行）：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**（3）PostgreSQL / Redis（给 compose.prod 用）**

`compose.prod.yaml` 会用下面这些拼连接串，**必须强密码**：

```env
POSTGRES_USER=agentflow
POSTGRES_PASSWORD=强密码英文字母数字组合
POSTGRES_DB=agentflow_db
REDIS_PASSWORD=另一条强密码
```

**（4）浏览器访问地址（必须和 Cloudflare 上域名一致，且是 HTTPS）**

```env
PUBLIC_APP_ORIGIN=https://app.treehouserly.asia
PUBLIC_API_BASE_URL=https://api.treehouserly.asia
```

若你的子域名不是 `app`/`api`，这里改成你的真实地址。

**（5）`DATABASE_URL` / `REDIS_URL`（新手容易踩坑）**

生产环境里 **`compose.prod.yaml` 会自动给 API 容器设置** 连到 Docker 里的 Postgres 和 Redis，**一般不用再抄一长串到 `.env`**。

但你的 `.env` 里如果还留着从 `.env.example` 复制来的 **带 `password123` 的整行 `DATABASE_URL=`**，在 `APP_ENV=production` 时有可能被**启动安全校验**误判为弱密码而**拒绝启动**。遇到这种情况：**删掉那行 `DATABASE_URL`**，或改成与你的 `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` 一致且**不是** `password123`。`REDIS_URL` 同理：**不要**再写无密码的 `redis://localhost:6379/0`。

**（6）API_KEYS**

公网给朋友用时，建议设一个密钥，前端需在请求头带（当前 Streamlit 若未带全局 API Key，则可能需代码支持或暂时留空仅 JWT——以你仓库实际为准）。若为简化首期上线，可先 **留空** 再尽快收紧。**不要**把真实 `API_KEYS` 提交到 GitHub。

保存 `.env` 后执行：

```bash
chmod 600 .env
```

---

## 第六部分：用生产 Compose 启动整套服务

在 **`agentflow-platform` 目录**：

```bash
docker compose -f compose.prod.yaml up -d --build
```

第一次会较久（拉镜像、构建）。

查看是否在跑：

```bash
docker compose -f compose.prod.yaml ps
```

跑自检脚本：

```bash
bash scripts/check-deploy.sh
```

本机应有响应（不要求外网直连）：

```bash
curl -sS http://127.0.0.1:8000/healthz
curl -sS http://127.0.0.1:8501/ -o /dev/null -w "%{http_code}\n"
```

若 `docker compose` 报错提到 **JWT / CORS / Redis 密码**：说明 `.env` 未达到生产校验，按提示逐项改 **`PUBLIC_*`（https、非 localhost）、`JWT_SECRET`、`REDIS_URL` 带密码、数据库不要用弱口令**。

**常见新手问题：内存不够** → 轻则慢，重则构建失败；考虑升级套餐或加 swap（略）。

---

## 第七部分：安装 Nginx（装在服务器操作系统里，不在 Docker 里）

```bash
sudo apt-get install -y nginx
```

把项目里的示例配置拷过去：

```bash
sudo cp /root/agentflow-platform/docs/nginx-agentflow.conf /etc/nginx/sites-available/agentflow
sudo nano /etc/nginx/sites-available/agentflow
```

把文件里的：

- `agentflow.example.com` 全部改成 **`app.treehouserly.asia`**
- `api.agentflow.example.com` 改成 **`api.treehouserly.asia`**

**不要改** `proxy_pass http://127.0.0.1:8501` 和 `8000`，这是 Docker 映射到本机回环地址。

启用站点：

```bash
sudo ln -sf /etc/nginx/sites-available/agentflow /etc/nginx/sites-enabled/agentflow
sudo nginx -t
sudo systemctl reload nginx
```

此时用 **http** 访问应能通（未加密）：

```bash
curl -I -H "Host: app.treehouserly.asia" http://127.0.0.1
```

---

## 第八部分：HTTPS 证书（Let's Encrypt + Certbot）

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d app.treehouserly.asia -d api.treehouserly.asia
```

按提示填邮箱、同意协议。成功后 Nginx 会自动改为 **443**。

**Cloudflare 侧建议：**

- 打开 Cloudflare → **SSL/TLS** → 概述：选 **Full (strict)**（推荐）或至少 **Full**。
- 若选 **Full (strict)**，源站必须是**有效证书**（Certbot 给出的就是）。

证书会自动续期；可测试：

```bash
sudo certbot renew --dry-run
```

---

## 第九部分：从外网自检

在你**自己电脑**浏览器打开：

1. `https://app.treehouserly.asia` —— 应出现 Streamlit 登录/注册。
2. `https://api.treehouserly.asia/healthz` —— 应返回 JSON `{"status":"ok"}` 之类。
3. `https://api.treehouserly.asia/readyz` —— 依赖都 OK 时为 ready。

再让朋友用手机 **4G** 打开 `https://app.treehouserly.asia`（避免局域网缓存）。

---

## 第十部分：第一个账号与给朋友用

1. **你先打开网页点「注册」**——本项目约定 **第一个注册用户为管理员**。  
2. 把 **`https://app.treehouserly.asia`** 发给朋友，各自注册，聊天记录按用户分开。

Admin 页面仅管理员可看；运营若要用知识库上传，可通过 API 或后续单独页面。

---

## 第十一部分：以后怎么更新代码

```bash
cd ~/agentflow-platform
git pull
docker compose -f compose.prod.yaml up -d --build
```

数据库在 Docker volume **`pgdata`** 里，一般不会因为重建容器清空（除非手动删 volume）。

---

## 备份（强烈建议）

```bash
cd ~/agentflow-platform
bash scripts/backup-postgres.sh
```

备份文件在 **`backups/`**（已被 `.gitignore` 忽略，勿上传 GitHub）。

---

## 常见问题（FAQ）

### Q1：`nslookup app` 看到的是 104.x、172.x，不是 139.x？

**正常。** 小黄云代理后，对外是 Cloudflare IP。只要在 Cloudflare DNS 表里 **`app` 的 Content 是你的 139.x** 就对了。

### Q2：浏览器报 502 / 521？

- **521**：Cloudflare **能到你的公网 IP，但连不上你在 80/443 提供服务的程序**（或防火墙拦了）。请在服务器 SSH 里执行自检（项目在 `/root/agentflow-platform` 时）：
  ```bash
  cd /root/agentflow-platform   # 按你的实际路径改
  sudo bash scripts/check-origin-cloudflare.sh
  ```
  按输出里 **`[!!]`** 逐项修：**阿里云轻量防火墙**必须放行 TCP **80、443**；`sudo systemctl status nginx` 要是 **running**；`sudo nginx -t` 要能过。
- **502**：源站 HTTP 通了，但 Nginx **反代到 Docker 失败** → `docker compose ps` 看 api 是否 healthy；服务器上 `curl -sS http://127.0.0.1:8000/healthz`。另：**`/healthz` 在 API 域名**上测：`https://api.你的域名/healthz`，不要用在 `app` 前台域名上强求同一路径。

### Q3：CORS 错误？

`.env` 里 **`PUBLIC_APP_ORIGIN` 必须和浏览器地址栏完全一致**（https、子域名、无末尾斜杠多试一次）。

### Q4：想改子域名？

Cloudflare 加新 A 记录，`.env` 两个 `PUBLIC_*` 一起改，Nginx `server_name` 一起改，再 `certbot` 加 `-d` 新域名。

---

## 你需要的最短清单（核对用）

- [ ] 阿里云防火墙：22 / 80 / 443  
- [ ] Cloudflare：`app`、`api` → A → 服务器 IP，小黄云 ON  
- [ ] 服务器：`docker compose -f compose.prod.yaml up -d --build` 成功  
- [ ] `.env`：`APP_ENV=production`，`PUBLIC_*` 为 https 真实域名，密钥强  
- [ ] Nginx：`server_name` 与域名一致，`proxy_pass` 127.0.0.1:8501 / 8000  
- [ ] Certbot：两个域名都有证书  
- [ ] 浏览器：`https://app...` 能注册登录  

如需针对某一步报错截图（打码密钥），可把**完整报错最后 20 行**发出来逐项排查。
