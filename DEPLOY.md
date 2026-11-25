# 恒业 × 新月工作台 — 国内公网正式上线指南

本工作台已内置 **PBKDF2 账号密码登录保护**、**PC 微信插件独立接口 Token（`X-WeChat-Token`）**、**SQLite 持久化** 与 **国内网络直连行情抓取**，打包仅需 `server.py`、`index.html` 与 `Dockerfile`。

---

## 环境变量说明

| 变量名 | 默认值 | 生产建议 |
| :--- | :--- | :--- |
| `WORKBENCH_ADMIN_USER` | `admin` | 管理员登录账号 |
| `WORKBENCH_ADMIN_PASS` | `Hengye@2026` | **务必在上线环境变量中改为强密码（或首登后立即在页面修改）** |
| `WORKBENCH_WX_TOKEN` | 启动自动生成 `wxk_...` | 可选，固定 PC 微信插件请求头 `X-WeChat-Token` |
| `WORKBENCH_DB_PATH` | `/data/workbench.db`（Docker） | 务必指向持久化挂载目录，防止重启丢数据 |
| `PORT` | `8765` | 监听端口，自动适配云平台注入端口 |

---

## 方式一（推荐）：Sealos 国内区上线（`bja.sealos.run` / `hzh.sealos.run`）

Sealos 国内区原生支持 Kubernetes PVC 存储卷挂载（`/data` 永久保存 SQLite）以及自带平台 HTTPS 公网二级域名免备案，无需自己买云服务器、无需配置 Nginx/SSL。

### 路径 A：GitHub 自动构建镜像 + Sealos 应用管理（最稳）
1. 将本目录推送至你的 GitHub 仓库（`main` 分支），自带的 `.github/workflows/docker.yml` 会自动触发云端构建，约 1 分钟后生成镜像：`ghcr.io/<你的GitHub用户名>/<仓库名>:latest`。
   - **重要**：请在 GitHub 仓库右侧 **Packages** 中将该包可见性设为 **Public**（或在 Sealos 部署页填写 GitHub PAT 私有镜像凭据）。
   - 如果国内节点拉取 `ghcr.io` 较慢，可将镜像地址前缀替换为国内高校加速镜像：`ghcr.nju.edu.cn/<你的GitHub用户名>/<仓库名>:latest`。
2. 登录 **Sealos 国内区**（`https://bja.sealos.run` 或 `https://hzh.sealos.run`）进入 **「应用管理 (App Launchpad)」→「新建应用」**：
   - **镜像名**：填入 `ghcr.io/<用户名>/<仓库名>:latest`（或 `ghcr.nju.edu.cn/...`）。
   - **容器端口**：填 **`8765`**，勾选开启 **「外网访问」**。
   - **环境变量**：至少设置 `WORKBENCH_ADMIN_PASS=你的强密码`（可选 `WORKBENCH_ADMIN_USER=admin`）。
   - **存储卷（关键）**：点击添加存储卷，容量填 `1` Gi 以上，挂载路径务必填 **`/data`**（确保 `/data/workbench.db` 重启与发版不丢失）。
3. 点击部署，等待状态变为 `Running` 后，点击平台生成的 `https://xxx.bja.sealos.run` 即可公网打开！

### 路径 B：Sealos DevBox（直接导入 GitHub 仓库）
在 Sealos 控制台进入 **「DevBox」→「从 GitHub 导入」** 选择本仓库，配置运行端口 `8765` 并开启公网访问，按指引安装依赖（`starlette uvicorn httpx beautifulsoup4 python-multipart`）后运行 `uvicorn server:app --host 0.0.0.0 --port 8765 --proxy-headers --forwarded-allow-ips='*'` 即可。

> **⚠️ 微信云托管重要避坑提示**：经核实，微信云托管标准容器默认**不支持挂载持久化磁盘卷**，实例缩容到 0 或重新发布版本时会重置容器本地文件系统，导致 SQLite 数据库（`workbench.db`）清空。因此**不建议直接使用当前 SQLite 版部署到微信云托管**（若必须使用微信云托管，需先改写后端连接微信云托管内置 MySQL）。

---

## 方式二：阿里云 / 腾讯云国内轻量服务器（公网 IP + 端口直连）

适合需要**固定国内公网 IP**（直接填进微信公众号后台「IP 白名单」）的正式生产环境。

### A. Docker 一键启动（推荐）
将 `Dockerfile`、`server.py`、`index.html` 上传至服务器 `/opt/hengye-workbench` 后执行：
```bash
cd /opt/hengye-workbench
docker build -t hengye-workbench .
docker run -d --name hengye-workbench \
  --restart unless-stopped \
  -p 8765:8765 \
  -v /opt/hengye-data:/data \
  -e WORKBENCH_ADMIN_USER=admin \
  -e WORKBENCH_ADMIN_PASS='请替换为你的强密码' \
  hengye-workbench
```
然后在阿里云/腾讯云控制台「防火墙 / 安全组」放行 TCP **`8765`** 端口，即可通过 `http://你的服务器公网IP:8765` 让客户直接访问！

### B. 无 Docker 直接 Python + systemd 守护启动
```bash
mkdir -p /opt/hengye-workbench /opt/hengye-data
# 上传 server.py 和 index.html 到 /opt/hengye-workbench
pip3 install -i https://pypi.tuna.tsinghua.edu.cn/simple starlette uvicorn httpx beautifulsoup4 python-multipart

cat >/etc/systemd/system/hengye-workbench.service <<'EOF'
[Unit]
Description=Hengye Xinyue AI Workbench
After=network.target

[Service]
WorkingDirectory=/opt/hengye-workbench
Environment=WORKBENCH_DB_PATH=/opt/hengye-data/workbench.db
Environment=WORKBENCH_ADMIN_USER=admin
Environment=WORKBENCH_ADMIN_PASS=请替换为你的强密码
Environment=PORT=8765
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port 8765
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload && systemctl enable --now hengye-workbench
```

---

## 上线后首登检查清单

1. 使用 `WORKBENCH_ADMIN_USER` / 密码登录工作台，在 **【模型与凭据配置】➔【账号安全】** 中修改默认密码。
2. 填入 **图片生成模型 API Key**、**Dify 工作流 Key / 国内大模型 Key** 与 **微信公众号 AppID / AppSecret**。
3. 若使用微信公众号自动直推草稿箱，将服务器固定公网 IP 加入微信公众平台「开发 ➔ 基本配置 ➔ IP 白名单」。
4. 在 **【WeChat 接口中心】** 下载 `hengye_wx_plugin.py`（已自动内置当前公网地址与 `X-WeChat-Token`），放在运行 PC 微信的电脑上运行即可。
