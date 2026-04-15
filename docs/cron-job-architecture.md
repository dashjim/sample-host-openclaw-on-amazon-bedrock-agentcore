# Cron Job 架构设计与迁移指南

> **文档状态**: 当前 (2026-04-15)  
> **目标读者**: 需要将定时任务能力迁移到其他项目的开发者  
> **源代码版本**: feature/per-user-token-tracking 分支

---

## 1. 概述

OpenClaw 项目使用 **Amazon EventBridge Scheduler** 实现用户自助定时任务（Cron Job）。用户通过自然语言对话创建定时计划（如"每天早上9点提醒我查邮件"），系统自动转换为 EventBridge Schedule，到时间后唤醒用户的 AI 容器执行任务，并将结果投递到用户的聊天频道。

### 核心特点

- **用户自助**: 通过 AI 对话自然语言创建/管理，无需理解 cron 语法
- **Per-User 隔离**: Schedule 命名和 DynamoDB 记录均绑定用户 namespace，不可跨用户操作
- **Serverless**: 容器按需唤醒，执行完毕后自然空闲终止
- **多频道投递**: 支持 Telegram、Slack、Feishu 消息投递
- **双重状态存储**: EventBridge Schedule（执行引擎）+ DynamoDB（元数据 + 所有权校验）

---

## 2. 端到端数据流

```
                    ┌─────────────────────────────────────────────┐
                    │           用户对话（创建阶段）                │
                    │                                             │
                    │  用户: "每天早上9点提醒我查邮件"              │
                    │    ↓                                        │
                    │  AI Agent 识别意图 → 调用 create_schedule   │
                    │    ↓                                        │
                    │  create.js:                                 │
                    │    1. 创建 EventBridge Schedule              │
                    │       Target = Cron Lambda ARN              │
                    │       Input = {userId, actorId, channel,    │
                    │                channelTarget, message, ...} │
                    │    2. 写 DynamoDB CRON# 记录                │
                    └─────────────────────────────────────────────┘

                                   ↓ 到达定时时间

                    ┌─────────────────────────────────────────────┐
                    │        EventBridge Scheduler 触发            │
                    │                                             │
                    │  EventBridge → 通过 Scheduler IAM Role      │
                    │    → invoke Cron Lambda                     │
                    │    → 传入 Target Input 作为 event            │
                    └─────────────┬───────────────────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────────────────────────┐
                    │        Cron Lambda 执行（4 个 Phase）        │
                    │                                             │
                    │  Phase 1: resolve userId + get/create       │
                    │           AgentCore session                 │
                    │                                             │
                    │  Phase 2: warmup loop                       │
                    │           invoke(action="warmup") 每 15s    │
                    │           直到 status="ready" 或超时 300s   │
                    │                                             │
                    │  Phase 3: invoke(action="cron",             │
                    │           message="[Scheduled: ...] msg")   │
                    │           → AI 处理 → 返回 responseText     │
                    │                                             │
                    │  Phase 4: deliver_response()                │
                    │           → Telegram/Slack/Feishu API       │
                    └─────────────┬───────────────────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────────────────────────┐
                    │        AgentCore 容器处理                    │
                    │                                             │
                    │  warmup action:                             │
                    │    已初始化 → {status: "ready"}             │
                    │    未初始化 → 触发 init() →                 │
                    │               {status: "initializing"}      │
                    │                                             │
                    │  cron action:                               │
                    │    阻塞等待 init 完成                        │
                    │    → WebSocket bridge → OpenClaw 处理       │
                    │    → 返回 responseText                      │
                    │    (bridge 空响应时 fallback lightweight)    │
                    └─────────────────────────────────────────────┘
```

---

## 3. 组件详解

### 3.1 Skill 脚本（容器内，Node.js）

位于 `bridge/skills/eventbridge-cron/`，在用户的 AI 容器内运行。

#### 3.1.1 common.js — 共享基础设施

**职责**: 验证、命名、DynamoDB CRUD。

```
所需环境变量:
  AWS_REGION                  — AWS 区域
  EVENTBRIDGE_SCHEDULE_GROUP  — Schedule 分组名 (默认 "openclaw-cron")
  CRON_LAMBDA_ARN             — Cron 执行器 Lambda ARN
  EVENTBRIDGE_ROLE_ARN        — Scheduler 调用 Lambda 的 IAM Role ARN
  IDENTITY_TABLE_NAME         — DynamoDB 表名
```

**关键函数:**

| 函数 | 说明 |
|------|------|
| `validateUserId(userId)` | 校验 namespace 格式 `{channel}_{id}`，拒绝 `default-user` |
| `validateExpression(expr)` | 校验 `cron()`/`rate()`/`at()` 格式，强制最小 5 分钟间隔 |
| `validateTimezone(tz)` | 通过 `Intl.DateTimeFormat` 校验 IANA 时区 |
| `generateScheduleId()` | 8 字符随机 hex（`crypto.randomBytes(4)`）|
| `buildScheduleName(userId, id)` | `openclaw-{userId}-{scheduleId}`，64 字符上限 |
| `extractChannelInfo(userId)` | `telegram_12345` → `{channel: "telegram", channelTarget: "12345"}` |
| `saveCronRecord()` | DynamoDB PutItem: `PK=USER#{userId}, SK=CRON#{scheduleId}` |
| `getCronRecord()` | DynamoDB GetItem |
| `listCronRecords()` | DynamoDB Query: `begins_with(SK, "CRON#")` |
| `updateCronRecord()` | DynamoDB UpdateItem（动态 SET 表达式）|
| `deleteCronRecord()` | DynamoDB DeleteItem |

**Node.js 依赖:** `@aws-sdk/client-dynamodb`, `@aws-sdk/lib-dynamodb`

#### 3.1.2 create.js — 创建 Schedule

**流程:**
1. 解析 CLI 参数: `userId`, `cron_expression`, `timezone`, `message`, `[channel]`, `[channel_target]`, `[schedule_name]`
2. 验证全部输入（userId、表达式、时区）
3. 生成 `scheduleId` + 构建 EventBridge schedule name
4. 使用 `INTERNAL_USER_ID`（容器初始化时设置）作为 Lambda payload 中的 `userId`，确保和容器的 DynamoDB 写权限一致
5. 调用 `CreateScheduleCommand`:
   - `GroupName`: `openclaw-cron`
   - `ScheduleExpression`: 用户的 cron 表达式
   - `ScheduleExpressionTimezone`: IANA 时区
   - `FlexibleTimeWindow`: OFF（精确触发）
   - `Target.Arn`: Cron Lambda ARN
   - `Target.RoleArn`: Scheduler IAM Role ARN
   - `Target.Input`: JSON payload（userId, actorId, channel, channelTarget, message, scheduleId, scheduleName）
6. 写 DynamoDB `CRON#` 记录
7. **失败回滚**: 如果 DynamoDB 写入失败，立即删除已创建的 EventBridge Schedule，防止孤儿 schedule

**Node.js 依赖:** `@aws-sdk/client-scheduler`

#### 3.1.3 update.js — 更新 Schedule

**支持的更新参数:**
- `--expression "cron(...)"` — 新的定时表达式
- `--timezone "Asia/Tokyo"` — 新的时区
- `--message "new message"` — 新的任务消息
- `--name "new name"` — 新的显示名称
- `--enable` / `--disable` — 启用/禁用

**流程:** 先从 DynamoDB 确认记录存在 → 从 EventBridge 获取当前 schedule → 合并更新 → `UpdateScheduleCommand` + `updateCronRecord()`

#### 3.1.4 list.js — 列出 Schedule

从 DynamoDB 查询用户的所有 `CRON#` 记录。包含一个 `describeSchedule()` 函数将 cron 表达式转为人类可读文本（如 `cron(0 9 * * ? *)` → `daily at 09:00 Asia/Shanghai`）。

#### 3.1.5 delete.js — 删除 Schedule

先验证 DynamoDB 记录存在 → `DeleteScheduleCommand` → `deleteCronRecord()`。EventBridge 已不存在时（`ResourceNotFoundException`）仅清理 DynamoDB。

---

### 3.2 Cron Executor Lambda（Python）

位于 `lambda/cron/index.py`，被 EventBridge Scheduler 触发。

#### 配置

```python
AGENTCORE_RUNTIME_ARN       # AgentCore Runtime ARN
AGENTCORE_QUALIFIER         # Runtime Endpoint ID (通常 "DEFAULT")
IDENTITY_TABLE_NAME         # DynamoDB 表名
TELEGRAM_TOKEN_SECRET_ID    # Telegram bot token 的 Secrets Manager ID
SLACK_TOKEN_SECRET_ID       # Slack credentials 的 Secrets Manager ID
FEISHU_TOKEN_SECRET_ID      # Feishu credentials 的 Secrets Manager ID
LAMBDA_TIMEOUT_SECONDS      # Lambda 超时（默认 600s）
```

#### 执行流程

```python
def handler(event, context):
    # event = EventBridge Target Input (JSON):
    # {userId, actorId, channel, channelTarget, message, scheduleId, scheduleName}

    # 1. 解析并校验必填字段
    # 2. 解析最新 userId（防止旧 schedule 中的 userId 过期）
    current_user_id = resolve_current_user_id(actor_id) or user_id

    # 3. 所有权验证 — 查 DynamoDB CRON# 记录
    cron_record = identity_table.get_item(PK=f"USER#{current_user_id}", SK=f"CRON#{schedule_id}")
    if not cron_record:
        return 403  # 不属于该用户，拒绝执行

    # 4. 获取/创建 session
    session_id = get_or_create_session(current_user_id)

    # 5. 预热容器（最多 300s，每 15s 轮询）
    warmup_ok = warmup_and_wait(session_id, ...)
    if not warmup_ok:
        deliver_response(channel, target, "启动超时提示")
        return 503

    # 6. 发送 cron 消息
    result = invoke_agentcore(session_id, "cron", ..., message=f"[Scheduled: {name}] {message}")

    # 7. 投递响应到聊天频道
    deliver_response(channel, channel_target, result["response"])
```

#### 消息投递

Lambda 自己直接调用各频道 API 发消息（不经过 Router Lambda）:

- **Telegram**: `POST https://api.telegram.org/bot{token}/sendMessage`，Markdown→HTML 转换后发送，失败 fallback 纯文本
- **Slack**: `POST https://slack.com/api/chat.postMessage`，Bearer token 认证
- **Feishu**: `POST {FEISHU_API_DOMAIN}/open-apis/im/v1/messages`，tenant_access_token 认证，自动分片 20000 字符

#### 关键设计决策

| 决策 | 原因 |
|------|------|
| Lambda 直接发消息，不走 Router | 减少延迟，避免 Router Lambda 的异步自调用增加复杂度 |
| 先 warmup 再 cron | 冷启动容器需要 1-2 分钟，不预热直接 cron 会超时 |
| 轮询式 warmup（非事件驱动）| AgentCore 没有容器就绪回调，只能主动查询 |
| 所有权验证在 Lambda 侧 | 防止伪造 schedule payload 的跨用户攻击 |
| resolve_current_user_id | 用户可能重新注册导致 userId 变更，确保用同一个 session |

---

### 3.3 Contract Server 处理（容器内，Node.js）

位于 `bridge/agentcore-contract.js`，是容器的 HTTP 入口（端口 8080）。

#### warmup action (L1677-1694)

```javascript
if (action === "warmup") {
  if (openclawReady && proxyReady) {
    return { status: "ready" };      // 已就绪
  }
  if (!initInProgress && userId && actorId) {
    init(userId, actorId, channel);  // 触发后台初始化
  }
  return { status: "initializing" }; // 还在启动中
}
```

#### cron action (L1697-1787)

```javascript
if (action === "cron") {
  // 1. 阻塞等待初始化完成（与 chat 不同，chat 用 lightweight agent 立即响应）
  if (!openclawReady || !proxyReady) {
    await init(userId, actorId, channel);  // 或 await initPromise
  }

  // 2. 标记活跃任务，防止空闲终止
  activeTaskCount++;

  // 3. 通过 WebSocket bridge 发送消息到 OpenClaw
  responseText = await enqueueMessage(message);

  // 4. bridge 空响应时 fallback 到 lightweight agent
  if (!responseText) {
    responseText = await agent.chat(message, actorId, deadline);
  }

  activeTaskCount--;
  return { response: responseText };
}
```

#### 环境变量自动推导 (L84-94)

```javascript
// 如果 EVENTBRIDGE_ROLE_ARN 未设置，从 EXECUTION_ROLE_ARN 推导
// arn:aws:iam::{account}:role/openclaw-agentcore-execution-role-{region}
//   → arn:aws:iam::{account}:role/openclaw-cron-scheduler-role-{region}
if (!process.env.EVENTBRIDGE_ROLE_ARN && process.env.EXECUTION_ROLE_ARN) {
  const account = process.env.EXECUTION_ROLE_ARN.match(/(\d+)/)[1];
  process.env.EVENTBRIDGE_ROLE_ARN =
    `arn:aws:iam::${account}:role/openclaw-cron-scheduler-role-${region}`;
}
```

---

### 3.4 Tool 定义（Warm-up 阶段）

位于 `bridge/lightweight-agent.js`，在 OpenClaw 完全启动前提供 cron 操作能力。

#### OpenAI Function Calling 格式 (L124-229)

```javascript
const TOOLS = [
  // ...其他 tools...
  {
    type: "function",
    function: {
      name: "create_schedule",
      description: "Create a recurring cron schedule...",
      parameters: {
        type: "object",
        properties: {
          cron_expression: { type: "string", description: "..." },
          timezone:        { type: "string", description: "..." },
          message:         { type: "string", description: "..." },
          schedule_name:   { type: "string", description: "..." },
        },
        required: ["cron_expression", "timezone", "message"],
      },
    },
  },
  { name: "list_schedules",  parameters: { properties: {} } },
  { name: "update_schedule", parameters: { properties: { schedule_id, expression, timezone, message, name, enable, disable }, required: ["schedule_id"] } },
  { name: "delete_schedule", parameters: { properties: { schedule_id }, required: ["schedule_id"] } },
];
```

#### Tool→脚本映射 (L452-458)

```javascript
const TOOL_SCRIPTS = {
  create_schedule:  "/skills/eventbridge-cron/create.js",
  list_schedules:   "/skills/eventbridge-cron/list.js",
  update_schedule:  "/skills/eventbridge-cron/update.js",
  delete_schedule:  "/skills/eventbridge-cron/delete.js",
};
```

#### 参数转换 buildToolArgs() (L898-927)

将 AI 返回的 JSON 参数转为 CLI `process.argv`:

```javascript
case "create_schedule": {
  const result = [script, userId, args.cron_expression, args.timezone, args.message];
  if (args.schedule_name) result.push("", "", args.schedule_name);
  // create.js 期望: argv[2]=userId argv[3]=expr argv[4]=tz argv[5]=msg argv[6]=channel argv[7]=target argv[8+]=name
  return result;
}
case "update_schedule": {
  const result = [script, userId, args.schedule_id];
  if (args.expression) result.push("--expression", args.expression);
  if (args.timezone)   result.push("--timezone", args.timezone);
  // ...
  return result;
}
```

---

### 3.5 CDK 基础设施

位于 `stacks/cron_stack.py`，通过 `app.py` L91-104 实例化。

#### 创建的 AWS 资源

| 资源 | 类型 | 说明 |
|------|------|------|
| `openclaw-cron` | `CfnScheduleGroup` | EventBridge Scheduler 分组，所有用户的 schedule 集中在此 |
| `openclaw-cron-scheduler-role-{region}` | `iam.Role` | EventBridge 调用 Lambda 的 IAM 角色 |
| `openclaw-cron-executor` | `lambda.Function` | Cron 执行器 Lambda (Python 3.13) |
| `/openclaw/lambda/cron` | `logs.LogGroup` | CloudWatch 日志组 |

#### IAM 权限矩阵

**Scheduler Role（EventBridge → Lambda）:**

| Action | Resource |
|--------|----------|
| `lambda:InvokeFunction` | Cron Lambda ARN |

**Cron Lambda 执行角色:**

| Action | Resource | 用途 |
|--------|----------|------|
| `bedrock-agentcore:InvokeAgentRuntime` | Runtime ARN + `/*` | 调用 AgentCore |
| `dynamodb:GetItem/PutItem/UpdateItem/Query` | Identity Table + indexes | Session + 所有权查询 |
| `secretsmanager:GetSecretValue/DescribeSecret` | `openclaw/*` | 频道 token |
| `kms:Decrypt` | CMK ARN | 解密 secrets |

**AgentCore Execution Role（容器 → AWS）— 由 CronStack 追加:**

| Action | Resource | 用途 |
|--------|----------|------|
| `scheduler:Create/Get/Update/Delete/ListSchedules` | `openclaw-cron/*` | 管理 EventBridge Schedule |
| `iam:PassRole` (条件: `scheduler.amazonaws.com`) | Scheduler Role ARN | 创建 schedule 时传递角色 |
| `dynamodb:GetItem/PutItem/UpdateItem/DeleteItem/Query` | Identity Table + indexes | CRON# 记录 CRUD |

---

## 4. DynamoDB 数据模型

### 4.1 CRON# 记录

存储在 `openclaw-identity` 表中（与用户 profile、session 共用同一张单表）:

| 字段 | 类型 | 说明 |
|------|------|------|
| `PK` | String | `USER#{userId}` (如 `USER#user_abc123`) |
| `SK` | String | `CRON#{scheduleId}` (如 `CRON#a1b2c3d4`) |
| `scheduleId` | String | 8 字符 hex 标识符 |
| `scheduleName` | String | 人类可读名称 |
| `expression` | String | EventBridge 表达式 (如 `cron(0 9 * * ? *)`) |
| `timezone` | String | IANA 时区 (如 `Asia/Shanghai`) |
| `message` | String | 定时执行的任务消息 |
| `channel` | String | 投递频道 (`telegram`/`slack`/`feishu`) |
| `channelTarget` | String | 频道内目标 ID |
| `actorId` | String | `{channel}:{channelTarget}` |
| `enabled` | Boolean | 启用状态 |
| `createdAt` | String | ISO 8601 创建时间 |
| `updatedAt` | String | ISO 8601 更新时间 |

### 4.2 查询模式

- **列出用户所有 schedule**: `Query(PK="USER#{userId}", SK begins_with "CRON#")`
- **获取单个 schedule**: `GetItem(PK="USER#{userId}", SK="CRON#{scheduleId}")`
- **所有权验证**: 同上 GetItem，记录存在即表示属于该用户

---

## 5. EventBridge Schedule 命名与配置

### 命名规范

```
openclaw-{userId}-{scheduleId}
```

- `userId`: namespace 格式 (如 `telegram_12345`)
- `scheduleId`: 8 字符 hex
- 总长度上限: 64 字符（超长时截断 userId）
- 全部 schedule 在 `openclaw-cron` 分组下

### Schedule 配置

```json
{
  "Name": "openclaw-telegram_12345-a1b2c3d4",
  "GroupName": "openclaw-cron",
  "ScheduleExpression": "cron(0 9 * * ? *)",
  "ScheduleExpressionTimezone": "Asia/Shanghai",
  "FlexibleTimeWindow": { "Mode": "OFF" },
  "State": "ENABLED",
  "Target": {
    "Arn": "arn:aws:lambda:us-west-2:123456789:function:openclaw-cron-executor",
    "RoleArn": "arn:aws:iam::123456789:role/openclaw-cron-scheduler-role-us-west-2",
    "Input": "{\"userId\":\"user_abc\",\"actorId\":\"telegram:12345\",\"channel\":\"telegram\",\"channelTarget\":\"12345\",\"message\":\"Check email\",\"scheduleId\":\"a1b2c3d4\",\"scheduleName\":\"Daily email\"}"
  }
}
```

### 支持的表达式格式

| 格式 | 示例 | 说明 |
|------|------|------|
| `cron()` | `cron(0 9 * * ? *)` | 6 字段: 分 时 日 月 周 年 |
| `rate()` | `rate(1 hour)` | 固定间隔，最小 5 分钟 |
| `at()` | `at(2025-12-31T23:59:00)` | 一次性定时 |

---

## 6. 安全设计

### 6.1 三层防护

```
Layer 1 — Schedule 命名隔离
  Schedule name 包含 userId，EventBridge IAM 仅允许 openclaw-cron/* 前缀
  → 用户只能操作自己 namespace 下的 schedule

Layer 2 — DynamoDB 所有权校验
  create: 写入 CRON# 记录到自己的 USER# PK 下
  update/delete: 先 getCronRecord 确认记录存在
  execute: Lambda 执行前 getItem 验证 CRON# 属于该 userId

Layer 3 — STS Session Credentials（容器内）
  容器使用 STS AssumeRole 获取 scoped credentials
  DynamoDB 写操作在 execution role 策略层面限制到 identity table
  （注意: session policy 因 2048 字节限制未添加 DynamoDB Condition）
```

### 6.2 防滥用

- `validateExpression()` 强制最小 5 分钟间隔（拒绝 `*` 和 `*/1` 的分钟字段）
- `validateUserId()` 拒绝 `default-user`，要求匹配 `{channel}_{id}` 格式
- EventBridge `FlexibleTimeWindow: OFF` 精确触发，避免时间窗口内重复执行

---

## 7. 迁移指南

### 7.1 最小可行迁移（MVP）

如果你只需要"定时触发 → 执行任务 → 投递消息"的核心能力，需要迁移以下组件:

#### 必须迁移

| 组件 | 源文件 | 迁移说明 |
|------|--------|----------|
| Schedule CRUD | `bridge/skills/eventbridge-cron/*.js` | **核心**。可独立运行，只依赖 AWS SDK + 环境变量 |
| Cron Executor | `lambda/cron/index.py` | **核心**。替换 `invoke_agentcore()` 为你的任务执行方式 |
| CDK 基础设施 | `stacks/cron_stack.py` | **核心**。EventBridge + Lambda + IAM 定义 |
| CDK 入口 | `app.py` L91-104 | 实例化参数 |

#### 可选迁移

| 组件 | 源文件 | 何时需要 |
|------|--------|----------|
| Tool 定义 | `lightweight-agent.js` L124-229 | 使用 OpenAI function calling 格式的 AI 框架 |
| 参数转换 | `lightweight-agent.js` L898-927 | AI JSON 参数 → CLI 参数转换 |
| Contract handler | `agentcore-contract.js` L1677-1787 | 使用 AgentCore Runtime |
| 消息格式化 | `lambda/cron/index.py` `_markdown_to_telegram_html()` | 需要 Telegram HTML 格式 |

### 7.2 需要替换的部分

| 原始实现 | 你需要替换为 |
|----------|-------------|
| `invoke_agent_runtime(action="warmup"/"cron")` | 你的 AI/任务执行 runtime 的调用方式 |
| `warmup_and_wait()` 轮询逻辑 | 如果你的 runtime 没有冷启动问题，可以去掉 |
| `resolve_current_user_id()` | 你的用户身份解析逻辑 |
| DynamoDB 单表设计 (`PK/SK`) | 你的数据存储（可以用独立表或 RDS）|
| `send_telegram_message()` 等 | 你的消息投递方式 |
| STS scoped credentials | 你的权限隔离方案 |

### 7.3 环境变量清单

#### 容器内（Skill 脚本需要）

```
AWS_REGION=us-west-2
EVENTBRIDGE_SCHEDULE_GROUP=openclaw-cron
CRON_LAMBDA_ARN=arn:aws:lambda:...:function:openclaw-cron-executor
EVENTBRIDGE_ROLE_ARN=arn:aws:iam::...:role/openclaw-cron-scheduler-role-us-west-2
IDENTITY_TABLE_NAME=openclaw-identity
INTERNAL_USER_ID=user_abc123          # 容器初始化时设置
```

#### Lambda 需要

```
AGENTCORE_RUNTIME_ARN=arn:aws:bedrock-agentcore:...:runtime/...
AGENTCORE_QUALIFIER=DEFAULT
IDENTITY_TABLE_NAME=openclaw-identity
TELEGRAM_TOKEN_SECRET_ID=openclaw/channels/telegram
SLACK_TOKEN_SECRET_ID=openclaw/channels/slack
FEISHU_TOKEN_SECRET_ID=openclaw/channels/feishu
LAMBDA_TIMEOUT_SECONDS=600
```

### 7.4 迁移检查清单

- [ ] 创建 EventBridge Schedule Group
- [ ] 部署 Cron Executor Lambda（或等价的触发处理器）
- [ ] 创建 Scheduler IAM Role（允许 EventBridge invoke Lambda）
- [ ] 创建 Lambda Resource Policy（允许 EventBridge 服务调用）
- [ ] 准备 DynamoDB 表（或替代存储）用于 CRON# 元数据
- [ ] 授权你的 runtime/容器角色:
  - `scheduler:CreateSchedule/GetSchedule/UpdateSchedule/DeleteSchedule/ListSchedules`
  - `iam:PassRole`（仅限传递 Scheduler Role 给 `scheduler.amazonaws.com`）
  - 对元数据存储的读写权限
- [ ] 授权 Lambda:
  - 调用你的 AI/任务 runtime
  - 读取元数据存储（所有权校验）
  - 读取消息频道 token（Secrets Manager 或其他）
- [ ] 设置容器环境变量（`EVENTBRIDGE_SCHEDULE_GROUP`, `CRON_LAMBDA_ARN`, `EVENTBRIDGE_ROLE_ARN` 等）
- [ ] 实现消息投递到目标频道
- [ ] 测试: 创建 schedule → 等待触发 → 确认消息投递

### 7.5 简化方案

如果你的场景不需要 per-user AI 容器，可以大幅简化:

```
简化前 (OpenClaw):
  EventBridge → Lambda → warmup AgentCore → invoke cron → deliver

简化后 (无 AI 容器):
  EventBridge → Lambda → 直接执行任务逻辑 → deliver
```

去掉 `warmup_and_wait()` 和 `invoke_agentcore()` 步骤，直接在 Lambda 内执行定时任务逻辑，可将 Lambda 超时从 600s 降至 30-60s。

---

## 8. 文件索引

所有与 Cron Job 相关的文件，按重要程度排序:

```
# ========== 核心实现 ==========

bridge/skills/eventbridge-cron/
  common.js              # 验证 + DynamoDB CRUD + 命名
  create.js              # 创建 EventBridge Schedule + DynamoDB 记录
  update.js              # 更新 Schedule
  list.js                # 列出 Schedule（含人类可读描述）
  delete.js              # 删除 Schedule
  SKILL.md               # OpenClaw Skill 声明

lambda/cron/
  index.py               # Cron 执行器 Lambda（warmup → invoke → deliver）

stacks/
  cron_stack.py           # CDK 基础设施（EventBridge + Lambda + IAM）

# ========== 集成点 ==========

app.py                    # L91-104: CronStack 实例化
bridge/agentcore-contract.js
                          # L84-94:   EVENTBRIDGE_ROLE_ARN 自动推导
                          # L457:     deny 内置 cron tool
                          # L519-534: 系统提示中 cron 说明
                          # L860-864: INTERNAL_USER_ID 环境变量传播
                          # L951:     OPENCLAW_SKIP_CRON=1 禁用内置 cron
                          # L1677-1694: warmup action handler
                          # L1697-1787: cron action handler

bridge/lightweight-agent.js
                          # L124-229: Tool 定义 (create/list/update/delete_schedule)
                          # L452-458: Tool → 脚本路径映射
                          # L898-927: buildToolArgs() 参数转换

# ========== 文档 ==========

bridge/CLAUDE.md          # "Scheduling (Cron Jobs)" 段落 — 容器内 AI 的使用说明
CLAUDE.md                 # "EventBridge Cron Scheduling" 段落 — 项目级说明
```
