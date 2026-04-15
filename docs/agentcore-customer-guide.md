# OpenClaw on AgentCore Runtime：客户选型注意事项

## 1. AgentCore 8 小时 Runtime 上限的影响

### 1.1 "8 小时"是什么意思？

AgentCore Runtime 的 `session_max_lifetime` 配置为 **28800 秒（8 小时）**。这是**单个容器实例的最大存活时间**，不是用户的使用时长限制。

**关键理解**：这 8 小时是指收到单个用户消息/指令/任务后，该容器可以连续运行的最长时间。当前来看，AI Agent 也还不具备超过 8 小时自主运行的能力——即使是最复杂的 deep-research 任务，通常也在几十分钟内完成。

### 1.2 解耦架构：Channel 7x24 在线

我们将 OpenClaw Gateway 从 Channel 入口解耦到 Router Lambda，Runtime 容器中只运行 OpenClaw Agent。**用户可以随时给龙虾发消息，Channel 是 7x24 在线的。**

```
用户 (Telegram/Slack/飞书)
  │ 随时可发消息（7x24）
  ▼
API Gateway HTTP API + Router Lambda（始终在线，毫秒级响应）
  │ 1. 验证 Webhook 签名（Telegram/Slack HMAC/飞书 AES 加密）
  │ 2. 立即返回 200（防止 Channel 超时重试）
  │ 3. 异步自调用（InvocationType=Event）处理消息
  ▼
DynamoDB 身份表（用户→会话映射）
  │ 查找或创建用户的 AgentCore Session
  │ 确定性 userId：sha256(channel:platform_id)[:16]
  ▼
AgentCore Runtime（按需启动的每用户 MicroVM 容器）
  │ 容器不在？→ 自动创建新 MicroVM，从 S3 恢复工作区
  │ 容器空闲？→ 直接路由到已有容器（毫秒级）
  ▼
用户收到回复（Telegram/Slack/飞书）
```

**核心设计**：Router Lambda 是无状态的——它只负责 Webhook 验证、身份解析和转发。所有有状态的逻辑（Agent 上下文、工作区、记忆）都在 AgentCore 容器内。这意味着：

- **Channel 层不会宕机**：Lambda + API Gateway 的可用性 > 99.9%
- **容器回收不影响消息接收**：用户发消息时如果容器已回收，Lambda 会触发新容器创建
- **多 Channel 统一路由**：同一用户绑定 Telegram + Slack 后，消息路由到同一个 Agent 会话

### 1.3 三个关键时间参数

| 参数 | 默认值 | 含义 | 可配置 |
|---|---|---|---|
| `session_idle_timeout` | 1800 秒（30 分钟） | 无消息后容器自动回收 | cdk.json |
| `session_max_lifetime` | 28800 秒（8 小时） | 容器最大存活时间硬上限 | cdk.json |
| `workspace_sync_interval` | 300 秒（5 分钟） | 工作区 S3 同步间隔 | 环境变量 |

### 1.4 容器生命周期详解

```
                              ┌──────────────────────────────────────────────┐
                              │          容器生命周期（最长 8 小时）            │
                              │                                              │
用户发消息 ──► 容器启动(~5-6s)  │  Proxy就绪(~5s)    OpenClaw就绪(~1-2min)      │
              │               │       │                    │                  │
              │  恢复.openclaw/│       │ 轻量代理立即响应    │ 全功能代理接管     │
              │  从 S3         │       ▼                    ▼                  │
              │               │  ┌─────────┐         ┌──────────┐            │
              ▼               │  │轻量代理   │ ──────► │ OpenClaw │            │
                              │  │(17个工具) │  切换   │(全部技能) │            │
                              │  └─────────┘         └──────────┘            │
                              │                                              │
                              │  每5分钟自动同步 .openclaw/ 到 S3              │
                              │                                              │
                              │             空闲30分钟 或 达到8小时             │
                              └───────────────────┬──────────────────────────┘
                                                  │
                                                  ▼
                                            SIGTERM（10秒窗口）
                                            ├── 保存工作区到 S3
                                            ├── 关闭浏览器会话
                                            └── 停止子进程
                                                  │
                                                  ▼ 容器销毁
                                                  │
用户再次发消息 ──► 新容器启动 ──► 从S3恢复 ──────────┘（对用户透明）
```

**对用户的体验**：

| 场景 | 延迟 | 说明 |
|---|---|---|
| 容器在运行中 | **< 1 秒** | 直接路由到已有容器 |
| 容器已回收（冷启动） | **5-6 秒** | AgentCore MicroVM 快速拉起 |
| 冷启动后 Proxy 先就绪 | **~5 秒后可回复** | 轻量代理先响应，OpenClaw 后台启动 |
| 8 小时后重建 | **同冷启动** | 工作区从 S3 恢复，用户无感 |

### 1.5 "HealthyBusy"：防止长任务被中断

当 Agent 正在处理长任务时（如 deep-research），容器向 AgentCore 报告 `HealthyBusy` 状态，**阻止空闲回收**：

```js
// agentcore-contract.js — /ping 健康检查
const status = activeTaskCount > 0 ? "HealthyBusy" : "Healthy";
// HealthyBusy → AgentCore 延长超时，不回收
// Healthy     → 可被空闲回收
```

即使用户 30 分钟没发新消息，只要 Agent 还在处理之前的任务，容器就不会被回收。

---

## 2. 数据同步与持久化

### 2.1 AgentCore Session Storage（推荐）

AgentCore 原生提供 **Session Storage**——全托管的持久化文件系统，无需自建同步逻辑。

> **文档**：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-persistent-filesystems.html
> **状态**：Preview

**核心特性**：
- **全托管**：无需编写同步代码，AgentCore 自动管理数据持久化
- **POSIX 兼容**：挂载为本地目录，支持标准文件操作（read/write/mkdir/rename/chmod），兼容 git、npm、pip 等工具
- **Session 级隔离**：每个 Session 只能访问自己的存储，无法跨 Session 读写
- **透明复制**：文件操作异步复制到持久存储，Session 停止时自动 flush 未持久化数据
- **跨 Stop/Resume 持久**：停止 Session 后重新启动，文件系统状态完整恢复（源码、node_modules、.git 历史全部保留）

**配置方式**：

创建 Agent Runtime 时添加 `filesystemConfigurations`：

```python
client.create_agent_runtime(
    agentRuntimeName="my-agent",
    roleArn=role_arn,
    agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
    filesystemConfigurations=[{
        "sessionStorage": {
            "mountPath": "/mnt/workspace"
        }
    }]
)
```

Starter Toolkit 同样支持，配置 `filesystem_configurations` 参数即可。

**生命周期**：

| 事件 | 行为 |
|---|---|
| 首次 invoke | 新 Session，挂载空目录 |
| Agent 写文件 | 异步复制到持久存储（对 Agent 透明） |
| Session 停止 | flush 所有未持久化数据，计算资源销毁 |
| 同一 Session 恢复 | 新计算资源，文件系统从持久存储恢复 |
| 14 天无 invoke | 数据自动清除 |
| Runtime 版本更新 | 下次 invoke 时获得全新文件系统 |

**结合对话历史持久化**：

```python
from strands.session import FileSessionManager

# 对话历史也存到 Session Storage，跨 Stop/Resume 保留
session_manager = FileSessionManager(
    session_id=session_id,
    storage_dir="/mnt/workspace/.sessions"
)

agent = Agent(model=model, tools=tools, session_manager=session_manager)
```

**POSIX 限制**（不影响常规使用）：
- 不支持硬链接（可用符号链接替代）
- 不支持设备文件、FIFO、UNIX socket
- 不支持 xattr 扩展属性
- 不支持 fallocate 稀疏文件预分配

**VPC 模式下的网络要求**：Session Storage 数据存储在 AgentCore 托管的 S3 桶（`acr-storage-*`），VPC 模式需要 S3 Gateway Endpoint 的出站连接。

**与自建 S3 同步的对比**：

| 维度 | Session Storage（推荐） | 自建 S3 同步（2.2） |
|---|---|---|
| 管理方式 | 全托管，零代码 | 需编写 workspace-sync 模块 |
| 数据一致性 | 实时异步复制 + 停止时 flush | 5 分钟周期同步，最多丢 5 分钟 |
| 文件系统接口 | POSIX 挂载，本地路径直接读写 | S3 API 或自建同步脚本 |
| 隔离机制 | AgentCore 服务级隔离 | 需自行实现 STS Session Policy |
| 适用场景 | 所有新项目 | 需要自定义同步逻辑或精细控制的场景 |

---

### 2.2 自建工作区同步（参考实现）

> **注意**：对于新项目，推荐使用上述 Session Storage。以下是参考实现中的自建方案，适用于需要精细控制同步策略的场景。

参考实现中，我们实现了一个**独立的同步模块** (`workspace-sync.js`) 来将 `.openclaw/` 目录同步到 S3。实现简洁但完备：

**同步策略**：

| 事件 | 动作 | 说明 |
|---|---|---|
| 容器启动 | S3 → 本地 | 从 `s3://{bucket}/{namespace}/.openclaw/` 下载到 `$HOME/.openclaw/` |
| 每 5 分钟 | 本地 → S3 | 增量上传变更文件 |
| SIGTERM | 本地 → S3 | 最终保存（10 秒硬超时，超时则放弃） |

**S3 Key 结构**：

```
s3://openclaw-user-files-{account}-{region}/
  └── telegram_123456789/              ← 用户命名空间（由 Channel:ID 确定性生成）
      ├── .openclaw/                   ← 工作区同步（Agent 内部状态）
      │   ├── SOUL.md                 ←   Agent 人设/性格
      │   ├── USER.md                 ←   用户偏好
      │   ├── IDENTITY.md             ←   Agent 身份（名字、表情）
      │   ├── MEMORY.md               ←   记忆笔记
      │   └── TOOLS.md                ←   工具文档
      ├── report.md                    ← 用户文件（通过 s3-user-files 技能管理）
      ├── notes.txt
      ├── _uploads/                    ← 图片上传（Router Lambda 写入）
      │   └── img_1712345678_abc.jpeg
      └── _screenshots/                ← 浏览器截图（AgentCore Browser）
```

**安全过滤**（不同步的文件）：

| 跳过模式 | 原因 |
|---|---|
| `node_modules/`、`.cache/`、`.npm/` | 临时依赖目录，体积大 |
| `*.log`、`*.lock` | 临时文件，每次重建 |
| `openclaw.json` | 每次启动重新生成，确保与容器版本匹配 |
| `AGENTS.md` | 系统指令文件，每次启动重新写入（防止旧版本覆盖新功能） |
| `*.pem`、`*.key`、`.env`、`.secrets/` | 安全敏感文件，不应同步到 S3 |

**凭证感知**：同步模块会扫描文件前 64KB 内容，检测是否包含 AWS Key (`AKIA*`)、API Token (`sk-*`、`ghp_*`、`xoxb-*`) 等敏感信息。检测到时记录告警日志，可用于合规审计。

### 2.3 用户文件存储（s3-user-files 技能）

除了工作区自动同步，用户还可以通过 Agent 的 `s3-user-files` 技能**主动管理**持久文件：

```
用户: "帮我把这个分析报告保存下来"
Agent: → write_user_file("telegram_123456789", "report.md", content)
       → S3: s3://bucket/telegram_123456789/report.md

用户: "我之前保存的报告在哪？"
Agent: → list_user_files("telegram_123456789")
       → read_user_file("telegram_123456789", "report.md")
```

| 维度 | 工作区同步 (.openclaw/) | S3 User Files 技能 |
|---|---|---|
| 用途 | Agent 配置、人设、记忆 | 用户主动保存的文档 |
| 同步方式 | 自动定期同步 | 按需读写（实时） |
| 跨会话持久 | 是 | 是 |
| 谁控制 | Agent 内部使用 | 用户直接操作 |
| 可监管 | 是（S3 + CloudTrail） | 是（S3 + CloudTrail） |

### 2.4 监管与审计能力

同步到 S3 的数据天然具备监管能力：

- **S3 存储桶策略**：KMS CMK 加密（SSE-KMS），防止未授权访问
- **CloudTrail 审计**：所有 S3 读写操作有完整审计日志
- **Admin 控制面板**：管理员可通过 Admin UI 在线浏览任意用户的 S3 文件
- **Secrets Manager**：API Key 等高敏感数据可存储在 AWS Secrets Manager（KMS 加密、CloudTrail 审计），不经过 S3 文件系统
- **Skill 内容可审查**：Agent 安装的 ClawHub 技能同步到 S3，管理员可审查技能内容

---

## 3. 每用户权限隔离（Session Scoped Credentials）

这是参考实现中的**核心安全设计**——每个用户的 OpenClaw 进程只能访问自己的数据，即使 AI 被 Prompt Injection 攻击也无法越权。

### 3.1 设计原理

容器内有两个进程，拥有**完全不同的 AWS 权限**：

```
AgentCore MicroVM 容器
┌────────────────────────────────────────────────────┐
│ Contract Server（父进程，管理生命周期）               │
│                                                    │
│  ┌─────────────────────┐  ┌──────────────────────┐ │
│  │ Proxy 进程（可信）    │  │ OpenClaw 进程（受限） │ │
│  │                     │  │                      │ │
│  │ ✅ 完整执行角色凭证   │  │ ❌ AWS_ACCESS_KEY_ID │ │
│  │ ✅ Bedrock API      │  │ ❌ AWS_SECRET_*      │ │
│  │ ✅ Cognito 管理      │  │ ❌ 容器凭证 URI      │ │
│  │ ✅ S3 全量访问       │  │                      │ │
│  │ ✅ Secrets Manager  │  │ ✅ credential_process │ │
│  │                     │  │    → STS scoped 凭证  │ │
│  │ 开发者编写的可信代码  │  │    → 仅 S3 {ns}/*   │ │
│  └─────────────────────┘  └──────────────────────┘ │
│           ▲ 信任边界 ▲                              │
└────────────────────────────────────────────────────┘
```

### 3.2 实现机制

**第一步：创建限定范围凭证**

容器启动时，Contract Server 通过 STS AssumeRole + Session Policy 生成**限定到用户命名空间的临时凭证**：

```json
{
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
    "Resource": "arn:aws:s3:::openclaw-user-files-{account}-{region}/telegram_123456789/*"
  }]
}
```

**数学保证**：`最终权限 = 执行角色权限 ∩ Session Policy = 仅用户命名空间`

Session Policy 只能**缩小**权限，不能扩大。即使 AI 被 Prompt Injection 攻击，通过 Bash 执行 `aws s3 ls s3://bucket/other_user/`，AWS 返回 **403 AccessDenied**——这是 IAM 层面的硬隔离。

**第二步：凭证投递**

限定范围凭证通过 `credential_process` 机制投递给 OpenClaw（不是环境变量）：

```
/tmp/scoped-creds/scoped-creds.json  → 凭证 JSON（原子写，mode 0600）
/tmp/scoped-creds/scoped-aws-config  → AWS config（credential_process = cat creds.json）

OpenClaw 环境变量：
  AWS_CONFIG_FILE=/tmp/scoped-creds/scoped-aws-config
  AWS_SDK_LOAD_CONFIG=1
```

**第三步：环境变量清洗**

所有可能泄露容器完整凭证的环境变量被**显式删除**：

```
被删除的环境变量（OpenClaw 不可见）：
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_SESSION_TOKEN
  AWS_CONTAINER_CREDENTIALS_RELATIVE_URI   ← ECS/AgentCore 容器凭证
  AWS_CONTAINER_CREDENTIALS_FULL_URI
  AWS_WEB_IDENTITY_TOKEN_FILE
  AWS_ROLE_ARN
```

**第四步：凭证刷新**

STS 自假设角色最大有效期 1 小时。容器每 **45 分钟**自动刷新，采用原子写（.tmp → rename）确保不会读到半写文件。

### 3.3 安全降级策略

如果 STS AssumeRole 失败（如 IAM 配置错误）：

- **不会降级到完整凭证**（Fail-closed 设计）
- OpenClaw 以**零 AWS 权限**启动
- 所有 AWS 工具优雅失败（"抱歉，我暂时无法访问文件存储"）
- 核心对话功能不受影响（Proxy 进程仍有完整凭证调用 Bedrock）

---

## 4. 客户选型注意事项总结

### 4.1 适用场景

| 场景 | 适配度 | 关键能力 |
|---|---|---|
| 企业内部 AI 助手 | 非常适合 | 每用户隔离、数据合规、Admin 监管 |
| 多 Channel 客服机器人 | 非常适合 | Telegram/Slack/飞书、跨 Channel 绑定 |
| 个人 AI 助理 | 非常适合 | 跨会话记忆、个性化人设、文件存储 |
| 需要长任务的场景 | 适合 | HealthyBusy 防中断，8 小时上限足够当前 Agent 能力 |
| 需要 24 小时不间断自主运行 | 需评估 | 当前 Agent 不具备超 8 小时自运行能力，可通过 EventBridge Cron 实现定时任务 |

### 4.2 需要了解的限制

| 限制 | 默认值 | 影响 | 缓解方案 |
|---|---|---|---|
| 容器最大存活时间 | 8 小时 | 超长自主任务需分段 | 当前 Agent 无超 8 小时自运行需求；可通过 EventBridge 编排分段任务 |
| 冷启动延迟 | 5-6 秒 | 空闲后首条消息有短暂延迟 | 轻量代理 ~5 秒先响应；几乎无感 |
| Session Policy 大小 | 2048 字节 | 限制可加入的 IAM 条件数 | 当前策略约 668 字节，远低于上限 |

### 4.3 架构优势

| 维度 | OpenClaw on AgentCore | 纯 Lambda 架构 | 传统 EC2/ECS |
|---|---|---|---|
| 状态保持 | 容器内有状态 + S3 持久化 | 无状态，每次冷启动 | 有状态，需自管理 |
| 启动延迟 | 首次 5-6s，后续毫秒 | 每次 10-30s | 分钟级 |
| 复杂工具 | Bash、浏览器、子代理 | 受限 | 完全支持 |
| 每用户隔离 | MicroVM + IAM Session Policy | 需自行实现 | 需自行实现 |
| 费用模型 | 按实际 CPU/内存消耗 | 按调用次数 | 按实例时间 |
| 长任务支持 | 最长 8 小时 | 15 分钟上限 | 无限制 |
| 运维负担 | 全托管（无服务器） | 全托管 | 需自管理 |
| Channel 可用性 | 7x24（Lambda + API Gateway） | 7x24 | 取决于部署策略 |
