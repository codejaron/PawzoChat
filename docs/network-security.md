# Web 访问与安全模型

PawzoChat 有两种运行模式：

| 模式 | 监听与入口 | 认证 |
| --- | --- | --- |
| 桌面模式 | 本地 `127.0.0.1` HTTP；可选旧版随机路径公网 HTTPS | 本地入口免登录；旧版公网入口需要密码 |
| 服务器模式 | 单一固定 HTTP 端口，默认 `127.0.0.1:62000`，由同机反向代理提供可信 HTTPS | 除健康检查和登录资源外全部需要管理员认证 |

服务器模式的完整安装边界见[无 GUI Linux 服务器部署](server-deployment.md)。下文“双服务实例”和“随机路径”只描述桌面模式的兼容功能。

## 桌面模式的双服务实例

桌面模式最多创建两个 Cheroot WSGI Server，二者共享同一个 Flask 应用和运行时数据：

| | 本地面板 | 公网面板 |
| --- | --- | --- |
| 绑定地址 | `127.0.0.1:web.port` | 直接模式为 `0.0.0.0:web.public_port`；反向代理模式为 `127.0.0.1:web.public_port` |
| 协议 | HTTP | HTTPS（自签名证书） |
| 启动条件 | 始终启动 | `public_enabled=true`，且密码、端口、路径前缀均有效 |
| 登录 | 免登录 | 必须通过密码登录 |

更改公网开关、端口、路径或密码后，需要重启 PawzoChat 才能重建监听实例并完整生效。清空密码会同时把 `public_enabled` 设为 `false`。

> 重要：清空密码后应立即重启。当前进程不会动态停止已经运行的公网 Server，而认证中间件会立即读到空密码；因此在重启前，原公网 URL 仍可能继续可达且不再要求登录。单纯关闭 `public_enabled` 也要重启后才会停止监听。

## 公网路径前缀

本地和公网 URL 结构不同：

```text
本地：http://127.0.0.1:62000/api/settings
公网：https://<IP>:38271/aB3xK9mQ2pLw/api/settings
                         └─ public_secret ─┘
```

公网 Server 由 `SecretPrefixMiddleware` 包装：

1. 请求路径必须等于 `/<public_secret>` 或以 `/<public_secret>/` 开头，否则返回 404。
2. 中间件剥离前缀，并设置 `SCRIPT_NAME=/<public_secret>`。
3. 在 WSGI environ 中标记访问策略为 `desktop_public`，供认证和管理路由判断来源。

Flask 内部仍只定义 `/api/settings` 等普通路由。模板使用 `request.script_root`，前端使用 `window.PAWZOCHAT_BASE` 自动补前缀：

```javascript
const base = window.PAWZOCHAT_BASE || "";
fetch(`${base}/api/settings`);
```

后端不得通过客户端 IP 或端口猜测来源，应统一使用 `pawzochat.web.access`：

```python
from pawzochat.web.access import is_legacy_public_access

is_public = is_legacy_public_access()
```

## 登录与会话

旧版桌面公网入口和服务器入口共用以下认证流程：

```text
请求进入
  ├─ 桌面公网路径前缀不匹配 → 404
  ├─ /login、/static/style.css、/static/logo.png → 免登录
  ├─ session.authenticated=true → 放行
  ├─ /api/* → 401 JSON
  └─ 其他页面 → 重定向 /login
```

登录页使用随机 CSRF Token。登录成功后的 Session 有效期为 24 小时。桌面模式每次启动生成临时 Session 密钥；服务器模式把 Session 密钥保存在数据目录的 `auth/session.key`，正常重启不会让所有设备掉登录。Cookie 名为 `pawzochat_session`，设置 `HttpOnly`、`SameSite=Lax`，远程入口还设置 `Secure`。

服务器通过 `pawzochat server passwd` 修改密码时会同时轮换 Session 密钥，使所有既有登录立即失效。桌面模式沿用当前会话行为。

本地请求不检查面板密码。这是密码恢复与本机维护入口，也意味着能在本机访问 PawzoChat 进程的用户拥有完整面板权限。

## 密码与暴力破解

新密码必须：

- 至少 8 个字符
- 同时包含大写字母、小写字母和数字

密码使用 PBKDF2-HMAC-SHA256 保存，参数为 60 万次迭代和 16 字节随机盐：

```text
$pbkdf2-sha256$600000$<salt_hex>$<hash_hex>
```

启动时会把旧版明文密码自动迁移为上述哈希格式。

登录失败按客户端 IP 独立统计：15 分钟内连续 5 次错误后，该客户端在窗口结束前收到 429；其他客户端不受影响。一次成功登录会清除该客户端的失败记录。服务器模式只在监听本机且配置可信反向代理层数时接受 `X-Forwarded-For`，避免客户端直接伪造限流身份。

## 公网写请求防护

除登录表单外，所有需要认证的远程 `POST`、`PUT`、`PATCH`、`DELETE` 请求必须满足同源检查：

- `Origin` 等于当前请求 Origin；或
- `Referer` 的 Origin 等于当前请求 Origin。

二者都不匹配时返回 403。此检查与 `SameSite=Lax` Cookie、登录页 CSRF Token 共同降低跨站请求风险。

进入 Flask 后生成的响应默认带有（路径前缀中间件直接生成的 403/404 不经过这一步）：

- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: same-origin`
- 远程响应额外带 `Content-Security-Policy: frame-ancestors 'none'`
- 服务器模式经代理确认 HTTPS 后还发送 HSTS

本地插件配置 iframe 的静态资源路由会单独把 `X-Frame-Options` 改为 `SAMEORIGIN`，以允许宿主页面嵌入沙箱 iframe。

## 管理功能边界

桌面模式的旧版公网入口即使已登录，仍有以下硬限制：

- `GET /api/settings` 不返回 `web` 段，不暴露端口、路径和密码状态。
- `PATCH /api/settings` 禁止修改 `web` 段。
- `POST /api/settings/regenerate-public` 禁止重新生成公网端口和路径。
- `/api/plugins/*` 全部仅限本地访问，公网返回 403。
- `/api/update/check|state|download|apply` 仅限本地访问，公网返回 404。
- 前端在公网模式下隐藏网络设置和插件管理入口。

其他已认证 API 仍可能修改角色、会话、服务商等数据；公网密码泄露应按完整面板权限泄露处理。

服务器模式的网页是唯一管理入口，因此已认证管理员可以管理 MCP 和插件；监听地址、端口、外部域名、代理层数和时区只能通过 `/etc/pawzochat/server.env` 修改，内置网页更新接口在服务器模式下返回 404。启用第三方插件等同于允许其在 PawzoChat 服务用户权限下执行代码。

## 证书、凭据与部署边界

服务器模式不生成或读取应用内 TLS 证书。PawzoChat 只提供固定 HTTP 源站，公共 CA 证书必须在 Nginx 或其他同机入口层终止。

桌面模式的旧版公网证书首次启用时写入：

```text
data/certs/server.crt
data/certs/server.key
```

证书为 PawzoChat 自签名证书，浏览器首次访问通常会显示不受信任警告。它提供传输加密，但不提供公共 CA 身份背书；需要受信任证书、域名或反向代理时，应由部署者在 PawzoChat 外部配置。

## 受信任 HTTPS 与浏览器通知

浏览器通知依赖 Web Push。手机浏览器不会把“手动忽略自签名证书警告”视为可信安全上下文，因此直接访问桌面模式的自签名公网地址不能启用通知。

部署在公网服务器时应使用[服务器部署模式](server-deployment.md)，由域名和公共 CA 证书提供稳定可信 Origin，不需要 Cloudflare Tunnel。以下 Cloudflare Tunnel 步骤只适用于没有公网 IP、需要从家庭电脑远程访问桌面模式的用户。

### 使用 Cloudflare Tunnel

前提：你的域名已经添加到 Cloudflare。

如果还没有 Tunnel：

1. 登录 Cloudflare，进入 `Networking → Tunnels`。
2. 点击 `Create tunnel`，按页面提示在运行 PawzoChat 的电脑上安装并启动 `cloudflared`。
3. 等 Tunnel 状态变为 `Healthy`。

然后连接 PawzoChat：

1. 打开刚创建的 Tunnel，进入 `Routes`。
2. 点击 `Add route → Published application`。
3. `Hostname` 填写你准备使用的域名，例如 `chat.example.com`。
4. `Service URL` 填写 `https://127.0.0.1:<公网端口>`。其中“公网端口”是 PawzoChat“设置 → 网络设置”页面显示的数字；例如页面显示 `46447`，这里就填写 `https://127.0.0.1:46447`，不要附加随机路径。
5. 展开 TLS 设置，开启 `No TLS Verify`。
6. 保存后重启 PawzoChat。
7. 打开页面显示的最终地址，例如 `https://chat.example.com/随机路径`。

`No TLS Verify` 只用于 Cloudflare 连接本机 PawzoChat，不会关闭用户访问域名时的 HTTPS。

不要把无密码的本地面板端口 `62000` 交给 Tunnel。也不要使用每次启动都会变化的临时域名；域名或随机路径改变后，浏览器会把它视为新的应用范围，需要重新订阅通知。重新生成 PawzoChat 随机路径时，服务会主动删除旧路径下的订阅。

平台侧的用户操作不同：Windows 的 Chrome、Edge、Firefox 可直接开启；macOS 的 Safari 16.1+、Chrome、Edge、Firefox 可直接开启且不要求添加到程序坞；Android 的 Chrome、Edge、Firefox 等可直接授权且不要求添加到主屏幕；iPhone/iPad 需要 iOS/iPadOS 16.4+，必须先在 Safari 中“添加到主屏幕”，再从主屏幕图标打开并开启通知。最终仍以页面对当前浏览器的能力检测为准。

`data/config/config.yaml` 中的密码是哈希，但 `public_secret` 不是加密凭据；`data/auth/accounts.json` 还包含通道令牌或 App Secret。应限制整个 `data/` 目录的读取权限，不要把它提交到公开仓库或直接共享。

绑定 `0.0.0.0` 只代表监听所有网卡，不会自动完成防火墙放行、路由器端口转发或云安全组配置。公网暴露前还应确认：

- 使用足够强且唯一的面板密码。
- 公网路径未通过截图、日志或浏览器分享泄露。
- 直接模式只开放实际使用的 `public_port`；反向代理模式不要把该端口对外放行。
- 能接受自签名证书的信任提示，或在外部终止可信 TLS。
- 了解登录限流保存在单进程内存中；PawzoChat 不支持多副本共享状态。

## 密码恢复

在运行 PawzoChat 的同一台机器上访问：

```text
http://127.0.0.1:<web.port>
```

进入“设置 → 网络设置”重设密码。本地面板免登录，无需直接编辑配置文件；保存后重启应用。

服务器模式没有免登录入口。使用服务用户执行：

```bash
systemctl stop pawzochat
pawzochat server passwd
systemctl start pawzochat
```

命令会安全更新密码哈希并撤销现有 Session，不需要修改 YAML。

## 配置

```yaml
web:
  port: 62000
  password: ""             # PBKDF2 哈希；空值表示未设置
  public_enabled: false
  public_port: 0           # 启用时由设置 API 自动生成
  public_secret: ""        # 启用时由设置 API 自动生成
  reverse_proxy_enabled: false
  public_base_url: ""      # 外部反向代理实际提供的 HTTPS origin，不含随机路径
```

相关实现：

- `pawzochat/app.py`：桌面双 Server 与服务器单 Server 生命周期
- `pawzochat/runtime.py`：服务器部署参数校验
- `pawzochat/server_cli.py`：初始化、运行、密码与诊断命令
- `pawzochat/web/access.py`：请求访问策略
- `pawzochat/web/app.py`：路径前缀、认证、登录限流、同源检查和安全响应头
- `pawzochat/web/routes/api_settings.py`：密码规则与公网设置限制
- `pawzochat/utils/crypto.py`：密码哈希与校验
- `pawzochat/utils/certs.py`：自签名证书生成
