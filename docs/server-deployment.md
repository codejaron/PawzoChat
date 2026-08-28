# 无 GUI Linux 服务器部署

服务器模式面向具有公网 IP 的 Linux 主机。服务器不需要安装桌面或浏览器：PawzoChat 只监听本机固定 HTTP 端口，Nginx 在同一台机器上终止可信 HTTPS，管理员从自己的电脑或手机打开域名完成配置和账号扫码。

```text
浏览器 → https://chat.example.com → Nginx → http://127.0.0.1:62000 → PawzoChat
```

服务器模式不创建自签名证书、随机公网端口、随机路径或 Cloudflare Tunnel。家庭电脑没有公网 IP 时需要的内网穿透不属于本模式。

## 运行边界

- PawzoChat 必须保持单进程。消息队列、账号连接、主动消息、朋友圈和 Web Push 都在进程内运行，不要配置 Gunicorn/uWSGI 多 worker。
- 只把 Nginx 的 `80/443` 暴露到公网；不要在防火墙或云安全组中开放 PawzoChat 的 `62000`。
- `/var/lib/pawzochat` 是唯一运行数据目录，应纳入备份。代码更新和卸载不得删除它。
- 管理员密码、服务商密钥和账号凭据不得写入仓库或 `/etc/pawzochat/server.env`。

## 1. 安装程序

以下以 Debian/Ubuntu 和 `/opt/pawzochat` 为例，使用 root 执行：

```bash
apt update
apt install -y git python3 python3-venv nginx

useradd --system --user-group --home-dir /var/lib/pawzochat \
  --shell /usr/sbin/nologin pawzochat
install -d -m 0700 -o pawzochat -g pawzochat /var/lib/pawzochat

install -d -m 0755 /opt/pawzochat
git clone https://github.com/codejaron/PawzoChat.git /opt/pawzochat/app
python3 -m venv /opt/pawzochat/venv
/opt/pawzochat/venv/bin/pip install --upgrade pip
/opt/pawzochat/venv/bin/pip install -e /opt/pawzochat/app
```

`pip install -e` 只把命令入口连接到当前代码目录；用户数据不会写进代码仓库。

## 2. 配置部署参数

```bash
install -d -m 0750 -o root -g pawzochat /etc/pawzochat
install -m 0640 -o root -g pawzochat \
  /opt/pawzochat/app/deploy/server.env.example \
  /etc/pawzochat/server.env
```

编辑 `/etc/pawzochat/server.env`：

```ini
PAWZOCHAT_DATA_DIR=/var/lib/pawzochat
PAWZOCHAT_BIND=127.0.0.1
PAWZOCHAT_PORT=62000
PAWZOCHAT_PUBLIC_URL=https://chat.example.com
PAWZOCHAT_PROXY_HOPS=1
TZ=Asia/Shanghai
```

- `PAWZOCHAT_PUBLIC_URL` 必须是最终访问的可信 HTTPS Origin，不能包含路径。
- Nginx 与 PawzoChat 在同一台主机时，`PAWZOCHAT_PROXY_HOPS=1`。
- `TZ` 只改变 PawzoChat 进程时间，不修改服务器系统时区。
- 环境文件只支持上述键和普通 `KEY=VALUE`，不会作为 Shell 脚本执行。

## 3. 初始化管理员和数据

初始化命令不会启动网络监听。它会创建配置、PBKDF2 管理员密码、持久化 Session 密钥、VAPID 私钥，并把内置表情、主题和 MCP 资源复制到独立数据目录：

```bash
runuser -u pawzochat -- /opt/pawzochat/venv/bin/pawzochat server init
```

终端会要求输入两次管理员密码。密码至少 8 位，并同时包含大写字母、小写字母和数字。

无人值守初始化可以从标准输入读取一行密码，但不要把密码直接写在命令参数中：

```bash
runuser -u pawzochat -- /opt/pawzochat/venv/bin/pawzochat \
  server init --password-stdin
```

## 4. 安装 systemd 服务

```bash
install -m 0644 /opt/pawzochat/app/deploy/pawzochat.service \
  /etc/systemd/system/pawzochat.service
systemctl daemon-reload
systemctl enable --now pawzochat
systemctl status pawzochat --no-pager
```

此时只有服务器本机的 `127.0.0.1:62000` 可以连接。服务器模式对所有页面和 API 强制管理员登录，`/healthz` 除外。

## 5. 配置 Nginx

先让域名的 DNS 记录指向服务器公网 IP，然后创建 `/etc/nginx/sites-available/pawzochat`：

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name chat.example.com;

    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:62000;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Port $server_port;

        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

启用配置：

```bash
ln -s /etc/nginx/sites-available/pawzochat /etc/nginx/sites-enabled/pawzochat
nginx -t
systemctl reload nginx
```

然后使用你选择的公共 CA 为 `chat.example.com` 配置有效证书，并把 HTTP 重定向到 HTTPS。例如使用系统发行版提供的 Certbot Nginx 集成：

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d chat.example.com
```

证书安装后，浏览器只能通过 `https://chat.example.com` 登录；不要直接把 HTTP 源站端口提供给用户。

## 6. 部署诊断

```bash
runuser -u pawzochat -- /opt/pawzochat/venv/bin/pawzochat server doctor
```

该命令自动读取 `/etc/pawzochat/server.env`，并检查：

- 数据目录权限与管理员密码哈希
- 持久化 Session 和 VAPID 私钥
- PawzoChat 本地监听端口
- 最终公网 HTTPS `/healthz`
- 进程时区

任一必要检查失败时返回非零退出码。尚未配置 DNS/证书时可临时跳过公网请求：

```bash
runuser -u pawzochat -- /opt/pawzochat/venv/bin/pawzochat \
  server doctor --skip-public-check
```

## 从现有桌面数据迁移

全新部署可跳过本节。需要保留桌面端已有账号、角色、聊天和插件时，在执行 `server init` 之前停止桌面端，把原项目的整个 `data/` 内容复制进服务器数据目录，而不是只复制 `config.yaml`：

```bash
rsync -a --chown=pawzochat:pawzochat \
  /原项目绝对路径/data/ /var/lib/pawzochat/
runuser -u pawzochat -- /opt/pawzochat/venv/bin/pawzochat server init
```

初始化会补齐缺失的服务器密钥和内置资源，并关闭仅适用于桌面模式的旧公网监听；不会覆盖已有聊天、账号凭据、插件和自定义资源。迁移完成并确认数据可用前，不要删除原始 `data/` 目录。

## 日常管理

查看日志：

```bash
journalctl -u pawzochat -f
```

修改管理员密码并让现有登录会话全部失效：

```bash
systemctl stop pawzochat
runuser -u pawzochat -- /opt/pawzochat/venv/bin/pawzochat server passwd
systemctl start pawzochat
```

`init` 和 `passwd` 都使用同一个数据目录锁；服务仍在运行时会明确拒绝修改，避免两个进程同时写配置。

升级代码：

```bash
systemctl stop pawzochat
git -C /opt/pawzochat/app pull --ff-only
/opt/pawzochat/venv/bin/pip install -e /opt/pawzochat/app
systemctl start pawzochat
```

备份与恢复只操作运行数据：

```text
/var/lib/pawzochat/
```

备份前应停止服务，或者使用能够提供一致文件系统快照的备份工具。不要只备份 `config.yaml`；账号凭据、聊天、插件、VAPID 身份和通知订阅都位于同一数据目录。

## Docker 边界

当前正式部署材料只维护 systemd。后续 Docker 镜像也必须调用同一个 `pawzochat server run`，保持单容器、单进程，并把 `/var/lib/pawzochat` 挂载为持久卷；不能在容器编排层横向扩容多个 PawzoChat 副本共享同一数据目录。
