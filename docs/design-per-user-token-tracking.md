# Per-User Token 用量追踪与 CloudWatch 指标修复

## 1. 背景

### 1.1 现状问题

**CloudWatch Dashboard 无 Token 数据**：当前的 token 监控管道依赖 Bedrock Model Invocation Logging（账户级别）→ CloudWatch Logs → Lambda 处理器 → DynamoDB + CloudWatch 自定义指标。该管道在 AgentCore Runtime 场景下失效，原因：

1. **Bedrock Invocation Logging 在 AgentCore 容器环境下可能不会产生日志**——调用由 AgentCore 平台发起，非用户账户直接调用
2. **跨区域推理** (`global.anthropic.claude-opus-4-6-v1`) 可能将日志路由到其他区域
3. **Custom Resource 静默失败**——CDK 中的 `PutModelInvocationLoggingConfiguration` 如果部署时失败不会阻塞堆栈

**无每用户追踪**：Proxy 内有全局计数器 (`chatRequestCount`, `subagentRequestCount`)，但无每用户维度的 token 统计。

### 1.2 已有数据

Proxy（`agentcore-proxy.js`）在每次 Bedrock 调用结束后已经持有以下完整信息：

| 数据 | 来源 | 代码位置 |
|---|---|---|
| `actorId` (如 `telegram:123456`) | `extractSessionMetadata()` | proxy:1457 |
| `inputTokens` / `outputTokens` | Bedrock `ConverseStream` 响应 | proxy:1306-1308 |
| `isSubagent` | 模型名检测 | proxy:1463 |
| `modelId` | `resolveModelId()` | proxy:1175 |
| `channel` | 身份解析 | proxy:1457 |

这些数据当前仅 `console.log()` 输出到容器 stdout，未持久化。

## 2. 设计方案

### 2.1 方案对比

| 方案 | 优点 | 缺点 |
|---|---|---|
| **A. Proxy 内置追踪（推荐）** | 实时、零延迟、可执行预算切断、无外部依赖 | 容器重启丢失（需定期持久化） |
| B. Bedrock Projects (bedrock-mantle) | AWS 原生账单集成 | 需换 API 端点（架构大改），仅支持 OpenAI 兼容 API |
| C. Application Inference Profiles | AWS 原生成本分配标签 | 24-48h 延迟、每用户需创建 profile、有配额限制 |
| D. 修复现有 Invocation Logging 管道 | 不改 Proxy 代码 | AgentCore 环境下可能根本不产生日志 |

**选择方案 A**：在 Proxy 内实现每用户 Token 追踪，直接发布 CloudWatch 自定义指标。

### 2.2 架构设计

```
OpenClaw / Lightweight Agent
        │
        ▼
  agentcore-proxy.js (:18790)
        │
        ├─ 调用 Bedrock ConverseStream
        │    └─ 获取 inputTokens / outputTokens
        │
        ├─ tokenTracker.record(actorId, modelId, input, output, isSubagent)
        │    ├─ 内存累加器 (userTokens Map)
        │    ├─ /health 端点暴露统计
        │    └─ 定时批量发布 CloudWatch 指标 (60s 间隔)
        │         ├─ OpenClaw/TokenUsage 命名空间
        │         ├─ 维度: ActorId, ModelId, Channel
        │         └─ 指标: InputTokens, OutputTokens, TotalTokens,
        │                  EstimatedCostUSD, RequestCount
        │
        ▼
  CloudWatch Dashboard (OpenClaw-Token-Analytics)
```

### 2.3 模块设计

#### 2.3.1 `token-tracker.js`（新建）

```
职责：内存累加 + CloudWatch 批量发布
生命周期：Proxy 进程启动时初始化，SIGTERM 时 flush

数据结构：
  userTokens: Map<actorId, {
    input: number,          // 累计输入 token
    output: number,         // 累计输出 token
    requests: number,       // 请求次数
    subagentInput: number,  // 子代理输入 token
    subagentOutput: number, // 子代理输出 token
    subagentRequests: number,
    channel: string,        // 最后一次请求的渠道
    lastActivity: number,   // 最后活动时间戳 (epoch ms)
  }>

  // 每分钟聚合窗口（用于 CloudWatch 发布）
  pendingMetrics: Array<{actorId, modelId, input, output, isSubagent, timestamp}>

API:
  record(actorId, channel, modelId, inputTokens, outputTokens, isSubagent)
  getStats(actorId?) → 单用户或全部统计
  flush() → 立即发布 pending 到 CloudWatch
  startPublisher(intervalMs = 60000) → 启动定时发布
  stopPublisher() → 停止定时器 + final flush
```

#### 2.3.2 CloudWatch 指标设计

**命名空间**：`OpenClaw/TokenUsage`（复用现有，保持 Dashboard 兼容）

**指标列表**：

| 指标名 | 维度 | 聚合方式 | 说明 |
|---|---|---|---|
| `InputTokens` | ActorId | Sum | 每用户输入 token |
| `OutputTokens` | ActorId | Sum | 每用户输出 token |
| `TotalTokens` | ActorId | Sum | 输入 + 输出 |
| `EstimatedCostUSD` | ActorId | Sum | 估算费用 |
| `RequestCount` | ActorId | Sum | 请求次数 |
| `InputTokens` | (无维度) | Sum | 全局输入 token |
| `OutputTokens` | (无维度) | Sum | 全局输出 token |
| `TotalTokens` | (无维度) | Sum | 全局总计 |

**发布频率**：每 60 秒批量发布一次（CloudWatch PutMetricData 限制 1000 条/次，150 TPS）

**费用估算函数**（与现有 `token_metrics/index.py` 保持一致）：
- Claude Opus 4.6: $15/M input, $75/M output
- 可通过环境变量配置

#### 2.3.3 Proxy 集成点

在 `agentcore-proxy.js` 的两个出口插入 `record()` 调用：

1. **非流式路径** `invokeBedrock()` 返回后（~行 1601）
2. **流式路径** `invokeBedrockStreaming()` 流结束后（~行 1343）

#### 2.3.4 `/health` 端点增强

在现有 `/health` 响应中增加：

```json
{
  "status": "ok",
  "token_stats": {
    "total_input": 12500,
    "total_output": 3400,
    "total_requests": 45,
    "users": {
      "telegram:123456": { "input": 8000, "output": 2100, "requests": 30 },
      "slack:U0AGD41": { "input": 4500, "output": 1300, "requests": 15 }
    }
  }
}
```

### 2.4 CloudWatch Dashboard 修复

**问题**：`OpenClaw-Token-Analytics` 依赖从 Bedrock Invocation Logging 管道来的指标。

**修复**：
- 保留现有 `token_monitoring_stack.py` 中的 Lambda 管道（作为备份来源）
- Proxy 直接发布到同一命名空间 `OpenClaw/TokenUsage`，Dashboard 无需修改
- Dashboard 上的 `TotalTokens`、`InputTokens`、`OutputTokens` 等 widget 自动接收来自 Proxy 的新数据

### 2.5 不做的事情

- **不做预算切断**——本次只做计量，预算执行留作后续
- **不做 DynamoDB 持久化**——现有 Lambda 管道可继续写 DynamoDB（如果 Invocation Logging 修好），Proxy 只负责 CloudWatch 指标
- **不做 Bedrock Projects / Inference Profile 集成**——当前架构不适合（参见 2.1 对比）
- **不改 OpenClaw 代码**——只改 Proxy 层

## 3. 文件变更清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `bridge/token-tracker.js` | **新建** | Token 累加器 + CloudWatch 发布器 |
| `bridge/token-tracker.test.js` | **新建** | 单元测试 |
| `bridge/agentcore-proxy.js` | 修改 | 集成 token-tracker，调用 record() |
| `stacks/agentcore_stack.py` | 修改 | 执行角色增加 `cloudwatch:PutMetricData` 权限 |

## 4. 测试策略

- **单元测试**：`token-tracker.test.js` — record/getStats/flush 逻辑、CloudWatch 批量发布 mock
- **现有测试**：确保 `lightweight-agent.test.js`、`image-support.test.js` 等不受影响
- **手动验证**：部署后通过 Telegram 发消息，检查 CloudWatch Dashboard 是否出现指标数据
