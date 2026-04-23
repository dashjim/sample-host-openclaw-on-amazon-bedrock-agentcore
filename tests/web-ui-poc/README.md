# Web UI WebSocket POC — 部署与测试指南

## 概述

本 POC 验证了浏览器通过 AgentCore 原生 WebSocket 直连 OpenClaw Gateway Protocol 的可行性。

**核心发现**：AgentCore 平台会自动发现容器内的 WebSocket 端口（OpenClaw Gateway 的 18789），并直接桥接浏览器的 WSS 连接。**不需要任何容器代码改动**。

```
浏览器/脚本
    ↓ wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<arn>/ws
AgentCore 平台 (SigV4/OAuth 认证 + Session 路由)
    ↓ 自动桥接到容器内 WebSocket 端口
OpenClaw Gateway (ws://127.0.0.1:18789)
    ↓ 完整 Gateway Protocol v3
    ↓ 97 methods, 19 events
Bedrock Claude (ConverseStream)
```

## 前置条件

- AWS CLI 已配置（需要 `bedrock-agentcore` 权限）
- Python 3.12+ 已安装
- `bedrock-agentcore` SDK 已安装：`pip3 install bedrock-agentcore websockets boto3`
- OpenClaw 容器已部署并运行

## 测试步骤

### 1. 确认容器状态

```bash
# 设置环境变量
export AWS_REGION=us-west-2
export RUNTIME_ARN="arn:aws:bedrock-agentcore:us-west-2:<ACCOUNT_ID>:runtime/<RUNTIME_ID>"
export SESSION_ID="ses_<your_session_id>"
export ACTOR_ID="<channel>:<user_id>"  # e.g. telegram:123456

# 检查容器状态
PAYLOAD=$(echo -n '{"action":"status"}' | base64 -w0)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn $RUNTIME_ARN \
  --runtime-session-id $SESSION_ID \
  --runtime-user-id $ACTOR_ID \
  --payload "$PAYLOAD" \
  --content-type "application/json" \
  --accept "application/json" \
  --region $AWS_REGION \
  /tmp/status.json && cat /tmp/status.json
```

确认 `openclawReady: true`。如果容器未运行，先 warmup：

```bash
PAYLOAD=$(echo -n "{\"action\":\"warmup\",\"userId\":\"<USER_ID>\",\"actorId\":\"${ACTOR_ID}\",\"channel\":\"<CHANNEL>\"}" | base64 -w0)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn $RUNTIME_ARN \
  --runtime-session-id $SESSION_ID \
  --runtime-user-id $ACTOR_ID \
  --payload "$PAYLOAD" \
  --content-type "application/json" \
  --accept "application/json" \
  --region $AWS_REGION \
  /tmp/warmup.json && cat /tmp/warmup.json
```

等待约 1-2 分钟，重复 status 检查直到 `openclawReady: true`。

### 2. 运行自动化测试脚本

```bash
cd tests/web-ui-poc/

# 设置环境变量（如果尚未设置）
export AWS_REGION=us-west-2
export RUNTIME_ARN="arn:aws:bedrock-agentcore:us-west-2:<ACCOUNT_ID>:runtime/<RUNTIME_ID>"
export SESSION_ID="ses_<your_session_id>"

# 运行测试
python3 test_ws_bridge.py
```

**预期输出**：

```
[1] Generating signed WebSocket URL...
[2] Connecting WebSocket...
    Connected!
[3] Waiting for connect.challenge (up to 20s)...
    ...
[4] Using provided gateway token
[5] Waiting for hello-ok response...
    OK — hello-ok received!
    Protocol: v3
    Methods: 97, Events: 19
[6] Sending health request...
[7] Sending chat.send...
[8] Collecting streaming responses...
    WS_BRIDGE_OK
    [final] response length: XX chars
============================================================
RESULT: PASS
============================================================
```

### 3. 手动交互测试（可选）

用 Python 直接与 Gateway Protocol 交互：

```python
import asyncio, json, boto3, websockets
from bedrock_agentcore.runtime import AgentCoreRuntimeClient

async def interactive():
    # 获取 Gateway token
    sm = boto3.client('secretsmanager', region_name='us-west-2')
    token = sm.get_secret_value(SecretId='openclaw/gateway-token')['SecretString']
    
    # 生成签名 URL
    client = AgentCoreRuntimeClient(region='us-west-2')
    url = client.generate_presigned_url(
        runtime_arn='<RUNTIME_ARN>',
        session_id='<SESSION_ID>',
        expires=300,
    )
    
    ws = await websockets.connect(url, open_timeout=30)
    
    # 1. Gateway 握手
    await ws.send(json.dumps({
        'type': 'req', 'id': 'c1', 'method': 'connect',
        'params': {
            'minProtocol': 3, 'maxProtocol': 3,
            'client': {'id': 'openclaw-control-ui', 'version': '1.0.0',
                       'platform': 'linux', 'mode': 'backend'},
            'role': 'operator',
            'scopes': ['operator.admin', 'operator.read', 'operator.write'],
            'caps': [], 'commands': [], 'permissions': {},
            'auth': {'token': token},
            'locale': 'en-US', 'userAgent': 'poc/1.0',
        },
    }))
    
    # 读取 hello-ok
    resp = await ws.recv()
    data = json.loads(resp if isinstance(resp, str) else resp.decode())
    print(f"hello-ok: protocol=v{data['payload']['protocol']}")
    
    # 2. 发送聊天消息
    await ws.send(json.dumps({
        'type': 'req', 'id': 'chat1', 'method': 'chat.send',
        'params': {'sessionKey': 'global', 'message': 'Hello!',
                   'idempotencyKey': 'idem_1'},
    }))
    
    # 3. 接收流式响应
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=30)
        data = json.loads(msg if isinstance(msg, str) else msg.decode())
        if data.get('type') == 'event':
            payload = data.get('payload', {})
            if payload.get('state') == 'delta':
                print(payload.get('text', ''), end='', flush=True)
            elif payload.get('state') == 'final':
                print('\n--- DONE ---')
                break
    
    await ws.close()

asyncio.run(interactive())
```

### 4. 测试其他 Gateway Protocol 方法

Gateway Protocol 支持 97 个 RPC 方法。以下是常用方法的测试示例：

```python
# sessions.list — 列出所有会话
await ws.send(json.dumps({
    'type': 'req', 'id': 's1', 'method': 'sessions.list', 'params': {}
}))

# chat.history — 获取对话历史
await ws.send(json.dumps({
    'type': 'req', 'id': 'h1', 'method': 'chat.history',
    'params': {'sessionKey': 'global'}
}))

# health — 网关健康状态
await ws.send(json.dumps({
    'type': 'req', 'id': 'hp1', 'method': 'health', 'params': {}
}))

# agents.files.list — 工作区文件列表
await ws.send(json.dumps({
    'type': 'req', 'id': 'f1', 'method': 'agents.files.list', 'params': {}
}))

# cron.list — 定时任务列表
await ws.send(json.dumps({
    'type': 'req', 'id': 'cr1', 'method': 'cron.list', 'params': {}
}))

# skills.status — 技能状态
await ws.send(json.dumps({
    'type': 'req', 'id': 'sk1', 'method': 'skills.status', 'params': {}
}))
```

## 已知限制

| 限制 | 详情 | 影响 |
|---|---|---|
| 32KB 帧大小 | AgentCore 平台限制，Gateway 自身支持 25MB | 大文件需分块 |
| Gateway Token 认证 | 当前 POC 使用 Secrets Manager 中的共享 token | 生产需要 per-user 认证 |
| Session 需预存在 | WebSocket 依赖已有的 AgentCore session | 需先 HTTP warmup |
| 无浏览器测试 | 浏览器需 OAuth Bearer 认证（需 Cognito 配置） | 当前仅 CLI 测试 |

## 文件说明

| 文件 | 用途 |
|---|---|
| `test_ws_bridge.py` | 自动化端到端测试脚本 |
| `index.html` | 浏览器测试页面（需配置 OAuth 后使用） |
| `README.md` | 本文件 |
