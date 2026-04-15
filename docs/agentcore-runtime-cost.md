# AgentCore Runtime 成本分析（1 万用户规模）

> 基于 2026-03-23 ~ 2026-03-28 连续 6 天 Cost Explorer 真实账单数据，2 个活跃用户，us-west-2 区域。

## 计费模型

AgentCore Runtime 采用 **Consumption-based（按实际消耗）** 计费，而非按容器预置资源。

| 概念 | 说明 |
|------|------|
| 容器配置（如 2 vCPU / 8 GB） | 资源**上限**，不是计费基数 |
| CPU 计费 | 按实际消耗的 vCPU 周期，Cost Explorer 类型 `Runtime:Consumption-based:vCPU` |
| 内存计费 | 按进程实际占用内存，Cost Explorer 类型 `Runtime:Consumption-based:Memory` |
| 空闲回收 | 容器在 `idleRuntimeSessionTimeout` 后被回收，回收后不产生费用 |
| CloudWatch 指标 | `MemoryUsed-GBHours` / `CPUUsed-vCPUHours` 与 Cost Explorer 账单**精确一致**（UTC 午夜对齐，零 overhead） |

### 官方单价

| 资源 | 单价 |
|------|------|
| CPU | $0.0895 / vCPU-hour |
| 内存 | $0.00945 / GB-hour |

> 来源：[Amazon Bedrock AgentCore Pricing](https://aws.amazon.com/cn/bedrock/agentcore/pricing/)

## 实测数据

### 真实账单（6 天，2 用户，仅 Runtime）

| 指标 | 6 天合计 | 每用户每天 |
|------|----------|------------|
| Memory 用量 | 247.62 GB-hours | 20.6 GB-hours |
| Memory 费用 | $2.34 | $0.195 |
| CPU 用量 | 1.67 vCPU-hours | 0.139 vCPU-hours |
| CPU 费用 | $0.15 | $0.012 |
| **合计** | **$2.49** | **$0.207** |

### 每 Session-Hour 实测指标

通过 CloudWatch 小时粒度数据推导（每活跃 session 每小时）：

| 资源 | 每 Session-Hour 消耗 | 费用 |
|------|----------------------|------|
| CPU | 0.01 vCPU-hours | $0.0009 |
| 内存 | 1.5 GB-hours | $0.0142 |
| **合计** | | **$0.0151** |

### 成本构成

```
Memory: 94%  ████████████████████████████████████████████████
CPU:     6%  ███
```

LLM Agent 是极度 I/O 密集型应用——绝大部分时间在等待 Bedrock API 返回，CPU 几乎为零。**内存是唯一的成本驱动因素。**

## 场景假设

| 参数 | 值 |
|------|-----|
| 用户数 | 10,000 |
| 每用户每日活跃时长 | 2 小时 |
| 容器内存占用 | 1.5 GB（Node.js 22 + Agent 进程，实测值） |
| CPU 消耗 | 0.01 vCPU-hours / session-hour（实测值） |
| idle timeout | 900 秒（15 分钟） |

### 计费时长说明

用户活跃 2 小时 ≠ 计费 2 小时。每次交互暂停后，容器在 idle timeout 到期前持续计费。

| 场景 | 计费时长 | 说明 |
|------|----------|------|
| 单次连续使用 | 2h | 最理想：一次登录，连续使用，退出 |
| 典型使用 | 3h | 分 3-4 次交互，每次间隔产生 15 分钟 idle 尾巴 |
| 频繁交互 | 4h | 全天零散使用，多次触发 idle timeout |

## 单用户成本

以典型场景（3 小时计费）为例：

| 项目 | 用量 / 天 | 单价 | 费用 / 天 |
|------|-----------|------|-----------|
| Runtime CPU | 0.03 vCPU-hours | $0.0895/vCPU-h | $0.003 |
| Runtime Memory | 4.5 GB-hours | $0.00945/GB-h | $0.043 |
| **合计** | | | **$0.045** |

| 周期 | 费用 |
|------|------|
| 每天 | $0.045 |
| **每月** | **$1.36** |

## 1 万用户总成本

### 按场景估算

| 场景 | 计费时长 | 每用户/月 | 10,000 用户/月 |
|------|----------|-----------|----------------|
| 单次连续使用 | 2h | $0.91 | **$9,048** |
| **典型使用** | **3h** | **$1.36** | **$13,571** |
| 频繁交互 | 4h | $1.81 | **$18,095** |

### 推荐估算：典型使用（3h/user/day）

| 项目 | 月费 | 占比 |
|------|------|------|
| Memory | $12,758 | 94.0% |
| CPU | $813 | 6.0% |
| **合计** | **$13,571** | 100% |

### 月度成本区间

```
$9,048 ─────────── $13,571 ─────────── $18,095
 轻度使用            典型使用            频繁交互
```

## 成本优化建议

| 方向 | 预期效果 | 说明 |
|------|----------|------|
| 降低 idle timeout | 高 | 从 900s 降至 300s，减少空闲计费时长 |
| 优化进程内存 | 高 | 内存占 94% 成本，每降低 0.1 GB 节省 ~6% |
| 关闭 BrowserTool | 极高 | 如启用，Browser 成本可达 Runtime 的 1.3-2 倍 |
| 优化 CPU | 低 | CPU 仅占 6%，优化空间有限 |

## 与 Bedrock 模型推理成本的关系

AgentCore Runtime 是**计算层**成本，不包含 Bedrock 模型调用费用。完整的每用户成本 = Runtime + 模型推理 + Guardrails（如启用）+ 数据传输。

以当前配置（`global.anthropic.claude-opus-4-6-v1`、`daily_cost_budget_usd: 5`）为参考，模型推理通常是更大的成本项。

## 附录：数据验证方法

CloudWatch 指标与 Cost Explorer 账单可以交叉验证，确保估算可靠：

```bash
# 1. CloudWatch 实际用量（需 UTC 午夜对齐）
aws cloudwatch get-metric-statistics \
  --namespace "AWS/Bedrock-AgentCore" \
  --metric-name "MemoryUsed-GBHours" \
  --dimensions \
    Name=Resource,Value="$RUNTIME_ARN" \
    Name=Service,Value="AgentCore.Runtime" \
    Name=Name,Value="$RUNTIME_NAME::DEFAULT" \
  --start-time 2026-03-23T00:00:00Z \
  --end-time 2026-03-29T00:00:00Z \
  --period 86400 --statistics Sum --region us-west-2

# 2. Cost Explorer 账单（Ground Truth）
aws ce get-cost-and-usage \
  --time-period Start=2026-03-23,End=2026-03-29 \
  --granularity DAILY \
  --metrics UnblendedCost UsageQuantity \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Bedrock AgentCore"]}}' \
  --group-by Type=DIMENSION,Key=USAGE_TYPE \
  --region us-east-1
```

两者在 UTC 午夜对齐后数值完全一致（6 天验证，偏差 = 0）。
