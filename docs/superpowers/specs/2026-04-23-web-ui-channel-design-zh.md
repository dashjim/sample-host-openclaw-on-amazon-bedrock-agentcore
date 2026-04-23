# Web UI 通道设计：基于 AgentCore WebSocket 的 OpenClaw Gateway Protocol 接入

## Situation（现状）

OpenClaw on AgentCore Runtime 是一个运行在 AWS 上的多通道 AI 对话平台，采用 per-user 无服务器容器架构。目前支持三个消息通道（Telegram、Slack、飞书），均遵循相同的模式：

```
通道 Webhook → Router Lambda → invoke_agent_runtime(HTTP) → 容器 /invocations
                                                                  ↓
                                                            agentcore-contract.js
                                                                  ↓
                                                            OpenClaw Gateway (WS, 端口 18789)
```

容器内运行 `openclaw gateway run --port 18789`，这是一个**完整的 OpenClaw Gateway 服务器**，支持完整的 Gateway Protocol（WebSocket 协议）。然而，当前 `agentcore-contract.js` 的桥接层仅使用了该协议的两个方法：`connect` + `chat.send`（在 `bridgeMessage()` 函数中）。Gateway Protocol 的绝大部分能力（会话管理、对话历史、Agent 文件 CRUD、技能管理、定时任务、审批、工具目录等）在容器内已具备，但完全未暴露给用户。

目前没有 Web UI。用户只能通过 Telegram、Slack 或飞书进行纯文本交互，无法访问高级 Gateway 功能，如会话浏览、工作区文件、对话历史或实时流式响应。

### 现有认证 + 隔离链路分析

当前系统有 5 层安全保障，每层各司其职：

```
  Telegram/Slack/飞书 App
         │
    ① Webhook 签名验证（Telegram secret_token / Slack HMAC-SHA256 / 飞书 AES 解密）
         │
    ② DynamoDB 用户解析 + 白名单检查
         │  Router Lambda: resolve_user("telegram", "123456")
         │  → 查找 CHANNEL#telegram:123456 → USER#user_abc
         │  → 新用户：检查 ALLOW#telegram:123456 是否存在
         │
    ③ AgentCore 平台认证（IAM SigV4）
         │  invoke_agent_runtime(runtimeSessionId, runtimeUserId)
         │
    ④ Per-user Session 隔离
         │  runtimeSessionId → 独立 microVM
         │  每用户一个容器实例，进程级隔离
         │
    ⑤ Scoped STS Credentials（S3 命名空间隔离）
         │  init(userId, actorId, channel)
         │  → namespace = actorId.replace(/:/g, "_")  // "telegram_123456"
         │  → STS AssumeRole + session policy: s3:* 限制到 {namespace}/*
         │  → OpenClaw 进程仅持有范围限定凭证，无容器级 AWS 凭证
```

**Web UI 对各层的复用性评估**：

| 层级 | 现有机制 | Web UI 复用方案 | 改动量 |
|---|---|---|---|
| ① 通道认证 | Webhook 签名（HMAC） | Cognito JWT（SRP / IdP 联邦） | 新增 — 不同认证模式 |
| ② 用户解析 | `resolve_user(channel, id)` + 白名单检查 | **完全复用** — `resolve_user("web", cognito_sub)` | 零 |
| ③ 平台认证 | IAM（Lambda 角色） | OAuth Bearer（AgentCore JWT 授权器） | 配置变更 |
| ④ Session 隔离 | `runtimeSessionId` → per-user microVM | **完全复用** — WS header 中的 `Session-Id` | 零 |
| ⑤ Scoped Credentials | `createScopedCredentials(namespace)` | **完全复用** — namespace = `web_{cognito_sub}` | 零 |

### 现有用户注册与白名单机制

当前系统采用**受控注册**模式（`registration_open` 默认为 `false`）：

```python
# lambda/router/index.py — is_user_allowed()
def is_user_allowed(channel, channel_user_id):
    if REGISTRATION_OPEN:           # cdk.json 配置，默认 false
        return True
    channel_key = f"{channel}:{channel_user_id}"
    resp = identity_table.get_item(Key={"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"})
    return "Item" in resp
```

**注册流程**：
1. 新用户给 Bot 发消息 → Bot 回复拒绝消息并附带用户 ID（如 `telegram:123456`）
2. 管理员执行 `./scripts/manage-allowlist.sh add telegram:123456`
3. 用户再次发消息 → `resolve_user()` 创建 `CHANNEL#` + `USER#` 记录 → 注册完成

**DynamoDB 记录结构**：
- 白名单：`PK=ALLOW#telegram:123456, SK=ALLOW`
- 通道映射：`PK=CHANNEL#telegram:123456, SK=PROFILE` → `{userId: "user_abc"}`
- 用户档案：`PK=USER#user_abc, SK=PROFILE`
- 跨通道绑定绕过白名单检查（已认证用户关联新通道）

### Cognito 在系统中的三重角色

| Cognito Pool | 用途 | 面向对象 | 认证流程 |
|---|---|---|---|
| `openclaw-identity-pool` | **内部组件** — proxy 为每个 actorId 自动创建用户，HMAC 派生密码（`HMAC-SHA256(secret, actorId).slice(0,32)`），生成 JWT 传给 OpenClaw | 不可见 | `AdminInitiateAuth`（服务端） |
| `openclaw-admin-users` | Admin UI 管理控制台登录 | 管理员 | 用户密码认证 |
| `openclaw-web-users`（本方案新建） | Web UI 终端用户登录，支持企业 IdP 联邦 | 终端用户 | SRP / 授权码 / IdP 跳转 |

三个 pool 完全独立，互不干扰。内部 pool 的存在对 Web UI 设计透明 — Web UI 的 JWT 在 API Lambda 层消费，不进入容器内部的 Cognito 链路。

### 研究中发现的关键约束

| 约束 | 详情 |
|---|---|
| **OpenClaw Gateway Protocol** | 仅 WebSocket 传输。JSON 文本帧。完整 RPC 接口：`sessions.*`、`chat.*`、`agents.files.*`、`cron.*`、`skills.*`、`tools.*`、`device.*` 等 |
| **AgentCore Runtime WebSocket** | 原生 `/ws` 端点支持。容器在 8080 端口实现 `/ws` 路径的 WebSocket handler。平台通过 `wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<arn>/ws` 路由浏览器连接。支持 SigV4、Pre-signed URL、OAuth Bearer 认证。通过 `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header 实现会话粘性。32KB 帧大小限制 |
| **容器架构** | OpenClaw Gateway 在 18789 端口（仅 loopback）。Contract 服务器在 8080 端口（`/ping` + `/invocations`）。Proxy 在 18790 端口。Per-user 范围限定 STS 凭证在 `init()` 期间创建 |
| **AgentCore WS 限制** | 平台转发原始 WebSocket 帧到容器。WS 上下文中无 `userId`/`actorId` — 仅有 `session_id` header 可用。**解决方案**：HTTP 引导阶段先通过 `invoke_agent_runtime` 传递身份信息并完成 `init()`，WS 仅需通过相同 `session_id` 路由到已初始化的容器 |
| **Per-user 隔离** | 每个用户拥有独立的 AgentCore session（microVM）。`init(userId, actorId, channel)` 创建命名空间范围限定的 STS 凭证，将 S3/DynamoDB 访问限制在用户前缀内 |
| **Cognito 内部角色** | 现有 `openclaw-identity-pool` 是**纯后端组件** — proxy 使用 HMAC 派生密码自动创建用户，生成 JWT 给 OpenClaw。终端用户从不接触它。Web UI 需要新建面向用户的 Cognito Pool |
| **OpenClaw Gateway 已在容器中** | `openclaw gateway run --port 18789` 是完整 Gateway 服务器。当前 `bridgeMessage()` 仅用 `connect` + `chat.send`（冰山一角）。浏览器无法直连 18789（loopback），需在 8080 `/ws` 做透明桥接 |

---

## Task（目标）

设计一个 Web UI 通道，实现以下目标：

1. **暴露完整的 OpenClaw Gateway Protocol** 给浏览器客户端 — 不仅仅是聊天，还包括会话管理、文件 CRUD（`agents.files.*`）、对话历史（`chat.history`）、定时任务调度、技能管理、自动修复等全部 RPC 族
2. **复用现有的 per-user 隔离体系** — DynamoDB 身份解析、会话管理、范围限定 STS 凭证、S3 命名空间隔离
3. **最小化容器侧改动** — 理想情况下仅在 `agentcore-contract.js` 中添加 `/ws` handler
4. **支持企业 IdP 联邦** — 企业 SAML/OIDC 提供商可即插即用，无需修改内部认证链路
5. **支持实时流式传输** — 逐 token 响应增量、会话事件、在线状态、工具执行进度

### 目标 UI 能力（参考：OpenClaw Control UI）

| 功能 | Gateway Protocol 方法 |
|---|---|
| 流式增量聊天 | `chat.send`、`chat.history`、`chat.abort`、`chat.inject` |
| 会话管理 | `sessions.list`、`sessions.create`、`sessions.delete`、`sessions.compact` |
| 工作区文件 | `agents.files.list`、`agents.files.get`、`agents.files.set` |
| 定时任务 | `cron.list`、`cron.add`、`cron.update`、`cron.remove`、`cron.run` |
| 技能/工具管理 | `skills.status`、`skills.install`、`skills.search`、`tools.catalog` |
| 系统状态 | `health`、`status`、`diagnostics.stability` |
| 模型选择 | `models.list` |
| 配置管理 | `config.get`、`config.set`、`config.patch` |
| 自动修复 | `sessions.steer`、`sessions.abort` |

---

## Action（方案）

### 架构：HTTP 引导 + WebSocket 桥接

核心洞察：通过**将连接生命周期拆分为两个阶段**来解决 WebSocket 身份信息缺失的问题 — 复用现有 HTTP 调用进行身份识别/初始化，然后升级到 WebSocket 进行实时 Gateway Protocol 访问。

```
阶段 1：HTTP 引导（完全复用现有逻辑）
══════════════════════════════════════════════════════

  浏览器                   Web API Lambda              AgentCore 平台
    │                          │                              │
    │── POST /api/session ────→│                              │
    │   (Cognito JWT)          │── resolve_user() ──→ DynamoDB│
    │                          │← user_id, session_id ────────│
    │                          │                              │
    │                          │── invoke_agent_runtime() ───→│
    │                          │   action: "warmup"           │
    │                          │   runtimeSessionId: ses_xxx  │──→ 容器 /invocations
    │                          │   userId, actorId, channel   │    init(userId, actorId, "web")
    │                          │                              │    → 范围限定凭证已创建
    │                          │                              │    → OpenClaw Gateway 已启动
    │                          │←── {status: "ready"} ────────│
    │                          │                              │
    │←── {sessionId, wsUrl} ───│                              │
    │                          │                              │

阶段 2：WebSocket Gateway Protocol（新增，容器侧最小改动）
════════════════════════════════════════════════════════════════════

  浏览器                                AgentCore 平台          容器
    │                                        │                      │
    │── WSS 连接 ──────────────────────────→│                      │
    │   wss://bedrock-agentcore.../ws        │                      │
    │   ?Session-Id=ses_xxx                  │──── WS 升级 ───────→│ /ws handler
    │   (OAuth Bearer token)                 │   (同一 session!)    │
    │                                        │                      │
    │←─────────────── WS 已连接 ─────────────│←─────────────────────│
    │                                        │                      │
    │── Gateway Protocol 帧 ───────────────────────────────────────│
    │   {type:"req", method:"connect", ...}  │                      │──→ ws://127.0.0.1:18789
    │                                        │                      │    (OpenClaw Gateway)
    │←── {type:"res", ok:true, ...} ─────────│←─────────────────────│
    │                                        │                      │
    │── {method:"chat.send", ...} ─────────────────────────────────│──→ OpenClaw Gateway
    │←── {event:"chat", state:"delta"} ────────────────────────────│    完整协议
    │←── {event:"chat", state:"delta"} ────────────────────────────│
    │←── {event:"chat", state:"final"} ────────────────────────────│
    │                                        │                      │
    │── {method:"agents.files.list"} ──────────────────────────────│──→ OpenClaw Gateway
    │←── {files: [...]} ───────────────────────────────────────────│
    │                                        │                      │
    │── {method:"cron.list"} ──────────────────────────────────────│──→ OpenClaw Gateway
    │←── {schedules: [...]} ───────────────────────────────────────│
```

### 组件分解

#### 1. Web 认证 Cognito User Pool（新建 CDK 资源）

一个**独立的、面向用户的 Cognito User Pool**，用于 Web UI 认证。与 proxy 使用的内部 `openclaw-identity-pool` 完全分离。

```python
# stacks/security_stack.py — 新增资源
self.web_user_pool = cognito.UserPool(
    self, "WebUserPool",
    user_pool_name="openclaw-web-users",
    self_sign_up_enabled=False,           # 管理员创建或 IdP 联邦
    sign_in_aliases=cognito.SignInAliases(username=True, email=True),
)

# 联邦：在此接入企业 IdP
# self.web_user_pool.register_identity_provider(
#     cognito.UserPoolIdentityProviderOidc(...)  # Okta, Azure AD 等
#     cognito.UserPoolIdentityProviderSaml(...)  # ADFS 等
# )

self.web_user_pool_client = self.web_user_pool.add_client(
    "WebClient",
    user_pool_client_name="openclaw-web-ui",
    auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
    o_auth=cognito.OAuthSettings(
        flows=cognito.OAuthFlows(authorization_code_grant=True),
        scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.PROFILE],
        callback_urls=["https://<cloudfront-domain>/callback"],
    ),
)
```

**为什么与内部 Cognito 分离**：内部 pool 使用 `AdminCreateUser` + HMAC 派生密码 — 这是机器对机器的模式，不适合人工登录。Web pool 支持标准认证流程（SRP、授权码）和 IdP 联邦。

**企业 IdP 接入路径**：
- **SAML**：注册 `UserPoolIdentityProviderSaml`（ADFS、Azure AD）
- **OIDC**：注册 `UserPoolIdentityProviderOidc`（Okta、Auth0、Google Workspace）
- **社交登录**：Cognito 内置适配器（Google、Facebook、Apple）
- 容器代码零改动 — 无论上游 IdP 是什么，token 格式均为标准 JWT

#### 2. AgentCore Runtime OAuth 授权器（新增配置）

配置 AgentCore Runtime Endpoint 接受 Web Cognito User Pool 的 JWT：

```python
# 通过 AWS SDK（Starter Toolkit 尚未暴露此功能）
client.update_agent_runtime_endpoint(
    agentRuntimeEndpointId=endpoint_id,
    authorizerType="CUSTOM_JWT",
    authorizerConfiguration={
        "customJwtAuthorizer": {
            "discoveryUrl": f"https://cognito-idp.{region}.amazonaws.com/{web_user_pool_id}",
            "allowedAudience": [web_user_pool_client_id],
        }
    },
)
```

这使浏览器可以直接使用 Cognito JWT 连接 AgentCore WebSocket — HTTP 引导阶段通过 API Lambda（IAM 签名），WebSocket 直连阶段通过 OAuth Bearer。

#### 3. Web API Lambda（新建，基于现有 Router Lambda 的薄层封装）

处理 HTTP 引导阶段的轻量 Lambda。复用 Router Lambda 的核心函数（用户解析、会话管理、AgentCore 调用），但使用 Web 专属认证：

```python
# lambda/web_api/index.py

def handler(event, context):
    """Web UI API — 会话引导和用户管理"""
    path = event["rawPath"]
    method = event["requestContext"]["http"]["method"]

    if method == "POST" and path == "/api/session":
        return handle_create_session(event)
    if method == "GET" and path == "/api/session":
        return handle_get_session(event)
    if method == "POST" and path == "/api/link":
        return handle_link_channel(event)

def handle_create_session(event):
    """引导：解析用户 → 预热 AgentCore → 返回会话信息"""
    # 1. 提取 Cognito JWT claims
    jwt_claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    cognito_sub = jwt_claims["sub"]
    actor_id = f"web:{cognito_sub}"

    # 2. 在 DynamoDB 中解析或创建用户（复用现有逻辑）
    user_id = resolve_user(actor_id)

    # 3. 获取或创建 AgentCore session（复用现有逻辑）
    session_id = get_or_create_session(user_id)

    # 4. 预热容器（触发 init，建立身份 + 范围限定凭证）
    invoke_agent_runtime(
        session_id=session_id,
        user_id=user_id,
        actor_id=actor_id,
        channel="web",
        message=None,
        action="warmup",
    )

    # 5. 返回会话信息，供 WebSocket 连接使用
    return {
        "statusCode": 200,
        "body": json.dumps({
            "sessionId": session_id,
            "wsEndpoint": f"wss://bedrock-agentcore.{REGION}.amazonaws.com"
                          f"/runtimes/{RUNTIME_ARN}/ws",
            "runtimeArn": RUNTIME_ARN,
            "status": "ready",
        }),
    }
```

API Gateway HTTP API 配合 Cognito JWT 授权器 — 无需自定义认证代码。

#### 4. 容器 `/ws` Handler（`agentcore-contract.js` 最小改动）

唯一的容器侧改动：从 AgentCore `/ws` 到 OpenClaw Gateway 18789 端口的透明 WebSocket-to-WebSocket 桥接。

```javascript
// agentcore-contract.js — 新增 /ws handler（添加到现有 HTTP 服务器）

server.on("upgrade", (req, socket, head) => {
  if (req.url !== "/ws") {
    socket.destroy();
    return;
  }

  // 容器已通过 HTTP warmup 完成初始化 — 直接桥接
  if (!openclawReady) {
    socket.write("HTTP/1.1 503 Service Unavailable\r\n\r\n");
    socket.destroy();
    return;
  }

  // 创建到 OpenClaw Gateway 的上游连接
  const upstream = new WebSocket(`ws://127.0.0.1:${OPENCLAW_PORT}`, {
    origin: `http://127.0.0.1:${OPENCLAW_PORT}`,
  });

  // 接受下游连接（AgentCore 平台 → 浏览器）
  const wss = new WebSocket.Server({ noServer: true });
  wss.handleUpgrade(req, socket, head, (downstream) => {
    // 双向帧转发 — 零解析、零转换
    downstream.on("message", (data) => {
      if (upstream.readyState === WebSocket.OPEN) {
        upstream.send(data);
      }
    });
    upstream.on("message", (data) => {
      if (downstream.readyState === WebSocket.OPEN) {
        downstream.send(data);
      }
    });

    // 生命周期管理
    downstream.on("close", () => upstream.close());
    upstream.on("close", () => downstream.close());
    downstream.on("error", () => upstream.close());
    upstream.on("error", () => downstream.close());
  });
});
```

关键设计决策：
- **零帧解析** — 原始双向转发。Gateway Protocol 在浏览器和 OpenClaw Gateway 之间；桥接层完全透明
- **桥接层无认证逻辑** — 认证已在两层完成：AgentCore 平台（OAuth JWT）和 OpenClaw Gateway（`connect` 握手时的 token 认证）
- **无 init 逻辑** — HTTP 引导阶段保证容器在 WebSocket 连接前已完成初始化
- **32KB AgentCore 帧限制** — OpenClaw Gateway 自身 `maxPayload` 为 25MB，但 AgentCore 限制为 32KB。大型 `agents.files.set` 负载需要客户端分块（在客户端 SDK 层面处理）

#### 5. Web UI 前端（S3+CloudFront 上的 React SPA）

沿用现有 admin-ui 基础设施模式。前端通过 AgentCore WebSocket 桥接连接到 OpenClaw Gateway Protocol。

```
admin-ui/           （现有 — 管理控制台）
web-ui/             （新建 — 面向用户的聊天 + 工作区 UI）
  src/
    services/
      auth.ts         # Cognito 认证（Amplify v6，与 admin-ui 相同模式）
      gateway.ts      # Gateway Protocol WebSocket 客户端
      bootstrap.ts    # HTTP 会话引导（POST /api/session）
    pages/
      Chat.tsx        # 带流式增量的聊天界面
      Sessions.tsx    # 会话列表、创建、删除、压缩
      Files.tsx       # 工作区文件浏览器（agents.files.*）
      Cron.tsx        # 定时任务管理（cron.*）
      Skills.tsx      # 技能浏览器和安装器
    components/
      MessageStream.tsx   # 实时增量渲染
      FileEditor.tsx      # 浏览器内文件查看/编辑器
      CronScheduler.tsx   # 可视化 cron 表达式构建器
```

部署方式：与 `admin-ui/` 相同的 S3+CloudFront+OAC 模式，独立 distribution。

#### 6. CDK Stack 变更

```python
# 新建 stack 或扩展 admin_stack.py
class WebUiStack(Stack):
    """Web UI 通道 — Cognito（面向用户）、API Lambda、S3+CloudFront"""

    def __init__(self, scope, id, *, security_stack, router_stack, agentcore_stack, **kwargs):
        super().__init__(scope, id, **kwargs)

        # 1. Web Cognito User Pool（面向用户，支持 IdP 联邦）
        #    与内部 openclaw-identity-pool 分离

        # 2. API Gateway HTTP API + Cognito JWT 授权器
        #    路由：POST /api/session、GET /api/session、POST /api/link

        # 3. Web API Lambda（薄层封装 — 复用 router 逻辑）

        # 4. S3 桶 + CloudFront distribution（React SPA）
        #    OAC 访问 S3 origin，与 admin-ui 相同模式

        # 5. 输出：CloudFront 域名、Cognito 配置、WS 端点
```

#### 7. Web 用户注册与白名单（复用现有机制 + 新增管理脚本）

Web UI 用户注册复用现有的 DynamoDB 白名单机制，与 Telegram/Slack/飞书完全一致。

**注册流程设计**：

```
场景 A：管理员预注册（推荐，与现有通道一致）
═══════════════════════════════════════════════

  1. 管理员创建 Cognito 用户
     $ ./scripts/manage-web-users.sh add user@company.com

     → 在 openclaw-web-users pool 中创建用户
     → 获取 cognito_sub（Cognito 自动生成的 UUID）
     → 在 DynamoDB 写入 ALLOW#web:{cognito_sub}
     → 发送临时密码邮件（Cognito 自动）

  2. 用户首次登录 Web UI
     → Cognito 强制修改密码
     → POST /api/session（JWT 中携带 sub）
     → Web API Lambda: resolve_user("web", cognito_sub)
       → 检查 ALLOW#web:{cognito_sub} ✓
       → 创建 CHANNEL#web:{sub} + USER#user_xxx
     → 返回 sessionId → WebSocket 连接

场景 B：企业 IdP 联邦（自动注册）
═══════════════════════════════════

  1. 管理员配置 Cognito IdP 联邦（Okta/Azure AD/SAML）
  2. 管理员批量添加白名单
     $ ./scripts/manage-web-users.sh add-batch user-list.csv

     → CSV 包含预期的 IdP 用户邮箱
     → 脚本预计算 Cognito sub（或首次登录时自动匹配）

  3. 用户通过 IdP 登录
     → Cognito Hosted UI → 跳转企业 IdP → 认证 → 回调
     → POST /api/session → resolve_user("web", cognito_sub)
     → 白名单检查 → 注册 → 正常使用

场景 C：开放注册（可选）
═════════════════════════

  cdk.json: "registration_open": true
  → is_user_allowed() 直接返回 true
  → 任何通过 Cognito 认证的用户均可注册
  → 适用于内部测试或企业 IdP 已做准入控制的场景
```

**管理脚本**（`scripts/manage-web-users.sh`）：

```bash
#!/bin/bash
# Web UI 用户管理 — 创建 Cognito 用户 + DynamoDB 白名单
#
# 用法:
#   ./scripts/manage-web-users.sh add user@company.com     # 添加用户
#   ./scripts/manage-web-users.sh remove user@company.com  # 移除用户
#   ./scripts/manage-web-users.sh list                     # 列出所有 Web 用户
#   ./scripts/manage-web-users.sh add-batch users.csv      # 批量添加

cmd_add() {
    local email="$1"

    # 1. 在 Web Cognito User Pool 中创建用户
    local cognito_sub
    cognito_sub=$(aws cognito-idp admin-create-user \
        --user-pool-id "$WEB_USER_POOL_ID" \
        --username "$email" \
        --user-attributes Name=email,Value="$email" Name=email_verified,Value=true \
        --region "$REGION" \
        --query 'User.Attributes[?Name==`sub`].Value' \
        --output text)

    # 2. 添加到 DynamoDB 白名单
    aws dynamodb put-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        --item "{
            \"PK\": {\"S\": \"ALLOW#web:${cognito_sub}\"},
            \"SK\": {\"S\": \"ALLOW\"},
            \"channelKey\": {\"S\": \"web:${cognito_sub}\"},
            \"email\": {\"S\": \"${email}\"},
            \"addedAt\": {\"S\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}
        }"

    echo "Done. $email (web:$cognito_sub) 已添加到白名单。"
    echo "用户将收到临时密码邮件，首次登录时需修改密码。"
}
```

**白名单 DynamoDB 记录**（与现有格式完全一致）：

| PK | SK | 附加字段 |
|---|---|---|
| `ALLOW#web:abc123-def456` | `ALLOW` | `email`, `addedAt` |

**关键设计决策**：
- Web 用户的 `actorId` 格式为 `web:{cognito_sub}`，与 `telegram:{user_id}` 平行
- 复用 `resolve_user(channel, channel_user_id)` — 第一个参数传 `"web"`，第二个传 `cognito_sub`
- 白名单检查走相同的 `is_user_allowed("web", cognito_sub)` 路径
- 管理脚本风格与现有 `manage-allowlist.sh` 保持一致
- 企业 IdP 场景：如果 IdP 本身已做准入控制（如仅限公司员工），可设 `registration_open: true` 省去白名单步骤

### 认证流程（完整链路）

```
浏览器                      Cognito             API Lambda          AgentCore        容器
  │                           │                     │                  │               │
  │── 登录 (SRP/IdP) ───────→│                     │                  │               │
  │←── JWT (IdToken) ─────────│                     │                  │               │
  │                           │                     │                  │               │
  │── POST /api/session ─────────────────────────→ │                  │               │
  │   Authorization: Bearer <JWT>                   │                  │               │
  │                         [API GW 验证 JWT]       │                  │               │
  │                                                 │── warmup ──────→│               │
  │                                                 │  (IAM SigV4)    │──/invocations→│
  │                                                 │                  │  init(userId) │
  │                                                 │                  │  scopedCreds  │
  │←── {sessionId, wsEndpoint} ─────────────────────│                  │               │
  │                                                 │                  │               │
  │── WSS 连接 ─────────────────────────────────────────────────────→ │               │
  │   Sec-WebSocket-Protocol:                       │                  │               │
  │     base64UrlBearerAuthorization.<JWT>          │                  │               │
  │   ?Session-Id=ses_xxx                           │[OAuth 验证]      │               │
  │                                                 │                  │── /ws ───────→│
  │←── WS 已连接 ───────────────────────────────────────────────────────────────────── │
  │                                                 │                  │               │
  │── Gateway Protocol ─────────────────────────────────────────────────────────────→ │
  │   (connect → chat.send → sessions.list → ...)   │                  │  ↕ 桥接 ↕     │
  │←── Gateway Protocol 事件 ──────────────────────────────────────────────────────── │
  │                                                 │                  │  ws://18789   │
```

**4 层安全保障**（与现有通道对等）：

| 层级 | Telegram/Slack | Web UI |
|---|---|---|
| 通道认证 | Webhook 签名（HMAC） | Cognito JWT（SRP / IdP 联邦） |
| 平台认证 | IAM（Lambda 角色） | OAuth Bearer（AgentCore JWT 授权器） |
| 会话隔离 | `runtimeSessionId` → per-user microVM | 相同 — WS header 中的 `runtimeSessionId` |
| 数据隔离 | 范围限定 STS（S3 命名空间） | 相同 — `init()` 创建范围限定凭证 |
| 内部认证 | Cognito HMAC token（proxy→OpenClaw） | 相同 — proxy 自动创建 |

### 跨通道绑定

Web 用户可绑定到现有 Telegram/Slack 账号：

1. 用户在 Web UI 中说"关联账号"
2. OpenClaw 生成 6 位绑定码（复用现有逻辑，存储在 DynamoDB 中，10 分钟 TTL）
3. 用户在 Telegram/Slack 输入该绑定码
4. 两个通道映射到同一个 `USER#user_abc` — 共享会话、工作区、文件、定时任务

此外，Web UI 提供 `/api/link` 端点用于编程式绑定。

---

## Result（成果）

### 本设计实现的能力

| 能力 | 状态 |
|---|---|
| 浏览器访问完整 Gateway Protocol | 通过 WS 桥接暴露所有 RPC 族 |
| 实时流式增量聊天 | `chat.send` → `session.message` 事件 |
| 会话管理（创建/列表/删除/压缩） | `sessions.*` 方法 |
| 工作区文件浏览/编辑 | `agents.files.list/get/set` |
| 定时任务调度 UI | `cron.list/add/update/remove/run` |
| 技能/工具管理 | `skills.status/install/search`、`tools.catalog` |
| 自动修复 / 干预 | `sessions.steer`、`sessions.abort` |
| 对话历史 | `chat.history`（显示规范化） |
| 企业 IdP 联邦 | Cognito SAML/OIDC → 容器零改动 |
| 跨通道身份绑定 | 复用现有 `link` 机制，共享工作区 |
| Per-user S3 隔离 | 复用现有范围限定 STS 凭证，无改动 |

### 变更影响范围

| 组件 | 变更类型 | 工作量 |
|---|---|---|
| `bridge/agentcore-contract.js` | 新增 `/ws` handler（约 50 行） | 小 |
| `stacks/security_stack.py` | 新增 Web Cognito User Pool | 小 |
| `stacks/web_ui_stack.py` | 新建 stack（API GW + Lambda + S3/CF） | 中 |
| `lambda/web_api/index.py` | 新建 Lambda（复用 router 逻辑） | 中 |
| `scripts/manage-web-users.sh` | 新建管理脚本（Cognito + 白名单） | 小 |
| AgentCore Endpoint 配置 | 添加 CUSTOM_JWT 授权器 | 仅配置 |
| `web-ui/` | 新建 React SPA | 大（但独立） |
| 现有通道 | **零改动** | 无 |
| `bridge/agentcore-proxy.js` | **零改动** | 无 |
| `bridge/lightweight-agent.js` | **零改动** | 无 |
| 范围限定凭证 | **零改动** | 无 |

### 不在范围内

- 语音/音频（WebRTC） — 未来阶段
- 多用户协作会话 — 单用户单会话
- WebSocket 内的 Canvas 渲染 — `canvas` 工具保持禁用
- 替换 Telegram/Slack 通道 — Web UI 是增量新增

---

## 附录：Q&A

### Q1: OpenClaw Gateway 到底在不在 Docker 容器里？

**在。** `openclaw gateway run --port 18789` 就是完整的 OpenClaw Gateway 服务器。Dockerfile 安装 `openclaw@2026.3.8`（全局 npm），`agentcore-contract.js` 在 `init()` 时 spawn 这个进程。当前的 `bridgeMessage()` 只用了 `connect` + `chat.send` 两个方法，但 Gateway 原生支持完整的 Protocol surface。

### Q2: 为什么不能让浏览器直接 WebSocket 连到 OpenClaw Gateway（18789）？

因为 OpenClaw Gateway 绑定 `127.0.0.1:18789`（容器内部 loopback）。AgentCore 平台只暴露容器的 8080 端口（`/ping`、`/invocations`、`/ws`）。所以必须在 8080 的 `/ws` 做一层透明桥接。

### Q3: AgentCore WebSocket 的 32KB 帧限制会影响什么？

主要影响 `agents.files.set`（写文件）和 `chat.history`（长对话历史）。解决方案：
- 文件写入：客户端分块发送，每块 < 32KB
- 历史读取：使用 `sessions.preview` 的有界预览，或分页加载
- 普通聊天消息很少超过 32KB

OpenClaw Gateway 自身的 `maxPayload` 是 25MB（`hello-ok.policy`），所以限制来自 AgentCore 平台层。

### Q4: 企业 IdP 如何对接？改动在哪里？

仅 CDK 配置变更，零容器代码改动：

```python
# stacks/security_stack.py
okta_provider = cognito.UserPoolIdentityProviderOidc(
    self, "OktaProvider",
    user_pool=self.web_user_pool,
    client_id="<okta-client-id>",
    client_secret="<okta-client-secret>",
    issuer_url="https://your-company.okta.com",
    scopes=["openid", "profile", "email"],
    attribute_mapping=cognito.AttributeMapping(
        email=cognito.ProviderAttribute.other("email"),
        fullname=cognito.ProviderAttribute.other("name"),
    ),
)
```

支持的 IdP 类型：
- **SAML 2.0**：ADFS、Azure AD、OneLogin
- **OIDC**：Okta、Auth0、Google Workspace、Keycloak
- **社交登录**：Google、Facebook、Apple、Amazon（Cognito 内置）

用户登录流程：浏览器 → Cognito Hosted UI → 跳转企业 IdP → 认证 → 回调 → JWT token → 一切如常。

### Q5: 新用户如何注册？需要管理员操作吗？

**需要管理员操作**（默认模式），与 Telegram/Slack 完全一致。流程：

```
管理员                              用户
  │                                  │
  │── manage-web-users.sh add ──→   │  (Cognito 创建用户 + DynamoDB 白名单)
  │   user@company.com              │
  │                                  │←── 收到临时密码邮件
  │                                  │
  │                                  │── 首次登录 Web UI
  │                                  │   (强制修改密码)
  │                                  │── POST /api/session
  │                                  │   → resolve_user("web", sub)
  │                                  │   → ALLOW#web:{sub} 检查通过 ✓
  │                                  │   → 创建 USER# 记录
  │                                  │── WebSocket 连接 → 正常使用
```

**三种注册模式**：
- **管理员预注册**（默认）：`manage-web-users.sh add email` → 创建 Cognito 用户 + 白名单
- **企业 IdP 联邦**：管理员批量导入白名单，用户通过 IdP SSO 登录
- **开放注册**：`registration_open: true` → 任何通过 Cognito 认证的用户均可注册（适用于 IdP 已做准入控制的场景）

Web 用户独立于 Telegram/Slack，actorId 格式为 `web:<cognito_sub>`。如果用户同时有 Telegram 账号，可通过 `link` 命令绑定 — 两个通道共享同一个 user profile、session、workspace、files、cron。

### Q6: Cognito 在系统中有三个角色，分别是什么？

| Cognito Pool | 用途 | 用户可见？ | IdP 联邦？ |
|---|---|---|---|
| `openclaw-identity-pool` | 内部 — proxy 为每个 actorId 自动创建用户，HMAC 密码，生成 JWT 给 OpenClaw | 不可见 | 不需要 |
| `openclaw-admin-users` | Admin UI 登录 | Admin 可见 | 可选 |
| `openclaw-web-users`（新建） | Web UI 终端用户登录 | 用户可见 | 推荐 — 企业 IdP |

三个 pool 完全独立，互不干扰。

### Q7: 现有的 Scoped Credentials 如何被复用？

完全不需要改动。流程：

1. HTTP 引导调用 `invoke_agent_runtime(action:"warmup", userId, actorId:"web:sub_xxx")`
2. 容器 `init("user_abc", "web:sub_xxx", "web")` → `namespace = "web_sub_xxx"`
3. `createScopedCredentials("web_sub_xxx")` → STS session policy：`s3:*` 限制到 `web_sub_xxx/*`
4. 后续 WebSocket 上的 `agents.files.*` 操作都在 OpenClaw 内执行，受范围限定凭证约束

S3 目录结构：`s3://openclaw-user-files-{account}-{region}/web_sub_xxx/`

### Q8: 如果浏览器断开 WebSocket 重连会怎样？

- **Session 存活**：AgentCore session 有空闲超时（默认 15 分钟），WS 断开不会立即销毁 session
- **重连流程**：浏览器直接带相同 `session_id` 重新 WSS 连接 → 路由到同一容器 → `/ws` 桥接重建到 OpenClaw Gateway → Gateway 协议重新握手（`connect`）
- **状态恢复**：OpenClaw Gateway 维护会话状态，重连后 `chat.history` 返回完整对话历史
- **不需要重新引导**：容器已初始化，范围限定凭证有效（45 分钟刷新），直接 WS 重连即可

### Q9: HTTP 引导的 warmup 和现有 Telegram 流程有什么区别？

几乎没有区别：

| 步骤 | Telegram | Web UI |
|---|---|---|
| 用户解析 | Router Lambda `resolve_user("telegram:123")` | Web API Lambda `resolve_user("web:sub_xxx")` |
| Session 创建 | `get_or_create_session(user_id)` | 相同 |
| AgentCore 调用 | `invoke_agent_runtime(action:"chat")` | `invoke_agent_runtime(action:"warmup")` |
| 容器 init | `init(userId, "telegram:123", "telegram")` | `init(userId, "web:sub_xxx", "web")` |
| 范围限定凭证 | `createScopedCredentials("telegram_123")` | `createScopedCredentials("web_sub_xxx")` |
| 后续交互 | HTTP invocation（chat action） | WebSocket（Gateway Protocol） |

唯一区别：Telegram 后续消息也走 HTTP invocation，Web UI 后续消息走 WebSocket 直连。

### Q10: 现有通道（Telegram/Slack/飞书）是否受影响？

**零影响。** Web UI 是一个完全独立的新通道：
- 独立的 Cognito User Pool
- 独立的 API Gateway
- 独立的 Lambda
- 容器侧只新增 `/ws` handler，不修改 `/invocations`
- 现有 `bridgeMessage()`、`chat` action、`cron` action 全部保持不变

### Q11: 为什么用 HTTP 引导 + WS 两阶段，而不是纯 WebSocket？

核心原因：AgentCore WebSocket 协议**不携带 userId/actorId**，只有 `session_id`。

如果纯 WS，容器 `/ws` handler 收到的是匿名帧 — 不知道是谁，无法调用 `init(userId, actorId, channel)` 创建 scoped credentials。

HTTP 引导解决了这个问题：
1. `invoke_agent_runtime(action:"warmup")` 的 payload 中包含 `userId`、`actorId`、`channel`
2. 容器完成 `init()` → scoped credentials 已就位、OpenClaw Gateway 已启动
3. WS 通过相同 `session_id` 路由到同一个已初始化的容器 → 无需再传身份
4. 后续所有 Gateway Protocol 帧直接桥接，容器侧零解析

**额外好处**：HTTP 引导完全复用了现有 Router Lambda 的 `resolve_user()` + `get_or_create_session()` + `invoke_agent_runtime()` 三个核心函数，不需要在容器侧重新实现用户解析和白名单检查逻辑。

### Q12: 企业 IdP 对接需要改动容器代码吗？

**完全不需要。** IdP 变更仅影响 Cognito 配置（CDK 层面）：

```
用户 → Cognito Hosted UI → 企业 IdP（Okta/Azure AD/ADFS）→ 认证
  → 回调 Cognito → JWT（标准格式，sub 来自 IdP 映射）
  → API Lambda 消费 JWT → invoke_agent_runtime → 容器
```

容器收到的始终是 `{action:"warmup", userId:"user_abc", actorId:"web:xxx"}` — 不关心 JWT 是 Cognito 原生认证还是 IdP 联邦产生的。三个 Cognito Pool（内部、Admin、Web）完全独立运作，互不干扰。
