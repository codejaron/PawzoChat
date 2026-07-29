# PawzoChat 插件功能介绍

> 术语约定：本文中的“插件”均指“运行时插件（runtime plugin）”；“hook”统一指扩展钩子；“MCP 工具”与“能力适配器”属于模型工具能力，不属于插件本体。

## 1. 定位

PawzoChat 的 `plugins` 指的是**第三方后端运行时扩展机制**，不是核心业务模块装配层。

当前这些能力属于核心功能，不通过插件实现：

- 内建微信、QQ 与 Web 通道
- 对话队列与 LLM 主流程
- 回复发送
- 表情包管理与表情包发送
- Web 前端页面结构与交互逻辑

运行时插件系统负责让外部开发者在既定 hook 上扩展行为，例如：

- 收到消息后做过滤或改写
- 消息落库后做审计、统计、同步
- 构建上下文时注入补充信息
- LLM 回复生成后追加或改写消息
- 发送前做脱敏、拦截
- 发送后做通知或日志记录
- 由插件自身（定时器、事件钩子、外部信号）主动向会话推送消息
- 注册新的聊天通道
- 调用 MCP 工具，或发布进程内工具供 LLM 使用

前端设置页提供了完整的插件管理界面，可以浏览、启停、配置插件。插件的配置界面有两种渲染方式：

- 默认：由系统根据 `config_schema` 自动渲染表单
- 进阶：插件声明 `config_ui` 后，由插件自带的 HTML 在**沙箱 iframe** 内渲染，通过 `postMessage` 与宿主通信

当前 `data/plugins/` 目录默认存在，仓库里**没有预置插件**。

---

## 2. 目录与运行模型

所有第三方插件都放在：

```text
data/plugins/{directory_name}/
```

目录名建议与 manifest 的 `id` 一致，但并非强制；发现、依赖和 API 标识都以 `plugin.yaml` 中的 `id` 为准。

一个插件目录就是一个完整工作区。当前约定的常见结构如下：

```text
data/plugins/reply_filter/
├── plugin.yaml
├── plugin.py
├── config.yaml
├── state/
└── ui/                # 可选：自定义 HTML 配置面板的静态资源
    └── index.html
```

说明：

- `plugin.yaml`：插件静态元数据
- `plugin.py`：插件入口代码
- `config.yaml`：插件启停状态和配置
- `state/`：插件运行时持久化数据目录
- `ui/`：可选目录。当 `plugin.yaml` 声明了 `config_ui` 后，宿主会通过 `GET /api/plugins/{plugin_id}/ui/<path>` 提供该目录下的静态文件给沙箱 iframe 加载

插件目录为空时，系统仍可正常启动；插件管理器会自动确保 `data/plugins/` 存在。

---

## 3. 生命周期

插件由 `pawzochat/core/extensions/manager.py` 统一管理，生命周期如下：

1. 启动或刷新时扫描 `data/plugins/*/plugin.yaml`
2. 读取 manifest，校验 `api_version`；若发现重复 `plugin_id`，跳过后发现的目录并记录警告
3. 读取插件目录下的 `config.yaml`
4. 如果 `config.yaml` 不存在，则按 `config_schema` 自动生成默认配置，默认 `enabled: false`
5. 使用 Kahn 拓扑排序建立依赖顺序；无法排入拓扑序的插件（循环依赖或依赖了处于循环中的插件）会在加载阶段被依赖检查自然拦截，不影响其余插件
6. 加载 `entrypoint` 指向的 Python 工厂函数
7. 调用 `create_plugin()`，获得 `Plugin` 实例
8. 调用 `plugin.setup(ctx)` 注入上下文；插件在 `setup()` 中自行注册 hook、工具或聊天通道

如果 `setup()` 半途抛错，宿主会清掉已注册的 hook、工具和通道，但不会调用这个尚未完成加载的实例的 `teardown()`；插件需自行回滚在 `setup()` 中已启动的外部资源。

卸载或刷新时：

- 自动移除该插件注册的全部 hook
- 调用 `plugin.teardown()`
- 自动注销该插件发布的 LLM 工具
- 停止并注销该插件注册的聊天通道
- 卸载对应模块

---

## 4. Manifest 字段

`plugin.yaml` 当前支持这些字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 插件唯一标识，必须匹配 `[a-z0-9][a-z0-9_-]*` |
| `name` | 否 | 展示名称；缺省时使用 `id` |
| `version` | 否 | 插件版本；缺省时为 `0.0.0` |
| `api_version` | 是 | 当前必须为 `1` |
| `entrypoint` | 启用时是 | 入口工厂，如 `plugin:create_plugin`；缺失时可被发现，但启用会进入 `broken` |
| `description` | 否 | 描述（会展示在前端管理界面） |
| `author` | 否 | 作者（会展示在前端管理界面） |
| `hooks` | 否 | 声明会用到的 hook 列表，仅用于展示；实际注册以 `setup()` 调用为准 |
| `depends_on` | 否 | 依赖的其他插件 `id` |
| `permissions` | 否 | 申请使用的受控能力；当前识别 5 种权限，其他值仅展示用于审计（见 §6） |
| `config_schema` | 否 | 用于默认值填充、基本校验和前端配置表单自动渲染 |
| `config_ui` | 否 | 启用自定义 HTML 配置面板，详见 §5.1 |

示例：

```yaml
id: reply_filter
name: Reply Filter
version: 1.0.0
api_version: 1
entrypoint: plugin:create_plugin
description: Filter blocked words before send
author: acme
hooks:
  - reply.pre_send
permissions:
  - messaging.send
config_schema:
  type: object
  properties:
    blocked_words:
      type: array
      title: 屏蔽词
      description: 每行一个，匹配时阻止发送
      items:
        type: string
      default: []
```

---

## 5. 配置文件

插件配置文件位于：

```text
data/plugins/{directory_name}/config.yaml
```

结构固定为：

```yaml
enabled: false
settings:
  some_key: some_value
```

说明：

- `enabled`：是否启用插件
- `settings`：插件业务配置

`config_schema` 当前支持 JSON Schema 的一个精简子集。

**校验字段：**

- `type`、`properties`、`required`、`default`、`items`

**支持的基础类型：**

- `object`、`string`、`boolean`、`integer`、`number`、`array`

**UI hint 字段（可选）：**

前端管理界面会根据 `config_schema` 自动渲染配置表单。以下字段不影响后端校验，仅供前端展示：

| hint | 说明 | 适用类型 |
| --- | --- | --- |
| `title` | 控件标签，回退为属性 key | 所有 |
| `description` | 控件下方提示文字 | 所有 |
| `placeholder` | 输入框占位文本 | string / number / integer |
| `secret` | 为 `true` 时渲染为密码框 | string |
| `multiline` | 为 `true` 时渲染为多行文本域 | string |
| `enum` | 可选值列表，渲染为下拉框 | string |
| `enum_labels` | `{值: 显示名}` 映射 | string + enum |
| `minimum` / `maximum` | 数字范围约束 | integer / number |
| `order` | 字段排序权重，数字越小越靠前 | 所有 |

后端只校验上面列出的类型、必填项与数组元素；`enum`、`minimum`、`maximum` 等是前端控件提示，不会由后端再次约束。Schema 未声明的额外 `settings` 字段目前也会被保留。

`secret: true` 仅改变前端输入框外观，不会加密存储；对应值仍明文保存在插件 `config.yaml`，并可由仅限本地的插件详情 API 和自定义配置面板读取。

示例：

```yaml
config_schema:
  type: object
  properties:
    api_key:
      type: string
      title: API Key
      secret: true
      placeholder: 输入你的 API Key
    mode:
      type: string
      title: 运行模式
      enum: [strict, relaxed]
      enum_labels:
        strict: 严格模式
        relaxed: 宽松模式
      default: relaxed
    max_retries:
      type: integer
      title: 最大重试次数
      minimum: 0
      maximum: 10
      default: 3
```

---

### 5.1 自定义 HTML 配置面板（`config_ui`）

如果 `config_schema` 自动渲染的表单不能满足需求（例如需要分组、向导、即时预览、自定义控件），可以在 `plugin.yaml` 中声明 `config_ui` 字段，由插件提供 HTML 文件渲染配置面板：

```yaml
config_ui: true               # 简写：等价于 entry: index.html, height: auto
```

或显式形式：

```yaml
config_ui:
  entry: index.html           # 相对 ui/ 目录；旧写法 ui/index.html 会被兼容归一化
  height: auto                # 或固定像素值，如 480
```

**渲染机制：**

- 宿主在插件详情页插入 `<iframe sandbox="allow-scripts">`，源地址为 `/api/plugins/{plugin_id}/ui/{entry}`
- iframe 的源是 **null**（沙箱属性不带 `allow-same-origin`）：拿不到 cookie、localStorage、父窗口 DOM、也无法 fetch 宿主 `/api/*`
- 宿主与 iframe 通过 `window.postMessage` 通信，宿主以 `event.source === iframe.contentWindow` 识别消息来源
- 声明 `config_ui` 后，宿主**不再渲染 `config_schema` 表单**和默认的“保存配置”按钮——保存动作完全由 iframe 内部触发
- `config_schema` 仍用于默认值和已声明字段的类型校验；它不会拒绝未声明的额外字段
- 静态资源路由 `GET /api/plugins/{plugin_id}/ui/<path>` 仅限本地访问，且只对声明了 `config_ui` 的插件开放，路径会做防越目录校验

**postMessage 协议：**

iframe → 宿主：

| `type` | 字段 | 说明 |
| --- | --- | --- |
| `ready` | — | iframe 加载完成，请求初始数据 |
| `save` | `id`, `settings` | 请求保存（宿主调 `PATCH /api/plugins/{id}/config`） |
| `resize` | `height` | 当 `height: auto` 时，告知宿主调整 iframe 高度 |
| `toast` | `level`, `message` | 弹出宿主级 toast（`success` / `error` / `info`） |

宿主 → iframe：

| `type` | 字段 | 说明 |
| --- | --- | --- |
| `init` | `plugin`, `schema`, `settings`, `locale` | 响应 `ready`，下发初始数据 |
| `save-result` | `id`, `ok`, `settings` 或 `error` | 保存结果回执，`id` 与 `save` 请求对应 |

详细的握手代码模板见《PawzoChat 插件开发指南》§14.4。

---

## 6. 权限（`permissions`）

`permissions` 字段声明插件需要的受控能力。宿主当前只对已知权限授予 facade 能力；未知权限会保留在前端详情页中供用户审计，但不会让插件获得额外宿主能力。

当前白名单：

| 权限 | 含义 | 受控 facade |
| --- | --- | --- |
| `messaging.send` | 允许调用 `ctx.messaging.send_message(...)` 主动向会话推送消息（文本 / 图片 / 文件三种内容块都用同一个权限） | `ctx.messaging` |
| `mcp.read` | 允许调用 `ctx.mcp.list_tools()` 和 `ctx.mcp.list_servers()` 枚举当前用户在控制面板配置的 MCP 工具与服务器状态 | `ctx.mcp` 只读方法 |
| `mcp.invoke` | 允许调用 `ctx.mcp.call_tool(name, arguments)` 直接发起 MCP 工具调用（与 LLM 在 tool_use 流程中调用同一底层路径） | `ctx.mcp.call_tool` |
| `mcp.publish` | 允许调用 `ctx.mcp.register_tool(...)` 向 LLM 暴露插件提供的新工具（命名空间 `plugin_<id>__<name>`，由插件代码本地处理） | `ctx.mcp.register_tool` |
| `channel.register` | 允许注册 `plugin:<id>` 聊天通道并提交该通道的入站消息 | `ctx.channels` |

未来新增的受控能力会扩展到这张表。

`mcp.read` 与 `mcp.invoke` 互相独立——只想做工具清单展示的插件应只申请 `mcp.read`，避免给用户造成不必要的授权焦虑；同时声明 `mcp.invoke` 也**不会**自动获得 `mcp.read` 能力，两者必须各自申请。

`mcp.publish` 与上述两个权限同样独立：注册工具的插件不会因此自动获得列举或调用其它工具的能力，按需各自申请即可。注册的工具在插件停用时会被宿主统一注销，详见开发指南 §12.6。

`ctx.llm`（包括显式 provider 调用、按 persona 绑定调用和 provider 枚举）当前**不**受权限门控，插件作者无需声明权限即可进入这些接口。需要注意：截至本文核对版本，`chat_as_persona()` 和 `get_persona_binding()` 会对 `Persona` 数据类误用 `.get()` 而抛 `AttributeError`；修复前可使用 `list_providers()` 配合显式的 `chat()`。

**安全模型说明**：插件本身已运行在主进程内，权限检查不是安全沙箱，而是“能力可见性”——它把插件声明了什么暴露给用户审计，并对宿主提供的受控 facade 做进入许可。前端插件详情页会展示该插件声明的全部权限，用户可在启用前查看。

---

## 7. Hook（hook）列表

当前正式支持 6 个 hook：

| hook | 时机 | 可修改 | 可取消 | 典型用途 |
| --- | --- | --- | --- | --- |
| `message.received` | 收到消息后、入队前 | 是 | 是 | 黑名单、重写 persona、消息过滤 |
| `message.stored` | 用户消息落库后 | 否 | 否 | 审计、统计、外部同步 |
| `context.build` | 调用 LLM 前 | 是 | 否 | 注入上下文、补充 prompt |
| `reply.compose` | LLM 生成回复后 | 是 | 否 | 追加消息、重排消息、补充内容 |
| `reply.pre_send` | 单条消息发送前 | 是 | 是 | 脱敏、替换、拦截 |
| `reply.sent` | 单条消息发送后 | 否 | 否 | 投递日志、通知、统计 |

注意：

- 插件不是通过返回值生效
- 插件应直接修改事件对象
- 取消动作通过 `event.cancel()` 完成
- `message.received` 还可改写 `text`、`images`、`files`、`voices`、`reply_ctx` 等字段；完整字段见开发指南

---

## 8. 运行状态

插件状态有四种：

| 状态 | 含义 | 前端显示 |
| --- | --- | --- |
| `active` | 已成功加载并启用 | 绿色"运行中" |
| `disabled` | 存在但未启用 | 灰色"已停用" |
| `broken` | 发现到了，但加载失败或依赖异常 | 红色"异常" |
| `discovered` | 刚发现、尚未完成本次同步加载流程的内部过渡状态 | 灰色"已发现" |

插件详情会记录：

- `last_error`：最近一次错误信息
- `hooks`：声明的 hook 列表
- `depends_on`：依赖列表

---

## 9. 管理方式

### 前端管理界面

在设置页中点击「插件管理」即可进入插件管理界面：

- **插件列表页**：显示所有已安装插件的名称、描述、版本、状态和启停开关
- **插件详情页**：显示 manifest 信息、启停开关、重载按钮，配置区域根据是否声明 `config_ui` 渲染为自动表单或沙箱 iframe；若插件声明了 `permissions`，会在元信息行展示权限列表供用户审计

列表页顶部始终显示风险警告：安装第三方插件等同于在本机执行不受限的代码，请确认插件来源可信后再启用。PawzoChat 不对第三方插件的安全性负责，使用风险自负。

### 访问控制

插件管理 API **整体仅限本地访问**，公网请求对所有端点（含自定义 UI 静态资源）均返回 403。前端设置页在公网模式下不显示插件管理入口。

### REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/plugins` | 列出所有插件 |
| GET | `/api/plugins/{plugin_id}` | 获取插件详情 |
| POST | `/api/plugins/refresh` | 重新扫描插件目录 |
| POST | `/api/plugins/{plugin_id}/enable` | 启用插件 |
| POST | `/api/plugins/{plugin_id}/disable` | 停用插件 |
| POST | `/api/plugins/{plugin_id}/reload` | 重载插件 |
| PATCH | `/api/plugins/{plugin_id}/config` | 更新插件配置 |
| GET | `/api/plugins/{plugin_id}/ui/{path}` | 仅当声明 `config_ui` 时可用，提供 `ui/` 目录下的静态资源给沙箱 iframe |

当前上述 refresh、enable、disable、reload 和配置 PATCH 最终都会执行一次**全量刷新**，使全部插件经历卸载和重新加载；`/{plugin_id}/reload` 目前不是隔离的单插件热重载。

---

## 10. 边界与限制

当前插件系统的边界：

- 仅支持进程内 Python 运行时插件，不支持独立进程沙箱
- 插件可以通过 `config_ui` 在沙箱 iframe 内渲染自定义配置面板，但不能向宿主主页面注入 JS/CSS，也不能直接修改主界面布局
- 不支持在线安装市场或远程安装
- 不支持 `pip entry points`
- 不暴露完整 `App` 对象
- `plugin_id` 必须为小写字母、数字、下划线和连字符，以字母或数字开头
- 多个目录声明相同 `plugin_id` 时，仅加载目录名排序在前的那个，后续重复的将被跳过
- 循环依赖不会导致所有插件崩溃；环上的插件及其下游依赖者会各自得到准确的错误信息（"Dependencies not active"），不影响无关插件
- 插件管理 API 整体仅限本地访问；加载错误的 `last_error` 可能包含入口文件等本地绝对路径，不应把本地 API 响应转发给不受信任方

正常开发入口是受控 `PluginContext`，而不是完整 `App` 对象；这减少了对内部实现的耦合，但不构成强制隔离。

**安全提示**：

- 插件 Python 代码运行在主进程内，拥有完整的 Python 运行时权限。`PluginContext` 的 facade 与 `permissions` 提供的是 API 清晰度和可审计性，不是安全沙箱。安装插件等同于执行不受限的第三方代码
- 自定义 HTML 面板加载在 `sandbox="allow-scripts"`（无 `allow-same-origin`）的 iframe 内，源为 null：拿不到 cookie、localStorage、父窗口、宿主 `/api/*`，与宿主只能通过 `postMessage` 通信。这是浏览器层面的隔离，不是插件 Python 代码的隔离

---

## 11. 当前推荐用法

适合做成插件的能力：

- 回复过滤器
- 自动标签
- 审计记录
- 简单记忆注入
- 外部通知桥接
- 回复发送前后加工
- 定时主动消息（依赖 `messaging.send` 权限 + 插件自管的 `threading.Timer` 等调度器）
- 主动文件投递（如定时把统计报表 PDF 推到某个 persona，需要 `messaging.send`）
- 侧链推理（如归纳长会话写入插件自有 state；当前请用 `ctx.llm.chat` 显式指定 provider/model，persona 便捷接口修复后再用 `chat_as_persona`，均无需权限）
- 自动调用 MCP 工具的工作流（如定时触发 `web_search` 抓资讯并主动推送，需要 `mcp.invoke` + `messaging.send`；如果还要枚举工具，再加 `mcp.read`）
- 新增 Telegram 等第三方聊天通道（需要 `channel.register`）

不适合做成插件的能力：

- 修改或替换内建微信/QQ通道
- 聊天主流程
- 表情包管理
- Web 前端页面结构
- 核心数据存储

判断标准：如果这是产品必备主链路，放核心；如果这是可选扩展点，放插件。
